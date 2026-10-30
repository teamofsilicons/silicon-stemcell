//! Silicon Apps owns installation and updates. The interpreter only notices a replaced
//! executable so it can restart after its current work is done.
use crate::runtime::Runtime;
use anyhow::{anyhow, bail, Context, Result};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::Duration;

pub(crate) fn version(tag: &str) -> Option<[u64; 3]> {
    let parts: Vec<_> = tag.strip_prefix('v').unwrap_or(tag).split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    Some([
        parts[0].parse().ok()?,
        parts[1].parse().ok()?,
        parts[2].parse().ok()?,
    ])
}

pub fn managed_prefix() -> Result<PathBuf> {
    let executable = std::env::current_exe()?.canonicalize()?;
    let root = executable
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| anyhow!("executable has no package directory"))?;
    if root.join("apps.yaml").is_file() {
        let manifest: serde_yaml::Value =
            serde_yaml::from_slice(&fs::read(root.join("apps.yaml"))?)?;
        if manifest["app_id"].as_str() == Some("silicon") {
            return Ok(root.to_owned());
        }
    }
    let prefix =
        fs::read_to_string(root.join("PREFIX")).context("not a managed Silicon installation")?;
    let prefix = PathBuf::from(prefix.trim()).canonicalize()?;
    if !executable.starts_with(prefix.join("lib/silicon/releases")) {
        bail!("Silicon prefix does not own the running executable");
    }
    Ok(prefix)
}

fn fingerprint(path: &Path) -> std::io::Result<(u64, u64, u64, i64, i64)> {
    let metadata = fs::metadata(path)?;
    // Package archives normalize mtimes; replacement still changes the inode.
    Ok((
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.mtime_nsec(),
    ))
}

/// The standalone installer's command links besides `silicon` and `si`.
const BUNDLE_TOOLS: &[&str] = &["omnid", "silicon-omni", "omni", "so", "caddy"];

/// The Silicon Apps home a package is installed in: `<home>/.apps/installed/silicon`.
fn apps_home_of(package: &Path) -> Result<PathBuf> {
    Ok(package
        .parent()
        .filter(|path| path.file_name().is_some_and(|name| name == "installed"))
        .and_then(Path::parent)
        .filter(|path| path.file_name().is_some_and(|name| name == ".apps"))
        .and_then(Path::parent)
        .context("this package is not installed in a Silicon Apps home")?
        .to_owned())
}

fn replace_link(link: &Path, target: &Path) -> Result<()> {
    let name = link.file_name().context("link has no file name")?;
    let staged = link.with_file_name(format!(
        ".{}-{}",
        name.to_string_lossy(),
        uuid::Uuid::new_v4()
    ));
    std::os::unix::fs::symlink(target, &staged)?;
    if let Err(error) = fs::rename(&staged, link) {
        let _ = fs::remove_file(&staged);
        return Err(error).with_context(|| format!("cannot replace {}", link.display()));
    }
    Ok(())
}

/// Moving a bundle to the Apps package repoints only `silicon` and `si`. Its other links
/// still resolve through `lib/silicon/current`, which nothing updates again, so `omni` and
/// `omnid` on PATH would keep the old bundle's Omni. Point each installer-owned one at the
/// package's copy, whose path survives package updates. Returns the names it moved.
fn relink_bundle_tools(prefix: &Path, package_bin: &Path) -> Result<Vec<&'static str>> {
    let mut moved = Vec::new();
    for &name in BUNDLE_TOOLS {
        let link = prefix.join("bin").join(name);
        let owned = fs::read_link(&link)
            .is_ok_and(|target| target == Path::new(&format!("../lib/silicon/current/bin/{name}")));
        let target = package_bin.join(name);
        if owned && target.is_file() {
            replace_link(&link, &target)?;
            moved.push(name);
        }
    }
    Ok(moved)
}

/// Bundle prefixes already moved to the package in `home`: their `bin/silicon` links to
/// `<home>/.apps/bin/silicon`. The installer put `<prefix>/bin` on PATH; its default
/// prefix is checked as well.
fn moved_prefixes(home: &Path, path: impl IntoIterator<Item = PathBuf>) -> Vec<PathBuf> {
    let moved = home.join(".apps/bin/silicon");
    let mut found: Vec<PathBuf> = Vec::new();
    for bin in path
        .into_iter()
        .chain([home.join(".local/share/silicon/bin")])
    {
        if !fs::read_link(bin.join("silicon")).is_ok_and(|target| target == moved) {
            continue;
        }
        if let Some(prefix) = bin
            .parent()
            .filter(|root| root.join("lib/silicon").is_dir())
        {
            if !found.iter().any(|seen| seen == prefix) {
                found.push(prefix.to_owned());
            }
        }
    }
    found
}

