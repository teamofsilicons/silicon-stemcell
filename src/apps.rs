//! Honeycomb owns package downloads, platform selection, verification and removal.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Stdio,
};

/// Honeycomb's org>app handle grammar; these values are arguments, never shell text.
pub fn valid_id(id: &str) -> bool {
    id.split_once('>').is_some_and(|(org, app)| {
        [org, app].iter().all(|part| {
            !part.is_empty()
                && part.len() <= 64
                && part.as_bytes()[0].is_ascii_alphanumeric()
                && part
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
    })
}

fn validate(id: &str) -> Result<()> {
    if !valid_id(id) {
        bail!("expected a Honeycomb org>app ID with lowercase letters, digits and hyphens; quote it in your shell");
    }
    Ok(())
}

fn executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    fs::metadata(path).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

fn on_path(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path)
            .map(|p| p.join(name))
            .find(|p| executable(p))
    })
}

pub(crate) fn honeycomb(home: &Path) -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("SILICON_HONEYCOMB") {
        return Ok(PathBuf::from(path));
    }
    on_path("honeycomb")
        .or_else(|| {
            let path = crate::update::managed_prefix()
                .ok()?
                .join(".honeycomb/dir/system/bin/honeycomb");
            executable(&path).then_some(path)
        })
        .or_else(|| {
            let path = home.join(".honeycomb/dir/system/bin/honeycomb");
            executable(&path).then_some(path)
        })
        .or_else(|| {
            let path = PathBuf::from(std::env::var_os("HOME")?)
                .join(".honeycomb/dir/system/bin/honeycomb");
            executable(&path).then_some(path)
        })
        .context("Honeycomb CLI is missing; install Honeycomb or set SILICON_HONEYCOMB")
}

/// Keep each Silicon's package registry separate from personal Honeycomb state.
pub(crate) fn package_home(home: &Path) -> Result<PathBuf> {
    let directory = home.join(".silicon");
    let packages = directory.join("packages");
    for path in [
        &directory,
        &packages,
        &packages.join(".honeycomb"),
        &packages.join(".honeycomb/dir"),
    ] {
        if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_symlink()) {
            bail!("managed package directories must not be symlinks");
        }
    }
    if packages.join(".honeycomb/home.json").exists() {
        bail!("managed Honeycomb home must not redirect to another directory");
    }
    crate::state::private_dir(&packages)?;
    Ok(packages)
}

pub(crate) fn prepare_honeycomb(home: &Path) -> Result<PathBuf> {
    let packages = package_home(home)?;
    // Earlier interpreters forced this private home's updates off. Restore Honeycomb's
    // own default once; later app/user preferences belong to Honeycomb. 4.0.6 deleted
    // the setting instead, and Honeycomb requires it, so a missing one is always
    // repaired: without it every command in this home fails as invalid configuration.
    let migrated = packages.join(".silicon-update-policy-migrated");
    let config = packages.join(".honeycomb/dir/config.json");
    if let Ok(mut settings) =
        serde_json::from_slice::<Value>(&fs::read(&config).unwrap_or_default())
    {
        if let Some(object) = settings.as_object_mut() {
            let forced_off = object.get("auto_update") == Some(&Value::Bool(false));
            if !object.contains_key("auto_update") || (forced_off && !migrated.exists()) {
                object.insert("auto_update".into(), Value::Bool(true));
                crate::state::write_json(&config, &settings)?;
            }
        }
    }
    if !migrated.exists() {
        fs::write(migrated, "")?;
    }
    Ok(packages)
}

fn run(home: &Path, binary: &Path, args: &[&str]) -> Result<Value> {
    let packages = prepare_honeycomb(home)?;
    invoke(&packages, binary, args)
}

fn invoke(home: &Path, binary: &Path, args: &[&str]) -> Result<Value> {
    // Packages live in a private registry. Unrelated commands on the user's PATH
    // must not block its installs; Honeycomb and expose still reject owned-home collisions.
    let output = crate::command(binary, home)
        .env_remove("PATH")
        .args(args)
        .arg("--json")
        .stdin(Stdio::null())
        .output()
        .context("could not execute Honeycomb")?;
    if !output.status.success() {
        // Captured app output may contain credentials, even on a package operation.
        bail!("Honeycomb {} failed ({}); run the same honeycomb command with SILICON_HOME={} to inspect its diagnostic", args[0], output.status, home.display());
    }
    serde_json::from_slice(&output.stdout).context("Honeycomb returned invalid JSON")
}

