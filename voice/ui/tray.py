"""System tray icon (StatusNotifierItem on KDE) with a small menu."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from voice import APP_NAME
from voice.ui.icons import icon_for


class Tray(QObject):
    state_changed = Signal(str, str)

    def __init__(self, on_action: Callable[[str], None]):
        super().__init__()
        self._on_action = on_action
        self.icon = QSystemTrayIcon(icon_for("idle"))
        self.menu = QMenu()
        self._actions: dict[str, QAction] = {}
        for key, label in (("recall", "Recall last dictation"), ("retry", "Retry last recording")):
            self._add(key, label)
        self.menu.addSeparator()
        self.profile_menu = self.menu.addMenu("Transcription profile")
        self._profile_group = QActionGroup(self)
        self._profile_group.setExclusive(True)
        self.menu.addSeparator()
        self._add("settings", "Settings…")
        self._add("quit", "Quit")
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.state_changed.connect(self.set_state)
        self.set_state("idle", "ready")

    def _add(self, key: str, label: str) -> None:
        act = QAction(label, self.menu)
        act.triggered.connect(lambda _=False, k=key: self._on_action(k))
        self.menu.addAction(act)
        self._actions[key] = act

    def action(self, key: str) -> QAction:
        return self._actions[key]

    def _activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._on_action("settings")

    def set_state(self, state: str, detail: str = "") -> None:
        self.icon.setIcon(icon_for(state))
        self.icon.setToolTip(f"{APP_NAME} · {state}" + (f" · {detail}" if detail else ""))

    def set_profiles(self, names: list[str], active: str) -> None:
        for act in list(self.profile_menu.actions()):
            self.profile_menu.removeAction(act)
            self._profile_group.removeAction(act)
        for name in names:
            act = QAction(name, self.profile_menu)
            act.setCheckable(True)
            act.setChecked(name == active)
            act.triggered.connect(lambda _=False, n=name: self._on_action(f"profile:{n}"))
            self._profile_group.addAction(act)
            self.profile_menu.addAction(act)

    def show(self) -> None:
        self.icon.show()
