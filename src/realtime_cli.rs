//! IAM login and read-only subscriptions for Carbon or Silicon CLI users.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::TcpStream,
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};
use tungstenite::{stream::MaybeTlsStream, Message, WebSocket};
use uuid::Uuid;

pub(crate) fn base() -> Result<String> {
    validate_base(
        &std::env::var("SILICON_REALTIME_URL").unwrap_or_else(|_| crate::realtime::BACKEND.into()),
    )
}
fn validate_base(value: &str) -> Result<String> {
    let uri: tungstenite::http::Uri = value.parse().context("invalid realtime URL")?;
    let scheme = uri.scheme_str().unwrap_or_default();
    let host = uri.host().unwrap_or_default();
    if (scheme != "https"
        && !(scheme == "http" && matches!(host, "127.0.0.1" | "localhost" | "[::1]")))
        || host.is_empty()
        || uri.authority().is_some_and(|a| a.as_str().contains('@'))
        || uri.query().is_some()
        || !matches!(uri.path(), "" | "/")
        || value.contains('#')
    {
        bail!("realtime URL requires an HTTPS origin without credentials (HTTP allowed only on loopback)");
    }
    Ok(value.trim_end_matches('/').into())
}
pub(crate) fn testing() -> Result<Value> {
    match std::env::var_os("SILICON_REALTIME_TEST_CONTEXT") {
        None => Ok(Value::Null),
        Some(path) => serde_json::from_slice(&fs::read(path)?)
            .context("invalid realtime testing context file"),
    }
}

#[derive(Debug)]
struct HttpFailure(Option<u16>);
impl std::fmt::Display for HttpFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.0 {
            Some(code) => write!(f, "realtime request failed (HTTP {code})"),
            None => f.write_str("realtime service unavailable; saved credentials retained"),
        }
    }
}
impl std::error::Error for HttpFailure {}
fn unauthorized(error: &anyhow::Error) -> bool {
    error
        .downcast_ref::<HttpFailure>()
        .is_some_and(|e| matches!(e.0, Some(401 | 403)))
}
fn request(
    base: &str,
    context: &Value,
    path: &str,
    mut body: Value,
    token: Option<&str>,
) -> Result<Value> {
    body["testing_context"] = context.clone();
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(20)))
        .max_redirects(0)
        .build()
        .new_agent();
    let mut request = agent.post(&format!("{base}{path}"));
    if let Some(token) = token {
        request = request.header("Authorization", &format!("Bearer {token}"));
    }
    let mut response = request.send_json(body).map_err(|error| {
        HttpFailure(match error {
            ureq::Error::StatusCode(code) => Some(code),
            _ => None,
        })
    })?;
    response
        .body_mut()
        .read_json()
        .context("invalid realtime response")
}

