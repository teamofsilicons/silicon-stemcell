use crate::process::Starting;
use crate::Recover;
use crate::{
    auth,
    config::Config,
    eval, failure, flow, log_line_scoped,
    state::{self, Session},
};
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, TimeDelta, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use silicon_omni::{
    raw::{Client, Request},
    Ask, Chat, DaemonInfo, Event, Inference, PROTOCOL,
};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{symlink, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::process::{Child, Stdio};
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    mpsc, Arc, Condvar, Mutex, RwLock, TryLockError, Weak,
};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

/// How long a stop may wait for Omni to end a session; omnid is terminated either way.
const STOP_TIMEOUT: Duration = if cfg!(test) {
    Duration::from_secs(2)
} else {
    Duration::from_secs(10)
};
/// How long shutdown and disconnect wait for every session and Ting hook to stop together.
const STOP_BUDGET: Duration = Duration::from_secs(60);
/// The pause before a failed Ting batch is tried again.
const INBOX_RETRY: Duration = if cfg!(test) {
    Duration::from_secs(1)
} else {
    Duration::from_secs(60)
};
/// Session-addressed heartbeat targets are read from disk at most this often.
const TARGETS_TTL: Duration = Duration::from_secs(10);
/// Provider lines a worker keeps for the failure that usually follows them.
const TROUBLE_KEPT: usize = 200;
/// A background failure that keeps repeating unchanged is written again at most this often.
const REPEAT_MINUTES: i64 = 10;
/// heartbeats.json is written at most this often while heartbeats keep changing it.
const BEATS_WRITE: Duration = Duration::from_secs(1);
/// The failure-report key of one heartbeat target is this followed by its address.
const BEAT_REPORT: &str = "heartbeat for ";

#[derive(Clone, Debug)]
pub struct Caller {
    pub silicon: String,
    pub isi: String,
    pub session: Uuid,
    worker: Weak<Worker>,
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

#[derive(Debug)]
struct ReceiptPending;

impl std::fmt::Display for ReceiptPending {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("timed out after 60s awaiting provider START/INJECTED; the accepted delivery may still run")
    }
}

impl std::error::Error for ReceiptPending {}

impl SendReceipt {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            result: Mutex::new(None),
            changed: Condvar::new(),
            deadline: Instant::now() + Duration::from_secs(60),
        })
    }
    fn finish(&self, result: std::result::Result<(), String>) {
        let mut current = self.result.lock().recover();
        if current.is_none() {
            *current = Some(result);
            self.changed.notify_all();
        }
    }
    fn wait(&self) -> Result<()> {
        let result = self.result.lock().recover();
        let (result, _) = self
            .changed
            .wait_timeout_while(
                result,
                self.deadline.saturating_duration_since(Instant::now()),
                |result| result.is_none(),
            )
            .recover();
        match result.as_ref() {
            Some(Ok(())) => Ok(()),
            Some(Err(error)) => bail!("{error}"),
            None => Err(ReceiptPending.into()),
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

pub(crate) struct Activity<'a>(&'a AtomicUsize);
impl Drop for Activity<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

pub struct Connected {
    pub cfg: Config,
    pub app_settings: RwLock<crate::config::Silicon>,
    pub ting: crate::ting::Inbox,
    outbox: crate::outbox::Outbox,
    /// Accepted dispatches awaiting acknowledgement, keyed by their stable outbox run/slot.
    pending_outgoing: Mutex<HashMap<String, Sent>>,
    generation: Uuid,
    workers: Mutex<BTreeMap<Uuid, Arc<Worker>>>,
    pub enabled: AtomicBool,
    /// Heartbeat due times by address, mirrored in `.silicon/heartbeats.json`.
    beats: Mutex<BTreeMap<String, Beat>>,
    /// `beats` changed since heartbeats.json was last written.
    beats_changed: AtomicBool,
    /// Serializes writes of heartbeats.json, so the newest schedule is the one saved, and
    /// holds when it was last written.
    beats_file: Mutex<Option<Instant>>,
    /// Background failures already written, by key: the text and when it was written.
    reported: Mutex<HashMap<String, (String, DateTime<Utc>)>>,
}

/// One heartbeat's place on the wall clock. `every_seconds` is what `next` last evaluated
/// to, so a due time further away than that shows the clock went back. `next` is the
/// `heartbeat.next` it was worked out from: a reconnect with a different one works the
/// due time out again, so a shortened interval does not wait out the old one.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Beat {
    due: DateTime<Utc>,
    every_seconds: f64,
    #[serde(default)]
    next: String,
}

impl Beat {
    fn new(due: DateTime<Utc>, every: Duration, next: &str) -> Self {
        Self {
            due,
            every_seconds: every.as_secs_f64(),
            next: next.to_owned(),
        }
    }
    fn every(&self) -> TimeDelta {
        TimeDelta::milliseconds((self.every_seconds * 1000.) as i64)
    }
    /// A hand-edited or damaged entry is dropped rather than trusted.
    fn sane(&self) -> bool {
        self.every_seconds.is_finite() && (1. ..=315360000.).contains(&self.every_seconds)
    }
}

/// A send reached a session worker that was retired for inactivity. The session itself is
/// still active; a fresh worker resumes it.
#[derive(Debug)]
struct Retired(Uuid);

impl std::fmt::Display for Retired {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "session {} was retired for inactivity; send again to resume it",
            self.0
        )
    }
}

impl std::error::Error for Retired {}

struct Dispatch {
    id: Uuid,
    message: String,
    turn: Option<i64>,
    receipt: Arc<SendReceipt>,
    origin: Option<Caller>,
    /// Heartbeats coalesce: a target with an unfinished one skips the next instead of queuing it.
    heartbeat: bool,
}

struct WorkerState {
    record: Session,
    pending: Vec<Dispatch>,
    text: String,
    /// When the DNA is next refreshed and the interval that set it, on the wall clock so
    /// system sleep does not stretch it.
    next_dna: Option<(DateTime<Utc>, Duration)>,
    /// (provider, what it said) for the latest provider errors and stderr lines since the
    /// last END. A failure or provider removal quotes it, because the cause is usually said
    /// earlier. Only the newest TROUBLE_KEPT are held; silicon.log has every one.
    trouble: VecDeque<(String, String)>,
    /// How many older trouble lines were dropped to keep the list bounded.
    trouble_dropped: usize,
    /// Wall clock of the last send or event. Idle session workers retire after an hour.
    last_activity: DateTime<Utc>,
    /// Awake time of the last send or event, for the stall watchdog: a laptop asleep in the
    /// middle of a turn receives nothing, and must not fail that turn when it wakes.
    last_progress: Instant,
    /// Between a START or INJECTED and the END that leaves Omni idle.
    turn_open: bool,
}

impl WorkerState {
    fn new(record: Session) -> Self {
        Self {
            record,
            pending: Vec::new(),
            text: String::new(),
            next_dna: None,
            trouble: VecDeque::new(),
            trouble_dropped: 0,
            last_activity: Utc::now(),
            last_progress: Instant::now(),
            turn_open: false,
        }
    }

    fn touched(&mut self) {
        self.last_activity = Utc::now();
        self.last_progress = Instant::now();
    }

    fn keep_trouble(&mut self, provider: String, said: String) {
        self.trouble.push_back((provider, said));
        while self.trouble.len() > TROUBLE_KEPT {
            self.trouble.pop_front();
            self.trouble_dropped += 1;
        }
    }

    fn clear_trouble(&mut self) {
        self.trouble.clear();
        self.trouble_dropped = 0;
    }

    /// "earlier this turn:" and the provider lines kept for it, when there are any.
    fn quote_trouble(&self, provider: Option<&str>) -> String {
        let lines: Vec<&str> = self
            .trouble
            .iter()
            .filter(|(from, _)| provider.is_none_or(|provider| provider == from))
            .map(|(_, text)| text.as_str())
            .collect();
        let mut quoted = String::new();
        if lines.is_empty() {
            return quoted;
        }
        if provider.is_none() {
            quoted.push_str("\nearlier this turn:");
        }
        if self.trouble_dropped > 0 {
            quoted.push_str(&format!(
                "\n({} earlier provider lines are not repeated here; silicon.log has every one)",
                self.trouble_dropped
            ));
        }
        for line in lines {
            quoted.push('\n');
            quoted.push_str(line);
        }
        quoted
    }
}

/// The omnid a worker started, and where this run's output begins in its log.
struct Daemon {
    child: Child,
    log: PathBuf,
    start: u64,
}

pub struct Worker {
    connected: Weak<Connected>,
    runtime: Weak<Runtime>,
    state: Mutex<WorkerState>,
    /// Held only through initialization/send acceptance; never while waiting for a turn.
    client: Mutex<Option<Client>>,
    daemon: Mutex<Option<Daemon>>,
    capability: String,
    pub session_id: Uuid,
    stopped: AtomicBool,
    /// Stopped because it sat idle, not because the session ended: a send resumes it.
    retired: AtomicBool,
}

impl Connected {
    /// Write a background failure in full when it first happens, when its text changes,
    /// and at most every ten minutes while it keeps repeating unchanged.
    fn report(&self, key: &str, origin: &str, message: &str) {
        let now = Utc::now();
        {
            let mut reported = self.reported.lock().recover();
            if let Some((text, at)) = reported.get(key) {
                // abs(): a clock that went back must not silence the failure until it catches up.
                if text == message && (now - *at).num_minutes().abs() < REPEAT_MINUTES {
                    return;
                }
            }
            reported.insert(key.to_owned(), (message.to_owned(), now));
        }
        log_error(&self.cfg.home, Some(self.generation), origin, message);
    }

    /// The failure `key` stopped; the next one is written in full again.
    fn recovered(&self, key: &str) {
        self.reported.lock().recover().remove(key);
    }

    /// Change one heartbeat's due time. The scheduler writes the file within about a second.
    fn set_beat(&self, address: &str, beat: Beat) {
        self.beats.lock().recover().insert(address.to_owned(), beat);
        // After the insert: a write that clears this flag always sees the new entry.
        self.beats_changed.store(true, Ordering::SeqCst);
    }

    /// Write the heartbeat schedule when it changed, at most once per BEATS_WRITE unless
    /// `now` (disconnect and shutdown). Every fire, skip and reschedule changes it, and a
    /// thousand heartbeat targets must not each rewrite and fsync the whole file. A replaced
    /// connection no longer owns the file.
    fn write_beats(&self, now: bool) {
        let mut written = self.beats_file.lock().recover();
        if !self.enabled.load(Ordering::SeqCst)
            || (!now && written.is_some_and(|at| at.elapsed() < BEATS_WRITE))
            || !self.beats_changed.swap(false, Ordering::SeqCst)
        {
            return;
        }
        let beats = self.beats.lock().recover().clone();
        *written = Some(Instant::now());
        let path = beats_path(&self.cfg.home);
        match state::write_json(&path, &beats) {
            Ok(()) => self.recovered("heartbeat schedule file"),
            Err(error) => {
                // Tried again at the next write.
                self.beats_changed.store(true, Ordering::SeqCst);
                self.report(
                    "heartbeat schedule file",
                    "heartbeat",
                    &format!("saving the heartbeat schedule: {error:#}"),
                );
            }
        }
    }
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

    pub fn connect(self: &Arc<Self>, mut cfg: Config) -> Result<()> {
        let _activity = self.activity()?;
        crate::ting::Inbox::new(&cfg)?;
        let id = cfg
            .silicon
            .id
            .as_deref()
            .ok_or_else(|| anyhow!("silicon.id missing in {}", cfg.path.display()))?
            .to_owned();
        {
            let silicons = self.silicons.read().recover();
            if silicons.contains_key(&id) {
                bail!("{id} is already connected");
            }
            if let Some(other) = silicons
                .values()
                .find(|s| s.cfg.path == cfg.path || s.cfg.home == cfg.home)
            {
                bail!(
                    "{} is already connected from {} with SILICON_HOME {}; {id} would share its {}",
                    other.cfg.silicon.id.as_deref().unwrap_or_default(),
                    other.cfg.path.display(),
                    other.cfg.home.display(),
                    if other.cfg.path == cfg.path {
                        "YAML file"
                    } else {
                        "SILICON_HOME"
                    }
                );
            }
        }
        state::private_dir(&cfg.home.join(".silicon"))?;
        state::write_json(
            &cfg.home.join(".silicon/org.json"),
            &cfg.silicon.silicon_org,
        )?;
        cfg.generation = Uuid::new_v4();
        crate::telemetry::register(&cfg);
        let preparation = (|| -> Result<()> {
            for (index, script) in cfg.silicon.setup.iter().enumerate() {
                crate::progress::step(
                    &cfg.home,
                    Some(cfg.generation),
                    &format!("Running setup step {}", index + 1),
                    &format!("Completed setup step {}", index + 1),
                    || eval::setup(script, &environment(&cfg), &cfg.home),
                )
                .with_context(|| {
                    format!("silicon.setup[{index}] failed; connection was not started")
                })?;
            }
            let apps = cfg.silicon.managed_apps();
            crate::apps::install_all(&cfg.home, &apps, cfg.generation)?;
            // Apps only the registry lists never stop a connection, so `si auth remove`
            // stays reachable for one that can no longer be installed.
            crate::apps::install_registered(&cfg.home, &apps, cfg.generation)?;
            auth::ensure_all_scoped(
                &cfg.home,
                &id,
                cfg.silicon.org_id.as_deref().unwrap_or_default(),
                cfg.silicon.token.as_deref().unwrap_or_default(),
                &cfg.silicon.managed_apps(),
                cfg.generation,
            )?;
            // One app that rejects its configuration is logged in full and does not keep the
            // Silicon offline.
            auth::configure_all(&cfg.home, &cfg.silicon.app_configs, cfg.generation);
            Ok(())
        })();
        if let Err(error) = preparation {
            // The whole chain, with the token and app config values registered above masked.
            let message = failure::mask(&cfg.home, &format!("{error:#}"), &[]);
            crate::telemetry::unregister(&cfg.home);
            bail!("{message}");
        }
        self.connect_prepared(cfg)
    }

    fn connect_prepared(self: &Arc<Self>, mut cfg: Config) -> Result<()> {
        let id = cfg.silicon.id.clone().context("silicon.id missing")?;
        let ting = crate::ting::Inbox::new(&cfg)?;
        let outbox = crate::outbox::Outbox::new(&cfg);
        outbox.reconcile(&ting.pending_ids()?)?;
        let app_settings = RwLock::new(cfg.silicon.clone());
        // Flows belong to the source file and are reloaded for every accepted batch.
        cfg.flow = serde_yaml::Value::Null;
        cfg.functions = serde_yaml::Value::Null;
        log_line_scoped(
            &cfg.home,
            Some(cfg.generation),
            "runtime",
            "interpreter",
            &format!("connected {id}"),
        )?;
        let beats = load_beats(&cfg.home, cfg.generation);
        self.silicons.write().recover().insert(
            id,
            Arc::new(Connected {
                generation: cfg.generation,
                cfg,
                app_settings,
                ting,
                outbox,
                pending_outgoing: Mutex::new(HashMap::new()),
                workers: Mutex::new(BTreeMap::new()),
                enabled: AtomicBool::new(true),
                beats: Mutex::new(beats),
                beats_changed: AtomicBool::new(false),
                beats_file: Mutex::new(None),
                reported: Mutex::new(HashMap::new()),
            }),
        );
        Ok(())
    }

    pub fn disconnect(&self, id: &str) -> Result<()> {
        let _activity = self.activity()?;
        let connected = self
            .silicons
            .read()
            .recover()
            .get(id)
            .cloned()
            .ok_or_else(|| anyhow!("unknown silicon: {id}"))?;
        // The last word on the heartbeat schedule, while this connection still owns the file.
        connected.write_beats(true);
        connected.enabled.store(false, Ordering::SeqCst);
        let workers: Vec<_> = connected
            .workers
            .lock()
            .recover()
            .values()
            .cloned()
            .collect();
        // Sessions and the Ting hook stop together, so wedged omnids cost one timeout, not one each.
        let mut tasks: Vec<Task> = workers
            .into_iter()
            .map(|worker| -> Task {
                (
                    format!("stopping session {}", worker.session_id),
                    Box::new(move || worker.stop(None)),
                )
            })
            .collect();
        let hook = connected.clone();
        tasks.push((
            "removing the Ting webhook".into(),
            Box::new(move || hook.ting.unhook(&hook.cfg)),
        ));
        let mut errors = Vec::new();
        for (what, outcome) in in_parallel(tasks, STOP_BUDGET) {
            match outcome {
                Some(Ok(())) => {}
                Some(Err(error)) => errors.push(format!("{what}: {error:#}")),
                None => errors.push(format!(
                    "{what}: still running after {}s; it continues in the background",
                    STOP_BUDGET.as_secs()
                )),
            }
        }
        self.callers
            .write()
            .recover()
            .retain(|_, c| c.silicon != id);
        self.silicons.write().recover().remove(id);
        if let Err(error) = log_line_scoped(
            &connected.cfg.home,
            Some(connected.cfg.generation),
            "runtime",
            "interpreter",
            "disconnected",
        ) {
            errors.push(format!("recording the disconnect: {error:#}"));
        }
        crate::telemetry::unregister(&connected.cfg.home);
        if !errors.is_empty() {
            bail!("{id} was disconnected with errors:\n{}", errors.join("\n"));
        }
        Ok(())
    }

    pub fn caller(&self, token: &str) -> Option<Caller> {
        self.callers.read().recover().get(token).cloned()
    }
    pub fn get(&self, id: &str) -> Result<Arc<Connected>> {
        self.silicons
            .read()
            .recover()
            .get(id)
            .cloned()
            .filter(|s| s.enabled.load(Ordering::SeqCst))
            .ok_or_else(|| anyhow!("silicon is not connected: {id}"))
    }

    pub(crate) fn caller_connection(&self, caller: &Caller) -> Result<Arc<Connected>> {
        let worker = caller
            .worker
            .upgrade()
            .ok_or_else(|| anyhow!("ISI session ended"))?;
        let connected = worker
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        if worker.stopped.load(Ordering::SeqCst) || !connected.enabled.load(Ordering::SeqCst) {
            bail!("ISI capability belongs to an ended session or disconnected silicon");
        }
        Ok(connected)
    }

    pub(crate) fn session_caller(&self, connected: &Connected, session: Uuid) -> Result<Caller> {
        let workers = connected.workers.lock().recover();
        let worker = workers
            .get(&session)
            .ok_or_else(|| anyhow!("current session {session} not found"))?;
        self.caller(&worker.capability)
            .ok_or_else(|| anyhow!("current session {session} ended"))
    }

    pub fn event(self: &Arc<Self>, id: &str, request: Value) -> Result<Value> {
        let connected = self.get(id)?;
        let run = Uuid::new_v4().to_string();
        let result = self.event_connected(&connected, &run, request, 1)?;
        connected.outbox.complete(&run)?;
        Ok(result)
    }

    /// Run the flow for one Ting batch. `attempt` counts tries of the same head batch: its
    /// full request is written to silicon.log on the first, and a one-line note after that,
    /// so a batch blocked for a weekend does not copy itself into the log every minute.
    fn event_connected(
        self: &Arc<Self>,
        connected: &Arc<Connected>,
        run: &str,
        request: Value,
        attempt: u32,
    ) -> Result<Value> {
        let _activity = self.activity()?;
        if !connected.enabled.load(Ordering::SeqCst) {
            bail!("silicon disconnected");
        }
        let cfg = &connected.cfg;
        log_line_scoped(
            &cfg.home,
            Some(cfg.generation),
            "event",
            "webhook",
            &if attempt <= 1 {
                request.to_string()
            } else if !ting_ids(&request).is_empty() {
                format!(
                    "retrying the Ting batch of tings {} (attempt {attempt}); its full request was written on the first attempt",
                    ting_ids(&request).join(", ")
                )
            } else {
                format!("retrying webhook request {run} (attempt {attempt}); its full request was written on the first attempt")
            },
        )?;
        let mut env = environment(cfg);
        env["request"] = request;
        let (steps, functions) = cfg
            .load_program()
            .with_context(|| format!("loading the flow from {}", cfg.path.display()))?;
        let mut slot = 0;
        flow::execute_with_functions(
            &steps,
            &functions,
            env,
            &cfg.home,
            "interpreter",
            |delivery| {
                let result = connected.outbox.deliver(
                    run,
                    slot,
                    delivery,
                    cfg.silicon.max_retries,
                    |key, delivery| self.deliver_outgoing(connected, key, delivery),
                    || self.delivery_ready(connected),
                );
                slot += 1;
                result
            },
        )?;
        Ok(json!({"status":"ok","event_id":run}))
    }

    fn delivery_ready(&self, connected: &Connected) -> Result<()> {
        if self.stopping.load(Ordering::SeqCst) {
            return Err(flow::Interrupted("interpreter is stopping".into()).into());
        }
        if !connected.enabled.load(Ordering::SeqCst) {
            return Err(flow::Interrupted("silicon disconnected".into()).into());
        }
        Ok(())
    }

    /// Actual provider delivery, bypassing the public send's outbox recovery hook.
    fn deliver_outgoing(
        self: &Arc<Self>,
        connected: &Arc<Connected>,
        key: &str,
        delivery: &flow::Delivery,
    ) -> Result<()> {
        self.delivery_ready(connected)?;
        // Outbox serializes this callback. Do not hold the receipt-map lock while sending
        // or waiting: provider callbacks and session teardown must remain able to progress.
        let pending = connected.pending_outgoing.lock().recover().remove(key);
        let sent = match pending {
            Some(sent) => Ok(sent),
            None => self.send_marked(
                connected,
                None,
                &delivery.isi,
                &delivery.message,
                &SendOptions {
                    id: delivery.session_id.clone(),
                    title: delivery.session_id.clone(),
                    new: true,
                    ..Default::default()
                },
                false,
                false,
            ),
        };
        let result = sent.and_then(|sent| {
            let result = sent.wait_started();
            if result
                .as_ref()
                .is_err_and(|error| error.is::<ReceiptPending>())
            {
                connected
                    .pending_outgoing
                    .lock()
                    .recover()
                    .insert(key.to_owned(), sent);
            }
            result
        });
        result.map_err(|error| match self.delivery_ready(connected) {
            Err(interrupted) => error.context(flow::Interrupted(format!("{interrupted:#}"))),
            Ok(()) if error.is::<ReceiptPending>() => error.context(crate::outbox::AwaitingReceipt),
            Ok(()) => error,
        })
    }

    pub fn start_inbox(self: &Arc<Self>, connected: &Arc<Connected>) {
        let runtime = Arc::downgrade(self);
        let weak = Arc::downgrade(connected);
        let started = thread::Builder::new()
            .name(format!(
                "ting inbox {}",
                connected.cfg.silicon.id.as_deref().unwrap_or_default()
            ))
            .spawn(move || {
                // The batch at the head of the inbox and how many times it has been tried.
                let mut head: Option<(String, u32)> = None;
                loop {
                    thread::sleep(Duration::from_millis(200));
                    let (Some(runtime), Some(connected)) = (runtime.upgrade(), weak.upgrade())
                    else {
                        break;
                    };
                    if runtime.stopping.load(Ordering::SeqCst)
                        || !connected.enabled.load(Ordering::SeqCst)
                    {
                        break;
                    }
                    // A panic in one batch must not end Ting processing for this Silicon.
                    let result = catch_unwind(AssertUnwindSafe(|| -> Result<()> {
                        #[cfg(test)]
                        tests::injected_inbox_panic(&connected.cfg.home);
                        let _activity = runtime.activity()?;
                        let mut completed = None;
                        connected.ting.process_identified(|run, request| {
                            let attempt = next_attempt(&mut head, run);
                            runtime.event_connected(&connected, run, request, attempt)?;
                            completed = Some(run.to_owned());
                            Ok(())
                        })?;
                        if let Some(run) = completed {
                            connected.outbox.complete(&run)?;
                            head = None;
                        }
                        Ok(())
                    }))
                    .unwrap_or_else(|panic| {
                        Err(anyhow!(
                            "the flow panicked: {}",
                            failure::panic_message(&*panic)
                        ))
                    });
                    let Err(error) = result else {
                        connected.recovered("ting inbox");
                        continue;
                    };
                    let message = format!("pending Ting flow: {error:#}");
                    if flow::interrupted(&error) {
                        // The flow logged the refused send as an error where it happened;
                        // keeping the batch for the next connection is not another failure.
                        if let Err(failed) = log_line_scoped(
                            &connected.cfg.home,
                            Some(connected.cfg.generation),
                            "runtime",
                            "ting",
                            &message,
                        ) {
                            crate::stderr_line(&format!(
                                "{failed:#}; the entry it was recording: {message}"
                            ));
                        }
                    } else {
                        // Written in full when it first happens or changes, not every minute.
                        connected.report("ting inbox", "ting", &message);
                    }
                    // Keep failed work durable. Re-read the flow on retry so edits can repair it.
                    for _ in 0..INBOX_RETRY.as_secs() {
                        if runtime.stopping.load(Ordering::SeqCst)
                            || !connected.enabled.load(Ordering::SeqCst)
                        {
                            return;
                        }
                        thread::sleep(Duration::from_secs(1));
                    }
                }
            });
        if let Err(error) = started {
            log_error(
                &connected.cfg.home,
                Some(connected.cfg.generation),
                "ting",
                &format!("could not start the Ting inbox thread, so accepted Ting batches wait until this Silicon reconnects or the interpreter restarts: {error}"),
            );
        }
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
        let connected = self.get(id)?;
        self.send_connected(&connected, caller, target, message, options, control)
    }

    pub(crate) fn send_connected(
        self: &Arc<Self>,
        connected: &Arc<Connected>,
        caller: Option<&Caller>,
        target: &str,
        message: &str,
        options: &SendOptions,
        control: bool,
    ) -> Result<Sent> {
        self.validate_send(connected, caller, target, options)?;
        self.recover_outgoing(connected, target, options)?;
        self.send_marked(connected, caller, target, message, options, control, false)
    }

    fn recover_outgoing(
        self: &Arc<Self>,
        connected: &Arc<Connected>,
        target: &str,
        options: &SendOptions,
    ) -> Result<()> {
        if !options.archived {
            let delivery = flow::Delivery {
                isi: target.to_owned(),
                session_id: options.id.clone(),
                message: String::new(),
            };
            if !connected.outbox.flush(
                &delivery,
                connected.cfg.silicon.max_retries,
                |key, pending| self.deliver_outgoing(connected, key, pending),
                || self.delivery_ready(connected),
            )? {
                bail!("{target} is still unavailable; earlier flow messages remain stashed for this session");
            }
        }
        Ok(())
    }

