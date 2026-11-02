"""Live CPU, GPU and memory use for the activity popup, drawn like a trading terminal.

A black panel in both themes (a deliberate exception to the system palette:
it reads as an instrument, like a market display), monospace throughout, hard
1-pixel lines with no anti-aliasing, step lines between readings, the
current value tagged on the scale, and each readout with its change over the
last minute and the high, low and average of the window.

The chart is drawn against the clock, not per sample, so it glides left
between the two-second readings and a pause in sampling shows as a gap
instead of a jump. A readout flashes green or red for a moment when its
value ticks up or down. With "Reduce motion" on, nothing moves or flashes;
values simply update on each reading.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..services.system_monitor import Snapshot
from . import motion, style

WINDOW = 120.0  # seconds of history on screen
GAP = 6.0  # seconds without a reading that break the line (sampling pauses while idle)
FPS = 30
EASE = 0.3  # share of the remaining distance a readout covers per frame
FLASH = 0.7  # seconds a tick flash takes to fade
TICK = 0.5  # percentage points a value must move to flash

# The terminal palette: fixed, not themed.
BG = QColor("#050505")
PANEL = QColor("#0E0E0E")
GRID = QColor("#1E1E1E")
FRAME = QColor("#3A3A3A")
AMBER = QColor("#FF9F1C")  # labels and the header, as on a market terminal
TEXT = QColor("#EDEDED")
DIM = QColor("#7C7C7C")
UP = QColor("#00C46A")
DOWN = QColor("#FF3B3B")
SERIES = {"cpu": QColor("#FFB000"), "gpu": QColor("#00B7E8"), "ram": QColor("#B0B0B0")}

HEADER_H, QUOTE_H, PLOT_H = 18, 62, 116


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
        self.flash: dict[str, tuple[float, QColor]] = {}  # key → (when, green or red)
        self.hover_x: float | None = None
        self.setMouseTracking(True)
        self.setMinimumHeight(self._height())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.frame = QTimer(self, interval=1000 // FPS)
        self.frame.timeout.connect(self._tick)

    @staticmethod
    def _height() -> int:
        return HEADER_H + QUOTE_H + PLOT_H

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(self._height())
        return size

    def update_from(self, snap: Snapshot) -> None:
        tips = ["CPU: the whole Mac"]
        if snap.gpu_mem_gb is not None:
            tips.append(f"GPU: {snap.gpu_mem_gb:.1f} GB of memory held by the GPU (loaded models)")
        if snap.ram_total_gb:
            tips.append(f"Memory: share of {snap.ram_total_gb:.0f} GB in use")
        tips.append("Δ1M: change over the last minute · H/L/AVG: over the two minutes shown")
        self.setToolTip("\n".join(tips))
        before = self._targets()
        self.snapshot = snap  # the readouts count up from zero when the popup opens
        if motion.enabled():
            now = time.monotonic()
            for key, value in self._targets().items():
                if before[key] and abs(value - before[key]) >= TICK:
                    self.flash[key] = (now, UP if value > before[key] else DOWN)
        else:
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
            self.shown[key] = target if abs(target - current) < 0.05 else current + (target - current) * EASE
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

    def _plot_rect(self) -> QRectF:
        return QRectF(0, HEADER_H + QUOTE_H, self.width(), PLOT_H)

    def _fonts(self) -> tuple[QFont, QFont, QFont]:
        family = style.tokens().mono
        base = self.font().pointSizeF()
        small, value, label = QFont(family), QFont(family), QFont(family)
        small.setPointSizeF(base - 3)
        label.setPointSizeF(base - 2.5)
        label.setWeight(QFont.Weight.Bold)
        value.setPointSizeF(base + 7)
        value.setWeight(QFont.Weight.DemiBold)
        return small, label, value

    def paintEvent(self, event) -> None:
        painter = QPainter(self)  # no anti-aliasing: every line lands on whole pixels
        painter.fillRect(self.rect(), BG)
        small, label, value = self._fonts()
        now = time.monotonic()
        readings = [r for r in self.history.readings if now - r.t <= WINDOW + GAP]
        self._paint_header(painter, small, label, readings)
        self._paint_quotes(painter, small, label, value, readings, now)
        self._paint_plot(painter, small, readings, now)
        painter.setPen(QPen(FRAME, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def _paint_header(self, painter: QPainter, small: QFont, label: QFont, readings) -> None:
        bar = QRectF(0, 0, self.width(), HEADER_H)
        painter.fillRect(bar, AMBER)
        painter.setFont(label)
        painter.setPen(BG)
        painter.drawText(bar.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "SYS LOAD  ·  LOCAL AI")
        live = bool(readings) and time.monotonic() - readings[-1].t <= GAP
        painter.drawText(
            bar.adjusted(6, 0, -6, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            ("■ LIVE" if live else "□ IDLE") + "  2M  2S",
        )

    def _paint_quotes(self, painter: QPainter, small: QFont, label: QFont, value: QFont, readings, now: float) -> None:
        snap = self.snapshot
        notes = {
            "cpu": "ALL CORES",
            "gpu": f"{snap.gpu_mem_gb:.1f}GB VRAM" if snap.gpu_mem_gb is not None else "APPLE GPU",
            "ram": f"OF {snap.ram_total_gb:.0f}GB" if snap.ram_total_gb else "",
        }
        known = {"cpu": snap.cpu is not None, "gpu": snap.gpu is not None, "ram": snap.ram_used is not None}
        width = self.width() / 3
        top = HEADER_H
        for i, (key, name) in enumerate((("cpu", "CPU"), ("gpu", "GPU"), ("ram", "MEM"))):
            cell = QRectF(i * width, top, width, QUOTE_H)
            if i:
                painter.setPen(QPen(FRAME, 1))
                painter.drawLine(int(cell.left()), int(cell.top()), int(cell.left()), int(cell.bottom()) - 1)
            when_colour = self.flash.get(key)
            if when_colour is not None:
                age = now - when_colour[0]
                if age < FLASH:
                    tint = QColor(when_colour[1])
                    tint.setAlpha(int(90 * (1 - age / FLASH)))
                    painter.fillRect(cell.adjusted(1, 1, 0, 0), tint)
            inner = cell.adjusted(8, 4, -8, -4)
            # Ticker line: colour key, name, note.
            painter.fillRect(QRectF(inner.left(), inner.top() + 3, 6, 6), SERIES[key])
            painter.setFont(label)
            painter.setPen(AMBER)
            painter.drawText(inner.adjusted(10, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, name)
            painter.setFont(small)
            painter.setPen(DIM)
            painter.drawText(inner, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, notes[key])
            # Last value, then the change over the last minute.
            painter.setFont(value)
            metrics = QFontMetricsF(value)
            baseline = inner.top() + 14 + metrics.ascent()
            painter.setPen(TEXT)
            text = f"{self.shown[key]:5.1f}" if known[key] else "  —"
            painter.drawText(QPointF(inner.left(), baseline), text.strip())
            change = _change(readings, key, now)
            if change is not None and known[key]:
                arrow = "▲" if change > 0.05 else "▼" if change < -0.05 else "■"
                colour = UP if change > 0.05 else DOWN if change < -0.05 else DIM
                painter.setFont(small)
                painter.setPen(colour)
                painter.drawText(
                    QRectF(inner.left(), baseline - metrics.ascent(), inner.width(), metrics.ascent()),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                    f"{arrow}{abs(change):4.1f}",
                )
            # Range of the window: high, low, average.
            stats = _stats(readings, key)
            painter.setFont(small)
            painter.setPen(DIM)
            line = f"H{stats[0]:3.0f} L{stats[1]:3.0f} AVG{stats[2]:3.0f}" if stats else "H  — L  — AVG  —"
            painter.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, line)
        painter.setPen(QPen(FRAME, 1))
        y = int(top + QUOTE_H) - 1
        painter.drawLine(0, y, self.width(), y)

    def _paint_plot(self, painter: QPainter, small: QFont, readings, now: float) -> None:
        plot = self._plot_rect()
        painter.fillRect(plot.adjusted(1, 0, -1, -1), PANEL)
        painter.setFont(small)
        metrics = QFontMetricsF(small)
        gutter = metrics.horizontalAdvance("100.0") + 10
        area = QRectF(plot.left() + 6, plot.top() + 8, plot.width() - gutter - 6, plot.height() - metrics.height() - 14)
        left, right, top, bottom = int(area.left()), int(area.right()), int(area.top()), int(area.bottom())

        def y_of(value: float) -> int:
            return round(bottom - (bottom - top) * max(0.0, min(value, 100.0)) / 100)

        def x_of(r: Reading) -> int:
            return round(right - (now - r.t) / WINDOW * (right - left))

        # Grid: every 10 % faint, every 50 % stronger; vertical every 30 s.
        for level in range(0, 101, 10):
            painter.setPen(QPen(FRAME if level % 50 == 0 else GRID, 1))
            painter.drawLine(left, y_of(level), right, y_of(level))
        for seconds in range(0, int(WINDOW) + 1, 30):
            x = round(right - seconds / WINDOW * (right - left))
            painter.setPen(QPen(GRID, 1))
            painter.drawLine(x, top, x, bottom)
            painter.setPen(QPen(FRAME, 1))
            painter.drawLine(x, bottom, x, bottom + 3)
            text = "NOW" if seconds == 0 else f"-{seconds // 60}:{seconds % 60:02d}"
            box = QRectF(x - 30, bottom + 4, 60, metrics.height())
            align = Qt.AlignmentFlag.AlignHCenter
            if seconds == 0:
                box.moveRight(x + 2)
                align = Qt.AlignmentFlag.AlignRight
            elif seconds == int(WINDOW):
                box.moveLeft(x)
                align = Qt.AlignmentFlag.AlignLeft
            painter.setPen(DIM)
            painter.drawText(box, align | Qt.AlignmentFlag.AlignTop, text)
        # The scale: a ruled axis on the right.
        painter.setPen(QPen(FRAME, 1))
        painter.drawLine(right, top, right, bottom)
        for level in (0, 50, 100):
            painter.setPen(DIM)
            painter.drawText(
                QRectF(right + 4, y_of(level) - metrics.height() / 2, gutter - 6, metrics.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"{level:.0f}",
            )

        if not readings:
            painter.setPen(AMBER)
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "AWAITING DATA")
            return

        painter.save()
        painter.setClipRect(QRectF(left, top - 1, right - left, bottom - top + 2))
        for key in ("gpu", "cpu"):  # CPU on top
            painter.setPen(QPen(SERIES[key], 1))
            for run in _runs(readings, key):
                painter.drawPath(_steps([QPointF(x_of(r), y_of(getattr(r, key))) for r in run]))
        painter.restore()

        # Last value tagged on the scale, like a price label; the later tag yields if they collide.
        taken: list[float] = []
        for key in ("cpu", "gpu"):
            last = next((r for r in reversed(readings) if getattr(r, key) is not None), None)
            if last is None:
                continue
            y = y_of(getattr(last, key))
            if any(abs(y - other) < metrics.height() for other in taken):
                y = max(taken) + metrics.height() + 1 if y >= max(taken) else min(taken) - metrics.height() - 1
            taken.append(y)
            tag = QRectF(right + 1, y - metrics.height() / 2, gutter - 2, metrics.height())
            painter.fillRect(tag, SERIES[key])
            painter.setPen(BG)
            painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, f"{getattr(last, key):.1f}")

        if self.hover_x is not None:
            self._paint_crosshair(painter, metrics, readings, x_of, y_of, now, (left, right, top, bottom), gutter)

    def _paint_crosshair(self, painter, metrics, readings, x_of, y_of, now, bounds, gutter) -> None:
        left, right, top, bottom = bounds
        nearest = min(readings, key=lambda r: abs(x_of(r) - self.hover_x))
        x = x_of(nearest)
        if abs(x - self.hover_x) > 12:
            return
        y = y_of(nearest.cpu)
        painter.setPen(QPen(DIM, 1, Qt.PenStyle.DashLine))
        painter.drawLine(x, top, x, bottom)
        painter.drawLine(left, y, right, y)
        for key in ("cpu", "gpu"):
            value = getattr(nearest, key)
            if value is not None:
                painter.fillRect(QRectF(x - 2, y_of(value) - 2, 5, 5), SERIES[key])
        # Readout box at the top, and the time on the axis below the line.
        ago = int(now - nearest.t)
        gpu = f"{nearest.gpu:5.1f}" if nearest.gpu is not None else "    —"
        text = f"CPU {nearest.cpu:5.1f}  GPU {gpu}"
        box = QRectF(0, top, metrics.horizontalAdvance(text) + 10, metrics.height() + 4)
        box.moveLeft(x + 4 if x + 4 + box.width() <= right else x - 4 - box.width())
        painter.fillRect(box, BG)
        painter.setPen(QPen(AMBER, 1))
        painter.drawRect(box.adjusted(0, 0, -1, -1))
        painter.setPen(TEXT)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        stamp = f"-{ago // 60}:{ago % 60:02d}"
        tag = QRectF(x - 22, bottom + 3, 44, metrics.height() + 1)
        painter.fillRect(tag, AMBER)
        painter.setPen(BG)
        painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, stamp)


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


def _steps(points: list[QPointF]) -> QPainterPath:
    """A step line: each reading holds until the next one, as a tick chart draws it."""
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point.x(), path.currentPosition().y())
        path.lineTo(point)
    if len(points) == 1:
        path.lineTo(points[0] + QPointF(2, 0))
    return path


def _change(readings: list[Reading], key: str, now: float) -> float | None:
    """The latest value minus the one about a minute ago (the oldest within the minute)."""
    values = [(r.t, getattr(r, key)) for r in readings if getattr(r, key) is not None and now - r.t <= 60]
    if len(values) < 2:
        return None
    return values[-1][1] - values[0][1]


def _stats(readings: list[Reading], key: str) -> tuple[float, float, float] | None:
    values = [getattr(r, key) for r in readings if getattr(r, key) is not None]
    if not values:
        return None
    return max(values), min(values), sum(values) / len(values)
