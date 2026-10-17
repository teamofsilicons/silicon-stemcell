//! Accounts owns token issuance; applications own their exchanged sessions.
use crate::failure;
use crate::process::Limit;
use crate::Recover;
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::{
    cell::{Cell, RefCell},
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Output,
    sync::{Arc, Mutex},
};

/// Exchanges for one Silicon run one at a time: they share its Accounts directory and state
/// files. Silicons never wait for each other, so a slow Accounts or app call holds up only its
/// own home. Keyed by canonical home, so two spellings of one home share a lock.
static AUTH_LOCKS: Mutex<BTreeMap<PathBuf, Arc<Mutex<()>>>> = Mutex::new(BTreeMap::new());

fn canonical(home: &Path) -> PathBuf {
    home.canonicalize().unwrap_or_else(|_| home.to_path_buf())
}

fn home_lock(home: &Path) -> Arc<Mutex<()>> {
    let home = canonical(home);
    let mut locks = AUTH_LOCKS.lock().recover();
    // A home nobody holds or waits on is dropped, so years of connects cannot grow the map.
    locks.retain(|_, lock| Arc::strong_count(lock) > 1);
    locks.entry(home).or_default().clone()
}

/// How long an app whose automatic check failed is left alone. A burst of new sessions
/// then runs its CLI and Accounts sequence, and logs its failure, once instead of per session.
const RETRY_AFTER_SECONDS: i64 = 10 * 60;

/// Which automatic check failed: canonical home, connection generation and check key.
/// A reconnect (say, after fixing silicon.token) is a new generation and checks at once.
type Failure = (PathBuf, Option<uuid::Uuid>, String);

/// When each automatic check last failed (UTC seconds). Wall-clock time: a laptop that
/// slept through the wait does not wait again on waking.
static FAILED: Mutex<BTreeMap<Failure, i64>> = Mutex::new(BTreeMap::new());

fn failed_check(home: &Path, generation: Option<uuid::Uuid>, key: &str) -> Failure {
    (canonical(home), generation, key.to_owned())
}

/// True while an automatic check that failed at most ten minutes ago is left alone.
fn waiting(home: &Path, generation: Option<uuid::Uuid>, key: &str, now: i64) -> bool {
    let failure = failed_check(home, generation, key);
    let mut failed = FAILED.lock().recover();
    failed.retain(|_, at| (0..RETRY_AFTER_SECONDS).contains(&(now - *at)));
    failed.contains_key(&failure)
}

/// A check that succeeded, automatically or explicitly, is not held back in any generation.
fn forgive(home: &Path, key: &str) {
    let home = canonical(home);
    FAILED
        .lock()
        .recover()
        .retain(|(failed, _, failed_key), _| *failed != home || failed_key != key);
}

/// An informational line about work that succeeded. When silicon.log cannot take it the
/// line goes to daemon.log instead; either way the work stands.
fn note(home: &Path, generation: Option<uuid::Uuid>, kind: &str, origin: &str, message: &str) {
    if let Err(error) = crate::log_line_scoped(home, generation, kind, origin, message) {
        crate::stderr_line(&format!(
            "{}\nThe [{kind}] {origin} entry it was recording:\n{}",
            failure::report(&error),
            failure::mask(home, message, &[])
        ));
    }
}

/// A report that would otherwise repeat on every check: logged when it first happens and
/// whenever its text changes. `None` clears it, so a later recurrence is logged again.
fn report(home: &Path, generation: Option<uuid::Uuid>, topic: &str, text: Option<String>) {
    static SAID: Mutex<BTreeMap<(PathBuf, String), String>> = Mutex::new(BTreeMap::new());
    let key = (canonical(home), topic.to_owned());
    let Some(text) = text else {
        SAID.lock().recover().remove(&key);
        return;
    };
    if SAID.lock().recover().insert(key, text.clone()).as_ref() != Some(&text) {
        note(home, generation, "error", "auth", &text);
    }
}

/// State saved after the work it records succeeded. A failed save is reported and the
/// work stands; `consequence` says what the missing record costs.
fn recorded(home: &Path, generation: Option<uuid::Uuid>, consequence: &str, result: Result<()>) {
    let text = result
        .err()
        .map(|error| format!("{}\n{consequence}", failure::report(&error)));
    report(home, generation, consequence, text);
}

fn validate_identity(sid: &str) -> Result<()> {
    if !crate::config::valid_silicon_id(sid) {
        bail!("silicon.id must be si:<handle> (3–30 lowercase letters, digits, underscores or hyphens), got {sid:?}");
    }
    Ok(())
}

/// Missing state is empty. These files only record past checks, grants and registrations,
/// so one that is not valid JSON never blocks a Silicon: it moves aside to
/// `NAME.corrupt-<UTC time>`, the parse error is logged once, and the default is used,
/// which at most checks apps again. A file that cannot be read at all still fails.
fn state<T: serde::de::DeserializeOwned + Default>(
    home: &Path,
    path: &Path,
    what: &str,
) -> Result<T> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(T::default()),
        Err(error) => {
            return Err(error)
                .with_context(|| format!("could not read {what} from {}", path.display()))
        }
    };
    let invalid = match serde_json::from_slice(&bytes) {
        Ok(value) => return Ok(value),
        Err(error) => format!("invalid {what} in {}: {error}", path.display()),
    };
    let aside = crate::numbered(
        path,
        &format!(
            "corrupt-{}",
            chrono::Utc::now().format("%Y%m%dT%H%M%S%.3fZ")
        ),
    );
    let text = match fs::rename(path, &aside) {
        Ok(()) => format!(
            "{invalid}; moved it to {} and continued without it, so apps are checked again",
            aside.display()
        ),
        // Another reader moved it aside first and reported it.
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(T::default()),
        Err(error) => format!(
            "{invalid}; could not move it aside ({error}), so it is ignored until it is repaired or removed"
        ),
    };
    report(home, None, &format!("state {}", path.display()), Some(text));
    Ok(T::default())
}

fn save(path: &Path, value: &impl serde::Serialize) -> Result<()> {
    crate::state::write_json(path, value)
        .with_context(|| format!("could not write {}", path.display()))
}

fn grants(home: &Path) -> Result<BTreeMap<String, String>> {
    state(
        home,
        &home.join(".silicon/auth-grants.json"),
        "app grant identities",
    )
}

fn signed_out(home: &Path) -> Result<BTreeSet<String>> {
    state(
        home,
        &home.join(".silicon/auth-signed-out.json"),
        "signed-out apps",
    )
}

fn set_signed_out(home: &Path, reference: &str, removed: bool) -> Result<()> {
    let mut apps = signed_out(home)?;
    if removed {
        apps.insert(reference.to_owned());
    } else {
        apps.remove(reference);
    }
    save(&home.join(".silicon/auth-signed-out.json"), &apps)
}

/// The managed app registry, read under the home's lock like every write of it, so a
/// corrupt copy is never moved aside while another thread replaces it.
pub(crate) fn registered(home: &Path) -> Result<Vec<String>> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    registry(home)
}

