use std::env;
use std::io;
use std::path::Path;
use std::process::{Command, Output, Stdio};

const DISTRO: &str = "Silicon";
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

fn forwarded(name: &str) -> bool {
    [
        "SILICON_",
        "SI_",
        "OMNI_",
        "HONEYCOMB_",
        "SPACE_STATION_",
        "BRIEFCASE_",
        "DM_",
        "WAVEFORM_",
        "COMMIT_",
        "REMIND_",
        "HOOK_",
        "TING_",
        "IAM_",
        "ANTHROPIC_",
        "OPENAI_",
    ]
    .iter()
    .any(|prefix| name.starts_with(prefix))
        && name != "SILICON_WSL"
}

/// The command line for an error, quoted the way Windows parses it.
fn argv(command: &Command) -> String {
    std::iter::once(command.get_program())
        .chain(command.get_args())
        .map(|word| {
            let word = word.to_string_lossy();
            if word.is_empty() || word.contains([' ', '\t', '"']) {
                format!("\"{}\"", word.replace('"', "\\\""))
            } else {
                word.into_owned()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// wsl.exe writes its own messages as UTF-16LE; Linux programs write UTF-8.
fn text(bytes: &[u8]) -> String {
    if !bytes.contains(&0) {
        return String::from_utf8_lossy(bytes).into_owned();
    }
    let units: Vec<u16> = bytes
        .chunks(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair.get(1).copied().unwrap_or(0)]))
        .collect();
    String::from_utf16_lossy(&units)
        .trim_start_matches('\u{feff}')
        .to_owned()
}

/// Exit status plus both streams verbatim, the shape Silicon gives every failure.
fn describe(output: &Output) -> String {
    let mut description = output.status.to_string();
    for (name, bytes) in [("stderr", &output.stderr), ("stdout", &output.stdout)] {
        let stream = text(bytes);
        let stream = stream.trim_end_matches(['\r', '\n']);
        if stream.trim().is_empty() {
            description.push_str(&format!("\n{name}: (empty)"));
        } else {
            description.push_str(&format!("\n{name}:\n{stream}"));
        }
    }
    description
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
    if let Err(reason) = installed(probe.output(), &argv(&probe), version) {
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
    let arguments = env::args_os().len().saturating_sub(1);
    let status = launch
        .args(env::args_os().skip(1))
        .status()
        .map_err(|error| {
            format!("could not run `{shown}` with this command's {arguments} argument(s): {error}")
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
    fn linux_home_and_path_cannot_be_overridden_through_windows() {
        for name in ["HOME", "USERPROFILE", "PATH", "WSLENV", "SILICON_WSL"] {
            assert!(!forwarded(name));
        }
        assert!(forwarded("SILICON_TELEMETRY"));
        assert!(forwarded("SILICON_HOME"));
        assert!(forwarded("SPACE_STATION_TABLE_KEY"));
        assert!(forwarded("TING_HOME"));
        assert!(COMMANDS.contains(&"ting"));
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

    #[test]
    fn wsl_utf16_messages_read_as_text() {
        let wide: Vec<u8> = "\u{feff}Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        assert_eq!(
            text(&wide),
            "Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND\r\n"
        );
        assert_eq!(text("v5.0.1 Ω\n".as_bytes()), "v5.0.1 Ω\n");
    }
}
