//! Ting owns transport retries; the interpreter durably accepts batches before running flows.
use crate::{auth, config::Config, failure, state};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Output,
    sync::Mutex,
};

// ponytail: serialize journal I/O across connection generations; use per-path locks at high volume.
static INBOXES: Mutex<Vec<PathBuf>> = Mutex::new(Vec::new());

pub struct Inbox {
    directory: PathBuf,
}

#[derive(Default, Serialize, Deserialize)]
struct Queue {
    seen: BTreeMap<String, i64>,
    pending: Vec<Value>,
}

impl Inbox {
    pub fn new(cfg: &Config) -> Result<Self> {
        let root = cfg.home.join(".silicon/ting");
        if fs::symlink_metadata(&root).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
            let target = fs::read_link(&root).map_or_else(
                |error| format!("an unreadable target: {error}"),
                |target| target.display().to_string(),
            );
            bail!(
                "Ting state directory {} must not be a symlink; it points to {target}",
                root.display()
            );
        }
        let expected = cfg.silicon.id.as_deref();
        match fs::read_dir(&root) {
            Ok(entries) => {
                let mut stray = Vec::new();
                for entry in entries {
                    let entry = entry.with_context(|| format!("list {}", root.display()))?;
                    let path = entry.path();
                    let kind = entry
                        .file_type()
                        .with_context(|| format!("inspect {}", path.display()))?;
                    if !kind.is_dir() {
                        let kind = if kind.is_symlink() {
                            "a symlink"
                        } else {
                            "not a directory"
                        };
                        stray.push(format!("{} is {kind}", path.display()));
                    } else if entry.file_name().to_str() != expected {
                        stray.push(format!(
                            "{} belongs to Silicon ID {:?}",
                            path.display(),
                            entry.file_name()
                        ));
                    }
                }
                if !stray.is_empty() {
                    bail!(
                        "Ting state belongs to a different Silicon ID or is not an ordinary directory; {} may hold only this Silicon's directory {}, but:\n{}\nStop the interpreter and follow docs/PUBLIC-IDENTIFIER-MIGRATION.md before reconnecting",
                        root.display(),
                        expected.map_or("(this Silicon has no ID)".to_owned(), |id| format!("{id:?}")),
                        stray.join("\n")
                    );
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| {
                    format!("inspect retained Ting namespaces in {}", root.display())
                })
            }
        }
        Ok(Self {
            directory: cfg
                .home
                .join(".silicon/ting")
                .join(expected.unwrap_or_default())
                .join(cfg.silicon.silicon_org.as_deref().unwrap_or_default()),
        })
    }

    fn read(&self) -> Result<Queue> {
        let path = self.directory.join("inbox.json");
        match fs::read(&path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .with_context(|| format!("invalid Ting inbox {}", path.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Queue::default()),
            Err(e) => Err(e).with_context(|| format!("read Ting inbox {}", path.display())),
        }
    }

    pub fn accept(&self, request: Value) -> Result<()> {
        let tings = match request.get("tings") {
            Some(Value::Array(tings)) => tings,
            Some(other) => bail!("Ting requires a tings array; `tings` was {other}"),
            None => match &request {
                Value::Object(fields) => bail!(
                    "Ting requires a tings array; the request has no `tings` field, only [{}]",
                    fields.keys().cloned().collect::<Vec<_>>().join(", ")
                ),
                other => bail!("Ting requires a tings array; the request was {other}"),
            },
        };
        let size = serde_json::to_vec(&request)?.len();
        if tings.is_empty() || tings.len() > 100 || size > 1024 * 1024 {
            bail!(
                "Ting batches require 1–100 tings and at most 1 MiB; this batch has {} tings in {size} bytes",
                tings.len()
            );
        }
        let problems: Vec<_> = tings
            .iter()
            .enumerate()
            .flat_map(|(index, ting)| problems(index, ting))
            .collect();
        if !problems.is_empty() {
            bail!(
                "each ting requires id:string, type:string, data:object, metadata:object; rejected the whole batch:\n{}",
                problems.join("\n")
            );
        }
        let _guard = INBOXES.lock().unwrap();
        let mut queue = self.read()?;
        let now = chrono::Utc::now().timestamp();
        // Ting retains at most three calendar months; keep deduplication longer than that.
        queue
            .seen
            .retain(|_, accepted| now - *accepted < 100 * 86400);
        let fresh: Vec<_> = tings
            .iter()
            .filter(|ting| {
                let id = ting["id"].as_str().unwrap();
                if queue.seen.contains_key(id) {
                    false
                } else {
                    queue.seen.insert(id.to_owned(), now);
                    true
                }
            })
            .cloned()
            .collect();
        if !fresh.is_empty() {
            queue
                .pending
                .push(json!({"id":uuid::Uuid::new_v4(),"request":{"tings":fresh}}));
            // ponytail: rewrite one private journal; use SQLite if inbox volume makes this costly.
            state::write_json(&self.directory.join("inbox.json"), &queue)
                .context("the Ting batch was not accepted, so Ting must deliver it again")?;
        }
        Ok(())
    }

    pub fn process(&self, run: impl FnOnce(Value) -> Result<()>) -> Result<()> {
        let batch = {
            let mut active = INBOXES.lock().unwrap();
            if active.contains(&self.directory) {
                return Ok(());
            }
            let Some(batch) = self.read()?.pending.into_iter().next() else {
                return Ok(());
            };
            active.push(self.directory.clone());
            batch
        };
        // Acceptance stays available while the flow runs. Replacement connections wait
        // for this generation's flow to finish before attempting the same pending batch.
        let result = run(batch["request"].clone());
        let mut active = INBOXES.lock().unwrap();
        let result = result.and_then(|()| {
            self.remove(&batch["id"]).context(
                "the flow finished, but its batch could not be removed from the Ting inbox, so it will run again",
            )
        });
        active.retain(|path| path != &self.directory);
        result
    }

    fn remove(&self, batch: &Value) -> Result<()> {
        let mut queue = self.read()?;
        queue.pending.retain(|pending| &pending["id"] != batch);
        state::write_json(&self.directory.join("inbox.json"), &queue)
    }

    fn hook(&self) -> Result<Option<String>> {
        let path = self.directory.join("hook.json");
        match fs::read(&path) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes).with_context(|| {
                format!(
                    "invalid Ting hook ID in {}: {}",
                    path.display(),
                    String::from_utf8_lossy(&bytes).trim_end()
                )
            })?)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e).with_context(|| format!("read Ting hook ID {}", path.display())),
        }
    }

    fn save_hook(&self, id: &str) -> Result<()> {
        state::write_json(&self.directory.join("hook.json"), &id)
            .with_context(|| format!("save Ting hook ID {id}"))
    }

    /// What a reconnect will do after a failed registration, for the error that reports it.
    fn reuse(&self) -> String {
        let path = self.directory.join("hook.json");
        match self.hook() {
            Ok(Some(id)) => format!(
                "the saved hook {id} ({}) will be reused on reconnect",
                path.display()
            ),
            Ok(None) => {
                "no hook ID is saved, so reconnect will look for it in Ting's registration list"
                    .to_owned()
            }
            Err(error) => format!("the saved hook ID could not be read back: {error:#}"),
        }
    }

    pub fn register(&self, cfg: &Config, url: &str) -> Result<()> {
        let home = &cfg.home;
        let mut id = self.hook()?;
        if id.is_none() {
            // Recover a completed creation if the interpreter lost its CLI response.
            id = listed(home, url, &self.directory.join("hook.json"))
                .with_context(|| format!("could not inspect Ting registrations for {url}"))?;
            if let Some(id) = &id {
                self.save_hook(id)?;
            }
        }
        self.connect(home, url, id.as_deref()).with_context(|| {
            format!(
                "Ting webhook registration for {url} failed; {}",
                self.reuse()
            )
        })
    }

    fn connect(&self, home: &Path, url: &str, id: Option<&str>) -> Result<()> {
        let saved = self.directory.join("hook.json");
        let mut args = vec!["webhook", url, "--json"];
        if let Some(id) = id {
            args.extend(["--id", id]);
        }
        let (command, result) = ting(home, &args)?;
        let differs = |returned: &str| {
            format!(
                "returned webhook ID {returned:?}, but this interpreter retains {:?} in {}",
                id.unwrap_or_default(),
                saved.display()
            )
        };
        if !result.status.success() {
            // Ting names a hook it created but could not confirm in its JSON stderr.
            // Any other stderr is not an ID and is reported verbatim below.
            let answer = serde_json::from_slice::<Value>(&result.stderr).unwrap_or_default();
            let error = failure::command(home, &command, &result, &[]);
            let Some(returned) = answer
                .pointer("/error/details/webhook_id")
                .and_then(Value::as_str)
            else {
                return Err(error);
            };
            if id.is_some_and(|id| id != returned) {
                return Err(error.context(format!("Ting {}", differs(returned))));
            }
            // A failed save rides along; Ting's own failure stays the root cause.
            return Err(match self.save_hook(returned) {
                Ok(()) => error,
                Err(save) => error.context(format!(
                    "Ting named webhook {returned:?} in its failure, but this interpreter could not retain that ID: {save:#}"
                )),
            });
        }
        let wrong = |problem: &str| failure::answer(home, &command, problem, &result, &[]);
        let answer: Value = serde_json::from_slice(&result.stdout)
            .map_err(|error| wrong(&format!("returned invalid JSON: {error}")))?;
        let returned = answer["id"]
            .as_str()
            .ok_or_else(|| wrong(&format!("returned no webhook `id` (id: {})", answer["id"])))?;
        if id.is_some_and(|id| id != returned) {
            return Err(wrong(&differs(returned)));
        }
        if let Err(save) = self.save_hook(returned) {
            return Err(wrong(&format!(
                "returned webhook ID {returned:?}, but this interpreter could not retain it: {save:#}"
            )));
        }
        if answer["state"] != "connected" {
            return Err(wrong(&format!(
                "returned webhook state {} instead of \"connected\"",
                answer["state"]
            )));
        }
        Ok(())
    }

    pub fn unhook(&self, cfg: &Config) -> Result<()> {
        if let Some(id) = self.hook()? {
            ting(&cfg.home, &["unhook", &id, "--json"])
                .and_then(|(command, result)| {
                    if result.status.success() {
                        Ok(())
                    } else {
                        Err(failure::command(&cfg.home, &command, &result, &[]))
                    }
                })
                .with_context(|| {
                    format!(
                        "Ting webhook removal failed; its stable ID {id} is retained in {}",
                        self.directory.join("hook.json").display()
                    )
                })?;
        }
        Ok(())
    }
}

