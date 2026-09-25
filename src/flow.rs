//! Ordered event flows. Consecutive `if`s all run; trailing `else` means none matched.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value as Json};
use serde_yaml::{Mapping, Value as Yaml};
use std::path::Path;

fn get<'a>(map: &'a Mapping, name: &str) -> Option<&'a Yaml> {
    map.get(Yaml::String(name.into()))
}
fn required<'a>(map: &'a Mapping, name: &str) -> Result<&'a Yaml> {
    get(map, name).ok_or_else(|| anyhow!("missing required field `{name}`"))
}

/// Flow sources include multi-line message templates; keep one `[flow]` entry readable.
/// `[error]` entries name their step with the whole source instead.
fn summarize(source: &str) -> String {
    let flat = source.split_whitespace().collect::<Vec<_>>().join(" ");
    const LIMIT: usize = 160;
    if flat.chars().count() <= LIMIT {
        flat
    } else {
        format!("{}…", flat.chars().take(LIMIT).collect::<String>())
    }
}

/// What the step about to run says in the configuration, so the log names the
/// branch or assignment rather than only its operation. These are unevaluated
/// sources on purpose: rendering here would run any `!` expression a second
/// time. Anything without a readable source logs as the bare operation.
fn describe(name: &str, body: &Yaml, shown: fn(&str) -> String) -> String {
    let Some(map) = body.as_mapping() else {
        return name.to_owned();
    };
    let source = |key: &str| get(map, key).and_then(Yaml::as_str).map(shown);
    match name {
        "if" => source("condition").map(|condition| format!("if {condition}")),
        "var" => source("name").map(|target| match source("value") {
            Some(value) => format!("var {target} = {value}"),
            None => format!("var {target}"),
        }),
        "send" => source("isi").map(|isi| match source("session_id") {
            Some(session) => format!("send {isi} session {session}"),
            None => format!("send {isi}"),
        }),
        _ => None,
    }
    .unwrap_or_else(|| name.to_owned())
}

fn operation(step: &Yaml) -> Result<(&str, &Yaml, Option<&Yaml>)> {
    let map = step
        .as_mapping()
        .ok_or_else(|| anyhow!("flow step must be an object, got {step:?}"))?;
    let operations = map
        .iter()
        .filter(|(key, _)| key.as_str() != Some("catch"))
        .collect::<Vec<_>>();
    let &[(name, body)] = operations.as_slice() else {
        bail!(
            "flow step must contain exactly one operation, got {:?}",
            operations.iter().map(|(key, _)| key).collect::<Vec<_>>()
        );
    };
    let name = name
        .as_str()
        .ok_or_else(|| anyhow!("flow operation must be a string, got {name:?}"))?;
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
        _ => bail!(
            "flow branch must be an operation, a list of operations, or an expression, got {value:?}"
        ),
    }
}

fn string_field(value: &Yaml) -> Result<()> {
    match value {
        Yaml::String(_) | Yaml::Number(_) | Yaml::Bool(_) | Yaml::Tagged(_) => {
            crate::eval::validate(value)
        }
        _ => bail!("expected a string expression, got {value:?}"),
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
                validate(body).context("else")?;
            } else {
                chain = name == "if";
                let map = body
                    .as_mapping()
                    .ok_or_else(|| anyhow!("{name} must contain an object, got {body:?}"))?;
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
                        string_field(required(map, "condition")?).context("if.condition")?;
                        validate(required(map, "then")?).context("if.then")?;
                        if let Some(branch) = get(map, "else") {
                            validate(branch).context("if.else")?;
                            chain = false;
                        }
                    }
                    "var" => {
                        string_field(required(map, "name")?).context("var.name")?;
                        crate::eval::validate(required(map, "value")?).context("var.value")?;
                    }
                    "send" => {
                        string_field(required(map, "isi")?).context("send.isi")?;
                        string_field(required(map, "message")?).context("send.message")?;
                        if let Some(session) = get(map, "session_id") {
                            string_field(session).context("send.session_id")?;
                        }
                    }
                    "log" => string_field(required(map, "message")?).context("log.message")?,
                    _ => unreachable!(),
                }
            }
            if let Some(catch) = catch.filter(|value| !value.is_null()) {
                validate(catch).context("catch")?;
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
                tag.value.as_str().ok_or_else(|| {
                    anyhow!("Bash tag {} must be a string, got {:?}", tag.tag, tag.value)
                })?
            )),
            env,
            home,
            origin,
        ),
        Yaml::Bool(value) => Ok(value.to_string()),
        Yaml::Number(value) => Ok(value.to_string()),
        other => bail!("expected a string expression, got {other:?}"),
    }
}

