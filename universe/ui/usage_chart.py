"""Live CPU, GPU and memory use for the activity popup: three readouts and a two-minute chart.

Drawn like an instrument rather than a card: hairlines, square colour keys,
tabular numbers and straight segments between the actual readings.

The chart is drawn against the clock, not per sample, so it glides left
between the two-second readings and a pause in sampling shows as a gap
instead of a jump. The readouts ease to each new value. With "Reduce
motion" on, both simply update on each reading.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..services.system_monitor import Snapshot
from . import motion, style

WINDOW = 120.0  # seconds of history on screen
GAP = 6.0  # seconds without a reading that break the line (sampling pauses while idle)
FPS = 30
EASE = 0.25  # share of the remaining distance a readout covers per frame

# GPU is the second series: teal, far enough from the blue accent in hue and lightness
# to tell the lines apart, and outside the state colours (red/amber/green).
GPU_LIGHT, GPU_DARK = "#1F7A72", "#4FB3A9"

TILE_H, PLOT_H, SPACING = 54, 100, 8


@dataclass
class Reading:
    t: float
    cpu: float
    gpu: float | None
    ram: float | None


class UsageHistory:
    """The last few minutes of readings; kept by the indicator so the chart has a past when it opens."""

    def __init__(self):
        self.readings: deque[Reading] = deque(maxlen=int(WINDOW * 2))

    def add(self, snap: Snapshot, t: float | None = None) -> None:
        if snap.cpu is None:  # battery-only reading while idle
            return
        self.readings.append(Reading(time.monotonic() if t is None else t, snap.cpu, snap.gpu, snap.ram_used))

    def latest(self) -> Reading | None:
        return self.readings[-1] if self.readings else None


class UsageChart(QWidget):
    def __init__(self, history: UsageHistory, parent=None):
        super().__init__(parent)
        self.history = history
        self.snapshot = Snapshot()
        self.shown = {"cpu": 0.0, "gpu": 0.0, "ram": 0.0}  # what the readouts show while easing
        self.hover_x: float | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(TILE_H + SPACING + PLOT_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.frame = QTimer(self, interval=1000 // FPS)
        self.frame.timeout.connect(self._tick)

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(TILE_H + SPACING + PLOT_H)
        return size

    def update_from(self, snap: Snapshot) -> None:
        tips = ["CPU: the whole Mac"]
        if snap.gpu_mem_gb is not None:
            tips.append(f"GPU: {snap.gpu_mem_gb:.1f} GB of memory held by the GPU (loaded models)")
        if snap.ram_total_gb:
            tips.append(f"Memory: share of {snap.ram_total_gb:.0f} GB in use")
        self.setToolTip("\n".join(tips))
        self.snapshot = snap  # the readouts count up from zero when the popup opens
        if not motion.enabled():
            self._settle()
        self.update()

    def _targets(self) -> dict[str, float]:
        snap = self.snapshot
        return {"cpu": snap.cpu or 0.0, "gpu": snap.gpu or 0.0, "ram": snap.ram_used or 0.0}

    def _settle(self) -> None:
        self.shown = self._targets()

    def _tick(self) -> None:
        for key, target in self._targets().items():
            current = self.shown[key]
            self.shown[key] = target if abs(target - current) < 0.3 else current + (target - current) * EASE
        self.update()

    # The frame timer only runs while the popup is open, and not at all with "Reduce motion".
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if motion.enabled():
            self.frame.start()

    def hideEvent(self, event) -> None:
        self.frame.stop()
        super().hideEvent(event)

    def mouseMoveEvent(self, event) -> None:
        self.hover_x = event.position().x() if self._plot_rect().contains(event.position()) else None
        self.update()

    def leaveEvent(self, event) -> None:
        self.hover_x = None
        self.update()

    # --- painting -------------------------------------------------------------

    def _colors(self) -> dict[str, QColor]:
        t = style.tokens()
        return {"cpu": style.qcolor(t.accent), "gpu": QColor(GPU_DARK if t.dark else GPU_LIGHT), "ram": style.qcolor(t.muted)}

    def _plot_rect(self) -> QRectF:
        return QRectF(0, TILE_H + SPACING, self.width(), PLOT_H)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = style.tokens()
        colors = self._colors()
        self._paint_tiles(painter, t, colors)
        self._paint_plot(painter, t, colors)

    def _paint_tiles(self, painter: QPainter, t, colors) -> None:
        """Three readouts in columns split by hairlines, like a spec sheet: no cards, no bars."""
        snap = self.snapshot
        gpu_note = f"{snap.gpu_mem_gb:.1f} GB" if snap.gpu_mem_gb is not None else ""
        ram_note = f"{snap.ram_total_gb:.0f} GB" if snap.ram_total_gb else ""
        tiles = [
            ("CPU", "cpu", "", snap.cpu is not None),
            ("GPU", "gpu", gpu_note, snap.gpu is not None),
            ("MEMORY", "ram", ram_note, snap.ram_used is not None),
        ]
        base = self.font().pointSizeF()
        label_font = QFont(self.font())
        label_font.setPointSizeF(base - 2.5)
        label_font.setWeight(QFont.Weight.DemiBold)
        label_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
        note_font = QFont(t.mono)
        note_font.setPointSizeF(base - 2.5)
        value_font = QFont(self.font())
        value_font.setPointSizeF(base + 9)
        value_font.setWeight(QFont.Weight.Medium)
        _tabular(value_font)
        unit_font = QFont(self.font())
        unit_font.setPointSizeF(base + 1)
        width = self.width() / len(tiles)
        rule = style.qcolor(t.separator)
        for i, (label, key, note, known) in enumerate(tiles):
            rect = QRectF(i * width, 0, width, TILE_H)
            inner = rect.adjusted(0 if i == 0 else 12, 2, -12 if i < len(tiles) - 1 else 0, -8)
            if i:
                painter.setPen(QPen(rule, 1))
                x = round(rect.left()) + 0.5
                painter.drawLine(QPointF(x, rect.top() + 2), QPointF(x, rect.bottom() - 8))
            # A square key in the line's colour (memory has no line, so a muted one).
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colors[key])
            painter.drawRect(QRectF(inner.left(), inner.top() + 3, 7, 7))
            painter.setPen(style.qcolor(t.muted))
            painter.setFont(label_font)
            painter.drawText(inner.adjusted(12, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
            painter.setFont(note_font)
            painter.drawText(inner, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, note)
            # The number large, the unit small beside it on the same baseline.
            painter.setFont(value_font)
            number = f"{self.shown[key]:.0f}" if known else "–"
            metrics = painter.fontMetrics()
            baseline = inner.bottom() - metrics.descent()
            painter.setPen(style.qcolor(t.text))
            painter.drawText(QPointF(inner.left(), baseline), number)
            if known:
                left = inner.left() + metrics.horizontalAdvance(number) + 2
                painter.setFont(unit_font)
                painter.setPen(style.qcolor(t.muted))
                painter.drawText(QPointF(left, baseline), "%")
        painter.setPen(QPen(rule, 1))
        y = TILE_H + 0.5
        painter.drawLine(QPointF(0, y), QPointF(self.width(), y))

    def _paint_plot(self, painter: QPainter, t, colors) -> None:
        plot = self._plot_rect()
        scale = QFont(t.mono)
        scale.setPointSizeF(self.font().pointSizeF() - 3)
        painter.setFont(scale)
        metrics = painter.fontMetrics()
        gutter = metrics.horizontalAdvance("100") + 6
        area = plot.adjusted(0, 6, -gutter, -(metrics.height() + 4))  # scale on the right, time below

        # Hairline grid every 25 %, on whole pixels so it stays crisp; labels at 0/50/100.
        grid, muted = style.qcolor(t.border), style.qcolor(t.muted)
        for level in (0, 25, 50, 75, 100):
            y = round(area.bottom() - area.height() * level / 100) + 0.5
            painter.setPen(QPen(style.qcolor(t.separator) if level == 0 else grid, 1))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            if level % 50 == 0:
                painter.setPen(muted)
                painter.drawText(
                    QRectF(area.right(), y - metrics.height() / 2, gutter, metrics.height()),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(level),
                )
        # Time ticks: a minute apart, labelled like a clock offset.
        axis_y = round(area.bottom()) + 0.5
        for seconds, text in ((120, "−2:00"), (60, "−1:00"), (0, "now")):
            x = round(area.right() - seconds / WINDOW * area.width()) + 0.5
            painter.setPen(QPen(style.qcolor(t.separator), 1))
            painter.drawLine(QPointF(x, axis_y), QPointF(x, axis_y + 3))
            align = {120: Qt.AlignmentFlag.AlignLeft, 0: Qt.AlignmentFlag.AlignRight}.get(seconds, Qt.AlignmentFlag.AlignHCenter)
            box = QRectF(x - 40, axis_y + 3, 80, metrics.height())
            if seconds == 120:
                box.moveLeft(x)
            elif seconds == 0:
                box.moveRight(x)
            painter.setPen(muted)
            painter.drawText(box, align | Qt.AlignmentFlag.AlignVCenter, text)

        now = time.monotonic()
        readings = [r for r in self.history.readings if now - r.t <= WINDOW + GAP]
        if not readings:
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Measuring…")
            return

        def x_of(r: Reading) -> float:
            return area.right() - (now - r.t) / WINDOW * area.width()

        def y_of(value: float) -> float:
            return area.bottom() - area.height() * max(0.0, min(value, 100.0)) / 100

        painter.save()
        painter.setClipRect(area.adjusted(0, -2, 0, 0))
        for key in ("gpu", "cpu"):  # CPU on top
            for run in _runs(readings, key):
                points = [QPointF(x_of(r), y_of(getattr(r, key))) for r in run]
                line = _polyline(points)
                fill = QPainterPath(line)
                fill.lineTo(points[-1].x(), area.bottom())
                fill.lineTo(points[0].x(), area.bottom())
                fill.closeSubpath()
                tint = QColor(colors[key])
                tint.setAlpha(26 if key == "cpu" else 18)  # flat, faint: the line carries the data
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(tint)
                painter.drawPath(fill)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(colors[key], 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin))
                painter.drawPath(line)
        painter.restore()

        if self.hover_x is not None:
            self._paint_hover(painter, t, colors, area, readings, x_of, y_of, now)

    def _paint_hover(self, painter, t, colors, area, readings, x_of, y_of, now) -> None:
        nearest = min(readings, key=lambda r: abs(x_of(r) - self.hover_x))
        x = x_of(nearest)
        if abs(x - self.hover_x) > 12:
            return
        x = round(x) + 0.5
        painter.setPen(QPen(style.qcolor(t.text), 1))
        painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        for key in ("cpu", "gpu"):
            value = getattr(nearest, key)
            if value is not None:
                painter.setPen(QPen(style.qcolor(t.raised), 1))
                painter.setBrush(colors[key])
                painter.drawRect(QRectF(x - 3, y_of(value) - 3, 6, 6))
        ago = int(now - nearest.t)
        gpu = f"{nearest.gpu:3.0f}" if nearest.gpu is not None else "  –"
        text = f"CPU {nearest.cpu:3.0f}  GPU {gpu}  −0:{ago:02d}" if ago < 60 else f"CPU {nearest.cpu:3.0f}  GPU {gpu}  −{ago // 60}:{ago % 60:02d}"
        metrics = painter.fontMetrics()
        box = QRectF(0, 0, metrics.horizontalAdvance(text) + 10, metrics.height() + 4)
        box.moveTop(area.top())
        box.moveLeft(x + 6)
        if box.right() > area.right():  # flip to the other side of the line
            box.moveRight(x - 6)
        painter.setPen(QPen(style.qcolor(t.separator), 1))
        painter.setBrush(style.qcolor(t.raised))
        painter.drawRect(box.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.setPen(style.qcolor(t.text))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)


def _runs(readings: list[Reading], key: str) -> list[list[Reading]]:
    """Unbroken stretches of a series: split where sampling paused or the value is unknown."""
    runs: list[list[Reading]] = []
    previous = None
    for reading in readings:
        if getattr(reading, key) is None:
            previous = None
            continue
        if previous is None or reading.t - previous.t > GAP:
            runs.append([])
        runs[-1].append(reading)
        previous = reading
    return runs


def _polyline(points: list[QPointF]) -> QPainterPath:
    """Straight segments between readings: each corner is a real measurement."""
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    if len(points) == 1:
        path.lineTo(points[0] + QPointF(1, 0))
    return path


def _tabular(font: QFont) -> None:
    """Digits of equal width, so a changing readout doesn't jitter (Qt 6.7+)."""
    if hasattr(font, "setFeature"):
        font.setFeature(QFont.Tag("tnum"), 1)
