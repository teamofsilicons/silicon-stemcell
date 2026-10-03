//! Tools the interpreter runs get a time limit. A hung CLI, script or network call must not
//! hold a lock, a Ting queue or a shutdown for months. When a tool runs out of time its whole
//! process group is stopped, and the failure still carries everything it said.
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::Recover;

/// Starting a program that was written a moment ago (an app Honeycomb just installed, a script
/// a test just wrote) can fail on Linux with ETXTBSY while another thread's freshly forked child
/// still holds the file open for writing, until that child execs. That passes within moments.
pub(crate) trait Starting {
    fn spawn_retrying(&mut self) -> std::io::Result<std::process::Child>;
    fn output_retrying(&mut self) -> std::io::Result<Output>;
}

impl Starting for Command {
    fn spawn_retrying(&mut self) -> std::io::Result<std::process::Child> {
        retrying(|| self.spawn())
    }

    fn output_retrying(&mut self) -> std::io::Result<Output> {
        retrying(|| self.output())
    }
}

fn retrying<T>(mut start: impl FnMut() -> std::io::Result<T>) -> std::io::Result<T> {
    let mut attempt = 0;
    loop {
        match start() {
            Err(error) if error.raw_os_error() == Some(libc::ETXTBSY) && attempt < 20 => {
                attempt += 1;
                thread::sleep(Duration::from_millis(10 * attempt));
            }
            other => return other,
        }
    }
}

/// The process groups of tools running right now, so a stopping interpreter can end them:
/// their time limits die with the threads that enforce them.
static RUNNING: Mutex<Vec<libc::pid_t>> = Mutex::new(Vec::new());

/// Stop every tool still running: SIGTERM to each process group, and SIGKILL to those left
/// after the usual grace. Called when the interpreter stops.
pub(crate) fn terminate_all() {
    let groups = RUNNING.lock().recover().clone();
    if groups.is_empty() {
        return;
    }
    crate::stderr_line(&format!(
        "stopping {} tool process group(s) still running: {groups:?}",
        groups.len()
    ));
    for group in &groups {
        signal(*group, libc::SIGTERM);
    }
    let deadline = std::time::Instant::now() + GRACE;
    while std::time::Instant::now() < deadline {
        let left = RUNNING.lock().recover().clone();
        if left.is_empty() {
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }
    for group in RUNNING.lock().recover().iter() {
        signal(*group, libc::SIGKILL);
    }
}

/// What a tool is doing decides how long it may take. Each limit can be changed with
/// `SILICON_<KIND>_TIMEOUT_SECS`, for example `SILICON_EXPRESSION_TIMEOUT_SECS=600`.
#[derive(Clone, Copy, Debug)]
pub(crate) enum Limit {
    /// `!` Bash expressions: DNA, heartbeats, credentials, flow values.
    Expression,
    /// `silicon.setup` scripts.
    Setup,
    /// Honeycomb installs, updates and removals.
    Install,
    /// IAM and application CLIs: discovery, status, login, configuration.
    App,
    /// The Ting CLI: webhook registration, listing and removal.
    Ting,
}

impl Limit {
    fn name(self) -> &'static str {
        match self {
            Limit::Expression => "EXPRESSION",
            Limit::Setup => "SETUP",
            Limit::Install => "INSTALL",
            Limit::App => "APP",
            Limit::Ting => "TING",
        }
    }

    fn default(self) -> Duration {
        Duration::from_secs(match self {
            Limit::Expression => 5 * 60,
            Limit::Setup => 30 * 60,
            Limit::Install => 20 * 60,
            Limit::App => 2 * 60,
            Limit::Ting => 60,
        })
    }

    /// The configured limit. An unreadable override is reported once and the default kept.
    pub(crate) fn duration(self) -> Duration {
        let name = format!("SILICON_{}_TIMEOUT_SECS", self.name());
        match std::env::var(&name) {
            Err(_) => self.default(),
            Ok(value) => match value.trim().parse::<u64>() {
                Ok(seconds) if seconds > 0 => Duration::from_secs(seconds),
                _ => {
                    static SAID: Mutex<Vec<String>> = Mutex::new(Vec::new());
                    let mut said = SAID.lock().recover();
                    if !said.contains(&name) {
                        said.push(name.clone());
                        crate::stderr_line(&format!(
                            "{name}={value:?} is not a positive number of seconds; using {}s",
                            self.default().as_secs()
                        ));
                    }
                    self.default()
                }
            },
        }
    }
}

/// How long a tool gets to stop after SIGTERM before its process group is killed.
const GRACE: Duration = Duration::from_secs(5);
/// How long to keep reading after the tool itself exited, for output its children still
/// hold open. A daemon it started may keep the pipe forever; reading stops then.
const DRAIN: Duration = Duration::from_secs(2);

