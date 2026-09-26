//! IAM owns token issuance; applications own their exchanged sessions.
use crate::failure;
use crate::process::Limit;
use crate::Recover;
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Output,
    sync::{Arc, Mutex},
};

/// Exchanges for one Silicon run one at a time: they share its IAM directory and state
/// files. Silicons never wait for each other, so a slow IAM or app call holds up only its
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
/// then runs its CLI and IAM sequence, and logs its failure, once instead of per session.
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
            "{error:#}; the [{kind}] {origin} entry it was recording: {}",
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
        .map(|error| format!("{error:#}; {consequence}"));
    report(home, generation, consequence, text);
}

fn validate_identity(sid: &str, org_id: &str) -> Result<()> {
    if !crate::config::valid_silicon_id(sid) {
        bail!("silicon.id must be si:<handle>, got {sid:?}; migrate old IDs using IAM's verified mapping");
    }
    if !crate::config::valid_org(org_id) {
        bail!(
            "silicon.org_id must be an explicit canonical IAM organization handle, got {org_id:?}"
        );
    }
    Ok(())
}

fn selected_org(home: &Path, default: &str) -> Result<String> {
    let command = crate::command("iam", home);
    let org = command
        .get_envs()
        .find(|(key, _)| *key == "SILICON_ORG")
        .and_then(|(_, value)| value.map(|value| value.to_owned()))
        .or_else(|| std::env::var_os("SILICON_ORG"))
        .map(|value| {
            value
                .into_string()
                .map_err(|value| anyhow!("SILICON_ORG must be UTF-8, got {value:?}"))
        })
        .transpose()?
        .unwrap_or_else(|| default.to_owned());
    if !crate::config::valid_org(&org) {
        bail!(
            "SILICON_ORG must be a canonical IAM organization handle, got {org:?} (from {} or the environment)",
            home.join(".silicon/org.json").display()
        );
    }
    Ok(org)
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
        "app grant organizations",
    )
}

/// The managed app registry, read under the home's lock like every write of it, so a
/// corrupt copy is never moved aside while another thread replaces it.
pub(crate) fn registered(home: &Path) -> Result<Vec<String>> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    registry(home)
}

