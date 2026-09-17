//! Local connection progress uses the existing redacted, append-only Silicon log.
use anyhow::{Context, Result};
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
    };
    record("running", working)?;
    let result = action();
    // Reporting must not replace the original operation's failure.
    let reported = record(
        if result.is_ok() { "done" } else { "failed" },
        if result.is_ok() { complete } else { working },
    );
    let value = result?;
    reported?;
    Ok(value)
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
            if let Some(body) = line
                .strip_prefix("[progress] [interpreter] [")
                .and_then(|line| line.split_once("] [").map(|(_, body)| body))
                .and_then(|body| body.strip_suffix(']'))
            {
                if let Ok(event) = serde_json::from_str::<Value>(body) {
                    if let (Some(state), Some(message)) =
                        (event["state"].as_str(), event["message"].as_str())
                    {
                        self.show(state, message)?;
                    }
                }
            } else if line.starts_with("[setup] [stdout] ") || line.starts_with("[setup] [stderr] ")
            {
                if self.terminal && self.pending.is_some() {
                    write!(self.output, "\x1b8\x1b[J")?;
                }
                self.pending = None;
                writeln!(
                    self.output,
                    "{}",
                    line.chars().filter(|c| !c.is_control()).collect::<String>()
                )?;
                self.output.flush()?;
            }
        }
        Ok(())
    }
}

pub(crate) fn connect(yaml: std::path::PathBuf) -> Result<Value> {
    let mut display = Display {
        terminal: std::io::stdout().is_terminal() && std::env::var("TERM").as_deref() != Ok("dumb"),
        output: std::io::stdout(),
        pending: None,
    };
    display.show("running", "Validating configuration")?;
    let cfg = match crate::server::compile(yaml) {
        Ok(cfg) => cfg,
        Err(error) => {
            display.show("failed", "Configuration validation failed")?;
            return Err(error);
        }
    };
    display.show("done", "Validated configuration")?;
    let path = cfg.home.join(".silicon/silicon.log");
    let mut offset = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    display.show("running", "Connecting Silicon")?;
    let result = thread::scope(|scope| {
        let request = scope.spawn(|| {
            crate::server::call(
                &crate::server::daemon(true)?,
                "connect",
                json!({"yaml": cfg.path}),
            )
        });
        while !request.is_finished() {
            display.lines(&path, &mut offset)?;
            thread::sleep(Duration::from_millis(100));
        }
        let result = request
            .join()
            .map_err(|_| anyhow::anyhow!("connection request panicked"))?;
        display.lines(&path, &mut offset)?;
        result
    });
    if display.pending.is_some() || result.is_err() {
        display.show(
            if result.is_ok() { "done" } else { "failed" },
            if result.is_ok() {
                "Connected Silicon"
            } else {
                "Connection failed"
            },
        )?;
    }
    result.context("connect failed")
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
}
