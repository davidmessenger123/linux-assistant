"""Entry point for the GTK application.

Run it with ``python3 -m assistant`` from a checkout. The only argument that
matters is ``--model``; everything else is handled by Adwaita.

Because this is a normal window rather than a compositor overlay, it needs no
layer-shell support, so the same code runs on GNOME, Plasma, Hyprland, Sway and
anything else with a Wayland or X11 session.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from .ui import theme  # noqa: E402
from .ui.window import present  # noqa: E402

APP_ID = "dev.linuxassistant.Assistant"


class Application(Adw.Application):
    def __init__(self, model: str = "") -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.model = model

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        theme.install()
        self._install_accelerators()

    def do_activate(self) -> None:
        window = present(self, self.model)
        window.present()

    def _install_accelerators(self) -> None:
        from .ui.actions import ACCELERATORS, install_actions

        install_actions(self)
        for accelerator, action, target in ACCELERATORS:
            self.set_accels_for_action(f"win.{action}", [accelerator])
            if target:
                self.set_accels_for_action(f"app.{action}", [accelerator])


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    ours: list[str] = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == "--version":
            print("linux-assistant")
            return 0
        if item == "--model":
            if index + 1 < len(arguments):
                ours.append(arguments[index + 1])
            index += 2
            continue
        if item.startswith("--model="):
            ours.append(item.split("=", 1)[1])
            index += 1
            continue
        # Anything left is not ours. GApplication would try to open it as a file
        # and refuse, so it is dropped rather than passed on.
        index += 1

    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Assistant")
    application = Application(ours[0] if ours else "")
    return application.run([])


if __name__ == "__main__":
    sys.exit(main())
