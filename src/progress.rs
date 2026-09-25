//! Local connection progress uses the existing redacted, append-only Silicon log.
//! The display is a view: failures themselves are always returned whole, and main prints them.
use crate::failure::{also, panic_message};
use anyhow::{anyhow, Context, Result};
use serde_json::{json, Value};
use std::{
    fs::{self, File},
    io::{IsTerminal, Read, Seek, SeekFrom, Write},
    path::Path,
    thread,
    time::Duration,
};

pub(crate) fn step<T>(
    home: &Path,
    generation: Option<uuid::Uuid>,
    working: &str,
    complete: &str,
    action: impl FnOnce() -> Result<T>,
) -> Result<T> {
    let record = |state, message| {
        crate::log_line_scoped(
            home,
            generation,
            "progress",
            "interpreter",
            &json!({"state": state, "message": message}).to_string(),
        )
        .with_context(|| format!("record {state} progress"))
    };
    record("running", working)?;
    let result = action();
    let reported = record(
        if result.is_ok() { "done" } else { "failed" },
        if result.is_ok() { complete } else { working },
    );
    match result {
        Ok(value) => reported.map(|()| value),
        // Reporting must not replace the original operation's failure, nor vanish.
        Err(error) => Err(also(error, reported)),
    }
}

/// Strips `[kind] [origin]` and the timestamp's opening bracket, leaving
/// `TIMESTAMP] [MESSAGE]`. The origin may carry a `/cli` or `/daemon` process
/// role, so entries written before roles existed still match.
fn strip_origin<'a>(line: &'a str, kind: &str, origin: &str) -> Option<&'a str> {
    let rest = line
        .strip_prefix(kind)?
        .strip_prefix(" [")?
        .strip_prefix(origin)?;
    let rest = match rest.strip_prefix('/') {
        Some(role) => role.split_once(']')?.1,
        None => rest.strip_prefix(']')?,
    };
    rest.strip_prefix(" [")
}

struct Display<W> {
    output: W,
    terminal: bool,
    pending: Option<String>,
}

impl<W: Write> Display<W> {
    fn show(&mut self, state: &str, message: &str) -> Result<()> {
        let message: String = message.chars().filter(|c| !c.is_control()).collect();
        let mark = match state {
            "running" => "…",
            "done" => "✓",
            "failed" => "✗",
            _ => return Ok(()),
        };
        if self.terminal && self.pending.is_some() {
            // Restore the start even when a long app name wrapped onto another row.
            write!(self.output, "\x1b8\x1b[J")?;
        }
        if self.terminal && state == "running" {
            write!(self.output, "\x1b7{mark} {message}")?;
        } else {
            writeln!(self.output, "{mark} {message}")?;
        }
        self.pending = (state == "running").then_some(message);
        self.output.flush()?;
        Ok(())
    }

    fn lines(&mut self, path: &Path, offset: &mut u64) -> Result<()> {
        let mut file = match File::open(path) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e.into()),
        };
        file.seek(SeekFrom::Start(*offset))?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        // A writer may still be appending a record (including a multibyte character).
        let Some(end) = bytes.iter().rposition(|b| *b == b'\n') else {
            return Ok(());
        };
        *offset += end as u64 + 1;
        for line in String::from_utf8_lossy(&bytes[..=end]).lines() {
            if let Some(body) = strip_origin(line, "[progress]", "interpreter")
                .and_then(|line| line.split_once("] [").map(|(_, body)| body))
                .and_then(|body| body.strip_suffix(']'))
            {
                let event = serde_json::from_str::<Value>(body).unwrap_or_default();
                match (event["state"].as_str(), event["message"].as_str()) {
                    (Some(state), Some(message)) => self.show(state, message)?,
                    // A record this display cannot read is shown raw rather than dropped.
                    _ => self.raw(line)?,
                }
            } else if strip_origin(line, "[setup]", "stdout").is_some()
                || strip_origin(line, "[setup]", "stderr").is_some()
                || line.starts_with("[error] [")
            {
                // Errors logged mid-connect show live, even when the connect still succeeds.
                self.raw(line)?;
            }
        }
        Ok(())
    }

    fn raw(&mut self, line: &str) -> Result<()> {
        if self.terminal && self.pending.is_some() {
            write!(self.output, "\x1b8\x1b[J")?;
        }
        self.pending = None;
        // silicon.log escapes newlines; an error is unfolded to read as the tool wrote it.
        let unfolded = if line.starts_with("[error] [") {
            line.replace("\\n", "\n    ")
        } else {
            line.to_owned()
        };
        for part in unfolded.split('\n') {
            let part: String = part.chars().filter(|c| !c.is_control()).collect();
            writeln!(self.output, "{part}")?;
        }
        self.output.flush()?;
        Ok(())
    }
}

