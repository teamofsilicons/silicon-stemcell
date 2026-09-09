//! An owned, isolated Caddy process. It never contacts the machine's default admin API.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
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
}

impl Proxy {
    pub fn start(state_dir: &Path, interpreter_port: u16, hosts: &[String]) -> Result<Self> {
        Self::start_on(state_dir, interpreter_port, hosts, 80)
    }

    fn start_on(
        state_dir: &Path,
        interpreter_port: u16,
        hosts: &[String],
        http_port: u16,
    ) -> Result<Self> {
        if interpreter_port == 0 || http_port == 0 {
            bail!("proxy ports must be nonzero");
        }
        validate_hosts(hosts)?;
        let state = state_dir.join("caddy");
        crate::state::private_dir(&state)?;
        let state = state.canonicalize()?;
        // Unix socket paths are limited to 104 bytes on macOS; user-selected
        // state directories can be much longer. This private, unique directory
        // contains only the short-lived admin socket and is removed on drop.
        let socket_dir = PathBuf::from("/tmp").join(format!("silicon-caddy-{}", Uuid::new_v4()));
        fs::DirBuilder::new().mode(0o700).create(&socket_dir)?;
        let socket = socket_dir.join("admin.sock");
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
        };
        let initial = proxy.base_config();
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(proxy.state.join("caddy.log"))?;
        let binary = std::env::var_os("SILICON_CADDY").unwrap_or_else(|| "caddy".into());
        let child = Command::new(&binary).args(["run", "--config", "-"])
            .stdin(Stdio::piped()).stdout(log.try_clone()?).stderr(log)
            .env("XDG_DATA_HOME", proxy.state.join("data"))
            .env("XDG_CONFIG_HOME", proxy.state.join("config"))
            .spawn().with_context(|| format!("could not start Caddy ({:?}); install Caddy or set SILICON_CADDY to its executable", binary))?;
        proxy.child = Some(child);
        let mut input = proxy.child.as_mut().unwrap().stdin.take().unwrap();
        input.write_all(&serde_json::to_vec(&initial)?)?;
        drop(input);
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
                Err(error) => {
                    return Err(error).context("connect to the private Caddy admin socket")
                }
            }
            if Instant::now() >= deadline {
                bail!(
                    "Caddy did not open its private admin socket; inspect {}",
                    proxy.state.join("caddy.log").display()
                );
            }
            thread::sleep(Duration::from_millis(25));
        }
        proxy.config = initial;
        let config = proxy.configuration(hosts)?;
        proxy.replace_config(config).with_context(|| format!(
            "Caddy could not bind HTTP port {http_port}; check whether another server uses that port and, on Linux, grant the Caddy executable permission to bind port 80. Log: {}",
            proxy.state.join("caddy.log").display()))?;
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
        if let Err(error) = crate::state::write_json(&self.state.join("config.json"), &next) {
            self.rollback()
                .with_context(|| format!("Caddy config could not be saved: {error:#}"))?;
            crate::state::write_json(&self.state.join("config.json"), &self.config)
                .with_context(|| format!("previous Caddy routes are active, but saved config could not be restored after {error:#}"))?;
            return Err(error).context("Caddy config could not be saved; restored previous routes");
        }
        self.config = next;
        Ok(())
    }

    fn rollback(&mut self) -> Result<()> {
        if let Err(error) = self.request(
            "POST",
            "/load",
            &serde_json::to_vec(&self.config)?,
            Duration::from_secs(10),
        ) {
            let _ = self.stop();
            return Err(error)
                .context("could not restore previous routes; stopped the owned Caddy proxy");
        }
        Ok(())
    }

    fn ensure_running(&mut self) -> Result<()> {
        let child = self
            .child
            .as_mut()
            .ok_or_else(|| anyhow!("Caddy proxy is stopped"))?;
        if let Some(status) = child.try_wait()? {
            let tail = fs::read_to_string(self.state.join("caddy.log"))
                .unwrap_or_default()
                .lines()
                .rev()
                .take(5)
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect::<Vec<_>>()
                .join("\n");
            bail!(
                "Caddy exited with {status}; inspect {}\n{tail}",
                self.state.join("caddy.log").display()
            );
        }
        Ok(())
    }

    fn request(&self, method: &str, path: &str, body: &[u8], timeout: Duration) -> Result<()> {
        let mut stream =
            UnixStream::connect(&self.socket).context("connect to private Caddy admin socket")?;
        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;
        write!(stream, "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len())?;
        stream.write_all(body)?;
        let mut response = String::new();
        stream
            .take(1024 * 1024)
            .read_to_string(&mut response)
            .context("read Caddy admin response")?;
        let status = response
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|code| code.parse::<u16>().ok());
        if !status.is_some_and(|code| (200..300).contains(&code)) {
            bail!(
                "Caddy {path} failed: {}",
                response
                    .split_once("\r\n\r\n")
                    .map(|(_, body)| body)
                    .unwrap_or(&response)
                    .trim()
            );
        }
        Ok(())
    }

    /// Stops only this child. Config and logs are retained for inspection.
    pub fn stop(&mut self) -> Result<()> {
        if self.child.is_none() {
            return Ok(());
        }
        let _ = self.request("POST", "/stop", &[], Duration::from_secs(2));
        let mut child = self.child.take().unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while child.try_wait()?.is_none() {
            if Instant::now() >= deadline {
                child.kill().context("stop Caddy child")?;
                child.wait()?;
                break;
            }
            thread::sleep(Duration::from_millis(20));
        }
        Ok(())
    }
}

impl Drop for Proxy {
    fn drop(&mut self) {
        let _ = self.stop();
        let _ = fs::remove_dir_all(&self.socket_dir);
    }
}

fn validate_hosts(hosts: &[String]) -> Result<Vec<String>> {
    let mut allowed = BTreeSet::from(["silicon.localhost".to_owned()]);
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
            bail!(
                "invalid Silicon proxy hostname {host:?}; expected a DNS name ending in .localhost"
            );
        }
        allowed.insert(host);
    }
    Ok(allowed.into_iter().collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpStream;
    use std::os::unix::fs::PermissionsExt;

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
