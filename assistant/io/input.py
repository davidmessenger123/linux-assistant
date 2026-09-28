"""Typing and clicking on any Wayland or X11 desktop.

The original used ``wtype`` for typing, which only works on wlroots
compositors, so autonomous typing was impossible on GNOME and Plasma even
though everything else about those desktops was fine. It also could not click at
all without Hyprland, for no better reason.

Both are done here by writing to ``/dev/uinput`` directly. The kernel has no
idea which compositor is running, so one implementation covers GNOME Wayland,
Plasma Wayland, every wlroots compositor and X11 alike. External tools are kept
as fallbacks for the cases where a virtual device is not permitted, because
``/dev/uinput`` is root-owned on most distributions and needs either a udev rule
or a seat group.
"""

from __future__ import annotations

import fcntl
import math
import os
import struct
import time

from .. import proc

UINPUT = "/dev/uinput"

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0x00
ABS_X = 0x00
ABS_Y = 0x01
ABS_CNT = 64
BUS_VIRTUAL = 0x06

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

KEY_ESC = 1
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_ENTER = 28
KEY_LEFTSHIFT = 42
KEY_LEFTCTRL = 29
KEY_LEFTALT = 56
KEY_SPACE = 57
KEY_CAPSLOCK = 58
KEY_RIGHTCTRL = 97
KEY_RIGHTALT = 100
KEY_HOME = 102
KEY_UP = 103
KEY_PAGEUP = 104
KEY_LEFT = 105
KEY_RIGHT = 106
KEY_END = 107
KEY_DOWN = 108
KEY_PAGEDOWN = 109
KEY_INSERT = 110
KEY_DELETE = 111
KEY_LEFTMETA = 125
KEY_RIGHTMETA = 126

_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}

NAMED_KEYS = {
    "return": KEY_ENTER, "enter": KEY_ENTER, "tab": KEY_TAB, "space": KEY_SPACE,
    "backspace": KEY_BACKSPACE, "esc": KEY_ESC, "escape": KEY_ESC,
    "up": KEY_UP, "down": KEY_DOWN, "left": KEY_LEFT, "right": KEY_RIGHT,
    "home": KEY_HOME, "end": KEY_END, "pageup": KEY_PAGEUP, "pagedown": KEY_PAGEDOWN,
    "insert": KEY_INSERT, "delete": KEY_DELETE,
    "super": KEY_LEFTMETA, "meta": KEY_LEFTMETA, "cmd": KEY_LEFTMETA,
    "ctrl": KEY_LEFTCTRL, "control": KEY_LEFTCTRL,
    "alt": KEY_LEFTALT, "shift": KEY_LEFTSHIFT,
    "capslock": KEY_CAPSLOCK,
}

_PUNCTUATION = {
    " ": KEY_SPACE, "-": 12, "=": 13, "[": 26, "]": 27, "\\": 43, ";": 39, "'": 40,
    ",": 51, ".": 52, "/": 53, "`": 41,
}
# The number row runs 1-9 then 0, so '0' is not where arithmetic would put it.
for _offset, _digit in enumerate("123456789"):
    _PUNCTUATION[_digit] = 2 + _offset
_PUNCTUATION["0"] = 11
for _index, _letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _PUNCTUATION[_letter] = 30 + _index

BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}


class InputError(RuntimeError):
    """Input could not be sent."""


class UinputMissing(InputError):
    """The virtual device is not available, usually a permissions problem."""


def uinput_available() -> bool:
    return os.path.exists(UINPUT) and os.access(UINPUT, os.W_OK)


def available() -> list[str]:
    """Every input method this machine could use, best first."""
    found: list[str] = []
    if uinput_available():
        found.append("uinput")
    if proc.have("wtype"):
        found.append("wtype")
    if proc.have("ydotool"):
        found.append("ydotool")
    if proc.have("xdotool") and os.environ.get("DISPLAY"):
        found.append("xdotool")
    return found


def uinput_hint() -> str:
    if not os.path.exists(UINPUT):
        return (
            "/dev/uinput is missing. Load the uinput module with "
            "'sudo modprobe uinput' and add it to /etc/modules-load.d/uinput.conf."
        )
    if not os.access(UINPUT, os.W_OK):
        return (
            "/dev/uinput is not writable by this user. Either run the assistant as "
            "root, add your user to the 'input' group, or install a udev rule giving "
            "your user write access to /dev/uinput."
        )
    return ""


def _event(event_type: int, code: int, value: int) -> bytes:
    now = time.time_ns()
    return struct.pack(
        "llHHi",
        now // 1_000_000_000,
        (now % 1_000_000_000) // 1_000,
        event_type,
        code,
        int(value),
    )


