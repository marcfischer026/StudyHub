"""Live CPU, GPU and memory use for the activity popup: three readouts and a two-minute chart.

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
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
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

TILE_H, PLOT_H, SPACING = 56, 92, 10


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
        snap = self.snapshot
        # Notes stay short so three tiles fit the popup's width; the tooltip has the long form.
        gpu_note = f"{snap.gpu_mem_gb:.1f} GB" if snap.gpu_mem_gb is not None else ""
        ram_note = f"{snap.ram_total_gb:.0f} GB" if snap.ram_total_gb else ""
        tiles = [
            ("CPU", "cpu", "", snap.cpu is not None),
            ("GPU", "gpu", gpu_note, snap.gpu is not None),
            ("Memory", "ram", ram_note, snap.ram_used is not None),
        ]
        gap = 8
        width = (self.width() - gap * (len(tiles) - 1)) / len(tiles)
        label_font, value_font, note_font = QFont(self.font()), QFont(t.mono), QFont(self.font())
        label_font.setPointSizeF(self.font().pointSizeF() - 1)
        note_font.setPointSizeF(self.font().pointSizeF() - 2)
        value_font.setPointSizeF(self.font().pointSizeF() + 6)
        value_font.setWeight(QFont.Weight.DemiBold)
        for i, (label, key, note, known) in enumerate(tiles):
            rect = QRectF(i * (width + gap), 0, width, TILE_H)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(style.qcolor(t.card))
            painter.drawRoundedRect(rect, style.RADIUS_M, style.RADIUS_M)
            inner = rect.adjusted(10, 6, -10, -6)
            # A short bar in the series colour ties the readout to its line; its length is the value.
            share = max(0.0, min(self.shown[key], 100.0)) / 100 if known else 0.0
            track = QRectF(inner.left(), rect.bottom() - 4, inner.width(), 2)
            painter.setBrush(style.qcolor(t.border))
            painter.drawRoundedRect(track, 1, 1)
            painter.setBrush(colors[key])
            painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * share, 2), 1, 1)

            painter.setPen(style.qcolor(t.muted))
            painter.setFont(label_font)
            painter.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
            bottom = inner.adjusted(0, 0, 0, -3)  # clear of the bar
            painter.setFont(note_font)
            painter.drawText(inner, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, note)
            painter.setPen(style.qcolor(t.text))
            painter.setFont(value_font)
            value = f"{self.shown[key]:.0f}%" if known else "–"
            painter.drawText(bottom, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, value)

    def _paint_plot(self, painter: QPainter, t, colors) -> None:
        plot = self._plot_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(style.qcolor(t.card))
        painter.drawRoundedRect(plot, style.RADIUS_M, style.RADIUS_M)
        area = plot.adjusted(8, 8, -8, -16)  # room for the time axis below

        small = QFont(self.font())
        small.setPointSizeF(self.font().pointSizeF() - 2)
        painter.setFont(small)
        grid = style.qcolor(t.border)
        for level in (0, 50, 100):
            y = area.bottom() - area.height() * level / 100
            painter.setPen(QPen(grid, 1, Qt.PenStyle.SolidLine if level == 0 else Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        painter.setPen(style.qcolor(t.muted))
        axis = QRectF(area.left(), area.bottom() + 2, area.width(), 14)
        painter.drawText(axis, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "2 min ago")
        painter.drawText(axis, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "now")
        # Legend in the middle of the axis: a dot in each line's colour.
        metrics = painter.fontMetrics()
        entries = [("CPU", colors["cpu"]), ("GPU", colors["gpu"])]
        widths = [10 + metrics.horizontalAdvance(name) for name, _ in entries]
        x = axis.center().x() - (sum(widths) + 12 * (len(entries) - 1)) / 2
        for (name, color), width in zip(entries, widths):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x + 3, axis.center().y()), 3, 3)
            painter.setPen(style.qcolor(t.muted))
            painter.drawText(QRectF(x + 10, axis.top(), width, axis.height()), Qt.AlignmentFlag.AlignVCenter, name)
            x += width + 12

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
        painter.setClipRect(area.adjusted(0, -4, 0, 1))
        for key in ("gpu", "cpu"):  # CPU on top
            runs = _runs(readings, key)
            for run in runs:
                points = [QPointF(x_of(r), y_of(getattr(r, key))) for r in run]
                line = _smooth(points)
                fill = QPainterPath(line)
                fill.lineTo(points[-1].x(), area.bottom())
                fill.lineTo(points[0].x(), area.bottom())
                fill.closeSubpath()
                shade = QLinearGradient(0, area.top(), 0, area.bottom())
                top, bottom = QColor(colors[key]), QColor(colors[key])
                top.setAlpha(70 if key == "cpu" else 50)
                bottom.setAlpha(0)
                shade.setColorAt(0, top)
                shade.setColorAt(1, bottom)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(shade)
                painter.drawPath(fill)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(colors[key], 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawPath(line)
            if runs and runs[-1][-1] is readings[-1]:
                last = QPointF(x_of(readings[-1]), y_of(getattr(readings[-1], key)))
                halo = QColor(colors[key])
                halo.setAlpha(60)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(last, 5, 5)
                painter.setBrush(colors[key])
                painter.drawEllipse(last, 2.5, 2.5)
        painter.restore()

        if self.hover_x is not None:
            self._paint_hover(painter, t, colors, area, readings, x_of, y_of, now)

    def _paint_hover(self, painter, t, colors, area, readings, x_of, y_of, now) -> None:
        nearest = min(readings, key=lambda r: abs(x_of(r) - self.hover_x))
        x = x_of(nearest)
        if abs(x - self.hover_x) > 12:
            return
        painter.setPen(QPen(style.qcolor(t.muted), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        for key in ("cpu", "gpu"):
            value = getattr(nearest, key)
            if value is not None:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(colors[key])
                painter.drawEllipse(QPointF(x, y_of(value)), 3, 3)
        ago = int(now - nearest.t)
        gpu = f"{nearest.gpu:.0f}%" if nearest.gpu is not None else "–"
        text = f"CPU {nearest.cpu:.0f}%  GPU {gpu}  ·  {ago} s ago"
        metrics = painter.fontMetrics()
        box = QRectF(0, 0, metrics.horizontalAdvance(text) + 12, metrics.height() + 6)
        box.moveTop(area.top())
        box.moveLeft(min(max(area.left(), x + 8), area.right() - box.width()))
        if box.left() < x < box.right():  # flip to the other side of the line
            box.moveRight(x - 8)
        painter.setPen(QPen(style.qcolor(t.border), 1))
        painter.setBrush(style.qcolor(t.raised))
        painter.drawRoundedRect(box, style.RADIUS_S, style.RADIUS_S)
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


def _smooth(points: list[QPointF]) -> QPainterPath:
    """A gentle curve through the points (midpoint quadratics), so the line doesn't look jagged."""
    path = QPainterPath(points[0])
    if len(points) > 1:
        path.lineTo((points[0] + points[1]) / 2)
        for corner, after in zip(points[1:-1], points[2:]):
            path.quadTo(corner, (corner + after) / 2)
    path.lineTo(points[-1])
    return path
