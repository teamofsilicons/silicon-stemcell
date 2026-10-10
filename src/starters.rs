//! Starter owns downloading, version verification and safe archive extraction.
//! This module only resolves the downloaded definitions into ordinary Silicon YAML.
use anyhow::{bail, Context, Result};
use serde_yaml::{Mapping, Value as Yaml};
use std::{
    collections::{BTreeMap, HashSet},
    ffi::{OsStr, OsString},
    fs,
    path::{Path, PathBuf},
};

pub(crate) fn is_reference(source: &str) -> bool {
    source.starts_with("starter:")
}

struct Reference {
    kind: &'static str,
    name: String,
    spec: String,
    directory: String,
}

impl Reference {
    fn parse(source: &str, expected: &'static str) -> Result<Self> {
        let source = source
            .strip_prefix("starter:")
            .context("expected a starter: reference")?;
        let (id, version) = source
            .split_once('@')
            .map_or((source, None), |(id, version)| (id, Some(version)));
        let (kind, name) = id.split_once(':').unwrap_or((expected, id));
        if kind != expected {
            bail!("starter:{source} is a {kind} reference; expected a {expected}");
        }
        if name.is_empty()
            || name.len() > 128
            || !name
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        {
            bail!("invalid Starter {kind} name {name:?}; expected 1–128 lowercase letters, digits or hyphens");
        }
        if version.is_some_and(|v| {
            v != "latest" && (v.len() != 64 || !v.bytes().all(|b| b.is_ascii_hexdigit()))
        }) {
            bail!("Starter version must be latest or a full SHA-256 content hash");
        }
        let suffix = version
            .filter(|v| *v != "latest")
            .map(|v| format!("@{v}"))
            .unwrap_or_default();
        Ok(Self {
            kind: expected,
            name: name.into(),
            spec: format!("{expected}:{name}{suffix}"),
            directory: format!("{name}{suffix}"),
        })
    }
}

pub(crate) struct Resolver {
    home: PathBuf,
    refresh: bool,
    downloaded: HashSet<String>,
    files: Vec<PathBuf>,
    functions: BTreeMap<String, Yaml>,
    starter: Option<OsString>,
}

impl Resolver {
    pub(crate) fn new(home: &Path, refresh: bool) -> Self {
        Self {
            home: home.into(),
            refresh,
            downloaded: HashSet::new(),
            files: Vec::new(),
            functions: BTreeMap::new(),
            starter: None,
        }
    }

    pub(crate) fn cached_files(&self) -> &[PathBuf] {
        &self.files
    }

    fn block(&mut self, reference: &Reference) -> Result<PathBuf> {
        if self.downloaded.len() >= 256 && !self.downloaded.contains(&reference.spec) {
            bail!("a Silicon may reference at most 256 Starter blocks");
        }
        let root = self.home.join(".fromstarter");
        let parent = root.join(reference.kind);
        let target = parent.join(&reference.directory);
        for path in [&root, &parent, &target] {
            if fs::symlink_metadata(path).is_ok_and(|m| m.is_symlink() || !m.is_dir()) {
                bail!(
                    "Starter cache must contain ordinary directories: {}",
                    path.display()
                );
            }
        }
        if !self.downloaded.contains(&reference.spec) && (self.refresh || !target.is_dir()) {
            fs::create_dir_all(&parent)
                .with_context(|| format!("create Starter cache {}", parent.display()))?;
            let staging = parent.join(format!(".download-{}", uuid::Uuid::new_v4()));
            let result = self.download(reference, &staging).and_then(|()| {
                // Retain the old download until the CLI has verified the complete replacement.
                let old = parent.join(format!(".previous-{}", uuid::Uuid::new_v4()));
                let exists = target.exists();
                if exists {
                    fs::rename(&target, &old)?;
                }
                if let Err(error) = fs::rename(&staging, &target) {
                    if exists {
                        let _ = fs::rename(&old, &target);
                    }
                    return Err(error.into());
                }
                if exists {
                    fs::remove_dir_all(old)?;
                }
                Ok(())
            });
            if staging.exists() {
                let _ = fs::remove_dir_all(&staging);
            }
            result.with_context(|| format!("download starter:{}", reference.spec))?;
        }
        self.downloaded.insert(reference.spec.clone());
        Ok(target)
    }

