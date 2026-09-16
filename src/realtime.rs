//! One multiplexed read-only WebSocket per interpreter. Local execution never waits on the relay.
use crate::{
    config::Config,
    realtime_cli::{base, testing},
    runtime::Runtime,
};
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    net::TcpStream,
    path::{Path, PathBuf},
    sync::{
        mpsc::{self, SyncSender, TryRecvError},
        Arc, OnceLock,
    },
    thread,
    time::{Duration, Instant},
};
use tungstenite::{stream::MaybeTlsStream, Message, WebSocket};
use uuid::Uuid;

pub const APP_ID: &str = "tos>silicon-realtime";
pub const BACKEND: &str = "https://realtime.teamofsilicons.com";
static EVENTS: OnceLock<SyncSender<(PathBuf, Uuid, Value)>> = OnceLock::new();

pub fn publish(home: &Path, generation: Uuid, event: Value) {
    if let Some(sender) = EVENTS.get() {
        let _ = sender.try_send((home.into(), generation, event));
    }
}
pub fn configuration(cfg: &Config) -> Value {
    let mut value = serde_json::to_value(cfg).unwrap_or(Value::Null);
    let mut secrets = Vec::new();
    if let Some(token) = &cfg.silicon.token {
        secrets.push(token.clone());
    }
    if let Some(key) = value
        .pointer("/silicon/space_station/table_key")
        .and_then(Value::as_str)
    {
        secrets.push(key.into());
    }
    crate::telemetry::redact_value(&mut value, &secrets);
    value
}
fn enabled() -> bool {
    crate::settings::load().is_ok_and(|s| s.realtime)
        && std::env::var("SILICON_REALTIME").as_deref() != Ok("0")
}
fn telemetry_enabled() -> bool {
    crate::settings::load().is_ok_and(|s| s.telemetry)
        && std::env::var("SILICON_TELEMETRY").as_deref() != Ok("0")
}