/// One Ting CLI call, returned with the command line that names it in errors.
fn ting(home: &Path, args: &[&str]) -> Result<(String, Output)> {
    let command = failure::argv("ting", args);
    let output = auth::run_app(home, "ting", args)?;
    Ok((command, output))
}

/// The hook Ting lists for `url`, reading every page so duplicates are all named.
fn listed(home: &Path, url: &str, saved: &Path) -> Result<Option<String>> {
    let mut found = Vec::new();
    let mut cursor = None::<String>;
    let mut pages = BTreeSet::new();
    loop {
        let mut args = vec!["webhook", "list", "--limit", "100", "--json"];
        if let Some(cursor) = &cursor {
            args.extend(["--cursor", cursor]);
        }
        let (command, result) = ting(home, &args)?;
        if !result.status.success() {
            return Err(failure::command(home, &command, &result, &[]));
        }
        let wrong = |problem: &str| failure::answer(home, &command, problem, &result, &[]);
        let list: Value = serde_json::from_slice(&result.stdout)
            .map_err(|error| wrong(&format!("returned invalid JSON: {error}")))?;
        let items = list["items"].as_array().ok_or_else(|| {
            wrong(&format!(
                "returned no `items` array (items: {})",
                list["items"]
            ))
        })?;
        for (index, hook) in items.iter().enumerate() {
            if hook["url"].as_str() == Some(url) {
                let id = hook["id"].as_str().ok_or_else(|| {
                    wrong(&format!(
                        "listed item {index} for {url} without a string `id`: {hook}"
                    ))
                })?;
                found.push(id.to_owned());
            }
        }
        cursor = match &list["next_cursor"] {
            Value::Null => None,
            // A repeated cursor would page forever; say so instead of hanging.
            Value::String(next) if !pages.insert(next.clone()) => {
                return Err(wrong(&format!(
                    "returned `next_cursor` {next:?} a second time, so the list never ends"
                )))
            }
            Value::String(next) => Some(next.clone()),
            other => {
                return Err(wrong(&format!(
                    "returned a `next_cursor` that is neither a string nor null: {other}"
                )))
            }
        };
        if cursor.is_none() {
            break;
        }
    }
    if found.len() > 1 {
        bail!(
            "multiple Ting hooks target this interpreter at {url}: {}; select the retained hook by saving its ID as a JSON string in {} before reconnecting",
            found.join(", "),
            saved.display()
        );
    }
    Ok(found.pop())
}

