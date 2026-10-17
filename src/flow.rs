//! Ordered event flows. Consecutive `if`s all run; trailing `else` means none matched.
use anyhow::{anyhow, bail, Context, Result};
use serde_json::{json, Value as Json};
use serde_yaml::{Mapping, Value as Yaml};
use std::path::Path;

/// Deepest nesting of branches, catches and generated flows. A flow expression that
/// produces itself would otherwise recurse until the thread's stack overflows, which
/// aborts the whole interpreter instead of failing one step.
const MAX_DEPTH: usize = 64;

/// A send refused because the interpreter is stopping or the Silicon disconnected. That is
/// not the step's failure, so no `catch` handles it: the flow stops there, and the Ting
/// batch that ran it stays queued for the next connection instead of being dropped as done.
#[derive(Debug)]
pub struct Interrupted(pub String);

impl std::fmt::Display for Interrupted {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for Interrupted {}

/// True when `error` stopped a flow because the work is being torn down.
pub fn interrupted(error: &anyhow::Error) -> bool {
    error.downcast_ref::<Interrupted>().is_some()
}

/// The runtime's own words for a send refused during shutdown or disconnect. Matched whole,
/// so a tool's output that merely mentions them is still an ordinary failure.
const TEARDOWN: [&str; 3] = [
    "interpreter is stopping",
    "interpreter stopped",
    "silicon disconnected",
];

fn teardown(error: &anyhow::Error) -> bool {
    interrupted(error)
        || error
            .chain()
            .any(|cause| TEARDOWN.contains(&cause.to_string().as_str()))
}

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
    // A return object is data, including a field literally named `catch`.
    let inner_catch = (name != "return")
        .then(|| body.as_mapping().and_then(|body| get(body, "catch")))
        .flatten();
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
    validate_at(flow, 0, 0, false)
}

pub fn validate_with_functions(flow: &Yaml, functions: &Yaml) -> Result<()> {
    if !functions.is_null() {
        let definitions = functions
            .as_mapping()
            .ok_or_else(|| anyhow!("functions must be an object"))?;
        for (name, definition) in definitions {
            if !name.as_str().is_some_and(|name| !name.is_empty()) {
                bail!("function name must be a nonempty string");
            }
            let map = definition
                .as_mapping()
                .ok_or_else(|| anyhow!("function {name:?} must be an object"))?;
            fields(map, "function", &["params", "do"])?;
            parameters(map)?;
            validate_at(required(map, "do")?, 0, 0, true)
                .with_context(|| format!("function {}", name.as_str().unwrap()))?;
        }
    }
    validate(flow)
}

fn parameters(map: &Mapping) -> Result<Vec<String>> {
    let Some(params) = get(map, "params") else {
        return Ok(Vec::new());
    };
    let params = params
        .as_sequence()
        .ok_or_else(|| anyhow!("function.params must be a list"))?;
    let mut names = Vec::new();
    for param in params {
        let name = param
            .as_str()
            .filter(|name| !name.is_empty())
            .ok_or_else(|| anyhow!("function parameter must be a nonempty string"))?;
        if names.iter().any(|previous| previous == name) {
            bail!("duplicate function parameter {name:?}");
        }
        names.push(name.to_owned());
    }
    Ok(names)
}

fn fields(map: &Mapping, name: &str, allowed: &[&str]) -> Result<()> {
    for key in map.keys() {
        if !key.as_str().is_some_and(|key| allowed.contains(&key)) {
            bail!("unknown field in {name}: {key:?}");
        }
    }
    Ok(())
}

