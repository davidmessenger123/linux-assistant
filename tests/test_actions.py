"""The boundary between what a model says and what happens on the desktop.

These tests are mostly about refusal. The interesting question is not "does a
valid click get through" but "can a crafted proposal get somewhere it should
not", so the hostile cases get as much attention as the ordinary ones.

Nothing here executes anything. ``parse`` is a pure function by design, which is
what lets the whole file be tested without a compositor.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from assistant.actions import Action, Refused, parse


def proposal(**fields) -> str:
    return json.dumps({"ok": True, **fields})


class Click(unittest.TestCase):
    def test_a_normal_click_is_accepted(self):
        action = parse(proposal(action="click", x=0.5, y=0.25, target="the OK button"))
        self.assertEqual(action.kind, "click")
        self.assertEqual((action.payload["x"], action.payload["y"]), (0.5, 0.25))
        self.assertEqual(action.payload["button"], "left")

    def test_coordinates_outside_the_screen_are_refused(self):
        for x, y in ((-0.01, 0.5), (1.01, 0.5), (0.5, -1), (0.5, 2)):
            with self.subTest(x=x, y=y):
                with self.assertRaises(Refused):
                    parse(proposal(action="click", x=x, y=y))

    def test_coordinates_that_are_not_numbers_are_refused(self):
        for value in ("half", None, "", [0.5], {"x": 1}):
            with self.subTest(value=value):
                with self.assertRaises(Refused):
                    parse(proposal(action="click", x=value, y=0.5))

    def test_nan_and_infinity_are_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(Refused):
                    parse(proposal(action="click", x=value, y=0.5))

    def test_numbers_given_as_strings_are_still_checked(self):
        with self.assertRaises(Refused):
            parse(proposal(action="click", x="1.5", y=0.5))

    def test_an_unknown_button_falls_back_to_left(self):
        action = parse(proposal(action="click", x=0.5, y=0.5, button="middle"))
        self.assertEqual(action.payload["button"], "left")

    def test_a_click_without_a_screenshot_is_refused(self):
        with self.assertRaises(Refused) as caught:
            parse(proposal(action="click", x=0.5, y=0.5), screen_attached=False)
        self.assertIn("no screenshot", str(caught.exception))


class Typing(unittest.TestCase):
    def test_text_with_a_target_is_accepted(self):
        action = parse(proposal(action="type", text="hello", target="the search box"))
        self.assertEqual(action.payload["text"], "hello")

    def test_text_without_a_target_is_refused(self):
        with self.assertRaises(Refused):
            parse(proposal(action="type", text="hello", target=""))

    def test_empty_text_is_refused(self):
        with self.assertRaises(Refused):
            parse(proposal(action="type", text="", target="the search box"))

    def test_a_null_character_in_the_text_is_refused(self):
        with self.assertRaises(Refused) as caught:
            parse(proposal(action="type", text="a\x00b", target="the search box"))
        self.assertIn("null character", str(caught.exception))

    def test_type_text_is_allowed_more_room_than_a_short_proposal(self):
        text = "x" * 9000
        long_form = parse(proposal(action="type", text=text, target="t", source="type_text"))
        short_form = parse(proposal(action="type", text=text, target="t", source="screen_type"))
        self.assertEqual(len(long_form.payload["text"]), 9000)
        self.assertEqual(len(short_form.payload["text"]), 4000)

    def test_text_beyond_the_long_limit_is_still_truncated(self):
        action = parse(proposal(action="type", text="x" * 25000, target="t", source="type_text"))
        self.assertEqual(len(action.payload["text"]), 20000)

    def test_typing_without_a_screenshot_is_refused(self):
        with self.assertRaises(Refused):
            parse(proposal(action="type", text="hi", target="t"), screen_attached=False)


class OpenApplication(unittest.TestCase):
    """The desktop path check is the one that has to be right."""

    GOOD = "/usr/share/applications/org.gnome.Calculator.desktop"

    def test_a_real_desktop_entry_is_accepted(self):
        action = parse(proposal(
            action="open_application", application="Calculator",
            id="org.gnome.Calculator", source=self.GOOD,
        ))
        self.assertEqual(action.payload["id"], "org.gnome.Calculator")

    def test_a_path_outside_an_applications_directory_is_refused(self):
        for source in (
            "/home/david/.config/autostart/evil.desktop",
            "/tmp/evil.desktop",
            "/usr/bin/evil.desktop",
            "/home/david/.bashrc.desktop",
        ):
            with self.subTest(source=source):
                with self.assertRaises(Refused):
                    parse(proposal(
                        action="open_application", application="Evil",
                        id="evil", source=source,
                    ))

    def test_traversal_is_refused(self):
        with self.assertRaises(Refused):
            parse(proposal(
                action="open_application", application="Evil", id="evil",
                source="/usr/share/applications/../../../tmp/evil.desktop",
            ))

    def test_a_relative_path_is_refused(self):
        with self.assertRaises(Refused):
            parse(proposal(
                action="open_application", application="Evil", id="evil",
                source="applications/evil.desktop",
            ))

    def test_a_path_that_does_not_end_with_its_own_id_is_refused(self):
        with self.assertRaises(Refused) as caught:
            parse(proposal(
                action="open_application", application="Evil", id="evil",
                source="/usr/share/applications/somethingelse.desktop",
            ))
        self.assertIn("refused", str(caught.exception))

    def test_an_id_with_a_path_separator_is_refused(self):
        for bad_id in ("../evil", "a/b", "evil.desktop", ""):  # including a doubled extension
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(Refused):
                    parse(proposal(
                        action="open_application", application="Evil", id=bad_id,
                        source=f"/usr/share/applications/{bad_id}.desktop",
                    ))

    def test_a_null_byte_in_the_path_is_refused(self):
        with self.assertRaises(Refused):
            parse(proposal(
                action="open_application", application="Evil", id="evil",
                source="/usr/share/applications/ev\x00il.desktop",
            ))

    def test_a_user_directory_entry_is_accepted(self):
        action = parse(proposal(
            action="open_application", application="Mine", id="mine",
            source="/home/david/.local/share/applications/mine.desktop",
        ))
        self.assertEqual(action.payload["application"], "Mine")


class Allowlists(unittest.TestCase):
    def test_an_action_nobody_heard_of_is_refused(self):
        for name in ("rm_rf", "exec", "run_shell", "sudo", "open_url"):
            with self.subTest(name=name):
                with self.assertRaises(Refused):
                    parse(proposal(action=name))

    def test_only_the_allowed_notification_command_is_accepted(self):
        action = parse(proposal(action="command", command="dismiss_notifications"))
        self.assertEqual(action.payload["command"], "dismiss_notifications")

    def test_any_other_command_is_refused(self):
        for name in ("shutdown", "rm", "notify_all", "dismiss", "DISMISS_NOTIFICATIONS"):
            with self.subTest(name=name):
                with self.assertRaises(Refused):
                    parse(proposal(action="command", command=name))

    def test_only_the_allowed_recipe_operations_are_accepted(self):
        for name in ("save", "delete", "run"):
            with self.subTest(name=name):
                parse(proposal(action="recipe", operation=name, recipe="{}", summary="s"))

    def test_other_recipe_operations_are_refused(self):
        for name in ("exec", "upload", "share", "run_all"):
            with self.subTest(name=name):
                with self.assertRaises(Refused):
                    parse(proposal(action="recipe", operation=name, recipe="{}"))


class Shape(unittest.TestCase):
    def test_something_that_is_not_json_is_refused(self):
        with self.assertRaises(Refused):
            parse("not json at all")

    def test_a_json_array_is_refused(self):
        with self.assertRaises(Refused):
            parse("[1, 2, 3]")

    def test_a_failed_result_is_simply_nothing_to_do(self):
        self.assertIsNone(parse(proposal(action="click", x=0.5, y=0.5, ok=False)))

    def test_empty_output_is_nothing_to_do(self):
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("   "))
        self.assertIsNone(parse(None))

    def test_a_dict_is_accepted_as_well_as_a_string(self):
        action = parse({"ok": True, "action": "click", "x": 0.5, "y": 0.5})
        self.assertEqual(action.kind, "click")

    def test_an_ok_result_with_no_action_is_nothing_to_do(self):
        self.assertIsNone(parse(proposal(note="just informing you")))

    def test_a_window_plan_carries_no_approval_of_its_own(self):
        action = parse(proposal(action="window", op="focus", target="Firefox"))
        self.assertEqual(action.kind, "window_plan")
        self.assertFalse(action.requires_approval)

    def test_a_window_proposal_without_an_operation_is_refused(self):
        with self.assertRaises(Refused):
            parse(proposal(action="window", target="Firefox"))

    def test_risk_is_only_honoured_as_the_exact_word_high(self):
        self.assertEqual(parse(proposal(action="click", x=0.5, y=0.5, risk="high")).risk, "high")
        self.assertEqual(parse(proposal(action="click", x=0.5, y=0.5, risk="HIGH")).risk, "")
        self.assertEqual(parse(proposal(action="click", x=0.5, y=0.5, risk="medium")).risk, "")

    def test_an_empty_action_name_is_nothing_to_do_not_an_error(self):
        self.assertIsNone(parse(proposal(note="no action here")))

    def test_a_summary_is_never_allowed_to_be_unbounded(self):
        action = parse(proposal(action="click", x=0.5, y=0.5, target="z" * 5000))
        self.assertLessEqual(len(action.summary), 200)


class ExecutionIsSeparate(unittest.TestCase):
    def test_parsing_alone_changes_nothing(self):
        """parse must not reach the desktop, or a proposal would be a command."""
        from assistant import actions

        with mock.patch.object(actions, "execute") as execute:
            parse(proposal(action="click", x=0.5, y=0.5, target="the close button"))
        execute.assert_not_called()

    def test_an_action_this_version_does_not_implement_is_refused_at_execution(self):
        from assistant import actions

        with self.assertRaises(Refused):
            actions.execute(Action(kind="invented", summary="x"), backend=None)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class LaunchIsCheckedAgain(unittest.TestCase):
    """launch() re-checks the path, because it is the last gate before exec."""

    def test_a_vetted_path_is_accepted(self):
        from assistant.io import launch

        self.assertTrue(launch._is_application_entry("/usr/share/applications/firefox.desktop"))
        self.assertTrue(launch._is_application_entry("/home/x/.local/share/applications/mine.desktop"))

    def test_paths_outside_an_applications_directory_are_rejected(self):
        from assistant.io import launch

        for path in (
            "/tmp/evil.desktop",
            "/home/x/.config/autostart/evil.desktop",
            "/usr/bin/evil.desktop",
            "/usr/share/applications/../../evil.desktop",
            "applications/evil.desktop",
            "/evil.desktop",
            "",
        ):
            with self.subTest(path=path):
                self.assertFalse(launch._is_application_entry(path))

    def test_launch_refuses_a_source_that_is_not_an_application_entry(self):
        from assistant import actions
        from assistant.io import launch

        action = Action(kind="open_application", summary="x", payload={"source": "/tmp/evil.desktop"})
        with self.assertRaises(launch.LaunchError):
            actions.execute(action, backend=None)

    def test_execution_uses_the_vetted_path_not_the_id(self):
        from unittest import mock
        from assistant import actions

        action = Action(
            kind="open_application", summary="x",
            payload={"application": "Calculator", "id": "org.gnome.Calculator",
                     "source": "/usr/share/applications/org.gnome.Calculator.desktop"},
        )
        with mock.patch("assistant.io.launch.launch", return_value={"ok": True}) as called:
            actions.execute(action, backend=None)
        called.assert_called_once_with("", source="/usr/share/applications/org.gnome.Calculator.desktop")

    def test_a_name_is_never_treated_as_a_path(self):
        """A crafted name must resolve through the search, not be executed."""
        from assistant.io import launch

        with self.assertRaises(launch.LaunchError):
            launch.launch("../../../tmp/evil.desktop")
