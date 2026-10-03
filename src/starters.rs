//! Starter owns downloading, version verification and safe archive extraction.
//! This module only resolves the downloaded definitions into ordinary Silicon YAML.
use anyhow::{bail, Context, Result};
use serde_yaml::{Mapping, Value as Yaml};
use std::{
    collections::{BTreeMap, HashSet},
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
}

impl Resolver {
    pub(crate) fn new(home: &Path, refresh: bool) -> Self {
        Self {
            home: home.into(),
            refresh,
            downloaded: HashSet::new(),
            files: Vec::new(),
            functions: BTreeMap::new(),
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

    fn download(&self, reference: &Reference, target: &Path) -> Result<()> {
        let binary = std::env::var_os("SILICON_STARTER").unwrap_or_else(|| "starter".into());
        let run = || {
            let mut command = crate::command(&binary, &self.home);
            command
                .args(["download", &reference.spec, "--dir"])
                .arg(target);
            crate::process::output(&mut command, crate::process::Limit::Install)
        };
        let output = match run() {
            Err(error)
                if error.kind() == std::io::ErrorKind::NotFound
                    && std::env::var_os("SILICON_STARTER").is_none() =>
            {
                crate::apps::install_at(&self.home, "starter")?;
                run().context("start Starter after installing it with Honeycomb")?
            }
            result => result.context("start Starter CLI")?,
        };
        if !output.status.success() {
            return Err(crate::failure::command(
                &self.home,
                &format!("starter download {}", reference.spec),
                &output,
                &[],
            ));
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

#[cfg(test)]
mod tests {
    use super::*;
    fn cache(home: &Path, kind: &str, name: &str, filename: &str, source: &str) {
        let target = home
            .join(".fromstarter")
            .join(kind)
            .join(name)
            .join(filename);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(target, source).unwrap();
    }

    #[test]
    fn resolves_genes_isi_overrides_and_nested_function_calls_from_cache() -> Result<()> {
        let dir = tempfile::tempdir()?;
        cache(
            dir.path(),
            "gene",
            "careful",
            "careful.md",
            "Work carefully.",
        );
        cache(dir.path(), "isi", "researcher", "isi.yaml", "isi:\n  researcher:\n    model: fast\n    apps: [search]\n    dna: {assemble: [prompts/research.md, 'starter:gene:careful'], next_refresh: 60}\n");
        cache(dir.path(), "function", "hello", "function.yaml", "functions:\n  greeting:\n    params: [name]\n    do:\n      - call: {function: 'starter:function:decorate'}\n      - return: 'Hello {args.name}'\n");
        cache(
            dir.path(),
            "function",
            "decorate",
            "function.yaml",
            "functions:\n  decorate: {do: [{return: true}]}\n",
        );
        let mut resolver = Resolver::new(dir.path(), false);
        let mut isi: Yaml = serde_yaml::from_str("starter:isi:researcher:\n  model: smart\n  dna: {next_refresh: 5}\nlocal:\n  dna: {assemble: ['starter:careful']}\n")?;
        resolver.resolve_isi(&mut isi)?;
        assert_eq!(isi["researcher"]["model"], "smart");
        assert_eq!(isi["researcher"]["apps"][0], "search");
        assert_eq!(isi["researcher"]["dna"]["next_refresh"], 5);
        assert!(isi["researcher"]["dna"]["assemble"][0]
            .as_str()
            .unwrap()
            .ends_with(".fromstarter/isi/researcher/prompts/research.md"));
        assert!(isi["local"]["dna"]["assemble"][0]
            .as_str()
            .unwrap()
            .ends_with(".fromstarter/gene/careful/careful.md"));
        let mut flow: Yaml = serde_yaml::from_str("- if:\n    condition: true\n    then:\n      - call: {function: 'starter:function:hello', args: {name: world}}\n")?;
        let mut functions = Yaml::Null;
        resolver.resolve_program(&mut flow, &mut functions)?;
        crate::flow::validate_with_functions(&flow, &functions)?;
        assert_eq!(functions["starter:function:hello"]["params"][0], "name");
        assert!(functions.get("starter:function:decorate").is_some());
        assert_eq!(resolver.downloaded.len(), 4);
        assert_eq!(resolver.cached_files().len(), 4);
        Ok(())
    }

    #[test]
    fn rejects_wrong_kinds_traversal_and_duplicate_isi_names() -> Result<()> {
        use std::os::unix::fs::symlink;
        for source in [
            "starter:isi:worker",
            "starter:gene:../escape",
            "starter:gene:x@bad",
            "starter:gene:",
        ] {
            assert!(Reference::parse(source, "gene").is_err(), "{source}");
        }
        let dir = tempfile::tempdir()?;
        cache(
            dir.path(),
            "isi",
            "worker",
            "isi.yaml",
            "isi: {worker: {model: fast}}\n",
        );
        let mut resolver = Resolver::new(dir.path(), false);
        let mut isi = serde_yaml::from_str("starter:isi:worker: default\nworker: {}\n")?;
        assert!(resolver
            .resolve_isi(&mut isi)
            .unwrap_err()
            .to_string()
            .contains("duplicate ISI"));
        let linked = tempfile::tempdir()?;
        symlink(
            dir.path().join(".fromstarter"),
            linked.path().join(".fromstarter"),
        )?;
        let mut isi = serde_yaml::from_str("starter:isi:worker: default")?;
        assert!(Resolver::new(linked.path(), true)
            .resolve_isi(&mut isi)
            .unwrap_err()
            .to_string()
            .contains("ordinary directories"));
        Ok(())
    }

    #[test]
    fn config_load_compiles_remote_isi_and_reloads_cached_function_edits() -> Result<()> {
        let dir = tempfile::tempdir()?;
        cache(
            dir.path(),
            "gene",
            "careful",
            "careful.md",
            "Check your sources.",
        );
        cache(
            dir.path(),
            "isi",
            "researcher",
            "isi.yaml",
            r#"isi:
  researcher:
    model: ! printf 'fast'
    primary_send_mode: global
    session_type: persistent
    apps: [search]
    dna:
      assemble: ['missing.md !>> prompts/default.md', 'starter:gene:careful !>> "Unavailable"']
      next_refresh: 30min
"#,
        );
        cache(
            dir.path(),
            "isi",
            "researcher",
            "prompts/default.md",
            "Research carefully.",
        );
        cache(dir.path(), "function", "hello", "function.yaml", "functions:\n  greeting:\n    params: [name]\n    do:\n      - call: {function: 'starter:function:decorate'}\n      - return: before\n");
        cache(
            dir.path(),
            "function",
            "decorate",
            "function.yaml",
            "functions:\n  decorate: {do: [{return: true}]}\n",
        );
        let path = dir.path().join("silicon.yaml");
        fs::write(
            &path,
            r#"silicon:
  id: si:test
  org_id: org
  token: token
  timezone: UTC
  SILICON_HOME: .
  inference_providers: [all-available-providers]
isi:
  starter:isi:researcher:
    session_type: ephemeral
    dna: {next_refresh: 5}
access: {researcher: []}
functions: starter:function:hello
flow:
  - starter:function:hello:
      args: {name: world}
      then: [{log: {message: '{self.result}'}}]
"#,
        )?;
        let cfg = crate::config::Config::load(&path)?;
        assert_eq!(cfg.isi["researcher"].model.as_deref(), Some("fast"));
        assert_eq!(
            cfg.isi["researcher"].session_type.as_deref(),
            Some("ephemeral")
        );
        assert!(cfg.managed_apps().contains(&"search".into()));
        let dna = cfg.isi["researcher"].dna.as_ref().unwrap();
        assert_eq!(dna["next_refresh"], 5);
        let source = dna["assemble"][0].as_str().unwrap();
        assert_eq!(
            crate::eval::dna(source, &serde_json::json!({}), dir.path(), "researcher")?,
            "Research carefully."
        );
        assert_eq!(
            crate::eval::dna(
                dna["assemble"][1].as_str().unwrap(),
                &serde_json::json!({}),
                dir.path(),
                "researcher"
            )?,
            "Check your sources."
        );
        let (_, functions) = cfg.load_program()?;
        assert_eq!(functions["greeting"]["do"][1]["return"], "before");
        assert!(functions.get("starter:function:decorate").is_some());
        cache(
            dir.path(),
            "function",
            "hello",
            "function.yaml",
            "functions:\n  greeting:\n    params: [name]\n    do:\n      - return: after-edit\n",
        );
        let (_, functions) = cfg.load_program()?;
        assert_eq!(functions["greeting"]["do"][0]["return"], "after-edit");
        assert_eq!(
            functions["starter:function:hello"]["do"][0]["return"],
            "after-edit"
        );
        let source = fs::read_to_string(&path)?.replace(
            "  starter:isi:researcher:\n    session_type: ephemeral\n    dna: {next_refresh: 5}",
            "  starter:isi:researcher: default",
        );
        fs::write(&path, source)?;
        let installed = crate::config::set_app(&path, "researcher", "dm", true)?;
        assert_eq!(installed.isi["researcher"].apps, ["search", "dm"]);
        assert!(fs::read_to_string(&path)?.contains("starter:isi:researcher:"));
        let removed = crate::config::set_app(&path, "researcher", "search", false)?;
        assert_eq!(removed.isi["researcher"].apps, ["dm"]);
        Ok(())
    }

    #[test]
    fn function_references_do_not_rewrite_argument_or_return_data() -> Result<()> {
        let mut steps: Yaml = serde_yaml::from_str(
            r#"
- call:
    function: recursive
    args: {data: {call: {function: recursive}}}
    then: [{call: {function: recursive}}]
- return:
    then: [{call: {function: recursive}}]
    catch: ['starter:gene:literal-data']
"#,
        )?;
        let args = steps[0]["call"]["args"].clone();
        let returned = steps[1]["return"].clone();
        rename_recursive_calls(&mut steps, "recursive", "starter:function:recursive");
        assert_eq!(steps[0]["call"]["function"], "starter:function:recursive");
        assert_eq!(
            steps[0]["call"]["then"][0]["call"]["function"],
            "starter:function:recursive"
        );
        assert_eq!(steps[0]["call"]["args"], args);
        assert_eq!(steps[1]["return"], returned);
        let dir = tempfile::tempdir()?;
        let mut data = Yaml::Sequence(vec![steps[1].clone()]);
        Resolver::new(dir.path(), false).steps(&mut data, 0)?;
        assert_eq!(data[0]["return"], returned);
        Ok(())
    }

    #[test]
    fn reconnect_fetches_once_per_block_and_failed_download_preserves_cache() -> Result<()> {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir()?;
        let bin = dir.path().join(".silicon/bin");
        fs::create_dir_all(&bin)?;
        let starter = bin.join("starter");
        fs::write(
            &starter,
            r##"#!/bin/sh
set -eu
[ "$1" = download ] && [ "$2" = gene:careful ] && [ "$3" = --dir ]
[ ! -e "$SILICON_HOME/fail" ] || exit 3
count=0
[ ! -e "$SILICON_HOME/count" ] || count=$(cat "$SILICON_HOME/count")
count=$((count + 1))
printf '%s' "$count" > "$SILICON_HOME/count"
mkdir -p "$4"
printf 'Revision %s' "$count" > "$4/careful.md"
"##,
        )?;
        fs::set_permissions(&starter, fs::Permissions::from_mode(0o700))?;
        let original: Yaml = serde_yaml::from_str(
            "a: {dna: {assemble: ['starter:gene:careful', 'starter:gene:careful']}}",
        )?;
        let cached = dir.path().join(".fromstarter/gene/careful/careful.md");
        Resolver::new(dir.path(), true).resolve_isi(&mut original.clone())?;
        assert_eq!(fs::read_to_string(&cached)?, "Revision 1");
        Resolver::new(dir.path(), false).resolve_isi(&mut original.clone())?;
        assert_eq!(fs::read_to_string(&cached)?, "Revision 1");
        Resolver::new(dir.path(), true).resolve_isi(&mut original.clone())?;
        assert_eq!(fs::read_to_string(&cached)?, "Revision 2");
        fs::write(dir.path().join("fail"), "")?;
        assert!(Resolver::new(dir.path(), true)
            .resolve_isi(&mut original.clone())
            .is_err());
        assert_eq!(fs::read_to_string(&cached)?, "Revision 2");
        Ok(())
    }
}
