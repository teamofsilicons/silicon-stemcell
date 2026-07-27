# Silicon Stemcell

Silicon is an autonomous manager-worker agent. It talks to contacts through Silicon Interface, enforces Glass-confirmed trust and keeps local memory, delegates real work to workers, uses Glass for crons/backups/control, and uses `silicon-browser` for browser automation.

## Runtime Shape

```
Glass -> Interface CLI v2 daemon -> durable inbox -> immediate dispatcher
                                                   -> manager -> workers
Periodic loop -> crons, local manager queues, release checks, worker checkbacks
```

The event loop:
1. consumes committed Interface inbox frames as soon as they arrive
2. checks Glass cron records locally
3. delivers local manager messages
4. checks completed workers
5. cleans old archives

The CLI owns WebSocket-before-sync barriers, signed/vector cursors, reconnect
backfill, inbox spooling, and delivery acknowledgements. Stemcell commits its
own inbox byte offset only after interpreting a complete durable frame. Manager
turns are serialized per contact, while unrelated contacts run independently,
so a long task cannot stop Interface ingestion.

Managers are persistent Claude or Codex sessions per fixed contact id. Workers
are separate Claude/Codex runs for browser, terminal, and writing tasks.

## Contact Model

Local contact state lives in `core/interface_state/contacts.json` at runtime.

Stemcell owns:
- fixed `carbon_id` / `silicon_id` contact keys
- the local cache and enforcement of effective trust
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
- canonical Team base trust and per-Silicon trust overrides

Effective trust is `central Carbon → per-Silicon override → Team base → very_low`.
Glass is authoritative, and the local `core/interface_state/trust_policy.json`
cache contains only the last confirmed revision. Local manager changes use
`trust/set`, which commits the same Glass override row used by the console
before updating `contacts.json`. Typed `carbon:<carbon_id>` and
`silicon:<silicon_id>` keys prevent cross-kind or display-name collisions.

## Setup

Install the supported machine-level CLI and pull the team from Glass:

```bash
python3 -m pip install --upgrade silicon-cli
silicon pull
```

The default Docker runtime supplies Node.js 22, Silicon Interface CLI v2,
Silicon Browser, Claude Code, Codex, Python, and Git. `silicon pull` hydrates,
configures, registers, and starts every Silicon returned by the Glass team
setup token.

For Stemcell source development without the managed runtime:

```bash
python3 -m pip install -r requirements.txt
```

Then provide Node.js 22+, an authenticated Claude Code or Codex CLI,
Silicon Browser, Silicon Interface CLI v2, and `.glass.json`, and run:

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

## Silicon Extend

Silicon Extend is an independently installed package, separate from the private
manager tools above. When this Silicon is connected, the package discovers its
team from Glass and uses that shared team directory as its source of truth. At
prompt construction the Stemcell fetches only its team's enabled directory;
those entries are exposed through the single dynamic form:

```json
{"tool":"extend","name":"gmail.messages.send","arguments":{}}
```

Setup can also be requested without attempting an invocation:

```json
{"tool":"extend","type":"request_setup","name":"gmail.messages.send","note":"Needed for the current task"}
```

The package uses the Silicon identity in `.glass.json`; connection secrets stay
server-side and are never copied into the Stemcell. If a connection is
missing—or setup is requested proactively—the package creates a durable request
for the assigned Carbon to complete inside Interface. Glass remains the
directory and proactive team-management surface; Interface has no Extend tab
or general management surface. Catalog lookup is best-effort and briefly
cached, so an unavailable directory does not prevent the Silicon from using its
private tools.

## Crons

User crons are Glass records read with Interface `crons list --mine --json`.

Stemcell computes due/missed fires locally and keeps watermarks in `core/interface_state/crons.json`.

Worker checkbacks stay local one-shot operational timers in `core/cron/checkbacks.json`.

## Backups

`.backupsilicon` is the single canonical manifest for Glass backups. Read that
tracked file for the current default coverage; documentation does not maintain
a second path list.

If Silicon edits another file that must persist, it should append the relative path to `.backupsilicon` without duplicates.

The Glass sidecar connects to `/ws/glass/agent/`, authenticates with the
`X-Silicon-Key` header, and runs a manifest backup on Glass `backup` commands.

## Team context and advertising memory

Glass is the authority for the Silicon roster, hierarchy, role metadata, and
canonical advertising memories. Stemcell mirrors that data locally:

```text
prompts/
├── TEAM.md
└── advertising/
    └── <silicon_id>.md
```

