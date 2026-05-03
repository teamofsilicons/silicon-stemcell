# Installing Silicon

## One-liner install

**Mac / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.ps1 | iex
```

The installer will check for prerequisites, install what's missing (with your confirmation), download Silicon, and set up the `silicon` CLI command.

---

## What gets installed

| Component | Purpose | Install method |
|-----------|---------|---------------|
| Python 3.9+ | Runtime | brew / apt / winget |
| Node.js | For Claude Code, Codex & silicon-browser | brew / apt / winget |
| Claude Code CLI / Codex | AI backbone | `npm install -g @anthropic-ai/claude-code` / `npm install -g @openai/codex` |
| silicon-browser | Browser automation | `npm install -g silicon-browser` |
| pip packages | Python dependencies | `pip install -r requirements.txt` |

## What you'll need ready

- **Telegram bot token** — create one via [@BotFather](https://t.me/BotFather) on Telegram
- **OpenAI API key** (optional) — for incoming voice message transcription (Whisper)
- **Gemini API key** (optional) — for outgoing text-to-speech (Gemini TTS)

---

## Manual install

If you prefer to install manually:

```bash
# 1. Clone
git clone https://github.com/unlikefraction/silicon-stemcell.git ~/silicon
cd ~/silicon

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Make sure these are installed globally
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex
npm install -g silicon-browser

# 4. Configure (will prompt on first run)
python main.py
```

---

## Using the `silicon` CLI

After installation, open a new terminal and use:

```bash
silicon                  # Show status or list instances
silicon start            # Start silicon
silicon stop             # Stop silicon
silicon browser          # Open browser for manual login
silicon pull myagent     # Pull a silicon from Glass into ./myagent
silicon list             # List all installed instances
silicon status           # Show status
silicon update myagent   # Update an instance when it can merge safely
silicon install          # Install another instance
silicon help             # Show help
```

## Registry

All installations are tracked in `~/.silicon/registry.json`. Each instance has its own PID file for tracking running status. The CLI reads this registry to manage multiple silicon instances on the same machine.

## Re-running the installer

The installer is idempotent — running it again won't break anything. It will:
- Skip prerequisites that are already installed
- Detect existing installations
- Not duplicate PATH or registry entries
