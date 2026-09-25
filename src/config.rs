//! YAML plus the small shell/ordered-flow notation used by silicon.yaml.
use anyhow::{anyhow, bail, Context, Result};
use chrono_tz::Tz;
use serde::de::{DeserializeOwned, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Value as Json};
use serde_yaml::{Mapping, Number, Value as Yaml};
use std::{
    collections::{BTreeMap, HashSet},
    fmt, fs,
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Silicon {
    pub id: Option<String>,
    pub org_id: Option<String>,
    pub token: Option<String>,
    pub timezone: Option<String>,
    #[serde(rename = "SILICON_HOME")]
    pub silicon_home: Option<String>,
    #[serde(rename = "SILICON_ORG")]
    pub silicon_org: Option<String>,
    #[serde(default)]
    pub inference_providers: Yaml,
    #[serde(default)]
    pub setup: Vec<String>,
    #[serde(default)]
    pub apps: Vec<String>,
    #[serde(default)]
    pub app_configs: BTreeMap<String, Json>,
    pub space_station: Option<SpaceStation>,
    #[serde(default)]
    pub login: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SpaceStation {
    pub table_name: String,
    pub table_key: String,
}

impl Silicon {
    pub fn managed_apps(&self) -> Vec<String> {
        let mut seen = HashSet::new();
        self.apps
            .iter()
            .chain(&self.login)
            .cloned()
            .chain(std::iter::once("ting".to_owned()))
            .filter(|app| seen.insert(app.clone()))
            .collect()
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Isi {
    pub model: Option<String>,
    pub primary_send_mode: Option<String>,
    pub session_type: Option<String>,
    pub dna: Option<Yaml>,
    pub heartbeat: Option<Yaml>,
    pub new_session_suggestion: Option<Yaml>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub silicon: Silicon,
    pub isi: BTreeMap<String, Isi>,
    pub access: BTreeMap<String, Vec<String>>,
    pub flow: Yaml,
    #[serde(skip)]
    pub path: PathBuf,
    #[serde(skip)]
    pub home: PathBuf,
    #[serde(skip)]
    pub warnings: Vec<String>,
    #[serde(skip)]
    pub generation: uuid::Uuid,
}

impl Config {
    /// Flow stays editable while a Silicon is connected; compile-time expressions are untouched.
    pub fn load_flow(&self) -> Result<Yaml> {
        let source = fs::read_to_string(&self.path)
            .with_context(|| format!("read {}", self.path.display()))?;
        let document = parse_document(&source, &mut Vec::new())
            .with_context(|| format!("invalid silicon YAML in {}", self.path.display()))?;
        let flow = document["flow"].clone();
        crate::eval::validate(&flow).context("invalid flow expression syntax")?;
        validate_static_sends(&flow, &self.isi)?;
        Ok(flow)
    }

    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path
            .as_ref()
            .canonicalize()
            .with_context(|| format!("config not found: {}", path.as_ref().display()))?;
        let mut warnings = Vec::new();
        let source =
            fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
        let mut document = parse_document(&source, &mut warnings)
            .with_context(|| format!("invalid silicon YAML in {}", path.display()))?;
        validate_expressions(&document).context("invalid silicon expression syntax")?;
        let base = path
            .parent()
            .ok_or_else(|| anyhow!("config {} has no parent directory", path.display()))?;
        #[cfg(target_os = "linux")]
        if std::env::var("SILICON_WSL").as_deref() == Ok("1") {
            crate::state::validate_wsl_filesystem(base)?;
        }
        let mut env = json!({"request": {}, "var": {}, "silicon": document["silicon"],
            "isi": document["isi"], "access": document["access"]});
        let home_source = document["silicon"]["SILICON_HOME"]
            .as_str()
            .ok_or_else(|| {
                anyhow!(
                    "silicon.SILICON_HOME is required and must be a string, found {}",
                    shown(&document["silicon"]["SILICON_HOME"])
                )
            })?;
        let home = crate::eval::evaluate(home_source, &env, base, "interpreter")
            .context("evaluate silicon.SILICON_HOME")?;
        if home.trim().is_empty() {
            bail!("silicon.SILICON_HOME evaluated to an empty path");
        }
        let requested = base.join(home);
        let home = requested
            .canonicalize()
            .with_context(|| format!("silicon.SILICON_HOME must exist: {}", requested.display()))?;
        if !home.is_dir() {
            bail!(
                "silicon.SILICON_HOME must be a directory: {}",
                home.display()
            );
        }
        #[cfg(target_os = "linux")]
        if std::env::var("SILICON_WSL").as_deref() == Ok("1") {
            crate::state::validate_wsl_filesystem(&home)?;
        }
        document["silicon"]["SILICON_HOME"] = Yaml::String(home.to_string_lossy().into_owned());
        env["silicon"]["SILICON_HOME"] = json!(home);
        for key in ["id", "org_id", "token", "timezone", "SILICON_ORG"] {
            evaluate_field(
                &mut document["silicon"],
                "silicon",
                key,
                &env,
                &home,
                "interpreter",
            )?;
            env["silicon"][key] = to_json(&document["silicon"][key], &format!("silicon.{key}"))?;
        }
        if document["silicon"]["SILICON_ORG"].is_null() {
            document["silicon"]["SILICON_ORG"] = document["silicon"]["org_id"].clone();
            env["silicon"]["SILICON_ORG"] =
                to_json(&document["silicon"]["SILICON_ORG"], "silicon.SILICON_ORG")?;
        }
        evaluate_provider_values(
            &mut document["silicon"]["inference_providers"],
            "silicon.inference_providers",
            &env,
            &home,
        )?;
        env["silicon"]["inference_providers"] = to_json(
            &document["silicon"]["inference_providers"],
            "silicon.inference_providers",
        )?;
        if document["silicon"]["space_station"].is_mapping() {
            for key in ["table_name", "table_key"] {
                evaluate_field(
                    &mut document["silicon"]["space_station"],
                    "silicon.space_station",
                    key,
                    &env,
                    &home,
                    "interpreter",
                )?;
            }
            env["silicon"]["space_station"] = to_json(
                &document["silicon"]["space_station"],
                "silicon.space_station",
            )?;
        }
        for key in ["apps", "login"] {
            if let Some(commands) = document["silicon"][key].as_sequence_mut() {
                for (index, command) in commands.iter_mut().enumerate() {
                    *command = Yaml::String(
                        crate::eval::app_command(command.as_str().unwrap(), &env, &home)
                            .with_context(|| format!("evaluate silicon.{key}[{index}]"))?,
                    );
                }
                env["silicon"][key] = to_json(commands, &format!("silicon.{key}"))?;
            }
        }
        if let Some(configs) = document["silicon"]["app_configs"].as_mapping_mut() {
            for (app, config) in configs.iter_mut() {
                let path = format!("silicon.app_configs.{}", key_name(app));
                evaluate_app_config(config, &path, &env, &home)?;
            }
            env["silicon"]["app_configs"] = to_json(configs, "silicon.app_configs")?;
        }
        if let Some(isies) = document["isi"].as_mapping_mut() {
            for (name, isi) in isies {
                let name = name
                    .as_str()
                    .ok_or_else(|| anyhow!("isi names must be strings, found {}", shown(name)))?;
                for key in ["model", "primary_send_mode", "session_type"] {
                    evaluate_field(isi, &format!("isi.{name}"), key, &env, &home, name)?;
                }
                // Removed in favour of the canonical pair. Rejected here, before
                // deserialization, so the error names the replacement instead of
                // reporting an unknown field.
                for (removed, canonical, mapping) in [
                    (
                        "sticky",
                        "primary_send_mode",
                        "true is global, false is session",
                    ),
                    (
                        "archive_on_end",
                        "session_type",
                        "true is ephemeral, false is persistent",
                    ),
                ] {
                    if !isi[removed].is_null() {
                        bail!("isi.{name}.{removed} was removed; use {canonical} ({mapping})");
                    }
                }
            }
        }
        if let Some(access) = document["access"].as_mapping_mut() {
            for (name, targets) in access {
                let name = name.as_str().ok_or_else(|| {
                    anyhow!("access names must be strings, found {}", shown(name))
                })?;
                if let Some(targets) = targets.as_sequence_mut() {
                    for (index, target) in targets.iter_mut().enumerate() {
                        if let Some(source) = target.as_str() {
                            *target = Yaml::String(
                                crate::eval::evaluate(source, &env, &home, name)
                                    .with_context(|| format!("evaluate access.{name}[{index}]"))?,
                            );
                        }
                    }
                }
            }
        }
        let result = (|| {
            let mut config = Self::deserialize(&document).map_err(|error| {
                anyhow!(error).context(format!(
                    "invalid silicon schema in {}",
                    schema_section(&document)
                ))
            })?;
            config.path = path;
            config.home = home;
            config.warnings = warnings;
            config.validate()?;
            Ok(config)
        })();
        // Evaluated credentials can surface in schema or validation text: mask only those values.
        result.map_err(|error| masked(error, &crate::telemetry::silicon_secrets(&env["silicon"])))
    }

    fn validate(&mut self) -> Result<()> {
        let id = required(&self.silicon.id, "silicon.id")?;
        if !valid_silicon_id(id) {
            bail!("silicon.id must be si:<handle> (3–50 lowercase letters, digits, underscores or hyphens), found {id:?}; migrate old IDs using IAM's verified mapping and set silicon.org_id separately");
        }
        for (path, org) in [
            ("silicon.org_id", &self.silicon.org_id),
            ("silicon.SILICON_ORG", &self.silicon.silicon_org),
        ] {
            let org = required(org, path)?;
            if !valid_org(org) {
                bail!("{path} must be a canonical IAM organization handle (3–50 lowercase letters, digits, underscores or hyphens), found {org:?}");
            }
        }
        required(&self.silicon.token, "silicon.token")?;
        let timezone = required(&self.silicon.timezone, "silicon.timezone")?;
        timezone.parse::<Tz>().with_context(|| {
            format!("silicon.timezone must be an IANA timezone such as UTC or Asia/Kolkata, found {timezone:?}")
        })?;
        validate_providers(&self.silicon.inference_providers)?;
        let mut seen = HashSet::new();
        for app in &self.silicon.apps {
            if !crate::apps::valid_id(app) {
                bail!("silicon.apps entries must be bare IAM app IDs, e.g. dm, found {app:?}; migrate old org>app IDs using IAM's verified mapping");
            }
            if !seen.insert(app) {
                bail!("silicon.apps repeats {app}");
            }
        }
        for (app, config) in &self.silicon.app_configs {
            let managed = self.silicon.managed_apps();
            if !crate::apps::valid_id(app) || !managed.contains(app) {
                bail!(
                    "silicon.app_configs keys must be managed bare IAM app IDs, e.g. dm, found {app:?}; managed apps are {}",
                    managed.join(", ")
                );
            }
            if !config.is_object() {
                bail!("silicon.app_configs.{app} must be a key-value mapping");
            }
        }
        for (index, source) in self.silicon.setup.iter().enumerate() {
            if source.trim().trim_start_matches('!').trim().is_empty() || source.contains('\0') {
                bail!("silicon.setup[{index}] must be a nonempty shell command without NUL bytes, found {source:?}");
            }
        }
        if let Some(station) = &self.silicon.space_station {
            for (name, value) in [
                ("table_name", &station.table_name),
                ("table_key", &station.table_key),
            ] {
                required(
                    &Some(value.clone()),
                    &format!("silicon.space_station.{name}"),
                )?;
            }
        }
        for (index, command) in self.silicon.login.iter().enumerate() {
            if !command.trim().starts_with('!')
                && command.contains('>')
                && !command.contains('/')
                && !command.chars().any(char::is_whitespace)
            {
                bail!("silicon.login[{index}] is a legacy org>app ID {command:?}; migrate it using IAM's verified mapping");
            }
            let command = command.trim().strip_prefix('!').unwrap_or(command).trim();
            if command.is_empty() || command.contains(['\n', '\0']) {
                bail!("silicon.login[{index}] must be a nonempty single-line command, found {command:?}");
            }
        }
        if self.isi.is_empty() {
            bail!("isi must contain at least one internal silicon");
        }
        for (name, isi) in &mut self.isi {
            if !safe_name(name) {
                bail!("invalid isi name {name:?}; use letters, digits, '.', '_' or '-'");
            }
            required(&isi.model, &format!("isi.{name}.model"))?;
            let mode = required(
                &isi.primary_send_mode,
                &format!("isi.{name}.primary_send_mode"),
            )?;
            if !matches!(mode, "global" | "session") {
                bail!("isi.{name}.primary_send_mode must be global or session, found {mode:?}");
            }
            let kind = required(&isi.session_type, &format!("isi.{name}.session_type"))?;
            if !matches!(kind, "persistent" | "ephemeral") {
                bail!("isi.{name}.session_type must be persistent or ephemeral, found {kind:?}");
            }
            validate_isi_blocks(name, isi)?;
            let allowed = self
                .access
                .get(name)
                .ok_or_else(|| anyhow!("access must define {name}"))?;
            let mut seen = std::collections::HashSet::new();
            for target in allowed {
                if !seen.insert(target) {
                    bail!("access.{name} repeats {target}");
                }
            }
        }
        for (name, targets) in &self.access {
            if !self.isi.contains_key(name) {
                bail!("access defines unknown isi {name}");
            }
            for target in targets {
                if !self.isi.contains_key(target) {
                    bail!("access.{name} references unknown isi {target}");
                }
            }
        }
        validate_static_sends(&self.flow, &self.isi)?;
        Ok(())
    }
}

/// Rebuild the chain link by link so every cause survives with only credential values masked.
fn masked(error: anyhow::Error, secrets: &[String]) -> anyhow::Error {
    let mut causes: Vec<_> = error
        .chain()
        .map(|cause| crate::telemetry::redact_text(&cause.to_string(), secrets))
        .collect();
    let mut rebuilt = anyhow!(causes.pop().unwrap_or_default());
    while let Some(outer) = causes.pop() {
        rebuilt = rebuilt.context(outer);
    }
    rebuilt
}

fn validate_expressions(document: &Yaml) -> Result<()> {
    fields(document, "config", &["silicon", "isi", "access", "flow"])?;
    for (key, value) in document.as_mapping().unwrap() {
        let section = key_name(key);
        crate::eval::validate(key).with_context(|| format!("top-level key {section}"))?;
        if key.as_str() == Some("silicon") {
            for removed in ["webhooks", "webhook"] {
                if value
                    .as_mapping()
                    .is_some_and(|map| map.contains_key(removed))
                {
                    bail!("silicon.{removed} was removed; Ting is registered automatically for every Silicon");
                }
            }
            fields(
                value,
                "silicon",
                &[
                    "id",
                    "org_id",
                    "token",
                    "timezone",
                    "SILICON_HOME",
                    "SILICON_ORG",
                    "inference_providers",
                    "setup",
                    "apps",
                    "app_configs",
                    "space_station",
                    "login",
                ],
            )?;
            if !value["space_station"].is_null() {
                // A bare value here is most likely the table key itself: name its type only.
                if !value["space_station"].is_mapping() {
                    bail!(
                        "silicon.space_station must be a mapping, found {}; expected table_name and table_key",
                        kind(&value["space_station"])
                    );
                }
                fields(
                    &value["space_station"],
                    "silicon.space_station",
                    &["table_name", "table_key"],
                )?;
            }
            let mut silicon = value.clone();
            if let Some(token) = silicon.as_mapping_mut().and_then(|map| map.remove("token")) {
                crate::eval::validate_secret(&token).context("silicon.token")?;
            }
            if let Some(table_key) = silicon["space_station"]
                .as_mapping_mut()
                .and_then(|map| map.remove("table_key"))
            {
                crate::eval::validate_secret(&table_key)
                    .context("silicon.space_station.table_key")?;
            }
            if let Some(configs) = silicon
                .as_mapping_mut()
                .and_then(|map| map.remove("app_configs"))
            {
                let configs = configs.as_mapping().with_context(|| {
                    format!(
                        "silicon.app_configs must map IAM app IDs to key-value mappings, found {}",
                        kind(&configs)
                    )
                })?;
                for (app, config) in configs {
                    let Some(app) = app.as_str().filter(|app| crate::apps::valid_id(app)) else {
                        bail!(
                            "silicon.app_configs keys must be bare IAM app IDs, e.g. dm, found {}",
                            shown(app)
                        );
                    };
                    if !config.is_mapping() {
                        bail!(
                            "silicon.app_configs.{app} must be a key-value mapping, found {}",
                            kind(config)
                        );
                    }
                    crate::eval::validate_secret(config)
                        .with_context(|| format!("silicon.app_configs.{app}"))?;
                }
            }
            for key in ["setup", "apps", "login"] {
                if let Some(commands) = silicon
                    .as_mapping_mut()
                    .and_then(|map| map.remove(Yaml::String(key.into())))
                {
                    let commands = commands.as_sequence().ok_or_else(|| {
                        anyhow!("silicon.{key} must be a list, found {}", shown(&commands))
                    })?;
                    for (index, command) in commands.iter().enumerate() {
                        let command = command.as_str().ok_or_else(|| {
                            anyhow!(
                                "silicon.{key}[{index}] must be a string, found {}",
                                shown(command)
                            )
                        })?;
                        (if key == "setup" {
                            crate::eval::validate_template(command)
                        } else {
                            crate::eval::validate_app_command(command)
                        })
                        .with_context(|| format!("silicon.{key}[{index}]"))?;
                    }
                }
            }
            crate::eval::validate(&silicon).context("silicon")?;
        } else {
            crate::eval::validate(value).with_context(|| section.clone())?;
        }
    }
    Ok(())
}

fn evaluate_field(
    object: &mut Yaml,
    path: &str,
    key: &str,
    env: &Json,
    home: &Path,
    isi: &str,
) -> Result<()> {
    if let Some(source) = object[key].as_str() {
        object[key] = Yaml::String(
            (if matches!(key, "token" | "table_key") {
                crate::eval::evaluate_secret(source, env, home, isi)
            } else {
                crate::eval::evaluate(source, env, home, isi)
            })
            .with_context(|| format!("evaluate {path}.{key}"))?,
        );
    }
    Ok(())
}

fn evaluate_provider_values(value: &mut Yaml, path: &str, env: &Json, home: &Path) -> Result<()> {
    match value {
        Yaml::String(source) => {
            *source = crate::eval::evaluate(source, env, home, "interpreter")
                .with_context(|| format!("evaluate {path}"))?;
        }
        Yaml::Sequence(values) => {
            for (index, value) in values.iter_mut().enumerate() {
                evaluate_provider_values(value, &format!("{path}[{index}]"), env, home)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn evaluate_app_config(value: &mut Yaml, path: &str, env: &Json, home: &Path) -> Result<()> {
    match value {
        Yaml::String(source) => {
            *source = crate::eval::evaluate_secret(source, env, home, "interpreter")
                .with_context(|| format!("evaluate {path}"))?;
        }
        Yaml::Sequence(values) => {
            for (index, value) in values.iter_mut().enumerate() {
                evaluate_app_config(value, &format!("{path}[{index}]"), env, home)?;
            }
        }
        Yaml::Mapping(values) => {
            for (key, value) in values.iter_mut() {
                evaluate_app_config(value, &format!("{path}.{}", key_name(key)), env, home)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// Update only the Silicon section, preserving live flow and other source verbatim.
pub(crate) fn set_app(path: &Path, id: &str, installed: bool) -> Result<Config> {
    use std::{fs::OpenOptions, io::Write, os::unix::fs::OpenOptionsExt};
    if !crate::apps::valid_id(id) {
        bail!("expected a bare Honeycomb app ID (e.g. dm), got {id:?}");
    }
    if !installed && ["iam", "ting"].contains(&id) {
        bail!("{id} is required by the interpreter and cannot be uninstalled");
    }
    let path = path
        .canonicalize()
        .with_context(|| format!("config not found: {}", path.display()))?;
    let read = || fs::read_to_string(&path).with_context(|| format!("read {}", path.display()));
    let source = read()?;
    let current = Config::load(&path)
        .with_context(|| format!("the current {} does not compile", path.display()))?;
    if read()? != source {
        bail!(
            "{} changed while updating apps; retry the command",
            path.display()
        );
    }
    if installed && current.silicon.apps.iter().any(|app| app == id) {
        return Ok(current);
    }
    let mut document = parse_document(&source, &mut Vec::new())
        .with_context(|| format!("invalid silicon YAML in {}", path.display()))?;
    let found = kind(&document["silicon"]);
    let silicon = document["silicon"]
        .as_mapping_mut()
        .with_context(|| format!("silicon must be a mapping, found {found}"))?;
    let apps = silicon
        .entry(Yaml::String("apps".into()))
        .or_insert(Yaml::Sequence(Vec::new()));
    let found = shown(apps);
    let apps = apps
        .as_sequence_mut()
        .with_context(|| format!("silicon.apps must be a list, found {found}"))?;
    if installed {
        apps.push(Yaml::String(id.to_owned()));
    } else {
        let mut index = 0;
        apps.retain(|_| {
            let keep = current.silicon.apps[index] != id;
            index += 1;
            keep
        });
        if let Some(configs) = silicon
            .get_mut("app_configs")
            .and_then(Yaml::as_mapping_mut)
        {
            configs.remove(id);
        }
    }
    let mut start = None;
    let mut end = source.len();
    let mut offset = 0;
    for line in source.split_inclusive('\n') {
        if !line.starts_with(char::is_whitespace) && !line.starts_with('#') {
            if let Some(colon) = mapping_colon(line) {
                let key = serde_yaml::from_str::<String>(&line[..colon]).ok();
                if key.as_deref() == Some("silicon") {
                    start = Some(offset);
                } else if start.is_some() {
                    end = offset;
                    break;
                }
            }
        }
        offset += line.len();
    }
    // ponytail: preserve block-style YAML; add a source-span parser if compact root maps need editing.
    let start = start.with_context(|| {
        format!(
            "si app requires silicon as a top-level YAML block in {}",
            path.display()
        )
    })?;
    let replacement = serde_yaml::to_string(&BTreeMap::from([("silicon", &document["silicon"])]))
        .context("serialize the updated silicon block")?;
    let next = format!("{}{}{}", &source[..start], replacement, &source[end..]);
    let reparsed =
        parse_document(&next, &mut Vec::new()).context("re-read the rewritten silicon block")?;
    if reparsed != document {
        bail!(
            "cannot safely update the YAML layout of {}; use a top-level silicon block",
            path.display()
        );
    }
    let parent = path
        .parent()
        .with_context(|| format!("config {} has no parent directory", path.display()))?;
    let staged = parent.join(format!(".silicon-{}.yaml", uuid::Uuid::new_v4()));
    let result = (|| {
        let write = || format!("write staged config {}", staged.display());
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&staged)
            .with_context(write)?;
        file.write_all(next.as_bytes()).with_context(write)?;
        file.sync_all().with_context(write)?;
        let mut config = Config::load(&staged).with_context(|| {
            format!(
                "the updated {} would not compile (staged as {})",
                path.display(),
                staged.display()
            )
        })?;
        if read()? != source {
            bail!(
                "{} changed while updating apps; retry the command",
                path.display()
            );
        }
        fs::metadata(&path)
            .and_then(|metadata| fs::set_permissions(&staged, metadata.permissions()))
            .with_context(|| {
                format!(
                    "copy permissions of {} to {}",
                    path.display(),
                    staged.display()
                )
            })?;
        fs::rename(&staged, &path)
            .with_context(|| format!("replace {} with {}", path.display(), staged.display()))?;
        fs::File::open(parent)
            .and_then(|directory| directory.sync_all())
            .with_context(|| format!("sync directory {}", parent.display()))?;
        config.path = path.clone();
        Ok(config)
    })();
    result.map_err(|error| match fs::remove_file(&staged) {
        Err(cleanup) if cleanup.kind() != std::io::ErrorKind::NotFound => error.context(format!(
            "updating apps failed and left {} behind (remove: {cleanup})",
            staged.display()
        )),
        _ => error,
    })
}

fn required<'a>(value: &'a Option<String>, path: &str) -> Result<&'a str> {
    let value = value
        .as_deref()
        .filter(|v| !v.trim().is_empty())
        .ok_or_else(|| anyhow!("{path} is required"))?;
    if value.trim() == "..." {
        bail!(
            "{path} is a template placeholder; provide your own value in a separate silicon.yaml"
        );
    }
    if value.contains('\0') {
        bail!("{path} cannot contain NUL bytes");
    }
    Ok(value)
}

pub(crate) fn valid_silicon_id(id: &str) -> bool {
    id.strip_prefix("si:").is_some_and(|handle| {
        (3..=50).contains(&handle.len())
            && handle
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-".contains(&b))
    })
}

pub(crate) fn valid_org(value: &str) -> bool {
    (3..=50).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'-'))
}

pub(crate) fn host_label(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value.as_bytes()[value.len() - 1].is_ascii_alphanumeric()
}

pub fn safe_name(value: &str) -> bool {
    !value.is_empty()
        && value != "."
        && value != ".."
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

fn validate_providers(value: &Yaml) -> Result<()> {
    let values = value.as_sequence().ok_or_else(|| {
        anyhow!(
            "silicon.inference_providers must be a list (lists may be nested), found {}",
            shown(value)
        )
    })?;
    if values.is_empty() {
        bail!("silicon.inference_providers cannot be empty");
    }
    for value in values {
        match value {
            Yaml::Sequence(_) => validate_providers(value)?,
            Yaml::String(s) if !s.trim().is_empty() && s.trim() != "except" => {},
            _ => bail!("inference_providers entries must be provider names, 'except provider', or nested lists, found {}", shown(value)),
        }
    }
    Ok(())
}

/// A YAML value as compact JSON for an error message.
fn shown(value: &Yaml) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| format!("{value:?}"))
}

/// A value's type alone, for errors about fields whose values are credentials.
fn kind(value: &Yaml) -> &'static str {
    match value {
        Yaml::Null => "null",
        Yaml::Bool(_) => "a boolean",
        Yaml::Number(_) => "a number",
        Yaml::String(_) => "a string",
        Yaml::Sequence(_) => "a list",
        Yaml::Mapping(_) => "a mapping",
        Yaml::Tagged(_) => "a tagged value",
    }
}

/// Expressions read evaluated sections as JSON, which allows only string keys.
fn to_json(value: impl Serialize, path: &str) -> Result<Json> {
    serde_json::to_value(value)
        .with_context(|| format!("{path} cannot be converted to JSON for expressions"))
}

fn key_name(key: &Yaml) -> String {
    key.as_str().map_or_else(|| shown(key), str::to_owned)
}

// serde_yaml gives no location for in-memory values, so name the section it rejected,
// checking sections in document order as serde does, and the field within it.
fn schema_section(document: &Yaml) -> String {
    fn rejected<T: DeserializeOwned>(path: String, value: &Yaml) -> Option<String> {
        if T::deserialize(value).is_ok() {
            return None;
        }
        // Every Silicon and Isi field is optional, so one rejected alone is the culprit.
        let field = value
            .as_mapping()
            .into_iter()
            .flatten()
            .find_map(|(key, field)| {
                let alone = Yaml::Mapping(Mapping::from_iter([(key.clone(), field.clone())]));
                T::deserialize(&alone)
                    .is_err()
                    .then(|| format!("{path}.{}", key_name(key)))
            });
        Some(field.unwrap_or(path))
    }
    fn entries(
        section: &str,
        value: &Yaml,
        entry: fn(String, &Yaml) -> Option<String>,
    ) -> Option<String> {
        match value.as_mapping() {
            Some(map) => map
                .iter()
                .find_map(|(name, value)| entry(format!("{section}.{}", key_name(name)), value)),
            None => Some(section.into()),
        }
    }
    document
        .as_mapping()
        .into_iter()
        .flatten()
        .find_map(|(key, value)| match key.as_str() {
            Some("silicon") => rejected::<Silicon>("silicon".into(), value),
            Some("isi") => entries("isi", value, rejected::<Isi>),
            Some("access") => entries("access", value, rejected::<Vec<String>>),
            _ => None,
        })
        .unwrap_or_else(|| "config".into())
}

fn fields<'a>(value: &'a Yaml, path: &str, allowed: &[&str]) -> Result<&'a Mapping> {
    let map = value
        .as_mapping()
        .ok_or_else(|| anyhow!("{path} must be a mapping, found {}", shown(value)))?;
    for key in map.keys() {
        if !key.as_str().is_some_and(|k| allowed.contains(&k)) {
            bail!(
                "unknown field in {path}: {}; expected one of {}",
                shown(key),
                allowed.join(", ")
            );
        }
    }
    Ok(map)
}

fn nonempty_scalar(value: &Yaml, path: &str) -> Result<()> {
    if value.as_str().is_some_and(|s| !s.is_empty()) || value.is_number() || value.is_bool() {
        return Ok(());
    }
    bail!("{path} must be a nonempty scalar, found {}", shown(value))
}

fn interval(value: &Yaml, path: &str) -> Result<()> {
    nonempty_scalar(value, path)?;
    let source = value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| serde_yaml::to_string(value).unwrap().trim().to_owned());
    let source = source.trim();
    if source.starts_with('!') || source.contains('{') || source.contains("!>>") {
        return Ok(());
    }
    let (number, multiplier) = if let Some(n) = source
        .strip_suffix("min")
        .or_else(|| source.strip_suffix('m'))
    {
        (n, 60.)
    } else if let Some(n) = source.strip_suffix('s') {
        (n, 1.)
    } else if let Some(n) = source.strip_suffix('h') {
        (n, 3600.)
    } else {
        (source, 60.)
    };
    if number.trim().parse::<f64>().is_ok_and(|n| {
        let seconds = n * multiplier;
        seconds.is_finite() && seconds > 0.0 && seconds <= 315360000.
    }) {
        return Ok(());
    }
    bail!("{path} must be a positive interval of at most ten years, e.g. 30min, or an expression, found {source:?}")
}

fn nonempty_string(value: &Yaml, path: &str) -> Result<()> {
    if value.as_str().is_some_and(|s| !s.is_empty()) {
        return Ok(());
    }
    bail!("{path} must be a nonempty string, found {}", shown(value))
}

fn validate_isi_blocks(name: &str, isi: &Isi) -> Result<()> {
    let path = format!("isi.{name}.dna");
    let dna = isi
        .dna
        .as_ref()
        .ok_or_else(|| anyhow!("{path} is required"))?;
    fields(dna, &path, &["assemble", "next_refresh"])?;
    let assemble = dna["assemble"].as_sequence().ok_or_else(|| {
        anyhow!(
            "{path}.assemble must be a list, found {}",
            shown(&dna["assemble"])
        )
    })?;
    for (index, value) in assemble.iter().enumerate() {
        if !value.as_str().is_some_and(|source| !source.is_empty()) {
            bail!(
                "{path}.assemble[{index}] must be a nonempty path or shell expression, found {}",
                shown(value)
            );
        }
    }
    interval(&dna["next_refresh"], &format!("{path}.next_refresh"))?;
    if let Some(heartbeat) = &isi.heartbeat {
        let path = format!("isi.{name}.heartbeat");
        fields(heartbeat, &path, &["next", "message"])?;
        interval(&heartbeat["next"], &format!("{path}.next"))?;
        nonempty_string(&heartbeat["message"], &format!("{path}.message"))?;
    }
    if let Some(suggestion) = &isi.new_session_suggestion {
        let path = format!("isi.{name}.new_session_suggestion");
        fields(
            suggestion,
            &path,
            &["cooldown_minutes", "min_new_messages", "suggestion_message"],
        )?;
        interval(
            &suggestion["cooldown_minutes"],
            &format!("{path}.cooldown_minutes"),
        )?;
        let count = &suggestion["min_new_messages"];
        if !count.as_u64().is_some_and(|n| n > 0)
            && !count.as_str().is_some_and(|s| {
                s.parse::<u64>().is_ok_and(|n| n > 0)
                    || s.starts_with('!')
                    || s.contains('{')
                    || s.contains("!>>")
            })
        {
            bail!(
                "{path}.min_new_messages must be a positive integer or expression, found {}",
                shown(count)
            );
        }
        nonempty_string(
            &suggestion["suggestion_message"],
            &format!("{path}.suggestion_message"),
        )?;
    }
    Ok(())
}

fn validate_static_sends(value: &Yaml, isies: &BTreeMap<String, Isi>) -> Result<()> {
    match value {
        Yaml::Sequence(values) => {
            for value in values {
                validate_static_sends(value, isies)?;
            }
        }
        Yaml::Mapping(map) => {
            if let Some(send) = map.get(Yaml::String("send".into())) {
                if let Some(target) = send["isi"]
                    .as_str()
                    .filter(|s| !s.starts_with('!') && !s.contains('{') && !s.contains("!>>"))
                {
                    let isi = isies.get(target).ok_or_else(|| {
                        anyhow!(
                            "flow sends to unknown isi {target:?}; known ISIs are {}",
                            isies.keys().cloned().collect::<Vec<_>>().join(", ")
                        )
                    })?;
                    if isi.primary_send_mode.as_deref() == Some("session")
                        && send["session_id"].is_null()
                    {
                        bail!("flow send to {target} requires session_id");
                    }
                }
            }
            for (action, body) in map {
                if action.as_str() == Some("else") {
                    validate_static_sends(body, isies)?;
                } else {
                    for branch in ["then", "else", "catch"] {
                        if !body[branch].is_null() {
                            validate_static_sends(&body[branch], isies)?;
                        }
                    }
                }
            }
        }
        _ => {}
    }
    Ok(())
}

// Keep map entries until flow has been converted: a YAML Mapping would discard
// or reject the reference DSL's repeated `if` actions.
#[derive(Debug)]
enum Node {
    Scalar(Yaml),
    Seq(Vec<Node>),
    Map(Vec<(String, Node)>),
}

impl Node {
    fn describe(&self) -> String {
        match self {
            Node::Scalar(value) => shown(value),
            Node::Seq(_) => "a list".into(),
            Node::Map(_) => "a mapping".into(),
        }
    }
}

impl<'de> Deserialize<'de> for Node {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct NodeVisitor;
        impl<'de> Visitor<'de> for NodeVisitor {
            type Value = Node;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("a YAML value")
            }
            fn visit_unit<E: serde::de::Error>(self) -> std::result::Result<Node, E> {
                Ok(Node::Scalar(Yaml::Null))
            }
            fn visit_bool<E: serde::de::Error>(self, v: bool) -> std::result::Result<Node, E> {
                Ok(Node::Scalar(Yaml::Bool(v)))
            }
            fn visit_i64<E: serde::de::Error>(self, v: i64) -> std::result::Result<Node, E> {
                Ok(Node::Scalar(Yaml::Number(v.into())))
            }
            fn visit_u64<E: serde::de::Error>(self, v: u64) -> std::result::Result<Node, E> {
                Ok(Node::Scalar(Yaml::Number(v.into())))
            }
            fn visit_f64<E: serde::de::Error>(self, v: f64) -> std::result::Result<Node, E> {
                Ok(Node::Scalar(Yaml::Number(Number::from(v))))
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> std::result::Result<Node, E> {
                Ok(Node::Scalar(Yaml::String(v.into())))
            }
            fn visit_string<E: serde::de::Error>(self, v: String) -> std::result::Result<Node, E> {
                Ok(Node::Scalar(Yaml::String(v)))
            }
            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut seq: A,
            ) -> std::result::Result<Node, A::Error> {
                let mut values = Vec::new();
                while let Some(value) = seq.next_element()? {
                    values.push(value);
                }
                Ok(Node::Seq(values))
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Node, A::Error> {
                let mut entries = Vec::new();
                while let Some(entry) = map.next_entry()? {
                    entries.push(entry);
                }
                Ok(Node::Map(entries))
            }
        }
        deserializer.deserialize_any(NodeVisitor)
    }
}

