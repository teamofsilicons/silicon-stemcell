//! Hourly GitHub releases, installed through the same verified bundle path as curl + sh.
//!
//! The interpreter runs for months, so the updater keeps its schedule and its failures in
//! `update-state.json` by wall clock: restarts, sleep and a clock set back neither skip
//! checks nor hammer GitHub, a release that keeps failing is retried with backoff and
//! reported once plus a daily line, and a release that needs a person waits for one.
use crate::Recover;
use crate::{runtime::Runtime, state};
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, TimeDelta, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, OnceLock, Weak,
};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

/// install.sh's exit status when only a person can finish the installation (sudo for
/// Caddy's port 80). Retrying cannot help, so the updater waits for them.
const NEEDS_PERSON: i32 = 77;
/// How long install.sh may run unattended; `SILICON_UPDATE_TIMEOUT_SECS` overrides it.
const INSTALL_LIMIT: Duration = Duration::from_secs(30 * 60);
/// How long install.sh gets to clean up (its lock, its staging directory) after SIGTERM.
const GRACE: Duration = Duration::from_secs(5);
/// How often the updater looks at the wall clock.
const TICK_SECONDS: u64 = 60;
const HOUR: TimeDelta = TimeDelta::hours(1);
const DAY: TimeDelta = TimeDelta::days(1);
/// The longest wait before retrying a release that failed to install.
const MAX_BACKOFF_HOURS: u64 = 7 * 24;

pub(crate) fn version(tag: &str) -> Option<[u64; 3]> {
    let parts: Vec<_> = tag.strip_prefix('v').unwrap_or(tag).split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    Some([
        parts[0].parse().ok()?,
        parts[1].parse().ok()?,
        parts[2].parse().ok()?,
    ])
}

/// This build's version. A build whose version is not x.y.z cannot compare itself with
/// releases, so it is not updated.
fn running() -> Result<[u64; 3]> {
    version(env!("CARGO_PKG_VERSION")).ok_or_else(|| {
        anyhow!(
            "this build's version {} is not x.y.z, so it cannot compare itself with releases",
            env!("CARGO_PKG_VERSION")
        )
    })
}

/// A managed installation, resolved once. Installs remove old releases, and a restart must
/// still find the prefix after the release this process started from is gone.
struct Managed {
    prefix: PathBuf,
    /// The release directory holding this executable.
    release: PathBuf,
}

static MANAGED: OnceLock<Managed> = OnceLock::new();

fn managed() -> Result<&'static Managed> {
    if let Some(managed) = MANAGED.get() {
        return Ok(managed);
    }
    let executable = std::env::current_exe()
        .and_then(|path| path.canonicalize())
        .context("cannot locate the running silicon executable")?;
    let release = executable
        .parent()
        .and_then(|p| p.parent())
        .with_context(|| {
            format!(
                "executable path {} has no release directory",
                executable.display()
            )
        })?;
    let marker = release.join("PREFIX");
    let named = fs::read_to_string(&marker).with_context(|| {
        format!(
            "{} is not a managed bundle installation: cannot read {}",
            executable.display(),
            marker.display()
        )
    })?;
    let named = PathBuf::from(named.trim());
    let prefix = named.canonicalize().with_context(|| {
        format!(
            "{} names prefix {}, which cannot be resolved",
            marker.display(),
            named.display()
        )
    })?;
    if !executable.starts_with(prefix.join("lib/silicon/releases")) {
        bail!(
            "{} names prefix {}, which does not own this executable {}",
            marker.display(),
            prefix.display(),
            executable.display()
        );
    }
    let release = release.to_path_buf();
    Ok(MANAGED.get_or_init(|| Managed { prefix, release }))
}

pub fn managed_prefix() -> Result<PathBuf> {
    managed().map(|managed| managed.prefix.clone())
}

fn repository() -> Result<String> {
    let repository = std::env::var("SILICON_REPOSITORY")
        .unwrap_or_else(|_| "teamofsilicons/silicon-stemcell".into());
    if repository.split('/').count() != 2
        || !repository
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-._/".contains(&c))
    {
        bail!("invalid SILICON_REPOSITORY {repository:?}; expected owner/name");
    }
    Ok(repository)
}

fn latest_url(repository: &str) -> String {
    format!("https://api.github.com/repos/{repository}/releases/latest")
}

/// Returns the new executable after the complete bundle has been verified and activated,
/// or the installed one when a newer release is already there (a restart is all it takes).
pub fn install_latest() -> Result<Option<PathBuf>> {
    let managed = managed().context("updates require a managed bundle installation")?;
    let repository = repository()?;
    let url = latest_url(&repository);
    let answer = fetch(&url, None).map_err(|unasked| unasked.error)?;
    let release = release(&url, answer.status, &answer.body)?;
    let latest = stable(&url, &release, &answer.body)?;
    let executable = managed.prefix.join("bin/silicon");
    match decide(
        running()?,
        &installed(&managed.prefix, &managed.release),
        latest,
    ) {
        Decision::Current => Ok(None),
        Decision::Restart(_) => Ok(Some(executable)),
        Decision::Install(tag) => {
            // A person runs it and can stop it; nothing else does.
            run_installer(managed, &repository, &tag, false, &|| false)
                .map_err(|error| for_a_person(error, &executable, &repository, &tag, in_wsl()))?;
            Ok(Some(executable))
        }
    }
}

/// One answer from GitHub's latest-release endpoint.
struct Answer {
    status: ureq::http::StatusCode,
    etag: Option<String>,
    retry_after: Option<String>,
    remaining: Option<String>,
    reset: Option<String>,
    body: String,
}

/// GitHub could not be asked at all. `offline` when the network is missing (no DNS, no
/// connection, a timeout), which is ordinary on a laptop for a while; TLS, proxy and
/// protocol failures are not, and are shown at once.
struct Unasked {
    error: anyhow::Error,
    offline: bool,
}

impl Unasked {
    fn new(error: ureq::Error, what: String) -> Self {
        Unasked {
            offline: offline(&error),
            error: anyhow::Error::new(error).context(what),
        }
    }
}

fn offline(error: &ureq::Error) -> bool {
    use std::io::ErrorKind::*;
    match error {
        ureq::Error::HostNotFound | ureq::Error::ConnectionFailed | ureq::Error::Timeout(_) => true,
        // Name lookups fail uncategorized; refused, reset, unreachable and the like are the
        // network too. A permission or a malformed answer is not.
        ureq::Error::Io(error) => !matches!(
            error.kind(),
            PermissionDenied | InvalidInput | InvalidData | Unsupported | OutOfMemory
        ),
        _ => false,
    }
}

fn fetch(url: &str, etag: Option<&str>) -> std::result::Result<Answer, Unasked> {
    // Statuses are read here, not raised by ureq, so GitHub's own explanation survives.
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(30)))
        .http_status_as_error(false)
        .build()
        .new_agent();
    let mut request = agent
        .get(url)
        .header("User-Agent", concat!("silicon/", env!("CARGO_PKG_VERSION")))
        .header("Accept", "application/vnd.github+json");
    if let Some(etag) = etag {
        // A 304 answer costs nothing against GitHub's rate limit.
        request = request.header("If-None-Match", etag);
    }
    let mut response = request
        .call()
        .map_err(|error| Unasked::new(error, format!("cannot reach GitHub at {url}")))?;
    let status = response.status();
    let header = |name: &str| {
        response
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned)
    };
    let (etag, retry_after, remaining, reset) = (
        header("etag"),
        header("retry-after"),
        header("x-ratelimit-remaining"),
        header("x-ratelimit-reset"),
    );
    let body = response.body_mut().read_to_string().map_err(|error| {
        Unasked::new(
            error,
            format!("cannot read GitHub's {status} answer from {url}"),
        )
    })?;
    Ok(Answer {
        status,
        etag,
        retry_after,
        remaining,
        reset,
        body,
    })
}

/// GitHub's own words travel with any failure: rate limits and outages explain themselves.
fn release(url: &str, status: ureq::http::StatusCode, body: &str) -> Result<Value> {
    if !status.is_success() {
        bail!("GitHub answered {url} with HTTP {status}:\n{body}");
    }
    serde_json::from_str(body)
        .map_err(|error| anyhow!("GitHub answered {url} with invalid JSON ({error}):\n{body}"))
}

/// The tag of a stable release, or None for a draft or prerelease.
fn stable<'a>(url: &str, release: &'a Value, body: &str) -> Result<Option<&'a str>> {
    let tag = release["tag_name"]
        .as_str()
        .ok_or_else(|| anyhow!("GitHub release from {url} has no tag_name:\n{body}"))?;
    // Drafts and prereleases are not updates; an answer without the flags is malformed.
    let flag = |name: &str| {
        release[name]
            .as_bool()
            .ok_or_else(|| anyhow!("GitHub release from {url} has no boolean {name}:\n{body}"))
    };
    if flag("draft")? || flag("prerelease")? {
        return Ok(None);
    }
    version(tag).ok_or_else(|| {
        anyhow!("release tag {tag:?} from {url} is not a stable semantic version")
    })?;
    Ok(Some(tag))
}

/// The tag of a stable release newer than this build, or None when there is no update.
#[cfg(test)]
fn newer<'a>(url: &str, release: &'a Value, body: &str) -> Result<Option<&'a str>> {
    let running = running()?;
    Ok(stable(url, release, body)?.filter(|tag| version(tag).is_some_and(|v| v > running)))
}

/// What `current` names: its VERSION, and whether it is the release this process runs.
#[derive(Clone, Debug, Default, PartialEq)]
struct Installed {
    version: Option<String>,
    running: bool,
}

fn installed(prefix: &Path, release: &Path) -> Installed {
    let runtime = prefix.join("lib/silicon");
    let version = fs::read_to_string(runtime.join("current/VERSION"))
        .ok()
        .map(|version| version.trim().to_owned())
        .filter(|version| !version.is_empty());
    let active = fs::read_link(runtime.join("current"))
        .ok()
        .and_then(|target| target.file_name().map(OsString::from));
    Installed {
        version,
        running: active.is_some() && active.as_deref() == release.file_name(),
    }
}

#[derive(Debug, PartialEq)]
enum Decision {
    Current,
    /// A newer release is already installed; restarting runs it.
    Restart(String),
    Install(String),
}

/// What is installed decides, not only what runs: after `silicon update` the new release
/// is already active, and downloading it again would only store a second copy. The release
/// this process runs is never "newer than itself", so a VERSION marker that disagrees with
/// its own binary cannot cause a restart loop.
fn decide(running: [u64; 3], installed: &Installed, latest: Option<&str>) -> Decision {
    let newer_installed = installed
        .version
        .as_deref()
        .and_then(version)
        .filter(|found| !installed.running && *found > running);
    let baseline = newer_installed.unwrap_or(running);
    if let Some(tag) = latest.filter(|tag| version(tag).is_some_and(|found| found > baseline)) {
        return Decision::Install(tag.to_owned());
    }
    match (newer_installed, &installed.version) {
        (Some(_), Some(release)) => Decision::Restart(release.clone()),
        _ => Decision::Current,
    }
}

/// A run of failures, until something succeeds. Errors are known by a digest that ignores
/// what changes on every run (staging directory names, numbers), so a repeat is recognized.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
struct Trouble {
    failures: u32,
    last_error_digest: Option<String>,
    failing_since: Option<DateTime<Utc>>,
    last_report: Option<DateTime<Utc>>,
    /// Errors already shown in full during this run; errors that alternate are not
    /// shown again each time they come back.
    reported_digests: Vec<String>,
}

#[derive(Debug, PartialEq)]
enum Tell {
    Nothing,
    Full,
    Daily,
}

/// How long a run of failures goes untold. A laptop without network is ordinary, a day of
/// it is not; attempts count as well as time, so a day spent asleep is not a day offline.
#[derive(Clone, Copy, Debug)]
struct Quiet {
    time: TimeDelta,
    attempts: u32,
}

/// Told at once.
const LOUD: Quiet = Quiet {
    time: TimeDelta::zero(),
    attempts: 0,
};
/// No network: told after a day with at least twelve failed hourly checks.
const OFFLINE: Quiet = Quiet {
    time: DAY,
    attempts: 12,
};

impl Trouble {
    /// Count one more failure and say what to tell: the first one and every error not yet
    /// shown in full, otherwise at most one line a day, all held back while `quiet` lasts.
    fn failed(&mut self, digest: &str, now: DateTime<Utc>, quiet: Quiet) -> Tell {
        self.failures = self.failures.saturating_add(1);
        self.last_error_digest = Some(digest.to_owned());
        // A clock set back must not silence the next line for as long as it moved.
        let since = self
            .failing_since
            .filter(|since| *since <= now)
            .unwrap_or(now);
        self.failing_since = Some(since);
        if self.last_report.is_some_and(|at| at > now) {
            self.last_report = Some(now);
        }
        let tell = if now - since < quiet.time || self.failures < quiet.attempts {
            Tell::Nothing
        } else if !self.reported_digests.iter().any(|told| told == digest) {
            if self.reported_digests.len() >= 16 {
                self.reported_digests.remove(0);
            }
            self.reported_digests.push(digest.to_owned());
            Tell::Full
        } else if self.last_report.is_none_or(|at| now - at >= DAY) {
            Tell::Daily
        } else {
            Tell::Nothing
        };
        if tell != Tell::Nothing {
            self.last_report = Some(now);
        }
        tell
    }

    /// End the run; the trouble it was, when it had been told, so its end is told too.
    fn cleared(&mut self) -> Option<Trouble> {
        let trouble = std::mem::take(self);
        trouble.last_report.is_some().then_some(trouble)
    }
}

/// Numbers in an error are mostly what changes between runs (process ids, byte counts,
/// milliseconds, times), so they are left out, except the statuses that say what went
/// wrong: HTTP and curl codes, exit statuses, signals and OS error numbers. A 404 that
/// becomes a 403 is a new error.
fn digest(text: &str) -> String {
    static VOLATILE: OnceLock<regex::Regex> = OnceLock::new();
    let volatile = VOLATILE.get_or_init(|| {
        regex::Regex::new(
            r"(?P<status>(?i:exit status|exit code|signal|status|error|HTTP(?:/[0-9.]+)?)\s*:?\s*[0-9]+|curl: \([0-9]+\))|\.install\.[A-Za-z0-9]+|\.installer-[0-9A-Fa-f-]+\.sh|v[0-9]+\.[0-9]+\.[0-9]+-[A-Za-z0-9]+|[0-9]+",
        )
        .unwrap()
    });
    let kept = volatile.replace_all(text, |found: &regex::Captures| {
        found
            .name("status")
            .map_or(String::new(), |status| status.as_str().to_owned())
    });
    // FNV-1a: stable across releases, unlike std's hasher.
    let hash = kept.bytes().fold(0xcbf29ce484222325u64, |hash, byte| {
        (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3)
    });
    format!("{hash:016x}")
}

