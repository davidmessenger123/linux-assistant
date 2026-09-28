"""The desktop-control contract every window manager backend implements.

A backend is the only code in this project that knows a window manager exists.
Everything above it works in terms of the dataclasses here and the capability
names in ``CAPABILITIES``, so adding a compositor means adding one module and
nothing else.

Two rules keep the layer honest:

* **Geometry is always logical.** Backends convert from whatever their
  compositor reports, which is not the same thing on every one of them. Hyprland
  reports monitor sizes in physical pixels but positions in logical pixels, and
  getting that wrong silently moves windows to the wrong place.
* **Unsupported operations raise, they do not fall back.** ``has()`` is the
  single source of truth for what a backend can do, and the planner refuses to
  build a proposal it cannot honour rather than emitting one that fails later.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

CAP_FOCUS = "focus"
CAP_MOVE = "move"
CAP_SWAP = "swap"
CAP_RESIZE = "resize"
CAP_FLOAT = "float"
CAP_FULLSCREEN = "fullscreen"
CAP_WORKSPACE = "workspace"
CAP_MONITOR = "monitor"
CAP_SCRATCHPAD = "scratchpad"
CAP_TILE = "tile"
CAP_PLACEMENT = "placement"

# What a window manager integration is allowed to claim. Screen capture, typing,
# clicking and launching are not window management and live in assistant.io,
# which detects them separately -- a compositor that can manage windows has no
# reason to be good at any of those.
WINDOW_CAPABILITIES = (
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
)

# Operations that are built from other operations rather than issued to the
# compositor directly. A backend only claims them when every part is present.
COMPOSITE_CAPABILITIES: dict[str, frozenset[str]] = {
    CAP_TILE: frozenset({CAP_FLOAT, CAP_RESIZE, CAP_PLACEMENT}),
    CAP_SCRATCHPAD: frozenset({CAP_MOVE}),
}

MIN_WINDOW_WIDTH = 200
MIN_WINDOW_HEIGHT = 150
MAX_WINDOW_EDGE = 20000
MAX_TITLE = 60
MAX_WINDOWS = 40


class BackendError(RuntimeError):
    """A backend could not do what was asked. The message is shown to the user."""


class Unsupported(BackendError):
    """The active backend does not implement the requested operation at all."""


@dataclass(frozen=True)
class Window:
    """One open window, in logical coordinates.

    ``id`` is opaque and only meaningful to the backend that produced it. It is
    handed back verbatim to address the window later, so it has to survive a
    round trip through the plan JSON and a separate process.
    """

    id: str
    app_id: str = ""
    title: str = ""
    workspace: str = ""
    monitor: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    floating: bool = False
    focused: bool = False
    minimized: bool = False

    def label(self) -> str:
        """A short human description, used when a target is ambiguous."""
        title = self.title.strip()
        return f"{self.app_id} ({title[:MAX_TITLE]})" if title else (self.app_id or "window")

    def brief(self) -> dict[str, Any]:
        """The shape offered to the model, kept small on purpose."""
        return {
            "id": self.id,
            "app_id": self.app_id,
            "title": self.title[:MAX_TITLE],
            "workspace": self.workspace,
            "monitor": self.monitor,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "floating": self.floating,
        }


@dataclass(frozen=True)
class Monitor:
    """One output. Geometry is logical and the origin is the desktop's."""

    id: str
    name: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    scale: float = 1.0
    focused: bool = False
    reserved: tuple[int, int, int, int] = (0, 0, 0, 0)
    disabled: bool = False

    def work_area(self) -> tuple[int, int, int, int]:
        """The usable rectangle, with any reserved edges (panels, docks) removed."""
        left, top, right, bottom = self.reserved
        x0 = self.x + left
        y0 = self.y + top
        x1 = x0 + max(self.width - left - right, MIN_WINDOW_WIDTH)
        y1 = y0 + max(self.height - top - bottom, MIN_WINDOW_HEIGHT)
        return x0, y0, x1, y1


