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
/// (the same floor local logs use for secrets) are masked; the message itself stays.
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
    context.add_function(
        "groupBy",
        |ctx: &FunctionContext,
         This(items): This<Arc<Vec<Value>>>,
         Identifier(binding): Identifier,
         key: IdedExpr|
         -> Result<Value, ExecutionError> {
            if ctx.args.len() != 2 {
                return Err(ctx.error(format!(
                    "expected groupBy(binding, key), got {} arguments",
                    ctx.args.len()
                )));
            }
            let mut scope = ctx.ptx.new_inner_scope();
            let mut groups: Vec<(Value, Vec<Value>)> = Vec::new();
            // ponytail: quadratic CEL equality scan; hash canonical keys if batches grow large.
            for item in items.iter() {
                scope.add_variable_from_value(binding.as_str(), item.clone());
                let key = scope.resolve(&key)?;
                if let Some((_, items)) = groups.iter_mut().find(|(existing, _)| existing == &key) {
                    items.push(item.clone());
                } else {
                    groups.push((key, vec![item.clone()]));
                }
            }
            Ok(groups
                .into_iter()
                .map(|(key, items)| {
                    Value::from(HashMap::from([
                        ("key".to_owned(), key),
                        ("items".to_owned(), Value::from(items)),
                    ]))
                })
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
    let secrets = crate::redaction::silicon_secrets(&env["silicon"]);
    crate::redaction::redact_text(message, &secrets)
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
    let withheld = Output {
        stdout: b"(withheld; it is the credential)".to_vec(),
        ..output.clone()
    };
    if problem.trim() == "failed:" {
        failure::command(home, CREDENTIAL, &withheld, &[])
    } else {
        failure::answer(home, CREDENTIAL, problem.trim(), &withheld, &[])
    }
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
        crate::auth::disable_ting_reporting(home)?;
        let mut bash = crate::command("bash", home);
        bash.arg("-c").arg(command).env("ISI", isi);
        let output =
            bounded(&mut bash, mode).map_err(|error| failure::spawn(home, &name, &error))?;
        // A script stopped at its time limit still shows what it printed before it was
        // stopped, and the closing `silicon: stopped after` line says why it ended.
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

/// The interpreter bounds every expression so one hung script cannot hold a lock or a
/// queue. A CLI command such as `silicon compile` keeps the terminal, so a credential
/// command there can still prompt.
fn bounded(bash: &mut std::process::Command, mode: Mode) -> std::io::Result<Output> {
    if crate::process_role() != "daemon" {
        return crate::process::Starting::output_retrying(bash);
    }
    let limit = if matches!(mode, Mode::Setup) {
        crate::process::Limit::Setup
    } else {
        crate::process::Limit::Expression
    };
    crate::process::output(bash, limit)
}

/// One failing candidate is the error; several are listed in the order they were tried.
/// Each error is masked once, before it is logged, so the log and the caller read the same text.
fn run(source: &str, env: &Json, home: &Path, isi: &str, mode: Mode) -> Result<String> {
    let masked =
        |error: anyhow::Error| failure::mask(home, &redact(&failure::report(&error), env), &[]);
    let mut errors = Vec::new();
    for source in fallbacks(source, mode).map_err(|error| failure::received(masked(error)))? {
        match candidate(source, env, home, isi, mode) {
            Ok(value) => return Ok(value),
            Err(error) => {
                let error = masked(error);
                log(
                    env,
                    home,
                    "error",
                    isi,
                    &format!("{error}\nVia: evaluation failed"),
                )?;
                errors.push(error);
            }
        }
    }
    if let [error] = errors.as_slice() {
        return Err(failure::received(error.clone()));
    }
    Err(
        failure::received(errors.join("\n\nNext fallback candidate:\n"))
            .context(format!("all {} fallback candidates failed", errors.len())),
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

/// Reuse the evaluator's quoting and CEL-aware fallback boundaries for Starter file paths.
pub(crate) fn dna_sources(source: &str) -> Result<Vec<&str>> {
    fallbacks(source, Mode::Dna)
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
mod reporting_tests {
    use super::*;

    #[test]
    fn credential_failure_keeps_diagnostic_but_withholds_command_and_stdout() {
        let home = std::env::temp_dir().join(format!("silicon-secret-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&home).unwrap();
        let error = evaluate_secret(
            "! printf opaque-secret-to-withhold; printf 'credential lookup failed\\n' >&2; exit 9",
            &serde_json::json!({}),
            &home,
            "test",
        )
        .unwrap_err();
        let report = failure::report(&error);
        assert!(report.starts_with("stderr:\ncredential lookup failed"));
        assert!(report.contains("stdout:\n(withheld; it is the credential)"));
        assert!(report.contains("Source: `[credential expression]`"));
        assert!(report.contains("Status: exit status: 9"));
        assert!(!report.contains("Meaning:"));
        let log = std::fs::read_to_string(home.join(".silicon/silicon.log")).unwrap();
        assert!(!report.contains("opaque-secret-to-withhold"));
        assert!(!log.contains("opaque-secret-to-withhold"));
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn bash_disables_legacy_ting_reporting_before_running() {
        let home = std::env::temp_dir().join(format!("silicon-bash-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&home).unwrap();
        let output = evaluate(
            "! cat .ting/settings.json",
            &serde_json::json!({}),
            &home,
            "test",
        )
        .unwrap();
        let settings: Json = serde_json::from_str(&output).unwrap();
        assert_eq!(settings["telemetry"], false);
        std::fs::remove_dir_all(home).unwrap();
    }
}
