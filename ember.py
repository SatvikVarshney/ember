#!/usr/bin/env python3
"""Ember -- a warm, ambient assistant widget for the GNOME/X11 desktop.

GTK3 rather than GTK4 on purpose: GTK4 dropped set_keep_below(),
set_type_hint() and set_skip_taskbar_hint(), which are exactly the X11 hints
needed to sit on the desktop layer.

The window itself never resizes. It is a fixed transparent canvas and the card
inside it animates, which avoids janky window-manager resizes; an input shape
region keeps clicks on the transparent area falling through to the desktop.

Two views share the card. The *launcher* is the quick one: a greeting, the
input and local results. The *chat* is a scrolling transcript -- it takes over
as soon as a question goes to the model, survives the card being folded away,
and only ends on Ctrl+N or when the session ages out. One-shot replies that
vanished after a few seconds turned out to be the wrong shape: most real
problems need a few rounds of back and forth.
"""

import atexit
import math
import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkPixbuf, GdkX11, GLib, GObject, Gtk, Pango, PangoCairo  # noqa: E402

import cairo  # noqa: E402

import config as cfg  # noqa: E402
import ipc  # noqa: E402
import launcher  # noqa: E402
import render  # noqa: E402
import storyteller  # noqa: E402
import tracker  # noqa: E402
from runner import EmberRunner, resolve_model, strip_markdown  # noqa: E402

IDLE, LISTENING, THINKING, RESPONDING = "idle", "listening", "thinking", "responding"
LAUNCHER, CHAT = "launcher", "chat"

DEBUG = bool(os.environ.get("EMBER_DEBUG"))


def trace(*parts):
    if DEBUG:
        print(f"[{time.monotonic():9.3f}]", *parts, file=sys.stderr, flush=True)

# The canvas is the fixed transparent window the card grows inside. It is sized
# for the tallest chat card, and the card is positioned around the resting
# dot's anchor but clamped inside the canvas, so a tall card near the bottom of
# the screen shifts up instead of being clipped.
CANVAS_W, CANVAS_H_MAX = 780, 920
LAUNCHER_MAX_H = 460
# Where the resting dot sits on first run, measured up from the work-area bottom.
DOT_ABOVE_BOTTOM = 150
ANIM_MS = 16
ANIM_DURATION = 0.30
# A sent message lifting out of the input bar and settling into its bubble.
FLY_DURATION = 0.38
BUBBLE_RADIUS = 18
# Opening and closing are a shape change, not just a resize, so they get a
# little longer for the morph to actually be seen.
MORPH_DURATION = 0.42
# The card's own corner radius, and the band the dot's outer ring becomes
# around it when open: 9px is ~2.5mm on this 1080p 24" panel.
CARD_RADIUS = 26
RING_BAND = 9
# Card padding (the inner box's border width).
PAD = 20

# Ceiling for the results list. Left deliberately short of the card ceiling so
# the entry and footer always have room -- the list scrolls rather than pushing
# them out of the card.
MAX_RESULTS_H = 260

# Messages kept on disk for the restored transcript. Old enough turns stop
# mattering on screen long before they stop mattering to the session.
TRANSCRIPT_KEEP = 80

# Focus-out must not act instantly. Pressing the hotkey makes gnome-shell take
# focus for its own key grab, and that fires focus-out about 30ms BEFORE the
# toggle message arrives on the socket. Acting straight away meant the toggle
# then found an idle widget and re-summoned it, so the hotkey could open Ember
# but never close it. Deferring a few frames lets the toggle land first.
FOCUS_OUT_GRACE_MS = 140

# A run is not interruptible by accident. The model chains real commands, and
# dying between two of them can leave the machine worse off than never having
# asked. So the first Escape only warns, and a second within this window stops.
FORCE_CANCEL_MS = 3000

# Cadence of the "working…" ellipsis. Slow enough to read as breathing rather
# than as a spinner.
ELLIPSIS_MS = 420

# What a tool call is doing, in the widget's own voice, for when the model gave
# no description of its own.
TOOL_ACTIVITY = {
    "Bash": "running a command",
    "WebSearch": "searching the web",
    "WebFetch": "reading a page",
    "Read": "reading a file",
    "Write": "writing a file",
    "Edit": "editing a file",
    "Glob": "looking around",
    "Grep": "looking around",
    "ToolSearch": "getting ready",
}


def _ms_since(started):
    return int((time.monotonic() - started) * 1000)


def ease_out_cubic(t):
    return 1 - pow(1 - t, 3)


def lerp(a, b, t):
    return a + (b - a) * t


def rounded_rect(ctx, x, y, w, h, radius):
    radius = max(0.0, min(radius, w / 2, h / 2))
    ctx.new_sub_path()
    ctx.arc(x + w - radius, y + radius, radius, -math.pi / 2, 0)
    ctx.arc(x + w - radius, y + h - radius, radius, 0, math.pi / 2)
    ctx.arc(x + radius, y + h - radius, radius, math.pi / 2, math.pi)
    ctx.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _activity_from(step):
    """A step's plain-language label: the model's own description when it gave
    one ("List paired Bluetooth devices"), else a generic verb."""
    text = (step.get("description") or "").strip().rstrip(".")
    if not text:
        return TOOL_ACTIVITY.get(step.get("name"), "working")
    text = text[0].lower() + text[1:]
    return text if len(text) <= 58 else text[:56].rstrip() + "…"


def _steps_markup(items):
    """The expanded body of a steps fold: what each step was for, and under it
    the exact command, small and monospaced. Hidden until asked for."""
    lines = []
    for item in items:
        desc = GLib.markup_escape_text(item.get("description") or TOOL_ACTIVITY.get(item.get("name"), item.get("name") or ""))
        cmd = GLib.markup_escape_text(item.get("command") or "")
        entry = desc
        if cmd:
            entry += f"\n<span font_family=\"monospace\" size=\"small\">{cmd}</span>"
        lines.append(entry)
    return "\n\n".join(lines)


def _code_markup(code):
    return f"<span font_family=\"monospace\" size=\"small\">{GLib.markup_escape_text(code)}</span>"


def _wrap_label(markup="", css=None, selectable=True):
    label = Gtk.Label(xalign=0.0, yalign=0.0)
    label.set_line_wrap(True)
    label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_markup(markup)
    if selectable:
        # Selectable so a reply can be copied, but never focusable: the entry
        # has to keep the keyboard or typing a follow-up stops working.
        label.set_selectable(True)
        label.set_can_focus(False)
    if css:
        label.get_style_context().add_class(css)
    return label


class Chip(Gtk.EventBox):
    """A small text button. EventBox rather than Gtk.Button so the theme's
    button chrome never fights the card's look."""

    def __init__(self, text, on_click, css="ember-chip"):
        super().__init__()
        self.set_visible_window(False)
        self.label = Gtk.Label(label=text)
        self.label.get_style_context().add_class(css)
        self.add(self.label)
        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                        | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("enter-notify-event", lambda *_: self._hot(True))
        self.connect("leave-notify-event", lambda *_: self._hot(False))
        self.connect("button-press-event", self._on_press)
        self._on_click = on_click

    def _hot(self, on):
        ctx = self.label.get_style_context()
        ctx.add_class("hot") if on else ctx.remove_class("hot")
        return False

    def _on_press(self, widget, event):
        if event.button == 1:
            self._on_click(event)
            return True
        return False

    def set_text(self, text):
        self.label.set_text(text)


class Fold(Gtk.Box):
    """A quiet one-line summary that opens to show detail on click -- how
    commands and code stay out of the conversation without being lost."""

    def __init__(self, on_resize):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._on_resize = on_resize
        self._open = False
        self._title = ""
        self.header = Chip("", self._toggle, css="ember-fold")
        self.header.set_halign(Gtk.Align.START)
        self.pack_start(self.header, False, False, 0)
        self.body = _wrap_label(css="ember-fold-body")
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.NONE)
        self.revealer.add(self.body)
        self.pack_start(self.revealer, False, False, 0)

    def set_title(self, title):
        self._title = title
        self.header.set_text(("▾  " if self._open else "›  ") + title)

    def set_body(self, markup):
        self.body.set_markup(markup)

    def _toggle(self, *_):
        self._open = not self._open
        self.revealer.set_reveal_child(self._open)
        self.set_title(self._title)
        self._on_resize()


def _glyph_spark(ctx, w, h, t, energy, rgb):
    """Off the cuff: a quick little spark that can't keep still."""
    cx, base = w / 2, h * 0.72
    hop = abs(math.sin(t * (4.5 + 5 * energy)))
    lift = (h * 0.42) * hop * (0.45 + 0.55 * energy)
    for lag, fade in ((0.09, 0.18), (0.045, 0.35)):
        trail = abs(math.sin((t - lag) * (4.5 + 5 * energy))) * (h * 0.42) * (0.45 + 0.55 * energy)
        ctx.set_source_rgba(*rgb, fade)
        ctx.arc(cx, base - trail, 3.2, 0, 2 * math.pi)
        ctx.fill()
    # Squashes a touch on landing, which is most of what makes a hop read.
    squash = 1 + 0.2 * (1 - hop) ** 4
    ctx.save()
    ctx.translate(cx, base - lift)
    ctx.scale(squash, 1 / squash)
    ctx.set_source_rgba(*rgb, 1.0)
    ctx.arc(0, 0, 5.5, 0, 2 * math.pi)
    ctx.fill()
    ctx.restore()


def _glyph_breathe(ctx, w, h, t, energy, rgb):
    """Thinking cap: Ember itself, in miniature, breathing as usual."""
    cx, cy = w / 2, h / 2
    phase = (math.sin(t * 2 * math.pi / (2.6 - energy)) + 1) / 2
    core = 7.5 * (0.8 + 0.2 * phase)
    ctx.set_source_rgba(*rgb, 0.25 * (0.6 + 0.4 * phase))
    ctx.arc(cx, cy, core * 1.75, 0, 2 * math.pi)
    ctx.fill()
    ctx.set_source_rgba(*rgb, 0.6 + 0.4 * phase)
    ctx.arc(cx, cy, core, 0, 2 * math.pi)
    ctx.fill()


def _glyph_orbit(ctx, w, h, t, energy, rgb):
    """Sleeves rolled up: a core with three dots hard at work around it."""
    cx, cy = w / 2, h / 2
    ctx.set_source_rgba(*rgb, 1.0)
    ctx.arc(cx, cy, 5.5, 0, 2 * math.pi)
    ctx.fill()
    spin = t * (1.4 + 3.2 * energy)
    for i in range(3):
        angle = spin + i * 2 * math.pi / 3
        # A tilted orbit, so they swing past rather than just going round.
        x = cx + 13 * math.cos(angle)
        y = cy + 6 * math.sin(angle)
        behind = math.sin(angle) < 0
        ctx.set_source_rgba(*rgb, 0.45 if behind else 0.95)
        ctx.arc(x, y, 2.4 if behind else 3.0, 0, 2 * math.pi)
        ctx.fill()


def _glyph_steam(ctx, w, h, t, energy, rgb):
    """Slow burn: an ember glowing away while the kettle gets going."""
    cx, cy = w / 2, h * 0.7
    glow = (math.sin(t * 2 * math.pi / 4.5) + 1) / 2
    ctx.set_source_rgba(*rgb, 0.22 + 0.18 * glow)
    ctx.arc(cx, cy, 10 + 2 * glow, 0, 2 * math.pi)
    ctx.fill()
    ctx.set_source_rgba(*rgb, 0.75 + 0.25 * glow)
    ctx.arc(cx, cy, 6, 0, 2 * math.pi)
    ctx.fill()
    ctx.set_line_width(1.8)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    rise_speed = 0.35 + 0.45 * energy
    for i, offset in enumerate((-5.0, 4.0)):
        life = (t * rise_speed + i * 0.5) % 1.0
        ctx.set_source_rgba(*rgb, 0.7 * math.sin(life * math.pi))
        top = cy - 12 - life * (h * 0.45)
        for step in range(9):
            k = step / 8
            y = top + k * 9
            x = cx + offset + 2.5 * math.sin(k * 5 + t * 2.2 + i)
            ctx.line_to(x, y) if step else ctx.move_to(x, y)
        ctx.stroke()


MOOD_GLYPHS = {"haiku": _glyph_spark, "sonnet": _glyph_breathe,
               "opus": _glyph_orbit, "fable": _glyph_steam}


