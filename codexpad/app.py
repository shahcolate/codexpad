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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__ as VERSION
from . import config

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification",
               "Stop", "StopFailure", "SessionEnd"]


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


_daemon_proc = [None]


def start_daemon():
    """Spawn python -m codexpad.daemon and wait briefly for its socket.

    On failure the daemon's own output — which includes the wired-mode and
    Input Monitoring instructions — is returned so the UI can show it.
    """
    if daemon_running():
        return {"ok": 1, "note": "daemon already running"}
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen([sys.executable, "-m", "codexpad.daemon"],
                            cwd=repo, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    _daemon_proc[0] = proc
    for _ in range(25):                 # up to ~2.5s
        time.sleep(0.1)
        if proc.poll() is not None:     # it exited: report why, verbatim
            out, _ = proc.communicate()
            return {"error": (out or "").strip() or
                             f"daemon exited with code {proc.returncode}"}
        if daemon_running():
            return {"ok": 1, "note": "daemon started"}
    return {"error": "daemon is starting but its socket isn't answering yet — "
                     "hit Re-check in a moment"}


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
  select, textarea { background: #FBFAF7; color: var(--ink);
    border: 1px solid var(--line); border-radius: 10px; padding: .4rem .6rem; font: inherit; }
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
      </div>
    </div>
  </div>
  <div id="toast">loading…</div>
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

<div class="card">
  <h2>Mic &amp; bindings</h2>
  <p>Mic ring colour <input type="color" id="mic"></p>
  <p class="hint">Shell commands by control — AG00–AG05, ACT06–ACT09,
  ACT12, ENC_CW/ENC_CC/ENC_CLK, STICK_N/E/S/W, MIC_ON/MIC_OFF. Saved with
  the button above.</p>
  <textarea id="commands" rows="5"></textarea>
</div>

<div class="card">
  <h2>Claude Code setup</h2>
  <ul class="check" id="checks"></ul>
  <div class="row">
    <button onclick="startDaemon()">▶ Start daemon</button>
    <button class="primary" onclick="install()">Install hooks</button>
    <button class="primary" onclick="service()">Run at login</button>
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
in <a href="https://github.com/shahcolate/codex-micro-for-claude/blob/main/PROTOCOL.md">PROTOCOL.md</a>.</footer>

<script>
const EFFECTS = {0:"off",1:"solid",2:"snake",3:"rainbow",4:"breath",5:"gradient",6:"shallow breath"};
const PRESETS = {
  Classic: {idle:["FFFFFF",1,.35], working:["0000FF",4,1], blocked:["FF8000",6,1], done:["00FF00",1,1], error:["FF0000",1,1], rainbow:["FFFFFF",3,1]},
  Matrix:  {idle:["013220",1,.3],  working:["00FF41",2,1], blocked:["CCFF00",6,1], done:["00FF41",1,1], error:["FF2222",1,1], rainbow:["00FF41",3,1]},
  Sunset:  {idle:["331133",1,.35], working:["FF4E88",4,1], blocked:["FFB300",6,1], done:["FF7A59",1,1], error:["D7263D",1,1], rainbow:["FF4E88",3,1]},
  Ocean:   {idle:["0A2A3A",1,.35], working:["00B4D8",4,1], blocked:["FFD166",6,1], done:["06D6A0",1,1], error:["EF476F",1,1], rainbow:["00B4D8",3,1]},
  Mono:    {idle:["222222",1,.3],  working:["AAAAAA",4,1], blocked:["FFFFFF",6,1], done:["FFFFFF",1,.6], error:["FFFFFF",2,1], rainbow:["FFFFFF",3,1]},
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
                 mic_color: $("#mic").value.slice(1).toUpperCase(), port: cfg.port };
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
    } else if (spec.effect === 3) {
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
}
async function pollStatus() {
  const s = await api("/api/status");
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
  $("#checks").innerHTML =
    checkItem(d.hidapi, "hidapi installed", "pip install hidapi") +
    checkItem(d.device, "Codex Micro on USB",
              "wired mode: hold the touch control 3s, tap past BLE until white") +
    checkItem(d.daemon, "daemon running", "use ▶ Start daemon below") +
    checkItem(hookCount === 6, `Claude Code hooks (${hookCount}/6) in ${d.hooks.path}`,
              "click Install hooks, then fully restart Claude Code") +
    checkItem(d.service === null ? null : d.service,
              "daemon runs at login (no terminal needed)",
              d.service === null ? "macOS only for now" : "click Run at login");
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
