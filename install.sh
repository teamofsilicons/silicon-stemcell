#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# Silicon Stemcell – Universal Installer (Mac / Linux)
# curl -fsSL https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.sh | bash
# ─────────────────────────────────────────────────────────────

REPO_URL="https://github.com/unlikefraction/silicon-stemcell.git"
REPO_ZIP="https://github.com/unlikefraction/silicon-stemcell/archive/refs/heads/main.zip"
REGISTRY_DIR="$HOME/.silicon"
REGISTRY_FILE="$REGISTRY_DIR/registry.json"
BIN_DIR="$REGISTRY_DIR/bin"
CLI_SCRIPT="$BIN_DIR/silicon"

# ── Colors & helpers ──────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'
DIM='\033[2m'; RESET='\033[0m'

info()    { printf "${BLUE}→${RESET} %s\n" "$*"; }
success() { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn()    { printf "${YELLOW}⚠${RESET} %s\n" "$*"; }
error()   { printf "${RED}✗${RESET} %s\n" "$*"; }
header()  { printf "\n${BOLD}${CYAN}── %s ──${RESET}\n\n" "$*"; }
ask()     { printf "${BOLD}? %s${RESET} " "$1"; }

confirm() {
    ask "$1 [Y/n]"
    read -r ans </dev/tty
    case "$ans" in
        [nN]*) return 1 ;;
        *) return 0 ;;
    esac
}

choose_worker_provider_order() {
    local worker_type="$1"
    local default_choice="${2:-claude}"
    local choice
    local result

    printf "${BOLD}? Which provider should %s workers use – claude or codex?${RESET} [%s]: " "$worker_type" "$default_choice" >&2
    read -r choice </dev/tty
    choice="${choice:-$default_choice}"
    case "$choice" in
        codex)
            if confirm "Keep claude as fallback for $worker_type workers?"; then
                result='["codex", "claude"]'
            else
                result='["codex"]'
            fi
            ;;
        *)
            if confirm "Keep codex as fallback for $worker_type workers?"; then
                result='["claude", "codex"]'
            else
                result='["claude"]'
            fi
            ;;
    esac
    echo "$result"
}

# If piped (curl | bash), we need /dev/tty for user input
read_input() {
    local prompt="$1" default="${2:-}"
    if [ -n "$default" ]; then
        printf "${BOLD}? %s [%s]:${RESET} " "$prompt" "$default" >&2
    else
        printf "${BOLD}? %s:${RESET} " "$prompt" >&2
    fi
    read -r val </dev/tty
    if [ -z "$val" ] && [ -n "$default" ]; then
        val="$default"
    fi
    echo "$val"
}

read_secret() {
    local prompt="$1"
    printf "${BOLD}? %s:${RESET} " "$prompt" >&2
    local val=""
    local char=""
    while IFS= read -r -s -n1 char </dev/tty; do
        if [[ -z "$char" ]]; then
            break
        elif [[ "$char" == $'\x7f' ]] || [[ "$char" == $'\b' ]]; then
            if [[ -n "$val" ]]; then
                val="${val%?}"
                printf "\b \b" >&2
            fi
        else
            val="${val}${char}"
            printf "*" >&2
        fi
    done
    printf "\n" >&2
    echo "$val"
}

# ── Banner ────────────────────────────────────────────────────

clear 2>/dev/null || true
printf "${BOLD}${CYAN}"
cat << 'BANNER'

  ███████╗██╗██╗     ██╗ ██████╗ ██████╗ ███╗   ██╗
  ██╔════╝██║██║     ██║██╔════╝██╔═══██╗████╗  ██║
  ███████╗██║██║     ██║██║     ██║   ██║██╔██╗ ██║
  ╚════██║██║██║     ██║██║     ██║   ██║██║╚██╗██║
  ███████║██║███████╗██║╚██████╗╚██████╔╝██║ ╚████║
  ╚══════╝╚═╝╚══════╝╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═══╝
                                        stemcell

BANNER
printf "${RESET}"
echo ""
info "Universal installer for Silicon – your autonomous AI agent"
echo ""

# ═════════════════════════════════════════════════════════════
# STEP 1: System checks
# ═════════════════════════════════════════════════════════════

header "Step 1 · System Checks"

# Detect OS
OS="unknown"
case "$(uname -s)" in
    Darwin*) OS="mac" ;;
    Linux*)  OS="linux" ;;
    *)       OS="unknown" ;;
esac

if [ "$OS" = "unknown" ]; then
    error "Unsupported operating system: $(uname -s)"
    error "This installer supports macOS and Linux. For Windows, use install.ps1"
    exit 1
fi

success "Operating system: $(uname -s) ($(uname -m))"

# Check sudo/admin
if [ "$EUID" -eq 0 ] 2>/dev/null || [ "$(id -u)" -eq 0 ]; then
    warn "Running as root. Some tools will be installed system-wide."
else
    info "Running as normal user. You may be prompted for sudo if needed."
fi

# Check disk space (need at least 500MB)
if command -v df &>/dev/null; then
    if [ "$OS" = "mac" ]; then
        avail_kb=$(df -k "$HOME" | tail -1 | awk '{print $4}')
    else
        avail_kb=$(df -k "$HOME" | tail -1 | awk '{print $4}')
    fi
    avail_mb=$((avail_kb / 1024))
    if [ "$avail_mb" -lt 500 ]; then
        error "Low disk space: ${avail_mb}MB available. Need at least 500MB."
        exit 1
    fi
    success "Disk space: ${avail_mb}MB available"
fi

# ═════════════════════════════════════════════════════════════
# STEP 2: Prerequisites
# ═════════════════════════════════════════════════════════════

header "Step 2 · Prerequisites"

install_glass_cli() {
    local glass_repo="unlikefraction/glass"
    local glass_archive="https://codeload.github.com/${glass_repo}/tar.gz/refs/heads/main"
    local glass_dir="$HOME/.glass"
    local glass_bin_dir="$HOME/.local/bin"
    local glass_wrapper="$glass_bin_dir/glass"
    if command -v glass &>/dev/null; then
        success "glass CLI: installed"
        return 0
    fi

    warn "glass CLI not found"
    mkdir -p "$glass_dir" "$glass_bin_dir"
    local tmp_dir
    tmp_dir=$(mktemp -d /tmp/glass-install-XXXXXX)
    trap 'rm -rf "$tmp_dir"' RETURN

    info "Installing glass CLI..."
    if command -v curl &>/dev/null; then
        curl -fsSL "$glass_archive" | tar -xzf - -C "$tmp_dir"
    elif command -v wget &>/dev/null; then
        wget -qO- "$glass_archive" | tar -xzf - -C "$tmp_dir"
    else
        warn "Could not auto-install glass CLI. Need curl or wget."
        return 1
    fi

    local src_dir
    src_dir=$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d -name 'glass-*' | head -1)
    if [ -z "$src_dir" ] || [ ! -f "$src_dir/glass" ] || [ ! -f "$src_dir/glass_cli.py" ]; then
        warn "Could not auto-install glass CLI. Downloaded archive was invalid."
        return 1
    fi

    rm -rf "$glass_dir"
    mkdir -p "$glass_dir"
    (
        cd "$src_dir"
        tar --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' -cf - .
    ) | (
        cd "$glass_dir"
        tar -xf -
    )

    chmod +x "$glass_dir/glass" "$glass_dir/install.sh"
    ln -sf "$glass_dir/glass" "$glass_wrapper"

    if command -v glass &>/dev/null; then
        success "glass CLI installed"
        return 0
    fi

    warn "glass CLI install finished, but it's not on PATH yet."
    warn "Add ~/.local/bin to PATH."
    return 1
}

# ── Python 3.9+ ───────────────────────────────────────────────

install_python() {
    if [ "$OS" = "mac" ]; then
        if command -v brew &>/dev/null; then
            info "Installing Python via Homebrew..."
            brew install python3
        else
            error "Homebrew not found. Install Python 3.9+ manually from https://python.org/downloads"
            error "Or install Homebrew first: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            exit 1
        fi
    else
        if command -v apt-get &>/dev/null; then
            info "Installing Python via apt..."
            sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip python3-venv
        elif command -v dnf &>/dev/null; then
            info "Installing Python via dnf..."
            sudo dnf install -y python3 python3-pip
        elif command -v yum &>/dev/null; then
            info "Installing Python via yum..."
            sudo yum install -y python3 python3-pip
        elif command -v pacman &>/dev/null; then
            info "Installing Python via pacman..."
            sudo pacman -S --noconfirm python python-pip
        else
            error "No supported package manager found. Install Python 3.9+ manually."
            exit 1
        fi
    fi
}

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -n "$PYTHON_CMD" ]; then
    success "Python: $($PYTHON_CMD --version) ($PYTHON_CMD)"
else
    warn "Python 3.9+ not found"
    if confirm "Install Python?"; then
        install_python
        # Re-check
        for cmd in python3 python; do
            if command -v "$cmd" &>/dev/null; then
                ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
                major=$(echo "$ver" | cut -d. -f1)
                minor=$(echo "$ver" | cut -d. -f2)
                if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
                    PYTHON_CMD="$cmd"
                    break
                fi
            fi
        done
        if [ -z "$PYTHON_CMD" ]; then
            error "Python installation failed. Install Python 3.9+ manually and re-run."
            exit 1
        fi
        success "Python installed: $($PYTHON_CMD --version)"
    else
        error "Python 3.9+ is required. Aborting."
        exit 1
    fi
fi

install_glass_cli || true

# ── Node.js / npm ─────────────────────────────────────────────

install_git() {
    if [ "$OS" = "mac" ]; then
        if command -v brew &>/dev/null; then
            info "Installing git via Homebrew..."
            brew install git
        else
            error "Install git manually or install Homebrew first."
            exit 1
        fi
    else
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y git
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y git
        elif command -v yum &>/dev/null; then
            sudo yum install -y git
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm git
        else
            error "No supported package manager found to install git."
            exit 1
        fi
    fi
}

install_node() {
    if [ "$OS" = "mac" ]; then
        if command -v brew &>/dev/null; then
            info "Installing Node.js via Homebrew..."
            brew install node
        else
            error "Install Node.js from https://nodejs.org or install Homebrew first."
            exit 1
        fi
    else
        if command -v apt-get &>/dev/null; then
            info "Installing Node.js via apt..."
            # Use NodeSource for a recent version
            if ! command -v curl &>/dev/null; then
                sudo apt-get install -y curl
            fi
            curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
            sudo apt-get install -y nodejs
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y nodejs npm
        elif command -v yum &>/dev/null; then
            sudo yum install -y nodejs npm
        elif command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm nodejs npm
        else
            error "Install Node.js from https://nodejs.org manually."
            exit 1
        fi
    fi
}

