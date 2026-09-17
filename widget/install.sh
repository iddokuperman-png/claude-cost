#!/usr/bin/env bash
# Installs the Claude Cost Tracker desktop widget on macOS and sets it to
# open automatically at login. Safe to re-run (idempotent).
#
#   curl -fsSL https://raw.githubusercontent.com/iddokuperman-png/claude-cost/main/widget/install.sh | bash
#
# or, to read it before running:
#   git clone https://github.com/iddokuperman-png/claude-cost.git
#   bash claude-cost/widget/install.sh
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This widget is macOS-only (it's a native Tk/Cocoa app)." >&2
  exit 1
fi

REPO_RAW="https://raw.githubusercontent.com/iddokuperman-png/claude-cost/main/widget"
APP="$HOME/Applications/Claude Cost Tracker.app"
PLIST="$HOME/Library/LaunchAgents/com.claude-cost.tracker.plist"

echo "== 1/5  Homebrew =="
# `curl | bash` runs a non-login shell, which does NOT source ~/.zprofile —
# that's where the Homebrew installer puts its PATH setup by default on
# Apple Silicon. So `brew` can be genuinely installed and still invisible to
# this script. Check the two standard install locations directly before
# giving up.
if ! command -v brew >/dev/null 2>&1; then
  for _b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$_b" ]]; then
      eval "$("$_b" shellenv)"
      break
    fi
  done
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew isn't installed (checked PATH, /opt/homebrew, /usr/local)." >&2
  echo "This script doesn't install it for you (that step needs your" >&2
  echo "password interactively). Install it from" >&2
  echo "https://brew.sh, then re-run this script." >&2
  exit 1
fi
BREW_PREFIX="$(brew --prefix)"

echo "== 2/5  python-tk@3.11 (gives modern Tk 8.6 — the system Python's Tk 8.5"
echo "        renders a blank window on current macOS) =="
brew list python-tk@3.11 >/dev/null 2>&1 || brew install python-tk@3.11
PY="$BREW_PREFIX/bin/python3.11"
if [[ ! -x "$PY" ]]; then
  echo "Expected $PY after installing python-tk@3.11 but it's missing." >&2
  exit 1
fi

echo "== 3/5  Pillow (charts) =="
"$PY" -c "import PIL" >/dev/null 2>&1 || "$PY" -m pip install --quiet --disable-pip-version-check pillow

echo "== 4/5  building $APP =="
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)"
if [[ -f "$SCRIPT_DIR/CC_tracker.py" ]]; then
  cp "$SCRIPT_DIR/CC_tracker.py" "$APP/Contents/Resources/CC_tracker.py"
  cp "$SCRIPT_DIR/Info.plist.template" "$APP/Contents/Info.plist"
else
  curl -fsSL "$REPO_RAW/CC_tracker.py" -o "$APP/Contents/Resources/CC_tracker.py"
  curl -fsSL "$REPO_RAW/Info.plist.template" -o "$APP/Contents/Info.plist"
fi
cat > "$APP/Contents/MacOS/CC_Tracker" <<EOF
#!/bin/bash
DIR="\$(cd "\$(dirname "\$0")/../Resources" && pwd)"
exec "$PY" "\$DIR/CC_tracker.py" >>"\$DIR/tracker.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/CC_Tracker"

echo "== 5/5  open automatically at login =="
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-cost.tracker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>$APP</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load -w "$PLIST"

open "$APP"
sleep 2
LOG="$APP/Contents/Resources/tracker.log"

if pgrep -f "$APP/Contents/Resources/CC_tracker.py" >/dev/null 2>&1; then
  cat <<'DONE'

Done. The widget will now open automatically every login, and is open now.

To customize your Work/Personal split and (optionally) your org's real
pricing, see the main README for:
  ~/.claude/cc_tracker_buckets.json
  ~/.claude/cc_tracker_pricing.json
Neither file is part of this repo — they stay local to your machine.

To stop auto-opening: launchctl unload ~/Library/LaunchAgents/com.claude-cost.tracker.plist
To uninstall entirely: rm -rf ~/Applications/"Claude Cost Tracker.app" ~/Library/LaunchAgents/com.claude-cost.tracker.plist
DONE
else
  echo
  echo "The app was launched but the process isn't running a couple seconds" >&2
  echo "later — it likely crashed on startup. Log:" >&2
  echo >&2
  tail -n 30 "$LOG" 2>/dev/null >&2 || echo "(no log file at $LOG)" >&2
  exit 1
fi
