"""Window and workspace planning, with no compositor in sight.

This is the logic that used to live in ``assistant_windows.py`` and that the
model talks to. It reads a :class:`Snapshot` from whichever backend is active,
resolves the window a request means, and returns a proposal the user approves
before anything happens. The only change from the original is that it asks the
backend what it can do and refuses to propose anything the backend cannot
deliver, rather than building a plan and failing at the last step.

There is deliberately still no close or kill. Everything here is reversible by
the user, which is the property that makes an approval prompt meaningful.
"""

from __future__ import annotations

import argparse
import json
import sys

from .backends import build, detect
from .backends.base import (
    BackendError,
    CAP_FLOAT,
    CAP_FOCUS,
    CAP_FULLSCREEN,
    CAP_MOVE,
    CAP_RESIZE,
    CAP_SCRATCHPAD,
    CAP_SWAP,
    CAP_TILE,
    CAP_WORKSPACE,
    DesktopBackend,
    Snapshot,
    Unsupported,
    Window,
    clamp_size,
)

GAP = 8
MAX_TITLE = 60
MAX_WINDOWS = 40
MIN_SIDE = 200

PLANNABLE = ("list", "focus", "move", "swap", "resize", "float", "fullscreen", "workspace", "monitor", "scratchpad", "tile")

_OP_CAPABILITY = {
    "focus": CAP_FOCUS,
    "move": CAP_MOVE,
    "swap": CAP_SWAP,
    "resize": CAP_RESIZE,
    "float": CAP_FLOAT,
    "fullscreen": CAP_FULLSCREEN,
    "workspace": CAP_WORKSPACE,
    "monitor": "monitor",
    "scratchpad": CAP_SCRATCHPAD,
    "tile": CAP_TILE,
}


def emit(payload: dict) -> int:
    print(json.dumps(payload, separators=(",", ":")))
    return 0 if payload.get("ok") is True else 1


def fail(error: str, **extra) -> int:
    payload: dict = {"ok": False, "error": str(error)[:400]}
    payload.update(extra)
    return emit(payload)


def resolve(target: str, windows: list[Window]) -> Window:
    """Find the one window a request means, or explain why it is ambiguous."""
    needle = str(target or "").strip()
    if not needle:
        raise BackendError("No window was named.")

    exact_id = [w for w in windows if w.id == needle]
    if len(exact_id) == 1:
        return exact_id[0]

    exact_app = [w for w in windows if w.app_id.lower() == needle.lower()]
    if len(exact_app) == 1:
        return exact_app[0]
    part_app = [w for w in windows if needle.lower() in w.app_id.lower()]
    if len(part_app) == 1:
        return part_app[0]
    part_title = [w for w in windows if needle.lower() in w.title.lower()]
    if len(part_title) == 1:
        return part_title[0]

    matches = exact_app or part_app or part_title
    if not matches:
        raise BackendError(f"No open window matches '{needle}'.")
    names = sorted({w.label() for w in matches})
    if len(matches) > 1 and len(names) == 1:
        raise BackendError(
            f"'{needle}' matches {len(matches)} windows all titled {names[0]}. "
            "Name an application or a longer piece of the title."
        )
    raise BackendError(f"'{needle}' is ambiguous: " + ", ".join(names[:6]))


def find_monitor(destination: str, snapshot: Snapshot) -> str:
    """Turn a monitor name, 'left'/'right', or 'focused' into a monitor id."""
    needle = str(destination or "").strip()
    monitors = snapshot.live_monitors()
    if not needle:
        raise BackendError("No monitor was named.")
    if not monitors:
        raise BackendError("No monitor is available.")
    if needle.lower() in ("focused", "focus", "current"):
        focused = snapshot.focused_monitor()
        if focused is None:
            raise BackendError("No monitor reported as focused.")
        return focused.name
    if needle.lower() in ("left", "right"):
        ordered = sorted(monitors, key=lambda m: (m.x, m.y))
        return ordered[0].name if needle.lower() == "left" else ordered[-1].name
    for monitor in monitors:
        if monitor.name.lower() == needle.lower() or monitor.id == needle:
            return monitor.name
    available = ", ".join(m.name for m in monitors)
    raise BackendError(f"No monitor named '{needle}'. Available: {available}.")


def strip_prefix(destination: str, prefix: str) -> str:
    """Accept both 'workspace:3' and a bare '3' so the caller can be sloppy."""
    value = str(destination or "").strip()
    if value.lower().startswith(prefix + ":"):
        return value.split(":", 1)[1].strip()
    return value


