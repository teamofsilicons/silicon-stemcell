# Installing Silicon

## One-liner

Mac / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.ps1 | iex
```

## Requirements

- Python 3.9+
- Node.js
- Claude Code CLI or Codex
- `silicon-browser`
- Silicon Interface CLI (`./.silicon-interface/bin/si`, `si`, or `silicon-interface`)
- `.glass.json` for Glass-connected instances

## Manual Install

```bash
git clone https://github.com/unlikefraction/silicon-stemcell.git ~/silicon
cd ~/silicon
python3 -m pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex
npm install -g silicon-browser
python3 main.py
```

Set `BROWSER_PROFILE` in `env.py` if you want a profile name other than `silicon`.

Silicon Interface and Glass provide contact transport, media, STT/TTS, crons, take-back, remote browser events, backups, and control.

## CLI

```bash
silicon
silicon start <name>
silicon stop <name>
silicon browser <name>
silicon pull <name>
silicon status <name>
silicon update <name>
silicon help
```

The installer is idempotent. It skips installed prerequisites, updates registry entries, and preserves local Interface state.
