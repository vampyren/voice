"""The settings window's pill placer: this screen in miniature, pill included.

The real pill cannot be dragged - it is deliberately input-transparent, which is
what keeps it from taking the keyboard mid-dictation, and a layer-shell surface
has anchors rather than a position. So the dragging happens here instead: the
owner moves a scale model of the pill around a scale model of the screen, and
the drop is written back as one of the nine anchors plus `ui.overlay_margin_x` /
`ui.overlay_margin_y`, which is what the compositor can actually be asked for.

Nothing here draws the pill through `voice.ui.overlay_draw`: that renderer is
cairo, which is the *helper's* dependency and not the daemon's. A capsule of the
right proportion is what a preview needs anyway.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from voice.ui.placement import (DEFAULT_MARGIN_X, DEFAULT_MARGIN_Y, DEFAULT_POSITION,
                                pill_origin, placement_at)

#: The pill's real size in screen pixels: the helper's default height, and about
#: what `overlay_draw.natural_width` gives a recording pill. The preview is to
#: scale, so this only has to be right enough to aim with.
PILL_SIZE = (280, 44)
#: The preview's height in widget pixels; the width follows the screen's shape.
PREVIEW_H = 150
#: How near an anchor a drop must land to take it exactly, in widget pixels.
SNAP_PX = 6
#: Arrow keys, in screen pixels; Shift multiplies by ten.
NUDGE_PX = 1
NUDGE_BIG_PX = 10
#: When Qt cannot tell us about a display (a headless run, an empty geometry).
DEFAULT_SCREEN = (1920, 1080)

_SCREEN_BG = QColor("#1b1d22")
_SCREEN_EDGE = QColor("#5b6472")
_GUIDE = QColor(255, 255, 255, 40)
_PILL_BG = QColor("#0f1014")
_PILL_EDGE = QColor(255, 255, 255, 60)
_DOT = QColor("#f0616d")
_BAR = QColor("#7aa2f7")
_FOCUS = QColor("#7aa2f7")


class PillPlacer(QWidget):
    """A screen in miniature. Drag the pill; arrow keys nudge it by a pixel."""

    #: The owner moved the pill. Not emitted by `set_placement`: showing what the
    #: file holds is not a choice the owner made in this window.
    placement_changed = Signal()

    def __init__(self, screen: tuple[int, int] | None = None, parent=None):
        super().__init__(parent)
        self._screen = _sane_screen(screen if screen is not None else _primary_screen())
        self._position = DEFAULT_POSITION
        self._margin_x, self._margin_y = DEFAULT_MARGIN_X, DEFAULT_MARGIN_Y
        #: Where the pill is being dragged to, in screen pixels, while a drag is
        #: in flight; None the rest of the time, when the placement decides.
        self._dragging: tuple[int, int] | None = None
        self._grab = (0, 0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag the pill to where you want it. Arrow keys nudge it "
                        "by a pixel, Shift+arrow by ten.")

    # -- the placement ----------------------------------------------------

    def screen_size(self) -> tuple[int, int]:
        return self._screen

    def placement(self) -> tuple[str, int, int]:
        """Where the pill sits: (position, margin_x, margin_y)."""
        return (self._position, self._margin_x, self._margin_y)

    def set_placement(self, position: str, margin_x: int, margin_y: int) -> None:
        """Show a placement. Silent: this is the file talking, not the owner."""
        self._position, self._margin_x, self._margin_y = position, int(margin_x), int(margin_y)
        self._dragging = None
        self.update()

    def pill_origin(self) -> tuple[int, int]:
        """The pill's top-left corner in screen pixels, drag included."""
        if self._dragging is not None:
            return self._dragging
        return pill_origin(self._position, self._margin_x, self._margin_y,
                           self._screen, PILL_SIZE)

    def _commit(self, origin: tuple[int, int], snap: int) -> None:
        placement = placement_at(*origin, self._screen, PILL_SIZE, snap=snap)
        self._dragging = None
        if placement == self.placement():
            self.update()
            return
        self._position, self._margin_x, self._margin_y = placement
        self.update()
        self.placement_changed.emit()

    # -- geometry ---------------------------------------------------------

    def sizeHint(self) -> QSize:
        width, height = self._screen
        return QSize(max(1, round(PREVIEW_H * width / height)), PREVIEW_H)

    def _screen_rect(self) -> QRectF:
        """The screen, drawn as large as fits while keeping its own shape."""
        width, height = self._screen
        scale = min(self.width() / width, self.height() / height)
        w, h = width * scale, height * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _scale(self) -> float:
        """Widget pixels per screen pixel."""
        return self._screen_rect().width() / self._screen[0]

    def _to_screen(self, point) -> tuple[float, float]:
        rect, scale = self._screen_rect(), self._scale()
        return ((point.x() - rect.x()) / scale, (point.y() - rect.y()) / scale)

    def _pill_rect(self) -> QRectF:
        rect, scale = self._screen_rect(), self._scale()
        x, y = self.pill_origin()
        return QRectF(rect.x() + x * scale, rect.y() + y * scale,
                      PILL_SIZE[0] * scale, PILL_SIZE[1] * scale)

    def _clamped(self, x: float, y: float) -> tuple[int, int]:
        """A corner the pill can actually have: never off its own screen."""
        return (int(round(min(max(x, 0), self._screen[0] - PILL_SIZE[0]))),
                int(round(min(max(y, 0), self._screen[1] - PILL_SIZE[1]))))

    # -- dragging ---------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        x, y = self._to_screen(event.position())
        origin = self.pill_origin()
        inside = (origin[0] <= x <= origin[0] + PILL_SIZE[0]
                  and origin[1] <= y <= origin[1] + PILL_SIZE[1])
        if inside:
            self._grab = (x - origin[0], y - origin[1])
        else:
            # Clicking the empty screen picks the pill up by its middle: aiming
            # at a 44-pixel capsule in a thumbnail is not the point of this.
            self._grab = (PILL_SIZE[0] / 2, PILL_SIZE[1] / 2)
        self._dragging = self._clamped(x - self._grab[0], y - self._grab[1])
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging is None:
            return super().mouseMoveEvent(event)
        x, y = self._to_screen(event.position())
        self._dragging = self._clamped(x - self._grab[0], y - self._grab[1])
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging is None or event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        x, y = self._to_screen(event.position())
        origin = self._clamped(x - self._grab[0], y - self._grab[1])
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._commit(origin, snap=int(round(SNAP_PX / max(self._scale(), 1e-9))))

    # -- the keyboard -----------------------------------------------------

    def keyPressEvent(self, event) -> None:
        steps = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                 Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        step = steps.get(Qt.Key(event.key()))
        if step is None:
            return super().keyPressEvent(event)
        size = NUDGE_BIG_PX if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else NUDGE_PX
        x, y = self.pill_origin()
        # No snapping: a nudge that snapped its own step away would look like a
        # key that does nothing at all.
        self._commit(self._clamped(x + step[0] * size, y + step[1] * size), snap=0)

    # -- drawing ----------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._screen_rect()
        painter.setPen(QPen(_FOCUS if self.hasFocus() else _SCREEN_EDGE, 2))
        painter.setBrush(_SCREEN_BG)
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
        if self._dragging is not None:
            self._draw_guides(painter, rect)
        self._draw_pill(painter, self._pill_rect())
        painter.end()

    def _draw_guides(self, painter: QPainter, rect: QRectF) -> None:
        """While dragging: where the nine anchors would put the pill."""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_GUIDE)
        scale = self._scale()
        for position in _GUIDE_POSITIONS:
            x, y = pill_origin(position, 0, 0, self._screen, PILL_SIZE)
            ghost = QRectF(rect.x() + x * scale, rect.y() + y * scale,
                           PILL_SIZE[0] * scale, PILL_SIZE[1] * scale)
            painter.drawRoundedRect(ghost, ghost.height() / 2, ghost.height() / 2)

    def _draw_pill(self, painter: QPainter, pill: QRectF) -> None:
        radius = pill.height() / 2
        painter.setPen(QPen(_PILL_EDGE, 1))
        painter.setBrush(_PILL_BG)
        painter.drawRoundedRect(pill, radius, radius)
        # A dot and a few bars, so the thing being dragged reads as the pill.
        dot = min(pill.height() / 4, 3.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_DOT)
        painter.drawEllipse(QPointF(pill.x() + radius, pill.center().y()), dot, dot)
        painter.setBrush(_BAR)
        bars = 7
        step = (pill.width() - 4 * radius) / max(bars, 1)
        for index in range(bars):
            height = pill.height() * (0.25 if index % 2 else 0.45)
            painter.drawRect(QRectF(pill.x() + 2 * radius + index * step,
                                    pill.center().y() - height / 2,
                                    max(step / 2, 1.0), height))


#: The nine anchors, as ghosts under a drag.
_GUIDE_POSITIONS = tuple(f"{v}-{h}" for v in ("top", "middle", "bottom")
                         for h in ("left", "center", "right"))


def _primary_screen() -> tuple[int, int]:
    """This desktop's screen, or nothing at all when Qt has no display."""
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return (0, 0)
    geometry = screen.geometry()
    return (geometry.width(), geometry.height())


def _sane_screen(size: tuple[int, int]) -> tuple[int, int]:
    """A screen big enough to hold the pill, whatever Qt said.

    An empty geometry is what a headless session hands back, and dividing a
    preview by it is a crash on the first paint.
    """
    width, height = int(size[0]), int(size[1])
    if width <= PILL_SIZE[0] or height <= PILL_SIZE[1]:
        return DEFAULT_SCREEN
    return (width, height)
