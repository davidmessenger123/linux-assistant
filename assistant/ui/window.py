"""The assistant's window: a conversation, a prompt, and approval cards.

This is the part that decides whether the fork works on GNOME and Plasma at all.
The original drew a full-screen overlay through ``wlr-layer-shell`` and grabbed
every keystroke on the desktop, which only wlroots compositors implement. An
ordinary GTK window works on every desktop, Wayland or X11, and gets a real
window title, alt-tab entry, focus handling and accessibility for free.

Two things carried over from the original and kept deliberately:

* Nothing the model proposes happens without a visible card and a press. The
  card is built from an :class:`~assistant.actions.Action`, so the same
  validation applies however the interface is drawn.
* Tool activity is shown as it happens. A turn that takes thirty seconds with
  no feedback reads as a hang.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from ..agent import Event, Session, Turn  # noqa: E402
from ..actions import Action  # noqa: E402
from ..backends import describe, detect  # noqa: E402
from . import theme  # noqa: E402

CSS = """
.assistant-window { background-color: transparent; }
.assistant-card { background-color: @card_bg_color; border-radius: 12px; }
"""


class Message(Gtk.Box):
    """One entry in the transcript."""

    def __init__(self, role: str, text: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.get_style_context().add_class("message-user" if role == "user" else "message-assistant")
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        if role != "tool":
            heading = Gtk.Label(label=role.capitalize())
            heading.add_css_class("message-role")
            heading.set_halign(Gtk.Align.START)
            self.append(heading)

        self.label = Gtk.Label(label=text)
        self.label.set_wrap(True)
        self.label.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.label.set_selectable(role == "assistant")
        self.label.add_css_class("message-body")
        self.label.set_halign(Gtk.Align.START)
        self.append(self.label)

    def set_text(self, text: str) -> None:
        self.label.set_text(text)
        self.label.set_visible(bool(text))


class ActionCard(Gtk.Box):
    """A proposal waiting on the user, with the two buttons that decide it."""

    def __init__(self, action: Action, on_decide) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.get_style_context().add_class("action-card")
        if action.risk == "high":
            self.get_style_context().add_class("risk-high")
        self.action = action
        self.settled = False

        summary = Gtk.Label(label=action.summary)
        summary.set_wrap(True)
        summary.add_css_class("action-summary")
        summary.set_halign(Gtk.Align.START)
        self.append(summary)

        if action.detail:
            detail = Gtk.Label(label=action.detail)
            detail.set_wrap(True)
            detail.add_css_class("action-detail")
            detail.set_halign(Gtk.Align.START)
            self.append(detail)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.END)
        decline = Gtk.Button(label="No")
        decline.add_css_class("flat")
        decline.connect("clicked", lambda *_a: self._decide(False, on_decide))
        allow = Gtk.Button(label="Do it")
        allow.add_css_class("suggested-action")
        allow.connect("clicked", lambda *_a: self._decide(True, on_decide))
        row.append(decline)
        row.append(allow)
        self.append(row)

    def _decide(self, allowed: bool, on_decide) -> None:
        if self.settled:
            return
        self.settled = True
        on_decide(self.action, allowed)


class AssistantWindow(Adw.ApplicationWindow):
    def __init__(self, application, model: str = "") -> None:
        super().__init__(application=application, title="Assistant")
        self.set_default_size(780, 720)
        self.set_size_request(420, 400)
        self.session = Session(model=model, app_dir=_app_dir())
        self.turn: Turn | None = None
        self._streaming: Message | None = None
        self._cards: list[ActionCard] = []

        backend, notes = detect()
        self.backend_name = backend.name
        self.backend_summary = describe(backend)

        self._build()
        self._banner_for(notes)

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Assistant", subtitle=f"{self.backend_name} backend")
        header.set_title_widget(title)
        root.append(header)

        self.banner = Adw.Banner(revealed=False)
        root.append(self.banner)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("assistant-card")
        card.set_margin_top(10)
        card.set_margin_bottom(14)
        card.set_margin_start(14)
        card.set_margin_end(14)
        root.append(card)

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

        self.transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.transcript.set_margin_top(6)
        self.transcript.set_margin_bottom(6)
        self.transcript.set_margin_start(8)
        self.transcript.set_margin_end(8)
        scroller.set_child(self.transcript)
        card.append(scroller)

        self.status = Gtk.Label(label="Ready")
        self.status.add_css_class("status-line")
        self.status.set_halign(Gtk.Align.START)
        card.append(self.status)

        self.entry = Gtk.Entry(placeholder_text="Ask for something, or say what to do next")
        self.entry.add_css_class("prompt-entry")
        self.entry.connect("activate", self._on_activate)

        prompt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        frame.add_css_class("prompt-frame")
        frame.append(self.entry)
        prompt_row.append(frame)

        self.send_button = Gtk.Button(label="Send")
        self.send_button.add_css_class("send-button")
        self.send_button.set_valign(Gtk.Align.CENTER)
        self.send_button.connect("clicked", lambda *_a: self._on_activate(self.entry))
        prompt_row.append(self.send_button)
        card.append(prompt_row)

        self._add_message("assistant", "Hello. I can see your windows, take screenshots, "
                                       "and propose changes for you to approve.")

    def _banner_for(self, notes: list[str]) -> None:
        useful = [n for n in notes if "not available" in n or "cannot" in n or "No supported" in n]
        if useful:
            self.banner.set_title(notes[0])
            self.banner.set_revealed(True)

    def _add_message(self, role: str, text: str = "") -> Message:
        message = Message(role, text)
        self.transcript.append(message)
        self._scroll_to_end()
        return message

    def _scroll_to_end(self) -> None:
        def later() -> bool:
            scroller = self.get_content()
            while scroller is not None and not isinstance(scroller, Gtk.ScrolledWindow):
                scroller = scroller.get_parent() if hasattr(scroller, "get_parent") else None
            if scroller is not None:
                scroller.set_vadjustment(
                    Gtk.Adjustment(
                        value=scroller.get_vadjustment().get_upper(),
                        lower=0,
                        upper=scroller.get_vadjustment().get_upper(),
                        page_size=scroller.get_vadjustment().get_page_size(),
                        step_increment=48,
                        page_increment=240,
                    )
                )
            return False

        GLib.idle_add(later)

    def _on_activate(self, _entry) -> None:
        prompt = self.entry.get_text().strip()
        if not prompt or (self.turn and not self.turn.finished.is_set()):
            return
        self.entry.set_text("")
        self._add_message("user", prompt)
        self._send(prompt)

    def _send(self, prompt: str) -> None:
        self._set_busy(True, "Thinking")
        self._streaming = self._add_message("assistant", "")

        def on_event(event: Event) -> None:
            GLib.idle_add(self._apply, event)

        def on_text(chunk: str) -> None:
            GLib.idle_add(self._append_stream, chunk)

        self.turn = self.session.send(prompt, on_event=on_event, on_text=on_text)

    def _apply(self, event: Event) -> bool:
        if event.kind == "status":
            self._set_busy(True, event.text)
        elif event.kind == "tool":
            self._add_message("tool", event.text)
        elif event.kind == "refused":
            self._add_message("tool", f"refused: {event.text}")
            self._set_busy(True, "That request was not allowed")
        elif event.kind == "action" and event.action is not None:
            self._add_card(event.action)
            self._set_busy(True, "Waiting for you")
        elif event.kind == "window_plan" and event.action is not None:
            GLib.idle_add(self._run_window_plan, event.action)
        elif event.kind == "finished":
            self._finish(event.text or self._current_error())
        return False

    def _append_stream(self, chunk: str) -> bool:
        if self._streaming is None:
            return False
        current = self._streaming.label.get_text()
        self._streaming.label.set_text(current + chunk)
        self._streaming.label.set_visible(True)
        self._scroll_to_end()
        return False

    def _run_window_plan(self, action: Action) -> bool:
        from ..windows import plan

        try:
            proposal = plan(self._backend(), action.payload["op"], _namespace(action.payload["arguments"]))
        except Exception as error:  # noqa: BLE001 - shown in the transcript
            self._add_message("tool", f"window plan refused: {error}")
            return False
        if not proposal.get("ok"):
            self._add_message("tool", f"window plan refused: {proposal.get('error')}")
            return False
        if not proposal.get("requires_approval"):
            return False
        follow = Action(
            kind="window_run",
            summary=proposal.get("summary", "Window action"),
            detail=proposal.get("detail", ""),
            requires_approval=True,
            payload={"proposal": proposal},
        )
        self._add_card(follow)
        return False

    def _backend(self):
        backend, _notes = detect()
        return backend

    def _add_card(self, action: Action) -> ActionCard:
        card = ActionCard(action, self._on_decide)
        self._cards.append(card)
        if self._streaming is not None:
            self._streaming.append(card)
        else:
            self.transcript.append(card)
        self._scroll_to_end()
        return card

    def _on_decide(self, action: Action, allowed: bool) -> None:
        from ..actions import execute

        if not allowed:
            self._add_message("tool", f"declined: {action.summary}")
            self._set_busy(False, "Ready")
            return
        self._set_busy(True, action.summary)
        self._add_message("tool", f"running: {action.summary}")

        def work() -> None:
            try:
                result = execute(action, self._backend())
            except Exception as error:  # noqa: BLE001 - reported, never swallowed
                result = {"ok": False, "error": str(error)}
            GLib.idle_add(self._report, result)

        GLib.idle_add(lambda: _spawn(work) and False)

    def _report(self, result: dict) -> bool:
        if result.get("ok"):
            self._add_message("tool", f"done: {result.get('summary') or result.get('method') or 'ok'}")
            self._set_busy(False, "Ready")
        else:
            self._add_message("tool", f"failed: {result.get('error') or 'unknown error'}")
            self._set_busy(False, "That did not work")
        return False

    def _current_error(self) -> str:
        return self.turn.error if self.turn and self.turn.error else ""

    def _finish(self, problem: str = "") -> None:
        if self._streaming is not None and not self._streaming.label.get_text().strip():
            self.transcript.remove(self._streaming)
        self._streaming = None
        if problem:
            self._add_message("tool", f"turn ended: {problem}")
            self._set_busy(False, "Stopped")
        else:
            self._set_busy(False, "Ready")
        self.entry.set_sensitive(True)

    def _set_busy(self, busy: bool, text: str) -> None:
        self.status.set_text(text)
        self.status.get_style_context().add_class("busy" if busy else "status-line")
        if not busy:
            self.status.get_style_context().remove_class("busy")
        self.send_button.set_sensitive(not busy)


def _app_dir() -> str:
    import os
    import pathlib

    return os.environ.get("ASSISTANT_APP_DIR") or str(pathlib.Path(__file__).resolve().parent.parent.parent)


def _namespace(arguments: dict):
    from ..actions import _namespace as build

    return build(arguments)


def _spawn(work) -> bool:
    import threading

    threading.Thread(target=work, daemon=True).start()
    return True


def present(application, model: str = "") -> AssistantWindow:
    window = application.props.active_window
    if window is None:
        window = AssistantWindow(application, model)
    window.present()
    return window
