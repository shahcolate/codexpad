#!/usr/bin/env python3
"""codexpad MCP server - drive the Codex Micro from any MCP client.

    claude mcp add codexpad -- python -m codexpad.mcp

Exposes the pad to Claude Desktop chats, agents, and anything else that
speaks MCP: paint a key, track a named session, light the ring, run the
rainbow, read status. Everything is forwarded to the running codexpad
daemon over its unix socket -- start the daemon first.

Stdlib-only implementation of MCP's stdio transport (JSON-RPC 2.0, one
message per line). No SDK, no dependencies, same spirit as the rest of
codexpad.
"""
import json
import os
import socket
import sys

SOCK_PATH = os.environ.get("CODEXPAD_SOCK", "/tmp/codexpad.sock")
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {"name": "pad_status",
     "description": "Current pad state: device connection (with diagnosis), "
                    "per-key sessions, mic, brightness, pause, running "
                    "session stats, and the Orca fleet link (worktrees "
                    "followed, how many are waiting on you).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pad_set",
     "description": "Paint one Agent Key directly (slot 0-5). Effects: 0 off, "
                    "1 solid, 3 rainbow (hue cycle), 4 breath, 6 shallow "
                    "breath. Snake (2) and gradient (5) are strip effects "
                    "that only run on the ring, not per-key.",
     "inputSchema": {"type": "object", "required": ["slot", "color"],
                     "properties": {
                         "slot": {"type": "integer", "minimum": 0, "maximum": 5},
                         "color": {"type": "string",
                                   "description": "RRGGBB hex, e.g. 00FF00"},
                         "effect": {"type": "integer", "default": 1},
                         "brightness": {"type": "number", "default": 1.0},
                         "speed": {"type": "number", "default": 1.0,
                                   "description": "animation speed; higher = "
                                                  "faster"}}}},
    {"name": "pad_session",
     "description": "Set a named session's state the way Claude Code hooks "
                    "do; the pad allocates/reuses a key for the name. States: "
                    "idle, working, blocked, done, error, end (end frees the "
                    "key).",
     "inputSchema": {"type": "object", "required": ["name", "state"],
                     "properties": {
                         "name": {"type": "string",
                                  "description": "session identity, e.g. a "
                                                 "project path or task name"},
                         "state": {"type": "string",
                                   "enum": ["idle", "working", "blocked",
                                            "done", "error", "end"]}}}},
    {"name": "pad_ring",
     "description": "Light or clear the ambient ring (the glow around the "
                    "pad).",
     "inputSchema": {"type": "object", "required": ["on"],
                     "properties": {
                         "on": {"type": "boolean"},
                         "color": {"type": "string",
                                   "description": "RRGGBB hex; default is the "
                                                  "configured mic colour"}}}},
    {"name": "pad_rainbow",
     "description": "The firmware's real rainbow: all six keys cycle hues "
                    "until someone presses the dial.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pad_off",
     "description": "Blank every Agent Key.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def ask_daemon(payload, expect_reply=True):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    try:
        s.connect(SOCK_PATH)
        s.send(json.dumps(payload).encode())
        if not expect_reply:
            return {"ok": 1}
        raw = s.recv(8192).decode()
        return json.loads(raw) if raw.strip() else {"ok": 1}
    finally:
        s.close()


def call_tool(name, args):
    if name == "pad_status":
        return ask_daemon({"cmd": "status"})
    if name == "pad_set":
        return ask_daemon({"cmd": "preview", "slot": int(args["slot"]),
                           "color": str(args["color"]),
                           "effect": int(args.get("effect", 1)),
                           "brightness": float(args.get("brightness", 1.0)),
                           "s": float(args.get("speed", 1.0))})
    if name == "pad_session":
        # hook-shaped messages get no reply by design
        return ask_daemon({"state": args["state"],
                           "cwd": "mcp:" + str(args["name"])},
                          expect_reply=False)
    if name == "pad_ring":
        req = {"cmd": "ring", "on": bool(args["on"])}
        if args.get("color"):
            req["color"] = str(args["color"])
        return ask_daemon(req)
    if name == "pad_rainbow":
        return ask_daemon({"cmd": "rainbow"})
    if name == "pad_off":
        return ask_daemon({"cmd": "off"})
    raise ValueError(f"unknown tool {name!r}")


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if msg_id is None:              # notification: nothing to answer
            continue
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion":
                    msg.get("params", {}).get("protocolVersion",
                                              PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codexpad", "version": "0.7.0"}})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                result = call_tool(params.get("name"),
                                   params.get("arguments") or {})
                is_err = isinstance(result, dict) and "error" in result
                reply(msg_id, {"content": [{"type": "text",
                                            "text": json.dumps(result)}],
                               "isError": bool(is_err)})
            except Exception as exc:
                reply(msg_id, {"content": [{"type": "text",
                                            "text": f"codexpad: {exc}"}],
                               "isError": True})
        elif method == "ping":
            reply(msg_id, {})
        else:
            reply(msg_id, error={"code": -32601,
                                 "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
