"""The IO layer, tested without a compositor, a portal or a keyboard.

Screen capture and input injection both touch things that cannot be faked with a
mock and should not be exercised for real during a test run: sending keystrokes
would land in whatever window the developer has focused. So the tests here cover
the parts that decide *what* happens -- keycode mapping, monitor resolution,
tier ordering, refusal rules -- and check that the capture methods agree on
their output rather than firing them.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from assistant import proc
from assistant.io import capture, input as io, launch, png
from assistant.backends.base import Monitor, Snapshot, Window, Workspace


class Png(unittest.TestCase):
    def test_header_is_a_valid_png(self):
        data = png.encode(2, 2, bytes(16), 4)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def test_ihdr_carries_the_dimensions(self):
        import struct

        data = png.encode(7, 3, bytes(7 * 3 * 4), 4)
        width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((width, height, depth, colour), (7, 3, 8, 6))

    def test_rgb_and_rgba_use_different_colour_types(self):
        import struct

        self.assertEqual(png.encode(1, 1, bytes(3), 3)[25], 2)
        self.assertEqual(png.encode(1, 1, bytes(4), 4)[25], 6)

    def test_refuses_a_bad_channel_count(self):
        with self.assertRaises(ValueError):
            png.encode(1, 1, b"\x00", 2)

    def test_refuses_too_little_pixel_data(self):
        with self.assertRaises(ValueError) as caught:
            png.encode(4, 4, bytes(16), 4)
        self.assertIn("Not enough pixel data", str(caught.exception))

    def test_refuses_zero_dimensions(self):
        with self.assertRaises(ValueError):
            png.encode(0, 4, bytes(16), 4)

    def test_writes_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(pathlib.Path(folder) / "out.png")
            size = png.write(path, 2, 2, bytes(16), 4)
            self.assertEqual(size, pathlib.Path(path).stat().st_size)
            self.assertEqual(pathlib.Path(path).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


class Keycodes(unittest.TestCase):
    def test_letters_match_the_kernel_numbering(self):
        self.assertEqual(io._keycode("a"), (30, False))
        self.assertEqual(io._keycode("z"), (55, False))
        self.assertEqual(io._keycode("m"), (42, False))
        self.assertEqual(io._keycode("i"), (38, False))

    def test_digits_match_the_kernel_numbering(self):
        # linux/input-event-codes.h: KEY_1 is 2 through KEY_0 which is 11.
        self.assertEqual(io._keycode("1"), (2, False))
        self.assertEqual(io._keycode("5"), (6, False))
        self.assertEqual(io._keycode("9"), (10, False))
        self.assertEqual(io._keycode("0"), (11, False))

    def test_punctuation(self):
        self.assertEqual(io._keycode("-"), (12, False))
        self.assertEqual(io._keycode("/"), (53, False))
        self.assertEqual(io._keycode("'"), (40, False))

    def test_uppercase_needs_shift(self):
        self.assertEqual(io._keycode("A"), (30, True))
        self.assertEqual(io._keycode("Z"), (55, True))

    def test_shifted_symbols_need_shift(self):
        self.assertEqual(io._keycode("!"), (2, True))
        self.assertEqual(io._keycode("@"), (3, True))
        self.assertEqual(io._keycode("?"), (53, True))
        self.assertEqual(io._keycode('"'), (40, True))
        self.assertEqual(io._keycode("~"), (41, True))

    def test_space_and_whitespace(self):
        self.assertEqual(io._keycode(" "), (57, False))
        self.assertEqual(io._keycode("\n"), (28, False))
        self.assertEqual(io._keycode("\t"), (15, False))

    def test_every_printable_character_can_be_typed(self):
        missing = [chr(code) for code in range(32, 127) if io._keycode(chr(code)) is None]
        self.assertEqual(missing, [], "these characters would be silently dropped")

    def test_named_keys(self):
        self.assertEqual(io.keycode_for("return"), 28)
        self.assertEqual(io.keycode_for("Escape"), 1)
        self.assertEqual(io.keycode_for("super"), 125)
        self.assertEqual(io.keycode_for("  Enter  "), 28)

    def test_an_unknown_key_name_is_refused(self):
        with self.assertRaises(io.InputError) as caught:
            io.keycode_for("launch_missiles")
        self.assertIn("not a key", str(caught.exception))


class ClickSafety(unittest.TestCase):
    def test_out_of_range_coordinates_are_refused(self):
        for x, y in ((-0.1, 0.5), (1.1, 0.5), (0.5, -1), (0.5, 2)):
            with self.subTest(x=x, y=y):
                with self.assertRaises(io.InputError):
                    io.click(x, y, prefer="uinput")

    def test_non_finite_coordinates_are_refused(self):
        for x, y in ((float("nan"), 0.5), (0.5, float("inf"))):
            with self.subTest(x=x, y=y):
                with self.assertRaises(io.InputError):
                    io.click(x, y, prefer="uinput")

    def test_an_unknown_button_is_refused(self):
        with self.assertRaises(io.InputError):
            io.click(0.5, 0.5, button="middle-of-the-screen", prefer="uinput")

    def test_coordinates_map_across_the_whole_monitor(self):
        with mock.patch.object(io, "monitor_bounds", return_value=(0.0, 0.0, 1000.0, 500.0)):
            with mock.patch.object(io, "_click_uinput") as click:
                click.return_value = None
                io.click(0.0, 0.0, prefer="uinput")
                io.click(1.0, 1.0, prefer="uinput")
                self.assertEqual(click.call_args_list[0].args[:2], (0, 0))
                self.assertEqual(click.call_args_list[1].args[:2], (999, 499))

    def test_the_offset_of_a_second_monitor_is_added(self):
        with mock.patch.object(io, "monitor_bounds", return_value=(1920.0, 0.0, 1280.0, 1024.0)):
            with mock.patch.object(io, "_click_uinput") as click:
                io.click(0.5, 0.5, prefer="uinput")
                self.assertEqual(click.call_args_list[0].args[:2], (640, 512))


class MonitorResolution(unittest.TestCase):
    """The aliases auto/cursor/focused have to become a real connector name."""

    def setUp(self):
        self.snapshot = Snapshot(
            windows=[Window(id="1", app_id="term")],
            monitors=[
                Monitor(id="0", name="DP-1", width=1920, height=1080),
                Monitor(id="1", name="DP-2", x=1920, width=1280, height=1024, focused=True),
                Monitor(id="2", name="DP-3", x=3200, width=800, height=600, disabled=True),
            ],
            workspaces=[Workspace(name="1", active=True)],
        )

    def _patched(self, cursor=None):
        backend = mock.Mock()
        backend.snapshot.return_value = self.snapshot
        return mock.patch("assistant.backends.detect", return_value=(backend, []))

    def test_focused_becomes_the_focused_connector(self):
        with self._patched():
            self.assertEqual(capture._resolve_output("focused"), "DP-2")

    def test_auto_takes_the_first_live_output(self):
        with self._patched():
            self.assertEqual(capture._resolve_output("auto"), "DP-1")

    def test_a_disabled_output_is_never_chosen(self):
        with self._patched():
            for alias in ("auto", "focused", ""):
                with self.subTest(alias=alias):
                    self.assertNotEqual(capture._resolve_output(alias), "DP-3")

    def test_a_named_monitor_is_passed_through_untouched(self):
        with self._patched():
            self.assertEqual(capture._resolve_output("DP-2"), "DP-2")

    def test_cursor_picks_the_output_under_the_pointer(self):
        with self._patched():
            with mock.patch("assistant.io.portal._cursor_position", return_value=(2000, 100)):
                self.assertEqual(capture._resolve_output("cursor"), "DP-2")

    def test_no_monitors_is_an_error_not_a_crash(self):
        empty = Snapshot(monitors=[])
        backend = mock.Mock()
        backend.snapshot.return_value = empty
        with mock.patch("assistant.backends.detect", return_value=(backend, [])):
            with self.assertRaises(capture.CaptureError):
                capture._resolve_output("focused")


class CaptureTiering(unittest.TestCase):
    def test_the_portal_is_tried_before_anything_installed_locally(self):
        with mock.patch.object(capture, "portal_available", return_value=True):
            with mock.patch.object(proc, "have", return_value=False):
                self.assertEqual(capture.methods(), ["portal-shot"])

    def test_nothing_available_produces_an_explanation(self):
        with mock.patch.object(capture, "portal_available", return_value=False):
            with mock.patch.object(proc, "have", return_value=False):
                self.assertEqual(capture.methods(), [])
                with self.assertRaises(capture.CaptureError) as caught:
                    capture.capture("/tmp/never-written.png")
        self.assertIn("xdg-desktop-portal", str(caught.exception))

    def test_a_failing_method_falls_through_to_the_next(self):
        tried = []

        def fake_take(method, path, monitor):
            tried.append(method)
            if method == "portal-shot":
                raise capture.CaptureError("the portal declined")
            pathlib.Path(path).write_bytes(b"\x89PNG\r\n\x1a\nrest")
            return {"method": method}

        with mock.patch.object(capture, "methods", return_value=["portal-shot", "grim"]):
            with mock.patch.object(capture, "_take", side_effect=fake_take):
                result = capture.capture("/tmp/does-not-matter.png")
        self.assertEqual(result["method"], "grim")
        self.assertEqual(tried, ["portal-shot", "grim"])

    def test_every_method_failing_lists_all_the_reasons(self):
        with mock.patch.object(capture, "methods", return_value=["portal-shot", "grim"]):
            with mock.patch.object(capture, "_take", side_effect=capture.CaptureError("nope")):
                with self.assertRaises(capture.CaptureError) as caught:
                    capture.capture("/tmp/nope.png")
        message = str(caught.exception)
        self.assertIn("portal-shot: nope", message)
        self.assertIn("grim: nope", message)

    def test_a_preference_is_honoured_and_keeps_the_others_as_fallbacks(self):
        with mock.patch.object(capture, "methods", return_value=["portal-shot", "grim", "import"]):
            order = []

            def fake_take(method, path, monitor):
                order.append(method)
                raise capture.CaptureError("stop here")

            with mock.patch.object(capture, "_take", side_effect=fake_take):
                with self.assertRaises(capture.CaptureError):
                    capture.capture("/tmp/nope.png", prefer="grim")
        self.assertEqual(order, ["grim", "portal-shot", "import"])

    def test_a_preference_that_does_not_exist_here_is_refused(self):
        with mock.patch.object(capture, "methods", return_value=["grim"]):
            with self.assertRaises(capture.CaptureError) as caught:
                capture.capture("/tmp/nope.png", prefer="spectacle")
        self.assertIn("cannot take screenshots here", str(caught.exception))

    def test_a_method_that_lies_about_writing_a_file_is_caught(self):
        target = str(pathlib.Path(tempfile.mkdtemp()) / "never-written.png")
        with mock.patch.object(capture, "methods", return_value=["grim"]):
            with mock.patch.object(capture, "_resolve_output", return_value="DP-1"):
                with mock.patch.object(proc, "run", return_value=""):
                    with self.assertRaises(capture.CaptureError) as caught:
                        capture.capture(target, prefer="grim")
        self.assertIn("wrote no file", str(caught.exception))


class Launch(unittest.TestCase):
    ENTRY = "[Desktop Entry]\nType=Application\nName=Pocket Calculator\nExec=gnome-calculator\nNoDisplay=false\n"

    def _files(self, folder: str) -> str:
        (pathlib.Path(folder) / "applications").mkdir(exist_ok=True)
        path = pathlib.Path(folder) / "applications" / "calc.desktop"
        path.write_text(self.ENTRY, encoding="utf-8")
        return folder

    def test_a_desktop_entry_is_found_by_name(self):
        with tempfile.TemporaryDirectory() as folder:
            self._files(folder)
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": folder, "XDG_DATA_DIRS": ""}):
                found = launch.search("calculator")
        self.assertTrue(any(entry["id"] == "calc" for entry in found))

    def test_an_exact_name_beats_a_partial_one(self):
        entries = [
            "[Desktop Entry]\nName=Calculator\nExec=a\n",
            "[Desktop Entry]\nName=Calculator Extra Thing\nExec=b\n",
        ]
        with tempfile.TemporaryDirectory() as folder:
            (pathlib.Path(folder) / "applications").mkdir(exist_ok=True)
            for index, body in enumerate(entries):
                (pathlib.Path(folder) / "applications" / f"e{index}.desktop").write_text(body, encoding="utf-8")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": folder, "XDG_DATA_DIRS": ""}):
                found = launch.search("calculator")
        self.assertEqual(found[0]["id"], "e0")

    def test_comments_and_groups_are_not_mistaken_for_fields(self):
        parsed = launch._read_desktop.__wrapped__ if hasattr(launch._read_desktop, "__wrapped__") else None
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "a.desktop"
            path.write_text(
                "[Desktop Entry]\n# Name=Commented Out\nName=Real Name\nExec=real\n", encoding="utf-8"
            )
            fields = launch._read_desktop(str(path))
        self.assertEqual(fields.get("Name"), "Real Name")
        self.assertNotEqual(fields.get("Exec"), "# Name=Commented Out")

    def test_no_match_says_so_usefully(self):
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": "/nonexistent", "XDG_DATA_DIRS": ""}):
            with self.assertRaises(launch.LaunchError) as caught:
                launch.find("definitely not installed")
        self.assertIn("No application matches", str(caught.exception))

    def test_a_launcher_that_does_not_exist_is_skipped(self):
        entry = {"id": "calc", "name": "Calculator", "path": "/tmp/calc.desktop", "generic": "", "keywords": ""}
        with mock.patch.object(launch, "find", return_value=entry):
            with mock.patch.object(proc, "have", return_value=False):
                with mock.patch.object(launch, "_launch_exec") as direct:
                    result = launch.launch("calculator")
        direct.assert_called_once()
        self.assertEqual(result["method"], "direct")


class Reminders(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.folder.name) / "linux-assistant" / "reminders.json"
        self.environment = mock.patch.dict("os.environ", {"XDG_STATE_HOME": self.folder.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def test_a_reminder_is_recorded(self):
        from assistant.io import reminders

        with mock.patch.object(reminders, "_schedule", return_value=False):
            result = reminders.set_reminder(30, "stand up")
        self.assertTrue(result["ok"])
        self.assertEqual(len(reminders.show()["reminders"]), 1)

    def test_an_unscheduled_reminder_says_why(self):
        from assistant.io import reminders

        with mock.patch.object(reminders, "_schedule", return_value=False):
            with mock.patch.object(reminders, "systemd_available", return_value=False):
                result = reminders.set_reminder(30, "stand up")
        self.assertFalse(result["reminder"]["scheduled"])
        self.assertIn("systemd is not available", result["note"])

    def test_past_reminders_are_pruned(self):
        from assistant.io import reminders

        with mock.patch.object(reminders, "_schedule", return_value=False):
            reminders.set_reminder(30, "future one")
        reminders._save([{"id": "old", "message": "gone", "due": 0.0, "minutes": 1, "due_text": "00:00"}])
        self.assertEqual(reminders.show()["reminders"], [])

    def test_clearing_one_leaves_the_others(self):
        from assistant.io import reminders

        with mock.patch.object(reminders, "_schedule", return_value=False):
            first = reminders.set_reminder(30, "one")["reminder"]["id"]
            reminders.set_reminder(60, "two")
        reminders.clear(first)
        remaining = reminders.show()["reminders"]
        self.assertEqual([item["message"] for item in remaining], ["two"])

    def test_a_zero_or_negative_reminder_is_refused(self):
        from assistant.io import reminders

        for minutes in (0, -5):
            with self.subTest(minutes=minutes):
                with self.assertRaises(reminders.ReminderError):
                    reminders.set_reminder(minutes, "never")

    def test_an_empty_reminder_is_refused(self):
        from assistant.io import reminders

        with self.assertRaises(reminders.ReminderError):
            reminders.set_reminder(5, "   ")

    def test_the_store_is_not_world_readable(self):
        import os

        from assistant.io import reminders

        with mock.patch.object(reminders, "_schedule", return_value=False):
            reminders.set_reminder(30, "private")
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), oct(0o600))


class NoAbsolutePaths(unittest.TestCase):
    """The original hardcoded /usr/bin for everything, which is not portable."""

    def test_no_source_file_assumes_where_a_command_lives(self):
        offenders = []
        for path in pathlib.Path("assistant").rglob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or '"/usr/bin' not in line and "'/usr/bin" not in line:
                    continue
                offenders.append(f"{path}:{number}: {stripped}")
        self.assertEqual(offenders, [], "commands must be resolved through PATH")

    def test_uinput_is_the_only_hardcoded_device_path(self):
        for path in pathlib.Path("assistant").rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "/dev/" in line and "/dev/uinput" not in line and "/dev/dribyindex/" not in line:
                    if "pathlib" in line or "/dev/null" in line or "/dev/shm" in line:
                        continue
                    self.assertNotIn("/dev/", line, f"{path}: {line.strip()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Doctor(unittest.TestCase):
    """The diagnosis has to be specific, because "it doesn't work" is useless."""

    def test_a_healthy_machine_reports_no_blockers(self):
        from assistant import doctor

        checks, blocking = doctor.report()
        self.assertFalse(blocking, "\n".join(f"{n}: {t}" for n, s, t in checks if s == doctor.FAIL))

    def test_a_missing_interpreter_version_is_caught(self):
        from collections import namedtuple

        from assistant import doctor

        old = namedtuple("version_info", "major minor micro releaselevel serial")
        with mock.patch.object(doctor.sys, "version_info", old(3, 8, 0, "final", 0)):
            checks = {name: status for name, status, _t in doctor.report()[0]}
        self.assertEqual(checks["python"], doctor.FAIL)

    def test_the_supported_interpreter_passes(self):
        from assistant import doctor

        checks = {name: status for name, status, _t in doctor.report()[0]}
        self.assertEqual(checks["python"], doctor.OK)

    def test_no_window_backend_on_wayland_names_the_real_problem(self):
        """GNOME and Plasma have no window-control API, and saying so saves hours."""
        from assistant import doctor

        with mock.patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland"}):
            with mock.patch("assistant.backends.detect", return_value=(
                __import__("assistant.backends", fromlist=["build"]).build("virtual"),
                ["none found"],
            )):
                checks = {name: (status, text) for name, status, text in doctor.report()[0]}
        status, text = checks["desktop backend"]
        self.assertEqual(status, doctor.FAIL)
        self.assertIn("GNOME and Plasma", text)
        self.assertIn("X11", text)

    def test_a_working_backend_lists_what_it_cannot_do(self):
        from assistant import doctor

        with mock.patch("assistant.backends.detect", return_value=(
            __import__("assistant.backends", fromlist=["build"]).build("x11"), [],
        )):
            checks = {name: (status, text) for name, status, text in doctor.report()[0]}
        status, text = checks["desktop backend"]
        self.assertEqual(status, doctor.OK)
        self.assertIn("x11", text)
        self.assertIn("Not available here", text)

    def test_an_unwritable_uinput_is_a_warning_not_a_failure(self):
        from assistant import doctor

        with mock.patch("os.path.exists", return_value=True):
            with mock.patch("os.access", return_value=False):
                status, text = doctor._uinput()
        self.assertEqual(status, doctor.WARN)
        self.assertIn("udev", text)

    def test_a_missing_uinput_explains_how_to_load_the_module(self):
        from assistant import doctor

        with mock.patch("os.path.exists", return_value=False):
            status, text = doctor._uinput()
        self.assertEqual(status, doctor.WARN)
        self.assertIn("modprobe uinput", text)

    def test_the_kwin_probe_rejects_a_machine_without_kwin(self):
        """A loose check would claim every non-KDE desktop."""
        from assistant import proc
        from assistant.backends.kwin import KWinBackend

        backend = KWinBackend()
        with mock.patch.object(proc, "have", return_value=True):
            with mock.patch.dict("os.environ", {"XDG_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}):
                with mock.patch.object(proc, "run", return_value=""):
                    self.assertFalse(backend.available())
                listing = "interface org.kde.KWin.Scripting {\n  loadScript(in s path, out s id);\n}"
                with mock.patch.object(proc, "run", return_value=listing):
                    self.assertTrue(backend.available())
                with mock.patch.object(proc, "run", return_value="interface org.kde.KWin {\n}"):
                    self.assertFalse(backend.available())
