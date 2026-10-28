"""Pause / resume / cancel for long-running background work.

A :class:`JobControl` is handed to a running job. The job calls
:meth:`checkpoint` between steps (it blocks while paused) and registers child
processes such as whisper.cpp, which are frozen with SIGSTOP on pause and
continued with SIGCONT — so a transcription really stops using CPU/GPU.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading


class Cancelled(Exception):
    pass


class JobControl:
    def __init__(self):
        self._running = threading.Event()
        self._running.set()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()
        self.cancelled = False

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def checkpoint(self) -> None:
        """Call between steps: waits while paused, raises :class:`Cancelled` if cancelled."""
        self._running.wait()
        if self.cancelled:
            raise Cancelled()

    def register_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes.add(proc)
            if self.paused:
                _signal(proc, signal.SIGSTOP)

    def unregister_process(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes.discard(proc)

    def pause(self) -> None:
        with self._lock:
            self._running.clear()
            for proc in self._processes:
                _signal(proc, signal.SIGSTOP)

    def resume(self) -> None:
        with self._lock:
            for proc in self._processes:
                _signal(proc, signal.SIGCONT)
            self._running.set()

    def cancel(self) -> None:
        self.cancelled = True
        with self._lock:
            for proc in self._processes:
                _signal(proc, signal.SIGCONT)
                _signal(proc, signal.SIGTERM)
        self._running.set()


def _signal(proc: subprocess.Popen, sig: int) -> None:
    if proc.poll() is None:
        try:
            os.kill(proc.pid, sig)
        except ProcessLookupError:
            pass
