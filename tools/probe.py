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
                                                (via the daemon socket - the
                                                daemon keeps the device, no
                                                sudo needed; watch the pad and
                                                note what each id really does)

On macOS you may need Input Monitoring, or sudo, to open the device
(the `effects` sweep is the exception: it only needs the daemon running).
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


def cmd_effects(slot="0"):
    slot = int(slot)
    try:
        pong = ask_daemon({"cmd": "ping"})
    except OSError as exc:
        sys.exit(f"daemon not reachable on {SOCK_PATH} ({exc}) - start it first")
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
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
