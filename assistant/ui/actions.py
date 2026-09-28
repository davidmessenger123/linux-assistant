"""Window actions and their shortcuts.

The original had three keyboard shortcuts bound in the compositor. The
equivalent here is an application accelerator, which works the same on every
desktop and does not need a keybinding installed in the window manager's config,
which was the part that was Hyprland-only.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gio, Gtk  # noqa: E402

ACCELERATORS = (
    ("<Primary>Return", "send", None),
    ("<Primary>K", "clear", None),
    ("<Primary>W", "toggle-window-list", None),
    ("Escape", "close", None),
)


def install_actions(window) -> None:
    """Attach the actions to a window. Called for each window as it opens."""
    actions = [
        ("send", lambda *_a: window._on_activate(window.entry)),
        ("clear", lambda *_a: _clear(window)),
        ("toggle-window-list", lambda *_a: _show_backend(window)),
        ("close", lambda *_a: window.close()),
    ]
    for name, handler in actions:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", handler)
        window.add_action(action)


def _clear(window) -> None:
    child = window.transcript.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        window.transcript.remove(child)
        child = following
    window._streaming = None
    window._cards = []


def _show_backend(window) -> None:
    """Say what the current backend can do, which is the first thing to check
    when an action is refused on a new desktop."""
    summary = window.backend_summary
    lines = [f"Backend: {summary['backend']}"]
    if summary["capabilities"]:
        lines.append("Can: " + ", ".join(summary["capabilities"]))
    if summary["unsupported"]:
        lines.append("Cannot: " + ", ".join(summary["unsupported"]))
    window._add_message("tool", "\n".join(lines))
    window._scroll_to_end()
