use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::process::Command;

#[test]
fn bundle_tools_resolve_through_public_symlink_and_keep_home_priority() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap();
    let bin = root.join("lib/silicon/releases/current/bin");
    let public = root.join("public");
    let home = root.join("home");
    let system = root.join("system");
    for dir in [&bin, &public, &home, &system] {
        fs::create_dir_all(dir).unwrap();
    }
    fs::copy(env!("CARGO_BIN_EXE_silicon"), bin.join("silicon")).unwrap();
    symlink(bin.join("silicon"), public.join("silicon")).unwrap();
    symlink("/bin/bash", system.join("bash")).unwrap();
    fs::write(
        bin.parent().unwrap().join("VERSION"),
        env!("CARGO_PKG_VERSION"),
    )
    .unwrap();
    fs::write(bin.parent().unwrap().join("PREFIX"), root.to_str().unwrap()).unwrap();
    let old_bin = root.join("lib/silicon/releases/old/bin");
    fs::create_dir_all(&old_bin).unwrap();
    let probe = "#!/bin/sh\nprintf '%s\\n%s\\n%s\\n' \"$PWD\" \"$HONEYCOMB_AUTO_UPDATE\" \"$PATH\" > \"$TEST_RECEIPT\"\nprintf 'bundled:org\\n'\n";
    fs::write(bin.join("bundle-probe"), probe).unwrap();
    fs::set_permissions(bin.join("bundle-probe"), fs::Permissions::from_mode(0o755)).unwrap();
    let yaml = root.join("silicon.yaml");
    fs::write(
        &yaml,
        format!(
            "silicon:\n  id: ! bundle-probe\n  token: test\n  timezone: UTC\n  SILICON_HOME: {}\n  inference_providers: [all-available-providers]\nisi:\n  worker:\n    model: code\n    primary_send_mode: global\n    session_type: persistent\n    dna: {{assemble: [], next_refresh: 30min}}\naccess: {{worker: []}}\nflow: []\n",
            home.display()
        ),
    )
    .unwrap();
    let run = |path: &std::ffi::OsStr| {
        Command::new(public.join("silicon"))
            .args(["--json", "compile"])
            .arg(&yaml)
            .current_dir(&public)
            .env("PATH", path)
            .env("HOME", &home)
            .env("SILICON_HOME", &home)
            .env("SILICON_TELEMETRY", "0")
            .env("HONEYCOMB_AUTO_UPDATE", "user-choice")
            .env("TEST_RECEIPT", root.join("receipt"))
            .output()
            .unwrap()
    };
    let result = run(system.as_os_str());
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let output: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(output["silicon"], "bundled:org");
    assert!(fs::read_to_string(root.join("receipt"))
        .unwrap()
        .starts_with(&format!("{}\nuser-choice\n", home.display())));
    let result = run(&std::env::join_paths([&bin, &system, &old_bin, &bin]).unwrap());
    assert!(result.status.success());
    let receipt = fs::read_to_string(root.join("receipt")).unwrap();
    assert_eq!(
        std::env::split_paths(receipt.lines().nth(2).unwrap())
            .filter(|path| path == &bin)
            .count(),
        1
    );

    assert!(!std::env::split_paths(receipt.lines().nth(2).unwrap()).any(|path| path == old_bin));

    let home_bin = home.join(".silicon/bin");
    fs::create_dir_all(&home_bin).unwrap();
    fs::write(
        home_bin.join("bundle-probe"),
        probe.replace("bundled:org", "home:org"),
    )
    .unwrap();
    fs::set_permissions(
        home_bin.join("bundle-probe"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let result = run(system.as_os_str());
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let output: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(output["silicon"], "home:org");

    fs::remove_file(home_bin.join("bundle-probe")).unwrap();
    fs::remove_file(bin.parent().unwrap().join("VERSION")).unwrap();
    let result = run(system.as_os_str());
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("bundle-probe"));
}
