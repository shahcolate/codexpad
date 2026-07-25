#!/usr/bin/env python3
"""codexpad probe - inspect the Codex Micro vendor HID interface.

Reproduces the observations documented in PROTOCOL.md. Use it to verify the
protocol against your own device before trusting anything in that document.

    python tools/probe.py enumerate       list HID collections for the device
    python tools/probe.py listen          print decoded notifications live
    python tools/probe.py version         send sys.version and print the reply
    python tools/probe.py call <method> [json_params]
    python tools/probe.py color <slot> <hex>    e.g. color 0 00FF00
    python tools/probe.py effects [slot]        cycle effect ids 0-9 on a key
    python tools/probe.py speeds [slot]         do s/m make the dead effects
                                                (snake/rainbow/gradient) move?
    python tools/probe.py ring                  effect sweep on the ambient ring
    python tools/probe.py keys                  first probe of the unexplored
                                                'keys' zone - watch the pad!
    python tools/probe.py ledmap                guided LED mapping: id sweep
                                                0-15 + slow snakes per zone

The four sweeps go through the RUNNING DAEMON's socket - the daemon keeps
the device, no sudo needed. Everything else opens the device directly, which
on macOS may need Input Monitoring, or sudo.
"""
import json
import os
import socket
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("hidapi not installed. Run: pip install hidapi")

VID = 0x303A
PID = 0x8360
REPORT_ID = 0x06
CHANNEL = 0x02
REPORT_LEN = 64
MAX_BODY = REPORT_LEN - 3

_seq = [0]


def frame(msg):
    body = json.dumps(msg, separators=(",", ":")).encode() + b"\r\n"
    if len(body) > MAX_BODY:
        raise ValueError(f"body {len(body)}B exceeds {MAX_BODY}B limit")
    return (bytes([REPORT_ID, CHANNEL, len(body)]) + body).ljust(REPORT_LEN, b"\x00")


def open_device():
    handle = hid.device()
    handle.open(VID, PID)
    return handle


def decode(report):
    """Extract the JSON body from a 64-byte report, or None."""
    if not report or report[0] != REPORT_ID:
        return None
    length = report[2]
    return bytes(report[3:3 + length]).decode("utf-8", "replace")


BUS = {0: "unknown", 1: "USB", 2: "BLUETOOTH", 3: "I2C", 4: "SPI"}


def cmd_enumerate():
    rows = [d for d in hid.enumerate() if (d["vendor_id"], d["product_id"]) == (VID, PID)]
    if not rows:
        print("Codex Micro not found.")
        print("Is it in WIRED mode? In BLE mode, USB charges but does not enumerate.")
        return
    buses = set()
    for d in rows:
        bus = BUS.get(d.get("bus_type", 0), str(d.get("bus_type")))
        buses.add(bus)
        print(f"{d['vendor_id']:#06x}:{d['product_id']:#06x} "
              f"page={d['usage_page']:#06x} usage={d['usage']:#06x} "
              f"iface={d['interface_number']} :: {d['product_string']} [{bus}]")
    if "BLUETOOTH" in buses:
        print()
        print("!! The pad is connected over BLUETOOTH, not USB. codexpad needs")
        print("!! wired mode: quit the ChatGPT app, Forget/disconnect the pad in")
        print("!! Bluetooth settings (or turn Bluetooth off), power-cycle the pad,")
        print("!! then hold the touch control 3s and tap until the ring is WHITE.")


def cmd_listen():
    handle = open_device()
    handle.set_nonblocking(True)
    print("listening - press keys, turn the dial, move the stick. ctrl-c to stop.")
    try:
        while True:
            body = decode(handle.read(REPORT_LEN))
            if body:
                stamp = time.strftime("%H:%M:%S")
                try:
                    print(stamp, json.dumps(json.loads(body.strip())))
                except json.JSONDecodeError:
                    print(stamp, body.strip())
            time.sleep(0.005)
    except KeyboardInterrupt:
        handle.close()


def call(handle, method, params=None, timeout=1.5):
    """Send an RPC call and reassemble the chunked reply."""
    _seq[0] = (_seq[0] % 90) + 1
    msg = {"m": method, "id": _seq[0]}
    if params is not None:
        msg["p"] = params
    handle.write(frame(msg))

    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        body = decode(handle.read(REPORT_LEN))
        if body:
            buf += body
            try:
                return json.loads(buf.strip())
            except json.JSONDecodeError:
                pass          # response split across reports; keep accumulating
        time.sleep(0.005)
    return {"error": "timeout", "partial": buf}


