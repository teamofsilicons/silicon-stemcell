//! IAM owns token issuance; applications own their exchanged sessions.
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

#[derive(Clone, Copy)]
struct SiliconIdentity<'a> {
    sid: &'a str,
    org: &'a str,
    stk: &'a str,
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
                .map_err(|_| anyhow!("SILICON_ORG must be UTF-8"))
        })
        .transpose()?
        .unwrap_or_else(|| default.to_owned());
    if !crate::config::valid_org(&org) {
        bail!("SILICON_ORG must be a canonical IAM organization handle");
    }
    Ok(org)
}

fn grants(home: &Path) -> Result<BTreeMap<String, String>> {
    match fs::read(home.join(".silicon/auth-grants.json")) {
        Ok(bytes) => serde_json::from_slice(&bytes).context("invalid app grant organizations"),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(BTreeMap::new()),
        Err(error) => Err(error.into()),
    }
}

pub(crate) fn registered(home: &Path) -> Result<Vec<String>> {
    let path = home.join(".silicon/auth-apps.json");
    if path.exists() {
        serde_json::from_slice(&fs::read(path)?).context("invalid managed app registry")
    } else {
        Ok(Vec::new())
    }
}

fn remember(home: &Path, app: &App, present: bool) -> Result<()> {
    let command = app.reference.clone();
    let mut commands = registered(home)?;
    commands.retain(|item| item != &command);
    if present {
        commands.push(command);
    }
    crate::state::write_json(&home.join(".silicon/auth-apps.json"), &commands)
}

/// Check managed apps at connect/session creation, reusing successful checks for 48 hours.
pub fn ensure_all(
    home: &Path,
    sid: &str,
    org: &str,
    stk: &str,
    configured: &[String],
) -> Result<()> {
    ensure_all_using(home, sid, org, stk, configured, None)
}

pub fn ensure_all_scoped(
    home: &Path,
    sid: &str,
    org: &str,
    stk: &str,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    ensure_all_using(home, sid, org, stk, configured, Some(generation))
}

