use super::*;
use serde_json::json;
use std::os::unix::fs::PermissionsExt;

fn fixture() -> Result<tempfile::TempDir> {
    let directory = tempfile::tempdir()?;
    let home = directory.path();
    let bin = home.join(".silicon/bin");
    fs::create_dir_all(&bin)?;
    fs::write(home.join("selected-profile"), "work")?;
    fs::write(
        bin.join("hook"),
        r#"#!/bin/sh
set -eu
profile=$(cat selected-profile)
if [ "${1:-}" = --profile ]; then profile=$2; shift 2; fi
case "$*" in
  'iam --json')
    if [ -f flip-org ]; then printf '"other"' > .silicon/org.json; fi
    echo '{"app_id":"hook"}' ;;
  'login status --json')
    printf 'status:%s\n' "$profile" >> calls
    if [ -f fail-status ]; then exit 1; fi
    if [ -f "active-$profile.json" ]; then cat "active-$profile.json"
    else printf '{"authenticated":false,"profile":"%s","test":null}\n' "$profile"; fi ;;
  'login issued-slt')
    printf 'login:%s\n' "$profile" >> calls
    if [ -f fail-login ]; then exit 1; fi
    printf '{"authenticated":true,"profile":"%s","test":null,"actor":{"type":"silicon","id":"si:agent"},"org_id":"%s"}\n' "$profile" "$SILICON_ORG" > "active-$profile.json"
    if [ -f after.json ]; then cp after.json "active-$profile.json"; fi
    if [ -f flip-profile ]; then printf 'other' > selected-profile; fi ;;
  'logout --help') exit 0 ;;
  'logout') printf 'logout:%s\n' "$profile" >> calls; rm -f "active-$profile.json" ;;
  *) exit 2 ;;
esac
"#,
    )?;
    fs::write(
        bin.join("iam"),
        r#"#!/bin/sh
set -eu
if [ "$*" = 'silicon-login --help' ]; then echo --approve-scopes; exit 0; fi
printf 'mint\n' >> calls
echo '{"slt":"issued-slt","expires_in":120}'
"#,
    )?;
    for name in ["hook", "iam"] {
        fs::set_permissions(bin.join(name), fs::Permissions::from_mode(0o700))?;
    }
    Ok(directory)
}

fn active(home: &Path, profile: &str, kind: &str, actor: &str, org: &str) -> Result<()> {
    crate::state::write_json(
        &home.join(format!("active-{profile}.json")),
        &json!({"authenticated":true,"profile":profile,"test":null,"actor":{"type":kind,"id":actor},"org_id":org}),
    )
}

#[test]
fn migrated_apps_recheck_selected_identity_despite_legacy_cache_and_new_generation() -> Result<()> {
    let directory = fixture()?;
    let home = directory.path();
    active(home, "work", "silicon", "si:agent", "tos")?;
    let key = serde_json::to_string(&("si:agent", "tos", "hook"))?;
    crate::state::write_json(
        &home.join(".silicon/auth-checked.json"),
        &BTreeMap::from([(key, chrono::Utc::now().timestamp())]),
    )?;
    for _ in 0..2 {
        ensure_all_scoped(
            home,
            "si:agent",
            "tos",
            "unused",
            &["hook".into()],
            uuid::Uuid::new_v4(),
        )?;
    }
    assert_eq!(
        fs::read_to_string(home.join("calls"))?,
        "status:work\nstatus:work\n"
    );
    for (kind, actor, org) in [
        ("carbon", "c:alice", "tos"),
        ("silicon", "si:other", "tos"),
        ("silicon", "si:agent", "other"),
    ] {
        active(home, "other", kind, actor, org)?;
        fs::write(home.join("selected-profile"), "other")?;
        let before = fs::read(home.join("active-other.json"))?;
        assert!(ensure_all(home, "si:agent", "tos", "unused", &["hook".into()]).is_err());
        assert_eq!(fs::read(home.join("active-other.json"))?, before);
    }
    assert!(!fs::read_to_string(home.join("calls"))?.contains("mint"));
    Ok(())
}

#[test]
fn login_pins_selected_profile_and_removal_invalidates_only_that_command_receipts() -> Result<()> {
    let directory = fixture()?;
    let home = directory.path();
    fs::write(home.join("flip-profile"), "")?;
    ensure_all(home, "si:agent", "tos", "stk-fixture", &["hook".into()])?;
    assert_eq!(
        fs::read_to_string(home.join("calls"))?,
        "status:work\nmint\nlogin:work\nstatus:work\n"
    );
    assert!(home.join("active-work.json").exists());
    assert!(!home.join("active-other.json").exists());
    fs::write(home.join("selected-profile"), "work")?;
    let sibling = serde_json::to_string(&("si:agent", "tos", "other-command"))?;
    let key = serde_json::to_string(&("si:agent", "tos", "hook"))?;
    for name in ["auth-checked.json", "auth-grants.json"] {
        let path = home.join(".silicon").join(name);
        let mut values: BTreeMap<String, Value> = serde_json::from_slice(&fs::read(&path)?)?;
        values.insert(sibling.clone(), values[&key].clone());
        crate::state::write_json(&path, &values)?;
    }
    remove(home, "hook")?;
    for name in ["auth-checked.json", "auth-grants.json"] {
        let values: BTreeMap<String, Value> =
            serde_json::from_slice(&fs::read(home.join(".silicon").join(name))?)?;
        assert!(!values.contains_key(&key));
        assert!(values.contains_key(&sibling));
    }
    assert!(registered(home)?.is_empty());
    fs::remove_file(home.join("flip-profile"))?;
    ensure_all(home, "si:agent", "tos", "stk-fixture", &["hook".into()])?;
    assert_eq!(
        fs::read_to_string(home.join("calls"))?
            .matches("mint\n")
            .count(),
        2
    );
    Ok(())
}

