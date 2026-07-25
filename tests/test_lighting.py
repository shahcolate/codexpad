#!/usr/bin/env python3
"""What codexpad is and isn't allowed to do to the pad.

These are regression tests for one class of bug: codexpad leaving the device
in a state no other host can recover from. The pad has two kinds of lighting
call (PROTOCOL.md §5.1/§5.3) and they behave very differently --
`v.oai.thstatus` is transient per-key status that any host repaints, while
`v.oai.rgbcfg` is zone CONFIGURATION that stays written. Treating the second
like the first is what makes a pad look bricked: the ambient ring and the key
backlight stay dark for the vendor client too, and nothing in its UI puts
them back.

Run:  python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CODEXPAD_CONFIG", tempfile.mktemp(suffix=".json"))
os.environ.setdefault("CODEXPAD_SOCK", tempfile.mktemp(suffix=".sock"))

from codexpad import config, daemon           # noqa: E402

daemon.WRITE_GAP[0] = 0        # no need to pace writes at a fake device


class FakePad:
    """Records the frames a handle is asked to write, decoded back to JSON."""

    def __init__(self):
        self.frames = []
        self.raw = []
        self.lost = False

    def write(self, data):
        self.raw.append(bytes(data))
        length = data[2]
        self.frames.append(json.loads(bytes(data[3:3 + length]).decode().strip()))
        return len(data)

    def read(self, _n):
        return []

    def set_nonblocking(self, _flag):
        pass

    def close(self):
        pass

    # convenience -------------------------------------------------------
    def zone_writes(self, zone):
        """Every field dict sent to one rgbcfg zone, in order."""
        return [f["p"][zone] for f in self.frames
                if f.get("m") == "v.oai.rgbcfg" and zone in f.get("p", {})]

    def zone_state(self, zone):
        """Zone fields as the device would hold them: partial updates merged."""
        merged = {}
        for fields in self.zone_writes(zone):
            merged.update(fields)
        return merged


class ZoneSafety(unittest.TestCase):
    """rgbcfg persists, so codexpad must never leave a zone dark."""

    def setUp(self):
        daemon.load_config()
        daemon._zones_touched.clear()
        daemon._slot_state.clear()
        daemon._slots.clear()
        del daemon._order[:]
        daemon._paused[0] = False
        daemon._mic.update({"down": set(), "latched": False, "open": False})
        self.pad = FakePad()

    def test_closing_the_mic_relights_the_ring(self):
        daemon.set_ring(self.pad, True)
        daemon.set_ring(self.pad, False)
        final = self.pad.zone_state("ambient")
        self.assertNotEqual(final.get("b"), 0,
                            "mic close left the ambient ring at brightness 0 — "
                            "that config outlives us and darkens the ring for "
                            "the vendor client too")
        self.assertNotEqual(final.get("e"), 0,
                            "mic close left the ambient ring effect off")

    def test_ring_off_can_still_be_opted_into(self):
        daemon.RING_OFF_RESTORES[0] = False
        try:
            daemon.set_ring(self.pad, True)
            daemon.set_ring(self.pad, False)
            self.assertEqual(self.pad.zone_state("ambient").get("b"), 0)
        finally:
            daemon.RING_OFF_RESTORES[0] = True

    def test_restore_covers_zones_this_process_never_touched(self):
        """The rescue path exists for damage done by an earlier run."""
        done = daemon.restore_zones(self.pad, force=True)
        self.assertIn("ambient", done)
        self.assertIn("keys", done, "the keys zone is the one a probe sweep "
                                    "used to leave switched off")
        for zone in ("ambient", "keys"):
            self.assertEqual(self.pad.zone_state(zone).get("b"), 1.0)
            self.assertEqual(self.pad.zone_state(zone).get("e"), 1)

    def test_restore_does_not_pin_the_ring_colour(self):
        """The ring's hue is the firmware's transport tell (blue BLE / white
        wired). Restoring brightness is ours to do; the colour is not."""
        daemon.restore_zones(self.pad, force=True)
        self.assertNotIn("c", self.pad.zone_state("ambient"))

    def test_handing_the_pad_over_restores_zones_first(self):
        daemon.set_ring(self.pad, True)          # ring is ours, and red
        daemon.hand_over(self.pad, "auto")
        self.assertEqual(self.pad.zone_state("ambient").get("b"), 1.0)
        self.assertTrue(daemon._paused[0])
        self.assertEqual(daemon._paused_by[0], "auto")

    def test_shutdown_restores_zones(self):
        daemon.set_ring(self.pad, True)
        daemon.shutdown(self.pad)
        self.assertEqual(self.pad.zone_state("ambient").get("b"), 1.0)


class FrameLimits(unittest.TestCase):
    """§2.1: a body over 61 bytes is simply not transmitted."""

    def setUp(self):
        daemon.load_config()
        self.pad = FakePad()

    def test_every_frame_we_emit_fits(self):
        daemon._seq[0] = 89                      # widest id the sequencer uses
        daemon.zone_write(self.pad, "ambient",
                          {"c": 0xFFFFFF, "e": 4, "b": 1.0, "s": 0.5})
        daemon.set_slot(self.pad, 0, "working")
        daemon.restore_zones(self.pad, force=True)
        self.assertTrue(self.pad.raw)
        for frame in self.pad.raw:
            body = frame[3:3 + frame[2]]
            self.assertLessEqual(len(body), daemon.MAX_BODY,
                                 f"frame body {len(body)}B > "
                                 f"{daemon.MAX_BODY}B: {body!r}")

    def test_colour_never_shares_a_zone_frame(self):
        """An 8-digit colour plus 'ambient' is already at the limit, so it has
        to travel alone -- that is why zone_write splits."""
        daemon.zone_write(self.pad, "ambient", {"c": 0xFFFFFF, "e": 1, "b": 1})
        for fields in self.pad.zone_writes("ambient"):
            if "c" in fields:
                self.assertEqual(list(fields), ["c"])

    def test_every_slot_state_arrives_at_every_id_and_trim(self):
        """The regression that made keys look dead.

        A full-brightness thstatus update with a two-digit request id is
        exactly 61 bytes; two decimal places pushed it to 62 and the device
        never saw it. The colour frame still landed, so the key changed hue
        and kept its old brightness -- from 'off' that is zero, i.e. nothing
        lights. Trimming the dial makes almost every brightness two-decimal,
        which is why it looked like the pad died the moment you touched it.
        """
        for trim in (1.0, 0.9, 0.7, 0.3, 0.35):
            for seq in range(1, 91):
                for state in daemon.STATES:
                    pad = FakePad()
                    daemon._trim[0] = trim
                    daemon._seq[0] = seq - 1
                    daemon.set_slot(pad, 3, state)
                    sent = {}
                    for frame in pad.frames:
                        sent.update(frame["p"][0])
                    self.assertEqual(
                        set(sent) - {"id"}, {"c", "e", "b"},
                        f"state={state} trim={trim} id={seq}: the device only "
                        f"received {sorted(set(sent) - {'id'})}")
        daemon._trim[0] = 1.0

    def test_integral_numbers_are_encoded_short(self):
        self.assertEqual(daemon._num(1.0), 1)
        self.assertEqual(daemon._num(0.0), 0)
        self.assertEqual(daemon._num(0.35), 0.35)
        self.assertEqual(daemon._num(0.349), 0.35)

    def test_packer_splits_rather_than_dropping(self):
        """Six fields cannot fit one frame; all six must still arrive."""
        daemon._seq[0] = 89
        daemon.thread_write(self.pad, 5, {"c": 0xFFFFFF, "e": 4, "b": 0.85,
                                          "s": 0.55, "sk": 1, "sa": 1})
        sent = {}
        for frame in self.pad.frames:
            sent.update(frame["p"][0])
        self.assertEqual(set(sent) - {"id"},
                         {"c", "e", "b", "s", "sk", "sa"})
        for frame in self.pad.raw:
            self.assertLessEqual(frame[2], daemon.MAX_BODY)


class StartupAndPause(unittest.TestCase):

    def setUp(self):
        daemon.load_config()
        daemon._slot_state.clear()
        daemon._slots.clear()
        del daemon._order[:]
        daemon._paused[0] = False
        daemon._paused_by[0] = ""
        self.pad = FakePad()
        try:
            os.unlink(daemon.PAUSE_FLAG)
        except OSError:
            pass

    def test_blank_owned_touches_nothing_when_we_own_nothing(self):
        """Start-up and replug must not wipe lighting we never set."""
        self.assertEqual(daemon.blank_owned(self.pad), [])
        self.assertEqual(self.pad.frames, [])

    def test_blank_owned_clears_only_our_keys(self):
        daemon.set_slot(self.pad, 2, "working")
        self.pad.frames.clear()
        self.assertEqual(daemon.blank_owned(self.pad), [2])
        ids = {f["p"][0]["id"] for f in self.pad.frames
               if f.get("m") == "v.oai.thstatus"}
        self.assertEqual(ids, {2})

    def test_pause_flag_round_trips_with_provenance(self):
        daemon.write_pause_flag("auto")
        self.assertEqual(daemon.read_pause_flag(), (True, "auto"))
        self.assertTrue(daemon.clear_pause_flag())
        self.assertEqual(daemon.read_pause_flag(), (False, ""))

    def test_stale_auto_pause_resolves_itself(self):
        """An auto-handoff that outlived its ChatGPT session must not keep a
        daemon silently paused forever."""
        with open(daemon.PAUSE_FLAG, "w") as fh:
            json.dump({"by": "auto", "at": 0, "pid": 1}, fh)
        self.assertEqual(daemon.read_pause_flag(), (False, ""))

    def test_manual_pause_never_expires(self):
        with open(daemon.PAUSE_FLAG, "w") as fh:
            json.dump({"by": "manual", "at": 0, "pid": 1}, fh)
        self.assertEqual(daemon.read_pause_flag(), (True, "manual"))

    def test_legacy_flag_is_still_understood(self):
        with open(daemon.PAUSE_FLAG, "w") as fh:
            fh.write("manual")
        self.assertEqual(daemon.read_pause_flag(), (True, "manual"))

    def test_emptied_flag_reads_as_not_paused(self):
        """clear_pause_flag() falls back to truncating when it cannot unlink
        a root-owned flag; that must count as cleared."""
        with open(daemon.PAUSE_FLAG, "w"):
            pass
        self.assertEqual(daemon.read_pause_flag(), (False, ""))


class SocketCommands(unittest.TestCase):
    """handle_request() is the whole external surface: hooks, panel, MCP."""

    def setUp(self):
        daemon.load_config()
        daemon._zones_touched.clear()
        daemon._slot_state.clear()
        daemon._slots.clear()
        del daemon._order[:]
        daemon._paused[0] = False
        daemon._paused_by[0] = ""
        daemon._trim[0] = 1.0
        self.pad = FakePad()
        try:
            os.unlink(daemon.PAUSE_FLAG)
        except OSError:
            pass

    def ask(self, **req):
        return daemon.handle_request(self.pad, req)

    def test_a_hook_state_lights_its_key(self):
        self.assertEqual(self.ask(state="working", cwd="/x"), {"ok": 1})
        sent = {}
        for frame in self.pad.frames:
            sent.update(frame["p"][0])
        self.assertEqual(sent["id"], 0)
        self.assertEqual(sent["c"], 0x0000FF)
        self.assertEqual(sent["b"], 1)

    def test_restore_relights_zones_and_reports_them(self):
        reply = self.ask(cmd="restore")
        self.assertEqual(sorted(reply["zones"]), ["ambient", "keys"])
        for zone in ("ambient", "keys"):
            self.assertEqual(self.pad.zone_state(zone)["b"], 1)

    def test_restore_works_while_paused(self):
        """The rescue must not be blocked by the very handoff that hid the
        problem -- a paused daemon is exactly when people go looking."""
        daemon.hand_over(self.pad, "manual")
        self.pad.frames.clear()
        reply = self.ask(cmd="restore")
        self.assertNotIn("error", reply)
        self.assertFalse(daemon._paused[0])

    def test_pause_then_resume_repaints_the_sessions(self):
        self.ask(state="blocked", cwd="/x")
        self.ask(cmd="pause")
        self.assertTrue(daemon._paused[0])
        self.pad.frames.clear()
        self.ask(cmd="resume")
        self.assertFalse(daemon._paused[0])
        sent = {}
        for frame in self.pad.frames:
            sent.update(frame["p"][0])
        self.assertEqual(sent["c"], 0xFF8000, "amber session was not repainted")

    def test_hook_states_are_tracked_while_paused(self):
        self.ask(cmd="pause")
        self.pad.frames.clear()
        self.ask(state="working", cwd="/y")
        self.assertEqual(self.pad.frames, [],
                         "wrote to the pad while the vendor client owned it")
        self.assertEqual(daemon._slot_state.get(0), "working")

    def test_status_reports_what_the_panel_needs(self):
        st = self.ask(cmd="status")
        for key in ("paused", "paused_by", "device", "slots", "stats",
                    "zones_touched"):
            self.assertIn(key, st)

    def test_unknown_command_is_an_error_not_a_hook(self):
        self.assertIn("error", self.ask(cmd="nonsense"))


class Handoff(unittest.TestCase):

    def setUp(self):
        daemon.load_config()
        daemon._slot_state.clear()
        daemon._paused[0] = False
        daemon._paused_by[0] = ""
        self.pad = FakePad()
        try:
            os.unlink(daemon.PAUSE_FLAG)
        except OSError:
            pass

    def test_turning_auto_handoff_off_releases_an_auto_pause(self):
        """Otherwise the daemon is stranded: the only code that undoes an auto
        pause used to return one line before the undo."""
        daemon.hand_over(self.pad, "auto")
        daemon.AUTO_HANDOFF[0] = False
        try:
            daemon.take_back(self.pad, "auto-handoff switched off")
            self.assertFalse(daemon._paused[0])
            self.assertEqual(daemon.read_pause_flag(), (False, ""))
        finally:
            daemon.AUTO_HANDOFF[0] = True

    def test_manual_handoff_is_not_auto(self):
        daemon.hand_over(self.pad, "manual")
        self.assertEqual(daemon.read_pause_flag(), (True, "manual"))


class ConfigZones(unittest.TestCase):

    def test_zone_fields_maps_config_to_the_wire(self):
        self.assertEqual(config.zone_fields({"effect": 1, "brightness": 0.5}),
                         {"e": 1, "b": 0.5})
        self.assertEqual(config.zone_fields({"color": "00FF00"}),
                         {"c": 0x00FF00})

    def test_zone_fields_ignores_junk(self):
        self.assertEqual(config.zone_fields({"effect": "nope", "nonsense": 1}),
                         {})

    def test_defaults_leave_both_zones_lit(self):
        cfg = config.load()
        for zone in ("ambient", "keys"):
            fields = config.zone_fields(cfg["zones"][zone])
            self.assertEqual(fields["e"], 1)
            self.assertEqual(fields["b"], 1.0)


if __name__ == "__main__":
    unittest.main()