fn registry(home: &Path) -> Result<Vec<String>> {
    state(
        home,
        &home.join(".silicon/auth-apps.json"),
        "managed app registry",
    )
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

/// True when a check or grant key (sid, org, command) belongs to `reference`.
fn keyed_to(key: &str, reference: &str) -> bool {
    serde_json::from_str::<(String, String, String)>(key)
        .is_ok_and(|(_, _, command)| command == reference)
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
/// recently authenticated, so once it runs again its next check asks IAM afresh instead of
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
        ("auth-grants.json", "app grant organizations"),
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

/// Check managed apps at connect/session creation, reusing successful checks for 48 hours.
///
/// One broken app must not stop a Silicon: every app is tried, each failure is logged in
/// full under the app's name and leaves its timestamp alone, and the app is not tried
/// again automatically for ten minutes within that connection. Sessions start without
/// it; the app's own error then reaches whoever uses it. Explicit `setup` still fails.
/// A check that finds an app signed in under a session no grant for this identity and
/// organization opened, and cannot sign it in afresh, signs it out; when that fails too,
/// the check fails (see [`close_foreign`]).
pub fn ensure_all(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    configured: &[String],
) -> Result<()> {
    ensure_all_using(home, sid, org_id, stk, configured, None)
}

pub fn ensure_all_scoped(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    ensure_all_using(home, sid, org_id, stk, configured, Some(generation))
}

fn ensure_all_using(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    configured: &[String],
    generation: Option<uuid::Uuid>,
) -> Result<()> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    validate_identity(sid, org_id)?;
    let grant_org = selected_org(home, org_id)?;
    let grants = grants(home)?;
    let path = home.join(".silicon/auth-checked.json");
    let mut checked: BTreeMap<String, i64> = state(home, &path, "auth check timestamps")?;
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
        let key = serde_json::to_string(&(sid, org_id, &command))?;
        let now = chrono::Utc::now().timestamp();
        if grants.get(&key).map(String::as_str) == Some(grant_org.as_str())
            && checked
                .get(&key)
                .and_then(|last| now.checked_sub(*last))
                .is_some_and(|age| (0..48 * 60 * 60).contains(&age))
        {
            continue;
        }
        if waiting(home, generation, &key, now) {
            continue;
        }
        let result = setup_locked(
            home,
            sid,
            org_id,
            stk,
            &command,
            Path::new("iam"),
            Check::Automatic,
            generation,
        );
        match result {
            Ok(_) => {
                checked.insert(key, chrono::Utc::now().timestamp());
                recorded(
                    home,
                    generation,
                    "the next automatic check runs it again",
                    save(&path, &checked),
                );
            }
            Err(error) => {
                let (what, error) = match error.downcast::<Foreign>() {
                    // Nothing starts while the app holds a session this Silicon may not use.
                    Ok(Foreign::StillSignedIn(error)) => return Err(error),
                    Ok(Foreign::SignedOut(error)) => {
                        // Its records were dropped; saving a later check must not restore them.
                        checked.retain(|key, _| !keyed_to(key, &command));
                        (SIGNED_OUT, error)
                    }
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
                        "{what}; sessions start without it, and it is checked again automatically after {} minutes, on the next connect, or by `si auth setup`{unconfigured}: {error:#}",
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
    /// `setup`: always a fresh IAM login.
    Fresh,
    /// `ensure`: reuse a current session with a matching grant.
    IfNeeded,
    /// Connect and new sessions: like `IfNeeded` without registering; `ensure_all`
    /// reports its failure instead of raising it, unless it leaves a foreign session open.
    Automatic,
}

/// Why an automatic check signed an app out, and how its log line begins.
const SIGNED_OUT: &str = "signed out because its session belonged to another identity or organization and a new grant could not be minted";

/// How an automatic check ended that found an app signed in under a session no grant for
/// this identity and organization opened, and could not sign it in afresh.
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
            Self::SignedOut(error) => write!(f, "{SIGNED_OUT}: {error:#}"),
            Self::StillSignedIn(error) => write!(f, "{error:#}"),
        }
    }
}

impl std::error::Error for Foreign {}

struct App {
    home: PathBuf,
    argv: Vec<String>,
    reference: String,
    expected_id: Option<String>,
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
        if !explicit
            && command.contains('>')
            && !command.contains('/')
            && !command.chars().any(char::is_whitespace)
        {
            bail!(
                "legacy org>app ID {} requires migration using IAM's verified mapping",
                quoted()
            );
        }
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
        let iam_home = home.join(".silicon-iam");
        match fs::symlink_metadata(&iam_home) {
            Ok(metadata) if metadata.file_type().is_symlink() => bail!(
                "the Silicon IAM credential directory {} must not be a symlink",
                iam_home.display()
            ),
            // Missing is fine: authentication creates it.
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                return Err(error).with_context(|| {
                    format!(
                        "could not inspect the Silicon IAM credential directory {}",
                        iam_home.display()
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
        })
    }

    /// A canonical app ID bound to the command that answered as it.
    fn installed(home: &Path, id: &str, path: &Path) -> Result<Self> {
        let mut app = Self::new(home, &shell_words::quote(&path.to_string_lossy()))?;
        app.reference = id.to_owned();
        app.expected_id = Some(id.to_owned());
        Ok(app)
    }

    fn resolve(home: &Path, command: &str) -> Result<Self> {
        if !crate::apps::valid_id(command) {
            return Self::new(home, command);
        }
        let path = crate::apps::resolve(home, command)?.map_err(|passed_over| {
            unresolved(home, command, &passed_over).context(format!(
                "IAM app {command} is not installed or does not answer as {command}; install it with `si app install {command}`"
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
        failure::command(&self.home, &self.show(args, secrets), output, secrets)
    }

    fn answered(
        &self,
        args: &[&str],
        problem: &str,
        output: &Output,
        secrets: &[&str],
    ) -> anyhow::Error {
        failure::answer(
            &self.home,
            &self.show(args, secrets),
            problem,
            output,
            secrets,
        )
    }

    fn run(&self, args: &[&str]) -> Result<Output> {
        self.run_in_org(args, None, &[])
    }

    /// `secrets` are values in `args`; they are masked wherever the command is named.
    fn run_in_org(&self, args: &[&str], org: Option<&str>, secrets: &[&str]) -> Result<Output> {
        self.execute(args, org, secrets, Limit::App)
    }

    fn execute(
        &self,
        args: &[&str],
        org: Option<&str>,
        secrets: &[&str],
        limit: Limit,
    ) -> Result<Output> {
        // Configured commands are argv, never shell programs.
        let mut command = crate::command(&self.argv[0], &self.home);
        if let Some(org) = org {
            command.env("SPACE_STATION_ORG", org);
        }
        command.args(&self.argv[1..]).args(args);
        crate::process::output(&mut command, limit)
            .map_err(|error| failure::spawn(&self.home, &self.show(args, secrets), &error))
            .with_context(|| {
                format!(
                    "could not execute IAM app {}; check the executable and SILICON_HOME {}",
                    self.argv[0],
                    self.home.display()
                )
            })
    }

    fn discover(&self) -> Result<String> {
        const ASK: &[&str] = &["iam", "--json"];
        let output = self.run(ASK)?;
        if !output.status.success() {
            return Err(self.failed(ASK, &output, &[]).context(
                "IAM app must answer `iam --json`; update the app to the Silicon IAM CLI contract",
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
                    &format!("returned {found}, but must return a bare app_id; update or migrate the app using IAM's verified mapping"),
                    &output,
                    &[],
                ));
            }
        };
        if let Some(expected) = self.expected_id.as_deref().filter(|id| *id != app_id) {
            return Err(self.answered(
                ASK,
                &format!("reports app_id {app_id:?}, but IAM app {expected:?} was requested"),
                &output,
                &[],
            ));
        }
        Ok(app_id.to_owned())
    }

    fn status(&self) -> Result<(Contract, bool)> {
        let mut answers = Vec::new();
        for contract in [Contract::Login, Contract::Auth] {
            let args = contract.status();
            let output = self.run(args)?;
            let state = serde_json::from_slice::<Value>(&output.stdout);
            let authenticated = state
                .as_ref()
                .ok()
                .and_then(|s| s.get("authenticated"))
                .and_then(Value::as_bool);
            let error = match (authenticated, state) {
                // Some apps use a nonzero status to report an expired session.
                (Some(authenticated), _) if !authenticated || output.status.success() => {
                    return Ok((contract, authenticated));
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
            answers.push(format!("{error:#}"));
        }
        bail!(
            "IAM app must support `login status --json` or `auth status --json` with a boolean authenticated field; neither worked:\n{}",
            answers.join("\n")
        );
    }

    /// `secrets` were handed to the app earlier in this exchange; its status may echo them.
    fn check_status(&self, contract: Contract, expected: bool, secrets: &[&str]) -> Result<()> {
        let args = contract.status();
        let output = self.run_in_org(args, None, secrets)?;
        let problem = match serde_json::from_slice::<Value>(&output.stdout) {
            Ok(state)
                if state.get("authenticated").and_then(Value::as_bool) == Some(expected)
                    && (!expected || output.status.success()) =>
            {
                return Ok(());
            }
            Ok(_) => format!("did not confirm authenticated {expected}"),
            Err(error) => format!("returned invalid JSON: {error}"),
        };
        Err(self.answered(args, &problem, &output, secrets))
    }
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
        "no executable named {id} in {} or on PATH answered `{id} iam --json` with app_id {id:?}, and this home's Honeycomb registry has no command for it",
        home.join(".silicon/bin").display()
    );
    for reason in passed_over {
        text.push_str(&format!("\n{reason}"));
    }
    anyhow!(text)
}

/// Log an application in using this Silicon's credential, never a Carbon session.
/// Returns only the public application ID, not the issued token.
pub fn setup(home: &Path, sid: &str, org_id: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, org_id, stk, app, Path::new("iam"), false)
}

pub fn setup_scoped(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    app: &str,
    generation: uuid::Uuid,
) -> Result<String> {
    setup_using_scoped(
        home,
        sid,
        org_id,
        stk,
        app,
        Path::new("iam"),
        false,
        Some(generation),
    )
}

/// Check an application's session immediately, bypassing automatic check timestamps.
pub fn ensure(home: &Path, sid: &str, org_id: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, org_id, stk, app, Path::new("iam"), true)
}

fn setup_using(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    command: &str,
    iam: &Path,
    only_if_needed: bool,
) -> Result<String> {
    setup_using_scoped(home, sid, org_id, stk, command, iam, only_if_needed, None)
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity/organization context without an auth wrapper.
fn setup_using_scoped(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    command: &str,
    iam: &Path,
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    setup_locked(
        home,
        sid,
        org_id,
        stk,
        command,
        iam,
        if only_if_needed {
            Check::IfNeeded
        } else {
            Check::Fresh
        },
        generation,
    )
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity/organization context without an auth wrapper.
fn setup_locked(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    command: &str,
    iam: &Path,
    check: Check,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    validate_identity(sid, org_id)?;
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
            // Anything passed over is already in silicon.log; Honeycomb may still provide it.
            Err(_) => {
                crate::apps::step(
                    home,
                    generation,
                    &format!("Installing {command} through Honeycomb"),
                    &format!("Installed {command}"),
                    || crate::apps::install_at(home, command),
                )?;
                note(
                    home,
                    generation,
                    "app",
                    command,
                    "installed through Honeycomb",
                );
                crate::apps::resolve(home, command)?.map_err(|passed_over| {
                    unresolved(home, command, &passed_over).context(format!(
                        "Honeycomb installed {command}, but none of its commands answers as that IAM app"
                    ))
                })?
            }
        };
        App::installed(home, command, &path)?
    } else {
        App::new(home, command)?
    };
    authenticate(home, sid, org_id, stk, app, iam, check, generation)
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity/organization context without an auth wrapper.
fn authenticate(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    app: App,
    iam: &Path,
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
                org_id,
                stk,
                &app,
                iam,
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
/// and organization opened (another identity's or organization's, or one nobody recorded),
/// and could not sign it in afresh (`error`). That session must not stay usable: the app is
/// signed out as `remove` signs apps out and its grant and check records are dropped, so
/// the Silicon carries on with it unauthenticated. If it cannot be signed out, the check
/// fails closed, and connect or the new session fails with both errors.
fn close_foreign(home: &Path, app: &App, label: &str, error: anyhow::Error) -> anyhow::Error {
    anyhow::Error::new(match app.sign_out() {
        Ok(()) => Foreign::SignedOut(failure::also(
            error,
            drop_records(home, &app.reference).map(drop).context(
                "its grant and check records could not be dropped, so an identity they name may skip its automatic check for up to 48 hours while it is signed out",
            ),
        )),
        Err(signing_out) => Foreign::StillSignedIn(failure::also(
            error.context(format!(
                "{label} is signed in with a session that belongs to another identity or organization, a new grant could not be minted, and it could not be signed out, so connect and new sessions fail until it is signed in afresh or signed out"
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
    remember(home, app, true).with_context(|| {
        format!(
            "{app_id} is signed in, but could not be added to the managed app registry, so automatic checks skip it unless it is configured; run the setup again once the registry can be written"
        )
    })
}

/// `foreign` is set once an automatic check passes over a signed-in session it may not reuse.
#[allow(clippy::too_many_arguments)] // Keep explicit identity/organization context without an auth wrapper.
fn authenticate_inner(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    app: &App,
    iam: &Path,
    check: Check,
    generation: Option<uuid::Uuid>,
    foreign: &mut bool,
) -> Result<String> {
    let only_if_needed = check != Check::Fresh;
    let app_id = app.discover()?;
    let (contract, authenticated) = app.status()?;
    let grant_org = selected_org(home, org_id)?;
    let mut grants = grants(home)?;
    let key = serde_json::to_string(&(sid, org_id, &app.reference))?;
    // Only a grant recorded for this exact canonical identity and explicit owner
    // can reuse a session. Legacy identity/cache entries require a fresh IAM login.
    if only_if_needed && authenticated && grants.get(&key) == Some(&grant_org) {
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
    // The app's session was opened under another identity's or organization's grant (or
    // one nobody recorded); an automatic check must not leave it usable if what follows fails.
    *foreign = check == Check::Automatic && authenticated;
    let unusable = if stk.trim().is_empty() {
        Some("is empty")
    } else if stk == "..." {
        Some("is the ... placeholder")
    } else if stk.contains('\0') {
        Some("contains a NUL byte")
    } else {
        None
    };
    if let Some(unusable) = unusable {
        let needed = if !only_if_needed {
            "a fresh login was requested".to_owned()
        } else if !authenticated {
            format!("{app_id} reports it is not authenticated")
        } else {
            format!("no IAM grant for organization {grant_org} is recorded")
        };
        bail!("silicon.token is required to authenticate {app_id} with IAM ({needed}), but it {unusable}");
    }
    // IAM uses SILICON_IAM_HOME, not SILICON_HOME. Reject a redirected
    // credential directory instead of ever touching the user's personal IAM state.
    let home = home
        .canonicalize()
        .with_context(|| format!("SILICON_HOME {} must exist", home.display()))?;
    let iam_home = home.join(".silicon-iam");
    fs::create_dir_all(&iam_home).with_context(|| {
        format!(
            "could not create Silicon IAM credential directory {}",
            iam_home.display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&iam_home, fs::Permissions::from_mode(0o700))
            .with_context(|| format!("could not make {} private", iam_home.display()))?;
    }
    // This is the installed IAM CLI's noninteractive contract. Only IAM gets
    // the STK; only the single-use SLT crosses the application boundary.
    let mut issuer = crate::command(iam, &home);
    // Discover CLI capabilities without constraining the installed version.
    const HELP: &[&str] = &["silicon-login", "--help"];
    let inspect = || crate::process::output(crate::command(iam, &home).args(HELP), Limit::App);
    let unstarted =
        |error: std::io::Error| failure::spawn(&home, &failure::argv(iam, HELP), &error);
    let help = match inspect() {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && iam == Path::new("iam") => {
            crate::apps::install_at(&home, "iam").with_context(|| {
                format!(
                    "the iam CLI is not on PATH ({}), and installing it through Honeycomb failed",
                    unstarted(error)
                )
            })?;
            inspect()
                .map_err(unstarted)
                .context("Honeycomb installed the iam CLI, but it still cannot be run")
        }
        result => result.map_err(unstarted),
    }
    .context("could not inspect IAM's silicon-login contract; install the iam CLI")?;
    let approve =
        help.status.success() && String::from_utf8_lossy(&help.stdout).contains("--approve-scopes");
    // Minting still runs and reports its own failure; this explains a missing --approve-scopes.
    let probe = (!help.status.success()).then(|| {
        let error = failure::command(&home, &failure::argv(iam, HELP), &help, &[]);
        format!(
            "could not inspect silicon-login options, minting without --approve-scopes: {error:#}"
        )
    });
    if let Some(probe) = &probe {
        note(&home, generation, "error", "iam", probe);
    }
    let mut mint = vec![
        "--output",
        "json",
        "--org",
        org_id,
        "silicon-login",
        "--sid",
        sid,
        "--stk",
        stk,
        "--app-id",
        &app_id,
        "--grant-org",
        &grant_org,
    ];
    if approve {
        mint.push("--approve-scopes");
    }
    // The STK rides in argv, so it is masked wherever this command is named.
    let shown = masked_argv(&home, iam, &mint, &[stk]);
    issuer
        .args(&mint)
        .current_dir(&home)
        .env("SILICON_HOME", &home)
        .env("SILICON_IAM_HOME", &iam_home);
    let output = crate::process::output(&mut issuer, Limit::App)
        .map_err(|error| failure::spawn(&home, &shown, &error))
        .context("could not execute IAM; install the iam CLI")?;
    if !output.status.success() {
        let error = failure::command(&home, &shown, &output, &[stk]).context(format!(
            "IAM could not mint the application token for {app_id}; check the Silicon credential, application ID, and this Silicon's IAM environment configuration"
        ));
        return Err(match probe {
            Some(probe) => anyhow!("{error:#}\nalso: {probe}"),
            None => error,
        });
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
            if !slt.is_empty() && !slt.chars().any(|c| c.is_whitespace() || c.is_control()) =>
        {
            slt.as_str()
        }
        found => {
            let problem = match found {
                None => "returned no slt".to_owned(),
                Some(Value::String(slt)) if slt.is_empty() => "returned an empty slt".to_owned(),
                Some(Value::String(_)) => {
                    "returned an slt containing whitespace or control characters".to_owned()
                }
                Some(other) => format!("returned a non-string slt: {other}"),
            };
            // Whatever IAM put under `slt` is its credential, whatever its shape.
            let mut secrets = found.map(strings).unwrap_or_default();
            secrets.push(stk);
            return Err(failure::answer(&home, &shown, &problem, &output, &secrets));
        }
    };
    let expires_in = issued.get("expires_in");
    if expires_in
        .and_then(Value::as_u64)
        .is_none_or(|seconds| seconds == 0 || seconds > 120)
    {
        let found = expires_in.map_or("nothing".to_owned(), Value::to_string);
        return Err(failure::answer(
            &home,
            &shown,
            &format!("returned an invalid short-lived token lifetime: expires_in must be 1 to 120 seconds, got {found}"),
            &output,
            &[stk, slt],
        ));
    }
    let args = match contract {
        Contract::Auth => vec!["auth", "token", slt],
        Contract::Login => vec!["login", slt],
    };
    // Space Station binds terminal sessions to an organization, including a first login.
    let app_org = (app_id == "spacestation").then_some(grant_org.as_str());
    let secrets = [slt, stk];
    let output = app.run_in_org(&args, app_org, &secrets)?;
    if !output.status.success() {
        // Never retry another login spelling with a possibly consumed SLT.
        return Err(app.failed(&args, &output, &secrets).context(
            "app rejected the IAM short-lived token; start authentication again after fixing the app's login failure",
        ));
    }
    app.check_status(contract, true, &secrets).context(
        "could not confirm the app signed in after it accepted the IAM short-lived token",
    )?;
    grants.insert(key.clone(), grant_org);
    recorded(
        &home,
        generation,
        "the app's IAM grant is not recorded, so its next check asks IAM for a new one",
        save(&home.join(".silicon/auth-grants.json"), &grants),
    );
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

/// Remove application credentials using its advertised logout command.
///
/// After a clean sign-out the app leaves the managed registry, and its check and grant
/// records stay: a configured app is authenticated again only once its cached check
/// expires. When it cannot run (missing, broken, unpublished, a legacy entry) or its
/// logout fails, it still leaves the registry, its grant and check records go too, and the
/// error says what could not be done; otherwise a broken app could never be removed.
pub fn remove(home: &Path, command: &str) -> Result<()> {
    let lock = home_lock(home);
    let _guard = lock.lock().recover();
    // The registry entry is known without running anything. An entry is removable as
    // written even when it is no longer a valid command, such as a legacy `org>app` ID.
    let listed = registry(home).is_ok_and(|commands| commands.iter().any(|item| item == command));
    let reference = if listed || crate::apps::valid_id(command) {
        command.to_owned()
    } else {
        App::new(home, command)?.reference
    };
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
        let (contract, authenticated) = self.status()?;
        if !authenticated {
            return Ok(());
        }
        let candidates: &[&[&str]] = match contract {
            Contract::Auth => &[&["auth", "remove"], &["auth", "logout"], &["logout"]],
            Contract::Login => &[&["logout"], &["auth", "remove"], &["auth", "logout"]],
        };
        let mut unsupported = Vec::new();
        for args in candidates {
            let mut help = args.to_vec();
            help.push("--help");
            let output = self.run(&help)?;
            if !output.status.success() {
                unsupported.push(format!("{:#}", self.failed(&help, &output, &[])));
                continue;
            }
            let output = self.run(args)?;
            if !output.status.success() {
                return Err(self
                    .failed(args, &output, &[])
                    .context("app logout failed; credentials were not confirmed removed"));
            }
            return self
                .check_status(contract, false, &[])
                .context("could not confirm the app signed out after logout");
        }
        bail!(
            "IAM app must expose `auth remove`, `auth logout`, or `logout`; every help check failed:\n{}",
            unsupported.join("\n")
        );
    }
}

pub(crate) fn run_app(home: &Path, app: &str, args: &[&str]) -> Result<Output> {
    let limit = if app == "ting" {
        Limit::Ting
    } else {
        Limit::App
    };
    App::resolve(home, app)?.execute(args, None, &[], limit)
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
                    "could not apply silicon.app_configs.{id} to {id}; the Silicon connects without it, the app keeps the configuration it last accepted, and the next connect applies it again: {error:#}"
                ),
            );
        }
    }
}

/// Config values are app secrets: a failure shows the app's full answer with the config
/// JSON masked; its individual strings are masked as this home's registered secrets.
fn configure_app(home: &Path, id: &str, config: &Value) -> Result<()> {
    let value = serde_json::to_string(config)?;
    let app = App::resolve(home, id)?;
    let args = ["config", "set", value.as_str()];
    let output = app.run_in_org(&args, None, &[&value])?;
    if !output.status.success() {
        return Err(app.failed(&args, &output, &[&value]));
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn legacy_ids_fail_closed_without_rewriting_registrations() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        for (sid, org_id) in [
            ("silicon:test", "test"),
            ("c:alice", "test"),
            ("si:silicon", ""),
        ] {
            assert!(ensure_all(home, sid, org_id, "secret", &[]).is_err());
        }
        let path = home.join(".silicon/auth-apps.json");
        crate::state::write_json(&path, &["test>app"])?;
        let original = fs::read(&path)?;
        // A legacy registration is never authenticated. The automatic check reports it in
        // full under its name and the Silicon keeps working; explicit setup refuses it.
        ensure_all(home, "si:silicon", "test", "secret", &[])?;
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(
            log.contains("[error] [test>app/")
                && log.contains("automatic credential check failed")
                // It is only registered, so the line says how to drop it.
                && log.contains("(it is in the managed app registry but not in the YAML; `si auth remove 'test>app'` unregisters it): ")
                && log.contains("legacy org>app ID \"test>app\"")
                && log.contains("verified mapping"),
            "{log}"
        );
        let error = format!(
            "{:#}",
            setup(home, "si:silicon", "test", "secret", "test>app").unwrap_err()
        );
        assert!(
            error.contains("legacy org>app ID \"test>app\"") && error.contains("verified mapping"),
            "{error}"
        );
        let error = format!(
            "{:#}",
            ensure_all(home, "silicon:test", "test", "secret", &[]).unwrap_err()
        );
        assert!(error.contains("got \"silicon:test\""), "{error}");
        assert_eq!(fs::read(&path)?, original);
        let explicit = App::new(home, "! legacy-app --flag")?;
        assert_eq!(explicit.reference, "! legacy-app --flag");
        assert_eq!(App::new(home, &explicit.reference)?.argv, explicit.argv);
        // The legacy registration can still be removed as written, with the reason it
        // could not be signed out.
        let error = format!("{:#}", remove(home, "test>app").unwrap_err());
        assert!(
            error.starts_with("test>app was unregistered, but its credentials were not confirmed removed: legacy org>app ID \"test>app\""),
            "{error}"
        );
        assert!(registered(home)?.is_empty());
        Ok(())
    }

    #[test]
    fn selected_org_changes_force_new_grants_despite_fresh_checks_and_active_sessions() -> Result<()>
    {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        let bin = home.join(".silicon/bin");
        fs::create_dir_all(&bin)?;
        let iam = bin.join("iam");
        let app = bin.join("app");
        fs::write(
            &iam,
            r#"#!/bin/sh
set -eu
if [ "$*" = 'silicon-login --help' ]; then echo --approve-scopes; exit 0; fi
[ "$1 $2 $3 $4 $5 $6 $7 $8 $9" = '--output json --org home-org silicon-login --sid si:silicon --stk stk-secret' ]
[ "${10} ${11} ${12} ${13} ${14}" = "--app-id app --grant-org $SILICON_ORG --approve-scopes" ]
printf '%s\n' "$SILICON_ORG" >> minted
printf '{"slt":"issued-%s","expires_in":120}\n' "$SILICON_ORG"
"#,
        )?;
        fs::write(
            &app,
            r#"#!/bin/sh
set -eu
case "$*" in
  'iam --json') echo '{"app_id":"app"}' ;;
  'login status --json') echo '{"authenticated":true}' ;;
  'login issued-'*) [ "$2" = "issued-$SILICON_ORG" ]; printf '%s' "$SILICON_ORG" > active-org ;;
  *) exit 1 ;;
esac
"#,
        )?;
        for path in [&iam, &app] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }
        let key = serde_json::to_string(&("si:silicon", "home-org", "app"))?;
        crate::state::write_json(
            &home.join(".silicon/auth-checked.json"),
            &BTreeMap::from([(
                serde_json::to_string(&("silicon:home-org", "test>app"))?,
                chrono::Utc::now().timestamp(),
            )]),
        )?;
        crate::state::write_json(
            &home.join(".silicon/auth-grants.json"),
            &BTreeMap::from([(
                serde_json::to_string(&("silicon:home-org", "test>app"))?,
                "work-org",
            )]),
        )?;
        let mut expected = Vec::new();
        for org in [
            "work-org",
            "another_org",
            "-selected-",
            "home-org",
            "work-org",
        ] {
            crate::state::write_json(&home.join(".silicon/org.json"), &org)?;
            for _ in 0..2 {
                ensure_all(
                    home,
                    "si:silicon",
                    "home-org",
                    "stk-secret",
                    &["app".into()],
                )?;
            }
            expected.push(org);
            assert_eq!(
                fs::read_to_string(home.join("minted"))?
                    .lines()
                    .collect::<Vec<_>>(),
                expected
            );
            assert_eq!(fs::read_to_string(home.join("active-org"))?, org);
            assert_eq!(grants(home)?[&key], org);
        }
        Ok(())
    }

    #[test]
    fn automatic_checks_are_cached_per_app_and_silicon_for_48_hours() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        let app = home.join("app");
        fs::write(
            &app,
            r#"#!/bin/sh
echo "$*" >> calls
[ ! -f fail ] || { echo 'app: session store unreachable' >&2; exit 1; }
case "$*" in
  'iam --json') echo '{"app_id":"app"}' ;;
  'login status --json') echo '{"authenticated":true}' ;;
  *) exit 1 ;;
esac
"#,
        )?;
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700))?;
        let command = app.to_str().unwrap().to_owned();
        let configured = vec![command.clone()];
        let key = serde_json::to_string(&("si:silicon", "test", &command))?;
        crate::state::write_json(
            &home.join(".silicon/auth-grants.json"),
            &BTreeMap::from([(key.clone(), "test")]),
        )?;
        ensure_all(home, "si:silicon", "test", "", &configured)?;
        let calls = fs::read_to_string(home.join("calls"))?;
        let path = home.join(".silicon/auth-checked.json");
        let original = fs::read(&path)?;
        // An automatic check never registers the app; only explicit setup does.
        assert!(registered(home)?.is_empty());

        // A cached check must skip even discovery, including across connection generations.
        fs::write(home.join("fail"), "")?;
        ensure_all_scoped(
            home,
            "si:silicon",
            "test",
            "",
            &configured,
            uuid::Uuid::new_v4(),
        )?;
        assert_eq!(fs::read_to_string(home.join("calls"))?, calls);
        // Explicit setup fails closed; automatic checks report the failure and carry on.
        assert!(setup(home, "si:silicon", "test", "", &command).is_err());
        ensure_all(home, "si:other", "test", "", &configured)?;
        ensure_all(home, "si:silicon", "other-org", "", &configured)?;
        let other = home.join("other-app");
        fs::copy(&app, &other)?;
        ensure_all(
            home,
            "si:silicon",
            "test",
            "",
            &[other.to_str().unwrap().into()],
        )?;
        assert_eq!(fs::read(&path)?, original);
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert_eq!(log.matches("[error] [other-app/").count(), 1, "{log}");
        assert!(
            log.contains("automatic credential check failed; sessions start without it")
                && log.contains(
                    "`other-app iam --json` failed: exit status: 1\\nstderr:\\napp: session store unreachable"
                ),
            "{log}"
        );

        for timestamp in [
            chrono::Utc::now().timestamp() - 48 * 60 * 60,
            chrono::Utc::now().timestamp() + 3600,
        ] {
            let stale = BTreeMap::from([(key.clone(), timestamp)]);
            crate::state::write_json(&path, &stale)?;
            age_failures(home, RETRY_AFTER_SECONDS);
            let calls = fs::read_to_string(home.join("calls"))?;
            ensure_all(home, "si:silicon", "test", "", &configured)?;
            assert_ne!(fs::read_to_string(home.join("calls"))?, calls);
            let after: BTreeMap<String, i64> = serde_json::from_slice(&fs::read(&path)?)?;
            assert_eq!(after, stale); // Failed checks must not advance the timestamp.
        }
        // For ten minutes after its failure the app is left alone, even once it recovers.
        fs::remove_file(home.join("fail"))?;
        let calls = fs::read_to_string(home.join("calls"))?;
        ensure_all(home, "si:silicon", "test", "", &configured)?;
        assert_eq!(fs::read_to_string(home.join("calls"))?, calls);
        age_failures(home, RETRY_AFTER_SECONDS);
        ensure_all(home, "si:silicon", "test", "", &configured)?;
        let calls = fs::read_to_string(home.join("calls"))?;
        ensure_all(home, "si:silicon", "test", "", &configured)?;
        assert_eq!(fs::read_to_string(home.join("calls"))?, calls);
        Ok(())
    }

    /// Move this home's remembered failures `seconds` into the past.
    fn age_failures(home: &Path, seconds: i64) {
        let home = canonical(home);
        for ((failed, _, _), at) in FAILED.lock().recover().iter_mut() {
            if *failed == home {
                *at -= seconds;
            }
        }
    }

    #[test]
    fn application_tokens_are_iam_issued_isolated_and_never_logged() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        let iam = home.join("iam-stub");
        let app = home.join("app with spaces");
        fs::write(
            &iam,
            r#"#!/bin/sh
set -eu
if [ "$1 $2" = "silicon-login --help" ]; then echo --approve-scopes; exit 0; fi
[ "$PWD" = "$SILICON_HOME" ]
[ "$SILICON_IAM_HOME" = "$SILICON_HOME/.silicon-iam" ]
[ "$1 $2 $3 $4 $5 $6 $7 $8 $9" = '--output json --org test silicon-login --sid si:silicon --stk stk-secret' ]
[ "${12} ${13} ${14}" = '--grant-org test --approve-scopes' ]
shift 9
if [ -f space-app ]; then expected='spacestation'; else expected='app'; fi
[ "$1 $2" = "--app-id $expected" ]
echo minted >> "$SILICON_HOME/minted"
if [ -f bad-mint ]; then echo '{"slt":"iam-issued-secret"}'; exit 0; fi
echo '{"slt":"iam-issued-secret","expires_in":120}'
"#,
        )?;
        fs::write(
            &app,
            r#"#!/bin/sh
set -eu
[ "$PWD" = "$SILICON_HOME" ]
case "$1" in
  modern|legacy) mode="$1"; shift ;;
  *) exit 2 ;;
