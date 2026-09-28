"""KDE Plasma's KWin, through its D-Bus scripting interface.

KWin exposes no command-line tool, so state and actions both go through
``org.kde.KWin.Scripting``. A small QJS helper is written to disk once, loaded
with ``loadScript``, and told what to do through a JSON parameter file; it
answers in a JSON result file. The helper is re-read on every call, so editing
it while Plasma is running is enough to change behaviour.

Geometry requests that KWin has no shortcut for (an exact position, a tiling
column) are done by the helper, because Plasma overrides plain position
requests for tiled windows. Everything the helper is asked to do is a fixed
operation chosen here; no text from a model ever reaches the script.

This backend is written against the KWin 6 scripting API. It has not been run
against a live Plasma session, so treat it as the least proven of the set.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

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

SERVICE = "org.kde.KWin"
OBJECT = "/KWin"
SCRIPTING = "org.kde.KWin.Scripting"

HELPER = r"""
function operation(params) {
    const out = { ok: true, windows: [], monitors: [], workspaces: [], active: "" };
    const ws = workspace();
    const active = ws.activeWindow || null;

    if (params.op === "snapshot") {
        for (const w of ws.windowList()) {
            if (w.internalId && w.keepAbove === undefined) continue;
            const c = w.frameGeometry();
            const c2 = w.output ? w.output.name : "";
            out.windows.push({
                id: String(w.internalId ? w.internalId.toString() : ""),
                app_id: w.resourceClass || w.resourceName || "",
                title: w.caption || "",
                workspace: w.desktop ? String(w.desktop) : "",
                monitor: String(c2),
                x: Math.round(c.x),
                y: Math.round(c.y),
                width: Math.round(c.width),
                height: Math.round(c.height),
                floating: w.floating === true,
                focused: w === active,
                minimized: w.minimized === true
            });
        }
        for (const s of ws.screens) {
            const g = s.geometry;
            out.monitors.push({
                id: s.name, name: s.name, x: Math.round(g.x), y: Math.round(g.y),
                width: Math.round(g.width), height: Math.round(g.height),
                scale: s.scale || 1, focused: s === ws.activeScreen
            });
        }
        for (const d of ws.desktops) {
            out.workspaces.push({
                name: d.name, monitor: "", active: d === ws.currentDesktop,
                count: ws.desktops ? (d.desktopCount || 0) : 0, special: false
            });
        }
        return out;
    }

    const target = ws.windowList().find(w => w.internalId && String(w.internalId.toString()) === String(params.id));
    if (!target && (params.op === "focus" || params.op === "move" || params.op === "resize" ||
                    params.op === "float" || params.op === "fullscreen" || params.op === "place")) {
        return { ok: false, error: "That window is no longer open." };
    }

    if (params.op === "focus") { ws.activeWindow = target; }
    else if (params.op === "move" && params.workspace) { target.desktop = ws.desktops.find(d => d.name === params.workspace) || ws.currentDesktop; }
    else if (params.op === "move" && params.monitor) {
        const screen = ws.screens.find(s => s.name === params.monitor);
        if (screen) { target.output = screen; target.frameGeometry = target.frameGeometry.translated(target.frameGeometry.x - screen.geometry.x, target.frameGeometry.y - screen.geometry.y); }
    }
    else if (params.op === "resize") { target.frameGeometry = { x: target.frameGeometry.x, y: target.frameGeometry.y, width: params.width, height: params.height }; }
    else if (params.op === "place") {
        target.floating = true;
        target.frameGeometry = { x: params.x, y: params.y, width: target.frameGeometry.width, height: target.frameGeometry.height };
    }
    else if (params.op === "float") { target.floating = params.on; }
    else if (params.op === "fullscreen") { target.fullScreen = params.on; }
    else if (params.op === "swap") {
        const other = ws.windowList().find(w => w.internalId && String(w.internalId.toString()) === String(params.other));
        if (other) { const a = target.frameGeometry; target.frameGeometry = other.frameGeometry; other.frameGeometry = a; }
    }
    else if (params.op === "workspace") { ws.currentDesktop = ws.currentActivity ? ws.currentActivity.desktops().find(d => d.name === params.workspace) : ws.desktops.find(d => d.name === params.workspace) || ws.currentDesktop; }
    else if (params.op === "monitor") {
        const screen = ws.screens.find(s => s.name === params.monitor);
        if (screen) {
            for (const d of ws.desktops) {
                if (screen.desktops && screen.desktops.length && screen.desktops.indexOf(d) >= 0) { ws.currentDesktop = d; break; }
            }
            const on = ws.windowList().find(w => w.output && w.output.name === params.monitor);
            if (on) ws.activeWindow = on;
        }
    }
    else { return { ok: false, error: "Unknown operation" }; }

    return { ok: true };
}
"""


class KWinBackend(DesktopBackend):
    name = "kwin"
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

    def __init__(self, workdir: str | None = None) -> None:
        self.workdir = workdir or tempfile.mkdtemp(prefix="linux-assistant-kwin-")
        os.makedirs(self.workdir, mode=0o700, exist_ok=True)

    def available(self) -> bool:
        """Whether KWin is on the session bus and offers its scripting interface.

        The check has to be for the Scripting interface specifically. On a
        machine not running Plasma there is no org.kde.KWin at all, and a loose
        check would either match nothing useful or, worse, treat the empty reply
        from a failed call as a success and claim every other desktop.
        """
        if not proc.have("gdbus"):
            return False
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.environ.get("XDG_RUNTIME_DIR"):
            return False
        try:
            listing = proc.run(
                [
                    "gdbus", "introspect", "--session", "--dest", SERVICE,
                    "--object-path", OBJECT,
                ],
                timeout=5,
                check=False,
            )
        except (proc.ToolMissing, OSError):
            return False
        if not listing.strip():
            return False
        return f"interface {SCRIPTING}" in listing and "loadScript" in listing

    def _call(self, method: str, argument: str) -> str:
        output = proc.run(
            [
                "gdbus", "call", "--session", "--dest", SERVICE,
                "--object-path", OBJECT,
                "--method", f"{SCRIPTING}.{method}", argument,
            ],
            check=True,
        )
        return output.strip().strip("()").strip("'")

    def _invoke(self, payload: dict) -> dict:
        request = os.path.join(self.workdir, "request.json")
        result = os.path.join(self.workdir, "result.json")
        script = os.path.join(self.workdir, "helper.js")
        if os.path.exists(result):
            os.unlink(result)

        with open(request, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(self._source())

        self._call("loadScript", script)
        self._call("start", "")
        for _ in range(50):
            if os.path.exists(result):
                with open(result, encoding="utf-8") as handle:
                    return json.load(handle)
            time.sleep(0.1)
        raise OSError("KWin did not answer the scripting request in time.")

    def _source(self) -> str:
        return f"""{HELPER}