fn parse_document(source: &str, warnings: &mut Vec<String>) -> Result<Yaml> {
    let node: Node = serde_yaml::from_str(&prepare_scalars(source)?)?;
    let Node::Map(entries) = node else {
        bail!("config must be a YAML mapping, found {}", node.describe());
    };
    let mut result = Mapping::new();
    for (key, node) in entries {
        if result.contains_key(Yaml::String(key.clone())) {
            bail!("duplicate top-level key {key}");
        }
        let value = if key == "flow" {
            steps(node, "flow", warnings)?
        } else {
            ordinary(node, &key)?
        };
        result.insert(Yaml::String(key), value);
    }
    for key in ["silicon", "isi", "access", "flow"] {
        if !result.contains_key(Yaml::String(key.into())) {
            bail!("missing required top-level key {key}");
        }
    }
    Ok(Yaml::Mapping(result))
}

fn ordinary(node: Node, path: &str) -> Result<Yaml> {
    Ok(match node {
        Node::Scalar(value) => value,
        Node::Seq(values) => Yaml::Sequence(
            values
                .into_iter()
                .map(|value| ordinary(value, path))
                .collect::<Result<_>>()?,
        ),
        Node::Map(entries) => {
            let mut map = Mapping::new();
            for (key, node) in entries {
                if map.contains_key(Yaml::String(key.clone())) {
                    bail!("duplicate key {path}.{key}");
                }
                map.insert(
                    Yaml::String(key.clone()),
                    ordinary(node, &format!("{path}.{key}"))?,
                );
            }
            Yaml::Mapping(map)
        }
    })
}

