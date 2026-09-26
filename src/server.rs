use crate::Recover;
use crate::{
    auth,
    config::Config,
    failure, flow,
    runtime::{Connected, NewSession, Runtime, SendOptions},
    state,
};
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, TimeDelta, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::fs::{FileExt, MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc, Mutex, OnceLock, TryLockError, Weak,
};
use std::thread;
use std::time::{Duration, Instant};
use tiny_http::{Header, Method, Request, Response, Server};
use uuid::Uuid;

#[derive(Clone, Default, Deserialize, Serialize)]
pub struct Daemon {
    pub pid: u32,
    pub port: u16,
    pub token: String,
    /// The release answering on `port`; interpreters before this field left it out.
    #[serde(default)]
    pub version: String,
    /// When this interpreter started, RFC 3339 in UTC.
    #[serde(default)]
    pub started_at: String,
    /// The supervisor that started it (`SILICON_SERVICE`: launchd, systemd, run,
    /// windows-task), or none for an interpreter `silicon connect` started by itself.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub supervisor: Option<String>,
    /// Started in the background by `silicon connect` (not in a terminal, not by a process
    /// manager of the user's): the one kind that hands over to an installed service.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub detached: bool,
}

/// Everything but the token, which is a credential.
impl std::fmt::Debug for Daemon {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Daemon")
            .field("pid", &self.pid)
            .field("port", &self.port)
            .field("version", &self.version)
            .field("started_at", &self.started_at)
            .field("supervisor", &self.supervisor)
            .field("detached", &self.detached)
            .finish_non_exhaustive()
    }
}

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
pub struct Connection {
    pub id: String,
    pub yaml: PathBuf,
    pub home: PathBuf,
    pub host: String,
}

/// One saved Silicon as `list` answers it: the saved descriptor, then how its restore is going.
/// Older interpreters listed only connected Silicons, with no state.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Listed {
    #[serde(flatten)]
    pub connection: Connection,
    /// "connected", "restoring" or "waiting".
    #[serde(default = "connected_state")]
    pub state: String,
    /// Why the last restore attempt failed (waiting only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// Restore attempts that failed in a row (waiting only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u32>,
    /// When the next restore attempt runs (waiting only).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_at: Option<String>,
    /// A connected Silicon whose Ting webhook registration keeps failing: why, how often, next try.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ting_error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ting_attempt: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ting_retry_at: Option<String>,
}

fn connected_state() -> String {
    "connected".into()
}

/// How long a quick control action may take before the CLI stops waiting for its answer.
const QUICK: Duration = Duration::from_secs(15);
const QUICK_ACTIONS: &[&str] = &[
    "ping",
    "list",
    "shutdown",
    "restart",
    "settings",
    "settings-set",
    "silicon-ping",
    "configuration",
    "sessions",
    "show",
    "logs",
];
/// Archived session listings grow for years; ureq's 10 MB default would refuse them.
const ANSWER_LIMIT: u64 = 256 * 1024 * 1024;
/// Request threads beyond this answer 503 instead of starting another thread.
const MAX_IN_FLIGHT: usize = 256;
/// How long `silicon stop` and a starting `silicon connect` wait on an interpreter that holds
/// daemon.lock (it is starting, restoring, restarting or stopping).
const LOCK_PATIENCE: Duration = Duration::from_secs(120);
/// How long `silicon connect` waits for another `connect` that is starting the interpreter.
const STARTUP_PATIENCE: Duration = Duration::from_secs(180);
/// `silicon stop --force`: how long SIGTERM gets before SIGKILL, and SIGKILL before giving up.
const FORCE_GRACE: Duration = Duration::from_secs(10);
/// How long the listener may stay unbindable before the interpreter exits for a supervisor.
const REBIND_LIMIT: Duration = Duration::from_secs(300);
/// A listener replaced after an accept failure still answers its open connections until it
/// has had no request for this long.
const RETIRED: Duration = Duration::from_secs(600);
/// Ting registration is idempotent (the saved hook ID is reused), so it is re-asserted this
/// often to heal a hook that silently left the connected state.
const TING_REASSERT: Duration = Duration::from_secs(6 * 60 * 60);

/// The interpreter's state directory: `SILICON_INTERPRETER_HOME`, else
/// `~/.silicon-interpreter`. Never relative to the working directory, which a service
/// manager sets to `/` and a terminal to wherever the user happens to be.
pub fn locate() -> Result<PathBuf> {
    interpreter_directory(
        std::env::var_os("SILICON_INTERPRETER_HOME"),
        std::env::var_os("HOME"),
        passwd_home,
        std::env::current_dir,
    )
}

/// [`locate`] for callers that cannot fail. `serve` and `daemon` locate first and stop with its
/// error, so this absolute fallback is never where an interpreter keeps its state.
pub fn directory() -> PathBuf {
    locate().unwrap_or_else(|_| PathBuf::from("/.silicon-interpreter"))
}

/// A service manager or `env -i` can start silicon without HOME. The interpreter directory,
/// app homes and tools all derive from it, so take the account's home from the password
/// database first. Call at the top of `main`, before any thread starts.
pub fn init_home() {
    let usable = std::env::var_os("HOME").is_some_and(|home| Path::new(&home).is_absolute());
    if !usable {
        if let Ok(home) = passwd_home() {
            std::env::set_var("HOME", home);
        }
    }
}

fn interpreter_directory(
    explicit: Option<OsString>,
    home: Option<OsString>,
    passwd: impl FnOnce() -> std::result::Result<PathBuf, String>,
    current: impl FnOnce() -> std::io::Result<PathBuf>,
) -> Result<PathBuf> {
    if let Some(explicit) = explicit.filter(|value| !value.is_empty()) {
        let path = PathBuf::from(explicit);
        if path.is_absolute() {
            return Ok(path);
        }
        let base = current().with_context(|| {
            format!(
                "SILICON_INTERPRETER_HOME={} is relative, and the current directory it is relative to cannot be read",
                path.display()
            )
        })?;
        return Ok(base.join(path));
    }
    if let Some(home) = home.as_ref().map(PathBuf::from).filter(|p| p.is_absolute()) {
        return Ok(home.join(".silicon-interpreter"));
    }
    let variable = match &home {
        None => "HOME is not set".to_owned(),
        Some(value) if value.is_empty() => "HOME is empty".to_owned(),
        Some(value) => format!("HOME is the relative path {:?}", PathBuf::from(value)),
    };
    match passwd() {
        Ok(account) => Ok(account.join(".silicon-interpreter")),
        Err(reason) => bail!(
            "cannot locate the interpreter directory: SILICON_INTERPRETER_HOME is not set, {variable}, and {reason}; set HOME or SILICON_INTERPRETER_HOME to an absolute path"
        ),
    }
}

/// This account's home directory from the password database.
fn passwd_home() -> std::result::Result<PathBuf, String> {
    use std::os::unix::ffi::OsStrExt;
    let uid = unsafe { libc::getuid() };
    let mut size = 1024;
    loop {
        let mut buffer = vec![0 as libc::c_char; size];
        let mut entry: libc::passwd = unsafe { std::mem::zeroed() };
        let mut found: *mut libc::passwd = std::ptr::null_mut();
        let status = unsafe {
            libc::getpwuid_r(
                uid,
                &mut entry,
                buffer.as_mut_ptr(),
                buffer.len(),
                &mut found,
            )
        };
        if status == libc::ERANGE && size < 1 << 20 {
            size *= 4;
            continue;
        }
        if status != 0 {
            return Err(format!(
                "looking up uid {uid} in the password database failed: {}",
                std::io::Error::from_raw_os_error(status)
            ));
        }
        if found.is_null() || entry.pw_dir.is_null() {
            return Err(format!(
                "the password database has no home directory for uid {uid}"
            ));
        }
        let bytes = unsafe { std::ffi::CStr::from_ptr(entry.pw_dir) }.to_bytes();
        let path = PathBuf::from(std::ffi::OsStr::from_bytes(bytes));
        return if path.is_absolute() {
            Ok(path)
        } else {
            Err(format!(
                "the password database gives uid {uid} the home {path:?}, which is not an absolute path"
            ))
        };
    }
}

pub fn saved() -> Result<Vec<Connection>> {
    let path = directory().join("connections.json");
    match fs::read(&path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .with_context(|| format!("invalid connection registry {}", path.display())),
        // Nothing connected yet; any other read failure is reported, not taken for "empty".
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(error) => Err(error).with_context(|| format!("read {}", path.display())),
    }
}

pub fn descriptor(cfg: &Config) -> Connection {
    let id = cfg.silicon.id.clone().unwrap();
    Connection {
        host: identity_host(
            id.strip_prefix("si:").unwrap(),
            cfg.silicon.org_id.as_deref().unwrap(),
        ),
        id,
        yaml: cfg.path.clone(),
        home: cfg.home.clone(),
    }
}

// Preserve readable existing hosts where possible. Encode both components for
// IAM handles which are not DNS labels; no lossy underscore/hyphen substitution.
fn identity_host(handle: &str, org: &str) -> String {
    if crate::config::host_label(handle) && crate::config::host_label(org) {
        return format!("{handle}.{org}.localhost");
    }
    let encode = |value: &str| {
        let hex: String = value.bytes().map(|b| format!("{b:02x}")).collect();
        hex.as_bytes()
            .chunks(60)
            .map(|chunk| std::str::from_utf8(chunk).unwrap())
            .collect::<Vec<_>>()
            .join(".")
    };
    format!("{}.si.{}.org.localhost", encode(handle), encode(org))
}

#[test]
fn identifier_hosts_preserve_distinct_iam_handles_and_dns_limits() {
    assert_eq!(identity_host("worker", "tos"), "worker.tos.localhost");
    assert_ne!(
        identity_host("worker_name", "tos"),
        identity_host("worker--name", "tos")
    );
    let maximum = identity_host(&"_".repeat(50), &"_".repeat(50));
    assert!(maximum.len() <= 253);
    assert!(maximum.split('.').all(crate::config::host_label));
}

/// A saved host Caddy accepts. A hand-edited registry must not take routing down for every
/// Silicon, so a saved host that is not a `.localhost` DNS name is left unrouted.
fn routable(host: &str) -> bool {
    host.len() <= 253
        && host.to_ascii_lowercase().ends_with(".localhost")
        && host.split('.').all(crate::config::host_label)
}

pub fn compile(path: impl AsRef<Path>) -> Result<Config> {
    let cfg = Config::load(path)?;
    flow::validate(&cfg.flow).context("invalid flow")?;
    for (name, isi) in &cfg.isi {
        if let Some(dna) = &isi.dna {
            crate::eval::validate(dna).with_context(|| format!("isi.{name}.dna"))?;
        }
        if let Some(heartbeat) = &isi.heartbeat {
            crate::eval::validate(heartbeat).with_context(|| format!("isi.{name}.heartbeat"))?;
        }
        if let Some(suggestion) = &isi.new_session_suggestion {
            crate::eval::validate(suggestion)
                .with_context(|| format!("isi.{name}.new_session_suggestion"))?;
        }
    }
    Ok(cfg)
}

/// The running interpreter; with `start`, start one when none is running: through the
/// installed service when there is one, so it is supervised, else as a detached process.
pub fn daemon(start: bool) -> Result<Daemon> {
    let dir = locate()?;
    state::private_dir(&dir)?;
    if start {
        // Starting it is asking for it: a `silicon stop` earlier this boot no longer holds.
        if let Err(error) = crate::service::clear_stopped() {
            crate::stderr_line(&format!("warning: {error:#}"));
        }
    }
    daemon_in(&dir, start, LOCK_PATIENCE, start_serve)
}

/// How a starting interpreter was started.
enum Started {
    /// A detached `silicon serve`, and daemon.log's size before it.
    Spawned(std::process::Child, u64),
    /// Its supervisor was asked to start it; daemon.log's size before the request.
    Supervised { by: String, from: u64 },
}

/// Through the installed service when there is one; if the supervisor cannot start it,
/// say why and start a detached interpreter instead.
fn start_serve(dir: &Path) -> Result<Started> {
    match crate::service::installed() {
        Ok(Some(service)) => {
            let from = fs::metadata(dir.join("daemon.log"))
                .map(|m| m.len())
                .unwrap_or(0);
            // Starting it from a terminal is when 5.0.2 handed over that terminal's
            // environment; the service gets the same through service.env.
            match crate::service::refresh_environment() {
                Ok(notes) => {
                    for note in notes {
                        crate::stderr_line(&format!("note: {note}"));
                    }
                }
                Err(error) => crate::stderr_line(&format!(
                    "warning: service.env was not refreshed from this terminal: {error:#}"
                )),
            }
            match service.start() {
                Ok(()) => {
                    return Ok(Started::Supervised {
                        by: service.mechanism().to_owned(),
                        from,
                    })
                }
                Err(error) => crate::stderr_line(&format!(
                    "warning: the {} service could not start the interpreter, so it starts without a supervisor: {error:#}",
                    service.mechanism()
                )),
            }
        }
        Ok(None) => {}
        Err(error) => crate::stderr_line(&format!(
            "warning: could not read the service record, so the interpreter starts without a supervisor: {error:#}"
        )),
    }
    let (child, start) = spawn_serve(dir)?;
    Ok(Started::Spawned(child, start))
}

fn daemon_in(
    dir: &Path,
    start: bool,
    patience: Duration,
    spawn: impl FnOnce(&Path) -> Result<Started>,
) -> Result<Daemon> {
    // Probing is normal; the reason it failed still explains a stuck or missing interpreter.
    let last = match running(dir) {
        Ok(daemon) => return Ok(daemon),
        Err(error) => error,
    };
    let lock = dir.join("daemon.lock");
    if !start {
        if held(&lock).unwrap_or(false) {
            return Err(last.context(format!(
                "the interpreter{} holds {} but did not answer; it is starting, restarting or stopping, so retry shortly",
                pid_suffix(holder(dir)),
                lock.display()
            )));
        }
        return Err(last.context("interpreter is not running; run silicon connect YAML"));
    }
    let _startup = wait_lock(&dir.join("startup.lock"), STARTUP_PATIENCE)?;
    let last = match running(dir) {
        Ok(daemon) => return Ok(daemon),
        Err(error) => error,
    };
    // One that holds the lock is starting, restoring or restarting; a second would only fail.
    if held(&lock)? {
        if let Some(daemon) = await_holder(dir, patience, last)? {
            return Ok(daemon);
        }
    }
    match spawn(dir)? {
        Started::Spawned(mut child, start) => {
            await_start(dir, &mut child, start, Duration::from_secs(30))
        }
        Started::Supervised { by, from } => await_supervised(dir, &by, from, patience),
    }
}

/// Wait for an interpreter a supervisor was asked to start. launchd may hold a restart for
/// its throttle interval, and the portable supervisor for its crash backoff.
fn await_supervised(dir: &Path, by: &str, from: u64, patience: Duration) -> Result<Daemon> {
    let deadline = Instant::now() + patience;
    loop {
        let last = match running(dir) {
            Ok(daemon) => return Ok(daemon),
            Err(error) => error,
        };
        if Instant::now() >= deadline {
            bail!(
                "the {by} service was asked to start the interpreter, but it did not answer within {}s (see `silicon service status`)\n{}\nlast ping: {last:#}",
                patience.as_secs(),
                startup_output(dir, from)
            );
        }
        thread::sleep(Duration::from_millis(250));
    }
}

/// Start `silicon serve` for `dir`, writing to daemon.log; also the daemon.log size before it.
fn spawn_serve(dir: &Path) -> Result<(std::process::Child, u64)> {
    use std::os::unix::process::CommandExt;
    let log = dir.join("daemon.log");
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(&log)
        .with_context(|| format!("open {}", log.display()))?;
    let start = file
        .metadata()
        .with_context(|| format!("read the size of {}", log.display()))?
        .len();
    let executable = std::env::current_exe()
        .context("locate the silicon executable to start the interpreter")?;
    let mut command = Command::new(&executable);
    command
        .arg("serve")
        // Not the caller's directory, which it would otherwise pin (and a volume with it).
        .current_dir(dir)
        // Started here, it is not supervised, whatever this CLI inherited.
        .env_remove("SILICON_SERVICE")
        .env("SILICON_DETACHED", "1")
        .stdin(Stdio::null())
        .stdout(
            file.try_clone()
                .with_context(|| format!("share {} with the interpreter", log.display()))?,
        )
        .stderr(file);
    if std::env::var_os("SILICON_INTERPRETER_HOME").is_some() {
        // The absolute form: a relative one would resolve against the new working directory.
        command.env("SILICON_INTERPRETER_HOME", dir);
    }
    // Its own session and process group: Ctrl-C, a closed terminal, `timeout` or a job kill
    // aimed at this command never reaches the interpreter or the Caddy and omnids it starts.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
    let child = command
        .spawn()
        .map_err(|error| failure::spawn(dir, &failure::argv(&executable, &["serve"]), &error))
        .with_context(|| format!("start the interpreter from {}", executable.display()))?;
    Ok((child, start))
}

/// The interpreter daemon.json describes, once it answers a ping.
fn running(dir: &Path) -> Result<Daemon> {
    let path = dir.join("daemon.json");
    let bytes = fs::read(&path).with_context(|| format!("read {}", path.display()))?;
    let daemon: Daemon = serde_json::from_slice(&bytes)
        .with_context(|| format!("invalid daemon metadata in {}", path.display()))?;
    call(&daemon, "ping", json!({}))?;
    Ok(daemon)
}

/// Wait for a just-started interpreter. If it dies or stalls, the error carries everything
/// it wrote to daemon.log since `start` and why the last ping failed.
fn await_start(
    dir: &Path,
    child: &mut std::process::Child,
    start: u64,
    patience: Duration,
) -> Result<Daemon> {
    let deadline = Instant::now() + patience;
    loop {
        let last = match running(dir) {
            Ok(daemon) => return Ok(daemon),
            Err(error) => error,
        };
        if let Some(status) = child
            .try_wait()
            .context("check on the starting interpreter")?
        {
            bail!(
                "interpreter exited before answering: {status}\n{}\nlast ping: {last:#}",
                startup_output(dir, start)
            );
        }
        if Instant::now() >= deadline {
            bail!(
                "interpreter (pid {}) did not answer within {}s\n{}\nlast ping: {last:#}",
                child.id(),
                patience.as_secs(),
                startup_output(dir, start)
            );
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// An interpreter holds daemon.lock but does not answer yet. Wait for it rather than start a
/// second one. None: it let go of the lock instead (it stopped), so one may be started.
fn await_holder(dir: &Path, patience: Duration, mut last: anyhow::Error) -> Result<Option<Daemon>> {
    let lock = dir.join("daemon.lock");
    let from = fs::metadata(dir.join("daemon.log"))
        .map(|m| m.len())
        .unwrap_or(0);
    let deadline = Instant::now() + patience;
    loop {
        if !held(&lock)? {
            return Ok(None);
        }
        if Instant::now() >= deadline {
            bail!(
                "the interpreter{} holds {} but did not answer within {}s\n{}\nlast ping: {last:#}",
                pid_suffix(holder(dir)),
                lock.display(),
                patience.as_secs(),
                startup_output(dir, from)
            );
        }
        thread::sleep(Duration::from_millis(250));
        match running(dir) {
            Ok(daemon) => return Ok(Some(daemon)),
            Err(error) => last = error,
        }
    }
}

/// What a starting interpreter wrote (stdout and stderr) to daemon.log, verbatim.
fn startup_output(dir: &Path, start: u64) -> String {
    let path = dir.join("daemon.log");
    match since(&path, start) {
        Ok(text) if text.trim().is_empty() => format!("{}: (empty)", path.display()),
        Ok(text) => failure::mask(
            dir,
            &format!("{}:\n{}", path.display(), text.trim_end()),
            &[],
        ),
        Err(error) => format!("could not read {}: {error:#}", path.display()),
    }
}

/// Everything appended to `path` after byte `start`, across a rotation; a missing file has
/// nothing yet.
fn since(path: &Path, start: u64) -> Result<String> {
    let bytes = crate::appended_since(path, start)
        .with_context(|| format!("read {} from byte {start}", path.display()))?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn lock(path: &Path, nonblocking: bool) -> Result<File> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("open {}", path.display()))?;
    let flags = libc::LOCK_EX | if nonblocking { libc::LOCK_NB } else { 0 };
    if unsafe { libc::flock(file.as_raw_fd(), flags) } != 0 {
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::WouldBlock {
            return Err(error)
                .with_context(|| format!("another interpreter already holds {}", path.display()));
        }
        if error
            .raw_os_error()
            .is_some_and(|code| [libc::ENOTSUP, libc::EOPNOTSUPP, libc::ENOLCK].contains(&code))
        {
            return Err(error).with_context(|| {
                format!(
                    "could not lock {}: it is on a filesystem without file locking; set SILICON_INTERPRETER_HOME to a directory on a local disk",
                    path.display()
                )
            });
        }
        return Err(error).with_context(|| format!("could not lock {}", path.display()));
    }
    Ok(file)
}

/// Whether a lock failed only because another process holds it.
fn would_block(error: &anyhow::Error) -> bool {
    error
        .root_cause()
        .downcast_ref::<std::io::Error>()
        .is_some_and(|cause| cause.kind() == std::io::ErrorKind::WouldBlock)
}

/// A lock another process may hold for a while, taken within `patience`.
fn wait_lock(path: &Path, patience: Duration) -> Result<File> {
    let deadline = Instant::now() + patience;
    loop {
        match lock(path, true) {
            Ok(file) => return Ok(file),
            Err(error) if !would_block(&error) => return Err(error),
            Err(_) if Instant::now() >= deadline => bail!(
                "another `silicon connect` held {} for {}s while starting the interpreter",
                path.display(),
                patience.as_secs()
            ),
            Err(_) => thread::sleep(Duration::from_millis(100)),
        }
    }
}

/// Whether a process holds `path` right now. When it is free the probe takes it for an
/// instant; a starting `serve` allows for that.
fn held(path: &Path) -> Result<bool> {
    // Opened for writing too: NFS emulates flock with byte-range locks, and an exclusive one
    // needs a descriptor open for writing (read-only answers EBADF).
    let file = match OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .or_else(|error| match error.kind() {
            // Someone else's lock file (another user started that interpreter).
            std::io::ErrorKind::PermissionDenied => OpenOptions::new().read(true).open(path),
            _ => Err(error),
        }) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error).with_context(|| format!("open {}", path.display())),
    };
    // Closing the file releases a lock the probe took.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(false);
    }
    let error = std::io::Error::last_os_error();
    if error.kind() == std::io::ErrorKind::WouldBlock {
        return Ok(true);
    }
    Err(error).with_context(|| format!("check whether a process holds {}", path.display()))
}

/// Whether an interpreter holds daemon.lock: running, starting, restarting or stopping.
pub fn interpreter_present() -> bool {
    held(&directory().join("daemon.lock")).unwrap_or(false)
}

/// The pid of the interpreter holding daemon.lock, which it writes there right after locking
/// it; daemon.json's pid for interpreters that did not.
fn holder(dir: &Path) -> Option<u32> {
    fs::read_to_string(dir.join("daemon.lock"))
        .ok()
        .and_then(|text| text.lines().next()?.trim().parse().ok())
        .or_else(|| {
            fs::read(dir.join("daemon.json"))
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Daemon>(&bytes).ok())
                .map(|daemon| daemon.pid)
        })
        .filter(|pid| *pid > 1)
}

fn pid_suffix(pid: Option<u32>) -> String {
    pid.map(|pid| format!(" (pid {pid})")).unwrap_or_default()
}

/// Record this process in the lock it holds, so `silicon stop --force` signals only the holder.
/// Written over the old pid, then cut to length: the first line is never empty, so a reader
/// never falls back to a daemon.json that may name a process long gone.
fn record_holder(file: &File) {
    let line = format!("{}\n", std::process::id());
    let written = file
        .write_all_at(line.as_bytes(), 0)
        .and_then(|()| file.set_len(line.len() as u64));
    if let Err(error) = written {
        crate::stderr_line(&format!(
            "could not record this interpreter's pid in daemon.lock: {error}"
        ));
    }
}

/// `silicon stop`: ask the interpreter to stop, then wait until it lets go of daemon.lock,
/// which proves the process is gone. `waiting` hears the holder's pid once, when a wait begins.
/// With `force`, SIGTERM the holder and SIGKILL it (its process group, when it leads one) if
/// it is still there after ten seconds.
pub fn stop(force: bool, waiting: impl FnMut(Option<u32>)) -> Result<Value> {
    let dir = locate()?;
    stop_in(&dir, force, LOCK_PATIENCE, FORCE_GRACE, waiting)
}

fn stop_in(
    dir: &Path,
    force: bool,
    patience: Duration,
    grace: Duration,
    mut waiting: impl FnMut(Option<u32>),
) -> Result<Value> {
    let lock = dir.join("daemon.lock");
    let from = fs::metadata(dir.join("daemon.log"))
        .map(|m| m.len())
        .unwrap_or(0);
    if force {
        return force_stop(dir, grace, from);
    }
    let ask = || running(dir).and_then(|daemon| call(&daemon, "shutdown", json!({})));
    let mut answer = json!({"stopping": true});
    let mut refused = match ask() {
        Ok(value) => {
            answer = value;
            None
        }
        Err(error) => {
            if !held(&lock)? {
                return Err(error.context("interpreter is not running; run silicon connect YAML"));
            }
            Some(error)
        }
    };
    let pid = holder(dir);
    if held(&lock)? {
        waiting(pid);
    }
    let deadline = Instant::now() + patience;
    let mut asked = Instant::now();
    while held(&lock)? {
        if Instant::now() >= deadline {
            bail!(
                "the interpreter{} still holds {} {}s after it was asked to stop{}\n{}\nrun `silicon stop --force` to terminate it",
                pid_suffix(pid),
                lock.display(),
                patience.as_secs(),
                refused
                    .map(|error| format!(" (the request failed: {error:#})"))
                    .unwrap_or_default(),
                startup_output(dir, from)
            );
        }
        // One that was still starting may answer now.
        if refused.is_some() && asked.elapsed() >= Duration::from_secs(2) {
            asked = Instant::now();
            if let Ok(value) = ask() {
                answer = value;
                refused = None;
            }
        }
        thread::sleep(Duration::from_millis(200));
    }
    answer["stopped"] = json!(true);
    answer["pid"] = json!(pid);
    Ok(answer)
}

