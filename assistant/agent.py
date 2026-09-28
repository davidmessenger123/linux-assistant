"""One conversation turn: spawn opencode, read its events, hand work back.

The original did this in QML, in about 2,500 lines that mixed process management,
JSON parsing, approval state and interface updates in one file. This is the
process management and parsing half, with no idea what an interface is: it takes
a prompt, emits events, and calls back when a turn ends. Anything the interface
needs to show a person arrives as a :class:`~assistant.actions.Action` through
``on_action``, and nothing is executed until :meth:`Turn.approve` is called.

Keeping the split here is what lets the same turn logic drive a GTK window, a
command line, or a test.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .actions import Action, Refused, parse

# Tools whose output is a proposal the user might act on. Anything else is
# progress, not an action, and its output is never interpreted.
PROPOSING_TOOLS = frozenset({
    "open_application", "click_screen", "screen_type", "type_text", "window_control",
})

# Tools worth a line of status text while they run, for the interface to show.
TOOL_LABELS = {
    "websearch": "Searching the web",
    "webfetch": "Reading a web page",
    "glob": "Searching files",
    "grep": "Searching files",
    "list": "Searching files",
    "read": "Reading a file",
    "write": "Writing a file",
    "edit": "Editing a file",
    "bash": "Running a command",
    "read_clipboard": "Reading the clipboard",
    "set_reminder": "Setting a reminder",
    "clipboard_history": "Checking the clipboard history",
    "notifications": "Checking notifications",
    "recipes": "Checking recipes",
    "open_application": "Preparing an application",
    "click_screen": "Preparing a click",
    "screen_type": "Preparing to type",
    "type_text": "Preparing to type",
    "window_control": "Checking the windows",
}


class TurnError(RuntimeError):
    """The turn could not be started."""


@dataclass
class Event:
    """One thing that happened during a turn, for the interface to react to."""

    kind: str
    text: str = ""
    tool: str = ""
    action: Action | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Turn:
    """A single request to the model, and everything that came back."""

    prompt: str
    model: str = ""
    files: list[str] = field(default_factory=list)
    app_dir: str = ""
    binary: str = ""
    session_id: str = ""
    timeout: int = 900

    on_event: Callable[[Event], None] | None = None
    on_action: Callable[[Action], None] | None = None
    on_text: Callable[[str], None] | None = None
    on_session: Callable[[str], None] | None = None

    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    finished: threading.Event = field(default_factory=threading.Event)
    exit_code: int = 0
    error: str = ""
    approved: list[Action] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def command(self) -> list[str]:
        binary = self.binary or os.environ.get("ASSISTANT_OPENCODE") or "opencode"
        args = [binary, "run", "--format", "json", "--pure"]
        if self.model:
            args += ["--model", self.model]
        if self.app_dir:
            args += ["--dir", self.app_dir]
        if self.session_id:
            args += ["--session", self.session_id]
        for path in self.files:
            if path:
                args += ["--file", path]
        args += ["--", self.prompt]
        return args

    def environment(self) -> dict:
        env = dict(os.environ)
        if self.app_dir:
            env["ASSISTANT_APP_DIR"] = self.app_dir
        return env

    def start(self) -> None:
        try:
            self.process = subprocess.Popen(
                self.command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self.environment(),
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as error:
            raise TurnError(
                "opencode was not found. The assistant needs opencode installed and "
                "signed in, because it is what talks to the model."
            ) from error
        except OSError as error:
            raise TurnError(f"opencode could not be started: {error}") from error

        self.thread = threading.Thread(target=self._read, name="assistant-turn", daemon=True)
        self.thread.start()

    def _emit(self, event: Event) -> None:
        if self.on_event:
            self.on_event(event)

    def _read(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                self._ingest(line)
        except Exception as error:  # noqa: BLE001 - a turn must always end
            self.error = str(error)
        finally:
            self._drain_stderr()
            try:
                self.exit_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                self.exit_code = process.wait()
                self.error = self.error or "opencode did not stop when asked to."
            self.finished.set()
            self._emit(Event(kind="finished", text=self.error, data={"exit_code": self.exit_code}))

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            remaining = process.stderr.read()
        except (OSError, ValueError):
            return
        text = (remaining or "").strip()
        if text and not self.error:
            first = text.splitlines()[0]
            if "not authenticated" in text.lower() or "login" in text.lower():
                self.error = f"opencode is not signed in: {first}"
            else:
                self.error = first

    def _ingest(self, line: str) -> None:
        text = str(line or "").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return

        session = event.get("sessionID") or event.get("session_id")
        if session:
            with self._lock:
                self.session_id = str(session)
            if self.on_session:
                self.on_session(self.session_id)

        # Everything opencode says is wrapped in "part"; the top-level fields are
        # the envelope. Reading the wrong one silently loses the whole reply, so
        # both are checked and the nested form is preferred.
        part = event.get("part") if isinstance(event.get("part"), dict) else None
        body = part if part is not None else event
        kind = str(event.get("type") or body.get("type") or "")

        if kind in ("text", "message", "assistant"):
            message = body.get("text")
            if message:
                message = str(message)
                if self.on_text:
                    self.on_text(message)
                self._emit(Event(kind="text", text=message))
            return

        if kind == "step_finish":
            self._emit(Event(kind="step_finish", data=body.get("tokens") or {}))
            return

        tool = str(body.get("tool") or "")
        if not tool:
            return

        label = TOOL_LABELS.get(tool)
        if label:
            self._emit(Event(kind="status", text=label, tool=tool))

        if tool not in PROPOSING_TOOLS:
            return
        state = body.get("state")
        output = state.get("output") if isinstance(state, dict) else None
        if output is None:
            return
        try:
            action = parse(output)
        except Refused as error:
            self._emit(Event(kind="refused", text=str(error), tool=tool))
            return
        if action is None:
            return
        if action.kind == "window_plan":
            self._emit(Event(kind="window_plan", action=action, tool=tool))
            return
        with self._lock:
            self.approved.append(action)
        if self.on_action:
            self.on_action(action)
        self._emit(Event(kind="action", action=action, tool=tool))

    def approve(self, action: Action) -> dict:
        """Run a proposal the user accepted."""
        from . import actions as action_module
        from .backends import detect

        backend, _notes = detect()
        result = action_module.execute(action, backend)
        self._emit(Event(kind="executed", action=action, data=result))
        return result

    def wait(self, timeout: int | None = None) -> bool:
        return self.finished.wait(timeout if timeout is not None else self.timeout)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.finished.set()


class Session:
    """Keeps the conversation id so a follow-up turn continues the thread."""

    def __init__(self, model: str = "", app_dir: str = "", binary: str = "") -> None:
        self.model = model
        self.app_dir = app_dir
        self.binary = binary
        self.session_id = ""
        self.last: Turn | None = None
        self.history: list[dict] = []
        self.turns = 0

    def send(self, prompt: str, files: list[str] | None = None, **callbacks) -> Turn:
        """Start a turn, threading the conversation id forward for the next one."""
        captured: dict = {}

        def remember(session_id: str) -> None:
            captured["id"] = session_id
            self.session_id = session_id

        turn = Turn(
            prompt=prompt,
            model=self.model,
            binary=self.binary,
            files=list(files or []),
            app_dir=self.app_dir,
            session_id=self.session_id,
            on_session=remember,
            **callbacks,
        )
        turn.start()
        self.last = turn
        self.turns += 1
        self.history.append({"prompt": prompt[:500], "turns": self.turns})
        if not captured:
            # The first line of output carries the id; give it a moment so the
            # next turn is not sent as a brand new conversation.
            for _ in range(200):
                if captured:
                    break
                time.sleep(0.01)
        return turn


def new_session_id() -> str:
    return uuid.uuid4().hex
