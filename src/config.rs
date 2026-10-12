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
    pub token: Option<String>,
    pub timezone: Option<String>,
    #[serde(rename = "SILICON_HOME")]
    pub silicon_home: Option<String>,
    #[serde(default)]
    pub inference_providers: Yaml,
    #[serde(default)]
    pub setup: Vec<String>,
    #[serde(default)]
    pub apps: Vec<String>,
    #[serde(default)]
    pub app_configs: BTreeMap<String, Json>,
    #[serde(default)]
    pub login: Vec<String>,
    #[serde(default = "default_max_retries")]
    pub max_retries: usize,
}

fn default_max_retries() -> usize {
    10
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
    #[serde(default)]
    pub apps: Vec<String>,
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
    #[serde(default)]
    pub functions: Yaml,
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
    /// Apps are shared by the Silicon; ISI lists only organize their declarations.
    pub fn managed_apps(&self) -> Vec<String> {
        let mut seen = HashSet::new();
        self.isi
            .values()
            .flat_map(|isi| isi.apps.iter().cloned())
            .chain(self.silicon.managed_apps())
            .filter(|app| seen.insert(app.clone()))
            .collect()
    }

    /// Flow stays editable while a Silicon is connected; compile-time expressions are untouched.
    /// Every batch sees the file as it is now; the parse is reused only while it is unchanged.
    pub fn load_flow(&self) -> Result<Yaml> {
        self.load_program().map(|(flow, _)| flow)
    }

    pub fn load_program(&self) -> Result<(Yaml, Yaml)> {
        program_at(&self.path, &self.home, SETTLE)
    }

    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        Self::load_with_starters(path.as_ref(), false, |_| Ok(()))
    }

    pub(crate) fn load_for_connect(
        path: impl AsRef<Path>,
        before_starters: impl FnOnce(&Path) -> Result<()>,
    ) -> Result<Self> {
        Self::load_with_starters(path.as_ref(), true, before_starters)
    }

    fn load_with_starters(
        path: &Path,
        refresh: bool,
        before_starters: impl FnOnce(&Path) -> Result<()>,
    ) -> Result<Self> {
        let path = path
            .canonicalize()
            .with_context(|| format!("config not found: {}", path.display()))?;
        utf8(&path, "the config path")?;
        let mut warnings = Vec::new();
        let source =
            fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
        let mut document = parse_document(&source, &mut warnings)
            .with_context(|| format!("invalid silicon YAML in {}", path.display()))?;
        let base = path
            .parent()
            .ok_or_else(|| anyhow!("config {} has no parent directory", path.display()))?;
        validate_expressions(&document).context("invalid silicon expression syntax")?;
        // Check local imports and control flow before evaluating even SILICON_HOME.
        // Starter files need that home first; their syntax is checked immediately after download.
        let local_functions = resolve_functions(
            &document["functions"],
            base,
            &mut Vec::new(),
            &mut Vec::new(),
            &mut Vec::new(),
            &mut None,
        )?;
        crate::flow::validate_with_functions(&document["flow"], &local_functions)?;
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
        let text = utf8(&home, "silicon.SILICON_HOME")?.to_owned();
        before_starters(&home)?;
        let mut starters = Some(crate::starters::Resolver::new(&home, refresh));
        starters
            .as_mut()
            .unwrap()
            .resolve_isi(&mut document["isi"])?;
        document["functions"] = resolve_functions(
            &document["functions"],
            base,
            &mut Vec::new(),
            &mut Vec::new(),
            &mut warnings,
            &mut starters,
        )?;
        let mut flow = document["flow"].clone();
        starters
            .as_mut()
            .unwrap()
            .resolve_program(&mut flow, &mut document["functions"])?;
        document["flow"] = flow;
        validate_expressions(&document).context("invalid silicon expression syntax")?;
        crate::flow::validate_with_functions(&document["flow"], &document["functions"])?;
        env["isi"] = to_json(&document["isi"], "isi")?;
        document["silicon"]["SILICON_HOME"] = Yaml::String(text.clone());
        env["silicon"]["SILICON_HOME"] = Json::String(text);
        for key in ["id", "token", "timezone"] {
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
                if let Some(apps) = isi["apps"].as_sequence_mut() {
                    for (index, app) in apps.iter_mut().enumerate() {
                        *app = Yaml::String(
                            crate::eval::app_command(app.as_str().unwrap(), &env, &home)
                                .with_context(|| format!("evaluate isi.{name}.apps[{index}]"))?,
                        );
                    }
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
        result.map_err(|error| masked(error, &crate::redaction::silicon_secrets(&env["silicon"])))
    }

    fn validate(&mut self) -> Result<()> {
        let id = required(&self.silicon.id, "silicon.id")?;
        if !valid_silicon_id(id) {
            bail!("silicon.id must be si:<handle> (3–30 lowercase letters, digits, underscores or hyphens), found {id:?}");
        }
        required(&self.silicon.token, "silicon.token")?;
        let timezone = required(&self.silicon.timezone, "silicon.timezone")?;
        timezone.parse::<Tz>().with_context(|| {
            format!("silicon.timezone must be an IANA timezone such as UTC or Asia/Kolkata, found {timezone:?}")
        })?;
        validate_providers(&self.silicon.inference_providers)?;
        validate_apps(&self.silicon.apps, "silicon.apps")?;
        for (name, isi) in &self.isi {
            validate_apps(&isi.apps, &format!("isi.{name}.apps"))?;
        }
        let managed = self.managed_apps();
        for (app, config) in &self.silicon.app_configs {
            if !crate::apps::valid_id(app) || !managed.contains(app) {
                bail!(
                    "silicon.app_configs keys must be managed Silicon Apps IDs, e.g. dm, found {app:?}; managed apps are {}",
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
        for (index, command) in self.silicon.login.iter().enumerate() {
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
            validate_isi_blocks(name, isi, &mut self.warnings)?;
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
        Ok(())
    }
}

/// Expressions, the YAML, the saved connection registry and every answer about a connection
/// carry paths as text, so a path that is not UTF-8 is refused by name instead of failing
/// (or panicking) later. Unlike a sub-second interval this refuses nothing 5.0.2 kept: it
/// panicked on such a SILICON_HOME and could not save a connection with such a YAML path,
/// so no saved Silicon has one to restore. Carried lossily, the path would connect a
/// Silicon whose registry entry cannot be written, which stops every Silicon being saved.
fn utf8<'a>(path: &'a Path, what: &str) -> Result<&'a str> {
    path.to_str().ok_or_else(|| {
        anyhow!(
            "{what} {} is not valid UTF-8, and the saved connection registry and every answer about a connection carry it as text; rename it so every part of the path is UTF-8",
            path.display()
        )
    })
}

/// How long after its last change a YAML file must be before its parse is reused.
/// Timestamps are coarse on some filesystems, so an edit within the same tick as the read
/// could keep every stamp; a file that changed that recently is simply read again.
const SETTLE: std::time::Duration = std::time::Duration::from_secs(3);

/// Which version of a file was read: an edit, a replacement or a restore changes one of these.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Stamp {
    device: u64,
    inode: u64,
    length: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}

impl Stamp {
    fn of(path: &Path) -> Result<Self> {
        use std::os::unix::fs::MetadataExt;
        let metadata = fs::metadata(path).with_context(|| format!("read {}", path.display()))?;
        Ok(Self {
            device: metadata.dev(),
            inode: metadata.ino(),
            length: metadata.size(),
            modified: (metadata.mtime(), metadata.mtime_nsec()),
            changed: (metadata.ctime(), metadata.ctime_nsec()),
        })
    }

    /// True when the file last changed at least `settle` ago by the wall clock.
    fn settled(&self, settle: std::time::Duration) -> bool {
        let (seconds, nanos) = self.modified.max(self.changed);
        let last = std::time::Duration::new(
            u64::try_from(seconds).unwrap_or(0),
            u32::try_from(nanos.clamp(0, 999_999_999)).unwrap_or(0),
        );
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .is_ok_and(|now| last + settle <= now)
    }
}

#[derive(Clone)]
struct Program {
    home: PathBuf,
    files: Vec<(PathBuf, Stamp)>,
    flow: Yaml,
    functions: Yaml,
}

/// Cache a flow together with its definitions and all imported file stamps.
static FLOWS: std::sync::Mutex<BTreeMap<PathBuf, Program>> = std::sync::Mutex::new(BTreeMap::new());

fn program_at(path: &Path, home: &Path, settle: std::time::Duration) -> Result<(Yaml, Yaml)> {
    use crate::Recover;
    let before = Stamp::of(path)?;
    if let Some(program) = FLOWS.lock().recover().get(path) {
        if program.home == home
            && program
                .files
                .iter()
                .all(|(path, stamp)| Stamp::of(path).is_ok_and(|now| now == *stamp))
        {
            return Ok((program.flow.clone(), program.functions.clone()));
        }
    }
    let source = fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
    let document = parse_document(&source, &mut Vec::new())
        .with_context(|| format!("invalid silicon YAML in {}", path.display()))?;
    let mut flow = document["flow"].clone();
    let mut starters = Some(crate::starters::Resolver::new(home, false));
    let mut files = vec![(path.to_owned(), before)];
    let mut functions = resolve_functions(
        &document["functions"],
        path.parent().unwrap(),
        &mut files,
        &mut Vec::new(),
        &mut Vec::new(),
        &mut starters,
    )?;
    let starters = starters.as_mut().unwrap();
    starters.resolve_program(&mut flow, &mut functions)?;
    for path in starters.cached_files() {
        files.push((path.clone(), Stamp::of(path)?));
    }
    crate::flow::validate_with_functions(&flow, &functions)?;
    // Kept only when nothing changed while it was read and the change before is settled.
    if files.iter().all(|(path, stamp)| {
        stamp.settled(settle) && Stamp::of(path).is_ok_and(|now| now == *stamp)
    }) {
        let mut flows = FLOWS.lock().recover();
        // One entry per connected YAML; files connected long ago are simply dropped.
        if flows.len() >= 64 {
            flows.clear();
        }
        flows.insert(
            path.to_owned(),
            Program {
                home: home.to_owned(),
                files,
                flow: flow.clone(),
                functions: functions.clone(),
            },
        );
    }
    Ok((flow, functions))
}

/// Import paths are literal, relative to their containing file; loading never executes them.
fn resolve_functions(
    value: &Yaml,
    base: &Path,
    files: &mut Vec<(PathBuf, Stamp)>,
    loading: &mut Vec<PathBuf>,
    warnings: &mut Vec<String>,
    starters: &mut Option<crate::starters::Resolver>,
) -> Result<Yaml> {
    if loading.len() >= 64 {
        bail!("function imports exceed 64 levels");
    }
    match value {
        Yaml::Null => Ok(Yaml::Mapping(Mapping::new())),
        Yaml::Mapping(_) => Ok(value.clone()),
        Yaml::Sequence(sources) => {
            let mut merged = Mapping::new();
            for source in sources {
                let functions =
                    resolve_functions(source, base, files, loading, warnings, starters)?;
                for (name, definition) in functions.as_mapping().unwrap() {
                    if merged.insert(name.clone(), definition.clone()).is_some() {
                        bail!("duplicate function {}", key_name(name));
                    }
                }
            }
            Ok(Yaml::Mapping(merged))
        }
        Yaml::String(source) => {
            if crate::starters::is_reference(source) {
                return match starters {
                    Some(starters) => starters.function_source(source),
                    None => Ok(Yaml::Mapping(Mapping::new())),
                };
            }
            if source.trim().is_empty() {
                bail!("functions import path must not be empty");
            }
            let path = base.join(source);
            let canonical = path
                .canonicalize()
                .with_context(|| format!("read functions {}", path.display()))?;
            if loading.contains(&canonical) {
                bail!("circular functions import: {}", path.display());
            }
            files.push((path.clone(), Stamp::of(&path)?));
            let source = fs::read_to_string(&path)
                .with_context(|| format!("read functions {}", path.display()))?;
            let node: Node = serde_yaml::from_str(&prepare_scalars(&source)?)
                .with_context(|| format!("invalid functions YAML in {}", path.display()))?;
            let functions = function_definitions(node, "functions", warnings)
                .with_context(|| format!("invalid functions in {}", path.display()))?;
            loading.push(canonical);
            let result = resolve_functions(
                &functions,
                path.parent().unwrap(),
                files,
                loading,
                warnings,
                starters,
            );
            loading.pop();
            result
        }
        _ => bail!(
            "functions must be a mapping, a file path, or a list of these, found {}",
            shown(value)
        ),
    }
}

/// Rebuild the chain link by link so every cause survives with only credential values masked.
fn masked(error: anyhow::Error, secrets: &[String]) -> anyhow::Error {
    let mut causes: Vec<_> = error
        .chain()
        .map(|cause| crate::redaction::redact_text(&cause.to_string(), secrets))
        .collect();
    let mut rebuilt = anyhow!(causes.pop().unwrap_or_default());
    while let Some(outer) = causes.pop() {
        rebuilt = rebuilt.context(outer);
    }
    rebuilt
}

fn validate_expressions(document: &Yaml) -> Result<()> {
    fields(
        document,
        "config",
        &["silicon", "isi", "access", "flow", "functions"],
    )?;
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
                    "token",
                    "timezone",
                    "SILICON_HOME",
                    "inference_providers",
                    "setup",
                    "apps",
                    "app_configs",
                    "login",
                    "max_retries",
                ],
            )?;
            let mut silicon = value.clone();
            if let Some(token) = silicon.as_mapping_mut().and_then(|map| map.remove("token")) {
                crate::eval::validate_secret(&token).context("silicon.token")?;
            }
            if let Some(configs) = silicon
                .as_mapping_mut()
                .and_then(|map| map.remove("app_configs"))
            {
                let configs = configs.as_mapping().with_context(|| {
                    format!(
                        "silicon.app_configs must map Silicon Apps IDs to key-value mappings, found {}",
                        kind(&configs)
                    )
                })?;
                for (app, config) in configs {
                    let Some(app) = app.as_str().filter(|app| crate::apps::valid_id(app)) else {
                        bail!(
                            "silicon.app_configs keys must be Silicon Apps IDs, e.g. dm, found {}",
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
            if key.as_str() == Some("isi") {
                if let Some(isies) = value.as_mapping() {
                    for (name, isi) in isies {
                        if let Some(apps) = isi.as_mapping().and_then(|map| map.get("apps")) {
                            let path = format!("isi.{}.apps", key_name(name));
                            let apps = apps.as_sequence().with_context(|| {
                                format!("{path} must be a list, found {}", shown(apps))
                            })?;
                            for (index, app) in apps.iter().enumerate() {
                                let source = app.as_str().with_context(|| {
                                    format!(
                                        "{path}[{index}] must be a string, found {}",
                                        shown(app)
                                    )
                                })?;
                                crate::eval::validate_app_command(source)
                                    .with_context(|| format!("{path}[{index}]"))?;
                            }
                        }
                    }
                }
            }
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

/// Update app declarations, preserving live flow and all other source verbatim.
pub(crate) fn set_app(path: &Path, isi_name: &str, id: &str, installed: bool) -> Result<Config> {
    use std::{fs::OpenOptions, io::Write, os::unix::fs::OpenOptionsExt};
    if !crate::apps::valid_id(id) {
        bail!("expected a Silicon Apps ID (e.g. dm), got {id:?}");
    }
    if !installed && ["silicon-accounts", "ting"].contains(&id) {
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
    if !current.isi.contains_key(isi_name) {
        bail!("unknown isi {isi_name}");
    }
    if installed && current.isi[isi_name].apps.iter().any(|app| app == id) {
        return Ok(current);
    }
    let mut document = parse_document(&source, &mut Vec::new())
        .with_context(|| format!("invalid silicon YAML in {}", path.display()))?;
    let original = document.clone();
    // A Starter ISI needs a local override even when its original value was `default`.
    for (key, isi) in document["isi"].as_mapping_mut().unwrap() {
        let key = key.as_str().unwrap();
        let name = key
            .strip_prefix("starter:isi:")
            .or_else(|| key.strip_prefix("starter:"))
            .unwrap_or(key)
            .split('@')
            .next()
            .unwrap();
        let configured = &current.isi[name].apps;
        if (installed && name != isi_name)
            || (!installed && !configured.iter().any(|app| app == id))
        {
            continue;
        }
        if !isi.is_mapping() {
            *isi = Yaml::Mapping(Mapping::new());
        }
        let mut apps = isi["apps"]
            .as_sequence()
            .cloned()
            .unwrap_or_else(|| configured.iter().cloned().map(Yaml::String).collect());
        if installed {
            apps.push(id.into());
        } else {
            apps = apps
                .into_iter()
                .zip(configured)
                .filter_map(|(source, app)| (app != id).then_some(source))
                .collect();
        }
        isi["apps"] = Yaml::Sequence(apps);
    }
    if !installed {
        let silicon = document["silicon"].as_mapping_mut().unwrap();
        for (key, configured) in [
            ("apps", &current.silicon.apps),
            ("login", &current.silicon.login),
        ] {
            if let Some(apps) = silicon.get_mut(key).and_then(Yaml::as_sequence_mut) {
                *apps = apps
                    .iter()
                    .zip(configured)
                    .filter_map(|(source, app)| {
                        let command = app.trim().strip_prefix('!').unwrap_or(app).trim();
                        let matches = shell_words::split(command)
                            .is_ok_and(|args| args.first().is_some_and(|program| program == id));
                        (!matches).then_some(source.clone())
                    })
                    .collect();
            }
        }
        if let Some(configs) = silicon
            .get_mut("app_configs")
            .and_then(Yaml::as_mapping_mut)
        {
            configs.remove(id);
        }
    }
    let mut next = source.clone();
    for section in ["silicon", "isi"] {
        if original[section] == document[section] {
            continue;
        }
        next = replace_section(&next, section, &document[section])?;
    }
    let reparsed =
        parse_document(&next, &mut Vec::new()).context("re-read the rewritten app declarations")?;
    if reparsed != document {
        bail!(
            "cannot safely update the YAML layout of {}; use top-level silicon and isi blocks",
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

fn replace_section(source: &str, section: &str, value: &Yaml) -> Result<String> {
    let mut start = None;
    let mut end = source.len();
    let mut offset = 0;
    for line in source.split_inclusive('\n') {
        if !line.starts_with(char::is_whitespace) && !line.starts_with('#') {
            if let Some(colon) = mapping_colon(line) {
                let key = serde_yaml::from_str::<String>(&line[..colon]).ok();
                if key.as_deref() == Some(section) {
                    start = Some(offset);
                } else if start.is_some() {
                    end = offset;
                    break;
                }
            }
        }
        offset += line.len();
    }
    // ponytail: preserve block-style YAML; add source spans if compact root maps need editing.
    let start =
        start.with_context(|| format!("si app requires {section} as a top-level YAML block"))?;
    for line in source[start..end].split_inclusive('\n').rev() {
        if line.trim().is_empty() || line.starts_with('#') {
            end -= line.len();
        } else {
            break;
        }
    }
    let replacement = serde_yaml::to_string(&BTreeMap::from([(section, value)]))?;
    Ok(format!(
        "{}{}{}",
        &source[..start],
        replacement,
        &source[end..]
    ))
}

fn validate_apps(apps: &[String], path: &str) -> Result<()> {
    let mut seen = HashSet::new();
    for app in apps {
        if !crate::apps::valid_id(app) {
            bail!("{path} entries must be Silicon Apps IDs, e.g. dm, found {app:?}");
        }
        if !seen.insert(app) {
            bail!("{path} repeats {app}");
        }
    }
    Ok(())
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
        (3..=30).contains(&handle.len())
            && handle
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-".contains(&b))
    })
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

/// A literal below a second is accepted, as 5.0.2 accepted it, so a Silicon saved with one
/// still restores after an update; the runtime floors every interval at a second, which a
/// warning says. Anything not positive, not finite or above ten years is refused.
fn interval(value: &Yaml, path: &str, warnings: &mut Vec<String>) -> Result<()> {
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
    match number.trim().parse::<f64>().map(|n| n * multiplier) {
        Ok(seconds) if seconds.is_finite() && seconds > 0.0 && seconds <= 315360000. => {
            if seconds < 1.0 {
                warnings.push(format!("{path} is below 1 second; it runs every second"));
            }
            Ok(())
        }
        _ => bail!("{path} must be a positive interval of at most ten years, e.g. 30min, or an expression, found {source:?}"),
    }
}

fn nonempty_string(value: &Yaml, path: &str) -> Result<()> {
    if value.as_str().is_some_and(|s| !s.is_empty()) {
        return Ok(());
    }
    bail!("{path} must be a nonempty string, found {}", shown(value))
}

fn validate_isi_blocks(name: &str, isi: &Isi, warnings: &mut Vec<String>) -> Result<()> {
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
    interval(
        &dna["next_refresh"],
        &format!("{path}.next_refresh"),
        warnings,
    )?;
    if let Some(heartbeat) = &isi.heartbeat {
        let path = format!("isi.{name}.heartbeat");
        fields(heartbeat, &path, &["next", "message"])?;
        interval(&heartbeat["next"], &format!("{path}.next"), warnings)?;
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
            warnings,
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
        let mut value = if key == "flow" {
            steps(node, "flow", warnings)?
        } else if key == "functions" {
            function_definitions(node, "functions", warnings)?
        } else {
            ordinary(node, &key)?
        };
        if key == "silicon" {
            if let Some(silicon) = value.as_mapping_mut() {
                // Ignore legacy telemetry credentials before validating or evaluating expressions.
                silicon.remove("space_station");
            }
        }
        result.insert(Yaml::String(key), value);
    }
    for key in ["silicon", "isi", "access", "flow"] {
        if !result.contains_key(Yaml::String(key.into())) {
            bail!("missing required top-level key {key}");
        }
    }
    Ok(Yaml::Mapping(result))
}

pub(crate) fn parse_starter_functions(source: &str) -> Result<Yaml> {
    let node: Node = serde_yaml::from_str(&prepare_scalars(source)?)?;
    let Node::Map(mut entries) = node else {
        bail!("Starter function.yaml must be a mapping");
    };
    if entries.len() != 1 || entries[0].0 != "functions" {
        bail!("Starter function.yaml must contain one functions mapping");
    }
    function_definitions(entries.remove(0).1, "functions", &mut Vec::new())
}

pub(crate) fn parse_starter_isi(source: &str) -> Result<Yaml> {
    let node: Node = serde_yaml::from_str(&prepare_scalars(source)?)?;
    ordinary(node, "Starter isi.yaml")
}

fn function_definitions(node: Node, path: &str, warnings: &mut Vec<String>) -> Result<Yaml> {
    match node {
        Node::Scalar(Yaml::Null | Yaml::String(_)) => ordinary(node, path),
        Node::Seq(sources) => Ok(Yaml::Sequence(
            sources
                .into_iter()
                .map(|source| function_definitions(source, path, warnings))
                .collect::<Result<_>>()?,
        )),
        Node::Map(entries) => {
            let mut functions = Mapping::new();
            for (name, definition) in entries {
                if functions.contains_key(&name) {
                    bail!("duplicate function {name}");
                }
                let Node::Map(fields) = definition else {
                    bail!(
                        "{path}.{name} must be a mapping, found {}",
                        definition.describe()
                    );
                };
                let mut function = Mapping::new();
                for (key, node) in fields {
                    let field = format!("{path}.{name}.{key}");
                    if function.contains_key(&key) {
                        bail!("duplicate field {field}");
                    }
                    let value = match key.as_str() {
                        "do" => steps(node, &field, warnings)?,
                        "params" => ordinary(node, &field)?,
                        _ => bail!("unknown field {field}"),
                    };
                    function.insert(key.into(), value);
                }
                functions.insert(name.into(), Yaml::Mapping(function));
            }
            Ok(Yaml::Mapping(functions))
        }
        _ => bail!(
            "{path} must be a mapping, a file path, or a list of these, found {}",
            node.describe()
        ),
    }
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
            if crate::starters::is_reference(&action) {
                // Reuse call parsing, including ordered steps in then/catch branches.
                let mut fields = match payload {
                    Node::Map(fields) => fields,
                    Node::Scalar(Yaml::Null) => Vec::new(),
                    Node::Scalar(Yaml::String(value)) if value == "default" => Vec::new(),
                    other => bail!(
                        "{step_path}.{action} expects an args/then/catch mapping, found {}",
                        other.describe()
                    ),
                };
                if fields.iter().any(|(key, _)| key == "function") {
                    bail!("{step_path}.{action} cannot override its function reference");
                }
                fields.insert(0, ("function".into(), Node::Scalar(action.into())));
                let parsed = steps(
                    Node::Map(vec![("call".into(), Node::Map(fields))]),
                    &step_path,
                    warnings,
                )?;
                output.extend(parsed.as_sequence().unwrap().iter().cloned());
                continue;
            }
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
            if action == "return" {
                let value = ordinary(payload, &format!("{step_path}.return"))?;
                output.push(Yaml::Mapping(Mapping::from_iter([(
                    "return".into(),
                    value,
                )])));
                continue;
            }
            let allowed: &[&str] = match action.as_str() {
                "if" => &["condition", "then", "else", "catch"],
                "var" => &["name", "value", "catch"],
                "send" => &["isi", "session_id", "message", "aggregate", "catch"],
                "log" => &["message", "catch"],
                "for" => &["list", "var", "then", "catch"],
                "call" => &["function", "args", "then", "catch"],
                "switch" => &["value", "cases", "default", "catch"],
                "collect" => &["name", "value", "catch"],
                "continue" | "break" | "exit" => &["reason", "catch"],
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
                let value = if ["then", "else", "catch", "default"].contains(&key.as_str()) {
                    steps(node, &format!("{step_path}.{action}.{key}"), warnings)?
                } else if action == "switch" && key == "cases" {
                    switch_cases(node, &format!("{step_path}.switch.cases"), warnings)?
                } else {
                    ordinary(node, &format!("{step_path}.{action}.{key}"))?
                };
                body.insert(Yaml::String(key), value);
            }
            let required: &[&str] = match action.as_str() {
                "if" => &["condition", "then"],
                "var" | "collect" => &["name", "value"],
                "send" => &["isi", "message"],
                "for" => &["list", "var", "then"],
                "call" => &["function"],
                "switch" => &["value", "cases"],
                "continue" | "break" | "exit" => &["reason"],
                _ => &["message"],
            };
            for field in required {
                if !body.contains_key(Yaml::String((*field).into())) {
                    bail!("{step_path}.{action}.{field} is required");
                }
                if !["then", "value", "list", "cases"].contains(field) {
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

fn switch_cases(node: Node, path: &str, warnings: &mut Vec<String>) -> Result<Yaml> {
    let Node::Seq(cases) = node else {
        bail!("{path} must be a list, found {}", node.describe());
    };
    let mut output = Vec::new();
    for (index, node) in cases.into_iter().enumerate() {
        let Node::Map(fields) = node else {
            bail!(
                "{path}[{index}] must be a mapping, found {}",
                node.describe()
            );
        };
        let mut case = Mapping::new();
        for (key, node) in fields {
            let field = format!("{path}[{index}].{key}");
            if case.contains_key(&key) {
                bail!("duplicate field {field}");
            }
            let value = match key.as_str() {
                "case" => ordinary(node, &field)?,
                "then" => steps(node, &field, warnings)?,
                _ => bail!("unknown field {field}"),
            };
            case.insert(key.into(), value);
        }
        output.push(Yaml::Mapping(case));
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
mod retired_reporting_tests {
    use super::*;

    #[test]
    fn legacy_space_station_expressions_are_ignored() {
        let home = std::env::temp_dir().join(format!("silicon-config-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&home).unwrap();
        let path = home.join("silicon.yaml");
        fs::write(
            &path,
            r#"silicon:
  id: si:example
  token: test-token
  timezone: UTC
  SILICON_HOME: .
  inference_providers: [all-available-providers]
  space_station:
    table_name: '{invalid expression'
    table_key: '! touch reporting-was-run'
isi:
  main:
    model: fast
    primary_send_mode: global
    session_type: persistent
    dna:
      assemble: []
      next_refresh: 30min
access:
  main: []
flow: []
"#,
        )
        .unwrap();
        let config = Config::load(&path).unwrap();
        assert!(serde_json::to_value(config).unwrap()["silicon"]
            .get("space_station")
            .is_none());
        assert!(!home.join("reporting-was-run").exists());
        fs::remove_dir_all(home).unwrap();
    }
}