fn steps(node: Node, path: &str, warnings: &mut Vec<String>) -> Result<Yaml> {
    let nodes = match node {
        Node::Seq(nodes) => nodes,
        Node::Map(entries) => vec![Node::Map(entries)],
        Node::Scalar(Yaml::Null) => return Ok(Yaml::Sequence(vec![])),
        Node::Scalar(Yaml::String(expression)) => return Ok(Yaml::String(expression)),
        other => bail!(
            "{path} must be steps or an expression, found {}",
            other.describe()
        ),
    };
    let mut output = Vec::new();
    for (index, node) in nodes.into_iter().enumerate() {
        let step_path = format!("{path}[{index}]");
        let Node::Map(mut entries) = node else {
            if let Node::Scalar(Yaml::String(expression)) = node {
                output.push(Yaml::String(expression));
                continue;
            }
            bail!(
                "{step_path} must be a step mapping or an expression, found {}",
                node.describe()
            );
        };
        // The reference's first `- var:` has its fields beside, not below, var.
        // Normalize this unambiguous shape and disclose it through warnings.
        if entries.len() > 1
            && matches!(entries.first(), Some((action, Node::Scalar(Yaml::Null))) if ["if", "var", "send", "log"].contains(&action.as_str()))
        {
            let (action, _) = entries.remove(0);
            warnings.push(format!("{step_path}: indent fields beneath {action}:"));
            entries = vec![(action, Node::Map(entries))];
        }
        for (action, payload) in entries {
            if action == "else" {
                let branch = steps(payload, &format!("{step_path}.else"), warnings)?;
                if !output
                    .last()
                    .is_some_and(|previous| !previous["if"].is_null())
                {
                    bail!("{step_path}.else must follow an if chain");
                }
                let mut wrapper = Mapping::new();
                wrapper.insert(Yaml::String("else".into()), branch);
                output.push(Yaml::Mapping(wrapper));
                continue;
            }
            let allowed: &[&str] = match action.as_str() {
                "if" => &["condition", "then", "else", "catch"],
                "var" => &["name", "value", "catch"],
                "send" => &["isi", "session_id", "message", "catch"],
                "log" => &["message", "catch"],
                _ => bail!("unknown flow action {action} at {step_path}"),
            };
            let Node::Map(fields) = payload else {
                bail!(
                    "{step_path}.{action} must be a mapping, found {}",
                    payload.describe()
                );
            };
            let mut body = Mapping::new();
            for (key, node) in fields {
                if !allowed.contains(&key.as_str()) {
                    bail!("unknown field {step_path}.{action}.{key}");
                }
                if body.contains_key(Yaml::String(key.clone())) {
                    bail!("duplicate field {step_path}.{action}.{key}");
                }
                let value = if ["then", "else", "catch"].contains(&key.as_str()) {
                    steps(node, &format!("{step_path}.{action}.{key}"), warnings)?
                } else {
                    ordinary(node, &format!("{step_path}.{action}.{key}"))?
                };
                body.insert(Yaml::String(key), value);
            }
            let required: &[&str] = match action.as_str() {
                "if" => &["condition", "then"],
                "var" => &["name", "value"],
                "send" => &["isi", "message"],
                _ => &["message"],
            };
            for field in required {
                if !body.contains_key(Yaml::String((*field).into())) {
                    bail!("{step_path}.{action}.{field} is required");
                }
                if !["then", "value"].contains(field) {
                    nonempty_scalar(
                        &body[Yaml::String((*field).into())],
                        &format!("{step_path}.{action}.{field}"),
                    )?;
                }
            }
            let mut wrapper = Mapping::new();
            wrapper.insert(Yaml::String(action), Yaml::Mapping(body));
            output.push(Yaml::Mapping(wrapper));
        }
    }
    Ok(Yaml::Sequence(output))
}

