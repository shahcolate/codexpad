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

EFFECTS = {0: "off", 1: "solid", 2: "snake", 3: "rainbow",
           4: "breath", 5: "gradient", 6: "shallow breath"}

DEFAULTS = {
    "states": {
        "idle":    {"color": "FFFFFF", "effect": 1, "brightness": 0.35},
        "working": {"color": "0000FF", "effect": 4, "brightness": 1.0},
        "blocked": {"color": "FF8000", "effect": 6, "brightness": 1.0},
        "done":    {"color": "00FF00", "effect": 1, "brightness": 1.0},
        "error":   {"color": "FF0000", "effect": 1, "brightness": 1.0},
        "rainbow": {"color": "FFFFFF", "effect": 3, "brightness": 1.0},
        "off":     {"color": "000000", "effect": 0, "brightness": 0.0},
    },
    "commands": {},
    "mic_color": "FF0000",
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
    if isinstance(user.get("port"), int):
        cfg["port"] = user["port"]
    return cfg


def save(user_cfg):
    """Write the user-editable subset to CONFIG_PATH."""
    keep = {key: user_cfg[key]
            for key in ("states", "commands", "mic_color", "port")
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
