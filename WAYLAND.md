# Running Ember under Wayland

**Status: unverified.** Ember was built and has only ever run on X11 (GNOME
Shell 46, `XDG_SESSION_TYPE=x11`). Everything below is derived from how GDK's
Wayland backend behaves, not from a measured run. Treat it as a map of where to
look first, not as a test report.

Check which session you are on before anything else:

```sh
echo $XDG_SESSION_TYPE
```

Ubuntu has defaulted to Wayland since 21.04, and since 24.04 that includes
machines on the NVIDIA proprietary driver. Assume Wayland unless told otherwise.

## What breaks

Ember's window model is built out of X11 hints. Under a GTK3 Wayland backend
most of `_build_window()` becomes a silent no-op — xdg-shell has no window
types, no stacking hints, and no client-side positioning. Nothing errors; the
calls just stop meaning anything.

| Call | Site | Under Wayland |
|---|---|---|
| `set_type_hint(DESKTOP)` | `ember.py:88` | No-op. Loses `Super+D` survival — the reason the hint exists. |
| `set_keep_below()` | `ember.py:108-112` | No-op. No desktop layer. |
| `move()` | `ember.py:126`, `:480` | No-op. Cannot self-place; drag-to-move stops working. |
| `get_position()` | `ember.py:471`, `:484` | Returns `(0,0)`. The saved `x`/`y` config becomes meaningless. |
| `event.x_root` / `y_root` | `ember.py:477` | Surface-relative, not root-relative — drag delta math is wrong regardless of `move()`. |
| `stick()`, `set_skip_taskbar_hint()` | `ember.py:82`, `:92` | No-ops. Ember starts appearing in alt-tab and the dash. |

Net effect: Ember degrades into an ordinary floating window that the compositor
parks wherever it likes.

Two things do survive: RGBA transparency, and the click-through region at
`ember.py:427` — `input_shape_combine_region` maps onto `wl_surface.set_input_region`,
which is core Wayland protocol.

## The fix: force Xwayland

One line, in `~/.config/autostart/ember.desktop`:

```
Exec=env GDK_BACKEND=x11 python3 /home/satvik/Desktop/ember/ember.py
```

Xwayland restores real X11 semantics, and Mutter honours
`_NET_WM_WINDOW_TYPE_DESKTOP` and `keep_below` for X11 clients. This should
recover essentially all of the behaviour above.

Known cost: **Xwayland renders blurry under fractional scaling.** If the display
is at 125% or 150%, expect soft text. That is the main reason to check before
assuming this is a free fix.

A slightly better version of the same idea is to set the backend inside
`ember.py` before importing Gtk, so the behaviour travels with the code rather
than living in one machine's autostart file:

```python
import os
if os.environ.get("XDG_SESSION_TYPE") == "wayland":
    os.environ.setdefault("GDK_BACKEND", "x11")
```

This must run before `gi.repository.Gtk` is imported — GDK picks its backend at
import time.

## There is no native Wayland path

Worth knowing before sinking time into one.

The standard answer for desktop-layer windows is `gtk-layer-shell`, which
depends on the `wlr-layer-shell` protocol. **Mutter does not implement it.**
Under GNOME Wayland the only genuine way to sit on the desktop layer is to *be*
a Shell extension.

That is the same wall as the existing stacking constraint: `keep_below` already
renders Ember *under* the wallpaper in some configurations, and only a Shell
extension gets the real desktop layer. So "port Ember to Wayland properly" is
not an afternoon of fixes — it is a rewrite into a GNOME Shell extension, with a
different language (GJS), a different UI toolkit (Clutter/St instead of GTK),
and no direct way to spawn a subprocess the way `runner.py` does today.

Recommendation: use the Xwayland fallback. Only consider the rewrite if the
fractional-scaling blur is genuinely unacceptable.

## Smaller Wayland caveats

- **`wmctrl` and `xdotool`** (allowed in `settings.json`) only see Xwayland
  windows under Wayland. Window-manipulation requests become unreliable and
  will fail in ways the model may report as success. Consider removing them
  from the allowlist on a Wayland machine so Ember doesn't reach for a tool
  that half-works.
- **Everything else in the allowlist is session-agnostic** — `gtk-launch`,
  `pkill`/`pgrep`, `playerctl` (D-Bus/MPRIS), `pactl`, `brightnessctl`,
  `notify-send` all behave identically.
- **The persona hardcodes X11.** `runner.py:20` tells the model it lives on an
  "Ubuntu, GNOME, X11" desktop. On a Wayland machine that is a false statement
  fed to the model every turn, and it will shape how it reasons about window
  and display problems. Make it reflect the actual session.
- **`_on_window_state` / `deiconify`** (`ember.py:460`) is an X11-shaped
  workaround. Under Xwayland it should keep working; under native Wayland the
  iconified state is not something the client controls the same way.

## Related repos

Not part of this repo, recorded here because the assessment covered them
together.

**The GNOME widgets extension needs no changes.** It runs inside the
`gnome-shell` process using Clutter/St and `Main.layoutManager._backgroundGroup`.
X11 and Wayland are identical from in there.

**The desktop folder widgets (the DING fork) need no Wayland changes either** —
upstream DING already handles this. `desktopManager.js` detects the
GTK-X11-under-Wayland case explicitly, and `emulateX11WindowType.js` exists so
Mutter treats DING's window as the desktop under Wayland. The fork only touched
GSettings logic and left that machinery alone.

Their real risks are unrelated to the session type, and are likelier to bite
first:

- The DING fork installs by copying whole files over root-owned
  `/usr/share/gnome-shell/extensions/ding@rastersoft.com/`. That needs sudo on a
  work machine, is silently reverted by any `apt upgrade` of the DING package,
  and — being a file copy rather than a patch — breaks outright against a
  different upstream DING version. `metadata.json` pins
  `shell-version: ["45","46"]`, fine for Ubuntu 24.04 (GNOME 46) and not for
  anything newer.
- The ricing restore script can never report healthy on another machine as
  written: `configuration_is_healthy()` requires every entry in
  `REQUIRED_EXTENSIONS` to be enabled, including
  `disable-unredirect-fullscreen-windows@local.codex` — a local hack that is
  meaningless on Wayland, where there is no unredirection to disable. The
  hardcoded wallpaper path and the Tahoe/MacTahoe theme names also have to exist
  there.
