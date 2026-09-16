pub mod apps;
pub mod auth;
pub mod cli;
pub mod config;
pub mod eval;
pub mod flow;
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
use std::sync::Mutex;

static LOG_LOCK: Mutex<()> = Mutex::new(());

/// Call at process startup, before starting threads, so private bundle tools are discoverable.
pub fn init_bundle_path() -> Result<()> {
    let executable = std::env::current_exe()?.canonicalize()?;
    if let Some(bin) = executable.parent() {
        if bin.parent().is_some_and(|root| {
            std::fs::read_to_string(root.join("VERSION")).is_ok_and(|version| {
                version.trim().trim_start_matches('v') == env!("CARGO_PKG_VERSION")
            })
        }) {
            let path = std::env::join_paths(
                std::iter::once(bin.to_path_buf()).chain(
                    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
                        .filter(|path| path != bin),
                ),
            )?;
            std::env::set_var("PATH", path);
        }
    }
    Ok(())
}

/// Every tool process uses the Silicon's app state; the bundled interpreter owns updates.
pub(crate) fn command(program: impl AsRef<std::ffi::OsStr>, home: &Path) -> std::process::Command {
    let mut command = std::process::Command::new(program);
    command
        .current_dir(home)
        .env("SILICON_HOME", home)
        .env("SILICON_IAM_HOME", home.join(".silicon-iam"))
        .env("SILICON_IAM_AUTO_UPDATE", "false")
        .env("HONEYCOMB_AUTO_UPDATE", "0")
        .env("BRIEFCASE_AUTO_UPDATE", "0")
        .env("WAVEFORM_AUTO_UPDATE", "0")
        .env("SPACE_STATION_UPDATE", "0")
        .env("SILICON_HOOK_AUTO_UPDATE", "0");
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
    let line = format!(
        "[{kind}] [{origin}] [{}] [{}]\n",
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
