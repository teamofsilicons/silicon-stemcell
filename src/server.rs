use crate::{
    auth,
    config::Config,
    failure, flow,
    runtime::{NewSession, Runtime, SendOptions},
    state,
};
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread;
use std::time::{Duration, Instant};
use tiny_http::{Header, Method, Request, Response, Server};
use uuid::Uuid;

#[derive(Clone, Deserialize, Serialize)]
pub struct Daemon {
    pub pid: u32,
    pub port: u16,
    pub token: String,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct Connection {
    pub id: String,
    pub yaml: PathBuf,
    pub home: PathBuf,
    pub host: String,
}

pub fn directory() -> PathBuf {
    std::env::var_os("SILICON_INTERPRETER_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".silicon-interpreter")
        })
}

pub fn saved() -> Result<Vec<Connection>> {
    let path = directory().join("connections.json");
    match fs::read(&path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .with_context(|| format!("invalid connection registry {}", path.display())),
        // Nothing connected yet; any other read failure is reported, not taken for "empty".
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(error) => Err(error).with_context(|| format!("read {}", path.display())),
    }
}

pub fn descriptor(cfg: &Config) -> Connection {
    let id = cfg.silicon.id.clone().unwrap();
    Connection {
        host: identity_host(
            id.strip_prefix("si:").unwrap(),
            cfg.silicon.org_id.as_deref().unwrap(),
        ),
        id,
        yaml: cfg.path.clone(),
        home: cfg.home.clone(),
    }
}

// Preserve readable existing hosts where possible. Encode both components for
// IAM handles which are not DNS labels; no lossy underscore/hyphen substitution.
fn identity_host(handle: &str, org: &str) -> String {
    if crate::config::host_label(handle) && crate::config::host_label(org) {
        return format!("{handle}.{org}.localhost");
    }
    let encode = |value: &str| {
        let hex: String = value.bytes().map(|b| format!("{b:02x}")).collect();
        hex.as_bytes()
            .chunks(60)
            .map(|chunk| std::str::from_utf8(chunk).unwrap())
            .collect::<Vec<_>>()
            .join(".")
    };
    format!("{}.si.{}.org.localhost", encode(handle), encode(org))
}

#[test]
fn identifier_hosts_preserve_distinct_iam_handles_and_dns_limits() {
    assert_eq!(identity_host("worker", "tos"), "worker.tos.localhost");
    assert_ne!(
        identity_host("worker_name", "tos"),
        identity_host("worker--name", "tos")
    );
    let maximum = identity_host(&"_".repeat(50), &"_".repeat(50));
    assert!(maximum.len() <= 253);
    assert!(maximum.split('.').all(crate::config::host_label));
}

pub fn compile(path: impl AsRef<Path>) -> Result<Config> {
    let cfg = Config::load(path)?;
    flow::validate(&cfg.flow).context("invalid flow")?;
    for (name, isi) in &cfg.isi {
        if let Some(dna) = &isi.dna {
            crate::eval::validate(dna).with_context(|| format!("isi.{name}.dna"))?;
        }
        if let Some(heartbeat) = &isi.heartbeat {
            crate::eval::validate(heartbeat).with_context(|| format!("isi.{name}.heartbeat"))?;
        }
        if let Some(suggestion) = &isi.new_session_suggestion {
            crate::eval::validate(suggestion)
                .with_context(|| format!("isi.{name}.new_session_suggestion"))?;
        }
    }
    Ok(cfg)
}

pub fn daemon(start: bool) -> Result<Daemon> {
    let dir = directory();
    state::private_dir(&dir)?;
    // Probing is normal; the reason it failed still explains a stuck or missing interpreter.
    let last = match running(&dir) {
        Ok(daemon) => return Ok(daemon),
        Err(error) => error,
    };
    if !start {
        return Err(last.context("interpreter is not running; run silicon connect YAML"));
    }
    let _startup = lock(&dir.join("startup.lock"), false)?;
    if let Ok(daemon) = running(&dir) {
        return Ok(daemon);
    }
    let log = dir.join("daemon.log");
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(&log)
        .with_context(|| format!("open {}", log.display()))?;
    let start = file
        .metadata()
        .with_context(|| format!("read the size of {}", log.display()))?
        .len();
    let executable = std::env::current_exe()
        .context("locate the silicon executable to start the interpreter")?;
    let mut child = Command::new(&executable)
        .arg("serve")
        .stdin(Stdio::null())
        .stdout(
            file.try_clone()
                .with_context(|| format!("share {} with the interpreter", log.display()))?,
        )
        .stderr(file)
        .spawn()
        .map_err(|error| failure::spawn(&dir, &failure::argv(&executable, &["serve"]), &error))
        .with_context(|| format!("start the interpreter from {}", executable.display()))?;
    await_start(&dir, &mut child, start, Duration::from_secs(30))
}

/// The interpreter daemon.json describes, once it answers a ping.
fn running(dir: &Path) -> Result<Daemon> {
    let path = dir.join("daemon.json");
    let bytes = fs::read(&path).with_context(|| format!("read {}", path.display()))?;
    let daemon: Daemon = serde_json::from_slice(&bytes)
        .with_context(|| format!("invalid daemon metadata in {}", path.display()))?;
    call(&daemon, "ping", json!({}))?;
    Ok(daemon)
}

/// Wait for a just-started interpreter. If it dies or stalls, the error carries everything
/// it wrote to daemon.log since `start` and why the last ping failed.
fn await_start(
    dir: &Path,
    child: &mut std::process::Child,
    start: u64,
    patience: Duration,
) -> Result<Daemon> {
    let deadline = Instant::now() + patience;
    loop {
        let last = match running(dir) {
            Ok(daemon) => return Ok(daemon),
            Err(error) => error,
        };
        if let Some(status) = child
            .try_wait()
            .context("check on the starting interpreter")?
        {
            bail!(
                "interpreter exited before answering: {status}\n{}\nlast ping: {last:#}",
                startup_output(dir, start)
            );
        }
        if Instant::now() >= deadline {
            bail!(
                "interpreter (pid {}) did not answer within {}s\n{}\nlast ping: {last:#}",
                child.id(),
                patience.as_secs(),
                startup_output(dir, start)
            );
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// What a starting interpreter wrote (stdout and stderr) to daemon.log, verbatim.
fn startup_output(dir: &Path, start: u64) -> String {
    let path = dir.join("daemon.log");
    match since(&path, start) {
        Ok(text) if text.trim().is_empty() => format!("{}: (empty)", path.display()),
        Ok(text) => failure::mask(
            dir,
            &format!("{}:\n{}", path.display(), text.trim_end()),
            &[],
        ),
        Err(error) => format!("could not read {}: {error:#}", path.display()),
    }
}

/// Everything appended to `path` after byte `start`; a missing file has nothing yet.
fn since(path: &Path, start: u64) -> Result<String> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(String::new()),
        Err(error) => return Err(error).with_context(|| format!("open {}", path.display())),
    };
    file.seek(SeekFrom::Start(start))
        .with_context(|| format!("seek {} to byte {start}", path.display()))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .with_context(|| format!("read {}", path.display()))?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn lock(path: &Path, nonblocking: bool) -> Result<File> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("open {}", path.display()))?;
    let flags = libc::LOCK_EX | if nonblocking { libc::LOCK_NB } else { 0 };
    if unsafe { libc::flock(file.as_raw_fd(), flags) } != 0 {
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::WouldBlock {
            return Err(error)
                .with_context(|| format!("another interpreter already holds {}", path.display()));
        }
        return Err(error).with_context(|| format!("could not lock {}", path.display()));
    }
    Ok(file)
}

