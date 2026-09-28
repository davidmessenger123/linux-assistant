"""Finding the backend for the running session.

Detection is by capability probe rather than by guessing from the environment,
because a Wayland session under GNOME and a Wayland session under Hyprland look
identical in ``XDG_CURRENT_DESKTOP``. Every backend is asked whether it can
actually talk to something; the first one that says yes wins.

The order below is deliberate. Compositor-native backends come first because
they understand tiling and workspaces properly, and X11 comes last as the
fallback that keeps the assistant usable under XWayland on GNOME and KDE.
"""

from __future__ import annotations

import os
from typing import Callable

from .base import (
    BackendError,
    CAP_FOCUS,
    CAP_MONITOR,
    CAP_MOVE,
    CAP_RESIZE,
    CAP_TILE,
    DesktopBackend,
    Monitor,
    Snapshot,
    Unsupported,
    WINDOW_CAPABILITIES,
    Window,
    Workspace,
)
from .hyprland import HyprlandBackend
from .kwin import KWinBackend
from .niri import NiriBackend
from .sway import SwayBackend
from .virtual import VirtualBackend
from .x11 import X11Backend

BACKEND_ORDER: tuple[str, ...] = ("hyprland", "sway", "niri", "kwin", "x11")

_LOADERS: dict[str, Callable[[], DesktopBackend]] = {
    "hyprland": HyprlandBackend,
    "sway": SwayBackend,
    "niri": NiriBackend,
    "kwin": KWinBackend,
    "x11": X11Backend,
    "virtual": VirtualBackend,
}


def backend_names() -> tuple[str, ...]:
    return BACKEND_ORDER + ("virtual",)


def build(name: str) -> DesktopBackend:
    """Instantiate one backend by name."""
    loader = _LOADERS.get(name)
    if loader is None:
        raise BackendError(f"Unknown backend: {name}. Known: {', '.join(backend_names())}.")
    return loader()


def preferred() -> str:
    """The backend named in the environment, if it is one we know."""
    choice = os.environ.get("ASSISTANT_BACKEND", "").strip().lower()
    if choice:
        if choice not in _LOADERS:
            raise BackendError(
                f"ASSISTANT_BACKEND is set to '{choice}', which is not a known backend. "
                f"Known: {', '.join(backend_names())}."
            )
        return choice
    for name in BACKEND_ORDER:
        if name in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
            return name
    session = os.environ.get("XDG_SESSION_DESKTOP", "").lower()
    for name in BACKEND_ORDER:
        if name in session:
            return name
    return ""


def detect(force: str = "") -> tuple[DesktopBackend, list[str]]:
    """Return the best backend for this session plus why the others were skipped."""
    notes: list[str] = []
    wanted = force.strip().lower() or preferred()
    if wanted:
        try:
            backend = build(wanted)
        except BackendError:
            backend = None
        if backend is not None:
            if backend.available():
                return backend, [f"Using {backend.name} because it was requested."]
            notes.append(f"{wanted} was requested but is not available in this session.")
    for name in BACKEND_ORDER:
        if name == wanted:
            continue
        backend = build(name)
        try:
            usable = backend.available()
        except Exception as error:  # noqa: BLE001 - a probe must never be fatal
            notes.append(f"{name} could not be checked: {error}")
            continue
        if usable:
            return backend, notes + [f"Using {name}."]

    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session == "wayland":
        notes.append(
            "No supported Wayland compositor was found, so the assistant cannot see or "
            "control windows. GNOME and Plasma on Wayland expose no window-control API. "
            "Log in on an X11 session to get window control through the x11 backend."
        )
    return VirtualBackend(available=False), notes


def describe(backend: DesktopBackend) -> dict:
    """A summary for the settings panel and for `assistant-doctor`."""
    return {
        "backend": backend.name,
        "available": backend.available(),
        "capabilities": sorted(c for c in WINDOW_CAPABILITIES if backend.has(c)),
        "unsupported": sorted(c for c in WINDOW_CAPABILITIES if not backend.has(c)),
    }


__all__ = [
    "BackendError",
    "CAP_FOCUS",
    "CAP_MONITOR",
    "CAP_MOVE",
    "CAP_RESIZE",
    "CAP_TILE",
    "DesktopBackend",
    "Monitor",
    "Snapshot",
    "Unsupported",
    "WINDOW_CAPABILITIES",
    "Window",
    "Workspace",
    "build",
    "backend_names",
    "describe",
    "detect",
    "preferred",
]