/// Every way one ting breaks the inbox contract, named by its position and ID.
fn problems(index: usize, ting: &Value) -> Vec<String> {
    let Some(fields) = ting.as_object() else {
        return vec![format!("ting {index} must be an object, got {ting}")];
    };
    let id = fields.get("id").and_then(Value::as_str);
    let name = id.map_or(format!("ting {index}"), |id| {
        format!("ting {index} (id {id:?})")
    });
    let got = |field: &str| {
        fields.get(field).map_or(
            "nothing (the field is missing)".to_owned(),
            Value::to_string,
        )
    };
    let mut problems = Vec::new();
    match id {
        Some(id) if !id.is_empty() && id.len() <= 256 => {}
        Some(id) => problems.push(format!(
            "{name}: id must be 1–256 bytes, got {} bytes",
            id.len()
        )),
        None => problems.push(format!("{name}: id must be a string, got {}", got("id"))),
    }
    if !fields
        .get("type")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty())
    {
        problems.push(format!(
            "{name}: type must be a non-empty string, got {}",
            got("type")
        ));
    }
    for field in ["data", "metadata"] {
        if !fields.get(field).is_some_and(Value::is_object) {
            problems.push(format!(
                "{name}: {field} must be an object, got {}",
                got(field)
            ));
        }
    }
    problems
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registration_retains_ids_and_recovers_lost_responses() -> Result<()> {
        use std::os::unix::fs::PermissionsExt;

        for mode in ["new", "error", "lost"] {
            let home = tempfile::tempdir()?;
            let mut cfg: Config = serde_json::from_value(json!({
                "silicon":{"id":"si:test", "org_id":"org", "SILICON_ORG":"selected-org"},
                "isi":{}, "access":{}, "flow":[]
            }))?;
            cfg.home = home.path().to_owned();
            state::write_json(&home.path().join(".silicon/org.json"), &"selected-org")?;
            fs::write(home.path().join("mode"), mode)?;
            let executable = home.path().join(".silicon/bin/ting");
            fs::create_dir_all(executable.parent().unwrap())?;
            fs::write(
                &executable,
                r#"#!/bin/sh
set -eu
[ "$SILICON_ORG" = selected-org ]
if [ "$*" = 'iam --json' ]; then echo '{"app_id":"ting"}'; exit 0; fi
printf '%s\n' "$*" >> calls
case "$*" in
  'webhook list --limit 100 --json')
    if [ -f remote-hook ]; then
      echo '{"items":[{"id":"stable-hook","url":"http://test.org.localhost/"}],"next_cursor":null}'
    else
      echo '{"items":[],"next_cursor":null}'
    fi ;;
  'webhook http://test.org.localhost/ --json')
    [ ! -f remote-hook ]
    touch remote-hook
    case "$(cat mode)" in
      error) echo '{"error":{"details":{"webhook_id":"stable-hook"}}}' >&2; exit 1 ;;
      lost) echo 'response lost after creation' >&2; exit 1 ;;
    esac
    echo '{"id":"stable-hook","state":"connected"}' ;;
  'webhook http://test.org.localhost/ --json --id stable-hook')
    [ -f remote-hook ]
    echo '{"id":"stable-hook","state":"connected"}' ;;
  'unhook stable-hook --json')
    [ -f remote-hook ]
    echo '{"id":"stable-hook","state":"disconnected"}' ;;
  *) exit 2 ;;
