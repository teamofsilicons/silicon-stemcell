use crate::{
    auth,
    config::Config,
    flow,
    runtime::{NewSession, Runtime, SendOptions},
    state,
};
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io::Read;
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
    if path.exists() {
        serde_json::from_slice(&fs::read(path)?).context("invalid connection registry")
    } else {
        Ok(Vec::new())
    }
}

pub fn descriptor(cfg: &Config) -> Connection {
    let id = cfg.silicon.id.clone().unwrap();
    Connection {
        host: format!("{}.localhost", id.replace(':', ".")),
        id,
        yaml: cfg.path.clone(),
        home: cfg.home.clone(),
    }
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
    let read = || -> Result<Daemon> {
        serde_json::from_slice(&fs::read(dir.join("daemon.json"))?)
            .context("invalid daemon metadata")
    };
    if let Ok(daemon) = read() {
        if call(&daemon, "ping", json!({})).is_ok() {
            return Ok(daemon);
        }
    }
    if !start {
        bail!("interpreter is not running; run silicon connect YAML");
    }
    let _startup = lock(&dir.join("startup.lock"), false)?;
    if let Ok(daemon) = read() {
        if call(&daemon, "ping", json!({})).is_ok() {
            return Ok(daemon);
        }
    }
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(dir.join("daemon.log"))?;
    let mut child = Command::new(std::env::current_exe()?)
        .arg("serve")
        .stdin(Stdio::null())
        .stdout(file.try_clone()?)
        .stderr(file)
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if let Ok(daemon) = read() {
            if call(&daemon, "ping", json!({})).is_ok() {
                return Ok(daemon);
            }
        }
        if let Some(status) = child.try_wait()? {
            bail!(
                "interpreter exited {status}; see {}",
                dir.join("daemon.log").display()
            );
        }
        if Instant::now() >= deadline {
            bail!(
                "interpreter is still starting; see {}",
                dir.join("daemon.log").display()
            );
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn lock(path: &Path, nonblocking: bool) -> Result<File> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)?;
    let flags = libc::LOCK_EX | if nonblocking { libc::LOCK_NB } else { 0 };
    if unsafe { libc::flock(file.as_raw_fd(), flags) } != 0 {
        bail!("another interpreter already holds {}", path.display());
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
        .context("interpreter request failed")?;
    let status = response.status().as_u16();
    let value: Value = response
        .body_mut()
        .read_json()
        .context("interpreter response was not JSON")?;
    if status >= 400 {
        bail!(
            "{}",
            value
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("interpreter request failed")
        );
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
    let dir = directory();
    state::private_dir(&dir)?;
    let _lock = lock(&dir.join("daemon.lock"), true)?;
    let mut selected = port;
    let server = loop {
        match Server::http(("127.0.0.1", selected)) {
            Ok(server) => break server,
            Err(error) if selected <= 1024 => bail!("no available port in 1024..={port}: {error}"),
            Err(_) => selected -= 1,
        }
    };
    let runtime = Runtime::new(format!("http://127.0.0.1:{selected}"));
    for connection in saved()? {
        match compile(&connection.yaml).and_then(|cfg| runtime.connect(cfg)) {
            Ok(()) => {}
            Err(error) => {
                eprintln!("restore {} failed: {error:#}", connection.id);
            }
        }
    }
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
        Some(crate::proxy::Proxy::start(&dir, selected, &hosts)?)
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
    let update_ready = Arc::new(AtomicBool::new(false));
    crate::update::start(&runtime, update_ready.clone());
    let mut signals = signal_hook::iterator::Signals::new([libc::SIGINT, libc::SIGTERM])?;
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
        if let Some(request) = server.recv_timeout(Duration::from_millis(200))? {
            let app = app.clone();
            thread::spawn(move || handle(app, request));
        }
    }
    runtime.shutdown();
    app.proxy.lock().unwrap().take();
    signal_handle.close();
    let _ = signal_thread.join();
    fs::remove_file(dir.join("daemon.json"))?;
    if restarting {
        use std::os::unix::process::CommandExt;
        let executable = crate::update::managed_prefix()?.join("bin/silicon");
        let mut command = Command::new(executable);
        command.arg("serve").arg("--port").arg(port.to_string());
        if no_proxy {
            command.arg("--no-proxy");
        }
        drop(_lock);
        return Err(command.exec()).context("could not restart updated interpreter");
    }
    Ok(())
}

fn header<'a>(req: &'a Request, key: &str) -> &'a str {
    req.headers()
        .iter()
        .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case(key))
        .map(|h| h.value.as_str())
        .unwrap_or("")
}