fn exchange(base: &str, mut body: Value) -> Result<Value> {
    body["testing_context"] = testing()?;
    let agent = ureq::Agent::config_builder()
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(20)))
        .build()
        .new_agent();
    let mut response = agent
        .post(&format!("{base}/v1/auth/token"))
        .send_json(body)
        .map_err(|_| anyhow!("realtime token exchange failed"))?;
    response
        .body_mut()
        .read_json()
        .context("invalid realtime authentication response")
}
fn token(cfg: &Config, base: &str) -> Result<String> {
    let file = cfg.home.join(".silicon/realtime.json");
    let mut saved: Value = fs::read(&file)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null);
    let test = testing()?;
    let plane = test["iam_test_key"].as_str().unwrap_or("");
    if saved["backend"] == base
        && saved["plane"] == plane
        && saved["silicon"] == cfg.silicon.id.as_deref().unwrap_or("")
    {
        if saved["valid_until"].as_i64().unwrap_or(0) > chrono::Utc::now().timestamp() + 600 {
            if let Some(token) = saved["access_token"].as_str() {
                return Ok(token.into());
            }
        }
        if let Some(refresh) = saved["refresh_token"].as_str().map(str::to_owned) {
            let key = saved["refresh_key"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| Uuid::new_v4().to_string());
            saved["refresh_key"] = json!(key);
            crate::state::write_json(&file, &saved)?;
            if let Ok(tokens) =
                exchange(base, json!({"refresh_token":refresh,"idempotency_key":key}))
            {
                return save_tokens(&file, cfg, base, plane, tokens);
            }
        }
    }
    let sid = cfg.silicon.id.as_deref().context("silicon.id missing")?;
    if !test.is_null() {
        let tokens = exchange(
            base,
            json!({"slt":sid,"idempotency_key":Uuid::new_v4().to_string()}),
        )?;
        return save_tokens(&file, cfg, base, plane, tokens);
    }
    let (_, org) = sid
        .split_once(':')
        .context("silicon.id must be local:org")?;
    let iam_home = cfg.home.join(".silicon-iam");
    if fs::symlink_metadata(&iam_home).is_ok_and(|m| m.file_type().is_symlink()) {
        bail!("the Silicon IAM credential directory must not be a symlink");
    }
    crate::state::private_dir(&iam_home)?;
    let output = crate::command("iam", &cfg.home)
        .args([
            "--json",
            "--org",
            org,
            "silicon-login",
            "--sid",
            sid,
            "--stk",
            cfg.silicon.token.as_deref().unwrap_or_default(),
            "--app-id",
            APP_ID,
            "--grant-org",
            org,
            "--approve-scopes",
        ])
        .output()
        .context("realtime authentication requires iam CLI")?;
    if !output.status.success() {
        bail!("IAM could not authorize Silicon Realtime; check Silicon credentials and IAM environment")
    }
    let issued: Value =
        serde_json::from_slice(&output.stdout).context("IAM returned invalid JSON")?;
    let slt = issued["slt"]
        .as_str()
        .context("IAM did not return a short-lived token")?;
    let tokens = exchange(
        base,
        json!({"slt":slt,"idempotency_key":Uuid::new_v4().to_string()}),
    )?;
    save_tokens(&file, cfg, base, plane, tokens)
}
fn save_tokens(
    file: &Path,
    cfg: &Config,
    base: &str,
    plane: &str,
    mut tokens: Value,
) -> Result<String> {
    let token = tokens["access_token"]
        .as_str()
        .context("realtime access token missing")?
        .to_owned();
    tokens["valid_until"] = json!(
        chrono::Utc::now().timestamp() + tokens["expires_in"].as_i64().unwrap_or(1800).min(1800)
    );
    tokens["backend"] = json!(base);
    tokens["plane"] = json!(plane);
    tokens["silicon"] = json!(cfg.silicon.id);
    crate::state::write_json(file, &tokens)?;
    Ok(token)
}
fn timeout(socket: &mut WebSocket<MaybeTlsStream<TcpStream>>) -> Result<()> {
    let stream = match socket.get_mut() {
        MaybeTlsStream::Plain(s) => s,
        MaybeTlsStream::Rustls(s) => &mut s.sock,
        _ => bail!("unsupported realtime TLS stream"),
    };
    stream.set_read_timeout(Some(Duration::from_millis(100)))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    Ok(())
}
fn send(socket: &mut WebSocket<MaybeTlsStream<TcpStream>>, value: Value) -> Result<()> {
    socket
        .send(Message::Text(value.to_string().into()))
        .context("realtime websocket send failed")
}

