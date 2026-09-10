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
        self._profile_group = self._exclusive_group()
        self.language_menu = self.menu.addMenu("Language")
        self._language_group = self._exclusive_group()
        self.menu.addSeparator()
        self._add("settings", "Settings…")
        self._add("quit", "Quit")
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.state_changed.connect(self.set_state)
        self.set_state("idle", "ready")

    def _exclusive_group(self) -> QActionGroup:
        group = QActionGroup(self)
        group.setExclusive(True)
        return group

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
        self._set_radio(self.profile_menu, self._profile_group, "profile",
                        [(name, name) for name in names], active)

    def set_languages(self, codes: list[str], active: str) -> None:
        """The language cycle, as a radio list. Codes are shown upper-case."""
        self._set_radio(self.language_menu, self._language_group, "language",
                        [(code, str(code).upper()) for code in codes], active)

    def _set_radio(self, menu: QMenu, group: QActionGroup, prefix: str,
                   entries: list[tuple[str, str]], active: str) -> None:
        """Rebuild `menu` as one exclusive radio entry per (value, label).

        Rebuilt rather than updated: the list itself changes when the config is
        reloaded, and a stale entry would send a command for something gone.
        """
        for act in list(menu.actions()):
            menu.removeAction(act)
            group.removeAction(act)
        for value, label in entries:
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(value == active)
            act.triggered.connect(lambda _=False, v=value: self._on_action(f"{prefix}:{v}"))
            group.addAction(act)
            menu.addAction(act)

    def show(self) -> None:
        self.icon.show()
