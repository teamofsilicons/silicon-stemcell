//! An owned, isolated Caddy process. It never contacts the machine's default admin API.
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::ffi::{CString, OsStr, OsString};
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

/// A health check or probe is a local call; a Caddy that needs longer is hung.
const CHECK_TIMEOUT: Duration = Duration::from_secs(5);
/// How long a Caddy left running by an earlier interpreter gets to exit after each request
/// to stop: its admin API's /stop, then SIGTERM, then SIGKILL.
const REAP_GRACE: Duration = Duration::from_secs(5);
/// Requests per probe of the HTTP port, each on a new connection. Linux spreads connections
/// across the SO_REUSEPORT listeners of one user, so a server sharing the port with this
/// Caddy gets each request by chance, and 32 all miss it with odds of 2^-32 per listener
/// share. A start that fails the probe is retried, so a few requests would only delay a split.
const PROBES: u32 = 32;
/// Healthy checks between probes of the HTTP port. serve checks every 5 s, so a server that
/// starts sharing the port after this Caddy started is found within about a minute.
const PROBE_EVERY: u32 = 12;

pub struct Proxy {
    child: Option<Child>,
    socket_dir: PathBuf,
    socket: PathBuf,
    state: PathBuf,
    config: Value,
    interpreter_port: u16,
    http_port: u16,
    ipv6: bool,
    /// caddy.log length after the last operation Caddy completed or the last healthy check;
    /// later lines explain later failures.
    log_mark: u64,
    /// Why this proxy stopped its Caddy, for every later call that finds it stopped.
    stopped: Option<String>,
    /// Only this Caddy answers the probe host with it, so a start can tell whether this
    /// Caddy is the one answering the HTTP port.
    nonce: String,
    /// Healthy checks since the HTTP port was last probed.
    unprobed: u32,
    /// The last failure of a health check's upkeep (socket timestamps, caddy.log size),
    /// reported once per distinct text rather than on every check.
    upkeep_failure: Option<String>,
}

/// `<state>/owner.json`: the Caddy a Proxy started. An interpreter that ends without
/// stopping it (SIGKILL, OOM, a crash) leaves it running, and the next start stops it.
#[derive(Debug, Serialize, Deserialize)]
struct Owner {
    pid: u32,
    socket: String,
    started_at: DateTime<Utc>,
    /// The exact argv, which names this state directory's pid file, so another
    /// interpreter's Caddy that reuses the pid after a reboot never matches.
    command: Vec<String>,
}

/// The Caddy executable: SILICON_CADDY, else `caddy` on PATH.
fn binary() -> OsString {
    std::env::var_os("SILICON_CADDY").unwrap_or_else(|| "caddy".into())
}

impl Proxy {
    pub fn start(state_dir: &Path, interpreter_port: u16, hosts: &[String]) -> Result<Self> {
        Self::start_on(state_dir, interpreter_port, hosts, 80, &binary())
            .context("could not start the owned Caddy proxy")
    }

    fn start_on(
        state_dir: &Path,
        interpreter_port: u16,
        hosts: &[String],
        http_port: u16,
        binary: &OsStr,
    ) -> Result<Self> {
        if interpreter_port == 0 || http_port == 0 {
            bail!("proxy ports must be nonzero (interpreter {interpreter_port}, HTTP {http_port})");
        }
        validate_hosts(hosts)?;
        let state = state_dir.join("caddy");
        crate::state::private_dir(&state).context("create Caddy state directory")?;
        let state = state
            .canonicalize()
            .with_context(|| format!("resolve Caddy state directory {}", state.display()))?;
        // Checked before anything is stopped or started for this state directory.
        utf8(&state)?;
        let earlier = reap_orphan(&state)?;
        Self::launch(state, interpreter_port, hosts, http_port, binary).map_err(|error| {
            match &earlier {
                Some(earlier) => anyhow!("{error:#}\nBefore this start: {earlier}"),
                None => error,
            }
        })
    }