fn commands(records: &Value, id: &str) -> Result<BTreeMap<String, PathBuf>> {
    if !records.is_object() {
        bail!("Honeycomb installed must return an app-ID object");
    }
    let Some(record) = records.get(id) else {
        return Ok(BTreeMap::new());
    };
    if record.get("app_id").and_then(Value::as_str) != Some(id) {
        bail!("Honeycomb installation identity does not match {id}");
    }
    let commands = record
        .get("commands")
        .and_then(Value::as_object)
        .context("Honeycomb installation has no command map")?;
    let mut result = BTreeMap::new();
    for (name, path) in commands {
        if name.is_empty()
            || name == "."
            || name == ".."
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
        {
            bail!("Honeycomb returned an unsafe command name");
        }
        let path = PathBuf::from(
            path.as_str()
                .context("Honeycomb command path must be a string")?,
        );
        if !path.is_absolute() {
            bail!("Honeycomb command {name} is not an absolute path");
        }
        result.insert(name.clone(), path);
    }
    if result.is_empty() {
        bail!("Honeycomb installation has no commands");
    }
    Ok(result)
}

fn matches(home: &Path, executable: &Path, id: &str) -> bool {
    crate::command(executable, home)
        .args(["iam", "--json"])
        .stdin(Stdio::null())
        .output()
        .ok()
        .is_some_and(|output| {
            output.status.success()
                && serde_json::from_slice::<Value>(&output.stdout)
                    .is_ok_and(|v| v.get("app_id").and_then(Value::as_str) == Some(id))
        })
}

/// Resolve the executable through app discovery, including packages with renamed commands.
pub(crate) fn resolve(home: &Path, id: &str) -> Result<Option<PathBuf>> {
    validate(id)?;
    let canonical = home.canonicalize().context("SILICON_HOME must exist")?;
    let home = canonical.as_path();
    let name = id.split_once('>').unwrap().1;
    for path in [Some(home.join(".silicon/bin").join(name)), on_path(name)]
        .into_iter()
        .flatten()
    {
        if executable(&path) && matches(home, &path, id) {
            return Ok(Some(path));
        }
    }
    let packages = package_home(home)?;
    // A native app on PATH is reusable; only inspect the package registry owned by this home.
    if packages.join(".honeycomb").exists() {
        let binary = honeycomb(home)?;
        let records = run(home, &binary, &["installed"])?;
        let installed = commands(&records, id)?;
        for path in installed.values() {
            if matches(home, path, id) {
                expose(home, &installed)?;
                return Ok(Some(path.clone()));
            }
        }
        if records.get(id).is_some() {
            bail!("no installed command advertises IAM app {id}");
        }
    }
    Ok(None)
}