struct Session {
    file: PathBuf,
    _lock: File,
    base: String,
    context: Value,
    saved: Value,
}
impl Session {
    fn open() -> Result<Self> {
        let home = std::env::var_os("SILICON_HOME")
            .or_else(|| std::env::var_os("HOME"))
            .context("HOME or SILICON_HOME is required")?;
        Self::at(Path::new(&home), base()?, testing()?)
    }
    fn at(home: &Path, base: String, context: Value) -> Result<Self> {
        let directory = home.join(".silicon");
        if fs::symlink_metadata(&directory).is_ok_and(|m| m.file_type().is_symlink()) {
            bail!("realtime credential directory must not be a symlink");
        }
        crate::state::private_dir(&directory)?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(directory.join("realtime-reader.lock"))?;
        // Separate CLI processes may rotate the same refresh token. Hold the lock only for a request transaction.
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let file = directory.join("realtime-reader.json");
        let saved = match OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&file)
        {
            Ok(mut file) => {
                let mut bytes = Vec::new();
                file.read_to_end(&mut bytes)?;
                serde_json::from_slice(&bytes).context("invalid realtime credentials")?
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Value::Null,
            Err(e) => return Err(e.into()),
        };
        Ok(Self {
            file,
            _lock: lock,
            base,
            context,
            saved,
        })
    }
    fn matches(&self) -> bool {
        self.saved["backend"] == self.base && self.saved["testing_context"] == self.context
    }
    fn save(&self) -> Result<()> {
        crate::state::write_json(&self.file, &self.saved)
    }
    fn save_tokens(&mut self, mut tokens: Value) -> Result<String> {
        let access = tokens["access_token"]
            .as_str()
            .filter(|v| !v.is_empty())
            .context("realtime response omitted its access token")?
            .to_owned();
        if tokens["refresh_token"]
            .as_str()
            .is_none_or(|v| v.is_empty())
        {
            bail!("realtime response omitted its refresh token");
        }
        let lifetime = tokens["expires_in"]
            .as_i64()
            .filter(|v| *v > 0)
            .context("realtime response omitted its token lifetime")?;
        tokens["valid_until"] = json!(chrono::Utc::now().timestamp() + lifetime.min(1800));
        tokens["backend"] = json!(self.base);
        tokens["testing_context"] = self.context.clone();
        self.saved = tokens;
        self.save()?;
        Ok(access)
    }
    fn access(&mut self) -> Result<Option<String>> {
        if !self.matches() {
            return Ok(None);
        }
        if self.saved["valid_until"].as_i64().unwrap_or_default()
            > chrono::Utc::now().timestamp() + 60
        {
            return Ok(self.saved["access_token"].as_str().map(str::to_owned));
        }
        let Some(refresh) = self.saved["refresh_token"].as_str().map(str::to_owned) else {
            return Ok(None);
        };
        let key = self.saved["refresh_key"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        self.saved["refresh_key"] = json!(key);
        self.save()?;
        let value = match request(
            &self.base,
            &self.context,
            "/v1/auth/token",
            json!({"refresh_token":refresh,"idempotency_key":key}),
            None,
        ) {
            Ok(value) => value,
            Err(error) if unauthorized(&error) => return Ok(None),
            Err(error) => return Err(error),
        };
        self.save_tokens(value).map(Some)
    }
    fn status(&mut self) -> Result<Value> {
        let Some(token) = self.access()? else {
            return Ok(json!({"authenticated":false}));
        };
        match request(
            &self.base,
            &self.context,
            "/v1/auth/status",
            json!({}),
            Some(&token),
        ) {
            Ok(status) if status["authenticated"].is_boolean() => Ok(status),
            Ok(_) => bail!("realtime status omitted authentication state"),
            Err(error) if unauthorized(&error) => Ok(json!({"authenticated":false})),
            Err(error) => Err(error),
        }
    }
}

impl Session {
    fn login(&mut self, slt: &str) -> Result<Value> {
        if slt.is_empty() {
            bail!("a short-lived IAM token is required");
        }
        let value = request(
            &self.base,
            &self.context,
            "/v1/auth/token",
            json!({"slt":slt,"idempotency_key":Uuid::new_v4().to_string()}),
            None,
        )?;
        self.save_tokens(value)?;
        let status = self.status()?;
        if status["authenticated"] != true {
            bail!("realtime login was not confirmed by IAM");
        }
        Ok(status)
    }
    fn logout(&mut self) -> Result<Value> {
        if !self.matches() {
            return Ok(json!({"authenticated":false}));
        }
        let Some(refresh) = self.saved["refresh_token"].as_str().map(str::to_owned) else {
            return Ok(json!({"authenticated":false}));
        };
        let key = self.saved["logout_key"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| Uuid::new_v4().to_string());
        self.saved["logout_key"] = json!(key);
        self.save()?;
        let value = request(
            &self.base,
            &self.context,
            "/v1/auth/logout",
            json!({"refresh_token":refresh,"idempotency_key":key}),
            None,
        )?;
        if value["authenticated"] != false || value["revoked"] != true {
            bail!("realtime logout was not confirmed; credentials retained");
        }
        fs::remove_file(&self.file)?;
        Ok(value)
    }
}
pub fn login(slt: &str) -> Result<Value> {
    Session::open()?.login(slt)
}
pub fn login_status() -> Result<Value> {
    Session::open()?.status()
}
pub fn logout() -> Result<Value> {
    Session::open()?.logout()
}
fn socket_timeout(socket: &mut WebSocket<MaybeTlsStream<TcpStream>>) -> Result<()> {
    let stream = match socket.get_mut() {
        MaybeTlsStream::Plain(s) => s,
        MaybeTlsStream::Rustls(s) => &mut s.sock,
        _ => bail!("unsupported realtime transport"),
    };
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    Ok(())
}
fn connect(sid: &str, org: &str) -> Result<WebSocket<MaybeTlsStream<TcpStream>>> {
    let mut session = Session::open()?;
    let token = session
        .access()?
        .context("login to Silicon Realtime with an IAM short-lived token first")?;
    let ticket = request(
        &session.base,
        &session.context,
        "/v1/subscribe",
        json!({"silicon":sid,"org_id":org}),
        Some(&token),
    )?;
    let ticket = ticket["ticket"]
        .as_str()
        .context("realtime subscription ticket missing")?
        .to_owned();
    let url = format!(
        "{}/v1/subscribe",
        session
            .base
            .replacen("https://", "wss://", 1)
            .replacen("http://", "ws://", 1)
    );
    drop(session);
    let mut request = tungstenite::client::IntoClientRequest::into_client_request(url)?;
    request.headers_mut().insert(
        "User-Agent",
        tungstenite::http::HeaderValue::from_static(concat!("silicon/", env!("CARGO_PKG_VERSION"))),
    );
    let (mut socket, _) = tungstenite::client::connect_with_config(request, None, 0)
        .map_err(|_| anyhow!("realtime websocket connection failed"))?;
    socket_timeout(&mut socket)?;
    socket.send(Message::Text(json!({"ticket":ticket}).to_string().into()))?;
    Ok(socket)
}
pub fn watch(sid: &str, org: Option<&str>) -> Result<()> {
    let (local, sid_org) = sid
        .split_once(':')
        .filter(|(local, org)| !local.is_empty() && !org.is_empty())
        .context("Silicon id must be local:org")?;
    if local.chars().any(char::is_whitespace)
        || sid_org.chars().any(char::is_whitespace)
        || org.is_some_and(|org| org != sid_org)
    {
        bail!("subscription organization must match the Silicon id");
    }
    let mut socket = connect(sid, sid_org)?;
    let mut ping = Instant::now();
    loop {
        if ping.elapsed() >= Duration::from_secs(15) {
            if socket.send(Message::Ping(Vec::new().into())).is_err() {
                let _ = socket.close(None);
            }
            ping = Instant::now();
        }
        match socket.read() {
            Ok(Message::Text(text)) => {
                let value: Value =
                    serde_json::from_str(&text).context("invalid realtime websocket event")?;
                let mut stdout = std::io::stdout().lock();
                writeln!(stdout, "{value}")?;
                stdout.flush()?;
                continue;
            }
            Ok(Message::Close(_))
            | Err(tungstenite::Error::ConnectionClosed | tungstenite::Error::AlreadyClosed) => {}
            Ok(_) => continue,
            Err(tungstenite::Error::Io(e))
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                continue
            }
            Err(_) => {}
        }
        // Each connection consumes a fresh, single-use subscription ticket.
        loop {
            thread::sleep(Duration::from_secs(2));
            match connect(sid, sid_org) {
                Ok(next) => {
                    socket = next;
                    ping = Instant::now();
                    break;
                }
                Err(error) if unauthorized(&error) => return Err(error),
                Err(error) if error.to_string().contains("login to Silicon Realtime") => {
                    return Err(error)
                }
                Err(_) => eprintln!("realtime subscription interrupted; reconnecting"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn login_refresh_status_and_logout_preserve_retry_keys_and_keep_tokens_private() {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let base = format!("http://{}", server.server_addr());
        let worker = thread::spawn(move || {
            let mut refresh_key = Value::Null;
            let mut logout_key = Value::Null;
            for step in 0..7 {
                let mut request = server
                    .recv_timeout(Duration::from_secs(10))
                    .unwrap()
                    .unwrap();
                let mut bytes = Vec::new();
                request.as_reader().read_to_end(&mut bytes).unwrap();
                let body: Value = serde_json::from_slice(&bytes).unwrap();
                let path = request.url();
                let (status, value) = match step {
                    0 => {
                        assert_eq!(path, "/v1/auth/token");
                        assert_eq!(body["slt"], "single-use-secret");
                        (
                            200,
                            json!({"access_token":"access-one","refresh_token":"refresh-one","expires_in":1800}),
                        )
                    }
                    1 | 4 => {
                        assert_eq!(path, "/v1/auth/status");
                        let token = if step == 1 {
                            "Bearer access-one"
                        } else {
                            "Bearer access-two"
                        };
                        assert!(request
                            .headers()
                            .iter()
                            .any(|h| h.field.equiv("Authorization") && h.value.as_str() == token));
                        (
                            200,
                            json!({"authenticated":true,"actor_type":"carbon","organizations":["example"]}),
                        )
                    }
                    2 | 3 => {
                        assert_eq!(path, "/v1/auth/token");
                        assert_eq!(body["refresh_token"], "refresh-one");
                        if step == 2 {
                            refresh_key = body["idempotency_key"].clone();
                            (503, json!({"error":"transient failure"}))
                        } else {
                            assert_eq!(body["idempotency_key"], refresh_key);
                            (
                                200,
                                json!({"access_token":"access-two","refresh_token":"refresh-two","expires_in":1800}),
                            )
                        }
                    }
                    5 | 6 => {
                        assert_eq!(path, "/v1/auth/logout");
                        assert_eq!(body["refresh_token"], "refresh-two");
                        if step == 5 {
                            logout_key = body["idempotency_key"].clone();
                            (503, json!({"error":"transient failure"}))
                        } else {
                            assert_eq!(body["idempotency_key"], logout_key);
                            (200, json!({"authenticated":false,"revoked":true}))
                        }
                    }
                    _ => unreachable!(),
                };
                request
                    .respond(
                        tiny_http::Response::from_string(value.to_string())
                            .with_status_code(status),
                    )
                    .unwrap();
            }
        });
        let home = tempfile::tempdir().unwrap();
        let mut session = Session::at(home.path(), base, Value::Null).unwrap();
        let status = session.login("single-use-secret").unwrap();
        assert_eq!(status["authenticated"], true);
        assert!(!status.to_string().contains("secret"));
        assert!(!status.to_string().contains("access-one"));
        session.saved["valid_until"] = json!(0);
        session.save().unwrap();
        assert!(session.status().is_err());
        assert!(session.file.exists());
        assert_eq!(session.status().unwrap()["authenticated"], true);
        assert_eq!(session.saved["refresh_token"], "refresh-two");
        assert!(session.logout().is_err());
        assert!(session.file.exists());
        assert_eq!(session.logout().unwrap()["revoked"], true);
        assert!(!session.file.exists());
        worker.join().unwrap();
    }
    #[test]
    fn validates_service_origin_before_sending_credentials() {
        for value in [
            "https://realtime.example/",
            "http://127.0.0.1:9000",
            "http://localhost:9000",
            "http://[::1]:9000",
        ] {
            assert!(validate_base(value).is_ok(), "{value}");
        }
        for value in [
            "http://example.com",
            "http://localhost:80@evil.example",
            "https://user@evil.example",
            "https://example.com/path",
            "https://example.com?token=x",
            "https://example.com/#secret",
        ] {
            assert!(validate_base(value).is_err(), "{value}");
        }
    }
    #[test]
    fn credential_storage_is_private_and_bound_to_backend_and_test_plane() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let home = tempfile::tempdir().unwrap();
        let mut session =
            Session::at(home.path(), "https://example.com".into(), Value::Null).unwrap();
        session.save_tokens(json!({"access_token":"private-access","refresh_token":"private-refresh","expires_in":1800})).unwrap();
        assert_eq!(session.access().unwrap().as_deref(), Some("private-access"));
        assert_eq!(
            fs::metadata(&session.file).unwrap().permissions().mode() & 0o777,
            0o600
        );
        session.base = "https://different.example".into();
        assert!(session.access().unwrap().is_none());
        session.base = "https://example.com".into();
        session.context = json!({"iam_test_key":"other-environment"});
        assert!(session.access().unwrap().is_none());
        drop(session);
        let other = tempfile::tempdir().unwrap();
        symlink(home.path().join(".silicon"), other.path().join(".silicon")).unwrap();
        assert!(Session::at(other.path(), "https://example.com".into(), Value::Null).is_err());
    }
}
