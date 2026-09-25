pub mod apps;
pub mod auth;
pub mod cli;
pub mod config;
pub mod eval;
pub(crate) mod failure;
pub mod flow;
mod progress;
pub mod proxy;
pub mod runtime;
pub mod server;
pub mod settings;
pub mod state;
pub mod telemetry;
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
        .env("SILICON_HOME", home)
        .env("SILICON_IAM_HOME", home.join(".silicon-iam"));
    let org = home.join(".silicon/org.json");
    match std::fs::read(&org) {
        Ok(bytes) => match serde_json::from_slice::<String>(&bytes) {
            Ok(selected) => {
                command.env("SILICON_ORG", selected);
            }
            Err(error) => tool_setup_failed(
                home,
                &format!(
                    "{} must hold the selected organization as a JSON string, so tools run without SILICON_ORG: {error}; it holds: {}",
                    org.display(),
                    String::from_utf8_lossy(&bytes)
                ),
            ),
        },
        // No organization selected yet.
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => tool_setup_failed(
            home,
            &format!(
                "could not read {}, so tools run without SILICON_ORG: {error}",
                org.display()
            ),
        ),
    }
    let bin = home.join(".silicon/bin");
    match std::env::join_paths(std::iter::once(bin.clone()).chain(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ))) {
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
    let telemetry_off = match settings::load() {
        Ok(settings) => !settings.telemetry,
        Err(error) => {
            tool_setup_failed(
                home,
                &format!("tools keep their own telemetry defaults because the interpreter settings could not be read: {error:#}"),
            );
            false
        }
    };
    if telemetry_off || std::env::var("SILICON_TELEMETRY").as_deref() == Ok("0") {
        command
            .env("IAM_TELEMETRY", "off")
            .env("HONEYCOMB_TELEMETRY", "0")
            .env("DM_TELEMETRY_ENABLED", "false")
            .env("BRIEFCASE_TELEMETRY", "0")
            .env("SPACE_STATION_TELEMETRY", "0");
    }
    command
}

/// A tool will run, but not as configured. silicon.log says why, once per home and reason,
/// because every tool start would otherwise repeat it.
fn tool_setup_failed(home: &Path, message: &str) {
    static SAID: Mutex<Vec<(std::path::PathBuf, String)>> = Mutex::new(Vec::new());
    let key = (home.to_path_buf(), message.to_owned());
    {
        let mut said = SAID.lock().unwrap();
        if said.contains(&key) {
            return;
        }
        said.push(key);
    }
    if let Err(error) = log_line(home, "error", "interpreter", message) {
        eprintln!(
            "{error:#}; the error it was recording: {}",
            telemetry::redact(home, message)
        );
    }
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
    let path = home.join(".silicon/silicon.log");
    OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(&path)
        .and_then(|mut file| file.write_all(line.as_bytes()))
        .with_context(|| format!("cannot append to {}", path.display()))?;
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

    #[test]
    fn a_failed_silicon_log_write_names_the_file_and_the_os_error() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join(".silicon/silicon.log");
        std::fs::create_dir_all(&log).unwrap();
        let error = format!(
            "{:#}",
            log_line_scoped(dir.path(), None, "send", "a", "hello").unwrap_err()
        );
        assert!(
            error.starts_with(&format!("cannot append to {}: ", log.display()))
                && error.contains("os error"),
            "{error}"
        );
    }

    #[test]
    fn a_broken_selected_organization_is_logged_once_with_its_contents() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path();
        std::fs::create_dir_all(home.join(".silicon")).unwrap();
        std::fs::write(home.join(".silicon/org.json"), "tos\n").unwrap();
        for _ in 0..2 {
            let tool = command("true", home);
            assert!(!tool.get_envs().any(|(name, _)| name == "SILICON_ORG"));
        }
        let log = std::fs::read_to_string(home.join(".silicon/silicon.log")).unwrap();
        assert_eq!(log.matches("SILICON_ORG").count(), 1, "{log}");
        assert!(
            log.starts_with("[error] [interpreter/")
                && log.contains(&format!(
                    "{} must hold the selected organization as a JSON string, so tools run without SILICON_ORG: expected ident at line 1 column 2; it holds: tos\\n]",
                    home.join(".silicon/org.json").display()
                )),
            "{log}"
        );
        // A valid selection reaches the tool and adds nothing to the log.
        std::fs::write(home.join(".silicon/org.json"), "\"tos\"").unwrap();
        let tool = command("true", home);
        assert!(tool
            .get_envs()
            .any(|(name, value)| name == "SILICON_ORG" && value == Some("tos".as_ref())));
        let after = std::fs::read_to_string(home.join(".silicon/silicon.log")).unwrap();
        assert_eq!(after, log);
    }
}