/// Only rewrite scalar starts. Literal/folded blocks and quoted YAML remain
/// untouched; YAML still owns indentation, escaping, comments and collections.
fn prepare_scalars(source: &str) -> Result<String> {
    let mut output = Vec::new();
    let mut block_indent = None;
    let mut inline = (0usize, None, true);
    for (line_number, line) in source.lines().enumerate() {
        let indent = line.len() - line.trim_start_matches(' ').len();
        if inline.0 > 0 || inline.1.is_some() {
            check_inline_scalar(line, line_number + 1, &mut inline)?;
            output.push(line.to_owned());
            continue;
        }
        if block_indent.is_some_and(|base| line.trim().is_empty() || indent > base) {
            output.push(line.to_owned());
            continue;
        }
        block_indent = None;
        if line.trim().is_empty() || line.trim_start().starts_with('#') {
            output.push(line.to_owned());
            continue;
        }
        let content = &line[indent..];
        let seq_prefix = if content.starts_with("- ") { 2 } else { 0 };
        let after_seq = &content[seq_prefix..];
        let scalar_offset = mapping_colon(after_seq)
            .map(|i| indent + seq_prefix + i + 1)
            .unwrap_or(indent + seq_prefix);
        let whitespace = line[scalar_offset..].len() - line[scalar_offset..].trim_start().len();
        let start = scalar_offset + whitespace;
        let scalar = &line[start..];
        if scalar.starts_with(['|', '>']) {
            block_indent = Some(indent);
            output.push(line.to_owned());
            continue;
        }
        let quoted = scalar.starts_with(['\'', '"', '[', '{']);
        if quoted {
            inline.2 = true;
            check_inline_scalar(scalar, line_number + 1, &mut inline)?;
            output.push(line.to_owned());
            continue;
        }
        if scalar.starts_with('!') || (!quoted && scalar.contains(": ")) {
            let value = strip_comment(scalar).trim_end();
            let value = if let Some(command) = value.strip_prefix('!') {
                format!("! {}", command.trim_start())
            } else {
                value.to_owned()
            };
            output.push(format!(
                "{}{}",
                &line[..start],
                serde_json::to_string(&value)?
            ));
        } else {
            output.push(line.to_owned());
        }
    }
    Ok(output.join("\n") + "\n")
}