def cmd_version():
    handle = open_device()
    handle.set_nonblocking(True)
    print(json.dumps(call(handle, "sys.version"), indent=2))
    handle.close()


def cmd_call(method, params_json=None):
    params = json.loads(params_json) if params_json else None
    handle = open_device()
    handle.set_nonblocking(True)
    print(json.dumps(call(handle, method, params), indent=2))
    handle.close()


def cmd_color(slot, hex_color):
    color = int(hex_color.lstrip("#"), 16)
    handle = open_device()
    handle.set_nonblocking(True)
    print(json.dumps(call(handle, "v.oai.thstatus", [{"id": int(slot), "c": color}])))
    print(json.dumps(call(handle, "v.oai.thstatus", [{"id": int(slot), "e": 1, "b": 1}])))
    handle.close()


SOCK_PATH = os.environ.get("CODEXPAD_SOCK", "/tmp/codexpad.sock")

# What our current effect map claims each id is. The sweep exists precisely
# because some of these are wrong on hardware (reported: 3 shows red, 2 and 5
# do nothing) - run it, write down what each id actually looks like.
CLAIMED = {0: "off", 1: "solid", 2: "snake", 3: "rainbow", 4: "breath",
           5: "gradient", 6: "shallow breath", 7: "?", 8: "?", 9: "?"}


def ask_daemon(payload):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect(SOCK_PATH)
    s.send(json.dumps(payload).encode())
    reply = json.loads(s.recv(8192).decode())
    s.close()
    return reply


def _need_daemon():
    try:
        return ask_daemon({"cmd": "ping"})
    except OSError as exc:
        sys.exit(f"daemon not reachable on {SOCK_PATH} ({exc}) - start it first")


def cmd_speeds(slot="0"):
    """Do the dead effects come alive with the undocumented s (speed?) or m
    fields? Field report says even breath renders solid per-key, so every
    non-solid effect is on trial. 4s per combo, watch the key."""
    slot = int(slot)
    pong = _need_daemon()
    print(f"daemon {pong.get('v', '?')} - testing s/m on the dead effects, AG0{slot}.")
    print("Watch the key. Note ANY difference from a plain solid/red/nothing.\n")
    for eff, name in ((4, "breath"), (2, "snake"), (3, "rainbow"),
                      (5, "gradient")):
        for extra in ({"s": 1}, {"s": 5}, {"s": 0.2}, {"m": 1}, {"s": 3, "m": 2}):
            r = ask_daemon({"cmd": "preview", "slot": slot, "effect": eff,
                            "brightness": 1.0, "color": "00A0FF", **extra})
            if "error" in r:
                sys.exit(f"daemon said: {r['error']}")
            print(f"  effect {eff} ({name:8s}) + {extra}")
            time.sleep(4)
    ask_daemon({"cmd": "preview", "slot": slot, "effect": 0,
                "brightness": 0, "color": "000000"})
    print("\ndone - key cleared. Report which combos (if any) animated.")


def cmd_ringfx(_slot=None):
    """Sweep effects on the AMBIENT ring (only solid is verified there)."""
    pong = _need_daemon()
    print(f"daemon {pong.get('v', '?')} - sweeping ring effects. Watch the "
          "glow around the pad.\n")
    for eff in range(7):
        r = ask_daemon({"cmd": "zone", "zone": "ambient",
                        "fields": {"c": "00A0FF", "e": eff, "b": 1}})
        if "error" in r:
            sys.exit(f"daemon said: {r['error']}")
        print(f"  ambient e={eff} ({CLAIMED.get(eff, '?')})")
        time.sleep(4)
    for s in (1, 5):
        ask_daemon({"cmd": "zone", "zone": "ambient",
                    "fields": {"c": "00A0FF", "e": 2, "b": 1, "s": s}})
        print(f"  ambient e=2 (snake) + s={s}")
        time.sleep(4)
    ask_daemon({"cmd": "zone", "zone": "ambient", "fields": {"e": 0, "b": 0}})
    print("\ndone - ring cleared. Report what each id did.")


