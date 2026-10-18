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
    #[serde(default)]
    needs_recovery: bool,
    attempts: u64,
    last_error: Option<String>,
}

impl Outbox {
    pub fn new(cfg: &Config) -> Result<Self> {
        let directory = cfg
            .home
            .join(".silicon/outbox")
            .join(cfg.silicon.id.as_deref().unwrap_or_default());
        crate::migrate_account_state(&directory, &[])?;
        let gate = GATES
            .lock()
            .recover()
            .entry(directory.clone())
            .or_default()
            .clone();
        Ok(Self {
            directory,
            home: cfg.home.clone(),
            generation: cfg.generation,
            gate,
        })
    }

    fn path(&self, run: &str) -> Result<PathBuf> {
        Ok(self
            .directory
            .join(format!("{}.json", Uuid::parse_str(run)?)))
    }

    /// Save before attempting delivery. The stable run/slot pair also retains successful
    /// receipts until the input is removed, so retrying a partial flow does not resend them.
    /// The callback's final argument forbids sending again when only a receipt may be checked.
    pub fn deliver(
        &self,
        run: &str,
        slot: usize,
        delivery: &flow::Delivery,
        max_retries: usize,
        send: impl FnMut(&str, &flow::Delivery, bool) -> Result<()>,
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
                    needs_recovery: false,
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
        send: impl FnMut(&str, &flow::Delivery, bool) -> Result<()>,
        ready: impl Fn() -> Result<()>,
    ) -> Result<bool> {
        let _guard = self.gate.lock().recover();
        self.flush_locked(delivery, max_retries, send, ready)
    }

