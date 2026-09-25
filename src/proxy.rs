//! An owned, isolated Caddy process. It never contacts the machine's default admin API.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::TcpListener;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;

pub struct Proxy {
    child: Option<Child>,
    socket_dir: PathBuf,
    socket: PathBuf,
    state: PathBuf,
    config: Value,
    interpreter_port: u16,
    http_port: u16,
    ipv6: bool,
    /// caddy.log length after the last operation Caddy completed; later lines explain later failures.
    log_mark: u64,
    /// Why this proxy stopped its Caddy, for every later call that finds it stopped.
    stopped: Option<String>,
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
        };
        let initial = proxy.base_config();
        let log_path = proxy.log_path();
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(&log_path)
            .with_context(|| format!("open Caddy log {}", log_path.display()))?;
        proxy.log_mark = proxy.log_len();
        let command = crate::failure::argv(binary, &["run", "--config", "-"]);
        let child = Command::new(binary)
            .args(["run", "--config", "-"])
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
        proxy.child = Some(child);
        let mut input = proxy.child.as_mut().unwrap().stdin.take().unwrap();
        let sent = input.write_all(&serde_json::to_vec(&initial)?);
        drop(input);
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
        proxy.replace_config(config).with_context(|| format!(
            "Caddy could not serve the Silicon routes on HTTP port {http_port}; if it reports a bind error, check whether another server uses that port and, on Linux, grant the Caddy executable permission to bind port 80"))?;
        Ok(proxy)
    }

    /// Caddy's /load endpoint keeps serving the old configuration if a reload fails.
    /// Persist only an accepted configuration; roll back if the disk commit fails.
    pub fn update(&mut self, hosts: &[String]) -> Result<()> {
        self.ensure_running()?;
        let config = self.configuration(hosts)?;
        if config != self.config {
            self.replace_config(config)?;
        }
        Ok(())
    }

    fn base_config(&self) -> Value {
        json!({
            "admin": {"listen": format!("unix/{}|0600", self.socket.display()), "config": {"persist": false}},
            "storage": {"module": "file_system", "root": self.state.join("storage")}
        })
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
        let mut config = self.base_config();
        config["apps"] = json!({"http": {
            "grace_period": "1s",
            "servers": {"silicon": {
                "listen": listen,
                "automatic_https": {"disable": true},
                "routes": [
                    {"match": [{"host": hosts, "remote_ip": {"ranges": ["127.0.0.1/32", "::1/128"]}}], "handle": [{"handler": "reverse_proxy",
                        "upstreams": [{"dial": format!("127.0.0.1:{}", self.interpreter_port)}]}], "terminal": true},
                    {"handle": [{"handler": "static_response", "status_code": 404, "body": "Unknown Silicon host\n"}]}
                ]
            }}
        }});
        Ok(config)
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
                "the owned Caddy proxy is stopped, so Silicon hosts are not routed; restart the interpreter with `silicon stop`, then `silicon serve`. It stopped because: {}",
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
        let call = format!(
            "Caddy admin API `{method} {path}` on {}",
            self.socket.display()
        );
        let fail = |problem: String| anyhow!("{call} {problem}\n{}", self.log_since(mark));
        let mut stream = UnixStream::connect(&self.socket)
            .map_err(|error| fail(format!("could not connect: {error}")))?;
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

    /// Stops only this child. Config and logs are retained for inspection.
    pub fn stop(&mut self) -> Result<()> {
        if self.child.is_none() {
            return Ok(());
        }
        // Asking first lets Caddy close listeners cleanly; the kill below covers a refusal.
        let asked = self.request("POST", "/stop", &[], Duration::from_secs(2));
        let mut child = self.child.take().unwrap();
        let pid = child.id();
        let deadline = Instant::now() + Duration::from_secs(2);
        while child
            .try_wait()
            .with_context(|| format!("check whether Caddy process {pid} stopped"))?
            .is_none()
        {
            if Instant::now() >= deadline {
                child.kill().with_context(|| match &asked {
                    Ok(()) => format!(
                        "kill Caddy process {pid}, still running 2s after it accepted /stop"
                    ),
                    Err(error) => format!("kill Caddy process {pid} after /stop failed: {error:#}"),
                })?;
                child
                    .wait()
                    .with_context(|| format!("wait for killed Caddy process {pid}"))?;
                break;
            }
            thread::sleep(Duration::from_millis(20));
        }
        Ok(())
    }
}

