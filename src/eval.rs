//! Shared CEL → Bash → string evaluation. Configuration is trusted executable input.
use anyhow::{anyhow, bail, Context as _, Result};
use cel_interpreter::{extractors::This, Context, ExecutionError, Program, Value};
use chrono::DateTime;
use chrono_tz::Tz;
use serde_json::Value as Json;
use serde_yaml::Value as Yaml;
use std::{collections::HashMap, fs, panic::AssertUnwindSafe, path::Path, sync::Arc};

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

fn compile(source: &str) -> Result<Program> {
    // cel-parser 0.10's error recovery can panic on malformed input; contain that upstream bug.
    std::panic::catch_unwind(|| Program::compile(source))
        .map_err(|_| anyhow!("invalid CEL `{source}` (parser rejected malformed input)"))?
        .map_err(|e| anyhow!("invalid CEL `{source}`: {e}"))
}

fn context(env: &Json) -> Result<Context<'static>> {
    let mut context = Context::default();
    for (name, value) in env
        .as_object()
        .ok_or_else(|| anyhow!("CEL environment must be an object"))?
    {
        context.add_variable_from_value(name, cel_value(value.clone()));
    }
    context.add_function(
        "tz_time",
        |when: Arc<String>, zone: Arc<String>| -> Result<Value, ExecutionError> {
            let time = DateTime::parse_from_rfc3339(&when)
                .map_err(|e| ExecutionError::function_error("tz_time", e))?;
            let tz: Tz = zone
                .parse()
                .map_err(|e| ExecutionError::function_error("tz_time", e))?;
            Ok(format!(
                "{} {}",
                time.with_timezone(&tz).format("%H:%M:%S %d:%m:%y"),
                zone
            )
            .into())
        },
    );
    context.add_function(
        "to_json",
        |source: Arc<String>| -> Result<Value, ExecutionError> {
            let value: Json = serde_json::from_str(&source)
                .map_err(|e| ExecutionError::function_error("to_json", e))?;
            Ok(cel_value(value))
        },
    );
    context.add_function("to_yaml", |value: Value| -> Result<Value, ExecutionError> {
        let json = value
            .json()
            .map_err(|e| ExecutionError::function_error("to_yaml", e))?;
        serde_yaml::to_string(&json)
            .map(Value::from)
            .map_err(|e| ExecutionError::function_error("to_yaml", e))
    });
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
    Ok(context)
}

fn cel(source: &str, env: &Json) -> Result<Json> {
    let program = compile(source)?;
    let context = context(env)?;
    let value = std::panic::catch_unwind(AssertUnwindSafe(|| program.execute(&context)))
        .map_err(|_| anyhow!("CEL evaluation failed safely: `{source}`"))?
        .with_context(|| format!("CEL `{source}`"))?;
    value.json().map_err(|e| anyhow!(e.to_string()))
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
        bail!("empty fallback candidate");
    }
    Ok(result)
}

fn unquote(source: &str) -> Result<(String, bool)> {
    if source.starts_with('"') && source.ends_with('"') && source.len() >= 2 {
        return Ok((
            serde_json::from_str::<String>(source).context("invalid quoted fallback")?,
            true,
        ));
    }
    if source.starts_with('\'') && source.ends_with('\'') && source.len() >= 2 {
        return Ok((source[1..source.len() - 1].replace("''", "'"), true));
    }
    Ok((source.to_owned(), false))
}

fn interpolate(source: &str, env: &Json) -> Result<String> {
    template(source)?
        .into_iter()
        .map(|part| match part {
            Part::Text(text) => Ok(text),
            Part::Cel(source) => cel(&source, env).map(text),
        })
        .collect()
}

#[derive(Clone, Copy)]
enum Mode {
    Text,
    Dna,
    AppCommand,
}

