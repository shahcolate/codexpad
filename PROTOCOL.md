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
selected using the front-left touch control. The mode is observed **not to survive
power loss reliably**: after unplugging (the pad flashes blue — BLE advertising on
battery), it can come back in BLE mode and must be switched to wired again.

**An existing BLE bond dominates.** With the pad paired to a host, it returns to BLE
and reconnects — the vendor's ChatGPT client went on driving it over Bluetooth while
the pad was entirely absent from the USB bus. Wired mode holds reliably only after the
Bluetooth pairing is removed on the host.

That makes the transport a practical ownership switch in this configuration, but note
what was *not* tested: whether the ChatGPT client can also drive the pad over USB
(it was only ever observed working over BLE, never denied USB), and whether the device
can serve a USB and a BLE host concurrently. "One host stack at a time" describes every
session observed here; it is not a device capability that was established. Both gaps are
one experiment each — see §6.

**In BLE mode the pad is still an HID device** — HID over GATT, same `0x303a:0x8360`
identity and the same six collections, so `hid.enumerate()` lists it identically on
either transport. Enumeration alone therefore cannot distinguish wired from BLE;
check `bus_type` (hidapi ≥ 0.13, surfaced by `tools/probe.py enumerate`) or the USB
bus itself (`system_profiler SPUSBDataType` on macOS). The vendor-protocol traffic in
this document was captured on USB; behaviour over the BLE HID transport has not been
characterised.

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

All thirteen key identifiers have been exercised individually, and a stated-order pass
(see `captures/notifications.md`) established the physical map. The Command Keys continue
the Agent Keys' zero-based index space at 6. Releases (`act: 0`) are confirmed for the
mic pair `ACT10`/`ACT11` — hold-to-talk closing on release requires them — and presumed
identical for the remaining `ACT` keys.

#### Physical map

Four rows, top to bottom, positions left to right:

| Position | Control | Identifier(s) |
|---|---|---|
| Row 1, far left | Dial — rotate / press | `ENC_CW` `ENC_CC` / `ENC_CLK` |
| Row 1, keys 2–3 | Agent Keys | `AG00` `AG01` |
| Row 1, far right | Analog stick | `v.oai.rad` (§4.2) |
| Row 2, all four keys | Agent Keys | `AG02` `AG03` `AG04` `AG05` |
| Row 3, all four keys | Command Keys ⚡ ✓ ✗ ⑂ | `ACT06` `ACT07` `ACT08` `ACT09` |
| Row 4, far left | Mode touch control | none — emits no notification |
| Row 4, wide key | Mic bar | `ACT10` **and** `ACT11` |
| Row 4, far right | Codex key | `ACT12` |

**The mic bar sits on two switches.** A full press usually emits `ACT10` and `ACT11`
together (ordering not fixed); an off-centre press can emit only one. Hosts should fold
the pair into one logical key.

For interoperability reference, the vendor client's default Command Key actions are:
⚡ toggles Fast mode, ✓ approves the current request, ✗ declines it, ⑂ continues the
chat in a new chat, the mic bar starts push-to-talk, and the Codex key sends the
composer message. These are host-side bindings, not device behaviour.

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

**Orientation.** Directed flicks establish the zero point: `a` is `0.0` with the stick
pushed down (toward the user) and increases **counter-clockwise** — right ≈ `0.25`,
up ≈ `0.5`, left ≈ `0.75`. Observed: up `0.49`, right `0.24`, down `0.93` (slightly
off-axis); a later four-direction sweep clustered at `0.01` / `0.24` / `0.49` /
`0.69`–`0.80`, exercising the left region and completing the circle.

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

**The `s` field is the animation switch.** A three-sweep hardware session
(ids 0–9 with `c`/`e`/`b` only, then the same effects with `s`/`m`, on all
three zones) established the rule this document previously missed: **without
`s`, no effect animates anywhere** — breath renders as plain solid and
rainbow renders as solid red. With `s` riding along (any tested value:
0.2–5), the animated effects run. The vendor client evidently always sends
`s`; every earlier "broken effect" observation was a missing parameter.

Effect ids vs zones, fw v0.4.1, all observed on hardware:

| Value | Effect | Per-key (`thstatus`), with `s` | `ambient` ring | `keys` zone |
|---|---|---|---|---|
| `0` | Off | ✅ | ✅ | ✅ (clears) |
| `1` | Solid | ✅ (no `s` needed) | ✅ (no `s` needed) | ✅ (no `s` needed) |
| `2` | Snake | ❌ dead even with `s` | ✅ **with `s`** (without: lit, malformed) | ✅ with `s` |
| `3` | Rainbow | ✅ cycles hues **with `s`**; solid red without | ✅ | untested |
| `4` | Breath | ✅ breathes **with `s`**; solid without | ❌ without `s` (with `s`: untested) | ❌ without `s` (consistent) |
| `5` | Gradient | ❌ dead even with `s` | ⚠️ lights, spatial-looking, "weird" | untested |
| `6` | Shallow breath | solid without `s`; with `s` presumed like `4`, untested | ❌ without `s` | untested |
| `7`–`9` | (not enumerated) | nothing noteworthy | — | — |

