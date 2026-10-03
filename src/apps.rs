//! Honeycomb owns package downloads, platform selection, verification and removal.
use crate::failure;
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
};

/// Honeycomb's bare app handle grammar; these values are arguments, never shell text.
pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 80
        && id.as_bytes()[0].is_ascii_lowercase()
        && id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-".contains(&b))
}

fn validate(id: &str) -> Result<()> {
    if !valid_id(id) {
        bail!("{id:?} is not a bare Honeycomb app ID (1–80 lowercase letters, digits, underscores or hyphens, starting with a letter); migrate old org>app IDs using IAM's verified mapping");
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

pub(crate) fn honeycomb(home: &Path) -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("SILICON_HONEYCOMB") {
        return Ok(PathBuf::from(path));
    }
    if let Some(path) = on_path("honeycomb") {
        return Ok(path);
    }
    // Say where it looked and why each place failed, not only that it is missing.
    let mut searched = vec![format!(
        "no executable honeycomb on PATH={}",
        std::env::var_os("PATH")
            .unwrap_or_default()
            .to_string_lossy()
    )];
    let mut candidates = Vec::new();
    match crate::update::managed_prefix() {
        Ok(prefix) => candidates.push(prefix.join(".honeycomb/dir/system/bin/honeycomb")),
        Err(error) => searched.push(format!("no managed bundle: {error:#}")),
    }
    candidates.push(home.join(".honeycomb/dir/system/bin/honeycomb"));
    match std::env::var_os("HOME") {
        Some(user) => {
            candidates.push(PathBuf::from(user).join(".honeycomb/dir/system/bin/honeycomb"))
        }
        None => searched.push("HOME is not set".into()),
    }
    for path in candidates {
        match runnable(&path) {
            Ok(()) => return Ok(path),
            Err(error) => searched.push(format!("{error:#}")),
        }
    }
    bail!(
        "Honeycomb CLI is missing; install Honeycomb or set SILICON_HONEYCOMB. Searched:\n{}",
        searched.join("\n")
    )
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
            let target = fs::read_link(path)
                .map(|target| target.display().to_string())
                .unwrap_or_else(|error| format!("an unreadable target ({error})"));
            bail!(
                "managed package directories must not be symlinks: {} points to {target}",
                path.display()
            );
        }
    }
    let redirect = packages.join(".honeycomb/home.json");
    if redirect.exists() {
        bail!(
            "managed Honeycomb home must not redirect to another directory; remove {}",
            redirect.display()
        );
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
    // Settings the interpreter cannot read stay untouched for Honeycomb to report;
    // the log says why the repair was skipped.
    let settings = match fs::read(&config) {
        Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
            .with_context(|| format!("{} is not valid JSON", config.display()))
            .and_then(|settings| match settings {
                Value::Object(object) => Ok(Some(object)),
                other => Err(anyhow!(
                    "{} is not a JSON object: {}",
                    config.display(),
                    shown(home, &other)
                )),
            }),
        // Honeycomb creates its settings on first use.
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("cannot read {}", config.display())),
    };
    match settings {
        Ok(Some(mut object)) => {
            let forced_off = object.get("auto_update") == Some(&Value::Bool(false));
            if !object.contains_key("auto_update") || (forced_off && !migrated.exists()) {
                object.insert("auto_update".into(), Value::Bool(true));
                crate::state::write_json(&config, &object)?;
            }
        }
        Ok(None) => {}
        Err(error) => crate::log_line(
            home,
            "error",
            "honeycomb",
            &format!("left Honeycomb settings unrepaired: {error:#}"),
        )?,
    }
    if !migrated.exists() {
        fs::write(&migrated, "").with_context(|| format!("cannot write {}", migrated.display()))?;
    }
    Ok(packages)
}

fn run(home: &Path, binary: &Path, args: &[&str]) -> Result<Value> {
    let packages = prepare_honeycomb(home)?;
    invoke(home, &packages, binary, args)
}

/// Honeycomb's own diagnostic is the error: exit status and both streams, verbatim.
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
    let args = [args, &["--json"]].concat();
    let command = failure::argv(binary, &args);
    let context = || {
        format!(
            "running Honeycomb {} with SILICON_HOME={}",
            binary.display(),
            packages.display()
        )
    };
    // Packages live in a private registry. Unrelated commands on the user's PATH
    // must not block its installs; Honeycomb and expose still reject owned-home collisions.
    let mut honeycomb = crate::command(binary, packages);
    honeycomb.env_remove("PATH").args(&args);
    let output = crate::process::output(&mut honeycomb, limit)
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

