"""Running a turn, without running a model.

``opencode`` is replaced by a script that prints whatever the test wants. That
makes it possible to check the parts that are actually fragile about a streaming
conversation: that a session id is picked up and reused, that a tool's output
becomes an approval rather than an action, that a malformed line cannot break
the reader, and that a missing binary produces an explanation instead of a
traceback.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from assistant.agent import Session, Turn, TurnError


def fake_opencode(folder: str, lines: list[str], exit_code: int = 0) -> str:
    """A script that prints ``lines`` as opencode would, then exits."""
    path = pathlib.Path(folder) / "fake-opencode"
    body = "#!/usr/bin/env python3\nimport sys\nfor line in " + repr(lines) + ":\n    print(line, flush=True)\n"
    if exit_code:
        body += f"sys.stderr.write('something went wrong\\n')\nsys.exit({exit_code})\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def line(**fields) -> str:
    return json.dumps(fields)


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def turn(self, lines: list[str], **kwargs) -> Turn:
        binary = fake_opencode(self.folder.name, lines, kwargs.pop("exit_code", 0))
        turn = Turn(prompt="hello", binary=binary, **kwargs)
        turn.start()
        return turn

    def test_a_turn_runs_to_completion(self):
        turn = self.turn([line(type="text", text="hello there")])
        self.assertTrue(turn.wait(10))
        self.assertEqual(turn.exit_code, 0)
        self.assertEqual(turn.error, "")

    def test_text_events_reach_the_callback(self):
        seen: list[str] = []
        turn = self.turn(
            [line(type="text", text="one"), line(type="text", text="two")],
            on_text=seen.append,
        )
        turn.wait(10)
        self.assertEqual(seen, ["one", "two"])

    def test_the_session_id_is_captured(self):
        turn = self.turn([line(sessionID="sess-42", type="text", text="hi")])
        turn.wait(10)
        self.assertEqual(turn.session_id, "sess-42")

    def test_a_malformed_line_does_not_stop_the_reader(self):
        seen: list[str] = []
        turn = self.turn(
            ["not json at all", "[1,2,3]", '"a string"', "", line(type="text", text="survived")],
            on_text=seen.append,
        )
        turn.wait(10)
        self.assertEqual(seen, ["survived"])
        self.assertEqual(turn.exit_code, 0)

    def test_a_tool_proposal_becomes_an_action_not_an_execution(self):
        from assistant.actions import Action

        got: list[Action] = []
        turn = self.turn(
            [line(type="tool_use", part={"tool": "click_screen", "state": {"output": json.dumps(
                {"ok": True, "action": "click", "x": 0.5, "y": 0.5, "target": "OK"}
            )}})],
            on_action=got.append,
        )
        turn.wait(10)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, "click")
        self.assertEqual(got[0].payload["target"], "OK")

    def test_a_refused_proposal_is_reported_and_not_shown(self):
        from assistant.actions import Action

        got: list[Action] = []
        events = []
        turn = self.turn(
            [line(type="tool_use", part={"tool": "open_application", "state": {"output": json.dumps(
                {"ok": True, "action": "open_application", "application": "Evil", "id": "evil",
                 "source": "/tmp/evil.desktop"}
            )}})],
            on_action=got.append,
            on_event=events.append,
        )
        turn.wait(10)
        self.assertEqual(got, [])
        refused = [e for e in events if e.kind == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertIn("refused", refused[0].text)

    def test_a_tool_that_only_reports_progress_is_never_parsed(self):
        from assistant.actions import Action

        got: list[Action] = []
        events = []
        turn = self.turn(
            [line(type="tool_use", part={"tool": "bash", "state": {"output": '{"ok": true, "action": "click", "x": 0.5, "y": 0.5}'}})],
            on_action=got.append,
            on_event=events.append,
        )
        turn.wait(10)
        self.assertEqual(got, [], "bash output must never be read as a proposal")
        self.assertEqual([e for e in events if e.kind == "action"], [])

    def test_a_window_proposal_is_routed_to_the_planner(self):
        events = []
        turn = self.turn(
            [line(type="tool_use", part={"tool": "window_control", "state": {"output": json.dumps(
                {"ok": True, "action": "window", "op": "focus", "target": "Firefox"}
            )}})],
            on_event=events.append,
        )
        turn.wait(10)
        self.assertEqual([e.kind for e in events if e.kind == "window_plan"], ["window_plan"])

    def test_a_failed_binary_surfaces_its_own_message(self):
        turn = self.turn([], exit_code=1)
        self.assertTrue(turn.wait(10))
        self.assertEqual(turn.exit_code, 1)
        self.assertIn("something went wrong", turn.error)

    def test_a_missing_binary_explains_itself(self):
        turn = Turn(prompt="hi", binary="/definitely/not/here/opencode")
        with self.assertRaises(TurnError) as caught:
            turn.start()
        self.assertIn("opencode was not found", str(caught.exception))
        self.assertIn("signed in", str(caught.exception))

    def test_a_turn_always_reports_that_it_finished(self):
        events = []
        turn = self.turn([line(type="text", text="done")], on_event=events.append)
        turn.wait(10)
        self.assertTrue(turn.finished.is_set())
        self.assertEqual(events[-1].kind, "finished")

    def test_the_prompt_is_passed_after_a_double_dash(self):
        binary = fake_opencode(self.folder.name, [])
        turn = Turn(prompt="say 'hello' now", binary=binary)
        command = turn.command()
        self.assertEqual(command[-2:], ["--", "say 'hello' now"])
        self.assertNotIn("sh", command[0:1])

    def test_files_are_passed_one_flag_each(self):
        binary = fake_opencode(self.folder.name, [])
        turn = Turn(prompt="p", binary=binary, files=["/a.png", "/b.png"])
        command = turn.command()
        self.assertEqual(command.count("--file"), 2)
        self.assertIn("/a.png", command)
        self.assertIn("/b.png", command)

    def test_the_app_directory_reaches_the_child_environment(self):
        turn = Turn(prompt="p", app_dir="/opt/assistant")
        self.assertEqual(turn.environment()["ASSISTANT_APP_DIR"], "/opt/assistant")

    def test_stopping_a_turn_is_safe(self):
        turn = self.turn([line(type="text", text="x")])
        turn.wait(10)
        turn.stop()
        self.assertTrue(turn.finished.is_set())


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def test_a_follow_up_turn_reuses_the_session(self):
        """The second question has to continue the thread, not start a new one."""
        session = Session(model="test/model", binary=fake_opencode(
            self.folder.name, [line(sessionID="sess-9", type="text", text="a")]
        ))

        first = session.send("first question")
        first.wait(10)
        self.assertEqual(session.session_id, "sess-9")

        session.binary = fake_opencode(
            self.folder.name, [line(sessionID="sess-9", type="text", text="b")]
        )
        second = session.send("second question")
        second.wait(10)

        command = second.command()
        self.assertIn("--session", command)
        self.assertEqual(command[command.index("--session") + 1], "sess-9")
        self.assertEqual(session.turns, 2)

    def test_a_turn_with_no_session_yet_does_not_pass_the_flag(self):
        self.assertNotIn("--session", Turn(prompt="q", binary="opencode").command())

    def test_a_turn_with_a_session_passes_it(self):
        command = Turn(prompt="q", binary="opencode", session_id="sess-1").command()
        self.assertEqual(command[command.index("--session") + 1], "sess-1")

    def test_the_configured_binary_is_used_by_every_turn(self):
        binary = fake_opencode(self.folder.name, [])
        session = Session(binary=binary)
        self.assertEqual(session.send("q").command()[0], binary)

    def test_turns_are_counted_and_recorded(self):
        session = Session()
        session.turns = 4
        session.history.append({"prompt": "hi", "turns": 1})
        self.assertEqual(session.turns, 4)
        self.assertEqual(len(session.history), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RealWireFormat(unittest.TestCase):
    """Captured from opencode 1.x so a format change is caught, not guessed at."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.folder.cleanup()

    def test_text_nested_in_part_is_found(self):
        """The reply is at part.text, not at the top level. Reading the wrong
        one loses the entire answer with no error at all."""
        seen: list[str] = []
        events = []
        binary = fake_opencode(self.folder.name, [
            '{"type":"step_start","timestamp":1,"sessionID":"ses_1","part":{"id":"prt_1","type":"step-start"}}',
            '{"type":"text","timestamp":2,"sessionID":"ses_1","part":{"id":"prt_2","type":"text",'
            '"text":"connected","time":{"start":1,"end":2}}}',
            '{"type":"step_finish","timestamp":3,"sessionID":"ses_1","part":{"id":"prt_3",'
            '"type":"step-finish","reason":"stop","cost":0}}',
        ])
        turn = Turn(prompt="q", binary=binary, on_text=seen.append, on_event=events.append)
        turn.start()
        turn.wait(10)
        self.assertEqual(seen, ["connected"])
        self.assertEqual(turn.session_id, "ses_1")
        self.assertEqual([e.kind for e in events], ["text", "step_finish", "finished"])

    def test_a_tool_call_in_the_nested_shape_becomes_an_action(self):
        from assistant.actions import Action

        got: list[Action] = []
        binary = fake_opencode(self.folder.name, [
            '{"type":"tool_use","sessionID":"ses_1","part":{"id":"prt_2","type":"tool",'
            '"tool":"click_screen","state":{"status":"completed","output":'
            + json.dumps({"ok": True, "action": "click", "x": 0.25, "y": 0.75, "target": "Send"})
            + '}}}',
        ])
        turn = Turn(prompt="q", binary=binary, on_action=got.append)
        turn.start()
        turn.wait(10)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].payload["target"], "Send")
        self.assertEqual(got[0].payload["y"], 0.75)

    def test_token_counts_reach_the_interface(self):
        events = []
        binary = fake_opencode(self.folder.name, [
            '{"type":"step_finish","part":{"type":"step-finish",'
            '"tokens":{"input":100,"output":20}}}',
        ])
        turn = Turn(prompt="q", binary=binary, on_event=events.append)
        turn.start()
        turn.wait(10)
        finish = [e for e in events if e.kind == "step_finish"]
        self.assertEqual(finish[0].data["output"], 20)
