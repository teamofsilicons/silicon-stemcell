//! Boot autostart and crash restart. The interpreter must come back by itself after a
//! reboot, a logout or a crash, so it runs under the platform's own supervisor:
//! - macOS: a LaunchAgent, loaded by launchd at login.
//! - Linux with a systemd user manager: a user unit, started at boot through linger.
//! - Linux without one: cron runs `silicon service ensure`, which starts `silicon service
//!   run`, a small supervisor that restarts `silicon serve` after a failure.
//! - Windows (WSL): the Windows installer's logon task supervises the distribution.
//!
//! Every definition runs `<managed prefix>/bin/silicon serve`, the symlink through
//! `lib/silicon/current`, so updates never rewrite it, with `SILICON_SERVICE` set so serve
//! waits for daemon.lock and writes its own output to daemon.log. A clean stop exits 0 and
//! is never restarted; any other exit is.
use crate::{failure, server, state};
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Output, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant, SystemTime};

const VERSION: &str = env!("CARGO_PKG_VERSION");
/// launchd label for the default interpreter directory; another directory adds its hash.
const LABEL: &str = "com.teamofsilicons.silicon";
/// systemd unit stem, likewise.
const UNIT: &str = "silicon";
/// Marks the crontab lines Silicon owns; the directory hash follows it.
const TAG: &str = "# silicon-service";
/// How every systemd unit Silicon writes begins, so one is told from a person's own.
const UNIT_MARK: &str = "# Written by `silicon service install`";
/// Under %LOCALAPPDATA%: touching it ends the Windows helper's wait before a restart.
const WAKE: &str = r"Silicon\service-wake";
/// launchctl, systemctl, loginctl, crontab and PowerShell answer quickly or not at all.
const TOOL_LIMIT: Duration = Duration::from_secs(60);
/// Stopping waits for the interpreter's orderly shutdown, which the definitions allow 90 s.
const STOP_LIMIT: Duration = Duration::from_secs(120);
const LOGIN_SHELL_LIMIT: Duration = Duration::from_secs(10);
const SERVICE_LOG_CAP: u64 = 10 * 1024 * 1024;
const SERVICE_LOG_KEEP: usize = 3;
/// What `service.env` captures besides these exact names.
const CAPTURED: &[&str] = &["PATH", "LANG", "LC_ALL", "LC_CTYPE", "SHELL"];
const CAPTURED_PREFIXES: &[&str] = &[
    "SILICON_",
    "OMNI_",
    "HONEYCOMB_",
    "IAM_",
    "TING_",
    "SPACE_STATION_",
    "ANTHROPIC_",
    "OPENAI_",
    "CLAUDE_",
    "CODEX_",
    "GEMINI_",
];
/// The supervisor sets these itself; a stale copy in service.env must never win.
const NEVER_LOADED: &[&str] = &["HOME", "SILICON_SERVICE", "SILICON_INTERPRETER_HOME"];

#[derive(clap::Subcommand, Debug, Clone)]
pub enum Action {
    /// Start the interpreter at login or boot and restart it after a crash (launchd, systemd or cron).
    Install,
    /// Stop the interpreter and turn autostart off until `silicon service install`.
    Uninstall,
    /// Show how the interpreter is supervised, its state and the logs to read.
    Status,
    /// Restart the interpreter now through its supervisor.
    Restart,
    /// Supervise `silicon serve` in the foreground (started by `silicon service ensure`).
    #[command(hide = true)]
    Run,
    /// Start the supervisor unless the interpreter runs or was stopped this boot (run by cron).
    #[command(hide = true)]
    Ensure,
}

/// `service.json` in the interpreter directory. Unknown fields are ignored, so an older
/// release still reads what a newer one wrote.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
struct Record {
    #[serde(default)]
    mechanism: String,
    #[serde(default)]
    definition: Option<PathBuf>,
    #[serde(default)]
    label: String,
    #[serde(default)]
    installed_by: String,
    #[serde(default)]
    declined: bool,
    #[serde(default)]
    linger_enabled_by_silicon: bool,
}

/// An installed, not declined, service.
pub struct Installed {
    record: Record,
}

impl Installed {
    /// Start the interpreter through its supervisor now. Returns once the supervisor took
    /// the request; the interpreter itself may still be starting.
    pub fn start(&self) -> Result<()> {
        let tools = System;
        let host = Host::real(&tools)?;
        start(&host, &self.record)
    }

    /// `launchd`, `systemd`, `run` (cron and the Silicon supervisor) or `windows-task`.
    pub fn mechanism(&self) -> &str {
        &self.record.mechanism
    }
}

/// The service recorded for this interpreter directory, unless there is none or it was
/// turned off with `silicon service uninstall`.
pub fn installed() -> Result<Option<Installed>> {
    Ok(load(&server::directory())?
        .filter(|record| !record.declined && !record.mechanism.is_empty())
        .map(|record| Installed { record }))
}

/// Install the service the first time `silicon connect` runs, so the interpreter comes
/// back after a reboot without anyone asking for it. Nothing happens when it is already
/// installed, was turned off, `SILICON_NO_SERVICE=1`, this silicon is not a managed
/// installation (a cargo build), SILICON_INTERPRETER_HOME names another directory (a
/// test installation; `silicon service install` still installs one for it on request), or
/// daemon.json says the user's own process manager runs the interpreter
/// (SILICON_SERVICE=external). `Some` explains what was done or what the user must do.
pub fn ensure_for_connect() -> Result<Option<String>> {
    if std::env::var("SILICON_NO_SERVICE").as_deref() == Ok("1") {
        return Ok(None);
    }
    let tools = System;
    ensure_for_connect_with(&Host::real(&tools)?)
}

/// Call right before `silicon connect` starts the interpreter through its service
/// ([`Installed::start`]): 5.0.2 handed the connecting terminal's environment to the serve it
/// started, so a rotated key or a changed setting took effect at the next stop and connect.
/// This takes the terminal's variables into service.env the same way (it is what the
/// supervised serve loads, now and at every boot): variables only the file has stay, the
/// earlier file is kept as service.env.previous, and what changed is named, never valued.
/// No notes when there is no service, it was turned off, or nothing changed.
pub fn refresh_environment() -> Result<Vec<String>> {
    refresh_environment_in(&server::directory(), &process_vars())
}

/// This process's environment, without the variables that are not Unicode.
fn process_vars() -> Vec<(String, String)> {
    std::env::vars_os()
        .filter_map(|(name, value)| Some((name.into_string().ok()?, value.into_string().ok()?)))
        .collect()
}

fn refresh_environment_in(dir: &Path, vars: &[(String, String)]) -> Result<Vec<String>> {
    match load(dir)? {
        Some(record) if !record.declined && !record.mechanism.is_empty() => {
            write_environment(dir, vars)
        }
        _ => Ok(Vec::new()),
    }
}

/// Warnings for the connection `silicon connect` is about to make, when a LaunchAgent will
/// run the interpreter: macOS keeps privacy-protected folders from programs launchd starts,
/// so a YAML or home there fails to load under the agent. Call it after
/// [`ensure_for_connect`]; the install note only covers connections saved before.
pub fn connect_warnings(yaml: &Path, home: &Path) -> Vec<String> {
    if !cfg!(target_os = "macos") {
        return Vec::new();
    }
    let Some(user_home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return Vec::new();
    };
    connect_warnings_in(&server::directory(), &user_home, yaml, home)
}

fn connect_warnings_in(dir: &Path, user_home: &Path, yaml: &Path, home: &Path) -> Vec<String> {
    let launchd = matches!(
        load(dir),
        Ok(Some(record)) if !record.declined && record.mechanism == "launchd"
    );
    if !launchd {
        return Vec::new();
    }
    let protected = protected_folders(user_home);
    [("yaml", yaml), ("home", home)]
        .into_iter()
        .filter_map(|(key, path)| {
            privacy_warning(&protected, "this connection", key, &path.to_string_lossy())
        })
        .collect()
}

/// `silicon serve` under a supervisor calls this first, before it starts any thread:
/// supervisors hand their jobs a minimal environment (launchd: PATH=/usr/bin:/bin:/usr/sbin:/sbin,
/// no LANG; cron: PATH=/usr/bin:/bin), so setup scripts, DNA and app CLIs that work in a
/// terminal would fail after the first unattended reboot. This loads `service.env` (never
/// HOME), then appends the PATH entries the login shell adds. Returns warnings to log.
pub fn prepare_environment() -> Vec<String> {
    let mut warnings = Vec::new();
    let dir = server::directory();
    let file = environment_file(&dir);
    let loaded = match fs::read_to_string(&file) {
        Ok(text) => parse_environment(&dir, &text, &mut warnings),
        Err(error) if error.kind() == ErrorKind::NotFound => Vec::new(),
        Err(error) => {
            warnings.push(format!(
                "could not read {}, so the interpreter runs with the supervisor's environment: {error}",
                file.display()
            ));
            Vec::new()
        }
    };
    let shell = loaded
        .iter()
        .find(|(name, _)| name == "SHELL")
        .map(|(_, value)| value.clone())
        .or_else(|| std::env::var("SHELL").ok());
    let login = match shell.as_deref() {
        Some(shell) if Path::new(shell).is_absolute() => match login_path(Path::new(shell)) {
            Ok(path) => Some(path),
            Err(error) => {
                warnings.push(format!(
                    "the login shell's PATH is not added to the interpreter's: {error:#}"
                ));
                None
            }
        },
        _ => None,
    };
    let current = std::env::var("PATH").unwrap_or_default();
    for (name, value) in plan_environment(&loaded, &current, login.as_deref()) {
        std::env::set_var(name, value);
    }
    // The bundle's own tools go first again, as at every start.
    if let Err(error) = crate::init_bundle_path() {
        warnings.push(format!("{error:#}"));
    }
    warnings
}

/// `silicon stop` under cron and the Silicon supervisor: `silicon service ensure` leaves the
/// interpreter stopped until the next boot or `silicon connect`. Write it before sending
/// `shutdown` (and [`clear_stopped`] if that fails): written after, a cron `ensure` between
/// the interpreter's exit and the marker would start it again.
pub fn note_stopped() -> Result<()> {
    let dir = server::directory();
    state::write_json(
        &dir.join("stopped"),
        &Stopped {
            boot_id: boot_id(),
            at: Utc::now(),
        },
    )
}

/// `silicon connect`, `silicon service install` and a service start clear the stop.
pub fn clear_stopped() -> Result<()> {
    clear_stopped_in(&server::directory())
}

/// `silicon service ACTION`: human text, or JSON with `--json`.
pub fn cli(action: Action, json: bool) -> Result<()> {
    let tools = System;
    if matches!(action, Action::Ensure) {
        // Before Host::real, which would create the directory again.
        let dir = server::directory();
        if vanished_directory(&dir) {
            return print_ensured(&forget_vanished(&tools, &dir)?, json);
        }
    }
    let host = Host::real(&tools)?;
    match action {
        Action::Install => print_report(&install(&host)?, json),
        Action::Uninstall => print_report(&uninstall(&host)?, json),
        Action::Status => print_status(&status(&host)?, json),
        Action::Restart => print_report(&restart(&host, &stop_running_interpreter)?, json),
        Action::Run => run_supervisor(&host),
        Action::Ensure => print_ensured(&ensure(&host)?, json),
    }
}

fn print_ensured(outcome: &Ensured, json: bool) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(&outcome.value())?);
    } else {
        println!("{}", outcome.text());
    }
    Ok(())
}

/// A SILICON_INTERPRETER_HOME that cron still names but someone deleted (a finished test
/// installation). The default directory is always recreated.
fn vanished_directory(dir: &Path) -> bool {
    vanished(
        dir,
        std::env::var_os("SILICON_INTERPRETER_HOME").is_some(),
        std::env::var_os("HOME").map(PathBuf::from).as_deref(),
    )
}

fn vanished(dir: &Path, named: bool, home: Option<&Path>) -> bool {
    named
        && home
            .map(|home| home.join(".silicon-interpreter"))
            .as_deref()
            != Some(dir)
        && fs::symlink_metadata(dir).is_err_and(|error| error.kind() == ErrorKind::NotFound)
}

/// cron's `ensure` for a deleted interpreter directory: starting serve would create it
/// again and run a second interpreter against the main one at every boot, so its cron
/// lines are removed instead. The lines carry the hash of exactly this path.
fn forget_vanished(tools: &dyn Tools, dir: &Path) -> Result<Ensured> {
    let tag = format!("{TAG} {}", hash(dir));
    let current = crontab_read_with(tools, dir)?;
    let merged = merge_crontab(&current, &tag, &[]);
    if merged != current {
        crontab_write_with(tools, dir, &std::env::temp_dir(), &merged)?;
    }
    Ok(Ensured::Gone(dir.to_path_buf(), tag))
}

/// Runs the supervisors' command-line tools. Tests script the answers, so no test ever
/// touches this machine's launchd, systemd, logind or crontab.
trait Tools {
    fn run(
        &self,
        program: &Path,
        args: &[String],
        env: &[(String, String)],
        limit: Duration,
    ) -> std::io::Result<Output>;
    /// Start `program` in `cwd`, in a session of its own with its output appended to `log`,
    /// and do not wait for it. Returns its pid.
    fn detach(&self, program: &Path, args: &[&str], cwd: &Path, log: &Path)
        -> std::io::Result<u32>;
    fn signal(&self, pid: i32, signal: i32) -> std::io::Result<()>;
}

struct System;

impl Tools for System {
    fn run(
        &self,
        program: &Path,
        args: &[String],
        env: &[(String, String)],
        limit: Duration,
    ) -> std::io::Result<Output> {
        let mut command = Command::new(program);
        command.args(args);
        for (name, value) in env {
            command.env(name, value);
        }
        crate::process::output_within(&mut command, limit)
    }

    fn detach(
        &self,
        program: &Path,
        args: &[&str],
        cwd: &Path,
        log: &Path,
    ) -> std::io::Result<u32> {
        let file = open_log(log)?;
        let mut command = Command::new(program);
        // Not the caller's directory: a supervisor that lives for months would keep a USB
        // or network mount busy, as launchd and systemd would not (they use HOME).
        command
            .args(args)
            .current_dir(cwd)
            .env_remove("SILICON_SERVICE")
            .stdin(Stdio::null())
            .stdout(file.try_clone()?)
            .stderr(file);
        // Out of the caller's session, so the terminal or cron job that started it can end.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    Err(std::io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
        Ok(command.spawn()?.id())
    }

    fn signal(&self, pid: i32, signal: i32) -> std::io::Result<()> {
        if unsafe { libc::kill(pid, signal) } == 0 {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Platform {
    Mac,
    Linux,
    Wsl,
}

/// Everything the service code reads about this machine, gathered once so tests can
/// describe another machine.
struct Host<'a> {
    tools: &'a dyn Tools,
    platform: Platform,
    /// The interpreter directory, canonical.
    dir: PathBuf,
    home: PathBuf,
    /// The managed installation prefix, or why this silicon is not one.
    prefix: std::result::Result<PathBuf, String>,
    uid: u32,
    user: String,
    /// This shell's XDG_CONFIG_HOME when it is not ~/.config: earlier releases wrote the
    /// systemd unit there, where a user manager PAM started without it never looks.
    xdg_config_home: Option<PathBuf>,
    /// XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS for systemctl --user when a `su` or
    /// `sudo -u` shell lacks them but the user's bus exists.
    bus: Vec<(String, String)>,
    /// First 8 hex digits of a stable hash of `dir`; tags cron lines and names the
    /// definitions of a non-default interpreter directory.
    hash: String,
    /// The interpreter directory is not `~/.silicon-interpreter`.
    custom: bool,
    /// The environment `service install` captures.
    vars: Vec<(String, String)>,
    /// /opt/silicon inside the Silicon WSL distribution.
    wsl_root: PathBuf,
    boot_id: Option<String>,
    /// /proc, where a running cron daemon shows.
    proc_root: PathBuf,
    /// Between looks at something that is changing (a job launchd is removing, a task).
    poll: Duration,
}

impl<'a> Host<'a> {
    fn real(tools: &'a dyn Tools) -> Result<Self> {
        let dir = server::directory();
        state::private_dir(&dir)?;
        let dir = dir
            .canonicalize()
            .with_context(|| format!("resolve the interpreter directory {}", dir.display()))?;
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .filter(|home| home.is_absolute())
            .ok_or_else(|| {
                anyhow!(
                    "HOME is {:?}, not an absolute path, so there is no home for the service to run in",
                    std::env::var_os("HOME")
                )
            })?;
        let default = home.join(".silicon-interpreter");
        let custom = default.canonicalize().ok().as_ref() != Some(&dir);
        let uid = unsafe { libc::getuid() };
        let user = user_name(uid)
            .or_else(|| std::env::var("USER").ok())
            .or_else(|| std::env::var("LOGNAME").ok())
            .unwrap_or_else(|| uid.to_string());
        let xdg_config_home = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute() && *path != home.join(".config"));
        let runtime = PathBuf::from(format!("/run/user/{uid}"));
        let bus = if std::env::var_os("XDG_RUNTIME_DIR").is_none() && runtime.join("bus").exists() {
            vec![
                (
                    "XDG_RUNTIME_DIR".to_owned(),
                    runtime.to_string_lossy().into_owned(),
                ),
                (
                    "DBUS_SESSION_BUS_ADDRESS".to_owned(),
                    format!("unix:path={}", runtime.join("bus").display()),
                ),
            ]
        } else {
            Vec::new()
        };
        let wsl_root = PathBuf::from("/opt/silicon");
        let platform = if cfg!(target_os = "macos") {
            Platform::Mac
        } else if std::env::var("SILICON_WSL").as_deref() == Ok("1")
            || wsl_root.join("windows-version").exists()
        {
            Platform::Wsl
        } else {
            Platform::Linux
        };
        Ok(Self {
            tools,
            platform,
            hash: hash(&dir),
            dir,
            home,
            prefix: crate::update::managed_prefix().map_err(|error| format!("{error:#}")),
            uid,
            user,
            xdg_config_home,
            bus,
            custom,
            vars: process_vars(),
            wsl_root,
            boot_id: boot_id(),
            proc_root: PathBuf::from("/proc"),
            poll: Duration::from_millis(500),
        })
    }

    fn label(&self) -> String {
        if self.custom {
            format!("{LABEL}.{}", self.hash)
        } else {
            LABEL.to_owned()
        }
    }

    fn unit(&self) -> String {
        if self.custom {
            format!("{UNIT}-{}.service", self.hash)
        } else {
            format!("{UNIT}.service")
        }
    }

    fn plist(&self) -> PathBuf {
        self.home
            .join("Library/LaunchAgents")
            .join(format!("{}.plist", self.label()))
    }

    /// Always under ~/.config, whatever this shell's XDG_CONFIG_HOME: that is where the
    /// user manager looks, and PAM starts it without the variables a shell rc exports.
    fn unit_path(&self) -> PathBuf {
        self.home.join(".config/systemd/user").join(self.unit())
    }

    /// Units an earlier release wrote for this directory somewhere else: under this shell's
    /// XDG_CONFIG_HOME, or at `recorded` (a record's definition). Never the current unit
    /// under another name (a ~/.config linked to where XDG_CONFIG_HOME points).
    fn stale_units(&self, recorded: Option<&Path>) -> Vec<PathBuf> {
        let current = self.unit_path();
        let legacy = self
            .xdg_config_home
            .as_ref()
            .map(|config| config.join("systemd/user").join(self.unit()));
        let mut stale: Vec<PathBuf> = Vec::new();
        for path in legacy.into_iter().chain(recorded.map(Path::to_path_buf)) {
            if !same_file(&path, &current)
                && !stale.iter().any(|seen| same_file(seen, &path))
                && written_by_silicon(&path)
            {
                stale.push(path);
            }
        }
        stale
    }

    fn log(&self) -> PathBuf {
        self.dir.join("service.log")
    }

    fn tag(&self) -> String {
        format!("{TAG} {}", self.hash)
    }

    /// The silicon that runs `serve` and `service run`: the stable prefix symlink, or this
    /// executable when it is not a managed installation.
    fn silicon(&self) -> Result<PathBuf> {
        match &self.prefix {
            Ok(prefix) => Ok(prefix.join("bin/silicon")),
            Err(_) => std::env::current_exe().context("locate the running silicon executable"),
        }
    }

    fn call(
        &self,
        program: &str,
        args: &[&str],
        env: &[(String, String)],
        limit: Duration,
    ) -> Result<Output> {
        let owned: Vec<String> = args.iter().map(|arg| (*arg).to_owned()).collect();
        self.tools
            .run(Path::new(program), &owned, env, limit)
            .map_err(|error| failure::spawn(&self.dir, &failure::argv(program, args), &error))
    }

    /// The failure of a command that ran: argv, exit status and both streams.
    fn failed(&self, program: &str, args: &[&str], output: &Output) -> anyhow::Error {
        failure::command(&self.dir, &failure::argv(program, args), output, &[])
    }

    fn launchctl(&self, args: &[&str], limit: Duration) -> Result<Output> {
        self.call("launchctl", args, &[], limit)
    }

    fn systemctl(&self, args: &[&str], limit: Duration) -> Result<Output> {
        let args: Vec<&str> = std::iter::once("--user")
            .chain(args.iter().copied())
            .collect();
        self.call("systemctl", &args, &self.bus, limit)
    }

    /// systemctl --user that must succeed.
    fn systemctl_ok(&self, args: &[&str], limit: Duration) -> Result<Output> {
        let output = self.systemctl(args, limit)?;
        if !output.status.success() {
            let full: Vec<&str> = std::iter::once("--user")
                .chain(args.iter().copied())
                .collect();
            return Err(self.failed("systemctl", &full, &output));
        }
        Ok(output)
    }

    /// The mechanism this machine offers, and why a better one was not available.
    fn choose(&self) -> Result<(&'static str, Option<String>)> {
        Ok(match self.platform {
            Platform::Mac => ("launchd", None),
            Platform::Wsl => ("windows-task", None),
            Platform::Linux => {
                let args = ["show-environment"];
                match self.systemctl(&args, TOOL_LIMIT) {
                    Ok(output) if output.status.success() => ("systemd", None),
                    Ok(output) => (
                        "run",
                        Some(format!(
                            "no systemd user manager answers: {:#}",
                            self.failed("systemctl", &["--user", "show-environment"], &output)
                        )),
                    ),
                    Err(error) => ("run", Some(format!("no systemd user manager: {error:#}"))),
                }
            }
        })
    }

    fn render_plist(&self, prefix: &Path) -> String {
        let mut env = vec![
            ("SILICON_SERVICE".to_owned(), "launchd".to_owned()),
            ("HOME".to_owned(), self.home.to_string_lossy().into_owned()),
        ];
        if self.custom {
            env.push((
                "SILICON_INTERPRETER_HOME".to_owned(),
                self.dir.to_string_lossy().into_owned(),
            ));
        }
        // PATH is not secret; the rest of service.env stays out of this world-readable file.
        if let Some(path) = captured_path(&self.dir) {
            env.push(("PATH".to_owned(), path));
        }
        render_plist(&PlistInput {
            label: self.label(),
            program: prefix.join("bin/silicon"),
            home: self.home.clone(),
            env,
            log: self.log(),
        })
    }

    fn render_unit(&self, prefix: &Path) -> String {
        render_unit(
            &prefix.join("bin/silicon"),
            self.custom.then_some(self.dir.as_path()),
        )
    }

    fn cron_lines(&self, prefix: &Path) -> Vec<String> {
        let mut command = String::new();
        if self.custom {
            command.push_str(&format!(
                "SILICON_INTERPRETER_HOME={} ",
                shell_words::quote(&self.dir.to_string_lossy())
            ));
        }
        command.push_str(&format!(
            "{} service ensure",
            shell_words::quote(&prefix.join("bin/silicon").to_string_lossy())
        ));
        // cron turns an unescaped % into a newline.
        let command = command.replace('%', "\\%");
        let tag = self.tag();
        vec![
            format!("@reboot {command} >/dev/null 2>&1 {tag}"),
            format!("*/5 * * * * {command} >/dev/null 2>&1 {tag}"),
        ]
    }

    /// Windows PowerShell, as provision.sh recorded it for this distribution.
    fn powershell(&self, script: &str) -> Result<Output> {
        let file = self.wsl_root.join("windows-powershell");
        let program = fs::read_to_string(&file).with_context(|| {
            format!(
                "read {} (the Windows installer writes it) to find Windows PowerShell",
                file.display()
            )
        })?;
        self.call(
            program.trim(),
            &[
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            &[],
            TOOL_LIMIT,
        )
    }

    fn powershell_ok(&self, script: &str) -> Result<Output> {
        let output = self.powershell(script)?;
        if !output.status.success() {
            let file = self.wsl_root.join("windows-powershell");
            let program = fs::read_to_string(file).unwrap_or_default();
            return Err(self.failed(
                program.trim(),
                &[
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                &output,
            ));
        }
        Ok(output)
    }

    /// Ready, Running, Disabled, ... for the task at `label`.
    fn task_state(&self, label: &str) -> Result<String> {
        let output = self.powershell_ok(&format!(
            "Get-ScheduledTask {} | Select-Object -ExpandProperty State",
            task_query(label)
        ))?;
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
    }

    /// Start the task, and end the helper's wait if it is waiting to restart the
    /// interpreter after a failure: a task that runs ignores another start.
    fn start_task(&self, label: &str) -> Result<Output> {
        self.powershell_ok(&format!(
            "$ErrorActionPreference = 'Stop'; Start-ScheduledTask {}; New-Item -ItemType File -Force -Path (Join-Path $env:LOCALAPPDATA '{WAKE}') | Out-Null",
            task_query(label)
        ))
    }
}

/// `-TaskPath '\' -TaskName 'Silicon Interpreter S-1-5-21-…'` for the task whose full path
/// provision.sh recorded as the label.
fn task_query(label: &str) -> String {
    let (folder, name) = label.rsplit_once('\\').unwrap_or(("", label));
    let quoted = |text: &str| text.replace('\'', "''");
    format!(
        "-TaskPath '{}\\' -TaskName '{}'",
        quoted(folder),
        quoted(name)
    )
}

/// What install, uninstall and restart did.
#[derive(Debug, Serialize)]
struct Report {
    summary: String,
    mechanism: String,
    label: String,
    definition: Option<PathBuf>,
    notes: Vec<String>,
}

impl Report {
    fn text(&self) -> String {
        std::iter::once(self.summary.clone())
            .chain(self.notes.iter().map(|note| format!("note: {note}")))
            .collect::<Vec<_>>()
            .join("\n")
    }
}

fn print_report(report: &Report, json: bool) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(report)?);
    } else {
        println!("{}", report.text());
    }
    Ok(())
}