pub fn call(daemon: &Daemon, action: &str, args: Value) -> Result<Value> {
    request(
        &format!("http://127.0.0.1:{}/control", daemon.port),
        &daemon.token,
        action,
        args,
    )
}

pub fn internal(action: &str, args: Value) -> Result<Value> {
    let url = std::env::var("SI_URL").context("si must run inside an ISI (SI_URL missing)")?;
    let token =
        std::env::var("SI_TOKEN").context("si must run inside an ISI (SI_TOKEN missing)")?;
    request(
        &format!("{}/si", url.trim_end_matches('/')),
        &token,
        action,
        args,
    )
}

fn request(url: &str, token: &str, action: &str, args: Value) -> Result<Value> {
    let agent = ureq::Agent::config_builder()
        .http_status_as_error(false)
        .timeout_connect(Some(Duration::from_secs(2)))
        .build()
        .new_agent();
    let mut response = agent
        .post(url)
        .header("Authorization", &format!("Bearer {token}"))
        .send_json(json!({"action":action,"args":args}))
        .with_context(|| format!("interpreter request `{action}` to {url} failed"))?;
    let status = response.status();
    let body = response.body_mut().read_to_string().with_context(|| {
        format!("could not read the HTTP {status} answer to `{action}` from {url}")
    })?;
    let value: Value = serde_json::from_str(&body).with_context(|| {
        format!(
            "interpreter answered `{action}` at {url} with HTTP {status} that is not JSON:\n{}",
            if body.trim().is_empty() {
                "(empty body)"
            } else {
                &body
            }
        )
    })?;
    if status.as_u16() >= 400 {
        // The daemon's `error` is already its full cause chain; anything else is shown whole.
        match value.get("error").and_then(Value::as_str) {
            Some(error) => bail!("{error}"),
            None => bail!(
                "interpreter answered `{action}` at {url} with HTTP {status} and no error message:\n{body}"
            ),
        }
    }
    Ok(value)
}

struct App {
    runtime: Arc<Runtime>,
    daemon: Daemon,
    mutation: Mutex<()>,
    proxy: Mutex<Option<crate::proxy::Proxy>>,
}

pub fn serve(port: u16, no_proxy: bool) -> Result<()> {
    crate::mark_daemon();
    let dir = directory();
    state::private_dir(&dir)?;
    let _lock = lock(&dir.join("daemon.lock"), true)?;
    let mut selected = port;
    // A busy port is expected, so step down; why the requested one failed is still kept.
    let mut refused = None;
    let server = loop {
        match Server::http(("127.0.0.1", selected)) {
            Ok(server) => break server,
            Err(error) if selected <= 1024 => bail!(
                "no available port in 1024..={port}: {}port {selected}: {error}",
                refused
                    .map(|first| format!("port {port}: {first}; "))
                    .unwrap_or_default()
            ),
            Err(error) => {
                refused.get_or_insert_with(|| error.to_string());
                selected -= 1;
            }
        }
    };
    if let Some(first) = &refused {
        eprintln!("port {port} is unavailable ({first}); listening on {selected} instead");
    }
    let runtime = Runtime::new(format!("http://127.0.0.1:{selected}"));
    let mut connections = saved()?;
    for connection in &mut connections {
        match compile(&connection.yaml).and_then(|cfg| {
            let current = descriptor(&cfg);
            runtime.connect(cfg)?;
            Ok(current)
        }) {
            Ok(current) => *connection = current,
            Err(error) => report(
                &connection.home,
                None,
                "interpreter",
                &format!(
                    "restore {} from {} failed: {error:#}",
                    connection.id,
                    connection.yaml.display()
                ),
            ),
        }
    }
    state::write_json(&dir.join("connections.json"), &connections)?;
    let hosts = runtime
        .silicons
        .read()
        .unwrap()
        .values()
        .map(|s| descriptor(&s.cfg).host)
        .collect::<Vec<_>>();
    let proxy = if no_proxy {
        None
    } else {
        Some(
            crate::proxy::Proxy::start(&dir, selected, &hosts)
                .context("start the local routing proxy (use --no-proxy to skip it)")?,
        )
    };
    let daemon = Daemon {
        pid: std::process::id(),
        port: selected,
        token: Uuid::new_v4().to_string(),
    };
    state::write_json(&dir.join("daemon.json"), &daemon)?;
    runtime.start_scheduler();
    let app = Arc::new(App {
        runtime: runtime.clone(),
        daemon,
        mutation: Mutex::new(()),
        proxy: Mutex::new(proxy),
    });
    // Listen while restored hooks attach: Ting may immediately replay pending batches.
    let restore = app.clone();
    thread::spawn(move || {
        let _guard = restore.mutation.lock().unwrap();
        let connections: Vec<_> = restore
            .runtime
            .silicons
            .read()
            .unwrap()
            .values()
            .cloned()
            .collect();
        for connected in connections {
            restore.runtime.start_inbox(&connected);
            if let Err(error) = register_ting(&connected) {
                let id = connected.cfg.silicon.id.as_deref().unwrap();
                let error = rollback(
                    &restore,
                    id,
                    error.context(format!("restore Ting for {id}")),
                );
                report(
                    &connected.cfg.home,
                    Some(connected.cfg.generation),
                    "ting",
                    &format!("{error:#}"),
                );
            }
        }
    });
    let update_ready = Arc::new(AtomicBool::new(false));
    crate::update::start(&runtime, update_ready.clone());
    let mut signals = signal_hook::iterator::Signals::new([libc::SIGINT, libc::SIGTERM])
        .context("listen for SIGINT and SIGTERM")?;
    let signal_handle = signals.handle();
    let stop = runtime.clone();
    let signal_thread = thread::spawn(move || {
        if signals.forever().next().is_some() {
            stop.stopping.store(true, Ordering::SeqCst);
        }
    });
    println!("silicon interpreter listening on {}", runtime.url);
    let mut restarting = false;
    while !runtime.stopping.load(Ordering::SeqCst) {
        if update_ready.load(Ordering::SeqCst) {
            if let Ok(_guard) = app.mutation.try_lock() {
                if runtime.begin_restart_if_idle() {
                    restarting = true;
                    break;
                }
            }
        }
        if let Some(request) = server
            .recv_timeout(Duration::from_millis(200))
            .with_context(|| format!("receive the next request on {}", runtime.url))?
        {
            let app = app.clone();
            thread::spawn(move || handle(app, request));
        }
    }
    runtime.shutdown();
    app.proxy.lock().unwrap().take();
    signal_handle.close();
    if let Err(panic) = signal_thread.join() {
        eprintln!(
            "signal listener panicked: {}",
            crate::failure::panic_message(&*panic)
        );
    }
    fs::remove_file(dir.join("daemon.json"))
        .with_context(|| format!("remove {}", dir.join("daemon.json").display()))?;
    if restarting {
        use std::os::unix::process::CommandExt;
        let executable = crate::update::managed_prefix()?.join("bin/silicon");
        let mut command = Command::new(&executable);
        command.arg("serve").arg("--port").arg(port.to_string());
        if no_proxy {
            command.arg("--no-proxy");
        }
        drop(_lock);
        return Err(command.exec()).with_context(|| {
            format!(
                "could not restart the updated interpreter {}",
                executable.display()
            )
        });
    }
    Ok(())
}

