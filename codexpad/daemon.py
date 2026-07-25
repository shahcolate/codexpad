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
    python -m codexpad.daemon            # run the daemon
    python -m codexpad.daemon --test     # cycle key 0 through every state and exit
    python -m codexpad.daemon --off      # turn all keys off and exit
    python -m codexpad.daemon --restore  # RESCUE: relight every zone and exit
    python -m codexpad.app               # colours & bindings UI (separate process)

Colours, effects and command bindings come from ~/.codexpad.json (see
codexpad/config.py for the defaults). See PROTOCOL.md for the wire format.
"""
import argparse
import json
import os
import signal
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
    ZONE_BASELINE.clear()
    ZONE_BASELINE.update({name: config.zone_fields(spec)
                          for name, spec in (cfg.get("zones") or {}).items()
                          if isinstance(spec, dict)})
    RING_OFF_RESTORES[0] = cfg.get("ring_off_restores", True) is not False
    AUTO_HANDOFF[0] = cfg.get("auto_handoff", True) is not False
    return cfg


APPROVE_FROM_PAD = [False]    # checkmark/cross answer the focused prompt
NAG_MINUTES = [10]            # ring lights after this long blocked; 0 = off
AUTO_HANDOFF = [True]         # release the pad while the vendor client runs
RING_OFF_RESTORES = [True]    # ring "off" = baseline, not brightness zero

# --- zones: the part that outlives us ----------------------------------------
# v.oai.thstatus is per-thread STATUS -- transient, and any host repaints it.
# v.oai.rgbcfg is zone CONFIGURATION, and what we write to a zone stays
# written: it survives our process, a reboot, and a switch to the vendor
# client. Writing {"e": 0, "b": 0} to a zone therefore does not mean "stop
# showing my thing", it means "configure this zone dark for everyone" -- and
# no vendor UI puts it back. That is how a pad ends up looking bricked: the
# ambient ring (which is also the BLE-blue / wired-white mode tell) and the
# key backlight stay off no matter who drives the device.
#
# So codexpad never releases a zone by zeroing it. It releases a zone by
# writing ZONE_BASELINE, and it remembers which zones it has touched so it
# can put them back on pause, on handoff, and on the way out.
ZONE_BASELINE = {}            # zone -> wire fields, from config["zones"]
_zones_touched = set()


load_config()

_seq = [0]
_trim = [1.0]                 # global brightness trim, dial-adjustable 0.1-1.0
_stick = [None]               # flick currently held, so one push fires once
_paused = [False]             # True: the vendor client owns the pad
_paused_by = [""]             # who paused: see AUTO_REASONS
_paused_at = [0.0]            # when, so a handoff can be given time to land

# Why the pad was handed over, and what that implies for taking it back.
#   "manual" — a person clicked Hand pad to Codex. Never auto-resumed.
#   "auto"   — the watcher saw the vendor client running. Resumed the moment
#              it isn't.
#   "key"    — the ✦ Codex key was pressed. Also self-healing, but the app
#              usually isn't up YET: the press is what makes you go open it.
#              Reclaiming on the next 3s tick would undo the handoff before
#              ChatGPT finished launching, so this one gets a grace window.
AUTO_REASONS = ("auto", "key")
KEY_HANDOFF_GRACE_S = 90
_lock = threading.RLock()     # serialises HID writes and the slot tables

# Pause must survive a daemon restart: the login app supervises the daemon
# with a restart loop, and a respawn that forgot it was paused would repaint
# Claude states all over the vendor client mid-handoff.
#
# But a pause that survives too well is its own outage -- a paused daemon is
# a daemon where nothing works, silently. Two things used to make it stick:
# the flag is written by a ROOT daemon (the login app sudo-runs it) so a
# later user-owned daemon's os.unlink raised EPERM into a bare `except: pass`
# and "resume" reported success while the flag stayed on disk; and an AUTO
# pause had no expiry, so one that outlived its ChatGPT session sat there
# until someone found the file. Both are handled below.
PAUSE_FLAG = SOCK_PATH + ".paused"
AUTO_PAUSE_MAX_S = 6 * 3600   # an auto-handoff older than this is stale


def write_pause_flag(reason):
    """Record the pause with its provenance and age. World-writable, so a
    daemon under a different uid can still clear it later."""
    try:
        with open(PAUSE_FLAG, "w") as fh:
            json.dump({"by": reason, "at": time.time(), "pid": os.getpid()}, fh)
        os.chmod(PAUSE_FLAG, 0o666)
    except OSError as exc:
        print(f"  pause   could not persist the flag: {exc}", flush=True)


def clear_pause_flag():
    """Remove the flag, and say so if we couldn't.

    Falls back to truncating: unlink needs write permission on /tmp's entry
    (root's file, sticky directory), but the 0o666 file itself can always be
    emptied. read_pause_flag() treats an empty file as 'not paused'.
    """
    try:
        os.unlink(PAUSE_FLAG)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass
    try:
        with open(PAUSE_FLAG, "w"):
            pass
        return True
    except OSError as exc:
        print(f"  resume  WARNING: the pause flag {PAUSE_FLAG} is not ours to "
              f"clear ({exc}) — the next restart would come up paused. "
              f"Remove it by hand: sudo rm -f {PAUSE_FLAG}", flush=True)
        return False


def read_pause_flag():
    """(paused, reason) from the flag, resolving stale auto-pauses to False."""
    try:
        with open(PAUSE_FLAG) as fh:
            raw = fh.read().strip()
    except OSError:
        return False, ""
    if not raw:                       # truncated by a clear we couldn't unlink
        return False, ""
    try:
        info = json.loads(raw)
        reason = str(info.get("by") or "manual")
        age = time.time() - float(info.get("at") or 0)
    except (ValueError, TypeError):
        return True, raw or "manual"  # pre-0.6 flag: a bare reason string
    if reason in AUTO_REASONS and age > AUTO_PAUSE_MAX_S:
        print(f"  pause   ignoring a stale auto-handoff flag "
              f"({int(age / 3600)}h old) — taking the pad back", flush=True)
        clear_pause_flag()
        return False, ""
    return True, reason

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


# Pacing between consecutive frames. The device is happy to be written to
# back-to-back over USB, but a partial update that arrives out of order looks
# like a dropped field, and this has never been characterised. Tests set it
# to 0.
WRITE_GAP = [0.02]


def _encode(method, seq, params):
    msg = {"m": method, "id": seq}
    if params is not None:
        msg["p"] = params
    return json.dumps(msg, separators=(",", ":")).encode() + b"\r\n"


def _num(value):
    """Shortest faithful encoding of a lighting number.

    Worth two bytes, and two bytes decide whether a frame is sent at all.
    json.dumps writes 1.0 as "1.0" but 1 as "1", and a full-brightness
    thstatus update with a two-digit request id lands at exactly 61 bytes --
    the limit -- so the float form pushed it to 62 and the device never saw
    it. Symptom: the key took its new colour and kept its old brightness,
    which from 'off' means it silently stayed dark.
    """
    try:
        value = round(float(value), 2)
    except (TypeError, ValueError):
        return value
    return int(value) if value == int(value) else value


def _rpc(handle, method, params=None):
    """Send one JSON-RPC frame. Fire and forget; replies are never awaited.

    Lighting calls are idempotent and their acks carry nothing we need, so the
    reader thread drops them (see reader()).
    """
    _seq[0] = (_seq[0] % 90) + 1
    body = _encode(method, _seq[0], params)
    if len(body) > MAX_BODY:
        # Never silently. An over-long frame is simply not transmitted (§2.1),
        # which is indistinguishable from the pad ignoring us -- and lighting
        # frames sit within a byte or two of the limit, so this fired in
        # normal use for years' worth of brightness values without a word.
        print(f"  DROPPED {method} — body {len(body)}B over the {MAX_BODY}B "
              f"frame limit: {body[:48]!r}…", flush=True)
        return False
    frame = bytes([REPORT_ID, CHANNEL, len(body)]) + body
    with _lock:
        handle.write(frame.ljust(REPORT_LEN, b"\x00"))
        if WRITE_GAP[0]:
            time.sleep(WRITE_GAP[0])
    return True


def _send_fields(handle, method, wrap, fields):
    """Send a partial lighting update, packed into as few frames as fit.

    §2.1 gives us 61 bytes and §5.1 gives us partial updates, so the right
    strategy is to measure rather than guess: pack greedily, and start a new
    frame the moment one more field would go over. Colour is emitted first
    and usually alone -- an 8-digit colour is most of a frame by itself.

    Guessing was the old strategy (two fields per frame, always) and it was
    wrong by one or two bytes exactly where it mattered most.
    """
    fields = {k: (_num(v) if k in ("b", "s") else v)
              for k, v in fields.items() if v is not None}
    if not fields:
        return True
    ordered = ([("c", fields.pop("c"))] if "c" in fields else []) \
        + sorted(fields.items())
    ok, batch = True, {}
    for key, value in ordered:
        candidate = dict(batch, **{key: value})
        # +2 bytes of headroom: the request id grows from one digit to two
        # as the sequence wraps, and a frame that fits at id 9 must still fit
        # at id 90.
        if batch and len(_encode(method, _seq[0], wrap(candidate))) + 2 > MAX_BODY:
            ok &= _rpc(handle, method, wrap(batch))
            batch = {key: value}
        else:
            batch = candidate
    if batch:
        ok &= _rpc(handle, method, wrap(batch))
    return ok


# Software rainbow: firmware effect 3 renders solid red on the Agent Keys
# (PROTOCOL.md §5.2), so the party is built from confirmed effects instead --
# one hue per key, spread across the spectrum.
RAINBOW_HUES = [0xFF0000, 0xFF8800, 0xFFEE00, 0x00E020, 0x0066FF, 0xC400FF]


def thread_write(handle, slot, fields):
    """Partial per-key lighting update, split to fit the frame limit."""
    return _send_fields(handle, "v.oai.thstatus",
                        lambda f: [dict(f, id=slot)], fields)


def set_slot(handle, slot, state):
    """Apply a named state to one Agent Key.

    Split across frames: a full ThreadLighting object with an 8-digit decimal
    colour exceeds the 61-byte body limit. Partial updates are legal (omitted
    fields are left unchanged on the device), so this is safe -- as long as
    every piece actually goes out, which is what _send_fields guarantees and
    the old fixed two-frame split did not.
    """
    color, effect, brightness, speed = STATES[state]
    if state == "rainbow":
        color = RAINBOW_HUES[slot % len(RAINBOW_HUES)]
    with _lock:
        _slot_state[slot] = state
        fields = {"c": color, "e": effect, "b": brightness * _trim[0]}
        if speed and effect not in (0, 1):
            # the animation switch, hardware-discovered: without `s` breath
            # renders solid and rainbow renders red (PROTOCOL.md §5.2). It
            # needs no frame of its own -- thread_write measures and packs,
            # so `s` rides with whatever else fits.
            fields["s"] = speed
        thread_write(handle, slot, fields)


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


_focus_cycle = [0]


def _cycle_focus(direction):
    """Stick session navigation: emit a FOCUS for the next/previous owned
    session — the panel raises its window. The pad becomes a session dial."""
    with _lock:
        owned = sorted(_slots.items(), key=lambda kv: kv[1])   # (cwd, slot)
    if not owned:
        return
    _focus_cycle[0] = (_focus_cycle[0] + direction) % len(owned)
    cwd, slot = owned[_focus_cycle[0]]
    emit_event({"t": "FOCUS", "cwd": cwd,
                "state": _slot_state.get(slot) or "idle"})
    print(f"  stick   focus -> slot {slot} {cwd}", flush=True)


def flick(a, d):
    """Quantise stick deflection into one bindable flick per push.

    The stick streams v.oai.rad continuously while deflected, so this fires
    once when deflection crosses 0.7 and re-arms only after it falls below
    0.3 -- the hysteresis stops a wobbling hold from machine-gunning events.
    East/west default to session navigation unless the user bound them.
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
        elif name == "STICK_E":
            _cycle_focus(+1)
        elif name == "STICK_W":
            _cycle_focus(-1)


def zone_write(handle, zone, fields):
    """Write one rgbcfg zone, split so no frame exceeds the 61-byte body.

    Colour goes alone (an 8-digit colour plus the zone name is already within
    a byte of the limit), then the rest two fields at a time. Records the zone
    as touched so restore_zones() knows to put it back.
    """
    fields = dict(fields)
    if not fields:
        return
    _zones_touched.add(zone)
    with _lock:
        return _send_fields(handle, "v.oai.rgbcfg",
                            lambda f: {zone: f}, fields)


def restore_zones(handle, zones=None, force=False):
    """Hand zones back the way we promised to leave them.

    This is the un-brick: it rewrites ZONE_BASELINE (lit, solid, full
    brightness by default) over whatever codexpad last configured. By default
    it only touches zones we actually wrote to; `force` covers every zone in
    the baseline, which is what the rescue paths want -- they run precisely
    because some *earlier* process left a zone dark.
    """
    names = zones if zones is not None else sorted(
        ZONE_BASELINE if force else _zones_touched)
    done = []
    for zone in names:
        fields = ZONE_BASELINE.get(zone)
        if not fields:
            continue
        zone_write(handle, zone, fields)
        _zones_touched.discard(zone)
        done.append(zone)
    if done:
        print(f"  zones   restored to baseline: {', '.join(done)}", flush=True)
    return done


def set_ring(handle, on, color=None):
    """Light the ambient ring (mic indicator, nag light, MCP callers).

    Single-zone partial updates, split across two frames to fit the 61-byte
    body, fire and forget. This path is what confirmed the ambient zone on
    hardware (PROTOCOL.md §5.3) -- but only for c/e/b at solid, and replies
    are never read. If the ring stays dark the mic events still fire; probe
    the method directly and report what it returns.

    Turning it OFF restores the zone baseline rather than writing brightness
    zero. rgbcfg is configuration and outlives us: a zeroed ring stays dark
    for the vendor client too, and the ring is the pad's own transport
    indicator, so zeroing it also destroys the only tell for which bus the
    pad is on. Set "ring_off_restores": false to get the old behaviour back.
    """
    if on:
        zone_write(handle, "ambient",
                   {"c": MIC_COLOR[0] if color is None else color,
                    "e": 1, "b": 1})
    elif RING_OFF_RESTORES[0]:
        restore_zones(handle, ["ambient"])
    else:
        zone_write(handle, "ambient", {"e": 0, "b": 0})


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
        _, _, brightness, _ = STATES[state]
        thread_write(handle, slot,
                     {"b": max(0.15, brightness * _trim[0] * 0.3)})

    def restore():
        with _lock:
            if _slot_state.get(slot) == "working" and not _paused[0]:
                thread_write(handle, slot, {"b": brightness * _trim[0]})
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
    elif key == "ACT12" and key not in COMMANDS:
        # the Codex key does the obvious: hand the pad to Codex. Self-healing
        # — the watcher takes the pad back when ChatGPT isn't running
        # (accidental press included) — but tagged "key" rather than "auto",
        # because when you press this the app is usually not up YET. An
        # "auto" pause is reclaimed on the next 3s tick, which would undo the
        # handoff while ChatGPT was still launching; "key" gets a grace
        # window first. One-way from the pad by nature: once released, the
        # daemon can't hear keys.
        print("  press   ACT12 (hand pad to Codex)", flush=True)
        handle_request(handle, {"cmd": "pause", "by": "key"})
        return
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
            # Only repaint what's ours. This used to blank all six first,
            # which meant every replug wiped whatever else had the pad lit.
            for slot, state in lit:
                set_slot(self, slot, state)


# --- sharing the pad with the vendor client ----------------------------------
# Auto-handoff used to live entirely in the control panel, which made it an
# optional feature of an optional process: run the daemon on its own -- the
# root wrapper, service.sh, the README's own `sudo python -m codexpad.daemon`
# -- and nothing ever released the pad. The daemon held the handle and
# repainted over the vendor client indefinitely. The process that OWNS the
# device is the one that has to know how to let go of it, so the watcher
# lives here now. The panel's copy is harmless and idempotent; whichever
# notices first wins.
VENDOR_APPS = ("ChatGPT",)


def vendor_client_running():
    """Is the vendor's app up? None when we can't tell (no pgrep, not macOS)."""
    if sys.platform != "darwin":
        return None
    for name in VENDOR_APPS:
        try:
            proc = subprocess.run(["pgrep", "-x", name], capture_output=True)
        except (OSError, ValueError):
            return None
        if proc.returncode == 0:
            return True
    return False


def take_back(handle, why):
    """Undo an auto-handoff and repaint our sessions."""
    with _lock:
        _paused[0] = False
        _paused_by[0] = ""
        _paused_at[0] = 0.0
        lit = [(s, st) for s, st in _slot_state.items()
               if st and st != "off"]
    clear_pause_flag()
    if isinstance(handle, Device):
        handle.reconnect_now()
    for slot, state in lit:
        set_slot(handle, slot, state)
    print(f"  resume  {why} — pad reclaimed", flush=True)


def vendor_watcher(handle):
    while True:
        time.sleep(3)
        try:
            auto_paused = _paused[0] and _paused_by[0] in AUTO_REASONS
            if not AUTO_HANDOFF[0]:
                # Turning auto-handoff off while it was in effect used to
                # strand the daemon: the only code that could undo an auto
                # pause bailed out one line earlier than the undo.
                if auto_paused:
                    take_back(handle, "auto-handoff switched off")
                continue
            running = vendor_client_running()
            if running is None:
                continue
            if running and not _paused[0]:
                hand_over(handle, "auto")
            elif not running and auto_paused:
                if (_paused_by[0] == "key"
                        and time.time() - _paused_at[0] < KEY_HANDOFF_GRACE_S):
                    continue    # you just pressed ✦; give ChatGPT time to open
                take_back(handle, "vendor client quit")
        except Exception as exc:
            print(f"  handoff err {exc}", flush=True)


def shutdown(handle, blank=True):
    """Leave the pad the way we found it, as far as we're able.

    Called on SIGTERM and SIGHUP, and on Ctrl-C via the KeyboardInterrupt
    path. SIGTERM is the one that matters: it is how the login app's
    supervisor, codexpad-stop and every pkill in the shell scripts actually
    stop this process, and until now none of them got a clean exit. The
    daemon simply died holding whatever it had last configured, so a stop
    left the ring dark and the keys frozen on stale session states -- for the
    vendor client as much as for us.
    """
    try:
        if blank:
            blank_owned(handle)
        restore_zones(handle)
        if isinstance(handle, Device):
            handle.release()
        else:
            handle.close()
    except Exception:
        pass
    try:
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
    except OSError:
        pass


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
    """Every Agent Key off. An explicit user action only (--off, the Off
    button, the dial): it overwrites whatever the vendor client had lit."""
    for i in range(NSLOTS):
        set_slot(handle, i, "off")


def blank_owned(handle, everything=False):
    """Blank only the keys codexpad has actually painted.

    The difference matters at start-up and on every USB reconnect. Blanking
    all six there was codexpad announcing itself by wiping the pad -- if the
    vendor client had keys lit, they went dark and stayed dark. With no
    sessions of our own there is nothing to clear, so we touch nothing.
    """
    with _lock:
        slots = (range(NSLOTS) if everything
                 else [s for s, st in _slot_state.items() if st and st != "off"])
    for slot in slots:
        set_slot(handle, slot, "off")
    return list(slots)


def hand_over(handle, reason):
    """Give the pad to the vendor client: clear our keys, put every zone we
    touched back to baseline, then let go of the device entirely.

    Order matters. The zone restore has to go out while we still hold the
    handle, or the ring stays however codexpad last configured it for the
    whole time the vendor client owns the pad -- which is exactly the state
    that reads as a dead pad.
    """
    with _lock:
        snapshot = dict(_slot_state)
    blank_owned(handle)
    restore_zones(handle)
    with _lock:
        _slot_state.clear()
        _slot_state.update(snapshot)   # keep tracking; resume repaints
        _paused[0] = True
        _paused_by[0] = reason
        _paused_at[0] = time.time()
    _nag_on[0] = False
    if isinstance(handle, Device):
        handle.release()
    write_pause_flag(reason)
    print(f"  pause   pad handed to the vendor client ({reason})", flush=True)


def rescue():
    """Put the pad's lighting back, from a cold start, with nothing running.

    This is the path out of the state this whole mechanism exists to prevent:
    a zone left configured dark, so the pad looks dead under EVERY host --
    codexpad, the vendor client, anything. It has to work when the daemon is
    wedged, when it's holding the device, and when it isn't running at all,
    so it tries the socket first and falls back to opening the pad directly.

    It does not start a daemon and it does not stay resident.
    """
    print("codexpad rescue — relighting every zone and clearing stuck state\n")
    cleared = clear_pause_flag()
    print(f"  pause flag  {'cleared' if cleared else 'COULD NOT CLEAR'} "
          f"({PAUSE_FLAG})")

    if os.path.exists(SOCK_PATH):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(4.0)
            sock.connect(SOCK_PATH)
            # a paused daemon still holds no device; resume first so the
            # restore has something to write through
            sock.send(json.dumps({"cmd": "resume"}).encode())
            sock.recv(8192)
            sock.close()
            reply = _rescue_via_socket()
            if reply is not None and "error" not in reply:
                print(f"  zones       restored via the running daemon: "
                      f"{', '.join(reply.get('zones') or []) or 'none'}")
                print("\nIf the pad is still dark, quit the vendor app, "
                      "unplug and replug the pad, and run this again.")
                return
            if reply is not None:
                print(f"  daemon      said: {reply['error']}")
        except OSError as exc:
            print(f"  daemon      not usable ({exc}) — opening the pad direct")

    print("  device      opening directly…")
    try:
        handle = hid.device()
        handle.open(VID, PID)
    except Exception as exc:
        sys.exit(
            f"  FAILED: {exc}\n\n"
            "  The rescue needs to reach the pad. In order:\n"
            "    1. Quit the ChatGPT app — it may be holding the device.\n"
            "    2. Make sure the pad is in WIRED mode (hold the front-left\n"
            "       touch control 3s and tap through the channels). If the\n"
            "       ring is dark you cannot read the mode off it any more —\n"
            "       check `python tools/probe.py enumerate` for a [USB] row.\n"
            "    3. macOS Input Monitoring, or re-run this under sudo:\n"
            f"         sudo {sys.executable} -m codexpad.daemon --restore")
    handle.set_nonblocking(True)
    restore_zones(handle, force=True)
    for i in range(NSLOTS):
        set_slot(handle, i, "off")
    handle.close()
    print("\n  done. Every zone in your config's \"zones\" table is lit again\n"
          "  and all six Agent Keys are cleared. Open the ChatGPT app and\n"
          "  check the pad; if the ring is still dark, power-cycle the pad\n"
          "  (the firmware reasserts its own ring colour on boot) and rerun.")


def _rescue_via_socket():
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(6.0)
        sock.connect(SOCK_PATH)
        sock.send(json.dumps({"cmd": "restore"}).encode())
        raw = sock.recv(8192).decode()
        sock.close()
        return json.loads(raw) if raw.strip() else None
    except (OSError, ValueError):
        return None


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
                        "paused_by": _paused_by[0],
                        "zones_touched": sorted(_zones_touched),
                        "vendor_running": vendor_client_running(),
                        # tells the panel to keep its hands off the handoff:
                        # this daemon runs the watcher itself, and only it
                        # knows about the ✦-key grace window
                        "handoff_owner": "daemon",
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
            reason = req.get("by") or ("auto" if req.get("auto") else "manual")
            hand_over(handle, reason if reason in AUTO_REASONS else "manual")
        elif cmd == "resume":
            take_back(handle, "asked for the pad back")
        elif cmd == "restore":
            # The un-brick: put every zone in the baseline back, whether or
            # not THIS process is the one that darkened it. A paused daemon
            # has let go of the device, so take it back for the write —
            # rescuing the lighting is worth interrupting a handoff for, and
            # the vendor watcher hands it straight back on the next tick.
            if _paused[0]:
                with _lock:
                    _paused[0] = False
                    _paused_by[0] = ""
                clear_pause_flag()
                if isinstance(handle, Device):
                    handle.reconnect_now()
            pad_err = _pad_error(handle)
            if pad_err:
                return pad_err
            done = restore_zones(handle, force=True)
            if req.get("keys") is not False:
                blank_owned(handle, everything=True)
            return {"ok": 1, "zones": done}
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
            # undocumented per-key fields ride along verbatim so
            # tools/probe.py sweeps can test them on real hardware;
            # thread_write packs everything into as many frames as it takes
            extras = {k: req[k] for k in ("s", "m", "sk", "sa") if k in req}
            thread_write(handle, slot, dict(
                extras,
                c=config.color_int(req.get("color", "FF00FF")),
                e=int(req.get("effect", 1)),
                b=float(req.get("brightness", 1.0)) * _trim[0]))
            print(f"  preview slot={slot}", flush=True)
        elif cmd == "zone":
            # Raw rgbcfg passthrough for zone probing ('ambient' is verified,
            # 'keys' is the unexplored one). zone_write splits the frames and,
            # importantly, records the zone as touched — a probe sweep that
            # leaves a zone somewhere odd is then something shutdown() and
            # `restore` know to undo.
            zone = str(req.get("zone", "ambient"))
            fields = {k: v for k, v in (req.get("fields") or {}).items()
                      if k in ("c", "e", "b", "s", "m")}
            if isinstance(fields.get("c"), str):
                fields["c"] = config.color_int(fields["c"])
            zone_write(handle, zone, fields)
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
    threading.Thread(target=vendor_watcher, args=(handle,),
                     daemon=True).start()
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
    ap.add_argument("--restore", action="store_true",
                    help="rescue: put every lighting zone back to a lit "
                         "baseline, clear all six keys and any stuck pause, "
                         "then exit. Use when the pad's lights are dead "
                         "everywhere, including in the vendor app.")
    args = ap.parse_args()

    if args.restore:
        return rescue()

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

    _paused[0], _paused_by[0] = read_pause_flag()   # a restart forgets nothing
    _paused_at[0] = time.time()   # unknown age; the flag's own expiry governs
    if _paused[0]:
        print(f"  pause   still in effect from before the restart "
              f"({_paused_by[0]}) — the pad belongs to the vendor client "
              f"until it quits, or until you click Take pad back", flush=True)

    if args.wait:
        # Socket first, device whenever it shows up: while the pad is in BLE
        # mode or blocked by a missing grant, the panel still reaches us and
        # relays the diagnosis instead of showing a dead daemon.
        handle = Device()
    else:
        handle = Device(open_device())  # manual runs still fail fast, loudly

    # Starting up is not a reason to touch the pad. This used to blank all
    # six keys on every launch -- and the login app relaunches the daemon
    # whenever it dies -- so codexpad repeatedly wiped lighting it had never
    # set. We only own a key once a session claims it.

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: (shutdown(handle), os._exit(0)))
        except (ValueError, OSError):
            pass          # not the main thread / platform without it
    try:
        serve(handle)
    except KeyboardInterrupt:
        shutdown(handle)
        print("\nstopped")


if __name__ == "__main__":
    main()
