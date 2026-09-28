# linux-assistant

A desktop assistant that works on any Linux distribution and desktop environment,
rather than only on Arch with Hyprland.

This is a fork of [omarchy-assistant](../omarchy-assistant), which is a good
application that happens to be welded to three things: the Hyprland compositor,
the Quickshell toolkit, and `/usr/bin` absolute paths. That version stays where
it is and remains the right choice on Omarchy. This one is for everyone else.

## Status

Milestone 1 is done: the window-control layer is compositor-neutral and tested.

| | |
|---|---|
| Working | Hyprland, verified live against a running session |
| Implemented, fixture-tested | Sway, niri, KWin, X11 |
| Not started | GTK interface, portable screen capture, portable input |

There is no interface yet, so nothing is runnable end to end. See
[Roadmap](#roadmap).

## What is different

The original reaches for `hyprctl` directly, and for a Lua dispatcher
(`hl.dsp.focus(...)`) that only exists in recent Hyprland builds. Every window
operation lives in one file. Here, that file is split in two:

- `assistant/backends/` knows a window manager exists. One module per
  compositor, and nothing above this layer names one.
- `assistant/windows.py` is the planner. It resolves which window a request
  means and produces a proposal the user approves. It is the only part the
  model talks to, and it is identical for every compositor.

Three consequences worth knowing about:

**Capabilities are checked before a plan is offered.** Each backend declares what
it can do, and the planner refuses to build a proposal the backend cannot
deliver. On X11, for example, there is no standard way to float a window, so
`float` and the `tile` built on top of it are reported as unavailable instead of
failing after the user has already approved them.

**Hyprland's dialect problem is fixed.** The original only spoke the Lua
dispatcher and broke on compositor upgrades. Both that and plain
`hyprctl dispatch` are supported, probed once at runtime.

**Tiling no longer leaves a strip.** The original sized columns as
`width // n - gap`, which does not fit `n` columns and `n-1` gaps into the work
area, so an 8px gap was left at one edge. Fixed, with a test.

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

Python 3.9 or newer, `opencode`, and a backend's dependencies from the table
above. No Node, no Bun, no Quickshell, no compositor-specific packages.

```sh
git clone https://github.com/davidmessenger123/linux-assistant
cd linux-assistant
python3 -m assistant.windows plan list      # read-only, safe to run
```

## Tests

```sh
python3 -m unittest discover -s tests
```

Standard library only, so they run anywhere. The Hyprland fixture is a real
captured session; the others are hand-written from each compositor's documented
output, which means they check parsing and shape rather than that those
compositors are installed. The KWin backend is the least proven of the set and
has never run against a live Plasma session.

## Roadmap

1. **Done** — `DesktopBackend` abstraction, six backends, planner, tests.
2. **Next** — screen capture through `xdg-desktop-portal`, input through uinput,
   and removal of the remaining `/usr/bin` assumptions.
3. **Then** — a GTK4/libadwaita interface. This is the part that makes GNOME and
   KDE possible at all: the original draws a full-screen overlay through
   `wlr-layer-shell`, which those desktops do not implement.
4. **Later** — collapse the TypeScript tool bridge into one local server, which
   removes Node and Bun from the runtime.

## Licence

Same terms as the original. `THIRD_PARTY_NOTICES.md` covers the bundled grim
build carried over from it.
