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
