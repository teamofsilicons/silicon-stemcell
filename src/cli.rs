use crate::server;
use anyhow::{anyhow, bail, Context, Result};
use chrono::{DateTime, NaiveDate, Utc};
use chrono_tz::Tz;
use clap::{Args, CommandFactory, Parser, Subcommand};
use regex::Regex;
use serde_json::{json, Value};
use std::{
    fs::File,
    io::{IsTerminal, Read, Seek, SeekFrom, Write},
    os::{fd::AsRawFd, unix::fs::MetadataExt},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::Duration,
};

#[derive(Parser)]
#[command(
    name = "silicon",
    version,
    about = "Silicon interpreter: connect a silicon.yaml and run its internal silicons",
    propagate_version = true
)]
struct SiliconCli {
    /// Print machine-readable results.
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Option<SiliconCommand>,
}
#[derive(Subcommand)]
enum SiliconCommand {
    /// Discover the interpreter application identity and documentation.
    Iam,
    /// Validate all configuration and flow expressions without connecting.
    Compile { yaml: PathBuf },
    /// Compile and connect a silicon.yaml; start the interpreter if needed.
    Connect { yaml: PathBuf },
    /// Disconnect by silicon id or YAML path. Without a target, list choices.
    Disconnect { target: Option<String> },
    /// List connected Silicons, optionally matching a quoted glob such as 'si:*'.
    #[command(alias = "list")]
    Ls { pattern: Option<String> },
    /// Read the append-only Silicon log.
    Logs {
        #[command(subcommand)]
        command: LogsCommand,
    },
    /// Run the interpreter in the foreground (normally started by connect).
    Serve {
        #[arg(long, default_value_t = 1823)]
        port: u16,
        /// Development only: serve directly without Caddy or localhost aliases.
        #[arg(long)]
        no_proxy: bool,
    },
    /// Stop the interpreter and its child processes, and wait until it has exited.
    Stop {
        /// Terminate an interpreter that does not stop: SIGTERM, then SIGKILL 10 seconds later.
        #[arg(long)]
        force: bool,
    },
    /// Install a newer stable GitHub release into a managed bundle installation.
    Update,
    /// Start the interpreter at login or boot and restart it after a crash.
    Service {
        #[command(subcommand)]
        action: crate::service::Action,
    },
    /// Install an app for this system through Honeycomb, for example 'dm'.
    Install { app_id: String },
    /// Remove a Honeycomb-managed app from this home.
    Uninstall { app_id: String },
    /// Inspect or change persistent interpreter settings. Changes apply while running.
    Settings {
        #[command(subcommand)]
        command: Option<SettingsCommand>,
    },
    /// Check whether a Silicon is connected without sending it a message.
    Ping { silicon: String },
    /// Read a connected Silicon's configuration with credentials redacted.
    Config { silicon: String },
    /// Source, documentation, dependency, and protocol details.
    Info,
    /// Submit a reproducible GitHub bug report, optionally linking a fix PR. Requires gh login.
    BugReport {
        #[arg(long)]
        title: String,
        #[arg(long)]
        body: String,
        #[arg(long)]
        pr: Option<String>,
        /// Show the report without submitting it.
        #[arg(long)]
        dry_run: bool,
    },
    /// Open the local dashboard with this user's interpreter credential.
    Web {
        #[arg(long)]
        no_open: bool,
    },
    /// Send immediately to an ISI in a connected Silicon.
    Send {
        silicon: String,
        #[command(flatten)]
        send: SendArgs,
    },
    /// List an ISI's active or archived sessions.
    Sessions {
        silicon: String,
        isi: String,
        #[command(flatten)]
        archive: ArchiveArgs,
    },
    /// Show progress and recent events without sending a message.
    Show {
        silicon: String,
        #[command(flatten)]
        target: TargetArgs,
    },
    /// End an ISI's current session without starting another.
    End {
        silicon: String,
        #[command(flatten)]
        target: TargetArgs,
    },
}
#[derive(Subcommand)]
enum LogsCommand {
    /// Print the last 100 lines, then follow new entries. Ctrl-C exits.
    Show {
        silicon: String,
        #[arg(long, default_value_t = 100)]
        lines: usize,
        /// Print the tail and exit; suitable for scripts.
        #[arg(long)]
        no_follow: bool,
    },
}
#[derive(Subcommand)]
enum SettingsCommand {
    /// Read all settings, or one of telemetry or auto_update.
    Get { key: Option<String> },
    /// Toggle telemetry or auto_update. Example: settings set telemetry --off.
    Set {
        key: String,
        #[arg(long, conflicts_with = "off", required_unless_present = "off")]
        on: bool,
        #[arg(long, conflicts_with = "on", required_unless_present = "on")]
        off: bool,
    },
}
#[derive(Parser)]
#[command(
    name = "si",
    version,
    about = "Internal Silicon commands; identity and access come from the current ISI",
    propagate_version = true
)]
struct SiCli {
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Option<SiCommand>,
}
#[derive(Subcommand)]
enum SiCommand {
    /// Install or uninstall an app and update this Silicon's YAML configuration.
    App {
        #[command(subcommand)]
        command: AppCommand,
    },
    /// Set up an IAM application; alias for `si auth setup APP`.
    Setup {
        #[command(subcommand)]
        command: SetupCommand,
    },
    /// Authenticate IAM applications without exposing the Silicon token.
    Auth {
        #[command(subcommand)]
        command: AuthCommand,
    },
    /// Communicate with permitted ISIs and inspect their sessions.
    Isi {
        #[command(subcommand)]
        command: IsiCommand,
    },
    /// Archive your current persistent session and start its successor.
    Session {
        #[command(subcommand)]
        command: SessionCommand,
    },
}
#[derive(Subcommand)]
enum AppCommand {
    /// Install a Honeycomb app and add its bare app ID to this ISI's apps.
    Install { app_id: String },
    /// Uninstall a Honeycomb app and remove its app entry and configuration.
    Uninstall { app_id: String },
}
#[derive(Subcommand)]
enum SetupCommand {
    /// Authenticate an IAM app ID without exposing the Silicon token.
    Auth { app: String },
}
#[derive(Subcommand)]
enum AuthCommand {
    /// Discover and authenticate an application, for example: si auth setup dm.
    Setup { app: String },
    /// Remove this Silicon's authentication for an application.
    Remove { app: String },
}
#[derive(Subcommand)]
enum IsiCommand {
    /// Deliver now, including while the receiving ISI is already working.
    Send(SendArgs),
    /// List active sessions; --archived alone selects the previous 3 days.
    Ls {
        isi: String,
        #[command(flatten)]
        archive: ArchiveArgs,
    },
    /// Inspect progress without interrupting the ISI.
    Show(TargetArgs),
    /// End the current session without creating a successor.
    End(TargetArgs),
}
#[derive(Args)]
struct SendArgs {
    isi: String,
    message: String,
    /// Session address, required for session-mode and archived ISIs.
    #[arg(long)]
    id: Option<String>,
    /// Title when creating a session (required for ephemeral session-mode ISIs).
    #[arg(long)]
    title: Option<String>,
    /// Create a persistent session if this id does not exist.
    #[arg(long, conflicts_with = "archived")]
    new: bool,
    /// Send to an archived session by id.
    #[arg(long, requires = "id")]
    archived: bool,
}
impl SendArgs {
    fn value(&self) -> Value {
        json!({"isi":self.isi,"message":self.message,"id":self.id,"title":self.title,"new":self.new,"archived":self.archived})
    }
}
#[derive(Args)]
struct TargetArgs {
    isi: String,
    #[arg(long)]
    id: Option<String>,
}
impl TargetArgs {
    fn value(&self) -> Value {
        json!({"isi":self.isi,"id":self.id})
    }
}
#[derive(Args, Default)]
struct ArchiveArgs {
    /// Archived sessions. Filters intersect: DD:MM:YYYY, DATE-DATE, or title/description glob.
    /// Bare --archived means the past 72 hours; explicit filters search all history.
    #[arg(long,num_args=0..,action=clap::ArgAction::Append,default_missing_value="",value_name="FILTER")]
    archived: Option<Vec<String>>,
    /// Override the Silicon timezone when selecting archive dates.
    #[arg(long, value_name = "IANA")]
    timezone: Option<String>,
}
impl ArchiveArgs {
    fn value(&self, isi: &str) -> Value {
        let mut value = json!({"isi":isi,"archived":self.archived.is_some()});
        if let Some(filters) = &self.archived {
            value["filters"] = json!(filters);
        }
        if let Some(timezone) = &self.timezone {
            value["timezone"] = json!(timezone);
        }
        value
    }
}
#[derive(Subcommand)]
enum SessionCommand {
    /// Archive this session under --id, preserving timestamps and conversation.
    New {
        #[arg(long, required = true)]
        archive_current_session: bool,
        #[arg(long)]
        id: String,
        #[arg(long)]
        title: String,
        #[arg(long, alias = "summary")]
        description: String,
    },
}

