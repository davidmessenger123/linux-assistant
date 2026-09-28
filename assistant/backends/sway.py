"""Sway, through ``swaymsg``.

Sway's tree is the friendliest of any compositor's: one recursive JSON call
returns every window with its geometry, and every action is a criteria string
addressed at a container id. Geometry arrives already logical, so nothing needs
converting.

The tree mixes workspaces, containers and leaves, so the walk below only keeps
leaves that actually carry a window.
"""

from __future__ import annotations

from typing import Iterator

from .. import proc
from .base import (
    CAP_FLOAT,
    CAP_FOCUS,
    CAP_FULLSCREEN,
    CAP_MONITOR,
    CAP_PLACEMENT,
    CAP_MOVE,
    CAP_RESIZE,
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

SWAYMSG = "swaymsg"


def _leaves(node: dict, workspace: str = "", output: str = "") -> Iterator[tuple[dict, str, str]]:
    """Yield every window leaf in the tree with the workspace and output it sits on.

    Sway hangs floating windows off a sibling ``floating_nodes`` key at every
    level of the tree rather than nesting them among their siblings, so both
    keys are walked. They carry no workspace name of their own, which is why the
    walk falls back to the one inherited from the enclosing workspace.
    """
    kind = node.get("type")
    current_workspace = workspace
    current_output = output
    if kind == "output":
        current_workspace = str(node.get("name") or workspace)
        current_output = str(node.get("name") or output)
    elif kind == "workspace":
        current_workspace = str(node.get("name") or workspace)
    if kind in ("con", "floating_con") and (node.get("app_id") or node.get("window_properties") or node.get("pid")):
        yield node, current_workspace, current_output
        return
    for key in ("nodes", "floating_nodes"):
        for child in node.get(key) or []:
            if isinstance(child, dict):
                yield from _leaves(child, current_workspace, current_output)


class SwayBackend(DesktopBackend):
    name = "sway"
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
            CAP_TILE,
            CAP_PLACEMENT,
        }
    )

    def available(self) -> bool:
        if not proc.have(SWAYMSG):
            return False
        try:
            proc.run([SWAYMSG, "-t", "get_version"], timeout=5, check=False)
        except (proc.ToolMissing, OSError):
            return False
        return True

    def snapshot(self) -> Snapshot:
        tree = proc.run_json([SWAYMSG, "-t", "get_tree"]) or {}
        outputs = proc.run_json([SWAYMSG, "-t", "get_outputs"]) or []
        workspaces = proc.run_json([SWAYMSG, "-t", "get_workspaces"]) or []
        nodes = tree.get("nodes") if isinstance(tree, dict) else None
        windows: list[Window] = []
        for root in nodes or []:
            if isinstance(root, dict):
                for node, workspace, output in _leaves(root):
                    windows.append(self._window(node, workspace, output))
        return Snapshot(
            windows=windows,
            monitors=[self._monitor(item) for item in outputs if isinstance(item, dict)],
            workspaces=[self._workspace(item) for item in workspaces if isinstance(item, dict)],
        )

    @staticmethod
    def _window(node: dict, workspace: str, output: str) -> Window:
        rect = node.get("rect") or {}
        properties = node.get("window_properties") or {}
        return Window(
            id=str(node.get("id") if node.get("id") is not None else ""),
            app_id=str(node.get("app_id") or properties.get("class") or ""),
            title=str(properties.get("title") or ""),
            workspace=workspace,
            monitor=output,
            x=int(rect.get("x") or 0),
            y=int(rect.get("y") or 0),
            width=int(rect.get("width") or 0),
            height=int(rect.get("height") or 0),
            floating=node.get("is_floating") is True or node.get("type") == "floating_con",
            focused=node.get("is_focused") is True,
        )

    @staticmethod
    def _monitor(output: dict) -> Monitor:
        rect = output.get("rect") or {}
        return Monitor(
            id=str(output.get("name") or ""),
            name=str(output.get("name") or ""),
            x=int(rect.get("x") or 0),
            y=int(rect.get("y") or 0),
            width=int(rect.get("width") or 0),
            height=int(rect.get("height") or 0),
            scale=float(output.get("scale") or 1) or 1.0,
            focused=output.get("focused") is True,
            disabled=output.get("active") is not True,
        )

    @staticmethod
    def _workspace(item: dict) -> Workspace:
        return Workspace(
            name=str(item.get("name") or ""),
            monitor=str(item.get("output") or ""),
            active=item.get("focused") is True,
        )

    @staticmethod
    def _criteria(window_id: str) -> str:
        return f"[con_id={int(window_id)}]" if str(window_id).isdigit() else f"[id={window_id}]"

    def _command(self, target: str, *words: str) -> None:
        proc.run([SWAYMSG, "-t", "run_command", f"{target} {(' '.join(words))}".strip()], check=True)

    def focus(self, window_id: str) -> None:
        self._command(self._criteria(window_id), "focus")

    def move(self, window_id: str, workspace: str = "", monitor: str = "") -> None:
        target = self._criteria(window_id)
        if workspace:
            self._command(target, "move", "container", "to", "workspace", workspace)
            return
        if monitor:
            self._command(target, "move", "container", "to", "output", monitor)
            return
        raise Unsupported("Moving a window in sway needs a workspace or an output.")

    def resize(self, window_id: str, width: int, height: int) -> None:
        self._command(
            self._criteria(window_id),
            "resize",
            "set",
            "width",
            str(int(width)),
            "height",
            str(int(height)),
        )

    def move_rect(self, window_id: str, x: int, y: int) -> None:
        self._command(self._criteria(window_id), "move", "position", str(int(x)), str(int(y)))

    def set_floating(self, window_id: str, floating: bool) -> None:
        self._command(self._criteria(window_id), "floating", "enable" if floating else "disable")

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        self._command(self._criteria(window_id), "fullscreen", "enable" if fullscreen else "disable")

    def switch_workspace(self, workspace: str) -> None:
        proc.run([SWAYMSG, "-t", "run_command", f"workspace {workspace}"], check=True)

    def focus_monitor(self, monitor: str) -> None:
        proc.run([SWAYMSG, "-t", "run_command", f"focus output {monitor}"], check=True)

    def swap(self, first: str, second: str) -> None:
        self._command(self._criteria(first), "swap", self._criteria(second))