    fn validate_send(
        &self,
        connected: &Arc<Connected>,
        caller: Option<&Caller>,
        target: &str,
        options: &SendOptions,
    ) -> Result<()> {
        if let Some(caller) = caller {
            if !Arc::ptr_eq(connected, &self.caller_connection(caller)?) {
                bail!("ISI capability belongs to another silicon connection");
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
        let isi = connected
            .cfg
            .isi
            .get(target)
            .ok_or_else(|| anyhow!("unknown isi: {target}"))?;
        if isi.primary_send_mode.as_deref() == Some("session") && options.id.is_none() {
            bail!("{target} uses session addressing; session_id/--id is required");
        }
        Ok(())
    }

    /// [`Runtime::send_connected`], saying whether the message is a heartbeat.
    #[allow(clippy::too_many_arguments)]
    fn send_marked(
        self: &Arc<Self>,
        connected: &Arc<Connected>,
        caller: Option<&Caller>,
        target: &str,
        message: &str,
        options: &SendOptions,
        control: bool,
        heartbeat: bool,
    ) -> Result<Sent> {
        let _activity = self.activity()?;
        self.validate_send(connected, caller, target, options)?;
        let worker = self.worker(connected, target, options)?;
        match worker.send_as(message, caller.cloned(), control, heartbeat) {
            // The worker retired for inactivity between the lookup and the send; the session
            // is still active, and a fresh worker resumes it.
            Err(error) if error.is::<Retired>() => self
                .worker(connected, target, options)?
                .send_as(message, caller.cloned(), control, heartbeat),
            sent => sent,
        }
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
        // Global ephemeral work is use-and-throw: never reused, never read back from disk.
        let reusable = !options.archived && !(ephemeral && !by_session);
        let live = |workers: &BTreeMap<Uuid, Arc<Worker>>| {
            workers
                .values()
                .find(|w| {
                    let state = w.state.lock().recover();
                    !w.stopped.load(Ordering::SeqCst)
                        && state.record.isi == target
                        && state.record.archived_at.is_none()
                        && (!by_session || options.id.as_deref() == Some(state.record.id.as_str()))
                })
                .cloned()
        };
        {
            let workers = connected.workers.lock().recover();
            if !connected.enabled.load(Ordering::SeqCst) {
                bail!("silicon disconnected");
            }
            if reusable {
                if let Some(worker) = live(&workers) {
                    return Ok(worker);
                }
            }
        }
        // Disk reads and the credential check below run without `workers`: an app CLI that
        // hangs must not stop every other send, list and shutdown of this Silicon.
        let scan = if reusable || options.archived {
            state::scan_sessions(&connected.cfg.home, target, options.archived)?
        } else {
            state::Scan {
                sessions: Vec::new(),
                skipped: Vec::new(),
            }
        };
        let saved = scan.sessions.clone();
        let mut creating = false;
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
                        bail!(
                            "session {target}:{} does not exist; pass --new to create it",
                            options.id.as_deref().unwrap_or_default()
                        );
                    }
                    // A record that could not be loaded may be this very session.
                    scan.complete(&format!("start a new {target} session"))?;
                    creating = true;
                    Session::new(
                        target,
                        if by_session {
                            options.id.as_deref()
                        } else {
                            None
                        },
                        options.title.as_deref().unwrap_or(""),
                        false,
                        false,
                    )
                }
            }
        } else {
            if by_session && options.title.is_none() {
                bail!("new ephemeral session requires --title");
            }
            creating = true;
            Session::new(
                target,
                if by_session {
                    options.id.as_deref()
                } else {
                    None
                },
                options.title.as_deref().unwrap_or(""),
                true,
                // Only global ephemeral work is use-and-throw.
                !by_session,
            )
        };
        if !creating {
            if let Some(worker) = connected
                .workers
                .lock()
                .recover()
                .get(&record.session_id)
                .filter(|w| !w.stopped.load(Ordering::SeqCst))
            {
                return Ok(worker.clone());
            }
        }
        if creating {
            let silicon = &connected.cfg.silicon;
            auth::ensure_all_scoped(
                &connected.cfg.home,
                silicon.id.as_deref().unwrap_or_default(),
                silicon.org_id.as_deref().unwrap_or_default(),
                silicon.token.as_deref().unwrap_or_default(),
                &connected.app_settings.read().recover().managed_apps(),
                connected.cfg.generation,
            )
            .with_context(|| {
                format!("checking app credentials before starting a {target} session")
            })?;
        }
        let mut workers = connected.workers.lock().recover();
        if !connected.enabled.load(Ordering::SeqCst) {
            bail!("silicon disconnected");
        }
        // Another thread may have opened the same session while the lock was released.
        if reusable {
            if let Some(worker) = live(&workers) {
                return Ok(worker);
            }
        }
        if let Some(worker) = workers
            .get(&record.session_id)
            .filter(|w| !w.stopped.load(Ordering::SeqCst))
        {
            return Ok(worker.clone());
        }
        // An end or archive can move a session read above; it must not be brought back.
        if !creating && !record.path(&connected.cfg.home).exists() {
            bail!(
                "session {target}:{} was ended or archived while it was being opened",
                record.id
            );
        }
        let session_id = record.session_id;
        let capability = Uuid::new_v4().to_string();
        let worker = Arc::new(Worker {
            connected: Arc::downgrade(connected),
            runtime: Arc::downgrade(self),
            state: Mutex::new(WorkerState::new(record)),
            client: Mutex::new(None),
            daemon: Mutex::new(None),
            capability: capability.clone(),
            session_id,
            stopped: AtomicBool::new(false),
            retired: AtomicBool::new(false),
        });
        self.callers.write().recover().insert(
            capability,
            Caller {
                silicon: connected.cfg.silicon.id.clone().unwrap_or_default(),
                isi: target.into(),
                session: session_id,
                worker: Arc::downgrade(&worker),
            },
        );
        workers.insert(session_id, worker.clone());
        Ok(worker)
    }

    pub fn list(&self, id: &str, target: &str, archived: bool) -> Result<Vec<Session>> {
        let connected = self.get(id)?;
        self.list_connected(&connected, target, archived)
    }

    pub(crate) fn list_connected(
        &self,
        connected: &Connected,
        target: &str,
        archived: bool,
    ) -> Result<Vec<Session>> {
        if !connected.enabled.load(Ordering::SeqCst) {
            bail!("silicon disconnected");
        }
        if !connected.cfg.isi.contains_key(target) {
            bail!("unknown isi: {target}");
        }
        let mut records: BTreeMap<_, _> = state::sessions(&connected.cfg.home, target, archived)?
            .into_iter()
            .map(|s| (s.session_id, s))
            .collect();
        for worker in connected.workers.lock().recover().values() {
            let record = worker.state.lock().recover().record.clone();
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
        self.show_connected(&connected, target, session)
    }

    pub(crate) fn show_connected(
        &self,
        connected: &Connected,
        target: &str,
        session: Option<&str>,
    ) -> Result<Value> {
        let mut records = self.list_connected(connected, target, false)?;
        if session.is_some() {
            records.extend(self.list_connected(connected, target, true)?);
        }
        let record = records
            .iter()
            .find(|r| {
                session.is_none()
                    || session == Some(r.id.as_str())
                    || session == Some(r.session_id.to_string().as_str())
            })
            .ok_or_else(|| match session {
                Some(session) => anyhow!("session {session} not found for {target}"),
                None => anyhow!("{target} has no active session"),
            })?;
        let log = connected
            .cfg
            .home
            .join(".silicon/sessions/events")
            .join(format!("{}.jsonl", record.session_id));
        // The log rotates; right after a rotation the older copies fill in the history.
        let lines = state::events(&log, 100)
            .with_context(|| format!("reading the event history of session {}", record.id))?;
        // A torn line (a full disk, a power cut) must not hide the rest of the history; it is
        // shown beside it, whole, with the parser's reason.
        let mut events = Vec::new();
        let mut unreadable = Vec::new();
        for event in lines {
            match (event.get("unparsable"), event.get("error")) {
                (Some(line), Some(error)) if event.as_object().is_some_and(|e| e.len() == 2) => {
                    unreadable.push(json!({"line": line, "error": error}))
                }
                _ => events.push(event),
            }
        }
        let mut shown = json!({"session":record,"events":events});
        if !unreadable.is_empty() {
            shown["unreadable"] = json!({
                "count": unreadable.len(),
                "file": log,
                "lines": unreadable,
            });
        }
        Ok(shown)
    }

    pub fn end(&self, id: &str, target: &str, session: Option<&str>) -> Result<()> {
        let connected = self.get(id)?;
        self.end_connected(&connected, target, session)
    }

    pub(crate) fn end_connected(
        &self,
        connected: &Connected,
        target: &str,
        session: Option<&str>,
    ) -> Result<()> {
        let _activity = self.activity()?;
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
            let workers = connected.workers.lock().recover();
            if !connected.enabled.load(Ordering::SeqCst) {
                bail!("silicon disconnected");
            }
            for mut record in state::sessions(&connected.cfg.home, target, false)? {
                if matches(&record) && !workers.contains_key(&record.session_id) {
                    record.archive(&connected.cfg.home, None, None, None)?;
                    ended += 1;
                }
            }
            workers
                .values()
                .filter(|worker| matches(&worker.state.lock().recover().record))
                .cloned()
                .collect::<Vec<_>>()
        };
        let mut loaded: VecDeque<_> = loaded.into();
        while let Some(worker) = loaded.pop_front() {
            let mut client = worker.client.lock().recover();
            if !connected.enabled.load(Ordering::SeqCst) {
                bail!("silicon disconnected");
            }
            if worker.stopped.load(Ordering::SeqCst) {
                // It stopped while this end waited for it: retired for inactivity, failed, or
                // ended by another request. An active session is ended all the same.
                drop(client);
                ended += self.end_stopped(connected, &worker, &matches, &mut loaded)?;
                continue;
            }
            {
                let mut state = worker.state.lock().recover();
                if !matches(&state.record) {
                    continue;
                }
                if !state.record.disposable {
                    state
                        .record
                        .archive(&connected.cfg.home, None, None, None)?;
                }
            }
            worker.stop_locked(&mut client, Some("session ended".into()))?;
            ended += 1;
        }
        if ended == 0 {
            match session {
                Some(session) => bail!("no active session {session} for {target}"),
                None => bail!("no active session for {target}"),
            }
        }
        Ok(())
    }

    /// End the session of `worker`, which stopped while an end waited for it. A send may
    /// have opened the session again in a new worker since; that one is queued to be ended
    /// next. Otherwise its record, while still active, is archived, under `workers` so no
    /// send restores it meanwhile. Returns how many sessions this ended.
    fn end_stopped(
        &self,
        connected: &Connected,
        worker: &Worker,
        matches: &dyn Fn(&Session) -> bool,
        queue: &mut VecDeque<Arc<Worker>>,
    ) -> Result<usize> {
        let workers = connected.workers.lock().recover();
        if !connected.enabled.load(Ordering::SeqCst) {
            bail!("silicon disconnected");
        }
        if let Some(next) = workers
            .get(&worker.session_id)
            .filter(|next| !next.stopped.load(Ordering::SeqCst))
        {
            queue.push_back(next.clone());
            return Ok(0);
        }
        let isi = worker.state.lock().recover().record.isi.clone();
        let mut ended = 0;
        for mut record in state::sessions(&connected.cfg.home, &isi, false)? {
            if record.session_id == worker.session_id && matches(&record) {
                record.archive(&connected.cfg.home, None, None, None)?;
                ended += 1;
            }
        }
        Ok(ended)
    }

    pub fn new_session(self: &Arc<Self>, caller: &Caller, options: &NewSession) -> Result<Session> {
        let connected = self.caller_connection(caller)?;
        self.new_session_connected(&connected, caller, options)
    }

    pub(crate) fn new_session_connected(
        self: &Arc<Self>,
        connected: &Arc<Connected>,
        caller: &Caller,
        options: &NewSession,
    ) -> Result<Session> {
        let _activity = self.activity()?;
        let worker = caller.worker.upgrade().ok_or_else(|| {
            anyhow!(
                "current session {} of {} not found",
                caller.session,
                caller.isi
            )
        })?;
        let record = worker.state.lock().recover().record.clone();
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
            let _workers = connected.workers.lock().recover();
            if !connected.enabled.load(Ordering::SeqCst) || worker.stopped.load(Ordering::SeqCst) {
                bail!("silicon disconnected");
            }
            let mut current = worker.state.lock().recover();
            if current.record.archived_at.is_some() {
                bail!("the current session is already archived");
            }
            let archived = state::scan_sessions(&connected.cfg.home, &caller.isi, true)?;
            archived.complete(&format!(
                "archive as {}, since an unreadable archived {} record may already use that ID",
                options.id, caller.isi
            ))?;
            if archived.sessions.iter().any(|s| s.id == options.id) {
                bail!(
                    "archive id {} already exists for {}",
                    options.id,
                    caller.isi
                );
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
            connected,
            &caller.isi,
            &SendOptions {
                id: by_session.then_some(record.id),
                new: true,
                ..Default::default()
            },
        )?;
        // New sessions are fully initialized (auth/DNA/Omni) before this CLI returns.
        {
            let mut client = next.client.lock().recover();
            // A concurrent end or disconnect may have stopped the successor already.
            if next.stopped.load(Ordering::SeqCst) || !connected.enabled.load(Ordering::SeqCst) {
                bail!("silicon disconnected or session ended");
            }
            if client.is_none() {
                *client = Some(next.initialize()?);
            }
        }
        let new_record = next.state.lock().recover();
        new_record.record.save(&connected.cfg.home)?;
        Ok(new_record.record.clone())
    }

    pub(crate) fn authorize_target(
        &self,
        connected: &Connected,
        caller: &Caller,
        target: &str,
    ) -> Result<()> {
        if caller.isi != target
            && !connected
                .cfg
                .access
                .get(&caller.isi)
                .is_some_and(|targets| targets.iter().any(|n| n == target))
        {
            bail!("{} cannot access {target}", caller.isi);
        }
        Ok(())
    }

    pub fn start_scheduler(self: &Arc<Self>) {
        let runtime = Arc::downgrade(self);
        let started = thread::Builder::new()
            .name("heartbeat scheduler".into())
            .spawn(move || {
                let mut scheduler = Scheduler::default();
                let mut said: Option<(String, Instant)> = None;
                loop {
                    thread::sleep(Duration::from_millis(250));
                    let Some(runtime) = runtime.upgrade() else {
                        break;
                    };
                    let Ok(_activity) = runtime.activity() else {
                        break;
                    };
                    // One bad tick must not end every heartbeat for the life of the process.
                    if let Err(panic) = catch_unwind(AssertUnwindSafe(|| scheduler.tick(&runtime)))
                    {
                        let text = format!(
                            "the heartbeat scheduler panicked and continues with its next tick: {}",
                            failure::panic_message(&*panic)
                        );
                        // Said when it changes or every ten minutes, not four times a second.
                        if said.as_ref().is_none_or(|(before, at)| {
                            *before != text
                                || at.elapsed() >= Duration::from_secs(REPEAT_MINUTES as u64 * 60)
                        }) {
                            crate::stderr_line(&text);
                            said = Some((text, Instant::now()));
                        }
                    }
                }
            });
        if let Err(error) = started {
            crate::stderr_line(&format!(
                "could not start the heartbeat scheduler thread, so no heartbeat fires and no idle session retires until the interpreter restarts: {error}"
            ));
        }
    }

    /// Stop every idle session-addressed worker of `connected` that has had no send and no
    /// event for `limit`. Its omnid stops; its record stays active, so the next send resumes
    /// the same Omni session. Returns how many retired.
    pub(crate) fn retire_quiet(&self, connected: &Connected, limit: TimeDelta) -> usize {
        if !connected.enabled.load(Ordering::SeqCst) {
            return 0;
        }
        let workers: Vec<_> = connected
            .workers
            .lock()
            .recover()
            .values()
            .cloned()
            .collect();
        let mut retired = 0;
        for worker in workers {
            let record = worker.state.lock().recover().record.clone();
            let session_mode = connected
                .cfg
                .isi
                .get(&record.isi)
                .and_then(|isi| isi.primary_send_mode.as_deref())
                == Some("session");
            // Global persistent ISIs keep one warm session; archived answers retire at END.
            if !session_mode || record.archived_at.is_some() {
                continue;
            }
            let address = format!("{}:{}", record.isi, record.id);
            match worker.retire_if_quiet(limit) {
                Ok(None) => {}
                Ok(Some(idle)) => {
                    retired += 1;
                    note(
                        &connected.cfg.home,
                        Some(connected.cfg.generation),
                        "runtime",
                        &address,
                        &format!(
                            "retired idle session {} (Omni session {}) after {} minutes without a send or an event; its omnid is stopped and the next send resumes the same Omni session",
                            record.id,
                            record.session_id,
                            idle.num_minutes()
                        ),
                    );
                }
                Err(error) => log_error(
                    &connected.cfg.home,
                    Some(connected.cfg.generation),
                    &address,
                    &format!("retiring idle session {}: {error:#}", record.id),
                ),
            }
        }
        retired
    }

    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        let connections: Vec<_> = self.silicons.read().recover().values().cloned().collect();
        // Every Silicon's sessions and Ting hook stop together, within one overall budget,
        // so a wedged omnid or a stalled network cannot outlast a service manager's patience.
        let mut tasks: Vec<Task> = Vec::new();
        let mut owners = Vec::new();
        for connected in &connections {
            connected.write_beats(true);
            connected.enabled.store(false, Ordering::SeqCst);
            let id = connected.cfg.silicon.id.clone().unwrap_or_default();
            let hook = connected.clone();
            tasks.push((
                format!("{id}: removing the Ting webhook at shutdown"),
                Box::new(move || hook.ting.unhook(&hook.cfg)),
            ));
            owners.push((connected.clone(), "ting".to_owned(), None));
            let workers: Vec<_> = connected
                .workers
                .lock()
                .recover()
                .values()
                .cloned()
                .collect();
            for worker in workers {
                let session = worker.session_id;
                tasks.push((
                    format!("{id}: stopping session {session}"),
                    Box::new(move || worker.stop(None)),
                ));
                owners.push((connected.clone(), "shutdown".to_owned(), Some(session)));
            }
        }
        let mut unfinished = Vec::new();
        for ((what, outcome), (connected, origin, session)) in
            in_parallel(tasks, STOP_BUDGET).into_iter().zip(owners)
        {
            let message = match (outcome, session) {
                (Some(Ok(())), _) => continue,
                (Some(Err(error)), Some(session)) => {
                    format!("stopping session {session}: {error:#}")
                }
                (Some(Err(error)), None) => {
                    format!("removing the Ting webhook at shutdown: {error:#}")
                }
                (None, _) => {
                    unfinished.push(what.clone());
                    format!(
                        "{what} did not finish within {}s; shutdown went on without it",
                        STOP_BUDGET.as_secs()
                    )
                }
            };
            log_error(
                &connected.cfg.home,
                Some(connected.cfg.generation),
                &origin,
                &message,
            );
        }
        if !unfinished.is_empty() {
            crate::stderr_line(&format!(
                "shutdown waited {}s and went on without: {}",
                STOP_BUDGET.as_secs(),
                unfinished.join("; ")
            ));
        }
    }

    pub fn idle(&self) -> bool {
        self.activities.load(Ordering::SeqCst) == 0
            && self.silicons.read().recover().values().all(|connected| {
                connected
                    .workers
                    .lock()
                    .recover()
                    .values()
                    .all(|worker| worker.state.lock().recover().pending.is_empty())
            })
    }

    pub(crate) fn activity(&self) -> Result<Activity<'_>> {
        // The short reader lets callbacks dispatch recursively while a restart writer is waiting.
        let _gate = self.dispatch_gate.read().recover();
        if self.stopping.load(Ordering::SeqCst) {
            bail!("interpreter is stopping");
        }
        self.activities.fetch_add(1, Ordering::SeqCst);
        Ok(Activity(&self.activities))
    }

    pub fn begin_restart_if_idle(&self) -> bool {
        let _gate = self.dispatch_gate.write().recover();
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
        if self.stopped.load(Ordering::SeqCst) {
            bail!("session has ended");
        }
        let cfg = &connected.cfg;
        let record = self.state.lock().recover().record.clone();
        let omni_home = cfg
            .home
            .join(".silicon/omni")
            .join(record.session_id.to_string());
        // private_dir names the directory in its own errors.
        state::private_dir(&omni_home).context("creating this session's Omni home")?;
        // Unix socket paths have a 104-byte ceiling on macOS. The data stays under SILICON_HOME.
        let short_base = PathBuf::from(format!("/tmp/silicon-{}", unsafe { libc::getuid() }));
        state::private_dir(&short_base).context("creating the short Omni socket directory")?;
        let short_home = short_base.join(record.session_id.simple().to_string());
        match fs::read_link(&short_home) {
            Ok(existing) if existing != omni_home => bail!(
                "Omni socket alias {} points to {}, not this session's {}",
                short_home.display(),
                existing.display(),
                omni_home.display()
            ),
            Ok(_) => {}
            Err(unread) => symlink(&omni_home, &short_home).with_context(|| {
                format!(
                    "linking Omni socket alias {} -> {} (reading it first said: {unread})",
                    short_home.display(),
                    omni_home.display()
                )
            })?,
        }
        let socket = short_home.join("omnid.sock");
        let address = address_of(cfg, &record);
        // The DNA runs `!` scripts. Assemble it before omnid exists, so a slow script never
        // leaves a started daemon waiting on it.
        let dna = assemble_dna(cfg, &record.isi, &address)
            .with_context(|| format!("assembling the DNA for {address}"))?;
        // next_refresh can be Bash too, and is evaluated after each assembly.
        self.refresh_deadline()?;
        let daemon = std::env::var_os("OMNI_DAEMON")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("omnid"));
        let log = omni_home.join("daemon.log");
        // Every start of this session appends to one log; keep it bounded across restarts.
        if let Err(error) = crate::rotate(&log, crate::LOG_CAP, 2) {
            log_error(
                &cfg.home,
                Some(cfg.generation),
                &address,
                &format!("rotating omnid's log: {error:#}"),
            );
        }
        let lock = omni_home.join("omnid.pid");
        // The pid of an omnid left by an earlier interpreter run, once it has been stopped.
        let mut replaced: Option<u32> = None;
        let (child, start, client, chat) = loop {
            let daemon_log = OpenOptions::new()
                .create(true)
                .append(true)
                .mode(0o600)
                .open(&log)
                .with_context(|| format!("opening {}", log.display()))?;
            // Earlier runs of this session share the log; this run's output starts here.
            let start = daemon_log
                .metadata()
                .with_context(|| format!("reading the size of {}", log.display()))?
                .len();
            let mut child = crate::command(&daemon, &cfg.home)
                .current_dir(&cfg.home)
                .env("OMNI_HOME", &short_home)
                .env("OMNI_STDIO_LOGGED", "1")
                .env("SILICON_HOME", &cfg.home)
                .env(
                    "SILICON_ORG",
                    cfg.silicon.silicon_org.as_deref().unwrap_or_default(),
                )
                .env("ISI", &address)
                .env("TZ", cfg.silicon.timezone.as_deref().unwrap_or("UTC"))
                .env("SI_URL", &runtime.url)
                .env("SI_TOKEN", &self.capability)
                .env_remove("SILICON_TOKEN")
                .env_remove("SILICON_INTERPRETER_TOKEN")
                .stdin(Stdio::null())
                .stdout(
                    daemon_log
                        .try_clone()
                        .with_context(|| format!("sharing {} with omnid", log.display()))?,
                )
                .stderr(daemon_log)
                .spawn_retrying()
                .map_err(|error| {
                    failure::spawn(&cfg.home, &daemon.display().to_string(), &error)
                        .context("starting Omni (install Omni, or set OMNI_DAEMON to omnid's path)")
                })?;
            let setup = (|| -> Result<Started> {
                let inference = match own_daemon(&socket, &mut child, &lock)? {
                    Answer::Ours(inference) => inference,
                    Answer::Other(info, stale) => return Ok(Started::Stale(info, stale)),
                };
                let providers = select_providers(&cfg.silicon.inference_providers, &inference)?;
                let mut chat = inference
                    .load_or_create_session(record.session_id.to_string(), Some(providers));
                chat.cwd(&cfg.home).with_context(|| {
                    format!("setting Omni's working directory to {}", cfg.home.display())
                })?;
                let model = cfg.isi[&record.isi].model.as_deref().unwrap_or_default();
                chat.model(Ask::key(model))
                    .with_context(|| format!("asking Omni for model {model}"))?;
                chat.system_prompt(dna.clone())
                    .context("sending the system prompt (DNA) to Omni")?;
                chat.start_since(-1)
                    .with_context(|| format!("starting Omni session {}", record.session_id))?;
                Ok(Started::Ready(inference.raw().clone(), Box::new(chat)))
            })();
            match setup {
                Ok(Started::Ready(client, chat)) => break (child, start, client, *chat),
                Ok(Started::Stale(info, stale)) if replaced.is_none() => {
                    let said = daemon_output(&cfg.home, &log, start, &[&self.capability]);
                    let ours = child.id();
                    // It has exited already; this only reaps it.
                    let _ = terminate_child(child);
                    note(
                        &cfg.home,
                        Some(cfg.generation),
                        "runtime",
                        &address,
                        &format!(
                            "Omni socket {} is answered by omnid pid {} (started {}), not by pid {ours}, which this interpreter just started for the session and which exited because of it; that omnid was left by an earlier interpreter run and still carries that run's SI_TOKEN and SI_URL, so it is being stopped. The new omnid said: {said}",
                            socket.display(),
                            info.pid,
                            info.started
                        ),
                    );
                    stop_stale(cfg, &address, &socket, &lock, &info, stale).with_context(
                        || format!("starting Omni for {address}: stopping stale omnid pid {}", info.pid),
                    )?;
                    replaced = Some(info.pid);
                }
                Ok(Started::Stale(info, _)) => {
                    return Err(self.start_failed(
                        anyhow!(
                            "after stopping the stale omnid pid {}, Omni socket {} is answered by omnid pid {} rather than pid {} that this interpreter started",
                            replaced.unwrap_or_default(),
                            socket.display(),
                            info.pid,
                            child.id()
                        ),
                        child,
                        cfg,
                        &log,
                        start,
                        &address,
                    ))
                }
                Err(error) => {
                    return Err(self.start_failed(error, child, cfg, &log, start, &address))
                }
            }
        };
        if self.stopped.load(Ordering::SeqCst) {
            // An end or disconnect stopped this worker while Omni was starting.
            return Err(self.start_failed(
                anyhow!("the session ended while Omni was starting"),
                child,
                cfg,
                &log,
                start,
                &address,
            ));
        }
        *self.daemon.lock().recover() = Some(Daemon { child, log, start });
        let worker = self.clone();
        let listening = thread::Builder::new()
            .name(format!("isi listen {}", self.session_id))
            .spawn(move || worker.listen(chat));
        if let Err(error) = listening {
            let Some(Daemon { child, log, start }) = self.daemon.lock().recover().take() else {
                bail!("could not start the Omni event listener for {address}: {error}");
            };
            return Err(self.start_failed(
                anyhow!("could not start the Omni event listener thread: {error}"),
                child,
                cfg,
                &log,
                start,
                &address,
            ));
        }
        Ok(client)
    }

    /// Stop an omnid whose start failed and say everything about it: the cause first, then
    /// what omnid wrote this run.
    fn start_failed(
        &self,
        error: anyhow::Error,
        child: Child,
        cfg: &Config,
        log: &Path,
        start: u64,
        address: &str,
    ) -> anyhow::Error {
        // Child does not terminate on Drop. A failed setup must not leave an untracked daemon.
        let pid = child.id();
        let stopped = terminate_child(child);
        let mut said = format!(
            "{error:#}\n{}",
            daemon_output(&cfg.home, log, start, &[&self.capability])
        );
        if let Err(stop) = stopped {
            said.push_str(&format!(
                "\nomnid (pid {pid}) could not be stopped and may still be running: {stop:#}"
            ));
        }
        anyhow!(said).context(format!("starting Omni for {address}"))
    }

    fn send(
        self: &Arc<Self>,
        message: &str,
        origin: Option<Caller>,
        control: bool,
    ) -> Result<Sent> {
        self.send_as(message, origin, control, false)
    }

    fn send_as(
        self: &Arc<Self>,
        message: &str,
        origin: Option<Caller>,
        control: bool,
        heartbeat: bool,
    ) -> Result<Sent> {
        let runtime = self
            .runtime
            .upgrade()
            .ok_or_else(|| anyhow!("interpreter stopped"))?;
        let _activity = runtime.activity()?;
        let mut client = self.client.lock().recover();
        if self.stopped.load(Ordering::SeqCst) {
            if self.retired.load(Ordering::SeqCst) {
                return Err(anyhow::Error::new(Retired(self.session_id)));
            }
            bail!("session has ended");
        }
        if client.is_none() {
            match self.initialize() {
                Ok(ready) => *client = Some(ready),
                Err(error) => return Err(self.discard_disposable(&mut client, error)),
            }
        }
        if let Some(exit) = self.daemon_exit() {
            let error = anyhow!("Omni did not accept the message for session {}: its daemon had already exited\n{exit}", self.session_id);
            return Err(self.discard_disposable(&mut client, error));
        }
        let id = Uuid::new_v4();
        let receipt = SendReceipt::new();
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        // Name the sender while it is still in hand. A provider reports the tool
        // call that produced an ISI-to-ISI send well after the send itself has
        // landed, so the entry has to say who sent it rather than leaving the
        // reader to find a later line. Read from the Caller's own fields only:
        // locking the sending worker here would nest two session locks and can
        // deadlock when two ISIs send to each other at once.
        let from = origin.as_ref().map(|caller| {
            let session_addressed = connected
                .cfg
                .isi
                .get(&caller.isi)
                .and_then(|isi| isi.primary_send_mode.as_deref())
                == Some("session");
            if session_addressed {
                format!("{}:{}", caller.isi, caller.session)
            } else {
                caller.isi.clone()
            }
        });
        {
            let mut state = self.state.lock().recover();
            state.pending.push(Dispatch {
                id,
                message: message.into(),
                turn: None,
                receipt: receipt.clone(),
                origin,
                heartbeat,
            });
            state.record.status = "running".into();
            state.record.last = Utc::now();
            state.touched();
        }
        let result = client
            .as_ref()
            .unwrap()
            .send(&self.session_id.to_string(), message);
        if matches!(
            &result,
            Ok(false) | Err(silicon_omni::DaemonError::Daemon(_))
        ) {
            // "the omni daemon went away" means nothing without the daemon's own last words.
            let error = self.with_daemon_exit(match result {
                Err(error) => format!(
                    "Omni did not accept the message for session {}: {error}",
                    self.session_id
                ),
                _ => format!(
                    "Omni answered the send for session {} with accepted: false",
                    self.session_id
                ),
            });
            {
                let mut state = self.state.lock().recover();
                if let Some(pos) = state.pending.iter().position(|d| d.id == id) {
                    state.pending.remove(pos).receipt.finish(Err(error.clone()));
                }
            }
            return Err(self.discard_disposable(&mut client, anyhow!("{error}")));
        }
        if let Err(error) = result {
            // A lost/malformed transport acknowledgement does not prove rejection: Omni
            // may already be running the message. Its provider receipt remains authoritative.
            log_error(
                &connected.cfg.home,
                Some(connected.cfg.generation),
                &self.state.lock().recover().record.isi,
                &format!("Omni did not confirm acceptance for delivery {id}: {error}; retained its receipt because the message may still run"),
            );
        }
        let mut state = self.state.lock().recover();
        if !control {
            state.record.new_messages += 1;
        }
        if let Err(error) = state.record.save(&connected.cfg.home) {
            // Provider acceptance cannot be undone by a metadata failure. Keep its receipt
            // so delivery recovery never turns this bookkeeping failure into a second send.
            let error = error.context(format!(
                "{} accepted the message, but saving its session state failed",
                state.record.isi
            ));
            log_error(
                &connected.cfg.home,
                Some(connected.cfg.generation),
                &state.record.isi,
                &format!("{error:#}"),
            );
        }
        if let Err(error) = log_line_scoped(
            &connected.cfg.home,
            Some(connected.cfg.generation),
            "send",
            &state.record.isi,
            &match &from {
                Some(from) => format!("from {from}: {message}"),
                // Flow and interpreter sends have no ISI sender to name.
                None => message.to_owned(),
            },
        ) {
            log_error(
                &connected.cfg.home,
                Some(connected.cfg.generation),
                &state.record.isi,
                &format!("message accepted, but recording its send failed: {error:#}"),
            );
        }
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
                log_error(
                    &connected.cfg.home,
                    Some(connected.cfg.generation),
                    &name,
                    &format!("new session suggestion: {error:#}"),
                );
            }
        }
        Ok(sent)
    }

    /// Global ephemeral work is use-and-throw: a send that could not start or was refused
    /// leaves no worker, capability, omnid, Omni home or /tmp alias behind.
    fn discard_disposable(
        &self,
        client: &mut Option<Client>,
        error: anyhow::Error,
    ) -> anyhow::Error {
        let disposable = {
            let state = self.state.lock().recover();
            state.record.disposable
                && state.record.archived_at.is_none()
                && state.pending.is_empty()
        };
        if !disposable {
            return error;
        }
        // The caller reports `error`; stop_locked is given none so it is not logged twice.
        failure::also(
            error,
            self.stop_locked(client, None)
                .context("cleaning up the disposable session afterwards"),
        )
    }

    fn suggest_session(self: &Arc<Self>) -> Result<()> {
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let record = self.state.lock().recover().record.clone();
        let isi = &connected.cfg.isi[&record.isi];
        let Some(suggestion) = &isi.new_session_suggestion else {
            return Ok(());
        };
        let address = address_of(&connected.cfg, &record);
        let env = environment(&connected.cfg);
        let minimum = eval::value(
            &suggestion["min_new_messages"],
            &env,
            &connected.cfg.home,
            &address,
        )
        .context("evaluating new_session_suggestion.min_new_messages")?;
        let minimum = minimum.as_u64().filter(|count| *count > 0).ok_or_else(|| {
            anyhow!(
                "new_session_suggestion.min_new_messages must be a positive integer, got {minimum}"
            )
        })?;
        let cooldown = interval(&suggestion["cooldown_minutes"], &connected.cfg, &address)
            .context("evaluating new_session_suggestion.cooldown_minutes")?;
        let now = Utc::now();
        let message = {
            let state = self.state.lock().recover();
            if !suggestion_due(&state.record, minimum, cooldown, now) {
                return Ok(());
            }
            drop(state);
            let source = suggestion["suggestion_message"].as_str().ok_or_else(|| {
                anyhow!(
                    "new_session_suggestion.suggestion_message must be a string, got {}",
                    shown(&suggestion["suggestion_message"])
                )
            })?;
            eval::evaluate(source, &env, &connected.cfg.home, &address)
                .context("evaluating new_session_suggestion.suggestion_message")?
        };
        let previous = {
            let mut state = self.state.lock().recover();
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
                return Err(error.context("saving the suggestion time to session state"));
            }
            previous
        };
        if let Err(error) = self.send(&message, None, true) {
            let error = error.context("sending the new session suggestion");
            let mut state = self.state.lock().recover();
            if state.record.last_suggestion == Some(now) {
                state.record.last_suggestion = previous.0;
                state.record.messages_at_suggestion = previous.1;
                // Two independent failures: report both rather than the rollback alone.
                if let Err(rollback) = state.record.save(&connected.cfg.home) {
                    bail!("{error:#}\nrestoring the session state afterwards also failed: {rollback:#}");
                }
            }
            return Err(error);
        }
        log_line_scoped(
            &connected.cfg.home,
            Some(connected.cfg.generation),
            "suggestion",
            &address,
            &message,
        )?;
        Ok(())
    }

    fn listen(self: Arc<Self>, mut chat: Chat) {
        // A panic here must not leave a deaf worker that still looks alive: fail it, so the
        // next send starts a fresh one.
        if let Err(panic) = catch_unwind(AssertUnwindSafe(|| self.listen_loop(&mut chat))) {
            self.fail(&format!(
                "the Omni event listener for session {} panicked: {}",
                self.session_id,
                failure::panic_message(&*panic)
            ));
        }
        // The worker has already stopped or failed and said why; a dead daemon refusing
        // the detach changes nothing.
        let _ = chat.detach();
    }

    fn listen_loop(self: &Arc<Self>, chat: &mut Chat) {
        let stall = minutes_from_env("SILICON_TURN_STALL_MINUTES", 360);
        let mut last_event: Option<String> = None;
        while !self.stopped.load(Ordering::SeqCst) {
            match chat.next_event_timeout(Duration::from_secs(1)) {
                Ok(Some(event)) => {
                    let queued = chat.state().map_or(0, |state| state.queued);
                    let kind = event.event_type.clone();
                    last_event = serde_json::to_string(&event).ok();
                    if let Err(error) = self.on_event(event, chat.idle(), queued) {
                        if !self.winding_down() {
                            self.fail(&format!("handling Omni {kind} event: {error:#}"));
                        }
                        break;
                    }
                }
                Ok(None) => {
                    if chat.status() == "stopped" {
                        if !self.winding_down() {
                            let trouble = self.state.lock().recover().quote_trouble(None);
                            self.fail(&self.with_daemon_exit(format!(
                                "Omni session {} stopped before completion; Omni's last state: {}{trouble}",
                                self.session_id,
                                json!(chat.state()),
                            )));
                        }
                        break;
                    }
                    let in_turn = chat.state().is_some_and(|state| state.in_turn);
                    if let Some(stalled) = self.stall_report(
                        in_turn,
                        json!(chat.state()),
                        stall,
                        last_event.as_deref(),
                    ) {
                        self.fail(&stalled);
                        break;
                    }
                }
                Err(error) => {
                    if !self.winding_down() {
                        let trouble = self.state.lock().recover().quote_trouble(None);
                        self.fail(&self.with_daemon_exit(format!(
                            "Omni event stream for session {} failed: {error}{trouble}",
                            self.session_id,
                        )));
                    }
                    break;
                }
            }
            self.schedule_dna_refresh();
        }
    }

    /// An orderly stop is under way (shutdown, restart, disconnect, or this worker's own
    /// stop). Its stream ending then is expected, not a session failure.
    fn winding_down(&self) -> bool {
        self.stopped.load(Ordering::SeqCst)
            || self
                .runtime
                .upgrade()
                .is_none_or(|runtime| runtime.stopping.load(Ordering::SeqCst))
            || self
                .connected
                .upgrade()
                .is_none_or(|connected| !connected.enabled.load(Ordering::SeqCst))
    }

    /// The full picture when outstanding work has heard nothing from Omni for `limit`. A
    /// wedged provider must not hold the worker, idle detection and self-updates forever.
    fn stall_report(
        &self,
        in_turn: bool,
        omni: Value,
        limit: Duration,
        last_event: Option<&str>,
    ) -> Option<String> {
        let (quiet, pending, trouble) = {
            let state = self.state.lock().recover();
            (
                state.last_progress.elapsed(),
                state.pending.len(),
                state.quote_trouble(None),
            )
        };
        if quiet < limit || (!in_turn && pending == 0) {
            return None;
        }
        let waiting = if in_turn {
            "a turn was in progress".to_owned()
        } else {
            format!("{pending} accepted messages had not finished")
        };
        let message = format!(
            "no Omni event for session {} in {} minutes while {waiting} (SILICON_TURN_STALL_MINUTES is {}); stopping the worker so the next send starts the session again\nOmni's last state: {omni}\nlast event: {}\naccepted messages outstanding: {pending}{trouble}\n{}",
            self.session_id,
            quiet.as_secs() / 60,
            limit.as_secs() / 60,
            last_event.unwrap_or("none since this worker started"),
            self.daemon_report()
        );
        let home = self.connected.upgrade()?.cfg.home.clone();
        Some(failure::mask(&home, &message, &[&self.capability]))
    }

    fn on_event(self: &Arc<Self>, event: Event, omni_idle: bool, queued: usize) -> Result<()> {
        let runtime = self
            .runtime
            .upgrade()
            .ok_or_else(|| anyhow!("interpreter stopped"))?;
        // During an orderly shutdown or restart the event is still recorded, but it starts
        // no lifecycle work and fails nothing: the stop that follows says what happened.
        let activity = runtime.activity().ok();
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let cfg = &connected.cfg;
        let mut state = self.state.lock().recover();
        state.touched();
        let line = serde_json::to_string(&event)?;
        // Record-keeping that fails is reported on daemon.log; the turn carries on.
        if let Err(error) = log_line_scoped(
            &cfg.home,
            Some(cfg.generation),
            &event.event_type,
            &state.record.isi,
            &line,
        ) {
            self.unrecorded(cfg, &error, "silicon.log", &state.record.isi, &line);
        }
        if !state.record.disposable || state.record.archived_at.is_some() {
            let events = cfg
                .home
                .join(".silicon/sessions/events")
                .join(format!("{}.jsonl", self.session_id));
            if let Err(error) = state::append_json(&events, &event) {
                self.unrecorded(cfg, &error, "the session history", &state.record.isi, &line);
            }
        }
        if activity.is_none() {
            return Ok(());
        }
        // Replaced providers can emit late errors/ENDs; these belong in logs, not current lifecycle.
        if event.extra.get("late").and_then(Value::as_bool) == Some(true) {
            return Ok(());
        }
        match event.event_type.as_str() {
            Event::START | Event::INJECTED => {
                state.turn_open = true;
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
                let who = if event.provider.is_empty() {
                    "omni"
                } else {
                    event.provider.as_str()
                };
                let said = format!("{who} reported {}: {}", event.kind, event.error);
                // Stderr chatter and retried errors do not end the turn, but they usually
                // explain the failure that does, so they are kept for its message.
                if event.kind != "stderr"
                    && event.extra.get("willRetry").and_then(Value::as_bool) != Some(true)
                    && omni_idle
                {
                    let error = format!("{said}\nevent: {line}{}", state.quote_trouble(None));
                    // A blocked provider can fail before START and never emit END.
                    // Retire its daemon so failed receipts/Omni work cannot strand idle detection.
                    drop(state);
                    // Providers run with this session's SI_TOKEN capability in their environment.
                    self.fail(&failure::mask(&cfg.home, &error, &[&self.capability]));
                    return Ok(());
                }
                state.keep_trouble(event.provider.clone(), said);
            }
            Event::CONFIG => {
                // Omni takes a failing provider off the chat and says so before switching.
                if event.text == "provider_removed" {
                    let why = event.extra.get("why").and_then(Value::as_str);
                    let left: Option<Vec<&str>> = event
                        .extra
                        .get("left")
                        .and_then(Value::as_array)
                        .map(|left| left.iter().filter_map(Value::as_str).collect());
                    let remaining = match &left {
                        // An unreported list is not an empty one.
                        None => "remaining providers not reported".to_string(),
                        Some(left) if left.is_empty() => {
                            "no providers remain; Omni retries them all on the next send"
                                .to_string()
                        }
                        Some(left) => format!("remaining providers: {}", left.join(", ")),
                    };
                    let mut message = format!(
                        "Omni removed provider {}: {}; {remaining}",
                        event.provider,
                        why.unwrap_or("no reason given")
                    );
                    if why.is_none() || left.is_none() {
                        message.push_str(&format!("\nevent: {line}"));
                    }
                    // `why` is only a kind; the provider's own words came in earlier events.
                    message.push_str(&state.quote_trouble(Some(&event.provider)));
                    if let Err(error) = log_line_scoped(
                        &cfg.home,
                        Some(cfg.generation),
                        "provider_removed",
                        &state.record.isi,
                        &message,
                    ) {
                        self.unrecorded(cfg, &error, "silicon.log", &state.record.isi, &message);
                    }
                }
            }
            Event::END => {
                state.clear_trouble();
                // Native next-turn injections can share event.turn with an earlier END.
                // Omni's snapshot is the authority for whether all accepted work is now idle.
                if !omni_idle || queued != 0 {
                    return Ok(());
                }
                state.turn_open = false;
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
                // A failed save is reported below; the replies and retirement still happen.
                let saved = state.record.save(&cfg.home);
                let reply = state.text.clone();
                let reply_to_caller = state.record.ephemeral || state.record.archived_at.is_some();
                let name = state.record.isi.clone();
                drop(state);
                // Disposable work and questions to archived sessions return to their caller.
                for dispatch in finished {
                    if !reply_to_caller {
                        continue;
                    }
                    let Some(origin) = dispatch.origin else {
                        continue;
                    };
                    let worker = self.clone();
                    let reply_runtime = runtime.clone();
                    let reply_connected = connected.clone();
                    let message = format!("{name} completed:\n{reply}");
                    let replying_isi = name.clone();
                    // Recovery waits for provider events, so it must run off this listener.
                    // Reserve activity before spawn so an idle restart cannot race this job.
                    runtime.activities.fetch_add(1, Ordering::SeqCst);
                    let started = thread::Builder::new()
                        .name(format!("reply from {}", self.session_id))
                        .spawn(move || {
                            let _activity = Activity(&reply_runtime.activities);
                            if let Err(error) =
                                worker.reply(&reply_runtime, &reply_connected, &origin, &message)
                            {
                                log_error(
                                    &reply_connected.cfg.home,
                                    Some(reply_connected.cfg.generation),
                                    &replying_isi,
                                    &format!("ephemeral reply: {error:#}"),
                                );
                            }
                        });
                    if let Err(error) = started {
                        runtime.activities.fetch_sub(1, Ordering::SeqCst);
                        log_error(
                            &cfg.home,
                            Some(cfg.generation),
                            &name,
                            &format!("could not start ephemeral reply: {error}"),
                        );
                    }
                }
                if let Err(error) = saved {
                    log_error(
                        &cfg.home,
                        Some(cfg.generation),
                        &name,
                        &format!(
                            "saving session {} after its turn ended: {error:#}",
                            self.session_id
                        ),
                    );
                }
                // This worker has stopped by now even when its cleanup failed, so the listener
                // ends quietly after it: the failure is written here or nowhere.
                if let Err(error) = self.retire_if_idle() {
                    log_error(
                        &cfg.home,
                        Some(cfg.generation),
                        &name,
                        &format!(
                            "session {} finished its work, but stopping it afterwards failed: {error:#}",
                            self.session_id
                        ),
                    );
                }
                return Ok(());
            }
            _ => {}
        }
        Ok(())
    }

    /// A record-keeping write failed; the interpreter's own stderr (daemon.log) gets the
    /// failure and what it was recording.
    fn unrecorded(
        &self,
        cfg: &Config,
        error: &anyhow::Error,
        place: &str,
        isi: &str,
        record: &str,
    ) {
        crate::stderr_line(&failure::mask(
            &cfg.home,
            &format!("{error:#}; the {isi} event it was writing to {place}: {record}"),
            &[&self.capability],
        ));
    }

    /// Return an ephemeral or archived answer to the session that asked. When the asking
    /// worker has stopped (retired for inactivity, or failed) while its session is still
    /// active, the answer goes to the worker a later send opened for that session, busy or
    /// not, or resumes the session for it, in this same connection.
    fn reply(
        &self,
        runtime: &Arc<Runtime>,
        connected: &Arc<Connected>,
        origin: &Caller,
        text: &str,
    ) -> Result<()> {
        let context = || {
            format!(
                "returning the reply to {} session {}",
                origin.isi, origin.session
            )
        };
        runtime.delivery_ready(connected).with_context(context)?;
        let target = self.reply_target(runtime, connected, origin)?;
        let by_session = connected
            .cfg
            .isi
            .get(&origin.isi)
            .and_then(|isi| isi.primary_send_mode.as_deref())
            == Some("session");
        let options = SendOptions {
            id: by_session.then(|| target.state.lock().recover().record.id.clone()),
            ..Default::default()
        };
        runtime
            .recover_outgoing(connected, &origin.isi, &options)
            .with_context(context)?;
        // Twice: the worker found can itself retire between the lookup and the send.
        for _ in 0..2 {
            runtime.delivery_ready(connected).with_context(context)?;
            let target = self.reply_target(runtime, connected, origin)?;
            match target.send(text, None, true) {
                Err(error) if error.is::<Retired>() => {}
                sent => return sent.map(|_| ()).with_context(context),
            }
        }
        Err(anyhow!(
            "{} session {} retired for inactivity twice while the reply was being returned",
            origin.isi,
            origin.session
        ))
        .with_context(context)
    }

    /// The live worker of the session that asked, or a new one resuming it while its record
    /// is still active. An ended or archived session, or finished use-and-throw work (never
    /// saved), cannot take the answer.
    fn reply_target(
        &self,
        runtime: &Arc<Runtime>,
        connected: &Arc<Connected>,
        origin: &Caller,
    ) -> Result<Arc<Worker>> {
        let context = || {
            format!(
                "returning the reply to {} session {}",
                origin.isi, origin.session
            )
        };
        let ended = || {
            anyhow!(
                "{} session {} ended before the ephemeral reply could reach it",
                origin.isi,
                origin.session
            )
        };
        if let Some(live) = connected
            .workers
            .lock()
            .recover()
            .get(&origin.session)
            .filter(|worker| !worker.stopped.load(Ordering::SeqCst))
        {
            return Ok(live.clone());
        }
        let record = state::sessions(&connected.cfg.home, &origin.isi, false)
            .with_context(context)?
            .into_iter()
            .find(|record| record.session_id == origin.session)
            .ok_or_else(ended)?;
        let by_session = connected
            .cfg
            .isi
            .get(&origin.isi)
            .and_then(|isi| isi.primary_send_mode.as_deref())
            == Some("session");
        let target = runtime
            .worker(
                connected,
                &origin.isi,
                &SendOptions {
                    id: by_session.then(|| record.id.clone()),
                    ..Default::default()
                },
            )
            .with_context(context)?;
        if target.session_id != origin.session {
            return Err(ended());
        }
        Ok(target)
    }

    fn fail(&self, message: &str) {
        // A failed listener cannot be reused; remove it so a later send restores a fresh worker.
        if let Err(error) = self.stop(Some(message.into())) {
            if let Some(connected) = self.connected.upgrade() {
                log_error(
                    &connected.cfg.home,
                    Some(connected.cfg.generation),
                    "worker cleanup",
                    &format!("{message}\ncleaning up after it failed too: {error:#}"),
                );
            }
        }
    }

    /// `message`, plus omnid's exit status and output when the daemon has died.
    fn with_daemon_exit(&self, mut message: String) -> String {
        if let Some(exit) = self.daemon_exit() {
            message.push('\n');
            message.push_str(&exit);
        }
        message
    }

    /// When this worker's omnid has exited: its status and everything it wrote this run.
    fn daemon_exit(&self) -> Option<String> {
        let home = self.connected.upgrade()?.cfg.home.clone();
        let mut daemon = self.daemon.lock().recover();
        let daemon = daemon.as_mut()?;
        let status = match daemon.child.try_wait() {
            Ok(Some(status)) => status.to_string(),
            Ok(None) => return None,
            Err(error) => format!("status unknown ({error})"),
        };
        Some(format!(
            "omnid (pid {}) exited: {status}\n{}",
            daemon.child.id(),
            daemon_output(&home, &daemon.log, daemon.start, &[&self.capability])
        ))
    }

    /// omnid's state and everything it wrote this run, whether or not it is still running.
    fn daemon_report(&self) -> String {
        if let Some(exit) = self.daemon_exit() {
            return exit;
        }
        let Some(home) = self.connected.upgrade().map(|c| c.cfg.home.clone()) else {
            return "the Silicon is disconnected".into();
        };
        match self.daemon.lock().recover().as_ref() {
            Some(daemon) => format!(
                "omnid (pid {}) is still running\n{}",
                daemon.child.id(),
                daemon_output(&home, &daemon.log, daemon.start, &[&self.capability])
            ),
            None => "this worker has no omnid".into(),
        }
    }

    pub fn stop(&self, error: Option<String>) -> Result<()> {
        let mut client = self.client.lock().recover();
        self.stop_locked(&mut client, error)
    }

    fn retire_if_idle(&self) -> Result<bool> {
        // Same lock as send: either the new message is accepted first, or it sees an ended session.
        let mut client = self.client.lock().recover();
        let eligible = {
            let state = self.state.lock().recover();
            (state.record.ephemeral || state.record.archived_at.is_some())
                && state.pending.is_empty()
        };
        if !eligible {
            return Ok(false);
        }
        self.stop_locked(&mut client, None)?;
        Ok(true)
    }

    /// Retire this worker when nothing is outstanding, no turn is open and nothing has
    /// happened for `limit`: its omnid stops, its record stays active. Busy workers (a send
    /// or an initialization holding the client) are left for the next pass. Returns how long
    /// it had been idle.
    fn retire_if_quiet(&self, limit: TimeDelta) -> Result<Option<TimeDelta>> {
        let mut client = match self.client.try_lock() {
            Ok(client) => client,
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(TryLockError::WouldBlock) => return Ok(None),
        };
        if self.stopped.load(Ordering::SeqCst) || client.is_none() {
            return Ok(None);
        }
        let idle = {
            let state = self.state.lock().recover();
            let idle = Utc::now() - state.last_activity;
            (state.pending.is_empty() && !state.turn_open && idle >= limit).then_some(idle)
        };
        let Some(idle) = idle else {
            return Ok(None);
        };
        self.end_locked(&mut client, None, true)?;
        Ok(Some(idle))
    }

    fn stop_locked(&self, client: &mut Option<Client>, error: Option<String>) -> Result<()> {
        self.end_locked(client, error, false)
    }

    /// Stop this worker. `idle` retires it for inactivity: the record stays an active, idle
    /// session that the next send resumes, and kept ephemeral work is not archived.
    fn end_locked(
        &self,
        client: &mut Option<Client>,
        error: Option<String>,
        idle: bool,
    ) -> Result<()> {
        if self.stopped.load(Ordering::SeqCst) {
            return Ok(());
        }
        if let Some(client) = client.take() {
            // omnid is terminated next regardless, but a refused stop can leave its provider
            // running. A wedged omnid must not hold a stop for the default two minutes.
            let stopped = client.call_with_timeout(
                Request::new(0, "stop").on(&self.session_id.to_string()),
                STOP_TIMEOUT,
            );
            if let Err(error) = stopped {
                if let Some(connected) = self.connected.upgrade() {
                    let isi = self.state.lock().recover().record.isi.clone();
                    log_error(
                        &connected.cfg.home,
                        Some(connected.cfg.generation),
                        &isi,
                        &format!("asking Omni to stop session {}: {error}", self.session_id),
                    );
                }
            }
        }
        let mut errors = Vec::new();
        if let Some(daemon) = self.daemon.lock().recover().take() {
            let pid = daemon.child.id();
            if let Err(error) = terminate_child(daemon.child) {
                errors.push(format!("stopping omnid (pid {pid}): {error:#}"));
            }
        }
        if idle {
            self.retired.store(true, Ordering::SeqCst);
        }
        // Keep the old worker discoverable until its daemon exits. Its client lock rejects racing sends.
        self.stopped.store(true, Ordering::SeqCst);
        let mut state = self.state.lock().recover();
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
        } else if idle {
            "idle"
        } else {
            "stopped"
        }
        .into();
        // Retiring is not activity; the record keeps the time of its last real work.
        if !idle {
            state.record.last = Utc::now();
        }
        let ephemeral = state.record.disposable && state.record.archived_at.is_none();
        // Kept ephemeral work auto-archives when it retires, so its id stays reachable
        // through --archived instead of lingering as a stopped active session.
        let archive_on_retire = !idle
            && state.record.ephemeral
            && !state.record.disposable
            && state.record.archived_at.is_none();
        let connected = self.connected.upgrade();
        if let Some(connected) = connected.as_ref() {
            let stored = if archive_on_retire {
                state.record.archive(&connected.cfg.home, None, None, None)
            } else {
                state.record.save(&connected.cfg.home)
            };
            if let Err(error) = stored {
                errors.push(format!("saving session state: {error:#}"));
            }
            if let Some(error) = error.as_deref() {
                if let Err(log) = log_line_scoped(
                    &connected.cfg.home,
                    Some(connected.cfg.generation),
                    "error",
                    &state.record.isi,
                    error,
                ) {
                    // The failure itself still reaches the receipts above and fail()'s message.
                    errors.push(format!("recording this failure: {log:#}"));
                }
            }
        }
        drop(state);
        if let Some(runtime) = self.runtime.upgrade() {
            runtime.callers.write().recover().remove(&self.capability);
        }
        if let Some(connected) = connected {
            // A concurrent restart can already have replaced a failed worker with the same UUID.
            let mut workers = connected.workers.lock().recover();
            if workers
                .get(&self.session_id)
                .is_some_and(|worker| worker.capability == self.capability)
            {
                workers.remove(&self.session_id);
            }
            drop(workers);
            // Its refresh stops with it; a resumed worker reports a failure in full again.
            connected.recovered(&format!("dna refresh {}", self.session_id));
            if ephemeral {
                let directory = connected
                    .cfg
                    .home
                    .join(".silicon/omni")
                    .join(self.session_id.to_string());
                if directory.exists() {
                    if let Err(error) = fs::remove_dir_all(&directory) {
                        errors.push(format!("removing {}: {error}", directory.display()));
                    }
                }
                let alias = PathBuf::from(format!(
                    "/tmp/silicon-{}/{}",
                    unsafe { libc::getuid() },
                    self.session_id.simple()
                ));
                if fs::read_link(&alias).is_ok_and(|target| target == directory) {
                    if let Err(error) = fs::remove_file(&alias) {
                        errors.push(format!("removing {}: {error}", alias.display()));
                    }
                }
            }
        }
        if !errors.is_empty() {
            bail!("{}", errors.join("\n"));
        }
        Ok(())
    }

    fn refresh_deadline(&self) -> Result<()> {
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let record = self.state.lock().recover().record.clone();
        let isi = &connected.cfg.isi[&record.isi];
        let address = address_of(&connected.cfg, &record);
        let next = isi
            .dna
            .as_ref()
            .and_then(|dna| dna.get("next_refresh"))
            .map(|next| {
                interval(next, &connected.cfg, &address)
                    .context("evaluating dna.next_refresh")
                    .map(|every| (after(Utc::now(), delta(every)), every))
            })
            .transpose()?;
        self.state.lock().recover().next_dna = next;
        Ok(())
    }

    fn schedule_dna_refresh(self: &Arc<Self>) {
        {
            let now = Utc::now();
            let mut state = self.state.lock().recover();
            let Some((due, every)) = state.next_dna else {
                return;
            };
            if due - now > delta(every) {
                // The wall clock went back; a refresh never waits more than one interval.
                let due = after(now, delta(every));
                state.next_dna = Some((due, every));
                let isi = state.record.isi.clone();
                drop(state);
                if let Some(connected) = self.connected.upgrade() {
                    note(
                        &connected.cfg.home,
                        Some(connected.cfg.generation),
                        "runtime",
                        &isi,
                        &format!(
                            "the DNA refresh for session {} was due more than one interval ({}s) ahead, so the clock went back; it is now due at {due}",
                            self.session_id,
                            every.as_secs()
                        ),
                    );
                }
                return;
            }
            if now < due {
                return;
            }
            state.next_dna = None;
        }
        let worker = self.clone();
        let started = thread::Builder::new()
            .name(format!("dna refresh {}", self.session_id))
            .spawn(move || {
                let result = catch_unwind(AssertUnwindSafe(|| -> Result<()> {
                    let runtime = worker
                        .runtime
                        .upgrade()
                        .ok_or_else(|| anyhow!("interpreter stopped"))?;
                    let _activity = runtime.activity()?;
                    if worker.stopped.load(Ordering::SeqCst) {
                        return Ok(());
                    }
                    worker.refresh_dna()
                }))
                .unwrap_or_else(|panic| {
                    Err(anyhow!(
                        "the DNA refresh panicked: {}",
                        failure::panic_message(&*panic)
                    ))
                });
                match result {
                    Ok(()) => {
                        if let Some(connected) = worker.connected.upgrade() {
                            connected.recovered(&format!("dna refresh {}", worker.session_id));
                        }
                    }
                    Err(error) => worker.dna_retry_later(&format!("DNA refresh: {error:#}")),
                }
            });
        if let Err(error) = started {
            self.dna_retry_later(&format!("DNA refresh: could not start its thread: {error}"));
        }
    }

    /// A failed DNA refresh is reported and tried again in about a minute.
    fn dna_retry_later(&self, message: &str) {
        let Some(connected) = self.connected.upgrade() else {
            return;
        };
        // A worker that stopped meanwhile is not retried, and leaves no repeat record behind.
        if self.stopped.load(Ordering::SeqCst) {
            log_error(
                &connected.cfg.home,
                Some(connected.generation),
                "dna",
                message,
            );
            return;
        }
        connected.report(&format!("dna refresh {}", self.session_id), "dna", message);
        let every = Duration::from_secs(60);
        self.state.lock().recover().next_dna = Some((after(Utc::now(), delta(every)), every));
    }

    fn refresh_dna(&self) -> Result<()> {
        let connected = self
            .connected
            .upgrade()
            .ok_or_else(|| anyhow!("silicon disconnected"))?;
        let record = self.state.lock().recover().record.clone();
        let name = address_of(&connected.cfg, &record);
        let prompt = assemble_dna(&connected.cfg, &record.isi, &name)?;
        if let Some(client) = self.client.lock().recover().as_ref() {
            let accepted = client
                .set(&self.session_id.to_string(), "system_prompt", json!(prompt))
                .context("sending the refreshed system prompt to Omni")?;
            if !accepted {
                bail!("Omni answered the refreshed system prompt with accepted: false");
            }
        }
        self.refresh_deadline()?;
        Ok(())
    }
}