The repository ships with a clearly marked, unverified `TEAM.md` placeholder
and an empty `advertising/` directory, so both paths exist before the first
Glass connection. After a successful fetch, `TEAM.md` contains the current
hierarchy plus each active Silicon's name, description, job description, and
advertising-memory path. It never embeds the advertising-memory contents. The
manager prompt receives the hash-verified `TEAM.md` data together with every
currently hash-verified team advertising-memory mirror; the placeholder,
missing mirrors, stale mirrors, and locally modified peer files are not
injected.

Stemcell reconciles the complete context at startup and after reconnecting to
Glass, responds to `team_context.changed` invalidations, and performs a
conditional reconciliation every 60 seconds as recovery. On the existing
10-second loop it hashes the owning Silicon's local advertising file and
uploads only a changed value.

The manager normally replaces its own snapshot with the
`advertising_memory/update` tool. Manual edits to that same fixed file are also
noticed by the 10-second hash check. Updates use optimistic concurrency, so a
concurrent Glass edit preserves the local draft and requires an explicit
conflict resolution instead of silently overwriting either version.

Synchronization state is scoped to both the authenticated Glass server and a
one-way fingerprint of the configured Silicon key. A credential change,
confirmed revocation, team move, Silicon identity change, or Glass-server
change immediately hides the old `TEAM.md` and tracked peer mirrors while
Glass re-verifies identity. If an identity rotation would turn the former
owner's file into a peer mirror, unpublished or invalid local work is first
preserved privately under
`core/interface_state/team_context_drafts/`.

Advertising memory is visible to active Silicons on the same team. It is
teammate-authored, potentially stale data, not trusted instructions or
authorization. A Silicon must never advertise credentials, private Carbon
memory, or private conversations. The server and Stemcell both enforce the
100-line and 65,536-byte limits.

Fetched `TEAM.md` data and peer files are generated mirrors; the owning
Silicon's file is its managed local draft. Advertising-memory contents are
git-ignored and intentionally absent from `.backupsilicon` and `templates/`;
Glass restores canonical published data.

## Release checks and credential rotation

Read-only update checks use the canonical Stemcell Git repository as their
source of truth. They consider only exact stable `vMAJOR.MINOR.PATCH` tags,
select the highest version numerically, and report an update only when that
version is strictly newer than the running Stemcell. They never use `main`,
prereleases, timestamps, or Glass release records.

Each published stable tag is immutable. Protect the `v*` tag namespace, never
force-push or reuse a version, and create the tag only after the matching
`silicon.info` version and immutable runtime-image digest are committed.

The running Stemcell never mutates its source or Git configuration. Apply a
release through the machine-level CLI:

```bash
silicon update <name>
```

The CLI pins, verifies, and pre-stages the release before maintenance, lets the
running Stemcell finish already-admitted work, then performs the stop,
generation activation, restart, and stable-health validation itself. New
messages remain durably queued while the maintenance fence is active.

Glass credential rotation remains separate from release discovery. While the
current key is valid, rotate it explicitly with:

```bash
python3 update.py rotate-key
```

Stemcell generates and stages the replacement locally before the authenticated
rotation request. A lost response is resolved by probing the staged key, and no
key material is printed. The probe must return the configured Silicon's exact
identity from `/api/v1/silicons/me`; a generic infrastructure `404` or unrelated
`200` is never accepted as proof. Rotation is process-locked, authenticated requests
never follow redirects, and non-loopback HTTP origins are rejected. Once Glass
proves the replacement active, the pending key journal durably updates
`.glass.json`; legacy duplicates in `silicon.json`, `.env`, and `env.py` are
scrubbed after Glass proves the replacement. `.glass.json` is git-ignored and
updated first because messaging, backup, and browser traffic use it directly.

## Memory

- global: `prompts/MEMORY.md`
- carbon: `prompts/memory/carbons/{carbon_id}.md`
- silicon: `prompts/memory/silicons/{silicon_id}.md`
- projects: `prompts/memory/projects/`
- team roster and hierarchy: `prompts/TEAM.md` (generated by Glass)
- team-visible advertising: `prompts/advertising/{silicon_id}.md` (mirrored from Glass)

## Project Structure

```
core/interface.py             # Interface CLI adapter, contacts, events, replies
core/extend.py                # team Extend discovery, execution, setup messages
core/team_context.py          # Glass TEAM.md and advertising-memory synchronization
core/trust.py                 # Glass-first trust policy sync and local mutations
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
