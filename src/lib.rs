pub mod apps;
pub mod auth;
pub mod cli;
pub mod config;
pub mod eval;
pub(crate) mod failure;
pub mod flow;
mod outbox;
pub(crate) mod process;
mod progress;
pub mod proxy;
pub mod redaction;
pub mod runtime;
pub mod server;
pub mod service;
pub(crate) mod starters;
pub mod state;
pub mod ting;
pub mod update;

use anyhow::{Context, Result};
use chrono::Utc;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

static LOG_LOCK: Mutex<()> = Mutex::new(());

/// A panic on one thread must not wedge a daemon that runs for months. Every lock in the
/// interpreter guards plain bookkeeping, so the next holder takes the data as the
/// panicking thread left it instead of panicking in turn.
pub(crate) trait Recover<T> {
    fn recover(self) -> T;
}

impl<T> Recover<T> for std::sync::LockResult<T> {
    fn recover(self) -> T {
        self.unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

/// The interpreter's own stderr is daemon.log. `eprintln!` panics when that write fails
/// (a full disk, a closed stream), which would kill the thread that was only reporting.
pub(crate) fn stderr_line(text: &str) {
    let _ = writeln!(std::io::stderr().lock(), "{text}");
}

/// [`stderr_line`] for the interpreter's stdout, which is also daemon.log.
pub(crate) fn stdout_line(text: &str) {
    let mut stdout = std::io::stdout().lock();
    let _ = writeln!(stdout, "{text}").and_then(|()| stdout.flush());
}

/// silicon.log and the interpreter's other logs are capped, so years of work cannot fill
/// the disk: past this size a log moves to `NAME.1` (older copies shift up to
/// `NAME.{LOG_KEEP}`, the oldest is dropped) and a new file starts.
pub const LOG_CAP: u64 = 64 * 1024 * 1024;
pub const LOG_KEEP: usize = 5;

/// Rotate `path` when it has reached `cap` bytes; true when it moved. Several processes
/// append to the same logs, so the move happens under an exclusive lock on `PATH.lock`
/// and is skipped when another process already rotated. A writer that opened the file
/// just before the move finishes its line in `PATH.1`; nothing is lost.
pub(crate) fn rotate(path: &Path, cap: u64, keep: usize) -> Result<bool> {
    let full = |path: &Path| match std::fs::metadata(path) {
        Ok(metadata) => Ok(metadata.len() >= cap),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error).with_context(|| format!("read the size of {}", path.display())),
    };
    if !full(path)? {
        return Ok(false);
    }
    let lock_path = numbered(path, "lock");
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .open(&lock_path)
        .with_context(|| format!("open {}", lock_path.display()))?;
    use std::os::fd::AsRawFd;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(std::io::Error::last_os_error())
            .with_context(|| format!("lock {}", lock_path.display()));
    }
    if !full(path)? {
        return Ok(false);
    }
    for index in (1..keep.max(1)).rev() {
        let from = numbered(path, &index.to_string());
        let to = numbered(path, &(index + 1).to_string());
        match std::fs::rename(&from, &to) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("rotate {} to {}", from.display(), to.display()))
            }
        }
    }
    let first = numbered(path, "1");
    std::fs::rename(path, &first)
        .with_context(|| format!("rotate {} to {}", path.display(), first.display()))?;
    Ok(true)
}

/// `silicon.log` -> `silicon.log.1`.
pub(crate) fn numbered(path: &Path, suffix: &str) -> std::path::PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".");
    name.push(suffix);
    name.into()
}