fn terminate_child(mut child: Child) -> Result<()> {
    if child
        .try_wait()
        .context("checking whether it exited")?
        .is_none()
    {
        if let Err(error) = child.kill() {
            if child
                .try_wait()
                .context("checking whether it exited")?
                .is_none()
            {
                return Err(error).context("sending SIGKILL");
            }
        }
    }
    child.wait().context("waiting for it to exit")?;
    Ok(())
}

/// Background failures have no caller to return to, so silicon.log is where they are read.
/// If that write fails too, the interpreter's own stderr (its daemon.log) gets both.
fn log_error(home: &Path, generation: Option<Uuid>, origin: &str, message: &str) {
    if let Err(error) = log_line_scoped(home, generation, "error", origin, message) {
        crate::stderr_line(&format!(
            "{error:#}; the {origin} error it was recording: {}",
            failure::mask(home, message, &[])
        ));
    }
}

/// What omnid wrote to `log` from `start` on, masked. The daemon explains its own failures there.
fn daemon_output(home: &Path, log: &Path, start: u64, secrets: &[&str]) -> String {
    let read = || -> std::io::Result<String> {
        let mut file = fs::File::open(log)?;
        file.seek(SeekFrom::Start(start))?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    };
    let text = match read() {
        Ok(text) if text.trim().is_empty() => format!("omnid wrote nothing to {}", log.display()),
        Ok(text) => format!("omnid wrote to {}:\n{}", log.display(), text.trim_end()),
        Err(error) => format!("omnid's log {} could not be read: {error}", log.display()),
    };
    failure::mask(home, &text, secrets)
}

