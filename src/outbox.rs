//! Durable flow deliveries. A stashed destination is retried when another send targets it.
use crate::{config::Config, flow, state, Recover};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use uuid::Uuid;

/// This dispatch may already be accepted, but its receipt has not settled. Sending it again
/// would duplicate queued work; only a later same-destination trigger checks its receipt.
#[derive(Debug)]
pub(crate) struct AwaitingReceipt;

impl std::fmt::Display for AwaitingReceipt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("provider acknowledgement is still pending; retained the existing dispatch for a later delivery check")
    }
}
impl std::error::Error for AwaitingReceipt {}

static GATES: Mutex<BTreeMap<PathBuf, Arc<Mutex<()>>>> = Mutex::new(BTreeMap::new());

pub(crate) struct Outbox {
    directory: PathBuf,
    home: PathBuf,
    generation: Uuid,
    gate: Arc<Mutex<()>>,
}

#[derive(Serialize, Deserialize)]
struct Record {
    completed: bool,
    deliveries: BTreeMap<usize, Entry>,
}

#[derive(Serialize, Deserialize)]
struct Entry {
    sequence: u64,
    delivery: flow::Delivery,
    delivered: bool,
    attempts: u64,
    last_error: Option<String>,
}

impl Outbox {
    pub fn new(cfg: &Config) -> Self {
        let directory = cfg
            .home
            .join(".silicon/outbox")
            .join(cfg.silicon.id.as_deref().unwrap_or_default())
            .join(cfg.silicon.silicon_org.as_deref().unwrap_or_default());
        let gate = GATES
            .lock()
            .recover()
            .entry(directory.clone())
            .or_default()
            .clone();
        Self {
            directory,
            home: cfg.home.clone(),
            generation: cfg.generation,
            gate,
        }
    }

    fn path(&self, run: &str) -> Result<PathBuf> {
        Ok(self
            .directory
            .join(format!("{}.json", Uuid::parse_str(run)?)))
    }

    /// Save before attempting delivery. The stable run/slot pair also retains successful
    /// receipts until the input is removed, so retrying a partial flow does not resend them.
    pub fn deliver(
        &self,
        run: &str,
        slot: usize,
        delivery: &flow::Delivery,
        max_retries: usize,
        send: impl FnMut(&str, &flow::Delivery) -> Result<()>,
        ready: impl Fn() -> Result<()>,
    ) -> Result<()> {
        // ponytail: one delivery gate per Silicon; split by destination if parallel flow
        // delivery becomes necessary. It also serializes replacement connection generations.
        let _guard = self.gate.lock().recover();
        ready()?;
        let path = self.path(run)?;
        let mut record = read(&path)?.unwrap_or_else(|| Record {
            completed: false,
            deliveries: BTreeMap::new(),
        });
        if let Some(entry) = record.deliveries.get(&slot) {
            if !same_target(&entry.delivery, delivery) {
                bail!("flow delivery {run}/{slot} changed destination while its input was pending; retained original delivery in {}. Restore its routing or recover the retained request before changing destinations", path.display());
            }
            // The persisted message is the plan for this slot, even when its expression
            // (for example a timestamp or shell command) changes on a replay.
            if entry.delivered {
                return Ok(());
            }
        } else {
            record.deliveries.insert(
                slot,
                Entry {
                    sequence: self.next_sequence()?,
                    delivery: delivery.clone(),
                    delivered: false,
                    attempts: 0,
                    last_error: None,
                },
            );
            save(&path, &mut record)?;
            self.note(
                "queued",
                &format!("{run}/{slot} for {}", destination(delivery)),
            );
        }
        self.flush_locked(delivery, max_retries, send, ready)
            .map(|_| ())
    }

    /// A direct send can also wake an older stashed flow delivery for this destination.
    pub fn flush(
        &self,
        delivery: &flow::Delivery,
        max_retries: usize,
        send: impl FnMut(&str, &flow::Delivery) -> Result<()>,
        ready: impl Fn() -> Result<()>,
    ) -> Result<bool> {
        let _guard = self.gate.lock().recover();
        self.flush_locked(delivery, max_retries, send, ready)
    }

