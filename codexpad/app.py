#!/usr/bin/env python3
"""codexpad app - the control panel for Codex Micro × Claude Code.

    python -m codexpad              # starts the daemon too, then serves
                                    # http://127.0.0.1:8378

Launches the daemon if one isn't already running (skip with --no-daemon).
A live mockup of the pad showing what each Agent Key is doing, colour pickers
and effect menus for every state with preview on any key, theme presets, a
master brightness slider, mic ring colour, command bindings, the rainbow
button, and a Setup card that checks hidapi / device / daemon / hooks, can
install the Claude Code hooks (runs install.sh), and can start the daemon.

Stdlib only. Binds to 127.0.0.1 only — command bindings are shell commands
run by the daemon and the install button writes ~/.claude/settings.json, so
this page must never be reachable off-machine.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__ as VERSION
from . import config

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification",
               "Stop", "StopFailure", "SessionEnd", "PreToolUse"]


def ask_daemon(payload):
    """Send one cmd to the daemon socket and return its JSON reply."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(config.SOCK_PATH)
        sock.send(json.dumps(payload).encode())
        raw = sock.recv(8192).decode()
        sock.close()
    except Exception as exc:
        return {"error": f"daemon not running ({exc})"}
    if not raw.strip():
        return {"error": "daemon gave no reply — it predates app commands; "
                         "restart it (git pull, then python -m codexpad)"}
    try:
        return json.loads(raw)
    except Exception:
        return {"error": "unparseable reply from daemon"}


def daemon_running():
    return "error" not in ask_daemon({"cmd": "ping"})