pub fn silicon() -> Result<()> {
    let cli = SiliconCli::parse();
    match cli.command.unwrap_or(SiliconCommand::Ls { pattern: None }) {
        SiliconCommand::Iam => print_json(
            &json!({"app_id":"silicon","docs_url":"https://docs.teamofsilicons.com","repository":"https://github.com/teamofsilicons/silicon-stemcell"}),
        )?,
        SiliconCommand::Compile { yaml } => {
            let cfg = server::compile(yaml)?;
            if cli.json {
                print_json(
                    &json!({"valid":true,"yaml":cfg.path,"silicon":cfg.silicon.id,"isi_count":cfg.isi.len(),"warnings":cfg.warnings}),
                )?;
            } else {
                warnings(&json!(cfg.warnings));
                println!("valid: {} ({} ISIs)", cfg.path.display(), cfg.isi.len());
            }
        }
        SiliconCommand::Connect { yaml } => {
            let value = if cli.json {
                let cfg = server::compile(yaml)?;
                supervise(&cfg);
                server::call(&server::daemon(true)?, "connect", json!({"yaml":cfg.path}))?
            } else {
                crate::progress::connect(yaml)?
            };
            if cli.json {
                print_json(&value)?;
            } else {
                warnings(&value["warnings"]);
                println!(
                    "connected {} at http://{}",
                    field(&value["connection"], "id")?,
                    field(&value["connection"], "host")?
                );
            }
        }
        SiliconCommand::Disconnect {
            target: Some(target),
        } => {
            let target = if crate::config::valid_silicon_id(&target) {
                target
            } else {
                if target.contains(':') && !target.contains('/') {
                    bail!("expected a canonical Silicon ID si:<handle> or a YAML path, got {target:?}; migrate legacy IDs using IAM's verified mapping");
                }
                Path::new(&target)
                    .canonicalize()
                    .with_context(|| format!("resolve disconnect YAML path {target:?}"))?
                    .to_string_lossy()
                    .into_owned()
            };
            let value = server::call(
                &server::daemon(false)?,
                "disconnect",
                json!({"target":target}),
            )?;
            if cli.json {
                print_json(&value)?;
            } else {
                warnings(&value["warnings"]);
                println!("disconnected {}", field(&value, "disconnected")?);
            }
        }
        SiliconCommand::Disconnect { target: None } => {
            let rows = connections()?;
            if cli.json {
                print_json(&json!(rows))?;
            } else {
                print_connections(&rows);
                println!("run: silicon disconnect <silicon-id-or-yaml-path>");
            }
        }
        SiliconCommand::Ls { pattern } => {
            let filter = glob(pattern.as_deref().unwrap_or("*"), true, false)?;
            let rows: Vec<_> = connections()?
                .into_iter()
                .filter(|row| filter.is_match(row["id"].as_str().unwrap_or("")))
                .collect();
            if cli.json {
                print_json(&json!(rows))?;
            } else {
                print_connections(&rows);
            }
        }
        SiliconCommand::Logs {
            command:
                LogsCommand::Show {
                    silicon,
                    lines,
                    no_follow,
                },
        } => logs(&silicon, lines, !no_follow, cli.json)?,
        SiliconCommand::Serve { port, no_proxy } => server::serve(port, no_proxy)?,
        SiliconCommand::Stop { force } => {
            let terminal = std::io::stderr().is_terminal() && !cli.json;
            // Before the shutdown: a cron `service ensure` between the interpreter's exit and
            // a later marker would start it again.
            if let Err(error) = crate::service::note_stopped() {
                eprintln!("warning: {error:#}");
            }
            let result = server::stop(force, |pid| {
                if terminal {
                    eprintln!(
                        "waiting for the interpreter{} to finish stopping",
                        pid.map(|pid| format!(" (pid {pid})")).unwrap_or_default()
                    );
                }
            });
            // The marker stays even when this failed: the interpreter may be stopping slowly or
            // waiting out a restart delay, and the portable supervisor must not start it again.
            // `silicon connect` and `silicon service restart` clear it.
            let result = match result {
                Ok(result) => result,
                // Nothing runs, but the portable supervisor may be waiting to restart it: the
                // marker keeps it stopped, so this is a stop, not a failure.
                Err(error)
                    if !server::interpreter_present()
                        && matches!(crate::service::installed(), Ok(Some(ref s)) if s.mechanism() == "run") =>
                {
                    if cli.json {
                        print_json(&json!({"stopped": true, "running": false}))?;
                    } else {
                        println!("the interpreter was not running ({error:#}); it stays stopped until the next boot or `silicon connect`");
                    }
                    return Ok(());
                }
                Err(error) => return Err(error),
            };
            let supervised = matches!(crate::service::installed(), Ok(Some(_)));
            if cli.json {
                print_json(&result)?;
            } else if supervised {
                println!("interpreter stopped; it starts again at the next login or boot, or with `silicon connect`. `silicon service uninstall` turns autostart off");
            } else {
                println!("interpreter stopped");
            }
        }
        SiliconCommand::Service { action } => crate::service::cli(action, cli.json)?,
        SiliconCommand::Update => {
            let installed = crate::update::install_latest()?;
            // A running interpreter restarts into the new release by itself once it is idle;
            // so does one still running an older release than this, the installed one.
            let behind = || {
                server::daemon(false)
                    .ok()
                    .filter(|daemon| daemon.version != env!("CARGO_PKG_VERSION"))
            };
            let restart = match &installed {
                Some(_) => Some(
                    server::daemon(false)
                        .and_then(|daemon| server::call(&daemon, "restart", json!({}))),
                ),
                None => behind().map(|daemon| server::call(&daemon, "restart", json!({}))),
            };
            let requested = matches!(restart, Some(Ok(_)));
            if cli.json {
                print_json(
                    &json!({"updated": installed.is_some(), "executable": installed, "restart_required": installed.is_some(), "restart_requested": requested}),
                )?;
            } else {
                match restart {
                    None => println!("Silicon {} is current", env!("CARGO_PKG_VERSION")),
                    Some(Ok(_)) if installed.is_none() => println!(
                        "Silicon {} is current; the running interpreter is on an older release and restarts into this one as soon as no work is running",
                        env!("CARGO_PKG_VERSION")
                    ),
                    Some(Ok(_)) => println!(
                        "updated; the running interpreter restarts into the new release as soon as no work is running"
                    ),
                    Some(Err(error)) if never_started(&error) => {
                        println!("updated; the interpreter uses the new release when it next starts")
                    }
                    Some(Err(error)) => println!(
                        "updated; the interpreter uses the new release when it next starts. The running interpreter could not be asked to restart ({error:#}); to restart it now, run `silicon stop`, then `silicon connect` with your silicon.yaml"
                    ),
                }
            }
        }
        SiliconCommand::Web { no_open } => {
            let daemon = server::daemon(false)?;
            let url = format!("http://127.0.0.1:{}/#{}", daemon.port, daemon.token);
            if cli.json {
                print_json(&json!({"url":url}))?;
            } else if no_open {
                println!("{url}");
            } else {
                let program = if cfg!(target_os = "macos") {
                    "open"
                } else {
                    "xdg-open"
                };
                open_dashboard(program, &url, &daemon.token)?;
                println!("opened Silicon dashboard");
            }
        }
        SiliconCommand::Install { app_id } => print_json(&crate::apps::install(&app_id)?)?,
        SiliconCommand::Uninstall { app_id } => print_json(&crate::apps::uninstall(&app_id)?)?,
        SiliconCommand::Settings { command } => {
            let value = match command {
                Some(SettingsCommand::Set { key, on, .. }) => {
                    // The daemon only serializes this same file write; without one, write it here.
                    match server::daemon(false) {
                        Ok(daemon) => {
                            server::call(&daemon, "settings-set", json!({"key":key,"enabled":on}))?
                        }
                        Err(error) => {
                            if !never_started(&error) {
                                eprintln!("warning: writing the settings file directly; the interpreter did not answer: {error:#}");
                            }
                            json!(crate::settings::set(&key, on)?)
                        }
                    }
                }
                Some(SettingsCommand::Get { key: Some(key) }) => json!(crate::settings::load()?)
                    .get(&key)
                    .cloned()
                    .ok_or_else(|| {
                        anyhow!("unknown setting {key:?}; expected telemetry or auto_update")
                    })?,
                _ => json!(crate::settings::load()?),
            };
            print_json(&value)?;
        }
        SiliconCommand::Ping { silicon } => {
            public_action("silicon-ping", &silicon, json!({}), true)?
        }
        SiliconCommand::Config { silicon } => {
            public_action("configuration", &silicon, json!({}), true)?
        }
        SiliconCommand::Info => {
            print_json(&json!({"version":env!("CARGO_PKG_VERSION"),"protocol":1,
            "source":"https://github.com/teamofsilicons/silicon-stemcell",
            "docs":"https://docs.teamofsilicons.com", "rust_package":"silicon",
            "dependencies":["silicon-omni","iam","ting","honeycomb","space-station","caddy"],
            "bugs":"https://github.com/teamofsilicons/silicon-stemcell/issues"}))?
        }
        SiliconCommand::BugReport {
            title,
            body,
            pr,
            dry_run,
        } => {
            let body = format!(
                "{body}\n\nSilicon version: {}{}",
                env!("CARGO_PKG_VERSION"),
                pr.map(|p| format!("\nFix PR: {p}")).unwrap_or_default()
            );
            if dry_run {
                print_json(&json!({"title":title,"body":body}))?;
            } else {
                file_bug_report("gh", &title, &body)?;
            }
        }
        SiliconCommand::Send { silicon, send } => {
            public_action("send", &silicon, send.value(), cli.json)?
        }
        SiliconCommand::Show { silicon, target } => {
            public_action("show", &silicon, target.value(), cli.json)?
        }
        SiliconCommand::End { silicon, target } => {
            public_action("end", &silicon, target.value(), cli.json)?
        }
        SiliconCommand::Sessions {
            silicon,
            isi,
            archive,
        } => {
            let result = server::call(&server::daemon(false)?, "sessions", {
                let mut args = archive.value(&isi);
                args["silicon"] = json!(silicon);
                args
            })?;
            show_sessions(result, cli.json)?;
        }
    }
    Ok(())
}
pub fn si() -> Result<()> {
    let cli = SiCli::parse();
    let Some(command) = cli.command else {
        if cli.json {
            return print_json(
                &json!({"isi":std::env::var("ISI").ok(),"services":["app","auth","isi","session"]}),
            );
        }
        if let Ok(isi) = std::env::var("ISI") {
            println!("ISI: {isi}\n");
        }
        SiCli::command().print_help()?;
        println!();
        return Ok(());
    };
    let (action, args) = match command {
        SiCommand::App {
            command: AppCommand::Install { app_id },
        } => ("app-install", json!({"app_id":app_id})),
        SiCommand::App {
            command: AppCommand::Uninstall { app_id },
        } => ("app-uninstall", json!({"app_id":app_id})),
        SiCommand::Setup {
            command: SetupCommand::Auth { app },
        } => ("auth-setup", json!({"app":app})),
        SiCommand::Auth {
            command: AuthCommand::Setup { app },
        } => ("auth-setup", json!({"app":app})),
        SiCommand::Auth {
            command: AuthCommand::Remove { app },
        } => ("auth-remove", json!({"app":app})),
        SiCommand::Isi {
            command: IsiCommand::Send(send),
        } => ("send", send.value()),
        SiCommand::Isi {
            command: IsiCommand::Show(target),
        } => ("show", target.value()),
        SiCommand::Isi {
            command: IsiCommand::End(target),
        } => ("end", target.value()),
        SiCommand::Isi {
            command: IsiCommand::Ls { isi, archive },
        } => {
            let value = server::internal("sessions", archive.value(&isi))?;
            return show_sessions(value, cli.json);
        }
        SiCommand::Session {
            command:
                SessionCommand::New {
                    archive_current_session,
                    id,
                    title,
                    description,
                },
        } => (
            "new-session",
            json!({"archive_current_session":archive_current_session,"id":id,"title":title,"description":description}),
        ),
    };
    show_result(action, server::internal(action, args)?, cli.json)
}
/// Launchers hand the URL to a browser that can hold captured pipes open for its
/// whole life, so their output goes straight to the terminal instead of the error.
fn open_dashboard(program: &str, url: &str, token: &str) -> Result<()> {
    let home = server::directory();
    let shown = crate::failure::mask(&home, &crate::failure::argv(program, &[url]), &[token]);
    let hint = "could not open the Silicon dashboard; use silicon web --no-open to print its URL";
    let status = std::process::Command::new(program)
        .arg(url)
        .status()
        .map_err(|error| crate::failure::spawn(&home, &shown, &error))
        .context(hint)?;
    if !status.success() {
        return Err(anyhow!(
            "`{shown}` failed: {status}; its output, if any, is above"
        ))
        .context(hint);
    }
    Ok(())
}
fn file_bug_report(program: &str, title: &str, body: &str) -> Result<()> {
    let args = [
        "issue",
        "create",
        "--repo",
        "teamofsilicons/silicon-stemcell",
        "--title",
        title,
        "--body",
        body,
    ];
    let home = server::directory();
    let shown = crate::failure::argv(program, &args);
    let output =
        crate::process::Starting::output_retrying(std::process::Command::new(program).args(args))
            .map_err(|error| crate::failure::spawn(&home, &shown, &error))
            .context(
                "could not file the bug report; install the GitHub CLI (gh) and run gh auth login",
            )?;
    if !output.status.success() {
        return Err(crate::failure::command(&home, &shown, &output, &[]))
            .context("GitHub CLI could not file the bug report; check gh auth status");
    }
    // gh answers with the new issue's URL.
    std::io::stdout().write_all(&output.stdout)?;
    std::io::stderr().write_all(&output.stderr)?;
    Ok(())
}
/// Before `connect` starts or reaches the interpreter: install autostart the first time
/// (the interpreter then comes back after a reboot or crash), warn about paths a supervisor
/// cannot read, and let an interpreter started without a supervisor hand over to it. All of
/// it goes to stderr, so `--json` output stays JSON, and none of it can fail the connect.
pub(crate) fn supervise(cfg: &crate::config::Config) {
    match crate::service::ensure_for_connect() {
        Ok(Some(note)) => eprintln!("{note}"),
        Ok(None) => {}
        Err(error) => eprintln!(
            "warning: autostart was not set up; the interpreter runs without a supervisor: {error:#}"
        ),
    }
    for warning in crate::service::connect_warnings(&cfg.path, &cfg.home) {
        eprintln!("warning: {warning}");
    }
    let Ok(Some(service)) = crate::service::installed() else {
        return;
    };
    // Started in the background by an earlier connect, not by a supervisor: it hands over to
    // one whose interpreter can wait beside it, once no work is running. One run in a terminal
    // or by the user's own process manager is theirs and is left alone.
    if !crate::server::HANDOVER_MECHANISMS.contains(&service.mechanism()) {
        return;
    }
    if let Ok(daemon) = server::daemon(false) {
        if daemon.supervisor.is_none() && daemon.detached {
            match server::call(&daemon, "restart", json!({"handover": true})) {
                Ok(_) => eprintln!(
                    "the running interpreter (pid {}) hands over to the {} service as soon as no work is running",
                    daemon.pid,
                    service.mechanism()
                ),
                Err(error) => eprintln!(
                    "warning: the running interpreter (pid {}) was not started by the {} service and could not be asked to hand over to it: {error:#}",
                    daemon.pid,
                    service.mechanism()
                ),
            }
        }
    }
}
/// Only a missing daemon.json with nothing holding daemon.lock means no interpreter was
/// ever started. A leftover file, one that is starting or restarting (it holds the lock
/// before it writes daemon.json), or any other failure is worth a reason.
fn never_started(error: &anyhow::Error) -> bool {
    error
        .root_cause()
        .downcast_ref::<std::io::Error>()
        .is_some_and(|cause| cause.kind() == std::io::ErrorKind::NotFound)
        && !server::directory()
            .join("daemon.json")
            .try_exists()
            .unwrap_or(true)
        && !server::interpreter_present()
}
/// Saved Silicons as the interpreter lists them, each with its state.
fn connections() -> Result<Vec<Value>> {
    let daemon = match server::daemon(false) {
        Ok(daemon) => daemon,
        Err(error) => {
            if !never_started(&error) {
                eprintln!(
                    "warning: no connections listed; the interpreter did not answer: {error:#}"
                );
            }
            return Ok(Vec::new());
        }
    };
    let value = server::call(&daemon, "list", json!({}))?;
    let rows = value.as_array().cloned().ok_or_else(|| {
        anyhow!(
            "interpreter returned an invalid connection list (expected an array): {}",
            shown(&value)
        )
    })?;
    for row in &rows {
        serde_json::from_value::<server::Listed>(row.clone()).with_context(|| {
            format!(
                "interpreter returned an invalid connection list entry: {}",
                shown(row)
            )
        })?;
    }
    Ok(rows)
}
/// One line per Silicon; one that is not connected shows its state after the host, and a
/// failure it is retrying is shown whole on the lines below it.
fn print_connections(rows: &[Value]) {
    if rows.is_empty() {
        println!("No Silicons connected.");
    }
    for row in rows {
        let Ok(listed) = serde_json::from_value::<server::Listed>(row.clone()) else {
            println!("{}", shown(row));
            continue;
        };
        for line in connection_lines(&listed) {
            println!("{line}");
        }
    }
}
fn connection_lines(listed: &server::Listed) -> Vec<String> {
    let row = &listed.connection;
    let state = match listed.state.as_str() {
        "connected" => String::new(),
        "waiting" => format!(
            "\twaiting to be restored (attempt {} failed; next attempt {})",
            listed.attempt.unwrap_or_default(),
            listed.retry_at.as_deref().unwrap_or("soon")
        ),
        other => format!("\t{}", clean(other)),
    };
    let mut lines = vec![format!(
        "{}\thttp://{}{state}\t{}",
        row.id,
        row.host,
        row.yaml.display()
    )];
    let indented = |error: &str, lines: &mut Vec<String>| {
        lines.extend(error.lines().map(|line| format!("    {}", clean(line))));
    };
    if let Some(error) = &listed.error {
        indented(error, &mut lines);
    }
    if let Some(error) = &listed.ting_error {
        lines.push(format!(
            "  Ting webhook registration is failing (attempt {}; next attempt {}):",
            listed.ting_attempt.unwrap_or_default(),
            listed.ting_retry_at.as_deref().unwrap_or("soon")
        ));
        indented(error, &mut lines);
    }
    lines
}
fn warnings(value: &Value) {
    for warning in value
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
    {
        eprintln!("warning: {warning}");
    }
}
fn print_json(value: &Value) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}
fn field<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key].as_str().ok_or_else(|| {
        anyhow!(
            "interpreter response has no string {key:?}: {}",
            shown(value)
        )
    })
}
/// A response as JSON for an error, with only credential values masked.
fn shown(value: &Value) -> String {
    crate::failure::mask(&server::directory(), &value.to_string(), &[])
}
fn public_action(action: &str, silicon: &str, mut args: Value, json: bool) -> Result<()> {
    args["silicon"] = json!(silicon);
    show_result(
        action,
        server::call(&server::daemon(false)?, action, args)?,
        json,
    )
}
fn show_result(action: &str, value: Value, json: bool) -> Result<()> {
    if json || action == "show" {
        return print_json(&value);
    }
    match action {
        "send" => println!(
            "sent to {}:{}",
            field(&value["session"], "isi")?,
            field(&value["session"], "id")?
        ),
        "end" => println!("session ended"),
        "new-session" => println!("started session {}", field(&value, "id")?),
        "auth-setup" => println!("authenticated {}", field(&value, "app_id")?),
        "auth-remove" => println!("authentication removed"),
        "app-install" => println!(
            "installed {} and updated silicon.yaml",
            field(&value, "app_id")?
        ),
        "app-uninstall" => println!(
            "uninstalled {} and updated silicon.yaml",
            field(&value, "app_id")?
        ),
        _ => return print_json(&value),
    }
    Ok(())
}

