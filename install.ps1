# ─────────────────────────────────────────────────────────────
# Silicon Stemcell – Universal Installer (Windows PowerShell)
# irm https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.ps1 | iex
# ─────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$RepoUrl    = "https://github.com/unlikefraction/silicon-stemcell.git"
$RepoZip    = "https://github.com/unlikefraction/silicon-stemcell/archive/refs/heads/main.zip"
$RegistryDir = Join-Path $env:USERPROFILE ".silicon"
$RegistryFile = Join-Path $RegistryDir "registry.json"
$BinDir     = Join-Path $RegistryDir "bin"
$CliScript  = Join-Path $BinDir "silicon.cmd"
$CliPs1     = Join-Path $BinDir "silicon-cli.ps1"

# ── Colors & helpers ──────────────────────────────────────────

function Write-Info    { param($msg) Write-Host "→ " -ForegroundColor Blue -NoNewline; Write-Host $msg }
function Write-Ok      { param($msg) Write-Host "✓ " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Warn    { param($msg) Write-Host "⚠ " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Write-Err     { param($msg) Write-Host "✗ " -ForegroundColor Red -NoNewline; Write-Host $msg }
function Write-Header  { param($msg) Write-Host ""; Write-Host "── $msg ──" -ForegroundColor Cyan; Write-Host "" }

function Read-Confirm {
    param([string]$Prompt)
    $ans = Read-Host "$Prompt [Y/n]"
    return ($ans -eq "" -or $ans -match "^[yY]")
}

function Read-Value {
    param([string]$Prompt, [string]$Default = "")
    if ($Default) {
        $val = Read-Host "? $Prompt [$Default]"
        if (-not $val) { $val = $Default }
    } else {
        $val = Read-Host "? $Prompt"
    }
    return $val
}

function Read-Secret {
    param([string]$Prompt)
    $secure = Read-Host "? $Prompt" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    return [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
}

function Read-WorkerProviderOrder {
    param([string]$WorkerType, [string]$Default = "claude")
    $choice = Read-Value "Which provider should $WorkerType workers use – claude or codex?" $Default
    if ($choice -eq "codex") {
        if (Read-Confirm "Keep claude as fallback for $WorkerType workers?") {
            return @("codex", "claude")
        }
        return @("codex")
    }
    if (Read-Confirm "Keep codex as fallback for $WorkerType workers?") {
        return @("claude", "codex")
    }
    return @("claude")
}

# ── Banner ────────────────────────────────────────────────────

Clear-Host
Write-Host @"

  ███████╗██╗██╗     ██╗ ██████╗ ██████╗ ███╗   ██╗
  ██╔════╝██║██║     ██║██╔════╝██╔═══██╗████╗  ██║
  ███████╗██║██║     ██║██║     ██║   ██║██╔██╗ ██║
  ╚════██║██║██║     ██║██║     ██║   ██║██║╚██╗██║
  ███████║██║███████╗██║╚██████╗╚██████╔╝██║ ╚████║
  ╚══════╝╚═╝╚══════╝╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝
                                        stemcell

"@ -ForegroundColor Cyan

Write-Info "Universal installer for Silicon – your autonomous AI agent"
Write-Host ""

# ═════════════════════════════════════════════════════════════
# STEP 1: System checks
# ═════════════════════════════════════════════════════════════

Write-Header "Step 1 · System Checks"

# OS
Write-Ok "Operating system: Windows $([System.Environment]::OSVersion.Version)"

# Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if ($isAdmin) {
    Write-Warn "Running as Administrator. Some tools will be installed system-wide."
} else {
    Write-Info "Running as normal user. You may need to run as Administrator for some installs."
}

# Disk space
$drive = (Get-Item $env:USERPROFILE).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
if ($freeGB -lt 0.5) {
    Write-Err "Low disk space: ${freeGB}GB available. Need at least 0.5GB."
    exit 1
}
Write-Ok "Disk space: ${freeGB}GB available"

# ═════════════════════════════════════════════════════════════
# STEP 2: Prerequisites
# ═════════════════════════════════════════════════════════════

Write-Header "Step 2 · Prerequisites"

# ── Python 3.9+ ───────────────────────────────────────────────

$PythonCmd = $null
foreach ($cmd in @("python3", "python", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 9) {
                $PythonCmd = $cmd
                break
            }
        }
    } catch { }
}

