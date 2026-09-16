use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::{fs, path::Path};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
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

fn read(path: &Path) -> Result<Settings> {
    match fs::read(path) {
        Ok(bytes) => {
            let mut value: serde_json::Value =
                serde_json::from_slice(&bytes).context("invalid interpreter settings.json")?;
            // Older interpreter releases persisted the retired relay preference.
            if let Some(settings) = value.as_object_mut() {
                settings.remove("realtime");
            }
            serde_json::from_value(value).context("invalid interpreter settings.json")
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Settings::default()),
        Err(error) => Err(error.into()),
    }
}

pub fn load() -> Result<Settings> {
    read(&crate::server::directory().join("settings.json"))
}

pub fn set(key: &str, enabled: bool) -> Result<Settings> {
    let path = crate::server::directory().join("settings.json");
    let mut settings = read(&path)?;
    match key {
        "telemetry" => settings.telemetry = enabled,
        "auto_update" => settings.auto_update = enabled,
        _ => bail!("unknown setting {key:?}; expected telemetry or auto_update"),
    }
    crate::state::write_json(&path, &settings)?;
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
        fs::write(&path, r#"{"telemtry":false}"#).unwrap();
        assert!(read(&path).is_err());
    }
}
