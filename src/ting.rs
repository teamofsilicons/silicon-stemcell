//! Durably accept JSON webhook requests before running flows; deduplicate canonical Ting batches.
use crate::Recover;
use crate::{auth, config::Config, failure, state};
use anyhow::{anyhow, bail, Context, Result};
use serde::de::{self, DeserializeSeed, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet, HashSet},
    fs,
    io::{BufReader, Read},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    process::Output,
    sync::{Arc, Mutex},
};

/// Queue state per inbox directory, shared by every connection generation that uses it.
// ponytail: queue I/O is serialized per directory; one Silicon's backlog never stalls another's.
static INBOXES: Mutex<BTreeMap<PathBuf, Arc<Mutex<Shared>>>> = Mutex::new(BTreeMap::new());

/// Ting retains at most three calendar months; keep deduplication longer than that.
const SEEN_SECONDS: i64 = 100 * 86400;
/// 2025-01-01. No ting was accepted earlier, so an older stamp came from a clock that was
/// not set yet (a board without a clock battery, a restored VM snapshot).
const CLOCK_FLOOR: i64 = 1_735_689_600;

/// Pending work one inbox holds before it answers "deliver again later": weeks of a blocked
/// flow at ordinary volume. Every batch is its own file, so accepting or finishing one costs
/// the same at any backlog; the cap bounds the disk a stuck flow can fill.
#[derive(Clone, Copy)]
struct Limits {
    batches: usize,
    bytes: u64,
}
const LIMITS: Limits = Limits {
    batches: 10_000,
    bytes: 256 * 1024 * 1024,
};

/// The inbox cannot take this request now: it is full, or its storage failed. Nothing was
/// saved, so the sender must retry later; the server answers HTTP 503.
#[derive(Debug)]
pub struct Unavailable(pub String);

impl std::fmt::Display for Unavailable {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for Unavailable {}

pub struct Inbox {
    directory: PathBuf,
    /// Where a corrupt file is reported; an inbox built without a Silicon has none.
    home: Option<PathBuf>,
    generation: Option<uuid::Uuid>,
    shared: Arc<Mutex<Shared>>,
    limits: Limits,
}

#[derive(Default)]
struct Shared {
    /// A flow is running this directory's head batch; a replacement generation waits for it.
    active: bool,
    /// The queue directory as last listed or written. While it is unchanged, an idle or
    /// blocked processor only stats it instead of listing and reading every batch again.
    queue: Option<Queue>,
    /// seen.json as last read or written, for the same reason.
    seen: Option<Seen>,
    /// Why the head batch's last run failed, for the answer a full inbox gives.
    blocked: Option<String>,
    /// The last failure to record processed IDs, so a repeat is not reported again.
    unrecorded: Option<String>,
}

/// How often each queue directory was listed, so a test can tell a stat from a scan.
#[cfg(test)]
static SCANS: Mutex<BTreeMap<PathBuf, usize>> = Mutex::new(BTreeMap::new());

/// The pending batches, oldest first by the sequence number that starts each file name.
struct Queue {
    stamp: Option<Stamp>,
    batches: BTreeMap<u64, Pending>,
    bytes: u64,
}

struct Pending {
    name: String,
    id: String,
    bytes: u64,
    /// The ting IDs it holds: taken until the batch finishes and they move to seen.json.
    tings: Vec<String>,
}

struct Seen {
    stamp: Option<Stamp>,
    ids: BTreeMap<String, i64>,
}

/// Drop a batch from the index once its file is gone or moved aside.
fn forget(queue: &mut Queue, sequence: u64) {
    if let Some(pending) = queue.batches.remove(&sequence) {
        queue.bytes = queue.bytes.saturating_sub(pending.bytes);
    }
}

/// The claimed head batch.
struct Head {
    sequence: u64,
    name: String,
    batch: Value,
}

/// What a path held when it was read. The interpreter is the only writer, so this only has
/// to notice outside changes, such as a batch file put back by hand; seen.json is replaced
/// by a rename on every write, which always changes its inode.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Stamp {
    device: u64,
    inode: u64,
    length: u64,
    modified: (i64, i64),
}

impl Stamp {
    fn of(metadata: &fs::Metadata) -> Self {
        Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            length: metadata.len(),
            modified: (metadata.mtime(), metadata.mtime_nsec()),
        }
    }
}

/// Read one stored request at a time when rebuilding the queue index.
#[derive(Deserialize)]
struct Stored {
    id: String,
    #[serde(default)]
    request: Value,
}

/// Releases a directory's flow claim however `process` leaves it, a panic included, so one
/// bad batch cannot stop that Silicon's inbox for the rest of the interpreter's life.
struct Claim(Arc<Mutex<Shared>>);

impl Drop for Claim {
    fn drop(&mut self) {
        self.0.lock().recover().active = false;
    }
}

fn stamp(path: &Path) -> Result<Option<Stamp>> {
    match fs::metadata(path) {
        Ok(metadata) => Ok(Some(Stamp::of(&metadata))),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("inspect {}", path.display())),
    }
}

/// A queued batch's file name is `<20-digit sequence>-<batch UUID>.json`, so names sort in
/// arrival order. Anything else in the directory (a staged write, a file moved aside,
/// Finder's litter) is not a batch.
fn sequence(name: &str) -> Option<u64> {
    let (sequence, id) = name.strip_suffix(".json")?.split_once('-')?;
    if sequence.len() != 20
        || !sequence.bytes().all(|byte| byte.is_ascii_digit())
        || id.len() != 36
        || uuid::Uuid::parse_str(id).is_err()
    {
        return None;
    }
    sequence.parse().ok()
}

/// Deduplication ages by the wall clock, which can be unset at boot or jump. A stamp from
/// the future, or from before any ting existed, counts as accepted now: it is kept at most
/// SEEN_SECONDS from now, never forever, and never dropped early.
fn settle(seen: &mut BTreeMap<String, i64>, now: i64) {
    seen.retain(|_, accepted| {
        if *accepted > now || (*accepted < CLOCK_FLOOR && now >= CLOCK_FLOOR) {
            *accepted = now;
        }
        now - *accepted < SEEN_SECONDS
    });
}

/// The ting IDs a stored batch holds.
fn ids(batch: &Value) -> impl Iterator<Item = &str> {
    canonical_tings(&batch["request"])
        .into_iter()
        .flatten()
        .filter_map(|ting| ting["id"].as_str())
}

/// Streams a journal from before the queue directory, handing over each pending batch as
/// soon as it is parsed, so a journal of any size moves in memory bounded by one batch.
struct Legacy<'a> {
    seen: &'a mut BTreeMap<String, i64>,
    batch: &'a mut dyn FnMut(Value) -> Result<()>,
    /// Why handing a batch over failed; the parser only carries a short marker.
    failure: &'a mut Option<anyhow::Error>,
}

impl<'de> DeserializeSeed<'de> for Legacy<'_> {
    type Value = ();

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_map(self)
    }
}

impl<'de> Visitor<'de> for Legacy<'_> {
    type Value = ();

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a Ting inbox object with `seen` and `pending`")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        while let Some(key) = map.next_key::<String>()? {
            match key.as_str() {
                "seen" => self.seen.extend(map.next_value::<BTreeMap<String, i64>>()?),
                "pending" => map.next_value_seed(Batches {
                    batch: &mut *self.batch,
                    failure: &mut *self.failure,
                })?,
                _ => {
                    map.next_value::<IgnoredAny>()?;
                }
            }
        }
        Ok(())
    }
}

struct Batches<'a> {
    batch: &'a mut dyn FnMut(Value) -> Result<()>,
    failure: &'a mut Option<anyhow::Error>,
}

impl<'de> DeserializeSeed<'de> for Batches<'_> {
    type Value = ();

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_seq(self)
    }
}

impl<'de> Visitor<'de> for Batches<'_> {
    type Value = ();

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("a list of pending Ting batches")
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut batches: A) -> Result<(), A::Error> {
        while let Some(batch) = batches.next_element::<Value>()? {
            if let Err(error) = (self.batch)(batch) {
                *self.failure = Some(error);
                return Err(de::Error::custom("a pending batch could not be queued"));
            }
        }
        Ok(())
    }
}