if ($PythonCmd) {
    $pyVer = & $PythonCmd --version 2>&1
    Write-Ok "Python: $pyVer ($PythonCmd)"
} else {
    Write-Warn "Python 3.9+ not found"
    if (Read-Confirm "Install Python?") {
        $hasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)
        if ($hasWinget) {
            Write-Info "Installing Python via winget..."
            winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements
        } else {
            Write-Err "winget not found. Please install Python 3.9+ from https://python.org/downloads"
            Write-Err "Make sure to check 'Add Python to PATH' during installation."
            exit 1
        }
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        foreach ($cmd in @("python3", "python", "py")) {
            try {
                $ver = & $cmd --version 2>&1
                if ($ver -match "Python (\d+)\.(\d+)") {
                    if ([int]$Matches[1] -ge 3 -and [int]$Matches[2] -ge 9) {
                        $PythonCmd = $cmd
                        break
                    }
                }
            } catch { }
        }
        if (-not $PythonCmd) {
            Write-Err "Python installation may require a restart. Close and reopen PowerShell, then re-run."
            exit 1
        }
        Write-Ok "Python installed: $(& $PythonCmd --version 2>&1)"
    } else {
        Write-Err "Python 3.9+ is required. Aborting."
        exit 1
    }
}

# ── Node.js / npm ─────────────────────────────────────────────

$hasNode = $null -ne (Get-Command node -ErrorAction SilentlyContinue)
$hasNpm  = $null -ne (Get-Command npm -ErrorAction SilentlyContinue)

if ($hasNode -and $hasNpm) {
    Write-Ok "Node.js: $(node --version)"
} else {
    Write-Warn "Node.js / npm not found (needed for Claude Code CLI, Codex CLI & silicon-browser)"
    if (Read-Confirm "Install Node.js?") {
        $hasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)
        if ($hasWinget) {
            Write-Info "Installing Node.js via winget..."
            winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
        } else {
            Write-Err "Please install Node.js from https://nodejs.org"
            exit 1
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
            Write-Err "Node.js installation may require a restart. Close and reopen PowerShell, then re-run."
            exit 1
        }
        Write-Ok "Node.js installed: $(node --version)"
    } else {
        Write-Err "Node.js is required for AI backends and silicon-browser. Aborting."
        exit 1
    }
}

# ── git ───────────────────────────────────────────────────────

if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Ok "git: $(git --version)"
} else {
    Write-Warn "git not found"
    if (Read-Confirm "Install git?") {
        $hasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)
        if ($hasWinget) {
            Write-Info "Installing git via winget..."
            winget install Git.Git --accept-source-agreements --accept-package-agreements
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            if (Get-Command git -ErrorAction SilentlyContinue) {
                Write-Ok "git installed: $(git --version)"
            } else {
                Write-Warn "git install did not succeed. Updates will use a weaker merge strategy."
            }
        } else {
            Write-Warn "winget not found. Install git manually for the best update merge strategy."
        }
    } else {
        Write-Warn "Skipping git. Updates will use a weaker merge strategy."
    }
}

# ── Claude Code CLI ───────────────────────────────────────────

