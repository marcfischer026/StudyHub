"""Live CPU, GPU and memory use for the activity panel: three readouts and a two-minute chart.

Quiet on purpose, in the app's own colours: each readout is the value (memory
in GB) with the high, low and average of the window under it; the chart is
three smooth lines on a three-level grid, with a dot where each line is now
and the values at any moment on hover.

The chart is drawn against the clock, one reading behind it, so each line
grows in continuously instead of jumping a segment every two seconds, and a
pause in sampling shows as a gap. The numbers ease to each new reading. With
"Reduce motion" on, nothing moves; values simply update on each reading.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..services.system_monitor import Snapshot
from . import motion, style

WINDOW = 120.0  # seconds of history on screen
GAP = 6.0  # seconds without a reading that break the line (sampling pauses while idle)
FPS = 30
EASE = 0.12  # share of the remaining distance a readout covers per frame: a calm glide
INTERVAL = 2.0  # seconds between readings (the indicator's timer)

# The lines need more colour than the app's calm accent to read at 2 px: saturated
# blue, teal and orange, far apart in hue, each ≥ 4.8:1 on the chart's background
# (white in light mode, the card tint in dark mode). Outside the state colours.
SERIES_LIGHT = {"cpu": "#0058D6", "gpu": "#00806E", "ram": "#C24E00"}
SERIES_DARK = {"cpu": "#5AA9FF", "gpu": "#2ED3B7", "ram": "#FFAA33"}


@dataclass
class Palette:
    panel: QColor
    grid: QColor
    frame: QColor
    text: QColor
    dim: QColor
    hover: QColor
    series: dict


def palette() -> Palette:
    """The app's own tokens, so the panel sits in the window like the rest of it."""
    t = style.tokens()
    # Secondary text a step darker than the app's muted token: small mono figures on a
    # tinted panel need more contrast than a muted line of body text.
    dim = style.qcolor(t.text)
    dim.setAlphaF(0.7)
    return Palette(
        panel=style.qcolor(t.card if t.dark else t.raised),
        grid=style.qcolor(t.border),
        frame=style.qcolor(t.separator),
        text=style.qcolor(t.text),
        dim=dim,
        hover=style.qcolor(t.raised),
        series={key: QColor(colour) for key, colour in (SERIES_DARK if t.dark else SERIES_LIGHT).items()},
    )


