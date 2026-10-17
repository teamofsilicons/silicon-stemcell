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
                        "Ting state belongs to a different Silicon ID or is not an ordinary directory; {} may hold only this Silicon's directory {}, but:\n{}\nStop the interpreter and reconcile the retained state for the configured Silicon before reconnecting",
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
        let directory = root.join(expected.unwrap_or_default());
        crate::migrate_account_state(&directory, &["pending"])?;
        let mut inbox = Self::at(directory);
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
                    "{}\nThe Ting error it was recording:\n{masked}",
                    failure::report(&error)
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
            error.context(Unavailable(
                "the webhook request was not accepted, so the sender must deliver it again".into(),
            ))
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
                shared.blocked = Some(failure::report(&error));
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
                let error = failure::report(&error);
                if shared.unrecorded.as_ref() != Some(&error) {
                    self.report(&format!(
                        "{error}\nProcessed Ting batch {} but could not record its ting IDs, so a Ting redelivery of them runs the flow again",
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
            Err(error) => format!(
                "{}\nThe saved hook ID could not be read back",
                failure::report(&error)
            ),
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
            let error = command_failure(home, &command, &result);
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
                Err(save) => failure::also(
                    error,
                    Err(save.context(format!(
                        "Ting named webhook {returned:?} in its failure, but this interpreter could not retain that ID"
                    ))),
                ),
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
            return Err(save.context(format!(
                "Ting returned webhook ID {returned:?}, but this interpreter could not retain it; response from `{command}`:\n{}",
                failure::mask(home, &failure::describe(&result), &[])
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
                        Err(command_failure(&cfg.home, &command, &result))
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

fn command_failure(home: &Path, command: &str, output: &Output) -> anyhow::Error {
    let failure = failure::command(home, command, output, &[]);
    let retired_field = [&output.stderr, &output.stdout].iter().any(|stream| {
        serde_json::from_slice::<Value>(stream).is_ok_and(|answer| {
            answer["error"]["code"] == "invalid_input"
                && matches!(
                    answer["error"]["message"].as_str(),
                    Some("Missing org_id" | "Missing org_id.")
                )
        })
    });
    if retired_field {
        failure::explain(failure, "Ting requested org_id, a field from its retired contract. Do not add org_id to the Silicon configuration.")
    } else {
        failure
    }
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
            return Err(command_failure(home, &command, &result));
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
mod command_failure_tests {
    use super::*;
    use std::os::unix::process::ExitStatusExt;

    #[test]
    fn unavailable_inbox_preserves_its_storage_error_and_retry_classification() {
        let directory =
            std::env::temp_dir().join(format!("silicon-ting-unavailable-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("pending"), "not a directory").unwrap();
        let error = Inbox::at(directory.clone())
            .accept(json!({"event": "ping"}))
            .unwrap_err();
        assert!(error.is::<Unavailable>());
        assert!(error.is::<std::io::Error>());
        let shown = failure::report(&error);
        assert!(shown.starts_with(&error.root_cause().to_string()));
        assert!(shown.contains("the webhook request was not accepted"));
        assert!(shown.contains("list the Ting queue"));
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn retired_contract_hint_keeps_raw_error_first_without_guessing_the_cause() {
        let home = Path::new("/silicon-ting-error-test");
        for message in ["Missing org_id", "Missing org_id."] {
            for stderr in [true, false] {
                let error = serde_json::to_vec(&json!({
                    "error": {
                        "code": "invalid_input",
                        "message": message,
                        "details": {"token": "private-ting-test-token", "webhook_id": "hook-123"}
                    }
                }))
                .unwrap();
                let diagnostic = b"additional diagnostic\nsecond line\n".to_vec();
                let (stdout, stderr) = if stderr {
                    (diagnostic, error)
                } else {
                    (error, diagnostic)
                };
                let output = Output {
                    status: std::process::ExitStatus::from_raw(1 << 8),
                    stdout,
                    stderr,
                };
                let command = "ting webhook http://localhost/ting --json";
                let failure = command_failure(home, command, &output);
                let shown = failure::report(&failure);
                assert!(shown.contains(
                    "Meaning: Ting requested org_id, a field from its retired contract."
                ));
                assert!(!shown.contains("legacy shared Ting daemon"));
                assert!(!shown.contains("Upgrade and restart"));
                assert!(shown.contains("Do not add org_id"));
                assert!(shown.contains("Source: `ting webhook http://localhost/ting --json`"));
                assert!(shown.contains("Project: https://github.com/teamofsilicons/silicon-ting"));
                assert!(shown.find(message).unwrap() < shown.find("Source:").unwrap());
                assert!(shown.find("Source:").unwrap() < shown.find("Meaning:").unwrap());
                assert!(shown.contains("exit status: 1"));
                assert!(shown.contains("additional diagnostic\nsecond line"));
                assert!(shown.contains("hook-123"));
                assert!(shown.contains("[redacted]"));
                assert!(!shown.contains("private-ting-test-token"));
            }
        }
    }

    #[test]
    fn unrelated_errors_have_no_speculative_meaning() {
        let home = Path::new("/silicon-ting-error-test");
        for error in [
            r#"{"error":{"code":"invalid_input","message":"Missing account_id."}}"#,
            r#"{"error":{"code":"unauthorized","message":"Missing org_id."}}"#,
            r#"{"error":{"code":"invalid_input","message":"Missing org_id in another request."}}"#,
            r#"{"code":"invalid_input","message":"Missing org_id."}"#,
            "invalid_input: Missing org_id.",
            "not JSON",
        ] {
            let output = Output {
                status: std::process::ExitStatus::from_raw(1 << 8),
                stdout: Vec::new(),
                stderr: error.as_bytes().to_vec(),
            };
            let command = "ting unhook hook-123 --json";
            let shown = failure::report(&command_failure(home, command, &output));
            assert!(shown.contains(error));
            assert!(shown.contains("Source: `ting unhook hook-123 --json`"));
            assert!(!shown.contains("Meaning:"));
        }
    }
}
