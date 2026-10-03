use crate::Recover;
use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::HashMap;
use std::os::unix::fs::MetadataExt;
use std::sync::{Mutex, OnceLock};
use std::time::SystemTime;
use std::{
    fs,
    path::{Path, PathBuf},
};

/// Unknown keys are ignored rather than refused: a settings.json written by a newer release
/// must never stop an older interpreter (and its updater) from reading the keys it knows.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub telemetry: bool,
    pub auto_update: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            telemetry: true,
            auto_update: true,
        }
    }
}

/// The settings a file holds, plus every key this release does not know, so `set` can
/// write them back untouched.
fn read_all(path: &Path) -> Result<(Settings, Map<String, Value>)> {
    match fs::read(path) {
        Ok(bytes) => {
            let invalid = || format!("invalid interpreter settings in {}", path.display());
            let mut value: Value = serde_json::from_slice(&bytes).with_context(invalid)?;
            // Older interpreter releases persisted the retired relay preference.
            if let Some(settings) = value.as_object_mut() {
                settings.remove("realtime");
            }
            let settings = serde_json::from_value(value.clone()).with_context(invalid)?;
            let mut all = match value {
                Value::Object(all) => all,
                _ => Map::new(),
            };
            for known in ["telemetry", "auto_update"] {
                all.remove(known);
            }
            Ok((settings, all))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Ok((Settings::default(), Map::new()))
        }
        Err(error) => {
            Err(error).with_context(|| format!("read interpreter settings {}", path.display()))
        }
    }
}

fn read(path: &Path) -> Result<Settings> {
    read_all(path).map(|(settings, _)| settings)
}

/// What identifies one version of the file: a write through `state::write_json` renames a
/// new inode into place, and any edit changes its size or modification time.
#[derive(Clone, Debug, PartialEq)]
struct Stamp {
    device: u64,
    inode: u64,
    length: u64,
    modified: Option<SystemTime>,
}

struct Cached {
    stamp: Option<Stamp>,
    settings: std::result::Result<Settings, String>,
}

/// Parsed settings by file. A daemon reads one file; each path keeps its own entry, so
/// reading another (a test's, a changed HOME) never makes the next read of this one miss.
type Cache = Mutex<HashMap<PathBuf, Cached>>;

/// More files than any process reads; past this the cache starts over.
const CACHED_FILES: usize = 64;

/// Every log line and tool start asks for the settings; the file is parsed again only when
/// it changed (a missing file means the defaults).
fn read_cached(path: &Path) -> Result<Settings> {
    static CACHE: OnceLock<Cache> = OnceLock::new();
    read_cached_in(CACHE.get_or_init(Default::default), path)
}

fn read_cached_in(cache: &Cache, path: &Path) -> Result<Settings> {
    let stamp = match fs::metadata(path) {
        Ok(metadata) => Some(Stamp {
            device: metadata.dev(),
            inode: metadata.ino(),
            length: metadata.len(),
            modified: metadata.modified().ok(),
        }),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        // Unusual failures are read, uncached, so their error is exactly what read says.
        Err(_) => return read(path),
    };
    if let Some(cached) = cache
        .lock()
        .recover()
        .get(path)
        .filter(|cached| cached.stamp == stamp)
    {
        return cached.settings.clone().map_err(|error| anyhow!(error));
    }
    let settings = read(path);
    let mut cache = cache.lock().recover();
    if cache.len() >= CACHED_FILES && !cache.contains_key(path) {
        cache.clear();
    }
    cache.insert(
        path.to_path_buf(),
        Cached {
            stamp,
            settings: settings
                .as_ref()
                .map(Clone::clone)
                .map_err(|error| format!("{error:#}")),
        },
    );
    settings
}

pub fn load() -> Result<Settings> {
    read_cached(&crate::server::directory().join("settings.json"))
}

pub fn set(key: &str, enabled: bool) -> Result<Settings> {
    write(
        &crate::server::directory().join("settings.json"),
        key,
        enabled,
    )
}