fn ensure_all_using(
    home: &Path,
    sid: &str,
    org: &str,
    stk: &str,
    configured: &[String],
    generation: Option<uuid::Uuid>,
) -> Result<()> {
    let _guard = AUTH_LOCK.lock().unwrap();
    let identity_org = org;
    let grant_org = selected_org(home, identity_org)?;
    let grants = grants(home)?;
    let path = home.join(".silicon/auth-checked.json");
    let mut checked: BTreeMap<String, i64> = match fs::read(&path) {
        Ok(bytes) => serde_json::from_slice(&bytes).context("invalid auth check timestamps")?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
        Err(error) => return Err(error.into()),
    };
    let mut commands = registered(home)?;
    for command in configured {
        let command = if crate::apps::valid_id(command) {
            command.clone()
        } else {
            shell_words::join(&App::new(home, command)?.argv)
        };
        if !commands.contains(&command) {
            commands.push(command);
        }
    }
    for command in commands {
        let key = serde_json::to_string(&(sid, org, &command))?;
        let now = chrono::Utc::now().timestamp();
        if grants.get(&key).map(String::as_str).unwrap_or(identity_org) == grant_org
            && checked
                .get(&key)
                .and_then(|last| now.checked_sub(*last))
                .is_some_and(|age| (0..48 * 60 * 60).contains(&age))
        {
            continue;
        }
        setup_locked(
            home,
            SiliconIdentity { sid, org, stk },
            &command,
            Path::new("iam"),
            true,
            generation,
        )?;
        checked.insert(key, chrono::Utc::now().timestamp());
        crate::state::write_json(&path, &checked)?;
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
        let command = command
            .trim()
            .strip_prefix('!')
            .unwrap_or(command.trim())
            .trim();
        let argv =
            shell_words::split(command).map_err(|_| anyhow!("invalid app command quoting"))?;
        if argv.first().is_none_or(|arg| arg.is_empty())
            || argv.iter().any(|arg| arg.contains('\0'))
        {
            bail!("app command must contain an executable and valid arguments");
        }
        let home = home.canonicalize().context("SILICON_HOME must exist")?;
        if fs::symlink_metadata(home.join(".silicon-iam")).is_ok_and(|m| m.file_type().is_symlink())
        {
            bail!("the Silicon IAM credential directory must not be a symlink");
        }
        let reference = shell_words::join(&argv);
        Ok(Self {
            home,
            argv,
            reference,
            expected_id: None,
        })
    }

    fn resolve(home: &Path, command: &str) -> Result<Self> {
        if !crate::apps::valid_id(command) {
            return Self::new(home, command);
        }
        let path = crate::apps::resolve(home, command)?.ok_or_else(|| {
            anyhow!("IAM app {command} is not installed in this Silicon home or on PATH")
        })?;
        let mut app = Self::new(home, &shell_words::quote(&path.to_string_lossy()))?;
        app.reference = command.to_owned();
        app.expected_id = Some(command.to_owned());
        Ok(app)
    }

    fn run(&self, args: &[&str]) -> Result<Output> {
        self.run_in_org(args, None)
    }

    fn run_in_org(&self, args: &[&str], org: Option<&str>) -> Result<Output> {
        // Configured commands are argv, never shell programs; captured streams
        // may contain credentials and must not be forwarded to logs or errors.
        let mut command = crate::command(&self.argv[0], &self.home);
        if let Some(org) = org {
            command.env("SPACE_STATION_ORG", org);
        }
        command
            .args(&self.argv[1..])
            .args(args)
            .stdin(Stdio::null())
            .output()
            .map_err(|_| {
                anyhow!(
                    "could not execute configured IAM app; check its executable and SILICON_HOME"
                )
            })
    }

    fn discover(&self) -> Result<String> {
        let output = self.run(&["iam", "--json"])?;
        if !output.status.success() {
            bail!("IAM app does not support `iam --json`; update the app to the Silicon IAM CLI contract");
        }
        let info: Value = serde_json::from_slice(&output.stdout)
            .map_err(|_| anyhow!("app `iam --json` returned invalid JSON"))?;
        let app_id = info
            .get("app_id")
            .and_then(Value::as_str)
            .filter(|id| {
                !id.is_empty()
                    && !id.chars().any(char::is_whitespace)
                    && !id.chars().any(char::is_control)
            })
            .ok_or_else(|| anyhow!("app `iam --json` must return a nonempty app_id string"))?;
        if self
            .expected_id
            .as_deref()
            .is_some_and(|expected| expected != app_id)
        {
            bail!("installed app identity does not match requested IAM app");
        }
        Ok(app_id.to_owned())
    }

    fn status(&self) -> Result<(Contract, bool)> {
        for contract in [Contract::Login, Contract::Auth] {
            let output = self.run(contract.status())?;
            let state = serde_json::from_slice::<Value>(&output.stdout).ok();
            if let Some(authenticated) = state
                .as_ref()
                .and_then(|s| s.get("authenticated"))
                .and_then(Value::as_bool)
            {
                // Some apps use a nonzero status to report an expired session.
                if !authenticated || output.status.success() {
                    return Ok((contract, authenticated));
                }
            }
        }
        bail!("IAM app must support `auth status --json` or `login status --json` with a boolean authenticated field");
    }

    fn check_status(&self, contract: Contract, expected: bool) -> Result<()> {
        let output = self.run(contract.status())?;
        let state: Value = serde_json::from_slice(&output.stdout)
            .map_err(|_| anyhow!("app auth status returned invalid JSON"))?;
        if state.get("authenticated").and_then(Value::as_bool) != Some(expected)
            || (expected && !output.status.success())
        {
            bail!("app did not confirm the requested authentication state");
        }
        Ok(())
    }
}

/// Log an application in using this Silicon's credential, never a Carbon session.
/// Returns only the public application ID, not the issued token.
pub fn setup(home: &Path, sid: &str, org: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, org, stk, app, Path::new("iam"), false)
}

pub fn setup_scoped(
    home: &Path,
    sid: &str,
    org: &str,
    stk: &str,
    app: &str,
    generation: uuid::Uuid,
) -> Result<String> {
    setup_using_scoped(
        home,
        SiliconIdentity { sid, org, stk },
        app,
        Path::new("iam"),
        false,
        Some(generation),
    )
}

/// Check an application's session immediately, bypassing automatic check timestamps.
pub fn ensure(home: &Path, sid: &str, org: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, org, stk, app, Path::new("iam"), true)
}

fn setup_using(
    home: &Path,
    sid: &str,
    org: &str,
    stk: &str,
    command: &str,
    iam: &Path,
    only_if_needed: bool,
) -> Result<String> {
    setup_using_scoped(
        home,
        SiliconIdentity { sid, org, stk },
        command,
        iam,
        only_if_needed,
        None,
    )
}

fn setup_using_scoped(
    home: &Path,
    identity: SiliconIdentity<'_>,
    command: &str,
    iam: &Path,
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    let _guard = AUTH_LOCK.lock().unwrap();
    setup_locked(home, identity, command, iam, only_if_needed, generation)
}