fn configuration(cfg: &Config) -> Result<Value> {
    let mut value = serde_json::to_value(cfg).context("serialize the configuration")?;
    let secrets = crate::telemetry::silicon_secrets(&value["silicon"]);
    if let Some(configs) = value
        .pointer_mut("/silicon/app_configs")
        .and_then(Value::as_object_mut)
    {
        for config in configs.values_mut() {
            *config = json!("[redacted]");
        }
    }
    crate::telemetry::redact_value(&mut value, &secrets);
    Ok(value)
}

fn header<'a>(req: &'a Request, key: &str) -> &'a str {
    req.headers()
        .iter()
        .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case(key))
        .map(|h| h.value.as_str())
        .unwrap_or("")
}

fn handle(app: Arc<App>, mut request: Request) {
    let _activity = match app.runtime.activity() {
        Ok(activity) => activity,
        Err(error) => {
            respond(
                request,
                503,
                json!({"error":format!("{error:#}; retry after it restarts")}),
            );
            return;
        }
    };
    let path = request.url().split('?').next().unwrap_or("").to_owned();
    let host = header(&request, "Host")
        .split(':')
        .next()
        .unwrap_or("")
        .to_owned();
    if request.method() == &Method::Get && path == "/ping" {
        let (status, value) = ping(&app, &host);
        respond(request, status, value);
        return;
    }
    if request.method() == &Method::Get
        && path == "/"
        && (host == "silicon.localhost" || host == "127.0.0.1")
    {
        send(
            request,
            Response::from_string(include_str!("dashboard.html")).with_header(
                Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap(),
            ),
        );
        return;
    }
    if request.method() != &Method::Post {
        let error = format!("{} {path} is not supported; use POST", request.method());
        respond(request, 405, json!({"error":error}));
        return;
    }
    let content_type = header(&request, "Content-Type");
    if !content_type
        .split(';')
        .next()
        .is_some_and(|v| v.trim() == "application/json")
    {
        let error = format!("Content-Type must be application/json, got {content_type:?}");
        respond(request, 415, json!({"error":error}));
        return;
    }
    let origin = header(&request, "Origin");
    if !origin.is_empty() && origin != format!("http://{host}") && origin != app.runtime.url {
        let error = format!(
            "cross-origin requests are not permitted: Origin {origin} is neither http://{host} nor {}",
            app.runtime.url
        );
        respond(request, 403, json!({"error":error}));
        return;
    }
    let auth = header(&request, "Authorization")
        .strip_prefix("Bearer ")
        .unwrap_or("")
        .to_owned();
    let caller = if path == "/si" {
        match app.runtime.caller(&auth) {
            Some(caller) => Some(caller),
            None => {
                let error = if auth.is_empty() {
                    "ISI capability required: send Authorization: Bearer $SI_TOKEN"
                } else {
                    "invalid ISI capability: this interpreter did not issue that SI_TOKEN, or its session has ended"
                };
                respond(request, 401, json!({"error":error}));
                return;
            }
        }
    } else {
        None
    };
    if path == "/control" && auth != app.daemon.token {
        let file = directory().join("daemon.json");
        let error = if auth.is_empty() {
            format!(
                "interpreter token required: send Authorization: Bearer <token from {}>",
                file.display()
            )
        } else {
            format!(
                "interpreter token rejected: it is not the token in {}; the interpreter may have restarted since it was read",
                file.display()
            )
        };
        respond(request, 401, json!({"error":error}));
        return;
    }
    let event_request = path == "/" || path == "/events";
    let limit = if event_request {
        1024 * 1024
    } else {
        16 * 1024 * 1024
    };
    let mut body = Vec::new();
    if let Err(error) = request.as_reader().take(limit + 1).read_to_end(&mut body) {
        let error = format!("could not read the request body for {path}: {error}");
        respond(request, 400, json!({"error":error}));
        return;
    }
    if body.len() as u64 > limit {
        let error = format!("request body for {path} exceeds its {limit}-byte limit");
        respond(request, 413, json!({"error":error}));
        return;
    }
    let body: Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(error) => {
            respond(
                request,
                400,
                json!({"error":format!("invalid JSON request body for {path}: {error}")}),
            );
            return;
        }
    };
    if path == "/control" || path == "/si" {
        crate::telemetry::interpreter("daemon", "request", json!({"path":path,"body":body}));
    }
    let result = if path == "/control" {
        guarded(|| control(&app, &body))
    } else if let Some(caller) = caller {
        guarded(|| internal_action(&app, &caller, &body))
    } else if event_request {
        let id = app
            .runtime
            .silicons
            .read()
            .unwrap()
            .iter()
            .find(|(_, c)| descriptor(&c.cfg).host == host)
            .map(|(id, _)| id.clone());
        let Some(id) = id else {
            let error = format!("no connected silicon serves host {host:?}");
            respond(request, 404, json!({"error":error}));
            return;
        };
        guarded(|| deliver(&app, &id, body))
    } else {
        let error = format!("unknown endpoint {path}; use /control, /si or /events");
        respond(request, 404, json!({"error":error}));
        return;
    };
    match result {
        Ok(_) if event_request => send(request, Response::empty(204)),
        Ok(value) => respond(request, 200, value),
        // Errors are masked where they are built; this last pass covers every Silicon's
        // registered values, because the answer leaves the process.
        Err(error) => respond(
            request,
            400,
            json!({"error":failure::mask_all(&format!("{error:#}"))}),
        ),
    }
}

