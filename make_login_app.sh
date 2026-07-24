#!/usr/bin/env bash
# Build ~/Applications/Codexpad.app — an app-shaped wrapper for the daemon.
#
#   ./make_login_app.sh "$(which python)"
#
# macOS grants Input Monitoring to real apps far more reliably than to bare
# python binaries (the ChatGPT app drives this very pad that way). This
# builds a minimal, ad-hoc-signed app bundle that just runs the daemon:
# grant IT the permission, add it to Login Items, and the daemon runs at
# login with no terminal — same end state as the launchd services, but with
# an identity TCC actually respects.
set -euo pipefail
[ "$(uname)" = "Darwin" ] || { echo "macOS only"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${1:-$(command -v python3)}"
[ -x "$PYTHON" ] || { echo "python not found: $PYTHON"; exit 1; }
"$PYTHON" -c "import hid" 2>/dev/null || {
  echo "that python can't import hidapi: $PYTHON"
  echo "pass the one you use for codexpad:  ./make_login_app.sh \"\$(which python)\""
  exit 1
}

APP="$HOME/Applications/Codexpad.app"
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
cd "$REPO"
exec "$PYTHON" -m codexpad.daemon --wait >> /tmp/codexpad.daemon.log 2>&1
EOF
chmod +x "$APP/Contents/MacOS/codexpad"

# ad-hoc signature gives the bundle a stable identity for TCC
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "built: $APP"
echo
echo "Finish in System Settings (one time):"
echo "  1. Privacy & Security > Input Monitoring > '+' > ~/Applications > Codexpad  -> toggle ON"
echo "  2. General > Login Items & Extensions > '+' > Codexpad  (starts it at every login)"
echo
echo "Remove any launchd services first so daemons don't fight:"
echo "  sudo ./service.sh remove"
echo "  launchctl unload ~/Library/LaunchAgents/com.codexpad.daemon.plist 2>/dev/null"
echo
echo "Start it now: open $APP"
echo "Watch:        tail -f /tmp/codexpad.daemon.log"
echo "Stop:         pkill -f codexpad.daemon"