/// Everything the updater remembers across restarts, in `<interpreter dir>/update-state.json`.
/// `tag` is GitHub's latest stable release, as of `last_check` and its `etag`; `failures`
/// and the rest of the flattened trouble count failed installs of that tag.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
struct State {
    last_check: Option<DateTime<Utc>>,
    etag: Option<String>,
    tag: Option<String>,
    #[serde(flatten)]
    install: Trouble,
    next_attempt: Option<DateTime<Utc>>,
    /// A release that needs a person, and the installed VERSION when it did: it is not
    /// tried again until either changes.
    blocked_tag: Option<String>,
    blocked_version: Option<String>,
    /// GitHub's rate limit asked to wait until then.
    next_check: Option<DateTime<Utc>>,
    /// Failures to ask GitHub (or to read the settings), apart from failed installs.
    check: Trouble,
}

fn state_path() -> PathBuf {
    crate::server::directory().join("update-state.json")
}

/// The saved state, and why it had to start over when it could not be read.
fn load_state(path: &Path) -> (State, Option<String>) {
    match fs::read(path) {
        Ok(bytes) => match serde_json::from_slice(&bytes) {
            Ok(state) => (state, None),
            Err(error) => (
                State::default(),
                Some(format!(
                    "automatic update state {} is invalid ({error}), so its schedule and failure history start over; it held:\n{}",
                    path.display(),
                    String::from_utf8_lossy(&bytes)
                )),
            ),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (State::default(), None),
        Err(error) => (
            State::default(),
            Some(format!(
                "cannot read automatic update state {} ({error}), so its schedule and failure history start over",
                path.display()
            )),
        ),
    }
}

/// A uniformly random whole number of seconds in `low..=high`.
fn random_seconds(low: i64, high: i64) -> TimeDelta {
    let span = (high - low + 1).max(1) as u128;
    TimeDelta::seconds(low + (uuid::Uuid::new_v4().as_u128() % span) as i64)
}

/// An hour, give or take ten minutes, so many interpreters do not ask GitHub in step.
fn hourly(now: DateTime<Utc>) -> DateTime<Utc> {
    now + random_seconds(50 * 60, 70 * 60)
}

/// After a start: soon when the last check is old (a crash-looping or often restarted
/// daemon still checks), otherwise an hour after it. A last check in the future means the
/// clock was set back; it says nothing, so the check comes soon.
fn first_check(state: &State, now: DateTime<Utc>) -> DateTime<Utc> {
    let soon = now + random_seconds(5 * 60, 10 * 60);
    let mut at = match state.last_check {
        Some(last) if last <= now && now - last < HOUR => {
            hourly(last).max(now + TimeDelta::minutes(5))
        }
        _ => soon,
    };
    if let Some(until) = state
        .next_check
        .filter(|until| *until > at && *until <= now + DAY)
    {
        at = until;
    }
    at
}

/// How long to wait after the `failures`th failed install of one release.
fn backoff(failures: u32) -> TimeDelta {
    let hours = 1u64
        .checked_shl(failures.saturating_sub(1))
        .unwrap_or(u64::MAX)
        .min(MAX_BACKOFF_HOURS);
    TimeDelta::hours(hours as i64)
}

/// When GitHub's 403/429 answer says checks may resume: `retry-after` (seconds or an HTTP
/// date), or `x-ratelimit-reset` when the limit is spent. At most a day ahead, since the
/// local clock can be wrong.
fn limited_until(answer: &Answer, now: DateTime<Utc>) -> Option<DateTime<Utc>> {
    use ureq::http::StatusCode;
    if answer.status != StatusCode::FORBIDDEN && answer.status != StatusCode::TOO_MANY_REQUESTS {
        return None;
    }
    let retry = answer.retry_after.as_deref().and_then(|value| {
        let value = value.trim();
        match value.parse::<i64>() {
            Ok(seconds) => Some(now + TimeDelta::seconds(seconds.clamp(0, 86_400))),
            Err(_) => DateTime::parse_from_rfc2822(value)
                .ok()
                .map(|at| at.with_timezone(&Utc)),
        }
    });
    let reset = answer
        .reset
        .as_deref()
        .filter(|_| answer.remaining.as_deref().map(str::trim) == Some("0"))
        .and_then(|value| value.trim().parse::<i64>().ok())
        .and_then(|seconds| DateTime::from_timestamp(seconds, 0));
    let until = retry.max(reset)?;
    Some(until.clamp(now + TimeDelta::minutes(1), now + DAY) + random_seconds(0, 60))
}

/// What the updater needs from the world, so its decisions can be tested without it.
trait Io {
    fn auto_update(&mut self) -> Result<bool>;
    fn installed(&mut self) -> Installed;
    fn fetch(&mut self, etag: Option<&str>) -> std::result::Result<Answer, Unasked>;
    fn install(&mut self, tag: &str) -> Result<()>;
    /// What a person does when installing `tag` needs them.
    fn by_hand(&self, tag: &str) -> String;
}

/// A line for the logs: daemon.log only (`kind` None), or also each connected Silicon's.
#[derive(Debug)]
struct Said {
    kind: Option<&'static str>,
    text: String,
}

impl Said {
    fn error(text: String) -> Self {
        Said {
            kind: Some("error"),
            text,
        }
    }
    fn note(text: String) -> Self {
        Said {
            kind: Some("runtime"),
            text,
        }
    }
    fn daemon(text: String) -> Self {
        Said { kind: None, text }
    }
}

/// A newer release is installed and the interpreter waits to restart into it.
struct Ready {
    release: String,
    since: DateTime<Utc>,
    told: Option<DateTime<Utc>>,
}

impl Ready {
    /// Active work postpones the restart. After an hour that is said, then once a day.
    fn waiting(&mut self, now: DateTime<Utc>) -> Vec<Said> {
        if self.since > now {
            self.since = now;
        }
        if self.told.is_some_and(|told| told > now) {
            self.told = Some(now);
        }
        let due = match self.told {
            None => now - self.since >= HOUR,
            Some(told) => now - told >= DAY,
        };
        if !due {
            return Vec::new();
        }
        self.told = Some(now);
        vec![Said::note(format!(
            "automatic update: Silicon {} has been installed for {} hours (since {}), and the interpreter is still waiting for active work (dispatches, nested activity, pending provider work) to finish before it restarts into it",
            self.release,
            (now - self.since).num_hours(),
            self.since.to_rfc3339()
        ))]
    }
}

struct Updater {
    state: State,
    running: [u64; 3],
    url: String,
    /// The next check, and the wall-clock time it was planned at, to notice a clock set back.
    next: DateTime<Utc>,
    planned: DateTime<Utc>,
    ready: Option<Ready>,
}

impl Updater {
    fn new(state: State, running: [u64; 3], url: String, now: DateTime<Utc>) -> Self {
        let next = first_check(&state, now);
        Updater {
            state,
            running,
            url,
            next,
            planned: now,
            ready: None,
        }
    }

    /// One look at the clock. Checks GitHub when due; once a release is ready, only says
    /// how long the restart has waited.
    fn tick(&mut self, now: DateTime<Utc>, io: &mut impl Io) -> Vec<Said> {
        if now < self.planned - TimeDelta::minutes(1) {
            // The clock went back: keep the wait that was left rather than the date.
            self.next = now + (self.next - self.planned);
            self.planned = now;
        }
        if let Some(ready) = &mut self.ready {
            return ready.waiting(now);
        }
        if now < self.next {
            return Vec::new();
        }
        let mut said = Vec::new();
        self.next = self.due(now, io, &mut said);
        self.planned = now;
        said
    }

    /// A panic in a check is reported like any failure; the next check comes as usual.
    fn panicked(&mut self, now: DateTime<Utc>, message: &str) -> Vec<Said> {
        let mut said = Vec::new();
        self.check_failed(
            now,
            format!("automatic update check panicked: {message}"),
            false,
            &mut said,
        );
        self.next = hourly(now);
        self.planned = now;
        said
    }

    /// Returns when to check next.
    fn due(&mut self, now: DateTime<Utc>, io: &mut impl Io, said: &mut Vec<Said>) -> DateTime<Utc> {
        let next = hourly(now);
        match io.auto_update() {
            Ok(true) => {}
            Ok(false) => return next,
            Err(error) => {
                self.check_failed(
                    now,
                    format!("automatic update skipped: cannot load settings: {error:#}"),
                    false,
                    said,
                );
                return next;
            }
        }
        let installed = io.installed();
        if let Decision::Restart(release) = decide(self.running, &installed, None) {
            self.restart(
                now,
                &release,
                format!("release {release} is already installed"),
                said,
            );
            return next;
        }
        let answer = match io.fetch(self.state.etag.as_deref()) {
            Ok(answer) => answer,
            Err(Unasked {
                error,
                offline: true,
            }) => {
                self.check_failed(now, format!("{error:#}"), true, said);
                return next;
            }
            Err(Unasked { error, .. }) => {
                self.check_failed(
                    now,
                    format!("automatic update check failed: {error:#}"),
                    false,
                    said,
                );
                return next;
            }
        };
        let latest = match self.checked(&answer) {
            Ok(latest) => latest,
            Err(error) => {
                let until = limited_until(&answer, now);
                self.state.next_check = until;
                self.check_failed(
                    now,
                    format!("automatic update check failed: {error:#}"),
                    false,
                    said,
                );
                // A limit lifts later than the next hourly check, or not at all before it.
                return until.map_or(next, |until| until.max(next));
            }
        };
        self.state.last_check = Some(now);
        self.state.next_check = None;
        if let Some(trouble) = self.state.check.cleared() {
            said.push(Said::note(format!(
                "automatic update check works again after {} failed attempts since {}",
                trouble.failures,
                shown_time(trouble.failing_since)
            )));
        }
        match decide(self.running, &installed, latest.as_deref()) {
            Decision::Install(tag) => self.attempt(now, &tag, &installed, io, said),
            Decision::Restart(release) => self.restart(
                now,
                &release,
                format!("release {release} is already installed"),
                said,
            ),
            Decision::Current => {}
        }
        next
    }

    /// GitHub's latest stable tag from its answer. A 304 means it has not changed since the
    /// stored ETag; a new tag starts a new count of failed installs.
    fn checked(&mut self, answer: &Answer) -> Result<Option<String>> {
        if answer.status == ureq::http::StatusCode::NOT_MODIFIED && self.state.etag.is_some() {
            return Ok(self.state.tag.clone());
        }
        let release = release(&self.url, answer.status, &answer.body)?;
        let tag = stable(&self.url, &release, &answer.body)?.map(str::to_owned);
        if tag != self.state.tag {
            self.state.tag = tag.clone();
            self.state.install = Trouble::default();
            self.state.next_attempt = None;
        }
        self.state.etag = answer.etag.clone();
        Ok(tag)
    }

    fn attempt(
        &mut self,
        now: DateTime<Utc>,
        tag: &str,
        installed: &Installed,
        io: &mut impl Io,
        said: &mut Vec<Said>,
    ) {
        let state = &mut self.state;
        if state.blocked_tag.as_deref() == Some(tag) && state.blocked_version == installed.version {
            return;
        }
        state.blocked_tag = None;
        state.blocked_version = None;
        if let Some(at) = state.next_attempt {
            // A clock set back must not postpone the retry by as much as it moved.
            let at = at.min(now + backoff(state.install.failures.max(1)));
            state.next_attempt = Some(at);
            if at > now {
                return;
            }
        }
        match io.install(tag) {
            Ok(()) => {
                state.next_attempt = None;
                if let Some(trouble) = state.install.cleared() {
                    said.push(Said::note(format!(
                        "automatic update to {tag} succeeded after {} failed attempts since {}",
                        trouble.failures,
                        shown_time(trouble.failing_since)
                    )));
                }
                self.restart(now, tag, format!("new release {tag} installed"), said);
            }
            Err(error) if needs_person(&error) => {
                state.blocked_tag = Some(tag.to_owned());
                state.blocked_version = installed.version.clone();
                state.install = Trouble::default();
                state.next_attempt = None;
                said.push(Said::error(format!(
                    "automatic update to {tag} needs a person: {}. Until the installed release changes or a newer release appears, automatic updates do not retry {tag}. {error:#}",
                    io.by_hand(tag)
                )));
            }
            // Stopping the interpreter stopped the installer; the release did not fail.
            Err(error) if interrupted(&error) => {
                said.push(Said::daemon(format!(
                    "automatic update to {tag} was interrupted; it is tried again after the interpreter starts: {error:#}"
                )));
            }
            Err(error) => {
                let text = format!("{error:#}");
                let tell = state.install.failed(&digest(&text), now, LOUD);
                let failures = state.install.failures;
                let retry = now + backoff(failures);
                state.next_attempt = Some(retry);
                match tell {
                    Tell::Full => said.push(Said::error(format!(
                        "automatic update to {tag} failed (attempt {failures}; next attempt after {}): {text}",
                        retry.to_rfc3339()
                    ))),
                    Tell::Daily => said.push(Said::error(format!(
                        "automatic update to {tag} still failing ({failures} attempts since {}); next attempt after {}. Its error was shown in full earlier.",
                        shown_time(state.install.failing_since),
                        retry.to_rfc3339()
                    ))),
                    Tell::Nothing => {}
                }
            }
        }
    }

    /// `offline` failures (no network) are told only once they have lasted a day; after that
    /// each different one is shown in full once, like any other failure.
    fn check_failed(
        &mut self,
        now: DateTime<Utc>,
        text: String,
        offline: bool,
        said: &mut Vec<Said>,
    ) {
        let (key, quiet) = if offline {
            (format!("offline {}", digest(&text)), OFFLINE)
        } else {
            (digest(&text), LOUD)
        };
        let trouble = &mut self.state.check;
        match trouble.failed(&key, now, quiet) {
            Tell::Full if offline => said.push(Said::error(format!(
                "automatic update check cannot reach GitHub ({} attempts since {}): {text}",
                trouble.failures,
                shown_time(trouble.failing_since)
            ))),
            Tell::Full => said.push(Said::error(text)),
            Tell::Daily => said.push(Said::error(format!(
                "automatic update check still failing ({} attempts since {}): {}",
                trouble.failures,
                shown_time(trouble.failing_since),
                text.lines().next().unwrap_or_default()
            ))),
            Tell::Nothing => {}
        }
    }

    fn restart(&mut self, now: DateTime<Utc>, release: &str, what: String, said: &mut Vec<Said>) {
        said.push(Said::daemon(format!(
            "{what}; waiting for active work before restarting"
        )));
        self.ready = Some(Ready {
            release: release.to_owned(),
            since: now,
            told: None,
        });
    }
}

fn shown_time(time: Option<DateTime<Utc>>) -> String {
    time.map(|time| time.to_rfc3339())
        .unwrap_or_else(|| "(unknown)".into())
}

/// install.sh stopped with [`NEEDS_PERSON`].
#[derive(Debug)]
struct NeedsPerson(String);

impl std::fmt::Display for NeedsPerson {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for NeedsPerson {}

fn needs_person(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| cause.is::<NeedsPerson>())
}

/// install.sh was stopped because the interpreter is stopping.
#[derive(Debug)]
struct Interrupted(String);

impl std::fmt::Display for Interrupted {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for Interrupted {}

fn interrupted(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| cause.is::<Interrupted>())
}

struct Live {
    managed: &'static Managed,
    repository: String,
    url: String,
    runtime: Weak<Runtime>,
}

impl Io for Live {
    fn auto_update(&mut self) -> Result<bool> {
        Ok(crate::settings::load()?.auto_update)
    }
    fn installed(&mut self) -> Installed {
        installed(&self.managed.prefix, &self.managed.release)
    }
    fn fetch(&mut self, etag: Option<&str>) -> std::result::Result<Answer, Unasked> {
        fetch(&self.url, etag)
    }
    fn install(&mut self, tag: &str) -> Result<()> {
        // The installer has its own process group, which a stop of the daemon's group does
        // not reach: it is stopped here instead of running on without its time limit.
        let stopping = || {
            self.runtime
                .upgrade()
                .is_none_or(|runtime| runtime.stopping.load(Ordering::SeqCst))
        };
        run_installer(self.managed, &self.repository, tag, true, &stopping)
    }
    fn by_hand(&self, tag: &str) -> String {
        person_steps(
            &self.managed.prefix.join("bin/silicon"),
            &self.repository,
            tag,
            in_wsl(),
        )
    }
}

/// Inside the Silicon WSL distribution, whose launcher and profile set SILICON_WSL=1.
fn in_wsl() -> bool {
    std::env::var("SILICON_WSL").as_deref() == Ok("1")
}

/// `silicon update` run by hand in the WSL distribution: install.sh's exit 77 asks for sudo,
/// which the `silicon` user there does not have, so the way that works leads the error.
fn for_a_person(
    error: anyhow::Error,
    silicon: &Path,
    repository: &str,
    tag: &str,
    wsl: bool,
) -> anyhow::Error {
    if wsl && needs_person(&error) {
        let steps = person_steps(silicon, repository, tag, true);
        error.context(format!("installing {tag} needs a person: {steps}"))
    } else {
        error
    }
}

/// How a person finishes an installation install.sh could not (Caddy's port 80 permission).
/// Inside the Windows (WSL) distribution the `silicon` user has no sudo and only the Windows
/// installer, whose provision.sh runs as root, grants Caddy the permission; that release's
/// install.ps1 installs exactly `tag`.
fn person_steps(silicon: &Path, repository: &str, tag: &str, wsl: bool) -> String {
    if wsl {
        format!(
            "on Windows, rerun the Windows installer of {tag}, which installs it and grants Caddy its port 80 permission: in PowerShell run `irm https://github.com/{repository}/releases/download/{tag}/install.ps1 | iex`, or run install.ps1 from the {tag} release. `silicon update` with sudo cannot do it here: the `silicon` user in the WSL distribution has no sudo"
        )
    } else {
        format!(
            "run `{} update` in a terminal, as a user who can use sudo, to install it",
            shell_words::quote(&silicon.to_string_lossy())
        )
    }
}

fn install_limit() -> Duration {
    match std::env::var("SILICON_UPDATE_TIMEOUT_SECS") {
        Err(_) => INSTALL_LIMIT,
        Ok(value) => match value.trim().parse::<u64>() {
            Ok(seconds) if seconds > 0 => Duration::from_secs(seconds),
            _ => {
                crate::stderr_line(&format!(
                    "SILICON_UPDATE_TIMEOUT_SECS={value:?} is not a positive number of seconds; using {}",
                    shown(INSTALL_LIMIT)
                ));
                INSTALL_LIMIT
            }
        },
    }
}

fn shown(limit: Duration) -> String {
    let seconds = limit.as_secs();
    if seconds >= 60 && seconds.is_multiple_of(60) {
        format!("{}m", seconds / 60)
    } else if seconds > 0 {
        format!("{seconds}s")
    } else {
        format!("{}ms", limit.as_millis())
    }
}

fn run_installer(
    managed: &Managed,
    repository: &str,
    tag: &str,
    unattended: bool,
    stop: &dyn Fn() -> bool,
) -> Result<()> {
    let dir = crate::server::directory();
    state::private_dir(&dir)?;
    let mut command = Command::new("sh");
    command
        .env("SILICON_PREFIX", &managed.prefix)
        .env("SILICON_VERSION", tag)
        .env("SILICON_NONINTERACTIVE", if unattended { "1" } else { "0" })
        .env("SILICON_REPOSITORY", repository)
        // The release this process runs survives the installer's pruning.
        .env("SILICON_KEEP_RELEASE", &managed.release)
        .env_remove("SILICON_SOURCE_DIR")
        .env_remove("SILICON_GIT_REV")
        .env_remove("SILICON_DEPENDENCY_BIN_DIR")
        .env_remove("SILICON_RELEASE_BASE_URL");
    installer(
        command,
        &dir.join("updates.log"),
        include_str!("../install.sh"),
        install_limit(),
        unattended,
        stop,
    )
    .with_context(|| format!("installing Silicon {tag} from {repository} failed"))
}

/// Run install.sh with its output appended to `log`; a failure carries this run's output.
///
/// Unattended, install.sh runs in its own process group so that at `limit`, or when `stop`
/// says so, the whole group (curl, tar, the probes) is stopped: SIGTERM, which lets its trap
/// release the lock, then SIGKILL. A person running `silicon update` keeps it in their
/// terminal's group, where sudo can ask for a password and Ctrl-C reaches it.
fn installer(
    mut command: Command,
    log: &Path,
    script: &str,
    limit: Duration,
    own_group: bool,
    stop: &dyn Fn() -> bool,
) -> Result<()> {
    if let Err(error) = crate::rotate(log, crate::LOG_CAP, crate::LOG_KEEP) {
        crate::stderr_line(&format!("{error:#}"));
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(log)
        .with_context(|| format!("cannot open update log {}", log.display()))?;
    let start = file
        .metadata()
        .with_context(|| format!("cannot inspect update log {}", log.display()))?
        .len();
    // The script is a file, not piped to sh's stdin: stdin is /dev/null, so nothing it runs
    // can read the script, and the deadline starts as soon as sh does.
    let directory = log.parent().unwrap_or(Path::new("."));
    remove_stale_scripts(directory);
    let staged = directory.join(format!(".installer-{}.sh", uuid::Uuid::new_v4()));
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staged)
        .and_then(|mut staged| staged.write_all(script.as_bytes()))
        .with_context(|| format!("cannot write install.sh to {}", staged.display()))?;
    let outcome = run_script(&mut command, &staged, &file, limit, own_group, stop);
    if let Err(error) = fs::remove_file(&staged) {
        if error.kind() != std::io::ErrorKind::NotFound {
            crate::stderr_line(&format!(
                "cannot remove the installer copy {}: {error}",
                staged.display()
            ));
        }
    }
    let (status, stopped) = outcome?;
    if status.success() && stopped.is_none() {
        return Ok(());
    }
    let output = appended(log, start);
    let problem = match &stopped {
        Some((_, note)) => {
            // updates.log says why the run ended; the error below already has everything.
            let _ = writeln!(file, "silicon: {note}");
            format!("`sh` running install.sh {note}; it ended with {status}")
        }
        None => format!("`sh` running install.sh failed: {status}"),
    };
    let message = format!("{problem}\noutput (also in {}):\n{output}", log.display());
    match stopped {
        Some((Cut::Stopping, _)) => Err(Interrupted(message).into()),
        None if status.code() == Some(NEEDS_PERSON) => Err(NeedsPerson(message).into()),
        _ => Err(anyhow!(message)),
    }
}

/// Why a run of install.sh was cut short.
#[derive(Debug, PartialEq)]
enum Cut {
    /// It outlived its time limit.
    Limit,
    /// The interpreter is stopping.
    Stopping,
}

/// Copies left by an interpreter that was killed while an installer ran.
fn remove_stale_scripts(directory: &Path) {
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        let old = entry
            .metadata()
            .and_then(|metadata| metadata.modified())
            .ok()
            .and_then(|modified| SystemTime::now().duration_since(modified).ok())
            .is_some_and(|age| age > Duration::from_secs(24 * 3600));
        if old && name.starts_with(".installer-") && name.ends_with(".sh") {
            let _ = fs::remove_file(entry.path());
        }
    }
}