fn validate_at(flow: &Yaml, depth: usize, loops: usize, function: bool) -> Result<()> {
    if depth > MAX_DEPTH {
        bail!("flow nesting exceeds {MAX_DEPTH} levels");
    }
    let validate = |flow: &Yaml| validate_at(flow, depth + 1, loops, function);
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
            } else if name == "return" {
                if !function {
                    bail!("return requires a function");
                }
                chain = false;
                crate::eval::validate(body).context("return")?;
            } else {
                chain = name == "if";
                let map = body
                    .as_mapping()
                    .ok_or_else(|| anyhow!("{name} must contain an object, got {body:?}"))?;
                let allowed: &[&str] = match name {
                    "if" => &["condition", "then", "else", "catch"],
                    "var" | "collect" => &["name", "value", "catch"],
                    "send" => &["isi", "session_id", "message", "aggregate", "catch"],
                    "log" => &["message", "catch"],
                    "for" => &["list", "var", "then", "catch"],
                    "call" => &["function", "args", "then", "catch"],
                    "switch" => &["value", "cases", "default", "catch"],
                    "continue" | "break" | "exit" => &["reason", "catch"],
                    _ => bail!("unknown flow operation: {name}"),
                };
                fields(map, name, allowed)?;
                match name {
                    "if" => {
                        string_field(required(map, "condition")?).context("if.condition")?;
                        validate(required(map, "then")?).context("if.then")?;
                        if let Some(branch) = get(map, "else") {
                            validate(branch).context("if.else")?;
                            chain = false;
                        }
                    }
                    "var" | "collect" => {
                        if name == "collect" && loops == 0 {
                            bail!("collect requires a for loop");
                        }
                        string_field(required(map, "name")?)
                            .with_context(|| format!("{name}.name"))?;
                        crate::eval::validate(required(map, "value")?)
                            .with_context(|| format!("{name}.value"))?;
                    }
                    "send" => {
                        string_field(required(map, "isi")?).context("send.isi")?;
                        string_field(required(map, "message")?).context("send.message")?;
                        for key in ["session_id", "aggregate"] {
                            if let Some(value) = get(map, key) {
                                string_field(value).with_context(|| format!("send.{key}"))?;
                            }
                        }
                    }
                    "log" => string_field(required(map, "message")?).context("log.message")?,
                    "for" => {
                        crate::eval::validate(required(map, "list")?).context("for.list")?;
                        string_field(required(map, "var")?).context("for.var")?;
                        validate_at(required(map, "then")?, depth + 1, loops + 1, function)
                            .context("for.then")?;
                    }
                    "call" => {
                        string_field(required(map, "function")?).context("call.function")?;
                        if let Some(args) = get(map, "args") {
                            crate::eval::validate(args).context("call.args")?;
                        }
                        if let Some(branch) = get(map, "then") {
                            validate(branch).context("call.then")?;
                        }
                    }
                    "switch" => {
                        crate::eval::validate(required(map, "value")?).context("switch.value")?;
                        let cases = required(map, "cases")?
                            .as_sequence()
                            .ok_or_else(|| anyhow!("switch.cases must be a list"))?;
                        for case in cases {
                            let case = case
                                .as_mapping()
                                .ok_or_else(|| anyhow!("switch case must be an object"))?;
                            fields(case, "switch case", &["case", "then"])?;
                            crate::eval::validate(required(case, "case")?)
                                .context("switch.case")?;
                            validate(required(case, "then")?).context("switch.then")?;
                        }
                        if let Some(branch) = get(map, "default") {
                            validate(branch).context("switch.default")?;
                        }
                    }
                    "continue" | "break" | "exit" => {
                        if name != "exit" && loops == 0 {
                            bail!("{name} requires a for loop");
                        }
                        string_field(required(map, "reason")?)
                            .with_context(|| format!("{name}.reason"))?;
                    }
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

/// Rendered data only: the runtime persists these values without evaluating them again.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Delivery {
    pub isi: String,
    pub message: String,
    pub session_id: Option<String>,
}

#[derive(Debug)]
struct DeliveryFailure;
impl std::fmt::Display for DeliveryFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("outgoing delivery could not be stored or completed")
    }
}
impl std::error::Error for DeliveryFailure {}

fn fatal(error: &anyhow::Error) -> bool {
    interrupted(error) || error.is::<DeliveryFailure>()
}

#[derive(Debug, PartialEq)]
enum Control {
    Next,
    Matched(bool),
    Continue,
    Break,
    Exit,
    Return(Json),
}

