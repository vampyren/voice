"""The recording pill: a helper process the daemon drives over stdin.

One JSON object per line, one line per event:

    {"state": "recording"}          {"level": 0.42}
    {"state": "transcribing"}       {"language": "sv"}
    {"state": "done"}               {"state": "notice", "text": "EN → SV"}
    {"state": "error", "text": "pw-record: no such target"}
    {"state": "hidden"}

Everything above the GTK layer is importable without PyGObject, so the
protocol is unit-tested without a display; `gi` is imported inside `main`.

Running it: the daemon spawns `voice-overlay`, or, where PyGObject is not in
the project's virtualenv (the usual case - it is a system package), the
equivalent `python3 -m voice.ui.overlay` on the system interpreter with
PYTHONPATH pointing at the repository.

Focus, and why it matters to the daemon: only a layer-shell surface can refuse
keyboard focus. GTK 4 removed `set_accept_focus`/`set_focus_on_map`, and a
plain toplevel *is* focused by the compositor when it maps (measured on GNOME:
`is_active()` is True a second after `present()`), which would send the paste
chord to the pill instead of the user's window. gtk4-layer-shell being
installed is not enough - the compositor has to implement zwlr_layer_shell_v1,
which GNOME does not, so `Gtk4LayerShell.is_supported()` decides and a False
there counts as absent. Without a usable layer shell the helper logs a warning;
pass `--require-layer-shell` to make it exit 2 instead, so the daemon can
decide to run without a pill rather than break dictation.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading

from voice.ui.overlay_model import OverlayModel
from voice.ui.placement import (DEFAULT_MARGIN_X, DEFAULT_MARGIN_Y, DEFAULT_POSITION,
                                LEGACY_POSITIONS, POSITIONS, anchors, normalise_position,
                                pill_origin, placement_note)

log = logging.getLogger(__name__)

#: Redraw period in milliseconds, ~30 fps, matching the model's FRAME.
FRAME_MS = 33

#: Set to 1 to stop the breathing dot and the transcribing sweep.
REDUCED_MOTION_ENV = "VOICE_OVERLAY_REDUCED_MOTION"

#: Most of one screen axis a padded window may take. The padding is transparent
#: but, where the input region cannot be set (below), it still takes clicks, so
#: this is deliberately a half and not the whole screen: at 0.5 a `bottom-*`
#: pill sits about a quarter-screen below centre, which is what the placement
#: was asking for, without covering the desktop.
PAD_SCREEN_FRACTION = 0.5

#: Assumed screen when GDK will not say how big the monitor is. Conservative on
#: purpose: too small only means less padding, while too large would put the
#: pill's edge off the screen.
FALLBACK_SCREEN = (1280, 720)


def parse_line(line: str) -> dict | None:
    """One JSON object per line; anything else is logged and dropped."""
    line = line.strip()
    if not line:
        return None
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        log.warning("overlay: ignoring malformed line (%s)", exc)
        return None
    if not isinstance(message, dict):
        log.warning("overlay: ignoring non-object message %r", message)
        return None
    return message


def apply_message(model: OverlayModel, message: dict) -> bool:
    """Apply one decoded message to the model. Returns True if it took effect.

    A bad field never kills the helper: the daemon must be able to keep
    dictating even when it sends us nonsense.
    """
    applied = False
    if "language" in message:
        code = message["language"]
        if isinstance(code, str) and code.strip():
            model.set_language(code)
            applied = True
        else:
            log.warning("overlay: ignoring bad language %r", code)
    if "level" in message:
        try:
            model.push_level(float(message["level"]))
            applied = True
        except (TypeError, ValueError):
            log.warning("overlay: bad level %r", message["level"])
    if "state" in message:
        text = message.get("text")
        try:
            model.set_state(str(message["state"]), text=None if text is None else str(text))
            applied = True
        except ValueError as exc:
            log.warning("overlay: %s", exc)
    return applied


def reduced_motion(gtk_setting: bool | None = None) -> bool:
    """Honour `prefers-reduced-motion`: our env override, then GTK's setting."""
    override = os.environ.get(REDUCED_MOTION_ENV, "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    return gtk_setting is False


def layer_shell_exit_code(shell, require: bool) -> int | None:
    """Exit code when layer-shell is demanded but unusable here, else None."""
    if require and shell is None:
        return 2
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-overlay", description=__doc__)
    parser.add_argument("--lang", default="en", help="language code for the badge")
    parser.add_argument("--height", type=int, default=44, help="pill height in pixels")
    parser.add_argument("--position", choices=POSITIONS + tuple(LEGACY_POSITIONS),
                        default=DEFAULT_POSITION,
                        help="where the pill sits, e.g. bottom-right (layer-shell only)")
    parser.add_argument("--margin-x", type=int, default=DEFAULT_MARGIN_X,
                        help="pixels in from the anchored side, horizontally; a centred "
                             "half has no anchored side and ignores this")
    parser.add_argument("--margin-y", type=int, default=DEFAULT_MARGIN_Y,
                        help="the same, vertically (layer-shell only)")
    parser.add_argument("--pad-to-place", action="store_true",
                        help="where there is no layer shell, pad the window out and draw "
                             "the pill at the edge the placement names; off by default "
                             "because the padding takes clicks meant for what is behind it")
    parser.add_argument("--require-layer-shell", action="store_true",
                        help="exit 2 instead of falling back to a focus-stealing "
                             "plain window when no layer surface is available")
    parser.add_argument("--verbose", action="store_true")
    return parser


def init_layer_shell(shell, window, position: str, margin_x: int, margin_y: int) -> None:
    """Anchor the pill's surface where the placement says, and keep it unfocusable.

    Every edge is stated, anchored or not: an unstated anchor is whatever the
    library defaults to, and the pill's position must not rest on that.
    """
    wanted = anchors(position, margin_x=margin_x, margin_y=margin_y)
    shell.init_for_window(window)
    shell.set_layer(window, shell.Layer.OVERLAY)
    for name in ("top", "bottom", "left", "right"):
        edge = getattr(shell.Edge, name.upper())
        shell.set_anchor(window, edge, name in wanted)
        if name in wanted:
            shell.set_margin(window, edge, wanted[name])
    shell.set_keyboard_mode(window, shell.KeyboardMode.NONE)
    shell.set_exclusive_zone(window, -1)
    log.info("overlay: gtk4-layer-shell on the overlay layer, %s%s",
             normalise_position(position),
             f" with margins {wanted}" if wanted else "")


def warn_about_placement(position: str, margin_x: int, margin_y: int) -> None:
    """Say once that a placement asked for cannot be applied to a plain window.

    GTK 4 has no way to move a toplevel and Wayland gives the compositor the
    last word, so without a layer surface the setting is silently useless -
    which is exactly what an owner who moved the pill must not be left with.
    """
    note = placement_note(position, margin_x, margin_y)
    if note is not None:
        log.warning("%s", note)


def padded_window(position: str, margin_x: int, margin_y: int, screen: tuple[int, int],
                  pill: tuple[int, int],
                  fraction: float = PAD_SCREEN_FRACTION) -> tuple[tuple[int, int],
                                                                  tuple[int, int]]:
    """Window size, and the pill's top-left inside it, for a compositor that centres us.

    A plain Wayland window cannot choose where it appears - but the compositor
    centres the *window*, not the pill drawn inside it. So ask for a window
    bigger than the pill and put the pill against the edge the placement names:
    the window's centre is still the screen's centre, so the pill ends up as far
    off centre as the padding is deep, and the rest of the window is left
    transparent.

    Exact where it can be: a window `2d` bigger than the pill moves it `d` px
    off centre, so the padding a placement needs is however far off centre it
    asks for. `fraction` caps the window at that much of each screen axis;
    beyond the cap the pill rests against the window's edge, as near the
    placement as the cap allows. A centred half (`middle`, `center`) asks for
    nothing and gets a window the size of the pill, exactly as before.
    """
    screen_w, screen_h = max(1, int(screen[0])), max(1, int(screen[1]))
    pill_w, pill_h = int(pill[0]), int(pill[1])
    want_x, want_y = pill_origin(position, margin_x, margin_y,
                                 (screen_w, screen_h), (pill_w, pill_h))
    width, x = _padded_axis(want_x, screen_w, pill_w, fraction)
    height, y = _padded_axis(want_y, screen_h, pill_h, fraction)
    return (width, height), (x, y)


def _padded_axis(want: int, extent: int, size: int, fraction: float) -> tuple[int, int]:
    """One axis of `padded_window`: window length, and the pill's offset in it.

    A window of length `L` is centred at `(extent - L) // 2` - the compositor's
    floor, so this stays in whole pixels - and the pill can then sit anywhere
    from there to `L - size` further on. The smallest `L` that reaches `want` is
    therefore the larger of what the near side needs (`L >= extent - 2*want - 1`)
    and what the far side does (`L >= 2*(want + size) - extent`); the padding is
    not symmetric when the screen's leftover pixel is odd, and it does not need
    to be. Capped at `room`, the pill rests against that end of the window, and
    the clamp is also what keeps it on screen when `want` is off it (a negative
    margin, which only a layer surface can really honour).
    """
    room = max(size, min(extent, int(extent * fraction)))
    length = min(room, max(size, extent - 2 * want - 1, 2 * (want + size) - extent))
    left = (extent - length) // 2
    return length, max(0, min(length - size, want - left))


def monitor_size(gdk, fallback: tuple[int, int] = FALLBACK_SCREEN) -> tuple[int, int]:
    """The first monitor's size in logical pixels, or a conservative guess.

    Only the padding needs this, and being wrong about it is never fatal: it
    decides how far off centre the pill can be pushed, not whether there is one.
    """
    try:
        monitor = gdk.Display.get_default().get_monitors().get_item(0)
        geometry = monitor.get_geometry()
        width, height = int(geometry.width), int(geometry.height)
    except Exception:
        log.debug("overlay: cannot read the monitor geometry", exc_info=True)
        return fallback
    if width <= 0 or height <= 0:
        return fallback
    return width, height


class _Pill:
    """The GTK side: one undecorated window holding one drawing area."""

    def __init__(self, args, gtk, gdk, glib, layer_shell):
        from voice.ui import overlay_draw

        self._draw = overlay_draw
        self._gtk, self._gdk, self._glib = gtk, gdk, glib
        self._args = args
        self._loop = glib.MainLoop()
        self._timer = None
        self._width = 0
        self._geometry: tuple[tuple[int, int], tuple[int, int]] | None = None
        #: Only a plain window needs padding: a layer surface is anchored where
        #: the placement says, and padding it would only move the pill away.
        self._pad = layer_shell is None and bool(getattr(args, "pad_to_place", False))
        self._screen = monitor_size(gdk) if self._pad else None
        #: Set once the region binding has been found unusable, so the helper
        #: says so a single time rather than on every frame.
        self._no_input_region = False
        settings = gtk.Settings.get_default()
        animations = settings.get_property("gtk-enable-animations") if settings else None
        self.model = OverlayModel(lang=args.lang, reduced_motion=reduced_motion(animations))

        self.window = gtk.Window()
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_can_focus(False)
        self.window.set_title("voice overlay")
        self.window.add_css_class("voice-overlay")
        self.view = gtk.Picture()
        self.view.set_can_shrink(False)
        self.view.set_content_fit(gtk.ContentFit.FILL)
        self.window.set_child(self.view)
        self._style(gtk, gdk)
        if layer_shell is not None:
            self._init_layer_shell(layer_shell)

    # -- setup ------------------------------------------------------------

    def _style(self, gtk, gdk) -> None:
        """The pill draws its own background; the window must not draw one."""
        css = gtk.CssProvider()
        css.load_from_data(b"window.voice-overlay, window.voice-overlay > * "
                           b"{ background: none; background-color: transparent; "
                           b"box-shadow: none; }")
        display = gdk.Display.get_default()
        if display is not None:
            gtk.StyleContext.add_provider_for_display(
                display, css, gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _init_layer_shell(self, shell) -> None:
        init_layer_shell(shell, self.window, self._args.position,
                         margin_x=self._args.margin_x, margin_y=self._args.margin_y)

    # -- drawing ----------------------------------------------------------

    def _window_for(self, width: int, height: int) -> tuple[tuple[int, int],
                                                            tuple[int, int]]:
        """The window to ask for, and where the pill goes in it."""
        if not self._pad:
            return (width, height), (0, 0)
        return padded_window(self._args.position, self._args.margin_x,
                             self._args.margin_y, self._screen, (width, height))

    def _render(self) -> None:
        """Draw one frame with cairo and hand GDK the pixels as a texture."""
        height = self._args.height
        width = self._draw.natural_width(self.model, height)
        window, origin = self._window_for(width, height)
        surface = self._draw.render_window_surface(self.model, window, (width, height), origin)
        texture = self._gdk.MemoryTexture.new(
            window[0], window[1], self._gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
            self._glib.Bytes.new(bytes(surface.get_data())), surface.get_stride())
        self.view.set_paintable(texture)
        self._width = width                     # the counter and badge set the width
        if (window, origin) != self._geometry:
            self._geometry = (window, origin)
            self.view.set_size_request(window[0], window[1])
            self._set_input_region()

    def _on_frame(self) -> bool:
        self.model.tick()
        if not self.model.visible:
            self._timer = None
            self.window.set_visible(False)
            return self._glib.SOURCE_REMOVE
        self._render()
        return self._glib.SOURCE_CONTINUE

    def _wake(self) -> None:
        """Show the window and start the frame clock, if they are not already."""
        if not self.model.visible:
            return
        self._render()
        if not self.window.get_visible():
            self.window.set_visible(True)
            self._glib.idle_add(self._log_mapped)
        if self._timer is None:
            self._timer = self._glib.timeout_add(FRAME_MS, self._on_frame)

    def _set_input_region(self) -> None:
        """Keep the padding from taking clicks meant for what is behind it.

        The pill itself is never clickable, so the region it does claim changes
        nothing; everything around it becomes input-transparent. `Gdk.Surface`
        wants a `cairo.Region`, which PyGObject can only convert where its cairo
        foreign-struct support is installed (Debian's `python3-gi-cairo`) - the
        same reason the pill is drawn to a texture rather than into a draw
        handler. Without it the padding takes clicks it does not use, which is
        a trade for the placement, not a reason to fail or to stop drawing.
        """
        if not self._pad or self._no_input_region or self._geometry is None:
            return
        surface = self.window.get_surface()
        if surface is None or not hasattr(surface, "set_input_region"):
            return
        (_, _), (x, y) = self._geometry
        try:
            import cairo

            surface.set_input_region(
                cairo.Region(cairo.RectangleInt(x, y, self._width, self._args.height)))
        except Exception as exc:
            self._no_input_region = True
            log.info("overlay: cannot set the pill's input region (%s); the padding "
                     "around it will take clicks - install python3-gi-cairo, or set "
                     "ui.overlay_pad_to_place = false", exc)

    def _log_mapped(self) -> bool:
        """Once per appearance, say whether the compositor really gave us a surface."""
        self._set_input_region()                # the surface exists only now
        window = self._geometry[0] if self._geometry else (self._width, self._args.height)
        log.debug("overlay: %s %dx%d in a %dx%d window mapped=%s surface=%s",
                  self.model.state, self._width, self._args.height, window[0], window[1],
                  self.window.get_mapped(), self.window.get_surface() is not None)
        return self._glib.SOURCE_REMOVE

    # -- input ------------------------------------------------------------

    def feed(self, line: str) -> bool:
        message = parse_line(line)
        if message and apply_message(self.model, message):
            self._wake()
        return self._glib.SOURCE_REMOVE

    def quit(self) -> bool:
        log.info("overlay: stdin closed, exiting")
        self._loop.quit()
        return self._glib.SOURCE_REMOVE

    def _read_stdin(self) -> None:
        for line in sys.stdin:
            self._glib.idle_add(self.feed, line)
        self._glib.idle_add(self.quit)

    def run(self) -> int:
        self.window.present()
        self.window.set_visible(False)          # nothing to show until told
        threading.Thread(target=self._read_stdin, name="overlay-stdin",
                         daemon=True).start()
        self._loop.run()
        return 0


def _load_gtk():
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, GLib, Gtk

    layer_shell = None
    try:
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as layer_shell
    except (ImportError, ValueError):
        log.info("overlay: gtk4-layer-shell is not available")
    if layer_shell is not None and not _layer_shell_supported(layer_shell):
        # Installed is not usable: the library needs zwlr_layer_shell_v1 from
        # the compositor, which GNOME does not implement. Initialising a window
        # anyway maps a focus-taking toplevel, so this counts as absent.
        log.info("overlay: gtk4-layer-shell is installed but this compositor does not "
                 "support it (no zwlr_layer_shell_v1)")
        layer_shell = None
    return Gtk, Gdk, GLib, layer_shell


def _layer_shell_supported(shell) -> bool:
    """Whether the compositor really offers layer surfaces.

    An older binding without `is_supported` cannot be asked; trust it then,
    rather than turning the pill off on a compositor that would have worked.
    """
    is_supported = getattr(shell, "is_supported", None)
    if is_supported is None:
        return True
    try:
        return bool(is_supported())
    except Exception:
        log.debug("overlay: gtk4-layer-shell is_supported() failed", exc_info=True)
        return True


def _note_the_padding(position: str) -> None:
    """Say what the window is doing instead of being placed - once, at INFO.

    Not the warning above it: with the padding on, the placement is not being
    ignored, it is being approximated, and an owner reading "does nothing here"
    would go looking for a fault in a setting that just worked.
    """
    log.info("overlay: this compositor centres the window itself, so the window is "
             "padded out and the pill drawn at its %s edge - near, not at, the "
             "placement. Set ui.overlay_pad_to_place = false for a pill-sized "
             "window wherever the compositor puts it.", normalise_position(position))


def _warn_about_focus() -> None:
    """Said once, only when we really are about to show a plain window."""
    log.warning(
        "overlay: falling back to a plain window - the compositor places it, and "
        "it WILL take keyboard focus when it appears, so a paste can land in the "
        "pill instead of your window. GTK 4 dropped the accept-focus and "
        "focus-on-map hints, so only a layer-shell surface can refuse focus: "
        "install gtk4-layer-shell, or pass --require-layer-shell to have the "
        "helper exit instead of showing this window.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        gtk, gdk, glib, layer_shell = _load_gtk()
    except (ImportError, ValueError) as exc:
        log.error("overlay: PyGObject with GTK 4 is required (%s)", exc)
        return 1
    refused = layer_shell_exit_code(layer_shell, args.require_layer_shell)
    if refused is not None:
        log.error("overlay: --require-layer-shell was given but no layer surface is "
                  "available (gtk4-layer-shell missing, or unsupported by this "
                  "compositor); not showing a focus-stealing window")
        return refused
    if layer_shell is None:
        _warn_about_focus()
        if args.pad_to_place:
            _note_the_padding(args.position)
        else:
            warn_about_placement(args.position, args.margin_x, args.margin_y)
    return _Pill(args, gtk, gdk, glib, layer_shell).run()


if __name__ == "__main__":
    raise SystemExit(main())