#[test]
fn migrated_failures_never_use_cache_or_failure_backoff() -> Result<()> {
    let directory = fixture()?;
    let home = directory.path();
    let generation = uuid::Uuid::new_v4();
    let key = serde_json::to_string(&("si:agent", "tos", "hook"))?;
    active(home, "work", "silicon", "si:agent", "tos")?;
    let before = fs::read(home.join("active-work.json"))?;
    for name in ["auth-checked.json", "auth-grants.json"] {
        let value = if name == "auth-checked.json" {
            json!(chrono::Utc::now().timestamp())
        } else {
            json!("tos")
        };
        save(
            &home.join(".silicon").join(name),
            &BTreeMap::from([(key.clone(), value)]),
        )?;
    }
    FAILED.lock().recover().insert(
        failed_check(home, Some(generation), &key),
        chrono::Utc::now().timestamp(),
    );
    fs::write(home.join("fail-status"), "")?;
    for _ in 0..2 {
        let error = ensure_all_scoped(
            home,
            "si:agent",
            "tos",
            "stk-fixture",
            &["hook".into()],
            generation,
        )
        .unwrap_err();
        assert!(format!("{error:#}").contains("requires a verified IAM account"));
    }
    assert_eq!(fs::read(home.join("active-work.json"))?, before);
    fs::remove_file(home.join("fail-status"))?;
    ensure_all_scoped(
        home,
        "si:agent",
        "tos",
        "stk-fixture",
        &["hook".into()],
        generation,
    )?;
    assert_eq!(
        fs::read_to_string(home.join("calls"))?,
        "status:work\nstatus:work\nstatus:work\n"
    );
    Ok(())
}

#[test]
fn explicit_migrated_wrapper_cannot_use_a_legacy_receipt_for_another_account() -> Result<()> {
    let directory = fixture()?;
    let home = directory.path();
    let command = format!(
        "! {}",
        shell_words::quote(&home.join(".silicon/bin/hook").to_string_lossy())
    );
    let key = serde_json::to_string(&("si:agent", "tos", &command))?;
    save(
        &home.join(".silicon/auth-checked.json"),
        &BTreeMap::from([(key.clone(), chrono::Utc::now().timestamp())]),
    )?;
    save(
        &home.join(".silicon/auth-grants.json"),
        &BTreeMap::from([(key, "tos")]),
    )?;
    active(home, "work", "carbon", "c:owner", "tos")?;
    let before = fs::read(home.join("active-work.json"))?;
    for _ in 0..2 {
        assert!(ensure_all(
            home,
            "si:agent",
            "tos",
            "stk-fixture",
            std::slice::from_ref(&command)
        )
        .is_err());
    }
    assert!(setup(home, "si:agent", "tos", "stk-fixture", &command).is_err());
    assert_eq!(fs::read(home.join("active-work.json"))?, before);
    assert_eq!(
        fs::read_to_string(home.join("calls"))?,
        "status:work\nstatus:work\nstatus:work\n"
    );
    Ok(())
}

#[test]
fn failed_fresh_login_leaves_a_verified_migrated_session_signed_in() -> Result<()> {
    let directory = fixture()?;
    let home = directory.path();
    active(home, "work", "silicon", "si:agent", "tos")?;
    fs::write(home.join("fail-login"), "")?;
    let before = fs::read(home.join("active-work.json"))?;
    assert!(setup(home, "si:agent", "tos", "stk-fixture", "hook").is_err());
    assert_eq!(fs::read(home.join("active-work.json"))?, before);
    assert_eq!(
        fs::read_to_string(home.join("calls"))?,
        "status:work\nmint\nlogin:work\n"
    );
    Ok(())
}

#[test]
fn post_login_context_changes_fail_before_recording_any_success() -> Result<()> {
    for patch in [
        json!({"actor":{"type":"carbon","id":"c:other"}}),
        json!({"org_id":"other"}),
        json!({"profile":"other"}),
        json!({"test":"testing-other"}),
    ] {
        let directory = fixture()?;
        let home = directory.path();
        let mut after = json!({"authenticated":true,"profile":"work","test":null,"actor":{"type":"silicon","id":"si:agent"},"org_id":"tos"});
        after
            .as_object_mut()
            .unwrap()
            .extend(patch.as_object().unwrap().clone());
        save(&home.join("after.json"), &after)?;
        assert!(ensure_all(home, "si:agent", "tos", "stk-fixture", &["hook".into()]).is_err());
        assert!(!home.join(".silicon/auth-grants.json").exists());
        assert!(!home.join(".silicon/auth-checked.json").exists());
        assert!(!fs::read_to_string(home.join("calls"))?.contains("logout"));
    }
    Ok(())
}

#[test]
fn discovery_cannot_reselect_the_exchange_organization() -> Result<()> {
    let directory = fixture()?;
    let home = directory.path();
    save(&home.join(".silicon/org.json"), &json!("work-org"))?;
    fs::write(home.join("flip-org"), "")?;
    // Use the explicit command to freeze its environment before its first discovery.
    let command = format!(
        "! {}",
        shell_words::quote(&home.join(".silicon/bin/hook").to_string_lossy())
    );
    setup(home, "si:agent", "tos", "stk-fixture", &command)?;
    let status: Value = serde_json::from_slice(&fs::read(home.join("active-work.json"))?)?;
    assert_eq!(status["org_id"], "work-org");
    assert_eq!(
        grants(home)?[&serde_json::to_string(&("si:agent", "tos", command))?],
        "work-org"
    );
    Ok(())
}
