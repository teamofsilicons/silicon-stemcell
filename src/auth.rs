//! IAM owns token issuance; applications own their exchanged sessions.
use crate::failure;
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::{Output, Stdio},
    sync::Mutex,
};

// ponytail: serialize auth exchanges; use per-home/app locks if authentication throughput matters.
static AUTH_LOCK: Mutex<()> = Mutex::new(());

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

/// Missing state is empty; unreadable or invalid state names its file and cause.
fn state<T: serde::de::DeserializeOwned + Default>(path: &Path, what: &str) -> Result<T> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes)
            .with_context(|| format!("invalid {what} in {}", path.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(T::default()),
        Err(error) => {
            Err(error).with_context(|| format!("could not read {what} from {}", path.display()))
        }
    }
}

fn save(path: &Path, value: &impl serde::Serialize) -> Result<()> {
    crate::state::write_json(path, value)
        .with_context(|| format!("could not write {}", path.display()))
}

fn grants(home: &Path) -> Result<BTreeMap<String, String>> {
    state(
        &home.join(".silicon/auth-grants.json"),
        "app grant organizations",
    )
}

pub(crate) fn registered(home: &Path) -> Result<Vec<String>> {
    state(
        &home.join(".silicon/auth-apps.json"),
        "managed app registry",
    )
}

fn remember(home: &Path, app: &App, present: bool) -> Result<()> {
    let command = app.reference.clone();
    let mut commands = registered(home)?;
    commands.retain(|item| item != &command);
    if present {
        commands.push(command);
    }
    save(&home.join(".silicon/auth-apps.json"), &commands)
}

