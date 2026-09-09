//! IAM owns token issuance; applications own their exchanged sessions.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Output, Stdio},
    sync::Mutex,
};

// ponytail: serialize auth exchanges; use per-home/app locks if authentication throughput matters.
static AUTH_LOCK: Mutex<()> = Mutex::new(());

fn registered(home: &Path) -> Result<Vec<String>> {
    let path = home.join(".silicon/auth-apps.json");
    if path.exists() {
        serde_json::from_slice(&fs::read(path)?).context("invalid managed app registry")
    } else {
        Ok(Vec::new())
    }
}

fn remember(home: &Path, app: &App, present: bool) -> Result<()> {
    let command = shell_words::join(&app.argv);
    let mut commands = registered(home)?;
    commands.retain(|item| item != &command);
    if present {
        commands.push(command);
    }
    crate::state::write_json(&home.join(".silicon/auth-apps.json"), &commands)
}

/// Configured and dynamically authenticated apps are checked at every session/heartbeat boundary.
pub fn ensure_all(home: &Path, sid: &str, stk: &str, configured: &[String]) -> Result<()> {
    let mut commands = registered(home)?;
    for command in configured {
        let command = shell_words::join(&App::new(home, command)?.argv);
        if !commands.contains(&command) {
            commands.push(command);
        }
    }
    for command in commands {
        ensure(home, sid, stk, &command)?;
    }
    Ok(())
}

struct App {
    home: PathBuf,
    argv: Vec<String>,
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
        Ok(Self { home, argv })
    }

    fn run(&self, args: &[&str]) -> Result<Output> {
        // Configured commands are argv, never shell programs; captured streams
        // may contain credentials and must not be forwarded to logs or errors.
        crate::command(&self.argv[0], &self.home)
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
        Ok(app_id.to_owned())
    }

    fn status(&self) -> Result<(Contract, bool)> {
        for contract in [Contract::Auth, Contract::Login] {
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
pub fn setup(home: &Path, sid: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, stk, app, Path::new("iam"), false)
}

/// Check an application's session before a heartbeat or new ISI session.
pub fn ensure(home: &Path, sid: &str, stk: &str, app: &str) -> Result<String> {
    setup_using(home, sid, stk, app, Path::new("iam"), true)
}

fn setup_using(
    home: &Path,
    sid: &str,
    stk: &str,
    command: &str,
    iam: &Path,
    only_if_needed: bool,
) -> Result<String> {
    let _guard = AUTH_LOCK.lock().unwrap();
    let app = App::new(home, command)?;
    let app_id = app.discover()?;
    let (contract, authenticated) = app.status()?;
    if only_if_needed && authenticated {
        remember(home, &app, true)?;
        return Ok(app_id);
    }
    let (_, org) = sid
        .split_once(':')
        .filter(|(local, org)| {
            !local.is_empty()
                && !org.is_empty()
                && !org.contains(':')
                && !sid.chars().any(char::is_whitespace)
        })
        .ok_or_else(|| anyhow!("silicon.id must be in local-id:organization form"))?;
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
    let output = crate::command(iam, &home)
        .args([
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
            org,
        ])
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
    if !app.run(&args)?.status.success() {
        // Never retry another login spelling with a possibly consumed SLT.
        bail!("app rejected the IAM short-lived token; start authentication again after fixing the app's login failure");
    }
    app.check_status(contract, true)?;
    remember(&home, &app, true)?;
    Ok(app_id)
}

/// Remove application credentials using its advertised logout command.
pub fn remove(home: &Path, command: &str) -> Result<()> {
    let _guard = AUTH_LOCK.lock().unwrap();
    let app = App::new(home, command)?;
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

/// Register the silicon listening URL with an IAM application.
pub fn webhook(home: &Path, command: &str, url: &str) -> Result<()> {
    if url.is_empty() || url.chars().any(char::is_control) {
        bail!("webhook URL is required");
    }
    let app = App::new(home, command)?;
    if !app.run(&["webhook", url])?.status.success() {
        bail!("app webhook registration failed");
    }
    Ok(())
}

/// Remove a listening URL when a Silicon disconnects.
pub fn unhook(home: &Path, command: &str) -> Result<()> {
    let app = App::new(home, command)?;
    if !app.run(&["unhook"])?.status.success() {
        bail!("app webhook removal failed");
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

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
[ "$PWD" = "$SILICON_HOME" ]
[ "$SILICON_IAM_HOME" = "$SILICON_HOME/.silicon-iam" ]
[ "$1 $2 $3 $4 $5 $6 $7 $8 $9" = '--output json --org test silicon-login --sid silicon:test --stk stk-secret' ]
[ "${12} ${13}" = '--grant-org test' ]
shift 9
[ "$1 $2" = '--app-id test>app' ]
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
    if [ -f bad-discovery ]; then echo '{"app_id":null}'; else echo '{"app_id":"test>app"}'; fi ;;
  'auth status --json')
    [ "$mode" = modern ] || exit 2
    if [ -f bad-status ]; then echo '{"authenticated":"true"}'; exit 0; fi
    if [ -f active ]; then echo '{"authenticated":true}'; else echo '{"authenticated":false}'; fi ;;
  'login status --json')
    [ "$mode" = legacy ] || exit 2
    if [ -f active ]; then echo '{"authenticated":true}'; else echo '{"authenticated":false}'; fi ;;
  'auth token iam-issued-secret'|'login iam-issued-secret')
    if [ -f fail ]; then echo 'stk-secret iam-issued-secret' >&2; exit 1; fi
    touch active ;;
  'auth remove --help') [ "$mode" = modern ] ;;
  'logout --help') [ "$mode" = legacy ] ;;
  'auth remove'|'logout') rm active ;;
  'webhook test.localhost') touch hooked ;;
  unhook) rm hooked ;;
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
                setup_using(home, "silicon:test", "stk-secret", &command, &iam, false)?,
                "test>app"
            );
            let minted = fs::read_to_string(home.join("minted"))?;
            assert_eq!(registered(home)?.len(), 1);
            setup_using(
                home,
                "silicon:test",
                "stk-secret",
                &command,
                Path::new("missing-iam"),
                true,
            )?;
            assert_eq!(fs::read_to_string(home.join("minted"))?, minted);
            webhook(home, &command, "test.localhost")?;
            assert!(home.join("hooked").exists());
            unhook(home, &command)?;
            remove(home, &command)?;
            assert!(registered(home)?.is_empty());
            assert!(!home.join("active").exists());
            fs::write(home.join("fail"), "")?;
            let error = setup_using(home, "silicon:test", "stk-secret", &command, &iam, false)
                .unwrap_err()
                .to_string();
            assert!(!error.contains("stk-secret") && !error.contains("iam-issued-secret"));
            fs::remove_file(home.join("fail"))?;
        }
        assert!(App::new(home, "'unclosed").is_err());
        // Shell-looking text is passed literally, never evaluated.
        let literal = App::new(home, "app '$(touch injected)' ';'")?;
        assert_eq!(literal.argv, ["app", "$(touch injected)", ";"]);
        assert!(!home.join("injected").exists());
        let command = format!("{} modern", shell_words::quote(app.to_str().unwrap()));
        for invalid in ["bad-discovery", "bad-status", "bad-mint"] {
            fs::write(home.join(invalid), "")?;
            assert!(
                setup_using(home, "silicon:test", "stk-secret", &command, &iam, false).is_err()
            );
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