def tell_daemon(payload):
    """Fire-and-forget a hook-shaped message (those get no reply)."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(config.SOCK_PATH)
        sock.send(json.dumps(payload).encode())
        sock.close()
        return True
    except Exception:
        return False


_daemon_proc = [None]

WRAPPER_BIN = "/usr/local/bin/codexpad-daemon"
DAEMON_LOG = "/tmp/codexpad.daemon.log"          # the root wrapper logs here
USER_DAEMON_LOG = os.path.join(config._home(), ".codexpad.daemon.log")


def _log_tail(path, lines=10):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8192))
            return b"\n".join(fh.read().splitlines()[-lines:]) \
                    .decode("utf-8", "replace")
    except OSError:
        return ""


def start_daemon():
    """Start the daemon and wait briefly for its socket.

    Prefers the passwordless root wrapper installed by make_login_app.sh /
    install-login.sh when it exists — on Macs that need root + Input
    Monitoring together, that is the only spawn that works. Falls back to
    plain python otherwise. On failure the daemon's own words come back from
    its log file so the UI can show them (a PIPE would deadlock the daemon
    once full, and the wrapper redirects to its log anyway).
    """
    if daemon_running():
        return {"ok": 1, "note": "daemon already running"}
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(WRAPPER_BIN):
        cmd = ["/usr/bin/sudo", "-n", WRAPPER_BIN]
        log_path = DAEMON_LOG
        sink = subprocess.DEVNULL                # the wrapper logs itself
    else:
        cmd = [sys.executable, "-m", "codexpad.daemon", "--wait"]
        log_path = USER_DAEMON_LOG
        sink = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(cmd, cwd=repo, stdout=sink, stderr=sink)
    if sink is not subprocess.DEVNULL:
        sink.close()                    # the child holds its own copy
    _daemon_proc[0] = proc
    for _ in range(30):                 # up to ~3s
        time.sleep(0.1)
        if daemon_running():
            return {"ok": 1, "note": "daemon started"}
        if proc.poll() is not None:
            break
    tail = _log_tail(log_path)
    if proc.poll() is not None and cmd[0] == "/usr/bin/sudo":
        return {"error": "the root wrapper exited straight away — the "
                         "passwordless sudo rule is probably missing or "
                         "stale. Re-run:  ./make_login_app.sh \"$(which "
                         "python)\"\n" + (("\ndaemon log:\n" + tail) if tail
                                          else "")}
    if proc.poll() is not None:
        return {"error": tail or f"daemon exited with code {proc.returncode}"}
    return {"error": "daemon is starting but its socket isn't answering yet — "
                     "hit Re-check in a moment" + (("\n\ndaemon log:\n" + tail)
                                                   if tail else "")}


def hook_status(path=None):
    """Which Claude Code hook events currently point at notify.py."""
    path = path or os.path.join(config._home(), ".claude", "settings.json")
    out = {"path": path, "found": False,
           "events": {e: False for e in HOOK_EVENTS}}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        return out
    out["found"] = True
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    for event in HOOK_EVENTS:
        out["events"][event] = "notify.py" in json.dumps(hooks.get(event, ""))
    return out


def doctor():
    """One-shot health check for the Setup card."""
    result = {"config": config.CONFIG_PATH, "sock": config.SOCK_PATH,
              "hidapi": False, "device": None, "hooks": hook_status()}
    try:
        import hid
        result["hidapi"] = True
        try:
            result["device"] = any(
                (d["vendor_id"], d["product_id"]) == (0x303A, 0x8360)
                for d in hid.enumerate())
        except Exception:
            result["device"] = None      # enumeration itself failed
    except ImportError:
        pass
    ping = ask_daemon({"cmd": "ping"})
    result["daemon"] = "error" not in ping
    result["daemon_version"] = ping.get("v")
    result["app_version"] = VERSION
    result["service"] = (os.path.exists(SERVICE_PLIST)
                         if sys.platform == "darwin" else None)
    # Codexpad.app in Login Items IS the run-at-login mechanism on the macOS
    # path; when it's installed the LaunchAgent button must not be offered —
    # a second spawner would only fight it over the pad.
    result["login_app"] = (
        os.path.exists(os.path.expanduser("~/Applications/Codexpad.app"))
        or os.path.exists("/Applications/Codexpad.app")
        if sys.platform == "darwin" else False)
    result["wrapper"] = os.path.exists(WRAPPER_BIN)
    return result


SERVICE_PLIST = os.path.expanduser(
    "~/Library/LaunchAgents/com.codexpad.daemon.plist")


def install_service():
    """Install a launchd agent: the daemon runs at login, no terminal needed."""
    if sys.platform != "darwin":
        return {"error": "background service install is macOS-only for now"}
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(os.path.dirname(SERVICE_PLIST), exist_ok=True)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.codexpad.daemon</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>-m</string><string>codexpad.daemon</string>
    <string>--wait</string>
  </array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/codexpad.daemon.log</string>
  <key>StandardErrorPath</key><string>/tmp/codexpad.daemon.log</string>
</dict></plist>
""".format(python=sys.executable, repo=repo)
    with open(SERVICE_PLIST, "w") as fh:
        fh.write(xml)
    subprocess.run(["launchctl", "unload", SERVICE_PLIST], capture_output=True)
    proc = subprocess.run(["launchctl", "load", SERVICE_PLIST],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return {"error": "launchctl load failed: "
                         + (proc.stderr or proc.stdout).strip()}
    return {"ok": 1, "plist": SERVICE_PLIST,
            "note": "Installed. The daemon now starts at login and restarts if "
                    "it dies — no terminal needed (log: /tmp/codexpad.daemon.log). "
                    "If keys stay dark after a reboot, grant Input Monitoring to "
                    + sys.executable + ". To remove: launchctl unload "
                    + SERVICE_PLIST}


_codex = {"running": False}


def codex_watch_step(running=None):
    """One tick of the ChatGPT auto-handoff (macOS).

    ChatGPT app appears -> pause the daemon (blank + release the device) so
    the vendor client drives the pad, exactly like before codexpad existed.
    ChatGPT not running -> undo any AUTO pause, wherever it came from: the
    daemon persists who paused (auto vs manual), so a stale auto-handoff
    left over from a restart of either process still resolves here. Manual
    'Hand pad to Codex' is never auto-resumed — that was a person deciding.
    """
    if running is None:
        running = subprocess.run(["pgrep", "-x", "ChatGPT"],
                                 capture_output=True).returncode == 0
    was = _codex["running"]
    _codex["running"] = running
    if not config.load().get("auto_handoff", True):
        return
    if running:
        if not was:
            st = ask_daemon({"cmd": "status"})
            if "error" not in st and not st.get("paused"):
                ask_daemon({"cmd": "pause", "auto": True})
                print("handoff: ChatGPT opened — pad released to it",
                      flush=True)
    else:
        st = ask_daemon({"cmd": "status"})
        if "error" not in st and st.get("paused") \
                and st.get("paused_by") == "auto":
            ask_daemon({"cmd": "resume"})
            print("handoff: ChatGPT is closed — pad reclaimed", flush=True)


def codex_watcher():
    while True:
        try:
            codex_watch_step()
        except Exception:
            pass
        time.sleep(3)


# Focus targets, most specific first: the running one gets `open -a`'d,
# which raises an app without needing any AppleEvents/Accessibility grant.
FOCUS_APPS = [("Claude.app", "Claude"), ("Cursor.app", "Cursor"),
              ("iTerm.app", "iTerm"),
              ("Visual Studio Code.app", "Visual Studio Code"),
              ("Terminal.app", "Terminal")]


def _run(cmd, **extra_env):
    env = dict(os.environ, **extra_env)
    subprocess.Popen(cmd, shell=True, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def focus_session(cwd, cfg):
    """Bring the session that needs you to the front (amber-key press)."""
    custom = (cfg.get("focus_command") or "").strip()
    if custom:
        _run(custom, CODEXPAD_CWD=cwd or "")
        return "focus_command"
    if sys.platform != "darwin":
        return None
    for marker, app in FOCUS_APPS:
        if subprocess.run(["pgrep", "-f", marker],
                          capture_output=True).returncode == 0:
            _run(f"open -a '{app}'")
            return app
    return None


def handle_panel_event(ev, cfg):
    """One pad event, acted on in the user's login session."""
    if isinstance(ev, str):             # mic events travel as plain names
        cmd = {"MIC_ON": cfg.get("mic_on_command"),
               "MIC_OFF": cfg.get("mic_off_command")}.get(ev)
        if cmd:
            _run(cmd)
            print(f"mic: {ev} -> {cmd}", flush=True)
        return
    t = ev.get("t") if isinstance(ev, dict) else None
    if t == "FOCUS":
        target = focus_session(ev.get("cwd"), cfg)
        print(f"focus: {ev.get('cwd')} -> {target or 'no target found'}",
              flush=True)
    elif t == "APPROVE":
        _run("osascript -e 'tell application \"System Events\" "
             "to keystroke return'")
        print("approve: Enter sent to the focused app", flush=True)
    elif t == "DECLINE":
        _run("osascript -e 'tell application \"System Events\" "
             "to key code 53'")
        print("decline: Esc sent to the focused app", flush=True)


def event_pump():
    """Mirror pad events into the user's login session, forever.

    Long-polls the daemon's wait_event and acts on what comes back — mic
    open/close commands, session-focus requests, approve/decline keystrokes.
    All of it runs here, as the logged-in user, where dictation shortcuts
    and AppleScript actually work; the (possibly root) daemon never executes
    any of it. Quietly rides out daemon restarts and daemons too old to know
    wait_event.
    """
    last = -1
    while True:
        r = ask_daemon({"cmd": "wait_event", "after": last, "timeout": 20})
        if "error" in r:
            last = -1                   # daemon gone; resync when it's back
            time.sleep(3)
            continue
        if "seq" not in r:
            time.sleep(15)              # daemon predates events
            continue
        last = r.get("seq", last)
        events = r.get("events") or []
        if not events:
            continue
        cfg = config.load()
        for ev in events:
            try:
                handle_panel_event(ev, cfg)
            except Exception as exc:
                print(f"event {ev!r} failed: {exc}", flush=True)


def run_install():
    """Run install.sh with this interpreter; returns its output."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "install.sh")
    if not os.path.exists(script):
        return {"error": f"install.sh not found at {script}"}
    try:
        proc = subprocess.run([script, sys.executable], capture_output=True,
                              text=True, timeout=30)
        return {"ok": int(proc.returncode == 0), "code": proc.returncode,
                "output": (proc.stdout + proc.stderr).strip()}
    except Exception as exc:
        return {"error": str(exc)}


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>codexpad — Codex Micro × Claude Code</title>
<style>
  :root {
    color-scheme: light;
    --bg: #F5F3EC; --card: #FFFFFF; --line: #E4E0D4;
    --ink: #1F1E1B; --muted: #7A7468; --faint: #A39D90;
    --accent: #C96442; --accent-soft: #F6E5DD;
    --pad: #ECE9E0; --pad-line: #DCD8CB; --key: #FDFCFA;
  }
  * { box-sizing: border-box; }
  body { font: 15.5px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--ink);
         max-width: 780px; margin: 0 auto 5rem; padding: 2.5rem 1.25rem 0; }
  .eyebrow { font-size: .7rem; letter-spacing: .16em; text-transform: uppercase;
             color: var(--muted); margin: 0 0 .6rem; }
  h1 { font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
       font-weight: 500; font-size: 2.4rem; letter-spacing: -.01em;
       margin: 0 0 .25rem; }
  .sub { color: var(--muted); margin: 0 0 1.5rem; }
  h2 { font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
       font-weight: 500; font-size: 1.35rem; margin: 0 0 1rem; }
  .card { background: var(--card); border: 1px solid var(--line);
          border-radius: 16px; padding: 1.5rem 1.6rem; margin: 1.1rem 0; }
  #banner { display: none; background: #FBEFE9; border: 1px solid #E8C4B2;
            color: #8A3B22; border-radius: 12px; padding: .85rem 1.1rem;
            margin: 1rem 0; white-space: pre-wrap; font-size: .9rem; }
  #banner button { margin-top: .6rem; display: block; }
  #banner a { display: block; margin-top: .5rem; color: var(--accent); }
  #toast { min-height: 1.4rem; color: var(--muted); font-size: .9rem;
           margin: .6rem 0 0; }
  table { border-collapse: collapse; width: 100%; }
  td, th { padding: .5rem .5rem; text-align: left; border-bottom: 1px solid var(--line); }
  tr:last-child td { border-bottom: none; }
  th { color: var(--faint); font-weight: 600; font-size: .72rem;
       letter-spacing: .1em; text-transform: uppercase; }
  input[type=color] { width: 2.6rem; height: 1.9rem; border: 1px solid var(--line);
                      border-radius: 8px; background: none; cursor: pointer; padding: 2px; }
  input[type=range] { width: 7rem; accent-color: var(--accent); }
  select, textarea, input[type=text] { background: #FBFAF7; color: var(--ink);
    border: 1px solid var(--line); border-radius: 10px; padding: .4rem .6rem; font: inherit; }
  input[type=text] { width: 100%;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .84rem; }
  /* ---- hardware status strip ---- */
  #hw { display: none; border-radius: 12px; padding: .7rem 1rem; margin: 0 0 1rem;
        font-size: .9rem; white-space: pre-wrap; }
  #hw.ok   { display: block; background: #EAF3E6; border: 1px solid #C8DFBC; color: #3D6B2E; }
  #hw.warn { display: block; background: #FDF3DC; border: 1px solid #EDDCB0; color: #7A5A16; }
  #hw.bad  { display: block; background: #FBEFE9; border: 1px solid #E8C4B2; color: #8A3B22; }
  button { background: var(--card); color: var(--ink); border: 1px solid var(--line);
           border-radius: 999px; padding: .45rem 1.05rem; font: inherit;
           cursor: pointer; transition: border-color .15s, background .15s, transform .05s; }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button:active { transform: scale(.97); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { background: #B4552F; color: #fff; }
  .row { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1rem; }
  textarea { width: 100%; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size: .84rem; }
  .hint { color: var(--muted); font-size: .86rem; }
  code { background: #EFECE2; border-radius: 6px; padding: .1em .4em;
         font-size: .85em; }
  /* ---- state legend ---- */
  #legend { display: flex; flex-wrap: wrap; gap: 1rem .5rem; margin: .2rem 0 1.2rem; }
  #legend .chip { display: flex; align-items: center; gap: .45rem;
                  font-size: .85rem; color: var(--muted); }
  #legend .dot { width: 1.5rem; height: 1.5rem; border-radius: 6px;
                 border: 1px solid rgba(0,0,0,.12); }
  /* ---- the pad mockup ---- */
  .padwrap { display: flex; gap: 1.6rem; flex-wrap: wrap; align-items: center; }
  .pad { background: var(--pad); border: 1px solid var(--pad-line);
         border-radius: 22px; padding: 15px;
         display: grid; grid-template-columns: repeat(4, 56px); gap: 10px;
         transition: box-shadow .3s; }
  .pad.miclive { box-shadow: 0 0 22px 3px rgba(217, 90, 60, .45); }
  .pad > div { height: 56px; border-radius: 12px; background: var(--key);
               border: 1px solid var(--pad-line);
               display: flex; align-items: center; justify-content: center;
               font-size: 1rem; color: var(--faint); user-select: none; }
  .key { cursor: pointer; box-shadow: inset 0 0 13px 2px var(--glow, transparent);
         transition: box-shadow .25s, border-color .15s; }
  .key.sel { border-color: var(--accent); border-width: 2px; }
  .key .n { font-size: .62rem; color: var(--muted); }
  .knob { border-radius: 50% !important; background: #E3E0D6 !important; }
  .stick { border-radius: 50% !important; background: #33312D !important; color: #8B857B !important; }
  .micbar { grid-column: span 2; }
  .micbar.on { background: #F8E0D6; border-color: #E8B49E; color: #B4552F; }
  .touch { border-radius: 50% !important; background: #33312D !important;
           transform: scale(.5); }
  @keyframes breath { 0%,100% { opacity: 1 } 50% { opacity: .4 } }
  @keyframes hue { to { filter: hue-rotate(360deg) } }
  .fx-breath { animation: breath 2.4s ease-in-out infinite; }
  .fx-rainbow { background: conic-gradient(red, yellow, lime, cyan, blue, magenta, red) !important;
                animation: hue 3s linear infinite; }
  .padside { flex: 1; min-width: 215px; }
  .padside label { display: block; color: var(--muted); font-size: .86rem;
                   margin-top: .8rem; }
  .swatches { margin-bottom: .8rem; }
  .swatches button { margin: 0 .35rem .35rem 0; font-size: .88rem;
                     padding: .35rem .9rem; }
  .check { list-style: none; padding: 0; margin: .2rem 0 .4rem; }
  .check li { padding: .18rem 0; }
  .check .ok::before  { content: "✅ "; }
  .check .bad::before { content: "❌ "; }
  .check .meh::before { content: "⚠️ "; }
  #installout { display: none; background: #F2F0E8; border-radius: 10px;
                padding: .7rem .9rem; white-space: pre-wrap;
                font: .8rem ui-monospace, Menlo, monospace; margin-top: .7rem; }
  details { margin-top: .9rem; }
  summary { cursor: pointer; color: var(--muted); }
  footer { color: var(--faint); font-size: .8rem; margin-top: 2.5rem;
           text-align: center; }
  footer a { color: var(--muted); }
</style>

<p class="eyebrow">Codex Micro × Claude Code</p>
<h1>codexpad</h1>
<p class="sub">Your sessions, in colour — blue while Claude works, amber when
it needs you, green when it's done.</p>
<div id="banner"></div>

<div class="card">
  <h2>Your pad, live</h2>
  <div id="hw"></div>
  <div id="legend"></div>
  <div class="padwrap">
    <div class="pad" id="pad">
      <div class="knob" title="dial — brightness / acknowledge">◉</div>
      <div class="key" data-slot="0"><span class="n">AG00</span></div>
      <div class="key" data-slot="1"><span class="n">AG01</span></div>
      <div class="stick" title="joystick">✛</div>
      <div class="key" data-slot="2"><span class="n">AG02</span></div>
      <div class="key" data-slot="3"><span class="n">AG03</span></div>
      <div class="key" data-slot="4"><span class="n">AG04</span></div>
      <div class="key" data-slot="5"><span class="n">AG05</span></div>
      <div title="Fast mode (vendor)">⚡</div>
      <div title="approve (vendor)">✓</div>
      <div title="decline (vendor)">✗</div>
      <div title="new chat (vendor)">⑂</div>
      <div class="touch" title="mode touch control"></div>
      <div class="micbar" id="micbar" title="mic bar — hold or double-press">🎙</div>
      <div title="Codex key">✦</div>
    </div>
    <div class="padside">
      <div class="hint">Click a key to make it the preview target.
        Selected: <b id="selslot">AG00</b></div>
      <label>Master brightness <span id="trimval"></span>
        <input type="range" id="trim" min="0.1" max="1" step="0.1"></label>
      <label>Mic: <b id="micstate">–</b></label>
      <div class="row">
        <button onclick="party()">🌈 Rainbow</button>
        <button onclick="demo()">▶ Demo</button>
        <button onclick="off()">⏻ Off</button>
        <button id="handoff" onclick="handoff()">⇆ Hand pad to Codex</button>
        <button onclick="restore()" title="Relight every lighting zone. Use when the pad looks dead everywhere — including in the ChatGPT app.">✚ Restore lighting</button>
      </div>
      <label class="hint" style="display:block;margin-top:.7rem">
        <input type="checkbox" id="autohand">
        auto-handoff: give ChatGPT the pad while it's open, take it back when it quits
      </label>
    </div>
  </div>
  <div id="toast">loading…</div>
  <div id="today" class="hint" style="margin-top:.4rem"></div>
</div>

<div class="card">
  <h2>Colours &amp; effects</h2>
  <div class="swatches" id="presets"></div>
  <table id="states">
    <tr><th>State</th><th>Colour</th><th>Effect</th><th>Brightness</th><th></th></tr>
  </table>
  <div class="row">
    <button class="primary" onclick="save()">Save &amp; apply</button>
  </div>
  <p class="hint">Writes <code id="cfgpath"></code> — the daemon reloads it live.</p>
</div>

<div class="card" id="miccard">
  <h2>Mic &amp; bindings</h2>
  <p>Mic ring colour <input type="color" id="mic"></p>
  <p class="hint">When the mic bar opens or closes, this app runs these in
  <b>your login session</b> — so dictation shortcuts, AppleScript and
  Raycast all work. <b>Use macOS Dictation</b> fills in the double-Fn
  trigger; you also need System Settings → Keyboard → <b>Dictation on</b>
  with shortcut <b>"Press Fn Twice"</b>, and the first Test will ask for an
  Accessibility grant — allow it, that's macOS asking, not us.</p>
  <div class="row" style="margin:.4rem 0 .8rem">
    <button onclick="dictationPreset()">🎙 Use macOS Dictation</button>
    <button onclick="testMic('on')">Test open</button>
    <button onclick="testMic('off')">Test close</button>
  </div>
  <label class="hint">mic opens →
    <input type="text" id="micon" placeholder="command run when the mic opens"></label>
  <label class="hint" style="display:block;margin-top:.4rem">mic closes →
    <input type="text" id="micoff" placeholder="command run when the mic closes"></label>
  <h2 style="margin-top:1.6rem">Pad → Claude</h2>
  <p class="hint">Press a <b>working or amber key</b> and this app brings that
  session's window forward (auto-detects Claude / Cursor / iTerm / VS Code /
  Terminal — or set your own command, run with <code>CODEXPAD_CWD</code>):</p>
  <label class="hint">focus command (blank = auto)
    <input type="text" id="focuscmd" placeholder="auto — raises the first running app it knows"></label>
  <label class="hint" style="display:block;margin-top:.6rem">
    <input type="checkbox" id="approve">
    ✓ / ✗ keys answer the <b>focused</b> prompt (Enter / Escape — AppleScript,
    needs Accessibility; make sure the right window is front)
  </label>
  <label class="hint" style="display:block;margin-top:.6rem">
    ring nags after <input type="number" id="nagmin" min="0" max="120"
      style="width:4.5rem"> minutes blocked (0 = off)
  </label>
  <p class="hint" style="margin-top:1rem">Shell commands by control — AG00–AG05,
  ACT06–ACT09, ACT12, ENC_CW/ENC_CC/ENC_CLK, STICK_N/E/S/W, MIC_ON/MIC_OFF
  (these run from the daemon, dropped to your user). Saved with the button above.</p>
  <textarea id="commands" rows="5"></textarea>
</div>

<div class="card">
  <h2>Claude Code setup</h2>
  <ul class="check" id="checks"></ul>
  <div class="row">
    <button onclick="startDaemon()">▶ Start daemon</button>
    <button class="primary" onclick="install()">Install hooks</button>
    <button class="primary" id="svcbtn" onclick="service()">Run at login</button>
    <button onclick="refreshDoctor()">↻ Re-check</button>
  </div>
  <div id="installout"></div>
  <details>
    <summary>Manual steps &amp; gotchas</summary>
    <ol class="hint">
      <li>Put the pad in <b>wired mode</b>: hold the front-left touch control 3s,
          tap past the three BLE channels until the underglow turns white.</li>
      <li>macOS: grant <b>Input Monitoring</b> to your terminal, then fully quit
          and relaunch it (<code>sudo</code> works as a stopgap).</li>
      <li>Install hooks (button above, or <code>./install.sh</code>), then
          <b>fully quit and reopen Claude Code</b> — it only reads settings at launch.</li>
      <li>Desktop app: use the <b>Code</b> tab with a <b>Local</b> environment.
          Cloud/SSH sessions run hooks remotely and can't reach your pad.</li>
    </ol>
  </details>
</div>

<footer>Unofficial, unaffiliated, MIT. Every protocol claim is status-tagged
in <a href="https://github.com/shahcolate/codexpad/blob/main/PROTOCOL.md">PROTOCOL.md</a>.</footer>

<script>
// ids the firmware disagrees with on real keys: 2/5 do nothing, 3 = solid red
const EFFECTS = {0:"off",1:"solid",2:"snake (n/a on keys)",3:"rainbow (fw bug: red)",4:"breath",5:"gradient (n/a on keys)",6:"shallow breath"};
const PRESETS = {
  Classic: {idle:["FFFFFF",1,.35], working:["0000FF",4,1], blocked:["FF8000",6,1], done:["00FF00",1,1], error:["FF0000",1,1], rainbow:["FFFFFF",4,1]},
  Matrix:  {idle:["013220",1,.3],  working:["00FF41",4,1], blocked:["CCFF00",6,1], done:["00FF41",1,1], error:["FF2222",1,1], rainbow:["00FF41",4,1]},
  Sunset:  {idle:["331133",1,.35], working:["FF4E88",4,1], blocked:["FFB300",6,1], done:["FF7A59",1,1], error:["D7263D",1,1], rainbow:["FF4E88",4,1]},
  Ocean:   {idle:["0A2A3A",1,.35], working:["00B4D8",4,1], blocked:["FFD166",6,1], done:["06D6A0",1,1], error:["EF476F",1,1], rainbow:["00B4D8",4,1]},
  Mono:    {idle:["222222",1,.3],  working:["AAAAAA",4,1], blocked:["FFFFFF",6,1], done:["FFFFFF",1,.6], error:["FFFFFF",6,1], rainbow:["FFFFFF",4,1]},
};
let cfg = null, selSlot = 0;
const $ = (q) => document.querySelector(q);
const $$ = (q) => document.querySelectorAll(q);
function say(t) { $("#toast").textContent = t; }
async function api(path, body) {
  try {
    const r = await fetch(path, body ? {method: "POST", body: JSON.stringify(body)} : {});
    return await r.json();
  } catch (e) { return {error: "app server unreachable: " + e.message}; }
}
function row(name, spec) {
  const tr = document.createElement("tr");
  const effopts = Object.entries(EFFECTS).map(([v, n]) =>
    `<option value="${v}" ${v == spec.effect ? "selected" : ""}>${n}</option>`).join("");
  tr.innerHTML = `<td><b>${name}</b></td>
    <td><input type="color" value="#${spec.color}" data-k="color"></td>
    <td><select data-k="effect">${effopts}</select></td>
    <td><input type="range" min="0" max="1" step="0.05" value="${spec.brightness}" data-k="brightness"></td>
    <td><button>Try</button></td>`;
  tr.querySelector("button").onclick = () => tryRow(name, tr);
  tr.dataset.name = name;
  return tr;
}
function specOf(tr) {
  return { color: tr.querySelector("[data-k=color]").value.slice(1).toUpperCase(),
           effect: +tr.querySelector("[data-k=effect]").value,
           brightness: +tr.querySelector("[data-k=brightness]").value };
}
function tableSpec(name) {
  const tr = document.querySelector(`#states tr[data-name="${name}"]`);
  if (tr) return specOf(tr);
  const s = cfg && cfg.states[name];
  return s || {color: "000000", effect: 0, brightness: 0};
}
function legend() {
  $("#legend").innerHTML = ["idle", "working", "blocked", "done", "error"]
    .map(n => `<span class="chip"><span class="dot"
      style="background:#${tableSpec(n).color}"></span>${n}</span>`).join("");
}
async function tryRow(name, tr) {
  const r = await api("/api/preview", {slot: selSlot, ...specOf(tr)});
  say(r.error || `AG0${selSlot} → ${name}`);
  legend();
}
async function party() {
  const r = await api("/api/rainbow", {});
  say(r.error || "🌈 press the dial to end the party");
}
async function off() { const r = await api("/api/off", {}); say(r.error || "all off"); }
async function restore() {
  const r = await api("/api/restore", {});
  say(r.error || ("lighting zones relit: " + ((r.zones || []).join(", ") || "none") +
                  " — check the pad, and the ChatGPT app if it was dark there too"));
}
let padPaused = false;
async function handoff() {
  const r = await api(padPaused ? "/api/resume" : "/api/pause", {});
  say(r.error || (padPaused ? "pad is yours again — Claude states repainted"
                            : "pad handed to Codex — open the ChatGPT app; lights are theirs"));
}
async function service() {
  say("installing background service…");
  const r = await api("/api/service/install", {});
  const out = $("#installout");
  out.style.display = "block";
  out.textContent = r.error || r.note || "installed";
  say(r.error ? "service install failed" : "daemon now runs at login — no terminal needed");
  refreshDoctor();
}
async function demo() {
  say("demo: cycling states on AG0" + selSlot);
  for (const name of ["idle", "working", "blocked", "done", "error"]) {
    const s = tableSpec(name);
    const r = await api("/api/preview", {slot: selSlot, ...s});
    if (r.error) return say(r.error);
    say(`demo: ${name}`);
    await new Promise(res => setTimeout(res, 1300));
  }
  await api("/api/preview", {slot: selSlot, ...tableSpec("idle")});
  say("demo done");
}
async function save() {
  const states = {};
  $$("#states tr[data-name]").forEach(tr => { states[tr.dataset.name] = specOf(tr); });
  let commands;
  try { commands = JSON.parse($("#commands").value || "{}"); }
  catch (e) { return say("commands isn't valid JSON: " + e.message); }
  const body = { states, commands,
                 mic_color: $("#mic").value.slice(1).toUpperCase(),
                 mic_on_command: $("#micon").value.trim(),
                 mic_off_command: $("#micoff").value.trim(),
                 auto_handoff: $("#autohand").checked,
                 focus_command: $("#focuscmd").value.trim(),
                 approve_from_pad: $("#approve").checked,
                 nag_minutes: Math.max(0, +$("#nagmin").value || 0),
                 port: cfg.port };
  const r = await api("/api/config", body);
  say(r.error ? "saved, but: " + r.error : "saved — daemon reloaded ✓");
  legend();
}
function applyPreset(name) {
  for (const [state, [c, e, b]] of Object.entries(PRESETS[name])) {
    const tr = document.querySelector(`#states tr[data-name="${state}"]`);
    if (!tr) continue;
    tr.querySelector("[data-k=color]").value = "#" + c;
    tr.querySelector("[data-k=effect]").value = e;
    tr.querySelector("[data-k=brightness]").value = b;
  }
  legend();
  say(`${name} preset loaded — Try a row, then Save & apply`);
}
function paintPad(status) {
  const bySlot = {};
  (status.slots || []).forEach(s => bySlot[s.slot] = s);
  $$(".key").forEach(el => {
    const s = bySlot[+el.dataset.slot] || {state: "off"};
    const spec = tableSpec(s.state);
    el.classList.remove("fx-breath", "fx-rainbow");
    el.style.background = "";
    el.style.opacity = 1;
    if (s.state === "off" || spec.effect === 0) {
      el.style.setProperty("--glow", "transparent");
    } else if (s.state === "rainbow" || spec.effect === 3) {
      el.classList.add("fx-rainbow");
      el.style.setProperty("--glow", "transparent");
    } else {
      el.style.setProperty("--glow", `#${spec.color}`);
      el.style.background = `#${spec.color}30`;
      el.style.opacity = Math.max(spec.brightness, .35);
      if (spec.effect === 4 || spec.effect === 6) el.classList.add("fx-breath");
    }
    el.title = s.cwd ? `${s.state} — ${s.cwd}` : s.state;
  });
  $("#pad").classList.toggle("miclive", !!(status.mic && status.mic.open));
  $("#micbar").classList.toggle("on", !!(status.mic && status.mic.open));
  $("#micstate").textContent = status.mic
    ? (status.mic.open ? (status.mic.latched ? "open (latched)" : "open (hold)") : "closed")
    : "–";
  if (status.trim && document.activeElement !== $("#trim")) {
    $("#trim").value = status.trim;
    $("#trimval").textContent = Math.round(status.trim * 100) + "%";
  }
  padPaused = !!status.paused;
  $("#handoff").textContent = padPaused ? "⇤ Take pad back" : "⇆ Hand pad to Codex";
  $("#pad").style.opacity = padPaused ? .45 : 1;
  if (status.stats) {
    const t = status.stats, mins = s => Math.round(s / 60);
    const parts = [];
    if (t.sessions) parts.push(`${t.sessions} session${t.sessions > 1 ? "s" : ""}`);
    if (t.turns) parts.push(`${t.turns} turn${t.turns > 1 ? "s" : ""} done`);
    if (t.errors) parts.push(`${t.errors} error${t.errors > 1 ? "s" : ""}`);
    if (t.working_s >= 60) parts.push(`${mins(t.working_s)}m of Claude working`);
    if (t.blocked_s >= 30) parts.push(`${mins(Math.max(t.blocked_s, 60))}m of Claude waiting on you`);
    $("#today").textContent = parts.length
      ? "since daemon start: " + parts.join(" · ") : "";
  }
}
function hw(cls, msg) {
  const el = $("#hw");
  el.className = cls || "";
  el.textContent = msg || "";
}
function paintHw(s) {
  if (s.error) {
    hw("bad", "● daemon not reachable — " + s.error +
       "\\nStart it below, or open Codexpad.app (opening the app is always the fix).");
    return;
  }
  const d = s.device || {connected: true};
  if (s.paused) {
    hw("warn", s.paused_by === "auto"
       ? "● pad handed to Codex automatically — it comes back the moment ChatGPT quits."
       : "● pad handed to Codex (manual) — click Take pad back when you want it.");
  } else if (d.connected) {
    hw("ok", "● daemon running · pad connected — buttons below act on the real pad.");
  } else if (d.seen) {
    hw("bad", "● daemon running, pad on USB, but macOS blocks opening it — Input Monitoring.\\n" +
       "Fix: System Settings → Privacy & Security → Input Monitoring → REMOVE the old " +
       "Codexpad row (a rebuild voids the grant), re-add ~/Applications/Codexpad.app, " +
       "toggle ON, then reopen the app." + (d.error ? "\\n(" + d.error + ")" : ""));
  } else if (d.seen === false) {
    hw("bad", "● daemon running but the pad isn't on USB.\\n" +
       "Fix: data-capable cable, quit the ChatGPT app, and put the pad in wired mode — " +
       "hold the front-left touch key 3s, tap until the underglow turns white.");
  } else {
    hw("warn", "● daemon running — still probing USB for the pad…");
  }
}
async function pollStatus() {
  const s = await api("/api/status");
  paintHw(s);
  if (!s.error) { paintPad(s); }
  setTimeout(pollStatus, 2500);
}
function banner(msg, offerStart) {
  const b = $("#banner");
  if (!msg) { b.style.display = "none"; return; }
  b.style.display = "block";
  b.textContent = msg;
  if (offerStart !== false) {
    const btn = document.createElement("button");
    btn.textContent = "▶ Start daemon";
    btn.onclick = startDaemon;
    b.appendChild(btn);
  }
}
async function startDaemon() {
  say("starting daemon…");
  const r = await api("/api/daemon/start", {});
  if (r.error) {
    banner(r.error + "\\n\\n(stopgap: run in a terminal:  sudo python -m codexpad.daemon\\n"
           + "— the app will find it and stop trying to spawn its own)");
    if (r.error.includes("Input Monitoring")) {
      const a = document.createElement("a");
      a.href = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent";
      a.textContent = "Open Input Monitoring settings";
      $("#banner").appendChild(a);
    }
    say("daemon didn't start — the notice above says why");
    refreshDoctor(true);            // update checklist, keep this banner
  } else {
    say(r.note || "daemon started");
    banner(null);
    refreshDoctor();
  }
}
function checkItem(ok, label, hint) {
  const cls = ok === true ? "ok" : (ok === false ? "bad" : "meh");
  return `<li class="${cls}">${label}${ok !== true && hint ? " — <span class='hint'>" + hint + "</span>" : ""}</li>`;
}
async function refreshDoctor(keepBanner) {
  const d = await api("/api/doctor");
  if (d.error) return;
  const hookCount = Object.values(d.hooks.events).filter(Boolean).length;
  const hookTotal = Object.keys(d.hooks.events).length;
  const atLogin = d.login_app ? true : (d.service === null ? null : d.service);
  $("#svcbtn").style.display = d.login_app ? "none" : "";
  $("#checks").innerHTML =
    checkItem(d.hidapi, "hidapi installed", "pip install hidapi") +
    checkItem(d.device, "Codex Micro on USB",
              "wired mode: hold the touch control 3s, tap past BLE until white") +
    checkItem(d.daemon, "daemon running", "use ▶ Start daemon below") +
    checkItem(hookCount === hookTotal,
              `Claude Code hooks (${hookCount}/${hookTotal}) in ${d.hooks.path}`,
              "click Install hooks, then fully restart Claude Code") +
    checkItem(atLogin,
              d.login_app ? "starts at login via Codexpad.app (keep it in Login Items)"
                          : "daemon runs at login (no terminal needed)",
              d.service === null ? "macOS only for now"
                                 : "click Run at login — or better, build Codexpad.app: ./make_login_app.sh");
  if (d.daemon && d.daemon_version !== d.app_version) {
    banner("Mixed builds: the running daemon is " +
      (d.daemon_version || "an older build") + " but this app is " + d.app_version +
      ".\\nStop it (sudo pkill -f codexpad.daemon), then start the daemon from " +
      "THIS repo folder — watch out for stale copies like ~/codexpad or a " +
      "nested clone.", false);
    return;
  }
  if (d.daemon) banner(null);       // running and matching: warnings can go
  else if (!keepBanner) banner("Daemon not running.");
}
async function install() {
  say("running install.sh…");
  const r = await api("/api/install", {});
  const out = $("#installout");
  out.style.display = "block";
  out.textContent = r.error || r.output || "(no output)";
  say(r.error ? "install failed" : (r.ok ? "hooks installed — now fully restart Claude Code" : "install.sh exited non-zero"));
  refreshDoctor();
}
$$(".key").forEach(el => el.onclick = () => {
  selSlot = +el.dataset.slot;
  $$(".key").forEach(k => k.classList.remove("sel"));
  el.classList.add("sel");
  $("#selslot").textContent = "AG0" + selSlot;
});
$("#micbar").onclick = () => {
  $("#miccard").scrollIntoView({behavior: "smooth"});
  say("mic bar: ring colour and open/close commands live in the Mic card (just scrolled there)");
};
document.querySelector(".knob").onclick = () =>
  say("the dial is physical: rotate on the pad to trim brightness, press to acknowledge — mirror it with the slider here");
$$("#pad > div:not(.key):not(.micbar):not(.knob)").forEach(el => el.onclick = () =>
  say("that control is in the vendor firmware's lighting zone — codexpad can't paint it yet ('keys' zone: roadmap). It IS bindable: give it a command in the Mic & bindings card."));
const DICT = `osascript -e 'tell application "System Events" to key code 63' -e 'tell application "System Events" to key code 63'`;
function dictationPreset() {
  $("#micon").value = DICT;
  $("#micoff").value = DICT;      // the same double-Fn toggles dictation off
  say("dictation trigger filled in for open AND close — now Save & apply, then Test open");
}
async function testMic(which) {
  const cmd = (which === "on" ? $("#micon") : $("#micoff")).value.trim();
  if (!cmd) return say("that field is empty — nothing to test");
  const r = await api("/api/mictest", {command: cmd});
  say(r.error || "ran it — did dictation pop up? If not: enable the Fn-twice shortcut and allow Accessibility");
}
$("#trim").oninput = () => { $("#trimval").textContent = Math.round($("#trim").value * 100) + "%"; };
$("#trim").onchange = async () => {
  const r = await api("/api/trim", {value: +$("#trim").value});
  say(r.error || `brightness ${Math.round($("#trim").value * 100)}%`);
};
(async () => {
  cfg = await api("/api/config");
  if (cfg.error) return say(cfg.error);
  for (const [name, spec] of Object.entries(cfg.states)) {
    if (name !== "off") $("#states").appendChild(row(name, spec));
  }
  $("#mic").value = "#" + cfg.mic_color;
  $("#micon").value = cfg.mic_on_command || "";
  $("#micoff").value = cfg.mic_off_command || "";
  $("#autohand").checked = cfg.auto_handoff !== false;
  $("#autohand").onchange = () => { save(); };
  $("#focuscmd").value = cfg.focus_command || "";
  $("#approve").checked = !!cfg.approve_from_pad;
  $("#approve").onchange = () => { save(); };
  $("#nagmin").value = cfg.nag_minutes === undefined ? 10 : cfg.nag_minutes;
  $("#commands").value = JSON.stringify(cfg.commands, null, 2);
  $("#cfgpath").textContent = cfg.config_path || "~/.codexpad.json";
  $("#presets").innerHTML = Object.keys(PRESETS)
    .map(n => `<button onclick="applyPreset('${n}')">${n}</button>`).join("");
  document.querySelector('.key[data-slot="0"]').classList.add("sel");
  legend();
  say("ready");
  refreshDoctor();
  pollStatus();
})();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "codexpad"

    def log_message(self, *args):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(PAGE.encode(), ctype="text/html; charset=utf-8")
        elif self.path == "/api/config":
            cfg = config.load()
            cfg["config_path"] = config.CONFIG_PATH
            self._send(cfg)
        elif self.path == "/api/doctor":
            self._send(doctor())
        elif self.path == "/api/status":
            self._send(ask_daemon({"cmd": "status"}))
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            return self._send({"error": "bad JSON"}, 400)
        if self.path == "/api/config":
            config.save(body)
            self._send(ask_daemon({"cmd": "reload"}))
        elif self.path == "/api/preview":
            fields = {k: body[k] for k in ("slot", "color", "effect", "brightness")
                      if k in body}
            self._send(ask_daemon({"cmd": "preview", **fields}))
        elif self.path == "/api/trim":
            self._send(ask_daemon({"cmd": "trim", "value": body.get("value", 1.0)}))
        elif self.path == "/api/rainbow":
            self._send(ask_daemon({"cmd": "rainbow"}))
        elif self.path == "/api/off":
            self._send(ask_daemon({"cmd": "off"}))
        elif self.path == "/api/ping":
            self._send(ask_daemon({"cmd": "ping"}))
        elif self.path == "/api/install":
            self._send(run_install())
        elif self.path == "/api/daemon/start":
            self._send(start_daemon())
        elif self.path == "/api/service/install":
            self._send(install_service())
        elif self.path == "/api/pause":
            self._send(ask_daemon({"cmd": "pause"}))
        elif self.path == "/api/resume":
            self._send(ask_daemon({"cmd": "resume"}))
        elif self.path == "/api/restore":
            self._send(ask_daemon({"cmd": "restore"}))
        elif self.path == "/api/hook":
            # relay for sessions that can't reach the unix socket themselves
            # (remote/cloud Claude Code over an SSH tunnel): same shape as
            # notify.py sends, forwarded verbatim. Localhost-only server.
            state = body.get("state")
            if not isinstance(state, str):
                self._send({"error": "need {\"state\": ..., \"cwd\": ...}"})
            else:
                ok = tell_daemon({"state": state,
                                  "cwd": body.get("cwd") or "remote"})
                self._send({"ok": int(ok)} if ok
                           else {"error": "daemon not reachable"})
        elif self.path == "/api/mictest":
            # run a mic command once, right now, in the user's session — the
            # page is localhost-only and this is the same trust level as
            # saving the command and pressing the mic bar
            cmd = (body.get("command") or "").strip()
            if not cmd:
                self._send({"error": "no command given"})
            else:
                try:
                    subprocess.Popen(cmd, shell=True,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    self._send({"ok": 1})
                except Exception as exc:
                    self._send({"error": str(exc)})
        else:
            self._send({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="codexpad web app")
    ap.add_argument("--no-daemon", action="store_true",
                    help="don't auto-start the daemon")
    args = ap.parse_args()

    if not args.no_daemon:
        result = start_daemon()
        print("daemon: " + (result.get("note") or result.get("error", "started")),
              flush=True)

    threading.Thread(target=event_pump, daemon=True).start()
    if sys.platform == "darwin":
        threading.Thread(target=codex_watcher, daemon=True).start()

    port = config.load().get("port", config.PORT)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"codexpad app on http://127.0.0.1:{port}  (config: {config.CONFIG_PATH})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
