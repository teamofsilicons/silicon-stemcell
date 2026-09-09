//! YAML plus the small shell/ordered-flow notation used by silicon.yaml.
use anyhow::{anyhow, bail, Context, Result};
use chrono_tz::Tz;
use serde::de::{MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Value as Json};
use serde_yaml::{Mapping, Number, Value as Yaml};
use std::{
    collections::BTreeMap,
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
    pub login: Vec<String>,
    #[serde(default)]
    pub webhook: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Isi {
    pub model: Option<String>,
    pub primary_send_mode: Option<String>,
    pub session_type: Option<String>,
    pub archive_on_end: Option<bool>,
    pub sticky: Option<bool>,
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
}

impl Config {
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path
            .as_ref()
            .canonicalize()
            .with_context(|| format!("config not found: {}", path.as_ref().display()))?;
        let mut warnings = Vec::new();
        let mut document = parse_document(&fs::read_to_string(&path)?, &mut warnings)
            .with_context(|| format!("invalid silicon YAML in {}", path.display()))?;
        validate_expressions(&document).context("invalid silicon expression syntax")?;
        let base = path
            .parent()
            .ok_or_else(|| anyhow!("config has no parent directory"))?;
        let mut env = json!({"request": {}, "var": {}, "silicon": document["silicon"],
            "isi": document["isi"], "access": document["access"]});
        let home_source = document["silicon"]["SILICON_HOME"]
            .as_str()
            .ok_or_else(|| anyhow!("silicon.SILICON_HOME is required and must be a string"))?;
        let home = crate::eval::evaluate(home_source, &env, base, "interpreter")
            .context("evaluate silicon.SILICON_HOME")?;
        if home.trim().is_empty() {
            bail!("silicon.SILICON_HOME evaluated to an empty path");
        }
        let home = base
            .join(home)
            .canonicalize()
            .context("silicon.SILICON_HOME must exist")?;
        if !home.is_dir() {
            bail!("silicon.SILICON_HOME must be a directory");
        }
        document["silicon"]["SILICON_HOME"] = Yaml::String(home.to_string_lossy().into_owned());
        env["silicon"]["SILICON_HOME"] = json!(home);
        for key in ["id", "token", "timezone"] {
            evaluate_field(&mut document["silicon"], key, &env, &home, "interpreter")?;
            env["silicon"][key] = serde_json::to_value(&document["silicon"][key])?;
        }
        evaluate_provider_values(&mut document["silicon"]["inference_providers"], &env, &home)?;
        env["silicon"]["inference_providers"] =
            serde_json::to_value(&document["silicon"]["inference_providers"])?;
        for key in ["login", "webhook"] {
            if let Some(commands) = document["silicon"][key].as_sequence_mut() {
                for (index, command) in commands.iter_mut().enumerate() {
                    *command = Yaml::String(
                        crate::eval::app_command(command.as_str().unwrap(), &env, &home)
                            .with_context(|| format!("evaluate silicon.{key}[{index}]"))?,
                    );
                }
                env["silicon"][key] = serde_json::to_value(commands)?;
            }
        }
        if let Some(isies) = document["isi"].as_mapping_mut() {
            for (name, isi) in isies {
                let name = name
                    .as_str()
                    .ok_or_else(|| anyhow!("isi names must be strings"))?;
                for key in ["model", "primary_send_mode", "session_type"] {
                    evaluate_field(isi, key, &env, &home, name)?;
                }
                for key in ["sticky", "archive_on_end"] {
                    if let Some(source) = isi[key].as_str() {
                        let result = crate::eval::evaluate(source, &env, &home, name)?;
                        isi[key] = Yaml::Bool(result.parse::<bool>().with_context(|| {
                            format!("isi.{name}.{key} must evaluate to true or false")
                        })?);
                    }
                }
            }
        }
        if let Some(access) = document["access"].as_mapping_mut() {
            for (name, targets) in access {
                let name = name
                    .as_str()
                    .ok_or_else(|| anyhow!("access names must be strings"))?;
                if let Some(targets) = targets.as_sequence_mut() {
                    for target in targets {
                        if let Some(source) = target.as_str() {
                            *target =
                                Yaml::String(crate::eval::evaluate(source, &env, &home, name)?);
                        }
                    }
                }
            }
        }
        let mut config: Self =
            serde_yaml::from_value(document).context("invalid silicon schema")?;
        config.path = path;
        config.home = home;
        config.warnings = warnings;
        config.validate()?;
        Ok(config)
    }

    fn validate(&mut self) -> Result<()> {
        let id = required(&self.silicon.id, "silicon.id")?;
        let (local, org) = id
            .split_once(':')
            .ok_or_else(|| anyhow!("silicon.id must be local-id:org-id"))?;
        if !host_label(local) || !host_label(org) {
            bail!("silicon.id must contain two DNS labels separated by one colon");
        }
        required(&self.silicon.token, "silicon.token")?;
        required(&self.silicon.timezone, "silicon.timezone")?
            .parse::<Tz>()
            .context("silicon.timezone must be an IANA timezone")?;
        validate_providers(&self.silicon.inference_providers)?;
        for (name, commands) in [
            ("login", &self.silicon.login),
            ("webhook", &self.silicon.webhook),
        ] {
            for command in commands {
                let command = command.trim().strip_prefix('!').unwrap_or(command).trim();
                if command.is_empty() || command.contains(['\n', '\0']) {
                    bail!("silicon.{name} commands must be nonempty single lines");
                }
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
            if let Some(sticky) = isi.sticky {
                let legacy = if sticky { "global" } else { "session" };
                if isi
                    .primary_send_mode
                    .as_deref()
                    .is_some_and(|mode| mode != legacy)
                {
                    bail!("isi.{name}.sticky conflicts with primary_send_mode");
                }
                isi.primary_send_mode = Some(legacy.into());
                self.warnings.push(format!(
                    "isi.{name}.sticky is legacy; use primary_send_mode: {legacy}"
                ));
            }
            if let Some(archive) = isi.archive_on_end {
                let legacy = if archive { "ephemeral" } else { "persistent" };
                if isi
                    .session_type
                    .as_deref()
                    .is_some_and(|kind| kind != legacy)
                {
                    bail!("isi.{name}.archive_on_end conflicts with session_type");
                }
                isi.session_type = Some(legacy.into());
                self.warnings.push(format!(
                    "isi.{name}.archive_on_end is legacy; use session_type: {legacy}"
                ));
            }
            if !matches!(
                required(
                    &isi.primary_send_mode,
                    &format!("isi.{name}.primary_send_mode")
                )?,
                "global" | "session"
            ) {
                bail!("isi.{name}.primary_send_mode must be global or session");
            }
            if !matches!(
                required(&isi.session_type, &format!("isi.{name}.session_type"))?,
                "persistent" | "ephemeral"
            ) {
                bail!("isi.{name}.session_type must be persistent or ephemeral");
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

fn validate_expressions(document: &Yaml) -> Result<()> {
    for (key, value) in document.as_mapping().unwrap() {
        crate::eval::validate(key)?;
        if key.as_str() == Some("silicon") {
            let mut silicon = value.clone();
            for key in ["login", "webhook"] {
                if let Some(commands) = silicon
                    .as_mapping_mut()
                    .and_then(|map| map.remove(Yaml::String(key.into())))
                {
                    let commands = commands
                        .as_sequence()
                        .ok_or_else(|| anyhow!("silicon.{key} must be a list"))?;
                    for (index, command) in commands.iter().enumerate() {
                        let command = command
                            .as_str()
                            .ok_or_else(|| anyhow!("silicon.{key}[{index}] must be a string"))?;
                        crate::eval::validate_app_command(command)
                            .with_context(|| format!("silicon.{key}[{index}]"))?;
                    }
                }
            }
            crate::eval::validate(&silicon)?;
        } else {
            crate::eval::validate(value)?;
        }
    }
    Ok(())
}

fn evaluate_field(object: &mut Yaml, key: &str, env: &Json, home: &Path, isi: &str) -> Result<()> {
    if let Some(source) = object[key].as_str() {
        object[key] = Yaml::String(
            crate::eval::evaluate(source, env, home, isi)
                .with_context(|| format!("evaluate {isi}.{key}"))?,
        );
    }
    Ok(())
}

fn evaluate_provider_values(value: &mut Yaml, env: &Json, home: &Path) -> Result<()> {
    match value {
        Yaml::String(source) => *source = crate::eval::evaluate(source, env, home, "interpreter")?,
        Yaml::Sequence(values) => {
            for value in values {
                evaluate_provider_values(value, env, home)?;
            }
        }
        _ => {}
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

fn host_label(value: &str) -> bool {
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
        anyhow!("silicon.inference_providers must be a list (lists may be nested)")
    })?;
    if values.is_empty() {
        bail!("silicon.inference_providers cannot be empty");
    }
    for value in values {
        match value {
            Yaml::Sequence(_) => validate_providers(value)?,
            Yaml::String(s) if !s.trim().is_empty() && s.trim() != "except" => {},
            _ => bail!("inference_providers entries must be provider names, 'except provider', or nested lists"),
        }
    }
    Ok(())
}

fn fields<'a>(value: &'a Yaml, path: &str, allowed: &[&str]) -> Result<&'a Mapping> {
    let map = value
        .as_mapping()
        .ok_or_else(|| anyhow!("{path} must be a mapping"))?;
    for key in map.keys() {
        if !key.as_str().is_some_and(|k| allowed.contains(&k)) {
            bail!("unknown field in {path}: {key:?}");
        }
    }
    Ok(map)
}

fn nonempty_scalar(value: &Yaml, path: &str) -> Result<()> {
    if value.as_str().is_some_and(|s| !s.is_empty()) || value.is_number() || value.is_bool() {
        return Ok(());
    }
    bail!("{path} must be a nonempty scalar")
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
    bail!("{path} must be a positive interval of at most ten years, e.g. 30min, or an expression")
}

fn nonempty_string(value: &Yaml, path: &str) -> Result<()> {
    if value.as_str().is_some_and(|s| !s.is_empty()) {
        return Ok(());
    }
    bail!("{path} must be a nonempty string")
}

fn validate_isi_blocks(name: &str, isi: &Isi) -> Result<()> {
    let path = format!("isi.{name}.dna");
    let dna = isi
        .dna
        .as_ref()
        .ok_or_else(|| anyhow!("{path} is required"))?;
    fields(dna, &path, &["assemble", "next_refresh"])?;
    let assemble = dna["assemble"]
        .as_sequence()
        .ok_or_else(|| anyhow!("{path}.assemble must be a list"))?;
    for value in assemble {
        if !value.as_str().is_some_and(|source| !source.is_empty()) {
            bail!("{path}.assemble entries must be nonempty paths or shell expressions");
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
            bail!("{path}.min_new_messages must be a positive integer or expression");
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
                    let isi = isies
                        .get(target)
                        .ok_or_else(|| anyhow!("flow sends to unknown isi {target}"))?;
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
        bail!("config must be a YAML mapping");
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
        _ => bail!("{path} must be steps or an expression"),
    };
    let mut output = Vec::new();
    for (index, node) in nodes.into_iter().enumerate() {
        let step_path = format!("{path}[{index}]");
        let Node::Map(mut entries) = node else {
            if let Node::Scalar(Yaml::String(expression)) = node {
                output.push(Yaml::String(expression));
                continue;
            }
            bail!("{step_path} must be a step mapping or an expression");
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
                bail!("{step_path}.{action} must be a mapping");
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

    #[test]
    fn parser_keeps_shell_commands_and_order_without_rewriting_templates() {
        let source = include_str!("../stemcell/silicon/silicon.yaml");
        let mut warnings = Vec::new();
        let config = parse_document(source, &mut warnings).unwrap();
        assert_eq!(config["silicon"]["SILICON_HOME"].as_str(), Some("! pwd"));
        assert_eq!(config["silicon"]["login"][0].as_str(), Some("! dm"));
        assert_eq!(
            config["isi"]["intuit"]["dna"]["assemble"][2].as_str(),
            Some("! ./contacts.sh !>> \"You have no contacts\"")
        );
        assert_eq!(config["flow"].as_sequence().unwrap().len(), 3);
        assert_eq!(
            config["flow"][0]["if"]["then"][0]["var"]["name"].as_str(),
            Some("new_message")
        );
        assert!(config["flow"][0]["if"]["then"][4]["else"].is_sequence());
        assert!(!warnings.is_empty());
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
    fn load_resolves_home_and_validates_template_and_modes() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("silicon.yaml");
        let source = "silicon:\n  id: test:org\n  token: token\n  timezone: UTC\n  SILICON_HOME: ! pwd\n  inference_providers: [all-available-providers]\nisi:\n  worker:\n    model: code\n    sticky: false\n    archive_on_end: false\n    dna: {assemble: [], next_refresh: 30min}\naccess: {worker: []}\nflow: []\n";
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
        fs::write(&path, source.replace("id: test:org", "id: ...")).unwrap();
        assert!(Config::load(&path)
            .unwrap_err()
            .to_string()
            .contains("template placeholder"));
        fs::write(
            &path,
            source.replace("sticky: false", "primary_send_mode: invalid"),
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
        let webhook = format!("{{missing.command}} !>> ! {app} --zone \"{{silicon.timezone}}\"");
        let source = format!("silicon:\n  id: test:org\n  token: token\n  timezone: UTC\n  SILICON_HOME: ! pwd\n  inference_providers: [all-available-providers]\n  login: [{}]\n  webhook: [{}]\nisi:\n  worker:\n    model: code\n    primary_send_mode: global\n    session_type: persistent\n    dna: {{assemble: [], next_refresh: 30min}}\naccess: {{worker: []}}\nflow: []\n", serde_json::to_string(&login).unwrap(), serde_json::to_string(&webhook).unwrap());
        fs::write(&path, &source).unwrap();
        let config = Config::load(&path).unwrap();
        assert_eq!(
            config.silicon.login,
            [format!("{app} --title \"hello test:org\" --literal '!>>'")]
        );
        assert_eq!(config.silicon.webhook, [format!("{app} --zone \"UTC\"")]);
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
                app.to_string(),
                format!("{app} --title \"hello test:org\" --literal '!>>'")
            ]
        );
        assert!(!temp.path().join("app-was-invoked").exists());
    }
}