if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Ok "Claude Code CLI: installed"
} else {
    Write-Warn "Claude Code CLI not found"
    if (Read-Confirm "Install Claude Code CLI via npm?") {
        Write-Info "Installing @anthropic-ai/claude-code globally..."
        npm install -g @anthropic-ai/claude-code
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (Get-Command claude -ErrorAction SilentlyContinue) {
            Write-Ok "Claude Code CLI installed"
        } else {
            Write-Warn "Claude Code CLI installation failed. Try: npm install -g @anthropic-ai/claude-code"
        }
    } else {
        Write-Warn "Skipping Claude Code CLI."
    }
}

# ── Codex CLI ─────────────────────────────────────────────────

if (Get-Command codex -ErrorAction SilentlyContinue) {
    Write-Ok "Codex CLI: installed"
} else {
    Write-Warn "Codex CLI not found"
    if (Read-Confirm "Install Codex CLI via npm?") {
        Write-Info "Installing @openai/codex globally..."
        npm install -g @openai/codex
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (Get-Command codex -ErrorAction SilentlyContinue) {
            Write-Ok "Codex CLI installed"
        } else {
            Write-Warn "Codex CLI installation failed. Try: npm install -g @openai/codex"
        }
    } else {
        Write-Warn "Skipping Codex CLI."
    }
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue) -and -not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Err "Install at least one AI backend: Claude Code CLI or Codex CLI."
    exit 1
}

# ── Manager brain ─────────────────────────────────────────────

$BrainChoice = "claude"
$BrowserWorkers = @("claude")
$TerminalWorkers = @("claude")
$WriterWorkers = @("claude")
$HasClaude = $null -ne (Get-Command claude -ErrorAction SilentlyContinue)
$HasCodex = $null -ne (Get-Command codex -ErrorAction SilentlyContinue)
if ($HasClaude -and $HasCodex) {
    $brainInput = Read-Value "Which brain should Silicon use – claude or codex?" "claude"
    if ($brainInput -eq "codex") { $BrainChoice = "codex" }
    $BrowserWorkers = Read-WorkerProviderOrder "browser" "claude"
    $TerminalWorkers = Read-WorkerProviderOrder "terminal" "claude"
    $WriterWorkers = Read-WorkerProviderOrder "writer" "claude"
} elseif ($HasCodex) {
    $BrainChoice = "codex"
    $BrowserWorkers = @("codex")
    $TerminalWorkers = @("codex")
    $WriterWorkers = @("codex")
}

# ── silicon-browser ───────────────────────────────────────────

if (Get-Command silicon-browser -ErrorAction SilentlyContinue) {
    Write-Ok "silicon-browser: installed"
} else {
    Write-Warn "silicon-browser not found"
    if (Read-Confirm "Install silicon-browser via npm?") {
        Write-Info "Installing silicon-browser globally..."
        npm install -g silicon-browser
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (Get-Command silicon-browser -ErrorAction SilentlyContinue) {
            Write-Ok "silicon-browser installed"
        } else {
            Write-Warn "silicon-browser install may have failed. Browser workers may not work."
        }
    } else {
        Write-Warn "Skipping silicon-browser. Browser workers will not be available."
    }
}

# ═════════════════════════════════════════════════════════════
# STEP 3: Download the repo
# ═════════════════════════════════════════════════════════════

Write-Header "Step 3 · Download Silicon"

$DefaultDir = Join-Path $env:USERPROFILE "silicon"
$InstallDir = Read-Value "Install directory" $DefaultDir
$InstanceName = Read-Value "Name this silicon instance" "silicon"

$SkipClone = $false

if (Test-Path $InstallDir) {
    if ((Test-Path (Join-Path $InstallDir "main.py")) -and (Test-Path (Join-Path $InstallDir "config.py"))) {
        Write-Warn "Silicon already exists at $InstallDir"
        if (Read-Confirm "Use existing installation?") {
            Write-Ok "Using existing installation at $InstallDir"
            $SkipClone = $true
        } else {
            $backup = "${InstallDir}.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
            Write-Warn "Backing up existing to $backup"
            Move-Item $InstallDir $backup
        }
    } else {
        Write-Warn "Directory exists but doesn't look like silicon."
        if (-not (Read-Confirm "Continue and clone into it?")) {
            Write-Err "Aborting."
            exit 1
        }
    }
}