fn setup_locked(
    home: &Path,
    identity: SiliconIdentity<'_>,
    command: &str,
    iam: &Path,
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    let app = if crate::apps::valid_id(command) {
        let path = match crate::apps::resolve(home, command)? {
            Some(path) => {
                crate::log_line_scoped(
                    home,
                    generation,
                    "app",
                    command,
                    "installed command available",
                )?;
                path
            }
            None => {
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
                crate::apps::resolve(home, command)?
                    .context("installed package does not advertise the requested IAM app")?
            }
        };
        let mut app = App::new(home, &shell_words::quote(&path.to_string_lossy()))?;
        app.reference = command.to_owned();
        app.expected_id = Some(command.to_owned());
        app
    } else {
        App::new(home, command)?
    };
    authenticate(home, identity, app, iam, only_if_needed, generation)
}

fn authenticate(
    home: &Path,
    identity: SiliconIdentity<'_>,
    app: App,
    iam: &Path,
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    // Display an app handle or executable name, never its arguments or credentials.
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
        || authenticate_inner(home, identity, app, iam, only_if_needed, generation),
    )
}

fn authenticate_inner(
    home: &Path,
    identity: SiliconIdentity<'_>,
    app: App,
    iam: &Path,
    only_if_needed: bool,
    generation: Option<uuid::Uuid>,
) -> Result<String> {
    let SiliconIdentity { sid, org, stk } = identity;
    let app_id = app.discover()?;
    let (contract, authenticated) = app.status()?;
    let grant_org = selected_org(home, org)?;
    let mut grants = grants(home)?;
    let key = serde_json::to_string(&(sid, org, &app.reference))?;
    // Legacy sessions were granted the identity's org; a different selected org
    // requires fresh consent even if the app still reports authenticated:true.
    let granted = grants.get(&key).map(String::as_str).unwrap_or(org);
    if only_if_needed && authenticated && granted == grant_org {
        remember(home, &app, true)?;
        crate::log_line_scoped(home, generation, "auth", &app_id, "already authenticated")?;
        return Ok(app_id);
    }
    if !sid.strip_prefix("si:").is_some_and(|handle| {
        (3..=50).contains(&handle.len())
            && handle.bytes().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
            })
    }) {
        bail!("silicon.id must be si:<handle>");
    }
    if !crate::config::valid_org(org) {
        bail!("silicon.org_id must be a canonical IAM organization handle");
    }
    if stk.trim().is_empty() || stk == "..." || stk.contains('\0') {
        bail!("silicon.token is required for IAM authentication");
    }
    // IAM uses SILICON_IAM_HOME, not SILICON_HOME. Reject a redirected
    // credential directory instead of ever touching the user's personal IAM state.
    let home = home.canonicalize().context("SILICON_HOME must exist")?;
    let iam_home = home.join(".silicon-iam");
    fs::create_dir_all(&iam_home).context("could not create Silicon IAM credential directory")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&iam_home, fs::Permissions::from_mode(0o700))?;
    }
    // This is the installed IAM CLI's noninteractive contract. Only IAM gets
    // the STK; only the single-use SLT crosses the application boundary.
    let mut issuer = crate::command(iam, &home);
    // Discover CLI capabilities without constraining the installed version.
    let inspect = || {
        crate::command(iam, &home)
            .args(["silicon-login", "--help"])
            .stdin(Stdio::null())
            .output()
    };
    let help = match inspect() {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && iam == Path::new("iam") => {
            crate::apps::install_at(&home, "iam")?;
            inspect()
        }
        result => result,
    }
    .context("could not inspect IAM silicon-login contract")?;
    issuer.args([
        "--output",
        "json",
        "--org",
        org,
        "silicon-login",
        "--sid",
        sid,
        "--stk",
        stk,
        "--app-id",
        &app_id,
        "--grant-org",
        &grant_org,
    ]);
    if help.status.success() && String::from_utf8_lossy(&help.stdout).contains("--approve-scopes") {
        issuer.arg("--approve-scopes");
    }
    let output = issuer
        .current_dir(&home)
        .env("SILICON_HOME", &home)
        .env("SILICON_IAM_HOME", &iam_home)
        .stdin(Stdio::null())
        .output()
        .map_err(|_| anyhow!("could not execute IAM; install the iam CLI"))?;
    if !output.status.success() {
        bail!("IAM could not mint the application token; check the Silicon credential, application ID, and this Silicon's IAM environment configuration");
    }
    let issued: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| anyhow!("IAM silicon-login returned invalid JSON"))?;
    let slt = issued
        .get("slt")
        .and_then(Value::as_str)
        .filter(|s| {
            !s.is_empty() && !s.chars().any(char::is_whitespace) && !s.chars().any(char::is_control)
        })
        .ok_or_else(|| anyhow!("IAM silicon-login did not return a valid slt"))?;
    if issued
        .get("expires_in")
        .and_then(Value::as_u64)
        .is_none_or(|seconds| seconds == 0 || seconds > 120)
    {
        bail!("IAM returned an invalid short-lived token lifetime");
    }
    let args = match contract {
        Contract::Auth => vec!["auth", "token", slt],
        Contract::Login => vec!["login", slt],
    };
    // Space Station binds terminal sessions to an organization, including a first login.
    let app_org = (app_id == "spacestation").then_some(grant_org.as_str());
    if !app.run_in_org(&args, app_org)?.status.success() {
        // Never retry another login spelling with a possibly consumed SLT.
        bail!("app rejected the IAM short-lived token; start authentication again after fixing the app's login failure");
    }
    app.check_status(contract, true)?;
    grants.insert(key, grant_org);
    crate::state::write_json(&home.join(".silicon/auth-grants.json"), &grants)?;
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
        remember(home, &app, false)?;
        return Ok(());
    }
    let candidates: &[&[&str]] = match contract {
        Contract::Auth => &[&["auth", "remove"], &["auth", "logout"], &["logout"]],
        Contract::Login => &[&["logout"], &["auth", "remove"], &["auth", "logout"]],
    };
    for args in candidates {
        let mut help = args.to_vec();
        help.push("--help");
        if app.run(&help)?.status.success() {
            if !app.run(args)?.status.success() {
                bail!("app logout failed; credentials were not confirmed removed");
            }
            app.check_status(contract, false)?;
            return remember(home, &app, false);
        }
    }
    bail!("IAM app must expose `auth remove`, `auth logout`, or `logout`");
}