fn force_stop(dir: &Path, grace: Duration, from: u64) -> Result<Value> {
    let lock = dir.join("daemon.lock");
    if !held(&lock)? {
        bail!(
            "interpreter is not running: no process holds {}",
            lock.display()
        );
    }
    let pid = holder(dir).ok_or_else(|| {
        anyhow!(
            "a process holds {} but neither it nor daemon.json records its pid; find it with `ps` and stop it yourself",
            lock.display()
        )
    })?;
    if pid == std::process::id() {
        bail!(
            "{} names this process ({pid}) as its holder",
            lock.display()
        );
    }
    let pid = libc::pid_t::try_from(pid).with_context(|| format!("pid {pid} is out of range"))?;
    signal(
        pid,
        libc::SIGTERM,
        "SIGTERM",
        &format!("the interpreter (pid {pid})"),
    )?;
    // An interpreter already stopping (or one that gets a second signal) exits at once and
    // cleanly, so its supervisor does not start it again, as it would after SIGKILL.
    if released(&lock, grace.min(SAME_REQUEST + Duration::from_secs(1)))? {
        return Ok(json!({"stopped":true,"pid":pid,"signal":"SIGTERM"}));
    }
    if held(&lock)? {
        signal(
            pid,
            libc::SIGTERM,
            "SIGTERM",
            &format!("the interpreter (pid {pid}) a second time"),
        )?;
    }
    if released(&lock, grace)? {
        return Ok(json!({"stopped":true,"pid":pid,"signal":"SIGTERM"}));
    }
    // Started by connect (setsid) or a service manager, it leads a group holding its Caddy
    // and omnids; otherwise only the process itself is known to be ours.
    let (target, what) = if unsafe { libc::getpgid(pid) } == pid {
        (-pid, format!("the interpreter's process group {pid}"))
    } else {
        (pid, format!("the interpreter (pid {pid})"))
    };
    if held(&lock)? {
        signal(target, libc::SIGKILL, "SIGKILL", &what)?;
    }
    if released(&lock, grace)? {
        return Ok(json!({"stopped":true,"pid":pid,"signal":"SIGKILL"}));
    }
    bail!(
        "the interpreter (pid {pid}) still holds {} after SIGTERM and then SIGKILL to {what}\n{}",
        lock.display(),
        startup_output(dir, from)
    )
}

/// `kill(target, signal)`; a process that is already gone is what was wanted.
fn signal(target: libc::pid_t, signal: libc::c_int, name: &str, what: &str) -> Result<()> {
    if unsafe { libc::kill(target, signal) } != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).with_context(|| format!("could not send {name} to {what}"));
        }
    }
    Ok(())
}

/// Whether `lock` became free within `patience`.
fn released(lock: &Path, patience: Duration) -> Result<bool> {
    let deadline = Instant::now() + patience;
    loop {
        if !held(lock)? {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        thread::sleep(Duration::from_millis(100));
    }
}

pub fn call(daemon: &Daemon, action: &str, args: Value) -> Result<Value> {
    request(
        &format!("http://127.0.0.1:{}/control", daemon.port),
        &daemon.token,
        action,
        args,
    )
}

pub fn internal(action: &str, args: Value) -> Result<Value> {
    let url = std::env::var("SI_URL").context("si must run inside an ISI (SI_URL missing)")?;
    let token =
        std::env::var("SI_TOKEN").context("si must run inside an ISI (SI_TOKEN missing)")?;
    request(
        &format!("{}/si", url.trim_end_matches('/')),
        &token,
        action,
        args,
    )
}

fn request(url: &str, token: &str, action: &str, args: Value) -> Result<Value> {
    // Quick actions get an answer or an error; connect, installs and sends show progress instead.
    let limit = QUICK_ACTIONS.contains(&action).then_some(QUICK);
    let agent = ureq::Agent::config_builder()
        .http_status_as_error(false)
        // Always loopback: HTTP(S)_PROXY or ALL_PROXY must never carry these calls or their token.
        .proxy(None)
        .timeout_connect(Some(Duration::from_secs(2)))
        .timeout_global(limit)
        .build()
        .new_agent();
    let mut response = agent
        .post(url)
        .header("Authorization", &format!("Bearer {token}"))
        .send_json(json!({"action":action,"args":args}))
        .map_err(|error| waited(error, limit))
        .with_context(|| format!("interpreter request `{action}` to {url} failed"))?;
    let status = response.status();
    let body = response
        .body_mut()
        .with_config()
        .limit(ANSWER_LIMIT)
        .read_to_string()
        .map_err(|error| waited(error, limit))
        .with_context(|| {
            format!("could not read the HTTP {status} answer to `{action}` from {url}")
        })?;
    let value: Value = serde_json::from_str(&body).with_context(|| {
        format!(
            "interpreter answered `{action}` at {url} with HTTP {status} that is not JSON:\n{}",
            if body.trim().is_empty() {
                "(empty body)"
            } else {
                &body
            }
        )
    })?;
    if status.as_u16() >= 400 {
        // The daemon's `error` is already its full cause chain; anything else is shown whole.
        match value.get("error").and_then(Value::as_str) {
            Some(error) => bail!("{error}"),
            None => bail!(
                "interpreter answered `{action}` at {url} with HTTP {status} and no error message:\n{body}"
            ),
        }
    }
    Ok(value)
}

/// A timeout says how long the caller waited, which ureq's own words leave out.
fn waited(error: ureq::Error, limit: Option<Duration>) -> anyhow::Error {
    match (&error, limit) {
        (ureq::Error::Timeout(_), Some(limit)) => {
            anyhow::Error::new(error).context(format!("no answer within {}s", limit.as_secs()))
        }
        _ => anyhow::Error::new(error),
    }
}

struct App {
    runtime: Arc<Runtime>,
    daemon: Daemon,
    /// The interpreter directory: connections.json, daemon.json, daemon.log.
    dir: PathBuf,
    /// Serializes connect, disconnect, settings and app changes, and one restore attempt.
    mutation: Mutex<()>,
    proxy: Mutex<Option<Box<dyn Router>>>,
    /// Held while a Caddy is started, or stopped after it was taken out of `proxy` (each takes
    /// seconds, so neither holds `proxy`). The orderly stop takes it too, so the process never
    /// exits or execs while a Caddy it cannot see is on its way up or down: that one would
    /// be left running, holding port 80.
    handover: Mutex<()>,
    routing: Mutex<Routing>,
    /// Saved Silicons (connections.json) and how each one's restore is going.
    registry: Mutex<Registry>,
    /// The registry version last written to connections.json.
    written: Mutex<u64>,
    /// The root cause of the last failure to save the registry, so the retry does not repeat it.
    unsaved: Mutex<Option<String>>,
    /// Per Silicon: held while its Ting webhook is registered or removed, so a background
    /// retry never re-registers a hook a disconnect or shutdown just removed.
    ting_locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    restart: Restart,
    in_flight: AtomicUsize,
    /// Interactive requests waiting for `mutation`; the restorer lets them in between its
    /// attempts instead of taking the lock straight back.
    waiting: AtomicUsize,
}

/// `mutation` for a request someone is waiting on: connect, disconnect, app changes.
fn interactive(app: &App) -> std::sync::MutexGuard<'_, ()> {
    app.waiting.fetch_add(1, Ordering::SeqCst);
    let guard = app.mutation.lock().recover();
    app.waiting.fetch_sub(1, Ordering::SeqCst);
    guard
}

/// settings.json is read, changed and written whole; two changes at once must not lose one.
static SETTINGS: Mutex<()> = Mutex::new(());

impl App {
    fn new(
        runtime: Arc<Runtime>,
        daemon: Daemon,
        dir: PathBuf,
        saved: Vec<Connection>,
        proxied: bool,
    ) -> Self {
        Self {
            runtime,
            daemon,
            dir,
            mutation: Mutex::new(()),
            proxy: Mutex::new(None),
            handover: Mutex::new(()),
            routing: Mutex::new(Routing {
                disabled: !proxied,
                ..Routing::default()
            }),
            registry: Mutex::new(Registry::restoring(saved)),
            written: Mutex::new(0),
            unsaved: Mutex::new(None),
            ting_locks: Mutex::new(HashMap::new()),
            restart: Restart::new(Arc::new(AtomicBool::new(false))),
            in_flight: AtomicUsize::new(0),
            waiting: AtomicUsize::new(0),
        }
    }
}

/// The owned routing proxy as serve drives it: `update` applies the hosts (and is its health
/// check), and dropping it stops Caddy. A trait so tests can stand in for Caddy.
trait Router: Send {
    fn update(&mut self, hosts: &[String]) -> Result<()>;
}

impl Router for crate::proxy::Proxy {
    fn update(&mut self, hosts: &[String]) -> Result<()> {
        crate::proxy::Proxy::update(self, hosts)
    }
}

/// Start the owned Caddy for `hosts`.
fn start_caddy(dir: &Path, port: u16, hosts: &[String]) -> Result<Box<dyn Router>> {
    crate::proxy::Proxy::start(dir, port, hosts).map(|proxy| Box::new(proxy) as Box<dyn Router>)
}

/// How the owned Caddy is doing, for `ping`, warnings and the supervisor's retries.
#[derive(Default)]
struct Routing {
    /// `--no-proxy`: nothing routes Silicon hosts, by choice.
    disabled: bool,
    up: bool,
    /// When it last came up.
    up_since: Option<DateTime<Utc>>,
    /// Why routing is down, whole.
    error: Option<String>,
    /// Failed starts and health checks since routing last stayed up for ten minutes; they
    /// space out the next start.
    failures: u32,
    next_start: Option<DateTime<Utc>>,
    /// The error last reported in full, so a repeat is not reported again.
    reported: Option<String>,
}

impl Routing {
    fn state(&self) -> String {
        if self.disabled {
            "disabled".into()
        } else if self.up {
            "up".into()
        } else {
            format!(
                "down: {}",
                self.error.as_deref().unwrap_or("it has not started yet")
            )
        }
    }
}

/// An update restart: requested by the updater or the `restart` action, run once the new
/// release passed its check and no work is running.
struct Restart {
    requested: Arc<AtomicBool>,
    check: Mutex<Check>,
    /// This process's executable, remembered at startup: the fallback when the new one will not run.
    current: Option<PathBuf>,
    /// The arguments the restarted interpreter gets.
    args: Vec<OsString>,
    /// Instead of restarting in place, hand over to the installed service: start it (it waits
    /// for daemon.lock) and exit, so the interpreter runs supervised from then on.
    handover: AtomicBool,
    /// The service was started for the handover; the idle-gated restart then exits into it.
    handed: AtomicBool,
}

#[derive(Default)]
struct Check {
    /// The executable that passed its check for this request, resolved, so the restart runs
    /// exactly the binary that passed even when `current` is repointed afterwards.
    passed: Option<PathBuf>,
    /// The resolved executable the last check ran; another one (a later `silicon update`
    /// installed it) is checked at once.
    checked: Option<PathBuf>,
    /// After a failed check: when to check again.
    retry_at: Option<DateTime<Utc>>,
    reported: Option<String>,
    /// When the restart began waiting for idle, and when that was last said.
    since: Option<DateTime<Utc>>,
    noticed: Option<DateTime<Utc>>,
}

impl Restart {
    fn new(requested: Arc<AtomicBool>) -> Self {
        Self {
            requested,
            check: Mutex::new(Check::default()),
            current: remembered(std::env::current_exe()),
            args: vec!["serve".into()],
            handover: AtomicBool::new(false),
            handed: AtomicBool::new(false),
        }
    }

    /// The executable to restart into, once requested and checked.
    fn ready(&self) -> Option<PathBuf> {
        if !self.requested.load(Ordering::SeqCst) {
            return None;
        }
        // A handover stops this interpreter only once the service that takes over has started.
        if self.handover.load(Ordering::SeqCst) && !self.handed.load(Ordering::SeqCst) {
            return None;
        }
        self.check.lock().recover().passed.clone()
    }
}

/// The running executable, resolved. macOS reports the path it was started by, which for a
/// service or PATH start is the stable `<prefix>/bin/silicon`: the very symlink an update
/// repoints, so it would be no fallback at all. Resolved, it is this release's own binary.
fn remembered(executable: std::io::Result<PathBuf>) -> Option<PathBuf> {
    executable
        .map(|path| path.canonicalize().unwrap_or(path))
        .ok()
}

/// What an update restart tries to run, in order: the checked release, then the executable
/// this process started from (and on Linux, this very executable even if its release
/// directory is gone).
fn exec_candidates(target: Option<PathBuf>, current: Option<PathBuf>) -> Vec<PathBuf> {
    let mut candidates: Vec<PathBuf> = target.into_iter().chain(current).collect();
    if cfg!(target_os = "linux") {
        candidates.push(PathBuf::from("/proc/self/exe"));
    }
    candidates.dedup();
    candidates
}

/// What the signal listener needs to stop the interpreter, filled in as startup reaches it.
#[derive(Default)]
struct Stop {
    signalled: AtomicBool,
    /// When the first stop signal arrived, and which: the same signal again moments later is
    /// the same request (system shutdown signals serve directly and through
    /// `silicon service run`); a different one is someone escalating.
    first: Mutex<Option<(Instant, libc::c_int)>>,
    runtime: OnceLock<Arc<Runtime>>,
    app: OnceLock<Weak<App>>,
}

/// Set by the control `shutdown` action (`silicon stop`): a signal after it means the
/// orderly stop is taking too long, as `silicon stop --force` says, so it exits at once and
/// cleanly, and no supervisor restarts an interpreter someone stopped.
static SHUTDOWN_REQUESTED: AtomicBool = AtomicBool::new(false);

/// A second signal this soon after the first is the same stop request.
const SAME_REQUEST: Duration = Duration::from_secs(2);

/// How long a stop waits for Ting registrations in flight before it unhooks.
const TING_DRAIN: Duration = Duration::from_secs(15);

struct Listener {
    handle: signal_hook::iterator::Handle,
    thread: thread::JoinHandle<()>,
}

impl Listener {
    fn close(self) {
        self.handle.close();
        if let Err(panic) = self.thread.join() {
            crate::stderr_line(&format!(
                "signal listener panicked: {}",
                failure::panic_message(&*panic)
            ));
        }
    }
}

/// SIGINT, SIGTERM and SIGHUP (a closed terminal) all mean an orderly stop. The listener keeps
/// listening: a SIGINT or SIGTERM while stopping exits at once, so a stuck stop can always end.
/// A hang-up that was ignored when this process started (`nohup`) stays ignored.
fn listen(stop: Arc<Stop>, dir: PathBuf) -> Result<Listener> {
    let mut wanted = vec![libc::SIGINT, libc::SIGTERM];
    if !ignored(libc::SIGHUP) {
        wanted.push(libc::SIGHUP);
    }
    let mut signals = signal_hook::iterator::Signals::new(&wanted).with_context(|| {
        let names: Vec<String> = wanted.iter().map(|&signal| signal_name(signal)).collect();
        format!("listen for {}", names.join(", "))
    })?;
    let handle = signals.handle();
    let thread = thread::Builder::new()
        .name("signals".into())
        .spawn(move || {
            for signal in signals.forever() {
                let name = signal_name(signal);
                let requested = SHUTDOWN_REQUESTED.load(Ordering::SeqCst);
                if stop.signalled.swap(true, Ordering::SeqCst) || requested {
                    // A closing terminal hangs up more than once (the shell passes it on, and
                    // the kernel sends it to the foreground group); that is no reason to cut
                    // the orderly stop short.
                    if signal == libc::SIGHUP {
                        crate::stderr_line(
                            "received SIGHUP again while stopping; the orderly stop continues (SIGINT or SIGTERM exits at once)",
                        );
                        continue;
                    }
                    let soon = stop
                        .first
                        .lock()
                        .recover()
                        .is_some_and(|(at, first)| first == signal && at.elapsed() < SAME_REQUEST);
                    if soon && !requested {
                        crate::stderr_line(&format!(
                            "received {name} again within {}s of the first; the orderly stop continues",
                            SAME_REQUEST.as_secs()
                        ));
                        continue;
                    }
                    crate::stderr_line(&format!(
                        "received {name} again while stopping; exiting now without finishing the orderly stop"
                    ));
                    exit_now(&stop, &dir);
                }
                stop.first
                    .lock()
                    .recover()
                    .get_or_insert_with(|| (Instant::now(), signal));
                crate::stderr_line(&format!(
                    "received {name}; stopping the interpreter (SIGINT or SIGTERM while it stops exits at once)"
                ));
                if let Some(runtime) = stop.runtime.get() {
                    runtime.stopping.store(true, Ordering::SeqCst);
                }
            }
        })
        .context("start the signal listener")?;
    Ok(Listener { handle, thread })
}

/// Whether `signal` is ignored right now, as `nohup` leaves SIGHUP for the command it starts.
fn ignored(signal: libc::c_int) -> bool {
    let mut current: libc::sigaction = unsafe { std::mem::zeroed() };
    let read = unsafe { libc::sigaction(signal, std::ptr::null(), &mut current) } == 0;
    read && current.sa_sigaction == libc::SIG_IGN
}

fn signal_name(signal: libc::c_int) -> String {
    match signal {
        libc::SIGINT => "SIGINT".into(),
        libc::SIGTERM => "SIGTERM".into(),
        libc::SIGHUP => "SIGHUP".into(),
        other => format!("signal {other}"),
    }
}

/// The second stop signal: stop Caddy if nobody is using it right now, and exit.
fn exit_now(stop: &Stop, dir: &Path) -> ! {
    if let Some(app) = stop.app.get().and_then(Weak::upgrade) {
        let proxy = match app.proxy.try_lock() {
            Ok(mut slot) => slot.take(),
            Err(TryLockError::Poisoned(slot)) => slot.into_inner().take(),
            Err(TryLockError::WouldBlock) => None,
        };
        drop(proxy);
        let _ = fs::remove_file(dir.join("daemon.json"));
    }
    crate::process::terminate_all();
    std::process::exit(0)
}

/// A panic anywhere lands in daemon.log as one line, written without panicking again.
fn install_panic_hook() {
    std::panic::set_hook(Box::new(|info| {
        let current = thread::current();
        let location = info
            .location()
            .map(ToString::to_string)
            .unwrap_or_else(|| "an unknown location".into());
        crate::stderr_line(&format!(
            "thread '{}' panicked at {location}: {}",
            current.name().unwrap_or("<unnamed>"),
            failure::panic_message(info.payload())
        ));
    }));
}

/// macOS starts processes from Terminal and launchd with a soft limit of 256 open files, which
/// months of sessions, sockets and tool pipes can reach, and at the limit accept() fails.
/// Raise the soft limit as far as the hard limit (and on macOS kern.maxfilesperproc) allows,
/// up to 65536. None: it already was that high.
fn raise_open_files() -> Result<Option<(libc::rlim_t, libc::rlim_t)>> {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } != 0 {
        return Err(std::io::Error::last_os_error())
            .context("read the open-file limit (RLIMIT_NOFILE)");
    }
    #[allow(unused_mut)]
    let mut wanted = limit.rlim_max.min(65536);
    #[cfg(target_os = "macos")]
    {
        // RLIM_INFINITY, or anything above this, is refused on macOS.
        wanted = wanted.min(files_per_process()?);
    }
    if wanted <= limit.rlim_cur {
        return Ok(None);
    }
    let raised = libc::rlimit {
        rlim_cur: wanted,
        rlim_max: limit.rlim_max,
    };
    if unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &raised) } != 0 {
        return Err(std::io::Error::last_os_error()).with_context(|| {
            format!(
                "raise the open-file limit from {} to {wanted} (hard limit {})",
                limit.rlim_cur, limit.rlim_max
            )
        });
    }
    Ok(Some((limit.rlim_cur, wanted)))
}

#[cfg(target_os = "macos")]
fn files_per_process() -> Result<libc::rlim_t> {
    let mut value: libc::c_int = 0;
    let mut size = std::mem::size_of::<libc::c_int>();
    let status = unsafe {
        libc::sysctlbyname(
            c"kern.maxfilesperproc".as_ptr(),
            (&mut value as *mut libc::c_int).cast(),
            &mut size,
            std::ptr::null_mut(),
            0,
        )
    };
    if status != 0 {
        return Err(std::io::Error::last_os_error()).context("read kern.maxfilesperproc");
    }
    Ok(libc::rlim_t::try_from(value).unwrap_or(0))
}

/// Point stdout and stderr (fds 1 and 2) at `path`, appending.
fn redirect_output(path: &Path) -> Result<()> {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("open {}", path.display()))?;
    for fd in [1, 2] {
        if unsafe { libc::dup2(file.as_raw_fd(), fd) } < 0 {
            return Err(std::io::Error::last_os_error()).with_context(|| {
                format!(
                    "send the interpreter's output (fd {fd}) to {}",
                    path.display()
                )
            });
        }
    }
    Ok(())
}

/// Whether `fd` is the file at `path` (the same file, not merely the same name).
fn same_file(fd: RawFd, path: &Path) -> bool {
    let copy = unsafe { libc::dup(fd) };
    if copy < 0 {
        return false;
    }
    // Owning the copy closes it again.
    let file = unsafe { File::from_raw_fd(copy) };
    match (file.metadata(), fs::metadata(path)) {
        (Ok(open), Ok(named)) => open.dev() == named.dev() && open.ino() == named.ino(),
        _ => false,
    }
}

/// daemon.log is capped like every other log: past `cap` it moves to daemon.log.1 and this
/// process continues in a fresh file. A daemon.log that was deleted is recreated. True when
/// the output moved to a new file.
fn keep_log(path: &Path, cap: u64) -> Result<bool> {
    let rotated = crate::rotate(path, cap, crate::LOG_KEEP)?;
    if !rotated && same_file(2, path) {
        return Ok(false);
    }
    redirect_output(path)?;
    if rotated {
        crate::stderr_line(&format!(
            "{} reached {cap} bytes; the earlier lines are in {}",
            path.display(),
            crate::numbered(path, "1").display()
        ));
    }
    Ok(true)
}

/// daemon.lock makes one interpreter per directory. Under a supervisor a second copy waits for
/// the first to exit rather than fail (it would only be started again). A CLI probing the
/// lock holds it for an instant, so a plain `serve` tries for a second before it gives up.
/// None: a stop signal arrived while waiting.
fn take_daemon_lock(path: &Path, wait: bool, stop: &Stop) -> Result<Option<File>> {
    let started = Instant::now();
    let mut said = false;
    loop {
        match lock(path, true) {
            Ok(file) => {
                record_holder(&file);
                return Ok(Some(file));
            }
            Err(error) if !would_block(&error) => return Err(error),
            Err(error) if !wait && started.elapsed() >= Duration::from_secs(1) => {
                return Err(error)
            }
            Err(_) if wait && !said => {
                said = true;
                crate::stderr_line(&format!(
                    "another interpreter{} holds {}; waiting for it to exit",
                    pid_suffix(path.parent().and_then(holder)),
                    path.display()
                ));
            }
            Err(_) => {}
        }
        if stop.signalled.load(Ordering::SeqCst) {
            return Ok(None);
        }
        thread::sleep(if wait {
            Duration::from_secs(1)
        } else {
            Duration::from_millis(100)
        });
    }
}

/// Listen on `port`, or the first free port below it.
fn bind(port: u16) -> Result<(Server, u16)> {
    let mut selected = port;
    // A busy port is expected, so step down; why the requested one failed is still kept.
    let mut refused = None;
    let server = loop {
        match Server::http(("127.0.0.1", selected)) {
            Ok(server) => break server,
            Err(error) if selected <= 1024 => bail!(
                "no available port in 1024..={port}: {}port {selected}: {error}",
                refused
                    .map(|first| format!("port {port}: {first}; "))
                    .unwrap_or_default()
            ),
            Err(error) => {
                refused.get_or_insert_with(|| error.to_string());
                selected -= 1;
            }
        }
    };
    if let Some(first) = &refused {
        crate::stderr_line(&format!(
            "port {port} is unavailable ({first}); listening on {selected} instead"
        ));
    }
    Ok((server, selected))
}

/// Run the interpreter until it is stopped. Under a supervisor (`SILICON_SERVICE` set) it
/// waits for daemon.lock instead of failing and writes its own output to daemon.log. A stop
/// (control `shutdown`, SIGINT, SIGTERM, SIGHUP) returns Ok; anything unexpected is an error.
pub fn serve(port: u16, no_proxy: bool) -> Result<()> {
    crate::mark_daemon();
    let supervised = std::env::var_os("SILICON_SERVICE").is_some_and(|value| !value.is_empty());
    let dir = locate()?;
    state::private_dir(&dir)?;
    if supervised {
        // Under any supervisor, daemon.log stays the one place the interpreter's output goes.
        redirect_output(&dir.join("daemon.log"))?;
        // Supervisors hand their jobs a minimal environment; before any thread starts, take
        // back what the user's terminal had (service.env, the login shell's PATH).
        for warning in crate::service::prepare_environment() {
            crate::stderr_line(&warning);
        }
    }
    install_panic_hook();
    let stop = Arc::new(Stop::default());
    let listener = listen(stop.clone(), dir.clone())?;
    let result = serve_locked(port, no_proxy, &dir, supervised, &stop);
    listener.close();
    result
}

