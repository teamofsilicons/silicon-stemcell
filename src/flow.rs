//! Ordered event flows. Consecutive `if`s all run; trailing `else` means none matched.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value as Json};
use serde_yaml::{Mapping, Value as Yaml};
use std::path::Path;

fn get<'a>(map: &'a Mapping, name: &str) -> Option<&'a Yaml> {
    map.get(Yaml::String(name.into()))
}
fn required<'a>(map: &'a Mapping, name: &str) -> Result<&'a Yaml> {
    get(map, name).ok_or_else(|| anyhow!("missing {name}"))
}

fn operation(step: &Yaml) -> Result<(&str, &Yaml, Option<&Yaml>)> {
    let map = step
        .as_mapping()
        .ok_or_else(|| anyhow!("flow step must be an object"))?;
    let mut operations = map.iter().filter(|(key, _)| key.as_str() != Some("catch"));
    let (name, body) = operations
        .next()
        .ok_or_else(|| anyhow!("empty flow step"))?;
    if operations.next().is_some() {
        bail!("flow step must contain exactly one operation");
    }
    let name = name
        .as_str()
        .ok_or_else(|| anyhow!("flow operation must be a string"))?;
    let inner_catch = body.as_mapping().and_then(|body| get(body, "catch"));
    let outer_catch = get(map, "catch");
    if inner_catch.is_some() && outer_catch.is_some() {
        bail!("catch cannot be specified twice");
    }
    Ok((name, body, inner_catch.or(outer_catch)))
}

fn steps(value: &Yaml) -> Result<&[Yaml]> {
    match value {
        Yaml::Sequence(items) => Ok(items),
        Yaml::Mapping(_) | Yaml::String(_) | Yaml::Tagged(_) => Ok(std::slice::from_ref(value)),
        _ => bail!("flow branch must be an operation, a list of operations, or an expression"),
    }
}

fn string_field(value: &Yaml) -> Result<()> {
    match value {
        Yaml::String(_) | Yaml::Number(_) | Yaml::Bool(_) | Yaml::Tagged(_) => {
            crate::eval::validate(value)
        }
        _ => bail!("expected a string expression"),
    }
}

/// Validate structure and every CEL expression, including unselected branches/catches.
pub fn validate(flow: &Yaml) -> Result<()> {
    let mut chain = false;
    for (index, step) in steps(flow)?.iter().enumerate() {
        let result = (|| {
            if matches!(step, Yaml::String(_) | Yaml::Tagged(_)) {
                chain = false;
                return crate::eval::validate(step);
            }
            let (name, body, catch) = operation(step)?;
            if name == "else" {
                if !chain {
                    bail!("else requires a preceding if chain");
                }
                chain = false;
                validate(body)?;
            } else {
                chain = name == "if";
                let map = body
                    .as_mapping()
                    .ok_or_else(|| anyhow!("{name} must contain an object"))?;
                let fields: &[&str] = match name {
                    "if" => &["condition", "then", "else", "catch"],
                    "var" => &["name", "value", "catch"],
                    "send" => &["isi", "session_id", "message", "catch"],
                    "log" => &["message", "catch"],
                    _ => bail!("unknown flow operation: {name}"),
                };
                for key in map.keys() {
                    if !key.as_str().is_some_and(|key| fields.contains(&key)) {
                        bail!("unknown field in {name}: {key:?}");
                    }
                }
                match name {
                    "if" => {
                        string_field(required(map, "condition")?)?;
                        validate(required(map, "then")?)?;
                        if let Some(branch) = get(map, "else") {
                            validate(branch)?;
                            chain = false;
                        }
                    }
                    "var" => {
                        string_field(required(map, "name")?)?;
                        crate::eval::validate(required(map, "value")?)?;
                    }
                    "send" => {
                        string_field(required(map, "isi")?)?;
                        string_field(required(map, "message")?)?;
                        if let Some(session) = get(map, "session_id") {
                            string_field(session)?;
                        }
                    }
                    "log" => string_field(required(map, "message")?)?,
                    _ => unreachable!(),
                }
            }
            if let Some(catch) = catch.filter(|value| !value.is_null()) {
                validate(catch)?;
            }
            Ok(())
        })();
        result.with_context(|| format!("flow step {index}"))?;
    }
    Ok(())
}