esac
case "$*" in
  'iam --json')
    if [ -f bad-discovery ]; then echo '{"app_id":null}'
    elif [ -f old-discovery ]; then echo '{"app_id":"test>app"}'
    elif [ -f space-app ]; then echo '{"app_id":"spacestation"}'
    else echo '{"app_id":"app"}'; fi ;;
  'auth status --json')
    [ "$mode" = modern ] || exit 2
    if [ -f bad-status ]; then echo '{"authenticated":"true"}'; exit 0; fi
    if [ -f forgetful ] && [ -f active ]; then echo '{"authenticated":false,"last":"iam-issued-secret"}'; exit 0; fi
    if [ -f active ]; then echo '{"authenticated":true}'; else echo '{"authenticated":false}'; fi ;;
  'login status --json')
    [ "$mode" = legacy ] || exit 2
    if [ -f active ]; then echo '{"authenticated":true}'; else echo '{"authenticated":false}'; fi ;;
  'auth token iam-issued-secret'|'login iam-issued-secret')
    if [ -f space-app ]; then [ "$SPACE_STATION_ORG" = test ]; fi
    if [ -f fail ]; then echo 'stk-secret iam-issued-secret' >&2; exit 1; fi
    touch active ;;
  'auth remove --help') [ "$mode" = modern ] ;;
  'logout --help') [ "$mode" = legacy ] ;;
  'auth remove'|'logout') rm active ;;
  'config set '*) printf '%s' "$3" > configured ;;
  *) exit 2 ;;
