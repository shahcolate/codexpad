#!/usr/bin/env bash
# No-terminal auto-start for Macs where the pad needs root AND Input Monitoring.
#
#   ./install-login.sh "$(which python)"     install
#   ./install-login.sh remove                uninstall
#
# Why this exists: on some Macs opening the pad's vendor HID interface needs
# BOTH root privileges AND Input Monitoring at once. Only "sudo from a granted
# app" provides both -- a plain root LaunchDaemon has root but no Input
# Monitoring (launchd can't hold that grant), and a non-root login agent has
# the grant but not root. This installs:
#
#   1. a tiny root-owned wrapper at /usr/local/bin/codexpad-daemon
#   2. a passwordless-sudo rule for ONLY that one command (validated first)
#   3. a per-user LaunchAgent that sudo-runs it at login (KeepAlive, --wait)
#
# The agent runs in your GUI session, so the daemon it launches is attributed
# to a context that carries your Input Monitoring grant, while sudo supplies
# root. No terminal, survives reboot, restarts if it dies.
set -euo pipefail
[ "$(uname)" = "Darwin" ] || { echo "macOS only"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
WRAPPER=/usr/local/bin/codexpad-daemon
SUDOERS=/etc/sudoers.d/codexpad
AGENT="$HOME/Library/LaunchAgents/cc.codexpad.login.plist"

if [ "${1:-}" = "remove" ]; then
  launchctl unload "$AGENT" 2>/dev/null || true
  rm -f "$AGENT"
  if [ -d "$HOME/Applications/Codexpad.app" ]; then
    # the wrapper and sudoers rule are shared with Codexpad.app — deleting
    # them here would silently break the app's passwordless start
    echo "removed: login agent (kept $WRAPPER and $SUDOERS — Codexpad.app uses them;"
    echo "         to remove everything use ./make_login_app.sh remove)"
  else
    sudo rm -f "$WRAPPER" "$SUDOERS"
    echo "removed: login agent, $WRAPPER, $SUDOERS"
  fi
  exit 0
fi

if [ -d "$HOME/Applications/Codexpad.app" ]; then
  echo "Codexpad.app is installed — it already starts the daemon at login and"
  echo "shares $WRAPPER with this script. Installing both would fight over the"
  echo "pad. Use the app, or './make_login_app.sh remove' first."
  exit 1
fi

PYTHON="${1:-$(command -v python3)}"
[ -x "$PYTHON" ] || { echo "python not found: $PYTHON"; exit 1; }
"$PYTHON" -c "import hid" 2>/dev/null || {
  echo "that python can't import hidapi: $PYTHON"
  echo "pass the one you use for codexpad:  ./install-login.sh \"\$(which python)\""
  exit 1
}

echo "Installing no-terminal auto-start. This needs sudo ONCE to add a"
echo "passwordless rule for a single command ($WRAPPER)."
echo

# stop anything else that might fight over the pad / socket
sudo "$REPO/service.sh" remove 2>/dev/null || true
launchctl unload "$HOME/Library/LaunchAgents/com.codexpad.daemon.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.codexpad.daemon.plist"
launchctl unload "$AGENT" 2>/dev/null || true
sudo pkill -f "[-]m codexpad[.]daemon" 2>/dev/null || true
sudo rm -f /tmp/codexpad.sock /tmp/codexpad.daemon.log

# 1. root-owned wrapper -- fixed path so the sudoers rule can be exact
sudo mkdir -p /usr/local/bin
sudo tee "$WRAPPER" >/dev/null <<EOF
#!/bin/bash
cd "$REPO"
exec "$PYTHON" -m codexpad.daemon --wait
EOF
sudo chown root:wheel "$WRAPPER"
sudo chmod 755 "$WRAPPER"

# 2. passwordless sudo for ONLY that command -- validate before installing so
#    a syntax error can never lock you out of sudo
TMP="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: $WRAPPER" > "$TMP"
if ! sudo visudo -cf "$TMP" >/dev/null; then
  echo "sudoers rule failed validation; aborting, nothing installed"; rm -f "$TMP"; exit 1
fi
sudo chown root:wheel "$TMP"
sudo chmod 440 "$TMP"
sudo mv "$TMP" "$SUDOERS"

# 3. login agent that sudo-runs the wrapper in the GUI session
mkdir -p "$(dirname "$AGENT")"
cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>cc.codexpad.login</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/sudo</string><string>-n</string><string>$WRAPPER</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/codexpad.daemon.log</string>
  <key>StandardErrorPath</key><string>/tmp/codexpad.daemon.log</string>
</dict></plist>
EOF
launchctl load -w "$AGENT"
sleep 2
echo
tail -4 /tmp/codexpad.daemon.log 2>/dev/null || true
echo
echo "installed. log: tail -f /tmp/codexpad.daemon.log"
echo "remove:    ./install-login.sh remove"
echo
echo "If the log says 'codexpad ready', you're done -- no terminal, and it"
echo "comes back on every login. If it still says 'waiting' with the pad"
echo "wired, this Mac won't grant Input Monitoring through the agent either;"
echo "fall back to running 'sudo python -m codexpad.daemon' in a terminal."