impl Inbox {
    pub fn new(cfg: &Config) -> Result<Self> {
        let root = cfg.home.join(".silicon/ting");
        if fs::symlink_metadata(&root).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            let target = fs::read_link(&root).map_or_else(
                |error| format!("an unreadable target: {error}"),
                |target| target.display().to_string(),
            );
            bail!(
                "Ting state directory {} must not be a symlink; it points to {target}",
                root.display()
            );
        }
        let expected = cfg.silicon.id.as_deref();
        match fs::read_dir(&root) {
            Ok(entries) => {
                let mut stray = Vec::new();
                for entry in entries {
                    let entry = entry.with_context(|| format!("list {}", root.display()))?;
                    let path = entry.path();
                    let kind = entry
                        .file_type()
                        .with_context(|| format!("inspect {}", path.display()))?;
                    if kind.is_file() {
                        // Finder's .DS_Store and AppleDouble `._*` land here; a plain file
                        // cannot hold a Silicon's Ting state, so it orphans nothing.
                        continue;
                    }
                    if !kind.is_dir() {
                        let kind = if kind.is_symlink() {
                            "a symlink"
                        } else {
                            "not a directory"
                        };
                        stray.push(format!("{} is {kind}", path.display()));
                    } else if entry.file_name().to_str() != expected {
                        stray.push(format!(
                            "{} belongs to Silicon ID {:?}",
                            path.display(),
                            entry.file_name()
                        ));
                    }
                }
                if !stray.is_empty() {
                    bail!(
                        "Ting state belongs to a different Silicon ID or is not an ordinary directory; {} may hold only this Silicon's directory {}, but:\n{}\nStop the interpreter and follow docs/PUBLIC-IDENTIFIER-MIGRATION.md before reconnecting",
                        root.display(),
                        expected.map_or("(this Silicon has no ID)".to_owned(), |id| format!("{id:?}")),
                        stray.join("\n")
                    );
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| {
                    format!("inspect retained Ting namespaces in {}", root.display())
                })
            }
        }
        let mut inbox = Self::at(
            root.join(expected.unwrap_or_default())
                .join(cfg.silicon.silicon_org.as_deref().unwrap_or_default()),
        );
        inbox.home = Some(cfg.home.clone());
        inbox.generation = Some(cfg.generation).filter(|generation| !generation.is_nil());
        Ok(inbox)
    }

    fn at(directory: PathBuf) -> Self {
        let shared = INBOXES
            .lock()
            .recover()
            .entry(directory.clone())
            .or_default()
            .clone();
        Self {
            directory,
            home: None,
            generation: None,
            shared,
            limits: LIMITS,
        }
    }

    /// One file per pending batch.
    fn queue_directory(&self) -> PathBuf {
        self.directory.join("pending")
    }

    /// The single journal every earlier interpreter kept; moved into the queue on first use.
    fn journal(&self) -> PathBuf {
        self.directory.join("inbox.json")
    }

    fn seen_path(&self) -> PathBuf {
        self.directory.join("seen.json")
    }

    /// Say it in daemon.log, and in the Silicon's log when this inbox belongs to one.
    fn report(&self, message: &str) {
        let masked = match &self.home {
            Some(home) => failure::mask(home, message, &[]),
            None => failure::mask_all(message),
        };
        crate::stderr_line(&format!("ting: {masked}"));
        if let Some(home) = &self.home {
            if let Err(error) =
                crate::log_line_scoped(home, self.generation, "error", "ting", message)
            {
                crate::stderr_line(&format!(
                    "{error:#}; the ting error it was recording: {masked}"
                ));
            }
        }
    }

    /// Move a file that no longer parses aside, as evidence, so the inbox carries on
    /// without it. Reported in full once: the moved file is never read again.
    fn quarantine(&self, path: &Path, problem: &str, consequence: &str) -> Result<()> {
        let aside = crate::numbered(
            path,
            &format!(
                "corrupt-{}",
                chrono::Utc::now().format("%Y%m%dT%H%M%S%.6fZ")
            ),
        );
        fs::rename(path, &aside).with_context(|| {
            format!(
                "{} is corrupt ({problem}), and it could not be moved aside to {}",
                path.display(),
                aside.display()
            )
        })?;
        let durable = match path.parent().map(fs::File::open) {
            Some(Ok(directory)) => directory.sync_all().err(),
            Some(Err(error)) => Some(error),
            None => None,
        }
        .map_or(String::new(), |error| {
            format!(
                " (the move may not survive a power loss: syncing its directory failed: {error})"
            )
        });
        self.report(&format!(
            "{} is corrupt ({problem}); moved it to {}{durable} and continued without it: {consequence}",
            path.display(),
            aside.display()
        ));
        Ok(())
    }

    /// The queue index, listing the directory again only when it changed. A journal from
    /// before the queue directory moves into it first.
    fn queue<'a>(
        &self,
        slot: &'a mut Option<Queue>,
        seen: &mut Option<Seen>,
    ) -> Result<&'a mut Queue> {
        let current = stamp(&self.queue_directory())?;
        if slot.as_ref().is_some_and(|queue| queue.stamp != current) {
            *slot = None;
        }
        match slot {
            Some(queue) => Ok(queue),
            None => {
                self.migrate(seen)?;
                Ok(slot.insert(self.scan()?))
            }
        }
    }

    /// Every batch file, oldest first, with the ting IDs it holds. A file that no longer
    /// parses is moved aside, so one damaged batch never stops the ones behind it.
    fn scan(&self) -> Result<Queue> {
        let directory = self.queue_directory();
        let mut queue = Queue {
            stamp: stamp(&directory)?,
            batches: BTreeMap::new(),
            bytes: 0,
        };
        #[cfg(test)]
        {
            *SCANS
                .lock()
                .recover()
                .entry(self.directory.clone())
                .or_default() += 1;
        }
        let listing = || format!("list the Ting queue {}", directory.display());
        let entries = match fs::read_dir(&directory) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(queue),
            Err(error) => return Err(error).with_context(listing),
        };
        let mut moved = false;
        for entry in entries {
            let entry = entry.with_context(listing)?;
            let Some((sequence, name)) = entry
                .file_name()
                .to_str()
                .and_then(|name| Some((sequence(name)?, name.to_owned())))
            else {
                continue;
            };
            if !entry.file_type().is_ok_and(|kind| kind.is_file()) {
                continue;
            }
            let path = entry.path();
            let bytes = match fs::read(&path) {
                Ok(bytes) => bytes,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Err(error)
                        .with_context(|| format!("read pending Ting batch {}", path.display()))
                }
            };
            match serde_json::from_slice::<Stored>(&bytes) {
                Ok(stored) => {
                    let size = bytes.len() as u64;
                    queue.bytes += size;
                    queue.batches.insert(
                        sequence,
                        Pending {
                            name,
                            id: stored.id,
                            bytes: size,
                            tings: canonical_tings(&stored.request)
                                .into_iter()
                                .flatten()
                                .filter_map(|ting| ting["id"].as_str().map(str::to_owned))
                                .collect(),
                        },
                    );
                }
                Err(error) => {
                    self.quarantine(
                        &path,
                        &format!("{} bytes that do not parse as a pending Ting batch: {error}", bytes.len()),
                        "that batch's flow does not run and its tings stay in the moved file; the batches behind it run as usual",
                    )?;
                    moved = true;
                }
            }
        }
        if moved {
            queue.stamp = stamp(&directory)?;
        }
        Ok(queue)
    }

    /// After the interpreter's own change to the queue directory, remember its new state so
    /// the next look only stats it; list it again if it cannot be inspected.
    fn restamp(&self, slot: &mut Option<Queue>) {
        match stamp(&self.queue_directory()) {
            Ok(current) => {
                if let Some(queue) = slot {
                    queue.stamp = current;
                }
            }
            Err(_) => *slot = None,
        }
    }

    /// Move a journal from before the queue directory into it. inbox.json held every
    /// pending batch and, before seen.json existed, the deduplication IDs too. Each batch
    /// and the IDs are written before the journal is removed; after a crash midway the move
    /// repeats and skips the batches already queued.
    fn migrate(&self, seen: &mut Option<Seen>) -> Result<()> {
        let path = self.journal();
        let reading = || format!("read the Ting inbox journal {}", path.display());
        let file = match fs::File::open(&path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error).with_context(reading),
        };
        let length = file.metadata().with_context(reading)?.len();
        let queued = self.scan()?;
        let mut known: HashSet<String> = queued.batches.values().map(|p| p.id.clone()).collect();
        let mut next = queued
            .batches
            .last_key_value()
            .map_or(1, |(sequence, _)| sequence + 1);
        let directory = self.queue_directory();
        let mut moved = 0;
        let mut write = |mut batch: Value| -> Result<()> {
            let id = match batch.get("id").and_then(Value::as_str) {
                Some(id) => id.to_owned(),
                None => {
                    // Only a hand-edited journal lacks one, so a crash midway may queue such a
                    // batch twice. A batch that is not even an object is kept whole; its flow
                    // gets no request, as it did before.
                    let id = uuid::Uuid::new_v4().to_string();
                    match batch.as_object_mut() {
                        Some(fields) => {
                            fields.insert("id".into(), json!(id));
                        }
                        None => batch = json!({"id": id, "legacy": batch}),
                    }
                    id
                }
            };
            if !known.insert(id.clone()) {
                return Ok(());
            }
            let named = uuid::Uuid::parse_str(&id)
                .ok()
                .filter(|_| id.len() == 36)
                .unwrap_or_else(uuid::Uuid::new_v4);
            state::write_json_compact(&directory.join(format!("{next:020}-{named}.json")), &batch)?;
            next += 1;
            moved += 1;
            Ok(())
        };
        let mut legacy = BTreeMap::new();
        let mut failure = None;
        let parsed = {
            let mut parser = serde_json::Deserializer::from_reader(BufReader::new(file));
            Legacy {
                seen: &mut legacy,
                batch: &mut write,
                failure: &mut failure,
            }
            .deserialize(&mut parser)
            .and_then(|()| parser.end())
        };
        if let Some(error) = failure {
            return Err(error.context(format!(
                "moving the pending batches of {} into {}",
                path.display(),
                directory.display()
            )));
        }
        let complete = match parsed {
            Ok(()) => true,
            Err(error) if error.is_io() => return Err(error).with_context(reading),
            Err(error) => {
                self.quarantine(
                    &path,
                    &format!("{length} bytes that do not parse as a Ting inbox: {error}"),
                    &format!("queued the {moved} pending batches before the damage; any after it do not run and stay in the moved file"),
                )?;
                false
            }
        };
        if !legacy.is_empty() {
            let seen = self.seen(seen)?;
            for (id, accepted) in legacy {
                let kept = seen.ids.entry(id).or_insert(accepted);
                *kept = (*kept).max(accepted);
            }
            self.store_seen(seen)?;
        }
        if complete {
            fs::remove_file(&path).with_context(|| {
                format!(
                    "remove {} after moving it into {}",
                    path.display(),
                    directory.display()
                )
            })?;
            fs::File::open(&self.directory)
                .and_then(|directory| directory.sync_all())
                .with_context(|| format!("sync {}", self.directory.display()))?;
        }
        Ok(())
    }

    /// Ting IDs already taken, each with when, as read from exactly this file.
    fn read_seen(&self) -> Result<Seen> {
        let path = self.seen_path();
        let reading = || format!("read Ting deduplication IDs {}", path.display());
        let empty = || Seen {
            stamp: None,
            ids: BTreeMap::new(),
        };
        let (bytes, found) = match fs::File::open(&path) {
            Ok(mut file) => {
                let found = Stamp::of(&file.metadata().with_context(reading)?);
                let mut bytes = Vec::new();
                file.read_to_end(&mut bytes).with_context(reading)?;
                (bytes, found)
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(empty()),
            Err(error) => return Err(error).with_context(reading),
        };
        match serde_json::from_slice(&bytes) {
            Ok(ids) => Ok(Seen {
                stamp: Some(found),
                ids,
            }),
            Err(error) => {
                self.quarantine(
                    &path,
                    &format!("{} bytes that do not parse as Ting deduplication IDs: {error}", bytes.len()),
                    "deduplication history was reset, so a Ting redelivery of a ting processed before now runs its flow again",
                )?;
                Ok(empty())
            }
        }
    }

    /// The deduplication IDs, reading seen.json again only when it changed.
    fn seen<'a>(&self, slot: &'a mut Option<Seen>) -> Result<&'a mut Seen> {
        let current = stamp(&self.seen_path())?;
        if slot.as_ref().is_some_and(|seen| seen.stamp != current) {
            *slot = None;
        }
        match slot {
            Some(seen) => Ok(seen),
            None => Ok(slot.insert(self.read_seen()?)),
        }
    }

    /// Write the IDs and remember the file that now holds them. Should the write fail, the
    /// IDs still deduplicate in memory until the file changes or the interpreter restarts.
    fn store_seen(&self, seen: &mut Seen) -> Result<()> {
        let path = self.seen_path();
        state::write_json_compact(&path, &seen.ids)?;
        if let Ok(current) = stamp(&path) {
            seen.stamp = current;
        }
        Ok(())
    }

    pub fn accept(&self, request: Value) -> Result<()> {
        let size = serde_json::to_vec(&request)?.len();
        if size > 1024 * 1024 {
            bail!("webhook requests require at most 1 MiB; this request has {size} bytes");
        }
        let mut shared = self.shared.lock().recover();
        self.enqueue(&mut shared, request)
    }

    /// Queue any JSON request. Canonical Ting batches omit previously accepted IDs;
    /// every other request is preserved whole, including fields that happen to be named tings.
    /// The single file makes acceptance and pending deduplication durable together.
    fn enqueue(&self, shared: &mut Shared, mut request: Value) -> Result<()> {
        let unavailable = |error: anyhow::Error| {
            anyhow::Error::new(Unavailable(format!(
                "{:#}",
                error.context(
                    "the webhook request was not accepted, so the sender must deliver it again"
                )
            )))
        };
        let Shared {
            queue: slot,
            seen,
            active,
            blocked,
            ..
        } = shared;
        let queue = self.queue(slot, seen).map_err(unavailable)?;
        let mut taking = Vec::new();
        if let Some(tings) = canonical_tings(&request) {
            let seen = self.seen(seen).map_err(unavailable)?;
            settle(&mut seen.ids, chrono::Utc::now().timestamp());
            let mut taken: HashSet<&str> = queue
                .batches
                .values()
                .flat_map(|pending| pending.tings.iter().map(String::as_str))
                .collect();
            let fresh: Vec<_> = tings
                .iter()
                .filter(|ting| {
                    let id = ting["id"].as_str().unwrap_or_default();
                    !seen.ids.contains_key(id) && taken.insert(id)
                })
                .cloned()
                .collect();
            if fresh.is_empty() {
                return Ok(());
            }
            taking = fresh
                .iter()
                .filter_map(|ting| ting["id"].as_str().map(str::to_owned))
                .collect();
            request["tings"] = Value::Array(fresh);
        }
        let id = uuid::Uuid::new_v4();
        let batch = json!({"id":id,"request":request});
        // What the compact file will hold, newline included.
        let size = serde_json::to_vec(&batch).map_or(0, |bytes| bytes.len() as u64 + 1);
        if queue.batches.len() >= self.limits.batches || queue.bytes + size > self.limits.bytes {
            return Err(self.full(queue, blocked.as_deref(), *active));
        }
        let sequence = queue
            .batches
            .last_key_value()
            .map_or(1, |(sequence, _)| sequence + 1);
        let name = format!("{sequence:020}-{id}.json");
        state::write_json_compact(&self.queue_directory().join(&name), &batch)
            .map_err(unavailable)?;
        queue.batches.insert(
            sequence,
            Pending {
                name,
                id: id.to_string(),
                bytes: size,
                tings: taking,
            },
        );
        queue.bytes += size;
        self.restamp(slot);
        Ok(())
    }

    fn full(&self, queue: &Queue, blocked: Option<&str>, active: bool) -> anyhow::Error {
        let head = queue.batches.first_key_value().map_or_else(
            || "(unknown)".to_owned(),
            |(_, head)| json!(head.id).to_string(),
        );
        let blocking = match (blocked, active) {
            (Some(error), _) => format!("its last run failed: {error}"),
            (None, true) => "its flow is still running".to_owned(),
            (None, false) => "no run of it has failed since the interpreter started".to_owned(),
        };
        anyhow::Error::new(Unavailable(format!(
            "the webhook inbox {} is full: it holds {} pending batches in {} bytes and takes at most {} batches or {} bytes, so the sender must deliver this request again later. The oldest pending batch {head} is not finishing; {blocking}",
            self.directory.display(),
            queue.batches.len(),
            queue.bytes,
            self.limits.batches,
            self.limits.bytes
        )))
    }

    pub fn process(&self, run: impl FnOnce(Value) -> Result<()>) -> Result<()> {
        self.process_identified(|_, request| run(request))
    }

    /// Snapshot retained request IDs before reconnecting the outgoing delivery journal.
    pub(crate) fn pending_ids(&self) -> Result<HashSet<String>> {
        let mut shared = self.shared.lock().recover();
        let Shared { queue, seen, .. } = &mut *shared;
        Ok(self
            .queue(queue, seen)?
            .batches
            .values()
            .map(|batch| batch.id.clone())
            .collect())
    }

    /// The durable request ID stays the same across retries, even for identical JSON payloads.
    pub fn process_identified(&self, run: impl FnOnce(&str, Value) -> Result<()>) -> Result<()> {
        let Some((claim, head)) = self.claim()? else {
            return Ok(());
        };
        // Acceptance stays available while the flow runs. Replacement connections wait
        // for this generation's flow to finish before attempting the same pending batch.
        let id = head.batch["id"]
            .as_str()
            .context("the pending webhook request has no string id")?;
        // A panicking flow fails like any other: the batch stays queued, this thread lives on.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            run(id, head.batch["request"].clone())
        }))
        .unwrap_or_else(|panic| {
            Err(anyhow!(
                "the flow for Ting batch {id} panicked: {}",
                failure::panic_message(&*panic)
            ))
        });
        let mut shared = self.shared.lock().recover();
        let result = match result {
            Ok(()) => {
                shared.blocked = None;
                self.remove(&mut shared, &head)
            }
            Err(error) => {
                shared.blocked = Some(format!("{error:#}"));
                Err(if crate::flow::interrupted(&error) {
                    error.context(format!(
                        "the flow for Ting batch {id} was interrupted; the batch stays in the Ting inbox and runs again on the next connection"
                    ))
                } else {
                    error
                })
            }
        };
        drop(shared);
        drop(claim);
        result
    }

    /// The oldest pending batch, claimed for one flow run. None while a flow already runs
    /// from this directory, or when nothing is pending.
    fn claim(&self) -> Result<Option<(Claim, Head)>> {
        let mut shared = self.shared.lock().recover();
        if shared.active {
            return Ok(None);
        }
        loop {
            let Shared {
                queue: slot,
                seen,
                active,
                ..
            } = &mut *shared;
            let queue = self.queue(slot, seen)?;
            let Some((&sequence, head)) = queue.batches.first_key_value() else {
                return Ok(None);
            };
            let name = head.name.clone();
            let path = self.queue_directory().join(&name);
            let problem = match fs::read(&path) {
                Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
                    Ok(batch) => {
                        *active = true;
                        return Ok(Some((
                            Claim(self.shared.clone()),
                            Head {
                                sequence,
                                name,
                                batch,
                            },
                        )));
                    }
                    Err(error) => Some(format!(
                        "{} bytes that do not parse as a pending Ting batch: {error}",
                        bytes.len()
                    )),
                },
                // Removed by hand since the directory was listed.
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => {
                    return Err(error)
                        .with_context(|| format!("read pending Ting batch {}", path.display()))
                }
            };
            if let Some(problem) = &problem {
                self.quarantine(
                    &path,
                    problem,
                    "that batch's flow does not run and its tings stay in the moved file; the batches behind it run as usual",
                )?;
            }
            forget(queue, sequence);
            self.restamp(slot);
        }
    }

    /// Record the finished batch's IDs, then delete its file.
    fn remove(&self, shared: &mut Shared, head: &Head) -> Result<()> {
        let now = chrono::Utc::now().timestamp();
        // The IDs become the deduplication record before the batch leaves the queue.
        let recorded = if canonical_tings(&head.batch["request"]).is_some() {
            self.seen(&mut shared.seen).and_then(|seen| {
                settle(&mut seen.ids, now);
                seen.ids
                    .extend(ids(&head.batch).map(|id| (id.to_owned(), now)));
                self.store_seen(seen)
            })
        } else {
            Ok(())
        };
        match recorded {
            Ok(()) => shared.unrecorded = None,
            Err(error) => {
                // Removing the batch anyway only weakens deduplication of its IDs;
                // keeping it would run its whole flow again.
                let error = format!("{error:#}");
                if shared.unrecorded.as_ref() != Some(&error) {
                    self.report(&format!(
                        "processed Ting batch {} but could not record its ting IDs, so a Ting redelivery of them runs the flow again: {error}",
                        head.batch["id"]
                    ));
                    shared.unrecorded = Some(error);
                }
            }
        }
        let directory = self.queue_directory();
        let path = directory.join(&head.name);
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| {
                    format!(
                        "the flow finished, but its batch {} could not be removed from the Ting inbox, so it will run again",
                        path.display()
                    )
                })
            }
        }
        if let Some(queue) = &mut shared.queue {
            forget(queue, head.sequence);
        }
        self.restamp(&mut shared.queue);
        fs::File::open(&directory)
            .and_then(|directory| directory.sync_all())
            .with_context(|| {
                format!(
                    "the flow finished and its batch {} was removed, but syncing {} failed, so after a power loss the batch may run again",
                    path.display(),
                    directory.display()
                )
            })
    }

    fn hook(&self) -> Result<Option<String>> {
        let path = self.directory.join("hook.json");
        match fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice(&bytes) {
                Ok(id) => Ok(Some(id)),
                Err(error) => {
                    self.quarantine(
                        &path,
                        &format!(
                            "an invalid Ting hook ID: {error}; it held: {}",
                            String::from_utf8_lossy(&bytes).trim_end()
                        ),
                        "the next registration looks the hook up in Ting's registration list, or creates one",
                    )?;
                    Ok(None)
                }
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e).with_context(|| format!("read Ting hook ID {}", path.display())),
        }
    }
    fn save_hook(&self, id: &str) -> Result<()> {
        state::write_json(&self.directory.join("hook.json"), &id)
            .with_context(|| format!("save Ting hook ID {id}"))
    }

    /// What a reconnect will do after a failed registration, for the error that reports it.
    fn reuse(&self) -> String {
        let path = self.directory.join("hook.json");
        match self.hook() {
            Ok(Some(id)) => format!(
                "the saved hook {id} ({}) will be reused on reconnect",
                path.display()
            ),
            Ok(None) => {
                "no hook ID is saved, so reconnect will look for it in Ting's registration list"
                    .to_owned()
            }
            Err(error) => format!("the saved hook ID could not be read back: {error:#}"),
        }
    }

    pub fn register(&self, cfg: &Config, url: &str) -> Result<()> {
        let home = &cfg.home;
        let mut id = self.hook()?;
        if id.is_none() {
            // Recover a completed creation if the interpreter lost its CLI response.
            id = listed(home, url, &self.directory.join("hook.json"))
                .with_context(|| format!("could not inspect Ting registrations for {url}"))?;
            if let Some(id) = &id {
                self.save_hook(id)?;
            }
        }
        self.connect(home, url, id.as_deref()).with_context(|| {
            format!(
                "Ting webhook registration for {url} failed; {}",
                self.reuse()
            )
        })
    }

    fn connect(&self, home: &Path, url: &str, id: Option<&str>) -> Result<()> {
        let saved = self.directory.join("hook.json");
        let mut args = vec!["webhook", url, "--json"];
        if let Some(id) = id {
            args.extend(["--id", id]);
        }
        let (command, result) = ting(home, &args)?;
        let differs = |returned: &str| {
            format!(
                "returned webhook ID {returned:?}, but this interpreter retains {:?} in {}",
                id.unwrap_or_default(),
                saved.display()
            )
        };
        if !result.status.success() {
            // Ting names a hook it created but could not confirm in its JSON stderr.
            // Any other stderr is not an ID and is reported verbatim below.
            let answer = serde_json::from_slice::<Value>(&result.stderr).unwrap_or_default();
            let error = failure::command(home, &command, &result, &[]);
            let Some(returned) = answer
                .pointer("/error/details/webhook_id")
                .and_then(Value::as_str)
            else {
                return Err(error);
            };
            if id.is_some_and(|id| id != returned) {
                return Err(error.context(format!("Ting {}", differs(returned))));
            }
            // A failed save rides along; Ting's own failure stays the root cause.
            return Err(match self.save_hook(returned) {
                Ok(()) => error,
                Err(save) => error.context(format!(
                    "Ting named webhook {returned:?} in its failure, but this interpreter could not retain that ID: {save:#}"
                )),
            });
        }
        let wrong = |problem: &str| failure::answer(home, &command, problem, &result, &[]);
        let answer: Value = serde_json::from_slice(&result.stdout)
            .map_err(|error| wrong(&format!("returned invalid JSON: {error}")))?;
        let returned = answer["id"]
            .as_str()
            .ok_or_else(|| wrong(&format!("returned no webhook `id` (id: {})", answer["id"])))?;
        if id.is_some_and(|id| id != returned) {
            return Err(wrong(&differs(returned)));
        }
        if let Err(save) = self.save_hook(returned) {
            return Err(wrong(&format!(
                "returned webhook ID {returned:?}, but this interpreter could not retain it: {save:#}"
            )));
        }
        if answer["state"] != "connected" {
            return Err(wrong(&format!(
                "returned webhook state {} instead of \"connected\"",
                answer["state"]
            )));
        }
        Ok(())
    }

    pub fn unhook(&self, cfg: &Config) -> Result<()> {
        if let Some(id) = self.hook()? {
            ting(&cfg.home, &["unhook", &id, "--json"])
                .and_then(|(command, result)| {
                    if result.status.success() {
                        Ok(())
                    } else {
                        Err(failure::command(&cfg.home, &command, &result, &[]))
                    }
                })
                .with_context(|| {
                    format!(
                        "Ting webhook removal failed; its stable ID {id} is retained in {}",
                        self.directory.join("hook.json").display()
                    )
                })?;
        }
        Ok(())
    }
}

