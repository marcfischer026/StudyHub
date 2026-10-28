"""Background AI work: one queue, a status-bar indicator and a detail popup.

Transcription (whisper.cpp) and sorting (local LLM) run one job at a time so
they never compete for the GPU. Everything can be paused — by hand, or
automatically on low battery / when unplugged.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..services import lmstudio, local_llm
from ..services.jobcontrol import JobControl
from ..services.system_monitor import Snapshot, SystemMonitor
from .workers import Worker

AUTO_REASONS = ("battery", "unplugged")
_ids = itertools.count(1)


@dataclass
class Job:
    kind: str  # "transcribe" | "classify"
    title: str
    fn: Callable[[Callable[[int], None], JobControl], Any]
    on_done: Callable[[Any], None] | None = None
    on_fail: Callable[[str], None] | None = None
    key: Any = None  # identical keys are not queued twice
    id: int = field(default_factory=lambda: next(_ids))
    status: str = "queued"  # queued | running | paused | done | failed | cancelled
    progress: int = 0
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    control: JobControl | None = None

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return (self.finished or time.monotonic()) - self.started

    def eta(self) -> float | None:
        if self.status != "running" or self.progress < 3:
            return None
        return self.elapsed() * (100 - self.progress) / self.progress


class JobManager(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queue: list[Job] = []
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=6)
        self.pause_reason = ""  # "" = running; "manual" | "battery" | "unplugged"
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._worker: Worker | None = None

    # --- queue --------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return bool(self.pause_reason)

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    def find(self, key) -> Job | None:
        return next((j for j in [self.current, *self.queue] if j is not None and j.key == key), None)

    def submit(self, kind: str, title: str, fn, on_done=None, on_fail=None, key=None) -> Job | None:
        if key is not None and self.find(key):
            return None
        job = Job(kind, title, fn, on_done, on_fail, key)
        self.queue.append(job)
        self.changed.emit()
        self._start_next()
        return job

    def _start_next(self) -> None:
        if self.current is not None or not self.queue or self.paused:
            return
        job = self.current = self.queue.pop(0)
        job.status, job.started, job.control = "running", time.monotonic(), JobControl()

        def run(progress):
            return job.fn(progress, job.control)

        worker = self._worker = Worker(run, with_progress=True)
        worker.setAutoDelete(False)
        worker.signals.progress.connect(lambda pct: self._progress(job, pct))
        worker.signals.finished.connect(lambda result: self._finish(job, result, ""))
        worker.signals.failed.connect(lambda message: self._finish(job, None, message))
        self._pool.start(worker)
        self.changed.emit()

    def _progress(self, job: Job, pct: int) -> None:
        job.progress = max(job.progress, min(100, pct))
        self.changed.emit()

    def _finish(self, job: Job, result, error: str) -> None:
        job.finished = time.monotonic()
        if job.control and job.control.cancelled:
            job.status = "cancelled"
        else:
            job.status, job.error = ("failed", error) if error else ("done", "")
            if not error:
                job.progress = 100
        self.current, self._worker = None, None
        self.history.appendleft(job)
        if job.status == "done" and job.on_done:
            job.on_done(result)
        elif job.status == "failed" and job.on_fail:
            job.on_fail(error)
        self.changed.emit()
        self._start_next()

    # --- control ------------------------------------------------------------

    def pause(self, reason: str = "manual") -> None:
        if self.pause_reason == "manual" and reason in AUTO_REASONS:
            return  # the user's own pause wins
        self.pause_reason = reason
        if self.current and self.current.control:
            self.current.control.pause()
            self.current.status = "paused"
        self.changed.emit()

    def resume(self) -> None:
        self.pause_reason = ""
        if self.current and self.current.control:
            self.current.control.resume()
            self.current.status = "running"
        self.changed.emit()
        self._start_next()

    def cancel(self, job: Job) -> None:
        if job in self.queue:
            self.queue.remove(job)
            job.status = "cancelled"
            self.history.appendleft(job)
        elif job is self.current and job.control:
            job.control.cancel()  # the worker finishes with Cancelled → _finish
        self.changed.emit()

    def shutdown(self, wait_ms: int = 3000) -> None:
        for job in list(self.queue):
            self.cancel(job)
        if self.current:
            self.cancel(self.current)
        self._pool.waitForDone(wait_ms)


class ModelKeeper(QObject):
    """Watches LM Studio: which model instances are loaded, and unloads the one
    UniVerse used once no AI work has happened for ``local_llm_unload_after`` s.

    LM Studio keeps hand-loaded models in memory forever; on-demand (JIT) loads
    get the same TTL through the ``ttl`` field UniVerse sends with each request.
    """

    changed = Signal()
    message = Signal(str)

    def __init__(self, jobs: JobManager, settings: Settings, parent=None):
        super().__init__(parent)
        self.jobs, self.settings = jobs, settings
        self.root: str | None = None  # LM Studio server, e.g. http://localhost:8080
        self.instances: list[lmstudio.Instance] | None = None  # None = no LM Studio reachable
        self.used: dict[str, float] = {}  # instance id → last time UniVerse used it
        self.busy_unloading: set[str] = set()
        self._polling = False
        self._mark_pending = False  # a classify job ran since the last poll
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._keep: set[Worker] = set()
        jobs.changed.connect(self._on_jobs_changed)
        self.timer = QTimer(self, interval=10_000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        QTimer.singleShot(0, self, self.poll)  # tied to self: skipped if the keeper is gone

    @property
    def loaded_gb(self) -> float:
        return sum(i.size_gb for i in self.instances or [])

    def _on_jobs_changed(self) -> None:
        job = self.jobs.current
        if job is not None and job.kind == "classify":
            if not self._mark_pending:
                self._mark_pending = True
                self.poll()

    def poll(self) -> None:
        """Refresh the instance list in the background, then unload idle models."""
        if self._polling:
            return
        self._polling = True
        settings = self.settings

        def fetch():
            try:
                url, model = local_llm.find_server(settings)
            except local_llm.LocalLLMUnavailable:
                return None, None, None
            root = lmstudio.server_root(url)
            return root, model, lmstudio.loaded_instances(root)

        worker = Worker(fetch)
        worker.setAutoDelete(False)
        self._keep.add(worker)

        def done(result):
            self._keep.discard(worker)
            self._polling = False
            root, model, instances = result
            self.root = root if instances is not None else None
            self.instances = instances
            classifying = self.jobs.current is not None and self.jobs.current.kind == "classify"
            if model and (self._mark_pending or classifying):
                self.used[model] = time.monotonic()  # UniVerse is (or just was) using this model
                self._mark_pending = classifying
            self._unload_idle()
            self.changed.emit()

        def failed(_message):
            self._keep.discard(worker)
            self._polling = False

        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        self._pool.start(worker)

    def _unload_idle(self) -> None:
        after = self.settings.local_llm_unload_after
        if not after or self.jobs.busy or not self.instances:
            return
        loaded = {i.instance_id for i in self.instances}
        now = time.monotonic()
        for instance_id, last in list(self.used.items()):
            if instance_id not in loaded:
                del self.used[instance_id]  # already unloaded (by LM Studio's own TTL or the user)
            elif now - last >= after:
                self.unload(instance_id, reason=f"idle for {after // 60} min")

    def unload(self, instance_id: str, reason: str = "") -> None:
        if not self.root or instance_id in self.busy_unloading:
            return
        if self.jobs.current is not None and self.jobs.current.kind == "classify":
            self.message.emit("The local AI is busy — pause or cancel the job before unloading its model.")
            return
        root = self.root
        size = next((i.size_gb for i in self.instances or [] if i.instance_id == instance_id), 0)
        self.busy_unloading.add(instance_id)
        worker = Worker(lmstudio.unload, root, instance_id)
        worker.setAutoDelete(False)
        self._keep.add(worker)

        def finished(_result=None, error: str = ""):
            self._keep.discard(worker)
            self.busy_unloading.discard(instance_id)
            self.used.pop(instance_id, None)
            if error:
                self.message.emit(error)
            else:
                why = f" ({reason})" if reason else ""
                self.message.emit(f"Unloaded {instance_id} from LM Studio{why} — {size:.1f} GB memory freed.")
            self.poll()

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(lambda error: finished(error=error))
        self._pool.start(worker)
        self.changed.emit()

    def unload_all(self) -> None:
        for instance in self.instances or []:
            self.unload(instance.instance_id, reason="unloaded by you")


# --- widgets ------------------------------------------------------------------


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds < 3600 else f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


def _bar(value: float | None) -> str:
    return "–" if value is None else f"{value:.0f}%"


class ActivityIndicator(QWidget):
    """Status-bar field: what the local AI is doing, load, battery, ⏸."""

    def __init__(self, jobs: JobManager, settings: Settings, parent=None):
        super().__init__(parent)
        self.jobs, self.settings = jobs, settings
        self.models = ModelKeeper(jobs, settings, self)
        self.models.changed.connect(self.refresh)
        self.monitor = SystemMonitor()
        self.snapshot = Snapshot()
        self.popup: ActivityPopup | None = None

        self.label = QToolButton()
        self.label.setObjectName("activityLabel")
        self.label.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.label.setToolTip("Local AI activity — click for details")
        self.label.setMaximumWidth(480)  # never widen the window beyond the screen
        self.label.clicked.connect(self.toggle_popup)
        self.mini = QProgressBar(maximum=100, textVisible=False)
        self.mini.setObjectName("activityBar")
        self.mini.setFixedSize(60, 6)
        self.pause_btn = QToolButton(text="⏸")
        self.pause_btn.setObjectName("activityPause")
        self.pause_btn.clicked.connect(self.toggle_pause)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(6)
        layout.addWidget(self.label)
        layout.addWidget(self.mini)
        layout.addWidget(self.pause_btn)

        jobs.changed.connect(self.refresh)
        self.timer = QTimer(self, interval=2000)
        self.timer.timeout.connect(self._sample)
        self.timer.start()
        self._sample()

    def toggle_pause(self) -> None:
        if self.jobs.paused:
            self.jobs.resume()
        else:
            self.jobs.pause("manual")

    def toggle_popup(self) -> None:
        if self.popup and self.popup.isVisible():
            self.popup.close()
            return
        self.popup = ActivityPopup(self)
        self.popup.adjustSize()
        anchor = self.mapToGlobal(QPoint(self.width(), 0))
        self.popup.move(anchor.x() - self.popup.width(), anchor.y() - self.popup.height() - 6)
        self.popup.show()

    def _sample(self) -> None:
        busy = self.jobs.busy
        detailed = busy or bool(self.popup and self.popup.isVisible())
        self.snapshot = self.monitor.snapshot(with_gpu=detailed) if detailed else self._battery_only()
        self._apply_battery_rules()
        self.refresh()

    def _battery_only(self) -> Snapshot:
        snap = Snapshot()
        snap.battery, snap.plugged = self.monitor.battery()
        return snap

    def _apply_battery_rules(self) -> None:
        pct, plugged = self.snapshot.battery, self.snapshot.plugged
        if pct is None or plugged is None:
            return  # desktop Mac: nothing to protect
        reason = ""
        if not plugged and self.settings.only_when_charging:
            reason = "unplugged"
        elif not plugged and self.settings.battery_pause_below and pct < self.settings.battery_pause_below:
            reason = "battery"
        if reason and self.jobs.pause_reason != reason:
            self.jobs.pause(reason)
        elif not reason and self.jobs.pause_reason in AUTO_REASONS:
            self.jobs.resume()

    def refresh(self) -> None:
        job, jobs = self.jobs.current, self.jobs
        snap = self.snapshot
        if jobs.paused:
            why = {"manual": "Paused", "battery": f"Paused — battery {snap.battery}%", "unplugged": "Paused — on battery"}
            text = "⏸ " + why.get(jobs.pause_reason, "Paused")
            if jobs.busy:
                text += f" · {len(jobs.queue) + (job is not None)} waiting"
        elif job:
            title = job.title if len(job.title) <= 30 else job.title[:29] + "…"
            text = f"⚙ {title} · {job.progress}%"
            if jobs.queue:
                text += f" (+{len(jobs.queue)})"
        else:
            text = "AI idle"
        if jobs.busy and snap.cpu is not None:
            text += f"  ·  CPU {_bar(snap.cpu)}  GPU {_bar(snap.gpu)}"
        if self.models.instances:
            text += f"  ·  🧠 {self.models.loaded_gb:.1f} GB"
        if snap.battery is not None:
            text += f"  ·  {'⚡' if snap.plugged else '🔋'}{snap.battery}%"
        self.label.setText(text)
        self.mini.setVisible(job is not None)
        self.mini.setValue(job.progress if job else 0)
        self.pause_btn.setText("▶" if jobs.paused else "⏸")
        self.pause_btn.setToolTip("Resume background AI work" if jobs.paused else "Pause background AI work")
        if self.popup and self.popup.isVisible():
            self.popup.refresh()


class ActivityPopup(QFrame):
    """Details: current job, queue, resource use, battery rules."""

    def __init__(self, indicator: ActivityIndicator):
        super().__init__(indicator, Qt.WindowType.Popup)
        self.setObjectName("activityPopup")
        self.indicator = indicator
        self.jobs, self.settings = indicator.jobs, indicator.settings
        self.setMinimumWidth(380)

        title = QLabel("Local AI activity")
        title.setObjectName("cardTitle")
        self.pause_btn = QPushButton()
        self.pause_btn.clicked.connect(lambda: (indicator.toggle_pause(), self.refresh()))
        head = QHBoxLayout()
        head.addWidget(title, 1)
        head.addWidget(self.pause_btn)

        self.job_label = QLabel(wordWrap=True)
        self.job_bar = QProgressBar(maximum=100)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(lambda: self.jobs.current and self.jobs.cancel(self.jobs.current))
        job_row = QHBoxLayout()
        job_row.addWidget(self.job_bar, 1)
        job_row.addWidget(self.cancel_btn)
        self.queue_label = QLabel(wordWrap=True)
        self.queue_label.setObjectName("muted")
        self.resources = QLabel()
        self.resources.setTextFormat(Qt.TextFormat.RichText)
        self.history = QLabel(wordWrap=True)
        self.history.setObjectName("muted")

        lm_title = QLabel("LM Studio models")
        lm_title.setObjectName("cardTitle")
        self.unload_all_btn = QPushButton("Unload all")
        self.unload_all_btn.clicked.connect(lambda: (indicator.models.unload_all(), self.refresh()))
        lm_head = QHBoxLayout()
        lm_head.addWidget(lm_title, 1)
        lm_head.addWidget(self.unload_all_btn)
        self.lm_rows = QVBoxLayout()
        self.lm_rows.setSpacing(4)
        self.auto_unload = QCheckBox("Unload after")
        self.auto_unload.setChecked(self.settings.local_llm_unload_after > 0)
        self.unload_minutes = QSpinBox(minimum=1, maximum=120, suffix=" min without AI work")
        self.unload_minutes.setValue(max(1, (self.settings.local_llm_unload_after or 300) // 60))
        self.auto_unload.toggled.connect(self._save_unload)
        self.unload_minutes.valueChanged.connect(self._save_unload)
        unload_row = QHBoxLayout()
        unload_row.addWidget(self.auto_unload)
        unload_row.addWidget(self.unload_minutes)
        unload_row.addStretch()
        self._lm_signature = None

        self.low_battery = QCheckBox("Pause on battery below")
        self.low_battery.setChecked(self.settings.battery_pause_below > 0)
        self.threshold = QSpinBox(minimum=5, maximum=95, singleStep=5, suffix=" %")
        self.threshold.setValue(self.settings.battery_pause_below or 20)
        self.only_charging = QCheckBox("Only run while charging")
        self.only_charging.setChecked(self.settings.only_when_charging)
        for widget in (self.low_battery, self.only_charging):
            widget.toggled.connect(self._save_rules)
        self.threshold.valueChanged.connect(self._save_rules)
        battery_row = QHBoxLayout()
        battery_row.addWidget(self.low_battery)
        battery_row.addWidget(self.threshold)
        battery_row.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        layout.addLayout(head)
        layout.addWidget(self.job_label)
        layout.addLayout(job_row)
        layout.addWidget(self.queue_label)
        layout.addWidget(_rule())
        layout.addWidget(self.resources)
        layout.addWidget(_rule())
        layout.addLayout(lm_head)
        layout.addLayout(self.lm_rows)
        layout.addLayout(unload_row)
        layout.addWidget(_rule())
        layout.addLayout(battery_row)
        layout.addWidget(self.only_charging)
        layout.addWidget(_rule())
        layout.addWidget(self.history)
        self.refresh()

    def _save_rules(self) -> None:
        self.settings.battery_pause_below = self.threshold.value() if self.low_battery.isChecked() else 0
        self.settings.only_when_charging = self.only_charging.isChecked()
        self.threshold.setEnabled(self.low_battery.isChecked())
        self.settings.save()
        self.indicator._apply_battery_rules()
        self.indicator.refresh()

    def _save_unload(self) -> None:
        self.settings.local_llm_unload_after = self.unload_minutes.value() * 60 if self.auto_unload.isChecked() else 0
        self.unload_minutes.setEnabled(self.auto_unload.isChecked())
        self.settings.save()

    def _refresh_models(self) -> None:
        keeper = self.indicator.models
        instances = keeper.instances
        signature = (
            None
            if instances is None
            else tuple(
                (i.instance_id, i.instance_id in keeper.busy_unloading, i.ttl_left, i.instance_id in keeper.used)
                for i in instances
            )
        )
        self.unload_all_btn.setEnabled(bool(instances))
        if signature == self._lm_signature:
            return
        self._lm_signature = signature
        while self.lm_rows.count():
            item = self.lm_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if instances is None:
            self.lm_rows.addWidget(_muted("LM Studio is not running (or not reachable)."))
            return
        if not instances:
            self.lm_rows.addWidget(_muted("No model loaded — LM Studio is idle and uses no model memory."))
            return
        for inst in instances:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            used = " · used by UniVerse" if inst.instance_id in keeper.used else ""
            if inst.ttl_left is not None:
                stay = f"auto-unload in {_fmt_time(inst.ttl_left)}"
            elif inst.instance_id in keeper.used and self.settings.local_llm_unload_after:
                stay = "UniVerse unloads it when idle"
            else:
                stay = "<span style='color:#c27c0e'>stays loaded (no timer)</span>"
            label = QLabel(f"<b>{inst.name}</b> · {inst.size_gb:.1f} GB{used}<br>{stay}", wordWrap=True)
            button = QPushButton("Unloading…" if inst.instance_id in keeper.busy_unloading else "Unload")
            button.setEnabled(inst.instance_id not in keeper.busy_unloading)
            button.clicked.connect(lambda _=False, i=inst.instance_id: (keeper.unload(i, reason="unloaded by you"), self.refresh()))
            line.addWidget(label, 1)
            line.addWidget(button)
            self.lm_rows.addWidget(row)

    def refresh(self) -> None:
        self._refresh_models()
        jobs, job, snap = self.jobs, self.jobs.current, self.indicator.snapshot
        self.pause_btn.setText("▶ Resume" if jobs.paused else "⏸ Pause all")
        if job:
            state = {"paused": "paused", "running": "running"}.get(job.status, job.status)
            eta = job.eta()
            pause_note = ""
            if job.status == "paused" and job.kind == "classify":
                pause_note = "<br><i>Stops after the current batch.</i>"
            self.job_label.setText(
                f"<b>{job.title}</b> — {state}, {_fmt_time(job.elapsed())} elapsed"
                + (f", about {_fmt_time(eta)} left" if eta else "")
                + pause_note
            )
            self.job_bar.setValue(job.progress)
        else:
            self.job_label.setText("<b>Nothing running.</b> Transcription and sorting start here after a recording.")
            self.job_bar.setValue(0)
        self.job_bar.setVisible(job is not None)
        self.cancel_btn.setVisible(job is not None)
        self.queue_label.setText(
            "Waiting: " + ", ".join(j.title for j in jobs.queue) if jobs.queue else "Queue is empty."
        )

        rows = []
        if snap.cpu is not None:
            rows.append(f"CPU (whole Mac): <b>{_bar(snap.cpu)}</b>")
            rows.append(f"GPU: <b>{_bar(snap.gpu)}</b>" + (f" · {snap.gpu_mem_gb:.1f} GB in GPU memory (models)" if snap.gpu_mem_gb is not None else ""))
            rows.append(f"RAM: <b>{_bar(snap.ram_used)}</b> of {snap.ram_total_gb:.0f} GB")
            for proc in snap.processes:
                rows.append(f"&nbsp;&nbsp;{proc.name}: {proc.cpu:.0f}% CPU · {proc.ram_gb:.2f} GB")
        elif not SystemMonitor.available():
            rows.append("Install <code>psutil</code> to see CPU, GPU and memory use.")
        if snap.battery is not None:
            rows.append(f"Battery: <b>{snap.battery}%</b> · {'charging / plugged in' if snap.plugged else 'on battery'}")
        self.resources.setText("<br>".join(rows) or "Measuring…")

        done = [
            f"{'✓' if j.status == 'done' else '✗' if j.status == 'failed' else '–'} {j.title} ({_fmt_time(j.elapsed())})"
            for j in jobs.history
        ]
        self.history.setText("Recent: " + " · ".join(done) if done else "")
        self.history.setVisible(bool(done))
        self.adjustSize()


def _muted(text: str) -> QLabel:
    label = QLabel(text, wordWrap=True)
    label.setObjectName("muted")
    return label


def _rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setObjectName("rule")
    return line
