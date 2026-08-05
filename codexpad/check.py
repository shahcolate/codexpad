#!/usr/bin/env python3
"""codexpad check - prove every link of the chain, in order, out loud.

    python -m codexpad.check

The chain: Claude Code hook -> notify.py -> unix socket -> daemon -> pad.
This walks it link by link, says PASS or FAIL for each, fires a real test
light at the pad, and ends with exactly one instruction — whatever the
first broken link needs. No guessing, no archaeology.
"""
import json
import os
import socket
import sys
import time

from . import __version__ as VERSION
from . import config
from . import orca

GREEN, RED, DIM, END = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def say(ok, label, extra=""):
    mark = f"{GREEN}PASS{END}" if ok else f"{RED}FAIL{END}"
    print(f"  {mark}  {label}" + (f"  {DIM}{extra}{END}" if extra else ""))


def ask(payload, timeout=3.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(config.SOCK_PATH)
        s.send(json.dumps(payload).encode())
        if "cmd" not in payload:
            return {}
        return json.loads(s.recv(8192).decode())
    finally:
        s.close()


def main():
    print(f"codexpad check {VERSION} — walking the chain\n")

    # 1. daemon
    try:
        pong = ask({"cmd": "ping"})
        daemon_ok = pong.get("ok") == 1
    except OSError as exc:
        pong, daemon_ok = {"err": str(exc)}, False
    say(daemon_ok, "daemon answering on its socket",
        f"v{pong.get('v', '?')}" if daemon_ok else str(pong.get("err", "")))
    if not daemon_ok:
        print(f"\n→ THE fix: start it the proven way, from Terminal:\n"
              f"    sudo -n /usr/local/bin/codexpad-daemon &\n"
              f"  (plain 'python -m codexpad.daemon' cannot open the pad on "
              f"Macs that need root+Input Monitoring)")
        sys.exit(1)
    if daemon_ok and pong.get("v") != VERSION:
        say(False, "daemon build matches this checkout",
            f"daemon {pong.get('v')} vs repo {VERSION}")
        print("\n→ THE fix: restart the daemon so it runs the pulled code:\n"
              "    sudo -n /usr/local/bin/codexpad-daemon &")
        sys.exit(1)

    # 2. pad
    st = ask({"cmd": "status"})
    dev = st.get("device", {})
    if st.get("paused"):
        say(False, "pad is ours (not handed to Codex)",
            f"paused ({st.get('paused_by')})")
        print("\n→ THE fix: quit the ChatGPT app (auto-handoff returns the "
              "pad within ~3s),\n  or click 'Take pad back' in the panel.")
        sys.exit(1)
    say(bool(dev.get("connected")), "pad connected over USB",
        "" if dev.get("connected") else dev.get("error", ""))
    if not dev.get("connected"):
        if dev.get("seen"):
            print("\n→ THE fix: the pad is visible but macOS blocks the open "
                  "— the daemon was\n  started without the grant. Kill it and "
                  "start from your granted Terminal:\n"
                  "    sudo pkill -f codexpad.daemon\n"
                  "    sudo -n /usr/local/bin/codexpad-daemon &")
        else:
            print("\n→ THE fix: pad not on USB — data cable, quit ChatGPT, "
                  "and hold the front-left\n  touch key 3s, tap until the "
                  "underglow is WHITE (wired mode).")
        sys.exit(1)

    # 3. hooks
    hooks_path = os.path.join(config._home(), ".claude", "settings.json")
    try:
        with open(hooks_path) as fh:
            data = json.load(fh)
        hooks = data.get("hooks", {})
    except OSError:
        hooks = {}
    events = ["SessionStart", "UserPromptSubmit", "Notification",
              "Stop", "StopFailure", "SessionEnd", "PreToolUse"]
    have = [e for e in events if "notify.py" in json.dumps(hooks.get(e, ""))]
    say(len(have) == len(events),
        f"Claude Code hooks installed ({len(have)}/{len(events)})",
        hooks_path)
    hooks_ok = len(have) == len(events)

    # 3b. Orca, if the user runs one. Never fatal: the pad works fine
    # without it, so this reports and moves on.
    orca_stat = st.get("orca") or {}
    if orca_stat.get("enabled", True):
        info = orca.probe()
        installed = bool(info["metadata"]) and \
            os.path.isdir(os.path.dirname(info["metadata"]))
        if not info["running"]:
            # not an Orca user, or Orca is simply closed — neither is a fault
            if installed:
                print(f"  {DIM}····  Orca is installed but not running — its "
                      f"worktrees will light the pad when you open it{END}")
        elif not info["reachable"]:
            say(False, "Orca fleet visible (optional)", info["error"])
            print("\n→ Orca wrote its runtime file but won't answer. Restart "
                  "the Orca app;\n  codexpad keeps running on Claude Code "
                  "hooks meanwhile.")
        else:
            say(True, "Orca fleet visible (optional)",
                f"{info['worktrees']} worktrees, {info['agents']} agents, "
                f"{len(info['active'])} would light")
            if orca_stat.get("running") and not orca_stat.get("reachable"):
                print("  (this process can see Orca but the daemon can't — "
                      "a root daemon\n   reads the runtime file from "
                      "$SUDO_USER's home; check ORCA_USER_DATA_PATH)")

    # 4. fire a real light: if this works, codexpad itself is HEALTHY
    print("\n  WATCH THE PAD: a key should go blue-breathing now, "
          "then green, then off…")
    probe_cwd = "codexpad-check"
    ask({"state": "working", "cwd": probe_cwd})
    time.sleep(3)
    ask({"state": "done", "cwd": probe_cwd})
    time.sleep(2)
    ask({"state": "end", "cwd": probe_cwd})
    print("  (if you saw blue → green → off, the daemon and pad are fine)\n")

    if not hooks_ok:
        print("→ THE fix: open the panel (http://127.0.0.1:8378), click "
              "'Install hooks',\n  then FULLY QUIT AND REOPEN Claude Code — "
              "it only reads hooks at launch.")
        sys.exit(1)

    print("→ Every link passes. If Claude STILL doesn't light the pad:\n"
          "  1. FULLY quit and reopen Claude Code (hooks load at launch — "
          "a window\n     that was already open never fires them).\n"
          "  2. Desktop app: sessions must be LOCAL (Code tab → Local "
          "environment).\n     Cloud/web sessions run their hooks remotely "
          "and can't reach this machine.\n"
          "  3. Then submit any prompt and watch for 'working' in:\n"
          "     tail -f /tmp/codexpad.daemon.log")


if __name__ == "__main__":
    main()
