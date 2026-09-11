"""Settings dialog: edits config.toml through Config so comments survive."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable, Iterable

from html import escape

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPalette, QRegion
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
                               QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
                               QToolButton, QToolTip, QVBoxLayout, QWidget)

from voice import APP_ID
from voice.audio.capture import Source
from voice.config import INJECT_MODES, Config, is_language_code
from voice.hotkey.desktop_shortcuts import (GNOME_KEY_TEMPLATE, ShortcutStoreError,
                                            desktop_shortcut_store)
from voice.hotkey.keyspec import parse_keyspec
from voice.hotkey.portal_listener import DIALOG_MESSAGE, NO_TRIGGER
from voice.ui.pill_placer import PillPlacer
from voice.ui.placement import NO_LAYER_SHELL_NOTE, placement_summary

log = logging.getLogger(__name__)

PROFILE_TEMPLATES: dict[str, dict] = {
    "openai": {"backend": "openai_compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-transcribe", "api_key": "", "prompt": ""},
    "groq": {"backend": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "model": "whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "mistral": {"backend": "openai_compatible", "base_url": "https://api.mistral.ai/v1", "model": "voxtral-mini-latest", "api_key": "", "prompt": ""},
    "openrouter": {"backend": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "model": "openai/whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "together": {"backend": "openai_compatible", "base_url": "https://api.together.xyz/v1", "model": "openai/whisper-large-v3", "api_key": "", "prompt": ""},
    "local-swedish": {"backend": "local", "model": "KBLab/kb-whisper-large", "device": "cuda", "compute_type": "float16", "beam_size": 5, "prompt": ""},
}
_LOCAL_FIELDS = ["model", "device", "compute_type", "beam_size", "prompt"]
#: The profile form reads as a form, not as a config file: the keys stay the
#: keys (they are what is written), only what the user reads changes.
_FIELD_LABELS = {"backend": "Backend", "base_url": "Base URL", "model": "Model",
                 "api_key": "API key", "api_key_env": "API key variable",
                 "prompt": "Vocabulary hint", "device": "Device",
                 "compute_type": "Compute type", "beam_size": "Beam size"}
_CLOUD_FIELDS = ["base_url", "model", "api_key", "api_key_env", "prompt"]
_LANGUAGES = [("English", "en"), ("Swedish", "sv"), ("Auto-detect", "auto")]
#: inject.mode, in the order the combo shows it; the data is the config value.
_INJECT_MODE_LABELS = {"paste": "Paste automatically",
                       "clipboard": "Copy only (press Ctrl+V yourself)"}
#: Under the preview: what dragging it there can and cannot do.
PILL_PLACEMENT_NOTE = (
    "Drag the pill to where it should appear; arrow keys nudge it a pixel at a time, "
    "Shift+arrow ten. Dropping it near one of the nine anchors takes that anchor exactly. "
    "It is the anchor and the gap that are saved, not a free position, because that is "
    "what a compositor can be asked for - and it needs gtk4-layer-shell: without one the "
    "compositor decides where the pill goes and this setting does nothing.")
#: The "leave stt.active alone for this language" row of the profile table.
KEEP_CURRENT = "(keep current)"
#: The portal shortcuts, in the order they are shown, with their labels.
PORTAL_TRIGGERS = [("dictate", "Dictate"), ("recall", "Recall last"),
                   ("cancel", "Cancel recording"), ("language_toggle", "Switch language")]
#: The desktop's own shortcut editor, tried in PATH order: GNOME has no portal
#: reconfigure dialog, so its Keyboard panel is the next best thing.
SHORTCUT_SETTINGS_COMMANDS = (("gnome-control-center", "keyboard"),
                              ("systemsettings", "kcm_keys"))
#: Where to click when neither is installed, and the button's own subject.
SHORTCUT_SETTINGS_PATH = "Settings → Keyboard → Keyboard Shortcuts"
#: How the effective trigger reads beside a field. An id the desktop never
#: mentioned is one we never asked it to bind (an empty hotkeys.portal_* key).
EFFECTIVE_PREFIX = "desktop: "
NOT_REGISTERED = "not registered"
UNKNOWN_TRIGGER = "waiting for an answer"
#: Where GNOME really keeps the key, named in full because the whole complaint
#: was "I can't find CTRL+space anywhere in GNOME's keyboard settings".
GNOME_SHORTCUTS_KEY = GNOME_KEY_TEMPLATE.format(app_id=APP_ID)
#: Above the trigger fields. One line: the detail is behind the "?" beside it.
PORTAL_NOTE = ("Hold Dictate to talk. Your desktop owns these keys - Save writes them to "
               "it and rebinds, so the change takes effect at once.")
#: Behind the "?" on the Hotkeys tab: the detail a first-time reader needs once.
HOTKEY_HELP = {
    "evdev": ("voice reads the key straight from the keyboard device. \"Capture key\" fills "
              "the field in with the name of the key you press; combinations are typed by "
              "hand, e.g. KEY_LEFTMETA+KEY_SPACE."),
    "portal": (
        "Type a trigger the way your desktop spells it: CTRL+space, F13, CTRL+SHIFT+l. "
        "A bare modifier on its own will not bind.\n\n"
        f"GNOME keeps the key in its own store, not in voice's config: dconf, under "
        f"{GNOME_SHORTCUTS_KEY}. Its Settings app does not show that usefully, which is why "
        "Save writes it there for you and then rebinds. Beside each field is the key the "
        "desktop actually holds right now.\n\n"
        "On KDE the desktop's own dialog owns the key: use \"Open shortcut settings\". "
        "The evdev key fields below apply again if you switch hotkeys.backend to evdev."),
}
#: How long after the last drag or nudge the pill is shown on the desktop. Long
#: enough that a run of arrow keys is one preview, short enough to feel immediate.
PREVIEW_DELAY_MS = 600
#: What the window says while the real pill is on screen at the new placement.
PREVIEW_SHOWING = "Showing the pill there on your desktop…"
#: The same, where the compositor ignores placement: the pill does appear, just
#: not where it was dropped. Saying "there" beside the note explaining that the
#: desktop chooses is a contradiction the owner has to resolve on their own.
PREVIEW_SHOWING_ANYWHERE = "Showing the pill now - your desktop chooses where."
#: The daemon's refusals, in words that say what to do about it. The owner drags,
#: drops, sees nothing and concludes the feature is broken; the daemon knew why
#: all along and told the window, which threw it away.
PREVIEW_REFUSALS = {
    "not while a dictation is running": "Not while you are dictating - try again in a moment.",
}
#: Anything else it refuses with is shown as it comes rather than swallowed: the
#: other refusal names a setting ("ui.overlay = false") and is worth reading.
PREVIEW_REFUSED = "Not showing it: {error}"
#: And a refusal with nothing to say for itself.
PREVIEW_CANNOT = "Cannot show it here."
#: How long a refusal stays before it clears itself. A note left on screen is
#: read as the state of things now, so none of them may outlive what it describes:
#: the "showing" line goes when the pill does, the reason after long enough to
#: read it twice.
REFUSAL_NOTE_MS = 6000
#: How long to assume a preview lasts when the daemon's reply does not say, or
#: says something that is not a number. The daemon has its own PREVIEW_SECONDS
#: and normally tells us; this is only what to do when it has not.
ASSUMED_PREVIEW_SECONDS = 5.0
#: The one-liner beside each backend's fields; the rest is behind the "?".
HOTKEY_HINTS = {
    "evdev": "Type an evdev key name, or press \"Capture key\".",
    "portal": "These evdev keys apply only if hotkeys.backend goes back to evdev.",
}
#: Behind the other "?" buttons, one paragraph each.
HELP = {
    "pill_position": PILL_PLACEMENT_NOTE,
    "profile_per_language": (
        "Switching to one of these languages also activates the profile beside it, so a "
        "Swedish dictation uses a Swedish model without a second switch. \"(keep current)\" "
        "leaves the profile alone. Add the profile first on the Transcription tab."),
    "text_insertion": (
        "\"Paste automatically\" copies the text and sends the paste chord for you. "
        "\"Copy only\" leaves it on the clipboard and tells you to press Ctrl+V - which is "
        "what to use on a desktop that refuses synthetic keystrokes, or where the pill would "
        "take the keyboard."),
    "max_seconds": ("A recording stops itself after this many seconds, so a hotkey left held "
                    "by accident cannot record all afternoon."),
    "profiles": ("A profile is one transcription backend and its settings - a local model, or "
                 "a cloud API. \"Use this profile\" makes the selected one active; "
                 "\"Add from template\" fills in a known service, and you add the API key."),
    "dictionary": (
        "Every replacement is applied to the text before it is inserted, in order: names and "
        "jargon the model hears wrong, fixed once here. Flags are optional: \"icase\" matches "
        "any capitalisation, \"regex\" treats the left column as a regular expression."),
}
#: The longest run of text allowed to sit on a tab. Anything above this belongs
#: behind a "?": the window is a form, not a manual.
MAX_INLINE_TEXT = 160
#: The circled "?" itself, and how wide its answer is allowed to be.
HELP_SIZE = 18
HELP_WIDTH = 320
#: The circled "?", with its one colour left to be filled in per theme: a
#: colour written in here is a colour that cannot follow the desktop.
HELP_STYLE = """
QToolButton {{
    border: 1px solid {colour}; border-radius: {radius}px;
    color: {colour}; font-weight: bold; padding: 0px;
}}
QToolButton:hover {{ border-color: palette(highlight); color: palette(highlight); }}
"""
#: Layout only. What the dim captions are coloured with is taken from the
#: palette per widget - see `secondary_text_colour`.
DIALOG_STYLE = """
QGroupBox { font-weight: bold; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 2px; padding: 0 3px; }
"""
#: The least contrast a colour of ours may have against what is painted behind
#: it. These are sentences the owner has to read, not decoration, so the bar is
#: the WCAG AA floor for normal text rather than the 3:1 allowed for incidental
#: text - which left no margin at all: at 3:1 the worst caption measured 3.14:1
#: on the real pixels, and dim text that cannot be read is not dim, it is
#: missing.
MIN_CONTRAST = 4.5
#: How far the theme's own text colour is faded when the palette offers nothing
#: dim that can be read: enough to read as secondary, not enough to disappear.
DIM_ALPHA = 0.65
#: What the chooser actually aims for. No pixel of a thin glyph is ever the full
#: colour - a "?" is mostly antialiasing, and its circle is a one-pixel line -
#: so a colour computed to land exactly on the bar measures a few hundredths
#: under it once it is drawn. Aim a little over, and the rendered pixels clear
#: the bar rather than sitting just below it.
TARGET_CONTRAST = MIN_CONTRAST * 1.05
#: How far from `Window` a palette colour may be and still be believable as a
#: panel this window's text sits on. A tab pane is a shade of the window; a role
#: a hand-built palette never filled in is not.
SHADE_OF_WINDOW = 2.0
#: An error is a signal, not a theme colour, so the hue stays put on every
#: desktop - but `error_text_colour` lightens or darkens it until it can be
#: read, because a red the window swallows is not a signal at all.
ERROR_HUE = "#e5484d"
#: The one spacing the whole window uses, so no two tabs breathe differently.
ROW_SPACING = 8
COLUMN_SPACING = 12
MARGIN = 12


def shortcut_settings_command(which: Callable[[str], str | None] | None = None) -> list[str] | None:
    """The desktop's own shortcut editor, or None if neither is installed."""
    look_up = which or shutil.which          # resolved per call, not at import
    for command in SHORTCUT_SETTINGS_COMMANDS:
        if look_up(command[0]):
            return list(command)
    return None