/// The exit status, and why and how the run was stopped when it outlived `limit` or `stop`
/// said so first.
fn run_script(
    command: &mut Command,
    script: &Path,
    log: &File,
    limit: Duration,
    own_group: bool,
    stop: &dyn Fn() -> bool,
) -> Result<(ExitStatus, Option<(Cut, String)>)> {
    let share = || {
        log.try_clone()
            .context("cannot share the update log with install.sh")
    };
    command
        .arg(script)
        .stdin(Stdio::null())
        .stdout(share()?)
        .stderr(share()?);
    if own_group {
        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .context("could not run `sh` for install.sh")?;
    let deadline = Instant::now() + limit;
    if let Some(status) = wait_until(&mut child, deadline, stop)? {
        return Ok((status, None));
    }
    let (cut, why) = if Instant::now() >= deadline {
        (
            Cut::Limit,
            format!(
                "stopped after {} without finishing (its time limit, SILICON_UPDATE_TIMEOUT_SECS)",
                shown(limit)
            ),
        )
    } else {
        (
            Cut::Stopping,
            "was stopped because the interpreter is stopping".to_owned(),
        )
    };
    signal(&child, own_group, libc::SIGTERM);
    let status = match wait_until(&mut child, Instant::now() + GRACE, &|| false)? {
        Some(status) => status,
        None => {
            signal(&child, own_group, libc::SIGKILL);
            child.wait().context("could not wait for install.sh")?
        }
    };
    if own_group {
        // Whatever it left behind in its group goes too.
        signal(&child, true, libc::SIGKILL);
    }
    let target = if own_group { "its process group" } else { "it" };
    Ok((
        status,
        Some((
            cut,
            format!(
                "{why}: SIGTERM to {target}, then SIGKILL {}s later",
                GRACE.as_secs()
            ),
        )),
    ))
}

/// The exit status, or None when `deadline` passed or `stop` said so while it still ran.
fn wait_until(
    child: &mut Child,
    deadline: Instant,
    stop: &dyn Fn() -> bool,
) -> Result<Option<ExitStatus>> {
    loop {
        if let Some(status) = child.try_wait().context("could not wait for install.sh")? {
            return Ok(Some(status));
        }
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() || stop() {
            return Ok(None);
        }
        thread::sleep(left.min(Duration::from_millis(100)));
    }
}

fn signal(child: &Child, group: bool, signal: libc::c_int) {
    let pid = child.id() as libc::pid_t;
    // It may already be gone; that is the outcome wanted.
    unsafe {
        if group {
            libc::killpg(pid, signal);
        } else {
            libc::kill(pid, signal);
        }
    }
}

/// Everything written to `log` after byte `start`, verbatim.
fn appended(log: &Path, start: u64) -> String {
    match crate::appended_since(log, start) {
        Ok(bytes) if bytes.trim_ascii().is_empty() => "(empty)".into(),
        Ok(bytes) => String::from_utf8_lossy(&bytes)
            .trim_end_matches(['\r', '\n'])
            .to_owned(),
        Err(error) => format!("(cannot read {}: {error})", log.display()),
    }
}

/// Remove releases nothing needs any more. Kept: the release `current` names, the one this
/// process runs, the newest other one (to roll back to), and any release a running
/// interpreter recorded in `lib/silicon/running`. Installs made by older installers never
/// pruned, so the updater does this once when it starts. Each removal is said in daemon.log.
pub fn prune_releases() -> Result<Vec<PathBuf>> {
    let managed = managed().context("pruning releases requires a managed bundle installation")?;
    prune(&managed.prefix.join("lib/silicon"), &managed.release)
}

/// The lock of an installer that runs, or is reclaiming a stale lock, in `runtime`. That
/// installer prunes when it activates; pruning alongside it could remove its new release.
fn installing(runtime: &Path) -> Option<PathBuf> {
    [".install-lock", ".install-lock.reclaim"]
        .into_iter()
        .map(|name| runtime.join(name))
        .find(|lock| fs::symlink_metadata(lock).is_ok())
}

fn prune(runtime: &Path, running: &Path) -> Result<Vec<PathBuf>> {
    if let Some(lock) = installing(runtime) {
        crate::stderr_line(&format!(
            "old releases were not pruned: an installer holds {}",
            lock.display()
        ));
        return Ok(Vec::new());
    }
    let releases = runtime.join("releases");
    let current = runtime.join("current");
    let target = fs::read_link(&current).with_context(|| {
        format!(
            "cannot read {}, so no release was pruned",
            current.display()
        )
    })?;
    let active = target.file_name().map(OsString::from).with_context(|| {
        format!(
            "{} points to {}, which names no release; no release was pruned",
            current.display(),
            target.display()
        )
    })?;
    if !releases.join(&active).is_dir() {
        bail!(
            "{} points to {}, which is not a release in {}; no release was pruned",
            current.display(),
            target.display(),
            releases.display()
        );
    }
    let mut keep = HashSet::from([active]);
    if running.parent() == Some(releases.as_path()) {
        keep.extend(running.file_name().map(OsString::from));
    }
    keep.extend(live_releases(&runtime.join("running")));
    let mut others = Vec::new();
    for entry in
        fs::read_dir(&releases).with_context(|| format!("cannot list {}", releases.display()))?
    {
        let entry = entry.with_context(|| format!("cannot list {}", releases.display()))?;
        let name = entry.file_name();
        if keep.contains(&name) || name.to_string_lossy().starts_with('.') {
            continue;
        }
        let modified = entry
            .metadata()
            .and_then(|metadata| metadata.modified())
            .unwrap_or(SystemTime::UNIX_EPOCH);
        others.push((modified, name, entry.path()));
    }
    others.sort();
    // The newest other release is what a rollback would return to.
    others.pop();
    let mut removed = Vec::new();
    let mut failed = Vec::new();
    for (_, _, path) in others {
        // An installer that starts meanwhile is left alone from then on.
        if let Some(lock) = installing(runtime) {
            crate::stderr_line(&format!(
                "pruning old releases stopped: an installer took {}",
                lock.display()
            ));
            return Ok(removed);
        }
        match remove(&path) {
            Ok(()) => {
                crate::stderr_line(&format!("removed old release {}", path.display()));
                removed.push(path);
            }
            Err(error) => failed.push(format!("{}: {error}", path.display())),
        }
    }
    // Staging directories of installers that were killed; a live one holds the lock.
    if let Ok(entries) = fs::read_dir(runtime) {
        for entry in entries.flatten() {
            let stale = entry
                .metadata()
                .ok()
                .filter(|metadata| metadata.is_dir())
                .and_then(|metadata| metadata.modified().ok())
                .and_then(|modified| SystemTime::now().duration_since(modified).ok())
                .is_some_and(|age| age > Duration::from_secs(3600));
            if stale && entry.file_name().to_string_lossy().starts_with(".install.") {
                let path = entry.path();
                match fs::remove_dir_all(&path) {
                    Ok(()) => {
                        crate::stderr_line(&format!(
                            "removed stale installer staging directory {}",
                            path.display()
                        ));
                        removed.push(path);
                    }
                    Err(error) => failed.push(format!("{}: {error}", path.display())),
                }
            }
        }
    }
    if !failed.is_empty() {
        bail!(
            "could not remove old releases (the active and running releases are untouched):\n{}",
            failed.join("\n")
        );
    }
    Ok(removed)
}

fn remove(path: &Path) -> std::io::Result<()> {
    if fs::symlink_metadata(path)?.is_dir() {
        fs::remove_dir_all(path)
    } else {
        fs::remove_file(path)
    }
}

/// Releases that running interpreters recorded as theirs; records of processes that are
/// gone are removed.
fn live_releases(directory: &Path) -> Vec<OsString> {
    let Ok(entries) = fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut live = Vec::new();
    for entry in entries.flatten() {
        let pid = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<libc::pid_t>().ok())
            .filter(|pid| *pid > 0);
        let Some(pid) = pid else { continue };
        if alive(pid) {
            if let Ok(name) = fs::read_to_string(entry.path()) {
                let name = name.trim();
                if !name.is_empty() {
                    live.push(OsString::from(name));
                }
            }
        } else {
            let _ = fs::remove_file(entry.path());
        }
    }
    live
}

