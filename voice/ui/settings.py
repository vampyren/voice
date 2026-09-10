"""Settings dialog: edits config.toml through Config so comments survive."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from voice.audio.capture import Source
from voice.config import Config
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


class SettingsDialog(QDialog):
    saved = Signal()
    _captured = Signal(str)

    def __init__(self, config: Config, capture_key: Callable[[Callable[[str], None]], None],
                 sources: Callable[[], list[Source]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("voice settings")
        self.setMinimumWidth(560)
        self._cfg, self._capture_key, self._sources = config, capture_key, sources
        self._current_profile: str | None = None
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

    # -- tabs -----------------------------------------------------------------
    def _general_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.language_combo = QComboBox()
        for label, code in _LANGUAGES:
            self.language_combo.addItem(label, code)
        self.notifications_combo = QComboBox()
        self.notifications_combo.addItems(["on", "off"])
        form.addRow("Language", self.language_combo)
        form.addRow("Notifications", self.notifications_combo)
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
        form.addRow("Dictate key", row)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Recall last", self.recall_edit)
        form.addRow("Cancel recording", self.cancel_edit)
        form.addRow(QLabel("Combinations: type KEY_LEFTMETA+KEY_SPACE. Names are evdev key names."))
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

    def _commit_profile_form(self) -> None:
        if not self._current_profile or not self.profile_form:
            return
        for field, edit in self.profile_form.items():
            value: object = edit.text()
            if field == "beam_size":
                value = int(value or 5)
            self._cfg.set(f"stt.profiles.{self._current_profile}.{field}", value)

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
            self.active_label.setText(f"Active profile: {self._current_profile}")

    def _start_capture(self) -> None:
        self.capture_button.setText("Press a key…")
        self._capture_key(self._captured.emit)

    def _on_captured(self, name: str) -> None:
        self.hotkey_edit.setText(name)
        self.capture_button.setText("Capture key")

    def _save(self) -> None:
        c = self._cfg
        for field, edit in (("hotkeys.dictate", self.hotkey_edit), ("hotkeys.recall", self.recall_edit), ("hotkeys.cancel", self.cancel_edit)):
            try:
                parse_keyspec(edit.text())
            except ValueError as exc:
                self.error_label.setText(f"{field}: {exc}")
                return
            c.set(field, edit.text().strip())
        c.set("general.language", self.language_combo.currentData())
        c.set("general.notifications", self.notifications_combo.currentText() == "on")
        c.set("hotkeys.dictate_mode", self.mode_combo.currentText())
        c.set("audio.device", self.device_combo.currentData() or "")
        c.set("audio.max_seconds", self.max_seconds.value())
        self._commit_profile_form()
        rules = []
        for r in range(self.replacements_table.rowCount()):
            cells = [self.replacements_table.item(r, col) for col in range(3)]
            src, dst, flags = [(x.text() if x else "").strip() for x in cells]
            if src:
                rules.append([src, dst, flags] if flags else [src, dst])
        c.set("dictionary.replacements", rules)
        errs = c.errors()
        if errs:
            self.error_label.setText("; ".join(errs))
            return
        c.save()
        self.error_label.setText("")
        self.saved.emit()
