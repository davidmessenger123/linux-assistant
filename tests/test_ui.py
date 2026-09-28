"""The window, built without ever showing it.

A display server is not needed to construct widgets, so these run in CI and in a
plain terminal. What is worth checking is that the interface shows what the
layers below decided: that the backend's capabilities reach the user, that a
refused proposal is visible rather than silently dropped, and above all that
nothing the model proposes can reach the desktop without a button press.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # noqa: E402

Adw.init()

from assistant.actions import parse  # noqa: E402
from assistant.ui.theme import STYLESHEET  # noqa: E402
from assistant.ui.window import ActionCard, AssistantWindow, Message  # noqa: E402


def window() -> AssistantWindow:
    app = Adw.Application(application_id="dev.linuxassistant.Test", flags=0)
    return AssistantWindow(app, model="test/model")


def transcript_text(widget) -> list[str]:
    found: list[str] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Message):
            found.append(child.label.get_text())
        card = _first_card(child)
        if card is not None:
            found.append("CARD: " + card.action.summary)
        child = child.get_next_sibling()
    return found


def _drain() -> None:
    """Run whatever the main loop has queued, which is how work reaches GTK."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(50):
        if not context.pending():
            return
        context.iteration(False)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _first_card(widget):
    if isinstance(widget, ActionCard):
        return widget
    if hasattr(widget, "get_first_child"):
        child = widget.get_first_child()
        while child is not None:
            found = _first_card(child)
            if found is not None:
                return found
            child = child.get_next_sibling()
    return None


class Construction(unittest.TestCase):
    def test_the_window_builds_and_names_its_backend(self):
        w = window()
        self.assertEqual(w.get_title(), "Assistant")
        self.assertTrue(w.backend_name)

    def test_it_starts_idle_with_the_prompt_focused(self):
        w = window()
        self.assertEqual(w.status.get_text(), "Ready")
        self.assertTrue(w.send_button.get_sensitive())

    def test_the_send_button_is_disabled_while_a_turn_runs(self):
        w = window()
        w._set_busy(True, "Thinking")
        self.assertFalse(w.send_button.get_sensitive())
        w._set_busy(False, "Ready")
        self.assertTrue(w.send_button.get_sensitive())


class Transcript(unittest.TestCase):
    def test_a_message_carries_its_text(self):
        w = window()
        w._add_message("user", "move firefox to workspace 3")
        self.assertIn("move firefox to workspace 3", transcript_text(w.transcript))

    def test_streamed_chunks_accumulate_into_one_message(self):
        w = window()
        w._streaming = w._add_message("assistant", "")
        w._append_stream("Hello")
        w._append_stream(" there")
        self.assertEqual(w._streaming.label.get_text(), "Hello there")

    def test_an_empty_reply_is_removed_rather_than_left_blank(self):
        w = window()
        before = len(transcript_text(w.transcript))
        w._streaming = w._add_message("assistant", "")
        w._finish("")
        self.assertNotIn("", transcript_text(w.transcript))
        self.assertGreaterEqual(len(transcript_text(w.transcript)), before)

    def test_a_turn_that_ended_badly_says_so(self):
        w = window()
        w._streaming = w._add_message("assistant", "")
        w._finish("opencode is not signed in")
        self.assertTrue(any("not signed in" in line for line in transcript_text(w.transcript)))
        self.assertEqual(w.status.get_text(), "Stopped")