fn serve_locked(
    port: u16,
    no_proxy: bool,
    dir: &Path,
    supervised: bool,
    stop: &Arc<Stop>,
) -> Result<()> {
    let Some(_lock) = take_daemon_lock(&dir.join("daemon.lock"), supervised, stop)? else {
        return Ok(());
    };
    match raise_open_files() {
        Ok(Some((from, to))) => {
            crate::stderr_line(&format!("raised the open-file limit from {from} to {to}"))
        }
        Ok(None) => {}
        Err(error) => crate::stderr_line(&format!("{error:#}; continuing with the current limit")),
    }
    let log = dir.join("daemon.log");
    // A serve in a terminal writes there; only a daemon.log it writes to is its to rotate.
    let owns_log = supervised || same_file(2, &log);
    if owns_log {
        if let Err(error) = keep_log(&log, crate::LOG_CAP) {
            crate::stderr_line(&format!("{error:#}"));
        }
    }
    let (server, selected) = bind(port)?;
    let runtime = Runtime::new(format!("http://127.0.0.1:{selected}"));
    let _ = stop.runtime.set(runtime.clone());
    if stop.signalled.load(Ordering::SeqCst) {
        runtime.stopping.store(true, Ordering::SeqCst);
    }
    let saved = load_registry(dir)?;
    let daemon = Daemon {
        pid: std::process::id(),
        port: selected,
        token: Uuid::new_v4().to_string(),
        version: env!("CARGO_PKG_VERSION").into(),
        started_at: Utc::now().to_rfc3339(),
        supervisor: std::env::var("SILICON_SERVICE")
            .ok()
            .filter(|value| !value.is_empty()),
        detached: std::env::var_os("SILICON_DETACHED").is_some_and(|value| value == "1"),
    };
    // Written before anything slow, so `ping` answers while Silicons are still being restored.
    state::write_json(&dir.join("daemon.json"), &daemon)?;
    let mut app = App::new(runtime.clone(), daemon, dir.to_path_buf(), saved, !no_proxy);
    app.restart.args = restart_args(port, no_proxy);
    let app = Arc::new(app);
    let _ = stop.app.set(Arc::downgrade(&app));
    // A failure is reported and retried; routing is the only thing that needs Caddy.
    supervise_proxy(&app, Utc::now(), |hosts| start_caddy(dir, selected, hosts));
    runtime.start_scheduler();
    let exit = match start_threads(&app, owns_log) {
        Ok(()) => {
            crate::update::start(&runtime, app.restart.requested.clone());
            crate::stdout_line(&format!("silicon interpreter listening on {}", runtime.url));
            accept(&app, server)
        }
        Err(error) => {
            drop(server);
            Exit::Failed(error)
        }
    };
    close(&app);
    match exit {
        Exit::Restart if !stop.signalled.load(Ordering::SeqCst) => {
            if app.restart.handed.load(Ordering::SeqCst) {
                crate::stderr_line(
                    "handed over to the service, which takes daemon.lock and restores every saved Silicon now that this process exits",
                );
                return Ok(());
            }
            let target = app.restart.check.lock().recover().passed.clone();
            Err(exec_restart(&app, target))
        }
        Exit::Failed(error) => Err(error),
        _ => Ok(()),
    }
}

/// Supervisors whose `serve` waits for daemon.lock when started beside a running interpreter,
/// so it can take over the moment this one exits. The portable supervisor (cron) starts only
/// when nothing holds the lock; it takes over at the next boot instead.
pub(crate) const HANDOVER_MECHANISMS: &[&str] = &["launchd", "systemd", "windows-task"];

/// Start the installed service for this interpreter directory; its `serve` waits for
/// daemon.lock, which this process releases when it exits.
fn start_handover() -> Result<String> {
    let service = crate::service::installed()?
        .ok_or_else(|| anyhow!("no service is installed (see `silicon service status`)"))?;
    let mechanism = service.mechanism().to_owned();
    if !HANDOVER_MECHANISMS.contains(&mechanism.as_str()) {
        bail!("the {mechanism} supervisor starts an interpreter only when none is running, so it takes over at the next boot instead");
    }
    service.start()?;
    Ok(mechanism)
}

/// A handover asked for by `silicon connect`: start the service first, while this interpreter
/// keeps working. Only once it has started does the idle-gated restart exit into it; if it
/// cannot start, this interpreter keeps running and says why, once.
fn handover_tick(app: &App, start: impl FnOnce() -> Result<String>) {
    let restart = &app.restart;
    if !restart.handover.load(Ordering::SeqCst)
        || restart.handed.load(Ordering::SeqCst)
        || !restart.requested.load(Ordering::SeqCst)
    {
        return;
    }
    match start() {
        Ok(mechanism) => {
            restart.handed.store(true, Ordering::SeqCst);
            crate::stderr_line(&format!(
                "started the {mechanism} service; its interpreter takes over as soon as no work is running here"
            ));
        }
        Err(error) => {
            restart.handover.store(false, Ordering::SeqCst);
            restart.requested.store(false, Ordering::SeqCst);
            report_all(
                app,
                "interpreter",
                &format!("could not hand over to the installed service, so this interpreter keeps running without a supervisor: {error:#}"),
            );
        }
    }
}

fn restart_args(port: u16, no_proxy: bool) -> Vec<OsString> {
    let mut args: Vec<OsString> = vec!["serve".into(), "--port".into(), port.to_string().into()];
    if no_proxy {
        args.push("--no-proxy".into());
    }
    args
}

/// Why the accept loop ended.
enum Exit {
    Stopped,
    Restart,
    Failed(anyhow::Error),
}

/// The accept loop. An accept failure (EMFILE, ECONNABORTED …) ends tiny_http's accept thread
/// for good, so the listener is bound again on the same port rather than taking the
/// interpreter down with it.
fn accept(app: &Arc<App>, mut server: Server) -> Exit {
    let port = app.daemon.port;
    let mut retired: Vec<(Server, Instant)> = Vec::new();
    loop {
        if app.runtime.stopping.load(Ordering::SeqCst) {
            return Exit::Stopped;
        }
        if app.restart.ready().is_some() {
            let guard = match app.mutation.try_lock() {
                Ok(guard) => Some(guard),
                Err(TryLockError::Poisoned(guard)) => Some(guard.into_inner()),
                Err(TryLockError::WouldBlock) => None,
            };
            if guard.is_some() && app.runtime.begin_restart_if_idle() {
                return Exit::Restart;
            }
        }
        drain_retired(
            &mut retired,
            Instant::now(),
            |old| old.try_recv().ok().flatten(),
            |request| dispatch(app, request),
        );
        match server.recv_timeout(Duration::from_millis(200)) {
            Ok(Some(request)) => dispatch(app, request),
            Ok(None) => {}
            Err(error) => {
                let url = app.runtime.url.clone();
                crate::stderr_line(&format!(
                    "accepting connections on {url} failed: {error}; listening on port {port} again"
                ));
                let pause = if out_of_files(&error) {
                    Duration::from_secs(1)
                } else {
                    Duration::ZERO
                };
                let stopping = || app.runtime.stopping.load(Ordering::SeqCst);
                match rebind(port, REBIND_LIMIT, pause, stopping) {
                    Ok(Some(fresh)) => {
                        crate::stderr_line(&format!("listening on {url} again"));
                        retired.push((std::mem::replace(&mut server, fresh), Instant::now()));
                    }
                    Ok(None) => return Exit::Stopped,
                    Err(rebinding) => {
                        return Exit::Failed(rebinding.context(format!(
                            "accepting connections on {url} failed ({error}) and the port could not be listened on again"
                        )))
                    }
                }
            }
        }
    }
}

/// A listener replaced after an accept failure still hears requests on connections it had
/// already accepted, and Caddy keeps an upstream connection open for as long as events keep
/// arriving on it. So each is kept until it has had nothing for `RETIRED`, counted from its
/// last request rather than from when it was replaced.
fn drain_retired<S, R>(
    retired: &mut Vec<(S, Instant)>,
    now: Instant,
    next: impl Fn(&S) -> Option<R>,
    mut handle: impl FnMut(R),
) {
    for (old, active) in retired.iter_mut() {
        while let Some(request) = next(old) {
            *active = now;
            handle(request);
        }
    }
    retired.retain(|(_, active)| now.saturating_duration_since(*active) < RETIRED);
}

fn out_of_files(error: &(dyn std::error::Error + 'static)) -> bool {
    error
        .downcast_ref::<std::io::Error>()
        .and_then(std::io::Error::raw_os_error)
        .is_some_and(|code| code == libc::EMFILE || code == libc::ENFILE)
}

/// Listen on 127.0.0.1:`port` again, backing off from 100 ms to 5 s (at least `pause` after
/// running out of file descriptors). None: the interpreter started stopping meanwhile.
fn rebind(
    port: u16,
    limit: Duration,
    pause: Duration,
    stopping: impl Fn() -> bool,
) -> Result<Option<Server>> {
    let started = Instant::now();
    let mut delay = Duration::from_millis(100);
    let mut wait = pause;
    let mut attempts = 0;
    let mut last: Option<String> = None;
    loop {
        let until = Instant::now() + wait;
        while Instant::now() < until && !stopping() {
            thread::sleep(
                until
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(100)),
            );
        }
        if stopping() {
            return Ok(None);
        }
        attempts += 1;
        match Server::http(("127.0.0.1", port)) {
            Ok(server) => return Ok(Some(server)),
            Err(error) => {
                let text = error.to_string();
                if last.as_deref() != Some(text.as_str()) {
                    crate::stderr_line(&format!(
                        "could not listen on 127.0.0.1:{port} again: {text}; retrying"
                    ));
                }
                wait = if out_of_files(error.as_ref()) {
                    delay.max(pause).max(Duration::from_secs(1))
                } else {
                    delay
                };
                last = Some(text);
            }
        }
        if started.elapsed() >= limit {
            bail!(
                "could not listen on 127.0.0.1:{port} again for {}s ({attempts} attempts); last error: {}",
                limit.as_secs(),
                last.unwrap_or_default()
            );
        }
        delay = (delay * 2).min(Duration::from_secs(5));
    }
}

/// Handle `request` on its own thread, unless too many are already running.
fn dispatch(app: &Arc<App>, request: Request) {
    struct InFlight(Arc<App>);
    impl Drop for InFlight {
        fn drop(&mut self) {
            self.0.in_flight.fetch_sub(1, Ordering::SeqCst);
        }
    }
    let running = app.in_flight.fetch_add(1, Ordering::SeqCst);
    let guard = InFlight(app.clone());
    if running >= MAX_IN_FLIGHT {
        drop(guard);
        respond(
            request,
            503,
            json!({"error":format!("interpreter busy: {running} requests in flight; retry")}),
        );
        return;
    }
    // A failed spawn drops its closure; the request stays here to be answered.
    let slot = Arc::new(Mutex::new(Some(request)));
    let (worker, pending) = (app.clone(), slot.clone());
    let spawned = thread::Builder::new().name("http".into()).spawn(move || {
        let _guard = guard;
        let request = pending.lock().recover().take();
        if let Some(request) = request {
            handle(worker, request);
        }
    });
    if let Err(error) = spawned {
        let request = slot.lock().recover().take();
        if let Some(request) = request {
            respond(
                request,
                503,
                json!({"error":format!("could not start a request thread: {error}; retry")}),
            );
        }
    }
}

/// The orderly end: Ting retries finish first, so none re-registers a hook the shutdown
/// removes; then every Silicon, Caddy and daemon.json. Cleanup failures are reported, never
/// raised: an update restart must still happen after them.
fn close(app: &App) {
    app.runtime.stopping.store(true, Ordering::SeqCst);
    // A Ting registration in flight finishes (or is dropped) before the unhook, but only for
    // so long: supervisors allow a stop 90 seconds, and a registration that completes after
    // the unhook only leaves a hook the next start reuses.
    let locks: Vec<_> = app.ting_locks.lock().recover().values().cloned().collect();
    let deadline = Instant::now() + TING_DRAIN;
    for lock in locks {
        loop {
            match lock.try_lock() {
                Ok(_) | Err(TryLockError::Poisoned(_)) => break,
                Err(TryLockError::WouldBlock) if Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(50))
                }
                Err(TryLockError::WouldBlock) => {
                    crate::stderr_line(&format!(
                        "a Ting registration was still running after {}s; stopping without waiting for it (the next start registers the same hook again)",
                        TING_DRAIN.as_secs()
                    ));
                    break;
                }
            }
        }
    }
    app.runtime.shutdown();
    // A Caddy the supervisor is starting right now is stored or stopped before this goes on;
    // once this process exits or execs, nothing would stop it.
    let handover = app.handover.lock().recover();
    let proxy = app.proxy.lock().recover().take();
    drop(handover);
    drop(proxy);
    // Tools still running for a request that was cut short (a setup script, an install)
    // run in their own process groups; nothing would enforce their time limits any more.
    crate::process::terminate_all();
    let path = app.dir.join("daemon.json");
    match fs::remove_file(&path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => crate::stderr_line(&format!("could not remove {}: {error}", path.display())),
    }
}

/// Replace this process with the updated interpreter. When that will not start, the
/// executable this process started from, so the machine keeps an interpreter; only when
/// neither starts does the error end the process (a supervisor then restarts it).
fn exec_restart(app: &App, target: Option<PathBuf>) -> anyhow::Error {
    use std::os::unix::process::CommandExt;
    crate::telemetry::flush();
    let candidates = exec_candidates(target, app.restart.current.clone());
    let args = &app.restart.args;
    let shown = args
        .iter()
        .map(|arg| arg.to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join(" ");
    let mut failures = Vec::new();
    for executable in candidates {
        crate::stderr_line(&format!(
            "restarting the interpreter as {} {shown}",
            executable.display()
        ));
        let error = Command::new(&executable).args(args).exec();
        let message = format!(
            "could not restart the interpreter as {} {shown}: {error}",
            executable.display()
        );
        report_all(app, "interpreter", &message);
        failures.push(message);
    }
    if failures.is_empty() {
        return anyhow!("could not restart the interpreter: no executable to run");
    }
    anyhow!("{}", failures.join("\n"))
}

/// Start the background loops: restore, Ting re-registration, and the supervisor (Caddy,
/// daemon.log, the registry file, update restarts).
fn start_threads(app: &Arc<App>, owns_log: bool) -> Result<()> {
    every(app, "restorer", Duration::from_secs(1), |app| {
        restore_next(app, Utc::now(), attempt_restore)
    })?;
    every(app, "ting", Duration::from_secs(1), |app| {
        reassert_next(app, Utc::now(), register_quietly)
    })?;
    let log = app.dir.join("daemon.log");
    let mut checked = Instant::now();
    every(app, "supervisor", Duration::from_secs(5), move |app| {
        supervise_proxy(app, Utc::now(), |hosts| {
            start_caddy(&app.dir, app.daemon.port, hosts)
        });
        save_pending(app);
        if owns_log && checked.elapsed() >= Duration::from_secs(60) {
            checked = Instant::now();
            if let Err(error) = keep_log(&log, crate::LOG_CAP) {
                crate::stderr_line(&format!("{error:#}"));
            }
        }
        restart_tick(
            app,
            Utc::now(),
            || restart_target(app),
            |executable| preflight(&app.dir, executable),
        );
        handover_tick(app, start_handover);
        false
    })?;
    Ok(())
}

/// A background loop that outlives its own panics: each round runs under catch_unwind, and a
/// panic is reported and the loop goes on. `work` answers true when there is more to do now.
fn every(
    app: &Arc<App>,
    name: &'static str,
    tick: Duration,
    mut work: impl FnMut(&App) -> bool + Send + 'static,
) -> Result<()> {
    let app = app.clone();
    thread::Builder::new()
        .name(name.into())
        .spawn(move || {
            while !app.runtime.stopping.load(Ordering::SeqCst) {
                let busy = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| work(&app)))
                    .unwrap_or_else(|panic| {
                        crate::stderr_line(&format!(
                            "the interpreter's {name} loop panicked: {}; it continues",
                            failure::panic_message(&*panic)
                        ));
                        false
                    });
                if !busy {
                    pause(&app.runtime, tick);
                }
            }
        })
        .map(|_| ())
        .with_context(|| format!("start the interpreter's {name} thread"))
}

/// `step`, with a panic turned into its failure, so it is retried with backoff like any other
/// instead of at once, forever.
fn contained<T>(what: &str, step: impl FnOnce() -> Result<T>) -> Result<T> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(step)).unwrap_or_else(|panic| {
        Err(anyhow!(
            "{what} panicked: {}",
            failure::panic_message(&*panic)
        ))
    })
}

/// Sleep for `length`, waking early when the interpreter starts stopping.
fn pause(runtime: &Runtime, length: Duration) {
    let deadline = Instant::now() + length;
    while !runtime.stopping.load(Ordering::SeqCst) {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            break;
        }
        thread::sleep(left.min(Duration::from_millis(200)));
    }
}

/// `first` doubled after each failure in a row, up to `cap`.
fn backoff(failures: u32, first: Duration, cap: Duration) -> Duration {
    let factor = 1u32
        .checked_shl(failures.saturating_sub(1))
        .unwrap_or(u32::MAX);
    first.saturating_mul(factor).min(cap)
}

/// `delay` spread uniformly over ±`spread` (a fraction), so Silicons that failed together do
/// not retry in lockstep.
fn jittered(delay: Duration, spread: f64) -> Duration {
    // 53 random bits of a v4 UUID (its low 62 bits carry no version or variant).
    let bits = (Uuid::new_v4().as_u128() as u64) & ((1 << 62) - 1);
    let unit = (bits >> 9) as f64 / (1u64 << 53) as f64;
    delay.mul_f64(1.0 - spread + 2.0 * spread * unit)
}

/// The next restore or Ting attempt after `failures` failures in a row: 5 s doubling to
/// 10 minutes, ±20 %.
fn retry_delay(failures: u32) -> Duration {
    jittered(
        backoff(failures, Duration::from_secs(5), Duration::from_secs(600)),
        0.2,
    )
}

fn later(now: DateTime<Utc>, delay: Duration) -> DateTime<Utc> {
    now + TimeDelta::from_std(delay).unwrap_or(TimeDelta::days(1))
}

/// Whether a wall-clock time `at`, set at most `longest` ahead, has come. One further ahead than
/// that means the clock was set back since, so it is due now rather than hours or days late.
fn arrived(at: DateTime<Utc>, now: DateTime<Utc>, longest: Duration) -> bool {
    at <= now || at > later(now, longest)
}

/// The longest restore or Ting retry delay: 10 minutes plus its 20 % jitter.
const LONGEST_RETRY: Duration = Duration::from_secs(720);

/// How a saved Silicon's restore is going.
#[derive(Clone, Debug, PartialEq)]
enum Restore {
    /// Not attempted yet in this interpreter, or an attempt is running.
    Restoring,
    Connected,
    /// The last attempt failed; the next runs at `next_at`.
    Waiting {
        next_at: DateTime<Utc>,
        error: String,
    },
}

/// Ting registration for a connected Silicon: the next re-assertion, or a retry after failures.
#[derive(Clone, Debug, Default)]
struct TingState {
    next_at: Option<DateTime<Utc>>,
    failures: u32,
    error: Option<String>,
    reported: Option<String>,
}

#[derive(Clone, Debug)]
struct Entry {
    connection: Connection,
    state: Restore,
    /// Restore attempts that failed in a row.
    failures: u32,
    /// The restore error last reported in full, so a repeat is not reported again.
    reported: Option<String>,
    /// `failures` when daemon.log last said Ting deliveries for it are refused.
    refused: Option<u32>,
    ting: TingState,
}

impl Entry {
    fn new(connection: Connection) -> Self {
        Self {
            connection,
            state: Restore::Restoring,
            failures: 0,
            reported: None,
            refused: None,
            ting: TingState::default(),
        }
    }

    /// The same Silicon, YAML file or home: one registry entry each.
    fn overlaps(&self, other: &Connection) -> bool {
        self.connection.id == other.id
            || self.connection.yaml == other.yaml
            || self.connection.home == other.home
    }

    fn due(&self, now: DateTime<Utc>) -> bool {
        match &self.state {
            Restore::Restoring => true,
            Restore::Connected => false,
            Restore::Waiting { next_at, .. } => arrived(*next_at, now, LONGEST_RETRY),
        }
    }
}

/// connections.json, the source of truth for which Silicons this interpreter restores; the
/// live runtime set is only the part that is connected right now. Every change to the saved
/// set bumps `version`, and `persist` writes each version once.
#[derive(Clone, Debug, Default)]
struct Registry {
    entries: Vec<Entry>,
    version: u64,
}

/// A failed restore attempt: when the next one runs, and whether the error is new.
struct Failed {
    attempt: u32,
    next_at: DateTime<Utc>,
    changed: bool,
}

impl Registry {
    /// Saved connections, each waiting for its first restore.
    fn restoring(connections: Vec<Connection>) -> Self {
        let mut registry = Self::default();
        for connection in connections {
            if !registry
                .entries
                .iter()
                .any(|entry| entry.connection.id == connection.id)
            {
                registry.entries.push(Entry::new(connection));
            }
        }
        registry
    }

    fn entry(&self, id: &str) -> Option<&Entry> {
        self.entries.iter().find(|entry| entry.connection.id == id)
    }

    fn entry_mut(&mut self, id: &str) -> Option<&mut Entry> {
        self.entries
            .iter_mut()
            .find(|entry| entry.connection.id == id)
    }

    /// Save `connection` in place of `anchor` (or the entry with its ID, YAML or home, in that
    /// order); every other entry for the same Silicon, YAML or home is superseded.
    fn upsert(&mut self, anchor: Option<&str>, connection: Connection) -> &mut Entry {
        let found = anchor
            .and_then(|anchor| self.entries.iter().position(|e| e.connection.id == anchor))
            .or_else(|| {
                self.entries
                    .iter()
                    .position(|e| e.connection.id == connection.id)
            })
            .or_else(|| {
                self.entries
                    .iter()
                    .position(|e| e.connection.yaml == connection.yaml)
            })
            .or_else(|| {
                self.entries
                    .iter()
                    .position(|e| e.connection.home == connection.home)
            });
        let mut index = match found {
            Some(index) => {
                if self.entries[index].connection != connection {
                    self.entries[index].connection = connection.clone();
                    self.version += 1;
                }
                index
            }
            None => {
                self.entries.push(Entry::new(connection.clone()));
                self.version += 1;
                self.entries.len() - 1
            }
        };
        for other in (0..self.entries.len()).rev() {
            if other != index && self.entries[other].overlaps(&connection) {
                self.entries.remove(other);
                self.version += 1;
                if other < index {
                    index -= 1;
                }
            }
        }
        &mut self.entries[index]
    }

    fn remove(&mut self, id: &str) -> Option<Entry> {
        let index = self.entries.iter().position(|e| e.connection.id == id)?;
        self.version += 1;
        Some(self.entries.remove(index))
    }

    fn connections(&self) -> Vec<Connection> {
        self.entries.iter().map(|e| e.connection.clone()).collect()
    }

    /// The next Silicon to restore: every one gets a first attempt before any is retried,
    /// then the one that has waited longest goes first.
    fn due_restore(&self, now: DateTime<Utc>) -> Option<String> {
        self.entries
            .iter()
            .filter(|entry| entry.due(now))
            .min_by_key(|entry| {
                let next = match &entry.state {
                    Restore::Waiting { next_at, .. } => Some(*next_at),
                    _ => None,
                };
                (entry.failures > 0, next)
            })
            .map(|entry| entry.connection.id.clone())
    }

    fn due_ting(&self, now: DateTime<Utc>) -> Option<String> {
        self.entries
            .iter()
            .find(|entry| {
                entry.state == Restore::Connected
                    && entry
                        .ting
                        .next_at
                        .is_some_and(|at| arrived(at, now, TING_REASSERT))
            })
            .map(|entry| entry.connection.id.clone())
    }

    /// `connection` came up (restored in place of `anchor`, or connected): Ting was just
    /// registered, so the next re-assertion is due in six hours.
    fn connected(&mut self, anchor: Option<&str>, connection: Connection, now: DateTime<Utc>) {
        let entry = self.upsert(anchor, connection);
        entry.state = Restore::Connected;
        entry.failures = 0;
        entry.reported = None;
        entry.refused = None;
        entry.ting = TingState {
            next_at: Some(later(now, TING_REASSERT)),
            ..TingState::default()
        };
    }

    /// A restore attempt of `id` failed with `error` (already masked).
    fn failed(&mut self, id: &str, error: String, now: DateTime<Utc>) -> Option<Failed> {
        let entry = self.entry_mut(id)?;
        entry.failures += 1;
        let next_at = later(now, retry_delay(entry.failures));
        let changed = entry.reported.as_deref() != Some(error.as_str());
        entry.reported = Some(error.clone());
        entry.state = Restore::Waiting { next_at, error };
        Some(Failed {
            attempt: entry.failures,
            next_at,
            changed,
        })
    }

    /// Registering `id`'s Ting webhook failed with `error` (already masked).
    fn ting_failed(&mut self, id: &str, error: String, now: DateTime<Utc>) -> Option<Failed> {
        let ting = &mut self.entry_mut(id)?.ting;
        ting.failures += 1;
        let next_at = later(now, retry_delay(ting.failures));
        let changed = ting.reported.as_deref() != Some(error.as_str());
        ting.reported = Some(error.clone());
        ting.error = Some(error);
        ting.next_at = Some(next_at);
        Some(Failed {
            attempt: ting.failures,
            next_at,
            changed,
        })
    }

    /// Registering `id`'s Ting webhook succeeded; the failures before it, if any.
    fn ting_ok(&mut self, id: &str, now: DateTime<Utc>) -> Option<u32> {
        let ting = &mut self.entry_mut(id)?.ting;
        let failures = ting.failures;
        *ting = TingState {
            next_at: Some(later(now, TING_REASSERT)),
            ..TingState::default()
        };
        Some(failures)
    }