esac
"#,
            )?;
            fs::set_permissions(&executable, fs::Permissions::from_mode(0o700))?;
            let inbox = Inbox::new(&cfg)?;
            let first = inbox.register(&cfg, "http://test.org.localhost/");
            assert_eq!(first.is_ok(), mode == "new", "{mode}: {first:?}");
            if let Err(error) = &first {
                // Ting's own words reach the reader, not a generic summary.
                let error = format!("{error:#}");
                assert!(
                    error.contains(
                        "`ting webhook http://test.org.localhost/ --json` failed: exit status: 1"
                    ),
                    "{error}"
                );
                let said = if mode == "error" {
                    r#"{"error":{"details":{"webhook_id":"stable-hook"}}}"#
                } else {
                    "response lost after creation"
                };
                assert!(error.contains(&format!("stderr:\n{said}")), "{error}");
                assert!(error.contains("stdout: (empty)"), "{error}");
            }
            assert_eq!(
                inbox.hook()?.as_deref(),
                (mode != "lost").then_some("stable-hook")
            );

            // A fresh instance models reconnect after the CLI reply or interpreter was lost.
            let reconnected = Inbox::new(&cfg)?;
            reconnected.register(&cfg, "http://test.org.localhost/")?;
            assert_eq!(reconnected.hook()?.as_deref(), Some("stable-hook"));
            assert_eq!(
                serde_json::from_slice::<String>(&fs::read(
                    home.path()
                        .join(".silicon/ting/si:test/selected-org/hook.json")
                )?)?,
                "stable-hook"
            );
            reconnected.unhook(&cfg)?;
            let calls = fs::read_to_string(home.path().join("calls"))?;
            let calls: Vec<_> = calls.lines().collect();
            assert_eq!(
                calls
                    .iter()
                    .filter(|call| **call == "webhook http://test.org.localhost/ --json")
                    .count(),
                1
            );
            assert_eq!(
                calls
                    .iter()
                    .filter(|call| **call == "webhook list --limit 100 --json")
                    .count(),
                if mode == "lost" { 2 } else { 1 }
            );
            assert_eq!(
                &calls[calls.len() - 2..],
                [
                    "webhook http://test.org.localhost/ --json --id stable-hook",
                    "unhook stable-hook --json"
                ]
            );
            assert_eq!(reconnected.hook()?.as_deref(), Some("stable-hook"));
        }
        Ok(())
    }

    #[test]
    fn legacy_namespace_cannot_silently_orphan_pending_deliveries() -> Result<()> {
        let home = tempfile::tempdir()?;
        let mut cfg: Config = serde_json::from_value(json!({
            "silicon":{"id":"si:test", "org_id":"org", "SILICON_ORG":"org"},
            "isi":{}, "access":{}, "flow":[]
        }))?;
        cfg.home = home.path().to_owned();
        Inbox::new(&cfg)?;
        let root = cfg.home.join(".silicon/ting");
        let legacy = root.join("test:org");
        state::write_json(
            &legacy.join("org/inbox.json"),
            &json!({"pending":["untouched"]}),
        )?;
        let bytes = fs::read(legacy.join("org/inbox.json"))?;
        let refused = |cfg: &Config| format!("{:#}", Inbox::new(cfg).err().unwrap());
        let error = refused(&cfg);
        assert!(
            error.contains(&format!(
                "{} belongs to Silicon ID \"test:org\"",
                legacy.display()
            )),
            "{error}"
        );
        assert!(error.contains("PUBLIC-IDENTIFIER-MIGRATION.md"), "{error}");
        let canonical = root.join("si:test");
        fs::rename(&legacy, &canonical)?;
        Inbox::new(&cfg)?;
        assert_eq!(fs::read(canonical.join("org/inbox.json"))?, bytes);

        let retained = cfg.home.join("retained");
        fs::rename(&canonical, &retained)?;
        std::os::unix::fs::symlink(&retained, &canonical)?;
        let error = refused(&cfg);
        assert!(
            error.contains(&format!("{} is a symlink", canonical.display())),
            "{error}"
        );
        fs::remove_file(&canonical)?;
        fs::remove_dir(&root)?;
        let missing = cfg.home.join("missing");
        std::os::unix::fs::symlink(&missing, &root)?;
        let error = refused(&cfg);
        assert!(
            error.contains(&format!(
                "must not be a symlink; it points to {}",
                missing.display()
            )),
            "{error}"
        );
        fs::remove_file(&root)?;
        fs::write(&root, "not a directory")?;
        // The operating system's own reason reaches the reader with the path it concerns.
        let error = refused(&cfg);
        assert!(
            error.contains(&format!(
                "inspect retained Ting namespaces in {}: ",
                root.display()
            )),
            "{error}"
        );
        assert!(error.contains("Not a directory"), "{error}");
        assert_eq!(fs::read(retained.join("org/inbox.json"))?, bytes);
        Ok(())
    }

    #[test]
    fn batches_are_atomic_durable_and_deduplicated() -> Result<()> {
        let home = tempfile::tempdir()?;
        let inbox = Inbox {
            directory: home.path().into(),
        };
        let ting = |id| json!({"id":id,"type":"tos>app.event","data":{},"metadata":{}});
        let rejected = inbox
            .accept(json!({"tings":[ting("a"),{"id":"bad","type":"","data":[]},7]}))
            .unwrap_err()
            .to_string();
        for problem in [
            r#"ting 1 (id "bad"): type must be a non-empty string, got """#,
            r#"ting 1 (id "bad"): data must be an object, got []"#,
            r#"ting 1 (id "bad"): metadata must be an object, got nothing (the field is missing)"#,
            "ting 2 must be an object, got 7",
        ] {
            assert!(rejected.contains(problem), "{rejected}");
        }
        assert!(!rejected.contains("ting 0"), "{rejected}");
        let empty = inbox.accept(json!({"events":[]})).unwrap_err().to_string();
        assert!(empty.contains("no `tings` field, only [events]"), "{empty}");
        assert!(inbox.read()?.pending.is_empty());
        inbox.accept(json!({"tings":[ting("a"),ting("b"),ting("a")]}))?;
        let replacement = Inbox {
            directory: home.path().into(),
        };
        inbox.process(|request| {
            assert_eq!(request["tings"].as_array().unwrap().len(), 2);
            replacement.accept(json!({"tings":[ting("c")]}))?;
            replacement.process(|_| panic!("a replacement must wait for the active flow"))?;
            Ok(())
        })?;
        // The flow's own error comes back as it was, not dressed as an inbox failure.
        let failed = replacement
            .process(|_| bail!("retry this flow"))
            .unwrap_err();
        assert_eq!(format!("{failed:#}"), "retry this flow");
        replacement.process(|request| {
            assert_eq!(request["tings"][0]["id"], "c");
            Ok(())
        })?;
        let reopened = Inbox {
            directory: home.path().into(),
        };
        reopened.accept(json!({"tings":[ting("a"),ting("b")]}))?;
        assert!(reopened.read()?.pending.is_empty());
        Ok(())
    }

    #[test]
    fn failing_ting_calls_report_status_streams_and_answers() -> Result<()> {
        use std::os::unix::fs::PermissionsExt;

        let home = tempfile::tempdir()?;
        let mut cfg: Config = serde_json::from_value(json!({
            "silicon":{"id":"si:test", "org_id":"org", "SILICON_ORG":"org"},
            "isi":{}, "access":{}, "flow":[]
        }))?;
        cfg.home = home.path().to_owned();
        let executable = home.path().join(".silicon/bin/ting");
        fs::create_dir_all(executable.parent().unwrap())?;
        fs::write(
            &executable,
            r#"#!/bin/sh
if [ "$*" = 'iam --json' ]; then echo '{"app_id":"ting"}'; exit 0; fi
case "$(cat mode)" in
  fail)
    echo "partial answer for $1 $2"
    echo "ting: $1 $2 rejected: token stk-0123456789abcdef expired" >&2
    exit 7 ;;
  garbage) echo 'not json at all'; exit 0 ;;
  loop) echo '{"items":[],"next_cursor":"page-2"}' ;;
  pending) echo '{"id":"stable-hook","state":"pending","reason":"endpoint unreachable"}' ;;
  other) echo '{"id":"other-hook","state":"connected"}' ;;