class _Device:
    """A uinput device that is created on entry and always torn down."""

    def __init__(self, name: bytes, keybits: list[int], absmax: dict[int, int] | None = None):
        self.name = name
        self.keybits = keybits
        self.absmax = absmax or {}
        self.fd = -1
        self._created = False

    def __enter__(self) -> "_Device":
        try:
            self.fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise UinputMissing(uinput_hint() or f"{UINPUT} could not be opened: {error}") from error

        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in self.keybits:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            if self.absmax:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
                for axis in self.absmax:
                    fcntl.ioctl(self.fd, UI_SET_ABSBIT, axis)
            self._create()
        except OSError as error:
            self.__exit__(None, None, None)
            raise UinputMissing(f"uinput refused the device: {error}") from error
        time.sleep(0.06)
        return self

    def _create(self) -> None:
        if self._created:
            return
        self._created = False
        absmax = [0] * ABS_CNT
        absmin = [0] * ABS_CNT
        absfuzz = [0] * ABS_CNT
        absflat = [0] * ABS_CNT
        for axis, value in self.absmax.items():
            absmax[axis] = value
        os.write(
            self.fd,
            struct.pack(
                "80sHHHHI64i64i64i64i",
                self.name,
                BUS_VIRTUAL,
                0x1209,
                0x3478,
                1,
                0,
                *absmax,
                *absmin,
                *absfuzz,
                *absflat,
            ),
        )
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self._created = True

    def __exit__(self, *_exc) -> None:
        if self.fd < 0:
            return
        try:
            if self._created:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = -1


def _keycode(character: str) -> tuple[int, bool] | None:
    """Map one character to a Linux key code and whether shift is needed."""
    if character in _PUNCTUATION:
        return _PUNCTUATION[character], False
    if character in _SHIFTED:
        return _PUNCTUATION[_SHIFTED[character]], True
    if character.isalpha():
        return _PUNCTUATION[character.lower()], character.isupper()
    if character == "\n":
        return KEY_ENTER, False
    if character == "\t":
        return KEY_TAB, False
    return None


def keycode_for(name: str) -> int:
    key = NAMED_KEYS.get(str(name).strip().lower())
    if key is None:
        raise InputError(f"'{name}' is not a key the assistant is allowed to press.")
    return key


def type_text(text: str, prefer: str = "") -> dict:
    """Type text into whatever has focus."""
    order = [prefer] if prefer else available()
    if not order:
        raise InputError(
            "No way to send keystrokes was found. " + (uinput_hint() or "Install wtype, ydotool or xdotool.")
        )
    problems: list[str] = []
    for method in order:
        try:
            if method == "uinput":
                _type_uinput(text)
            elif method == "wtype":
                proc.run(["wtype", "--", text], timeout=30)
            elif method == "ydotool":
                for character in text:
                    mapping = _keycode(character)
                    if mapping is None:
                        continue
                    code, shift = mapping
                    sequence = [f"{KEY_LEFTSHIFT}:1"] if shift else []
                    sequence += [f"{code}:1", f"{code}:0"]
                    if shift:
                        sequence.append(f"{KEY_LEFTSHIFT}:0")
                    proc.run(["ydotool", "key", *sequence], timeout=15)
            elif method == "xdotool":
                proc.run(["xdotool", "type", "--clearmodifiers", "--", text], timeout=30)
            else:
                raise InputError(f"{method} is not a known input method.")
            return {"ok": True, "method": method, "characters": len(text)}
        except Exception as error:  # noqa: BLE001 - try the next method
            problems.append(f"{method}: {error}")
    raise InputError("Every way to type failed. " + " | ".join(problems))


def _type_uinput(text: str) -> None:
    needed: set[int] = set()
    plan: list[tuple[int, bool]] = []
    for character in text:
        mapping = _keycode(character)
        if mapping is None:
            continue
        plan.append(mapping)
        needed.add(mapping[0])
        if mapping[1]:
            needed.add(KEY_LEFTSHIFT)
    if not plan:
        return
    needed.add(KEY_LEFTSHIFT)

    with _Device(b"linux-assistant keyboard", sorted(needed)) as device:
        write = lambda code, value: os.write(device.fd, _event(EV_KEY, code, value))
        for code, shift in plan:
            if shift:
                write(KEY_LEFTSHIFT, 1)
            write(code, 1)
            write(code, 0)
            if shift:
                write(KEY_LEFTSHIFT, 0)
            write(EV_SYN, SYN_REPORT, 0)
            time.sleep(0.004)


