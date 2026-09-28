"""Every backend must turn its compositor's JSON into the same shape.

Each test feeds a recorded fixture through a backend with its command runner
replaced, then checks the resulting Snapshot. This is what catches the class of
bug where a backend works on one compositor's version and silently produces
empty windows on another's.

The Hyprland fixture was captured from a live session. The rest are hand-written
from each compositor's documented output, so they check parsing and shape, not
that those compositors are installed.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from assistant import proc
from assistant.backends import build, describe
from assistant.backends.base import WINDOW_CAPABILITIES

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def answer(mapping: dict):
    """A fake proc.run_json/run that replies from a fixed command table."""
    def run_json(args, timeout=proc.DEFAULT_TIMEOUT):
        key = " ".join(args)
        for prefix, value in mapping.items():
            if key.startswith(prefix):
                return json.loads(json.dumps(value))
        raise AssertionError(f"unexpected command: {key}")

    def run(args, timeout=proc.DEFAULT_TIMEOUT, check=True):
        key = " ".join(args)
        for prefix, value in mapping.items():
            if key.startswith(prefix):
                return value if isinstance(value, str) else json.dumps(value)
        raise AssertionError(f"unexpected command: {key}")

    return mock.Mock(side_effect=run), mock.Mock(side_effect=run_json)


class HyprlandShape(unittest.TestCase):
    def setUp(self):
        self.backend = build("hyprland")
        self.patcher = mock.patch.object(proc, "run_json", side_effect=answer({
            "hyprctl -j clients": load("hyprland-clients.json"),
            "hyprctl -j monitors": load("hyprland-monitors.json"),
            "hyprctl -j workspaces": load("hyprland-workspaces.json"),
        })[1])
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_windows_are_converted(self):
        snapshot = self.backend.snapshot()
        self.assertTrue(snapshot.windows)
        for window in snapshot.windows:
            self.assertTrue(window.id.startswith("0x"))
            self.assertGreaterEqual(window.width, 0)

    def test_monitor_size_is_logical_not_physical(self):
        snapshot = self.backend.snapshot()
        for monitor in snapshot.monitors:
            raw = next(m for m in load("hyprland-monitors.json") if str(m.get("id")) == monitor.id)
            self.assertEqual(monitor.width, int(round(raw["width"] / raw.get("scale", 1))))
            self.assertEqual(monitor.x, int(raw["x"]))

    def test_monitor_zero_is_not_treated_as_missing(self):
        """Hyprland counts monitors from zero, so id 0 must survive conversion."""
        from assistant.backends.hyprland import HyprlandBackend

        monitor = HyprlandBackend._monitor(
            {"id": 0, "name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1}
        )
        self.assertEqual(monitor.id, "0")
        self.assertEqual(monitor.name, "eDP-1")

    def test_reserved_edges_shrink_the_work_area(self):
        from assistant.backends.base import Monitor

        monitor = Monitor(id="0", name="DP-1", x=0, y=0, width=1920, height=1080, reserved=(0, 30, 0, 8))
        x0, y0, x1, y1 = monitor.work_area()
        self.assertEqual((x0, y0), (0, 30))
        self.assertEqual(x1, 1920)
        self.assertEqual(y1, 1080 - 8)

    def test_work_area_never_collapses_below_the_minimum_window(self):
        from assistant.backends.base import Monitor

        monitor = Monitor(id="0", name="tiny", x=0, y=0, width=300, height=200, reserved=(0, 190, 0, 190))
        x0, y0, x1, y1 = monitor.work_area()
        self.assertGreater(x1 - x0, 0)
        self.assertGreater(y1 - y0, 0)

    def test_live_fixture_reserved_edges_are_carried_through(self):
        snapshot = self.backend.snapshot()
        for monitor in snapshot.monitors:
            raw = next(m for m in load("hyprland-monitors.json") if str(m.get("id")) == monitor.id)
            self.assertEqual(list(monitor.reserved), list(raw["reserved"]))


class SwayShape(unittest.TestCase):
    def setUp(self):
        self.backend = build("sway")
        self.patcher = mock.patch.object(proc, "run_json", side_effect=answer({
            "swaymsg -t get_tree": load("sway-tree.json"),
            "swaymsg -t get_outputs": load("sway-outputs.json"),
            "swaymsg -t get_workspaces": load("sway-workspaces.json"),
        })[1])
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_leaves_become_windows(self):
        snapshot = self.backend.snapshot()
        self.assertEqual(len(snapshot.windows), 3)
        self.assertEqual({w.app_id for w in snapshot.windows}, {"Alacritty", "brave", "Ghostty"})

    def test_floating_con_is_marked_floating(self):
        snapshot = self.backend.snapshot()
        ghostty = next(w for w in snapshot.windows if w.app_id == "Ghostty")
        self.assertTrue(ghostty.floating)

    def test_workspace_and_output_are_inherited_by_children(self):
        snapshot = self.backend.snapshot()
        alacritty = next(w for w in snapshot.windows if w.app_id == "Alacritty")
        self.assertEqual(alacritty.workspace, "1")
        self.assertEqual(alacritty.monitor, "DP-2")

    def test_title_comes_from_window_properties(self):
        snapshot = self.backend.snapshot()
        self.assertIn("zsh", next(w.title for w in snapshot.windows if w.app_id == "Alacritty"))

    def test_scaling_output_is_reported(self):
        snapshot = self.backend.snapshot()
        hdmi = next(m for m in snapshot.monitors if m.name == "HDMI-A-1")
        self.assertEqual(hdmi.scale, 2)
        self.assertEqual(hdmi.width, 1920)


class NiriShape(unittest.TestCase):
    def setUp(self):
        self.backend = build("niri")
        self.patcher = mock.patch.object(proc, "run_json", side_effect=answer({
            "niri msg --json windows": load("niri-windows.json"),
            "niri msg --json outputs": load("niri-outputs.json"),
            "niri msg --json workspaces": load("niri-workspaces.json"),
        })[1])
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_bare_and_wrapped_lists_both_parse(self):
        snapshot = self.backend.snapshot()
        self.assertEqual(len(snapshot.windows), 2)
        self.assertEqual(len(snapshot.monitors), 1)
        self.assertEqual(len(snapshot.workspaces), 1)

    def test_output_may_be_a_string_or_an_object(self):
        from assistant.backends.niri import _output_name

        self.assertEqual(_output_name("eDP-1"), "eDP-1")
        self.assertEqual(_output_name({"name": "DP-1"}), "DP-1")
        self.assertEqual(_output_name(None), "")

    def test_logical_geometry_is_used(self):
        snapshot = self.backend.snapshot()
        monitor = snapshot.monitors[0]
        self.assertEqual((monitor.width, monitor.height), (2560, 1440))


class KWinShape(unittest.TestCase):
    def test_helper_output_maps_onto_dataclasses(self):
        from assistant.backends.kwin import _only
        from assistant.backends.base import Window

        payload = load("kwin-snapshot.json")
        window = Window(**_only(payload["windows"][0], Window))
        self.assertEqual(window.id, "12345")
        self.assertEqual(window.app_id, "konsole")
        self.assertTrue(window.focused)

    def test_unknown_fields_from_a_newer_kwin_are_ignored(self):
        from assistant.backends.kwin import _only
        from assistant.backends.base import Monitor

        built = Monitor(**_only({"id": "DP-1", "name": "DP-1", "some_future_field": 5}, Monitor))
        self.assertEqual(built.name, "DP-1")

    def test_helper_never_builds_script_text_from_a_request(self):
        """The KWin helper must read its request, never interpolate it into code."""
        import re

        source = build("kwin")._source()
        self.assertIn("JSON.parse", source)
        for forbidden in (r"\beval\s*\(", r"new\s+Function", r"import\s*\("):
            with self.subTest(token=forbidden):
                self.assertIsNone(re.search(forbidden, source))
        # Assigning to a request field would mean a value reached the parser as
        # code. Every use of params has to be a comparison or a value.
        for line in source.splitlines():
            with self.subTest(line=line.strip()):
                self.assertIsNone(re.search(r"params\.\w+\s*=[^=]", line))

    def test_request_reaches_the_helper_as_a_file_not_an_argument(self):
        """The operation must travel through a JSON file, not a D-Bus argument."""
        backend = build("kwin")
        with mock.patch.object(backend, "_call") as call:
            with mock.patch("os.path.exists", return_value=True):
                with mock.patch("os.unlink"):
                    with mock.patch("builtins.open", mock.mock_open(read_data='{"ok": true}')):
                        answer = backend._invoke({"op": "focus", "id": "12345"})

        self.assertTrue(answer["ok"])
        self.assertEqual([c.args[0] for c in call.call_args_list], ["loadScript", "start"])
        for _method, argument in call.call_args_list:
            with self.subTest(argument=argument):
                self.assertNotIn("focus", argument)
                self.assertNotIn("12345", argument)


class X11Shape(unittest.TestCase):
    def setUp(self):
        self.backend = build("x11")
        self.texts = {
            "wmctrl -lG": text("x11-wmctrl-lG.txt"),
            "wmctrl -d": text("x11-wmctrl-d.txt"),
            "xrandr --query": text("x11-xrandr.txt"),
            "xprop -id": 'WM_CLASS(STRING) = "konsole", "Konsole"\n',
            "xdotool getactivewindow": "0x03e00007\n",
        }

        def run(args, timeout=proc.DEFAULT_TIMEOUT, check=True):
            for prefix, value in self.texts.items():
                if " ".join(args).startswith(prefix):
                    return value
            raise AssertionError(f"unexpected command: {' '.join(args)}")

        self.patcher = mock.patch.object(proc, "run", side_effect=run)
        self.patcher.start()
        # The backends probe the real PATH before shelling out, so the tools the
        # fixtures stand in for have to be reported as present.
        self.present = mock.patch.object(proc, "have", side_effect=lambda name: name in self.texts or name in ("wmctrl", "xrandr", "xprop", "xdotool"))
        self.present.start()

    def tearDown(self):
        self.present.stop()
        self.patcher.stop()

    def test_desktop_window_is_not_treated_as_a_window(self):
        snapshot = self.backend.snapshot()
        self.assertNotIn("Desktop", [w.title for w in snapshot.windows])

    def test_windows_are_parsed(self):
        snapshot = self.backend.snapshot()
        self.assertEqual({w.title for w in snapshot.windows}, {"Konsole", "Hacker News - Firefox"})

    def test_app_id_comes_from_wm_class(self):
        snapshot = self.backend.snapshot()
        self.assertTrue(all(w.app_id == "konsole" for w in snapshot.windows))

    def test_active_window_is_marked_focused(self):
        snapshot = self.backend.snapshot()
        focused = [w for w in snapshot.windows if w.focused]
        self.assertEqual(len(focused), 1)
        self.assertEqual(focused[0].id, "0x03e00007")

    def test_disconnected_outputs_are_dropped(self):
        snapshot = self.backend.snapshot()
        self.assertEqual({m.name for m in snapshot.monitors}, {"DP-2", "HDMI-A-1"})

    def test_window_is_placed_on_the_monitor_containing_it(self):
        snapshot = self.backend.snapshot()
        left = next(w for w in snapshot.windows if w.title == "Konsole")
        right = next(w for w in snapshot.windows if w.title.startswith("Hacker News"))
        self.assertEqual(left.monitor, "DP-2")
        self.assertEqual(right.monitor, "HDMI-A-1")

    def test_float_is_honestly_reported_as_missing(self):
        self.assertNotIn("float", describe(self.backend)["capabilities"])


class CapabilityModel(unittest.TestCase):
    def test_every_window_backend_declares_a_subset(self):
        for name in ("hyprland", "sway", "niri", "kwin", "x11", "virtual"):
            with self.subTest(backend=name):
                self.assertLessEqual(build(name).capabilities, set(WINDOW_CAPABILITIES))

    def test_tile_requires_placement_not_just_move(self):
        from assistant.backends.base import COMPOSITE_CAPABILITIES

        self.assertIn("placement", COMPOSITE_CAPABILITIES["tile"])

    def test_a_backend_missing_a_part_refuses_the_composite(self):
        backend = build("x11")
        self.assertFalse(backend.has("float"))
        self.assertFalse(backend.has("tile"))
        self.assertIn("float", backend.missing("tile"))

    def test_virtual_can_do_everything_it_declares(self):
        backend = build("virtual")
        for capability in WINDOW_CAPABILITIES:
            with self.subTest(capability=capability):
                self.assertTrue(backend.has(capability))

    def test_require_names_what_is_missing(self):
        from assistant.backends.base import Unsupported

        with self.assertRaises(Unsupported) as caught:
            build("x11").require("focus", "float")
        self.assertIn("float", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