if (-not $SkipClone) {
    if (Read-Confirm "Download Silicon to $InstallDir?") {
        $hasGit = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
        if ($hasGit) {
            Write-Info "Cloning via git..."
            git clone $RepoUrl $InstallDir
            Write-Ok "Cloned to $InstallDir"
        } else {
            Write-Info "git not found. Downloading ZIP..."
            $tmpZip = Join-Path $env:TEMP "silicon-stemcell.zip"
            $tmpDir = Join-Path $env:TEMP "silicon-extract"
            Invoke-WebRequest -Uri $RepoZip -OutFile $tmpZip -UseBasicParsing
            Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force
            Move-Item (Join-Path $tmpDir "silicon-stemcell-main") $InstallDir
            Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
            Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
            Write-Ok "Downloaded and extracted to $InstallDir"
        }
    } else {
        Write-Err "Aborting."
        exit 1
    }
}

$UpdaterScript = Join-Path $InstallDir "scripts\silicon_update.py"
if (Test-Path $UpdaterScript) {
    & $PythonCmd $UpdaterScript snapshot --source $InstallDir --target $InstallDir *> $null
}

# ── pip packages ──────────────────────────────────────────────

$reqFile = Join-Path $InstallDir "requirements.txt"
if (Test-Path $reqFile) {
    Write-Info "Installing Python dependencies..."
    & $PythonCmd -m pip install -r $reqFile --quiet 2>$null
    Write-Ok "Python dependencies installed"
}

# ═════════════════════════════════════════════════════════════
# STEP 4: Configure
# ═════════════════════════════════════════════════════════════

Write-Header "Step 4 · Configure"

$EnvFile = Join-Path $InstallDir "env.py"
$AlreadyConfigured = $false

if (Test-Path $EnvFile) {
    $envContent = Get-Content $EnvFile -Raw
    if ($envContent -match 'BROWSER_PROFILE\s*=') {
        $AlreadyConfigured = $true
    }
}

if ($AlreadyConfigured) {
    Write-Ok "Already configured (env.py exists)"
    if (Read-Confirm "Reconfigure?") {
        $AlreadyConfigured = $false
    }
}

if (-not $AlreadyConfigured) {
    @"
GLASS_API_KEY = ""
BROWSER_PROFILE = "$InstanceName"
"@ | Set-Content $EnvFile -Encoding UTF8

    Write-Ok "Configuration saved to $EnvFile"
}

$SiliconJson = Join-Path $InstallDir "silicon.json"
if (Test-Path $SiliconJson) {
    try {
        $silicon = Get-Content $SiliconJson -Raw | ConvertFrom-Json
    } catch {
        $silicon = [pscustomobject]@{}
    }
} else {
    $silicon = [pscustomobject]@{}
}
if (-not ($silicon.PSObject.Properties.Name -contains "name")) {
    $silicon | Add-Member -NotePropertyName "name" -NotePropertyValue "Silicon"
}
if (-not ($silicon.PSObject.Properties.Name -contains "run")) {
    $silicon | Add-Member -NotePropertyName "run" -NotePropertyValue "python main.py"
}
if (-not ($silicon.PSObject.Properties.Name -contains "workers")) {
    $silicon | Add-Member -NotePropertyName "workers" -NotePropertyValue @{}
}
if ($silicon.PSObject.Properties.Name -contains "brain") {
    $silicon.brain = $BrainChoice
} else {
    $silicon | Add-Member -NotePropertyName "brain" -NotePropertyValue $BrainChoice
}
$silicon.workers = @{
    browser  = $BrowserWorkers
    terminal = $TerminalWorkers
    writer   = $WriterWorkers
}
$silicon | ConvertTo-Json -Depth 10 | Set-Content $SiliconJson -Encoding UTF8

