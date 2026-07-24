#!/usr/bin/env python3
"""codexpad app - a local web UI for colours, setup and party mode.

    python -m codexpad.app          # then open http://127.0.0.1:8378

Launches the daemon too, if one isn't already running (skip with
--no-daemon). A live mockup of the pad showing what each Agent Key is doing,
colour pickers and effect menus for every state with preview on any key,
theme presets, a master brightness slider, mic ring colour, command bindings,
the rainbow button, and a Setup card that checks hidapi / device / daemon /
hooks, can install the Claude Code hooks (runs install.sh), and can start the
daemon.

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
                         "restart it (git pull, then python -m codexpad.daemon)"}
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
    result["daemon"] = "error" not in ask_daemon({"cmd": "ping"})
    return result


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
<title>codexpad</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 15px/1.5 -apple-system, system-ui, "Segoe UI", sans-serif;
         background: #0d1117; color: #e6edf3; max-width: 760px;
         margin: 1.5rem auto 4rem; padding: 0 1rem; }
  h1 { font-size: 1.5rem; margin: 0;
       background: linear-gradient(90deg, #7ee8fa, #eec0c6);
       -webkit-background-clip: text; background-clip: text; color: transparent; }
  .sub { color: #8b949e; margin: .2rem 0 1rem; }
  .card { background: #161b22; border: 1px solid #21262d; border-radius: 14px;
          padding: 1.1rem 1.2rem; margin: 1rem 0;
          box-shadow: 0 4px 24px rgba(0,0,0,.35); }
  .card h2 { font-size: 1.05rem; margin: 0 0 .8rem; color: #c9d1d9; }
  #banner { display: none; background: #3d1d1d; border: 1px solid #6e2c2c;
            color: #ffb4b4; border-radius: 10px; padding: .6rem .9rem; margin: .8rem 0;
            white-space: pre-wrap; }
  #banner button { margin-top: .5rem; display: block; }
  #toast { min-height: 1.3rem; color: #8b949e; margin: .6rem 0; }
  table { border-collapse: collapse; width: 100%; }
  td, th { padding: .4rem .5rem; text-align: left; border-bottom: 1px solid #21262d; }
  th { color: #8b949e; font-weight: 600; font-size: .8rem; text-transform: uppercase; }
  input[type=color] { width: 2.8rem; height: 2rem; border: none; background: none;
                      cursor: pointer; padding: 0; }
  input[type=range] { width: 7rem; accent-color: #7ee8fa; }
  select, button, textarea { background: #21262d; color: #e6edf3;
    border: 1px solid #30363d; border-radius: 8px; padding: .35rem .6rem; font: inherit; }
  button { cursor: pointer; transition: border-color .15s, transform .05s; }
  button:hover { border-color: #7ee8fa; }
  button:active { transform: scale(.97); }
  .big { font-size: 1rem; padding: .5rem 1rem; margin: 0 .4rem .4rem 0; }
  textarea { width: 100%; font-family: ui-monospace, SFMono-Regular, monospace;
             font-size: .85rem; }
  .hint { color: #6e7681; font-size: .85rem; }
  /* ---- the pad mockup ---- */
  .padwrap { display: flex; gap: 1.4rem; flex-wrap: wrap; align-items: center; }
  .pad { background: #1c2128; border: 1px solid #2d333b; border-radius: 20px;
         padding: 16px; display: grid; grid-template-columns: repeat(4, 58px);
         gap: 10px; transition: box-shadow .3s; }
  .pad.miclive { box-shadow: 0 0 24px 4px rgba(255, 60, 60, .55); }
  .pad > div { height: 58px; border-radius: 12px; background: #2a3038;
               display: flex; align-items: center; justify-content: center;
               font-size: 1.05rem; color: #768390; user-select: none; }
  .key { cursor: pointer; border: 2px solid transparent;
         box-shadow: inset 0 0 14px 2px var(--glow, transparent);
         transition: box-shadow .25s, border-color .15s; }
  .key.sel { border-color: #7ee8fa; }
  .key .n { font-size: .65rem; color: #a3aab3; }
  .knob { border-radius: 50% !important; background: #3a414a !important; }
  .stick { border-radius: 50% !important; background: #14171c !important; }
  .micbar { grid-column: span 2; }
  .micbar.on { background: #4a1f1f; color: #ffb4b4; }
  .touch { border-radius: 50% !important; background: #14171c !important;
           transform: scale(.55); }
  @keyframes breath { 0%,100% { opacity: 1 } 50% { opacity: .35 } }
  @keyframes hue { to { filter: hue-rotate(360deg) } }
  .fx-breath { animation: breath 2.4s ease-in-out infinite; }
  .fx-rainbow { background: conic-gradient(red, yellow, lime, cyan, blue, magenta, red) !important;
                animation: hue 3s linear infinite; }
  .padside { flex: 1; min-width: 210px; }
  .padside label { display: block; color: #8b949e; font-size: .85rem; margin-top: .7rem; }
  .swatches button { margin: 0 .35rem .35rem 0; }
  .check { list-style: none; padding: 0; margin: .4rem 0; }
  .check li { padding: .15rem 0; }
  .check .ok::before  { content: "✅ "; }
  .check .bad::before { content: "❌ "; }
  .check .meh::before { content: "⚠️ "; }
  #installout { display: none; background: #0d1117; border-radius: 8px;
                padding: .6rem .8rem; white-space: pre-wrap;
                font: .8rem ui-monospace, monospace; margin-top: .6rem; }
  details { margin-top: .8rem; } summary { cursor: pointer; color: #8b949e; }
</style>

<h1>🎛️ codexpad</h1>
<p class="sub">Codex Micro × Claude Code — colours, buttons, setup.</p>
<div id="banner"></div>
<div id="toast">loading…</div>

<div class="card">
  <h2>Your pad, live</h2>
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
      <p style="margin-top:.9rem">
        <button class="big" onclick="party()">🌈 Rainbow</button>
        <button class="big" onclick="demo()">▶ Demo</button>
        <button class="big" onclick="off()">⏻ Off</button>
      </p>
    </div>
  </div>
</div>

<div class="card">
  <h2>Colours &amp; effects</h2>
  <div class="swatches" id="presets"></div>
  <table id="states">
    <tr><th>State</th><th>Colour</th><th>Effect</th><th>Brightness</th><th></th></tr>
  </table>
  <p><button class="big" onclick="save()">💾 Save &amp; apply</button>
     <span class="hint">writes <code id="cfgpath"></code>, daemon reloads live</span></p>
</div>

<div class="card">
  <h2>Mic &amp; bindings</h2>
  <p>Mic ring colour <input type="color" id="mic"></p>
  <p class="hint">Shell commands by control — AG00–AG05, ACT06–ACT09, ACT12,
  ENC_CW/ENC_CC/ENC_CLK, STICK_N/E/S/W, MIC_ON/MIC_OFF. Saved with the button above.</p>
  <textarea id="commands" rows="5"></textarea>
</div>

<div class="card">
  <h2>Claude Code setup</h2>
  <ul class="check" id="checks"></ul>
  <p>
    <button class="big" onclick="startDaemon()">▶ Start daemon</button>
    <button class="big" onclick="install()">⚙️ Install hooks</button>
    <button class="big" onclick="refreshDoctor()">↻ Re-check</button>
  </p>
  <div id="installout"></div>
  <details>
    <summary>Manual steps &amp; gotchas</summary>
    <ol class="hint">
      <li>Put the pad in <b>wired mode</b>: hold the front-left touch control 3s,
          tap past the three BLE channels until the underglow turns white.</li>
      <li>macOS: grant <b>Input Monitoring</b> to your terminal, then fully quit
          and relaunch it (<code>sudo</code> works as a stopgap).</li>
      <li>Run the daemon: <code>python -m codexpad.daemon</code> and leave it running.</li>
      <li>Install hooks (button above, or <code>./install.sh</code>), then
          <b>fully quit and reopen Claude Code</b> — it only reads settings at launch.</li>
      <li>Desktop app: use the <b>Code</b> tab with a <b>Local</b> environment.
          Cloud/SSH sessions run hooks remotely and can't reach your pad.</li>
    </ol>
  </details>
</div>

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
async function tryRow(name, tr) {
  const r = await api("/api/preview", {slot: selSlot, ...specOf(tr)});
  say(r.error || `AG0${selSlot} → ${name}`);
}
async function party() {
  const r = await api("/api/rainbow", {});
  say(r.error || "🌈 press the dial to end the party");
}
async function off() { const r = await api("/api/off", {}); say(r.error || "all off"); }
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
}
function applyPreset(name) {
  for (const [state, [c, e, b]] of Object.entries(PRESETS[name])) {
    const tr = document.querySelector(`#states tr[data-name="${state}"]`);
    if (!tr) continue;
    tr.querySelector("[data-k=color]").value = "#" + c;
    tr.querySelector("[data-k=effect]").value = e;
    tr.querySelector("[data-k=brightness]").value = b;
  }
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
      el.style.background = `#${spec.color}2e`;   // colour tint under the glow
      el.style.opacity = Math.max(spec.brightness, .3);
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
}
async function pollStatus() {
  const s = await api("/api/status");
  if (!s.error) { paintPad(s); banner(null); }
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
      a.style.cssText = "display:block;margin-top:.5rem;color:#7ee8fa";
      $("#banner").appendChild(a);
    }
    say("daemon didn't start — the red box says why");
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
              "click Install hooks, then fully restart Claude Code");
  if (d.daemon) banner(null);       // running: any stale warning can go
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
