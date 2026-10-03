use silicon::config::Config;
use std::fs;

#[test]
fn local_syntax_errors_precede_home_evaluation_with_starter_imports() -> anyhow::Result<()> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("silicon.yaml");
    let source = "silicon:\n  id: si:test\n  org_id: org\n  token: token\n  timezone: UTC\n  SILICON_HOME: ! touch home-was-evaluated; pwd\n  inference_providers: [all-available-providers]\nisi:\n  worker:\n    model: fast\n    primary_send_mode: global\n    session_type: persistent\n    dna: {assemble: [], next_refresh: 30min}\naccess: {worker: []}\nfunctions: [starter:function:unused, ./functions.yaml]\nflow: []\n";
    for (functions, flow, expected) in [
        (
            "bad: {do: [{return: '{args.n +* 2}'}]}",
            "[]",
            "invalid CEL",
        ),
        (
            "valid: {do: []}",
            "[{break: {reason: stop}}]",
            "break requires a for loop",
        ),
    ] {
        fs::write(dir.path().join("functions.yaml"), functions)?;
        fs::write(&path, source.replace("flow: []", &format!("flow: {flow}")))?;
        let error = format!("{:#}", Config::load(&path).unwrap_err());
        assert!(error.contains(expected), "{error}");
        assert!(!dir.path().join("home-was-evaluated").exists());
        assert!(!dir.path().join(".fromstarter").exists());
    }
    Ok(())
}
