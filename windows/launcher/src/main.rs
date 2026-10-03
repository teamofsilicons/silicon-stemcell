use silicon_windows_launcher::{
    argv, describe, forwarded, output_within, registered_task, text, DISTRO,
};
use std::env;
use std::ffi::OsString;
use std::io;
use std::path::Path;
use std::process::{Command, Output, Stdio};
use std::time::Duration;

const COMMANDS: &[&str] = &[
    "silicon",
    "si",
    "omnid",
    "silicon-omni",
    "omni",
    "so",
    "caddy",
    "iam",
    "honeycomb",
    "spacestation",
    "dm",
    "briefcase",
    "waveform",
    "commit",
    "remind",
    "hook",
    "ting",
];

/// `silicon connect` (after global flags such as `--json`).
fn is_connect(args: &[OsString]) -> bool {
    args.iter()
        .map(|arg| arg.to_string_lossy())
        .find(|arg| !arg.starts_with('-'))
        .is_some_and(|command| command == "connect")
}

/// schtasks /Query /FO CSV answers `"\Silicon Interpreter S-1-…","N/A","Running"`. Other
/// languages name the state differently; starting a running task is then a no-op anyway,
/// because the task ignores a second instance.
fn task_running(csv: &str) -> bool {
    csv.lines().any(|line| line.contains("\"Running\""))
}

/// Before `silicon connect`, start this user's interpreter logon task if install.ps1
/// registered one and it is not running, so the interpreter lives in that long-lived
/// wsl.exe session and not in this command's short one, which WSL would idle out. A
/// failure is reported and the command continues: connect then starts the interpreter.
fn start_interpreter_task(system_root: &str) {
    if env::var("SILICON_NO_SERVICE").as_deref() == Ok("1") {
        return;
    }
    // No record: installed with -NoService, or by an installer before tasks existed.
    let Some(task) = env::var_os("LOCALAPPDATA")
        .and_then(|local| registered_task(&Path::new(&local).join("Silicon")))
    else {
        return;
    };
    let limit = Duration::from_secs(60);
    let schtasks = Path::new(system_root).join("System32/schtasks.exe");
    let mut query = Command::new(&schtasks);
    query.args(["/Query", "/TN", task.as_str(), "/FO", "CSV", "/NH"]);
    let found = match output_within(&mut query, limit) {
        Ok(output) if output.status.success() => output,
        // Recorded but not found: removed in Task Scheduler, or registered by another user.
        Ok(output) => {
            eprintln!(
                "Silicon Windows launcher: `{}` failed: {}\ncontinuing without the logon task; rerun install.ps1 to register it again",
                argv(&query),
                describe(&output)
            );
            return;
        }
        Err(error) => {
            eprintln!(
                "Silicon Windows launcher: could not run `{}`: {error}; continuing without the logon task",
                argv(&query)
            );
            return;
        }
    };
    if task_running(&text(&found.stdout)) {
        return;
    }
    let mut start = Command::new(&schtasks);
    start.args(["/Run", "/TN", task.as_str()]);
    match output_within(&mut start, limit) {
        Ok(output) if output.status.success() => {}
        Ok(output) => eprintln!(
            "Silicon Windows launcher: could not start the interpreter's logon task: `{}` failed: {}\ncontinuing; connect starts the interpreter itself",
            argv(&start),
            describe(&output)
        ),
        Err(error) => eprintln!(
            "Silicon Windows launcher: could not run `{}`: {error}; continuing; connect starts the interpreter itself",
            argv(&start)
        ),
    }
}

/// Whether WSL already holds this exact release; otherwise why not, in WSL's own words.
fn installed(probe: io::Result<Output>, command: &str, version: &str) -> Result<(), String> {
    let output = probe.map_err(|error| format!("could not run `{command}`: {error}"))?;
    if !output.status.success() {
        return Err(format!("`{command}` failed: {}", describe(&output)));
    }
    let found = text(&output.stdout);
    if found.trim() != version {
        return Err(format!(
            "`{command}` says WSL holds Silicon {:?}, not {version}\n{}",
            found.trim(),
            describe(&output)
        ));
    }
    Ok(())
}

/// Answers that mean WSL lacks this release: no WSL, no distribution, an unprovisioned
/// one, or another version. Anything else may be WSL starting up after logon or updating.
fn needs_provisioning(reason: &str) -> bool {
    reason.starts_with("could not run `")
        || reason.contains("WSL_E_DISTRO_NOT_FOUND")
        || reason.contains("There is no distribution with the supplied name")
        || reason.contains("/opt/silicon/windows-version: No such file or directory")
        || reason.contains("` says WSL holds Silicon ")
}