fn registry(home: &Path) -> Result<Vec<String>> {
    let path = home.join(".silicon/auth-apps.json");
    let mut apps: Vec<String> = state(home, &path, "managed app registry")?;
    let original = apps.clone();
    for app in &mut apps {
        match app.as_str() {
            "iam" => *app = "silicon-accounts".into(),
            "honeycomb" => *app = "silicon-apps".into(),
            _ => {}
        }
    }
    let mut seen = BTreeSet::new();
    apps.retain(|app| seen.insert(app.clone()));
    if apps != original {
        save(&path, &apps)?;
    }
    Ok(apps)
}

fn remember(home: &Path, app: &App, present: bool) -> Result<()> {
    let command = app.reference.clone();
    let mut commands = registry(home)?;
    commands.retain(|item| item != &command);
    if present {
        commands.push(command);
    }
    save(&home.join(".silicon/auth-apps.json"), &commands)
}

/// True when a check or grant key (sid, command) belongs to `reference`.
fn keyed_to(key: &str, reference: &str) -> bool {
    serde_json::from_str::<(String, String)>(key).is_ok_and(|(_, command)| command == reference)
}

/// Take `reference` out of the managed app registry and forget its failed automatic
/// checks. Returns whether it was registered.
fn unregister(home: &Path, reference: &str) -> Result<bool> {
    let failed_home = canonical(home);
    FAILED
        .lock()
        .recover()
        .retain(|(failed, _, key), _| *failed != failed_home || !keyed_to(key, reference));
    let mut commands = registry(home)?;
    if !commands.iter().any(|item| item == reference) {
        return Ok(false);
    }
    commands.retain(|item| item != reference);
    save(&home.join(".silicon/auth-apps.json"), &commands)?;
    Ok(true)
}

/// What [`forget`] found to remove.
struct Forgotten {
    registered: bool,
    records: bool,
}

/// [`unregister`], then [`drop_records`]: an app whose sign-out failed is not treated as
/// recently authenticated, so once it runs again its next check asks Accounts afresh instead of
/// trusting a session nobody confirmed.
fn forget(home: &Path, reference: &str) -> Result<Forgotten> {
    let registered = unregister(home, reference)?;
    let records = drop_records(home, reference)?;
    Ok(Forgotten {
        registered,
        records,
    })
}

/// Drop `reference`'s grant and check records for every identity. Returns whether it had any.
fn drop_records(home: &Path, reference: &str) -> Result<bool> {
    let mut records = false;
    for (name, what) in [
        ("auth-grants.json", "app grant identities"),
        ("auth-checked.json", "auth check timestamps"),
    ] {
        let path = home.join(".silicon").join(name);
        let mut kept: BTreeMap<String, Value> = state(home, &path, what)?;
        let before = kept.len();
        kept.retain(|key, _| !keyed_to(key, reference));
        if kept.len() != before {
            save(&path, &kept)?;
            records = true;
        }
    }
    Ok(records)
}

/// The name an app's log lines carry: its ID, or the file name of a command's executable.
fn origin(command: &str) -> String {
    if crate::apps::valid_id(command) {
        return command.to_owned();
    }
    let text = command.trim().trim_start_matches('!').trim();
    let program = shell_words::split(text)
        .ok()
        .and_then(|argv| argv.into_iter().next())
        .unwrap_or_else(|| text.to_owned());
    let name = Path::new(&program)
        .file_name()
        .map_or(program.clone(), |name| name.to_string_lossy().into_owned());
    // A bracket or a line break would add a field to the four-field log line.
    let name: String = name
        .chars()
        .map(|c| {
            if matches!(c, '[' | ']') || c.is_control() {
                '_'
            } else {
                c
            }
        })
        .collect();
    if name.trim().is_empty() {
        "auth".into()
    } else {
        name
    }
}

/// Check managed apps at connect/session creation and let each app refresh its own session.
///
/// One broken app must not stop a Silicon: every app is tried, each failure is logged in
/// full under the app's name, and the app is not tried
/// again automatically for ten minutes within that connection. Sessions start without
/// it; the app's own error then reaches whoever uses it. Explicit `setup` still fails.
/// A check that finds an app signed in under another identity, and cannot sign it in afresh, signs it out; when that fails too,
/// the check fails (see [`close_foreign`]).
pub fn ensure_all(home: &Path, sid: &str, stk: &str, configured: &[String]) -> Result<()> {
    ensure_all_using(home, sid, stk, configured, None)
}

pub fn ensure_all_scoped(
    home: &Path,
    sid: &str,
    stk: &str,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    ensure_all_using(home, sid, stk, configured, Some(generation))
}

fn ensure_all_using(
    home: &Path,
    sid: &str,
    stk: &str,
    configured: &[String],
    generation: Option<uuid::Uuid>,
) -> Result<()> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    validate_identity(sid)?;
    let signed_out = signed_out(home)?;
    let mut commands = registry(home)?;
    let mut configured_commands = Vec::new();
    for command in configured {
        let command = if crate::apps::valid_id(command) {
            command.clone()
        } else {
            // A command that does not parse is checked below like any app, so its
            // failure is reported and retried the same way.
            App::new(home, command).map_or_else(|_| command.clone(), |app| app.reference)
        };
        if !commands.contains(&command) {
            commands.push(command.clone());
        }
        configured_commands.push(command);
    }
    for command in commands {
        let key = serde_json::to_string(&(sid, &command))?;
        let now = chrono::Utc::now().timestamp();
        if signed_out.contains(&command) {
            continue;
        }
        if waiting(home, generation, &key, now) {
            continue;
        }
        let result = setup_locked(
            home,
            sid,
            stk,
            &command,
            Path::new("silicon-accounts"),
            Check::Automatic,
            generation,
        );
        match result {
            Ok(_) => {}
            Err(error) => {
                let (what, error) = match error.downcast::<Foreign>() {
                    // Nothing starts while the app holds a session this Silicon may not use.
                    Ok(Foreign::StillSignedIn(error)) => return Err(error),
                    Ok(Foreign::SignedOut(error)) => (SIGNED_OUT, error),
                    Err(error) => ("automatic credential check failed", error),
                };
                FAILED.lock().recover().insert(
                    failed_check(home, generation, &key),
                    chrono::Utc::now().timestamp(),
                );
                // An app only the registry still lists may be long gone; say how to drop it.
                let unconfigured = if configured_commands.contains(&command) {
                    String::new()
                } else {
                    format!(
                        " (it is in the managed app registry but not in the YAML; `si auth remove {}` unregisters it)",
                        shell_words::quote(&command)
                    )
                };
                note(
                    home,
                    generation,
                    "error",
                    &origin(&command),
                    &format!(
                        "{}\n{what}; sessions start without it, and it is checked again automatically after {} minutes, on the next connect, or by `si auth setup`{unconfigured}",
                        failure::report(&error),
                        RETRY_AFTER_SECONDS / 60
                    ),
                );
            }
        }
    }
    Ok(())
}

/// How a check was asked for. Automatic checks never register an app: the registry holds
/// only what `si auth setup` and app installs added, so removing an app from the YAML is
/// enough to stop checking it.
#[derive(Clone, Copy, PartialEq)]
enum Check {
    /// `setup`: always a fresh Accounts login.
    Fresh,
    /// `ensure`: reuse a current session with a matching grant.
    IfNeeded,
    /// Connect and new sessions: like `IfNeeded` without registering; `ensure_all`
    /// reports its failure instead of raising it, unless it leaves a foreign session open.
    Automatic,
}