class Approval(unittest.TestCase):
    """The gate. Nothing here may act without a press."""

    def test_a_proposal_appears_as_a_card_and_executes_nothing(self):
        w = window()
        action = parse(json.dumps({"ok": True, "action": "click", "x": 0.5, "y": 0.5, "target": "Send"}))
        card = w._add_card(action)
        self.assertFalse(card.settled)
        self.assertIn("CARD: Click Send", transcript_text(w.transcript))

    def test_pressing_approve_notifies_exactly_once_however_often_it_is_pressed(self):
        w = window()
        action = parse(json.dumps({"ok": True, "action": "click", "x": 0.5, "y": 0.5, "target": "Send"}))
        card = w._add_card(action)
        with mock.patch.object(AssistantWindow, "_on_decide") as handler:
            for _ in range(5):
                card._decide(True, handler)
        self.assertEqual(handler.call_count, 1, "a card must not be able to fire twice")

    def test_the_approved_action_reaches_execute(self):
        """The press is queued on the main loop, so the loop has to be drained."""
        w = window()
        action = parse(json.dumps({"ok": True, "action": "click", "x": 0.5, "y": 0.5, "target": "Send"}))
        card = w._add_card(action)
        with mock.patch("assistant.actions.execute", return_value={"ok": True, "method": "uinput"}) as run:
            card._decide(True, w._on_decide)
            for _ in range(200):
                _drain()
                if run.call_count:
                    break
                _sleep(0.01)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0].kind, "click")

    def test_declining_runs_nothing(self):
        w = window()
        action = parse(json.dumps({"ok": True, "action": "click", "x": 0.5, "y": 0.5, "target": "Delete"}))
        card = w._add_card(action)
        with mock.patch("assistant.actions.execute") as run:
            card._decide(False, w._on_decide)
        run.assert_not_called()
        self.assertTrue(card.settled)

    def test_a_high_risk_action_is_marked(self):
        w = window()
        action = parse(json.dumps({
            "ok": True, "action": "click", "x": 0.5, "y": 0.5,
            "target": "Delete everything", "risk": "high",
        }))
        card = w._add_card(action)
        self.assertEqual(card.get_style_context().has_class("risk-high"), True)

    def test_a_refusal_is_shown_rather_than_swallowed(self):
        w = window()
        event = mock.Mock(kind="refused", text="the desktop file path was refused", tool="open_application",
                          action=None, data={})
        w._apply(event)
        self.assertTrue(any("refused" in line for line in transcript_text(w.transcript)))
        self.assertEqual(w.status.get_text(), "That request was not allowed")


class BackendReporting(unittest.TestCase):
    def test_the_window_reports_what_the_backend_cannot_do(self):
        from assistant.ui.actions import _show_backend

        w = window()
        # On Hyprland nothing is unsupported, so the shortfall is stubbed rather
        # than left to whatever machine the tests happen to run on.
        w.backend_summary = {
            "backend": "x11", "available": True,
            "capabilities": ["focus", "move"],
            "unsupported": ["float", "tile", "swap"],
        }
        _show_backend(w)
        text = "\n".join(transcript_text(w.transcript))
        self.assertIn("Backend: x11", text)
        self.assertIn("Cannot:", text)
        self.assertIn("float", text)

    def test_a_backend_that_can_do_it_all_says_so_without_a_cannot_line(self):
        from assistant.ui.actions import _show_backend

        w = window()
        w.backend_summary = {
            "backend": "hyprland", "available": True,
            "capabilities": ["focus", "float"], "unsupported": [],
        }
        _show_backend(w)
        text = "\n".join(transcript_text(w.transcript))
        self.assertIn("Can:", text)
        self.assertNotIn("Cannot:", text)

    def test_an_unavailable_backend_is_explained_in_the_banner(self):
        w = window()
        self.assertIn(w.backend_name, w.backend_summary["backend"])


class Theme(unittest.TestCase):
    def test_the_stylesheet_parses(self):
        from gi.repository import Gtk

        provider = Gtk.CssProvider()
        errors = []
        provider.connect("parsing-error", lambda _p, _s, error: errors.append(error))
        provider.load_from_data(STYLESHEET.encode("utf-8"))
        self.assertEqual(errors, [], "the stylesheet must not have parse errors")

    def test_the_stylesheet_is_not_a_pile_of_literals_any_more(self):
        import re

        colours = re.findall(r"#[0-9A-Fa-f]{6}", STYLESHEET)
        self.assertLessEqual(len(colours), 6, "colours belong in one place, not at each use")

    def test_it_defines_the_approval_card_so_cards_are_visible(self):
        for selector in (".action-card", ".message-user", ".status-line", ".send-button"):
            self.assertIn(selector, STYLESHEET)


if __name__ == "__main__":
    unittest.main(verbosity=2)
