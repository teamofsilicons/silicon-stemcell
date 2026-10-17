//! Failures carry everything the failing tool said. A Carbon or Silicon can only fix
//! what it can read, so exit status, stderr and stdout travel verbatim and untruncated.
//! Only literal credential values are masked; the surrounding error always survives.
use regex::Regex;
use std::{ffi::OsStr, fmt, path::Path, process::Output, sync::OnceLock};

/// Shell-quoted argv for naming a command in an error, e.g. `ting webhook URL --json`.
/// Pass the result through [`mask`] (the constructors below do) when it may hold a secret.
pub(crate) fn argv<S: AsRef<str>>(program: impl AsRef<OsStr>, args: &[S]) -> String {
    let program = program.as_ref().to_string_lossy();
    shell_words::join(std::iter::once(program.as_ref()).chain(args.iter().map(AsRef::as_ref)))
}

/// Exit status plus both streams, verbatim. Stderr comes first because that is where
/// most tools explain themselves; JSON-speaking tools often answer on stdout instead.
pub(crate) fn describe(output: &Output) -> String {
    format!("{}\n{}", output.status, streams(output))
}

fn streams(output: &Output) -> String {
    let mut text = String::new();
    for (name, bytes) in [("stderr", &output.stderr), ("stdout", &output.stdout)] {
        if !text.is_empty() {
            text.push('\n');
        }
        let stream = String::from_utf8_lossy(bytes);
        let stream = stream.trim_end_matches(['\r', '\n']);
        if stream.trim().is_empty() {
            text.push_str(&format!("{name}: (empty)"));
        } else {
            text.push_str(&format!("{name}:\n{stream}"));
        }
    }
    text
}

#[derive(Debug)]
struct ToolFailure {
    diagnostic: String,
    command: String,
    status: String,
    project: Option<String>,
    meaning: Option<String>,
}

impl fmt::Display for ToolFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}\n\nSource: `{}`\nStatus: {}",
            self.diagnostic, self.command, self.status
        )?;
        if let Some(project) = &self.project {
            write!(f, "\nProject: {project}")?;
        }
        if let Some(meaning) = &self.meaning {
            write!(f, "\nMeaning: {meaning}")?;
        }
        Ok(())
    }
}

impl std::error::Error for ToolFailure {}

/// These are bundled or required tools with known projects. App links come from discovery;
/// an arbitrary command name is not enough to invent a repository URL.
fn project(command: &str) -> Option<&'static str> {
    let args = shell_words::split(command).ok()?;
    let name = Path::new(args.first()?).file_name()?.to_str()?;
    match name {
        "silicon" | "si" => Some("https://github.com/teamofsilicons/silicon-stemcell"),
        "silicon-apps" => Some("https://github.com/teamofsilicons/silicon-apps"),
        "silicon-accounts" => Some("https://github.com/teamofsilicons/silicon-accounts"),
        "ting" | "ting-daemon" => Some("https://github.com/teamofsilicons/silicon-ting"),
        "omnid" | "omni" | "silicon-omni" | "so" => {
            Some("https://github.com/teamofsilicons/silicon-omni")
        }
        "caddy" => Some("https://github.com/caddyserver/caddy"),
        _ => None,
    }
}

/// A command that ran and failed, retaining its raw answer before attribution or explanation.
/// `secrets` lists values this call handed the tool (tokens, config JSON) so they can be
/// masked in addition to everything registered for `home`.
pub(crate) fn command(
    home: &Path,
    command: &str,
    output: &Output,
    secrets: &[&str],
) -> anyhow::Error {
    ToolFailure {
        diagnostic: mask(home, &streams(output), secrets),
        command: mask(home, command, secrets),
        status: output.status.to_string(),
        project: project(command).map(str::to_owned),
        meaning: None,
    }
    .into()
}

