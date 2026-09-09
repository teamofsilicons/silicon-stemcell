pub mod auth;
pub mod cli;
pub mod config;
pub mod eval;
pub mod flow;
pub mod proxy;
pub mod runtime;
pub mod server;
pub mod state;
pub mod update;

use anyhow::Result;
use chrono::Utc;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::Mutex;

static LOG_LOCK: Mutex<()> = Mutex::new(());

/// Every tool process uses the Silicon's app state; the bundled interpreter owns updates.
pub(crate) fn command(program: impl AsRef<std::ffi::OsStr>, home: &Path) -> std::process::Command {
    let mut command = std::process::Command::new(program);
    command
        .current_dir(home)
        .env("SILICON_HOME", home)
        .env("SILICON_IAM_HOME", home.join(".silicon-iam"))
        .env("SILICON_IAM_AUTO_UPDATE", "false")
        .env("BRIEFCASE_AUTO_UPDATE", "0")
        .env("WAVEFORM_AUTO_UPDATE", "0")
        .env("SILICON_HOOK_AUTO_UPDATE", "0");
    command
}

pub fn log_line(home: &Path, kind: &str, origin: &str, message: &str) -> Result<()> {
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
    Ok(())
}