/// One Ting CLI call, returned with the command line that names it in errors.
fn ting(home: &Path, args: &[&str]) -> Result<(String, Output)> {
    let command = failure::argv("ting", args);
    let output = auth::run_app(home, "ting", args)?;
    Ok((command, output))
}

/// The hook Ting lists for `url`, reading every page so duplicates are all named.
fn listed(home: &Path, url: &str, saved: &Path) -> Result<Option<String>> {
    let mut found = Vec::new();
    let mut cursor = None::<String>;
    let mut pages = BTreeSet::new();
    loop {
        let mut args = vec!["webhook", "list", "--limit", "100", "--json"];
        if let Some(cursor) = &cursor {
            args.extend(["--cursor", cursor]);
        }
        let (command, result) = ting(home, &args)?;
        if !result.status.success() {
            return Err(failure::command(home, &command, &result, &[]));
        }
        let wrong = |problem: &str| failure::answer(home, &command, problem, &result, &[]);
        let list: Value = serde_json::from_slice(&result.stdout)
            .map_err(|error| wrong(&format!("returned invalid JSON: {error}")))?;
        let items = list["items"].as_array().ok_or_else(|| {
            wrong(&format!(
                "returned no `items` array (items: {})",
                list["items"]
            ))
        })?;
        for (index, hook) in items.iter().enumerate() {
            if hook["url"].as_str() == Some(url) {
                let id = hook["id"].as_str().ok_or_else(|| {
                    wrong(&format!(
                        "listed item {index} for {url} without a string `id`: {hook}"
                    ))
                })?;
                found.push(id.to_owned());
            }
        }
        cursor = match &list["next_cursor"] {
            Value::Null => None,
            // A repeated cursor would page forever; say so instead of hanging.
            Value::String(next) if !pages.insert(next.clone()) => {
                return Err(wrong(&format!(
                    "returned `next_cursor` {next:?} a second time, so the list never ends"
                )))
            }
            Value::String(next) => Some(next.clone()),
            other => {
                return Err(wrong(&format!(
                    "returned a `next_cursor` that is neither a string nor null: {other}"
                )))
            }
        };
        if cursor.is_none() {
            break;
        }
    }
    if found.len() > 1 {
        bail!(
            "multiple Ting hooks target this interpreter at {url}: {}; select the retained hook by saving its ID as a JSON string in {} before reconnecting",
            found.join(", "),
            saved.display()
        );
    }
    Ok(found.pop())
}