def effective_trigger_text(triggers: dict[str, str] | None, name: str) -> str:
    """What the desktop holds for `name`, in words.

    An empty answer is "we have not been told yet", which is not the same as
    "no key assigned" - the portal has not answered before the first bind.
    """
    if not triggers:
        return UNKNOWN_TRIGGER
    if name not in triggers:
        return NOT_REGISTERED
    return triggers[name] or NO_TRIGGER


def _luminance(colour: QColor) -> float:
    """The WCAG relative luminance of an opaque colour, 0.0 to 1.0."""
    def channel(value: int) -> float:
        part = value / 255.0
        return part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(colour.red()) + 0.7152 * channel(colour.green())
            + 0.0722 * channel(colour.blue()))


def contrast_ratio(one: QColor, other: QColor) -> float:
    """How far apart two opaque colours are: 1.0 identical, 21.0 black on white."""
    high, low = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _over(colour: QColor, behind: QColor) -> QColor:
    """`colour` composited onto `behind`: a palette colour may be translucent.

    `PlaceholderText` usually is - it is the theme's own text at half alpha -
    and half of an invisible colour is still invisible, so it has to be flattened
    before anything is measured.
    """
    alpha = colour.alphaF()
    return QColor.fromRgbF(*(colour.redF() * alpha + behind.redF() * (1 - alpha),
                             colour.greenF() * alpha + behind.greenF() * (1 - alpha),
                             colour.blueF() * alpha + behind.blueF() * (1 - alpha)))


def _legible(colour: QColor, behind: QColor) -> QColor:
    """`colour`, moved towards white or black until it can be read on `behind`."""
    towards = QColor("white") if _luminance(behind) < 0.5 else QColor("black")
    mixed = colour
    for step in range(21):
        mixed = _over(QColor(towards.red(), towards.green(), towards.blue(),
                             round(255 * step / 20)), colour)
        if contrast_ratio(mixed, behind) >= TARGET_CONTRAST:
            break
    return mixed


def _mix(one: QColor, other: QColor, part: float) -> QColor:
    """`part` of the way from `one` to `other`."""
    return QColor.fromRgbF(one.redF() + (other.redF() - one.redF()) * part,
                           one.greenF() + (other.greenF() - one.greenF()) * part,
                           one.blueF() + (other.blueF() - one.blueF()) * part)


def conservative_background(palette: QPalette) -> QColor:
    """The hardest surface in the palette to read dim text on.

    The style does not paint `Window` behind a caption. Fusion fills a tab pane
    with a colour it derives from `Button` and lightens - `#424242` where the
    window is `#2b2b2b` - and that shade is nowhere in the palette. Where the
    real colour cannot be sampled (`painted_background`), the palette's own
    extreme in the direction that hurts is used instead: the lightest surface on
    a dark theme, where the text will be light, and the darkest on a light one.
    That errs towards too much contrast rather than towards too little.

    Only surfaces that are a shade of the window count. A palette built by hand
    leaves roles it was never given at their light defaults, and an
    `AlternateBase` of near-white behind a `#2b2b2b` window is not a panel this
    text will ever sit on - believing it would choose dark text for a dark
    window, which is the bug this whole ladder exists to avoid.
    """
    window = palette.color(QPalette.ColorRole.Window)
    surfaces = [colour for colour in
                (palette.color(role) for role in (QPalette.ColorRole.Base,
                                                  QPalette.ColorRole.AlternateBase,
                                                  QPalette.ColorRole.Button))
                if contrast_ratio(colour, window) <= SHADE_OF_WINDOW]
    surfaces.append(window)
    dark_theme = _luminance(window) < 0.5
    return max(surfaces, key=_luminance) if dark_theme else min(surfaces, key=_luminance)


def theme_palette(widget: QWidget) -> QPalette:
    """The palette a widget should take its theme from: its window's.

    Not its parent's. A stylesheet anywhere up the tree stops Qt propagating a
    palette set on that widget down to its children - even a stylesheet with no
    colour in it, which is what this dialog has - so a parent halfway down can
    still be holding the theme before last. The window is always current: it is
    what the palette was set on. An application-wide change, which is what a
    desktop switching theme actually does, reaches everything either way.
    """
    window = widget.window()
    if window is None or window is widget:
        return QApplication.palette()
    return window.palette()