fn ensure_for_connect_with(host: &Host) -> Result<Option<String>> {
    // A test installation's service would start again at every boot and fight the main
    // interpreter for its ports; only an explicit `silicon service install` makes one.
    if host.custom || externally_supervised(&host.dir) {
        return Ok(None);
    }
    let record = load(&host.dir)?;
    if host.platform == Platform::Wsl {
        return Ok(match record {
            Some(record) if record.declined => None,
            Some(record) if !record.mechanism.is_empty() => {
                // The task's interpreter does not inherit this terminal's settings the way
                // one connect started would; service.env carries them, as on Unix.
                if !environment_file(&host.dir).exists() {
                    write_environment(&host.dir, &host.vars)?;
                }
                None
            }
            _ => Some(format!(
                "autostart on Windows is the logon task install.ps1 registers; rerun the Windows installer to add it (with -Service if it was turned off with -NoService). This interpreter directory {} has no service.json from it",
                host.dir.display()
            )),
        });
    }
    if record.is_some_and(|record| record.declined || !record.mechanism.is_empty()) {
        return Ok(None);
    }
    if host.prefix.is_err() {
        return Ok(None);
    }
    let _serial = changing(&host.dir)?;
    // Another connect may have installed it while this one waited.
    if load(&host.dir)?.is_some_and(|record| record.declined || !record.mechanism.is_empty()) {
        return Ok(None);
    }
    Ok(Some(install_serialized(host)?.text()))
}

/// daemon.json names `"supervisor": "external"`: the interpreter runs under the user's own
/// process manager (SILICON_SERVICE=external), and a second supervisor installed beside it
/// would start another serve at every boot to wait on daemon.lock against it.
fn externally_supervised(dir: &Path) -> bool {
    fs::read(dir.join("daemon.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|daemon| daemon["supervisor"] == "external")
}

/// Installs and uninstalls run one at a time per interpreter directory: two first
/// `silicon connect`s at once would otherwise both load the agent, and the second fail.
fn changing(dir: &Path) -> Result<File> {
    let path = dir.join("service-change.lock");
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .mode(0o600)
        .open(&path)
        .with_context(|| format!("open {}", path.display()))?;
    // Every command under it is bounded, so this wait is too.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(std::io::Error::last_os_error())
            .with_context(|| format!("lock {}", path.display()));
    }
    Ok(file)
}

fn unmanaged(why: &str) -> String {
    format!(
        "autostart needs a managed installation (install.sh puts one in ~/.local/share/silicon), so a development build never becomes a service: {why}. To supervise this build yourself, run `silicon serve` under your own process manager with SILICON_SERVICE=external set; a clean stop exits 0 and anything else exits non-zero"
    )
}

fn install(host: &Host) -> Result<Report> {
    let _serial = changing(&host.dir)?;
    install_serialized(host)
}

fn install_serialized(host: &Host) -> Result<Report> {
    if host.platform == Platform::Wsl {
        let record = load(&host.dir)?;
        // The one part of the service that lives in the distribution: the settings the
        // task's interpreter runs with, taken from this terminal.
        let captured = write_environment(&host.dir, &host.vars)?;
        let mut report = windows_report(
            host,
            format!(
                "on Windows the interpreter's autostart is the logon task the Windows installer registers and refreshes; rerun install.ps1 (with -Service if it was turned off) to add it. The interpreter it runs takes its settings from {}",
                environment_file(&host.dir).display()
            ),
            record.as_ref(),
        );
        report.notes.splice(0..0, captured);
        return Ok(report);
    }
    let prefix = host
        .prefix
        .clone()
        .map_err(|why| anyhow!(unmanaged(&why)))?;
    let previous = load(&host.dir)?;
    let (mechanism, why) = host.choose()?;
    let mut notes = Vec::new();
    if let Some(why) = why {
        notes.push(format!(
            "using cron and the Silicon supervisor because there is {why}"
        ));
    }
    if let Some(previous) = previous
        .as_ref()
        .filter(|old| !old.declined && !old.mechanism.is_empty() && old.mechanism != mechanism)
    {
        notes.push(format!(
            "replacing the {} service recorded in {}",
            previous.mechanism,
            record_path(&host.dir).display()
        ));
        if let Err(error) = remove(host, previous, &mut notes) {
            notes.push(format!("{error:#}"));
        }
    }
    clear_stopped_in(&host.dir)?;
    rotate_log(&host.log());
    notes.extend(write_environment(&host.dir, &host.vars)?);
    let linger = previous
        .as_ref()
        .is_some_and(|old| old.linger_enabled_by_silicon);
    let recorded = previous
        .as_ref()
        .filter(|old| old.mechanism == "systemd")
        .and_then(|old| old.definition.as_deref());
    let (record, when) = match mechanism {
        "launchd" => (install_launchd(host, &prefix, &mut notes)?, "at login"),
        "systemd" => install_systemd(host, &prefix, linger, recorded, &mut notes)?,
        _ => install_cron(host, &prefix, &mut notes)?,
    };
    Ok(Report {
        summary: format!(
            "autostart installed: {}{}; the interpreter starts {when} and restarts after a crash. `silicon service uninstall` turns it off",
            named(&record.mechanism, &record.label),
            record
                .definition
                .as_ref()
                .map(|path| format!(" ({})", path.display()))
                .unwrap_or_default()
        ),
        mechanism: record.mechanism,
        label: record.label,
        definition: record.definition,
        notes,
    })
}

/// "launchd agent com.teamofsilicons.silicon", "systemd user unit silicon.service", ...
fn named(mechanism: &str, label: &str) -> String {
    match mechanism {
        "launchd" => format!("launchd agent {label}"),
        "systemd" => format!("systemd user unit {label}"),
        "run" => format!("cron lines tagged `{TAG} {label}` and the Silicon supervisor"),
        "windows-task" => format!("Windows logon task {label}"),
        other => format!("{other} service {label}"),
    }
}

fn install_launchd(host: &Host, prefix: &Path, notes: &mut Vec<String>) -> Result<Record> {
    let plist = host.plist();
    let text = host.render_plist(prefix);
    let unchanged = fs::read_to_string(&plist).is_ok_and(|old| old == text);
    write_file(&plist, &text, 0o644)?;
    let record = Record {
        mechanism: "launchd".into(),
        definition: Some(plist.clone()),
        label: host.label(),
        installed_by: VERSION.into(),
        declined: false,
        linger_enabled_by_silicon: false,
    };
    // Recorded before loading: the plist exists and launchd loads it at the next login
    // whatever launchctl answers now, and uninstall must know to remove it.
    save(&host.dir, &record)?;
    notes.extend(privacy_warnings(&host.home, &host.dir));
    let target = format!("gui/{}/{}", host.uid, record.label);
    let busy = held(&host.dir.join("daemon.lock"))?;
    let loaded = (unchanged || busy)
        && host
            .launchctl(&["print", &target], TOOL_LIMIT)?
            .status
            .success();
    if busy && !loaded {
        // An interpreter no supervisor started holds daemon.lock. Loading the agent now
        // would queue a second serve behind it (RunAtLoad), and the next `silicon stop`
        // would hand over to that one instead of stopping.
        let on = ["enable", target.as_str()];
        let enable = host.launchctl(&on, TOOL_LIMIT)?;
        if !enable.status.success() {
            notes.push(format!("{:#}", host.failed("launchctl", &on, &enable)));
        }
        notes.push(unsupervised_note(
            host,
            "launchd loads the agent at the next login",
        ));
        return Ok(record);
    }
    if unchanged && loaded {
        // Loaded with this exact definition: make sure it runs, without restarting it.
        let _ = host.launchctl(&["enable", &target], TOOL_LIMIT)?;
        if busy {
            // Running already (the lock holder); a kickstart could only queue another.
            return Ok(record);
        }
        let args = ["kickstart", target.as_str()];
        let kick = host.launchctl(&args, TOOL_LIMIT)?;
        if !kick.status.success() {
            return Err(host.failed("launchctl", &args, &kick));
        }
        return Ok(record);
    }
    let out = ["bootout", target.as_str()];
    let bootout = host.launchctl(&out, STOP_LIMIT)?;
    let mut earlier = Vec::new();
    // A loaded job: bootout may answer before launchd has removed it, and a bootstrap
    // then fails with 5 or 37 while the old job is still stopping.
    let removing = bootout.status.success() || launchd_in_progress(&bootout);
    if removing {
        if let Err(error) = wait_unloaded(host, &target) {
            earlier.push(error);
        }
    } else if !launchd_not_loaded(&bootout) {
        earlier.push(host.failed("launchctl", &out, &bootout));
    }
    let on = ["enable", target.as_str()];
    let enable = host.launchctl(&on, TOOL_LIMIT)?;
    if !enable.status.success() {
        earlier.push(host.failed("launchctl", &on, &enable));
    }
    bootstrap(host, &plist, earlier, notes, removing)?;
    Ok(record)
}

/// Why the service was installed without being started now.
fn unsupervised_note(host: &Host, later: &str) -> String {
    format!(
        "an interpreter that no supervisor started is running (it holds {}), so autostart is installed but not started now: starting it would queue a second interpreter behind that one. `silicon stop` stops that interpreter for good; {later}, and `silicon connect` starts the service when no interpreter answers",
        host.dir.join("daemon.lock").display()
    )
}

/// `bootout` of a job launchd is still stopping: 36 Operation now in progress.
fn launchd_in_progress(output: &Output) -> bool {
    output.status.code() == Some(36) || said(output).contains("Operation now in progress")
}

/// Wait until `launchctl print` no longer finds `target`, for as long as its
/// interpreter may take to stop.
fn wait_unloaded(host: &Host, target: &str) -> Result<()> {
    let deadline = Instant::now() + STOP_LIMIT;
    loop {
        if !host
            .launchctl(&["print", target], TOOL_LIMIT)?
            .status
            .success()
        {
            return Ok(());
        }
        if Instant::now() >= deadline {
            bail!(
                "launchd still lists {target} {}s after `launchctl bootout {target}`",
                STOP_LIMIT.as_secs()
            );
        }
        thread::sleep(host.poll);
    }
}

/// Load the agent into the GUI domain; false when there is no GUI login to load it into
/// (SSH only): the plist stays and launchd loads it at the next login. `settling`: a job
/// with this label was just booted out, so 5 and 37 are retried for a while.
fn bootstrap(
    host: &Host,
    plist: &Path,
    earlier: Vec<anyhow::Error>,
    notes: &mut Vec<String>,
    settling: bool,
) -> Result<bool> {
    let domain = format!("gui/{}", host.uid);
    let plist_text = plist.to_string_lossy().into_owned();
    let args = ["bootstrap", domain.as_str(), plist_text.as_str()];
    let mut attempts = 1;
    let output = loop {
        let output = host.launchctl(&args, TOOL_LIMIT)?;
        let busy = matches!(output.status.code(), Some(5 | 37))
            || said(&output).contains("Operation already in progress");
        if output.status.success() || !settling || !busy || attempts >= 20 {
            break output;
        }
        attempts += 1;
        thread::sleep(host.poll);
    };
    if output.status.success() {
        return Ok(true);
    }
    let mut error = host.failed("launchctl", &args, &output);
    if attempts > 1 {
        error = error.context(format!(
            "launchctl bootstrap failed {attempts} times while launchd removed the earlier {}",
            plist.display()
        ));
    }
    if launchd_no_gui(&output) {
        notes.push(format!(
            "there is no GUI login for {} right now (for example over SSH), so launchd loads {} at the next login; until then `silicon connect` starts the interpreter without a supervisor. A Mac that must run with nobody logged in needs automatic login. {error:#}",
            host.user,
            plist.display()
        ));
        return Ok(false);
    }
    let error = earlier
        .into_iter()
        .fold(error, |error, earlier| failure::also(error, Err(earlier)));
    Err(error.context(format!(
        "launchd did not load {}; it stays there and launchd tries again at the next login. If System Settings > General > Login Items shows Silicon turned off, turn it on and run `silicon service install` again",
        plist.display()
    )))
}

/// `bootout` answers for a job that is not loaded (3 No such process, 113 Could not find
/// specified service, 125 when the GUI domain itself is missing).
fn launchd_not_loaded(output: &Output) -> bool {
    let said = said(output);
    matches!(output.status.code(), Some(3 | 113 | 125))
        || said.contains("No such process")
        || said.contains("Could not find")
        || launchd_no_gui(output)
}

fn launchd_no_gui(output: &Output) -> bool {
    let said = said(output);
    output.status.code() == Some(125)
        || said.contains("Domain does not support specified action")
        || said.contains("Could not find domain")
}

fn said(output: &Output) -> String {
    format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stderr),
        String::from_utf8_lossy(&output.stdout)
    )
}

/// The unit, and when it starts: at boot with linger, otherwise at login. `recorded`: where
/// the previous record says the unit was written.
fn install_systemd(
    host: &Host,
    prefix: &Path,
    linger_before: bool,
    recorded: Option<&Path>,
    notes: &mut Vec<String>,
) -> Result<(Record, &'static str)> {
    let unit = host.unit_path();
    write_file(&unit, &host.render_unit(prefix), 0o644)?;
    for stale in host.stale_units(recorded) {
        notes.push(match remove_unit(&stale) {
            Ok(()) => format!(
                "removed {}, which an earlier release wrote under XDG_CONFIG_HOME, where the systemd user manager may not look; the unit is now {}",
                stale.display(),
                unit.display()
            ),
            Err(error) => format!(
                "the unit an earlier release wrote under XDG_CONFIG_HOME stays behind: {error:#}"
            ),
        });
    }
    let mut record = Record {
        mechanism: "systemd".into(),
        definition: Some(unit),
        label: host.unit(),
        installed_by: VERSION.into(),
        declined: false,
        linger_enabled_by_silicon: linger_before,
    };
    save(&host.dir, &record)?;
    host.systemctl_ok(&["daemon-reload"], TOOL_LIMIT)?;
    // As for launchd: with an unsupervised interpreter holding daemon.lock, starting the
    // unit would queue a second serve that the next `silicon stop` hands over to.
    let busy = held(&host.dir.join("daemon.lock"))?
        && !host
            .systemctl(&["is-active", &record.label], TOOL_LIMIT)?
            .status
            .success();
    if busy {
        host.systemctl_ok(&["enable", &record.label], TOOL_LIMIT)?;
        notes.push(unsupervised_note(
            host,
            "systemd starts the unit at the next boot (or login)",
        ));
    } else {
        host.systemctl_ok(&["enable", "--now", &record.label], TOOL_LIMIT)?;
    }
    let (on, enabled_now) = ensure_linger(host, notes);
    if enabled_now {
        record.linger_enabled_by_silicon = true;
        save(&host.dir, &record)?;
    }
    Ok((record, if on { "at boot" } else { "at login" }))
}

/// `loginctl show-user USER -p Linger`: Some(yes/no), None when logind does not know the
/// user yet (no session and no linger, which is "no").
fn linger(host: &Host) -> Result<Option<bool>> {
    let args = ["show-user", host.user.as_str(), "-p", "Linger"];
    let output = host.call("loginctl", &args, &host.bus, TOOL_LIMIT)?;
    if !output.status.success() {
        if said(&output).contains("is not logged in or lingering") {
            return Ok(Some(false));
        }
        return Err(host.failed("loginctl", &args, &output));
    }
    let text = String::from_utf8_lossy(&output.stdout);
    Ok(match text.trim() {
        "Linger=yes" => Some(true),
        "Linger=no" => Some(false),
        _ => None,
    })
}

/// A user manager without linger starts at login and stops at logout. Whether linger is
/// on now, and whether this call turned it on.
fn ensure_linger(host: &Host, notes: &mut Vec<String>) -> (bool, bool) {
    let checked = linger(host);
    if matches!(checked, Ok(Some(true))) {
        return (true, false);
    }
    let args = ["enable-linger", host.user.as_str()];
    let refusal = match host.call("loginctl", &args, &host.bus, TOOL_LIMIT) {
        Ok(output) if output.status.success() => {
            notes.push(format!(
                "turned on linger for {}, so the interpreter starts at boot, not only at login",
                host.user
            ));
            return (true, true);
        }
        Ok(output) => host.failed("loginctl", &args, &output),
        Err(error) => error,
    };
    let refusal = match checked {
        Err(error) => failure::also(error, Err(refusal)),
        Ok(_) => refusal,
    };
    notes.push(format!(
        "without linger the interpreter runs only while {user} is logged in; to start it at boot, run `sudo loginctl enable-linger {user}`. {refusal:#}",
        user = host.user
    ));
    (false, false)
}

/// The crontab lines, and when the interpreter starts: at boot only while a cron daemon runs.
fn install_cron(
    host: &Host,
    prefix: &Path,
    notes: &mut Vec<String>,
) -> Result<(Record, &'static str)> {
    let current = crontab_read(host)?;
    let merged = merge_crontab(&current, &host.tag(), &host.cron_lines(prefix));
    if merged != current {
        crontab_write(host, &merged)?;
    }
    let record = Record {
        mechanism: "run".into(),
        definition: None,
        label: host.hash.clone(),
        installed_by: VERSION.into(),
        declined: false,
        linger_enabled_by_silicon: false,
    };
    save(&host.dir, &record)?;
    notes.push(match ensure(host)? {
        Ensured::Started(pid) => format!("started the Silicon supervisor now (pid {pid})"),
        other => other.text(),
    });
    // WSL without systemd, containers and minimal servers often have crontab but no
    // daemon reading it; then nothing runs the @reboot line.
    let when = if cron_running(&host.proc_root) == Some(false) {
        notes.push(format!(
            "no cron daemon is running (no cron or crond process in {}), so nothing runs the @reboot line: the supervisor started now keeps the interpreter up only until this machine (or WSL) restarts. Start cron and have it start at boot, for example `sudo service cron start` and, on WSL without systemd, `[boot]` `command=service cron start` in /etc/wsl.conf",
            host.proc_root.display()
        ));
        "now (at boot only once a cron daemon runs)"
    } else {
        "at boot"
    };
    Ok((record, when))
}

/// Whether a cron daemon runs, from `/proc/*/comm`; None when /proc cannot tell (not
/// Linux, or hidepid hides other users' processes, pid 1 included).
fn cron_running(proc_root: &Path) -> Option<bool> {
    if !proc_root.join("1").exists() {
        return None;
    }
    let entries = fs::read_dir(proc_root).ok()?;
    for entry in entries.flatten() {
        if !entry
            .file_name()
            .to_string_lossy()
            .bytes()
            .all(|byte| byte.is_ascii_digit())
        {
            continue;
        }
        let comm = fs::read_to_string(entry.path().join("comm")).unwrap_or_default();
        match comm.trim() {
            "cron" | "crond" | "cronie" | "dcron" | "fcron" => return Some(true),
            // `busybox crond`
            "busybox" => {
                let cmdline = fs::read(entry.path().join("cmdline")).unwrap_or_default();
                if cmdline
                    .split(|byte| *byte == 0)
                    .any(|arg| arg == b"crond" || arg.ends_with(b"/crond"))
                {
                    return Some(true);
                }
            }
            _ => {}
        }
    }
    Some(false)
}

fn no_cron(error: &std::io::Error) -> anyhow::Error {
    anyhow!(
        "this system has neither a systemd user manager nor crontab (could not run `crontab -l`: {error}), so Silicon cannot start the interpreter at boot by itself. Run `silicon serve` under your own process manager (s6, runit, supervisord, OpenRC, a container restart policy) with SILICON_SERVICE=external set; a clean stop exits 0 and anything else exits non-zero, so restart it on failure only"
    )
}

fn crontab_read(host: &Host) -> Result<String> {
    crontab_read_with(host.tools, &host.dir)
}

fn crontab_read_with(tools: &dyn Tools, dir: &Path) -> Result<String> {
    let args = ["-l".to_owned()];
    match tools.run(Path::new("crontab"), &args, &[], TOOL_LIMIT) {
        Err(error) if error.kind() == ErrorKind::NotFound => Err(no_cron(&error)),
        Err(error) => Err(failure::spawn(dir, "crontab -l", &error)),
        Ok(output) if output.status.success() => {
            Ok(String::from_utf8_lossy(&output.stdout).into_owned())
        }
        Ok(output) if empty_crontab(&output) => Ok(String::new()),
        Ok(output) => Err(failure::command(dir, "crontab -l", &output, &[])),
    }
}

/// "no crontab for USER" (cronie, Vixie), "can't open ... No such file" (BusyBox).
fn empty_crontab(output: &Output) -> bool {
    let said = said(output).to_lowercase();
    said.contains("no crontab for")
        || (said.contains("can't open") && said.contains("no such file"))
}

fn crontab_write(host: &Host, text: &str) -> Result<()> {
    crontab_write_with(host.tools, &host.dir, &host.dir, text)
}

/// Install `text` as the crontab, staged in `stage` (`crontab FILE`: the bounded runner
/// gives tools no stdin).
fn crontab_write_with(tools: &dyn Tools, dir: &Path, stage: &Path, text: &str) -> Result<()> {
    let staged = stage.join(format!(".crontab-{}", uuid::Uuid::new_v4().simple()));
    write_file(&staged, text, 0o600)?;
    let file = staged.to_string_lossy().into_owned();
    let shown = failure::argv("crontab", &[file.as_str()]);
    let result = tools
        .run(
            Path::new("crontab"),
            std::slice::from_ref(&file),
            &[],
            TOOL_LIMIT,
        )
        .map_err(|error| failure::spawn(dir, &shown, &error))
        .and_then(|output| {
            if output.status.success() {
                Ok(())
            } else {
                Err(failure::command(dir, &shown, &output, &[]))
            }
        });
    let _ = fs::remove_file(&staged);
    result.with_context(|| {
        format!(
            "install the merged crontab ({} lines, the tagged Silicon lines included)",
            text.lines().count()
        )
    })
}

/// Replace this interpreter directory's lines; every other line stays exactly as it was.
fn merge_crontab(current: &str, tag: &str, add: &[String]) -> String {
    let mut lines: Vec<&str> = current.lines().collect();
    // Old Vixie cron prints a three-line header with `crontab -l`; installing it back would
    // stack another header each time.
    if lines
        .first()
        .is_some_and(|line| line.starts_with("# DO NOT EDIT THIS FILE"))
        && lines.len() >= 3
        && lines[1].starts_with("# (")
        && lines[2].starts_with("# (")
    {
        lines.drain(..3);
    }
    let mut merged: Vec<String> = lines
        .into_iter()
        .filter(|line| !line.trim_end().ends_with(tag))
        .map(str::to_owned)
        .collect();
    merged.extend(add.iter().cloned());
    if merged.is_empty() {
        return String::new();
    }
    merged.join("\n") + "\n"
}