# ═════════════════════════════════════════════════════════════
# STEP 5: Silicon registry
# ═════════════════════════════════════════════════════════════

Write-Header "Step 5 · Registry"

if (-not (Test-Path $RegistryDir)) { New-Item -ItemType Directory -Path $RegistryDir -Force | Out-Null }
if (-not (Test-Path $BinDir))      { New-Item -ItemType Directory -Path $BinDir -Force | Out-Null }

if (-not (Test-Path $RegistryFile)) {
    @{ installations = @() } | ConvertTo-Json | Set-Content $RegistryFile -Encoding UTF8
    Write-Ok "Created registry at $RegistryFile"
}

$AbsInstallDir = (Resolve-Path $InstallDir).Path
$registry = Get-Content $RegistryFile -Raw | ConvertFrom-Json

$alreadyRegistered = $false
foreach ($inst in $registry.installations) {
    if ($inst.path -eq $AbsInstallDir -or $inst.name -eq $InstanceName) {
        $alreadyRegistered = $true
        break
    }
}

if ($alreadyRegistered) {
    Write-Ok "Instance '$InstanceName' already registered"
} else {
    $newEntry = @{
        name       = $InstanceName
        path       = $AbsInstallDir
        created_at = (Get-Date).ToString("o")
        pid_file   = Join-Path $AbsInstallDir ".silicon.pid"
    }
    $installations = [System.Collections.ArrayList]@()
    foreach ($inst in $registry.installations) { [void]$installations.Add($inst) }
    [void]$installations.Add($newEntry)
    $registry.installations = $installations.ToArray()
    $registry | ConvertTo-Json -Depth 10 | Set-Content $RegistryFile -Encoding UTF8
    Write-Ok "Registered '$InstanceName' at $AbsInstallDir"
}

# ═════════════════════════════════════════════════════════════
# STEP 6: Create CLI
# ═════════════════════════════════════════════════════════════

Write-Header "Step 6 · CLI Setup"

# Create the PowerShell CLI script
@'
# Silicon CLI – manages silicon installations
param([string]$Command, [string]$Arg)

$RegistryDir  = Join-Path $env:USERPROFILE ".silicon"
$RegistryFile = Join-Path $RegistryDir "registry.json"
$RepoZip = "https://github.com/unlikefraction/silicon-stemcell/archive/refs/heads/main.zip"

function Write-Err  { param($m) Write-Host "✗ " -ForegroundColor Red    -NoNewline; Write-Host $m }
function Write-Info { param($m) Write-Host "→ " -ForegroundColor Blue   -NoNewline; Write-Host $m }
function Write-Ok   { param($m) Write-Host "✓ " -ForegroundColor Green  -NoNewline; Write-Host $m }
function Write-Warn { param($m) Write-Host "⚠ " -ForegroundColor Yellow -NoNewline; Write-Host $m }

$PythonCmd = $null
foreach ($c in @("python3", "python", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $PythonCmd = $c; break }
}

function Get-Registry {
    if (-not (Test-Path $RegistryFile)) { return @{ installations = @() } }
    return Get-Content $RegistryFile -Raw | ConvertFrom-Json
}