esac
"#,
        )?;
        for path in [&iam, &app] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }
        for mode in ["modern", "legacy"] {
            let command = format!("! {} {mode}", shell_words::quote(app.to_str().unwrap()));
            assert_eq!(
                setup_using(
                    home,
                    "si:silicon",
                    "test",
                    "stk-secret",
                    &command,
                    &iam,
                    false
                )?,
                "app"
            );
            let minted = fs::read_to_string(home.join("minted"))?;
            assert_eq!(registered(home)?, std::slice::from_ref(&command));
            setup_using(
                home,
                "si:silicon",
                "test",
                "stk-secret",
                &command,
                Path::new("missing-iam"),
                true,
            )?;
            assert_eq!(fs::read_to_string(home.join("minted"))?, minted);
            remove(home, &command)?;
            assert!(registered(home)?.is_empty());
            assert!(!home.join("active").exists());
            fs::write(home.join("fail"), "")?;
            let error = format!(
                "{:#}",
                setup_using(
                    home,
                    "si:silicon",
                    "test",
                    "stk-secret",
                    &command,
                    &iam,
                    false,
                )
                .unwrap_err()
            );
            assert!(!error.contains("stk-secret") && !error.contains("iam-issued-secret"));
            // The app's own failure is shown in full, with only the credentials masked.
            let login = if mode == "modern" {
                "auth token"
            } else {
                "login"
            };
            let expected = format!(
                "app rejected the IAM short-lived token; start authentication again after fixing the app's login failure: \
                 `'app with spaces' {mode} {login} '[redacted]'` failed: exit status: 1\n\
                 stderr:\n[redacted] [redacted]\nstdout: (empty)"
            );
            assert_eq!(error, expected);
            fs::remove_file(home.join("fail"))?;
        }
        // A canonical app ID resolves a verified native command without Honeycomb or shell parsing.
        fs::create_dir_all(home.join(".silicon/bin"))?;
        let canonical_app = home.join(".silicon/bin/app");
        fs::write(
            &canonical_app,
            "#!/bin/sh\nexec \"$SILICON_HOME/app with spaces\" legacy \"$@\"\n",
        )?;
        fs::set_permissions(&canonical_app, fs::Permissions::from_mode(0o700))?;
        assert_eq!(
            setup_using(home, "si:silicon", "test", "stk-secret", "app", &iam, false)?,
            "app"
        );
        assert_eq!(registered(home)?, ["app"]);
        let config = serde_json::json!({"silicon_org": "test", "nested": {"enabled": true}});
        configure(home, &BTreeMap::from([("app".into(), config.clone())]))?;
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(home.join("configured"))?)?,
            config
        );
        remove(home, "app")?;
        assert!(registered(home)?.is_empty());
        fs::write(home.join("space-app"), "")?;
        let space_command = format!("{} legacy", shell_words::quote(app.to_str().unwrap()));
        assert_eq!(
            setup_using(
                home,
                "si:silicon",
                "test",
                "stk-secret",
                &space_command,
                &iam,
                false
            )?,
            "spacestation"
        );
        remove(home, &space_command)?;
        fs::remove_file(home.join("space-app"))?;
        let error = format!("{:#}", App::new(home, "'unclosed").err().unwrap());
        assert_eq!(
            error,
            "invalid quoting in app command \"'unclosed\": missing closing quote"
        );
        // Shell-looking text is passed literally, never evaluated.
        let literal = App::new(home, "app '$(touch injected)' ';'")?;
        assert_eq!(literal.argv, ["app", "$(touch injected)", ";"]);
        assert!(!home.join("injected").exists());
        let command = format!("{} modern", shell_words::quote(app.to_str().unwrap()));
        // Each wrong answer names the command, what was wrong, and what the tool printed.
        for (invalid, expected) in [
            (
                "bad-discovery",
                "`'app with spaces' modern iam --json` returned app_id null, but must return a bare app_id; \
                 update or migrate the app using IAM's verified mapping\nexit status: 0\nstderr: (empty)\n\
                 stdout:\n{\"app_id\":null}",
            ),
            ("old-discovery", "returned app_id \"test>app\""),
            (
                "bad-status",
                "`'app with spaces' modern login status --json` failed: exit status: 2",
            ),
            (
                "bad-status",
                "`'app with spaces' modern auth status --json` returned no boolean authenticated field\n\
                 exit status: 0\nstderr: (empty)\nstdout:\n{\"authenticated\":\"true\"}",
            ),
            (
                "bad-mint",
                "returned an invalid short-lived token lifetime: expires_in must be 1 to 120 seconds, got nothing\n\
                 exit status: 0\nstderr: (empty)\nstdout:\n{\"slt\":\"[redacted]\"}",
            ),
        ] {
            fs::write(home.join(invalid), "")?;
            let error = format!(
                "{:#}",
                setup_using(
                    home,
                    "si:silicon",
                    "test",
                    "stk-secret",
                    &command,
                    &iam,
                    false
                )
                .unwrap_err()
            );
            assert!(error.contains(expected), "missing {expected:?} in {error}");
            assert!(!error.contains("stk-secret") && !error.contains("iam-issued-secret"));
            assert!(!home.join("active").exists());
            fs::remove_file(home.join(invalid))?;
        }
        // The status confirming the login may echo the SLT it was just handed; it is masked there too.
        fs::write(home.join("forgetful"), "")?;
        let error = format!(
            "{:#}",
            setup_using(
                home,
                "si:silicon",
                "test",
                "stk-secret",
                &command,
                &iam,
                false
            )
            .unwrap_err()
        );
        assert_eq!(
            error,
            "could not confirm the app signed in after it accepted the IAM short-lived token: \
             `'app with spaces' modern auth status --json` did not confirm authenticated true\n\
             exit status: 0\nstderr: (empty)\nstdout:\n{\"authenticated\":false,\"last\":\"[redacted]\"}"
        );
        fs::remove_file(home.join("forgetful"))?;
        fs::remove_file(home.join("active"))?;
        fs::remove_dir(home.join(".silicon-iam"))?;
        let outside = tempfile::tempdir()?;
        std::os::unix::fs::symlink(outside.path(), home.join(".silicon-iam"))?;
        let error = format!("{:#}", App::new(home, &command).err().unwrap());
        assert!(error.contains("must not be a symlink"), "{error}");
        Ok(())
    }

    fn assert_shows(error: &str, expected: &[&str]) {
        for expected in expected {
            assert!(
                error.contains(expected),
                "missing {expected:?} in:\n{error}"
            );
        }
    }

    #[test]
    fn failing_apps_and_iam_show_exit_status_and_both_streams() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        let bin = home.join(".silicon/bin");
        fs::create_dir_all(&bin)?;
        let app = bin.join("failing-app");
        let iam = home.join("iam");
        fs::write(
            &app,
            r#"#!/bin/sh
case "$*" in
  'iam --json')
    if [ -f broken ]; then echo 'dyld: Library not loaded: libapp.dylib' >&2; echo 'partial banner'; exit 3; fi
    echo '{"app_id":"failing-app"}' ;;
  'login status --json')
    if [ -f active ]; then echo '{"authenticated":true}'; else echo '{"authenticated":false}'; fi ;;
  'logout --help') ;;
  'logout') echo 'session locked by pid 42' >&2; echo '{"removed":false}'; exit 6 ;;
  'config set '*) echo "rejected $3" >&2; echo '{"ok":false,"field":"api_key"}'; exit 5 ;;
  *) exit 2 ;;