if command -v node &>/dev/null && command -v npm &>/dev/null; then
    success "Node.js: $(node --version)"
else
    warn "Node.js / npm not found (needed for Claude Code CLI, Codex CLI & silicon-browser)"
    if confirm "Install Node.js?"; then
        install_node
        if command -v node &>/dev/null; then
            success "Node.js installed: $(node --version)"
        else
            error "Node.js installation failed. Install from https://nodejs.org and re-run."
            exit 1
        fi
    else
        error "Node.js is required for AI backends and silicon-browser. Aborting."
        exit 1
    fi
fi

# ── git ───────────────────────────────────────────────────────

if command -v git &>/dev/null; then
    success "git: $(git --version | head -1)"
else
    warn "git not found"
    if confirm "Install git?"; then
        install_git
        if command -v git &>/dev/null; then
            success "git installed: $(git --version | head -1)"
        else
            warn "git install did not succeed. Silicon can still run, but updates will use a weaker merge strategy."
        fi
    else
        warn "Skipping git. Silicon can still run, but updates will use a weaker merge strategy."
    fi
fi

# ── Claude Code CLI ───────────────────────────────────────────

if command -v claude &>/dev/null; then
    success "Claude Code CLI: installed"
else
    warn "Claude Code CLI not found"
    if confirm "Install Claude Code CLI via npm?"; then
        info "Installing @anthropic-ai/claude-code globally..."
        npm install -g @anthropic-ai/claude-code
        if command -v claude &>/dev/null; then
            success "Claude Code CLI installed"
        else
            warn "Claude Code CLI installation failed."
            warn "Try manually: npm install -g @anthropic-ai/claude-code"
        fi
    else
        warn "Skipping Claude Code CLI."
    fi
fi

# ── Codex CLI ─────────────────────────────────────────────────

if command -v codex &>/dev/null; then
    success "Codex CLI: installed"
else
    warn "Codex CLI not found"
    if confirm "Install Codex CLI via npm?"; then
        info "Installing @openai/codex globally..."
        npm install -g @openai/codex
        if command -v codex &>/dev/null; then
            success "Codex CLI installed"
        else
            warn "Codex CLI installation failed."
            warn "Try manually: npm install -g @openai/codex"
        fi
    else
        warn "Skipping Codex CLI."
    fi
fi

if ! command -v claude &>/dev/null && ! command -v codex &>/dev/null; then
    error "Install at least one AI backend: Claude Code CLI or Codex CLI."
    exit 1
fi

# ── Manager brain ─────────────────────────────────────────────

BRAIN_CHOICE="claude"
BROWSER_WORKERS='["claude"]'
TERMINAL_WORKERS='["claude"]'
WRITER_WORKERS='["claude"]'
if command -v claude &>/dev/null && command -v codex &>/dev/null; then
    ask "Which brain should Silicon use – claude or codex? [claude]"
    read -r brain_ans </dev/tty
    brain_ans="${brain_ans:-claude}"
    case "$brain_ans" in
        codex) BRAIN_CHOICE="codex" ;;
        *) BRAIN_CHOICE="claude" ;;
    esac
    BROWSER_WORKERS=$(choose_worker_provider_order "browser" "claude")
    TERMINAL_WORKERS=$(choose_worker_provider_order "terminal" "claude")
    WRITER_WORKERS=$(choose_worker_provider_order "writer" "claude")
elif command -v codex &>/dev/null; then
    BRAIN_CHOICE="codex"
    BROWSER_WORKERS='["codex"]'
    TERMINAL_WORKERS='["codex"]'
    WRITER_WORKERS='["codex"]'
fi

# ── silicon-browser ───────────────────────────────────────────

if command -v silicon-browser &>/dev/null; then
    success "silicon-browser: installed"
else
    warn "silicon-browser not found"
    if confirm "Install silicon-browser via npm?"; then
        info "Installing silicon-browser globally..."
        npm install -g silicon-browser
        if command -v silicon-browser &>/dev/null; then
            success "silicon-browser installed"
        else
            warn "silicon-browser install may have failed. Browser workers may not work."
            warn "Try manually: npm install -g silicon-browser"
        fi
    else
        warn "Skipping silicon-browser. Browser workers will not be available."
    fi
fi

# ═════════════════════════════════════════════════════════════
# Create a new silicon?
# ═════════════════════════════════════════════════════════════

SKIP_SILICON=false
if ! confirm "Create a new silicon?"; then
    SKIP_SILICON=true
fi

if [ "$SKIP_SILICON" = "false" ]; then

# ═════════════════════════════════════════════════════════════
# STEP 3: Download the repo
# ═════════════════════════════════════════════════════════════

header "Step 3 · Download Silicon"

INSTANCE_NAME=$(read_input "Name your silicon" "silicon")

