# codexpad

**Drive OpenAI's Codex Micro macropad from Claude Code.**

The Codex Micro's six frosted Agent Keys light up to show what your Codex chats
are doing. This makes them do the same for Claude Code sessions: blue while
Claude works, amber when it's blocked on your approval, green when a turn
finishes.

Includes [`PROTOCOL.md`](PROTOCOL.md) — documentation of the device's vendor HID
protocol, derived from observing the hardware. As far as I know it's the first
public description of it.

> Unofficial and unaffiliated. Not endorsed by OpenAI or Work Louder.
> Written against firmware `v0.4.1`; a firmware update may break any of it.

---

## What you get

| Claude Code event | Agent Key |
|---|---|
| Session starts | white, dim |
| You submit a prompt | blue, breathing |
| Claude needs approval or input | **amber, breathing** |
| Turn completes | green |
| Turn fails on an API error | red |
| Session ends | off |

Each session gets its own key. Desktop gives every session an isolated git
worktree, so the working directory is a stable per-session identity — which is
what makes the mapping work without any session bookkeeping.

The amber is the one that earns the hardware. Everything else is decoration.

---

## Requirements

- Codex Micro, **in wired mode** (see below)
- macOS or Linux, Python 3.9+
- `pip install hidapi`
- Claude Code CLI or the Desktop app's **Code** tab

### Put the device in wired mode

In BLE mode, plugging in USB charges the device without enumerating it, so the
protocol is unreachable. To switch:

1. Hold the front-left touch control for 3 seconds — underglow turns **blue**
2. Tap to cycle BLE channels 1, 2, 3
3. A fourth tap selects **wired** — underglow turns **white**

Confirm with `python tools/probe.py enumerate`.

---

## Quickstart

```bash
git clone https://github.com/shahcolate/codex-micro-for-claude && cd codex-micro-for-claude
pip install -r requirements.txt

python tools/probe.py enumerate      # device should appear
python -m codexpad.daemon --test     # key 0 cycles through all five states

./install.sh                         # merges hooks into ~/.claude/settings.json
python -m codexpad.daemon            # leave this running
```

Then quit and reopen Claude Code and send a prompt.

`install.sh` writes absolute paths into your settings and backs up the existing
file first. If you prefer to do it by hand, copy
[`hooks/settings.example.json`](hooks/settings.example.json) and replace
`PYTHON` and `/PATH/TO/`.

---

## How it works

```
Claude Code hook  ──stdin JSON──▶  notify.py
                                       │  unix socket
                                       ▼
                                   daemon.py  ──vendor HID──▶  Codex Micro
```

Three deliberate choices:

- **The daemon holds the HID handle.** Opening the device per hook invocation is
  slow and races against itself.
- **`notify.py` never fails.** Every error is swallowed, so a dead daemon or an
  unplugged pad can't break a Claude Code turn.
- **The daemon never reads from the device.** Key-press notifications share
  report ID `0x06` with RPC replies, so a reader here would corrupt response
  reassembly. Lighting calls are idempotent; dropping the ack costs nothing.

Two frames go out per state change, because a fully-populated lighting object
with an 8-digit decimal colour exceeds the 61-byte body limit. Partial updates
are legal, so it's split. See [`PROTOCOL.md`](PROTOCOL.md) §2.1.

---

## Customising

Colours and effects live in `STATES` at the top of `codexpad/daemon.py`:

```python
STATES = {
    "working": (0x0000FF, 4, 1.0),   # (0xRRGGBB, effect, brightness)
    "blocked": (0xFF8000, 6, 1.0),
}
```

Effects: `0` off, `1` solid, `2` snake, `3` rainbow, `4` breath, `5` gradient,
`6` shallow breath. Colour is packed `0xRRGGBB` — verified, no byte swap.

Try a colour without editing anything:

```bash
python tools/probe.py color 0 FF00FF
```

---

## Troubleshooting

**`open failed` on macOS.** The device exposes a keyboard collection, so macOS
gates opening it behind Input Monitoring. Add your terminal in System Settings →
Privacy & Security → Input Monitoring, then **fully quit and relaunch** the
terminal — grants only apply to new processes. `sudo` works as a stopgap.

**Device doesn't enumerate.** It's in BLE mode. See wired mode above. A
charge-only USB-C cable produces identical symptoms.

**Hooks don't fire.** Run `/hooks` inside Claude Code — your events should be
listed with source `User`. If they aren't, the app hasn't reloaded settings;
quit it completely (not just the window) and reopen.

**Hooks fire but nothing lights.** Run with `CODEXPAD_DEBUG=1` and check
`/tmp/codexpad.log`. Test the socket directly:

```bash
printf '{"state":"blocked","cwd":"/tmp/test"}' | nc -U /tmp/codexpad.sock
```

**Desktop app does nothing.** Make sure you're in the **Code** tab, not Chat,
and that the environment selector says **Local**. Cloud and SSH sessions execute
hooks on the remote machine, which can't reach a socket on yours.

**Two sessions share a key.** Only six keys exist; a seventh session evicts the
least recently used.

---

## Protocol work

[`PROTOCOL.md`](PROTOCOL.md) documents the frame format, the JSON-RPC envelope,
the notification schemas, and the lighting methods, with a verification status
for every claim so you can tell what was confirmed on hardware from what wasn't.

`tools/probe.py` is the instrument used to produce it — `enumerate`, `listen`,
`version`, `call`, and `color` subcommands. Point it at your own device and
check the document.

Two firmware observations were reported to Work Louder before publication and
are noted in §7 of the protocol document.

### On scope

This documents observable device behaviour: bytes sent, bytes received, and the
resulting state of the hardware. No vendor code is included or redistributed
here, and the implementation is independent. The goal is interoperability, not
substitution — if Work Louder or OpenAI ship an official SDK, use that instead.

---

## Roadmap

- [ ] Remove the `sudo` requirement with a documented Input Monitoring setup
- [ ] Bind Command Keys to Claude Code actions (capture their `k` identifiers first)
- [ ] Use the joystick's analog `v.oai.rad` output for session navigation
- [ ] Characterise `v.oai.rgbcfg` for the ambient ring
- [ ] Linux and Windows testing — currently macOS only
- [ ] Reconcile with the vendor's own layer system so Codex and Claude coexist

## License

MIT. See [LICENSE](LICENSE).
