"""X11, through ``wmctrl``, ``xprop`` and ``xrandr``.

This is the backend that decides whether the assistant is portable at all: it
runs under any X11 window manager, and GNOME and KDE both still expose one
through XWayland, so a Wayland session that would otherwise have no usable
backend can fall back here.

Two honest limitations are encoded as missing capabilities rather than worked
around:

* **There is no float state in X11.** No standard atom reports it, so floating
  cannot be toggled and the backend does not claim it.
* **There is no per-monitor focus.** ``focus_monitor`` activates a window that
  already sits on the requested output, which is the closest a plain X11
  server allows.

Tiling still works because it is built from geometry requests, which i3, Openbox
and friends honour for non-maximised windows.
"""

from __future__ import annotations

import dataclasses
import re
import shutil

from .. import proc
from .base import (
    CAP_FOCUS,
    CAP_FULLSCREEN,
    CAP_MONITOR,
    CAP_PLACEMENT,
    CAP_MOVE,
    CAP_RESIZE,
    CAP_TILE,
    CAP_WORKSPACE,
    DesktopBackend,
    Monitor,
    Snapshot,
    Unsupported,
    Window,
    Workspace,
)

WMCTRL = "wmctrl"
XPROP = "xprop"
XRANDR = "xrandr"
XDOTOOL = "xdotool"

_LISTING = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s+(-?\d+)\s+(\S.*?)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s*$")
_XRANDR_GEOMETRY = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")

# wmctrl reports desktops -1 and -2 for sticky and "on all desktops" windows, and
# the window manager's own desktop window shows up like any other. Neither is
# something the assistant should offer to act on.
_SKIP_DESKTOP = {"-1", "-2"}
_WM_WINDOWS = {"desktop", "nautilus-desktop", "plasmashell", "xroot", "window manager"}