fn render(value: &Yaml, env: &Json, home: &Path, origin: &str) -> Result<String> {
    match value {
        Yaml::String(source) => crate::eval::evaluate(source, env, home, origin),
        Yaml::Tagged(tag) => render(
            &Yaml::String(format!(
                "! {}",
                tag.value
                    .as_str()
                    .ok_or_else(|| anyhow!("Bash tag must be a string"))?
            )),
            env,
            home,
            origin,
        ),
        Yaml::Bool(value) => Ok(value.to_string()),
        Yaml::Number(value) => Ok(value.to_string()),
        _ => bail!("expected a string expression"),
    }
}

struct Runner<'a, F> {
    env: Json,
    home: &'a Path,
    origin: &'a str,
    send: F,
}

impl<F: FnMut(&str, &str, Option<&str>) -> Result<()>> Runner<'_, F> {
    fn run(&mut self, flow: &Yaml) -> Result<()> {
        let mut chain: Option<bool> = None;
        for step in steps(flow)? {
            if matches!(step, Yaml::String(_) | Yaml::Tagged(_)) {
                chain = None;
                let result = (|| {
                    let expanded = crate::eval::value(step, &self.env, self.home, self.origin)?;
                    if !expanded.is_array() && !expanded.is_object() {
                        bail!("flow expression must produce an operation or list");
                    }
                    let expanded = serde_yaml::to_value(expanded)?;
                    validate(&expanded)?;
                    self.run(&expanded)
                })();
                if let Err(error) = result {
                    crate::log_line(
                        self.home,
                        "error",
                        self.origin,
                        &format!("flow expression: {error:#}"),
                    )?;
                }
                continue;
            }
            let (name, body, catch) = operation(step)?;
            if name == "else" {
                if chain.take() == Some(false) {
                    self.run(body)?;
                }
                continue;
            }
            if name != "if" {
                chain = None;
            }
            crate::log_line(self.home, "flow", self.origin, name)?;
            match self.step(name, body) {
                Ok(matched) if name == "if" => {
                    chain = Some(chain.unwrap_or(false) || matched);
                    if body
                        .as_mapping()
                        .is_some_and(|map| get(map, "else").is_some())
                    {
                        chain = None;
                    }
                }
                Ok(_) => {}
                Err(error) => {
                    if name == "if" {
                        chain = Some(chain.unwrap_or(false));
                    }
                    crate::log_line(
                        self.home,
                        "error",
                        self.origin,
                        &format!("{name}: {error:#}"),
                    )?;
                    if let Some(catch) = catch.filter(|value| !value.is_null()) {
                        let previous = self
                            .env
                            .as_object_mut()
                            .unwrap()
                            .insert("error".into(), json!(format!("{error:#}")));
                        let result = self.run(catch);
                        if let Some(previous) = previous {
                            self.env["error"] = previous;
                        } else {
                            self.env.as_object_mut().unwrap().remove("error");
                        }
                        result?;
                    }
                }
            }
        }
        Ok(())
    }

    fn field(&self, map: &Mapping, key: &str) -> Result<String> {
        render(required(map, key)?, &self.env, self.home, self.origin)
    }

    fn step(&mut self, name: &str, body: &Yaml) -> Result<bool> {
        let map = body
            .as_mapping()
            .ok_or_else(|| anyhow!("{name} must be an object"))?;
        match name {
            "if" => {
                let condition = self.field(map, "condition")?;
                let matched = match condition.trim() {
                    "true" => true,
                    "false" => false,
                    _ => bail!("if.condition must evaluate to true or false, got {condition:?}"),
                };
                if matched {
                    self.run(required(map, "then")?)?;
                } else if let Some(branch) = get(map, "else") {
                    self.run(branch)?;
                }
                return Ok(matched);
            }
            "var" => {
                let name = self.field(map, "name")?;
                if name.is_empty() {
                    bail!("var.name must not be empty");
                }
                let value =
                    crate::eval::value(required(map, "value")?, &self.env, self.home, self.origin)?;
                self.env["var"][name] = value;
            }
            "send" => {
                let target = self.field(map, "isi")?;
                let message = self.field(map, "message")?;
                let session = get(map, "session_id")
                    .map(|value| render(value, &self.env, self.home, self.origin))
                    .transpose()?;
                if target.is_empty() || session.as_deref() == Some("") {
                    bail!("send target/session must not be empty");
                }
                (self.send)(&target, &message, session.as_deref())?;
                crate::log_line(
                    self.home,
                    "send",
                    self.origin,
                    &format!("{target}: {message}"),
                )?;
            }
            "log" => crate::log_line(
                self.home,
                "runtime",
                self.origin,
                &self.field(map, "message")?,
            )?,
            _ => bail!("unknown flow operation: {name}"),
        }
        Ok(false)
    }
}