/// A bundle moved to the package by an older release's `silicon update` still has its
/// other command links on that bundle. Run by the packaged interpreter and its updates;
/// returns one line per prefix it repaired.
pub fn repair_moved_bundles() -> Result<Vec<String>> {
    // A source build or a standalone bundle has no package to point at.
    let Ok(package) = managed_prefix() else {
        return Ok(Vec::new());
    };
    if !package.join("apps.yaml").is_file() {
        return Ok(Vec::new());
    }
    let home = apps_home_of(&package)?.canonicalize()?;
    let path =
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).collect::<Vec<_>>();
    let mut repaired = Vec::new();
    for prefix in moved_prefixes(&home, path) {
        let moved = relink_bundle_tools(&prefix, &package.join("bin"))?;
        if !moved.is_empty() {
            repaired.push(format!(
                "pointed {} in {} at the Silicon Apps package",
                moved.join(", "),
                prefix.join("bin").display()
            ));
        }
    }
    Ok(repaired)
}

pub fn install_latest() -> Result<Option<PathBuf>> {
    let prefix = managed_prefix()?;
    let packaged = prefix.join("apps.yaml").is_file();
    let home = if packaged {
        apps_home_of(&prefix)?
    } else {
        std::env::var_os("SILICON_HOME")
            .or_else(|| std::env::var_os("HOME"))
            .map(PathBuf::from)
            .ok_or_else(|| anyhow!("HOME is not set"))?
    }
    .canonicalize()?;
    let apps = crate::apps::silicon_apps(&home)?;
    let executable = prefix.join("bin/silicon");
    let before = fingerprint(&executable)?;
    if !packaged {
        // Only the standalone installer's own links can be moved to the Apps package.
        for name in ["silicon", "si"] {
            let link = prefix.join("bin").join(name);
            if fs::read_link(&link)? != Path::new(&format!("../lib/silicon/current/bin/{name}")) {
                bail!("{} is not an installer-owned command; install Silicon through Silicon Apps explicitly", link.display());
            }
        }
    }
    let action: &[&str] = if packaged {
        &["update", "silicon", "--json"]
    } else {
        &["install", "silicon", "--yes", "--json"]
    };
    let home_text = home.to_str().context("Silicon Apps home must be UTF-8")?;
    let args = [&["--home", home_text][..], action].concat();
    let shown = crate::failure::argv(&apps, &args);
    let output = crate::process::output(
        crate::command(&apps, &home).args(&args),
        crate::process::Limit::Install,
    )?;
    if !output.status.success() {
        let error = crate::failure::command(&home, &shown, &output, &[]);
        return if packaged {
            Err(error)
        } else {
            Err(error.context(format!(
                "could not move this standalone bundle to Silicon Apps. If this system has no published Apps package, download install.sh from https://github.com/teamofsilicons/silicon-stemcell/releases/latest and run it with SILICON_PREFIX={} to update the existing bundle",
                prefix.display()
            )))
        };
    }
    if !packaged {
        for name in ["silicon", "si"] {
            let target = home.join(".apps/bin").join(name);
            if !target.is_file() {
                bail!("Silicon Apps did not install {}", target.display());
            }
        }
        for name in ["silicon", "si"] {
            replace_link(
                &prefix.join("bin").join(name),
                &home.join(".apps/bin").join(name),
            )?;
        }
        // The move itself succeeded; a link left on the old bundle is reported, not fatal.
        let relinked = home
            .join(".apps/bin/silicon")
            .canonicalize()
            .context("locate the installed Silicon package")
            .and_then(|silicon| {
                let bin = silicon
                    .parent()
                    .context("package executable has no directory")?;
                relink_bundle_tools(&prefix, bin)
            });
        if let Err(error) = relinked {
            crate::stderr_line(&format!(
                "{}\nVia: omni, omnid and caddy in {} still run the previous bundle",
                crate::failure::report(&error),
                prefix.join("bin").display()
            ));
        }
    } else {
        match repair_moved_bundles() {
            Ok(repaired) => repaired.iter().for_each(|line| crate::stderr_line(line)),
            Err(error) => crate::stderr_line(&format!(
                "{}\nVia: repairing command links left on a previous bundle",
                crate::failure::report(&error)
            )),
        }
    }
    Ok((fingerprint(&executable)? != before).then_some(executable))
}

