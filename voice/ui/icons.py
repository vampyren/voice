"""Tray icons drawn at runtime so no asset files are needed.

The panel scales the whole pixmap into its icon slot, so every pixel of margin
left inside the square is margin the glyph never gets back - which is why the
microphone read as tiny beside its stock neighbours. The glyph is therefore
laid out in fractions of a *box* rather than at fixed offsets: capsule, cradle,
stem and base all scale with it, so the shape stays balanced whatever size the
panel asks for. The box is the whole icon less a hair of padding, and is inset
for the two states that draw a ring, so the ring never crosses the glyph.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

_COLORS = {
    "idle": ("#d8dee9", None),
    "recording": ("#e5484d", None),
    "transcribing": ("#d8dee9", "#f5a524"),
    "injecting": ("#d8dee9", "#3b82f6"),
    "error": ("#f97316", None),
}

#: Padding around the glyph, as a fraction of the icon: a hair, the way a stock
#: panel icon leaves one, rather than the quarter of the square this left
#: before. A microphone is taller than it is wide, so the two differ - what
#: matters is that the drawn pixels reach within a tenth of every edge.
PAD_X, PAD_Y = 0.09, 0.05

#: The ring, in fractions of the icon: its ellipse and its stroke. Together they
#: put its outer edge at 0.04 and its inner edge at 0.16, so it never reaches
#: the pixmap's own edge and is never clipped into an arc.
RING_BOX = (0.08, 0.08, 0.84, 0.84)
RING_STROKE = 0.08

#: How much of its box the glyph keeps when a ring is drawn round it, chosen so
#: that its furthest corner - the end of the base bar - still clears the ring's
#: 0.38 inner radius.
RING_SCALE = 0.75

# -- the microphone, in fractions of the box it is drawn into -----------------
#: The stroke shared by cradle, stem and base, as a fraction of the icon.
STROKE = 0.085
#: The capsule: a fully rounded rect hanging from the top of the box, half again
#: as tall as it is wide once the box's own proportions are applied.
CAPSULE = (0.323, 0.0, 0.354, 0.556)
#: The cradle, drawn as the bottom half of an ellipse - 180 to 360 degrees, so
#: its arms end vertical at their widest point, which is what lets the glyph
#: reach both sides of its box. The rect is already inset by half a stroke.
CRADLE = (0.055, 0.261, 0.890, 0.544)
CRADLE_FROM, CRADLE_SPAN = 180, 180
#: The stand: a stem down from the cradle and a bar across the bottom.
STEM = (0.5, 0.806, 0.5, 0.95)
BASE = (0.293, 0.95, 0.707, 0.95)
#: The error state's exclamation, drawn over the capsule.
MARK = (0.5, 0.11, 0.5, 0.32)
MARK_DOT = (0.5, 0.43)
MARK_STROKE = 0.075


def _at(box: QRectF, x: float, y: float) -> QPointF:
    """A point given in fractions of `box`, as a point on the pixmap."""
    return QPointF(box.x() + x * box.width(), box.y() + y * box.height())


def _rect(box: QRectF, x: float, y: float, w: float, h: float) -> QRectF:
    return QRectF(box.x() + x * box.width(), box.y() + y * box.height(),
                  w * box.width(), h * box.height())


def _box(size: float, scale: float = 1.0) -> QRectF:
    """The glyph's box: the padded icon, shrunk about its centre by `scale`."""
    w, h = (1 - 2 * PAD_X) * scale, (1 - 2 * PAD_Y) * scale
    return QRectF((1 - w) / 2 * size, (1 - h) / 2 * size, w * size, h * size)


def _microphone(p: QPainter, colour: QColor, box: QRectF, stroke: float) -> None:
    """Draw the microphone filling `box`: capsule, cradle, stem, base."""
    x, y, w, h = CAPSULE
    radius = w * box.width() / 2                     # round in pixels, not in x
    p.setPen(Qt.NoPen)
    p.setBrush(colour)
    p.drawRoundedRect(_rect(box, x, y, w, h), radius, radius)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(colour, stroke, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(_rect(box, *CRADLE), CRADLE_FROM * 16, CRADLE_SPAN * 16)
    p.drawLine(_at(box, STEM[0], STEM[1]), _at(box, STEM[2], STEM[3]))
    p.drawLine(_at(box, BASE[0], BASE[1]), _at(box, BASE[2], BASE[3]))


def pixmap_for(state: str, size: int = 64) -> QPixmap:
    fill, ring = _COLORS.get(state, _COLORS["idle"])
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    if ring:
        p.setPen(QPen(QColor(ring), RING_STROKE * s))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(*(v * s for v in RING_BOX)))
    scale = RING_SCALE if ring else 1.0
    box = _box(s, scale)
    _microphone(p, QColor(fill), box, STROKE * s * scale)
    if state == "error":
        p.setPen(QPen(QColor("#111111"), MARK_STROKE * s * scale, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(_at(box, MARK[0], MARK[1]), _at(box, MARK[2], MARK[3]))
        p.drawPoint(_at(box, *MARK_DOT))
    p.end()
    return pm


def icon_for(state: str, size: int = 64) -> QIcon:
    return QIcon(pixmap_for(state, size))