pub fn start(runtime: &Arc<Runtime>) {
    let (sender, receiver) = mpsc::sync_channel(2048);
    if EVENTS.set(sender).is_err() {
        return;
    }
    let runtime = Arc::downgrade(runtime);
    thread::spawn(move || loop {
        let Some(runtime) = runtime.upgrade() else {
            return;
        };
        if runtime.stopping.load(std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        if !enabled() || runtime.silicons.read().unwrap().is_empty() {
            while receiver.try_recv().is_ok() {}
            thread::sleep(Duration::from_secs(1));
            continue;
        }
        let result = (|| -> Result<()> {
            let base = base()?;
            let ws = format!(
                "{}/v1/publish",
                base.replacen("https://", "wss://", 1)
                    .replacen("http://", "ws://", 1)
            );
            let mut request = tungstenite::client::IntoClientRequest::into_client_request(ws)?;
            request.headers_mut().insert(
                "User-Agent",
                tungstenite::http::HeaderValue::from_static(concat!(
                    "silicon/",
                    env!("CARGO_PKG_VERSION")
                )),
            );
            let (mut socket, _) = tungstenite::client::connect_with_config(request, None, 0)?;
            timeout(&mut socket)?;
            let mut registered: HashMap<String, (PathBuf, Uuid, Instant)> = HashMap::new();
            let mut check = Instant::now() - Duration::from_secs(15);
            while enabled() && !runtime.stopping.load(std::sync::atomic::Ordering::SeqCst) {
                if check.elapsed() >= Duration::from_secs(10) {
                    let current: Vec<_> =
                        runtime.silicons.read().unwrap().values().cloned().collect();
                    let removed: Vec<_> = registered
                        .keys()
                        .filter(|id| {
                            !current
                                .iter()
                                .any(|c| c.cfg.silicon.id.as_ref() == Some(*id))
                        })
                        .cloned()
                        .collect();
                    for id in removed {
                        send(&mut socket, json!({"type":"unregister","silicon":id}))?;
                        registered.remove(&id);
                    }
                    for connected in current {
                        let cfg = &connected.cfg;
                        let id = cfg.silicon.id.as_ref().unwrap();
                        if registered.get(id).is_none_or(|(_, generation, time)| {
                            *generation != connected.generation
                                || time.elapsed() >= Duration::from_secs(1200)
                        }) {
                            match token(cfg, &base) {
                                Ok(token) => {
                                    let org = id.split_once(':').unwrap().1;
                                    send(
                                        &mut socket,
                                        json!({"type":"register","telemetry":telemetry_enabled(),"silicon":id,"org_id":org,"access_token":token,"configuration":configuration(cfg),"testing_context":testing()?}),
                                    )?;
                                    registered.insert(
                                        id.clone(),
                                        (cfg.home.clone(), connected.generation, Instant::now()),
                                    );
                                }
                                Err(error) => {
                                    eprintln!("realtime authentication for {id}: {error}")
                                }
                            }
                        }
                    }
                    send(
                        &mut socket,
                        json!({"type":"ping","telemetry":telemetry_enabled()}),
                    )?;
                    check = Instant::now();
                }
                for _ in 0..256 {
                    match receiver.try_recv() {
                        Ok((home, generation, mut data)) => {
                            if let Some((id, _)) =
                                registered
                                    .iter()
                                    .find(|(_, (path, current_generation, _))| {
                                        path == &home && *current_generation == generation
                                    })
                            {
                                let Ok(connected) = runtime.get(id) else {
                                    continue;
                                };
                                if connected.generation != generation {
                                    continue;
                                }
                                {
                                    let secrets = connected
                                        .cfg
                                        .silicon
                                        .token
                                        .iter()
                                        .cloned()
                                        .collect::<Vec<_>>();
                                    crate::telemetry::redact_value(&mut data, &secrets);
                                }
                                send(
                                    &mut socket,
                                    json!({"type":"event","telemetry":telemetry_enabled(),"silicon":id,"data":data}),
                                )?;
                            }
                        }
                        Err(TryRecvError::Empty) => break,
                        Err(TryRecvError::Disconnected) => return Ok(()),
                    }
                }
                match socket.read() {
                    Ok(Message::Close(_)) => bail!("realtime relay closed connection"),
                    Ok(Message::Text(body)) => {
                        let value: Value = serde_json::from_str(&body)?;
                        if value["error"].is_string() {
                            bail!("realtime relay rejected a message; verify IAM authorization")
                        }
                    }
                    Ok(_) => {}
                    Err(tungstenite::Error::Io(e))
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                        ) => {}
                    Err(e) => return Err(e.into()),
                }
            }
            let _ = socket.close(None);
            Ok(())
        })();
        if let Err(error) = result {
            eprintln!("realtime connection unavailable: {error}");
        }
        drop(runtime);
        thread::sleep(Duration::from_secs(10));
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn configuration_excludes_credentials_even_when_interpolated() {
        let mut value = json!({"silicon":{"token":"sensitive-value","space_station":{"table_key":"table-key"}},"isi":{"a":{"dna":"token sensitive-value"}}});
        crate::telemetry::redact_value(&mut value, &["sensitive-value".into(), "table-key".into()]);
        let text = value.to_string();
        assert!(!text.contains("sensitive-value"));
        assert!(!text.contains("table-key"));
        assert!(text.contains("redacted"));
    }
}
