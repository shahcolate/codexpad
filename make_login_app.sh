#!/usr/bin/env bash
# Build ~/Applications/Codexpad.app — a granted-app launcher that runs the
# daemon as root, giving it BOTH things this pad needs at once.
#
#   ./make_login_app.sh "$(which python)"
#
# The catch this solves: on some Macs opening the pad needs root AND Input
# Monitoring together. A granted app has Input Monitoring but runs as you (no
# root); a root LaunchDaemon has root but no Input Monitoring. This app is
# granted Input Monitoring AND runs the daemon via passwordless sudo, so the
# daemon inherits BOTH — the same Terminal->sudo->python chain that works by
# hand, packaged as a login app. Installs three pieces:
#
#   1. /usr/local/bin/codexpad-daemon   root-owned wrapper (fixed path)
#   2. /etc/sudoers.d/codexpad          NOPASSWD for ONLY that command
#   3. ~/Applications/Codexpad.app      granted app that sudo-runs the wrapper
set -euo pipefail
[ "$(uname)" = "Darwin" ] || { echo "macOS only"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
WRAPPER=/usr/local/bin/codexpad-daemon
SUDOERS=/etc/sudoers.d/codexpad
APP="$HOME/Applications/Codexpad.app"

if [ "${1:-}" = "remove" ]; then
  rm -rf "$APP"
  sudo rm -f "$WRAPPER" "$SUDOERS"
  echo "removed: $APP, $WRAPPER, $SUDOERS"
  echo "also remove Codexpad from Login Items and Input Monitoring by hand."
  exit 0
fi

PYTHON="${1:-$(command -v python3)}"
[ -x "$PYTHON" ] || { echo "python not found: $PYTHON"; exit 1; }
"$PYTHON" -c "import hid" 2>/dev/null || {
  echo "that python can't import hidapi: $PYTHON"
  echo "pass the one you use:  ./make_login_app.sh \"\$(which python)\""
  exit 1
}

echo "Building the login app. Needs sudo ONCE for a passwordless rule on a"
echo "single command ($WRAPPER)."
echo

# don't let other daemons fight over the pad / socket
sudo ./service.sh remove 2>/dev/null || true
./install-login.sh remove 2>/dev/null || true
launchctl unload "$HOME/Library/LaunchAgents/com.codexpad.daemon.plist" 2>/dev/null || true
pkill -f "codexpad.daemon" 2>/dev/null || true
sudo rm -f /tmp/codexpad.sock

# 1. root-owned wrapper -- fixed path so the sudoers rule is exact
sudo mkdir -p /usr/local/bin
sudo tee "$WRAPPER" >/dev/null <<EOF
#!/bin/bash
cd "$REPO"
exec "$PYTHON" -m codexpad.daemon --wait
EOF
sudo chown root:wheel "$WRAPPER"
sudo chmod 755 "$WRAPPER"

# 2. passwordless sudo for ONLY that command, validated so it can't lock you out
TMP="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: $WRAPPER" > "$TMP"
if ! sudo visudo -cf "$TMP" >/dev/null; then
  echo "sudoers rule failed validation; aborting"; rm -f "$TMP"; exit 1
fi
sudo chown root:wheel "$TMP"; sudo chmod 440 "$TMP"; sudo mv "$TMP" "$SUDOERS"

# 3. the app: a granted identity that sudo-runs the wrapper (root + the grant)
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Codexpad</string>
  <key>CFBundleIdentifier</key><string>cc.codexpad.daemon</string>
  <key>CFBundleExecutable</key><string>codexpad</string>
  <key>CFBundleVersion</key><string>0.2.0</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
EOF
cat > "$APP/Contents/MacOS/codexpad" <<EOF
#!/bin/bash
# granted app (Input Monitoring) + sudo (root) = both, the working chain
exec /usr/bin/sudo -n "$WRAPPER"
EOF
chmod +x "$APP/Contents/MacOS/codexpad"
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "built: $APP"
echo
echo "Finish (one time — the app changed, so re-grant even if it was granted):"
echo "  1. Input Monitoring: '+' > Cmd+Shift+G > $HOME/Applications > Codexpad -> ON"
echo "     (remove any stale Codexpad row first with '-')"
echo "  2. Login Items: '+' > $HOME/Applications > Codexpad"
echo "  3. Start it now:  open \"$APP\""
echo
echo "Watch:  tail -f /tmp/codexpad.daemon.log   (want: codexpad ready)"
echo "Remove: ./make_login_app.sh remove"
