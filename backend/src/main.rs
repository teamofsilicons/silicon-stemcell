//! Read-only relay. IAM proves actor + selected organization; the relay never executes commands.
use axum::{
    body::Bytes,
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        DefaultBodyLimit, State,
    },
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::broadcast;
use uuid::Uuid;

type Error = (StatusCode, Json<Value>);
type Result<T> = std::result::Result<T, Error>;
fn error(status: StatusCode, message: &str) -> Error {
    (status, Json(json!({"error":message})))
}
fn denied() -> Error {
    error(
        StatusCode::UNAUTHORIZED,
        "IAM authorization is missing, expired, or outside the selected organization",
    )
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key].as_str().filter(|s| !s.is_empty()).ok_or_else(|| {
        error(
            StatusCode::BAD_REQUEST,
            &format!("{key} must be a nonempty string"),
        )
    })
}
fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

#[derive(Clone)]
struct Credentials {
    secret: String,
    test_key: Option<String>,
}
#[derive(Clone)]
struct Identity {
    org: String,
    plane: String,
    public_id: String,
    kind: String,
    expires: i64,
}
impl Identity {
    fn key(&self, sid: &str) -> String {
        format!("{}|{}|{}", self.plane, self.org, sid)
    }
}
struct Publisher {
    owner: Uuid,
    silicon: String,
    config: Value,
    seen: Instant,
    telemetry: bool,
}
#[derive(Clone)]
struct Event {
    key: String,
    value: Value,
}
struct Ticket {
    identity: Identity,
    silicon: String,
    deadline: Instant,
    token: Option<String>,
    credentials: Credentials,
}
struct App {
    http: reqwest::Client,
    iam_url: String,
    app_id: String,
    credentials: Credentials,
    publishers: Mutex<HashMap<String, Publisher>>,
    tickets: Mutex<HashMap<String, Ticket>>,
    events: broadcast::Sender<Event>,
    webhook_key: String,
    telemetry: Option<space_station::SpaceClient>,
}

