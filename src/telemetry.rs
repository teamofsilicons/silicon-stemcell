//! Space Station supplies the queue, durable spool, retry, and shared websocket.
use crate::{config::Config, settings};
use regex::Regex;
use serde_json::{json, Value};
use space_station::SpaceClient;
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
    time::Duration,
};
use uuid::Uuid;

struct Context {
    id: String,
    generation: Uuid,
    user: Option<Arc<SpaceClient>>,
}
static CONTEXTS: OnceLock<Mutex<HashMap<PathBuf, Context>>> = OnceLock::new();
// ponytail: retain known secrets until exit so late work cannot leak after reconnect;
// use reference-counted log contexts if millions of connection changes become routine.
static REDACTIONS: OnceLock<Mutex<HashMap<PathBuf, Vec<String>>>> = OnceLock::new();
static CLIENTS: OnceLock<Mutex<HashMap<String, Arc<SpaceClient>>>> = OnceLock::new();

/// `what` names where the key came from, for the one message a bad key earns.
fn client(key: &str, what: &str) -> Option<Arc<SpaceClient>> {
    if key.is_empty() {
        return None;
    }
    let mut clients = CLIENTS.get_or_init(Default::default).lock().unwrap();
    if let Some(client) = clients.get(key) {
        return Some(client.clone());
    }
    let client = match SpaceClient::builder(key)
        .home(crate::server::directory().join("space-station"))
        .url(space_station::default_url())
        .flush_timeout(Duration::from_millis(100))
        .on_error(|error| eprintln!("Space Station telemetry: {error}"))
        .build()
    {
        Ok(client) => client,
        Err(error) => {
            // Telemetry must never fail the work it describes, nor vanish without a reason.
            once(
                &format!("client:{what}"),
                &format!("Space Station telemetry to {what} is off: {error}"),
            );
            return None;
        }
    };
    let client = Arc::new(client);
    clients.insert(key.into(), client.clone());
    Some(client)
}

pub fn register(cfg: &Config) {
    let secrets = silicon_secrets(&serde_json::to_value(&cfg.silicon).unwrap());
    let user = cfg
        .silicon
        .space_station
        .as_ref()
        .and_then(|station| client(&station.table_key, "silicon.space_station.table_key"));
    let mut redactions = REDACTIONS.get_or_init(Default::default).lock().unwrap();
    let known = redactions.entry(cfg.home.clone()).or_default();
    for secret in secrets {
        if !known.contains(&secret) {
            known.push(secret);
        }
    }
    known.sort_by_key(|secret| std::cmp::Reverse(secret.len()));
    drop(redactions);
    CONTEXTS
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .insert(
            cfg.home.clone(),
            Context {
                id: cfg.silicon.id.clone().unwrap_or_default(),
                generation: cfg.generation,
                user,
            },
        );
}

pub fn unregister(home: &Path) {
    CONTEXTS
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .remove(home);
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
        .unwrap()
        .get(home)
        .cloned()
        .unwrap_or_default()
}

/// Values registered for every home this process has seen.
pub(crate) fn all_secrets() -> Vec<String> {
    REDACTIONS
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .values()
        .flatten()
        .cloned()
        .collect()
}

/// Say `message` on stderr (daemon.log for the interpreter) once per `key` per process.
/// Telemetry runs inside every log write, so repeating it there would drown the log it serves.
fn once(key: &str, message: &str) {
    static SAID: OnceLock<Mutex<std::collections::HashSet<String>>> = OnceLock::new();
    if SAID
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .insert(key.to_owned())
    {
        eprintln!("{message}");
    }
}

/// Shorter configured values ("en", "true", "30") are settings, not credentials;
/// replacing every occurrence of them garbles the errors and logs people must read.
pub(crate) const MIN_SECRET_LEN: usize = 8;