class X11Backend(DesktopBackend):
    name = "x11"
    capabilities = frozenset(
        {
            CAP_FOCUS,
            CAP_MOVE,
            CAP_RESIZE,
            CAP_FULLSCREEN,
            CAP_WORKSPACE,
            CAP_MONITOR,
            CAP_TILE,
            CAP_PLACEMENT,
        }
    )

    def available(self) -> bool:
        import os

        if not os.environ.get("DISPLAY"):
            return False
        return proc.have(WMCTRL)

    def snapshot(self) -> Snapshot:
        workspaces = self._workspaces()
        monitors = self._monitors()
        windows = self._windows(workspaces, monitors)
        focused = self._focused_id()
        if focused:
            windows = [
                dataclasses.replace(window, focused=True) if window.id == focused else window
                for window in windows
            ]
        return Snapshot(windows=windows, monitors=monitors, workspaces=workspaces)

    def _focused_id(self) -> str:
        if not proc.have(XDOTOOL):
            return ""
        try:
            return proc.run([XDOTOOL, "getactivewindow"], timeout=5, check=False).strip()
        except (proc.ToolMissing, OSError):
            return ""

    def _windows(self, workspaces: list[Workspace], monitors: list[Monitor]) -> list[Window]:
        try:
            listing = proc.run([WMCTRL, "-lG"], check=False)
        except (proc.ToolMissing, OSError):
            return []
        active = next((w.name for w in workspaces if w.active), "")
        windows: list[Window] = []
        for line in listing.splitlines():
            match = _LISTING.match(line)
            if not match:
                continue
            window_id, desktop, title, x, y, width, height = match.groups()
            title = title.strip()
            if desktop in _SKIP_DESKTOP:
                continue
            app_id = self._app_id(window_id)
            if title.lower() in _WM_WINDOWS or app_id.lower() in _WM_WINDOWS:
                continue
            windows.append(
                Window(
                    id=window_id.lower(),
                    app_id=app_id,
                    title=title,
                    workspace=self._workspace_name(desktop, workspaces, active),
                    monitor=self._monitor_at(int(x), int(y), monitors),
                    x=int(x),
                    y=int(y),
                    width=int(width),
                    height=int(height),
                    floating=False,
                    focused=False,
                    minimized=False,
                )
            )
        return windows

    @staticmethod
    def _app_id(window_id: str) -> str:
        """Read WM_CLASS, which is the only app name X11 guarantees."""
        if not proc.have(XPROP):
            return ""
        try:
            raw = proc.run([XPROP, "-id", window_id, "WM_CLASS"], timeout=5, check=False)
        except (proc.ToolMissing, OSError):
            return ""
        match = re.search(r'=\s*(.+)\s*$', raw)
        if not match:
            return ""
        for token in re.findall(r'"([^"]*)"', match.group(1)):
            if token:
                return token
        return ""

    @staticmethod
    def _workspace_name(desktop: str, workspaces: list[Workspace], active: str) -> str:
        if desktop in ("-1", "-2"):
            return ""
        for workspace in workspaces:
            if workspace.name == desktop:
                return workspace.name
        return active

    @staticmethod
    def _monitor_at(x: int, y: int, monitors: list[Monitor]) -> str:
        for monitor in monitors:
            if monitor.x <= x < monitor.x + monitor.width and monitor.y <= y < monitor.y + monitor.height:
                return monitor.name
        return monitors[0].name if monitors else ""

    def _workspaces(self) -> list[Workspace]:
        try:
            raw = proc.run([WMCTRL, "-d"], check=False)
        except (proc.ToolMissing, OSError):
            return []
        workspaces: list[Workspace] = []
        for line in raw.splitlines():
            parts = line.split()
            if not parts or not parts[0].isdigit():
                continue
            workspaces.append(Workspace(name=parts[0], active="*" in line))
        return workspaces

    def _monitors(self) -> list[Monitor]:
        """Parse the ``connected`` lines of xrandr.

        The framebuffer size and position sit on the connected line itself, not
        on the indented mode list below it. The scale is left at 1 because
        deriving it means comparing the framebuffer size against the mode's
        native size, and getting that wrong moves tiled windows; a slightly
        off tile on a fallback backend is a better trade than a wrong one.
        """
        if not proc.have(XRANDR):
            return []
        try:
            raw = proc.run([XRANDR, "--query"], check=False)
        except (proc.ToolMissing, OSError):
            return []
        monitors: list[Monitor] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("Screen "):
                continue
            fields = stripped.split()
            if len(fields) < 2 or fields[1] not in ("connected", "disconnected"):
                continue
            name, state = fields[0], fields[1]
            if state == "disconnected":
                continue
            geometry = _XRANDR_GEOMETRY.search(stripped)
            if not geometry:
                continue
            width, height, x, y = (int(value) for value in geometry.groups())
            monitors.append(
                Monitor(
                    id=name,
                    name=name,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    scale=1.0,
                    focused="primary" in fields,
                )
            )
        return monitors

    @staticmethod
    def _command(*words: str) -> None:
        proc.run([WMCTRL, *words], check=True)

    def focus(self, window_id: str) -> None:
        self._command("-i", "-a", window_id)

    def move(self, window_id: str, workspace: str = "", monitor: str = "") -> None:
        for monitor_item in self._monitors():
            if monitor and monitor_item.name == monitor:
                self._command("-i", "-r", window_id, "-e", "0", str(monitor_item.x), str(monitor_item.y), "-1", "-1")
                return
        if workspace and not monitor:
            self._command("-i", "-r", window_id, "-t", workspace)
            return
        raise Unsupported("Moving a window under X11 needs a monitor or a desktop number.")

    def resize(self, window_id: str, width: int, height: int) -> None:
        self._command("-i", "-r", window_id, "-e", "0", "-1", "-1", str(int(width)), str(int(height)))

    def move_rect(self, window_id: str, x: int, y: int) -> None:
        self._command("-i", "-r", window_id, "-e", "0", str(int(x)), str(int(y)), "-1", "-1")

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        self._command("-i", "-r", window_id, "-b", "add,fullscreen" if fullscreen else "remove,fullscreen")

    def switch_workspace(self, workspace: str) -> None:
        self._command("-s", workspace)

    def focus_monitor(self, monitor: str) -> None:
        for window in self._windows(self._workspaces(), self._monitors()):
            if window.monitor == monitor:
                self.focus(window.id)
                return
        raise Unsupported(f"No open window is on {monitor}, so X11 cannot focus that output.")

    def launch(self, command: list[str]) -> None:
        import subprocess

        subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def has_display() -> bool:
    import os

    return bool(os.environ.get("DISPLAY")) and bool(shutil.which(WMCTRL))