def painted_background(widget: QWidget, theme: QPalette) -> QColor | None:
    """What is really painted behind `widget`, or None while nothing can say.

    Walks out through the ancestors, rendering each one alone - no children, and
    no forced background - onto a transparent pixel at the widget's own centre.
    The first one that covers that pixel is what the text sits on: a group box
    paints only its frame, a tab page paints nothing at all, and it is the tab
    widget's pane that is actually behind every caption in this window.

    A whole column of the widget's height is sampled, not one pixel: a tab pane
    is a gradient, so the top of a caption sits on a slightly different shade
    from its bottom, and the one that is taken is the shade that makes the text
    hardest to read - the lightest on a dark theme, the darkest on a light one.

    None until the widget has been laid out, which is why everything here is
    re-taken on `showEvent` as well as on a palette change.
    """
    height = widget.height()
    if not widget.isVisible() or widget.width() <= 0 or height <= 0:
        # Nothing is painted behind a widget that is not on screen, and its
        # ancestors have neither been laid out nor, on a window that was given a
        # palette it could not pass on, repainted in the theme now running. What
        # they would draw is fiction; the palette's own worst case is not.
        return None
    top = QPoint(widget.width() // 2, 0)
    column = QImage(1, height, QImage.Format.Format_ARGB32)
    dark_theme = _luminance(theme.color(QPalette.ColorRole.Window)) < 0.5
    ancestor = widget.parentWidget()
    while ancestor is not None:
        spot = widget.mapTo(ancestor, top)
        if ancestor.rect().contains(spot):
            column.fill(Qt.GlobalColor.transparent)
            try:
                ancestor.render(column, QPoint(0, 0), QRegion(QRect(spot, QSize(1, height))),
                                QWidget.RenderFlag(0))
            except Exception as exc:        # a colour is never worth a broken window
                log.debug("could not sample what is behind %s: %s", widget.objectName(), exc)
                return None
            painted = [column.pixelColor(0, y) for y in range(height)
                       if column.pixelColor(0, y).alpha() == 255]
            if painted:
                return (max(painted, key=_luminance) if dark_theme
                        else min(painted, key=_luminance))
        ancestor = ancestor.parentWidget()
    return None


def secondary_text_colour(palette: QPalette, behind: QColor | None = None) -> QColor:
    """Dim text the running theme can actually show, taken from its own palette.

    `PlaceholderText` is the role meant for exactly this and most themes fill it
    in; a palette built by hand leaves it black, which a dark window swallows.
    The disabled `WindowText` is the usual second best, and on a light theme it
    is a grey too pale to read. The theme's own text colour, faded, is the third.
    None is trusted on its own: whichever of them can be read on `behind` wins.

    And if none of them can, the ladder does not stop at a best effort - the
    dimmest candidate is moved towards the theme's own text until it clears the
    bar, and past even that towards black or white. There is always an answer,
    because dim text that cannot be read is not dim, it is missing.
    """
    behind = behind if behind is not None and behind.isValid() else conservative_background(palette)
    foreground = palette.color(QPalette.ColorRole.WindowText)
    faded = QColor(foreground)
    faded.setAlphaF(DIM_ALPHA)
    ladder = [_over(palette.color(QPalette.ColorGroup.Active,
                                  QPalette.ColorRole.PlaceholderText), behind),
              _over(palette.color(QPalette.ColorGroup.Disabled,
                                  QPalette.ColorRole.WindowText), behind),
              _over(faded, behind)]
    for candidate in ladder:
        if contrast_ratio(candidate, behind) >= TARGET_CONTRAST:
            return candidate
    for step in range(1, 21):
        towards_text = _mix(ladder[0], foreground, step / 20)
        if contrast_ratio(towards_text, behind) >= TARGET_CONTRAST:
            return towards_text
    return _legible(foreground, behind)     # a theme whose own text cannot be read here


def error_text_colour(palette: QPalette, behind: QColor | None = None) -> QColor:
    """The red beside Save, lightened or darkened until this theme can show it."""
    behind = behind if behind is not None and behind.isValid() else conservative_background(palette)
    return _legible(QColor(ERROR_HUE), behind)


class TintedLabel(QLabel):
    """A wrapping label whose colour is taken from the palette, never written down.

    A grey written into a stylesheet suits one desktop and vanishes on the next;
    this asks the theme instead, and asks again whenever the theme changes, so a
    desktop that switches to dark while the window is open is followed.

    The colour is derived from the *parent's* palette rather than from its own,
    which this class overwrites - deriving from a colour it had already dimmed
    would fade a shade further on every palette change - and it is measured
    against the colour really painted behind the label, not against `Window`,
    which the style does not put there.
    """

    def __init__(self, text: str = "",
                 tint: Callable[[QPalette, QColor | None], QColor] = secondary_text_colour,
                 parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        self._tint = tint
        self._tinting = False
        self.retint()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ParentChange):
            self.retint()

    def showEvent(self, event) -> None:
        # Laid out at last: only now can what is behind this label be sampled.
        super().showEvent(event)
        self.retint()

    def theme(self) -> QPalette:
        """The palette this label should take its colour from."""
        return theme_palette(self)

    def retint(self) -> None:
        if self._tinting:                    # setPalette comes back through changeEvent
            return
        self._tinting = True
        try:
            theme = self.theme()
            colour = self._tint(theme, painted_background(self, theme))
            # Two roles on an otherwise untouched palette, never a copy of the
            # resolved one: a palette with every role spoken for stops
            # inheriting, so Qt would never tell this label the theme changed
            # again - and the first thing it would miss is the switch to dark.
            palette = QPalette()
            palette.setColor(QPalette.ColorRole.WindowText, colour)
            palette.setColor(QPalette.ColorRole.Text, colour)
            self.setPalette(palette)
        finally:
            self._tinting = False


def _wrapped(text: str) -> QLabel:
    """A label that wraps: these hold sentences, not words."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _caption(text: str = "") -> TintedLabel:
    """A dim one-line note under a control. Never a paragraph - see HELP."""
    label = TintedLabel(text)
    label.setProperty("caption", True)
    return label


class HelpButton(QToolButton):
    """The circled "?" beside a control: one click, one paragraph of detail.

    The tabs used to carry that paragraph inline, which is what made the window
    read as a wall of text. The words are unchanged; they are simply not on
    screen until they are asked for - and they are the tooltip as well, so
    hovering answers the question without a click.

    The circle and the glyph take the same theme-derived colour as the captions:
    a "?" nobody can see is a paragraph nobody can reach.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.help_text = text
        #: The colour the circle and the "?" are drawn in, per theme.
        self.glyph_colour = QColor()
        self._tinting = False
        self.setText("?")
        self.setAccessibleName("More information")
        self.setToolTip(_as_rich(text))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(HELP_SIZE, HELP_SIZE)
        self.retint()
        self.clicked.connect(self._show)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ParentChange):
            self.retint()

    def showEvent(self, event) -> None:
        # Laid out at last: only now can what is behind this glyph be sampled.
        super().showEvent(event)
        self.retint()

    def retint(self) -> None:
        """Take the glyph's colour from whatever theme is running now."""
        if self._tinting:                    # applying a stylesheet re-enters here
            return
        self._tinting = True
        try:
            theme = theme_palette(self)
            self.glyph_colour = secondary_text_colour(theme, painted_background(self, theme))
            self.setStyleSheet(HELP_STYLE.format(colour=self.glyph_colour.name(),
                                                 radius=HELP_SIZE // 2))
        finally:
            self._tinting = False

    def _show(self) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), _as_rich(self.help_text),
                          self)


def _as_rich(text: str) -> str:
    """Help text as the small rich-text block a tooltip will wrap for us."""
    body = "<br><br>".join(escape(part.strip()) for part in text.split("\n\n") if part.strip())
    return f"<div style='max-width:{HELP_WIDTH}px'>{body}</div>"