impl App {
    fn credentials(&self, body: &Value) -> Result<Credentials> {
        if body.get("testing_context").is_none_or(Value::is_null) {
            return Ok(self.credentials.clone());
        }
        let test = &body["testing_context"];
        if test["app_id"].as_str().is_some_and(|id| id != self.app_id) {
            return Err(denied());
        }
        let key = text(test, "iam_test_key")?;
        if key.len() != 32 || !key.bytes().all(|c| c.is_ascii_alphanumeric()) {
            return Err(denied());
        }
        Ok(Credentials {
            secret: text(test, "app_secret")?.into(),
            test_key: Some(key.into()),
        })
    }
    fn iam(&self, path: &str, credentials: &Credentials) -> reqwest::RequestBuilder {
        let mut r = self
            .http
            .post(format!("{}/api/v1/{path}", self.iam_url))
            .basic_auth(&self.app_id, Some(&credentials.secret))
            .header("Silicon-IAM-Supported-API-Versions", "v1");
        if let Some(key) = &credentials.test_key {
            r = r.header("X-Testing-Environment-Key", key);
        }
        r
    }
    async fn response(r: reqwest::RequestBuilder) -> Result<Value> {
        let response = r.send().await.map_err(|_| {
            error(
                StatusCode::BAD_GATEWAY,
                "IAM is unavailable; retry with fresh credentials",
            )
        })?;
        if !response.status().is_success() {
            return Err(denied());
        }
        response
            .json()
            .await
            .map_err(|_| error(StatusCode::BAD_GATEWAY, "IAM returned invalid JSON"))
    }
    async fn identity(
        &self,
        token: &str,
        org: &str,
        credentials: &Credentials,
    ) -> Result<Identity> {
        let body = Self::response(
            self.iam("oauth/introspect", credentials)
                .header("X-Org-ID", org)
                .form(&[("token", token), ("token_type_hint", "access_token")]),
        )
        .await?;
        if body["active"] != true || body["audience"] != self.app_id {
            return Err(denied());
        }
        let a = &body["authorization"];
        let id = identity_from(
            a,
            body["expires_at"]
                .as_str()
                .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
                .map(|t| t.timestamp())
                .or_else(|| body["expires_at"].as_i64())
                .unwrap_or(0),
            credentials,
        )?;
        if id.org != org || a["audience"] != self.app_id || id.expires <= now() {
            return Err(denied());
        }
        Ok(id)
    }
    fn emit(&self, key: &str, silicon: &str, kind: &str, data: Value, telemetry: bool) {
        if let Some(client) = self.telemetry.as_ref().filter(|_| telemetry) {
            client.record(json!({"source":"silicon-realtime","step":"relay","silicon":silicon,"kind":kind,"timestamp":chrono::Utc::now().to_rfc3339()}));
        }
        let _ = self.events.send(Event { key:key.into(), value:json!({"type":kind,"silicon":silicon,"timestamp":chrono::Utc::now().to_rfc3339(),"data":data}) });
    }
    fn offline(&self, key: &str, owner: Uuid) {
        let mut rows = self.publishers.lock().unwrap();
        if rows.get(key).is_some_and(|row| row.owner == owner) {
            let row = rows.remove(key).unwrap();
            self.emit(
                key,
                &row.silicon,
                "presence",
                json!({"online":false}),
                row.telemetry,
            );
        }
    }
    fn snapshot(&self, identity: &Identity, silicon: &str) -> Value {
        let rows = self.publishers.lock().unwrap();
        let row = rows
            .get(&identity.key(silicon))
            .filter(|r| r.seen.elapsed() < Duration::from_secs(45));
        match row {
            Some(row) => json!({"silicon":silicon,"online":true,"configuration":row.config}),
            None => json!({"silicon":silicon,"online":false,"configuration":null}),
        }
    }
}
fn identity_from(a: &Value, expires: i64, credentials: &Credentials) -> Result<Identity> {
    let plane = a["testing_environment_id"]
        .as_str()
        .unwrap_or("")
        .to_owned();
    if credentials.test_key.is_some() == plane.is_empty() {
        return Err(denied());
    }
    Ok(Identity {
        org: text(a, "org_id")?.into(),
        public_id: text(a, "public_id")?.into(),
        kind: text(a, "actor_type")?.into(),
        plane,
        expires,
    })
}
fn silicon_in_org(silicon: &str, org: &str) -> bool {
    silicon.split_once(':').is_some_and(|(id, o)| {
        !id.is_empty() && o == org && !id.contains('|') && !id.chars().any(char::is_whitespace)
    })
}