fn expose(home: &Path, commands: &BTreeMap<String, PathBuf>) -> Result<()> {
    let bin = home.join(".silicon/bin");
    crate::state::private_dir(&bin)?;
    for (name, path) in commands {
        if !executable(path) {
            bail!("Honeycomb command {name} is missing or not executable");
        }
        let alias = bin.join(name);
        match fs::symlink_metadata(&alias) {
            Ok(_) if fs::read_link(&alias).is_ok_and(|target| target == *path) => continue,
            Ok(_) => bail!(
                "app command collision at {}; remove or rename that command before installing",
                alias.display()
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        std::os::unix::fs::symlink(path, alias)?;
    }
    Ok(())
}

pub(crate) fn install_at(home: &Path, id: &str) -> Result<Value> {
    install_using(home, id, &honeycomb(home)?)
}

fn install_using(home: &Path, id: &str, binary: &Path) -> Result<Value> {
    validate(id)?;
    let mut result = run(home, binary, &["install", id])?;
    let records = run(home, binary, &["installed"])?;
    let commands = commands(&records, id)?;
    if commands.is_empty() {
        bail!("Honeycomb did not register installed app {id}");
    }
    expose(home, &commands)?;
    result
        .as_object_mut()
        .context("Honeycomb install must return an object")?
        .insert(
            "silicon_bin_directory".into(),
            json!(home.join(".silicon/bin")),
        );
    Ok(result)
}

fn selected_home() -> Result<PathBuf> {
    PathBuf::from(
        std::env::var_os("SILICON_HOME")
            .or_else(|| std::env::var_os("HOME"))
            .ok_or_else(|| anyhow!("set SILICON_HOME or HOME for application installation"))?,
    )
    .canonicalize()
    .context("application home must exist")
}

/// Install the current distribution using Honeycomb's verified package installer.
pub fn install(id: &str) -> Result<Value> {
    install_at(&selected_home()?, id)
}

/// Resolve the latest release afresh on every connect; authentication has its own cache.
pub(crate) fn install_all(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    let mut ids = Vec::new();
    for id in configured.iter().filter(|id| valid_id(id)) {
        if !ids.contains(id) {
            ids.push(id.clone());
        }
    }
    if ids.is_empty() {
        return Ok(());
    }
    if !ids.iter().any(|id| id == "tos>iam") {
        ids.insert(0, "tos>iam".into());
    }
    for id in ids {
        crate::progress::step(
            home,
            Some(generation),
            &format!("Installing latest {id} through Honeycomb"),
            &format!("Installed latest {id}"),
            || install_at(home, &id),
        )?;
    }
    Ok(())
}

/// Honeycomb removes only its owned package files; application credentials remain app-owned.
pub fn uninstall(id: &str) -> Result<Value> {
    let home = selected_home()?;
    uninstall_using(&home, id, &honeycomb(&home)?)
}

fn uninstall_using(home: &Path, id: &str, binary: &Path) -> Result<Value> {
    validate(id)?;
    let records = run(home, binary, &["installed"])?;
    let commands = commands(&records, id)?;
    let result = run(home, binary, &["uninstall", id])?;
    for (name, path) in commands {
        let alias = home.join(".silicon/bin").join(name);
        if fs::read_link(&alias).is_ok_and(|target| target == path) {
            fs::remove_file(alias)?;
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn honeycomb_lifecycle_validates_identity_paths_and_owned_aliases() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let canonical = dir.path().canonicalize()?;
        let home = canonical.as_path();
        let app = home.join("app with spaces");
        fs::write(
            &app,
            "#!/bin/sh\nprintf '%s\\n' '{\"app_id\":\"test>app\"}'\n",
        )?;
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700))?;
        let records = json!({"test>app":{"app_id":"test>app","commands":{"renamed":app}}});
        fs::write(home.join("records.json"), records.to_string())?;
        let cli = home.join("honeycomb");
        fs::write(
            &cli,
            r#"#!/bin/sh
set -eu
[ "$PWD" = "$SILICON_HOME" ]
case "$1" in
install) [ "$*" = 'install test>app --json' ]; echo install >> calls; touch installed; mkdir -p .honeycomb; echo '{"status":"installed"}' ;;
installed) if [ -f installed ]; then cat ../../records.json; else echo '{}'; fi ;;
uninstall) rm installed; echo '{"uninstalled":"test>app"}' ;;
*) exit 2 ;;
esac
"#,
        )?;
        fs::set_permissions(&cli, fs::Permissions::from_mode(0o700))?;
        let packages = package_home(home)?;
        let config = packages.join(".honeycomb/dir/config.json");
        crate::state::write_json(&config, &json!({"auto_update":false,"telemetry":false}))?;
        prepare_honeycomb(home)?;
        // Honeycomb requires this setting, so the migration restores its default value.
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(&config)?)?,
            json!({"auto_update":true,"telemetry":false})
        );
        // Once migrated, the interpreter leaves any later user preference intact.
        crate::state::write_json(&config, &json!({"auto_update":false}))?;
        prepare_honeycomb(home)?;
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(&config)?)?["auto_update"],
            false
        );
        // A home left without the setting by 4.0.6 is repaired even after migration.
        crate::state::write_json(&config, &json!({"telemetry":false}))?;
        prepare_honeycomb(home)?;
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(&config)?)?,
            json!({"auto_update":true,"telemetry":false})
        );
        // Configuration the interpreter cannot read stays untouched for Honeycomb to report.
        fs::write(&config, "not json")?;
        prepare_honeycomb(home)?;
        assert_eq!(fs::read_to_string(&config)?, "not json");
        fs::remove_file(&config)?;
        assert!(valid_id("tos>space-station"));
        for id in [
            "../>app",
            "Tos>app",
            "tos>app;touch x",
            "tos>app>other",
            "tos>",
            "-tos>app",
        ] {
            assert!(!valid_id(id));
        }
        install_using(home, "test>app", &cli)?;
        assert!(!home.join(".honeycomb").exists());
        assert!(!home
            .join(".silicon/packages/.honeycomb/dir/config.json")
            .exists());
        assert_eq!(fs::read_link(home.join(".silicon/bin/renamed"))?, app);
        assert!(matches(home, &app, "test>app"));
        assert!(!matches(home, &app, "test>other"));
        std::os::unix::fs::symlink(&app, home.join(".silicon/bin/app"))?;
        fs::remove_file(home.join(".silicon/bin/app"))?;
        install_using(home, "test>app", &cli)?;
        assert_eq!(
            fs::read_to_string(home.join(".silicon/packages/calls"))?,
            "install\ninstall\n"
        );
        uninstall_using(home, "test>app", &cli)?;
        assert!(!home.join(".silicon/bin/renamed").exists());
        fs::write(home.join(".silicon/bin/renamed"), "unmanaged")?;
        assert!(install_using(home, "test>app", &cli).is_err());
        uninstall_using(home, "test>app", &cli)?;
        assert_eq!(
            fs::read_to_string(home.join(".silicon/bin/renamed"))?,
            "unmanaged"
        );
        fs::remove_file(home.join(".silicon/bin/renamed"))?;
        install_using(home, "test>app", &cli)?;
        fs::remove_file(&app)?;
        uninstall_using(home, "test>app", &cli)?;
        assert!(fs::symlink_metadata(home.join(".silicon/bin/renamed")).is_err());
        for records in [
            json!([]),
            json!({"test>app":{"app_id":"other>app","commands":{"cli":app}}}),
            json!({"test>app":{"app_id":"test>app","commands":{"../cli":app}}}),
            json!({"test>app":{"app_id":"test>app","commands":{"cli":"relative"}}}),
        ] {
            assert!(commands(&records, "test>app").is_err());
        }
        Ok(())
    }
}
