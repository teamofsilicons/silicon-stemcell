param([Parameter(Mandatory)][string]$LinuxArchive, [Parameter(Mandatory)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$source = Split-Path $PSScriptRoot
$versionLine = Select-String '^version = "([^"]+)"' "$source/windows/launcher/Cargo.toml"
if (!$versionLine) { throw "No version = line found in $source/windows/launcher/Cargo.toml" }
$version = $versionLine.Matches[0].Groups[1].Value
$arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$target = if ($arch -eq 'Arm64') { 'aarch64-pc-windows-msvc' } elseif ($arch -eq 'X64') { 'x86_64-pc-windows-msvc' } else { throw "Unsupported architecture $arch" }
$env:RUSTFLAGS = '-C target-feature=+crt-static'
cargo build --manifest-path "$PSScriptRoot/launcher/Cargo.toml" --locked --release --target $target
if ($LASTEXITCODE -ne 0) { throw "Native Windows launcher build failed: cargo build exited $($LASTEXITCODE) (its output is above)" }
$stage = Join-Path $env:RUNNER_TEMP "silicon-$target"
New-Item -ItemType Directory -Path $stage -Force | Out-Null
foreach ($command in 'silicon si omnid silicon-omni omni so caddy iam honeycomb spacestation dm briefcase waveform commit remind hook ting'.Split(' ')) {
    Copy-Item "$PSScriptRoot/launcher/target/$target/release/silicon-windows-launcher.exe" "$stage/$command.exe"
}
Copy-Item $LinuxArchive "$stage/runtime.tar.gz"
[IO.File]::WriteAllText("$stage/RUNTIME.sha256", (Get-FileHash $LinuxArchive -Algorithm SHA256).Hash.ToLowerInvariant() + "`n")
[IO.File]::WriteAllText("$stage/VERSION", "v$version`n")
Copy-Item "$source/install.ps1" $stage
Copy-Item "$PSScriptRoot/launch.sh", "$PSScriptRoot/provision.sh", "$PSScriptRoot/interop.sh", "$PSScriptRoot/open-url.ps1", "$source/LICENSE" $stage
New-Item -ItemType Directory -Path "$stage/LICENSES" -Force | Out-Null
foreach ($notice in 'README.md omni-LICENSE.txt caddy-LICENSE.txt caddy-AUTHORS.txt'.Split(' ')) {
    Copy-Item "$source/LICENSES/$notice" "$stage/LICENSES"
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
Compress-Archive -Path "$stage/*" -DestinationPath "$OutputDirectory/silicon-$target.zip" -Force
Copy-Item "$source/install.ps1" $OutputDirectory
Write-Output $stage
