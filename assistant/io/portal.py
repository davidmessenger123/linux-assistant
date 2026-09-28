"""One screenshot through xdg-desktop-portal and PipeWire.

This is the portable path, and it is the reason the assistant can capture a
screen on GNOME, Plasma and wlroots without knowing which one it is on. The
portal hands back a PipeWire node id rather than pixels, so the frames are read
with GStreamer's ``pipewiresrc`` and written out by :mod:`assistant.io.png`.

Both PyGObject imports are deliberate and load-bearing: Gio is what makes the
portal's response signal waitable, and Gst is what pulls frames out of
PipeWire. Neither is needed for the grim or X11 fallbacks, so they are imported
inside the function that uses them.
"""

from __future__ import annotations

import shutil
import urllib.parse

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SOURCES_IFACE = "org.freedesktop.portal.Sources"

DEFAULT_TIMEOUT = 25


class PortalError(RuntimeError):
    """The portal refused, or never answered."""


def _node_id(streams) -> int:
    """Pull the PipeWire node id out of the portal's stream list."""
    for entry in streams or []:
        _node, properties = entry
        for key, value in properties.items():
            if key == "node-id":
                return int(value)
    raise PortalError("The portal returned no usable stream.")


def _pick_stream(streams, monitor: str):
    """Choose a stream by connector name, cursor position or focus.

    The portal's stream properties carry the connector name, so a named monitor
    is honoured exactly. ``cursor`` and ``focused`` are resolved by picking the
    stream whose position matches, which is the best a portal caller can do.
    """
    if not streams:
        raise PortalError("The portal returned no streams.")
    if monitor in ("", "auto", None):
        return streams[0]

    for entry in streams:
        _node, properties = entry
        name = str(properties.get("name") or "")
        if name.lower() == monitor.lower():
            return entry

    if monitor == "cursor":
        from ..backends.x11 import X11Backend

        position = _cursor_position()
        if position is None:
            raise PortalError("The cursor position is not available.")
        x, y = position
        return _nearest(streams, x, y)

    if monitor == "focused":
        from ..backends import detect

        backend, _notes = detect()
        try:
            current = backend.snapshot().focused_monitor()
        except Exception:  # noqa: BLE001 - a read failure just means no focus
            current = None
        if current is None:
            raise PortalError("No monitor reported as focused.")
        return _nearest(streams, current.x + current.width // 2, current.y + current.height // 2)

    raise PortalError(f"The portal did not offer a stream for '{monitor}'.")


def _nearest(streams, x: int, y: int):
    def centre(entry):
        _node, properties = entry
        position = properties.get("position") or (0, 0)
        size = properties.get("size") or (0, 0)
        try:
            return (int(position[0]) + int(size[0]) // 2, int(position[1]) + int(size[1]) // 2)
        except (TypeError, ValueError, IndexError):
            return (0, 0)

    return min(streams, key=lambda entry: (centre(entry)[0] - x) ** 2 + (centre(entry)[1] - y) ** 2)


def _cursor_position() -> tuple[int, int] | None:
    from .. import proc

    for command, args, pick in (
        ("hyprctl", ["hyprctl", "cursorpos"], None),
    ):
        if not proc.have(command):
            continue
        try:
            raw = proc.run(args, timeout=5, check=False)
        except (proc.ToolMissing, OSError):
            continue
        parts = raw.replace(",", " ").split()
        numbers = [p for p in parts if p.isdigit()]
        if len(numbers) >= 2:
            return int(numbers[0]), int(numbers[1])
    return None


def grab_screenshot(path: str) -> dict:
    """Ask the portal for a whole-screen PNG, the simple way.

    ``Screenshot`` is the method almost every portal backend implements, and it
    returns a finished image, so it needs neither PipeWire nor GStreamer. It
    cannot be told which monitor to capture, so the caller has to crop
    afterwards if that matters.
    """
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    options = {
        "interactive": GLib.Variant("b", False),
        "modal": GLib.Variant("b", False),
    }
    reply = connection.call_sync(
        PORTAL_BUS,
        PORTAL_PATH,
        "org.freedesktop.portal.Screenshot",
        "Screenshot",
        GLib.Variant("(sa{sv})", ("", options)),
        GLib.VariantType("(o)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )
    request_path = reply.unpack()[0]

    result: dict = {}
    loop = GLib.MainLoop()

    def on_response(_connection, _sender, _path, _iface, _signal, parameters):
        code, answer = parameters.unpack()
        result["code"] = int(code)
        result["uri"] = answer.get("uri")
        loop.quit()

    subscription = connection.signal_subscribe(
        PORTAL_BUS,
        REQUEST_IFACE,
        "Response",
        request_path,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
    )
    try:
        loop.run()
    finally:
        # signal_unsubscribe wants the id signal_subscribe returned, not the
        # callback. Passing the function raises and hides the real result.
        connection.signal_unsubscribe(subscription)

    if result.get("code") != 0 or not result.get("uri"):
        raise PortalError(f"The portal declined the screenshot (response {result.get('code')}).")

    source = urllib.parse.unquote(str(result["uri"]).removeprefix("file://"))
    if source != path:
        shutil.copyfile(source, path)
    return {"width": 0, "height": 0, "note": "captured through the portal"}


def request_stream(monitor: str = "auto", interactive: bool = True) -> int:
    """Ask the portal for a screencast and return a PipeWire node id.

    The portal shows a permission prompt the first time. ``interactive`` false
    means a request that cannot be granted silently is refused rather than
    waiting for an answer that will never come.
    """
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    properties = {
        "cursor_mode": GLib.Variant("u", 2 if interactive else 0),
        "multiple": GLib.Variant("b", False),
    }
    reply = connection.call_sync(
        PORTAL_BUS,
        PORTAL_PATH,
        SCREENCAST_IFACE,
        "ScreenCast",
        GLib.Variant("(sa{sv})", ("", properties)),
        GLib.VariantType("(o)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )
    request_path = reply.unpack()[0]

    result: dict = {}
    loop = GLib.MainLoop()

    def on_response(_connection, _sender, _path, _iface, _signal, parameters):
        code, answer = parameters.unpack()
        result["code"] = int(code)
        result["streams"] = answer.get("streams")
        result["restored_token"] = answer.get("restore_token")
        loop.quit()

    subscription = connection.signal_subscribe(
        PORTAL_BUS,
        REQUEST_IFACE,
        "Response",
        request_path,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
    )
    try:
        loop.run()
    finally:
        # signal_unsubscribe wants the id signal_subscribe returned, not the
        # callback. Passing the function raises and hides the real result.
        connection.signal_unsubscribe(subscription)

    if result.get("code") not in (0, 1):
        raise PortalError(f"The portal declined the screencast (response {result.get('code')}).")
    streams = _pick_stream(result.get("streams"), monitor)
    return _node_id([streams])


def pull_frame(node_id: int, timeout: int = DEFAULT_TIMEOUT):
    """Read one RGBA frame out of a PipeWire node. Returns width, height, bytes."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)

    pipeline = Gst.parse_launch(
        f"pipewiresrc path={int(node_id)} ! videoconvert ! "
        "video/x-raw,format=RGBA ! appsink name=sink emit-signals=false max-buffers=1 drop=true"
    )
    sink = pipeline.get_by_name("sink")
    if sink is None:
        raise PortalError("GStreamer could not build the capture pipeline.")

    loop = __import__("gi.repository.GLib", fromlist=["GLib"]).MainLoop()
    frame: dict = {}
    error: dict = {}

    def on_sample(_sink):
        sample = _sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, info = buffer.map_range(0, -1, 3600)  # Gst.MapFlags.READ
        if not ok:
            error["why"] = "the frame could not be read"
        else:
            caps = buffer.get_caps().get_structure(0)
            frame["width"] = caps.get_value("width")
            frame["height"] = caps.get_value("height")
            frame["data"] = bytes(info.data)
            buffer.unmap(info)
        loop.quit()
        return Gst.FlowReturn.OK

    def on_error(_bus, message):
        error["why"] = message.parse_error().message if hasattr(message, "parse_error") else str(message)
        loop.quit()

    sink.connect("new-sample", on_sample)
    pipeline.bus.connect("message", on_error)
    pipeline.set_state(Gst.State.PLAYING)

    import threading

    timer = threading.Timer(timeout, loop.quit)
    timer.start()
    try:
        loop.run()
    finally:
        timer.cancel()
        pipeline.set_state(Gst.State.NULL)

    if error:
        raise PortalError(f"PipeWire did not deliver a frame: {error.get('why')}")
    if "data" not in frame:
        raise PortalError(f"PipeWire delivered no frame within {timeout}s.")
    return frame["width"], frame["height"], frame["data"]


def grab_once(monitor: str = "auto") -> tuple[int, int, bytes]:
    """The whole portable capture in one call."""
    node_id = request_stream(monitor)
    return pull_frame(node_id)
