//! Local connection progress uses the existing redacted, append-only Silicon log.
//! The display is a view: failures themselves are always returned whole, and main prints them.
use crate::failure::{also, panic_message};
use anyhow::{anyhow, Context, Result};
use serde_json::{json, Value};
use std::{
    fs,
    io::{IsTerminal, Write},
    path::Path,
    thread,
    time::Duration,
};

/// A recovered failure remains a complete diagnostic, visible as a warning and kept in the log.
pub(crate) fn warning(home: &Path, generation: uuid::Uuid, message: &str) {
    if let Err(error) = crate::log_line_scoped(
        home,
        Some(generation),
        "progress",
        "interpreter",
        &json!({"state": "warning", "message": message}).to_string(),
    ) {
        crate::stderr_line(&crate::failure::mask(
            home,
            &format!(
                "{}\nVia: recording connection warning: {message}",
                crate::failure::report(&error)
            ),
            &[],
        ));
    }
}

/// Run `action`, recording it as progress. Progress is a view of the work, never a gate on
/// it: the action runs even when its "running" record cannot be written, and a record that
/// fails after a successful action goes to the interpreter's stderr (daemon.log) instead of
/// turning that success into a failure.
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
    let started = record("running", working);
    let result = action();
    let reported = record(
        if result.is_ok() { "done" } else { "failed" },
        if result.is_ok() { complete } else { working },
    );
    let reported = match (started, reported) {
        (Ok(()), reported) => reported,
        (Err(started), Ok(())) => Err(started),
        (Err(started), Err(reported)) => Err(also(started, Err(reported))),
    };
    match result {
        Ok(value) => {
            if let Err(error) = reported {
                crate::stderr_line(&crate::failure::report(
                    &error.context(format!("recording progress for: {working}")),
                ));
            }
            Ok(value)
        }
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
        if state == "warning" {
            return self.raw(&format!("⚠ {message}"));
        }
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
        // A writer may still be appending a record (including a multibyte character);
        // only complete lines are read, across a rotation of the log.
        let bytes = crate::complete_lines_since(path, offset)
            .with_context(|| format!("read {}", path.display()))?;
        for line in String::from_utf8_lossy(&bytes).lines() {
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
    crate::cli::supervise(&cfg);
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
                json!({"yaml": cfg.path, "progress_streamed": true}),
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
                eprintln!(
                    "{}",
                    crate::failure::report(
                        &error.context(
                            "connection succeeded, but its progress could not be displayed"
                        )
                    )
                );
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
    fn recovered_install_is_displayed_as_a_warning() {
        let home = std::env::temp_dir().join(format!("silicon-progress-{}", uuid::Uuid::new_v4()));
        warning(
            &home,
            uuid::Uuid::new_v4(),
            "stderr:\npackage unavailable\n\nSource: `silicon-apps install ting`\nRecovery: using installed copy",
        );
        crate::log_line(&home, "warning", "ting", "full package diagnostic").unwrap();
        let mut display = Display {
            output: Vec::new(),
            terminal: false,
            pending: None,
        };
        display
            .lines(&home.join(".silicon/silicon.log"), &mut 0)
            .unwrap();
        assert_eq!(
            String::from_utf8(display.output).unwrap(),
            "⚠ stderr:\npackage unavailable\n\nSource: `silicon-apps install ting`\nRecovery: using installed copy\n"
        );
        fs::remove_dir_all(home).unwrap();
    }
}