/// Health for a Silicon host; when it is offline, `error` says why.
fn ping(app: &App, host: &str) -> (u16, Value) {
    let silicons = app.runtime.silicons.read().unwrap();
    let found = silicons.values().find(|c| descriptor(&c.cfg).host == host);
    let online = found
        .filter(|c| c.enabled.load(Ordering::SeqCst))
        .map(|c| descriptor(&c.cfg).id);
    let mut value = json!({"online":online.is_some(),"silicon":online,"timestamp":chrono::Utc::now().to_rfc3339()});
    if online.is_some() {
        return (200, value);
    }
    value["error"] = json!(match found {
        Some(c) => format!("silicon {} is disconnected", descriptor(&c.cfg).id),
        None => format!("no connected silicon serves host {host:?}"),
    });
    (404, value)
}

/// Queue a Ting batch. Ting alone hears the reply, so a rejection also goes to the Silicon's log
/// (and, through `respond`, to daemon.log).
fn deliver(app: &App, id: &str, body: Value) -> Result<Value> {
    let connected = app.runtime.get(id)?;
    let home = &connected.cfg.home;
    connected
        .ting
        .accept(body)
        .map(|()| Value::Null)
        .map_err(|error| {
            let reason = failure::mask(home, &format!("{error:#}"), &[]);
            let message = format!("rejected a Ting delivery: {reason}");
            log_error(home, Some(connected.cfg.generation), "webhook", &message);
            anyhow!(reason)
        })
}

/// A panic answers the request with its message instead of dropping the connection.
fn guarded(action: impl FnOnce() -> Result<Value>) -> Result<Value> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(action)).unwrap_or_else(|panic| {
        Err(anyhow!(
            "interpreter panicked: {} (where: {})",
            crate::failure::panic_message(&*panic),
            directory().join("daemon.log").display()
        ))
    })
}

/// A failure no caller is waiting on: daemon.log for the operator, silicon.log for the Silicon.
fn report(home: &Path, generation: Option<Uuid>, origin: &str, message: &str) {
    let message = failure::mask(home, message, &[]);
    eprintln!("{message}");
    log_error(home, generation, origin, &message);
}

/// An `[error]` line in the Silicon's log. A home whose directory is gone is not recreated
/// just to hold it; a failed write lands in daemon.log with the line it could not write.
fn log_error(home: &Path, generation: Option<Uuid>, origin: &str, message: &str) {
    let gone = fs::metadata(home.join(".silicon"))
        .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound);
    if gone {
        return;
    }
    if let Err(error) = crate::log_line_scoped(home, generation, "error", origin, message) {
        eprintln!("{error:#}; the {origin} error it was recording:\n{message}");
    }
}

/// Undo a half-made connection; failures here ride along with the error that caused it.
fn rollback(app: &App, id: &str, error: anyhow::Error) -> anyhow::Error {
    let error = crate::failure::also(
        error,
        app.runtime
            .disconnect(id)
            .with_context(|| format!("roll back: disconnect {id}")),
    );
    crate::failure::also(
        error,
        update_proxy(app).context("roll back: update local routing"),
    )
}

fn respond(request: Request, status: u16, value: Value) {
    let path = request.url().split('?').next().unwrap_or("");
    // Ting alone hears a rejected delivery; daemon.log keeps why that Silicon's events went missing.
    if status >= 400 && (path == "/" || path == "/events") {
        eprintln!(
            "answered {} {}{path} with HTTP {status}: {}",
            request.method(),
            header(&request, "Host"),
            value["error"].as_str().unwrap_or(&value.to_string())
        );
    }
    send(
        request,
        Response::from_string(value.to_string())
            .with_status_code(status)
            .with_header(Header::from_bytes("Content-Type", "application/json").unwrap()),
    );
}

/// A client that left before its answer is not an error to it, but daemon.log says what it missed.
fn send<R: Read>(request: Request, response: Response<R>) {
    let path = request.url().split('?').next().unwrap_or("").to_owned();
    let status = response.status_code().0;
    if let Err(error) = request.respond(response) {
        eprintln!("could not send the HTTP {status} answer for {path}: {error}");
    }
}

fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    match v.get(key) {
        Some(Value::String(value)) => Ok(value),
        None | Some(Value::Null) => bail!("{key} is required"),
        Some(other) => bail!("{key} must be a string, got {other}"),
    }
}

/// An optional string argument; a value of the wrong type is an error, never "absent".
fn optional<'a>(v: &'a Value, key: &str) -> Result<Option<&'a str>> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => text(v, key).map(Some),
    }
}

/// An optional boolean argument, false when absent.
fn flag(v: &Value, key: &str) -> Result<bool> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(false),
        Some(Value::Bool(value)) => Ok(*value),
        Some(other) => bail!("{key} must be boolean, got {other}"),
    }
}