fn write(path: &Path, key: &str, enabled: bool) -> Result<Settings> {
    let (mut settings, mut all) = read_all(path)?;
    match key {
        "telemetry" => settings.telemetry = enabled,
        "auto_update" => settings.auto_update = enabled,
        _ => bail!("unknown setting {key:?}; expected telemetry or auto_update"),
    }
    if let Value::Object(known) =
        serde_json::to_value(&settings).context("serialize interpreter settings")?
    {
        all.extend(known);
    }
    // write_json names the file in its own error.
    crate::state::write_json(path, &all).context("write interpreter settings")?;
    Ok(settings)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn settings_default_and_partial_files_preserve_other_defaults() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("settings.json");
        assert!(read(&path).unwrap().telemetry);
        fs::write(&path, r#"{"telemetry":false}"#).unwrap();
        let settings = read(&path).unwrap();
        assert!(!settings.telemetry);
        assert!(settings.auto_update);
        fs::write(&path, r#"{"telemetry":false,"realtime":true}"#).unwrap();
        let upgraded = read(&path).unwrap();
        assert!(!upgraded.telemetry && upgraded.auto_update);
        assert!(serde_json::to_value(upgraded)
            .unwrap()
            .get("realtime")
            .is_none());
        // A key this release does not know (a newer release's, or a typo) is ignored.
        fs::write(&path, r#"{"telemtry":false,"auto_update":false}"#).unwrap();
        let unknown = read(&path).unwrap();
        assert!(unknown.telemetry && !unknown.auto_update);
        assert_eq!(
            serde_json::to_value(unknown).unwrap(),
            serde_json::json!({"telemetry": true, "auto_update": false})
        );
    }

    #[test]
    fn set_keeps_keys_a_newer_release_wrote() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("settings.json");
        fs::write(
            &path,
            r#"{"telemetry":true,"autostart":{"mode":"login"},"realtime":true}"#,
        )
        .unwrap();
        let settings = write(&path, "auto_update", false).unwrap();
        assert!(settings.telemetry && !settings.auto_update);
        let stored: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            stored,
            serde_json::json!({"telemetry": true, "auto_update": false, "autostart": {"mode": "login"}})
        );
        let settings = write(&path, "telemetry", false).unwrap();
        assert!(!settings.telemetry && !settings.auto_update);
        let stored: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(stored["autostart"]["mode"], "login");
        let error = format!("{:#}", write(&path, "autostart", true).unwrap_err());
        assert_eq!(
            error,
            "unknown setting \"autostart\"; expected telemetry or auto_update"
        );
        // A missing file starts from the defaults.
        let fresh = temp.path().join("fresh/settings.json");
        write(&fresh, "telemetry", false).unwrap();
        let stored: Value = serde_json::from_slice(&fs::read(&fresh).unwrap()).unwrap();
        assert_eq!(
            stored,
            serde_json::json!({"telemetry": false, "auto_update": true})
        );
    }

    #[test]
    fn settings_are_parsed_again_only_when_the_file_changes() {
        use std::os::unix::fs::PermissionsExt;
        // Its own cache: parallel tests load other settings files through the shared one.
        let cache = Cache::default();
        let read_cached = |path: &Path| read_cached_in(&cache, path);
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("settings.json");
        let other = temp.path().join("other.json");
        assert!(
            read_cached(&path).unwrap().telemetry,
            "missing means defaults"
        );
        write(&path, "telemetry", false).unwrap();
        write(&other, "auto_update", false).unwrap();
        assert!(!read_cached(&path).unwrap().telemetry);
        // Another file's settings are kept apart and do not push this one out.
        assert!(!read_cached(&other).unwrap().auto_update);
        // An unchanged file is not read again: even an unreadable one answers from the cache.
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
        assert!(!read_cached(&path).unwrap().telemetry);
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        // A write (a new inode) and an in-place edit of another length are both noticed.
        write(&path, "telemetry", true).unwrap();
        assert!(read_cached(&path).unwrap().telemetry);
        fs::write(&path, r#"{"telemetry":false}"#).unwrap();
        assert!(!read_cached(&path).unwrap().telemetry);
        fs::write(&path, "{\"telemetry\":false,\n\"auto_update\": yes}").unwrap();
        let error = format!("{:#}", read_cached(&path).unwrap_err());
        assert!(
            error.contains("expected value at line 2 column 16"),
            "{error}"
        );
        // The cached failure is the same text.
        assert_eq!(format!("{:#}", read_cached(&path).unwrap_err()), error);
        fs::remove_file(&path).unwrap();
        assert!(read_cached(&path).unwrap().telemetry);
        assert_eq!(cache.lock().recover().len(), 2);
        // However many files are read, the cache stays bounded.
        for index in 0..CACHED_FILES * 2 {
            read_cached(&temp.path().join(format!("absent-{index}.json"))).unwrap();
        }
        assert!(cache.lock().recover().len() <= CACHED_FILES);
    }

    #[test]
    fn settings_errors_name_the_file_and_keep_the_parser_message() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("settings.json");
        fs::write(&path, "{\"telemetry\":false,\n\"auto_update\": yes}").unwrap();
        let error = format!("{:#}", read(&path).unwrap_err());
        assert!(error.contains(&path.display().to_string()), "{error}");
        assert!(
            error.contains("expected value at line 2 column 16"),
            "{error}"
        );
        fs::write(&path, r#"{"telemetry":"no"}"#).unwrap();
        let error = format!("{:#}", read(&path).unwrap_err());
        assert!(error.contains(&path.display().to_string()), "{error}");
        assert!(
            error.contains("invalid type: string \"no\", expected a boolean"),
            "{error}"
        );
        let error = format!("{:#}", read(temp.path()).unwrap_err());
        assert!(
            error.starts_with(&format!(
                "read interpreter settings {}: ",
                temp.path().display()
            )),
            "{error}"
        );
    }
}
