"""An in-memory backend that records what it was asked to do.

Two uses. In tests it is the only backend that runs, so planner behaviour can be
checked without a live compositor. In production it is the fallback when no
window manager can be identified, which turns "the assistant cannot see your
windows" into a clear message instead of a crash.
"""

from __future__ import annotations

from .base import (
    CAP_FLOAT,
    CAP_FOCUS,
    CAP_FULLSCREEN,
    CAP_MONITOR,
    CAP_MOVE,
    CAP_RESIZE,
    CAP_SCRATCHPAD,
    CAP_SWAP,
    CAP_TILE,
    CAP_PLACEMENT,
    CAP_WORKSPACE,
    DesktopBackend,
    Monitor,
    Snapshot,
    Window,
    Workspace,
)


class VirtualBackend(DesktopBackend):
    name = "virtual"
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

    def __init__(
        self,
        windows: list[Window] | None = None,
        monitors: list[Monitor] | None = None,
        workspaces: list[Workspace] | None = None,
        available: bool = True,
    ) -> None:
        self.windows = list(windows or [])
        self.monitors = list(
            monitors
            or [Monitor(id="0", name="headless-1", width=1920, height=1080, focused=True)]
        )
        self.workspaces = list(workspaces or [Workspace(name="1", active=True)])
        self.calls: list[tuple] = []
        self._available = available

    def available(self) -> bool:
        return self._available

    def snapshot(self) -> Snapshot:
        return Snapshot(
            windows=list(self.windows),
            monitors=list(self.monitors),
            workspaces=list(self.workspaces),
        )

    def _record(self, *call) -> None:
        self.calls.append(call)

    def focus(self, window_id: str) -> None:
        self._record("focus", window_id)
        self.windows = [Window(**{**_as_dict(w), "focused": w.id == window_id}) for w in self.windows]

    def move(self, window_id: str, workspace: str = "", monitor: str = "") -> None:
        self._record("move", window_id, workspace, monitor)
        self.windows = [
            Window(
                **{
                    **_as_dict(w),
                    "workspace": workspace or w.workspace,
                    "monitor": monitor or w.monitor,
                }
            )
            for w in self.windows
        ]

    def resize(self, window_id: str, width: int, height: int) -> None:
        self._record("resize", window_id, width, height)
        self.windows = [
            Window(
                **{
                    **_as_dict(w),
                    "width": int(width) if w.id == window_id else w.width,
                    "height": int(height) if w.id == window_id else w.height,
                }
            )
            for w in self.windows
        ]

    def move_rect(self, window_id: str, x: int, y: int) -> None:
        self._record("move_rect", window_id, x, y)

    def set_floating(self, window_id: str, floating: bool) -> None:
        self._record("set_floating", window_id, floating)
        self.windows = [Window(**{**_as_dict(w), "floating": bool(floating)}) for w in self.windows]

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        self._record("set_fullscreen", window_id, fullscreen)

    def switch_workspace(self, workspace: str) -> None:
        self._record("switch_workspace", workspace)
        self.workspaces = [
            Workspace(**{**_as_dict(w), "active": w.name == workspace}) for w in self.workspaces
        ]

    def focus_monitor(self, monitor: str) -> None:
        self._record("focus_monitor", monitor)
        self.monitors = [
            Monitor(**{**_as_dict(m), "focused": m.name == monitor}) for m in self.monitors
        ]

    def swap(self, first: str, second: str) -> None:
        self._record("swap", first, second)

    def scratchpad(self, window_id: str = "", name: str = "minimized") -> None:
        self._record("scratchpad", window_id, name)

    def launch(self, command: list[str]) -> None:
        self._record("launch", list(command))


def _as_dict(value) -> dict:
    import dataclasses

    return dataclasses.asdict(value)