struct Runner<'a, F> {
    env: Json,
    functions: &'a Yaml,
    home: &'a Path,
    origin: &'a str,
    send: F,
    pending: Vec<Delivery>,
    /// Parent variable scopes updated only by explicit `collect` operations.
    loops: Vec<Json>,
    function_depth: usize,
    path: Vec<String>,
}

impl<F: FnMut(&Delivery) -> Result<()>> Runner<'_, F> {
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

    fn nested(&mut self, label: String, flow: &Yaml) -> Result<Control> {
        if self.path.len() >= MAX_DEPTH {
            bail!("flow nesting exceeds {MAX_DEPTH} levels at {} > {label}; a flow expression may be producing itself", self.path.join(" > "));
        }
        self.path.push(label);
        let result = self.run(flow);
        self.path.pop();
        result
    }

    /// Introduce one scoped `self` member without losing enclosing results/errors/loop data.
    fn scoped(&mut self, key: &str, value: Json, label: String, flow: &Yaml) -> Result<Control> {
        let previous = self.env["self"].clone();
        self.env["self"][key] = value;
        let result = self.nested(label, flow);
        self.env["self"] = previous;
        result
    }

    fn run(&mut self, flow: &Yaml) -> Result<Control> {
        let mut chain: Option<bool> = None;
        for (index, step) in steps(flow)?.iter().enumerate() {
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
                    validate_at(&expanded, 0, self.loops.len(), self.function_depth > 0)
                        .with_context(|| {
                            format!("flow expression produced an invalid flow {produced}")
                        })?;
                    self.nested(format!("step {index} (expression)"), &expanded)
                })();
                match result {
                    Ok(Control::Next) => {}
                    Ok(control) => return Ok(control),
                    Err(error) if fatal(&error) => return Err(error),
                    Err(error) => {
                        let source = match step {
                            Yaml::Tagged(tag) => match tag.value.as_str() {
                                Some(value) => format!("{} {value}", tag.tag),
                                None => format!("{} {:?}", tag.tag, tag.value),
                            },
                            other => other
                                .as_str()
                                .map_or_else(|| format!("{other:?}"), str::to_owned),
                        };
                        let error = error.context(format!("flow expression {source}"));
                        self.log("error", &crate::failure::report(&error))?;
                        if self.function_depth > 0 {
                            return Err(error);
                        }
                    }
                }
                continue;
            }
            let (name, body, catch) = operation(step)?;
            if name == "else" {
                if chain.take() == Some(false) {
                    let control = self.nested(format!("step {index} else"), body)?;
                    if control != Control::Next {
                        return Ok(control);
                    }
                }
                continue;
            }
            if name != "if" {
                chain = None;
            }
            self.log("flow", &describe(name, body, summarize))?;
            match self.step(index, name, body) {
                Ok(Control::Matched(matched)) => {
                    chain = Some(chain.unwrap_or(false) || matched);
                    if body
                        .as_mapping()
                        .is_some_and(|map| get(map, "else").is_some())
                    {
                        chain = None;
                    }
                }
                Ok(Control::Next) => {}
                Ok(control) => return Ok(control),
                Err(error) if fatal(&error) => return Err(error),
                Err(error) => {
                    if name == "if" {
                        chain = Some(chain.unwrap_or(false));
                    }
                    let error = error.context(describe(name, body, str::to_owned));
                    let report = crate::failure::report(&error);
                    self.log("error", &report)?;
                    if let Some(catch) = catch.filter(|value| !value.is_null()) {
                        let control = self.scoped(
                            "error",
                            json!(report),
                            format!("step {index} catch"),
                            catch,
                        )?;
                        if control != Control::Next {
                            return Ok(control);
                        }
                    } else if self.function_depth > 0 {
                        return Err(error);
                    }
                }
            }
        }
        Ok(Control::Next)
    }

    fn field(&self, map: &Mapping, key: &str) -> Result<String> {
        render(required(map, key)?, &self.env, self.home, self.origin)
    }

    fn value(&self, value: &Yaml) -> Result<Json> {
        crate::eval::value(value, &self.env, self.home, self.origin)
    }

    fn boolean(&self, map: &Mapping, key: &str) -> Result<bool> {
        let value = self.field(map, key)?;
        match value.trim() {
            "true" => Ok(true),
            "false" => Ok(false),
            _ => bail!("{key} must evaluate to true or false, got {value:?}"),
        }
    }

    fn deliver(&mut self, delivery: &Delivery) -> Result<()> {
        if let Err(error) = (self.send)(delivery) {
            let entry = format!(
                "{}\nVia: send {}; the flow stops here and no catch runs",
                crate::failure::report(&error),
                delivery.isi
            );
            if let Err(log) = self.log("error", &entry) {
                crate::stderr_line(&crate::failure::report(&log));
            }
            return Err(if interrupted(&error) {
                error
            } else if teardown(&error) {
                error.context(Interrupted(format!(
                    "the flow stopped at `send {}`",
                    delivery.isi
                )))
            } else {
                error.context(DeliveryFailure)
            });
        }
        self.log("send", &format!("{}: {}", delivery.isi, delivery.message))
            .map_err(|error| error.context(DeliveryFailure))
    }

    fn flush(&mut self) -> Result<()> {
        for delivery in std::mem::take(&mut self.pending) {
            self.deliver(&delivery)?;
        }
        Ok(())
    }

    fn call(&mut self, index: usize, map: &Mapping) -> Result<Control> {
        let name = self.field(map, "function")?;
        let definition = self
            .functions
            .as_mapping()
            .and_then(|functions| get(functions, &name))
            .and_then(Yaml::as_mapping)
            .cloned()
            .ok_or_else(|| anyhow!("undefined function {name:?}"))?;
        let args = get(map, "args")
            .map(|value| self.value(value))
            .transpose()?
            .unwrap_or_else(|| json!({}));
        let arguments = args
            .as_object()
            .ok_or_else(|| anyhow!("call.args must evaluate to an object"))?;
        let params = parameters(&definition)?;
        for param in &params {
            if !arguments.contains_key(param) {
                bail!("function {name} requires argument {param:?}");
            }
        }
        for param in arguments.keys() {
            if !params.contains(param) {
                bail!("function {name} has no parameter {param:?}");
            }
        }
        let previous_var = self.env["var"].take();
        let previous_self = self.env["self"].take();
        let previous_args = self
            .env
            .as_object_mut()
            .unwrap()
            .insert("args".into(), args);
        let previous_loops = std::mem::take(&mut self.loops);
        self.env["var"] = json!({});
        self.env["self"] = json!({});
        self.function_depth += 1;
        let result = self.nested(
            format!("step {index} call {name}"),
            required(&definition, "do")?,
        );
        self.function_depth -= 1;
        self.env["var"] = previous_var;
        self.env["self"] = previous_self;
        if let Some(args) = previous_args {
            self.env["args"] = args;
        } else {
            self.env.as_object_mut().unwrap().remove("args");
        }
        self.loops = previous_loops;
        let result = match result? {
            Control::Return(value) => value,
            Control::Next => Json::Null,
            Control::Exit => return Ok(Control::Exit),
            _ => bail!("function {name} tried to control a caller's loop"),
        };
        if let Some(branch) = get(map, "then") {
            self.scoped("result", result, format!("step {index} call.then"), branch)
        } else {
            Ok(Control::Next)
        }
    }

    fn for_loop(&mut self, index: usize, map: &Mapping) -> Result<Control> {
        let list = self.value(required(map, "list")?)?;
        let list = list
            .as_array()
            .ok_or_else(|| anyhow!("for.list must evaluate to a list"))?;
        let name = self.field(map, "var")?;
        if name.is_empty() {
            bail!("for.var must not be empty");
        }
        let previous_self = self.env["self"].clone();
        // ponytail: clone JSON scopes; use shared maps if large loop state becomes costly.
        self.loops.push(self.env["var"].clone());
        let result = (|| {
            for (position, value) in list.iter().enumerate() {
                self.env["var"] = self.loops.last().unwrap().clone();
                self.env["var"][&name] = value.clone();
                self.env["self"]["for"] = json!({"index": position});
                match self.nested(
                    format!("step {index} for[{position}]"),
                    required(map, "then")?,
                )? {
                    Control::Next | Control::Continue => {}
                    Control::Break => break,
                    control => return Ok(control),
                }
            }
            Ok(Control::Next)
        })();
        self.env["var"] = self.loops.pop().unwrap();
        self.env["self"] = previous_self;
        result
    }

    fn step(&mut self, index: usize, name: &str, body: &Yaml) -> Result<Control> {
        if name == "return" {
            return Ok(Control::Return(self.value(body)?));
        }
        let map = body
            .as_mapping()
            .ok_or_else(|| anyhow!("{name} must be an object, got {body:?}"))?;
        match name {
            "if" => {
                let matched = self.boolean(map, "condition")?;
                let branch = if matched {
                    Some(required(map, "then")?)
                } else {
                    get(map, "else")
                };
                if let Some(branch) = branch {
                    let control = self.nested(
                        format!("step {index} if.{}", if matched { "then" } else { "else" }),
                        branch,
                    )?;
                    if control != Control::Next {
                        return Ok(control);
                    }
                }
                return Ok(Control::Matched(matched));
            }
            "var" | "collect" => {
                let target = self.field(map, "name")?;
                if target.is_empty() {
                    bail!("{name}.name must not be empty");
                }
                let value = self.value(required(map, "value")?)?;
                if name == "collect" {
                    let scope = self
                        .loops
                        .last_mut()
                        .ok_or_else(|| anyhow!("collect requires a for loop"))?;
                    if scope.get(&target).is_none() {
                        scope[&target] = json!([]);
                    }
                    scope[&target]
                        .as_array_mut()
                        .ok_or_else(|| anyhow!("collect target {target:?} must be a list"))?
                        .push(value);
                    self.env["var"][&target] = scope[&target].clone();
                } else {
                    self.env["var"][target] = value;
                }
            }
            "send" => {
                let target = self.field(map, "isi")?;
                let message = self.field(map, "message")?;
                let session_id = get(map, "session_id")
                    .map(|value| render(value, &self.env, self.home, self.origin))
                    .transpose()?;
                if target.is_empty() {
                    bail!("send.isi evaluated to an empty string");
                }
                if session_id.as_deref() == Some("") {
                    bail!("send.session_id evaluated to an empty string");
                }
                if session_id
                    .as_ref()
                    .is_some_and(|session| session.len() > 1024)
                {
                    bail!("session id must contain 1–1024 bytes");
                }
                if let Some(isies) = self.env.get("isi") {
                    let isi = isies
                        .get(&target)
                        .ok_or_else(|| anyhow!("unknown isi: {target}"))?;
                    if isi["primary_send_mode"] == "session" && session_id.is_none() {
                        bail!("{target} uses session addressing; session_id is required");
                    }
                }
                let aggregate = if get(map, "aggregate").is_some() {
                    self.boolean(map, "aggregate")?
                } else {
                    true
                };
                let delivery = Delivery {
                    isi: target,
                    message,
                    session_id,
                };
                // ponytail: linear destination lookup; index this if batches have many destinations.
                let pending = self.pending.iter().position(|pending| {
                    pending.isi == delivery.isi && pending.session_id == delivery.session_id
                });
                if aggregate {
                    if let Some(index) = pending {
                        self.pending[index].message.push('\n');
                        self.pending[index].message.push_str(&delivery.message);
                    } else {
                        self.pending.push(delivery);
                    }
                } else {
                    if let Some(index) = pending {
                        let pending = self.pending.remove(index);
                        self.deliver(&pending)?;
                    }
                    self.deliver(&delivery)?;
                }
            }
            "log" => self.log("runtime", &self.field(map, "message")?)?,
            "call" => return self.call(index, map),
            "for" => return self.for_loop(index, map),
            "switch" => {
                let value = self.value(required(map, "value")?)?;
                for (position, case) in required(map, "cases")?
                    .as_sequence()
                    .unwrap()
                    .iter()
                    .enumerate()
                {
                    let case = case.as_mapping().unwrap();
                    if value == self.value(required(case, "case")?)? {
                        return self.nested(
                            format!("step {index} switch.case[{position}]"),
                            required(case, "then")?,
                        );
                    }
                }
                if let Some(branch) = get(map, "default") {
                    return self.nested(format!("step {index} switch.default"), branch);
                }
            }
            "continue" | "break" | "exit" => {
                let reason = self.field(map, "reason")?;
                if reason.trim().is_empty() {
                    bail!("{name}.reason must not be empty");
                }
                self.log("runtime", &format!("{name}: {reason}"))?;
                return Ok(match name {
                    "continue" => Control::Continue,
                    "break" => Control::Break,
                    _ => Control::Exit,
                });
            }
            _ => bail!("unknown flow operation: {name}"),
        }
        Ok(Control::Next)
    }
}

