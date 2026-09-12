"""The first-run wizard: three questions, asked once.

Everything it asks is also in the settings window, and that is the point - the
two decisions that matter most on a new machine are the ones nobody finds
until something has already gone wrong. Several gigabytes land in a directory
chosen by a library default, and each language quietly picks a model. Both are
easy to answer up front and tedious to undo afterwards.

It writes nothing until Finish. Escaping leaves the config untouched *and*
leaves `general.setup_complete` false, so the next start asks again: a wizard
escaped from is not a wizard answered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QStackedWidget, QVBoxLayout,
                               QWidget)

from voice.config import Config
from voice.ui.settings import (MARGIN, MODEL_CHOICES, MODEL_NOTES, RECOMMENDED_MODELS,
                               ROW_SPACING, language_name)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Page:
    """One step: a heading, a paragraph, and whatever widgets it needs."""

    key: str
    title: str
    body: str


PAGES = (
    Page("welcome", "Welcome to voice",
         "voice turns what you say into text and types it into whatever window you "
         "are using. Three things to settle before the first time, all of them "
         "changeable later in Settings.\n\n"
         "Nothing is written until you press Finish."),
    Page("models", "Where to keep the speech models",
         "The models are a few gigabytes each and are downloaded the first time you "
         "dictate in a language - never during installation. They go to the shared "
         "Hugging Face cache unless you choose somewhere else.\n\n"
         "Leave it empty for ~/.cache/huggingface, which is where any other program "
         "on this computer keeps its models too."),
    Page("quality", "Which model for each language",
         "One model rarely wins in two languages. Bigger is more accurate and "
         "slower; the Swedish models are trained on Swedish speech rather than on a "
         "hundred languages at once.\n\n"
         "The recommended pair is already selected. Hover a name to see what it is."),
    Page("done", "One last thing: the key",
         "voice has no shortcut until your desktop gives it one. Open\n"
         "    Settings → Keyboard → Keyboard Shortcuts\n"
         "and assign a key to \"voice\". The desktop owns that key from then on.\n\n"
         "Then run `voice doctor` in a terminal: it checks this machine for "
         "everything voice needs and says what to fix."),
)


class SetupWizard(QDialog):
    """Asked once on a new install; `voice setup` runs it again."""

    def __init__(self, config: Config, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = config
        self.setWindowTitle("Set up voice")
        self.setModal(True)

        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-size: 16pt; font-weight: bold")
        self.body_label = QLabel()
        self.body_label.setWordWrap(True)
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #f97316")

        self.pages = QStackedWidget()
        self.model_dir_edit = QLineEdit()
        self.model_combos: dict[str, QComboBox] = {}
        for page in PAGES:
            self.pages.addWidget(self._build(page))

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self._back)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next)
        self.skip_button = QPushButton("Not now")
        self.skip_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.skip_button)
        buttons.addStretch()
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN * 2, MARGIN * 2, MARGIN * 2, MARGIN * 2)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)
        self.resize(620, 460)
        self._show_page(0)

    # -- pages ---------------------------------------------------------------

    @property
    def page_index(self) -> int:
        return self.pages.currentIndex()

    def _build(self, page: Page) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)
        if page.key == "models":
            layout.addLayout(self._model_dir_row())
        elif page.key == "quality":
            layout.addLayout(self._model_rows())
        layout.addStretch()
        return w

    def _model_dir_row(self) -> QHBoxLayout:
        self.model_dir_edit.setText(str(self._cfg.get("stt.model_dir", "") or ""))
        self.model_dir_edit.setPlaceholderText(
            "~/.cache/huggingface — the shared cache")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_model_dir)
        row = QHBoxLayout()
        row.setSpacing(ROW_SPACING // 2)
        row.addWidget(QLabel("Folder"))
        row.addWidget(self.model_dir_edit, 1)
        row.addWidget(browse)
        return row

    def _model_rows(self) -> QFormLayout:
        """One row per language that actually has a profile behind it."""
        form = QFormLayout()
        form.setHorizontalSpacing(ROW_SPACING)
        form.setVerticalSpacing(ROW_SPACING)
        profiles = self._cfg.get("stt.profiles", {}) or {}
        for code in self._cfg.languages():
            name = self._cfg.profile_for_language(code)
            profile = profiles.get(name) if name else None
            # A language with nothing to point at gets no row: writing a model
            # into a profile table that does not exist would only invent one.
            if not isinstance(profile, dict) or profile.get("backend") != "local":
                continue
            combo = self._model_combo(str(profile.get("model", "")))
            self.model_combos[code] = combo
            form.addRow(language_name(code), combo)
        return form

    def _model_combo(self, current: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for index, name in enumerate(MODEL_CHOICES):
            combo.addItem(name)
            note = MODEL_NOTES[name]
            if name in RECOMMENDED_MODELS:
                note = f"Recommended. {note}"
                font = combo.font()
                font.setBold(True)
                combo.setItemData(index, font, Qt.ItemDataRole.FontRole)
            combo.setItemData(index, note, Qt.ItemDataRole.ToolTipRole)
        combo.setCurrentText(current)
        return combo

    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        page = PAGES[index]
        self.title_label.setText(page.title)
        self.body_label.setText(page.body)
        self.error_label.setText("")
        self.back_button.setEnabled(index > 0)
        self.next_button.setText("Finish" if index == len(PAGES) - 1 else "Next")

    def _back(self) -> None:
        if self.page_index > 0:
            self._show_page(self.page_index - 1)

    def _next(self) -> None:
        if self.page_index < len(PAGES) - 1:
            self._show_page(self.page_index + 1)
        else:
            self.finish()

    def _pick_model_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Where to keep the downloaded models", self.model_dir_edit.text())
        if chosen:
            self.model_dir_edit.setText(chosen)

    # -- the answers ---------------------------------------------------------

    def finish(self) -> bool:
        """Write the answers. False - and nothing written - if they do not hold."""
        self._cfg.set("stt.model_dir", self.model_dir_edit.text().strip())
        for code, combo in self.model_combos.items():
            name = self._cfg.profile_for_language(code)
            chosen = combo.currentText().strip()
            if name and chosen:
                self._cfg.set(f"stt.profiles.{name}.model", chosen)
        self._cfg.set("general.setup_complete", True)
        errs = self._cfg.errors()
        if errs:
            # Reloaded, not patched back: the config object is the daemon's, and
            # leaving half-applied answers on it would outlive this dialog.
            self._cfg.reload()
            self.error_label.setText("; ".join(errs))
            return False
        self._cfg.save()
        self.accept()
        return True