    fn counts(&self) -> (usize, usize) {
        let restoring = self
            .entries
            .iter()
            .filter(|e| e.state == Restore::Restoring)
            .count();
        let waiting = self
            .entries
            .iter()
            .filter(|e| matches!(e.state, Restore::Waiting { .. }))
            .count();
        (restoring, waiting)
    }

    fn listed(&self) -> Vec<Listed> {
        self.entries
            .iter()
            .map(|entry| {
                let mut row = Listed {
                    connection: entry.connection.clone(),
                    state: connected_state(),
                    error: None,
                    attempt: None,
                    retry_at: None,
                    ting_error: None,
                    ting_attempt: None,
                    ting_retry_at: None,
                };
                match &entry.state {
                    Restore::Restoring => row.state = "restoring".into(),
                    Restore::Connected => {
                        if let Some(error) = &entry.ting.error {
                            row.ting_error = Some(error.clone());
                            row.ting_attempt = Some(entry.ting.failures);
                            row.ting_retry_at = entry.ting.next_at.map(|at| at.to_rfc3339());
                        }
                    }
                    Restore::Waiting { next_at, error } => {
                        row.state = "waiting".into();
                        row.error = Some(error.clone());
                        row.attempt = Some(entry.failures);
                        row.retry_at = Some(next_at.to_rfc3339());
                    }
                }
                row
            })
            .collect()
    }

    /// Why a saved Silicon that is not connected cannot take requests yet.
    fn unavailable(&self, id: &str) -> String {
        match self.entry(id).map(|entry| (&entry.state, entry.failures)) {
            Some((Restore::Waiting { next_at, error }, failures)) => format!(
                "silicon {id} is not restored yet (attempt {failures} failed; the next runs at {}): {error}",
                next_at.to_rfc3339()
            ),
            Some((Restore::Restoring, _)) => {
                format!("silicon {id} is being restored; retry shortly")
            }
            _ => format!("silicon {id} is not connected"),
        }
    }
}

/// connections.json. One that cannot be read or parsed must not keep every Silicon offline:
/// it is moved aside whole (never deleted), reported, and the interpreter starts with none.
fn load_registry(dir: &Path) -> Result<Vec<Connection>> {
    let path = dir.join("connections.json");
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        // Not damaged, only unreadable right now (permissions, a disk error): starting empty
        // would forget every saved Silicon at the next save. The file stays where it is, and
        // a supervisor starts the interpreter again after this error.
        Err(error) => {
            return Err(error).with_context(|| {
                format!(
                    "read the saved connections in {}; the file is left in place",
                    path.display()
                )
            })
        }
    };
    match serde_json::from_slice(&bytes) {
        Ok(connections) => Ok(connections),
        Err(error) => {
            quarantine(
                &path,
                &format!("is not a valid connection registry: {error}"),
            )?;
            Ok(Vec::new())
        }
    }
}

fn quarantine(path: &Path, problem: &str) -> Result<PathBuf> {
    let target = crate::numbered(
        path,
        &format!("corrupt-{}", Utc::now().format("%Y%m%dT%H%M%S%.3fZ")),
    );
    fs::rename(path, &target).with_context(|| {
        format!(
            "{} {problem}, and it could not be moved aside to {}",
            path.display(),
            target.display()
        )
    })?;
    crate::stderr_line(&format!(
        "{} {problem}; moved it to {} and started with no saved Silicons. Connect them again with `silicon connect`, or repair that file and put it back while the interpreter is stopped.",
        path.display(),
        target.display()
    ));
    Ok(target)
}

/// Write the registry when it changed since the last write. Writers take a snapshot under the
/// registry lock and write under `written`, so the newest snapshot always wins.
fn persist(app: &App) -> Result<()> {
    let (version, connections) = {
        let registry = app.registry.lock().recover();
        (registry.version, registry.connections())
    };
    let mut written = app.written.lock().recover();
    if *written >= version {
        return Ok(());
    }
    state::write_json(&app.dir.join("connections.json"), &connections)?;
    *written = version;
    Ok(())
}

/// The supervisor's retry of a registry write that failed (a full or read-only disk). Each
/// attempt's error names a new staged file, so the failure is reported whole once per root
/// cause (the OS error), not once per attempt. True when it wrote a line.
fn save_pending(app: &App) -> bool {
    let result = persist(app);
    let mut unsaved = app.unsaved.lock().recover();
    match result {
        Ok(()) => {
            let recovered = unsaved.take().is_some();
            if recovered {
                crate::stderr_line("saved the connection registry after the earlier failure");
            }
            recovered
        }
        Err(error) => {
            let cause = error.root_cause().to_string();
            if unsaved.as_deref() == Some(cause.as_str()) {
                return false;
            }
            crate::stderr_line(&format!("{error:#}; retrying every 5 seconds"));
            *unsaved = Some(cause);
            true
        }
    }
}

/// The result of one restore attempt that connected the Silicon.
struct Restored {
    connection: Connection,
    home: PathBuf,
    generation: Uuid,
    /// Ting registration may still have failed; the Silicon stays connected either way.
    ting: Result<()>,
}

/// One restore attempt for the first saved Silicon that is due, holding `mutation` for that
/// one Silicon only. False when none was due.
fn restore_next(
    app: &App,
    now: DateTime<Utc>,
    attempt: impl FnOnce(&App, &Connection) -> Result<Restored>,
) -> bool {
    let Some(id) = app.registry.lock().recover().due_restore(now) else {
        return false;
    };
    // Someone is waiting for `mutation`: pause, so they get it before the next attempt.
    if app.waiting.load(Ordering::SeqCst) > 0 {
        return false;
    }
    let _guard = app.mutation.lock().recover();
    if app.runtime.stopping.load(Ordering::SeqCst) {
        return false;
    }
    // A connect or disconnect may have settled it while this waited for `mutation`, which
    // can take a while: the next attempt is timed from now.
    let now = now.max(Utc::now());
    let due = {
        let mut registry = app.registry.lock().recover();
        registry.entry_mut(&id).filter(|e| e.due(now)).map(|entry| {
            entry.state = Restore::Restoring;
            (entry.connection.clone(), entry.failures + 1)
        })
    };
    let Some((saved, tries)) = due else {
        return true;
    };
    let outcome = contained("restoring a Silicon", || attempt(app, &saved));
    // The next attempt is timed from when this one ended: one that runs longer than its
    // backoff must not be due again at once and keep every other Silicon waiting.
    let now = now.max(Utc::now());
    match outcome {
        Ok(restored) => {
            let current = restored.connection.id.clone();
            app.registry
                .lock()
                .recover()
                .connected(Some(&id), restored.connection, now);
            if tries > 1 {
                let message = format!("restored {current} on attempt {tries}");
                crate::stderr_line(&message);
                if let Err(error) = crate::log_line_scoped(
                    &restored.home,
                    Some(restored.generation),
                    "runtime",
                    "interpreter",
                    &message,
                ) {
                    crate::stderr_line(&format!("{error:#}"));
                }
            }
            // The supervisor retries a write that fails.
            if let Err(error) = persist(app) {
                crate::stderr_line(&format!("{error:#}"));
            }
            if let Err(error) = restored.ting {
                ting_failure(
                    app,
                    &current,
                    &restored.home,
                    Some(restored.generation),
                    error.context(format!("restore Ting for {current}")),
                    now,
                );
            }
        }
        Err(error) => restore_failure(app, &id, &saved, tries, error, now),
    }
    true
}

/// compile, connect, route, start the inbox, register with Ting. A Ting failure does not undo
/// the rest: the Silicon's inbox, sends and heartbeats work without it, and it is retried.
fn attempt_restore(app: &App, saved: &Connection) -> Result<Restored> {
    let cfg = compile(&saved.yaml)?;
    let connection = descriptor(&cfg);
    // An earlier attempt that was cut short (a panic) may have connected it already.
    let connected = match app.runtime.get(&connection.id) {
        Ok(live) if live.cfg.path == connection.yaml => live,
        _ => {
            let warnings = cfg.warnings.clone();
            app.runtime.connect(cfg)?;
            let live = app.runtime.get(&connection.id)?;
            // What an interactive connect would have shown, for the Silicon's own log.
            for warning in warnings {
                if let Err(error) = crate::log_line_scoped(
                    &live.cfg.home,
                    Some(live.cfg.generation),
                    "runtime",
                    "interpreter",
                    &format!("configuration warning: {warning}"),
                ) {
                    crate::stderr_line(&format!("{error:#}"));
                }
            }
            live
        }
    };
    if let Some(warning) = update_proxy(app) {
        crate::stderr_line(&format!("restore {}: {warning}", connection.id));
    }
    app.runtime.start_inbox(&connected);
    // Stopping: the shutdown removes hooks, so there is nothing to register.
    let ting = register_unless_stopping(app, &connection.id, || register_quietly(&connected))
        .unwrap_or(Ok(()));
    Ok(Restored {
        connection,
        home: connected.cfg.home.clone(),
        generation: connected.cfg.generation,
        ting,
    })
}

fn restore_failure(
    app: &App,
    id: &str,
    saved: &Connection,
    tries: u32,
    error: anyhow::Error,
    now: DateTime<Utc>,
) {
    let error = failure::mask(&saved.home, &format!("{error:#}"), &[]);
    let Some(failed) = app.registry.lock().recover().failed(id, error.clone(), now) else {
        return;
    };
    let when = failed.next_at.to_rfc3339();
    if failed.changed {
        report(
            &saved.home,
            None,
            "interpreter",
            &format!(
                "restore {id} from {} failed (attempt {tries}); the next attempt runs at {when}: {error}",
                saved.yaml.display()
            ),
        );
    } else {
        crate::stderr_line(&format!(
            "restore {id} failed again (attempt {}, the same error as before); the next attempt runs at {when}",
            failed.attempt
        ));
    }
}

/// Re-register the first connected Silicon whose Ting registration is due: a retry after a
/// failure, or the six-hourly re-assertion. Runs without `mutation`; the Silicon's Ting lock
/// keeps a disconnect or shutdown from removing the hook while it runs.
fn reassert_next(
    app: &App,
    now: DateTime<Utc>,
    register: impl FnOnce(&Connected) -> Result<()>,
) -> bool {
    let Some(id) = app.registry.lock().recover().due_ting(now) else {
        return false;
    };
    let lock = ting_lock(app, &id);
    let _held = lock.lock().recover();
    if app.runtime.stopping.load(Ordering::SeqCst) {
        return false;
    }
    let now = now.max(Utc::now());
    let Ok(connected) = app.runtime.get(&id) else {
        // Not live right now (a disconnect is finishing); look again later.
        if let Some(entry) = app.registry.lock().recover().entry_mut(&id) {
            entry.ting.next_at = Some(later(now, TING_REASSERT));
        }
        return true;
    };
    let still_due = app.registry.lock().recover().entry(&id).is_some_and(|e| {
        e.state == Restore::Connected
            && e.ting
                .next_at
                .is_some_and(|at| arrived(at, now, TING_REASSERT))
    });
    if !still_due {
        return true;
    }
    let outcome = contained("registering a Ting webhook", || register(&connected));
    // Timed from when this attempt ended, as for restores.
    let now = now.max(Utc::now());
    match outcome {
        Ok(()) => {
            let failures = app.registry.lock().recover().ting_ok(&id, now);
            if failures.is_some_and(|failures| failures > 0) {
                let message = format!(
                    "Ting webhook registration for {id} succeeded after {} failed attempts",
                    failures.unwrap_or_default()
                );
                crate::stderr_line(&message);
                if let Err(error) = crate::log_line_scoped(
                    &connected.cfg.home,
                    Some(connected.cfg.generation),
                    "runtime",
                    "ting",
                    &message,
                ) {
                    crate::stderr_line(&format!("{error:#}"));
                }
            }
        }
        Err(error) => ting_failure(
            app,
            &id,
            &connected.cfg.home,
            Some(connected.cfg.generation),
            error,
            now,
        ),
    }
    true
}

fn ting_failure(
    app: &App,
    id: &str,
    home: &Path,
    generation: Option<Uuid>,
    error: anyhow::Error,
    now: DateTime<Utc>,
) {
    let error = failure::mask(home, &format!("{error:#}"), &[]);
    let Some(failed) = app
        .registry
        .lock()
        .recover()
        .ting_failed(id, error.clone(), now)
    else {
        return;
    };
    let when = failed.next_at.to_rfc3339();
    if failed.changed {
        report(
            home,
            generation,
            "ting",
            &format!(
                "Ting webhook registration for {id} failed (attempt {}); {id} stays connected and registration is retried at {when}: {error}",
                failed.attempt
            ),
        );
    } else {
        crate::stderr_line(&format!(
            "Ting webhook registration for {id} failed again (attempt {}, the same error as before); retrying at {when}",
            failed.attempt
        ));
    }
}

/// Run `register` (a Ting webhook registration for `id`) under that Silicon's Ting lock, unless
/// the interpreter is stopping, which is checked only once the lock is held. The orderly stop
/// sets `stopping` and then waits on every Ting lock before it removes hooks, so a
/// registration either ends before that removal or never starts. None: it is stopping.
fn register_unless_stopping(
    app: &App,
    id: &str,
    register: impl FnOnce() -> Result<()>,
) -> Option<Result<()>> {
    let lock = ting_lock(app, id);
    let _held = lock.lock().recover();
    if app.runtime.stopping.load(Ordering::SeqCst) {
        return None;
    }
    Some(register())
}

fn ting_lock(app: &App, id: &str) -> Arc<Mutex<()>> {
    app.ting_locks
        .lock()
        .recover()
        .entry(id.to_owned())
        .or_default()
        .clone()
}

/// Every host Caddy should route: saved Silicons (so one still being restored answers 503 and
/// Ting retries, instead of Caddy's 404) and connected ones.
fn current_hosts(app: &App) -> Vec<String> {
    let mut hosts: Vec<String> = app
        .registry
        .lock()
        .recover()
        .entries
        .iter()
        .map(|entry| entry.connection.host.clone())
        .filter(|host| routable(host))
        .collect();
    for connected in app.runtime.silicons.read().recover().values() {
        let host = descriptor(&connected.cfg).host;
        if !hosts.contains(&host) {
            hosts.push(host);
        }
    }
    hosts
}

/// Keep the owned Caddy running: when it is up, apply the current hosts (which also checks it
/// is alive); a failure stops it and the next round starts a new one. When it is down, start
/// it once it is due, backing off from 1 s to 5 minutes.
fn supervise_proxy(
    app: &App,
    now: DateTime<Utc>,
    start: impl FnOnce(&[String]) -> Result<Box<dyn Router>>,
) {
    let due = {
        let routing = app.routing.lock().recover();
        if routing.disabled {
            return;
        }
        routing
            .next_start
            .is_none_or(|at| arrived(at, now, Duration::from_secs(300)))
    };
    let hosts = current_hosts(app);
    let mut slot = app.proxy.lock().recover();
    if let Some(proxy) = slot.as_mut() {
        match proxy.update(&hosts) {
            Ok(()) => {
                drop(slot);
                routing_up(app, now);
            }
            Err(error) => {
                let failed = slot.take();
                drop(slot);
                retire(app, failed);
                routing_failed(
                    app,
                    &format!(
                        "the local routing proxy failed its health check and was stopped; it is started again: {error:#}"
                    ),
                    now,
                );
            }
        }
        return;
    }
    drop(slot);
    if !due {
        return;
    }
    // Held across the start, the store and stopping one not kept (see `App::handover`).
    let _handover = app.handover.lock().recover();
    if app.runtime.stopping.load(Ordering::SeqCst) {
        return;
    }
    match contained("starting the local routing proxy", || start(&hosts)) {
        Ok(proxy) => {
            let spare = adopt(&app.proxy, &app.runtime.stopping, proxy);
            let kept = spare.is_none();
            // Dropping the one not kept stops its Caddy; not while holding the lock.
            drop(spare);
            if kept {
                routing_up(app, now);
            }
        }
        Err(error) => routing_failed(
            app,
            &format!(
                "could not start the local routing proxy, so Silicon hosts do not answer on port 80; it is retried (`silicon serve --no-proxy` runs without it): {error:#}"
            ),
            now,
        ),
    }
}

/// Stop a Caddy taken out of `app.proxy`: outside that lock (it takes seconds), but under
/// `handover`, so the orderly stop does not finish before this Caddy is gone.
fn retire(app: &App, proxy: Option<Box<dyn Router>>) {
    if proxy.is_some() {
        let _handover = app.handover.lock().recover();
        drop(proxy);
    }
}

/// Keep a proxy started outside `slot`, unless the interpreter began stopping meanwhile or one
/// is already there. The one not kept is handed back, to be dropped (which stops its Caddy).
fn adopt<P>(slot: &Mutex<Option<P>>, stopping: &AtomicBool, proxy: P) -> Option<P> {
    let mut slot = slot.lock().recover();
    // Checked under the lock the orderly stop takes the proxy under: either this sees the
    // stop, or the stop finds the proxy stored here.
    if stopping.load(Ordering::SeqCst) || slot.is_some() {
        return Some(proxy);
    }
    *slot = Some(proxy);
    None
}

/// Routing answered. Restarts keep backing off until it has stayed up for ten minutes, so a
/// Caddy that dies right after every start is not restarted every few seconds forever.
fn routing_up(app: &App, now: DateTime<Utc>) {
    let recovered = {
        let mut routing = app.routing.lock().recover();
        if routing.up {
            if routing
                .up_since
                .is_some_and(|since| now - since >= TimeDelta::minutes(10) || since > now)
            {
                routing.failures = 0;
            }
            return;
        }
        let recovered = routing.error.is_some();
        routing.up = true;
        routing.up_since = Some(now);
        routing.error = None;
        routing.next_start = None;
        routing.reported = None;
        recovered
    };
    if recovered {
        crate::stderr_line("local routing is up again");
    }
}

/// Routing is down with `error`: start it again after a backoff from 1 s to 5 minutes.
fn routing_failed(app: &App, error: &str, now: DateTime<Utc>) {
    let changed = {
        let mut routing = app.routing.lock().recover();
        routing.up = false;
        routing.up_since = None;
        routing.error = Some(error.to_owned());
        routing.failures += 1;
        routing.next_start = Some(later(
            now,
            backoff(
                routing.failures,
                Duration::from_secs(1),
                Duration::from_secs(300),
            ),
        ));
        // Each start names a fresh admin socket directory and process: the same failure
        // must still read as a repeat.
        let seen = recurring(error);
        let changed = routing.reported.as_deref() != Some(seen.as_str());
        routing.reported = Some(seen);
        changed
    };
    if changed {
        report_all(app, "proxy", error);
    }
}

/// `error` without the parts that differ on every attempt (random directory names, process
/// ids), for telling a new failure from a repeated one. The report itself stays whole.
fn recurring(error: &str) -> String {
    static VARYING: OnceLock<regex::Regex> = OnceLock::new();
    VARYING
        .get_or_init(|| {
            regex::Regex::new(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\b(?:pid|process|Caddy process) \d+",
            )
            .unwrap()
        })
        .replace_all(error, "<varies>")
        .into_owned()
}

/// Apply the current hosts to the owned Caddy. Routing that is down never fails the caller:
/// the supervisor restarts Caddy with the hosts it finds then, so the answer is a warning
/// that says so, with the reason.
fn update_proxy(app: &App) -> Option<String> {
    if app.routing.lock().recover().disabled {
        return None;
    }
    let hosts = current_hosts(app);
    let mut slot = app.proxy.lock().recover();
    let Some(proxy) = slot.as_mut() else {
        drop(slot);
        let routing = app.routing.lock().recover();
        return Some(format!(
            "local routing is down and is restored automatically; last error: {}",
            routing.error.as_deref().unwrap_or("it has not started yet")
        ));
    };
    let error = match proxy.update(&hosts) {
        Ok(()) => return None,
        Err(error) => format!("{error:#}"),
    };
    let failed = slot.take();
    drop(slot);
    retire(app, failed);
    routing_failed(
        app,
        &format!("the local routing proxy failed and was stopped; it is started again: {error}"),
        Utc::now(),
    );
    Some(format!(
        "local routing is down and is restored automatically; last error: {error}"
    ))
}

/// The executable an update restart would run, resolved through its symlinks: what
/// `<prefix>/bin/silicon` points at for a managed installation, else this process's own
/// executable (the `restart` action on a source build).
fn restart_target(app: &App) -> Result<PathBuf> {
    resolve_target(crate::update::managed_prefix(), app.restart.current.clone())
}

fn resolve_target(prefix: Result<PathBuf>, current: Option<PathBuf>) -> Result<PathBuf> {
    let executable = match prefix {
        Ok(prefix) => prefix.join("bin/silicon"),
        Err(managed) => {
            return current.ok_or_else(|| {
                managed.context("no executable to restart into: this is not a managed installation and the running executable could not be located")
            })
        }
    };
    executable
        .canonicalize()
        .with_context(|| format!("resolve {}", executable.display()))
}

/// Whether `executable` starts at all: `--version` within a minute.
fn preflight(dir: &Path, executable: &Path) -> Result<()> {
    let shown = format!("{} --version", executable.display());
    let output = crate::process::output_within(
        Command::new(executable).arg("--version"),
        Duration::from_secs(60),
    )
    .map_err(|error| failure::spawn(dir, &shown, &error))?;
    if !output.status.success() {
        return Err(failure::command(dir, &shown, &output, &[]));
    }
    Ok(())
}

/// The update restart's bookkeeping: check the new release before anything stops (a failed
/// check keeps this release running and is repeated hourly; a release installed after the
/// check is checked before it can run), and while the restart waits for idle, say so once a
/// day. `resolve` names the executable a restart would run, `check` runs its check.
fn restart_tick(
    app: &App,
    now: DateTime<Utc>,
    resolve: impl FnOnce() -> Result<PathBuf>,
    check: impl FnOnce(&Path) -> Result<()>,
) {
    let requested = app.restart.requested.load(Ordering::SeqCst);
    if !requested && app.restart.check.lock().recover().retry_at.is_none() {
        return;
    }
    let target = resolve();
    let due = {
        let mut state = app.restart.check.lock().recover();
        // Another release was installed since the last check (a second `silicon update`):
        // the one that passed is not what would run now.
        if let (Ok(target), Some(checked)) = (&target, &state.checked) {
            if target != checked {
                state.passed = None;
                state.retry_at = None;
            }
        }
        state.passed.is_none()
            && state
                .retry_at
                .is_none_or(|at| arrived(at, now, Duration::from_secs(3600)))
    };
    if due {
        let checked = target.as_ref().ok().cloned();
        match target.and_then(|executable| check(&executable).map(|()| executable)) {
            Ok(executable) => {
                *app.restart.check.lock().recover() = Check {
                    passed: Some(executable.clone()),
                    checked: Some(executable.clone()),
                    since: Some(now),
                    noticed: Some(now),
                    ..Check::default()
                };
                app.restart.requested.store(true, Ordering::SeqCst);
                crate::stderr_line(&format!(
                    "{} passed its check; the interpreter restarts into it as soon as no work is running",
                    executable.display()
                ));
            }
            Err(error) => {
                app.restart.requested.store(false, Ordering::SeqCst);
                let error = format!("{error:#}");
                let changed = {
                    let mut state = app.restart.check.lock().recover();
                    state.passed = None;
                    state.checked = checked;
                    state.retry_at = Some(later(now, Duration::from_secs(3600)));
                    let changed = state.reported.as_deref() != Some(error.as_str());
                    state.reported = Some(error.clone());
                    changed
                };
                if changed {
                    report_all(
                        app,
                        "interpreter",
                        &format!(
                            "an installed update was not applied: the new release failed its check, so the interpreter keeps running {} and checks again every hour: {error}",
                            env!("CARGO_PKG_VERSION")
                        ),
                    );
                }
            }
        }
        return;
    }
    if !requested {
        return;
    }
    let since = {
        let mut state = app.restart.check.lock().recover();
        match (state.passed.is_some(), state.noticed) {
            (true, Some(at)) if now - at >= TimeDelta::hours(24) || at > now => {
                state.noticed = Some(now);
                state.since
            }
            _ => None,
        }
    };
    if let Some(since) = since {
        report_all(
            app,
            "interpreter",
            &format!(
                "an installed update has been waiting since {} for the interpreter to be idle before it restarts into it: {}",
                since.to_rfc3339(),
                waiting_reason(app)
            ),
        );
    }
}

/// Why an update restart is still waiting.
// ORCHESTRATOR: once Runtime exposes activity and pending-dispatch counts, name them here.
fn waiting_reason(app: &App) -> String {
    let mut reasons = Vec::new();
    if matches!(app.mutation.try_lock(), Err(TryLockError::WouldBlock)) {
        reasons.push("a connect, disconnect, restore or app change is running");
    }
    if !app.runtime.idle() {
        reasons.push("requests, Ting flows or session work are still running");
    }
    if reasons.is_empty() {
        "it was busy at the last check".into()
    } else {
        reasons.join("; ")
    }
}

fn configuration(cfg: &Config) -> Result<Value> {
    let mut value = serde_json::to_value(cfg).context("serialize the configuration")?;
    let secrets = crate::telemetry::silicon_secrets(&value["silicon"]);
    if let Some(configs) = value
        .pointer_mut("/silicon/app_configs")
        .and_then(Value::as_object_mut)
    {
        for config in configs.values_mut() {
            *config = json!("[redacted]");
        }
    }
    crate::telemetry::redact_value(&mut value, &secrets);
    Ok(value)
}

fn header<'a>(req: &'a Request, key: &str) -> &'a str {
    req.headers()
        .iter()
        .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case(key))
        .map(|h| h.value.as_str())
        .unwrap_or("")
}