/// Only the exact Ting envelope opts into ID deduplication. An arbitrary webhook may use
/// the same field names; never discard its fields or treat a malformed event as a Ting ID.
fn canonical_tings(request: &Value) -> Option<&[Value]> {
    let fields = request.as_object()?;
    if fields.len() != 1 {
        return None;
    }
    let tings = fields.get("tings")?.as_array()?;
    if tings.is_empty() || tings.len() > 100 {
        return None;
    }
    tings
        .iter()
        .all(|ting| {
            ting["id"]
                .as_str()
                .is_some_and(|id| !id.is_empty() && id.len() <= 256)
                && ting["type"].as_str().is_some_and(|kind| !kind.is_empty())
                && ting["data"].is_object()
                && ting["metadata"].is_object()
        })
        .then_some(tings.as_slice())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registration_retains_ids_and_recovers_lost_responses() -> Result<()> {
        use std::os::unix::fs::PermissionsExt;

        for mode in ["new", "error", "lost"] {
            let home = tempfile::tempdir()?;
            let mut cfg: Config = serde_json::from_value(json!({
                "silicon":{"id":"si:test", "org_id":"org", "SILICON_ORG":"selected-org"},
                "isi":{}, "access":{}, "flow":[]
            }))?;
            cfg.home = home.path().to_owned();
            state::write_json(&home.path().join(".silicon/org.json"), &"selected-org")?;
            fs::write(home.path().join("mode"), mode)?;
            let executable = home.path().join(".silicon/bin/ting");
            fs::create_dir_all(executable.parent().unwrap())?;
            fs::write(
                &executable,
                r#"#!/bin/sh
set -eu
[ "$SILICON_ORG" = selected-org ]
if [ "$*" = 'iam --json' ]; then echo '{"app_id":"ting"}'; exit 0; fi
printf '%s\n' "$*" >> calls
case "$*" in
  'webhook list --limit 100 --json')
    if [ -f remote-hook ]; then
      echo '{"items":[{"id":"stable-hook","url":"http://test.org.localhost/"}],"next_cursor":null}'
    else
      echo '{"items":[],"next_cursor":null}'
    fi ;;
  'webhook http://test.org.localhost/ --json')
    [ ! -f remote-hook ]
    touch remote-hook
    case "$(cat mode)" in
      error) echo '{"error":{"details":{"webhook_id":"stable-hook"}}}' >&2; exit 1 ;;
      lost) echo 'response lost after creation' >&2; exit 1 ;;
    esac
    echo '{"id":"stable-hook","state":"connected"}' ;;
  'webhook http://test.org.localhost/ --json --id stable-hook')
    [ -f remote-hook ]
    echo '{"id":"stable-hook","state":"connected"}' ;;
  'unhook stable-hook --json')
    [ -f remote-hook ]
    echo '{"id":"stable-hook","state":"disconnected"}' ;;
  *) exit 2 ;;
