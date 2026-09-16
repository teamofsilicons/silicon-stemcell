param(
    [string]$Version = 'v4.0.0',
    [string]$Prefix = (Join-Path $env:LOCALAPPDATA 'Silicon'),
    [string]$PayloadRoot,
    [switch]$NoPath
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows 11 or Windows Server with WSL2.' }
if ($Version -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { throw 'Version must be an exact stable release tag such as v4.0.0.' }
$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
switch ($architecture) {
    'X64' {
        $target = 'x86_64-pc-windows-msvc'
        $linuxArch = 'x86_64'
        $ubuntuArch = 'amd64'
        $ubuntuHash = '2a790896740b14d637dbdc583cce1ba081ac53b9e9cdb46dc09a2f73abbd9934'
    }
    'Arm64' {
        $target = 'aarch64-pc-windows-msvc'
        $linuxArch = 'aarch64'
        $ubuntuArch = 'arm64'
        $ubuntuHash = 'e113b8c49af3ab49b992b8e29550fc921e689f211abc338176f8243786173a32'
    }
    default { throw "Unsupported Windows architecture: $architecture" }
}
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$wslReady = $false
if (Test-Path $wsl) {
    & $wsl --status *> $null
    $wslReady = $LASTEXITCODE -eq 0
}
if (!$wslReady) {
    Write-Host 'Enabling the official Windows Subsystem for Linux. Windows may request administrator approval and a restart.'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $enable = 'Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux,VirtualMachinePlatform -All -NoRestart; if (Test-Path "$env:SystemRoot\System32\wsl.exe") { & "$env:SystemRoot\System32\wsl.exe" --install --no-distribution --web-download }; exit $LASTEXITCODE'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($enable))
    $process = Start-Process -FilePath $powershell -Verb RunAs -ArgumentList @('-NoProfile', '-EncodedCommand', $encoded) -PassThru -Wait
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "Windows could not enable WSL2 (exit $($process.ExitCode)). Ensure virtualization is enabled in firmware and Windows policy permits WSL. Restart if Windows requested it, then rerun the same installer."
    }
    if (Test-Path $wsl) {
        & $wsl --status *> $null
        $wslReady = $LASTEXITCODE -eq 0
    }
    if (!$wslReady) { throw 'Windows must restart to finish enabling WSL2. Restart, then rerun the same Silicon installation command to resume. No project files or credentials were changed.' }
}
New-Item -ItemType Directory -Path $Prefix -Force | Out-Null
$controlDirectory = Join-Path $env:LOCALAPPDATA 'Silicon'
New-Item -ItemType Directory -Path $controlDirectory -Force | Out-Null
$lockPath = Join-Path $controlDirectory '.install-lock'
try { $installerLock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
catch { throw 'Another Silicon installer is running. Wait for it to finish, then retry.' }
$originalWslEnv = $env:WSLENV
$originalConsoleEncoding = [Console]::OutputEncoding
$env:WSLENV = ''
$temporary = Join-Path $Prefix ('.install-' + [guid]::NewGuid().ToString('N'))
$createdDistro = $false
$activated = $false
try {
    # Linux commands return UTF-8, including paths with non-ASCII user names.
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    New-Item -ItemType Directory -Path $temporary | Out-Null
    if (!$PayloadRoot) {
        $asset = "silicon-$target.zip"
        $base = "https://github.com/teamofsilicons/silicon-stemcell/releases/download/$Version"
        $zip = Join-Path $temporary $asset
        Invoke-WebRequest "$base/$asset" -OutFile $zip -UseBasicParsing
        $sumFile = Join-Path $temporary 'SHA256SUMS'
        Invoke-WebRequest "$base/SHA256SUMS" -OutFile $sumFile -UseBasicParsing
        $sums = Get-Content -LiteralPath $sumFile -Raw
        $checksumLines = @($sums -split "`n" | Where-Object { $_ -match ('^[a-fA-F0-9]{64}  ' + [regex]::Escape($asset) + '\s*$') })
        if ($checksumLines.Count -ne 1) { throw 'Release checksum is missing or ambiguous.' }
        $expected = $checksumLines[0].Substring(0, 64).ToLowerInvariant()
        if ((Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw 'Windows bundle checksum mismatch. Existing installation was not changed.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
        try {
            foreach ($entry in $archive.Entries) {
                if ($entry.FullName -match '(^[/\\]|(^|[/\\])\.\.([/\\]|$)|:)' -or ($entry.ExternalAttributes -shr 16 -band 0xF000) -eq 0xA000) { throw 'Unsafe Windows bundle path.' }
            }
        } finally { $archive.Dispose() }
        $release = Join-Path $Prefix "releases\$Version-$expected"
        if (!(Test-Path $release)) {
            $expanded = Join-Path $temporary 'payload'
            Expand-Archive -LiteralPath $zip -DestinationPath $expanded
            New-Item -ItemType Directory -Path (Split-Path $release) -Force | Out-Null
            Move-Item -LiteralPath $expanded -Destination $release
        }
        $PayloadRoot = $release
    }
    $PayloadRoot = (Resolve-Path -LiteralPath $PayloadRoot).Path
    if ((Get-Content -LiteralPath (Join-Path $PayloadRoot 'VERSION') -Raw).Trim() -ne $Version) { throw 'Windows payload version does not match the requested release.' }
    $runtimeHash = (Get-Content -LiteralPath (Join-Path $PayloadRoot 'RUNTIME.sha256') -Raw).Trim()
    if ($runtimeHash -notmatch '^[a-f0-9]{64}$') { throw 'Invalid Linux runtime checksum.' }
    if ((Get-FileHash (Join-Path $PayloadRoot 'runtime.tar.gz') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $runtimeHash) { throw 'Linux runtime checksum mismatch.' }
    $distros = ((& $wsl --list --quiet) -replace "`0", '')
    if ($LASTEXITCODE -ne 0) { throw 'Could not list WSL distributions.' }
    if ('Silicon' -notin @($distros | ForEach-Object { $_.Trim() })) {
        $rootfs = Join-Path $temporary 'ubuntu.tar.gz'
        Write-Host 'Downloading checksum-pinned Ubuntu 24.04 for the dedicated Silicon WSL2 distribution...'
        Invoke-WebRequest "https://cloud-images.ubuntu.com/wsl/releases/24.04/20240423/ubuntu-noble-wsl-$ubuntuArch-24.04lts.rootfs.tar.gz" -OutFile $rootfs -UseBasicParsing
        if ((Get-FileHash $rootfs -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ubuntuHash) { throw 'Ubuntu rootfs checksum mismatch.' }
        $storage = Join-Path $env:LOCALAPPDATA 'Silicon\WSL'
        & $wsl --import Silicon $storage $rootfs --version 2
        if ($LASTEXITCODE -ne 0) { throw 'WSL2 import failed. Enable Virtual Machine Platform and hardware virtualization; if Windows requested a restart, restart and rerun. Virtual machines need nested virtualization.' }
        $createdDistro = $true
        & $wsl --distribution Silicon --user root --exec sh -c 'printf silicon-wsl-v1 > /etc/silicon-distribution'
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize Silicon WSL2 ownership marker.' }
    }
    $owner = & $wsl --distribution Silicon --user root --exec cat /etc/silicon-distribution
    if ($LASTEXITCODE -ne 0 -or "$owner".Trim() -ne 'silicon-wsl-v1') { throw 'An unrelated WSL distribution named Silicon already exists. It has not been modified; rename it before installing.' }
    $machine = & $wsl --distribution Silicon --user root --exec uname -m
    if ($LASTEXITCODE -ne 0 -or "$machine".Trim() -ne $linuxArch) { throw 'The Silicon WSL distribution architecture does not match Windows.' }
    $filesystem = & $wsl --distribution Silicon --user root --exec stat -f -c %T /
    if ($LASTEXITCODE -ne 0 -or "$filesystem".Trim() -ne 'ext2/ext3') { throw 'The Silicon distribution must use WSL2 with the native Linux filesystem.' }
    $linuxPayload = & $wsl --distribution Silicon --user root --exec wslpath -u $PayloadRoot
    if ($LASTEXITCODE -ne 0) { throw 'Cannot translate the Windows payload path into WSL.' }
    $linuxPayload = "$linuxPayload".Trim()
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $linuxPowerShell = (& $wsl --distribution Silicon --user root --exec wslpath -u $windowsPowerShell).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not locate the Windows browser bridge.' }
    & $wsl --distribution Silicon --user root --exec sh "$linuxPayload/provision.sh" $linuxPayload $runtimeHash $Version $linuxPowerShell (Join-Path $PayloadRoot 'open-url.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Silicon WSL provisioning failed. Existing projects were not moved.' }
    $activated = $true
    if ($createdDistro) {
        & $wsl --terminate Silicon
        if ($LASTEXITCODE -ne 0) { throw 'Silicon was installed, but WSL could not reload its non-root default user. Restart Windows before using this distribution.' }
    }
    if (!$NoPath) {
        $previous = [Environment]::GetEnvironmentVariable('Path', 'User')
        $entries = @($previous -split ';' | Where-Object { $_ -and $_ -notlike "$Prefix\releases\*" -and $_ -ne $PayloadRoot })
        [Environment]::SetEnvironmentVariable('Path', (@($PayloadRoot) + $entries -join ';'), 'User')
        $env:Path = "$PayloadRoot;$env:Path"
    }
    Write-Host "Silicon $Version is installed. Open a new terminal to use silicon.exe."
    Write-Host 'Keep silicon.yaml and SILICON_HOME under \\wsl.localhost\Silicon\home\silicon. Windows data is accessible at /mnt/c.'
    Write-Host 'To open the Linux shell: wsl --distribution Silicon --user silicon --cd ~'
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    if ($createdDistro -and !$activated) {
        & $wsl --unregister Silicon
    }
    $installerLock.Dispose()
    $env:WSLENV = $originalWslEnv
    [Console]::OutputEncoding = $originalConsoleEncoding
}
