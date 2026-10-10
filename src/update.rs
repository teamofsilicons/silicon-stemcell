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

pub fn install_latest() -> Result<Option<PathBuf>> {
    let prefix = managed_prefix()?;
    let packaged = prefix.join("apps.yaml").is_file();
    let home = if packaged {
        prefix
            .parent()
            .filter(|path| path.file_name().is_some_and(|name| name == "installed"))
            .and_then(Path::parent)
            .filter(|path| path.file_name().is_some_and(|name| name == ".apps"))
            .and_then(Path::parent)
            .context("this package is not installed in a Silicon Apps home")?
            .to_owned()
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
            let link = prefix.join("bin").join(name);
            let staged = prefix
                .join("bin")
                .join(format!(".{name}-{}", uuid::Uuid::new_v4()));
            std::os::unix::fs::symlink(home.join(".apps/bin").join(name), &staged)?;
            if let Err(error) = fs::rename(&staged, &link) {
                let _ = fs::remove_file(&staged);
                return Err(error).with_context(|| format!("cannot replace {}", link.display()));
            }
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