    fn prepare_starter(
        &mut self,
        binary: OsString,
        explicit: bool,
        install: impl FnOnce(&Path) -> Result<()>,
    ) -> Result<()> {
        if self.starter.is_some() {
            return Ok(());
        }
        if let Some(reason) = starter_upgrade_reason(&self.home, &binary)? {
            if explicit {
                return Err(reason).context("SILICON_STARTER must point to Starter 0.3.0 or newer; explicit overrides are not replaced");
            }
            install(&self.home).with_context(|| {
                format!("{reason:#}; installing Starter 0.3.0 or newer with Silicon Apps")
            })?;
            if let Some(reason) = starter_upgrade_reason(&self.home, &binary)
                .context("check Starter after installing it with Silicon Apps")?
            {
                return Err(reason)
                    .context("Silicon Apps installation did not provide Starter 0.3.0 or newer");
            }
        }
        self.starter = Some(binary);
        Ok(())
    }

    fn download(&mut self, reference: &Reference, target: &Path) -> Result<()> {
        let override_binary = std::env::var_os("SILICON_STARTER");
        let explicit = override_binary.is_some();
        self.prepare_starter(
            override_binary.unwrap_or_else(|| "starter".into()),
            explicit,
            |home| crate::apps::install_at(home, "starter").map(|_| ()),
        )?;
        let binary = self.starter.as_ref().unwrap();
        let args = [
            "download",
            &reference.spec,
            "--dir",
            target
                .to_str()
                .context("Starter cache path must be UTF-8")?,
        ];
        let shown = crate::failure::argv(binary, &args);
        let output = crate::process::output(
            crate::command(binary, &self.home).args(args),
            crate::process::Limit::Install,
        )
        .map_err(|error| crate::failure::spawn(&self.home, &shown, &error))?;
        if !output.status.success() {
            return Err(crate::failure::command(&self.home, &shown, &output, &[]));
        }
        if !target.is_dir() {
            bail!("Starter download did not create {}", target.display());
        }
        crate::log_line(
            &self.home,
            "starter",
            "interpreter",
            &format!(
                "downloaded starter:{} into {}",
                reference.spec,
                target.display()
            ),
        )?;
        Ok(())
    }

    fn read(&mut self, path: &Path) -> Result<String> {
        // The CLI rejects archive symlinks; also reject manually replaced definition files.
        if fs::symlink_metadata(path).is_ok_and(|m| m.is_symlink()) {
            bail!(
                "Starter definition must not be a symlink: {}",
                path.display()
            );
        }
        let source = fs::read_to_string(path)
            .with_context(|| format!("read Starter definition {}", path.display()))?;
        if !self.files.contains(&path.to_owned()) {
            self.files.push(path.into());
        }
        Ok(source)
    }

    pub(crate) fn resolve_isi(&mut self, isi: &mut Yaml) -> Result<()> {
        let Some(entries) = isi.as_mapping() else {
            return Ok(());
        };
        let mut resolved = Mapping::new();
        for (key, value) in entries {
            let Some(name) = key.as_str() else {
                bail!("ISI names must be strings");
            };
            let (name, mut definition) = if is_reference(name) {
                let reference = Reference::parse(name, "isi")?;
                let definition = self.isi_definition(&reference)?;
                let mut definition = definition;
                match value {
                    Yaml::String(value) if value == "default" => {}
                    Yaml::Null => {}
                    Yaml::Mapping(_) => merge(&mut definition, value),
                    _ => bail!("{name} must be default or an ISI override mapping"),
                }
                (reference.name, definition)
            } else {
                (name.into(), value.clone())
            };
            self.genes(&mut definition)?;
            if resolved.insert(name.clone().into(), definition).is_some() {
                bail!("duplicate ISI {name} after resolving Starter references");
            }
        }
        *isi = Yaml::Mapping(resolved);
        Ok(())
    }

    fn isi_definition(&mut self, reference: &Reference) -> Result<Yaml> {
        let directory = self.block(reference)?;
        let path = directory.join("isi.yaml");
        let root = crate::config::parse_starter_isi(&self.read(&path)?)
            .with_context(|| format!("invalid Starter ISI {}", path.display()))?;
        let entries = root["isi"]
            .as_mapping()
            .context("Starter isi.yaml must contain an isi mapping")?;
        if entries.len() != 1 || !entries.contains_key(Yaml::String(reference.name.clone())) {
            bail!(
                "Starter isi.yaml must contain exactly the ISI {}",
                reference.name
            );
        }
        let mut definition = entries[&Yaml::String(reference.name.clone())].clone();
        if !definition.is_mapping() {
            bail!("Starter ISI {} must be a mapping", reference.name);
        }
        if let Some(assemble) = definition
            .get_mut("dna")
            .and_then(|dna| dna.get_mut("assemble"))
            .and_then(Yaml::as_sequence_mut)
        {
            for item in assemble {
                if let Some(source) = item.as_str() {
                    // Shell commands retain the documented SILICON_HOME working directory.
                    // Literal file entries refer to their supporting files in the archive.
                    let sources = crate::eval::dna_sources(source)?
                        .into_iter()
                        .map(|candidate| {
                            let candidate = candidate.trim();
                            if !is_reference(candidate)
                                && !candidate.starts_with(['!', '{', '\'', '"'])
                                && !Path::new(candidate).is_absolute()
                            {
                                directory.join(candidate).to_string_lossy().into_owned()
                            } else {
                                candidate.to_owned()
                            }
                        })
                        .collect::<Vec<_>>();
                    *item = sources.join(" !>> ").into();
                }
            }
        }
        Ok(definition)
    }