fn alive(pid: libc::pid_t) -> bool {
    // EPERM: it exists, it is another user's.
    let signalled = unsafe { libc::kill(pid, 0) } == 0;
    signalled || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

/// Record which release this interpreter runs, so an installer started elsewhere (a manual
/// `silicon update`, a rerun of install.sh) never removes it while it runs. Records of
/// interpreters that are gone are removed at every start, updates on or off, so a daemon
/// restarted often does not collect one per start.
fn mark_running(directory: &Path, release: &Path) -> Result<()> {
    let name = release
        .file_name()
        .with_context(|| format!("release {} has no name", release.display()))?;
    fs::create_dir_all(directory)
        .with_context(|| format!("cannot create {}", directory.display()))?;
    let path = directory.join(std::process::id().to_string());
    fs::write(&path, format!("{}\n", name.to_string_lossy()))
        .with_context(|| format!("cannot write {}", path.display()))?;
    live_releases(directory);
    Ok(())
}

/// The updater serves every Silicon, so its failures go to daemon.log and to each connected
/// Silicon's log, where `silicon logs` and the dashboard show them.
fn report(runtime: &Runtime, kind: Option<&str>, message: &str) {
    crate::stderr_line(message);
    let Some(kind) = kind else {
        return;
    };
    let connected: Vec<_> = runtime
        .silicons
        .read()
        .recover()
        .values()
        .cloned()
        .collect();
    for silicon in connected {
        let home = &silicon.cfg.home;
        // A home whose directory is gone is not recreated just to hold the line.
        if fs::metadata(home.join(".silicon"))
            .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound)
        {
            continue;
        }
        let masked = crate::failure::mask(home, message, &[]);
        if let Err(error) = crate::log_line_scoped(
            home,
            Some(silicon.cfg.generation),
            kind,
            "interpreter",
            &masked,
        ) {
            crate::stderr_line(&format!(
                "{error:#}; the updater line it was recording is above"
            ));
        }
    }
}

pub fn start(runtime: &Arc<Runtime>, ready: Arc<AtomicBool>) {
    let disabled = std::env::var("SILICON_AUTO_UPDATE").as_deref() == Ok("0");
    let managed = match managed() {
        Ok(managed) => managed,
        Err(error) => {
            if !disabled {
                crate::stderr_line(&format!("automatic updates require a managed bundle installation; source builds remain under your control: {error:#}"));
            }
            return;
        }
    };
    if let Err(error) = mark_running(
        &managed.prefix.join("lib/silicon/running"),
        &managed.release,
    ) {
        crate::stderr_line(&format!(
            "{error:#}; an installer run elsewhere could remove this interpreter's release while it runs"
        ));
    }
    if disabled {
        return;
    }
    let (running, repository) = match (running(), repository()) {
        (Ok(running), Ok(repository)) => (running, repository),
        (Err(error), _) | (_, Err(error)) => {
            crate::stderr_line(&format!("automatic updates are off: {error:#}"));
            return;
        }
    };
    let runtime = Arc::downgrade(runtime);
    let spawned = thread::Builder::new()
        .name("updater".into())
        .spawn(move || {
            match std::panic::catch_unwind(prune_releases) {
                Ok(Ok(_)) => {}
                Ok(Err(error)) => crate::stderr_line(&format!("{error:#}")),
                Err(panic) => crate::stderr_line(&format!(
                    "pruning old releases panicked: {}",
                    crate::failure::panic_message(&*panic)
                )),
            }
            let url = latest_url(&repository);
            let io = Live {
                managed,
                repository,
                url: url.clone(),
                runtime: runtime.clone(),
            };
            run(runtime, ready, running, url, io);
        });
    if let Err(error) = spawned {
        crate::stderr_line(&format!(
            "automatic updates are off: could not start the updater thread: {error}"
        ));
    }
}