pub fn start(runtime: &Arc<Runtime>, ready: Arc<AtomicBool>) {
    let Ok(prefix) = managed_prefix() else {
        return;
    };
    let executable = prefix.join("bin/silicon");
    let Ok(initial) = fingerprint(&executable) else {
        return;
    };
    let runtime = Arc::downgrade(runtime);
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(15));
        let Some(runtime) = runtime.upgrade() else {
            return;
        };
        if runtime.stopping.load(Ordering::SeqCst) {
            return;
        }
        if fingerprint(&executable).is_ok_and(|value| value != initial) {
            ready.store(true, Ordering::SeqCst);
            return;
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn scratch() -> PathBuf {
        let root = std::env::temp_dir().join(format!("silicon-update-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        root.canonicalize().unwrap()
    }

    fn bundle(prefix: &Path) {
        fs::create_dir_all(prefix.join("bin")).unwrap();
        fs::create_dir_all(prefix.join("lib/silicon")).unwrap();
    }

    #[test]
    fn relink_moves_only_installer_owned_tools_the_package_ships() {
        let root = scratch();
        let prefix = root.join("prefix");
        let package = root.join("home/.apps/installed/silicon/bin");
        bundle(&prefix);
        fs::create_dir_all(&package).unwrap();
        for name in ["omni", "so", "caddy"] {
            fs::write(package.join(name), "").unwrap();
        }
        for name in ["omni", "omnid", "so"] {
            symlink(
                format!("../lib/silicon/current/bin/{name}"),
                prefix.join("bin").join(name),
            )
            .unwrap();
        }
        // The user's own caddy is not the installer's to move.
        symlink("/usr/local/bin/caddy", prefix.join("bin/caddy")).unwrap();

        let moved = relink_bundle_tools(&prefix, &package).unwrap();

        assert_eq!(moved, ["omni", "so"]);
        for name in ["omni", "so"] {
            assert_eq!(
                fs::read_link(prefix.join("bin").join(name)).unwrap(),
                package.join(name)
            );
        }
        assert_eq!(
            fs::read_link(prefix.join("bin/omnid")).unwrap(),
            Path::new("../lib/silicon/current/bin/omnid"),
            "a tool the package does not ship stays on the bundle"
        );
        assert_eq!(
            fs::read_link(prefix.join("bin/caddy")).unwrap(),
            Path::new("/usr/local/bin/caddy")
        );
        assert!(fs::read_dir(prefix.join("bin")).unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with('.')));
        assert!(relink_bundle_tools(&prefix, &package).unwrap().is_empty());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn moved_prefixes_are_found_on_path_and_at_the_default_prefix() {
        let root = scratch();
        let home = root.join("home");
        fs::create_dir_all(home.join(".apps/bin")).unwrap();
        fs::write(home.join(".apps/bin/silicon"), "").unwrap();
        let custom = root.join("custom");
        let default = home.join(".local/share/silicon");
        let unmoved = root.join("unmoved");
        let unmanaged = root.join("unmanaged");
        for prefix in [&custom, &default, &unmoved] {
            bundle(prefix);
        }
        fs::create_dir_all(unmanaged.join("bin")).unwrap();
        symlink(home.join(".apps/bin/silicon"), custom.join("bin/silicon")).unwrap();
        symlink(home.join(".apps/bin/silicon"), default.join("bin/silicon")).unwrap();
        symlink(
            "../lib/silicon/current/bin/silicon",
            unmoved.join("bin/silicon"),
        )
        .unwrap();
        // A link to the package alone does not make a directory an installer prefix.
        symlink(
            home.join(".apps/bin/silicon"),
            unmanaged.join("bin/silicon"),
        )
        .unwrap();

        let found = moved_prefixes(
            &home,
            [
                custom.join("bin"),
                unmoved.join("bin"),
                unmanaged.join("bin"),
                custom.join("bin"),
                root.join("missing/bin"),
            ],
        );

        assert_eq!(found, [custom, default]);
        fs::remove_dir_all(root).unwrap();
    }
}
