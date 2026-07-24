#!/usr/bin/env python3
"""codexpad daemon - drives Codex Micro Agent Key lighting from Claude Code hooks.

Listens on a Unix socket for state updates from notify.py, maps each Claude Code
session (identified by its working directory) to one of the six Agent Keys, and
writes lighting commands to the device over vendor HID.

Input flows back too: a reader thread dispatches the device's own notifications.
Pressing an Agent Key acknowledges a finished session, the dial trims brightness
and clears the board, stick pushes become one-shot STICK_N/E/S/W flick events,
the mic bar is a hold-to-talk / double-press-to-latch toggle with MIC_ON and
MIC_OFF hooks, and COMMANDS binds any control to a shell command.

Usage:
    python -m codexpad.daemon           # run the daemon
    python -m codexpad.daemon --test    # cycle key 0 through every state and exit
    python -m codexpad.daemon --off     # turn all keys off and exit
    python -m codexpad.app              # colours & bindings UI (separate process)

Colours, effects and command bindings come from ~/.codexpad.json (see
codexpad/config.py for the defaults). See PROTOCOL.md for the wire format.
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

from . import __version__ as VERSION
from . import config

# --- device ----------------------------------------------------------------
VID = 0x303A          # Espressif
PID = 0x8360          # Codex Micro
REPORT_ID = 0x06
CHANNEL = 0x02
REPORT_LEN = 64
MAX_BODY = REPORT_LEN - 3   # 61 bytes for JSON + CRLF

SOCK_PATH = config.SOCK_PATH
NSLOTS = 6

# --- state palette ---------------------------------------------------------
# name -> (packed 0xRRGGBB, effect, brightness), built by load_config() from
# codexpad/config.py defaults overlaid with ~/.codexpad.json. Effects: 0 off,
# 1 solid, 2 snake, 3 rainbow, 4 breath, 5 gradient, 6 shallow breath.
STATES = {}

# --- controls ----------------------------------------------------------------
# Built-in behaviour: an Agent Key press acknowledges a finished (green, red
# or rainbow) key, the dial trims global brightness, and a dial press
# acknowledges everything finished at once.
#
# COMMANDS binds any control to a shell command on top of that, run detached
# with CODEXPAD_KEY, CODEXPAD_CWD and CODEXPAD_STATE in the environment. It is
# populated from the "commands" table in ~/.codexpad.json -- edit that with
# the app (python -m codexpad.app). Known identifiers (PROTOCOL.md §4.1):
# AG00-AG05 Agent Keys; ACT06-ACT09 Command Keys (lightning, check, cross,
# fork); ACT12 Codex key; ENC_CW/ENC_CC/ENC_CLK dial; STICK_N/E/S/W flicks;
# and MIC_ON/MIC_OFF from the mic bar's state machine (ACT10/ACT11 are
# consumed by it, bind the MIC events instead). The daemon prints the
# identifier of every press it sees.
COMMANDS = {}

# --- mic bar -----------------------------------------------------------------
# The wide mic key sits on two switches and a full press usually fires both,
# so ACT10 and ACT11 fold into one logical key. Hold it to keep the mic open
# for the hold; double-press to latch it open until the next double-press.
# Opening and closing fire MIC_ON / MIC_OFF (bind those in COMMANDS -- the
# daemon has no microphone of its own) and light the ambient ring red.
MIC_KEYS = frozenset(("ACT10", "ACT11"))
MIC_DOUBLE_S = 0.4    # max gap between taps of a double-press
MIC_HOLD_S = 0.35     # held longer than this = push-to-talk
MIC_COLOR = [0xFF0000]   # set by load_config()

_mic = {"down": set(), "down_at": 0.0, "last_down": 0.0,
        "latched": False, "open": False}


def load_config():
    """Build the live tables from defaults + ~/.codexpad.json."""
    cfg = config.load()
    STATES.clear()
    STATES.update(config.states_as_tuples(cfg))
    COMMANDS.clear()
    COMMANDS.update({k: v for k, v in cfg["commands"].items()
                     if isinstance(v, str)})
    MIC_COLOR[0] = config.color_int(cfg["mic_color"])
    APPROVE_FROM_PAD[0] = bool(cfg.get("approve_from_pad"))
    NAG_MINUTES[0] = cfg.get("nag_minutes", 10)
    return cfg


APPROVE_FROM_PAD = [False]    # checkmark/cross answer the focused prompt
NAG_MINUTES = [10]            # ring lights after this long blocked; 0 = off


load_config()

_seq = [0]
_trim = [1.0]                 # global brightness trim, dial-adjustable 0.1-1.0
_stick = [None]               # flick currently held, so one push fires once
_paused = [False]             # True: the vendor client owns the pad
_lock = threading.RLock()     # serialises HID writes and the slot tables

# Pause must survive a daemon restart: the login app supervises the daemon
# with a restart loop, and a respawn that forgot it was paused would repaint
# Claude states all over the vendor client mid-handoff.
PAUSE_FLAG = SOCK_PATH + ".paused"

# --- session stats -----------------------------------------------------------
# The daemon sees every session's lifecycle anyway; keep the running tally the
# panel shows as the "Today" card. blocked_s is the headline: how long Claude
# sat waiting on YOU.
_stats = {"since": time.time(), "sessions": 0, "turns": 0, "errors": 0,
          "working_s": 0.0, "blocked_s": 0.0}
_state_since = {}             # cwd -> (semantic state, t0) from hook messages


def _note_transition(cwd, new_state):
    """Accumulate time spent working/blocked as hook states change."""
    now = time.time()
    prev = _state_since.get(cwd)
    if prev:
        old, t0 = prev
        if old == "working":
            _stats["working_s"] += now - t0
        elif old == "blocked":
            _stats["blocked_s"] += now - t0
    if new_state == "end":
        _state_since.pop(cwd, None)
    else:
        _state_since[cwd] = (new_state, now)

# --- event feed --------------------------------------------------------------
# The panel long-polls {"cmd": "wait_event"} to mirror pad activity into the
# user's login session -- that's where mic open/close can trigger things a
# root daemon never could (dictation, AppleScript). Tiny ring buffer; a
# client passes the last seq it saw and blocks until something newer.
_event_seq = [0]
_event_log = []               # [(seq, name)], newest last, capped
_event_cond = threading.Condition()


def emit_event(name):
    with _event_cond:
        _event_seq[0] += 1
        _event_log.append((_event_seq[0], name))
        del _event_log[:-64]
        _event_cond.notify_all()


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


# Software rainbow: firmware effect 3 renders solid red on the Agent Keys
# (PROTOCOL.md §5.2), so the party is built from confirmed effects instead --
# one hue per key, spread across the spectrum.
RAINBOW_HUES = [0xFF0000, 0xFF8800, 0xFFEE00, 0x00E020, 0x0066FF, 0xC400FF]


def set_slot(handle, slot, state):
    """Apply a named state to one Agent Key.

    Split across two frames: a full ThreadLighting object with an 8-digit
    decimal colour exceeds the 61-byte body limit. Partial updates are legal
    (omitted fields are left unchanged on the device), so this is safe.
    """
    color, effect, brightness = STATES[state]
    if state == "rainbow":
        color = RAINBOW_HUES[slot % len(RAINBOW_HUES)]
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
    """Run a COMMANDS binding, detached, never blocking the daemon.

    A sudo'd daemon drops to the invoking user first: the bindings live in a
    user-writable config file, and a root daemon must not execute those as
    root. (Commands that need the login session anyway -- dictation,
    AppleScript -- belong in mic_on_command/mic_off_command, which the panel
    runs; see wait_event.)
    """
    env = dict(os.environ,
               CODEXPAD_KEY=key,
               CODEXPAD_CWD=cwd or "",
               CODEXPAD_STATE=state or "")
    kwargs = {}
    if getattr(os, "geteuid", lambda: -1)() == 0 and os.environ.get("SUDO_USER"):
        kwargs["user"] = os.environ["SUDO_USER"]
    try:
        try:
            subprocess.Popen(COMMANDS[key], shell=True, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kwargs)
        except TypeError:            # Python < 3.9: no user=; run as-is
            subprocess.Popen(COMMANDS[key], shell=True, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
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


def _direction(a):
    """Quantise a normalised angle into a compass quadrant.

    Orientation established by directed flicks on hardware (PROTOCOL.md §4.2):
    a=0 is down/south and increases counter-clockwise, so up is 0.5 and right
    is 0.25. Down, up and right are confirmed; the west bucket is inferred by
    symmetry -- if a left push doesn't print STICK_W, this is the place to fix.
    """
    if a >= 0.875 or a < 0.125:
        return "S"
    if a < 0.375:
        return "E"
    if a < 0.625:
        return "N"
    return "W"


def flick(a, d):
    """Quantise stick deflection into one bindable flick per push.

    The stick streams v.oai.rad continuously while deflected, so this fires
    once when deflection crosses 0.7 and re-arms only after it falls below
    0.3 -- the hysteresis stops a wobbling hold from machine-gunning events.
    """
    if _stick[0] is not None:
        if d < 0.3:
            _stick[0] = None
        return
    if d >= 0.7:
        name = "STICK_" + _direction(a)
        _stick[0] = name
        print(f"  flick   {name} (a={a:.2f})", flush=True)
        if name in COMMANDS:
            run_command(name, None, None)


def set_ring(handle, on, color=None):
    """Light the ambient ring (mic indicator, nag light, MCP callers).

    Single-zone partial updates, split across two frames like set_slot to fit
    the 61-byte body, fire and forget. This path is what confirmed the ambient
    zone on hardware (PROTOCOL.md §5.3) -- but only for c/e/b at solid, and
    replies are never read. If the ring stays dark the mic events still fire;
    probe the method directly and report what it returns.
    """
    if on:
        _rpc(handle, "v.oai.rgbcfg",
             {"ambient": {"c": MIC_COLOR[0] if color is None else color}})
        _rpc(handle, "v.oai.rgbcfg", {"ambient": {"e": 1, "b": 1}})
    else:
        _rpc(handle, "v.oai.rgbcfg", {"ambient": {"e": 0, "b": 0}})


_last_pulse = {}              # slot -> last shimmer, rate-limited


def pulse(handle, slot):
    """A one-blink shimmer on a working key: each tool call flickers it, so a
    busy session visibly differs from one just sitting in 'working'."""
    now = time.time()
    if now - _last_pulse.get(slot, 0) < 0.5:
        return
    _last_pulse[slot] = now
    with _lock:
        state = _slot_state.get(slot)
        if state != "working":
            return
        _, _, brightness = STATES[state]
        _rpc(handle, "v.oai.thstatus",
             [{"id": slot, "b": round(max(0.15, brightness * _trim[0] * 0.3), 2)}])

    def restore():
        with _lock:
            if _slot_state.get(slot) == "working" and not _paused[0]:
                _rpc(handle, "v.oai.thstatus",
                     [{"id": slot, "b": round(brightness * _trim[0], 2)}])
    threading.Timer(0.12, restore).start()


_nag_on = [False]


def nag_tick(handle):
    """Escalate a long-ignored amber: light the ambient ring in the blocked
    colour once any session has waited longer than nag_minutes."""
    if not NAG_MINUTES[0] or _paused[0] or _mic["open"]:
        if _mic["open"]:
            _nag_on[0] = False   # mic owns the ring; re-light after it closes
        return
    now = time.time()
    with _lock:
        overdue = any(st == "blocked" and now - t0 > NAG_MINUTES[0] * 60
                      for st, t0 in _state_since.values())
    if overdue and not _nag_on[0]:
        _nag_on[0] = True
        set_ring(handle, True, color=STATES["blocked"][0])
        print("  nag     a session has been waiting on you — ring lit",
              flush=True)
    elif not overdue and _nag_on[0]:
        _nag_on[0] = False
        set_ring(handle, False)


def nagger(handle):
    while True:
        time.sleep(20)
        try:
            nag_tick(handle)
        except Exception:
            pass


def _mic_set(handle, is_open, how):
    if _mic["open"] == is_open:
        return
    _mic["open"] = is_open
    print(f"  mic     {f'ON ({how})' if is_open else 'OFF'}", flush=True)
    set_ring(handle, is_open)
    name = "MIC_ON" if is_open else "MIC_OFF"
    emit_event(name)
    if name in COMMANDS:
        run_command(name, None, None)


def _mic_hold_check(handle):
    with _lock:
        if _mic["down"] and not _mic["latched"] and not _mic["open"]:
            _mic_set(handle, True, "hold")


def mic_event(handle, key, act):
    """Fold ACT10/ACT11 into one logical key and run the mic state machine.

    Relies on release notifications (act 0), whose pattern for ACT keys is
    presumed from the other keys but not yet captured -- if holds never
    close, that presumption is wrong and this needs a timeout fallback.
    """
    with _lock:
        now = time.time()
        if act != 0:                      # press; tolerate a stray act=2
            first = not _mic["down"]
            _mic["down"].add(key)
            if not first:
                return                    # other switch of the same press
            if now - _mic["last_down"] <= MIC_DOUBLE_S:
                if _mic["latched"]:
                    _mic["latched"] = False
                    _mic_set(handle, False, "")
                else:
                    _mic["latched"] = True
                    _mic_set(handle, True, "latched")
            elif not _mic["latched"]:
                threading.Timer(MIC_HOLD_S, _mic_hold_check, (handle,)).start()
            _mic["last_down"] = now
            _mic["down_at"] = now
        else:                             # release
            _mic["down"].discard(key)
            if _mic["down"]:
                return                    # other switch still down
            if (_mic["open"] and not _mic["latched"]
                    and now - _mic["down_at"] >= MIC_HOLD_S):
                _mic_set(handle, False, "")


def dispatch(handle, msg):
    """Turn one device notification into an action (PROTOCOL.md §4.1)."""
    if _paused[0]:
        return          # vendor client owns the pad; stay silent
    params = msg.get("p") or {}
    if msg.get("m") == "v.oai.rad":
        flick(params.get("a") or 0, params.get("d") or 0)
        return
    if msg.get("m") != "v.oai.hid":
        return
    key, act = params.get("k", "?"), params.get("act")
    if key in MIC_KEYS:
        mic_event(handle, key, act)
        return
    if act == 0:
        return          # releases only matter to the mic bar

    cwd = state = None
    if key.startswith("AG") and key[2:].isdigit() and int(key[2:]) < NSLOTS:
        slot = int(key[2:])
        with _lock:
            cwd = next((c for c, s in _slots.items() if s == slot), None)
            state = _slot_state.get(slot)
        print(f"  press   {key} ({state if state not in (None, 'off') else 'free'})",
              flush=True)
        if state in ("done", "error", "rainbow"):
            set_slot(handle, slot, "idle" if cwd else "off")
        elif state in ("working", "blocked") and cwd:
            # take me there: the panel focuses this session's window
            emit_event({"t": "FOCUS", "cwd": cwd, "state": state})
    elif key == "ACT07" and APPROVE_FROM_PAD[0]:
        print("  press   ACT07 (approve -> Enter)", flush=True)
        emit_event({"t": "APPROVE"})
    elif key == "ACT08" and APPROVE_FROM_PAD[0]:
        print("  press   ACT08 (decline -> Esc)", flush=True)
        emit_event({"t": "DECLINE"})
    elif key == "ENC_CW":
        trim(handle, +0.1)
    elif key == "ENC_CC":
        trim(handle, -0.1)
    elif key == "ENC_CLK":
        print("  press   ENC_CLK (ack all)", flush=True)
        with _lock:
            finished = [s for s, st in _slot_state.items()
                        if st in ("done", "error", "rainbow")]
            owned = set(_slots.values())
        for slot in finished:
            set_slot(handle, slot, "idle" if slot in owned else "off")
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
class Device:
    """Wraps the hid handle so the daemon survives unplug/replug -- and can
    start before the pad is even reachable.

    Writes and reads mark the device lost on failure instead of raising; the
    watch() loop (run in a background thread) opens the pad whenever it shows
    up and repaints the current session states. While lost it keeps a
    diagnosis current: `seen` says whether the pad is visible on USB at all
    (enumeration needs no permission), `last_error` is the open failure
    verbatim -- together they tell "not plugged in / BLE mode" apart from
    "macOS is blocking the open", and status replies carry both.
    """

    def __init__(self, handle=None):
        self._h = handle
        self.lost = handle is None
        self.seen = None          # pad visible in hid.enumerate()?
        self.last_error = ""      # last open() failure, verbatim

    def write(self, data):
        if self.lost:
            return 0
        try:
            return self._h.write(data)
        except Exception:
            self._mark_lost()
            return 0

    def read(self, n):
        if self.lost:
            time.sleep(0.2)
            return []
        try:
            return self._h.read(n)
        except Exception:
            self._mark_lost()
            return []

    def set_nonblocking(self, flag):
        try:
            self._h.set_nonblocking(flag)
        except Exception:
            pass

    def close(self):
        try:
            self._h.close()
        except Exception:
            pass

    def _mark_lost(self):
        if not self.lost:
            self.lost = True
            print("  device  unplugged? waiting for it to come back", flush=True)

    def status(self):
        return {"connected": not self.lost, "seen": self.seen,
                "error": self.last_error}

    def diagnosis(self):
        """One line saying why the pad is unusable right now."""
        if not self.lost:
            return ""
        if self.seen:
            why = ("the pad is on USB but opening it is blocked — that's "
                   "macOS Input Monitoring. Re-grant it to whatever runs the "
                   "daemon (Codexpad.app users: REMOVE the old row first — "
                   "every rebuild voids the grant — then re-add and relaunch)")
            if self.last_error:
                why += f" [{self.last_error}]"
            return why
        if self.seen is False:
            return ("the pad isn't on USB — data-capable cable? wired mode? "
                    "(hold the front-left touch key 3s, tap until the "
                    "underglow turns white, and quit the ChatGPT app)")
        return "still probing USB for the pad…"

    def _try_open(self):
        try:
            self.seen = any(d["vendor_id"] == VID and d["product_id"] == PID
                            for d in hid.enumerate())
        except Exception:
            self.seen = None
        try:
            fresh = hid.device()
            fresh.open(VID, PID)
            fresh.set_nonblocking(True)
        except Exception as exc:
            self.last_error = str(exc)
            return None
        self.last_error = ""
        return fresh

    def release(self):
        """Let go of the pad entirely (handoff): close the handle so the
        vendor client gets it clean. watch() won't reopen while paused."""
        if not self.lost:
            self.close()
            self._h = None
            self.lost = True
            print("  device  released to the vendor client", flush=True)

    def reconnect_now(self):
        """One immediate open attempt (resume shouldn't wait for watch())."""
        if not self.lost:
            return True
        fresh = self._try_open()
        if fresh is None:
            return False
        self.close()
        self._h = fresh
        self.lost = False
        print("  device  connected", flush=True)
        return True

    def watch(self):
        announced = False
        while True:
            if _paused[0]:              # handed off: leave the pad alone
                time.sleep(2)
                continue
            if not self.lost:
                announced = False
                time.sleep(2)
                continue
            if not self.reconnect_now():
                if not announced:
                    print("  device  waiting for the Codex Micro: "
                          + self.diagnosis(), flush=True)
                    announced = True
                time.sleep(2)
                continue
            announced = False
            with _lock:
                lit = [(s, st) for s, st in _slot_state.items()
                       if st and st != "off"]
            blank_all(self)             # clear whatever the pad was showing
            for slot, state in lit:
                set_slot(self, slot, state)