// Track only quoted scalars and flow collections, leaving their syntax to YAML.
// YAML otherwise discards the anonymous `!` tag before our DSL sees the command.
fn check_inline_scalar(
    source: &str,
    line: usize,
    state: &mut (usize, Option<u8>, bool),
) -> Result<()> {
    let bytes = source.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let ch = bytes[i];
        if let Some(quote) = state.1 {
            if ch == b'\\' && quote == b'"' {
                i += 2;
                continue;
            }
            if ch == quote {
                if quote == b'\'' && bytes.get(i + 1) == Some(&quote) {
                    i += 2;
                    continue;
                }
                state.1 = None;
                state.2 = false;
                if state.0 == 0 {
                    break;
                }
            }
        } else {
            match ch {
                b'#' if i == 0 || bytes[i - 1].is_ascii_whitespace() => break,
                b'\'' | b'"' if state.2 => state.1 = Some(ch),
                b'[' | b'{' => {
                    state.0 += 1;
                    state.2 = true;
                }
                b']' | b'}' => {
                    state.0 = state.0.saturating_sub(1);
                    state.2 = false;
                }
                b',' => state.2 = true,
                b':' if bytes
                    .get(i + 1)
                    .is_none_or(|next| next.is_ascii_whitespace()) =>
                {
                    state.2 = true
                }
                b'!' if state.0 > 0
                    && state.2
                    && bytes
                        .get(i + 1)
                        .is_none_or(|next| next.is_ascii_whitespace()) =>
                {
                    bail!("bare ! command in inline YAML collection on line {line}; quote the complete expression, e.g. [\"! pwd\"]");
                }
                ch if !ch.is_ascii_whitespace() => state.2 = false,
                _ => {}
            }
        }
        i += 1;
    }
    Ok(())
}

fn mapping_colon(source: &str) -> Option<usize> {
    let mut quote = None;
    let mut escaped = false;
    for (i, ch) in source.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' && quote == Some('"') {
            escaped = true;
            continue;
        }
        if let Some(q) = quote {
            if ch == q {
                quote = None;
            }
            continue;
        }
        if ch == '\'' || ch == '"' {
            quote = Some(ch);
            continue;
        }
        if ch == ':'
            && source[i + 1..]
                .chars()
                .next()
                .is_none_or(char::is_whitespace)
        {
            return Some(i);
        }
        if ch == '[' || ch == '{' || ch == '!' {
            return None;
        }
    }
    None
}