/// A YAML value as JSON, for saying what was wrong with it.
fn shown(value: &serde_yaml::Value) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| format!("{value:?}"))
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
    silicon.as_object_mut().unwrap().remove("app_configs");
    if let Some(station) = silicon
        .get_mut("space_station")
        .and_then(Value::as_object_mut)
    {
        station.remove("table_key");
    }
    json!({"_connection":cfg.generation,"silicon":silicon,"isi":cfg.isi,"access":cfg.access,"request":{},"var":{}})
}

fn assemble_dna(cfg: &Config, isi: &str, address: &str) -> Result<String> {
    let mut parts = Vec::new();
    if let Some(items) = cfg.isi[isi]
        .dna
        .as_ref()
        .and_then(|d| d.get("assemble"))
        .and_then(|v| v.as_sequence())
    {
        for (index, item) in items.iter().enumerate() {
            let source = item.as_str().ok_or_else(|| {
                anyhow!(
                    "isi.{isi}.dna.assemble[{index}] must be a string, got {}",
                    shown(item)
                )
            })?;
            let text = eval::dna(source, &environment(cfg), &cfg.home, address);
            match text {
                Ok(text) => parts.push(format!("{source}\n{text}")),
                // A skipped entry does not stop the session; silicon.log is where it is read.
                Err(error) => log_error(
                    &cfg.home,
                    Some(cfg.generation),
                    address,
                    &format!(
                        "DNA entry skipped: isi.{isi}.dna.assemble[{index}] {source}: {error:#}"
                    ),
                ),
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
    Ok(parts.join("\n\n\n"))
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
    let seconds = n.trim().parse::<f64>().with_context(|| {
        format!("interval {text:?} must be a number with an optional s, m, min or h suffix")
    })? * mul;
    if !seconds.is_finite() || seconds <= 0. || seconds > 315360000. {
        bail!("interval {text:?} must be positive, at most ten years");
    }
    // A sub-second schedule would spin a scheduler meant to run for years.
    Ok(Duration::from_secs_f64(seconds.max(1.)))
}

/// The providers a session opens with, always as an explicit list. Omni keeps a session's
/// provider list durably and drops a provider from it when its login fails; only an
/// explicit list at open puts a provider that logged back in on the session again, so even
/// a lone `all-available-providers` sends what Omni reports as available right now.
fn select_providers(value: &serde_yaml::Value, inference: &Inference) -> Result<Vec<String>> {
    use serde_yaml::Value as Y;
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
                    bail!(
                        "inference provider is not installed/authenticated: {s} (Omni reports these as available: {})",
                        listed(available)
                    );
                }
                if !selected.contains(s) {
                    selected.push(s.clone());
                }
            }
            _ => bail!(
                "inference_providers must contain names or nested lists, got {}",
                shown(v)
            ),
        }
        Ok(())
    }
    fn listed(names: &[String]) -> String {
        if names.is_empty() {
            "none".into()
        } else {
            names.join(", ")
        }
    }
    let available = inference
        .get_available_providers(None)
        .context("asking Omni which inference providers are available")?;
    let mut selected = Vec::new();
    walk(value, &available, &mut selected)?;
    if selected.is_empty() {
        bail!(
            "inference_providers {} selects no authenticated providers (Omni reports these as available: {})",
            shown(value),
            listed(&available)
        );
    }
    Ok(selected)
}

/// How an ISI session is addressed: `isi`, or `isi:id` when it is session-addressed.
fn address_of(cfg: &Config, record: &Session) -> String {
    if cfg
        .isi
        .get(&record.isi)
        .and_then(|isi| isi.primary_send_mode.as_deref())
        == Some("session")
    {
        format!("{}:{}", record.isi, record.id)
    } else {
        record.isi.clone()
    }
}

/// A positive whole number of minutes from `name`, or `default`. Anything else is said once
/// on daemon.log and the default used.
fn minutes_from_env(name: &str, default: u64) -> Duration {
    let minutes = match std::env::var(name) {
        Err(_) => default,
        Ok(value) => match value.trim().parse::<u64>() {
            Ok(minutes) if minutes > 0 => minutes,
            _ => {
                static SAID: Mutex<Vec<String>> = Mutex::new(Vec::new());
                let mut said = SAID.lock().recover();
                if !said.contains(&name.to_owned()) {
                    said.push(name.to_owned());
                    crate::stderr_line(&format!(
                        "{name}={value:?} is not a positive whole number of minutes; using {default}"
                    ));
                }
                default
            }
        },
    };
    Duration::from_secs(minutes.saturating_mul(60))
}

/// A std duration as a chrono one; intervals are capped at ten years, far inside its range.
fn delta(duration: Duration) -> TimeDelta {
    TimeDelta::from_std(duration).unwrap_or(TimeDelta::MAX)
}

/// `at + by`, saturating instead of panicking at the end of the representable range.
fn after(at: DateTime<Utc>, by: TimeDelta) -> DateTime<Utc> {
    at.checked_add_signed(by)
        .unwrap_or(DateTime::<Utc>::MAX_UTC)
}

/// A random offset below `limit`, so work found due together does not start together.
fn jitter(limit: TimeDelta) -> TimeDelta {
    let millis = limit.num_milliseconds().max(0) as u128;
    if millis == 0 {
        return TimeDelta::zero();
    }
    TimeDelta::milliseconds((Uuid::new_v4().as_u128() % millis) as i64)
}

/// An interpreter note for silicon.log; if that write fails, daemon.log gets it instead.
fn note(home: &Path, generation: Option<Uuid>, kind: &str, origin: &str, message: &str) {
    if let Err(error) = log_line_scoped(home, generation, kind, origin, message) {
        crate::stderr_line(&format!(
            "{error:#}; the {kind} note it was recording for {origin}: {}",
            failure::mask(home, message, &[])
        ));
    }
}

fn beats_path(home: &Path) -> PathBuf {
    home.join(".silicon/heartbeats.json")
}