function Test-Running {
    param([string]$PidFile)
    if (-not (Test-Path $PidFile)) { return $false }
    $pid = Get-Content $PidFile -Raw -ErrorAction SilentlyContinue
    if (-not $pid) { return $false }
    try {
        $proc = Get-Process -Id ([int]$pid.Trim()) -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Get-Pid {
    param([string]$PidFile)
    if (Test-Path $PidFile) { return (Get-Content $PidFile -Raw).Trim() }
    return $null
}

function Find-Installation {
    param([string]$Search)
    $reg = Get-Registry
    $cwd = (Get-Location).Path
    foreach ($inst in $reg.installations) {
        if ($Search -and $inst.name -eq $Search) { return $inst }
        if (-not $Search -and $cwd.StartsWith($inst.path)) { return $inst }
    }
    return $null
}

function Select-Installation {
    $reg = Get-Registry
    $count = $reg.installations.Count
    if ($count -eq 0) { Write-Err "No silicon installations found. Run 'silicon install' first."; exit 1 }
    if ($count -eq 1) { return $reg.installations[0] }

    Write-Host "`nSelect a silicon instance:`n" -ForegroundColor White
    for ($i = 0; $i -lt $count; $i++) {
        $inst = $reg.installations[$i]
        $status = if (Test-Running $inst.pid_file) { "● running" } else { "○ stopped" }
        $color  = if (Test-Running $inst.pid_file) { "Green" } else { "DarkGray" }
        Write-Host "  $($i+1)) " -NoNewline; Write-Host "$($inst.name)".PadRight(20) -NoNewline
        Write-Host $status -ForegroundColor $color -NoNewline; Write-Host "  $($inst.path)" -ForegroundColor DarkGray
    }
    $choice = Read-Host "`n? Choice [1]"
    if (-not $choice) { $choice = "1" }
    $idx = [int]$choice - 1
    if ($idx -lt 0 -or $idx -ge $count) { Write-Err "Invalid choice"; exit 1 }
    return $reg.installations[$idx]
}

function Invoke-List {
    $reg = Get-Registry
    if ($reg.installations.Count -eq 0) {
        Write-Info "No silicon installations found."
        Write-Info "Run 'silicon install' to set up a new instance."
        return
    }
    Write-Host "`nSilicon Installations`n" -ForegroundColor Cyan
    Write-Host ("  {0,-4} {1,-20} {2,-12} {3}" -f "#", "NAME", "STATUS", "PATH") -ForegroundColor DarkGray
    Write-Host ("  {0,-4} {1,-20} {2,-12} {3}" -f "---", "----", "------", "----") -ForegroundColor DarkGray
    for ($i = 0; $i -lt $reg.installations.Count; $i++) {
        $inst = $reg.installations[$i]
        $running = Test-Running $inst.pid_file
        $status = if ($running) { "● running" } else { "○ stopped" }
        $color  = if ($running) { "Green" } else { "DarkGray" }
        $pidInfo = if ($running) { " (PID $(Get-Pid $inst.pid_file))" } else { "" }
        Write-Host "  " -NoNewline
        Write-Host ("{0,-4}" -f ($i+1)) -NoNewline
        Write-Host ("{0,-20}" -f $inst.name) -NoNewline
        Write-Host ("{0,-12}" -f $status) -ForegroundColor $color -NoNewline
        Write-Host "$pidInfo " -ForegroundColor DarkGray -NoNewline
        Write-Host $inst.path -ForegroundColor DarkGray
    }
    Write-Host ""
}

function Invoke-Start {
    param([string]$Target)
    $inst = if ($Target) { Find-Installation $Target } else { Find-Installation }
    if (-not $inst) { $inst = Select-Installation }

    if (Test-Running $inst.pid_file) {
        $p = Get-Pid $inst.pid_file
        Write-Warn "'$($inst.name)' is already running (PID $p)"
        return
    }

    Write-Info "Starting '$($inst.name)'..."
    $logFile = Join-Path $inst.path ".silicon.log"
    $proc = Start-Process -FilePath $PythonCmd -ArgumentList "-u", "main.py" -WorkingDirectory $inst.path -RedirectStandardOutput $logFile -RedirectStandardError (Join-Path $inst.path ".silicon.err.log") -PassThru -WindowStyle Hidden
    $proc.Id | Set-Content $inst.pid_file -Encoding UTF8

    Start-Sleep -Seconds 1
    try {
        Get-Process -Id $proc.Id -ErrorAction Stop | Out-Null
        Write-Ok "'$($inst.name)' started (PID $($proc.Id))"
        Write-Info "Logs: $logFile"
    } catch {
        Write-Err "'$($inst.name)' failed to start. Check logs: $logFile"
        Remove-Item $inst.pid_file -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Stop {
    param([string]$Target)
    $inst = if ($Target) { Find-Installation $Target } else { Find-Installation }
    if (-not $inst) { $inst = Select-Installation }

    if (-not (Test-Running $inst.pid_file)) {
        Write-Warn "'$($inst.name)' is not running"
        Remove-Item $inst.pid_file -Force -ErrorAction SilentlyContinue
        return
    }

    $p = Get-Pid $inst.pid_file
    Write-Info "Stopping '$($inst.name)' (PID $p)..."
    try {
        Stop-Process -Id ([int]$p) -Force -ErrorAction Stop
    } catch { }
    Remove-Item $inst.pid_file -Force -ErrorAction SilentlyContinue
    Write-Ok "'$($inst.name)' stopped"
}

function Invoke-Status {
    param([string]$Target)
    if ($Target) {
        $inst = Find-Installation $Target
        if (-not $inst) { Invoke-List; return }
    } else {
        $inst = Find-Installation
        if (-not $inst) { Invoke-List; return }
    }
    $running = Test-Running $inst.pid_file
    $status = if ($running) { "● running (PID $(Get-Pid $inst.pid_file))" } else { "○ stopped" }
    $color  = if ($running) { "Green" } else { "DarkGray" }
    Write-Host "`n$($inst.name) " -ForegroundColor White -NoNewline
    Write-Host $status -ForegroundColor $color
    Write-Host "  Path: $($inst.path)" -ForegroundColor DarkGray
    Write-Host ""
}

function Invoke-Browser {
    param([string]$Target)
    $inst = if ($Target) { Find-Installation $Target } else { Find-Installation }
    if (-not $inst) { $inst = Select-Installation }

    Write-Info "Opening browser for '$($inst.name)'..."
    Push-Location $inst.path
    & $PythonCmd main.py browser
    Pop-Location
}

function Ensure-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) { return $true }
    Write-Warn "git not found. It's needed for the best merge strategy during updates."
    if (-not (Read-Confirm "Install git now?")) { return $false }

    $hasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)
    if (-not $hasWinget) {
        Write-Err "winget not found. Install git manually and retry."
        return $false
    }

    Write-Info "Installing git via winget..."
    winget install Git.Git --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    return ($null -ne (Get-Command git -ErrorAction SilentlyContinue))
}

function Invoke-Update {
    param([string]$Target)
    $inst = if ($Target) { Find-Installation $Target } else { Find-Installation }
    if (-not $inst) { $inst = Select-Installation }

    if (Test-Running $inst.pid_file) {
        Write-Err "'$($inst.name)' is running. Stop it first with: silicon stop $($inst.name)"
        exit 1
    }

    if (-not (Ensure-Git)) {
        Write-Warn "Proceeding without git. Some safe auto-merges may be skipped."
    }

    $tmpRoot = Join-Path $env:TEMP ("silicon-update-" + [guid]::NewGuid().ToString("N"))
    $tmpZip  = "$tmpRoot.zip"
    New-Item -ItemType Directory -Path $tmpRoot -Force | Out-Null
    try {
        Write-Info "Downloading latest Silicon source..."
        Invoke-WebRequest -Uri $RepoZip -OutFile $tmpZip -UseBasicParsing
        Expand-Archive -Path $tmpZip -DestinationPath $tmpRoot -Force
        $sourceDir = Get-ChildItem $tmpRoot -Directory | Select-Object -First 1
        if (-not $sourceDir) {
            Write-Err "Could not find extracted source directory."
            exit 1
        }

        $updater = Join-Path $sourceDir.FullName "scripts\silicon_update.py"
        if (-not (Test-Path $updater)) {
            Write-Err "Downloaded source did not include the updater script."
            exit 1
        }

        Write-Info "Updating '$($inst.name)' safely..."
        & $PythonCmd $updater update --source $sourceDir.FullName --target $inst.path
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "'$($inst.name)' updated successfully"
        } elseif ($LASTEXITCODE -eq 2) {
            Write-Err "Update aborted because merge conflicts were detected."
            Write-Info "No local files were overwritten."
            exit 2
        } else {
            Write-Err "Update failed."
            exit $LASTEXITCODE
        }
    } finally {
        Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
        Remove-Item $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-Install {
    $url = "https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.ps1"
    irm $url | iex
}

function Invoke-Help {
    Write-Host "`nsilicon" -ForegroundColor Cyan -NoNewline; Write-Host " – manage your silicon instances`n"
    Write-Host "Usage:" -ForegroundColor White
    Write-Host "  silicon                     Show status or list instances"
    Write-Host "  silicon start [name]        Start a silicon instance"
    Write-Host "  silicon stop [name]         Stop a running instance"
    Write-Host "  silicon status [name]       Show instance status"
    Write-Host "  silicon browser [name]      Open headed browser for login"
    Write-Host "  silicon update [name]       Update a silicon instance to latest without overwriting local changes"
    Write-Host "  silicon list                List all instances"
    Write-Host "  silicon install             Install a new instance"
    Write-Host "  silicon help                Show this help"
    Write-Host ""
}

# ── Dispatch ──────────────────────────────────────────────────

switch ($Command) {
    "start"   { Invoke-Start $Arg }
    "stop"    { Invoke-Stop $Arg }
    "status"  { Invoke-Status $Arg }
    "browser" { Invoke-Browser $Arg }
    "update"  { Invoke-Update $Arg }
    { $_ -in "list", "ls" } { Invoke-List }
    "install" { Invoke-Install }
    { $_ -in "help", "-h", "--help" } { Invoke-Help }
    ""        { Invoke-Status "" }
    default   { Write-Err "Unknown command: $Command"; Invoke-Help; exit 1 }
}
'@ | Set-Content $CliPs1 -Encoding UTF8

# Create .cmd wrapper
@"
@echo off
powershell -ExecutionPolicy Bypass -File "$CliPs1" %*
"@ | Set-Content $CliScript -Encoding UTF8

Write-Ok "CLI created at $CliScript"

# ── Add to PATH ───────────────────────────────────────────────

$currentPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($currentPath -split ";" | Where-Object { $_ -eq $BinDir }) {
    Write-Ok "CLI already in PATH"
} else {
    [System.Environment]::SetEnvironmentVariable("Path", "$BinDir;$currentPath", "User")
    $env:Path = "$BinDir;$env:Path"
    Write-Ok "Added $BinDir to user PATH"
}

# ═════════════════════════════════════════════════════════════
# STEP 7: Summary
# ═════════════════════════════════════════════════════════════

Write-Header "Installation Complete"

Write-Host @"

  ╔══════════════════════════════════════════╗
  ║     Silicon is ready to go!             ║
  ╚══════════════════════════════════════════╝

"@ -ForegroundColor Green

Write-Host "  Instance:  $InstanceName"
Write-Host "  Location:  $AbsInstallDir"
Write-Host "  Registry:  $RegistryFile"
Write-Host "  CLI:       $CliScript"
Write-Host ""
Write-Host "  Quick start:" -ForegroundColor Cyan
Write-Host "    # Open a new PowerShell window, then:" -ForegroundColor DarkGray
Write-Host "    silicon start          # Start silicon"
Write-Host "    silicon browser        # Login to services"
Write-Host "    silicon stop           # Stop silicon"
Write-Host "    silicon list           # See all instances"
Write-Host ""
Write-Host "  All commands:" -ForegroundColor Cyan
Write-Host "    silicon help"
Write-Host ""
