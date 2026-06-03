# Silicon Stemcell

An autonomous AI assistant framework that lives on Telegram. Silicon (the AI) serves Carbons (humans) through a manager-worker architecture powered by Claude Code CLI or Codex.

Silicon isn't a chatbot. It's a full agent — it delegates work to specialized workers, browses the web, runs terminal commands, writes content, manages cron jobs, maintains memory across sessions, and handles multiple users with trust-based access control.

## How It Works

```
Telegram ←→ Event Loop ←→ Manager (Claude or Codex) ←→ Workers (Claude or Codex)
                ↕
         Crons, Memory, Messages
```

**Event loop** runs every 10 seconds:
1. Polls Telegram for new messages (text, photos, voice, files — all native)
2. Checks cron jobs
3. Delivers inter-manager messages
4. Detects completed workers
5. Cleans old archives

**Manager** is a persistent Claude or Codex session per user. It doesn't do the work itself — it plans, delegates to workers, and communicates with the carbon. It outputs structured JSON tool calls.

**Workers** are separate Claude or Codex processes that do the actual work:
- **Browser** — Browser access via silicon-browser + terminal + web search. Queued (one at a time) to prevent race conditions.
- **Terminal** — Full terminal access. Code, system ops, anything. Runs in parallel.
- **Writer** — Writing-specialized with anti-AI-slop skills baked in. Runs in parallel.

## Setup

### Prerequisites