def _pad_error(handle):
    """None if the pad is writable, else an error dict saying exactly why.

    Lighting commands used to be absorbed silently while the pad was away —
    the panel's buttons "worked" and nothing lit. Now they come back with the
    live diagnosis instead.
    """
    if isinstance(handle, Device) and handle.lost:
        return {"error": "pad not connected: " + handle.diagnosis()}
    return None


def open_device():
    handle = hid.device()
    try:
        handle.open(VID, PID)
    except OSError:
        sys.exit(
            "Could not open the Codex Micro.\n"
            "  Most likely cause on macOS: Input Monitoring. In System Settings\n"
            "  > Privacy & Security > Input Monitoring, enable BOTH your\n"
            "  TERMINAL app AND any 'python' entry (a failed attempt adds one\n"
            "  toggled OFF; if neither appears, add them with '+'. This python\n"
            "  is: " + sys.executable + ").\n"
            "  Then FULLY quit and relaunch the terminal (Cmd+Q) - grants only\n"
            "  apply to new processes. sudo works as a stopgap.\n"
            "  To tell the causes apart, run: python tools/probe.py enumerate\n"
            "  - device listed  -> it's the permission above, nothing is "
            "'holding' it\n"
            "  - device missing -> wired mode: hold the front-left touch "
            "control 3s,\n"
            "    tap past the three BLE channels until the underglow turns "
            "white;\n"
            "    charge-only USB-C cables also cause this. And quit the "
            "ChatGPT app."
        )
    return handle


