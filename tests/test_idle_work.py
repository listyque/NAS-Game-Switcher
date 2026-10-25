"""Launcher idle/power regressions; real Qt timers, no games or hardware actions."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtTest import QTest

from ngs.app import LauncherBridge
from ngs.play_time import PlayTime
from ngs.portable import PortableStorage


class IdleWorkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ngs-idle-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.storage = PortableStorage(Path(self.temporary.name))
        # Only the Windows queue-lock boundary is replaced. These tests exercise
        # bridge lifecycle/timers; queue persistence has its own regression suite.
        self.queue_patch = patch('ngs.app.DownloadQueue')
        self.queue = self.queue_patch.start().return_value
        self.queue.get.return_value = dict(progress=0)
        self.queue.jobs.return_value = []
        self.addCleanup(self.queue_patch.stop)
        self.bridges = []

    def bridge(self):
        bridge = LauncherBridge(self.storage, demo=True)
        self.bridges.append(bridge)
        return bridge

    def tearDown(self):
        for bridge in self.bridges:
            bridge.game_worker = bridge.process = None
            bridge._closing = True
            bridge.stop()
            bridge.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def transfer(self, bridge):
        bridge._io_active = True
        bridge.game_worker = SimpleNamespace(cancel=threading.Event(), mode='install')
        bridge.sync_download_timer()

    def test_idle_launcher_has_no_one_second_download_wakeup(self):
        bridge = self.bridge()
        wakes = []
        bridge.download_timer.timeout.connect(lambda: wakes.append(1))
        self.assertFalse(bridge.download_timer.isActive())
        QTest.qWait(1150)
        self.assertEqual(wakes, [])

    def test_transfer_sampling_tracks_visibility_without_cancelling_worker(self):
        bridge = self.bridge()
        self.transfer(bridge)
        worker = bridge.game_worker
        self.assertTrue(bridge.download_timer.isActive())
        bridge.set_ui_active(False)
        self.assertFalse(bridge.download_timer.isActive())
        bridge._io_bytes = [2_000_000, 3_000_000]
        bridge.set_ui_active(True)
        self.assertTrue(bridge.download_timer.isActive())
        self.assertEqual(bridge._io_sample, bridge._io_bytes)
        self.assertIsNot(bridge._io_sample, bridge._io_bytes)
        self.assertIs(bridge.game_worker, worker)
        self.assertFalse(worker.cancel.is_set())

    def test_cancelled_transfer_publishes_zero_once_and_stops_sampling(self):
        bridge = self.bridge()
        self.transfer(bridge)
        bridge._download_statistics.update(network=100, disk=200)
        changed = []
        bridge.downloadStatisticsChanged.connect(lambda: changed.append(1))
        bridge.game_worker.cancel.set()
        bridge.sample_download_io()
        self.assertFalse(bridge.download_timer.isActive())
        self.assertEqual((bridge._download_statistics['network'], bridge._download_statistics['disk']), (0, 0))
        bridge.sample_download_io()
        self.assertEqual(changed, [1])

    def test_real_operation_lifecycle_starts_and_finishes_sampling(self):
        bridge = self.bridge()
        worker = Mock(mode='install')
        worker.cancel = threading.Event()
        with patch('ngs.app.GameWorker', return_value=worker):
            bridge.start_operation(dict(id='fixture', name='Fixture'), 'install')
        worker.start.assert_called_once()
        self.assertTrue(bridge.download_timer.isActive())
        bridge._closing = True
        bridge.operation_finished()
        self.assertIsNone(bridge.game_worker)
        self.assertFalse(bridge.download_timer.isActive())
        worker.deleteLater.assert_called_once()

    def test_hidden_statistics_keep_play_time_but_do_not_notify_each_second(self):
        bridge = self.bridge()
        clock = [0.0]
        bridge.play_time = PlayTime(bridge.library.path, lambda: clock[0])
        bridge.play_time.start('fixture')
        bridge.set_ui_active(False)
        notifications = []
        bridge.runningChanged.connect(lambda: notifications.append(1))
        for second in range(1, 66):
            clock[0] = float(second)
            bridge.update_running()
        self.assertEqual(notifications, [])
        self.assertEqual(bridge.play_time.snapshot()['session_seconds'], 65)
        bridge.set_ui_active(True)
        self.assertEqual(notifications, [1])
        bridge.set_ui_active(True)
        self.assertEqual(notifications, [1])

    def test_forced_statistics_transitions_notify_even_when_hidden(self):
        bridge = self.bridge()
        bridge.set_ui_active(False)
        notifications = []
        bridge.runningChanged.connect(lambda: notifications.append(1))
        with patch.object(bridge.players, 'sync') as sync:
            bridge.update_running(force=True)
            bridge.update_running(reason='completed')
        self.assertEqual(notifications, [1, 1])
        sync.assert_called_once()

    def test_sleep_and_wake_suspend_only_periodic_work(self):
        bridge = self.bridge()
        self.transfer(bridge)
        worker = bridge.game_worker
        bridge._demo = False
        bridge._automatic_updates = True
        bridge.process = Mock(frozen=False)
        bridge.update_timer.start(300000)
        bridge.offline_timer.start()
        bridge.process_timer.start()
        bridge._sleeping = True
        bridge.frozen_updated()
        timers = (bridge.download_timer, bridge.update_timer, bridge.offline_timer,
                  bridge.process_timer, bridge.players.timer)
        self.assertTrue(all(not timer.isActive() for timer in timers))
        self.assertFalse(worker.cancel.is_set())
        bridge.resume_play_clock()
        self.assertTrue(all(timer.isActive() for timer in timers))
        self.assertEqual(bridge.update_timer.interval(), 3500)
        self.assertEqual(bridge.players.timer.interval(), 60000)

    def test_failed_freeze_still_stops_periodic_work_during_sleep(self):
        bridge = self.bridge()
        self.transfer(bridge)
        bridge._sleeping = True
        bridge.power_failed('fixture freeze failure')
        self.assertFalse(bridge.download_timer.isActive())
        self.assertFalse(bridge.players.timer.isActive())
        self.assertEqual(bridge.powerError, 'fixture freeze failure')

    def test_update_check_does_not_launch_a_worker_during_sleep(self):
        bridge = self.bridge()
        bridge._automatic_updates = True
        bridge._sleeping = True
        with patch('ngs.app.UpdateWorker') as worker:
            bridge.checkUpdates()
        worker.assert_not_called()
        self.assertFalse(bridge.update_timer.isActive())

    def test_malformed_update_result_types_do_not_crash_startup(self):
        path = self.storage.data / 'updates/result.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        for value in ([], None, 1, 'installed', {'status': 'installed', 'version': []},
                      {'status': 'failed', 'message': {}}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding='utf8')
                bridge = self.bridge()
                self.assertIsInstance(bridge.updatesState['status'], str)
                self.assertEqual(json.loads(path.read_text()), value)

    def test_pending_first_start_blocks_replacing_recovery_backup(self):
        directory = self.storage.data / 'updates'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'first-start.json').write_text('{}')
        (directory / 'result.json').write_text(json.dumps(dict(status='pending_first_start', version='2')))
        bridge = self.bridge()
        bridge._update.update(available=True, current='1.0.0.0')
        self.assertFalse(bridge.updatesState['can_install'])
        self.assertIn('первый запуск', bridge.updatesState['status'])
        bridge._first_start_pending = False
        self.assertTrue(bridge.updatesState['can_install'])

    def test_window_close_is_marked_before_first_start_acknowledgement(self):
        bridge = self.bridge()
        self.assertFalse(bridge._closing)
        event = QCloseEvent()
        bridge.eventFilter(bridge, event)
        self.assertTrue(bridge._closing)
        self.assertTrue(event.isAccepted())


if __name__ == '__main__':
    unittest.main()