    fn flush_locked(
        &self,
        target: &flow::Delivery,
        max_retries: usize,
        mut send: impl FnMut(&str, &flow::Delivery) -> Result<()>,
        ready: impl Fn() -> Result<()>,
    ) -> Result<bool> {
        let paths = match fs::read_dir(&self.directory) {
            Ok(paths) => paths,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(true),
            Err(error) => return Err(error).context("read outgoing deliveries"),
        };
        let mut pending = Vec::new();
        // ponytail: scan pending request files; add a destination index if the stash grows large.
        for path in paths {
            let path = path?.path();
            if path.extension().and_then(|v| v.to_str()) != Some("json") {
                continue;
            }
            if let Some(record) = self.read_or_quarantine(&path)? {
                for (slot, entry) in &record.deliveries {
                    if !entry.delivered && same_target(&entry.delivery, target) {
                        pending.push((entry.sequence, path.clone(), *slot));
                    }
                }
            }
        }
        pending.sort();
        for (_, path, slot) in pending {
            let Some(mut record) = read(&path)? else {
                continue;
            };
            let mut succeeded = false;
            for retry in 0..=max_retries {
                ready()?;
                if retry > 0 {
                    let delay = if cfg!(test) {
                        0
                    } else {
                        100_u64.saturating_mul(1_u64 << retry.min(4)).min(1000)
                    };
                    for _ in 0..delay / 50 {
                        std::thread::sleep(Duration::from_millis(50));
                        ready()?;
                    }
                }
                let entry = record.deliveries.get_mut(&slot).unwrap();
                entry.attempts = entry.attempts.saturating_add(1);
                let attempt = entry.attempts;
                let delivery = entry.delivery.clone();
                save(&path, &mut record)?;
                let key = format!("{}/{slot}", path.file_stem().unwrap().to_string_lossy());
                match send(&key, &delivery) {
                    Ok(()) => {
                        let entry = record.deliveries.get_mut(&slot).unwrap();
                        entry.delivered = true;
                        entry.last_error = None;
                        save(&path, &mut record)?;
                        self.note(
                            "delivered",
                            &format!("{} after {attempt} attempt(s)", destination(&delivery)),
                        );
                        succeeded = true;
                        break;
                    }
                    Err(error) => {
                        let message = crate::failure::mask(&self.home, &format!("{error:#}"), &[]);
                        record.deliveries.get_mut(&slot).unwrap().last_error =
                            Some(message.clone());
                        save(&path, &mut record)?;
                        self.note(
                            "error",
                            &format!("{} attempt {attempt}: {message}", destination(&delivery)),
                        );
                        if flow::interrupted(&error) {
                            return Err(error);
                        }
                        if error.is::<AwaitingReceipt>() {
                            self.note("stashed", &format!("{} is awaiting its accepted dispatch; retained for the next same-destination send", destination(&delivery)));
                            return Ok(false);
                        }
                    }
                }
            }
            if !succeeded {
                self.note("stashed", &format!("{} exhausted {max_retries} retries; retained in {} and will retry when the next message targets this ISI/session", destination(target), path.display()));
                // Keep older messages ahead of newer ones for the same destination.
                return Ok(false);
            }
        }
        Ok(true)
    }

    /// Called only after durable removal of the incoming request. Pending deliveries stay;
    /// successful receipts no longer need to protect a replay of that request.
    pub fn complete(&self, run: &str) -> Result<()> {
        let _guard = self.gate.lock().recover();
        let path = self.path(run)?;
        if let Some(mut record) = read(&path)? {
            record.completed = true;
            save(&path, &mut record)?;
        }
        Ok(())
    }

