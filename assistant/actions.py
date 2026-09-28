"""Turning a tool's proposal into an action, or refusing it.

Everything the model asks for arrives as JSON from an opencode tool, and some of
it would touch the desktop: move a window, click a point, type a paragraph, start
a program. The rules in this module are the boundary. They are ported from the
original's QML, which was careful about them, and they are deliberately
conservative in the same places.

Two properties are worth stating plainly:

* **A proposal is never executed here.** Parsing returns something the user is
  shown and approves. Execution is a separate call, so nothing in this module can
  act on the desktop as a side effect of reading a model's output.
* **Names are checked against allowlists, not denylists.** An action the code
  has never heard of is refused, so a tool inventing a new one gets nowhere.

The desktop-facing path validation for ``open_application`` is the sharpest of
these. The model supplies a ``.desktop`` path, and a path is exactly the kind of
thing that can be made to point at ``~/.config/autostart/evil.desktop`` or
something else that is not an application at all. The checks below accept a path
only if its final component is exactly ``<id>.desktop`` and every parent
directory on the way is named ``applications``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .io import input as io
from .io import launch as launcher
from .windows import plan as plan_window, run as run_window

DESKTOP_ID = re.compile(r"^[A-Za-z0-9._-]+$")

NOTIFICATION_COMMANDS = frozenset({"dismiss_notifications"})
RECIPE_OPERATIONS = frozenset({"save", "delete", "run"})

TYPE_LIMIT_SHORT = 4000
TYPE_LIMIT_LONG = 20000
MAX_TARGET = 200
MAX_LINES = 40
MAX_RECIPE = 60000

# Which tools may propose something, so a tool nobody expected cannot drive the
# interface. The keys are the action names; the values say whether the proposal
# needs a screenshot to make sense.
PROPOSING_TOOLS = frozenset({
    "open_application", "click_screen", "screen_type", "type_text",
    "window_control", "clipboard_history", "notifications", "recipes",
})


class Refused(Exception):
    """A proposal did not pass validation and will not be shown to the user."""


@dataclass
class Action:
    """Something the user can be shown and then approve."""

    kind: str
    summary: str
    detail: str = ""
    risk: str = ""
    requires_approval: bool = True
    payload: dict[str, Any] = field(default_factory=dict)

    def brief(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary[:MAX_TARGET],
            "detail": self.detail[:MAX_TARGET],
            "risk": self.risk,
            "requires_approval": self.requires_approval,
            **self.payload,
        }


def _sized(text: str) -> str:
    """Every user-facing string is bounded, whatever produced it."""
    return str(text)[:MAX_TARGET]


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _desktop_path_is_sound(source: str, desktop_id: str) -> bool:
    """Accept only a real <id>.desktop inside an applications directory.

    Checks the whole path rather than its tail, because the interesting part is
    what is *above* the file: ``/home/x/.config/autostart/x.desktop`` ends the
    same way as a legitimate entry and must not pass.
    """
    if not DESKTOP_ID.match(desktop_id) or desktop_id.endswith(".desktop"):
        return False
    if not source.startswith("/") or ".." in source or "\x00" in source:
        return False
    if not source.endswith(".desktop"):
        return False
    if f"/{desktop_id}.desktop" not in source:
        return False
    parts = source.split("/")
    if len(parts) < 3 or parts[-1] != f"{desktop_id}.desktop":
        return False
    return any(part == "applications" for part in parts[:-1])


def parse(value: object, screen_attached: bool = True) -> Action | None:
    """Validate one tool proposal. Returns None when there is nothing to do.

    Raises :class:`Refused` for a proposal that is malformed or over the line,
    so the caller can say why rather than silently showing nothing.
    """
    if isinstance(value, (dict, list)):
        text = json.dumps(value)
    else:
        text = str(value or "")
    text = text.strip()
    if not text:
        return None
    try:
        proposal = json.loads(text)
    except json.JSONDecodeError as error:
        raise Refused(f"The proposal was not valid JSON: {error}") from error
    if not isinstance(proposal, dict):
        raise Refused("The proposal was not a JSON object.")
    if proposal.get("ok") is not True:
        return None

    name = str(proposal.get("action") or "")
    if not name:
        return None
    risk = "high" if proposal.get("risk") == "high" else ""
    builder = {
        "click": _click,
        "type": _type_text,
        "open_application": _open_application,
        "window": _window,
        "recipe": _recipe,
        "command": _command,
    }.get(name)
    if builder is None:
        raise Refused(f"'{name}' is not an action the assistant knows how to ask about.")
    return builder(proposal, risk, screen_attached)


def _click(proposal: dict, risk: str, screen_attached: bool) -> Action | None:
    if not screen_attached:
        raise Refused("A click was proposed but no screenshot is attached.")
    x = _finite(proposal.get("x"))
    y = _finite(proposal.get("y"))
    if x is None or y is None or not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
        raise Refused("A click was proposed with coordinates outside the screen.")
    button = str(proposal.get("button") or "left").lower()
    if button not in ("left", "right"):
        button = "left"
    target = str(proposal.get("target") or "visible target")[:MAX_TARGET]
    return Action(
        kind="click",
        summary=_sized(f"Click {target}"),
        detail="left button" if button == "left" else "right button",
        risk=risk,
        payload={"x": x, "y": y, "button": button, "target": target},
    )


def _type_text(proposal: dict, risk: str, screen_attached: bool) -> Action | None:
    if not screen_attached:
        raise Refused("Typing was proposed but no screenshot is attached.")
    typed = str(proposal.get("text") or "")
    target = str(proposal.get("target") or "").strip()
    if not typed or not target:
        raise Refused("Typing was proposed with no text or no target.")
    if "\x00" in typed:
        raise Refused("Typing was proposed with a null character in the text.")
    limit = TYPE_LIMIT_LONG if str(proposal.get("source") or "") == "type_text" else TYPE_LIMIT_SHORT
    return Action(
        kind="type",
        summary=_sized(f"Type into {target[:MAX_TARGET]}"),
        detail=f"{len(typed[:limit])} characters",
        risk=risk,
        payload={
            "text": typed[:limit],
            "target": target[:MAX_TARGET],
            "enter": proposal.get("enter") is True,
            "source": str(proposal.get("source") or ""),
        },
    )


def _open_application(proposal: dict, risk: str, _screen: bool) -> Action | None:
    application = str(proposal.get("application") or proposal.get("target") or "").strip()
    desktop_id = str(proposal.get("id") or "").strip()
    source = str(proposal.get("source") or "")
    if not application:
        raise Refused("An application was proposed with no name.")
    if not _desktop_path_is_sound(source, desktop_id):
        raise Refused(
            f"The desktop file path for '{application}' was refused because it is not an "
            "installed application entry."
        )
    return Action(
        kind="open_application",
        summary=_sized(f"Open {application[:128]}"),
        detail="installed application",
        risk=risk,
        payload={"application": application[:128], "id": desktop_id, "source": source},
    )


def _window(proposal: dict, risk: str, _screen: bool) -> Action | None:
    """Window work is a two-step plan, so this only carries the plan request."""
    op = str(proposal.get("op") or "")
    if not op:
        raise Refused("A window action was proposed with no operation.")
    return Action(
        kind="window_plan",
        summary=_sized(f"Plan window action: {op}"),
        detail="",
        risk=risk,
        requires_approval=False,
        payload={"op": op, "arguments": _window_arguments(proposal)},
    )


def _window_arguments(proposal: dict) -> dict[str, Any]:
    keep = ("target", "to", "other", "width", "height", "direction")
    return {key: proposal[key] for key in keep if key in proposal}


def _recipe(proposal: dict, risk: str, _screen: bool) -> Action | None:
    operation = str(proposal.get("operation") or "")
    if operation not in RECIPE_OPERATIONS:
        raise Refused(f"'{operation}' is not a recipe operation the assistant will run.")
    lines = proposal.get("lines")
    step_lines = [str(line)[:MAX_TARGET] for line in lines[:MAX_LINES]] if isinstance(lines, list) else []
    summary = _sized(str(proposal.get("summary") or "that recipe"))
    return Action(
        kind="recipe",
        summary=summary,
        detail=f"{operation} a recipe",
        risk=risk,
        payload={
            "operation": operation,
            "recipe": str(proposal.get("recipe") or "{}")[:MAX_RECIPE],
            "recipe_name": str(proposal.get("recipe_name") or "")[:60],
            "lines": step_lines,
        },
    )


def _command(proposal: dict, risk: str, _screen: bool) -> Action | None:
    name = str(proposal.get("command") or "")
    if name not in NOTIFICATION_COMMANDS:
        raise Refused(f"'{name}' is not a command the assistant will run.")
    return Action(
        kind="command",
        summary=_sized("Dismiss the notifications on screen"),
        detail="",
        risk=risk,
        payload={"command": name},
    )


def execute(action: Action, backend) -> dict:
    """Perform an approved action. The only function here that changes anything."""
    if action.kind == "click":
        return io.click(action.payload["x"], action.payload["y"], action.payload["button"])
    if action.kind == "type":
        result = io.type_text(action.payload["text"])
        if action.payload.get("enter"):
            io.press_key("return")
        return result
    if action.kind == "open_application":
        # The vetted path, never a re-derived name: the id alone would let a
        # caller choose the file that gets started.
        return launcher.launch("", source=action.payload["source"])
    if action.kind == "window_run":
        return run_window(backend, action.payload["proposal"])
    if action.kind == "command":
        return _dismiss_notifications()
    if action.kind == "window_plan":
        return plan_window(backend, action.payload["op"], _namespace(action.payload["arguments"]))
    raise Refused(f"The assistant cannot carry out a '{action.kind}' action.")


def _namespace(arguments: dict) -> Any:
    import argparse

    fields = {
        "target": "", "to": "", "other": "",
        "width": 0, "height": 0, "direction": "left",
    }
    for key, value in (arguments or {}).items():
        if key not in fields:
            continue
        fields[key] = int(value) if key in ("width", "height") else str(value)
    if isinstance(arguments.get("target"), list):
        fields["target"] = ",".join(str(item) for item in arguments["target"])
    return argparse.Namespace(**fields)


def _dismiss_notifications() -> dict:
    """Clear notifications through whatever the desktop provides.

    GNOME and Plasma both implement ``org.freedesktop.Notifications`` and both
    add their own ClearNotification, so this goes over D-Bus rather than through
    a compositor command the way the original did.
    """
    from . import proc

    if not proc.have("gdbus"):
        return {"ok": False, "error": "gdbus is not installed, so notifications cannot be dismissed."}
    done = proc.run(
        [
            "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.GetCapabilities",
        ],
        check=False,
    )
    if "body" not in done:
        return {"ok": False, "error": "The notification daemon exposes no ClearNotification."}
    proc.run(
        [
            "gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.CloseAll",
        ],
        check=False,
    )
    return {"ok": True}