    fn genes(&mut self, definition: &mut Yaml) -> Result<()> {
        let Some(assemble) = definition
            .get_mut("dna")
            .and_then(|dna| dna.get_mut("assemble"))
            .and_then(Yaml::as_sequence_mut)
        else {
            return Ok(());
        };
        for item in assemble {
            let Some(source) = item.as_str() else {
                continue;
            };
            let sources = crate::eval::dna_sources(source)?;
            let mut resolved = Vec::new();
            let mut changed = false;
            for candidate in sources {
                if is_reference(candidate.trim()) {
                    let reference = Reference::parse(candidate.trim(), "gene")?;
                    let path = self
                        .block(&reference)?
                        .join(format!("{}.md", reference.name));
                    self.read(&path)?;
                    resolved.push(path.to_string_lossy().into_owned());
                    changed = true;
                } else {
                    resolved.push(candidate.to_owned());
                }
            }
            if changed {
                *item = resolved.join(" !>> ").into();
            }
        }
        Ok(())
    }

    /// A function import keeps its published name, just like a local definition file.
    pub(crate) fn function_source(&mut self, source: &str) -> Result<Yaml> {
        let reference = Reference::parse(source, "function")?;
        let path = self.block(&reference)?.join("function.yaml");
        let source = self.read(&path)?;
        let functions = crate::config::parse_starter_functions(&source)
            .with_context(|| format!("invalid Starter function {}", path.display()))?;
        if functions.as_mapping().is_none_or(|map| map.len() != 1) {
            bail!("Starter function.yaml must contain exactly one function");
        }
        Ok(functions)
    }

    /// Calls retain the full reference as their function name, avoiding local name collisions.
    fn function(&mut self, source: &str) -> Result<()> {
        if self.functions.contains_key(source) {
            return Ok(());
        }
        let functions = self.function_source(source)?;
        let (published_name, definition) = functions.as_mapping().unwrap().iter().next().unwrap();
        let mut definition = definition.clone();
        rename_recursive_calls(
            &mut definition["do"],
            published_name
                .as_str()
                .context("Starter function name must be a string")?,
            source,
        );
        self.functions.insert(source.into(), definition);
        Ok(())
    }

    pub(crate) fn resolve_program(&mut self, flow: &mut Yaml, functions: &mut Yaml) -> Result<()> {
        if functions.is_null() {
            *functions = Yaml::Mapping(Mapping::new());
        }
        let definitions = functions
            .as_mapping_mut()
            .context("functions must be resolved before Starter calls")?;
        self.steps(flow, 0)?;
        for definition in definitions.values_mut() {
            self.steps(&mut definition["do"], 0)?;
        }
        // A downloaded function may call another block (including itself).
        let mut completed = HashSet::new();
        while let Some(name) = self
            .functions
            .keys()
            .find(|name| !completed.contains(*name))
            .cloned()
        {
            completed.insert(name.clone());
            let mut definition = self.functions[&name].clone();
            self.steps(&mut definition["do"], 0)?;
            self.functions.insert(name, definition);
        }
        for (name, definition) in &self.functions {
            if definitions
                .insert(name.clone().into(), definition.clone())
                .is_some()
            {
                bail!("duplicate function {name} after resolving Starter references");
            }
        }
        Ok(())
    }

