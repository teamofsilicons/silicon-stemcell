use crate::{
    auth,
    config::Config,
    eval, flow, log_line,
    state::{self, Session},
};
use anyhow::{anyhow, bail, Context, Result};
use chrono::Utc;
use serde::Deserialize;
use serde_json::{json, Value};
use silicon_omni::{raw::Client, Ask, Chat, Event, Inference};
use std::collections::{BTreeMap, HashMap};
use std::fs::{self, OpenOptions};
use std::os::unix::fs::{symlink, OpenOptionsExt};
use std::path::PathBuf;
use std::process::{Child, Stdio};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc, Condvar, Mutex, RwLock, Weak,
};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

#[derive(Clone, Debug)]
pub struct Caller {
    pub silicon: String,
    pub isi: String,
    pub session: Uuid,
}

#[derive(Default, Clone, Deserialize)]
pub struct SendOptions {
    pub id: Option<String>,
    pub title: Option<String>,
    #[serde(default)]
    pub new: bool,
    #[serde(default)]
    pub archived: bool,
}

#[derive(Deserialize)]
pub struct NewSession {
    pub id: String,
    pub title: String,
    pub description: String,
    pub archive_current_session: bool,
}

/// Durable dispatch acceptance and its separate provider-delivery receipt.
pub struct Sent {
    pub session: Session,
    pub id: Uuid,
    receipt: Arc<SendReceipt>,
}

impl Sent {
    /// Wait for START/INJECTED, never for the inference turn to finish.
    pub fn wait_started(&self) -> Result<()> {
        self.receipt.wait().with_context(|| {
            format!(
                "delivery {} to {}:{}",
                self.id, self.session.isi, self.session.id
            )
        })
    }
}

struct SendReceipt {
    result: Mutex<Option<std::result::Result<(), String>>>,
    changed: Condvar,
    deadline: Instant,
}

impl SendReceipt {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            result: Mutex::new(None),
            changed: Condvar::new(),
            deadline: Instant::now() + Duration::from_secs(60),
        })
    }
    fn finish(&self, result: std::result::Result<(), String>) {
        let mut current = self.result.lock().unwrap();
        if current.is_none() {
            *current = Some(result);
            self.changed.notify_all();
        }
    }
    fn wait(&self) -> Result<()> {
        let result = self.result.lock().unwrap();
        let (result, _) = self
            .changed
            .wait_timeout_while(
                result,
                self.deadline.saturating_duration_since(Instant::now()),
                |result| result.is_none(),
            )
            .unwrap();
        match result.as_ref() {
            Some(Ok(())) => Ok(()),
            Some(Err(error)) => bail!("{error}"),
            None => bail!(
                "timed out awaiting provider START/INJECTED; the accepted delivery may still run"
            ),
        }
    }
}

pub struct Runtime {
    pub silicons: RwLock<BTreeMap<String, Arc<Connected>>>,
    callers: RwLock<HashMap<String, Caller>>,
    pub url: String,
    pub stopping: AtomicBool,
    dispatch_gate: RwLock<()>,
    activities: AtomicUsize,
}

struct Activity<'a>(&'a AtomicUsize);
impl Drop for Activity<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

pub struct Connected {
    pub cfg: Config,
    workers: Mutex<BTreeMap<Uuid, Arc<Worker>>>,
    pub enabled: AtomicBool,
}

struct Dispatch {
    id: Uuid,
    message: String,
    turn: Option<i64>,
    receipt: Arc<SendReceipt>,
    origin: Option<Caller>,
}

struct WorkerState {
    record: Session,
    pending: Vec<Dispatch>,
    text: String,
    next_dna: Option<Instant>,
}

pub struct Worker {
    connected: Weak<Connected>,
    runtime: Weak<Runtime>,
    state: Mutex<WorkerState>,
    /// Held only through initialization/send acceptance; never while waiting for a turn.
    client: Mutex<Option<Client>>,
    child: Mutex<Option<Child>>,
    capability: String,
    pub session_id: Uuid,
    stopped: AtomicBool,
}

impl Runtime {
    pub fn new(url: String) -> Arc<Self> {
        Arc::new(Self {
            silicons: RwLock::new(BTreeMap::new()),
            callers: RwLock::new(HashMap::new()),
            url,
            stopping: AtomicBool::new(false),
            dispatch_gate: RwLock::new(()),
            activities: AtomicUsize::new(0),
        })
    }

    pub fn connect(self: &Arc<Self>, cfg: Config) -> Result<()> {
        let _activity = self.activity()?;
        let id = cfg
            .silicon
            .id
            .as_deref()
            .ok_or_else(|| anyhow!("silicon.id missing"))?
            .to_owned();
        {
            let silicons = self.silicons.read().unwrap();
            if silicons.contains_key(&id) {
                bail!("{id} is already connected");
            }
            if silicons
                .values()
                .any(|s| s.cfg.path == cfg.path || s.cfg.home == cfg.home)
            {
                bail!("this YAML path or SILICON_HOME is already connected");
            }
        }
        state::private_dir(&cfg.home.join(".silicon"))?;
        auth::ensure_all(
            &cfg.home,
            &id,
            cfg.silicon.token.as_deref().unwrap_or_default(),
            &cfg.silicon.login,
        )?;
        log_line(
            &cfg.home,
            "runtime",
            "interpreter",
            &format!("connected {id}"),
        )?;
        self.silicons.write().unwrap().insert(
            id,
            Arc::new(Connected {
                cfg,
                workers: Mutex::new(BTreeMap::new()),
                enabled: AtomicBool::new(true),
            }),
        );
        Ok(())
    }

    pub fn disconnect(&self, id: &str) -> Result<()> {
        let _activity = self.activity()?;
        let connected = self
            .silicons
            .read()
            .unwrap()
            .get(id)
            .cloned()
            .ok_or_else(|| anyhow!("unknown silicon: {id}"))?;
        connected.enabled.store(false, Ordering::SeqCst);
        let workers: Vec<_> = connected
            .workers
            .lock()
            .unwrap()
            .values()
            .cloned()
            .collect();
        let mut errors = Vec::new();
        for worker in workers {
            if let Err(error) = worker.stop(None) {
                errors.push(error.to_string());
            }
        }
        for app in &connected.cfg.silicon.webhook {
            if let Err(error) = auth::unhook(&connected.cfg.home, app) {
                errors.push(error.to_string());
            }
        }
        self.callers.write().unwrap().retain(|_, c| c.silicon != id);
        self.silicons.write().unwrap().remove(id);
        if let Err(error) = log_line(
            &connected.cfg.home,
            "runtime",
            "interpreter",
            "disconnected",
        ) {
            errors.push(error.to_string());
        }
        if !errors.is_empty() {
            bail!("{}", errors.join("; "));
        }
        Ok(())
    }

