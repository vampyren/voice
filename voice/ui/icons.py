"""Tray icons drawn at runtime so no asset files are needed."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

_COLORS = {
    "idle": ("#d8dee9", None),
    "recording": ("#e5484d", None),
    "transcribing": ("#d8dee9", "#f5a524"),
    "injecting": ("#d8dee9", "#3b82f6"),
    "error": ("#f97316", None),
}


def pixmap_for(state: str, size: int = 64) -> QPixmap:
    fill, ring = _COLORS.get(state, _COLORS["idle"])
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size
    if ring:
        p.setPen(QPen(QColor(ring), s * 0.08))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(s * 0.08, s * 0.08, s * 0.84, s * 0.84))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(fill))
    p.drawRoundedRect(QRectF(s * 0.36, s * 0.16, s * 0.28, s * 0.44), s * 0.14, s * 0.14)   # capsule
    p.setPen(QPen(QColor(fill), s * 0.07))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(s * 0.26, s * 0.30, s * 0.48, s * 0.42), 200 * 16, 140 * 16)            # cradle
    p.drawLine(int(s * 0.5), int(s * 0.72), int(s * 0.5), int(s * 0.84))                      # stem
    p.drawLine(int(s * 0.38), int(s * 0.84), int(s * 0.62), int(s * 0.84))                    # base
    if state == "error":
        p.setPen(QPen(QColor("#111111"), s * 0.06))
        p.drawLine(int(s * 0.5), int(s * 0.24), int(s * 0.5), int(s * 0.46))
        p.drawPoint(int(s * 0.5), int(s * 0.54))
    p.end()
    return pm


def icon_for(state: str, size: int = 64) -> QIcon:
    return QIcon(pixmap_for(state, size))