esac
"#,
        )?;
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700))?;
        let url = "http://test.org.localhost/";
        let inbox = Inbox::new(&cfg)?;
        let failure = |mode: &str, run: &dyn Fn() -> Result<()>| -> Result<String> {
            fs::write(home.path().join("mode"), mode)?;
            let error = format!("{:#}", run().unwrap_err());
            // Everything Ting said arrives, except the credential value itself.
            assert!(!error.contains("stk-0123456789abcdef"), "{error}");
            Ok(error)
        };

        let error = failure("fail", &|| inbox.register(&cfg, url))?;
        for said in [
            "could not inspect Ting registrations for http://test.org.localhost/",
            "`ting webhook list --limit 100 --json` failed: exit status: 7",
            "stderr:\nting: webhook list rejected: token [redacted] expired",
            "stdout:\npartial answer for webhook list",
        ] {
            assert!(error.contains(said), "{error}");
        }

        let error = failure("garbage", &|| inbox.register(&cfg, url))?;
        assert!(
            error
                .contains("`ting webhook list --limit 100 --json` returned invalid JSON: expected"),
            "{error}"
        );
        assert!(error.contains("exit status: 0"), "{error}");
        assert!(error.contains("stdout:\nnot json at all"), "{error}");

        let error = failure("loop", &|| inbox.register(&cfg, url))?;
        assert!(
            error.contains(
                "`ting webhook list --limit 100 --json --cursor page-2` returned `next_cursor` \"page-2\" a second time"
            ),
            "{error}"
        );
        assert!(
            error.contains("stdout:\n{\"items\":[],\"next_cursor\":\"page-2\"}"),
            "{error}"
        );

        inbox.save_hook("stable-hook")?;
        let error = failure("fail", &|| inbox.register(&cfg, url))?;
        for said in [
            "Ting webhook registration for http://test.org.localhost/ failed; the saved hook stable-hook",
            "`ting webhook http://test.org.localhost/ --json --id stable-hook` failed: exit status: 7",
            "stderr:\nting: webhook http://test.org.localhost/ rejected: token [redacted] expired",
            "stdout:\npartial answer for webhook http://test.org.localhost/",
        ] {
            assert!(error.contains(said), "{error}");
        }
        let error = failure("pending", &|| inbox.register(&cfg, url))?;
        assert!(
            error.contains(r#"returned webhook state "pending" instead of "connected""#),
            "{error}"
        );
        assert!(error.contains("endpoint unreachable"), "{error}");
        let error = failure("other", &|| inbox.register(&cfg, url))?;
        assert!(
            error.contains(
                r#"returned webhook ID "other-hook", but this interpreter retains "stable-hook""#
            ),
            "{error}"
        );
        assert_eq!(inbox.hook()?.as_deref(), Some("stable-hook"));

        let error = failure("fail", &|| inbox.unhook(&cfg))?;
        for said in [
            "Ting webhook removal failed; its stable ID stable-hook is retained",
            "`ting unhook stable-hook --json` failed: exit status: 7",
            "stderr:\nting: unhook stable-hook rejected: token [redacted] expired",
            "stdout:\npartial answer for unhook stable-hook",
        ] {
            assert!(error.contains(said), "{error}");
        }
        Ok(())
    }
}