async fn exchange(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Result<Json<Value>> {
    let c = app.credentials(&body)?;
    let mut fields = vec![("app_id", app.app_id.as_str())];
    let path = if body["refresh_token"].is_string() {
        fields.push(("refresh_token", text(&body, "refresh_token")?));
        "app-auth/tokens"
    } else {
        fields.push(("slt", text(&body, "slt")?));
        "app-auth/tokens"
    };
    let tokens = App::response(
        app.iam(path, &c)
            .header("Idempotency-Key", text(&body, "idempotency_key")?)
            .form(&fields),
    )
    .await?;
    Ok(Json(tokens))
}

async fn auth_status(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Result<Json<Value>> {
    let credentials = app.credentials(&body)?;
    let token = headers
        .get("Authorization")
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "))
        .ok_or_else(denied)?;
    let mut request = app
        .iam("oauth/introspect", &credentials)
        .form(&[("token", token), ("token_type_hint", "access_token")]);
    if let Some(org) = body["org_id"].as_str() {
        request = request.header("X-Org-ID", org);
    }
    let value = App::response(request).await?;
    if value["active"] != true || value["audience"] != app.app_id {
        return Ok(Json(json!({"authenticated":false})));
    }
    let snapshots = if let Some(items) = value["authorizations"].as_array() {
        items.clone()
    } else {
        vec![value["authorization"].clone()]
    };
    if snapshots
        .iter()
        .any(|a| a["testing_environment_id"].as_str().is_some() != credentials.test_key.is_some())
    {
        return Err(denied());
    }
    Ok(Json(
        json!({"authenticated":true,"app_id":app.app_id,"actor_type":value["actor_type"],"principal_id":value["principal_id"],"organizations":snapshots.iter().filter_map(|a|a["org_id"].as_str()).collect::<Vec<_>>(),"expires_at":value["expires_at"]}),
    ))
}
async fn logout(State(app): State<Arc<App>>, Json(body): Json<Value>) -> Result<Json<Value>> {
    let credentials = app.credentials(&body)?;
    let response = app
        .iam("oauth/revoke", &credentials)
        .header("Idempotency-Key", text(&body, "idempotency_key")?)
        .form(&[
            ("token", text(&body, "refresh_token")?),
            ("token_type_hint", "refresh_token"),
        ])
        .send()
        .await
        .map_err(|_| {
            error(
                StatusCode::BAD_GATEWAY,
                "IAM revocation unavailable; credentials retained for retry",
            )
        })?;
    if !response.status().is_success() {
        return Err(denied());
    }
    Ok(Json(json!({"authenticated":false,"revoked":true})))
}

async fn authorize(
    app: &App,
    headers: &HeaderMap,
    body: &Value,
    raw: &[u8],
    path: &str,
) -> Result<(Identity, Credentials, Option<String>)> {
    let c = app.credentials(body)?;
    let org = text(body, "org_id")?;
    let sid = text(body, "silicon")?;
    if !silicon_in_org(sid, org) {
        return Err(denied());
    }
    if path.contains("/obo/") {
        let proof = headers
            .get("X-OBO-Proof")
            .and_then(|h| h.to_str().ok())
            .ok_or_else(denied)?;
        // No retries: verification consumes this exact single-use proof.
        let verified=App::response(app.iam("obo-access/verify",&c).json(&json!({"access_proof":proof,"request":{"method":"POST","path":path,"body_sha256":format!("{:x}",Sha256::digest(raw))}}))).await?;
        let expires = verified["expires_at"]
            .as_str()
            .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
            .map(|time| time.timestamp())
            .ok_or_else(denied)?;
        let endpoint = if path.ends_with("/config") {
            "silicon.configuration.read"
        } else {
            "silicon.events.subscribe"
        };
        if verified["valid"] != true
            || verified["audience"] != app.app_id
            || verified["endpoint"]["path"] != path
            || verified["endpoint"]["endpoint_id"] != endpoint
            || verified["authorization"]["audience"] != app.app_id
            || expires <= now()
        {
            return Err(denied());
        }
        let mut a = verified["authorization"].clone();
        if a["public_id"].is_null() {
            a["public_id"] = verified["actor"]["public_id"].clone();
        }
        if a["actor_type"].is_null() {
            a["actor_type"] = verified["actor"]["actor_type"].clone();
        }
        let id = identity_from(&a, expires.min(now() + 60), &c)?;
        if id.org != org || verified["org_id"] != org {
            return Err(denied());
        }
        Ok((id, c, None))
    } else {
        let token = headers
            .get("Authorization")
            .and_then(|h| h.to_str().ok())
            .and_then(|s| s.strip_prefix("Bearer "))
            .ok_or_else(denied)?;
        Ok((app.identity(token, org, &c).await?, c, Some(token.into())))
    }
}
async fn read_request(
    app: Arc<App>,
    headers: HeaderMap,
    raw: Bytes,
    path: &str,
    subscription: bool,
) -> Result<Json<Value>> {
    let body: Value =
        serde_json::from_slice(&raw).map_err(|_| error(StatusCode::BAD_REQUEST, "invalid JSON"))?;
    let (id, c, token) = authorize(&app, &headers, &body, &raw, path).await?;
    let silicon = text(&body, "silicon")?.to_owned();
    if !subscription {
        return Ok(Json(app.snapshot(&id, &silicon)));
    }
    let ticket = Uuid::new_v4().to_string();
    let mut tickets = app.tickets.lock().unwrap();
    tickets.retain(|_, t| t.deadline > Instant::now());
    if tickets.len() >= 4096 {
        return Err(error(
            StatusCode::SERVICE_UNAVAILABLE,
            "too many pending subscriptions",
        ));
    }
    tickets.insert(
        ticket.clone(),
        Ticket {
            identity: id,
            silicon,
            deadline: Instant::now() + Duration::from_secs(30),
            token,
            credentials: c,
        },
    );
    Ok(Json(
        json!({"ticket":ticket,"websocket_path":"/v1/subscribe","ticket_expires_in":30,"subscription_expires_in":if path.contains("/obo/"){60}else{1800}}),
    ))
}
async fn config(State(a): State<Arc<App>>, h: HeaderMap, b: Bytes) -> Result<Json<Value>> {
    read_request(a, h, b, "/v1/config", false).await
}
async fn obo_config(State(a): State<Arc<App>>, h: HeaderMap, b: Bytes) -> Result<Json<Value>> {
    read_request(a, h, b, "/v1/obo/config", false).await
}
async fn subscribe(State(a): State<Arc<App>>, h: HeaderMap, b: Bytes) -> Result<Json<Value>> {
    read_request(a, h, b, "/v1/subscribe", true).await
}
async fn obo_subscribe(State(a): State<Arc<App>>, h: HeaderMap, b: Bytes) -> Result<Json<Value>> {
    read_request(a, h, b, "/v1/obo/subscribe", true).await
}
async fn send(socket: &mut WebSocket, v: Value) -> bool {
    socket
        .send(Message::Text(v.to_string().into()))
        .await
        .is_ok()
}
async fn read(socket: &mut WebSocket) -> Option<Value> {
    match tokio::time::timeout(Duration::from_secs(15), socket.recv())
        .await
        .ok()??
        .ok()?
    {
        Message::Text(t) => serde_json::from_str(&t).ok(),
        _ => None,
    }
}
async fn subscriber(State(app): State<Arc<App>>, ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.max_message_size(16384).on_upgrade(move|mut socket| async move {
        let Some(body)=read(&mut socket).await else{return};
        let ticket=body["ticket"].as_str().and_then(|t|app.tickets.lock().unwrap().remove(t));
        let Some(mut ticket)=ticket.filter(|t|t.deadline>Instant::now()) else {let _=send(&mut socket,json!({"error":"invalid or consumed subscription ticket"})).await;return};
        if ticket.identity.expires <= now() { return }
        if let Some(token) = &ticket.token {
            match app.identity(token, &ticket.identity.org, &ticket.credentials).await {
                Ok(identity) => ticket.identity = identity,
                Err(_) => return,
            }
        }
        let key=ticket.identity.key(&ticket.silicon);
        let mut events=app.events.subscribe();
        if !send(&mut socket,json!({"type":"snapshot","data":app.snapshot(&ticket.identity,&ticket.silicon)})).await{return}
        let mut check=tokio::time::interval(Duration::from_secs(15));
        loop {
            tokio::select! {
                _=check.tick()=> {
                    if ticket.identity.expires<=now(){break}
                    if let Some(token)=&ticket.token {
                        match app.identity(token,&ticket.identity.org,&ticket.credentials).await {Ok(id)=>ticket.identity=id,Err(_)=>break}
                    }
                    if socket.send(Message::Ping(Vec::new().into())).await.is_err(){break}
                }
                message=socket.recv()=>{match message{Some(Ok(Message::Ping(p)))=>{if socket.send(Message::Pong(p)).await.is_err(){break}},Some(Ok(Message::Pong(_)))=>{},_=>break}}
                event=events.recv()=>{match event{Ok(e) if e.key==key=>{if !send(&mut socket,e.value).await{break}},Err(broadcast::error::RecvError::Lagged(_))=>{if !send(&mut socket,json!({"type":"gap","data":{"reason":"subscriber fell behind; fetch current snapshot"}})).await{break}},Err(_)=>break,_=>{}}}
            }
        }
        let _=socket.send(Message::Close(None)).await;
    })
}

#[derive(Clone)]
struct Registration {
    identity: Identity,
    token: String,
    credentials: Credentials,
    silicon: String,
}
async fn publisher(State(app): State<Arc<App>>, ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.max_message_size(1024*1024).on_upgrade(move|mut socket|async move{
        let owner=Uuid::new_v4();
        let mut registrations:HashMap<String,Registration>=HashMap::new();
        let mut heartbeat=tokio::time::interval(Duration::from_secs(15));
        let mut received=Instant::now();
        loop {
            tokio::select! {
                _=heartbeat.tick()=>{
                    if received.elapsed()>Duration::from_secs(45){break}
                    let mut invalid=Vec::new();
                    for (key,r) in &registrations {
                        if app.identity(&r.token,&r.identity.org,&r.credentials).await.is_err(){invalid.push(key.clone())}
                    }
                    for key in invalid {registrations.remove(&key);app.offline(&key,owner);}
                    if socket.send(Message::Ping(Vec::new().into())).await.is_err(){break}
                }
                message=socket.recv()=>{
                    let Some(Ok(message))=message else{break};
                    received=Instant::now();
                    let body:Value=match message {Message::Text(t)=>match serde_json::from_str(&t){Ok(b)=>b,Err(_)=>break},Message::Ping(p)=>{if socket.send(Message::Pong(p)).await.is_err(){break}continue},Message::Pong(_)=>{continue},_=>break};
                    let result=publisher_message(&app,owner,&mut registrations,body).await;
                    match result {Ok(reply)=>{if !send(&mut socket,reply).await{break}},Err((_,body))=>{if !send(&mut socket,body.0).await{break}}}
                }
            }
        }
        for key in registrations.keys(){app.offline(key,owner)}
    })
}
async fn publisher_message(
    app: &App,
    owner: Uuid,
    registrations: &mut HashMap<String, Registration>,
    body: Value,
) -> Result<Value> {
    let kind = text(&body, "type")?;
    let telemetry = body["telemetry"].as_bool().unwrap_or(true);
    if kind == "ping" {
        let mut rows = app.publishers.lock().unwrap();
        for (key, r) in registrations.iter() {
            if let Some(row) = rows.get_mut(key).filter(|r| r.owner == owner) {
                row.seen = Instant::now();
                row.telemetry = telemetry;
                app.emit(
                    key,
                    &r.silicon,
                    "presence",
                    json!({"online":true}),
                    telemetry,
                );
            }
        }
        return Ok(json!({"type":"pong","timestamp":now()}));
    }
    let sid = text(&body, "silicon")?;
    if kind == "register" {
        if registrations.len() >= 256 && !registrations.values().any(|r| r.silicon == sid) {
            return Err(error(
                StatusCode::BAD_REQUEST,
                "one interpreter supports at most 256 Silicon registrations",
            ));
        }
        let org = text(&body, "org_id")?;
        let credentials = app.credentials(&body)?;
        let token = text(&body, "access_token")?;
        let identity = app.identity(token, org, &credentials).await?;
        if identity.kind != "silicon" || identity.public_id != sid || !silicon_in_org(sid, org) {
            return Err(denied());
        }
        let key = identity.key(sid);
        let mut config = body["configuration"].clone();
        redact(&mut config);
        app.publishers.lock().unwrap().insert(
            key.clone(),
            Publisher {
                owner,
                silicon: sid.into(),
                config,
                seen: Instant::now(),
                telemetry,
            },
        );
        registrations.insert(
            key.clone(),
            Registration {
                identity,
                token: token.into(),
                credentials,
                silicon: sid.into(),
            },
        );
        app.emit(&key, sid, "presence", json!({"online":true}), telemetry);
        return Ok(json!({"type":"registered","silicon":sid}));
    }
    let key = registrations
        .iter()
        .find(|(_, r)| r.silicon == sid)
        .map(|(key, _)| key.clone())
        .ok_or_else(denied)?;
    if !app
        .publishers
        .lock()
        .unwrap()
        .get(&key)
        .is_some_and(|r| r.owner == owner)
    {
        return Err(denied());
    }
    match kind {
        "unregister" => {
            registrations.remove(&key);
            app.offline(&key, owner);
        }
        "event" => {
            let mut data = body["data"].clone();
            redact(&mut data);
            app.emit(&key, sid, "event", data, telemetry)
        }
        _ => {
            return Err(error(
                StatusCode::BAD_REQUEST,
                "read-only relay accepts register, unregister, event and ping only",
            ))
        }
    }
    Ok(json!({"type":"ack","silicon":sid}))
}
fn redact(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, value) in map {
                let key = key.to_ascii_lowercase();
                if [
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "table_key",
                    "api_key",
                    "authorization",
                    "iam_test_key",
                ]
                .iter()
                .any(|s| key.contains(s))
                {
                    *value = json!("[REDACTED]")
                } else {
                    redact(value)
                }
            }
        }
        Value::Array(a) => a.iter_mut().for_each(redact),
        _ => {}
    }
}