struct Runner<'a, F> {
    env: Json,
    home: &'a Path,
    origin: &'a str,
    send: F,
}

impl<F: FnMut(&str, &str, Option<&str>) -> Result<()>> Runner<'_, F> {
    /// A failed write still carries the entry, so a step error is never swapped for an I/O one.
    fn log(&self, kind: &str, message: &str) -> Result<()> {
        let generation = self.env["_connection"]
            .as_str()
            .and_then(|id| id.parse().ok());
        crate::log_line_scoped(self.home, generation, kind, self.origin, message).with_context(
            || {
                format!(
                    "could not write this [{kind}] entry to silicon.log: {}",
                    crate::failure::mask(self.home, message, &[])
                )
            },
        )
    }

    fn run(&mut self, flow: &Yaml) -> Result<()> {
        let mut chain: Option<bool> = None;
        for step in steps(flow)? {
            if matches!(step, Yaml::String(_) | Yaml::Tagged(_)) {
                chain = None;
                let result = (|| {
                    let expanded = crate::eval::value(step, &self.env, self.home, self.origin)?;
                    let produced = crate::failure::mask(self.home, &expanded.to_string(), &[]);
                    if !expanded.is_array() && !expanded.is_object() {
                        bail!("flow expression must produce an operation or list, got {produced}");
                    }
                    let expanded = serde_yaml::to_value(expanded).with_context(|| {
                        format!("flow expression produced {produced}, which is not YAML")
                    })?;
                    validate(&expanded).with_context(|| {
                        format!("flow expression produced an invalid flow {produced}")
                    })?;
                    self.run(&expanded)
                })();
                if let Err(error) = result {
                    let source = match step {
                        Yaml::Tagged(tag) => match tag.value.as_str() {
                            Some(value) => format!("{} {value}", tag.tag),
                            None => format!("{} {:?}", tag.tag, tag.value),
                        },
                        other => other
                            .as_str()
                            .map_or_else(|| format!("{other:?}"), str::to_owned),
                    };
                    self.log("error", &format!("flow expression {source}: {error:#}"))?;
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
            self.log("flow", &describe(name, body, summarize))?;
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
                    let step = describe(name, body, str::to_owned);
                    self.log("error", &format!("{step}: {error:#}"))?;
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
            .ok_or_else(|| anyhow!("{name} must be an object, got {body:?}"))?;
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
                if target.is_empty() {
                    bail!("send.isi evaluated to an empty string");
                }
                if session.as_deref() == Some("") {
                    bail!("send.session_id evaluated to an empty string");
                }
                (self.send)(&target, &message, session.as_deref())?;
                self.log("send", &format!("{target}: {message}"))?;
            }
            "log" => self.log("runtime", &self.field(map, "message")?)?,
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
    use std::fs;

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
    fn flow_entries_name_the_branch_and_assignment_they_ran() {
        let flow: Yaml = serde_yaml::from_str(
            r#"
- var: {name: sender, value: '{request.who}'}
- if:
    condition: '{request.who == "saket"}'
    then:
      - send: {isi: intuit, message: hello}
- if:
    condition: '{request.who == "nobody"}'
    then:
      - send: {isi: wrong, message: wrong}
- var: {name: shape, value: {nested: value}}
- send: {isi: intuit, session_id: job, message: bye}
"#,
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        execute(
            &flow,
            json!({"request": {"who": "saket"}}),
            dir.path(),
            "interpreter",
            |_, _, _| Ok(()),
        )
        .unwrap();
        let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        let flow_lines: Vec<&str> = log
            .lines()
            .filter(|line| line.starts_with("[flow] "))
            .filter_map(|line| line.rsplit_once("] [").map(|(_, body)| body))
            .map(|body| body.trim_end_matches(']'))
            .collect();
        assert_eq!(
            flow_lines,
            vec![
                "var sender = {request.who}",
                r#"if {request.who == "saket"}"#,
                "send intuit",
                r#"if {request.who == "nobody"}"#,
                // A non-string value has no readable source, so the name alone.
                "var shape",
                "send intuit session job",
            ],
            "{log}"
        );
    }

    #[test]
    fn long_flow_sources_are_flattened_and_truncated() {
        let long = "x".repeat(300);
        assert_eq!(summarize("a\n  b\tc"), "a b c");
        let short = summarize(&long);
        assert_eq!(short.chars().count(), 161);
        assert!(short.ends_with('…'));
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

    #[test]
    fn step_errors_reach_catch_and_log_in_the_tools_own_words() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join(".silicon/bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(
            bin.join("ledger"),
            "#!/bin/sh\necho '{\"ok\":false}'\necho 'ledger: quota exceeded' >&2\nexit 4\n",
        )
        .unwrap();
        fs::set_permissions(bin.join("ledger"), fs::Permissions::from_mode(0o700)).unwrap();
        let flow: Yaml = serde_yaml::from_str(
            &r#"
- var:
    name: total
    value: '! ledger --json'
    catch:
      - var: {name: tool, value: '{error}'}
- send:
    isi: intuit
    message: hi
    catch:
      - var: {name: provider, value: '{error}'}
- send: {isi: '{""}', message: hi, catch: [{var: {name: empty, value: '{error}'}}]}
- '{42}'
- var: {name: long, value: '! ledger --json # {long}'}
"#
            .replace("{long}", &"x".repeat(200)),
        )
        .unwrap();
        let vars = execute(&flow, json!({}), dir.path(), "interpreter", |_, _, _| {
            Err(anyhow!("session is closed").context("intuit refused the message"))
        })
        .unwrap();
        let tool = "`bash -c 'ledger --json'` failed: exit status: 4\nstderr:\nledger: quota exceeded\nstdout:\n{\"ok\":false}";
        assert_eq!(vars["tool"], tool);
        assert_eq!(
            vars["provider"],
            "intuit refused the message: session is closed"
        );
        assert_eq!(vars["empty"], "send.isi evaluated to an empty string");
        let log = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        assert!(
            log.contains(&format!(
                "[var total = ! ledger --json: {}]",
                tool.replace('\n', "\\n")
            )),
            "{log}"
        );
        assert!(
            log.contains("[send intuit: intuit refused the message: session is closed]"),
            "{log}"
        );
        assert!(
            log.contains(
                "[flow expression {42}: flow expression must produce an operation or list, got 42]"
            ),
            "{log}"
        );
        // [flow] progress entries are shortened; the [error] entry names the whole step.
        let long = format!("! ledger --json # {}", "x".repeat(200));
        assert!(
            log.contains(&format!("[var long = {long}: `bash -c ")),
            "{log}"
        );
        let error = format!(
            "{:#}",
            validate(
                &serde_yaml::from_str(
                    "- if: {condition: 'true', then: [{send: {isi: a, message: [x]}}]}"
                )
                .unwrap()
            )
            .unwrap_err()
        );
        assert_eq!(
            error,
            "flow step 0: if.then: flow step 0: send.message: expected a string expression, got Sequence [String(\"x\")]"
        );
    }
}
