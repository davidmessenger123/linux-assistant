"""Hyprland, through ``hyprctl``.

Two Hyprland dialects are in the wild. Recent builds expose a Lua dispatcher
reached with ``hyprctl eval "hl.dispatch(...)"``; older ones take plain keyword
commands with ``hyprctl dispatch``. The original assistant only spoke the first
one, which is why it broke the moment the compositor was upgraded. Every
operation here carries both forms and picks the right one at runtime.

The other Hyprland wart handled here: monitors report their size in physical
pixels but their position in logical ones, so sizes are divided by the output
scale on the way in.
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

HYPRCTL = "hyprctl"

_lua_dispatch: bool | None = None


def _quote(value: object) -> str:
    """Quote a value for the Lua dispatcher. Non-ASCII becomes decimal escapes."""
    out = ['"']
    for character in str(value):
        if character == '"':
            out.append('\\"')
        elif character == "\\":
            out.append("\\\\")
        elif ord(character) < 32 or ord(character) > 126:
            out.append("\\%d" % ord(character))
        else:
            out.append(character)
    out.append('"')
    return "".join(out)


def _address(window_id: str) -> str:
    return "address:" + str(window_id)


def _uses_lua() -> bool:
    """Whether this build wants Lua, decided by asking once and remembering."""
    global _lua_dispatch
    if _lua_dispatch is None:
        try:
            _lua_dispatch = proc.run([HYPRCTL, "eval", "1 + 1"], timeout=5, check=False).strip() == "2"
        except (proc.ToolMissing, OSError):
            _lua_dispatch = False
    return _lua_dispatch


def _either(lua: str, words: list[str]) -> None:
    """Run one dispatcher call in whichever dialect this build speaks."""
    if not _uses_lua():
        proc.run([HYPRCTL, "dispatch", *words], check=True)
        return
    output = proc.run([HYPRCTL, "eval", "hl.dispatch(" + lua + ")"], check=False).strip()
    if output.lower().startswith("error"):
        first = output.splitlines()[0].strip() if output else ""
        raise OSError(first[:300] or "hyprctl reported an error")


class HyprlandBackend(DesktopBackend):
    name = "hyprland"
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
        if not proc.have(HYPRCTL):
            return False
        try:
            proc.run([HYPRCTL, "version"], timeout=5, check=False)
        except (proc.ToolMissing, OSError):
            return False
        return True

    def snapshot(self) -> Snapshot:
        clients = proc.run_json([HYPRCTL, "-j", "clients"]) or []
        monitors = proc.run_json([HYPRCTL, "-j", "monitors"]) or []
        workspaces = proc.run_json([HYPRCTL, "-j", "workspaces"]) or []
        if not isinstance(clients, list) or not isinstance(monitors, list) or not isinstance(workspaces, list):
            raise OSError("hyprctl returned an unexpected shape")
        return Snapshot(
            windows=[self._window(client) for client in clients if isinstance(client, dict)],
            monitors=[self._monitor(monitor) for monitor in monitors if isinstance(monitor, dict)],
            workspaces=[self._workspace(item) for item in workspaces if isinstance(item, dict)],
        )

    @staticmethod
    def _window(client: dict) -> Window:
        at = client.get("at") or [0, 0]
        size = client.get("size") or [0, 0]
        workspace = client.get("workspace") or {}
        return Window(
            id=str(client.get("address") or ""),
            app_id=str(client.get("class") or ""),
            title=str(client.get("title") or ""),
            workspace=str(workspace.get("name") or ""),
            monitor=str(client.get("monitor") if client.get("monitor") is not None else ""),
            x=int(at[0]),
            y=int(at[1]),
            width=int(size[0]),
            height=int(size[1]),
            floating=client.get("floating") == 1,
            focused=client.get("focused") is True,
            minimized=client.get("hidden") is True,
        )

    @staticmethod
    def _monitor(monitor: dict) -> Monitor:
        scale = float(monitor.get("scale") or 1) or 1.0
        reserved = monitor.get("reserved") or [0, 0, 0, 0]
        if not isinstance(reserved, list) or len(reserved) != 4:
            reserved = [0, 0, 0, 0]
        return Monitor(
            id=str(monitor.get("id") if monitor.get("id") is not None else ""),
            name=str(monitor.get("name") or ""),
            x=int(monitor.get("x") or 0),
            y=int(monitor.get("y") or 0),
            width=int(round(float(monitor.get("width") or 0) / scale)),
            height=int(round(float(monitor.get("height") or 0) / scale)),
            scale=scale,
            focused=monitor.get("focused") is True,
            reserved=(int(reserved[0]), int(reserved[1]), int(reserved[2]), int(reserved[3])),
            disabled=monitor.get("disabled") is True,
        )

    @staticmethod
    def _workspace(item: dict) -> Workspace:
        name = str(item.get("name") or "")
        return Workspace(
            name=name,
            monitor=str(item.get("monitor") if item.get("monitor") is not None else ""),
            active=item.get("active") is True,
            count=int(item.get("windows") or 0),
            special=name.startswith("special:"),
        )

    def focus(self, window_id: str) -> None:
        _either(
            f"hl.dsp.focus({{ window = {_quote(_address(window_id))} }})",
            ["focuswindow", _address(window_id)],
        )

    def move(self, window_id: str, workspace: str = "", monitor: str = "") -> None:
        if monitor and not workspace:
            _either(
                f"hl.dsp.window.move({{ monitor = {_quote(monitor)}, follow = true, window = {_quote(_address(window_id))} }})",
                ["movewindow", _address(window_id), f"monitor:{monitor}"],
            )
        elif workspace:
            _either(
                f"hl.dsp.window.move({{ workspace = {_quote(workspace)}, follow = true, window = {_quote(_address(window_id))} }})",
                ["movewindow", _address(window_id), workspace],
            )
        else:
            raise Unsupported("Moving a window needs a workspace or a monitor to move it to.")

    def resize(self, window_id: str, width: int, height: int) -> None:
        _either(
            f"hl.dsp.window.resize({{ x = {int(width)}, y = {int(height)}, window = {_quote(_address(window_id))} }})",
            ["resizewindowpixel", "exact", str(int(width)), str(int(height))],
        )

    def move_rect(self, window_id: str, x: int, y: int) -> None:
        _either(
            f"hl.dsp.window.move({{ x = {int(x)}, y = {int(y)}, window = {_quote(_address(window_id))} }})",
            ["movewindowpixel", "exact", str(int(x)), str(int(y))],
        )

    def set_floating(self, window_id: str, floating: bool) -> None:
        if floating:
            _either(
                f'hl.dsp.window.float({{ action = "enable", window = {_quote(_address(window_id))} }})',
                ["float"],
            )
        else:
            _either(
                f'hl.dsp.window.float({{ action = "disable", window = {_quote(_address(window_id))} }})',
                ["unfloat"],
            )

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        _either(
            f"hl.dsp.window.fullscreen({{ window = {_quote(_address(window_id))} }})",
            ["fullscreen", "1" if fullscreen else "0"],
        )

    def switch_workspace(self, workspace: str) -> None:
        _either(
            f"hl.dsp.focus({{ workspace = {_quote(workspace)} }})",
            ["workspace", workspace],
        )

    def focus_monitor(self, monitor: str) -> None:
        _either(
            f"hl.dsp.focus({{ monitor = {_quote(monitor)} }})",
            ["focusmonitor", monitor],
        )

    def swap(self, first: str, second: str) -> None:
        _either(
            f"hl.dsp.window.swap({{ target = {_quote(_address(second))}, window = {_quote(_address(first))} }})",
            ["swapwindow", _address(first), _address(second)],
        )

    def scratchpad(self, window_id: str = "", name: str = "minimized") -> None:
        if window_id:
            _either(
                f"hl.dsp.window.move({{ workspace = {_quote('special:' + name)}, window = {_quote(_address(window_id))} }})",
                ["movewindow", _address(window_id), f"special:{name}"],
            )
        else:
            _either(
                f"hl.dsp.workspace.toggle_special({_quote(name)})",
                ["togglespecialworkspace", name],
            )