    pub fn caller(&self, token: &str) -> Option<Caller> {
        self.callers.read().unwrap().get(token).cloned()
    }
    pub fn get(&self, id: &str) -> Result<Arc<Connected>> {
        self.silicons
            .read()
            .unwrap()
            .get(id)
            .cloned()
            .filter(|s| s.enabled.load(Ordering::SeqCst))
            .ok_or_else(|| anyhow!("silicon is not connected: {id}"))
    }

    pub fn event(self: &Arc<Self>, id: &str, request: Value) -> Result<Value> {
        let _activity = self.activity()?;
        if !request.get("type").is_some_and(Value::is_string)
            || !request.get("data").is_some_and(Value::is_object)
            || !request.get("metadata").is_some_and(Value::is_object)
        {
            bail!("event requires type:string, data:object, metadata:object");
        }
        let connected = self.get(id)?;
        let event_id = Uuid::new_v4();
        let cfg = &connected.cfg;
        log_line(&cfg.home, "event", "webhook", &request.to_string())?;
        let mut env = environment(cfg);
        env["request"] = request;
        let mut sent = Vec::new();
        let result = flow::execute(
            &cfg.flow,
            env,
            &cfg.home,
            "interpreter",
            |target, message, session| {
                let options = SendOptions {
                    id: session.map(str::to_owned),
                    new: true,
                    ..Default::default()
                };
                sent.push(self.send(id, None, target, message, &options, false)?);
                Ok(())
            },
        );
        result?;
        let mut errors = Vec::new();
        for delivery in sent {
            if let Err(error) = delivery.wait_started() {
                let error = format!("{error:#}");
                log_line(&cfg.home, "error", "webhook", &error)?;
                errors.push(error);
            }
        }
        if !errors.is_empty() {
            bail!("{}", errors.join("; "));
        }
        Ok(json!({"status":"ok","event_id":event_id}))
    }

    pub fn send(
        self: &Arc<Self>,
        id: &str,
        caller: Option<&Caller>,
        target: &str,
        message: &str,
        options: &SendOptions,
        control: bool,
    ) -> Result<Sent> {
        let _activity = self.activity()?;
        let connected = self.get(id)?;
        if let Some(caller) = caller {
            if caller.silicon != id {
                bail!("cannot send across silicons");
            }
            if !(options.archived && caller.isi == target)
                && !connected
                    .cfg
                    .access
                    .get(&caller.isi)
                    .is_some_and(|v| v.iter().any(|n| n == target))
            {
                bail!("{} cannot send to {target}", caller.isi);
            }
        }
        if options
            .id
            .as_ref()
            .is_some_and(|id| id.is_empty() || id.len() > 1024)
        {
            bail!("session id must contain 1–1024 bytes");
        }
        let worker = self.worker(&connected, target, options)?;
        worker.send(message, caller.cloned(), control)
    }

    fn worker(
        self: &Arc<Self>,
        connected: &Arc<Connected>,
        target: &str,
        options: &SendOptions,
    ) -> Result<Arc<Worker>> {
        let isi = connected
            .cfg
            .isi
            .get(target)
            .ok_or_else(|| anyhow!("unknown isi: {target}"))?;
        let by_session = isi.primary_send_mode.as_deref() == Some("session");
        let ephemeral = isi.session_type.as_deref() == Some("ephemeral");
        if by_session && options.id.is_none() {
            bail!("{target} uses session addressing; session_id/--id is required");
        }
        let mut workers = connected.workers.lock().unwrap();
        if !options.archived && !(ephemeral && !by_session) {
            if let Some(worker) = workers.values().find(|w| {
                let state = w.state.lock().unwrap();
                !w.stopped.load(Ordering::SeqCst)
                    && state.record.isi == target
                    && state.record.archived_at.is_none()
                    && (!by_session || options.id.as_deref() == Some(state.record.id.as_str()))
            }) {
                return Ok(worker.clone());
            }
        }
        let saved = state::sessions(&connected.cfg.home, target, options.archived)?;
        let record = if options.archived {
            let id = options
                .id
                .as_deref()
                .ok_or_else(|| anyhow!("--archived requires --id"))?;
            saved
                .into_iter()
                .find(|s| s.id == id || s.session_id.to_string() == id)
                .ok_or_else(|| anyhow!("archived session not found: {target}:{id}"))?
        } else if !ephemeral {
            match saved
                .into_iter()
                .find(|s| !by_session || options.id.as_deref() == Some(s.id.as_str()))
            {
                Some(s) => s,
                None => {
                    if by_session && !options.new {
                        bail!("session does not exist; pass --new to create it");
                    }
                    Session::new(
                        target,
                        if by_session {
                            options.id.as_deref()
                        } else {
                            None
                        },
                        options.title.as_deref().unwrap_or(""),
                        false,
                    )
                }
            }
        } else {
            if by_session && options.title.is_none() && !options.new {
                bail!("new ephemeral session requires --title");
            }
            Session::new(
                target,
                if by_session {
                    options.id.as_deref()
                } else {
                    None
                },
                options.title.as_deref().unwrap_or(""),
                true,
            )
        };
        if let Some(worker) = workers
            .get(&record.session_id)
            .filter(|w| !w.stopped.load(Ordering::SeqCst))
        {
            return Ok(worker.clone());
        }
        let session_id = record.session_id;
        let capability = Uuid::new_v4().to_string();
        let worker = Arc::new(Worker {
            connected: Arc::downgrade(connected),
            runtime: Arc::downgrade(self),
            state: Mutex::new(WorkerState {
                record,
                pending: Vec::new(),
                text: String::new(),
                next_dna: None,
            }),
            client: Mutex::new(None),
            child: Mutex::new(None),
            capability: capability.clone(),
            session_id,
            stopped: AtomicBool::new(false),
        });
        self.callers.write().unwrap().insert(
            capability,
            Caller {
                silicon: connected.cfg.silicon.id.clone().unwrap(),
                isi: target.into(),
                session: session_id,
            },
        );
        workers.insert(session_id, worker.clone());
        Ok(worker)
    }

    pub fn list(&self, id: &str, target: &str, archived: bool) -> Result<Vec<Session>> {
        let connected = self.get(id)?;
        if !connected.cfg.isi.contains_key(target) {
            bail!("unknown isi: {target}");
        }
        let mut records: BTreeMap<_, _> = state::sessions(&connected.cfg.home, target, archived)?
            .into_iter()
            .map(|s| (s.session_id, s))
            .collect();
        for worker in connected.workers.lock().unwrap().values() {
            let record = worker.state.lock().unwrap().record.clone();
            if record.isi == target
                && record.archived_at.is_some() == archived
                && !worker.stopped.load(Ordering::SeqCst)
            {
                records.insert(record.session_id, record);
            }
        }
        let mut records: Vec<_> = records.into_values().collect();
        records.sort_by_key(|r| std::cmp::Reverse(r.last));
        Ok(records)
    }

