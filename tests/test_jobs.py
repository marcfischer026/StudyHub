"""Tests for pausing background work: process freezing, queue, battery rules."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("UNIVERSE_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import psutil  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import universe  # noqa: E402,F401
from universe.config import Settings  # noqa: E402
from universe.services.jobcontrol import Cancelled, JobControl  # noqa: E402
from universe.services.system_monitor import Snapshot  # noqa: E402
from universe.ui.jobs import ActivityIndicator, JobManager  # noqa: E402

app = QApplication.instance() or QApplication([])


def wait_until(condition, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        loop = QEventLoop()
        QTimer.singleShot(20, loop.quit)
        loop.exec()
    return condition()


class JobControlTests(unittest.TestCase):
    def test_pause_really_freezes_a_child_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "while True: pass"])
        control = JobControl()
        try:
            control.register_process(proc)
            control.pause()
            time.sleep(0.2)
            self.assertEqual(psutil.Process(proc.pid).status(), psutil.STATUS_STOPPED)
            control.resume()
            time.sleep(0.2)
            self.assertNotEqual(psutil.Process(proc.pid).status(), psutil.STATUS_STOPPED)
            control.cancel()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_checkpoint_blocks_while_paused_and_raises_on_cancel(self):
        control = JobControl()
        control.pause()
        passed = threading.Event()
        thread = threading.Thread(target=lambda: (control.checkpoint(), passed.set()))
        thread.start()
        time.sleep(0.1)
        self.assertFalse(passed.is_set())
        control.resume()
        thread.join(1)
        self.assertTrue(passed.is_set())
        control.cancel()
        with self.assertRaises(Cancelled):
            control.checkpoint()


class JobManagerTests(unittest.TestCase):
    def test_runs_one_at_a_time_and_waits_while_paused(self):
        jobs = JobManager()
        order, results = [], []

        def make(name):
            def fn(progress, control):
                order.append(name)
                for pct in (50, 100):
                    control.checkpoint()
                    progress(pct)
                return name

            return fn

        jobs.pause("manual")
        jobs.submit("transcribe", "A", make("A"), results.append, key="A")
        self.assertIsNone(jobs.submit("transcribe", "A again", make("A"), key="A"))  # de-duplicated
        jobs.submit("classify", "B", make("B"), results.append)
        time.sleep(0.2)
        self.assertEqual(order, [])  # paused: nothing starts
        jobs.resume()
        self.assertTrue(wait_until(lambda: results == ["A", "B"]))
        self.assertEqual(order, ["A", "B"])
        self.assertEqual([j.status for j in jobs.history], ["done", "done"])

    def test_cancel_running_job(self):
        jobs = JobManager()
        started = threading.Event()

        def slow(progress, control):
            started.set()
            while True:
                control.checkpoint()
                time.sleep(0.01)

        job = jobs.submit("classify", "slow", slow)
        self.assertTrue(started.wait(2))
        jobs.cancel(job)
        self.assertTrue(wait_until(lambda: job.status == "cancelled"))
        self.assertFalse(jobs.busy)


class BatteryRuleTests(unittest.TestCase):
    def make(self, **settings):
        jobs = JobManager()
        indicator = ActivityIndicator(jobs, Settings(**settings))
        indicator.timer.stop()
        return jobs, indicator

    def set_battery(self, indicator, pct, plugged):
        indicator.snapshot = Snapshot(battery=pct, plugged=plugged)
        indicator._apply_battery_rules()

    def test_low_battery_pauses_and_charging_resumes(self):
        jobs, indicator = self.make(battery_pause_below=20)
        self.set_battery(indicator, 15, False)
        self.assertEqual(jobs.pause_reason, "battery")
        self.set_battery(indicator, 15, True)
        self.assertFalse(jobs.paused)

    def test_only_while_charging(self):
        jobs, indicator = self.make(only_when_charging=True, battery_pause_below=0)
        self.set_battery(indicator, 90, False)
        self.assertEqual(jobs.pause_reason, "unplugged")
        self.set_battery(indicator, 90, True)
        self.assertFalse(jobs.paused)

    def test_manual_pause_is_not_undone_by_battery_rules(self):
        jobs, indicator = self.make(battery_pause_below=20)
        jobs.pause("manual")
        self.set_battery(indicator, 80, True)
        self.assertEqual(jobs.pause_reason, "manual")


class ModelKeeperTests(unittest.TestCase):
    """LM Studio is simulated: one loaded instance, unload recorded."""

    def setUp(self):
        from universe.services import lmstudio, local_llm

        self.lmstudio, self.local_llm = lmstudio, local_llm
        self.loaded = {"qwen3-14b-mlx"}
        self.unloaded = []
        self.originals = (local_llm.find_server, lmstudio.loaded_instances, lmstudio.unload)
        local_llm.find_server = lambda settings: ("http://fake:8080/v1", "qwen3-14b-mlx")
        lmstudio.loaded_instances = lambda root, timeout=1.5: [
            lmstudio.Instance(i, i, "Qwen3 14B", "llm", 8.3, 35072) for i in sorted(self.loaded)
        ]

        def fake_unload(root, instance_id, timeout=30):
            self.unloaded.append((root, instance_id))
            self.loaded.discard(instance_id)

        lmstudio.unload = fake_unload

    def tearDown(self):
        self.local_llm.find_server, self.lmstudio.loaded_instances, self.lmstudio.unload = self.originals

    def test_unloads_model_after_idle_time_but_not_while_busy(self):
        from universe.ui.jobs import ModelKeeper

        jobs = JobManager()
        keeper = ModelKeeper(jobs, Settings(local_llm_unload_after=1))
        keeper.timer.stop()
        self.assertTrue(wait_until(lambda: keeper.instances is not None))
        self.assertAlmostEqual(keeper.loaded_gb, 8.3)
        keeper.used["qwen3-14b-mlx"] = time.monotonic() - 5  # UniVerse used it 5 s ago

        jobs.queue.append(object())  # pretend a job is waiting → must not unload
        keeper.poll()
        wait_until(lambda: not keeper._polling)
        self.assertEqual(self.unloaded, [])
        jobs.queue.clear()

        keeper.used["qwen3-14b-mlx"] = time.monotonic() - 5
        keeper.poll()
        self.assertTrue(wait_until(lambda: self.unloaded == [("http://fake:8080", "qwen3-14b-mlx")]))
        self.assertTrue(wait_until(lambda: keeper.instances == []))

    def test_models_used_by_others_are_left_alone(self):
        from universe.ui.jobs import ModelKeeper

        keeper = ModelKeeper(JobManager(), Settings(local_llm_unload_after=1))
        keeper.timer.stop()
        self.assertTrue(wait_until(lambda: keeper.instances is not None))
        time.sleep(0.05)
        keeper.poll()
        wait_until(lambda: not keeper._polling)
        self.assertEqual(self.unloaded, [])  # never used by UniVerse → not touched automatically


if __name__ == "__main__":
    unittest.main()
