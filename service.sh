#!/usr/bin/env bash
# Install the codexpad daemon as a ROOT system service (macOS LaunchDaemon).
#
#   sudo ./service.sh "$(which python)"     install / update
#   sudo ./service.sh remove                uninstall
#
# NOTE: on some Macs this is NOT enough. Opening the pad's vendor HID
# interface can require root AND Input Monitoring together, and a LaunchDaemon
# has root but cannot hold an Input Monitoring grant (launchd has no GUI
# identity) -- the daemon then just logs "waiting for the Codex Micro" forever
# with the pad wired. If that happens, use ./install-login.sh instead, which
# sudo-runs the daemon from your login session so it gets both. This script
# still helps where root alone suffices, and starts the daemon at boot,
# restarts it if it dies, waits for the pad, and logs to /tmp.
#
# Prefer the user-level service (the app's "Run at login" button) when Input
# Monitoring cooperates; remove this one first if you switch.
set -euo pipefail

[ "$(uname)" = "Darwin" ] || { echo "macOS only"; exit 1; }
[ "$(id -u)" = "0" ] || { echo "run with sudo:  sudo ./service.sh \"\$(which python)\""; exit 1; }

LABEL=com.codexpad.daemon.root
PLIST=/Library/LaunchDaemons/$LABEL.plist
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "remove" ]; then
  launchctl bootout system/$LABEL 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $PLIST"
  exit 0
fi

PYTHON="${1:-$(command -v python3)}"
[ -x "$PYTHON" ] || { echo "python not found: $PYTHON"; exit 1; }
"$PYTHON" -c "import hid" 2>/dev/null || {
  echo "that python can't import hidapi: $PYTHON"
  echo "pass the one you use for codexpad:  sudo ./service.sh \"\$(which python)\""
  exit 1
}

# don't fight other copies of the daemon
launchctl bootout system/$LABEL 2>/dev/null || true
launchctl bootout "gui/$(id -u "${SUDO_USER:-root}")/com.codexpad.daemon" 2>/dev/null || true
pkill -f "codexpad.daemon" 2>/dev/null || true
rm -f /tmp/codexpad.sock

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON</string><string>-m</string>
    <string>codexpad.daemon</string><string>--wait</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/codexpad.daemon.log</string>
  <key>StandardErrorPath</key><string>/tmp/codexpad.daemon.log</string>
</dict></plist>
EOF
chown root:wheel "$PLIST"
chmod 644 "$PLIST"

launchctl bootstrap system "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
sleep 2
echo
tail -5 /tmp/codexpad.daemon.log 2>/dev/null || true
echo
echo "installed: $PLIST"
echo "log:       tail -f /tmp/codexpad.daemon.log"
echo "remove:    sudo ./service.sh remove"