def _preview_seconds(reply: dict) -> float:
    """How long the note about `reply` should stay up.

    A line about a pill on screen goes when the pill does, so it takes the
    daemon's own number; a reason stays long enough to be read twice. Whatever
    the reply holds, this returns a number: the note is set from a Qt slot, and
    a daemon a version ahead must not be able to raise out of one.
    """
    if not reply.get("ok"):
        return REFUSAL_NOTE_MS / 1000
    try:
        seconds = float(reply.get("seconds"))
    except (TypeError, ValueError):
        return ASSUMED_PREVIEW_SECONDS
    return seconds if seconds > 0 else ASSUMED_PREVIEW_SECONDS


def _spawn(command: list[str]) -> None:
    """Start the desktop's settings app detached: it outlives this dialog."""
    subprocess.Popen(command, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SettingsDialog(QDialog):
    saved = Signal()
    #: The desktop's own shortcut store took a new trigger, so whatever is
    #: listening has to bind again - otherwise the old key stays live until the
    #: daemon is restarted, which is exactly the surprise this feature removes.
    shortcuts_rebound = Signal()
    _captured = Signal(str)
    #: The portal answers capture_next from the listener thread; both of these
    #: hop back onto the Qt thread before a widget is touched.
    _desktop_answered = Signal(str)

    def __init__(self, config: Config, capture_key: Callable[[Callable[[str], None]], None],
                 sources: Callable[[], list[Source]], parent=None, backend: str = "evdev",
                 triggers: Callable[[], dict[str, str]] | None = None,
                 shortcut_store: Callable[[], object | None] = desktop_shortcut_store,
                 preview_pill: Callable[[str, int, int], dict] | None = None):
        super().__init__(parent)
        self._backend = backend
        #: Asks the daemon to show the real pill at a placement for a few
        #: seconds. None when nothing can show one (no daemon behind us), and
        #: then the placer simply records the placement as it always did.
        self._preview_pill = preview_pill
        #: Where this desktop keeps its global shortcuts, or None where it keeps
        #: them somewhere we must not touch (KDE, which has its own dialog).
        self._shortcut_store = shortcut_store
        #: Reads back what the desktop actually holds per shortcut id. The
        #: portal_* fields can only ever ask for a trigger - on GNOME not even
        #: that - so this is the only truthful thing the tab can show.
        self._triggers = triggers
        self.setWindowTitle("voice settings")
        self.setMinimumWidth(560)
        # The dialog edits a private Config loaded from the same file: Close simply
        # discards it, and the daemon's live Config is never mutated from here.
        # Save writes to disk and emits `saved`; the daemon reloads from disk.
        self._cfg = Config.load(config.path)
        self._capture_key, self._sources = capture_key, sources
        #: The microphone the *user* picked in this window, or None while the
        #: combo is only showing what the config holds. Listing the sources is
        #: slow enough that the daemon does it in the background and calls
        #: `set_sources` later, and that must not overwrite a live choice.
        self._device_choice: str | None = None
        #: Whether this desktop can place the pill at all; None until probed.
        self._layer_shell: bool | None = None
        self._current_profile: str | None = None
        self._active_changed = False       # True once "Use this profile" was pressed
        self._language_changed = False     # True once the user picked a language here
        self._inject_mode_changed = False  # True once the user picked a text insertion mode here
        self._pill_placement_changed = False  # True once the pill was moved here
        self._captured.connect(self._on_captured)
        self._desktop_answered.connect(self._on_desktop_answered)
        #: Every circled "?" in the window, by the control it explains.
        self.help_buttons: dict[str, HelpButton] = {}
        self.setStyleSheet(DIALOG_STYLE)
        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._hotkeys_tab(), "Hotkeys")
        tabs.addTab(self._audio_tab(), "Audio")
        tabs.addTab(self._transcription_tab(), "Transcription")
        tabs.addTab(self._dictionary_tab(), "Dictionary")
        self.error_label = TintedLabel("", tint=error_text_colour)
        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(tabs)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self._load()

    # -- the pieces every tab is built from -----------------------------------
    def _form(self, parent: QWidget | None = None) -> QFormLayout:
        """One form layout, spaced and aligned like every other one here."""
        form = QFormLayout(parent) if parent is not None else QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(COLUMN_SPACING)
        form.setVerticalSpacing(ROW_SPACING)
        form.setContentsMargins(0, 0, 0, 0)
        return form

    def _group(self, title: str, layout) -> QGroupBox:
        """A titled box around one group of rows, so a tab reads as sections."""
        box = QGroupBox(title)
        box.setLayout(layout)
        layout.setContentsMargins(MARGIN, ROW_SPACING, MARGIN, MARGIN)
        return box

    def _row(self, form: QFormLayout, label: str, field: QWidget,
             help_key: str | None = None) -> None:
        """One labelled row, with the circled "?" at the end where there is one."""
        form.addRow(label, field if help_key is None
                    else self._with_help(field, help_key, HELP[help_key]))

    def _with_help(self, field: QWidget, key: str, text: str) -> QWidget:
        """`field` with a circled "?" after it, as one widget the form can take.

        The "?" ends up in the same place on every row, so the buttons read as
        a column rather than as decoration stuck to each field.
        """
        button = HelpButton(text)
        self.help_buttons[key] = button
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(COLUMN_SPACING // 2)
        if (field.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed
                or field.minimumWidth() == field.maximumWidth()):
            # A narrow field stays narrow and on the left; the "?" still lines
            # up with every other one down the right of the column.
            row.addWidget(field, 0)
            row.addStretch(1)
        else:
            row.addWidget(field, 1)
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        return holder

    def showEvent(self, event) -> None:
        """Re-take every dim colour once the window has been laid out.

        A child's own `showEvent` arrives before this one and, on the tab that
        opens first, before the tab widget has a pane to sample - so that tab's
        captions would keep the colour they were given against a guess while
        every other tab got the real one. Qt sends this last; by here the
        geometry is final and there is something real behind them.
        """
        super().showEvent(event)
        self.retint_all()

    def changeEvent(self, event) -> None:
        """A palette set on this window has to be passed on by hand.

        This dialog has a stylesheet, and a stylesheet stops Qt propagating a
        palette change from here down to the children - so the captions would
        never hear about it. An application-wide change reaches them on its own;
        this covers the case where the window is given a palette directly.
        """
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self.retint_all()

    def retint_all(self) -> None:
        """Every dim colour in the window, re-taken from the theme as it is now."""
        for widget in self.findChildren(TintedLabel) + self.findChildren(HelpButton):
            widget.retint()

    def reload_from_disk(self) -> None:
        """Re-read the file and repopulate every widget, discarding unsaved edits."""
        self._cfg = Config.load(self._cfg.path)
        self._current_profile = None       # so repopulating cannot commit stale form values
        self._active_changed = False
        self._language_changed = False
        self._inject_mode_changed = False
        self._pill_placement_changed = False
        self.preview_timer.stop()          # an abandoned gesture shows nothing
        self._say_about_preview("")
        self.profile_form = {}
        self._device_choice = None
        self.error_label.setText("")
        self._load()

    # -- tabs -----------------------------------------------------------------
    def _general_tab(self) -> QWidget:
        w = QWidget()
        self.language_combo = QComboBox()
        for label, code in _LANGUAGES:
            self.language_combo.addItem(label, code)
        # Only a change made *here* may overwrite the language on disk: the
        # toggle hotkey and the tray change it while this dialog sits open.
        self.language_combo.currentIndexChanged.connect(self._on_language_picked)
        self.notifications_combo = QComboBox()
        self.notifications_combo.addItems(["on", "off"])
        self.inject_mode_combo = QComboBox()
        for code in INJECT_MODES:
            self.inject_mode_combo.addItem(_INJECT_MODE_LABELS[code], code)
        # Connected after the items exist, and for the same reason as the language
        # combo: only a change made *here* may overwrite what the file says.
        self.inject_mode_combo.currentIndexChanged.connect(self._on_inject_mode_picked)
        # This screen in miniature, with the pill in it. Only a drag or a nudge
        # *here* may overwrite what the file says - it can be hand-edited while
        # this window sits open - so the widget stays quiet when it is merely
        # shown a placement.
        self.pill_placer = PillPlacer()
        self.pill_placer.placement_changed.connect(self._on_pill_placement_picked)
        # Three captions under the placement, each at most a line: what it is,
        # whether this desktop will honour it, and what the preview is doing.
        # None of them is an error, and none of them is a paragraph.
        #: One preview per gesture: a drag emits once, but arrow keys emit per
        #: keystroke, and a helper restarted per keystroke flickers across the
        #: screen. Every move restarts this; the pause after the last one shows.
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self._request_pill_preview)
        #: And what takes the note off again. A note describes what is happening
        #: now - a pill on screen, or a refusal a moment old - so none of them
        #: may outlive that and be read as still true.
        self.preview_note_timer = QTimer(self)
        self.preview_note_timer.setSingleShot(True)
        self.preview_note_timer.timeout.connect(lambda: self.preview_note.setText(""))
        self.pill_placement_label = _caption()
        self.placement_warning = _caption(NO_LAYER_SHELL_NOTE)
        self.placement_warning.setVisible(False)
        self.preview_note = _caption()
        beside = QVBoxLayout()
        beside.setSpacing(ROW_SPACING // 2)
        beside.addWidget(self.pill_placement_label)
        beside.addWidget(self.placement_warning)
        beside.addWidget(self.preview_note)
        # Full width rather than a form row: the placer is as wide as the screen
        # it models, and a label column beside it leaves the captions in a
        # gutter too narrow to read.
        pill_help = HelpButton(HELP["pill_position"])
        self.help_buttons["pill_position"] = pill_help
        preview = QHBoxLayout()
        preview.setSpacing(COLUMN_SPACING)
        preview.addWidget(self.pill_placer, 0, Qt.AlignmentFlag.AlignTop)
        preview.addStretch(1)
        preview.addWidget(pill_help, 0, Qt.AlignmentFlag.AlignTop)
        # The captions go under the placer, not beside it: a column beside a
        # 320 px preview is too narrow to read a sentence in.
        placement = QVBoxLayout()
        placement.setSpacing(ROW_SPACING)
        placement.addLayout(preview)
        placement.addLayout(beside)
        self.language_profile_table = QTableWidget(0, 2)
        self.language_profile_table.setHorizontalHeaderLabels(["Language", "Profile"])
        self.language_profile_table.horizontalHeader().setStretchLastSection(True)
        self.language_profile_table.verticalHeader().setVisible(False)
        # The last column stretches, so there is nothing to scroll to sideways -
        # and a scrollbar that appears anyway steals the height the rows need.
        self.language_profile_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.language_profile_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.language_profile_combos: dict[str, QComboBox] = {}

        dictation = self._form()
        self._row(dictation, "Language", self.language_combo)
        self._row(dictation, "Notifications", self.notifications_combo)
        self._row(dictation, "Text insertion", self.inject_mode_combo, "text_insertion")
        self._row(dictation, "Profile per language", self.language_profile_table,
                  "profile_per_language")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self._group("Dictation", dictation))
        layout.addWidget(self._group("Recording pill", placement))
        layout.addStretch()
        return w

    def _hotkeys_tab(self) -> QWidget:
        w = QWidget()
        self.hotkey_edit = QLineEdit()
        self.capture_button = QPushButton("Capture key")
        self.capture_button.clicked.connect(self._start_capture)
        row = QHBoxLayout()
        row.setSpacing(COLUMN_SPACING // 2)
        row.addWidget(self.hotkey_edit)
        row.addWidget(self.capture_button)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["hold", "toggle"])
        self.recall_edit = QLineEdit()
        self.cancel_edit = QLineEdit()
        self.portal_edits: dict[str, QLineEdit] = {}
        #: The trigger the desktop holds, shown beside each field it belongs to.
        self.portal_effective: dict[str, QLabel] = {}
        self.portal_note: QLabel | None = None
        self.shortcuts_button: QPushButton | None = None
        self.shortcut_note: QLabel | None = None
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        if self._backend == "portal":
            # The compositor consumes the chord before we see it, so there is
            # nothing to capture: these fields are the desktop's own keys, and
            # Save writes them into its store (see _apply_desktop_shortcuts).
            self.capture_button.setVisible(False)
            desktop = self._form()
            self.portal_note = _caption(PORTAL_NOTE)
            desktop.addRow(self._with_help(self.portal_note, "hotkeys",
                                           HOTKEY_HELP[self._backend]))
            for name, label in PORTAL_TRIGGERS:
                edit = QLineEdit()
                edit.setPlaceholderText("not bound")
                effective = _caption()
                self.portal_edits[name] = edit
                self.portal_effective[name] = effective
                pair = QVBoxLayout()          # never `row`: that one holds the dictate key
                pair.setSpacing(2)
                pair.addWidget(edit)
                pair.addWidget(effective)
                desktop.addRow(label, pair)
            self.shortcuts_button = QPushButton("Open shortcut settings…")
            self.shortcuts_button.clicked.connect(self._open_shortcut_settings)
            self.shortcut_note = _caption()
            desktop.addRow("", self.shortcuts_button)
            desktop.addRow("", self.shortcut_note)
            layout.addWidget(self._group("Desktop shortcuts", desktop))
        self.hotkey_hint = _caption(HOTKEY_HINTS.get(self._backend, HOTKEY_HINTS["evdev"]))
        keys = self._form()
        if self._backend == "portal":
            keys.addRow(self.hotkey_hint)          # spans: it is about the group
        else:
            keys.addRow(self._with_help(self.hotkey_hint, "hotkeys",
                                        HOTKEY_HELP[self._backend]))
        keys.addRow("Dictate key", row)
        keys.addRow("Mode", self.mode_combo)
        keys.addRow("Recall last", self.recall_edit)
        keys.addRow("Cancel recording", self.cancel_edit)
        layout.addWidget(self._group("Keyboard device keys", keys))
        layout.addStretch()
        return w

    def _audio_tab(self) -> QWidget:
        w = QWidget()
        self.device_combo = QComboBox()
        self.device_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
        self.device_combo.currentIndexChanged.connect(self._on_device_picked)
        self.max_seconds = QSpinBox()
        self.max_seconds.setRange(5, 600)
        self.max_seconds.setSuffix(" s")
        self.max_seconds.setFixedWidth(90)
        form = self._form()
        self._row(form, "Microphone", self.device_combo)
        self._row(form, "Stop after", self.max_seconds, "max_seconds")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self._group("Recording", form))
        layout.addStretch()
        return w

    def _transcription_tab(self) -> QWidget:
        w = QWidget()
        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._show_profile)
        self.add_profile_combo = QComboBox()
        self.add_profile_combo.addItems(list(PROFILE_TEMPLATES))
        self.add_profile_button = QPushButton("Add from template")
        self.add_profile_button.clicked.connect(self._add_profile)
        self.activate_button = QPushButton("Use this profile")
        self.activate_button.clicked.connect(self._activate_profile)
        left = QVBoxLayout()
        left.setSpacing(ROW_SPACING // 2)
        left.addWidget(self.profile_list, 1)
        add = QHBoxLayout()                       # the two halves of one action
        add.setSpacing(ROW_SPACING // 2)
        add.addWidget(self.add_profile_combo, 1)
        add.addWidget(self.add_profile_button)
        left.addLayout(add)
        left.addWidget(self.activate_button)
        self.profile_form: dict[str, QLineEdit] = {}
        self._form_widget = QWidget()
        self._form_layout = self._form(self._form_widget)
        self.active_label = QLabel()
        self.active_label.setStyleSheet("font-weight: bold")
        right = QVBoxLayout()
        right.setSpacing(ROW_SPACING)
        right.addWidget(self.active_label)
        right.addWidget(self._form_widget)
        right.addStretch()
        columns = QHBoxLayout()
        columns.setSpacing(COLUMN_SPACING)
        columns.addWidget(self._group("Profiles", left), 1)
        columns.addWidget(self._group("Settings", right), 2)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(ROW_SPACING)
        layout.addLayout(columns)
        layout.addWidget(self._with_help(_caption("The active profile transcribes every "
                                                  "dictation."), "profiles", HELP["profiles"]))
        return w

    def _dictionary_tab(self) -> QWidget:
        w = QWidget()
        self.replacements_table = QTableWidget(0, 3)
        self.replacements_table.setHorizontalHeaderLabels(["Heard", "Replace with", "Flags"])
        self.replacements_table.horizontalHeader().setStretchLastSection(True)
        self.replacements_table.verticalHeader().setVisible(False)
        add = QPushButton("Add row")
        add.clicked.connect(lambda: self.replacements_table.insertRow(self.replacements_table.rowCount()))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self.replacements_table.removeRow(self.replacements_table.currentRow()))
        row = QHBoxLayout()
        row.setSpacing(ROW_SPACING // 2)
        row.addStretch()
        row.addWidget(add)
        row.addWidget(remove)
        inner = QVBoxLayout()
        inner.setSpacing(ROW_SPACING)
        inner.addWidget(self._with_help(_caption("Fixes applied to every dictation, in order."),
                                        "dictionary", HELP["dictionary"]))
        inner.addWidget(self.replacements_table, 1)
        inner.addLayout(row)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.addWidget(self._group("Word replacements", inner))
        return w

    # -- load/save --------------------------------------------------------------
    def set_sources(self, sources: Iterable[Source]) -> None:
        """Repopulate the microphone list, keeping the chosen device chosen.

        Listing them means running `pw-dump`, a subprocess with a five second
        timeout, so the daemon hands the window whatever it listed last and
        calls this again when a fresh listing arrives. Two rules make that
        safe: a device the listing does not mention still gets a row of its
        own, because a window that quietly reselected "System default" would
        write that back the next time the user pressed Save; and a choice the
        user has already made here wins over the file.
        """
        wanted = self._device_choice
        if wanted is None:
            wanted = self._cfg.get("audio.device", "") or ""
        blocked = self.device_combo.blockSignals(True)   # repopulating is not a pick
        try:
            self.device_combo.clear()
            self.device_combo.addItem("System default", "")
            for src in sources:
                self.device_combo.addItem(
                    src.description + (" (default)" if src.is_default else ""), src.name)
            if wanted and self.device_combo.findData(wanted) < 0:
                self.device_combo.addItem(wanted, wanted)
            self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(wanted)))
        finally:
            self.device_combo.blockSignals(blocked)

    def _on_device_picked(self) -> None:
        self._device_choice = self.device_combo.currentData()

    def _load(self) -> None:
        c = self._cfg
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(c.get("general.language", "en"))))
        self.notifications_combo.setCurrentText("on" if c.get("general.notifications", True) else "off")
        self.inject_mode_combo.setCurrentIndex(max(0, self.inject_mode_combo.findData(c.get("inject.mode", "paste"))))
        self._show_pill_placement(*c.overlay_placement())
        self.hotkey_edit.setText(c.get("hotkeys.dictate", ""))
        self.mode_combo.setCurrentText(c.get("hotkeys.dictate_mode", "hold"))
        self.recall_edit.setText(c.get("hotkeys.recall", ""))
        self.cancel_edit.setText(c.get("hotkeys.cancel", ""))
        for name, edit in self.portal_edits.items():
            edit.setText(c.portal_trigger(name))
        self.refresh_effective_triggers()
        self._device_choice = None         # populating the combo is not a user edit
        self.set_sources(self._sources())
        self.max_seconds.setValue(int(c.get("audio.max_seconds", 120)))
        self.profile_list.clear()
        for name in (c.get("stt.profiles", {}) or {}):
            self.profile_list.addItem(name)
        self.active_label.setText(f"Active profile: {c.get('stt.active')}")
        self.profile_list.setCurrentRow(0)
        rules = c.get("dictionary.replacements", []) or []
        self.replacements_table.setRowCount(len(rules))
        for i, rule in enumerate(rules):
            self.set_replacement_row(i, *(list(rule) + ["", "", ""])[:3])
        self._load_language_profiles()
        self._language_changed = False     # populating the combos is not a user edit
        self._inject_mode_changed = False
        self._pill_placement_changed = False

    def _show_pill_placement(self, position: str, margin_x: int, margin_y: int) -> None:
        """Put a placement into the preview and say it in words beside it.

        Fed from `Config.overlay_placement()`, so a file holding one of the two
        older values - or something that is not a placement at all - still shows
        the pill somewhere real.
        """
        self.pill_placer.set_placement(position, margin_x, margin_y)
        self.pill_placement_label.setText(placement_summary(position, margin_x, margin_y))

    def set_layer_shell(self, available: bool | None) -> None:
        """Say whether this desktop can put the pill where the placer says.

        `False` is the GNOME case the owner hit: a plain GTK window on Wayland
        cannot position itself and GNOME has no layer shell, so the compositor
        decides and the pill appears in the middle whatever is dragged here.
        The control stays live - the setting is recorded, and it applies on a
        machine that does have one. `None` is "nobody has probed yet", which is
        not a claim either way and shows nothing.
        """
        self._layer_shell = available
        self.placement_warning.setVisible(available is False)

    def _chosen_pill_placement(self) -> tuple[str, int, int]:
        """The placement as this dialog would save it."""
        return self.pill_placer.placement()

    def _load_language_profiles(self, mapping: dict[str, str] | None = None) -> None:
        """One row per general.languages entry, each with the profiles that exist.

        `mapping` overrides what the file says, so the table can be rebuilt after a
        profile is added without losing choices made here but not yet saved.
        """
        mapping = self._cfg.language_profiles() if mapping is None else mapping
        profiles = list(self._cfg.get("stt.profiles", {}) or {})
        codes = self._cfg.languages()
        table = self.language_profile_table
        # Take the old combos out by hand: replacing a cell widget only schedules
        # the previous one for deletion, and until that runs it stays parented to
        # the viewport and paints over the first cell.
        for row in range(table.rowCount()):
            for col in range(table.columnCount()):
                widget = table.cellWidget(row, col)
                if widget is not None:
                    table.removeCellWidget(row, col)
                    widget.setParent(None)
                    widget.deleteLater()
        self.language_profile_combos = {}
        table.setRowCount(len(codes))
        for row, code in enumerate(codes):
            item = QTableWidgetItem(code)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 0, item)
            combo = QComboBox()
            combo.addItem(KEEP_CURRENT, "")
            for name in profiles:
                combo.addItem(name, name)
            # A map naming a profile that no longer exists falls back to
            # "(keep current)" rather than offering something unbuildable.
            combo.setCurrentIndex(max(0, combo.findData(mapping.get(code.lower(), ""))))
            table.setCellWidget(row, 1, combo)
            self.language_profile_combos[code] = combo
        # Exactly as tall as its rows: a fixed height leaves either dead space
        # under two languages or a scrollbar under four. The rows have to be
        # measured after the combos are in them - a row still holding only its
        # default height reports about half what the combo will need, which is
        # what squashed this table to a row and a half.
        table.resizeRowsToContents()
        rows = sum(max(table.rowHeight(r),
                       table.cellWidget(r, 1).sizeHint().height() if table.cellWidget(r, 1) else 0)
                   for r in range(table.rowCount()))
        table.setFixedHeight(table.horizontalHeader().height() + rows + 2 * table.frameWidth())

    def _on_language_picked(self, index: int) -> None:
        self._language_changed = True

    def _on_inject_mode_picked(self, index: int) -> None:
        self._inject_mode_changed = True

    def _on_pill_placement_picked(self) -> None:
        """The owner dragged or nudged the pill in the preview."""
        self._pill_placement_changed = True
        self.pill_placement_label.setText(placement_summary(*self.pill_placer.placement()))
        # Whatever the note said was about the placement that has just been left
        # behind, so it is not true of this one even where it will be again.
        self._say_about_preview("")
        if self._preview_pill is not None:
            self.preview_timer.start(PREVIEW_DELAY_MS)

    def _say_about_preview(self, note: str, seconds: float = 0.0) -> None:
        """Put one line about the preview beside the placer, for `seconds`.

        Its own line, under the note about what this desktop does with a
        placement: the two answer different questions - "will this setting be
        honoured here" and "did anything happen just now" - and an owner on
        GNOME needs both at once.
        """
        self.preview_note_timer.stop()
        self.preview_note.setText(note)
        if note:
            self.preview_note_timer.start(int(seconds * 1000))

    def preview_message(self, reply: dict) -> str:
        """What a reply from the daemon says, in words for this window."""
        if reply.get("ok"):
            return PREVIEW_SHOWING if self._layer_shell is not False else PREVIEW_SHOWING_ANYWHERE
        error = str(reply.get("error") or "").strip()
        if not error:
            return PREVIEW_CANNOT
        return PREVIEW_REFUSALS.get(error.lower(), PREVIEW_REFUSED.format(error=error))

    def _request_pill_preview(self) -> None:
        """Ask the daemon to put the real pill where the placer says, briefly.

        Nothing here is a settings error: a daemon that refuses (no pill, or a
        dictation in flight) has a reason worth reading, and a daemon that has
        gone away is worth saying once - but neither belongs in the red label
        beside Save, and neither may stop the placement being saved.

        What it may not do is say nothing at all, which is what it used to do:
        the owner dragged the pill while dictating, saw nothing happen, and
        reported the feature as broken. The daemon had said why the whole time.
        """
        if self._preview_pill is None:
            return
        try:
            reply = self._preview_pill(*self.pill_placer.placement()) or {}
        except Exception as exc:
            log.debug("the pill preview could not be started: %s", exc)
            self._say_about_preview(PREVIEW_REFUSED.format(error=exc),
                                    REFUSAL_NOTE_MS / 1000)
            return
        self._say_about_preview(self.preview_message(reply), _preview_seconds(reply))

    def set_replacement_row(self, row: int, src: str, dst: str, flags: str = "") -> None:
        for col, val in enumerate((src, dst, flags)):
            self.replacements_table.setItem(row, col, QTableWidgetItem(str(val)))

    def _show_profile(self, row: int) -> None:
        self._commit_profile_form()
        item = self.profile_list.item(row)
        if item is None:
            return
        name = item.text()
        self._current_profile = name
        profile = self._cfg.get(f"stt.profiles.{name}", {}) or {}
        while self._form_layout.rowCount():
            self._form_layout.removeRow(0)
        self.profile_form = {}
        fields = _LOCAL_FIELDS if profile.get("backend") == "local" else _CLOUD_FIELDS
        self._form_layout.addRow(_FIELD_LABELS["backend"],
                                 QLabel(str(profile.get("backend", ""))))
        for field in fields:
            edit = QLineEdit(str(profile.get(field, "")))
            if field == "api_key":
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.profile_form[field] = edit
            self._form_layout.addRow(_FIELD_LABELS.get(field, field), edit)

    def _commit_profile_form(self) -> bool:
        if not self._current_profile or not self.profile_form:
            return True
        values: dict[str, object] = {}
        for field, edit in self.profile_form.items():
            value: object = edit.text()
            if field == "beam_size":
                try:
                    value = int(value or 5)
                except ValueError:
                    value = None
                if value is None or value < 1:
                    self.error_label.setText(f"{self._current_profile}.{field} must be a positive integer")
                    return False
            values[field] = value
        for field, value in values.items():
            self._cfg.set(f"stt.profiles.{self._current_profile}.{field}", value)
        return True

    def _add_profile(self) -> None:
        name = self.add_profile_combo.currentText()
        if self.profile_list.findItems(name, Qt.MatchFlag.MatchExactly):
            return
        for field, value in PROFILE_TEMPLATES[name].items():
            self._cfg.set(f"stt.profiles.{name}.{field}", value)
        self.profile_list.addItem(name)
        self.profile_list.setCurrentRow(self.profile_list.count() - 1)
        # The new profile has to be selectable per language straight away: adding
        # local-swedish and mapping sv to it is one visit to this window.
        self._load_language_profiles(self._chosen_language_profiles())

    def _activate_profile(self) -> None:
        if self._current_profile:
            self._cfg.set("stt.active", self._current_profile)
            self._active_changed = True
            self.active_label.setText(f"Active profile: {self._current_profile}")

    def refresh_effective_triggers(self) -> None:
        """Show what the desktop holds right now, beside each trigger field.

        Called on every load, so reopening the window (which re-reads the file,
        and makes the daemon ask the portal again) also re-reads the keys - a
        label captured when the daemon started is the same lie as a field that
        cannot move one.
        """
        if not self.portal_effective:
            return
        triggers: dict[str, str] = {}
        if self._triggers is not None:
            try:
                triggers = dict(self._triggers() or {})
            except Exception:
                triggers = {}          # never let a dead accessor block the window
        for name, label in self.portal_effective.items():
            label.setText(EFFECTIVE_PREFIX + effective_trigger_text(triggers, name))

    def hotkey_help_text(self) -> str:
        """The detail behind the Hotkeys tab's "?", for this backend."""
        return HOTKEY_HELP.get(self._backend, HOTKEY_HELP["evdev"])

    def _apply_desktop_shortcuts(self) -> bool:
        """Put the saved triggers into the desktop's own store, and say so.

        Only the portal backend has any: on evdev the key is ours to read from
        /dev/input and no desktop is involved. A desktop that keeps its
        shortcuts somewhere we must not touch answers None and is left alone -
        the trigger is still saved, and KDE's own dialog applies it.

        Returns whether the listener now needs to bind again.
        """
        if self._backend != "portal" or not self.portal_edits:
            return False
        try:
            store = self._shortcut_store()
        except Exception as exc:
            log.debug("cannot reach this desktop's shortcut store: %s", exc)
            return False
        if store is None:
            return False
        triggers = {name: edit.text().strip() for name, edit in self.portal_edits.items()}
        try:
            message = store.write(triggers)
        except ShortcutStoreError as exc:
            # A refusal is the store protecting the user's other shortcuts, and
            # the one thing they have to see: it is why the key did not move.
            self.error_label.setText(str(exc))
            return False
        except Exception as exc:
            log.exception("writing the desktop's shortcut store failed")
            self.error_label.setText(f"Could not change this desktop's shortcuts: {exc}")
            return False
        if self.shortcut_note is not None:
            self.shortcut_note.setText(message)
        return True

    def _open_shortcut_settings(self) -> None:
        """Take the user to wherever this desktop really keeps the key.

        Portal version 2 (KDE) has a reconfigure dialog and the listener opens
        it; that is the same call as "Capture key", and it answers with a
        sentence either way. Anything else - GNOME, an older portal, a bind that
        never succeeded - falls through to the desktop's settings app.
        """
        try:
            self._capture_key(self._desktop_answered.emit)
        except Exception as exc:
            log.debug("the portal could not open its shortcut dialog: %s", exc)
            self._launch_shortcut_settings()

    def _on_desktop_answered(self, message: str) -> None:
        if message == DIALOG_MESSAGE:               # the desktop's own dialog is up
            self.shortcut_note.setText(message)
            return
        self._launch_shortcut_settings()

    def _launch_shortcut_settings(self) -> None:
        command = shortcut_settings_command()
        if command is None:
            self.shortcut_note.setText(
                f"No desktop settings app found here - open {SHORTCUT_SETTINGS_PATH} yourself.")
            return
        try:
            _spawn(command)
        except Exception as exc:
            self.shortcut_note.setText(
                f"Could not start {command[0]}: {exc}. Open {SHORTCUT_SETTINGS_PATH} yourself.")
            return
        self.shortcut_note.setText(f"Opened {command[0]}: {SHORTCUT_SETTINGS_PATH}.")

    def _start_capture(self) -> None:
        self.capture_button.setText("Press a key…")
        self._capture_key(self._captured.emit)

    def _on_captured(self, name: str) -> None:
        # The portal backend cannot hand back a key name - the compositor keeps
        # the keystroke - so it answers with a sentence for the user instead.
        if name.startswith(("KEY_", "BTN_")):
            self.hotkey_edit.setText(name)
        else:
            self.error_label.setText(name)
        self.capture_button.setText("Capture key")

    def _carry_over_external_edits(self) -> bool:
        """Keep settings switched from the tray, a hotkey or the CLI since this
        dialog opened.

        The dialog edits a whole-document snapshot, so writing it back would
        otherwise revert them. Each field is carried only when the user did not
        change it here. Returns False if the file cannot be read.
        """
        try:
            on_disk = Config.load(self._cfg.path)
        except ValueError as exc:
            self.error_label.setText(f"{self._cfg.path.name}: {exc}")
            return False
        if not self._active_changed:
            self._carry_over_active_profile(on_disk)
        if not self._language_changed:
            self._carry_over_language(on_disk)
        if not self._inject_mode_changed:
            self._carry_over_inject_mode(on_disk)
        if not self._pill_placement_changed:
            self._carry_over_pill_placement(on_disk)
        return True

    def _carry_over_active_profile(self, on_disk: Config) -> None:
        active = on_disk.get("stt.active")
        if not active or active == self._cfg.get("stt.active"):
            return
        if self._language_changed and self._chosen_for_another_language(on_disk, active):
            return
        # Only if this document actually defines it; otherwise the write would
        # produce a config errors() rejects and the save would be blocked.
        if active in (self._cfg.get("stt.profiles", {}) or {}):
            self._cfg.set("stt.active", active)
            self.active_label.setText(f"Active profile: {active}")

    def _chosen_for_another_language(self, on_disk: Config, active: str) -> bool:
        """Whether `active` is the profile the *file's* language selected, while
        this dialog is about to write a different language.

        Carrying it over then pairs the Swedish model with English: the switch
        that wrote the two wrote them as a pair, and only half of it would
        survive here. Asked only when the user picked a language in this dialog -
        otherwise the pair is carried over whole, language included.
        """
        on_disk_language = str(on_disk.get("general.language", "") or "").strip().lower()
        if on_disk.profile_for_language(on_disk_language) != active:
            return False
        return str(self._cfg.get("general.language", "") or "").strip().lower() != on_disk_language

    def _carry_over_language(self, on_disk: Config) -> None:
        language = on_disk.get("general.language")
        if not language or language == self._cfg.get("general.language"):
            return
        if not is_language_code(language):     # same guard: never write a bad value
            return
        self._cfg.set("general.language", language)
        index = self.language_combo.findData(language)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)   # show what was really saved
        self._language_changed = False         # that was us, not the user

    def _carry_over_inject_mode(self, on_disk: Config) -> None:
        """Same as the language above: inject.mode can be changed from the CLI
        while this dialog holds its snapshot."""
        mode = on_disk.get("inject.mode")
        if not mode or mode == self._cfg.get("inject.mode"):
            return
        if mode not in INJECT_MODES:           # never write a value errors() rejects
            return
        self._cfg.set("inject.mode", mode)
        index = self.inject_mode_combo.findData(mode)
        if index >= 0:
            self.inject_mode_combo.setCurrentIndex(index)   # show what was really saved
        self._inject_mode_changed = False      # that was us, not the user

    def _carry_over_pill_placement(self, on_disk: Config) -> None:
        """The same as the language and the mode above: the pill can be moved by
        a hand edit and a `voice reload` while this window holds its snapshot.

        Read through overlay_placement(), so what is carried over is a placement
        the config accepts rather than whatever spelling the file happened to use.
        """
        placement = on_disk.overlay_placement()
        if placement == self._cfg.overlay_placement():
            return
        position, margin_x, margin_y = placement
        self._cfg.set("ui.overlay_position", position)
        self._cfg.set("ui.overlay_margin_x", margin_x)
        self._cfg.set("ui.overlay_margin_y", margin_y)
        self._show_pill_placement(*placement)        # show what was really saved
        self._pill_placement_changed = False         # that was us, not the user

    def _chosen_language_profiles(self) -> dict[str, str]:
        """The map as this dialog would save it: what the file says, with the rows
        the table actually shows overlaid.

        The table has a row per general.languages entry only, so a mapping for a
        language it cannot show - `de`, or `auto`, which the Language combo does
        offer - is carried through untouched instead of being dropped by a save
        that had nothing to do with it.
        """
        # Only entries that still name a real profile: a dangling one for a
        # language with no row is a config error errors() rejects, and no row
        # means no way to clear it - it would block every save from here.
        profiles = self._cfg.get("stt.profiles", {}) or {}
        mapping = {code: name for code, name in self._cfg.language_profiles().items()
                   if name in profiles}
        for code, combo in self.language_profile_combos.items():
            chosen = combo.currentData()
            if chosen:
                mapping[code.strip().lower()] = chosen
            else:
                mapping.pop(code.strip().lower(), None)     # "(keep current)"
        return mapping

    def _save_language_profiles(self) -> None:
        """Write the map, then apply it when the language was picked here.

        The table is only written when it says something, or when the file
        already had a map: an owner who ignores the feature keeps the commented
        example the default config ships.
        """
        mapping = self._chosen_language_profiles()
        if mapping or (self._cfg.get("general.language_profiles") or {}):
            self._cfg.set("general.language_profiles", mapping)
        if not self._language_changed or self._active_changed:
            # Nothing to follow (the language came from the file or elsewhere),
            # or "Use this profile" was pressed and that choice wins.
            return
        name = mapping.get(str(self.language_combo.currentData()).strip().lower())
        if not name or name not in (self._cfg.get("stt.profiles", {}) or {}):
            return
        self._cfg.set("stt.active", name)
        self.active_label.setText(f"Active profile: {name}")
        # This dialog decided the active profile, so the on-disk value must not
        # be carried back over it in _carry_over_external_edits.
        self._active_changed = True

    def _save(self) -> None:
        c = self._cfg
        for field, edit in (("hotkeys.dictate", self.hotkey_edit), ("hotkeys.recall", self.recall_edit), ("hotkeys.cancel", self.cancel_edit)):
            try:
                parse_keyspec(edit.text())
            except ValueError as exc:
                self.error_label.setText(f"{field}: {exc}")
                return
            c.set(field, edit.text().strip())
        for name, edit in self.portal_edits.items():
            c.set(f"hotkeys.portal_{name}", edit.text().strip())
        c.set("general.language", self.language_combo.currentData())
        c.set("general.notifications", self.notifications_combo.currentText() == "on")
        c.set("inject.mode", self.inject_mode_combo.currentData())
        c.set("hotkeys.dictate_mode", self.mode_combo.currentText())
        c.set("audio.device", self.device_combo.currentData() or "")
        c.set("audio.max_seconds", self.max_seconds.value())
        position, margin_x, margin_y = self._chosen_pill_placement()
        c.set("ui.overlay_position", position)
        c.set("ui.overlay_margin_x", margin_x)
        c.set("ui.overlay_margin_y", margin_y)
        if not self._commit_profile_form():
            return
        rules = []
        for r in range(self.replacements_table.rowCount()):
            cells = [self.replacements_table.item(r, col) for col in range(3)]
            src, dst, flags = [(x.text() if x else "").strip() for x in cells]
            if src:
                rules.append([src, dst, flags] if flags else [src, dst])
        c.set("dictionary.replacements", rules)
        self._save_language_profiles()
        if not self._carry_over_external_edits():
            return
        errs = c.errors()
        if errs:
            self.error_label.setText("; ".join(errs))
            return
        c.save()
        self.error_label.setText("")
        # After the file, and only after it: the desktop's store is the other
        # half of a portal trigger, and a rebind to a key the config does not
        # hold is a key that disappears on the next reload.
        rebind = self._apply_desktop_shortcuts()
        # Save leaves the window open, so the "the user decided this here" flags
        # must not outlive the save they belong to: a second language switch has
        # to move its profile too, and an external switch made after this save
        # still has to be carried over by the next one.
        self._active_changed = False
        self._language_changed = False
        self._inject_mode_changed = False
        self._pill_placement_changed = False
        if rebind:
            # Before `saved`: the daemon reloads and rebinds in one pass, so the
            # desktop's permission dialog cannot be asked for twice.
            self.shortcuts_rebound.emit()
        self.saved.emit()