esac
"#,
            )?;
            fs::set_permissions(&executable, fs::Permissions::from_mode(0o700))?;
            let inbox = Inbox::new(&cfg)?;
            let first = inbox.register(&cfg, "http://test.org.localhost/");
            assert_eq!(first.is_ok(), mode == "new", "{mode}: {first:?}");
            if let Err(error) = &first {
                // Ting's own words reach the reader, not a generic summary.
                let error = format!("{error:#}");
                assert!(
                    error.contains(
                        "`ting webhook http://test.org.localhost/ --json` failed: exit status: 1"
                    ),
                    "{error}"
                );
                let said = if mode == "error" {
                    r#"{"error":{"details":{"webhook_id":"stable-hook"}}}"#
                } else {
                    "response lost after creation"
                };
                assert!(error.contains(&format!("stderr:\n{said}")), "{error}");
                assert!(error.contains("stdout: (empty)"), "{error}");
            }
            assert_eq!(
                inbox.hook()?.as_deref(),
                (mode != "lost").then_some("stable-hook")
            );

            // A fresh instance models reconnect after the CLI reply or interpreter was lost.
            let reconnected = Inbox::new(&cfg)?;
            reconnected.register(&cfg, "http://test.org.localhost/")?;
            assert_eq!(reconnected.hook()?.as_deref(), Some("stable-hook"));
            assert_eq!(
                serde_json::from_slice::<String>(&fs::read(
                    home.path()
                        .join(".silicon/ting/si:test/selected-org/hook.json")
                )?)?,
                "stable-hook"
            );
            reconnected.unhook(&cfg)?;
            let calls = fs::read_to_string(home.path().join("calls"))?;
            let calls: Vec<_> = calls.lines().collect();
            assert_eq!(
                calls
                    .iter()
                    .filter(|call| **call == "webhook http://test.org.localhost/ --json")
                    .count(),
                1
            );
            assert_eq!(
                calls
                    .iter()
                    .filter(|call| **call == "webhook list --limit 100 --json")
                    .count(),
                if mode == "lost" { 2 } else { 1 }
            );
            assert_eq!(
                &calls[calls.len() - 2..],
                [
                    "webhook http://test.org.localhost/ --json --id stable-hook",
                    "unhook stable-hook --json"
                ]
            );
            assert_eq!(reconnected.hook()?.as_deref(), Some("stable-hook"));
        }
        Ok(())
    }

    #[test]
    fn legacy_namespace_cannot_silently_orphan_pending_deliveries() -> Result<()> {
        let home = tempfile::tempdir()?;
        let mut cfg: Config = serde_json::from_value(json!({
            "silicon":{"id":"si:test", "org_id":"org", "SILICON_ORG":"org"},
            "isi":{}, "access":{}, "flow":[]
        }))?;
        cfg.home = home.path().to_owned();
        Inbox::new(&cfg)?;
        let root = cfg.home.join(".silicon/ting");
        let legacy = root.join("test:org");
        state::write_json(
            &legacy.join("org/inbox.json"),
            &json!({"pending":["untouched"]}),
        )?;
        let bytes = fs::read(legacy.join("org/inbox.json"))?;
        let refused = |cfg: &Config| format!("{:#}", Inbox::new(cfg).err().unwrap());
        let error = refused(&cfg);
        assert!(
            error.contains(&format!(
                "{} belongs to Silicon ID \"test:org\"",
                legacy.display()
            )),
            "{error}"
        );
        assert!(error.contains("PUBLIC-IDENTIFIER-MIGRATION.md"), "{error}");
        let canonical = root.join("si:test");
        fs::rename(&legacy, &canonical)?;
        Inbox::new(&cfg)?;
        assert_eq!(fs::read(canonical.join("org/inbox.json"))?, bytes);

        let retained = cfg.home.join("retained");
        fs::rename(&canonical, &retained)?;
        std::os::unix::fs::symlink(&retained, &canonical)?;
        let error = refused(&cfg);
        assert!(
            error.contains(&format!("{} is a symlink", canonical.display())),
            "{error}"
        );
        fs::remove_file(&canonical)?;
        fs::remove_dir(&root)?;
        let missing = cfg.home.join("missing");
        std::os::unix::fs::symlink(&missing, &root)?;
        let error = refused(&cfg);
        assert!(
            error.contains(&format!(
                "must not be a symlink; it points to {}",
                missing.display()
            )),
            "{error}"
        );
        fs::remove_file(&root)?;
        fs::write(&root, "not a directory")?;
        // The operating system's own reason reaches the reader with the path it concerns.
        let error = refused(&cfg);
        assert!(
            error.contains(&format!(
                "inspect retained Ting namespaces in {}: ",
                root.display()
            )),
            "{error}"
        );
        assert!(error.contains("Not a directory"), "{error}");
        assert_eq!(fs::read(retained.join("org/inbox.json"))?, bytes);
        Ok(())
    }

    #[test]
    fn batches_are_atomic_durable_and_deduplicated() -> Result<()> {
        let home = tempfile::tempdir()?;
        let inbox = Inbox::at(home.path().into());
        inbox.accept(json!({"tings":[ting("a"),ting("b"),ting("a")]}))?;
        let replacement = Inbox::at(home.path().into());
        inbox.process(|request| {
            assert_eq!(request["tings"].as_array().unwrap().len(), 2);
            replacement.accept(json!({"tings":[ting("c")]}))?;
            replacement.process(|_| panic!("a replacement must wait for the active flow"))?;
            Ok(())
        })?;
        // The flow's own error comes back as it was, not dressed as an inbox failure.
        let failed = replacement
            .process(|_| bail!("retry this flow"))
            .unwrap_err();
        assert_eq!(format!("{failed:#}"), "retry this flow");
        replacement.process(|request| {
            assert_eq!(request["tings"][0]["id"], "c");
            Ok(())
        })?;
        let reopened = Inbox::at(home.path().into());
        reopened.accept(json!({"tings":[ting("a"),ting("b")]}))?;
        assert!(queued(home.path()).is_empty());
        Ok(())
    }

    #[test]
    fn generic_json_and_malformed_tings_survive_restart_unchanged() -> Result<()> {
        let home = tempfile::tempdir()?;
        let inbox = Inbox::at(home.path().into());
        let requests = vec![
            json!({"event":"created", "data":{"id":1}}),
            json!([1, {"nested":true}, null]),
            json!("hello"),
            json!(42),
            json!(true),
            Value::Null,
            json!({}),
            json!({"tings":[]}),
            json!({"tings":"arbitrary field"}),
            json!({"tings":[ting("a"), {"id":"bad", "type":"", "data":[]}, 7]}),
            json!({"tings":[ting("a")], "source":"keep this metadata"}),
            json!({"tings":vec![ting("a"); 101]}),
        ];
        for request in &requests {
            inbox.accept(request.clone())?;
        }
        // Generic fields named id/tings do not reserve canonical Ting IDs, even after restart.
        restart(home.path()).accept(json!({"tings":[ting("a")]}))?;
        let expected: Vec<_> = requests
            .iter()
            .cloned()
            .chain([json!({"tings":[ting("a")]})])
            .collect();
        let persisted: Vec<_> = queued(home.path())
            .into_iter()
            .map(|batch| batch["request"].clone())
            .collect();
        assert_eq!(persisted, expected);
        for request in &expected {
            let mut ran = false;
            restart(home.path()).process(|actual| {
                assert_eq!(&actual, request);
                ran = true;
                Ok(())
            })?;
            assert!(ran, "a valid JSON request was quarantined or dropped");
        }
        let inbox = restart(home.path());
        // Generic requests repeat verbatim; canonical Ting deliveries still deduplicate.
        inbox.accept(json!({"tings":[ting("a")]}))?;
        assert!(queued(home.path()).is_empty());
        for request in &requests {
            inbox.accept(request.clone())?;
        }
        assert_eq!(queued(home.path()).len(), requests.len());
        let seen: BTreeMap<String, i64> =
            serde_json::from_slice(&fs::read(home.path().join("seen.json"))?)?;
        assert_eq!(seen.keys().collect::<Vec<_>>(), ["a"]);
        Ok(())
    }

    #[test]
    fn generic_requests_have_durable_distinct_ids_and_respect_backpressure() -> Result<()> {
        let home = tempfile::tempdir()?;
        let mut inbox = Inbox::at(home.path().into());
        inbox.limits.batches = 2;
        inbox.accept(json!([1]))?;
        inbox.accept(json!([1]))?;
        let error = inbox.accept(Value::Null).unwrap_err();
        assert!(error.downcast_ref::<Unavailable>().is_some());
        let mut first_id = String::new();
        let error = inbox
            .process_identified(|id, request| {
                first_id = id.to_owned();
                assert_eq!(request, json!([1]));
                bail!("retry")
            })
            .unwrap_err();
        assert_eq!(error.to_string(), "retry");
        let inbox = restart(home.path());
        inbox.process_identified(|id, request| {
            assert_eq!(id, first_id);
            assert_eq!(request, json!([1]));
            Ok(())
        })?;
        inbox.process_identified(|id, _| {
            assert_ne!(id, first_id);
            Ok(())
        })?;
        assert!(queued(home.path()).is_empty());
        Ok(())
    }

    #[test]
    fn failing_ting_calls_report_status_streams_and_answers() -> Result<()> {
        use std::os::unix::fs::PermissionsExt;

        let home = tempfile::tempdir()?;
        let mut cfg: Config = serde_json::from_value(json!({
            "silicon":{"id":"si:test", "org_id":"org", "SILICON_ORG":"org"},
            "isi":{}, "access":{}, "flow":[]
        }))?;
        cfg.home = home.path().to_owned();
        let executable = home.path().join(".silicon/bin/ting");
        fs::create_dir_all(executable.parent().unwrap())?;
        fs::write(
            &executable,
            r#"#!/bin/sh
if [ "$*" = 'iam --json' ]; then echo '{"app_id":"ting"}'; exit 0; fi
case "$(cat mode)" in
  fail)
    echo "partial answer for $1 $2"
    echo "ting: $1 $2 rejected: token stk-0123456789abcdef expired" >&2
    exit 7 ;;
  garbage) echo 'not json at all'; exit 0 ;;
  loop) echo '{"items":[],"next_cursor":"page-2"}' ;;
  pending) echo '{"id":"stable-hook","state":"pending","reason":"endpoint unreachable"}' ;;
  other) echo '{"id":"other-hook","state":"connected"}' ;;