/// Run with reusable functions and hand rendered destination groups to the durable runtime.
/// Callback failures are fatal infrastructure failures; they never enter a step's catch.
pub fn execute_with_functions<F>(
    flow: &Yaml,
    functions: &Yaml,
    mut env: Json,
    home: &Path,
    origin: &str,
    send: F,
) -> Result<Json>
where
    F: FnMut(&Delivery) -> Result<()>,
{
    validate_with_functions(flow, functions)?;
    let object = env
        .as_object_mut()
        .ok_or_else(|| anyhow!("flow environment must be an object"))?;
    object.insert("var".into(), json!({}));
    object.insert("self".into(), json!({}));
    object.remove("error");
    let mut runner = Runner {
        env,
        functions,
        home,
        origin,
        send,
        pending: Vec::new(),
        loops: Vec::new(),
        function_depth: 0,
        path: Vec::new(),
    };
    match runner.run(flow)? {
        Control::Next | Control::Exit => {}
        _ => bail!("flow attempted to return or control a loop outside its scope"),
    }
    runner.flush()?;
    Ok(runner.env["var"].take())
}

/// Compatibility entry point for callers without function definitions.
pub fn execute<F>(flow: &Yaml, env: Json, home: &Path, origin: &str, mut send: F) -> Result<Json>
where
    F: FnMut(&str, &str, Option<&str>) -> Result<()>,
{
    execute_with_functions(flow, &Yaml::Null, env, home, origin, |delivery| {
        send(
            &delivery.isi,
            &delivery.message,
            delivery.session_id.as_deref(),
        )
    })
}

