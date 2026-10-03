param(
    [string]$Version = 'v6.1.0',
    [string]$Prefix = (Join-Path $env:LOCALAPPDATA 'Silicon'),
    [string]$PayloadRoot,
    [switch]$NoPath,
    # Skip the logon task that starts the interpreter at logon and restarts it; removes an
    # existing one. Remembered: later installs and upgrades keep it off until -Service.
    [switch]$NoService,
    # Register the logon task again after an earlier -NoService.
    [switch]$Service
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows 11 or Windows Server with WSL2.' }
if ($Version -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+$') { throw 'Version must be an exact stable release tag such as v6.1.0.' }
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
if ($NoService -and $Service) { throw 'Pass -Service or -NoService, not both.' }
# The choice is remembered per Windows user, so the launcher's automatic setup and plain
# upgrades never turn a declined task back on. SILICON_NO_SERVICE=1 does the same as
# -NoService for `irm ... | iex`, which takes no switches.
$serviceDeclined = Join-Path $env:LOCALAPPDATA 'Silicon\service-declined'
$serviceTaskFile = Join-Path $env:LOCALAPPDATA 'Silicon\service-task'
$serviceWanted = if ($Service) { $true } elseif ($NoService -or $env:SILICON_NO_SERVICE -eq '1') { $false } else { !(Test-Path -LiteralPath $serviceDeclined) }
$serviceMode = if ($serviceWanted) { 'task' } else { 'none' }
# Task names are machine-wide but WSL distributions are per user, and a standard user may
# not create task folders: one task per user in the root folder, named by the user's SID.
$taskPath = '\'
$taskName = 'Silicon Interpreter ' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
# A download failure names its address; the web error keeps its own words.
function Get-File([string]$Url, [string]$Path) {
    try { Invoke-WebRequest $Url -OutFile $Path -UseBasicParsing }
    catch {
        $body = if ($_.ErrorDetails) { "`n$($_.ErrorDetails.Message)" } else { '' }
        throw "Could not download ${Url}: $($_.Exception.Message)$body"
    }
}
# What a command printed, readable whether Linux (UTF-8) or wsl.exe itself (UTF-16) wrote it.
function Format-Said($said) {
    $text = ((@($said) -replace "`0", '') -join "`n").Trim()
    if ($text) { $text } else { '(nothing)' }
}
# Keep WSL's own words for the error if WSL is not ready.
$wslStatus = "$wsl does not exist."
function Test-WslReady {
    if (!(Test-Path $wsl)) { return $false }
    $said = & $wsl --status
    $ready = $LASTEXITCODE -eq 0
    $script:wslStatus = "wsl --status exited $($LASTEXITCODE) and said:`n$(Format-Said $said)"
    return $ready
}
$wslReady = Test-WslReady
if (!$wslReady) {
    Write-Host 'Enabling the official Windows Subsystem for Linux. Windows may request administrator approval and a restart.'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # The elevated window closes when it ends; its words come back through this log.
    # A terminating error is caught so it lands in the log too, with a failing exit code.
    $enableLog = [IO.Path]::GetTempFileName()
    $enable = '& { try { Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux,VirtualMachinePlatform -All -NoRestart; if (Test-Path "$env:SystemRoot\System32\wsl.exe") { & "$env:SystemRoot\System32\wsl.exe" --install --no-distribution --web-download } } catch { $_; $global:LASTEXITCODE = 1 } } *>&1 | Out-File -LiteralPath ''SILICON_ENABLE_LOG'' -Encoding utf8 -Width 4096; exit $LASTEXITCODE'.Replace('SILICON_ENABLE_LOG', $enableLog.Replace("'", "''"))
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($enable))
    $process = Start-Process -FilePath $powershell -Verb RunAs -ArgumentList @('-NoProfile', '-EncodedCommand', $encoded) -PassThru -Wait
    $enableOutput = Format-Said (Get-Content -LiteralPath $enableLog -Raw)
    Remove-Item -LiteralPath $enableLog -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "Windows could not enable WSL2 (exit $($process.ExitCode)). Ensure virtualization is enabled in firmware and Windows policy permits WSL. Restart if Windows requested it, then rerun the same installer.`nThe elevated setup said:`n$enableOutput"
    }
    $wslReady = Test-WslReady
    if (!$wslReady) { throw "Windows must restart to finish enabling WSL2. Restart, then rerun the same Silicon installation command to resume. No project files or credentials were changed.`n$wslStatus`nThe elevated setup said:`n$enableOutput" }
    Write-Host "The elevated WSL setup said:`n$enableOutput"
}
New-Item -ItemType Directory -Path $Prefix -Force | Out-Null
$controlDirectory = Join-Path $env:LOCALAPPDATA 'Silicon'
New-Item -ItemType Directory -Path $controlDirectory -Force | Out-Null
$lockPath = Join-Path $controlDirectory '.install-lock'
try { $installerLock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
catch { throw "Another Silicon installer may be running. Wait for it to finish, then retry. Locking $lockPath failed: $($_.Exception.Message)" }
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
        Get-File "$base/$asset" $zip
        $sumFile = Join-Path $temporary 'SHA256SUMS'
        Get-File "$base/SHA256SUMS" $sumFile
        $sums = Get-Content -LiteralPath $sumFile -Raw
        $checksumLines = @($sums -split "`n" | Where-Object { $_ -match ('^[a-fA-F0-9]{64}  ' + [regex]::Escape($asset) + '\s*$') })
        if ($checksumLines.Count -ne 1) { throw "Release checksum for $asset is missing or ambiguous. $base/SHA256SUMS says:`n$sums" }
        $expected = $checksumLines[0].Substring(0, 64).ToLowerInvariant()
        $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw "Windows bundle checksum mismatch: SHA256SUMS lists $expected, the download has $actual. Existing installation was not changed." }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
        try {
            foreach ($entry in $archive.Entries) {
                if ($entry.FullName -match '(^[/\\]|(^|[/\\])\.\.([/\\]|$)|:)' -or ($entry.ExternalAttributes -shr 16 -band 0xF000) -eq 0xA000) { throw "Unsafe Windows bundle path: $($entry.FullName)" }
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
    $payloadVersion = (Get-Content -LiteralPath (Join-Path $PayloadRoot 'VERSION') -Raw).Trim()
    if ($payloadVersion -ne $Version) { throw "Windows payload version $payloadVersion does not match the requested release $Version." }
    $runtimeHash = (Get-Content -LiteralPath (Join-Path $PayloadRoot 'RUNTIME.sha256') -Raw).Trim()
    if ($runtimeHash -notmatch '^[a-f0-9]{64}$') { throw "Invalid Linux runtime checksum in RUNTIME.sha256: '$runtimeHash'" }
    $runtimeActual = (Get-FileHash (Join-Path $PayloadRoot 'runtime.tar.gz') -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($runtimeActual -ne $runtimeHash) { throw "Linux runtime checksum mismatch: RUNTIME.sha256 lists $runtimeHash, runtime.tar.gz has $runtimeActual." }
    $distros = ((& $wsl --list --quiet) -replace "`0", '')
    if ($LASTEXITCODE -ne 0) { throw "Could not list WSL distributions: wsl --list --quiet exited $($LASTEXITCODE) and said:`n$(Format-Said $distros)" }
    if ('Silicon' -notin @($distros | ForEach-Object { $_.Trim() })) {
        $rootfs = Join-Path $temporary 'ubuntu.tar.gz'
        Write-Host 'Downloading checksum-pinned Ubuntu 24.04 for the dedicated Silicon WSL2 distribution...'
        Get-File "https://cloud-images.ubuntu.com/wsl/releases/24.04/20240423/ubuntu-noble-wsl-$ubuntuArch-24.04lts.rootfs.tar.gz" $rootfs
        $rootfsActual = (Get-FileHash $rootfs -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($rootfsActual -ne $ubuntuHash) { throw "Ubuntu rootfs checksum mismatch: expected $ubuntuHash, the download has $rootfsActual." }
        $storage = Join-Path $env:LOCALAPPDATA 'Silicon\WSL'
        & $wsl --import Silicon $storage $rootfs --version 2
        if ($LASTEXITCODE -ne 0) { throw "WSL2 import failed: wsl --import exited $($LASTEXITCODE) (its output is above). Enable Virtual Machine Platform and hardware virtualization; if Windows requested a restart, restart and rerun. Virtual machines need nested virtualization." }
        $createdDistro = $true
        & $wsl --distribution Silicon --user root --exec sh -c 'printf silicon-wsl-v1 > /etc/silicon-distribution'
        if ($LASTEXITCODE -ne 0) { throw "Could not initialize Silicon WSL2 ownership marker: exit $($LASTEXITCODE) (its output is above)." }
    }
    $owner = & $wsl --distribution Silicon --user root --exec cat /etc/silicon-distribution
    if ($LASTEXITCODE -ne 0 -or "$owner".Trim() -ne 'silicon-wsl-v1') { throw "An unrelated WSL distribution named Silicon already exists. It has not been modified; rename it before installing.`ncat /etc/silicon-distribution exited $($LASTEXITCODE) and printed:`n$(Format-Said $owner)" }
    $machine = & $wsl --distribution Silicon --user root --exec uname -m
    if ($LASTEXITCODE -ne 0 -or "$machine".Trim() -ne $linuxArch) { throw "The Silicon WSL distribution architecture does not match Windows ($linuxArch): uname -m exited $($LASTEXITCODE) and printed:`n$(Format-Said $machine)" }
    $filesystem = & $wsl --distribution Silicon --user root --exec stat -f -c %T /
    if ($LASTEXITCODE -ne 0 -or "$filesystem".Trim() -ne 'ext2/ext3') { throw "The Silicon distribution must use WSL2 with the native Linux filesystem: stat -f -c %T / exited $($LASTEXITCODE) and printed:`n$(Format-Said $filesystem)" }
    $linuxPayload = & $wsl --distribution Silicon --user root --exec wslpath -u $PayloadRoot
    if ($LASTEXITCODE -ne 0) { throw "Cannot translate the Windows payload path $PayloadRoot into WSL: wslpath exited $($LASTEXITCODE) and printed:`n$(Format-Said $linuxPayload)" }
    $linuxPayload = "$linuxPayload".Trim()
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $linuxPowerShell = & $wsl --distribution Silicon --user root --exec wslpath -u $windowsPowerShell
    if ($LASTEXITCODE -ne 0) { throw "Could not locate the Windows browser bridge: wslpath -u $windowsPowerShell exited $($LASTEXITCODE) and printed:`n$(Format-Said $linuxPowerShell)" }
    $linuxPowerShell = "$linuxPowerShell".Trim()
    & $wsl --distribution Silicon --user root --exec sh "$linuxPayload/provision.sh" $linuxPayload $runtimeHash $Version $linuxPowerShell (Join-Path $PayloadRoot 'open-url.ps1') $serviceMode $taskName
    if ($LASTEXITCODE -ne 0) { throw "Silicon WSL provisioning failed: provision.sh exited $($LASTEXITCODE) (its output is above). Existing projects were not moved." }
    $activated = $true
    if ($createdDistro) {
        & $wsl --terminate Silicon
        if ($LASTEXITCODE -ne 0) { throw "Silicon was installed, but WSL could not reload its non-root default user: wsl --terminate Silicon exited $($LASTEXITCODE) (its output is above). Restart Windows before using this distribution." }
    }
    # WSL2 registers PE execution at VM startup. Ubuntu package upgrades can
    # remove it, and reloading only this distribution does not restart that VM.
    & $wsl --distribution Silicon --user silicon --exec sh "$linuxPayload/interop.sh" check $linuxPowerShell
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Windows executable interoperability check exited $($LASTEXITCODE) (its output is above); repairing it after Linux package updates..."
        & $wsl --distribution Silicon --user root --exec sh "$linuxPayload/interop.sh" repair
        if ($LASTEXITCODE -ne 0) { throw "Windows interoperability could not be restored: interop.sh repair exited $($LASTEXITCODE) (its output is above). Check Windows/WSL policy, then rerun this installer." }
        & $wsl --distribution Silicon --user silicon --exec sh "$linuxPayload/interop.sh" check $linuxPowerShell
        if ($LASTEXITCODE -ne 0) { throw "WSL cannot launch Windows applications: interop.sh check exited $($LASTEXITCODE) (its output is above). Check Windows/WSL policy, then rerun this installer." }
    }
    if (!$NoPath) {
        $previous = [Environment]::GetEnvironmentVariable('Path', 'User')
        $entries = @($previous -split ';' | Where-Object { $_ -and $_ -notlike "$Prefix\releases\*" -and $_ -ne $PayloadRoot })
        [Environment]::SetEnvironmentVariable('Path', (@($PayloadRoot) + $entries -join ';'), 'User')
        $env:Path = "$PayloadRoot;$env:Path"
    }
    # The interpreter lives in the logon task's wsl.exe session: it keeps the distribution
    # running, starts the interpreter at logon and restarts it after a crash, `wsl --shutdown`
    # or a WSL update. Registering again refreshes the helper's path for this release.
    if ($serviceWanted) {
        try {
            $me = "$env:USERDOMAIN\$env:USERNAME"
            $taskAction = New-ScheduledTaskAction -Execute (Join-Path $PayloadRoot 'silicon-service.exe') -WorkingDirectory $PayloadRoot
            $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $me
            $taskPrincipal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
            # Windows' defaults would stop it after 72 hours, on battery, or never start it on battery.
            $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -Hidden
            Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description "Keeps the Silicon interpreter of $me running in its WSL2 distribution and restarts it after a crash. Rerun install.ps1 -NoService to remove it." -Force | Out-Null
            # silicon.exe connect starts this task by the name recorded here.
            [IO.File]::WriteAllText($serviceTaskFile, "$taskPath$taskName`n")
            Remove-Item -LiteralPath $serviceDeclined -Force -ErrorAction SilentlyContinue
            # A helper from an earlier release keeps running; it is replaced at the next logon.
            if ((Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName).State -ne 'Running') {
                Start-ScheduledTask -TaskPath $taskPath -TaskName $taskName
            }
            Write-Host "The Silicon interpreter now starts at logon and restarts after a crash (scheduled task $taskPath$taskName). Rerun this installer with -NoService to turn that off."
        } catch {
            Write-Warning "Silicon is installed, but the logon task $taskPath$taskName could not be set up, so the interpreter starts only with silicon.exe connect and is not restarted after a crash or logon. Rerun this installer to try again. The error:`n$($_.Exception.Message)`n$($_ | Out-String)"
        }
    } else {
        try {
            [IO.File]::WriteAllText($serviceDeclined, "The Silicon logon task was turned off with install.ps1 -NoService (or SILICON_NO_SERVICE=1). install.ps1 -Service turns it back on.`n")
            Remove-Item -LiteralPath $serviceTaskFile -Force -ErrorAction SilentlyContinue
            $existingTask = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue
            if ($existingTask) {
                Stop-ScheduledTask -TaskPath $taskPath -TaskName $taskName
                Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false
                Write-Host "Removed the logon task $taskPath$taskName; the interpreter no longer starts at logon. Rerun this installer with -Service to turn it back on."
            }
        } catch {
            Write-Warning "Could not remove the logon task $taskPath$taskName. The error:`n$($_.Exception.Message)`n$($_ | Out-String)"
        }
    }
    Write-Host "Silicon $Version is installed. Open a new terminal to use silicon.exe."
    Write-Host 'Keep silicon.yaml and SILICON_HOME under \\wsl.localhost\Silicon\home\silicon. Windows data is accessible at /mnt/c.'
    Write-Host 'To open the Linux shell: wsl --distribution Silicon --user silicon --cd ~'
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    if ($createdDistro -and !$activated) {
        & $wsl --unregister Silicon
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not remove the partially created Silicon WSL distribution: wsl --unregister Silicon exited $($LASTEXITCODE) (its output is above). Remove it with that command before rerunning." }
    }
    $installerLock.Dispose()
    $env:WSLENV = $originalWslEnv
    [Console]::OutputEncoding = $originalConsoleEncoding
}
