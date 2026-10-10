//! Silicon Apps owns package downloads, platform selection, verification and removal.
use crate::failure;
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

/// Silicon Apps's bare app handle grammar; these values are arguments, never shell text.
pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 30
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-".contains(&b))
}

fn validate(id: &str) -> Result<()> {
    if !valid_id(id) {
        bail!("{id:?} is not a bare Silicon Apps app ID (1–30 lowercase letters, digits, underscores or hyphens)");
    }
    Ok(())
}

/// Why `path` cannot be run: the OS error, or what its metadata shows.
fn runnable(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let metadata =
        fs::metadata(path).with_context(|| format!("cannot inspect {}", path.display()))?;
    if !metadata.is_file() {
        bail!("{} is not a regular file", path.display());
    }
    let mode = metadata.permissions().mode();
    if mode & 0o111 == 0 {
        bail!(
            "{} is not executable (mode {:o})",
            path.display(),
            mode & 0o7777
        );
    }
    Ok(())
}

fn executable(path: &Path) -> bool {
    runnable(path).is_ok()
}

fn on_path(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path)
            .map(|p| p.join(name))
            .find(|p| executable(p))
    })
}

pub(crate) fn silicon_apps(home: &Path) -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("SILICON_APPS") {
        return Ok(PathBuf::from(path));
    }
    if let Some(path) = on_path("silicon-apps") {
        return Ok(path);
    }
    // Say where it looked and why each place failed, not only that it is missing.
    let mut searched = vec![format!(
        "no executable silicon-apps on PATH={}",
        std::env::var_os("PATH")
            .unwrap_or_default()
            .to_string_lossy()
    )];
    let mut candidates = Vec::new();
    match crate::update::managed_prefix() {
        Ok(prefix) => candidates.push(prefix.join(".apps/bin/silicon-apps")),
        Err(error) => searched.push(format!("no managed bundle: {error:#}")),
    }
    candidates.push(home.join(".apps/bin/silicon-apps"));
    match std::env::var_os("HOME") {
        Some(user) => candidates.push(PathBuf::from(user).join(".apps/bin/silicon-apps")),
        None => searched.push("HOME is not set".into()),
    }
    for path in candidates {
        match runnable(&path) {
            Ok(()) => return Ok(path),
            Err(error) => searched.push(format!("{error:#}")),
        }
    }
    bail!(
        "Silicon Apps CLI is missing; install Silicon Apps or set SILICON_APPS. Searched:\n{}",
        searched.join("\n")
    )
}

/// Apps state belongs to the selected Carbon or Silicon home, including its session.
pub(crate) fn package_home(home: &Path) -> Result<PathBuf> {
    for path in [
        home.join(".apps"),
        home.join(".apps/bin"),
        home.join(".apps/installed"),
    ] {
        if fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) {
            bail!(
                "managed app directories must not be symlinks: {}",
                path.display()
            );
        }
    }
    Ok(home.to_path_buf())
}

fn run(home: &Path, binary: &Path, args: &[&str]) -> Result<Value> {
    let packages = package_home(home)?;
    invoke(home, &packages, binary, args)
}

/// Silicon Apps's own diagnostic is the error: exit status and both streams, verbatim.
fn invoke(home: &Path, packages: &Path, binary: &Path, args: &[&str]) -> Result<Value> {
    invoke_limited(home, packages, binary, args, crate::process::Limit::Install)
}

fn invoke_limited(
    home: &Path,
    packages: &Path,
    binary: &Path,
    args: &[&str],
    limit: crate::process::Limit,
) -> Result<Value> {
    let home_arg = packages
        .to_str()
        .context("Silicon Apps home must be UTF-8")?;
    let args = [&["--home", home_arg, "--json"][..], args].concat();
    let command = failure::argv(binary, &args);
    let context = || {
        format!(
            "running Silicon Apps {} with SILICON_HOME={}",
            binary.display(),
            packages.display()
        )
    };
    let mut silicon_apps = crate::command(binary, packages);
    silicon_apps.args(&args);
    let output = crate::process::output(&mut silicon_apps, limit)
        .map_err(|error| failure::spawn(home, &command, &error))
        .with_context(context)?;
    if !output.status.success() {
        return Err(failure::command(home, &command, &output, &[])).with_context(context);
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| {
            failure::answer(
                home,
                &command,
                &format!("returned invalid JSON: {error}"),
                &output,
                &[],
            )
        })
        .with_context(context)
}