/// Whether request bodies go to interpreter telemetry, checked before a body is copied for it.
// ORCHESTRATOR: replace with a cached `crate::telemetry` predicate when it offers one; this
// mirrors its policy (tests, SILICON_TELEMETRY=0, the telemetry setting).
fn telemetry_wanted() -> bool {
    if cfg!(test) || std::env::var("SILICON_TELEMETRY").as_deref() == Ok("0") {
        return false;
    }
    crate::settings::load().is_ok_and(|settings| settings.telemetry)
}

fn handle(app: Arc<App>, mut request: Request) {
    if app.runtime.stopping.load(Ordering::SeqCst) {
        respond(
            request,
            503,
            json!({"error":"interpreter is stopping; retry after it restarts"}),
        );
        return;
    }
    let path = request.url().split('?').next().unwrap_or("").to_owned();
    let host = header(&request, "Host")
        .split(':')
        .next()
        .unwrap_or("")
        .to_owned();
    if request.method() == &Method::Get && path == "/ping" {
        let (status, value) = ping(&app, &host);
        respond(request, status, value);
        return;
    }
    if request.method() == &Method::Get
        && path == "/"
        && (host == "silicon.localhost" || host == "127.0.0.1")
    {
        send(
            request,
            Response::from_string(include_str!("dashboard.html")).with_header(
                Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap(),
            ),
        );
        return;
    }
    if request.method() != &Method::Post {
        let error = format!("{} {path} is not supported; use POST", request.method());
        respond(request, 405, json!({"error":error}));
        return;
    }
    let content_type = header(&request, "Content-Type");
    if !content_type
        .split(';')
        .next()
        .is_some_and(|v| v.trim() == "application/json")
    {
        let error = format!("Content-Type must be application/json, got {content_type:?}");
        respond(request, 415, json!({"error":error}));
        return;
    }
    let origin = header(&request, "Origin");
    if !origin.is_empty() && origin != format!("http://{host}") && origin != app.runtime.url {
        let error = format!(
            "cross-origin requests are not permitted: Origin {origin} is neither http://{host} nor {}",
            app.runtime.url
        );
        respond(request, 403, json!({"error":error}));
        return;
    }
    let auth = header(&request, "Authorization")
        .strip_prefix("Bearer ")
        .unwrap_or("")
        .to_owned();
    let caller = if path == "/si" {
        match app.runtime.caller(&auth) {
            Some(caller) => Some(caller),
            None => {
                let error = if auth.is_empty() {
                    "ISI capability required: send Authorization: Bearer $SI_TOKEN"
                } else {
                    "invalid ISI capability: this interpreter did not issue that SI_TOKEN, or its session has ended"
                };
                respond(request, 401, json!({"error":error}));
                return;
            }
        }
    } else {
        None
    };
    if path == "/control" && auth != app.daemon.token {
        let file = app.dir.join("daemon.json");
        let error = if auth.is_empty() {
            format!(
                "interpreter token required: send Authorization: Bearer <token from {}>",
                file.display()
            )
        } else {
            format!(
                "interpreter token rejected: it is not the token in {}; the interpreter may have restarted since it was read",
                file.display()
            )
        };
        respond(request, 401, json!({"error":error}));
        return;
    }
    let event_request = path == "/" || path == "/events";
    let limit = if event_request {
        1024 * 1024
    } else {
        16 * 1024 * 1024
    };
    let mut body = Vec::new();
    if let Err(error) = request.as_reader().take(limit + 1).read_to_end(&mut body) {
        let error = format!("could not read the request body for {path}: {error}");
        respond(request, 400, json!({"error":error}));
        return;
    }
    if body.len() as u64 > limit {
        let error = format!("request body for {path} exceeds its {limit}-byte limit");
        respond(request, 413, json!({"error":error}));
        return;
    }
    let body: Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(error) => {
            respond(
                request,
                400,
                json!({"error":format!("invalid JSON request body for {path}: {error}")}),
            );
            return;
        }
    };
    // Taken only now: a client that stalls mid-body must not hold off an update restart.
    let _activity = match app.runtime.activity() {
        Ok(activity) => activity,
        Err(error) => {
            respond(
                request,
                503,
                json!({"error":format!("{error:#}; retry after it restarts")}),
            );
            return;
        }
    };
    if (path == "/control" || path == "/si") && telemetry_wanted() {
        crate::telemetry::interpreter("daemon", "request", json!({"path":path,"body":body}));
    }
    let result = if path == "/control" {
        guarded(|| control(&app, &body))
    } else if let Some(caller) = caller {
        guarded(|| internal_action(&app, &caller, &body))
    } else if event_request {
        match addressed(&app, &host) {
            Addressed::Live(id) => guarded(|| deliver(&app, &id, body)),
            Addressed::Saved(why) => {
                // Ting gets the whole restore error; daemon.log a short line per attempt.
                if let Some(note) = refusal_note(&app, &host) {
                    crate::stderr_line(&note);
                }
                reply(request, 503, json!({"error":why}));
                return;
            }
            Addressed::Unknown => {
                let error = format!("no connected silicon serves host {host:?}");
                respond(request, 404, json!({"error":error}));
                return;
            }
        }
    } else {
        let error = format!("unknown endpoint {path}; use /control, /si or /events");
        respond(request, 404, json!({"error":error}));
        return;
    };
    match result {
        Ok(_) if event_request => send(request, Response::empty(204)),
        Ok(value) => respond(request, 200, value),
        // Errors are masked where they are built; this last pass covers every Silicon's
        // registered values, because the answer leaves the process.
        Err(error) => {
            let status = if error
                .downcast_ref::<Rejected>()
                .is_some_and(|rejected| rejected.retry)
            {
                503
            } else {
                400
            };
            respond(
                request,
                status,
                json!({"error":failure::mask_all(&format!("{error:#}"))}),
            );
        }
    }
}

/// Who a request's Host names.
enum Addressed {
    Live(String),
    /// A saved Silicon that is not connected yet: why.
    Saved(String),
    Unknown,
}

fn addressed(app: &App, host: &str) -> Addressed {
    if let Some(id) = app
        .runtime
        .silicons
        .read()
        .recover()
        .iter()
        .find(|(_, c)| descriptor(&c.cfg).host == host)
        .map(|(id, _)| id.clone())
    {
        return Addressed::Live(id);
    }
    let registry = app.registry.lock().recover();
    match registry
        .entries
        .iter()
        .find(|entry| entry.connection.host == host)
    {
        Some(entry) => Addressed::Saved(registry.unavailable(&entry.connection.id)),
        None => Addressed::Unknown,
    }
}

/// The daemon.log line for Ting deliveries refused because the Silicon at `host` is not
/// restored yet: once per restore attempt, and without the restore error, which the restorer
/// reports whole whenever it changes. Ting retries every 30 seconds; repeating that error
/// (often kilobytes of command output) on each retry would bury everything else. None: this
/// attempt was already mentioned.
fn refusal_note(app: &App, host: &str) -> Option<String> {
    let mut registry = app.registry.lock().recover();
    let entry = registry
        .entries
        .iter_mut()
        .find(|entry| entry.connection.host == host)?;
    if entry.refused == Some(entry.failures) {
        return None;
    }
    entry.refused = Some(entry.failures);
    let id = &entry.connection.id;
    Some(match &entry.state {
        Restore::Waiting { next_at, .. } => format!(
            "answering Ting deliveries for {host} with HTTP 503 until {id} is restored: restore attempt {} failed (its error is reported in full, here and in its silicon.log, whenever it changes) and the next runs at {}",
            entry.failures,
            next_at.to_rfc3339()
        ),
        _ => format!(
            "answering Ting deliveries for {host} with HTTP 503 while {id} is being restored"
        ),
    })
}

/// Health for a Silicon host; when it is offline, `error` says why.
fn ping(app: &App, host: &str) -> (u16, Value) {
    let silicons = app.runtime.silicons.read().recover();
    let found = silicons.values().find(|c| descriptor(&c.cfg).host == host);
    let online = found
        .filter(|c| c.enabled.load(Ordering::SeqCst))
        .map(|c| descriptor(&c.cfg).id);
    let mut value = json!({"online":online.is_some(),"silicon":online,"timestamp":chrono::Utc::now().to_rfc3339()});
    if online.is_some() {
        return (200, value);
    }
    if let Some(c) = found {
        value["error"] = json!(format!("silicon {} is disconnected", descriptor(&c.cfg).id));
        return (404, value);
    }
    drop(silicons);
    if let Addressed::Saved(why) = addressed(app, host) {
        value["error"] = json!(why);
        return (503, value);
    }
    value["error"] = json!(format!("no connected silicon serves host {host:?}"));
    (404, value)
}

/// Why a Ting delivery was refused, and whether that is temporary (503: Ting retries it later)
/// or a problem with the batch itself (400).
#[derive(Debug)]
struct Rejected {
    reason: String,
    retry: bool,
}

impl std::fmt::Display for Rejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.reason)
    }
}

impl std::error::Error for Rejected {}

/// Errors that mean "try again later" rather than "this request is wrong".
fn retryable(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        if cause.is::<crate::ting::Unavailable>() {
            return true;
        }
        let text = cause.to_string();
        text.starts_with("interpreter is stopping")
            || text.starts_with("silicon is not connected:")
            || text == "silicon disconnected"
    })
}

/// Queue a Ting batch. Ting alone hears the reply, so a rejection also goes to the Silicon's log
/// (and, through `respond`, to daemon.log).
fn deliver(app: &App, id: &str, body: Value) -> Result<Value> {
    let connected = app.runtime.get(id).map_err(|error| Rejected {
        retry: retryable(&error),
        reason: format!("{error:#}"),
    })?;
    let home = &connected.cfg.home;
    // Ting retries a full inbox; the same refusal is written once, not on every retry.
    static REFUSED: Mutex<std::collections::BTreeMap<String, String>> =
        Mutex::new(std::collections::BTreeMap::new());
    connected
        .ting
        .accept(body)
        .map(|()| {
            REFUSED.lock().recover().remove(id);
            Value::Null
        })
        .map_err(|error| {
            let retry = retryable(&error);
            let reason = failure::mask(home, &format!("{error:#}"), &[]);
            let message = format!("rejected a Ting delivery: {reason}");
            let repeated = retry
                && REFUSED
                    .lock()
                    .recover()
                    .insert(id.to_owned(), reason.clone())
                    .as_ref()
                    == Some(&reason);
            if !repeated {
                log_error(home, Some(connected.cfg.generation), "webhook", &message);
            }
            anyhow::Error::new(Rejected { reason, retry })
        })
}

/// A panic answers the request with its message instead of dropping the connection.
fn guarded(action: impl FnOnce() -> Result<Value>) -> Result<Value> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(action)).unwrap_or_else(|panic| {
        Err(anyhow!(
            "interpreter panicked: {} (where: {})",
            crate::failure::panic_message(&*panic),
            directory().join("daemon.log").display()
        ))
    })
}

/// A failure no caller is waiting on: daemon.log for the operator, silicon.log for the Silicon.
fn report(home: &Path, generation: Option<Uuid>, origin: &str, message: &str) {
    let message = failure::mask(home, message, &[]);
    crate::stderr_line(&message);
    log_error(home, generation, origin, &message);
}

/// A failure that concerns every saved Silicon (routing, updates): daemon.log once, and an
/// `[error]` line in each Silicon's log.
fn report_all(app: &App, origin: &str, message: &str) {
    crate::stderr_line(&failure::mask_all(message));
    let mut homes: Vec<PathBuf> = app
        .registry
        .lock()
        .recover()
        .entries
        .iter()
        .map(|entry| entry.connection.home.clone())
        .collect();
    homes.sort();
    homes.dedup();
    for home in homes {
        log_error(&home, None, origin, &failure::mask(&home, message, &[]));
    }
}

/// An `[error]` line in the Silicon's log. A home whose directory is gone is not recreated
/// just to hold it; a failed write lands in daemon.log with the line it could not write.
fn log_error(home: &Path, generation: Option<Uuid>, origin: &str, message: &str) {
    let gone = fs::metadata(home.join(".silicon"))
        .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound);
    if gone {
        return;
    }
    if let Err(error) = crate::log_line_scoped(home, generation, "error", origin, message) {
        crate::stderr_line(&format!(
            "{error:#}; the {origin} error it was recording:\n{message}"
        ));
    }
}

/// Undo a half-made connection; failures here ride along with the error that caused it.
fn rollback(app: &App, id: &str, error: anyhow::Error) -> anyhow::Error {
    let error = crate::failure::also(
        error,
        app.runtime
            .disconnect(id)
            .with_context(|| format!("roll back: disconnect {id}")),
    );
    match update_proxy(app) {
        None => error,
        Some(warning) => anyhow!("{error:#}\nalso: {warning}"),
    }
}

fn respond(request: Request, status: u16, value: Value) {
    let path = request.url().split('?').next().unwrap_or("");
    // Ting alone hears a rejected delivery; daemon.log keeps why that Silicon's events went missing.
    if status >= 400 && (path == "/" || path == "/events") {
        let host = header(&request, "Host").to_owned();
        let said = value["error"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| value.to_string());
        // Ting retries a refused delivery; while the refusal is unchanged it is written once.
        static REFUSED: Mutex<Option<HashMap<String, String>>> = Mutex::new(None);
        let repeated = status == 503 && {
            let mut refused = REFUSED.lock().recover();
            let refused = refused.get_or_insert_with(HashMap::new);
            refused.insert(host.clone(), said.clone()).as_ref() == Some(&said)
        };
        if !repeated {
            crate::stderr_line(&format!(
                "answered {} {host}{path} with HTTP {status}: {said}",
                request.method(),
            ));
        }
    }
    reply(request, status, value);
}

/// `respond` without its daemon.log line, for a caller that writes its own.
fn reply(request: Request, status: u16, value: Value) {
    let mut response = Response::from_string(value.to_string())
        .with_status_code(status)
        .with_header(Header::from_bytes("Content-Type", "application/json").unwrap());
    if status == 503 {
        response = response.with_header(Header::from_bytes("Retry-After", "30").unwrap());
    }
    send(request, response);
}

/// A client that left before its answer is not an error to it, but daemon.log says what it missed.
fn send<R: Read>(request: Request, response: Response<R>) {
    let path = request.url().split('?').next().unwrap_or("").to_owned();
    let status = response.status_code().0;
    if let Err(error) = request.respond(response) {
        crate::stderr_line(&format!(
            "could not send the HTTP {status} answer for {path}: {error}"
        ));
    }
}

fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    match v.get(key) {
        Some(Value::String(value)) => Ok(value),
        None | Some(Value::Null) => bail!("{key} is required"),
        Some(other) => bail!("{key} must be a string, got {other}"),
    }
}

/// An optional string argument; a value of the wrong type is an error, never "absent".
fn optional<'a>(v: &'a Value, key: &str) -> Result<Option<&'a str>> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => text(v, key).map(Some),
    }
}

/// An optional boolean argument, false when absent.
fn flag(v: &Value, key: &str) -> Result<bool> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(false),
        Some(Value::Bool(value)) => Ok(*value),
        Some(other) => bail!("{key} must be boolean, got {other}"),
    }
}

fn control(app: &Arc<App>, body: &Value) -> Result<Value> {
    let args = &body["args"];
    let action = text(body, "action")?;
    match action {
        "settings" => Ok(json!(crate::settings::load()?)),
        "settings-set" => {
            // Not `mutation`: a settings change must not wait behind a restore or a connect.
            let _guard = SETTINGS.lock().recover();
            Ok(json!(crate::settings::set(
                text(args, "key")?,
                args["enabled"]
                    .as_bool()
                    .ok_or_else(|| anyhow!("enabled must be boolean, got {}", args["enabled"]))?
            )?))
        }
        "silicon-ping" => {
            let id = text(args, "silicon")?;
            let online = app.runtime.get(id);
            let mut value = json!({"silicon":id,"online":online.is_ok(),"timestamp":chrono::Utc::now().to_rfc3339()});
            if let Err(error) = online {
                let registry = app.registry.lock().recover();
                value["error"] = json!(match registry.entry(id) {
                    Some(_) => registry.unavailable(id),
                    None => format!("{error:#}"),
                });
            }
            Ok(value)
        }
        "configuration" => {
            let connected = app.runtime.get(text(args, "silicon")?)?;
            let mut cfg = connected.cfg.clone();
            cfg.silicon = connected.app_settings.read().recover().clone();
            cfg.flow = cfg.load_flow()?;
            configuration(&cfg)
        }
        "install" => crate::apps::install(text(args, "app_id")?),
        "uninstall" => crate::apps::uninstall(text(args, "app_id")?),
        "ping" => {
            let (restoring, waiting) = app.registry.lock().recover().counts();
            let proxy = app.routing.lock().recover().state();
            Ok(json!({
                "version":env!("CARGO_PKG_VERSION"),
                "pid":app.daemon.pid,
                "restoring":restoring,
                "waiting":waiting,
                "proxy":proxy
            }))
        }
        "compile" => {
            let cfg = compile(text(args, "yaml")?)?;
            Ok(json!({"valid":true,"connection":descriptor(&cfg),"warnings":cfg.warnings}))
        }
        "list" => Ok(json!(app.registry.lock().recover().listed())),
        "connect" => connect(app, text(args, "yaml")?),
        "disconnect" => disconnect(app, text(args, "target")?),
        "event" => app
            .runtime
            .event(text(args, "silicon")?, args["event"].clone()),
        "sessions" => {
            let connected = app.runtime.get(text(args, "silicon")?)?;
            sessions(&app.runtime, &connected, args)
        }
        "show" => app.runtime.show(
            text(args, "silicon")?,
            text(args, "isi")?,
            optional(args, "id")?,
        ),
        "end" => {
            app.runtime.end(
                text(args, "silicon")?,
                text(args, "isi")?,
                optional(args, "id")?,
            )?;
            Ok(json!({"ended":true}))
        }
        "send" => {
            let options: SendOptions =
                serde_json::from_value(args.clone()).context("invalid send options")?;
            let sent = app.runtime.send(
                text(args, "silicon")?,
                None,
                text(args, "isi")?,
                text(args, "message")?,
                &options,
                false,
            )?;
            Ok(json!({"session":sent.session,"delivery_id":sent.id}))
        }
        "new-session" => {
            let id = text(args, "silicon")?;
            let isi = text(args, "isi")?;
            let connected = app.runtime.get(id)?;
            let current =
                app.runtime
                    .show_connected(&connected, isi, optional(args, "current_id")?)?;
            let session = &current["session"]["session_id"];
            let caller = app.runtime.session_caller(
                &connected,
                serde_json::from_value(session.clone()).with_context(|| {
                    format!("{isi}'s current session has an unexpected session_id {session}")
                })?,
            )?;
            Ok(json!(app.runtime.new_session_connected(
                &connected,
                &caller,
                &serde_json::from_value::<NewSession>(args.clone())
                    .context("invalid new-session arguments")?
            )?))
        }
        "logs" => {
            let connected = app.runtime.get(text(args, "silicon")?)?;
            let path = connected.cfg.home.join(".silicon/silicon.log");
            Ok(json!({"path":path,"lines":tail(&path,100)?}))
        }
        "auth-setup" => {
            let c = app.runtime.get(text(args, "silicon")?)?;
            Ok(
                json!({"app_id":auth::setup_scoped(&c.cfg.home,c.cfg.silicon.id.as_deref().unwrap(),c.cfg.silicon.org_id.as_deref().unwrap(),c.cfg.silicon.token.as_deref().unwrap(),text(args,"app")?,c.cfg.generation)?}),
            )
        }
        "auth-remove" => {
            let c = app.runtime.get(text(args, "silicon")?)?;
            auth::remove(&c.cfg.home, text(args, "app")?)?;
            Ok(json!({"removed":true}))
        }
        "restart" => {
            // Asked for by a person (`silicon update`, which may just have installed another
            // release): whatever was checked before, check what would run now.
            *app.restart.check.lock().recover() = Check::default();
            if args["handover"].as_bool() == Some(true) {
                app.restart.handover.store(true, Ordering::SeqCst);
            }
            app.restart.requested.store(true, Ordering::SeqCst);
            Ok(json!({"restart":"requested","when":"once no work is running"}))
        }
        "shutdown" => {
            SHUTDOWN_REQUESTED.store(true, Ordering::SeqCst);
            app.runtime.stopping.store(true, Ordering::SeqCst);
            Ok(json!({"stopping":true}))
        }
        _ => bail!("unknown control action {action:?}"),
    }
}

/// Connect a YAML while its caller waits. A saved Silicon still waiting for its restore is
/// connected here instead; when this fails too it goes back to waiting, with this error.
fn connect(app: &App, yaml: &str) -> Result<Value> {
    let _guard = interactive(app);
    let cfg = compile(yaml)?;
    let connection = descriptor(&cfg);
    let mut warnings = cfg.warnings.clone();
    let log = cfg.home.join(".silicon/silicon.log");
    let start = fs::metadata(&log).map(|m| m.len()).unwrap_or(0);
    if let Err(error) = app.runtime.connect(cfg) {
        let error = with_progress(error, &log, start);
        requeue(app, &connection, &error);
        return Err(error);
    }
    let result = (|| -> Result<Option<String>> {
        let connected = app.runtime.get(&connection.id)?;
        let routing = crate::progress::step(
            &connected.cfg.home,
            Some(connected.cfg.generation),
            "Configuring local routing",
            "Configured local routing",
            || Ok(update_proxy(app)),
        )?;
        app.runtime.start_inbox(&connected);
        register_unless_stopping(app, &connection.id, || register_ting(&connected))
            .unwrap_or_else(|| {
                Err(anyhow!(
                    "interpreter is stopping; connect again once it runs"
                ))
            })?;
        Ok(routing)
    })();
    match result {
        Ok(routing) => warnings.extend(routing),
        Err(error) => {
            let error = with_progress(error, &log, start);
            let error = rollback(app, &connection.id, error);
            requeue(app, &connection, &error);
            return Err(error);
        }
    }
    app.registry
        .lock()
        .recover()
        .connected(None, connection.clone(), Utc::now());
    if let Err(error) = persist(app) {
        warnings.push(format!(
            "{error:#}; {} is connected, and saving it (so it is restored after a restart) is retried every few seconds",
            connection.id
        ));
    }
    Ok(
        json!({"connection":connection,"warnings":warnings,"progress":connection_progress(&log,start)}),
    )
}

/// A saved Silicon that a connect could not bring up waits for its next restore attempt, with
/// this failure as its latest error; the caller already has it in full.
fn requeue(app: &App, connection: &Connection, error: &anyhow::Error) {
    let mut registry = app.registry.lock().recover();
    let Some(id) = registry
        .entries
        .iter()
        .find(|entry| entry.overlaps(connection) && entry.state != Restore::Connected)
        .map(|entry| entry.connection.id.clone())
    else {
        return;
    };
    let error = failure::mask(&connection.home, &format!("{error:#}"), &[]);
    registry.failed(&id, error, Utc::now());
}

/// Disconnect a Silicon, connected or still waiting for its restore, and forget it.
fn disconnect(app: &App, target: &str) -> Result<Value> {
    let _guard = interactive(app);
    let id = resolve(app, target)?;
    let mut errors = Vec::new();
    if app.runtime.silicons.read().recover().contains_key(&id) {
        let lock = ting_lock(app, &id);
        let _held = lock.lock().recover();
        if let Err(error) = app.runtime.disconnect(&id) {
            errors.push(format!("disconnect: {error:#}"));
        }
    }
    app.registry.lock().recover().remove(&id);
    let warnings: Vec<String> = update_proxy(app).into_iter().collect();
    if let Err(error) = persist(app) {
        // write_json names the file it could not save; the supervisor retries it.
        errors.push(format!("{error:#}"));
    }
    if !errors.is_empty() {
        bail!(
            "disconnected {id} with cleanup errors:\n{}",
            errors.join("\n")
        );
    }
    Ok(json!({"disconnected":id,"warnings":warnings}))
}

/// The Silicon's log lines since `start`; a read failure becomes the last line, never the answer.
fn connection_progress(path: &Path, start: u64) -> Vec<String> {
    match since(path, start) {
        Ok(text) => text.lines().map(str::to_owned).collect(),
        Err(error) => vec![format!("could not read progress: {error:#}")],
    }
}

/// The failure first, then what the Silicon's log recorded while it happened.
fn with_progress(error: anyhow::Error, log: &Path, start: u64) -> anyhow::Error {
    let lines = connection_progress(log, start);
    if lines.is_empty() {
        return error;
    }
    anyhow!("{error:#}\n{}", lines.join("\n"))
}

/// A saved or connected Silicon by ID or YAML path; a miss says what was tried and what is known.
fn resolve(app: &App, target: &str) -> Result<String> {
    let path = Path::new(target);
    let resolved = path.is_absolute().then(|| path.canonicalize());
    let named =
        |id: &str, yaml: &Path| id == target || matches!(&resolved, Some(Ok(path)) if yaml == path);
    let mut known: Vec<String> = Vec::new();
    for entry in &app.registry.lock().recover().entries {
        if named(&entry.connection.id, &entry.connection.yaml) {
            return Ok(entry.connection.id.clone());
        }
        known.push(entry.connection.id.clone());
    }
    for (id, connected) in app.runtime.silicons.read().recover().iter() {
        if named(id, &connected.cfg.path) {
            return Ok(id.clone());
        }
        if !known.contains(id) {
            known.push(id.clone());
        }
    }
    let mut message = format!("unknown silicon: {target}");
    if let Some(Err(error)) = &resolved {
        message.push_str(&format!(" (resolving it as a YAML path failed: {error})"));
    }
    known.sort_unstable();
    message.push_str(&if known.is_empty() {
        "; no Silicons are connected".to_owned()
    } else {
        format!("; connected: {}", known.join(", "))
    });
    bail!("{message}")
}

fn webhook(cfg: &Config) -> String {
    format!("http://{}/events", descriptor(cfg).host)
}

