# Codex Micro Vendor HID Protocol

Interoperability notes for the **Work Louder × OpenAI `kbd-1.0-codex-micro`**, derived from
observation of the author's own device.

- **Device:** Codex Micro (Work Louder Creator Micro 2 chassis)
- **Firmware observed:** `v0.4.1`
- **Host observed:** macOS, Apple Silicon
- **Documented:** 2026-07-24

> **Scope and provenance.** Everything below describes observable behaviour of the
> hardware: bytes sent to it, bytes received from it, and the resulting device state.
> Each claim carries a verification status (see [Verification status](#verification-status)).
> Nothing in this document is derived by copying vendor source code, and no vendor
> code is reproduced here.
>
> This is unofficial. It is not endorsed by Work Louder or OpenAI, and firmware
> updates may invalidate any of it.

---

## 1. Transport

| Property | Value |
|---|---|
| USB Vendor ID | `0x303a` (Espressif) |
| USB Product ID | `0x8360` |
| Manufacturer string | `Work Louder` |
| Product string | `Codex Micro` |
| USB interfaces | 1 (`interface_number = 0`) |
| HID collections | 6 |

The `0x303a` vendor ID indicates an Espressif (ESP32-family) microcontroller. The device
does **not** expose a QMK Raw HID interface (`usage_page 0xFF60`, `usage 0x61`), so QMK
and VIA tooling do not apply.

### 1.1 HID collections

| Usage page | Usage | Purpose |
|---|---|---|
| `0x0001` | `0x0001` | Generic Desktop — Pointer |
| `0x0001` | `0x0002` | Mouse |
| `0x0001` | `0x0005` | Game Pad (analog stick) |
| `0x0001` | `0x0006` | Keyboard |
| `0x000c` | `0x0001` | Consumer Control |
| `0xff00` | `0x0001` | **Vendor-defined — the protocol below** |

All six report the same interface and, on macOS, the same device path. Opening any one
of them yields the vendor traffic; filter on report ID rather than selecting a collection.

### 1.2 Connection mode

The device supports Bluetooth LE and USB. **In BLE mode, connecting USB charges the
device but does not enumerate it** — the protocol is unavailable until wired mode is
selected using the front-left touch control.

### 1.3 Host permissions (macOS)

Because the device exposes a Keyboard collection, macOS gates opening it behind
**Input Monitoring**. Enumeration succeeds without the grant; `open()` fails with
`open failed` until it is given (or the process runs as root).

---

## 2. Frame format

Every frame is a 64-byte HID report.

```
offset  size  field
------  ----  --------------------------------------------------
     0     1  Report ID           always 0x06
     1     1  Channel / class     always 0x02 in all observed traffic
     2     1  Body length         payload length, CRLF included
     3     N  Body                UTF-8 JSON, terminated by \r\n
   3+N     -  Padding             remainder of the 64-byte report
```

The body is JSON-RPC 2.0 with abbreviated envelope keys.

**Worked example** — Agent Key 0 pressed:

```
06 02 2c 7b 22 6d 22 3a 22 76 2e 6f 61 69 2e 68 69 64 22 2c 22 70 22 3a
7b 22 6b 22 3a 22 41 47 30 30 22 2c 22 61 63 74 22 3a 31 7d 7d 0d 0a ...
```

- `0x06` report ID, `0x02` channel, `0x2c` = 44 byte body
- Body: `{"m":"v.oai.hid","p":{"k":"AG00","act":1}}` (42 chars) + `\r\n` = 44 ✓

### 2.1 Maximum body size

The body must fit the remaining 61 bytes of the report. Requests exceeding this are not
transmitted. In practice this is restrictive: a single lighting update carrying `id`,
`c`, `b`, `e`, `s`, `sk` and `sa` with a 8-digit decimal colour requires ~79 bytes.

Two mitigations exist:

1. **Partial updates.** Omitted lighting fields are left unchanged on the device, so a
   large update can be split across successive frames.
2. **Chunking.** Responses are observed to split across multiple reports (§2.2). A
   corresponding outbound mechanism is presumed to exist but has not been characterised.

### 2.2 Response chunking

Responses longer than one report are split across consecutive reports, each with its own
length byte, and must be concatenated before parsing. Observed split:

```
report 1 body: {"result":{"version":"v0.4.1"},"id":1,"method":"sys.version"
report 2 body: }
```

A reader should accumulate bodies and attempt to parse after each report, treating a
successful parse as the frame boundary. Note that unsolicited notifications share report
ID `0x06`, so a naive accumulator can be corrupted by a keypress arriving mid-response.

### 2.3 Padding

Bytes after the terminating CRLF are not zeroed by the device. Observed padding varies
between otherwise identical frames and contains values consistent with uninitialised
memory. See [Notes for the vendor](#notes-for-the-vendor).

Host-originated frames should zero-pad; the device accepts `0x00` padding.

---

## 3. Message envelope

### 3.1 Request (host → device)

```json
{"m": "<method>", "id": <number>, "p": <params>}
```

| Key | Meaning |
|---|---|
| `m` | Method name |
| `id` | Request identifier, echoed in the response |
| `p` | Parameters; omit entirely for methods that take none |

**`id` is the correct key.** Sending `i` instead is accepted without error but the
response returns `"id": null` — the device ignores the unrecognised key rather than
rejecting the frame. This is a silent failure mode worth guarding against.

### 3.2 Response (device → host)

Success:

```json
{"result": {"version": "v0.4.1"}, "id": 1, "method": "sys.version"}
```

Error:

```json
{"error": {"code": 404, "message": "Method not found"}, "id": 1, "method": "sys.status"}
```

The `method` echo is convenient for correlation. Unknown methods return `404`, which
makes the device a reliable oracle for probing method names.

### 3.3 Notification (device → host)

Notifications use the same framing but carry no `id` and expect no response.

---

## 4. Notifications

### 4.1 `v.oai.hid` — discrete controls

```json
{"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
```

| Field | Meaning |
|---|---|
| `k` | Control identifier |
| `act` | `1` = press, `0` = release, `2` = momentary/tick |
| `ag` | Agent index (present in the vendor's own client type; not yet observed on the wire) |

Control identifiers observed:

| `k` | Control | `act` pattern |
|---|---|---|
| `AG00` … `AG05` | Agent Keys, zero-indexed left to right | `1` then `0` |
| `ACT06` … `ACT12` | Command Keys | press observed; release pattern not recorded |
| `ENC_CW` | Dial, clockwise detent | `2` per detent |
| `ENC_CC` | Dial, counter-clockwise detent | `2` per detent |
| `ENC_CLK` | Dial press | `1` then `0` |

All six Agent Key identifiers have now been exercised individually. The seven Command
Keys emit `ACT06`–`ACT12`, continuing the Agent Keys' zero-based index space at 6; all
seven identifiers were observed as press notifications, but which physical key carries
which identifier was not recorded, and the release pattern (presumed `1` then `0` like
the other keys) was not captured. See `captures/notifications.md`.

The front-left touch control produces no notification — layer cycling appears to be
handled entirely in firmware.

### 4.2 `v.oai.rad` — analog stick

```json
{"m": "v.oai.rad", "p": {"a": 0.805284, "d": 0.015105}}
```

| Field | Meaning |
|---|---|
| `a` | Angle, normalised `0.0`–`1.0` |
| `d` | Deflection from centre, normalised `0.0`–`1.0` |

Emitted continuously while deflected, terminating with `{"a": 0, "d": 0}` on recentre.
Full analog resolution is available; the four cardinal directions exposed in the ChatGPT
UI are a host-side interpretation, not a device limitation.

---

## 5. Methods

### 5.1 `v.oai.thstatus` — per-thread lighting

Sets lighting for the Agent Keys. Params are an **array**, one object per thread.

```json
{"m": "v.oai.thstatus", "id": 2, "p": [{"id": 0, "c": 255, "e": 1}]}
```

Response: `{"result": {"ok": 1}, "id": 2, "method": "v.oai.thstatus"}`

| Field | Type | Meaning |
|---|---|---|
| `id` | int | Thread index. Required. Note this key is **not** abbreviated. |
| `c` | int | Packed RGB colour |
| `b` | float | Brightness, `0.0` (off) – `1.0` (full) |
| `e` | int | Effect (§5.2) |
| `s` | float | Effect speed, `0.0` (stopped) – `1.0` (fast) |
| `sk` | int | `1`/`0` — key backlight follows this thread's colour |
| `sa` | int | `1`/`0` — ambient ring follows this thread's colour |

All fields except `id` are optional; omitted fields leave that aspect unchanged.

**Colour encoding is `0xRRGGBB`.** Verified: `{"c": 255}` (`0x0000FF`) renders **blue**,
confirming no byte swap.

### 5.2 Lighting effects

| Value | Effect |
|---|---|
| `0` | Off |
| `1` | Solid |
| `2` | Snake — a lit segment travels the strip |
| `3` | Rainbow — cycles the hue spectrum |
| `4` | Breath — fades in and out |
| `5` | Gradient |
| `6` | Shallow breath — as breath, but floors at half brightness |

Effect `1` (solid) is confirmed on hardware; the remaining values are documented from the
vendor client's enumeration and have not each been individually exercised.

### 5.3 `v.oai.rgbcfg` — zone lighting

Configures the two lighting zones: `ambient` (outer ring) and `keys` (under-keycap
backlight). Each zone takes `e` (effect), `b` (brightness), `s` (speed), `c` (colour) and
`m` (an additional effect parameter of undetermined meaning).

Not yet exercised on hardware. Given the 61-byte body limit, a two-zone update is
unlikely to fit in a single frame.

### 5.4 Other methods

Present on the device; names confirmed but behaviour not characterised:

| Method | Apparent purpose |
|---|---|
| `sys.version` | Firmware version. **Verified**, returns `{"version": "v0.4.1"}` |
| `sys.selftest` | Device self-test |
| `sys.bootloader` | Enter bootloader — **likely destructive to the running session; untested** |
| `host.focused_app` | Host reports the frontmost application. Presumed to drive layer auto-switching |
| `fs.list` `fs.read` `fs.readbin` `fs.write` `fs.writebin` `fs.delete` `fs.rmdir` | On-device filesystem |
| `fs.txbegin` `fs.txcommit` | Transactional file writes |
| `mp.write_info` | Undetermined |

Unknown methods return `404`, so this list can be extended safely by probing.

---

## 6. Verification status

| Claim | Status |
|---|---|
| VID/PID, collections, transport | Verified |
| Frame layout, length semantics | Verified |
| `id` envelope key; `i` silently ignored | Verified |
| Response format, `404` on unknown method | Verified |
| Response chunking | Verified |
| 61-byte body limit | Verified |
| `v.oai.hid` schema, `AG00`–`AG05`/`ENC_*` | Verified |
| `ACT06`–`ACT12` Command Key identifiers | Observed; physical mapping and release pattern not recorded |
| `v.oai.rad` schema and range | Verified |
| `v.oai.thstatus` accepted, returns `{"ok":1}` | Verified |
| Colour is `0xRRGGBB` | Verified |
| Effect `1` = solid | Verified |
| `sys.version` | Verified |
| Effects `0`, `2`–`6` | Documented, not individually tested |
| `b`, `s`, `sk`, `sa` field behaviour | Documented, not individually tested |
| `AG02`–`AG05` identifiers | Inferred from pattern |
| `v.oai.rgbcfg` | Not tested |
| `fs.*`, `sys.*`, `host.*`, `mp.*` behaviour | Names only |
| Outbound chunking mechanism | Unknown |

---

## 7. Notes for the vendor

Two observations offered constructively, reported to Work Louder prior to publication:

1. **Uninitialised padding.** Bytes following the CRLF terminator in device-originated
   frames are not zeroed and vary between otherwise identical frames, in a pattern
   consistent with uninitialised memory being emitted over USB. Zeroing the report buffer
   before populating it would resolve this.

2. **Silent envelope-key mismatch.** A request using an unrecognised identifier key is
   processed and answered with `"id": null` rather than rejected. Returning a
   `-32600 Invalid Request` would make client bugs fail loudly instead of silently.

---

## 8. Compatibility

Written against firmware `v0.4.1`. The device ships with a documented bootloader path and
the vendor tooling includes firmware update machinery, so field names, limits, and method
availability may change without notice. Re-verify against your own device before relying
on any of this.
