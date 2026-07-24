#!/usr/bin/env bash
# Merge codexpad hooks into ~/.claude/settings.json with absolute paths.
#
#   ./install.sh              use the python on PATH
#   ./install.sh /path/to/python
#
# Backs up your existing settings to ~/.claude/settings.json.bak first.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${1:-$(command -v python3 || command -v python)}"
SETTINGS="$HOME/.claude/settings.json"
NOTIFY="$REPO_DIR/codexpad/notify.py"

command -v jq >/dev/null || { echo "jq is required: brew install jq"; exit 1; }
[ -x "$PYTHON_BIN" ] || { echo "python not found: $PYTHON_BIN"; exit 1; }
[ -f "$NOTIFY" ] || { echo "missing $NOTIFY"; exit 1; }

echo "python : $PYTHON_BIN"
echo "notify : $NOTIFY"

mkdir -p "$HOME/.claude"
[ -s "$SETTINGS" ] || echo '{}' > "$SETTINGS"
cp "$SETTINGS" "$SETTINGS.bak"
echo "backup : $SETTINGS.bak"

sed -e "s|PYTHON|$PYTHON_BIN|g" \
    -e "s|/PATH/TO/codexpad/notify.py|$NOTIFY|g" \
    "$REPO_DIR/hooks/settings.example.json" > /tmp/codexpad-hooks.json

jq -s '.[0] * .[1]' "$SETTINGS" /tmp/codexpad-hooks.json > /tmp/codexpad-merged.json
mv /tmp/codexpad-merged.json "$SETTINGS"
rm -f /tmp/codexpad-hooks.json

echo
echo "hooks installed:"
jq '.hooks | keys' "$SETTINGS"
echo
echo "Next:"
echo "  1. $PYTHON_BIN -m codexpad.daemon --test   # confirm the device responds"
echo "  2. $PYTHON_BIN -m codexpad.daemon          # leave running"
echo "  3. Quit and reopen Claude Code, then send a prompt"
echo
echo "In the desktop app, use the Code tab with a Local environment."
echo "Cloud and SSH sessions run hooks remotely and cannot reach the socket."
