//! Ting owns transport retries; the interpreter durably accepts batches before running flows.
use crate::{auth, config::Config, state};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::BTreeMap, fs, path::PathBuf, sync::Mutex};

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
            bail!("Ting state directory must not be a symlink");
        }
        match fs::read_dir(&root) {
            Ok(entries) => {
                for entry in entries {
                    let entry = entry?;
                    if !entry.file_type()?.is_dir()
                        || entry.file_name().to_str() != cfg.silicon.id.as_deref()
                    {
                        bail!("Ting state belongs to a different Silicon ID or is not an ordinary directory; stop the interpreter and follow docs/PUBLIC-IDENTIFIER-MIGRATION.md before reconnecting");
                    }
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error).context("inspect retained Ting namespaces"),
        }
        Ok(Self {
            directory: cfg
                .home
                .join(".silicon/ting")
                .join(cfg.silicon.id.as_deref().unwrap_or_default())
                .join(cfg.silicon.silicon_org.as_deref().unwrap_or_default()),
        })
    }

    fn read(&self) -> Result<Queue> {
        match fs::read(self.directory.join("inbox.json")) {
            Ok(bytes) => Ok(serde_json::from_slice(&bytes).context("invalid Ting inbox")?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Queue::default()),
            Err(e) => Err(e.into()),
        }
    }

    pub fn accept(&self, request: Value) -> Result<()> {
        let tings = request
            .get("tings")
            .and_then(Value::as_array)
            .context("Ting requires a tings array")?;
        if tings.is_empty()
            || tings.len() > 100
            || serde_json::to_vec(&request)?.len() > 1024 * 1024
        {
            bail!("Ting batches require 1–100 tings and at most 1 MiB");
        }
        for ting in tings {
            if !ting["id"]
                .as_str()
                .is_some_and(|s| !s.is_empty() && s.len() <= 256)
                || !ting["type"].as_str().is_some_and(|s| !s.is_empty())
                || !ting["data"].is_object()
                || !ting["metadata"].is_object()
            {
                bail!("each ting requires id:string, type:string, data:object, metadata:object");
            }
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
            state::write_json(&self.directory.join("inbox.json"), &queue)?;
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
            let mut queue = self.read()?;
            queue.pending.retain(|pending| pending["id"] != batch["id"]);
            state::write_json(&self.directory.join("inbox.json"), &queue)
        });
        active.retain(|path| path != &self.directory);
        result
    }

    fn hook(&self) -> Result<Option<String>> {
        match fs::read(self.directory.join("hook.json")) {
            Ok(bytes) => Ok(Some(
                serde_json::from_slice(&bytes).context("invalid Ting hook ID")?,
            )),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    pub fn register(&self, cfg: &Config, url: &str) -> Result<()> {
        let mut id = self.hook()?;
        if id.is_none() {
            // Recover a completed creation if the interpreter lost its CLI response.
            let mut cursor = None::<String>;
            loop {
                let mut args = vec!["webhook", "list", "--limit", "100", "--json"];
                if let Some(cursor) = &cursor {
                    args.extend(["--cursor", cursor]);
                }
                let result = auth::run_app(&cfg.home, "ting", &args)?;
                if !result.status.success() {
                    bail!("could not inspect Ting registrations");
                }
                let list: Value = serde_json::from_slice(&result.stdout)
                    .context("invalid Ting registration list")?;
                for hook in list["items"]
                    .as_array()
                    .context("Ting registration list requires items")?
                {
                    if hook["url"].as_str() == Some(url) {
                        if id.is_some() {
                            bail!("multiple Ting hooks target this interpreter; select the retained hook before reconnecting");
                        }
                        id = Some(
                            hook["id"]
                                .as_str()
                                .context("Ting hook requires an ID")?
                                .to_owned(),
                        );
                    }
                }
                cursor = list["next_cursor"].as_str().map(str::to_owned);
                if cursor.is_none() {
                    break;
                }
            }
        }
        if let Some(id) = &id {
            state::write_json(&self.directory.join("hook.json"), id)?;
        }
        let mut args = vec!["webhook", url, "--json"];
        if let Some(id) = &id {
            args.extend(["--id", id]);
        }
        let result = auth::run_app(&cfg.home, "ting", &args)?;
        let value: Value = serde_json::from_slice(if result.status.success() {
            &result.stdout
        } else {
            &result.stderr
        })
        .context("Ting registration returned invalid JSON")?;
        let returned = if result.status.success() {
            value["id"].as_str()
        } else {
            value
                .pointer("/error/details/webhook_id")
                .and_then(Value::as_str)
        };
        if let Some(returned) = returned {
            if id.as_deref().is_some_and(|id| id != returned) {
                bail!("Ting returned a different webhook ID");
            }
            state::write_json(&self.directory.join("hook.json"), &returned)?;
        }
        if !result.status.success() || returned.is_none() || value["state"] != "connected" {
            bail!("Ting webhook registration failed; the saved hook will be reused on reconnect");
        }
        Ok(())
    }

    pub fn unhook(&self, cfg: &Config) -> Result<()> {
        if let Some(id) = self.hook()? {
            if !auth::run_app(&cfg.home, "ting", &["unhook", &id, "--json"])?
                .status
                .success()
            {
                bail!("Ting webhook removal failed; its stable ID is retained");
            }
        }
        Ok(())
    }
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
        assert!(Inbox::new(&cfg).is_err());
        let canonical = root.join("si:test");
        fs::rename(&legacy, &canonical)?;
        Inbox::new(&cfg)?;
        assert_eq!(fs::read(canonical.join("org/inbox.json"))?, bytes);

        let retained = cfg.home.join("retained");
        fs::rename(&canonical, &retained)?;
        std::os::unix::fs::symlink(&retained, &canonical)?;
        assert!(Inbox::new(&cfg).is_err());
        fs::remove_file(&canonical)?;
        fs::remove_dir(&root)?;
        std::os::unix::fs::symlink(cfg.home.join("missing"), &root)?;
        assert!(Inbox::new(&cfg).is_err());
        fs::remove_file(&root)?;
        fs::write(&root, "not a directory")?;
        assert!(Inbox::new(&cfg).is_err());
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
        assert!(inbox
            .accept(json!({"tings":[ting("a"),{"id":"bad"}]}))
            .is_err());
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
        assert!(replacement.process(|_| bail!("retry this flow")).is_err());
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
}
