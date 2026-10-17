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
use crate::process::Starting;
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
    "APPS_",
    "ACCOUNTS_",
    "TING_",
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
        Some(shell) if Path::new(shell).is_absolute() => match login_path(&dir, Path::new(shell)) {
            Ok(path) => Some(path),
            Err(error) => {
                warnings.push(format!(
                    "{}\nThe login shell's PATH is not added to the interpreter's",
                    failure::report(&error)
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
        warnings.push(failure::report(&error));
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
        Ok(command.spawn_retrying()?.id())
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
            prefix: crate::update::managed_prefix().map_err(|error| failure::report(&error)),
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
                            "{}\nNo systemd user manager answers",
                            failure::report(&self.failed(
                                "systemctl",
                                &["--user", "show-environment"],
                                &output
                            ))
                        )),
                    ),
                    Err(error) => (
                        "run",
                        Some(format!(
                            "{}\nNo systemd user manager is available",
                            failure::report(&error)
                        )),
                    ),
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

fn unmanaged() -> &'static str {
    "autostart needs a managed installation (install.sh puts one in ~/.local/share/silicon), so a development build never becomes a service. To supervise this build yourself, run `silicon serve` under your own process manager with SILICON_SERVICE=external set; a clean stop exits 0 and anything else exits non-zero"
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
        .map_err(|why| failure::received(why).context(unmanaged()))?;
    let previous = load(&host.dir)?;
    let (mechanism, why) = host.choose()?;
    let mut notes = Vec::new();
    if let Some(why) = why {
        notes.push(format!("{why}\nUsing cron and the Silicon supervisor"));
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
            notes.push(failure::report(&error));
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
            notes.push(failure::report(&host.failed("launchctl", &on, &enable)));
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
            "{}\nThere is no GUI login for {} right now (for example over SSH), so launchd loads {} at the next login; until then `silicon connect` starts the interpreter without a supervisor. A Mac that must run with nobody logged in needs automatic login.",
            failure::report(&error),
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
                "{}\nThe unit an earlier release wrote under XDG_CONFIG_HOME stays behind", failure::report(&error)
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
        "{}\nWithout linger the interpreter runs only while {user} is logged in; to start it at boot, run `sudo loginctl enable-linger {user}`.",
        failure::report(&refusal),
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
            Err(error) => format!("{}\nCould not read the state of the logon task {label}", failure::report(&error)),
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
        crate::stderr_line(&failure::report(&error));
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
                "{}\nWarning: the unit an earlier release wrote stays behind",
                failure::report(&error)
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
            notes.extend(problems.iter().map(failure::report));
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
            Err(error) => format!("{}\nInterpreter state is unknown", failure::report(&error)),
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
                        format!("{}\nNo GUI login right now, so launchd loads the agent at the next login", failure::report(&error))
                    } else {
                        format!("{}\nLaunchd has not loaded the agent; `silicon connect` or `silicon service restart` loads it", failure::report(&error))
                    });
                }
                Err(error) => status.notes.push(failure::report(&error)),
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
                Err(error) => status.notes.push(failure::report(&error)),
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
                Err(error) => status.notes.push(failure::report(&error)),
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
                Err(error) => status.notes.push(failure::report(&error)),
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
                Err(error) => status.notes.push(failure::report(&error)),
            }
        }
        "windows-task" => match host.task_state(&record.label) {
            Ok(state) => {
                status.details.insert("task state".into(), state);
            }
            Err(error) => status.notes.push(failure::report(&error)),
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
        .spawn_retrying()
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
        crate::stderr_line(&failure::report(&error));
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
        .filter(|(name, _)| {
            !NEVER_LOADED.contains(&name.as_str()) && !name.starts_with("SPACE_STATION_")
        })
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
        .filter(|(name, _)| {
            name != "PATH"
                && !NEVER_LOADED.contains(&name.as_str())
                && !name.starts_with("SPACE_STATION_")
        })
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
fn login_path(dir: &Path, shell: &Path) -> Result<String> {
    const MARKER: &str = "SILICON_LOGIN_PATH=";
    let script = format!("printf '\\n{MARKER}%s\\n' \"$PATH\"");
    let args = ["-l", "-c", script.as_str()];
    let shown = failure::argv(shell, &args);
    let mut command = Command::new(shell);
    command.args(args);
    let output = crate::process::output_within(&mut command, LOGIN_SHELL_LIMIT)
        .map_err(|error| failure::spawn(dir, &shown, &error))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    match stdout
        .lines()
        .rev()
        .find_map(|line| line.strip_prefix(MARKER))
    {
        Some(path) if output.status.success() => Ok(path.to_owned()),
        _ => Err(failure::command(dir, &shown, &output, &[])),
    }
}
