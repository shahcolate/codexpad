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

The keys press back, too: a green or red key acknowledges its session, and the
dial trims brightness — see [The buttons](#the-buttons).

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
python -m codexpad.app               # starts the daemon too — open http://127.0.0.1:8378
```

The app's **Claude Code setup** card walks you through the rest: it checks
device, daemon and hooks, and the **⚙️ Install hooks** button does the settings
merge for you. Then fully quit and reopen Claude Code and send a prompt.

Prefer the terminal? The pieces run separately (`--no-daemon` stops the app
auto-starting one):

```bash
python tools/probe.py enumerate      # device should appear
python -m codexpad.daemon --test     # key 0 cycles through all five states
./install.sh                         # merges hooks into ~/.claude/settings.json
python -m codexpad.daemon            # leave this running
```

`install.sh` writes absolute paths into your settings and backs up the existing
file first. If you prefer to do it by hand, copy
[`hooks/settings.example.json`](hooks/settings.example.json) and replace
`PYTHON` and `/PATH/TO/`. After every `git pull`, restart the daemon (the app
does this check for you and offers a **▶ Start daemon** button).

---

## How it works

```
Claude Code hook  ──stdin JSON──▶  notify.py
                                       │  unix socket
                                       ▼
                                   daemon.py  ──lighting──▶  Codex Micro
                                       ▲                         │
                                       └── key / dial presses ───┘
```

Three deliberate choices:

- **The daemon holds the HID handle.** Opening the device per hook invocation is
  slow and races against itself.
- **`notify.py` never fails.** Every error is swallowed, so a dead daemon or an
  unplugged pad can't break a Claude Code turn.
- **The daemon never reassembles replies.** Notifications and RPC replies
  share report ID `0x06`, which is what makes naive reading dangerous
  ([`PROTOCOL.md`](PROTOCOL.md) §2.2). The reader thread exploits an
  asymmetry instead: every notification fits one report and parses standalone,
  while fragments of a chunked reply never do — so anything that doesn't parse
  alone is dropped, not accumulated. Lighting calls are idempotent and their
  acks carry nothing; RPC that needs replies lives in `probe.py`.

Two frames go out per state change, because a fully-populated lighting object
with an 8-digit decimal colour exceeds the 61-byte body limit. Partial updates
are legal, so it's split. See [`PROTOCOL.md`](PROTOCOL.md) §2.1.

---

## Customising

Run the app:

```bash
python -m codexpad.app               # http://127.0.0.1:8378, localhost only
```

A live mockup of the pad that mirrors the real one — which session owns each
key, mic open/closed, master brightness — plus colour pickers and effect
menus for every state with **Try** on whichever key you click, five theme
presets (Classic, Matrix, Sunset, Ocean, Mono), a **Demo** cycle, the mic
ring colour, command bindings, and the 🌈 **Rainbow** button — all six keys
run the device's own rainbow effect until you press the dial. **Save**
writes `~/.codexpad.json` and the daemon reloads it on the spot, repainting
anything currently lit.

The **Claude Code setup** card is the wiring-up story: a live checklist
(hidapi installed → device on USB → daemon running → hooks in
`~/.claude/settings.json`) with an **Install hooks** button that runs
`install.sh` for you. After installing, fully quit and reopen Claude Code.

The same file edits by hand:

```json
{
  "states": {
    "working": {"color": "0000FF", "effect": 4, "brightness": 1.0}
  },
  "commands": {"MIC_ON": "shortcuts run 'Start Dictation'"},
  "mic_color": "FF0000"
}
```

Effects: `0` off, `1` solid, `2` snake, `3` rainbow, `4` breath, `5` gradient,
`6` shallow breath. Colours are `RRGGBB` — verified, no byte swap. Defaults
live in `codexpad/config.py`; anything you don't override keeps its default.

The daemon's socket also takes commands directly, no app needed:

```bash
printf '{"cmd":"rainbow"}' | nc -U /tmp/codexpad.sock    # party
printf '{"cmd":"off"}'     | nc -U /tmp/codexpad.sock
printf '{"cmd":"reload"}'  | nc -U /tmp/codexpad.sock    # re-read the config
```

Try a colour with no daemon at all:

```bash
python tools/probe.py color 0 FF00FF
```

---

## The buttons

Lighting is half the loop. The daemon also reads the device's notifications
and acts on them:

| Control | Built-in action |
|---|---|
| Agent Key (`AG00`–`AG05`) | acknowledge that session — a green or red key returns to dim idle |
| Dial rotate (`ENC_CW`/`ENC_CC`) | brightness trim for the whole pad, one step per detent |
| Dial press (`ENC_CLK`) | acknowledge everything finished at once |
| Command Keys (`ACT06`–`ACT09` ⚡✓✗⑂, `ACT12` Codex) | nothing built in — bind them below |
| Mic bar (`ACT10`+`ACT11`) | hold = push-to-talk, double-press = latch; fires `MIC_ON`/`MIC_OFF` |
| Stick flick (`STICK_N/E/S/W`) | nothing built in — bind them below |

An amber key can't be answered from the pad — Claude Code has no remote
approval interface — so it stays amber until you respond in the app. What a
press *can* do is run something of yours. The `commands` table in
`~/.codexpad.json` — or the Command bindings box in the app — binds any
control identifier to a shell command:

```json
"commands": {
  "ACT06":   "open -a 'Claude'",
  "ENC_CLK": "say all clear",
  "STICK_N": "say up"
}
```

Commands run detached with `CODEXPAD_KEY`, `CODEXPAD_CWD` and `CODEXPAD_STATE`
in the environment, so a binding knows which session's key was pressed and
what state it was in. The daemon prints the identifier of every press it sees
— press a control, read the log, bind it.

The analog stick streams `v.oai.rad` continuously, so the daemon quantises it:
a hard push fires a single `STICK_N/E/S/W` flick and re-arms once the stick
recentres. Orientation was established on hardware (PROTOCOL.md §4.2): `a` is
zero pushing down and increases counter-clockwise, so up is `0.5`.

### The mic bar

The wide mic key sits on two switches (`ACT10`/`ACT11`), folded into one
logical key. **Hold it** and the mic is open for exactly the hold;
**double-press** and it latches open until the next double-press. Opening and
closing fire `MIC_ON` and `MIC_OFF` — the daemon has no microphone of its
own, so bind them to your dictation tool:

```json
"commands": {
  "MIC_ON":  "shortcuts run 'Start Dictation'",
  "MIC_OFF": "shortcuts run 'Stop Dictation'"
}
```

While the mic is open the daemon lights the ambient ring red (colour
configurable in the app) — confirmed on hardware, and the first
characterisation of `v.oai.rgbcfg` (PROTOCOL.md §5.3).

---

## Troubleshooting

**`open failed` on macOS.** The device exposes a keyboard collection, so macOS
gates opening it behind Input Monitoring. Add your terminal in System Settings →
Privacy & Security → Input Monitoring, then **fully quit and relaunch** the
terminal — grants only apply to new processes. `sudo` works as a stopgap.
To rule out other causes: if `python tools/probe.py enumerate` lists the
device, it's this permission — not the cable, and not another app "holding"
the device. And after running under `sudo`, clean up before going sudo-free:
`sudo rm -f /tmp/codexpad.sock` (the daemon tells you when this is the issue).

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

**Presses do nothing.** The daemon prints every press it receives. If no
`press` lines appear, the device isn't reaching the reader — wrong mode, or
another process holds the handle. If presses print but nothing changes, that's
expected for keys that aren't green or red; bind them via `COMMANDS`.

**The app's Try button does nothing.** The app's status line and banner say
why. Usual causes: the daemon isn't running; the daemon is an older build
that predates app commands — restart it after every `git pull` (the app
now detects this and says so); or the daemon runs under `sudo` and loaded
root's config instead of yours (fixed — root now resolves `SUDO_USER`'s
home). The Setup card's checklist shows device/daemon/hooks state live.

**Keys stay lit after tests.** The device keeps the last lighting it was
given; nothing clears it but another command. Starting the daemon blanks all
six keys, and `python -m codexpad.daemon --off` is the one-shot version. While
the daemon runs, pressing a green or red key clears it; amber deliberately
doesn't clear on press — it means a session is waiting on you, and if no
session actually is, the state is stale: restart the daemon.

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
- [x] Capture the Command Key `k` identifiers — `ACT06`–`ACT12`, physically mapped (PROTOCOL.md §4.1)
- [x] Confirm the stick's angle orientation — `a=0` down, counter-clockwise (PROTOCOL.md §4.2)
- [ ] Session navigation on the joystick — flicks fire, but nothing consumes them yet
- [x] Characterise `v.oai.rgbcfg` `ambient` — confirmed via the mic ring (`keys` zone, `s`/`m`, non-solid effects still open)
- [x] Confirm `ACT` key releases — hold-to-talk closes on release, so `act: 0` arrives (mic pair, directly)
- [ ] Linux and Windows testing — currently macOS only
- [ ] Reconcile with the vendor's own layer system so Codex and Claude coexist

## License

MIT. See [LICENSE](LICENSE).
