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

fn client(key: &str) -> Option<Arc<SpaceClient>> {
    if key.is_empty() {
        return None;
    }
    let mut clients = CLIENTS.get_or_init(Default::default).lock().unwrap();
    if let Some(client) = clients.get(key) {
        return Some(client.clone());
    }
    let client = SpaceClient::builder(key)
        .home(crate::server::directory().join("space-station"))
        .url(space_station::default_url())
        .flush_timeout(Duration::from_millis(100))
        .on_error(|error| eprintln!("Space Station telemetry: {error}"))
        .build()
        .ok()?;
    let client = Arc::new(client);
    clients.insert(key.into(), client.clone());
    Some(client)
}

pub fn register(cfg: &Config) {
    let mut secrets = cfg.silicon.token.iter().cloned().collect::<Vec<_>>();
    let user = cfg.silicon.space_station.as_ref().and_then(|station| {
        secrets.push(station.table_key.clone());
        client(&station.table_key)
    });
    let mut redactions = REDACTIONS.get_or_init(Default::default).lock().unwrap();
    let known = redactions.entry(cfg.home.clone()).or_default();
    for secret in secrets {
        if !known.contains(&secret) {
            known.push(secret);
        }
    }
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

pub fn redact_text(text: &str, secrets: &[String]) -> String {
    static TOKENS: OnceLock<Regex> = OnceLock::new();
    let mut text = text.to_owned();
    for secret in secrets.iter().filter(|s| !s.is_empty()) {
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
    !cfg!(test)
        && std::env::var("SILICON_TELEMETRY").as_deref() != Ok("0")
        && settings::load().is_ok_and(|s| s.telemetry)
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
    let secrets = REDACTIONS
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .values()
        .flatten()
        .cloned()
        .collect::<Vec<_>>();
    redact_value(context, &secrets);
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
        REDACTIONS
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .insert(home.clone(), vec!["custom-configured-secret".into()]);
        unregister(&home);
        let mut request =
            json!({"action":"send","args":{"message":"using custom-configured-secret"}});
        redact_interpreter_context(&mut request);
        assert_eq!(request["args"]["message"], "using [redacted]");
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
