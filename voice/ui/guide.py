"""A short guide to using voice. It reads nothing and writes nothing.

This was an interactive first-run wizard: it asked where the models should go
and which model each language should use, and wrote the answers. Three review
rounds found eleven defects in it, and every fix introduced more - a guard that
skipped the line below it, a key matched as a substring, a page describing rows
it did not have, a modal dialog racing the IPC socket. None of them were in the
settings those questions map to; all of them were in the asking.

So it asks nothing. Every setting it used to write has a row in the settings
window, which is tested and has none of this machinery around it. What is left
is four pages of prose with Back and Next, which cannot be wrong about the
state of the program because it never looks at it.

Keep it that way. A control here that changes something is how this started.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from voice.ui.settings import MARGIN, ROW_SPACING


@dataclass(frozen=True)
class Page:
    """One step: a heading and a paragraph. Nothing else, deliberately."""

    title: str
    body: str


PAGES = (
    Page("Welcome to voice",
         "voice turns what you say into text and types it into whatever window you "
         "are using - an editor, a chat box, a terminal.\n\n"
         "It runs in the background and shows an icon in your system tray. Right-click "
         "that icon for settings, to switch language, or to quit."),
    Page("Give it a key",
         "voice has no shortcut until your desktop gives it one. Open\n"
         "    Settings → Keyboard → Keyboard Shortcuts\n"
         'and assign a key to "voice".\n\n'
         "The desktop owns that key from then on - changing it there is the way to "
         "change it. `voice status` in a terminal says which key it actually holds."),
    Page("Dictating",
         "Hold the key, speak, let go. A pill appears at the bottom of the screen "
         "while you talk, showing the sound level and how long you have been going.\n\n"
         "Let go and the text is typed where your cursor was. The first time in a "
         "language there is a wait while the speech model downloads - a few gigabytes, "
         "once.\n\n"
         "Nothing is sent anywhere: the model runs on this computer."),
    Page("When something is wrong",
         "Run this in a terminal:\n"
         "    voice doctor\n\n"
         "It checks everything voice needs on this machine and says what to fix. A "
         "line marked (optional) failing does not stop dictation working.\n\n"
         "Everything else - the language, which model, where models are kept, where "
         "the pill sits - is in Settings, from the tray icon."),
)


class Guide(QDialog):
    """Four pages of prose. `voice guide` opens it."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("How to use voice")

        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-size: 16pt; font-weight: bold")
        self.body_label = QLabel()
        self.body_label.setWordWrap(True)
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self._back)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN * 2, MARGIN * 2, MARGIN * 2, MARGIN * 2)
        layout.setSpacing(ROW_SPACING)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label, 1)
        layout.addLayout(buttons)
        self.resize(560, 380)
        self._page = 0
        self._show_page(0)

    @property
    def page_index(self) -> int:
        return self._page

    def _show_page(self, index: int) -> None:
        self._page = index
        page = PAGES[index]
        self.title_label.setText(page.title)
        self.body_label.setText(page.body)
        self.back_button.setEnabled(index > 0)
        # The last page ends it, so Next is never a button that does nothing.
        self.next_button.setText("Done" if index == len(PAGES) - 1 else "Next")

    def _back(self) -> None:
        if self._page > 0:
            self._show_page(self._page - 1)

    def _next(self) -> None:
        if self._page < len(PAGES) - 1:
            self._show_page(self._page + 1)
        else:
            self.accept()