/// The heartbeat schedule saved for `home`. Entries already due are spread over the next
/// minute (at most one interval), so a restart does not start every session's omnid at once.
fn load_beats(home: &Path, generation: Uuid) -> BTreeMap<String, Beat> {
    let path = beats_path(home);
    let mut beats: BTreeMap<String, Beat> = match fs::read(&path) {
        Ok(bytes) => match serde_json::from_slice(&bytes) {
            Ok(beats) => beats,
            Err(error) => {
                log_error(
                    home,
                    Some(generation),
                    "heartbeat",
                    &format!(
                        "{} is not a heartbeat schedule, so every heartbeat starts a fresh interval: {error}; it holds: {}",
                        path.display(),
                        String::from_utf8_lossy(&bytes)
                    ),
                );
                BTreeMap::new()
            }
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
        Err(error) => {
            log_error(
                home,
                Some(generation),
                "heartbeat",
                &format!(
                    "could not read {}, so every heartbeat starts a fresh interval: {error}",
                    path.display()
                ),
            );
            BTreeMap::new()
        }
    };
    beats.retain(|_, beat| beat.sane());
    let now = Utc::now();
    for beat in beats.values_mut() {
        if beat.due <= now {
            beat.due = after(now, jitter(beat.every().min(TimeDelta::seconds(60))));
        }
    }
    beats
}

/// The ids of the tings in a batch request, which together name the batch.
fn ting_ids(request: &Value) -> Vec<String> {
    request["tings"]
        .as_array()
        .map(|tings| {
            tings
                .iter()
                .map(|ting| match &ting["id"] {
                    Value::String(id) => id.clone(),
                    other => other.to_string(),
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Which try this is at `request`, the batch now at the head of the Ting inbox.
fn next_attempt(head: &mut Option<(String, u32)>, key: &str) -> u32 {
    match head {
        Some((current, attempts)) if current == key => {
            *attempts = attempts.saturating_add(1);
            *attempts
        }
        _ => {
            *head = Some((key.to_owned(), 1));
            1
        }
    }
}

type Task = (String, Box<dyn FnOnce() -> Result<()> + Send>);

/// Run every task on its own thread and wait for all of them together, at most `budget`.
/// Outcomes come back in task order; `None` is a task still running at the deadline, left
/// to finish in the background. A task whose thread cannot start runs on this one.
fn in_parallel(tasks: Vec<Task>, budget: Duration) -> Vec<(String, Option<Result<()>>)> {
    let (done, finished) = mpsc::channel::<(usize, Result<()>)>();
    let run = |task: Box<dyn FnOnce() -> Result<()> + Send>| {
        catch_unwind(AssertUnwindSafe(task))
            .unwrap_or_else(|panic| Err(anyhow!("panicked: {}", failure::panic_message(&*panic))))
    };
    let mut outcomes: Vec<(String, Option<Result<()>>)> = Vec::new();
    let mut waiting = 0;
    for (index, (name, task)) in tasks.into_iter().enumerate() {
        let slot = Arc::new(Mutex::new(Some(task)));
        let taken = slot.clone();
        let done = done.clone();
        let spawned = thread::Builder::new()
            .name(format!("stop {index}"))
            .spawn(move || {
                if let Some(task) = taken.lock().recover().take() {
                    let _ = done.send((index, run(task)));
                }
            });
        let inline = match spawned {
            Ok(_) => None,
            Err(_) => slot.lock().recover().take(),
        };
        match inline {
            Some(task) => outcomes.push((name, Some(run(task)))),
            None => {
                waiting += 1;
                outcomes.push((name, None));
            }
        }
    }
    drop(done);
    let deadline = Instant::now() + budget;
    while waiting > 0 {
        match finished.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok((index, result)) => {
                outcomes[index].1 = Some(result);
                waiting -= 1;
            }
            Err(_) => break,
        }
    }
    outcomes
}

/// What answered a new omnid's socket.
enum Answer {
    Ours(Inference),
    /// Another omnid, while the one just started exited: it holds this session's home.
    Other(DaemonInfo, Client),
}

/// How far a start got.
enum Started {
    Ready(Client, Box<Chat>),
    Stale(DaemonInfo, Client),
}

/// Connect to `socket` and ask who answers. omnid refuses to start while another omnid
/// holds the session's home, so a socket answered by another pid while `child` exits
/// belongs to an omnid left by an earlier interpreter run. It is never adopted: it carries
/// that run's SI_TOKEN and SI_URL.
fn own_daemon(socket: &Path, child: &mut Child, lock: &Path) -> Result<Answer> {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let attempt = (|| -> Result<(Client, DaemonInfo)> {
            let stream = UnixStream::connect(socket)
                .with_context(|| format!("cannot connect to {}", socket.display()))?;
            // A daemon that stops reading must not hold a send, and the locks around it, forever.
            stream
                .set_write_timeout(Some(Duration::from_secs(30)))
                .with_context(|| format!("setting a write timeout on {}", socket.display()))?;
            let client = Client::from_stream(stream)?;
            let answer =
                client.call_with_timeout(Request::new(0, "ping"), Duration::from_secs(5))?;
            let info = serde_json::from_value::<DaemonInfo>(answer.clone())
                .with_context(|| format!("omnid answered ping with {answer}"))?;
            Ok((client, info))
        })();
        match attempt {
            Ok((client, info)) if info.pid == child.id() => {
                if info.protocol != PROTOCOL {
                    bail!(
                        "omni protocol mismatch: this interpreter speaks {PROTOCOL}, but omnid {} (pid {}) speaks {}",
                        info.version,
                        info.pid,
                        info.protocol
                    );
                }
                return Ok(Answer::Ours(Inference::from_client(client)));
            }
            Ok((client, info)) => {
                // Ours is still starting, or standing down because that one holds the home.
                let settle = Instant::now() + Duration::from_secs(5);
                loop {
                    if child.try_wait().context("checking on omnid")?.is_some() {
                        return Ok(Answer::Other(info, client));
                    }
                    if Instant::now() >= settle {
                        bail!(
                            "Omni socket {} is answered by omnid pid {} while pid {} that this interpreter started for the session is still running; if OMNI_DAEMON is a wrapper script, make it `exec` omnid",
                            socket.display(),
                            info.pid,
                            child.id()
                        );
                    }
                    thread::sleep(Duration::from_millis(50));
                }
            }
            Err(error) => {
                if let Some(status) = child.try_wait().context("checking on omnid")? {
                    let mut message = format!(
                        "omnid exited ({status}) before accepting connections; last attempt: {error:#}"
                    );
                    if let Some(holder) = lock_holder(lock) {
                        message.push_str(&format!(
                            "\n{} is locked by another process ({holder}); an omnid left by an earlier interpreter run may still own this session, but it does not answer on {}, so it was not stopped",
                            lock.display(),
                            socket.display()
                        ));
                    }
                    bail!("{message}");
                }
                if Instant::now() >= deadline {
                    return Err(error.context("omnid did not accept connections within 20s"));
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
    }
}

/// Whether omnid's lock at `lock` is free: no omnid owns the session's home.
fn lock_free(lock: &Path) -> Result<bool> {
    let file = match fs::File::open(lock) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(true),
        Err(error) => return Err(error).with_context(|| format!("opening {}", lock.display())),
    };
    // Dropping `file` releases a lock taken here.
    Ok(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0)
}

/// Who holds `lock`, as far as its contents say, when it is held.
fn lock_holder(lock: &Path) -> Option<String> {
    if lock_free(lock).unwrap_or(true) {
        return None;
    }
    Some(match fs::read_to_string(lock) {
        Ok(pid) if !pid.trim().is_empty() => format!("it names pid {}", pid.trim()),
        Ok(_) => "it names no pid".into(),
        Err(error) => format!("its contents could not be read: {error}"),
    })
}

/// Wait up to `within` for `lock` to be released.
fn released(lock: &Path, within: Duration) -> Result<bool> {
    let deadline = Instant::now() + within;
    loop {
        if lock_free(lock)? {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// Stop the omnid an earlier interpreter run left on this session's home: ask it, then
/// SIGTERM, then SIGKILL, five seconds apart. Only the pid that answered on the session's
/// own socket is signalled, and only while the session's omnid.pid lock is still held.
fn stop_stale(
    cfg: &Config,
    address: &str,
    socket: &Path,
    lock: &Path,
    info: &DaemonInfo,
    client: Client,
) -> Result<()> {
    let pid = info.pid;
    let say = |text: &str| note(&cfg.home, Some(cfg.generation), "runtime", address, text);
    let asked = client.call_with_timeout(Request::new(0, "shutdown"), Duration::from_secs(10));
    drop(client);
    // omnid notices a shutdown request when its next connection arrives.
    let _ = UnixStream::connect(socket);
    say(&match &asked {
        Ok(answer) => format!("asked stale omnid pid {pid} to shut down; it answered {answer}"),
        Err(error) => {
            format!("asked stale omnid pid {pid} to shut down; the request failed: {error}")
        }
    });
    if released(lock, Duration::from_secs(5))? {
        say(&format!("stale omnid pid {pid} stopped"));
        return Ok(());
    }
    for (signal, name) in [(libc::SIGTERM, "SIGTERM"), (libc::SIGKILL, "SIGKILL")] {
        let sent = unsafe { libc::kill(pid as libc::pid_t, signal) };
        say(&if sent == 0 {
            format!(
                "stale omnid pid {pid} still held {} 5s later; sent it {name}",
                lock.display()
            )
        } else {
            format!(
                "stale omnid pid {pid} still held {} 5s later; sending it {name} failed: {}",
                lock.display(),
                std::io::Error::last_os_error()
            )
        });
        if released(lock, Duration::from_secs(5))? {
            say(&format!("stale omnid pid {pid} stopped after {name}"));
            return Ok(());
        }
    }
    bail!(
        "stale omnid pid {pid} still holds {} after a shutdown request, SIGTERM and SIGKILL; it has to be stopped by hand before session {address} can start",
        lock.display()
    )
}

/// What a heartbeat job does when it runs.
enum Job {
    /// Work out the first due time for an address that has none, or whose saved one came
    /// from a different `heartbeat.next` (given here): that one is kept when it is sooner.
    Schedule(Option<Beat>),
    /// The heartbeat was due at the given tick: reschedule it and send it.
    Fire(DateTime<Utc>),
}

/// Session ids read for a heartbeat ISI, and when; `None` until a read has succeeded.
type Listed = (Instant, Option<Vec<String>>);

/// The heartbeat scheduler's own state. Due times live on each connection (and on disk);
/// this keeps what is in flight and what was recently read.
#[derive(Default)]
struct Scheduler {
    /// Heartbeat work still running, by connection generation and address.
    jobs: HashMap<(Uuid, String), Weak<()>>,
    /// Session-addressed targets per generation and ISI, and when they were read.
    targets: HashMap<(Uuid, String), Listed>,
    /// Targets skipped because their last heartbeat is unfinished; said once per busy spell.
    busy: HashSet<(Uuid, String)>,
    /// The idle-retirement pass: when it last started, and whether it still runs.
    retiring: Option<(Instant, Weak<()>)>,
}

impl Scheduler {
    fn tick(&mut self, runtime: &Arc<Runtime>) {
        let connections: Vec<_> = runtime
            .silicons
            .read()
            .recover()
            .values()
            .cloned()
            .collect();
        let mut live = HashSet::new();
        for connected in &connections {
            if !connected.enabled.load(Ordering::SeqCst) {
                continue;
            }
            let outcome = catch_unwind(AssertUnwindSafe(|| {
                self.tick_connection(runtime, connected, &mut live)
            }));
            if let Err(panic) = outcome {
                // A panic that repeats every tick is written once per change or ten minutes.
                connected.report(
                    "heartbeat scheduler",
                    "heartbeat",
                    &format!(
                        "the heartbeat scheduler panicked on this Silicon and carries on: {}",
                        failure::panic_message(&*panic)
                    ),
                );
            }
        }
        self.jobs
            .retain(|key, job| live.contains(key) || job.strong_count() > 0);
        self.busy.retain(|key| live.contains(key));
        let generations: HashSet<Uuid> = connections.iter().map(|c| c.generation).collect();
        self.targets
            .retain(|(generation, _), _| generations.contains(generation));
        self.retire_idle(runtime, &connections);
    }

    fn tick_connection(
        &mut self,
        runtime: &Arc<Runtime>,
        connected: &Arc<Connected>,
        live: &mut HashSet<(Uuid, String)>,
    ) {
        let mut wanted = BTreeSet::new();
        let mut complete = true;
        for (name, isi) in &connected.cfg.isi {
            let Some(heartbeat) = &isi.heartbeat else {
                continue;
            };
            let next = shown(&heartbeat["next"]);
            let sessions: Vec<Option<String>> =
                if isi.primary_send_mode.as_deref() == Some("session") {
                    match self.session_targets(runtime, connected, name) {
                        Some(ids) => ids.into_iter().map(Some).collect(),
                        None => {
                            // Never listed yet: keep whatever schedule this ISI has.
                            complete = false;
                            continue;
                        }
                    }
                } else {
                    vec![None]
                };
            for session in sessions {
                let address = session
                    .as_ref()
                    .map(|s| format!("{name}:{s}"))
                    .unwrap_or_else(|| name.clone());
                wanted.insert(address.clone());
                let key = (connected.generation, address.clone());
                live.insert(key.clone());
                if self
                    .jobs
                    .get(&key)
                    .is_some_and(|job| job.strong_count() > 0)
                {
                    continue;
                }
                let beat = connected.beats.lock().recover().get(&address).cloned();
                let job = match beat {
                    None => Job::Schedule(None),
                    // Worked out from another heartbeat.next, before a reconnect changed it.
                    Some(beat) if beat.next != next => Job::Schedule(Some(beat)),
                    Some(beat) => {
                        // Read after the schedule, so a job's newer due time is never "ahead".
                        let now = Utc::now();
                        let every = beat.every();
                        if beat.due - now > every {
                            let due = after(now, every);
                            connected.set_beat(
                                &address,
                                Beat {
                                    due,
                                    ..beat.clone()
                                },
                            );
                            note(
                                &connected.cfg.home,
                                Some(connected.cfg.generation),
                                "heartbeat",
                                &address,
                                &format!(
                                    "the heartbeat was due at {}, more than one interval ({}s) ahead of now, so the clock went back; it is now due at {due}",
                                    beat.due, beat.every_seconds
                                ),
                            );
                            continue;
                        }
                        if now < beat.due {
                            continue;
                        }
                        // Coalesce: one heartbeat at a time per target, however long it runs.
                        if heartbeat_unfinished(connected, name, session.as_deref()) {
                            let due = after(now, every);
                            connected.set_beat(
                                &address,
                                Beat {
                                    due,
                                    ..beat.clone()
                                },
                            );
                            if self.busy.insert(key.clone()) {
                                note(
                                    &connected.cfg.home,
                                    Some(connected.cfg.generation),
                                    "heartbeat",
                                    &address,
                                    &format!("skipped: the previous heartbeat to {address} has not finished; the next is due at {due}"),
                                );
                            }
                            continue;
                        }
                        self.busy.remove(&key);
                        Job::Fire(now)
                    }
                };
                let token = Arc::new(());
                self.jobs.insert(key, Arc::downgrade(&token));
                spawn_heartbeat(
                    runtime, connected, name, heartbeat, address, session, job, token,
                );
            }
        }
        if complete {
            let mut beats = connected.beats.lock().recover();
            let before = beats.len();
            beats.retain(|address, _| wanted.contains(address));
            if beats.len() != before {
                connected.beats_changed.store(true, Ordering::SeqCst);
            }
            drop(beats);
            // A session that ended or was archived takes its heartbeat's failure record with it.
            connected.reported.lock().recover().retain(|key, _| {
                key.strip_prefix(BEAT_REPORT)
                    .is_none_or(|address| wanted.contains(address))
            });
        }
        connected.write_beats(false);
    }

    /// Active sessions of a session-addressed ISI: read from disk at most every TARGETS_TTL,
    /// plus the live workers. A failed read keeps the last good list; `None` means there has
    /// never been one.
    fn session_targets(
        &mut self,
        runtime: &Runtime,
        connected: &Connected,
        name: &str,
    ) -> Option<Vec<String>> {
        let key = (connected.generation, name.to_owned());
        if !self
            .targets
            .get(&key)
            .is_some_and(|(read, _)| read.elapsed() < TARGETS_TTL)
        {
            let previous = self.targets.remove(&key).and_then(|(_, ids)| ids);
            let report = format!("heartbeat sessions {name}");
            let ids = match runtime.list_connected(connected, name, false) {
                Ok(records) => {
                    connected.recovered(&report);
                    Some(records.into_iter().map(|record| record.id).collect())
                }
                Err(error) => {
                    connected.report(&report, name, &format!("heartbeat sessions: {error:#}"));
                    previous
                }
            };
            self.targets.insert(key.clone(), (Instant::now(), ids));
        }
        let listed = self.targets.get(&key).and_then(|(_, ids)| ids.clone())?;
        // Sessions opened since the last read are live workers; those cost no disk read.
        let mut ids: BTreeSet<String> = listed.into_iter().collect();
        for worker in connected.workers.lock().recover().values() {
            let state = worker.state.lock().recover();
            if !worker.stopped.load(Ordering::SeqCst)
                && state.record.isi == name
                && state.record.archived_at.is_none()
            {
                ids.insert(state.record.id.clone());
            }
        }
        Some(ids.into_iter().collect())
    }

    /// Once a minute, on its own thread, retire session workers idle for longer than
    /// SILICON_IDLE_SESSION_MINUTES (default 60).
    fn retire_idle(&mut self, runtime: &Arc<Runtime>, connections: &[Arc<Connected>]) {
        if self.retiring.as_ref().is_some_and(|(started, job)| {
            started.elapsed() < Duration::from_secs(60) || job.strong_count() > 0
        }) {
            return;
        }
        let token = Arc::new(());
        self.retiring = Some((Instant::now(), Arc::downgrade(&token)));
        let limit = delta(minutes_from_env("SILICON_IDLE_SESSION_MINUTES", 60));
        let (runtime, connections) = (runtime.clone(), connections.to_vec());
        let started = thread::Builder::new()
            .name("idle sessions".into())
            .spawn(move || {
                let _token = token;
                for connected in connections {
                    if let Err(panic) =
                        catch_unwind(AssertUnwindSafe(|| runtime.retire_quiet(&connected, limit)))
                    {
                        log_error(
                            &connected.cfg.home,
                            Some(connected.cfg.generation),
                            "runtime",
                            &format!(
                                "retiring idle sessions panicked: {}",
                                failure::panic_message(&*panic)
                            ),
                        );
                    }
                }
            });
        if let Err(error) = started {
            crate::stderr_line(&format!(
                "could not start the idle-session thread; it is tried again in a minute: {error}"
            ));
        }
    }
}

/// Whether a live worker for this heartbeat target still holds an undelivered or unfinished
/// heartbeat. Global ephemeral ISIs count any of their workers.
fn heartbeat_unfinished(connected: &Connected, isi: &str, session: Option<&str>) -> bool {
    connected.workers.lock().recover().values().any(|worker| {
        let state = worker.state.lock().recover();
        !worker.stopped.load(Ordering::SeqCst)
            && state.record.isi == isi
            && session.is_none_or(|id| state.record.id == id)
            && state.pending.iter().any(|dispatch| dispatch.heartbeat)
    })
}

/// Evaluate and send one heartbeat on its own thread. `token` marks it in flight until the
/// thread ends. Its failures are written in full, once per change or ten minutes.
#[allow(clippy::too_many_arguments)]
fn spawn_heartbeat(
    runtime: &Arc<Runtime>,
    connected: &Arc<Connected>,
    name: &str,
    heartbeat: &serde_yaml::Value,
    address: String,
    session: Option<String>,
    job: Job,
    token: Arc<()>,
) {
    let (runtime, owner, name, heartbeat) = (
        runtime.clone(),
        connected.clone(),
        name.to_owned(),
        heartbeat.clone(),
    );
    let key = format!("{BEAT_REPORT}{address}");
    let next = shown(&heartbeat["next"]);
    let thread_address = address.clone();
    let thread_key = key.clone();
    let started = thread::Builder::new()
        // Session ids are arbitrary text, and a NUL in a thread name would panic the spawn.
        .name(format!("heartbeat {address}").replace('\0', "?"))
        .spawn(move || {
            let _token = token;
            let (address, key) = (thread_address, thread_key);
            let outcome = catch_unwind(AssertUnwindSafe(|| {
                run_heartbeat(&runtime, &owner, &name, &heartbeat, &address, session, job)
            }));
            let message = match outcome {
                Ok(Ok(())) => {
                    owner.recovered(&key);
                    return;
                }
                // Refused because the interpreter is stopping: nothing failed.
                Ok(Err(_)) if runtime.stopping.load(Ordering::SeqCst) => return,
                Ok(Err(error)) => format!("{error:#}"),
                Err(panic) => format!("heartbeat panicked: {}", failure::panic_message(&*panic)),
            };
            owner.report(&key, &address, &message);
        });
    if let Err(error) = started {
        let every = Duration::from_secs(60);
        connected.set_beat(
            &address,
            Beat::new(after(Utc::now(), delta(every)), every, &next),
        );
        connected.report(
            &key,
            &address,
            &format!(
                "heartbeat: could not start its thread, so it is tried again in a minute: {error}"
            ),
        );
    }
}

fn run_heartbeat(
    runtime: &Arc<Runtime>,
    connected: &Arc<Connected>,
    name: &str,
    heartbeat: &serde_yaml::Value,
    address: &str,
    session: Option<String>,
    job: Job,
) -> Result<()> {
    let _activity = runtime.activity().context("heartbeat")?;
    if !connected.enabled.load(Ordering::SeqCst) {
        bail!("heartbeat: silicon disconnected");
    }
    let cfg = &connected.cfg;
    let next = shown(&heartbeat["next"]);
    // `next` can be Bash, so it is evaluated here and never on the scheduler thread.
    let every = match interval(&heartbeat["next"], cfg, address) {
        Ok(every) => every,
        Err(error) => {
            // Failed timing is tried again in about a minute.
            let retry = Duration::from_secs(60);
            connected.set_beat(
                address,
                Beat::new(after(Utc::now(), delta(retry)), retry, &next),
            );
            return Err(error
                .context("evaluating heartbeat.next")
                .context("heartbeat schedule"));
        }
    };
    let fired = match job {
        // The first heartbeat waits for its interval.
        Job::Schedule(None) => {
            connected.set_beat(
                address,
                Beat::new(after(Utc::now(), delta(every)), every, &next),
            );
            return Ok(());
        }
        // A changed heartbeat.next: a shorter interval applies now, and a longer one after
        // the heartbeat the old one had already scheduled.
        Job::Schedule(Some(saved)) => {
            let due = after(Utc::now(), delta(every)).min(saved.due);
            note(
                &cfg.home,
                Some(cfg.generation),
                "heartbeat",
                address,
                &format!(
                    "heartbeat.next is now {next} ({}s), and the saved due time {} was worked out from {}; the next heartbeat is due at {due}",
                    every.as_secs_f64(),
                    saved.due,
                    if saved.next.is_empty() {
                        "an unrecorded heartbeat.next".to_owned()
                    } else {
                        format!("{} ({}s)", saved.next, saved.every_seconds)
                    }
                ),
            );
            connected.set_beat(address, Beat::new(due, every, &next));
            return Ok(());
        }
        Job::Fire(at) => at,
    };
    connected.set_beat(address, Beat::new(after(fired, delta(every)), every, &next));
    (|| -> Result<()> {
        let source = heartbeat["message"].as_str().ok_or_else(|| {
            anyhow!(
                "heartbeat.message must be a string, got {}",
                shown(&heartbeat["message"])
            )
        })?;
        let message = eval::evaluate(source, &environment(cfg), &cfg.home, address)
            .context("evaluating heartbeat.message")?;
        let options = SendOptions {
            id: session,
            ..Default::default()
        };
        runtime.recover_outgoing(connected, name, &options)?;
        runtime.send_marked(connected, None, name, &message, &options, true, true)?;
        Ok(())
    })()
    .context("heartbeat")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_setup_logs_both_streams_redacts_errors_and_never_connects() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg: Config = serde_yaml::from_str(
            r#"
silicon:
  id: si:test
  org_id: org
  token: private-setup-credential
  timezone: UTC
  setup:
    - '! printf "setup output"; printf "private-setup-credential" >&2; exit 9'
isi: {a: {model: fast, primary_send_mode: global, session_type: persistent}}
access: {a: []}
flow: []
"#,
        )
        .unwrap();
        cfg.home = dir.path().to_owned();
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        let error = runtime.connect(cfg).unwrap_err().to_string();
        // The script's own status and output survive connect's masking, all of it.
        assert!(
            error.contains("setup[0]")
                && error.contains("exit status: 9")
                && error.contains("setup output"),
            "{error}"
        );
        assert!(!error.contains("private-setup-credential"));
        assert!(runtime.get("si:test").is_err());
        let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        assert!(log.contains("[stdout/") && log.contains("[stderr/"));
        assert!(log.contains("setup output") && !log.contains("private-setup-credential"));
        assert_eq!(
            crate::telemetry::redact(dir.path(), "private-setup-credential"),
            "[redacted]"
        );
    }

    #[test]
    fn dna_includes_verbatim_sources_before_contents_and_skips_failed_entries() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("prompt.md"), "file contents").unwrap();
        let mut cfg: Config = serde_yaml::from_str(
            r#"
silicon: {id: 'si:test', org_id: org, SILICON_ORG: org, token: test, timezone: UTC}
isi:
  a:
    model: fast
    primary_send_mode: session
    session_type: persistent
    dna:
      assemble:
        - prompt.md
        - '! printf "$ISI"'
        - 'absent.md !>> "No contacts"'
        - absent.md
access: {a: []}
flow: []
"#,
        )
        .unwrap();
        cfg.home = dir.path().to_owned();
        let prompt = assemble_dna(&cfg, "a", "a:job").unwrap();
        assert!(prompt.starts_with("prompt.md\nfile contents\n\n\n! printf \"$ISI\"\na:job\n\n\nabsent.md !>> \"No contacts\"\nNo contacts\n\n\nYou are a:job."));
        assert_eq!(prompt.matches("absent.md").count(), 1);
        let log = std::fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        // The skipped entry is named, and its failure follows in full.
        assert!(
            log.contains("[DNA entry skipped: isi.a.dna.assemble[3] absent.md: "),
            "{log}"
        );
    }

    fn connect(runtime: &Arc<Runtime>, mut cfg: Config) {
        cfg.generation = Uuid::new_v4();
        if cfg.flow.is_null() {
            cfg.flow = serde_yaml::Value::Sequence(Vec::new());
        }
        fs::write(&cfg.path, serde_yaml::to_string(&cfg).unwrap()).unwrap();
        let key = serde_json::to_string(&(
            cfg.silicon.id.as_deref().unwrap(),
            cfg.silicon.org_id.as_deref().unwrap(),
            "ting",
        ))
        .unwrap();
        state::write_json(
            &cfg.home.join(".silicon/auth-grants.json"),
            &BTreeMap::from([(key.clone(), cfg.silicon.silicon_org.as_deref().unwrap())]),
        )
        .unwrap();
        state::write_json(
            &cfg.home.join(".silicon/auth-checked.json"),
            &BTreeMap::from([(key, Utc::now().timestamp())]),
        )
        .unwrap();
        runtime.connect_prepared(cfg).unwrap();
    }

    fn worker(ephemeral: bool) -> (tempfile::TempDir, Arc<Runtime>, Arc<Connected>, Arc<Worker>) {
        worker_with_flow(ephemeral, serde_yaml::Value::Sequence(Vec::new()))
    }

    fn worker_with_flow(
        ephemeral: bool,
        flow: serde_yaml::Value,
    ) -> (tempfile::TempDir, Arc<Runtime>, Arc<Connected>, Arc<Worker>) {
        worker_with_mode(ephemeral, flow, "global")
    }

    fn worker_with_mode(
        ephemeral: bool,
        flow: serde_yaml::Value,
        send_mode: &str,
    ) -> (tempfile::TempDir, Arc<Runtime>, Arc<Connected>, Arc<Worker>) {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg: Config = serde_yaml::from_str(&format!(
            r#"
silicon: {{id: 'si:test', org_id: org, SILICON_ORG: org, token: test, timezone: UTC, inference_providers: [all-available-providers]}}
isi:
  a: {{model: fast, primary_send_mode: {}, session_type: {}}}
access: {{a: []}}
flow: []
"#,
            send_mode,
            if ephemeral { "ephemeral" } else { "persistent" }
        ))
        .unwrap();
        cfg.home = dir.path().to_owned();
        cfg.path = dir.path().join("silicon.yaml");
        cfg.flow = flow;
        // Transport tests control each receipt; retry policy has its own outbox checks.
        cfg.silicon.max_retries = 0;
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        // Session addressing needs an id; global addressing must not be given one.
        let options = SendOptions {
            id: (send_mode == "session").then(|| "job".to_owned()),
            title: (send_mode == "session").then(|| "job".to_owned()),
            new: send_mode == "session",
            ..Default::default()
        };
        let worker = runtime.worker(&connected, "a", &options).unwrap();
        (dir, runtime, connected, worker)
    }

    fn pending(worker: &Worker, message: &str) -> Sent {
        let id = Uuid::new_v4();
        let receipt = SendReceipt::new();
        let mut state = worker.state.lock().recover();
        state.pending.push(Dispatch {
            id,
            message: message.into(),
            turn: None,
            receipt: receipt.clone(),
            origin: None,
            heartbeat: false,
        });
        Sent {
            session: state.record.clone(),
            id,
            receipt,
        }
    }

    fn transport(worker: &Arc<Worker>) -> thread::JoinHandle<Vec<String>> {
        transport_ending(worker, true)
    }

    /// A fake Omni connection for `worker`: it accepts every send and emits START, then END
    /// when `ended`, and returns the messages it received once asked to stop.
    fn transport_ending(worker: &Arc<Worker>, ended: bool) -> thread::JoinHandle<Vec<String>> {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixStream;

        let (client, mut daemon) = UnixStream::pair().unwrap();
        daemon
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
        let worker = worker.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(daemon.try_clone().unwrap());
            let mut messages = Vec::new();
            loop {
                let mut line = String::new();
                assert!(reader.read_line(&mut line).unwrap() > 0);
                let request: Value = serde_json::from_str(&line).unwrap();
                let stopped = request["op"] == "stop";
                assert!(stopped || request["op"] == "send");
                writeln!(
                    daemon,
                    "{}",
                    json!({"id":request["id"], "ok":true,
                    "result":if stopped {json!({"stopped":true})} else {json!({"accepted":true})}})
                )
                .unwrap();
                if stopped {
                    return messages;
                }
                let message = request["text"].as_str().unwrap();
                messages.push(message.to_owned());
                worker
                    .on_event(Event::new(Event::START).saying(message), false, 0)
                    .unwrap();
                // The turn finishes, so a heartbeat to this worker is not left unfinished.
                if ended {
                    worker.on_event(Event::new(Event::END), true, 0).unwrap();
                }
            }
        })
    }

    fn wait_until(ready: impl FnMut() -> bool) -> bool {
        wait_for(Duration::from_secs(5), ready)
    }

    fn wait_for(limit: Duration, mut ready: impl FnMut() -> bool) -> bool {
        let deadline = Instant::now() + limit;
        loop {
            if ready() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            thread::sleep(Duration::from_millis(5));
        }
    }

    #[test]
    fn slow_heartbeat_coalesces_ticks_without_blocking_other_sessions() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixStream;
        use std::sync::mpsc;

        let (home, runtime, original, _) = worker(false);
        let mut cfg = original.cfg.clone();
        // The helper's global worker must not become a third session heartbeat
        // backed by a real Omni process after changing to session addressing.
        runtime.end("si:test", "a", None).unwrap();
        runtime.disconnect("si:test").unwrap();
        let isi = cfg.isi.get_mut("a").unwrap();
        isi.primary_send_mode = Some("session".into());
        isi.heartbeat = Some(serde_yaml::to_value(json!({
            "next": "! value=$(cat heartbeat-interval); if [ \"$value\" = 30min ]; then touch long-interval; fi; printf %s \"$value\"",
            "message": "! printf '%s\\n' \"$ISI\" >> heartbeat-started; printf heartbeat"
        })).unwrap());
        fs::write(home.path().join("heartbeat-interval"), "0.01s").unwrap();
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        let options = |id: &str| SendOptions {
            id: Some(id.into()),
            new: true,
            ..Default::default()
        };
        let slow = runtime.worker(&connected, "a", &options("slow")).unwrap();
        let fast = runtime.worker(&connected, "a", &options("fast")).unwrap();
        // Heartbeats must deliver even when a managed app cannot be checked.
        state::write_json(
            &home.path().join(".silicon/auth-apps.json"),
            &vec!["/missing-heartbeat-app"],
        )
        .unwrap();
        assert_eq!(runtime.list("si:test", "a", false).unwrap().len(), 2);
        let fast_transport = transport(&fast);
        let (client, mut daemon) = UnixStream::pair().unwrap();
        daemon
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        *slow.client.lock().recover() = Some(Client::from_stream(client).unwrap());
        let (started_tx, started_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let slow_worker = slow.clone();
        let slow_transport = thread::spawn(move || {
            let mut reader = BufReader::new(daemon.try_clone().unwrap());
            let mut blocked_once = false;
            loop {
                let mut line = String::new();
                assert!(reader.read_line(&mut line).unwrap() > 0);
                let request: Value = serde_json::from_str(&line).unwrap();
                let stopped = request["op"] == "stop";
                if !blocked_once && request["text"] == "heartbeat" {
                    blocked_once = true;
                    started_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                }
                writeln!(
                    daemon,
                    "{}",
                    json!({"id":request["id"], "ok":true,
                    "result":if stopped {json!({"stopped":true})} else {json!({"accepted":true})}})
                )
                .unwrap();
                if stopped {
                    break;
                }
                slow_worker
                    .on_event(
                        Event::new(Event::START).saying(request["text"].as_str().unwrap()),
                        false,
                        0,
                    )
                    .unwrap();
            }
        });
        runtime.start_scheduler();
        let started = started_rx.recv_timeout(Duration::from_secs(5)).is_ok();
        let calls = || {
            fs::read_to_string(home.path().join("heartbeat-started"))
                .unwrap_or_default()
                .lines()
                .map(str::to_owned)
                .collect::<Vec<_>>()
        };
        // Keep one Omni RPC blocked over several real scheduler ticks. The fast
        // session proves the scheduler continues processing those ticks.
        // Intervals are floored at one second, so three heartbeats take a few seconds.
        let fast_progressed = wait_for(Duration::from_secs(20), || {
            calls().iter().filter(|s| *s == "a:fast").count() >= 3
        });
        let slow_calls = calls().iter().filter(|s| *s == "a:slow").count();
        fs::write(home.path().join("heartbeat-interval"), "30min").unwrap();
        let rescheduled = wait_until(|| home.path().join("long-interval").exists());
        let foreground = runtime.clone();
        let (sent_tx, sent_rx) = mpsc::channel();
        let sending = thread::spawn(move || {
            let result = foreground
                .send(
                    "si:test",
                    None,
                    "a",
                    "foreground",
                    &SendOptions {
                        id: Some("slow".into()),
                        ..Default::default()
                    },
                    false,
                )
                .and_then(|sent| sent.wait_started());
            sent_tx.send(result).unwrap();
        });
        // Release before assertions so a regression cannot strand the blocked RPC.
        release_tx.send(()).unwrap();
        let foreground_finished = sent_rx.recv_timeout(Duration::from_secs(5));
        let drained = wait_until(|| runtime.activities.load(Ordering::SeqCst) == 0);
        runtime.shutdown();
        sending.join().unwrap();
        slow_transport.join().unwrap();
        fast_transport.join().unwrap();
        assert!(started && fast_progressed && rescheduled);
        assert_eq!(slow_calls, 1, "slow heartbeat accumulated overlapping work");
        assert!(
            foreground_finished.unwrap().is_ok(),
            "foreground send was starved"
        );
        assert!(drained, "heartbeat work remained queued");
    }

    #[test]
    fn reconnect_keeps_blocked_event_and_heartbeat_work_out_of_the_replacement() {
        for heartbeat in [false, true] {
            let (old_home, runtime, first, _worker) = worker(false);
            let mut cfg = first.cfg.clone();
            runtime.disconnect("si:test").unwrap();
            let message =
                "! touch started; while ! test -f release; do sleep 0.01; done; printf old-message";
            if heartbeat {
                cfg.isi.get_mut("a").unwrap().heartbeat = Some(serde_yaml::to_value(json!({
                    "next":"! if test -f scheduled; then printf 30min; else touch scheduled; printf 0.01s; fi",
                    "message":message
                })).unwrap());
            } else {
                cfg.flow = serde_yaml::to_value(json!([
                    {"send":{"isi":"a", "message":message, "aggregate":false, "catch":[{"log":{"message":"caught: {self.error}"}}]}},
                    {"log":{"message":"continued"}}
                ])).unwrap();
            }
            connect(&runtime, cfg.clone());
            let old = runtime.get("si:test").unwrap();
            let event = if heartbeat {
                runtime.start_scheduler();
                None
            } else {
                let runtime = runtime.clone();
                Some(thread::spawn(move || {
                    runtime.event("si:test", json!({"tings":[{"id":"test-event", "type":"test", "data":{}, "metadata":{}}]}))
                }))
            };
            let started = wait_until(|| old_home.path().join("started").exists());
            runtime.disconnect("si:test").unwrap();
            let new_home = tempfile::tempdir().unwrap();
            cfg.home = new_home.path().to_owned();
            cfg.path = cfg.home.join("silicon.yaml");
            if heartbeat {
                cfg.isi.get_mut("a").unwrap().heartbeat = Some(
                    serde_yaml::to_value(json!({
                        "next":"! touch new-schedule; printf 30min", "message":"new-message"
                    }))
                    .unwrap(),
                );
            }
            connect(&runtime, cfg);
            let replacement = runtime.get("si:test").unwrap();
            let worker = runtime
                .worker(&replacement, "a", &SendOptions::default())
                .unwrap();
            let transport = transport(&worker);
            let scheduled =
                !heartbeat || wait_until(|| new_home.path().join("new-schedule").exists());
            // Release Bash before assertions, including when deadline isolation regresses.
            fs::write(old_home.path().join("release"), "").unwrap();
            let event_result = event.map(|event| event.join().unwrap());
            let finished = wait_until(|| runtime.activities.load(Ordering::SeqCst) == 0);
            runtime.shutdown();
            let messages = transport.join().unwrap();
            assert!(started, "work did not reach the blocking Bash command");
            assert!(
                scheduled,
                "replacement inherited the old heartbeat deadline"
            );
            assert!(finished, "old task did not finish");
            assert_ne!(old.generation, replacement.generation);
            assert!(
                messages.is_empty(),
                "old work reached replacement: {messages:?}"
            );
            if let Some(result) = event_result {
                // A send refused by the disconnect stops the flow uncaught, so the Ting
                // batch that ran it stays queued for the next connection.
                let error = result.unwrap_err();
                assert!(crate::flow::interrupted(&error), "{error:#}");
            }
            let log = fs::read_to_string(old_home.path().join(".silicon/silicon.log")).unwrap();
            assert!(log.contains("silicon disconnected"));
            if !heartbeat {
                assert!(
                    !log.contains("[caught:") && !log.contains("[continued]"),
                    "{log}"
                );
            }
        }
    }

    #[test]
    fn only_global_ephemeral_work_is_discarded() {
        // UNDERSTANDING.md: global + ephemeral is use-and-throw. Session-addressed
        // ephemeral work is reached by an id its caller holds, so it is kept and
        // archives when it retires, exactly like a persistent session.
        for (send_mode, kept) in [("session", true), ("global", false)] {
            let (dir, runtime, connected, worker) =
                worker_with_mode(true, serde_yaml::Value::Sequence(Vec::new()), send_mode);
            assert_eq!(
                worker.state.lock().recover().record.disposable,
                !kept,
                "{send_mode}"
            );
            // A record is written on its first send, not at creation.
            let (client, mut daemon) = std::os::unix::net::UnixStream::pair().unwrap();
            daemon
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
            let transport = thread::spawn(move || {
                use std::io::{BufRead, BufReader, Write};
                let mut reader = BufReader::new(daemon.try_clone().unwrap());
                let mut line = String::new();
                assert!(reader.read_line(&mut line).unwrap() > 0);
                let request: Value = serde_json::from_str(&line).unwrap();
                writeln!(
                    daemon,
                    "{}",
                    json!({"id": request["id"], "ok": true,
                           "result": json!({"accepted": true})})
                )
                .unwrap();
                daemon.flush().unwrap();
            });
            worker.send("working", None, false).unwrap();
            transport.join().unwrap();
            worker
                .on_event(Event::new(Event::START).saying("working"), false, 0)
                .unwrap();

            let events = dir
                .path()
                .join(".silicon/sessions/events")
                .join(format!("{}.jsonl", worker.session_id));
            assert_eq!(events.exists(), kept, "event history for {send_mode}");
            assert_eq!(
                !state::sessions(dir.path(), "a", false).unwrap().is_empty(),
                kept,
                "active record for {send_mode}"
            );

            worker.stop(None).unwrap();
            let archived = state::sessions(dir.path(), "a", true).unwrap();
            assert_eq!(archived.len(), usize::from(kept), "archive for {send_mode}");
            if kept {
                // Retired kept work is reachable again through --archived.
                assert_eq!(archived[0].id, "job");
                assert_eq!(archived[0].status, "archived");
                assert!(archived[0].ephemeral && !archived[0].disposable);
                assert!(state::sessions(dir.path(), "a", false).unwrap().is_empty());
                assert!(events.exists());
            }
            runtime.shutdown();
            drop(connected);
        }
    }

    #[test]
    fn send_entries_name_the_isi_that_sent_them() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixStream;

        // global-mode senders are named by ISI; session-mode ones carry the session.
        for send_mode in ["global", "session"] {
            let (dir, runtime, _connected, worker) =
                worker_with_mode(false, serde_yaml::Value::Sequence(Vec::new()), send_mode);
            let (client, mut daemon) = UnixStream::pair().unwrap();
            daemon
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
            let transport = thread::spawn(move || {
                let mut reader = BufReader::new(daemon.try_clone().unwrap());
                for _ in 0..2 {
                    let mut line = String::new();
                    assert!(reader.read_line(&mut line).unwrap() > 0);
                    let request: Value = serde_json::from_str(&line).unwrap();
                    writeln!(
                        daemon,
                        "{}",
                        json!({"id": request["id"], "ok": true,
                               "result": json!({"accepted": true})})
                    )
                    .unwrap();
                    daemon.flush().unwrap();
                }
            });
            let caller = runtime.caller(&worker.capability).unwrap();
            worker
                .send("from-an-isi", Some(caller.clone()), false)
                .unwrap();
            // A flow or interpreter send has no ISI sender and stays unprefixed.
            worker.send("from-the-flow", None, false).unwrap();
            transport.join().unwrap();

            let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
            let sends: Vec<&str> = log
                .lines()
                .filter(|line| line.starts_with("[send] "))
                .filter_map(|line| line.rsplit_once("] [").map(|(_, body)| body))
                .map(|body| body.trim_end_matches(']'))
                .collect();
            let expected = if send_mode == "session" {
                format!("from a:{}: from-an-isi", caller.session)
            } else {
                "from a: from-an-isi".to_owned()
            };
            assert_eq!(sends, vec![expected.as_str(), "from-the-flow"], "{log}");
            runtime.shutdown();
        }
    }

    #[test]
    fn captured_caller_expires_when_its_worker_or_connection_is_replaced() {
        let (_dir, runtime, connected, worker) = worker(false);
        let caller = runtime.caller(&worker.capability).unwrap();
        assert!(runtime.caller_connection(&caller).is_ok());
        worker.stop(None).unwrap();
        let replacement = runtime
            .worker(&connected, "a", &SendOptions::default())
            .unwrap();
        assert_eq!(replacement.session_id, caller.session);
        assert!(runtime.caller_connection(&caller).is_err());
        let fresh = runtime.caller(&replacement.capability).unwrap();
        assert!(runtime.caller_connection(&fresh).is_ok());
        runtime.disconnect("si:test").unwrap();
        connect(&runtime, connected.cfg.clone());
        let reconnected = runtime.get("si:test").unwrap();
        let resumed = runtime
            .worker(&reconnected, "a", &SendOptions::default())
            .unwrap();
        assert_eq!(resumed.session_id, caller.session);
        assert!(runtime.caller_connection(&caller).is_err());
        assert!(runtime.caller_connection(&fresh).is_err());
        assert!(runtime
            .caller_connection(&runtime.caller(&resumed.capability).unwrap())
            .is_ok());
        runtime.shutdown();
    }

    #[test]
    fn session_ephemeral_creation_requires_title_even_with_new_but_active_sends_do_not() {
        let (_dir, runtime, original, _worker) = worker(false);
        let mut cfg = original.cfg.clone();
        runtime.disconnect("si:test").unwrap();
        let isi = cfg.isi.get_mut("a").unwrap();
        isi.primary_send_mode = Some("session".into());
        isi.session_type = Some("ephemeral".into());
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        let mut options = SendOptions {
            id: Some("job".into()),
            new: true,
            ..Default::default()
        };
        assert!(runtime
            .worker(&connected, "a", &options)
            .err()
            .unwrap()
            .to_string()
            .contains("--title"));
        options.title = Some("Job title".into());
        let worker = runtime.worker(&connected, "a", &options).unwrap();
        options.title = None;
        options.new = false;
        assert!(Arc::ptr_eq(
            &worker,
            &runtime.worker(&connected, "a", &options).unwrap()
        ));
        runtime.shutdown();
    }

    #[test]
    fn stale_disconnected_connection_cannot_create_workers_or_capabilities() {
        let (_dir, runtime, connected, worker) = worker(false);
        runtime.disconnect("si:test").unwrap();
        assert!(worker.stopped.load(Ordering::SeqCst));
        assert!(runtime
            .worker(&connected, "a", &SendOptions::default())
            .is_err());
        assert!(connected.workers.lock().recover().is_empty());
        assert!(runtime.callers.read().recover().is_empty());
    }

    #[test]
    fn flow_aggregates_before_delivery_and_stashes_failure_without_send_catch() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixStream;
        use std::sync::mpsc;

        for failed in [false, true] {
            let flow = serde_yaml::from_str(
                r#"
- send:
    isi: a
    message: hello
    catch:
      - log: {message: 'caught: {self.error}'}
- log: {message: continued}
"#,
            )
            .unwrap();
            let (dir, runtime, _connected, worker) = worker_with_flow(false, flow);
            let (client, mut daemon) = UnixStream::pair().unwrap();
            daemon
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
            let (accepted, delivered) = mpsc::channel();
            let transport = thread::spawn(move || {
                let mut reader = BufReader::new(daemon.try_clone().unwrap());
                for operation in ["send", "stop"] {
                    let mut line = String::new();
                    assert!(reader.read_line(&mut line).unwrap() > 0);
                    let request: Value = serde_json::from_str(&line).unwrap();
                    assert_eq!(request["op"], operation);
                    let result = if operation == "send" {
                        json!({"accepted": true})
                    } else {
                        json!({"stopped": true})
                    };
                    writeln!(
                        daemon,
                        "{}",
                        json!({"id": request["id"], "ok": true, "result": result})
                    )
                    .unwrap();
                    if operation == "send" {
                        accepted.send(()).unwrap();
                    }
                }
            });
            let (ack, response) = mpsc::channel();
            let event_runtime = runtime.clone();
            let event = thread::spawn(move || {
                ack.send(event_runtime.event(
                    "si:test",
                    json!({"tings":[{"id":"test-event", "type": "test", "data": {}, "metadata": {}}]}),
                ))
                .unwrap();
            });
            delivered.recv_timeout(Duration::from_secs(5)).unwrap();
            assert!(response.recv_timeout(Duration::from_millis(50)).is_err());
            let log = dir.path().join(".silicon/silicon.log");
            assert!(fs::read_to_string(&log).unwrap().contains("[continued]"));
            if failed {
                let mut error = Event::new(Event::ERROR);
                error.kind = "crash".into();
                error.error = "blocked before START".into();
                worker.on_event(error, true, 1).unwrap();
            } else {
                worker
                    .on_event(Event::new(Event::START).saying("hello"), false, 0)
                    .unwrap();
            }
            let result = response.recv_timeout(Duration::from_secs(5)).unwrap();
            assert_eq!(result.unwrap()["status"], "ok");
            let log = fs::read_to_string(log).unwrap();
            assert!(log.contains("[continued]"));
            if failed {
                assert!(log.contains("blocked before START"));
                assert!(log.contains("exhausted 0 retries"));
                assert!(!log.contains("[caught:"));
            } else {
                assert!(!log.contains("[caught:"));
                assert!(!runtime.idle(), "ACK must not wait for provider END");
            }
            runtime.shutdown();
            event.join().unwrap();
            transport.join().unwrap();
        }
    }

    #[test]
    fn a_delivery_cut_by_disconnect_interrupts_the_flow_instead_of_its_catch() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixStream;
        use std::sync::mpsc;

        let flow = serde_yaml::from_str(
            r#"
- send:
    isi: a
    message: hello
    aggregate: false
    catch:
      - log: {message: 'caught: {self.error}'}
- log: {message: continued}
"#,
        )
        .unwrap();
        let (dir, runtime, _connected, worker) = worker_with_flow(false, flow);
        let (client, mut daemon) = UnixStream::pair().unwrap();
        daemon
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
        let (accepted, delivered) = mpsc::channel();
        let transport = thread::spawn(move || {
            let mut reader = BufReader::new(daemon.try_clone().unwrap());
            for operation in ["send", "stop"] {
                let mut line = String::new();
                assert!(reader.read_line(&mut line).unwrap() > 0);
                let request: Value = serde_json::from_str(&line).unwrap();
                assert_eq!(request["op"], operation);
                let result = if operation == "send" {
                    json!({"accepted": true})
                } else {
                    json!({"stopped": true})
                };
                writeln!(
                    daemon,
                    "{}",
                    json!({"id": request["id"], "ok": true, "result": result})
                )
                .unwrap();
                if operation == "send" {
                    accepted.send(()).unwrap();
                }
            }
        });
        let event_runtime = runtime.clone();
        let event = thread::spawn(move || {
            event_runtime.event(
                "si:test",
                json!({"tings":[{"id":"test-event", "type": "test", "data": {}, "metadata": {}}]}),
            )
        });
        delivered.recv_timeout(Duration::from_secs(5)).unwrap();
        // The session ends before the provider starts the message: not the step's failure.
        runtime.disconnect("si:test").unwrap();
        let error = event.join().unwrap().unwrap_err();
        assert!(crate::flow::interrupted(&error), "{error:#}");
        let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        assert!(
            !log.contains("[caught:") && !log.contains("[continued]"),
            "{log}"
        );
        assert!(log.contains("no catch runs"), "{log}");
        runtime.shutdown();
        transport.join().unwrap();
    }

    #[test]
    fn a_session_behind_an_unreadable_record_is_not_created_twice() {
        let (dir, runtime, connected, _worker) =
            worker_with_mode(false, serde_yaml::Value::Sequence(Vec::new()), "session");
        let broken = dir.path().join(".silicon/sessions/active/a/broken.json");
        fs::create_dir_all(broken.parent().unwrap()).unwrap();
        fs::write(&broken, "{\"id\":\"ticket-42\"").unwrap();
        let options = SendOptions {
            id: Some("ticket-42".into()),
            title: Some("ticket-42".into()),
            new: true,
            ..Default::default()
        };
        let error = format!(
            "{:#}",
            runtime.worker(&connected, "a", &options).err().unwrap()
        );
        assert!(
            error.contains("cannot start a new a session")
                && error.contains(&broken.display().to_string()),
            "{error}"
        );
        // The session already running is still reached.
        let running = SendOptions {
            id: Some("job".into()),
            ..Default::default()
        };
        runtime.worker(&connected, "a", &running).unwrap();
        fs::remove_file(&broken).unwrap();
        runtime.worker(&connected, "a", &options).unwrap();
        runtime.shutdown();
    }

    #[test]
    fn provider_removed_config_events_get_a_readable_log_line() {
        let (dir, _runtime, _connected, worker) = worker(false);
        let removed = Event::config("provider_removed")
            .from("claude-code-cli")
            .with("why", "crash")
            .with("left", json!(["codex-cli"]));
        worker.on_event(removed, false, 0).unwrap();
        let log = dir.path().join(".silicon/silicon.log");
        let lines = fs::read_to_string(&log).unwrap();
        assert!(
            lines.contains("[config] [a/"),
            "raw event still logged: {lines}"
        );
        assert!(
            lines.contains("[provider_removed] [a/")
                && lines.contains(
                    "[Omni removed provider claude-code-cli: crash; remaining providers: codex-cli]"
                ),
            "{lines}"
        );
        let last = Event::config("provider_removed")
            .from("codex-cli")
            .with("why", "limit")
            .with("left", json!([]));
        worker.on_event(last, true, 0).unwrap();
        let lines = fs::read_to_string(&log).unwrap();
        assert!(
            lines.contains("[Omni removed provider codex-cli: limit; no providers remain; Omni retries them all on the next send]"),
            "{lines}"
        );
        let other = Event::config("retune").from("codex-cli");
        worker.on_event(other, true, 0).unwrap();
        assert_eq!(
            fs::read_to_string(&log)
                .unwrap()
                .matches("[provider_removed]")
                .count(),
            2
        );
        // `why` is only a kind: the removal quotes what that provider said this turn.
        worker
            .on_event(
                Event::failure("stderr", "Error: 401 {\"type\":\"authentication_error\"}")
                    .from("claude-code-cli"),
                false,
                1,
            )
            .unwrap();
        worker
            .on_event(
                Event::failure("stderr", "unrelated").from("codex-cli"),
                false,
                1,
            )
            .unwrap();
        let removed = Event::config("provider_removed")
            .from("claude-code-cli")
            .with("why", "auth")
            .with("left", json!(["codex-cli"]));
        worker.on_event(removed, false, 1).unwrap();
        let lines = fs::read_to_string(&log).unwrap();
        let line = lines.lines().last().unwrap();
        assert!(
            line.ends_with("[Omni removed provider claude-code-cli: auth; remaining providers: codex-cli\\nclaude-code-cli reported stderr: Error: 401 {\"type\":\"authentication_error\"}]"),
            "{line}"
        );
        // A completed turn starts the next one without the old chatter.
        worker.on_event(Event::new(Event::END), false, 1).unwrap();
        assert!(worker.state.lock().recover().trouble.is_empty());
        // Missing fields are said to be missing, and the raw event shows what did arrive.
        let bare = Event::config("provider_removed")
            .from("gemini-cli")
            .with("detail", "quota");
        worker.on_event(bare, false, 1).unwrap();
        let lines = fs::read_to_string(&log).unwrap();
        let line = lines.lines().last().unwrap();
        assert!(
            line.contains("[Omni removed provider gemini-cli: no reason given; remaining providers not reported\\nevent: {")
                && line.contains("\"detail\":\"quota\""),
            "{line}"
        );
    }

    #[test]
    fn provider_failure_carries_its_event_and_everything_said_before_it() {
        let (dir, _runtime, _connected, worker) = worker(false);
        let delivery = pending(&worker, "hello");
        let chatter = Event::failure(
            "stderr",
            "Error: ENOENT: no such file or directory, open '/home/silicon/.claude.json'",
        )
        .from("claude-code-cli");
        worker.on_event(chatter, false, 1).unwrap();
        let retried = Event::failure("crash", "socket hang up")
            .from("claude-code-cli")
            .with("willRetry", true);
        worker.on_event(retried, true, 1).unwrap();
        assert!(delivery.receipt.result.lock().recover().is_none());
        let crash = Event::failure("crash", "claude exited with code 1")
            .from("claude-code-cli")
            .with("exitCode", 1);
        worker.on_event(crash, true, 1).unwrap();
        let error = format!("{:#}", delivery.wait_started().unwrap_err());
        for said in [
            "claude-code-cli reported crash: claude exited with code 1",
            "\"exitCode\":1",
            "claude-code-cli reported stderr: Error: ENOENT: no such file or directory, open '/home/silicon/.claude.json'",
            "claude-code-cli reported crash: socket hang up",
        ] {
            assert!(error.contains(said), "missing {said:?} in {error}");
        }
        let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        assert!(
            log.contains("claude exited with code 1")
                && log.contains("open '/home/silicon/.claude.json'"),
            "{log}"
        );
    }

    #[test]
    fn omnid_dying_at_startup_reaches_the_sender_with_its_status_and_output() {
        use std::os::unix::fs::PermissionsExt;
        // The fake daemon is found on SILICON_HOME/.silicon/bin; OMNI_DAEMON would bypass it.
        if std::env::var_os("OMNI_DAEMON").is_some() {
            return;
        }
        let (dir, runtime, _connected, worker) = worker(false);
        let app = dir.path().join(".silicon/bin/omnid");
        fs::create_dir_all(app.parent().unwrap()).unwrap();
        fs::write(
            &app,
            "#!/bin/sh\n\
             echo 'omnid: provider registry is corrupt: {\"code\":\"E_REGISTRY\"}' >&2\n\
             echo \"omnid: listening as $SI_TOKEN\"\n\
             exit 3\n",
        )
        .unwrap();
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700)).unwrap();
        let omni_home = dir
            .path()
            .join(".silicon/omni")
            .join(worker.session_id.to_string());
        fs::create_dir_all(&omni_home).unwrap();
        fs::write(omni_home.join("daemon.log"), "a previous run's output\n").unwrap();
        let error = format!(
            "{:#}",
            worker
                .send("hello", None, false)
                .err()
                .expect("omnid exited")
        );
        let _ = fs::remove_file(format!(
            "/tmp/silicon-{}/{}",
            unsafe { libc::getuid() },
            worker.session_id.simple()
        ));
        runtime.shutdown();
        for said in [
            "starting Omni for a",
            "omnid exited (exit status: 3)",
            "omnid: provider registry is corrupt: {\"code\":\"E_REGISTRY\"}",
            "omnid: listening as [redacted]",
        ] {
            assert!(error.contains(said), "missing {said:?} in {error}");
        }
        assert!(!error.contains(&worker.capability), "{error}");
        assert!(!error.contains("a previous run's output"), "{error}");
        // The cause reads before the daemon's own account of it.
        assert!(
            error.find("before accepting connections").unwrap()
                < error.find("omnid wrote to").unwrap(),
            "{error}"
        );
    }

    #[test]
    fn omnid_dying_after_start_reaches_the_sender_with_its_status_and_output() {
        let (dir, runtime, _connected, worker) = worker(false);
        let omni_home = dir
            .path()
            .join(".silicon/omni")
            .join(worker.session_id.to_string());
        fs::create_dir_all(&omni_home).unwrap();
        let log = omni_home.join("daemon.log");
        fs::write(&log, "a previous run's output\n").unwrap();
        let start = fs::metadata(&log).unwrap().len();
        let output = OpenOptions::new().append(true).open(&log).unwrap();
        let mut child = std::process::Command::new("/bin/sh")
            .arg("-c")
            .arg(
                "echo 'omnid: panicked at registry.rs:12: {\"code\":\"E_PANIC\"}' >&2\n\
                 echo \"omnid: last session token $SI_TOKEN\"\n\
                 exit 4",
            )
            .env("SI_TOKEN", &worker.capability)
            .stdout(output.try_clone().unwrap())
            .stderr(output)
            .spawn()
            .unwrap();
        assert!(wait_until(|| child.try_wait().unwrap().is_some()));
        *worker.daemon.lock().recover() = Some(Daemon { child, log, start });
        // The daemon's end of the socket is gone, as it is when omnid dies.
        let (client, daemon) = std::os::unix::net::UnixStream::pair().unwrap();
        drop(daemon);
        *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
        let error = format!(
            "{:#}",
            worker.send("hello", None, false).err().expect("omnid died")
        );
        runtime.shutdown();
        let refused = format!(
            "Omni did not accept the message for session {}: ",
            worker.session_id
        );
        for said in [
            refused.as_str(),
            "exited: exit status: 4\nomnid wrote to ",
            "omnid: panicked at registry.rs:12: {\"code\":\"E_PANIC\"}",
            "omnid: last session token [redacted]",
        ] {
            assert!(error.contains(said), "missing {said:?} in {error}");
        }
        assert!(!error.contains(&worker.capability), "{error}");
        assert!(!error.contains("a previous run's output"), "{error}");
    }

    #[test]
    fn omni_refusing_a_send_reaches_the_sender_verbatim() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::net::UnixStream;

        let (_dir, runtime, _connected, worker) = worker(false);
        let (client, mut daemon) = UnixStream::pair().unwrap();
        daemon
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
        let refusal = "session is wedged: {\"reason\":\"provider_lock\",\"holder\":\"codex-cli\"}";
        let transport = thread::spawn(move || {
            let mut reader = BufReader::new(daemon.try_clone().unwrap());
            let mut line = String::new();
            assert!(reader.read_line(&mut line).unwrap() > 0);
            let request: Value = serde_json::from_str(&line).unwrap();
            writeln!(
                daemon,
                "{}",
                json!({"id": request["id"], "ok": false, "error": refusal})
            )
            .unwrap();
            daemon.flush().unwrap();
        });
        let error = format!(
            "{:#}",
            worker.send("hello", None, false).err().expect("refused")
        );
        transport.join().unwrap();
        runtime.shutdown();
        assert!(
            error.contains(&format!(
                "Omni did not accept the message for session {}: {refusal}",
                worker.session_id
            )),
            "{error}"
        );
    }

    #[test]
    fn unreadable_settings_and_session_history_name_the_value_and_file() {
        let (dir, runtime, connected, worker) = worker(false);
        let soon = format!(
            "{:#}",
            interval(
                &serde_yaml::Value::String("soon".into()),
                &connected.cfg,
                "a"
            )
            .unwrap_err()
        );
        assert!(
            soon.contains("interval \"soon\" must be a number")
                && soon.contains("invalid float literal"),
            "{soon}"
        );
        let never = format!(
            "{:#}",
            interval(&serde_yaml::Value::String("0s".into()), &connected.cfg, "a").unwrap_err()
        );
        assert!(
            never.contains("interval \"0s\" must be positive"),
            "{never}"
        );
        let events = dir
            .path()
            .join(".silicon/sessions/events")
            .join(format!("{}.jsonl", worker.session_id));
        fs::create_dir_all(events.parent().unwrap()).unwrap();
        fs::write(&events, "{\"type\":\"start\"}\nhalf-written {\"type\n").unwrap();
        // A torn line no longer hides the rest of the history: it is skipped, counted and
        // shown whole with the parser's reason and the file it is in.
        let shown = runtime.show("si:test", "a", None).unwrap();
        assert_eq!(shown["events"], json!([{"type":"start"}]), "{shown}");
        let unreadable = &shown["unreadable"];
        assert_eq!(unreadable["count"], 1, "{shown}");
        assert_eq!(unreadable["file"], json!(events), "{shown}");
        assert_eq!(unreadable["lines"][0]["line"], "half-written {\"type");
        assert!(
            unreadable["lines"][0]["error"]
                .as_str()
                .unwrap()
                .contains("line 1 column"),
            "{shown}"
        );
        // Sub-second schedules are floored at one second.
        assert_eq!(
            interval(
                &serde_yaml::Value::String("0.01s".into()),
                &connected.cfg,
                "a"
            )
            .unwrap(),
            Duration::from_secs(1)
        );
        runtime.shutdown();
    }

    #[test]
    fn outgoing_timeouts_reuse_the_accepted_receipt_until_it_settles() {
        let (_home, runtime, connected, worker) = worker(false);
        let daemon = transport_ending(&worker, false);
        let delivery = flow::Delivery {
            isi: "a".into(),
            session_id: None,
            message: "retry after rejection".into(),
        };
        for succeeded in [true, false] {
            let key = format!("{}/0", Uuid::new_v4());
            let receipt = Arc::new(SendReceipt {
                result: Mutex::new(None),
                changed: Condvar::new(),
                deadline: Instant::now(),
            });
            let id = Uuid::new_v4();
            connected.pending_outgoing.lock().recover().insert(
                key.clone(),
                Sent {
                    session: worker.state.lock().recover().record.clone(),
                    id,
                    receipt: receipt.clone(),
                },
            );
            for _ in 0..2 {
                let error = runtime
                    .deliver_outgoing(&connected, &key, &delivery)
                    .unwrap_err();
                assert!(error.is::<crate::outbox::AwaitingReceipt>(), "{error:#}");
                assert_eq!(connected.pending_outgoing.lock().recover()[&key].id, id);
            }
            receipt.finish(if succeeded {
                Ok(())
            } else {
                Err("provider rejected it".into())
            });
            let result = runtime.deliver_outgoing(&connected, &key, &delivery);
            assert_eq!(result.is_ok(), succeeded);
            assert!(!connected
                .pending_outgoing
                .lock()
                .recover()
                .contains_key(&key));
            if !succeeded {
                runtime
                    .deliver_outgoing(&connected, &key, &delivery)
                    .unwrap();
            }
        }
        runtime.shutdown();
        assert_eq!(daemon.join().unwrap(), ["retry after rejection"]);
    }

    #[test]
    fn accepted_sends_keep_their_receipts_when_recording_fails() {
        for broken_log in [false, true] {
            let (home, runtime, _connected, worker) = worker(false);
            let daemon = transport_ending(&worker, false);
            let path = if broken_log {
                home.path().join(".silicon/silicon.log")
            } else {
                worker.state.lock().recover().record.path(home.path())
            };
            if path.exists() {
                fs::remove_file(&path).unwrap();
            }
            fs::create_dir_all(&path).unwrap();
            let sent = worker.send("accepted once", None, false).unwrap();
            sent.wait_started().unwrap();
            fs::remove_dir(&path).unwrap();
            runtime.shutdown();
            assert_eq!(daemon.join().unwrap(), ["accepted once"]);
        }
    }

    #[test]
    fn an_uncertain_transport_ack_keeps_the_provider_receipt() {
        use std::io::{BufRead, BufReader, Write};
        let (_home, runtime, _connected, worker) = worker(false);
        let (client, mut daemon) = UnixStream::pair().unwrap();
        *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
        let received = worker.clone();
        let server = thread::spawn(move || {
            let mut reader = BufReader::new(daemon.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["op"], "send");
            // Omni accepted the message, but its transport reply cannot be decoded.
            writeln!(
                daemon,
                "{}",
                json!({"id":request["id"],"ok":true,"result":{}})
            )
            .unwrap();
            received
                .on_event(
                    Event::new(Event::START).saying("accepted despite bad ack"),
                    false,
                    0,
                )
                .unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            let stop: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(stop["op"], "stop");
            writeln!(
                daemon,
                "{}",
                json!({"id":stop["id"],"ok":true,"result":{"stopped":true}})
            )
            .unwrap();
        });
        let sent = worker
            .send("accepted despite bad ack", None, false)
            .unwrap();
        sent.wait_started().unwrap();
        runtime.shutdown();
        server.join().unwrap();
    }

    #[test]
    fn a_heartbeat_recovers_stashed_messages_before_its_own_message() {
        let (_home, runtime, connected, worker) = worker(false);
        let daemon = transport(&worker);
        let run = Uuid::new_v4().to_string();
        connected
            .outbox
            .deliver(
                &run,
                0,
                &flow::Delivery {
                    isi: "a".into(),
                    session_id: None,
                    message: "stashed first".into(),
                },
                0,
                |_, _| bail!("offline"),
                || Ok(()),
            )
            .unwrap();
        connected.outbox.complete(&run).unwrap();
        run_heartbeat(
            &runtime,
            &connected,
            "a",
            &serde_yaml::from_str("next: 13min\nmessage: heartbeat").unwrap(),
            "a",
            None,
            Job::Fire(Utc::now()),
        )
        .unwrap();
        assert!(wait_until(|| worker
            .state
            .lock()
            .recover()
            .pending
            .is_empty()));
        runtime.shutdown();
        assert_eq!(daemon.join().unwrap(), ["stashed first", "heartbeat"]);
    }

    #[test]
    fn receipts_ack_provider_delivery_before_end_and_native_next_turns_do_not_retire_early() {
        let (_dir, _runtime, _connected, worker) = worker(true);
        let first = pending(&worker, "one");
        let second = pending(&worker, "two");
        assert!(first.receipt.result.lock().recover().is_none());
        let mut start = Event::new(Event::START).saying("one");
        start.turn = 0;
        worker.on_event(start, false, 0).unwrap();
        first.wait_started().unwrap();
        assert!(second.receipt.result.lock().recover().is_none());
        let mut injected = Event::new(Event::INJECTED).saying("two");
        injected.turn = 0;
        injected.extra.insert("landed".into(), json!("next_turn"));
        worker.on_event(injected, false, 0).unwrap();
        second.wait_started().unwrap();
        worker.on_event(Event::new(Event::END), false, 0).unwrap();
        assert!(!worker.stopped.load(Ordering::SeqCst));
        assert_eq!(worker.state.lock().recover().pending.len(), 2);
        worker.on_event(Event::new(Event::END), true, 0).unwrap();
        assert!(worker.stopped.load(Ordering::SeqCst));
        assert!(worker.state.lock().recover().pending.is_empty());
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
        assert!(delivery.receipt.result.lock().recover().is_none());
        worker.fail("socket lost");
        assert!(delivery.wait_started().is_err());
        assert!(worker.stopped.load(Ordering::SeqCst));
        let replacement = runtime
            .worker(&connected, "a", &SendOptions::default())
            .unwrap();
        assert!(!Arc::ptr_eq(&worker, &replacement));
        assert_eq!(replacement.session_id, worker.session_id);
        assert!(!replacement.stopped.load(Ordering::SeqCst));
        let blocked = pending(&replacement, "cannot route before START");
        let mut error = Event::new(Event::ERROR);
        error.kind = "blocked".into();
        error.error = "no available providers".into();
        replacement.on_event(error, true, 1).unwrap();
        assert!(blocked.wait_started().is_err());
        assert!(replacement.state.lock().recover().pending.is_empty());
        assert!(replacement.stopped.load(Ordering::SeqCst));
        assert!(runtime.idle());
    }

    #[test]
    fn retirement_rechecks_pending_work_after_waiting_for_send_lock() {
        let (_dir, _runtime, _connected, worker) = worker(true);
        let client = worker.client.lock().recover();
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
                "si:test",
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
        let mut record = worker.state.lock().recover().record.clone();
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
            .recover()
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
    fn disconnect_ting_unhook_failure_still_removes_connection_and_capabilities() {
        let (_dir, runtime, connected, first) = worker(false);
        let cfg = connected.cfg.clone();
        runtime.disconnect("si:test").unwrap();
        assert!(first.stopped.load(Ordering::SeqCst));
        use std::os::unix::fs::PermissionsExt;
        let app = cfg.home.join(".silicon/bin/ting");
        std::fs::create_dir_all(app.parent().unwrap()).unwrap();
        std::fs::write(
            &app,
            r#"#!/bin/sh
case "$*" in
  'iam --json') echo '{"app_id":"ting"}' ;;
  'unhook retained-hook --json') echo 'hook is held by another org' >&2; exit 1 ;;
  *) exit 2 ;;