const PRINT: &str = "print connection progress to stdout";

pub(crate) fn connect(yaml: std::path::PathBuf) -> Result<Value> {
    let mut display = Display {
        terminal: std::io::stdout().is_terminal() && std::env::var("TERM").as_deref() != Ok("dumb"),
        output: std::io::stdout(),
        pending: None,
    };
    display
        .show("running", "Validating configuration")
        .context(PRINT)?;
    let cfg = match crate::server::compile(yaml) {
        Ok(cfg) => cfg,
        Err(error) => {
            return Err(also(
                error,
                display
                    .show("failed", "Configuration validation failed")
                    .context(PRINT),
            ))
        }
    };
    display
        .show("done", "Validated configuration")
        .context(PRINT)?;
    let path = cfg.home.join(".silicon/silicon.log");
    let mut offset = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    display
        .show("running", "Connecting Silicon")
        .context(PRINT)?;
    let (result, shown) = thread::scope(|scope| {
        let request = scope.spawn(|| {
            crate::server::call(
                &crate::server::daemon(true)?,
                "connect",
                json!({"yaml": cfg.path}),
            )
        });
        // Showing progress must never abandon the request or hide its answer.
        let mut shown = Ok(());
        while !request.is_finished() {
            if shown.is_ok() {
                shown = display.lines(&path, &mut offset);
            }
            thread::sleep(Duration::from_millis(100));
        }
        let result = request.join().unwrap_or_else(|panic| {
            Err(anyhow!(
                "connection request panicked: {}",
                panic_message(&*panic)
            ))
        });
        if shown.is_ok() {
            shown = display.lines(&path, &mut offset);
        }
        (result, shown)
    });
    let shown = shown.with_context(|| format!("show progress from {}", path.display()));
    let finished = if display.pending.is_some() || result.is_err() {
        display
            .show(
                if result.is_ok() { "done" } else { "failed" },
                if result.is_ok() {
                    "Connected Silicon"
                } else {
                    "Connection failed"
                },
            )
            .context(PRINT)
    } else {
        Ok(())
    };
    match result {
        Ok(value) => {
            // Connected: a display problem is worth a warning, not a failure.
            if let Err(error) = shown.and(finished) {
                eprintln!("warning: {error:#}");
            }
            Ok(value)
        }
        Err(error) => Err(also(also(error, shown), finished))
            .with_context(|| format!("connect {} failed", cfg.path.display())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn progress_is_live_handles_partial_records_and_never_marks_failure_done() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".silicon/silicon.log");
        let mut display = Display {
            output: Vec::new(),
            terminal: true,
            pending: None,
        };
        let mut offset = 0;
        step(
            dir.path(),
            None,
            "Authenticating demo",
            "Authenticated demo",
            || {
                display.lines(&path, &mut offset)?;
                assert_eq!(
                    String::from_utf8_lossy(&display.output),
                    "\x1b7… Authenticating demo"
                );
                Ok(())
            },
        )
        .unwrap();
        display.lines(&path, &mut offset).unwrap();
        assert!(
            String::from_utf8_lossy(&display.output).ends_with("\x1b8\x1b[J✓ Authenticated demo\n")
        );
        let error = step(
            dir.path(),
            None,
            "Authenticating broken",
            "Authenticated broken",
            || -> Result<()> { anyhow::bail!("private-token-output") },
        )
        .unwrap_err();
        assert_eq!(error.to_string(), "private-token-output");
        display.lines(&path, &mut offset).unwrap();
        let output = String::from_utf8_lossy(&display.output);
        assert!(output.contains("✗ Authenticating broken"));
        assert!(
            !output.contains("✓ Authenticated broken") && !output.contains("private-token-output")
        );

        display.terminal = false;
        let partial = "[progress] [interpreter] [time] [{\"state\":\"done\",\"message\":\"雪\"}]\n";
        let split = partial.find('雪').unwrap() + 1;
        let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
        file.write_all(&partial.as_bytes()[..split]).unwrap();
        let before = offset;
        display.lines(&path, &mut offset).unwrap();
        assert_eq!(offset, before);
        file.write_all(&partial.as_bytes()[split..]).unwrap();
        display.lines(&path, &mut offset).unwrap();
        assert!(String::from_utf8_lossy(&display.output).ends_with("✓ 雪\n"));
        let count = display.output.len();
        display.lines(&path, &mut offset).unwrap();
        assert_eq!(display.output.len(), count);
    }

    #[test]
    fn a_failing_tool_reaches_the_error_whole_and_logged_errors_show_live() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join(".silicon/bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(
            bin.join("demo"),
            "#!/bin/sh\necho '{\"ok\":false,\"reason\":\"quota\"}'\necho \"demo: login expired for $2\" >&2\nexit 7\n",
        )
        .unwrap();
        fs::set_permissions(bin.join("demo"), fs::Permissions::from_mode(0o700)).unwrap();
        let error = step(
            dir.path(),
            None,
            "Authenticating demo",
            "Authenticated demo",
            || -> Result<()> {
                let args = ["login", "private-login-secret"];
                let output = crate::command("demo", dir.path())
                    .args(args)
                    .output()
                    .unwrap();
                crate::log_line(
                    dir.path(),
                    "error",
                    "demo",
                    "login refused:\nstderr:\nquota",
                )
                .unwrap();
                assert!(!output.status.success());
                Err(crate::failure::command(
                    dir.path(),
                    &crate::failure::argv("demo", &args),
                    &output,
                    &["private-login-secret"],
                ))
            },
        )
        .unwrap_err();
        assert_eq!(
            format!("{error:#}"),
            "`demo login [redacted]` failed: exit status: 7\nstderr:\ndemo: login expired for [redacted]\nstdout:\n{\"ok\":false,\"reason\":\"quota\"}"
        );
        assert!(!format!("{error:?}").contains("private-login-secret"));
        let mut display = Display {
            output: Vec::new(),
            terminal: false,
            pending: None,
        };
        display
            .lines(&dir.path().join(".silicon/silicon.log"), &mut 0)
            .unwrap();
        let shown = String::from_utf8_lossy(&display.output);
        assert!(shown.contains("✗ Authenticating demo"), "{shown}");
        // A multi-line error is shown unfolded, as the tool wrote it.
        assert!(
            shown.contains("[error] [demo/cli] [")
                && shown.contains("] [login refused:\n    stderr:\n    quota]\n"),
            "{shown}"
        );
    }

    #[test]
    fn progress_reads_entries_with_and_without_a_process_role() {
        // Roles were added after some logs were written; both shapes must parse.
        for origin in ["interpreter", "interpreter/cli", "interpreter/daemon"] {
            let line = format!("[progress] [{origin}] [time] [{{\"state\":\"done\"}}]");
            assert_eq!(
                strip_origin(&line, "[progress]", "interpreter"),
                Some("time] [{\"state\":\"done\"}]"),
                "{origin}"
            );
        }
        for origin in ["stdout", "stdout/daemon"] {
            let line = format!("[setup] [{origin}] [time] [text]");
            assert!(
                strip_origin(&line, "[setup]", "stdout").is_some(),
                "{origin}"
            );
        }
        // A different origin that merely shares a prefix must not match.
        assert_eq!(
            strip_origin(
                "[progress] [interpreter-x] [time] [body]",
                "[progress]",
                "interpreter"
            ),
            None
        );
        assert_eq!(
            strip_origin(
                "[progress] [intuit/cli] [time] [body]",
                "[progress]",
                "interpreter"
            ),
            None
        );
    }
}
