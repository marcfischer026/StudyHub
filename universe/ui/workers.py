"""Run blocking service calls off the UI thread."""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..services.jobcontrol import Cancelled


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int)


class Worker(QRunnable):
    """Calls ``fn(*args, **kwargs)``; pass ``with_progress=True`` to receive a
    ``progress`` callable as keyword argument."""

    def __init__(self, fn: Callable[..., Any], *args, with_progress: bool = False, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = WorkerSignals()
        if with_progress:
            self.kwargs["progress"] = self.signals.progress.emit

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Cancelled:
            self.signals.failed.emit("Cancelled")
        except Exception as exc:  # surfaced to the user via `failed`
            traceback.print_exc()
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.finished.emit(result)


# Indexing writes to LanceDB, so it runs strictly one job at a time.
index_pool = QThreadPool()
index_pool.setMaxThreadCount(1)


def run(worker: Worker, pool: QThreadPool | None = None) -> Worker:
    (pool or QThreadPool.globalInstance()).start(worker)
    return worker
