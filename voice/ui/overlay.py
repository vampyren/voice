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

log = logging.getLogger(__name__)

#: Redraw period in milliseconds, ~30 fps, matching the model's FRAME.
FRAME_MS = 33

#: Set to 1 to stop the breathing dot and the transcribing sweep.
REDUCED_MOTION_ENV = "VOICE_OVERLAY_REDUCED_MOTION"


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
    parser.add_argument("--position", choices=("bottom", "top"), default="bottom")
    parser.add_argument("--margin", type=int, default=48,
                        help="distance from the anchored screen edge (layer-shell only)")
    parser.add_argument("--require-layer-shell", action="store_true",
                        help="exit 2 instead of falling back to a focus-stealing "
                             "plain window when no layer surface is available")
    parser.add_argument("--verbose", action="store_true")
    return parser


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
        shell.init_for_window(self.window)
        shell.set_layer(self.window, shell.Layer.OVERLAY)
        edge = shell.Edge.TOP if self._args.position == "top" else shell.Edge.BOTTOM
        shell.set_anchor(self.window, edge, True)
        shell.set_margin(self.window, edge, self._args.margin)
        shell.set_keyboard_mode(self.window, shell.KeyboardMode.NONE)
        shell.set_exclusive_zone(self.window, -1)
        log.info("overlay: using gtk4-layer-shell on the overlay layer")

    # -- drawing ----------------------------------------------------------

    def _render(self) -> None:
        """Draw one frame with cairo and hand GDK the pixels as a texture."""
        height = self._args.height
        width = self._draw.natural_width(self.model, height)
        surface = self._draw.render_surface(self.model, width, height)
        texture = self._gdk.MemoryTexture.new(
            width, height, self._gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
            self._glib.Bytes.new(bytes(surface.get_data())), surface.get_stride())
        self.view.set_paintable(texture)
        if width != self._width:                # the counter and badge set the width
            self._width = width
            self.view.set_size_request(width, height)

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

    def _log_mapped(self) -> bool:
        """Once per appearance, say whether the compositor really gave us a surface."""
        log.debug("overlay: %s %dx%d mapped=%s surface=%s", self.model.state,
                  self._width, self._args.height, self.window.get_mapped(),
                  self.window.get_surface() is not None)
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
    return _Pill(args, gtk, gdk, glib, layer_shell).run()


if __name__ == "__main__":
    raise SystemExit(main())
