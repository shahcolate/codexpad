# Raw captures

Frames observed from a Codex Micro on firmware `v0.4.1`, macOS, wired mode.
These are the evidence base for [`PROTOCOL.md`](../PROTOCOL.md). Reproduce with
`python tools/probe.py listen`.

---

## Agent Key press and release

`AG00` pressed, then released:

```
06 02 2c 7b 22 6d 22 3a 22 76 2e 6f 61 69 2e 68 69 64 22 2c 22 70 22 3a 7b 22
6b 22 3a 22 41 47 30 30 22 2c 22 61 63 74 22 3a 31 7d 7d 0d 0a 00 00 b0 5d 0a
82 30 ba cc 3f 1b 00 00 00 00 00 00

06 02 2c 7b 22 6d 22 3a 22 76 2e 6f 61 69 2e 68 69 64 22 2c 22 70 22 3a 7b 22
6b 22 3a 22 41 47 30 30 22 2c 22 61 63 74 22 3a 30 7d 7d 0d 0a 00 00 b0 5d 0a
82 30 ba cc 3f 1e 00 00 00 00 00 00
```

Decoded:

```json
{"m":"v.oai.hid","p":{"k":"AG00","act":1}}
{"m":"v.oai.hid","p":{"k":"AG00","act":0}}
```

Length check: 42 JSON characters + CRLF = 44 = `0x2c`. ✓

Note the bytes after `0d 0a`. They differ between two otherwise identical
frames, and are not zeroed — see PROTOCOL.md §2.3 and §7.

---

## Dial

Clockwise, counter-clockwise, and press:

```json
{"m":"v.oai.hid","p":{"k":"ENC_CW","act":2}}
{"m":"v.oai.hid","p":{"k":"ENC_CC","act":2}}
{"m":"v.oai.hid","p":{"k":"ENC_CLK","act":1}}
{"m":"v.oai.hid","p":{"k":"ENC_CLK","act":0}}
```

Detents emit a single `act: 2` with no matching release. The dial press behaves
like a key, with `1` then `0`.

---

## Analog stick

One deflection and recentre:

```json
{"m":"v.oai.rad","p":{"a":0.805284,"d":0.015105}}
{"m":"v.oai.rad","p":{"a":0.758271,"d":1}}
{"m":"v.oai.rad","p":{"a":0.75789,"d":0.988791}}
{"m":"v.oai.rad","p":{"a":0,"d":0}}
```

`a` is angle and `d` is deflection, both normalised 0–1. The stream terminates
with `a: 0, d: 0` when the stick returns to centre. Full analog resolution is
available; the four cardinal directions in the vendor UI are a host-side
interpretation.

---

## RPC round trip

`sys.version`, showing that a reply longer than one report is chunked:

```
tx: {"m":"sys.version","id":1}

rx report 1 body: {"result":{"version":"v0.4.1"},"id":1,"method":"sys.version"
rx report 2 body: }
```

Unknown method:

```
tx: {"m":"sys.status","id":1}
rx: {"error":{"code":404,"message":"Method not found"},"id":1,"method":"sys.status"}
```

---

## Envelope key

The identifier key is `id`, not `i`. Sending `i` is accepted without error, but
the reply echoes `null`, showing the device ignored the unrecognised key rather
than rejecting the frame:

```
tx: {"m":"sys.version","i":1}
rx: {"result":{"version":"v0.4.1"},"id":null,"method":"sys.version"}

tx: {"m":"sys.version","id":1}
rx: {"result":{"version":"v0.4.1"},"id":1,"method":"sys.version"}
```

---

## Lighting

Setting thread 0 to `0x0000FF` with effect `1` (solid):

```
tx: {"m":"v.oai.thstatus","id":2,"p":[{"id":0,"c":255,"e":1}]}
rx: {"result":{"ok":1},"id":2,"method":"v.oai.thstatus"}
```

**The key rendered blue**, confirming `c` is packed `0xRRGGBB` with no byte swap.

### Size limit

A fully-populated thread object does not fit one report:

```
{"m":"v.oai.thstatus","id":1,"p":[{"id":0,"c":16744192,"b":1,"e":1,"s":1,"sk":1,"sa":1}]}
```

79 bytes against the 61-byte body budget, rejected before transmission. Splitting
across two partial updates works, since omitted fields are left unchanged.