fn handle(app: Arc<App>, mut request: Request) {
    if app.runtime.stopping.load(Ordering::SeqCst) {
        respond(
            request,
            503,
            json!({"error":"interpreter is stopping; retry after it restarts"}),
        );
        return;
    }
    let path = request.url().split('?').next().unwrap_or("").to_owned();
    let host = header(&request, "Host")
        .split(':')
        .next()
        .unwrap_or("")
        .to_owned();
    if request.method() == &Method::Get
        && path == "/"
        && (host == "silicon.localhost" || host == "127.0.0.1")
    {
        let _ = request.respond(
            Response::from_string(include_str!("dashboard.html")).with_header(
                Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap(),
            ),
        );
        return;
    }
    if request.method() != &Method::Post {
        respond(request, 405, json!({"error":"POST required"}));
        return;
    }
    if !header(&request, "Content-Type")
        .split(';')
        .next()
        .is_some_and(|v| v.trim() == "application/json")
    {
        respond(
            request,
            415,
            json!({"error":"Content-Type must be application/json"}),
        );
        return;
    }
    let origin = header(&request, "Origin");
    if !origin.is_empty() && origin != format!("http://{host}") && origin != app.runtime.url {
        respond(
            request,
            403,
            json!({"error":"cross-origin requests are not permitted"}),
        );
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
                respond(request, 401, json!({"error":"invalid ISI capability"}));
                return;
            }
        }
    } else {
        None
    };
    if path == "/control" && auth != app.daemon.token {
        respond(request, 401, json!({"error":"interpreter token required"}));
        return;
    }
    let mut body = Vec::new();
    let read = request
        .as_reader()
        .take(16 * 1024 * 1024 + 1)
        .read_to_end(&mut body);
    if read.is_err() || body.len() > 16 * 1024 * 1024 {
        respond(
            request,
            413,
            json!({"error":"event body exceeds 16 MiB or could not be read"}),
        );
        return;
    }
    let body: Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(error) => {
            respond(
                request,
                400,
                json!({"error":format!("invalid JSON: {error}")}),
            );
            return;
        }
    };
    let result = if path == "/control" {
        control(&app, &body)
    } else if let Some(caller) = caller {
        internal_action(&app, &caller, &body)
    } else if path == "/" || path == "/events" {
        let id = app
            .runtime
            .silicons
            .read()
            .unwrap()
            .iter()
            .find(|(_, c)| descriptor(&c.cfg).host == host)
            .map(|(id, _)| id.clone());
        if let Some(id) = id {
            app.runtime.event(&id, body)
        } else {
            respond(request, 404, json!({"error":"unknown silicon host"}));
            return;
        }
    } else {
        respond(request, 404, json!({"error":"unknown endpoint"}));
        return;
    };
    match result {
        Ok(value) => respond(request, 200, value),
        Err(error) => respond(request, 400, json!({"error":format!("{error:#}")})),
    }
}

fn respond(request: Request, status: u16, value: Value) {
    let _ = request.respond(
        Response::from_string(value.to_string())
            .with_status_code(status)
            .with_header(Header::from_bytes("Content-Type", "application/json").unwrap()),
    );
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("{key} is required"))
}