/// Silicon Apps's answer as the reader should see it in an error.
fn shown(home: &Path, value: &Value) -> String {
    failure::mask(home, &value.to_string(), &[])
}

#[derive(Deserialize, Serialize)]
struct AppDescription {
    app_id: String,
    name: String,
    description: String,
}

/// A shared DNA file: declaring an app on an ISI does not restrict other ISIs' access.
/// Keep Silicon Apps's last successful metadata so offline reconnects retain the descriptions.
pub(crate) fn refresh_dna(cfg: &crate::config::Config) -> Result<()> {
    let packages = package_home(&cfg.home)?;
    let binary = silicon_apps(&cfg.home);
    refresh_dna_using(&cfg.home, &cfg.managed_apps(), cfg.generation, |id| {
        let binary = binary.as_ref().map_err(|error| anyhow!("{error:#}"))?;
        invoke_limited(
            &cfg.home,
            &packages,
            binary,
            &["show", "--", id],
            crate::process::Limit::App,
        )
    })
}

fn refresh_dna_using(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
    mut fetch: impl FnMut(&str) -> Result<Value>,
) -> Result<()> {
    let path = home.join(".silicon/app-descriptions.json");
    let cached = match fs::read(&path) {
        Ok(bytes) => serde_json::from_slice::<BTreeMap<String, AppDescription>>(&bytes)
            .with_context(|| format!("invalid app metadata cache {}", path.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(BTreeMap::new()),
        Err(error) => Err(error).with_context(|| format!("cannot read {}", path.display())),
    };
    let mut cached = cached.unwrap_or_else(|error| {
        logged(home, generation, "silicon_apps", &format!("{error:#}"));
        BTreeMap::new()
    });
    let ids: BTreeSet<_> = configured.iter().filter(|id| valid_id(id)).collect();
    cached.retain(|id, app| ids.contains(id) && app.app_id == *id && !app.name.trim().is_empty());
    let mut entries = Vec::new();
    for id in ids {
        let metadata = fetch(id).and_then(|value| {
            let app: AppDescription = serde_json::from_value(value).with_context(|| {
                format!(
                    "Silicon Apps metadata for {id} must include app_id, name and description strings"
                )
            })?;
            if app.app_id != *id || app.name.trim().is_empty() {
                bail!("Silicon Apps metadata identity or name does not match {id}");
            }
            Ok(app)
        });
        match metadata {
            Ok(app) => { cached.insert(id.clone(), app); }
            Err(error) => logged(home, generation, id, &format!("could not refresh app DNA from Silicon Apps: {error:#}; keeping cached details when available")),
        }
        let (name, description) = cached
            .get(id)
            .map(|app| (app.name.as_str(), app.description.as_str()))
            .unwrap_or((
                "Unavailable",
                "Silicon Apps metadata is unavailable. Use the CLI help for this app.",
            ));
        entries.push(format!("App Name: {name}\nApp Id: {id}\nCLI: run `{id} --help` to know about it\n\nAbout: {description}"));
    }
    crate::state::write_json(&path, &cached)?;
    let dna = home.join(".silicon/apps.md");
    let staged = home.join(format!(".silicon/.{}.tmp", uuid::Uuid::new_v4()));
    fs::write(&staged, entries.join("\n\n\n"))
        .with_context(|| format!("cannot write app DNA {}", staged.display()))?;
    let result = fs::rename(&staged, &dna)
        .with_context(|| format!("cannot replace app DNA {}", dna.display()));
    if result.is_err() {
        let _ = fs::remove_file(&staged);
    }
    result
}

fn commands(home: &Path, records: &Value, id: &str) -> Result<BTreeMap<String, PathBuf>> {
    let items = records
        .get("items")
        .and_then(Value::as_array)
        .with_context(|| {
            format!(
                "Silicon Apps installed must return an items array, got: {}",
                shown(home, records)
            )
        })?;
    let mut result = BTreeMap::new();
    for record in items.iter().filter(|record| {
        record.get("app_id").and_then(Value::as_str) == Some(id)
            && record.get("channel").and_then(Value::as_str) == Some("production")
    }) {
        let name = record
            .get("command")
            .and_then(Value::as_str)
            .with_context(|| {
                format!(
                    "Silicon Apps installation of {id} has no command: {}",
                    shown(home, record)
                )
            })?;
        if name.is_empty()
            || name.len() > 80
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
        {
            bail!("Silicon Apps returned an unsafe command name {name:?} for {id}");
        }
        result.insert(name.to_owned(), home.join(".apps/bin").join(name));
    }
    Ok(result)
}

/// Ok when `executable accounts --json` names `id`; otherwise what it actually did.
fn advertises(home: &Path, executable: &Path, id: &str) -> Result<()> {
    let command = failure::argv(executable, &["accounts", "--json"]);
    let output = crate::process::output(
        crate::command(executable, home).args(["accounts", "--json"]),
        crate::process::Limit::App,
    )
    .map_err(|error| failure::spawn(home, &command, &error))?;
    if !output.status.success() {
        return Err(failure::command(home, &command, &output, &[]));
    }
    let problem = match serde_json::from_slice::<Value>(&output.stdout) {
        Err(error) => format!("returned invalid JSON: {error}"),
        Ok(value) => match value.get("app_id") {
            Some(Value::String(found)) if found == id => return Ok(()),
            Some(found) => format!("advertises app_id {found}, not {id:?}"),
            None => format!("returned no app_id; expected {id:?}"),
        },
    };
    Err(failure::answer(home, &command, &problem, &output, &[]))
}

/// The command that answers as an app, or every candidate passed over and why.
/// An empty list means nothing named the app was found at all.
pub(crate) type Resolution = std::result::Result<PathBuf, Vec<String>>;

/// Resolve the executable through app discovery, including packages with renamed commands.
pub(crate) fn resolve(home: &Path, id: &str) -> Result<Resolution> {
    resolve_using(home, id, silicon_apps)
}

fn resolve_using(
    home: &Path,
    id: &str,
    silicon_apps: impl FnOnce(&Path) -> Result<PathBuf>,
) -> Result<Resolution> {
    validate(id)?;
    let canonical = home
        .canonicalize()
        .with_context(|| format!("SILICON_HOME {} must exist", home.display()))?;
    let home = canonical.as_path();
    // A candidate that fails its identity check is passed over, never forgotten:
    // a crashing app would otherwise look exactly like a missing one.
    let mut rejected = Vec::new();
    let found = discover(home, id, silicon_apps, &mut rejected);
    if let Ok(Some(path)) = found {
        return Ok(Ok(path));
    }
    // Whatever ended the search, the candidates passed over on the way stay on record,
    // even when a Silicon Apps install follows and succeeds.
    let logged = rejected.iter().try_for_each(|reason| {
        crate::log_line(
            home,
            "error",
            id,
            &format!("passed over while resolving Silicon app {id}: {reason}"),
        )
    });
    match found {
        Ok(_) => logged.map(|()| Err(rejected)),
        Err(error) if rejected.is_empty() => Err(crate::failure::also(error, logged)),
        Err(error) => Err(crate::failure::also(
            error.context(format!(
                "resolving Silicon app {id}, after passing over:\n{}",
                rejected.join("\n")
            )),
            logged,
        )),
    }
}

/// True when `path` answers as `id`; otherwise its reason joins `rejected`.
fn answers(home: &Path, path: &Path, id: &str, rejected: &mut Vec<String>) -> bool {
    match advertises(home, path, id) {
        Ok(()) => true,
        Err(error) => {
            rejected.push(format!(
                "{} is not Silicon app {id}: {error:#}",
                path.display()
            ));
            false
        }
    }
}

fn discover(
    home: &Path,
    id: &str,
    silicon_apps: impl FnOnce(&Path) -> Result<PathBuf>,
    rejected: &mut Vec<String>,
) -> Result<Option<PathBuf>> {
    let own = home.join(".silicon/bin").join(id);
    match runnable(&own) {
        Ok(()) if answers(home, &own, id, rejected) => return Ok(Some(own)),
        Ok(()) => {}
        // Present but unrunnable (e.g. a removed package's dangling link) is a reason, not absence.
        Err(error) if fs::symlink_metadata(&own).is_ok() => {
            let target = fs::read_link(&own)
                .map(|target| format!(" (it links to {})", target.display()))
                .unwrap_or_default();
            rejected.push(format!("{}{target} cannot run: {error:#}", own.display()));
        }
        Err(_) => {}
    }
    if let Some(path) = on_path(id).filter(|path| *path != own) {
        if answers(home, &path, id, rejected) {
            return Ok(Some(path));
        }
    }
    let packages = package_home(home)?;
    // A native app on PATH is reusable; only inspect the package registry owned by this home.
    if packages.join(".apps").exists() {
        let binary = silicon_apps(home)?;
        let records = run(home, &binary, &["installed"])?;
        let installed = commands(home, &records, id)?;
        for path in installed.values() {
            if answers(home, path, id, rejected) {
                expose(home, &installed)?;
                return Ok(Some(path.clone()));
            }
        }
        if !installed.is_empty() {
            // The reasons become the error, so they are not logged a second time.
            bail!(
                "no installed command advertises Silicon app {id}:\n{}",
                std::mem::take(rejected).join("\n")
            );
        }
    }
    Ok(None)
}

fn expose(home: &Path, commands: &BTreeMap<String, PathBuf>) -> Result<()> {
    let bin = home.join(".silicon/bin");
    crate::state::private_dir(&bin)?;
    for (name, path) in commands {
        runnable(path).with_context(|| format!("Silicon Apps command {name} cannot be run"))?;
        let alias = bin.join(name);
        match fs::symlink_metadata(&alias) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                let target = fs::read_link(&alias)
                    .with_context(|| format!("cannot read link {}", alias.display()))?;
                if target == *path {
                    continue;
                }
                if target.starts_with(home.join(".silicon/packages")) {
                    fs::remove_file(&alias)
                        .with_context(|| format!("cannot replace app command {}", alias.display()))?;
                    std::os::unix::fs::symlink(path, &alias)
                        .with_context(|| format!("cannot link {} to {}", alias.display(), path.display()))?;
                    continue;
                }
                bail!(
                    "app command collision at {}: it links to {}, but Silicon Apps installs {name} at {}; remove or rename that command before installing",
                    alias.display(),
                    target.display(),
                    path.display()
                );
            }
            Ok(metadata) => bail!(
                "app command collision at {}: an unmanaged {} is there, but Silicon Apps installs {name} at {}; remove or rename that command before installing",
                alias.display(),
                if metadata.is_dir() { "directory" } else { "file" },
                path.display()
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| format!("cannot inspect {}", alias.display()))
            }
        }
        std::os::unix::fs::symlink(path, &alias)
            .with_context(|| format!("cannot link {} to {}", alias.display(), path.display()))?;
    }
    Ok(())
}

