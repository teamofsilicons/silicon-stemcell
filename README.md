# Silicon Stemcell

Silicon is an autonomous manager-worker agent. It talks to contacts through Silicon Interface, keeps local trust and memory, delegates real work to workers, uses Glass for crons/backups/control, and uses `silicon-browser` for browser automation.

## Runtime Shape

```
Silicon Interface -> event loop -> manager -> workers
                           |
                  Glass crons, local memory,
                  manager queues, worker checkbacks
```

The event loop:
1. reads Interface events
2. checks Glass cron records locally
3. delivers local manager messages
4. checks completed workers
5. cleans old archives

Managers are persistent Claude or Codex sessions per fixed contact id. Workers are separate Claude/Codex runs for browser, terminal, and writing tasks.

## Contact Model

Local contact state lives in `core/interface_state/contacts.json` at runtime.

Stemcell owns:
- fixed `carbon_id` / `silicon_id` contact keys
- local trust levels
- central carbon flag
- manager sessions
- memory files
- cron execution watermarks

Interface/Glass owns:
- rooms
- events
- media
- read receipts
- STT/TTS
- take-back
- remote-browser events
- cron records
- backups/control

The first carbon discovered becomes central carbon with `ultimate` trust. Later contacts default to `very_low`. IDs are never renamed.

## Setup

Prerequisites:
- Python 3.9+
- Claude Code CLI or Codex installed and authenticated
- Silicon Interface CLI available as `./.silicon-interface/bin/si`, `si`, or `silicon-interface`
- `silicon-browser`
- `.glass.json` when this instance is connected to Glass

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run:

```bash
python3 main.py
```

Open the shared browser profile manually when needed:

```bash
python3 main.py browser
```

## Provider Selection

`silicon.json` controls manager and worker backends:

```json
{
  "brain": "claude",
  "workers": {
    "browser": ["claude", "codex"],
    "terminal": ["claude", "codex"],
    "writer": ["claude", "codex"]
  }
}
```

New workers try providers in order. Once a worker starts, its provider/session is persisted behind the worker id.

## Interface Media

Incoming `m.text`, `m.image`, `m.file`, `m.voice`, and `m.tts` events are normalized into manager context.

Media is downloaded under `core/interface_state/media/` and absolute paths are included. Voice/TTS events use transcript from Interface when present, otherwise stemcell calls Interface STT.

Replies use Interface:
- text: `send`
- `[file=/path]`: `send-file`
- `[voice=...]`: `tts --room`

## Manager Tools

Main tools:
- `reply`
- `message_manager`
- `remote_browser`
- `take_back`
- `cron/create`, `cron/update`, `cron/delete`, `cron/list`
- worker tools
- `new_session`
- `restart_silicon_service`
- `do_nothing`

See `prompts/MANAGER_TOOLS.md`.

## Crons

User crons are Glass records read with Interface `crons list --mine --json`.

Stemcell computes due/missed fires locally and keeps watermarks in `core/interface_state/crons.json`.

Worker checkbacks stay local one-shot operational timers in `core/cron/checkbacks.json`.

## Backups

`.backupsilicon` is the manifest for Glass backups. Default coverage:

```text
prompts/MEMORY.md
prompts/memory/**
```

If Silicon edits another file that must persist, it should append the relative path to `.backupsilicon` without duplicates.

The Glass sidecar connects to `/ws/glass/agent/?silicon_key=<api_key>` and runs a manifest backup on Glass `backup` commands.

## Memory

- global: `prompts/MEMORY.md`
- carbon: `prompts/memory/carbons/{carbon_id}.md`
- silicon: `prompts/memory/silicons/{silicon_id}.md`
- projects: `prompts/memory/projects/`

## Project Structure

```
core/interface.py             # Interface CLI adapter, contacts, events, replies
core/interface_state/         # runtime state, ignored
core/cron/                    # Glass cron execution + local checkbacks
core/messages.py              # local manager queue
core/backup.py                # manifest backup upload
glass_agent.py                # Glass live sidecar
manager.py                    # manager backend invocation
main.py                       # event loop and tool execution
worker/handler.py             # worker lifecycle
prompts/                      # Silicon prompt/memory system
```

This repo is the stemcell. It starts generic and differentiates through memory, prompts, trust, and the first real conversations.
