"""The planner is the only part of window control the model talks to.

Everything here runs against the virtual backend, so the same checks hold no
matter which compositor is underneath. The two things worth protecting are that
a plan never claims something the backend cannot do, and that an action is
refused when the window it was planned for has gone away.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from assistant.backends import build
from assistant.backends.base import Monitor, Unsupported, Window, Workspace
from assistant.backends.virtual import VirtualBackend
from assistant.windows import find_monitor, find_workspace, plan, resolve, run


def sample() -> VirtualBackend:
    return VirtualBackend(
        windows=[
            Window(id="1", app_id="Alacritty", title="zsh", workspace="1", monitor="DP-1",
                   x=0, y=0, width=800, height=600, focused=True),
            Window(id="2", app_id="brave", title="Hacker News", workspace="1", monitor="DP-1",
                   x=800, y=0, width=800, height=600),
            Window(id="3", app_id="code", title="main.py", workspace="2", monitor="DP-2",
                   x=1600, y=0, width=800, height=600),
        ],
        monitors=[
            Monitor(id="0", name="DP-1", x=0, y=0, width=1600, height=900, focused=True, reserved=(0, 30, 0, 0)),
            Monitor(id="1", name="DP-2", x=1600, y=0, width=1920, height=1080),
        ],
        workspaces=[Workspace(name="1", active=True), Workspace(name="2")],
    )


def args(**overrides) -> argparse.Namespace:
    base = {"target": "", "to": "", "other": "", "width": 0, "height": 0, "direction": "left"}
    base.update(overrides)
    return argparse.Namespace(**base)


class Resolve(unittest.TestCase):
    def test_by_app_id(self):
        self.assertEqual(resolve("brave", sample().windows).id, "2")

    def test_by_exact_id(self):
        self.assertEqual(resolve("3", sample().windows).id, "3")

    def test_by_partial_title(self):
        self.assertEqual(resolve("Hacker", sample().windows).id, "2")

    def test_case_is_ignored(self):
        self.assertEqual(resolve("ALACRITTY", sample().windows).id, "1")

    def test_no_match_says_so(self):
        with self.assertRaises(Exception) as caught:
            resolve("nothing", sample().windows)
        self.assertIn("No open window", str(caught.exception))

    def test_ambiguity_lists_the_candidates(self):
        windows = [
            Window(id="1", app_id="brave", title="Hacker News"),
            Window(id="2", app_id="brave", title="Reddit"),
        ]
        with self.assertRaises(Exception) as caught:
            resolve("brave", windows)
        self.assertIn("ambiguous", str(caught.exception))

    def test_identical_titles_ask_for_more_detail(self):
        windows = [
            Window(id="1", app_id="brave", title="Hacker News"),
            Window(id="2", app_id="brave", title="Hacker News"),
        ]
        with self.assertRaises(Exception) as caught:
            resolve("brave", windows)
        self.assertIn("longer piece of the title", str(caught.exception))


class Monitors(unittest.TestCase):
    def setUp(self):
        self.snapshot = sample().snapshot()

    def test_focused_alias(self):
        self.assertEqual(find_monitor("focused", self.snapshot), "DP-1")

    def test_left_and_right_are_geographic(self):
        self.assertEqual(find_monitor("left", self.snapshot), "DP-1")
        self.assertEqual(find_monitor("right", self.snapshot), "DP-2")

    def test_exact_name(self):
        self.assertEqual(find_monitor("dp-2", self.snapshot), "DP-2")

    def test_unknown_name_lists_what_exists(self):
        with self.assertRaises(Exception) as caught:
            find_monitor("DP-9", self.snapshot)
        self.assertIn("DP-1", str(caught.exception))


class Workspaces(unittest.TestCase):
    def setUp(self):
        self.snapshot = sample().snapshot()

    def test_numbers_pass_through(self):
        self.assertEqual(find_workspace("7", self.snapshot), "7")

    def test_relative_numbers_pass_through(self):
        self.assertEqual(find_workspace("-1", self.snapshot), "-1")

    def test_named_workspace(self):
        self.assertEqual(find_workspace("2", self.snapshot), "2")

    def test_next_and_prev_are_kept_for_the_backend(self):
        self.assertEqual(find_workspace("next", self.snapshot), "next")

    def test_unknown_lists_open_workspaces(self):
        with self.assertRaises(Exception) as caught:
            find_workspace("main", self.snapshot)
        self.assertIn("Open workspaces", str(caught.exception))


class Planning(unittest.TestCase):
    def setUp(self):
        self.backend = sample()

    def test_list_is_always_allowed(self):
        result = plan(self.backend, "list", args())
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["windows"]), 3)
        self.assertEqual(result["backend"], "virtual")

    def test_focus_proposes_rather_than_acting(self):
        result = plan(self.backend, "focus", args(target="brave"))
        self.assertTrue(result["requires_approval"])
        self.assertEqual(result["id"], "2")
        self.assertEqual(self.backend.calls, [])

    def test_resize_keeps_the_other_dimension_when_unset(self):
        result = plan(self.backend, "resize", args(target="brave", width=1000))
        self.assertEqual((result["width"], result["height"]), (1000, 600))

    def test_resize_is_clamped_to_something_a_window_manager_accepts(self):
        result = plan(self.backend, "resize", args(target="brave", width=5, height=5))
        self.assertGreaterEqual(result["width"], 200)
        self.assertGreaterEqual(result["height"], 150)

    def test_move_to_a_monitor(self):
        result = plan(self.backend, "move", args(target="brave", to="monitor:DP-2"))
        self.assertEqual(result["op"], "movewindow")
        self.assertEqual(result["monitor"], "DP-2")

    def test_move_to_a_workspace(self):
        result = plan(self.backend, "move", args(target="brave", to="workspace:3"))
        self.assertEqual(result["workspace"], "3")

    def test_swap_needs_two_different_windows(self):
        with self.assertRaises(Exception):
            plan(self.backend, "swap", args(target="brave", other="brave"))

    def test_tiling_refuses_windows_spread_over_monitors(self):
        with self.assertRaises(Exception) as caught:
            plan(self.backend, "tile", args(target="Alacritty,code"))
        self.assertIn("one monitor", str(caught.exception))

    def test_tiling_refuses_a_single_window(self):
        with self.assertRaises(Exception) as caught:
            plan(self.backend, "tile", args(target="Alacritty"))
        self.assertIn("at least two", str(caught.exception))

    def test_tiling_two_windows_produces_column_ids(self):
        result = plan(self.backend, "tile", args(target="Alacritty,brave", direction="right"))
        self.assertEqual(sorted(result["ids"]), ["1", "2"])
        self.assertEqual(result["direction"], "right")

    def test_an_unsupported_operation_is_refused_before_it_is_proposed(self):
        x11 = build("x11")
        with self.assertRaises(Unsupported) as caught:
            plan(x11, "float", args(target="anything"))
        self.assertIn("x11", str(caught.exception))

    def test_tile_accepts_a_comma_separated_string_or_a_list(self):
        as_string = plan(self.backend, "tile", args(target="Alacritty,brave"))
        as_list = plan(self.backend, "tile", args(target=["Alacritty", "brave"]))
        self.assertEqual(as_string["ids"], as_list["ids"])

    def test_a_plan_is_never_built_for_a_backend_that_cannot_run_it(self):
        x11 = build("x11")
        with self.assertRaises(Unsupported):
            plan(x11, "tile", args(target="a,b"))


class Running(unittest.TestCase):
    def setUp(self):
        self.backend = sample()

    def test_focus_reaches_the_backend(self):
        run(self.backend, {"op": "focus", "id": "2"})
        self.assertEqual(self.backend.calls, [("focus", "2")])

    def test_a_window_closed_since_the_plan_is_not_acted_on(self):
        payload = plan(self.backend, "focus", args(target="brave"))
        self.backend.windows = [w for w in self.backend.windows if w.id != "2"]
        result = run(self.backend, payload)
        self.assertFalse(result["ok"])
        self.assertIn("closed", result["error"])
        self.assertEqual(self.backend.calls, [])

    def test_workspace_switch(self):
        run(self.backend, {"op": "workspace", "workspace": "2"})
        self.assertEqual(self.backend.calls, [("switch_workspace", "2")])

    def test_tiling_places_windows_in_equal_columns(self):
        result = run(self.backend, {
            "op": "tile", "ids": ["1", "2"], "monitor": "DP-1", "direction": "left",
        })
        self.assertTrue(result["ok"])
        kinds = [call[0] for call in self.backend.calls]
        self.assertEqual(kinds, ["set_floating", "resize", "move_rect"] * 2)
        first = next(c for c in self.backend.calls if c[0] == "move_rect")
        second = [c for c in self.backend.calls if c[0] == "move_rect"][1]
        self.assertLess(first[2], second[2])

    def test_tiling_respects_the_reserved_top_edge(self):
        run(self.backend, {"op": "tile", "ids": ["1", "2"], "monitor": "DP-1", "direction": "left"})
        for call in self.backend.calls:
            if call[0] == "move_rect":
                self.assertEqual(call[3], 30)

    def test_tiling_from_the_right_ends_at_the_work_area_edge(self):
        monitor = sample().monitors[0]
        run(self.backend, {"op": "tile", "ids": ["1", "2"], "monitor": "DP-1", "direction": "right"})
        x1 = monitor.work_area()[2]
        last = [c for c in self.backend.calls if c[0] == "move_rect"][-1]
        width = [c for c in self.backend.calls if c[0] == "resize"][-1][2]
        self.assertEqual(last[2] + width, x1)

    def test_an_unsupported_action_reports_rather_than_raises(self):
        x11 = build("x11")
        result = run(x11, {"op": "float", "id": "1"})
        self.assertFalse(result["ok"])
        self.assertIn("x11", result["error"])

    def test_unknown_operation(self):
        self.assertFalse(run(self.backend, {"op": "nonsense"})["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