esac
"#,
        )?;
        fs::write(
            &iam,
            r#"#!/bin/sh
if [ "$1 $2" = 'silicon-login --help' ]; then echo 'iam: config.toml unreadable' >&2; exit 9; fi
echo "iam: stk $9 is not valid for org $4" >&2
echo '{"error":{"code":"denied","access_token":"leaked-token-value"}}'
exit 4
"#,
        )?;
        for path in [&app, &iam] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        }

        // IAM refuses to mint: its status and both streams reach the error; the STK never does.
        let error = format!(
            "{:#}",
            setup_using(
                home,
                "si:silicon",
                "test",
                "stk-secret",
                "failing-app",
                &iam,
                false
            )
            .unwrap_err()
        );
        assert_shows(
            &error,
            &[
                "IAM could not mint the application token for failing-app",
                "`iam --output json --org test silicon-login --sid si:silicon --stk '[redacted]' --app-id failing-app --grant-org test` failed: exit status: 4",
                "stderr:\niam: stk [redacted] is not valid for org test",
                "stdout:\n{\"error\":{\"code\":\"denied\",\"access_token\":\"[redacted]\"}}",
                // The failed capability probe explains the missing --approve-scopes.
                "\nalso: could not inspect silicon-login options, minting without --approve-scopes: \
                 `iam silicon-login --help` failed: exit status: 9\nstderr:\niam: config.toml unreadable\nstdout: (empty)",
            ],
        );
        assert!(!error.contains("stk-secret") && !error.contains("leaked-token-value"));
        // The failed capability probe is logged instead of silently dropped.
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(
            log.contains("`iam silicon-login --help` failed: exit status: 9\\nstderr:\\niam: config.toml unreadable"),
            "{log}"
        );

        // The app cannot say who it is.
        fs::write(home.join("broken"), "")?;
        let command = shell_words::quote(app.to_str().unwrap()).into_owned();
        let error = format!(
            "{:#}",
            setup_using(
                home,
                "si:silicon",
                "test",
                "stk-secret",
                &command,
                &iam,
                false
            )
            .unwrap_err()
        );
        assert_shows(
            &error,
            &[
                "IAM app must answer `iam --json`",
                "`failing-app iam --json` failed: exit status: 3\nstderr:\ndyld: Library not loaded: libapp.dylib\nstdout:\npartial banner",
            ],
        );
        // "Not installed" carries what the only candidate said.
        let error = format!("{:#}", remove(home, "failing-app").unwrap_err());
        assert_shows(
            &error,
            &[
                "IAM app failing-app is not installed or does not answer as failing-app",
                &format!("\n{} is not IAM app failing-app: ", app.canonicalize()?.display()),
                "`failing-app iam --json` failed: exit status: 3\nstderr:\ndyld: Library not loaded: libapp.dylib",
            ],
        );
        fs::remove_file(home.join("broken"))?;

        // Config values are masked; everything else the app said is shown. A quote in a
        // value must not let shell quoting of the command slip the value past the mask.
        let config =
            serde_json::json!({"api_key": "private-key-value-123", "note": "it's-private-456"});
        let error = format!(
            "{:#}",
            configure(home, &BTreeMap::from([("failing-app".into(), config)])).unwrap_err()
        );
        assert_shows(
            &error,
            &[
                "could not apply silicon.app_configs.failing-app to failing-app",
                "`failing-app config set '[redacted]'` failed: exit status: 5",
                "stderr:\nrejected [redacted]",
                "stdout:\n{\"ok\":false,\"field\":\"api_key\"}",
            ],
        );
        assert!(
            !error.contains("private-key-value-123") && !error.contains("private-456"),
            "{error}"
        );

        // Logout failure keeps the app's own explanation.
        fs::write(home.join("active"), "")?;
        let error = format!("{:#}", remove(home, "failing-app").unwrap_err());
        assert_shows(
            &error,
            &[
                "app logout failed; credentials were not confirmed removed",
                "`failing-app logout` failed: exit status: 6\nstderr:\nsession locked by pid 42\nstdout:\n{\"removed\":false}",
            ],
        );
        Ok(())
    }

    /// `name` in this home's command directory: answers as `name`, says `status` to a
    /// status check, accepts the `issued-slt` login and records every call.
    fn id_app(home: &Path, name: &str, status: &str) -> Result<()> {
        let bin = home.join(".silicon/bin");
        fs::create_dir_all(&bin)?;
        let path = bin.join(name);
        fs::write(
            &path,
            format!(
                r#"#!/bin/sh
echo "$*" >> "$SILICON_HOME/{name}-calls"
case "$*" in
  'iam --json') echo '{{"app_id":"{name}"}}' ;;
  'login status --json') {status} ;;
  'login issued-slt') ;;
  *) exit 1 ;;