fn run(
    runtime: Weak<Runtime>,
    ready: Arc<AtomicBool>,
    running: [u64; 3],
    url: String,
    mut io: Live,
) {
    let path = state_path();
    let (state, problem) = load_state(&path);
    if let Some(problem) = problem {
        crate::stderr_line(&problem);
    }
    let mut updater = Updater::new(state, running, url, Utc::now());
    let mut saved = updater.state.clone();
    let mut unsaved: Option<String> = None;
    loop {
        for _ in 0..TICK_SECONDS {
            thread::sleep(Duration::from_secs(1));
            match runtime.upgrade() {
                Some(runtime) if !runtime.stopping.load(Ordering::SeqCst) => {}
                _ => return,
            }
        }
        let Some(runtime) = runtime.upgrade() else {
            return;
        };
        let now = Utc::now();
        // A panic ends this tick, not the updater: it runs for the life of the daemon.
        let said = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            updater.tick(now, &mut io)
        })) {
            Ok(said) => said,
            Err(panic) => updater.panicked(now, crate::failure::panic_message(&*panic)),
        };
        if updater.ready.is_some() {
            ready.store(true, Ordering::SeqCst);
        }
        let delivered = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            for line in said {
                report(&runtime, line.kind, &line.text);
            }
            if updater.state == saved {
                return;
            }
            match state::write_json(&path, &updater.state) {
                Ok(()) => {
                    saved = updater.state.clone();
                    unsaved = None;
                }
                Err(error) => {
                    let error = format!("{error:#}");
                    if unsaved.as_ref() != Some(&error) {
                        crate::stderr_line(&format!(
                            "automatic update state could not be saved, so a restart forgets its schedule and failures: {error}"
                        ));
                        unsaved = Some(error);
                    }
                }
            }
        }));
        if let Err(panic) = delivered {
            crate::stderr_line(&format!(
                "automatic update reporting panicked: {}",
                crate::failure::panic_message(&*panic)
            ));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ureq::http::StatusCode;

    #[test]
    fn only_newer_stable_releases_are_candidates() {
        assert!(version("v3.10.0") > version("3.9.2"));
        assert_eq!(version("v3.5.0"), version("3.5.0"));
        for bad in ["v3.5", "v3.5.0-rc1", "3.5.0/evil", "3.5.0.1", "latest"] {
            assert!(version(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn github_and_installer_failures_carry_their_own_words() {
        let url = "https://api.github.com/repos/o/r/releases/latest";
        let limited = r#"{"message":"API rate limit exceeded for 1.2.3.4."}"#;
        let error = format!(
            "{:#}",
            release(url, ureq::http::StatusCode::FORBIDDEN, limited).unwrap_err()
        );
        assert!(
            error.contains("HTTP 403 Forbidden") && error.contains(limited),
            "{error}"
        );
        let error = format!(
            "{:#}",
            release(url, ureq::http::StatusCode::OK, "<html>maintenance</html>").unwrap_err()
        );
        assert!(
            error.contains("invalid JSON (expected value at line 1 column 1)")
                && error.contains("<html>maintenance</html>"),
            "{error}"
        );
        // A release without its draft/prerelease flags is malformed, not "no update".
        let body = r#"{"tag_name":"v999.0.0"}"#;
        let error = format!(
            "{:#}",
            newer(url, &serde_json::from_str(body).unwrap(), body).unwrap_err()
        );
        assert!(
            error.contains(&format!(
                "GitHub release from {url} has no boolean draft:\n{body}"
            )),
            "{error}"
        );
        let answer = |tag: &str, prerelease: bool| serde_json::json!({"tag_name": tag, "draft": false, "prerelease": prerelease});
        assert_eq!(
            newer(url, &answer("v999.0.0", false), "").unwrap(),
            Some("v999.0.0")
        );
        assert_eq!(newer(url, &answer("v999.0.0", true), "").unwrap(), None);
        assert_eq!(newer(url, &answer("v0.0.1", false), "").unwrap(), None);

        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("updates.log");
        fs::write(&log, "earlier run\n").unwrap();
        let script = "echo 'silicon install: downloading v9.9.9'\necho 'silicon install: checksum mismatch for silicon.tar.gz' >&2\nexit 3\n";
        let error = installer(
            Command::new("sh"),
            &log,
            script,
            INSTALL_LIMIT,
            true,
            &|| false,
        )
        .unwrap_err();
        assert!(!needs_person(&error));
        let error = format!("{error:#}");
        for expected in [
            "`sh` running install.sh failed: exit status: 3",
            "silicon install: downloading v9.9.9",
            "silicon install: checksum mismatch for silicon.tar.gz",
            &log.display().to_string(),
        ] {
            assert!(error.contains(expected), "missing {expected:?} in {error}");
        }
        assert!(!error.contains("earlier run"), "{error}");
        installer(
            Command::new("sh"),
            &log,
            "echo fine\n",
            INSTALL_LIMIT,
            false,
            &|| false,
        )
        .unwrap();
        let spawn = format!(
            "{:#}",
            installer(
                Command::new(dir.path().join("absent")),
                &log,
                "",
                INSTALL_LIMIT,
                true,
                &|| false
            )
            .unwrap_err()
        );
        assert!(spawn.contains("No such file or directory"), "{spawn}");
        // The private copy of the script is gone after every run.
        let left: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| name.starts_with(".installer-"))
            .collect();
        assert!(left.is_empty(), "{left:?}");
    }

    #[test]
    fn an_installer_that_needs_a_person_is_recognized_through_context() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("updates.log");
        let script = "echo 'silicon install: Caddy needs port 80 permission' >&2\nexit 77\n";
        let error = installer(
            Command::new("sh"),
            &log,
            script,
            INSTALL_LIMIT,
            true,
            &|| false,
        )
        .context("installing Silicon v9.9.9 from o/r failed")
        .unwrap_err();
        assert!(needs_person(&error));
        let text = format!("{error:#}");
        assert!(
            text.starts_with("installing Silicon v9.9.9 from o/r failed: `sh` running install.sh failed: exit status: 77\noutput (also in ")
                && text.ends_with("silicon install: Caddy needs port 80 permission"),
            "{text}"
        );
    }

    #[test]
    fn an_installer_past_its_limit_is_stopped_with_its_whole_group() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("updates.log");
        let child = dir.path().join("child.pid");
        // The background sleep stands in for curl: it must not outlive the installer.
        let script = format!(
            "echo downloading\n(sleep 60; echo late) &\necho $! > '{}'\nsleep 60\necho never\n",
            child.display()
        );
        let started = Instant::now();
        let error = format!(
            "{:#}",
            installer(
                Command::new("sh"),
                &log,
                &script,
                Duration::from_millis(500),
                true,
                &|| false
            )
            .unwrap_err()
        );
        assert!(started.elapsed() < Duration::from_secs(10), "{error}");
        assert!(
            error.starts_with("`sh` running install.sh stopped after 500ms without finishing (its time limit, SILICON_UPDATE_TIMEOUT_SECS): SIGTERM to its process group, then SIGKILL 5s later; it ended with signal: 15"),
            "{error}"
        );
        assert!(error.contains("\ndownloading"), "{error}");
        assert!(!error.contains("never"), "{error}");
        let pid: libc::pid_t = fs::read_to_string(&child).unwrap().trim().parse().unwrap();
        let gone = (0..50).any(|_| {
            if unsafe { libc::kill(pid, 0) } != 0 {
                return true;
            }
            thread::sleep(Duration::from_millis(100));
            false
        });
        assert!(gone, "the installer's child {pid} survived it");
        assert!(fs::read_to_string(&log)
            .unwrap()
            .ends_with("SIGKILL 5s later\n"));
    }

    #[test]
    fn an_installer_is_stopped_with_the_interpreter_and_that_is_no_failed_install() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("updates.log");
        let child = dir.path().join("child.pid");
        let script = format!(
            "echo downloading\n(sleep 60; echo late) &\necho $! > '{}'\nsleep 60\necho never\n",
            child.display()
        );
        let stopping = AtomicBool::new(false);
        let started = Instant::now();
        let error = thread::scope(|scope| {
            scope.spawn(|| {
                thread::sleep(Duration::from_millis(300));
                stopping.store(true, Ordering::SeqCst);
            });
            installer(
                Command::new("sh"),
                &log,
                &script,
                INSTALL_LIMIT,
                true,
                &|| stopping.load(Ordering::SeqCst),
            )
            .unwrap_err()
        });
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(interrupted(&error) && !needs_person(&error));
        let text = format!("{error:#}");
        assert!(
            text.starts_with("`sh` running install.sh was stopped because the interpreter is stopping: SIGTERM to its process group, then SIGKILL 5s later; it ended with signal: 15")
                && text.contains("\noutput (also in "),
            "{text}"
        );
        assert!(text.ends_with("\ndownloading"), "{text}");
        let pid: libc::pid_t = fs::read_to_string(&child).unwrap().trim().parse().unwrap();
        let gone = (0..50).any(|_| {
            if unsafe { libc::kill(pid, 0) } != 0 {
                return true;
            }
            thread::sleep(Duration::from_millis(100));
            false
        });
        assert!(gone, "the installer's child {pid} survived it");

        // The updater neither counts it against the release nor tells the Silicons.
        let mut io = Fake::new();
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.0"))));
        io.install = Box::new(|_| {
            Err(anyhow::Error::new(Interrupted(
                "`sh` running install.sh was stopped because the interpreter is stopping".into(),
            ))
            .context("installing Silicon v2.0.0 from o/r failed"))
        });
        let mut updater = fresh(at(0));
        let said = updater.tick(at(0), &mut io);
        assert_eq!(said.len(), 1);
        assert!(said[0].kind.is_none(), "daemon.log only");
        assert_eq!(
            said[0].text,
            "automatic update to v2.0.0 was interrupted; it is tried again after the interpreter starts: installing Silicon v2.0.0 from o/r failed: `sh` running install.sh was stopped because the interpreter is stopping"
        );
        assert_eq!(updater.state.install, Trouble::default());
        assert_eq!(updater.state.next_attempt, None);
        assert!(updater.ready.is_none());
    }

    fn answer(status: StatusCode, etag: Option<&str>, body: &str) -> Answer {
        Answer {
            status,
            etag: etag.map(str::to_owned),
            retry_after: None,
            remaining: None,
            reset: None,
            body: body.to_owned(),
        }
    }

    fn latest(tag: &str) -> String {
        serde_json::json!({"tag_name": tag, "draft": false, "prerelease": false}).to_string()
    }

    type Installs = dyn FnMut(&str) -> Result<()>;

    /// A world in which every answer is scripted and every call is counted.
    struct Fake {
        auto_update: bool,
        installed: Installed,
        answers: Vec<std::result::Result<Answer, Unasked>>,
        etags: Vec<Option<String>>,
        installs: Vec<String>,
        install: Box<Installs>,
        /// Inside the Windows (WSL) distribution.
        wsl: bool,
    }

    impl Fake {
        fn new() -> Self {
            Fake {
                wsl: false,
                auto_update: true,
                installed: Installed {
                    version: Some("v1.0.0".into()),
                    running: true,
                },
                answers: Vec::new(),
                etags: Vec::new(),
                installs: Vec::new(),
                install: Box::new(|_| Ok(())),
            }
        }
    }

    impl Io for Fake {
        fn auto_update(&mut self) -> Result<bool> {
            Ok(self.auto_update)
        }
        fn installed(&mut self) -> Installed {
            self.installed.clone()
        }
        fn fetch(&mut self, etag: Option<&str>) -> std::result::Result<Answer, Unasked> {
            self.etags.push(etag.map(str::to_owned));
            self.answers.remove(0)
        }
        fn install(&mut self, tag: &str) -> Result<()> {
            self.installs.push(tag.to_owned());
            (self.install)(tag)
        }
        fn by_hand(&self, tag: &str) -> String {
            person_steps(Path::new("/prefix/bin/silicon"), "o/r", tag, self.wsl)
        }
    }

    fn at(hours: i64) -> DateTime<Utc> {
        DateTime::from_timestamp(1_800_000_000, 0).unwrap() + TimeDelta::hours(hours)
    }

    fn fresh(now: DateTime<Utc>) -> Updater {
        let mut updater = Updater::new(State::default(), [1, 0, 0], "https://x/latest".into(), now);
        updater.next = now;
        updater
    }

    fn texts(said: &[Said]) -> Vec<&str> {
        said.iter().map(|line| line.text.as_str()).collect()
    }

    #[test]
    fn checks_are_scheduled_by_wall_clock_with_jitter_and_backoff() {
        let now = at(0);
        for _ in 0..50 {
            // Never checked (or long ago): five to ten minutes after the start.
            let first = first_check(&State::default(), now);
            assert!(first >= now + TimeDelta::minutes(5) && first <= now + TimeDelta::minutes(10));
            let next = hourly(now);
            assert!(next >= now + TimeDelta::minutes(50) && next <= now + TimeDelta::minutes(70));
            // Checked twenty minutes ago: about an hour after that check.
            let recent = State {
                last_check: Some(now - TimeDelta::minutes(20)),
                ..State::default()
            };
            let first = first_check(&recent, now);
            assert!(first >= now + TimeDelta::minutes(30) && first <= now + TimeDelta::minutes(50));
            // A last check "in the future" means the clock went back: check soon.
            let ahead = State {
                last_check: Some(now + TimeDelta::days(300)),
                ..State::default()
            };
            assert!(first_check(&ahead, now) <= now + TimeDelta::minutes(10));
        }
        // A rate limit that outlives the restart is honoured, a far-future one is not.
        let limited = State {
            next_check: Some(now + TimeDelta::hours(3)),
            ..State::default()
        };
        assert_eq!(first_check(&limited, now), now + TimeDelta::hours(3));
        let bogus = State {
            next_check: Some(now + TimeDelta::days(30)),
            ..State::default()
        };
        assert!(first_check(&bogus, now) <= now + TimeDelta::minutes(10));

        assert_eq!(backoff(1), TimeDelta::hours(1));
        assert_eq!(backoff(2), TimeDelta::hours(2));
        assert_eq!(backoff(4), TimeDelta::hours(8));
        assert_eq!(backoff(8), TimeDelta::hours(128));
        assert_eq!(backoff(9), TimeDelta::days(7));
        assert_eq!(backoff(64), TimeDelta::days(7));
        assert_eq!(backoff(u32::MAX), TimeDelta::days(7));

        // The clock set back a year keeps the wait that was left instead of the date.
        let mut io = Fake::new();
        let mut updater = fresh(now);
        updater.next = now + TimeDelta::minutes(40);
        let back = now - TimeDelta::days(365);
        assert!(updater.tick(back, &mut io).is_empty());
        assert_eq!(updater.next, back + TimeDelta::minutes(40));
        assert_eq!(updater.planned, back);
    }

    #[test]
    fn an_unchanged_release_is_asked_for_with_its_etag_and_a_304_changes_nothing() {
        let mut io = Fake::new();
        io.installed.version = Some("v1.0.0".into());
        io.answers.push(Ok(answer(
            StatusCode::OK,
            Some("W/\"one\""),
            &latest("v1.0.0"),
        )));
        io.answers
            .push(Ok(answer(StatusCode::NOT_MODIFIED, None, "")));
        io.answers.push(Ok(answer(
            StatusCode::OK,
            Some("\"two\""),
            &latest("v1.1.0"),
        )));
        let mut updater = fresh(at(0));
        assert!(updater.tick(at(0), &mut io).is_empty());
        assert_eq!(updater.state.etag.as_deref(), Some("W/\"one\""));
        assert_eq!(updater.state.tag.as_deref(), Some("v1.0.0"));
        assert_eq!(updater.state.last_check, Some(at(0)));
        assert!(updater.next >= at(0) + TimeDelta::minutes(50));
        // Not due yet: nothing is fetched.
        assert!(updater
            .tick(at(0) + TimeDelta::minutes(30), &mut io)
            .is_empty());
        assert_eq!(io.etags.len(), 1);
        assert!(updater.tick(at(2), &mut io).is_empty());
        assert_eq!(io.etags[1].as_deref(), Some("W/\"one\""));
        assert_eq!(updater.state.last_check, Some(at(2)));
        assert!(io.installs.is_empty());
        // A new release arrives with a new ETag and is installed.
        let said = updater.tick(at(4), &mut io);
        assert_eq!(io.installs, ["v1.1.0"]);
        assert_eq!(updater.state.etag.as_deref(), Some("\"two\""));
        assert_eq!(
            texts(&said),
            ["new release v1.1.0 installed; waiting for active work before restarting"]
        );
        assert!(said[0].kind.is_none(), "daemon.log only");
        assert!(updater.ready.is_some());
    }

    #[test]
    fn a_release_already_installed_is_restarted_into_without_a_download() {
        let running = [1, 0, 0];
        let behind = |version: &str| Installed {
            version: Some(version.into()),
            running: false,
        };
        assert_eq!(
            decide(running, &behind("v1.1.0"), Some("v1.1.0")),
            Decision::Restart("v1.1.0".into())
        );
        assert_eq!(
            decide(running, &behind("v1.1.0"), None),
            Decision::Restart("v1.1.0".into())
        );
        assert_eq!(
            decide(running, &behind("v1.1.0"), Some("v1.2.0")),
            Decision::Install("v1.2.0".into())
        );
        // Never "downgrade" the installed release to GitHub's older latest.
        assert_eq!(
            decide(running, &behind("v1.3.0"), Some("v1.2.0")),
            Decision::Restart("v1.3.0".into())
        );
        assert_eq!(
            decide(running, &behind("v0.9.0"), Some("v1.0.0")),
            Decision::Current
        );
        assert_eq!(
            decide(running, &behind("v0.9.0"), Some("v1.0.1")),
            Decision::Install("v1.0.1".into())
        );
        // The release this process runs cannot be newer than itself, whatever VERSION says.
        let own = Installed {
            version: Some("v9.0.0".into()),
            running: true,
        };
        assert_eq!(decide(running, &own, None), Decision::Current);
        assert_eq!(
            decide(running, &own, Some("v1.0.1")),
            Decision::Install("v1.0.1".into())
        );

        // The updater restarts without asking GitHub or running the installer.
        let mut io = Fake::new();
        io.installed = behind("v1.1.0");
        let mut updater = fresh(at(0));
        let said = updater.tick(at(0), &mut io);
        assert!(io.etags.is_empty() && io.installs.is_empty());
        assert_eq!(
            texts(&said),
            ["release v1.1.0 is already installed; waiting for active work before restarting"]
        );
        // While active work postpones the restart it is said after an hour, then daily.
        assert!(updater
            .tick(at(0) + TimeDelta::minutes(59), &mut io)
            .is_empty());
        let said = updater.tick(at(1), &mut io);
        assert_eq!(said.len(), 1);
        assert!(
            said[0].text.starts_with(&format!("automatic update: Silicon v1.1.0 has been installed for 1 hours (since {}), and the interpreter is still waiting for active work", at(0).to_rfc3339())),
            "{}",
            said[0].text
        );
        assert!(updater.tick(at(20), &mut io).is_empty());
        assert_eq!(updater.tick(at(25), &mut io).len(), 1);
        assert!(
            io.etags.is_empty(),
            "no update checks while a restart waits"
        );
    }

    #[test]
    fn a_failing_install_backs_off_and_is_told_once_then_daily() {
        let mut io = Fake::new();
        for _ in 0..12 {
            io.answers
                .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.0"))));
        }
        let mut run = 0;
        io.install = Box::new(move |_| {
            run += 1;
            bail!("`sh` running install.sh failed: exit status: 1\noutput:\nsilicon install: could not store release v2.0.0-Ab{run}x in /p/lib/silicon/.install.Q{run}z")
        });
        let mut updater = fresh(at(0));
        let said = updater.tick(at(0), &mut io);
        assert_eq!(io.installs.len(), 1);
        assert_eq!(said.len(), 1);
        assert!(
            said[0].text.starts_with(&format!(
                "automatic update to v2.0.0 failed (attempt 1; next attempt after {}): `sh` running install.sh failed",
                (at(1)).to_rfc3339()
            )),
            "{}",
            said[0].text
        );
        assert_eq!(said[0].kind, Some("error"));
        assert_eq!(updater.state.next_attempt, Some(at(1)));
        // The hourly check before the retry is due does not install.
        updater.next = at(0) + TimeDelta::minutes(50);
        assert!(updater
            .tick(at(0) + TimeDelta::minutes(50), &mut io)
            .is_empty());
        assert_eq!(io.installs.len(), 1);
        // Retries at 1, 2, 4 and 8 hours; the same error (in another staging directory) is
        // not repeated.
        let mut now = at(0);
        for (failures, wait) in [(2, 1), (3, 2), (4, 4)] {
            now += TimeDelta::hours(wait);
            updater.next = now;
            let said = updater.tick(now, &mut io);
            assert_eq!(io.installs.len(), failures as usize);
            assert!(said.is_empty(), "{:?}", texts(&said));
            assert_eq!(updater.state.install.failures, failures);
            assert_eq!(updater.state.next_attempt, Some(now + backoff(failures)));
        }
        // A day after the full report, one line says it is still failing.
        now += TimeDelta::hours(8);
        updater.next = now;
        let said = updater.tick(now, &mut io);
        assert!(now - at(0) < DAY);
        assert!(said.is_empty());
        now += TimeDelta::hours(16);
        updater.next = now;
        let said = updater.tick(now, &mut io);
        assert_eq!(
            texts(&said),
            [format!(
                "automatic update to v2.0.0 still failing (6 attempts since {}); next attempt after {}. Its error was shown in full earlier.",
                at(0).to_rfc3339(),
                (now + TimeDelta::hours(32)).to_rfc3339()
            )
            .as_str()]
        );
        // A different error is told in full at once; the backoff keeps counting.
        io.install = Box::new(|_| bail!("checksum mismatch for silicon.tar.gz"));
        now = updater.state.next_attempt.unwrap();
        updater.next = now;
        let said = updater.tick(now, &mut io);
        assert_eq!(said.len(), 1);
        assert!(said[0]
            .text
            .ends_with("checksum mismatch for silicon.tar.gz"));
        assert_eq!(updater.state.install.failures, 7);
        assert_eq!(updater.state.next_attempt, Some(now + backoff(7)));
        // Success after reported failures says so, then waits to restart.
        io.install = Box::new(|_| Ok(()));
        now = updater.state.next_attempt.unwrap();
        updater.next = now;
        let said = updater.tick(now, &mut io);
        assert_eq!(said.len(), 2, "{:?}", texts(&said));
        assert_eq!(
            said[0].text,
            format!(
                "automatic update to v2.0.0 succeeded after 7 failed attempts since {}",
                at(0).to_rfc3339()
            )
        );
        assert_eq!(updater.state.install, Trouble::default());
        assert!(updater.ready.is_some());
    }

    #[test]
    fn errors_that_alternate_are_shown_in_full_once_per_run() {
        let mut trouble = Trouble::default();
        assert_eq!(trouble.failed("a", at(0), LOUD), Tell::Full);
        assert_eq!(trouble.failed("b", at(1), LOUD), Tell::Full);
        assert_eq!(trouble.failed("a", at(2), LOUD), Tell::Nothing);
        assert_eq!(trouble.failed("b", at(3), LOUD), Tell::Nothing);
        assert_eq!(trouble.failed("a", at(25), LOUD), Tell::Daily);
        assert_eq!(trouble.failures, 5);
        assert_eq!(trouble.failing_since, Some(at(0)));
        // A clock set back does not hold the next line back by as much as it moved.
        let back = at(0) - TimeDelta::days(365);
        assert_eq!(trouble.failed("a", back, LOUD), Tell::Nothing);
        assert_eq!(trouble.failing_since, Some(back));
        assert_eq!(trouble.failed("a", back + DAY, LOUD), Tell::Daily);
        assert!(trouble.cleared().is_some());
        assert_eq!(trouble, Trouble::default());
        assert!(trouble.cleared().is_none(), "an untold run ends quietly");
    }

    #[test]
    fn a_new_release_and_a_clock_set_back_reset_the_backoff() {
        let mut io = Fake::new();
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.0"))));
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.0"))));
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.1"))));
        io.install = Box::new(|_| bail!("broken"));
        let mut updater = fresh(at(0));
        updater.state.install.failures = 7;
        updater.state.install.last_error_digest = Some(digest("installing failed: broken"));
        updater.state.tag = Some("v2.0.0".into());
        updater.state.next_attempt = Some(at(64));
        // The clock went back a year: the retry is at most one backoff away, not a year.
        let back = at(0) - TimeDelta::days(365);
        updater.next = back;
        updater.planned = back;
        updater.tick(back, &mut io);
        assert!(io.installs.is_empty());
        assert_eq!(updater.state.next_attempt, Some(back + backoff(7)));
        // A newer release is tried at once.
        updater.next = back + TimeDelta::hours(1);
        updater.tick(back + TimeDelta::hours(1), &mut io);
        assert!(io.installs.is_empty());
        updater.next = back + TimeDelta::hours(2);
        updater.tick(back + TimeDelta::hours(2), &mut io);
        assert_eq!(io.installs, ["v2.0.1"]);
        assert_eq!(updater.state.install.failures, 1);
    }

    #[test]
    fn a_release_that_needs_a_person_waits_until_the_installed_release_or_the_tag_changes() {
        let mut io = Fake::new();
        for _ in 0..4 {
            io.answers
                .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.0"))));
        }
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.1"))));
        io.install = Box::new(|_| {
            Err(anyhow::Error::new(NeedsPerson(
                "`sh` running install.sh failed: exit status: 77\noutput:\nsilicon install: Caddy needs port 80 permission".into(),
            ))
            .context("installing Silicon v2.0.0 from o/r failed"))
        });
        let mut updater = fresh(at(0));
        let said = updater.tick(at(0), &mut io);
        assert_eq!(io.installs.len(), 1);
        assert_eq!(said.len(), 1);
        assert!(
            said[0].text.starts_with("automatic update to v2.0.0 needs a person: run `/prefix/bin/silicon update` in a terminal, as a user who can use sudo, to install it.")
                && said[0].text.ends_with("Caddy needs port 80 permission"),
            "{}",
            said[0].text
        );
        assert_eq!(updater.state.blocked_tag.as_deref(), Some("v2.0.0"));
        assert_eq!(updater.state.blocked_version.as_deref(), Some("v1.0.0"));
        // Hourly checks, days later: no retry, nothing said.
        for hours in [1, 30] {
            updater.next = at(hours);
            assert!(updater.tick(at(hours), &mut io).is_empty());
        }
        assert_eq!(io.installs.len(), 1);
        // The installed release changed (someone ran an update by hand): tried again.
        io.installed.version = Some("v1.0.1".into());
        io.installed.running = true;
        updater.next = at(31);
        updater.tick(at(31), &mut io);
        assert_eq!(io.installs.len(), 2);
        // A newer tag is tried even while the old one is blocked.
        updater.next = at(32);
        updater.tick(at(32), &mut io);
        assert_eq!(io.installs, ["v2.0.0", "v2.0.0", "v2.0.1"]);
    }

    #[test]
    fn on_wsl_a_release_that_needs_a_person_points_to_the_windows_installer() {
        let mut io = Fake::new();
        io.wsl = true;
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v2.0.0"))));
        io.install = Box::new(|_| {
            Err(anyhow::Error::new(NeedsPerson(
                "`sh` running install.sh failed: exit status: 77\noutput:\nsilicon install: Caddy needs port 80 permission".into(),
            ))
            .context("installing Silicon v2.0.0 from o/r failed"))
        });
        let mut updater = fresh(at(0));
        let said = updater.tick(at(0), &mut io);
        assert_eq!(said.len(), 1);
        let text = &said[0].text;
        // The `silicon` user in the distribution has no sudo: the Windows installer of
        // exactly the blocked release is the way, with the full failure after it.
        assert!(
            text.starts_with("automatic update to v2.0.0 needs a person: on Windows, rerun the Windows installer of v2.0.0, which installs it and grants Caddy its port 80 permission: in PowerShell run `irm https://github.com/o/r/releases/download/v2.0.0/install.ps1 | iex`, or run install.ps1 from the v2.0.0 release.")
                && text.contains("the `silicon` user in the WSL distribution has no sudo")
                && !text.contains("as a user who can use sudo")
                && text.ends_with("Caddy needs port 80 permission"),
            "{text}"
        );
        assert_eq!(updater.state.blocked_tag.as_deref(), Some("v2.0.0"));
        // Elsewhere the step stays `silicon update` with sudo, the prefix quoted.
        assert_eq!(
            person_steps(Path::new("/my prefix/bin/silicon"), "o/r", "v2.0.0", false),
            "run `'/my prefix/bin/silicon' update` in a terminal, as a user who can use sudo, to install it"
        );
        // `silicon update` by hand in the distribution: the same way leads its error, and
        // the installer's full words follow.
        let failed = || {
            anyhow::Error::new(NeedsPerson(
                "`sh` running install.sh failed: exit status: 77".into(),
            ))
            .context("installing Silicon v2.0.0 from o/r failed")
        };
        let silicon = Path::new("/prefix/bin/silicon");
        let text = format!(
            "{:#}",
            for_a_person(failed(), silicon, "o/r", "v2.0.0", true)
        );
        assert!(
            text.starts_with("installing v2.0.0 needs a person: on Windows, rerun the Windows installer of v2.0.0")
                && text.ends_with(": installing Silicon v2.0.0 from o/r failed: `sh` running install.sh failed: exit status: 77"),
            "{text}"
        );
        assert_eq!(
            format!(
                "{:#}",
                for_a_person(failed(), silicon, "o/r", "v2.0.0", false)
            ),
            format!("{:#}", failed())
        );
        let other = anyhow!("download failed");
        assert_eq!(
            format!("{:#}", for_a_person(other, silicon, "o/r", "v2.0.0", true)),
            "download failed"
        );
    }

    fn no_network(text: &str) -> std::result::Result<Answer, Unasked> {
        Err(Unasked {
            error: anyhow!("cannot reach GitHub at https://x/latest: {text}"),
            offline: true,
        })
    }

    #[test]
    fn check_failures_are_told_once_and_an_unreachable_github_only_after_a_day() {
        let mut io = Fake::new();
        for hour in 0..30 {
            io.answers.push(no_network(if hour < 27 {
                "dns failed after 12 ms"
            } else {
                "connection refused"
            }));
        }
        let mut updater = fresh(at(0));
        let mut told = Vec::new();
        for hour in 0..30 {
            updater.next = at(hour);
            told.extend(
                updater
                    .tick(at(hour), &mut io)
                    .into_iter()
                    .map(|said| (hour, said.text)),
            );
        }
        // A day offline is told in full once; an error that changes afterwards is told in
        // full too.
        assert_eq!(told.len(), 2, "{told:?}");
        assert_eq!(told[0].0, 24);
        assert_eq!(
            told[0].1,
            format!(
                "automatic update check cannot reach GitHub (25 attempts since {}): cannot reach GitHub at https://x/latest: dns failed after 12 ms",
                at(0).to_rfc3339()
            )
        );
        assert_eq!(told[1].0, 27);
        assert_eq!(
            told[1].1,
            format!(
                "automatic update check cannot reach GitHub (28 attempts since {}): cannot reach GitHub at https://x/latest: connection refused",
                at(0).to_rfc3339()
            )
        );
        // Back online: said once, since the trouble had been told.
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v1.0.0"))));
        updater.next = at(30);
        let said = updater.tick(at(30), &mut io);
        assert_eq!(
            texts(&said),
            [format!(
                "automatic update check works again after 30 failed attempts since {}",
                at(0).to_rfc3339()
            )
            .as_str()]
        );
        // GitHub's rate limit: said in full, and the next check waits for its reset.
        let mut limited = answer(
            StatusCode::FORBIDDEN,
            None,
            r#"{"message":"API rate limit exceeded for 1.2.3.4."}"#,
        );
        limited.remaining = Some("0".into());
        let reset = at(31) + TimeDelta::hours(3);
        limited.reset = Some(reset.timestamp().to_string());
        let mut soon = answer(StatusCode::TOO_MANY_REQUESTS, None, "slow down");
        soon.retry_after = Some("60".into());
        io.answers.push(Ok(limited));
        io.answers.push(Ok(soon));
        updater.next = at(31);
        let said = updater.tick(at(31), &mut io);
        assert_eq!(said.len(), 1);
        assert!(
            said[0].text.starts_with("automatic update check failed: GitHub answered https://x/latest with HTTP 403 Forbidden:\n{\"message\":\"API rate limit exceeded"),
            "{}",
            said[0].text
        );
        assert!(updater.next >= reset && updater.next <= reset + TimeDelta::minutes(1));
        assert_eq!(updater.state.next_check, Some(updater.next));
        // A limit that lifts sooner than the next hourly check does not bring it forward.
        let said = updater.tick(updater.next, &mut io);
        assert_eq!(said.len(), 1, "a different failure is told");
        let asked = updater.planned;
        assert!(
            updater.next >= asked + TimeDelta::minutes(50),
            "{}",
            updater.next
        );
        let mut retry = answer(StatusCode::TOO_MANY_REQUESTS, None, "slow down");
        retry.retry_after = Some("120".into());
        let until = limited_until(&retry, at(0)).unwrap();
        assert!(until >= at(0) + TimeDelta::minutes(2) && until <= at(0) + TimeDelta::minutes(3));
        retry.retry_after = Some("99999999999".into());
        assert!(limited_until(&retry, at(0)).unwrap() <= at(0) + DAY + TimeDelta::minutes(1));
        // An ordinary 403 (no spent limit, no retry-after) waits the usual hour.
        let forbidden = answer(StatusCode::FORBIDDEN, None, "no");
        assert_eq!(limited_until(&forbidden, at(0)), None);
        // Settings that cannot be read are told once, not every hour.
        let mut updater = fresh(at(0));
        let mut io = Broken;
        let first = updater.tick(at(0), &mut io);
        assert_eq!(
            texts(&first),
            ["automatic update skipped: cannot load settings: invalid interpreter settings in s.json: expected value at line 1 column 1"]
        );
        updater.next = at(1);
        assert!(updater.tick(at(1), &mut io).is_empty());
        updater.next = at(25);
        assert_eq!(updater.tick(at(25), &mut io).len(), 1);
    }

    struct Broken;
    impl Io for Broken {
        fn auto_update(&mut self) -> Result<bool> {
            bail!("invalid interpreter settings in s.json: expected value at line 1 column 1")
        }
        fn installed(&mut self) -> Installed {
            unreachable!()
        }
        fn fetch(&mut self, _: Option<&str>) -> std::result::Result<Answer, Unasked> {
            unreachable!()
        }
        fn install(&mut self, _: &str) -> Result<()> {
            unreachable!()
        }
        fn by_hand(&self, _: &str) -> String {
            unreachable!()
        }
    }

    #[test]
    fn a_day_asleep_is_not_a_day_offline_and_tls_failures_are_told_at_once() {
        // Offline just before two days asleep, and on the first check after waking (before
        // the network is back): two attempts are not a day offline.
        let mut io = Fake::new();
        io.answers.push(no_network("dns failed"));
        io.answers.push(no_network("dns failed"));
        let mut updater = fresh(at(0));
        assert!(updater.tick(at(0), &mut io).is_empty());
        updater.next = at(49);
        assert!(updater.tick(at(49), &mut io).is_empty());
        assert_eq!(updater.state.check.failures, 2);
        // A certificate or proxy failure is not the network being away: told at once.
        io.answers.push(Err(Unasked::new(
            ureq::Error::Tls("invalid peer certificate"),
            "cannot reach GitHub at https://x/latest".into(),
        )));
        updater.next = at(50);
        let said = updater.tick(at(50), &mut io);
        assert_eq!(said.len(), 1);
        assert!(
            said[0].text.starts_with(
                "automatic update check failed: cannot reach GitHub at https://x/latest: "
            ) && said[0].text.contains("invalid peer certificate"),
            "{}",
            said[0].text
        );

        use std::io::{Error as IoError, ErrorKind};
        for (error, expected) in [
            (ureq::Error::HostNotFound, true),
            (ureq::Error::ConnectionFailed, true),
            (ureq::Error::Timeout(ureq::Timeout::Connect), true),
            (
                ureq::Error::Io(IoError::new(ErrorKind::ConnectionRefused, "refused")),
                true,
            ),
            (
                ureq::Error::Io(IoError::new(ErrorKind::NetworkUnreachable, "no route")),
                true,
            ),
            (
                ureq::Error::Io(IoError::other("failed to lookup address information")),
                true,
            ),
            (
                ureq::Error::Io(IoError::new(ErrorKind::PermissionDenied, "sandbox")),
                false,
            ),
            (ureq::Error::Tls("invalid peer certificate"), false),
            (
                ureq::Error::ConnectProxyFailed("407 Proxy Authentication Required".into()),
                false,
            ),
            (ureq::Error::TooManyRedirects, false),
        ] {
            assert_eq!(offline(&error), expected, "{error}");
        }
    }

    #[test]
    fn a_panicking_check_is_reported_and_the_updater_goes_on() {
        let mut updater = fresh(at(0));
        let mut io = Fake::new();
        // No scripted answer: fetching panics, as any bug in a check might.
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            updater.tick(at(0), &mut io)
        }))
        .unwrap_err();
        let said = updater.panicked(at(0), crate::failure::panic_message(&*panic));
        assert_eq!(said.len(), 1);
        assert!(
            said[0]
                .text
                .starts_with("automatic update check panicked: "),
            "{}",
            said[0].text
        );
        assert!(updater.next > at(0));
        io.answers
            .push(Ok(answer(StatusCode::OK, None, &latest("v1.0.0"))));
        updater.next = at(1);
        updater.tick(at(1), &mut io);
        assert_eq!(updater.state.last_check, Some(at(1)));
    }

    #[test]
    fn state_round_trips_and_digests_ignore_what_changes_every_run() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("update-state.json");
        assert_eq!(load_state(&path), (State::default(), None));
        let state = State {
            last_check: Some(at(0)),
            etag: Some("W/\"x\"".into()),
            tag: Some("v2.0.0".into()),
            install: Trouble {
                failures: 3,
                last_error_digest: Some("abc".into()),
                failing_since: Some(at(-5)),
                last_report: Some(at(-5)),
                reported_digests: vec!["abc".into()],
            },
            next_attempt: Some(at(4)),
            blocked_tag: None,
            blocked_version: None,
            next_check: None,
            check: Trouble::default(),
        };
        state::write_json(&path, &state).unwrap();
        let stored: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        for key in [
            "last_check",
            "etag",
            "tag",
            "failures",
            "next_attempt",
            "last_error_digest",
            "blocked_tag",
        ] {
            assert!(stored.get(key).is_some(), "{key} missing from {stored}");
        }
        assert_eq!(load_state(&path), (state, None));
        fs::write(&path, "{not json").unwrap();
        let (state, problem) = load_state(&path);
        assert_eq!(state, State::default());
        assert!(problem.unwrap().contains("it held:\n{not json"));
        // A file from a newer release keeps what this one knows.
        fs::write(&path, r#"{"tag":"v3.0.0","later":true}"#).unwrap();
        assert_eq!(load_state(&path).0.tag.as_deref(), Some("v3.0.0"));

        assert_eq!(
            digest("could not store release v2.0.0-Ab3xQ in /p/.install.Zk82Lq (pid 4411)"),
            digest("could not store release v2.0.0-9mNq in /p/.install.P0o1 (pid 17)")
        );
        assert_ne!(digest("checksum mismatch"), digest("lock held"));
        // Durations, sizes and times change every run; statuses say what went wrong.
        let curl = |status: &str, took: &str| {
            format!("`sh` running install.sh failed: exit status: 1\noutput:\nsilicon install: could not download\n`curl --max-time 3600 https://x/a.tar.gz` failed: exit status: 22\nstderr:\ncurl: (22) The requested URL returned error: {status} after {took} ms at 2027-01-15T08:00:12Z")
        };
        assert_eq!(digest(&curl("404", "31")), digest(&curl("404", "2210")));
        assert_ne!(digest(&curl("404", "31")), digest(&curl("403", "31")));
        for (one, other) in [
            ("exit status: 1", "exit status: 77"),
            ("curl: (6) failed", "curl: (7) failed"),
            ("signal: 9", "signal: 15"),
            (
                "Permission denied (os error 13)",
                "Permission denied (os error 1)",
            ),
            (
                "GitHub answered with HTTP 500",
                "GitHub answered with HTTP 503",
            ),
            ("HTTP/2 502", "HTTP/2 504"),
        ] {
            assert_ne!(digest(one), digest(other), "{one} / {other}");
        }
    }

    fn release_dir(releases: &Path, name: &str, age: u64) -> PathBuf {
        let dir = releases.join(name);
        fs::create_dir_all(dir.join("bin")).unwrap();
        let modified = SystemTime::now() - Duration::from_secs(age);
        File::open(&dir).unwrap().set_modified(modified).unwrap();
        dir
    }

    #[test]
    fn pruning_keeps_current_running_rollback_and_live_interpreters() {
        let dir = tempfile::tempdir().unwrap();
        let runtime = dir.path().join("lib/silicon");
        let releases = runtime.join("releases");
        let oldest = release_dir(&releases, "v1.0.0-aaaaaa", 5000);
        let older = release_dir(&releases, "v1.1.0-bbbbbb", 4000);
        let rollback = release_dir(&releases, "v1.2.0-cccccc", 3000);
        let running = release_dir(&releases, "v1.0.1-dddddd", 6000);
        let current = release_dir(&releases, "v1.3.0-eeeeee", 100);
        let live = release_dir(&releases, "v0.9.0-ffffff", 7000);
        let dead = release_dir(&releases, "v0.8.0-gggggg", 7000);
        std::os::unix::fs::symlink("releases/v1.3.0-eeeeee", runtime.join("current")).unwrap();
        // Another live interpreter (this test process) and a record of one that is gone.
        let records = runtime.join("running");
        mark_running(&records, &live).unwrap();
        let mut sleeper = Command::new("sleep").arg("0").spawn().unwrap();
        let gone = sleeper.id();
        sleeper.wait().unwrap();
        fs::write(records.join(gone.to_string()), "v0.8.0-gggggg\n").unwrap();
        let stale = runtime.join(".install.Old123");
        fs::create_dir_all(stale.join("payload")).unwrap();
        File::open(&stale)
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(7200))
            .unwrap();
        let fresh = runtime.join(".install.New456");
        fs::create_dir_all(&fresh).unwrap();

        // While an installer holds the lock, or is reclaiming a stale one, nothing is touched.
        for lock in [".install-lock", ".install-lock.reclaim"] {
            fs::create_dir(runtime.join(lock)).unwrap();
            assert!(prune(&runtime, &running).unwrap().is_empty());
            assert!(oldest.exists() && stale.exists());
            fs::remove_dir(runtime.join(lock)).unwrap();
        }

        let mut removed = prune(&runtime, &running).unwrap();
        removed.sort();
        let mut expected = vec![oldest, older, dead, stale];
        expected.sort();
        assert_eq!(removed, expected);
        for kept in [&rollback, &running, &current, &live, &fresh] {
            assert!(kept.exists(), "{} was removed", kept.display());
        }
        assert!(!records.join(gone.to_string()).exists());
        assert!(records.join(std::process::id().to_string()).exists());
        // Pruning again removes nothing more.
        assert!(prune(&runtime, &running).unwrap().is_empty());

        // Without a readable `current` nothing is removed.
        fs::remove_file(runtime.join("current")).unwrap();
        let error = format!("{:#}", prune(&runtime, &running).unwrap_err());
        assert!(error.contains("so no release was pruned"), "{error}");
        std::os::unix::fs::symlink("releases/missing", runtime.join("current")).unwrap();
        let error = format!("{:#}", prune(&runtime, &running).unwrap_err());
        assert!(error.contains("which is not a release in"), "{error}");
        assert!(rollback.exists());
    }

    #[test]
    fn records_of_interpreters_that_are_gone_are_removed_at_every_start() {
        let dir = tempfile::tempdir().unwrap();
        let records = dir.path().join("running");
        fs::create_dir_all(&records).unwrap();
        for _ in 0..3 {
            fs::write(records.join(dead_pid().to_string()), "v0.1.0-aaaaaa\n").unwrap();
        }
        mark_running(&records, Path::new("/p/lib/silicon/releases/v1.0.0-bbbbbb")).unwrap();
        let mine = std::process::id().to_string();
        assert_eq!(names(&records), [mine.as_str()]);
        assert_eq!(
            fs::read_to_string(records.join(&mine)).unwrap(),
            "v1.0.0-bbbbbb\n"
        );
    }

    #[test]
    fn installed_reads_current_and_knows_its_own_release() {
        let dir = tempfile::tempdir().unwrap();
        let runtime = dir.path().join("lib/silicon");
        let mine = release_dir(&runtime.join("releases"), "v1.0.0-aaaaaa", 0);
        let other = release_dir(&runtime.join("releases"), "v1.1.0-bbbbbb", 0);
        assert_eq!(installed(dir.path(), &mine), Installed::default());
        fs::write(other.join("VERSION"), "v1.1.0\n").unwrap();
        std::os::unix::fs::symlink("releases/v1.1.0-bbbbbb", runtime.join("current")).unwrap();
        assert_eq!(
            installed(dir.path(), &mine),
            Installed {
                version: Some("v1.1.0".into()),
                running: false
            }
        );
        assert!(installed(dir.path(), &other).running);
    }

    /// A prefix to run the real install.sh in, with a fake bundle behind a fake curl.
    struct Bench {
        root: tempfile::TempDir,
        mocks: PathBuf,
        downloads: PathBuf,
    }

    const COMMANDS: [&str; 7] = [
        "silicon",
        "si",
        "omnid",
        "silicon-omni",
        "omni",
        "so",
        "caddy",
    ];
    const NOTICES: [&str; 5] = [
        "LICENSE",
        "LICENSES/README.md",
        "LICENSES/omni-LICENSE.txt",
        "LICENSES/caddy-LICENSE.txt",
        "LICENSES/caddy-AUTHORS.txt",
    ];

    fn script(path: &Path, body: &str) {
        use std::os::unix::fs::PermissionsExt;
        fs::write(path, body).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn output(command: &mut Command) -> String {
        let output = command.output().unwrap();
        assert!(output.status.success(), "{output:?}");
        String::from_utf8(output.stdout).unwrap().trim().to_owned()
    }

    fn sha256(path: &Path) -> String {
        let hashed = Command::new("sha256sum")
            .arg(path)
            .output()
            .ok()
            .filter(|output| output.status.success())
            .unwrap_or_else(|| {
                Command::new("shasum")
                    .args(["-a", "256"])
                    .arg(path)
                    .output()
                    .unwrap()
            });
        String::from_utf8(hashed.stdout).unwrap()[..64].to_owned()
    }

    impl Bench {
        /// `system` and `machine` as `uname` reports them for the bundle to build.
        fn new(system: &str, machine: &str) -> Self {
            let root = tempfile::tempdir().unwrap();
            let mocks = root.path().join("mocks");
            let downloads = root.path().join("downloads");
            let bundle = root.path().join("bundle");
            for dir in [
                &mocks,
                &downloads,
                &bundle.join("bin"),
                &bundle.join("LICENSES"),
            ] {
                fs::create_dir_all(dir).unwrap();
            }
            script(
                &mocks.join("curl"),
                "#!/bin/sh\nout= url=\nwhile [ \"$#\" -gt 0 ]; do\n if [ \"$1\" = --output ]; then out=$2; shift 2; else url=$1; shift; fi\ndone\nexec cp \"$MOCK_DOWNLOADS/${url##*/}\" \"$out\"\n",
            );
            script(
                &mocks.join("cat"),
                "#!/bin/sh\nif [ \"$1\" = /proc/sys/net/ipv4/ip_unprivileged_port_start ]; then echo \"${MOCK_PORT_START:-0}\"; exit 0; fi\nexec /bin/cat \"$@\"\n",
            );
            let (target, honeycomb) = match (system, machine) {
                ("Darwin", "arm64") => ("aarch64-apple-darwin", "macos-aarch64"),
                ("Darwin", "x86_64") => ("x86_64-apple-darwin", "macos-x86_64"),
                ("Linux", "x86_64") => ("x86_64-unknown-linux-gnu", "linux-x86_64"),
                ("Linux", _) => ("aarch64-unknown-linux-gnu", "linux-aarch64"),
                other => panic!("unsupported test host {other:?}"),
            };
            for name in COMMANDS {
                script(
                    &bundle.join("bin").join(name),
                    "#!/bin/sh\necho test-binary\n",
                );
            }
            fs::write(bundle.join("VERSION"), "v9.9.9\n").unwrap();
            fs::copy(
                concat!(env!("CARGO_MANIFEST_DIR"), "/install.sh"),
                bundle.join("installer.sh"),
            )
            .unwrap();
            for notice in NOTICES {
                fs::copy(
                    Path::new(env!("CARGO_MANIFEST_DIR")).join(notice),
                    bundle.join(notice),
                )
                .unwrap();
            }
            let asset = format!("silicon-{target}.tar.gz");
            let tar = |archive: &Path, from: &Path, members: &[&str]| {
                output(
                    Command::new("tar")
                        .env("COPYFILE_DISABLE", "1")
                        .arg("-czf")
                        .arg(archive)
                        .arg("-C")
                        .arg(from)
                        .args(members),
                );
            };
            tar(
                &downloads.join(&asset),
                &bundle,
                &["bin", "VERSION", "installer.sh", "LICENSE", "LICENSES"],
            );
            fs::write(
                downloads.join("SHA256SUMS"),
                format!("{}  {asset}\n", sha256(&downloads.join(&asset))),
            )
            .unwrap();
            let hive = root.path().join("honeycomb");
            fs::create_dir_all(&hive).unwrap();
            script(&hive.join("honeycomb"), "#!/bin/sh\nexit 0\n");
            let asset = format!("honeycomb-{honeycomb}.tar.gz");
            tar(&downloads.join(&asset), &hive, &["honeycomb"]);
            fs::write(
                downloads.join(format!("{asset}.sha256")),
                format!("{}  {asset}\n", sha256(&downloads.join(&asset))),
            )
            .unwrap();
            Bench {
                root,
                mocks,
                downloads,
            }
        }

        fn host() -> Self {
            Bench::new(
                &output(Command::new("uname").arg("-s")),
                &output(Command::new("uname").arg("-m")),
            )
        }

        fn prefix(&self) -> PathBuf {
            self.root.path().canonicalize().unwrap().join("prefix")
        }

        fn runtime(&self) -> PathBuf {
            self.prefix().join("lib/silicon")
        }

        fn install(&self, extra: &[(&str, &OsString)]) -> std::process::Output {
            self.command(extra).output().unwrap()
        }

        fn command(&self, extra: &[(&str, &OsString)]) -> Command {
            let path = std::env::join_paths(
                std::iter::once(self.mocks.clone())
                    .chain(std::env::split_paths(&std::env::var_os("PATH").unwrap())),
            )
            .unwrap();
            let mut command = Command::new("sh");
            command
                .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/install.sh"))
                .stdin(Stdio::null())
                .env("SILICON_PREFIX", self.prefix())
                .env("SILICON_VERSION", "v9.9.9")
                .env("SILICON_NONINTERACTIVE", "1")
                .env("HOME", self.root.path())
                .env("PATH", path)
                .env("MOCK_DOWNLOADS", &self.downloads);
            for name in [
                "SILICON_SOURCE_DIR",
                "SILICON_GIT_REV",
                "SILICON_DEPENDENCY_BIN_DIR",
                "SILICON_RELEASE_BASE_URL",
                "SILICON_REPOSITORY",
                "SILICON_KEEP_RELEASE",
            ] {
                command.env_remove(name);
            }
            for (name, value) in extra {
                command.env(name, value);
            }
            command
        }

        /// A lock as an installer leaves it: owned by `pid`, which started at `start`, taken
        /// `age` ago.
        fn lock(&self, pid: u32, start: Option<&str>, age: Duration) {
            let lock = self.runtime().join(".install-lock");
            fs::create_dir_all(&lock).unwrap();
            fs::write(lock.join("pid"), format!("{pid}\n")).unwrap();
            if let Some(start) = start {
                fs::write(lock.join("start"), format!("{start}\n")).unwrap();
            }
            File::open(&lock)
                .unwrap()
                .set_modified(SystemTime::now() - age)
                .unwrap();
        }
    }

    /// When `pid` started, in the words of install.sh's own `process_start`.
    fn start_of(pid: u32) -> String {
        let script =
            fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/install.sh")).unwrap();
        let from = script.find("\nprocess_start() {").unwrap() + 1;
        let to = from + script[from..].find("\n}\n").unwrap() + 3;
        output(
            Command::new("sh")
                .arg("-c")
                .arg(format!("{}process_start {pid}", &script[from..to])),
        )
    }

    fn dead_pid() -> u32 {
        let mut child = Command::new("true").spawn().unwrap();
        let pid = child.id();
        child.wait().unwrap();
        pid
    }

    fn names(dir: &Path) -> Vec<String> {
        let mut names: Vec<_> = fs::read_dir(dir)
            .unwrap()
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    #[test]
    fn the_installer_parses_as_a_posix_shell_script() {
        let checked = Command::new("sh")
            .arg("-n")
            .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/install.sh"))
            .output()
            .unwrap();
        assert!(checked.status.success(), "{checked:?}");
    }

    #[test]
    fn the_installer_reclaims_a_stale_lock_and_prunes_what_nothing_needs() {
        let bench = Bench::host();
        let runtime = bench.runtime();
        let releases = runtime.join("releases");
        for name in [
            "v0.1.0-old111",
            "v0.2.0-prev22",
            "v0.3.0-keep33",
            "v0.4.0-live44",
            "v0.5.0-dead55",
        ] {
            fs::create_dir_all(releases.join(name).join("bin")).unwrap();
        }
        std::os::unix::fs::symlink("releases/v0.2.0-prev22", runtime.join("current")).unwrap();
        let records = runtime.join("running");
        fs::create_dir_all(&records).unwrap();
        fs::write(
            records.join(std::process::id().to_string()),
            "v0.4.0-live44\n",
        )
        .unwrap();
        let dead = dead_pid();
        fs::write(records.join(dead.to_string()), "v0.5.0-dead55\n").unwrap();
        let stale = runtime.join(".install.oldstg");
        fs::create_dir_all(stale.join("payload")).unwrap();
        File::open(&stale)
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(7200))
            .unwrap();
        let fresh = runtime.join(".install.newstg");
        fs::create_dir_all(&fresh).unwrap();
        bench.lock(dead_pid(), None, Duration::from_secs(300));

        let keep = OsString::from(releases.join("v0.3.0-keep33/"));
        let result = bench.install(&[("SILICON_KEEP_RELEASE", &keep)]);
        let stdout = String::from_utf8_lossy(&result.stdout);
        assert!(result.status.success(), "{result:?}");
        for said in [
            "silicon install: removing a stale installer lock",
            "is not running",
            "silicon install: removed old release v0.1.0-old111",
            "silicon install: removed old release v0.5.0-dead55",
            &format!(
                "silicon install: removed the staging directory {}",
                stale.display()
            ),
        ] {
            assert!(stdout.contains(said), "missing {said:?} in {stdout}");
        }
        let active = fs::read_link(runtime.join("current")).unwrap();
        let active = active.file_name().unwrap().to_string_lossy().into_owned();
        assert!(active.starts_with("v9.9.9-"), "{active}");
        let mut expected = vec![
            active,
            "v0.2.0-prev22".to_owned(),
            "v0.3.0-keep33".to_owned(),
            "v0.4.0-live44".to_owned(),
        ];
        expected.sort();
        assert_eq!(names(&releases), expected);
        assert_eq!(names(&records), [std::process::id().to_string()]);
        assert!(!runtime.join(".install-lock").exists());
        assert!(!stale.exists() && fresh.exists());
        assert!(bench.prefix().join("bin/silicon").exists());
    }

    #[test]
    fn the_installer_leaves_a_lock_that_may_still_be_held() {
        let bench = Bench::host();
        let lock = bench.runtime().join(".install-lock");
        // A running installer holds it, whatever its process is called: a saved script run
        // by its own name is not "sh".
        let mut holder = Command::new("sleep").arg("30").spawn().unwrap();
        bench.lock(
            holder.id(),
            Some(&start_of(holder.id())),
            Duration::from_secs(300),
        );
        let result = bench.install(&[]);
        let _ = holder.kill();
        let _ = holder.wait();
        let stderr = String::from_utf8_lossy(&result.stderr);
        assert_eq!(result.status.code(), Some(1), "{result:?}");
        assert!(
            stderr.contains(&format!(
                "silicon install: another installer may hold {} (installer process {} is running); if none is running, remove that directory and retry\n`mkdir ",
                lock.display(),
                holder.id()
            )),
            "{stderr}"
        );
        assert!(lock.join("pid").exists(), "the holder's lock is untouched");
        assert!(!bench.runtime().join(".install-lock.reclaim").exists());
        // A lock taken moments ago is not judged, even when its process is gone.
        fs::remove_dir_all(&lock).unwrap();
        bench.lock(dead_pid(), None, Duration::ZERO);
        let result = bench.install(&[]);
        assert_eq!(result.status.code(), Some(1), "{result:?}");
        assert!(String::from_utf8_lossy(&result.stderr)
            .contains("(it was taken less than a minute ago)"));
        // Its number now belongs to a process that started later: not the installer.
        fs::remove_dir_all(&lock).unwrap();
        bench.lock(
            std::process::id(),
            Some("an earlier start"),
            Duration::from_secs(300),
        );
        let result = bench.install(&[]);
        assert!(result.status.success(), "{result:?}");
        assert!(String::from_utf8_lossy(&result.stdout).contains(&format!(
            "silicon install: removing a stale installer lock {}: process {} is not the installer that took it: that one started at an earlier start, this one at {}\n",
            lock.display(),
            std::process::id(),
            start_of(std::process::id())
        )));
        // An owner whose start was not recorded cannot be checked: it waits two hours.
        bench.lock(std::process::id(), None, Duration::from_secs(3600));
        let result = bench.install(&[]);
        assert_eq!(result.status.code(), Some(1), "{result:?}");
        assert!(String::from_utf8_lossy(&result.stderr).contains(&format!(
            "(it does not record when installer process {} started, and it is under two hours old)",
            std::process::id()
        )));
        File::open(&lock)
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(3 * 3600))
            .unwrap();
        let result = bench.install(&[]);
        assert!(result.status.success(), "{result:?}");
        assert!(String::from_utf8_lossy(&result.stdout).contains(&format!(
            "it does not record when installer process {} started, and it is over two hours old",
            std::process::id()
        )));
        // An older installer's lock (no pid) is stale only after two hours.
        fs::create_dir_all(&lock).unwrap();
        File::open(&lock)
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(3600))
            .unwrap();
        let result = bench.install(&[]);
        assert_eq!(result.status.code(), Some(1), "{result:?}");
        File::open(&lock)
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(3 * 3600))
            .unwrap();
        let result = bench.install(&[]);
        assert!(result.status.success(), "{result:?}");
        assert!(String::from_utf8_lossy(&result.stdout)
            .contains("it names no installer process, and it is over two hours old"));
    }

    #[test]
    fn two_installers_never_both_reclaim_one_stale_lock() {
        let bench = Bench::host();
        let runtime = bench.runtime();
        let reclaim = runtime.join(".install-lock.reclaim");
        // A slow ps holds a reclaimer between judging the lock and replacing it.
        let ps = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|dir| dir.join("ps"))
            .find(|path| path.is_file())
            .unwrap();
        script(
            &bench.mocks.join("ps"),
            &format!("#!/bin/sh\nsleep 1\nexec '{}' \"$@\"\n", ps.display()),
        );
        bench.lock(dead_pid(), None, Duration::from_secs(300));
        // The second starts while the first is judging, as a manual `silicon update` might
        // while the updater's installer runs: both find the lock stale.
        let runs: Vec<_> = (0..2)
            .map(|run| {
                thread::sleep(Duration::from_millis(500 * run));
                bench
                    .command(&[])
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .unwrap()
            })
            .collect();
        let results: Vec<_> = runs
            .into_iter()
            .map(|run| run.wait_with_output().unwrap())
            .collect();
        let (won, lost): (Vec<_>, Vec<_>) =
            results.iter().partition(|result| result.status.success());
        assert_eq!((won.len(), lost.len()), (1, 1), "{results:?}");
        let stderr = String::from_utf8_lossy(&lost[0].stderr);
        assert!(
            stderr.contains("another installer is reclaiming")
                || stderr.contains("(it was taken less than a minute ago)"),
            "{stderr}"
        );
        let installed: Vec<_> = names(&runtime.join("releases"))
            .into_iter()
            .filter(|name| name.starts_with("v9.9.9-"))
            .collect();
        assert_eq!(installed.len(), 1, "{installed:?}");
        assert!(!runtime.join(".install-lock").exists() && !reclaim.exists());

        // A turn at reclaiming lasts moments: a fresh one is waited for, an old one was left
        // by a reclaimer that was killed.
        bench.lock(dead_pid(), None, Duration::from_secs(300));
        fs::create_dir(&reclaim).unwrap();
        let result = bench.install(&[]);
        assert_eq!(result.status.code(), Some(1), "{result:?}");
        assert!(String::from_utf8_lossy(&result.stderr).contains(&format!(
            "another installer is reclaiming {} right now; if none is running, remove {} and retry",
            runtime.join(".install-lock").display(),
            reclaim.display()
        )));
        File::open(&reclaim)
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(300))
            .unwrap();
        let result = bench.install(&[]);
        assert!(result.status.success(), "{result:?}");
        let stdout = String::from_utf8_lossy(&result.stdout);
        assert!(
            stdout.contains(&format!(
                "removed {}, left by an installer stopped while it reclaimed",
                reclaim.display()
            )) && stdout.contains("is not running"),
            "{stdout}"
        );
        assert!(!reclaim.exists());
    }

    /// A function as windows/provision.sh defines it: one line, or up to its closing `}`.
    fn provision_function(script: &str, name: &str) -> String {
        let from = script
            .find(&format!("\n{name}() {{"))
            .unwrap_or_else(|| panic!("windows/provision.sh defines no {name}()"))
            + 1;
        let line = &script[from..from + script[from..].find('\n').unwrap() + 1];
        if line.trim_end().ends_with('}') {
            return line.to_owned();
        }
        let to = from + script[from..].find("\n}\n").unwrap() + 3;
        script[from..to].to_owned()
    }

    /// provision.sh's own lock taking and recording, run against `runtime` as it runs them
    /// (under `set -eu`), without the chown to `silicon` that only root can do.
    fn provision_lock(runtime: &Path) -> std::process::Output {
        let script =
            fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/windows/provision.sh"))
                .unwrap();
        let mut body = String::from("set -eu\n");
        for name in [
            "fail",
            "command_line",
            "run",
            "say",
            "boot_id",
            "process_running",
            "process_start",
            "lock_is_stale",
            "take_lock",
            "record_owner",
        ] {
            body.push_str(&provision_function(&script, name));
        }
        body.push_str(
            "runtime=$1\nlock=\"$runtime/.install-lock\"\nreclaim=\"$lock.reclaim\"\nlock_owned=false\nreclaiming=false\ntake_lock\nrecord_owner\nprintf 'taken by %s\\n' \"$$\"\n",
        );
        Command::new("sh")
            .arg("-c")
            .arg(body)
            .arg("provision")
            .arg(runtime)
            .output()
            .unwrap()
    }

    #[test]
    fn provisioning_reclaims_a_stale_installer_lock_by_the_installers_rules() {
        let syntax = Command::new("sh")
            .arg("-n")
            .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/windows/provision.sh"))
            .output()
            .unwrap();
        assert!(syntax.status.success(), "{syntax:?}");
        let bench = Bench::host();
        let runtime = bench.runtime();
        fs::create_dir_all(&runtime).unwrap();
        let lock = runtime.join(".install-lock");
        let owner = || fs::read_to_string(lock.join("pid")).unwrap();
        // Free: taken and recorded.
        let taken = provision_lock(&runtime);
        assert!(taken.status.success(), "{taken:?}");
        let stdout = String::from_utf8_lossy(&taken.stdout);
        assert_eq!(stdout, format!("taken by {}", owner()));
        // Left by an installer that is gone (the updater's install.sh, stopped by
        // `wsl --shutdown`): removed as root and taken once more.
        fs::remove_dir_all(&lock).unwrap();
        bench.lock(dead_pid(), None, Duration::from_secs(300));
        let reclaimed = provision_lock(&runtime);
        let stderr = String::from_utf8_lossy(&reclaimed.stderr);
        assert!(reclaimed.status.success(), "{reclaimed:?}");
        assert!(
            stderr.contains(&format!(
                "Silicon WSL provisioning: removing a stale installer lock {}, by the rules install.sh uses: installer process",
                lock.display()
            )) && stderr.contains("is not running"),
            "{stderr}"
        );
        assert_eq!(
            String::from_utf8_lossy(&reclaimed.stdout),
            format!("taken by {}", owner())
        );
        assert!(!runtime.join(".install-lock.reclaim").exists());
        // A running process that took it before this boot started is not its owner.
        fs::remove_dir_all(&lock).unwrap();
        bench.lock(std::process::id(), None, Duration::from_secs(300));
        fs::write(lock.join("boot"), "a-boot-before-this-one\n").unwrap();
        File::open(&lock)
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(300))
            .unwrap();
        let reclaimed = provision_lock(&runtime);
        assert!(reclaimed.status.success(), "{reclaimed:?}");
        assert!(
            String::from_utf8_lossy(&reclaimed.stderr)
                .contains("ran before this machine last started"),
            "{reclaimed:?}"
        );
        // A live installer's lock, and one taken moments ago, are left alone.
        for (pid, start, age, why) in [
            (
                std::process::id(),
                Some(start_of(std::process::id())),
                Duration::from_secs(300),
                "",
            ),
            (
                dead_pid(),
                None,
                Duration::ZERO,
                "(it was taken less than a minute ago)",
            ),
        ] {
            fs::remove_dir_all(&lock).unwrap();
            bench.lock(pid, start.as_deref(), age);
            let refused = provision_lock(&runtime);
            let stderr = String::from_utf8_lossy(&refused.stderr);
            assert_eq!(refused.status.code(), Some(2), "{refused:?}");
            assert!(
                stderr.contains(&format!(
                    "Silicon WSL provisioning: another installer holds {} (",
                    lock.display()
                )) && stderr.contains(why)
                    && stderr.contains(
                        "if none is running, remove that directory and rerun the Windows installer"
                    ),
                "{stderr}"
            );
            assert_eq!(owner(), format!("{pid}\n"));
            assert!(!runtime.join(".install-lock.reclaim").exists());
        }
    }

    #[test]
    fn the_installer_exits_77_when_caddys_port_80_needs_a_person() {
        let bench = Bench::new("Linux", "x86_64");
        script(
            &bench.mocks.join("uname"),
            "#!/bin/sh\ncase \"$1\" in -s) echo Linux ;; -m) echo x86_64 ;; *) exit 1 ;; esac\n",
        );
        script(&bench.mocks.join("id"), "#!/bin/sh\necho 1000\n");
        script(&bench.mocks.join("getcap"), "#!/bin/sh\nexit 0\n");
        script(&bench.mocks.join("setcap"), "#!/bin/sh\nexit 0\n");
        script(
            &bench.mocks.join("sudo"),
            "#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n",
        );
        let port = OsString::from("1024");
        let result = bench.install(&[("MOCK_PORT_START", &port)]);
        let stderr = String::from_utf8_lossy(&result.stderr);
        assert_eq!(result.status.code(), Some(NEEDS_PERSON), "{result:?}");
        assert!(
            stderr.contains("silicon install: Caddy needs port 80 permission; rerun this installation in a terminal with sudo access. The active release was not changed")
                && stderr.contains("sudo: a password is required"),
            "{stderr}"
        );
        assert!(!bench.runtime().join("current").exists());
        assert!(!bench.runtime().join(".install-lock").exists());
    }
}