/// Remove a recorded service's definition. Failures come back after everything else ran.
fn remove(host: &Host, record: &Record, notes: &mut Vec<String>) -> Result<()> {
    let mut errors: Vec<anyhow::Error> = Vec::new();
    match record.mechanism.as_str() {
        "launchd" => {
            let label = if record.label.is_empty() {
                host.label()
            } else {
                record.label.clone()
            };
            let target = format!("gui/{}/{label}", host.uid);
            let args = ["bootout", target.as_str()];
            match host.launchctl(&args, STOP_LIMIT) {
                Ok(output) if output.status.success() || launchd_not_loaded(&output) => {}
                Ok(output) => errors.push(host.failed("launchctl", &args, &output)),
                Err(error) => errors.push(error),
            }
            let plist = record.definition.clone().unwrap_or_else(|| host.plist());
            if let Err(error) = remove_file(&plist) {
                errors.push(error);
            }
        }
        "systemd" => {
            let unit = if record.label.is_empty() {
                host.unit()
            } else {
                record.label.clone()
            };
            let args = ["disable", "--now", unit.as_str()];
            match host.systemctl(&args, STOP_LIMIT) {
                Ok(output)
                    if output.status.success()
                        || said(&output).contains("does not exist")
                        || said(&output).contains("not loaded") => {}
                Ok(output) => errors.push(host.failed(
                    "systemctl",
                    &["--user", "disable", "--now", &unit],
                    &output,
                )),
                Err(error) => errors.push(error),
            }
            let path = record.definition.clone().unwrap_or_else(|| host.unit_path());
            if let Err(error) = remove_unit(&path) {
                errors.push(error);
            }
            // Also where this release writes it and where an earlier one wrote it, when
            // the record names another place.
            let current = Some(host.unit_path()).filter(|unit| written_by_silicon(unit));
            for other in current.into_iter().chain(host.stale_units(None)) {
                if other != path {
                    if let Err(error) = remove_unit(&other) {
                        errors.push(error);
                    }
                }
            }
            if let Err(error) = host.systemctl_ok(&["daemon-reload"], TOOL_LIMIT) {
                errors.push(error);
            }
            if record.linger_enabled_by_silicon {
                notes.push(format!(
                    "Silicon turned on linger for {user} when it installed the service; if nothing else needs to start at boot, run `loginctl disable-linger {user}`",
                    user = host.user
                ));
            }
        }
        "run" => {
            match crontab_read(host) {
                Ok(current) => {
                    let merged = merge_crontab(&current, &host.tag(), &[]);
                    if merged != current {
                        if let Err(error) = crontab_write(host, &merged) {
                            errors.push(error);
                        }
                    }
                }
                Err(error) => errors.push(error),
            }
            match supervisor(&host.dir) {
                Ok(Some(pid)) => match host.tools.signal(pid, libc::SIGTERM) {
                    Ok(()) => notes.push(format!(
                        "stopped the Silicon supervisor (pid {pid}) and the interpreter under it"
                    )),
                    Err(error) => errors.push(anyhow!(
                        "could not send SIGTERM to the Silicon supervisor (pid {pid}): {error}"
                    )),
                },
                Ok(None) => {}
                Err(error) => errors.push(error),
            }
        }
        "windows-task" => notes.push(format!(
            "the Windows installer owns the logon task {}; run install.ps1 -NoService on Windows to remove it",
            record.label
        )),
        other => notes.push(format!("unknown service mechanism {other:?}; nothing removed")),
    }
    match errors
        .into_iter()
        .reduce(|error, next| failure::also(error, Err(next)))
    {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

fn uninstall(host: &Host) -> Result<Report> {
    let _serial = changing(&host.dir)?;
    if host.platform == Platform::Wsl {
        return Ok(windows_report(
            host,
            "on Windows the logon task belongs to the Windows installer; run install.ps1 -NoService on Windows to remove it (install.ps1 -Service adds it again)".into(),
            load(&host.dir)?.as_ref(),
        ));
    }
    let previous = load(&host.dir)?;
    // Without a record, remove what this directory's names would be, so a lost
    // service.json cannot leave an agent behind.
    let record = match previous.filter(|record| !record.mechanism.is_empty()) {
        Some(record) => record,
        None => Record {
            mechanism: match host.platform {
                Platform::Mac => "launchd",
                _ if host.unit_path().exists() || !host.stale_units(None).is_empty() => "systemd",
                _ => "run",
            }
            .into(),
            ..Record::default()
        },
    };
    let mut notes = Vec::new();
    let removed = if record.mechanism == "run" && record.label.is_empty() {
        // Nothing recorded and no unit: clean up cron quietly when there is no crontab.
        match crontab_read(host) {
            Ok(_) => remove(host, &record, &mut notes),
            Err(_) => Ok(()),
        }
    } else {
        remove(host, &record, &mut notes)
    };
    save(
        &host.dir,
        &Record {
            mechanism: record.mechanism.clone(),
            definition: None,
            label: record.label.clone(),
            installed_by: VERSION.into(),
            declined: true,
            linger_enabled_by_silicon: record.linger_enabled_by_silicon,
        },
    )?;
    let report = Report {
        summary: "autostart is off: the interpreter no longer starts at login or boot, and `silicon connect` will not install it again. `silicon service install` turns it back on".into(),
        mechanism: record.mechanism,
        label: record.label,
        definition: record.definition,
        notes,
    };
    removed.map_err(|error| error.context(report.text()))?;
    Ok(report)
}

/// The task is per Windows user, named by provision.sh's record; without one there is no
/// task to name.
fn windows_report(host: &Host, summary: String, record: Option<&Record>) -> Report {
    let label = record
        .map(|record| record.label.clone())
        .filter(|label| !label.is_empty());
    let note = match &label {
        Some(label) => match host.task_state(label) {
            Ok(state) => format!("the logon task {label} is {state}"),
            Err(error) => format!("could not read the state of the logon task {label}: {error:#}"),
        },
        None => format!(
            "{} has no record from the Windows installer, so this distribution does not know its logon task; rerun install.ps1 on Windows",
            record_path(&host.dir).display()
        ),
    };
    Report {
        summary,
        mechanism: "windows-task".into(),
        label: label.unwrap_or_default(),
        definition: None,
        notes: vec![note],
    }
}

fn start(host: &Host, record: &Record) -> Result<()> {
    match record.mechanism.as_str() {
        "launchd" => start_launchd(host, record),
        "systemd" => {
            refresh_unit(host, record)?;
            host.systemctl_ok(&["start", &record.label], TOOL_LIMIT)?;
            Ok(())
        }
        "run" => {
            clear_stopped_in(&host.dir)?;
            match ensure(host)? {
                // Only supervisor.lock is held, so no serve runs: the supervisor is waiting
                // out its restart delay (up to 5 minutes). SIGUSR2 ends the wait; a running
                // serve ignores it, unlike SIGUSR1, which restarts one.
                Ensured::Running(lock) if lock.ends_with("supervisor.lock") => {
                    if let Some(pid) = supervisor(&host.dir)? {
                        host.tools.signal(pid, libc::SIGUSR2).map_err(|error| {
                            anyhow!("could not send SIGUSR2 to the Silicon supervisor (pid {pid}) to restart the interpreter now: {error}")
                        })?;
                    }
                    Ok(())
                }
                Ensured::Started(_) | Ensured::Running(_) => Ok(()),
                other => bail!("{}", other.text()),
            }
        }
        "windows-task" => {
            host.start_task(&record.label)?;
            Ok(())
        }
        other => bail!(
            "{} records an unknown service mechanism {other:?}; run `silicon service install` to replace it",
            record_path(&host.dir).display()
        ),
    }
}

fn start_launchd(host: &Host, record: &Record) -> Result<()> {
    let plist = record.definition.clone().unwrap_or_else(|| host.plist());
    if let Err(error) = refresh(host, record) {
        crate::stderr_line(&format!("{error:#}"));
    }
    let target = format!("gui/{}/{}", host.uid, record.label);
    let args = ["kickstart", target.as_str()];
    let kick = host.launchctl(&args, TOOL_LIMIT)?;
    if kick.status.success() {
        return Ok(());
    }
    // Not loaded: booted out, or written while there was no GUI login.
    let first = host.failed("launchctl", &args, &kick);
    let _ = host.launchctl(&["enable", &target], TOOL_LIMIT)?;
    let mut notes = Vec::new();
    let loaded = bootstrap(host, &plist, Vec::new(), &mut notes, false);
    match loaded {
        Ok(true) => {}
        Ok(false) => bail!("{}", notes.join("\n")),
        Err(error) => {
            // Loaded after all (an answer bootstrap gives for an existing job): try once more.
            let again = host.launchctl(&args, TOOL_LIMIT)?;
            if again.status.success() {
                return Ok(());
            }
            return Err(failure::also(first, Err(error)));
        }
    }
    let again = host.launchctl(&args, TOOL_LIMIT)?;
    if again.status.success() {
        Ok(())
    } else {
        Err(failure::also(
            first,
            Err(host.failed("launchctl", &args, &again)),
        ))
    }
}

#[derive(Debug, PartialEq)]
enum Refreshed {
    Same,
    Rewritten,
    /// A systemd unit an earlier release wrote elsewhere now lives in ~/.config.
    Moved,
}

/// Rewrite a missing or outdated definition, so a deleted plist or a newer release's
/// template heals at the next start. A systemd unit recorded anywhere but ~/.config (an
/// earlier release's XDG_CONFIG_HOME) is written there instead and the record follows it.
fn refresh(host: &Host, record: &Record) -> Result<Refreshed> {
    let Ok(prefix) = &host.prefix else {
        return Ok(Refreshed::Same);
    };
    let (path, text) = match record.mechanism.as_str() {
        "launchd" if record.label == host.label() => (
            record.definition.clone().unwrap_or_else(|| host.plist()),
            host.render_plist(prefix),
        ),
        "systemd" if record.label == host.unit() => (host.unit_path(), host.render_unit(prefix)),
        _ => return Ok(Refreshed::Same),
    };
    let moved_from = record
        .definition
        .clone()
        .filter(|old| record.mechanism == "systemd" && *old != path);
    if moved_from.is_none() && fs::read_to_string(&path).is_ok_and(|old| old == text) {
        return Ok(Refreshed::Same);
    }
    write_file(&path, &text, 0o644)
        .with_context(|| format!("refresh the {} definition", record.mechanism))?;
    let Some(old) = moved_from else {
        return Ok(Refreshed::Rewritten);
    };
    // Asked after the write: a name for the file just written is no earlier copy.
    let other_file = !same_file(&old, &path);
    save(
        &host.dir,
        &Record {
            definition: Some(path),
            ..record.clone()
        },
    )?;
    if other_file && written_by_silicon(&old) {
        if let Err(error) = remove_unit(&old) {
            crate::stderr_line(&format!(
                "warning: the unit an earlier release wrote stays behind: {error:#}"
            ));
        }
    }
    Ok(Refreshed::Moved)
}

/// Before systemctl starts the unit: a changed file needs daemon-reload, and a moved one
/// is enabled again where the manager reads it, so it still starts at boot.
fn refresh_unit(host: &Host, record: &Record) -> Result<()> {
    let refreshed = refresh(host, record)?;
    if refreshed != Refreshed::Same {
        host.systemctl_ok(&["daemon-reload"], TOOL_LIMIT)?;
    }
    if refreshed == Refreshed::Moved {
        host.systemctl_ok(&["enable", &record.label], TOOL_LIMIT)?;
    }
    Ok(())
}

/// Restart now through the supervisor. `stop` asks a running interpreter for an orderly
/// stop and waits for it (Windows only, where the task restarts on its next start).
fn restart(host: &Host, stop: &dyn Fn(&Path) -> Result<bool>) -> Result<Report> {
    let record = load(&host.dir)?
        .filter(|record| !record.declined && !record.mechanism.is_empty())
        .ok_or_else(|| {
            anyhow!("autostart is not installed for {}, so there is no supervisor to restart the interpreter; `silicon service install` installs it", host.dir.display())
        })?;
    clear_stopped_in(&host.dir)?;
    let mut notes = Vec::new();
    let summary = match record.mechanism.as_str() {
        "launchd" => {
            let target = format!("gui/{}/{}", host.uid, record.label);
            let args = ["kickstart", "-k", target.as_str()];
            let output = host.launchctl(&args, STOP_LIMIT)?;
            if !output.status.success() {
                start_launchd(host, &record)
                    .map_err(|error| failure::also(host.failed("launchctl", &args, &output), Err(error)))?;
            }
            format!("launchd is restarting the interpreter ({})", record.label)
        }
        "systemd" => {
            refresh_unit(host, &record)?;
            host.systemctl_ok(&["restart", &record.label], STOP_LIMIT)?;
            format!("systemd restarted the interpreter ({})", record.label)
        }
        "run" => match supervisor(&host.dir)? {
            Some(pid) => {
                host.tools.signal(pid, libc::SIGUSR1).map_err(|error| {
                    anyhow!("could not send SIGUSR1 to the Silicon supervisor (pid {pid}): {error}")
                })?;
                format!("asked the Silicon supervisor (pid {pid}) to restart the interpreter")
            }
            None => match ensure(host)? {
                Ensured::Started(pid) => format!(
                    "the Silicon supervisor was not running; started it (pid {pid})"
                ),
                Ensured::Running(lock) => bail!(
                    "an interpreter runs without the Silicon supervisor ({} is held); stop it with `silicon stop`, then run `silicon service restart`",
                    lock.display()
                ),
                other => bail!("{}", other.text()),
            },
        },
        "windows-task" => {
            let label = record.label.as_str();
            let stopped = stop(&host.dir)?;
            // From here on the interpreter is down, so every step runs whatever the one
            // before answered: a failed look must not leave it stopped.
            let mut problems = Vec::new();
            // The task ends when the interpreter it supervises stops cleanly; a start while
            // it still runs is ignored (MultipleInstances IgnoreNew).
            let deadline = Instant::now() + Duration::from_secs(30);
            let running = loop {
                match host.task_state(label) {
                    Ok(state) if state != "Running" => break false,
                    Ok(_) if Instant::now() >= deadline => break true,
                    Ok(_) => thread::sleep(host.poll),
                    Err(error) => {
                        problems.push(error);
                        break true;
                    }
                }
            };
            // Still running (the helper is waiting out its backoff, or stuck), or unknown:
            // end it, so the start below is not ignored.
            if running {
                if let Err(error) = host.powershell_ok(&format!(
                    "Stop-ScheduledTask {}",
                    task_query(label)
                )) {
                    problems.push(error);
                }
            }
            if let Err(error) = host.start_task(label) {
                return Err(problems
                    .into_iter()
                    .fold(error, |error, earlier| failure::also(error, Err(earlier))));
            }
            notes.extend(problems.iter().map(|error| format!("{error:#}")));
            if stopped {
                format!("stopped the interpreter and started the logon task {label} again")
            } else {
                format!("started the logon task {label}")
            }
        }
        other => bail!("unknown service mechanism {other:?} in {}", record_path(&host.dir).display()),
    };
    Ok(Report {
        summary,
        mechanism: record.mechanism,
        label: record.label,
        definition: record.definition,
        notes,
    })
}

/// Ask the interpreter answering for `dir` to stop and wait until it released daemon.lock.
/// False when none answered.
fn stop_running_interpreter(dir: &Path) -> Result<bool> {
    let Ok(daemon) = server::daemon(false) else {
        return Ok(false);
    };
    server::call(&daemon, "shutdown", json!({}))?;
    let lock = dir.join("daemon.lock");
    let deadline = Instant::now() + Duration::from_secs(120);
    while held(&lock)? {
        if Instant::now() >= deadline {
            bail!(
                "the interpreter (pid {}) did not release {} within 120s of `shutdown`",
                daemon.pid,
                lock.display()
            );
        }
        thread::sleep(Duration::from_millis(200));
    }
    Ok(true)
}

/// `silicon service status`.
#[derive(Debug, Default, Serialize)]
struct Status {
    installed: bool,
    declined: bool,
    mechanism: Option<String>,
    label: Option<String>,
    definition: Option<PathBuf>,
    /// What the supervisor reports: state, pid, last exit, restarts, linger, ...
    details: BTreeMap<String, String>,
    /// Whether an interpreter holds daemon.lock.
    interpreter: String,
    /// The `stopped` marker `silicon stop` leaves, with `this_boot`: a marker from an
    /// earlier boot no longer holds anything back.
    stopped: Option<Value>,
    logs: Vec<PathBuf>,
    notes: Vec<String>,
}

fn status(host: &Host) -> Result<Status> {
    let mut status = Status {
        interpreter: match held(&host.dir.join("daemon.lock")) {
            Ok(true) => "running".into(),
            Ok(false) => "not running".into(),
            Err(error) => format!("unknown: {error:#}"),
        },
        stopped: stop_marker(host),
        logs: vec![host.log(), host.dir.join("daemon.log")],
        ..Status::default()
    };
    let record = load(&host.dir)?;
    let Some(record) = record.filter(|record| !record.mechanism.is_empty() || record.declined)
    else {
        status.notes.push(match host.platform {
            Platform::Wsl => format!("no service.json from the Windows installer in {}; rerun install.ps1 on Windows (with -Service if it was turned off) to add the logon task", host.dir.display()),
            _ => "autostart is not installed; `silicon service install` or the next `silicon connect` installs it".into(),
        });
        return Ok(status);
    };
    status.mechanism = Some(record.mechanism.clone());
    status.label = Some(record.label.clone());
    status.definition = record.definition.clone();
    if record.declined {
        status.declined = true;
        status.notes.push(
            "autostart was turned off with `silicon service uninstall`; `silicon service install` turns it back on".into(),
        );
        return Ok(status);
    }
    status.installed = true;
    if let Some(path) = &record.definition {
        status
            .details
            .insert("definition present".into(), path.exists().to_string());
    }
    match record.mechanism.as_str() {
        "launchd" => {
            let target = format!("gui/{}/{}", host.uid, record.label);
            let args = ["print", target.as_str()];
            match host.launchctl(&args, TOOL_LIMIT) {
                Ok(output) if output.status.success() => {
                    status.details.insert("loaded".into(), "yes".into());
                    status
                        .details
                        .extend(launchd_details(&String::from_utf8_lossy(&output.stdout)));
                }
                Ok(output) => {
                    status.details.insert("loaded".into(), "no".into());
                    let error = host.failed("launchctl", &args, &output);
                    status.notes.push(if launchd_no_gui(&output) {
                        format!("no GUI login right now, so launchd loads the agent at the next login: {error:#}")
                    } else {
                        format!("launchd has not loaded the agent; `silicon connect` or `silicon service restart` loads it: {error:#}")
                    });
                }
                Err(error) => status.notes.push(format!("{error:#}")),
            }
            status.notes.extend(privacy_warnings(&host.home, &host.dir));
            status.notes.push("launchd starts the agent when you log in: a Mac restarted with nobody logging in (or with FileVault still locked) runs no Silicon until then".into());
        }
        "systemd" => {
            let args = [
                "show",
                record.label.as_str(),
                "-p",
                "ActiveState,SubState,MainPID,ExecMainStatus,NRestarts,UnitFileState",
            ];
            match host.systemctl_ok(&args, TOOL_LIMIT) {
                Ok(output) => status
                    .details
                    .extend(key_values(&String::from_utf8_lossy(&output.stdout))),
                Err(error) => status.notes.push(format!("{error:#}")),
            }
            match linger(host) {
                Ok(Some(true)) => {
                    status.details.insert("Linger".into(), "yes".into());
                }
                Ok(state) => {
                    status.details.insert(
                        "Linger".into(),
                        if state.is_some() { "no" } else { "unknown" }.into(),
                    );
                    status.notes.push(format!(
                        "without linger the interpreter runs only while {user} is logged in; to start it at boot, run `sudo loginctl enable-linger {user}`",
                        user = host.user
                    ));
                }
                Err(error) => status.notes.push(format!("{error:#}")),
            }
        }
        "run" => {
            match supervisor(&host.dir) {
                Ok(Some(pid)) => {
                    status
                        .details
                        .insert("supervisor".into(), format!("running (pid {pid})"));
                }
                Ok(None) => {
                    status
                        .details
                        .insert("supervisor".into(), "not running".into());
                }
                Err(error) => status.notes.push(format!("{error:#}")),
            }
            match crontab_read(host) {
                Ok(text) => {
                    let lines = text
                        .lines()
                        .filter(|line| line.trim_end().ends_with(&host.tag()))
                        .count();
                    status
                        .details
                        .insert("crontab lines".into(), lines.to_string());
                    if lines == 0 {
                        status.notes.push(format!("the crontab has no line tagged `{}`, so nothing starts the interpreter at boot; `silicon service install` adds them", host.tag()));
                    }
                }
                Err(error) => status.notes.push(format!("{error:#}")),
            }
        }
        "windows-task" => match host.task_state(&record.label) {
            Ok(state) => {
                status.details.insert("task state".into(), state);
            }
            Err(error) => status.notes.push(format!("{error:#}")),
        },
        _ => {}
    }
    Ok(status)
}

fn print_status(status: &Status, json: bool) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(status)?);
        return Ok(());
    }
    let autostart = match (&status.mechanism, status.installed, status.declined) {
        (Some(mechanism), true, _) => format!(
            "installed ({})",
            named(mechanism, status.label.as_deref().unwrap_or_default())
        ),
        (_, _, true) => "turned off".into(),
        _ => "not installed".into(),
    };
    println!("autostart: {autostart}");
    if let Some(path) = &status.definition {
        println!("definition: {}", path.display());
    }
    for (key, value) in &status.details {
        println!("{key}: {value}");
    }
    println!("interpreter: {}", status.interpreter);
    if let Some(stopped) = &status.stopped {
        if stopped["this_boot"] == Value::Bool(true) {
            println!("stopped by `silicon stop` during this boot: {stopped}");
        } else {
            println!("a `silicon stop` from an earlier boot (ignored): {stopped}");
        }
    }
    println!(
        "logs: {}",
        status
            .logs
            .iter()
            .map(|path| path.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    );
    for note in &status.notes {
        println!("note: {note}");
    }
    Ok(())
}

/// The `stopped` marker as status shows it: its fields plus whether it is from this boot.
fn stop_marker(host: &Host) -> Option<Value> {
    let bytes = fs::read(host.dir.join("stopped")).ok()?;
    let mut marker = serde_json::from_slice::<Value>(&bytes).unwrap_or_else(
        |error| json!({"unreadable": error.to_string(), "text": String::from_utf8_lossy(&bytes)}),
    );
    let this_boot = host.boot_id.is_some() && marker["boot_id"].as_str() == host.boot_id.as_deref();
    if let Value::Object(fields) = &mut marker {
        fields.insert("this_boot".into(), Value::Bool(this_boot));
    }
    Some(marker)
}

/// The top-level fields of `launchctl print` that say whether the job runs and how it ended.
fn launchd_details(text: &str) -> BTreeMap<String, String> {
    let mut found = BTreeMap::new();
    for line in text.lines() {
        let Some(rest) = line.strip_prefix('\t') else {
            continue;
        };
        if rest.starts_with('\t') {
            continue;
        }
        let Some((key, value)) = rest.split_once(" = ") else {
            continue;
        };
        let key = key.trim();
        if matches!(
            key,
            "state" | "pid" | "runs" | "last exit code" | "last terminating signal"
        ) {
            found
                .entry(key.to_owned())
                .or_insert_with(|| value.trim().to_owned());
        }
    }
    found
}

fn key_values(text: &str) -> BTreeMap<String, String> {
    text.lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key.trim().to_owned(), value.trim().to_owned()))
        .collect()
}

/// macOS keeps these from processes launchd starts unless they have Full Disk Access,
/// which an ad-hoc signed binary loses at every update.
fn privacy_warnings(home: &Path, dir: &Path) -> Vec<String> {
    let path = dir.join("connections.json");
    let rows = match fs::read(&path) {
        Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
            Ok(Value::Array(rows)) => rows,
            Ok(other) => {
                return vec![format!(
                    "could not check saved connections for macOS privacy-protected folders: {} holds {other}, not a list",
                    path.display()
                )]
            }
            Err(error) => {
                return vec![format!(
                    "could not check saved connections for macOS privacy-protected folders: {} is not valid JSON: {error}",
                    path.display()
                )]
            }
        },
        Err(error) if error.kind() == ErrorKind::NotFound => return Vec::new(),
        Err(error) => {
            return vec![format!(
                "could not check saved connections for macOS privacy-protected folders: read {}: {error}",
                path.display()
            )]
        }
    };
    let protected = protected_folders(home);
    let mut warnings = Vec::new();
    for row in rows {
        let id = row["id"].as_str().unwrap_or("a saved connection");
        for key in ["yaml", "home"] {
            if let Some(warning) = row[key]
                .as_str()
                .and_then(|value| privacy_warning(&protected, id, key, value))
            {
                warnings.push(warning);
            }
        }
    }
    warnings
}

/// The folders macOS privacy protection covers, as given and resolved.
fn protected_folders(home: &Path) -> Vec<PathBuf> {
    let mut protected: Vec<PathBuf> = [
        "Desktop",
        "Documents",
        "Downloads",
        "Library/Mobile Documents",
    ]
    .iter()
    .map(|folder| home.join(folder))
    .chain(std::iter::once(PathBuf::from("/Volumes")))
    .collect();
    let resolved: Vec<PathBuf> = protected
        .iter()
        .filter_map(|folder| folder.canonicalize().ok())
        .collect();
    protected.extend(resolved);
    protected
}

/// The warning for one connection's `key` (yaml or home) at `value`, if it lies in a
/// protected folder.
fn privacy_warning(protected: &[PathBuf], id: &str, key: &str, value: &str) -> Option<String> {
    let given = PathBuf::from(value);
    let real = given.canonicalize().ok();
    let folder = protected.iter().find(|folder| {
        given.starts_with(folder) || real.as_ref().is_some_and(|real| real.starts_with(folder))
    })?;
    Some(format!(
        "{id}: its {key} {} is inside {}, which macOS privacy protection keeps from programs launchd starts, so it may fail to load under the agent, now or after a login or restart; move it elsewhere (for example ~/silicon) and connect it again. A Full Disk Access grant would not survive Silicon updates",
        given.display(),
        folder.display()
    ))
}

/// `silicon service ensure`: what it found, or the supervisor it started.
#[derive(Debug, PartialEq)]
enum Ensured {
    Stopped(DateTime<Utc>),
    Running(PathBuf),
    Declined,
    Other(String),
    Started(u32),
    /// The interpreter directory is gone; its crontab lines (this tag) were removed.
    Gone(PathBuf, String),
}

impl Ensured {
    fn text(&self) -> String {
        match self {
            Ensured::Stopped(at) => format!("the interpreter was stopped with `silicon stop` at {} during this boot; it stays stopped until the next boot or `silicon connect`", at.to_rfc3339()),
            Ensured::Running(lock) => format!("an interpreter or its supervisor is already running ({} is held)", lock.display()),
            Ensured::Declined => "autostart was turned off with `silicon service uninstall`; nothing to start".into(),
            Ensured::Other(mechanism) => format!("the interpreter is supervised by {mechanism}, not cron; nothing to start"),
            Ensured::Started(pid) => format!("started the Silicon supervisor (pid {pid})"),
            Ensured::Gone(dir, tag) => format!("the interpreter directory {} no longer exists, so nothing was started and it was not created again; removed the crontab lines tagged `{tag}`", dir.display()),
        }
    }

