use std::env;
use std::path::Path;
use std::process::{Command, Stdio};

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
        "IAM_",
        "ANTHROPIC_",
        "OPENAI_",
    ]
    .iter()
    .any(|prefix| name.starts_with(prefix))
        && name != "SILICON_WSL"
}

fn run() -> Result<i32, Box<dyn std::error::Error>> {
    let executable = env::current_exe()?;
    let directory = executable.parent().ok_or("launcher has no directory")?;
    let name = executable
        .file_stem()
        .and_then(|p| p.to_str())
        .ok_or("invalid command name")?;
    if !COMMANDS.contains(&name) {
        return Err(format!("unsupported launcher name {name:?}").into());
    }
    let version = std::fs::read_to_string(directory.join("VERSION"))?;
    let version = version.trim();
    let wsl = Path::new(&env::var("SystemRoot")?).join("System32/wsl.exe");
    let installed = Command::new(&wsl)
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
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output();
    if !installed
        .is_ok_and(|o| o.status.success() && String::from_utf8_lossy(&o.stdout).trim() == version)
    {
        eprintln!("Preparing Silicon {version} in its dedicated WSL2 distribution...");
        let powershell = Path::new(&env::var("SystemRoot")?)
            .join("System32/WindowsPowerShell/v1.0/powershell.exe");
        let mut installer = Command::new(powershell)
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
            .stdout(Stdio::piped())
            .spawn()?;
        std::io::copy(
            &mut installer.stdout.take().unwrap(),
            &mut std::io::stderr(),
        )?;
        let status = installer.wait()?;
        if !status.success() {
            return Ok(status.code().unwrap_or(1));
        }
    }
    // WSLENV transports only application settings; Windows HOME/PATH never become
    // the Linux credential home or executable search path. No values are logged.
    let variables: Vec<String> = env::vars_os()
        .filter_map(|(name, _)| name.into_string().ok())
        .filter(|name| forwarded(name))
        .collect();
    let cwd = env::current_dir()?;
    let status = Command::new(wsl)
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
        .arg(cwd)
        .args(env::args_os().skip(1))
        .status()?;
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
    }
}
