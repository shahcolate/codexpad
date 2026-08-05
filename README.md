<div align="center">

<img src="docs/icon.png" width="110" alt="codexpad icon">

# codexpad

**Drive OpenAI's Codex Micro macropad from Claude Code.**

Blue while Claude works · amber when it needs you · green when it's done

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-macOS%20%C2%B7%20Linux-lightgrey)
[![Protocol](https://img.shields.io/badge/protocol-documented%20%C2%B7%20status%20tagged-C96442)](PROTOCOL.md)

<img src="docs/demo.gif" width="720" alt="The codexpad control panel mirroring live session states on the pad"><br>
<sub>Live: a session starting, working, blocked, done · the mic bar opening dictation · software rainbow · and ChatGPT taking the pad over automatically.</sub>

</div>

---

The [Codex Micro](https://openai.com/supply/co-lab/work-louder/)'s six frosted
Agent Keys light up to show what your Codex chats are doing. **codexpad makes
them do the same for Claude Code sessions** — each session claims a key, and
the one glance that matters is amber: *Claude is waiting on you.*

Along the way it documents the device's vendor HID protocol in what is, as
far as we know, its first public description: [`PROTOCOL.md`](PROTOCOL.md),
with a hardware-verification status on every claim, raw captures as evidence
in [`captures/notifications.md`](captures/notifications.md), and
[`tools/probe.py`](tools/probe.py) so you can check the document against
your own pad instead of trusting it.

> Unofficial and unaffiliated — not endorsed by OpenAI or Work Louder.
> Written against firmware `v0.4.1`; a firmware update may break any of it.

## What lights up when

| Claude Code event | Agent Key |
|---|---|
| Session starts | white, dim |
| You submit a prompt | blue, breathing |
| Every tool call | a quick **shimmer** on the blue — you can *see* it working |
| Claude needs approval or input | **amber, breathing** |
| Ignored amber for 10 minutes | the **ambient ring** lights amber too (configurable nag) |
| Turn completes | green |
| Turn fails on an API error | red |
| Session ends | off |

Running a fleet in [**Orca**](https://www.onorca.dev)? Every worktree lights
the same way — Codex, Gemini, Cursor and the rest included — and its key
presses act inside Orca. [Jump to that section](#orca--the-whole-fleet-on-six-keys).

Each session is identified by its working directory — Desktop gives every
session an isolated worktree, so the mapping needs no bookkeeping. A seventh
session evicts the least recently used key. Pressing a green or red key
acknowledges it; the dial trims brightness and clears the board.

And it flows the other way — **the pad drives Claude**:

| You press | What happens |
|---|---|
| A **working or amber** key | that session's window comes to the front (auto-detects Claude / Cursor / iTerm / VS Code / Terminal, or your own `focus_command`) |
| **✓** / **✗** (opt-in) | Enter / Escape typed into the focused prompt — approve or decline without touching the keyboard |
| The **mic bar** | your `mic_on_command` fires in your login session — one click wires it to macOS dictation |
| **✦** (the Codex key) | hands the pad to Codex — and it comes back when ChatGPT quits |
| **Stick** east / west | cycles focus through your active sessions, one flick per session |
| **⑂** (new chat) | opens the Claude app (fresh-install default; rebind to anything) |

## Parity scorecard

Every control, side by side — what the vendor stack does with Codex, what
codexpad does with Claude:

| Control | With Codex (ChatGPT app) | With Claude (codexpad) |
|---|---|---|
| 6 Agent Keys | thread status lights | session status lights **+ press to ack / jump to the window** |
| Dial | brightness | brightness trim **+ press = acknowledge everything** |
| Mic bar | native voice input | hold / double-press-latch, ring indicator, dictation into the prompt |
| ✓ / ✗ | approve / decline | approve / decline the focused prompt (opt-in) — or, under [Orca](#orca--the-whole-fleet-on-six-keys), the asking agent's own pane |
| ⑂ new chat | new Codex chat | opens Claude (default binding, rebindable) |
| ✦ Codex key | opens Codex | **hands the pad to Codex** — the two stacks share one device |
| ⚡ fast mode | Codex fast mode | bindable (no safe automatic analog — `/fast` would need keystroke injection; recipe welcome) |
| Stick | — | session navigation: E/W cycles focus; N/S bindable |
| Touch control | BLE channel / wired switch | same — it's the physical Claude↔Codex transport switch |

The one honest asterisk is ⚡ — everything else answers, lights, or acts.

## Install

### macOS — Codexpad.app

One script builds a small app that behaves like the vendor's client:
**opening it is always the fix.** It stops strays, starts the daemon (with
root, via a passwordless rule for one fixed command), supervises daemon and
panel so they restart if they die, and opens the control panel.

```bash
git clone https://github.com/shahcolate/codexpad && cd codexpad
pip install -r requirements.txt
./make_login_app.sh "$(which python)"
```

Two one-time clicks in System Settings (the script prints them):

1. **Privacy & Security → Input Monitoring** → **+** → `~/Applications` →
   **Codexpad** → toggle on. *(Remove any stale Codexpad row first —
   rebuilding the app voids old grants.)*
2. **General → Login Items** → **+** → **Codexpad**.

Then `open ~/Applications/Codexpad.app`. Put the pad in **wired mode**
(hold the front-left touch control 3s, tap until the ring is **white**) and
watch `tail -f /tmp/codexpad.daemon.log` for `codexpad ready`. Finally, in
the panel, click **Install hooks** and fully restart Claude Code.

The panel's status strip always tells you where you stand — daemon
reachable, pad connected, pad visible-but-blocked (Input Monitoring), or
pad off USB — with the exact fix for each. When you later `git pull` new
code, run **`./make_login_app.sh update`**: it refreshes the root wrappers
without touching the app bundle, so your Input Monitoring grant survives.
Only a full rebuild voids the grant.

If the strip says the pad is *blocked* even after a fresh grant, your Mac
is the stubborn kind that won't pass the grant through an app bundle — the
[field guide](#field-guide) truth table covers it, and the one-word
terminal launch there has never failed anywhere.

### Anywhere else — one command

```bash
python -m codexpad
```

starts the daemon and opens the panel at **http://127.0.0.1:8378**; the
setup card walks you through the rest with buttons. On Linux the daemon
usually just opens the device (udev permitting) with none of the macOS
ceremony. Also pip-installable straight from GitHub
(`pip install git+https://github.com/shahcolate/codexpad`), which puts a
`codexpad` command on your PATH.

### If macOS fights you

Some Macs (observed on an anaconda install) require **root and Input
Monitoring at the same time**. The [field guide](#field-guide) has the
truth table and every fallback, from `sudo python -m codexpad.daemon` to a
one-word shell function.

## The control panel

<div align="center">
<img src="docs/app.png" width="680" alt="The full codexpad control panel">
</div>

- **The status strip** — always at the top: daemon state, pad state, and
  when something's wrong, the *exact* fix ("pad on USB but blocked — Input
  Monitoring, remove the old row…"). Nothing fails silently; every button
  that can't reach the pad says why.
- **Your pad, live** — a mockup mirroring the hardware in real time: which
  session owns each key, its state and effect, mic open/closed, master
  brightness (tracks the physical dial), and the **auto-handoff** toggle
  (give ChatGPT the pad while it's open).
- **Colours & effects** — pickers, effect menus and brightness per state,
  five presets (Classic, Matrix, Sunset, Ocean, Mono), **Try** on any key
  you click, a **Demo** cycle. **Save** writes `~/.codexpad.json` and the
  daemon repaints live.
- **Mic & bindings** — ring colour, **Use macOS Dictation** one-click setup
  with Test buttons, the **Pad → Claude** controls (focus command, ✓/✗
  approve toggle, nag threshold), and shell bindings for every control.
- **A running tally** under the mockup — sessions, turns, and the honest
  number: minutes Claude spent waiting on *you*.
- **Setup** — a health checklist (hidapi → device → daemon → hooks →
  runs-at-login) where every ❌ has a button that fixes it, including
  **Install hooks** (merges into `~/.claude/settings.json`, backup first)
  and a **Start daemon** that uses the passwordless root wrapper when
  installed.
- 🌈 **Rainbow** — the firmware's *real* rainbow, cycling hues on all six
  keys until you press the dial. Unlocked by a hardware session that found
  the undocumented `s` field is the animation switch: without it rainbow
  renders solid red and breath renders solid — with it they run. codexpad
  now sends `s` for every animated effect (PROTOCOL.md §5.2 has the full
  zone-by-zone truth table; `python tools/probe.py effects|speeds|ring|keys`
  reproduces it).

## The hardware, mapped

Input flows back — the daemon reads the pad's own notifications:

| Control | Built-in action |
|---|---|
| Agent Key (`AG00`–`AG05`) | acknowledge that session — green/red returns to dim idle |
| Dial rotate (`ENC_CW`/`ENC_CC`) | brightness trim, one step per detent |
| Dial press (`ENC_CLK`) | acknowledge everything finished |
| Mic bar (`ACT10`+`ACT11`) | hold = push-to-talk, double-press = latch; fires `MIC_ON`/`MIC_OFF` |
| Command Keys (`ACT06`–`ACT09` ⚡✓✗⑂, `ACT12` ✦) | bindable |
| Stick flick (`STICK_N/E/S/W`) | bindable |

An amber key deliberately can't be answered from the pad — Claude Code has
no remote-approval interface, so clearing it would lie. What any control
*can* do is run something of yours, via the `commands` table (in the panel
or `~/.codexpad.json`), executed with `CODEXPAD_KEY`, `CODEXPAD_CWD` and
`CODEXPAD_STATE` in the environment:

```json
"commands": {
  "ACT06":   "open -a 'Claude'",
  "STICK_N": "say up"
}
```

**The mic bar is a trigger, not a microphone**: two switches folded into one
logical key. Hold it and it's open for exactly the hold; double-press
latches until the next double-press. The ambient ring lights red while open
(`mic_color`) — confirmed on hardware: `v.oai.rgbcfg`'s `ambient` zone takes
the same split partial updates as the key lighting, with `c`, `e` and `b`
behaving as documented. That is one zone, three fields and one effect value,
observed at one colour; the `keys` zone, `s`/`m` and every non-solid effect
are untested (PROTOCOL.md §5.3).

To make the mic *do* something — like dictating into Claude Code — use
`mic_on_command` / `mic_off_command` (the panel's Mic card): the **panel**
runs those in your login session, where dictation shortcuts, AppleScript and
Raycast actually work and a root daemon can't reach. The card's **Use macOS
Dictation** button fills both fields with the double-Fn trigger; you supply
the two macOS-side switches: System Settings → Keyboard → **Dictation on**
with shortcut **"Press Fn Twice"**, and an Accessibility grant the first
Test asks for. Then: focus your Claude Code terminal, hold the mic bar, and
talk — dictation types straight into the prompt. What it fills in:

```json
"mic_on_command": "osascript -e 'tell application \"System Events\" to key code 63' -e 'tell application \"System Events\" to key code 63'"
```

The generic `commands` table runs from the daemon; a sudo'd daemon drops
those to your user first, so config bindings never execute as root.

## Claude or Codex — the handoff

The single most useful thing hardware testing taught us: **check the
transport first.** What was observed here, repeatedly, on one pad and one
Mac: with a Bluetooth bond in place the pad kept returning to BLE, where it
drove the ChatGPT app happily while being *completely absent from the USB
bus* — `system_profiler` empty, nothing for codexpad to open. Wired mode
only held after the pairing was forgotten on the host. So if you're running
both stacks: **Forget the pad in Bluetooth settings while you want it
wired**, and use the ring colour as your mode tell — blue is BLE, white is
wired.

Two caveats, because this is the part most likely to be a quirk of that
setup rather than the device: we never tested whether the ChatGPT app can
*also* drive the pad over USB (it may well — that would make "blue = Codex,
white = Claude" a property of our configuration, not a design), and we never
tried to hold a USB and a BLE host at once, so "one bus at a time" is what
we saw, not something we established. If you've run either experiment,
[open an issue](../../issues) — it settles the question.

Within USB the handoff is **automatic**: the panel watches for the ChatGPT
app, and the moment it opens, codexpad blanks its lights and **releases the
device entirely** — ChatGPT drives the pad exactly as if codexpad didn't
exist. When ChatGPT quits, codexpad reconnects and repaints your Claude
sessions. Zero clicks, on by default (the checkbox under the pad mockup
turns it off), state survives the round-trip, and a manual **⇆ Hand pad to
Codex / ⇤ Take pad back** is still there for when you want to force it.
True *simultaneous* use needs the vendor's layer system — roadmap.

## Orca — the whole fleet on six keys

[Orca](https://www.onorca.dev) is an agent development environment: it runs a
fleet of coding agents — Claude Code, Codex, Gemini, Cursor, Copilot, Amp,
Droid and a dozen more CLIs — each in its own git worktree, and it collects
every one of their statuses through hooks it installs itself.

That lines up with codexpad exactly, because both name a session the same
way: **by its working directory**. An Orca worktree *is* a working
directory, so it claims the key that directory already owns — no second
mapping, no double-claimed key when a Claude session runs inside Orca. Turn
Orca on and the pad stops being a Claude-only board:

| In Orca | On the pad |
|---|---|
| a worktree with an agent working | blue, breathing — **and a shimmer per tool call**, for every agent, not just Claude |
| an agent asking for permission | **amber, breathing** — the one glance that matters, now from Codex or Gemini too |
| a turn finished | green |
| a worktree with no agent / asleep | nothing: with six keys and twenty worktrees, only agents earn one |

And back the other way — this is where the Orca link beats the generic path:

| You press | What happens |
|---|---|
| a **working or amber** key | that worktree is **activated inside Orca** over its runtime socket, its agent's pane raised, and the Orca window brought forward |
| **✓** / **✗** | Enter / Escape goes **into that agent's own pane** — not keystrokes aimed at whatever window is focused, so no Accessibility grant is involved in this path |
| **Stick** E/W | cycles through Orca worktrees and Claude sessions alike |

It's on by default and costs nothing when you don't run Orca: the bridge
only wakes when Orca's runtime file appears (`orca: false` in
`~/.codexpad.json`, or the checkbox in the panel, turns it off). Quit Orca
and the keys it claimed go dark — an amber for an agent that no longer
exists is worse than a dark key. Check the link at any time:

```bash
python -m codexpad.orca      # what the bridge sees right now
```

```
metadata  ~/Library/Application Support/orca/orca-runtime.json
runtime   reachable — 7 worktrees, 4 live agents
keys      what the pad would show:
          working  working      api-retry     ~/orca/acme/api-retry
          blocked  permission   flaky-test    ~/orca/acme/flaky-test
          done     active       docs-pass     ~/orca/acme/docs-pass
```

How it talks to Orca: the running app writes `orca-runtime.json` into its
userData directory with a unix socket path and an auth token, and speaks one
JSON object per line over it — the same transport Orca's own CLI uses.
codexpad polls `worktree.ps` with Orca's snapshot cursor (an unchanged fleet
answers *unchanged*, not a catalogue) and calls `worktree.activate`,
`terminal.resolvePane` and `terminal.send` when a key is pressed. Stdlib
only, ~200 lines, in [`codexpad/orca.py`](codexpad/orca.py). Unaffiliated
with Orca; read against Orca 1.4.x, and if a future version renames a method
the bridge goes quiet and the pad stays on Claude Code hooks.

## MCP — let anything drive the pad

codexpad ships an MCP server (stdlib-only, like everything here):

```bash
claude mcp add codexpad -- python -m codexpad.mcp
```

Now any MCP client — Claude Desktop chats, agents, scripts — gets six tools:
`pad_status`, `pad_set` (paint a key), `pad_session` (named sessions with the
same lifecycle hooks use), `pad_ring`, `pad_rainbow`, `pad_off`. Which means
you can just *tell Claude* "light key 3 green when the deploy finishes" or
have an agent track its long task on a key by calling
`pad_session(name="deploy", state="working")` … `state="done"`. The daemon
must be running; the server forwards over its socket.

## Remote sessions

Hooks from cloud/web Claude Code sessions run on the remote machine, so they
can't reach your local socket. The panel exposes a relay: anything that can
reach `localhost:8378` (say, over an SSH tunnel) can post hook-shaped events:

```bash
curl -s -X POST http://127.0.0.1:8378/api/hook \
  -d '{"state": "working", "cwd": "remote:mybox"}'
```

The panel binds 127.0.0.1 only — expose it via a tunnel
(`ssh -R 8378:127.0.0.1:8378 …`), never directly.

## Configuration

Everything lives in `~/.codexpad.json` (defaults: `codexpad/config.py`):

```json
{
  "states": {
    "working": {"color": "0000FF", "effect": 4, "brightness": 1.0}
  },
  "commands": {"ACT06": "open -a 'Claude'"},
  "mic_color": "FF0000",
  "mic_on_command": "",
  "mic_off_command": "",
  "auto_handoff": true,
  "focus_command": "",
  "approve_from_pad": false,
  "nag_minutes": 10,
  "orca": true,
  "orca_poll_s": 1.5
}
```

Effects: `0` off, `1` solid, `2` snake, `3` rainbow, `4` breath, `5`
gradient, `6` shallow breath — animated ones need the `s` speed field,
which codexpad sends automatically (per-state override: a `"speed"` key).
Snake and gradient are strip effects: they run on the ring/keys zones, not
on a single Agent Key (PROTOCOL.md §5.2). Colours are `RRGGBB` — verified,
no byte swap. The daemon's socket takes commands directly:
`rainbow`, `off`, `ring`, `reload`, `pause`, `resume`, `status`, `trim`,
`wait_event` (long-poll for pad events), e.g.
`printf '{"cmd":"status"}' | nc -U /tmp/codexpad.sock`.

## How it works

```
Claude Code hook ──stdin JSON──▶ notify.py ──unix socket──▶ daemon.py ◀──vendor HID──▶ Codex Micro
                                                                ▲ ▲
                                              control panel ────┘ │  (reload · preview · pause · status)
                        Orca runtime ◀──unix socket──▶ orca.py ────┘  (worktree.ps · activate · send)
```

- **The daemon holds the HID handle** — opening per hook is slow and races.
  It survives unplug/replug (reconnects and repaints), and its socket comes
  up **before** the pad does: while the device is missing or blocked, the
  daemon still answers `status` with a live diagnosis (visible on USB but
  unopenable = permission; not visible = cable/BLE mode), and lighting
  commands return that diagnosis instead of silently doing nothing.
- **`notify.py` never fails** — every error is swallowed, so a dead daemon
  can't break a Claude Code turn. `CODEXPAD_DEBUG=1` logs to
  `/tmp/codexpad.log`.
- **The Orca bridge is edge-triggered** — it writes a key only when that
  worktree's status *changes*, so it never fights the Claude Code hooks over
  a session both can see; whichever saw the change last is the one that
  paints. Its one deference: Orca's "no agent running" never overwrites a
  green, because a finished turn you haven't acknowledged is the more useful
  thing to say.
- **The daemon never reassembles RPC replies** — notifications and replies
  share report ID `0x06` (PROTOCOL.md §2.2), but notifications parse
  standalone and reply fragments never do, so anything unparseable is
  dropped. RPC that needs replies lives in `probe.py`.

## The protocol work

[`PROTOCOL.md`](PROTOCOL.md) documents the `kbd-1.0-codex-micro` from
observation of the author's own device: the 64-byte frame format, the
abbreviated JSON-RPC envelope, notification schemas for every control, the
lighting methods, the physical key map, and the transport behaviour.

Verified on hardware: frame layout and the 61-byte body limit, response
chunking, the envelope's silent `id`-key failure mode, all thirteen key
identifiers and their physical positions, press/release patterns, the
analog stick's schema *and* orientation, colour encoding, per-thread
lighting with split partial updates, single-zone ambient-ring control, and
the pad's identical HID-over-GATT identity on Bluetooth. Two firmware
observations were reported to Work Louder before publication (§7). No
vendor code is included or redistributed; the goal is interoperability.

All of it is one device, one host and firmware `v0.4.1`, so read §6's status
column before relying on a claim: it separates what was exercised from what
is documented-but-untested or inferred, and the untested list is not short.
`tools/probe.py` exists so you can promote a row against your own pad.

## Field guide

**Start here when anything doesn't light:**

```bash
python -m codexpad.check
```

It walks the whole chain — daemon → build version → pad → handoff state →
hooks — fires a real test light at the pad, and prints **the one fix** for
the first broken link. No guessing, no archaeology.

Every symptom and fix below was hit and cleared on real hardware; the middle
column is our best explanation of *why*, which is interpretation rather than
something we instrumented. Start with the transport — most "broken" states
are the pad being on the wrong bus:

| Symptom | It means | Fix |
|---|---|---|
| Pad lights up only when the ChatGPT app opens | It's on **Bluetooth**; the vendor app drives it over BLE | Quit ChatGPT, *Forget* the pad in Bluetooth settings, tap to the **white ring** |
| `probe.py enumerate` lists it but nothing can open it | Check the bus tag — BLE HID looks identical to USB | `[BLUETOOTH]` → tap to wired; want `[USB]` |
| Flashes blue when unplugged; "wired" doesn't stick | BLE advertising; the mode reverts on power loss, bonds dominate | Re-check for white after any unplug |
| Charges, solid indicator, `system_profiler SPUSBDataType` empty | No USB data path | Data cable (the boxed one), direct port — or it's in BLE mode |
| `open failed` with the pad on `[USB]` | macOS Input Monitoring | Grant terminal **and** python; on stubborn Macs see the truth table below |
| Panel strip: *pad on USB but macOS blocks opening it* | The daemon's grant is missing or stale | Input Monitoring: **remove** the old Codexpad row, re-add the app, toggle on, reopen |
| Rebuilt the app and it all sits at "waiting" again | **Rebuilding voids the old grant** (the bundle is re-signed) | Same remove-and-re-add dance — or avoid it: `./make_login_app.sh update` keeps the bundle |
| Panel buttons "work" but nothing lights | On daemons ≥ 0.3.0 they *tell you why* instead | Read the reply / status strip; on older builds, update |
| Daemon under launchd waits forever, pad wired | launchd can't hold an Input Monitoring grant | Use Codexpad.app (a granted app), not a plist |
| `permission denied: /tmp/codexpad.daemon.log` | Root-owned log from earlier sudo runs breaks user redirects | `sudo rm -f` it; current wrappers log as root by design |
| One key lights by itself; panel says daemon unreachable | A **stale daemon build** misreads panel commands as hook messages | Restart the daemon from the up-to-date repo; the panel names mixed builds |
| Keys stay lit after tests | The device keeps its last lighting | Any daemon start blanks; `--off`; press green/red keys |
| "Rainbow" turned everything red / breath didn't breathe | The undocumented `s` field is the animation switch and it was missing | Update — codexpad sends `s` automatically now; effects genuinely animate |
| Snake or gradient on an Agent Key does nothing | They're strip effects — ring and `keys` zones only | Use solid/breath/rainbow per-key; snake lives on the ring |
| ChatGPT stopped driving the pad since codexpad arrived | The daemon held the device / stomped the vendor lights | Update — auto-handoff releases the pad while ChatGPT runs. For BLE instead: tap the touch key to a blue-ring channel and re-pair |
| Hooks don't fire | Claude Code reads settings at launch | Fully quit and reopen; Desktop: **Code** tab + **Local** environment |
| Need to stop everything | The app supervises and revives things by design | `sudo codexpad-stop` (daemon only) or `./make_login_app.sh remove` (stops, then uninstalls) |

Logs: daemon → `/tmp/codexpad.daemon.log` (safe to `tail -f`), panel →
`~/.codexpad.app.log`, hooks (with `CODEXPAD_DEBUG=1`) → `/tmp/codexpad.log`.

**The macOS permission truth table** (some Macs need root *and* Input
Monitoring at once — diagnose with `python tools/probe.py color 0 00FF00`
with and without `sudo`):

| launch | root | Input Monitoring | opens the pad |
|---|---|---|---|
| `sudo python -m codexpad.daemon` in a granted terminal | yes | yes (inherited) | ✅ |
| Codexpad.app (granted) → sudo wrapper | yes | yes, in theory | ⚠️ observed to still fail on at least one Mac |
| plain `python -m codexpad` | no | yes | ❌ on these Macs |
| root LaunchDaemon (`service.sh`) | yes | no | ❌ |

The app row is the honest surprise: a granted app bundle *should* pass its
grant to children the way Terminal does, but on the development Mac the pad
stayed blocked even with a fresh grant — the panel's status strip told us so
explicitly. The terminal chain has never failed anywhere. So: **the
always-works path is `sudo` from a granted terminal**, and the one-word
version below makes that painless (the app still gives you the panel and the
supervision; only the daemon spawn is the sore spot):

```bash
cat >> ~/.zshrc <<'EOF'
codexpad() { sudo -n /usr/local/bin/codexpad-daemon & (cd ~/codexpad && python -m codexpad.app --no-daemon >/dev/null 2>&1 &); sleep 1; open http://127.0.0.1:8378; }
EOF
```

(Adjust `~/codexpad` if your clone lives elsewhere. If
Codexpad.app is installed, prefer opening the app — the function and the
app's supervisor would otherwise both spawn daemons.)

## Roadmap

- [x] Protocol: frames, envelope, notifications, lighting, key map, stick orientation, `rgbcfg` ambient, BLE HID identity
- [x] Buttons, dial, stick flicks, mic state machine with ring indicator
- [x] Control panel, presets, hook installer, health checks
- [x] Codexpad.app: self-healing launch, login start, icon, grant-preserving `update`
- [x] Codex handoff: automatic — pad released while ChatGPT runs, reclaimed on quit (plus manual pause/resume, survives restarts) + the transport discovery
- [x] Self-diagnosing panel: daemon/pad status strip with the exact fix
- [x] Mic → your login session: `mic_on_command`/`mic_off_command` (dictation-ready)
- [x] Pad → Claude: amber-key window focus, opt-in ✓/✗ approve/decline
- [x] Tool-call shimmer (PreToolUse), amber nag escalation on the ring
- [x] MCP server: `pad_*` tools for Claude Desktop, agents, scripts
- [x] Remote-session relay (`/api/hook`), pip install from git, session stats
- [x] Orca fleet bridge: every agent's status on the keys, presses acted on inside Orca
- [ ] Simultaneous Codex + Claude via the vendor's layer system
- [ ] Native menu-bar app (panel without the browser)
- [x] Joystick session navigation (E/W focus cycling); `s` field discovered — effects fixed
- [ ] `keys` zone as an output surface (amber approve-glow, handoff indicator)
- [ ] Local Whisper on the mic bar (no macOS dictation dependency)
- [ ] Windows support (Linux untested but expected to work)

## License & scope

MIT — see [LICENSE](LICENSE). This documents observable device behaviour,
independently implemented, for interoperability. If Work Louder or OpenAI
ship an official SDK, use that instead.