    pub fn show(&self, id: &str, target: &str, session: Option<&str>) -> Result<Value> {
        let connected = self.get(id)?;
        let mut records = self.list(id, target, false)?;
        if session.is_some() {
            records.extend(self.list(id, target, true)?);
        }
        let record = records
            .iter()
            .find(|r| {
                session.is_none()
                    || session == Some(r.id.as_str())
                    || session == Some(r.session_id.to_string().as_str())
            })
            .ok_or_else(|| anyhow!("session not found for {target}"))?;
        let log = connected
            .cfg
            .home
            .join(".silicon/sessions/events")
            .join(format!("{}.jsonl", record.session_id));
        let events = crate::server::tail(&log, 100)?
            .iter()
            .map(|line| serde_json::from_str::<Value>(line))
            .collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(json!({"session":record,"events":events}))
    }

    pub fn end(&self, id: &str, target: &str, session: Option<&str>) -> Result<()> {
        let _activity = self.activity()?;
        let connected = self.get(id)?;
        if !connected.cfg.isi.contains_key(target) {
            bail!("unknown isi: {target}");
        }
        let matches = |record: &Session| {
            record.isi == target
                && record.archived_at.is_none()
                && (session.is_none()
                    || session == Some(record.id.as_str())
                    || session == Some(record.session_id.to_string().as_str()))
        };
        let mut ended = 0;
        let loaded = {
            // A restored session can be ended without initializing a provider. Keep
            // creation out of this disk transition so no worker restores the old record.
            let workers = connected.workers.lock().unwrap();
            for mut record in state::sessions(&connected.cfg.home, target, false)? {
                if matches(&record) && !workers.contains_key(&record.session_id) {
                    record.archive(&connected.cfg.home, None, None, None)?;
                    ended += 1;
                }
            }
            workers
                .values()
                .filter(|worker| matches(&worker.state.lock().unwrap().record))
                .cloned()
                .collect::<Vec<_>>()
        };
        for worker in loaded {
            let mut client = worker.client.lock().unwrap();
            {
                let mut state = worker.state.lock().unwrap();
                if !matches(&state.record) {
                    continue;
                }
                if !state.record.ephemeral {
                    state
                        .record
                        .archive(&connected.cfg.home, None, None, None)?;
                }
            }
            worker.stop_locked(&mut client, Some("session ended".into()))?;
            ended += 1;
        }
        if ended == 0 {
            bail!("no active session for {target}");
        }
        Ok(())
    }

    pub fn new_session(self: &Arc<Self>, caller: &Caller, options: &NewSession) -> Result<Session> {
        let _activity = self.activity()?;
        let connected = self.get(&caller.silicon)?;
        let worker = connected
            .workers
            .lock()
            .unwrap()
            .get(&caller.session)
            .cloned()
            .ok_or_else(|| anyhow!("current session not found"))?;
        let record = worker.state.lock().unwrap().record.clone();
        if record.ephemeral {
            bail!("ephemeral sessions cannot start a successor session");
        }
        if record.archived_at.is_some() {
            bail!("the current session is already archived");
        }
        if !options.archive_current_session {
            bail!("pass --archive-current-session to preserve the current session");
        }
        if options.id.is_empty() {
            bail!("archive id must not be empty");
        }
        {
            // Serialize archive names and re-entry before releasing the current session address.
            let _workers = connected.workers.lock().unwrap();
            let mut current = worker.state.lock().unwrap();
            if current.record.archived_at.is_some() {
                bail!("the current session is already archived");
            }
            if state::sessions(&connected.cfg.home, &caller.isi, true)?
                .iter()
                .any(|s| s.id == options.id)
            {
                bail!("archive id already exists");
            }
            current.record.archive(
                &connected.cfg.home,
                Some(&options.id),
                Some(&options.title),
                Some(&options.description),
            )?;
        }
        worker.retire_if_idle()?;
        let by_session =
            connected.cfg.isi[&caller.isi].primary_send_mode.as_deref() == Some("session");
        let next = self.worker(
            &connected,
            &caller.isi,
            &SendOptions {
                id: by_session.then_some(record.id),
                new: true,
                ..Default::default()
            },
        )?;
        // New sessions are fully initialized (auth/DNA/Omni) before this CLI returns.
        {
            let mut client = next.client.lock().unwrap();
            if client.is_none() {
                *client = Some(next.initialize()?);
            }
        }
        let new_record = next.state.lock().unwrap();
        new_record.record.save(&connected.cfg.home)?;
        Ok(new_record.record.clone())
    }

    pub fn authorize_target(&self, caller: &Caller, target: &str) -> Result<()> {
        let connected = self.get(&caller.silicon)?;
        if caller.isi != target
            && !connected.cfg.access[&caller.isi]
                .iter()
                .any(|n| n == target)
        {
            bail!("{} cannot access {target}", caller.isi);
        }
        Ok(())
    }

