#!/usr/bin/env bash
# Cellule.ai — one-liner worker installer (Linux + macOS)
#
# Usage:
#   curl -sSL https://cellule.ai/install-worker.sh | bash
#
# What it does:
#   1. Detects OS (Linux / macOS) and architecture
#   2. Checks Python 3.12+ availability (prompts install command if missing)
#   3. Creates ~/.cellule-worker venv
#   4. pip install iamine-ai from the private index
#   5. Creates a systemd --user (Linux) or launchd (macOS) service
#   6. Starts the worker in --auto mode (hardware detection + model pick)
#
# No sudo required if Python 3.12+ is already installed.

set -euo pipefail

# ---- colors ----
if [[ -t 1 ]]; then
    CYAN='\033[0;36m'
    GREEN='\033[0;32m'
    AMBER='\033[0;33m'
    RED='\033[0;31m'
    RESET='\033[0m'
    BOLD='\033[1m'
else
    CYAN='' GREEN='' AMBER='' RED='' RESET='' BOLD=''
fi

log()   { printf '%b\n' "${CYAN}[cellule]${RESET} $*"; }
ok()    { printf '%b\n' "${GREEN}[cellule]${RESET} $*"; }
warn()  { printf '%b\n' "${AMBER}[cellule]${RESET} $*" >&2; }
fail()  { printf '%b\n' "${RED}[cellule]${RESET} $*" >&2; exit 1; }

# ---- banner ----
cat <<'BANNER'

   ____     _ _       _             _
  / ___|___| | |_   _| | ___    __ _(_)
 | |   / _ \ | | | | | |/ _ \  / _` | |
 | |__|  __/ | | |_| | |  __/ | (_| | |
  \____\___|_|_|\__,_|_|\___|  \__,_|_|

  Worker installer — join the distributed LLM network

BANNER

# ---- OS detection ----
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Linux)   PLATFORM="linux" ;;
    Darwin)  PLATFORM="macos" ;;
    *)       fail "Unsupported OS: $OS. Windows users: see https://cellule.ai/docs/install-worker.html" ;;
esac
log "Detected: $PLATFORM $ARCH"

# ---- Python 3.12+ check ----
find_python() {
    for cmd in python3.13 python3.12 python3; do
        if command -v "$cmd" >/dev/null 2>&1; then
            if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" 2>/dev/null; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(find_python || true)"
if [[ -z "$PYTHON" ]]; then
    warn "Python 3.12+ not found."
    echo ""
    echo "Install it first:"
    if [[ "$PLATFORM" == "macos" ]]; then
        echo "  brew install python@3.12"
    else
        if command -v apt >/dev/null 2>&1; then
            echo "  sudo apt update && sudo apt install -y python3.12 python3.12-venv"
        elif command -v dnf >/dev/null 2>&1; then
            echo "  sudo dnf install -y python3.12"
        elif command -v pacman >/dev/null 2>&1; then
            echo "  sudo pacman -S python"
        else
            echo "  (use your distro's package manager to install Python 3.12)"
        fi
    fi
    echo ""
    fail "Re-run this script after installing Python."
fi
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Python $PY_VERSION ($PYTHON)"

# ---- venv + install ----
VENV_DIR="$HOME/.cellule-worker"
if [[ -d "$VENV_DIR" ]]; then
    log "Reusing existing venv at $VENV_DIR"
else
    log "Creating venv at $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
fi

PIP="$VENV_DIR/bin/pip"
"$PIP" install --quiet --upgrade pip setuptools wheel >/dev/null

log "Installing iamine-ai from cellule.ai/pypi (may take 1-3 min)"
"$PIP" install --quiet --upgrade \
    --index-url https://iamine.org/pypi \
    --extra-index-url https://pypi.org/simple \
    iamine-ai || fail "pip install failed. Check network + Python version."

IAMINE_BIN="$VENV_DIR/bin/iamine"
if ! [[ -x "$IAMINE_BIN" ]]; then
    # fallback: module invocation if entry-point is absent in this version
    IAMINE_BIN="$VENV_DIR/bin/python -m iamine"
fi

INSTALLED_VERSION="$($VENV_DIR/bin/python -c 'import iamine; print(iamine.__version__)')"
ok "Installed iamine-ai $INSTALLED_VERSION"

# ---- service setup ----
install_systemd_user() {
    local unit_dir="$HOME/.config/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/cellule-worker.service" <<UNIT
[Unit]
Description=Cellule.ai worker (distributed LLM inference)
After=network.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python -m iamine worker --auto
Restart=always
RestartSec=10
StandardOutput=append:$HOME/.cellule-worker/worker.log
StandardError=append:$HOME/.cellule-worker/worker.log

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable --now cellule-worker.service
    # linger so the service survives logout (if possible without sudo)
    loginctl enable-linger "$USER" 2>/dev/null || warn "Could not enable linger (needs sudo). Service will stop on logout."
    ok "systemd user service installed + started"
    echo ""
    echo "  Check status : systemctl --user status cellule-worker"
    echo "  Live logs    : journalctl --user -u cellule-worker -f"
    echo "  Stop         : systemctl --user stop cellule-worker"
}

install_launchd() {
    local plist="$HOME/Library/LaunchAgents/ai.cellule.worker.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>ai.cellule.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python</string>
        <string>-m</string>
        <string>iamine</string>
        <string>worker</string>
        <string>--auto</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$HOME/.cellule-worker/worker.log</string>
    <key>StandardErrorPath</key><string>$HOME/.cellule-worker/worker.log</string>
</dict>
</plist>
PLIST
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    ok "launchd agent installed + started"
    echo ""
    echo "  Check status : launchctl list | grep cellule"
    echo "  Live logs    : tail -f ~/.cellule-worker/worker.log"
    echo "  Stop         : launchctl unload $plist"
}

# Ask user — default is YES (install service)
echo ""
printf "${BOLD}Install as background service ?${RESET} [Y/n] "
# When piped from curl | bash, /dev/tty gives us the interactive terminal
if [[ -t 0 ]] || [[ -c /dev/tty ]]; then
    if [[ -c /dev/tty ]]; then
        read -r answer < /dev/tty || answer="y"
    else
        read -r answer || answer="y"
    fi
else
    answer="y"
fi
answer="${answer:-y}"

if [[ "$answer" =~ ^[Yy] ]]; then
    if [[ "$PLATFORM" == "linux" ]]; then
        if command -v systemctl >/dev/null 2>&1; then
            install_systemd_user
        else
            warn "systemctl not found — starting worker in foreground instead"
            exec "$VENV_DIR/bin/python" -m iamine worker --auto
        fi
    else
        install_launchd
    fi
else
    log "Running worker in foreground (Ctrl-C to stop)"
    exec "$VENV_DIR/bin/python" -m iamine worker --auto
fi

echo ""
ok "${BOLD}Done.${RESET} Your worker is joining the network."
echo ""
echo "  Admin dashboard : https://cellule.ai/admin"
echo "  See your worker : https://cellule.ai/status"
echo "  Full docs       : https://cellule.ai/docs/install-worker.html"
echo ""
echo "To uninstall later :"
if [[ "$PLATFORM" == "linux" ]]; then
    echo "  systemctl --user disable --now cellule-worker && rm -rf ~/.cellule-worker ~/.config/systemd/user/cellule-worker.service"
else
    echo "  launchctl unload ~/Library/LaunchAgents/ai.cellule.worker.plist && rm -rf ~/.cellule-worker ~/Library/LaunchAgents/ai.cellule.worker.plist"
fi
