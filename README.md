# linux-assistant

A desktop assistant that works on any Linux distribution and desktop environment,
rather than only on Arch with Hyprland.

This is a fork of [omarchy-assistant](../omarchy-assistant), which is a good
application that happens to be welded to three things: the Hyprland compositor,
the Quickshell toolkit, and `/usr/bin` absolute paths. That version stays where
it is and remains the right choice on Omarchy. This one is for everyone else.

## Status

It runs. `python3 -m assistant` opens a window that talks to a model, sees your
windows, and asks before it changes anything.

| | |
|---|---|
| Working, verified live on Hyprland | window control, portal screenshots, uinput input, the GTK window, a real turn through opencode |
| Implemented, fixture-tested | Sway, niri, KWin, X11 |
| Not done | packaging, the in-app settings panel, recipes, memory, dictation |

The window itself is a normal GTK4 window, so it behaves the same on any
desktop. See [Roadmap](#roadmap).

## Running it

```sh
git clone https://github.com/davidmessenger123/linux-assistant
cd linux-assistant
python3 -m assistant --model opencode/space-bunny-free
```

Shortcuts: <kbd>Ctrl</kbd>+<kbd>Return</kbd> send, <kbd>Ctrl</kbd>+<kbd>W</kbd>
list what the current backend can and cannot do.

## What is different

The original reaches for `hyprctl` directly, and for a Lua dispatcher
(`hl.dsp.focus(...)`) that only exists in recent Hyprland builds. Every window
operation lives in one file. Here, that file is split in two:

- `assistant/backends/` knows a window manager exists. One module per
  compositor, and nothing above this layer names one.
- `assistant/windows.py` is the planner. It resolves which window a request
  means and produces a proposal the user approves. It is the only part the
  model talks to, and it is identical for every compositor.

### Screenshots

The original shipped a hand-built grim binary, fell back to `/usr/bin/grim` on
any other architecture, and worked around Hyprland's screencopy stalling by
switching every other monitor off around each capture. That is a compositor bug
treated as an application concern, and it meant no capture at all on anything
that was not Hyprland on x86-64.

Capture now goes through `xdg-desktop-portal`, which is one API for GNOME,
Plasma and every wlroots compositor, with each desktop's own tool behind it and
X11 tools last. Nothing ever changes monitor state. Frames arriving as raw RGBA
are written by `assistant/io/png.py`, because GStreamer has no PNG encoder
installed on plenty of systems and requiring Pillow just to save a screenshot
would undo the point.

### Typing and clicking

`wtype` only works on wlroots compositors, so typing was impossible on GNOME
and Plasma even though nothing else about them was a problem. Both typing and
clicking are now done by writing to `/dev/uinput` directly, which the kernel
treats identically whatever compositor is running. `wtype`, `ydotool` and
`xdotool` remain as fallbacks for when `/dev/uinput` is not writable, which is
the common case without a udev rule.

### Two more things

**Capabilities are checked before a plan is offered.** Each backend declares what
it can do, and the planner refuses to build a proposal the backend cannot
deliver. On X11 there is no standard way to float a window, so `float` and the
`tile` built on top of it are reported unavailable instead of failing after the
user has approved them.

**Hyprland's dialect problem is fixed.** The original only spoke the Lua
dispatcher and broke on compositor upgrades. Both that and plain
`hyprctl dispatch` are supported, probed once at runtime.

## Backends

| Backend | Needs | Windows | Workspaces | Float | Scratchpad |
|---|---|---|---|---|---|
| `hyprland` | `hyprctl` | yes | yes | yes | yes |
| `sway` | `swaymsg` | yes | yes | yes | no |
| `niri` | `niri` | yes | yes | yes | overview only |
| `kwin` | `gdbus` + KWin 6 | yes | yes | yes | no |
| `x11` | `wmctrl`, `xprop`, `xrandr` | yes | EWMH | no | no |
| `virtual` | nothing | in memory | in memory | yes | yes |

`x11` is the fallback that keeps the assistant usable under XWayland on GNOME
and KDE, where no Wayland compositor runs. Detection probes each backend for
real rather than guessing from `XDG_CURRENT_DESKTOP`, because a GNOME Wayland
session and a Hyprland one look identical there.

Force a backend with `ASSISTANT_BACKEND`, or `--backend` on the command line.

## Requirements

Python 3.9 or newer, `opencode`, PyGObject, and a backend's dependencies from
the table above. No Node, no Bun, no Quickshell, no compositor-specific
packages.

For clicking, either run as root or grant write access to `/dev/uinput`;
`assistant-doctor` explains the three options.

```sh
git clone https://github.com/davidmessenger123/linux-assistant
cd linux-assistant
python3 -m assistant.windows plan list          # read-only, safe to run
python3 -m assistant.io.capture /tmp/shot.png    # takes a screenshot
python3 -m assistant.io.launch search terminal
```

## Tests

```sh
python3 -m unittest discover -s tests
```

Standard library only, so they run anywhere. 115 tests. The Hyprland fixture is
a real captured session; the others are hand-written from each compositor's
documented output, which means they check parsing and shape rather than that
those compositors are installed. The KWin backend is the least proven of the set
and has never run against a live Plasma session.

## Roadmap

1. **Done** — `DesktopBackend` abstraction, six backends, planner.
2. **Done** — portal screenshots, uinput input, `uwsm-app` and
   `omarchy reminder` replaced with GIO desktop files and `systemd-run`.
3. **Done** — the GTK4/libadwaita window. This is what makes GNOME and KDE
   possible at all: the original draws a full-screen overlay through
   `wlr-layer-shell`, which those desktops do not implement.
4. **Next** — an `assistant-doctor`, a `.desktop` file, and a flatpak manifest,
   so installing it is one command rather than a clone and a Python path.
5. **Later** — collapse the TypeScript tool bridge into one local server, which
   removes Node and Bun from the runtime.

## Known gaps

- **The KWin backend has never been run against Plasma.** It is written against
  the KWin 6 scripting API and its parsing is fixture-tested, but nothing has
  confirmed the live behaviour. Sway and niri are in the same position, minus
  being completely unrunnable, because neither is installed here.
- **No window control on GNOME or Plasma under Wayland.** Neither implements
  any window-control API, and neither has a wlr layer shell. Under those
  sessions the backend comes out as `virtual` and the assistant says so; log in
  on X11 and the `x11` backend takes over. Screenshots, typing and clicking all
  still work, because the portal and uinput do not care what drew the screen.
- **The ported features are a subset.** Recipes, memory, dictation, the action
  log and the image backends live in the original and have not been ported yet.
  The layer they would sit on is here and tested.

## Licence

Same terms as the original. `THIRD_PARTY_NOTICES.md` covers the bundled grim
build carried over from it.