    fn launch(
        state: PathBuf,
        interpreter_port: u16,
        hosts: &[String],
        http_port: u16,
        binary: &OsStr,
    ) -> Result<Self> {
        // Unix socket paths are limited to 104 bytes on macOS; user-selected
        // state directories can be much longer. This private, unique directory
        // contains only the short-lived admin socket and is removed on drop.
        let socket_dir = PathBuf::from("/tmp").join(format!("silicon-caddy-{}", Uuid::new_v4()));
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&socket_dir)
            .with_context(|| {
                format!(
                    "create Caddy admin socket directory {}",
                    socket_dir.display()
                )
            })?;
        let socket = socket_dir.join("admin.sock");
        // Probe only: without IPv6 loopback, Caddy listens on IPv4 alone.
        let ipv6 = TcpListener::bind("[::1]:0").is_ok();
        let mut proxy = Self {
            child: None,
            socket_dir,
            socket,
            state,
            config: Value::Null,
            interpreter_port,
            http_port,
            ipv6,
            log_mark: 0,
            stopped: None,
            nonce: Uuid::new_v4().simple().to_string(),
            unprobed: 0,
            upkeep_failure: None,
        };
        let initial = proxy.base_config()?;
        let log_path = proxy.log_path();
        // Caddy holds its log open for its whole life, so the size is capped where Silicon
        // opens the file. A failed rotation leaves Caddy appending to the same file.
        if let Err(error) = crate::rotate(&log_path, crate::LOG_CAP, crate::LOG_KEEP) {
            crate::stderr_line(&format!(
                "{error:#}; Caddy keeps appending to {}",
                log_path.display()
            ));
        }
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(&log_path)
            .with_context(|| format!("open Caddy log {}", log_path.display()))?;
        proxy.log_mark = proxy.log_len();
        let pidfile = proxy.state.join("caddy.pid");
        let args = ["run", "--config", "-", "--pidfile", utf8(&pidfile)?];
        let command = crate::failure::argv(binary, &args);
        let mut child = Command::new(binary)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(log.try_clone().with_context(|| format!("share Caddy log {}", log_path.display()))?)
            .stderr(log)
            .env("XDG_DATA_HOME", proxy.state.join("data"))
            .env("XDG_CONFIG_HOME", proxy.state.join("config"))
            .spawn()
            .with_context(|| {
                format!(
                    "could not run `{command}` ({}); install Caddy or set SILICON_CADDY to its executable",
                    Path::new(binary).display()
                )
            })?;
        let input = child.stdin.take();
        let pid = child.id();
        proxy.child = Some(child);
        proxy.record_owner(
            pid,
            std::iter::once(binary.to_string_lossy().into_owned())
                .chain(args.iter().map(|arg| (*arg).to_owned()))
                .collect(),
        );
        let sent = match input {
            Some(mut input) => input.write_all(&serde_json::to_vec(&initial)?),
            None => Err(std::io::Error::other("Caddy's stdin was not connected")),
        };
        if let Err(error) = sent {
            // Caddy closing stdin early usually means it exited; its status and log say why.
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                if let Err(exited) = proxy.ensure_running() {
                    return Err(exited).context(format!(
                        "could not send Caddy its initial configuration on stdin: {error}"
                    ));
                }
                thread::sleep(Duration::from_millis(25));
            }
            bail!(
                "could not send Caddy its initial configuration on stdin: {error}; Caddy is still running\n{}",
                proxy.log_since(proxy.log_mark)
            );
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            proxy.ensure_running()?;
            match UnixStream::connect(&proxy.socket) {
                Ok(stream) => {
                    drop(stream);
                    break;
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::NotFound | std::io::ErrorKind::ConnectionRefused
                    ) => {}
                Err(error) => bail!(
                    "could not connect to the private Caddy admin socket {}: {error}\n{}",
                    proxy.socket.display(),
                    proxy.log_since(proxy.log_mark)
                ),
            }
            if Instant::now() >= deadline {
                bail!(
                    "Caddy is running but did not open its private admin socket {} within 10s\n{}",
                    proxy.socket.display(),
                    proxy.log_since(proxy.log_mark)
                );
            }
            thread::sleep(Duration::from_millis(25));
        }
        proxy.config = initial;
        proxy.log_mark = proxy.log_len();
        let config = proxy.configuration(hosts)?;
        if let Err(error) = proxy.replace_config(config) {
            let error = error.context(format!(
                "Caddy could not serve the Silicon routes on HTTP port {http_port}; if it reports a bind error, check whether another server uses that port and, on Linux, grant the Caddy executable permission to bind port 80"));
            // Caddy's words stay first; the facts that explain a refused port follow them.
            if cfg!(target_os = "linux") && format!("{error:#}").contains("bind: permission denied")
            {
                return Err(anyhow!("{error:#}\n{}", port_facts(binary, http_port)));
            }
            return Err(error);
        }
        proxy.verify_listener().with_context(|| {
            format!("Caddy loaded the Silicon routes, but a probe of HTTP port {http_port} did not get only its answers")
        })?;
        Ok(proxy)
    }

    /// Checks Caddy's health (see [`Proxy::check`]), then applies `hosts`. Caddy's /load
    /// endpoint keeps serving the old configuration if a reload fails. Persist only an
    /// accepted configuration; roll back if the disk commit fails. Any error means this
    /// proxy cannot be trusted to route, and the interpreter replaces it.
    pub fn update(&mut self, hosts: &[String]) -> Result<()> {
        self.check()?;
        let config = self.configuration(hosts)?;
        if config != self.config {
            self.replace_config(config)?;
        }
        Ok(())
    }

    /// Caddy is running and its admin API answers, and every [`PROBE_EVERY`] checks it is
    /// still the server answering the HTTP port. A Caddy that exited, hangs, or lost its admin
    /// socket or its port fails with the reason and what Caddy logged since it was last
    /// healthy, and the interpreter then replaces it. A healthy check keeps the socket safe
    /// from age-based temp cleaners and caddy.log within its cap.
    pub fn check(&mut self) -> Result<()> {
        self.ensure_running()?;
        let pid = self.child.as_ref().map_or(0, Child::id);
        admin_call(&self.socket, "GET", "/config/", &[], CHECK_TIMEOUT)
            .map_err(|error| anyhow!("{error}\n{}", self.log_since(self.log_mark)))
            .with_context(|| {
                format!(
                    "Caddy process {pid} is running, but its private admin socket {} did not answer a health check, so its routes can no longer be changed",
                    self.socket.display()
                )
            })?;
        // A server that binds the port beside this Caddy later (SO_REUSEPORT) splits its
        // traffic without any error; the start's probe cannot see it.
        self.unprobed += 1;
        if self.unprobed >= PROBE_EVERY {
            self.verify_listener().with_context(|| {
                format!(
                    "Caddy process {pid} is running, but a probe of HTTP port {} did not get only its answers",
                    self.http_port
                )
            })?;
            self.unprobed = 0;
        }
        self.upkeep();
        // A later failure then shows only what Caddy said since it was last known healthy.
        self.log_mark = self.log_len();
        Ok(())
    }

    fn upkeep(&mut self) {
        let mut problems = Vec::new();
        // Age-based temp cleaners (systemd-tmpfiles after 10 days, macOS periodic) skip
        // recently used files, and nothing else touches the socket after Caddy creates it.
        for path in [&self.socket_dir, &self.socket] {
            if let Err(error) = touch(path) {
                problems.push(format!(
                    "could not refresh the timestamps of {}, so a temp cleaner may delete it: {error}",
                    path.display()
                ));
            }
        }
        if let Err(error) = self.trim_log(crate::LOG_CAP) {
            problems.push(format!("{error:#}"));
        }
        let problem = (!problems.is_empty()).then(|| problems.join("\n"));
        if problem != self.upkeep_failure {
            if let Some(problem) = &problem {
                crate::stderr_line(problem);
            }
            self.upkeep_failure = problem;
        }
    }

    /// Caddy writes caddy.log through descriptors it holds for its whole life, so renaming
    /// the file would not rotate it. Past `cap` its content is copied to caddy.log.1 (older
    /// copies shift up) and the file restarts empty; Silicon opened it O_APPEND, so Caddy's
    /// next write lands at the new end.
    fn trim_log(&mut self, cap: u64) -> Result<bool> {
        let path = self.log_path();
        let length = match fs::metadata(&path) {
            Ok(metadata) => metadata.len(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => {
                return Err(error).with_context(|| format!("read the size of {}", path.display()))
            }
        };
        if length < cap {
            return Ok(false);
        }
        for index in (1..crate::LOG_KEEP.max(1)).rev() {
            let from = crate::numbered(&path, &index.to_string());
            let to = crate::numbered(&path, &(index + 1).to_string());
            match fs::rename(&from, &to) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => {
                    return Err(error)
                        .with_context(|| format!("rotate {} to {}", from.display(), to.display()))
                }
            }
        }
        let first = crate::numbered(&path, "1");
        let copied = fs::copy(&path, &first)
            .with_context(|| format!("copy {} to {}", path.display(), first.display()))?;
        // Lines Caddy wrote during the copy follow it, so only a write in the instant
        // between this read and the truncation can be lost.
        let mut late = Vec::new();
        fs::File::open(&path)
            .and_then(|mut file| {
                file.seek(SeekFrom::Start(copied))?;
                file.read_to_end(&mut late)
            })
            .and_then(|_| {
                OpenOptions::new()
                    .append(true)
                    .open(&first)?
                    .write_all(&late)
            })
            .and_then(|()| OpenOptions::new().write(true).open(&path)?.set_len(0))
            .with_context(|| {
                format!(
                    "move {} into {} and empty it",
                    path.display(),
                    first.display()
                )
            })?;
        self.log_mark = 0;
        Ok(true)
    }

    fn base_config(&self) -> Result<Value> {
        let storage = self.state.join("storage");
        Ok(json!({
            "admin": {"listen": format!("unix/{}|0600", self.socket.display()), "config": {"persist": false}},
            // Caddy logs every admin API call (about 250 bytes), and health checks call it
            // every few seconds. Its answers, errors included, reach Silicon over the socket
            // and are reported in full from there.
            "logging": {"logs": {"default": {"exclude": ["admin.api"]}}},
            "storage": {"module": "file_system", "root": utf8(&storage)?}
        }))
    }

    fn configuration(&self, hosts: &[String]) -> Result<Value> {
        let hosts = validate_hosts(hosts)?;
        // macOS permits unprivileged port 80 on wildcard addresses but may deny
        // a loopback-only bind. The remote_ip matcher below still restricts all
        // forwarding to the actual loopback peer, regardless of HTTP headers.
        let (ipv4, ipv6) = if cfg!(target_os = "macos") {
            ("tcp4/0.0.0.0", "tcp6/[::]")
        } else {
            ("127.0.0.1", "[::1]")
        };
        let mut listen = vec![format!("{ipv4}:{}", self.http_port)];
        if self.ipv6 {
            listen.push(format!("{ipv6}:{}", self.http_port));
        }
        let loopback = json!({"ranges": ["127.0.0.1/32", "::1/128"]});
        let mut config = self.base_config()?;
        config["apps"] = json!({"http": {
            "grace_period": "1s",
            "servers": {"silicon": {
                "listen": listen,
                "automatic_https": {"disable": true},
                "routes": [
                    {"match": [{"host": [self.probe_host()], "remote_ip": loopback}], "handle": [{"handler": "static_response",
                        "status_code": 200, "body": self.nonce}], "terminal": true},
                    {"match": [{"host": hosts, "remote_ip": loopback}], "handle": [{"handler": "reverse_proxy",
                        "upstreams": [{"dial": format!("127.0.0.1:{}", self.interpreter_port)}]}], "terminal": true},
                    {"handle": [{"handler": "static_response", "status_code": 404, "body": "Unknown Silicon host\n"}]}
                ]
            }}
        }});
        Ok(config)
    }

    fn probe_host(&self) -> String {
        format!("probe-{}.silicon.localhost", self.nonce)
    }

    /// With SO_REUSEPORT another Caddy (one an earlier interpreter left behind, or the
    /// user's own) binds the same HTTP port without any error and can take every
    /// connection, or a share of them. Only this Caddy knows the probe host, so only its
    /// answer to every one of [`PROBES`] requests proves it alone listens.
    fn verify_listener(&self) -> Result<()> {
        let host = self.probe_host();
        let port = self.http_port;
        for attempt in 1..=PROBES {
            let response = match http_get(port, &host) {
                Ok(response) => response,
                Err(error) => bail!(
                    "request {attempt} of {PROBES} to http://127.0.0.1:{port}/ for Host {host}, which only this Caddy answers, failed: {error}\n{}",
                    self.log_since(self.log_mark)
                ),
            };
            let (_, body) = response.split_once("\r\n\r\n").unwrap_or(("", ""));
            if body != self.nonce {
                bail!(
                    "another server answers HTTP port {port}: request {attempt} of {PROBES} for Host {host}, which only this Caddy answers (with {}), got:\n{response}\n{}",
                    self.nonce,
                    self.log_since(self.log_mark)
                );
            }
        }
        Ok(())
    }

    fn replace_config(&mut self, next: Value) -> Result<()> {
        if let Err(error) = self.request(
            "POST",
            "/load",
            &serde_json::to_vec(&next)?,
            Duration::from_secs(10),
        ) {
            // A timeout can happen after Caddy accepted the new configuration.
            // Restore explicitly before reporting failure to the caller.
            self.rollback()
                .with_context(|| format!("Caddy reload failed: {error:#}"))?;
            return Err(error).context("Caddy reload failed; previous routes are active");
        }
        let saved = self.state.join("config.json");
        if let Err(error) = crate::state::write_json(&saved, &next) {
            self.rollback()
                .with_context(|| format!("Caddy config could not be saved: {error:#}"))?;
            crate::state::write_json(&saved, &self.config)
                .with_context(|| format!("previous Caddy routes are active, but the saved config could not be restored after {error:#}"))?;
            return Err(error).context("Caddy config could not be saved; restored previous routes");
        }
        self.config = next;
        self.log_mark = self.log_len();
        Ok(())
    }

    /// When the previous routes cannot be restored, nobody knows which routes Caddy serves,
    /// so it is stopped. Every later call fails with the reason, and the interpreter
    /// replaces this proxy with a fresh Caddy.
    fn rollback(&mut self) -> Result<()> {
        if let Err(error) = self.request(
            "POST",
            "/load",
            &serde_json::to_vec(&self.config)?,
            Duration::from_secs(10),
        ) {
            let stopped = match self.stop() {
                Ok(()) => "stopped the owned Caddy proxy".to_owned(),
                Err(stop) => format!("stopping the owned Caddy proxy also failed: {stop:#}"),
            };
            let error = error.context(format!("could not restore previous routes; {stopped}"));
            self.stopped = Some(format!("{error:#}"));
            return Err(error);
        }
        Ok(())
    }

    fn ensure_running(&mut self) -> Result<()> {
        let Some(child) = self.child.as_mut() else {
            bail!(
                "the owned Caddy proxy is stopped, so Silicon hosts are not routed until the interpreter starts a new one. It stopped because: {}",
                self.stopped.as_deref().unwrap_or("it was stopped on request")
            );
        };
        let pid = child.id();
        if let Some(status) = child
            .try_wait()
            .with_context(|| format!("check whether Caddy process {pid} is running"))?
        {
            bail!(
                "Caddy process {pid} exited with {status}\n{}",
                self.log_since(self.log_mark)
            );
        }
        Ok(())
    }

    fn log_path(&self) -> PathBuf {
        self.state.join("caddy.log")
    }

    fn owner_path(&self) -> PathBuf {
        self.state.join("owner.json")
    }

    /// Current caddy.log length; an unreadable log counts as empty, and `log_since` then says why.
    fn log_len(&self) -> u64 {
        fs::metadata(self.log_path()).map_or(0, |metadata| metadata.len())
    }

    /// Everything Caddy wrote to its stdout and stderr after `offset`, verbatim.
    fn log_since(&self, offset: u64) -> String {
        let path = self.log_path();
        let text = fs::File::open(&path).and_then(|mut file| {
            // A log replaced since `offset` is read from its start.
            if file.metadata()?.len() >= offset {
                file.seek(SeekFrom::Start(offset))?;
            }
            let mut bytes = Vec::new();
            file.read_to_end(&mut bytes)?;
            Ok(String::from_utf8_lossy(&bytes).into_owned())
        });
        match text {
            Ok(text) if text.trim().is_empty() => {
                format!("Caddy log {}: (nothing new)", path.display())
            }
            Ok(text) => format!("Caddy log {}:\n{}", path.display(), text.trim_end()),
            Err(error) => format!("Caddy log {} could not be read: {error}", path.display()),
        }
    }

    /// One admin API call. Failures carry the HTTP status and body Caddy answered with,
    /// plus everything Caddy logged while handling the call.
    fn request(&self, method: &str, path: &str, body: &[u8], timeout: Duration) -> Result<()> {
        let mark = self.log_len();
        admin_call(&self.socket, method, path, body, timeout)
            .map_err(|error| anyhow!("{error}\n{}", self.log_since(mark)))
    }

    /// Recorded right after the spawn, so an interpreter that dies without stopping this
    /// Caddy leaves the next start what it needs to stop it.
    fn record_owner(&self, pid: u32, command: Vec<String>) {
        let owner = Owner {
            pid,
            socket: self.socket.to_string_lossy().into_owned(),
            started_at: Utc::now(),
            command,
        };
        let path = self.owner_path();
        if let Err(error) = crate::state::write_json(&path, &owner) {
            crate::stderr_line(&format!(
                "could not record Caddy process {pid} in {}: {error:#}; if this interpreter ends without stopping it, the next start cannot find and stop it",
                path.display()
            ));
        }
    }

    /// Forget the record once its Caddy is gone, but only while it still names this child:
    /// a newer Proxy may already have recorded its own.
    fn forget_owner(&self, pid: u32) {
        let path = self.owner_path();
        let recorded = match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice::<Owner>(&bytes)
                .ok()
                .map(|owner| owner.pid),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(error) => {
                crate::stderr_line(&format!(
                    "could not read {} after stopping Caddy process {pid}: {error}",
                    path.display()
                ));
                return;
            }
        };
        if recorded == Some(pid) {
            match fs::remove_file(&path) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => crate::stderr_line(&format!(
                    "could not remove {} after stopping Caddy process {pid}: {error}",
                    path.display()
                )),
            }
        }
    }

    /// Stops only this child, and always reaps it: a refused /stop, an unreadable status or
    /// a Caddy that ignores the request still end with the process killed and waited for.
    /// Config and logs are retained for inspection.
    pub fn stop(&mut self) -> Result<()> {
        let Some(mut child) = self.child.take() else {
            return Ok(());
        };
        let pid = child.id();
        // Asking first lets Caddy close listeners cleanly; the kill below covers a refusal.
        let asked = match child.try_wait() {
            Ok(Some(_)) => None,
            _ => {
                let asked = self.request("POST", "/stop", &[], Duration::from_secs(2));
                if asked.is_err() {
                    // Caddy also shuts down cleanly on SIGTERM. The child is not reaped yet,
                    // so its pid cannot belong to anything else.
                    unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
                }
                Some(asked)
            }
        };
        let deadline = Instant::now() + Duration::from_secs(2);
        let outcome = loop {
            match child.try_wait() {
                Ok(Some(_)) => break Ok(()),
                Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
                state => {
                    let why = match (state, &asked) {
                        (Err(error), _) => format!("its status could not be read ({error})"),
                        (_, Some(Ok(()))) => {
                            "it was still running 2s after it accepted /stop".to_owned()
                        }
                        (_, Some(Err(error))) => {
                            format!("it was still running 2s after SIGTERM, sent because /stop failed: {error:#}")
                        }
                        (_, None) => "it was still running".to_owned(),
                    };
                    break kill_and_reap(&mut child)
                        .with_context(|| format!("kill Caddy process {pid} after {why}"));
                }
            }
        };
        if outcome.is_ok() {
            self.forget_owner(pid);
        }
        outcome
    }
}