impl Drop for Proxy {
    fn drop(&mut self) {
        // Drop cannot return an error; the daemon's stderr is its daemon.log.
        if let Err(error) = self.stop() {
            eprintln!("could not stop the owned Caddy proxy: {error:#}");
        }
        // Best effort: an empty private directory left in /tmp changes nothing.
        let _ = fs::remove_dir_all(&self.socket_dir);
    }
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
    use std::net::TcpStream;
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
        let caddy = dir.path().join("caddy");
        fs::write(
            &caddy,
            "#!/bin/sh\ncat >/dev/null\necho 'using config from stdin'\necho 'Error: loading initial config: listen tcp :80: bind: permission denied' >&2\nexit 3\n",
        )
        .unwrap();
        fs::set_permissions(&caddy, fs::Permissions::from_mode(0o700)).unwrap();
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

        let missing = dir.path().join("no-caddy");
        let error = Proxy::start_on(&state, 1, &[], 1, missing.as_os_str())
            .err()
            .unwrap();
        let error = format!("{error:#}");
        assert!(
            error.contains("could not run `no-caddy run --config -`"),
            "{error}"
        );
        assert!(error.contains("No such file or directory"), "{error}");
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
                let mut reader = BufReader::new(stream.try_clone().unwrap());
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
                OpenOptions::new()
                    .append(true)
                    .open(&log)
                    .unwrap()
                    .write_all(b"{\"level\":\"error\",\"logger\":\"admin.api\",\"msg\":\"request error\"}\n")
                    .unwrap();
                stream.write_all(answer.as_bytes()).unwrap();
            }
        });
        let mut proxy = Proxy {
            child: None,
            socket_dir: state.join("unused"),
            socket,
            state,
            config: json!({"previous": true}),
            interpreter_port: 1,
            http_port: 1,
            ipv6: false,
            log_mark: 0,
            stopped: None,
        };
        let error = format!(
            "{:#}",
            proxy.replace_config(json!({"next": true})).unwrap_err()
        );
        server.join().unwrap();
        for said in [
            "Caddy reload failed; previous routes are active",
            "Caddy admin API `POST /load` on ",
            "answered HTTP/1.1 400 Bad Request:\n{\"error\":\"loading new config: http app module: start: listening on 127.0.0.1:80: bind: address already in use\"}",
            "{\"level\":\"error\",\"logger\":\"admin.api\",\"msg\":\"request error\"}",
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
        let mut proxy = Proxy {
            child: None,
            socket_dir: dir.path().join("unused"),
            socket: socket.clone(),
            state: dir.path().to_path_buf(),
            config: json!({"previous": true}),
            interpreter_port: 1,
            http_port: 1,
            ipv6: false,
            log_mark: 0,
            stopped: None,
        };
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
        // A later call names the stop and the failure behind it, not just "stopped".
        let later = format!("{:#}", proxy.update(&[]).unwrap_err());
        for said in [
            "the owned Caddy proxy is stopped",
            "`silicon stop`, then `silicon serve`",
            "It stopped because: could not restore previous routes; stopped the owned Caddy proxy",
            refused.as_str(),
        ] {
            assert!(later.contains(said), "{later}");
        }
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
        server.join().unwrap();
        drop(proxy);
        assert!(!socket.exists());
        assert!(TcpStream::connect(("127.0.0.1", proxy_port)).is_err());
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
