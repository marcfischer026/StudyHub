"""CPU, GPU, memory and battery readings for the activity indicator.

Uses ``psutil`` plus macOS ``ioreg`` for the Apple-Silicon GPU, so no admin
rights are needed. Every reading degrades to ``None`` instead of failing.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

try:
    import psutil
except ImportError:  # optional: the indicator then only shows job progress
    psutil = None

_GPU_UTIL = re.compile(r'"Device Utilization %"\s*=\s*(\d+)')
_GPU_MEM = re.compile(r'"In use system memory"\s*=\s*(\d+)')
# Local model servers: LM Studio ("Bionic" from ~/.lmstudio), Ollama, llama.cpp, mlx_lm.
_AI_SERVER_HINTS = (".lmstudio", "ollama", "llama-server", "llama-cli", "mlx_lm")
GiB = 2**30


@dataclass
class ProcessUsage:
    name: str
    cpu: float  # percent of one core (can exceed 100)
    ram_gb: float


@dataclass
class Snapshot:
    cpu: float | None = None  # whole system, 0–100
    ram_used: float | None = None  # whole system, percent
    ram_total_gb: float | None = None
    gpu: float | None = None  # Apple GPU utilisation, 0–100
    gpu_mem_gb: float | None = None  # unified memory held by the GPU (models live here)
    battery: int | None = None  # percent, None on desktops
    plugged: bool | None = None
    processes: list[ProcessUsage] = field(default_factory=list)  # UniVerse, whisper, AI server


class SystemMonitor:
    def __init__(self):
        self._procs: dict[int, "psutil.Process"] = {}
        if psutil is not None:
            psutil.cpu_percent()  # first call primes the counter

    @staticmethod
    def available() -> bool:
        return psutil is not None

    def battery(self) -> tuple[int | None, bool | None]:
        if psutil is None:
            return None, None
        try:
            info = psutil.sensors_battery()
        except Exception:
            return None, None
        if info is None:
            return None, None
        return int(round(info.percent)), bool(info.power_plugged)

    def snapshot(self, with_gpu: bool = True) -> Snapshot:
        snap = Snapshot()
        snap.battery, snap.plugged = self.battery()
        if psutil is None:
            return snap
        snap.cpu = psutil.cpu_percent()
        memory = psutil.virtual_memory()
        snap.ram_used, snap.ram_total_gb = memory.percent, memory.total / GiB
        if with_gpu:
            snap.gpu, snap.gpu_mem_gb = _gpu()
        snap.processes = self._watched_processes()
        return snap

    def _watched_processes(self) -> list[ProcessUsage]:
        groups: dict[str, list] = {"UniVerse": [], "whisper.cpp": [], "Local AI server": []}
        me = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            info = proc.info
            name, exe = (info["name"] or ""), (info["exe"] or "")
            if info["pid"] == me:
                groups["UniVerse"].append(proc)
            elif name.startswith("whisper"):
                groups["whisper.cpp"].append(proc)
            elif any(h in exe.lower() or h in name.lower() for h in _AI_SERVER_HINTS):
                groups["Local AI server"].append(proc)
        usage = []
        for label, procs in groups.items():
            cpu = ram = 0.0
            for proc in procs:
                # Reuse Process objects: cpu_percent() needs a previous call to measure against.
                tracked = self._procs.setdefault(proc.pid, proc)
                try:
                    cpu += tracked.cpu_percent()
                    ram += tracked.memory_info().rss / GiB
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self._procs.pop(proc.pid, None)
            if procs:
                usage.append(ProcessUsage(label, cpu, ram))
        alive = {p.pid for group in groups.values() for p in group}
        for pid in list(self._procs):
            if pid not in alive:
                del self._procs[pid]
        return usage


def _gpu() -> tuple[float | None, float | None]:
    try:
        out = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], capture_output=True, text=True, timeout=2
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    util, mem = _GPU_UTIL.search(out), _GPU_MEM.search(out)
    return (float(util.group(1)) if util else None, int(mem.group(1)) / GiB if mem else None)