fn glob(pattern: &str, anchored: bool, insensitive: bool) -> Result<Regex> {
    let source = regex::escape(pattern)
        .replace("\\*", ".*")
        .replace("\\?", ".");
    let source = if anchored {
        format!("\\A{source}\\z")
    } else {
        source
    };
    regex::RegexBuilder::new(&source)
        .case_insensitive(insensitive)
        .dot_matches_new_line(true)
        .build()
        .with_context(|| format!("invalid glob {pattern:?}"))
}
enum ArchiveFilter {
    Dates(NaiveDate, NaiveDate),
    Text(Regex),
}

/// Apply the CLI's intersecting archive filters to authorized session records.
/// An empty filter list means the last 72 hours; explicit filters search all history.
pub fn filter_sessions(
    value: Value,
    filters: &[String],
    timezone: &str,
    now: DateTime<Utc>,
) -> Result<Value> {
    let timezone: Tz = timezone.parse().with_context(|| {
        format!("archive timezone must be an IANA timezone such as UTC or Asia/Kolkata, got {timezone:?}")
    })?;
    let mut parsed = Vec::new();
    for filter in filters.iter().map(|s| s.trim()).filter(|s| !s.is_empty()) {
        if filter.contains(':')
            && filter
                .chars()
                .all(|c| c.is_ascii_digit() || c == ':' || c == '-')
        {
            let (first, last) = filter.split_once('-').unwrap_or((filter, filter));
            let date = |date: &str| {
                NaiveDate::parse_from_str(date, "%d:%m:%Y").with_context(|| {
                    format!("archive date {date:?} in filter {filter:?} must be DD:MM:YYYY")
                })
            };
            let (first, last) = (date(first)?, date(last)?);
            if first > last {
                bail!("archive range {filter:?} starts after it ends");
            }
            parsed.push(ArchiveFilter::Dates(first, last));
        } else {
            parsed.push(ArchiveFilter::Text(glob(filter, false, true)?));
        }
    }
    let records = value.as_array().ok_or_else(|| {
        anyhow!(
            "interpreter returned an invalid session list (expected an array): {}",
            shown(&value)
        )
    })?;
    let mut found = Vec::new();
    for record in records {
        let timestamp = DateTime::parse_from_rfc3339(field(record, "archived_at")?)
            .with_context(|| format!("invalid session archive timestamp in {}", shown(record)))?;
        let day = timestamp.with_timezone(&timezone).date_naive();
        let matches = if parsed.is_empty() {
            timestamp >= now - chrono::Duration::days(3)
        } else {
            parsed.iter().all(|filter| match filter {
                ArchiveFilter::Dates(first, last) => *first <= day && day <= *last,
                ArchiveFilter::Text(pattern) => ["title", "description"]
                    .iter()
                    .any(|key| pattern.is_match(record[key].as_str().unwrap_or(""))),
            })
        };
        if matches {
            found.push(record.clone());
        }
    }
    Ok(Value::Array(found))
}
fn show_sessions(value: Value, json: bool) -> Result<()> {
    if json {
        return print_json(&value);
    }
    let rows = value.as_array().ok_or_else(|| {
        anyhow!(
            "interpreter returned an invalid session list (expected an array): {}",
            shown(&value)
        )
    })?;
    if rows.is_empty() {
        println!("No matching sessions.");
    }
    for row in rows {
        println!(
            "{}\t{}\t{}\t{}",
            field(row, "id")?,
            field(row, "status")?,
            field(row, "last")?,
            clean(row["title"].as_str().unwrap_or(""))
        );
        if let Some(description) = row["description"].as_str().filter(|s| !s.is_empty()) {
            println!("  {}", clean(description));
        }
    }
    Ok(())
}
fn log_path(id: &str) -> Result<PathBuf> {
    if !crate::config::valid_silicon_id(id) {
        bail!("expected a canonical Silicon ID si:<handle>, got {id:?}; migrate legacy IDs using IAM's verified mapping");
    }
    if let Some(connection) = server::saved()
        .with_context(|| format!("look up {id} among saved connections to find its log"))?
        .into_iter()
        .find(|connection| connection.id == id)
    {
        return Ok(connection.home.join(".silicon/silicon.log"));
    }
    let daemon = server::daemon(false).with_context(|| {
        format!("{id} has no saved connection, so only the interpreter can say where its log is")
    })?;
    let value = server::call(&daemon, "logs", json!({"silicon":id}))?;
    Ok(PathBuf::from(field(&value, "path")?))
}
fn clean(value: &str) -> String {
    value
        .chars()
        .filter(|c| !c.is_control() || *c == '\t')
        .collect()
}
fn colored(value: &str) -> String {
    let value = clean(value);
    let mut closings = value.match_indices(']');
    let Some((first, _)) = closings.next() else {
        return value;
    };
    let Some((last, _)) = closings.next() else {
        return value;
    };
    let kind = &value[..=first];
    let code = if kind.contains("error") {
        31
    } else if ["[runtime]", "[flow]", "[event]"].contains(&kind) {
        90
    } else {
        32 + value[first + 1..=last]
            .bytes()
            .fold(0u16, |sum, b| sum.wrapping_add(u16::from(b)))
            % 5
    };
    format!(
        "\x1b[{code}m{}\x1b[0m{}",
        &value[..=last],
        &value[last + 1..]
    )
}
fn terminal_size() -> (u16, u16) {
    let mut size = libc::winsize {
        ws_row: 0,
        ws_col: 0,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    if unsafe { libc::ioctl(std::io::stdout().as_raw_fd(), libc::TIOCGWINSZ, &mut size) } == 0 {
        (size.ws_row.max(4), size.ws_col.max(20))
    } else {
        (24, 80)
    }
}
struct Footer {
    id: String,
    path: String,
    size: (u16, u16),
}
impl Footer {
    fn start(id: &str, path: &Path) -> Result<Self> {
        let size = terminal_size();
        print!("\x1b[?1049h\x1b[?25l\x1b[1;{}r\x1b[H", size.0 - 2);
        std::io::stdout().flush()?;
        Ok(Self {
            id: clean(id),
            path: clean(&path.display().to_string()),
            size,
        })
    }
    fn draw(&mut self) -> Result<()> {
        let size = terminal_size();
        if size != self.size {
            print!("\x1b[1;{}r", size.0 - 2);
            self.size = size;
        }
        let id: String = format!("silicon {}  ·  Ctrl-C to exit", self.id)
            .chars()
            .take(size.1 as usize - 1)
            .collect();
        let path: String = self.path.chars().take(size.1 as usize - 1).collect();
        print!(
            "\x1b7\x1b[{};1H\x1b[2K\x1b[7m{id}\x1b[0m\x1b[{};1H\x1b[2K{path}\x1b8",
            size.0 - 1,
            size.0
        );
        std::io::stdout().flush()?;
        Ok(())
    }
}
impl Drop for Footer {
    fn drop(&mut self) {
        print!("\x1b[r\x1b[?25h\x1b[?1049l");
        let _ = std::io::stdout().flush();
    }
}
/// silicon.log escapes newlines to keep one entry per line; error entries are
/// unfolded again so multi-line tool output reads as the tool wrote it.
fn display_line(line: &str, color: bool) -> String {
    let text = if color { colored(line) } else { clean(line) };
    if line
        .split_once(']')
        .is_some_and(|(kind, _)| kind.contains("error"))
    {
        text.replace("\\n", "\n    ")
    } else {
        text
    }
}
fn emit_log(line: &str, id: &str, path: &Path, color: bool, json: bool) {
    if json {
        println!("{}", json!({"silicon":id,"path":path,"line":line}));
    } else {
        println!("{}", display_line(line, color));
    }
}
// Keep the tail and its byte offset from the same open file so appends between
// a separate tail/read cannot be lost when following begins.
fn tail_snapshot(file: &mut File, count: usize) -> Result<(Vec<String>, u64)> {
    let end = file.metadata()?.len();
    let (mut position, mut lines) = (end, 0);
    let mut chunks = Vec::new();
    while position > 0 && lines <= count {
        let size = position.min(8192) as usize;
        position -= size as u64;
        file.seek(SeekFrom::Start(position))?;
        let mut chunk = vec![0; size];
        file.read_exact(&mut chunk)?;
        lines += chunk.iter().filter(|byte| **byte == b'\n').count();
        chunks.push(chunk);
    }
    let bytes: Vec<_> = chunks.into_iter().rev().flatten().collect();
    let lines = String::from_utf8_lossy(&bytes)
        .lines()
        .rev()
        .take(count)
        .map(str::to_owned)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    file.seek(SeekFrom::Start(end))?;
    Ok((lines, end))
}
fn logs(id: &str, count: usize, follow: bool, json: bool) -> Result<()> {
    let path = log_path(id)?;
    if !follow {
        // tail names the file in its own errors.
        let lines = server::tail(&path, count).context("read the Silicon log")?;
        if json {
            return print_json(&json!({"silicon":id,"path":path,"lines":lines}));
        }
        for line in lines {
            emit_log(&line, id, &path, std::io::stdout().is_terminal(), false);
        }
        println!("silicon {id} · {}", path.display());
        return Ok(());
    }
    let color = std::io::stdout().is_terminal() && !json;
    let mut footer = if color {
        Some(Footer::start(id, &path)?)
    } else {
        eprintln!("silicon {id} · {} · Ctrl-C to exit", path.display());
        None
    };
    let stopping = Arc::new(AtomicBool::new(false));
    let sigint = signal_hook::flag::register(libc::SIGINT, stopping.clone())
        .context("install the Ctrl-C handler for following the log")?;
    let sigterm = signal_hook::flag::register(libc::SIGTERM, stopping.clone())
        .context("install the SIGTERM handler for following the log")?;
    let result = (|| -> Result<()> {
        let mut file: Option<File> = None;
        let mut identity = (0, 0);
        let mut offset = 0;
        let mut pending = Vec::new();
        let mut initial = true;
        while !stopping.load(Ordering::SeqCst) {
            match std::fs::metadata(&path) {
                Ok(metadata) => {
                    let current = (metadata.dev(), metadata.ino());
                    if file.is_none() || identity != current || metadata.len() < offset {
                        let read = || format!("read Silicon log {}", path.display());
                        // A rotated log keeps its last lines in the file already open.
                        if let Some(old) = file.as_mut() {
                            let mut bytes = Vec::new();
                            old.read_to_end(&mut bytes).with_context(read)?;
                            pending.extend(bytes);
                            if !pending.is_empty() && !pending.ends_with(b"\n") {
                                pending.push(b'\n');
                            }
                            for line in String::from_utf8_lossy(&pending).lines() {
                                emit_log(line, id, &path, color, json);
                            }
                        }
                        let mut opened = File::open(&path).with_context(read)?;
                        if initial {
                            let (lines, end) =
                                tail_snapshot(&mut opened, count).with_context(read)?;
                            for line in lines {
                                emit_log(&line, id, &path, color, json);
                            }
                            offset = end;
                            initial = false;
                        } else {
                            offset = 0;
                        }
                        file = Some(opened);
                        identity = current;
                        pending.clear();
                    }
                    if let Some(file) = file.as_mut() {
                        let mut bytes = Vec::new();
                        file.read_to_end(&mut bytes)
                            .with_context(|| format!("read Silicon log {}", path.display()))?;
                        offset += bytes.len() as u64;
                        pending.extend(bytes);
                        let mut consumed = 0;
                        for (i, byte) in pending.iter().enumerate() {
                            if *byte == b'\n' {
                                emit_log(
                                    &String::from_utf8_lossy(&pending[consumed..i]),
                                    id,
                                    &path,
                                    color,
                                    json,
                                );
                                consumed = i + 1;
                            }
                        }
                        pending.drain(..consumed);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => {
                    return Err(error)
                        .with_context(|| format!("read Silicon log {}", path.display()))
                }
            }
            if let Some(footer) = footer.as_mut() {
                footer.draw()?;
            }
            std::io::stdout().flush()?;
            thread::sleep(Duration::from_millis(200));
        }
        Ok(())
    })();
    signal_hook::low_level::unregister(sigint);
    signal_hook::low_level::unregister(sigterm);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn command_shapes_and_archive_filters_preserve_scope() {
        SiliconCli::command().debug_assert();
        SiCli::command().debug_assert();
        for verb in ["install", "uninstall"] {
            assert!(SiCli::try_parse_from(["si", "app", verb, "dm"]).is_ok());
            assert!(SiCli::try_parse_from(["si", "app", verb]).is_err());
        }
        let bare = SiCli::try_parse_from(["si", "isi", "ls", "worker", "--archived"]).unwrap();
        let Some(SiCommand::Isi {
            command: IsiCommand::Ls { archive, .. },
        }) = bare.command
        else {
            panic!()
        };
        assert!(archive.archived.is_some());
        let parsed = SiCli::try_parse_from([
            "si",
            "isi",
            "ls",
            "worker",
            "--archived",
            "*code",
            "01:01:2020-03:01:2020",
            "--archived",
            "design",
            "--timezone",
            "America/Los_Angeles",
        ])
        .unwrap();
        let Some(SiCommand::Isi {
            command: IsiCommand::Ls { archive, .. },
        }) = parsed.command
        else {
            panic!()
        };
        assert_eq!(archive.archived.as_ref().unwrap().len(), 3);
        let rows = json!([
            {"id":"yes","archived_at":"2020-01-04T07:00:00Z","title":"Code work","description":"Design completed"},
            {"id":"late","archived_at":"2020-01-04T08:00:00Z","title":"Code work","description":"Design completed"},
            {"id":"wrong","archived_at":"2020-01-03T00:00:00Z","title":"Code work","description":"Backend"}
        ]);
        let result = filter_sessions(
            rows,
            archive.archived.as_ref().unwrap(),
            archive.timezone.as_deref().unwrap(),
            Utc::now(),
        )
        .unwrap();
        assert_eq!(result.as_array().unwrap().len(), 1);
        assert_eq!(result[0]["id"], "yes");
        assert!(
            SiCli::try_parse_from(["si", "isi", "send", "worker", "hi", "--archived"]).is_err()
        );
        assert!(SiCli::try_parse_from([
            "si",
            "session",
            "new",
            "--archive-current-session",
            "--id",
            "archive",
            "--title",
            "Done",
            "--description",
            "Summary"
        ])
        .is_ok());
        assert!(glob("si:*", true, false).unwrap().is_match("si:hello"));
        assert!(!glob("a*b*c", true, false).unwrap().is_match("ac"));
        assert!(!glob("foo.bar", true, false).unwrap().is_match("fooXbar"));
    }
    #[test]
    fn listed_silicons_show_their_restore_state_and_whole_errors() {
        let row = |extra: Value| {
            let mut row =
                json!({"id":"si:a","yaml":"/a/silicon.yaml","home":"/a","host":"a.o.localhost"});
            row.as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            serde_json::from_value::<server::Listed>(row).unwrap()
        };
        // An older interpreter lists no state: those rows are connected.
        assert_eq!(
            connection_lines(&row(json!({}))),
            ["si:a\thttp://a.o.localhost\t/a/silicon.yaml"]
        );
        assert_eq!(
            connection_lines(&row(json!({"state":"restoring"}))),
            ["si:a\thttp://a.o.localhost\trestoring\t/a/silicon.yaml"]
        );
        let waiting = row(
            json!({"state":"waiting","attempt":3,"retry_at":"2026-09-26T10:00:00+00:00",
            "error":"`honeycomb install ting` failed: exit status: 1\nstderr:\nno network"}),
        );
        assert_eq!(
            connection_lines(&waiting),
            [
                "si:a\thttp://a.o.localhost\twaiting to be restored (attempt 3 failed; next attempt 2026-09-26T10:00:00+00:00)\t/a/silicon.yaml",
                "    `honeycomb install ting` failed: exit status: 1",
                "    stderr:",
                "    no network",
            ]
        );
        let ting = row(json!({"ting_error":"ting: refused","ting_attempt":2,"ting_retry_at":"T"}));
        assert_eq!(
            connection_lines(&ting),
            [
                "si:a\thttp://a.o.localhost\t/a/silicon.yaml",
                "  Ting webhook registration is failing (attempt 2; next attempt T):",
                "    ting: refused",
            ]
        );
        let parsed = SiliconCli::try_parse_from(["silicon", "stop", "--force"]).unwrap();
        assert!(matches!(
            parsed.command,
            Some(SiliconCommand::Stop { force: true })
        ));
    }
    #[test]
    fn tail_snapshot_follows_exact_read_boundary_and_colors_only_prefix() {
        let mut file = tempfile::tempfile().unwrap();
        write!(file, "one\ntwo\nthree\n").unwrap();
        let (lines, end) = tail_snapshot(&mut file, 2).unwrap();
        assert_eq!(lines, vec!["two", "three"]);
        assert_eq!(file.stream_position().unwrap(), end);
        assert!(colored("[error] [worker] [time] [body]").ends_with("\x1b[0m [time] [body]"));
        assert!(!clean("hello\x1b[2J").contains('\x1b'));
    }
    #[test]
    fn error_log_lines_unfold_tool_output_for_people_only() {
        let line = "[error] [worker/cli] [2026-09-25T00:00:00Z] [`bash -c false` failed: exit status: 1\\nstderr:\\n  boom\\nstdout: (empty)]";
        assert_eq!(
            display_line(line, false),
            "[error] [worker/cli] [2026-09-25T00:00:00Z] [`bash -c false` failed: exit status: 1\n    stderr:\n      boom\n    stdout: (empty)]"
        );
        assert!(display_line(line, true)
            .ends_with("exit status: 1\n    stderr:\n      boom\n    stdout: (empty)]"));
        let command = "[command] [worker/cli] [t] [running: printf 'a\\nb']";
        assert_eq!(display_line(command, false), command);
    }
    #[test]
    fn failing_tools_report_their_command_status_and_streams() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join(".silicon/bin");
        std::fs::create_dir_all(&bin).unwrap();
        let tool = |name: &str, script: &str| {
            let path = bin.join(name);
            std::fs::write(&path, format!("#!/bin/sh\n{script}")).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
            path.to_string_lossy().into_owned()
        };
        let gh = tool(
            "gh",
            "echo 'HTTP 401: Bad credentials (https://api.github.com/graphql)' >&2\necho '{\"hint\":\"run gh auth login\"}'\nexit 4\n",
        );
        let error = format!(
            "{:#}",
            file_bug_report(&gh, "Crash", "it broke").unwrap_err()
        );
        assert_eq!(
            error,
            "GitHub CLI could not file the bug report; check gh auth status: \
             `gh issue create --repo teamofsilicons/silicon-stemcell --title Crash --body 'it broke'` failed: exit status: 4\n\
             stderr:\nHTTP 401: Bad credentials (https://api.github.com/graphql)\n\
             stdout:\n{\"hint\":\"run gh auth login\"}"
        );
        let absent = bin.join("absent").to_string_lossy().into_owned();
        let error = format!("{:#}", file_bug_report(&absent, "t", "b").unwrap_err());
        assert!(
            error.contains("could not run `absent issue create"),
            "{error}"
        );
        assert!(error.contains("No such file or directory"), "{error}");
        let open = tool("open", "exit 2\n");
        let error = format!(
            "{:#}",
            open_dashboard(
                &open,
                "http://127.0.0.1:1823/#dashboard-credential",
                "dashboard-credential"
            )
            .unwrap_err()
        );
        assert!(
            error.contains("#[redacted]'` failed: exit status: 2"),
            "{error}"
        );
        assert!(!error.contains("dashboard-credential"), "{error}");
        let error = format!("{:#}", field(&json!({"id": 7}), "id").unwrap_err());
        assert_eq!(
            error,
            "interpreter response has no string \"id\": {\"id\":7}"
        );
        let error = format!(
            "{:#}",
            filter_sessions(json!([]), &["31:02:2020".into()], "UTC", Utc::now()).unwrap_err()
        );
        assert!(
            error.contains(
                "archive date \"31:02:2020\" in filter \"31:02:2020\" must be DD:MM:YYYY: "
            ),
            "{error}"
        );
    }
}
