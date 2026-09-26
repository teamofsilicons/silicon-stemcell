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
        "HONEYCOMB_",
        "SPACE_STATION_",
        "BRIEFCASE_",
        "DM_",
        "WAVEFORM_",
        "COMMIT_",
        "REMIND_",
        "HOOK_",
        "TING_",
        "IAM_",
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

#[cfg(test)]
mod tests {
    use super::service::*;
    use super::*;
    use std::cell::RefCell;
    use std::time::SystemTime;

    #[test]
    fn linux_home_and_path_cannot_be_overridden_through_windows() {
        for name in ["HOME", "USERPROFILE", "PATH", "WSLENV", "SILICON_WSL"] {
            assert!(!forwarded(name));
        }
        assert!(forwarded("SILICON_TELEMETRY"));
        assert!(forwarded("SILICON_HOME"));
        assert!(forwarded("SPACE_STATION_TABLE_KEY"));
        assert!(forwarded("TING_HOME"));
    }

    #[test]
    fn wsl_utf16_messages_read_as_text() {
        let wide: Vec<u8> = "\u{feff}Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        assert_eq!(
            text(&wide),
            "Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"
        );
        assert_eq!(text("v5.0.1 Ω\n".as_bytes()), "v5.0.1 Ω\n");
    }

    #[test]
    fn the_helper_runs_serve_in_the_distribution_home_with_the_launchers_settings() {
        assert_eq!(
            serve_args().join(" "),
            r"--distribution Silicon --user silicon --cd ~ --exec /opt/silicon/launch silicon \\wsl.localhost\Silicon\home\silicon serve"
        );
        let names = [
            "PATH",
            "ANTHROPIC_API_KEY",
            "SILICON_SERVICE",
            "SILICON_WSL",
            "USERPROFILE",
            "OPENAI_BASE_URL",
        ]
        .map(str::to_owned);
        assert_eq!(
            wslenv(names),
            "SILICON_SERVICE:ANTHROPIC_API_KEY:OPENAI_BASE_URL"
        );
    }

    #[test]
    fn helper_backoff_doubles_to_five_minutes_and_resets_after_ten_healthy_minutes() {
        let backoff = Backoff::standard();
        let delays: Vec<u64> = (1..=8).map(|n| backoff.delay(n).as_secs()).collect();
        assert_eq!(delays, [5, 10, 20, 40, 80, 160, 300, 300]);
        assert_eq!(backoff.delay(u32::MAX), Duration::from_secs(300));
        assert_eq!(backoff.failures_after(2, Some(Duration::from_secs(59))), 3);
        assert_eq!(backoff.failures_after(7, Some(Duration::from_secs(600))), 1);
        assert_eq!(backoff.failures_after(7, None), 1);
    }

    #[test]
    fn the_helper_restarts_until_a_clean_stop() {
        let dir = std::env::temp_dir().join(format!(
            "silicon-service-test-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("service.log");
        let _ = std::fs::remove_file(&log);
        let answers = RefCell::new(vec![
            Ok((true, "exit code: 0".to_owned())),
            Ok((false, "exit code: 1".to_owned())),
            Err("could not run wsl.exe: not found".to_owned()),
        ]);
        let slept = RefCell::new(Vec::new());
        let clock = RefCell::new(SystemTime::UNIX_EPOCH);
        let code = supervise(
            || answers.borrow_mut().pop().unwrap(),
            &log,
            &Backoff::standard(),
            |delay| {
                slept.borrow_mut().push(delay);
                false
            },
            || {
                let mut now = clock.borrow_mut();
                *now += Duration::from_secs(1);
                *now
            },
        );
        assert_eq!(code, 0);
        assert_eq!(
            *slept.borrow(),
            [Duration::from_secs(5), Duration::from_secs(10)]
        );
        let written = std::fs::read_to_string(&log).unwrap();
        assert!(
            written.contains("silicon-service: could not run wsl.exe: not found\n"),
            "{written}"
        );
        assert!(
            written.contains("the interpreter exited: exit code: 1 after 1s"),
            "{written}"
        );
        assert!(
            written.contains("restarting in 10s (2 failures in a row)"),
            "{written}"
        );
        assert!(
            written.contains("stopped cleanly (exit code: 0); the helper exits"),
            "{written}"
        );
        // Only one helper runs at a time.
        let lock = dir.join("service.lock");
        let held = single_instance(&lock).unwrap().unwrap();
        assert!(single_instance(&lock).unwrap().is_none());
        drop(held);
        // A child another test forked before its exec shares the lock for a moment.
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while single_instance(&lock).unwrap().is_none() {
            assert!(
                std::time::Instant::now() < deadline,
                "the lock was never released"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        // The log rotates at its cap.
        std::fs::write(&log, "0123456789").unwrap();
        assert!(!rotate(&log, 11, 3).unwrap());
        assert!(rotate(&log, 10, 3).unwrap());
        assert!(!log.exists());
        let mut first = log.clone().into_os_string();
        first.push(".1");
        assert_eq!(std::fs::read_to_string(first).unwrap(), "0123456789");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn connect_cuts_the_helpers_restart_delay_short() {
        let dir = std::env::temp_dir().join(format!(
            "silicon-service-wake-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let wake = dir.join(WAKE_FILE);
        let _ = std::fs::remove_file(&wake);
        let started = Instant::now();
        assert!(!sleep_unless_woken(
            Duration::from_millis(60),
            &wake,
            Duration::from_millis(10)
        ));
        assert!(started.elapsed() >= Duration::from_millis(60));
        std::fs::write(&wake, "").unwrap();
        let started = Instant::now();
        assert!(sleep_unless_woken(
            Duration::from_secs(300),
            &wake,
            Duration::from_millis(10)
        ));
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(!wake.exists(), "a wake is used once");
        // Woken, the helper restarts at once and starts its backoff over.
        let log = dir.join("service.log");
        let _ = std::fs::remove_file(&log);
        let answers = RefCell::new(vec![
            Ok((true, "exit code: 0".to_owned())),
            Ok((false, "exit code: 1".to_owned())),
            Ok((false, "exit code: 1".to_owned())),
            Ok((false, "exit code: 1".to_owned())),
        ]);
        let slept = RefCell::new(Vec::new());
        supervise(
            || answers.borrow_mut().pop().unwrap(),
            &log,
            &Backoff::standard(),
            |delay| {
                slept.borrow_mut().push(delay);
                slept.borrow().len() == 2
            },
            SystemTime::now,
        );
        assert_eq!(
            *slept.borrow(),
            [
                Duration::from_secs(5),
                Duration::from_secs(10),
                Duration::from_secs(5)
            ]
        );
        let written = std::fs::read_to_string(&log).unwrap();
        assert!(
            written.contains(
                "woken by `silicon connect` (service-wake); restarting the interpreter now"
            ),
            "{written}"
        );
        // install.ps1 names this user's task; without its file there is none.
        assert_eq!(registered_task(&dir), None);
        std::fs::write(
            dir.join(TASK_FILE),
            "\\Silicon Interpreter S-1-5-21-7-8-9-1001\r\n",
        )
        .unwrap();
        assert_eq!(
            registered_task(&dir).as_deref(),
            Some(r"\Silicon Interpreter S-1-5-21-7-8-9-1001")
        );
        std::fs::write(dir.join(TASK_FILE), " \n").unwrap();
        assert_eq!(registered_task(&dir), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn stamps_are_utc_rfc3339() {
        assert_eq!(stamp(SystemTime::UNIX_EPOCH), "1970-01-01T00:00:00Z");
        let leap = SystemTime::UNIX_EPOCH + Duration::from_secs(951_782_400 + 3_723);
        assert_eq!(stamp(leap), "2000-02-29T01:02:03Z");
        let later = SystemTime::UNIX_EPOCH + Duration::from_secs(1_790_380_800);
        assert_eq!(stamp(later), "2026-09-26T00:00:00Z");
        assert_eq!(shown(Duration::from_secs(125)), "2m 5s");
    }

    #[cfg(unix)]
    #[test]
    fn bounded_commands_are_stopped_at_their_limit() {
        let mut quick = Command::new("sh");
        quick.args(["-c", "echo out; echo err >&2; exit 3"]);
        let output = output_within(&mut quick, Duration::from_secs(10)).unwrap();
        assert_eq!(output.status.code(), Some(3));
        assert_eq!(output.stdout, b"out\n");
        assert_eq!(output.stderr, b"err\n");
        let mut slow = Command::new("sh");
        slow.args(["-c", "echo started; exec sleep 30"]);
        let started = Instant::now();
        let output = output_within(&mut slow, Duration::from_millis(300)).unwrap();
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(!output.status.success());
        assert_eq!(output.stdout, b"started\n");
        assert!(String::from_utf8_lossy(&output.stderr)
            .contains("silicon: stopped after 0s without finishing"));
    }
}