Snake and gradient appear to be **strip effects**: they render on the
multi-LED ambient ring and `keys` zone but never on a single Agent Key,
which is geometrically sensible. `m` alone changed nothing in any test.
Higher `s` is visibly faster (confirmed by eye across 0.2–5); the exact
value-to-rate mapping is unmeasured.
Sweeps run through a live daemon — `python tools/probe.py effects|speeds|
ring|keys` — so any pad owner can extend this table.

### 5.3 `v.oai.rgbcfg` — zone lighting

Configures lighting zones. The zone names `ambient` (outer ring) and `keys` (under-keycap
backlight) and the per-zone fields `e` (effect), `b` (brightness), `s` (speed), `c`
(colour) and `m` (an additional parameter of undetermined meaning) are taken from the
vendor client's schema, not from probing the device; only the subset below was exercised.

Given the 61-byte body limit, a two-zone update cannot fit in a single frame.
**Single-zone partial updates to `ambient` are confirmed on hardware**: codexpad's mic
indicator sends an `{"c": …}` frame followed by `{"e": 1, "b": 1}` (mirroring the §5.1
split) and the ring lights; `{"e": 0, "b": 0}` clears it. Scope of that confirmation:
one zone, the fields `c`/`e`/`b`, effect `1`, and a single colour — the default mic red,
`0xFF0000`. Red rendering red does at least rule out a byte swap on this zone (a BGR
reading would have shown blue). The frames are sent fire-and-forget, so the method's
reply and error behaviour are unknown; the confirmation is visual.

**The `keys` zone is confirmed on hardware** (first known third-party use): it accepts
the same split partial updates with `c`/`e`/`b`/`s` — solid colours at chosen
brightness light it, snake runs on it with `s`, and breath without `s` stays dark,
exactly matching the per-key `s` rule (§5.2). Ambient-ring effect behaviour is also
mapped in §5.2's zone table. The `m` field did nothing in any zone.

**LED addressing limits, from a stated-order mapping session:** `v.oai.thstatus`
accepts exactly ids `0`–`5` (the six Agent Keys) — a full sweep of ids `0`–`15`,
solid green, lit nothing beyond them, so the remaining LEDs are *not* reachable as
thread ids. The `keys` zone is driven only as a whole through `rgbcfg`; its LED
chain order was observed by running a slow snake (`e:2, s:0.3`) across it, which
**starts at the ⑂ (fork / new-chat) key** — remaining crawl order to be recorded.

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

All statuses below are from one device, one host (macOS, Apple Silicon) and firmware
`v0.4.1`. "Verified" means observed directly on that setup — not that it was replicated
across devices or hosts.

| Claim | Status |
|---|---|
| VID/PID, HID collections | Verified |
| BLE mode: USB charges but does not enumerate | Verified |
| An existing BLE bond returns the pad to BLE; wired holds after unpairing | Verified |
| Wired mode does not reliably survive power loss | Verified |
| Identical `hid.enumerate()` identity on BLE (HID over GATT) | Verified |
| Vendor protocol over the BLE HID transport | Not tested — all traffic here is USB |
| Whether the ChatGPT client can drive the pad over **USB** | **Not tested** — it was only ever seen on BLE, never refused USB |
| Whether the pad can serve a USB and a BLE host concurrently | **Not tested** — "one at a time" is observation, not an established limit |
| Frame layout, length semantics | Verified |
| `id` envelope key; `i` silently ignored | Verified |
| Response format, `404` on unknown method | Verified |
| Response chunking | Verified |
| 61-byte body limit | Verified |
| `v.oai.hid` schema, `AG00`–`AG05`/`ENC_*` | Verified |
| `ACT06`–`ACT12` identifiers and physical positions | Verified (stated-order pass); releases confirmed for `ACT10`/`ACT11`, presumed for the rest |
| Mic bar = two switches (`ACT10`+`ACT11`) | Verified across ~10 presses |
| `v.oai.rad` schema and range | Verified |
| `v.oai.rad` orientation (`a=0` down, counter-clockwise) | Verified — directed flicks plus a four-direction sweep |
| `v.oai.thstatus` accepted, returns `{"ok":1}` | Verified |
| Colour is `0xRRGGBB` | Verified |
| Effect `1` = solid | Verified |
| `sys.version` | Verified |
| Effect `0` (off) | Verified on all three zones |
| `s` = the animation enable/speed field | **Verified** — breath and rainbow animate per-key only with `s`; ring snake likewise |
| Effects `4` (breath), `3` (rainbow) per-key | Verified WITH `s`; render solid / solid-red without |
| Effects `2` (snake), `5` (gradient) per-key | Contradicted — dead even with `s`; they run on the ring (strip effects) |
| `keys` zone accepts `c`/`e`/`b`/`s`, split partial updates | **Verified — first known third-party drive of this zone** |
| `m` field | No observed behaviour in any test |
| `b`, `s`, `sk`, `sa` field behaviour | Documented, not individually tested |
| `AG00`–`AG05` physical positions | Verified (stated-order pass, `captures/`) |
| `v.oai.rgbcfg` `ambient`: split partial updates, `c`/`e`/`b`, effect `1` | Verified — mic ring lights and clears; visual only, replies not read |
| `v.oai.rgbcfg` `ambient` colours other than `0xFF0000` | Not tested (red does rule out a byte swap) |
| `v.oai.rgbcfg` zone/field names beyond those exercised | From the vendor client's schema, not probed |
| `v.oai.rgbcfg` `keys` zone, `s`/`m`, non-solid effects | Not tested |
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