    fn value(&self) -> Value {
        let (outcome, pid) = match self {
            Ensured::Stopped(_) => ("stopped", None),
            Ensured::Running(_) => ("running", None),
            Ensured::Declined => ("declined", None),
            Ensured::Other(_) => ("other", None),
            Ensured::Started(pid) => ("started", Some(*pid)),
            Ensured::Gone(..) => ("gone", None),
        };
        json!({"outcome": outcome, "pid": pid, "message": self.text()})
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct Stopped {
    boot_id: Option<String>,
    at: DateTime<Utc>,
}

/// The `stopped` marker `silicon stop` leaves in the interpreter directory, read against
/// this boot.
struct Marker {
    path: PathBuf,
    boot_id: Option<String>,
    /// What was last said about a marker that cannot be read, so a supervisor asking at
    /// every failure says it once, and again only when it changes.
    told: std::cell::RefCell<Option<String>>,
}

impl Marker {
    fn of(host: &Host) -> Self {
        Self::new(host.dir.join("stopped"), host.boot_id.clone())
    }

    fn new(path: PathBuf, boot_id: Option<String>) -> Self {
        Self {
            path,
            boot_id,
            told: Default::default(),
        }
    }

    /// When `silicon stop` stopped the interpreter during this boot. A marker from another
    /// boot, or with a boot id nobody can compare, holds nothing back.
    fn this_boot(&self) -> Option<DateTime<Utc>> {
        let problem = match fs::read(&self.path) {
            Ok(bytes) => match serde_json::from_slice::<Stopped>(&bytes) {
                Ok(stopped) => {
                    return (stopped.boot_id.is_some() && stopped.boot_id == self.boot_id)
                        .then_some(stopped.at)
                }
                // A broken marker must not keep the interpreter down.
                Err(error) => format!(
                    "ignoring {}: {error}; it held: {}",
                    self.path.display(),
                    String::from_utf8_lossy(&bytes)
                ),
            },
            Err(error) if error.kind() == ErrorKind::NotFound => return None,
            Err(error) => format!("ignoring {}: {error}", self.path.display()),
        };
        if self.told.borrow().as_ref() != Some(&problem) {
            crate::stderr_line(&problem);
            *self.told.borrow_mut() = Some(problem);
        }
        None
    }
}

/// Start `silicon service run` unless the interpreter was stopped this boot, or it or the
/// supervisor already runs. Fast and idempotent: cron runs it every five minutes.
fn ensure(host: &Host) -> Result<Ensured> {
    if let Some(record) = load(&host.dir)? {
        if record.declined {
            return Ok(Ensured::Declined);
        }
        if !record.mechanism.is_empty() && record.mechanism != "run" {
            return Ok(Ensured::Other(record.mechanism));
        }
    }
    if let Some(at) = Marker::of(host).this_boot() {
        return Ok(Ensured::Stopped(at));
    }
    for lock in ["daemon.lock", "supervisor.lock"] {
        let lock = host.dir.join(lock);
        if held(&lock)? {
            return Ok(Ensured::Running(lock));
        }
    }
    let program = host.silicon()?;
    rotate_log(&host.log());
    let pid = host
        .tools
        .detach(&program, &["service", "run"], &host.home, &host.log())
        .map_err(|error| {
            failure::spawn(
                &host.dir,
                &failure::argv(&program, &["service", "run"]),
                &error,
            )
        })?;
    Ok(Ensured::Started(pid))
}

/// The supervisor's pid when it holds supervisor.lock.
fn supervisor(dir: &Path) -> Result<Option<i32>> {
    let lock = dir.join("supervisor.lock");
    if !held(&lock)? {
        return Ok(None);
    }
    let text = fs::read_to_string(&lock).with_context(|| format!("read {}", lock.display()))?;
    text.trim().parse().map(Some).with_context(|| {
        format!(
            "{} is held but holds {text:?}, not the supervisor's pid",
            lock.display()
        )
    })
}

/// How the supervisor paces restarts. Wall-clock uptime decides "healthy", so a laptop
/// that slept through most of a run does not count it as a crash loop.
struct Policy {
    base: Duration,
    cap: Duration,
    healthy: Duration,
    /// After a failure, how long the processes left in the group get between SIGTERM and SIGKILL.
    grace: Duration,
    /// After a forwarded stop, how long serve gets before its group is killed.
    patience: Duration,
}

impl Policy {
    fn standard() -> Self {
        Self {
            base: Duration::from_secs(5),
            cap: Duration::from_secs(5 * 60),
            healthy: Duration::from_secs(10 * 60),
            grace: Duration::from_secs(5),
            patience: Duration::from_secs(90),
        }
    }

    /// Failures in a row after one that ended a run of `uptime`; None means the clock
    /// went backwards, which counts as long ago.
    fn failures_after(&self, previous: u32, uptime: Option<Duration>) -> u32 {
        match uptime {
            Some(uptime) if uptime < self.healthy => previous.saturating_add(1),
            _ => 1,
        }
    }

    /// base × 2^(failures−1), at most `cap`.
    fn delay(&self, failures: u32) -> Duration {
        let doublings = failures.saturating_sub(1).min(20);
        self.base.saturating_mul(1 << doublings).min(self.cap)
    }
}

enum Event {
    Signal(i32),
    Exited(std::io::Result<ExitStatus>),
}

/// `silicon service run`.
fn run_supervisor(host: &Host) -> Result<()> {
    run_supervisor_with(host, &Policy::standard(), listen_for_signals)
}

/// Stops the signal listener [`listen_for_signals`] started.
type Unlisten = Box<dyn FnOnce()>;

/// Deliver SIGTERM, SIGINT, SIGHUP, SIGUSR1 (restart) and SIGUSR2 (end a restart delay)
/// as events.
fn listen_for_signals(events: mpsc::Sender<Event>) -> Result<Unlisten> {
    let mut signals = signal_hook::iterator::Signals::new([
        libc::SIGTERM,
        libc::SIGINT,
        libc::SIGHUP,
        libc::SIGUSR1,
        libc::SIGUSR2,
    ])
    .context("listen for SIGTERM, SIGINT, SIGHUP, SIGUSR1 and SIGUSR2")?;
    let handle = signals.handle();
    thread::Builder::new()
        .name("service-signals".into())
        .spawn(move || {
            for signal in signals.forever() {
                if events.send(Event::Signal(signal)).is_err() {
                    break;
                }
            }
        })
        .context("start the supervisor's signal thread")?;
    Ok(Box::new(move || handle.close()))
}

/// [`run_supervisor`] with the restart policy and signal source a test chooses.
fn run_supervisor_with(
    host: &Host,
    policy: &Policy,
    listen: impl FnOnce(mpsc::Sender<Event>) -> Result<Unlisten>,
) -> Result<()> {
    let path = host.dir.join("supervisor.lock");
    let Some(mut lock) = try_lock(&path)? else {
        crate::stdout_line(&format!(
            "another Silicon supervisor holds {}; nothing to do",
            path.display()
        ));
        return Ok(());
    };
    // Resolved once: a later update may remove the release this supervisor runs from.
    let program = host.silicon()?;
    let (events, inbox) = mpsc::channel();
    // Listening before the pid is published: the default action of SIGUSR1 and SIGUSR2
    // would end the supervisor that `silicon service restart` or connect meant to ask.
    let unlisten = listen(events.clone())?;
    lock.set_len(0)
        .and_then(|()| lock.write_all(std::process::id().to_string().as_bytes()))
        .with_context(|| format!("record this supervisor's pid in {}", path.display()))?;
    let result = supervise(
        &program,
        &["serve".to_owned()],
        &host.home,
        &host.log(),
        &Marker::of(host),
        policy,
        &events,
        &inbox,
    );
    unlisten();
    drop(lock);
    result
}

/// Run `program args` in `cwd` with SILICON_SERVICE=run in its own process group until it
/// stops cleanly or a stop signal arrives. SIGTERM, SIGINT and SIGHUP are forwarded and end
/// the supervisor once serve exits; SIGUSR1 restarts serve now; SIGUSR2 ends a restart
/// delay and is ignored while serve runs. Any other exit is restarted with backoff, after
/// the processes serve left in its group (Caddy, omnid) are stopped, unless `marker` says
/// `silicon stop` stopped the interpreter during this boot: a stop that found it waiting
/// out the delay holds, and the supervisor exits 0.
#[allow(clippy::too_many_arguments)] // The job, its stop marker, its pacing and its events.
fn supervise(
    program: &Path,
    args: &[String],
    cwd: &Path,
    log: &Path,
    marker: &Marker,
    policy: &Policy,
    events: &mpsc::Sender<Event>,
    inbox: &mpsc::Receiver<Event>,
) -> Result<()> {
    // The full path: which release answered is what a reader of service.log needs.
    let shown = shell_words::join(
        std::iter::once(program.to_string_lossy().into_owned()).chain(args.iter().cloned()),
    );
    note(
        log,
        &format!(
            "supervising `{shown}` (supervisor pid {})",
            std::process::id()
        ),
    );
    // Asked before waiting to restart after a failure, and again before the restart. True
    // (and said, after `what` happened) when the supervisor is to exit instead.
    let held = |what: &str| {
        let Some(at) = marker.this_boot() else {
            return false;
        };
        note(
            log,
            &format!(
                "{what}; `silicon stop` stopped the interpreter at {} during this boot ({}), so it is not restarted and the supervisor exits",
                at.to_rfc3339(),
                marker.path.display()
            ),
        );
        true
    };
    let mut failures = 0u32;
    loop {
        let started = SystemTime::now();
        let child = match spawn_serve(program, args, cwd, log) {
            Ok(child) => child,
            Err(error) => {
                failures = policy.failures_after(failures, Some(Duration::ZERO));
                let delay = policy.delay(failures);
                let failed = format!("could not start `{shown}`: {error}");
                if held(&failed) {
                    return Ok(());
                }
                note(log, &format!("{failed}; trying again in {}", human(delay)));
                match pause(inbox, delay) {
                    Pause::Stop(signal) => {
                        note(
                            log,
                            &format!("received {}; the supervisor exits", signal_name(signal)),
                        );
                        return Ok(());
                    }
                    Pause::Now(signal) => {
                        note(
                            log,
                            &format!("received {}; trying again now", signal_name(signal)),
                        );
                        failures = 0;
                    }
                    Pause::Elapsed => {}
                }
                if held("the restart delay ended") {
                    return Ok(());
                }
                continue;
            }
        };
        let pid = child.id() as i32;
        note(log, &format!("started the interpreter (pid {pid})"));
        let exited = Arc::new(AtomicBool::new(false));
        {
            let (sender, exited) = (events.clone(), exited.clone());
            let mut child = child;
            thread::Builder::new()
                .name("service-wait".into())
                .spawn(move || {
                    let status = child.wait();
                    exited.store(true, Ordering::SeqCst);
                    let _ = sender.send(Event::Exited(status));
                })
                .context("start the supervisor's wait thread")?;
        }
        let mut stop = None;
        let mut restart = false;
        let mut deadline: Option<Instant> = None;
        let status = loop {
            let event = match deadline {
                Some(at) => {
                    match inbox.recv_timeout(at.saturating_duration_since(Instant::now())) {
                        Ok(event) => Some(event),
                        Err(mpsc::RecvTimeoutError::Timeout) => None,
                        Err(mpsc::RecvTimeoutError::Disconnected) => {
                            bail!("the supervisor's event channel closed")
                        }
                    }
                }
                None => Some(
                    inbox
                        .recv()
                        .map_err(|_| anyhow!("the supervisor's event channel closed"))?,
                ),
            };
            match event {
                Some(Event::Exited(status)) => break status,
                // Only a restart delay is cut short; serve runs.
                Some(Event::Signal(libc::SIGUSR2)) => {}
                Some(Event::Signal(signal)) => {
                    let sent = if signal == libc::SIGUSR1 {
                        restart = true;
                        libc::SIGTERM
                    } else {
                        stop = Some(signal);
                        signal
                    };
                    note(
                        log,
                        &format!(
                            "received {}; sending {} to the interpreter (pid {pid})",
                            signal_name(signal),
                            signal_name(sent)
                        ),
                    );
                    if !exited.load(Ordering::SeqCst) {
                        unsafe {
                            libc::kill(pid, sent);
                        }
                    }
                    deadline.get_or_insert(Instant::now() + policy.patience);
                }
                None => {
                    note(
                        log,
                        &format!(
                            "the interpreter (pid {pid}) did not stop within {}; killing its process group",
                            human(policy.patience)
                        ),
                    );
                    unsafe {
                        libc::killpg(pid, libc::SIGKILL);
                    }
                    deadline = None;
                }
            }
        };
        let uptime = SystemTime::now().duration_since(started).ok();
        let (clean, described) = match &status {
            Ok(status) => (status.success(), status.to_string()),
            Err(error) => (false, format!("an unknown status ({error})")),
        };
        if !clean {
            reap_group(pid, policy.grace, log);
        }
        if let Some(signal) = stop {
            note(
                log,
                &format!(
                    "the interpreter (pid {pid}) exited ({described}) after {}; the supervisor exits",
                    signal_name(signal)
                ),
            );
            return Ok(());
        }
        if restart {
            note(
                log,
                &format!(
                    "the interpreter (pid {pid}) exited ({described}); restarting it as asked"
                ),
            );
            failures = 0;
            continue;
        }
        if clean {
            note(
                log,
                &format!(
                    "the interpreter (pid {pid}) stopped cleanly ({described}), so it is not restarted; the supervisor exits"
                ),
            );
            return Ok(());
        }
        failures = policy.failures_after(failures, uptime);
        let delay = policy.delay(failures);
        let exited = format!(
            "the interpreter (pid {pid}) exited: {described} after {}",
            uptime
                .map(human)
                .unwrap_or_else(|| "an unknown time (the clock went back)".into())
        );
        if held(&exited) {
            return Ok(());
        }
        note(
            log,
            &format!(
                "{exited}; restarting in {} ({failures} failure{} in a row)",
                human(delay),
                if failures == 1 { "" } else { "s" }
            ),
        );
        match pause(inbox, delay) {
            Pause::Elapsed => {}
            Pause::Now(signal) => {
                note(
                    log,
                    &format!(
                        "received {}; restarting the interpreter now",
                        signal_name(signal)
                    ),
                );
                failures = 0;
            }
            Pause::Stop(signal) => {
                note(
                    log,
                    &format!(
                        "received {} while waiting to restart; the supervisor exits",
                        signal_name(signal)
                    ),
                );
                return Ok(());
            }
        }
        if held("the restart delay ended") {
            return Ok(());
        }
    }
}

enum Pause {
    Elapsed,
    /// SIGUSR1 (`silicon service restart`) or SIGUSR2 (a start from `silicon connect`).
    Now(i32),
    Stop(i32),
}

/// Wait `delay` unless a signal ends the wait.
fn pause(inbox: &mpsc::Receiver<Event>, delay: Duration) -> Pause {
    let deadline = Instant::now() + delay;
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        match inbox.recv_timeout(left) {
            Ok(Event::Signal(signal @ (libc::SIGUSR1 | libc::SIGUSR2))) => {
                return Pause::Now(signal)
            }
            Ok(Event::Signal(signal)) => return Pause::Stop(signal),
            Ok(Event::Exited(_)) => {}
            Err(mpsc::RecvTimeoutError::Timeout) => return Pause::Elapsed,
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                thread::sleep(left);
                return Pause::Elapsed;
            }
        }
    }
}

fn spawn_serve(
    program: &Path,
    args: &[String],
    cwd: &Path,
    log: &Path,
) -> std::io::Result<std::process::Child> {
    rotate_log(log);
    let file = open_log(log)?;
    Command::new(program)
        .args(args)
        .current_dir(cwd)
        .env("SILICON_SERVICE", "run")
        .stdin(Stdio::null())
        .stdout(file.try_clone()?)
        .stderr(file)
        .process_group(0)
        .spawn()
}

/// Stop what a failed serve left in its process group: Linux has no process-group cleanup
/// of its own, and an orphaned Caddy would hold port 80 against every restart.
fn reap_group(group: i32, grace: Duration, log: &Path) {
    let alive = || unsafe { libc::killpg(group, 0) } == 0;
    if !alive() {
        return;
    }
    unsafe {
        libc::killpg(group, libc::SIGTERM);
    }
    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        if !alive() {
            note(
                log,
                &format!("stopped the processes the interpreter left in its process group {group} (SIGTERM)"),
            );
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }
    unsafe {
        libc::killpg(group, libc::SIGKILL);
    }
    note(
        log,
        &format!(
            "killed the processes the interpreter left in its process group {group} (SIGKILL {} after SIGTERM)",
            human(grace)
        ),
    );
}

/// One line per supervisor event in service.log.
fn note(log: &Path, text: &str) {
    rotate_log(log);
    let line = format!("{} silicon service run: {text}\n", Utc::now().to_rfc3339());
    if let Err(error) = open_log(log).and_then(|mut file| file.write_all(line.as_bytes())) {
        crate::stderr_line(&format!(
            "could not append to {}: {error}; the line was: {}",
            log.display(),
            line.trim_end()
        ));
    }
}

fn rotate_log(log: &Path) {
    if let Err(error) = crate::rotate(log, SERVICE_LOG_CAP, SERVICE_LOG_KEEP) {
        crate::stderr_line(&format!("{error:#}"));
    }
}

fn open_log(log: &Path) -> std::io::Result<File> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(log)
}

fn signal_name(signal: i32) -> String {
    match signal {
        libc::SIGTERM => "SIGTERM".into(),
        libc::SIGINT => "SIGINT".into(),
        libc::SIGHUP => "SIGHUP".into(),
        libc::SIGUSR1 => "SIGUSR1".into(),
        libc::SIGUSR2 => "SIGUSR2".into(),
        libc::SIGKILL => "SIGKILL".into(),
        other => format!("signal {other}"),
    }
}

fn human(duration: Duration) -> String {
    let seconds = duration.as_secs();
    if seconds == 0 {
        return format!("{}ms", duration.as_millis());
    }
    let (hours, minutes, seconds) = (seconds / 3600, seconds / 60 % 60, seconds % 60);
    match (hours, minutes) {
        (0, 0) => format!("{seconds}s"),
        (0, _) => format!("{minutes}m {seconds}s"),
        _ => format!("{hours}h {minutes}m"),
    }
}

/// Whether another process holds an exclusive flock on `path`.
fn held(path: &Path) -> Result<bool> {
    // Opened for writing where allowed: on NFS, flock is emulated with fcntl locks, and
    // an exclusive one needs a file open for writing.
    let file = match OpenOptions::new().read(true).write(true).open(path) {
        Err(error) if error.kind() == ErrorKind::PermissionDenied => {
            OpenOptions::new().read(true).open(path)
        }
        other => other,
    };
    let file = match file {
        Ok(file) => file,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error).with_context(|| format!("open {}", path.display())),
    };
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        // Released when `file` closes.
        return Ok(false);
    }
    let error = std::io::Error::last_os_error();
    if error.kind() == ErrorKind::WouldBlock {
        return Ok(true);
    }
    Err(error).with_context(|| format!("probe the lock on {}", path.display()))
}

/// An exclusive flock on `path`, or None when another process holds it.
fn try_lock(path: &Path) -> Result<Option<File>> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("open {}", path.display()))?;
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(Some(file));
    }
    let error = std::io::Error::last_os_error();
    if error.kind() == ErrorKind::WouldBlock {
        return Ok(None);
    }
    Err(error).with_context(|| format!("lock {}", path.display()))
}

fn record_path(dir: &Path) -> PathBuf {
    dir.join("service.json")
}

/// service.json; a file that does not parse is moved aside and treated as absent, so a
/// damaged record never keeps the interpreter from starting.
fn load(dir: &Path) -> Result<Option<Record>> {
    let path = record_path(dir);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
    };
    match serde_json::from_slice(&bytes) {
        Ok(record) => Ok(Some(record)),
        Err(error) => {
            let aside = dir.join(format!(
                "service.json.corrupt-{}",
                Utc::now().format("%Y%m%dT%H%M%S%.3fZ")
            ));
            fs::rename(&path, &aside).with_context(|| {
                format!(
                    "{} is not valid service state ({error}) and could not be moved to {}",
                    path.display(),
                    aside.display()
                )
            })?;
            crate::stderr_line(&format!(
                "{} was not valid service state ({error}); moved it to {} and treating autostart as not installed. It held:\n{}",
                path.display(),
                aside.display(),
                String::from_utf8_lossy(&bytes)
            ));
            Ok(None)
        }
    }
}

fn save(dir: &Path, record: &Record) -> Result<()> {
    state::write_json(&record_path(dir), record)
}

fn clear_stopped_in(dir: &Path) -> Result<()> {
    remove_file(&dir.join("stopped"))
}

fn remove_file(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Err(error) if error.kind() != ErrorKind::NotFound => {
            Err(error).with_context(|| format!("remove {}", path.display()))
        }
        _ => Ok(()),
    }
}

/// Write `text` to `path` atomically with `mode`: a crash leaves the old file or the new one.
fn write_file(path: &Path, text: &str, mode: u32) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("{} has no parent directory", path.display()))?;
    fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    let staged = parent.join(format!(".{}.tmp", uuid::Uuid::new_v4().simple()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(&staged)
            .with_context(|| format!("create {}", staged.display()))?;
        file.write_all(text.as_bytes())
            .and_then(|()| file.sync_all())
            .with_context(|| format!("write {}", staged.display()))?;
        // The umask may have narrowed the mode.
        fs::set_permissions(&staged, fs::Permissions::from_mode(mode))
            .with_context(|| format!("set the mode of {} to {mode:o}", staged.display()))?;
        fs::rename(&staged, path)
            .with_context(|| format!("rename {} to {}", staged.display(), path.display()))?;
        File::open(parent)
            .and_then(|directory| directory.sync_all())
            .with_context(|| format!("sync {}", parent.display()))
    })();
    if result.is_err() {
        let _ = fs::remove_file(&staged);
    }
    result
}

/// FNV-1a: stable across releases and platforms, unlike std's hashers.
fn hash(dir: &Path) -> String {
    let mut value: u32 = 0x811c_9dc5;
    for byte in dir.as_os_str().as_encoded_bytes() {
        value ^= u32::from(*byte);
        value = value.wrapping_mul(0x0100_0193);
    }
    format!("{value:08x}")
}

fn user_name(uid: u32) -> Option<String> {
    let mut buffer = vec![0 as libc::c_char; 16 * 1024];
    let mut entry: libc::passwd = unsafe { std::mem::zeroed() };
    let mut found = std::ptr::null_mut();
    let code = unsafe {
        libc::getpwuid_r(
            uid,
            &mut entry,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut found,
        )
    };
    if code != 0 || found.is_null() || entry.pw_name.is_null() {
        return None;
    }
    let name = unsafe { std::ffi::CStr::from_ptr(entry.pw_name) };
    Some(name.to_string_lossy().into_owned())
}