pub(crate) fn install_at(home: &Path, id: &str) -> Result<Value> {
    install_using(home, id, &silicon_apps(home)?)
}

fn install_using(home: &Path, id: &str, binary: &Path) -> Result<Value> {
    validate(id)?;
    let mut result = run(home, binary, &["install", "--yes", "--", id])?;
    let records = run(home, binary, &["installed"])?;
    let commands = commands(home, &records, id)?;
    if commands.is_empty() {
        bail!(
            "Silicon Apps did not register installed app {id}; `silicon-apps installed` returned: {}",
            shown(home, &records)
        );
    }
    expose(home, &commands)?;
    let Some(object) = result.as_object_mut() else {
        bail!(
            "Silicon Apps install {id} must return an object, got: {}",
            shown(home, &result)
        );
    };
    // `json!` of a path panics when it is not UTF-8; this field only informs.
    object.insert(
        "silicon_bin_directory".into(),
        json!(home.join(".silicon/bin").to_string_lossy()),
    );
    Ok(result)
}

fn selected_home() -> Result<PathBuf> {
    let home = PathBuf::from(
        std::env::var_os("SILICON_HOME")
            .or_else(|| std::env::var_os("HOME"))
            .ok_or_else(|| anyhow!("set SILICON_HOME or HOME for application installation"))?,
    );
    home.canonicalize()
        .with_context(|| format!("application home {} must exist", home.display()))
}