- Python 3.9+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) or Codex installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) [Claude for Chrome](https://chromewebstore.google.com/detail/claude/danfohhogdcfmbihjbfmcaeaonjameja) extension for browser workers

### Install

```bash
git clone <repo-url> silicon-stemcell
cd silicon-stemcell
pip install -r requirements.txt
```

### Provider Selection

`silicon.json` controls the manager and worker backends independently:

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

`brain` chooses the manager. Each worker list is tried in order for new workers; once a worker starts, its provider and session are persisted for future `worker/message` calls.

### Configure

On first run, Silicon will prompt you for the Telegram bot token and save it to `env.py`. Or create it manually:

```python
# env.py
TELEGRAM_BOT_TOKEN = "your-bot-token-here"
OPENAI_API_KEY = "your-openai-key-here"  # for incoming voice transcription (Whisper)
GEMINI_API_KEY = "your-gemini-key-here"  # for outgoing TTS (Gemini)
```

For voice bubbles (`.ogg/opus`) on Telegram, install `ffmpeg` on your system. Without `ffmpeg`, voice messages fall back to `.wav` audio attachments.

### Run

```bash
python main.py
```

Send a message to your bot on Telegram. The first user to message becomes the **central carbon** with full (ultimate) trust.

## Telegram Native Media

Silicon handles all Telegram media types natively — both incoming and outgoing.

### Incoming (Carbon → Silicon)

| What carbon sends | What the manager sees |
|---|---|
| Text | Plain text |
| Photo | `[Photo received] (@/path/to/photo.jpg)` — viewable in Claude Code |
| Video | `[Video received] (saved at: /path/to/video.mp4)` |
| Voice message | Auto-transcribed via Whisper: `[Voice message transcription]: hey what's up` |
| Audio file | Downloaded + transcription attempted |
| Document/file | `[File received: report.pdf] (@/path/to/file.pdf)` |
| Sticker | `[Sticker 😎]` |
| Caption | Extracted alongside the media |

Voice messages are transcribed automatically using OpenAI Whisper. If transcription fails, the manager gets `[Audio message couldn't be transcribed]` with the file path.

### Outgoing (Silicon → Carbon)

The manager uses the `reply` tool with inline syntax to send rich media:

```
check out this screenshot
[file=/path/to/screenshot.png]
what do you think?
[voice=honestly I think this turned out pretty sick]
let me know if you want changes
```

This sends 5 messages in order:
1. Text: "check out this screenshot"
2. Photo: screenshot.png (auto-detected from extension)
3. Text: "what do you think?"
4. Voice bubble: TTS of "honestly I think this turned out pretty sick"
5. Text: "let me know if you want changes"

**Syntax:**
- `[file=/absolute/path/to/anything]` — sends photo/video/audio/document (auto-detected by extension)
- `[voice=text to speak out loud]` — converts to speech via Gemini TTS, sent as Telegram voice bubble. Supports inline audio tags (`[laughs]`, `[whispers]`, `[annoyance]`, `[short pause]`, etc.) and an optional structured persona/scene/director's-notes/transcript format — see `prompts/MANAGER_TOOLS.md`.

File types auto-detected:
- `.jpg`, `.png`, `.gif`, `.webp` → photo
- `.mp4`, `.mov`, `.avi` → video
- `.mp3`, `.m4a`, `.ogg` → audio
- Everything else → document

Unrecognized `[brackets]` are left as plain text — nothing breaks.

## Glass Integration

When a stemcell folder has been claimed from Glass, it contains a local `.glass.json`.

The helper module `core/glass.py` can then:

- Push the current folder snapshot back to Glass with `push_current_folder_now()`
- List silicon threads with `list_silicon_threads()`
- Read direct messages with `get_thread_messages(target_username)`
- Send direct silicon messages with `send_silicon_message(...)`

Backups use `.backupsilicon`: one path or glob per line, relative to the
silicon folder. The Glass sidecar accepts backup requests from Glass, archives
those paths, and uploads the archive to `/api/v1/silicon-backups/`. The
installable `silicon` command uses the same manifest for `silicon push <name>
now` and `silicon backup <name> now`.

This keeps Telegram as the carbon-facing surface while Glass handles silicon storage, respawn, and silicon-to-silicon transport.

## Architecture

### Multi-Carbon (Multi-User)

Silicon supports multiple users simultaneously. Each user (carbon) gets:
- Their own manager session (persistent Claude or Codex session)
- Their own workers (invisible to other users)
- Their own memory file at `prompts/memory/people/{carbon_id}.md`
- A trust level that controls what they can do

Managers run in parallel via `ThreadPoolExecutor`. They communicate with each other through the `message_manager` tool (delivered on the next event loop tick).

### Trust Levels

| Level | Who | Access |
|---|---|---|
| `ultimate` | Central carbon (first user) | Everything. Full access. |
| `very_high` | VIP | Almost everything, can modify configs and code |
| `high` | Trusted | All workers, own memory, cross-manager messaging |
| `ok` | Reasonable | All worker types for non-sensitive tasks |
| `low` | Known but untrusted | Simple terminal workers only |
| `very_low` | Unknown (default for new users) | Very limited, no sensitive ops |

### Manager Tools

The manager orchestrates everything through JSON tool calls:

| Tool | Purpose |
|---|---|
| `reply` | Send message to carbon (supports `[file=...]` and `[voice=...]` inline) |
| `worker/browser` | Spawn a browser worker |
| `worker/terminal` | Spawn a terminal worker |
| `worker/writer` | Spawn a writer worker |
| `worker` (status/stop/checkback/list) | Manage running workers |
| `message_manager` | Message another carbon's manager |
| `change_carbon_id` | Rename a carbon's ID |
| `new_session` | Fresh session (clears context) |
| `restart_silicon_service` | Restart the Python process |
| `do_nothing` | End the current manager loop |

Plus direct bash access for simple tasks (editing memory, cron jobs, etc).

### Cron System

Managers can create cron jobs by editing `core/cron/jobs.py`. Crons are stateless and timezone-agnostic (UTC timestamps). When triggered, they send a message back to the manager who can then decide what to do.

Workers also support **checkbacks** — timed status checks that auto-trigger and report back to the manager.

### Memory System

- `prompts/MEMORY.md` — hot-cache memory (quick-access facts)
- `prompts/memory/people/{carbon_id}.md` — per-user memory
- `prompts/memory/projects/` — per-project memory
- `prompts/LORE.md` — Silicon's backstory and identity
- `prompts/SOUL.md` — personality definition
- `prompts/SILICON.md` — writing style guide

All editable by the manager at runtime.

## Project Structure

```
silicon-stemcell/
├── main.py                    # Entry point — event loop, tool execution
├── manager.py                 # Claude/Codex invocation for managers
├── config.py                  # Event loop config, tick interval
├── env.py                     # Secrets (bot token, API keys) — gitignored
├── requirements.txt           # Python dependencies
│
├── core/
│   ├── carbon_id.py           # Carbon ID renaming across entire system
│   ├── messages.py            # Inter-manager messaging
│   ├── telegram/
│   │   ├── __init__.py        # Telegram bot (polling, media, TTS, send/receive)
│   │   ├── config.py          # Bot token, API URLs, media dir
│   │   ├── contacts.json      # User database
│   │   └── media/             # Downloaded media files — gitignored
│   └── cron/
│       ├── __init__.py        # Cron runner
│       ├── jobs.py            # Cron job definitions
│       └── checkback.py       # Worker checkback system
│
├── prompts/
│   ├── DNA.py                 # Prompt assembler
│   ├── SOUL.md                # Personality
│   ├── SILICON.md             # Writing style guide
│   ├── LORE.md                # Backstory
│   ├── BOOT.md                # First-run setup instructions
│   ├── MANAGER.md             # Manager role definition
│   ├── MANAGER_TOOLS.md       # All available tools
│   ├── MEMORY.md              # Hot-cache memory
│   ├── CONTACTS.md            # Contact system docs
│   ├── memory/                # Per-user and per-project memory
│   ├── trust/                 # Trust level definitions
│   └── worker/                # Worker prompts and tools
│
├── worker/
│   ├── handler.py             # Worker lifecycle management
│   └── outputs/               # Worker output files — gitignored
│
└── sessions/                  # Persistent manager sessions — gitignored
```

## Commands

Users can send these commands via Telegram:
- `/start` — Silicon confirms it's online
- `/new` — Start a fresh session (clears conversation context)

## Stemcell?

This repo is the stemcell — the base template. Clone it, run it, and let Silicon differentiate through its first conversation with you. It'll ask who you are, what you need, and shape itself accordingly. The BOOT.md guides the initial setup, then deletes itself.
