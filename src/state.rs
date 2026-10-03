use crate::Recover;
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::os::unix::fs::{FileExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime};
use uuid::Uuid;

pub fn private_dir(path: &Path) -> Result<()> {
    #[cfg(target_os = "linux")]
    if std::env::var("SILICON_WSL").as_deref() == Ok("1") {
        validate_wsl_filesystem(path)?;
    }
    fs::create_dir_all(path)
        .with_context(|| format!("cannot create directory {}", path.display()))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .with_context(|| format!("cannot make {} private (mode 700)", path.display()))?;
    Ok(())
}

/// Check the actual filesystem before creating state, following existing symlinks.
#[cfg(target_os = "linux")]
pub(crate) fn validate_wsl_filesystem(path: &Path) -> Result<()> {
    use std::os::unix::ffi::OsStrExt;
    let absolute = std::path::absolute(path)
        .with_context(|| format!("cannot make {} absolute", path.display()))?;
    let mut probe = absolute.as_path();
    loop {
        let name = std::ffi::CString::new(probe.as_os_str().as_bytes())
            .with_context(|| format!("path {} contains a NUL byte", probe.display()))?;
        let mut filesystem = std::mem::MaybeUninit::<libc::statfs>::uninit();
        if unsafe { libc::statfs(name.as_ptr(), filesystem.as_mut_ptr()) } == 0 {
            let found = unsafe { filesystem.assume_init() }.f_type;
            if found != libc::EXT4_SUPER_MAGIC {
                bail!("Windows WSL installation requires configuration and private state inside the Silicon distribution's Linux filesystem; copy your project to /home/silicon first. Windows data files remain accessible through /mnt/c and other mounted drives. {} (for {}) is on filesystem type {found:#x}, not ext4", probe.display(), path.display());
            }
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error).with_context(|| {
                format!(
                    "inspect WSL configuration filesystem at {}",
                    probe.display()
                )
            });
        }
        probe = probe.parent().ok_or(error).with_context(|| {
            format!(
                "find WSL configuration filesystem for {}",
                absolute.display()
            )
        })?;
    }
}

pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    write(path, value, true)
}

/// [`write_json`] without indentation, for journals the interpreter rewrites often and
/// alone reads.
pub fn write_json_compact(path: &Path, value: &impl Serialize) -> Result<()> {
    write(path, value, false)
}

fn write(path: &Path, value: &impl Serialize, pretty: bool) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("state path {} has no parent", path.display()))?;
    let saving = || format!("saving {} failed", path.display());
    private_dir(parent).with_context(saving)?;
    sweep_staged(parent);
    let staged = parent.join(format!(".{}.tmp", Uuid::new_v4()));
    let at = |step: &str, file: &Path| format!("{step} {}", file.display());
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staged)
        .with_context(|| at("cannot create", &staged))
        .with_context(saving)?;
    let result = (|| -> Result<()> {
        // serde_json writes token by token; unbuffered, that is one syscall per token.
        let mut writer = BufWriter::with_capacity(64 * 1024, &mut file);
        if pretty {
            serde_json::to_writer_pretty(&mut writer, value)
        } else {
            serde_json::to_writer(&mut writer, value)
        }
        .with_context(|| at("cannot serialize state into", &staged))?;
        writer
            .write_all(b"\n")
            .with_context(|| at("cannot write", &staged))?;
        writer
            .into_inner()
            .map_err(std::io::IntoInnerError::into_error)
            .with_context(|| at("cannot write", &staged))?;
        file.sync_all()
            .with_context(|| at("cannot sync", &staged))?;
        fs::rename(&staged, path)
            .with_context(|| format!("cannot rename {} to {}", staged.display(), path.display()))?;
        File::open(parent)
            .and_then(|directory| directory.sync_all())
            .with_context(|| at("cannot sync directory", parent))?;
        Ok(())
    })();
    // A leftover staged copy is reported with the failure that left it behind.
    result.map_err(|error| match fs::remove_file(&staged) {
        Err(cleanup) if cleanup.kind() != std::io::ErrorKind::NotFound => error.context(format!(
            "saving {} failed and its staged copy {} remains ({cleanup})",
            path.display(),
            staged.display()
        )),
        _ => error.context(saving()),
    })
}