impl Drop for Proxy {
    fn drop(&mut self) {
        // Drop cannot return an error; the daemon's stderr is its daemon.log.
        if let Err(error) = self.stop() {
            crate::stderr_line(&format!("could not stop the owned Caddy proxy: {error:#}"));
        }
        // Best effort: an empty private directory left in /tmp changes nothing.
        let _ = fs::remove_dir_all(&self.socket_dir);
    }
}

/// SIGKILL, then wait. A child that exited in between only needs the wait; one that cannot
/// be signalled is not waited for, because that wait could last forever.
fn kill_and_reap(child: &mut Child) -> std::io::Result<()> {
    if let Err(error) = child.kill() {
        return match child.try_wait() {
            Ok(Some(_)) => Ok(()),
            _ => Err(error),
        };
    }
    child.wait().map(drop)
}

/// Caddy's JSON configuration cannot carry a path that is not UTF-8.
fn utf8(path: &Path) -> Result<&str> {
    path.to_str().ok_or_else(|| {
        anyhow!(
            "Caddy state path {} is not valid UTF-8, which Caddy's JSON configuration requires; set SILICON_INTERPRETER_HOME to a UTF-8 path",
            path.display()
        )
    })
}

/// Set a path's access and modification times to now, without following a symlink.
fn touch(path: &Path) -> std::io::Result<()> {
    let name = CString::new(path.as_os_str().as_bytes())
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
    // Null times mean "now" for both.
    if unsafe {
        libc::utimensat(
            libc::AT_FDCWD,
            name.as_ptr(),
            std::ptr::null(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } == 0
    {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

/// One admin API call on `socket`. A failure names the call and carries the HTTP status and
/// body Caddy answered with.
fn admin_call(
    socket: &Path,
    method: &str,
    path: &str,
    body: &[u8],
    timeout: Duration,
) -> Result<()> {
    let call = format!("Caddy admin API `{method} {path}` on {}", socket.display());
    let fail = |problem: String| anyhow!("{call} {problem}");
    let mut stream =
        UnixStream::connect(socket).map_err(|error| fail(format!("could not connect: {error}")))?;
    stream
        .set_read_timeout(Some(timeout))
        .and_then(|()| stream.set_write_timeout(Some(timeout)))
        .map_err(|error| fail(format!("could not set a {timeout:?} timeout: {error}")))?;
    write!(stream, "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len())
        .and_then(|()| stream.write_all(body))
        .map_err(|error| fail(format!("could not send the request: {error}")))?;
    let mut response = Vec::new();
    let read = stream.read_to_end(&mut response);
    let response = String::from_utf8_lossy(&response);
    if let Err(error) = read {
        let received = if response.is_empty() {
            "nothing".to_owned()
        } else {
            format!("\n{response}")
        };
        return Err(fail(format!(
            "could not read the answer within {timeout:?}: {error}; received {received}"
        )));
    }
    let (head, answer) = response.split_once("\r\n\r\n").unwrap_or((&response, ""));
    let status_line = head.lines().next().unwrap_or_default();
    match status_line
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
    {
        Some(code) if (200..300).contains(&code) => Ok(()),
        Some(_) if answer.trim().is_empty() => {
            Err(fail(format!("answered {status_line} with an empty body")))
        }
        Some(_) => Err(fail(format!(
            "answered {status_line}:\n{}",
            answer.trim_end()
        ))),
        None => Err(fail(format!(
            "answered without an HTTP status line; full response: {response:?}"
        ))),
    }
}

/// `GET /` on 127.0.0.1:`port` for `host`: everything the server sent, or what it sent
/// before the timeout.
fn http_get(port: u16, host: &str) -> std::io::Result<String> {
    let mut stream =
        TcpStream::connect_timeout(&SocketAddr::from(([127, 0, 0, 1], port)), CHECK_TIMEOUT)?;
    stream.set_read_timeout(Some(CHECK_TIMEOUT))?;
    stream.set_write_timeout(Some(CHECK_TIMEOUT))?;
    write!(
        stream,
        "GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    )?;
    let mut bytes = Vec::new();
    if let Err(error) = stream.read_to_end(&mut bytes) {
        if bytes.is_empty() {
            return Err(error);
        }
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

/// What a process recorded in owner.json is now.
#[derive(Debug, PartialEq)]
enum Recorded {
    /// Still the Caddy that was recorded.
    Caddy,
    /// Exited (a zombie not yet reaped counts as exited).
    Gone,
    /// Another process has the pid; its command line, or why it can never be ours.
    Other(String),
    /// Its command line could not be read.
    Unknown(String),
}

/// kill() treats 0 and negative pids as process groups; 1 is init. None of those, nor this
/// process, is ever a Caddy this interpreter started.
fn candidate(pid: u32) -> bool {
    pid > 1 && pid <= i32::MAX as u32 && pid != std::process::id()
}

fn inspect(pid: u32, command: &[String]) -> Recorded {
    if !candidate(pid) {
        return Recorded::Other(format!(
            "pid {pid} can never be a Caddy this interpreter started"
        ));
    }
    if unsafe { libc::kill(pid as libc::pid_t, 0) } != 0 {
        let error = std::io::Error::last_os_error();
        return match error.raw_os_error() {
            Some(libc::ESRCH) => Recorded::Gone,
            Some(libc::EPERM) => {
                Recorded::Other("a process of another user, which this one cannot signal".into())
            }
            _ => Recorded::Unknown(format!(
                "could not check whether process {pid} exists: {error}"
            )),
        };
    }
    match command_line(pid) {
        Ok(None) => Recorded::Gone,
        Ok(Some(line)) if line.is_empty() || line == "<defunct>" => Recorded::Gone,
        Ok(Some(line)) if runs(&line, command) => Recorded::Caddy,
        Ok(Some(line)) => Recorded::Other(line),
        Err(error) => Recorded::Unknown(error),
    }
}

/// A process's argv joined with spaces; None when it no longer exists.
fn command_line(pid: u32) -> std::result::Result<Option<String>, String> {
    if cfg!(target_os = "linux") {
        let path = format!("/proc/{pid}/cmdline");
        return match fs::read(&path) {
            Ok(bytes) => Ok(Some(
                String::from_utf8_lossy(&bytes)
                    .trim_end_matches('\0')
                    .replace('\0', " "),
            )),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(format!("could not read {path}: {error}")),
        };
    }
    // By path: the interpreter's PATH is whatever its supervisor gave it.
    let shown = format!("/bin/ps -o command= -p {pid}");
    let mut ps = Command::new("/bin/ps");
    // Outside a UTF-8 locale (a launchd agent has none) ps escapes every non-ASCII byte, so
    // `josé` prints as `josM-CM-)` and a Caddy under such a path would never match.
    // en_US.UTF-8 ships with every macOS release; C.UTF-8 only with recent ones.
    ps.args(["-o", "command=", "-p", &pid.to_string()])
        .env("LC_ALL", "en_US.UTF-8");
    match crate::process::output_within(&mut ps, Duration::from_secs(10)) {
        Ok(output) if output.status.success() => Ok(Some(
            String::from_utf8_lossy(&output.stdout)
                .trim_end_matches(['\r', '\n'])
                .to_owned(),
        )),
        // ps answers a pid that does not exist with a failure status and no output.
        Ok(output)
            if output.stdout.iter().all(u8::is_ascii_whitespace)
                && output.stderr.iter().all(u8::is_ascii_whitespace) =>
        {
            Ok(None)
        }
        Ok(output) => Err(format!(
            "`{shown}` failed: {}",
            crate::failure::describe(&output)
        )),
        Err(error) => Err(format!("could not run `{shown}`: {error}")),
    }
}

/// Whether a command line runs `command`, directly or through the interpreter of a script
/// (`/bin/sh /path/caddy run ...`). The arguments include this state directory's pid file,
/// so no other interpreter's Caddy matches.
fn runs(line: &str, command: &[String]) -> bool {
    let Some((program, args)) = command.split_first() else {
        return false;
    };
    let name = Path::new(program)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    if args.is_empty() || name.is_empty() {
        return false;
    }
    line.strip_suffix(&format!(" {}", args.join(" ")))
        .and_then(|head| head.strip_suffix(name.as_str()))
        .is_some_and(|before| before.is_empty() || before.ends_with(['/', ' ']))
}

/// Whether `pid` stopped existing within `timeout`.
fn gone_within(pid: u32, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if unsafe { libc::kill(pid as libc::pid_t, 0) } != 0
            && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
        {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(50));
    }
}

/// The admin socket path a Proxy creates, so a recorded path is only ever used, and its
/// directory only ever removed, when it has that shape.
fn socket_shaped(socket: &Path) -> bool {
    socket.file_name() == Some(OsStr::new("admin.sock"))
        && socket
            .parent()
            .and_then(Path::file_name)
            .is_some_and(|name| name.as_bytes().starts_with(b"silicon-caddy-"))
}

/// Log a step of stopping an earlier Caddy to daemon.log and keep it for the start's result.
fn note(said: &mut Vec<String>, line: String) {
    crate::stderr_line(&line);
    said.push(line);
}

/// A Caddy whose interpreter ended without stopping it keeps port 80 with stale routes, and
/// with SO_REUSEPORT a new Caddy binds beside it without an error and may never see a
/// connection. owner.json names it, so the next start stops it first. A pid that is not that
/// Caddy any more is never signalled.
///
/// Returns every step taken, for daemon.log and any later start error; fails, keeping the
/// record and admin socket for the next attempt, when that Caddy is still running after
/// SIGKILL or whether it still runs cannot be told.
fn reap_orphan(state: &Path) -> Result<Option<String>> {
    reap_orphan_with(state, &inspect)
}

/// What a recorded pid is now: [`inspect`], or a stand-in in tests.
type Inspect = dyn Fn(u32, &[String]) -> Recorded;

fn reap_orphan_with(state: &Path, inspect: &Inspect) -> Result<Option<String>> {
    let path = state.join("owner.json");
    let mut said = Vec::new();
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            note(&mut said, format!(
                "could not read {}: {error}; a Caddy an earlier interpreter left running, if any, was not stopped",
                path.display()
            ));
            return Ok(Some(said.join("\n")));
        }
    };
    let owner = match serde_json::from_slice::<Owner>(&bytes) {
        Ok(owner) => owner,
        Err(error) => {
            note(&mut said, format!(
                "{} is not a Caddy owner record ({error}), so no Caddy an earlier interpreter left running was stopped; it held: {}",
                path.display(),
                String::from_utf8_lossy(&bytes)
            ));
            forget(&path, &mut said);
            return Ok(Some(said.join("\n")));
        }
    };
    let pid = owner.pid;
    let described = format!(
        "Caddy process {pid} (`{}`, started {}, admin socket {})",
        owner.command.join(" "),
        owner.started_at.to_rfc3339(),
        owner.socket
    );
    let recorded = format!("recorded in {}", path.display());
    let found = inspect(pid, &owner.command);
    if found == Recorded::Caddy {
        let line = format!(
            "{described}, {recorded} by an earlier interpreter, is still running; stopping it"
        );
        note(&mut said, line);
    }
    let socket = PathBuf::from(&owner.socket);
    let shown = socket.display();
    // The socket path is unique to that Caddy, so whatever answers there is it.
    let asked = if !socket_shaped(&socket) {
        let line = format!("the recorded admin socket {shown} is not one a Silicon Caddy uses, so it was left alone");
        note(&mut said, line);
        false
    } else if fs::symlink_metadata(&socket).is_ok() {
        let asked = admin_call(&socket, "POST", "/stop", &[], Duration::from_secs(2));
        let line = match &asked {
            Ok(()) => format!("asked Caddy to stop through {shown}: accepted"),
            Err(error) => format!("could not ask Caddy to stop through {shown}: {error:#}"),
        };
        note(&mut said, line);
        asked.is_ok()
    } else {
        false
    };
    let line = match found {
        Recorded::Caddy => {
            stop_orphan(pid, &owner.command, asked, &mut said, inspect)?;
            None
        }
        Recorded::Gone => Some(format!(
            "{described}, {recorded} by an earlier interpreter that did not stop it, is no longer running"
        )),
        // Only that Caddy answers its unique socket, so after an accepted /stop its pid is
        // worth waiting for, whatever its command line looks like; it is never signalled.
        Recorded::Other(line) if asked && candidate(pid) => Some(if gone_within(pid, REAP_GRACE) {
            format!("process {pid} exited after the Caddy at {shown} accepted /stop ({recorded} for {described}; its command line was {line})")
        } else {
            format!(
                "process {pid}, {recorded} for {described}, was still running {}s after the Caddy at {shown} accepted /stop; its command line ({line}) is not that Caddy's, so it was not signalled",
                REAP_GRACE.as_secs()
            )
        }),
        Recorded::Other(line) => Some(format!(
            "process {pid}, {recorded} for {described}, is not that Caddy any more ({line}), so it was left alone"
        )),
        Recorded::Unknown(error) if asked && gone_within(pid, REAP_GRACE) => Some(format!(
            "{described} exited after it accepted /stop (whether the pid was still that Caddy could not be checked: {error})"
        )),
        // Starting beside a Caddy that may still hold the port would split its traffic, and
        // forgetting the record would leave nothing to stop it with later.
        Recorded::Unknown(error) => bail!(
            "{}\ncould not tell whether process {pid} is still {described}, {recorded}: {error}; it was not signalled, and its record and admin socket were kept so the next start checks again (if process {pid} is not that Caddy, remove {})",
            said.join("\n"),
            path.display()
        ),
    };
    if let Some(line) = line {
        note(&mut said, line);
    }
    if socket_shaped(&socket) {
        remove_socket(&socket, &mut said);
    }
    forget(&path, &mut said);
    Ok(Some(said.join("\n")))
}

/// SIGTERM, then SIGKILL, each only after confirming the pid is still that Caddy.
fn stop_orphan(
    pid: u32,
    command: &[String],
    asked: bool,
    said: &mut Vec<String>,
    inspect: &Inspect,
) -> Result<()> {
    if asked && gone_within(pid, REAP_GRACE) {
        note(said, format!("Caddy process {pid} exited"));
        return Ok(());
    }
    for (signal, name) in [(libc::SIGTERM, "SIGTERM"), (libc::SIGKILL, "SIGKILL")] {
        match inspect(pid, command) {
            Recorded::Caddy => {}
            Recorded::Gone => {
                note(said, format!("Caddy process {pid} exited"));
                return Ok(());
            }
            Recorded::Other(line) => {
                note(said, format!(
                    "Caddy process {pid} exited; the pid now belongs to another process ({line}), which was left alone"
                ));
                return Ok(());
            }
            Recorded::Unknown(error) => bail!(
                "{}\ncould not confirm that process {pid} is still that Caddy before sending {name}, so it was not signalled: {error}",
                said.join("\n")
            ),
        }
        note(said, format!("sending {name} to Caddy process {pid}"));
        if unsafe { libc::kill(pid as libc::pid_t, signal) } != 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ESRCH) {
                note(said, format!("Caddy process {pid} exited"));
                return Ok(());
            }
            note(
                said,
                format!("{name} to Caddy process {pid} failed: {error}"),
            );
        }
        if gone_within(pid, REAP_GRACE) {
            note(said, format!("Caddy process {pid} exited after {name}"));
            return Ok(());
        }
    }
    // Still there: a zombie its new parent has not reaped yet has exited all the same.
    match inspect(pid, command) {
        Recorded::Gone | Recorded::Other(_) => {
            note(said, format!("Caddy process {pid} exited after SIGKILL"));
            Ok(())
        }
        Recorded::Caddy => bail!(
            "{}\nCaddy process {pid} is still running {}s after SIGKILL, so no second Caddy was started beside it",
            said.join("\n"),
            REAP_GRACE.as_secs()
        ),
        Recorded::Unknown(error) => bail!(
            "{}\nprocess {pid} still exists {}s after SIGKILL, and whether it is still that Caddy could not be checked, so no second Caddy was started beside it: {error}",
            said.join("\n"),
            REAP_GRACE.as_secs()
        ),
    }
}

/// Remove a dead Caddy's admin socket and its private directory. Only the socket file and
/// its then-empty directory go; anything else in it stays and is reported.
fn remove_socket(socket: &Path, said: &mut Vec<String>) {
    let Some(directory) = socket.parent() else {
        return;
    };
    let removed = [
        fs::remove_file(socket).map_err(|error| (socket, error)),
        fs::remove_dir(directory).map_err(|error| (directory, error)),
    ];
    let mut gone = false;
    for result in removed {
        match result {
            Ok(()) => gone = true,
            Err((_, error)) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err((path, error)) => note(
                said,
                format!("could not remove {}: {error}", path.display()),
            ),
        }
    }
    if gone {
        note(
            said,
            format!(
                "removed the stale admin socket directory {}",
                directory.display()
            ),
        );
    }
}

fn forget(path: &Path, said: &mut Vec<String>) {
    match fs::remove_file(path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => note(
            said,
            format!("could not remove {}: {error}", path.display()),
        ),
    }
}

/// On Linux a port below ip_unprivileged_port_start needs CAP_NET_BIND_SERVICE. The
/// installer grants it to the Caddy file, but the kernel ignores file capabilities on a
/// nosuid mount and under no_new_privs while getcap still lists them, so the facts go with
/// the error instead of a guess.
fn port_facts(binary: &OsStr, port: u16) -> String {
    let mut facts = vec![format!(
        "What this Linux system reports about binding port {port}:"
    )];
    let start = "/proc/sys/net/ipv4/ip_unprivileged_port_start";
    facts.push(match fs::read_to_string(start) {
        Ok(value) => format!(
            "{start}: {} (a port below it needs CAP_NET_BIND_SERVICE; `sudo sysctl net.ipv4.ip_unprivileged_port_start={port}` lowers it)",
            value.trim()
        ),
        Err(error) => format!("{start} could not be read: {error}"),
    });
    match resolve_program(binary) {
        Some(path) => {
            // getcap usually lives in an sbin directory, which a user's PATH may lack.
            let getcap = resolve_program(OsStr::new("getcap"))
                .or_else(|| {
                    ["/usr/sbin/getcap", "/sbin/getcap"]
                        .into_iter()
                        .map(PathBuf::from)
                        .find(|candidate| candidate.is_file())
                })
                .unwrap_or_else(|| PathBuf::from("getcap"));
            let shown = crate::failure::argv(&getcap, &[path.to_string_lossy()]);
            let mut getcap = Command::new(&getcap);
            getcap.arg(&path);
            facts.push(
                match crate::process::output_within(&mut getcap, Duration::from_secs(10)) {
                    Ok(output) => format!("`{shown}`: {}", crate::failure::describe(&output)),
                    Err(error) => format!("could not run `{shown}`: {error}"),
                },
            );
            facts.push(match fs::read_to_string("/proc/self/mountinfo") {
                Ok(text) => match mount_of(&path, &text) {
                    Some((point, options)) if options.split(',').any(|option| option == "nosuid") => format!(
                        "{} is on the mount {point} with options {options}: the kernel ignores file capabilities on a nosuid mount, even ones getcap lists",
                        path.display()
                    ),
                    Some((point, options)) => format!(
                        "{} is on the mount {point} with options {options} (not nosuid)",
                        path.display()
                    ),
                    None => format!(
                        "no mount in /proc/self/mountinfo contains {}",
                        path.display()
                    ),
                },
                Err(error) => format!("/proc/self/mountinfo could not be read: {error}"),
            });
        }
        None => facts.push(format!(
            "the Caddy executable `{}` was not found on PATH, so its capability and mount could not be inspected",
            binary.to_string_lossy()
        )),
    }
    facts.push(match fs::read_to_string("/proc/self/status") {
        Ok(status) => match status.lines().find(|line| line.starts_with("NoNewPrivs:")) {
            Some(line) if line.split_whitespace().nth(1) == Some("1") => format!(
                "/proc/self/status {line}: the interpreter runs with no_new_privs, which Caddy inherits, so the kernel ignores file capabilities"
            ),
            Some(line) => format!("/proc/self/status {line}"),
            None => "/proc/self/status has no NoNewPrivs line".to_owned(),
        },
        Err(error) => format!("/proc/self/status could not be read: {error}"),
    });
    facts.join("\n")
}

/// The file `Command::new(binary)` runs: the path itself, or the first match on PATH.
fn resolve_program(binary: &OsStr) -> Option<PathBuf> {
    let program = Path::new(binary);
    let found = if binary.as_bytes().contains(&b'/') {
        program.to_path_buf()
    } else {
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
            .map(|directory| directory.join(program))
            .find(|candidate| candidate.is_file())?
    };
    Some(found.canonicalize().unwrap_or(found))
}

/// The mount point and per-mount options of the mount containing `path`, from
/// /proc/self/mountinfo text. The longest containing mount point wins; a later line for the
/// same point is a mount stacked on top and replaces the earlier one.
fn mount_of(path: &Path, mountinfo: &str) -> Option<(String, String)> {
    let mut best: Option<(PathBuf, String)> = None;
    for line in mountinfo.lines() {
        let fields: Vec<&str> = line.split(' ').collect();
        let (Some(point), Some(options)) = (fields.get(4), fields.get(5)) else {
            continue;
        };
        let point = PathBuf::from(OsStr::from_bytes(&unescape(point)));
        if !path.starts_with(&point) {
            continue;
        }
        if best
            .as_ref()
            .is_none_or(|(current, _)| point.components().count() >= current.components().count())
        {
            best = Some((point, (*options).to_owned()));
        }
    }
    best.map(|(point, options)| (point.display().to_string(), options))
}

/// mountinfo escapes space, tab, newline and backslash as `\` plus three octal digits.
fn unescape(field: &str) -> Vec<u8> {
    let bytes = field.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let octal = bytes.get(index + 1..index + 4).filter(|digits| {
            bytes[index] == b'\\' && digits.iter().all(|digit| (b'0'..=b'7').contains(digit))
        });
        match octal {
            Some(digits) => {
                let value = digits
                    .iter()
                    .fold(0u32, |value, digit| value * 8 + u32::from(digit - b'0'));
                out.push(value as u8);
                index += 4;
            }
            None => {
                out.push(bytes[index]);
                index += 1;
            }
        }
    }
    out
}

fn validate_hosts(hosts: &[String]) -> Result<Vec<String>> {
    let mut allowed = BTreeSet::from(["silicon.localhost".to_owned()]);
    let mut invalid = Vec::new();
    for host in hosts {
        let host = host.to_ascii_lowercase();
        if !host.ends_with(".localhost")
            || host.len() > 253
            || !host.split('.').all(|label| {
                !label.is_empty()
                    && label.len() <= 63
                    && label.as_bytes()[0].is_ascii_alphanumeric()
                    && label.as_bytes()[label.len() - 1].is_ascii_alphanumeric()
                    && label
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            })
        {
            invalid.push(format!("{host:?}"));
        } else {
            allowed.insert(host);
        }
    }
    if !invalid.is_empty() {
        bail!(
            "invalid Silicon proxy hostname {}; expected a DNS name ending in .localhost, at most 253 characters, whose labels are 1–63 letters, digits or inner hyphens",
            invalid.join(", ")
        );
    }
    Ok(allowed.into_iter().collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::UnixListener;

    fn fetch(port: u16, host: &str) -> String {
        fetch_from("127.0.0.1", port, host, "")
    }

    fn fetch_from(address: &str, port: u16, host: &str, headers: &str) -> String {
        let mut stream = TcpStream::connect((address, port)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        write!(
            stream,
            "GET / HTTP/1.1\r\nHost: {host}\r\n{headers}Connection: close\r\n\r\n"
        )
        .unwrap();
        let mut response = String::new();
        stream.read_to_string(&mut response).unwrap();
        response
    }

    /// A Proxy over `state` that talks to `socket`, without a Caddy of its own.
    fn detached(state: &Path, socket: PathBuf) -> Proxy {
        Proxy {
            child: None,
            socket_dir: state.join("unused"),
            socket,
            state: state.to_path_buf(),
            config: json!({"previous": true}),
            interpreter_port: 1,
            http_port: 1,
            ipv6: false,
            log_mark: 0,
            stopped: None,
            nonce: "test-nonce".into(),
            unprobed: 0,
            upkeep_failure: None,
        }
    }

    /// Reads one HTTP request, body included; returns its request line.
    fn read_request(stream: &UnixStream) -> String {
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut first = String::new();
        reader.read_line(&mut first).unwrap();
        let mut length = 0;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if line == "\r\n" {
                break;
            }
            if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                length = value.trim().parse().unwrap();
            }
        }
        reader.read_exact(&mut vec![0; length]).unwrap();
        first.trim_end().to_owned()
    }

    fn fake_caddy(dir: &Path, script: &str) -> PathBuf {
        let caddy = dir.join("caddy");
        fs::write(&caddy, script).unwrap();
        fs::set_permissions(&caddy, fs::Permissions::from_mode(0o700)).unwrap();
        caddy
    }

    /// The argv a Proxy over `state` records for `caddy`.
    fn caddy_command(caddy: &Path, state: &Path) -> Vec<String> {
        [caddy.to_str().unwrap(), "run", "--config", "-", "--pidfile"]
            .into_iter()
            .map(str::to_owned)
            .chain([state.join("caddy.pid").to_str().unwrap().to_owned()])
            .collect()
    }

    /// A fake Caddy run the way a Proxy over `state` runs it, but not as this process's
    /// child: init reaps it, as it reaps a real orphan. It ends by itself after a minute so
    /// a failing test leaves nothing behind.
    fn orphan(dir: &Path, state: &Path, ignores_sigterm: bool) -> (u32, Vec<String>) {
        let ready = dir.join("ready");
        let caddy = fake_caddy(
            dir,
            &format!(
                "#!/bin/sh\n{}: > '{}'\ni=0\nwhile [ $i -lt 60 ]; do sleep 1; i=$((i+1)); done\n",
                if ignores_sigterm {
                    "trap '' TERM\n"
                } else {
                    ""
                },
                ready.display()
            ),
        );
        let command = caddy_command(&caddy, state);
        let output = Command::new("sh")
            .arg("-c")
            .arg("\"$@\" </dev/null >/dev/null 2>&1 & echo $!")
            .arg("sh")
            .args(&command)
            .output()
            .unwrap();
        let pid = String::from_utf8(output.stdout)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        // Until the background shell execs the script, its command line is the shell's, and
        // until the script signals readiness, its trap may not be set yet.
        let deadline = Instant::now() + Duration::from_secs(5);
        while !ready.exists() || inspect(pid, &command) != Recorded::Caddy {
            assert!(Instant::now() < deadline, "{:?}", inspect(pid, &command));
            thread::sleep(Duration::from_millis(20));
        }
        (pid, command)
    }

    fn record(state: &Path, pid: u32, socket: &Path, command: Vec<String>) {
        crate::state::write_json(
            &state.join("owner.json"),
            &Owner {
                pid,
                socket: socket.to_str().unwrap().to_owned(),
                started_at: Utc::now(),
                command,
            },
        )
        .unwrap();
    }

    fn set_old_times(path: &Path) {
        let name = CString::new(path.as_os_str().as_bytes()).unwrap();
        let mut old: libc::timespec = unsafe { std::mem::zeroed() };
        old.tv_sec = 946_684_800;
        let times = [old, old];
        assert_eq!(
            unsafe {
                libc::utimensat(
                    libc::AT_FDCWD,
                    name.as_ptr(),
                    times.as_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            },
            0
        );
    }

    fn modified_recently(path: &Path) -> bool {
        let modified = fs::symlink_metadata(path).unwrap().modified().unwrap();
        modified
            .elapsed()
            .is_ok_and(|age| age < Duration::from_secs(60))
    }

    #[test]
    fn only_local_dns_hosts_can_be_routed() {
        assert_eq!(
            validate_hosts(&["A.ORG.localhost".into(), "a.org.localhost".into()]).unwrap(),
            vec!["a.org.localhost", "silicon.localhost"]
        );
        for host in [
            "evil.com",
            "a.localhost:123",
            "*.localhost",
            "a/.localhost",
            ".localhost",
        ] {
            assert!(validate_hosts(&[host.into()]).is_err());
        }
        let error = validate_hosts(&[
            "ok.localhost".into(),
            "evil.com".into(),
            "*.localhost".into(),
        ])
        .unwrap_err()
        .to_string();
        assert!(error.contains(r#""evil.com", "*.localhost""#), "{error}");
    }

    #[test]
    fn caddy_that_exits_reports_status_and_everything_it_wrote() {
        let dir = tempfile::tempdir().unwrap();
        let caddy = fake_caddy(
            dir.path(),
            "#!/bin/sh\ncat >/dev/null\necho 'using config from stdin'\necho 'Error: loading initial config: listen tcp :80: bind: permission denied' >&2\nexit 3\n",
        );
        let state = dir.path().join("state");
        crate::state::private_dir(&state.join("caddy")).unwrap();
        // An earlier run's lines are not this failure's explanation.
        fs::write(state.join("caddy/caddy.log"), "earlier run\n").unwrap();
        let error = Proxy::start_on(&state, 1, &[], 1, caddy.as_os_str())
            .err()
            .unwrap();
        let error = format!("{error:#}");
        assert!(error.contains("exited with exit status: 3"), "{error}");
        assert!(error.contains("using config from stdin"), "{error}");
        assert!(
            error
                .contains("Error: loading initial config: listen tcp :80: bind: permission denied"),
            "{error}"
        );
        assert!(!error.contains("earlier run"), "{error}");
        // The failed start forgot the Caddy it recorded.
        assert!(!state.join("caddy/owner.json").exists());

        let missing = dir.path().join("no-caddy");
        let error = Proxy::start_on(&state, 1, &[], 1, missing.as_os_str())
            .err()
            .unwrap();
        let error = format!("{error:#}");
        let pidfile = state.canonicalize().unwrap().join("caddy/caddy.pid");
        assert!(
            error.contains(&format!(
                "could not run `no-caddy run --config - --pidfile {}`",
                pidfile.display()
            )),
            "{error}"
        );
        assert!(error.contains("No such file or directory"), "{error}");
    }

    #[test]
    fn a_caddy_log_past_its_cap_moves_aside_before_caddy_starts() {
        let dir = tempfile::tempdir().unwrap();
        let caddy = fake_caddy(
            dir.path(),
            "#!/bin/sh\ncat >/dev/null\necho 'this run'\nexit 1\n",
        );
        let state = dir.path().join("state");
        crate::state::private_dir(&state.join("caddy")).unwrap();
        let log = state.join("caddy/caddy.log");
        fs::write(&log, "months of lines\n").unwrap();
        // Sparse: the size counts, not the disk it would take.
        OpenOptions::new()
            .write(true)
            .open(&log)
            .unwrap()
            .set_len(crate::LOG_CAP)
            .unwrap();
        let error = format!(
            "{:#}",
            Proxy::start_on(&state, 1, &[], 1, caddy.as_os_str())
                .err()
                .unwrap()
        );
        assert!(error.contains("this run"), "{error}");
        assert!(!error.contains("months of lines"), "{error}");
        let rotated = crate::numbered(&log, "1");
        assert_eq!(fs::metadata(&rotated).unwrap().len(), crate::LOG_CAP);
        let mut start = [0; 16];
        fs::File::open(&rotated)
            .unwrap()
            .read_exact(&mut start)
            .unwrap();
        assert_eq!(&start, b"months of lines\n");
        assert_eq!(fs::read_to_string(&log).unwrap(), "this run\n");
    }

    #[test]
    fn a_state_path_that_is_not_utf8_is_an_error_naming_it() {
        let state = PathBuf::from(OsStr::from_bytes(b"/tmp/silicon-\xff/caddy"));
        let proxy = detached(&state, PathBuf::from("/tmp/silicon-caddy-x/admin.sock"));
        for error in [
            proxy.base_config().unwrap_err(),
            proxy.configuration(&[]).unwrap_err(),
        ] {
            let error = format!("{error:#}");
            assert!(
                error.starts_with(&format!(
                    "Caddy state path {} is not valid UTF-8",
                    state.join("storage").display()
                )) && error.contains("SILICON_INTERPRETER_HOME"),
                "{error}"
            );
        }
    }

    #[test]
    fn routes_answer_the_probe_first_and_keep_admin_calls_out_of_the_log() {
        let dir = tempfile::tempdir().unwrap();
        let proxy = detached(dir.path(), dir.path().join("admin.sock"));
        let config = proxy.configuration(&["a.org.localhost".into()]).unwrap();
        assert_eq!(
            config["logging"],
            json!({"logs": {"default": {"exclude": ["admin.api"]}}})
        );
        let routes = &config["apps"]["http"]["servers"]["silicon"]["routes"];
        assert_eq!(
            routes[0],
            json!({"match": [{"host": ["probe-test-nonce.silicon.localhost"],
                "remote_ip": {"ranges": ["127.0.0.1/32", "::1/128"]}}],
                "handle": [{"handler": "static_response", "status_code": 200, "body": "test-nonce"}],
                "terminal": true})
        );
        assert_eq!(
            routes[1]["match"][0]["host"],
            json!(["a.org.localhost", "silicon.localhost"])
        );
        assert_eq!(routes[2]["handle"][0]["status_code"], 404);
    }

    const OURS: &str =
        "HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\ntest-nonce";
    const SQUATTER: &str = "HTTP/1.1 404 Not Found\r\nServer: Caddy\r\nContent-Length: 21\r\nConnection: close\r\n\r\nUnknown Silicon host\n";

    /// An HTTP port whose `n`th connection gets `responses[n]`; returns the Host of each.
    fn http_port(responses: Vec<&'static str>) -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let mut hosts = Vec::new();
            for response in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    if let Some(host) = line.strip_prefix("Host: ") {
                        hosts.push(host.trim_end().to_owned());
                    }
                    if line == "\r\n" {
                        break;
                    }
                }
                stream.write_all(response.as_bytes()).unwrap();
            }
            hosts
        });
        (port, server)
    }

    #[test]
    fn only_this_caddy_answering_the_probe_host_counts_as_listening() {
        let dir = tempfile::tempdir().unwrap();
        let mut proxy = detached(dir.path(), dir.path().join("admin.sock"));
        let (port, ours) = http_port(vec![OURS; PROBES as usize]);
        proxy.http_port = port;
        proxy.verify_listener().unwrap();
        assert_eq!(
            ours.join().unwrap(),
            vec!["probe-test-nonce.silicon.localhost"; PROBES as usize]
        );

        fs::write(dir.path().join("caddy.log"), "loaded routes\n").unwrap();
        let (port, other) = http_port(vec![SQUATTER]);
        proxy.http_port = port;
        let error = format!("{:#}", proxy.verify_listener().unwrap_err());
        other.join().unwrap();
        assert_eq!(
            error,
            format!(
                "another server answers HTTP port {port}: request 1 of {PROBES} for Host probe-test-nonce.silicon.localhost, which only this Caddy answers (with test-nonce), got:\n{SQUATTER}\nCaddy log {}:\nloaded routes",
                dir.path().join("caddy.log").display()
            )
        );

        // Linux hands each connection to one of the listeners sharing the port, so a server
        // beside this Caddy answers only some requests; one of them is enough.
        let mut shared = vec![OURS; 7];
        shared.push(SQUATTER);
        let (port, split) = http_port(shared);
        proxy.http_port = port;
        let error = format!("{:#}", proxy.verify_listener().unwrap_err());
        assert_eq!(split.join().unwrap().len(), 8);
        assert!(
            error.starts_with(&format!(
                "another server answers HTTP port {port}: request 8 of {PROBES} for Host"
            )),
            "{error}"
        );

        // Nothing listening: the request's own error, and what Caddy logged.
        let closed = TcpListener::bind("127.0.0.1:0").unwrap();
        proxy.http_port = closed.local_addr().unwrap().port();
        drop(closed);
        let error = format!("{:#}", proxy.verify_listener().unwrap_err());
        assert!(
            error.starts_with(&format!(
                "request 1 of {PROBES} to http://127.0.0.1:{}/ for Host probe-test-nonce.silicon.localhost, which only this Caddy answers, failed: ",
                proxy.http_port
            )) && error.contains("refused")
                && error.ends_with("loaded routes"),
            "{error}"
        );
    }

    #[test]
    fn a_check_probes_the_http_port_every_so_often() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path();
        let socket_dir = state.join("silicon-caddy-live");
        fs::create_dir(&socket_dir).unwrap();
        let socket = socket_dir.join("admin.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let admin = thread::spawn(move || {
            for _ in 0..2 * PROBE_EVERY {
                let (mut stream, _) = listener.accept().unwrap();
                read_request(&stream);
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                    .unwrap();
            }
        });
        let mut proxy = detached(state, socket);
        proxy.socket_dir = socket_dir;
        let child = Command::new("sleep").arg("30").spawn().unwrap();
        let pid = child.id();
        proxy.child = Some(child);
        // The first probe finds only this Caddy; the next finds a server started beside it.
        let mut answers = vec![OURS; PROBES as usize];
        answers.extend([OURS, OURS, SQUATTER]);
        let (port, http) = http_port(answers);
        proxy.http_port = port;
        for _ in 1..PROBE_EVERY {
            proxy.check().unwrap();
        }
        assert_eq!(proxy.unprobed, PROBE_EVERY - 1);
        proxy.check().unwrap();
        assert_eq!(proxy.unprobed, 0);
        for _ in 1..PROBE_EVERY {
            proxy.check().unwrap();
        }
        let error = format!("{:#}", proxy.check().unwrap_err());
        admin.join().unwrap();
        assert_eq!(http.join().unwrap().len(), PROBES as usize + 3);
        assert!(
            error.starts_with(&format!(
                "Caddy process {pid} is running, but a probe of HTTP port {port} did not get only its answers: another server answers HTTP port {port}: request 3 of {PROBES}"
            )) && error.contains("Unknown Silicon host"),
            "{error}"
        );
        proxy.stop().unwrap();
    }

    #[test]
    fn a_failed_check_shows_what_caddy_logged_since_it_was_last_healthy() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path();
        let socket = state.join("silicon-caddy-hung/admin.sock");
        let log = state.join("caddy.log");
        fs::write(&log, "started\n").unwrap();
        let mut proxy = detached(state, socket.clone());
        proxy.child = Some(Command::new("sleep").arg("30").spawn().unwrap());
        // The last healthy check was here; Caddy explained the failure after it.
        proxy.log_mark = 8;
        fs::write(
            &log,
            "started\n{\"level\":\"error\",\"logger\":\"admin\",\"msg\":\"accept error: too many open files\"}\n",
        )
        .unwrap();
        let error = format!("{:#}", proxy.check().unwrap_err());
        assert!(
            error.contains(&format!(
                "Caddy admin API `GET /config/` on {} could not connect: ",
                socket.display()
            )) && error.ends_with(&format!(
                "Caddy log {}:\n{{\"level\":\"error\",\"logger\":\"admin\",\"msg\":\"accept error: too many open files\"}}",
                log.display()
            )),
            "{error}"
        );
        assert!(!error.contains("started"), "{error}");
        // A failed check is not a healthy one.
        assert_eq!(proxy.log_mark, 8);
        proxy.stop().unwrap();
    }

    #[test]
    fn rejected_reload_reports_caddy_status_body_and_log() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().to_path_buf();
        let log = state.join("caddy.log");
        fs::write(&log, "earlier line\n").unwrap();
        let socket = state.join("admin.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let server = thread::spawn(move || {
            for answer in [
                "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n\r\n{\"error\":\"loading new config: http app module: start: listening on 127.0.0.1:80: bind: address already in use\"}\n",
                "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                read_request(&stream);
                OpenOptions::new()
                    .append(true)
                    .open(&log)
                    .unwrap()
                    .write_all(b"{\"level\":\"error\",\"logger\":\"http\",\"msg\":\"listen tcp 127.0.0.1:80: bind: address already in use\"}\n")
                    .unwrap();
                stream.write_all(answer.as_bytes()).unwrap();
            }
        });
        let mut proxy = detached(&state, socket);
        let error = format!(
            "{:#}",
            proxy.replace_config(json!({"next": true})).unwrap_err()
        );
        server.join().unwrap();
        for said in [
            "Caddy reload failed; previous routes are active",
            "Caddy admin API `POST /load` on ",
            "answered HTTP/1.1 400 Bad Request:\n{\"error\":\"loading new config: http app module: start: listening on 127.0.0.1:80: bind: address already in use\"}",
            "{\"level\":\"error\",\"logger\":\"http\",\"msg\":\"listen tcp 127.0.0.1:80: bind: address already in use\"}",
        ] {
            assert!(error.contains(said), "{error}");
        }
        assert!(!error.contains("earlier line"), "{error}");
        assert_eq!(proxy.config, json!({"previous": true}));
        assert!(!proxy.state.join("config.json").exists());
    }

    #[test]
    fn unrestorable_routes_explain_every_later_call() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("gone.sock");
        let mut proxy = detached(dir.path(), socket.clone());
        let refused = format!(
            "`POST /load` on {} could not connect: No such file or directory",
            socket.display()
        );
        let error = format!(
            "{:#}",
            proxy.replace_config(json!({"next": true})).unwrap_err()
        );
        assert!(
            error.contains("could not restore previous routes"),
            "{error}"
        );
        assert_eq!(error.matches(&refused).count(), 2, "{error}");
        // A later call names the stop and the failure behind it, not just "stopped"; the
        // interpreter answers that error by replacing this proxy with a new Caddy.
        for later in [proxy.update(&[]).unwrap_err(), proxy.check().unwrap_err()] {
            let later = format!("{later:#}");
            for said in [
                "the owned Caddy proxy is stopped, so Silicon hosts are not routed until the interpreter starts a new one",
                "It stopped because: could not restore previous routes; stopped the owned Caddy proxy",
                refused.as_str(),
            ] {
                assert!(later.contains(said), "{later}");
            }
        }
    }

    #[test]
    fn a_caddy_that_lost_its_admin_socket_fails_its_check_and_is_still_stopped() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path();
        let socket = state.join("silicon-caddy-gone/admin.sock");
        // Alive but unreachable: nothing answers the socket, so /stop cannot be asked either.
        let child = Command::new("sleep").arg("30").spawn().unwrap();
        let pid = child.id();
        let mut proxy = detached(state, socket.clone());
        proxy.child = Some(child);
        record(state, pid, &socket, vec!["sleep".into(), "30".into()]);
        let error = format!("{:#}", proxy.update(&[]).unwrap_err());
        for said in [
            format!(
                "Caddy process {pid} is running, but its private admin socket {} did not answer a health check",
                socket.display()
            ),
            format!(
                "Caddy admin API `GET /config/` on {} could not connect: No such file or directory",
                socket.display()
            ),
        ] {
            assert!(error.contains(&said), "{error}");
        }
        let started = Instant::now();
        proxy.stop().unwrap();
        assert!(started.elapsed() < Duration::from_secs(10));
        assert!(proxy.child.is_none());
        assert!(gone_within(pid, Duration::from_secs(1)));
        assert!(!state.join("owner.json").exists());

        // An exited Caddy is only reaped, and a record naming another process stays.
        let mut exited = Command::new("true").spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while exited.try_wait().unwrap().is_none() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        record(state, pid, &socket, vec!["caddy".into()]);
        proxy.child = Some(exited);
        proxy.stop().unwrap();
        assert!(state.join("owner.json").exists());
        assert!(proxy.stop().is_ok(), "stopping twice is harmless");

        // One that ignores SIGTERM as well is killed 2s later, and reaped.
        let ready = state.join("ready");
        let stubborn = Command::new("sh")
            .arg("-c")
            .arg(format!(
                "trap '' TERM; : > '{}'; i=0; while [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done",
                ready.display()
            ))
            .spawn()
            .unwrap();
        let pid = stubborn.id();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !ready.exists() {
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(10));
        }
        proxy.child = Some(stubborn);
        let started = Instant::now();
        proxy.stop().unwrap();
        assert!(started.elapsed() >= Duration::from_secs(2));
        assert!(gone_within(pid, Duration::from_secs(1)));
    }

    #[test]
    fn a_healthy_check_keeps_the_socket_fresh_and_moves_the_log_mark() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path();
        let socket_dir = state.join("silicon-caddy-live");
        fs::create_dir(&socket_dir).unwrap();
        let socket = socket_dir.join("admin.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let request = read_request(&stream);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                .unwrap();
            request
        });
        fs::write(state.join("caddy.log"), "started\nroutes loaded\n").unwrap();
        set_old_times(&socket);
        set_old_times(&socket_dir);
        let mut proxy = detached(state, socket.clone());
        proxy.socket_dir = socket_dir.clone();
        proxy.child = Some(Command::new("sleep").arg("30").spawn().unwrap());
        proxy.check().unwrap();
        assert_eq!(server.join().unwrap(), "GET /config/ HTTP/1.1");
        assert!(modified_recently(&socket));
        assert!(modified_recently(&socket_dir));
        assert_eq!(proxy.log_mark, 22);
        assert_eq!(proxy.upkeep_failure, None);
    }

    #[test]
    fn a_caddy_log_past_its_cap_is_copied_aside_and_emptied_in_place() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path();
        let log = state.join("caddy.log");
        fs::write(&log, "one\ntwo\n").unwrap();
        // Caddy's own descriptor, opened the way Silicon opens it for Caddy.
        let mut caddy = OpenOptions::new().append(true).open(&log).unwrap();
        let mut proxy = detached(state, state.join("admin.sock"));
        proxy.log_mark = 8;
        assert!(!proxy.trim_log(100).unwrap());
        assert!(proxy.trim_log(8).unwrap());
        assert_eq!(proxy.log_mark, 0);
        assert_eq!(
            fs::read_to_string(crate::numbered(&log, "1")).unwrap(),
            "one\ntwo\n"
        );
        caddy.write_all(b"three\n").unwrap();
        assert_eq!(fs::read_to_string(&log).unwrap(), "three\n");
        assert!(proxy.trim_log(6).unwrap());
        assert_eq!(
            fs::read_to_string(crate::numbered(&log, "2")).unwrap(),
            "one\ntwo\n"
        );
        assert_eq!(
            fs::read_to_string(crate::numbered(&log, "1")).unwrap(),
            "three\n"
        );
        assert_eq!(fs::metadata(&log).unwrap().len(), 0);
    }

    #[test]
    fn an_earlier_interpreters_caddy_is_signalled_until_it_exits() {
        for ignores_sigterm in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let state = dir.path().join("state");
            fs::create_dir(&state).unwrap();
            let (pid, command) = orphan(dir.path(), &state, ignores_sigterm);
            let socket_dir = dir.path().join("silicon-caddy-old");
            fs::create_dir(&socket_dir).unwrap();
            record(&state, pid, &socket_dir.join("admin.sock"), command.clone());
            let note = reap_orphan(&state).unwrap().unwrap();
            for said in [
                format!("Caddy process {pid} (`{}`, started ", command.join(" ")),
                "by an earlier interpreter, is still running; stopping it".to_owned(),
                format!("sending SIGTERM to Caddy process {pid}"),
                format!(
                    "removed the stale admin socket directory {}",
                    socket_dir.display()
                ),
            ] {
                assert!(note.contains(&said), "{note}");
            }
            if ignores_sigterm {
                assert!(note.contains(&format!("sending SIGKILL to Caddy process {pid}")));
            } else {
                assert!(!note.contains("SIGKILL"), "{note}");
            }
            // "exited after <signal>", or plain "exited" when init had not reaped it yet.
            assert!(
                note.contains(&format!("Caddy process {pid} exited")),
                "{note}"
            );
            assert_ne!(inspect(pid, &command), Recorded::Caddy);
            assert!(!socket_dir.exists());
            assert!(!state.join("owner.json").exists());
            assert_eq!(reap_orphan(&state).unwrap(), None);
        }
    }

    #[test]
    fn an_earlier_caddy_that_accepts_stop_is_not_signalled() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().join("state");
        fs::create_dir(&state).unwrap();
        let (pid, command) = orphan(dir.path(), &state, false);
        let socket_dir = dir.path().join("silicon-caddy-live");
        fs::create_dir(&socket_dir).unwrap();
        let socket = socket_dir.join("admin.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        // Caddy answers /stop, then exits on its own.
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let request = read_request(&stream);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
            request
        });
        record(&state, pid, &socket, command);
        let note = reap_orphan(&state).unwrap().unwrap();
        assert_eq!(server.join().unwrap(), "POST /stop HTTP/1.1");
        for said in [
            format!("asked Caddy to stop through {}: accepted", socket.display()),
            format!("Caddy process {pid} exited"),
        ] {
            assert!(note.contains(&said), "{note}");
        }
        assert!(!note.contains("SIGTERM"), "{note}");
        assert!(!socket_dir.exists());
    }

    #[test]
    fn a_recorded_pid_that_is_not_that_caddy_is_never_signalled() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path();
        let socket = state.join("silicon-caddy-gone/admin.sock");
        let command = caddy_command(&state.join("caddy"), state);
        let mut sleeper = Command::new("sleep").arg("30").spawn().unwrap();
        record(state, sleeper.id(), &socket, command.clone());
        let note = reap_orphan(state).unwrap().unwrap();
        assert!(
            note.contains(&format!(
                "process {}, recorded in {} for Caddy process {}",
                sleeper.id(),
                state.join("owner.json").display(),
                sleeper.id()
            )) && note.contains("is not that Caddy any more (sleep 30), so it was left alone"),
            "{note}"
        );
        assert!(sleeper.try_wait().unwrap().is_none(), "{note}");
        assert!(!state.join("owner.json").exists());
        sleeper.kill().unwrap();
        sleeper.wait().unwrap();

        // Process groups, init and this process are never candidates.
        for pid in [0, 1, std::process::id(), u32::MAX] {
            record(state, pid, &socket, command.clone());
            let note = reap_orphan(state).unwrap().unwrap();
            assert!(
                note.contains(&format!(
                    "pid {pid} can never be a Caddy this interpreter started"
                )),
                "{note}"
            );
        }

        let mut exited = Command::new("true").spawn().unwrap();
        exited.wait().unwrap();
        record(state, exited.id(), &socket, command.clone());
        let note = reap_orphan(state).unwrap().unwrap();
        assert!(note.contains("is no longer running"), "{note}");

        fs::write(state.join("owner.json"), "not json").unwrap();
        let note = reap_orphan(state).unwrap().unwrap();
        assert!(
            note.contains("is not a Caddy owner record") && note.ends_with("it held: not json"),
            "{note}"
        );
        assert!(!state.join("owner.json").exists());
    }

    #[test]
    fn a_recorded_caddy_that_cannot_be_identified_is_kept_until_it_can() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().join("state");
        fs::create_dir(&state).unwrap();
        let path = state.join("owner.json");
        // ps could not run (a full process table), or /proc could not be read.
        let unreadable = |pid: u32, _: &[String]| {
            Recorded::Unknown(format!(
                "could not run `/bin/ps -o command= -p {pid}`: Resource temporarily unavailable (os error 35)"
            ))
        };
        // Alive, and its admin socket refuses connections: nothing says it stopped.
        let mut sleeper = Command::new("sleep").arg("30").spawn().unwrap();
        let pid = sleeper.id();
        let socket_dir = dir.path().join("silicon-caddy-old");
        fs::create_dir(&socket_dir).unwrap();
        let socket = socket_dir.join("admin.sock");
        drop(UnixListener::bind(&socket).unwrap());
        let command = caddy_command(&dir.path().join("caddy"), &state);
        record(&state, pid, &socket, command);
        let error = format!("{:#}", reap_orphan_with(&state, &unreadable).unwrap_err());
        for said in [
            format!("could not ask Caddy to stop through {}: ", socket.display()),
            format!("could not tell whether process {pid} is still Caddy process {pid} (`"),
            format!(
                "Resource temporarily unavailable (os error 35); it was not signalled, and its record and admin socket were kept so the next start checks again (if process {pid} is not that Caddy, remove {})",
                path.display()
            ),
        ] {
            assert!(error.contains(&said), "{error}");
        }
        assert!(sleeper.try_wait().unwrap().is_none(), "{error}");
        assert!(path.exists() && socket.exists());
        sleeper.kill().unwrap();
        sleeper.wait().unwrap();

        // Once that Caddy accepts /stop through its socket and exits, the start goes on.
        let (pid, command) = orphan(dir.path(), &state, false);
        fs::remove_file(&socket).unwrap();
        let listener = UnixListener::bind(&socket).unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            read_request(&stream);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        });
        record(&state, pid, &socket, command);
        let note = reap_orphan_with(&state, &unreadable).unwrap().unwrap();
        server.join().unwrap();
        assert!(
            note.contains(&format!(
                "asked Caddy to stop through {}: accepted",
                socket.display()
            )) && note.contains(" exited after it accepted /stop (whether the pid was still that Caddy could not be checked: could not run `/bin/ps"),
            "{note}"
        );
        assert!(!path.exists() && !socket_dir.exists(), "{note}");
    }

    #[test]
    fn a_caddy_that_accepted_stop_is_waited_for_whatever_its_command_line_shows() {
        let dir = tempfile::tempdir().unwrap();
        let state = dir.path().join("state");
        fs::create_dir(&state).unwrap();
        let (pid, command) = orphan(dir.path(), &state, false);
        let socket_dir = dir.path().join("silicon-caddy-live");
        fs::create_dir(&socket_dir).unwrap();
        let socket = socket_dir.join("admin.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        // Caddy answers /stop, then takes a moment to close its listeners and exit.
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            read_request(&stream);
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
            thread::sleep(Duration::from_millis(300));
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
        });
        record(&state, pid, &socket, command);
        // ps printed the command line differently from how it was recorded.
        let escaped = |_: u32, _: &[String]| Recorded::Other("/bin/sh /josM-CM-)/caddy run".into());
        let note = reap_orphan_with(&state, &escaped).unwrap().unwrap();
        // The new Caddy starts only once the old one released the port.
        assert_ne!(unsafe { libc::kill(pid as libc::pid_t, 0) }, 0, "{note}");
        server.join().unwrap();
        assert!(
            note.contains(&format!(
                "process {pid} exited after the Caddy at {} accepted /stop",
                socket.display()
            )) && note.contains("its command line was /bin/sh /josM-CM-)/caddy run"),
            "{note}"
        );
        assert!(!note.contains("sending"), "{note}");
        assert!(!socket_dir.exists() && !state.join("owner.json").exists());
    }

    #[test]
    fn a_caddy_under_a_non_ascii_path_is_recognised_and_stopped() {
        let dir = tempfile::tempdir().unwrap();
        // A launchd agent runs without a locale, where macOS ps escapes non-ASCII bytes.
        let home = dir.path().join("josé");
        fs::create_dir(&home).unwrap();
        let state = home.join("state");
        fs::create_dir(&state).unwrap();
        // orphan() waits until inspect() recognises it.
        let (pid, command) = orphan(&home, &state, false);
        let socket_dir = dir.path().join("silicon-caddy-old");
        fs::create_dir(&socket_dir).unwrap();
        record(&state, pid, &socket_dir.join("admin.sock"), command.clone());
        let note = reap_orphan(&state).unwrap().unwrap();
        for said in [
            "by an earlier interpreter, is still running; stopping it".to_owned(),
            format!("sending SIGTERM to Caddy process {pid}"),
        ] {
            assert!(note.contains(&said), "{note}");
        }
        assert_ne!(inspect(pid, &command), Recorded::Caddy);
    }

    #[test]
    fn command_lines_match_only_this_state_directorys_caddy() {
        let command: Vec<String> = [
            "/opt/bin/caddy",
            "run",
            "--config",
            "-",
            "--pidfile",
            "/a b/caddy/caddy.pid",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect();
        for line in [
            "/opt/bin/caddy run --config - --pidfile /a b/caddy/caddy.pid",
            "caddy run --config - --pidfile /a b/caddy/caddy.pid",
            "/bin/sh /opt/bin/caddy run --config - --pidfile /a b/caddy/caddy.pid",
        ] {
            assert!(runs(line, &command), "{line}");
        }
        for line in [
            "/opt/bin/caddy run --config - --pidfile /other/caddy/caddy.pid",
            "/opt/bin/caddy run --config -",
            "/opt/bin/notcaddy run --config - --pidfile /a b/caddy/caddy.pid",
            "sleep 30",
            "",
        ] {
            assert!(!runs(line, &command), "{line}");
        }
        assert!(!runs("caddy", &["caddy".to_owned()]));
        assert!(!runs("", &[]));
    }

    #[test]
    fn mount_options_come_from_the_mount_that_holds_caddy() {
        let mountinfo = "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n\
            40 22 8:2 / /home rw,nosuid,nodev,relatime shared:2 - ext4 /dev/sda2 rw\n\
            41 40 0:5 / /home/a\\040b rw,relatime - tmpfs tmpfs rw\n\
            42 22 0:6 / /opt rw - tmpfs tmpfs rw\n\
            43 22 0:7 / /opt ro,nosuid - tmpfs tmpfs rw\n";
        for (path, point, options) in [
            (
                "/home/u/.local/share/silicon/bin/caddy",
                "/home",
                "rw,nosuid,nodev,relatime",
            ),
            ("/usr/bin/caddy", "/", "rw,relatime"),
            ("/homework/caddy", "/", "rw,relatime"),
            ("/home/a b/caddy", "/home/a b", "rw,relatime"),
            ("/opt/caddy", "/opt", "ro,nosuid"),
        ] {
            assert_eq!(
                mount_of(Path::new(path), mountinfo),
                Some((point.to_owned(), options.to_owned())),
                "{path}"
            );
        }
        assert_eq!(mount_of(Path::new("/x"), "garbage\n"), None);
        assert_eq!(unescape(r"a\011b\134c\\d\08"), b"a\tb\\c\\\\d\\08");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn port_facts_name_what_the_kernel_decides_with() {
        let facts = port_facts(OsStr::new("sh"), 80);
        for said in [
            "What this Linux system reports about binding port 80:",
            "/proc/sys/net/ipv4/ip_unprivileged_port_start",
            "getcap ",
            "/proc/self/status NoNewPrivs:",
        ] {
            assert!(facts.contains(said), "{facts}");
        }
        assert!(port_facts(OsStr::new("no-such-caddy-anywhere"), 80)
            .contains("the Caddy executable `no-such-caddy-anywhere` was not found on PATH"));
    }

    #[test]
    #[ignore = "requires Caddy; set SILICON_CADDY and run with --ignored"]
    fn real_caddy_routes_reload_rollback_and_child_cleanup() {
        let backend = TcpListener::bind("127.0.0.1:0").unwrap();
        let backend_port = backend.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            for _ in 0..4 {
                let (mut stream, _) = backend.accept().unwrap();
                let mut bytes = [0u8; 4096];
                let count = stream.read(&mut bytes).unwrap();
                let request = String::from_utf8_lossy(&bytes[..count]);
                let host = request
                    .lines()
                    .find(|line| line.to_ascii_lowercase().starts_with("host:"))
                    .unwrap();
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{host}",
                    host.len()
                )
                .unwrap();
            }
        });
        let reserved = TcpListener::bind("127.0.0.1:0").unwrap();
        let proxy_port = reserved.local_addr().unwrap().port();
        drop(reserved);
        let dir = tempfile::tempdir().unwrap();
        let mut proxy = Proxy::start_on(
            dir.path(),
            backend_port,
            &["a.org.localhost".into()],
            proxy_port,
            &binary(),
        )
        .unwrap();
        let pid = proxy.child.as_ref().unwrap().id();
        let socket = proxy.socket.clone();
        assert_eq!(
            fs::metadata(&socket).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(&proxy.socket_dir)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        let owner: Owner = serde_json::from_slice(&fs::read(proxy.owner_path()).unwrap()).unwrap();
        assert_eq!(owner.pid, pid);
        assert_eq!(inspect(pid, &owner.command), Recorded::Caddy);
        // Health checks answer without adding to caddy.log, the one that probes the HTTP
        // port included.
        let logged = proxy.log_len();
        proxy.unprobed = PROBE_EVERY - 2;
        for _ in 0..3 {
            proxy.check().unwrap();
        }
        assert_eq!(proxy.unprobed, 1);
        assert_eq!(proxy.log_len(), logged);
        assert!(fetch(proxy_port, "silicon.localhost").contains("200 OK"));
        assert!(fetch(proxy_port, "a.org.localhost").contains("Host: a.org.localhost"));
        assert!(fetch(proxy_port, "unknown.org.localhost").contains("404"));
        proxy.update(&["b.org.localhost".into()]).unwrap();
        assert_eq!(proxy.child.as_ref().unwrap().id(), pid);
        assert!(fetch(proxy_port, "a.org.localhost").contains("404"));
        assert!(fetch(proxy_port, "b.org.localhost").contains("200 OK"));
        let previous = proxy.config.clone();
        let occupied = TcpListener::bind("127.0.0.1:0").unwrap();
        let mut invalid = previous.clone();
        invalid["apps"]["http"]["servers"]["silicon"]["listen"] =
            json!([occupied.local_addr().unwrap().to_string()]);
        assert!(proxy.replace_config(invalid).is_err());
        assert_eq!(proxy.config, previous);
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(proxy.state.join("config.json")).unwrap())
                .unwrap(),
            previous
        );
        assert!(fetch(proxy_port, "b.org.localhost").contains("200 OK"));
        proxy.check().unwrap();
        server.join().unwrap();
        let owner = proxy.owner_path();
        drop(proxy);
        assert!(!socket.exists());
        assert!(!owner.exists());
        assert!(TcpStream::connect(("127.0.0.1", proxy_port)).is_err());
    }

    #[test]
    #[ignore = "requires Caddy; set SILICON_CADDY and run with --ignored"]
    fn real_caddy_left_running_is_stopped_by_the_next_start() {
        let reserved = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = reserved.local_addr().unwrap().port();
        drop(reserved);
        let dir = tempfile::tempdir().unwrap();
        let first = Proxy::start_on(dir.path(), 1, &[], port, &binary()).unwrap();
        let pid = first.child.as_ref().unwrap().id();
        let old_socket_dir = first.socket_dir.clone();
        let owner: Owner = serde_json::from_slice(&fs::read(first.owner_path()).unwrap()).unwrap();
        // The interpreter dies without its Drop: this Caddy keeps running and keeps the port.
        std::mem::forget(first);
        let mut second = Proxy::start_on(dir.path(), 1, &[], port, &binary()).unwrap();
        assert_ne!(second.child.as_ref().unwrap().id(), pid);
        assert_ne!(inspect(pid, &owner.command), Recorded::Caddy);
        assert!(!old_socket_dir.exists());
        second.check().unwrap();
        second.verify_listener().unwrap();
    }

    /// macOS hands every connection to the first of two SO_REUSEPORT listeners, so the probe
    /// meets the unknown Caddy on its first request; Linux balances them, so it meets it on
    /// one of its requests.
    #[test]
    #[ignore = "requires Caddy; set SILICON_CADDY and run with --ignored"]
    fn real_caddy_beside_an_unknown_caddy_reports_what_answers() {
        let reserved = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = reserved.local_addr().unwrap().port();
        drop(reserved);
        let first_dir = tempfile::tempdir().unwrap();
        let first = Proxy::start_on(first_dir.path(), 1, &[], port, &binary()).unwrap();
        let pid = first.child.as_ref().unwrap().id();
        let first_socket_dir = first.socket_dir.clone();
        // Left running, and nothing in the second state directory records it.
        std::mem::forget(first);
        let second_dir = tempfile::tempdir().unwrap();
        let error = Proxy::start_on(second_dir.path(), 1, &[], port, &binary())
            .err()
            .map(|error| format!("{error:#}"));
        unsafe { libc::kill(pid as libc::pid_t, libc::SIGKILL) };
        fs::remove_dir_all(first_socket_dir).unwrap();
        let error = error.unwrap();
        let first = if cfg!(target_os = "macos") { "1" } else { "" };
        assert!(
            error.contains(&format!(
                "another server answers HTTP port {port}: request {first}"
            )) && error.contains(&format!(" of {PROBES} for Host probe-"))
                && error.contains("Unknown Silicon host"),
            "{error}"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    #[ignore = "requires Caddy, free port 80, and SILICON_TEST_LAN_IP; run with --ignored"]
    fn real_caddy_port_80_only_forwards_loopback_peers() {
        let lan_ip = std::env::var("SILICON_TEST_LAN_IP")
            .expect("set SILICON_TEST_LAN_IP to this machine's non-loopback IPv4 address");
        assert!(!lan_ip.parse::<std::net::IpAddr>().unwrap().is_loopback());
        let backend = TcpListener::bind("127.0.0.1:0").unwrap();
        let backend_port = backend.local_addr().unwrap().port();
        backend.set_nonblocking(true).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let proxy = Proxy::start(dir.path(), backend_port, &["a.org.localhost".into()]).unwrap();
        for host in [
            "silicon.localhost",
            "a.org.localhost",
            "unknown.org.localhost",
        ] {
            for headers in ["", "X-Forwarded-For: 127.0.0.1\r\nX-Real-IP: ::1\r\n"] {
                let response = fetch_from(&lan_ip, 80, host, headers);
                assert!(response.starts_with("HTTP/1.1 404"), "{response}");
            }
        }
        assert_eq!(
            backend.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
        backend.set_nonblocking(false).unwrap();
        let expected = if proxy.ipv6 { 2 } else { 1 };
        let server = thread::spawn(move || {
            for _ in 0..expected {
                let (mut stream, _) = backend.accept().unwrap();
                let mut bytes = [0u8; 4096];
                assert!(stream.read(&mut bytes).unwrap() > 0);
                stream
                    .write_all(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK",
                    )
                    .unwrap();
            }
        });
        assert!(fetch(80, "silicon.localhost").starts_with("HTTP/1.1 200"));
        if proxy.ipv6 {
            assert!(fetch_from("::1", 80, "a.org.localhost", "").starts_with("HTTP/1.1 200"));
        }
        server.join().unwrap();
        drop(proxy);
        assert!(TcpStream::connect(("127.0.0.1", 80)).is_err());
    }
}