    fn flush_locked(
        &self,
        target: &flow::Delivery,
        max_retries: usize,
        mut send: impl FnMut(&str, &flow::Delivery, bool) -> Result<()>,
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
        'deliveries: for (_, path, slot) in pending {
            let Some(mut record) = read(&path)? else {
                continue;
            };
            let mut succeeded = false;
            for retry in 0..=max_retries {
                ready()?;
                if retry > 0 {
                    let delay = 100_u64.saturating_mul(1_u64 << retry.min(4)).min(1000);
                    for _ in 0..delay / 50 {
                        std::thread::sleep(Duration::from_millis(50));
                        ready()?;
                    }
                }
                let entry = record.deliveries.get_mut(&slot).unwrap();
                entry.attempts = entry.attempts.saturating_add(1);
                let attempt = entry.attempts;
                let delivery = entry.delivery.clone();
                let check_only = entry.needs_recovery;
                save(&path, &mut record)?;
                let key = format!("{}/{slot}", path.file_stem().unwrap().to_string_lossy());
                match send(&key, &delivery, check_only) {
                    Ok(()) => {
                        let entry = record.deliveries.get_mut(&slot).unwrap();
                        entry.delivered = true;
                        entry.needs_recovery = false;
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
                        let message =
                            crate::failure::mask(&self.home, &crate::failure::report(&error), &[]);
                        let entry = record.deliveries.get_mut(&slot).unwrap();
                        entry.last_error = Some(message.clone());
                        if error.is::<AwaitingReceipt>() {
                            entry.needs_recovery = true;
                        }
                        save(&path, &mut record)?;
                        self.note(
                            "error",
                            &format!(
                                "{message}\nVia: {} attempt {attempt}",
                                destination(&delivery)
                            ),
                        );
                        if flow::interrupted(&error) {
                            return Err(error);
                        }
                        if record.deliveries[&slot].needs_recovery {
                            if !check_only {
                                self.note("recovery", &format!("delivery {key} to {} has an unknown outcome; retained in {} for receipt checks or manual recovery without resending; newer messages may proceed", destination(&delivery), path.display()));
                            }
                            continue 'deliveries;
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
                        "{}\nVia: preserved in {} for recovery; other destinations continue",
                        crate::failure::report(&error),
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
            crate::stderr_line(&format!(
                "{}\nVia: recording outbox {kind}: {message}",
                crate::failure::report(&error)
            ));
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
        Ok(bytes) => serde_json::from_slice::<Record>(&bytes)
            .with_context(|| {
                format!(
                    "read retained outgoing messages in {}; the file was left intact",
                    path.display()
                )
            })
            .map(|mut record| {
                // Older versions recorded uncertain delivery only in this generated diagnostic.
                for entry in record.deliveries.values_mut() {
                    if !entry.delivered
                        && entry.last_error.as_deref().is_some_and(|error| {
                            error.contains("timed out after 60s awaiting provider START/INJECTED")
                                || error.contains("provider acknowledgement is still pending; retained the existing dispatch for a later delivery check")
                        })
                    {
                        entry.needs_recovery = true;
                    }
                }
                Some(record)
            }),
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

    fn outbox() -> Outbox {
        let home = std::env::temp_dir().join(format!("silicon-outbox-{}", Uuid::new_v4()));
        Outbox {
            directory: home.join(".silicon/outbox"),
            home,
            generation: Uuid::new_v4(),
            gate: Arc::new(Mutex::new(())),
        }
    }

    fn delivery(message: &str) -> flow::Delivery {
        flow::Delivery {
            isi: "udd".into(),
            message: message.into(),
            session_id: None,
        }
    }

    #[test]
    fn uncertain_delivery_releases_queue_and_survives_until_a_late_receipt() {
        let outbox = outbox();
        let first = Uuid::new_v4().to_string();
        let second = Uuid::new_v4().to_string();
        outbox
            .deliver(
                &first,
                0,
                &delivery("first"),
                0,
                |_, _, _| bail!("temporarily unavailable"),
                || Ok(()),
            )
            .unwrap();
        outbox.complete(&first).unwrap();
        let mut called = Vec::new();
        outbox
            .deliver(
                &second,
                0,
                &delivery("second"),
                2,
                |_, delivery, check_only| {
                    called.push((delivery.message.clone(), check_only));
                    if delivery.message == "first" {
                        Err(AwaitingReceipt.into())
                    } else {
                        Ok(())
                    }
                },
                || Ok(()),
            )
            .unwrap();
        assert_eq!(called, [("first".into(), false), ("second".into(), false)]);
        outbox.complete(&second).unwrap();
        assert!(!outbox.path(&second).unwrap().exists());
        let first_path = outbox.path(&first).unwrap();
        let retained = read(&first_path).unwrap().unwrap();
        assert!(retained.completed);
        assert!(retained.deliveries[&0].needs_recovery);
        assert!(!retained.deliveries[&0].delivered);
        assert_eq!(retained.deliveries[&0].delivery.message, "first");
        assert!(retained.deliveries[&0].last_error.is_some());

        let restarted = Outbox {
            gate: Arc::new(Mutex::new(())),
            ..outbox
        };
        restarted.reconcile(&HashSet::new()).unwrap();
        let mut checks = 0;
        assert!(restarted
            .flush(
                &delivery("second"),
                3,
                |_, _, check_only| {
                    assert!(check_only);
                    checks += 1;
                    bail!("old worker is no longer available")
                },
                || Ok(()),
            )
            .unwrap());
        assert_eq!(checks, 1);
        assert!(first_path.exists());
        restarted
            .deliver(
                &first,
                0,
                &delivery("replayed expression"),
                3,
                |_, delivery, check_only| {
                    assert!(check_only);
                    assert_eq!(delivery.message, "first");
                    Err(AwaitingReceipt.into())
                },
                || Ok(()),
            )
            .unwrap();
        assert!(restarted
            .flush(
                &delivery("first"),
                0,
                |_, _, check_only| {
                    assert!(check_only);
                    Ok(())
                },
                || Ok(()),
            )
            .unwrap());
        assert!(!first_path.exists());
        fs::remove_dir_all(restarted.home).unwrap();
    }

    #[test]
    fn ordinary_failure_keeps_newer_messages_in_order() {
        let outbox = outbox();
        let first = Uuid::new_v4().to_string();
        let second = Uuid::new_v4().to_string();
        for (run, message) in [(&first, "first"), (&second, "second")] {
            outbox
                .deliver(
                    run,
                    0,
                    &delivery(message),
                    0,
                    |_, delivery, check_only| {
                        assert!(!check_only);
                        assert_eq!(delivery.message, "first");
                        bail!("provider unavailable")
                    },
                    || Ok(()),
                )
                .unwrap();
        }
        let retained = read(&outbox.path(&first).unwrap()).unwrap().unwrap();
        assert!(!retained.deliveries[&0].needs_recovery);
        let queued = read(&outbox.path(&second).unwrap()).unwrap().unwrap();
        assert_eq!(queued.deliveries[&0].attempts, 0);
        fs::remove_dir_all(outbox.home).unwrap();
    }

    #[test]
    fn interruption_stops_delivery_even_when_only_checking_a_receipt() {
        for uncertain in [false, true] {
            let outbox = outbox();
            let first = Uuid::new_v4().to_string();
            let second = Uuid::new_v4().to_string();
            outbox
                .deliver(
                    &first,
                    0,
                    &delivery("first"),
                    0,
                    |_, _, _| {
                        if uncertain {
                            Err(AwaitingReceipt.into())
                        } else {
                            bail!("provider unavailable")
                        }
                    },
                    || Ok(()),
                )
                .unwrap();
            let error = outbox
                .deliver(
                    &second,
                    0,
                    &delivery("second"),
                    3,
                    |_, delivery, check_only| {
                        assert_eq!(check_only, uncertain);
                        assert_eq!(delivery.message, "first");
                        Err(anyhow::Error::new(AwaitingReceipt)
                            .context(flow::Interrupted("interpreter stopped".into())))
                    },
                    || Ok(()),
                )
                .unwrap_err();
            assert!(flow::interrupted(&error));
            let retained = read(&outbox.path(&first).unwrap()).unwrap().unwrap();
            assert!(retained.deliveries[&0].needs_recovery);
            let queued = read(&outbox.path(&second).unwrap()).unwrap().unwrap();
            assert_eq!(queued.deliveries[&0].attempts, 0);
            fs::remove_dir_all(outbox.home).unwrap();
        }
    }

    #[test]
    fn legacy_timeout_checks_only_and_allows_newer_messages() {
        for (diagnostic, reconcile) in [
            ("timed out after 60s awaiting provider START/INJECTED; the accepted delivery may still run", false),
            ("provider acknowledgement is still pending; retained the existing dispatch for a later delivery check", true),
        ] {
            let outbox = outbox();
            let first = Uuid::new_v4().to_string();
            let path = outbox.path(&first).unwrap();
            state::write_json(&path, &serde_json::json!({
                "completed": true,
                "deliveries": {"0": {
                    "sequence": 0,
                    "delivery": delivery("first"),
                    "delivered": false,
                    "attempts": 15,
                    "last_error": diagnostic
                }}
            })).unwrap();
            if reconcile {
                outbox.reconcile(&HashSet::new()).unwrap();
                let persisted: serde_json::Value =
                    serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
                assert_eq!(persisted["deliveries"]["0"]["needs_recovery"], true);
            }
            let mut called = Vec::new();
            outbox.deliver(
                &Uuid::new_v4().to_string(),
                0,
                &delivery("second"),
                3,
                |_, delivery, check_only| {
                    called.push((delivery.message.clone(), check_only));
                    if delivery.message == "first" {
                        Err(AwaitingReceipt.into())
                    } else {
                        Ok(())
                    }
                },
                || Ok(()),
            ).unwrap();
            assert_eq!(called, [("first".into(), true), ("second".into(), false)]);
            let persisted: serde_json::Value =
                serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            assert_eq!(persisted["deliveries"]["0"]["needs_recovery"], true);
            assert_eq!(persisted["deliveries"]["0"]["delivered"], false);
            fs::remove_dir_all(outbox.home).unwrap();
        }
    }

    #[test]
    fn legacy_ordinary_failures_and_successes_are_not_parked() {
        let outbox = outbox();
        let path = outbox.path(&Uuid::new_v4().to_string()).unwrap();
        state::write_json(
            &path,
            &serde_json::json!({
                "completed": false,
                "deliveries": {
                    "0": {
                        "sequence": 0,
                        "delivery": delivery("first"),
                        "delivered": false,
                        "attempts": 1,
                        "last_error": "provider unavailable"
                    },
                    "1": {
                        "sequence": 1,
                        "delivery": delivery("second"),
                        "delivered": true,
                        "attempts": 2,
                        "last_error": "timed out after 60s awaiting provider START/INJECTED"
                    }
                }
            }),
        )
        .unwrap();
        let record = read(&path).unwrap().unwrap();
        assert!(!record.deliveries[&0].needs_recovery);
        assert!(!record.deliveries[&1].needs_recovery);
        assert!(record.deliveries[&1].delivered);
        fs::remove_dir_all(outbox.home).unwrap();
    }
}