    pub fn start_scheduler(self: &Arc<Self>) {
        let runtime = Arc::downgrade(self);
        thread::spawn(move || {
            let mut deadlines: HashMap<(String, String), Instant> = HashMap::new();
            loop {
                thread::sleep(Duration::from_millis(250));
                let Some(runtime) = runtime.upgrade() else {
                    break;
                };
                if runtime.stopping.load(Ordering::SeqCst) {
                    break;
                }
                let connections: Vec<_> = runtime
                    .silicons
                    .read()
                    .unwrap()
                    .iter()
                    .map(|(id, c)| (id.clone(), c.clone()))
                    .collect();
                let mut active = std::collections::HashSet::new();
                for (id, connected) in connections {
                    for (name, isi) in &connected.cfg.isi {
                        let Some(heartbeat) = &isi.heartbeat else {
                            continue;
                        };
                        let targets = if isi.primary_send_mode.as_deref() == Some("session") {
                            match runtime.list(&id, name, false) {
                                Ok(records) => {
                                    records.into_iter().map(|record| Some(record.id)).collect()
                                }
                                Err(error) => {
                                    let _ = log_line(
                                        &connected.cfg.home,
                                        "error",
                                        name,
                                        &format!("heartbeat sessions: {error:#}"),
                                    );
                                    continue;
                                }
                            }
                        } else {
                            vec![None]
                        };
                        for session in targets {
                            let address = session
                                .as_ref()
                                .map(|s| format!("{name}:{s}"))
                                .unwrap_or_else(|| name.clone());
                            let key = (id.clone(), address.clone());
                            active.insert(key.clone());
                            let now = Instant::now();
                            let due = deadlines.get(&key).is_some_and(|d| now >= *d);
                            if !deadlines.contains_key(&key) || due {
                                match interval(&heartbeat["next"], &connected.cfg, &address) {
                                    Ok(next) => {
                                        deadlines.insert(key, now + next);
                                    }
                                    Err(error) => {
                                        let _ = log_line(
                                            &connected.cfg.home,
                                            "error",
                                            &address,
                                            &format!("heartbeat schedule: {error:#}"),
                                        );
                                        deadlines.insert(key, now + Duration::from_secs(60));
                                        continue;
                                    }
                                }
                            }
                            if !due {
                                continue;
                            }
                            let (runtime, connected, id, name, heartbeat) = (
                                runtime.clone(),
                                connected.clone(),
                                id.clone(),
                                name.clone(),
                                heartbeat.clone(),
                            );
                            thread::spawn(move || {
                                let result = (|| -> Result<()> {
                                    let cfg = &connected.cfg;
                                    auth::ensure_all(
                                        &cfg.home,
                                        &id,
                                        cfg.silicon.token.as_deref().unwrap(),
                                        &cfg.silicon.login,
                                    )?;
                                    let source =
                                        heartbeat["message"].as_str().ok_or_else(|| {
                                            anyhow!("heartbeat.message must be a string")
                                        })?;
                                    let message = eval::evaluate(
                                        source,
                                        &environment(cfg),
                                        &cfg.home,
                                        &address,
                                    )?;
                                    runtime.send(
                                        &id,
                                        None,
                                        &name,
                                        &message,
                                        &SendOptions {
                                            id: session,
                                            ..Default::default()
                                        },
                                        true,
                                    )?;
                                    Ok(())
                                })();
                                if let Err(error) = result {
                                    let _ = log_line(
                                        &connected.cfg.home,
                                        "error",
                                        &address,
                                        &format!("heartbeat: {error:#}"),
                                    );
                                }
                            });
                        }
                    }
                }
                deadlines.retain(|key, _| active.contains(key));
            }
        });
    }

    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        for connected in self.silicons.read().unwrap().values() {
            let workers: Vec<_> = connected
                .workers
                .lock()
                .unwrap()
                .values()
                .cloned()
                .collect();
            for worker in workers {
                if let Err(e) = worker.stop(None) {
                    let _ = log_line(&connected.cfg.home, "error", "shutdown", &e.to_string());
                }
            }
        }
    }

    pub fn idle(&self) -> bool {
        self.activities.load(Ordering::SeqCst) == 0
            && self.silicons.read().unwrap().values().all(|connected| {
                connected
                    .workers
                    .lock()
                    .unwrap()
                    .values()
                    .all(|worker| worker.state.lock().unwrap().pending.is_empty())
            })
    }

    fn activity(&self) -> Result<Activity<'_>> {
        // The short reader lets callbacks dispatch recursively while a restart writer is waiting.
        let _gate = self.dispatch_gate.read().unwrap();
        if self.stopping.load(Ordering::SeqCst) {
            bail!("interpreter is stopping");
        }
        self.activities.fetch_add(1, Ordering::SeqCst);
        Ok(Activity(&self.activities))
    }

    pub fn begin_restart_if_idle(&self) -> bool {
        let _gate = self.dispatch_gate.write().unwrap();
        if self.stopping.load(Ordering::SeqCst) || !self.idle() {
            return false;
        }
        self.stopping.store(true, Ordering::SeqCst);
        true
    }
}