# Check if name is already registered
if [ -f "$REGISTRY_FILE" ] && command -v "$PYTHON_CMD" &>/dev/null; then
    NAME_EXISTS=$("$PYTHON_CMD" -c "
import json, sys
with open('$REGISTRY_FILE') as f:
    reg = json.load(f)
for inst in reg.get('installations', []):
    if inst.get('name') == '$INSTANCE_NAME':
        print('true')
        sys.exit(0)
print('false')
" 2>/dev/null || echo "false")

    if [ "$NAME_EXISTS" = "true" ]; then
        error "A silicon named '$INSTANCE_NAME' already exists."
        info "Pick a different name or run: silicon list"
        exit 1
    fi
fi

INSTALL_DIR="$(pwd)/$INSTANCE_NAME"

# Check if directory already exists
if [ -d "$INSTALL_DIR" ]; then
    error "Directory '$INSTANCE_NAME' already exists here."
    info "Pick a different name or remove the existing directory."
    exit 1
fi

info "Creating $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

# Clone or download
if command -v git &>/dev/null; then
    info "Cloning Silicon..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    success "Cloned to $INSTALL_DIR"
elif command -v curl &>/dev/null; then
    info "Downloading Silicon..."
    TMP_ZIP=$(mktemp /tmp/silicon-XXXXXX.zip)
    TMP_DIR=$(mktemp -d /tmp/silicon-extract-XXXXXX)
    curl -fsSL "$REPO_ZIP" -o "$TMP_ZIP"
    unzip -q "$TMP_ZIP" -d "$TMP_DIR"
    mv "$TMP_DIR"/silicon-main/* "$INSTALL_DIR/"
    rm -rf "$TMP_ZIP" "$TMP_DIR"
    success "Downloaded to $INSTALL_DIR"
elif command -v wget &>/dev/null; then
    info "Downloading Silicon..."
    TMP_ZIP=$(mktemp /tmp/silicon-XXXXXX.zip)
    TMP_DIR=$(mktemp -d /tmp/silicon-extract-XXXXXX)
    wget -q "$REPO_ZIP" -O "$TMP_ZIP"
    unzip -q "$TMP_ZIP" -d "$TMP_DIR"
    mv "$TMP_DIR"/silicon-main/* "$INSTALL_DIR/"
    rm -rf "$TMP_ZIP" "$TMP_DIR"
    success "Downloaded to $INSTALL_DIR"
else
    error "No git, curl, or wget found. Cannot download Silicon."
    exit 1
fi

snapshot_upstream_source "$INSTALL_DIR" "$INSTALL_DIR"

# ── pip packages ──────────────────────────────────────────────

if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    info "Installing Python dependencies..."
    "$PYTHON_CMD" -m pip install -r "$INSTALL_DIR/requirements.txt" --quiet 2>/dev/null || \
        "$PYTHON_CMD" -m pip install -r "$INSTALL_DIR/requirements.txt" --quiet --user
    success "Python dependencies installed"
fi

# ═════════════════════════════════════════════════════════════
# STEP 4: Configure
# ═════════════════════════════════════════════════════════════

header "Step 4 · Configure"

ENV_FILE="$INSTALL_DIR/env.py"

# Check if already configured
ALREADY_CONFIGURED=false
if [ -f "$ENV_FILE" ]; then
    if grep -q 'TELEGRAM_BOT_TOKEN = ""' "$ENV_FILE" 2>/dev/null || grep -q "TELEGRAM_BOT_TOKEN = ''" "$ENV_FILE" 2>/dev/null; then
        ALREADY_CONFIGURED=false
    else
        token_val=$(grep 'TELEGRAM_BOT_TOKEN' "$ENV_FILE" 2>/dev/null | head -1)
        if [ -n "$token_val" ] && ! echo "$token_val" | grep -q '""'; then
            ALREADY_CONFIGURED=true
        fi
    fi
fi

if [ "$ALREADY_CONFIGURED" = "true" ]; then
    success "Already configured (env.py has tokens)"
    if confirm "Reconfigure?"; then
        ALREADY_CONFIGURED=false
    fi
fi

if [ "$ALREADY_CONFIGURED" = "false" ]; then
    echo ""
    info "You need a Telegram bot token to use Silicon."
    printf "${DIM}  1. Open Telegram and search for @BotFather${RESET}\n"
    printf "${DIM}  2. Send /newbot and follow the prompts${RESET}\n"
    printf "${DIM}  3. Copy the token BotFather gives you${RESET}\n"
    echo ""

    TELEGRAM_TOKEN=$(read_secret "Telegram bot token")
    if [ -z "$TELEGRAM_TOKEN" ]; then
        error "Telegram bot token is required."
        exit 1
    fi

    echo ""
    info "OpenAI API key (for incoming voice transcription via Whisper)."
    info "Press Enter to skip – incoming voice transcription will be disabled."
    OPENAI_KEY=$(read_secret "OpenAI API key (optional)")

    echo ""
    info "Gemini API key (for outgoing text-to-speech)."
    info "Press Enter to skip – outgoing voice messages will be disabled."
    GEMINI_KEY=$(read_secret "Gemini API key (optional)")

    cat > "$ENV_FILE" << ENVEOF
TELEGRAM_BOT_TOKEN = "$TELEGRAM_TOKEN"
OPENAI_API_KEY = "$OPENAI_KEY"
GEMINI_API_KEY = "$GEMINI_KEY"
BROWSER_PROFILE = "$INSTANCE_NAME"
ENVEOF

    success "Configuration saved to $ENV_FILE"
fi

"$PYTHON_CMD" - <<PY
import json
import pathlib

def provider_list(raw, default):
    try:
        value = json.loads(raw)
    except Exception:
        return default
    allowed = {"claude", "codex", "chatgpt"}
    if not isinstance(value, list):
        return default
    out = []
    for item in value:
        if isinstance(item, str) and item in allowed and item not in out:
            out.append("codex" if item == "chatgpt" else item)
    return out or default

silicon_path = pathlib.Path("$INSTALL_DIR") / "silicon.json"
if silicon_path.exists():
    try:
        silicon = json.loads(silicon_path.read_text())
    except json.JSONDecodeError:
        silicon = {}
else:
    silicon = {}

silicon.setdefault("name", "Silicon")
silicon.setdefault("run", "python main.py")
silicon.setdefault("workers", {})
silicon["brain"] = "$BRAIN_CHOICE"
silicon["workers"] = {
    "browser": provider_list("""$BROWSER_WORKERS""", ["claude"]),
    "terminal": provider_list("""$TERMINAL_WORKERS""", ["claude"]),
    "writer": provider_list("""$WRITER_WORKERS""", ["claude"]),
}
silicon_path.write_text(json.dumps(silicon, indent=4) + "\\n")
PY

# ═════════════════════════════════════════════════════════════
# STEP 5: Silicon registry
# ═════════════════════════════════════════════════════════════

header "Step 5 · Registry"

mkdir -p "$REGISTRY_DIR"
mkdir -p "$BIN_DIR"

# Initialize registry if it doesn't exist
if [ ! -f "$REGISTRY_FILE" ]; then
    echo '{"installations": []}' > "$REGISTRY_FILE"
    success "Created registry at $REGISTRY_FILE"
fi

# Check if this installation is already registered
ABS_INSTALL_DIR=$(cd "$INSTALL_DIR" 2>/dev/null && pwd || echo "$INSTALL_DIR")
ALREADY_REGISTERED=false

if command -v "$PYTHON_CMD" &>/dev/null; then
    ALREADY_REGISTERED=$("$PYTHON_CMD" -c "
import json, sys
with open('$REGISTRY_FILE') as f:
    reg = json.load(f)
for inst in reg.get('installations', []):
    if inst.get('path') == '$ABS_INSTALL_DIR' or inst.get('name') == '$INSTANCE_NAME':
        print('true')
        sys.exit(0)
print('false')
" 2>/dev/null || echo "false")
fi

if [ "$ALREADY_REGISTERED" = "true" ]; then
    success "Instance '$INSTANCE_NAME' already registered"
else
    # Add to registry
    "$PYTHON_CMD" -c "
import json, os
from datetime import datetime
reg_path = '$REGISTRY_FILE'
with open(reg_path) as f:
    reg = json.load(f)
reg['installations'].append({
    'name': '$INSTANCE_NAME',
    'path': '$ABS_INSTALL_DIR',
    'created_at': datetime.now().isoformat(),
    'pid_file': os.path.join('$ABS_INSTALL_DIR', '.silicon.pid')
})
with open(reg_path, 'w') as f:
    json.dump(reg, f, indent=2)
"
    success "Registered '$INSTANCE_NAME' at $ABS_INSTALL_DIR"
fi

fi  # end SKIP_SILICON check

# ═════════════════════════════════════════════════════════════
# STEP 6: Create CLI
# ═════════════════════════════════════════════════════════════

header "Step 6 · CLI Setup"

# Ensure directories exist (needed even for CLI-only install)
mkdir -p "$REGISTRY_DIR"
mkdir -p "$BIN_DIR"
if [ ! -f "$REGISTRY_FILE" ]; then
    echo '{"installations": []}' > "$REGISTRY_FILE"
fi

cat > "$CLI_SCRIPT" << 'CLIEOF'
#!/usr/bin/env bash
# Silicon CLI – manages silicon installations

REGISTRY_DIR="$HOME/.silicon"
REGISTRY_FILE="$REGISTRY_DIR/registry.json"
GLASS_SERVER_URL="${GLASS_SERVER_URL:-https://glass.unlikefraction.com}"
REPO_URL="https://github.com/unlikefraction/silicon-stemcell.git"
REPO_ZIP="https://github.com/unlikefraction/silicon-stemcell/archive/refs/heads/main.zip"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'
DIM='\033[2m'; RESET='\033[0m'

error() { printf "${RED}✗${RESET} %s\n" "$*"; }
info()  { printf "${BLUE}→${RESET} %s\n" "$*"; }
success() { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}⚠${RESET} %s\n" "$*"; }

confirm() {
    printf "${BOLD}? %s [Y/n]${RESET} " "$1"
    local ans
    read -r ans
    case "$ans" in
        [nN]*) return 1 ;;
        *) return 0 ;;
    esac
}

choose_worker_provider_order() {
    local worker_type="$1"
    local default_choice="${2:-claude}"
    local choice
    local result

    printf "${BOLD}? Which provider should %s workers use – claude or codex?${RESET} [%s]: " "$worker_type" "$default_choice" >&2
    read -r choice
    choice="${choice:-$default_choice}"
    case "$choice" in
        codex)
            if confirm "Keep claude as fallback for $worker_type workers?"; then
                result='["codex", "claude"]'
            else
                result='["codex"]'
            fi
            ;;
        *)
            if confirm "Keep codex as fallback for $worker_type workers?"; then
                result='["claude", "codex"]'
            else
                result='["claude"]'
            fi
            ;;
    esac
    echo "$result"
}

read_secret() {
    local prompt="$1"
    printf "${BOLD}? %s:${RESET} " "$prompt" >&2
    local val=""
    local char=""
    while IFS= read -r -s -n1 char; do
        if [[ -z "$char" ]]; then
            break
        elif [[ "$char" == $'\x7f' ]] || [[ "$char" == $'\b' ]]; then
            if [[ -n "$val" ]]; then
                val="${val%?}"
                printf "\b \b" >&2
            fi
        else
            val="${val}${char}"
            printf "*" >&2
        fi
    done
    printf "\n" >&2
    echo "$val"
}

install_glass_cli() {
    local glass_repo="unlikefraction/glass"
    local glass_archive="https://codeload.github.com/${glass_repo}/tar.gz/refs/heads/main"
    local glass_dir="$HOME/.glass"
    local glass_bin_dir="$HOME/.local/bin"
    local glass_wrapper="$glass_bin_dir/glass"
    if command -v glass &>/dev/null; then
        return 0
    fi

    warn "glass CLI not found"
    mkdir -p "$glass_dir" "$glass_bin_dir"
    local tmp_dir
    tmp_dir=$(mktemp -d /tmp/glass-install-XXXXXX)
    trap 'rm -rf "$tmp_dir"' RETURN

    info "Installing glass CLI..."
    if command -v curl &>/dev/null; then
        curl -fsSL "$glass_archive" | tar -xzf - -C "$tmp_dir"
    elif command -v wget &>/dev/null; then
        wget -qO- "$glass_archive" | tar -xzf - -C "$tmp_dir"
    else
        warn "Could not auto-install glass CLI. Need curl or wget."
        return 1
    fi

    local src_dir
    src_dir=$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d -name 'glass-*' | head -1)
    if [ -z "$src_dir" ] || [ ! -f "$src_dir/glass" ] || [ ! -f "$src_dir/glass_cli.py" ]; then
        warn "Could not auto-install glass CLI. Downloaded archive was invalid."
        return 1
    fi

    rm -rf "$glass_dir"
    mkdir -p "$glass_dir"
    (
        cd "$src_dir"
        tar --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' -cf - .
    ) | (
        cd "$glass_dir"
        tar -xf -
    )

    chmod +x "$glass_dir/glass" "$glass_dir/install.sh"
    ln -sf "$glass_dir/glass" "$glass_wrapper"

    if command -v glass &>/dev/null; then
        success "glass CLI installed"
        return 0
    fi

    warn "glass CLI install finished, but it's not on PATH yet."
    warn "Add ~/.local/bin to PATH."
    return 1
}

# Find python
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    error "Python not found"
    exit 1
fi

# ── Registry helpers ──────────────────────────────────────────

get_installations() {
    "$PYTHON_CMD" - <<PY 2>/dev/null
import json
with open("$REGISTRY_FILE") as f:
    reg = json.load(f)
for i, inst in enumerate(reg.get("installations", [])):
    print(f"{i}|{inst['name']}|{inst['path']}|{inst.get('pid_file', '')}")
PY
}

get_count() {
    "$PYTHON_CMD" - <<PY 2>/dev/null
import json
with open("$REGISTRY_FILE") as f:
    reg = json.load(f)
print(len(reg.get("installations", [])))
PY
}

python_capture_to() {
    local __var_name="$1"
    local tmp_script
    local tmp_output
    local status
    local value
    tmp_script=$(mktemp /tmp/silicon-python-XXXXXX.py)
    tmp_output=$(mktemp /tmp/silicon-python-out-XXXXXX)
    cat > "$tmp_script"
    "$PYTHON_CMD" "$tmp_script" > "$tmp_output"
    status=$?
    value=$(cat "$tmp_output")
    rm -f "$tmp_script" "$tmp_output"
    printf -v "$__var_name" '%s' "$value"
    return "$status"
}

is_running() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "running"
            return 0
        fi
    fi
    echo "stopped"
    return 1
}

get_pid() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        cat "$pid_file" 2>/dev/null
    fi
}

ensure_glass_cli() {
    if command -v glass &>/dev/null; then
        return 0
    fi

    info "glass CLI not found. Installing it..."
    install_glass_cli || true

    command -v glass >/dev/null 2>&1 || {
        error "glass CLI installation failed"
        exit 1
    }
}

register_installation() {
    local instance_name="$1"
    local target_dir="$2"
    local pid_file="$target_dir/.silicon.pid"

    "$PYTHON_CMD" - <<PY
import json, os
reg_file = os.path.expanduser("$REGISTRY_FILE")
instance_name = "$instance_name"
target_dir = "$target_dir"
pid_file = "$pid_file"

if os.path.exists(reg_file):
    with open(reg_file) as f:
        reg = json.load(f)
else:
    reg = {"installations": []}

for inst in reg.get("installations", []):
    if inst.get("path") == target_dir or inst.get("name") == instance_name:
        print("exists")
        raise SystemExit(0)

reg.setdefault("installations", []).append({
    "name": instance_name,
    "path": target_dir,
    "pid_file": pid_file,
})

with open(reg_file, "w") as f:
    json.dump(reg, f, indent=2)

print("added")
PY
}

snapshot_upstream_source() {
    local source_dir="$1"
    local target_dir="$2"
    local updater="$source_dir/scripts/silicon_update.py"

    if [ -f "$updater" ]; then
        "$PYTHON_CMD" "$updater" snapshot --source "$source_dir" --target "$target_dir" >/dev/null 2>&1 || true
    fi
}

download_stemcell_source() {
    local target_dir="$1"
    rm -rf "$target_dir"
    mkdir -p "$target_dir"

    if command -v git &>/dev/null; then
        git clone --depth 1 "$REPO_URL" "$target_dir" >/dev/null 2>&1
        rm -rf "$target_dir/.git"
        return 0
    fi

    if command -v curl &>/dev/null; then
        local tmp_zip
        tmp_zip=$(mktemp /tmp/silicon-XXXXXX.zip)
        curl -fsSL "$REPO_ZIP" -o "$tmp_zip"
        unzip -q "$tmp_zip" -d "$target_dir"
        rm -f "$tmp_zip"
    elif command -v wget &>/dev/null; then
        local tmp_zip
        tmp_zip=$(mktemp /tmp/silicon-XXXXXX.zip)
        wget -q "$REPO_ZIP" -O "$tmp_zip"
        unzip -q "$tmp_zip" -d "$target_dir"
        rm -f "$tmp_zip"
    else
        error "Need git, curl, or wget to download Silicon."
        exit 1
    fi

    local extracted
    extracted=$(find "$target_dir" -mindepth 1 -maxdepth 1 -type d -name 'silicon-*' | head -1)
    if [ -n "$extracted" ]; then
        local final_dir="${target_dir}.tmp-root"
        mv "$extracted" "$final_dir"
        rm -rf "$target_dir"
        mv "$final_dir" "$target_dir"
    fi
}

hydrate_silicon_dir() {
    local target_dir="$1"
    local abs_target
    abs_target=$(cd "$target_dir" 2>/dev/null && pwd || echo "$target_dir")
    local tmp_src
    tmp_src=$(mktemp -d /tmp/silicon-src-XXXXXX)
    trap 'rm -rf "$tmp_src"' RETURN

    if [ ! -d "$abs_target" ]; then
        error "Directory not found: $abs_target"
        exit 1
    fi

    info "Downloading Silicon stemcell..."
    download_stemcell_source "$tmp_src"

    local instance_name
    python_capture_to instance_name <<PY
import json, pathlib
target = pathlib.Path("$abs_target")
silicon_path = target / "silicon.json"
name = ""
if silicon_path.exists():
    try:
        data = json.loads(silicon_path.read_text())
        name = (data.get("address") or data.get("name") or "").strip()
    except Exception:
        pass
if not name:
    name = target.name
print(name)
PY

    info "Hydrating $abs_target..."
    "$PYTHON_CMD" - <<PY
import json
import pathlib
import shutil

src = pathlib.Path("$tmp_src")
dst = pathlib.Path("$abs_target")

skip_names = {".git", "__pycache__", ".DS_Store"}
preserve_root_files = {"env.py", "silicon.json", ".glass.json"}

for path in src.rglob("*"):
    rel = path.relative_to(src)
    if any(part in skip_names for part in rel.parts):
        continue
    target = dst / rel
    if path.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        continue
    if rel.parts and rel.parts[0] in preserve_root_files and len(rel.parts) == 1 and target.exists():
        continue
    if target.exists():
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)

silicon_path = dst / "silicon.json"
if silicon_path.exists():
    try:
        silicon = json.loads(silicon_path.read_text())
    except json.JSONDecodeError:
        silicon = {}
else:
    silicon = {}

silicon.setdefault("name", "Silicon")
silicon.setdefault("run", "python main.py")
silicon.setdefault("brain", "claude")
silicon.setdefault("workers", {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]})
silicon.setdefault("address", "$instance_name")
silicon.pop("version", None)
silicon_path.write_text(json.dumps(silicon, indent=4) + "\\n")

env_path = dst / "env.py"
lines = env_path.read_text().splitlines() if env_path.exists() else []
required = {
    "TELEGRAM_BOT_TOKEN": "",
    "OPENAI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "BROWSER_PROFILE": "$instance_name",
}
seen = set()
out = []
for line in lines:
    matched = False
    for key in required:
        if line.startswith(f"{key} ="):
            out.append(line)
            seen.add(key)
            matched = True
            break
    if not matched:
        out.append(line)
for key, value in required.items():
    if key not in seen:
        out.append(f'{key} = "{value}"')
env_path.write_text("\\n".join(out).rstrip() + "\\n")
PY

    snapshot_upstream_source "$tmp_src" "$abs_target"

    local current_telegram=""
    local current_openai=""
    local current_gemini=""
    if [ -f "$abs_target/env.py" ]; then
        python_capture_to current_telegram <<PY
import pathlib, re
text = pathlib.Path("$abs_target/env.py").read_text()
m = re.search(r'^TELEGRAM_BOT_TOKEN\\s*=\\s*["\\'](.*)["\\']\\s*$', text, re.M)
print(m.group(1) if m else "")
PY
        python_capture_to current_openai <<PY
import pathlib, re
text = pathlib.Path("$abs_target/env.py").read_text()
m = re.search(r'^OPENAI_API_KEY\\s*=\\s*["\\'](.*)["\\']\\s*$', text, re.M)
print(m.group(1) if m else "")
PY
        python_capture_to current_gemini <<PY
import pathlib, re
text = pathlib.Path("$abs_target/env.py").read_text()
m = re.search(r'^GEMINI_API_KEY\\s*=\\s*["\\'](.*)["\\']\\s*$', text, re.M)
print(m.group(1) if m else "")
PY
    fi

    if [ -t 0 ] && [ -t 1 ]; then
        if [ -z "$current_telegram" ]; then
            echo ""
            info "You need a Telegram bot token to use Silicon."
            printf "${DIM}  1. Open Telegram and search for @BotFather${RESET}\n"
            printf "${DIM}  2. Send /newbot and follow the prompts${RESET}\n"
            printf "${DIM}  3. Copy the token BotFather gives you${RESET}\n"
            echo ""
            current_telegram=$(read_secret "Telegram bot token")
            if [ -z "$current_telegram" ]; then
                error "Telegram bot token is required."
                exit 1
            fi
        fi

        if [ -z "$current_openai" ]; then
            echo ""
            info "OpenAI API key (for incoming voice transcription via Whisper)."
            info "Press Enter to skip – incoming voice transcription will be disabled."
            current_openai=$(read_secret "OpenAI API key (optional)")
        fi

        if [ -z "$current_gemini" ]; then
            echo ""
            info "Gemini API key (for outgoing text-to-speech)."
            info "Press Enter to skip – outgoing voice messages will be disabled."
            current_gemini=$(read_secret "Gemini API key (optional)")
        fi

        # ── Terminal worker preference (codex detection) ──
        local brain_choice="claude"
        local browser_workers='["claude"]'
        local terminal_workers='["claude"]'
        local writer_workers='["claude"]'
        if command -v claude &>/dev/null && command -v codex &>/dev/null; then
            echo ""
            info "Detected both claude and codex."
            printf "${BOLD}? Which brain should Silicon use – claude or codex?${RESET} [claude]: "
            read -r brain_choice
            brain_choice="${brain_choice:-claude}"
            case "$brain_choice" in
                codex) brain_choice="codex" ;;
                *) brain_choice="claude" ;;
            esac
            browser_workers=$(choose_worker_provider_order "browser" "claude")
            terminal_workers=$(choose_worker_provider_order "terminal" "claude")
            writer_workers=$(choose_worker_provider_order "writer" "claude")
        elif command -v codex &>/dev/null; then
            brain_choice="codex"
            browser_workers='["codex"]'
            terminal_workers='["codex"]'
            writer_workers='["codex"]'
        fi

        "$PYTHON_CMD" - <<PY
import pathlib, re
env_path = pathlib.Path("$abs_target/env.py")
text = env_path.read_text()

def upsert(text, key, value):
    pattern = rf'^{key}\s*=\s*["\x27].*["\x27]\s*$'
    replacement = f'{key} = "{value}"'
    if re.search(pattern, text, re.M):
        return re.sub(pattern, replacement, text, flags=re.M)
    text = text.rstrip() + "\\n"
    return text + replacement + "\\n"

text = upsert(text, "TELEGRAM_BOT_TOKEN", """$current_telegram""")
text = upsert(text, "OPENAI_API_KEY", """$current_openai""")
text = upsert(text, "GEMINI_API_KEY", """$current_gemini""")
env_path.write_text(text.rstrip() + "\\n")
PY

        # Update silicon.json with terminal worker preference
        "$PYTHON_CMD" - <<PY
import json, pathlib

def provider_list(raw, default):
    try:
        value = json.loads(raw)
    except Exception:
        return default
    allowed = {"claude", "codex", "chatgpt"}
    if not isinstance(value, list):
        return default
    out = []
    for item in value:
        if isinstance(item, str) and item in allowed and item not in out:
            out.append("codex" if item == "chatgpt" else item)
    return out or default

silicon_path = pathlib.Path("$abs_target/silicon.json")
if silicon_path.exists():
    try:
        silicon = json.loads(silicon_path.read_text())
    except json.JSONDecodeError:
        silicon = {}
    silicon["brain"] = "$brain_choice"
    silicon["workers"] = {
        "browser": provider_list("""$browser_workers""", ["claude"]),
        "terminal": provider_list("""$terminal_workers""", ["claude"]),
        "writer": provider_list("""$writer_workers""", ["claude"]),
    }
    silicon_path.write_text(json.dumps(silicon, indent=4) + "\\n")
PY
    fi

    if [ -f "$abs_target/requirements.txt" ]; then
        info "Installing Python dependencies..."
        "$PYTHON_CMD" -m pip install -r "$abs_target/requirements.txt" --quiet 2>/dev/null || \
            "$PYTHON_CMD" -m pip install -r "$abs_target/requirements.txt" --quiet --user
    fi

    mkdir -p "$REGISTRY_DIR"
    if [ ! -f "$REGISTRY_FILE" ]; then
        echo '{"installations": []}' > "$REGISTRY_FILE"
    fi
    register_installation "$instance_name" "$abs_target" >/dev/null
    success "Hydrated '$instance_name' at $abs_target"
    info "Run 'silicon start $instance_name' when you're ready."
}

# Find installation by current directory or name
find_installation() {
    local search="${1:-}"
    local cwd
    cwd=$(pwd)

    while IFS='|' read -r idx name path pid_file; do
        if [ -n "$search" ]; then
            if [ "$name" = "$search" ]; then
                echo "$idx|$name|$path|$pid_file"
                return 0
            fi
        else
            # Check if cwd is inside this installation
            case "$cwd" in
                "$path"*) echo "$idx|$name|$path|$pid_file"; return 0 ;;
            esac
        fi
    done <<< "$(get_installations)"
    return 1
}

# Multi-target selector: "all", a number (1), or a comma list (1,2,4)
_is_multi_target() {
    local s="$1"
    [ "$s" = "all" ] && return 0
    [[ "$s" =~ ^[0-9]+(,[0-9]+)*$ ]] && return 0
    return 1
}

# Resolve a multi-target selector to a newline-separated list of installation names.
_resolve_targets() {
    local selector="$1"
    if [ "$selector" = "all" ]; then
        get_installations | awk -F'|' '{print $2}'
        return 0
    fi
    # Comma-separated 1-based indices
    local IFS=','
    for num in $selector; do
        local want=$((num - 1))
        get_installations | awk -F'|' -v w="$want" '$1 == w { print $2 }'
    done
}

# Ask to confirm a multi-target action. $1=verb, $2=newline list of names.
_confirm_multi() {
    local verb="$1"
    local names="$2"
    local joined
    joined=$(echo "$names" | paste -sd "," - | sed 's/,/, /g')
    confirm "Are you sure you want to $verb the following silicons: $joined?"
}

# Pick an installation (interactive)
pick_installation() {
    local count
    count=$(get_count)
    if [ -z "$count" ] || [ "$count" -eq 0 ]; then
        error "No silicon installations found. Run 'silicon install' first." >&2
        exit 1
    elif [ "$count" -eq 1 ]; then
        get_installations | head -1
        return 0
    fi

    printf "\n${BOLD}Select a silicon instance:${RESET}\n\n" >&2
    while IFS='|' read -r idx name path pid_file; do
        local status
        status=$(is_running "$pid_file")
        local status_color
        if [ "$status" = "running" ]; then
            status_color="${GREEN}● running${RESET}"
        else
            status_color="${DIM}○ stopped${RESET}"
        fi
        printf "  ${BOLD}%d)${RESET} %-20s %b  ${DIM}%s${RESET}\n" "$((idx + 1))" "$name" "$status_color" "$path" >&2
    done <<< "$(get_installations)"

    echo "" >&2
    printf "${BOLD}? Choice [1]:${RESET} " >&2
    read -r choice
    choice="${choice:-1}"
    local target_idx=$((choice - 1))

    while IFS='|' read -r idx name path pid_file; do
        if [ "$idx" -eq "$target_idx" ]; then
            echo "$idx|$name|$path|$pid_file"
            return 0
        fi
    done <<< "$(get_installations)"

    error "Invalid choice"
    exit 1
}

# ── Commands ──────────────────────────────────────────────────

cmd_list() {
    local count
    count=$(get_count)
    if [ "$count" -eq 0 ]; then
        info "No silicon installations found."
        info "Run 'silicon install' to set up a new instance."
        return
    fi

    printf "\n${BOLD}${CYAN}Silicon Installations${RESET}\n\n"
    printf "  ${DIM}%-4s %-20s %-10s %s${RESET}\n" "#" "NAME" "STATUS" "PATH"
    printf "  ${DIM}%-4s %-20s %-10s %s${RESET}\n" "---" "----" "------" "----"

    while IFS='|' read -r idx name path pid_file; do
        local status status_display pid_info
        status=$(is_running "$pid_file")
        if [ "$status" = "running" ]; then
            local pid
            pid=$(get_pid "$pid_file")
            status_display="${GREEN}● running${RESET}"
            pid_info=" ${DIM}(PID $pid)${RESET}"
        else
            status_display="${DIM}○ stopped${RESET}"
            pid_info=""
        fi
        printf "  %-4s %-20s %b%b  ${DIM}%s${RESET}\n" "$((idx + 1))" "$name" "$status_display" "$pid_info" "$path"
    done <<< "$(get_installations)"
    echo ""
}

_kill_floaters() {
    local target_path="$1"
    local skip_pid="${2:-}"
    local main_py="$target_path/main.py"

    # Find python processes running this specific main.py (absolute path in command)
    local pids
    pids=$(ps -eo pid,command 2>/dev/null | grep "[p]ython.*$main_py" | awk '{print $1}')

    for pid in $pids; do
        [ -z "$pid" ] && continue
        [ "$pid" = "$skip_pid" ] && continue
        warn "Killing orphaned process (PID $pid) from $target_path"
        kill "$pid" 2>/dev/null
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null
        fi
    done
}

_silicon_watchdog_loop() {
    local name="$1"
    local path="$2"
    local pid_file="$3"
    local log_file="$path/.silicon.log"
    local main_py="$path/main.py"
    local restart_delay=5
    local max_rapid=5
    local rapid_window=60
    local child_pid=""

    # On SIGTERM/SIGINT: kill python child, clean up, exit
    trap '
        if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
            kill "$child_pid" 2>/dev/null
            for _i in $(seq 1 6); do
                kill -0 "$child_pid" 2>/dev/null || break
                sleep 0.5
            done
            kill -0 "$child_pid" 2>/dev/null && kill -9 "$child_pid" 2>/dev/null
        fi
        rm -f "$pid_file"
        exit 0
    ' TERM INT

    # Track restart timestamps for crash loop detection
    local restart_times=""

    while true; do
        # Kill any floating processes from this directory
        _kill_floaters "$path" ""

        # Run python as a background child so traps fire immediately
        "$PYTHON_CMD" -u "$main_py" >> "$log_file" 2>&1 &
        child_pid=$!
        wait "$child_pid" 2>/dev/null
        local exit_code=$?
        child_pid=""

        # Check if we were told to stop
        if [ -f "$path/.silicon.stop" ]; then
            rm -f "$path/.silicon.stop"
            rm -f "$pid_file"
            break
        fi

        # Crash loop detection
        local now
        now=$(date +%s)
        restart_times="$restart_times $now"

        # Count recent restarts within the rapid window
        local cutoff=$((now - rapid_window))
        local recent=0
        local new_times=""
        for t in $restart_times; do
            if [ "$t" -ge "$cutoff" ]; then
                recent=$((recent + 1))
                new_times="$new_times $t"
            fi
        done
        restart_times="$new_times"

        if [ "$recent" -ge "$max_rapid" ]; then
            echo "[silicon-watchdog] $(date): '$name' crashed $max_rapid times in ${rapid_window}s. Giving up." >> "$log_file"
            rm -f "$pid_file"
            break
        fi

        echo "[silicon-watchdog] $(date): '$name' exited (code $exit_code). Restarting in ${restart_delay}s..." >> "$log_file"
        sleep "$restart_delay"
    done
}

_start_glass_agent() {
    local path="$1"
    local agent_pid_file="$path/.glass_agent.pid"
    local agent_log="$path/.glass_agent.log"

    # Only start if .glass.json exists
    [ -f "$path/.glass.json" ] || return 0

    # Check if already running
    if [ -f "$agent_pid_file" ]; then
        local old_pid
        old_pid=$(cat "$agent_pid_file" 2>/dev/null)
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
            return 0
        fi
    fi

    # Start glass agent
    "$PYTHON_CMD" -u "$path/glass_agent.py" >> "$agent_log" 2>&1 &
    local agent_pid=$!
    disown "$agent_pid" 2>/dev/null
    echo "$agent_pid" > "$agent_pid_file"
    info "Glass agent started (PID $agent_pid)"
}

_stop_glass_agent() {
    local path="$1"
    local agent_pid_file="$path/.glass_agent.pid"

    if [ -f "$agent_pid_file" ]; then
        local pid
        pid=$(cat "$agent_pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            info "Glass agent stopped"
        fi
        rm -f "$agent_pid_file"
    fi
}

_glass_agent_status() {
    local path="$1"
    local agent_pid_file="$path/.glass_agent.pid"

    if [ -f "$agent_pid_file" ]; then
        local pid
        pid=$(cat "$agent_pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "running"
            return 0
        fi
    fi
    echo "stopped"
    return 1
}

cmd_start() {
    local target="$1"

    # Multi-target dispatch
    if [ -n "$target" ] && _is_multi_target "$target"; then
        local names
        names=$(_resolve_targets "$target")
        [ -z "$names" ] && { error "No matching installations"; exit 1; }
        if [ "$target" = "all" ]; then
            _confirm_multi "start" "$names" || return
        fi
        while IFS= read -r n; do
            [ -z "$n" ] && continue
            _start_one "$n"
        done <<< "$names"
        return
    fi

    _start_one "$target"
}

_start_one() {
    local target="$1"
    local inst

    if [ -n "$target" ]; then
        inst=$(find_installation "$target") || { error "Silicon '$target' not found"; exit 1; }
    else
        inst=$(find_installation) || inst=$(pick_installation)
    fi

    IFS='|' read -r idx name path pid_file <<< "$inst"

    if [ "$(is_running "$pid_file")" = "running" ]; then
        local pid
        pid=$(get_pid "$pid_file")
        warn "'$name' is already running (PID $pid)"
        # Still ensure agent is running
        _start_glass_agent "$path"
        return
    fi

    # Clean up any orphaned processes from this directory
    _kill_floaters "$path" ""
    rm -f "$pid_file" "$path/.silicon.stop"

    info "Starting '$name' (with auto-restart)..."

    # Launch the watchdog wrapper in background
    _silicon_watchdog_loop "$name" "$path" "$pid_file" &
    local wrapper_pid=$!
    disown "$wrapper_pid" 2>/dev/null
    echo "$wrapper_pid" > "$pid_file"

    # Brief wait to check it didn't crash immediately
    sleep 2
    if kill -0 "$wrapper_pid" 2>/dev/null; then
        success "'$name' started (PID $wrapper_pid)"
        info "Auto-restart enabled. Logs: $path/.silicon.log"
    else
        error "'$name' failed to start. Check logs: $path/.silicon.log"
        rm -f "$pid_file"
    fi

    # Start glass agent for remote control
    _start_glass_agent "$path"
}

cmd_stop() {
    local full_stop=false
    local target="$1"

    # Check for --full flag
    if [ "$target" = "--full" ]; then
        full_stop=true
        target="$2"
    elif [ "$2" = "--full" ] || [ "${ARG3:-}" = "--full" ]; then
        full_stop=true
    fi

    # Multi-target dispatch
    if [ -n "$target" ] && _is_multi_target "$target"; then
        local names
        names=$(_resolve_targets "$target")
        [ -z "$names" ] && { error "No matching installations"; exit 1; }
        if [ "$target" = "all" ]; then
            _confirm_multi "stop" "$names" || return
        fi
        while IFS= read -r n; do
            [ -z "$n" ] && continue
            _stop_one "$n" "$full_stop"
        done <<< "$names"
        return
    fi

    _stop_one "$target" "$full_stop"
}

_stop_one() {
    local target="$1"
    local full_stop="$2"

    local inst
    if [ -n "$target" ]; then
        inst=$(find_installation "$target") || { error "Silicon '$target' not found"; exit 1; }
    else
        inst=$(find_installation) || inst=$(pick_installation)
    fi

    IFS='|' read -r idx name path pid_file <<< "$inst"

    if [ "$(is_running "$pid_file")" != "running" ]; then
        warn "'$name' is not running"
        # Still clean up any floaters
        _kill_floaters "$path" ""
        rm -f "$pid_file" "$path/.silicon.stop"
        if [ "$full_stop" = true ]; then
            _stop_glass_agent "$path"
        fi
        return
    fi

    local pid
    pid=$(get_pid "$pid_file")

    # Signal wrapper to not restart after python exits
    touch "$path/.silicon.stop"

    info "Stopping '$name' (PID $pid)..."
    kill "$pid" 2>/dev/null

    # Wait for graceful shutdown
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done

    # Force kill if still running
    if kill -0 "$pid" 2>/dev/null; then
        warn "Force stopping..."
        kill -9 "$pid" 2>/dev/null
    fi

    # Belt-and-suspenders: kill any remaining floaters
    _kill_floaters "$path" ""

    rm -f "$pid_file" "$path/.silicon.stop"
    success "'$name' stopped"

    if [ "$full_stop" = true ]; then
        _stop_glass_agent "$path"
    else
        info "Glass agent still running (use --full to stop it too)."
    fi
}

cmd_restart() {
    local target="$1"
    cmd_stop "$target"
    sleep 1
    cmd_start "$target"
}

cmd_agent() {
    local subcmd="$1"
    local target="$2"
    local inst

    if [ -z "$subcmd" ]; then
        error "Usage: silicon agent <start|stop|status> [name]"
        exit 1
    fi

    if [ -n "$target" ]; then
        inst=$(find_installation "$target") || { error "Silicon '$target' not found"; exit 1; }
    else
        inst=$(find_installation) || inst=$(pick_installation)
    fi

    IFS='|' read -r idx name path pid_file <<< "$inst"

    case "$subcmd" in
        start)
            _start_glass_agent "$path"
            ;;
        stop)
            _stop_glass_agent "$path"
            ;;
        status)
            local status
            status=$(_glass_agent_status "$path")
            if [ "$status" = "running" ]; then
                local pid
                pid=$(cat "$path/.glass_agent.pid" 2>/dev/null)
                printf "${GREEN}●${RESET} Glass agent running (PID %s)\n" "$pid"
            else
                printf "${DIM}○${RESET} Glass agent stopped\n"
            fi
            ;;
        *)
            error "Unknown agent command: $subcmd. Use start, stop, or status."
            exit 1
            ;;
    esac
}

cmd_status() {
    local target="$1"

    if [ -n "$target" ]; then
        local inst
        inst=$(find_installation "$target") || { cmd_list; return; }
        IFS='|' read -r idx name path pid_file <<< "$inst"
        local status
        status=$(is_running "$pid_file")
        if [ "$status" = "running" ]; then
            local pid
            pid=$(get_pid "$pid_file")
            printf "\n${BOLD}$name${RESET} ${GREEN}● running${RESET} (PID $pid)\n"
            printf "${DIM}  Path: $path${RESET}\n\n"
        else
            printf "\n${BOLD}$name${RESET} ${DIM}○ stopped${RESET}\n"
            printf "${DIM}  Path: $path${RESET}\n\n"
        fi
    else
        local inst
        inst=$(find_installation 2>/dev/null) && {
            IFS='|' read -r idx name path pid_file <<< "$inst"
            local status
            status=$(is_running "$pid_file")
            if [ "$status" = "running" ]; then
                local pid
                pid=$(get_pid "$pid_file")
                printf "\n${BOLD}$name${RESET} ${GREEN}● running${RESET} (PID $pid)\n"
                printf "${DIM}  Path: $path${RESET}\n\n"
            else
                printf "\n${BOLD}$name${RESET} ${DIM}○ stopped${RESET}\n"
                printf "${DIM}  Path: $path${RESET}\n\n"
            fi
        } || cmd_list
    fi
}

cmd_browser() {
    local target="$1"
    local inst

    if [ -n "$target" ]; then
        inst=$(find_installation "$target") || { error "Silicon '$target' not found"; exit 1; }
    else
        inst=$(find_installation) || inst=$(pick_installation)
    fi

    IFS='|' read -r idx name path pid_file <<< "$inst"

    info "Opening browser for '$name'..."
    cd "$path"
    "$PYTHON_CMD" main.py browser
}

ensure_git() {
    if command -v git >/dev/null 2>&1; then
        return 0
    fi

    warn "git not found. It's needed for the best merge strategy during updates."
    printf "${BOLD}? Install git now? [Y/n]${RESET} "
    local ans
    read -r ans </dev/tty
    case "$ans" in
        [nN]*) return 1 ;;
    esac

    if [ "$(uname -s)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            brew install git
        else
            error "Homebrew not found. Install git manually and retry."
            return 1
        fi
    else
        if command -v apt-get >/dev/null 2>&1; then
            sudo apt-get update -qq && sudo apt-get install -y git
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y git
        elif command -v yum >/dev/null 2>&1; then
            sudo yum install -y git
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -S --noconfirm git
        else
            error "No supported package manager found to install git."
            return 1
        fi
    fi

    command -v git >/dev/null 2>&1
}

cmd_pull() {
    local username="$1"
    if [ -z "$username" ]; then
        error "Usage: silicon pull <silicon-username>"
        exit 1
    fi

    ensure_glass_cli

    local target_dir
    target_dir="$(pwd)/$username"
    if [ -e "$target_dir" ]; then
        error "Target folder already exists: $target_dir"
        exit 1
    fi

    if ! command -v curl >/dev/null 2>&1; then
        error "curl is required for silicon pull"
        exit 1
    fi

    local connector_code
    connector_code=$(read_secret "Connector code")

    mkdir -p "$target_dir"

    local folder_fingerprint
    python_capture_to folder_fingerprint <<PY
import hashlib, pathlib, socket
target = pathlib.Path("$target_dir").resolve()
print(hashlib.sha256(f"{socket.gethostname()}::{target}".encode()).hexdigest())
PY

    local claim_file
    claim_file="$(mktemp /tmp/silicon-pull-claim-XXXXXX.json)"
    local payload
    python_capture_to payload <<PY
import json
print(json.dumps({
    "username": "$username",
    "connector_code": "$connector_code",
    "folder_label": "$username",
    "folder_fingerprint": "$folder_fingerprint",
}))
PY

    local http_code
    http_code=$(curl -sS -o "$claim_file" -w "%{http_code}" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$GLASS_SERVER_URL/sync/api/pull/claim/")

    if [ "$http_code" -lt 200 ] || [ "$http_code" -ge 300 ]; then
        local err
        python_capture_to err <<PY
import json
try:
    data = json.load(open("$claim_file"))
    print(data.get("error", "Pull claim failed."))
except Exception:
    print("Pull claim failed.")
PY
        rm -rf "$target_dir" "$claim_file"
        error "$err"
        exit 1
    fi

    local archive_file
    archive_file="$(mktemp /tmp/silicon-pull-archive-XXXXXX.tar.gz)"

    local has_snapshot
    python_capture_to has_snapshot <<PY
import json
claim = json.load(open("$claim_file"))
print("yes" if claim.get("has_snapshot") else "no")
PY

    if [ "$has_snapshot" = "yes" ]; then
        local source_token
        python_capture_to source_token <<PY
import json
claim = json.load(open("$claim_file"))
print(claim["source_token"])
PY
        curl -sS \
            -H "X-Source-Token: $source_token" \
            "$GLASS_SERVER_URL/sync/api/silicons/$username/latest.tar.gz" \
            -o "$archive_file"
        tar -xzf "$archive_file" -C "$target_dir"
    fi

    "$PYTHON_CMD" - <<PY
import json, pathlib

target = pathlib.Path("$target_dir")
claim = json.load(open("$claim_file"))
cfg = {
    "server_url": "$GLASS_SERVER_URL",
    "silicon_username": "$username",
    "source_token": claim["source_token"],
    "api_key": claim["api_key"],
    "folder_fingerprint": "$folder_fingerprint",
    "last_tree_hash": claim.get("latest_tree_hash", ""),
}

(target / ".glass.json").write_text(json.dumps(cfg, indent=2) + "\\n")

silicon_path = target / "silicon.json"
if silicon_path.exists():
    try:
        silicon = json.loads(silicon_path.read_text())
    except json.JSONDecodeError:
        silicon = {}
else:
    silicon = {}

silicon.setdefault("name", "Silicon")
silicon.setdefault("run", "python main.py")
silicon.setdefault("brain", "claude")
silicon.setdefault("workers", {"browser": ["claude"], "terminal": ["claude"], "writer": ["claude"]})
silicon.pop("version", None)
silicon["address"] = "$username"
silicon["glass"] = {
    "server_url": "$GLASS_SERVER_URL",
    "silicon_username": "$username",
    "api_key": claim["api_key"],
    "source_token": claim["source_token"],
}
silicon_path.write_text(json.dumps(silicon, indent=4) + "\\n")

env_path = target / "env.py"
lines = []
if env_path.exists():
    lines = env_path.read_text().splitlines()
out = []
seen = False
for line in lines:
    if line.startswith("GLASS_API_KEY ="):
        out.append(f'GLASS_API_KEY = "{claim["api_key"]}"')
        seen = True
    else:
        out.append(line)
if not seen:
    out.append(f'GLASS_API_KEY = "{claim["api_key"]}"')
env_path.write_text("\\n".join(out).rstrip() + "\\n")
PY

    register_installation "$username" "$target_dir" >/dev/null

    rm -f "$claim_file" "$archive_file"
    success "Pulled '$username' into $target_dir"
    info "Registered as a silicon instance."

    # ── Detect empty repository ──
    # If the pulled folder only has bare-minimum files (silicon.json, env.py, .glass.json)
    # it's likely an empty silicon that needs to be populated
    local real_file_count
    real_file_count=$("$PYTHON_CMD" - <<PY
import pathlib
target = pathlib.Path("$target_dir")
bare_minimum = {".glass.json", "silicon.json", "env.py"}
count = 0
for f in target.iterdir():
    if f.name.startswith(".") and f.name != ".glass.json":
        continue
    if f.name == "__pycache__":
        continue
    if f.name not in bare_minimum:
        count += 1
print(count)
PY
)
    if [ "$real_file_count" -eq 0 ] && [ -t 0 ] && [ -t 1 ]; then
        echo ""
        warn "This looks like an empty repository (only silicon.json and env.py)."
        if confirm "Do you want to populate it with Silicon?"; then
            hydrate_silicon_dir "$target_dir"
        fi
    fi

    # ── Offer to enable backups ──
    if [ -t 0 ] && [ -t 1 ]; then
        echo ""
        if confirm "Do you want to enable backups for this silicon?"; then
            ensure_glass_cli
            info "Running initial backup..."
            if (cd "$target_dir" && glass push now); then
                success "Backup complete."
                info "Starting hourly backup loop in background..."
                local push_pid_file="$target_dir/.glass-push.pid"
                (cd "$target_dir" && nohup glass push > "$target_dir/.glass-push.log" 2>&1 & echo $! > "$push_pid_file")
                success "Hourly backups running (PID $(cat "$push_pid_file")). Logs: $target_dir/.glass-push.log"
                info "Use 'silicon push $username now' for a manual backup anytime."
            else
                warn "Initial backup failed. You can retry with: silicon push $username now"
            fi
        fi
    fi
}

cmd_update_script() {
    # Update the silicon CLI script (not the instances themselves)
    info "Updating silicon CLI..."
    local script_url="https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.sh"
    local tmp_script
    tmp_script=$(mktemp /tmp/silicon-update-XXXXXX.sh)

    if command -v curl &>/dev/null; then
        curl -fsSL "$script_url" -o "$tmp_script"
    elif command -v wget &>/dev/null; then
        wget -q "$script_url" -O "$tmp_script"
    else
        error "Need curl or wget to update"
        rm -f "$tmp_script"
        exit 1
    fi

    # Extract just the CLI script portion (between CLIEOF markers)
    local cli_path="$HOME/.silicon/bin/silicon"
    local cli_dir
    cli_dir=$(dirname "$cli_path")
    local new_cli
    new_cli=$(mktemp "$cli_dir/.silicon-cli-XXXXXX")

    sed -n "/^cat > .*CLI_SCRIPT.*<< 'CLIEOF'/,/^CLIEOF$/p" "$tmp_script" | sed '1d;$d' > "$new_cli"

    if [ -s "$new_cli" ]; then
        chmod +x "$new_cli"
        if ! bash -n "$new_cli"; then
            error "Downloaded CLI failed syntax validation. Keeping current CLI."
            rm -f "$tmp_script" "$new_cli"
            exit 1
        fi
        if [ -f "$cli_path" ] && cmp -s "$new_cli" "$cli_path"; then
            rm -f "$new_cli"
            success "CLI already up to date"
        else
            mv -f "$new_cli" "$cli_path"
            success "CLI updated to latest version"
        fi
    else
        error "Failed to extract CLI from installer. Try: silicon install"
    fi

    rm -f "$tmp_script" "$new_cli"
}

cmd_update_instance() {
    local target="$1"

    ensure_git || warn "Proceeding without git. Some non-conflicting merges may be skipped."

    local tmp_src
    tmp_src=$(mktemp -d /tmp/silicon-update-src-XXXXXX)
    trap 'rm -rf "$tmp_src"' RETURN

    info "Downloading latest Silicon source..."
    download_stemcell_source "$tmp_src"

    local updater="$tmp_src/scripts/silicon_update.py"
    if [ ! -f "$updater" ]; then
        error "Downloaded source did not include the updater script."
        exit 1
    fi

    # Multi-target dispatch
    if [ -n "$target" ] && _is_multi_target "$target"; then
        local names
        names=$(_resolve_targets "$target")
        [ -z "$names" ] && { error "No matching installations"; exit 1; }
        if [ "$target" = "all" ]; then
            _confirm_multi "update" "$names" || return
        fi
        while IFS= read -r n; do
            [ -z "$n" ] && continue
            _update_one "$n" "$tmp_src" "$updater" multi || true
        done <<< "$names"
        return
    fi

    _update_one "$target" "$tmp_src" "$updater" single
}

_update_one() {
    local target="$1"
    local tmp_src="$2"
    local updater="$3"
    local mode="${4:-single}"
    local inst

    if [ -n "$target" ]; then
        inst=$(find_installation "$target") || { error "Silicon '$target' not found"; [ "$mode" = "multi" ] && return 1 || exit 1; }
    else
        inst=$(find_installation) || inst=$(pick_installation)
    fi

    IFS='|' read -r idx name path pid_file <<< "$inst"

    if [ "$(is_running "$pid_file")" = "running" ]; then
        if [ "$mode" = "multi" ]; then
            warn "Skipping '$name' — it is running. Stop it first with: silicon stop $name"
            return 1
        fi
        error "'$name' is running. Stop it first with: silicon stop $name"
        exit 1
    fi

    info "Updating '$name' safely..."
    if "$PYTHON_CMD" "$updater" update --source "$tmp_src" --target "$path"; then
        success "'$name' updated successfully"
    else
        local status=$?
        if [ "$status" -eq 2 ]; then
            error "Update aborted because merge conflicts were detected in '$name'."
            info "No local files were overwritten."
        else
            error "Update failed for '$name'."
        fi
        [ "$mode" = "multi" ] && return "$status" || exit "$status"
    fi
}

cmd_debug() {
    local target="$1"
    local inst

    if [ -n "$target" ]; then
        inst=$(find_installation "$target") || { error "Silicon '$target' not found"; exit 1; }
    else
        inst=$(find_installation) || inst=$(pick_installation)
    fi

    IFS='|' read -r idx name path pid_file <<< "$inst"

    if [ "$(is_running "$pid_file")" != "running" ]; then
        error "'$name' is not running. Start it first with: silicon start $name"
        exit 1
    fi

    local log_file="$path/.silicon.log"
    if [ ! -f "$log_file" ]; then
        error "No log file found at $log_file"
        exit 1
    fi

    local pid
    pid=$(get_pid "$pid_file")
    printf "\n${BOLD}${CYAN}Debugging '$name'${RESET} (PID $pid)\n"
    printf "${DIM}  Log: $log_file${RESET}\n"
    printf "${DIM}  Press Ctrl+C to detach${RESET}\n\n"

    tail -f "$log_file"
}

cmd_push() {
    local target="$1"
    local subcmd="$2"
    local inst

    if [ -n "$target" ]; then
        inst=$(find_installation "$target") || { error "Silicon '$target' not found"; exit 1; }
    else
        inst=$(find_installation) || inst=$(pick_installation)
    fi

    IFS='|' read -r idx name path pid_file <<< "$inst"

    if [ ! -f "$path/.glass.json" ]; then
        error "'$name' is not connected to Glass. No .glass.json found."
        exit 1
    fi

    ensure_glass_cli

    case "${subcmd:-}" in
        now)
            info "Pushing '$name' to Glass..."
            (cd "$path" && glass push now) && success "Backup complete." || error "Push failed."
            ;;
        stop)
            local push_pid_file="$path/.glass-push.pid"
            if [ -f "$push_pid_file" ]; then
                local pid
                pid=$(cat "$push_pid_file" 2>/dev/null)
                if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                    kill "$pid" 2>/dev/null
                    rm -f "$push_pid_file"
                    success "Stopped backup loop for '$name'."
                    return
                fi
            fi
            warn "No backup loop running for '$name'."
            rm -f "$push_pid_file"
            ;;
        *)
            # Start the hourly loop in background
            local push_pid_file="$path/.glass-push.pid"
            if [ -f "$push_pid_file" ]; then
                local existing_pid
                existing_pid=$(cat "$push_pid_file" 2>/dev/null)
                if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
                    warn "Backup loop already running for '$name' (PID $existing_pid)"
                    return
                fi
            fi
            info "Starting hourly backup loop for '$name'..."
            (cd "$path" && glass push now) && success "Initial backup complete." || warn "Initial push failed, loop will retry in 1 hour."
            (cd "$path" && nohup glass push > "$path/.glass-push.log" 2>&1 & echo $! > "$push_pid_file")
            success "Hourly backups running (PID $(cat "$push_pid_file")). Logs: $path/.glass-push.log"
            ;;
    esac
}

cmd_attach() {
    local target_dir="${1:-$(pwd)}"

    # Resolve to absolute path
    target_dir=$(cd "$target_dir" 2>/dev/null && pwd || echo "$target_dir")

    # Check if it's a silicon directory
    if [ ! -f "$target_dir/main.py" ] || [ ! -f "$target_dir/config.py" ]; then
        error "This doesn't look like a silicon directory."
        info "Expected main.py and config.py in: $target_dir"
        info "Navigate to a silicon directory and try again, or pass the path:"
        info "  silicon attach /path/to/silicon"
        return 1
    fi

    if [ ! -d "$target_dir/prompts" ] || [ ! -d "$target_dir/core" ]; then
        error "Missing prompts/ or core/ directory. Not a valid silicon."
        return 1
    fi

    # Check if already registered
    while IFS='|' read -r idx name path pid_file; do
        if [ "$path" = "$target_dir" ]; then
            warn "This silicon is already registered as '$name'"
            return 0
        fi
    done <<< "$(get_installations)"

    success "Found a silicon at: $target_dir"

    # Ask for name
    printf "\n${DIM}  Give this instance a name so you can tell it apart from others.${RESET}\n"
    local dir_basename
    dir_basename=$(basename "$target_dir")
    printf "${BOLD}? Instance name [%s]:${RESET} " "$dir_basename"
    read -r instance_name
    instance_name="${instance_name:-$dir_basename}"

    # Check if name already taken
    while IFS='|' read -r idx name path pid_file; do
        if [ "$name" = "$instance_name" ]; then
            error "Name '$instance_name' is already taken. Pick a different one."
            return 1
        fi
    done <<< "$(get_installations)"

    # Detect if running
    local is_currently_running=false
    local detected_pid=""

    # Check for existing .silicon.pid
    if [ -f "$target_dir/.silicon.pid" ]; then
        local pid
        pid=$(cat "$target_dir/.silicon.pid" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            is_currently_running=true
            detected_pid="$pid"
        fi
    fi

    # Also try to find python main.py running from that dir
    if [ "$is_currently_running" = false ]; then
        detected_pid=$(ps aux | grep "[p]ython.*main.py" | grep "$target_dir" | awk '{print $2}' | head -1)
        if [ -n "$detected_pid" ] && kill -0 "$detected_pid" 2>/dev/null; then
            is_currently_running=true
        fi
    fi

    local pid_file="$target_dir/.silicon.pid"

    if [ "$is_currently_running" = true ]; then
        success "Detected running instance (PID $detected_pid)"
        # Write PID file so we can track it
        echo "$detected_pid" > "$pid_file"
    else
        info "Instance is not currently running."
    fi

    # Register it
    "$PYTHON_CMD" - <<PY
import json, os
reg_file = "$REGISTRY_FILE"
if os.path.exists(reg_file):
    with open(reg_file) as f:
        reg = json.load(f)
else:
    reg = {"installations": []}

reg["installations"].append({
    "name": "$instance_name",
    "path": "$target_dir",
    "pid_file": "$pid_file"
})

with open(reg_file, "w") as f:
    json.dump(reg, f, indent=2)
PY

    success "Attached '$instance_name' at $target_dir"

    if [ "$is_currently_running" = true ]; then
        printf "\n  ${BOLD}%s${RESET} ${GREEN}● running${RESET} (PID %s)\n\n" "$instance_name" "$detected_pid"
    else
        printf "\n  ${BOLD}%s${RESET} ${DIM}○ stopped${RESET}\n" "$instance_name"
        printf "  Start it with: ${BOLD}silicon start %s${RESET}\n\n" "$instance_name"
    fi
}

cmd_install() {
    # Re-run the installer
    local script_url="https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/install.sh"
    if command -v curl &>/dev/null; then
        curl -fsSL "$script_url" | bash
    elif command -v wget &>/dev/null; then
        wget -qO- "$script_url" | bash
    else
        error "Need curl or wget to re-run installer"
        exit 1
    fi
}

cmd_new() {
    local target="${1:-}"
    if [ -n "$target" ]; then
        hydrate_silicon_dir "$target"
    else
        cmd_install
    fi
}

cmd_help() {
    printf "\n${BOLD}${CYAN}silicon${RESET} – manage your silicon instances\n\n"
    printf "${BOLD}Usage:${RESET}\n"
    printf "  silicon                     Show status or list instances\n"
    printf "  silicon new                 Create a new Silicon (same as install)\n"
    printf "  silicon new .               Hydrate current folder into a runnable silicon\n"
    printf "  silicon start <target>      Start silicon(s). target = name, index, 1,2,4, or all\n"
    printf "  silicon stop <target>       Stop silicon(s) (agent stays alive)\n"
    printf "  silicon stop --full <target> Stop silicon(s) and glass agent\n"
    printf "  silicon restart <target>    Restart silicon(s)\n"
    printf "  silicon agent <start|stop|status> [name]  Manage glass agent\n"
    printf "  silicon status [name]       Show instance status\n"
    printf "  silicon browser [name]      Open headed browser for login\n"
    printf "  silicon debug [name]        Attach to running instance (live logs)\n"
    printf "  silicon attach [path]       Register an existing silicon instance\n"
    printf "  silicon pull <username>     Pull a silicon from Glass into a new folder\n"
    printf "  silicon push [name]         Start hourly backup loop to Glass\n"
    printf "  silicon push [name] now     Push a one-time backup to Glass\n"
    printf "  silicon push [name] stop    Stop the hourly backup loop\n"
    printf "  silicon update <target>     Update silicon(s) to latest. target = name, index, 1,2,4, or all\n"
    printf "  silicon list                List all instances\n"
    printf "  silicon script update       Update the silicon CLI script\n"
    printf "  silicon install             Install a new instance\n"
    printf "  silicon help                Show this help\n"
    echo ""
}

# ── Fuzzy command matching ───────────────────────────────────

suggest_command() {
    local input="$1"
    local commands="start stop restart status browser debug attach pull push update list install new help script agent"
    local best_match=""
    local best_score=999

    for cmd in $commands; do
        # Simple Levenshtein-like: count chars in common
        local score=0
        local i=0
        local input_len=${#input}
        local cmd_len=${#cmd}

        # Length difference penalty
        local len_diff=$((input_len - cmd_len))
        [ "$len_diff" -lt 0 ] && len_diff=$((-len_diff))

        # Check prefix match
        if [[ "$cmd" == "$input"* ]] || [[ "$input" == "$cmd"* ]]; then
            score=$len_diff
        else
            # Character overlap score
            local common=0
            for ((i=0; i<input_len && i<cmd_len; i++)); do
                if [ "${input:$i:1}" = "${cmd:$i:1}" ]; then
                    common=$((common + 1))
                fi
            done
            local max_len=$cmd_len
            [ "$input_len" -gt "$max_len" ] && max_len=$input_len
            score=$((max_len - common + len_diff))
        fi

        if [ "$score" -lt "$best_score" ]; then
            best_score=$score
            best_match=$cmd
        fi
    done

    # Only suggest if reasonably close (score <= 3)
    if [ "$best_score" -le 3 ] && [ -n "$best_match" ]; then
        printf "\n${YELLOW}Did you mean?${RESET}\n"
        printf "  silicon %s\n\n" "$best_match"
    fi
}

# ── Main dispatch ─────────────────────────────────────────────

CMD="${1:-}"
ARG="${2:-}"
ARG3="${3:-}"

case "$CMD" in
    start)   cmd_start "$ARG" ;;
    stop)    cmd_stop "$ARG" "$ARG3" ;;
    restart) cmd_restart "$ARG" ;;
    status)  cmd_status "$ARG" ;;
    browser) cmd_browser "$ARG" ;;
    debug)   cmd_debug "$ARG" ;;
    attach)  cmd_attach "$ARG" ;;
    pull)    cmd_pull "$ARG" ;;
    push)    cmd_push "$ARG" "$ARG3" ;;
    update)  cmd_update_instance "$ARG" ;;
    list|ls) cmd_list ;;
    agent)   cmd_agent "$ARG" "$ARG3" ;;
    script)
        case "$ARG" in
            update) cmd_update_script ;;
            *) error "Unknown script command: $ARG. Did you mean: silicon script update?"; exit 1 ;;
        esac
        ;;
    new)     cmd_new "$ARG" ;;
    install) cmd_install ;;
    help|-h|--help) cmd_help ;;
    "")      cmd_status "" ;;
    *)
        error "Unknown command: $CMD"
        suggest_command "$CMD"
        ;;
esac
CLIEOF

chmod +x "$CLI_SCRIPT"
success "CLI created at $CLI_SCRIPT"

# ── Add to PATH ───────────────────────────────────────────────

add_to_path() {
    local shell_rc=""
    local current_shell
    current_shell=$(basename "${SHELL:-/bin/bash}")

    case "$current_shell" in
        zsh)  shell_rc="$HOME/.zshrc" ;;
        bash)
            if [ -f "$HOME/.bash_profile" ]; then
                shell_rc="$HOME/.bash_profile"
            else
                shell_rc="$HOME/.bashrc"
            fi
            ;;
        *)    shell_rc="$HOME/.profile" ;;
    esac

    local path_line="export PATH=\"$BIN_DIR:\$PATH\""

    # Check if already in PATH
    if echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
        success "CLI already in PATH"
        return
    fi

    # Check if already in rc file
    if [ -f "$shell_rc" ] && grep -qF "$BIN_DIR" "$shell_rc" 2>/dev/null; then
        success "PATH entry already in $shell_rc"
        # Still export for current session
        export PATH="$BIN_DIR:$PATH"
        return
    fi

    echo "" >> "$shell_rc"
    echo "# Silicon CLI" >> "$shell_rc"
    echo "$path_line" >> "$shell_rc"
    export PATH="$BIN_DIR:$PATH"
    success "Added to PATH via $shell_rc"
}

add_to_path

# ═════════════════════════════════════════════════════════════
# STEP 7: Summary
# ═════════════════════════════════════════════════════════════

header "Installation Complete"

printf "${GREEN}"
cat << 'DONE'
  ╔══════════════════════════════════════════╗
  ║     Silicon is ready to go! 🚀          ║
  ╚══════════════════════════════════════════╝
DONE
printf "${RESET}\n"

if [ "$SKIP_SILICON" = "false" ]; then
    printf "  ${BOLD}Instance:${RESET}  %s\n" "$INSTANCE_NAME"
    printf "  ${BOLD}Location:${RESET}  %s\n" "$ABS_INSTALL_DIR"
fi
printf "  ${BOLD}Registry:${RESET}  %s\n" "$REGISTRY_FILE"
printf "  ${BOLD}CLI:${RESET}       %s\n" "$CLI_SCRIPT"
echo ""
printf "  ${BOLD}${CYAN}Quick start:${RESET}\n"
printf "    ${DIM}# Start a new terminal (or run: source ~/.zshrc)${RESET}\n"
if [ "$SKIP_SILICON" = "false" ]; then
    printf "    silicon start          ${DIM}# Start silicon${RESET}\n"
    printf "    silicon debug           ${DIM}# Attach to live logs${RESET}\n"
    printf "    silicon browser        ${DIM}# Login to services${RESET}\n"
    printf "    silicon stop           ${DIM}# Stop silicon${RESET}\n"
else
    printf "    silicon new            ${DIM}# Create a new silicon${RESET}\n"
fi
printf "    silicon list           ${DIM}# See all instances${RESET}\n"
printf "    silicon script update  ${DIM}# Update CLI to latest${RESET}\n"
echo ""
printf "  ${BOLD}${CYAN}All commands:${RESET}\n"
printf "    silicon help\n"
echo ""
