//! Failures carry everything the failing tool said. A Carbon or Silicon can only fix
//! what it can read, so exit status, stderr and stdout travel verbatim and untruncated.
//! Only literal credential values are masked; the surrounding error always survives.
use regex::Regex;
use std::{ffi::OsStr, path::Path, process::Output, sync::OnceLock};

/// Shell-quoted argv for naming a command in an error, e.g. `ting webhook URL --json`.
/// Pass the result through [`mask`] (the constructors below do) when it may hold a secret.
pub(crate) fn argv<S: AsRef<str>>(program: impl AsRef<OsStr>, args: &[S]) -> String {
    let program = Path::new(program.as_ref())
        .file_name()
        .unwrap_or(program.as_ref())
        .to_string_lossy()
        .into_owned();
    shell_words::join(std::iter::once(program.as_str()).chain(args.iter().map(AsRef::as_ref)))
}

/// Exit status plus both streams, verbatim. Stderr comes first because that is where
/// most tools explain themselves; JSON-speaking tools often answer on stdout instead.
pub(crate) fn describe(output: &Output) -> String {
    let mut text = output.status.to_string();
    for (name, bytes) in [("stderr", &output.stderr), ("stdout", &output.stdout)] {
        let stream = String::from_utf8_lossy(bytes);
        let stream = stream.trim_end_matches(['\r', '\n']);
        if stream.trim().is_empty() {
            text.push_str(&format!("\n{name}: (empty)"));
        } else {
            text.push_str(&format!("\n{name}:\n{stream}"));
        }
    }
    text
}

/// A command that ran and failed: `` `command` failed: exit status: 1 `` then its streams.
/// `secrets` lists values this call handed the tool (tokens, config JSON) so they can be
/// masked in addition to everything registered for `home`.
pub(crate) fn command(
    home: &Path,
    command: &str,
    output: &Output,
    secrets: &[&str],
) -> anyhow::Error {
    anyhow::anyhow!(
        "{}",
        mask(
            home,
            &format!("`{command}` failed: {}", describe(output)),
            secrets
        )
    )
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
    anyhow::anyhow!(
        "{}",
        mask(
            home,
            &format!("`{command}` {problem}\n{}", describe(output)),
            secrets
        )
    )
}

/// A command that could not start at all, with the operating system's reason.
pub(crate) fn spawn(home: &Path, command: &str, error: &std::io::Error) -> anyhow::Error {
    anyhow::anyhow!(
        "{}",
        mask(home, &format!("could not run `{command}`: {error}"), &[])
    )
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
        Err(later) => anyhow::anyhow!("{error:#}\nalso: {later:#}"),
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