impl Worker {
    fn initialize(self: &Arc<Self>) -> Result<Client> {
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let runtime = self
            .runtime
            .upgrade()
            .ok_or_else(|| anyhow!("interpreter stopped"))?;
        let cfg = &connected.cfg;
        let record = self.state.lock().unwrap().record.clone();
        auth::ensure_all(
            &cfg.home,
            cfg.silicon.id.as_deref().unwrap(),
            cfg.silicon.token.as_deref().unwrap(),
            &cfg.silicon.login,
        )?;
        let omni_home = cfg
            .home
            .join(".silicon/omni")
            .join(record.session_id.to_string());
        state::private_dir(&omni_home)?;
        // Unix socket paths have a 104-byte ceiling on macOS. The data stays under SILICON_HOME.
        let short_base = PathBuf::from(format!("/tmp/silicon-{}", unsafe { libc::getuid() }));
        state::private_dir(&short_base)?;
        let short_home = short_base.join(record.session_id.simple().to_string());
        if let Ok(existing) = fs::read_link(&short_home) {
            if existing != omni_home {
                bail!("Omni socket alias belongs to another home");
            }
        } else {
            symlink(&omni_home, &short_home)?;
        }
        let socket = short_home.join("omnid.sock");
        let address = if cfg.isi[&record.isi].primary_send_mode.as_deref() == Some("session") {
            format!("{}:{}", record.isi, record.id)
        } else {
            record.isi.clone()
        };
        let daemon = std::env::var_os("OMNI_DAEMON")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("omnid"));
        let daemon_log = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(omni_home.join("daemon.log"))?;
        let mut child = crate::command(&daemon, &cfg.home)
            .current_dir(&cfg.home)
            .env("OMNI_HOME", &short_home)
            .env("OMNI_STDIO_LOGGED", "1")
            .env("SILICON_HOME", &cfg.home)
            .env("ISI", &address)
            .env("TZ", cfg.silicon.timezone.as_deref().unwrap_or("UTC"))
            .env("SI_URL", &runtime.url)
            .env("SI_TOKEN", &self.capability)
            .env_remove("SILICON_TOKEN")
            .env_remove("SILICON_INTERPRETER_TOKEN")
            .stdin(Stdio::null())
            .stdout(daemon_log.try_clone()?)
            .stderr(daemon_log)
            .spawn()
            .with_context(|| format!("cannot start {} — install Omni", daemon.display()))?;
        let setup = (|| -> Result<(Client, Chat)> {
            let deadline = Instant::now() + Duration::from_secs(20);
            let inference = loop {
                match Inference::connect_to(&socket) {
                    Ok(client) => break client,
                    Err(error) => {
                        if let Some(status) = child.try_wait()? {
                            bail!(
                                "Omni exited ({status}); see {}",
                                omni_home.join("daemon.log").display()
                            );
                        }
                        if Instant::now() >= deadline {
                            bail!("Omni startup timed out: {error}");
                        }
                        thread::sleep(Duration::from_millis(50));
                    }
                }
            };
            let providers = select_providers(&cfg.silicon.inference_providers, &inference)?;
            let mut chat =
                inference.load_or_create_session(record.session_id.to_string(), providers);
            chat.cwd(&cfg.home)?;
            chat.model(Ask::key(cfg.isi[&record.isi].model.as_deref().unwrap()))?;
            chat.system_prompt(assemble_dna(cfg, &record.isi, &address)?)?;
            chat.start_since(-1)?;
            self.refresh_deadline()?;
            Ok((inference.raw().clone(), chat))
        })();
        let (client, chat) = match setup {
            Ok(ready) => ready,
            Err(error) => {
                // Child does not terminate on Drop. A failed setup must not leave an untracked daemon.
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        *self.child.lock().unwrap() = Some(child);
        let worker = self.clone();
        thread::spawn(move || worker.listen(chat));
        Ok(client)
    }

    fn send(
        self: &Arc<Self>,
        message: &str,
        origin: Option<Caller>,
        control: bool,
    ) -> Result<Sent> {
        let runtime = self
            .runtime
            .upgrade()
            .ok_or_else(|| anyhow!("interpreter stopped"))?;
        let _activity = runtime.activity()?;
        let mut client = self.client.lock().unwrap();
        if self.stopped.load(Ordering::SeqCst) {
            bail!("session has ended");
        }
        if client.is_none() {
            *client = Some(self.initialize()?);
        }
        let id = Uuid::new_v4();
        let receipt = SendReceipt::new();
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        {
            let mut state = self.state.lock().unwrap();
            state.pending.push(Dispatch {
                id,
                message: message.into(),
                turn: None,
                receipt: receipt.clone(),
                origin,
            });
            state.record.status = "running".into();
            state.record.last = Utc::now();
        }
        let result = client
            .as_ref()
            .unwrap()
            .send(&self.session_id.to_string(), message);
        if !matches!(result, Ok(true)) {
            let error = result
                .err()
                .map(|e| e.to_string())
                .unwrap_or_else(|| "Omni rejected message".into());
            let mut state = self.state.lock().unwrap();
            if let Some(pos) = state.pending.iter().position(|d| d.id == id) {
                state.pending.remove(pos).receipt.finish(Err(error.clone()));
            }
            bail!("{error}");
        }
        let mut state = self.state.lock().unwrap();
        if !control {
            state.record.new_messages += 1;
        }
        if let Err(error) = state.record.save(&connected.cfg.home) {
            // Accepted work keeps running even if metadata persistence fails; the caller sees the failure.
            log_line(
                &connected.cfg.home,
                "error",
                &state.record.isi,
                &format!("session state write failed: {error}"),
            )?;
            return Err(error);
        }
        log_line(&connected.cfg.home, "send", &state.record.isi, message)?;
        let sent = Sent {
            session: state.record.clone(),
            id,
            receipt,
        };
        let name = state.record.isi.clone();
        drop(state);
        drop(client);
        if !control {
            if let Err(error) = self.suggest_session() {
                log_line(
                    &connected.cfg.home,
                    "error",
                    &name,
                    &format!("new session suggestion: {error:#}"),
                )?;
            }
        }
        Ok(sent)
    }

    fn suggest_session(self: &Arc<Self>) -> Result<()> {
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let record = self.state.lock().unwrap().record.clone();
        let isi = &connected.cfg.isi[&record.isi];
        let Some(suggestion) = &isi.new_session_suggestion else {
            return Ok(());
        };
        let address = if isi.primary_send_mode.as_deref() == Some("session") {
            format!("{}:{}", record.isi, record.id)
        } else {
            record.isi.clone()
        };
        let env = environment(&connected.cfg);
        let minimum = eval::value(
            &suggestion["min_new_messages"],
            &env,
            &connected.cfg.home,
            &address,
        )?
        .as_u64()
        .filter(|count| *count > 0)
        .ok_or_else(|| anyhow!("min_new_messages must be a positive integer"))?;
        let cooldown = interval(&suggestion["cooldown_minutes"], &connected.cfg, &address)?;
        let now = Utc::now();
        let message = {
            let state = self.state.lock().unwrap();
            if !suggestion_due(&state.record, minimum, cooldown, now) {
                return Ok(());
            }
            drop(state);
            let source = suggestion["suggestion_message"]
                .as_str()
                .ok_or_else(|| anyhow!("suggestion_message must be a string"))?;
            eval::evaluate(source, &env, &connected.cfg.home, &address)?
        };
        let previous = {
            let mut state = self.state.lock().unwrap();
            if !suggestion_due(&state.record, minimum, cooldown, now) {
                return Ok(());
            }
            let previous = (
                state.record.last_suggestion,
                state.record.messages_at_suggestion,
            );
            state.record.last_suggestion = Some(now);
            state.record.messages_at_suggestion = state.record.new_messages;
            if let Err(error) = state.record.save(&connected.cfg.home) {
                state.record.last_suggestion = previous.0;
                state.record.messages_at_suggestion = previous.1;
                return Err(error);
            }
            previous
        };
        if let Err(error) = self.send(&message, None, true) {
            let mut state = self.state.lock().unwrap();
            if state.record.last_suggestion == Some(now) {
                state.record.last_suggestion = previous.0;
                state.record.messages_at_suggestion = previous.1;
                state.record.save(&connected.cfg.home)?;
            }
            return Err(error);
        }
        log_line(&connected.cfg.home, "suggestion", &address, &message)?;
        Ok(())
    }

    fn listen(self: Arc<Self>, mut chat: Chat) {
        while !self.stopped.load(Ordering::SeqCst) {
            match chat.next_event_timeout(Duration::from_millis(200)) {
                Ok(Some(event)) => {
                    let queued = chat.state().map_or(0, |state| state.queued);
                    if let Err(error) = self.on_event(event, chat.idle(), queued) {
                        self.fail(&error.to_string());
                        break;
                    }
                }
                Ok(None) => {
                    if chat.status() == "stopped" {
                        self.fail("Omni session stopped before completion");
                        break;
                    }
                }
                Err(error) => {
                    self.fail(&format!("Omni event stream failed: {error}"));
                    break;
                }
            }
            self.schedule_dna_refresh();
        }
        let _ = chat.detach();
    }

    fn on_event(self: &Arc<Self>, event: Event, omni_idle: bool, queued: usize) -> Result<()> {
        let runtime = self
            .runtime
            .upgrade()
            .ok_or_else(|| anyhow!("interpreter stopped"))?;
        let _activity = runtime.activity()?;
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let cfg = &connected.cfg;
        let mut state = self.state.lock().unwrap();
        log_line(
            &cfg.home,
            &event.event_type,
            &state.record.isi,
            &serde_json::to_string(&event)?,
        )?;
        if !state.record.ephemeral || state.record.archived_at.is_some() {
            state::append_json(
                &cfg.home
                    .join(".silicon/sessions/events")
                    .join(format!("{}.jsonl", self.session_id)),
                &event,
            )?;
        }
        // Replaced providers can emit late errors/ENDs; these belong in logs, not current lifecycle.
        if event.extra.get("late").and_then(Value::as_bool) == Some(true) {
            return Ok(());
        }
        match event.event_type.as_str() {
            Event::START | Event::INJECTED => {
                if event.event_type == Event::START {
                    state.text.clear();
                }
                if let Some(dispatch) = state
                    .pending
                    .iter_mut()
                    .find(|d| d.turn.is_none() && d.message == event.text)
                {
                    dispatch.turn = Some(event.turn);
                    dispatch.receipt.finish(Ok(()));
                }
            }
            Event::TEXT => {
                if !state.text.is_empty() {
                    state.text.push('\n');
                }
                state.text.push_str(&event.text);
            }
            Event::ERROR => {
                if event.kind != "stderr"
                    && event.extra.get("willRetry").and_then(Value::as_bool) != Some(true)
                    && omni_idle
                {
                    let error = format!("{}: {}", event.kind, event.error);
                    for dispatch in &state.pending {
                        dispatch.receipt.finish(Err(error.clone()));
                    }
                    state.record.status = "error".into();
                    state.record.save(&cfg.home)?;
                }
            }
            Event::END => {
                // Native next-turn injections can share event.turn with an earlier END.
                // Omni's snapshot is the authority for whether all accepted work is now idle.
                if !omni_idle || queued != 0 {
                    return Ok(());
                }
                let mut finished = Vec::new();
                let mut pending = Vec::new();
                for dispatch in state.pending.drain(..) {
                    if dispatch.turn.is_some() {
                        finished.push(dispatch);
                    } else {
                        pending.push(dispatch);
                    }
                }
                state.pending = pending;
                state.record.last = Utc::now();
                if state.pending.is_empty() {
                    state.record.status = if state.record.archived_at.is_some() {
                        "archived"
                    } else {
                        "idle"
                    }
                    .into();
                }
                state.record.save(&cfg.home)?;
                let reply = state.text.clone();
                let reply_to_caller = state.record.ephemeral || state.record.archived_at.is_some();
                let name = state.record.isi.clone();
                drop(state);
                // Disposable work and questions to archived sessions return to their caller.
                for dispatch in finished {
                    let result = if reply_to_caller {
                        if let Some(origin) = dispatch.origin.as_ref() {
                            let target = connected
                                .workers
                                .lock()
                                .unwrap()
                                .get(&origin.session)
                                .cloned();
                            if let Some(target) = target {
                                target
                                    .send(&format!("{name} completed:\n{reply}"), None, true)
                                    .map(|_| ())
                            } else {
                                Err(anyhow!("caller session disappeared before ephemeral reply"))
                            }
                        } else {
                            Ok(())
                        }
                    } else {
                        Ok(())
                    };
                    if let Err(error) = result {
                        log_line(
                            &cfg.home,
                            "error",
                            &name,
                            &format!("ephemeral reply: {error}"),
                        )?;
                    }
                }
                self.retire_if_idle()?;
                return Ok(());
            }
            _ => {}
        }
        Ok(())
    }

    fn fail(&self, message: &str) {
        // A failed listener cannot be reused; remove it so a later send restores a fresh worker.
        if let Err(error) = self.stop(Some(message.into())) {
            if let Some(connected) = self.connected.upgrade() {
                let _ = log_line(
                    &connected.cfg.home,
                    "error",
                    "worker cleanup",
                    &format!("{message}; {error:#}"),
                );
            }
        }
    }

    pub fn stop(&self, error: Option<String>) -> Result<()> {
        let mut client = self.client.lock().unwrap();
        self.stop_locked(&mut client, error)
    }

    fn retire_if_idle(&self) -> Result<bool> {
        // Same lock as send: either the new message is accepted first, or it sees an ended session.
        let mut client = self.client.lock().unwrap();
        let eligible = {
            let state = self.state.lock().unwrap();
            (state.record.ephemeral || state.record.archived_at.is_some())
                && state.pending.is_empty()
        };
        if !eligible {
            return Ok(false);
        }
        self.stop_locked(&mut client, None)?;
        Ok(true)
    }

    fn stop_locked(&self, client: &mut Option<Client>, error: Option<String>) -> Result<()> {
        if self.stopped.load(Ordering::SeqCst) {
            return Ok(());
        }
        if let Some(client) = client.take() {
            let _ = client.stop(&self.session_id.to_string());
        }
        let mut errors = Vec::new();
        if let Some(child) = self.child.lock().unwrap().take() {
            if let Err(error) = terminate_child(child) {
                errors.push(error.to_string());
            }
        }
        // Keep the old worker discoverable until its daemon exits. Its client lock rejects racing sends.
        self.stopped.store(true, Ordering::SeqCst);
        let mut state = self.state.lock().unwrap();
        let receipt_error = error
            .clone()
            .unwrap_or_else(|| "session ended before provider delivery".into());
        for dispatch in state.pending.drain(..) {
            dispatch.receipt.finish(Err(receipt_error.clone()));
        }
        state.record.status = if state.record.archived_at.is_some() {
            "archived"
        } else if error.is_some() {
            "error"
        } else {
            "stopped"
        }
        .into();
        state.record.last = Utc::now();
        let ephemeral = state.record.ephemeral && state.record.archived_at.is_none();
        let connected = self.connected.upgrade();
        if let Some(connected) = connected.as_ref() {
            if let Err(error) = state.record.save(&connected.cfg.home) {
                errors.push(error.to_string());
            }
            if let Some(error) = error.as_deref() {
                if let Err(error) = log_line(&connected.cfg.home, "error", &state.record.isi, error)
                {
                    errors.push(error.to_string());
                }
            }
        }
        drop(state);
        if let Some(runtime) = self.runtime.upgrade() {
            runtime.callers.write().unwrap().remove(&self.capability);
        }
        if let Some(connected) = connected {
            // A concurrent restart can already have replaced a failed worker with the same UUID.
            let mut workers = connected.workers.lock().unwrap();
            if workers
                .get(&self.session_id)
                .is_some_and(|worker| worker.capability == self.capability)
            {
                workers.remove(&self.session_id);
            }
            drop(workers);
            if ephemeral {
                let directory = connected
                    .cfg
                    .home
                    .join(".silicon/omni")
                    .join(self.session_id.to_string());
                if directory.exists() {
                    if let Err(error) = fs::remove_dir_all(&directory) {
                        errors.push(error.to_string());
                    }
                }
                let alias = PathBuf::from(format!(
                    "/tmp/silicon-{}/{}",
                    unsafe { libc::getuid() },
                    self.session_id.simple()
                ));
                if fs::read_link(&alias).is_ok_and(|target| target == directory) {
                    if let Err(error) = fs::remove_file(alias) {
                        errors.push(error.to_string());
                    }
                }
            }
        }
        if !errors.is_empty() {
            bail!("{}", errors.join("; "));
        }
        Ok(())
    }

    fn refresh_deadline(&self) -> Result<()> {
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let record = self.state.lock().unwrap().record.clone();
        let isi = &connected.cfg.isi[&record.isi];
        let address = if isi.primary_send_mode.as_deref() == Some("session") {
            format!("{}:{}", record.isi, record.id)
        } else {
            record.isi
        };
        let next = isi
            .dna
            .as_ref()
            .and_then(|dna| dna.get("next_refresh"))
            .map(|next| {
                interval(next, &connected.cfg, &address).map(|duration| Instant::now() + duration)
            })
            .transpose()?;
        self.state.lock().unwrap().next_dna = next;
        Ok(())
    }

    fn schedule_dna_refresh(self: &Arc<Self>) {
        {
            let mut state = self.state.lock().unwrap();
            if !state.next_dna.is_some_and(|next| Instant::now() >= next) {
                return;
            }
            state.next_dna = None;
        }
        let worker = self.clone();
        thread::spawn(move || {
            let result = (|| -> Result<()> {
                let runtime = worker
                    .runtime
                    .upgrade()
                    .ok_or_else(|| anyhow!("interpreter stopped"))?;
                let _activity = runtime.activity()?;
                if worker.stopped.load(Ordering::SeqCst) {
                    return Ok(());
                }
                worker.refresh_dna()
            })();
            if let Err(error) = result {
                if let Some(connected) = worker.connected.upgrade() {
                    let _ = log_line(
                        &connected.cfg.home,
                        "error",
                        "dna",
                        &format!("DNA refresh: {error:#}"),
                    );
                }
                worker.state.lock().unwrap().next_dna =
                    Some(Instant::now() + Duration::from_secs(60));
            }
        });
    }

    fn refresh_dna(&self) -> Result<()> {
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let record = self.state.lock().unwrap().record.clone();
        let name = if connected.cfg.isi[&record.isi].primary_send_mode.as_deref() == Some("session")
        {
            format!("{}:{}", record.isi, record.id)
        } else {
            record.isi.clone()
        };
        let prompt = assemble_dna(&connected.cfg, &record.isi, &name)?;
        if let Some(client) = self.client.lock().unwrap().as_ref() {
            client.set(&self.session_id.to_string(), "system_prompt", json!(prompt))?;
        }
        self.refresh_deadline()?;
        Ok(())
    }
}

fn terminate_child(mut child: Child) -> Result<()> {
    if child.try_wait()?.is_none() {
        if let Err(error) = child.kill() {
            if child.try_wait()?.is_none() {
                return Err(error.into());
            }
        }
    }
    child.wait()?;
    Ok(())
}

fn suggestion_due(
    record: &Session,
    minimum: u64,
    cooldown: Duration,
    now: chrono::DateTime<Utc>,
) -> bool {
    record.archived_at.is_none()
        && record
            .new_messages
            .saturating_sub(record.messages_at_suggestion)
            >= minimum
        && record.last_suggestion.is_none_or(|last| {
            now.signed_duration_since(last)
                .to_std()
                .is_ok_and(|elapsed| elapsed >= cooldown)
        })
}

pub fn environment(cfg: &Config) -> Value {
    let mut silicon = serde_json::to_value(&cfg.silicon).unwrap();
    silicon.as_object_mut().unwrap().remove("token");
    json!({"silicon":silicon,"isi":cfg.isi,"access":cfg.access,"request":{},"var":{}})
}

fn assemble_dna(cfg: &Config, isi: &str, address: &str) -> Result<String> {
    let mut parts = Vec::new();
    if let Some(items) = cfg.isi[isi]
        .dna
        .as_ref()
        .and_then(|d| d.get("assemble"))
        .and_then(|v| v.as_sequence())
    {
        for item in items {
            let source = item
                .as_str()
                .ok_or_else(|| anyhow!("DNA item must be a string"))?;
            let text = eval::dna(source, &environment(cfg), &cfg.home, address);
            match text {
                Ok(text) => parts.push(text),
                Err(error) => log_line(
                    &cfg.home,
                    "error",
                    address,
                    &format!("DNA entry skipped: {error}"),
                )?,
            }
        }
    }
    let allowed: Vec<_> = cfg.access[isi]
        .iter()
        .map(|name| {
            let mode = cfg.isi[name].primary_send_mode.as_deref().unwrap();
            format!("{name} ({mode})")
        })
        .collect();
    parts.push(format!("You are {address}. SILICON_HOME is {}. Allowed ISIs: {}.\nUse `si isi send NAME MESSAGE`{}; `si isi --help`, `si session --help`, `si auth --help` explain the available commands.", cfg.home.display(), allowed.join(", "), if allowed.is_empty() { "" } else { " (session targets require --id)" }));
    Ok(parts.join("\n\n"))
}

fn interval(value: &serde_yaml::Value, cfg: &Config, isi: &str) -> Result<Duration> {
    let value = eval::value(value, &environment(cfg), &cfg.home, isi)?;
    let text = value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string());
    let text = text.trim();
    let (n, mul) = if let Some(n) = text.strip_suffix("min").or_else(|| text.strip_suffix('m')) {
        (n, 60.)
    } else if let Some(n) = text.strip_suffix('s') {
        (n, 1.)
    } else if let Some(n) = text.strip_suffix('h') {
        (n, 3600.)
    } else {
        (text, 60.)
    };
    let seconds = n.trim().parse::<f64>()? * mul;
    if !seconds.is_finite() || seconds <= 0. || seconds > 315360000. {
        bail!("interval must be positive, at most ten years");
    }
    Ok(Duration::from_secs_f64(seconds))
}