/// Run `command` to completion like [`Command::output`], within `limit`.
///
/// The tool runs in its own process group with stdin from /dev/null. Past `limit` the group
/// receives SIGTERM, then SIGKILL after five seconds, and the returned output carries the
/// signal status plus everything the tool printed. A closing `silicon:` line on stderr says
/// the interpreter stopped it, so an error built by [`crate::failure::command`] shows why.
pub(crate) fn output(command: &mut Command, limit: Limit) -> std::io::Result<Output> {
    output_within(command, limit.duration())
}

pub(crate) fn output_within(command: &mut Command, limit: Duration) -> std::io::Result<Output> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let mut child = command.spawn_retrying()?;
    let group = child.id() as libc::pid_t;
    RUNNING.lock().recover().push(group);
    // Removed however this call ends, once the child is reaped (its group id is then free).
    struct Running(libc::pid_t);
    impl Drop for Running {
        fn drop(&mut self) {
            RUNNING.lock().recover().retain(|group| *group != self.0);
        }
    }
    let _running = Running(group);
    let stdout = Stream::read(child.stdout.take());
    let stderr = Stream::read(child.stderr.take());
    let (done, exited) = mpsc::channel();
    let waiter = thread::Builder::new()
        .name("tool-wait".into())
        .spawn(move || {
            let _ = done.send(child.wait());
        })?;
    let mut note = None;
    let status = match exited.recv_timeout(limit) {
        Ok(status) => status?,
        Err(_) => {
            signal(group, libc::SIGTERM);
            let status = match exited.recv_timeout(GRACE) {
                Ok(status) => status?,
                Err(_) => {
                    signal(group, libc::SIGKILL);
                    exited
                        .recv()
                        .map_err(|_| std::io::Error::other("the tool's waiter thread ended"))??
                }
            };
            // Children the tool left behind in its group go too.
            signal(group, libc::SIGKILL);
            note = Some(format!(
                "silicon: stopped after {} without finishing (its time limit): SIGTERM to its process group, then SIGKILL {}s later",
                shown(limit),
                GRACE.as_secs()
            ));
            status
        }
    };
    let _ = waiter.join();
    let (stdout, stdout_open) = stdout.finish();
    let (mut stderr, stderr_open) = stderr.finish();
    if stdout_open || stderr_open {
        push_line(
            &mut stderr,
            "silicon: stopped reading output: the command exited, but a process it started still holds its output open",
        );
    }
    if let Some(note) = note {
        push_line(&mut stderr, &note);
    }
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

fn signal(group: libc::pid_t, signal: libc::c_int) {
    // The group may already be gone; that is the outcome wanted.
    unsafe {
        libc::killpg(group, signal);
    }
}

fn push_line(stream: &mut Vec<u8>, line: &str) {
    if !stream.is_empty() && !stream.ends_with(b"\n") {
        stream.push(b'\n');
    }
    stream.extend_from_slice(line.as_bytes());
    stream.push(b'\n');
}

fn shown(limit: Duration) -> String {
    let seconds = limit.as_secs();
    if seconds >= 60 && seconds.is_multiple_of(60) {
        format!("{}m", seconds / 60)
    } else {
        format!("{seconds}s")
    }
}

/// One output pipe, read on its own thread so a full pipe never blocks the tool.
struct Stream {
    bytes: Arc<Mutex<Vec<u8>>>,
    closed: mpsc::Receiver<()>,
    abandoned: Arc<AtomicBool>,
}