/// Why an automatic check signed an app out.
const SIGNED_OUT: &str = "signed out because its session could not be confirmed for this Silicon and a new grant could not be minted";

/// How an automatic check ended that found an app signed in under a session no grant for
/// this identity opened, and could not sign it in afresh.
#[derive(Debug)]
enum Foreign {
    /// It was signed out, so the Silicon carries on with it unauthenticated.
    SignedOut(anyhow::Error),
    /// It could not be signed out either, so the check fails closed.
    StillSignedIn(anyhow::Error),
}

impl std::fmt::Display for Foreign {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::SignedOut(_) => f.write_str(SIGNED_OUT),
            Self::StillSignedIn(_) => {
                f.write_str("the app could not be signed out after its credential check failed")
            }
        }
    }
}

impl std::error::Error for Foreign {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::SignedOut(error) | Self::StillSignedIn(error) => Some(error.as_ref()),
        }
    }
}

struct App {
    home: PathBuf,
    argv: Vec<String>,
    reference: String,
    expected_id: Option<String>,
    ting: Cell<bool>,
    project: RefCell<Option<String>>,
}

#[derive(Clone, Copy)]
enum Contract {
    Auth,
    Login,
}

impl Contract {
    fn status(self) -> &'static [&'static str] {
        match self {
            Self::Auth => &["auth", "status", "--json"],
            Self::Login => &["login", "status", "--json"],
        }
    }
}

fn account_id(state: &Value) -> Option<&str> {
    state
        .get("id")
        .and_then(Value::as_str)
        .or_else(|| state.pointer("/account/id").and_then(Value::as_str))
        .or_else(|| state.pointer("/identity/id").and_then(Value::as_str))
        .or_else(|| state.get("identity").and_then(Value::as_str))
}

fn accounts_login_error(error: anyhow::Error, output: &Output) -> anyhow::Error {
    let invalid_credentials = [&output.stderr, &output.stdout].into_iter().any(|bytes| {
        serde_json::from_slice::<Value>(bytes).is_ok_and(|value| {
            value.pointer("/error/code").and_then(Value::as_str) == Some("invalid_credentials")
        })
    });
    if invalid_credentials {
        failure::explain(error, "Silicon Accounts rejected the supplied credentials.")
    } else {
        error
    }
}

fn app_login_error(error: anyhow::Error, contract: Contract, output: &Output) -> anyhow::Error {
    let unsupported_slt = matches!(contract, Contract::Login)
        && [&output.stderr, &output.stdout].into_iter().any(|bytes| {
            String::from_utf8_lossy(bytes)
                .lines()
                .any(|line| line.trim() == "error: unexpected argument '--slt' found")
        });
    let error = if unsupported_slt {
        failure::explain(
            error,
            "This app does not support `login --slt`; it rejected the login command syntax.",
        )
    } else {
        error
    };
    error.context("app login failed after Silicon Accounts issued a token")
}

