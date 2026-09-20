pub mod apps;
pub mod auth;
pub mod cli;
pub mod config;
pub mod eval;
pub mod flow;
mod progress;
pub mod proxy;
pub mod runtime;
pub mod server;
pub mod settings;
pub mod state;
pub mod telemetry;
pub mod update;

use anyhow::Result;
use chrono::Utc;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

static LOG_LOCK: Mutex<()> = Mutex::new(());
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
    let executable = std::env::current_exe()?.canonicalize()?;
    if let Some(bin) = executable.parent() {
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
            ))?;
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
        .env("SILICON_HOME", home)
        .env("SILICON_IAM_HOME", home.join(".silicon-iam"));
    if let Ok(path) = std::env::join_paths(std::iter::once(home.join(".silicon/bin")).chain(
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()),
    )) {
        command.env("PATH", path);
    }
    if settings::load().is_ok_and(|s| !s.telemetry)
        || std::env::var("SILICON_TELEMETRY").as_deref() == Ok("0")
    {
        command
            .env("IAM_TELEMETRY", "off")
            .env("HONEYCOMB_TELEMETRY", "0")
            .env("DM_TELEMETRY_ENABLED", "false")
            .env("BRIEFCASE_TELEMETRY", "0")
            .env("SPACE_STATION_TELEMETRY", "0");
    }
    command
}

pub fn log_line(home: &Path, kind: &str, origin: &str, message: &str) -> Result<()> {
    log_line_scoped(home, None, kind, origin, message)
}

pub fn log_line_scoped(
    home: &Path,
    generation: Option<uuid::Uuid>,
    kind: &str,
    origin: &str,
    message: &str,
) -> Result<()> {
    let message = telemetry::redact(home, message);
    let origin = telemetry::redact(home, origin);
    let _guard = LOG_LOCK.lock().unwrap();
    state::private_dir(&home.join(".silicon"))?;
    // The role rides in the origin field so the entry keeps its four-part shape
    // and existing log readers keep parsing. Telemetry below still gets the bare
    // origin, whose exact value routes events.
    let line = format!(
        "[{kind}] [{origin}/{}] [{}] [{}]\n",
        process_role(),
        Utc::now().to_rfc3339(),
        message.replace('\n', "\\n")
    );
    OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(home.join(".silicon/silicon.log"))?
        .write_all(line.as_bytes())?;
    drop(_guard);
    telemetry::record_scoped(home, generation, kind, &origin, &message);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_lines_name_the_writing_process_and_keep_four_fields() {
        let dir = tempfile::tempdir().unwrap();
        // A test binary is not `silicon serve`, so it logs as a CLI process.
        assert_eq!(process_role(), "cli");
        log_line(dir.path(), "command", "interpreter", "running: pwd").unwrap();
        let line = std::fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        let line = line.trim_end();
        assert!(
            line.starts_with("[command] [interpreter/cli] ["),
            "unexpected line: {line}"
        );
        assert_eq!(
            line.matches("] [").count(),
            3,
            "field count changed: {line}"
        );
        assert!(line.ends_with("[running: pwd]"), "unexpected line: {line}");

        mark_daemon();
        assert_eq!(process_role(), "daemon");
        log_line(dir.path(), "command", "interpreter", "running: pwd").unwrap();
        let text = std::fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        let second = text.lines().nth(1).unwrap();
        assert!(
            second.starts_with("[command] [interpreter/daemon] ["),
            "unexpected line: {second}"
        );
    }
}