esac
"#,
        )
        .unwrap();
        std::fs::set_permissions(&app, std::fs::Permissions::from_mode(0o700)).unwrap();
        state::write_json(
            &cfg.home
                .join(".silicon/ting/si:test")
                .join(cfg.silicon.silicon_org.as_deref().unwrap_or_default())
                .join("hook.json"),
            &"retained-hook",
        )
        .unwrap();
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        let second = runtime
            .worker(&connected, "a", &SendOptions::default())
            .unwrap();
        let delivery = pending(&second, "will be cancelled");
        let error = format!("{:#}", runtime.disconnect("si:test").unwrap_err());
        assert!(
            error.starts_with("si:test was disconnected with errors:\nremoving the Ting webhook: ")
                && error.contains("exit status: 1")
                && error.contains("hook is held by another org"),
            "{error}"
        );
        assert!(runtime.get("si:test").is_err());
        assert!(runtime.caller(&second.capability).is_none());
        assert!(delivery.wait_started().is_err());
    }
    /// Homes whose next Ting inbox iteration panics, for the panic-isolation test.
    static INBOX_PANICS: Mutex<Vec<PathBuf>> = Mutex::new(Vec::new());

    pub(super) fn injected_inbox_panic(home: &Path) {
        let mut homes = INBOX_PANICS.lock().recover();
        if let Some(index) = homes.iter().position(|h| h == home) {
            homes.remove(index);
            drop(homes);
            panic!("injected inbox panic");
        }
    }

    /// Stands in for omnid when a test makes this binary a Silicon's omnid (see
    /// `fake_omnid_for`). Like omnid it takes the session's omnid.pid lock or exits 1 naming
    /// it, then answers the protocol on $OMNI_HOME/omnid.sock. Every request is recorded in
    /// $OMNI_HOME/requests.jsonl, and a send is answered with START then END.
    #[test]
    fn fake_omnid() {
        use std::io::Write;
        use std::os::unix::net::UnixListener;
        if std::env::var_os("SILICON_FAKE_OMNID").is_none() {
            return;
        }
        let home = PathBuf::from(std::env::var_os("OMNI_HOME").unwrap());
        let lock_path = home.join("omnid.pid");
        let mut lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&lock_path)
            .unwrap();
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            crate::stderr_line(&format!(
                "omnid: another omnid already owns {}",
                lock_path.display()
            ));
            std::process::exit(1);
        }
        lock.set_len(0).unwrap();
        write!(lock, "{}", std::process::id()).unwrap();
        let socket = home.join("omnid.sock");
        if socket.exists() {
            if UnixStream::connect(&socket).is_ok() {
                std::process::exit(1);
            }
            fs::remove_file(&socket).unwrap();
        }
        let listener = UnixListener::bind(&socket).unwrap();
        for stream in listener.incoming() {
            let Ok(stream) = stream else {
                continue;
            };
            let home = home.clone();
            thread::spawn(move || fake_omnid_connection(stream, &home));
        }
    }

    fn fake_omnid_connection(stream: UnixStream, home: &Path) {
        use std::io::{BufRead, BufReader, Write};
        let Ok(mut writer) = stream.try_clone() else {
            return;
        };
        let snapshot = |session: &str, seq: i64, in_turn: bool| silicon_omni::Snapshot {
            session: session.into(),
            status: "waiting".into(),
            ask: Ask::key("fast"),
            providers: vec!["claude-code-cli".into()],
            provider: "claude-code-cli".into(),
            model: "fake".into(),
            effort: "medium".into(),
            cwd: String::new(),
            seq,
            in_turn,
            queued: 0,
        };
        let mut seq = 0;
        for line in BufReader::new(stream).lines() {
            let Ok(line) = line else {
                return;
            };
            let request: Value = serde_json::from_str(&line).unwrap();
            state::append_json(&home.join("requests.jsonl"), &request).unwrap();
            let session = request["session"].as_str().unwrap_or_default().to_owned();
            let op = request["op"].as_str().unwrap_or_default().to_owned();
            let result = match op.as_str() {
                "ping" => json!({"protocol": PROTOCOL, "version": "fake",
                    "pid": std::process::id(), "started": "fake", "home": home}),
                "providers" => json!(["claude-code-cli"]),
                "open" => json!({"session": session, "replayed": 0, "listeners": 1,
                    "snapshot": snapshot(&session, seq, false)}),
                "send" | "set" => json!({"accepted": true}),
                "stop" => json!({"stopped": true}),
                "shutdown" => json!({"stopping": true}),
                _ => json!({}),
            };
            let reply = json!({"id": request["id"], "ok": true, "result": result});
            if writeln!(writer, "{reply}").is_err() {
                return;
            }
            if op == "send" {
                let text = request["text"].as_str().unwrap_or_default();
                for (kind, in_turn) in [(Event::START, true), (Event::END, false)] {
                    seq += 1;
                    let mut event = Event::new(kind);
                    if kind == Event::START {
                        event = event.saying(text);
                    }
                    event.seq = seq;
                    event.turn = 0;
                    let frame = silicon_omni::Frame::event(
                        &session,
                        event,
                        snapshot(&session, seq, in_turn),
                    );
                    if writeln!(writer, "{}", serde_json::to_string(&frame).unwrap()).is_err() {
                        return;
                    }
                }
            }
            if op == "shutdown" && std::env::var_os("FAKE_OMNID_IGNORE_SHUTDOWN").is_none() {
                std::process::exit(0);
            }
        }
    }

    /// Make this test binary the omnid that the Silicon at `home` starts.
    fn fake_omnid_for(home: &Path) {
        use std::os::unix::fs::PermissionsExt;
        let omnid = home.join(".silicon/bin/omnid");
        fs::create_dir_all(omnid.parent().unwrap()).unwrap();
        fs::write(
            &omnid,
            format!(
                "#!/bin/sh\nSILICON_FAKE_OMNID=1\nexport SILICON_FAKE_OMNID\nexec '{}' --exact runtime::tests::fake_omnid --nocapture --test-threads=1\n",
                std::env::current_exe().unwrap().display()
            ),
        )
        .unwrap();
        fs::set_permissions(&omnid, fs::Permissions::from_mode(0o700)).unwrap();
    }

    fn short_home(session: Uuid) -> PathBuf {
        PathBuf::from(format!(
            "/tmp/silicon-{}/{}",
            unsafe { libc::getuid() },
            session.simple()
        ))
    }

    fn requests(omni_home: &Path) -> Vec<Value> {
        fs::read_to_string(omni_home.join("requests.jsonl"))
            .unwrap_or_default()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    /// Tests that start real processes wait longer: fsync and process start can be slow on a
    /// loaded machine.
    const SLOW: Duration = Duration::from_secs(20);

    /// Retire sessions idle for an hour, trying again while a listener is still finishing
    /// the END it just handled (a retirement pass skips a session whose lock is busy).
    fn retire_quiet_soon(runtime: &Runtime, connected: &Connected) -> usize {
        let mut retired = 0;
        wait_for(SLOW, || {
            retired += runtime.retire_quiet(connected, TimeDelta::minutes(60));
            retired > 0
        });
        retired
    }

    fn silicon_log(home: &Path) -> String {
        fs::read_to_string(home.join(".silicon/silicon.log")).unwrap_or_default()
    }

    #[test]
    fn a_stale_omnid_is_stopped_never_adopted_and_the_open_names_its_providers() {
        // The fake daemon is found on SILICON_HOME/.silicon/bin; OMNI_DAEMON would bypass it.
        if std::env::var_os("OMNI_DAEMON").is_some() {
            return;
        }
        let (dir, runtime, _connected, worker) = worker(false);
        fake_omnid_for(dir.path());
        let omni_home = dir
            .path()
            .join(".silicon/omni")
            .join(worker.session_id.to_string());
        state::private_dir(&omni_home).unwrap();
        let alias = short_home(worker.session_id);
        state::private_dir(alias.parent().unwrap()).unwrap();
        symlink(&omni_home, &alias).unwrap();
        // An omnid an earlier interpreter run left on this session, deaf to a polite shutdown.
        let mut stale = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "runtime::tests::fake_omnid",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("SILICON_FAKE_OMNID", "1")
            .env("FAKE_OMNID_IGNORE_SHUTDOWN", "1")
            .env("OMNI_HOME", &alias)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let listening = wait_for(SLOW, || {
            UnixStream::connect(alias.join("omnid.sock")).is_ok()
        });
        let sent = worker.send("hello", None, false);
        let stale_stopped = wait_for(SLOW, || stale.try_wait().unwrap().is_some());
        let ours = worker
            .daemon
            .lock()
            .recover()
            .as_ref()
            .map(|daemon| daemon.child.id());
        let settled = wait_for(SLOW, || worker.state.lock().recover().pending.is_empty());
        runtime.shutdown();
        let _ = stale.kill();
        let _ = stale.wait();
        let _ = fs::remove_file(&alias);
        assert!(listening, "the stale omnid never listened");
        let log = silicon_log(dir.path());
        if let Err(error) = sent {
            panic!("{error:#}\n{log}");
        }
        assert!(stale_stopped && settled, "{log}");
        assert!(ours.is_some_and(|pid| pid != stale.id()), "{log}");
        for said in [
            format!("is answered by omnid pid {} (started fake)", stale.id()),
            "omnid: another omnid already owns".to_owned(),
            format!("asked stale omnid pid {} to shut down", stale.id()),
            "sent it SIGTERM".to_owned(),
            format!("stale omnid pid {} stopped after SIGTERM", stale.id()),
        ] {
            assert!(log.contains(&said), "missing {said:?} in {log}");
        }
        // A lone all-available-providers still opens with the probed list, so Omni rewrites
        // the session's saved providers instead of reusing a list an auth error shrank.
        let open = requests(&omni_home)
            .into_iter()
            .find(|request| request["op"] == "open")
            .unwrap();
        assert_eq!(open["providers"], json!(["claude-code-cli"]), "{open}");
    }

    #[test]
    fn idle_session_workers_retire_and_resume_the_same_omni_session() {
        if std::env::var_os("OMNI_DAEMON").is_some() {
            return;
        }
        let (dir, runtime, connected, worker) =
            worker_with_mode(false, serde_yaml::Value::Sequence(Vec::new()), "session");
        fake_omnid_for(dir.path());
        let omni_home = dir
            .path()
            .join(".silicon/omni")
            .join(worker.session_id.to_string());
        let job = SendOptions {
            id: Some("job".into()),
            ..Default::default()
        };
        let first = runtime
            .send("si:test", None, "a", "first", &job, false)
            .and_then(|sent| sent.wait_started());
        let settled = wait_for(SLOW, || {
            let state = worker.state.lock().recover();
            state.pending.is_empty() && !state.turn_open
        });
        // Recently active sessions stay.
        let kept = runtime.retire_quiet(&connected, TimeDelta::minutes(60));
        worker.state.lock().recover().last_activity = Utc::now() - TimeDelta::minutes(61);
        let retired = retire_quiet_soon(&runtime, &connected);
        let daemon_gone = worker.daemon.lock().recover().is_none();
        let records = state::sessions(dir.path(), "a", false).unwrap();
        // A send through the retired worker says so; through the runtime it resumes.
        let stale = worker.send("through the old worker", None, false);
        let second = runtime
            .send("si:test", None, "a", "second", &job, false)
            .and_then(|sent| sent.wait_started());
        let replacement = connected.workers.lock().recover().values().next().cloned();
        let reopened = wait_for(SLOW, || {
            requests(&omni_home)
                .iter()
                .filter(|request| request["op"] == "open")
                .count()
                == 2
        });
        runtime.shutdown();
        let _ = fs::remove_file(short_home(worker.session_id));
        let log = silicon_log(dir.path());
        for sent in [first, second] {
            if let Err(error) = sent {
                panic!("{error:#}\n{log}");
            }
        }
        assert!(settled, "{log}");
        assert!(reopened, "{:#?}\n{log}", requests(&omni_home));
        assert_eq!((kept, retired), (0, 1));
        assert!(worker.stopped.load(Ordering::SeqCst) && worker.retired.load(Ordering::SeqCst));
        assert!(daemon_gone);
        // The record stays active, idle, under the same Omni session.
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].session_id, worker.session_id);
        assert_eq!(records[0].status, "idle");
        assert!(stale.err().unwrap().is::<Retired>());
        let replacement = replacement.unwrap();
        assert!(!Arc::ptr_eq(&replacement, &worker));
        assert_eq!(replacement.session_id, worker.session_id);
        for open in requests(&omni_home)
            .iter()
            .filter(|request| request["op"] == "open")
        {
            assert_eq!(open["session"], json!(worker.session_id.to_string()));
        }
        assert!(
            log.contains("[runtime] [a:job/")
                && log.contains("retired idle session job (Omni session")
                && log.contains("after 61 minutes without a send or an event"),
            "{log}"
        );
    }

    #[test]
    fn an_ephemeral_reply_resumes_a_caller_retired_while_it_waited() {
        if std::env::var_os("OMNI_DAEMON").is_some() {
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let mut cfg: Config = serde_yaml::from_str(
            r#"
silicon: {id: 'si:test', org_id: org, SILICON_ORG: org, token: test, timezone: UTC, inference_providers: [all-available-providers]}
isi:
  a: {model: fast, primary_send_mode: session, session_type: persistent}
  b: {model: fast, primary_send_mode: global, session_type: ephemeral}
access: {a: [b], b: []}
flow: []
"#,
        )
        .unwrap();
        cfg.home = dir.path().to_owned();
        cfg.path = dir.path().join("silicon.yaml");
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        fake_omnid_for(dir.path());
        let job = SendOptions {
            id: Some("job".into()),
            title: Some("job".into()),
            new: true,
            ..Default::default()
        };
        let asking = runtime.worker(&connected, "a", &job).unwrap();
        let started = runtime
            .send("si:test", None, "a", "delegate", &job, false)
            .and_then(|sent| sent.wait_started());
        let settled = wait_for(SLOW, || asking.state.lock().recover().pending.is_empty());
        let caller = runtime.caller(&asking.capability).unwrap();
        // b took a request from a's session, and a retires while b works on it.
        let answering = runtime
            .worker(&connected, "b", &SendOptions::default())
            .unwrap();
        answering.state.lock().recover().pending.push(Dispatch {
            id: Uuid::new_v4(),
            message: "work".into(),
            turn: Some(0),
            receipt: SendReceipt::new(),
            origin: Some(caller),
            heartbeat: false,
        });
        asking.state.lock().recover().last_activity = Utc::now() - TimeDelta::minutes(61);
        let retired = retire_quiet_soon(&runtime, &connected);
        answering
            .on_event(Event::new(Event::TEXT).saying("the answer"), false, 0)
            .unwrap();
        answering.on_event(Event::new(Event::END), true, 0).unwrap();
        let omni_home = dir
            .path()
            .join(".silicon/omni")
            .join(asking.session_id.to_string());
        let replied = wait_for(SLOW, || {
            requests(&omni_home).iter().any(|request| {
                request["op"] == "send" && request["text"] == "b completed:\nthe answer"
            })
        });
        runtime.shutdown();
        let _ = fs::remove_file(short_home(asking.session_id));
        let log = silicon_log(dir.path());
        if let Err(error) = started {
            panic!("{error:#}\n{log}");
        }
        assert!(settled, "{log}");
        assert_eq!(retired, 1);
        assert!(replied, "{log}");
        assert!(!log.contains("ended before the ephemeral reply"), "{log}");
    }

    #[test]
    fn an_ephemeral_reply_reaches_a_retired_caller_that_a_later_send_resumed_and_keeps_busy() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg: Config = serde_yaml::from_str(
            r#"
silicon: {id: 'si:test', org_id: org, SILICON_ORG: org, token: test, timezone: UTC}
isi:
  a: {model: fast, primary_send_mode: session, session_type: persistent}
  b: {model: fast, primary_send_mode: global, session_type: ephemeral}
access: {a: [b], b: []}
flow: []
"#,
        )
        .unwrap();
        cfg.home = dir.path().to_owned();
        cfg.path = dir.path().join("silicon.yaml");
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        let job = SendOptions {
            id: Some("job".into()),
            title: Some("job".into()),
            new: true,
            ..Default::default()
        };
        // A started, saved session with nothing outstanding.
        let asking = runtime.worker(&connected, "a", &job).unwrap();
        let first = transport(&asking);
        asking
            .state
            .lock()
            .recover()
            .record
            .save(dir.path())
            .unwrap();
        let caller = runtime.caller(&asking.capability).unwrap();
        // b took a request from a's session, and a retires while b works on it.
        let answering = runtime
            .worker(&connected, "b", &SendOptions::default())
            .unwrap();
        answering.state.lock().recover().pending.push(Dispatch {
            id: Uuid::new_v4(),
            message: "work".into(),
            turn: Some(0),
            receipt: SendReceipt::new(),
            origin: Some(caller),
            heartbeat: false,
        });
        asking.state.lock().recover().last_activity = Utc::now() - TimeDelta::minutes(61);
        assert_eq!(runtime.retire_quiet(&connected, TimeDelta::minutes(60)), 1);
        assert!(first.join().unwrap().is_empty());
        // A user message resumes a's session in a new worker, whose turn is still running
        // (its record says so on disk) when b's answer arrives.
        let resumed = runtime.worker(&connected, "a", &job).unwrap();
        assert!(!Arc::ptr_eq(&resumed, &asking));
        let second = transport_ending(&resumed, false);
        runtime
            .send("si:test", None, "a", "busy turn", &job, false)
            .unwrap()
            .wait_started()
            .unwrap();
        let on_disk = state::sessions(dir.path(), "a", false).unwrap();
        assert_eq!(on_disk[0].status, "running");
        answering
            .on_event(Event::new(Event::TEXT).saying("the answer"), false, 0)
            .unwrap();
        answering.on_event(Event::new(Event::END), true, 0).unwrap();
        assert!(wait_until(|| resumed
            .state
            .lock()
            .recover()
            .pending
            .iter()
            .any(|dispatch| dispatch.message == "b completed:\nthe answer")));
        runtime.shutdown();
        let messages = second.join().unwrap();
        let log = silicon_log(dir.path());
        assert_eq!(messages, ["busy turn", "b completed:\nthe answer"], "{log}");
        assert!(!log.contains("ended before the ephemeral reply"), "{log}");
    }

    fn heartbeat_config(dir: &Path, heartbeat: Value) -> Config {
        let mut cfg: Config = serde_yaml::from_str(
            r#"
silicon: {id: 'si:test', org_id: org, SILICON_ORG: org, token: test, timezone: UTC, inference_providers: [all-available-providers]}
isi:
  a: {model: fast, primary_send_mode: global, session_type: persistent}
access: {a: []}
flow: []
"#,
        )
        .unwrap();
        cfg.isi.get_mut("a").unwrap().heartbeat = Some(serde_yaml::to_value(heartbeat).unwrap());
        cfg.home = dir.to_owned();
        cfg.path = dir.join("silicon.yaml");
        cfg
    }

    fn saved_beats(home: &Path) -> BTreeMap<String, Beat> {
        fs::read(beats_path(home))
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default()
    }

    #[test]
    fn heartbeat_schedules_survive_restarts_and_a_clock_that_went_back() {
        let dir = tempfile::tempdir().unwrap();
        let cfg = heartbeat_config(dir.path(), json!({"next": "2h", "message": "beat"}));
        let restart = || {
            let runtime = Runtime::new("http://127.0.0.1:1823".into());
            connect(&runtime, cfg.clone());
            let connected = runtime.get("si:test").unwrap();
            (runtime, connected)
        };
        let (first, connected) = restart();
        let mut scheduler = Scheduler::default();
        scheduler.tick(&first);
        let scheduled = wait_for(SLOW, || connected.beats.lock().recover().contains_key("a"));
        // Shutdown writes what the scheduler had not written yet.
        first.shutdown();
        assert!(scheduled);
        let beat = saved_beats(dir.path())["a"].clone();
        assert_eq!(beat.every_seconds, 7200.);
        assert_eq!(beat.next, "\"2h\"");
        assert!(beat.due > Utc::now() + TimeDelta::minutes(119), "{beat:?}");

        // A restart keeps the due time rather than starting the interval again.
        let (second, connected) = restart();
        let loaded = connected.beats.lock().recover()["a"].clone();
        Scheduler::default().tick(&second);
        thread::sleep(Duration::from_millis(200));
        let after_tick = connected.beats.lock().recover()["a"].clone();
        second.shutdown();
        assert_eq!(loaded, beat);
        assert_eq!(after_tick, beat);

        // Due more than one interval ahead: the clock went back, so it is pulled in.
        state::write_json(
            &beats_path(dir.path()),
            &BTreeMap::from([(
                "a",
                Beat::new(
                    Utc::now() + TimeDelta::hours(10),
                    Duration::from_secs(7200),
                    "\"2h\"",
                ),
            )]),
        )
        .unwrap();
        let (third, connected) = restart();
        Scheduler::default().tick(&third);
        let clamped = connected.beats.lock().recover()["a"].due;
        third.shutdown();
        let saved = saved_beats(dir.path())["a"].due;
        assert!(clamped <= Utc::now() + TimeDelta::hours(2), "{clamped}");
        assert!(clamped > Utc::now() + TimeDelta::minutes(119), "{clamped}");
        assert_eq!(saved, clamped);
        let log = silicon_log(dir.path());
        assert!(
            log.contains("[heartbeat] [a/") && log.contains("so the clock went back"),
            "{log}"
        );

        // Overdue at startup: spread over the next minute, not fired all at once.
        let now = Utc::now();
        state::write_json(
            &beats_path(dir.path()),
            &BTreeMap::from([(
                "a",
                Beat::new(
                    now - TimeDelta::hours(1),
                    Duration::from_secs(7200),
                    "\"2h\"",
                ),
            )]),
        )
        .unwrap();
        let (fourth, connected) = restart();
        let spread = connected.beats.lock().recover()["a"].due;
        fourth.shutdown();
        assert!(
            spread >= now && spread <= now + TimeDelta::seconds(61),
            "{spread}"
        );
    }

    #[test]
    fn a_due_heartbeat_fires_once_and_skips_while_the_last_is_unfinished() {
        let dir = tempfile::tempdir().unwrap();
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        connect(
            &runtime,
            heartbeat_config(dir.path(), json!({"next": "1h", "message": "beat"})),
        );
        let connected = runtime.get("si:test").unwrap();
        let worker = runtime
            .worker(&connected, "a", &SendOptions::default())
            .unwrap();
        // The turn never ends, as when a provider stalls.
        let transport = transport_ending(&worker, false);
        let hour = Duration::from_secs(3600);
        let overdue = || Beat::new(Utc::now() - TimeDelta::seconds(1), hour, "\"1h\"");
        connected.set_beat("a", overdue());
        let mut scheduler = Scheduler::default();
        scheduler.tick(&runtime);
        let fired = wait_for(SLOW, || {
            worker
                .state
                .lock()
                .recover()
                .pending
                .iter()
                .any(|dispatch| dispatch.heartbeat && dispatch.turn.is_some())
        });
        let finished = wait_for(SLOW, || {
            scheduler.jobs.values().all(|job| job.strong_count() == 0)
        });
        let rescheduled = connected.beats.lock().recover()["a"].due;
        // Due again twice while that heartbeat is unfinished: skipped, and said once.
        for _ in 0..2 {
            connected.set_beat("a", overdue());
            scheduler.tick(&runtime);
        }
        let skipped = connected.beats.lock().recover()["a"].due;
        let pending = worker.state.lock().recover().pending.len();
        runtime.shutdown();
        let messages = transport.join().unwrap();
        let log = silicon_log(dir.path());
        assert!(fired && finished, "{log}");
        assert_eq!(messages, vec!["beat"]);
        assert_eq!(pending, 1);
        assert!(
            rescheduled > Utc::now() + TimeDelta::minutes(59),
            "{rescheduled}"
        );
        assert!(skipped > Utc::now() + TimeDelta::minutes(59), "{skipped}");
        assert_eq!(
            log.matches("skipped: the previous heartbeat to a has not finished")
                .count(),
            1,
            "{log}"
        );
    }

    #[test]
    fn a_panicking_ting_batch_is_reported_and_the_inbox_carries_on() {
        let flow = serde_yaml::from_str("- log: {message: flow ran}").unwrap();
        let (dir, runtime, connected, _worker) = worker_with_flow(false, flow);
        connected
            .ting
            .accept(json!({"tings":[{"id":"t1", "type":"test", "data":{}, "metadata":{}}]}))
            .unwrap();
        INBOX_PANICS.lock().recover().push(dir.path().to_owned());
        runtime.start_inbox(&connected);
        let ran = wait_for(Duration::from_secs(15), || {
            silicon_log(dir.path()).contains("[flow ran]")
        });
        runtime.shutdown();
        let log = silicon_log(dir.path());
        assert!(ran, "{log}");
        let panicked = log
            .find("[pending Ting flow: the flow panicked: injected inbox panic]")
            .unwrap_or_else(|| panic!("{log}"));
        assert!(panicked < log.find("[flow ran]").unwrap(), "{log}");
        assert_eq!(log.matches("[flow ran]").count(), 1, "{log}");
    }

    #[test]
    fn a_blocked_ting_batch_is_written_in_full_once_and_noted_on_retries() {
        let (dir, runtime, connected, _worker) = worker(false);
        connected
            .ting
            .accept(json!({"tings":[{"id":"t-blocked", "type":"test",
                "data":{"marker":"full-batch-body"}, "metadata":{}}]}))
            .unwrap();
        // A broken edit of the live flow blocks the batch.
        fs::write(&connected.cfg.path, "flow: [").unwrap();
        runtime.start_inbox(&connected);
        let retried = wait_for(Duration::from_secs(15), || {
            silicon_log(dir.path()).contains("(attempt 3)")
        });
        runtime.shutdown();
        let log = silicon_log(dir.path());
        assert!(retried, "{log}");
        assert_eq!(log.matches("full-batch-body").count(), 1, "{log}");
        assert!(
            log.contains("[event] [webhook/")
                && log.contains("retrying the Ting batch of tings t-blocked (attempt 2); its full request was written on the first attempt"),
            "{log}"
        );
    }

    #[test]
    fn parallel_stops_finish_together_and_name_what_outlived_the_budget() {
        let started = Instant::now();
        let tasks: Vec<Task> = vec![
            (
                "quick".into(),
                Box::new(|| {
                    thread::sleep(Duration::from_millis(200));
                    Ok(())
                }),
            ),
            (
                "failing".into(),
                Box::new(|| {
                    thread::sleep(Duration::from_millis(200));
                    bail!("boom")
                }),
            ),
            (
                "stuck".into(),
                Box::new(|| {
                    thread::sleep(Duration::from_secs(5));
                    Ok(())
                }),
            ),
            ("panicking".into(), Box::new(|| panic!("kaboom"))),
        ];
        let outcomes = in_parallel(tasks, Duration::from_secs(1));
        let elapsed = started.elapsed();
        assert!(elapsed < Duration::from_secs(3), "{elapsed:?}");
        let names: Vec<_> = outcomes.iter().map(|(name, _)| name.as_str()).collect();
        assert_eq!(names, ["quick", "failing", "stuck", "panicking"]);
        assert!(matches!(outcomes[0].1, Some(Ok(()))));
        assert_eq!(
            format!(
                "{:#}",
                outcomes[1].1.as_ref().unwrap().as_ref().unwrap_err()
            ),
            "boom"
        );
        assert!(outcomes[2].1.is_none());
        assert_eq!(
            format!(
                "{:#}",
                outcomes[3].1.as_ref().unwrap().as_ref().unwrap_err()
            ),
            "panicked: kaboom"
        );
    }

    #[test]
    fn shutdown_stops_wedged_sessions_in_parallel_within_the_stop_timeout() {
        let (dir, runtime, connected, _worker) =
            worker_with_mode(false, serde_yaml::Value::Sequence(Vec::new()), "session");
        // Three omnids that take the stop request and never answer it.
        let mut daemons = Vec::new();
        for id in ["one", "two", "three"] {
            let worker = runtime
                .worker(
                    &connected,
                    "a",
                    &SendOptions {
                        id: Some(id.into()),
                        title: Some(id.into()),
                        new: true,
                        ..Default::default()
                    },
                )
                .unwrap();
            let (client, daemon) = UnixStream::pair().unwrap();
            *worker.client.lock().recover() = Some(Client::from_stream(client).unwrap());
            daemons.push(daemon);
        }
        let started = Instant::now();
        runtime.shutdown();
        let elapsed = started.elapsed();
        drop(daemons);
        // One after another they would take three stop timeouts.
        assert!(elapsed < STOP_TIMEOUT * 2, "{elapsed:?}");
        assert!(connected.workers.lock().recover().is_empty());
        let log = silicon_log(dir.path());
        assert_eq!(
            log.matches(&format!(
                "stop got no answer in {}s",
                STOP_TIMEOUT.as_secs()
            ))
            .count(),
            3,
            "{log}"
        );
    }

    #[test]
    fn a_failed_start_of_global_ephemeral_work_leaves_nothing_behind() {
        use std::os::unix::fs::PermissionsExt;
        if std::env::var_os("OMNI_DAEMON").is_some() {
            return;
        }
        let (dir, runtime, connected, worker) = worker(true);
        let omnid = dir.path().join(".silicon/bin/omnid");
        fs::create_dir_all(omnid.parent().unwrap()).unwrap();
        fs::write(
            &omnid,
            "#!/bin/sh\necho 'omnid: no providers' >&2\nexit 3\n",
        )
        .unwrap();
        fs::set_permissions(&omnid, fs::Permissions::from_mode(0o700)).unwrap();
        let error = format!("{:#}", worker.send("hello", None, false).err().unwrap());
        let omni_home = dir
            .path()
            .join(".silicon/omni")
            .join(worker.session_id.to_string());
        assert!(error.contains("omnid: no providers"), "{error}");
        assert!(worker.stopped.load(Ordering::SeqCst));
        assert!(connected.workers.lock().recover().is_empty());
        assert!(runtime.caller(&worker.capability).is_none());
        assert!(!omni_home.exists());
        assert!(fs::symlink_metadata(short_home(worker.session_id)).is_err());
        runtime.shutdown();
    }

    #[test]
    fn provider_chatter_is_bounded_and_the_failure_says_how_much_was_left_out() {
        let (_dir, _runtime, _connected, worker) = worker(false);
        let delivery = pending(&worker, "hello");
        for line in 0..250 {
            worker
                .on_event(
                    Event::failure("stderr", format!("line {line}")).from("claude-code-cli"),
                    false,
                    1,
                )
                .unwrap();
        }
        let (kept, dropped) = {
            let state = worker.state.lock().recover();
            (state.trouble.len(), state.trouble_dropped)
        };
        assert_eq!((kept, dropped), (TROUBLE_KEPT, 50));
        worker
            .on_event(
                Event::failure("crash", "gave up").from("claude-code-cli"),
                true,
                1,
            )
            .unwrap();
        let error = format!("{:#}", delivery.wait_started().unwrap_err());
        assert!(
            error.contains(
                "50 earlier provider lines are not repeated here; silicon.log has every one"
            ),
            "{error}"
        );
        assert!(!error.contains("reported stderr: line 49\n"), "{error}");
        assert!(
            error.contains("reported stderr: line 50\n")
                && error.contains("reported stderr: line 249"),
            "{error}"
        );
    }

    #[test]
    fn work_silent_past_the_stall_limit_is_reported_in_full() {
        let (_dir, _runtime, _connected, worker) = worker(false);
        // Nothing outstanding: silence is just an idle session.
        assert!(worker
            .stall_report(false, json!(null), Duration::ZERO, None)
            .is_none());
        let _delivery = pending(&worker, "hello");
        worker
            .on_event(
                Event::failure("stderr", "still thinking").from("codex-cli"),
                false,
                1,
            )
            .unwrap();
        assert!(worker
            .stall_report(
                true,
                json!({"in_turn": true}),
                Duration::from_secs(3600),
                None
            )
            .is_none());
        let report = worker
            .stall_report(
                true,
                json!({"in_turn": true}),
                Duration::ZERO,
                Some("{\"type\":\"start\"}"),
            )
            .unwrap();
        for said in [
            "no Omni event for session",
            "while a turn was in progress",
            "Omni's last state: {\"in_turn\":true}",
            "last event: {\"type\":\"start\"}",
            "accepted messages outstanding: 1",
            "codex-cli reported stderr: still thinking",
            "this worker has no omnid",
        ] {
            assert!(report.contains(said), "missing {said:?} in {report}");
        }
    }

    #[test]
    fn record_keeping_failures_and_orderly_stops_do_not_fail_a_turn() {
        let (dir, runtime, _connected, worker) = worker(false);
        let delivery = pending(&worker, "hello");
        // silicon.log cannot be written: stderr gets it, and the turn carries on.
        let log = dir.path().join(".silicon/silicon.log");
        let _ = fs::remove_file(&log);
        fs::create_dir_all(&log).unwrap();
        worker
            .on_event(Event::new(Event::START).saying("hello"), false, 0)
            .unwrap();
        fs::remove_dir_all(&log).unwrap();
        delivery.wait_started().unwrap();
        assert!(!worker.stopped.load(Ordering::SeqCst));
        // Once the interpreter is stopping, an event that would fail the worker is recorded only.
        runtime.stopping.store(true, Ordering::SeqCst);
        let mut error = Event::new(Event::ERROR);
        error.kind = "crash".into();
        error.error = "provider went down with the interpreter".into();
        worker.on_event(error, true, 0).unwrap();
        assert!(!worker.stopped.load(Ordering::SeqCst));
        assert_ne!(worker.state.lock().recover().record.status, "error");
        assert!(silicon_log(dir.path()).contains("provider went down with the interpreter"));
        runtime.shutdown();
        assert_eq!(worker.state.lock().recover().record.status, "stopped");
    }

    #[test]
    fn a_dna_refresh_due_past_one_interval_is_pulled_back_to_now() {
        let (dir, _runtime, _connected, worker) = worker(false);
        let every = Duration::from_secs(3600);
        worker.state.lock().recover().next_dna = Some((Utc::now() + TimeDelta::hours(10), every));
        worker.schedule_dna_refresh();
        let (due, kept) = worker.state.lock().recover().next_dna.unwrap();
        assert_eq!(kept, every);
        assert!(due <= Utc::now() + TimeDelta::hours(1), "{due}");
        assert!(due > Utc::now() + TimeDelta::minutes(59), "{due}");
        assert!(silicon_log(dir.path()).contains("so the clock went back"));
    }

    #[test]
    fn all_available_providers_opens_with_the_probed_list_or_says_there_is_none() {
        use std::io::{BufRead, BufReader, Write};
        for available in [json!(["claude-code-cli", "codex-app-server"]), json!([])] {
            let (client, mut daemon) = UnixStream::pair().unwrap();
            daemon
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let answer = available.clone();
            let fake = thread::spawn(move || {
                let mut line = String::new();
                BufReader::new(daemon.try_clone().unwrap())
                    .read_line(&mut line)
                    .unwrap();
                let request: Value = serde_json::from_str(&line).unwrap();
                assert_eq!(request["op"], "providers");
                writeln!(
                    daemon,
                    "{}",
                    json!({"id": request["id"], "ok": true, "result": answer})
                )
                .unwrap();
            });
            let inference = Inference::from_client(Client::from_stream(client).unwrap());
            let selected = select_providers(
                &serde_yaml::from_str("[all-available-providers]").unwrap(),
                &inference,
            );
            fake.join().unwrap();
            if available == json!([]) {
                let error = format!("{:#}", selected.unwrap_err());
                assert!(
                    error.contains("selects no authenticated providers (Omni reports these as available: none)"),
                    "{error}"
                );
            } else {
                assert_eq!(selected.unwrap(), ["claude-code-cli", "codex-app-server"]);
            }
        }
    }

    #[test]
    fn a_slow_credential_check_does_not_hold_the_silicon() {
        use std::os::unix::fs::PermissionsExt;
        let (dir, runtime, connected, _worker) =
            worker_with_mode(false, serde_yaml::Value::Sequence(Vec::new()), "session");
        // A registered app whose check takes two seconds and then fails.
        let app = dir.path().join("slow-app");
        fs::write(
            &app,
            "#!/bin/sh\nsleep 2\necho 'slow-app is unreachable' >&2\nexit 1\n",
        )
        .unwrap();
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700)).unwrap();
        state::write_json(
            &dir.path().join(".silicon/auth-apps.json"),
            &vec![app.display().to_string()],
        )
        .unwrap();
        let (creator, target) = (runtime.clone(), connected.clone());
        let creating = thread::spawn(move || {
            creator
                .worker(
                    &target,
                    "a",
                    &SendOptions {
                        id: Some("new".into()),
                        title: Some("new".into()),
                        new: true,
                        ..Default::default()
                    },
                )
                .map(|_| ())
        });
        // While the check runs, the Silicon's sessions stay listable.
        thread::sleep(Duration::from_millis(500));
        let started = Instant::now();
        let listed = runtime.list("si:test", "a", false);
        let waited = started.elapsed();
        let created = creating.join().unwrap();
        // An automatic check that fails is logged in full and does not stop the session.
        let workers = connected.workers.lock().recover().len();
        runtime.shutdown();
        created.unwrap();
        assert_eq!(workers, 2);
        assert_eq!(listed.unwrap().len(), 1);
        assert!(waited < Duration::from_secs(1), "{waited:?}");
        let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        assert!(
            log.contains("automatic credential check failed; sessions start without it")
                && log.contains("slow-app is unreachable"),
            "{log}"
        );
    }

    #[test]
    fn a_new_session_is_scheduled_without_waiting_for_the_next_disk_read() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg = heartbeat_config(dir.path(), json!({"next": "2h", "message": "beat"}));
        cfg.isi.get_mut("a").unwrap().primary_send_mode = Some("session".into());
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        let mut scheduler = Scheduler::default();
        // The first tick reads the (empty) session list from disk and caches it.
        scheduler.tick(&runtime);
        runtime
            .worker(
                &connected,
                "a",
                &SendOptions {
                    id: Some("fresh".into()),
                    new: true,
                    ..Default::default()
                },
            )
            .unwrap();
        scheduler.tick(&runtime);
        let scheduled = wait_for(SLOW, || {
            connected.beats.lock().recover().contains_key("a:fresh")
        });
        runtime.shutdown();
        assert!(scheduled);
    }

    #[test]
    fn a_changed_heartbeat_next_applies_at_reconnect_without_waiting_out_the_old_one() {
        let dir = tempfile::tempdir().unwrap();
        let connect_with = |next: &str| {
            let runtime = Runtime::new("http://127.0.0.1:1823".into());
            connect(
                &runtime,
                heartbeat_config(dir.path(), json!({"next": next, "message": "beat"})),
            );
            let connected = runtime.get("si:test").unwrap();
            Scheduler::default().tick(&runtime);
            let from = serde_json::to_string(next).unwrap();
            let settled = wait_for(SLOW, || {
                connected
                    .beats
                    .lock()
                    .recover()
                    .get("a")
                    .is_some_and(|beat| beat.next == from)
            });
            let beat = connected.beats.lock().recover().get("a").cloned();
            runtime.shutdown();
            assert!(settled, "{beat:?}\n{}", silicon_log(dir.path()));
            let saved = saved_beats(dir.path())["a"].clone();
            assert_eq!(Some(&saved), beat.as_ref());
            saved
        };
        let daily = connect_with("24h");
        assert!(daily.due > Utc::now() + TimeDelta::hours(23), "{daily:?}");
        // Shortened: one new interval from now, not the rest of the day.
        let quick = connect_with("1s");
        assert_eq!(quick.every_seconds, 1.);
        assert!(quick.due <= Utc::now() + TimeDelta::seconds(1), "{quick:?}");
        // Lengthened: the heartbeat the shorter interval already scheduled still comes first.
        let long = connect_with("12h");
        assert_eq!(long.every_seconds, 43200.);
        assert!(long.due <= Utc::now() + TimeDelta::seconds(2), "{long:?}");
        let log = silicon_log(dir.path());
        assert!(
            log.contains("[heartbeat] [a/")
                && log.contains("[heartbeat.next is now \"1s\" (1s), and the saved due time ")
                && log.contains(
                    " was worked out from \"24h\" (86400s); the next heartbeat is due at "
                ),
            "{log}"
        );
    }

    #[test]
    fn heartbeat_schedule_changes_are_written_at_most_once_a_second() {
        let dir = tempfile::tempdir().unwrap();
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        connect(
            &runtime,
            heartbeat_config(dir.path(), json!({"next": "1h", "message": "beat"})),
        );
        let connected = runtime.get("si:test").unwrap();
        let beat = |minutes| {
            Beat::new(
                Utc::now() + TimeDelta::minutes(minutes),
                Duration::from_secs(3600),
                "\"1h\"",
            )
        };
        let current = || connected.beats.lock().recover()["a"].clone();
        // A change alone writes nothing; the first write goes out at once.
        connected.set_beat("a", beat(10));
        assert!(saved_beats(dir.path()).is_empty());
        connected.write_beats(false);
        let first = current();
        assert_eq!(saved_beats(dir.path())["a"], first);
        // Changes within the next second wait for one write.
        for minutes in 20..120 {
            connected.set_beat("a", beat(minutes));
            connected.write_beats(false);
        }
        assert_eq!(saved_beats(dir.path())["a"], first);
        thread::sleep(BEATS_WRITE);
        connected.write_beats(false);
        assert_eq!(saved_beats(dir.path())["a"], current());
        // Disconnect writes the last change at once; the replaced connection writes no more.
        connected.set_beat("a", beat(500));
        let last = current();
        runtime.disconnect("si:test").unwrap();
        assert_eq!(saved_beats(dir.path())["a"], last);
        connected.set_beat("a", beat(600));
        connected.write_beats(true);
        assert_eq!(saved_beats(dir.path())["a"], last);
    }

    #[test]
    fn failure_records_of_ended_heartbeat_targets_and_sessions_are_let_go() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg = heartbeat_config(dir.path(), json!({"next": "2h", "message": "beat"}));
        cfg.isi.get_mut("a").unwrap().primary_send_mode = Some("session".into());
        let runtime = Runtime::new("http://127.0.0.1:1823".into());
        connect(&runtime, cfg);
        let connected = runtime.get("si:test").unwrap();
        let worker = runtime
            .worker(
                &connected,
                "a",
                &SendOptions {
                    id: Some("job".into()),
                    new: true,
                    ..Default::default()
                },
            )
            .unwrap();
        let (live, gone, dna) = (
            format!("{BEAT_REPORT}a:job"),
            format!("{BEAT_REPORT}a:gone"),
            format!("dna refresh {}", worker.session_id),
        );
        for key in [&live, &gone, &dna] {
            connected.report(key, "a", "it failed");
        }
        // Not due, so no heartbeat runs and recovers from it.
        connected.set_beat(
            "a:job",
            Beat::new(
                Utc::now() + TimeDelta::hours(1),
                Duration::from_secs(7200),
                "\"2h\"",
            ),
        );
        let keys = || -> BTreeSet<String> {
            connected
                .reported
                .lock()
                .recover()
                .keys()
                .cloned()
                .collect()
        };
        Scheduler::default().tick(&runtime);
        let ticked = keys();
        worker.stop(None).unwrap();
        let stopped = keys();
        runtime.shutdown();
        assert_eq!(ticked, BTreeSet::from([live.clone(), dna]));
        assert_eq!(stopped, BTreeSet::from([live]));
    }

    #[test]
    fn a_failed_cleanup_after_the_last_turn_is_written_to_silicon_log() {
        let (dir, runtime, _connected, worker) =
            worker_with_mode(true, serde_yaml::Value::Sequence(Vec::new()), "session");
        let _delivery = pending(&worker, "hello");
        // Kept ephemeral work is archived when it finishes, and the archive cannot be written.
        let archived = dir.path().join(".silicon/sessions/archived");
        fs::create_dir_all(archived.parent().unwrap()).unwrap();
        fs::write(&archived, "not a directory").unwrap();
        worker
            .on_event(Event::new(Event::START).saying("hello"), false, 0)
            .unwrap();
        worker.on_event(Event::new(Event::END), true, 0).unwrap();
        let stopped = worker.stopped.load(Ordering::SeqCst);
        runtime.shutdown();
        let log = silicon_log(dir.path());
        assert!(stopped);
        assert!(
            log.contains(&format!(
                "[session {} finished its work, but stopping it afterwards failed: saving session state: ",
                worker.session_id
            )),
            "{log}"
        );
    }

    #[test]
    fn an_end_that_waited_for_a_retiring_session_still_ends_it() {
        let (dir, runtime, connected, worker) =
            worker_with_mode(false, serde_yaml::Value::Sequence(Vec::new()), "session");
        // A started, saved session with nothing outstanding.
        let transport = transport(&worker);
        worker
            .state
            .lock()
            .recover()
            .record
            .save(dir.path())
            .unwrap();
        // An idle retirement holds the session when the end arrives, and finishes first.
        let mut client = worker.client.lock().recover();
        let (ender, target) = (runtime.clone(), connected.clone());
        let ending = thread::spawn(move || ender.end_connected(&target, "a", Some("job")));
        thread::sleep(Duration::from_millis(300));
        worker.end_locked(&mut client, None, true).unwrap();
        drop(client);
        let ended = ending.join().unwrap();
        let messages = transport.join().unwrap();
        let active = state::sessions(dir.path(), "a", false).unwrap();
        let archived = state::sessions(dir.path(), "a", true).unwrap();
        runtime.shutdown();
        if let Err(error) = ended {
            panic!("{error:#}\n{}", silicon_log(dir.path()));
        }
        assert!(messages.is_empty());
        assert!(worker.retired.load(Ordering::SeqCst));
        assert!(active.is_empty(), "{active:?}");
        assert_eq!(archived.len(), 1);
        assert_eq!(archived[0].session_id, worker.session_id);
    }
}
