//! Hourly GitHub releases, installed through the same verified bundle path as curl + sh.
use crate::{runtime::Runtime, state};
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::{Duration, Instant};

fn version(tag: &str) -> Option<[u64; 3]> {
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

pub fn managed_prefix() -> Result<PathBuf> {
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
    Ok(prefix)
}

/// Returns the new executable after the complete bundle has been verified and activated.
pub fn install_latest() -> Result<Option<PathBuf>> {
    install(false)
}

fn install(noninteractive: bool) -> Result<Option<PathBuf>> {
    let prefix = managed_prefix().context("updates require a managed bundle installation")?;
    let repository = std::env::var("SILICON_REPOSITORY")
        .unwrap_or_else(|_| "teamofsilicons/silicon-stemcell".into());
    if repository.split('/').count() != 2
        || !repository
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-._/".contains(&c))
    {
        bail!("invalid SILICON_REPOSITORY {repository:?}; expected owner/name");
    }
    let url = format!("https://api.github.com/repos/{repository}/releases/latest");
    // Statuses are read here, not raised by ureq, so GitHub's own explanation survives.
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(30)))
        .http_status_as_error(false)
        .build()
        .new_agent();
    let mut response = agent
        .get(&url)
        .header("User-Agent", concat!("silicon/", env!("CARGO_PKG_VERSION")))
        .header("Accept", "application/vnd.github+json")
        .call()
        .with_context(|| format!("cannot reach GitHub at {url}"))?;
    let status = response.status();
    let body = response
        .body_mut()
        .read_to_string()
        .with_context(|| format!("cannot read GitHub's {status} answer from {url}"))?;
    let release = release(&url, status, &body)?;
    let Some(tag) = newer(&url, &release, &body)? else {
        return Ok(None);
    };
    let dir = crate::server::directory();
    state::private_dir(&dir)?;
    let mut command = Command::new("sh");
    command
        .env("SILICON_PREFIX", &prefix)
        .env("SILICON_VERSION", tag)
        .env(
            "SILICON_NONINTERACTIVE",
            if noninteractive { "1" } else { "0" },
        )
        .env("SILICON_REPOSITORY", &repository)
        .env_remove("SILICON_SOURCE_DIR")
        .env_remove("SILICON_GIT_REV")
        .env_remove("SILICON_DEPENDENCY_BIN_DIR")
        .env_remove("SILICON_RELEASE_BASE_URL");
    installer(
        command,
        &dir.join("updates.log"),
        include_str!("../install.sh"),
    )
    .with_context(|| format!("installing Silicon {tag} from {repository} failed"))?;
    Ok(Some(prefix.join("bin/silicon")))
}

/// GitHub's own words travel with any failure: rate limits and outages explain themselves.
fn release(url: &str, status: ureq::http::StatusCode, body: &str) -> Result<Value> {
    if !status.is_success() {
        bail!("GitHub answered {url} with HTTP {status}:\n{body}");
    }
    serde_json::from_str(body)
        .map_err(|error| anyhow!("GitHub answered {url} with invalid JSON ({error}):\n{body}"))
}

/// The tag of a stable release newer than this build, or None when there is no update.
fn newer<'a>(url: &str, release: &'a Value, body: &str) -> Result<Option<&'a str>> {
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
    let latest = version(tag).ok_or_else(|| {
        anyhow!("release tag {tag:?} from {url} is not a stable semantic version")
    })?;
    Ok((latest > version(env!("CARGO_PKG_VERSION")).unwrap()).then_some(tag))
}

/// Run install.sh with its output appended to `log`; a failure carries this run's output.
fn installer(mut command: Command, log: &Path, script: &str) -> Result<()> {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(log)
        .with_context(|| format!("cannot open update log {}", log.display()))?;
    let start = file
        .metadata()
        .with_context(|| format!("cannot inspect update log {}", log.display()))?
        .len();
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(
            file.try_clone()
                .with_context(|| format!("cannot share update log {}", log.display()))?,
        )
        .stderr(file)
        .spawn()
        .context("could not run `sh` for install.sh")?;
    // Dropping stdin after the write lets sh see the end of the script.
    let fed = child.stdin.take().unwrap().write_all(script.as_bytes());
    let status = child.wait().context("could not wait for install.sh")?;
    if status.success() && fed.is_ok() {
        return Ok(());
    }
    let mut problem = format!("`sh` running install.sh failed: {status}");
    if let Err(error) = fed {
        problem.push_str(&format!("\ncould not send install.sh to sh: {error}"));
    }
    bail!(
        "{problem}\noutput (also in {}):\n{}",
        log.display(),
        appended(log, start)
    )
}