def press_key(name: str, prefer: str = "") -> dict:
    """Press one named key, refusing anything not on the allowed list."""
    code = keycode_for(name)
    order = [prefer] if prefer else available()
    if not order:
        raise InputError("No way to send keystrokes was found. " + (uinput_hint() or ""))
    problems: list[str] = []
    for method in order:
        try:
            if method == "uinput":
                with _Device(b"linux-assistant keyboard", [code, KEY_LEFTSHIFT]) as device:
                    os.write(device.fd, _event(EV_KEY, code, 1))
                    os.write(device.fd, _event(EV_SYN, SYN_REPORT, 0))
                    time.sleep(0.03)
                    os.write(device.fd, _event(EV_KEY, code, 0))
                    os.write(device.fd, _event(EV_SYN, SYN_REPORT, 0))
            elif method == "wtype":
                keysym = {"return": "Return", "space": "space", "tab": "Tab", "backspace": "BackSpace",
                          "esc": "Escape", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
                          "enter": "Return", "escape": "Escape", "delete": "Delete", "home": "Home",
                          "end": "End", "pageup": "Prior", "pagedown": "Next", "insert": "Insert",
                          "super": "super_L", "meta": "super_L", "cmd": "super_L", "ctrl": "ctrl",
                          "control": "ctrl", "alt": "alt_L", "shift": "shift_L",
                          "capslock": "Caps_Lock"}.get(str(name).lower())
                if keysym is None:
                    raise InputError(f"wtype cannot press '{name}'.")
                proc.run(["wtype", "-k", keysym], timeout=15)
            elif method == "ydotool":
                proc.run(["ydotool", "key", f"{code}:1", f"{code}:0"], timeout=15)
            elif method == "xdotool":
                keyname = {"return": "Return", "space": "space", "tab": "Tab", "backspace": "BackSpace",
                           "esc": "Escape", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
                           "enter": "Return", "escape": "Escape", "delete": "Delete", "home": "Home",
                           "end": "End", "pageup": "Prior", "pagedown": "Next", "insert": "Insert",
                           "super": "super", "meta": "super", "cmd": "super", "ctrl": "ctrl",
                           "control": "ctrl", "alt": "alt", "shift": "shift",
                           "capslock": "Caps_Lock"}.get(str(name).lower())
                if keyname is None:
                    raise InputError(f"xdotool cannot press '{name}'.")
                proc.run(["xdotool", "key", keyname], timeout=15)
            else:
                raise InputError(f"{method} is not a known input method.")
            return {"ok": True, "method": method, "key": name}
        except Exception as error:  # noqa: BLE001 - try the next method
            problems.append(f"{method}: {error}")
    raise InputError("Every way to press a key failed. " + " | ".join(problems))


def monitor_bounds() -> tuple[float, float, float, float]:
    """The focused monitor's rectangle, from whichever backend is active."""
    from ..backends import detect

    backend, notes = detect()
    try:
        snapshot = backend.snapshot()
    except Exception as error:  # noqa: BLE001
        raise InputError(f"The monitor layout could not be read: {error}") from error
    monitor = snapshot.focused_monitor()
    if monitor is None:
        live = snapshot.live_monitors()
        if not live:
            raise InputError("No monitor is available. " + (" ".join(notes) or ""))
        monitor = live[0]
    return float(monitor.x), float(monitor.y), float(monitor.width), float(monitor.height)


def click(x: float, y: float, button: str = "left", prefer: str = "") -> dict:
    """Click at a fraction of the focused monitor, with 0,0 top left.

    Fractions rather than pixels, because a screenshot is what the model looked
    at and it reasons in proportions of the image.
    """
    if not all(math.isfinite(value) for value in (x, y)):
        raise InputError("Click coordinates must be finite numbers.")
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise InputError("Click coordinates must be between 0 and 1.")
    code = BUTTONS.get(button)
    if code is None:
        raise InputError("Button must be left, right or middle.")

    left, top, width, height = monitor_bounds()
    if width <= 1 or height <= 1:
        raise InputError("The focused monitor has no usable size.")
    absolute_x = round(left + x * (width - 1))
    absolute_y = round(top + y * (height - 1))

    order = [prefer] if prefer else available()
    if not order:
        raise InputError("No way to send a click was found. " + (uinput_hint() or ""))
    problems: list[str] = []
    for method in order:
        try:
            if method == "uinput":
                _click_uinput(absolute_x - left, absolute_y - top, code, int(width) - 1, int(height) - 1)
            elif method == "xdotool":
                proc.run(["xdotool", "mousemove", "--sync", str(absolute_x), str(absolute_y),
                          "click", str(1 if code == BTN_LEFT else (3 if code == BTN_RIGHT else 2))],
                         timeout=15)
            else:
                raise InputError(f"{method} cannot send a click.")
            return {"ok": True, "method": method, "x": absolute_x, "y": absolute_y, "button": button}
        except Exception as error:  # noqa: BLE001 - try the next method
            problems.append(f"{method}: {error}")
    raise InputError("Every way to click failed. " + " | ".join(problems))


def _click_uinput(x: int, y: int, button: int, max_x: int, max_y: int) -> None:
    with _Device(b"linux-assistant pointer", sorted(BUTTONS.values()), {ABS_X: max_x, ABS_Y: max_y}) as device:
        os.write(device.fd, _event(EV_ABS, ABS_X, x))
        os.write(device.fd, _event(EV_ABS, ABS_Y, y))
        os.write(device.fd, _event(EV_SYN, SYN_REPORT, 0))
        time.sleep(0.03)
        os.write(device.fd, _event(EV_KEY, button, 1))
        os.write(device.fd, _event(EV_SYN, SYN_REPORT, 0))
        time.sleep(0.05)
        os.write(device.fd, _event(EV_KEY, button, 0))
        os.write(device.fd, _event(EV_SYN, SYN_REPORT, 0))
        time.sleep(0.05)