fn run() -> Result<i32, String> {
    let executable = env::current_exe()
        .map_err(|error| format!("could not locate this launcher's executable: {error}"))?;
    let directory = executable
        .parent()
        .ok_or_else(|| format!("launcher path {} has no directory", executable.display()))?;
    let name = executable
        .file_stem()
        .and_then(|p| p.to_str())
        .ok_or_else(|| format!("launcher path {} has no command name", executable.display()))?;
    if !COMMANDS.contains(&name) {
        return Err(format!(
            "unsupported launcher name {name:?} ({})",
            executable.display()
        ));
    }
    let version_file = directory.join("VERSION");
    let version = std::fs::read_to_string(&version_file)
        .map_err(|error| format!("could not read {}: {error}", version_file.display()))?;
    let version = version.trim();
    let system_root = env::var("SystemRoot")
        .map_err(|error| format!("could not read the SystemRoot environment variable: {error}"))?;
    let wsl = Path::new(&system_root).join("System32/wsl.exe");
    let mut probe = Command::new(&wsl);
    probe
        .env("WSLENV", "")
        .args([
            "--distribution",
            DISTRO,
            "--user",
            "silicon",
            "--exec",
            "cat",
            "/opt/silicon/windows-version",
        ])
        .stdin(Stdio::null());
    let mut outcome = installed(probe.output(), &argv(&probe), version);
    if let Err(first) = outcome.clone() {
        if !needs_provisioning(&first) {
            // Setup takes minutes and may ask for elevation, so a passing WSL error (the
            // service starting after logon, an update) gets one more look first.
            std::thread::sleep(Duration::from_secs(2));
            outcome = installed(probe.output(), &argv(&probe), version)
                .map_err(|again| format!("{first}\nand again 2s later: {again}"));
        }
    }
    if let Err(reason) = outcome {
        // ponytail: first use says why setup runs; a rerun on every call is a bug worth seeing.
        eprintln!("Preparing Silicon {version} in its dedicated WSL2 distribution...\n{reason}");
        let powershell =
            Path::new(&system_root).join("System32/WindowsPowerShell/v1.0/powershell.exe");
        let mut installer = Command::new(powershell);
        installer
            .args([
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
            ])
            .arg(directory.join("install.ps1"))
            .arg("-PayloadRoot")
            .arg(directory)
            .arg("-Version")
            .arg(version)
            // First-use setup must not corrupt JSON or other command stdout.
            .stdout(Stdio::piped());
        let command = argv(&installer);
        let mut child = installer
            .spawn()
            .map_err(|error| format!("could not run `{command}`: {error}"))?;
        std::io::copy(&mut child.stdout.take().unwrap(), &mut std::io::stderr())
            .map_err(|error| format!("could not relay the output of `{command}`: {error}"))?;
        let status = child
            .wait()
            .map_err(|error| format!("could not wait for `{command}`: {error}"))?;
        if !status.success() {
            eprintln!(
                "Silicon Windows launcher: `{command}` failed: {status} (its output is above)"
            );
            return Ok(status.code().unwrap_or(1));
        }
    }
    // WSLENV transports only application settings; Windows HOME/PATH never become
    // the Linux credential home or executable search path. No values are logged.
    let variables: Vec<String> = env::vars_os()
        .filter_map(|(name, _)| name.into_string().ok())
        .filter(|name| forwarded(name))
        .collect();
    let cwd = env::current_dir()
        .map_err(|error| format!("could not read the current directory: {error}"))?;
    let arguments: Vec<OsString> = env::args_os().skip(1).collect();
    if name == "silicon" && is_connect(&arguments) {
        start_interpreter_task(&system_root);
    }
    let mut launch = Command::new(&wsl);
    launch
        .env("WSLENV", variables.join(":"))
        .args([
            "--distribution",
            DISTRO,
            "--user",
            "silicon",
            "--cd",
            "~",
            "--exec",
            "/opt/silicon/launch",
        ])
        .arg(name)
        .arg(&cwd);
    // ponytail: the caller's own arguments are counted, not repeated; this launcher
    // cannot tell which of them are credentials, and the caller already has them.
    let shown = argv(&launch);
    let count = arguments.len();
    let status = launch.args(&arguments).status().map_err(|error| {
        format!("could not run `{shown}` with this command's {count} argument(s): {error}")
    })?;
    Ok(status.code().unwrap_or(1))
}

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("Silicon Windows launcher: {error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn every_bundled_command_has_a_launcher_name() {
        assert!(COMMANDS.contains(&"ting"));
        assert!(!COMMANDS.contains(&"silicon-service"));
    }

    #[test]
    fn connect_starts_the_logon_task_first() {
        let args = |words: &[&str]| words.iter().map(OsString::from).collect::<Vec<_>>();
        assert!(is_connect(&args(&["connect", "silicon.yaml"])));
        assert!(is_connect(&args(&["--json", "connect", "a.yaml"])));
        assert!(!is_connect(&args(&["compile", "connect"])));
        assert!(!is_connect(&args(&[])));
        assert!(task_running(
            "\"\\Silicon Interpreter S-1-5-21-7-8-9-1001\",\"N/A\",\"Running\"\r\n"
        ));
        assert!(!task_running(
            "\"\\Silicon Interpreter S-1-5-21-7-8-9-1001\",\"N/A\",\"Ready\"\r\n"
        ));
    }

    #[cfg(unix)]
    #[test]
    fn failed_probe_keeps_exit_status_and_both_streams() {
        let mut probe = Command::new("sh");
        probe.args([
            "-c",
            "echo '{\"installed\":false}'; echo 'There is no distribution with the supplied name.' >&2; exit 3",
        ]);
        let error = installed(probe.output(), &argv(&probe), "v5.0.1").unwrap_err();
        assert_eq!(
            error,
            "`sh -c \"echo '{\\\"installed\\\":false}'; echo 'There is no distribution with the supplied name.' >&2; exit 3\"` failed: exit status: 3\n\
             stderr:\nThere is no distribution with the supplied name.\n\
             stdout:\n{\"installed\":false}"
        );
        // A wrong answer keeps WSL's warnings too, not only the version it printed.
        let mut older = Command::new("sh");
        older.args([
            "-c",
            "echo v4.1.0; echo 'wsl: Processing fstab with mount -a failed.' >&2",
        ]);
        assert_eq!(
            installed(older.output(), &argv(&older), "v5.0.1").unwrap_err(),
            "`sh -c \"echo v4.1.0; echo 'wsl: Processing fstab with mount -a failed.' >&2\"` says WSL holds Silicon \"v4.1.0\", not v5.0.1\n\
             exit status: 0\n\
             stderr:\nwsl: Processing fstab with mount -a failed.\n\
             stdout:\nv4.1.0"
        );
        let mut current = Command::new("sh");
        current.args(["-c", "echo v5.0.1"]);
        assert!(installed(current.output(), &argv(&current), "v5.0.1").is_ok());
    }

    #[test]
    fn only_a_missing_or_outdated_release_skips_the_second_look() {
        let wide =
            |text: &str| -> Vec<u8> { text.encode_utf16().flat_map(u16::to_le_bytes).collect() };
        let answer = |code: i32, stdout: &[u8], stderr: &[u8]| {
            let mut probe = Command::new("wsl.exe");
            probe.arg("--status");
            let status = {
                #[cfg(unix)]
                {
                    std::os::unix::process::ExitStatusExt::from_raw(code << 8)
                }
                #[cfg(windows)]
                {
                    std::os::windows::process::ExitStatusExt::from_raw(code as u32)
                }
            };
            installed(
                Ok(Output {
                    status,
                    stdout: stdout.to_vec(),
                    stderr: stderr.to_vec(),
                }),
                &argv(&probe),
                "v5.0.2",
            )
            .unwrap_err()
        };
        assert!(needs_provisioning(&answer(
            1,
            &wide("There is no distribution with the supplied name.\r\nError code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"),
            b""
        )));
        assert!(needs_provisioning(&answer(
            1,
            b"",
            b"cat: /opt/silicon/windows-version: No such file or directory\n"
        )));
        assert!(needs_provisioning(&answer(0, b"v5.0.1\n", b"")));
        let missing = installed(
            Err(io::Error::from(io::ErrorKind::NotFound)),
            "wsl.exe --status",
            "v5.0.2",
        )
        .unwrap_err();
        assert!(needs_provisioning(&missing));
        // A WSL service hiccup is looked at again before minutes of setup.
        assert!(!needs_provisioning(&answer(
            1,
            &wide("Error code: Wsl/Service/CreateInstance/E_UNEXPECTED\r\n"),
            b""
        )));
    }

    #[test]
    fn unstartable_probe_names_the_command_and_the_os_reason() {
        let mut probe = Command::new("/nonexistent/wsl.exe");
        probe.arg("--status");
        let error = installed(probe.output(), &argv(&probe), "v5.0.1").unwrap_err();
        assert!(
            error.starts_with("could not run `/nonexistent/wsl.exe --status`: "),
            "{error}"
        );
        assert!(error.len() > "could not run `/nonexistent/wsl.exe --status`: ".len());
    }
}