fn control(app: &Arc<App>, body: &Value) -> Result<Value> {
    let args = &body["args"];
    match text(body, "action")? {
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
            app.runtime.connect(cfg)?;
            let result = (|| -> Result<()> {
                update_proxy(app)?;
                let connected = app.runtime.get(&connection.id)?;
                for command in &connected.cfg.silicon.webhook {
                    auth::webhook(
                        &connected.cfg.home,
                        command,
                        &format!("http://{}", connection.host),
                    )?;
                }
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
                let _ = app.runtime.disconnect(&connection.id);
                let _ = update_proxy(app);
                return Err(error);
            }
            Ok(json!({"connection":connection,"warnings":warnings}))
        }
        "disconnect" => {
            let _guard = app.mutation.lock().unwrap();
            let target = text(args, "target")?;
            let id = app
                .runtime
                .silicons
                .read()
                .unwrap()
                .iter()
                .find(|(id, c)| {
                    id.as_str() == target
                        || c.cfg.path == Path::new(target).canonicalize().unwrap_or_default()
                })
                .map(|(id, _)| id.clone())
                .ok_or_else(|| anyhow!("unknown silicon: {target}"))?;
            let mut errors = Vec::new();
            if let Err(error) = app.runtime.disconnect(&id) {
                errors.push(format!("{error:#}"));
            }
            if let Err(error) = update_proxy(app) {
                errors.push(format!("{error:#}"));
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
                errors.push(format!("{error:#}"));
            }
            if !errors.is_empty() {
                bail!("disconnected with cleanup errors: {}", errors.join("; "));
            }
            Ok(json!({"disconnected":id}))
        }
        "event" => app
            .runtime
            .event(text(args, "silicon")?, args["event"].clone()),
        "sessions" => sessions(&app.runtime, text(args, "silicon")?, args),
        "show" => app.runtime.show(
            text(args, "silicon")?,
            text(args, "isi")?,
            args["id"].as_str(),
        ),
        "end" => {
            app.runtime.end(
                text(args, "silicon")?,
                text(args, "isi")?,
                args["id"].as_str(),
            )?;
            Ok(json!({"ended":true}))
        }
        "send" => {
            let options: SendOptions = serde_json::from_value(args.clone())?;
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
            let current = app.runtime.show(id, isi, args["current_id"].as_str())?;
            let caller = crate::runtime::Caller {
                silicon: id.to_owned(),
                isi: isi.to_owned(),
                session: serde_json::from_value(current["session"]["session_id"].clone())?,
            };
            Ok(json!(app.runtime.new_session(
                &caller,
                &serde_json::from_value::<NewSession>(args.clone())?
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
                json!({"app_id":auth::setup(&c.cfg.home,c.cfg.silicon.id.as_deref().unwrap(),c.cfg.silicon.token.as_deref().unwrap(),text(args,"app")?)?}),
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
        _ => bail!("unknown control action"),
    }
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

fn internal_action(app: &Arc<App>, caller: &crate::runtime::Caller, body: &Value) -> Result<Value> {
    let args = &body["args"];
    let action = text(body, "action")?;
    if ["send", "sessions", "show", "end"].contains(&action) {
        app.runtime.authorize_target(caller, text(args, "isi")?)?;
    }
    match action {
        "send" => {
            let options: SendOptions = serde_json::from_value(args.clone())?;
            let sent = app.runtime.send(
                &caller.silicon,
                Some(caller),
                text(args, "isi")?,
                text(args, "message")?,
                &options,
                false,
            )?;
            Ok(json!({"session":sent.session,"delivery_id":sent.id}))
        }
        "sessions" => sessions(&app.runtime, &caller.silicon, args),
        "show" => app
            .runtime
            .show(&caller.silicon, text(args, "isi")?, args["id"].as_str()),
        "end" => {
            app.runtime
                .end(&caller.silicon, text(args, "isi")?, args["id"].as_str())?;
            Ok(json!({"ended":true}))
        }
        "new-session" => Ok(json!(app.runtime.new_session(
            caller,
            &serde_json::from_value::<NewSession>(args.clone())?
        )?)),
        "auth-setup" => {
            let c = app.runtime.get(&caller.silicon)?;
            Ok(
                json!({"app_id":auth::setup(&c.cfg.home,&caller.silicon,c.cfg.silicon.token.as_deref().unwrap(),text(args,"app")?)?}),
            )
        }
        "auth-remove" => {
            let c = app.runtime.get(&caller.silicon)?;
            auth::remove(&c.cfg.home, text(args, "app")?)?;
            Ok(json!({"removed":true}))
        }
        _ => bail!("unknown si action"),
    }
}

fn sessions(runtime: &Runtime, silicon: &str, args: &Value) -> Result<Value> {
    let archived = args["archived"].as_bool().unwrap_or(false);
    let records = json!(runtime.list(silicon, text(args, "isi")?, archived)?);
    if !archived {
        return Ok(records);
    }
    let filters: Vec<String> = if args["filters"].is_null() {
        Vec::new()
    } else {
        serde_json::from_value(args["filters"].clone())
            .context("archive filters must be a list of strings")?
    };
    let connected = runtime.get(silicon)?;
    let timezone = args["timezone"]
        .as_str()
        .unwrap_or(connected.cfg.silicon.timezone.as_deref().unwrap());
    crate::cli::filter_sessions(records, &filters, timezone, chrono::Utc::now())
}

pub fn tail(path: &Path, count: usize) -> Result<Vec<String>> {
    use std::io::{Seek, SeekFrom};
    if !path.exists() {
        return Ok(Vec::new());
    }
    let mut file = File::open(path)?;
    let mut position = file.metadata()?.len();
    let mut chunks = Vec::new();
    let mut lines = 0;
    while position > 0 && lines <= count {
        let size = position.min(8192) as usize;
        position -= size as u64;
        file.seek(SeekFrom::Start(position))?;
        let mut chunk = vec![0; size];
        file.read_exact(&mut chunk)?;
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