fn candidate_source(source: &str, mode: Mode) -> Result<(String, bool)> {
    let (mut source, quoted) = if matches!(mode, Mode::AppCommand) {
        (source.to_owned(), false)
    } else {
        unquote(source)?
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
    let expanded = interpolate(&source, env)?;
    if matches!(mode, Mode::AppCommand) {
        let command = expanded.trim();
        let command = if source.trim_start().starts_with('!') {
            command.strip_prefix('!').unwrap_or(command).trim_start()
        } else {
            command
        };
        let argv = shell_words::split(command).context("invalid app command quoting")?;
        if argv.first().is_none_or(|arg| arg.is_empty()) || command.contains(['\n', '\0']) {
            bail!("app command must contain an executable and valid single-line arguments");
        }
        return Ok(command.to_owned());
    }
    // A request value beginning with `!` is data, never an implicit shell command.
    if let Some(command) = expanded
        .strip_prefix('!')
        .filter(|_| !quoted && source.starts_with('!'))
    {
        let output = crate::command("bash", home)
            .arg("-c")
            .arg(command.trim_start())
            .env("ISI", isi)
            .output()
            .context("could not start Bash")?;
        if !output.status.success() {
            bail!(
                "Bash exited with {}: {}",
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }
        return Ok(String::from_utf8(output.stdout)
            .context("Bash output is not UTF-8")?
            .trim_end_matches(['\r', '\n'])
            .to_owned());
    }
    if matches!(mode, Mode::Dna) && !quoted {
        return fs::read_to_string(home.join(expanded)).context("could not read DNA file");
    }
    Ok(expanded)
}

fn run(source: &str, env: &Json, home: &Path, isi: &str, mode: Mode) -> Result<String> {
    let mut errors = Vec::new();
    for source in fallbacks(source, mode)? {
        match candidate(source, env, home, isi, mode) {
            Ok(value) => return Ok(value),
            Err(error) => {
                let message = format!("evaluation failed: {error:#}");
                crate::log_line(home, "error", isi, &message)?;
                errors.push(message);
            }
        }
    }
    bail!("all evaluation candidates failed: {}", errors.join("; "))
}

pub fn evaluate(source: &str, env: &Json, home: &Path, isi: &str) -> Result<String> {
    run(source, env, home, isi, Mode::Text)
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
            .map(|item| value(item, env, home, isi))
            .collect(),
        Yaml::Mapping(items) => {
            let mut result = serde_json::Map::new();
            for (key, item) in items {
                let key = key
                    .as_str()
                    .ok_or_else(|| anyhow!("JSON object keys must be strings"))?;
                let key = evaluate(key, env, home, isi)?;
                if result
                    .insert(key.clone(), value(item, env, home, isi)?)
                    .is_some()
                {
                    bail!("duplicate evaluated object key: {key}");
                }
            }
            Ok(Json::Object(result))
        }
        Yaml::Tagged(tag) => value(
            &Yaml::String(format!(
                "! {}",
                tag.value
                    .as_str()
                    .ok_or_else(|| anyhow!("Bash tag must contain a string"))?
            )),
            env,
            home,
            isi,
        ),
        other => Ok(serde_json::to_value(other)?),
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

fn validate_template_mode(source: &str, mode: Mode) -> Result<()> {
    for source in fallbacks(source, mode)? {
        let source = candidate_source(source, mode)?.0;
        for part in template(&source)? {
            if let Part::Cel(source) = part {
                compile(&source)?;
            }
        }
    }
    Ok(())
}

/// Syntax validation never executes shell commands or requires runtime request data.
pub fn validate(value: &Yaml) -> Result<()> {
    match value {
        Yaml::String(source) => match json_container(source) {
            Some(container) => validate(&container),
            None => validate_template(source),
        },
        Yaml::Sequence(items) => items.iter().try_for_each(validate),
        Yaml::Mapping(items) => items.iter().try_for_each(|(key, value)| {
            validate(key)?;
            validate(value)
        }),
        Yaml::Tagged(tag) => validate(&tag.value),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn real_cel_nested_templates_helpers_and_failures() {
        let dir = tempfile::tempdir().unwrap();
        let env = json!({"request": {"items": [1, 2, 3], "body": "{\"n\":7}"}, "var": {}});
        let eval = |source| evaluate(source, &env, dir.path(), "worker:job");
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
        assert!(eval("{tz_time('bad', 'UTC')}").is_err());
        assert!(validate_template("{request.type == }").is_err());
        assert!(validate_template("{request.type").is_err());
    }

    #[test]
    fn bash_fallbacks_and_dna_share_environment_without_losing_quoted_separators() {
        let dir = tempfile::tempdir().unwrap();
        let env = serde_json::json!({"var": {"x": 3}});
        fs::write(dir.path().join("prompt.md"), "file prompt").unwrap();
        assert_eq!(
            evaluate(
                "{missing.value} !>> ! false !>> ! printf '%s:%s:%s' \"$ISI\" \"$PWD\" {var.x}",
                &env,
                dir.path(),
                "worker:job"
            )
            .unwrap(),
            format!(
                "worker:job:{}:3",
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
        let env = json!({"silicon": {"id": "test:org"}});
        let source = r#""app with spaces" --title "hello {silicon.id}" --literal '!>>'"#;
        validate_app_command(source).unwrap();
        let command = app_command(source, &env, dir.path()).unwrap();
        assert_eq!(
            command,
            r#""app with spaces" --title "hello test:org" --literal '!>>'"#
        );
        assert_eq!(
            shell_words::split(&command).unwrap(),
            [
                "app with spaces",
                "--title",
                "hello test:org",
                "--literal",
                "!>>"
            ]
        );
        assert_eq!(
            app_command("{missing.command} !>> {error} !>> ! dm", &env, dir.path()).unwrap(),
            "dm"
        );
        assert_eq!(app_command("'' !>> ! dm", &env, dir.path()).unwrap(), "dm");
        assert_eq!(
            app_command("! false !>> dm", &env, dir.path()).unwrap(),
            "false"
        );
        assert!(app_command("! touch should-not-run", &env, dir.path()).is_ok());
        assert!(!dir.path().join("should-not-run").exists());
        assert!(app_command("{missing.command} !>> {error}", &env, dir.path()).is_err());
        assert!(app_command("dm 'unclosed", &env, dir.path()).is_err());
    }
}