/// A staged copy this old was left by a crash or kill mid-write; a live write takes far less.
const STALE_STAGED: Duration = Duration::from_secs(3600);

/// Remove staged copies a crash left in `directory`, sized like the state they were
/// replacing, so years of unclean stops cannot pile them up. Best effort, and at most
/// hourly per directory so frequent writes do not rescan it.
fn sweep_staged(directory: &Path) {
    static SWEPT: Mutex<BTreeMap<PathBuf, SystemTime>> = Mutex::new(BTreeMap::new());
    let now = SystemTime::now();
    {
        let mut swept = SWEPT.lock().recover();
        if swept
            .get(directory)
            .is_some_and(|last| matches!(now.duration_since(*last), Ok(age) if age < STALE_STAGED))
        {
            return;
        }
        swept.insert(directory.to_path_buf(), now);
    }
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let staged = name
            .to_str()
            .and_then(|name| name.strip_prefix('.')?.strip_suffix(".tmp"))
            .is_some_and(|id| id.len() == 36 && Uuid::parse_str(id).is_ok());
        // DirEntry::metadata does not follow a symlink, so only real staged files go.
        let stale = || {
            entry.metadata().is_ok_and(|metadata| {
                metadata.is_file()
                    && metadata.modified().is_ok_and(|modified| {
                        now.duration_since(modified)
                            .is_ok_and(|age| age >= STALE_STAGED)
                    })
            })
        };
        if staged && stale() {
            let _ = fs::remove_file(entry.path());
        }
    }
}