pub fn redact_text(text: &str, secrets: &[String]) -> String {
    static TOKENS: OnceLock<Regex> = OnceLock::new();
    let mut text = text.to_owned();
    for secret in secrets.iter().filter(|s| s.len() >= MIN_SECRET_LEN) {
        text = text.replace(secret, "[redacted]");
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
    let contexts = REDACTIONS.get_or_init(Default::default).lock().unwrap();
    let secrets = contexts.get(home).map(|c| c.as_slice()).unwrap_or(&[]);
    if let Ok(mut value) = serde_json::from_str::<Value>(text) {
        redact_value(&mut value, secrets);
        value.to_string()
    } else {
        redact_text(text, secrets)
    }
}

fn vendor_enabled() -> bool {
    if cfg!(test) || std::env::var("SILICON_TELEMETRY").as_deref() == Ok("0") {
        return false;
    }
    match settings::load() {
        Ok(settings) => settings.telemetry,
        // An unreadable choice is not consent; say why telemetry is off.
        Err(error) => {
            once(
                &format!("settings:{error:#}"),
                &format!(
                    "Silicon telemetry off: interpreter settings could not be read: {error:#}"
                ),
            );
            false
        }
    }
}

fn vendor(runtime: bool) -> Option<Arc<SpaceClient>> {
    if !vendor_enabled() {
        return None;
    }
    let (name, bundled) = if runtime {
        (
            "SILICON_RUNTIME_TABLE_KEY",
            option_env!("SILICON_RUNTIME_TABLE_KEY"),
        )
    } else {
        (
            "SILICON_INTERPRETER_TABLE_KEY",
            option_env!("SILICON_INTERPRETER_TABLE_KEY"),
        )
    };
    client(
        &std::env::var(name)
            .ok()
            .or_else(|| bundled.map(str::to_owned))
            .unwrap_or_default(),
        name,
    )
}

pub fn record(home: &Path, kind: &str, origin: &str, message: &str) {
    record_scoped(home, None, kind, origin, message);
}

pub fn record_scoped(
    home: &Path,
    generation: Option<Uuid>,
    kind: &str,
    origin: &str,
    message: &str,
) {
    let (id, user) = CONTEXTS
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .get(home)
        .filter(|c| Some(c.generation) == generation)
        .map(|c| (c.id.clone(), c.user.clone()))
        .unwrap_or_default();
    let runtime = match kind {
        "setup" | "auth" | "app" | "webhook" | "runtime" => false,
        "command" | "error" => origin != "interpreter",
        _ => true,
    };
    let event = json!({"source":"silicon", "version":env!("CARGO_PKG_VERSION"),
        "step":kind, "origin":origin, "silicon_id":id, "isi":origin,
        "timestamp":chrono::Utc::now().to_rfc3339(), "message":message});
    if let Some(client) = vendor(runtime) {
        client.record(&event);
    }
    if let Some(client) = user {
        client.record(&event);
    }
}

fn redact_interpreter_context(context: &mut Value) {
    redact_value(context, &all_secrets());
}

pub fn interpreter(source: &str, step: &str, mut context: Value) {
    redact_interpreter_context(&mut context);
    if let Some(client) = vendor(false) {
        client.record(json!({"source":source,"step":step,"context":context,
            "version":env!("CARGO_PKG_VERSION"),"timestamp":chrono::Utc::now().to_rfc3339()}));
    }
}

pub fn flush() {
    let clients: Vec<_> = CLIENTS
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .values()
        .cloned()
        .collect();
    for client in clients {
        client.flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn interpreter_requests_redact_known_secrets_even_after_disconnect() {
        let home = PathBuf::from(format!("/test/{}", Uuid::new_v4()));
        let mut cfg: Config = serde_json::from_value(json!({
            "silicon": {"id":"si:test", "org_id":"org", "token":"private-token", "app_configs":{
                "app":{"nested":["custom-configured-secret", {"value":"nested-app-secret"}]}
            }}, "isi":{}, "access":{}, "flow":[]
        }))
        .unwrap();
        cfg.home = home.clone();
        register(&cfg);
        let env = crate::runtime::environment(&cfg);
        assert!(env["silicon"].get("app_configs").is_none());
        assert!(env["silicon"].get("token").is_none());
        unregister(&home);
        let mut request = json!({"action":"send","args":{"message":"using custom-configured-secret and nested-app-secret"}});
        redact_interpreter_context(&mut request);
        assert_eq!(
            request["args"]["message"],
            "using [redacted] and [redacted]"
        );
        REDACTIONS.get().unwrap().lock().unwrap().remove(&home);
    }
    #[test]
    fn redacts_nested_credentials_and_known_secrets_without_losing_events() {
        let mut value = json!({"type":"tool", "data":{"access_token":"private", "content":"using custom-secret and stk-1234567890"}, "metadata":{"isi":"worker"}});
        redact_value(&mut value, &["custom-secret".into()]);
        assert_eq!(value["data"]["access_token"], "[redacted]");
        assert_eq!(value["data"]["content"], "using [redacted] and [redacted]");
        assert_eq!(value["metadata"]["isi"], "worker");
    }
}