/// Everything written to `log` after byte `start`, verbatim.
fn appended(log: &Path, start: u64) -> String {
    let read = || -> std::io::Result<Vec<u8>> {
        let mut file = File::open(log)?;
        file.seek(SeekFrom::Start(start))?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        Ok(bytes)
    };
    match read() {
        Ok(bytes) if bytes.trim_ascii().is_empty() => "(empty)".into(),
        Ok(bytes) => String::from_utf8_lossy(&bytes)
            .trim_end_matches(['\r', '\n'])
            .to_owned(),
        Err(error) => format!("(cannot read {}: {error})", log.display()),
    }
}

/// The updater serves every Silicon, so its failures go to daemon.log and to each connected
/// Silicon's log, where `silicon logs` and the dashboard show them.
fn report(runtime: &Runtime, message: &str) {
    eprintln!("{message}");
    let connected: Vec<_> = runtime.silicons.read().unwrap().values().cloned().collect();
    for silicon in connected {
        let home = &silicon.cfg.home;
        let masked = crate::failure::mask(home, message, &[]);
        if let Err(error) = crate::log_line_scoped(
            home,
            Some(silicon.cfg.generation),
            "error",
            "interpreter",
            &masked,
        ) {
            eprintln!("{error:#}; the updater error it was recording is above");
        }
    }
}

pub fn start(runtime: &Arc<Runtime>, ready: Arc<AtomicBool>) {
    if std::env::var("SILICON_AUTO_UPDATE").as_deref() == Ok("0") {
        return;
    }
    if let Err(error) = managed_prefix() {
        eprintln!("automatic updates require a managed bundle installation; source builds remain under your control: {error:#}");
        return;
    }
    let runtime = Arc::downgrade(runtime);
    thread::spawn(move || {
        let mut next = Instant::now() + Duration::from_secs(3600);
        loop {
            thread::sleep(Duration::from_secs(1));
            let Some(runtime) = runtime.upgrade() else {
                return;
            };
            if runtime.stopping.load(Ordering::SeqCst) {
                return;
            }
            if Instant::now() < next {
                continue;
            }
            next = Instant::now() + Duration::from_secs(3600);
            match crate::settings::load() {
                Ok(settings) if settings.auto_update => {}
                Ok(_) => continue,
                Err(error) => {
                    report(
                        &runtime,
                        &format!("automatic update skipped: cannot load settings: {error:#}"),
                    );
                    continue;
                }
            }
            match install(true) {
                Ok(Some(_)) => {
                    eprintln!("new release installed; waiting for active work before restarting");
                    ready.store(true, Ordering::SeqCst);
                    return;
                }
                Ok(None) => {}
                Err(error) => report(&runtime, &format!("automatic update failed: {error:#}")),
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
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
        let error = format!(
            "{:#}",
            installer(Command::new("sh"), &log, script).unwrap_err()
        );
        for expected in [
            "`sh` running install.sh failed: exit status: 3",
            "silicon install: downloading v9.9.9",
            "silicon install: checksum mismatch for silicon.tar.gz",
            &log.display().to_string(),
        ] {
            assert!(error.contains(expected), "missing {expected:?} in {error}");
        }
        assert!(!error.contains("earlier run"), "{error}");
        installer(Command::new("sh"), &log, "echo fine\n").unwrap();
        let spawn = format!(
            "{:#}",
            installer(Command::new(dir.path().join("absent")), &log, "").unwrap_err()
        );
        assert!(spawn.contains("No such file or directory"), "{spawn}");
    }
}
