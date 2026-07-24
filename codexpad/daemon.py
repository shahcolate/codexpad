#!/usr/bin/env python3
"""codexpad daemon - drives Codex Micro Agent Key lighting from Claude Code hooks.

Listens on a Unix socket for state updates from notify.py, maps each Claude Code
session (identified by its working directory) to one of the six Agent Keys, and
writes lighting commands to the device over vendor HID.

Input flows back too: a reader thread dispatches the device's own notifications.
Pressing an Agent Key acknowledges a finished session, the dial trims brightness
and clears the board, and COMMANDS binds any control to a shell command.

Usage:
    python -m codexpad.daemon           # run the daemon
    python -m codexpad.daemon --test    # cycle key 0 through every state and exit
    python -m codexpad.daemon --off     # turn all keys off and exit

See PROTOCOL.md for the wire format.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

try:
    import hid
except ImportError:
    sys.exit("hidapi not installed. Run: pip install hidapi")

# --- device ----------------------------------------------------------------
VID = 0x303A          # Espressif
PID = 0x8360          # Codex Micro
REPORT_ID = 0x06
CHANNEL = 0x02
REPORT_LEN = 64
MAX_BODY = REPORT_LEN - 3   # 61 bytes for JSON + CRLF

SOCK_PATH = os.environ.get("CODEXPAD_SOCK", "/tmp/codexpad.sock")
NSLOTS = 6

# --- state palette ---------------------------------------------------------
# (packed 0xRRGGBB, effect, brightness). Effects: 0 off, 1 solid, 2 snake,
# 3 rainbow, 4 breath, 5 gradient, 6 shallow breath.
STATES = {
    "idle":    (0xFFFFFF, 1, 0.35),
    "working": (0x0000FF, 4, 1.0),
    "blocked": (0xFF8000, 6, 1.0),
    "done":    (0x00FF00, 1, 1.0),
    "error":   (0xFF0000, 1, 1.0),
    "off":     (0x000000, 0, 0.0),
}

# --- controls ----------------------------------------------------------------
# Built-in behaviour: an Agent Key press acknowledges a finished (green or red)
# session back to idle, the dial trims global brightness, and a dial press
# acknowledges everything finished at once.
#
# COMMANDS binds any control to a shell command on top of that, run detached
# with CODEXPAD_KEY, CODEXPAD_CWD and CODEXPAD_STATE in the environment. The
# daemon prints the identifier of every press it sees -- press the control,
# read the log, bind it. Command Key identifiers are not captured yet; see
# PROTOCOL.md §4.1.
COMMANDS = {
    # "AG00":    'open -a "Claude"',
    # "ENC_CLK": "say all clear",
}

_seq = [0]
_trim = [1.0]                 # global brightness trim, dial-adjustable 0.1-1.0
_lock = threading.RLock()     # serialises HID writes and the slot tables


def _rpc(handle, method, params=None):
    """Send one JSON-RPC frame. Fire and forget; replies are never awaited.

    Lighting calls are idempotent and their acks carry nothing we need, so the
    reader thread drops them (see reader()).
    """
    _seq[0] = (_seq[0] % 90) + 1
    msg = {"m": method, "id": _seq[0]}
    if params is not None:
        msg["p"] = params
    body = json.dumps(msg, separators=(",", ":")).encode() + b"\r\n"
    if len(body) > MAX_BODY:
        return False
    frame = bytes([REPORT_ID, CHANNEL, len(body)]) + body
    with _lock:
        handle.write(frame.ljust(REPORT_LEN, b"\x00"))
        time.sleep(0.02)
    return True


def set_slot(handle, slot, state):
    """Apply a named state to one Agent Key.

    Split across two frames: a full ThreadLighting object with an 8-digit
    decimal colour exceeds the 61-byte body limit. Partial updates are legal
    (omitted fields are left unchanged on the device), so this is safe.
    """
    color, effect, brightness = STATES[state]
    with _lock:
        _slot_state[slot] = state
        _rpc(handle, "v.oai.thstatus", [{"id": slot, "c": color}])
        _rpc(handle, "v.oai.thstatus",
             [{"id": slot, "e": effect, "b": round(brightness * _trim[0], 2)}])


# --- slot allocation -------------------------------------------------------
# cwd -> slot index, with LRU eviction once all six keys are taken.
_slots = {}
_order = []
_slot_state = {}   # slot index -> state name last applied


def slot_for(cwd):
    """Return the Agent Key index for this working directory.

    Picks the lowest FREE index rather than len(_slots): after a release,
    len() can collide with a slot that is still occupied.
    """
    with _lock:
        if cwd in _slots:
            _order.remove(cwd)
            _order.append(cwd)
            return _slots[cwd]

        used = set(_slots.values())
        free = [i for i in range(NSLOTS) if i not in used]
        if free:
            slot = free[0]
        else:
            evicted = _order.pop(0)
            slot = _slots.pop(evicted)

        _slots[cwd] = slot
        _order.append(cwd)
        return slot


def release(cwd):
    """Free the slot held by this working directory, if any."""
    with _lock:
        if cwd in _slots:
            _order.remove(cwd)
            return _slots.pop(cwd)
        return None


# --- input -------------------------------------------------------------------
def decode(report):
    """Extract the JSON body from a 64-byte report, or None."""
    if not report or report[0] != REPORT_ID:
        return None
    length = report[2]
    return bytes(report[3:3 + length]).decode("utf-8", "replace")


def run_command(key, cwd, state):
    """Run a COMMANDS binding, detached, never blocking the daemon."""
    env = dict(os.environ,
               CODEXPAD_KEY=key,
               CODEXPAD_CWD=cwd or "",
               CODEXPAD_STATE=state or "")
    try:
        subprocess.Popen(COMMANDS[key], shell=True, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  run     {key} -> {COMMANDS[key]}", flush=True)
    except Exception as exc:
        print(f"  run     {key} failed: {exc}", flush=True)


def trim(handle, delta):
    """Dial detent: nudge the global brightness trim and reapply lit keys."""
    with _lock:
        _trim[0] = min(1.0, max(0.1, round(_trim[0] + delta, 1)))
        lit = [(s, st) for s, st in _slot_state.items() if st and st != "off"]
    print(f"  trim    {int(_trim[0] * 100)}%", flush=True)
    for slot, state in lit:
        set_slot(handle, slot, state)


def dispatch(handle, msg):
    """Turn one device notification into an action (PROTOCOL.md §4.1)."""
    if msg.get("m") != "v.oai.hid":
        return          # v.oai.rad (analog stick): on the roadmap, ignored
    params = msg.get("p") or {}
    key, act = params.get("k", "?"), params.get("act")
    if act == 0:
        return          # releases carry no meaning here

    cwd = state = None
    if key.startswith("AG") and key[2:].isdigit() and int(key[2:]) < NSLOTS:
        slot = int(key[2:])
        with _lock:
            cwd = next((c for c, s in _slots.items() if s == slot), None)
            state = _slot_state.get(slot)
        print(f"  press   {key} ({state or 'free'})", flush=True)
        if state in ("done", "error"):
            set_slot(handle, slot, "idle")
    elif key == "ENC_CW":
        trim(handle, +0.1)
    elif key == "ENC_CC":
        trim(handle, -0.1)
    elif key == "ENC_CLK":
        print("  press   ENC_CLK (ack all)", flush=True)
        with _lock:
            finished = [s for s, st in _slot_state.items()
                        if st in ("done", "error")]
        for slot in finished:
            set_slot(handle, slot, "idle")
    else:
        print(f"  press   {key} (unmapped)", flush=True)

    if key in COMMANDS:
        run_command(key, cwd, state)


def reader(handle):
    """Read device notifications and dispatch them.

    Every notification observed fits a single report, so its body parses on
    its own. Fragments of a chunked RPC reply never do (PROTOCOL.md §2.2),
    and the acks our lighting calls earn carry nothing we need -- so a body
    that fails to parse alone, or parses to a result or error, is dropped
    rather than reassembled. A keypress can therefore never be spliced into a
    half-received reply. RPC that wants replies lives in tools/probe.py.
    """
    while True:
        try:
            body = decode(handle.read(REPORT_LEN))
        except Exception:
            print("  input reader stopped (device gone?)", flush=True)
            return
        if not body:
            time.sleep(0.005)
            continue
        try:
            msg = json.loads(body.strip())
        except json.JSONDecodeError:
            continue        # reply fragment; not ours to reassemble
        if "result" in msg or "error" in msg:
            continue        # ack to one of our lighting calls
        try:
            dispatch(handle, msg)
        except Exception as exc:
            print(f"  input err {exc}", flush=True)


# --- device lifecycle ------------------------------------------------------
def open_device():
    handle = hid.device()
    try:
        handle.open(VID, PID)
    except OSError:
        sys.exit(
            "Could not open the Codex Micro.\n"
            "  - Is it in WIRED mode? Hold the front-left touch control 3s, "
            "then tap to cycle past the three BLE channels until the underglow "
            "turns white.\n"
            "  - On macOS, grant Input Monitoring to your terminal "
            "(System Settings > Privacy & Security > Input Monitoring), then "
            "fully quit and relaunch it.\n"
            "  - Quit the ChatGPT desktop app; it may hold the device."
        )
    return handle


def blank_all(handle):
    for i in range(NSLOTS):
        set_slot(handle, i, "off")


# --- main ------------------------------------------------------------------
def serve(handle):
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o777)   # hooks may run as a different uid than the daemon
    srv.listen(16)

    handle.set_nonblocking(True)
    threading.Thread(target=reader, args=(handle,), daemon=True).start()
    print(f"codexpad ready on {SOCK_PATH} ({NSLOTS} agent keys)", flush=True)

    while True:
        conn, _ = srv.accept()
        try:
            raw = conn.recv(8192).decode()
            req = json.loads(raw) if raw.strip() else {}
            state = req.get("state", "idle")
            cwd = req.get("cwd", "unknown")

            if state == "end":
                slot = release(cwd)
                if slot is not None:
                    set_slot(handle, slot, "off")
                    print(f"  {'end':<7} slot={slot} {cwd}", flush=True)
            elif state in STATES:
                slot = slot_for(cwd)
                set_slot(handle, slot, state)
                print(f"  {state:<7} slot={slot} {cwd}", flush=True)
            else:
                print(f"  ?? unknown state {state!r}", flush=True)
        except Exception as exc:
            print(f"  err {exc}", flush=True)
        finally:
            conn.close()


def main():
    ap = argparse.ArgumentParser(description="Codex Micro lighting daemon")
    ap.add_argument("--test", action="store_true",
                    help="cycle key 0 through every state, then exit")
    ap.add_argument("--off", action="store_true",
                    help="turn all keys off, then exit")
    args = ap.parse_args()

    handle = open_device()

    if args.off:
        blank_all(handle)
        handle.close()
        return

    if args.test:
        for state in ("idle", "working", "blocked", "done", "error"):
            print(f"slot 0 -> {state}", flush=True)
            set_slot(handle, 0, state)
            time.sleep(2.5)
        set_slot(handle, 0, "off")
        handle.close()
        return

    blank_all(handle)
    try:
        serve(handle)
    except KeyboardInterrupt:
        blank_all(handle)
        handle.close()
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
        print("\nstopped")


if __name__ == "__main__":
    main()