def find_workspace(destination: str, snapshot: Snapshot) -> str:
    """Turn a workspace number, name, or 'next'/'prev' into something usable."""
    needle = str(destination or "").strip()
    if not needle:
        raise BackendError("No workspace was named.")
    if needle.lower() in ("next", "prev", "previous", "last"):
        return needle.lower()
    if needle.isdigit() or (len(needle) > 1 and needle[1:].isdigit() and needle[0] in "+-"):
        return needle
    for workspace in snapshot.workspaces:
        if workspace.name == needle:
            return workspace.name
    named = [w.name for w in snapshot.workspaces]
    raise BackendError(
        f"No workspace named '{needle}'. Open workspaces: {', '.join(named) if named else 'none'}."
    )


def _guard(backend: DesktopBackend, op: str) -> None:
    capability = _OP_CAPABILITY.get(op)
    if capability and not backend.has(capability):
        raise Unsupported(backend.missing(capability))


def plan(backend: DesktopBackend, op: str, args: argparse.Namespace) -> dict:
    _guard(backend, op)
    snapshot = backend.snapshot()
    windows = snapshot.windows

    if op == "list":
        return {
            "ok": True,
            "op": op,
            "backend": backend.name,
            "windows": [w.brief() for w in windows[:MAX_WINDOWS]],
        }
    if not windows and op not in ("workspace", "monitor"):
        raise BackendError("There are no open windows.")

    if op == "focus":
        window = resolve(args.target, windows)
        return {
            "ok": True, "op": op, "id": window.id,
            "summary": f"Focus {window.label()}",
            "detail": f"workspace {window.workspace}",
            "requires_approval": True,
        }

    if op == "move":
        window = resolve(args.target, windows)
        destination = str(args.to or "").strip()
        if destination.lower() == "scratchpad":
            _guard(backend, "scratchpad")
            return {
                "ok": True, "op": "scratchpad", "id": window.id, "name": "minimized",
                "summary": f"Send {window.label()} to the scratchpad",
                "detail": "the special workspace", "requires_approval": True,
            }
        if destination.lower().startswith("monitor:"):
            monitor = find_monitor(destination.split(":", 1)[1], snapshot)
            return {
                "ok": True, "op": "movewindow", "id": window.id, "monitor": monitor,
                "summary": f"Move {window.label()} to {monitor}",
                "detail": f"from workspace {window.workspace}", "requires_approval": True,
            }
        workspace = find_workspace(strip_prefix(destination, "workspace"), snapshot)
        return {
            "ok": True, "op": "movewindow", "id": window.id, "workspace": workspace,
            "summary": f"Move {window.label()} to workspace {workspace}",
            "detail": f"from workspace {window.workspace}", "requires_approval": True,
        }

    if op == "swap":
        first = resolve(args.target, windows)
        second = resolve(args.other, windows)
        if first.id == second.id:
            raise BackendError("Those are the same window.")
        return {
            "ok": True, "op": "swap", "id": first.id, "other": second.id,
            "summary": f"Swap {first.label()} with {second.label()}",
            "detail": "their positions", "requires_approval": True,
        }

    if op == "resize":
        window = resolve(args.target, windows)
        width = int(args.width) if args.width else window.width
        height = int(args.height) if args.height else window.height
        width, height = clamp_size(width, height)
        return {
            "ok": True, "op": "resize", "id": window.id, "width": width, "height": height,
            "summary": f"Resize {window.label()} to {width}x{height}",
            "detail": f"from {window.width}x{window.height}", "requires_approval": True,
        }

    if op == "float":
        window = resolve(args.target, windows)
        action = str(args.to or "toggle").lower()
        if action not in ("toggle", "enable", "disable"):
            raise BackendError("Float action must be toggle, enable, or disable.")
        words = {"toggle": "Toggle floating on", "enable": "Float", "disable": "Unfloat"}[action]
        return {
            "ok": True, "op": "float", "id": window.id,
            "action": "enable" if action == "toggle" and window.floating else ("disable" if action == "toggle" else action),
            "summary": f"{words} {window.label()}",
            "detail": "floating" if window.floating else "tiled", "requires_approval": True,
        }

    if op == "fullscreen":
        window = resolve(args.target, windows)
        return {
            "ok": True, "op": "fullscreen", "id": window.id,
            "summary": f"Fullscreen {window.label()}", "detail": "toggle",
            "requires_approval": True,
        }

    if op in ("workspace", "monitor"):
        destination = str(args.to or args.target or "").strip()
        if destination.lower().startswith("monitor:") or op == "monitor":
            name = destination.split(":", 1)[1] if ":" in destination else destination
            monitor = find_monitor(name, snapshot)
            return {
                "ok": True, "op": "focusmonitor", "monitor": monitor,
                "summary": f"Switch to {monitor}", "detail": "", "requires_approval": True,
            }
        workspace = find_workspace(strip_prefix(destination, "workspace"), snapshot)
        return {
            "ok": True, "op": "workspace", "workspace": workspace,
            "summary": f"Switch to workspace {workspace}", "detail": "", "requires_approval": True,
        }

    if op == "scratchpad":
        if not args.target:
            return {
                "ok": True, "op": "togglescratchpad", "name": "minimized",
                "summary": "Show or hide the scratchpad", "detail": "special workspace",
                "requires_approval": True,
            }
        window = resolve(args.target, windows)
        return {
            "ok": True, "op": "scratchpad", "id": window.id, "name": "minimized",
            "summary": f"Send {window.label()} to the scratchpad",
            "detail": "the special workspace", "requires_approval": True,
        }

    if op == "tile":
        targets = [resolve(name, windows) for name in _names(args.target)]
        unique: list[Window] = []
        for window in targets:
            if all(window.id != seen.id for seen in unique):
                unique.append(window)
        if len(unique) < 2:
            raise BackendError("Tiling needs at least two different windows.")
        spread = {w.monitor for w in unique}
        if len(spread) > 1:
            raise BackendError(
                "Tiling needs its windows on one monitor, but these are spread over "
                + ", ".join(sorted(m for m in spread if m))
                + ". Move them together first."
            )
        direction = str(args.direction or "left").lower()
        if direction not in ("left", "right"):
            raise BackendError("Tile direction must be left or right.")
        listed = ", ".join(w.label() for w in unique)
        monitor_name = next((m for m in spread if m), "")
        return {
            "ok": True, "op": "tile",
            "ids": [w.id for w in unique], "monitor": monitor_name, "direction": direction,
            "summary": f"Tile {len(unique)} windows on {monitor_name}: {listed}",
            "detail": "floated into equal columns, unfloat to undo", "requires_approval": True,
        }

    raise BackendError(f"Unknown operation: {op}")


