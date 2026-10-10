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
}

impl Default for Settings {
    fn default() -> Self {
        Self { telemetry: true }
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
                settings.remove("auto_update");
            }
            let settings = serde_json::from_value(value.clone()).with_context(invalid)?;
            let mut all = match value {
                Value::Object(all) => all,
                _ => Map::new(),
            };
            all.remove("telemetry");
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
/// reading another (a different HOME) never makes the next read of this one miss.
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
        _ => bail!("unknown setting {key:?}; expected telemetry"),
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
