#!/usr/bin/env python3
"""codexpad hook client - forwards a Claude Code hook event to the daemon.

Claude Code invokes this from settings.json hooks. It reads the hook's JSON
payload on stdin, extracts the working directory (which identifies the session,
since Desktop gives every session its own git worktree), and sends a one-line
message to the daemon socket.

Usage:
    notify.py <state>

States: idle | working | blocked | done | error | end | pulse
(pulse rides PreToolUse: a shimmer on the session's key per tool call)

This script must never block or fail a Claude Code turn, so every error is
swallowed. Set CODEXPAD_DEBUG=1 to log to /tmp/codexpad.log instead of failing
silently.
"""
import datetime
import json
import os
import socket
import sys

SOCK_PATH = os.environ.get("CODEXPAD_SOCK", "/tmp/codexpad.sock")
DEBUG = os.environ.get("CODEXPAD_DEBUG") == "1"
LOG_PATH = "/tmp/codexpad.log"


def log(message):
    if not DEBUG:
        return
    try:
        with open(LOG_PATH, "a") as handle:
            handle.write(f"{datetime.datetime.now():%H:%M:%S} {message}\n")
    except Exception:
        pass


def main():
    state = sys.argv[1] if len(sys.argv) > 1 else "idle"
    log(f"START state={state}")

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        log(f"  stdin {len(raw)}B")
    except Exception as exc:
        payload = {}
        log(f"  stdin ERR {exc}")

    # cwd is a common field on every hook event. Desktop sessions each get their
    # own worktree, so cwd is a stable per-session identity; session_id is a
    # fallback for surfaces that don't set it.
    cwd = payload.get("cwd") or payload.get("session_id") or "unknown"

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(SOCK_PATH)
        sock.send(json.dumps({"state": state, "cwd": cwd}).encode())
        sock.close()
        log(f"  SENT {state} {cwd}")
    except Exception as exc:
        log(f"  SOCKET ERR {exc}")


if __name__ == "__main__":
    main()