/// Like [`command`], for a command that exited successfully but answered wrongly.
/// `problem` says what was wrong (e.g. "returned invalid JSON: expected value at line 1").
pub(crate) fn answer(
    home: &Path,
    command: &str,
    problem: &str,
    output: &Output,
    secrets: &[&str],
) -> anyhow::Error {
    explain(
        self::command(home, command, output, secrets),
        &mask(home, problem, secrets),
    )
}

/// A command that could not start at all, with the operating system's reason.
pub(crate) fn spawn(home: &Path, command: &str, error: &std::io::Error) -> anyhow::Error {
    ToolFailure {
        diagnostic: mask(home, &error.to_string(), &[]),
        command: mask(home, command, &[]),
        status: "process did not start".into(),
        project: project(command).map(str::to_owned),
        meaning: None,
    }
    .into()
}

/// Add only an explanation supported by the diagnostic, without replacing its cause chain.
pub(crate) fn explain(mut error: anyhow::Error, meaning: &str) -> anyhow::Error {
    if let Some(tool) = error.downcast_mut::<ToolFailure>() {
        tool.meaning = Some(meaning.to_owned());
        error
    } else {
        error.context(format!("Meaning: {meaning}"))
    }
}

/// An app's successfully validated discovery response may identify its project.
pub(crate) fn with_project(mut error: anyhow::Error, project: Option<&str>) -> anyhow::Error {
    if let Some(project) = project.filter(|url| {
        url.parse::<ureq::http::Uri>().is_ok_and(|uri| {
            matches!(uri.scheme_str(), Some("http" | "https"))
                && uri
                    .authority()
                    .is_some_and(|host| !host.as_str().contains('@'))
        })
    }) {
        if let Some(tool) = error.downcast_mut::<ToolFailure>() {
            tool.project = Some(project.to_owned());
        }
    }
    error
}

#[derive(Debug)]
struct Received(String);