def run(backend: DesktopBackend, payload: dict) -> dict:
    """Perform one already-approved action, re-checking that it still makes sense."""
    op = str(payload.get("op") or "")
    try:
        _guard(backend, op)
    except Unsupported as error:
        return {"ok": False, "error": str(error)}
    snapshot = backend.snapshot()

    if op == "focus":
        return _live(snapshot, payload.get("id"), lambda window: backend.focus(window.id))

    if op == "movewindow":
        workspace = str(payload.get("workspace") or "")
        monitor = str(payload.get("monitor") or "")
        return _live(
            snapshot, payload.get("id"),
            lambda window: backend.move(window.id, workspace=workspace, monitor=monitor),
        )

    if op == "swap":
        return _live(
            snapshot, payload.get("id"),
            lambda window: backend.swap(window.id, str(payload.get("other") or "")),
        )

    if op == "resize":
        width, height = clamp_size(int(payload.get("width") or 0), int(payload.get("height") or 0))
        return _live(snapshot, payload.get("id"), lambda window: backend.resize(window.id, width, height))

    if op == "float":
        return _live(
            snapshot, payload.get("id"),
            lambda window: backend.set_floating(window.id, payload.get("action") == "enable"),
        )

    if op == "fullscreen":
        return _live(snapshot, payload.get("id"), lambda window: backend.set_fullscreen(window.id, True))

    if op == "workspace":
        backend.switch_workspace(str(payload.get("workspace") or ""))
        return {"ok": True}

    if op == "focusmonitor":
        backend.focus_monitor(str(payload.get("monitor") or ""))
        return {"ok": True}

    if op == "scratchpad":
        return _live(
            snapshot, payload.get("id"),
            lambda window: backend.scratchpad(window.id, str(payload.get("name") or "minimized")),
        )

    if op == "togglescratchpad":
        backend.scratchpad("", str(payload.get("name") or "minimized"))
        return {"ok": True}

    if op == "tile":
        return _tile(backend, payload)

    return {"ok": False, "error": f"Unknown operation: {op}"}


def _live(snapshot: Snapshot, window_id: object, action) -> dict:
    """Run an action only if the window it was planned for is still open."""
    wanted = str(window_id or "")
    if not snapshot.window(wanted):
        return {"ok": False, "error": "That window was closed before the change ran."}
    try:
        action(snapshot.window(wanted))
    except Unsupported as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True}


