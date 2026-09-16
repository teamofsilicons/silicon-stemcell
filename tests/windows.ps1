param([Parameter(Mandatory)][string]$PayloadRoot)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$source = Split-Path $PSScriptRoot
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
$existing = ((& $wsl --list --quiet) -replace [char]0, '')
if ('Silicon' -in @($existing | ForEach-Object { $_.Trim() })) { throw 'Tests require a fresh machine without a Silicon WSL distribution.' }
$exe = Join-Path $PayloadRoot 'silicon.exe'
$env:SILICON_TELEMETRY = '0'
$env:SILICON_AUTO_UPDATE = '0'
$env:SILICON_INTERPRETER_HOME = '/home/silicon/native-cli-state'
try {
    & "$source/install.ps1" -PayloadRoot $PayloadRoot -NoPath
    $uid = & $wsl -d Silicon -u silicon --exec id -u
    if ($LASTEXITCODE -ne 0 -or "$uid".Trim() -eq '0') { throw 'Runtime user must not be root.' }
    $linuxSource = (& $wsl -d Silicon -u silicon --exec wslpath -u $source).Trim()
    & $wsl -d Silicon -u silicon --exec sh -ec 'mkdir -p /home/silicon/qa-source/tests; cp "$1/tests/e2e.py" /home/silicon/qa-source/tests/e2e.py' sh $linuxSource
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare tests on Linux FS.' }
    # Real Omni/Caddy, flow, setup, sessions, heartbeats, HTTP, restart and cleanup.
    & $wsl -d Silicon -u silicon --cd /home/silicon/qa-source --exec env SILICON_WSL=1 SILICON_TELEMETRY=0 SILICON_AUTO_UPDATE=0 SILICON_TEST_BIN_DIR=/home/silicon/.local/share/silicon/bin OMNI_DAEMON=/home/silicon/.local/share/silicon/bin/omnid SILICON_CADDY=/home/silicon/.local/share/silicon/bin/caddy python3 tests/e2e.py
    if ($LASTEXITCODE -ne 0) { throw 'Real WSL2 interpreter + Omni + Caddy E2E failed.' }
    # The actual PE entry points, quoted arguments, Unicode, pipes and exit codes.
    $env:SILICON_HOME = '/home/silicon/native project Ω'
    & $wsl -d Silicon -u silicon --exec mkdir -p $env:SILICON_HOME
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Linux project.' }
    $fixture = Join-Path $env:RUNNER_TEMP 'windows-fixture.yaml'
    $yaml = @'
silicon:
  id: windows-test:tos
  token: fixture-not-a-real-token
  timezone: UTC
  SILICON_HOME: ! pwd
  inference_providers: [all-available-providers]
  setup: ['! printf "setup-ran\n" >> setup-marker']
isi:
  worker:
    model: fast
    primary_send_mode: global
    session_type: persistent
    dna: {assemble: [], next_refresh: 30min}
access: {worker: []}
flow: []
'@
    [IO.File]::WriteAllText($fixture, $yaml)
    $linuxFixture = (& $wsl -d Silicon -u silicon --exec wslpath -u $fixture).Trim()
    & $wsl -d Silicon -u silicon --exec cp $linuxFixture "$env:SILICON_HOME/silicon.yaml"
    if ($LASTEXITCODE -ne 0) { throw 'Could not copy fixture.' }
    $compiled = & $exe --json compile '.\silicon.yaml' | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or !$compiled.valid) { throw 'Native compile failed.' }
    & $wsl -d Silicon -u silicon --exec test ! -e "$env:SILICON_HOME/setup-marker"
    if ($LASTEXITCODE -ne 0) { throw 'Compile executed setup.' }
    $uncYaml = '\\wsl.localhost\Silicon\home\silicon\native project Ω\silicon.yaml'
    $compiled = & $exe --json compile $uncYaml | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or !$compiled.valid) { throw 'UNC translation failed.' }
    $title = 'Unicode Ω, spaces, "quotes", dollar $; $(unchanged)' + [char]96
    $body = 'first line' + [char]10 + 'second line\with\slashes'
    $report = & $exe bug-report --title $title --body $body --dry-run | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $report.title -cne $title -or !$report.body.StartsWith($body)) { throw 'Quoted arguments changed.' }
    $pathTitle = 'C:\exact text'
    $pathBody = '\\wsl.localhost\Silicon\literal message'
    $report = & $exe bug-report --title $pathTitle --body $pathBody --dry-run | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $report.title -cne $pathTitle -or !$report.body.StartsWith($pathBody)) { throw 'Path-looking message content changed.' }
    $caddyInput = ':8089 {' + [char]10 + 'respond "hello"' + [char]10 + '}'
    $formatted = $caddyInput | & (Join-Path $PayloadRoot 'caddy.exe') fmt -
    if ($LASTEXITCODE -ne 0 -or "$formatted" -notmatch 'respond') { throw 'Piped stdin/stdout was not preserved.' }
    & $exe definitely-not-a-command 2>$null
    if ($LASTEXITCODE -ne 2) { throw 'Exit status was not preserved.' }
    & $exe settings set telemetry --off
    if ($LASTEXITCODE -ne 0) { throw 'Native settings failed.' }
    $settings = & $exe --json settings get | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $settings.telemetry -ne $false) { throw 'Settings did not persist.' }
    $log = Join-Path $env:RUNNER_TEMP 'windows-native-daemon.log'
    $err = Join-Path $env:RUNNER_TEMP 'windows-native-daemon.err'
    $daemon = Start-Process -FilePath $exe -ArgumentList @('serve', '--no-proxy', '--port', '1829') -RedirectStandardOutput $log -RedirectStandardError $err -PassThru -NoNewWindow
    try {
        $ready = $false
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            if ($daemon.HasExited) { throw "Native daemon exited: $(Get-Content $err -Raw)" }
            & $wsl -d Silicon -u silicon --exec test -f /home/silicon/native-cli-state/daemon.json
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
            Start-Sleep -Milliseconds 250
        }
        if (!$ready) { throw 'Native daemon did not become ready.' }
        $dashboard = & $exe --json web | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Native dashboard URL lookup failed.' }
        $address = [Uri]$dashboard.url
        $page = Invoke-WebRequest ($address.GetLeftPart([UriPartial]::Path))
        if ($page.StatusCode -ne 200) { throw 'Windows could not reach the WSL2 dashboard.' }
        & $exe web
        if ($LASTEXITCODE -ne 0) { throw 'Windows browser bridge failed.' }
        $connected = & $exe --json connect silicon.yaml | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or $connected.connection.id -ne 'windows-test:tos') { throw 'Native connect failed.' }
        & $exe ping windows-test:tos
        if ($LASTEXITCODE -ne 0) { throw 'Native ping failed.' }
        $configuration = & $exe --json config windows-test:tos
        if ($LASTEXITCODE -ne 0 -or "$configuration" -match 'fixture-not-a-real-token') { throw 'Config failed to redact token.' }
        & $exe disconnect windows-test:tos
        if ($LASTEXITCODE -ne 0) { throw 'Native disconnect failed.' }
    } finally {
        & $exe stop
        if (!$daemon.WaitForExit(10000)) { $daemon.Kill() }
    }
    $env:SILICON_HOME = $env:RUNNER_TEMP
    & $exe --version 2>$null
    if ($LASTEXITCODE -eq 0) { throw 'Windows credential home was accepted.' }
    Write-Host 'Windows native launcher + real WSL2 runtime E2E passed.'
} finally {
    Remove-Item Env:SILICON_HOME -ErrorAction SilentlyContinue
    & $wsl --unregister Silicon
}
