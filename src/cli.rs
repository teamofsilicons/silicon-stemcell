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
    /// Validate all configuration and flow expressions without connecting.
    Compile { yaml: PathBuf },
    /// Compile and connect a silicon.yaml; start the interpreter if needed.
    Connect { yaml: PathBuf },
    /// Disconnect by silicon id or YAML path. Without a target, list choices.
    Disconnect { target: Option<String> },
    /// List connected Silicons, optionally matching a quoted glob such as '*:org'.
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
    /// Stop the interpreter and its child processes.
    Stop,
    /// Install a newer stable GitHub release into a managed bundle installation.
    Update,
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
            let cfg = server::compile(yaml)?;
            let value = server::call(&server::daemon(true)?, "connect", json!({"yaml":cfg.path}))?;
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
            let value = server::call(
                &server::daemon(false)?,
                "disconnect",
                json!({"target":target}),
            )?;
            if cli.json {
                print_json(&value)?;
            } else {
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
                .filter(|row| filter.is_match(&row.id))
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
        SiliconCommand::Stop => {
            let result = server::call(&server::daemon(false)?, "shutdown", json!({}))?;
            if cli.json {
                print_json(&result)?;
            } else {
                println!("interpreter stopping");
            }
        }
        SiliconCommand::Update => {
            let installed = crate::update::install_latest()?;
            if cli.json {
                print_json(
                    &json!({"updated": installed.is_some(), "executable": installed, "restart_required": installed.is_some()}),
                )?;
            } else if installed.is_some() {
                println!(
                    "updated; restart the interpreter with `silicon stop`, then `silicon serve`"
                );
            } else {
                println!("Silicon {} is current", env!("CARGO_PKG_VERSION"));
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
                let status = std::process::Command::new(program)
                    .arg(&url)
                    .status()
                    .context("could not open browser; use silicon web --no-open")?;
                if !status.success() {
                    bail!("browser launcher failed; use silicon web --no-open");
                }
                println!("opened Silicon dashboard");
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
                &json!({"isi":std::env::var("ISI").ok(),"services":["auth","isi","session"]}),
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
fn connections() -> Result<Vec<server::Connection>> {
    let Ok(daemon) = server::daemon(false) else {
        return Ok(Vec::new());
    };
    serde_json::from_value(server::call(&daemon, "list", json!({}))?)
        .context("invalid connection list")
}
fn print_connections(rows: &[server::Connection]) {
    if rows.is_empty() {
        println!("No Silicons connected.");
    }
    for row in rows {
        println!("{}\thttp://{}\t{}", row.id, row.host, row.yaml.display());
    }
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
    value[key]
        .as_str()
        .ok_or_else(|| anyhow!("interpreter response is missing {key}"))
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
        .context("invalid glob")
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
    let timezone: Tz = timezone
        .parse()
        .context("archive timezone must be an IANA timezone")?;
    let mut parsed = Vec::new();
    for filter in filters.iter().map(|s| s.trim()).filter(|s| !s.is_empty()) {
        if filter.contains(':')
            && filter
                .chars()
                .all(|c| c.is_ascii_digit() || c == ':' || c == '-')
        {
            let (first, last) = filter.split_once('-').unwrap_or((filter, filter));
            let first = NaiveDate::parse_from_str(first, "%d:%m:%Y")
                .context("archive dates use DD:MM:YYYY")?;
            let last = NaiveDate::parse_from_str(last, "%d:%m:%Y")
                .context("archive dates use DD:MM:YYYY")?;
            if first > last {
                bail!("archive range start must not be after its end");
            }
            parsed.push(ArchiveFilter::Dates(first, last));
        } else {
            parsed.push(ArchiveFilter::Text(glob(filter, false, true)?));
        }
    }
    let records = value
        .as_array()
        .ok_or_else(|| anyhow!("interpreter returned an invalid session list"))?;
    let mut found = Vec::new();
    for record in records {
        let timestamp = DateTime::parse_from_rfc3339(field(record, "archived_at")?)
            .context("invalid session archive timestamp")?;
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
    let rows = value
        .as_array()
        .ok_or_else(|| anyhow!("invalid session list"))?;
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
    if let Some(connection) = server::saved()?
        .into_iter()
        .find(|connection| connection.id == id)
    {
        return Ok(connection.home.join(".silicon/silicon.log"));
    }
    let value = server::call(&server::daemon(false)?, "logs", json!({"silicon":id}))?;
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
fn emit_log(line: &str, id: &str, path: &Path, color: bool, json: bool) {
    if json {
        println!("{}", json!({"silicon":id,"path":path,"line":line}));
    } else if color {
        println!("{}", colored(line));
    } else {
        println!("{}", clean(line));
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
        let lines = server::tail(&path, count)?;
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
    let sigint = signal_hook::flag::register(libc::SIGINT, stopping.clone())?;
    let sigterm = signal_hook::flag::register(libc::SIGTERM, stopping.clone())?;
    let result = (|| -> Result<()> {
        let mut file = None;
        let mut identity = (0, 0);
        let mut offset = 0;
        let mut pending = Vec::new();
        let mut initial = true;
        while !stopping.load(Ordering::SeqCst) {
            match std::fs::metadata(&path) {
                Ok(metadata) => {
                    let current = (metadata.dev(), metadata.ino());
                    if file.is_none() || identity != current || metadata.len() < offset {
                        let mut opened = File::open(&path)?;
                        if initial {
                            let (lines, end) = tail_snapshot(&mut opened, count)?;
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
                        file.read_to_end(&mut bytes)?;
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
                Err(error) => return Err(error).context("read Silicon log"),
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
        assert!(glob("*:org", true, false).unwrap().is_match("hello:org"));
        assert!(!glob("a*b*c", true, false).unwrap().is_match("ac"));
        assert!(!glob("foo.bar", true, false).unwrap().is_match("fooXbar"));
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
}
