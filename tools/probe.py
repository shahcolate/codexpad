#!/usr/bin/env python3
"""codexpad probe - inspect the Codex Micro vendor HID interface.

Reproduces the observations documented in PROTOCOL.md. Use it to verify the
protocol against your own device before trusting anything in that document.

    python tools/probe.py enumerate       list HID collections for the device
    python tools/probe.py listen          print decoded notifications live
    python tools/probe.py version         send sys.version and print the reply
    python tools/probe.py call <method> [json_params]
    python tools/probe.py color <slot> <hex>    e.g. color 0 00FF00

On macOS you may need Input Monitoring, or sudo, to open the device.
"""
import json
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
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