fn select_providers(
    value: &serde_yaml::Value,
    inference: &Inference,
) -> Result<Option<Vec<String>>> {
    use serde_yaml::Value as Y;
    if value.as_str() == Some("all-available-providers")
        || value
            .as_sequence()
            .is_some_and(|v| v.len() == 1 && v[0].as_str() == Some("all-available-providers"))
    {
        return Ok(None);
    }
    fn walk(v: &Y, available: &[String], selected: &mut Vec<String>) -> Result<()> {
        match v {
            Y::Sequence(items) => {
                for item in items {
                    walk(item, available, selected)?;
                }
            }
            Y::String(s) if s == "all-available-providers" => {
                for name in available {
                    if !selected.contains(name) {
                        selected.push(name.clone());
                    }
                }
            }
            Y::String(s) if s.starts_with("except ") => {
                let name = s.trim_start_matches("except ").trim();
                selected.retain(|s| s != name);
            }
            Y::String(s) => {
                if !available.contains(s) {
                    bail!("inference provider is not installed/authenticated: {s}");
                }
                if !selected.contains(s) {
                    selected.push(s.clone());
                }
            }
            _ => bail!("inference_providers must contain names or nested lists"),
        }
        Ok(())
    }
    let available = inference.get_available_providers(None)?;
    let mut selected = Vec::new();
    walk(value, &available, &mut selected)?;
    if selected.is_empty() {
        bail!("inference_providers selects no authenticated providers");
    }
    Ok(Some(selected))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn worker(ephemeral: bool) -> (tempfile::TempDir, Arc<Runtime>, Arc<Connected>, Arc<Worker>) {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg: Config = serde_yaml::from_str(&format!(
            r#"
silicon: {{id: 'test:org', token: test, timezone: UTC}}
isi:
  a: {{model: fast, primary_send_mode: global, session_type: {}}}
access: {{a: []}}
flow: []
"#,
            if ephemeral { "ephemeral" } else { "persistent" }
        ))
        .unwrap();
        cfg.home = dir.path().to_owned();
        cfg.path = dir.path().join("silicon.yaml");
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        runtime.connect(cfg).unwrap();
        let connected = runtime.get("test:org").unwrap();
        let worker = runtime
            .worker(&connected, "a", &SendOptions::default())
            .unwrap();
        (dir, runtime, connected, worker)
    }

    fn pending(worker: &Worker, message: &str) -> Sent {
        let id = Uuid::new_v4();
        let receipt = SendReceipt::new();
        let mut state = worker.state.lock().unwrap();
        state.pending.push(Dispatch {
            id,
            message: message.into(),
            turn: None,
            receipt: receipt.clone(),
            origin: None,
        });
        Sent {
            session: state.record.clone(),
            id,
            receipt,
        }
    }

    #[test]
    fn receipts_ack_provider_delivery_before_end_and_native_next_turns_do_not_retire_early() {
        let (_dir, _runtime, _connected, worker) = worker(true);
        let first = pending(&worker, "one");
        let second = pending(&worker, "two");
        assert!(first.receipt.result.lock().unwrap().is_none());
        let mut start = Event::new(Event::START).saying("one");
        start.turn = 0;
        worker.on_event(start, false, 0).unwrap();
        first.wait_started().unwrap();
        assert!(second.receipt.result.lock().unwrap().is_none());
        let mut injected = Event::new(Event::INJECTED).saying("two");
        injected.turn = 0;
        injected.extra.insert("landed".into(), json!("next_turn"));
        worker.on_event(injected, false, 0).unwrap();
        second.wait_started().unwrap();
        worker.on_event(Event::new(Event::END), false, 0).unwrap();
        assert!(!worker.stopped.load(Ordering::SeqCst));
        assert_eq!(worker.state.lock().unwrap().pending.len(), 2);
        worker.on_event(Event::new(Event::END), true, 0).unwrap();
        assert!(worker.stopped.load(Ordering::SeqCst));
        assert!(worker.state.lock().unwrap().pending.is_empty());
    }

    #[test]
    fn retry_and_late_errors_preserve_receipts_and_failed_listeners_are_recreated() {
        let (_dir, runtime, connected, worker) = worker(false);
        let delivery = pending(&worker, "hello");
        let mut retry = Event::new(Event::ERROR);
        retry.kind = "crash".into();
        retry.error = "temporary".into();
        retry.extra.insert("willRetry".into(), json!(true));
        worker.on_event(retry, true, 1).unwrap();
        let mut late = Event::new(Event::ERROR);
        late.extra.insert("late".into(), json!(true));
        worker.on_event(late, true, 0).unwrap();
        assert!(delivery.receipt.result.lock().unwrap().is_none());
        worker.fail("socket lost");
        assert!(delivery.wait_started().is_err());
        assert!(worker.stopped.load(Ordering::SeqCst));
        let replacement = runtime
            .worker(&connected, "a", &SendOptions::default())
            .unwrap();
        assert!(!Arc::ptr_eq(&worker, &replacement));
        assert_eq!(replacement.session_id, worker.session_id);
        assert!(!replacement.stopped.load(Ordering::SeqCst));
    }

    #[test]
    fn retirement_rechecks_pending_work_after_waiting_for_send_lock() {
        let (_dir, _runtime, _connected, worker) = worker(true);
        let client = worker.client.lock().unwrap();
        let retiring = worker.clone();
        let retirement = thread::spawn(move || retiring.retire_if_idle().unwrap());
        let delivery = pending(&worker, "accepted while END was being handled");
        drop(client);
        assert!(!retirement.join().unwrap());
        assert!(!worker.stopped.load(Ordering::SeqCst));
        worker.stop(Some("test complete".into())).unwrap();
        assert!(delivery.wait_started().is_err());
    }

    #[test]
    fn restart_gate_rejects_active_nested_work_then_rejects_new_dispatches() {
        let (_dir, runtime, _connected, worker) = worker(false);
        let outer = runtime.activity().unwrap();
        let inner = runtime.activity().unwrap();
        assert!(!runtime.begin_restart_if_idle());
        drop(inner);
        assert!(!runtime.idle());
        drop(outer);
        let delivery = pending(&worker, "work");
        assert!(!runtime.begin_restart_if_idle());
        worker
            .on_event(Event::new(Event::START).saying("work"), false, 0)
            .unwrap();
        delivery.wait_started().unwrap();
        worker.on_event(Event::new(Event::END), true, 0).unwrap();
        assert!(runtime.begin_restart_if_idle());
        assert!(runtime
            .send(
                "test:org",
                None,
                "a",
                "too late",
                &SendOptions::default(),
                false
            )
            .is_err());
    }

    #[test]
    fn suggestions_require_new_messages_and_cooldown_and_archived_workers_retire() {
        let (_dir, _runtime, connected, worker) = worker(false);
        let mut record = worker.state.lock().unwrap().record.clone();
        let now = Utc::now();
        let cooldown = Duration::from_secs(60);
        record.new_messages = 9;
        assert!(!suggestion_due(&record, 10, cooldown, now));
        record.new_messages = 10;
        assert!(suggestion_due(&record, 10, cooldown, now));
        record.last_suggestion = Some(now);
        record.messages_at_suggestion = 10;
        record.new_messages = 20;
        assert!(!suggestion_due(
            &record,
            10,
            cooldown,
            now + chrono::Duration::seconds(59)
        ));
        assert!(suggestion_due(
            &record,
            10,
            cooldown,
            now + chrono::Duration::seconds(60)
        ));
        assert_eq!(
            interval(&serde_yaml::Value::String("2s".into()), &connected.cfg, "a").unwrap(),
            Duration::from_secs(2)
        );
        worker
            .state
            .lock()
            .unwrap()
            .record
            .archive(&connected.cfg.home, Some("old"), None, None)
            .unwrap();
        worker.on_event(Event::new(Event::END), true, 0).unwrap();
        assert!(worker.stopped.load(Ordering::SeqCst));
        assert_eq!(
            state::sessions(&connected.cfg.home, "a", true)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn disconnect_unhook_failure_still_removes_connection_and_capabilities() {
        let (_dir, runtime, connected, first) = worker(false);
        let mut cfg = connected.cfg.clone();
        runtime.disconnect("test:org").unwrap();
        assert!(first.stopped.load(Ordering::SeqCst));
        cfg.silicon.webhook = vec!["silicon-runtime-test-missing-app".into()];
        runtime.connect(cfg).unwrap();
        let connected = runtime.get("test:org").unwrap();
        let second = runtime
            .worker(&connected, "a", &SendOptions::default())
            .unwrap();
        let delivery = pending(&second, "will be cancelled");
        assert!(runtime.disconnect("test:org").is_err());
        assert!(runtime.get("test:org").is_err());
        assert!(runtime.caller(&second.capability).is_none());
        assert!(delivery.wait_started().is_err());
    }
}