impl App {
    fn new(home: &Path, command: &str) -> Result<Self> {
        let explicit = command.trim().starts_with('!');
        let command = command
            .trim()
            .strip_prefix('!')
            .unwrap_or(command.trim())
            .trim();
        // Mask before quoting: escaping a quote or backslash would hide a secret from the mask.
        let quoted = || format!("{:?}", failure::mask(home, command, &[]));
        let argv = shell_words::split(command)
            .with_context(|| format!("invalid quoting in app command {}", quoted()))?;
        if argv.first().is_none_or(|arg| arg.is_empty())
            || argv.iter().any(|arg| arg.contains('\0'))
        {
            bail!(
                "app command {} must contain an executable and valid arguments",
                quoted()
            );
        }
        let home = home
            .canonicalize()
            .with_context(|| format!("SILICON_HOME {} must exist", home.display()))?;
        let accounts_home = home.join(".accounts");
        match fs::symlink_metadata(&accounts_home) {
            Ok(metadata) if metadata.file_type().is_symlink() => bail!(
                "the Silicon Accounts credential directory {} must not be a symlink",
                accounts_home.display()
            ),
            // Missing is fine: authentication creates it.
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                return Err(error).with_context(|| {
                    format!(
                        "could not inspect the Silicon Accounts credential directory {}",
                        accounts_home.display()
                    )
                });
            }
            _ => {}
        }
        let reference = if explicit {
            format!("! {}", shell_words::join(&argv))
        } else {
            shell_words::join(&argv)
        };
        Ok(Self {
            home,
            argv,
            reference,
            expected_id: None,
            ting: Cell::new(false),
            project: RefCell::new(None),
        })
    }

    /// A canonical app ID bound to the command that answered as it.
    fn installed(home: &Path, id: &str, path: &Path) -> Result<Self> {
        let mut app = Self::new(home, &shell_words::quote(&path.to_string_lossy()))?;
        app.reference = id.to_owned();
        app.expected_id = Some(id.to_owned());
        app.ting.set(id == "ting");
        Ok(app)
    }

    fn resolve(home: &Path, command: &str) -> Result<Self> {
        if !crate::apps::valid_id(command) {
            return Self::new(home, command);
        }
        let path = crate::apps::resolve(home, command)?.map_err(|passed_over| {
            unresolved(home, command, &passed_over).context(format!(
                "Accounts app {command} is not installed or does not answer as {command}; install it with `si app install {command}`"
            ))
        })?;
        Self::installed(home, command, &path)
    }

    /// The command line failures name: the executable, its configured arguments, then `args`.
    fn show(&self, args: &[&str], secrets: &[&str]) -> String {
        let all: Vec<&str> = self.argv[1..]
            .iter()
            .map(String::as_str)
            .chain(args.iter().copied())
            .collect();
        masked_argv(&self.home, &self.argv[0], &all, secrets)
    }

    fn failed(&self, args: &[&str], output: &Output, secrets: &[&str]) -> anyhow::Error {
        failure::with_project(
            failure::command(&self.home, &self.show(args, secrets), output, secrets),
            self.project.borrow().as_deref(),
        )
    }

    fn answered(
        &self,
        args: &[&str],
        problem: &str,
        output: &Output,
        secrets: &[&str],
    ) -> anyhow::Error {
        failure::with_project(
            failure::answer(
                &self.home,
                &self.show(args, secrets),
                problem,
                output,
                secrets,
            ),
            self.project.borrow().as_deref(),
        )
    }

    fn run(&self, args: &[&str]) -> Result<Output> {
        self.run_secret(args, &[])
    }

    /// `secrets` are values in `args`; they are masked wherever the command is named.
    fn run_secret(&self, args: &[&str], secrets: &[&str]) -> Result<Output> {
        self.execute(args, secrets, Limit::App)
    }

    fn execute(&self, args: &[&str], secrets: &[&str], limit: Limit) -> Result<Output> {
        if self.ting.get() {
            disable_ting_reporting(&self.home)?;
        }
        // Configured commands are argv, never shell programs.
        let mut command = crate::command(&self.argv[0], &self.home);
        command.args(&self.argv[1..]).args(args);
        crate::process::output(&mut command, limit)
            .map_err(|error| {
                failure::with_project(
                    failure::spawn(&self.home, &self.show(args, secrets), &error),
                    self.project.borrow().as_deref(),
                )
            })
            .with_context(|| {
                format!(
                    "could not execute Accounts app {}; check the executable and SILICON_HOME {}",
                    self.argv[0],
                    self.home.display()
                )
            })
    }

    fn discover(&self) -> Result<String> {
        self.project.replace(None);
        const ASK: &[&str] = &["accounts", "--json"];
        let output = self.run(ASK)?;
        if !output.status.success() {
            return Err(self.failed(ASK, &output, &[]).context(
                "Accounts app must answer `accounts --json`; update the app to the Silicon Accounts CLI contract",
            ));
        }
        let info: Value = serde_json::from_slice(&output.stdout).map_err(|error| {
            self.answered(
                ASK,
                &format!("returned invalid JSON: {error}"),
                &output,
                &[],
            )
        })?;
        let app_id = match info.get("app_id") {
            Some(Value::String(id)) if crate::apps::valid_id(id) => id.as_str(),
            found => {
                let found = found.map_or("no app_id".to_owned(), |id| format!("app_id {id}"));
                return Err(self.answered(
                    ASK,
                    &format!("returned {found}, but must return a Silicon Apps app_id"),
                    &output,
                    &[],
                ));
            }
        };
        if let Some(expected) = self.expected_id.as_deref().filter(|id| *id != app_id) {
            return Err(self.answered(
                ASK,
                &format!("reports app_id {app_id:?}, but Accounts app {expected:?} was requested"),
                &output,
                &[],
            ));
        }
        self.ting.set(app_id == "ting");
        self.project.replace(
            info.get("repository_url")
                .and_then(Value::as_str)
                .or_else(|| info.get("repository").and_then(Value::as_str))
                .map(str::to_owned),
        );
        Ok(app_id.to_owned())
    }

    fn status(&self) -> Result<(Contract, bool, Option<String>)> {
        let mut failure = None;
        for contract in [Contract::Login, Contract::Auth] {
            let args = contract.status();
            let output = self.run(args)?;
            let state = serde_json::from_slice::<Value>(&output.stdout);
            let authenticated = state
                .as_ref()
                .ok()
                .and_then(|s| s.get("authenticated"))
                .and_then(Value::as_bool);
            let identity = state.as_ref().ok().and_then(account_id).map(str::to_owned);
            let error = match (authenticated, state) {
                // Some apps use a nonzero status to report an expired session.
                (Some(authenticated), _) if !authenticated || output.status.success() => {
                    return Ok((contract, authenticated, identity));
                }
                (Some(_), _) => self.answered(
                    args,
                    "reported authenticated true but did not exit successfully",
                    &output,
                    &[],
                ),
                (None, _) if !output.status.success() => self.failed(args, &output, &[]),
                (None, Err(error)) => self.answered(
                    args,
                    &format!("returned invalid JSON: {error}"),
                    &output,
                    &[],
                ),
                (None, Ok(_)) => self.answered(
                    args,
                    "returned no boolean authenticated field",
                    &output,
                    &[],
                ),
            };
            failure = Some(match failure {
                Some(first) => failure::also(first, Err(error)),
                None => error,
            });
        }
        Err(failure.expect("both status contracts were checked").context(
            "Accounts app must support `login status --json` or `auth status --json` with a boolean authenticated field; neither worked",
        ))
    }

    /// `secrets` were handed to the app earlier in this exchange; its status may echo them.
    fn check_status(
        &self,
        contract: Contract,
        expected: bool,
        identities: &[&str],
        secrets: &[&str],
    ) -> Result<()> {
        let args = contract.status();
        let output = self.run_secret(args, secrets)?;
        let problem = match serde_json::from_slice::<Value>(&output.stdout) {
            Ok(state)
                if state.get("authenticated").and_then(Value::as_bool) == Some(expected)
                    && (!expected || output.status.success())
                    && (!expected
                        || account_id(&state).is_some_and(|id| identities.contains(&id))) =>
            {
                return Ok(());
            }
            Ok(_) => format!("did not confirm authenticated {expected}"),
            Err(error) => format!("returned invalid JSON: {error}"),
        };
        Err(self.answered(args, &problem, &output, secrets))
    }
}

/// Older Ting releases read this preference for CLI events and every daemon delivery.
pub(crate) fn disable_ting_reporting(home: &Path) -> Result<()> {
    use std::os::{fd::AsRawFd, unix::fs::OpenOptionsExt};
    let directory = home.join(".ting");
    let path = directory.join("settings.json");
    let read_settings = || -> Result<serde_json::Map<String, Value>> {
        match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice(&bytes).with_context(|| {
                format!(
                    "read Ting settings {} before disabling reporting",
                    path.display()
                )
            }),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Default::default()),
            Err(error) => {
                Err(error).with_context(|| format!("read Ting settings {}", path.display()))
            }
        }
    };
    if read_settings()?.get("telemetry") == Some(&Value::Bool(false)) {
        return Ok(());
    }
    crate::state::private_dir(&directory)?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(directory.join("profile.lock"))
        .context("open Ting profile lock before disabling reporting")?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(std::io::Error::last_os_error())
            .context("Ting profile is busy; could not disable reporting before running Ting");
    }
    let mut settings = read_settings()?;
    if settings.get("telemetry") == Some(&Value::Bool(false)) {
        return Ok(());
    }
    settings.insert("telemetry".into(), Value::Bool(false));
    crate::state::write_json(&path, &settings).context("disable Ting reporting before running Ting")
}

/// `program` and `args` as failures name them. Arguments are masked before quoting,
/// because quoting escapes characters and a quoted secret would no longer match.
fn masked_argv(
    home: &Path,
    program: impl AsRef<std::ffi::OsStr>,
    args: &[&str],
    secrets: &[&str],
) -> String {
    let args: Vec<String> = args
        .iter()
        .map(|arg| failure::mask(home, arg, secrets))
        .collect();
    failure::argv(program, &args)
}

/// Every string inside `value`, for masking a value that is a credential as a whole.
fn strings(value: &Value) -> Vec<&str> {
    match value {
        Value::String(text) => vec![text.as_str()],
        Value::Array(items) => items.iter().flat_map(strings).collect(),
        Value::Object(fields) => fields.values().flat_map(strings).collect(),
        _ => Vec::new(),
    }
}

/// Where resolution looked, then what each candidate it passed over said instead.
fn unresolved(home: &Path, id: &str, passed_over: &[String]) -> anyhow::Error {
    let mut text = format!(
        "no executable named {id} in {} or on PATH answered `{id} accounts --json` with app_id {id:?}, and this home's Silicon Apps registry has no command for it",
        home.join(".silicon/bin").display()
    );
    for reason in passed_over {
        text.push_str(&format!("\n{reason}"));
    }
    anyhow!(text)
}

/// Log an application in using this Silicon's credential, never a Carbon session.
/// Returns only the public application ID, not the issued token.
pub fn setup(home: &Path, sid: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, stk, app, Path::new("silicon-accounts"), false)
}