/// Ting registration for an interactive connect, shown as progress.
fn register_ting(connected: &Connected) -> Result<()> {
    crate::progress::step(
        &connected.cfg.home,
        Some(connected.cfg.generation),
        "Registering Ting webhook",
        "Registered Ting webhook",
        || register_quietly(connected),
    )
}

/// Ting registration in the background: failures are reported by the caller, and there is no
/// progress display to feed.
fn register_quietly(connected: &Connected) -> Result<()> {
    connected
        .ting
        .register(&connected.cfg, &webhook(&connected.cfg))
}

fn internal_action(app: &Arc<App>, caller: &crate::runtime::Caller, body: &Value) -> Result<Value> {
    let args = &body["args"];
    let action = text(body, "action")?;
    let connected = app.runtime.caller_connection(caller)?;
    if ["send", "sessions", "show", "end"].contains(&action) {
        app.runtime
            .authorize_target(&connected, caller, text(args, "isi")?)?;
    }
    match action {
        "app-install" | "app-uninstall" => {
            let _guard = interactive(app);
            app.runtime.caller_connection(caller)?;
            let id = text(args, "app_id")?;
            if !crate::apps::valid_id(id) {
                bail!("expected a bare Honeycomb app ID (e.g. dm), got {id:?}");
            }
            let install = action == "app-install";
            if !install && ["iam", "ting"].contains(&id) {
                bail!("cannot uninstall {id}: IAM and Ting are required interpreter dependencies");
            }
            let cfg = &connected.cfg;
            if install {
                crate::apps::install_at(&cfg.home, id)?;
                auth::setup_scoped(
                    &cfg.home,
                    &caller.silicon,
                    cfg.silicon.org_id.as_deref().unwrap(),
                    cfg.silicon.token.as_deref().unwrap(),
                    id,
                    cfg.generation,
                )?;
            }
            let updated = if install {
                crate::config::set_app(&cfg.path, id, install)?
            } else {
                // Each step runs even when an earlier one fails: a broken app must still
                // leave the registry, its package and the YAML.
                let removed = auth::remove(&cfg.home, id);
                let uninstalled = crate::apps::uninstall_at(&cfg.home, id).map(|_| ());
                let updated = crate::config::set_app(&cfg.path, id, install);
                let first = match removed {
                    Ok(()) => uninstalled,
                    Err(error) => Err(crate::failure::also(error, uninstalled)),
                };
                match (first, updated) {
                    (Ok(()), updated) => updated?,
                    (Err(error), Ok(updated)) => {
                        *connected.app_settings.write().recover() = updated.silicon.clone();
                        return Err(error);
                    }
                    (Err(error), Err(later)) => {
                        return Err(crate::failure::also(error, Err(later)))
                    }
                }
            };
            let mut redactions = cfg.clone();
            redactions.silicon.app_configs = updated.silicon.app_configs.clone();
            crate::telemetry::register(&redactions);
            *connected.app_settings.write().recover() = updated.silicon.clone();
            if install {
                let configs = updated
                    .silicon
                    .app_configs
                    .into_iter()
                    .filter(|(app, _)| app == id)
                    .collect();
                auth::configure(&cfg.home, &configs)?;
            }
            Ok(json!({"app_id":id,"installed":install}))
        }
        "send" => {
            let options: SendOptions =
                serde_json::from_value(args.clone()).context("invalid send options")?;
            let sent = app.runtime.send_connected(
                &connected,
                Some(caller),
                text(args, "isi")?,
                text(args, "message")?,
                &options,
                false,
            )?;
            Ok(json!({"session":sent.session,"delivery_id":sent.id}))
        }
        "sessions" => sessions(&app.runtime, &connected, args),
        "show" => app
            .runtime
            .show_connected(&connected, text(args, "isi")?, optional(args, "id")?),
        "end" => {
            app.runtime
                .end_connected(&connected, text(args, "isi")?, optional(args, "id")?)?;
            Ok(json!({"ended":true}))
        }
        "new-session" => Ok(json!(app.runtime.new_session_connected(
            &connected,
            caller,
            &serde_json::from_value::<NewSession>(args.clone())
                .context("invalid new-session arguments")?
        )?)),
        "auth-setup" => {
            let c = &connected;
            Ok(
                json!({"app_id":auth::setup_scoped(&c.cfg.home,&caller.silicon,c.cfg.silicon.org_id.as_deref().unwrap(),c.cfg.silicon.token.as_deref().unwrap(),text(args,"app")?,c.cfg.generation)?}),
            )
        }
        "auth-remove" => {
            let c = &connected;
            auth::remove(&c.cfg.home, text(args, "app")?)?;
            Ok(json!({"removed":true}))
        }
        _ => bail!("unknown si action {action:?}"),
    }
}

fn sessions(runtime: &Runtime, connected: &Connected, args: &Value) -> Result<Value> {
    let archived = flag(args, "archived")?;
    let records = json!(runtime.list_connected(connected, text(args, "isi")?, archived)?);
    if !archived {
        return Ok(records);
    }
    let filters: Vec<String> = if args["filters"].is_null() {
        Vec::new()
    } else {
        serde_json::from_value(args["filters"].clone()).with_context(|| {
            format!(
                "archive filters must be a list of strings, got {}",
                args["filters"]
            )
        })?
    };
    let timezone =
        optional(args, "timezone")?.unwrap_or(connected.cfg.silicon.timezone.as_deref().unwrap());
    crate::cli::filter_sessions(records, &filters, timezone, chrono::Utc::now())
}

