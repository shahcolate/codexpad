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
STOPPER=/usr/local/bin/codexpad-stop
SUDOERS=/etc/sudoers.d/codexpad
APP="$HOME/Applications/Codexpad.app"

if [ "${1:-}" = "remove" ]; then
  rm -rf "$APP"
  sudo rm -f "$WRAPPER" "$STOPPER" "$SUDOERS"
  echo "removed: $APP, $WRAPPER, $STOPPER, $SUDOERS"
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

# 1. root-owned wrappers -- fixed paths so the sudoers rule is exact.
#    The start wrapper CONVERGES state every run: kills any older daemon,
#    clears the socket, then execs a fresh daemon. It does its own logging
#    as root, so no user-shell redirect can ever hit a root-owned log file.
sudo mkdir -p /usr/local/bin
sudo tee "$WRAPPER" >/dev/null <<EOF
#!/bin/bash
pkill -f "codexpad.daemon" 2>/dev/null
sleep 0.3
rm -f /tmp/codexpad.sock
cd "$REPO"
exec "$PYTHON" -m codexpad.daemon --wait >> /tmp/codexpad.daemon.log 2>&1
EOF
sudo tee "$STOPPER" >/dev/null <<'EOF'
#!/bin/bash
pkill -f "codexpad.daemon" 2>/dev/null
rm -f /tmp/codexpad.sock
exit 0
EOF
sudo chown root:wheel "$WRAPPER" "$STOPPER"
sudo chmod 755 "$WRAPPER" "$STOPPER"
sudo rm -f /tmp/codexpad.daemon.log   # clear any root-locked leftovers

# 2. passwordless sudo for ONLY that command, validated so it can't lock you out
TMP="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: $WRAPPER, $STOPPER" > "$TMP"
if ! sudo visudo -cf "$TMP" >/dev/null; then
  echo "sudoers rule failed validation; aborting"; rm -f "$TMP"; exit 1
fi
sudo chown root:wheel "$TMP"; sudo chmod 440 "$TMP"; sudo mv "$TMP" "$SUDOERS"

# 3. the app: a granted identity that sudo-runs the wrapper (root + the grant),
#    serves the control panel, and opens it
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# icon: build Codexpad.icns from docs/icon.png with the tools on every Mac
if [ -f "$REPO/docs/icon.png" ] && command -v iconutil >/dev/null; then
  ISET="$(mktemp -d)/Codexpad.iconset"; mkdir -p "$ISET"
  for s in 16 32 64 128 256 512 1024; do
    sips -z $s $s "$REPO/docs/icon.png" --out "$ISET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
  done
  # @2x variants LaunchServices expects
  cp "$ISET/icon_32x32.png"   "$ISET/icon_16x16@2x.png"   2>/dev/null || true
  cp "$ISET/icon_64x64.png"   "$ISET/icon_32x32@2x.png"   2>/dev/null || true
  cp "$ISET/icon_256x256.png" "$ISET/icon_128x128@2x.png" 2>/dev/null || true
  cp "$ISET/icon_512x512.png" "$ISET/icon_256x256@2x.png" 2>/dev/null || true
  cp "$ISET/icon_1024x1024.png" "$ISET/icon_512x512@2x.png" 2>/dev/null || true
  iconutil -c icns "$ISET" -o "$APP/Contents/Resources/Codexpad.icns" 2>/dev/null || true
fi
ICONKEY=""
[ -f "$APP/Contents/Resources/Codexpad.icns" ] && \
  ICONKEY="  <key>CFBundleIconFile</key><string>Codexpad</string>"

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
$ICONKEY
  <key>LSUIElement</key><true/>
</dict></plist>
EOF

cat > "$APP/Contents/MacOS/codexpad" <<EOF
#!/bin/bash
# granted app (Input Monitoring) + sudo (root) = both, the working chain.
# Every launch CONVERGES: stop strays, then supervise a fresh daemon and the
# panel -- if either dies, it comes back within 2 seconds. Opening the app is
# always the fix, exactly like the vendor's client.
cd "$REPO"
/usr/bin/sudo -n "$STOPPER" >/dev/null 2>&1
pkill -f "codexpad\.app --no-daemon" 2>/dev/null
( while true; do /usr/bin/sudo -n "$WRAPPER"; sleep 2; done ) &
( while true; do "$PYTHON" -m codexpad.app --no-daemon \\
    >> "\$HOME/.codexpad.app.log" 2>&1; sleep 2; done ) &
sleep 1.5
open http://127.0.0.1:8378
trap '/usr/bin/sudo -n "$STOPPER" >/dev/null 2>&1; pkill -f "codexpad\.app --no-daemon" 2>/dev/null' EXIT
wait
EOF
chmod +x "$APP/Contents/MacOS/codexpad"
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "built: $APP"
echo
echo "!! The app's binary changed, so its old Input Monitoring grant is void."
echo "!! You MUST remove the stale grant and re-add this build, or it will just"
echo "!! sit at 'waiting' -- that has been the sticking point."
echo
echo "Finish (one time):"
echo "  1. System Settings > Privacy & Security > Input Monitoring:"
echo "       - select any existing 'Codexpad' row and click '-' to remove it"
echo "       - click '+', Cmd+Shift+G, go to $HOME/Applications, pick Codexpad"
echo "       - toggle Codexpad ON"
echo "  2. Login Items: '+' > $HOME/Applications > Codexpad  (starts it each login)"
echo "  3. Launch:  open \"$APP\"   (opens the control panel automatically)"
echo
echo "Watch:  tail -f /tmp/codexpad.daemon.log   (want: codexpad ready)"
echo "Remove: ./make_login_app.sh remove"
