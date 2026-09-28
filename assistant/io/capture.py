"""Taking a screenshot, whichever desktop is running.

The original shipped a hand-built grim binary and worked around Hyprland's
screencopy stalling by switching every other monitor off around each capture.
That is a compositor bug treated as an application concern, and it meant no
capture at all on anything that was not Hyprland on x86-64.

The order here is chosen by how well a method travels, not by how well it works
on the machine that wrote it:

1. **xdg-desktop-portal** with a PipeWire stream. One API for GNOME, Plasma and
   every wlroots compositor, and the only one that gets the framebuffer through
   unmodified rather than re-encoding it.
2. **A compositor's own tool**, when the portal is absent.
3. **X11 tools**, which is how GNOME and Plasma get capture at all under
   XWayland, since neither implements the portal on a Wayland session the
   assistant can reach.

Nothing here ever changes monitor state.
"""

from __future__ import annotations

import os
import tempfile
import time

from .. import proc

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SCREENCAST = "org.freedesktop.portal.ScreenCast"

GRIM = "grim"
SPECTACLE = "spectacle"
GNOME_SCREENSHOT = "gnome-screenshot"
SCROT = "scrot"
IMPORT = "import"
FFMPEG = "ffmpeg"
MAIM = "maim"

DEFAULT_TIMEOUT = 25


class CaptureError(RuntimeError):
    """No capture method worked, or the one that was tried said why."""


def portal_available() -> bool:
    """Whether the portal is on the session bus, without asking for anything."""
    if not proc.have("gdbus"):
        return False
    try:
        listing = proc.run(
            ["gdbus", "introspect", "--session", "--dest", PORTAL_BUS, "--object-path", PORTAL_PATH],
            timeout=5,
            check=False,
        )
    except (proc.ToolMissing, OSError):
        return False
    return PORTAL_SCREENCAST in listing


def _tool(name: str) -> str | None:
    return proc.which(name) if proc.have(name) else None


def _resolve_output(monitor: str) -> str:
    """Turn ``auto``, ``cursor`` or ``focused`` into a real connector name.

    Tools like grim take a connector on the command line and nothing else, so
    the aliases have to become a name before they reach one. Asking the active
    backend is the only place that knows which output is focused, which is why
    this is not a string substitution.
    """
    if monitor not in ("", "auto", "cursor", "focused"):
        return monitor
    from ..backends import detect

    backend, _notes = detect()
    try:
        snapshot = backend.snapshot()
    except Exception as error:  # noqa: BLE001 - fall back to the default output
        raise CaptureError(f"The active output could not be determined: {error}")

    if monitor == "focused":
        current = snapshot.focused_monitor()
        if current is None:
            live = snapshot.live_monitors()
            if not live:
                raise CaptureError("No monitor is available to capture.")
            return live[0].name
        return current.name

    if monitor == "cursor":
        under = _monitor_under_cursor(snapshot)
        if under is not None:
            return under.name
        live = snapshot.live_monitors()
        return live[0].name if live else ""

    live = snapshot.live_monitors()
    return live[0].name if live else ""


def _monitor_under_cursor(snapshot):
    from .portal import _cursor_position

    position = _cursor_position()
    if position is None:
        return None
    x, y = position
    for monitor in snapshot.live_monitors():
        if monitor.x <= x < monitor.x + monitor.width and monitor.y <= y < monitor.y + monitor.height:
            return monitor
    return None


def methods() -> list[str]:
    """Every capture method this machine could actually use, best first."""
    found: list[str] = []
    if portal_available():
        found.append("portal-shot")
    for name, label in (
        (SPECTACLE, "spectacle"),
        (GNOME_SCREENSHOT, "gnome-screenshot"),
        (GRIM, "grim"),
        (MAIM, "maim"),
        (SCROT, "scrot"),
        (IMPORT, "import"),
        (FFMPEG, "ffmpeg"),
    ):
        if proc.have(name) and label not in found:
            found.append(label)
    return found


def capture(path: str, monitor: str = "auto", prefer: str = "") -> dict:
    """Save a screenshot to ``path`` and describe how it was taken.

    ``monitor`` accepts a connector name, ``cursor``, ``focused`` or ``auto``.
    Methods that cannot honour a monitor choice say so instead of quietly
    returning the wrong screen.
    """
    order = methods()
    if prefer:
        if prefer not in order:
            raise CaptureError(
                f"{prefer} cannot take screenshots here. Available: {', '.join(order) or 'none'}."
            )
        order = [prefer] + [m for m in order if m != prefer]

    if not order:
        raise CaptureError(
            "No screenshot tool is available. Install xdg-desktop-portal for the "
            "portable path, or grim, spectacle, scrot, ImageMagick or ffmpeg."
        )

    problems: list[str] = []
    for method in order:
        try:
            detail = _take(method, path, monitor)
            return {"ok": True, "path": path, "method": method, **detail}
        except CaptureError as error:
            problems.append(f"{method}: {error}")
        except Exception as error:  # noqa: BLE001 - one method failing must not stop the rest
            problems.append(f"{method}: {error}")
    raise CaptureError("Every screenshot method failed. " + " | ".join(problems))