pub fn setup_scoped(
    home: &Path,
    sid: &str,
    stk: &str,
    app: &str,
    generation: uuid::Uuid,
) -> Result<String> {
    setup_using_scoped(
        home,
        sid,
        stk,
        app,
        Path::new("silicon-accounts"),
        false,
        Some(generation),
    )
}

/// Check an application's session immediately, bypassing automatic check timestamps.
pub fn ensure(home: &Path, sid: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, stk, app, Path::new("silicon-accounts"), true)
}

fn setup_using(
    home: &Path,
    sid: &str,
    stk: &str,
    command: &str,
    accounts: &Path,
    only_if_needed: bool,
) -> Result<String> {
    setup_using_scoped(home, sid, stk, command, accounts, only_if_needed, None)
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity context without an auth wrapper.
fn setup_using_scoped(
    home: &Path,
    sid: &str,
    stk: &str,
    command: &str,
    accounts: &Path,
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    setup_locked(
        home,
        sid,
        stk,
        command,
        accounts,
        if only_if_needed {
            Check::IfNeeded
        } else {
            Check::Fresh
        },
        generation,
    )
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity context without an auth wrapper.
fn setup_locked(
    home: &Path,
    sid: &str,
    stk: &str,
    command: &str,
    accounts: &Path,
    check: Check,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    validate_identity(sid)?;
    let app = if crate::apps::valid_id(command) {
        let path = match crate::apps::resolve(home, command)? {
            Ok(path) => {
                note(
                    home,
                    generation,
                    "app",
                    command,
                    "installed command available",
                );
                path
            }
            // Anything passed over is already in silicon.log; Silicon Apps may still provide it.
            Err(_) => {
                crate::apps::step(
                    home,
                    generation,
                    &format!("Installing {command} through Silicon Apps"),
                    &format!("Installed {command}"),
                    || crate::apps::install_at(home, command),
                )?;
                note(
                    home,
                    generation,
                    "app",
                    command,
                    "installed through Silicon Apps",
                );
                crate::apps::resolve(home, command)?.map_err(|passed_over| {
                    unresolved(home, command, &passed_over).context(format!(
                        "Silicon Apps installed {command}, but none of its commands answers as that Accounts app"
                    ))
                })?
            }
        };
        App::installed(home, command, &path)?
    } else {
        App::new(home, command)?
    };
    authenticate(home, sid, stk, app, accounts, check, generation)
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity context without an auth wrapper.
fn authenticate(
    home: &Path,
    sid: &str,
    stk: &str,
    app: App,
    accounts: &Path,
    check: Check,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    // Progress names the app handle or executable; failures name the whole command.
    let label = app.expected_id.clone().unwrap_or_else(|| {
        Path::new(&app.argv[0])
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned()
    });
    // Set once an automatic check finds the app signed in under a grant it may not reuse.
    let mut foreign = false;
    let result = crate::apps::step(
        home,
        generation,
        &format!("Authenticating {label}"),
        &format!("Authenticated {label}"),
        || {
            authenticate_inner(
                home,
                sid,
                stk,
                &app,
                accounts,
                check,
                generation,
                &mut foreign,
            )
        },
    );
    match result {
        Err(error) if foreign => Err(close_foreign(home, &app, &label, error)),
        result => result,
    }
}

/// An automatic check found `app` signed in under a session that no grant for this identity
/// opened (another identity's, or one nobody recorded),
/// and could not sign it in afresh (`error`). That session must not stay usable: the app is
/// signed out as `remove` signs apps out and its grant and check records are dropped, so
/// the Silicon carries on with it unauthenticated. If it cannot be signed out, the check
/// fails closed, and connect or the new session fails with both errors.
fn close_foreign(home: &Path, app: &App, label: &str, error: anyhow::Error) -> anyhow::Error {
    anyhow::Error::new(match app.sign_out() {
        Ok(()) => Foreign::SignedOut(failure::also(
            error,
            drop_records(home, &app.reference).map(drop).context(
                "its stale grant records could not be dropped",
            ),
        )),
        Err(signing_out) => Foreign::StillSignedIn(failure::also(
            error.context(format!(
                "{label} is signed in with a session that could not be confirmed for this Silicon, a new grant could not be minted, and it could not be signed out, so connect and new sessions fail until it is signed in afresh or signed out"
            )),
            Err(signing_out.context("signing it out failed")),
        )),
    })
}

/// The app answered as authenticated: what is left is bookkeeping, and a failed log line is
/// reported without undoing the authentication. Registering the app is what an explicit
/// setup is for, so failing to register it fails that setup (the app stays signed in).
fn confirmed(
    home: &Path,
    generation: Option<uuid::Uuid>,
    app: &App,
    app_id: &str,
    key: &str,
    check: Check,
    message: &str,
) -> Result<()> {
    forgive(home, key);
    note(home, generation, "auth", app_id, message);
    if check == Check::Automatic {
        return Ok(());
    }
    set_signed_out(home, &app.reference, false)?;
    remember(home, app, true).with_context(|| {
        format!(
            "{app_id} is signed in, but could not be added to the managed app registry, so automatic checks skip it unless it is configured; run the setup again once the registry can be written"
        )
    })
}

/// `foreign` is set once an automatic check passes over a signed-in session it may not reuse.
#[allow(clippy::too_many_arguments)] // Keep explicit identity context without an auth wrapper.
fn authenticate_inner(
    home: &Path,
    sid: &str,
    stk: &str,
    app: &App,
    accounts: &Path,
    check: Check,
    generation: Option<uuid::Uuid>,
    foreign: &mut bool,
) -> Result<String> {
    let app_id = app.discover()?;
    let (contract, authenticated, identity) = app.status()?;
    let mut grants = grants(home)?;
    let key = serde_json::to_string(&(sid, &app.reference))?;
    if authenticated && identity.is_none() {
        *foreign = true;
        bail!("{app_id} reports an authenticated session without an account identity; update the app to the Silicon Accounts CLI contract");
    }
    let belongs_to_silicon = identity.as_deref().is_some_and(|owner| {
        owner == sid || grants.get(&key).is_some_and(|recorded| recorded == owner)
    });
    if check != Check::Fresh && authenticated && belongs_to_silicon {
        if !grants.contains_key(&key) {
            grants.insert(key.clone(), sid.to_owned());
            save(&home.join(".silicon/auth-grants.json"), &grants)?;
        }
        confirmed(
            home,
            generation,
            app,
            &app_id,
            &key,
            check,
            "already authenticated",
        )?;
        return Ok(app_id);
    }
    // An ended session must stay ended until an explicit setup; never undo a user's logout.
    if check == Check::Automatic && !authenticated && grants.contains_key(&key) {
        set_signed_out(home, &app.reference, true)?;
        note(
            home,
            generation,
            "auth",
            &app_id,
            "session ended; run si auth setup to sign in again",
        );
        return Ok(app_id);
    }
    *foreign = check == Check::Automatic && authenticated && !belongs_to_silicon;
    if stk.trim().is_empty() || stk == "..." || stk.contains('\0') {
        bail!("silicon.token is required to authenticate {app_id} with Silicon Accounts; provide this Silicon's STK");
    }
    if app_id == "silicon-accounts" {
        let mut args = vec!["login", "--silicon", sid, "--json"];
        if authenticated && !belongs_to_silicon {
            args.push("--force");
        }
        let mut login = crate::command(&app.argv[0], home);
        login
            .args(&app.argv[1..])
            .args(&args)
            .env("ACCOUNTS_STK", stk);
        let output = crate::process::output(&mut login, Limit::App).map_err(|error| {
            failure::with_project(
                failure::spawn(home, &app.show(&args, &[stk]), &error),
                app.project.borrow().as_deref(),
            )
        })?;
        if !output.status.success() {
            return Err(accounts_login_error(
                app.failed(&args, &output, &[stk]),
                &output,
            ));
        }
        if let Err(error) = app.check_status(contract, true, &[sid], &[stk]) {
            *foreign = true;
            return Err(error.context("could not confirm the configured Silicon Accounts login"));
        }
        grants.insert(key.clone(), sid.to_owned());
        save(&home.join(".silicon/auth-grants.json"), &grants)?;
        set_signed_out(home, &app.reference, false)?;
        confirmed(home, generation, app, &app_id, &key, check, "authenticated")?;
        return Ok(app_id);
    }
    let home = home
        .canonicalize()
        .with_context(|| format!("SILICON_HOME {} must exist", home.display()))?;
    let accounts_home = home.join(".accounts");
    fs::create_dir_all(&accounts_home)
        .with_context(|| format!("could not create {}", accounts_home.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&accounts_home, fs::Permissions::from_mode(0o700))
            .with_context(|| format!("could not make {} private", accounts_home.display()))?;
    }
    let invoke = |args: &[&str]| {
        let mut issuer = crate::command(accounts, &home);
        issuer.args(args).env("ACCOUNTS_STK", stk);
        crate::process::output(&mut issuer, Limit::App)
    };
    let status_args = ["login", "status", "--json"];
    let status_output = match invoke(&status_args) {
        Err(error)
            if error.kind() == std::io::ErrorKind::NotFound
                && accounts == Path::new("silicon-accounts") =>
        {
            crate::apps::install_at(&home, "silicon-accounts")
                .context("install the Silicon Accounts CLI through Silicon Apps")?;
            invoke(&status_args)
        }
        result => result,
    }
    .map_err(|error| {
        failure::spawn(
            &home,
            &masked_argv(&home, accounts, &status_args, &[stk]),
            &error,
        )
    })?;
    let status_shown = masked_argv(&home, accounts, &status_args, &[stk]);
    if !status_output.status.success() {
        return Err(failure::command(
            &home,
            &status_shown,
            &status_output,
            &[stk],
        ));
    }
    let status: Value = serde_json::from_slice(&status_output.stdout).map_err(|error| {
        failure::answer(
            &home,
            &status_shown,
            &format!("returned invalid JSON: {error}"),
            &status_output,
            &[stk],
        )
    })?;
    let authenticated = status
        .get("authenticated")
        .and_then(Value::as_bool)
        .ok_or_else(|| {
            failure::answer(
                &home,
                &status_shown,
                "returned no boolean authenticated field",
                &status_output,
                &[stk],
            )
        })?;
    let mut mint = vec!["login", "--silicon", sid, "--app", &app_id, "--json"];
    if authenticated && account_id(&status) != Some(sid) {
        mint.push("--force");
    }
    let shown = masked_argv(&home, accounts, &mint, &[stk]);
    let output = invoke(&mint).map_err(|error| failure::spawn(&home, &shown, &error))?;
    if !output.status.success() {
        return Err(accounts_login_error(
            failure::command(&home, &shown, &output, &[stk]),
            &output,
        )
        .context(format!(
            "Silicon Accounts could not issue the application token for {app_id}"
        )));
    }
    let issued: Value = serde_json::from_slice(&output.stdout).map_err(|error| {
        failure::answer(
            &home,
            &shown,
            &format!("returned invalid JSON: {error}"),
            &output,
            &[stk],
        )
    })?;
    let slt = match issued.get("slt") {
        Some(Value::String(slt))
            if slt.starts_with("slt_")
                && !slt.chars().any(|c| c.is_whitespace() || c.is_control()) =>
        {
            slt.as_str()
        }
        found => {
            let mut secrets = found.map(strings).unwrap_or_default();
            secrets.push(stk);
            return Err(failure::answer(
                &home,
                &shown,
                "returned no valid slt_ token",
                &output,
                &secrets,
            ));
        }
    };
    let expires = issued
        .get("expires_at")
        .and_then(Value::as_str)
        .and_then(|value| chrono::DateTime::parse_from_rfc3339(value).ok());
    let now = chrono::Utc::now();
    if issued.get("app_id").and_then(Value::as_str) != Some(&app_id)
        || expires.is_none_or(|at| at <= now || at > now + chrono::Duration::seconds(130))
    {
        return Err(failure::answer(
            &home,
            &shown,
            "returned the wrong app_id or an invalid short-lived token expiry",
            &output,
            &[stk, slt],
        ));
    }
    let account_output =
        invoke(&status_args).map_err(|error| failure::spawn(&home, &status_shown, &error))?;
    let account: Value = serde_json::from_slice(&account_output.stdout).map_err(|error| {
        failure::answer(
            &home,
            &status_shown,
            &format!("returned invalid JSON: {error}"),
            &account_output,
            &[stk, slt],
        )
    })?;
    let account_uuid = account
        .get("uuid")
        .and_then(Value::as_str)
        .filter(|id| uuid::Uuid::parse_str(id).is_ok());
    if !account_output.status.success()
        || account.get("authenticated").and_then(Value::as_bool) != Some(true)
        || account_id(&account) != Some(sid)
        || account_uuid.is_none()
    {
        return Err(failure::answer(
            &home,
            &status_shown,
            "did not confirm the configured Silicon account and UUID after token issuance",
            &account_output,
            &[stk, slt],
        ));
    }
    let account_uuid = account_uuid.unwrap();
    let args = match contract {
        Contract::Auth => vec!["auth", "token", slt],
        Contract::Login => vec!["login", "--slt", slt],
    };
    let secrets = [slt, stk];
    let output = app.run_secret(&args, &secrets)?;
    if !output.status.success() {
        return Err(app_login_error(
            app.failed(&args, &output, &secrets),
            contract,
            &output,
        ));
    }
    if let Err(error) = app.check_status(contract, true, &[sid, account_uuid], &secrets) {
        *foreign = true;
        return Err(error.context("could not confirm the app signed in as the configured Silicon after accepting its short-lived token"));
    }
    grants.insert(key.clone(), account_uuid.to_owned());
    recorded(
        &home,
        generation,
        "the app's account could not be recorded",
        save(&home.join(".silicon/auth-grants.json"), &grants),
    );
    set_signed_out(&home, &app.reference, false)?;
    confirmed(
        &home,
        generation,
        app,
        &app_id,
        &key,
        check,
        "authenticated",
    )?;
    Ok(app_id)
}

/// Sign out and remember the choice, including for apps still configured in silicon.yaml.
/// Only an explicit setup or ensure signs the app in again.
pub fn remove(home: &Path, command: &str) -> Result<()> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    // The registry entry is known without running anything. An entry is removable as
    // written even when it is no longer a valid command.
    let listed = registry(home).is_ok_and(|commands| commands.iter().any(|item| item == command));
    let reference = if listed || crate::apps::valid_id(command) {
        command.to_owned()
    } else {
        App::new(home, command)?.reference
    };
    set_signed_out(home, &reference, true)?;
    let error = match App::resolve(home, command).and_then(|app| app.sign_out()) {
        Ok(()) => {
            return unregister(home, &reference)
                .map(drop)
                .with_context(|| format!("{reference} signed out, but could not be unregistered"))
        }
        Err(error) => error,
    };
    match forget(home, &reference) {
        Ok(forgotten) => Err(error.context(match forgotten {
            Forgotten { registered: true, records: true } => format!(
                "{reference} was unregistered and its grant and check records dropped, but its credentials were not confirmed removed"
            ),
            Forgotten { registered: true, records: false } => format!(
                "{reference} was unregistered, but its credentials were not confirmed removed"
            ),
            Forgotten { registered: false, records: true } => format!(
                "{reference} was not registered; its grant and check records were dropped, but its credentials were not confirmed removed"
            ),
            Forgotten { registered: false, records: false } => format!(
                "{reference} is not registered and has no grant or check records, and it could not be signed out"
            ),
        })),
        Err(forgotten) => Err(failure::also(
            error,
            Err(forgotten.context(format!(
                "{reference} could not be unregistered, or its grant and check records dropped"
            ))),
        )),
    }
}

impl App {
    /// Sign out with the app's advertised logout command and confirm it; an app that is
    /// not signed in is left as it is.
    fn sign_out(&self) -> Result<()> {
        self.discover()?;
        let (contract, authenticated, _) = self.status()?;
        if !authenticated {
            return Ok(());
        }
        let candidates: &[&[&str]] = match contract {
            Contract::Auth => &[&["auth", "remove"], &["auth", "logout"], &["logout"]],
            Contract::Login => &[&["logout"], &["auth", "remove"], &["auth", "logout"]],
        };
        let mut unsupported = None;
        for args in candidates {
            let mut help = args.to_vec();
            help.push("--help");
            let output = self.run(&help)?;
            if !output.status.success() {
                let error = self.failed(&help, &output, &[]);
                unsupported = Some(match unsupported {
                    Some(first) => failure::also(first, Err(error)),
                    None => error,
                });
                continue;
            }
            let output = self.run(args)?;
            if !output.status.success() {
                return Err(self
                    .failed(args, &output, &[])
                    .context("app logout failed; credentials were not confirmed removed"));
            }
            return self
                .check_status(contract, false, &[], &[])
                .context("could not confirm the app signed out after logout");
        }
        Err(unsupported.expect("logout candidates were checked").context(
            "Accounts app must expose `auth remove`, `auth logout`, or `logout`; every help check failed",
        ))
    }
}

pub(crate) fn run_app(home: &Path, app: &str, args: &[&str]) -> Result<Output> {
    let limit = if app == "ting" {
        Limit::Ting
    } else {
        Limit::App
    };
    App::resolve(home, app)?.execute(args, &[], limit)
}

/// Apply app configuration for an explicit `si app install`, which fails on any rejection.
pub(crate) fn configure(home: &Path, configs: &BTreeMap<String, Value>) -> Result<()> {
    for (id, config) in configs {
        configure_app(home, id, config)
            .with_context(|| format!("could not apply silicon.app_configs.{id} to {id}"))?;
    }
    Ok(())
}

/// Apply every app's configuration at connect and restore. Like a failed automatic check,
/// one app that cannot run or rejects its configuration does not stop the Silicon: its
/// failure is logged in full under its name, the other apps are still configured, and the
/// app keeps whatever configuration it last accepted until the next connect.
pub fn configure_all(home: &Path, configs: &BTreeMap<String, Value>, generation: uuid::Uuid) {
    for (id, config) in configs {
        if let Err(error) = configure_app(home, id, config) {
            note(
                home,
                Some(generation),
                "error",
                &origin(id),
                &format!(
                    "{}\ncould not apply silicon.app_configs.{id} to {id}; the Silicon connects without it, the app keeps the configuration it last accepted, and the next connect applies it again",
                    failure::report(&error)
                ),
            );
        }
    }
}

/// Config values are app secrets: a failure shows the app's full answer with the config
/// JSON masked; its individual strings are masked as this home's registered secrets.
fn configure_app(home: &Path, id: &str, config: &Value) -> Result<()> {
    let mut config = config.clone();
    if id == "ting" {
        disable_ting_reporting(home)?;
        if let Some(fields) = config.as_object_mut() {
            fields.remove("telemetry");
            fields.remove("telemetry.enabled");
            if fields.is_empty() {
                return Ok(());
            }
        }
    }
    let value = serde_json::to_string(&config)?;
    let app = App::resolve(home, id)?;
    let args = ["config", "set", value.as_str()];
    let output = app.run_secret(&args, &[&value])?;
    if !output.status.success() {
        return Err(app.failed(&args, &output, &[&value]));
    }
    Ok(())
}

#[cfg(test)]
mod login_error_tests {
    use super::*;
    use std::os::unix::process::ExitStatusExt;

    fn output(stderr: &str, stdout: &str) -> Output {
        Output {
            status: std::process::ExitStatus::from_raw(2 << 8),
            stderr: stderr.as_bytes().to_vec(),
            stdout: stdout.as_bytes().to_vec(),
        }
    }

    #[test]
    fn unsupported_slt_explains_the_cli_mismatch_and_keeps_masked_diagnostics() {
        let output = output(
            "error: unexpected argument '--slt' found\nUsage: spotify login [OPTIONS] [SLT]\nopaque-login-secret",
            "additional login details",
        );
        let app = App::new(Path::new("."), "spotify").unwrap();
        let error = app_login_error(
            app.failed(
                &["login", "--slt", "opaque-login-secret"],
                &output,
                &["opaque-login-secret"],
            ),
            Contract::Login,
            &output,
        );
        let text = failure::report(&error);
        assert!(text.contains("does not support `login --slt`"));
        assert!(text.contains("Silicon Accounts issued a token"));
        assert!(text.contains("error: unexpected argument '--slt' found"));
        assert!(text.contains("Usage: spotify login [OPTIONS] [SLT]"));
        assert!(text.contains("additional login details"));
        assert!(text.contains("exit status: 2"));
        assert!(text.contains("[redacted]"));
        assert!(!text.contains("opaque-login-secret"));
        assert!(text.find("error: unexpected argument").unwrap() < text.find("Source:").unwrap());
        assert!(text.find("Source:").unwrap() < text.find("Meaning:").unwrap());
        assert!(text.find("Meaning:").unwrap() < text.find("Via:").unwrap());
    }

    #[test]
    fn other_login_failures_do_not_claim_a_syntax_or_credential_rejection() {
        for (contract, diagnostic) in [
            (Contract::Login, "network unavailable"),
            (
                Contract::Login,
                "error: unexpected argument '--other' found\nUsage: login --slt TOKEN",
            ),
            (Contract::Auth, "error: unexpected argument '--slt' found"),
        ] {
            let output = output(diagnostic, "");
            let error = app_login_error(
                failure::command(Path::new("."), "test-app login", &output, &[]),
                contract,
                &output,
            );
            let text = failure::report(&error);
            assert!(text.contains(diagnostic));
            assert!(text.contains("Source: `test-app login`"));
            assert!(text.contains("app login failed after Silicon Accounts issued a token"));
            assert!(!text.contains("Meaning:"));
            assert!(!text.contains("does not support"));
            assert!(!text.contains("rejected"));
        }
    }

    #[test]
    fn invalid_accounts_credentials_explain_only_the_known_rejection() {
        let diagnostic = r#"{"error":{"code":"invalid_credentials","message":"Account rejected opaque-account-secret"}}"#;
        for (stderr, stdout) in [(diagnostic, ""), ("", diagnostic)] {
            let output = output(stderr, stdout);
            let error = accounts_login_error(
                failure::command(
                    Path::new("."),
                    "silicon-accounts login",
                    &output,
                    &["opaque-account-secret"],
                ),
                &output,
            );
            let text = failure::report(&error);
            assert!(text.contains("Meaning: Silicon Accounts rejected the supplied credentials."));
            assert!(!text.contains("belong to the same Silicon"));
            assert!(!text.contains("silicon.yaml"));
            assert!(text.contains("invalid_credentials"));
            assert!(text.contains("Account rejected [redacted]"));
            assert!(text.contains("Project: https://github.com/teamofsilicons/silicon-accounts"));
            assert!(text.find("invalid_credentials").unwrap() < text.find("Source:").unwrap());
            assert!(text.find("Source:").unwrap() < text.find("Meaning:").unwrap());
            assert!(!text.contains("opaque-account-secret"));
        }
        for diagnostic in [
            r#"{"error":{"code":"service_unavailable","message":"Try again"}}"#,
            r#"{"error":{"code":"network_error","message":"invalid_credentials"}}"#,
            "invalid_credentials",
        ] {
            let output = output(diagnostic, "");
            assert_eq!(
                accounts_login_error(anyhow!("original failure"), &output).to_string(),
                "original failure"
            );
        }
    }

    #[test]
    fn discovered_repository_links_follow_command_answer_and_spawn_failures() {
        use std::os::unix::fs::PermissionsExt;

        for field in ["repository_url", "repository"] {
            let home =
                std::env::temp_dir().join(format!("silicon-auth-project-{}", uuid::Uuid::new_v4()));
            fs::create_dir_all(&home).unwrap();
            let binary = home.join("renamed-app");
            let project = "https://github.com/example/custom-app";
            let info = serde_json::json!({"app_id": "error-test-app", field: project});
            fs::write(
                &binary,
                format!(
                    "#!/bin/sh\nprintf '%s\\n' {}\n",
                    shell_words::quote(&info.to_string())
                ),
            )
            .unwrap();
            fs::set_permissions(&binary, fs::Permissions::from_mode(0o700)).unwrap();
            let app = App::installed(&home, "error-test-app", &binary).unwrap();
            assert_eq!(app.discover().unwrap(), "error-test-app");

            let status = app.status().err().unwrap();
            let text = failure::report(&status);
            assert!(text.contains("login status --json"));
            assert!(text.contains("auth status --json"));
            assert!(text.contains(&format!("Project: {project}")));
            assert!(text.find("error-test-app").unwrap() < text.find("Source:").unwrap());

            let output = output("raw app failure with opaque-app-secret", "extra details");
            for error in [
                app.failed(&["login"], &output, &["opaque-app-secret"]),
                app.answered(
                    &["login"],
                    "returned invalid JSON",
                    &output,
                    &["opaque-app-secret"],
                ),
            ] {
                let text = failure::report(&error);
                assert!(text.contains(&format!("Project: {project}")));
                assert!(text.contains("raw app failure with [redacted]"));
                assert!(text.contains("extra details"));
                assert!(!text.contains("opaque-app-secret"));
                assert!(text.find("raw app failure").unwrap() < text.find("Project:").unwrap());
            }
            fs::remove_file(&binary).unwrap();
            let error = app.execute(&["login"], &[], Limit::App).unwrap_err();
            let text = failure::report(&error);
            assert!(text.contains(&format!("Project: {project}")));
            assert!(text.contains("could not execute Accounts app"));
            fs::remove_dir_all(home).unwrap();
        }
    }

    #[test]
    fn invalid_discovery_identity_cannot_supply_a_project_link() {
        use std::os::unix::fs::PermissionsExt;

        let home =
            std::env::temp_dir().join(format!("silicon-auth-project-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&home).unwrap();
        let binary = home.join("renamed-app");
        fs::write(&binary, "#!/bin/sh\nprintf '%s\\n' '{\"app_id\":\"another-app\",\"repository\":\"https://github.com/example/another-app\"}'\n").unwrap();
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o700)).unwrap();
        let app = App::installed(&home, "error-test-app", &binary).unwrap();
        let error = app.discover().unwrap_err();
        assert!(!failure::report(&error).contains("Project:"));
        assert!(app.project.borrow().is_none());
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn foreign_session_wrapper_preserves_the_command_diagnostic() {
        let output = output("original auth rejection", "");
        let error = anyhow::Error::new(Foreign::SignedOut(failure::command(
            Path::new("."),
            "silicon-accounts login",
            &output,
            &[],
        )));
        let text = failure::report(&error);
        assert!(text.find("original auth rejection").unwrap() < text.find("Source:").unwrap());
        assert!(text.contains("Project: https://github.com/teamofsilicons/silicon-accounts"));
        assert!(text.contains(SIGNED_OUT));
        assert!(error.downcast_ref::<Foreign>().is_some());
    }
}

#[cfg(test)]
mod reporting_tests {
    use super::*;

    #[test]
    fn legacy_ting_reporting_is_forced_off_and_other_settings_survive() {
        let home =
            std::env::temp_dir().join(format!("silicon-ting-reporting-{}", uuid::Uuid::new_v4()));
        let path = home.join(".ting/settings.json");
        disable_ting_reporting(&home).unwrap();
        let initial: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(initial["telemetry"], false);
        crate::state::write_json(
            &path,
            &serde_json::json!({"telemetry":true,"future":{"value":42}}),
        )
        .unwrap();
        disable_ting_reporting(&home).unwrap();
        let settings: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            settings,
            serde_json::json!({"telemetry":false,"future":{"value":42}})
        );
        use std::os::fd::AsRawFd;
        let lock = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(home.join(".ting/profile.lock"))
            .unwrap();
        assert_eq!(unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) }, 0);
        disable_ting_reporting(&home).unwrap();
        crate::state::write_json(&path, &serde_json::json!({"telemetry":true})).unwrap();
        assert!(disable_ting_reporting(&home).is_err());
        let settings: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(settings["telemetry"], true);
        drop(lock);
        crate::state::write_json(&path, &serde_json::json!({"telemetry":false})).unwrap();
        configure_app(
            &home,
            "ting",
            &serde_json::json!({"telemetry.enabled": true, "telemetry": {"enabled":true}}),
        )
        .unwrap();
        let settings: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(settings["telemetry"], false);
        fs::write(&path, "invalid JSON").unwrap();
        assert!(disable_ting_reporting(&home).is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), "invalid JSON");
        fs::remove_dir_all(&home).unwrap();
    }
}
