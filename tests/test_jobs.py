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
from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
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

    def test_deleting_the_keeper_during_a_poll_does_not_hang(self):
        """Its destructor runs with the GIL held; a pool it owned would wait there for the
        poll, which needs the GIL: the whole app froze when a window went away at that moment."""
        from PySide6.QtTest import QTest

        from universe.ui.jobs import ModelKeeper, poll_pool

        started, release = threading.Event(), threading.Event()

        def slow(settings):
            started.set()
            release.wait(5)
            time.sleep(0.2)  # still running when the keeper is deleted
            return "http://fake:8080/v1", "qwen3-14b-mlx"

        self.local_llm.find_server = slow
        keeper = ModelKeeper(JobManager(), Settings(local_llm_unload_after=1))
        keeper.timer.stop()
        self.assertTrue(wait_until(lambda: started.is_set()))
        keeper.deleteLater()
        release.set()
        QTest.qWait(50)  # runs the deletion, as a window closed under the event loop does
        self.assertTrue(poll_pool.waitForDone(5000))
        QApplication.processEvents()  # the late result reaches a keeper that is gone: no error


if __name__ == "__main__":
    unittest.main()


class UsageChartTests(unittest.TestCase):
    def test_history_keeps_full_readings_only(self):
        from universe.ui.usage_chart import UsageHistory

        history = UsageHistory()
        history.add(Snapshot(battery=80, plugged=True))  # idle: battery only
        history.add(Snapshot(cpu=30, gpu=None, ram_used=60))
        self.assertEqual(len(history.readings), 1)
        self.assertEqual(history.latest().cpu, 30)

    def test_lines_break_where_sampling_paused(self):
        from universe.ui.usage_chart import Reading, _runs

        readings = [Reading(t, 10, 5 if t != 4 else None, 50) for t in (0, 2, 4, 6, 30, 32)]
        self.assertEqual([len(r) for r in _runs(readings, "cpu")], [4, 2])
        self.assertEqual([len(r) for r in _runs(readings, "gpu")], [2, 1, 2])

    def test_popup_shows_chart_and_readouts_ease_to_values(self):
        jobs = JobManager()
        indicator = ActivityIndicator(jobs, Settings())
        indicator.timer.stop()
        now = time.monotonic()
        for i in range(30):
            indicator.history.add(Snapshot(cpu=20 + i, gpu=10, ram_used=55), t=now - 60 + 2 * i)
        indicator.snapshot = Snapshot(cpu=50, gpu=12, gpu_mem_gb=4.2, ram_used=55, ram_total_gb=16)
        from universe.ui.jobs import ActivityPopup

        popup = ActivityPopup(indicator)
        chart = popup.chart
        chart.resize(380, chart.sizeHint().height())
        chart.hover_x = 300
        self.assertFalse(chart.grab().isNull())  # paints tiles, lines and the hover label
        for _ in range(60):
            chart._tick()
        self.assertAlmostEqual(chart.shown["cpu"], 50, delta=0.5)

    def test_chart_paints_with_history_before_its_first_snapshot(self):
        from PySide6.QtGui import QImage, QPainter

        from universe.ui.usage_chart import UsageChart, UsageHistory, palette

        history = UsageHistory()
        now = time.monotonic()
        for i in range(5):
            history.add(Snapshot(cpu=20, gpu=10, ram_used=55), t=now - 10 + 2 * i)
        chart = UsageChart(history)  # no update_from yet: the total memory is unknown
        chart.resize(380, chart.sizeHint().height())
        chart.p = palette()
        image = QImage(chart.size(), QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            chart._paint_quotes(painter, *chart._fonts(), list(history.readings), now)
        finally:
            painter.end()

    def test_range_line_fits_its_column(self):
        from PySide6.QtGui import QFont, QFontMetricsF

        from universe.ui.usage_chart import _fit_stats

        metrics = QFontMetricsF(QFont("Menlo", 10))
        self.assertEqual(_fit_stats((12.5, 12.3, 12.4), 1, metrics, 1000), "H 12.5  L 12.3  Ø 12.4")
        narrow = _fit_stats((12.5, 12.3, 12.4), 1, metrics, metrics.horizontalAdvance("H 12.5  L 12.3"))
        self.assertEqual(narrow, "H 12.5  L 12.3")  # the average goes before anything gets cut
        self.assertEqual(_fit_stats(None, 0, metrics, 100), "")

    def test_range_and_the_growing_head_of_a_line(self):
        from universe.ui.usage_chart import Reading, _stats, _until

        readings = [Reading(0, 80, None, 50), Reading(40, 20, 5, 50), Reading(90, 35, 9, 50)]
        self.assertEqual(_stats(readings, "cpu"), (80, 20, 45))
        self.assertIsNone(_stats(readings[:1], "gpu"))
        # Half-way between two readings the line has grown half of the next segment.
        self.assertEqual(_until([(0, 10), (2, 30)], 1), [(0, 10), (1, 20.0)])
        self.assertEqual(_until([(0, 10), (2, 30)], 5), [(0, 10), (2, 30)])

    def test_popup_is_a_panel_in_the_window_and_closes_like_a_popover(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QKeyEvent, QMouseEvent
        from PySide6.QtWidgets import QMainWindow, QPushButton

        window = QMainWindow()
        window.resize(900, 700)
        outside = QPushButton("elsewhere")
        window.setCentralWidget(outside)
        indicator = ActivityIndicator(JobManager(), Settings())
        indicator.timer.stop()
        window.statusBar().addPermanentWidget(indicator)
        window.show()
        indicator.toggle_popup()
        popup = indicator.popup
        self.assertFalse(popup.isWindow())  # no window of its own to flicker or switch Spaces
        self.assertTrue(popup.isVisible())
        self.assertGreaterEqual(popup.y(), 0)
        app.sendEvent(popup, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        self.assertFalse(popup.isVisible())
        indicator.toggle_popup()
        popup = indicator.popup
        press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(5, 5), outside.mapToGlobal(QPointF(5, 5)),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        app.sendEvent(outside, press)
        self.assertFalse(popup.isVisible())
        window.close()
