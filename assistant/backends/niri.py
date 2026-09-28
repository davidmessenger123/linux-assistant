"""Niri, through ``niri msg``.

Niri reports state as three flat JSON lists rather than a tree, and every
action is a subcommand of ``niri msg action`` taking flags rather than
positional words. Window ids are plain integers, which makes the opaque id used
across this project a simple stringified number.
"""

from __future__ import annotations

from .. import proc
from .base import (
    CAP_FLOAT,
    CAP_FOCUS,
    CAP_FULLSCREEN,
    CAP_MONITOR,
    CAP_PLACEMENT,
    CAP_MOVE,
    CAP_RESIZE,
    CAP_SCRATCHPAD,
    CAP_SWAP,
    CAP_TILE,
    CAP_WORKSPACE,
    DesktopBackend,
    Monitor,
    Snapshot,
    Unsupported,
    Window,
    Workspace,
)

NIRI = "niri"


class NiriBackend(DesktopBackend):
    name = "niri"
    capabilities = frozenset(
        {
            CAP_FOCUS,
            CAP_MOVE,
            CAP_SWAP,
            CAP_RESIZE,
            CAP_FLOAT,
            CAP_FULLSCREEN,
            CAP_WORKSPACE,
            CAP_MONITOR,
            CAP_SCRATCHPAD,
            CAP_TILE,
            CAP_PLACEMENT,
        }
    )

    def available(self) -> bool:
        if not proc.have(NIRI):
            return False
        try:
            proc.run([NIRI, "msg", "--json", "outputs"], timeout=5, check=False)
        except (proc.ToolMissing, OSError):
            return False
        return True

    def snapshot(self) -> Snapshot:
        windows = proc.run_json([NIRI, "msg", "--json", "windows"]) or {}
        outputs = proc.run_json([NIRI, "msg", "--json", "outputs"]) or {}
        workspaces = proc.run_json([NIRI, "msg", "--json", "workspaces"]) or {}
        return Snapshot(
            windows=[self._window(item) for item in _items(windows) if isinstance(item, dict)],
            monitors=[self._output(item) for item in _items(outputs) if isinstance(item, dict)],
            workspaces=[self._workspace(item) for item in _items(workspaces) if isinstance(item, dict)],
        )

    @staticmethod
    def _window(item: dict) -> Window:
        return Window(
            id=str(item.get("id") if item.get("id") is not None else ""),
            app_id=str(item.get("app_id") or ""),
            title=str(item.get("title") or ""),
            workspace=str(item.get("workspace_id") if item.get("workspace_id") is not None else ""),
            monitor=_output_name(item.get("output")),
            x=0,
            y=0,
            width=0,
            height=0,
            floating=item.get("is_floating") is True,
            focused=item.get("is_focused") is True,
        )

    @staticmethod
    def _output(item: dict) -> Monitor:
        logical = item.get("logical") or {}
        current = item.get("current_workspace") or {}
        return Monitor(
            id=str(item.get("name") or ""),
            name=str(item.get("name") or ""),
            x=int(logical.get("x") or 0),
            y=int(logical.get("y") or 0),
            width=int(logical.get("width") or 0),
            height=int(logical.get("height") or 0),
            scale=float(item.get("scale") or 1) or 1.0,
            focused=item.get("is_focused") is True,
            disabled=current.get("is_focused") is not True and not item.get("current_mode"),
        )

    @staticmethod
    def _workspace(item: dict) -> Workspace:
        return Workspace(
            name=str(item.get("id") if item.get("id") is not None else ""),
            monitor=_output_name(item.get("output")),
            active=item.get("is_active") is True or item.get("is_focused") is True,
        )

    def _action(self, *words: str) -> None:
        proc.run([NIRI, "msg", "action", *words], check=True)

    def focus(self, window_id: str) -> None:
        self._action("focus-window", "--id", str(window_id))

    def move(self, window_id: str, workspace: str = "", monitor: str = "") -> None:
        if workspace:
            reference = workspace if workspace.lstrip("-").isdigit() else None
            if reference is not None:
                self._action("move-window-to-workspace", "--id", str(window_id), "--reference", reference)
            else:
                self._action("move-window-to-workspace", "--id", str(window_id), workspace)
            return
        if monitor:
            self._action("move-window-to-output", "--id", str(window_id), monitor)
            return
        raise Unsupported("Moving a window in niri needs a workspace or an output.")

    def resize(self, window_id: str, width: int, height: int) -> None:
        self._action("set-window-width", "--id", str(window_id), "--fixed", str(int(width)))
        self._action("set-window-height", "--id", str(window_id), "--fixed", str(int(height)))

    def set_floating(self, window_id: str, floating: bool) -> None:
        self._action("toggle-window-floating", "--id", str(window_id))

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        self._action("toggle-window-fullscreen", "--id", str(window_id))

    def switch_workspace(self, workspace: str) -> None:
        if workspace.lstrip("-").isdigit():
            self._action("focus-workspace", "--reference", workspace)
        else:
            self._action("focus-workspace", workspace)

    def focus_monitor(self, monitor: str) -> None:
        self._action("focus-output", monitor)

    def swap(self, first: str, second: str) -> None:
        self._action("swap-window", "--id", str(first), "--with", str(second))

    def scratchpad(self, window_id: str = "", name: str = "minimized") -> None:
        if window_id:
            raise Unsupported("Niri has no special workspace, so a window cannot be sent to one.")
        self._action("toggle-overview")


def _items(payload: object) -> list[dict]:
    """Niri wraps its lists, but not consistently across versions."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("windows", "outputs", "workspaces"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _output_name(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")
