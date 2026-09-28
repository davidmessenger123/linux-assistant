"""The Adwaita style sheet, replacing 56 scattered hex literals.

The original hardcoded every colour at the point of use and overrode the
``contentItem`` and ``background`` of each control, which meant it ignored the
platform's Qt style entirely and could not follow a light or dark preference.
Doing it properly in one place buys two things: the assistant looks like it
belongs on GNOME, Plasma and anywhere else, and it follows the system theme
without a line of code.
"""

from __future__ import annotations

ACCENT = "#26a269"
ACCENT_FG = "#ffffff"
DANGER = "#c01c28"
SUCCESS = "#26a269"
WARNING = "#e5a50a"

CARD_RADIUS = 12
BUTTON_RADIUS = 9
ENTRY_RADIUS = 10

STYLESHEET = f"""
/* The card the whole conversation sits in. */
assistant-card {{
    background-color: @card_bg_color;
    border-radius: {CARD_RADIUS}px;
}}

/* A message the user sent. */
.message-user {{
    background-color: alpha(currentColor, 0.10);
    border-radius: {BUTTON_RADIUS}px;
    padding: 8px 12px;
}}

/* A reply from the model, including its source list and any action card. */
.message-assistant {{
    padding: 4px 2px;
}}

.message-role {{
    font-size: 0.78em;
    font-weight: 700;
    opacity: 0.65;
    margin-bottom: 2px;
}}

.message-body {{
    font-size: 1em;
}}

/* An action the model wants to take, shown for approval. Nothing happens
   until the user presses one of these two buttons. */
.action-card {{
    background-color: alpha(@accent_bg_color, 0.10);
    border: 1px solid alpha(@accent_color, 0.35);
    border-radius: {BUTTON_RADIUS}px;
    padding: 10px 12px;
    margin-top: 6px;
    margin-bottom: 6px;
}}

.action-card.risk-high {{
    background-color: alpha({DANGER}, 0.10);
    border-color: alpha({DANGER}, 0.45);
}}

.action-summary {{
    font-weight: 600;
}}

.action-detail {{
    font-size: 0.85em;
    opacity: 0.7;
}}

/* Status line above the input. */
.status-line {{
    font-size: 0.82em;
    opacity: 0.6;
    padding: 0 4px 4px 4px;
}}

.status-line.busy {{
    opacity: 0.9;
}}

/* The prompt box. */
.prompt-frame {{
    background-color: @view_bg_color;
    border: 1px solid alpha(currentColor, 0.18);
    border-radius: {ENTRY_RADIUS}px;
    padding: 2px;
}}

.prompt-entry {{
    background: none;
    border: none;
    box-shadow: none;
    padding: 6px 8px;
    min-height: 24px;
}}

.toolbar-button {{
    border-radius: {BUTTON_RADIUS}px;
    min-width: 34px;
    min-height: 34px;
    padding: 0;
}}

.send-button {{
    background-image: none;
    background-color: @accent_bg_color;
    color: @accent_fg;
    border-radius: {BUTTON_RADIUS}px;
    min-width: 34px;
    min-height: 34px;
    padding: 0;
    font-weight: 700;
}}

.send-button:disabled {{
    background-color: alpha(currentColor, 0.15);
    color: alpha(currentColor, 0.4);
}}

/* Tool activity, so a long turn does not look like a hang. */
.tool-line {{
    font-size: 0.80em;
    font-family: monospace;
    opacity: 0.55;
    padding-left: 2px;
}}

.tool-line.refused {{
    color: {DANGER};
    opacity: 0.9;
}}

/* The window is a single card floating over a scrim, which is what the
   original did with a wlr-layer-shell overlay. As an ordinary window it has
   to paint its own background to get a similar effect. */
window assistant-window {{
    background-color: alpha(@window_bg_color, 0.35);
}}
"""


def install() -> None:
    """Register the stylesheet with the default display."""
    from gi.repository import Gdk, Gtk

    provider = Gtk.CssProvider()
    provider.load_from_data(STYLESHEET.encode("utf-8"))
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