#[cfg(test)]
mod error_report_tests {
    use super::*;

    #[test]
    fn catch_receives_raw_fallback_diagnostics_and_the_complete_flow_trace() {
        let home = std::env::temp_dir().join(format!("silicon-flow-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&home).unwrap();
        let flow: Yaml = serde_yaml::from_str(
            r#"
- var:
    name: answer
    value: "! printf 'first failure' >&2; printf 'first stdout'; exit 7 !>> ! printf 'second failure' >&2; exit 8"
  catch:
    - send:
        isi: observer
        message: '{self.error}'
"#,
        )
        .unwrap();
        let mut reports = Vec::new();
        execute(&flow, json!({}), &home, "test", |_, message, _| {
            reports.push(message.to_owned());
            Ok(())
        })
        .unwrap();
        let [report] = reports.as_slice() else {
            panic!("the catch must send exactly one report");
        };
        assert!(report.starts_with("stderr:\nfirst failure"));
        assert!(report.contains("stdout:\nfirst stdout"));
        assert!(report.contains("second failure"));
        assert!(report.contains("Source: `bash -c"));
        assert!(report.contains("Status: exit status: 7"));
        assert!(report.contains("Status: exit status: 8"));
        assert!(report.contains("Via: all 2 fallback candidates failed"));
        assert!(report.contains("Via: var answer ="));
        assert!(!report.contains("Source: Silicon interpreter"));
        std::fs::remove_dir_all(home).unwrap();
    }
}
