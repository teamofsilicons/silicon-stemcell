//! Shared CEL → Bash → string evaluation. Configuration is trusted executable input.
use crate::failure::{self, panic_message};
use anyhow::{anyhow, bail, Context as _, Result};
use cel_interpreter::{
    extractors::{Identifier, This},
    Context, ExecutionError, FunctionContext, IdedExpr, ParseErrors, Program, Value,
};
use chrono::DateTime;
use chrono_tz::Tz;
use serde_json::Value as Json;
use serde_yaml::Value as Yaml;
use std::{
    collections::HashMap, fs, panic::AssertUnwindSafe, path::Path, process::Output, sync::Arc,
};

/// A credential expression's source may embed the credential, so its errors use this name.
const CREDENTIAL: &str = "[credential expression]";

fn shown(source: &str, secret: bool) -> &str {
    if secret {
        CREDENTIAL
    } else {
        source
    }
}

/// CEL and parser text quotes source tokens (`extraneous input ''sk-live-…''`). In a
/// credential expression those may be the credential, so its words of 8+ characters
/// (the same floor telemetry uses for secrets) are masked; the message itself stays.
fn scrub(text: &str, source: &str, secret: bool) -> String {
    if !secret {
        return text.to_owned();
    }
    let mut words = source
        .split(|c: char| !c.is_alphanumeric() && c != '_')
        .filter(|word| word.len() >= 8)
        .collect::<Vec<_>>();
    words.sort_by_key(|word| std::cmp::Reverse(word.len()));
    words.into_iter().fold(text.to_owned(), |text, word| {
        text.replace(word, "[redacted]")
    })
}