esac
"#,
        )?;
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700))?;
        let url = "http://test.org.localhost/";
        let inbox = Inbox::new(&cfg)?;
        let failure = |mode: &str, run: &dyn Fn() -> Result<()>| -> Result<String> {
            fs::write(home.path().join("mode"), mode)?;
            let error = format!("{:#}", run().unwrap_err());
            // Everything Ting said arrives, except the credential value itself.
            assert!(!error.contains("stk-0123456789abcdef"), "{error}");
            Ok(error)
        };

        let error = failure("fail", &|| inbox.register(&cfg, url))?;
        for said in [
            "could not inspect Ting registrations for http://test.org.localhost/",
            "`ting webhook list --limit 100 --json` failed: exit status: 7",
            "stderr:\nting: webhook list rejected: token [redacted] expired",
            "stdout:\npartial answer for webhook list",
        ] {
            assert!(error.contains(said), "{error}");
        }

        let error = failure("garbage", &|| inbox.register(&cfg, url))?;
        assert!(
            error
                .contains("`ting webhook list --limit 100 --json` returned invalid JSON: expected"),
            "{error}"
        );
        assert!(error.contains("exit status: 0"), "{error}");
        assert!(error.contains("stdout:\nnot json at all"), "{error}");

        let error = failure("loop", &|| inbox.register(&cfg, url))?;
        assert!(
            error.contains(
                "`ting webhook list --limit 100 --json --cursor page-2` returned `next_cursor` \"page-2\" a second time"
            ),
            "{error}"
        );
        assert!(
            error.contains("stdout:\n{\"items\":[],\"next_cursor\":\"page-2\"}"),
            "{error}"
        );

        inbox.save_hook("stable-hook")?;
        let error = failure("fail", &|| inbox.register(&cfg, url))?;
        for said in [
            "Ting webhook registration for http://test.org.localhost/ failed; the saved hook stable-hook",
            "`ting webhook http://test.org.localhost/ --json --id stable-hook` failed: exit status: 7",
            "stderr:\nting: webhook http://test.org.localhost/ rejected: token [redacted] expired",
            "stdout:\npartial answer for webhook http://test.org.localhost/",
        ] {
            assert!(error.contains(said), "{error}");
        }
        let error = failure("pending", &|| inbox.register(&cfg, url))?;
        assert!(
            error.contains(r#"returned webhook state "pending" instead of "connected""#),
            "{error}"
        );
        assert!(error.contains("endpoint unreachable"), "{error}");
        let error = failure("other", &|| inbox.register(&cfg, url))?;
        assert!(
            error.contains(
                r#"returned webhook ID "other-hook", but this interpreter retains "stable-hook""#
            ),
            "{error}"
        );
        assert_eq!(inbox.hook()?.as_deref(), Some("stable-hook"));

        let error = failure("fail", &|| inbox.unhook(&cfg))?;
        for said in [
            "Ting webhook removal failed; its stable ID stable-hook is retained",
            "`ting unhook stable-hook --json` failed: exit status: 7",
            "stderr:\nting: unhook stable-hook rejected: token [redacted] expired",
            "stdout:\npartial answer for unhook stable-hook",
        ] {
            assert!(error.contains(said), "{error}");
        }
        Ok(())
    }

    fn ting(id: &str) -> Value {
        json!({"id":id,"type":"tos>app.event","data":{},"metadata":{}})
    }

    fn silicon(home: &Path) -> Result<Config> {
        let mut cfg: Config = serde_json::from_value(json!({
            "silicon":{"id":"si:test", "org_id":"org", "SILICON_ORG":"org"},
            "isi":{}, "access":{}, "flow":[]
        }))?;
        cfg.home = home.to_owned();
        Ok(cfg)
    }

    /// The batch files in the queue directory, oldest first, read straight from disk.
    fn queued(directory: &Path) -> Vec<Value> {
        let queue = directory.join("pending");
        let mut names: Vec<String> = match fs::read_dir(&queue) {
            Ok(entries) => entries
                .map(|entry| entry.unwrap().file_name().into_string().unwrap())
                .filter(|name| sequence(name).is_some())
                .collect(),
            Err(_) => Vec::new(),
        };
        names.sort();
        names
            .iter()
            .map(|name| serde_json::from_slice(&fs::read(queue.join(name)).unwrap()).unwrap())
            .collect()
    }

    /// A new interpreter process: nothing about the directory is remembered.
    fn restart(directory: &Path) -> Inbox {
        INBOXES.lock().recover().remove(directory);
        Inbox::at(directory.to_owned())
    }

    fn scans(directory: &Path) -> usize {
        SCANS
            .lock()
            .recover()
            .get(directory)
            .copied()
            .unwrap_or_default()
    }

    /// Files beside `path` that it was moved aside to, with their contents.
    fn moved_aside(path: &Path) -> Vec<Vec<u8>> {
        let prefix = format!("{}.corrupt-", path.file_name().unwrap().to_str().unwrap());
        fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| entry.file_name().to_string_lossy().starts_with(&prefix))
            .map(|entry| fs::read(entry.path()).unwrap())
            .collect()
    }

    #[test]
    fn a_panicking_flow_releases_its_claim_and_keeps_its_batch() -> Result<()> {
        let home = tempfile::tempdir()?;
        let inbox = Inbox::at(home.path().into());
        inbox.accept(json!({"tings":[ting("a")]}))?;
        let error = format!(
            "{:#}",
            inbox
                .process(|_| panic!("flow bug in the send path"))
                .unwrap_err()
        );
        assert!(
            error.contains("panicked: flow bug in the send path"),
            "{error}"
        );
        assert_eq!(queued(home.path()).len(), 1);
        // A panic that escapes while the claim is held still releases it.
        let escaped = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _claim = inbox.claim().unwrap().unwrap();
            panic!("escaped");
        }));
        assert!(escaped.is_err());
        let mut ran = false;
        inbox.process(|request| {
            assert_eq!(request["tings"][0]["id"], "a");
            ran = true;
            Ok(())
        })?;
        assert!(ran, "the claim was never released");
        assert!(queued(home.path()).is_empty());
        Ok(())
    }

    #[test]
    fn a_batch_whose_numbers_do_not_round_trip_runs_once() -> Result<()> {
        // serde_json does not reproduce these floats exactly on a second parse, so a batch
        // matched by its contents instead of its file would never leave the queue.
        for number in ["1e-30", "3.4028234663852886e38", "6.02214076e23"] {
            let home = tempfile::tempdir()?;
            let inbox = Inbox::at(home.path().into());
            let request: Value = serde_json::from_str(&format!(
                r#"{{"tings":[{{"id":"f","type":"t","data":{{"x":{number}}},"metadata":{{}}}}]}}"#
            ))?;
            inbox.accept(request)?;
            let mut runs = 0;
            for _ in 0..3 {
                inbox.process(|_| {
                    runs += 1;
                    Ok(())
                })?;
            }
            assert_eq!(runs, 1, "{number}");
            assert!(queued(home.path()).is_empty(), "{number}");
        }
        Ok(())
    }

    #[test]
    fn finishing_a_batch_leaves_the_others_untouched() -> Result<()> {
        use std::os::unix::fs::MetadataExt;
        let home = tempfile::tempdir()?;
        let inbox = Inbox::at(home.path().into());
        for id in ["a", "b", "c"] {
            inbox.accept(json!({"tings":[ting(id)]}))?;
        }
        let queue = home.path().join("pending");
        let mut files: Vec<_> = fs::read_dir(&queue)?
            .map(|entry| entry.map(|entry| entry.path()))
            .collect::<std::io::Result<_>>()?;
        files.sort();
        assert_eq!(files.len(), 3);
        for file in &files {
            // One compact line per batch.
            assert_eq!(fs::read_to_string(file)?.lines().count(), 1);
        }
        let before: Vec<_> = files[1..]
            .iter()
            .map(|file| fs::metadata(file).map(|metadata| (metadata.ino(), metadata.mtime_nsec())))
            .collect::<std::io::Result<_>>()?;
        inbox.process(|request| {
            assert_eq!(request["tings"][0]["id"], "a");
            Ok(())
        })?;
        assert!(!files[0].exists());
        let after: Vec<_> = files[1..]
            .iter()
            .map(|file| fs::metadata(file).map(|metadata| (metadata.ino(), metadata.mtime_nsec())))
            .collect::<std::io::Result<_>>()?;
        assert_eq!(before, after);
        Ok(())
    }

    #[test]
    fn a_legacy_journal_moves_into_the_queue_and_seen_json() -> Result<()> {
        let home = tempfile::tempdir()?;
        let directory = home.path().to_owned();
        let now = chrono::Utc::now().timestamp();
        let first = uuid::Uuid::new_v4().to_string();
        let legacy = |pending: Value| json!({"seen":{"old":now,"p":now},"pending":pending});
        // The format every earlier interpreter wrote: one pretty file with both parts.
        state::write_json(
            &directory.join("inbox.json"),
            &legacy(json!([
                {"id":first,"request":{"tings":[ting("p")]}},
                {"request":{"tings":[ting("q")]}}
            ])),
        )?;
        let inbox = Inbox::at(directory.clone());
        inbox.accept(json!({"tings":[ting("old"),ting("p"),ting("q"),ting("new")]}))?;
        assert!(!directory.join("inbox.json").exists());
        let seen: BTreeMap<String, i64> =
            serde_json::from_slice(&fs::read(directory.join("seen.json"))?)?;
        assert_eq!(seen.keys().collect::<Vec<_>>(), ["old", "p"]);
        let batches = queued(&directory);
        assert_eq!(batches.len(), 3, "{batches:?}");
        assert_eq!(batches[0]["id"], first.as_str());
        assert_eq!(batches[0]["request"]["tings"], json!([ting("p")]));
        assert!(batches[1]["id"].is_string());
        assert_eq!(batches[1]["request"]["tings"], json!([ting("q")]));
        assert_eq!(batches[2]["request"]["tings"], json!([ting("new")]));

        // A crash before the journal was removed repeats the move without doubling a batch.
        state::write_json(
            &directory.join("inbox.json"),
            &legacy(json!([{"id":first,"request":{"tings":[ting("p")]}}])),
        )?;
        let inbox = restart(&directory);
        inbox.accept(json!({"tings":[ting("r")]}))?;
        assert!(!directory.join("inbox.json").exists());
        let mut order = Vec::new();
        for _ in 0..5 {
            inbox.process(|request| {
                order.push(request["tings"][0]["id"].as_str().unwrap().to_owned());
                Ok(())
            })?;
        }
        assert_eq!(order, ["p", "q", "new", "r"]);
        let seen: BTreeMap<String, i64> =
            serde_json::from_slice(&fs::read(directory.join("seen.json"))?)?;
        assert_eq!(
            seen.keys().collect::<Vec<_>>(),
            ["new", "old", "p", "q", "r"]
        );
        // A pending batch's IDs deduplicate before it runs; processed ones after.
        inbox.accept(json!({"tings":[ting("queued")]}))?;
        inbox.accept(json!({"tings":[ting("queued"),ting("new")]}))?;
        assert_eq!(queued(&directory).len(), 1);
        Ok(())
    }

    #[test]
    fn an_idle_inbox_does_not_list_its_queue_again() -> Result<()> {
        let home = tempfile::tempdir()?;
        let directory = home.path();
        let inbox = Inbox::at(directory.into());
        inbox.accept(json!({"tings":[ting("a")]}))?;
        inbox.process(|_| Ok(()))?;
        let listed = scans(directory);
        assert!(listed > 0);
        for _ in 0..3 {
            inbox.process(|_| panic!("nothing is pending"))?;
            // A redelivery is answered from memory too.
            inbox.accept(json!({"tings":[ting("a")]}))?;
        }
        // Another generation's batch is known at once, without listing the directory.
        Inbox::at(directory.into()).accept(json!({"tings":[ting("b")]}))?;
        let mut ran = Vec::new();
        inbox.process(|request| {
            ran.push(request["tings"][0]["id"].clone());
            Ok(())
        })?;
        assert_eq!(ran, ["b"]);
        assert_eq!(scans(directory), listed);
        // A batch file put back by hand changes the directory, so it is found.
        let id = uuid::Uuid::new_v4();
        let queue = directory.join("pending");
        fs::write(
            queue.join(format!("{:020}-{id}.json", 7)),
            json!({"id":id,"request":{"tings":[ting("c")]}}).to_string(),
        )?;
        fs::File::open(&queue)?
            .set_modified(std::time::SystemTime::now() + std::time::Duration::from_secs(5))?;
        inbox.process(|request| {
            ran.push(request["tings"][0]["id"].clone());
            Ok(())
        })?;
        assert_eq!(ran, ["b", "c"]);
        assert_eq!(scans(directory), listed + 1);
        Ok(())
    }

    #[test]
    fn a_full_inbox_answers_unavailable_with_what_blocks_it() -> Result<()> {
        let home = tempfile::tempdir()?;
        let mut inbox = Inbox::at(home.path().into());
        inbox.limits = Limits {
            batches: 2,
            bytes: 1024 * 1024,
        };
        inbox.accept(json!({"tings":[ting("done")]}))?;
        inbox.process(|_| Ok(()))?;
        inbox.accept(json!({"tings":[ting("b")]}))?;
        inbox.accept(json!({"tings":[ting("c")]}))?;
        inbox
            .process(|_| bail!("flow step 0: unknown flow operation: sned"))
            .unwrap_err();
        let error = inbox.accept(json!({"tings":[ting("d")]})).unwrap_err();
        let reason = error
            .downcast_ref::<Unavailable>()
            .expect("a full inbox is unavailable, not invalid")
            .to_string();
        let head = queued(home.path())[0]["id"].to_string();
        for said in [
            "is full: it holds 2 pending batches in ",
            "takes at most 2 batches or 1048576 bytes, so the sender must deliver this request again later",
            &format!("The oldest pending batch {head} is not finishing"),
            "its last run failed: flow step 0: unknown flow operation: sned",
        ] {
            assert!(reason.contains(said), "{reason}");
        }
        // A redelivery of processed work is still a duplicate, not a refusal.
        inbox.accept(json!({"tings":[ting("done")]}))?;
        inbox.process(|_| Ok(()))?;
        inbox.accept(json!({"tings":[ting("d")]}))?;
        assert_eq!(queued(home.path()).len(), 2);

        let bytes = tempfile::tempdir()?;
        let mut small = Inbox::at(bytes.path().into());
        small.limits = Limits {
            batches: 100,
            bytes: 300,
        };
        small.accept(json!({"tings":[ting("first")]}))?;
        let big = json!({"id":"big","type":"x","data":{"text":"y".repeat(300)},"metadata":{}});
        let error = small.accept(json!({"tings":[big]})).unwrap_err();
        let reason = error.downcast_ref::<Unavailable>().unwrap().to_string();
        assert!(
            reason.contains("takes at most 100 batches or 300 bytes"),
            "{reason}"
        );
        assert!(reason.contains("no run of it has failed"), "{reason}");

        // Storage that cannot be read is unavailable too, with the operating system's reason.
        let blocker = home.path().join("blocker");
        fs::write(&blocker, "a file, not a directory")?;
        let broken = Inbox::at(blocker.join("org"));
        let error = broken.accept(json!({"tings":[ting("e")]})).unwrap_err();
        let reason = error.downcast_ref::<Unavailable>().unwrap().to_string();
        assert!(
            reason.starts_with(
                "the webhook request was not accepted, so the sender must deliver it again: "
            ) && reason.contains(&blocker.join("org/pending").display().to_string())
                && reason.contains("os error"),
            "{reason}"
        );
        // Oversized requests stay a plain rejection.
        let invalid = inbox.accept(json!("x".repeat(1024 * 1024))).unwrap_err();
        assert!(invalid.downcast_ref::<Unavailable>().is_none());
        Ok(())
    }

    #[test]
    fn corrupt_files_are_moved_aside_and_the_inbox_continues() -> Result<()> {
        let home = tempfile::tempdir()?;
        let cfg = silicon(home.path())?;
        let inbox = Inbox::new(&cfg)?;
        let directory = home.path().join(".silicon/ting/si:test/org");
        fs::create_dir_all(directory.join("pending"))?;
        // An earlier interpreter's journal, cut off after its first batch.
        let journal = format!(
            "{{\"pending\":[{{\"id\":\"{}\",\"request\":{{\"tings\":[{}]}}}},{{\"id\":",
            uuid::Uuid::new_v4(),
            ting("p")
        );
        fs::write(directory.join("inbox.json"), &journal)?;
        let damaged = directory.join(format!("pending/{:020}-{}.json", 5, uuid::Uuid::new_v4()));
        fs::write(&damaged, "{\"id\":")?;
        fs::write(directory.join("seen.json"), "[1,2]")?;
        fs::write(directory.join("hook.json"), "stable-hook")?;
        inbox.accept(json!({"tings":[ting("a")]}))?;
        assert_eq!(
            moved_aside(&directory.join("inbox.json")),
            [journal.as_bytes()]
        );
        assert_eq!(moved_aside(&damaged), [b"{\"id\":"]);
        assert_eq!(moved_aside(&directory.join("seen.json")), [b"[1,2]"]);
        assert_eq!(inbox.hook()?, None);
        assert_eq!(moved_aside(&directory.join("hook.json")), [b"stable-hook"]);
        // The batch before the damage runs, then the new one.
        let mut ran = Vec::new();
        for _ in 0..3 {
            inbox.process(|request| {
                ran.push(request["tings"][0]["id"].clone());
                Ok(())
            })?;
        }
        assert_eq!(ran, ["p", "a"]);
        let log = fs::read_to_string(home.path().join(".silicon/silicon.log"))?;
        let errors: Vec<_> = log
            .lines()
            .filter(|line| line.starts_with("[error] [ting/"))
            .collect();
        assert_eq!(errors.len(), 4, "{log}");
        for said in [
            format!(
                "{} is corrupt ({} bytes that do not parse as a Ting inbox: EOF while parsing",
                directory.join("inbox.json").display(),
                journal.len()
            ),
            "queued the 1 pending batches before the damage; any after it do not run and stay in the moved file".to_owned(),
            format!(
                "{} is corrupt (6 bytes that do not parse as a pending Ting batch: EOF while parsing",
                damaged.display()
            ),
            "the batches behind it run as usual".to_owned(),
            "deduplication history was reset".to_owned(),
            "an invalid Ting hook ID: expected value at line 1 column 1; it held: stable-hook"
                .to_owned(),
        ] {
            assert!(log.contains(&said), "{said}\n{log}");
        }
        Ok(())
    }

    #[test]
    fn stray_files_in_the_ting_namespace_are_ignored() -> Result<()> {
        let home = tempfile::tempdir()?;
        let cfg = silicon(home.path())?;
        let root = home.path().join(".silicon/ting");
        fs::create_dir_all(root.join("si:test/org/pending"))?;
        fs::write(root.join(".DS_Store"), "Finder")?;
        fs::write(root.join("._si:test"), "AppleDouble")?;
        fs::write(root.join("si:test/org/pending/.DS_Store"), "Finder")?;
        let inbox = Inbox::new(&cfg)?;
        inbox.accept(json!({"tings":[ting("a")]}))?;
        let mut ran = false;
        inbox.process(|_| {
            ran = true;
            Ok(())
        })?;
        assert!(ran);
        std::os::unix::fs::symlink(root.join(".DS_Store"), root.join("linked"))?;
        let error = format!("{:#}", Inbox::new(&cfg).err().unwrap());
        assert!(
            error.contains(&format!("{} is a symlink", root.join("linked").display())),
            "{error}"
        );
        Ok(())
    }

    #[test]
    fn an_interrupted_flow_keeps_its_batch_for_the_next_connection() -> Result<()> {
        let home = tempfile::tempdir()?;
        let inbox = Inbox::at(home.path().join("inbox"));
        inbox.accept(json!({"tings":[ting("a")]}))?;
        let flow: serde_yaml::Value = serde_yaml::from_str(
            "- send: {isi: first, message: one}\n- send: {isi: second, message: two, catch: [{send: {isi: caught, message: no}}]}\n- send: {isi: third, message: three}\n",
        )?;
        let mut sent = Vec::new();
        let error = inbox
            .process(|request| {
                crate::flow::execute(
                    &flow,
                    json!({"request": request}),
                    home.path(),
                    "interpreter",
                    |target, _, _| {
                        sent.push(target.to_owned());
                        if target == "first" {
                            Ok(())
                        } else {
                            bail!("interpreter is stopping")
                        }
                    },
                )
                .map(|_| ())
            })
            .unwrap_err();
        assert!(crate::flow::interrupted(&error));
        assert_eq!(sent, ["first", "second"]);
        let error = format!("{error:#}");
        assert!(
            error.contains("the batch stays in the Ting inbox and runs again on the next connection: the flow stopped at `send second`: interpreter is stopping"),
            "{error}"
        );
        assert_eq!(queued(&home.path().join("inbox")).len(), 1);
        Ok(())
    }

    #[test]
    fn deduplication_survives_a_clock_that_jumps() {
        let now = 1_800_000_000;
        let mut seen = BTreeMap::from([
            ("future".to_owned(), now + 10 * 365 * 86400),
            ("recent".to_owned(), now - 86400),
            ("expired".to_owned(), now - SEEN_SECONDS),
            ("unset clock".to_owned(), 86400),
        ]);
        settle(&mut seen, now);
        // Stamps from the future or from an unset clock count as now: kept, but not forever.
        assert_eq!(
            seen,
            BTreeMap::from([
                ("future".to_owned(), now),
                ("recent".to_owned(), now - 86400),
                ("unset clock".to_owned(), now),
            ])
        );
        settle(&mut seen, now + SEEN_SECONDS);
        assert!(seen.is_empty(), "{seen:?}");
        // While the clock is still unset nothing ages out early.
        let mut unset = BTreeMap::from([("a".to_owned(), 5000), ("b".to_owned(), 90_000)]);
        settle(&mut unset, 86400);
        assert_eq!(
            unset,
            BTreeMap::from([("a".to_owned(), 5000), ("b".to_owned(), 86400)])
        );
    }
}
