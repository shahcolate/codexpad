"""codexpad configuration - defaults, and the user's ~/.codexpad.json overlay.

Shared by the daemon and the web app, and kept import-safe without hidapi so
the app can run on a machine where the device tooling isn't installed yet.

Colours are RRGGBB hex strings in the file and the app; the daemon packs them
to ints on load (0xRRGGBB, verified on hardware, no byte swap).
"""
import copy
import json
import os

SOCK_PATH = os.environ.get("CODEXPAD_SOCK", "/tmp/codexpad.sock")


def _home():
    """The real user's home, even when the daemon runs under sudo.

    A sudo'd daemon otherwise resolves ~ to /var/root and silently reads a
    different config file than the one the app saves.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        return os.path.expanduser("~" + os.environ["SUDO_USER"])
    return os.path.expanduser("~")


CONFIG_PATH = os.environ.get("CODEXPAD_CONFIG",
                             os.path.join(_home(), ".codexpad.json"))
PORT = int(os.environ.get("CODEXPAD_PORT", "8378"))

# Effect ids as the vendor client names them. On real Agent Keys (fw v0.4.1)
# 2 and 5 do nothing and 3 renders solid red — see PROTOCOL.md §5.2.
EFFECTS = {0: "off", 1: "solid", 2: "snake", 3: "rainbow",
           4: "breath", 5: "gradient", 6: "shallow breath"}

DEFAULTS = {
    "states": {
        "idle":    {"color": "FFFFFF", "effect": 1, "brightness": 0.35},
        "working": {"color": "0000FF", "effect": 4, "brightness": 1.0},
        "blocked": {"color": "FF8000", "effect": 6, "brightness": 1.0},
        "done":    {"color": "00FF00", "effect": 1, "brightness": 1.0},
        "error":   {"color": "FF0000", "effect": 1, "brightness": 1.0},
        # per-key colour comes from the daemon's hue spread; effect 3 (the
        # firmware's own "rainbow") renders solid red on real keys, so the
        # default is breath, which is confirmed on hardware
        "rainbow": {"color": "FFFFFF", "effect": 4, "brightness": 1.0},
        "off":     {"color": "000000", "effect": 0, "brightness": 0.0},
    },
    "commands": {},
    "mic_color": "FF0000",
    # How a zone should look when codexpad is asserting nothing on it.
    #
    # v.oai.rgbcfg is device CONFIGURATION, not transient status: what we
    # write to a zone stays written, across processes and across hosts. So
    # "turn the mic ring off" must NOT mean "write brightness 0 to the zone"
    # -- that leaves the pad's underglow dark for the vendor client too, and
    # nothing in the vendor UI puts it back. Releasing a zone means writing
    # this baseline instead. Colour is deliberately absent: the firmware
    # drives the ring's own colour (the BLE-blue / wired-white mode tell) and
    # we should hand that back rather than pin it. Add "color" here if you
    # want a fixed one.
    "zones": {
        "ambient": {"effect": 1, "brightness": 1.0},
        "keys":    {"effect": 1, "brightness": 1.0},
    },
    # False = the old behaviour: closing the mic writes the ring dark and
    # leaves it dark. Only useful if you actually want an unlit ring.
    "ring_off_restores": True,
    # Run by the PANEL in your login session when the mic opens/closes --
    # the place for dictation triggers and AppleScript, which a root daemon
    # can't reach. Left empty they do nothing.
    "mic_on_command": "",
    "mic_off_command": "",
    # macOS: when the ChatGPT app is running, hand the pad to it (release the
    # device, go quiet) and take it back when the app quits -- the vendor
    # client and codexpad share the pad with zero clicks.
    "auto_handoff": True,
    # Pressing a working/blocked Agent Key focuses that session. Empty = the
    # panel auto-raises the first running app it knows (Claude, Cursor,
    # iTerm, VS Code, Terminal); or set your own command (CODEXPAD_CWD env).
    "focus_command": "",
    # Opt-in: the checkmark/cross Command Keys answer the FOCUSED prompt by
    # typing Enter / Escape (panel-run AppleScript, needs Accessibility).
    "approve_from_pad": False,
    # A session blocked longer than this lights the ambient ring amber as a
    # louder nag. 0 disables.
    "nag_minutes": 10,
    "port": PORT,
}


def load():
    """DEFAULTS overlaid with the user's config file. Never raises."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            user = json.load(fh)
    except FileNotFoundError:
        return cfg
    except Exception as exc:
        print(f"codexpad: ignoring bad config {CONFIG_PATH}: {exc}", flush=True)
        return cfg
    if not isinstance(user, dict):
        return cfg
    for name, spec in (user.get("states") or {}).items():
        if isinstance(spec, dict):
            cfg["states"].setdefault(name, {}).update(spec)
    if isinstance(user.get("commands"), dict):
        cfg["commands"] = user["commands"]
    if isinstance(user.get("mic_color"), str):
        cfg["mic_color"] = user["mic_color"]
    for name, spec in (user.get("zones") or {}).items():
        if isinstance(spec, dict):
            cfg["zones"].setdefault(name, {}).update(spec)
    for key in ("mic_on_command", "mic_off_command", "focus_command"):
        if isinstance(user.get(key), str):
            cfg[key] = user[key]
    for key in ("auto_handoff", "approve_from_pad", "ring_off_restores"):
        if isinstance(user.get(key), bool):
            cfg[key] = user[key]
    if isinstance(user.get("nag_minutes"), (int, float)) \
            and not isinstance(user.get("nag_minutes"), bool):
        cfg["nag_minutes"] = max(0, user["nag_minutes"])
    if isinstance(user.get("port"), int):
        cfg["port"] = user["port"]
    return cfg


def save(user_cfg):
    """Write the user-editable subset to CONFIG_PATH."""
    keep = {key: user_cfg[key]
            for key in ("states", "commands", "mic_color", "zones",
                        "ring_off_restores",
                        "mic_on_command", "mic_off_command",
                        "auto_handoff", "focus_command", "approve_from_pad",
                        "nag_minutes", "port")
            if key in user_cfg}
    with open(CONFIG_PATH, "w") as fh:
        json.dump(keep, fh, indent=2)
        fh.write("\n")
    return keep


def color_int(value):
    """'#RRGGBB' / 'RRGGBB' / int -> packed colour int."""
    if isinstance(value, int):
        return value
    return int(str(value).lstrip("#"), 16)


def zone_fields(spec):
    """A zones[] config entry -> the wire fields v.oai.rgbcfg takes.

    Only keys the device is known to accept survive, and colour is packed the
    same way as everywhere else. An entry with no colour leaves the zone's
    colour alone, which is the point: we restore brightness and effect and let
    the firmware keep owning the hue.
    """
    out = {}
    if isinstance(spec.get("color"), (str, int)):
        try:
            out["c"] = color_int(spec["color"])
        except ValueError:
            pass
    if "effect" in spec:
        try:
            out["e"] = int(spec["effect"])
        except (TypeError, ValueError):
            pass
    for key, field in (("brightness", "b"), ("speed", "s")):
        if key in spec:
            try:
                out[field] = round(float(spec[key]), 2)
            except (TypeError, ValueError):
                continue
    return out


def states_as_tuples(cfg):
    """Config state specs -> the daemon's (colour, effect, brightness) tuples."""
    out = {}
    for name, spec in cfg["states"].items():
        try:
            out[name] = (color_int(spec.get("color", "FFFFFF")),
                         int(spec.get("effect", 1)),
                         float(spec.get("brightness", 1.0)))
        except Exception:
            continue
    return out
