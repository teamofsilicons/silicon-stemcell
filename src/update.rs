//! Hourly GitHub releases, installed through the same verified bundle path as curl + sh.
use crate::{runtime::Runtime, state};
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
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
    let executable = std::env::current_exe()?.canonicalize()?;
    let release = executable
        .parent()
        .and_then(|p| p.parent())
        .ok_or_else(|| anyhow!("invalid executable path"))?;
    let prefix = PathBuf::from(
        fs::read_to_string(release.join("PREFIX"))
            .context("automatic updates require a managed bundle installation")?
            .trim(),
    )
    .canonicalize()?;
    if !executable.starts_with(prefix.join("lib/silicon/releases")) {
        bail!("PREFIX does not own this executable");
    }
    Ok(prefix)
}

/// Returns the new executable after the complete bundle has been verified and activated.
pub fn install_latest() -> Result<Option<PathBuf>> {
    install(false)
}

fn install(noninteractive: bool) -> Result<Option<PathBuf>> {
    let prefix = managed_prefix()?;
    let repository = std::env::var("SILICON_REPOSITORY")
        .unwrap_or_else(|_| "teamofsilicons/silicon-stemcell".into());
    if repository.split('/').count() != 2
        || !repository
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-._/".contains(&c))
    {
        bail!("invalid SILICON_REPOSITORY");
    }
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(30)))
        .build()
        .new_agent();
    let release: Value = agent
        .get(format!(
            "https://api.github.com/repos/{repository}/releases/latest"
        ))
        .header("User-Agent", concat!("silicon/", env!("CARGO_PKG_VERSION")))
        .header("Accept", "application/vnd.github+json")
        .call()?
        .body_mut()
        .read_json()?;
    let tag = release["tag_name"]
        .as_str()
        .ok_or_else(|| anyhow!("GitHub release has no tag_name"))?;
    if release["draft"].as_bool() != Some(false) || release["prerelease"].as_bool() != Some(false) {
        return Ok(None);
    }
    let latest =
        version(tag).ok_or_else(|| anyhow!("release tag is not a stable semantic version"))?;
    if latest <= version(env!("CARGO_PKG_VERSION")).unwrap() {
        return Ok(None);
    }
    let dir = crate::server::directory();
    state::private_dir(&dir)?;
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(dir.join("updates.log"))?;
    let mut child = Command::new("sh")
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
        .env_remove("SILICON_RELEASE_BASE_URL")
        .stdin(Stdio::piped())
        .stdout(log.try_clone()?)
        .stderr(log)
        .spawn()?;
    child
        .stdin
        .take()
        .unwrap()
        .write_all(include_str!("../install.sh").as_bytes())?;
    if !child.wait()?.success() {
        bail!(
            "update installation failed; see {}",
            dir.join("updates.log").display()
        );
    }
    Ok(Some(prefix.join("bin/silicon")))
}

pub fn start(runtime: &Arc<Runtime>, ready: Arc<AtomicBool>) {
    if std::env::var("SILICON_AUTO_UPDATE").as_deref() == Ok("0") {
        return;
    }
    if managed_prefix().is_err() {
        eprintln!("automatic updates require a managed bundle installation; source builds remain under your control");
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
            match install(true) {
                Ok(Some(_)) => {
                    eprintln!("new release installed; waiting for active work before restarting");
                    ready.store(true, Ordering::SeqCst);
                    return;
                }
                Ok(None) => {}
                Err(error) => eprintln!("automatic update failed: {error:#}"),
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
}