    /// Reconnect closes the crash window between removing an inbox request and deleting
    /// its successful receipts. Unsent messages stay available for destination recovery.
    pub fn reconcile(&self, pending: &HashSet<String>) -> Result<()> {
        let _guard = self.gate.lock().recover();
        let paths = match fs::read_dir(&self.directory) {
            Ok(paths) => paths,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error).context("read outgoing deliveries"),
        };
        for path in paths {
            let path = path?.path();
            if path.extension().and_then(|v| v.to_str()) != Some("json") {
                continue;
            }
            let Some(run) = path.file_stem().and_then(|v| v.to_str()) else {
                continue;
            };
            if pending.contains(run) {
                continue;
            }
            if let Some(mut record) = self.read_or_quarantine(&path)? {
                record.completed = true;
                save(&path, &mut record)?;
            }
        }
        Ok(())
    }

    /// Global enqueue order under the delivery gate, including interleaved direct event
    /// runs. A wall-clock timestamp would reorder work when the system clock moves back.
    fn next_sequence(&self) -> Result<u64> {
        let paths = match fs::read_dir(&self.directory) {
            Ok(paths) => paths,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
            Err(error) => return Err(error).context("read outgoing deliveries"),
        };
        let mut sequence = 0;
        for path in paths {
            let path = path?.path();
            if path.extension().and_then(|v| v.to_str()) != Some("json") {
                continue;
            }
            if let Some(record) = self.read_or_quarantine(&path)? {
                for entry in record.deliveries.values() {
                    sequence = sequence.max(
                        entry
                            .sequence
                            .checked_add(1)
                            .context("outbox sequence overflow")?,
                    );
                }
            }
        }
        Ok(sequence)
    }

    fn read_or_quarantine(&self, path: &Path) -> Result<Option<Record>> {
        match read(path) {
            Err(error) if error.is::<serde_json::Error>() => {
                let retained = path.with_extension(format!("corrupt-{}", Uuid::new_v4()));
                fs::rename(path, &retained).context("retain corrupt outgoing delivery journal")?;
                fs::File::open(path.parent().unwrap())?.sync_all()?;
                self.note(
                    "error",
                    &format!(
                        "{error:#}; preserved in {} for recovery; other destinations continue",
                        retained.display()
                    ),
                );
                Ok(None)
            }
            result => result,
        }
    }

    fn note(&self, kind: &str, message: &str) {
        if let Err(error) =
            crate::log_line_scoped(&self.home, Some(self.generation), kind, "outbox", message)
        {
            crate::stderr_line(&format!("{error:#}; outbox {kind}: {message}"));
        }
    }
}

fn same_target(a: &flow::Delivery, b: &flow::Delivery) -> bool {
    a.isi == b.isi && a.session_id == b.session_id
}

fn destination(delivery: &flow::Delivery) -> String {
    match &delivery.session_id {
        Some(session) => format!("{}:{session}", delivery.isi),
        None => delivery.isi.clone(),
    }
}

fn read(path: &Path) -> Result<Option<Record>> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .with_context(|| {
                format!(
                    "read retained outgoing messages in {}; the file was left intact",
                    path.display()
                )
            })
            .map(Some),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("read {}", path.display())),
    }
}