QUOTE_H, PLOT_H = 62, 122


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
        self.setFixedHeight(self._height())
        self.frame = QTimer(self, interval=1000 // FPS)
        self.frame.timeout.connect(self._tick)

    @staticmethod
    def _height() -> int:
        return QUOTE_H + PLOT_H

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(self._height())
        return size

    def update_from(self, snap: Snapshot) -> None:
        tips = ["CPU: the whole Mac"]
        if snap.gpu_mem_gb is not None:
            tips.append(f"GPU: {snap.gpu_mem_gb:.1f} GB of memory held by the GPU (loaded models)")
        if snap.ram_total_gb:
            tips.append(f"Memory: in use, of {snap.ram_total_gb:.0f} GB")
        tips.append("H, L, Ø: high, low and average of the two minutes shown")
        self.setToolTip("\n".join(tips))
        first = self.snapshot.cpu is None
        self.snapshot = snap
        if first or not motion.enabled():  # open on the values, then ease between readings
            self._settle()
        self.update()

    def _targets(self) -> dict[str, float]:
        snap = self.snapshot
        return {"cpu": snap.cpu or 0.0, "gpu": snap.gpu or 0.0, "ram": self._gb(snap.ram_used) or 0.0}

    def _gb(self, share: float | None) -> float | None:
        """Memory in use in GB, from the percentage the readings keep."""
        total = self.snapshot.ram_total_gb
        return None if share is None or not total else share / 100 * total

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
        return QRectF(0, QUOTE_H, self.width(), PLOT_H)

    def _fonts(self) -> tuple[QFont, QFont, QFont]:
        family = style.tokens().mono
        base = self.font().pointSizeF()
        small, value, label = QFont(family), QFont(family), QFont(self.font())
        small.setPointSizeF(base - 3)
        label.setPointSizeF(base - 1.5)
        value = QFont(self.font())
        value.setPointSizeF(base + 6)
        value.setWeight(QFont.Weight.Medium)
        if hasattr(value, "setFeature"):  # digits of equal width, so easing numbers don't jitter
            value.setFeature(QFont.Tag("tnum"), 1)
        return small, label, value

    def paintEvent(self, event) -> None:
        painter = QPainter(self)  # no anti-aliasing: every line lands on whole pixels
        self.p = palette()
        small, label, value = self._fonts()
        now = time.monotonic()
        readings = [r for r in self.history.readings if now - r.t <= WINDOW + GAP]
        try:
            self._paint_quotes(painter, small, label, value, readings, now)
            self._paint_plot(painter, small, readings, now)
        finally:  # an active painter left behind breaks every later paint of the window
            painter.end()

    def _paint_quotes(self, painter: QPainter, small: QFont, label: QFont, value: QFont, readings, now: float) -> None:
        snap = self.snapshot
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        notes = {
            "cpu": "",
            "gpu": f"{snap.gpu_mem_gb:.1f} GB" if snap.gpu_mem_gb is not None else "",
            "ram": f"of {snap.ram_total_gb:.0f} GB" if snap.ram_total_gb else "",
        }
        units = {"cpu": "%", "gpu": "%", "ram": "GB"}
        known = {"cpu": snap.cpu is not None, "gpu": snap.gpu is not None, "ram": self._gb(snap.ram_used) is not None}
        width = self.width() / 3
        for i, (key, name) in enumerate((("cpu", "CPU"), ("gpu", "GPU"), ("ram", "Memory"))):
            cell = QRectF(i * width, 0, width, QUOTE_H)
            inner = cell.adjusted(0 if i == 0 else 10, 2, -10 if i < 2 else 0, -8)  # flush with the text above
            # Name with a dot in its line's colour, and a quiet note on the right.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.p.series[key])
            painter.drawEllipse(QPointF(inner.left() + 3, inner.top() + 7), 3, 3)
            painter.setFont(label)
            painter.setPen(self.p.dim)
            painter.drawText(inner.adjusted(11, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, name)
            painter.setFont(small)
            room = inner.width() - 11 - QFontMetricsF(label).horizontalAdvance(name) - 6
            if QFontMetricsF(small).horizontalAdvance(notes[key]) <= room:  # left out rather than over the name
                painter.drawText(inner, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, notes[key])
            # The value, with its unit small beside it.
            painter.setFont(value)
            metrics = QFontMetricsF(value)
            baseline = inner.top() + 16 + metrics.ascent()
            painter.setPen(self.p.text)
            number = _number(self.shown[key]) if known[key] else "–"
            painter.drawText(QPointF(inner.left(), baseline), number)
            if known[key]:
                painter.setFont(label)
                painter.setPen(self.p.dim)
                painter.drawText(QPointF(inner.left() + metrics.horizontalAdvance(number) + 3, baseline), units[key])
            # Range of the window: high, low, average.
            stats = _stats(readings, key)
            if key == "ram" and stats:
                stats = tuple(self._gb(v) for v in stats)
                if None in stats:  # the total isn't known before the first snapshot
                    stats = None
            painter.setFont(small)
            painter.setPen(self.p.dim)
            line = _fit_stats(stats, 1 if key == "ram" else 0, QFontMetricsF(small), inner.width())
            painter.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, line)

    def _paint_plot(self, painter: QPainter, small: QFont, readings, now: float) -> None:
        plot = self._plot_rect()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.p.panel)
        painter.drawRoundedRect(plot, style.RADIUS_M, style.RADIUS_M)
        painter.setFont(small)
        metrics = QFontMetricsF(small)
        scale = metrics.horizontalAdvance("100%") + 6  # left: the 0/50/100 scale
        tags = metrics.horizontalAdvance("88.8 GB") + 14  # right: each line's value beside its point
        area = QRectF(plot.left() + 8 + scale, plot.top() + 10, plot.width() - scale - tags - 10, plot.height() - metrics.height() - 18)
        left, right, top, bottom = area.left(), area.right(), area.top(), area.bottom()
        # Drawn one reading behind the clock, so the newest stretch of each line grows in
        # continuously instead of appearing a whole segment at a time.
        shown_now = now - INTERVAL if motion.enabled() else now

        def y_of(value: float) -> float:
            return bottom - (bottom - top) * max(0.0, min(value, 100.0)) / 100

        def x_of(t: float) -> float:
            return right - (shown_now - t) / WINDOW * (right - left)

        # A quiet grid: three levels and their labels, the two ends of the time axis.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for level in (0, 50, 100):
            y = round(y_of(level)) + 0.5
            painter.setPen(QPen(self.p.frame if level == 0 else self.p.grid, 1))
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            painter.setPen(self.p.dim)
            painter.drawText(
                QRectF(left - scale - 4, y - metrics.height() / 2, scale, metrics.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{level}%" if level == 100 else str(level),
            )
        axis = QRectF(left, bottom + 4, right - left, metrics.height())
        painter.drawText(axis, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "2 min ago")
        painter.drawText(axis, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, "now")
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if not any(r.t <= shown_now for r in readings):  # drawn a reading behind: nothing to show yet
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Measuring…")
            return

        heads: dict[str, tuple[float, float]] = {}  # where each line is now, and its value
        painter.save()
        painter.setClipRect(QRectF(left, top - 3, right - left + 4, bottom - top + 6))
        for key in ("ram", "gpu", "cpu"):  # CPU on top
            pen = QPen(self.p.series[key], 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            runs = _runs(readings, key)
            for run in runs:
                points = _until([(r.t, getattr(r, key)) for r in run], shown_now)
                if not points:
                    continue
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(_smooth([QPointF(x_of(t), y_of(v)) for t, v in points]))
                if run is runs[-1]:  # a dot where the line is now
                    head = QPointF(x_of(points[-1][0]), y_of(points[-1][1]))
                    painter.setPen(QPen(self.p.panel, 1.5))
                    painter.setBrush(self.p.series[key])
                    painter.drawEllipse(head, 3.5, 3.5)
                    if head.x() > right - 2:  # only a line that is live, not one that stopped earlier
                        heads[key] = (head.y(), points[-1][1])
        painter.restore()
        self._paint_heads(painter, metrics, heads, right, top, bottom)

        if self.hover_x is not None:
            self._paint_hover(painter, metrics, readings, x_of, y_of, shown_now, (left, right, top, bottom))

    def _paint_heads(self, painter, metrics, heads, right, top, bottom) -> None:
        """Each live line's value right beside its moving point, nudged apart where they'd overlap."""
        bold = QFont(painter.font())
        bold.setWeight(QFont.Weight.Bold)
        painter.setFont(bold)
        step = metrics.height() + 1
        placed: list[tuple[float, str]] = []
        for key, (y, value) in sorted(heads.items(), key=lambda item: item[1][0]):
            if placed and y - placed[-1][0] < step:
                y = placed[-1][0] + step
            placed.append((y, key))
        if placed and placed[-1][0] > bottom + step / 2:  # pushed off the bottom: shift all up
            shift = placed[-1][0] - (bottom + step / 2)
            placed = [(y - shift, key) for y, key in placed]
        for y, key in placed:
            value = heads[key][1]
            text = f"{self._gb(value):.1f} GB" if key == "ram" and self._gb(value) is not None else f"{value:.0f}%"
            painter.setPen(self.p.series[key])
            painter.drawText(
                QRectF(right + 8, y - metrics.height() / 2, metrics.horizontalAdvance(text) + 8, metrics.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )

    def _paint_hover(self, painter, metrics, readings, x_of, y_of, shown_now, bounds) -> None:
        left, right, top, bottom = bounds
        visible = [r for r in readings if r.t <= shown_now]
        if not visible:
            return
        nearest = min(visible, key=lambda r: abs(x_of(r.t) - self.hover_x))
        x = x_of(nearest.t)
        if abs(x - self.hover_x) > 12:
            return
        line = QColor(self.p.dim)
        line.setAlpha(110)
        painter.setPen(QPen(line, 1))
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        for key in ("cpu", "gpu", "ram"):
            value = getattr(nearest, key)
            if value is not None:
                painter.setPen(QPen(self.p.panel, 1.5))
                painter.setBrush(self.p.series[key])
                painter.drawEllipse(QPointF(x, y_of(value)), 3, 3)
        parts = [f"CPU {nearest.cpu:.0f}%"]
        if nearest.gpu is not None:
            parts.append(f"GPU {nearest.gpu:.0f}%")
        ram = self._gb(nearest.ram)
        if ram is not None:
            parts.append(f"{ram:.1f} GB")
        ago = max(0, int(self.history.latest().t - nearest.t)) if self.history.latest() else 0
        parts.append("now" if ago < 2 else f"{ago} s ago")
        text = "  ·  ".join(parts)
        box = QRectF(0, top, metrics.horizontalAdvance(text) + 14, metrics.height() + 8)
        box.moveLeft(x + 8 if x + 8 + box.width() <= right else x - 8 - box.width())
        if box.left() < left:  # too wide for either side: keep it inside the chart, under the top line
            box.moveLeft(left)
            box.moveTop(top + 6)
        painter.setPen(QPen(self.p.frame, 1))
        painter.setBrush(self.p.hover)
        painter.drawRoundedRect(box, style.RADIUS_S, style.RADIUS_S)
        painter.setPen(self.p.text)
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
    """A gentle curve through the readings (midpoint quadratics) instead of corners."""
    path = QPainterPath(points[0])
    if len(points) == 1:
        path.lineTo(points[0] + QPointF(1, 0))
        return path
    path.lineTo((points[0] + points[1]) / 2)
    for corner, after in zip(points[1:-1], points[2:]):
        path.quadTo(corner, (corner + after) / 2)
    path.lineTo(points[-1])
    return path


def _until(points: list[tuple[float, float]], end: float) -> list[tuple[float, float]]:
    """The points up to ``end``, the last one interpolated on the way to the next reading."""
    shown = [p for p in points if p[0] <= end]
    later = [p for p in points if p[0] > end]
    if shown and later:
        (t0, v0), (t1, v1) = shown[-1], later[0]
        share = (end - t0) / (t1 - t0)
        shown.append((end, v0 + (v1 - v0) * share))
    return shown


def _fit_stats(stats: tuple[float, float, float] | None, digits: int, metrics: QFontMetricsF, width: float) -> str:
    """High, low and average in the column's width: tighter spacing first, then without the average."""
    if not stats:
        return ""
    high, low, mean = (f"{v:.{digits}f}" for v in stats)
    candidates = (f"H {high}  L {low}  Ø {mean}", f"H {high} L {low} Ø {mean}", f"H {high}  L {low}", f"H {high} L {low}")
    return next((line for line in candidates if metrics.horizontalAdvance(line) <= width), candidates[-1])


def _number(value: float) -> str:
    """One decimal, except at 100 where the column has no room for it."""
    return f"{value:.0f}" if value >= 99.95 else f"{value:.1f}"


def _stats(readings: list[Reading], key: str) -> tuple[float, float, float] | None:
    values = [getattr(r, key) for r in readings if getattr(r, key) is not None]
    if not values:
        return None
    return max(values), min(values), sum(values) / len(values)
