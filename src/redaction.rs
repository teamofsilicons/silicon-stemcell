//! Credential redaction for local logs, errors, and configuration output.
use crate::{config::Config, Recover};
use regex::Regex;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
};

// Retain known secrets until exit so late work cannot leak them after reconnect.
static REDACTIONS: OnceLock<Mutex<HashMap<PathBuf, Vec<String>>>> = OnceLock::new();

pub fn register(cfg: &Config) {
    let secrets = silicon_secrets(&serde_json::to_value(&cfg.silicon).unwrap());
    let mut redactions = REDACTIONS.get_or_init(Default::default).lock().recover();
    let known = redactions.entry(cfg.home.clone()).or_default();
    for secret in secrets {
        if !known.contains(&secret) {
            known.push(secret);
        }
    }
    known.sort_by_key(|secret| std::cmp::Reverse(secret.len()));
}

/// App-specific credential names are unknown, so every configured string is private.
pub fn silicon_secrets(silicon: &Value) -> Vec<String> {
    fn strings(value: &Value, secrets: &mut Vec<String>) {
        match value {
            Value::String(value) if !value.is_empty() => secrets.push(value.clone()),
            Value::Array(items) => items.iter().for_each(|item| strings(item, secrets)),
            Value::Object(items) => items.values().for_each(|item| strings(item, secrets)),
            _ => {}
        }
    }
    let mut secrets = Vec::new();
    for value in [
        &silicon["token"],
        &silicon["space_station"]["table_key"],
        &silicon["app_configs"],
    ] {
        strings(value, &mut secrets);
    }
    secrets.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
    secrets.dedup();
    secrets
}

/// Values registered for `home` (its token, table key and every app config string).
pub(crate) fn known_secrets(home: &Path) -> Vec<String> {
    REDACTIONS
        .get_or_init(Default::default)
        .lock()
        .recover()
        .get(home)
        .cloned()
        .unwrap_or_default()
}

/// Values registered for every home this process has seen.
pub(crate) fn all_secrets() -> Vec<String> {
    REDACTIONS
        .get_or_init(Default::default)
        .lock()
        .recover()
        .values()
        .flatten()
        .cloned()
        .collect()
}

/// Shorter configured values ("en", "true", "30") are settings, not credentials;
/// replacing every occurrence of them garbles the errors and logs people must read.
pub(crate) const MIN_SECRET_LEN: usize = 8;

pub fn redact_text(text: &str, secrets: &[String]) -> String {
    static TOKENS: OnceLock<Regex> = OnceLock::new();
    let mut text = text.to_owned();
    // Every log line passes here twice; a secret that is not in it costs no new string.
    for secret in secrets.iter().filter(|s| s.len() >= MIN_SECRET_LEN) {
        if text.contains(secret.as_str()) {
            text = text.replace(secret, "[redacted]");
        }
    }
    TOKENS
        .get_or_init(|| {
            Regex::new(r"(?i)\b(?:stk|slt|sat|srt|sscli|apikey|spacewindow|table)-[a-z0-9_.-]{8,}")
                .unwrap()
        })
        .replace_all(&text, "[redacted]")
        .into_owned()
}

pub fn redact_value(value: &mut Value, secrets: &[String]) {
    match value {
        Value::Object(fields) => {
            for (key, value) in fields {
                let name = key.to_ascii_lowercase();
                if [
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "authorization",
                    "table_key",
                    "api_key",
                    "slt",
                    "stk",
                ]
                .iter()
                .any(|word| name.contains(word))
                {
                    *value = json!("[redacted]");
                } else {
                    redact_value(value, secrets);
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                redact_value(item, secrets);
            }
        }
        Value::String(text) => *text = redact_text(text, secrets),
        _ => {}
    }
}

pub fn redact(home: &Path, text: &str) -> String {
    let contexts = REDACTIONS.get_or_init(Default::default).lock().recover();
    let secrets = contexts.get(home).map(|c| c.as_slice()).unwrap_or(&[]);
    if let Ok(mut value) = serde_json::from_str::<Value>(text) {
        redact_value(&mut value, secrets);
        value.to_string()
    } else {
        redact_text(text, secrets)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_output_keeps_masking_configured_and_named_credentials() {
        let silicon = json!({
            "token": "account-secret-value",
            "space_station": {"table_key": "legacy-table-secret"},
            "app_configs": {"app": {"custom": "private-app-config", "language": "en"}}
        });
        let secrets = silicon_secrets(&silicon);
        assert_eq!(
            redact_text(
                "account-secret-value legacy-table-secret private-app-config en",
                &secrets
            ),
            "[redacted] [redacted] [redacted] en"
        );
        let mut output = json!({
            "authorization": "short",
            "messages": ["private-app-config", "stk-123456789"],
            "status": "connected"
        });
        redact_value(&mut output, &secrets);
        assert_eq!(
            output,
            json!({
                "authorization": "[redacted]",
                "messages": ["[redacted]", "[redacted]"],
                "status": "connected"
            })
        );
    }
}