/// The parser's own words. A credential expression's snippet lines are its source, so they go.
fn parse_errors(errors: &ParseErrors, source: &str, secret: bool) -> String {
    errors
        .errors
        .iter()
        .map(|error| {
            let mut text = if secret {
                format!(
                    "ERROR: <input>:{}:{}: {}",
                    error.pos.0, error.pos.1, error.msg
                )
            } else {
                error.to_string()
            };
            if let Some(cause) = &error.source {
                text.push_str(&format!("\ncaused by: {cause}"));
            }
            scrub(&text, source, secret)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

// JSON has no uint type: use CEL int where possible so `request.count + 1` works.
fn cel_value(value: Json) -> Value {
    match value {
        Json::Null => Value::Null,
        Json::Bool(value) => Value::Bool(value),
        Json::String(value) => value.into(),
        Json::Number(value) => {
            if let Some(value) = value.as_i64() {
                Value::Int(value)
            } else if let Some(value) = value.as_u64() {
                Value::UInt(value)
            } else {
                Value::Float(value.as_f64().unwrap())
            }
        }
        Json::Array(items) => items.into_iter().map(cel_value).collect::<Vec<_>>().into(),
        Json::Object(items) => items
            .into_iter()
            .map(|(key, value)| (key, cel_value(value)))
            .collect::<HashMap<_, _>>()
            .into(),
    }
}

fn compile(source: &str, secret: bool) -> Result<Program> {
    let name = shown(source, secret);
    // cel-parser 0.10's error recovery can panic on malformed input; contain that upstream bug.
    std::panic::catch_unwind(|| Program::compile(source))
        .map_err(|panic| {
            anyhow!(
                "invalid CEL `{name}`: the CEL parser panicked instead of reporting the syntax error: {}",
                scrub(panic_message(&*panic), source, secret)
            )
        })?
        .map_err(|errors| {
            anyhow!(
                "invalid CEL `{name}`: {}",
                parse_errors(&errors, source, secret)
            )
        })
}

fn context(env: &Json) -> Result<Context<'static>> {
    let mut context = Context::default();
    for (name, value) in env
        .as_object()
        .ok_or_else(|| anyhow!("CEL environment must be an object"))?
    {
        context.add_variable_from_value(name, cel_value(value.clone()));
    }
    for name in ["tz_time", "convert_time"] {
        context.add_function(
            name,
            move |when: Arc<String>, zone: Arc<String>| -> Result<Value, ExecutionError> {
                let time = DateTime::parse_from_rfc3339(&when).map_err(|e| {
                    ExecutionError::function_error(name, format!("{when:?} is not RFC 3339: {e}"))
                })?;
                // chrono-tz only says "failed to parse timezone"; name the zone it rejected.
                let tz: Tz = zone.parse().map_err(|e| {
                    ExecutionError::function_error(
                        name,
                        format!("{zone:?} is not an IANA time zone name: {e}"),
                    )
                })?;
                Ok(format!(
                    "{} {}",
                    time.with_timezone(&tz).format("%H:%M:%S %d:%m:%y"),
                    zone
                )
                .into())
            },
        );
    }
    context.add_function(
        "to_json",
        |source: Arc<String>| -> Result<Value, ExecutionError> {
            let value: Json = serde_json::from_str(&source)
                .map_err(|e| ExecutionError::function_error("to_json", e))?;
            Ok(cel_value(value))
        },
    );
    for name in ["to_yaml", "make_readable"] {
        context.add_function(name, move |value: Value| -> Result<Value, ExecutionError> {
            let json = value
                .json()
                .map_err(|e| ExecutionError::function_error(name, e))?;
            serde_yaml::to_string(&json)
                .map(Value::from)
                .map_err(|e| ExecutionError::function_error(name, e))
        });
    }
    // The reference dialect uses the Python spelling and split alongside standard CEL.
    context.add_function("startswith", cel_interpreter::functions::starts_with);
    context.add_function(
        "split",
        |This(source): This<Arc<String>>,
         separator: Arc<String>|
         -> Result<Value, ExecutionError> {
            Ok(source
                .split(separator.as_str())
                .map(|part| Value::from(part.to_owned()))
                .collect::<Vec<_>>()
                .into())
        },
    );
    context.add_function(
        "sortBy",
        |ctx: &FunctionContext,
         This(items): This<Arc<Vec<Value>>>,
         Identifier(binding): Identifier,
         key: IdedExpr|
         -> Result<Value, ExecutionError> {
            if ctx.args.len() != 2 {
                return Err(ctx.error(format!(
                    "expected sortBy(binding, key), got {} arguments",
                    ctx.args.len()
                )));
            }
            let mut scope = ctx.ptx.new_inner_scope();
            let mut keyed = items
                .iter()
                .map(|item| {
                    scope.add_variable_from_value(binding.as_str(), item.clone());
                    scope.resolve(&key).map(|key| (key, item.clone()))
                })
                .collect::<Result<Vec<_>, _>>()?;
            let compare = |a: &Value, b: &Value| match (a, b) {
                (Value::Bytes(a), Value::Bytes(b)) => Some(a.cmp(b)),
                (Value::Null, _) => None,
                _ => a.partial_cmp(b),
            };
            if let Some((first, _)) = keyed.first() {
                if keyed.iter().any(|(key, _)| {
                    std::mem::discriminant(key) != std::mem::discriminant(first)
                        || compare(first, key).is_none()
                }) {
                    return Err(ctx.error(format!(
                        "sort keys must have the same comparable type; got {:?}",
                        keyed.iter().map(|(key, _)| key).collect::<Vec<_>>()
                    )));
                }
            }
            keyed.sort_by(|(a, _), (b, _)| compare(a, b).unwrap());
            Ok(keyed
                .into_iter()
                .map(|(_, item)| item)
                .collect::<Vec<_>>()
                .into())
        },
    );
    for name in ["join", "distinct", "slice", "reverse", "flatten"] {
        context.add_function(name, list_function);
    }
    Ok(context)
}

fn list_function(ctx: &FunctionContext) -> Result<Value, ExecutionError> {
    let Some(Value::List(items)) = &ctx.this else {
        return Err(ctx.error(match &ctx.this {
            Some(this) => format!("expected a list receiver, got {this:?}"),
            None => "expected a list receiver, got none".to_owned(),
        }));
    };
    let args = ctx
        .args
        .iter()
        .map(|arg| ctx.ptx.resolve(arg))
        .collect::<Result<Vec<_>, _>>()?;
    match (ctx.name.as_str(), args.as_slice()) {
        ("join", [] | [Value::String(_)]) => {
            let separator = match args.first() {
                Some(Value::String(separator)) => separator.as_str(),
                _ => "",
            };
            items
                .iter()
                .map(|item| match item {
                    Value::String(item) => Ok(item.as_str()),
                    other => {
                        Err(ctx.error(format!("join requires string elements, got {other:?}")))
                    }
                })
                .collect::<Result<Vec<_>, _>>()
                .map(|items| items.join(separator).into())
        }
        ("distinct", []) => {
            let mut distinct = Vec::new();
            // ponytail: quadratic CEL equality scan; hash canonical keys if Ting batches grow large.
            for item in items.iter() {
                if !distinct.contains(item) {
                    distinct.push(item.clone());
                }
            }
            Ok(distinct.into())
        }
        ("slice", [Value::Int(start), Value::Int(end)])
            if *start >= 0 && start <= end && *end as u64 <= items.len() as u64 =>
        {
            Ok(items[*start as usize..*end as usize].to_vec().into())
        }
        ("reverse", []) => Ok(items.iter().rev().cloned().collect::<Vec<_>>().into()),
        ("flatten", [] | [Value::Int(_)]) => {
            let depth = match args.first() {
                Some(Value::Int(depth)) => *depth,
                _ => 1,
            };
            if depth < 0 {
                return Err(ctx.error(format!("flatten depth must not be negative, got {depth}")));
            }
            fn flatten(items: &[Value], depth: i64, output: &mut Vec<Value>) {
                for item in items {
                    match item {
                        Value::List(items) if depth > 0 => flatten(items, depth - 1, output),
                        _ => output.push(item.clone()),
                    }
                }
            }
            let mut output = Vec::new();
            flatten(items, depth, &mut output);
            Ok(output.into())
        }
        _ => Err(ctx.error(format!(
            "invalid arguments {args:?} for a list of {} items",
            items.len()
        ))),
    }
}

fn cel(source: &str, env: &Json, secret: bool) -> Result<Json> {
    let name = shown(source, secret);
    let program = compile(source, secret)?;
    let context = context(env)?;
    let value = std::panic::catch_unwind(AssertUnwindSafe(|| program.execute(&context)))
        .map_err(|panic| {
            anyhow!(
                "CEL `{name}` panicked during evaluation: {}",
                scrub(panic_message(&*panic), source, secret)
            )
        })?
        .map_err(|error| {
            anyhow!(
                "CEL `{name}`: {}",
                scrub(&error.to_string(), source, secret)
            )
        })?;
    value.json().map_err(|error| {
        anyhow!(
            "CEL `{name}` produced a value JSON cannot hold: {}",
            scrub(&error.to_string(), source, secret)
        )
    })
}

fn text(value: Json) -> String {
    match value {
        Json::String(value) => value,
        other => other.to_string(),
    }
}

#[derive(Debug)]
enum Part {
    Text(String),
    Cel(String),
}

/// Braces nest (CEL maps); braces within CEL string literals do not close a template.
fn template(source: &str) -> Result<Vec<Part>> {
    let bytes = source.as_bytes();
    let mut parts = Vec::new();
    let mut literal = String::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'\\'
            && bytes
                .get(i + 1)
                .is_some_and(|c| matches!(c, b'{' | b'}' | b'\\'))
        {
            literal.push(bytes[i + 1] as char);
            i += 2;
        } else if bytes[i] == b'{' {
            if !literal.is_empty() {
                parts.push(Part::Text(std::mem::take(&mut literal)));
            }
            let start = i + 1;
            let mut depth = 1;
            let mut quote = None;
            i += 1;
            while i < bytes.len() && depth > 0 {
                match (bytes[i], quote) {
                    (b'\\', Some(_)) => {
                        i += 2;
                        continue;
                    }
                    (c, Some(q)) if c == q => quote = None,
                    (_, Some(_)) => {}
                    (b'\'' | b'"', None) => quote = Some(bytes[i]),
                    (b'{', None) => depth += 1,
                    (b'}', None) => depth -= 1,
                    _ => {}
                }
                i += 1;
            }
            if depth != 0 {
                bail!("unclosed CEL interpolation at byte {}", start - 1);
            }
            parts.push(Part::Cel(source[start..i - 1].to_owned()));
        } else if bytes[i] == b'}' {
            bail!("unescaped closing brace at byte {i}; use \\}} for a literal brace");
        } else {
            let ch = source[i..].chars().next().unwrap();
            literal.push(ch);
            i += ch.len_utf8();
        }
    }
    if !literal.is_empty() {
        parts.push(Part::Text(literal));
    }
    Ok(parts)
}

/// Split only at unquoted separators outside CEL, so scripts may print literal `!>>`.
fn fallbacks(source: &str, mode: Mode) -> Result<Vec<&str>> {
    let bytes = source.as_bytes();
    let mut result = Vec::new();
    let (mut start, mut i, mut depth) = (0, 0, 0usize);
    let mut quote = None;
    let command = matches!(mode, Mode::AppCommand);
    let mut quoted_candidate = command || source.trim_start().starts_with(['!', '\'', '"']);
    while i < bytes.len() {
        match (bytes[i], quote) {
            (b'\\', _) => {
                i += 2;
                continue;
            }
            (c, Some(q)) if c == q => quote = None,
            (_, Some(_)) => {}
            (b'\'' | b'"', None) if depth > 0 || quoted_candidate => quote = Some(bytes[i]),
            (b'{', None) => depth += 1,
            (b'}', None) if depth > 0 => depth -= 1,
            (b'!', None) if depth == 0 && source[i..].starts_with("!>>") => {
                result.push(source[start..i].trim());
                start = i + 3;
                i += 3;
                quoted_candidate =
                    command || source[start..].trim_start().starts_with(['!', '\'', '"']);
                continue;
            }
            _ => {}
        }
        i += 1;
    }
    result.push(if result.is_empty() {
        source
    } else {
        source[start..].trim()
    });
    if result.iter().any(|part| part.is_empty()) && result.len() > 1 {
        bail!(
            "empty fallback candidate in `{}`: `!>>` needs an expression on each side",
            shown(source, mode.secret())
        );
    }
    Ok(result)
}

fn unquote(source: &str, secret: bool) -> Result<(String, bool)> {
    if source.starts_with('"') && source.ends_with('"') && source.len() >= 2 {
        return Ok((
            serde_json::from_str::<String>(source)
                .with_context(|| format!("invalid quoted fallback `{}`", shown(source, secret)))?,
            true,
        ));
    }
    if source.starts_with('\'') && source.ends_with('\'') && source.len() >= 2 {
        return Ok((source[1..source.len() - 1].replace("''", "'"), true));
    }
    Ok((source.to_owned(), false))
}

fn parts(source: &str, secret: bool) -> Result<Vec<Part>> {
    template(source).with_context(|| format!("invalid template `{}`", shown(source, secret)))
}

fn interpolate(source: &str, env: &Json, secret: bool) -> Result<String> {
    parts(source, secret)?
        .into_iter()
        .map(|part| match part {
            Part::Text(text) => Ok(text),
            Part::Cel(source) => cel(&source, env, secret).map(text),
        })
        .collect()
}

#[derive(Clone, Copy)]
enum Mode {
    Text,
    Secret,
    Dna,
    AppCommand,
    Setup,
}

impl Mode {
    fn secret(self) -> bool {
        matches!(self, Mode::Secret)
    }
}

fn redact(message: &str, env: &Json) -> String {
    let secrets = crate::telemetry::silicon_secrets(&env["silicon"]);
    crate::telemetry::redact_text(message, &secrets)
}

/// A failed write still carries the entry, so an evaluation error is never swapped for an I/O one.
fn log(env: &Json, home: &Path, kind: &str, isi: &str, message: &str) -> Result<()> {
    let generation = env["_connection"].as_str().and_then(|id| id.parse().ok());
    let message = redact(message, env);
    crate::log_line_scoped(home, generation, kind, isi, &message).with_context(|| {
        format!(
            "could not write this [{kind}] entry to silicon.log: {}",
            failure::mask(home, &message, &[])
        )
    })
}

/// A credential command's error keeps its status and stderr; its stdout would be the credential.
fn credential_failure(home: &Path, problem: &str, output: &Output) -> anyhow::Error {
    let streams = failure::describe(&Output {
        stdout: Vec::new(),
        ..output.clone()
    });
    let streams = streams
        .strip_suffix("\nstdout: (empty)")
        .unwrap_or(&streams);
    anyhow!(
        "{}",
        failure::mask(
            home,
            &format!("`{CREDENTIAL}` {problem}{streams}\nstdout: (withheld; it is the credential)"),
            &[]
        )
    )
}

fn candidate_source(source: &str, mode: Mode) -> Result<(String, bool)> {
    let (mut source, quoted) = if matches!(mode, Mode::AppCommand) {
        (source.to_owned(), false)
    } else {
        unquote(source, mode.secret())?
    };
    // Decode a quoted Bash body here; app commands need these same quotes for argv.
    if !quoted && !matches!(mode, Mode::AppCommand) {
        if let Some(command) = source.strip_prefix('!').map(str::trim_start) {
            if command.starts_with(['\'', '"']) {
                if let Ok(command) = serde_yaml::from_str::<String>(command) {
                    source = format!("! {command}");
                }
            }
        }
    }
    Ok((source, quoted))
}

fn candidate(source: &str, env: &Json, home: &Path, isi: &str, mode: Mode) -> Result<String> {
    let (source, quoted) = candidate_source(source, mode)?;
    let expanded = interpolate(&source, env, mode.secret())?;
    if matches!(mode, Mode::AppCommand) {
        let command = expanded.trim();
        let command = if source.trim_start().starts_with('!') {
            command.strip_prefix('!').unwrap_or(command).trim_start()
        } else {
            command
        };
        let argv = shell_words::split(command)
            .with_context(|| format!("invalid app command quoting in `{command}`"))?;
        if argv.first().is_none_or(|arg| arg.is_empty()) || command.contains(['\n', '\0']) {
            bail!(
                "app command {command:?} must contain an executable and valid single-line arguments"
            );
        }
        return Ok(if source.trim_start().starts_with('!') {
            format!("! {command}")
        } else {
            command.to_owned()
        });
    }
    // A request value beginning with `!` is data, never an implicit shell command.
    if let Some(command) = expanded
        .strip_prefix('!')
        .filter(|_| !quoted && source.starts_with('!'))
    {
        let command = command.trim_start();
        let kind = if matches!(mode, Mode::Setup) {
            "setup"
        } else {
            "command"
        };
        // Compile runs before runtime redaction is registered and may interpolate credentials.
        let compile_time = env["silicon"].get("token").is_some();
        log(
            env,
            home,
            kind,
            isi,
            &format!(
                "running: {}",
                if mode.secret() {
                    CREDENTIAL
                } else if compile_time {
                    "[compile-time expression]"
                } else {
                    command
                }
            ),
        )?;
        // Mask before quoting so shell quoting cannot split a credential past the mask.
        let name = if mode.secret() {
            CREDENTIAL.to_owned()
        } else {
            failure::argv(
                "bash",
                &["-c", &failure::mask(home, &redact(command, env), &[])],
            )
        };
        let output = crate::command("bash", home)
            .arg("-c")
            .arg(command)
            .env("ISI", isi)
            .env(
                "SILICON_ORG",
                env["silicon"]["SILICON_ORG"].as_str().unwrap_or_default(),
            )
            .output()
            .map_err(|error| failure::spawn(home, &name, &error))?;
        if matches!(mode, Mode::Setup) {
            for (stream, bytes) in [("stdout", &output.stdout), ("stderr", &output.stderr)] {
                for line in String::from_utf8_lossy(bytes).lines() {
                    log(env, home, "setup", stream, line)?;
                }
            }
        }
        log(
            env,
            home,
            kind,
            isi,
            &format!("finished: {}", output.status),
        )?;
        if !output.status.success() {
            return Err(if mode.secret() {
                credential_failure(home, "failed: ", &output)
            } else {
                failure::command(home, &name, &output, &[])
            });
        }
        return match std::str::from_utf8(&output.stdout) {
            Ok(stdout) => Ok(stdout.trim_end_matches(['\r', '\n']).to_owned()),
            Err(error) if mode.secret() => Err(credential_failure(
                home,
                &format!("printed a credential that is not UTF-8: {error}\n"),
                &output,
            )),
            Err(error) => Err(failure::answer(
                home,
                &name,
                &format!("printed output that is not UTF-8: {error}"),
                &output,
                &[],
            )),
        };
    }
    if matches!(mode, Mode::Dna) && !quoted {
        let path = home.join(&expanded);
        return fs::read_to_string(&path)
            .with_context(|| format!("could not read DNA file {}", path.display()));
    }
    Ok(expanded)
}

/// One failing candidate is the error; several are listed in the order they were tried.
/// Each error is masked once, before it is logged, so the log and the caller read the same text.
fn run(source: &str, env: &Json, home: &Path, isi: &str, mode: Mode) -> Result<String> {
    let masked =
        |error: anyhow::Error| failure::mask(home, &redact(&format!("{error:#}"), env), &[]);
    let mut errors = Vec::new();
    for source in fallbacks(source, mode).map_err(|error| anyhow!(masked(error)))? {
        match candidate(source, env, home, isi, mode) {
            Ok(value) => return Ok(value),
            Err(error) => {
                let error = masked(error);
                log(
                    env,
                    home,
                    "error",
                    isi,
                    &format!("evaluation failed: {error}"),
                )?;
                errors.push(error);
            }
        }
    }
    if let [error] = errors.as_slice() {
        bail!("{error}");
    }
    bail!(
        "all {} fallback candidates failed:{}",
        errors.len(),
        errors
            .iter()
            .enumerate()
            .map(|(index, error)| format!("\n{}. {error}", index + 1))
            .collect::<String>()
    )
}

pub fn evaluate(source: &str, env: &Json, home: &Path, isi: &str) -> Result<String> {
    run(source, env, home, isi, Mode::Text)
}

/// Errors keep the exit status, stderr and parser text. The source reads as
/// `[credential expression]` and stdout, being the credential, is never shown.
pub fn evaluate_secret(source: &str, env: &Json, home: &Path, isi: &str) -> Result<String> {
    run(source, env, home, isi, Mode::Secret)
}

/// Setup is deferred until connect; preserve shared CEL/Bash/fallback semantics.
pub fn setup(source: &str, env: &Json, home: &Path) -> Result<()> {
    let source = if source.trim_start().starts_with('!') {
        source.to_owned()
    } else {
        format!("! {source}")
    };
    run(&source, env, home, "interpreter", Mode::Setup).map(|_| ())
}

/// DNA candidates are paths by default; quoted fallbacks are literal prompt text.
pub fn dna(source: &str, env: &Json, home: &Path, isi: &str) -> Result<String> {
    run(source, env, home, isi, Mode::Dna)
}

/// Login/webhook entries name deferred app invocations: expand CEL and fallbacks,
/// retain argv quoting, and remove a literal leading `!` marker without running Bash.
pub fn app_command(source: &str, env: &Json, home: &Path) -> Result<String> {
    run(source, env, home, "interpreter", Mode::AppCommand)
}

/// Evaluate every string in JSON-shaped YAML; decoded JSON remains addressable in CEL.
pub fn value(input: &Yaml, env: &Json, home: &Path, isi: &str) -> Result<Json> {
    match input {
        Yaml::String(source) => {
            if let Some(container) = json_container(source) {
                return value(&container, env, home, isi);
            }
            let result = evaluate(source, env, home, isi)?;
            Ok(serde_json::from_str(&result).unwrap_or(Json::String(result)))
        }
        Yaml::Sequence(items) => items
            .iter()
            .enumerate()
            .map(|(index, item)| {
                value(item, env, home, isi).with_context(|| format!("in item {index}"))
            })
            .collect(),
        Yaml::Mapping(items) => {
            let mut result = serde_json::Map::new();
            for (key, item) in items {
                let key = key
                    .as_str()
                    .ok_or_else(|| anyhow!("JSON object keys must be strings, got {key:?}"))?;
                let key = evaluate(key, env, home, isi)?;
                let item = value(item, env, home, isi).with_context(|| format!("in `{key}`"))?;
                if result.insert(key.clone(), item).is_some() {
                    bail!("duplicate evaluated object key: {key}");
                }
            }
            Ok(Json::Object(result))
        }
        Yaml::Tagged(tag) => value(
            &Yaml::String(format!(
                "! {}",
                tag.value.as_str().ok_or_else(|| {
                    anyhow!(
                        "Bash tag {} must contain a string, got {:?}",
                        tag.tag,
                        tag.value
                    )
                })?
            )),
            env,
            home,
            isi,
        ),
        other => serde_json::to_value(other)
            .with_context(|| format!("could not convert YAML {other:?} to JSON")),
    }
}

fn json_container(source: &str) -> Option<Yaml> {
    let parsed: Json = serde_json::from_str(source).ok()?;
    if parsed.is_array() || parsed.is_object() {
        serde_yaml::to_value(parsed).ok()
    } else {
        None
    }
}

pub fn validate_template(source: &str) -> Result<()> {
    validate_template_mode(source, Mode::Text)
}

pub fn validate_app_command(source: &str) -> Result<()> {
    validate_template_mode(source, Mode::AppCommand)
}

/// Like [`validate`], naming each source `[credential expression]` in its errors.
pub fn validate_secret(value: &Yaml) -> Result<()> {
    validate_value(value, Mode::Secret)
}

fn validate_template_mode(source: &str, mode: Mode) -> Result<()> {
    for source in fallbacks(source, mode)? {
        let source = candidate_source(source, mode)?.0;
        for part in parts(&source, mode.secret())? {
            if let Part::Cel(source) = part {
                compile(&source, mode.secret())?;
            }
        }
    }
    Ok(())
}

/// Syntax validation never executes shell commands or requires runtime request data.
pub fn validate(value: &Yaml) -> Result<()> {
    validate_value(value, Mode::Text)
}

fn validate_value(value: &Yaml, mode: Mode) -> Result<()> {
    match value {
        Yaml::String(source) => match json_container(source) {
            Some(container) => validate_value(&container, mode),
            None => validate_template_mode(source, mode),
        },
        Yaml::Sequence(items) => items.iter().enumerate().try_for_each(|(index, item)| {
            validate_value(item, mode).with_context(|| format!("in item {index}"))
        }),
        Yaml::Mapping(items) => items.iter().try_for_each(|(key, value)| {
            validate_value(key, mode)?;
            validate_value(value, mode).with_context(|| match key.as_str() {
                Some(key) => format!("in `{key}`"),
                None => format!("in {key:?}"),
            })
        }),
        Yaml::Tagged(tag) => validate_value(&tag.value, mode),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn ting_list_extensions_compose_and_validate_arguments() {
        let env = json!({"request": {"tings": [
            {"at": 2, "text": "later"}, {"at": 1, "text": "first"},
            {"at": 2, "text": "last"}, {"at": 1, "text": "first"}
        ]}, "t": "outer"});
        assert_eq!(
            cel("request.tings.sortBy(t, t.at).map(t, t.text).distinct().slice(0, 3).reverse().join(' | ')", &env, false).unwrap(),
            json!("last | later | first")
        );
        for (source, expected) in [
            ("['a', 'b'].join()", json!("ab")),
            ("[].sortBy(t, t.missing)", json!([])),
            (
                "request.tings.sortBy(t, t.at).size() == 4 && t == 'outer'",
                json!(true),
            ),
            (
                "[1, 2, 1, {'n': 1}, {'n': 1}].distinct()",
                json!([1, 2, {"n": 1}]),
            ),
            ("[1, [2, [3]], [], 4].flatten()", json!([1, 2, [3], 4])),
            ("[1, [2, [3]], [], 4].flatten(2)", json!([1, 2, 3, 4])),
            ("[1, [2]].flatten(0)", json!([1, [2]])),
            ("[1].slice(1, 1)", json!([])),
        ] {
            assert_eq!(cel(source, &env, false).unwrap(), expected, "{source}");
        }
        for source in [
            "[1].join()",
            "[1].slice(-1, 1)",
            "[1].slice(0, 2)",
            "[1].slice(1, 0)",
            "[1].slice(0u, 1u)",
            "[1].flatten(-1)",
            "[1].reverse(2)",
            "[1].distinct(2)",
            "'abc'.reverse()",
            "[1, 'a'].sortBy(t, t)",
            "[{}].sortBy(t, t)",
            "[null].sortBy(t, t)",
            "[double('NaN')].sortBy(t, t)",
            "[1.0, double('NaN')].sortBy(t, t)",
            "[1].sortBy('t', t)",
            "[1].sortBy(t, t, 2)",
            "[1].sortBy(t, t.missing)",
        ] {
            assert!(cel(source, &env, false).is_err(), "accepted {source}");
        }
    }

    #[test]
    fn real_cel_nested_templates_helpers_and_failures() {
        let dir = tempfile::tempdir().unwrap();
        let env = json!({"request": {"items": [1, 2, 3], "body": "{\"n\":7}"}, "var": {}});
        let eval = |source: &str| evaluate(source, &env, dir.path(), "worker:job");
        assert_eq!(
            eval("{request.items.filter(x, x > 1).map(x, x * 2)}").unwrap(),
            "[4,6]"
        );
        assert_eq!(
            eval("{ {'brace': '}'}['brace'] } and \\{literal\\}").unwrap(),
            "} and {literal}"
        );
        assert_eq!(eval("{to_json(request.body).n + 1}").unwrap(), "8");
        assert_eq!(eval("{'!>>'}").unwrap(), "!>>");
        assert_eq!(eval("  preserve spaces  ").unwrap(), "  preserve spaces  ");
        assert_eq!(
            value(
                &Yaml::String(r#"{"n":"{1 + 2}"}"#.into()),
                &env,
                dir.path(),
                "a"
            )
            .unwrap(),
            json!({"n":3})
        );
        validate(&Yaml::String(r#"{"n":"{1 + 2}"}"#.into())).unwrap();
        assert_eq!(
            value(
                &Yaml::String("{request.body}".into()),
                &env,
                dir.path(),
                "a"
            )
            .unwrap(),
            json!({"n": 7})
        );
        assert_eq!(
            eval("{tz_time('2026-01-01T00:00:00Z', 'Asia/Kolkata')}").unwrap(),
            "05:30:00 01:01:26 Asia/Kolkata"
        );
        assert_eq!(eval("{to_yaml([1, 2])}").unwrap(), "- 1\n- 2\n");
        let request = json!({"type": "new_message", "data": {
            "message": "hola 👋\nsecond line", "sender": {"id": "shubham", "type": "carbon"},
            "attachments": [null, true, 7, 1.5, {"name": "photo"}], "reply_to": null
        }});
        let readable = evaluate(
            "{make_readable(request)}",
            &json!({"request": request}),
            dir.path(),
            "interpreter",
        )
        .unwrap();
        assert_eq!(serde_yaml::from_str::<Json>(&readable).unwrap(), request);
        assert!(readable.contains("sender:\n"));
        assert_eq!(
            eval("{make_readable(request.items)}").unwrap(),
            "- 1\n- 2\n- 3\n"
        );
        for source in ["null", "true", "42", "1.5", "'hola'", "[]", "{}"] {
            assert_eq!(
                eval(&format!("{{make_readable({source})}}")).unwrap(),
                eval(&format!("{{to_yaml({source})}}")).unwrap()
            );
        }
        for when in ["2026-01-01T00:00:00Z", "2026-07-01T12:00:00+05:30"] {
            for zone in ["Asia/Kolkata", "America/New_York"] {
                assert_eq!(
                    eval(&format!("{{convert_time('{when}', '{zone}')}}")).unwrap(),
                    eval(&format!("{{tz_time('{when}', '{zone}')}}")).unwrap()
                );
            }
        }
        assert!(eval("{convert_time('bad', 'UTC')}").is_err());
        assert!(eval("{convert_time('2026-01-01T00:00:00Z', 'invalid')}").is_err());
        assert!(eval("{tz_time('bad', 'UTC')}").is_err());
        assert!(validate_template("{request.type == }").is_err());
        assert!(validate_template("{request.type").is_err());
    }

    #[test]
    fn bash_fallbacks_and_dna_share_environment_without_losing_quoted_separators() {
        let dir = tempfile::tempdir().unwrap();
        let env = serde_json::json!({"var": {"x": 3}, "silicon": {"SILICON_ORG": "test-org"}});
        fs::write(dir.path().join("prompt.md"), "file prompt").unwrap();
        assert_eq!(
            evaluate(
                "{missing.value} !>> ! false !>> ! printf '%s:%s:%s:%s' \"$ISI\" \"$PWD\" {var.x} \"$SILICON_ORG\"",
                &env,
                dir.path(),
                "worker:job"
            )
            .unwrap(),
            format!(
                "worker:job:{}:3:test-org",
                dir.path().canonicalize().unwrap().display()
            )
        );
        assert_eq!(
            evaluate(
                "! printf '%s' '!>>' !>> \"fallback\"",
                &env,
                dir.path(),
                "a"
            )
            .unwrap(),
            "!>>"
        );
        assert_eq!(
            evaluate("! 'printf hello'", &env, dir.path(), "a").unwrap(),
            "hello"
        );
        assert_eq!(
            dna("absent.md !>> prompt.md", &env, dir.path(), "a").unwrap(),
            "file prompt"
        );
        assert_eq!(
            dna("absent.md !>> \"no contacts\"", &env, dir.path(), "a").unwrap(),
            "no contacts"
        );
        assert!(evaluate("! false !>> ! false", &env, dir.path(), "a").is_err());
    }

    #[test]
    fn app_commands_preserve_argv_and_use_fallbacks_without_running_commands() {
        let dir = tempfile::tempdir().unwrap();
        let env = json!({"silicon": {"id": "si:test"}});
        let source = r#""app with spaces" --title "hello {silicon.id}" --literal '!>>'"#;
        validate_app_command(source).unwrap();
        let command = app_command(source, &env, dir.path()).unwrap();
        assert_eq!(
            command,
            r#""app with spaces" --title "hello si:test" --literal '!>>'"#
        );
        assert_eq!(
            shell_words::split(&command).unwrap(),
            [
                "app with spaces",
                "--title",
                "hello si:test",
                "--literal",
                "!>>"
            ]
        );
        assert_eq!(
            app_command("{missing.command} !>> {error} !>> ! dm", &env, dir.path()).unwrap(),
            "! dm"
        );
        assert_eq!(
            app_command("'' !>> ! dm", &env, dir.path()).unwrap(),
            "! dm"
        );
        assert_eq!(
            app_command("! false !>> dm", &env, dir.path()).unwrap(),
            "! false"
        );
        assert!(app_command("! touch should-not-run", &env, dir.path()).is_ok());
        assert!(!dir.path().join("should-not-run").exists());
        assert!(app_command("{missing.command} !>> {error}", &env, dir.path()).is_err());
        assert!(app_command("dm 'unclosed", &env, dir.path()).is_err());
    }

    #[test]
    fn credential_expressions_and_compile_diagnostics_do_not_disclose_secrets() {
        let dir = tempfile::tempdir().unwrap();
        let env = json!({"silicon": {"token": "private-token-value",
            "app_configs": {"app": {"credentials": ["private-application-value"]}},
            "space_station": {"table_key": "private-table-value"}}});
        let log_path = dir.path().join(".silicon/silicon.log");
        assert_eq!(
            evaluate_secret(
                "! printf new-private-credential",
                &env,
                dir.path(),
                "interpreter"
            )
            .unwrap(),
            "new-private-credential"
        );
        // The reason survives; the source and stdout (the credential itself) never do.
        for (source, reasons) in [
            (
                "! printf new-private-credential; printf 'vault: permission denied' >&2; exit 7",
                &[
                    "`[credential expression]` failed: exit status: 7",
                    "\nstderr:\nvault: permission denied\n",
                    "stdout: (withheld; it is the credential)",
                ][..],
            ),
            (
                "{missing['new-private-credential']}",
                &["CEL `[credential expression]`: Undeclared reference to 'missing'"][..],
            ),
            (
                "{new-private-credential +* 2}",
                &["invalid CEL `[credential expression]`: ERROR: <input>:1:25: Syntax error: extraneous input '*'"][..],
            ),
            (
                "{new-private-credential + }",
                &["invalid CEL `[credential expression]`: the CEL parser panicked instead of reporting the syntax error: internal error: entered unreachable code"][..],
            ),
            (
                "new-private-credential !>>",
                &["empty fallback candidate in `[credential expression]`"][..],
            ),
            (
                "! printf 'new-private-credential\\377'; printf 'vault: bad encoding' >&2",
                &[
                    "`[credential expression]` printed a credential that is not UTF-8: invalid utf-8 sequence",
                    "\nexit status: 0\nstderr:\nvault: bad encoding\nstdout: (withheld; it is the credential)",
                ][..],
            ),
            // Parser and CEL text quote source tokens; a literal credential among them is masked.
            (
                "{silicon.token ?? 'sk_live_privatevalue'}",
                &["\nERROR: <input>:1:16: Syntax error: extraneous input '?' expecting"][..],
            ),
            (
                "{'sk' 'sk_live_privatevalue'}",
                &["Syntax error: extraneous input ''[redacted]'' expecting <EOF>"][..],
            ),
            (
                "{'sk_live_privatevalue' + 1}",
                &["CEL `[credential expression]`: Unsupported binary operator 'add': String(\"[redacted]\"), Int(1)"][..],
            ),
            (
                "{sk_live_privatevalue}",
                &["CEL `[credential expression]`: Undeclared reference to '[redacted]'"][..],
            ),
        ] {
            let error = format!(
                "{:#}",
                evaluate_secret(source, &env, dir.path(), "interpreter").unwrap_err()
            );
            for reason in reasons {
                assert!(error.contains(reason), "{source}: missing {reason:?} in {error}");
            }
            assert!(!error.contains("new-private-credential"), "{error}");
            assert!(!error.contains("sk_live_privatevalue"), "{error}");
        }
        let error = format!(
            "{:#}",
            validate_secret(&Yaml::String("{'new-private-credential' +* 2}".into())).unwrap_err()
        );
        assert!(
            error.starts_with(
                "invalid CEL `[credential expression]`: ERROR: <input>:1:27: Syntax error: extraneous input '*'"
            ),
            "{error}"
        );
        assert!(!error.contains("new-private-credential"), "{error}");
        assert_eq!(
            evaluate_secret(
                "! false !>> ! printf fallback-private-credential",
                &env,
                dir.path(),
                "interpreter"
            )
            .unwrap(),
            "fallback-private-credential"
        );
        for (source, reason) in [
            (
                "! printf '{silicon.token}' >&2; exit 1",
                "failed: exit status: 1\nstderr:\n[redacted]\nstdout: (empty)",
            ),
            (
                "! printf '{silicon.space_station.table_key}' >&2; exit 1",
                "failed: exit status: 1\nstderr:\n[redacted]\nstdout: (empty)",
            ),
            (
                "! printf '{silicon.app_configs['app'].credentials[0]}' >&2; exit 1",
                "failed: exit status: 1\nstderr:\n[redacted]\nstdout: (empty)",
            ),
            (
                "{missing['private-application-value']}",
                "Undeclared reference to 'missing'",
            ),
            (
                "{to_json(silicon.token)}",
                "Error executing function 'to_json'",
            ),
        ] {
            let error = format!(
                "{:#}",
                evaluate(source, &env, dir.path(), "interpreter").unwrap_err()
            );
            assert!(
                error.contains(reason),
                "{source}: missing {reason:?} in {error}"
            );
            assert!(!error.contains("private-token-value"), "{error}");
            assert!(!error.contains("private-table-value"), "{error}");
            assert!(!error.contains("private-application-value"), "{error}");
        }
        let logs = fs::read_to_string(log_path).unwrap();
        for secret in [
            "private-token-value",
            "private-table-value",
            "private-application-value",
            "new-private-credential",
            "fallback-private-credential",
            "sk_live_privatevalue",
        ] {
            assert!(!logs.contains(secret), "{logs}");
        }
        assert!(logs.contains("running: [compile-time expression]"));
        assert!(logs.contains("running: [credential expression]"));
        assert!(logs.contains("finished:"));
        evaluate(
            "! printf runtime-visible",
            &json!({"silicon": {}}),
            dir.path(),
            "worker",
        )
        .unwrap();
        assert!(fs::read_to_string(dir.path().join(".silicon/silicon.log"))
            .unwrap()
            .contains("running: printf runtime-visible"));
    }

    fn fake_app(home: &Path, name: &str, script: &str) {
        use std::os::unix::fs::PermissionsExt;
        let bin = home.join(".silicon/bin");
        fs::create_dir_all(&bin).unwrap();
        let path = bin.join(name);
        fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
    }

    #[test]
    fn failing_tools_report_status_stderr_stdout_and_every_candidate() {
        let dir = tempfile::tempdir().unwrap();
        let env = json!({"silicon": {}});
        fake_app(
            dir.path(),
            "ledger",
            "echo '{\"ok\":false,\"code\":\"quota\"}'\necho 'ledger: quota exceeded' >&2\necho '  at line 2' >&2\nexit 4",
        );
        let streams = "failed: exit status: 4\nstderr:\nledger: quota exceeded\n  at line 2\nstdout:\n{\"ok\":false,\"code\":\"quota\"}";
        let error = format!(
            "{:#}",
            evaluate("! ledger --json", &env, dir.path(), "worker").unwrap_err()
        );
        assert_eq!(error, format!("`bash -c 'ledger --json'` {streams}"));
        // Structured values say which key or item failed, then the tool's own words.
        let error = format!(
            "{:#}",
            value(
                &serde_yaml::from_str("{total: [ok, '! ledger --json']}").unwrap(),
                &env,
                dir.path(),
                "worker"
            )
            .unwrap_err()
        );
        assert_eq!(
            error,
            format!("in `total`: in item 1: `bash -c 'ledger --json'` {streams}")
        );
        for (source, reason) in [
            (
                "{tz_time('2026-01-01T00:00:00Z', 'Mars/Base')}",
                "\"Mars/Base\" is not an IANA time zone name: failed to parse timezone",
            ),
            (
                "{'abc'.reverse()}",
                "expected a list receiver, got String(\"abc\")",
            ),
            (
                "{[1].flatten(-1)}",
                "flatten depth must not be negative, got -1",
            ),
            (
                "{[1].sortBy(t, t, 2)}",
                "expected sortBy(binding, key), got 3 arguments",
            ),
        ] {
            let error = format!(
                "{:#}",
                evaluate(source, &env, dir.path(), "worker").unwrap_err()
            );
            assert!(
                error.contains(reason),
                "{source}: missing {reason:?} in {error}"
            );
        }
        // Every fallback candidate keeps its own complete reason.
        let error = format!(
            "{:#}",
            evaluate(
                "! ledger --json !>> {missing.value}",
                &env,
                dir.path(),
                "worker"
            )
            .unwrap_err()
        );
        assert_eq!(
            error,
            format!(
                "all 2 fallback candidates failed:\n1. `bash -c 'ledger --json'` {streams}\n2. CEL `missing.value`: Undeclared reference to 'missing'"
            )
        );
        let logs = fs::read_to_string(dir.path().join(".silicon/silicon.log")).unwrap();
        assert!(
            logs.contains(&format!(
                "evaluation failed: `bash -c 'ledger --json'` {}",
                streams.replace('\n', "\\n")
            )),
            "{logs}"
        );
        let error = format!(
            "{:#}",
            evaluate("! printf '\\377'", &env, dir.path(), "worker").unwrap_err()
        );
        assert!(
            error.contains("printed output that is not UTF-8: invalid utf-8 sequence"),
            "{error}"
        );
        let error = format!(
            "{:#}",
            dna("absent.md", &env, dir.path(), "worker").unwrap_err()
        );
        assert!(
            error.contains(&format!(
                "could not read DNA file {}: No such file or directory",
                dir.path().join("absent.md").display()
            )),
            "{error}"
        );
        let error = format!(
            "{:#}",
            evaluate("{1 +* 2}", &env, dir.path(), "worker").unwrap_err()
        );
        assert!(
            error.starts_with(
                "invalid CEL `1 +* 2`: ERROR: <input>:1:4: Syntax error: extraneous input '*'"
            ) && error.ends_with("\n| 1 +* 2\n| ...^"),
            "{error}"
        );
        // cel-parser panics on some malformed input; its own message still reaches the reader.
        let error = format!(
            "{:#}",
            evaluate("{1 + }", &env, dir.path(), "worker").unwrap_err()
        );
        assert_eq!(
            error,
            "invalid CEL `1 + `: the CEL parser panicked instead of reporting the syntax error: \
             internal error: entered unreachable code: \
             should have been properly implemented by generated context when reachable"
        );
        let error = format!(
            "{:#}",
            app_command("dm 'unclosed", &env, dir.path()).unwrap_err()
        );
        assert!(
            error.starts_with("invalid app command quoting in `dm 'unclosed`: "),
            "{error}"
        );
        assert_eq!(
            panic_message(&*std::panic::catch_unwind(|| panic!("parser bug")).unwrap_err()),
            "parser bug"
        );
        let detail = "index 3";
        assert_eq!(
            panic_message(
                &*std::panic::catch_unwind(|| panic!("out of range: {detail}")).unwrap_err()
            ),
            "out of range: index 3"
        );
    }
}