impl fmt::Display for Received {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Received {}

/// A report already rendered at an output boundary; callers may append their own context.
pub(crate) fn received(text: String) -> anyhow::Error {
    Received(text).into()
}

/// Present the actual failure first, then follow its context outward to the caller.
/// Formatting is deliberately separate from classification: retry and shutdown types survive.
pub fn report(error: &anyhow::Error) -> String {
    let mut chain: Vec<_> = error.chain().collect();
    let root = chain.pop().expect("an error always has a cause");
    let mut text = root.to_string();
    if root.is::<silicon_omni::DaemonError>() {
        text.push_str("\n\nSource: Silicon Omni client/daemon interface\nProject: https://github.com/teamofsilicons/silicon-omni");
    } else if !root.is::<ToolFailure>() && !root.is::<Received>() {
        text.push_str("\n\nSource: Silicon interpreter (reporting component)\nProject: https://github.com/teamofsilicons/silicon-stemcell");
    }
    for context in chain.into_iter().rev() {
        text.push_str(&format!("\nVia: {context}"));
    }
    mask_all(&text)
}

/// Mask literal credential values, recognized token prefixes, and the values of
/// credential-named JSON fields. Everything else in `text` is kept exactly.
pub(crate) fn mask(home: &Path, text: &str, secrets: &[&str]) -> String {
    mask_with(crate::redaction::known_secrets(home), text, secrets)
}

/// [`mask`] against every Silicon registered in this process, for text no single home
/// owns, such as the interpreter's HTTP error answers.
pub(crate) fn mask_all(text: &str) -> String {
    mask_with(crate::redaction::all_secrets(), text, &[])
}

fn mask_with(known: Vec<String>, text: &str, secrets: &[&str]) -> String {
    static FIELDS: OnceLock<Regex> = OnceLock::new();
    // Registered values under the redaction floor are settings ("en", "true"), and masking
    // every occurrence would garble the errors people must read. `secrets` handed to
    // this call are credentials by definition, so they are masked whatever their length.
    let mut values: Vec<String> = known
        .into_iter()
        .filter(|secret| secret.len() >= crate::redaction::MIN_SECRET_LEN)
        .chain(
            secrets
                .iter()
                .filter(|secret| !secret.trim().is_empty())
                .map(|secret| (*secret).to_owned()),
        )
        .collect();
    // A value can also appear escaped: shell-quoted in a named command, or inside JSON.
    let escaped: Vec<String> = values
        .iter()
        .flat_map(|secret| {
            let json = serde_json::to_string(secret).unwrap_or_default();
            [
                secret.replace('\'', r"'\''"),
                json.get(1..json.len().saturating_sub(1))
                    .unwrap_or_default()
                    .to_owned(),
            ]
        })
        .collect();
    values.extend(escaped);
    // Replace longer values first so a secret containing another is masked whole.
    values.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
    values.dedup();
    let mut text = text.to_owned();
    for secret in values.iter().filter(|secret| !secret.is_empty()) {
        text = text.replace(secret.as_str(), "[redacted]");
    }
    let text = crate::redaction::redact_text(&text, &[]);
    FIELDS
        .get_or_init(|| {
            Regex::new(
                r#"(?i)("[a-z0-9_.-]*(?:token|secret|password|credential|authorization|table_key|api_key|apikey|slt|stk)[a-z0-9_.-]*"\s*:\s*)"(?:[^"\\]|\\.)*""#,
            )
            .unwrap()
        })
        .replace_all(&text, r#"$1"[redacted]""#)
        .into_owned()
}

/// Keep `error` first; a later failure (reporting, cleanup) rides along instead of replacing it.
pub(crate) fn also(error: anyhow::Error, later: anyhow::Result<()>) -> anyhow::Error {
    match later {
        Ok(()) => error,
        Err(later) => error.context(format!("Additional failure:\n{}", report(&later))),
    }
}

/// The text a panic carried, for errors that would otherwise just say "panicked".
pub(crate) fn panic_message(payload: &(dyn std::any::Any + Send)) -> &str {
    payload
        .downcast_ref::<&str>()
        .copied()
        .or_else(|| payload.downcast_ref::<String>().map(String::as_str))
        .unwrap_or("(the panic carried no message)")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::process::ExitStatusExt;

    fn output(status: i32, stderr: &str, stdout: &str) -> Output {
        Output {
            status: std::process::ExitStatus::from_raw(status << 8),
            stderr: stderr.as_bytes().to_vec(),
            stdout: stdout.as_bytes().to_vec(),
        }
    }

    #[test]
    fn tool_report_keeps_raw_streams_then_source_and_outward_context() {
        let output = output(
            2,
            "first line\n  second line\n",
            "{\"error\":\"details\"}\n",
        );
        let error = command(
            Path::new("/silicon-error-test"),
            "'/opt/installed tools/ting' webhook http://localhost --json",
            &output,
            &[],
        )
        .context("register a webhook")
        .context("connect silicon.yaml");
        let text = report(&error);
        assert!(text
            .starts_with("stderr:\nfirst line\n  second line\nstdout:\n{\"error\":\"details\"}"));
        assert!(
            text.contains("Source: `'/opt/installed tools/ting' webhook http://localhost --json`")
        );
        assert!(text.contains("Status: exit status: 2"));
        assert!(text.contains("Project: https://github.com/teamofsilicons/silicon-ting"));
        assert!(text.ends_with("Via: register a webhook\nVia: connect silicon.yaml"));
        assert!(!text.contains("Meaning:"));
    }

    #[test]
    fn unknown_tool_gets_no_invented_meaning_or_project() {
        let error = command(
            Path::new("/silicon-error-test"),
            "custom-app sync",
            &output(1, "unrecognized upstream problem", ""),
            &[],
        );
        let text = report(&error);
        assert!(text.starts_with("stderr:\nunrecognized upstream problem"));
        assert!(text.contains("Source: `custom-app sync`"));
        assert!(!text.contains("Meaning:"));
        assert!(!text.contains("Project:"));
    }

    #[test]
    fn omni_errors_retain_the_known_interface_source_and_type() {
        for original in [
            silicon_omni::DaemonError::Daemon("provider rejected the request".into()),
            silicon_omni::DaemonError::Disconnected,
        ] {
            let diagnostic = original.to_string();
            let error = anyhow::Error::new(original).context("reading the Omni event stream");
            let text = report(&error);
            assert!(text.starts_with(&diagnostic));
            assert!(text.contains("Source: Silicon Omni client/daemon interface"));
            assert!(text.contains("Project: https://github.com/teamofsilicons/silicon-omni"));
            assert!(!text.contains("silicon-stemcell"));
            assert!(!text.contains("Meaning:"));
            assert!(text.ends_with("Via: reading the Omni event stream"));
            assert!(error.is::<silicon_omni::DaemonError>());
        }
    }

    #[test]
    fn explanation_and_discovered_project_preserve_context_and_redact_every_field() {
        let error = command(
            Path::new("/silicon-error-test"),
            "custom-app login --slt opaque-test-credential",
            &output(
                2,
                "rejected opaque-test-credential",
                "{\"access_token\":\"different-secret\"}",
            ),
            &["opaque-test-credential"],
        )
        .context("authenticate custom-app");
        let error = with_project(error, Some("https://example.org/custom-app"));
        let error = explain(error, "The app rejected the login syntax.");
        let text = report(&error);
        assert!(text.contains("rejected [redacted]"));
        assert!(text.contains("login --slt [redacted]"));
        assert!(!text.contains("opaque-test-credential"));
        assert!(!text.contains("different-secret"));
        assert!(text.contains("Project: https://example.org/custom-app"));
        assert!(text.contains("Meaning: The app rejected the login syntax."));
        assert!(text.ends_with("Via: authenticate custom-app"));
        assert!(error.is::<ToolFailure>());
    }

    #[test]
    fn invalid_project_urls_do_not_become_links() {
        for project in [
            "javascript:alert(1)",
            "file:///tmp/repo",
            "https://user:password@example.org/repo",
            "https://example.org/repo\nforged diagnostic",
            "not a URL",
        ] {
            let error = command(
                Path::new("/silicon-error-test"),
                "custom-app sync",
                &output(1, "failed", ""),
                &[],
            );
            assert!(!report(&with_project(error, Some(project))).contains("Project:"));
        }
    }

    #[test]
    fn cleanup_failure_cannot_replace_or_erase_the_primary_error_type() {
        let original = std::io::Error::new(std::io::ErrorKind::PermissionDenied, "primary failure");
        let error = also(original.into(), Err(anyhow::anyhow!("cleanup failure")));
        assert!(error.is::<std::io::Error>());
        let text = report(&error);
        assert!(text.starts_with("primary failure"));
        assert!(text.find("primary failure").unwrap() < text.find("cleanup failure").unwrap());
        assert_eq!(text.matches("primary failure").count(), 1);
    }

    #[test]
    fn invalid_answers_and_spawn_errors_retain_the_actual_diagnostic() {
        let home = Path::new("/silicon-error-test");
        let error = answer(
            home,
            "custom-app status",
            "returned invalid JSON: expected value",
            &output(0, "", "not JSON"),
            &[],
        );
        let text = report(&error);
        assert!(text.starts_with("stderr: (empty)\nstdout:\nnot JSON"));
        assert!(text.contains("Meaning: returned invalid JSON: expected value"));
        assert!(text.contains("Status: exit status: 0"));
        let error = spawn(
            home,
            "missing-app",
            &std::io::Error::from(std::io::ErrorKind::NotFound),
        );
        let text = report(&error);
        assert!(text.starts_with("entity not found"));
        assert!(text.contains("Source: `missing-app`\nStatus: process did not start"));
    }
}