/// Everything a log gained after byte `start`. When the log rotated in between, the rest
/// of the rotated copy comes first, then the whole new file.
pub(crate) fn appended_since(path: &Path, start: u64) -> std::io::Result<Vec<u8>> {
    use std::io::{Read, Seek, SeekFrom};
    fn from(path: &Path, start: u64) -> std::io::Result<Vec<u8>> {
        let mut file = match std::fs::File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error),
        };
        if file.metadata()?.len() < start {
            return Ok(Vec::new());
        }
        file.seek(SeekFrom::Start(start))?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        Ok(bytes)
    }
    let length = match std::fs::metadata(path) {
        Ok(metadata) => metadata.len(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => return Err(error),
    };
    if length >= start {
        return from(path, start);
    }
    let mut bytes = from(&numbered(path, "1"), start)?;
    bytes.extend(from(path, 0)?);
    Ok(bytes)
}

/// Complete lines a log gained since `*offset`, following a rotation in between; `*offset`
/// becomes the position after the last complete line, in the current file. A line still
/// being written stays for the next call.
pub(crate) fn complete_lines_since(path: &Path, offset: &mut u64) -> std::io::Result<Vec<u8>> {
    let length = match std::fs::metadata(path) {
        Ok(metadata) => metadata.len(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => return Err(error),
    };
    let mut bytes = Vec::new();
    if length < *offset {
        bytes = appended_since(path, *offset)?;
        // What follows the rotated copy is the whole new file, read from its start.
        let fresh = appended_since(path, 0)?.len();
        bytes.truncate(bytes.len() - fresh);
        *offset = 0;
    }
    let fresh = appended_since(path, *offset)?;
    if let Some(end) = fresh.iter().rposition(|byte| *byte == b'\n') {
        bytes.extend_from_slice(&fresh[..=end]);
        *offset += end as u64 + 1;
    }
    Ok(bytes)
}
static IS_DAEMON: AtomicBool = AtomicBool::new(false);

/// Call once at interpreter startup, before any logging, so every line this
/// process writes is attributed to the daemon rather than a CLI invocation.
pub fn mark_daemon() {
    IS_DAEMON.store(true, Ordering::Relaxed);
}

/// Which process wrote a log line. `silicon serve` is the daemon; every other
/// `silicon`/`si` invocation is a CLI process. Both append to the same file, so
/// without this a compile logged by `silicon connect` is indistinguishable from
/// the daemon's own compile of the same YAML.
pub fn process_role() -> &'static str {
    if IS_DAEMON.load(Ordering::Relaxed) {
        "daemon"
    } else {
        "cli"
    }
}

/// Call at process startup, before starting threads, so private bundle tools are discoverable.
pub fn init_bundle_path() -> Result<()> {
    let executable = std::env::current_exe().context("locate the running executable")?;
    let executable = executable
        .canonicalize()
        .with_context(|| format!("resolve the running executable {}", executable.display()))?;
    if let Some(bin) = executable.parent() {
        // Probes: no VERSION match means a source build, no prefix means no releases to skip.
        if bin.parent().is_some_and(|root| {
            std::fs::read_to_string(root.join("VERSION")).is_ok_and(|version| {
                version.trim().trim_start_matches('v') == env!("CARGO_PKG_VERSION")
            })
        }) {
            // A restarted updater can inherit a previous release's app binaries.
            let releases = crate::update::managed_prefix()
                .ok()
                .map(|prefix| prefix.join("lib/silicon/releases"));
            let path = std::env::join_paths(std::iter::once(bin.to_path_buf()).chain(
                std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).filter(
                    |path| {
                        path != bin && !releases.as_ref().is_some_and(|root| path.starts_with(root))
                    },
                ),
            ))
            .with_context(|| {
                format!(
                    "cannot put the bundle directory {} first on PATH",
                    bin.display()
                )
            })?;
            std::env::set_var("PATH", path);
        }
    }
    Ok(())
}

