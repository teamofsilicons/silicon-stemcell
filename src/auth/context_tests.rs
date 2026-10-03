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
  'iam --json') echo '{"app_id":"hook"}' ;;
  'login status --json')
    printf 'status:%s\n' "$profile" >> calls
    if [ -f "active-$profile.json" ]; then cat "active-$profile.json"
    else printf '{"authenticated":false,"profile":"%s","test":null}\n' "$profile"; fi ;;
  'login issued-slt')
    printf 'login:%s\n' "$profile" >> calls
    printf '{"authenticated":true,"profile":"%s","test":null,"actor":{"type":"silicon","id":"si:agent"},"org_id":"%s"}\n' "$profile" "$SILICON_ORG" > "active-$profile.json"
    if [ -f flip-profile ]; then printf 'other' > selected-profile; fi ;;
  'logout --help') exit 0 ;;
  'logout') rm -f "active-$profile.json" ;;
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