class MoodTile(Gtk.EventBox):
    """One choice in the model tray: a little animated glyph acting out the
    mood, its name, and its blurb. Paints its own pill so it matches the card
    rather than the theme."""

    def __init__(self, model, on_pick, palette):
        super().__init__()
        self.set_visible_window(False)
        self.alias = model["alias"]
        self._on_pick = on_pick
        self._palette = palette
        self.hot = False
        self.picked = False
        self._energy = 0.0
        self._pop_at = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        self.glyph = Gtk.DrawingArea()
        self.glyph.set_size_request(64, 52)
        self.glyph.connect("draw", self._draw_glyph)
        box.pack_start(self.glyph, False, False, 0)
        title = Gtk.Label(label=model["label"])
        title.get_style_context().add_class("ember-mood-title")
        box.pack_start(title, False, False, 0)
        blurb = Gtk.Label(label=model["blurb"])
        blurb.set_line_wrap(True)
        blurb.set_justify(Gtk.Justification.CENTER)
        blurb.set_max_width_chars(14)
        blurb.get_style_context().add_class("ember-mood-blurb")
        box.pack_start(blurb, False, False, 0)
        self.add(box)

        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                        | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("enter-notify-event", lambda *_: self._set_hot(True))
        self.connect("leave-notify-event", self._on_leave)
        self.connect("button-press-event", self._on_press)
        self.connect("draw", self._draw_pill)

    def _set_hot(self, on):
        self.hot = on
        self.queue_draw()
        return False

    def _on_leave(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self._set_hot(False)
        return False

    def _on_press(self, widget, event):
        if event.button == 1:
            self._on_pick(self)
            return True
        return False

    def pop(self):
        self._pop_at = time.monotonic()

    def tick(self, t, dt):
        # Hover and the current pick are fully awake; the rest idle gently.
        target = 1.0 if (self.hot or self.picked) else 0.25
        self._energy += (target - self._energy) * min(1.0, dt * 8)
        self._t = t
        self.glyph.queue_draw()

    def _draw_pill(self, widget, ctx):
        if not (self.hot or self.picked):
            return False
        a = widget.get_allocation()
        ctx.set_source_rgba(*self._palette()["dot"], 0.32 if self.picked else 0.16)
        rounded_rect(ctx, 0, 0, a.width, a.height, 16)
        ctx.fill()
        return False

    def _draw_glyph(self, widget, ctx):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        scale = 1.0
        if self._pop_at is not None:
            # A springy pop on being picked: up past full size and back.
            k = (time.monotonic() - self._pop_at) / 0.45
            if k >= 1:
                self._pop_at = None
            else:
                scale = 1 + 0.45 * math.sin(k * math.pi) * (1 - k)
        # Glyphs are drawn on a 48x40 grid and scaled up to the area.
        zoom = min(w / 48, h / 40) * scale
        ctx.translate(w / 2, h / 2)
        ctx.scale(zoom, zoom)
        ctx.translate(-24, -20)
        MOOD_GLYPHS.get(self.alias, _glyph_breathe)(
            ctx, 48, 40, getattr(self, "_t", 0.0), self._energy, self._palette()["dot"])
        return False


class ChatInput(Gtk.ScrolledWindow):
    """The input bar: a text box that wraps and grows with what is typed, up to
    a few lines and then scrolls. Enter sends, Shift+Enter starts a new line.

    Speaks the small slice of Gtk.Entry the rest of Ember uses (get_text,
    set_text, set_position, set_placeholder_text, grab_focus), so swapping it
    in left every caller alone. Signals: "submit" and "text-changed".
    """

    __gsignals__ = {
        "submit": (GObject.SignalFlags.RUN_LAST, None, ()),
        "text-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "resized": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    MAX_LINES = 5

    def __init__(self):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        self.set_shadow_type(Gtk.ShadowType.NONE)
        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_accepts_tab(False)
        self.view.set_left_margin(15)
        self.view.set_right_margin(15)
        self.view.set_top_margin(9)
        self.view.set_bottom_margin(9)
        self.view.get_style_context().add_class("ember-input-text")
        self.add(self.view)
        self.buffer = self.view.get_buffer()
        self.buffer.connect("changed", lambda *_: self._on_changed())
        self.view.connect("key-press-event", self._on_key)
        self.view.connect_after("draw", self._draw_placeholder)
        self.view.connect("style-updated", lambda *_: self._fit_lines())
        # The TextView lays its lines out in idle time, after "changed", so
        # the height is only known later; it is measured then, and whenever
        # the layout reports a change (a new width re-wraps the lines too).
        self._content_h = 0
        self._measure_id = None
        self.get_vadjustment().connect("changed", lambda *_: self._queue_measure())
        self._placeholder = ""
        self._fit_lines()

    def _fit_lines(self):
        # Cap the growth at MAX_LINES of the current font; past that it scrolls.
        metrics = self.view.get_pango_context().get_metrics(None, None)
        line = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE
        self._max_h = int(line * self.MAX_LINES * 1.25 + 18)
        self._content_h = 0
        self._queue_measure()

    def _queue_measure(self):
        # Low priority: after the TextView's own validation idle has run.
        if self._measure_id is None:
            self._measure_id = GLib.idle_add(self._measure, priority=GLib.PRIORITY_LOW)

    def _measure(self):
        """Size the bar to its text, from the laid-out lines themselves.

        Neither GTK's height-for-width (a few pixels short, clipping the top
        line) nor the scroll adjustment (never below the current height, so it
        could only grow) gives the real figure.
        """
        self._measure_id = None
        first_y, _ = self.view.get_line_yrange(self.buffer.get_start_iter())
        last_y, last_h = self.view.get_line_yrange(self.buffer.get_end_iter())
        content = (last_y + last_h - first_y) + self.view.get_top_margin() + self.view.get_bottom_margin()
        height = min(content, self._max_h)
        overflowing = content > self._max_h
        # No scrolling at all until it overflows: scrolling while it was still
        # growing left it a line down with the first line hidden, and the
        # stale offset never cleared once it had room.
        self.set_policy(Gtk.PolicyType.NEVER,
                        Gtk.PolicyType.AUTOMATIC if overflowing else Gtk.PolicyType.NEVER)
        if height > 1 and height != self._content_h:
            self._content_h = height
            self.set_size_request(-1, height)
            self.emit("resized")
        if overflowing:
            self.view.scroll_mark_onscreen(self.buffer.get_insert())
        else:
            self.get_vadjustment().set_value(0)
        return GLib.SOURCE_REMOVE

    def _on_changed(self):
        self._queue_measure()
        self.view.queue_draw()
        self.emit("text-changed")

    def _on_key(self, widget, event):
        key = Gdk.keyval_name(event.keyval)
        if key in ("Return", "KP_Enter"):
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                return False  # the TextView inserts the newline
            self.emit("submit")
            return True
        return False

    def _draw_placeholder(self, widget, ctx):
        if not self._placeholder or self.buffer.get_char_count():
            return False
        layout = widget.create_pango_layout(self._placeholder)
        color = widget.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        ctx.set_source_rgba(color.red, color.green, color.blue, 0.4)
        ctx.move_to(widget.get_left_margin(), widget.get_top_margin())
        PangoCairo.show_layout(ctx, layout)
        return False

    # -- the Entry-shaped API ---------------------------------------------

    def get_text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)

    def set_text(self, text):
        self.buffer.set_text(text)

    def set_position(self, position):
        if position < 0:
            self.buffer.place_cursor(self.buffer.get_end_iter())
        else:
            self.buffer.place_cursor(self.buffer.get_iter_at_offset(position))

    def set_placeholder_text(self, text):
        self._placeholder = text or ""
        self.view.queue_draw()

    def grab_focus(self):
        self.view.grab_focus()


class Segment(Gtk.Box):
    """One block of model text. Prose renders as markup; fenced code goes
    behind a fold. Updated in place while it streams, rebuilt only when the
    prose/code structure changes."""

    def __init__(self, on_resize):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_resize = on_resize
        self._kinds = []
        self.raw = ""

    def append(self, delta):
        self.set_raw(self.raw + delta)

    def set_raw(self, raw):
        self.raw = raw
        parts = render.pieces(raw)
        kinds = [kind for kind, _ in parts]
        if kinds != self._kinds:
            for child in self.get_children():
                self.remove(child)
            for kind, _ in parts:
                if kind == "prose":
                    widget = _wrap_label(css="ember-text")
                else:
                    widget = Fold(self._on_resize)
                self.pack_start(widget, False, False, 0)
            self._kinds = kinds
            self.show_all()
        for widget, (kind, body) in zip(self.get_children(), parts):
            if kind == "prose":
                widget.set_markup(body)
            else:
                lines = body.count("\n") + 1
                widget.set_title(f"code · {lines} line{'s' if lines != 1 else ''}")
                widget.set_body(_code_markup(body))


class Ember(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = cfg.load_config()
        self.runner = EmberRunner(self.config)

        self.state = IDLE
        self.view = LAUNCHER
        self.hovered = False
        self.response_text = ""
        self.last_session_id = None
        self.rate_limited = False

        self._anim_id = None
        self._dwell_id = None
        self._idle_timeout_id = None
        self._fold_id = None
        self._menu_open = False
        self._pulse_phase = 0.0
        self._drag_origin = None
        # The outer ring's pull towards the cursor: a deformation vector in
        # ring radii plus its velocity, chased towards _jelly_target by a
        # spring so it lags, overshoots and settles like something soft.
        self._jelly = [0.0, 0.0, 0.0, 0.0]
        self._jelly_target = (0.0, 0.0)
        # 0 = breathing freely, 1 = held at full size under the cursor.
        self._swell = 0.0
        # 0 = the resting dot, 1 = the open card; the dot's breath scale at
        # the moment it opened, so the morph starts from exactly what was seen.
        self._morph = 0.0
        self._morph_from_scale = 1.0
        self._card_rect = (0, 0, 1, 1)
        # A message in flight from the input bar to its bubble: where it left
        # from, what it says, and the bubble it is heading for.
        self._fly = None
        self._fly_from = None

        # The model for the conversation on screen. Starts at the configured
        # default; the chip, Alt+1-4, Ctrl+M or a "/opus" prefix change it, and
        # it is saved with the transcript so a restored chat keeps its model.
        self._model = self.config.get("model", "sonnet")
        self._pending_model = self._model

        # Local resolution: the whole point is that "brave" or "vol 40" never
        # reaches a model. `_results` is what is currently on offer, `_rows`
        # pairs each ListBox row with the Result it will activate, and `_sel`
        # is the highlighted index -- tracked by hand because the entry keeps
        # keyboard focus, so the ListBox never gets the arrow keys itself.
        self._index = launcher.AppIndex()
        self._results = []
        self._rows = []
        self._sel = 0
        self._raised = False
        self._focus_collapse_id = None

        # Progress and the interrupt guard. `_activity` is the line in the
        # footer while a run is going; `_force_cancel_id` is live only in the
        # seconds after a first Escape, and its existence is what makes the
        # second one bite.
        self._activity = ""
        self._ellipsis_id = None
        self._ellipsis_step = 0
        self._force_cancel_id = None
        # A TinyStories sentence about the step under way, which takes over the
        # activity line once it lands (a second or so in). Cleared whenever
        # the step changes, so it never describes something already finished.
        self._story_line = ""
        self._storyteller = storyteller.Storyteller() if self.config.get("story_lines", True) else None

        # The in-flight model turn, held open from _ask_model until whichever
        # terminal event closes it. One slot rather than a stack because the
        # runner refuses a second concurrent run anyway -- follow-ups typed
        # meanwhile wait in `_queue` instead.
        self._pending_turn = None
        self._queue = []
        # Bumped per run and on a forced stop, so the late events of a killed
        # run can't land in the conversation that replaced it.
        self._generation = 0

        # The conversation: `_messages` is the saved record, `_turn` the reply
        # being streamed right now.
        saved = cfg.load_transcript()
        self._messages = saved.get("messages") or []
        self._transcript_session = saved.get("session_id")
        self._transcript_model = saved.get("model")
        self._transcript_at = saved.get("updated_at") or 0
        self._chat_built = False
        self._turn = None
        self._stick_bottom = True

        self._build_window()
        self._build_ui()
        self._apply_css()

        # First scan reads ~190 files; off the UI thread so startup stays snappy.
        threading.Thread(target=self._index.refresh, daemon=True).start()

        GLib.timeout_add(ANIM_MS, self._on_pulse_tick)

    # -- window plumbing ---------------------------------------------------

    def _workarea(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_workarea()

    def _build_window(self):
        area = self._workarea()
        self.canvas_w = min(CANVAS_W, area.width)
        self.canvas_h = min(CANVAS_H_MAX, area.height)
        # Card ceilings follow the canvas so nothing is ever clipped.
        self.chat_max_h = min(self.config.get("chat_max_height", 760), self.canvas_h - 24)
        self.launcher_max_h = min(LAUNCHER_MAX_H, self.chat_max_h)
        # The dot's position inside the canvas; recomputed on placement.
        self._anchor_in = (self.canvas_w // 2, self.canvas_h // 2)

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(True)
        self.set_app_paintable(True)
        self.set_default_size(self.canvas_w, self.canvas_h)
        self.set_size_request(self.canvas_w, self.canvas_h)
        self.set_type_hint({
            "desktop": Gdk.WindowTypeHint.DESKTOP,
            "dock": Gdk.WindowTypeHint.DOCK,
        }.get(self.config.get("window_type"), Gdk.WindowTypeHint.NORMAL))
        self.stick()
        self._apply_stacking()

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self._on_key)
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("focus-in-event", self._on_focus_in)
        self.connect("window-state-event", self._on_window_state)
        self.connect("realize", lambda *_: self._place_window())

    def _apply_stacking(self):
        # `_raised` is the hotkey summon. Ember normally lives below working
        # windows, but a command centre opened by Super+Space has to be on top
        # of whatever is in front. Mutter does honour ABOVE on a DESKTOP-type
        # window and still gives it focus -- verified on GNOME 46 / X11.
        if self._raised or self.config.get("keep_above"):
            self.set_keep_below(False)
            self.set_keep_above(True)
        else:
            self.set_keep_above(False)
            self.set_keep_below(True)

    def _place_window(self):
        """Put the canvas so the resting dot lands on its saved anchor, with the
        canvas clamped on-screen. When clamping moves the canvas, the anchor's
        position *inside* it moves instead, so the dot still sits where it was
        left and the card simply has less room on that side."""
        area = self._workarea()
        ax, ay = self.config.get("anchor_x"), self.config.get("anchor_y")
        if ax is None or ay is None:
            ax = area.x + area.width // 2
            ay = area.y + area.height - DOT_ABOVE_BOTTOM
        x = max(area.x, min(ax - self.canvas_w // 2, area.x + area.width - self.canvas_w))
        y = max(area.y, min(ay - self.canvas_h // 2, area.y + area.height - self.canvas_h))
        self._anchor_in = (ax - x, ay - y)
        self.move(x, y)
        alloc = self.card.get_allocation() if hasattr(self, "card") else None
        if alloc and alloc.width > 1:
            self._apply_card_size(alloc.width, alloc.height)

    # -- ui ----------------------------------------------------------------

    def _build_ui(self):
        canvas = Gtk.Fixed()
        self.add(canvas)
        self._canvas = canvas

        self.card = Gtk.EventBox()
        self.card.get_style_context().add_class("ember-card")
        self.card.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.card.connect("enter-notify-event", self._on_enter)
        self.card.connect("leave-notify-event", self._on_leave)
        self.card.connect("button-press-event", self._on_button_press)
        self.card.connect("button-release-event", self._on_button_release)
        self.card.connect("motion-notify-event", self._on_motion)
        self.card.connect("draw", self._draw_card)
        # After the children, so the flying message passes over the chat.
        self.card.connect_after("draw", self._draw_flight)
        canvas.put(self.card, 0, 0)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.card.add(inner)
        self._inner = inner

        # The resting dot is the entire presence on the desktop.
        self.dot = Gtk.DrawingArea()
        size = self.config["dot_size"]
        self.dot.set_size_request(size, size)
        self.dot.set_halign(Gtk.Align.CENTER)
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.connect("draw", self._draw_dot)
        inner.pack_start(self.dot, True, True, 0)

        # Launcher view: one label carries the greeting (or a local result such
        # as a calculation), so it reads as something said rather than as
        # placeholder chrome.
        self.msg = _wrap_label(css="ember-text", selectable=False)
        self.msg.set_max_width_chars(42)
        self._msg_scroll = Gtk.ScrolledWindow()
        self._msg_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._msg_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self._msg_scroll.add(self.msg)
        inner.pack_start(self._msg_scroll, True, True, 0)

        # Chat view: the transcript.
        self.chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.chat_box.set_valign(Gtk.Align.END)
        self._chat_scroll = Gtk.ScrolledWindow()
        self._chat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._chat_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self._chat_scroll.add(self.chat_box)
        adj = self._chat_scroll.get_vadjustment()
        adj.connect("changed", self._on_chat_adj_changed)
        adj.connect("value-changed", self._on_chat_scrolled)
        # In a chat the local results float over the bottom of the transcript
        # instead of taking a slice of the card: pushing the chat and the input
        # up and down as matches came and went on each keystroke was the shake.
        self._chat_overlay = Gtk.Overlay()
        self._chat_overlay.add(self._chat_scroll)
        inner.pack_start(self._chat_overlay, True, True, 0)

        self.entry = ChatInput()
        self.entry.get_style_context().add_class("ember-input")
        self.entry.connect("submit", self._on_submit)
        self.entry.connect("text-changed", self._on_typing)
        self.entry.connect("resized", lambda *_: self.state != IDLE and self._relayout())
        inner.pack_start(self.entry, False, False, 0)

        # Results sit *below* the input, which is the layout every launcher
        # uses and the one the hands already know.
        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results.set_activate_on_single_click(True)
        self.results.get_style_context().add_class("ember-results")
        self.results.connect("row-activated", self._on_row_activated)

        self._results_scroll = Gtk.ScrolledWindow()
        self._results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._results_scroll.set_propagate_natural_height(True)
        self._results_scroll.set_max_content_height(MAX_RESULTS_H)
        self._results_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self._results_scroll.add(self.results)
        inner.pack_start(self._results_scroll, False, False, 0)
        self._results_floating = False

        # The model tray: the chip unfolds it inside the card rather than
        # popping a theme-coloured menu out of it.
        self.mood_tray = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        self._mood_tiles = []
        palette = lambda: {"dot": hex_to_rgb(cfg.accent_colors(self.config)["dot"])}
        for model in cfg.MODELS:
            tile = MoodTile(model, self._on_mood_pick, palette)
            self._mood_tiles.append(tile)
            self.mood_tray.pack_start(tile, True, True, 0)
        self._show_widget(self.mood_tray, False)
        self._mood_tick_id = None
        self._mood_close_id = None
        inner.pack_start(self.mood_tray, False, False, 0)

        # Footer: what's happening on the left, the conversation's controls on
        # the right. The model chip is always there once the card is open, so
        # picking the right model is one click rather than a phrase to recall.
        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.status = Gtk.Label(xalign=0.0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("ember-footer")
        self.status_chip = Chip("", self._on_status_click, css="ember-footer")
        self.status_chip.remove(self.status_chip.label)
        self.status_chip.label = self.status
        self.status_chip.add(self.status)
        self.footer.pack_start(self.status_chip, True, True, 0)
        self.new_chip = Chip("new chat", lambda *_: self._new_chat())
        self.new_chip.set_tooltip_text("Start a fresh conversation (Ctrl+N)")
        self.footer.pack_end(self.model_chip_box(), False, False, 0)
        self.footer.pack_end(self.new_chip, False, False, 0)
        inner.pack_start(self.footer, False, False, 0)
        self._status_action = None

    def model_chip_box(self):
        self.model_chip = Chip("", self._show_model_menu)
        self.model_chip.set_tooltip_text(
            "How hard to think about this chat — Alt+1–4 or Ctrl+M to switch")
        self._paint_model_chip()
        return self.model_chip

    def _apply_css(self):
        colors = cfg.accent_colors(self.config)
        surface = self.config["surface"]
        font = self.config["font_family"]
        size = self.config["font_size"]
        text = colors["text"]
        dot = colors["dot"]
        css = f"""
        window {{ background-color: transparent; }}
        /* The card's surface and its ring are painted in _draw_card, so the
           dot can morph into them; CSS only has to stay out of the way. */
        .ember-card {{ background-color: transparent; }}
        /* The input is the bubble-to-be: same tint, shape and type as your
           sent messages, stretched edge to edge. */
        .ember-card scrolledwindow.ember-input {{
            background-color: alpha({dot}, 0.30);
            background-image: none;
            border: none;
            border-radius: {BUBBLE_RADIUS}px;
            box-shadow: none;
        }}
        .ember-input-text, .ember-input-text text {{
            background-color: transparent;
            color: {text};
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {size - 1}px;
            caret-color: {text};
        }}
        .ember-input-text text selection {{ background-color: alpha({dot}, 0.55); color: {text}; }}
        .ember-card scrolledwindow.ember-input undershoot,
        .ember-card scrolledwindow.ember-input overshoot {{ background: none; }}
        /* Results floating over a chat need their own paper, or the
           transcript shows through them. */
        .ember-card scrolledwindow.ember-float {{
            background-color: {surface};
            border: 1px solid alpha({dot}, 0.55);
            border-radius: 16px;
            padding: 4px;
        }}
        .ember-card scrolledwindow,
        .ember-card viewport {{ background-color: transparent; }}
        .ember-card scrollbar {{ background-color: transparent; border: none; }}
        .ember-card scrollbar slider {{
            background-color: alpha({text}, 0.25);
            border: none;
            min-width: 5px;
        }}
        .ember-text {{
            color: {text};
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {size}px;
        }}
        .ember-text selection {{ background-color: alpha({dot}, 0.55); color: {text}; }}
        /* Your side of the conversation: a soft tinted bubble on the right.
           Ember's side is bare text -- it's the one talking. */
        .ember-you {{
            color: {text};
            background-color: alpha({dot}, 0.30);
            border-radius: {BUBBLE_RADIUS}px;
            padding: 8px 15px;
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {size - 1}px;
        }}
        .ember-note {{
            color: alpha({text}, 0.55);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(12, size - 3)}px;
            font-style: italic;
        }}
        .ember-fold {{
            color: alpha({text}, 0.42);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(11, size - 5)}px;
            padding: 1px 8px 1px 2px;
            border-radius: 9px;
        }}
        .ember-fold.hot {{ color: alpha({text}, 0.8); }}
        .ember-fold-body {{
            color: alpha({text}, 0.78);
            background-color: alpha({text}, 0.06);
            border-radius: 12px;
            padding: 10px 14px;
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(11, size - 5)}px;
        }}
        .ember-footer {{
            color: alpha({text}, 0.45);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(11, size - 6)}px;
        }}
        .ember-footer.hot {{ color: alpha({text}, 0.75); }}
        .ember-chip {{
            color: alpha({text}, 0.55);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(11, size - 6)}px;
            padding: 3px 10px;
            border-radius: 11px;
        }}
        .ember-chip.hot {{
            color: {text};
            background-color: alpha({dot}, 0.30);
        }}
        /* Rows have to sit inside the cream surface, not on top of it, so the
           list itself stays transparent and only the selection is painted. */
        .ember-results, .ember-results row {{
            background-color: transparent;
            border: none;
        }}
        .ember-results row {{ border-radius: 14px; }}
        .ember-results row:selected {{
            background-color: alpha({dot}, 0.38);
        }}
        .ember-results row:hover {{
            background-color: alpha({dot}, 0.18);
        }}
        .ember-row-title {{
            color: {text};
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(13, size - 3)}px;
        }}
        .ember-row-sub {{
            color: alpha({text}, 0.5);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(10, size - 7)}px;
        }}
        .ember-mood-title {{
            color: {text};
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(12, size - 5)}px;
            font-weight: 600;
        }}
        .ember-mood-blurb {{
            color: alpha({text}, 0.5);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(10, size - 7)}px;
        }}
        tooltip {{ border-radius: 10px; }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._css_provider = provider

    # -- painting ----------------------------------------------------------

    def _draw_dot(self, widget, ctx):
        r, g, b = hex_to_rgb(cfg.accent_colors(self.config)["dot"])
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        cx, cy = w / 2, h / 2

        if self.state == IDLE:
            scale, alpha, speed = self._breath()
        else:
            scale, alpha = 1.0, 1.0

        # 0.5 keeps the outer ring (1.75x) inside the allocation; anything
        # larger clips the ring against the widget edge.
        core = (min(w, h) / 2) * 0.5 * scale

        # Soft outer ring so it reads as a deliberate object on the wallpaper
        # rather than a stray speck, without resorting to a gradient.
        # Hover belongs to the ring alone: the core keeps breathing, and the
        # ring firms up a touch and reaches for the cursor.
        ring_alpha = 0.34 if self.hovered and self.state == IDLE else 0.25
        ctx.set_source_rgba(r, g, b, alpha * ring_alpha)
        if self.state == IDLE:
            self._trace_jelly(ctx, cx, cy, core * 1.75)
        else:
            ctx.arc(cx, cy, core * 1.75, 0, 2 * math.pi)
        ctx.fill()

        ctx.set_source_rgba(r, g, b, alpha)
        if self.state == IDLE:
            self._trace_wobble(ctx, cx, cy, core, speed)
        else:
            ctx.arc(cx, cy, core, 0, 2 * math.pi)
        ctx.fill()
        return False

    def _breath(self):
        """(scale, alpha, period) of the resting dot right now."""
        # Breathes quicker while a run is still going behind a folded card.
        speed = 0.85 if self.runner.busy else 2.6
        phase = (math.sin(self._pulse_phase * (2 * math.pi) / speed) + 1) / 2
        scale = 0.78 + 0.22 * phase
        alpha = 0.6 + 0.4 * phase
        # Under the cursor the breath is drawn up to full and held there;
        # already at full, nothing visibly changes.
        scale += (1.0 - scale) * self._swell
        alpha += (1.0 - alpha) * self._swell
        return scale, alpha, speed

    def _draw_card(self, widget, ctx):
        """The card's surface: the dot's core grown into the cream box, its
        outer ring grown into a thin band around it.

        Both shapes are interpolated from the dot's circles (centred in the
        card) to their final rounded rectangles, while the card itself is being
        resized by _animate_card -- so at 0 this draws the dot, at 1 the card.
        Runs before the children draw, so the content sits on top.
        """
        e = self._morph
        if e <= 0.0:
            return False
        x, y, w, h = self._card_rect
        # The dot sits on the anchor, which is the card's centre unless the
        # card is pinned against a canvas edge; the circles start from there
        # and travel to the box's centre.
        ax, ay = self._anchor_in
        cx = lerp(ax - x, w / 2, e)
        cy = lerp(ay - y, h / 2, e)
        core = self.config["dot_size"] / 4 * self._morph_from_scale
        ring = core * 1.75
        accent = hex_to_rgb(cfg.accent_colors(self.config)["dot"])
        surface = hex_to_rgb(self.config["surface"])

        # Corners lag the size, so it stays a stretched blob for a moment
        # rather than turning into a rounded square straight away.
        corner = e * e
        ctx.set_source_rgba(*accent, lerp(0.25, 0.45, e))
        rounded_rect(ctx, lerp(cx - ring, 0, e), lerp(cy - ring, 0, e),
                     lerp(2 * ring, w, e), lerp(2 * ring, h, e),
                     lerp(ring, CARD_RADIUS + RING_BAND, corner))
        ctx.fill()

        # The colour lags the shape slightly, so it still reads as the dot
        # stretching before it turns into paper.
        tint = min(1.0, e * 1.25)
        ctx.set_source_rgb(*(lerp(a, b, tint) for a, b in zip(accent, surface)))
        rounded_rect(ctx, lerp(cx - core, RING_BAND, e), lerp(cy - core, RING_BAND, e),
                     lerp(2 * core, w - 2 * RING_BAND, e), lerp(2 * core, h - 2 * RING_BAND, e),
                     lerp(core, CARD_RADIUS, corner))
        ctx.fill()
        return False

    def _set_morph(self, value):
        """Advance the open/close morph and fade whatever is inside to match:
        content only arrives once the box is mostly formed, and on the way back
        the dot fades in as the box closes around it."""
        self._morph = value
        if self.state == IDLE:
            self._inner.set_opacity(1.0 - value)
        else:
            self._inner.set_opacity(max(0.0, min(1.0, (value - 0.55) / 0.45)))
        self.card.queue_draw()

    def _trace_wobble(self, ctx, cx, cy, core, speed):
        """The core as a gently deforming blob rather than a rigid disc.

        A couple of slow lobes drift round the edge at different rates, so the
        shape never quite repeats. The wobble is tied to the breath's velocity:
        it swells while the dot is growing or shrinking and settles at the top
        and bottom of each breath, which is what makes it read as liquid
        moving rather than as a shape that is merely distorted.
        """
        t = self._pulse_phase
        velocity = abs(math.cos(t * (2 * math.pi) / speed))
        amount = 0.018 + 0.03 * velocity
        steps = 96
        for i in range(steps + 1):
            theta = 2 * math.pi * i / steps
            ripple = (0.6 * math.sin(2 * theta + t * 1.1)
                      + 0.4 * math.sin(3 * theta - t * 0.7 + 1.3))
            radius = core * (1 + amount * ripple)
            x = cx + radius * math.cos(theta)
            y = cy + radius * math.sin(theta)
            if i == 0:
                ctx.move_to(x, y)
            else:
                ctx.line_to(x, y)
        ctx.close_path()

    def _trace_jelly(self, ctx, cx, cy, ring):
        """The outer ring as a soft membrane the cursor tugs on.

        It stretches towards the pull, pinches slightly at the sides and gives
        a little at the back, so it reads as one body being drawn out rather
        than a circle sliding over. A faint surface ripple rides on top while
        the jelly is still moving, and dies away as it settles.
        """
        jx, jy, vx, vy = self._jelly
        pull = math.hypot(jx, jy)
        heading = math.atan2(jy, jx)
        shiver = min(1.0, math.hypot(vx, vy) * 0.15)
        t = self._pulse_phase
        steps = 96
        for i in range(steps + 1):
            theta = 2 * math.pi * i / steps
            c = math.cos(theta - heading)
            # Front +0.11, back -0.06, sides -0.025 at full pull: the front
            # stays inside the 0.5 allocation headroom at the top of a breath.
            stretch = pull * (0.085 * c + 0.05 * (c * c - 0.5))
            ripple = 0.012 * shiver * math.sin(3 * theta - t * 7.0)
            radius = ring * (1 + stretch + ripple)
            x = cx + radius * math.cos(theta)
            y = cy + radius * math.sin(theta)
            if i == 0:
                ctx.move_to(x, y)
            else:
                ctx.line_to(x, y)
        ctx.close_path()

    def _step_jelly(self, dt):
        """Underdamped spring towards the cursor's pull. Returns whether the
        jelly is still moving, so a settled ring costs nothing extra."""
        jx, jy, vx, vy = self._jelly
        tx, ty = self._jelly_target
        stiffness, damping = 90.0, 7.5
        vx += (stiffness * (tx - jx) - damping * vx) * dt
        vy += (stiffness * (ty - jy) - damping * vy) * dt
        jx += vx * dt
        jy += vy * dt
        if abs(jx - tx) + abs(jy - ty) + abs(vx) + abs(vy) < 1e-3:
            jx, jy, vx, vy = tx, ty, 0.0, 0.0
        self._jelly = [jx, jy, vx, vy]
        return bool(vx or vy)

    def _on_pulse_tick(self):
        if self.state == IDLE:
            self._pulse_phase += ANIM_MS / 1000.0
            self._step_jelly(ANIM_MS / 1000.0)
            # Exponential ease: quick enough to feel like a response, slow
            # enough (~0.3s) to be seen growing rather than snapping.
            target = 1.0 if self.hovered else 0.0
            self._swell += (target - self._swell) * min(1.0, ANIM_MS / 1000.0 * 11)
            self.dot.queue_draw()
        return GLib.SOURCE_CONTINUE

    # -- interrupt guard ---------------------------------------------------

    @property
    def busy_working(self):
        """True while a run is in flight and must not be killed by a stray
        keypress, click or hotkey.

        Both halves matter. `runner.busy` is the authority, but it is cleared on
        the worker thread, so between the subprocess ending and the `done` event
        reaching the GTK loop there is a window where the state is still THINKING
        and the reply has not been shown yet."""
        return self.runner.busy or self.state == THINKING

    def _arm_force_cancel(self):
        self._cancel_force_cancel()
        self._force_cancel_id = GLib.timeout_add(FORCE_CANCEL_MS, self._on_force_cancel_lapse)
        self._paint_activity()

    def _cancel_force_cancel(self):
        if self._force_cancel_id is not None:
            GLib.source_remove(self._force_cancel_id)
            self._force_cancel_id = None

    def _on_force_cancel_lapse(self):
        self._force_cancel_id = None
        if self.state == THINKING:
            self._paint_activity()  # back to the plain activity line
        return GLib.SOURCE_REMOVE

    def _force_stop(self):
        """The deliberate second Escape: kill the run, keep the conversation."""
        self._close_turn("cancelled")
        self._generation += 1
        self.runner.cancel()
        self._cancel_force_cancel()
        dropped = len(self._queue)
        self._queue = []
        if self._turn is not None:
            self._turn_note("Stopped." + (" Dropped what you queued." if dropped else ""))
            self._end_turn()
        self._set_state(LISTENING)

    # -- activity line -----------------------------------------------------

    def _start_ellipsis(self):
        if self._ellipsis_id is None:
            self._ellipsis_step = 0
            self._ellipsis_id = GLib.timeout_add(ELLIPSIS_MS, self._on_ellipsis_tick)
        self._paint_activity()

    def _stop_ellipsis(self):
        if self._ellipsis_id is not None:
            GLib.source_remove(self._ellipsis_id)
            self._ellipsis_id = None

    def _on_ellipsis_tick(self):
        if self.state != THINKING:
            self._ellipsis_id = None
            return GLib.SOURCE_REMOVE
        self._ellipsis_step += 1
        self._paint_activity()
        return GLib.SOURCE_CONTINUE

    def _paint_activity(self):
        """The card's one honest signal that something is still happening.

        A pending force-cancel takes the line over: if the user has already
        pressed Escape once, telling them how to actually stop matters more
        than telling them what is running.
        """
        if self._force_cancel_id is not None:
            self._set_status("still working — esc again to stop")
            return
        queued = f"  ·  {len(self._queue)} queued" if self._queue else ""
        if self._story_line:
            self._set_status(f"{self._story_line}{queued}")
            return
        dots = "." * (1 + self._ellipsis_step % 3)
        self._set_status(f"{self._activity or 'thinking'}{dots}{queued}")

    def _tell_story(self, step):
        """Ask TinyStories for a line about this step. It is used only if the
        same run is still thinking when it arrives; a newer step's request
        replaces this one in the storyteller anyway."""
        self._story_line = ""
        if not self._storyteller:
            return
        generation = self._generation

        def landed(line):
            GLib.idle_add(self._on_story, generation, line)

        self._storyteller.tell(step.get("description") or self._activity, landed)

    def _on_story(self, generation, line):
        if generation == self._generation and self.state == THINKING:
            self._story_line = line
            self._paint_activity()
        return GLib.SOURCE_REMOVE

    def _set_status(self, text, action=None):
        """Footer text. `action`, when given, makes it clickable."""
        self.status.set_text(text)
        self._status_action = action

    def _on_status_click(self, *_):
        if self._status_action:
            self._status_action()

    # -- state machine -----------------------------------------------------

    def _idle_size(self):
        # Never smaller than the dot: the resting card is also the hit region,
        # and one smaller than the dot left only its top-left part hoverable
        # (and pushed the dot's centre off the anchor).
        dot = self.config["dot_size"]
        return max(self.config["idle_width"], dot), max(self.config["idle_height"], dot)

    def _card_width(self):
        if self.state == IDLE:
            return self._idle_size()[0]
        # Plus the ring band either side, so the text keeps the width it had.
        width = self.config["chat_width"] if self.view == CHAT else self.config["active_width"]
        return width + 2 * RING_BAND

    def _target_geometry(self):
        if self.state == IDLE:
            return self._idle_size()
        return self._card_width(), self._content_height()

    def _content_height(self):
        # _inner carries its own border width, so the padding is counted here
        # once and only once -- double-counting it left a dead gap under the text.
        extra = 2 * (PAD + RING_BAND)
        body = 0
        spacing = self._inner.get_spacing()
        ceiling = self.chat_max_h if self.view == CHAT else self.launcher_max_h
        width = self._card_width() - 2 * (PAD + RING_BAND)

        if self.entry.get_visible():
            extra += self.entry.get_preferred_height_for_width(width)[1] + spacing
        if self.footer.get_visible():
            extra += self.footer.get_preferred_height()[1] + spacing
        if self.mood_tray.get_visible():
            extra += self.mood_tray.get_preferred_height_for_width(width)[1] + spacing
        if self._results_scroll.get_visible():
            _, rows_h = self.results.get_preferred_height()
            rows_h = min(rows_h, MAX_RESULTS_H)
            self._results_scroll.set_size_request(-1, rows_h)
            if not self._results_floating:
                extra += rows_h + spacing

        room = max(40, ceiling - extra)
        if self._msg_scroll.get_visible():
            # Height must be computed *for the known width*: a wrapping label
            # asked for its plain preferred height reports almost nothing.
            self.msg.set_size_request(width, -1)
            _, text_h = self.msg.get_preferred_height_for_width(width)
            visible_h = min(text_h, room)
            self._msg_scroll.set_size_request(-1, visible_h)
            body += visible_h
        if self._chat_scroll.get_visible():
            _, chat_h = self.chat_box.get_preferred_height_for_width(width - 10)
            visible_h = min(chat_h, room)
            self._chat_scroll.set_size_request(-1, visible_h)
            body += visible_h
        return max(72, min(body + extra, ceiling))

    def _body_visibility(self):
        """(greeting, transcript, results). The greeting and the results list
        are mutually exclusive in the launcher: while the list is up the
        greeting would only push the rows further from the input."""
        if self.state == IDLE:
            return False, False, False
        listing = bool(self._results) and self.state in (LISTENING, RESPONDING, THINKING)
        if self.view == CHAT:
            return False, True, listing
        return (self.state in (LISTENING, RESPONDING) and not listing), False, listing

    @staticmethod
    def _show_widget(widget, visible, deep=False):
        widget.set_no_show_all(not visible)
        widget.set_visible(visible)
        if visible:
            widget.show_all() if deep else widget.show()

    def _relayout(self, animate=True):
        showing_msg, showing_chat, showing_results = self._body_visibility()
        self._show_widget(self._msg_scroll, showing_msg, True)
        self._show_widget(self._chat_scroll, showing_chat, True)
        self._show_widget(self._chat_overlay, showing_chat)
        self._float_results(showing_chat)
        self._show_widget(self._results_scroll, showing_results, True)
        self._show_widget(self.new_chip, self.view == CHAT and self.state != THINKING, True)
        self._animate_card(*self._target_geometry(), animate)

    def _float_results(self, floating):
        """Move the results list between the card body (launcher) and an
        overlay on the transcript (chat)."""
        if floating == self._results_floating:
            return
        scroll = self._results_scroll
        style = scroll.get_style_context()
        scroll.get_parent().remove(scroll)
        if floating:
            scroll.set_valign(Gtk.Align.END)
            scroll.set_margin_bottom(6)
            style.add_class("ember-float")
            self._chat_overlay.add_overlay(scroll)
        else:
            scroll.set_valign(Gtk.Align.FILL)
            scroll.set_margin_bottom(0)
            style.remove_class("ember-float")
            self._inner.pack_start(scroll, False, False, 0)
            # Back to its launcher slot: directly under the input.
            self._inner.reorder_child(scroll, self._inner.get_children().index(self.entry) + 1)
        self._results_floating = floating

    def _set_state(self, state, animate=True):
        if self.state == IDLE and state != IDLE:
            self._morph_from_scale = self._breath()[0]
        previous, self.state = self.state, state
        if state not in (LISTENING, THINKING):
            # Offers belong to the query that produced them; carrying them into
            # a reply would leave stale rows under the answer.
            self._results = []
            self._clear_rows()

        open_ = state != IDLE
        if state in (IDLE, THINKING) and self.mood_tray.get_visible():
            self._set_mood_tray(False)
        self._show_widget(self.dot, not open_)
        self._show_widget(self.entry, open_)
        self._show_widget(self.footer, open_, True)
        if self.view == CHAT and open_ and not self._chat_built:
            self._rebuild_chat()

        self._inner.set_border_width(PAD + RING_BAND if open_ else 0)
        self._update_opacity()

        if state == THINKING:
            self._start_ellipsis()
        else:
            self._stop_ellipsis()
            # Leaving THINKING means the run is over one way or another, so a
            # half-armed "press again to stop" must not survive into the reply.
            self._cancel_force_cancel()
            if previous == THINKING:
                self._set_status("")
            self._activity = ""
            self._story_line = ""

        # Any route back to rest also drops the hotkey raise, so Ember can
        # never get stranded above the working windows.
        if state == IDLE:
            self._cancel_fold()
            if self._raised:
                self._raised = False
                self._apply_stacking()

        if open_:
            # In a chat the input sits under a wall of text; a faint prompt
            # keeps it findable when the caret blinks off.
            self.entry.set_placeholder_text(
                "Ask me anything…" if self.view == LAUNCHER else
                "Add something — it'll go next" if state == THINKING else "Reply…")
            self.entry.grab_focus()
            self._arm_idle_timeout()
        else:
            self._cancel_idle_timeout()

        self._relayout(animate)

    def _on_typing(self, *_):
        # Starting a follow-up must not be cut off by any pending collapse.
        self._cancel_dwell()
        self._cancel_focus_collapse()
        self._arm_idle_timeout()
        self._refresh_results()

    # -- conversation ------------------------------------------------------

    def _chat_resumable(self):
        """Whether reopening should bring the conversation back on screen."""
        if not self._messages:
            return False
        recent = time.time() - self._transcript_at < self.config.get("chat_resume_minutes", 30) * 60
        return recent and self.runner.will_resume()

    def _save_transcript(self):
        self._messages = self._messages[-TRANSCRIPT_KEEP:]
        self._transcript_at = time.time()
        self._transcript_model = self._model
        try:
            cfg.save_transcript({
                "session_id": self.last_session_id or self._transcript_session,
                "model": self._model,
                "updated_at": self._transcript_at,
                "messages": self._messages,
            })
        except OSError:
            pass

    def _new_chat(self):
        if self.busy_working:
            self._arm_force_cancel()
            return
        self.runner.new_session()
        self._messages = []
        self._transcript_session = None
        self.last_session_id = None
        self._save_transcript()
        self._clear_chat()
        self._model = self.config.get("model", "sonnet")
        self._paint_model_chip()
        self.view = LAUNCHER
        self._open_input()

    def _clear_chat(self):
        for child in self.chat_box.get_children():
            self.chat_box.remove(child)
        self._turn = None
        self._chat_built = False

    def _rebuild_chat(self):
        """Recreate the transcript widgets from the saved record."""
        self._clear_chat()
        # History appears in place; only the message being sent gets to fly.
        flight, self._fly_from = self._fly_from, None
        for message in self._messages:
            if message.get("role") == "you":
                self._add_bubble(message.get("text", ""))
            else:
                self._add_reply(message)
        self._fly_from = flight
        self._chat_built = True
        self._stick_bottom = True
        self.chat_box.show_all()

    def _add_bubble(self, text, queued=False):
        label = _wrap_label(GLib.markup_escape_text(text), css="ember-you")
        label.set_max_width_chars(38)
        label.set_xalign(0.0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.pack_end(label, False, False, 0)
        if queued:
            row.set_opacity(0.55)
        row.show_all()
        self.chat_box.pack_start(row, False, False, 0)
        self._stick_bottom = True
        start, self._fly_from = self._fly_from, None
        if start is not None:
            self._fly_bubble(label, text, start, 0.55 if queued else 1.0)
        return row

    # -- sent-message flight -----------------------------------------------

    def _entry_rect(self):
        """The input bar's rectangle in card coordinates, or None."""
        if not self.entry.get_visible():
            return None
        coords = self.entry.translate_coordinates(self.card, 0, 0)
        alloc = self.entry.get_allocation()
        if not coords or alloc.width <= 1:
            return None
        return (coords[0], coords[1], alloc.width, alloc.height)

    def _fly_bubble(self, label, text, start, alpha):
        """Lift the message out of the input bar and settle it into its bubble.

        The real bubble's label stays invisible underneath while a copy is
        drawn over the card, morphing from the bar's rectangle to the label's.
        The target is re-read every frame, so it still lands if the chat
        scrolls or the card resizes on the way (the first send does both).
        """
        if self._fly:
            self._end_flight()
        label.set_opacity(0.0)
        self._fly = {"label": label, "text": text, "start": start,
                     "began": time.monotonic(), "alpha": alpha, "id": None}

        def tick():
            fly = self._fly
            if fly is None:
                return GLib.SOURCE_REMOVE
            if time.monotonic() - fly["began"] >= FLY_DURATION:
                self._end_flight()
                return GLib.SOURCE_REMOVE
            self.card.queue_draw()
            return GLib.SOURCE_CONTINUE

        self._fly["id"] = GLib.timeout_add(ANIM_MS, tick)

    def _end_flight(self):
        fly, self._fly = self._fly, None
        if fly is None:
            return
        if fly["id"]:
            GLib.source_remove(fly["id"])
        fly["label"].set_opacity(1.0)
        self.card.queue_draw()

    def _draw_flight(self, widget, ctx):
        fly = self._fly
        if fly is None:
            return False
        label = fly["label"]
        coords = label.translate_coordinates(self.card, 0, 0) if label.get_mapped() else None
        alloc = label.get_allocation()
        if not coords or alloc.width <= 1:
            return False
        end = (coords[0], coords[1], alloc.width, alloc.height)
        k = min(1.0, (time.monotonic() - fly["began"]) / FLY_DURATION)
        e = ease_out_cubic(k)
        x, y, w, h = (lerp(a, b, e) for a, b in zip(fly["start"], end))

        colors = cfg.accent_colors(self.config)
        # Queued messages land faded, like the bubble they become.
        alpha = lerp(1.0, fly["alpha"], e)
        ctx.save()
        rounded_rect(ctx, x, y, w, h, BUBBLE_RADIUS)
        ctx.clip_preserve()
        ctx.set_source_rgba(*hex_to_rgb(colors["dot"]), 0.30 * alpha)
        ctx.fill()

        # The text re-wraps to the shrinking width as it goes, so it reads as
        # the same words settling rather than a picture being squashed.
        layout = label.create_pango_layout(fly["text"])
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_width(int(max(1, w - 30) * Pango.SCALE))
        _, text_h = layout.get_pixel_size()
        ctx.set_source_rgba(*hex_to_rgb(colors["text"]), alpha)
        ctx.move_to(x + 15, y + (h - text_h) / 2)
        PangoCairo.show_layout(ctx, layout)
        ctx.restore()
        return False

    def _add_reply(self, record):
        """Widgets for one saved Ember turn."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for part in record.get("parts") or []:
            self._part_widget(box, part)
        box.show_all()
        self.chat_box.pack_start(box, False, False, 0)
        return box

    def _part_widget(self, box, part):
        kind = part.get("kind")
        if kind == "text":
            widget = Segment(self._on_chat_resize)
            widget.set_raw(part.get("text", ""))
        elif kind == "steps":
            widget = Fold(self._on_chat_resize)
            self._paint_steps(widget, part["items"])
        else:
            widget = _wrap_label(GLib.markup_escape_text(part.get("text", "")), css="ember-note")
        box.pack_start(widget, False, False, 0)
        widget.show_all()
        return widget

    @staticmethod
    def _paint_steps(fold, items):
        n = len(items)
        fold.set_title(f"{n} step{'s' if n != 1 else ''}")
        fold.set_body(_steps_markup(items))

    def _begin_turn(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.show()
        self.chat_box.pack_start(box, False, False, 0)
        self._turn = {"box": box, "record": {"role": "ember", "parts": []}, "widgets": []}

    def _turn_part(self, kind):
        """The turn's current part of `kind`, starting a new one if the last
        part is something else."""
        turn = self._turn
        parts = turn["record"]["parts"]
        if parts and parts[-1]["kind"] == kind:
            return parts[-1], turn["widgets"][-1]
        part = {"kind": kind, "text": ""} if kind != "steps" else {"kind": "steps", "items": []}
        parts.append(part)
        widget = self._part_widget(turn["box"], part)
        turn["widgets"].append(widget)
        return part, widget

    def _turn_text(self, delta):
        part, widget = self._turn_part("text")
        part["text"] += delta
        widget.set_raw(part["text"])
        self._on_chat_resize()

    def _turn_segment(self):
        # A fresh text block after a tool call. If the previous text part is
        # still the last part (no step between), keep the two apart with a
        # paragraph break rather than starting another widget.
        parts = self._turn["record"]["parts"]
        if parts and parts[-1]["kind"] == "text" and parts[-1]["text"].strip():
            self._turn_text("\n\n")

    def _turn_step(self, step):
        part, widget = self._turn_part("steps")
        part["items"].append({k: step.get(k, "") for k in ("name", "description", "command")})
        self._paint_steps(widget, part["items"])
        self._on_chat_resize()

    def _turn_note(self, text):
        if self._turn is None:
            self._begin_turn()
        part = {"kind": "note", "text": text}
        self._turn["record"]["parts"].append(part)
        self._turn["widgets"].append(self._part_widget(self._turn["box"], part))
        self._on_chat_resize()

    def _end_turn(self):
        turn = self._turn
        self._turn = None
        if not turn:
            return
        record = turn["record"]
        # Tidy the final text the same way the runner does, so a trailing
        # "Sources:" list doesn't survive into the saved transcript.
        for part, widget in zip(record["parts"], turn["widgets"]):
            if part["kind"] == "text":
                part["text"] = part["text"].strip()
                widget.set_raw(part["text"])
        if record["parts"]:
            self._messages.append(record)
        self._save_transcript()

    def _on_chat_resize(self):
        if self.state != IDLE and self.view == CHAT:
            self._animate_card(self._card_width(), self._content_height())

    def _on_chat_adj_changed(self, adj):
        if self._stick_bottom:
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def _on_chat_scrolled(self, adj):
        # Follow new text only while the reader is at the bottom; scrolling up
        # to re-read something must not be yanked back down mid-sentence.
        self._stick_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 24

    def _show_chat(self):
        self.view = CHAT
        self._model = self._transcript_model or self._model
        self._paint_model_chip()
        self._set_status("")
        self._set_state(LISTENING)

    # -- model choice ------------------------------------------------------

    def _paint_model_chip(self):
        if hasattr(self, "model_chip"):
            arrow = "▴" if getattr(self, "mood_tray", None) and self.mood_tray.get_visible() else "▾"
            self.model_chip.set_text(f"{cfg.model_label(self._model)}  {arrow}")

    def _set_model(self, alias, announce=True):
        self._model = alias
        self._transcript_model = alias
        self._paint_model_chip()
        for tile in getattr(self, "_mood_tiles", ()):
            tile.picked = tile.alias == alias
            tile.queue_draw()
        if announce and self.state != THINKING:
            blurb = next((m["blurb"] for m in cfg.MODELS if m["alias"] == alias), "")
            self._set_status(f"{cfg.model_label(alias)} — {blurb}")
        if self._messages:
            self._save_transcript()

    def _cycle_model(self):
        aliases = [m["alias"] for m in cfg.MODELS]
        index = aliases.index(self._model) if self._model in aliases else 0
        self._set_model(aliases[(index + 1) % len(aliases)])

    def _show_model_menu(self, event=None):
        self._set_mood_tray(not self.mood_tray.get_visible())

    def _set_mood_tray(self, open_):
        if self._mood_close_id:
            GLib.source_remove(self._mood_close_id)
            self._mood_close_id = None
        if open_ == self.mood_tray.get_visible():
            return
        if open_:
            for tile in self._mood_tiles:
                tile.picked = tile.alias == self._model
                tile.hot = False
            started = [time.monotonic()]

            def tick():
                now = time.monotonic()
                for tile in self._mood_tiles:
                    tile.tick(now - started[0], ANIM_MS / 1000.0)
                return GLib.SOURCE_CONTINUE

            tick()
            self._mood_tick_id = GLib.timeout_add(ANIM_MS, tick)
        elif self._mood_tick_id:
            GLib.source_remove(self._mood_tick_id)
            self._mood_tick_id = None
        self._show_widget(self.mood_tray, open_, True)
        self._paint_model_chip()
        self._relayout()

    def _on_mood_pick(self, tile):
        for other in self._mood_tiles:
            other.picked = other is tile
            other.queue_draw()
        tile.pop()
        if tile.alias != self._model:
            self._set_model(tile.alias)
        # Long enough to see the pop land, short enough not to feel like a wait.
        self._mood_close_id = GLib.timeout_add(380, self._close_mood_tray)

    def _close_mood_tray(self):
        self._mood_close_id = None
        self._set_mood_tray(False)
        return GLib.SOURCE_REMOVE

    def _popup(self, menu, event):
        self._menu_open = True
        menu.connect("deactivate", self._on_menu_closed)
        menu.show_all()
        menu.popup_at_pointer(event)

    # -- local results -----------------------------------------------------

    def _refresh_results(self):
        """Re-resolve locally on every keystroke. This is a pure-python match
        over an in-memory index -- about 2ms -- so there is no debounce and
        nothing leaves the machine."""
        if self.state == IDLE:
            return
        query = self.entry.get_text().strip()

        if query and not query.startswith("/"):
            # Cheap: returns immediately unless a search dir actually changed,
            # so a newly installed app is findable without a restart.
            self._index.refresh()
            hits = launcher.resolve(
                query, self._index, limit=self.config.get("max_results", 5)
            )
        else:
            hits = []

        # Hybrid routing: with nothing matched the card looks exactly as it
        # always did and Enter goes to the model. In the launcher the "Ask
        # Ember" row is the escape hatch at the bottom; in a chat, replying is
        # what Enter should do, so it goes first and the local offers are an
        # arrow-key away rather than hijacking "yes" or "files" mid-thread.
        if hits:
            if self.view == CHAT:
                hits = [launcher.Result("ask", "Reply", query, -1.0, {}, "mail-reply-sender-symbolic")] + hits
            else:
                hits = hits + [launcher.Result(
                    "ask", "Ask Ember", query, -1.0, {}, "system-search-symbolic"
                )]

        self._results = hits
        self._sel = 0
        self._render_rows()
        self._relayout()

    def _clear_rows(self):
        for row in self.results.get_children():
            self.results.remove(row)
        self._rows = []

    def _icon_for(self, result):
        name = result.icon or "application-x-executable"
        if name.startswith("/") and os.path.exists(name):
            # Some entries name an icon file rather than a theme icon.
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(name, 22, 22)
                return Gtk.Image.new_from_pixbuf(pixbuf)
            except GLib.Error:
                name = "application-x-executable"
        image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.LARGE_TOOLBAR)
        image.set_pixel_size(22)
        return image

    def _render_rows(self):
        self._clear_rows()
        for result in self._results:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_border_width(7)
            box.pack_start(self._icon_for(result), False, False, 0)

            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            title = Gtk.Label(xalign=0.0, label=result.title)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.get_style_context().add_class("ember-row-title")
            text.pack_start(title, False, False, 0)
            if result.subtitle:
                sub = Gtk.Label(xalign=0.0, label=result.subtitle)
                sub.set_ellipsize(Pango.EllipsizeMode.END)
                sub.get_style_context().add_class("ember-row-sub")
                text.pack_start(sub, False, False, 0)
            box.pack_start(text, True, True, 0)

            row.add(box)
            self.results.add(row)
            self._rows.append((row, result))

        self.results.show_all()
        self._apply_selection()

    def _apply_selection(self):
        if not self._rows:
            return
        self._sel = max(0, min(self._sel, len(self._rows) - 1))
        row = self._rows[self._sel][0]
        self.results.select_row(row)
        # Keep the highlighted row in view when the list is long enough to
        # scroll; the entry holds focus, so the ListBox won't do this itself.
        adjustment = self._results_scroll.get_vadjustment()
        alloc = row.get_allocation()
        # Straight after show_all() GTK has not laid out yet and reports a 1px
        # allocation; scrolling on that would jump to nonsense.
        if adjustment is not None and alloc.height > 1:
            top, bottom = adjustment.get_value(), adjustment.get_value() + adjustment.get_page_size()
            if alloc.y < top:
                adjustment.set_value(alloc.y)
            elif alloc.y + alloc.height > bottom:
                adjustment.set_value(alloc.y + alloc.height - adjustment.get_page_size())

    def _move_selection(self, delta):
        if not self._rows:
            return False
        self._sel = (self._sel + delta) % len(self._rows)
        self._apply_selection()
        return True

    def _on_row_activated(self, listbox, row):
        for index, (candidate, _) in enumerate(self._rows):
            if candidate is row:
                self._sel = index
                break
        if not self._activate_selection():
            self._ask_model(self.entry.get_text().strip())

    def _activate_selection(self):
        """Run the highlighted offer. Returns False when the caller should fall
        through to the model instead."""
        if not self._rows:
            return False
        result = self._rows[self._sel][1]
        query = self.entry.get_text().strip()

        if result.kind == "ask":
            return False
        if result.kind == "calc":
            # A calculation is already its own answer, so show it rather than
            # collapsing to nothing and leaving the user wondering.
            self.entry.set_text("")
            self._results = []
            self._clear_rows()
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(result.title, -1)
            self._track(query, "calc", "ok", response=result.title,
                        how={"handler": "launcher.calculate"})
            if self.view == CHAT:
                self._set_status(f"= {result.title}  ·  copied")
                self._relayout()
                return True
            self.response_text = result.title
            self.msg.set_text(result.title)
            self._set_status("copied to clipboard")
            self._set_state(RESPONDING)
            self._start_dwell(result.title)
            return True

        # How long the local path actually took, so the report can put a real
        # number against the model latency it would be replacing.
        started = time.monotonic()
        how = {"handler": f"launcher.{result.kind}", "target": result.title,
               "commands": [" ".join(result.payload["argv"])] if result.payload.get("argv") else [],
               "path": result.payload.get("path")}
        try:
            launcher.activate(result)
        except Exception as error:  # noqa: BLE001 - surfaced to the user below
            self.entry.set_text("")
            self._results = []
            self._clear_rows()
            text = f"Couldn't open that: {error}"
            self._track(query, result.kind, "error", response=text,
                        how=how, error=str(error), duration_ms=_ms_since(started))
            if self.view == CHAT:
                self._set_status(text)
                self._relayout()
                return True
            self.response_text = text
            self.msg.set_text(text)
            self._set_state(RESPONDING)
            self._start_dwell(text)
            return True

        self._track(query, result.kind, "ok", response=result.title, how=how,
                    duration_ms=_ms_since(started))

        # Launching is the end of the interaction -- get out of the way. A chat
        # on screen isn't lost; it comes back on the next open.
        self.entry.set_text("")
        self._cancel_dwell()
        self._set_state(IDLE)
        return True

    def _open_input(self, initial_text=""):
        """Reopen where things were left. A recent conversation comes straight
        back; otherwise this is the launcher, with a greeting only when the
        thread is genuinely new -- mid-conversation it would read as if Ember
        had forgotten the last exchange."""
        if self._chat_resumable():
            self._show_chat()
        else:
            self.view = LAUNCHER
            self._model = self.config.get("model", "sonnet")
            self._paint_model_chip()
            fresh = not self.runner.will_resume() or not self._messages
            greeting = cfg.pick_greeting(name=self.config.get("user_name")) if fresh else ""
            trace("greeting:", repr(greeting))
            self.msg.set_text(greeting)
            self.response_text = ""
            if not fresh:
                # The session is still alive, just not recent enough to throw
                # back on screen uninvited. One click brings it back.
                self._set_status("↑  back to the last chat", self._show_chat)
            else:
                self._set_status("")
            self._set_state(LISTENING)
        if initial_text:
            self.entry.set_text(initial_text)
            self.entry.set_position(-1)

    def _update_opacity(self):
        # The surface never appears while idle -- a small cream squircle behind
        # the dot just reads as a blob. Hover feedback lives in the dot instead.
        style = self.card.get_style_context()
        if self.state == IDLE:
            style.add_class("dormant")
        else:
            style.remove_class("dormant")

    # -- card animation ----------------------------------------------------

    def _animate_card(self, target_w, target_h, animate=True):
        if self._anim_id:
            GLib.source_remove(self._anim_id)
            self._anim_id = None

        # From the size last asked for, not the allocation: content that has
        # just grown can make GTK hand the card more than it requested for a
        # frame, and animating back from that read as the card bouncing.
        _, _, start_w, start_h = self._card_rect
        if start_w <= 1 or start_h <= 1:
            alloc = self.card.get_allocation()
            start_w = alloc.width if alloc.width > 1 else target_w
            start_h = alloc.height if alloc.height > 1 else target_h

        start_m = self._morph
        target_m = 0.0 if self.state == IDLE else 1.0

        if not animate or ((start_w, start_h) == (target_w, target_h) and start_m == target_m):
            self._apply_card_size(target_w, target_h)
            self._set_morph(target_m)
            return

        started = time.monotonic()
        duration = MORPH_DURATION if start_m != target_m else ANIM_DURATION

        def tick():
            progress = min(1.0, (time.monotonic() - started) / duration)
            eased = ease_out_cubic(progress)
            w = start_w + (target_w - start_w) * eased
            h = start_h + (target_h - start_h) * eased
            self._apply_card_size(int(w), int(h))
            self._set_morph(lerp(start_m, target_m, eased))
            if progress >= 1.0:
                self._anim_id = None
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        self._anim_id = GLib.timeout_add(ANIM_MS, tick)

    def _apply_card_size(self, w, h):
        self.card.set_size_request(w, h)
        ax, ay = self._anchor_in
        x = max(0, min(ax - w // 2, self.canvas_w - w))
        y = max(0, min(ay - h // 2, self.canvas_h - h))
        self._canvas.move(self.card, x, y)
        self._update_input_region(x, y, w, h)
        # What was asked for, not what GTK allocated: content that is already
        # shown can force the card wider than requested mid-animation, and it
        # grows rightwards from x, so the allocation is no guide to the centre.
        self._card_rect = (x, y, w, h)

    def _update_input_region(self, x, y, w, h):
        """Only the card should swallow clicks; the transparent canvas around
        it stays click-through so desktop icons underneath remain usable."""
        gdk_window = self.get_window()
        if gdk_window is None:
            return
        region = cairo.Region(cairo.RectangleInt(x, y, max(w, 1), max(h, 1)))
        gdk_window.input_shape_combine_region(region, 0, 0)

    # -- interaction -------------------------------------------------------

    def _on_enter(self, *_):
        self.hovered = True
        self.dot.queue_draw()
        self._cancel_dwell()
        return False

    def _on_leave(self, widget, event):
        # Leaving into a child widget is not leaving the card.
        if event.detail == Gdk.NotifyType.INFERIOR:
            return False
        self.hovered = False
        # Let go: the spring carries it back to round with a wobble or two.
        self._jelly_target = (0.0, 0.0)
        self.dot.queue_draw()
        if self.state == RESPONDING:
            self._start_dwell(self.response_text)
        return False

    def _on_focus_out(self, *_):
        trace("focus-out state=", self.state, "raised=", self._raised)
        # The menu takes focus while it is open; ignore that or the card
        # collapses under it.
        if self._menu_open or self.state == IDLE:
            return False
        self._arm_focus_collapse()
        return False

    def _on_focus_in(self, *_):
        self._cancel_focus_collapse()
        self._cancel_fold()
        return False

    def _arm_focus_collapse(self):
        self._cancel_focus_collapse()
        self._focus_collapse_id = GLib.timeout_add(
            FOCUS_OUT_GRACE_MS, self._on_focus_collapse
        )

    def _cancel_focus_collapse(self):
        if self._focus_collapse_id:
            GLib.source_remove(self._focus_collapse_id)
            self._focus_collapse_id = None

    def _on_focus_collapse(self):
        self._focus_collapse_id = None
        # Re-check rather than trusting the event: focus may well have come
        # straight back during the grace window.
        if self.has_toplevel_focus() or self.state == IDLE:
            return GLib.SOURCE_REMOVE
        if self.view == CHAT or self.busy_working:
            # A conversation is not dismissed by clicking somewhere else -- you
            # click away to *do* the thing being discussed. It just steps
            # behind your windows, and Super+Space brings it straight back.
            if self._raised:
                self._raised = False
                self._apply_stacking()
            self._arm_fold()
        else:
            self._cancel_dwell()
            self._set_state(IDLE)
        return GLib.SOURCE_REMOVE

    # An open chat left alone eventually tidies itself back into the dot. It
    # comes back on the next open, so this costs nothing.
    def _arm_fold(self):
        self._cancel_fold()
        minutes = self.config.get("chat_fold_minutes", 15)
        if minutes:
            self._fold_id = GLib.timeout_add_seconds(int(minutes * 60), self._on_fold)

    def _cancel_fold(self):
        if self._fold_id:
            GLib.source_remove(self._fold_id)
            self._fold_id = None

    def _on_fold(self):
        self._fold_id = None
        if self.has_toplevel_focus() or self.state == IDLE:
            return GLib.SOURCE_REMOVE
        if self.busy_working:
            self._arm_fold()
            return GLib.SOURCE_REMOVE
        self._set_state(IDLE)
        return GLib.SOURCE_REMOVE

    def _on_window_state(self, widget, event):
        # Ordinary minimise gestures iconify the window; Ember is meant to stay
        # put on the desktop, so bounce straight back out of it. (GNOME's
        # show-desktop hides at the compositor level and cannot be caught here.)
        if event.new_window_state & Gdk.WindowState.ICONIFIED:
            GLib.idle_add(self.deiconify)
        return False

    def _on_button_press(self, widget, event):
        if event.button == 3:
            self._show_menu(event)
            return True
        if event.button == 1:
            target = Gtk.get_event_widget(event)
            if isinstance(target, Gtk.Label) and target.get_selectable():
                return False  # selecting text to copy, not dragging Ember about
            self._drag_origin = (event.x_root, event.y_root, *self.get_position())
            if self.state == IDLE:
                self._open_input()
        return False

    def _on_motion(self, widget, event):
        self._aim_jelly(widget, event)
        if not self._drag_origin:
            return False
        ox, oy, wx, wy = self._drag_origin
        dx, dy = event.x_root - ox, event.y_root - oy
        if abs(dx) > 2 or abs(dy) > 2:
            self.move(int(wx + dx), int(wy + dy))
        return False

    def _aim_jelly(self, widget, event):
        """Point the ring's pull at the cursor, strongest at the ring's edge."""
        if self.state != IDLE:
            return
        coords = widget.translate_coordinates(self.dot, int(event.x), int(event.y))
        if not coords:
            return
        size = min(self.dot.get_allocated_width(), self.dot.get_allocated_height())
        if size <= 0:
            return
        reach = size / 2 * 0.875
        dx = (coords[0] - self.dot.get_allocated_width() / 2) / reach
        dy = (coords[1] - self.dot.get_allocated_height() / 2) / reach
        length = math.hypot(dx, dy)
        if length > 1:
            dx, dy = dx / length, dy / length
        self._jelly_target = (dx, dy)

    def _on_button_release(self, widget, event):
        if self._drag_origin:
            x, y = self.get_position()
            ax, ay = x + self._anchor_in[0], y + self._anchor_in[1]
            if (ax, ay) != (self.config.get("anchor_x"), self.config.get("anchor_y")):
                self.config["anchor_x"], self.config["anchor_y"] = ax, ay
                cfg.save_config(self.config)
                self._place_window()
            self._drag_origin = None
        return False

    def _on_key(self, widget, event):
        key = Gdk.keyval_name(event.keyval)
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)

        if key == "Escape" and self.mood_tray.get_visible():
            self._set_mood_tray(False)
            return True
        if key == "Escape":
            if self.busy_working and self._force_cancel_id is None:
                # First press during a run: warn, don't kill. See FORCE_CANCEL_MS.
                self._arm_force_cancel()
                return True
            if self.busy_working:
                self._force_stop()
                return True
            if self._rows and self.entry.get_text():
                # First Escape clears a half-typed query; the next one closes.
                self.entry.set_text("")
                return True
            self._cancel_dwell()
            self._set_state(IDLE)
            return True
        if control and key in ("c", "C") and self._copy_selection():
            return True
        if control and key in ("t", "T"):
            self._open_in_terminal()
            return True
        if control and key in ("n", "N"):
            self._new_chat()
            return True
        if control and key in ("m", "M"):
            self._cycle_model()
            return True
        if alt and key in ("1", "2", "3", "4"):
            index = int(key) - 1
            if index < len(cfg.MODELS):
                self._set_model(cfg.MODELS[index]["alias"])
            return True

        if self._rows:
            if key in ("Down", "Tab"):
                return self._move_selection(1)
            if key in ("Up", "ISO_Left_Tab"):
                return self._move_selection(-1)
            if key in ("Return", "KP_Enter") and control:
                # Force the model past a confident local match, without having
                # to arrow down to the Ask row.
                self._ask_model(self.entry.get_text().strip())
                return True

        if key == "Up" and self.view == LAUNCHER and not self.entry.get_text() and self._status_action:
            self._status_action()
            return True

        if key in ("Page_Up", "Page_Down") and self.view == CHAT:
            adj = self._chat_scroll.get_vadjustment()
            step = adj.get_page_size() * 0.85 * (-1 if key == "Page_Up" else 1)
            adj.set_value(adj.get_value() + step)
            return True

        if self.state == IDLE and event.string and event.string.isprintable():
            self._open_input(event.string)
            return True
        return False

    # -- copying -------------------------------------------------------------

    def _selected_text(self):
        """Whatever is selected in the chat. Reply labels are selectable but
        never focusable (the entry has to keep the keyboard), so Ctrl+C goes
        to the entry, not to them, and has to be routed here instead."""
        def walk(widget):
            if isinstance(widget, Gtk.Label):
                if widget.get_selectable():
                    found, start, end = widget.get_selection_bounds()
                    if found and end > start:
                        return widget.get_text()[start:end]
            elif isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    text = walk(child)
                    if text:
                        return text
            return ""
        return walk(self.chat_box)

    def _last_reply_text(self):
        record = self._turn["record"] if self._turn else next(
            (m for m in reversed(self._messages) if m.get("role") == "ember"), None)
        if not record:
            return ""
        texts = [p.get("text", "") for p in record.get("parts") or [] if p.get("kind") == "text"]
        return strip_markdown("\n\n".join(t for t in texts if t.strip()))

    def _to_clipboard(self, text):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()

    def _copy_selection(self):
        """Ctrl+C: the entry's own selection wins; otherwise a reply's."""
        if self.entry.buffer.get_has_selection():
            return False
        text = self._selected_text()
        if text:
            self._to_clipboard(text)
        return bool(text)

    def _show_menu(self, event):
        menu = Gtk.Menu()

        selected = self._selected_text()
        reply = self._last_reply_text()
        copy = Gtk.MenuItem(label="Copy\tCtrl+C" if selected else "Copy last reply")
        copy.set_sensitive(bool(selected or reply))
        copy.connect("activate", lambda *_: self._to_clipboard(selected or reply))
        menu.append(copy)
        menu.append(Gtk.SeparatorMenuItem())

        new = Gtk.MenuItem(label="New chat\tCtrl+N")
        new.set_sensitive(bool(self._messages) and not self.busy_working)
        new.connect("activate", lambda *_: self._new_chat())
        menu.append(new)

        handoff = Gtk.MenuItem(label="Continue in terminal\tCtrl+T")
        handoff.set_sensitive(bool(self.last_session_id or self._transcript_session))
        handoff.connect("activate", lambda *_: self._open_in_terminal())
        menu.append(handoff)

        menu.append(Gtk.SeparatorMenuItem())

        default = Gtk.MenuItem(label="Default for new chats")
        sub = Gtk.Menu()
        for model in cfg.MODELS:
            item = Gtk.CheckMenuItem(label=model["label"])
            item.set_draw_as_radio(True)
            item.set_active(model["alias"] == self.config.get("model"))
            item.connect("activate", self._on_pick_default_model, model["alias"])
            sub.append(item)
        default.set_submenu(sub)
        menu.append(default)

        colour = Gtk.MenuItem(label="Colour")
        sub = Gtk.Menu()
        for name in cfg.ACCENTS:
            item = Gtk.CheckMenuItem(label=name.capitalize())
            item.set_draw_as_radio(True)
            item.set_active(name == self.config.get("accent"))
            item.connect("activate", self._on_pick_accent, name)
            sub.append(item)
        colour.set_submenu(sub)
        menu.append(colour)

        above = Gtk.CheckMenuItem(label="Float above windows")
        above.set_active(bool(self.config.get("keep_above")))
        above.connect("toggled", self._on_toggle_above)
        menu.append(above)

        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        # Quitting takes the subprocess down with it, so it is off the table for
        # the same reason dismissing is.
        quit_item.set_sensitive(not self.busy_working)
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        self._popup(menu, event)

    def _on_menu_closed(self, *_):
        self._menu_open = False
        if self.state != IDLE:
            self.entry.grab_focus()

    def _on_pick_default_model(self, item, alias):
        if not item.get_active() or self.config.get("model") == alias:
            return
        self.config["model"] = alias
        cfg.save_config(self.config)
        if not self._messages:
            self._set_model(alias, announce=False)

    def _on_pick_accent(self, item, name):
        if not item.get_active() or self.config.get("accent") == name:
            return
        self.config["accent"] = name
        cfg.save_config(self.config)
        Gtk.StyleContext.remove_provider_for_screen(self.get_screen(), self._css_provider)
        self._apply_css()
        self.dot.queue_draw()

    def _on_toggle_above(self, item):
        self.config["keep_above"] = item.get_active()
        cfg.save_config(self.config)
        self._apply_stacking()

    def _open_in_terminal(self):
        """Hand the whole conversation to a real terminal, context intact."""
        session = self.last_session_id or self._transcript_session
        if not session:
            return
        command = f"claude --resume {session}"
        terminal = self.config.get("terminal", "gnome-terminal")
        try:
            GLib.spawn_async(
                [terminal, "--working-directory", str(cfg.WORKSPACE), "--", "bash", "-lc", f"{command}; exec bash"],
                flags=GLib.SpawnFlags.SEARCH_PATH,
            )
        except GLib.Error:
            pass
        # The card wasn't enough for this one. Recorded because a request that
        # keeps ending in a terminal is a request the widget is the wrong shape
        # for -- the opposite finding from a scriptable one, and just as useful.
        last = next((m.get("text", "") for m in reversed(self._messages) if m.get("role") == "you"), "")
        self._track(last[:120], "handoff", "ok", session_id=session,
                    how={"handler": "terminal_handoff", "commands": [command]})
        self._cancel_dwell()
        if not self.busy_working:
            self._set_state(IDLE)

    # -- request flow ------------------------------------------------------

    def _on_submit(self, entry):
        prompt = entry.get_text().strip()
        if not prompt:
            return
        # Whatever is highlighted wins. With no local offers there is nothing
        # highlighted, so this falls straight through to the model.
        if self._activate_selection():
            return
        self._ask_model(prompt)

    def _ask_model(self, prompt):
        if not prompt:
            return
        directed, cleaned = resolve_model(prompt)
        if directed:
            self._set_model(directed, announce=not cleaned)
        self._fly_from = self._entry_rect() if cleaned else None
        self.entry.set_text("")
        self._results = []
        self._clear_rows()
        if not cleaned:
            # A bare "/opus": a switch with nothing to ask yet.
            self._relayout()
            return

        if self.busy_working:
            # Typed while a reply is still coming: hold it and send it next,
            # rather than refusing or killing the run in flight.
            self._queue.append(cleaned)
            self._add_bubble(cleaned, queued=True)
            self._paint_activity()
            self._relayout()
            return

        if self.view == LAUNCHER:
            if not self.runner.will_resume():
                # The old session is gone, so its transcript is history.
                self._messages = []
                self._transcript_session = None
                self._clear_chat()
            self.view = CHAT
            if not self._chat_built:
                self._rebuild_chat()
        self._send(cleaned)

    def _send(self, prompt, bubble=None):
        if bubble is not None:
            bubble.set_opacity(1.0)
        else:
            self._add_bubble(prompt)
        self._messages.append({"role": "you", "text": prompt})
        self._save_transcript()

        self._activity = ""
        self._story_line = ""
        self._set_status("")
        self._begin_turn()
        # Anything still waiting belongs after this reply, not above it.
        for waiting in self._queued_bubbles():
            self.chat_box.reorder_child(waiting, -1)
        if self.state != IDLE:
            self._set_state(THINKING)
        self._pending_turn = {"prompt": prompt, "started": time.monotonic(), "model": self._model}
        self._generation += 1
        generation = self._generation
        model = self._model

        def work():
            self.runner.run(prompt, lambda event: self._emit(event, generation), model=model)
            GLib.idle_add(self._on_run_finished)

        threading.Thread(target=work, daemon=True).start()

    def _on_run_finished(self):
        """The worker has fully let go of the runner, so a queued follow-up can
        go now. Driven from here rather than from `done`, because a stopped run
        never delivers one."""
        if self._queue and not self.runner.busy and self.state != THINKING:
            prompt = self._queue.pop(0)
            bubble = self._queued_bubble()
            self._send(prompt, bubble)
        return GLib.SOURCE_REMOVE

    def _queued_bubbles(self):
        return [c for c in self.chat_box.get_children() if c.get_opacity() < 1.0]

    def _queued_bubble(self):
        """The oldest still-faded bubble, which is the one about to be sent."""
        queued = self._queued_bubbles()
        return queued[0] if queued else None

    def _emit(self, event, generation):
        """Called from the worker thread; hop back to the GTK main loop."""
        GLib.idle_add(self._handle_event, event, generation)

    def _handle_event(self, event, generation):
        if generation != self._generation:
            return GLib.SOURCE_REMOVE  # a stopped run's last words
        kind = event["type"]

        if kind == "init":
            self.last_session_id = event.get("session_id")
            self._transcript_session = self.last_session_id
            self._pending_model = event.get("model", self._model)

        elif kind == "segment":
            if self._turn is not None:
                self._turn_segment()

        elif kind == "text":
            if self._turn is not None:
                self._turn_text(event["delta"])

        elif kind == "tool":
            if self.state == THINKING:
                self._activity = TOOL_ACTIVITY.get(event.get("name"), "working")
                self._story_line = ""
                self._paint_activity()

        elif kind == "step":
            if event.get("name") == "ToolSearch":
                pass  # plumbing, not a step anyone took
            elif self._turn is not None:
                self._turn_step(event)
                self._activity = _activity_from(event)
                self._tell_story(event)
                self._paint_activity()

        elif kind == "rate_limit":
            info = event.get("info", {})
            self.rate_limited = info.get("status") not in ("allowed", None)

        elif kind == "denied":
            pass  # The model explains it in plain language; no extra chrome.

        elif kind == "done":
            text = (event.get("text") or "").strip()
            if self._turn is not None and not any(
                    p["kind"] == "text" and p["text"].strip() for p in self._turn["record"]["parts"]):
                # Nothing streamed. After steps, "Done." is honest; with no
                # steps at all, an empty reply is a fault and must not be
                # dressed up as success.
                did_work = any(p["kind"] == "steps" for p in self._turn["record"]["parts"])
                if text or did_work:
                    self._turn_text(text or "Done.")
                else:
                    self._turn_note("No reply came back — try asking again.")
            self.response_text = text
            self._end_turn()
            # Folded mid-run (say, by launching an app from the list): the
            # reply is saved and waits for the next open rather than popping
            # the card back up uninvited.
            if self.state != IDLE:
                self._set_state(LISTENING)
            self._finish_footer()
            how = event.get("how") or {}
            # A turn that answered but had tool calls blocked is not a clean
            # success; the report should be able to tell those apart.
            outcome = "denied" if how.get("denied") else "ok"
            self._maybe_notify(text)
            self._close_turn(outcome, response=text, event=event)

        elif kind == "error":
            message = event.get("message", "Something went wrong.")
            self._turn_note(strip_markdown(message))
            self._end_turn()
            if self.state != IDLE:
                self._set_state(LISTENING)
            self._close_turn("error", response=message, event=event, error=message)

        return GLib.SOURCE_REMOVE

    def _finish_footer(self):
        parts = []
        if self.rate_limited:
            parts.append("quota running low")
        self._set_status("  ·  ".join(parts))

    def _maybe_notify(self, text):
        """A long job that finishes while you're elsewhere says so."""
        turn = self._pending_turn
        if not turn or self.has_toplevel_focus():
            return
        if _ms_since(turn["started"]) < self.config.get("notify_after_seconds", 15) * 1000:
            return
        body = strip_markdown(text).replace("\n", " ")
        if len(body) > 180:
            body = body[:178].rstrip() + "…"
        try:
            GLib.spawn_async(["notify-send", "-a", "Ember", "Ember", body or "Done."],
                             flags=GLib.SpawnFlags.SEARCH_PATH)
        except GLib.Error:
            pass

    # -- usage tracking ----------------------------------------------------
    #
    # Every interaction is appended to ~/.config/ember/history.jsonl so that
    # `python3 tracker.py report` can say which requests recur often enough, and
    # run consistently enough, to deserve a local handler instead of a model
    # call. See tracker.py.

    def _track(self, prompt, route, outcome, **fields):
        if not self.config.get("track", True):
            return
        tracker.log(dict({"prompt": prompt, "route": route, "outcome": outcome}, **fields))

    def _close_turn(self, outcome, response="", event=None, error=None):
        """Write the record for the in-flight model turn, exactly once.

        Popping the slot first is what guarantees the once: `done` and `error`
        can both arrive for a single run (a cancelled run emits an error after
        the user has already been recorded as cancelling), and a duplicate would
        double-count the very thing the report is trying to measure.
        """
        turn = self._pending_turn
        self._pending_turn = None
        if not turn:
            return
        event = event or {}
        # The CLI's own duration excludes process startup, so prefer the wall
        # clock the user actually waited through and fall back to the CLI's.
        duration_ms = _ms_since(turn["started"]) or event.get("duration_ms")
        self._track(
            turn["prompt"], "model", outcome,
            response=response,
            duration_ms=duration_ms,
            ttft_ms=event.get("ttft_ms"),
            model=self._pending_model or turn.get("model"),
            how=event.get("how") or {},
            cost_usd=event.get("cost_usd"),
            num_turns=event.get("num_turns"),
            session_id=event.get("session_id") or self.last_session_id,
            error=error,
        )

    # -- dwell (launcher-only local results) -------------------------------

    def _dwell_seconds(self, text):
        c = self.config
        return min(
            c["dwell_max_seconds"],
            c["dwell_base_seconds"] + len(text or "") * c["dwell_per_char_seconds"],
        )

    def _start_dwell(self, text):
        self._cancel_dwell()
        if self.hovered:
            return  # Reading; don't snatch it away.
        self._dwell_id = GLib.timeout_add(int(self._dwell_seconds(text) * 1000), self._on_dwell_done)

    def _cancel_dwell(self):
        if self._dwell_id:
            GLib.source_remove(self._dwell_id)
            self._dwell_id = None

    def _on_dwell_done(self):
        self._dwell_id = None
        if not self.hovered and self.state == RESPONDING:
            self._set_state(IDLE)
        return GLib.SOURCE_REMOVE

    # An empty launcher left open is the other way it used to get stuck:
    # nothing was ever scheduled to close it, so it sat open until Escape. A
    # chat is exempt -- reading a long answer is not inactivity.
    def _arm_idle_timeout(self):
        self._cancel_idle_timeout()
        if self.view == CHAT:
            return
        self._idle_timeout_id = GLib.timeout_add_seconds(
            self.config.get("listen_timeout_seconds", 20), self._on_idle_timeout
        )

    def _cancel_idle_timeout(self):
        if self._idle_timeout_id:
            GLib.source_remove(self._idle_timeout_id)
            self._idle_timeout_id = None

    def _on_idle_timeout(self):
        self._idle_timeout_id = None
        if (self.state == LISTENING and self.view == LAUNCHER
                and not self.entry.get_text().strip() and not self.runner.busy):
            self._set_state(IDLE)
        return GLib.SOURCE_REMOVE

    # -- summon ------------------------------------------------------------

    def summon(self):
        """Bring Ember up over whatever is on screen.

        At rest it lives *below* the working windows, which is right for an
        ambient widget and wrong for a hotkey command centre -- so the stacking
        flips for exactly as long as it is open.
        """
        self._raised = True
        self._cancel_focus_collapse()
        self._cancel_fold()
        self._apply_stacking()
        self.deiconify()
        self._take_focus()
        if self.state == IDLE:
            self._open_input()
        else:
            self._arm_idle_timeout()
        self.entry.grab_focus()
        return GLib.SOURCE_REMOVE

    def _take_focus(self):
        """present() alone carries no timestamp, and mutter's focus-stealing
        prevention then raises the window without giving it the keyboard --
        reliably so when the desktop itself held focus last. A hotkey is as
        deliberate as input gets, so claim focus with the server's current
        time instead."""
        self.present()
        gdk_window = self.get_window()
        if gdk_window is not None:
            try:
                gdk_window.focus(GdkX11.x11_get_server_time(gdk_window))
            except (TypeError, AttributeError):
                pass

    def dismiss(self):
        self._cancel_focus_collapse()
        if self.busy_working:
            # Closing mid-run once left a machine with brave-browser removed and
            # never reinstalled: the model had run the remove and was killed
            # before the install. Nothing that closes the card may end a run --
            # only a deliberate double Escape can. A chat can step behind the
            # windows while it works, though.
            if self._raised and self.has_toplevel_focus():
                self._raised = False
                self._apply_stacking()
                self._arm_fold()
                return GLib.SOURCE_REMOVE
            self.present()
            self.entry.grab_focus()
            self._arm_force_cancel()
            return GLib.SOURCE_REMOVE
        self._cancel_dwell()
        self._set_state(IDLE)  # also clears the raise
        return GLib.SOURCE_REMOVE

    def toggle(self):
        trace("toggle arrives, state=", self.state, "raised=", self._raised,
              "pending_collapse=", self._focus_collapse_id is not None)
        # A collapse still pending means Ember is in front as far as the user
        # is concerned -- that focus-out was the hotkey's own grab, not a click
        # away -- so this press is a dismiss. A chat that has stepped behind
        # the windows is open but out of sight, so the hotkey brings it back.
        in_front = self.state != IDLE and (
            self._focus_collapse_id is not None or self.has_toplevel_focus() or self._raised)
        self._cancel_focus_collapse()
        return self.dismiss() if in_front else self.summon()

    def on_ipc(self, message):
        """Called on the IPC worker thread; hop to the GTK loop before touching
        a single widget."""
        GLib.idle_add(self._handle_ipc, message)

    def _handle_ipc(self, message):
        {
            "toggle": self.toggle,
            "show": self.summon,
            "hide": self.dismiss,
            "quit": Gtk.main_quit,
        }.get(message, lambda: None)()
        return GLib.SOURCE_REMOVE


def main():
    argv = sys.argv[1:]
    wants_open = "--open" in argv or "--toggle" in argv

    # Hand off to a running instance rather than starting a second widget.
    if ipc.send("toggle" if wants_open else "ping"):
        return 0

    widget = Ember()
    widget.show_all()
    widget._set_state(IDLE, animate=False)

    if ipc.serve(widget.on_ipc) is None:
        print("Ember is already running.", file=sys.stderr)
        return 1
    atexit.register(ipc.cleanup)

    if wants_open:
        GLib.idle_add(widget.summon)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