@dataclass(frozen=True)
class Workspace:
    """A workspace, tagged so a compositor's hidden workspaces can be left out."""

    name: str
    monitor: str = ""
    active: bool = False
    count: int = 0
    special: bool = False


@dataclass(frozen=True)
class Snapshot:
    """Everything the planner is allowed to read before it proposes anything."""

    windows: list[Window] = field(default_factory=list)
    monitors: list[Monitor] = field(default_factory=list)
    workspaces: list[Workspace] = field(default_factory=list)

    def window(self, window_id: str) -> Window | None:
        for candidate in self.windows:
            if candidate.id == window_id:
                return candidate
        return None

    def focused_monitor(self) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.focused:
                return monitor
        return None

    def live_monitors(self) -> list[Monitor]:
        return [m for m in self.monitors if not m.disabled]


class DesktopBackend(abc.ABC):
    """What every window manager integration has to provide."""

    name: str = "unknown"
    capabilities: frozenset[str] = frozenset()

    def has(self, capability: str) -> bool:
        """Whether ``capability`` is genuinely usable, composites included.

        A composite is checked even when it is declared directly, so a backend
        cannot claim ``tile`` while missing the parts tile is made of. That is
        what keeps the advertised tool surface honest.
        """
        required = COMPOSITE_CAPABILITIES.get(capability)
        if required is not None:
            return required <= self.capabilities
        return capability in self.capabilities

    def require(self, *capabilities: str) -> None:
        missing = [c for c in capabilities if not self.has(c)]
        if missing:
            raise Unsupported(
                f"{self.name} cannot {' or '.join(missing)}. "
                f"It supports: {', '.join(sorted(self.capabilities)) or 'nothing'}."
            )

    def missing(self, capability: str) -> str:
        """A user-facing explanation for one operation being unavailable."""
        required = COMPOSITE_CAPABILITIES.get(capability, frozenset({capability}))
        absent = sorted(required - self.capabilities)
        return f"{self.name} cannot {capability} (needs {' and '.join(absent)})."
    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this backend is usable in the running session."""

    @abc.abstractmethod
    def snapshot(self) -> Snapshot:
        """Read current windows, monitors and workspaces. Must not change anything."""

    def focus(self, window_id: str) -> None:
        raise Unsupported(self.missing(CAP_FOCUS))

    def move(self, window_id: str, workspace: str = "", monitor: str = "") -> None:
        raise Unsupported(self.missing(CAP_MOVE))

    def resize(self, window_id: str, width: int, height: int) -> None:
        raise Unsupported(self.missing(CAP_RESIZE))

    def set_floating(self, window_id: str, floating: bool) -> None:
        raise Unsupported(self.missing(CAP_FLOAT))

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        raise Unsupported(self.missing(CAP_FULLSCREEN))

    def switch_workspace(self, workspace: str) -> None:
        raise Unsupported(self.missing(CAP_WORKSPACE))

    def focus_monitor(self, monitor: str) -> None:
        raise Unsupported(self.missing(CAP_MONITOR))

    def swap(self, first: str, second: str) -> None:
        raise Unsupported(self.missing(CAP_SWAP))

    def move_rect(self, window_id: str, x: int, y: int) -> None:
        """Place a window at an exact point. Backends without it fall back to move."""
        raise Unsupported(self.missing(CAP_PLACEMENT))

    def scratchpad(self, window_id: str = "", name: str = "minimized") -> None:
        raise Unsupported(self.missing(CAP_SCRATCHPAD))

    def launch(self, command: list[str]) -> None:
        raise Unsupported(self.missing(CAP_LAUNCH))


def clamp_size(width: int, height: int) -> tuple[int, int]:
    """Keep a requested size inside what any window manager will accept."""
    return (
        max(min(int(width), MAX_WINDOW_EDGE), MIN_WINDOW_WIDTH),
        max(min(int(height), MAX_WINDOW_EDGE), MIN_WINDOW_HEIGHT),
    )