/// Install the current distribution using Silicon Apps's verified package installer.
pub fn install(id: &str) -> Result<Value> {
    install_at(&selected_home()?, id)
}

/// Resolve the latest release afresh on every connect; authentication has its own cache.
///
/// A Silicon must come back after a reboot without the network: when Silicon Apps cannot
/// install the latest release (no DNS yet, Silicon Apps or its backend down, a registry
/// lock) but a working copy is already installed, the failure is logged in full and the
/// installed copy is used. Only an app that is not installed at all stops the connection.
pub(crate) fn install_all(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    install_all_using(home, configured, generation, &silicon_apps)
}

fn install_all_using(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
    silicon_apps: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<()> {
    let mut ids = Vec::new();
    // Login entries that are commands rather than app IDs authenticate without Silicon Apps.
    for id in configured.iter().filter(|id| valid_id(id)) {
        if !ids.contains(id) {
            ids.push(id.clone());
        }
    }
    if ids.is_empty() {
        return Ok(());
    }
    if !ids.iter().any(|id| id == "silicon-accounts") {
        ids.insert(0, "silicon-accounts".into());
    }
    for id in ids {
        install_or_keep(home, &id, generation, silicon_apps)?;
    }
    Ok(())
}

/// Install the app IDs this home's managed app registry lists but `configured` (the YAML's
/// apps, as [`install_all`] was given them) does not: ones added by `si auth setup`, and by
/// older interpreters for every app they checked. They are installed like configured
/// apps, but one that Silicon Apps cannot install and that has no working installed copy
/// (unpublished or renamed since) is logged in full and left out, so the Silicon still
/// connects and `si auth remove` can drop it. Its automatic check then reports it too.
pub fn install_registered(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    let registered = crate::auth::registered(home)?;
    install_registered_using(home, &registered, configured, generation, &silicon_apps)
}

fn install_registered_using(
    home: &Path,
    registered: &[String],
    configured: &[String],
    generation: uuid::Uuid,
    silicon_apps: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<()> {
    let mut done: Vec<&str> = configured.iter().map(String::as_str).collect();
    // `install_all` always installs Silicon Accounts first.
    done.push("silicon-accounts");
    for id in registered.iter().filter(|id| valid_id(id)) {
        if done.contains(&id.as_str()) {
            continue;
        }
        done.push(id);
        if let Err(error) = install_or_keep(home, id, generation, silicon_apps) {
            logged(
                home,
                generation,
                id,
                &format!(
                    "{error:#}; continuing without it, since it is in the managed app registry but not in the YAML; remove it with `si auth remove {id}`"
                ),
            );
        }
    }
    Ok(())
}

/// Ask Silicon Apps for the latest `id`. When that fails but a working copy is installed, the
/// failure is logged and the copy kept; only when no usable copy exists is it returned.
fn install_or_keep(
    home: &Path,
    id: &str,
    generation: uuid::Uuid,
    silicon_apps: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<()> {
    let installed = step(
        home,
        Some(generation),
        &format!("Installing latest {id} through Silicon Apps"),
        &format!("Installed latest {id}"),
        || install_using(home, id, &silicon_apps(home)?),
    );
    let Err(error) = installed else {
        return Ok(());
    };
    match usable(home, id, silicon_apps) {
        Ok(Some(path)) => {
            logged(
                home,
                generation,
                id,
                &format!(
                    "could not install the latest {id} through Silicon Apps, so {} (which answers as {id}) stays in use: {error:#}; continuing with the installed {id}",
                    path.display()
                ),
            );
            Ok(())
        }
        Ok(None) => Err(error),
        Err(checked) => Err(failure::also(
            error,
            Err(checked.context(format!("looking for an installed copy of {id}"))),
        )),
    }
}

/// An `[error]` line under `id` in silicon.log, or in daemon.log when silicon.log cannot
/// take it; the connection goes on either way.
fn logged(home: &Path, generation: uuid::Uuid, id: &str, message: &str) {
    if let Err(error) = crate::log_line_scoped(home, Some(generation), "error", id, message) {
        crate::stderr_line(&format!(
            "{error:#}; the {id} error it was recording: {}",
            failure::mask(home, message, &[])
        ));
    }
}

/// The installed copy must identify itself with the shared Accounts contract.
fn usable(
    home: &Path,
    id: &str,
    silicon_apps: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<Option<PathBuf>> {
    Ok(resolve_using(home, id, silicon_apps)?.ok())
}

/// [`crate::progress::step`] for work whose progress is only a display: a failed progress
/// record is reported (in daemon.log, since silicon.log is what failed) and never stops
/// the work or replaces its outcome.
pub(crate) fn step<T>(
    home: &Path,
    generation: Option<uuid::Uuid>,
    working: &str,
    complete: &str,
    action: impl FnOnce() -> Result<T>,
) -> Result<T> {
    let mut action = Some(action);
    let mut value = None;
    let recorded = crate::progress::step(home, generation, working, complete, || {
        if let Some(action) = action.take() {
            value = Some(action()?);
        }
        Ok(())
    });
    let unrecorded = |error: &anyhow::Error| {
        crate::stderr_line(&format!(
            "progress of \"{working}\" was not recorded: {error:#}"
        ))
    };
    match (value, recorded) {
        (Some(value), Ok(())) => Ok(value),
        (Some(value), Err(error)) => {
            unrecorded(&error);
            Ok(value)
        }
        (None, Err(error)) => match action.take() {
            // The record before the work failed, so the work has not run yet.
            Some(action) => {
                unrecorded(&error);
                action()
            }
            // The work ran and failed; a failed record already rides along with it.
            None => Err(error),
        },
        (None, Ok(())) => Err(anyhow!("\"{working}\" finished without its result")),
    }
}

/// Silicon Apps removes only its owned package files; application credentials remain app-owned.
pub fn uninstall(id: &str) -> Result<Value> {
    let home = selected_home()?;
    uninstall_at(&home, id)
}

pub(crate) fn uninstall_at(home: &Path, id: &str) -> Result<Value> {
    uninstall_using(home, id, &silicon_apps(home)?)
}

fn uninstall_using(home: &Path, id: &str, binary: &Path) -> Result<Value> {
    validate(id)?;
    let records = run(home, binary, &["installed"])?;
    let commands = commands(home, &records, id)?;
    let result = run(home, binary, &["uninstall", "--", id])?;
    for (name, path) in commands {
        let alias = home.join(".silicon/bin").join(name);
        // Only aliases this home created are removed; anything else is not ours.
        if fs::read_link(&alias).is_ok_and(|target| target == path) {
            fs::remove_file(&alias)
                .with_context(|| format!("cannot remove app command {}", alias.display()))?;
        }
    }
    Ok(result)
}