/// Check managed apps at connect/session creation, reusing successful checks for 48 hours.
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
    let _guard = AUTH_LOCK.lock().unwrap();
    validate_identity(sid, org_id)?;
    let grant_org = selected_org(home, org_id)?;
    let grants = grants(home)?;
    let path = home.join(".silicon/auth-checked.json");
    let mut checked: BTreeMap<String, i64> = state(&path, "auth check timestamps")?;
    let mut commands = registered(home)?;
    for command in configured {
        let command = if crate::apps::valid_id(command) {
            command.clone()
        } else {
            App::new(home, command)?.reference
        };
        if !commands.contains(&command) {
            commands.push(command);
        }
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
        setup_locked(
            home,
            sid,
            org_id,
            stk,
            &command,
            Path::new("iam"),
            true,
            generation,
        )?;
        checked.insert(key, chrono::Utc::now().timestamp());
        save(&path, &checked)?;
    }
    Ok(())
}

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
        // Configured commands are argv, never shell programs.
        let mut command = crate::command(&self.argv[0], &self.home);
        if let Some(org) = org {
            command.env("SPACE_STATION_ORG", org);
        }
        command
            .args(&self.argv[1..])
            .args(args)
            .stdin(Stdio::null())
            .output()
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
    let _guard = AUTH_LOCK.lock().unwrap();
    setup_locked(
        home,
        sid,
        org_id,
        stk,
        command,
        iam,
        only_if_needed,
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
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    validate_identity(sid, org_id)?;
    let app = if crate::apps::valid_id(command) {
        let path = match crate::apps::resolve(home, command)? {
            Ok(path) => {
                crate::log_line_scoped(
                    home,
                    generation,
                    "app",
                    command,
                    "installed command available",
                )?;
                path
            }
            // Anything passed over is already in silicon.log; Honeycomb may still provide it.
            Err(_) => {
                crate::progress::step(
                    home,
                    generation,
                    &format!("Installing {command} through Honeycomb"),
                    &format!("Installed {command}"),
                    || crate::apps::install_at(home, command),
                )?;
                crate::log_line_scoped(
                    home,
                    generation,
                    "app",
                    command,
                    "installed through Honeycomb",
                )?;
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
    authenticate(home, sid, org_id, stk, app, iam, only_if_needed, generation)
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity/organization context without an auth wrapper.
fn authenticate(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    app: App,
    iam: &Path,
    only_if_needed: bool,
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
    crate::progress::step(
        home,
        generation,
        &format!("Authenticating {label}"),
        &format!("Authenticated {label}"),
        || authenticate_inner(home, sid, org_id, stk, app, iam, only_if_needed, generation),
    )
}

#[allow(clippy::too_many_arguments)] // Keep explicit identity/organization context without an auth wrapper.
fn authenticate_inner(
    home: &Path,
    sid: &str,
    org_id: &str,
    stk: &str,
    app: App,
    iam: &Path,
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    let app_id = app.discover()?;
    let (contract, authenticated) = app.status()?;
    let grant_org = selected_org(home, org_id)?;
    let mut grants = grants(home)?;
    let key = serde_json::to_string(&(sid, org_id, &app.reference))?;
    // Only a grant recorded for this exact canonical identity and explicit owner
    // can reuse a session. Legacy identity/cache entries require a fresh IAM login.
    if only_if_needed && authenticated && grants.get(&key) == Some(&grant_org) {
        remember(home, &app, true)?;
        crate::log_line_scoped(home, generation, "auth", &app_id, "already authenticated")?;
        return Ok(app_id);
    }
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
    let inspect = || {
        crate::command(iam, &home)
            .args(HELP)
            .stdin(Stdio::null())
            .output()
    };
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
        crate::log_line_scoped(&home, generation, "error", "iam", probe)?;
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
    let output = issuer
        .args(&mint)
        .current_dir(&home)
        .env("SILICON_HOME", &home)
        .env("SILICON_IAM_HOME", &iam_home)
        .stdin(Stdio::null())
        .output()
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
    grants.insert(key, grant_org);
    save(&home.join(".silicon/auth-grants.json"), &grants)?;
    remember(&home, &app, true)?;
    crate::log_line_scoped(&home, generation, "auth", &app_id, "authenticated")?;
    Ok(app_id)
}

/// Remove application credentials using its advertised logout command.
pub fn remove(home: &Path, command: &str) -> Result<()> {
    let _guard = AUTH_LOCK.lock().unwrap();
    let app = App::resolve(home, command)?;
    app.discover()?;
    let (contract, authenticated) = app.status()?;
    if !authenticated {
        return remember(home, &app, false);
    }
    let candidates: &[&[&str]] = match contract {
        Contract::Auth => &[&["auth", "remove"], &["auth", "logout"], &["logout"]],
        Contract::Login => &[&["logout"], &["auth", "remove"], &["auth", "logout"]],
    };
    let mut unsupported = Vec::new();
    for args in candidates {
        let mut help = args.to_vec();
        help.push("--help");
        let output = app.run(&help)?;
        if !output.status.success() {
            unsupported.push(format!("{:#}", app.failed(&help, &output, &[])));
            continue;
        }
        let output = app.run(args)?;
        if !output.status.success() {
            return Err(app
                .failed(args, &output, &[])
                .context("app logout failed; credentials were not confirmed removed"));
        }
        app.check_status(contract, false, &[])
            .context("could not confirm the app signed out after logout")?;
        return remember(home, &app, false);
    }
    bail!(
        "IAM app must expose `auth remove`, `auth logout`, or `logout`; every help check failed:\n{}",
        unsupported.join("\n")
    );
}

pub(crate) fn run_app(home: &Path, app: &str, args: &[&str]) -> Result<Output> {
    App::resolve(home, app)?.run(args)
}

pub(crate) fn configure(home: &Path, configs: &BTreeMap<String, Value>) -> Result<()> {
    for (id, config) in configs {
        configure_app(home, id, config)
            .with_context(|| format!("could not apply silicon.app_configs.{id} to {id}"))?;
    }
    Ok(())
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
        let error = format!(
            "{:#}",
            ensure_all(home, "si:silicon", "test", "secret", &[]).unwrap_err()
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
[ ! -f fail ] || exit 1
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
        assert!(setup(home, "si:silicon", "test", "", &command).is_err());
        assert!(ensure_all(home, "si:other", "test", "", &configured).is_err());
        assert!(ensure_all(home, "si:silicon", "other-org", "", &configured).is_err());
        let other = home.join("other-app");
        fs::copy(&app, &other)?;
        assert!(ensure_all(
            home,
            "si:silicon",
            "test",
            "",
            &[other.to_str().unwrap().into()]
        )
        .is_err());
        assert_eq!(fs::read(&path)?, original);

        for timestamp in [
            chrono::Utc::now().timestamp() - 48 * 60 * 60,
            chrono::Utc::now().timestamp() + 3600,
        ] {
            let stale = BTreeMap::from([(key.clone(), timestamp)]);
            crate::state::write_json(&path, &stale)?;
            assert!(ensure_all(home, "si:silicon", "test", "", &configured).is_err());
            let after: BTreeMap<String, i64> = serde_json::from_slice(&fs::read(&path)?)?;
            assert_eq!(after, stale); // Failed checks must not advance the timestamp.
        }
        fs::remove_file(home.join("fail"))?;
        ensure_all(home, "si:silicon", "test", "", &configured)?;
        let calls = fs::read_to_string(home.join("calls"))?;
        ensure_all(home, "si:silicon", "test", "", &configured)?;
        assert_eq!(fs::read_to_string(home.join("calls"))?, calls);
        Ok(())
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
}