impl Stream {
    fn read(pipe: Option<impl Read + Send + 'static>) -> Self {
        let bytes = Arc::new(Mutex::new(Vec::new()));
        let abandoned = Arc::new(AtomicBool::new(false));
        let (close, closed) = mpsc::channel();
        if let Some(mut pipe) = pipe {
            let (bytes, abandoned) = (bytes.clone(), abandoned.clone());
            let reader = move || {
                let mut buffer = [0u8; 8192];
                loop {
                    match pipe.read(&mut buffer) {
                        Ok(0) => break,
                        Ok(read) => {
                            // Once abandoned, keep draining so the writer never blocks,
                            // but hold nothing more.
                            if !abandoned.load(Ordering::SeqCst) {
                                bytes.lock().recover().extend_from_slice(&buffer[..read]);
                            }
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                        Err(_) => break,
                    }
                }
                let _ = close.send(());
            };
            if thread::Builder::new()
                .name("tool-output".into())
                .spawn(reader)
                .is_err()
            {
                // Without a reader the pipe is dropped; the tool sees EPIPE, not a hang.
            }
        } else {
            drop(close);
        }
        Self {
            bytes,
            closed,
            abandoned,
        }
    }

    /// Everything read, and whether the pipe was still open when reading stopped.
    fn finish(self) -> (Vec<u8>, bool) {
        let open = matches!(
            self.closed.recv_timeout(DRAIN),
            Err(mpsc::RecvTimeoutError::Timeout)
        );
        self.abandoned.store(true, Ordering::SeqCst);
        let bytes = std::mem::take(&mut *self.bytes.lock().recover());
        (bytes, open)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    fn sh(script: &str) -> Command {
        let mut command = Command::new("sh");
        command.args(["-c", script]);
        command
    }

    #[test]
    fn finished_tools_answer_like_output() {
        let output = output_within(
            &mut sh("echo out; echo err >&2; exit 3"),
            Duration::from_secs(10),
        )
        .unwrap();
        assert_eq!(output.status.code(), Some(3));
        assert_eq!(output.stdout, b"out\n");
        assert_eq!(output.stderr, b"err\n");
    }

    #[test]
    fn a_tool_past_its_limit_is_stopped_with_everything_it_said() {
        let started = Instant::now();
        let output = output_within(
            &mut sh("echo started; echo working >&2; sleep 30 & sleep 30; echo never"),
            Duration::from_millis(300),
        )
        .unwrap();
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(!output.status.success());
        assert_eq!(output.stdout, b"started\n");
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(stderr.starts_with("working\n"), "{stderr}");
        assert!(
            stderr.contains("silicon: stopped after 0s without finishing (its time limit): SIGTERM to its process group, then SIGKILL 5s later"),
            "{stderr}"
        );
        let described = crate::failure::describe(&Output {
            status: output.status,
            stdout: output.stdout,
            stderr: stderr.clone().into_bytes(),
        });
        assert!(described.starts_with("signal: 15"), "{described}");
    }

    #[test]
    fn a_tool_ignoring_sigterm_is_killed() {
        let started = Instant::now();
        let output = output_within(
            &mut sh("trap '' TERM; echo ready; sleep 60"),
            Duration::from_millis(300),
        )
        .unwrap();
        let elapsed = started.elapsed();
        assert!(elapsed >= GRACE && elapsed < GRACE + Duration::from_secs(10));
        assert_eq!(output.stdout, b"ready\n");
        assert!(String::from_utf8_lossy(&output.stderr).contains("SIGKILL 5s later"));
    }

    #[test]
    fn a_background_child_holding_the_pipe_does_not_hold_the_answer() {
        let started = Instant::now();
        // The tool exits at once; the child it left keeps stdout open for a minute.
        let output = output_within(
            &mut sh("echo done; (sleep 60; echo late) &"),
            Duration::from_secs(30),
        )
        .unwrap();
        assert!(started.elapsed() < DRAIN + Duration::from_secs(5));
        assert!(output.status.success());
        assert_eq!(output.stdout, b"done\n");
        assert!(String::from_utf8_lossy(&output.stderr)
            .contains("a process it started still holds its output open"));
    }

    #[test]
    fn a_stopping_interpreter_ends_the_tools_still_running() {
        // In its own process: terminate_all would also end other tests' tools.
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "process::tests::terminate_all_child",
                "--exact",
                "--ignored",
                "--nocapture",
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[test]
    #[ignore = "run by a_stopping_interpreter_ends_the_tools_still_running in its own process"]
    fn terminate_all_child() {
        let started = Instant::now();
        let running = thread::spawn(|| {
            output_within(&mut sh("echo up; sleep 60"), Duration::from_secs(120)).unwrap()
        });
        while RUNNING.lock().recover().is_empty() {
            assert!(started.elapsed() < Duration::from_secs(10));
            thread::sleep(Duration::from_millis(20));
        }
        terminate_all();
        let output = running.join().unwrap();
        assert!(started.elapsed() < Duration::from_secs(30));
        assert!(!output.status.success());
        assert!(RUNNING.lock().recover().is_empty());
    }

    #[test]
    fn a_program_briefly_busy_being_written_is_started_after_a_retry() {
        let mut tries = 0;
        let started = retrying(|| {
            tries += 1;
            if tries < 3 {
                Err(std::io::Error::from_raw_os_error(libc::ETXTBSY))
            } else {
                Ok(tries)
            }
        })
        .unwrap();
        assert_eq!(started, 3);
        // Any other failure is the answer at once.
        let mut tries = 0;
        let error = retrying(|| -> std::io::Result<()> {
            tries += 1;
            Err(std::io::Error::from(std::io::ErrorKind::NotFound))
        })
        .unwrap_err();
        assert_eq!((tries, error.kind()), (1, std::io::ErrorKind::NotFound));
    }

    #[test]
    fn unstartable_tools_keep_the_operating_system_reason() {
        let error = output_within(
            &mut Command::new("/nonexistent/silicon-tool"),
            Duration::from_secs(1),
        )
        .unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
    }

    #[test]
    fn limits_have_defaults_and_overrides() {
        assert_eq!(Limit::Ting.default(), Duration::from_secs(60));
        assert_eq!(shown(Duration::from_secs(300)), "5m");
        assert_eq!(shown(Duration::from_secs(90)), "90s");
    }
}
