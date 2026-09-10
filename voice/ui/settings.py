"""Settings dialog: edits config.toml through Config so comments survive."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from voice.audio.capture import Source
from voice.config import Config, is_language_code
from voice.hotkey.keyspec import parse_keyspec

PROFILE_TEMPLATES: dict[str, dict] = {
    "openai": {"backend": "openai_compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-transcribe", "api_key": "", "prompt": ""},
    "groq": {"backend": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "model": "whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "mistral": {"backend": "openai_compatible", "base_url": "https://api.mistral.ai/v1", "model": "voxtral-mini-latest", "api_key": "", "prompt": ""},
    "openrouter": {"backend": "openai_compatible", "base_url": "https://openrouter.ai/api/v1", "model": "openai/whisper-large-v3-turbo", "api_key": "", "prompt": ""},
    "together": {"backend": "openai_compatible", "base_url": "https://api.together.xyz/v1", "model": "openai/whisper-large-v3", "api_key": "", "prompt": ""},
    "local-swedish": {"backend": "local", "model": "KBLab/kb-whisper-large", "device": "cuda", "compute_type": "float16", "beam_size": 5, "prompt": ""},
}
_LOCAL_FIELDS = ["model", "device", "compute_type", "beam_size", "prompt"]
_CLOUD_FIELDS = ["base_url", "model", "api_key", "api_key_env", "prompt"]
_LANGUAGES = [("English", "en"), ("Swedish", "sv"), ("Auto-detect", "auto")]
#: The "leave stt.active alone for this language" row of the profile table.
KEEP_CURRENT = "(keep current)"
#: The portal shortcuts, in the order they are shown, with their labels.
PORTAL_TRIGGERS = [("dictate", "Dictate"), ("recall", "Recall last"),
                   ("cancel", "Cancel recording"), ("language_toggle", "Switch language")]
HOTKEY_HINTS = {
    "evdev": "Combinations: type KEY_LEFTMETA+KEY_SPACE. Names are evdev key names.",
    "portal": ("This session binds its shortcuts through the desktop, so there is no key to "
               "capture here - type the trigger instead, in your desktop's syntax: F14, "
               "CTRL+space, CTRL+SHIFT+l. A bare modifier will not bind. Saving asks the "
               "desktop to bind them again, which may show its permission dialog. Your "
               "desktop's own shortcut settings still win over these. The evdev key fields "
               "below apply again if you switch hotkeys.backend to evdev."),
}


class SettingsDialog(QDialog):
    saved = Signal()
    _captured = Signal(str)

    def __init__(self, config: Config, capture_key: Callable[[Callable[[str], None]], None],
                 sources: Callable[[], list[Source]], parent=None, backend: str = "evdev"):
        super().__init__(parent)
        self._backend = backend
        self.setWindowTitle("voice settings")
        self.setMinimumWidth(560)
        # The dialog edits a private Config loaded from the same file: Close simply
        # discards it, and the daemon's live Config is never mutated from here.
        # Save writes to disk and emits `saved`; the daemon reloads from disk.
        self._cfg = Config.load(config.path)
        self._capture_key, self._sources = capture_key, sources
        self._current_profile: str | None = None
        self._active_changed = False       # True once "Use this profile" was pressed
        self._language_changed = False     # True once the user picked a language here
        self._captured.connect(self._on_captured)
        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "General")
        tabs.addTab(self._hotkeys_tab(), "Hotkeys")
        tabs.addTab(self._audio_tab(), "Audio")
        tabs.addTab(self._transcription_tab(), "Transcription")
        tabs.addTab(self._dictionary_tab(), "Dictionary")
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #e5484d")
        self.error_label.setWordWrap(True)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self._load()

    def reload_from_disk(self) -> None:
        """Re-read the file and repopulate every widget, discarding unsaved edits."""
        self._cfg = Config.load(self._cfg.path)
        self._current_profile = None       # so repopulating cannot commit stale form values
        self._active_changed = False
        self._language_changed = False
        self.profile_form = {}
        self.error_label.setText("")
        self._load()

    # -- tabs -----------------------------------------------------------------
    def _general_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.language_combo = QComboBox()
        for label, code in _LANGUAGES:
            self.language_combo.addItem(label, code)
        # Only a change made *here* may overwrite the language on disk: the
        # toggle hotkey and the tray change it while this dialog sits open.
        self.language_combo.currentIndexChanged.connect(self._on_language_picked)
        self.notifications_combo = QComboBox()
        self.notifications_combo.addItems(["on", "off"])
        self.language_profile_table = QTableWidget(0, 2)
        self.language_profile_table.setHorizontalHeaderLabels(["Language", "Profile"])
        self.language_profile_table.horizontalHeader().setStretchLastSection(True)
        self.language_profile_table.verticalHeader().setVisible(False)
        self.language_profile_combos: dict[str, QComboBox] = {}
        hint = QLabel("Switching to one of these languages also activates its profile.")
        hint.setWordWrap(True)
        form.addRow("Language", self.language_combo)
        form.addRow("Notifications", self.notifications_combo)
        form.addRow("Profile per language", self.language_profile_table)
        form.addRow(hint)
        return w

    def _hotkeys_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.hotkey_edit = QLineEdit()
        self.capture_button = QPushButton("Capture key")
        self.capture_button.clicked.connect(self._start_capture)
        row = QHBoxLayout()
        row.addWidget(self.hotkey_edit)
        row.addWidget(self.capture_button)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["hold", "toggle"])
        self.recall_edit = QLineEdit()
        self.cancel_edit = QLineEdit()
        self.portal_edits: dict[str, QLineEdit] = {}
        if self._backend == "portal":
            # The compositor consumes the chord before we see it, so there is
            # nothing to capture: these are the triggers we ask it to bind.
            self.capture_button.setVisible(False)
            for name, label in PORTAL_TRIGGERS:
                edit = QLineEdit()
                self.portal_edits[name] = edit
                form.addRow(f"{label} shortcut", edit)
        self.hotkey_hint = QLabel(HOTKEY_HINTS.get(self._backend, HOTKEY_HINTS["evdev"]))
        self.hotkey_hint.setWordWrap(True)
        form.addRow(self.hotkey_hint)
        form.addRow("Dictate key", row)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Recall last", self.recall_edit)
        form.addRow("Cancel recording", self.cancel_edit)
        return w

    def _audio_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.device_combo = QComboBox()
        self.device_combo.addItem("System default", "")
        for src in self._sources():
            self.device_combo.addItem(src.description + (" (default)" if src.is_default else ""), src.name)
        self.max_seconds = QSpinBox()
        self.max_seconds.setRange(5, 600)
        form.addRow("Microphone", self.device_combo)
        form.addRow("Max seconds", self.max_seconds)
        return w

    def _transcription_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)
        left = QVBoxLayout()
        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._show_profile)
        self.add_profile_combo = QComboBox()
        self.add_profile_combo.addItems(list(PROFILE_TEMPLATES))
        self.add_profile_button = QPushButton("Add from template")
        self.add_profile_button.clicked.connect(self._add_profile)
        self.activate_button = QPushButton("Use this profile")
        self.activate_button.clicked.connect(self._activate_profile)
        left.addWidget(self.profile_list)
        left.addWidget(self.add_profile_combo)
        left.addWidget(self.add_profile_button)
        left.addWidget(self.activate_button)
        self.profile_form: dict[str, QLineEdit] = {}
        self._form_widget = QWidget()
        self._form_layout = QFormLayout(self._form_widget)
        self.active_label = QLabel()
        right = QVBoxLayout()
        right.addWidget(self.active_label)
        right.addWidget(self._form_widget)
        right.addStretch()
        outer.addLayout(left, 1)
        outer.addLayout(right, 2)
        return w

    def _dictionary_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.replacements_table = QTableWidget(0, 3)
        self.replacements_table.setHorizontalHeaderLabels(["Heard", "Replace with", "Flags (icase, regex)"])
        self.replacements_table.horizontalHeader().setStretchLastSection(True)
        add = QPushButton("Add row")
        add.clicked.connect(lambda: self.replacements_table.insertRow(self.replacements_table.rowCount()))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(lambda: self.replacements_table.removeRow(self.replacements_table.currentRow()))
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        layout.addWidget(self.replacements_table)
        layout.addLayout(row)
        return w

    # -- load/save --------------------------------------------------------------
    def _load(self) -> None:
        c = self._cfg
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(c.get("general.language", "en"))))
        self.notifications_combo.setCurrentText("on" if c.get("general.notifications", True) else "off")
        self.hotkey_edit.setText(c.get("hotkeys.dictate", ""))
        self.mode_combo.setCurrentText(c.get("hotkeys.dictate_mode", "hold"))
        self.recall_edit.setText(c.get("hotkeys.recall", ""))
        self.cancel_edit.setText(c.get("hotkeys.cancel", ""))
        for name, edit in self.portal_edits.items():
            edit.setText(c.portal_trigger(name))
        self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(c.get("audio.device", ""))))
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
        self._language_changed = False     # populating the combo is not a user edit

    def _load_language_profiles(self) -> None:
        """One row per general.languages entry, each with the profiles that exist."""
        mapping = self._cfg.language_profiles()
        profiles = list(self._cfg.get("stt.profiles", {}) or {})
        codes = self._cfg.languages()
        table = self.language_profile_table
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
        self._form_layout.addRow("backend", QLabel(str(profile.get("backend", ""))))
        for field in fields:
            edit = QLineEdit(str(profile.get(field, "")))
            if field == "api_key":
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.profile_form[field] = edit
            self._form_layout.addRow(field, edit)

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

    def _activate_profile(self) -> None:
        if self._current_profile:
            self._cfg.set("stt.active", self._current_profile)
            self._active_changed = True
            self.active_label.setText(f"Active profile: {self._current_profile}")

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
        return True

    def _carry_over_active_profile(self, on_disk: Config) -> None:
        active = on_disk.get("stt.active")
        if not active or active == self._cfg.get("stt.active"):
            return
        # Only if this document actually defines it; otherwise the write would
        # produce a config errors() rejects and the save would be blocked.
        if active in (self._cfg.get("stt.profiles", {}) or {}):
            self._cfg.set("stt.active", active)
            self.active_label.setText(f"Active profile: {active}")

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

    def _save_language_profiles(self) -> None:
        """Write the map, then apply it when the language was picked here.

        The table is only written when it says something, or when the file
        already had a map: an owner who ignores the feature keeps the commented
        example the default config ships.
        """
        mapping = {code.strip().lower(): combo.currentData()
                   for code, combo in self.language_profile_combos.items() if combo.currentData()}
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
        c.set("hotkeys.dictate_mode", self.mode_combo.currentText())
        c.set("audio.device", self.device_combo.currentData() or "")
        c.set("audio.max_seconds", self.max_seconds.value())
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
        self.saved.emit()