/// Every tool process uses the Silicon's app state and preserves the app's update policy.
pub(crate) fn command(program: impl AsRef<std::ffi::OsStr>, home: &Path) -> std::process::Command {
    let mut command = std::process::Command::new(program);
    command
        .current_dir(home)
        // The supervisor marker is the interpreter's; a tool that runs `silicon serve` must
        // not think a supervisor owns it.
        .env_remove("SILICON_SERVICE")
        .env_remove("SILICON_DETACHED")
        .env("SILICON_HOME", home)
        .env("ACCOUNTS_HOME", home)
        .env_remove("ACCOUNTS_STK")
        .env_remove("ACCOUNTS_SILICON")
        .env_remove("ACCOUNTS_SILICON_KEY")
        .env_remove("SILICON_IAM_HOME")
        .env_remove("SILICON_ORG")
        .env_remove("SPACE_STATION_ORG");
    let bin = home.join(".silicon/bin");
    match std::env::join_paths(
        std::iter::once(bin.clone())
            .chain(std::iter::once(home.join(".apps/bin")))
            .chain(std::env::split_paths(
                &std::env::var_os("PATH").unwrap_or_default(),
            )),
    ) {
        Ok(path) => {
            command.env("PATH", path);
        }
        Err(error) => tool_setup_failed(
            home,
            &format!(
                "tools run without {} on PATH, so this Silicon's apps are not found first: {error}",
                bin.display()
            ),
        ),
    }
    command
        .env("DO_NOT_TRACK", "1")
        .env("DISABLE_TELEMETRY", "1")
        .env("DISABLE_ERROR_REPORTING", "1")
        .env("SILICON_TELEMETRY", "0")
        .env("ACCOUNTS_TELEMETRY", "off")
        .env("APPS_TELEMETRY", "0")
        .env("DM_TELEMETRY_ENABLED", "false")
        .env("BRIEFCASE_TELEMETRY", "0")
        .env("SPACE_STATION_TELEMETRY", "0");
    command
}

/// A tool will run, but not as configured. silicon.log says why, once per home and reason,
/// because every tool start would otherwise repeat it.
fn tool_setup_failed(home: &Path, message: &str) {
    static SAID: Mutex<Vec<(std::path::PathBuf, String)>> = Mutex::new(Vec::new());
    let key = (home.to_path_buf(), message.to_owned());
    {
        let mut said = SAID.lock().recover();
        if said.contains(&key) {
            return;
        }
        said.push(key);
    }
    if let Err(error) = log_line(home, "error", "interpreter", message) {
        stderr_line(&format!(
            "{error:#}; the error it was recording: {}",
            redaction::redact(home, message)
        ));
    }
}

pub fn log_line(home: &Path, kind: &str, origin: &str, message: &str) -> Result<()> {
    log_line_scoped(home, None, kind, origin, message)
}

pub fn log_line_scoped(
    home: &Path,
    _generation: Option<uuid::Uuid>,
    kind: &str,
    origin: &str,
    message: &str,
) -> Result<()> {
    let message = redaction::redact(home, message);
    let origin = redaction::redact(home, origin);
    let _guard = LOG_LOCK.lock().recover();
    state::private_dir(&home.join(".silicon"))?;
    // The role rides in the origin field so the entry keeps its four-part shape
    // and existing log readers keep parsing.
    let line = format!(
        "[{kind}] [{origin}/{}] [{}] [{}]\n",
        process_role(),
        Utc::now().to_rfc3339(),
        message.replace('\n', "\\n")
    );
    let path = home.join(".silicon/silicon.log");
    let length = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(&path)
        .and_then(|mut file| {
            file.write_all(line.as_bytes())?;
            Ok(file.metadata()?.len())
        })
        .with_context(|| format!("cannot append to {}", path.display()))?;
    if length >= LOG_CAP {
        // The line is written; a failed rotation is reported, not raised over it.
        if let Err(error) = rotate(&path, LOG_CAP, LOG_KEEP) {
            stderr_line(&format!("{error:#}"));
        }
    }
    drop(_guard);
    Ok(())
}

