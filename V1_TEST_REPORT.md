# Silicon Stemcell v1 Test Report

Date: 2026-06-04

Branch: `v1`

Migration code tested: `1837227 Complete Interface event lifecycle`

## What Passed

- Pushed the completed migration branch to `origin/v1`.
- `python3 -m compileall .` passed.
- `python3 -m unittest discover -s tests` passed.
- `bash -n install.sh` passed.
- Direct `py_compile` checks passed for the main runtime modules touched by the migration.
- Clean temporary venv install from `requirements.txt` passed.
- Clean temporary venv imports passed for `requests`, `websockets`, and `croniter`.
- Full unit test suite passed inside the clean venv.
- Fake Interface CLI smoke passed with stub `si` and stub `silicon-browser` for:
  - Interface text send
  - remote browser share
  - remote browser close
  - take-back complete
  - explicit event take-back
  - cron create/update/delete
- Entrypoint smoke passed with a fake `InterfaceClient` for:
  - incoming Interface event ingestion
  - read receipt marking
  - due cron injection
  - manager queue routing
  - rich reply with text, file, and voice segments
  - remote browser share/close tools
  - take-back tools
  - cron manager tools

## Breaks / Blockers Found

- The current global Python environment is missing `croniter`.
  - This is covered by `requirements.txt`, and a clean venv install proves the dependency set works.
  - Any existing deployed stemcell environment still needs `pip install -r requirements.txt` before cron execution can use the package.
- Real Interface smoke was not run because the environment did not expose a live usable `si` session.
- Real Glass sidecar smoke was not run because live Glass credentials/session state were not available for verification.
- Real `silicon-browser` remote share smoke was not run against a live browser service. Stubbed command behavior passed.
- `install.ps1` was not syntax-checked because neither `pwsh` nor `powershell` was available in this environment.
- Interface CLI command shapes are implemented from the migration plan, but still need a live contract pass against the actual CLI. The highest-risk shapes are:
  - direct-room discovery/creation arguments
  - `send-file`
  - `tts --room`
  - `remote-browser`
  - cron create/update target encoding
  - take-back force/reason flags

## Things That Might Need Improvement

- Add recorded or real contract tests for the `si` JSON payloads so CLI drift gets caught before runtime.
- Add a fake websocket server test for `glass_agent.py`, including reconnect, ping/pong, billing, and backup command handling.
- Add a listener restart test around `listen all` so process death and room membership refresh behavior are covered.
- Add a Windows CI parse check for `install.ps1`.
- Add a `scripts/doctor` health check that verifies:
  - Python dependencies
  - Interface CLI availability
  - Glass config
  - `silicon-browser` availability
  - writable Interface state/media paths
- Add runtime log throttling for repeated missing-CLI or network failures so long unattended runs do not spam logs.
- Add an explicit one-time migration report when old Telegram contacts are imported into Interface contact state.
- Add stricter validation and clearer errors for cron target schemas before calling the Interface CLI.

## Manual Smoke Still Needed

Run this with real credentials and a live Interface/Glass/browser setup:

- receive text
- receive image/file media and verify local download paths
- receive voice and verify transcript fallback
- send text reply
- send file reply
- send TTS/voice reply
- generate remote browser share link
- close remote browser session and verify profile persistence
- complete a pending take-back request
- execute explicit event take-back
- create/list/update/delete Glass cron records
- let local cron executor trigger a due cron
- start Glass sidecar and verify live backup command handling