fn strip_comment(source: &str) -> &str {
    let mut quote = None;
    let mut escaped = false;
    for (i, ch) in source.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' && quote == Some('"') {
            escaped = true;
            continue;
        }
        if let Some(q) = quote {
            if ch == q {
                quote = None;
            }
            continue;
        }
        if ch == '\'' || ch == '"' {
            quote = Some(ch);
            continue;
        }
        if ch == '#' && (i == 0 || source[..i].ends_with(char::is_whitespace)) {
            return &source[..i];
        }
    }
    source
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "linux")]
    #[test]
    fn wsl_rejects_non_linux_storage_even_through_a_symlink() {
        let directory = tempfile::tempdir().unwrap();
        let alias = directory.path().join("mounted-home");
        std::os::unix::fs::symlink("/proc", &alias).unwrap();
        let error = crate::state::validate_wsl_filesystem(&alias)
            .unwrap_err()
            .to_string();
        assert!(error.contains("copy your project to /home/silicon"));
        assert!(crate::state::validate_wsl_filesystem(&alias.join("missing")).is_err());
    }

    #[test]
    fn reference_flow_functions_execute_in_every_branch() {
        let config = parse_document(
            include_str!("../stemcell/silicon/silicon.yaml"),
            &mut vec![],
        )
        .unwrap();
        validate_expressions(&config).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let generated = crate::eval::value(
            &config["flow"][1],
            &json!({"request":{"tings":[{}]}}),
            dir.path(),
            "interpreter",
        )
        .unwrap();
        let reply_condition = generated[4]["if"]["condition"].as_str().unwrap();
        for data in [
            json!({"to":"deliberate@si:demo"}),
            json!({"to":"deliberate@si:demo","reply_to": null}),
        ] {
            assert_eq!(
                crate::eval::evaluate(
                    reply_condition,
                    &json!({"var": {"ting_kind":"new_message", "ting": {"data": data}}}),
                    dir.path(),
                    "interpreter"
                )
                .unwrap(),
                "false"
            );
        }
        // The reference expects this deployment-specific script; exercise its CEL arguments.
        let script = dir.path().join("time_delay.sh");
        fs::write(&script, "#!/bin/sh\nprintf '%s ' \"$@\"\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        for (kind, target, replies) in [
            ("new_message", "deliberate@si:demo", 3),
            ("new_message", "worker:job@si:demo", 3),
            ("new_message", "advisor@si:demo", 2),
            ("new_message", "intuit@si:demo", 1),
            ("tos>dm.msg.received", "deliberate@si:demo", 3),
            ("dm.msg.received", "deliberate@si:demo", 3),
            ("message_sent", "intuit@si:demo", 1),
            ("tos>dm.msg.sent", "intuit@si:demo", 1),
            ("dm.msg.sent", "intuit@si:demo", 1),
            ("event", "intuit@si:demo", 1),
        ] {
            let mut sent = Vec::new();
            let vars = crate::flow::execute(
                &config["flow"],
                json!({
                    "silicon": {"timezone": "Asia/Kolkata"},
                    "request": {"tings": [{"type": kind, "data": {
                        "from": "c:shubham", "to": target, "message": "hola {untrusted}", "msg_id": "message-1",
                        "sent_at": "2026-09-17T07:29:04Z", "sender_timezone": "UTC",
                        "timestamp": "2026-09-17T07:29:04Z",
                        "reply_to": {"sent_at": "2026-09-17T07:28:04Z", "sender_timezone": "UTC"}
                    }}]}
                }),
                dir.path(),
                "interpreter",
                |target, message, session| {
                    sent.push((
                        target.to_owned(),
                        message.to_owned(),
                        session.map(str::to_owned),
                    ));
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(sent.len(), replies, "{kind}: {target}: {sent:?}");
            assert!(sent[0].1.contains("hola"), "{sent:?}");
            if target.starts_with("deliberate") {
                assert!(vars["time_delay"]
                    .as_str()
                    .unwrap()
                    .contains("07:28:04 17:09:26 UTC"));
            }
            if target.starts_with("worker") {
                assert_eq!(
                    (&sent[2].0[..], sent[2].2.as_deref()),
                    ("worker", Some("job"))
                );
            }
        }
        let mut sent = Vec::new();
        crate::flow::execute(&config["flow"], json!({
            "silicon":{"timezone":"UTC"},
            "request":{"tings":[
                {"type":"event","data":{"timestamp":"2026-09-17T07:29:04Z","message":"first {untrusted}"}},
                {"type":"event","data":{"timestamp":"2026-09-17T07:29:05Z","message":"second"}},
                {"id":"canonical-id","type":"tos>dm.msg.received","data":{"message_id":"dm-id","text":"real notification"}},
                {"id":"other-id","type":"example>app.changed","data":{"text":"! touch must-not-run"}}
            ]}
        }), dir.path(), "interpreter", |_, message, _| { sent.push(message.to_owned()); Ok(()) }).unwrap();
        assert_eq!(sent.len(), 4);
        assert!(sent[0].contains("first {untrusted}"));
        assert!(sent[1].contains("second"));
        assert!(sent[2].contains("canonical-id") && sent[2].contains("real notification"));
        assert!(sent[3].contains("other-id") && sent[3].contains("! touch must-not-run"));
        assert!(!dir.path().join("must-not-run").exists());
        let logs = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        assert!(!logs.contains("[error]"), "{logs}");
    }

    #[test]
    fn parser_keeps_shell_commands_and_order_without_rewriting_templates() {
        let source = include_str!("../stemcell/silicon/silicon.yaml");
        let mut warnings = Vec::new();
        let config = parse_document(source, &mut warnings).unwrap();
        assert_eq!(config["silicon"]["SILICON_HOME"].as_str(), Some("! pwd"));
        assert_eq!(config["silicon"]["apps"][0].as_str(), Some("dm"));
        assert_eq!(
            config["silicon"]["setup"][0].as_str(),
            Some("! ./install_python.sh")
        );
        assert_eq!(
            config["isi"]["intuit"]["dna"]["assemble"][2].as_str(),
            Some("! ./contacts.sh !>> \"You have no contacts\"")
        );
        assert_eq!(config["flow"].as_sequence().unwrap().len(), 2);
        assert_eq!(
            config["flow"][0]["var"]["name"].as_str(),
            Some("ting_index")
        );
        assert!(config["flow"][1]
            .as_str()
            .unwrap()
            .contains("request.tings.map"));
        assert!(warnings.is_empty());
        assert!(parse_document(
            "silicon: {}\nsilicon: {}\nisi: {}\naccess: {}\nflow: []\n",
            &mut vec![]
        )
        .is_err());
        let source = "silicon: {}\nisi: {}\naccess: {}\nflow:\n  - log:\n      message: |\n        ! echo should-stay-text\n        command: unchanged\n  - log:\n      message: ! 'printf hello'\n";
        let parsed = parse_document(source, &mut vec![]).unwrap();
        assert_eq!(
            parsed["flow"][0]["log"]["message"].as_str(),
            Some("! echo should-stay-text\ncommand: unchanged\n")
        );
        assert_eq!(
            parsed["flow"][1]["log"]["message"].as_str(),
            Some("! 'printf hello'")
        );
        for collection in [
            "[! pwd]",
            "{assemble: [! pwd]}",
            "[\n ! pwd\n]",
            "[\"safe\", ! pwd]",
        ] {
            assert!(prepare_scalars(&format!("value: {collection}\n"))
                .unwrap_err()
                .to_string()
                .contains("quote the complete expression"));
        }
        let multiline = "silicon: {}\nisi: {}\naccess: {}\nflow:\n  - log:\n      message: \"first\n        ! keep this\n        colon: keep this too\"\n  - log: {message: Use ! as punctuation}\n  - log: {message: \"[! pwd]\"}\n";
        let parsed = parse_document(multiline, &mut vec![]).unwrap();
        assert_eq!(
            parsed["flow"][0]["log"]["message"].as_str(),
            Some("first ! keep this colon: keep this too")
        );
        assert_eq!(
            parsed["flow"][1]["log"]["message"].as_str(),
            Some("Use ! as punctuation")
        );
        assert_eq!(
            parsed["flow"][2]["log"]["message"].as_str(),
            Some("[! pwd]")
        );
        let inline_multiline =
            "value: {message: 'first\n  ! keep ''this''\n  colon: keep this too'}\n";
        assert_eq!(prepare_scalars(inline_multiline).unwrap(), inline_multiline);
    }

    #[test]
    fn identifiers_require_explicit_authority_and_produce_distinct_dns_hosts() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("silicon.yaml");
        let source = "silicon:\n  id: si:chef\n  org_id: acme\n  token: retained-token\n  timezone: UTC\n  SILICON_HOME: ! pwd\n  inference_providers: [all-available-providers]\nisi:\n  worker: {model: code, primary_send_mode: global, session_type: persistent, dna: {assemble: [], next_refresh: 30min}}\naccess: {worker: []}\nflow: []\n";
        fs::write(&path, source)?;
        let cfg = Config::load(&path)?;
        assert_eq!(cfg.silicon.silicon_org.as_deref(), Some("acme"));
        assert_eq!(crate::server::descriptor(&cfg).host, "chef.acme.localhost");
        let mut hosts = HashSet::new();
        for handle in [
            "chef".to_owned(),
            "head_of_growth".into(),
            "-chef-".into(),
            "hex".into(),
            "_".repeat(50),
        ] {
            fs::write(&path, source.replace("si:chef", &format!("si:{handle}")))?;
            let cfg = Config::load(&path)?;
            let host = crate::server::descriptor(&cfg).host;
            assert!(host.split('.').all(host_label));
            assert!(hosts.insert(host));
        }
        for org in ["owner_org".to_owned(), "-owner-".into(), "_".repeat(50)] {
            fs::write(
                &path,
                source.replace(
                    "org_id: acme",
                    &format!("org_id: '{org}'\n  SILICON_ORG: '{org}'"),
                ),
            )?;
            let cfg = Config::load(&path)?;
            assert_eq!(cfg.silicon.org_id.as_deref(), Some(org.as_str()));
            assert_eq!(cfg.silicon.silicon_org.as_deref(), Some(org.as_str()));
            assert!(crate::server::descriptor(&cfg)
                .host
                .split('.')
                .all(host_label));
        }
        for org in [
            "ab".to_owned(),
            "Owner".into(),
            "owner.org".into(),
            "a".repeat(51),
        ] {
            for field in ["org_id", "SILICON_ORG"] {
                fs::write(
                    &path,
                    if field == "org_id" {
                        source.replace("org_id: acme", &format!("org_id: '{org}'"))
                    } else {
                        source.replace("org_id: acme", &format!("org_id: acme\n  {field}: '{org}'"))
                    },
                )?;
                assert!(Config::load(&path).is_err(), "accepted {field}: {org}");
            }
        }
        for id in [
            "chef:acme",
            "c:chef",
            "si:si:chef",
            "si:ab",
            "si:Chef",
            "si:../chef",
            &format!("si:{}", "a".repeat(51)),
        ] {
            fs::write(&path, source.replace("si:chef", id))?;
            assert!(Config::load(&path).is_err(), "accepted {id}");
        }
        fs::write(&path, source.replace("  org_id: acme\n", ""))?;
        assert!(format!("{:#}", Config::load(&path).unwrap_err()).contains("silicon.org_id"));
        fs::write(
            &path,
            source.replace("org_id: acme", "org_id: acme\n  SILICON_ORG: selected"),
        )?;
        let cfg = Config::load(&path)?;
        assert_eq!(cfg.silicon.org_id.as_deref(), Some("acme"));
        assert_eq!(cfg.silicon.silicon_org.as_deref(), Some("selected"));
        assert_eq!(cfg.silicon.token.as_deref(), Some("retained-token"));
        fs::write(
            &path,
            source.replace("  token:", "  login: [acme>dm]\n  token:"),
        )?;
        assert!(format!("{:#}", Config::load(&path).unwrap_err()).contains("legacy org>app"));
        fs::write(
            &path,
            source.replace("  token:", "  login: ['! helper']\n  token:"),
        )?;
        assert_eq!(Config::load(&path)?.silicon.login, ["! helper"]);
        let old_inbox = cfg.home.join(".silicon/ting/chef:acme/selected/inbox.json");
        crate::state::write_json(
            &old_inbox,
            &json!({"pending": [{"unchanged": "chef:acme"}]}),
        )?;
        let bytes = fs::read(&old_inbox)?;
        assert!(crate::ting::Inbox::new(&cfg).is_err());
        assert_eq!(fs::read(&old_inbox)?, bytes);
        Ok(())
    }

    #[test]
    fn removed_legacy_mode_fields_name_their_replacement() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silicon.yaml");
        let base = "silicon:\n  id: si:test\n  org_id: org\n  token: token\n  timezone: UTC\n  SILICON_HOME: ! pwd\n  inference_providers: [all-available-providers]\nisi:\n  worker:\n    model: code\n    primary_send_mode: session\n    session_type: persistent\n    dna: {assemble: [], next_refresh: 30min}\naccess: {worker: []}\nflow: []\n";
        fs::write(&path, base).unwrap();
        Config::load(&path).unwrap();
        for (removed, line, canonical) in [
            ("sticky", "    sticky: false\n", "primary_send_mode"),
            (
                "archive_on_end",
                "    archive_on_end: false\n",
                "session_type",
            ),
        ] {
            fs::write(
                &path,
                base.replace("    model: code\n", &format!("    model: code\n{line}")),
            )
            .unwrap();
            let error = format!("{:#}", Config::load(&path).unwrap_err());
            // Not serde's "unknown field": the error has to say what to write instead.
            assert!(
                error.contains(removed) && error.contains(canonical),
                "{removed}: {error}"
            );
        }
    }

    #[test]
    fn load_resolves_home_and_validates_template_and_modes() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silicon.yaml");
        let source = "silicon:\n  id: si:test\n  org_id: org\n  token: token\n  timezone: UTC\n  SILICON_HOME: ! pwd\n  inference_providers: [all-available-providers]\nisi:\n  worker:\n    model: code\n    primary_send_mode: session\n    session_type: persistent\n    dna: {assemble: [], next_refresh: 30min}\naccess: {worker: []}\nflow: []\n";
        fs::write(&path, source).unwrap();
        let config = Config::load(&path).unwrap();
        assert_eq!(config.home, temp.path().canonicalize().unwrap());
        assert_eq!(
            config.isi["worker"].primary_send_mode.as_deref(),
            Some("session")
        );
        assert_eq!(
            config.isi["worker"].session_type.as_deref(),
            Some("persistent")
        );
        fs::write(&path, source.replace("id: si:test", "id: ...")).unwrap();
        assert!(Config::load(&path)
            .unwrap_err()
            .to_string()
            .contains("template placeholder"));
        fs::write(
            &path,
            source.replace("primary_send_mode: session", "primary_send_mode: invalid"),
        )
        .unwrap();
        assert!(Config::load(&path)
            .unwrap_err()
            .to_string()
            .contains("primary_send_mode"));
        fs::write(
            &path,
            source
                .replace(
                    "SILICON_HOME: ! pwd",
                    "SILICON_HOME: ! touch should-not-run; pwd",
                )
                .replace("token: token", "token: 'token !>> \"{unclosed\"'"),
        )
        .unwrap();
        assert!(Config::load(&path)
            .unwrap_err()
            .to_string()
            .contains("expression syntax"));
        assert!(!temp.path().join("should-not-run").exists());
        for invalid in ["heartbeat: {next: 30min, message: true}", "heartbeat: {next: 999999h, message: hi}", "new_session_suggestion: {cooldown_minutes: 30, min_new_messages: 2, suggestion_message: 123}"] {
            fs::write(&path, source.replace("    model: code", &format!("    model: code\n    {invalid}"))).unwrap();
            assert!(Config::load(&path).is_err(), "accepted {invalid}");
        }
        fs::write(
            &path,
            source.replace("next_refresh: 30min", "next_refresh: '30min !>> 60min'"),
        )
        .unwrap();
        Config::load(&path).unwrap();
        fs::write(&path, source.replace("    model: code", "    model: code\n    new_session_suggestion: {cooldown_minutes: 30, min_new_messages: '2 !>> 5', suggestion_message: hi}")).unwrap();
        Config::load(&path).unwrap();
    }

    #[test]
    fn load_expands_deferred_app_commands_without_invoking_them() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silicon.yaml");
        let app = temp.path().join("test dm");
        fs::write(&app, "#!/bin/sh\ntouch \"$SILICON_HOME/app-was-invoked\"\n").unwrap();
        fs::set_permissions(&app, fs::Permissions::from_mode(0o700)).unwrap();
        let app = shell_words::quote(app.to_str().unwrap());
        let login = format!("! {app} --title \"hello {{silicon.id}}\" --literal '!>>'");
        let source = format!("silicon:\n  id: si:test\n  org_id: org\n  token: token\n  timezone: UTC\n  SILICON_HOME: ! pwd\n  inference_providers: [all-available-providers]\n  login: [{}]\nisi:\n  worker:\n    model: code\n    primary_send_mode: global\n    session_type: persistent\n    dna: {{assemble: [], next_refresh: 30min}}\naccess: {{worker: []}}\nflow: []\n", serde_json::to_string(&login).unwrap());
        fs::write(&path, &source).unwrap();
        let config = Config::load(&path).unwrap();
        assert_eq!(
            config.silicon.login,
            [format!("! {app} --title \"hello si:test\" --literal '!>>'")]
        );
        assert!(!temp.path().join("app-was-invoked").exists());
        let source = source.replace(
            &format!("login: [{}]", serde_json::to_string(&login).unwrap()),
            &format!("login:\n    - ! {app}\n    - {login}"),
        );
        fs::write(&path, source).unwrap();
        let config = Config::load(&path).unwrap();
        assert_eq!(
            config.silicon.login,
            [
                format!("! {app}"),
                format!("! {app} --title \"hello si:test\" --literal '!>>'")
            ]
        );
        assert!(!temp.path().join("app-was-invoked").exists());
    }

    #[test]
    fn setup_is_deferred_and_app_ids_and_telemetry_are_validated() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silicon.yaml");
        let source = r#"silicon:
  id: si:test
  org_id: org
  token: token
  timezone: UTC
  SILICON_HOME: ! pwd
  inference_providers: [all-available-providers]
  setup:
    - ! touch must-not-exist; printf '{silicon.id}'
    - printf 'plain shell command'
  apps: ['{"dm"}', hook]
  login: [legacy-app]
  space_station:
    table_name: 'events-{silicon.id}'
    table_key: test-key