fn control(app: &Arc<App>, body: &Value) -> Result<Value> {
    let args = &body["args"];
    let action = text(body, "action")?;
    match action {
        "settings" => Ok(json!(crate::settings::load()?)),
        "settings-set" => {
            let _guard = app.mutation.lock().unwrap();
            Ok(json!(crate::settings::set(
                text(args, "key")?,
                args["enabled"]
                    .as_bool()
                    .ok_or_else(|| anyhow!("enabled must be boolean, got {}", args["enabled"]))?
            )?))
        }
        "silicon-ping" => {
            let id = text(args, "silicon")?;
            let online = app.runtime.get(id);
            let mut value = json!({"silicon":id,"online":online.is_ok(),"timestamp":chrono::Utc::now().to_rfc3339()});
            if let Err(error) = online {
                value["error"] = json!(format!("{error:#}"));
            }
            Ok(value)
        }
        "configuration" => {
            let connected = app.runtime.get(text(args, "silicon")?)?;
            let mut cfg = connected.cfg.clone();
            cfg.silicon = connected.app_settings.read().unwrap().clone();
            cfg.flow = cfg.load_flow()?;
            configuration(&cfg)
        }
        "install" => crate::apps::install(text(args, "app_id")?),
        "uninstall" => crate::apps::uninstall(text(args, "app_id")?),
        "ping" => Ok(json!({"version":env!("CARGO_PKG_VERSION"),"pid":app.daemon.pid})),
        "compile" => {
            let cfg = compile(text(args, "yaml")?)?;
            Ok(json!({"valid":true,"connection":descriptor(&cfg),"warnings":cfg.warnings}))
        }
        "list" => Ok(json!(app
            .runtime
            .silicons
            .read()
            .unwrap()
            .values()
            .map(|c| descriptor(&c.cfg))
            .collect::<Vec<_>>())),
        "connect" => {
            let _guard = app.mutation.lock().unwrap();
            let cfg = compile(text(args, "yaml")?)?;
            let connection = descriptor(&cfg);
            let warnings = cfg.warnings.clone();
            let log = cfg.home.join(".silicon/silicon.log");
            let start = fs::metadata(&log).map(|m| m.len()).unwrap_or(0);
            if let Err(error) = app.runtime.connect(cfg) {
                return Err(with_progress(error, &log, start));
            }
            let result = (|| -> Result<()> {
                let connected = app.runtime.get(&connection.id)?;
                crate::progress::step(
                    &connected.cfg.home,
                    Some(connected.cfg.generation),
                    "Configuring local routing",
                    "Configured local routing",
                    || update_proxy(app),
                )?;
                app.runtime.start_inbox(&connected);
                register_ting(&connected)?;
                state::write_json(
                    &directory().join("connections.json"),
                    &app.runtime
                        .silicons
                        .read()
                        .unwrap()
                        .values()
                        .map(|c| descriptor(&c.cfg))
                        .collect::<Vec<_>>(),
                )?;
                Ok(())
            })();
            if let Err(error) = result {
                let error = with_progress(error, &log, start);
                return Err(rollback(app, &connection.id, error));
            }
            Ok(
                json!({"connection":connection,"warnings":warnings,"progress":connection_progress(&log,start)}),
            )
        }
        "disconnect" => {
            let _guard = app.mutation.lock().unwrap();
            let target = text(args, "target")?;
            let id = resolve(app, target)?;
            let mut errors = Vec::new();
            if let Err(error) = app.runtime.disconnect(&id) {
                errors.push(format!("disconnect: {error:#}"));
            }
            if let Err(error) = update_proxy(app) {
                errors.push(format!("update local routing: {error:#}"));
            }
            if let Err(error) = state::write_json(
                &directory().join("connections.json"),
                &app.runtime
                    .silicons
                    .read()
                    .unwrap()
                    .values()
                    .map(|c| descriptor(&c.cfg))
                    .collect::<Vec<_>>(),
            ) {
                // write_json names the file it could not save.
                errors.push(format!("{error:#}"));
            }
            if !errors.is_empty() {
                bail!(
                    "disconnected {id} with cleanup errors:\n{}",
                    errors.join("\n")
                );
            }
            Ok(json!({"disconnected":id}))
        }
        "event" => app
            .runtime
            .event(text(args, "silicon")?, args["event"].clone()),
        "sessions" => {
            let connected = app.runtime.get(text(args, "silicon")?)?;
            sessions(&app.runtime, &connected, args)
        }
        "show" => app.runtime.show(
            text(args, "silicon")?,
            text(args, "isi")?,
            optional(args, "id")?,
        ),
        "end" => {
            app.runtime.end(
                text(args, "silicon")?,
                text(args, "isi")?,
                optional(args, "id")?,
            )?;
            Ok(json!({"ended":true}))
        }
        "send" => {
            let options: SendOptions =
                serde_json::from_value(args.clone()).context("invalid send options")?;
            let sent = app.runtime.send(
                text(args, "silicon")?,
                None,
                text(args, "isi")?,
                text(args, "message")?,
                &options,
                false,
            )?;
            Ok(json!({"session":sent.session,"delivery_id":sent.id}))
        }
        "new-session" => {
            let id = text(args, "silicon")?;
            let isi = text(args, "isi")?;
            let connected = app.runtime.get(id)?;
            let current =
                app.runtime
                    .show_connected(&connected, isi, optional(args, "current_id")?)?;
            let session = &current["session"]["session_id"];
            let caller = app.runtime.session_caller(
                &connected,
                serde_json::from_value(session.clone()).with_context(|| {
                    format!("{isi}'s current session has an unexpected session_id {session}")
                })?,
            )?;
            Ok(json!(app.runtime.new_session_connected(
                &connected,
                &caller,
                &serde_json::from_value::<NewSession>(args.clone())
                    .context("invalid new-session arguments")?
            )?))
        }
        "logs" => {
            let connected = app.runtime.get(text(args, "silicon")?)?;
            let path = connected.cfg.home.join(".silicon/silicon.log");
            Ok(json!({"path":path,"lines":tail(&path,100)?}))
        }
        "auth-setup" => {
            let c = app.runtime.get(text(args, "silicon")?)?;
            Ok(
                json!({"app_id":auth::setup_scoped(&c.cfg.home,c.cfg.silicon.id.as_deref().unwrap(),c.cfg.silicon.org_id.as_deref().unwrap(),c.cfg.silicon.token.as_deref().unwrap(),text(args,"app")?,c.cfg.generation)?}),
            )
        }
        "auth-remove" => {
            let c = app.runtime.get(text(args, "silicon")?)?;
            auth::remove(&c.cfg.home, text(args, "app")?)?;
            Ok(json!({"removed":true}))
        }
        "shutdown" => {
            app.runtime.stopping.store(true, Ordering::SeqCst);
            Ok(json!({"stopping":true}))
        }
        _ => bail!("unknown control action {action:?}"),
    }
}

/// The Silicon's log lines since `start`; a read failure becomes the last line, never the answer.
fn connection_progress(path: &Path, start: u64) -> Vec<String> {
    match since(path, start) {
        Ok(text) => text.lines().map(str::to_owned).collect(),
        Err(error) => vec![format!("could not read progress: {error:#}")],
    }
}

