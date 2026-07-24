#!/usr/bin/env python3
"""codexpad app - a tiny local web UI for colours, effects and party mode.

    python -m codexpad.app          # then open http://127.0.0.1:8378

Colour pickers and effect menus for every state, live preview on Agent Key 0,
mic ring colour, command bindings, and the rainbow button. Save writes
~/.codexpad.json and asks the running daemon to reload it, so changes apply
without a restart.

Stdlib only, binds to 127.0.0.1 only — command bindings are shell commands
run by the daemon, so this page must never be reachable off-machine.
"""
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config


def ask_daemon(payload):
    """Send one cmd to the daemon socket and return its JSON reply."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(config.SOCK_PATH)
        sock.send(json.dumps(payload).encode())
        raw = sock.recv(4096).decode()
        sock.close()
        return json.loads(raw) if raw.strip() else {"ok": 1}
    except Exception as exc:
        return {"error": f"daemon unreachable ({exc})"}


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>codexpad</title>
<style>
  :root { color-scheme: dark; }
  body { font: 15px/1.5 -apple-system, system-ui, sans-serif; background: #111418;
         color: #e8e8e8; max-width: 680px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.4rem; }
  h3 { margin-top: 1.6rem; }
  table { border-collapse: collapse; width: 100%; }
  td, th { padding: .45rem .5rem; text-align: left; border-bottom: 1px solid #2a2f36; }
  input[type=color] { width: 3rem; height: 2rem; border: none; background: none; cursor: pointer; }
  input[type=range] { width: 7rem; }
  select, button, textarea { background: #1c2127; color: #e8e8e8;
    border: 1px solid #333a43; border-radius: 6px; padding: .35rem .6rem; }
  button { cursor: pointer; }
  button:hover { border-color: #5a6470; }
  .big { font-size: 1.05rem; padding: .5rem 1rem; margin-right: .5rem; }
  #status { margin: .8rem 0; min-height: 1.2rem; color: #9aa4af; }
  textarea { width: 100%; font-family: ui-monospace, monospace; font-size: .85rem; }
  .hint { color: #77808a; font-size: .85rem; }
</style>
<h1>🎛️ codexpad</h1>
<p class="hint"><b>Try</b> shows a row live on Agent Key 0. <b>Save</b> writes
~/.codexpad.json and the daemon reloads it on the spot.</p>
<div id="status"></div>
<table id="states">
  <tr><th>State</th><th>Colour</th><th>Effect</th><th>Brightness</th><th></th></tr>
</table>
<p>
  <button class="big" onclick="party()">🌈 Rainbow</button>
  <button class="big" onclick="off()">⏻ All off</button>
  <button class="big" onclick="save()">💾 Save</button>
</p>
<h3>Mic ring colour <input type="color" id="mic"></h3>
<h3>Command bindings</h3>
<p class="hint">Shell commands by control identifier — AG00–AG05, ACT06–ACT09,
ACT12, ENC_CW/ENC_CC/ENC_CLK, STICK_N/E/S/W, MIC_ON/MIC_OFF. JSON object.</p>
<textarea id="commands" rows="6"></textarea>
<script>
const EFFECTS = {0:"off",1:"solid",2:"snake",3:"rainbow",4:"breath",5:"gradient",6:"shallow breath"};
let cfg = null;
const $ = (q) => document.querySelector(q);
function say(t) { $("#status").textContent = t; }
async function api(path, body) {
  const r = await fetch(path, body ? {method: "POST", body: JSON.stringify(body)} : {});
  return r.json();
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
async function tryRow(name, tr) {
  const r = await api("/api/preview", {slot: 0, ...specOf(tr)});
  say(r.error || `key 0 → ${name}`);
}
async function party() {
  const r = await api("/api/rainbow", {});
  say(r.error || "🌈 press the dial to end the party");
}
async function off() {
  const r = await api("/api/off", {});
  say(r.error || "all off");
}
async function save() {
  const states = {};
  document.querySelectorAll("#states tr[data-name]").forEach(tr => {
    states[tr.dataset.name] = specOf(tr);
  });
  let commands;
  try { commands = JSON.parse($("#commands").value || "{}"); }
  catch (e) { return say("commands isn't valid JSON: " + e.message); }
  const body = { states, commands,
                 mic_color: $("#mic").value.slice(1).toUpperCase(),
                 port: cfg.port };
  const r = await api("/api/config", body);
  say(r.error ? "saved, but: " + r.error : "saved — daemon reloaded");
}
(async () => {
  cfg = await api("/api/config");
  for (const [name, spec] of Object.entries(cfg.states)) {
    if (name !== "off") $("#states").appendChild(row(name, spec));
  }
  $("#mic").value = "#" + cfg.mic_color;
  $("#commands").value = JSON.stringify(cfg.commands, null, 2);
  const ping = await api("/api/ping", {});
  say(ping.error
      ? "⚠ daemon not reachable — start it with: python -m codexpad.daemon"
      : "daemon connected");
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
            self._send(config.load())
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
        elif self.path == "/api/rainbow":
            self._send(ask_daemon({"cmd": "rainbow"}))
        elif self.path == "/api/off":
            self._send(ask_daemon({"cmd": "off"}))
        elif self.path == "/api/ping":
            self._send(ask_daemon({"cmd": "ping"}))
        else:
            self._send({"error": "not found"}, 404)


def main():
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