pub fn tail(path: &Path, count: usize) -> Result<Vec<String>> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        // No log yet; any other open failure is reported, not shown as an empty log.
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error).with_context(|| format!("open {}", path.display())),
    };
    let mut position = file
        .metadata()
        .with_context(|| format!("read the size of {}", path.display()))?
        .len();
    let mut chunks = Vec::new();
    let mut lines = 0;
    while position > 0 && lines <= count {
        let size = position.min(8192) as usize;
        position -= size as u64;
        let mut chunk = vec![0; size];
        file.seek(SeekFrom::Start(position))
            .and_then(|_| file.read_exact(&mut chunk))
            .with_context(|| format!("read {} at byte {position}", path.display()))?;
        lines += chunk.iter().filter(|b| **b == b'\n').count();
        chunks.push(chunk);
    }
    let bytes: Vec<_> = chunks.into_iter().rev().flatten().collect();
    Ok(String::from_utf8_lossy(&bytes)
        .lines()
        .rev()
        .take(count)
        .map(str::to_owned)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write as _;

    fn test_app(dir: &Path, saved: Vec<Connection>, proxied: bool) -> Arc<App> {
        app_on(dir, saved, proxied, 1)
    }

    fn app_on(dir: &Path, saved: Vec<Connection>, proxied: bool, port: u16) -> Arc<App> {
        let daemon = Daemon {
            pid: std::process::id(),
            port,
            token: "test-capability".into(),
            ..Daemon::default()
        };
        let runtime = Runtime::new(format!("http://127.0.0.1:{port}"));
        Arc::new(App::new(runtime, daemon, dir.to_path_buf(), saved, proxied))
    }

    fn saved(root: &Path, name: &str) -> Connection {
        Connection {
            id: format!("si:{name}"),
            yaml: root.join(name).join("silicon.yaml"),
            home: root.join(name),
            host: format!("{name}.org.localhost"),
        }
    }

    /// A saved Silicon whose home exists, so its errors land in its silicon.log.
    fn saved_home(root: &Path, name: &str) -> Connection {
        let connection = saved(root, name);
        fs::create_dir_all(connection.home.join(".silicon")).unwrap();
        connection
    }

    fn errors_logged(home: &Path) -> Vec<String> {
        fs::read_to_string(home.join(".silicon/silicon.log"))
            .unwrap_or_default()
            .lines()
            .filter(|line| line.starts_with("[error] "))
            .map(str::to_owned)
            .collect()
    }

    fn ids(registry: &Registry) -> Vec<String> {
        registry
            .entries
            .iter()
            .map(|e| e.connection.id.clone())
            .collect()
    }

    /// Run one `#[ignore]`d test of this module in a child process: for tests that change
    /// process-wide state (environment, file descriptors, signals).
    fn child(test: &str, envs: &[(&str, &std::ffi::OsStr)]) -> std::process::Output {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                &format!("server::tests::{test}"),
                "--exact",
                "--ignored",
                "--nocapture",
            ])
            .envs(envs.iter().copied());
        command.output().unwrap()
    }

    /// One raw HTTP/1.1 request, for a Host header that clients would set themselves.
    fn raw(port: u16, method: &str, host: &str, path: &str, body: &str) -> String {
        let mut stream = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        write!(
            stream,
            "{method} {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        let mut answer = String::new();
        stream.read_to_string(&mut answer).unwrap();
        answer
    }

    #[test]
    fn configuration_redacts_credentials_and_their_interpolated_copies() {
        let cfg: Config = serde_json::from_value(json!({
            "silicon": {
                "id": "si:test", "org_id": "org", "token": "private-silicon-value", "timezone": "UTC",
                "SILICON_HOME": "/home/silicon/project", "inference_providers": [],
                "app_configs": {"app": {"nested": {"custom": "private-app-value"}}},
                "space_station": {"table_name": "activity", "table_key": "private-table-value"}
            },
            "isi": {"worker": {"model": "fast", "dna": {
                "assemble": ["token=private-silicon-value key=private-table-value"]
            }}},
            "access": {}, "flow": [{"value": "private-table-value/private-silicon-value/private-app-value"}]
        }))
        .unwrap();
        let snapshot = configuration(&cfg).unwrap();
        assert_eq!(snapshot["silicon"]["token"], "[redacted]");
        assert_eq!(
            snapshot["silicon"]["space_station"]["table_key"],
            "[redacted]"
        );
        assert_eq!(
            snapshot["isi"]["worker"]["dna"]["assemble"][0],
            "token=[redacted] key=[redacted]"
        );
        assert_eq!(
            snapshot["flow"][0]["value"],
            "[redacted]/[redacted]/[redacted]"
        );
        assert_eq!(snapshot["silicon"]["app_configs"]["app"], "[redacted]");
        assert_eq!(
            snapshot["silicon"]["space_station"]["table_name"],
            "activity"
        );
        assert_eq!(snapshot["isi"]["worker"]["model"], "fast");
        assert_eq!(cfg.silicon.token.as_deref(), Some("private-silicon-value"));
    }

    #[test]
    fn http_compile_blocks_restart_until_its_shell_and_response_finish() {
        let home = tempfile::tempdir().unwrap();
        let yaml = home.path().join("silicon.yaml");
        fs::write(
            &yaml,
            format!(
                r#"
silicon:
  id: si:test
  org_id: org
  token: test
  timezone: UTC
  SILICON_HOME: {}
  inference_providers: [all-available-providers]
isi:
  a:
    model: '! touch started; while ! test -f release; do sleep 0.01; done; printf fast'
    primary_send_mode: global
    session_type: persistent
    dna: {{assemble: [], next_refresh: 30min}}
access: {{a: []}}
flow: []
"#,
                serde_json::to_string(home.path()).unwrap()
            ),
        )
        .unwrap();
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        let app = app_on(home.path(), Vec::new(), false, port);
        let (runtime, daemon) = (app.runtime.clone(), app.daemon.clone());
        let handler = thread::spawn(move || {
            handle(
                app,
                server
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap()
                    .unwrap(),
            );
        });
        let request = thread::spawn(move || call(&daemon, "compile", json!({"yaml": yaml})));
        let deadline = Instant::now() + Duration::from_secs(5);
        let started = home.path().join("started");
        while !started.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        let shell_started = started.exists();
        let restarted_while_busy = runtime.begin_restart_if_idle();
        // Release the shell before asserting so even a failing check cannot strand it.
        fs::write(home.path().join("release"), "").unwrap();
        assert!(shell_started, "compile did not reach its Bash expression");
        assert!(!restarted_while_busy);
        assert_eq!(request.join().unwrap().unwrap()["valid"], true);
        handler.join().unwrap();
        assert!(runtime.begin_restart_if_idle());
    }

    #[test]
    fn http_errors_carry_the_tools_words_and_mask_every_registered_credential() {
        let home = tempfile::tempdir().unwrap();
        // A credential another Silicon registered with this interpreter.
        let other = PathBuf::from(format!("/test/{}", Uuid::new_v4()));
        let mut registered: Config = serde_json::from_value(json!({
            "silicon": {"id":"si:other", "org_id":"org", "token":"other-silicon-private-token"},
            "isi":{}, "access":{}, "flow":[]
        }))
        .unwrap();
        registered.home = other.clone();
        crate::telemetry::register(&registered);
        let yaml = home.path().join("silicon.yaml");
        fs::write(
            &yaml,
            format!(
                r#"
silicon:
  id: si:test
  org_id: org
  token: test
  timezone: UTC
  SILICON_HOME: {}
  inference_providers: [all-available-providers]
isi:
  a:
    model: '! echo "model lookup failed for other-silicon-private-token" >&2; echo "[\"no model\"]"; exit 3'
    primary_send_mode: global
    session_type: persistent
    dna: {{assemble: [], next_refresh: 30min}}
access: {{a: []}}
flow: []
"#,
                serde_json::to_string(home.path()).unwrap()
            ),
        )
        .unwrap();
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        let app = app_on(home.path(), Vec::new(), false, port);
        let daemon = app.daemon.clone();
        let handler = thread::spawn(move || {
            handle(
                app,
                server
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap()
                    .unwrap(),
            );
        });
        let error = format!(
            "{:#}",
            call(&daemon, "compile", json!({"yaml": yaml})).unwrap_err()
        );
        handler.join().unwrap();
        crate::telemetry::unregister(&other);
        for said in [
            "exit status: 3",
            "stderr:\nmodel lookup failed for [redacted]",
            "stdout:\n[\"no model\"]",
        ] {
            assert!(error.contains(said), "missing {said:?} in {error}");
        }
        assert!(!error.contains("other-silicon-private-token"), "{error}");
    }

    #[test]
    fn failed_interpreter_start_carries_its_status_and_every_line_it_wrote() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("daemon.log");
        fs::write(&log, "an earlier run's line\n").unwrap();
        let start = fs::metadata(&log).unwrap().len();
        let app = dir.path().join("silicon");
        fs::write(
            &app,
            "#!/bin/sh\necho 'binding 127.0.0.1:4242'\necho 'Error: Address already in use (os error 48)' >&2\necho '{\"hint\":\"stop the other interpreter\"}'\necho 'restoring with stk-0123456789abcdef' >&2\nexit 3\n",
        )
        .unwrap();
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700)).unwrap();
        let file = OpenOptions::new().append(true).open(&log).unwrap();
        let mut child = Command::new(&app)
            .stdout(file.try_clone().unwrap())
            .stderr(file)
            .spawn()
            .unwrap();
        let error = await_start(dir.path(), &mut child, start, Duration::from_secs(10))
            .err()
            .unwrap();
        let error = format!("{error:#}");
        assert!(
            error.starts_with("interpreter exited before answering: exit status: 3\n"),
            "{error}"
        );
        for expected in [
            "binding 127.0.0.1:4242",
            "Error: Address already in use (os error 48)",
            "{\"hint\":\"stop the other interpreter\"}",
            &log.display().to_string(),
            "last ping: read ",
        ] {
            assert!(error.contains(expected), "missing {expected:?} in {error}");
        }
        assert!(!error.contains("an earlier run's line"), "{error}");
        assert!(
            error.contains("restoring with [redacted]") && !error.contains("stk-0123456789abcdef"),
            "{error}"
        );
    }

    #[test]
    fn interpreter_answers_reach_the_caller_with_status_url_and_raw_body() {
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let url = format!(
            "http://127.0.0.1:{}/control",
            server.server_addr().to_ip().unwrap().port()
        );
        let tool = "`ting webhook http://x.localhost/events --json` failed: exit status: 3\nstderr:\nboom\nstdout:\n{\"ok\":false}";
        let answers = [
            (502, "upstream exploded\nsecond line".to_owned()),
            (400, json!({"error": tool}).to_string()),
            (500, json!({"detail": "no error field"}).to_string()),
        ];
        let replies = thread::spawn(move || {
            for (status, body) in answers {
                let request = server
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap()
                    .unwrap();
                request
                    .respond(Response::from_string(body).with_status_code(status))
                    .unwrap();
            }
        });
        let error = format!(
            "{:#}",
            request(&url, "t", "connect", json!({})).unwrap_err()
        );
        assert!(
            error.contains(&format!("`connect` at {url} with HTTP 502 Bad Gateway"))
                && error.contains("upstream exploded\nsecond line"),
            "{error}"
        );
        let error = request(&url, "t", "connect", json!({})).unwrap_err();
        assert_eq!(format!("{error:#}"), tool);
        let error = format!("{:#}", request(&url, "t", "list", json!({})).unwrap_err());
        assert!(
            error.contains("HTTP 500 Internal Server Error and no error message")
                && error.contains("{\"detail\":\"no error field\"}"),
            "{error}"
        );
        replies.join().unwrap();
    }

    #[test]
    fn a_quick_action_that_gets_no_answer_says_how_long_it_waited() {
        // Accepts the connection and never answers, like a wedged interpreter.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!(
            "http://127.0.0.1:{}/control",
            listener.local_addr().unwrap().port()
        );
        let started = Instant::now();
        let error = format!("{:#}", request(&url, "t", "ping", json!({})).unwrap_err());
        let waited = started.elapsed();
        drop(listener);
        assert!(
            waited >= QUICK && waited < QUICK + Duration::from_secs(10),
            "{waited:?}"
        );
        assert!(
            error.starts_with(&format!(
                "interpreter request `ping` to {url} failed: no answer within 15s: timeout"
            )),
            "{error}"
        );
    }

    #[test]
    #[ignore = "run by control_calls_never_use_a_proxy_from_the_environment, with proxy variables set"]
    fn proxied_environment_child() {
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        let replies = thread::spawn(move || {
            let request = server
                .recv_timeout(Duration::from_secs(10))
                .unwrap()
                .unwrap();
            request
                .respond(Response::from_string("{\"answered\":true}"))
                .unwrap();
        });
        let value = request(
            &format!("http://127.0.0.1:{port}/control"),
            "t",
            "ping",
            json!({}),
        )
        .unwrap();
        assert_eq!(value["answered"], true);
        replies.join().unwrap();
    }

    #[test]
    fn control_calls_never_use_a_proxy_from_the_environment() {
        // A proxy nothing listens on: a call that went through it would fail.
        let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let proxy = format!("http://127.0.0.1:{}", closed.local_addr().unwrap().port());
        drop(closed);
        let proxy = std::ffi::OsString::from(proxy);
        let mut envs: Vec<(&str, &std::ffi::OsStr)> = [
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        ]
        .into_iter()
        .map(|name| (name, proxy.as_os_str()))
        .collect();
        envs.push(("NO_PROXY", "".as_ref()));
        envs.push(("no_proxy", "".as_ref()));
        let output = child("proxied_environment_child", &envs);
        assert!(
            output.status.success(),
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    fn misses_and_panics_explain_themselves() {
        let dir = tempfile::tempdir().unwrap();
        let app = test_app(dir.path(), Vec::new(), false);
        let error = resolve(&app, "/nonexistent/silicon.yaml")
            .unwrap_err()
            .to_string();
        assert!(
            error.starts_with(
                "unknown silicon: /nonexistent/silicon.yaml (resolving it as a YAML path failed: "
            ) && error.ends_with("; no Silicons are connected"),
            "{error}"
        );
        let error = guarded(|| panic!("lock poisoned by {}", "worker")).unwrap_err();
        assert!(
            error
                .to_string()
                .starts_with("interpreter panicked: lock poisoned by worker"),
            "{error}"
        );
        assert_eq!(
            text(&json!({"isi": 3}), "isi").unwrap_err().to_string(),
            "isi must be a string, got 3"
        );
        // A mistyped optional argument is reported, not silently treated as absent.
        assert_eq!(optional(&json!({"id": null}), "id").unwrap(), None);
        assert_eq!(
            optional(&json!({"id": 7}), "id").unwrap_err().to_string(),
            "id must be a string, got 7"
        );
        assert_eq!(
            flag(&json!({"archived": "yes"}), "archived")
                .unwrap_err()
                .to_string(),
            "archived must be boolean, got \"yes\""
        );
    }

    #[test]
    fn unattended_failures_reach_the_silicon_log_whole_and_masked() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join(".silicon")).unwrap();
        let failure = "restore Ting for si:x: `ting webhook http://x.localhost/events --json` failed: exit status: 3\nstderr:\nboom {\"access_token\":\"private-access-value\"}\nstdout:\n{\"ok\":false} stk-0123456789abcdef";
        report(home.path(), None, "ting", failure);
        let log = fs::read_to_string(home.path().join(".silicon/silicon.log")).unwrap();
        assert!(log.starts_with("[error] [ting/cli] ["), "{log}");
        assert_eq!(log.lines().count(), 1, "{log}");
        for expected in [
            "[restore Ting for si:x: `ting webhook http://x.localhost/events --json` failed: exit status: 3",
            "\\nstderr:\\nboom {\"access_token\":\"[redacted]\"}",
            "\\nstdout:\\n{\"ok\":false} [redacted]]",
        ] {
            assert!(log.contains(expected), "missing {expected:?} in {log}");
        }
        assert!(
            !log.contains("private-access-value") && !log.contains("stk-0123456789abcdef"),
            "{log}"
        );
        // A home that is gone is not recreated just to hold the line.
        let gone = home.path().join("gone");
        report(&gone, None, "ting", failure);
        assert!(!gone.exists());
    }

    #[test]
    fn the_interpreter_directory_is_never_relative_to_the_working_directory() {
        let account = || Ok(PathBuf::from("/home/account"));
        let cwd = || Ok(PathBuf::from("/work"));
        let located = |explicit: Option<&str>, home: Option<&str>| {
            interpreter_directory(explicit.map(Into::into), home.map(Into::into), account, cwd)
                .unwrap()
        };
        assert_eq!(located(Some("/state"), None), PathBuf::from("/state"));
        assert_eq!(located(Some("state"), None), PathBuf::from("/work/state"));
        assert_eq!(
            located(Some(""), Some("/home/me")),
            PathBuf::from("/home/me/.silicon-interpreter")
        );
        for home in [None, Some(""), Some("relative/home")] {
            assert_eq!(
                located(None, home),
                PathBuf::from("/home/account/.silicon-interpreter"),
                "{home:?}"
            );
        }
        let error = format!(
            "{:#}",
            interpreter_directory(
                None,
                Some("relative".into()),
                || Err("the password database has no home directory for uid 7".into()),
                cwd
            )
            .unwrap_err()
        );
        assert!(
            error.contains("HOME is the relative path \"relative\"")
                && error.contains("the password database has no home directory for uid 7"),
            "{error}"
        );
        // This account has a home in the password database.
        assert!(passwd_home().unwrap().is_absolute());
    }

    #[test]
    fn registry_changes_never_drop_saved_silicons_that_wait_for_restore() {
        let root = Path::new("/test/registry");
        let (a, b, c) = (saved(root, "a"), saved(root, "b"), saved(root, "c"));
        // A duplicate saved entry is kept once.
        let mut registry = Registry::restoring(vec![a.clone(), c.clone(), a.clone()]);
        assert_eq!(registry.connections(), vec![a.clone(), c.clone()]);
        assert_eq!(registry.counts(), (2, 0));
        let now = Utc::now();
        registry
            .failed(&a.id, "network is down".into(), now)
            .unwrap();
        registry.connected(None, c.clone(), now);
        // Connecting B and disconnecting C leaves A saved, still waiting.
        registry.connected(None, b.clone(), now);
        assert!(registry.remove(&c.id).is_some());
        assert_eq!(ids(&registry), ["si:a", "si:b"]);
        assert!(matches!(
            registry.entry(&a.id).unwrap().state,
            Restore::Waiting { .. }
        ));
        assert_eq!(registry.counts(), (0, 1));
        // A restore that finds a new ID in the same YAML replaces its entry in place.
        let version = registry.version;
        let migrated = Connection {
            id: "si:a2".into(),
            ..a.clone()
        };
        registry.connected(Some(&a.id), migrated.clone(), now);
        assert_eq!(registry.connections(), vec![migrated.clone(), b.clone()]);
        assert!(registry.version > version);
        // Saving the same descriptor again is not a change.
        let version = registry.version;
        registry.connected(None, b.clone(), now);
        assert_eq!(registry.version, version);
        // Another Silicon connected from B's home supersedes B.
        let moved = Connection {
            id: "si:moved".into(),
            yaml: root.join("elsewhere.yaml"),
            ..b.clone()
        };
        registry.connected(None, moved.clone(), now);
        assert_eq!(registry.connections(), vec![migrated, moved]);
        assert!(registry.remove("si:unknown").is_none());
    }

    #[test]
    fn a_corrupt_registry_is_moved_aside_whole_and_the_interpreter_starts_empty() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("connections.json");
        assert!(load_registry(dir.path()).unwrap().is_empty());
        fs::write(&path, "[{\"id\": \"si:a\", truncated").unwrap();
        assert!(load_registry(dir.path()).unwrap().is_empty());
        assert!(!path.exists());
        let moved: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(moved.len(), 1, "{moved:?}");
        assert!(
            moved[0].starts_with("connections.json.corrupt-"),
            "{moved:?}"
        );
        assert_eq!(
            fs::read_to_string(dir.path().join(&moved[0])).unwrap(),
            "[{\"id\": \"si:a\", truncated"
        );
        // A valid one loads as saved.
        let a = saved(dir.path(), "a");
        state::write_json(&path, &vec![a.clone()]).unwrap();
        assert_eq!(load_registry(dir.path()).unwrap(), vec![a]);
    }

    #[test]
    fn retries_back_off_from_five_seconds_to_ten_minutes_with_bounded_jitter() {
        let (first, cap) = (Duration::from_secs(5), Duration::from_secs(600));
        let delays: Vec<_> = (0..=9).map(|n| backoff(n, first, cap).as_secs()).collect();
        assert_eq!(delays, [5, 5, 10, 20, 40, 80, 160, 320, 600, 600]);
        assert_eq!(backoff(u32::MAX, first, cap), cap);
        let samples: Vec<_> = (0..2000)
            .map(|_| jittered(Duration::from_secs(10), 0.2))
            .collect();
        let (low, high) = (Duration::from_secs(8), Duration::from_secs(12));
        assert!(samples.iter().all(|d| (low..=high).contains(d)));
        // Spread over the whole range, not stuck at one end.
        assert!(samples.iter().any(|d| *d < Duration::from_secs(9)));
        assert!(samples.iter().any(|d| *d > Duration::from_secs(11)));
        for _ in 0..100 {
            let delay = retry_delay(30);
            assert!(
                (Duration::from_secs(480)..=Duration::from_secs(720)).contains(&delay),
                "{delay:?}"
            );
        }
    }

    #[test]
    fn a_clock_set_back_never_postpones_a_retry() {
        let now = Utc::now();
        assert!(arrived(now, now, LONGEST_RETRY));
        assert!(!arrived(now + TimeDelta::minutes(5), now, LONGEST_RETRY));
        // Scheduled a few minutes ahead, then the clock went back a day: due now.
        assert!(arrived(now + TimeDelta::days(1), now, LONGEST_RETRY));
        let mut registry = Registry::restoring(vec![saved(Path::new("/test"), "a")]);
        registry.failed("si:a", "down".into(), now + TimeDelta::days(1));
        assert_eq!(registry.due_restore(now), Some("si:a".to_owned()));
        registry.connected(
            None,
            saved(Path::new("/test"), "a"),
            now + TimeDelta::days(2),
        );
        assert_eq!(registry.due_ting(now), Some("si:a".to_owned()));
    }

    #[test]
    fn a_failed_restore_waits_its_backoff_and_reports_each_new_error_once() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved_home(dir.path(), "a");
        let app = test_app(dir.path(), vec![a.clone()], false);
        let next = |app: &App| match &app.registry.lock().recover().entry(&a.id).unwrap().state {
            Restore::Waiting { next_at, .. } => *next_at,
            other => panic!("not waiting: {other:?}"),
        };
        let mut now = Utc::now();
        for (round, error) in ["network is down", "network is down", "DNS lookup failed"]
            .into_iter()
            .enumerate()
        {
            assert!(restore_next(&app, now, |_, attempted| {
                assert_eq!(attempted, &a);
                Err(anyhow!(error))
            }));
            let at = next(&app);
            let delay = (at - now).to_std().unwrap();
            let expected = backoff(round as u32 + 1, Duration::from_secs(5), Duration::MAX);
            assert!(
                delay >= expected.mul_f64(0.79) && delay <= expected.mul_f64(1.21),
                "round {round}: {delay:?}"
            );
            // Nothing is due before then.
            assert!(!restore_next(
                &app,
                at - TimeDelta::milliseconds(1),
                |_, _| panic!("attempted before its backoff")
            ));
            now = at;
        }
        let row = app.registry.lock().recover().listed()[0].clone();
        assert_eq!(row.state, "waiting");
        assert_eq!(row.attempt, Some(3));
        assert_eq!(row.error.as_deref(), Some("DNS lookup failed"));
        assert!(row.retry_at.is_some());
        // The first error and the changed one are reported whole; the repeat is not.
        let logged = errors_logged(&a.home);
        assert_eq!(logged.len(), 2, "{logged:?}");
        assert!(
            logged[0].contains("restore si:a from ")
                && logged[0].contains("failed (attempt 1); the next attempt runs at ")
                && logged[0].ends_with(": network is down]"),
            "{logged:?}"
        );
        assert!(logged[1].ends_with(": DNS lookup failed]"), "{logged:?}");
        // A later success connects it and saves the registry.
        assert!(restore_next(&app, now, |_, attempted| Ok(Restored {
            connection: attempted.clone(),
            home: attempted.home.clone(),
            generation: Uuid::new_v4(),
            ting: Ok(()),
        })));
        assert_eq!(app.registry.lock().recover().listed()[0].state, "connected");
        let log = fs::read_to_string(a.home.join(".silicon/silicon.log")).unwrap();
        assert!(log.contains("[restored si:a on attempt 4]"), "{log}");
    }

    #[test]
    fn a_slow_failing_restore_does_not_keep_the_others_waiting() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (saved(dir.path(), "a"), saved(dir.path(), "b"));
        let app = test_app(dir.path(), vec![a.clone(), b.clone()], false);
        let now = Utc::now();
        // A fails after twenty minutes, far past its first backoff.
        assert!(restore_next(&app, now, |_, attempted| {
            assert_eq!(attempted, &a);
            Err(anyhow!("setup timed out"))
        }));
        // Timed from when the attempt ended, and B has had no attempt yet: B goes next.
        let later_on = now + TimeDelta::minutes(20);
        assert!(restore_next(&app, later_on, |_, attempted| {
            assert_eq!(
                attempted, &b,
                "the failed Silicon was retried before the other one"
            );
            Err(anyhow!("offline"))
        }));
        // Of two waiting ones, the one that has waited longest goes first.
        let registry = app.registry.lock().recover();
        assert_eq!(
            registry.due_restore(later_on + TimeDelta::hours(1)),
            Some(a.id.clone())
        );
    }

    #[test]
    fn a_waiting_interactive_request_gets_the_lock_before_the_next_restore() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved(dir.path(), "a");
        let app = test_app(dir.path(), vec![a], false);
        app.waiting.fetch_add(1, Ordering::SeqCst);
        assert!(!restore_next(&app, Utc::now(), |_, _| panic!(
            "restored while a connect waited"
        )));
        app.waiting.fetch_sub(1, Ordering::SeqCst);
        assert!(restore_next(&app, Utc::now(), |_, _| Err(anyhow!(
            "offline"
        ))));
    }

    #[test]
    fn a_handover_starts_the_service_first_and_stays_put_when_it_cannot() {
        let dir = tempfile::tempdir().unwrap();
        let app = test_app(dir.path(), Vec::new(), false);
        *app.restart.check.lock().recover() = Check {
            passed: Some(PathBuf::from("/prefix/bin/silicon")),
            ..Check::default()
        };
        app.restart.handover.store(true, Ordering::SeqCst);
        app.restart.requested.store(true, Ordering::SeqCst);
        // Nothing stops until the service that takes over has started.
        assert!(app.restart.ready().is_none());
        handover_tick(&app, || Ok("launchd".into()));
        assert!(app.restart.handed.load(Ordering::SeqCst));
        assert!(app.restart.ready().is_some());
        // A service that cannot start now: the request is dropped and nothing restarts.
        let app = test_app(dir.path(), Vec::new(), false);
        app.restart.handover.store(true, Ordering::SeqCst);
        app.restart.requested.store(true, Ordering::SeqCst);
        handover_tick(&app, || {
            Err(anyhow!("no GUI login: launchctl answered 125"))
        });
        assert!(!app.restart.handed.load(Ordering::SeqCst));
        assert!(!app.restart.requested.load(Ordering::SeqCst));
        assert!(app.restart.ready().is_none());
    }

    #[test]
    fn repeated_proxy_failures_compare_without_their_varying_names() {
        let first = recurring("could not connect to /tmp/silicon-caddy-f8c9de31-5bb9-489f-9e8c-0e702df69b62/admin.sock; Caddy process 76541 exited");
        let second = recurring("could not connect to /tmp/silicon-caddy-0a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d/admin.sock; Caddy process 80031 exited");
        assert_eq!(first, second);
        assert_ne!(first, recurring("bind: address already in use"));
    }

    #[test]
    fn a_supervised_start_is_awaited_until_it_answers() {
        let dir = tempfile::tempdir().unwrap();
        let error = format!(
            "{:#}",
            await_supervised(dir.path(), "launchd", 0, Duration::from_millis(300)).unwrap_err()
        );
        assert!(
            error.starts_with("the launchd service was asked to start the interpreter, but it did not answer within 0s")
                && error.contains("last ping: read "),
            "{error}"
        );
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let daemon = Daemon {
            pid: std::process::id(),
            port: server.server_addr().to_ip().unwrap().port(),
            token: "t".into(),
            supervisor: Some("launchd".into()),
            ..Daemon::default()
        };
        let answering = thread::spawn(move || {
            let request = server
                .recv_timeout(Duration::from_secs(10))
                .unwrap()
                .unwrap();
            request
                .respond(Response::from_string("{}").with_status_code(200))
                .unwrap();
        });
        let writer = {
            let path = dir.path().join("daemon.json");
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(300));
                state::write_json(&path, &daemon).unwrap();
            })
        };
        let found = await_supervised(dir.path(), "launchd", 0, Duration::from_secs(10)).unwrap();
        assert_eq!(found.supervisor.as_deref(), Some("launchd"));
        writer.join().unwrap();
        answering.join().unwrap();
    }

    #[test]
    fn a_restore_that_panics_is_a_failure_with_backoff_not_a_tight_loop() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved(dir.path(), "a");
        let app = test_app(dir.path(), vec![a.clone()], false);
        let now = Utc::now();
        assert!(restore_next(&app, now, |_, _| -> Result<Restored> {
            panic!("unexpected descriptor")
        }));
        let row = app.registry.lock().recover().listed()[0].clone();
        assert_eq!(row.state, "waiting");
        assert_eq!(
            row.error.as_deref(),
            Some("restoring a Silicon panicked: unexpected descriptor")
        );
        assert!(!restore_next(&app, now, |_, _| panic!("retried at once")));
    }

    #[test]
    fn a_restore_whose_ting_registration_fails_stays_connected_and_retries_ting_alone() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved_home(dir.path(), "a");
        let app = test_app(dir.path(), vec![a.clone()], false);
        let now = Utc::now();
        // The saved descriptor is stale: the YAML now names another ID, saved in its place.
        let current = Connection {
            id: "si:current".into(),
            ..a.clone()
        };
        assert!(restore_next(&app, now, |_, _| Ok(Restored {
            connection: current.clone(),
            home: a.home.clone(),
            generation: Uuid::new_v4(),
            ting: Err(anyhow!("ting webhook: connection refused")),
        })));
        let row = app.registry.lock().recover().listed()[0].clone();
        assert_eq!(row.connection, current);
        assert_eq!(row.state, "connected");
        assert_eq!(row.ting_attempt, Some(1));
        assert!(row.ting_retry_at.is_some());
        assert!(
            row.ting_error
                .as_deref()
                .unwrap()
                .starts_with("restore Ting for si:current: ting webhook: connection refused"),
            "{row:?}"
        );
        let file: Vec<Connection> =
            serde_json::from_slice(&fs::read(dir.path().join("connections.json")).unwrap())
                .unwrap();
        assert_eq!(file, vec![current.clone()]);
        let logged = errors_logged(&a.home);
        assert_eq!(logged.len(), 1, "{logged:?}");
        assert!(
            logged[0].contains("si:current stays connected"),
            "{logged:?}"
        );
        // Ting alone is retried after its backoff, and a success clears the failure.
        let registry = app.registry.lock().recover().clone();
        assert_eq!(registry.due_ting(now), None);
        assert_eq!(
            registry.due_ting(now + TimeDelta::seconds(7)),
            Some(current.id.clone())
        );
        assert_eq!(
            app.registry.lock().recover().ting_ok(&current.id, now),
            Some(1)
        );
        let row = app.registry.lock().recover().listed()[0].clone();
        assert!(row.ting_error.is_none() && row.ting_attempt.is_none());
        let registry = app.registry.lock().recover().clone();
        assert_eq!(registry.due_ting(now + TimeDelta::hours(5)), None);
        assert!(registry.due_ting(now + TimeDelta::hours(7)).is_some());
        // Registered but not live (a disconnect finishing): no Ting call, looked at later.
        assert!(reassert_next(&app, now + TimeDelta::hours(7), |_| panic!(
            "registered a Silicon that is not live"
        )));
        assert_eq!(
            app.registry
                .lock()
                .recover()
                .due_ting(now + TimeDelta::hours(8)),
            None
        );
    }

    #[test]
    fn a_waiting_silicon_can_be_disconnected_and_is_forgotten() {
        let dir = tempfile::tempdir().unwrap();
        let (a, b) = (saved(dir.path(), "a"), saved(dir.path(), "b"));
        let app = test_app(dir.path(), vec![a.clone(), b.clone()], false);
        app.registry
            .lock()
            .recover()
            .failed(&a.id, "no network".into(), Utc::now());
        let listed = control(&app, &json!({"action":"list","args":{}})).unwrap();
        assert_eq!(listed[0]["state"], "waiting");
        assert_eq!(listed[0]["error"], "no network");
        assert_eq!(listed[1]["state"], "restoring");
        assert!(listed[1].get("error").is_none());
        let ping = control(&app, &json!({"action":"ping","args":{}})).unwrap();
        assert_eq!(
            (&ping["restoring"], &ping["waiting"], &ping["proxy"]),
            (&json!(1), &json!(1), &json!("disabled"))
        );
        let answer = control(
            &app,
            &json!({"action":"disconnect","args":{"target":"si:a"}}),
        )
        .unwrap();
        assert_eq!(answer["disconnected"], "si:a");
        let file: Vec<Connection> =
            serde_json::from_slice(&fs::read(dir.path().join("connections.json")).unwrap())
                .unwrap();
        assert_eq!(file, vec![b.clone()]);
        let error = format!(
            "{:#}",
            control(
                &app,
                &json!({"action":"disconnect","args":{"target":"si:a"}})
            )
            .unwrap_err()
        );
        assert_eq!(error, "unknown silicon: si:a; connected: si:b");
        let online = control(
            &app,
            &json!({"action":"silicon-ping","args":{"silicon":"si:b"}}),
        )
        .unwrap();
        assert_eq!(online["online"], false);
        assert_eq!(
            online["error"],
            "silicon si:b is being restored; retry shortly"
        );
    }

    #[test]
    fn list_rows_read_the_same_from_old_and_new_interpreters() {
        let old: Listed = serde_json::from_value(
            json!({"id":"si:a","yaml":"/a/silicon.yaml","home":"/a","host":"a.o.localhost"}),
        )
        .unwrap();
        assert_eq!(old.state, "connected");
        assert_eq!(old.connection.id, "si:a");
        let mut registry = Registry::restoring(vec![old.connection.clone()]);
        registry.failed("si:a", "boom\nsecond line".into(), Utc::now());
        let row = serde_json::to_value(registry.listed()).unwrap()[0].clone();
        for key in [
            "id", "yaml", "home", "host", "state", "error", "attempt", "retry_at",
        ] {
            assert!(row.get(key).is_some(), "missing {key} in {row}");
        }
        assert!(row.get("ting_error").is_none(), "{row}");
        let back: Listed = serde_json::from_value(row).unwrap();
        assert_eq!(back.error.as_deref(), Some("boom\nsecond line"));
    }

    #[test]
    fn events_for_a_silicon_still_being_restored_ask_ting_to_retry() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved(dir.path(), "a");
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        let app = app_on(dir.path(), vec![a.clone()], false, port);
        app.registry
            .lock()
            .recover()
            .failed(&a.id, "network is down".into(), Utc::now());
        let serving = {
            let app = app.clone();
            thread::spawn(move || {
                for _ in 0..3 {
                    let request = server
                        .recv_timeout(Duration::from_secs(10))
                        .unwrap()
                        .unwrap();
                    handle(app.clone(), request);
                }
            })
        };
        let body = json!({"tings":[]}).to_string();
        let answer = raw(port, "POST", "a.org.localhost", "/events", &body);
        assert!(answer.starts_with("HTTP/1.1 503"), "{answer}");
        assert!(answer.contains("Retry-After: 30"), "{answer}");
        assert!(
            answer.contains("silicon si:a is not restored yet (attempt 1 failed;")
                && answer.contains("): network is down"),
            "{answer}"
        );
        let answer = raw(port, "POST", "other.org.localhost", "/events", &body);
        assert!(answer.starts_with("HTTP/1.1 404"), "{answer}");
        let answer = raw(port, "GET", "a.org.localhost", "/ping", "");
        assert!(
            answer.starts_with("HTTP/1.1 503") && answer.contains("\"online\":false"),
            "{answer}"
        );
        serving.join().unwrap();
        // Only temporary failures ask for a retry.
        assert!(retryable(&anyhow!("silicon is not connected: si:a")));
        assert!(retryable(
            &anyhow!("interpreter is stopping").context("run the flow")
        ));
        assert!(!retryable(&anyhow!("event requires tings:array")));
    }

    #[test]
    fn a_busy_interpreter_answers_503_without_starting_a_thread() {
        let dir = tempfile::tempdir().unwrap();
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let port = server.server_addr().to_ip().unwrap().port();
        let app = app_on(dir.path(), Vec::new(), false, port);
        app.in_flight.store(MAX_IN_FLIGHT, Ordering::SeqCst);
        let client = thread::spawn(move || raw(port, "GET", "127.0.0.1", "/ping", ""));
        let request = server
            .recv_timeout(Duration::from_secs(10))
            .unwrap()
            .unwrap();
        dispatch(&app, request);
        let answer = client.join().unwrap();
        assert!(answer.starts_with("HTTP/1.1 503"), "{answer}");
        assert!(
            answer.contains(&format!(
                "interpreter busy: {MAX_IN_FLIGHT} requests in flight; retry"
            )),
            "{answer}"
        );
        assert_eq!(app.in_flight.load(Ordering::SeqCst), MAX_IN_FLIGHT);
        // Below the cap a request gets its thread, which gives its slot back when done.
        app.in_flight.store(0, Ordering::SeqCst);
        let client = thread::spawn(move || raw(port, "GET", "127.0.0.1", "/ping", ""));
        let request = server
            .recv_timeout(Duration::from_secs(10))
            .unwrap()
            .unwrap();
        dispatch(&app, request);
        assert!(client.join().unwrap().starts_with("HTTP/1.1 404"));
        let deadline = Instant::now() + Duration::from_secs(5);
        while app.in_flight.load(Ordering::SeqCst) != 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(app.in_flight.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn a_lost_listener_is_bound_again_on_the_same_port() {
        let holder = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = holder.local_addr().unwrap().port();
        // Still taken: the rebind gives up after its limit, saying why.
        let error = format!(
            "{:#}",
            rebind(port, Duration::from_millis(400), Duration::ZERO, || false)
                .err()
                .unwrap()
        );
        assert!(
            error.starts_with(&format!(
                "could not listen on 127.0.0.1:{port} again for 0s ("
            )) && error.contains("attempts); last error: "),
            "{error}"
        );
        // Stopping ends the wait at once.
        assert!(
            rebind(port, Duration::from_secs(30), Duration::ZERO, || true)
                .unwrap()
                .is_none()
        );
        // Freed while retrying: the same port is listened on again.
        let releaser = thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            drop(holder);
        });
        let started = Instant::now();
        let server = rebind(port, Duration::from_secs(30), Duration::ZERO, || false)
            .unwrap()
            .unwrap();
        releaser.join().unwrap();
        assert_eq!(server.server_addr().to_ip().unwrap().port(), port);
        assert!(started.elapsed() < Duration::from_secs(10));
        let emfile: Box<dyn std::error::Error + Send + Sync> =
            Box::new(std::io::Error::from_raw_os_error(libc::EMFILE));
        assert!(out_of_files(emfile.as_ref()));
        assert!(!out_of_files(&std::io::Error::from_raw_os_error(
            libc::ECONNABORTED
        )));
    }

    #[test]
    fn a_supervised_interpreter_waits_for_the_lock_and_a_plain_one_does_not() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("daemon.lock");
        // A longer pid from an earlier holder is replaced whole.
        fs::write(&path, "999999999\n").unwrap();
        let holding = lock(&path, true).unwrap();
        record_holder(&holding);
        assert_eq!(holder(dir.path()), Some(std::process::id()));
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            format!("{}\n", std::process::id())
        );
        let started = Instant::now();
        let error = take_daemon_lock(&path, false, &Stop::default()).unwrap_err();
        assert!(would_block(&error), "{error:#}");
        assert!(started.elapsed() >= Duration::from_secs(1));
        // Supervised: waits; a stop signal ends the wait.
        let stop = Arc::new(Stop::default());
        let signaller = {
            let stop = stop.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(300));
                stop.signalled.store(true, Ordering::SeqCst);
            })
        };
        assert!(take_daemon_lock(&path, true, &stop).unwrap().is_none());
        signaller.join().unwrap();
        // Once free it is taken, and it names its new holder.
        let releaser = thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            drop(holding);
        });
        let taken = take_daemon_lock(&path, true, &Stop::default())
            .unwrap()
            .unwrap();
        releaser.join().unwrap();
        assert!(held(&path).unwrap());
        drop(taken);
        // Another test's fork can hold a duplicate of the descriptor until its exec.
        let released = Instant::now() + Duration::from_secs(5);
        while held(&path).unwrap() {
            assert!(
                Instant::now() < released,
                "daemon.lock stayed held after release"
            );
            thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            format!("{}\n", std::process::id())
        );
    }

    #[test]
    fn a_starting_interpreter_is_awaited_instead_of_started_twice() {
        let dir = tempfile::tempdir().unwrap();
        let holding = lock(&dir.path().join("daemon.lock"), true).unwrap();
        record_holder(&holding);
        let error = format!(
            "{:#}",
            daemon_in(dir.path(), true, Duration::from_secs(1), |_| {
                panic!("started a second interpreter while one holds daemon.lock")
            })
            .unwrap_err()
        );
        assert!(
            error.starts_with(&format!(
                "the interpreter (pid {}) holds {} but did not answer within 1s\n",
                std::process::id(),
                dir.path().join("daemon.lock").display()
            )) && error.contains("last ping: read "),
            "{error}"
        );
        // Asked without starting one, it says the interpreter is on its way.
        let error = format!(
            "{:#}",
            daemon_in(dir.path(), false, Duration::ZERO, |_| panic!("started")).unwrap_err()
        );
        assert!(
            error.contains("but did not answer; it is starting, restarting or stopping"),
            "{error}"
        );
        // When the holder lets go instead of answering, one is started.
        let releaser = thread::spawn(move || {
            thread::sleep(Duration::from_millis(300));
            drop(holding);
        });
        let error = daemon_in(dir.path(), true, Duration::from_secs(30), |_| {
            Err(anyhow!("started one"))
        })
        .unwrap_err();
        releaser.join().unwrap();
        assert_eq!(error.to_string(), "started one");
    }

    #[test]
    fn stop_asks_and_then_waits_until_the_interpreter_lets_go_of_its_lock() {
        let dir = tempfile::tempdir().unwrap();
        let holding = lock(&dir.path().join("daemon.lock"), true).unwrap();
        record_holder(&holding);
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let daemon = Daemon {
            pid: std::process::id(),
            port: server.server_addr().to_ip().unwrap().port(),
            token: "t".into(),
            ..Daemon::default()
        };
        state::write_json(&dir.path().join("daemon.json"), &daemon).unwrap();
        // A fake interpreter: answers ping and shutdown, then takes a while to exit.
        let interpreter = thread::spawn(move || {
            for answer in [json!({}), json!({"stopping":true})] {
                let request = server
                    .recv_timeout(Duration::from_secs(10))
                    .unwrap()
                    .unwrap();
                request
                    .respond(Response::from_string(answer.to_string()))
                    .unwrap();
            }
            thread::sleep(Duration::from_millis(400));
            drop(holding);
        });
        let mut told = Vec::new();
        let started = Instant::now();
        let answer = stop_in(
            dir.path(),
            false,
            Duration::from_secs(20),
            Duration::from_secs(1),
            |pid| told.push(pid),
        )
        .unwrap();
        interpreter.join().unwrap();
        assert!(started.elapsed() >= Duration::from_millis(400));
        assert_eq!(answer["stopping"], true);
        assert_eq!(answer["stopped"], true);
        assert_eq!(told, [Some(std::process::id())]);
        // Nothing holds it now.
        let error = format!(
            "{:#}",
            stop_in(dir.path(), false, Duration::ZERO, Duration::ZERO, |_| {}).unwrap_err()
        );
        assert!(
            error.contains("interpreter is not running; run silicon connect YAML"),
            "{error}"
        );
    }

    #[test]
    fn stop_that_times_out_names_the_holder_and_the_way_out() {
        let dir = tempfile::tempdir().unwrap();
        let holding = lock(&dir.path().join("daemon.lock"), true).unwrap();
        record_holder(&holding);
        fs::write(dir.path().join("daemon.log"), "before\n").unwrap();
        let writer = {
            let log = dir.path().join("daemon.log");
            thread::spawn(move || {
                thread::sleep(Duration::from_millis(100));
                let mut file = OpenOptions::new().append(true).open(log).unwrap();
                writeln!(file, "still unhooking Ting").unwrap();
            })
        };
        let error = format!(
            "{:#}",
            stop_in(
                dir.path(),
                false,
                Duration::from_millis(600),
                Duration::ZERO,
                |_| {}
            )
            .unwrap_err()
        );
        writer.join().unwrap();
        drop(holding);
        assert!(
            error.starts_with(&format!(
                "the interpreter (pid {}) still holds {} 0s after it was asked to stop (the request failed: read ",
                std::process::id(),
                dir.path().join("daemon.lock").display()
            )),
            "{error}"
        );
        assert!(
            error.contains("still unhooking Ting") && !error.contains("before\n"),
            "{error}"
        );
        assert!(
            error.ends_with("run `silicon stop --force` to terminate it"),
            "{error}"
        );
    }

    #[test]
    #[ignore = "a daemon.lock holder, run in a child process by the stop tests"]
    fn lock_holder_child() {
        let dir = PathBuf::from(std::env::var_os("SILICON_TEST_DIR").unwrap());
        let holding = lock(&dir.join("daemon.lock"), true).unwrap();
        record_holder(&holding);
        if std::env::var_os("SILICON_TEST_IGNORE_TERM").is_some() {
            unsafe {
                libc::signal(libc::SIGTERM, libc::SIG_IGN);
            }
        }
        fs::write(dir.join("locked"), "").unwrap();
        thread::sleep(Duration::from_secs(60));
    }

    fn holder_process(dir: &Path, ignore_term: bool) -> std::process::Child {
        use std::os::unix::process::CommandExt;
        let _ = fs::remove_file(dir.join("locked"));
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "server::tests::lock_holder_child",
                "--exact",
                "--ignored",
                "--nocapture",
            ])
            .env("SILICON_TEST_DIR", dir)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            // Its own group, as an interpreter started by connect has.
            .process_group(0);
        if ignore_term {
            command.env("SILICON_TEST_IGNORE_TERM", "1");
        }
        let child = command.spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(30);
        while !dir.join("locked").exists() {
            assert!(Instant::now() < deadline, "the lock holder never locked");
            thread::sleep(Duration::from_millis(20));
        }
        child
    }

    #[test]
    fn stop_force_signals_only_the_lock_holder_and_escalates_to_sigkill() {
        use std::os::unix::process::ExitStatusExt;
        let dir = tempfile::tempdir().unwrap();
        let mut holder = holder_process(dir.path(), false);
        let answer = stop_in(
            dir.path(),
            true,
            Duration::ZERO,
            Duration::from_secs(10),
            |_| {},
        )
        .unwrap();
        assert_eq!(answer["signal"], "SIGTERM");
        assert_eq!(answer["pid"], holder.id());
        assert_eq!(holder.wait().unwrap().signal(), Some(libc::SIGTERM));
        let mut stubborn = holder_process(dir.path(), true);
        let answer = stop_in(
            dir.path(),
            true,
            Duration::ZERO,
            Duration::from_millis(500),
            |_| {},
        )
        .unwrap();
        assert_eq!(answer["signal"], "SIGKILL");
        assert_eq!(stubborn.wait().unwrap().signal(), Some(libc::SIGKILL));
        let error = format!(
            "{:#}",
            stop_in(dir.path(), true, Duration::ZERO, Duration::ZERO, |_| {}).unwrap_err()
        );
        assert!(
            error.starts_with("interpreter is not running: no process holds "),
            "{error}"
        );
    }

    #[test]
    #[ignore = "run by daemon_log_moves_to_a_fresh_file_at_its_cap, whose output it takes over"]
    fn daemon_log_child() {
        let log = PathBuf::from(std::env::var_os("SILICON_TEST_LOG").unwrap());
        redirect_output(&log).unwrap();
        crate::stderr_line("first line");
        assert!(same_file(2, &log) && same_file(1, &log));
        assert!(!keep_log(&log, 1 << 20).unwrap(), "under its cap it stays");
        crate::stderr_line(&"x".repeat(100));
        assert!(keep_log(&log, 64).unwrap());
        crate::stderr_line("after rotation");
        crate::stdout_line("stdout after rotation");
        fs::remove_file(&log).unwrap();
        assert!(
            keep_log(&log, 1 << 20).unwrap(),
            "a deleted log is recreated"
        );
        crate::stderr_line("after deletion");
    }

    #[test]
    fn daemon_log_moves_to_a_fresh_file_at_its_cap() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("daemon.log");
        let output = child("daemon_log_child", &[("SILICON_TEST_LOG", log.as_os_str())]);
        let current = fs::read_to_string(&log).unwrap();
        assert!(output.status.success(), "{current}");
        let rotated = fs::read_to_string(crate::numbered(&log, "1")).unwrap();
        assert!(
            rotated.starts_with("first line\n") && rotated.contains(&"x".repeat(100)),
            "{rotated}"
        );
        assert!(!rotated.contains("after"), "{rotated}");
        assert!(current.starts_with("after deletion\n"), "{current}");
        assert!(!crate::numbered(&log, "2").exists());
    }

    #[test]
    fn raising_the_open_file_limit_never_lowers_it() {
        let read = || {
            let mut limit = libc::rlimit {
                rlim_cur: 0,
                rlim_max: 0,
            };
            assert_eq!(
                unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) },
                0
            );
            limit
        };
        let before = read();
        let raised = raise_open_files().unwrap();
        let after = read();
        assert!(after.rlim_cur >= before.rlim_cur);
        assert!(after.rlim_cur <= before.rlim_cur.max(65536));
        if let Some((from, to)) = raised {
            assert_eq!((from, to), (before.rlim_cur, after.rlim_cur));
        }
        assert_eq!(raise_open_files().unwrap(), None);
    }

    /// Where a release's executable resolves to under a managed prefix.
    fn release(version: &str) -> PathBuf {
        PathBuf::from(format!(
            "/prefix/lib/silicon/releases/{version}/bin/silicon"
        ))
    }

    #[test]
    fn a_new_release_that_fails_its_check_keeps_this_one_and_is_checked_hourly() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved_home(dir.path(), "a");
        let app = test_app(dir.path(), vec![a.clone()], false);
        let now = Utc::now();
        let installed = || Ok(release("5.1.0"));
        app.restart.requested.store(true, Ordering::SeqCst);
        restart_tick(&app, now, installed, |_| Err(anyhow!("exec format error")));
        assert!(!app.restart.requested.load(Ordering::SeqCst));
        assert!(app.restart.ready().is_none());
        restart_tick(&app, now + TimeDelta::minutes(30), installed, |_| {
            panic!("checked again within the hour")
        });
        restart_tick(&app, now + TimeDelta::minutes(61), installed, |_| {
            Err(anyhow!("exec format error"))
        });
        let logged = errors_logged(&a.home);
        assert_eq!(logged.len(), 1, "the same failure twice: {logged:?}");
        assert!(
            logged[0].contains("an installed update was not applied")
                && logged[0].ends_with(": exec format error]"),
            "{logged:?}"
        );
        // Asked for again (`silicon update`): checked at the next round, and it passes.
        control(&app, &json!({"action":"restart","args":{}})).unwrap();
        let passed = now + TimeDelta::minutes(62);
        restart_tick(&app, passed, installed, |checked| {
            assert_eq!(checked, release("5.1.0"));
            Ok(())
        });
        assert_eq!(app.restart.ready(), Some(release("5.1.0")));
        // Waiting for idle is said once a day, not every round.
        for hours in [23, 25, 26] {
            restart_tick(&app, passed + TimeDelta::hours(hours), installed, |_| {
                panic!("checked again")
            });
        }
        let logged = errors_logged(&a.home);
        assert_eq!(logged.len(), 2, "{logged:?}");
        assert!(
            logged[1].contains("an installed update has been waiting since"),
            "{logged:?}"
        );
    }

    #[test]
    fn a_release_installed_after_the_check_is_checked_before_it_can_run() {
        let dir = tempfile::tempdir().unwrap();
        let app = test_app(dir.path(), Vec::new(), false);
        let now = Utc::now();
        app.restart.requested.store(true, Ordering::SeqCst);
        restart_tick(&app, now, || Ok(release("5.1.0")), |_| Ok(()));
        assert_eq!(app.restart.ready(), Some(release("5.1.0")));
        restart_tick(
            &app,
            now + TimeDelta::seconds(5),
            || Ok(release("5.1.0")),
            |_| panic!("checked again while nothing changed"),
        );
        // A second `silicon update` installed 5.1.1 and repointed `current` while the restart
        // waited for idle: 5.1.0 passing says nothing about it, so it is checked first.
        let mut tried = Vec::new();
        restart_tick(
            &app,
            now + TimeDelta::seconds(10),
            || Ok(release("5.1.1")),
            |executable| {
                tried.push(executable.to_path_buf());
                Err(anyhow!("5.1.1 crashes at startup"))
            },
        );
        assert_eq!(tried, [release("5.1.1")]);
        assert_eq!(app.restart.ready(), None);
        // Its `restart` request checks again at once, even within the hour.
        control(&app, &json!({"action":"restart","args":{}})).unwrap();
        let mut tried = Vec::new();
        restart_tick(
            &app,
            now + TimeDelta::seconds(15),
            || Ok(release("5.1.1")),
            |executable| {
                tried.push(executable.to_path_buf());
                Ok(())
            },
        );
        assert_eq!(tried, [release("5.1.1")]);
        assert_eq!(app.restart.ready(), Some(release("5.1.1")));
        // A `restart` request drops a check that already passed: it runs again first.
        control(&app, &json!({"action":"restart","args":{}})).unwrap();
        assert_eq!(app.restart.ready(), None);
    }

    #[test]
    fn an_update_restart_falls_back_to_the_release_it_runs_not_the_symlink_that_started_it() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let prefix = dir.path().canonicalize().unwrap();
        for version in ["5.0.2", "5.1.0"] {
            let bin = prefix.join(format!("lib/silicon/releases/{version}/bin"));
            fs::create_dir_all(&bin).unwrap();
            fs::write(bin.join("silicon"), "").unwrap();
        }
        let current = prefix.join("lib/silicon/current");
        symlink("releases/5.0.2", &current).unwrap();
        fs::create_dir_all(prefix.join("bin")).unwrap();
        let stable = prefix.join("bin/silicon");
        symlink("../lib/silicon/current/bin/silicon", &stable).unwrap();
        // A service or PATH start runs the stable symlink, and macOS reports that path.
        let running = remembered(Ok(stable.clone())).unwrap();
        assert_eq!(
            running,
            prefix.join("lib/silicon/releases/5.0.2/bin/silicon")
        );
        // The update repoints `current`; the target is what the symlink resolves to now.
        fs::remove_file(&current).unwrap();
        symlink("releases/5.1.0", &current).unwrap();
        let target = resolve_target(Ok(prefix.clone()), Some(running.clone())).unwrap();
        assert_eq!(
            target,
            prefix.join("lib/silicon/releases/5.1.0/bin/silicon")
        );
        // Two distinct executables: when the new one will not start, the running one does.
        let candidates = exec_candidates(Some(target.clone()), Some(running.clone()));
        assert_eq!(candidates[..2], [target, running.clone()]);
        // A source build restarts into itself.
        assert_eq!(
            resolve_target(Err(anyhow!("not managed")), Some(running.clone())).unwrap(),
            running
        );
        let error = format!(
            "{:#}",
            resolve_target(Err(anyhow!("not managed")), None).unwrap_err()
        );
        assert!(
            error.starts_with("no executable to restart into: ") && error.ends_with("not managed"),
            "{error}"
        );
        // One that no longer resolves is remembered as it is.
        let gone = prefix.join("gone/silicon");
        assert_eq!(remembered(Ok(gone.clone())), Some(gone));
        assert_eq!(remembered(Err(std::io::Error::other("unknown"))), None);
    }

    #[test]
    fn routing_that_cannot_start_is_retried_with_backoff_and_never_fails_a_caller() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved_home(dir.path(), "a");
        let mut broken = saved(dir.path(), "broken");
        broken.host = "not a host".into();
        let app = test_app(dir.path(), vec![a.clone(), broken], true);
        let now = Utc::now();
        let refuse = |hosts: &[String]| -> Result<Box<dyn Router>> {
            // Saved hosts are routed, except one Caddy would refuse.
            assert_eq!(hosts, ["a.org.localhost"]);
            Err(anyhow!("port 80 is in use"))
        };
        supervise_proxy(&app, now, refuse);
        let state = app.routing.lock().recover().state();
        assert!(
            state.starts_with("down: could not start the local routing proxy")
                && state.ends_with("port 80 is in use"),
            "{state}"
        );
        // Not again before its backoff: 1 s, then 2 s.
        supervise_proxy(&app, now + TimeDelta::milliseconds(500), |_| {
            panic!("started before its backoff")
        });
        supervise_proxy(&app, now + TimeDelta::seconds(1), refuse);
        supervise_proxy(&app, now + TimeDelta::milliseconds(2500), |_| {
            panic!("started before its backoff")
        });
        supervise_proxy(&app, now + TimeDelta::seconds(3), refuse);
        assert_eq!(app.routing.lock().recover().failures, 3);
        // A caller gets a warning, not a failure.
        let warning = update_proxy(&app).unwrap();
        assert!(
            warning
                .starts_with("local routing is down and is restored automatically; last error: ")
                && warning.ends_with("port 80 is in use"),
            "{warning}"
        );
        let ping = control(&app, &json!({"action":"ping","args":{}})).unwrap();
        assert!(ping["proxy"].as_str().unwrap().starts_with("down: "));
        // The same failure is reported once.
        assert_eq!(errors_logged(&a.home).len(), 1);
        // --no-proxy: nothing to supervise.
        let disabled = test_app(dir.path(), Vec::new(), false);
        supervise_proxy(&disabled, now, |_| panic!("started with --no-proxy"));
        assert_eq!(update_proxy(&disabled), None);
    }

    /// Stands in for Caddy: counts how often it is stopped (dropped). An unhealthy one fails
    /// its health check; a slow one says when its stop began and finishes it when told to.
    struct FakeRouter {
        stopped: Arc<AtomicUsize>,
        healthy: bool,
        slow: Option<(std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>)>,
    }

    fn fake(stopped: &Arc<AtomicUsize>) -> Box<dyn Router> {
        Box::new(FakeRouter {
            stopped: stopped.clone(),
            healthy: true,
            slow: None,
        })
    }

    impl Router for FakeRouter {
        fn update(&mut self, _: &[String]) -> Result<()> {
            if self.healthy {
                Ok(())
            } else {
                Err(anyhow!("Caddy exited: signal: 9 (SIGKILL)"))
            }
        }
    }

    impl Drop for FakeRouter {
        fn drop(&mut self) {
            if let Some((began, finish)) = self.slow.take() {
                began.send(()).unwrap();
                finish.recv().unwrap();
            }
            self.stopped.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn wait_for(what: &str, done: impl Fn() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !done() {
            assert!(Instant::now() < deadline, "{what} did not happen");
            thread::sleep(Duration::from_millis(10));
        }
    }

    /// Run `close` on its own thread; the flag says when it returned.
    fn closing(app: &Arc<App>) -> (thread::JoinHandle<()>, Arc<AtomicBool>) {
        let closed = Arc::new(AtomicBool::new(false));
        let (app, flag) = (app.clone(), closed.clone());
        let thread = thread::spawn(move || {
            close(&app);
            flag.store(true, Ordering::SeqCst);
        });
        (thread, closed)
    }

    #[test]
    fn a_caddy_started_or_stopped_while_the_interpreter_stops_is_gone_before_it_ends() {
        use std::sync::mpsc;
        let dir = tempfile::tempdir().unwrap();
        let app = test_app(dir.path(), Vec::new(), true);
        let stopped = Arc::new(AtomicUsize::new(0));
        let (started, starting) = mpsc::channel();
        let (finish, finishing) = mpsc::channel::<()>();
        let supervisor = {
            let (app, stopped) = (app.clone(), stopped.clone());
            thread::spawn(move || {
                supervise_proxy(&app, Utc::now(), |_| {
                    started.send(()).unwrap();
                    finishing.recv().unwrap();
                    Ok(fake(&stopped))
                })
            })
        };
        starting.recv().unwrap();
        let (stopping, closed) = closing(&app);
        wait_for("the stop", || app.runtime.stopping.load(Ordering::SeqCst));
        thread::sleep(Duration::from_millis(300));
        assert!(
            !closed.load(Ordering::SeqCst),
            "the stop finished while Caddy was still starting"
        );
        finish.send(()).unwrap();
        supervisor.join().unwrap();
        stopping.join().unwrap();
        assert_eq!(
            stopped.load(Ordering::SeqCst),
            1,
            "the Caddy started during the stop was left running"
        );
        assert!(app.proxy.lock().recover().is_none());
        assert!(!app.routing.lock().recover().up);
        // Once stopping, no Caddy is started at all.
        supervise_proxy(&app, Utc::now(), |_| panic!("started while stopping"));

        // Started before the stop: kept, and the stop stops it.
        let app = test_app(dir.path(), Vec::new(), true);
        let stopped = Arc::new(AtomicUsize::new(0));
        supervise_proxy(&app, Utc::now(), |_| Ok(fake(&stopped)));
        assert_eq!(app.routing.lock().recover().state(), "up");
        assert_eq!(stopped.load(Ordering::SeqCst), 0);
        close(&app);
        assert_eq!(stopped.load(Ordering::SeqCst), 1);

        // One that failed its health check is stopped outside the proxy lock; the stop waits
        // until it is gone.
        let app = test_app(dir.path(), Vec::new(), true);
        let stopped = Arc::new(AtomicUsize::new(0));
        let (began, beginning) = mpsc::channel();
        let (finish, finishing) = mpsc::channel();
        *app.proxy.lock().recover() = Some(Box::new(FakeRouter {
            stopped: stopped.clone(),
            healthy: false,
            slow: Some((began, finishing)),
        }));
        let supervisor = {
            let app = app.clone();
            thread::spawn(move || supervise_proxy(&app, Utc::now(), |_| panic!("started early")))
        };
        beginning.recv().unwrap();
        let (stopping, closed) = closing(&app);
        wait_for("the stop", || app.runtime.stopping.load(Ordering::SeqCst));
        thread::sleep(Duration::from_millis(300));
        assert!(
            !closed.load(Ordering::SeqCst),
            "the stop finished while a failed Caddy was still stopping"
        );
        finish.send(()).unwrap();
        supervisor.join().unwrap();
        stopping.join().unwrap();
        assert_eq!(stopped.load(Ordering::SeqCst), 1);
        assert!(app
            .routing
            .lock()
            .recover()
            .state()
            .ends_with("Caddy exited: signal: 9 (SIGKILL)"));
    }

    #[test]
    fn a_replaced_listener_is_kept_while_its_connections_are_in_use() {
        use std::collections::VecDeque;
        let start = Instant::now();
        let minutes = |m: u64| Duration::from_secs(m * 60);
        let next = |queue: &Mutex<VecDeque<u32>>| queue.lock().recover().pop_front();
        let mut retired = vec![
            (Mutex::new(VecDeque::from([1])), start),
            (Mutex::new(VecDeque::new()), start),
        ];
        let mut handled = Vec::new();
        drain_retired(&mut retired, start + minutes(9), next, |r| handled.push(r));
        assert_eq!(handled, [1]);
        assert_eq!(retired.len(), 2);
        // Ten minutes after both were replaced: the quiet one goes, the busy one stays.
        drain_retired(&mut retired, start + minutes(11), next, |r| handled.push(r));
        assert_eq!(retired.len(), 1);
        // A request just before it would be dropped is answered and keeps it.
        retired[0].0.lock().recover().push_back(2);
        let late = start + minutes(19) + Duration::from_secs(59);
        drain_retired(&mut retired, late, next, |r| handled.push(r));
        assert_eq!(handled, [1, 2]);
        assert_eq!(retired.len(), 1);
        drain_retired(&mut retired, late + minutes(9), next, |r| handled.push(r));
        assert_eq!(retired.len(), 1);
        // Idle for ten minutes: dropped.
        drain_retired(&mut retired, late + minutes(10), next, |r| handled.push(r));
        assert!(retired.is_empty());
        assert_eq!(handled, [1, 2]);
    }

    #[test]
    #[ignore = "run by hang_ups_never_cut_a_stop_short_and_nohup_is_kept, which reads its output"]
    fn signals_child() {
        let nohup = std::env::var_os("SILICON_TEST_NOHUP").is_some();
        // Explicit either way: the test run itself may have been started under nohup.
        unsafe {
            libc::signal(
                libc::SIGHUP,
                if nohup { libc::SIG_IGN } else { libc::SIG_DFL },
            );
        }
        let dir = tempfile::tempdir().unwrap();
        let stop = Arc::new(Stop::default());
        let listener = listen(stop.clone(), dir.path().to_path_buf()).unwrap();
        unsafe {
            libc::raise(libc::SIGHUP);
        }
        if nohup {
            thread::sleep(Duration::from_millis(500));
            assert!(
                !stop.signalled.load(Ordering::SeqCst),
                "nohup's SIG_IGN was overridden"
            );
            assert!(ignored(libc::SIGHUP));
            listener.close();
            return;
        }
        wait_for("the first hang-up", || {
            stop.signalled.load(Ordering::SeqCst)
        });
        // The terminal hangs up again: noted, the orderly stop goes on.
        unsafe {
            libc::raise(libc::SIGHUP);
        }
        thread::sleep(Duration::from_millis(500));
        crate::stderr_line("still stopping after the second hang-up");
        // A person insists: exit at once, with status 0.
        unsafe {
            libc::raise(libc::SIGTERM);
        }
        thread::sleep(Duration::from_secs(10));
        panic!("a SIGTERM while stopping did not exit");
    }

    #[test]
    fn hang_ups_never_cut_a_stop_short_and_nohup_is_kept() {
        let output = child("signals_child", &[]);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(output.status.success(), "{stderr}");
        let order = [
            "received SIGHUP; stopping the interpreter",
            "received SIGHUP again while stopping; the orderly stop continues",
            "still stopping after the second hang-up",
            "received SIGTERM again while stopping; exiting now",
        ];
        let mut from = 0;
        for line in order {
            let at = stderr[from..]
                .find(line)
                .unwrap_or_else(|| panic!("{line:?} missing or out of order in:\n{stderr}"));
            from += at + line.len();
        }
        let output = child("signals_child", &[("SILICON_TEST_NOHUP", "1".as_ref())]);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(output.status.success(), "{stderr}");
        assert!(!stderr.contains("received SIGHUP"), "{stderr}");
    }

    #[test]
    fn ting_registration_never_starts_once_the_stop_began() {
        use std::sync::mpsc;
        let dir = tempfile::tempdir().unwrap();
        let app = test_app(dir.path(), Vec::new(), false);
        // It runs holding the Silicon's Ting lock, which the stop waits on.
        let registered = register_unless_stopping(&app, "si:a", || {
            assert!(matches!(
                ting_lock(&app, "si:a").try_lock(),
                Err(TryLockError::WouldBlock)
            ));
            Ok(())
        });
        assert!(matches!(registered, Some(Ok(()))));
        // A registration under way is waited for before hooks are removed.
        let (inside, entered) = mpsc::channel();
        let (finish, finishing) = mpsc::channel::<()>();
        let registering = {
            let app = app.clone();
            thread::spawn(move || {
                register_unless_stopping(&app, "si:a", || {
                    inside.send(()).unwrap();
                    finishing.recv().unwrap();
                    Ok(())
                })
                .is_some()
            })
        };
        entered.recv().unwrap();
        let (stopping, closed) = closing(&app);
        wait_for("the stop", || app.runtime.stopping.load(Ordering::SeqCst));
        thread::sleep(Duration::from_millis(300));
        assert!(
            !closed.load(Ordering::SeqCst),
            "hooks removed mid-registration"
        );
        finish.send(()).unwrap();
        assert!(registering.join().unwrap());
        stopping.join().unwrap();
        // Stopping already, even for a Silicon the stop found no Ting lock for: never started.
        assert!(
            register_unless_stopping(&app, "si:new", || panic!("registered while stopping"))
                .is_none()
        );
    }

    #[test]
    fn refused_deliveries_are_logged_once_per_restore_attempt_without_the_error() {
        let dir = tempfile::tempdir().unwrap();
        let a = saved(dir.path(), "a");
        let app = test_app(dir.path(), vec![a.clone()], false);
        let note = refusal_note(&app, &a.host).unwrap();
        assert!(
            note.contains("a.org.localhost with HTTP 503 while si:a is being restored"),
            "{note}"
        );
        assert_eq!(refusal_note(&app, &a.host), None);
        let error = format!("compile failed:\n{}", "x".repeat(4096));
        let failed = |app: &App| {
            app.registry
                .lock()
                .recover()
                .failed(&a.id, error.clone(), Utc::now());
        };
        failed(&app);
        let note = refusal_note(&app, &a.host).unwrap();
        assert!(
            note.contains("until si:a is restored: restore attempt 1 failed")
                && !note.contains("xxx"),
            "{note}"
        );
        // Ting retries every 30 seconds: nothing more until the next attempt has run.
        assert_eq!(refusal_note(&app, &a.host), None);
        app.registry
            .lock()
            .recover()
            .entry_mut(&a.id)
            .unwrap()
            .state = Restore::Restoring;
        assert_eq!(refusal_note(&app, &a.host), None);
        failed(&app);
        assert!(refusal_note(&app, &a.host)
            .unwrap()
            .contains("restore attempt 2 failed"));
        assert_eq!(refusal_note(&app, "other.org.localhost"), None);
    }

    #[test]
    fn a_registry_that_cannot_be_saved_is_reported_once_per_cause() {
        let dir = tempfile::tempdir().unwrap();
        let app = test_app(dir.path(), vec![saved(dir.path(), "a")], false);
        // A directory where connections.json belongs: each write fails at its rename.
        let file = dir.path().join("connections.json");
        fs::create_dir(&file).unwrap();
        app.registry.lock().recover().version += 1;
        // Each attempt's error names its own staged file.
        let first = format!("{:#}", persist(&app).unwrap_err());
        let second = format!("{:#}", persist(&app).unwrap_err());
        assert_ne!(first, second);
        assert!(save_pending(&app), "the first failure is not reported");
        assert!(!save_pending(&app), "the same cause, reported again");
        assert!(!save_pending(&app), "the same cause, reported again");
        fs::remove_dir(&file).unwrap();
        assert!(save_pending(&app), "the recovery is not said");
        assert!(!save_pending(&app));
        let saved: Vec<Connection> = serde_json::from_slice(&fs::read(&file).unwrap()).unwrap();
        assert_eq!(saved.len(), 1);
    }
}