/// The failure first, then what the Silicon's log recorded while it happened.
fn with_progress(error: anyhow::Error, log: &Path, start: u64) -> anyhow::Error {
    let lines = connection_progress(log, start);
    if lines.is_empty() {
        return error;
    }
    anyhow!("{error:#}\n{}", lines.join("\n"))
}

/// A connected Silicon by ID or YAML path; a miss says what was tried and what is connected.
fn resolve(app: &App, target: &str) -> Result<String> {
    let path = Path::new(target);
    let resolved = path.is_absolute().then(|| path.canonicalize());
    let silicons = app.runtime.silicons.read().unwrap();
    if let Some(id) = silicons.iter().find_map(|(id, c)| {
        let by_path = matches!(&resolved, Some(Ok(path)) if c.cfg.path == *path);
        (id.as_str() == target || by_path).then(|| id.clone())
    }) {
        return Ok(id);
    }
    let mut message = format!("unknown silicon: {target}");
    if let Some(Err(error)) = &resolved {
        message.push_str(&format!(" (resolving it as a YAML path failed: {error})"));
    }
    let mut connected: Vec<_> = silicons.keys().map(String::as_str).collect();
    connected.sort_unstable();
    message.push_str(&if connected.is_empty() {
        "; no Silicons are connected".to_owned()
    } else {
        format!("; connected: {}", connected.join(", "))
    });
    bail!("{message}")
}

fn update_proxy(app: &App) -> Result<()> {
    let hosts = app
        .runtime
        .silicons
        .read()
        .unwrap()
        .values()
        .map(|c| descriptor(&c.cfg).host)
        .collect::<Vec<_>>();
    if let Some(proxy) = app.proxy.lock().unwrap().as_mut() {
        proxy.update(&hosts)?;
    }
    Ok(())
}

fn register_ting(connected: &crate::runtime::Connected) -> Result<()> {
    crate::progress::step(
        &connected.cfg.home,
        Some(connected.cfg.generation),
        "Registering Ting webhook",
        "Registered Ting webhook",
        || {
            connected.ting.register(
                &connected.cfg,
                &format!("http://{}/events", descriptor(&connected.cfg).host),
            )
        },
    )
}

fn internal_action(app: &Arc<App>, caller: &crate::runtime::Caller, body: &Value) -> Result<Value> {
    let args = &body["args"];
    let action = text(body, "action")?;
    let connected = app.runtime.caller_connection(caller)?;
    if ["send", "sessions", "show", "end"].contains(&action) {
        app.runtime
            .authorize_target(&connected, caller, text(args, "isi")?)?;
    }
    match action {
        "app-install" | "app-uninstall" => {
            let _guard = app.mutation.lock().unwrap();
            app.runtime.caller_connection(caller)?;
            let id = text(args, "app_id")?;
            if !crate::apps::valid_id(id) {
                bail!("expected a bare Honeycomb app ID (e.g. dm), got {id:?}");
            }
            let install = action == "app-install";
            if !install && ["iam", "ting"].contains(&id) {
                bail!("cannot uninstall {id}: IAM and Ting are required interpreter dependencies");
            }
            let cfg = &connected.cfg;
            if install {
                crate::apps::install_at(&cfg.home, id)?;
                auth::setup_scoped(
                    &cfg.home,
                    &caller.silicon,
                    cfg.silicon.org_id.as_deref().unwrap(),
                    cfg.silicon.token.as_deref().unwrap(),
                    id,
                    cfg.generation,
                )?;
            } else {
                auth::remove(&cfg.home, id)?;
                crate::apps::uninstall_at(&cfg.home, id)?;
            }
            let updated = crate::config::set_app(&cfg.path, id, install)?;
            let mut redactions = cfg.clone();
            redactions.silicon.app_configs = updated.silicon.app_configs.clone();
            crate::telemetry::register(&redactions);
            *connected.app_settings.write().unwrap() = updated.silicon.clone();
            if install {
                let configs = updated
                    .silicon
                    .app_configs
                    .into_iter()
                    .filter(|(app, _)| app == id)
                    .collect();
                auth::configure(&cfg.home, &configs)?;
            }
            Ok(json!({"app_id":id,"installed":install}))
        }
        "send" => {
            let options: SendOptions =
                serde_json::from_value(args.clone()).context("invalid send options")?;
            let sent = app.runtime.send_connected(
                &connected,
                Some(caller),
                text(args, "isi")?,
                text(args, "message")?,
                &options,
                false,
            )?;
            Ok(json!({"session":sent.session,"delivery_id":sent.id}))
        }
        "sessions" => sessions(&app.runtime, &connected, args),
        "show" => app
            .runtime
            .show_connected(&connected, text(args, "isi")?, optional(args, "id")?),
        "end" => {
            app.runtime
                .end_connected(&connected, text(args, "isi")?, optional(args, "id")?)?;
            Ok(json!({"ended":true}))
        }
        "new-session" => Ok(json!(app.runtime.new_session_connected(
            &connected,
            caller,
            &serde_json::from_value::<NewSession>(args.clone())
                .context("invalid new-session arguments")?
        )?)),
        "auth-setup" => {
            let c = &connected;
            Ok(
                json!({"app_id":auth::setup_scoped(&c.cfg.home,&caller.silicon,c.cfg.silicon.org_id.as_deref().unwrap(),c.cfg.silicon.token.as_deref().unwrap(),text(args,"app")?,c.cfg.generation)?}),
            )
        }
        "auth-remove" => {
            let c = &connected;
            auth::remove(&c.cfg.home, text(args, "app")?)?;
            Ok(json!({"removed":true}))
        }
        _ => bail!("unknown si action {action:?}"),
    }
}

fn sessions(
    runtime: &Runtime,
    connected: &crate::runtime::Connected,
    args: &Value,
) -> Result<Value> {
    let archived = flag(args, "archived")?;
    let records = json!(runtime.list_connected(connected, text(args, "isi")?, archived)?);
    if !archived {
        return Ok(records);
    }
    let filters: Vec<String> = if args["filters"].is_null() {
        Vec::new()
    } else {
        serde_json::from_value(args["filters"].clone()).with_context(|| {
            format!(
                "archive filters must be a list of strings, got {}",
                args["filters"]
            )
        })?
    };
    let timezone =
        optional(args, "timezone")?.unwrap_or(connected.cfg.silicon.timezone.as_deref().unwrap());
    crate::cli::filter_sessions(records, &filters, timezone, chrono::Utc::now())
}