def cmd_keyszone(_slot=None):
    """First-ever probe of the 'keys' zone (under-keycap backlight?).

    The zone name comes from the vendor client's schema and has never been
    exercised on hardware. Watch the WHOLE pad - especially the five keys
    codexpad can't paint (lightning/check/cross/fork/star)."""
    pong = _need_daemon()
    print(f"daemon {pong.get('v', '?')} - poking the unexplored 'keys' zone.")
    print("Watch the whole pad, especially the Command Keys.\n")
    combos = [{"c": "00FF00", "e": 1, "b": 1},
              {"c": "FF00FF", "e": 1, "b": 0.5},
              {"c": "00A0FF", "e": 4, "b": 1},
              {"c": "FFFF00", "e": 2, "b": 1, "s": 3}]
    for fields in combos:
        r = ask_daemon({"cmd": "zone", "zone": "keys", "fields": fields})
        if "error" in r:
            sys.exit(f"daemon said: {r['error']}")
        print(f"  keys <- {fields}")
        time.sleep(4)
    ask_daemon({"cmd": "zone", "zone": "keys", "fields": {"e": 0, "b": 0}})
    print("\ndone (sent keys off too). Report anything that lit, blinked or "
          "changed - or 'nothing at all', which is also an answer.")


def cmd_ledmap(_=None):
    """Map the pad's LEDs the way the inputs were mapped: one at a time.

    Phase A: does thstatus accept ids beyond the six Agent Keys? Each id
    0-15 lights green for 3s - note which PHYSICAL light answers.
    Phase B/C: a slow snake on the keys zone / ambient ring visits every
    LED in chain order - watch the crawl and the map draws itself.
    """
    pong = _need_daemon()
    print(f"daemon {pong.get('v', '?')} - LED mapping session, three phases.\n")
    print("PHASE A  thstatus id sweep 0-15, GREEN, 3s each. ids 0-5 should be")
    print("the six Agent Keys; anything lighting beyond id 5 is a discovery.")
    input("Enter to start...")
    for i in range(16):
        ask_daemon({"cmd": "preview", "slot": i, "color": "00FF00",
                    "effect": 1, "brightness": 1.0})
        print(f"  id {i:2d}  <- which light came on (if any)?")
        time.sleep(3)
        ask_daemon({"cmd": "preview", "slot": i, "color": "000000",
                    "effect": 0, "brightness": 0})
    print("\nPHASE B  slow snake on the KEYS zone, ~15s. Watch which LEDs it")
    print("visits and in what order - that order is the LED chain.")
    input("Enter to start...")
    ask_daemon({"cmd": "zone", "zone": "keys",
                "fields": {"c": "00A0FF", "e": 2, "b": 1, "s": 0.3}})
    time.sleep(15)
    ask_daemon({"cmd": "zone", "zone": "keys", "fields": {"e": 0, "b": 0}})
    print("\nPHASE C  slow snake on the AMBIENT ring, ~12s. Count the segments.")
    input("Enter to start...")
    ask_daemon({"cmd": "zone", "zone": "ambient",
                "fields": {"c": "00A0FF", "e": 2, "b": 1, "s": 0.3}})
    time.sleep(12)
    ask_daemon({"cmd": "zone", "zone": "ambient", "fields": {"e": 0, "b": 0}})
    print("\ndone. Report: phase A id -> light table, phase B crawl order,")
    print("phase C segment count. That's the whole LED map.")


def cmd_effects(slot="0"):
    slot = int(slot)
    pong = _need_daemon()
    print(f"daemon {pong.get('v', '?')} - sweeping effect ids on AG0{slot}.")
    print("Watch that key. For each id, note what you SEE (solid? moving?")
    print("breathing? colour cycling? nothing?). 4 seconds each, white first,")
    print("then blue - some effects may ignore or transform the colour.\n")
    for eff in range(10):
        for hexcol, name in ((0xFFFFFF, "white"), (0x0066FF, "blue")):
            r = ask_daemon({"cmd": "preview", "slot": slot, "effect": eff,
                            "brightness": 1.0, "color": f"{hexcol:06X}"})
            if "error" in r:
                sys.exit(f"daemon said: {r['error']}")
            print(f"  effect {eff} ({name:5s})  our map says: {CLAIMED[eff]}")
            time.sleep(4)
    ask_daemon({"cmd": "preview", "slot": slot, "effect": 0,
                "brightness": 0, "color": "000000"})
    print("\ndone - key cleared. Tell us what each id really did, and we'll")
    print("fix the effect map in codexpad/config.py and PROTOCOL.md.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "enumerate":
        cmd_enumerate()
    elif cmd == "listen":
        cmd_listen()
    elif cmd == "version":
        cmd_version()
    elif cmd == "call":
        cmd_call(*rest)
    elif cmd == "color":
        cmd_color(*rest)
    elif cmd == "effects":
        cmd_effects(*rest)
    elif cmd == "speeds":
        cmd_speeds(*rest)
    elif cmd == "ring":
        cmd_ringfx(*rest)
    elif cmd == "keys":
        cmd_keyszone(*rest)
    elif cmd == "ledmap":
        cmd_ledmap(*rest)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
