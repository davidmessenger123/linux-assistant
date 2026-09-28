"""Check whether this machine can run the assistant, and say what is missing.

The first thing anyone porting this to a new desktop will do is run it and get
nothing, so the diagnosis has to be specific. "It doesn't work" is not useful
when the answer might be that no window-control API exists for the session they
are logged into, which is a different problem from a missing package and needs
a different fix.

Run it with ``python3 -m assistant.doctor``.
"""

from __future__ import annotations

import os
import shutil
import sys

OK = "ok"
WARN = "warn"
FAIL = "fail"

REQUIRED = (
    ("python3", "the interpreter this is running under"),
    ("opencode", "talks to the model; install it and sign in"),
)

OPTIONAL = (
    ("gdbus", "needed for portal screenshots and dismissing notifications"),
    ("gio", "starting applications by desktop file"),
    ("git", "self-update, if it is ever added"),
)


def _python_version() -> tuple[bool, str]:
    if sys.version_info < (3, 9):
        return False, f"Python {sys.version_info.major}.{sys.version_info.minor} is too old; 3.9 or newer is needed"
    return True, f"Python {sys.version_info.major}.{sys.version_info.minor}"


def _pygobject() -> tuple[bool, str]:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
    except (ImportError, ValueError) as error:
        return False, (
            f"PyGObject with GTK 4 and libadwaita is needed: {error}. "
            "Debian/Ubuntu: python3-gi gir1.2-gtk-4.0 gir1.2-adw-1. "
            "Fedora: python3-gobject gtk4 libadwaita. Arch: python-gobject gtk4 libadwaita."
        )
    return True, f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, libadwaita {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}"


def _uinput() -> tuple[str, str]:
    device = "/dev/uinput"
    if not os.path.exists(device):
        return WARN, (
            f"{device} is missing, so clicking will not work. "
            "Load the module with 'sudo modprobe uinput' and add it to /etc/modules-load.d/uinput.conf."
        )
    if not os.access(device, os.W_OK):
        return WARN, (
            f"{device} is not writable by this user, so clicking will not work. "
            "Add your user to the 'input' group, or install a udev rule granting write access."
        )
    return OK, f"{device} is writable, so clicking and typing work"


def _portal() -> tuple[str, str]:
    from .io import capture

    if capture.portal_available():
        return OK, "xdg-desktop-portal is available, so screenshots do not need a compositor tool"
    return WARN, (
        "xdg-desktop-portal is not answering. Screenshots will fall back to a "
        "compositor tool: " + (", ".join(capture.methods()) or "none found, so screenshots will not work")
    )


def _backend() -> tuple[str, str]:
    from .backends import WINDOW_CAPABILITIES, detect, describe

    backend, notes = detect()
    summary = describe(backend)
    if backend.name == "virtual":
        session = os.environ.get("XDG_SESSION_TYPE", "unknown")
        if session == "wayland":
            return FAIL, (
                "No window manager backend works in this session, and on a Wayland session "
                "that usually means the desktop has no window-control API -- GNOME and Plasma "
                "do not provide one. Chat, screenshots, typing and clicking still work. "
                "For window control, log in on an X11 session."
            )
        return FAIL, "No window manager backend works in this session. " + "; ".join(notes)

    missing = [c for c in WINDOW_CAPABILITIES if not backend.has(c)]
    text = f"Using the {backend.name} backend, which can: " + ", ".join(summary["capabilities"])
    if missing:
        text += ". Not available here: " + ", ".join(missing)
    return OK, text


def _backends_available() -> list[tuple[str, str, str]]:
    from .backends import BACKEND_ORDER, build

    rows = []
    for name in BACKEND_ORDER:
        try:
            backend = build(name)
            usable = backend.available()
        except Exception as error:  # noqa: BLE001 - a probe must not be fatal
            rows.append((name, FAIL, f"could not be checked: {error}"))
            continue
        rows.append((name, OK, "usable here") if usable else (name, "", "not installed or not this session"))
    return rows


def report(verbose: bool = False) -> tuple[list[tuple[str, str, str]], bool]:
    """Return the checks and whether anything is a hard failure."""
    checks: list[tuple[str, str, str]] = []

    ok, text = _python_version()
    checks.append(("python", OK if ok else FAIL, text))

    for name, why in REQUIRED:
        found = shutil.which(name)
        checks.append((name, OK if found else FAIL, found or f"not installed -- {why}"))

    ok, text = _pygobject()
    checks.append(("pygobject", OK if ok else FAIL, text))

    status, text = _backend()
    checks.append(("desktop backend", status, text))

    status, text = _portal()
    checks.append(("screenshots", status, text))

    status, text = _uinput()
    checks.append(("input", status, text))

    if verbose:
        for name, status, text in OPTIONAL:
            found = shutil.which(name)
            checks.append((name, OK if found else "", found or f"not installed -- {why}"))

    blocking = any(status == FAIL for _name, status, _text in checks)
    return checks, blocking


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check whether the assistant can run here.")
    parser.add_argument("--verbose", "-v", action="store_true", help="include optional tools")
    args = parser.parse_args(argv)

    checks, blocking = report(args.verbose)
    width = max(len(name) for name, _s, _t in checks)
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}

    print("linux-assistant doctor")
    print("=" * (width + 8))
    for name, status, text in checks:
        print(f"  {marks.get(status, '  -  ')}  {name:<{width}}  {text}")

    print()
    print("  backends:")
    for name, status, text in _backends_available():
        print(f"    {name:<10} {text}")

    print()
    if blocking:
        print("  Some things are missing. The lines marked FAIL will stop it working;")
        print("  WARN lines only affect the features named.")
        return 1
    print("  Nothing is missing that the assistant needs to start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