pub(crate) fn run_app(home: &Path, app: &str, args: &[&str]) -> Result<Output> {
    App::resolve(home, app)?.run(args)
}

/// Configuration values may contain secrets; never log arguments or captured output.
pub(crate) fn configure(home: &Path, configs: &BTreeMap<String, Value>) -> Result<()> {
    for (app, config) in configs {
        let value = serde_json::to_string(config)?;
        if !run_app(home, app, &["config", "set", &value])?
            .status
            .success()
        {
            bail!("{app} config set failed; inspect the app configuration locally");
        }
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

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
            &BTreeMap::from([(key.clone(), chrono::Utc::now().timestamp())]),
        )?;
        let mut expected = Vec::new();
        for org in ["work-org", "another_org", "home-org", "work-org"] {
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

        let key = serde_json::to_string(&("si:silicon", "test", &command))?;
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
    elif [ -f space-app ]; then echo '{"app_id":"spacestation"}'
    else echo '{"app_id":"app"}'; fi ;;
  'auth status --json')
    [ "$mode" = modern ] || exit 2
    if [ -f bad-status ]; then echo '{"authenticated":"true"}'; exit 0; fi
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
            assert_eq!(registered(home)?.len(), 1);
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
            let error = setup_using(
                home,
                "si:silicon",
                "test",
                "stk-secret",
                &command,
                &iam,
                false,
            )
            .unwrap_err()
            .to_string();
            assert!(!error.contains("stk-secret") && !error.contains("iam-issued-secret"));
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
        assert!(App::new(home, "'unclosed").is_err());
        // Shell-looking text is passed literally, never evaluated.
        let literal = App::new(home, "app '$(touch injected)' ';'")?;
        assert_eq!(literal.argv, ["app", "$(touch injected)", ";"]);
        assert!(!home.join("injected").exists());
        let command = format!("{} modern", shell_words::quote(app.to_str().unwrap()));
        for invalid in ["bad-discovery", "bad-status", "bad-mint"] {
            fs::write(home.join(invalid), "")?;
            assert!(setup_using(
                home,
                "si:silicon",
                "test",
                "stk-secret",
                &command,
                &iam,
                false
            )
            .is_err());
            assert!(!home.join("active").exists());
            fs::remove_file(home.join(invalid))?;
        }
        fs::remove_dir(home.join(".silicon-iam"))?;
        let outside = tempfile::tempdir()?;
        std::os::unix::fs::symlink(outside.path(), home.join(".silicon-iam"))?;
        assert!(App::new(home, &command).is_err());
        Ok(())
    }
}