def _take(method: str, path: str, monitor: str) -> dict:
    if method == "portal-shot":
        return _via_portal_shot(path, monitor)
    if method == "portal":
        return _via_portal(path, monitor)
    if method == "grim":
        return _via_grim(path, monitor)
    if method == "spectacle":
        return _via_spectacle(path, monitor)
    if method == "gnome-screenshot":
        return _via_gnome(path, monitor)
    if method == "scrot":
        return _via_scrot(path, monitor)
    if method == "maim":
        return _via_maim(path, monitor)
    if method == "import":
        return _via_import(path, monitor)
    if method == "ffmpeg":
        return _via_ffmpeg(path, monitor)
    raise CaptureError(f"{method} is not a known method.")


def _finish(path: str, method: str, **detail) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise CaptureError("the command reported success but wrote no file")
    return {"method": method, "bytes": os.path.getsize(path), **detail}


def _via_portal_shot(path: str, monitor: str) -> dict:
    """The portal's own Screenshot method: no PipeWire, no GStreamer."""
    from .portal import grab_screenshot

    detail = grab_screenshot(path)
    return _finish(path, "portal-shot", **detail)


def _via_portal(path: str, monitor: str) -> dict:
    """Ask the portal for a stream, then pull one frame out of PipeWire."""
    from .portal import grab_once

    width, height, data = grab_once(monitor)
    from .png import write as write_png

    write_png(path, width, height, data, 4)
    return _finish(path, "portal", width=width, height=height, note="captured through the portal and PipeWire")


def _via_grim(path: str, monitor: str) -> dict:
    grim = _tool(GRIM) or GRIM
    output = _resolve_output(monitor)
    args = [grim, "-o", output, path] if output else [grim, path]
    proc.run(args, timeout=DEFAULT_TIMEOUT)
    return _finish(path, "grim", monitor=output or "all")


def _via_spectacle(path: str, monitor: str) -> dict:
    spectacle = _tool(SPECTACLE) or SPECTACLE
    args = [spectacle, "--nonotify", "--fullscreen", "--output", path]
    if monitor not in ("", "auto", "focused"):
        args = [spectacle, "--nonotify", "--fullscreen", "--output", path]
    proc.run(args, timeout=DEFAULT_TIMEOUT)
    return _finish(path, "spectacle", note="fullscreen; Plasma was not asked for a specific output")


def _via_gnome(path: str, monitor: str) -> dict:
    gnome = _tool(GNOME_SCREENSHOT) or GNOME_SCREENSHOT
    proc.run([gnome, "-f", path], timeout=DEFAULT_TIMEOUT)
    return _finish(path, "gnome-screenshot", note="whole screen")


def _via_scrot(path: str, monitor: str) -> dict:
    scrot = _tool(SCROT) or SCROT
    if monitor not in ("", "auto"):
        raise CaptureError("scrot cannot target a specific monitor")
    proc.run([scrot, "-o", path], timeout=DEFAULT_TIMEOUT)
    return _finish(path, "scrot")


def _via_maim(path: str, monitor: str) -> dict:
    maim = _tool(MAIM) or MAIM
    args = [maim]
    if monitor not in ("", "auto"):
        args += ["/dev/dribyindex/" + monitor] if monitor.isdigit() else []
    args.append(path)
    proc.run(args, timeout=DEFAULT_TIMEOUT)
    return _finish(path, "maim")


def _via_import(path: str, monitor: str) -> dict:
    image = _tool(IMPORT) or IMPORT
    if monitor not in ("", "auto"):
        geometry = _x11_monitor_geometry(monitor)
        if geometry is None:
            raise CaptureError(f"no X11 monitor called {monitor}")
        proc.run([image, "-window", "root", "-crop", geometry, "+repage", path], timeout=DEFAULT_TIMEOUT)
    else:
        proc.run([image, "-window", "root", path], timeout=DEFAULT_TIMEOUT)
    return _finish(path, "import")


def _via_ffmpeg(path: str, monitor: str) -> dict:
    ffmpeg = _tool(FFMPEG) or FFMPEG
    display = os.environ.get("DISPLAY")
    if not display:
        raise CaptureError("ffmpeg needs DISPLAY, so this is an X11 session")
    args = [ffmpeg, "-y", "-f", "x11grab", "-video_size", _x11_screen_size(), "-i", display, "-frames:v", "1"]
    args += [path if monitor in ("", "auto") else f"{path}"]
    proc.run(args, timeout=DEFAULT_TIMEOUT)
    return _finish(path, "ffmpeg", note="whole screen")


def _x11_screen_size() -> str:
    try:
        raw = proc.run(["xdpyinfo"], timeout=5, check=False)
    except (proc.ToolMissing, OSError):
        raw = ""
    for token in raw.replace("\n", " ").split():
        if "x" in token and any(char.isdigit() for char in token):
            parts = token.split("x")
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                return token
    return "1920x1080"


def _x11_monitor_geometry(monitor: str) -> str | None:
    if not proc.have("xrandr"):
        return None
    try:
        raw = proc.run(["xrandr", "--query"], timeout=5, check=False)
    except (proc.ToolMissing, OSError):
        return None
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == monitor and fields[1] in ("connected", "primary"):
            for token in fields[2:]:
                if "+" in token and "x" in token:
                    return token.split("+", 1)[1] and "+" + token.split("+", 1)[1] and token
    return None


def temporary_png(prefix: str = "assistant-screen-") -> str:
    handle, path = tempfile.mkstemp(prefix=prefix, suffix=".png")
    os.close(handle)
    return path


def available_after(seconds: float) -> bool:
    """Small helper kept for callers that want to check the portal has settled."""
    time.sleep(max(seconds, 0))
    return portal_available()
