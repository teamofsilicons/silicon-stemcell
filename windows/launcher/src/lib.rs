//! Shared by the Windows launchers (`silicon.exe` and the other command names) and
//! `silicon-service.exe`, the logon task that keeps the interpreter running.
use std::io::{self, Read};
use std::path::Path;
use std::process::{Command, Output, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

pub const DISTRO: &str = "Silicon";
/// In `%LOCALAPPDATA%\Silicon`: the full path of the logon task install.ps1 registered for
/// this Windows user (`\Silicon Interpreter <SID>`). Task names are machine-wide and WSL
/// distributions per user, so each user has a task of their own; no file, no task.
pub const TASK_FILE: &str = "service-task";
/// In `%LOCALAPPDATA%\Silicon`: `silicon connect` touches it (through PowerShell from WSL)
/// to end the helper's wait before restarting a failed interpreter.
pub const WAKE_FILE: &str = "service-wake";

/// The logon task install.ps1 registered for this user, from `control`
/// (`%LOCALAPPDATA%\Silicon`).
pub fn registered_task(control: &Path) -> Option<String> {
    let text = std::fs::read_to_string(control.join(TASK_FILE)).ok()?;
    let task = text.trim();
    (!task.is_empty()).then(|| task.to_owned())
}

/// Variables Windows hands to Linux commands through WSLENV: application settings only.
pub fn forwarded(name: &str) -> bool {
    [
        "SILICON_",
        "SI_",
        "OMNI_",
        "APPS_",
        "BRIEFCASE_",
        "DM_",
        "WAVEFORM_",
        "COMMIT_",
        "REMIND_",
        "HOOK_",
        "TING_",
        "ACCOUNTS_",
        "ANTHROPIC_",
        "OPENAI_",
    ]
    .iter()
    .any(|prefix| name.starts_with(prefix))
        && name != "SILICON_WSL"
}

/// The command line for an error, quoted the way Windows parses it.
pub fn argv(command: &Command) -> String {
    std::iter::once(command.get_program())
        .chain(command.get_args())
        .map(|word| {
            let word = word.to_string_lossy();
            if word.is_empty() || word.contains([' ', '\t', '"']) {
                format!("\"{}\"", word.replace('"', "\\\""))
            } else {
                word.into_owned()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// wsl.exe writes its own messages as UTF-16LE; Linux programs write UTF-8.
pub fn text(bytes: &[u8]) -> String {
    if !bytes.contains(&0) {
        return String::from_utf8_lossy(bytes).into_owned();
    }
    let units: Vec<u16> = bytes
        .chunks(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair.get(1).copied().unwrap_or(0)]))
        .collect();
    String::from_utf16_lossy(&units)
        .trim_start_matches('\u{feff}')
        .to_owned()
}

/// Exit status plus both streams verbatim, the shape Silicon gives every failure.
pub fn describe(output: &Output) -> String {
    let mut description = output.status.to_string();
    for (name, bytes) in [("stderr", &output.stderr), ("stdout", &output.stdout)] {
        let stream = text(bytes);
        let stream = stream.trim_end_matches(['\r', '\n']);
        if stream.trim().is_empty() {
            description.push_str(&format!("\n{name}: (empty)"));
        } else {
            description.push_str(&format!("\n{name}:\n{stream}"));
        }
    }
    description
}

/// [`Command::output`] with a time limit: a tool still running at `limit` is killed, and
/// its output carries a closing line saying so. Output a leftover child keeps open is not
/// waited for.
pub fn output_within(command: &mut Command, limit: Duration) -> io::Result<Output> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout = drain(child.stdout.take());
    let stderr = drain(child.stderr.take());
    let deadline = Instant::now() + limit;
    let mut stopped = false;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            // Already gone is the outcome wanted.
            let _ = child.kill();
            stopped = true;
            break child.wait()?;
        }
        thread::sleep(Duration::from_millis(50));
    };
    let finish = |stream: mpsc::Receiver<Vec<u8>>| {
        stream
            .recv_timeout(Duration::from_secs(2))
            .unwrap_or_default()
    };
    let stdout = finish(stdout);
    let mut stderr = finish(stderr);
    if stopped {
        if !stderr.is_empty() && !stderr.ends_with(b"\n") {
            stderr.push(b'\n');
        }
        stderr.extend_from_slice(
            format!(
                "silicon: stopped after {}s without finishing (its time limit)\n",
                limit.as_secs()
            )
            .as_bytes(),
        );
    }
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

fn drain(pipe: Option<impl Read + Send + 'static>) -> mpsc::Receiver<Vec<u8>> {
    let (done, finished) = mpsc::channel();
    if let Some(mut pipe) = pipe {
        thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = pipe.read_to_end(&mut bytes);
            let _ = done.send(bytes);
        });
    }
    finished
}