def _tile(backend: DesktopBackend, payload: dict) -> dict:
    """Float each window, then place it in an equal column across the work area."""
    ids = [str(value) for value in payload.get("ids") or []]
    monitor_wanted = str(payload.get("monitor") or "")
    if len(ids) < 2:
        return {"ok": False, "error": "Tiling needs at least two windows."}
    snapshot = backend.snapshot()
    missing = [value for value in ids if not snapshot.window(value)]
    if missing:
        return {"ok": False, "error": "One of those windows was closed before the tiling ran."}
    monitor = next((m for m in snapshot.live_monitors() if m.name == monitor_wanted), None)
    if monitor is None:
        return {"ok": False, "error": "That monitor is no longer available."}
    if any(snapshot.window(value).monitor != monitor.name for value in ids):
        return {"ok": False, "error": "Those windows are on different monitors now."}

    x0, y0, x1, y1 = monitor.work_area()
    count = len(ids)
    # n columns plus n-1 gaps have to fit exactly, or a strip is left over at one
    # edge. Dividing the total first and subtracting the gap afterwards does not.
    column = max((x1 - x0 - GAP * (count - 1)) // count, MIN_SIDE)
    for index, value in enumerate(ids):
        if str(payload.get("direction") or "left") == "right":
            left = x1 - (count - index) * column - (count - index - 1) * GAP
        else:
            left = x0 + index * (column + GAP)
        try:
            backend.set_floating(value, True)
            backend.resize(value, column, y1 - y0)
            backend.move_rect(value, left, y0)
        except Unsupported as error:
            return {"ok": False, "error": str(error)}
        except (OSError, BackendError) as error:
            return {"ok": False, "error": str(error)[:400]}
    return {"ok": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan and run window actions for the assistant.")
    parser.add_argument("--backend", default="", help="force a backend instead of detecting one")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan", help="read-only: resolve a window and return a proposal")
    plan_parser.add_argument("op", choices=list(PLANNABLE))
    plan_parser.add_argument("targets", nargs="*", default=[], help="window names, or several separated by commas")
    plan_parser.add_argument("--to", dest="to", default="")
    plan_parser.add_argument("--other", dest="other", default="")
    plan_parser.add_argument("--width", dest="width", type=int, default=0)
    plan_parser.add_argument("--height", dest="height", type=int, default=0)
    plan_parser.add_argument("--direction", dest="direction", default="left")

    run_parser = sub.add_parser("run", help="perform one approved action")
    run_parser.add_argument("payload", help="JSON object from a plan")
    return parser


def _names(target: object) -> list[str]:
    """Accept window names as a list or as one comma-separated string.

    The command line splits on commas before this point, but a tool calling
    ``plan`` directly may hand over a single string, and iterating that would
    resolve one window per character.
    """
    if isinstance(target, str):
        return [piece.strip() for piece in target.split(",") if piece.strip()]
    return [str(piece).strip() for piece in (target or []) if str(piece).strip()]


def split_targets(op: str, raw: list[str], other: str) -> tuple[object, str]:
    """Accept 'a b', 'a,b' or a mixture, and hand plan() the shape it expects."""
    parts: list[str] = []
    for chunk in list(raw) + ([other] if other else []):
        parts.extend(piece.strip() for piece in str(chunk).split(",") if piece.strip())
    if op == "tile":
        return parts, ""
    if op == "swap":
        if len(parts) < 2:
            raise BackendError("Swapping needs two windows.")
        return parts[0], parts[1]
    return (parts[0] if parts else ""), ""


def _select(force: str) -> DesktopBackend:
    backend, notes = detect(force)
    if not backend.available() and backend.name != "virtual":
        raise BackendError("; ".join(notes) or "No window manager backend is available.")
    return backend


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        backend = _select(args.backend)
    except BackendError as error:
        return fail(str(error))
    try:
        if args.command == "run":
            try:
                payload = json.loads(args.payload)
            except json.JSONDecodeError as error:
                return fail(f"The plan could not be read: {error}")
            if not isinstance(payload, dict):
                return fail("The plan was not an object.")
            return emit(run(backend, payload))
        if args.op == "tile" and not args.targets:
            return fail("Tiling needs at least two window names.")
        try:
            args.target, args.other = split_targets(str(args.op), list(args.targets), str(args.other))
        except BackendError as error:
            return fail(str(error))
        return emit(plan(backend, str(args.op), args))
    except (BackendError, OSError) as error:
        return fail(str(error))
    except Exception as error:  # noqa: BLE001 - the tool reports failures as data
        return fail(f"Unexpected failure: {error}")


if __name__ == "__main__":
    sys.exit(main())
