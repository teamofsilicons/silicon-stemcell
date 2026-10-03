//! Public status adapters for the IAM 5 consumers. Browser and Ting retain their own contracts.
use anyhow::{bail, Result};
use serde_json::Value;

pub(super) fn migrated(app: &str) -> bool {
    matches!(
        app,
        "briefcase"
            | "dm"
            | "hook"
            | "extend"
            | "waveform"
            | "commit"
            | "remind"
            | "peek"
            | "spacestation"
            | "starter"
    )
}

pub(super) fn verify(app: &str, status: &Value, sid: &str, org: &str) -> Result<()> {
    if !migrated(app) {
        return Ok(());
    }
    let (kind, actor, selected) = match app {
        "briefcase" => {
            let organizations = status["organizations"].as_array();
            let selected = organizations
                .filter(|v| v.len() == 1)
                .and_then(|v| v[0].as_str());
            (
                status["actor"]["type"].as_str(),
                status["actor"]["public_id"].as_str(),
                selected,
            )
        }
        "extend" => {
            let teams = status["teams"].as_array();
            if !teams.is_some_and(|teams| teams.len() == 1 && teams[0].as_str() == Some(org)) {
                bail!("Extend status must prove exactly the selected organization");
            }
            (
                status["member"]["type"].as_str(),
                status["member"]["id"].as_str(),
                status["team"].as_str(),
            )
        }
        "remind" => (
            status["actor_type"].as_str(),
            status["public_id"].as_str(),
            status["org_id"].as_str(),
        ),
        "spacestation" => (
            status["identity"]["kind"].as_str(),
            status["identity"]["id"].as_str(),
            status["org"].as_str(),
        ),
        "waveform" => (
            status["actor"]["actor_type"].as_str(),
            status["actor"]["public_id"].as_str(),
            status["org_id"].as_str(),
        ),
        "peek" | "starter" => (
            status["actor"]["type"].as_str(),
            status["actor"]["public_id"].as_str(),
            status["org_id"].as_str(),
        ),
        "dm" => (
            status["actor"]["type"].as_str(),
            status["actor"]["id"].as_str(),
            status["organization_id"].as_str(),
        ),
        _ => (
            status["actor"]["type"].as_str(),
            status["actor"]["id"].as_str(),
            status["org_id"].as_str(),
        ),
    };
    if status["authenticated"] != true
        || kind != Some("silicon")
        || actor != Some(sid)
        || selected != Some(org)
    {
        bail!("{app} selected login does not prove this Silicon and organization; choose a separate matching app profile or account before connecting");
    }
    if app == "spacestation" && status["identity"]["org"].as_str() != Some(org) {
        bail!("Space Station returned inconsistent organization identity");
    }
    Ok(())
}

pub(super) fn profile(app: &str, status: &Value) -> Option<String> {
    if !matches!(
        app,
        "briefcase" | "dm" | "hook" | "commit" | "peek" | "spacestation" | "starter"
    ) {
        return None;
    }
    status["profile"].as_str().map(str::to_owned)
}

/// These public selector fields can be compared across a login without persisting secrets.
pub(super) fn same_selection(before: &Value, after: &Value) -> bool {
    [
        "profile",
        "test",
        "testing_environment_id",
        "test_environment_id",
        "url",
        "api_url",
        "world",
    ]
    .iter()
    .all(|key| {
        before
            .get(key)
            .is_none_or(|value| after.get(key) == Some(value))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixtures() -> Vec<(&'static str, Value)> {
        vec![
            (
                "briefcase",
                json!({"authenticated":true,"actor":{"type":"silicon","public_id":"si:agent"},"organizations":["tos"]}),
            ),
            (
                "dm",
                json!({"authenticated":true,"actor":{"type":"silicon","id":"si:agent"},"organization_id":"tos"}),
            ),
            (
                "hook",
                json!({"authenticated":true,"actor":{"type":"silicon","id":"si:agent"},"org_id":"tos"}),
            ),
            (
                "commit",
                json!({"authenticated":true,"actor":{"type":"silicon","id":"si:agent"},"org_id":"tos"}),
            ),
            (
                "extend",
                json!({"authenticated":true,"member":{"type":"silicon","id":"si:agent"},"team":"tos","teams":["tos"]}),
            ),
            (
                "remind",
                json!({"authenticated":true,"actor_type":"silicon","public_id":"si:agent","org_id":"tos"}),
            ),
            (
                "waveform",
                json!({"authenticated":true,"actor":{"actor_type":"silicon","public_id":"si:agent"},"org_id":"tos"}),
            ),
            (
                "peek",
                json!({"authenticated":true,"actor":{"type":"silicon","public_id":"si:agent"},"org_id":"tos"}),
            ),
            (
                "starter",
                json!({"authenticated":true,"actor":{"type":"silicon","public_id":"si:agent"},"org_id":"tos","world":"production"}),
            ),
            (
                "spacestation",
                json!({"authenticated":true,"org":"tos","identity":{"kind":"silicon","id":"si:agent","org":"tos"}}),
            ),
        ]
    }

    #[test]
    fn actual_consumer_shapes_require_the_selected_silicon_and_one_organization() {
        for (app, status) in fixtures() {
            verify(app, &status, "si:agent", "tos").unwrap();
            assert!(verify(app, &status, "si:other", "tos").is_err(), "{app}");
            assert!(verify(app, &status, "si:agent", "other").is_err(), "{app}");
            assert!(
                verify(app, &json!({"authenticated":true}), "si:agent", "tos").is_err(),
                "{app}"
            );
        }
        assert!(verify("briefcase", &json!({"authenticated":true,"actor":{"type":"silicon","public_id":"si:agent"},"organizations":["tos","other"]}), "si:agent", "tos").is_err());
        assert!(verify("extend", &json!({"authenticated":true,"member":{"type":"carbon","id":"si:agent"},"team":"tos","teams":["tos"]}), "si:agent", "tos").is_err());
    }

    #[test]
    fn excluded_apps_keep_their_existing_status_contracts() {
        for app in ["browser", "ting", "custom-app"] {
            assert!(!migrated(app));
            verify(app, &json!({"authenticated":true}), "si:agent", "tos").unwrap();
        }
    }

    #[test]
    fn profile_origin_and_testing_selection_cannot_change_during_login() {
        let before =
            json!({"profile":"work","url":"https://app.example","testing_environment_id":null});
        assert!(same_selection(&before, &before));
        for after in [
            json!({"profile":"other"}),
            json!({"profile":"work","url":"https://other.example","testing_environment_id":null}),
            json!({"profile":"work","url":"https://app.example","testing_environment_id":"test"}),
        ] {
            assert!(!same_selection(&before, &after));
        }
    }
}