/// `silicon-service.exe`: the logon task's supervisor for `silicon serve` in WSL.
pub mod service {
    use super::WAKE_FILE;
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, Write};
    use std::path::{Path, PathBuf};
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    /// service.log rotates here, keeping `.1` to `.3`.
    pub const LOG_CAP: u64 = 10 * 1024 * 1024;
    pub const LOG_KEEP: usize = 3;

    /// wsl.exe arguments running `silicon serve` as the silicon user in its home.
    pub fn serve_args() -> Vec<&'static str> {
        vec![
            "--distribution",
            super::DISTRO,
            "--user",
            "silicon",
            "--cd",
            "~",
            "--exec",
            "/opt/silicon/launch",
            "silicon",
            r"\\wsl.localhost\Silicon\home\silicon",
            "serve",
        ]
    }

    /// WSLENV for serve: SILICON_SERVICE plus every forwarded variable present, the same
    /// settings a terminal's `silicon connect` would hand over.
    pub fn wslenv(names: impl IntoIterator<Item = String>) -> String {
        let mut chosen = vec!["SILICON_SERVICE".to_owned()];
        for name in names {
            if super::forwarded(&name) && !chosen.contains(&name) {
                chosen.push(name);
            }
        }
        chosen.join(":")
    }

    /// How the helper paces restarts: 5 s doubling to 5 minutes, back to 5 s after 10
    /// healthy minutes. Uptime is wall-clock, so sleep does not look like a crash loop.
    pub struct Backoff {
        pub base: Duration,
        pub cap: Duration,
        pub healthy: Duration,
    }

    impl Backoff {
        pub fn standard() -> Self {
            Self {
                base: Duration::from_secs(5),
                cap: Duration::from_secs(5 * 60),
                healthy: Duration::from_secs(10 * 60),
            }
        }

        /// Failures in a row after one that ended a run of `uptime`; None (the clock went
        /// back) counts as long ago.
        pub fn failures_after(&self, previous: u32, uptime: Option<Duration>) -> u32 {
            match uptime {
                Some(uptime) if uptime < self.healthy => previous.saturating_add(1),
                _ => 1,
            }
        }

        pub fn delay(&self, failures: u32) -> Duration {
            let doublings = failures.saturating_sub(1).min(20);
            self.base.saturating_mul(1 << doublings).min(self.cap)
        }
    }

    /// Run `serve` until it stops cleanly: `Ok(true)` is exit 0 (`silicon stop`), after
    /// which the helper ends until the next logon or `silicon connect`. Anything else
    /// (a crash, `wsl --shutdown`, a WSL update) is restarted with backoff. `serve`
    /// answers whether the exit was clean and how it ended; `sleep` waits out a delay and
    /// answers true when `silicon connect` cut it short, which also resets the backoff.
    pub fn supervise(
        mut serve: impl FnMut() -> Result<(bool, String), String>,
        log: &Path,
        backoff: &Backoff,
        mut sleep: impl FnMut(Duration) -> bool,
        mut now: impl FnMut() -> SystemTime,
    ) -> i32 {
        let mut failures = 0;
        loop {
            let started = now();
            let outcome = serve();
            let uptime = now().duration_since(started).ok();
            match outcome {
                Ok((true, how)) => {
                    note(
                        log,
                        &format!("the interpreter stopped cleanly ({how}); the helper exits until the next logon or `silicon connect`"),
                    );
                    return 0;
                }
                Ok((false, how)) => note(
                    log,
                    &format!(
                        "the interpreter exited: {how} after {}",
                        uptime.map_or_else(|| "an unknown time".into(), shown)
                    ),
                ),
                Err(error) => note(log, &error),
            }
            failures = backoff.failures_after(failures, uptime);
            let delay = backoff.delay(failures);
            note(
                log,
                &format!(
                    "restarting in {} ({failures} failure{} in a row)",
                    shown(delay),
                    if failures == 1 { "" } else { "s" }
                ),
            );
            if sleep(delay) {
                note(
                    log,
                    &format!(
                        "woken by `silicon connect` ({WAKE_FILE}); restarting the interpreter now"
                    ),
                );
                failures = 0;
            }
        }
    }

    /// Sleep `delay`, looking for `wake` every `tick`. True (and `wake` removed) when it
    /// appeared: `silicon connect` wants the interpreter now, not in five minutes.
    pub fn sleep_unless_woken(delay: Duration, wake: &Path, tick: Duration) -> bool {
        let deadline = Instant::now() + delay;
        loop {
            if wake.exists() {
                let _ = fs::remove_file(wake);
                return true;
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            std::thread::sleep(left.min(tick));
        }
    }

    /// Exclusive for the life of the returned file; None when another helper holds it.
    pub fn single_instance(path: &Path) -> io::Result<Option<File>> {
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(path)?;
        match file.try_lock() {
            Ok(()) => Ok(Some(file)),
            Err(fs::TryLockError::WouldBlock) => Ok(None),
            Err(fs::TryLockError::Error(error)) => Err(error),
        }
    }

    /// One timestamped line in service.log. A log that cannot be written must not stop
    /// the supervision it describes.
    pub fn note(log: &Path, text: &str) {
        let _ = rotate(log, LOG_CAP, LOG_KEEP);
        let line = format!("{} silicon-service: {text}\n", stamp(SystemTime::now()));
        let _ = open_log(log).and_then(|mut file| file.write_all(line.as_bytes()));
    }

    pub fn open_log(log: &Path) -> io::Result<File> {
        OpenOptions::new().create(true).append(true).open(log)
    }

    /// Move `path` to `path.1` (older copies up to `path.KEEP`) once it reaches `cap`.
    pub fn rotate(path: &Path, cap: u64, keep: usize) -> io::Result<bool> {
        match fs::metadata(path) {
            Ok(metadata) if metadata.len() >= cap => {}
            Ok(_) => return Ok(false),
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error),
        }
        let numbered = |index: usize| {
            let mut name = path.as_os_str().to_owned();
            name.push(format!(".{index}"));
            PathBuf::from(name)
        };
        for index in (1..keep.max(1)).rev() {
            match fs::rename(numbered(index), numbered(index + 1)) {
                Err(error) if error.kind() != io::ErrorKind::NotFound => return Err(error),
                _ => {}
            }
        }
        fs::rename(path, numbered(1))?;
        Ok(true)
    }

    /// RFC 3339 UTC, without a date library.
    pub fn stamp(time: SystemTime) -> String {
        let seconds = time
            .duration_since(UNIX_EPOCH)
            .map(|since| since.as_secs())
            .unwrap_or(0);
        let (year, month, day) = civil(seconds.div_euclid(86_400) as i64);
        let rest = seconds % 86_400;
        format!(
            "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
            rest / 3600,
            rest / 60 % 60,
            rest % 60
        )
    }

    /// Days since 1970-01-01 to a proleptic Gregorian date (Howard Hinnant's algorithm).
    fn civil(days: i64) -> (i64, u32, u32) {
        let z = days + 719_468;
        let era = z.div_euclid(146_097);
        let doe = z.rem_euclid(146_097);
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
        let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
        let year = yoe + era * 400 + i64::from(month <= 2);
        (year, month, day)
    }

    pub fn shown(duration: Duration) -> String {
        let seconds = duration.as_secs();
        if seconds == 0 {
            return format!("{}ms", duration.as_millis());
        }
        match (seconds / 3600, seconds / 60 % 60) {
            (0, 0) => format!("{seconds}s"),
            (0, minutes) => format!("{minutes}m {}s", seconds % 60),
            (hours, minutes) => format!("{hours}h {minutes}m"),
        }
    }
}