async fn webhook(
    State(app): State<Arc<App>>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>> {
    use hmac::{Hmac, Mac};
    let timestamp = headers
        .get("x-silicon-iam-timestamp")
        .and_then(|h| h.to_str().ok())
        .ok_or_else(denied)?;
    let stamp = timestamp.parse::<i64>().map_err(|_| denied())?;
    if now().abs_diff(stamp) > 300
        || headers
            .get("x-silicon-iam-key-version")
            .and_then(|h| h.to_str().ok())
            != Some("1")
    {
        return Err(denied());
    }
    let sig = headers
        .get("x-silicon-iam-signature")
        .and_then(|h| h.to_str().ok())
        .and_then(|s| s.strip_prefix("v1="))
        .ok_or_else(denied)?;
    let bytes = (0..sig.len())
        .step_by(2)
        .map(|i| {
            sig.get(i..i + 2)
                .and_then(|s| u8::from_str_radix(s, 16).ok())
        })
        .collect::<Option<Vec<_>>>()
        .ok_or_else(denied)?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(app.webhook_key.as_bytes()).map_err(|_| denied())?;
    mac.update(timestamp.as_bytes());
    mac.update(b".");
    mac.update(&body);
    mac.verify_slice(&bytes).map_err(|_| denied())?;
    // Webhooks confer no authority. Every connection is revalidated against IAM every 15 seconds.
    Ok(Json(json!({"accepted":true})))
}
fn router(app: Arc<App>) -> Router {
    Router::new()
        .route(
            "/health",
            get(|| async { Json(json!({"status":"ok","protocol":"v1","read_only":true})) }),
        )
        .route("/v1/auth/token", post(exchange))
        .route("/v1/auth/status", post(auth_status))
        .route("/v1/auth/logout", post(logout))
        .route("/v1/config", post(config))
        .route("/v1/obo/config", post(obo_config))
        .route("/v1/subscribe", post(subscribe).get(subscriber))
        .route("/v1/obo/subscribe", post(obo_subscribe))
        .route("/v1/publish", get(publisher))
        .route("/webhooks/iam", post(webhook))
        .layer(DefaultBodyLimit::max(1024 * 1024))
        .with_state(app)
}
#[tokio::main]
async fn main() {
    let (events, _) = broadcast::channel(1024);
    let app = Arc::new(App {
        http: reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .unwrap(),
        iam_url: std::env::var("SILICON_IAM_URL")
            .unwrap_or_else(|_| "https://backend.iam.teamofsilicons.com".into())
            .trim_end_matches('/')
            .into(),
        app_id: "tos>silicon-realtime".into(),
        credentials: Credentials {
            secret: std::env::var("SILICON_REALTIME_APP_SECRET")
                .expect("SILICON_REALTIME_APP_SECRET required"),
            test_key: None,
        },
        publishers: Mutex::new(HashMap::new()),
        tickets: Mutex::new(HashMap::new()),
        events,
        telemetry: std::env::var("SILICON_BACKEND_TABLE_KEY")
            .ok()
            .and_then(|key| space_station::SpaceClient::new(&key).ok()),
        webhook_key: std::env::var("SILICON_REALTIME_WEBHOOK_SECRET")
            .expect("SILICON_REALTIME_WEBHOOK_SECRET required"),
    });
    let address =
        std::env::var("SILICON_REALTIME_BIND").unwrap_or_else(|_| "127.0.0.1:1830".into());
    let listener = tokio::net::TcpListener::bind(&address).await.unwrap();
    eprintln!("silicon realtime v1 listening on {address}");
    axum::serve(listener, router(app))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .unwrap();
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::{SinkExt, StreamExt};
    use std::sync::atomic::{AtomicBool, Ordering};
    use tokio_tungstenite::{connect_async, tungstenite::Message as WsMessage};

    #[tokio::test]
    async fn relay_enforces_identity_org_read_only_redaction_and_single_use_tickets() {
        let consumed = Arc::new(AtomicBool::new(false));
        let c = consumed.clone();
        let iam=Router::new().route("/api/v1/oauth/introspect",post(|headers:HeaderMap,axum::extract::Form(body):axum::extract::Form<HashMap<String,String>>|async move{
            let token=body.get("token").map(String::as_str).unwrap_or("");
            if !["publisher","reader"].contains(&token)||headers.get("x-org-id").unwrap()!="org" {return Json(json!({"active":false}))}
            Json(json!({"active":true,"audience":"tos>silicon-realtime","expires_at":now()+1800,"authorization":{"audience":"tos>silicon-realtime","org_id":"org","public_id":if token=="publisher"{"bot:org"}else{"reader"},"actor_type":if token=="publisher"{"silicon"}else{"carbon"},"testing_environment_id":null}}))
        })).route("/api/v1/obo-access/verify",post(move|Json(body):Json<Value>|{let consumed=c.clone();async move{
            if body["access_proof"]!="one-use"||consumed.swap(true,Ordering::SeqCst){return Err(denied())}
            assert_eq!(body["request"]["path"],"/v1/obo/config");
            Ok(Json(json!({"valid":true,"audience":"tos>silicon-realtime","endpoint":{"path":"/v1/obo/config","endpoint_id":"silicon.configuration.read"},"expires_at":(chrono::Utc::now()+chrono::Duration::seconds(60)).to_rfc3339(),"org_id":"org","authorization":{"audience":"tos>silicon-realtime","org_id":"org","public_id":"reader","actor_type":"carbon","testing_environment_id":null}})))
        }}));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let iam_url = format!("http://{}", listener.local_addr().unwrap());
        let iam_task = tokio::spawn(async move { axum::serve(listener, iam).await.unwrap() });
        let (events, _) = broadcast::channel(16);
        let app = Arc::new(App {
            http: reqwest::Client::new(),
            iam_url,
            app_id: "tos>silicon-realtime".into(),
            credentials: Credentials {
                secret: "test-secret".into(),
                test_key: None,
            },
            publishers: Mutex::new(HashMap::new()),
            tickets: Mutex::new(HashMap::new()),
            events,
            webhook_key: "test-webhook".into(),
            telemetry: None,
        });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move { axum::serve(listener, router(app)).await.unwrap() });
        let http = reqwest::Client::new();
        let base = format!("http://{address}");
        let request = json!({"org_id":"org","silicon":"bot:org"});
        assert_eq!(
            http.post(format!("{base}/v1/config"))
                .json(&request)
                .send()
                .await
                .unwrap()
                .status(),
            401
        );
        let (mut publisher, _) = connect_async(format!("ws://{address}/v1/publish"))
            .await
            .unwrap();
        async fn receive(
            socket: &mut tokio_tungstenite::WebSocketStream<
                tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
            >,
        ) -> Value {
            loop {
                match tokio::time::timeout(Duration::from_secs(5), socket.next())
                    .await
                    .unwrap()
                    .unwrap()
                    .unwrap()
                {
                    WsMessage::Text(t) => return serde_json::from_str(&t).unwrap(),
                    WsMessage::Ping(p) => socket.send(WsMessage::Pong(p)).await.unwrap(),
                    _ => {}
                }
            }
        }
        publisher.send(WsMessage::Text(json!({"type":"register","org_id":"org","silicon":"wrong:org","access_token":"publisher","configuration":{}}).to_string().into())).await.unwrap();
        assert!(receive(&mut publisher).await["error"].is_string());
        publisher.send(WsMessage::Text(json!({"type":"register","org_id":"org","silicon":"bot:org","access_token":"publisher","configuration":{"silicon":{"token":"secret"},"isi":{"a":{"model":"fast"}}}}).to_string().into())).await.unwrap();
        assert_eq!(receive(&mut publisher).await["type"], "registered");
        let response: Value = http
            .post(format!("{base}/v1/config"))
            .bearer_auth("reader")
            .json(&request)
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(response["online"], true);
        assert_eq!(response["configuration"]["silicon"]["token"], "[REDACTED]");
        assert_eq!(
            http.post(format!("{base}/v1/config"))
                .bearer_auth("reader")
                .json(&json!({"org_id":"other","silicon":"bot:other"}))
                .send()
                .await
                .unwrap()
                .status(),
            401
        );
        let ticket: Value = http
            .post(format!("{base}/v1/subscribe"))
            .bearer_auth("reader")
            .json(&request)
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        let (mut subscriber, _) = connect_async(format!("ws://{address}/v1/subscribe"))
            .await
            .unwrap();
        subscriber
            .send(WsMessage::Text(ticket.to_string().into()))
            .await
            .unwrap();
        assert_eq!(receive(&mut subscriber).await["type"], "snapshot");
        let (mut replay, _) = connect_async(format!("ws://{address}/v1/subscribe"))
            .await
            .unwrap();
        replay
            .send(WsMessage::Text(ticket.to_string().into()))
            .await
            .unwrap();
        assert!(receive(&mut replay).await["error"].is_string());
        publisher
            .send(WsMessage::Text(
                json!({"type":"event","silicon":"bot:org","data":{"isi":"a","text":"hello"}})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        assert_eq!(receive(&mut publisher).await["type"], "ack");
        assert_eq!(receive(&mut subscriber).await["data"]["text"], "hello");
        publisher
            .send(WsMessage::Text(
                json!({"type":"execute","silicon":"bot:org","command":"anything"})
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
        assert!(receive(&mut publisher).await["error"].is_string());
        assert_eq!(
            http.post(format!("{base}/v1/obo/config"))
                .header("X-OBO-Proof", "one-use")
                .json(&request)
                .send()
                .await
                .unwrap()
                .status(),
            200
        );
        assert_eq!(
            http.post(format!("{base}/v1/obo/config"))
                .header("X-OBO-Proof", "one-use")
                .json(&request)
                .send()
                .await
                .unwrap()
                .status(),
            401
        );
        publisher.close(None).await.unwrap();
        let offline = receive(&mut subscriber).await;
        assert_eq!(offline["type"], "presence");
        assert_eq!(offline["data"]["online"], false);
        server.abort();
        iam_task.abort();
    }
}