esac
"#
            ),
        )?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
        Ok(())
    }

    /// An IAM that mints `issued-slt` for any request, unless the home has an `iam-down` file.
    fn fake_iam(home: &Path) -> Result<()> {
        let path = home.join(".silicon/bin/iam");
        fs::create_dir_all(path.parent().unwrap())?;
        fs::write(
            &path,
            "#!/bin/sh\nif [ \"$*\" = 'silicon-login --help' ]; then echo --approve-scopes; exit 0; fi\nif [ -f \"$SILICON_HOME/iam-down\" ]; then echo 'iam: connection refused' >&2; exit 7; fi\necho '{\"slt\":\"issued-slt\",\"expires_in\":60}'\n",
        )?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
        Ok(())
    }

    const HEALTHY: &str = "echo '{\"authenticated\":true}'";

    #[test]
    fn one_broken_app_is_reported_and_left_alone_while_the_others_are_checked() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        id_app(home, "alpha", HEALTHY)?;
        id_app(
            home,
            "broken",
            "echo 'broken: keychain locked' >&2; echo '{\"authenticated\":null}'; exit 3",
        )?;
        id_app(home, "gamma", HEALTHY)?;
        let apps = ["alpha", "broken", "gamma"].map(String::from).to_vec();
        let key = |app: &str| serde_json::to_string(&("si:silicon", "test", app)).unwrap();
        crate::state::write_json(
            &home.join(".silicon/auth-grants.json"),
            &apps
                .iter()
                .map(|app| (key(app), "test"))
                .collect::<BTreeMap<_, _>>(),
        )?;
        let checked = || -> Result<Vec<String>> {
            let path = home.join(".silicon/auth-checked.json");
            let checked: BTreeMap<String, i64> = serde_json::from_slice(&fs::read(path)?)?;
            Ok(checked.into_keys().collect())
        };
        let failures = || {
            fs::read_to_string(home.join(".silicon/silicon.log"))
                .unwrap()
                .lines()
                .filter(|line| line.starts_with("[error] [broken/"))
                .map(str::to_owned)
                .collect::<Vec<_>>()
        };
        let generation = uuid::Uuid::new_v4();
        ensure_all_scoped(home, "si:silicon", "test", "", &apps, generation)?;
        // The apps after the broken one were still checked; the broken one was not recorded.
        assert_eq!(checked()?, [key("alpha"), key("gamma")]);
        let logged = failures();
        assert_eq!(logged.len(), 1, "{logged:?}");
        assert!(
            logged[0].contains("automatic credential check failed; sessions start without it, and it is checked again automatically after 10 minutes, on the next connect, or by `si auth setup`: ")
                && logged[0].contains(
                    "`broken login status --json` failed: exit status: 3\\nstderr:\\nbroken: keychain locked\\nstdout:\\n{\"authenticated\":null}"
                ),
            "{logged:?}"
        );
        // A storm of new sessions leaves it alone for ten minutes and logs nothing new.
        let calls = fs::read_to_string(home.join("broken-calls"))?;
        for _ in 0..3 {
            ensure_all_scoped(home, "si:silicon", "test", "", &apps, generation)?;
        }
        assert_eq!(fs::read_to_string(home.join("broken-calls"))?, calls);
        assert_eq!(failures().len(), 1);
        // Explicit setup is not held back, and fails closed.
        let error = format!(
            "{:#}",
            ensure(home, "si:silicon", "test", "", "broken").unwrap_err()
        );
        assert!(error.contains("broken: keychain locked"), "{error}");
        // A reconnect (a new generation, perhaps with a repaired token) checks it at once.
        let generation = uuid::Uuid::new_v4();
        ensure_all_scoped(home, "si:silicon", "test", "", &apps, generation)?;
        assert_eq!(failures().len(), 2);
        ensure_all_scoped(home, "si:silicon", "test", "", &apps, generation)?;
        assert_eq!(failures().len(), 2);
        // Ten minutes on, the automatic check tries it again and reports it again.
        age_failures(home, RETRY_AFTER_SECONDS);
        ensure_all_scoped(home, "si:silicon", "test", "", &apps, generation)?;
        assert_eq!(failures().len(), 3);
        // Once repaired, a successful explicit setup lets the next automatic check run at once.
        id_app(home, "broken", HEALTHY)?;
        ensure(home, "si:silicon", "test", "", "broken")?;
        ensure_all_scoped(home, "si:silicon", "test", "", &apps, generation)?;
        assert_eq!(checked()?, [key("alpha"), key("broken"), key("gamma")]);
        Ok(())
    }

    #[test]
    fn silicons_do_not_wait_for_each_others_app_checks() -> Result<()> {
        let dirs = [tempfile::tempdir()?, tempfile::tempdir()?];
        let homes = dirs
            .iter()
            .map(|dir| dir.path().canonicalize())
            .collect::<std::io::Result<Vec<_>>>()?;
        for (index, home) in homes.iter().enumerate() {
            let other = homes[1 - index].join("started");
            // Discovery answers only once the other Silicon's check is running too, so
            // checks that waited for each other would time out here.
            let app = home.join("slow-app");
            fs::write(
                &app,
                format!(
                    r#"#!/bin/sh
case "$*" in
  'iam --json')
    touch "$SILICON_HOME/started"
    i=0
    while [ ! -f {other} ]; do i=$((i+1)); [ $i -lt 200 ] || exit 9; sleep 0.05; done
    echo '{{"app_id":"slow"}}' ;;
  'login status --json') echo '{{"authenticated":true}}' ;;
  *) exit 1 ;;
esac
"#,
                    other = shell_words::quote(other.to_str().unwrap())
                ),
            )?;
            fs::set_permissions(&app, fs::Permissions::from_mode(0o700))?;
            let command = app.to_str().unwrap().to_owned();
            crate::state::write_json(
                &home.join(".silicon/auth-grants.json"),
                &BTreeMap::from([(
                    serde_json::to_string(&("si:silicon", "test", &command))?,
                    "test",
                )]),
            )?;
        }
        let started = std::time::Instant::now();
        let checks = homes
            .iter()
            .map(|home| {
                let home = home.clone();
                std::thread::spawn(move || {
                    let command = home.join("slow-app").to_str().unwrap().to_owned();
                    ensure(&home, "si:silicon", "test", "", &command)
                })
            })
            .collect::<Vec<_>>();
        for check in checks {
            assert_eq!(check.join().unwrap()?, "slow");
        }
        assert!(started.elapsed() < std::time::Duration::from_secs(9));
        Ok(())
    }

    #[test]
    fn corrupt_auth_state_is_moved_aside_reported_once_and_replaced() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        id_app(home, "alpha", HEALTHY)?;
        fake_iam(home)?;
        let silicon = home.join(".silicon");
        let names = ["auth-checked.json", "auth-grants.json", "auth-apps.json"];
        for name in names {
            fs::write(silicon.join(name), "{\"truncated\": ")?;
        }
        let apps = ["alpha".to_owned()];
        ensure_all(home, "si:silicon", "test", "stk-secret", &apps)?;
        let log = fs::read_to_string(silicon.join("silicon.log"))?;
        for (name, what) in [
            ("auth-checked.json", "auth check timestamps"),
            ("auth-grants.json", "app grant organizations"),
            ("auth-apps.json", "managed app registry"),
        ] {
            let aside = fs::read_dir(&silicon)?
                .map(|entry| entry.unwrap().path())
                .filter(|path| {
                    path.file_name()
                        .unwrap()
                        .to_string_lossy()
                        .starts_with(&format!("{name}.corrupt-"))
                })
                .collect::<Vec<_>>();
            assert_eq!(aside.len(), 1, "{name}: {aside:?}");
            assert_eq!(fs::read_to_string(&aside[0])?, "{\"truncated\": ");
            let reported = log
                .lines()
                .filter(|line| {
                    line.starts_with("[error] [auth/")
                        && line.contains(&format!(
                            "[invalid {what} in {}: ",
                            silicon.join(name).display()
                        ))
                })
                .collect::<Vec<_>>();
            assert_eq!(reported.len(), 1, "{name}:\n{log}");
            assert!(
                reported[0].contains(" at line 1 column ")
                    && reported[0].ends_with(&format!(
                        "; moved it to {} and continued without it, so apps are checked again]",
                        aside[0].display()
                    )),
                "{}",
                reported[0]
            );
        }
        // The check ran with fresh state and recorded it.
        let checked: BTreeMap<String, i64> =
            serde_json::from_slice(&fs::read(silicon.join("auth-checked.json"))?)?;
        assert!(checked.contains_key(&serde_json::to_string(&("si:silicon", "test", "alpha"))?));
        assert!(grants(home)?.contains_key(&serde_json::to_string(&(
            "si:silicon",
            "test",
            "alpha"
        ))?));
        ensure_all(home, "si:silicon", "test", "stk-secret", &apps)?;
        assert_eq!(
            fs::read_to_string(silicon.join("silicon.log"))?
                .matches("[error] [auth/")
                .count(),
            3
        );
        // A file that cannot be read at all still fails, naming it.
        fs::create_dir(silicon.join("auth-apps.json"))?;
        let error = format!("{:#}", registered(home).unwrap_err());
        assert!(
            error.starts_with(&format!(
                "could not read managed app registry from {}: ",
                silicon.join("auth-apps.json").display()
            )),
            "{error}"
        );
        Ok(())
    }

    #[test]
    fn a_broken_or_missing_app_can_still_be_removed() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        let command = format!("! {} --profile work", home.join("removed-app").display());
        let reference = App::new(home, &command)?.reference;
        crate::state::write_json(
            &home.join(".silicon/auth-apps.json"),
            &["keep", "gone-test-app", reference.as_str(), "test>app"],
        )?;
        let key = |sid: &str, app: &str| serde_json::to_string(&(sid, "test", app)).unwrap();
        let records = |value: Value| -> BTreeMap<String, Value> {
            [
                key("si:one", "gone-test-app"),
                key("si:two", "gone-test-app"),
                key("si:one", "keep"),
                key("si:one", &reference),
                key("si:one", "stale-test-app"),
            ]
            .into_iter()
            .map(|key| (key, value.clone()))
            .collect()
        };
        crate::state::write_json(
            &home.join(".silicon/auth-grants.json"),
            &records(Value::from("test")),
        )?;
        crate::state::write_json(
            &home.join(".silicon/auth-checked.json"),
            &records(Value::from(chrono::Utc::now().timestamp())),
        )?;
        let kept = |path: &str| -> Result<Vec<String>> {
            let records: BTreeMap<String, Value> =
                serde_json::from_slice(&fs::read(home.join(".silicon").join(path))?)?;
            Ok(records.into_keys().collect())
        };

        let removed = |command: &str| format!("{:#}", remove(home, command).unwrap_err());

        // An app ID that no longer resolves anywhere.
        let error = removed("gone-test-app");
        assert!(
            error.starts_with("gone-test-app was unregistered and its grant and check records dropped, but its credentials were not confirmed removed: ")
                && error.contains("IAM app gone-test-app is not installed or does not answer as gone-test-app"),
            "{error}"
        );
        assert_eq!(registered(home)?, ["keep", reference.as_str(), "test>app"]);
        for path in ["auth-grants.json", "auth-checked.json"] {
            assert_eq!(
                kept(path)?,
                [
                    key("si:one", &reference),
                    key("si:one", "keep"),
                    key("si:one", "stale-test-app")
                ]
            );
        }

        // A configured command whose executable is gone.
        let error = removed(&command);
        assert!(
            error.starts_with(&format!("{reference} was unregistered and its grant and check records dropped, but its credentials were not confirmed removed: "))
                && error.contains("could not run `removed-app --profile work iam --json`: No such file or directory"),
            "{error}"
        );
        assert_eq!(registered(home)?, ["keep", "test>app"]);

        // A legacy entry is removed as written, though it is no longer a valid command.
        let error = removed("test>app");
        assert!(
            error.starts_with("test>app was unregistered, but its credentials were not confirmed removed: legacy org>app ID \"test>app\" requires migration"),
            "{error}"
        );
        assert_eq!(registered(home)?, ["keep"]);

        // Records left by an app that is no longer registered go too.
        let error = removed("stale-test-app");
        assert!(
            error.starts_with("stale-test-app was not registered; its grant and check records were dropped, but its credentials were not confirmed removed: IAM app stale-test-app is not installed"),
            "{error}"
        );
        for path in ["auth-grants.json", "auth-checked.json"] {
            assert_eq!(kept(path)?, [key("si:one", "keep")]);
        }

        // A name that matches nothing says so instead of claiming it removed something.
        let error = removed("typo-test-app");
        assert!(
            error.starts_with("typo-test-app is not registered and has no grant or check records, and it could not be signed out: IAM app typo-test-app is not installed"),
            "{error}"
        );
        assert_eq!(registered(home)?, ["keep"]);
        for path in ["auth-grants.json", "auth-checked.json"] {
            assert_eq!(kept(path)?, [key("si:one", "keep")]);
        }
        Ok(())
    }

    /// `name` in this home's command directory with a session of its own: `login SLT` signs
    /// it in (the `NAME-active` file), `logout` signs it out unless there is a `NAME-stuck`
    /// file, and every call is recorded.
    fn session_app(home: &Path, name: &str) -> Result<()> {
        let bin = home.join(".silicon/bin");
        fs::create_dir_all(&bin)?;
        let path = bin.join(name);
        fs::write(
            &path,
            format!(
                r#"#!/bin/sh
echo "$*" >> "$SILICON_HOME/{name}-calls"
active="$SILICON_HOME/{name}-active"
case "$*" in
  'iam --json') echo '{{"app_id":"{name}"}}' ;;
  'login status --json')
    if [ -f "$active" ]; then echo '{{"authenticated":true}}'; else echo '{{"authenticated":false}}'; fi ;;
  'login issued-slt') touch "$active" ;;
  'logout --help') ;;
  'logout')
    [ ! -f "$SILICON_HOME/{name}-stuck" ] || {{ echo '{name}: keychain locked' >&2; exit 6; }}
    rm "$active" ;;
  *) exit 1 ;;