/// Identifies this boot, so a `silicon stop` holds until the next one.
fn boot_id() -> Option<String> {
    #[cfg(target_os = "linux")]
    {
        fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .ok()
            .map(|id| id.trim().to_owned())
            .filter(|id| !id.is_empty())
    }
    #[cfg(target_os = "macos")]
    {
        let name = std::ffi::CString::new("kern.bootsessionuuid").ok()?;
        let mut size = 0usize;
        if unsafe {
            libc::sysctlbyname(
                name.as_ptr(),
                std::ptr::null_mut(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        } != 0
        {
            return None;
        }
        let mut buffer = vec![0u8; size];
        if unsafe {
            libc::sysctlbyname(
                name.as_ptr(),
                buffer.as_mut_ptr().cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        } != 0
        {
            return None;
        }
        buffer.truncate(size);
        let id = String::from_utf8_lossy(&buffer)
            .trim_end_matches('\0')
            .trim()
            .to_owned();
        (!id.is_empty()).then_some(id)
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        None
    }
}

struct PlistInput {
    label: String,
    program: PathBuf,
    home: PathBuf,
    env: Vec<(String, String)>,
    log: PathBuf,
}

fn render_plist(input: &PlistInput) -> String {
    let string = |value: &str| format!("<string>{}</string>", xml(value));
    let path = |value: &Path| string(&value.to_string_lossy());
    let mut text = String::from(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n\
         <plist version=\"1.0\">\n<dict>\n",
    );
    text.push_str(&format!("\t<key>Label</key>\n\t{}\n", string(&input.label)));
    text.push_str(&format!(
        "\t<key>ProgramArguments</key>\n\t<array>\n\t\t{}\n\t\t{}\n\t</array>\n",
        path(&input.program),
        string("serve")
    ));
    text.push_str("\t<key>EnvironmentVariables</key>\n\t<dict>\n");
    for (name, value) in &input.env {
        text.push_str(&format!(
            "\t\t<key>{}</key>\n\t\t{}\n",
            xml(name),
            string(value)
        ));
    }
    text.push_str("\t</dict>\n");
    text.push_str(&format!(
        "\t<key>WorkingDirectory</key>\n\t{}\n",
        path(&input.home)
    ));
    text.push_str(
        "\t<key>RunAtLoad</key>\n\t<true/>\n\
         \t<key>KeepAlive</key>\n\t<dict>\n\t\t<key>SuccessfulExit</key>\n\t\t<false/>\n\t</dict>\n\
         \t<key>ThrottleInterval</key>\n\t<integer>30</integer>\n\
         \t<key>ExitTimeOut</key>\n\t<integer>90</integer>\n\
         \t<key>ProcessType</key>\n\t<string>Standard</string>\n\
         \t<key>SoftResourceLimits</key>\n\t<dict>\n\t\t<key>NumberOfFiles</key>\n\t\t<integer>8192</integer>\n\t</dict>\n",
    );
    text.push_str(&format!(
        "\t<key>StandardOutPath</key>\n\t{log}\n\t<key>StandardErrorPath</key>\n\t{log}\n",
        log = path(&input.log)
    ));
    text.push_str("</dict>\n</plist>\n");
    text
}

fn xml(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&apos;"),
            other => escaped.push(other),
        }
    }
    escaped
}

/// Whether `path` is a unit file this code wrote (every release starts it with [`UNIT_MARK`]).
fn written_by_silicon(path: &Path) -> bool {
    fs::read_to_string(path).is_ok_and(|text| text.starts_with(UNIT_MARK))
}

/// Whether two paths name one file: the same text, or one existing file reached through a
/// linked directory (a ~/.config that points into dotfiles, a home under two names).
fn same_file(a: &Path, b: &Path) -> bool {
    a == b
        || matches!(
            (fs::metadata(a), fs::metadata(b)),
            (Ok(a), Ok(b)) if a.dev() == b.dev() && a.ino() == b.ino()
        )
}

/// Remove a unit file this code wrote, and the link `systemctl enable` made to it beside
/// it, which a manager that never reads that directory would not remove.
fn remove_unit(path: &Path) -> Result<()> {
    let link = path
        .parent()
        .zip(path.file_name())
        .map(|(dir, name)| dir.join("default.target.wants").join(name))
        .filter(|link| {
            fs::symlink_metadata(link).is_ok_and(|meta| meta.file_type().is_symlink())
                && fs::canonicalize(path)
                    .is_ok_and(|unit| fs::canonicalize(link).is_ok_and(|target| target == unit))
        });
    remove_file(path)?;
    match link {
        Some(link) => remove_file(&link),
        None => Ok(()),
    }
}

fn render_unit(program: &Path, interpreter_home: Option<&Path>) -> String {
    let mut text = format!(
        "{UNIT_MARK}; `silicon service uninstall` removes it.\n\
         [Unit]\n\
         Description=Silicon interpreter\n\
         Documentation=https://docs.teamofsilicons.com\n\
         StartLimitIntervalSec=0\n\
         \n\
         [Service]\n\
         Type=simple\n",
    );
    text.push_str(&format!(
        "ExecStart={} serve\n",
        systemd_word(&program.to_string_lossy(), true)
    ));
    text.push_str("WorkingDirectory=%h\nEnvironment=SILICON_SERVICE=systemd\n");
    if let Some(dir) = interpreter_home {
        text.push_str(&format!(
            "Environment={}\n",
            systemd_word(
                &format!("SILICON_INTERPRETER_HOME={}", dir.to_string_lossy()),
                false
            )
        ));
    }
    text.push_str(
        "Restart=on-failure\n\
         RestartSec=10\n\
         KillMode=mixed\n\
         TimeoutStopSec=90\n\
         LimitNOFILE=65536\n\
         \n\
         [Install]\n\
         WantedBy=default.target\n",
    );
    text
}

/// One unit-file word: `%` specifiers escaped, `$` too on command lines, and quoted when
/// it holds whitespace, quotes, backslashes or `;`.
fn systemd_word(value: &str, command: bool) -> String {
    let mut word = value.replace('%', "%%");
    if command {
        word = word.replace('$', "$$");
    }
    if word
        .chars()
        .any(|character| character.is_whitespace() || matches!(character, '"' | '\'' | '\\' | ';'))
    {
        format!("\"{}\"", word.replace('\\', "\\\\").replace('"', "\\\""))
    } else {
        word
    }
}

fn environment_file(dir: &Path) -> PathBuf {
    dir.join("service.env")
}

fn captured(name: &str) -> bool {
    CAPTURED.contains(&name)
        || (CAPTURED_PREFIXES
            .iter()
            .any(|prefix| name.starts_with(prefix))
            && !NEVER_LOADED.contains(&name))
}

/// service.env text for `vars`: one NAME=value per line, sorted, values escaped.
fn capture(vars: &[(String, String)]) -> String {
    render_environment(
        vars.iter()
            .filter(|(name, _)| captured(name))
            .map(|(name, value)| (name.as_str(), value.as_str())),
    )
}

fn render_environment<'a>(entries: impl IntoIterator<Item = (&'a str, &'a str)>) -> String {
    let mut chosen: Vec<(&str, &str)> = entries.into_iter().collect();
    chosen.sort();
    let mut text = String::from(
        "# Environment for `silicon serve` under its service, from the terminal of `silicon service install` and of each `silicon connect` that starts it.\n\
         # One NAME=value per line; in values \\n is a newline, \\r a carriage return, \\\\ a backslash. A variable only this file has is kept.\n\
         # Edit it, then run `silicon service restart`. HOME is never taken from here.\n",
    );
    for (name, value) in chosen {
        text.push_str(&format!("{name}={}\n", escape(value)));
    }
    text
}

/// Write service.env from `vars`. An earlier file's variables that `vars` lacks stay (a
/// person may have added keys there for the unattended interpreter); where both have a
/// variable, this environment's value wins. What changed is named, never valued, and the
/// earlier file is kept as service.env.previous. A file whose variables would not change is
/// left exactly as it is, comments and all.
fn write_environment(dir: &Path, vars: &[(String, String)]) -> Result<Vec<String>> {
    state::private_dir(dir)?;
    let file = environment_file(dir);
    let before = match fs::read_to_string(&file) {
        Ok(text) => text,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            write_file(&file, &capture(vars), 0o600)?;
            return Ok(Vec::new());
        }
        Err(error) => return Err(error).with_context(|| format!("read {}", file.display())),
    };
    let fresh: BTreeMap<&str, &str> = vars
        .iter()
        .filter(|(name, _)| captured(name))
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    // The last line wins, as when it is loaded.
    let earlier: BTreeMap<String, String> = parse_environment(dir, &before, &mut Vec::new())
        .into_iter()
        .filter(|(name, _)| !NEVER_LOADED.contains(&name.as_str()))
        .collect();
    let mut merged = fresh.clone();
    let (mut kept, mut replaced) = (Vec::new(), Vec::new());
    for (name, value) in &earlier {
        match fresh.get(name.as_str()) {
            None => {
                merged.insert(name, value);
                kept.push(name.as_str());
            }
            Some(new) if new != value => replaced.push(name.as_str()),
            Some(_) => {}
        }
    }
    let added: Vec<&str> = fresh
        .keys()
        .copied()
        .filter(|name| !earlier.contains_key(*name))
        .collect();
    if replaced.is_empty() && added.is_empty() {
        return Ok(Vec::new());
    }
    let text = render_environment(merged);
    let previous = dir.join("service.env.previous");
    write_file(&previous, &before, 0o600)?;
    write_file(&file, &text, 0o600)?;
    let list = |names: &[&str]| {
        if names.is_empty() {
            "none".to_owned()
        } else {
            names.join(", ")
        }
    };
    Ok(vec![format!(
        "updated {} from this terminal's environment: kept from the earlier file {}; took this terminal's value for {}; added {}. The earlier file is {}",
        file.display(),
        list(&kept),
        list(&replaced),
        list(&added),
        previous.display()
    )])
}

fn escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}

fn unescape(value: &str) -> String {
    let mut text = String::with_capacity(value.len());
    let mut characters = value.chars();
    while let Some(character) = characters.next() {
        if character != '\\' {
            text.push(character);
            continue;
        }
        match characters.next() {
            Some('n') => text.push('\n'),
            Some('r') => text.push('\r'),
            Some('\\') => text.push('\\'),
            Some(other) => {
                text.push('\\');
                text.push(other);
            }
            None => text.push('\\'),
        }
    }
    text
}

/// service.env's variables in file order. Lines a person broke are reported, not fatal.
fn parse_environment(dir: &Path, text: &str, warnings: &mut Vec<String>) -> Vec<(String, String)> {
    let mut found = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        match line.split_once('=') {
            Some((name, value)) if valid_name(name) && !value.contains('\0') => {
                found.push((name.to_owned(), unescape(value)));
            }
            _ => warnings.push(failure::mask(
                dir,
                &format!(
                    "{} line {} is not NAME=value (or holds a NUL byte) and is ignored: {}",
                    environment_file(dir).display(),
                    index + 1,
                    line.replace('\0', "\\0")
                ),
                &[],
            )),
        }
    }
    found
}

fn valid_name(name: &str) -> bool {
    let mut characters = name.chars();
    characters
        .next()
        .is_some_and(|first| first == '_' || first.is_ascii_alphabetic())
        && characters.all(|character| character == '_' || character.is_ascii_alphanumeric())
}

/// PATH as service.env recorded it.
fn captured_path(dir: &Path) -> Option<String> {
    let text = fs::read_to_string(environment_file(dir)).ok()?;
    parse_environment(dir, &text, &mut Vec::new())
        .into_iter()
        .rev()
        .find(|(name, _)| name == "PATH")
        .map(|(_, value)| value)
}

/// The variables to set: service.env's (never HOME, SILICON_SERVICE or
/// SILICON_INTERPRETER_HOME), and PATH as the terminal had it, then what the supervisor
/// gave, then what the login shell adds.
fn plan_environment(
    loaded: &[(String, String)],
    current_path: &str,
    login_path: Option<&str>,
) -> Vec<(String, String)> {
    let mut plan: Vec<(String, String)> = loaded
        .iter()
        .filter(|(name, _)| name != "PATH" && !NEVER_LOADED.contains(&name.as_str()))
        .cloned()
        .collect();
    let recorded = loaded
        .iter()
        .rev()
        .find(|(name, _)| name == "PATH")
        .map(|(_, value)| value.as_str());
    let mut entries: Vec<&str> = Vec::new();
    for path in [recorded, Some(current_path), login_path]
        .into_iter()
        .flatten()
    {
        for entry in path.split(':') {
            if !entry.is_empty() && !entry.contains('\0') && !entries.contains(&entry) {
                entries.push(entry);
            }
        }
    }
    let path = entries.join(":");
    if path != current_path {
        plan.push(("PATH".to_owned(), path));
    }
    plan
}