    fn steps(&mut self, steps: &mut Yaml, depth: usize) -> Result<()> {
        if depth > 64 {
            bail!("Starter flow exceeds 64 nested step lists");
        }
        if let Some(source) = steps
            .as_str()
            .filter(|source| is_reference(source))
            .map(str::to_owned)
        {
            self.function(&source)?;
            *steps = Yaml::Sequence(vec![call(&source, Yaml::Mapping(Mapping::new()))?]);
        }
        let Some(sequence) = steps.as_sequence_mut() else {
            return Ok(());
        };
        for step in sequence {
            if let Some(source) = step
                .as_str()
                .filter(|source| is_reference(source))
                .map(str::to_owned)
            {
                self.function(&source)?;
                *step = call(&source, Yaml::Mapping(Mapping::new()))?;
            }
            let Some(actions) = step.as_mapping_mut() else {
                continue;
            };
            let starter = actions.keys().find_map(|key| {
                key.as_str()
                    .filter(|key| is_reference(key))
                    .map(str::to_owned)
            });
            if let Some(source) = starter {
                if actions.len() != 1 {
                    bail!("Starter calls must be separate flow steps");
                }
                self.function(&source)?;
                let options = actions.remove(Yaml::String(source.clone())).unwrap();
                *step = call(&source, options)?;
            }
            let actions = step.as_mapping_mut().unwrap();
            for (action, payload) in actions {
                if action.as_str() == Some("return") {
                    continue;
                }
                if action.as_str() == Some("else") {
                    self.steps(payload, depth + 1)?;
                    continue;
                }
                let Some(fields) = payload.as_mapping_mut() else {
                    continue;
                };
                if action.as_str() == Some("call") {
                    if let Some(source) = fields
                        .get("function")
                        .and_then(Yaml::as_str)
                        .filter(|source| is_reference(source))
                    {
                        self.function(source)?;
                    }
                }
                for name in ["then", "else", "catch", "default"] {
                    if let Some(branch) = fields.get_mut(name) {
                        self.steps(branch, depth + 1)?;
                    }
                }
                if let Some(cases) = fields.get_mut("cases").and_then(Yaml::as_sequence_mut) {
                    for case in cases {
                        if let Some(branch) = case.get_mut("then") {
                            self.steps(branch, depth + 1)?;
                        }
                    }
                }
            }
        }
        Ok(())
    }
}

/// An actionable missing/old version can be repaired; other CLI failures stay diagnostic.
fn starter_upgrade_reason(home: &Path, binary: &OsStr) -> Result<Option<anyhow::Error>> {
    let shown = crate::failure::argv(binary, &["--version"]);
    let output = match crate::process::output(
        crate::command(binary, home).arg("--version"),
        crate::process::Limit::App,
    ) {
        Ok(output) => output,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(Some(crate::failure::spawn(home, &shown, &error)));
        }
        Err(error) => return Err(crate::failure::spawn(home, &shown, &error)),
    };
    if !output.status.success() {
        return Err(crate::failure::command(home, &shown, &output, &[]));
    }
    let version = std::str::from_utf8(&output.stdout)
        .ok()
        .and_then(|text| text.split_whitespace().last())
        .and_then(crate::update::version)
        .ok_or_else(|| {
            crate::failure::answer(
                home,
                &shown,
                "did not report a valid x.y.z Starter version",
                &output,
                &[],
            )
        })?;
    Ok((version < [0, 3, 0]).then(|| {
        crate::failure::answer(
            home,
            &shown,
            "reported a version older than Starter 0.3.0; reusable blocks require 0.3.0 or newer",
            &output,
            &[],
        )
    }))
}

fn call(source: &str, options: Yaml) -> Result<Yaml> {
    let mut options = match options {
        Yaml::Null => Mapping::new(),
        Yaml::String(value) if value == "default" => Mapping::new(),
        Yaml::Mapping(options) => options,
        _ => bail!("Starter flow call {source} expects an args/then/catch mapping"),
    };
    if options.contains_key("function") {
        bail!("Starter flow call cannot override its function reference");
    }
    options.insert("function".into(), source.into());
    Ok(Yaml::Mapping(Mapping::from_iter([(
        "call".into(),
        Yaml::Mapping(options),
    )])))
}

fn merge(default: &mut Yaml, overrides: &Yaml) {
    match (default, overrides) {
        (Yaml::Mapping(default), Yaml::Mapping(overrides)) => {
            for (key, value) in overrides {
                match default.get_mut(key) {
                    Some(default) => merge(default, value),
                    None => {
                        default.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        (default, value) => *default = value.clone(),
    }
}

fn rename_recursive_calls(value: &mut Yaml, from: &str, to: &str) {
    let Some(steps) = value.as_sequence_mut() else {
        return;
    };
    for step in steps {
        let Some(actions) = step.as_mapping_mut() else {
            continue;
        };
        for (action, payload) in actions {
            if action.as_str() == Some("return") {
                continue;
            }
            if action.as_str() == Some("else") {
                rename_recursive_calls(payload, from, to);
                continue;
            }
            let Some(fields) = payload.as_mapping_mut() else {
                continue;
            };
            if action.as_str() == Some("call")
                && fields.get("function").and_then(Yaml::as_str) == Some(from)
            {
                fields.insert("function".into(), to.into());
            }
            for name in ["then", "else", "catch", "default"] {
                if let Some(branch) = fields.get_mut(name) {
                    rename_recursive_calls(branch, from, to);
                }
            }
            if let Some(cases) = fields.get_mut("cases").and_then(Yaml::as_sequence_mut) {
                for case in cases {
                    if let Some(branch) = case.get_mut("then") {
                        rename_recursive_calls(branch, from, to);
                    }
                }
            }
        }
    }
}