isi:
  worker:
    model: code
    primary_send_mode: global
    session_type: persistent
    dna: {assemble: [], next_refresh: 30min}
access: {worker: []}
flow: []
"#;
        fs::write(&path, source).unwrap();
        for _ in 0..2 {
            let cfg = Config::load(&path).unwrap();
            assert_eq!(
                cfg.silicon.setup[0],
                "! touch must-not-exist; printf '{silicon.id}'"
            );
            assert_eq!(cfg.silicon.setup[1], "printf 'plain shell command'");
            assert_eq!(cfg.silicon.apps, ["dm", "hook"]);
            assert_eq!(
                cfg.silicon.managed_apps(),
                ["dm", "hook", "legacy-app", "ting"]
            );
            let station = cfg.silicon.space_station.unwrap();
            assert_eq!(station.table_name, "events-si:test");
            assert_eq!(station.table_key, "test-key");
            assert!(!temp.path().join("must-not-exist").exists());
            assert_eq!(fs::read_to_string(&path).unwrap(), source);
        }
        for invalid in [
            "dm>bad",
            "tos>DM",
            "tos>bad_app",
            "tos>bad.app",
            "tos>-dm",
            "tos>",
            "tos>dm",
            "'! touch must-not-exist'",
            "dm, dm",
        ] {
            fs::write(&path, source.replace("'{\"dm\"}', hook", invalid)).unwrap();
            assert!(
                Config::load(&path).is_err(),
                "accepted invalid app {invalid}"
            );
            assert!(!temp.path().join("must-not-exist").exists());
        }
        for invalid in [
            source.replace("table_key: test-key", "table_key: ''"),
            source.replace("table_key: test-key", "table_key: ..."),
            source.replace("table_key: test-key", "table_key: test-key\n    unknown: nope"),
            source.replace("table_key: test-key", "table_key: test-key\n    table_key: duplicate"),
            source.replace("apps: ['{\"dm\"}', hook]", "apps: [12]"),
            source.replace("setup:\n    - ! touch must-not-exist; printf '{silicon.id}'\n    - printf 'plain shell command'", "setup: [true]"),
            source.replace("printf 'plain shell command'", "!"),
        ] {
            fs::write(&path, invalid).unwrap();
            assert!(Config::load(&path).is_err());
        }
        // Reject schema and all deferred expression errors before compile-time Bash.
        for invalid in [
            source.replace("  apps:", "  typo:"),
            source.replace("printf 'plain shell command'", "! printf '{invalid + }'"),
        ] {
            fs::write(
                &path,
                invalid.replace(
                    "SILICON_HOME: ! pwd",
                    "SILICON_HOME: ! touch must-not-exist; pwd",
                ),
            )
            .unwrap();
            assert!(Config::load(&path).is_err());
            assert!(!temp.path().join("must-not-exist").exists());
        }
    }

    #[test]
    fn app_settings_and_mutations_preserve_live_flow_and_secret_sources() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("silicon.yaml");
        let source = r#"# leave the source expressions intact
silicon:
  id: si:test
  org_id: org
  token: '! printf private-token'
  timezone: UTC
  SILICON_HOME: ! pwd
  SILICON_ORG: '{"another-org"}'
  inference_providers: [all-available-providers]
  apps: ['{"waveform"}']
  app_configs:
    waveform:
      default_tts_provider: google
      api_key: '! printf private-api-key'
      retries: 2
      enabled: true
      nested: [null, '{silicon.SILICON_ORG}']
isi:
  worker:
    model: code
    primary_send_mode: global
    session_type: persistent
    dna: {assemble: [], next_refresh: 30min}
access: {worker: []}
# This exact flow source must survive app mutations.
flow:
  log: {message: before}
  log: {message: second}