fn save(path: &Path, record: &mut Record) -> Result<()> {
    if record.completed {
        record.deliveries.retain(|_, entry| !entry.delivered);
        if record.deliveries.is_empty() {
            fs::remove_file(path)
                .with_context(|| format!("remove completed outbox {}", path.display()))?;
            fs::File::open(path.parent().unwrap())?.sync_all()?;
            return Ok(());
        }
    }
    state::write_json(path, record)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(home: &Path) -> Config {
        let mut cfg: Config = serde_yaml::from_str("silicon: {id: 'si:test', org_id: org, SILICON_ORG: org}\nisi: {}\naccess: {}\nflow: []\n").unwrap();
        cfg.home = home.to_owned();
        cfg
    }

    fn message(session: &str, text: &str) -> flow::Delivery {
        flow::Delivery {
            isi: "trainer".into(),
            session_id: Some(session.into()),
            message: text.into(),
        }
    }

    #[test]
    fn retries_stashes_and_recovers_only_the_same_destination_after_restart() -> Result<()> {
        let home = tempfile::tempdir()?;
        let cfg = config(home.path());
        let outbox = Outbox::new(&cfg);
        let first = Uuid::new_v4().to_string();
        let second = Uuid::new_v4().to_string();
        let third = Uuid::new_v4().to_string();
        let mut attempts = 0;
        outbox.deliver(
            &first,
            0,
            &message("alice", "first"),
            2,
            |_, _| {
                attempts += 1;
                bail!("provider unavailable")
            },
            || Ok(()),
        )?;
        assert_eq!(attempts, 3);
        outbox.complete(&first)?;
        let stored = read(&outbox.path(&first)?)?.unwrap();
        assert_eq!(stored.deliveries[&0].attempts, 3);
        assert!(stored.deliveries[&0]
            .last_error
            .as_ref()
            .unwrap()
            .contains("provider unavailable"));

        let restarted = Outbox::new(&cfg);
        let mut sent = Vec::new();
        restarted.deliver(
            &second,
            0,
            &message("bob", "other owner"),
            2,
            |_, delivery| {
                sent.push(delivery.clone());
                Ok(())
            },
            || Ok(()),
        )?;
        assert_eq!(sent, [message("bob", "other owner")]);
        assert!(outbox.path(&first)?.exists());
        restarted.complete(&second)?;
        restarted.deliver(
            &third,
            0,
            &message("alice", "latest"),
            2,
            |_, delivery| {
                sent.push(delivery.clone());
                Ok(())
            },
            || Ok(()),
        )?;
        assert_eq!(
            sent,
            [
                message("bob", "other owner"),
                message("alice", "first"),
                message("alice", "latest")
            ]
        );
        assert!(!outbox.path(&first)?.exists());
        // A retry of the input after another step failed must not redeliver this group.
        restarted.deliver(
            &third,
            0,
            &message("alice", "latest"),
            2,
            |_, _| panic!("delivered twice"),
            || Ok(()),
        )?;
        restarted.complete(&third)?;
        assert_eq!(fs::read_dir(&outbox.directory)?.count(), 0);
        Ok(())
    }

    #[test]
    fn interruption_and_changed_flow_retain_original_messages() -> Result<()> {
        let home = tempfile::tempdir()?;
        let outbox = Outbox::new(&config(home.path()));
        let run = Uuid::new_v4().to_string();
        let delivery = message("alice", "original");
        let error = outbox
            .deliver(
                &run,
                0,
                &delivery,
                10,
                |_, _| Err(flow::Interrupted("disconnect".into()).into()),
                || Ok(()),
            )
            .unwrap_err();
        assert!(flow::interrupted(&error));
        assert_eq!(
            read(&outbox.path(&run)?)?.unwrap().deliveries[&0].attempts,
            1
        );
        let changed = outbox
            .deliver(
                &run,
                0,
                &message("bob", "changed"),
                10,
                |_, _| panic!("must not send changed routing"),
                || Ok(()),
            )
            .unwrap_err();
        assert!(changed
            .to_string()
            .contains("changed destination while its input was pending"));
        outbox.deliver(&run, 0, &delivery, 0, |_, _| Ok(()), || Ok(()))?;
        outbox.complete(&run)?;
        Ok(())
    }

    #[test]
    fn pending_receipts_wait_for_next_trigger_and_replays_keep_the_original_message() -> Result<()>
    {
        let home = tempfile::tempdir()?;
        let cfg = config(home.path());
        let outbox = Outbox::new(&cfg);
        let run = Uuid::new_v4().to_string();
        let mut attempts = 0;
        outbox.deliver(
            &run,
            0,
            &message("alice", "original timestamp"),
            10,
            |key, _| {
                assert_eq!(key, format!("{run}/0"));
                attempts += 1;
                Err(AwaitingReceipt.into())
            },
            || Ok(()),
        )?;
        assert_eq!(
            attempts, 1,
            "uncertain acceptance must not immediately dispatch again"
        );
        outbox.deliver(
            &run,
            0,
            &message("alice", "new timestamp"),
            10,
            |_, delivery| {
                assert_eq!(delivery.message, "original timestamp");
                Ok(())
            },
            || Ok(()),
        )?;
        // A crash after inbox removal leaves this successful receipt. Reconnect cleans it.
        Outbox::new(&cfg).reconcile(&HashSet::new())?;
        assert!(!outbox.path(&run)?.exists());
        Ok(())
    }

    #[test]
    fn reconnect_keeps_pending_inputs_and_stashes_and_quarantines_corruption() -> Result<()> {
        let home = tempfile::tempdir()?;
        let cfg = config(home.path());
        let outbox = Outbox::new(&cfg);
        let pending = Uuid::new_v4().to_string();
        let stashed = Uuid::new_v4().to_string();
        outbox.deliver(
            &pending,
            0,
            &message("alice", "sent"),
            0,
            |_, _| Ok(()),
            || Ok(()),
        )?;
        outbox.deliver(
            &stashed,
            0,
            &message("bob", "unsent"),
            0,
            |_, _| bail!("offline"),
            || Ok(()),
        )?;
        outbox.reconcile(&HashSet::from([pending.clone()]))?;
        assert!(!read(&outbox.path(&pending)?)?.unwrap().completed);
        assert!(read(&outbox.path(&stashed)?)?.unwrap().completed);
        let corrupt = outbox.path(&Uuid::new_v4().to_string())?;
        fs::write(&corrupt, "retain this damage")?;
        let mut sent = Vec::new();
        outbox.flush(
            &message("bob", "next"),
            0,
            |_, delivery| {
                sent.push(delivery.clone());
                Ok(())
            },
            || Ok(()),
        )?;
        assert_eq!(sent, [message("bob", "unsent")]);
        assert!(!corrupt.exists());
        assert!(fs::read_dir(&outbox.directory)?.any(|entry| {
            let path = entry.unwrap().path();
            path.extension()
                .unwrap_or_default()
                .to_string_lossy()
                .starts_with("corrupt-")
                && fs::read_to_string(path).unwrap() == "retain this damage"
        }));
        Ok(())
    }

    #[test]
    fn interleaved_requests_do_not_overtake_older_stashed_messages() -> Result<()> {
        let home = tempfile::tempdir()?;
        let outbox = Outbox::new(&config(home.path()));
        let first_run = Uuid::new_v4().to_string();
        let second_run = Uuid::new_v4().to_string();
        outbox.deliver(
            &first_run,
            0,
            &message("alice", "first destination"),
            0,
            |_, _| Ok(()),
            || Ok(()),
        )?;
        outbox.deliver(
            &second_run,
            0,
            &message("bob", "older"),
            0,
            |_, _| bail!("offline"),
            || Ok(()),
        )?;
        outbox.complete(&second_run)?;
        let mut sent = Vec::new();
        outbox.deliver(
            &first_run,
            1,
            &message("bob", "newer"),
            0,
            |_, delivery| {
                sent.push(delivery.message.clone());
                Ok(())
            },
            || Ok(()),
        )?;
        assert_eq!(sent, ["older", "newer"]);
        Ok(())
    }

    #[test]
    fn failed_storage_never_attempts_delivery_and_corruption_is_not_discarded() -> Result<()> {
        let home = tempfile::tempdir()?;
        let outbox = Outbox::new(&config(home.path()));
        let run = Uuid::new_v4().to_string();
        fs::create_dir_all(&outbox.directory)?;
        fs::write(outbox.path(&run)?, "broken retained message")?;
        assert!(outbox
            .deliver(
                &run,
                0,
                &message("alice", "hello"),
                0,
                |_, _| panic!("must persist before delivery"),
                || Ok(())
            )
            .is_err());
        assert_eq!(
            fs::read_to_string(outbox.path(&run)?)?,
            "broken retained message"
        );
        Ok(())
    }
}
