#!/usr/bin/env python3
"""codexpad daemon - drives Codex Micro Agent Key lighting from Claude Code hooks.

Listens on a Unix socket for state updates from notify.py, maps each Claude Code
session (identified by its working directory) to one of the six Agent Keys, and
writes lighting commands to the device over vendor HID.

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
import sys
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

_seq = [0]


def _rpc(handle, method, params=None):
    """Send one JSON-RPC frame. Fire and forget; we never read replies here.

    Notifications from the device share report ID 0x06, so a reader in this
    process would corrupt response reassembly. Lighting calls are idempotent,
    so dropping the ack costs nothing.
    """
    _seq[0] = (_seq[0] % 90) + 1
    msg = {"m": method, "id": _seq[0]}
    if params is not None:
        msg["p"] = params
    body = json.dumps(msg, separators=(",", ":")).encode() + b"\r\n"
    if len(body) > MAX_BODY:
        return False
    frame = bytes([REPORT_ID, CHANNEL, len(body)]) + body
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
    _rpc(handle, "v.oai.thstatus", [{"id": slot, "c": color}])
    _rpc(handle, "v.oai.thstatus", [{"id": slot, "e": effect, "b": brightness}])


# --- slot allocation -------------------------------------------------------
# cwd -> slot index, with LRU eviction once all six keys are taken.
_slots = {}
_order = []


def slot_for(cwd):
    """Return the Agent Key index for this working directory.

    Picks the lowest FREE index rather than len(_slots): after a release,
    len() can collide with a slot that is still occupied.
    """
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
    if cwd in _slots:
        _order.remove(cwd)
        return _slots.pop(cwd)
    return None


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