/// The PATH `shell -l` builds (Homebrew, nvm, ~/.local/bin, ...). Profiles may print
/// banners, so the answer is the last line carrying the marker.
fn login_path(shell: &Path) -> Result<String> {
    const MARKER: &str = "SILICON_LOGIN_PATH=";
    let script = format!("printf '\\n{MARKER}%s\\n' \"$PATH\"");
    let args = ["-l", "-c", script.as_str()];
    let shown = failure::argv(shell, &args);
    let mut command = Command::new(shell);
    command.args(args);
    let output = crate::process::output_within(&mut command, LOGIN_SHELL_LIMIT)
        .map_err(|error| anyhow!("could not run `{shown}`: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    match stdout
        .lines()
        .rev()
        .find_map(|line| line.strip_prefix(MARKER))
    {
        Some(path) if output.status.success() => Ok(path.to_owned()),
        _ => Err(anyhow!("`{shown}` failed: {}", failure::describe(&output))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::os::unix::process::ExitStatusExt;
    use std::sync::Mutex;

    enum Answer {
        Exit(i32, String, String),
        Missing,
    }

    fn ok(stdout: &str) -> Answer {
        Answer::Exit(0, stdout.to_owned(), String::new())
    }

    fn fail(code: i32, stderr: &str) -> Answer {
        Answer::Exit(code, String::new(), stderr.to_owned())
    }

    /// Answers each expected command in order and records what ran, so tests see the
    /// exact launchctl/systemctl/loginctl/crontab sequence without touching the machine.
    #[derive(Default)]
    struct Script {
        answers: Mutex<VecDeque<(String, Answer)>>,
        env: Mutex<Vec<Vec<(String, String)>>>,
        detached: Mutex<Vec<String>>,
        cwd: Mutex<Vec<PathBuf>>,
        signals: Mutex<Vec<(i32, i32)>>,
        crontab: Mutex<Option<String>>,
    }

    impl Script {
        fn new(answers: Vec<(String, Answer)>) -> Self {
            Self {
                answers: Mutex::new(answers.into()),
                ..Self::default()
            }
        }

        fn finished(&self) {
            let left: Vec<String> = self
                .answers
                .lock()
                .unwrap()
                .iter()
                .map(|(line, _)| line.clone())
                .collect();
            assert!(left.is_empty(), "expected commands never ran: {left:#?}");
        }
    }

    impl Tools for Script {
        fn run(
            &self,
            program: &Path,
            args: &[String],
            env: &[(String, String)],
            _limit: Duration,
        ) -> std::io::Result<Output> {
            let line = shell_words::join(
                std::iter::once(program.to_string_lossy().into_owned()).chain(args.iter().cloned()),
            );
            // `crontab FILE` installs FILE: keep what it held.
            if program == Path::new("crontab") && args.len() == 1 && args[0] != "-l" {
                *self.crontab.lock().unwrap() = Some(fs::read_to_string(&args[0]).unwrap());
            }
            self.env.lock().unwrap().push(env.to_vec());
            let (expected, answer) = self
                .answers
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or_else(|| panic!("unexpected command: {line}"));
            let expected = expected.replace("{crontab}", &args.last().cloned().unwrap_or_default());
            assert_eq!(line, expected);
            match answer {
                Answer::Exit(code, stdout, stderr) => Ok(Output {
                    status: ExitStatus::from_raw(code << 8),
                    stdout: stdout.into_bytes(),
                    stderr: stderr.into_bytes(),
                }),
                Answer::Missing => Err(std::io::Error::from(ErrorKind::NotFound)),
            }
        }

        fn detach(
            &self,
            program: &Path,
            args: &[&str],
            cwd: &Path,
            _log: &Path,
        ) -> std::io::Result<u32> {
            self.detached
                .lock()
                .unwrap()
                .push(failure::argv(program, args));
            self.cwd.lock().unwrap().push(cwd.to_path_buf());
            Ok(4242)
        }

        fn signal(&self, pid: i32, signal: i32) -> std::io::Result<()> {
            self.signals.lock().unwrap().push((pid, signal));
            Ok(())
        }
    }

    fn host<'a>(tools: &'a Script, root: &Path, platform: Platform) -> Host<'a> {
        let home = root.join("home");
        let dir = home.join(".silicon-interpreter");
        fs::create_dir_all(&dir).unwrap();
        Host {
            tools,
            platform,
            dir: dir.clone(),
            home: home.clone(),
            prefix: Ok(root.join("prefix")),
            uid: 501,
            user: "ada".into(),
            xdg_config_home: None,
            bus: Vec::new(),
            hash: hash(&dir),
            custom: false,
            vars: [
                ("PATH", "/opt/homebrew/bin:/usr/bin:/bin"),
                ("HOME", "/somewhere/else"),
                ("LANG", "en_US.UTF-8"),
                ("ANTHROPIC_API_KEY", "sk-test\\with\nnewline"),
                ("SILICON_SERVICE", "launchd"),
                ("SILICON_AUTO_UPDATE", "0"),
                ("UNRELATED", "not captured"),
            ]
            .iter()
            .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
            .collect(),
            wsl_root: root.join("opt-silicon"),
            boot_id: Some("boot-1".into()),
            proc_root: root.join("proc"),
            poll: Duration::from_millis(1),
        }
    }

    fn line(text: impl AsRef<str>, answer: Answer) -> (String, Answer) {
        (text.as_ref().to_owned(), answer)
    }

    #[test]
    fn plist_golden_and_valid_for_launchd() {
        let text = render_plist(&PlistInput {
            label: LABEL.into(),
            program: "/Users/ada/.local/share/silicon/bin/silicon".into(),
            home: "/Users/ada".into(),
            env: vec![
                ("SILICON_SERVICE".into(), "launchd".into()),
                ("HOME".into(), "/Users/ada".into()),
                (
                    "SILICON_INTERPRETER_HOME".into(),
                    "/Users/ada/R&D <x>".into(),
                ),
                ("PATH".into(), "/opt/homebrew/bin:/usr/bin".into()),
            ],
            log: "/Users/ada/.silicon-interpreter/service.log".into(),
        });
        assert_eq!(
            text,
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.teamofsilicons.silicon</string>
	<key>ProgramArguments</key>
	<array>
		<string>/Users/ada/.local/share/silicon/bin/silicon</string>
		<string>serve</string>
	</array>
	<key>EnvironmentVariables</key>
	<dict>
		<key>SILICON_SERVICE</key>
		<string>launchd</string>
		<key>HOME</key>
		<string>/Users/ada</string>
		<key>SILICON_INTERPRETER_HOME</key>
		<string>/Users/ada/R&amp;D &lt;x&gt;</string>
		<key>PATH</key>
		<string>/opt/homebrew/bin:/usr/bin</string>
	</dict>
	<key>WorkingDirectory</key>
	<string>/Users/ada</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>ThrottleInterval</key>
	<integer>30</integer>
	<key>ExitTimeOut</key>
	<integer>90</integer>
	<key>ProcessType</key>
	<string>Standard</string>
	<key>SoftResourceLimits</key>
	<dict>
		<key>NumberOfFiles</key>
		<integer>8192</integer>
	</dict>
	<key>StandardOutPath</key>
	<string>/Users/ada/.silicon-interpreter/service.log</string>
	<key>StandardErrorPath</key>
	<string>/Users/ada/.silicon-interpreter/service.log</string>
</dict>
</plist>
"#
        );
        // plutil only reads the file; it is safe to run on a developer's Mac.
        let plutil = Path::new("/usr/bin/plutil");
        if plutil.exists() {
            let dir = tempfile::tempdir().unwrap();
            let file = dir.path().join("agent.plist");
            fs::write(&file, &text).unwrap();
            let output = Command::new(plutil)
                .arg("-lint")
                .arg(&file)
                .output()
                .unwrap();
            assert!(output.status.success(), "{}", failure::describe(&output));
        }
    }

    #[test]
    fn unit_golden_with_and_without_a_custom_interpreter_directory() {
        let program = Path::new("/home/ada/.local/share/silicon/bin/silicon");
        let expected =
            "# Written by `silicon service install`; `silicon service uninstall` removes it.
[Unit]
Description=Silicon interpreter
Documentation=https://docs.teamofsilicons.com
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/home/ada/.local/share/silicon/bin/silicon serve
WorkingDirectory=%h
Environment=SILICON_SERVICE=systemd
Restart=on-failure
RestartSec=10
KillMode=mixed
TimeoutStopSec=90
LimitNOFILE=65536

[Install]
WantedBy=default.target
";
        assert_eq!(render_unit(program, None), expected);
        let custom = render_unit(
            Path::new("/home/ada/my apps/100%/silicon/bin/silicon"),
            Some(Path::new("/srv/si \"x\"")),
        );
        assert!(
            custom.contains("ExecStart=\"/home/ada/my apps/100%%/silicon/bin/silicon\" serve\n"),
            "{custom}"
        );
        assert!(
            custom.contains("Environment=SILICON_SERVICE=systemd\nEnvironment=\"SILICON_INTERPRETER_HOME=/srv/si \\\"x\\\"\"\nRestart="),
            "{custom}"
        );
        assert_eq!(systemd_word("/a/$HOME", true), "/a/$$HOME");
    }

    #[test]
    fn a_custom_interpreter_directory_gets_its_own_stable_names() {
        assert_eq!(
            hash(Path::new("/Users/ada/custom")),
            hash(Path::new("/Users/ada/custom"))
        );
        assert_ne!(
            hash(Path::new("/Users/ada/custom")),
            hash(Path::new("/Users/ada/custom2"))
        );
        // FNV-1a of a fixed path never changes between releases.
        assert_eq!(hash(Path::new("")), "811c9dc5");
        assert_eq!(hash(Path::new("a")), "e40c292c");
        let root = tempfile::tempdir().unwrap();
        let script = Script::default();
        let mut host = host(&script, root.path(), Platform::Mac);
        assert_eq!(host.label(), LABEL);
        assert_eq!(host.unit(), "silicon.service");
        host.custom = true;
        let expected = format!("{LABEL}.{}", host.hash);
        assert_eq!(host.label(), expected);
        assert_eq!(host.hash.len(), 8);
        assert_eq!(host.unit(), format!("silicon-{}.service", host.hash));
        assert_eq!(
            host.plist(),
            host.home
                .join(format!("Library/LaunchAgents/{expected}.plist"))
        );
        let plist = host.render_plist(Path::new("/p"));
        assert!(plist.contains(&format!(
            "<key>SILICON_INTERPRETER_HOME</key>\n\t\t<string>{}</string>",
            host.dir.display()
        )));
        let cron = host.cron_lines(Path::new("/p 100%"));
        assert_eq!(
            cron[0],
            format!(
                "@reboot SILICON_INTERPRETER_HOME={} '/p 100\\%/bin/silicon' service ensure >/dev/null 2>&1 # silicon-service {}",
                shell_words::quote(&host.dir.to_string_lossy()),
                host.hash
            )
        );
        assert!(cron[1].starts_with("*/5 * * * * SILICON_INTERPRETER_HOME="));
    }

    #[test]
    fn crontab_merge_is_idempotent_and_keeps_foreign_lines() {
        let ours = vec![
            "@reboot /p/bin/silicon service ensure >/dev/null 2>&1 # silicon-service 1a2b3c4d"
                .to_owned(),
            "*/5 * * * * /p/bin/silicon service ensure >/dev/null 2>&1 # silicon-service 1a2b3c4d"
                .to_owned(),
        ];
        let tag = "# silicon-service 1a2b3c4d";
        let foreign = "MAILTO=\"\"\n0 3 * * * backup.sh\n@reboot other.sh # silicon-service 99999999\n# a comment";
        let once = merge_crontab(foreign, tag, &ours);
        assert_eq!(once, format!("{foreign}\n{}\n{}\n", ours[0], ours[1]));
        assert_eq!(merge_crontab(&once, tag, &ours), once);
        // Old entries of this directory are replaced, not stacked.
        let stale =
            format!("{foreign}\n@reboot /old/silicon service ensure # silicon-service 1a2b3c4d\n");
        assert_eq!(merge_crontab(&stale, tag, &ours), once);
        assert_eq!(
            merge_crontab("", tag, &ours),
            format!("{}\n{}\n", ours[0], ours[1])
        );
        // Removal leaves the rest exactly.
        assert_eq!(merge_crontab(&once, tag, &[]), format!("{foreign}\n"));
        assert_eq!(merge_crontab(&format!("{}\n", ours[0]), tag, &[]), "");
        let vixie = "# DO NOT EDIT THIS FILE - edit the master and reinstall.\n# (/tmp/crontab.x installed on Mon)\n# (Cron version V5.0)\n0 1 * * * a\n";
        assert_eq!(merge_crontab(vixie, tag, &[]), "0 1 * * * a\n");
    }

    #[test]
    fn service_env_round_trips_escapes_and_never_carries_home() {
        let vars: Vec<(String, String)> = [
            ("PATH", "/opt/homebrew/bin:/usr/bin"),
            ("HOME", "/elsewhere"),
            ("SILICON_SERVICE", "launchd"),
            ("SILICON_INTERPRETER_HOME", "/x"),
            ("SILICON_AUTO_UPDATE", "0"),
            ("OPENAI_API_KEY", "line one\nline two\\n not a newline\r"),
            ("SHELL", "/bin/zsh"),
            ("TERM", "xterm"),
            ("SI_TOKEN", "never"),
        ]
        .iter()
        .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
        .collect();
        let text = capture(&vars);
        assert!(
            text.lines().all(|line| !line.starts_with("HOME=")),
            "{text}"
        );
        for absent in [
            "SILICON_SERVICE=",
            "SILICON_INTERPRETER_HOME=",
            "TERM=",
            "SI_TOKEN=",
        ] {
            assert!(!text.contains(absent), "{absent} in {text}");
        }
        assert!(
            text.contains("OPENAI_API_KEY=line one\\nline two\\\\n not a newline\\r\n"),
            "{text}"
        );
        let dir = tempfile::tempdir().unwrap();
        let mut warnings = Vec::new();
        let edited = format!("{text}\n  # a note\nbroken line\nHOME=/evil\nNUL=a\0b\n");
        let loaded = parse_environment(dir.path(), &edited, &mut warnings);
        assert_eq!(
            loaded,
            vec![
                (
                    "OPENAI_API_KEY".to_owned(),
                    "line one\nline two\\n not a newline\r".to_owned()
                ),
                ("PATH".to_owned(), "/opt/homebrew/bin:/usr/bin".to_owned()),
                ("SHELL".to_owned(), "/bin/zsh".to_owned()),
                ("SILICON_AUTO_UPDATE".to_owned(), "0".to_owned()),
                ("HOME".to_owned(), "/evil".to_owned()),
            ]
        );
        assert_eq!(warnings.len(), 2, "{warnings:?}");
        assert!(
            warnings[1].contains(
                "line 12 is not NAME=value (or holds a NUL byte) and is ignored: NUL=a\\0b"
            ),
            "{warnings:?}"
        );
        assert!(
            warnings[0].contains(
                "line 10 is not NAME=value (or holds a NUL byte) and is ignored: broken line"
            ),
            "{warnings:?}"
        );
        let plan = plan_environment(
            &loaded,
            "/usr/bin:/bin",
            Some("/Users/ada/.local/bin:/usr/bin"),
        );
        assert!(plan.iter().all(|(name, _)| name != "HOME"));
        assert_eq!(
            plan.iter().find(|(name, _)| name == "PATH").unwrap().1,
            "/opt/homebrew/bin:/usr/bin:/bin:/Users/ada/.local/bin"
        );
        assert!(plan.contains(&("SILICON_AUTO_UPDATE".to_owned(), "0".to_owned())));
        // Nothing recorded and nothing new: PATH stays as the supervisor gave it.
        assert!(plan_environment(&[], "/usr/bin:/bin", Some("/bin")).is_empty());
    }

    #[test]
    fn the_login_shell_path_survives_profile_banners() {
        let dir = tempfile::tempdir().unwrap();
        let shell = dir.path().join("fake-shell");
        fs::write(
            &shell,
            "#!/bin/sh\n[ \"$1\" = -l ] || exit 7\necho 'Welcome banner'\nPATH=/opt/tools/bin:/usr/bin\nshift\nexec /bin/sh \"$@\"\n",
        )
        .unwrap();
        fs::set_permissions(&shell, fs::Permissions::from_mode(0o755)).unwrap();
        // A freshly written script can be briefly busy for exec while another test forks.
        let path = (0..50)
            .find_map(|_| match login_path(&shell) {
                Ok(path) => Some(path),
                Err(error) if format!("{error:#}").contains("busy") => {
                    thread::sleep(Duration::from_millis(20));
                    None
                }
                Err(error) => panic!("{error:#}"),
            })
            .unwrap();
        assert_eq!(path, "/opt/tools/bin:/usr/bin");
        let broken = dir.path().join("broken-shell");
        fs::write(&broken, "#!/bin/sh\necho 'no path here' >&2\nexit 3\n").unwrap();
        fs::set_permissions(&broken, fs::Permissions::from_mode(0o755)).unwrap();
        let error = (0..50)
            .find_map(|_| match login_path(&broken) {
                Err(error) if format!("{error:#}").contains("busy") => {
                    thread::sleep(Duration::from_millis(20));
                    None
                }
                other => Some(other),
            })
            .unwrap()
            .unwrap_err();
        let error = format!("{error:#}");
        assert!(
            error.contains("failed: exit status: 3") && error.contains("no path here"),
            "{error}"
        );
    }

    #[test]
    fn supervisor_backoff_doubles_to_five_minutes_and_resets_after_a_healthy_run() {
        let policy = Policy::standard();
        let delays: Vec<u64> = (1..=8).map(|n| policy.delay(n).as_secs()).collect();
        assert_eq!(delays, [5, 10, 20, 40, 80, 160, 300, 300]);
        assert_eq!(policy.delay(u32::MAX), Duration::from_secs(300));
        assert_eq!(policy.failures_after(3, Some(Duration::from_secs(30))), 4);
        assert_eq!(policy.failures_after(6, Some(Duration::from_secs(600))), 1);
        // The clock went back: the run counts as long ago.
        assert_eq!(policy.failures_after(6, None), 1);
        assert_eq!(
            policy.failures_after(u32::MAX, Some(Duration::ZERO)),
            u32::MAX
        );
        assert_eq!(human(Duration::from_millis(250)), "250ms");
        assert_eq!(human(Duration::from_secs(125)), "2m 5s");
        assert_eq!(human(Duration::from_secs(3 * 3600 + 120)), "3h 2m");
    }

    fn quick() -> Policy {
        Policy {
            base: Duration::from_millis(10),
            cap: Duration::from_millis(40),
            healthy: Duration::from_secs(600),
            grace: Duration::from_secs(2),
            patience: Duration::from_millis(500),
        }
    }

    /// `DIR/stopped`, read as during boot `boot-1`.
    fn test_marker(dir: &Path) -> Marker {
        Marker::new(dir.join("stopped"), Some("boot-1".into()))
    }

    /// supervise() on its own thread with a fake serve: `/bin/sh SCRIPT DIR`.
    fn supervised(
        script: &str,
        dir: &Path,
        policy: Policy,
    ) -> (mpsc::Sender<Event>, thread::JoinHandle<Result<()>>) {
        let file = dir.join("serve.sh");
        fs::write(&file, script).unwrap();
        let (events, inbox) = mpsc::channel();
        let sender = events.clone();
        let log = dir.join("service.log");
        let args = vec![
            file.to_string_lossy().into_owned(),
            dir.to_string_lossy().into_owned(),
        ];
        let cwd = dir.to_path_buf();
        let marker = test_marker(dir);
        let handle = thread::spawn(move || {
            supervise(
                Path::new("/bin/sh"),
                &args,
                &cwd,
                &log,
                &marker,
                &policy,
                &events,
                &inbox,
            )
        });
        (sender, handle)
    }

    fn wait_for(path: &Path) {
        let deadline = Instant::now() + Duration::from_secs(20);
        while !path.exists() {
            assert!(
                Instant::now() < deadline,
                "{} never appeared",
                path.display()
            );
            thread::sleep(Duration::from_millis(20));
        }
    }

    fn finish(handle: thread::JoinHandle<Result<()>>) {
        let deadline = Instant::now() + Duration::from_secs(30);
        while !handle.is_finished() {
            assert!(Instant::now() < deadline, "the supervisor did not return");
            thread::sleep(Duration::from_millis(20));
        }
        handle.join().unwrap().unwrap();
    }

    #[test]
    fn the_supervisor_restarts_a_crashing_serve_and_stops_after_a_clean_exit() {
        let dir = tempfile::tempdir().unwrap();
        // Runs 1 and 2 fail (the first leaves a child in its group), run 3 stops cleanly.
        let script = r#"
n=$(cat "$1/runs" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$1/runs"
[ "$SILICON_SERVICE" = run ] || exit 9
[ "$2" = serve ] || exit 8
pwd -P > "$1/cwd"
if [ $n = 1 ]; then sleep 60 & echo $! > "$1/orphan"; fi
[ $n -ge 3 ] && exit 0
echo "crash $n" >&2
exit 1
"#;
        let file = dir.path().join("serve.sh");
        fs::write(&file, script).unwrap();
        let (events, inbox) = mpsc::channel();
        let log = dir.path().join("service.log");
        let args = vec![
            file.to_string_lossy().into_owned(),
            dir.path().to_string_lossy().into_owned(),
            "serve".to_owned(),
        ];
        // Serve runs in the home, never in the directory the supervisor was started from.
        let home = dir.path().join("home");
        fs::create_dir(&home).unwrap();
        supervise(
            Path::new("/bin/sh"),
            &args,
            &home,
            &log,
            &test_marker(dir.path()),
            &quick(),
            &events,
            &inbox,
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(dir.path().join("runs")).unwrap().trim(),
            "3"
        );
        assert_eq!(
            fs::read_to_string(dir.path().join("cwd")).unwrap().trim(),
            home.canonicalize().unwrap().to_string_lossy()
        );
        let orphan: i32 = fs::read_to_string(dir.path().join("orphan"))
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        // Dead once it is gone or a zombie waiting for init to reap it.
        let running = || {
            let exists = unsafe { libc::kill(orphan, 0) } == 0;
            exists
                && !fs::read_to_string(format!("/proc/{orphan}/stat"))
                    .is_ok_and(|stat| stat.contains(") Z "))
        };
        let deadline = Instant::now() + Duration::from_secs(10);
        while running() {
            assert!(
                Instant::now() < deadline,
                "the orphaned child {orphan} survived"
            );
            thread::sleep(Duration::from_millis(20));
        }
        let text = fs::read_to_string(&log).unwrap();
        assert!(
            text.contains("crash 1") && text.contains("crash 2"),
            "{text}"
        );
        assert!(text.contains("exited: exit status: 1 after"), "{text}");
        assert!(
            text.contains("restarting in 10ms (1 failure in a row)"),
            "{text}"
        );
        assert!(
            text.contains("restarting in 20ms (2 failures in a row)"),
            "{text}"
        );
        assert!(
            text.contains("stopped the processes the interpreter left in its process group")
                || text.contains("killed the processes the interpreter left in its process group"),
            "{text}"
        );
        assert!(
            text.contains("stopped cleanly (exit status: 0), so it is not restarted"),
            "{text}"
        );
    }

    #[test]
    fn the_supervisor_forwards_stop_signals_and_restarts_on_request() {
        let dir = tempfile::tempdir().unwrap();
        let script = r#"
n=$(cat "$1/runs" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$1/runs"
trap 'echo "stopping $n" >&2; exit 0' TERM
touch "$1/ready-$n"
while :; do sleep 0.05; done
"#;
        let (events, handle) = supervised(script, dir.path(), quick());
        wait_for(&dir.path().join("ready-1"));
        events.send(Event::Signal(libc::SIGUSR1)).unwrap();
        wait_for(&dir.path().join("ready-2"));
        events.send(Event::Signal(libc::SIGTERM)).unwrap();
        finish(handle);
        assert_eq!(
            fs::read_to_string(dir.path().join("runs")).unwrap().trim(),
            "2"
        );
        let text = fs::read_to_string(dir.path().join("service.log")).unwrap();
        assert!(text.contains("received SIGUSR1; sending SIGTERM"), "{text}");
        assert!(text.contains("restarting it as asked"), "{text}");
        assert!(
            text.contains("after SIGTERM; the supervisor exits"),
            "{text}"
        );
    }

    #[test]
    fn a_serve_that_ignores_the_stop_is_killed_after_its_patience() {
        let dir = tempfile::tempdir().unwrap();
        let script = "trap '' TERM\ntouch \"$1/ready\"\nwhile :; do sleep 0.05; done\n";
        let (events, handle) = supervised(script, dir.path(), quick());
        wait_for(&dir.path().join("ready"));
        let started = Instant::now();
        events.send(Event::Signal(libc::SIGTERM)).unwrap();
        finish(handle);
        assert!(started.elapsed() < Duration::from_secs(10));
        let text = fs::read_to_string(dir.path().join("service.log")).unwrap();
        assert!(
            text.contains("did not stop within 500ms; killing its process group"),
            "{text}"
        );
    }

    #[test]
    fn ensure_honours_the_stop_marker_and_held_locks() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::default();
        let mut host = host(&script, root.path(), Platform::Linux);
        let marker = |boot: &str| {
            state::write_json(
                &host_dir(root.path()).join("stopped"),
                &Stopped {
                    boot_id: Some(boot.into()),
                    at: Utc::now(),
                },
            )
            .unwrap()
        };
        marker("boot-1");
        assert!(matches!(ensure(&host).unwrap(), Ensured::Stopped(_)));
        // The next boot starts it again.
        host.boot_id = Some("boot-2".into());
        let lock = try_lock(&host.dir.join("daemon.lock")).unwrap().unwrap();
        assert_eq!(
            ensure(&host).unwrap(),
            Ensured::Running(host.dir.join("daemon.lock"))
        );
        drop(lock);
        released(&host.dir.join("daemon.lock"));
        let lock = try_lock(&host.dir.join("supervisor.lock"))
            .unwrap()
            .unwrap();
        assert_eq!(
            ensure(&host).unwrap(),
            Ensured::Running(host.dir.join("supervisor.lock"))
        );
        drop(lock);
        released(&host.dir.join("supervisor.lock"));
        assert_eq!(ensure(&host).unwrap(), Ensured::Started(4242));
        assert_eq!(
            script.detached.lock().unwrap().as_slice(),
            ["silicon service run".to_owned()]
        );
        // In the home, not in whatever directory connect or cron ran from.
        assert_eq!(script.cwd.lock().unwrap().as_slice(), [host.home.clone()]);
        // An unreadable boot id never keeps a marker in force.
        host.boot_id = None;
        marker("boot-1");
        assert_eq!(ensure(&host).unwrap(), Ensured::Started(4242));
        fs::write(host.dir.join("stopped"), "{broken").unwrap();
        assert_eq!(ensure(&host).unwrap(), Ensured::Started(4242));
        save(
            &host.dir,
            &Record {
                mechanism: "launchd".into(),
                ..Record::default()
            },
        )
        .unwrap();
        assert_eq!(ensure(&host).unwrap(), Ensured::Other("launchd".into()));
        save(
            &host.dir,
            &Record {
                mechanism: "run".into(),
                declined: true,
                ..Record::default()
            },
        )
        .unwrap();
        assert_eq!(ensure(&host).unwrap(), Ensured::Declined);
    }

    /// A flock is shared with any child another test forked before its exec, so a
    /// release can take a moment to show.
    fn released(path: &Path) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while held(path).unwrap() {
            assert!(
                Instant::now() < deadline,
                "{} stayed locked",
                path.display()
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn host_dir(root: &Path) -> PathBuf {
        root.join("home/.silicon-interpreter")
    }

    #[test]
    fn launchd_install_writes_the_agent_and_bootstraps_it() {
        let root = tempfile::tempdir().unwrap();
        let dir = host_dir(root.path());
        let plist = root
            .path()
            .join(format!("home/Library/LaunchAgents/{LABEL}.plist"));
        let script = Script::new(vec![
            line(
                format!("launchctl bootout gui/501/{LABEL}"),
                fail(3, "Boot-out failed: 3: No such process"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(
                format!("launchctl bootstrap gui/501 {}", plist.display()),
                ok(""),
            ),
        ]);
        let host = host(&script, root.path(), Platform::Mac);
        fs::write(dir.join("stopped"), "{}").unwrap();
        let report = install(&host).unwrap();
        script.finished();
        assert!(
            report.summary.starts_with(&format!(
                "autostart installed: launchd agent {LABEL} ({}); the interpreter starts at login",
                plist.display()
            )),
            "{}",
            report.summary
        );
        assert!(!dir.join("stopped").exists());
        let text = fs::read_to_string(&plist).unwrap();
        assert_eq!(
            fs::metadata(&plist).unwrap().permissions().mode() & 0o777,
            0o644
        );
        assert!(text.contains(&format!(
            "<string>{}</string>",
            root.path().join("prefix/bin/silicon").display()
        )));
        assert!(
            text.contains("<key>PATH</key>\n\t\t<string>/opt/homebrew/bin:/usr/bin:/bin</string>")
        );
        // Credentials never enter the world-readable plist.
        assert!(
            !text.contains("sk-test") && !text.contains("ANTHROPIC"),
            "{text}"
        );
        let env = dir.join("service.env");
        assert_eq!(
            fs::metadata(&env).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let env = fs::read_to_string(env).unwrap();
        assert!(
            env.contains("ANTHROPIC_API_KEY=sk-test\\\\with\\nnewline\n"),
            "{env}"
        );
        assert!(
            env.contains("SILICON_AUTO_UPDATE=0\n")
                && !env.contains("HOME=")
                && !env.contains("UNRELATED"),
            "{env}"
        );
        let record = load(&dir).unwrap().unwrap();
        assert_eq!(record.mechanism, "launchd");
        assert_eq!(record.definition.as_deref(), Some(plist.as_path()));
        assert_eq!(record.installed_by, VERSION);
        // Unchanged and loaded: it is kept running, not restarted.
        let again = Script::new(vec![
            line(
                format!("launchctl print gui/501/{LABEL}"),
                ok("gui/501/x = {\n\tstate = running\n}"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(format!("launchctl kickstart gui/501/{LABEL}"), ok("")),
        ]);
        let host = super::tests::host(&again, root.path(), Platform::Mac);
        install(&host).unwrap();
        again.finished();
    }

    #[test]
    fn launchd_without_a_gui_login_keeps_the_agent_for_the_next_login() {
        let root = tempfile::tempdir().unwrap();
        let plist = root
            .path()
            .join(format!("home/Library/LaunchAgents/{LABEL}.plist"));
        let script = Script::new(vec![
            line(
                format!("launchctl bootout gui/501/{LABEL}"),
                fail(
                    125,
                    "Boot-out failed: 125: Domain does not support specified action",
                ),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(
                format!("launchctl bootstrap gui/501 {}", plist.display()),
                fail(
                    125,
                    "Bootstrap failed: 125: Domain does not support specified action",
                ),
            ),
        ]);
        let host = host(&script, root.path(), Platform::Mac);
        let report = install(&host).unwrap();
        script.finished();
        assert!(plist.exists());
        let notes = report.notes.join("\n");
        assert!(
            notes.contains("there is no GUI login for ada right now"),
            "{notes}"
        );
        assert!(
            notes.contains(&format!(
                "launchd loads {} at the next login",
                plist.display()
            )),
            "{notes}"
        );
        assert!(notes.contains(&format!("`launchctl bootstrap gui/501 {}` failed: exit status: 125\nstderr:\nBootstrap failed: 125: Domain does not support specified action", plist.display())), "{notes}");
        // start() says the same when there is still no GUI login.
        let script = Script::new(vec![
            line(
                format!("launchctl kickstart gui/501/{LABEL}"),
                fail(113, "Could not find service"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(
                format!("launchctl bootstrap gui/501 {}", plist.display()),
                fail(
                    125,
                    "Bootstrap failed: 125: Domain does not support specified action",
                ),
            ),
        ]);
        let host = super::tests::host(&script, root.path(), Platform::Mac);
        let record = load(&host.dir).unwrap().unwrap();
        let error = format!("{:#}", start(&host, &record).unwrap_err());
        script.finished();
        assert!(error.contains("no GUI login"), "{error}");
    }

    #[test]
    fn launchd_failures_are_reported_in_full() {
        let root = tempfile::tempdir().unwrap();
        let plist = root
            .path()
            .join(format!("home/Library/LaunchAgents/{LABEL}.plist"));
        let script = Script::new(vec![
            line(
                format!("launchctl bootout gui/501/{LABEL}"),
                fail(113, "Could not find specified service"),
            ),
            line(
                format!("launchctl enable gui/501/{LABEL}"),
                fail(1, "enable refused"),
            ),
            line(
                format!("launchctl bootstrap gui/501 {}", plist.display()),
                Answer::Exit(
                    5,
                    "details on stdout".into(),
                    "Bootstrap failed: 5: Input/output error".into(),
                ),
            ),
        ]);
        let host = host(&script, root.path(), Platform::Mac);
        let error = format!("{:#}", install(&host).unwrap_err());
        script.finished();
        assert!(error.contains(&format!(
            "`launchctl bootstrap gui/501 {}` failed: exit status: 5\nstderr:\nBootstrap failed: 5: Input/output error\nstdout:\ndetails on stdout",
            plist.display()
        )), "{error}");
        assert!(error.contains("also: `launchctl enable gui/501/com.teamofsilicons.silicon` failed: exit status: 1\nstderr:\nenable refused"), "{error}");
        assert!(
            !error.contains("Could not find specified service"),
            "the expected bootout answer is not a failure: {error}"
        );
        // The definition is recorded, so uninstall knows to remove it.
        assert_eq!(load(&host.dir).unwrap().unwrap().mechanism, "launchd");
    }

    #[test]
    fn launchd_start_loads_an_agent_that_is_not_loaded() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![
            line(
                format!("launchctl kickstart gui/501/{LABEL}"),
                fail(113, "Could not find service"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(
                format!(
                    "launchctl bootstrap gui/501 {}",
                    root.path()
                        .join(format!("home/Library/LaunchAgents/{LABEL}.plist"))
                        .display()
                ),
                ok(""),
            ),
            line(format!("launchctl kickstart gui/501/{LABEL}"), ok("")),
        ]);
        let host = host(&script, root.path(), Platform::Mac);
        let record = Record {
            mechanism: "launchd".into(),
            label: LABEL.into(),
            ..Record::default()
        };
        start(&host, &record).unwrap();
        script.finished();
        // The missing plist was written again before loading it.
        assert!(host.plist().exists());
    }

    #[test]
    fn uninstall_records_declined_and_connect_then_leaves_it_alone() {
        let root = tempfile::tempdir().unwrap();
        let plist = root
            .path()
            .join(format!("home/Library/LaunchAgents/{LABEL}.plist"));
        fs::create_dir_all(plist.parent().unwrap()).unwrap();
        fs::write(&plist, "old").unwrap();
        let script = Script::new(vec![line(
            format!("launchctl bootout gui/501/{LABEL}"),
            fail(
                113,
                "Boot-out failed: 113: Could not find specified service",
            ),
        )]);
        let host = host(&script, root.path(), Platform::Mac);
        save(
            &host.dir,
            &Record {
                mechanism: "launchd".into(),
                definition: Some(plist.clone()),
                label: LABEL.into(),
                ..Record::default()
            },
        )
        .unwrap();
        let report = uninstall(&host).unwrap();
        script.finished();
        assert!(
            report.summary.starts_with("autostart is off"),
            "{}",
            report.summary
        );
        assert!(!plist.exists());
        let record = load(&host.dir).unwrap().unwrap();
        assert!(record.declined);
        // No launchctl call: connect never reinstalls a declined service.
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        let installed = load(&host.dir)
            .unwrap()
            .filter(|record| !record.declined && !record.mechanism.is_empty());
        assert!(installed.is_none());
    }

    #[test]
    fn connect_installs_once_and_never_for_an_unmanaged_build() {
        let root = tempfile::tempdir().unwrap();
        let plist = root
            .path()
            .join(format!("home/Library/LaunchAgents/{LABEL}.plist"));
        let script = Script::new(vec![
            line(
                format!("launchctl bootout gui/501/{LABEL}"),
                fail(3, "No such process"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(
                format!("launchctl bootstrap gui/501 {}", plist.display()),
                ok(""),
            ),
        ]);
        let mut host = host(&script, root.path(), Platform::Mac);
        host.prefix = Err("target/debug/silicon is not a managed bundle installation".into());
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        let error = format!("{:#}", install(&host).unwrap_err());
        assert!(
            error.starts_with("autostart needs a managed installation")
                && error.contains("SILICON_SERVICE=external")
                && error.contains("target/debug/silicon is not a managed"),
            "{error}"
        );
        host.prefix = Ok(root.path().join("prefix"));
        let note = ensure_for_connect_with(&host).unwrap().unwrap();
        assert!(
            note.starts_with("autostart installed: launchd agent"),
            "{note}"
        );
        // Installed now: the next connect does nothing.
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        script.finished();
    }

    #[test]
    fn systemd_install_enables_the_unit_and_explains_a_refused_linger() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![
            line("systemctl --user show-environment", ok("PATH=/usr/bin")),
            line("systemctl --user daemon-reload", ok("")),
            line("systemctl --user enable --now silicon.service", ok("")),
            line("loginctl show-user ada -p Linger", ok("Linger=no\n")),
            line(
                "loginctl enable-linger ada",
                fail(1, "Could not enable linger: Access denied"),
            ),
        ]);
        let mut host = host(&script, root.path(), Platform::Linux);
        host.bus = vec![("XDG_RUNTIME_DIR".into(), "/run/user/501".into())];
        let report = install(&host).unwrap();
        script.finished();
        let unit = root
            .path()
            .join("home/.config/systemd/user/silicon.service");
        assert_eq!(report.definition.as_deref(), Some(unit.as_path()));
        assert!(fs::read_to_string(&unit).unwrap().contains(&format!(
            "ExecStart={} serve\n",
            root.path().join("prefix/bin/silicon").display()
        )));
        assert!(
            report.summary.contains("starts at login"),
            "{}",
            report.summary
        );
        let notes = report.notes.join("\n");
        assert!(notes.contains("run `sudo loginctl enable-linger ada`") && notes.contains("`loginctl enable-linger ada` failed: exit status: 1\nstderr:\nCould not enable linger: Access denied"), "{notes}");
        let record = load(&host.dir).unwrap().unwrap();
        assert_eq!(
            (
                record.mechanism.as_str(),
                record.label.as_str(),
                record.linger_enabled_by_silicon
            ),
            ("systemd", "silicon.service", false)
        );
        // systemctl reaches the user's bus even from a `su` shell.
        assert!(script.env.lock().unwrap()[0]
            .contains(&("XDG_RUNTIME_DIR".to_owned(), "/run/user/501".to_owned())));
    }

    #[test]
    fn systemd_linger_turned_on_by_silicon_is_reported_at_uninstall() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![
            line("systemctl --user show-environment", ok("")),
            line("systemctl --user daemon-reload", ok("")),
            line("systemctl --user enable --now silicon.service", ok("")),
            line(
                "loginctl show-user ada -p Linger",
                fail(
                    1,
                    "Failed to get user: User ID 501 is not logged in or lingering",
                ),
            ),
            line("loginctl enable-linger ada", ok("")),
        ]);
        let host = host(&script, root.path(), Platform::Linux);
        let report = install(&host).unwrap();
        assert!(
            report.summary.contains("starts at boot"),
            "{}",
            report.summary
        );
        script.finished();
        assert!(load(&host.dir).unwrap().unwrap().linger_enabled_by_silicon);
        let script = Script::new(vec![
            line("systemctl --user disable --now silicon.service", ok("")),
            line("systemctl --user daemon-reload", ok("")),
        ]);
        let host = super::tests::host(&script, root.path(), Platform::Linux);
        let report = uninstall(&host).unwrap();
        script.finished();
        assert!(!root
            .path()
            .join("home/.config/systemd/user/silicon.service")
            .exists());
        assert!(
            report
                .notes
                .join("\n")
                .contains("run `loginctl disable-linger ada`"),
            "{:?}",
            report.notes
        );
        let record = load(&host.dir).unwrap().unwrap();
        assert!(record.declined && record.linger_enabled_by_silicon);
        // systemd status and start go through systemctl --user.
        let script = Script::new(vec![line("systemctl --user start silicon.service", ok(""))]);
        let host = super::tests::host(&script, root.path(), Platform::Linux);
        let unit_record = Record {
            mechanism: "systemd".into(),
            label: "silicon.service".into(),
            ..Record::default()
        };
        let mut host = host;
        host.prefix = Err("unmanaged".into());
        start(&host, &unit_record).unwrap();
        script.finished();
    }

    #[test]
    fn cron_install_merges_the_crontab_and_starts_the_supervisor() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![
            line(
                "systemctl --user show-environment",
                fail(1, "Failed to connect to bus: No medium found"),
            ),
            line("crontab -l", ok("0 3 * * * backup.sh\n")),
            line("crontab {crontab}", ok("")),
        ]);
        let host = host(&script, root.path(), Platform::Linux);
        let report = install(&host).unwrap();
        script.finished();
        let installed = script.crontab.lock().unwrap().clone().unwrap();
        let silicon = root.path().join("prefix/bin/silicon");
        assert_eq!(
            installed,
            format!(
                "0 3 * * * backup.sh\n@reboot {s} service ensure >/dev/null 2>&1 # silicon-service {h}\n*/5 * * * * {s} service ensure >/dev/null 2>&1 # silicon-service {h}\n",
                s = shell_words::quote(&silicon.to_string_lossy()),
                h = host.hash
            )
        );
        let notes = report.notes.join("\n");
        assert!(notes.contains("using cron and the Silicon supervisor because there is no systemd user manager answers: `systemctl --user show-environment` failed: exit status: 1\nstderr:\nFailed to connect to bus: No medium found"), "{notes}");
        assert!(
            notes.contains("started the Silicon supervisor now (pid 4242)"),
            "{notes}"
        );
        assert_eq!(
            script.detached.lock().unwrap().as_slice(),
            ["silicon service run".to_owned()]
        );
        let record = load(&host.dir).unwrap().unwrap();
        assert_eq!(
            (record.mechanism.as_str(), record.definition.is_none()),
            ("run", true)
        );
        // Leftover staged crontab files are removed.
        assert!(fs::read_dir(&host.dir).unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".crontab")));
    }

    #[test]
    fn no_systemd_and_no_crontab_explains_external_supervision() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![
            line("systemctl --user show-environment", Answer::Missing),
            line("crontab -l", Answer::Missing),
        ]);
        let host = host(&script, root.path(), Platform::Linux);
        let error = format!("{:#}", install(&host).unwrap_err());
        script.finished();
        assert!(
            error.contains("neither a systemd user manager nor crontab")
                && error.contains("SILICON_SERVICE=external"),
            "{error}"
        );
        assert!(load(&host.dir).unwrap().is_none());
        // A "no crontab" answer is an empty crontab, not a failure.
        let empty = Output {
            status: ExitStatus::from_raw(1 << 8),
            stdout: Vec::new(),
            stderr: b"no crontab for ada\n".to_vec(),
        };
        assert!(empty_crontab(&empty));
    }

    #[test]
    fn privacy_protected_folders_are_named_with_full_paths() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        let interpreter = home.join(".silicon-interpreter");
        fs::create_dir_all(&interpreter).unwrap();
        fs::write(
            interpreter.join("connections.json"),
            serde_json::to_vec(&json!([
                {"id": "si:docs", "yaml": home.join("Documents/a/silicon.yaml"), "home": home.join("silicon/a"), "host": "a"},
                {"id": "si:fine", "yaml": home.join("silicon/b.yaml"), "home": home.join("silicon/b"), "host": "b"},
                {"id": "si:usb", "yaml": "/Volumes/USB/c.yaml", "home": home.join("Downloads/c"), "host": "c"},
            ]))
            .unwrap(),
        )
        .unwrap();
        let warnings = privacy_warnings(&home, &interpreter);
        assert_eq!(warnings.len(), 3, "{warnings:#?}");
        assert!(warnings[0].starts_with(&format!(
            "si:docs: its yaml {} is inside {}, which macOS privacy protection keeps from programs launchd starts",
            home.join("Documents/a/silicon.yaml").display(),
            home.join("Documents").display()
        )), "{warnings:#?}");
        assert!(
            warnings[1].starts_with("si:usb: its yaml /Volumes/USB/c.yaml is inside /Volumes"),
            "{warnings:#?}"
        );
        assert!(
            warnings[2].contains(&home.join("Downloads/c").display().to_string()),
            "{warnings:#?}"
        );
        fs::write(interpreter.join("connections.json"), "{").unwrap();
        assert!(privacy_warnings(&home, &interpreter)[0].contains("is not valid JSON"));
    }

    #[test]
    fn a_corrupt_record_is_moved_aside_and_treated_as_absent() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("service.json"), "{\"mechanism\": ").unwrap();
        assert_eq!(load(dir.path()).unwrap(), None);
        assert!(!dir.path().join("service.json").exists());
        let aside: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|name| name.starts_with("service.json.corrupt-"))
            .collect();
        assert_eq!(aside.len(), 1, "{aside:?}");
        // The marker provision.sh writes on Windows reads as an installed record.
        fs::write(dir.path().join("service.json"), WSL_MARKER).unwrap();
        let record = load(dir.path()).unwrap().unwrap();
        assert_eq!(
            (
                record.mechanism.as_str(),
                record.label.as_str(),
                record.declined
            ),
            ("windows-task", WSL_TASK, false)
        );
    }

    /// What provision.sh writes for `install.ps1` run by the user with this SID.
    const WSL_MARKER: &str =
        r#"{"mechanism":"windows-task","label":"\\Silicon Interpreter S-1-5-21-7-8-9-1001"}"#;
    const WSL_TASK: &str = r"\Silicon Interpreter S-1-5-21-7-8-9-1001";

    #[test]
    fn a_task_label_names_its_folder_and_name_for_powershell() {
        assert_eq!(
            task_query(WSL_TASK),
            r"-TaskPath '\' -TaskName 'Silicon Interpreter S-1-5-21-7-8-9-1001'"
        );
        assert_eq!(
            task_query(r"\Silicon\Interpreter"),
            r"-TaskPath '\Silicon\' -TaskName 'Interpreter'"
        );
        assert_eq!(task_query(r"\it's"), r"-TaskPath '\' -TaskName 'it''s'");
    }

    /// `powershell -Command SCRIPT` as the scripted runner shows it.
    fn powershell_line(powershell: &str, script: &str) -> String {
        format!(
            "{powershell} -NoLogo -NoProfile -NonInteractive -Command {}",
            shell_words::quote(script)
        )
    }

    fn start_task_script(label: &str) -> String {
        format!(
            "$ErrorActionPreference = 'Stop'; Start-ScheduledTask {}; New-Item -ItemType File -Force -Path (Join-Path $env:LOCALAPPDATA 'Silicon\\service-wake') | Out-Null",
            task_query(label)
        )
    }

    fn state_script(label: &str) -> String {
        format!(
            "Get-ScheduledTask {} | Select-Object -ExpandProperty State",
            task_query(label)
        )
    }

    #[test]
    fn wsl_defers_to_the_windows_logon_task() {
        let root = tempfile::tempdir().unwrap();
        let powershell = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe";
        fs::create_dir_all(root.path().join("opt-silicon")).unwrap();
        fs::write(
            root.path().join("opt-silicon/windows-powershell"),
            format!("{powershell}\n"),
        )
        .unwrap();
        // The exact quoting WSL interop hands powershell.exe.
        let state = format!("{powershell} -NoLogo -NoProfile -NonInteractive -Command 'Get-ScheduledTask -TaskPath '\\''\\'\\'' -TaskName '\\''Silicon Interpreter S-1-5-21-7-8-9-1001'\\'' | Select-Object -ExpandProperty State'");
        assert_eq!(state, powershell_line(powershell, &state_script(WSL_TASK)));
        let script = Script::new(vec![
            line(&state, ok("Ready\r\n")),
            line(
                powershell_line(powershell, &start_task_script(WSL_TASK)),
                ok(""),
            ),
            line(
                &state,
                fail(1, "Get-ScheduledTask : No MSFT_ScheduledTask objects found"),
            ),
        ]);
        let host = host(&script, root.path(), Platform::Wsl);
        let env = host.dir.join("service.env");
        // No marker from the Windows installer: connect says what to do, once per connect.
        let note = ensure_for_connect_with(&host).unwrap().unwrap();
        assert!(note.contains("rerun the Windows installer"), "{note}");
        assert!(!env.exists());
        // Marked but declined (install.ps1 -NoService): nothing to say or capture.
        fs::write(
            host.dir.join("service.json"),
            WSL_MARKER.replace('}', r#","declined":true}"#),
        )
        .unwrap();
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        assert!(!env.exists());
        fs::write(host.dir.join("service.json"), WSL_MARKER).unwrap();
        // The task's interpreter gets this terminal's settings through service.env, as
        // the interpreter this connect would have started got them through WSLENV.
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        let captured = fs::read_to_string(&env).unwrap();
        assert!(
            captured.contains("ANTHROPIC_API_KEY=") && captured.contains("SILICON_AUTO_UPDATE=0\n"),
            "{captured}"
        );
        assert_eq!(
            fs::metadata(&env).unwrap().permissions().mode() & 0o777,
            0o600
        );
        // Captured once: a later connect leaves it (and any edits) alone.
        fs::write(&env, "EDITED=1\n").unwrap();
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        assert_eq!(fs::read_to_string(&env).unwrap(), "EDITED=1\n");
        let report = install(&host).unwrap();
        assert!(
            report.summary.contains("rerun install.ps1")
                && report.notes.last().map(String::as_str)
                    == Some("the logon task \\Silicon Interpreter S-1-5-21-7-8-9-1001 is Ready")
                && report.label == WSL_TASK,
            "{report:?}"
        );
        // `service install` in the distribution captures this terminal again, keeping edits.
        assert!(
            report.notes[0].contains("kept from the earlier file EDITED"),
            "{report:?}"
        );
        let record = load(&host.dir).unwrap().unwrap();
        start(&host, &record).unwrap();
        let report = uninstall(&host).unwrap();
        assert!(
            report.summary.contains("install.ps1 -NoService"),
            "{report:?}"
        );
        assert!(
            report.notes[0].contains("No MSFT_ScheduledTask objects found"),
            "{report:?}"
        );
        script.finished();
        // Nothing Windows owns was recorded as declined here.
        assert!(!load(&host.dir).unwrap().unwrap().declined);
    }

    #[test]
    fn windows_restart_stops_the_interpreter_then_starts_the_task() {
        let root = tempfile::tempdir().unwrap();
        let powershell = "/mnt/c/powershell.exe";
        fs::create_dir_all(root.path().join("opt-silicon")).unwrap();
        fs::write(
            root.path().join("opt-silicon/windows-powershell"),
            powershell,
        )
        .unwrap();
        let command = |script: &str| powershell_line(powershell, script);
        let script = Script::new(vec![
            line(command(&state_script(WSL_TASK)), ok("Running\r\n")),
            line(command(&state_script(WSL_TASK)), ok("Ready\r\n")),
            line(command(&start_task_script(WSL_TASK)), ok("")),
        ]);
        let host = host(&script, root.path(), Platform::Wsl);
        save(
            &host.dir,
            &Record {
                mechanism: "windows-task".into(),
                label: WSL_TASK.into(),
                ..Record::default()
            },
        )
        .unwrap();
        let stopped = std::cell::Cell::new(false);
        let report = restart(&host, &|dir| {
            assert_eq!(dir, host.dir.as_path());
            stopped.set(true);
            Ok(true)
        })
        .unwrap();
        script.finished();
        assert!(stopped.get());
        assert_eq!(
            report.summary,
            format!("stopped the interpreter and started the logon task {WSL_TASK} again")
        );
        // A failed look after the stop still ends in a start, with the failure reported.
        let script = Script::new(vec![
            line(
                command(&state_script(WSL_TASK)),
                fail(1, "powershell.exe: interop timed out"),
            ),
            line(
                command(&format!("Stop-ScheduledTask {}", task_query(WSL_TASK))),
                ok(""),
            ),
            line(command(&start_task_script(WSL_TASK)), ok("")),
        ]);
        let host = super::tests::host(&script, root.path(), Platform::Wsl);
        let report = restart(&host, &|_| Ok(true)).unwrap();
        script.finished();
        assert!(report.notes[0].contains("interop timed out"), "{report:?}");
        // A start that fails too carries both failures.
        let script = Script::new(vec![
            line(
                command(&state_script(WSL_TASK)),
                fail(1, "first look failed"),
            ),
            line(
                command(&format!("Stop-ScheduledTask {}", task_query(WSL_TASK))),
                ok(""),
            ),
            line(
                command(&start_task_script(WSL_TASK)),
                fail(1, "start refused"),
            ),
        ]);
        let host = super::tests::host(&script, root.path(), Platform::Wsl);
        let error = format!("{:#}", restart(&host, &|_| Ok(true)).unwrap_err());
        script.finished();
        assert!(
            error.contains("start refused") && error.contains("first look failed"),
            "{error}"
        );
        // Nothing installed: restart says how to install instead of guessing.
        let other = tempfile::tempdir().unwrap();
        let none = Script::default();
        let error = format!(
            "{:#}",
            restart(
                &super::tests::host(&none, other.path(), Platform::Linux),
                &|_| Ok(false)
            )
            .unwrap_err()
        );
        assert!(
            error.contains("autostart is not installed")
                && error.contains("`silicon service install` installs it"),
            "{error}"
        );
    }

    #[test]
    fn launchd_status_shows_the_job_and_privacy_warnings() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![line(
            format!("launchctl print gui/501/{LABEL}"),
            ok("gui/501/com.teamofsilicons.silicon = {\n\tstate = running\n\tpid = 812\n\truns = 2\n\tlast exit code = 0\n}\n"),
        )]);
        let host = host(&script, root.path(), Platform::Mac);
        let plist = host.plist();
        save(
            &host.dir,
            &Record {
                mechanism: "launchd".into(),
                definition: Some(plist.clone()),
                label: LABEL.into(),
                ..Record::default()
            },
        )
        .unwrap();
        fs::write(
            host.dir.join("connections.json"),
            serde_json::to_vec(&json!([{"id": "si:a", "yaml": host.home.join("Desktop/a.yaml"), "home": host.home.join("a"), "host": "a"}])).unwrap(),
        )
        .unwrap();
        let status = status(&host).unwrap();
        script.finished();
        assert!(status.installed && !status.declined);
        assert_eq!(status.definition.as_deref(), Some(plist.as_path()));
        assert_eq!(status.details["loaded"], "yes");
        assert_eq!(status.details["pid"], "812");
        assert_eq!(status.details["definition present"], "false");
        assert_eq!(status.interpreter, "not running");
        assert!(
            status.notes[0].starts_with("si:a: its yaml"),
            "{:?}",
            status.notes
        );
        assert_eq!(status.logs[0], host.dir.join("service.log"));
    }

    #[test]
    fn restart_signals_the_supervisor_and_status_reports_it() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![line("crontab -l", ok(""))]);
        let host = host(&script, root.path(), Platform::Linux);
        save(
            &host.dir,
            &Record {
                mechanism: "run".into(),
                label: host.hash.clone(),
                ..Record::default()
            },
        )
        .unwrap();
        let mut lock = try_lock(&host.dir.join("supervisor.lock"))
            .unwrap()
            .unwrap();
        lock.write_all(b"31337").unwrap();
        let report = restart(&host, &|_| Ok(false)).unwrap();
        assert_eq!(
            report.summary,
            "asked the Silicon supervisor (pid 31337) to restart the interpreter"
        );
        assert_eq!(
            script.signals.lock().unwrap().as_slice(),
            [(31337, libc::SIGUSR1)]
        );
        let status = status(&host).unwrap();
        script.finished();
        assert!(status.installed);
        assert_eq!(status.details["supervisor"], "running (pid 31337)");
        assert_eq!(status.details["crontab lines"], "0");
        assert!(
            status.notes[0].contains("the crontab has no line tagged"),
            "{:?}",
            status.notes
        );
        drop(lock);
        released(&host.dir.join("supervisor.lock"));
        assert_eq!(supervisor(&host.dir).unwrap(), None);
    }

    #[test]
    fn launchd_status_reads_state_pid_and_last_exit() {
        let print = "gui/501/com.teamofsilicons.silicon = {\n\tactive count = 1\n\tstate = running\n\tprogram = /p/bin/silicon\n\targuments = {\n\t\tstate = nested\n\t}\n\truns = 4\n\tpid = 812\n\tlast exit code = 1\n}\n";
        let details = launchd_details(print);
        assert_eq!(details["state"], "running");
        assert_eq!(details["runs"], "4");
        assert_eq!(details["pid"], "812");
        assert_eq!(details["last exit code"], "1");
        assert_eq!(details.len(), 4);
        let shown = key_values("ActiveState=active\nSubState=running\nMainPID=77\nNRestarts=2\n");
        assert_eq!(shown["MainPID"], "77");
        assert_eq!(shown["NRestarts"], "2");
    }

    #[test]
    fn installs_wait_for_each_other() {
        let dir = tempfile::tempdir().unwrap();
        let first = changing(dir.path()).unwrap();
        let (done, finished) = mpsc::channel();
        let path = dir.path().to_path_buf();
        let second = thread::spawn(move || {
            let _serial = changing(&path).unwrap();
            done.send(()).unwrap();
        });
        assert!(finished.recv_timeout(Duration::from_millis(300)).is_err());
        drop(first);
        finished.recv_timeout(Duration::from_secs(10)).unwrap();
        second.join().unwrap();
    }

    #[test]
    fn stopped_marker_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        state::write_json(
            &dir.path().join("stopped"),
            &Stopped {
                boot_id: boot_id(),
                at: Utc::now(),
            },
        )
        .unwrap();
        clear_stopped_in(dir.path()).unwrap();
        assert!(!dir.path().join("stopped").exists());
        clear_stopped_in(dir.path()).unwrap();
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        assert!(boot_id().is_some_and(|id| !id.is_empty()));
    }

    #[test]
    fn connect_leaves_a_test_installation_alone() {
        let root = tempfile::tempdir().unwrap();
        // Any command would fail the test: nothing may run.
        let script = Script::default();
        let mut host = host(&script, root.path(), Platform::Mac);
        host.custom = true;
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        host.platform = Platform::Linux;
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        host.platform = Platform::Wsl;
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        assert!(load(&host.dir).unwrap().is_none());
        assert!(!host.dir.join("service.env").exists());
    }

    #[test]
    fn a_deleted_test_directory_drops_its_cron_lines_instead_of_coming_back() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("home");
        let gone = root.path().join("test-interpreter");
        assert!(vanished(&gone, true, Some(&home)));
        // The default directory is created again, and an existing one is used.
        assert!(!vanished(
            &home.join(".silicon-interpreter"),
            true,
            Some(&home)
        ));
        assert!(!vanished(&gone, false, Some(&home)));
        assert!(!vanished(root.path(), true, Some(&home)));
        let tag = format!("{TAG} {}", hash(&gone));
        let main = format!("{TAG} {}", hash(&home.join(".silicon-interpreter")));
        let crontab = format!(
            "0 3 * * * backup.sh\n@reboot SILICON_INTERPRETER_HOME=x /p/bin/silicon service ensure >/dev/null 2>&1 {tag}\n*/5 * * * * SILICON_INTERPRETER_HOME=x /p/bin/silicon service ensure >/dev/null 2>&1 {tag}\n@reboot /p/bin/silicon service ensure >/dev/null 2>&1 {main}\n"
        );
        let script = Script::new(vec![
            line("crontab -l", ok(&crontab)),
            line("crontab {crontab}", ok("")),
        ]);
        let outcome = forget_vanished(&script, &gone).unwrap();
        script.finished();
        assert_eq!(outcome, Ensured::Gone(gone.clone(), tag.clone()));
        assert_eq!(
            script.crontab.lock().unwrap().as_deref(),
            Some(
                format!("0 3 * * * backup.sh\n@reboot /p/bin/silicon service ensure >/dev/null 2>&1 {main}\n")
                    .as_str()
            )
        );
        assert!(!gone.exists());
        assert!(
            outcome.text().contains("no longer exists") && outcome.text().contains(&tag),
            "{}",
            outcome.text()
        );
        assert_eq!(outcome.value()["outcome"], "gone");
        // Nothing of its own left: the crontab is not rewritten.
        let script = Script::new(vec![line("crontab -l", ok("0 3 * * * backup.sh\n"))]);
        forget_vanished(&script, &gone).unwrap();
        script.finished();
    }

    #[test]
    fn installing_beside_an_unsupervised_interpreter_does_not_queue_a_second_one() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::new(vec![
            line(
                format!("launchctl print gui/501/{LABEL}"),
                fail(113, "Could not find service"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
        ]);
        let host = host(&script, root.path(), Platform::Mac);
        let lock = try_lock(&host.dir.join("daemon.lock")).unwrap().unwrap();
        let report = install(&host).unwrap();
        script.finished();
        assert!(host.plist().exists());
        assert_eq!(load(&host.dir).unwrap().unwrap().mechanism, "launchd");
        let notes = report.notes.join("\n");
        assert!(
            notes.contains("an interpreter that no supervisor started is running")
                && notes.contains("`silicon stop` stops that interpreter for good")
                && notes.contains("launchd loads the agent at the next login"),
            "{notes}"
        );
        // Loaded with this definition, so the lock holder is its own interpreter: it is
        // enabled, and a kickstart that could only queue another is skipped.
        let again = Script::new(vec![
            line(
                format!("launchctl print gui/501/{LABEL}"),
                ok("state = running"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
        ]);
        install(&super::tests::host(&again, root.path(), Platform::Mac)).unwrap();
        again.finished();
        // systemd: enabled for the next boot, not started now.
        let other = tempfile::tempdir().unwrap();
        let script = Script::new(vec![
            line("systemctl --user show-environment", ok("")),
            line("systemctl --user daemon-reload", ok("")),
            line(
                "systemctl --user is-active silicon.service",
                Answer::Exit(3, "inactive\n".into(), String::new()),
            ),
            line("systemctl --user enable silicon.service", ok("")),
            line("loginctl show-user ada -p Linger", ok("Linger=yes\n")),
        ]);
        let linux = super::tests::host(&script, other.path(), Platform::Linux);
        let held_too = try_lock(&linux.dir.join("daemon.lock")).unwrap().unwrap();
        let report = install(&linux).unwrap();
        script.finished();
        assert!(
            report
                .notes
                .join("\n")
                .contains("systemd starts the unit at the next boot (or login)"),
            "{report:?}"
        );
        // The unit's own interpreter holds the lock: enabled and started as usual.
        let script = Script::new(vec![
            line("systemctl --user show-environment", ok("")),
            line("systemctl --user daemon-reload", ok("")),
            line("systemctl --user is-active silicon.service", ok("active\n")),
            line("systemctl --user enable --now silicon.service", ok("")),
            line("loginctl show-user ada -p Linger", ok("Linger=yes\n")),
        ]);
        let report = install(&super::tests::host(&script, other.path(), Platform::Linux)).unwrap();
        script.finished();
        assert!(
            !report.notes.join("\n").contains("no supervisor started"),
            "{report:?}"
        );
        drop((lock, held_too));
    }

    #[test]
    fn launchd_reinstall_waits_for_the_old_job_to_unload() {
        let root = tempfile::tempdir().unwrap();
        let plist = root
            .path()
            .join(format!("home/Library/LaunchAgents/{LABEL}.plist"));
        fs::create_dir_all(plist.parent().unwrap()).unwrap();
        fs::write(&plist, "an older definition").unwrap();
        let bootstrap = format!("launchctl bootstrap gui/501 {}", plist.display());
        let script = Script::new(vec![
            line(
                format!("launchctl bootout gui/501/{LABEL}"),
                fail(36, "Boot-out failed: 36: Operation now in progress"),
            ),
            line(
                format!("launchctl print gui/501/{LABEL}"),
                ok("state = stopping"),
            ),
            line(
                format!("launchctl print gui/501/{LABEL}"),
                fail(113, "Could not find service"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(
                &bootstrap,
                fail(5, "Bootstrap failed: 5: Input/output error"),
            ),
            line(
                &bootstrap,
                fail(37, "Bootstrap failed: 37: Operation already in progress"),
            ),
            line(&bootstrap, ok("")),
        ]);
        let host = host(&script, root.path(), Platform::Mac);
        let report = install(&host).unwrap();
        script.finished();
        assert!(
            report.summary.starts_with("autostart installed"),
            "{report:?}"
        );
        assert_ne!(fs::read_to_string(&plist).unwrap(), "an older definition");
    }

    #[test]
    fn a_start_ends_the_supervisors_restart_delay() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::default();
        let host = host(&script, root.path(), Platform::Linux);
        let record = Record {
            mechanism: "run".into(),
            label: host.hash.clone(),
            ..Record::default()
        };
        save(&host.dir, &record).unwrap();
        let mut lock = try_lock(&host.dir.join("supervisor.lock"))
            .unwrap()
            .unwrap();
        lock.write_all(b"31337").unwrap();
        // The supervisor runs but no interpreter does: it is waiting to restart one.
        start(&host, &record).unwrap();
        assert_eq!(
            script.signals.lock().unwrap().as_slice(),
            [(31337, libc::SIGUSR2)]
        );
        // With the interpreter running there is nothing to wake.
        let daemon = try_lock(&host.dir.join("daemon.lock")).unwrap().unwrap();
        start(&host, &record).unwrap();
        assert_eq!(script.signals.lock().unwrap().len(), 1);
        assert!(script.detached.lock().unwrap().is_empty());
        drop((lock, daemon));
    }

    #[test]
    fn sigusr2_restarts_a_waiting_serve_now_and_leaves_a_running_one_alone() {
        let dir = tempfile::tempdir().unwrap();
        let script = r#"
n=$(cat "$1/runs" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$1/runs"
[ $n = 1 ] && exit 1
trap 'exit 0' TERM
touch "$1/ready-$n"
while :; do sleep 0.05; done
"#;
        let policy = Policy {
            base: Duration::from_secs(600),
            cap: Duration::from_secs(600),
            ..quick()
        };
        let (events, handle) = supervised(script, dir.path(), policy);
        let log = dir.path().join("service.log");
        let deadline = Instant::now() + Duration::from_secs(20);
        while !fs::read_to_string(&log).is_ok_and(|text| text.contains("restarting in 10m 0s")) {
            assert!(Instant::now() < deadline, "the first run never failed");
            thread::sleep(Duration::from_millis(20));
        }
        events.send(Event::Signal(libc::SIGUSR2)).unwrap();
        wait_for(&dir.path().join("ready-2"));
        // Serve runs: SIGUSR2 changes nothing.
        events.send(Event::Signal(libc::SIGUSR2)).unwrap();
        thread::sleep(Duration::from_millis(300));
        assert!(!dir.path().join("ready-3").exists());
        events.send(Event::Signal(libc::SIGTERM)).unwrap();
        finish(handle);
        assert_eq!(
            fs::read_to_string(dir.path().join("runs")).unwrap().trim(),
            "2"
        );
        let text = fs::read_to_string(&log).unwrap();
        assert!(
            text.contains("received SIGUSR2; restarting the interpreter now"),
            "{text}"
        );
    }

    /// A /proc with `(pid, comm, cmdline)` entries.
    fn fake_proc(root: &Path, entries: &[(&str, &str, &[u8])]) -> PathBuf {
        let proc = root.join("proc");
        for (pid, comm, cmdline) in entries {
            let entry = proc.join(pid);
            fs::create_dir_all(&entry).unwrap();
            fs::write(entry.join("comm"), format!("{comm}\n")).unwrap();
            fs::write(entry.join("cmdline"), cmdline).unwrap();
        }
        proc
    }

    #[test]
    fn cron_without_a_running_daemon_is_not_promised_at_boot() {
        let root = tempfile::tempdir().unwrap();
        assert_eq!(cron_running(&root.path().join("proc")), None);
        let proc = fake_proc(
            root.path(),
            &[
                ("1", "init", b"/sbin/init\0"),
                ("42", "bash", b"-bash\0"),
                ("self", "cron", b"cron\0"),
                ("77", "busybox", b"busybox\0sh\0"),
            ],
        );
        assert_eq!(cron_running(&proc), Some(false));
        fake_proc(
            root.path(),
            &[("78", "busybox", b"/bin/busybox\0crond\0-f\0")],
        );
        assert_eq!(cron_running(&proc), Some(true));
        fs::remove_dir_all(proc.join("78")).unwrap();
        fake_proc(root.path(), &[("80", "cron", b"/usr/sbin/cron\0-f\0")]);
        assert_eq!(cron_running(&proc), Some(true));
        fs::remove_dir_all(proc.join("80")).unwrap();
        // Another user's processes hidden (hidepid): no pid 1, so no answer.
        let hidden = tempfile::tempdir().unwrap();
        assert_eq!(
            cron_running(&fake_proc(hidden.path(), &[("4242", "bash", b"")])),
            None
        );
        let script = Script::new(vec![
            line(
                "systemctl --user show-environment",
                fail(
                    1,
                    "System has not been booted with systemd as init system (PID 1).",
                ),
            ),
            line("crontab -l", fail(1, "no crontab for ada")),
            line("crontab {crontab}", ok("")),
        ]);
        let mut host = host(&script, root.path(), Platform::Linux);
        host.proc_root = proc;
        let report = install(&host).unwrap();
        script.finished();
        assert!(
            report
                .summary
                .contains("the interpreter starts now (at boot only once a cron daemon runs)"),
            "{}",
            report.summary
        );
        assert!(
            report
                .notes
                .join("\n")
                .contains("no cron daemon is running (no cron or crond process in"),
            "{report:?}"
        );
    }

    #[test]
    fn service_install_keeps_what_a_person_added_to_service_env() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("service.env");
        let previous = dir.path().join("service.env.previous");
        let vars = |pairs: &[(&str, &str)]| -> Vec<(String, String)> {
            pairs
                .iter()
                .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
                .collect()
        };
        let first = vars(&[
            ("PATH", "/usr/bin"),
            ("ANTHROPIC_API_KEY", "sk-first"),
            ("SILICON_AUTO_UPDATE", "0"),
        ]);
        assert!(write_environment(dir.path(), &first).unwrap().is_empty());
        // The same environment again changes nothing and says nothing.
        assert!(write_environment(dir.path(), &first).unwrap().is_empty());
        assert!(!previous.exists());
        let written = fs::read_to_string(&file).unwrap();
        let edited = format!(
            "{written}OPENAI_API_KEY=sk-added-by-hand\nANTHROPIC_API_KEY=sk-edited\nHOME=/evil\n"
        );
        fs::write(&file, &edited).unwrap();
        let notes = write_environment(
            dir.path(),
            &vars(&[
                ("PATH", "/usr/bin"),
                ("ANTHROPIC_API_KEY", "sk-first"),
                ("LANG", "C.UTF-8"),
            ]),
        )
        .unwrap();
        let note = notes.join("\n");
        assert!(
            note.contains("kept from the earlier file OPENAI_API_KEY, SILICON_AUTO_UPDATE")
                && note.contains("took this terminal's value for ANTHROPIC_API_KEY")
                && note.contains("added LANG")
                && note.contains(&previous.display().to_string()),
            "{note}"
        );
        // Names only: no value reaches the note.
        assert!(!note.contains("sk-"), "{note}");
        let loaded = parse_environment(
            dir.path(),
            &fs::read_to_string(&file).unwrap(),
            &mut Vec::new(),
        );
        let loaded: BTreeMap<String, String> = loaded.into_iter().collect();
        assert_eq!(loaded["OPENAI_API_KEY"], "sk-added-by-hand");
        assert_eq!(loaded["ANTHROPIC_API_KEY"], "sk-first");
        assert_eq!(loaded["SILICON_AUTO_UPDATE"], "0");
        assert_eq!(loaded["LANG"], "C.UTF-8");
        assert!(!loaded.contains_key("HOME"));
        assert_eq!(fs::read_to_string(&previous).unwrap(), edited);
        for path in [&file, &previous] {
            assert_eq!(
                fs::metadata(path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn status_tells_a_stop_from_this_boot_from_an_earlier_one() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::default();
        let host = host(&script, root.path(), Platform::Linux);
        let marker = |boot: &str| {
            state::write_json(
                &host.dir.join("stopped"),
                &Stopped {
                    boot_id: Some(boot.into()),
                    at: Utc::now(),
                },
            )
            .unwrap()
        };
        marker("boot-1");
        assert_eq!(status(&host).unwrap().stopped.unwrap()["this_boot"], true);
        marker("boot-0");
        let stopped = status(&host).unwrap().stopped.unwrap();
        assert_eq!(stopped["this_boot"], false);
        assert_eq!(stopped["boot_id"], "boot-0");
        fs::write(host.dir.join("stopped"), "{broken").unwrap();
        let stopped = status(&host).unwrap().stopped.unwrap();
        assert_eq!(stopped["this_boot"], false);
        assert_eq!(stopped["text"], "{broken");
    }

    #[test]
    fn a_lock_file_this_user_cannot_write_is_still_probed() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("daemon.lock");
        let lock = try_lock(&path).unwrap().unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o400)).unwrap();
        assert!(held(&path).unwrap());
        drop(lock);
        released(&path);
        assert!(!held(&dir.path().join("absent.lock")).unwrap());
    }

    #[test]
    fn the_runner_runs_the_managed_serve_once_listening_and_only_once() {
        let root = tempfile::tempdir().unwrap();
        let script = Script::default();
        let host = host(&script, root.path(), Platform::Linux);
        let lock_path = host.dir.join("supervisor.lock");
        let out = root.path().join("serve.out");
        let bin = root.path().join("prefix/bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(
            bin.join("silicon"),
            format!(
                "#!/bin/sh\n{{ printf '%s %s\\n' \"$1\" \"$SILICON_SERVICE\"; pwd -P; cat {}; }} > {}\nexit 0\n",
                shell_words::quote(&lock_path.to_string_lossy()),
                shell_words::quote(&out.to_string_lossy())
            ),
        )
        .unwrap();
        fs::set_permissions(bin.join("silicon"), fs::Permissions::from_mode(0o755)).unwrap();
        let listening = std::cell::Cell::new(false);
        run_supervisor_with(&host, &quick(), |_events| {
            // The pid is published only once signals are handled.
            assert_eq!(fs::read_to_string(&lock_path).unwrap(), "");
            listening.set(true);
            Ok(Box::new(|| {}))
        })
        .unwrap();
        assert!(listening.get());
        assert_eq!(
            fs::read_to_string(&out).unwrap(),
            format!(
                "serve run\n{}\n{}",
                host.home.canonicalize().unwrap().display(),
                std::process::id()
            )
        );
        // A second runner while one holds the lock exits at once and runs nothing.
        released(&lock_path);
        fs::remove_file(&out).unwrap();
        let first = try_lock(&lock_path).unwrap().unwrap();
        run_supervisor_with(&host, &quick(), |_| -> Result<Unlisten> {
            panic!("a second runner must not listen or start serve")
        })
        .unwrap();
        assert!(!out.exists());
        drop(first);
    }

    #[test]
    fn a_connection_in_a_protected_folder_is_warned_about_before_launchd_runs_it() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("home");
        let dir = home.join(".silicon-interpreter");
        fs::create_dir_all(&dir).unwrap();
        let yaml = home.join("Documents/project/silicon.yaml");
        let project = home.join("silicon/project");
        assert!(connect_warnings_in(&dir, &home, &yaml, &project).is_empty());
        let mut record = Record {
            mechanism: "systemd".into(),
            ..Record::default()
        };
        save(&dir, &record).unwrap();
        assert!(connect_warnings_in(&dir, &home, &yaml, &project).is_empty());
        record.mechanism = "launchd".into();
        save(&dir, &record).unwrap();
        let warnings = connect_warnings_in(&dir, &home, &yaml, &project);
        assert_eq!(warnings.len(), 1, "{warnings:?}");
        assert!(
            warnings[0].starts_with(&format!(
                "this connection: its yaml {} is inside {}",
                yaml.display(),
                home.join("Documents").display()
            )),
            "{warnings:?}"
        );
        assert_eq!(
            connect_warnings_in(&dir, &home, &home.join("a.yaml"), &home.join("Desktop/x")).len(),
            1
        );
        record.declined = true;
        save(&dir, &record).unwrap();
        assert!(connect_warnings_in(&dir, &home, &yaml, &project).is_empty());
    }

    #[test]
    fn a_connect_that_starts_the_interpreter_brings_this_terminals_settings() {
        let vars = |pairs: &[(&str, &str)]| -> Vec<(String, String)> {
            pairs
                .iter()
                .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
                .collect()
        };
        let terminal = vars(&[
            ("PATH", "/usr/bin"),
            ("ANTHROPIC_API_KEY", "sk-rotated"),
            ("OPENAI_API_KEY", "sk-same"),
            ("UNRELATED", "not captured"),
        ]);
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("service.env");
        // No service, or one turned off: nothing is written.
        assert!(refresh_environment_in(dir.path(), &terminal)
            .unwrap()
            .is_empty());
        save(
            dir.path(),
            &Record {
                mechanism: "launchd".into(),
                declined: true,
                ..Record::default()
            },
        )
        .unwrap();
        assert!(refresh_environment_in(dir.path(), &terminal)
            .unwrap()
            .is_empty());
        assert!(!file.exists());
        // Installed with a revoked key, and a variable a person added by hand.
        save(
            dir.path(),
            &Record {
                mechanism: "launchd".into(),
                label: LABEL.into(),
                ..Record::default()
            },
        )
        .unwrap();
        let installed = vars(&[
            ("PATH", "/usr/bin"),
            ("ANTHROPIC_API_KEY", "sk-revoked"),
            ("OPENAI_API_KEY", "sk-same"),
        ]);
        assert!(write_environment(dir.path(), &installed)
            .unwrap()
            .is_empty());
        let by_hand = format!(
            "{}IAM_EXTRA=kept by hand\n",
            fs::read_to_string(&file).unwrap()
        );
        fs::write(&file, &by_hand).unwrap();
        let notes = refresh_environment_in(dir.path(), &terminal).unwrap();
        let note = notes.join("\n");
        assert!(
            note.contains("kept from the earlier file IAM_EXTRA")
                && note.contains("took this terminal's value for ANTHROPIC_API_KEY;")
                && note.contains("added none"),
            "{note}"
        );
        assert!(!note.contains("sk-"), "{note}");
        let loaded: BTreeMap<String, String> = parse_environment(
            dir.path(),
            &fs::read_to_string(&file).unwrap(),
            &mut Vec::new(),
        )
        .into_iter()
        .collect();
        assert_eq!(loaded["ANTHROPIC_API_KEY"], "sk-rotated");
        assert_eq!(loaded["IAM_EXTRA"], "kept by hand");
        assert!(!loaded.contains_key("UNRELATED"));
        assert_eq!(
            fs::read_to_string(dir.path().join("service.env.previous")).unwrap(),
            by_hand
        );
        // The same terminal again: nothing to say, and the file is left as it is, even
        // one an older release wrote with another header or a person commented.
        let commented = format!(
            "# written by an older release\n{}# a person's note\n",
            fs::read_to_string(&file).unwrap()
        );
        fs::write(&file, &commented).unwrap();
        assert!(refresh_environment_in(dir.path(), &terminal)
            .unwrap()
            .is_empty());
        assert_eq!(fs::read_to_string(&file).unwrap(), commented);
        // On Windows the logon task's interpreter reads service.env in the distribution.
        let wsl = tempfile::tempdir().unwrap();
        fs::write(wsl.path().join("service.json"), WSL_MARKER).unwrap();
        assert!(refresh_environment_in(wsl.path(), &terminal)
            .unwrap()
            .is_empty());
        let written = fs::read_to_string(wsl.path().join("service.env")).unwrap();
        assert!(
            written.contains("ANTHROPIC_API_KEY=sk-rotated\n"),
            "{written}"
        );
        let notes = refresh_environment_in(
            wsl.path(),
            &vars(&[("PATH", "/usr/bin"), ("ANTHROPIC_API_KEY", "sk-next")]),
        )
        .unwrap();
        assert!(
            notes
                .join("\n")
                .contains("took this terminal's value for ANTHROPIC_API_KEY;"),
            "{notes:?}"
        );
    }

    #[test]
    fn a_stop_during_the_restart_delay_holds_and_the_supervisor_exits() {
        let dir = tempfile::tempdir().unwrap();
        // Run 1 fails with a marker from an earlier boot, which holds nothing back; run 2
        // fails after `silicon stop` marked this boot, so there is no run 3.
        let script = r#"
n=$(cat "$1/runs" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$1/runs"
[ $n = 1 ] && printf '{"boot_id":"boot-0","at":"2026-09-26T10:00:00Z"}' > "$1/stopped" && exit 1
[ $n = 2 ] && printf '{"boot_id":"boot-1","at":"2026-09-26T11:00:00Z"}' > "$1/stopped" && exit 1
exit 0
"#;
        let (_events, handle) = supervised(script, dir.path(), quick());
        finish(handle);
        assert_eq!(
            fs::read_to_string(dir.path().join("runs")).unwrap().trim(),
            "2"
        );
        let text = fs::read_to_string(dir.path().join("service.log")).unwrap();
        assert!(
            text.contains("restarting in 10ms (1 failure in a row)"),
            "{text}"
        );
        assert!(
            text.contains("exited: exit status: 1 after") && text.contains(&format!(
                "; `silicon stop` stopped the interpreter at 2026-09-26T11:00:00+00:00 during this boot ({}), so it is not restarted and the supervisor exits",
                dir.path().join("stopped").display()
            )),
            "{text}"
        );
        assert!(!text.contains("restarting in 20ms"), "{text}");

        // A stop that comes while the supervisor waits holds once the wait ends, however
        // it ends.
        let dir = tempfile::tempdir().unwrap();
        let script = "n=$(cat \"$1/runs\" 2>/dev/null || echo 0); n=$((n+1)); echo $n > \"$1/runs\"\nexit 1\n";
        let policy = Policy {
            base: Duration::from_secs(600),
            cap: Duration::from_secs(600),
            ..quick()
        };
        let (events, handle) = supervised(script, dir.path(), policy);
        let log = dir.path().join("service.log");
        let deadline = Instant::now() + Duration::from_secs(20);
        while !fs::read_to_string(&log).is_ok_and(|text| text.contains("restarting in 10m 0s")) {
            assert!(Instant::now() < deadline, "the first run never failed");
            thread::sleep(Duration::from_millis(20));
        }
        state::write_json(
            &dir.path().join("stopped"),
            &Stopped {
                boot_id: Some("boot-1".into()),
                at: Utc::now(),
            },
        )
        .unwrap();
        events.send(Event::Signal(libc::SIGUSR2)).unwrap();
        finish(handle);
        assert_eq!(
            fs::read_to_string(dir.path().join("runs")).unwrap().trim(),
            "1"
        );
        let text = fs::read_to_string(&log).unwrap();
        assert!(
            text.contains("the restart delay ended; `silicon stop` stopped the interpreter at"),
            "{text}"
        );
    }

    #[test]
    fn a_broken_stop_marker_holds_nothing_back_and_is_told_once_per_text() {
        let dir = tempfile::tempdir().unwrap();
        let marker = test_marker(dir.path());
        assert_eq!(marker.this_boot(), None);
        assert!(marker.told.borrow().is_none());
        fs::write(&marker.path, "{broken").unwrap();
        assert_eq!(marker.this_boot(), None);
        let first = marker.told.borrow().clone().unwrap();
        assert!(
            first.starts_with(&format!("ignoring {}: ", marker.path.display()))
                && first.ends_with("it held: {broken"),
            "{first}"
        );
        // Asked again at the next failure: the same words are not repeated.
        assert_eq!(marker.this_boot(), None);
        assert_eq!(marker.told.borrow().as_deref(), Some(first.as_str()));
        fs::write(&marker.path, "[]").unwrap();
        assert_eq!(marker.this_boot(), None);
        assert!(marker
            .told
            .borrow()
            .as_deref()
            .unwrap()
            .ends_with("it held: []"));
        // A readable one from this boot holds.
        let at = Utc::now();
        state::write_json(
            &marker.path,
            &Stopped {
                boot_id: Some("boot-1".into()),
                at,
            },
        )
        .unwrap();
        assert_eq!(marker.this_boot(), Some(at));
    }

    #[test]
    fn the_systemd_unit_goes_where_the_user_manager_looks_and_earlier_copies_go() {
        let root = tempfile::tempdir().unwrap();
        let unit = root
            .path()
            .join("home/.config/systemd/user/silicon.service");
        let legacy_dir = root.path().join("dotfiles/systemd/user");
        let legacy = legacy_dir.join("silicon.service");
        let wants = legacy_dir.join("default.target.wants/silicon.service");
        let earlier = |host: &Host| {
            fs::create_dir_all(wants.parent().unwrap()).unwrap();
            fs::write(&legacy, host.render_unit(Path::new("/old/prefix"))).unwrap();
            let _ = fs::remove_file(&wants);
            std::os::unix::fs::symlink(&legacy, &wants).unwrap();
        };
        let installing = || {
            Script::new(vec![
                line("systemctl --user show-environment", ok("")),
                line("systemctl --user daemon-reload", ok("")),
                line("systemctl --user enable --now silicon.service", ok("")),
                line("loginctl show-user ada -p Linger", ok("Linger=yes\n")),
            ])
        };
        // An earlier release wrote the unit under this shell's XDG_CONFIG_HOME, which a
        // user manager PAM started never reads.
        let script = installing();
        let mut host = host(&script, root.path(), Platform::Linux);
        host.xdg_config_home = Some(root.path().join("dotfiles"));
        earlier(&host);
        save(
            &host.dir,
            &Record {
                mechanism: "systemd".into(),
                definition: Some(legacy.clone()),
                label: "silicon.service".into(),
                ..Record::default()
            },
        )
        .unwrap();
        let report = install(&host).unwrap();
        script.finished();
        assert_eq!(report.definition.as_deref(), Some(unit.as_path()));
        assert!(written_by_silicon(&unit));
        assert!(!legacy.exists() && fs::symlink_metadata(&wants).is_err());
        assert!(
            report.notes.join("\n").contains(&format!(
                "removed {}, which an earlier release wrote under XDG_CONFIG_HOME, where the systemd user manager may not look; the unit is now {}",
                legacy.display(),
                unit.display()
            )),
            "{report:?}"
        );
        assert_eq!(
            load(&host.dir).unwrap().unwrap().definition.as_deref(),
            Some(unit.as_path())
        );
        // A person's own file there is not Silicon's to remove.
        fs::write(&legacy, "[Unit]\nDescription=mine\n").unwrap();
        let script = installing();
        let mut again = super::tests::host(&script, root.path(), Platform::Linux);
        again.xdg_config_home = Some(root.path().join("dotfiles"));
        let report = install(&again).unwrap();
        script.finished();
        assert!(legacy.exists());
        assert!(
            !report.notes.join("\n").contains("earlier release"),
            "{report:?}"
        );
        // A record from before still naming the old place: the next start moves the unit
        // and enables it where the manager reads it, so it still starts at boot.
        earlier(&again);
        save(
            &again.dir,
            &Record {
                mechanism: "systemd".into(),
                definition: Some(legacy.clone()),
                label: "silicon.service".into(),
                ..Record::default()
            },
        )
        .unwrap();
        fs::remove_file(&unit).unwrap();
        let script = Script::new(vec![
            line("systemctl --user daemon-reload", ok("")),
            line("systemctl --user enable silicon.service", ok("")),
            line("systemctl --user start silicon.service", ok("")),
        ]);
        let host = super::tests::host(&script, root.path(), Platform::Linux);
        let record = load(&host.dir).unwrap().unwrap();
        start(&host, &record).unwrap();
        script.finished();
        assert!(written_by_silicon(&unit) && !legacy.exists());
        assert_eq!(
            load(&host.dir).unwrap().unwrap().definition.as_deref(),
            Some(unit.as_path())
        );
        // Moved once: the next start only starts it.
        let script = Script::new(vec![line("systemctl --user start silicon.service", ok(""))]);
        let host = super::tests::host(&script, root.path(), Platform::Linux);
        start(&host, &load(&host.dir).unwrap().unwrap()).unwrap();
        script.finished();
        // Uninstall removes the unit wherever this code wrote it.
        let script = Script::new(vec![
            line("systemctl --user disable --now silicon.service", ok("")),
            line("systemctl --user daemon-reload", ok("")),
        ]);
        let mut host = super::tests::host(&script, root.path(), Platform::Linux);
        host.xdg_config_home = Some(root.path().join("dotfiles"));
        earlier(&host);
        uninstall(&host).unwrap();
        script.finished();
        assert!(!unit.exists() && !legacy.exists() && fs::symlink_metadata(&wants).is_err());
        assert!(load(&host.dir).unwrap().unwrap().declined);
    }

    #[test]
    fn connect_installs_no_second_supervisor_beside_an_external_one() {
        let root = tempfile::tempdir().unwrap();
        // Any command would fail the test: nothing may be installed.
        let script = Script::default();
        let mut host = host(&script, root.path(), Platform::Linux);
        let daemon = host.dir.join("daemon.json");
        fs::write(
            &daemon,
            r#"{"pid":4321,"port":1823,"token":"t","version":"5.0.2","supervisor":"external"}"#,
        )
        .unwrap();
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        host.platform = Platform::Mac;
        assert_eq!(ensure_for_connect_with(&host).unwrap(), None);
        assert!(load(&host.dir).unwrap().is_none());
        // An interpreter connect started by itself, or no daemon.json: installed as usual.
        let plist = host.plist();
        let script = Script::new(vec![
            line(
                format!("launchctl bootout gui/501/{LABEL}"),
                fail(3, "No such process"),
            ),
            line(format!("launchctl enable gui/501/{LABEL}"), ok("")),
            line(
                format!("launchctl bootstrap gui/501 {}", plist.display()),
                ok(""),
            ),
        ]);
        let host = super::tests::host(&script, root.path(), Platform::Mac);
        fs::write(&daemon, r#"{"pid":4321,"port":1823,"token":"t"}"#).unwrap();
        let note = ensure_for_connect_with(&host).unwrap().unwrap();
        script.finished();
        assert!(note.starts_with("autostart installed"), "{note}");
    }

    #[test]
    fn an_xdg_config_home_that_is_config_by_another_name_keeps_the_unit() {
        // ~/.config is a link to the dotfiles directory XDG_CONFIG_HOME names: one unit
        // file under two names, never an earlier copy to remove.
        let root = tempfile::tempdir().unwrap();
        let dotfiles = root.path().join("dotfiles/config");
        fs::create_dir_all(&dotfiles).unwrap();
        fs::create_dir_all(root.path().join("home")).unwrap();
        std::os::unix::fs::symlink(&dotfiles, root.path().join("home/.config")).unwrap();
        let other_name = dotfiles.join("systemd/user/silicon.service");
        let script = Script::new(vec![
            line("systemctl --user show-environment", ok("")),
            line("systemctl --user daemon-reload", ok("")),
            line("systemctl --user enable --now silicon.service", ok("")),
            line("loginctl show-user ada -p Linger", ok("Linger=yes\n")),
        ]);
        let mut host = host(&script, root.path(), Platform::Linux);
        host.xdg_config_home = Some(dotfiles.clone());
        let unit = host.unit_path();
        let report = install(&host).unwrap();
        script.finished();
        assert!(written_by_silicon(&unit), "{report:?}");
        assert!(
            !report.notes.join("\n").contains("earlier release"),
            "{report:?}"
        );
        assert!(host.stale_units(Some(&other_name)).is_empty());
        // A record that names the unit by the other name: the next start records the
        // ~/.config name and keeps the file.
        save(
            &host.dir,
            &Record {
                mechanism: "systemd".into(),
                definition: Some(other_name.clone()),
                label: "silicon.service".into(),
                ..Record::default()
            },
        )
        .unwrap();
        let script = Script::new(vec![
            line("systemctl --user daemon-reload", ok("")),
            line("systemctl --user enable silicon.service", ok("")),
            line("systemctl --user start silicon.service", ok("")),
        ]);
        let host = super::tests::host(&script, root.path(), Platform::Linux);
        start(&host, &load(&host.dir).unwrap().unwrap()).unwrap();
        script.finished();
        assert!(written_by_silicon(&unit));
        assert_eq!(
            load(&host.dir).unwrap().unwrap().definition.as_deref(),
            Some(unit.as_path())
        );
    }
}