"#;
        fs::write(&path, source)?;
        let cfg = Config::load(&path)?;
        assert_eq!(cfg.silicon.silicon_org.as_deref(), Some("another-org"));
        assert_eq!(
            cfg.silicon.app_configs["waveform"],
            json!({
                "default_tts_provider": "google", "api_key": "private-api-key", "retries": 2,
                "enabled": true, "nested": [null, "another-org"]
            })
        );
        assert_eq!(cfg.silicon.managed_apps(), ["waveform", "ting"]);
        let added = set_app(&path, "dm", true)?;
        assert_eq!(added.path, path.canonicalize()?);
        assert_eq!(added.silicon.apps, ["waveform", "dm"]);
        assert_eq!(set_app(&path, "dm", true)?.silicon.apps.len(), 2);
        let removed = set_app(&path, "waveform", false)?;
        assert_eq!(removed.silicon.apps, ["dm"]);
        assert!(removed.silicon.app_configs.is_empty());
        let updated = fs::read_to_string(&path)?;
        assert!(updated.starts_with("# leave the source expressions intact\n"));
        assert!(updated.ends_with(source.split_once("isi:\n").unwrap().1));
        assert!(updated.contains("! printf private-token"));
        assert!(!updated.contains("token: private-token"));
        for id in ["ting", "iam", "not>an-id"] {
            assert!(set_app(&path, id, false).is_err());
            assert_eq!(fs::read_to_string(&path)?, updated);
        }
        fs::write(
            &path,
            updated
                .replace("message: before", "message: after")
                .replace(
                    "! printf private-token",
                    "! touch must-not-run; printf private-token",
                ),
        )?;
        assert_eq!(cfg.load_flow()?[0]["log"]["message"], "after");
        assert!(!dir.path().join("must-not-run").exists());
        fs::write(
            &path,
            source.replace("  SILICON_ORG: '{\"another-org\"}'\n", ""),
        )?;
        assert_eq!(
            Config::load(&path)?.silicon.silicon_org.as_deref(),
            Some("org")
        );
        for invalid in [
            source.replace("  app_configs:", "  webhooks: []\n  app_configs:"),
            source.replace("  app_configs:", "  webhook: []\n  app_configs:"),
            source.replace(
                "  app_configs:\n    waveform:",
                "  app_configs:\n    unknown:",
            ),
            source.replace(
                "  app_configs:\n    waveform:",
                "  app_configs: []\n  unused:",
            ),
            source.replace(
                "! printf private-api-key",
                "! printf private-api-key; echo vault locked >&2; exit 1",
            ),
            source.replace(
                "api_key: '! printf private-api-key'",
                "api_key: \"{'private-api-key' + }\"",
            ),
            source.replace(
                "access: {worker: []}",
                "access: {worker: ['{silicon.app_configs[\"waveform\"].api_key}']}",
            ),
        ] {
            fs::write(&path, invalid)?;
            let error = format!("{:#}", Config::load(&path).unwrap_err());
            assert!(!error.contains("private-api-key"), "{error}");
        }
        // A failed credential command still says why; only its stdout is withheld.
        fs::write(
            &path,
            source.replace(
                "! printf private-api-key",
                "! printf private-api-key; echo vault locked >&2; exit 1",
            ),
        )?;
        let error = format!("{:#}", Config::load(&path).unwrap_err());
        for expected in [
            "evaluate silicon.app_configs.waveform.api_key",
            "exit status: 1",
            "vault locked",
        ] {
            assert!(error.contains(expected), "{expected}: {error}");
        }
        let logs = fs::read_to_string(dir.path().join(".silicon/silicon.log"))?;
        assert!(!logs.contains("private-api-key"));
        Ok(())
    }

    #[test]
    fn compile_errors_carry_tool_output_parser_positions_and_found_values() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silicon.yaml");
        let source = "silicon:\n  id: si:test\n  org_id: org\n  token: token\n  timezone: UTC\n  SILICON_HOME: ! pwd\n  inference_providers: [all-available-providers]\nisi:\n  worker:\n    model: code\n    primary_send_mode: global\n    session_type: persistent\n    dna: {assemble: [], next_refresh: 30min}\naccess: {worker: []}\nflow: []\n";
        let load = |text: String| {
            fs::write(&path, text).unwrap();
            format!("{:#}", Config::load(&path).unwrap_err())
        };
        let error = load(source.replace(
            "model: code",
            "model: ! echo model-stdout; echo model-stderr >&2; exit 3",
        ));
        for expected in [
            "evaluate isi.worker.model",
            "exit status: 3",
            "model-stderr",
            "model-stdout",
        ] {
            assert!(error.contains(expected), "{expected}: {error}");
        }
        let error = load(source.replace("  timezone: UTC\n", "  timezone: UTC\n\tbad: tab\n"));
        assert!(error.contains(&path.display().to_string()), "{error}");
        assert!(error.contains("line 6 column 1"), "{error}");
        for (from, to, expected) in [
            (
                "primary_send_mode: global",
                "primary_send_mode: sometimes",
                "isi.worker.primary_send_mode must be global or session, found \"sometimes\"",
            ),
            (
                "timezone: UTC",
                "timezone: Mars/Olympus",
                "found \"Mars/Olympus\": failed to parse timezone",
            ),
            (
                "[all-available-providers]",
                "[all-available-providers, 7]",
                "or nested lists, found 7",
            ),
            (
                "model: code",
                "model: [code]",
                "invalid silicon schema in isi.worker.model: invalid type: sequence, expected a string",
            ),
            (
                "id: si:test",
                "id: 7",
                "invalid silicon schema in silicon.id: invalid type: integer `7`, expected a string",
            ),
            (
                "flow: []",
                "flow: [{send: {isi: other, message: hi}}]",
                "flow sends to unknown isi \"other\"; known ISIs are worker",
            ),
            (
                "access: {worker: []}",
                "access: {worker: [], typo: []}",
                "access defines unknown isi typo",
            ),
            (
                "access: {worker: []}",
                "access: {worker: nope}",
                "invalid silicon schema in access.worker: invalid type: string \"nope\", expected a sequence",
            ),
            (
                "flow: []",
                "flow: 7",
                "flow must be steps or an expression, found 7",
            ),
            (
                "  timezone: UTC",
                "  timezone: UTC\n  webhookz: []",
                "unknown field in silicon: \"webhookz\"; expected one of id, org_id",
            ),
        ] {
            let error = load(source.replace(from, to));
            assert!(error.contains(expected), "{expected}: {error}");
        }
        // A credential-bearing field names its wrong type, never its value.
        let error = load(source.replace(
            "  timezone: UTC\n",
            "  timezone: UTC\n  apps: [dm]\n  app_configs: {dm: '! printf app-config-private'}\n",
        ));
        assert!(
            error.contains("silicon.app_configs.dm must be a key-value mapping, found a string"),
            "{error}"
        );
        assert!(!error.contains("app-config-private"), "{error}");
        let error = load(source.replace(
            "  timezone: UTC\n",
            "  timezone: UTC\n  space_station: sk-station-private-key\n",
        ));
        assert!(
            error.contains("silicon.space_station must be a mapping, found a string"),
            "{error}"
        );
        assert!(!error.contains("station-private"), "{error}");
        // si app edits say whether the file on disk or the edit is what fails to compile.
        fs::write(
            &path,
            source.replace("timezone: UTC", "timezone: Mars/Olympus"),
        )
        .unwrap();
        let error = format!("{:#}", set_app(&path, "dm", true).unwrap_err());
        let current = format!(
            "the current {} does not compile",
            path.canonicalize().unwrap().display()
        );
        assert!(error.starts_with(&current), "{error}");
        assert!(error.contains("found \"Mars/Olympus\""), "{error}");
        let error = format!("{:#}", set_app(&path, "Bad App", true).unwrap_err());
        assert!(error.contains("got \"Bad App\""), "{error}");
    }

    #[test]
    fn compilation_shows_credential_failures_but_never_credentials() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silicon.yaml");
        let source = r#"silicon:
  id: si:test
  org_id: org
  token: "! printf compile-private-token"
  timezone: UTC
  SILICON_HOME: ! pwd
  inference_providers: [all-available-providers]
  space_station:
    table_name: events
    table_key: "! printf compile-private-key"
isi:
  worker:
    model: code
    primary_send_mode: global
    session_type: persistent
    dna: {assemble: [], next_refresh: 30min}
access: {worker: []}
flow: []
"#;
        fs::write(&path, source).unwrap();
        let config = Config::load(&path).unwrap();
        assert_eq!(
            config.silicon.token.as_deref(),
            Some("compile-private-token")
        );
        assert_eq!(
            config.silicon.space_station.unwrap().table_key,
            "compile-private-key"
        );
        assert!(config.generation.is_nil());
        for (invalid, stderr) in [
            ("printf compile-private-token", "token service down"),
            ("printf compile-private-key", "table service down"),
        ] {
            fs::write(
                &path,
                source.replace(invalid, &format!("{invalid}; echo {stderr} >&2; exit 1")),
            )
            .unwrap();
            let error = format!("{:#}", Config::load(&path).unwrap_err());
            assert!(
                error.contains(stderr) && error.contains("exit status: 1"),
                "{error}"
            );
            assert!(!error.contains("compile-private"), "{error}");
        }
        for invalid in [
            source.replace(
                "! printf compile-private-token",
                "{missing['compile-private-token']}",
            ),
            source.replace(
                "! printf compile-private-key",
                "{missing['compile-private-key']}",
            ),
            source.replace(
                "! printf compile-private-token",
                "{'compile-private-token' + }",
            ),
            source.replace("! printf compile-private-key", "{'compile-private-key' + }"),
            source.replace(
                "model: code",
                "model: \"! printf '{silicon.token}' >&2; exit 1\"",
            ),
            source.replace(
                "model: code",
                "model: \"! printf '{silicon.space_station.table_key}' >&2; exit 1\"",
            ),
        ] {
            fs::write(&path, invalid).unwrap();
            let error = format!("{:#}", Config::load(&path).unwrap_err());
            assert!(!error.contains("compile-private-token"), "{error}");
            assert!(!error.contains("compile-private-key"), "{error}");
        }
        let logs = fs::read_to_string(temp.path().join(".silicon/silicon.log")).unwrap();
        assert!(!logs.contains("compile-private-token"), "{logs}");
        assert!(!logs.contains("compile-private-key"), "{logs}");
        assert!(logs.contains("running: [compile-time expression]"));
        assert!(logs.contains("running: [credential expression]"));
    }
}