/// The callback must deliver immediately. It owns access/session validation and turn tracking.
/// Return after every flow action/catch completes; provider turn completion is separate.
pub fn execute<F>(flow: &Yaml, mut env: Json, home: &Path, origin: &str, send: F) -> Result<Json>
where
    F: FnMut(&str, &str, Option<&str>) -> Result<()>,
{
    validate(flow)?;
    let object = env
        .as_object_mut()
        .ok_or_else(|| anyhow!("flow environment must be an object"))?;
    object.insert("var".into(), json!({}));
    object.remove("error");
    let mut runner = Runner {
        env,
        home,
        origin,
        send,
    };
    runner.run(flow)?;
    Ok(runner.env["var"].take())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flows_keep_json_variables_run_all_matching_ifs_scope_catches_and_continue() {
        let flow: Yaml = serde_yaml::from_str(
            r#"
- var: {name: data, value: {n: '{1 + 2}'}}
- if:
    condition: '{var.data.n == 3}'
    then:
      - send: {isi: a, message: '{var.data.n}'}
- if:
    condition: '{true}'
    then:
      - send: {isi: b, message: also-matched}
- if:
    condition: '{false}'
    then: []
- else:
    - send: {isi: wrong, message: wrong}
- send:
    isi: broken
    message: attempt
    catch:
      - var: {name: outer, value: '{error}'}
      - send:
          isi: broken
          message: nested
          catch:
            - var: {name: inner, value: '{error}'}
      - var: {name: restored, value: '{error}'}
- var: {name: leaked, value: '{error}'}
- var: {name: after, value: '{var.data.n * 2}'}
- send: {isi: a, session_id: job, message: '{var.after}'}
"#,
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let mut sent = Vec::new();
        let vars = execute(
            &flow,
            json!({}),
            dir.path(),
            "interpreter",
            |target, message, session| {
                if target == "broken" {
                    bail!("failed {message}");
                }
                sent.push((
                    target.to_owned(),
                    message.to_owned(),
                    session.map(str::to_owned),
                ));
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(
            sent,
            vec![
                ("a".into(), "3".into(), None),
                ("b".into(), "also-matched".into(), None),
                ("a".into(), "6".into(), Some("job".into()))
            ]
        );
        assert_eq!(vars["outer"], "failed attempt");
        assert_eq!(vars["inner"], "failed nested");
        assert_eq!(vars["restored"], vars["outer"]);
        assert!(vars.get("leaked").is_none());
    }

    #[test]
    fn compile_rejects_invalid_unselected_branches_and_malformed_steps() {
        for source in [
            "- else: []",
            "- send: {isi: a}",
            "- log: {message: ok, typo: value}",
            "- if: {condition: false, then: [{log: {message: '{1 + }'}}]}",
        ] {
            assert!(
                validate(&serde_yaml::from_str(source).unwrap()).is_err(),
                "{source}"
            );
        }
    }

    #[test]
    fn flow_and_branch_expressions_produce_operations() {
        let dir = tempfile::tempdir().unwrap();
        let flow = Yaml::String("{[{'var': {'name': 'answer', 'value': 42}}]}".into());
        let vars = execute(
            &flow,
            json!({}),
            dir.path(),
            "interpreter",
            |_, _, _| Ok(()),
        )
        .unwrap();
        assert_eq!(vars["answer"], 42);
    }
}
