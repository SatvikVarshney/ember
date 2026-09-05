#!/usr/bin/env python3
"""Ember -- a warm, ambient assistant widget for the GNOME/X11 desktop.

GTK3 rather than GTK4 on purpose: GTK4 dropped set_keep_below(),
set_type_hint() and set_skip_taskbar_hint(), which are exactly the X11 hints
needed to sit on the desktop layer.

The window itself never resizes. It is a fixed transparent canvas and the card
inside it animates, which avoids janky window-manager resizes; an input shape
region keeps clicks on the transparent area falling through to the desktop.
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
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

import cairo  # noqa: E402

import config as cfg  # noqa: E402
import ipc  # noqa: E402
import launcher  # noqa: E402
from runner import EmberRunner, strip_markdown  # noqa: E402

IDLE, LISTENING, THINKING, RESPONDING, ERROR = "idle", "listening", "thinking", "responding", "error"

DEBUG = bool(os.environ.get("EMBER_DEBUG"))


def trace(*parts):
    if DEBUG:
        print(f"[{time.monotonic():9.3f}]", *parts, file=sys.stderr, flush=True)

# The canvas is the fixed transparent window the card grows inside. It must be
# comfortably taller than the tallest card, because _apply_card_size centres the
# card -- anything larger than the canvas gets clipped at top and bottom.
CANVAS_W, CANVAS_H = 620, 560
MAX_CARD_H = 460
# Where the resting dot sits, measured up from the bottom of the work area.
DOT_ABOVE_BOTTOM = 150
ANIM_MS = 16
ANIM_DURATION = 0.34

# Ceiling for the results list. Left deliberately short of MAX_CARD_H so the
# entry and footer always have room -- the list scrolls rather than pushing
# them out of the card.
MAX_RESULTS_H = 260

# Focus-out must not collapse the card instantly. Pressing the hotkey makes
# gnome-shell take focus for its own key grab, and that fires focus-out about
# 30ms BEFORE the toggle message arrives on the socket. Collapsing straight
# away meant the toggle then found an idle widget and re-summoned it, so the
# hotkey could open Ember but never close it. Deferring a few frames lets the
# toggle land first and be read as the dismiss it is.
FOCUS_OUT_GRACE_MS = 140


def ease_out_cubic(t):
    return 1 - pow(1 - t, 3)


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


class Ember(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = cfg.load_config()
        self.runner = EmberRunner(self.config)

        self.state = IDLE
        self.hovered = False
        self.response_text = ""
        self.last_session_id = None
        self.rate_limited = False

        self._anim_id = None
        self._dwell_id = None
        self._idle_timeout_id = None
        self._menu_open = False
        self._pulse_phase = 0.0
        self._drag_origin = None
        self._pending_model = self.config.get("model", "haiku")

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

        self._build_window()
        self._build_ui()
        self._apply_css()

        # First scan reads ~190 files; off the UI thread so startup stays snappy.
        threading.Thread(target=self._index.refresh, daemon=True).start()

        GLib.timeout_add(ANIM_MS, self._on_pulse_tick)

    # -- window plumbing ---------------------------------------------------

    def _build_window(self):
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(True)
        self.set_app_paintable(True)
        self.set_default_size(CANVAS_W, CANVAS_H)
        self.set_size_request(CANVAS_W, CANVAS_H)
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
        x, y = self.config.get("x"), self.config.get("y")
        if x is None or y is None:
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            area = monitor.get_workarea()
            x = area.x + (area.width - CANVAS_W) // 2
            # Position by where the *dot* should land, not the canvas edge: the
            # card is centred in the canvas, so the canvas centre is the anchor.
            # Sizing the canvas by the tallest card would otherwise drag the
            # resting dot far up the screen.
            y = area.y + area.height - DOT_ABOVE_BOTTOM - CANVAS_H // 2
        self.move(x, y)

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
        canvas.put(self.card, 0, 0)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
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

        # One label carries both the opening greeting and the reply, so the
        # greeting reads as something said rather than as placeholder chrome.
        self.msg = Gtk.Label(xalign=0.0, yalign=0.0)
        self.msg.set_line_wrap(True)
        self.msg.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.msg.set_max_width_chars(42)
        self.msg.get_style_context().add_class("ember-text")

        # Grows with the text up to a ceiling, then scrolls rather than
        # overflowing the card and being clipped.
        self._msg_scroll = Gtk.ScrolledWindow()
        self._msg_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._msg_scroll.set_propagate_natural_height(True)
        self._msg_scroll.set_max_content_height(MAX_CARD_H - 110)
        self._msg_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self._msg_scroll.add(self.msg)
        inner.pack_start(self._msg_scroll, True, True, 0)

        self.entry = Gtk.Entry()
        self.entry.set_has_frame(False)
        self.entry.get_style_context().add_class("ember-input")
        self.entry.connect("activate", self._on_submit)
        self.entry.connect("changed", self._on_typing)
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

        self.footer = Gtk.Label(xalign=0.0)
        self.footer.get_style_context().add_class("ember-footer")
        inner.pack_start(self.footer, False, False, 0)

    def _apply_css(self):
        colors = cfg.accent_colors(self.config)
        surface = self.config["surface"]
        font = self.config["font_family"]
        size = self.config["font_size"]
        css = f"""
        window {{ background-color: transparent; }}
        .ember-card {{
            background-color: {surface};
            border-radius: 26px;
        }}
        /* At rest the surface disappears entirely and only the dot remains.
           Fading the whole card instead just turns the cream muddy grey. */
        .ember-card.dormant {{
            background-color: alpha({surface}, 0.0);
        }}
        .ember-input {{
            background: transparent;
            border: none;
            box-shadow: none;
            color: {colors['text']};
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {size}px;
            caret-color: {colors['text']};
        }}
        .ember-input placeholder {{ color: alpha({colors['text']}, 0.35); }}
        .ember-card scrolledwindow,
        .ember-card viewport {{ background-color: transparent; }}
        .ember-card scrollbar {{ background-color: transparent; border: none; }}
        .ember-card scrollbar slider {{
            background-color: alpha({colors['text']}, 0.25);
            border: none;
            min-width: 5px;
        }}
        .ember-text {{
            color: {colors['text']};
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {size}px;
        }}
        .ember-footer {{
            color: alpha({colors['text']}, 0.45);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(11, size - 6)}px;
        }}
        /* Rows have to sit inside the cream surface, not on top of it, so the
           list itself stays transparent and only the selection is painted. */
        .ember-results, .ember-results row {{
            background-color: transparent;
            border: none;
        }}
        .ember-results row {{ border-radius: 14px; }}
        .ember-results row:selected {{
            background-color: alpha({colors['dot']}, 0.38);
        }}
        .ember-results row:hover {{
            background-color: alpha({colors['dot']}, 0.18);
        }}
        .ember-row-title {{
            color: {colors['text']};
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(13, size - 3)}px;
        }}
        .ember-row-sub {{
            color: alpha({colors['text']}, 0.5);
            font-family: "{font}", "Cantarell", sans-serif;
            font-size: {max(10, size - 7)}px;
        }}
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

        if self.state in (IDLE, THINKING):
            speed = 2.6 if self.state == IDLE else 0.85
            phase = (math.sin(self._pulse_phase * (2 * math.pi) / speed) + 1) / 2
            scale = 0.78 + 0.22 * phase
            alpha = 0.6 + 0.4 * phase
            if self.hovered:
                # Only hover affordance while idle, since the card stays hidden.
                scale, alpha = 1.0, 1.0
        else:
            scale, alpha = 1.0, 1.0

        # 0.5 keeps the outer ring (1.75x) inside the allocation; anything
        # larger clips the ring against the widget edge.
        core = (min(w, h) / 2) * 0.5 * scale

        # Soft outer ring so it reads as a deliberate object on the wallpaper
        # rather than a stray speck, without resorting to a gradient.
        ctx.set_source_rgba(r, g, b, alpha * 0.25)
        ctx.arc(cx, cy, core * 1.75, 0, 2 * math.pi)
        ctx.fill()

        ctx.set_source_rgba(r, g, b, alpha)
        ctx.arc(cx, cy, core, 0, 2 * math.pi)
        ctx.fill()
        return False

    def _on_pulse_tick(self):
        if self.state in (IDLE, THINKING):
            self._pulse_phase += ANIM_MS / 1000.0
            self.dot.queue_draw()
        return GLib.SOURCE_CONTINUE

    # -- state machine -----------------------------------------------------

    def _target_geometry(self):
        c = self.config
        if self.state == IDLE:
            return c["idle_width"], c["idle_height"]
        if self.state == THINKING:
            return c["active_width"], c["response_height"]
        # Height follows content so the input card isn't padded with dead space.
        return c["active_width"], self._content_height()

    def _content_height(self):
        # _inner carries its own border width, so its natural height already
        # includes the padding -- adding more here double-counts it and leaves
        # a dead gap under the text.
        extra = 40  # _inner border top+bottom
        body = 0

        if self._msg_scroll.get_visible():
            # Height must be computed *for the known width*: a wrapping label
            # asked for its plain preferred height reports almost nothing,
            # which left the card stuck at its minimum no matter how long the
            # reply was.
            width = self.config["active_width"] - 72
            self.msg.set_size_request(width, -1)
            _, text_h = self.msg.get_preferred_height_for_width(width)
            visible_h = min(text_h, MAX_CARD_H - 110)
            self._msg_scroll.set_size_request(-1, visible_h)
            body += visible_h

        if self._results_scroll.get_visible():
            _, rows_h = self.results.get_preferred_height()
            rows_h = min(rows_h, MAX_RESULTS_H)
            self._results_scroll.set_size_request(-1, rows_h)
            body += rows_h + 6

        if self.entry.get_visible():
            extra += self.entry.get_preferred_height()[1] + 6
        if self.footer.get_visible():
            extra += self.footer.get_preferred_height()[1] + 6
        return max(72, min(body + extra, MAX_CARD_H))

    def _body_visibility(self):
        """The greeting and the results list are mutually exclusive.

        While the list is up the greeting would only push the rows further from
        the input for no benefit, and the rows are the thing being read.
        """
        listing = self.state == LISTENING and bool(self._results)
        showing_msg = self.state in (LISTENING, RESPONDING, ERROR) and not listing
        return showing_msg, listing

    @staticmethod
    def _show_widget(widget, visible, deep=False):
        widget.set_no_show_all(not visible)
        widget.set_visible(visible)
        if visible:
            widget.show_all() if deep else widget.show()

    def _set_state(self, state, animate=True):
        self.state = state
        # The input stays put after a reply so a follow-up is just typing --
        # the conversation carries on in the same session rather than each
        # exchange being a one-shot.
        if state != LISTENING:
            # Offers belong to the query that produced them; carrying them into
            # a reply would leave stale rows under the answer.
            self._results = []
            self._clear_rows()

        showing_input = state in (LISTENING, RESPONDING, ERROR)
        showing_footer = state in (RESPONDING, ERROR)
        showing_dot = state in (IDLE, THINKING)
        showing_msg, showing_results = self._body_visibility()

        for widget, visible, deep in (
            (self.dot, showing_dot, False),
            (self._msg_scroll, showing_msg, True),
            (self.entry, showing_input, False),
            (self._results_scroll, showing_results, True),
            (self.footer, showing_footer, False),
        ):
            self._show_widget(widget, visible, deep)

        self._inner.set_border_width(0 if state == IDLE else 20)
        self._update_opacity()

        # Any route back to rest also drops the hotkey raise, so Ember can
        # never get stranded above the working windows.
        if state == IDLE and self._raised:
            self._raised = False
            self._apply_stacking()

        if showing_input:
            self.entry.grab_focus()
            self._arm_idle_timeout()
        else:
            self._cancel_idle_timeout()

        w, h = self._target_geometry()
        self._animate_card(w, h, animate)

    def _on_typing(self, *_):
        # Starting a follow-up must not be cut off by the collapse timer that
        # was scheduled when the previous answer landed.
        self._cancel_dwell()
        self._cancel_focus_collapse()
        self._arm_idle_timeout()
        self._refresh_results()

    # -- local results -----------------------------------------------------

    def _refresh_results(self):
        """Re-resolve locally on every keystroke. This is a pure-python match
        over an in-memory index -- about 2ms -- so there is no debounce and
        nothing leaves the machine."""
        if self.state not in (LISTENING, RESPONDING, ERROR):
            return
        query = self.entry.get_text().strip()

        if query:
            # Cheap: returns immediately unless a search dir actually changed,
            # so a newly installed app is findable without a restart.
            self._index.refresh()
            hits = launcher.resolve(
                query, self._index, limit=self.config.get("max_results", 5)
            )
        else:
            hits = []

        # Hybrid routing: with nothing matched the card looks exactly as it
        # always did and Enter goes to the model. The "Ask Ember" row is only
        # added once there *are* offers, as the escape hatch that stops a good
        # local match from trapping a question.
        if hits:
            hits = hits + [launcher.Result(
                "ask", "Ask Ember", query, -1.0, {}, "system-search-symbolic"
            )]

        self._results = hits
        self._sel = 0
        self._render_rows()

        showing_msg, showing_results = self._body_visibility()
        self._show_widget(self._msg_scroll, showing_msg, True)
        self._show_widget(self._results_scroll, showing_results, True)
        self._animate_card(self.config["active_width"], self._content_height())

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
        self._activate_selection()

    def _activate_selection(self):
        """Run the highlighted offer. Returns False when the caller should fall
        through to the model instead."""
        if not self._rows:
            return False
        result = self._rows[self._sel][1]

        if result.kind == "ask":
            return False
        if result.kind == "calc":
            # A calculation is already its own answer, so show it rather than
            # collapsing to nothing and leaving the user wondering.
            self.entry.set_text("")
            self._results = []
            self._clear_rows()
            self.response_text = result.title
            self.msg.set_text(result.title)
            self.footer.set_text("copied to clipboard")
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(result.title, -1)
            self._set_state(RESPONDING)
            self._start_dwell(result.title)
            return True

        try:
            launcher.activate(result)
        except Exception as error:  # noqa: BLE001 - surfaced to the user below
            self.entry.set_text("")
            self._results = []
            self._clear_rows()
            self.response_text = f"Couldn't open that: {error}"
            self.msg.set_text(self.response_text)
            self._set_state(ERROR)
            self._start_dwell(self.response_text)
            return True

        # Launching is the end of the interaction -- get out of the way.
        self.entry.set_text("")
        self._cancel_dwell()
        self._set_state(IDLE)
        return True

    def _open_input(self, initial_text=""):
        """Opens with a greeting already in place, so it reads as a
        conversation that has started rather than a blank field. A greeting is
        only right when the thread is actually new -- mid-conversation it would
        read as if it had forgotten the last exchange."""
        # Greet on a genuinely new thread: either this process hasn't spoken yet
        # (the on-disk session file persists across restarts, so trusting it
        # alone silently suppressed the greeting for up to 30 minutes after a
        # restart), or the previous session has aged out and won't be resumed.
        fresh = self.last_session_id is None or not self.runner._should_resume(cfg.load_state())
        greeting = cfg.pick_greeting(name=self.config.get("user_name")) if fresh else ""
        trace("greeting:", repr(greeting))
        self.msg.set_text(greeting)
        self.footer.set_text("")
        self.response_text = ""
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

        alloc = self.card.get_allocation()
        start_w = alloc.width or target_w
        start_h = alloc.height or target_h

        if not animate:
            self._apply_card_size(target_w, target_h)
            return

        started = time.monotonic()

        def tick():
            progress = min(1.0, (time.monotonic() - started) / ANIM_DURATION)
            eased = ease_out_cubic(progress)
            w = start_w + (target_w - start_w) * eased
            h = start_h + (target_h - start_h) * eased
            self._apply_card_size(int(w), int(h))
            if progress >= 1.0:
                self._anim_id = None
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        self._anim_id = GLib.timeout_add(ANIM_MS, tick)

    def _apply_card_size(self, w, h):
        self.card.set_size_request(w, h)
        x = (CANVAS_W - w) // 2
        y = (CANVAS_H - h) // 2
        self._canvas.move(self.card, x, y)
        self._update_input_region(x, y, w, h)

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
        self._update_opacity()
        self.dot.queue_draw()
        self._cancel_dwell()
        return False

    def _on_leave(self, *_):
        self.hovered = False
        self._update_opacity()
        self.dot.queue_draw()
        if self.state in (RESPONDING, ERROR):
            self._start_dwell(self.response_text)
        return False

    def _on_focus_out(self, *_):
        # Clicking away should put it back to sleep. The menu takes focus while
        # it is open, so ignore that case or the widget collapses under it.
        trace("focus-out state=", self.state, "raised=", self._raised)
        if self._menu_open or self.runner.busy:
            return False
        if self.state in (LISTENING, RESPONDING, ERROR):
            self._arm_focus_collapse()
        return False

    def _on_focus_in(self, *_):
        self._cancel_focus_collapse()
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
        if not self.has_toplevel_focus() and self.state in (LISTENING, RESPONDING, ERROR):
            self._cancel_dwell()
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
            self._drag_origin = (event.x_root, event.y_root, *self.get_position())
            if self.state == IDLE:
                self._open_input()
        return False

    def _on_motion(self, widget, event):
        if not self._drag_origin:
            return False
        ox, oy, wx, wy = self._drag_origin
        dx, dy = event.x_root - ox, event.y_root - oy
        if abs(dx) > 2 or abs(dy) > 2:
            self.move(int(wx + dx), int(wy + dy))
        return False

    def _on_button_release(self, widget, event):
        if self._drag_origin:
            x, y = self.get_position()
            if (x, y) != (self.config.get("x"), self.config.get("y")):
                self.config["x"], self.config["y"] = x, y
                cfg.save_config(self.config)
            self._drag_origin = None
        return False

    def _on_key(self, widget, event):
        key = Gdk.keyval_name(event.keyval)
        control = bool(event.state & Gdk.ModifierType.CONTROL_MASK)

        if key == "Escape":
            self.runner.cancel()
            self._cancel_dwell()
            self._set_state(IDLE)
            return True
        if key in ("t", "T") and control:
            self._open_in_terminal()
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

        if self.state == IDLE and event.string and event.string.isprintable():
            self._open_input(event.string)
            return True
        return False

    def _show_menu(self, event):
        menu = Gtk.Menu()

        for name in cfg.ACCENTS:
            item = Gtk.CheckMenuItem(label=name.capitalize())
            item.set_draw_as_radio(True)
            item.set_active(name == self.config.get("accent"))
            item.connect("activate", self._on_pick_accent, name)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        above = Gtk.CheckMenuItem(label="Float above windows")
        above.set_active(bool(self.config.get("keep_above")))
        above.connect("toggled", self._on_toggle_above)
        menu.append(above)

        handoff = Gtk.MenuItem(label="Continue in terminal")
        handoff.set_sensitive(self.last_session_id is not None)
        handoff.connect("activate", lambda *_: self._open_in_terminal())
        menu.append(handoff)

        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(quit_item)

        self._menu_open = True
        menu.connect("deactivate", self._on_menu_closed)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _on_menu_closed(self, *_):
        self._menu_open = False
        if self.state == LISTENING:
            self.entry.grab_focus()

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
        if not self.last_session_id:
            return
        command = f"claude --resume {self.last_session_id}"
        terminal = self.config.get("terminal", "gnome-terminal")
        try:
            GLib.spawn_async(
                [terminal, "--working-directory", str(cfg.WORKSPACE), "--", "bash", "-lc", f"{command}; exec bash"],
                flags=GLib.SpawnFlags.SEARCH_PATH,
            )
        except GLib.Error:
            pass
        self._cancel_dwell()
        self._set_state(IDLE)

    # -- request flow ------------------------------------------------------

    def _on_submit(self, entry):
        prompt = entry.get_text().strip()
        if not prompt or self.runner.busy:
            return
        # Whatever is highlighted wins. With no local offers there is nothing
        # highlighted, so this falls straight through to the model exactly as
        # it always did.
        if self._activate_selection():
            return
        self._ask_model(prompt)

    def _ask_model(self, prompt):
        if not prompt or self.runner.busy:
            return
        self.entry.set_text("")
        self._results = []
        self._clear_rows()
        self.response_text = ""
        self.msg.set_text("")
        self.footer.set_text("")
        self._set_state(THINKING)

        thread = threading.Thread(target=self.runner.run, args=(prompt, self._emit), daemon=True)
        thread.start()

    def _emit(self, event):
        """Called from the worker thread; hop back to the GTK main loop."""
        GLib.idle_add(self._handle_event, event)

    def _handle_event(self, event):
        kind = event["type"]

        if kind == "init":
            self.last_session_id = event.get("session_id")
            self._pending_model = event.get("model", "haiku")

        elif kind == "tool":
            if self.state == THINKING:
                self.footer.set_text("checking…")

        elif kind == "text":
            if self.state != RESPONDING:
                self._set_state(RESPONDING)
            self.response_text += event["delta"]
            # Stripped as it streams, not just at the end: web search pulls the
            # model into a trailing "Sources:" list, and rendering it raw meant
            # watching a block of links appear and then vanish on completion.
            self.msg.set_text(strip_markdown(self.response_text))
            self._animate_card(self.config["active_width"], self._content_height())

        elif kind == "rate_limit":
            info = event.get("info", {})
            self.rate_limited = info.get("status") not in ("allowed", None)

        elif kind == "denied":
            pass  # The model explains it in plain language; no extra chrome.

        elif kind == "done":
            self.response_text = (event.get("text") or self.response_text).strip()
            self.msg.set_text(self.response_text)
            self._finish_footer()
            if self.state != RESPONDING:
                self._set_state(RESPONDING)
            else:
                self._animate_card(self.config["active_width"], self._content_height())
            self._start_dwell(self.response_text)

        elif kind == "error":
            self.response_text = event.get("message", "Something went wrong.")
            self.msg.set_text(self.response_text)
            self.footer.set_text("")
            self._set_state(ERROR)
            self._start_dwell(self.response_text)

        return GLib.SOURCE_REMOVE

    def _finish_footer(self):
        parts = [self._pending_model]
        if self.rate_limited:
            parts.append("quota low")
        if len(self.response_text) > self.config["handoff_char_threshold"]:
            parts.append("ctrl+t for terminal")
        self.footer.set_text("  ·  ".join(parts))

    # -- dwell -------------------------------------------------------------

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

    # An empty input left open is the other way it used to get stuck: nothing
    # was ever scheduled to close it, so it sat open until Escape.
    def _arm_idle_timeout(self):
        self._cancel_idle_timeout()
        self._idle_timeout_id = GLib.timeout_add_seconds(
            self.config.get("listen_timeout_seconds", 20), self._on_idle_timeout
        )

    def _cancel_idle_timeout(self):
        if self._idle_timeout_id:
            GLib.source_remove(self._idle_timeout_id)
            self._idle_timeout_id = None

    def _on_idle_timeout(self):
        self._idle_timeout_id = None
        if self.state == LISTENING and not self.entry.get_text().strip() and not self.runner.busy:
            self._set_state(IDLE)
        return GLib.SOURCE_REMOVE

    def _on_dwell_done(self):
        self._dwell_id = None
        if not self.hovered:
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
        self._apply_stacking()
        self.deiconify()
        self.present()
        if self.state == IDLE:
            self._open_input()
        else:
            self._arm_idle_timeout()
        self.entry.grab_focus()
        return GLib.SOURCE_REMOVE

    def dismiss(self):
        self._cancel_focus_collapse()
        self.runner.cancel()
        self._cancel_dwell()
        self._set_state(IDLE)  # also clears the raise
        return GLib.SOURCE_REMOVE

    def toggle(self):
        trace("toggle arrives, state=", self.state, "raised=", self._raised,
              "pending_collapse=", self._focus_collapse_id is not None)
        # A collapse still pending means Ember is open as far as the user is
        # concerned -- that focus-out was the hotkey's own grab, not a click
        # away -- so this press is a dismiss.
        open_now = self.state != IDLE or self._focus_collapse_id is not None
        self._cancel_focus_collapse()
        return self.dismiss() if open_now else self.summon()

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
