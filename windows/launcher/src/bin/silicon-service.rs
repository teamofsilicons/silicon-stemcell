//! `silicon-service.exe`, run by the logon task `\Silicon Interpreter <user SID>` that
//! install.ps1 registers. It keeps `silicon serve` running inside the Silicon WSL distribution: its
//! wsl.exe session stays attached for as long as the interpreter runs, so WSL never idles
//! the distribution out, and a crash, `wsl --shutdown` or WSL update is restarted with
//! backoff. A clean stop (`silicon stop`, exit 0) ends the helper until the next logon or
//! `silicon connect`. It never provisions WSL; the installer and the launcher do that.
#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(windows)]
fn main() {
    std::process::exit(helper::run());
}

#[cfg(not(windows))]
fn main() {
    eprintln!("silicon-service runs only on Windows, as the logon task install.ps1 registers");
    std::process::exit(2);
}

#[cfg(windows)]
mod helper {
    use silicon_windows_launcher::service::{
        note, open_log, rotate, serve_args, single_instance, sleep_unless_woken, supervise, wslenv,
        Backoff, LOG_CAP, LOG_KEEP,
    };
    use silicon_windows_launcher::{argv, forwarded, WAKE_FILE};
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::{Command, Stdio};
    use std::time::{Duration, SystemTime};

    /// No console window for wsl.exe: the task runs hidden.
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    pub fn run() -> i32 {
        // Without LOCALAPPDATA there is nowhere to log or lock; the task shows the exit code.
        let Some(local) = std::env::var_os("LOCALAPPDATA") else {
            return 3;
        };
        let directory = PathBuf::from(local).join("Silicon");
        let log = directory.join("service.log");
        if let Err(error) = std::fs::create_dir_all(&directory) {
            note(
                &log,
                &format!("could not create {}: {error}", directory.display()),
            );
            return 1;
        }
        let lock = directory.join("service.lock");
        let _instance = match single_instance(&lock) {
            Ok(Some(file)) => file,
            // Another helper already supervises the interpreter.
            Ok(None) => return 0,
            Err(error) => {
                note(&log, &format!("could not lock {}: {error}", lock.display()));
                return 1;
            }
        };
        let system_root = match std::env::var("SystemRoot") {
            Ok(root) => root,
            Err(error) => {
                note(
                    &log,
                    &format!("could not read the SystemRoot environment variable: {error}"),
                );
                return 1;
            }
        };
        let wsl = Path::new(&system_root).join("System32/wsl.exe");
        let variables = wslenv(
            std::env::vars_os()
                .filter_map(|(name, _)| name.into_string().ok())
                .filter(|name| forwarded(name)),
        );
        // `silicon connect` touches it when it finds no interpreter while this helper waits
        // (up to 5 minutes) to restart one.
        let wake = directory.join(WAKE_FILE);
        supervise(
            || serve(&wsl, &variables, &log, &wake),
            &log,
            &Backoff::standard(),
            |delay| sleep_unless_woken(delay, &wake, Duration::from_secs(1)),
            SystemTime::now,
        )
    }

    /// One run of `silicon serve`, its output appended to service.log.
    fn serve(
        wsl: &Path,
        variables: &str,
        log: &Path,
        wake: &Path,
    ) -> Result<(bool, String), String> {
        // A wake left from before this run must not cut the next delay short.
        let _ = std::fs::remove_file(wake);
        let _ = rotate(log, LOG_CAP, LOG_KEEP);
        let mut command = Command::new(wsl);
        command
            .args(serve_args())
            .env("SILICON_SERVICE", "windows-task")
            .env("WSLENV", variables)
            .stdin(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW);
        let shown = argv(&command);
        let output = open_log(log)
            .and_then(|file| Ok((file.try_clone()?, file)))
            .map_err(|error| format!("could not open {}: {error}", log.display()))?;
        command.stdout(output.0).stderr(output.1);
        note(log, &format!("starting the interpreter: {shown}"));
        let status = command
            .status()
            .map_err(|error| format!("could not run `{shown}`: {error}"))?;
        Ok((status.success(), status.to_string()))
    }
}