def blank_all(handle):
    for i in range(NSLOTS):
        set_slot(handle, i, "off")


# --- main ------------------------------------------------------------------
def handle_request(handle, req):
    """Apply one socket message. Returns a reply dict (sent for cmd messages).

    Two message shapes share the socket: hook updates from notify.py
    ({"state": ..., "cwd": ...}) and admin commands from the app or nc
    ({"cmd": "reload" | "preview" | "rainbow" | "off" | "ping"}).
    """
    if "cmd" in req:
        cmd = req["cmd"]
        if cmd in ("preview", "rainbow", "off", "trim", "ring",
                   "zone") and _paused[0]:
            return {"error": "pad is handed to Codex right now — quit the "
                             "ChatGPT app (auto-handoff) or click Take pad "
                             "back first"}
        if cmd in ("preview", "rainbow", "off", "trim", "ring", "zone",
                   "resume"):
            pad_err = _pad_error(handle)
            if pad_err and not (cmd == "resume" and _paused[0]):
                return pad_err
        if cmd == "ping":
            pass
        elif cmd == "status":
            with _lock:
                by_slot = {s: c for c, s in _slots.items()}
                return {"ok": 1, "trim": _trim[0], "paused": _paused[0],
                        "device": (handle.status() if isinstance(handle, Device)
                                   else {"connected": True, "seen": True,
                                         "error": ""}),
                        "stats": dict(_stats),
                        "mic": {"open": _mic["open"], "latched": _mic["latched"]},
                        "slots": [{"slot": i,
                                   "state": _slot_state.get(i) or "off",
                                   "cwd": by_slot.get(i)}
                                  for i in range(NSLOTS)]}
        elif cmd == "wait_event":
            after = int(req.get("after", -1))
            if after < 0 or after > _event_seq[0]:
                # negative: only future events. Ahead of us: the client's
                # cursor is from a previous daemon life — reset it, or its
                # events would stall until the count caught up again.
                after = _event_seq[0]
            deadline = time.time() + min(float(req.get("timeout", 20)), 25)
            with _event_cond:
                while _event_seq[0] <= after:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    _event_cond.wait(remaining)
                newer = [e for e in _event_log if e[0] > after]
            return {"ok": 1, "seq": newer[-1][0] if newer else after,
                    "events": [n for _, n in newer]}
        elif cmd == "trim":
            with _lock:
                _trim[0] = min(1.0, max(0.1, round(float(req.get("value", 1.0)), 2)))
                lit = [(s, st) for s, st in _slot_state.items()
                       if st and st != "off"]
            print(f"  trim    {int(_trim[0] * 100)}% (app)", flush=True)
            for slot, state in lit:
                set_slot(handle, slot, state)
        elif cmd == "pause":
            # hand the pad to the vendor client: blank our lights, then let
            # go of the device entirely so ChatGPT gets it clean — while
            # still tracking session states so resume can repaint them
            with _lock:
                snapshot = dict(_slot_state)
            blank_all(handle)
            with _lock:
                _slot_state.clear()
                _slot_state.update(snapshot)
                _paused[0] = True
            if isinstance(handle, Device):
                handle.release()
            try:                      # survive a daemon restart mid-handoff
                open(PAUSE_FLAG, "w").close()
            except OSError:
                pass
            print("  pause   pad handed to the vendor client", flush=True)
        elif cmd == "resume":
            with _lock:
                _paused[0] = False
                lit = [(s, st) for s, st in _slot_state.items()
                       if st and st != "off"]
            try:
                os.unlink(PAUSE_FLAG)
            except OSError:
                pass
            if isinstance(handle, Device):
                handle.reconnect_now()   # don't wait for watch()'s next tick
            blank_all(handle)            # clear the vendor client's leftovers
            for slot, state in lit:
                set_slot(handle, slot, state)
            print("  resume  pad is ours again", flush=True)
        elif cmd == "reload":
            load_config()
            with _lock:
                lit = [(s, st) for s, st in _slot_state.items()
                       if st and st != "off" and st in STATES]
            for slot, state in lit:
                set_slot(handle, slot, state)   # repaint with the new palette
            print("  reload  config applied", flush=True)
        elif cmd == "preview":
            slot = int(req.get("slot", 0))
            _rpc(handle, "v.oai.thstatus",
                 [{"id": slot, "c": config.color_int(req.get("color", "FF00FF"))}])
            frame = {"id": slot, "e": int(req.get("effect", 1)),
                     "b": round(float(req.get("brightness", 1.0)) * _trim[0], 2)}
            # undocumented per-key fields, passed through verbatim so
            # tools/probe.py sweeps can test what they do on real hardware
            for extra in ("s", "m", "sk", "sa"):
                if extra in req:
                    frame[extra] = req[extra]
            _rpc(handle, "v.oai.thstatus", [frame])
            print(f"  preview slot={slot}", flush=True)
        elif cmd == "zone":
            # raw rgbcfg passthrough for zone probing ('ambient' is verified,
            # 'keys' is the unexplored one). Colour first, rest after, same
            # split as the mic ring uses.
            zone = str(req.get("zone", "ambient"))
            fields = {k: v for k, v in (req.get("fields") or {}).items()
                      if k in ("c", "e", "b", "s", "m")}
            if isinstance(fields.get("c"), str):
                fields["c"] = config.color_int(fields["c"])
            if "c" in fields:
                _rpc(handle, "v.oai.rgbcfg", {zone: {"c": fields.pop("c")}})
            if fields:
                _rpc(handle, "v.oai.rgbcfg", {zone: fields})
            print(f"  zone    {zone} <- {req.get('fields')}", flush=True)
        elif cmd == "rainbow":
            for i in range(NSLOTS):
                set_slot(handle, i, "rainbow")
            print("  rainbow on all six -- press the dial to end the party",
                  flush=True)
        elif cmd == "off":
            blank_all(handle)
            print("  off     all keys", flush=True)
        elif cmd == "ring":
            if req.get("on"):
                set_ring(handle, True,
                         color=config.color_int(req["color"])
                         if req.get("color") else None)
            else:
                set_ring(handle, False)
            print(f"  ring    {'on' if req.get('on') else 'off'}", flush=True)
        else:
            return {"error": f"unknown cmd {cmd!r}"}
        return {"ok": 1}

    state = req.get("state", "idle")
    cwd = req.get("cwd", "unknown")
    if state == "pulse":
        # PreToolUse heartbeat: a short shimmer on the session's key shows
        # Claude actively doing things (vs idle thinking). Never allocates a
        # slot -- unknown sessions are ignored.
        with _lock:
            slot = _slots.get(cwd)
        if slot is not None and not _paused[0]:
            pulse(handle, slot)
        return {"ok": 1}
    if state == "end":
        _note_transition(cwd, "end")
        slot = release(cwd)
        if slot is not None:
            if _paused[0]:
                with _lock:
                    _slot_state[slot] = "off"
            else:
                set_slot(handle, slot, "off")
            print(f"  {'end':<7} slot={slot} {cwd}", flush=True)
    elif state in STATES:
        with _lock:
            is_new = cwd not in _slots
        _note_transition(cwd, state)
        if is_new:
            _stats["sessions"] += 1
        if state == "done":
            _stats["turns"] += 1
        elif state == "error":
            _stats["errors"] += 1
        slot = slot_for(cwd)
        if _paused[0]:
            with _lock:
                _slot_state[slot] = state   # track silently; resume repaints
        else:
            set_slot(handle, slot, state)
        print(f"  {state:<7} slot={slot} {cwd}", flush=True)
    else:
        print(f"  ?? unknown state {state!r}", flush=True)
        return {"error": f"unknown state {state!r}"}
    return {"ok": 1}


