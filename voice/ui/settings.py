"""Settings dialog: edits config.toml through Config so comments survive."""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Callable, Iterable

from html import escape

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QPushButton, QSizePolicy, QSpinBox,
                               QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
                               QToolTip, QVBoxLayout, QWidget)

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
HELP_STYLE = f"""
QToolButton {{
    border: 1px solid palette(mid); border-radius: {HELP_SIZE // 2}px;
    color: palette(mid); font-weight: bold; padding: 0px;
}}
QToolButton:hover {{ border-color: palette(highlight); color: palette(highlight); }}
"""
#: Dim, one line, under the control it belongs to.
DIALOG_STYLE = """
QLabel[caption="true"] { color: palette(mid); }
QGroupBox { font-weight: bold; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 2px; padding: 0 3px; }
"""
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


def _wrapped(text: str) -> QLabel:
    """A label that wraps: these hold sentences, not words."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _caption(text: str = "") -> QLabel:
    """A dim one-line note under a control. Never a paragraph - see HELP."""
    label = _wrapped(text)
    label.setProperty("caption", True)
    return label


class HelpButton(QToolButton):
    """The circled "?" beside a control: one click, one paragraph of detail.

    The tabs used to carry that paragraph inline, which is what made the window
    read as a wall of text. The words are unchanged; they are simply not on
    screen until they are asked for - and they are the tooltip as well, so
    hovering answers the question without a click.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.help_text = text
        self.setText("?")
        self.setAccessibleName("More information")
        self.setToolTip(_as_rich(text))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(HELP_SIZE, HELP_SIZE)
        self.setStyleSheet(HELP_STYLE)
        self.clicked.connect(self._show)

    def _show(self) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(0, self.height())), _as_rich(self.help_text),
                          self)


def _as_rich(text: str) -> str:
    """Help text as the small rich-text block a tooltip will wrap for us."""
    body = "<br><br>".join(escape(part.strip()) for part in text.split("\n\n") if part.strip())
    return f"<div style='max-width:{HELP_WIDTH}px'>{body}</div>"


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
        self.error_label = _wrapped("")
        self.error_label.setStyleSheet("color: #e5484d")
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

    def reload_from_disk(self) -> None:
        """Re-read the file and repopulate every widget, discarding unsaved edits."""
        self._cfg = Config.load(self._cfg.path)
        self._current_profile = None       # so repopulating cannot commit stale form values
        self._active_changed = False
        self._language_changed = False
        self._inject_mode_changed = False
        self._pill_placement_changed = False
        self.preview_timer.stop()          # an abandoned gesture shows nothing
        self.preview_note.setText("")
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
        # under two languages or a scrollbar under four.
        rows = sum(table.rowHeight(r) for r in range(table.rowCount()))
        table.setFixedHeight(table.horizontalHeader().height() + rows + 2 * table.frameWidth())

    def _on_language_picked(self, index: int) -> None:
        self._language_changed = True

    def _on_inject_mode_picked(self, index: int) -> None:
        self._inject_mode_changed = True

    def _on_pill_placement_picked(self) -> None:
        """The owner dragged or nudged the pill in the preview."""
        self._pill_placement_changed = True
        self.pill_placement_label.setText(placement_summary(*self.pill_placer.placement()))
        if self._preview_pill is not None:
            self.preview_timer.start(PREVIEW_DELAY_MS)

    def _request_pill_preview(self) -> None:
        """Ask the daemon to put the real pill where the placer says, briefly.

        Nothing here is a settings error: a daemon that refuses (no pill, or a
        dictation in flight) has a reason worth reading, and a daemon that has
        gone away is worth saying once - but neither belongs in the red label
        beside Save, and neither may stop the placement being saved.
        """
        if self._preview_pill is None:
            return
        try:
            reply = self._preview_pill(*self.pill_placer.placement()) or {}
        except Exception as exc:
            log.debug("the pill preview could not be started: %s", exc)
            self.preview_note.setText(f"Cannot show it here: {exc}")
            return
        self.preview_note.setText(PREVIEW_SHOWING if reply.get("ok")
                                  else str(reply.get("error") or "Cannot show it here."))

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