function main() {{
    const io = new File({json.dumps(os.path.join(self.workdir, "result.json"))});
    const request = new File({json.dumps(os.path.join(self.workdir, "request.json"))});
    request.open(1);
    const text = request.readAll();
    request.close();
    const params = JSON.parse(text);
    const answer = operation(params);
    io.open(2);
    io.write(JSON.stringify(answer));
    io.close();
}}
"""

    def snapshot(self) -> Snapshot:
        answer = self._invoke({"op": "snapshot"})
        if not answer.get("ok"):
            raise OSError(str(answer.get("error") or "KWin returned nothing usable."))
        return Snapshot(
            windows=[Window(**_only(item, Window)) for item in answer.get("windows") or []],
            monitors=[Monitor(**_only(item, Monitor)) for item in answer.get("monitors") or []],
            workspaces=[Workspace(**_only(item, Workspace)) for item in answer.get("workspaces") or []],
        )

    def _action(self, payload: dict) -> None:
        answer = self._invoke(payload)
        if not answer.get("ok"):
            raise OSError(str(answer.get("error") or "KWin refused the request."))

    def focus(self, window_id: str) -> None:
        self._action({"op": "focus", "id": window_id})

    def move(self, window_id: str, workspace: str = "", monitor: str = "") -> None:
        if not workspace and not monitor:
            raise Unsupported("Moving a KWin window needs a workspace or a monitor.")
        self._action({"op": "move", "id": window_id, "workspace": workspace, "monitor": monitor})

    def resize(self, window_id: str, width: int, height: int) -> None:
        self._action({"op": "resize", "id": window_id, "width": int(width), "height": int(height)})

    def move_rect(self, window_id: str, x: int, y: int) -> None:
        self._action({"op": "place", "id": window_id, "x": int(x), "y": int(y)})

    def set_floating(self, window_id: str, floating: bool) -> None:
        self._action({"op": "float", "id": window_id, "on": bool(floating)})

    def set_fullscreen(self, window_id: str, fullscreen: bool) -> None:
        self._action({"op": "fullscreen", "id": window_id, "on": bool(fullscreen)})

    def switch_workspace(self, workspace: str) -> None:
        self._action({"op": "workspace", "workspace": workspace})

    def focus_monitor(self, monitor: str) -> None:
        self._action({"op": "monitor", "monitor": monitor})

    def swap(self, first: str, second: str) -> None:
        self._action({"op": "swap", "id": first, "other": second})


def _only(item: dict, kind) -> dict:
    """Keep just the fields the dataclass declares, so a new KWin field is ignored."""
    names = set(getattr(kind, "__dataclass_fields__", {}))
    return {key: value for key, value in item.items() if key in names}