pub fn tail(path: &Path, count: usize) -> Result<Vec<String>> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        // No log yet; any other open failure is reported, not shown as an empty log.
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error).with_context(|| format!("open {}", path.display())),
    };
    let mut position = file
        .metadata()
        .with_context(|| format!("read the size of {}", path.display()))?
        .len();
    let mut chunks = Vec::new();
    let mut lines = 0;
    while position > 0 && lines <= count {
        let size = position.min(8192) as usize;
        position -= size as u64;
        let mut chunk = vec![0; size];
        file.seek(SeekFrom::Start(position))
            .and_then(|_| file.read_exact(&mut chunk))
            .with_context(|| format!("read {} at byte {position}", path.display()))?;
        lines += chunk.iter().filter(|b| **b == b'\n').count();
        chunks.push(chunk);
    }
    let bytes: Vec<_> = chunks.into_iter().rev().flatten().collect();
    Ok(String::from_utf8_lossy(&bytes)
        .lines()
        .rev()
        .take(count)
        .map(str::to_owned)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configuration_redacts_credentials_and_their_interpolated_copies() {
        let cfg: Config = serde_json::from_value(json!({
            "silicon": {
                "id": "si:test", "org_id": "org", "token": "private-silicon-value", "timezone": "UTC",
                "SILICON_HOME": "/home/silicon/project", "inference_providers": [],
                "app_configs": {"app": {"nested": {"custom": "private-app-value"}}},
                "space_station": {"table_name": "activity", "table_key": "private-table-value"}
            },
            "isi": {"worker": {"model": "fast", "dna": {
                "assemble": ["token=private-silicon-value key=private-table-value"]
            }}},
            "access": {}, "flow": [{"value": "private-table-value/private-silicon-value/private-app-value"}]
        }))
        .unwrap();
        let snapshot = configuration(&cfg).unwrap();
        assert_eq!(snapshot["silicon"]["token"], "[redacted]");
        assert_eq!(
            snapshot["silicon"]["space_station"]["table_key"],
            "[redacted]"
        );
        assert_eq!(
            snapshot["isi"]["worker"]["dna"]["assemble"][0],
            "token=[redacted] key=[redacted]"
        );
        assert_eq!(
            snapshot["flow"][0]["value"],
            "[redacted]/[redacted]/[redacted]"
        );
        assert_eq!(snapshot["silicon"]["app_configs"]["app"], "[redacted]");
        assert_eq!(
            snapshot["silicon"]["space_station"]["table_name"],
            "activity"
        );
        assert_eq!(snapshot["isi"]["worker"]["model"], "fast");
        assert_eq!(cfg.silicon.token.as_deref(), Some("private-silicon-value"));
    }

    #[test]
    fn http_compile_blocks_restart_until_its_shell_and_response_finish() {
        let home = tempfile::tempdir().unwrap();
        let yaml = home.path().join("silicon.yaml");
        fs::write(
            &yaml,
            format!(
                r#"
silicon:
  id: si:test
  org_id: org
  token: test
  timezone: UTC
  SILICON_HOME: {}
  inference_providers: [all-available-providers]
isi:
  a:
    model: '! touch started; while ! test -f release; do sleep 0.01; done; printf fast'
    primary_send_mode: global
    session_type: persistent
    dna: {{assemble: [], next_refresh: 30min}}
access: {{a: []}}
flow: []
"#,
                serde_json::to_string(home.path()).unwrap()
            ),
        )
        .unwrap();
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let daemon = Daemon {
            pid: std::process::id(),
            port: server.server_addr().to_ip().unwrap().port(),
            token: "test-capability".into(),
        };
        let runtime = Runtime::new(format!("http://127.0.0.1:{}", daemon.port));
        let app = Arc::new(App {
            runtime: runtime.clone(),
            daemon: daemon.clone(),
            mutation: Mutex::new(()),
            proxy: Mutex::new(None),
        });
        let handler = thread::spawn(move || {
            handle(
                app,
                server
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap()
                    .unwrap(),
            );
        });
        let request = thread::spawn(move || call(&daemon, "compile", json!({"yaml": yaml})));
        let deadline = Instant::now() + Duration::from_secs(5);
        let started = home.path().join("started");
        while !started.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        let shell_started = started.exists();
        let restarted_while_busy = runtime.begin_restart_if_idle();
        // Release the shell before asserting so even a failing check cannot strand it.
        fs::write(home.path().join("release"), "").unwrap();
        assert!(shell_started, "compile did not reach its Bash expression");
        assert!(!restarted_while_busy);
        assert_eq!(request.join().unwrap().unwrap()["valid"], true);
        handler.join().unwrap();
        assert!(runtime.begin_restart_if_idle());
    }

    #[test]
    fn http_errors_carry_the_tools_words_and_mask_every_registered_credential() {
        let home = tempfile::tempdir().unwrap();
        // A credential another Silicon registered with this interpreter.
        let other = PathBuf::from(format!("/test/{}", Uuid::new_v4()));
        let mut registered: Config = serde_json::from_value(json!({
            "silicon": {"id":"si:other", "org_id":"org", "token":"other-silicon-private-token"},
            "isi":{}, "access":{}, "flow":[]
        }))
        .unwrap();
        registered.home = other.clone();
        crate::telemetry::register(&registered);
        let yaml = home.path().join("silicon.yaml");
        fs::write(
            &yaml,
            format!(
                r#"
silicon:
  id: si:test
  org_id: org
  token: test
  timezone: UTC
  SILICON_HOME: {}
  inference_providers: [all-available-providers]
isi:
  a:
    model: '! echo "model lookup failed for other-silicon-private-token" >&2; echo "[\"no model\"]"; exit 3'
    primary_send_mode: global
    session_type: persistent
    dna: {{assemble: [], next_refresh: 30min}}
access: {{a: []}}
flow: []
"#,
                serde_json::to_string(home.path()).unwrap()
            ),
        )
        .unwrap();
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let daemon = Daemon {
            pid: std::process::id(),
            port: server.server_addr().to_ip().unwrap().port(),
            token: "test-capability".into(),
        };
        let app = Arc::new(App {
            runtime: Runtime::new(format!("http://127.0.0.1:{}", daemon.port)),
            daemon: daemon.clone(),
            mutation: Mutex::new(()),
            proxy: Mutex::new(None),
        });
        let handler = thread::spawn(move || {
            handle(
                app,
                server
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap()
                    .unwrap(),
            );
        });
        let error = format!(
            "{:#}",
            call(&daemon, "compile", json!({"yaml": yaml})).unwrap_err()
        );
        handler.join().unwrap();
        crate::telemetry::unregister(&other);
        for said in [
            "exit status: 3",
            "stderr:\nmodel lookup failed for [redacted]",
            "stdout:\n[\"no model\"]",
        ] {
            assert!(error.contains(said), "missing {said:?} in {error}");
        }
        assert!(!error.contains("other-silicon-private-token"), "{error}");
    }

    #[test]
    fn failed_interpreter_start_carries_its_status_and_every_line_it_wrote() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("daemon.log");
        fs::write(&log, "an earlier run's line\n").unwrap();
        let start = fs::metadata(&log).unwrap().len();
        let app = dir.path().join("silicon");
        fs::write(
            &app,
            "#!/bin/sh\necho 'binding 127.0.0.1:4242'\necho 'Error: Address already in use (os error 48)' >&2\necho '{\"hint\":\"stop the other interpreter\"}'\necho 'restoring with stk-0123456789abcdef' >&2\nexit 3\n",
        )
        .unwrap();
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700)).unwrap();
        let file = OpenOptions::new().append(true).open(&log).unwrap();
        let mut child = Command::new(&app)
            .stdout(file.try_clone().unwrap())
            .stderr(file)
            .spawn()
            .unwrap();
        let error = await_start(dir.path(), &mut child, start, Duration::from_secs(10))
            .err()
            .unwrap();
        let error = format!("{error:#}");
        assert!(
            error.starts_with("interpreter exited before answering: exit status: 3\n"),
            "{error}"
        );
        for expected in [
            "binding 127.0.0.1:4242",
            "Error: Address already in use (os error 48)",
            "{\"hint\":\"stop the other interpreter\"}",
            &log.display().to_string(),
            "last ping: read ",
        ] {
            assert!(error.contains(expected), "missing {expected:?} in {error}");
        }
        assert!(!error.contains("an earlier run's line"), "{error}");
        assert!(
            error.contains("restoring with [redacted]") && !error.contains("stk-0123456789abcdef"),
            "{error}"
        );
    }

    #[test]
    fn interpreter_answers_reach_the_caller_with_status_url_and_raw_body() {
        let server = Server::http(("127.0.0.1", 0)).unwrap();
        let url = format!(
            "http://127.0.0.1:{}/control",
            server.server_addr().to_ip().unwrap().port()
        );
        let tool = "`ting webhook http://x.localhost/events --json` failed: exit status: 3\nstderr:\nboom\nstdout:\n{\"ok\":false}";
        let answers = [
            (502, "upstream exploded\nsecond line".to_owned()),
            (400, json!({"error": tool}).to_string()),
            (500, json!({"detail": "no error field"}).to_string()),
        ];
        let replies = thread::spawn(move || {
            for (status, body) in answers {
                let request = server
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap()
                    .unwrap();
                request
                    .respond(Response::from_string(body).with_status_code(status))
                    .unwrap();
            }
        });
        let error = format!(
            "{:#}",
            request(&url, "t", "connect", json!({})).unwrap_err()
        );
        assert!(
            error.contains(&format!("`connect` at {url} with HTTP 502 Bad Gateway"))
                && error.contains("upstream exploded\nsecond line"),
            "{error}"
        );
        let error = request(&url, "t", "connect", json!({})).unwrap_err();
        assert_eq!(format!("{error:#}"), tool);
        let error = format!("{:#}", request(&url, "t", "list", json!({})).unwrap_err());
        assert!(
            error.contains("HTTP 500 Internal Server Error and no error message")
                && error.contains("{\"detail\":\"no error field\"}"),
            "{error}"
        );
        replies.join().unwrap();
    }

    #[test]
    fn misses_and_panics_explain_themselves() {
        let runtime = Runtime::new("http://127.0.0.1:1".into());
        let app = App {
            runtime,
            daemon: Daemon {
                pid: 1,
                port: 1,
                token: "t".into(),
            },
            mutation: Mutex::new(()),
            proxy: Mutex::new(None),
        };
        let error = resolve(&app, "/nonexistent/silicon.yaml")
            .unwrap_err()
            .to_string();
        assert!(
            error.starts_with(
                "unknown silicon: /nonexistent/silicon.yaml (resolving it as a YAML path failed: "
            ) && error.ends_with("; no Silicons are connected"),
            "{error}"
        );
        let error = guarded(|| panic!("lock poisoned by {}", "worker")).unwrap_err();
        assert!(
            error
                .to_string()
                .starts_with("interpreter panicked: lock poisoned by worker"),
            "{error}"
        );
        assert_eq!(
            text(&json!({"isi": 3}), "isi").unwrap_err().to_string(),
            "isi must be a string, got 3"
        );
        // A mistyped optional argument is reported, not silently treated as absent.
        assert_eq!(optional(&json!({"id": null}), "id").unwrap(), None);
        assert_eq!(
            optional(&json!({"id": 7}), "id").unwrap_err().to_string(),
            "id must be a string, got 7"
        );
        assert_eq!(
            flag(&json!({"archived": "yes"}), "archived")
                .unwrap_err()
                .to_string(),
            "archived must be boolean, got \"yes\""
        );
    }

    #[test]
    fn unattended_failures_reach_the_silicon_log_whole_and_masked() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join(".silicon")).unwrap();
        let failure = "restore Ting for si:x: `ting webhook http://x.localhost/events --json` failed: exit status: 3\nstderr:\nboom {\"access_token\":\"private-access-value\"}\nstdout:\n{\"ok\":false} stk-0123456789abcdef";
        report(home.path(), None, "ting", failure);
        let log = fs::read_to_string(home.path().join(".silicon/silicon.log")).unwrap();
        assert!(log.starts_with("[error] [ting/cli] ["), "{log}");
        assert_eq!(log.lines().count(), 1, "{log}");
        for expected in [
            "[restore Ting for si:x: `ting webhook http://x.localhost/events --json` failed: exit status: 3",
            "\\nstderr:\\nboom {\"access_token\":\"[redacted]\"}",
            "\\nstdout:\\n{\"ok\":false} [redacted]]",
        ] {
            assert!(log.contains(expected), "missing {expected:?} in {log}");
        }
        assert!(
            !log.contains("private-access-value") && !log.contains("stk-0123456789abcdef"),
            "{log}"
        );
        // A home that is gone is not recreated just to hold the line.
        let gone = home.path().join("gone");
        report(&gone, None, "ting", failure);
        assert!(!gone.exists());
    }
}