def _client(handle, conn):
    """One socket client, on its own thread.

    A thread per connection (with a recv timeout) means a stuck or idle
    client can never wedge the daemon — and wait_event long-polls can block
    here without holding anyone else up.
    """
    try:
        conn.settimeout(3.0)
        raw = conn.recv(8192).decode()
        req = json.loads(raw) if raw.strip() else {}
        reply = handle_request(handle, req)
        if "cmd" in req:                # notify.py never reads; the app does
            reply["v"] = VERSION        # lets the app spot a stale daemon
            try:
                conn.send(json.dumps(reply).encode())
            except Exception:
                pass
    except Exception as exc:
        print(f"  err {exc}", flush=True)
    finally:
        conn.close()


def serve(handle):
    if os.path.exists(SOCK_PATH):
        try:
            os.unlink(SOCK_PATH)
        except PermissionError:
            sys.exit(
                f"Stale socket {SOCK_PATH} is owned by another user - "
                f"left over from a sudo run.\n"
                f"Remove it first: sudo rm -f {SOCK_PATH}"
            )
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o777)   # hooks may run as a different uid than the daemon
    srv.listen(16)

    handle.set_nonblocking(True)
    threading.Thread(target=reader, args=(handle,), daemon=True).start()
    threading.Thread(target=nagger, args=(handle,), daemon=True).start()
    if isinstance(handle, Device):
        threading.Thread(target=handle.watch, daemon=True).start()
    print(f"codexpad ready on {SOCK_PATH} "
          f"({NSLOTS} agent keys, config {config.CONFIG_PATH})", flush=True)
    if isinstance(handle, Device) and handle.lost:
        print("  (socket is live before the pad is: the panel can already "
              "see status and say what's wrong)", flush=True)

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_client, args=(handle, conn),
                         daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="Codex Micro lighting daemon")
    ap.add_argument("--test", action="store_true",
                    help="cycle key 0 through every state, then exit")
    ap.add_argument("--off", action="store_true",
                    help="turn all keys off, then exit")
    ap.add_argument("--wait", action="store_true",
                    help="serve immediately and keep watching for the device "
                         "instead of exiting when it can't be opened (used "
                         "by the login service)")
    args = ap.parse_args()

    if args.off or args.test:
        handle = open_device()          # one-shots need the pad now
        if args.off:
            blank_all(handle)
            handle.close()
            return
        try:
            for state in ("idle", "working", "blocked", "done", "error"):
                print(f"slot 0 -> {state}", flush=True)
                set_slot(handle, 0, state)
                time.sleep(2.5)
        finally:
            # also on ctrl-c mid-cycle: never leave the test key lit
            set_slot(handle, 0, "off")
            handle.close()
        return

    _paused[0] = os.path.exists(PAUSE_FLAG)   # a restart forgets nothing
    if _paused[0]:
        print("  pause   still in effect from before the restart", flush=True)

    if args.wait:
        # Socket first, device whenever it shows up: while the pad is in BLE
        # mode or blocked by a missing grant, the panel still reaches us and
        # relays the diagnosis instead of showing a dead daemon.
        handle = Device()
    else:
        handle = Device(open_device())  # manual runs still fail fast, loudly
        if not _paused[0]:
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