esac
"#
            ),
        )?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
        Ok(())
    }

    /// The `[error]` lines silicon.log has under `origin`.
    fn errors(home: &Path, origin: &str) -> Vec<String> {
        fs::read_to_string(home.join(".silicon/silicon.log"))
            .unwrap_or_default()
            .lines()
            .filter(|line| line.starts_with(&format!("[error] [{origin}/")))
            .map(str::to_owned)
            .collect()
    }

    /// Records in one of this home's check or grant files.
    fn records(home: &Path, name: &str) -> Result<BTreeMap<String, Value>> {
        state(home, &home.join(".silicon").join(name), name)
    }

    #[test]
    fn a_session_under_a_foreign_grant_is_signed_out_when_no_new_grant_can_be_minted() -> Result<()>
    {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        session_app(home, "alpha")?;
        fake_iam(home)?;
        let apps = ["alpha".to_owned()];
        setup(home, "si:old", "test", "stk-secret", "alpha")?;
        ensure_all(home, "si:old", "test", "stk-secret", &apps)?;
        assert!(home.join("alpha-active").exists());
        assert_eq!(records(home, "auth-checked.json")?.len(), 1);
        // The home now connects as another identity while IAM cannot mint its grant: the
        // session si:old opened must not serve si:new.
        fs::write(home.join("iam-down"), "")?;
        let generation = uuid::Uuid::new_v4();
        ensure_all_scoped(home, "si:new", "test", "stk-secret", &apps, generation)?;
        assert!(!home.join("alpha-active").exists());
        let calls = fs::read_to_string(home.join("alpha-calls"))?;
        assert!(
            calls.ends_with("login status --json\nlogout --help\nlogout\nlogin status --json\n"),
            "{calls}"
        );
        // Its records go for every identity, so none trusts a session that is gone; it
        // stays registered and configured, to be signed in again once IAM can mint.
        assert!(records(home, "auth-grants.json")?.is_empty());
        assert!(records(home, "auth-checked.json")?.is_empty());
        assert_eq!(registered(home)?, ["alpha"]);
        // One line says what happened, with the whole failure and never the credential.
        let logged = errors(home, "alpha");
        assert_eq!(logged.len(), 1, "{logged:?}");
        assert_shows(
            &logged[0],
            &[
                "] [signed out because its session belonged to another identity or organization and a new grant could not be minted; sessions start without it, and it is checked again automatically after 10 minutes, on the next connect, or by `si auth setup`: ",
                "IAM could not mint the application token for alpha",
                "--sid si:new --stk '[redacted]' --app-id alpha --grant-org test --approve-scopes` failed: exit status: 7\\nstderr:\\niam: connection refused",
            ],
        );
        assert!(!logged[0].contains("stk-secret"), "{logged:?}");
        // Like any failed check it is left alone for ten minutes...
        ensure_all_scoped(home, "si:new", "test", "stk-secret", &apps, generation)?;
        assert_eq!(fs::read_to_string(home.join("alpha-calls"))?, calls);
        // ...then, signed out, a failure is an ordinary one: logged, and nothing to sign out.
        age_failures(home, RETRY_AFTER_SECONDS);
        ensure_all_scoped(home, "si:new", "test", "stk-secret", &apps, generation)?;
        let since = fs::read_to_string(home.join("alpha-calls"))?[calls.len()..].to_owned();
        assert!(
            since.ends_with("iam --json\nlogin status --json\n") && !since.contains("logout"),
            "{since}"
        );
        let logged = errors(home, "alpha");
        assert_eq!(logged.len(), 2, "{logged:?}");
        assert!(
            logged[1].contains("] [automatic credential check failed; sessions start without it")
                && logged[1].contains("iam: connection refused"),
            "{logged:?}"
        );
        // Once IAM mints again, the next check signs it in for the new identity.
        fs::remove_file(home.join("iam-down"))?;
        age_failures(home, RETRY_AFTER_SECONDS);
        ensure_all_scoped(home, "si:new", "test", "stk-secret", &apps, generation)?;
        assert!(home.join("alpha-active").exists());
        assert_eq!(
            grants(home)?,
            BTreeMap::from([(
                serde_json::to_string(&("si:new", "test", "alpha"))?,
                "test".to_owned()
            )])
        );
        Ok(())
    }

    #[test]
    fn a_foreign_session_is_signed_out_whichever_step_of_signing_in_again_fails() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        session_app(home, "alpha")?;
        let apps = ["alpha".to_owned()];
        for (sid, stk, failure) in [
            // The Silicon credential is the placeholder, so IAM is never asked.
            (
                "si:one",
                "...",
                "silicon.token is required to authenticate alpha with IAM (no IAM grant for organization test is recorded), but it is the ... placeholder",
            ),
            // IAM mints, but the app refuses the token it is handed.
            (
                "si:two",
                "stk-secret",
                "app rejected the IAM short-lived token; start authentication again after fixing the app's login failure: `alpha login '[redacted]'` failed: exit status: 1",
            ),
        ] {
            fake_iam(home)?;
            setup(home, "si:old", "test", "stk-secret", "alpha")?;
            assert!(home.join("alpha-active").exists());
            if stk == "stk-secret" {
                fs::write(
                    home.join(".silicon/bin/iam"),
                    "#!/bin/sh\nif [ \"$*\" = 'silicon-login --help' ]; then exit 0; fi\necho '{\"slt\":\"refused-slt\",\"expires_in\":60}'\n",
                )?;
            }
            ensure_all(home, sid, "test", stk, &apps)?;
            assert!(!home.join("alpha-active").exists(), "{sid}");
            assert!(records(home, "auth-grants.json")?.is_empty(), "{sid}");
            assert!(records(home, "auth-checked.json")?.is_empty(), "{sid}");
            let logged = errors(home, "alpha");
            let last = logged.last().map(String::as_str).unwrap_or_default();
            assert_shows(last, &[&format!("] [{SIGNED_OUT}; sessions start without it"), failure]);
            assert!(
                !last.contains("stk-secret") && !last.contains("refused-slt"),
                "{last}"
            );
        }
        assert_eq!(errors(home, "alpha").len(), 2);
        Ok(())
    }

    #[test]
    fn a_session_under_a_foreign_grant_that_cannot_be_signed_out_fails_the_check() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        session_app(home, "beta")?;
        session_app(home, "gamma")?;
        fake_iam(home)?;
        let apps = ["beta".to_owned(), "gamma".to_owned()];
        ensure_all(home, "si:silicon", "test", "stk-secret", &apps)?;
        let grants_before = records(home, "auth-grants.json")?;
        // The selected organization changes while IAM is down and beta cannot sign out.
        crate::state::write_json(&home.join(".silicon/org.json"), &"other-org")?;
        fs::write(home.join("iam-down"), "")?;
        fs::write(home.join("beta-stuck"), "")?;
        let generation = uuid::Uuid::new_v4();
        for _ in 0..2 {
            let calls = fs::read_to_string(home.join("beta-calls"))?;
            // Every attempt runs again: a check that fails closed is never held back.
            let error = format!(
                "{:#}",
                ensure_all_scoped(home, "si:silicon", "test", "stk-secret", &apps, generation)
                    .unwrap_err()
            );
            assert_ne!(fs::read_to_string(home.join("beta-calls"))?, calls);
            assert_shows(
                &error,
                &[
                    "beta is signed in with a session that belongs to another identity or organization, a new grant could not be minted, and it could not be signed out, so connect and new sessions fail until it is signed in afresh or signed out: IAM could not mint the application token for beta",
                    "--app-id beta --grant-org other-org --approve-scopes` failed: exit status: 7\nstderr:\niam: connection refused",
                    "\nalso: signing it out failed: app logout failed; credentials were not confirmed removed: `beta logout` failed: exit status: 6\nstderr:\nbeta: keychain locked\nstdout: (empty)",
                ],
            );
            assert!(!error.contains("stk-secret"), "{error}");
        }
        // The session and its records stay as they were; nothing was logged as carried on.
        assert!(home.join("beta-active").exists());
        assert_eq!(records(home, "auth-grants.json")?, grants_before);
        assert!(
            errors(home, "beta").is_empty(),
            "{:?}",
            errors(home, "beta")
        );
        // Once it can sign out, the check signs it out and the Silicon carries on, the
        // other app included.
        fs::remove_file(home.join("beta-stuck"))?;
        ensure_all_scoped(home, "si:silicon", "test", "stk-secret", &apps, generation)?;
        assert!(!home.join("beta-active").exists());
        assert!(!home.join("gamma-active").exists());
        for app in ["beta", "gamma"] {
            let logged = errors(home, app);
            assert_eq!(logged.len(), 1, "{logged:?}");
            assert!(
                logged[0].contains(&format!("] [{SIGNED_OUT}; ")),
                "{logged:?}"
            );
        }
        Ok(())
    }

    #[test]
    fn failures_that_leave_no_foreign_session_are_logged_and_the_silicon_carries_on() -> Result<()>
    {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        session_app(home, "delta")?;
        fake_iam(home)?;
        fs::write(home.join("iam-down"), "")?;
        fs::write(home.join("delta-stuck"), "")?;
        let apps = ["delta".to_owned()];
        // Not signed in: the failed check is logged as before and nothing is signed out.
        ensure_all(home, "si:silicon", "test", "stk-secret", &apps)?;
        let logged = errors(home, "delta");
        assert_eq!(logged.len(), 1, "{logged:?}");
        assert!(
            logged[0].contains("] [automatic credential check failed; sessions start without it, and it is checked again automatically after 10 minutes")
                && logged[0].contains("iam: connection refused"),
            "{logged:?}"
        );
        assert!(!fs::read_to_string(home.join("delta-calls"))?.contains("logout"));
        // A session under this identity's own current grant is reused without IAM.
        fs::remove_file(home.join("iam-down"))?;
        ensure(home, "si:silicon", "test", "stk-secret", "delta")?;
        fs::write(home.join("iam-down"), "")?;
        crate::state::write_json(
            &home.join(".silicon/auth-checked.json"),
            &BTreeMap::<String, i64>::new(),
        )?;
        age_failures(home, RETRY_AFTER_SECONDS);
        ensure_all(home, "si:silicon", "test", "stk-secret", &apps)?;
        assert!(home.join("delta-active").exists());
        assert_eq!(errors(home, "delta").len(), 1);
        // Explicit checks fail with the error and never sign an app out.
        let error = format!(
            "{:#}",
            ensure(home, "si:other", "test", "stk-secret", "delta").unwrap_err()
        );
        assert!(error.contains("iam: connection refused"), "{error}");
        assert!(home.join("delta-active").exists());
        assert!(!fs::read_to_string(home.join("delta-calls"))?.contains("logout"));
        Ok(())
    }

    #[test]
    fn a_clean_remove_unregisters_but_a_configured_app_waits_for_its_cached_check() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        session_app(home, "alpha")?;
        fake_iam(home)?;
        let apps = ["alpha".to_owned()];
        let generation = uuid::Uuid::new_v4();
        ensure_all_scoped(home, "si:silicon", "test", "stk-secret", &apps, generation)?;
        assert!(home.join("alpha-active").exists());
        setup(home, "si:silicon", "test", "stk-secret", "alpha")?;
        assert_eq!(registered(home)?, ["alpha"]);
        let records = |name: &str| fs::read(home.join(".silicon").join(name));
        let (checked, grants) = (records("auth-checked.json")?, records("auth-grants.json")?);

        remove(home, "alpha")?;
        assert!(registered(home)?.is_empty());
        assert!(!home.join("alpha-active").exists());
        // The check and grant records stay, so the next new session does not sign a
        // configured app straight back in; it is checked again once its cache expires.
        assert_eq!(records("auth-checked.json")?, checked);
        assert_eq!(records("auth-grants.json")?, grants);
        let calls = fs::read_to_string(home.join("alpha-calls"))?;
        ensure_all_scoped(home, "si:silicon", "test", "stk-secret", &apps, generation)?;
        assert_eq!(fs::read_to_string(home.join("alpha-calls"))?, calls);
        assert!(!home.join("alpha-active").exists());
        Ok(())
    }

    #[test]
    fn bookkeeping_failures_after_a_successful_login_are_reported_not_raised() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        id_app(home, "alpha", HEALTHY)?;
        fake_iam(home)?;
        let key = |app: &str| serde_json::to_string(&("si:silicon", "test", app)).unwrap();
        // silicon.log cannot be written, so there is no progress record or log line.
        fs::create_dir_all(home.join(".silicon/silicon.log"))?;
        assert_eq!(
            setup(home, "si:silicon", "test", "stk-secret", "alpha")?,
            "alpha"
        );
        assert_eq!(
            fs::read_to_string(home.join("alpha-calls"))?,
            "iam --json\niam --json\nlogin status --json\nlogin issued-slt\nlogin status --json\n"
        );
        assert_eq!(grants(home)?[&key("alpha")], "test");
        assert_eq!(registered(home)?, ["alpha"]);
        // Registering the app is what explicit setup is for, so a registry that cannot be
        // written fails it, though the app stays signed in with its grant recorded.
        id_app(home, "beta", HEALTHY)?;
        let registry = home.join(".silicon/auth-apps.json");
        fs::remove_file(&registry)?;
        fs::create_dir(&registry)?;
        let error = format!(
            "{:#}",
            setup(home, "si:silicon", "test", "stk-secret", "beta").unwrap_err()
        );
        assert!(
            error.starts_with(&format!(
                "beta is signed in, but could not be added to the managed app registry, so automatic checks skip it unless it is configured; run the setup again once the registry can be written: could not read managed app registry from {}: ",
                registry.canonicalize()?.display()
            )),
            "{error}"
        );
        assert!(fs::read_to_string(home.join("beta-calls"))?
            .ends_with("login issued-slt\nlogin status --json\n"));
        assert_eq!(grants(home)?[&key("beta")], "test");
        Ok(())
    }

    #[test]
    fn one_app_rejecting_its_configuration_does_not_stop_the_others() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path();
        let bin = home.join(".silicon/bin");
        fs::create_dir_all(&bin)?;
        for (name, answer) in [
            (
                "picky-app",
                "echo \"rejected $3\" >&2; echo '{\"ok\":false}'; exit 5",
            ),
            ("quiet-app", "printf '%s' \"$3\" > \"$SILICON_HOME/quiet\""),
        ] {
            let path = bin.join(name);
            fs::write(
                &path,
                format!("#!/bin/sh\ncase \"$1 $2\" in\n  'iam --json') echo '{{\"app_id\":\"{name}\"}}' ;;\n  'config set') {answer} ;;\n  *) exit 1 ;;\nesac\n"),
            )?;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
        }
        let configs = BTreeMap::from([
            (
                "absent-test-app".to_owned(),
                serde_json::json!({"on": true}),
            ),
            (
                "picky-app".to_owned(),
                serde_json::json!({"api_key": "private-key-value-123"}),
            ),
            ("quiet-app".to_owned(), serde_json::json!({"level": 3})),
        ]);
        // Explicit installs still stop at the first failure.
        assert!(configure(home, &configs).is_err());
        assert!(!home.join("quiet").exists());
        configure_all(home, &configs, uuid::Uuid::new_v4());
        assert_eq!(fs::read_to_string(home.join("quiet"))?, "{\"level\":3}");
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        for (id, reason) in [
            (
                "absent-test-app",
                "IAM app absent-test-app is not installed or does not answer as absent-test-app",
            ),
            (
                "picky-app",
                "`picky-app config set '[redacted]'` failed: exit status: 5\\nstderr:\\nrejected [redacted]\\nstdout:\\n{\"ok\":false}",
            ),
        ] {
            let line = log
                .lines()
                .find(|line| line.starts_with(&format!("[error] [{id}/")))
                .unwrap_or_else(|| panic!("no error line for {id}:\n{log}"));
            assert!(
                line.contains(&format!("could not apply silicon.app_configs.{id} to {id}; the Silicon connects without it, the app keeps the configuration it last accepted, and the next connect applies it again: "))
                    && line.contains(reason),
                "{line}"
            );
        }
        assert!(!log.contains("private-key-value-123"), "{log}");
        Ok(())
    }
}