/// Append one JSON record as a line. The file rotates like the interpreter's logs, keeping
/// two older copies, so a session that runs for months cannot fill the disk.
pub fn append_json(path: &Path, value: &impl Serialize) -> Result<()> {
    private_dir(
        path.parent()
            .ok_or_else(|| anyhow!("log {} has no parent", path.display()))?,
    )?;
    let mut data = serde_json::to_vec(value)
        .with_context(|| format!("cannot serialize a record for {}", path.display()))?;
    data.push(b'\n');
    let appending = || format!("cannot append to {}", path.display());
    let mut file = OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .mode(0o600)
        .open(path)
        .with_context(appending)?;
    let length = file.metadata().with_context(appending)?.len();
    if length > 0 {
        let mut last = [0u8];
        file.read_exact_at(&mut last, length - 1)
            .with_context(appending)?;
        if last[0] != b'\n' {
            // A write torn by a crash keeps its own line instead of spoiling this record.
            data.insert(0, b'\n');
        }
    }
    file.write_all(&data).with_context(appending)?;
    drop(file);
    // The record is written; a failed rotation is reported, not raised over it.
    if let Err(error) = crate::rotate(path, crate::LOG_CAP, 2) {
        crate::stderr_line(&format!("{error:#}"));
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Session {
    /// Logical address; the Omni session UUID never changes when an archive is named.
    pub id: String,
    pub session_id: Uuid,
    pub isi: String,
    pub title: String,
    pub description: String,
    pub first: DateTime<Utc>,
    pub last: DateTime<Utc>,
    pub archived_at: Option<DateTime<Utc>>,
    pub status: String,
    pub new_messages: u64,
    pub last_suggestion: Option<DateTime<Utc>>,
    pub messages_at_suggestion: u64,
    /// Ephemeral work returns its final output to the calling ISI and retires when
    /// idle, whether or not the session is kept.
    pub ephemeral: bool,
    /// Use-and-throw: global ephemeral work only. Session-addressed ephemeral work
    /// is created and reached by an id its caller holds, so it is retained and
    /// archived like a persistent session. Records written before this field
    /// existed are all persistent, so defaulting to kept is correct.
    #[serde(default)]
    pub disposable: bool,
}

impl Session {
    pub fn new(
        isi: &str,
        id: Option<&str>,
        title: &str,
        ephemeral: bool,
        disposable: bool,
    ) -> Self {
        let session_id = Uuid::new_v4();
        Self {
            id: id
                .map(str::to_owned)
                .unwrap_or_else(|| session_id.to_string()),
            session_id,
            isi: isi.to_owned(),
            title: title.to_owned(),
            description: String::new(),
            first: Utc::now(),
            last: Utc::now(),
            archived_at: None,
            status: "idle".into(),
            new_messages: 0,
            last_suggestion: None,
            messages_at_suggestion: 0,
            ephemeral,
            disposable,
        }
    }
    pub fn path(&self, home: &Path) -> PathBuf {
        home.join(".silicon/sessions")
            .join(if self.archived_at.is_some() {
                "archived"
            } else {
                "active"
            })
            .join(&self.isi)
            .join(format!("{}.json", self.session_id))
    }
    pub fn save(&self, home: &Path) -> Result<()> {
        if !self.disposable || self.archived_at.is_some() {
            write_json(&self.path(home), self)?;
        }
        Ok(())
    }
    pub fn archive(
        &mut self,
        home: &Path,
        id: Option<&str>,
        title: Option<&str>,
        description: Option<&str>,
    ) -> Result<()> {
        let active = self.path(home);
        if let Some(id) = id {
            if id.is_empty() {
                bail!("archive id cannot be empty");
            }
            self.id = id.to_owned();
        }
        if let Some(title) = title {
            self.title = title.to_owned();
        }
        if let Some(description) = description {
            self.description = description.to_owned();
        }
        self.archived_at = Some(Utc::now());
        self.last = Utc::now();
        self.status = "archived".into();
        self.save(home)?;
        if active.exists() {
            fs::remove_file(&active).with_context(|| {
                format!(
                    "archived, but cannot remove active copy {}",
                    active.display()
                )
            })?;
        }
        Ok(())
    }
}

/// The session records of one ISI, and every record file that could not be loaded.
pub struct Scan {
    pub sessions: Vec<Session>,
    /// Each unreadable or unparsable record file with why, e.g. a disk error or a hand edit.
    pub skipped: Vec<(PathBuf, String)>,
}

impl Scan {
    /// Fails, naming every skipped file, when `purpose` needs all the records: one that
    /// could not be loaded may be the very session or archive ID about to be created, and
    /// creating it again would leave two records with one ID once the file loads again.
    pub fn complete(&self, purpose: &str) -> Result<()> {
        if self.skipped.is_empty() {
            return Ok(());
        }
        bail!(
            "cannot {purpose}: {} session record file(s) could not be loaded, and one of them may already hold that ID. Repair or remove them first:\n{}",
            self.skipped.len(),
            self.skipped
                .iter()
                .map(|(_, reason)| reason.as_str())
                .collect::<Vec<_>>()
                .join("\n")
        );
    }
}

/// Every record in the directory that loads, newest first. One bad file never hides the
/// others: it is skipped and reported once through `silicon.log`.
pub fn sessions(home: &Path, isi: &str, archived: bool) -> Result<Vec<Session>> {
    Ok(scan_sessions(home, isi, archived)?.sessions)
}

/// [`sessions`], also returning the files it skipped (each reported once, as there), for a
/// caller that must know whether the listing is complete; see [`Scan::complete`].
pub fn scan_sessions(home: &Path, isi: &str, archived: bool) -> Result<Scan> {
    let dir = home
        .join(".silicon/sessions")
        .join(if archived { "archived" } else { "active" })
        .join(isi);
    let mut scan = Scan {
        sessions: Vec::new(),
        skipped: Vec::new(),
    };
    if !dir.exists() {
        return Ok(scan);
    }
    let listing = || format!("cannot list sessions in {}", dir.display());
    for entry in fs::read_dir(&dir).with_context(listing)? {
        let path = entry.with_context(listing)?.path();
        // AppleDouble `._*` files from exFAT or network volumes are not records.
        if path.extension().is_none_or(|s| s != "json")
            || path
                .file_name()
                .is_some_and(|name| name.as_encoded_bytes().starts_with(b"._"))
        {
            continue;
        }
        let bytes = match fs::read(&path) {
            Ok(bytes) => bytes,
            // Archived or ended between the listing and the read.
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => {
                scan.skipped.push((
                    path.clone(),
                    format!("cannot read {}: {error}", path.display()),
                ));
                continue;
            }
        };
        match serde_json::from_slice::<Session>(&bytes) {
            Ok(session) => scan.sessions.push(session),
            Err(error) => scan.skipped.push((
                path.clone(),
                format!("invalid session state {}: {error}", path.display()),
            )),
        }
    }
    scan.sessions.sort_by_key(|s| std::cmp::Reverse(s.last));
    for (path, reason) in &scan.skipped {
        skipped(home, path, reason);
    }
    Ok(scan)
}

/// The last `count` records of a per-session event log, oldest first. [`append_json`]
/// rotates the log, so right after a rotation the current file holds only a line or two
/// and the older copies fill in the rest. A line that is not JSON, such as a write torn by
/// a crash, is shown as it is with the reason instead of failing the whole history.
pub fn events(path: &Path, count: usize) -> Result<Vec<serde_json::Value>> {
    let mut events = Vec::new();
    for copy in [
        path.to_owned(),
        crate::numbered(path, "1"),
        crate::numbered(path, "2"),
    ] {
        if events.len() >= count {
            break;
        }
        let older: Vec<_> = crate::server::tail(&copy, count - events.len())?
            .into_iter()
            .map(|line| {
                serde_json::from_str(&line).unwrap_or_else(|error| {
                    serde_json::json!({
                        "unparsable": line,
                        "error": format!("{} holds an event line that is not JSON: {error}", copy.display()),
                    })
                })
            })
            .collect();
        events.splice(0..0, older);
    }
    Ok(events)
}

/// One `[error]` line per file and reason: the scheduler lists sessions several times a
/// second, and the same broken file must not flood the log.
fn skipped(home: &Path, path: &Path, reason: &str) {
    static SAID: Mutex<BTreeSet<(PathBuf, String)>> = Mutex::new(BTreeSet::new());
    if !SAID
        .lock()
        .recover()
        .insert((path.to_path_buf(), reason.to_owned()))
    {
        return;
    }
    let message = format!(
        "skipped a session record that could not be loaded; the other records still load. Repair or remove the file to bring that session back: {reason}"
    );
    if let Err(error) = crate::log_line(home, "error", "state", &message) {
        crate::stderr_line(&format!(
            "{error:#}; the state error it was recording: {}",
            crate::failure::mask(home, &message, &[])
        ));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(target_os = "linux")]
    #[test]
    fn wsl_state_rejects_a_redirected_parent_before_writing() {
        // Isolate SILICON_WSL from other tests in this process.
        if std::env::var_os("SILICON_WSL_STATE_TEST").is_none() {
            let status = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "state::tests::wsl_state_rejects_a_redirected_parent_before_writing",
                ])
                .env("SILICON_WSL", "1")
                .env("SILICON_WSL_STATE_TEST", "1")
                .status()
                .unwrap();
            assert!(status.success());
            return;
        }
        let directory = tempfile::tempdir().unwrap();
        let alias = directory.path().join(".silicon");
        std::os::unix::fs::symlink("/proc", &alias).unwrap();
        let path = alias.join(format!("silicon-state-{}/credentials.json", Uuid::new_v4()));
        let error = format!(
            "{:#}",
            write_json(&path, &serde_json::json!({"secret": "private"})).unwrap_err()
        );
        assert!(
            error.contains("copy your project to /home/silicon"),
            "{error}"
        );
        assert!(!path.exists());
    }

    #[test]
    fn state_failures_name_the_path_and_the_operating_system_reason() {
        let dir = tempfile::tempdir().unwrap();
        let blocker = dir.path().join("blocker");
        fs::write(&blocker, "a file, not a directory").unwrap();
        let path = blocker.join("state.json");
        let error = format!(
            "{:#}",
            write_json(&path, &serde_json::json!({"ok": true})).unwrap_err()
        );
        assert!(
            error.contains(&format!("saving {} failed", path.display()))
                && error.contains(&format!("cannot create directory {}", blocker.display()))
                && error.contains("(os error"),
            "{error}"
        );
        let error = format!(
            "{:#}",
            append_json(&path, &serde_json::json!({"ok": true})).unwrap_err()
        );
        assert!(error.contains(&blocker.display().to_string()), "{error}");
        // A directory in the way of the rename names both paths and the OS reason.
        let occupied = dir.path().join("occupied");
        fs::create_dir_all(occupied.join("child")).unwrap();
        let error = format!(
            "{:#}",
            write_json(&occupied, &serde_json::json!({})).unwrap_err()
        );
        assert!(
            error.contains(&format!("saving {} failed", occupied.display()))
                && error.contains("cannot rename")
                && error.contains("(os error"),
            "{error}"
        );
        // The failed save leaves no staged copy behind.
        let leftovers: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn one_bad_session_record_is_skipped_and_reported_once() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let sessions_dir = dir.path().join(".silicon/sessions/active/worker");
        let good = Session::new("worker", Some("good"), "Good", false, false);
        good.save(dir.path()).unwrap();
        fs::write(sessions_dir.join("broken.json"), "{\"id\":").unwrap();
        fs::write(sessions_dir.join("._broken.json"), "AppleDouble").unwrap();
        let locked = sessions_dir.join("locked.json");
        fs::write(&locked, serde_json::to_vec(&good).unwrap()).unwrap();
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
        let unreadable = fs::read(&locked).is_err();
        let scan = scan_sessions(dir.path(), "worker", false).unwrap();
        assert_eq!(scan.sessions.len(), 1 + usize::from(!unreadable));
        assert_eq!(scan.sessions[0].id, "good");
        let broken = sessions_dir.join("broken.json");
        let reasons: Vec<_> = scan.skipped.iter().map(|(_, reason)| reason).collect();
        assert!(
            scan.skipped.iter().any(|(path, reason)| path == &broken
                && reason.contains(&format!("invalid session state {}", broken.display()))
                && reason.contains("EOF while parsing")),
            "{reasons:?}"
        );
        if unreadable {
            assert!(
                reasons.iter().any(|reason| reason
                    .starts_with(&format!("cannot read {}: ", locked.display()))
                    && reason.contains("os error")),
                "{reasons:?}"
            );
        }
        assert_eq!(scan.skipped.len(), 1 + usize::from(unreadable));
        for _ in 0..3 {
            assert_eq!(sessions(dir.path(), "worker", false).unwrap()[0].id, "good");
        }
        let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        let lines: Vec<_> = log
            .lines()
            .filter(|line| line.starts_with("[error] [state/"))
            .collect();
        assert_eq!(lines.len(), scan.skipped.len(), "{log}");
        assert!(
            lines.iter().any(|line| line.contains("broken.json")
                && line.contains("the other records still load")),
            "{log}"
        );
        // Creating a record needs every one: the broken file may already hold that ID.
        let error = format!(
            "{:#}",
            scan.complete("create session worker:ticket-42")
                .unwrap_err()
        );
        assert!(
            error.starts_with("cannot create session worker:ticket-42: ")
                && error.contains(&format!("invalid session state {}", broken.display())),
            "{error}"
        );
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o600)).unwrap();
        fs::remove_file(&broken).unwrap();
        let scan = scan_sessions(dir.path(), "worker", false).unwrap();
        scan.complete("create session worker:ticket-42").unwrap();
        assert_eq!(scan.sessions.len(), 2);
    }

    #[test]
    fn event_history_reads_across_a_rotation_and_shows_torn_lines() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("events/session.jsonl");
        assert!(events(&path, 100).unwrap().is_empty());
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(crate::numbered(&path, "2"), "{\"a\":1}\n").unwrap();
        fs::write(crate::numbered(&path, "1"), "{\"b\":2}\n{\"c\":3}\n").unwrap();
        // Just rotated: the current file holds one record after a torn one.
        fs::write(&path, "{\"d\":\n{\"e\":5}\n").unwrap();
        let shown = events(&path, 3).unwrap();
        assert_eq!(shown[0], serde_json::json!({"c": 3}));
        assert_eq!(shown[1]["unparsable"], "{\"d\":");
        assert!(
            shown[1]["error"].as_str().unwrap().starts_with(&format!(
                "{} holds an event line that is not JSON: ",
                path.display()
            )),
            "{shown:?}"
        );
        assert_eq!(shown[2], serde_json::json!({"e": 5}));
        let all = events(&path, 100).unwrap();
        assert_eq!(all.len(), 5);
        assert_eq!(all[0], serde_json::json!({"a": 1}));
        assert_eq!(all[1], serde_json::json!({"b": 2}));
    }

    #[test]
    fn appending_after_a_torn_tail_starts_a_fresh_line_and_rotates() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("events/session.jsonl");
        append_json(&path, &serde_json::json!({"a": 1})).unwrap();
        // A crash left half a record.
        OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"{\"b\":")
            .unwrap();
        append_json(&path, &serde_json::json!({"c": 3})).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            "{\"a\":1}\n{\"b\":\n{\"c\":3}\n"
        );
        // Past the log cap the file moves to .1 and a new one starts; sparse, so cheap.
        OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(crate::LOG_CAP)
            .unwrap();
        append_json(&path, &serde_json::json!({"d": 4})).unwrap();
        assert!(!path.exists());
        let rotated = crate::numbered(&path, "1");
        assert!(fs::metadata(&rotated).unwrap().len() > crate::LOG_CAP);
        append_json(&path, &serde_json::json!({"e": 5})).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "{\"e\":5}\n");
    }

    #[test]
    fn writes_are_compact_on_request_and_sweep_stale_staged_copies() {
        let dir = tempfile::tempdir().unwrap();
        let hours_ago = SystemTime::now() - Duration::from_secs(2 * 3600);
        let staged = |name: &str, modified: SystemTime| {
            let path = dir.path().join(name);
            let file = File::create(&path).unwrap();
            file.set_modified(modified).unwrap();
            path
        };
        let stale = staged(&format!(".{}.tmp", Uuid::new_v4()), hours_ago);
        let live = staged(&format!(".{}.tmp", Uuid::new_v4()), SystemTime::now());
        let other = staged(".not-a-staged-copy.tmp", hours_ago);
        let path = dir.path().join("journal.json");
        write_json_compact(&path, &serde_json::json!({"a": [1, 2], "b": {"c": true}})).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            "{\"a\":[1,2],\"b\":{\"c\":true}}\n"
        );
        assert!(!stale.exists());
        assert!(live.exists() && other.exists());
        write_json(&path, &serde_json::json!({"a": 1})).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "{\n  \"a\": 1\n}\n");
    }

    #[test]
    fn archive_keeps_original_time_and_safe_disk_identity() {
        let dir = tempfile::tempdir().unwrap();
        let mut session = Session::new(
            "worker",
            Some("job/with arbitrary text"),
            "Build",
            false,
            false,
        );
        let first = session.first;
        session.save(dir.path()).unwrap();
        session
            .archive(dir.path(), Some("release"), None, Some("Finished"))
            .unwrap();
        assert!(sessions(dir.path(), "worker", false).unwrap().is_empty());
        let archive = sessions(dir.path(), "worker", true).unwrap();
        assert_eq!(archive[0].first, first);
        assert_eq!(archive[0].id, "release");
    }
}