/// Honeycomb's answer as the reader should see it in an error.
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
/// Keep Honeycomb's last successful metadata so offline reconnects retain the descriptions.
pub(crate) fn refresh_dna(cfg: &crate::config::Config) -> Result<()> {
    let packages = prepare_honeycomb(&cfg.home)?;
    let binary = honeycomb(&cfg.home);
    refresh_dna_using(&cfg.home, &cfg.managed_apps(), cfg.generation, |id| {
        let binary = binary.as_ref().map_err(|error| anyhow!("{error:#}"))?;
        invoke_limited(
            &cfg.home,
            &packages,
            binary,
            &["apps", "get", id],
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
        logged(home, generation, "honeycomb", &format!("{error:#}"));
        BTreeMap::new()
    });
    let ids: BTreeSet<_> = configured.iter().filter(|id| valid_id(id)).collect();
    cached.retain(|id, app| ids.contains(id) && app.app_id == *id && !app.name.trim().is_empty());
    let mut entries = Vec::new();
    for id in ids {
        let metadata = fetch(id).and_then(|value| {
            let app: AppDescription = serde_json::from_value(value).with_context(|| {
                format!(
                    "Honeycomb metadata for {id} must include app_id, name and description strings"
                )
            })?;
            if app.app_id != *id || app.name.trim().is_empty() {
                bail!("Honeycomb metadata identity or name does not match {id}");
            }
            Ok(app)
        });
        match metadata {
            Ok(app) => { cached.insert(id.clone(), app); }
            Err(error) => logged(home, generation, id, &format!("could not refresh app DNA from Honeycomb: {error:#}; keeping cached details when available")),
        }
        let (name, description) = cached
            .get(id)
            .map(|app| (app.name.as_str(), app.description.as_str()))
            .unwrap_or((
                "Unavailable",
                "Honeycomb metadata is unavailable. Use the CLI help for this app.",
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
    if !records.is_object() {
        bail!(
            "Honeycomb installed must return an app-ID object, got: {}",
            shown(home, records)
        );
    }
    let Some(record) = records.get(id) else {
        return Ok(BTreeMap::new());
    };
    if record.get("app_id").and_then(Value::as_str) != Some(id) {
        bail!(
            "Honeycomb installation identity does not match {id}: {}",
            shown(home, record)
        );
    }
    let commands = record
        .get("commands")
        .and_then(Value::as_object)
        .with_context(|| {
            format!(
                "Honeycomb installation of {id} has no command map: {}",
                shown(home, record)
            )
        })?;
    let mut result = BTreeMap::new();
    for (name, path) in commands {
        if name.is_empty()
            || name == "."
            || name == ".."
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
        {
            bail!(
                "Honeycomb returned an unsafe command name {name:?} for {id}: {}",
                shown(home, record)
            );
        }
        let path = PathBuf::from(path.as_str().with_context(|| {
            format!(
                "Honeycomb command path for {name} must be a string, got: {}",
                shown(home, path)
            )
        })?);
        if !path.is_absolute() {
            bail!(
                "Honeycomb command {name} is not an absolute path: {}",
                path.display()
            );
        }
        result.insert(name.clone(), path);
    }
    if result.is_empty() {
        bail!(
            "Honeycomb installation of {id} has no commands: {}",
            shown(home, record)
        );
    }
    Ok(result)
}

/// Ok when `executable iam --json` names `id`; otherwise what it actually did.
fn advertises(home: &Path, executable: &Path, id: &str) -> Result<()> {
    let command = failure::argv(executable, &["iam", "--json"]);
    let output = crate::process::output(
        crate::command(executable, home).args(["iam", "--json"]),
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
    resolve_using(home, id, honeycomb)
}

fn resolve_using(
    home: &Path,
    id: &str,
    honeycomb: impl FnOnce(&Path) -> Result<PathBuf>,
) -> Result<Resolution> {
    validate(id)?;
    let canonical = home
        .canonicalize()
        .with_context(|| format!("SILICON_HOME {} must exist", home.display()))?;
    let home = canonical.as_path();
    // A candidate that fails its identity check is passed over, never forgotten:
    // a crashing app would otherwise look exactly like a missing one.
    let mut rejected = Vec::new();
    let found = discover(home, id, honeycomb, &mut rejected);
    if let Ok(Some(path)) = found {
        return Ok(Ok(path));
    }
    // Whatever ended the search, the candidates passed over on the way stay on record,
    // even when a Honeycomb install follows and succeeds.
    let logged = rejected.iter().try_for_each(|reason| {
        crate::log_line(
            home,
            "error",
            id,
            &format!("passed over while resolving IAM app {id}: {reason}"),
        )
    });
    match found {
        Ok(_) => logged.map(|()| Err(rejected)),
        Err(error) if rejected.is_empty() => Err(crate::failure::also(error, logged)),
        Err(error) => Err(crate::failure::also(
            error.context(format!(
                "resolving IAM app {id}, after passing over:\n{}",
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
            rejected.push(format!("{} is not IAM app {id}: {error:#}", path.display()));
            false
        }
    }
}

fn discover(
    home: &Path,
    id: &str,
    honeycomb: impl FnOnce(&Path) -> Result<PathBuf>,
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
    if packages.join(".honeycomb").exists() {
        let binary = honeycomb(home)?;
        let records = run(home, &binary, &["installed"])?;
        let installed = commands(home, &records, id)?;
        for path in installed.values() {
            if answers(home, path, id, rejected) {
                expose(home, &installed)?;
                return Ok(Some(path.clone()));
            }
        }
        if records.get(id).is_some() {
            // The reasons become the error, so they are not logged a second time.
            bail!(
                "no installed command advertises IAM app {id}:\n{}",
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
        runnable(path).with_context(|| format!("Honeycomb command {name} cannot be run"))?;
        let alias = bin.join(name);
        match fs::symlink_metadata(&alias) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                let target = fs::read_link(&alias)
                    .with_context(|| format!("cannot read link {}", alias.display()))?;
                if target == *path {
                    continue;
                }
                bail!(
                    "app command collision at {}: it links to {}, but Honeycomb installs {name} at {}; remove or rename that command before installing",
                    alias.display(),
                    target.display(),
                    path.display()
                );
            }
            Ok(metadata) => bail!(
                "app command collision at {}: an unmanaged {} is there, but Honeycomb installs {name} at {}; remove or rename that command before installing",
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
    install_using(home, id, &honeycomb(home)?)
}

fn install_using(home: &Path, id: &str, binary: &Path) -> Result<Value> {
    validate(id)?;
    let mut result = run(home, binary, &["install", id])?;
    let records = run(home, binary, &["installed"])?;
    let commands = commands(home, &records, id)?;
    if commands.is_empty() {
        bail!(
            "Honeycomb did not register installed app {id}; `honeycomb installed` returned: {}",
            shown(home, &records)
        );
    }
    expose(home, &commands)?;
    let Some(object) = result.as_object_mut() else {
        bail!(
            "Honeycomb install {id} must return an object, got: {}",
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

/// Install the current distribution using Honeycomb's verified package installer.
pub fn install(id: &str) -> Result<Value> {
    install_at(&selected_home()?, id)
}

/// Resolve the latest release afresh on every connect; authentication has its own cache.
///
/// A Silicon must come back after a reboot without the network: when Honeycomb cannot
/// install the latest release (no DNS yet, Honeycomb or its backend down, a registry
/// lock) but a working copy is already installed, the failure is logged in full and the
/// installed copy is used. Only an app that is not installed at all stops the connection.
pub(crate) fn install_all(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    install_all_using(home, configured, generation, &honeycomb)
}

fn install_all_using(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
    honeycomb: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<()> {
    let mut ids = Vec::new();
    // Login entries that are commands rather than app IDs authenticate without Honeycomb.
    for id in configured.iter().filter(|id| valid_id(id)) {
        if !ids.contains(id) {
            ids.push(id.clone());
        }
    }
    if ids.is_empty() {
        return Ok(());
    }
    if !ids.iter().any(|id| id == "iam") {
        ids.insert(0, "iam".into());
    }
    for id in ids {
        install_or_keep(home, &id, generation, honeycomb)?;
    }
    Ok(())
}

/// Install the app IDs this home's managed app registry lists but `configured` (the YAML's
/// apps, as [`install_all`] was given them) does not: ones added by `si auth setup`, and by
/// older interpreters for every app they checked. They are installed like configured
/// apps, but one that Honeycomb cannot install and that has no working installed copy
/// (unpublished or renamed since) is logged in full and left out, so the Silicon still
/// connects and `si auth remove` can drop it. Its automatic check then reports it too.
pub fn install_registered(
    home: &Path,
    configured: &[String],
    generation: uuid::Uuid,
) -> Result<()> {
    let registered = crate::auth::registered(home)?;
    install_registered_using(home, &registered, configured, generation, &honeycomb)
}

fn install_registered_using(
    home: &Path,
    registered: &[String],
    configured: &[String],
    generation: uuid::Uuid,
    honeycomb: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<()> {
    let mut done: Vec<&str> = configured.iter().map(String::as_str).collect();
    // `install_all` always installs IAM first.
    done.push("iam");
    for id in registered.iter().filter(|id| valid_id(id)) {
        if done.contains(&id.as_str()) {
            continue;
        }
        done.push(id);
        if let Err(error) = install_or_keep(home, id, generation, honeycomb) {
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

/// Ask Honeycomb for the latest `id`. When that fails but a working copy is installed, the
/// failure is logged and the copy kept; only when no usable copy exists is it returned.
fn install_or_keep(
    home: &Path,
    id: &str,
    generation: uuid::Uuid,
    honeycomb: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<()> {
    let installed = step(
        home,
        Some(generation),
        &format!("Installing latest {id} through Honeycomb"),
        &format!("Installed latest {id}"),
        || install_using(home, id, &honeycomb(home)?),
    );
    let Err(error) = installed else {
        return Ok(());
    };
    match usable(home, id, honeycomb) {
        Ok(Some(path)) => {
            logged(
                home,
                generation,
                id,
                &format!(
                    "could not install the latest {id} through Honeycomb, so {} (which answers as {id}) stays in use: {error:#}; continuing with the installed {id}",
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

/// The copy of `id` this home already runs, when there is one. An app must answer as
/// itself ([`resolve`]). The IAM issuer is not an IAM app and answers no `iam --json`,
/// so its command (this home's, else the one on PATH) must answer its login help instead.
fn usable(
    home: &Path,
    id: &str,
    honeycomb: &dyn Fn(&Path) -> Result<PathBuf>,
) -> Result<Option<PathBuf>> {
    if id != "iam" {
        return Ok(resolve_using(home, id, honeycomb)?.ok());
    }
    let own = home.join(".silicon/bin/iam");
    let path = if executable(&own) {
        own
    } else if let Some(path) = on_path("iam") {
        path
    } else {
        return Ok(None);
    };
    const HELP: &[&str] = &["silicon-login", "--help"];
    let shown = failure::argv(&path, HELP);
    let output = crate::process::output(
        crate::command(&path, home).args(HELP),
        crate::process::Limit::App,
    )
    .map_err(|error| failure::spawn(home, &shown, &error))?;
    if !output.status.success() {
        return Err(failure::command(home, &shown, &output, &[]));
    }
    Ok(Some(path))
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

/// Honeycomb removes only its owned package files; application credentials remain app-owned.
pub fn uninstall(id: &str) -> Result<Value> {
    let home = selected_home()?;
    uninstall_at(&home, id)
}

pub(crate) fn uninstall_at(home: &Path, id: &str) -> Result<Value> {
    uninstall_using(home, id, &honeycomb(home)?)
}

fn uninstall_using(home: &Path, id: &str, binary: &Path) -> Result<Value> {
    validate(id)?;
    let records = run(home, binary, &["installed"])?;
    let commands = commands(home, &records, id)?;
    let result = run(home, binary, &["uninstall", id])?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn script(path: &Path, body: &str) -> Result<()> {
        fs::create_dir_all(path.parent().unwrap())?;
        fs::write(path, body)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
        Ok(())
    }

    #[test]
    fn app_dna_uses_honeycomb_metadata_and_keeps_cached_details_offline() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let home = dir.path().canonicalize()?;
        let cli = home.join("honeycomb");
        script(
            &cli,
            r#"#!/bin/sh
set -eu
[ "$PWD" = "$SILICON_HOME" ]
case "$*" in
'apps get dm --json') echo '{"app_id":"dm","name":"Silicon DM","description":"Direct messages.","revision":3}' ;;
'apps get ting --json') echo '{"app_id":"ting","name":"Silicon Ting","description":"Events."}' ;;
*) exit 2 ;;
esac
"#,
        )?;
        let packages = prepare_honeycomb(&home)?;
        let generation = uuid::Uuid::new_v4();
        let mut requested = Vec::new();
        refresh_dna_using(
            &home,
            &[
                "dm".into(),
                "ting".into(),
                "dm".into(),
                "./legacy login".into(),
            ],
            generation,
            |id| {
                requested.push(id.to_owned());
                invoke_limited(
                    &home,
                    &packages,
                    &cli,
                    &["apps", "get", id],
                    crate::process::Limit::App,
                )
            },
        )?;
        assert_eq!(requested, ["dm", "ting"]);
        let dna = fs::read_to_string(home.join(".silicon/apps.md"))?;
        assert!(dna.contains("App Name: Silicon DM\nApp Id: dm\nCLI: run `dm --help` to know about it\n\nAbout: Direct messages."));
        assert_eq!(dna.matches("App Id: dm").count(), 1);

        refresh_dna_using(&home, &["dm".into(), "newapp".into()], generation, |id| {
            if id == "dm" {
                bail!("network unavailable");
            }
            Ok(json!({"app_id":"wrong-app","name":"Forged name","description":"Wrong details"}))
        })?;
        let dna = fs::read_to_string(home.join(".silicon/apps.md"))?;
        assert!(dna.contains("App Name: Silicon DM"));
        assert!(dna.contains("App Id: newapp\nCLI: run `newapp --help`"));
        assert!(dna.contains("Honeycomb metadata is unavailable"));
        assert!(!dna.contains("ting") && !dna.contains("Forged name"));
        let cached: Value =
            serde_json::from_slice(&fs::read(home.join(".silicon/app-descriptions.json"))?)?;
        assert_eq!(cached.as_object().unwrap().len(), 1);
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(log.contains("network unavailable"));
        assert!(log.contains("metadata identity or name does not match newapp"));
        Ok(())
    }

    #[test]
    fn honeycomb_lifecycle_validates_identity_paths_and_owned_aliases() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let canonical = dir.path().canonicalize()?;
        let home = canonical.as_path();
        // Registry migration changes identity, never the physical installed package path.
        let app = home.join(".silicon/packages/.honeycomb/dir/apps/test>app/1.0.0/app with spaces");
        fs::create_dir_all(app.parent().unwrap())?;
        fs::write(&app, "#!/bin/sh\nprintf '%s\\n' '{\"app_id\":\"app\"}'\n")?;
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700))?;
        let records = json!({"app":{"app_id":"app","commands":{"renamed":app}}});
        fs::write(home.join("records.json"), records.to_string())?;
        let cli = home.join("honeycomb");
        fs::write(
            &cli,
            r#"#!/bin/sh
set -eu
[ "$PWD" = "$SILICON_HOME" ]
case "$1" in
install) [ "$*" = 'install app --json' ]; echo install >> calls; touch installed; mkdir -p .honeycomb; echo '{"status":"installed"}' ;;
installed) if [ -f installed ]; then cat ../../records.json; else echo '{}'; fi ;;
uninstall) rm installed; echo '{"uninstalled":"app"}' ;;
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
        // Configuration the interpreter cannot read stays untouched for Honeycomb to
        // report, and the log says why the repair was skipped.
        fs::write(&config, "not json")?;
        prepare_honeycomb(home)?;
        assert_eq!(fs::read_to_string(&config)?, "not json");
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(
            log.contains("left Honeycomb settings unrepaired")
                && log.contains("config.json is not valid JSON: expected"),
            "{log}"
        );
        fs::write(&config, "[\"auto_update\"]")?;
        prepare_honeycomb(home)?;
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(
            log.contains("config.json is not a JSON object: [\"auto_update\"]"),
            "{log}"
        );
        fs::remove_file(&config)?;
        for id in ["a", "space-station", "test_app0", &"a".repeat(80)] {
            assert!(valid_id(id));
        }
        for id in [
            "",
            "tos>app",
            "../app",
            "App",
            "app;touch x",
            "app>test@2.1.0",
            "app>test",
            "0app",
            "-app",
            "_app",
            "si:app",
            &"a".repeat(81),
        ] {
            assert!(!valid_id(id));
        }
        assert!(install_using(home, "test>app", &cli).is_err());
        install_using(home, "app", &cli)?;
        assert!(!home.join(".honeycomb").exists());
        assert!(!home
            .join(".silicon/packages/.honeycomb/dir/config.json")
            .exists());
        assert_eq!(fs::read_link(home.join(".silicon/bin/renamed"))?, app);
        advertises(home, &app, "app")?;
        let other = format!("{:#}", advertises(home, &app, "other").unwrap_err());
        assert!(
            other.contains(
                "`'app with spaces' iam --json` advertises app_id \"app\", not \"other\""
            ) && other.contains("stdout:\n{\"app_id\":\"app\"}"),
            "{other}"
        );
        assert!(advertises(home, &app, "test>app").is_err());
        std::os::unix::fs::symlink(&app, home.join(".silicon/bin/app"))?;
        assert_eq!(resolve(home, "app")?, Ok(home.join(".silicon/bin/app")));
        assert!(resolve(home, "test>app").is_err());
        fs::remove_file(home.join(".silicon/bin/app"))?;
        install_using(home, "app", &cli)?;
        assert_eq!(
            fs::read_to_string(home.join(".silicon/packages/calls"))?,
            "install\ninstall\n"
        );
        uninstall_using(home, "app", &cli)?;
        assert!(!home.join(".silicon/bin/renamed").exists());
        fs::write(home.join(".silicon/bin/renamed"), "unmanaged")?;
        let collision = format!("{:#}", install_using(home, "app", &cli).unwrap_err());
        assert!(
            collision.contains("an unmanaged file is there")
                && collision.contains(&app.display().to_string()),
            "{collision}"
        );
        uninstall_using(home, "app", &cli)?;
        assert_eq!(
            fs::read_to_string(home.join(".silicon/bin/renamed"))?,
            "unmanaged"
        );
        fs::remove_file(home.join(".silicon/bin/renamed"))?;
        install_using(home, "app", &cli)?;
        fs::remove_file(&app)?;
        uninstall_using(home, "app", &cli)?;
        assert!(fs::symlink_metadata(home.join(".silicon/bin/renamed")).is_err());
        for records in [
            json!([]),
            json!({"app":{"app_id":"other","commands":{"cli":app}}}),
            json!({"app":{"app_id":"test>app","commands":{"cli":app}}}),
            json!({"app":{"app_id":"app","commands":{"../cli":app}}}),
            json!({"app":{"app_id":"app","commands":{"cli":"relative"}}}),
        ] {
            assert!(commands(home, &records, "app").is_err());
        }
        // The offending record is part of the error.
        let unsafe_name = format!(
            "{:#}",
            commands(
                home,
                &json!({"app":{"app_id":"app","commands":{"../cli":"/x"}}}),
                "app"
            )
            .unwrap_err()
        );
        assert!(
            unsafe_name.contains(r#""../cli""#) && unsafe_name.contains(r#"{"app_id":"app""#),
            "{unsafe_name}"
        );
        Ok(())
    }

    #[test]
    fn failing_honeycomb_and_apps_surface_status_stderr_and_stdout() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let canonical = dir.path().canonicalize()?;
        let home = canonical.as_path();
        let cli = home.join("tools/honeycomb");
        script(
            &cli,
            "#!/bin/sh\necho 'honeycomb: registry lock held by pid 42 for stk-0123456789abcdef' >&2\necho '{\"error\":{\"code\":\"locked\",\"access_token\":\"hunter2-private-value\"}}'\nexit 7\n",
        )?;
        let error = format!("{:#}", install_using(home, "app", &cli).unwrap_err());
        for expected in [
            &format!(
                "running Honeycomb {} with SILICON_HOME={}: ",
                cli.display(),
                home.join(".silicon/packages").display()
            ),
            "`honeycomb install app --json` failed: exit status: 7",
            "stderr:\nhoneycomb: registry lock held by pid 42 for [redacted]",
            "stdout:\n{\"error\":{\"code\":\"locked\",\"access_token\":\"[redacted]\"}}",
        ] {
            assert!(error.contains(expected), "missing {expected:?} in {error}");
        }
        // Only the credential values themselves are masked.
        assert!(
            !error.contains("stk-0123456789abcdef") && !error.contains("hunter2-private-value"),
            "{error}"
        );
        // A zero exit with an unreadable answer still shows the parser's words and the output.
        script(
            &cli,
            "#!/bin/sh\necho 'Updating registry...'\necho 'warning: cache' >&2\n",
        )?;
        let error = format!("{:#}", install_using(home, "app", &cli).unwrap_err());
        assert!(
            error.contains("`honeycomb install app --json` returned invalid JSON: expected")
                && error.contains("stdout:\nUpdating registry...")
                && error.contains("stderr:\nwarning: cache"),
            "{error}"
        );
        // A crashing app is not "missing": resolution logs what it said.
        let crashing = "#!/bin/sh\necho 'dyld: Library not loaded: libfoo.dylib' >&2\necho 'partial' \nexit 127\n";
        script(&home.join(".silicon/bin/crashy-test-app"), crashing)?;
        let reasons = resolve(home, "crashy-test-app")?.unwrap_err().join("\n");
        assert!(
            reasons.starts_with(&format!(
                "{} is not IAM app crashy-test-app: `crashy-test-app iam --json` failed: exit status: 127\nstderr:\ndyld: Library not loaded: libfoo.dylib\nstdout:\npartial",
                home.join(".silicon/bin/crashy-test-app").display()
            )),
            "{reasons}"
        );
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(
            log.contains("[error] [crashy-test-app/")
                && log.contains("`crashy-test-app iam --json` failed: exit status: 127")
                && log.contains("stderr:\\ndyld: Library not loaded: libfoo.dylib")
                && log.contains("stdout:\\npartial"),
            "{log}"
        );
        // An alias that is present but cannot run is a reason too, naming where it points.
        let dangling = home.join(".silicon/bin/dangling-test-app");
        std::os::unix::fs::symlink(home.join("removed/package"), &dangling)?;
        assert!(resolve(home, "dangling-test-app")?.is_err_and(|reasons| reasons.len() == 1));
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(
            log.contains(&format!(
                "passed over while resolving IAM app dangling-test-app: {} (it links to {}) cannot run: cannot inspect",
                dangling.display(),
                home.join("removed/package").display()
            )) && log.contains("No such file or directory"),
            "{log}"
        );
        // When Honeycomb registered the app, the same reasons are the error itself.
        let id = "registered-crash-test";
        let app = home.join(".silicon/packages/.honeycomb/dir/apps/crash/1.0.0/crash");
        script(&app, crashing)?;
        fs::write(
            home.join("records.json"),
            json!({id:{"app_id":id,"commands":{"crash":app}}}).to_string(),
        )?;
        script(&cli, "#!/bin/sh\ncat ../../records.json\n")?;
        let error = format!(
            "{:#}",
            resolve_using(home, id, |_| Ok(cli.clone())).unwrap_err()
        );
        assert!(
            error.contains(&format!("no installed command advertises IAM app {id}"))
                && error.contains(&format!("{} is not IAM app {id}", app.display()))
                && error.contains("`crash iam --json` failed: exit status: 127")
                && error.contains("stderr:\ndyld: Library not loaded: libfoo.dylib")
                && error.contains("stdout:\npartial"),
            "{error}"
        );
        // When the registry cannot be read, the candidates passed over are still logged.
        script(&home.join(".silicon/bin/unreached-test-app"), crashing)?;
        let error = format!(
            "{:#}",
            resolve_using(home, "unreached-test-app", |_| Err(anyhow!(
                "no Honeycomb here"
            )))
            .unwrap_err()
        );
        // The failure that ended the search leads; the candidates passed over ride along.
        assert!(
            error.starts_with(&format!(
                "resolving IAM app unreached-test-app, after passing over:\n{} is not IAM app unreached-test-app: `unreached-test-app iam --json` failed: exit status: 127",
                home.join(".silicon/bin/unreached-test-app").display()
            )) && error.ends_with(": no Honeycomb here"),
            "{error}"
        );
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        assert!(
            log.contains("passed over while resolving IAM app unreached-test-app: ")
                && log.contains("`unreached-test-app iam --json` failed: exit status: 127"),
            "{log}"
        );
        // A Honeycomb that cannot start gives the operating system's reason.
        let missing = format!(
            "{:#}",
            invoke(home, home, &home.join("tools/absent"), &["installed"]).unwrap_err()
        );
        assert!(
            missing.contains("could not run `absent installed --json`: No such file or directory"),
            "{missing}"
        );
        Ok(())
    }

    #[test]
    fn a_failed_install_keeps_an_installed_copy_and_fails_only_a_missing_app() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let canonical = dir.path().canonicalize()?;
        let home = canonical.as_path();
        // Honeycomb cannot reach its backend; nothing it is asked to install arrives.
        let cli = home.join("tools/honeycomb");
        script(
            &cli,
            "#!/bin/sh\necho \"$*\" >> \"$SILICON_HOME/../../honeycomb-calls\"\necho 'error sending request for url (https://10.255.255.1/api/v2/apps)' >&2\nexit 1\n",
        )?;
        let honeycomb = |_: &Path| Ok(cli.clone());
        script(
            &home.join(".silicon/bin/iam"),
            "#!/bin/sh\n[ \"$*\" = 'silicon-login --help' ] && echo '--approve-scopes'\n",
        )?;
        script(
            &home.join(".silicon/bin/kept-test-app"),
            "#!/bin/sh\necho '{\"app_id\":\"kept-test-app\"}'\n",
        )?;
        let generation = uuid::Uuid::new_v4();
        install_all_using(home, &["kept-test-app".into()], generation, &honeycomb)?;
        assert_eq!(
            fs::read_to_string(home.join("honeycomb-calls"))?,
            "install iam --json\ninstall kept-test-app --json\n"
        );
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        for id in ["iam", "kept-test-app"] {
            let line = log
                .lines()
                .find(|line| line.starts_with(&format!("[error] [{id}/")))
                .unwrap_or_else(|| panic!("no error line for {id}:\n{log}"));
            assert!(
                line.contains(&format!(
                    "could not install the latest {id} through Honeycomb, so {} (which answers as {id}) stays in use: ",
                    home.join(".silicon/bin").join(id).display()
                )) && line.contains(&format!("`honeycomb install {id} --json` failed: exit status: 1"))
                    && line.contains("stderr:\\nerror sending request for url")
                    && line.ends_with(&format!("; continuing with the installed {id}]")),
                "{line}"
            );
        }
        // Progress still shows the failed install.
        assert!(log.contains("\"state\":\"failed\""), "{log}");

        // An app with no installed copy stops the connection with Honeycomb's own failure.
        let error = format!(
            "{:#}",
            install_all_using(home, &["absent-test-app".into()], generation, &honeycomb)
                .unwrap_err()
        );
        assert!(
            error.contains("`honeycomb install absent-test-app --json` failed: exit status: 1")
                && error.contains("error sending request for url")
                && !error.contains("also:"),
            "{error}"
        );
        // A copy that is there but cannot run does not count, and says why.
        script(
            &home.join(".silicon/bin/iam"),
            "#!/bin/sh\necho 'dyld: Library not loaded: libiam.dylib' >&2\nexit 134\n",
        )?;
        let error = format!(
            "{:#}",
            install_all_using(home, &["kept-test-app".into()], generation, &honeycomb).unwrap_err()
        );
        assert!(
            error.contains("`honeycomb install iam --json` failed: exit status: 1")
                && error.contains(
                    "\nalso: looking for an installed copy of iam: `iam silicon-login --help` failed: exit status: 134\nstderr:\ndyld: Library not loaded: libiam.dylib"
                ),
            "{error}"
        );
        Ok(())
    }

    #[test]
    fn a_registered_app_that_cannot_be_installed_does_not_stop_the_connection() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let canonical = dir.path().canonicalize()?;
        let home = canonical.as_path();
        // The package was unpublished, so Honeycomb cannot install it.
        let cli = home.join("tools/honeycomb");
        script(
            &cli,
            "#!/bin/sh\necho \"$*\" >> \"$SILICON_HOME/../../honeycomb-calls\"\necho \"app $2 not found\" >&2\nexit 1\n",
        )?;
        let honeycomb = |_: &Path| Ok(cli.clone());
        script(
            &home.join(".silicon/bin/kept-test-app"),
            "#!/bin/sh\necho '{\"app_id\":\"kept-test-app\"}'\n",
        )?;
        let registered = [
            "iam",
            "ting",
            "configured-test-app",
            "gone-test-app",
            "kept-test-app",
            "! legacy-command --flag",
            "test>app",
        ]
        .map(String::from);
        let configured = ["configured-test-app".to_owned(), "ting".to_owned()];
        install_registered_using(
            home,
            &registered,
            &configured,
            uuid::Uuid::new_v4(),
            &honeycomb,
        )?;
        // IAM and configured apps are `install_all`'s; commands never go through Honeycomb.
        assert_eq!(
            fs::read_to_string(home.join("honeycomb-calls"))?,
            "install gone-test-app --json\ninstall kept-test-app --json\n"
        );
        let log = fs::read_to_string(home.join(".silicon/silicon.log"))?;
        let line = |id: &str| {
            log.lines()
                .find(|line| line.starts_with(&format!("[error] [{id}/")))
                .unwrap_or_else(|| panic!("no error line for {id}:\n{log}"))
                .to_owned()
        };
        let gone = line("gone-test-app");
        assert!(
            gone.contains("`honeycomb install gone-test-app --json` failed: exit status: 1")
                && gone.contains("stderr:\\napp gone-test-app not found")
                && gone.ends_with("; continuing without it, since it is in the managed app registry but not in the YAML; remove it with `si auth remove gone-test-app`]"),
            "{gone}"
        );
        assert!(
            line("kept-test-app").ends_with("; continuing with the installed kept-test-app]"),
            "{log}"
        );
        Ok(())
    }
}