/// Flatten one retained namespace before opening account-owned queues. A marker makes
/// interrupted moves resumable; ambiguous histories never get combined automatically.
pub(crate) fn migrate_account_state(directory: &Path, native_dirs: &[&str]) -> Result<()> {
    use anyhow::bail;
    use std::fs;
    if fs::symlink_metadata(directory).is_ok_and(|meta| meta.file_type().is_symlink()) {
        bail!(
            "account state {} must not be a symlink",
            directory.display()
        );
    }
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries.collect::<std::io::Result<Vec<_>>>()?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("read account state {}", directory.display()))
        }
    };
    let marker = directory.join(".account-state-migration.json");
    let underway: Option<String> = match fs::read(&marker) {
        Ok(bytes) => {
            Some(serde_json::from_slice(&bytes).context("read retained account-state migration")?)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error).context("read account-state migration marker"),
    };
    let mut legacy = Vec::new();
    let mut current = false;
    for entry in entries {
        let name = entry.file_name();
        if name == ".account-state-migration.json"
            || name == ".DS_Store"
            || name.to_string_lossy().starts_with("._")
        {
            continue;
        }
        let kind = entry.file_type()?;
        if kind.is_symlink() {
            bail!(
                "retained account state {} must not be a symlink",
                entry.path().display()
            );
        }
        // An old namespace could itself have been named pending.
        let native = native_dirs.iter().any(|allowed| name == *allowed)
            && !["pending", "seen.json", "inbox.json", "hook.json"]
                .iter()
                .any(|name| entry.path().join(name).exists());
        if kind.is_dir() && !native {
            legacy.push(entry.path());
        } else {
            current = true;
        }
    }
    if legacy.is_empty() {
        if underway.is_some() {
            fs::remove_file(&marker).context("finish account-state migration")?;
        }
        return Ok(());
    }
    if legacy.len() != 1 || (current && underway.is_none()) {
        bail!("{} contains multiple retained namespaces or both old and current account state; no files were moved. Stop the interpreter and reconcile the retained queues before reconnecting", directory.display());
    }
    let source = &legacy[0];
    let name = source
        .file_name()
        .and_then(|name| name.to_str())
        .context("retained namespace must have a UTF-8 name")?;
    if underway.as_deref().is_some_and(|expected| expected != name) {
        bail!(
            "account-state migration in {} names another retained namespace; no files were moved",
            directory.display()
        );
    }
    let entries = fs::read_dir(source)?.collect::<std::io::Result<Vec<_>>>()?;
    for entry in &entries {
        if entry.file_type()?.is_symlink() {
            bail!(
                "retained account state {} must not be a symlink",
                entry.path().display()
            );
        }
        let target = directory.join(entry.file_name());
        if fs::symlink_metadata(&target).is_ok() {
            bail!("cannot move retained account state {} to {}: the destination exists; no files were overwritten", entry.path().display(), target.display());
        }
    }
    state::write_json(&marker, &name)?;
    for entry in entries {
        fs::rename(entry.path(), directory.join(entry.file_name()))
            .context("move retained account state")?;
    }
    fs::remove_dir(source).context("finish retained namespace migration")?;
    fs::remove_file(marker).context("finish account-state migration")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_commands_disable_telemetry() {
        let command = command("tool", Path::new("."));
        let env = command
            .get_envs()
            .collect::<std::collections::HashMap<_, _>>();
        for (name, value) in [
            ("DO_NOT_TRACK", "1"),
            ("DISABLE_TELEMETRY", "1"),
            ("DISABLE_ERROR_REPORTING", "1"),
            ("SILICON_TELEMETRY", "0"),
            ("ACCOUNTS_TELEMETRY", "off"),
            ("APPS_TELEMETRY", "0"),
            ("DM_TELEMETRY_ENABLED", "false"),
            ("BRIEFCASE_TELEMETRY", "0"),
            ("SPACE_STATION_TELEMETRY", "0"),
        ] {
            assert_eq!(
                env[std::ffi::OsStr::new(name)],
                Some(std::ffi::OsStr::new(value))
            );
        }
    }
}
