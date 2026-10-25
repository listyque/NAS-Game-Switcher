"""Real Qt frame/timer/signal wiring; no Windows processes or device effects."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

from ngs.portable import PortableStorage
from ngs.update_health import watch_first_start


class StartupSignals(QObject):
    aboutToQuit = Signal()
    readyChanged = Signal()


class QtUpdateHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = PortableStorage(self.temporary.name, resources=Path(self.temporary.name) / 'runtime')
        self.storage.resources.mkdir()
        self.version = '2026.10.21.130826'
        (self.storage.resources / 'release.json').write_text(json.dumps(dict(version=self.version)))
        self.base = self.storage.data / 'updates'
        self.base.mkdir()
        self.pending = self.base / 'first-start.json'
        self.marker = dict(schema=1, version=self.version, token='ab' * 16)
        self.pending.write_text(json.dumps(self.marker))
        self.window = QQuickWindow()
        self.window.resize(160, 100)
        self.signals = StartupSignals()
        self.now = 100
        self.ready = True
        self.closing = False
        self.confirmed = Mock()

    def tearDown(self):
        self.signals.aboutToQuit.emit()
        self.window.hide()
        self.window.deleteLater()
        self.signals.deleteLater()
        self.application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.application.processEvents()

    def watch(self):
        health = watch_first_start(self.storage, self.window, self.signals,
            ready=lambda: self.ready, closing=lambda: self.closing,
            ready_changed=self.signals.readyChanged, confirmed=self.confirmed)
        if health is not None:
            health.clock = lambda: self.now
        return health

    def render_frame(self, health):
        self.window.show()
        deadline = time.monotonic() + 2
        while health.frame_at is None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertEqual(health.frame_at, 100, 'The offscreen QQuickWindow must actually render its first frame')

    def test_real_rendered_frame_is_required_before_grace_and_ready_signal(self):
        health = self.watch()
        self.now = 200
        self.signals.readyChanged.emit()
        self.assertTrue(self.pending.exists())
        self.assertIsNone(health.frame_at)
        self.now = 100
        self.render_frame(health)
        self.now = 109
        self.signals.readyChanged.emit()
        self.assertTrue(self.pending.exists())
        self.now = 110
        self.signals.readyChanged.emit()
        self.assertFalse(self.pending.exists())
        self.confirmed.assert_called_once_with()

    def test_late_uac_readiness_confirms_after_polling_deadline_without_new_timer(self):
        self.ready = False
        health = self.watch()
        self.render_frame(health)
        timer = self.window._ngs_startup_health[1]
        self.now = 170
        with self.assertLogs(level='WARNING'):
            self.signals.readyChanged.emit()
        self.assertFalse(timer.isActive())
        self.assertTrue(self.pending.exists())
        self.now = 400
        self.ready = True
        self.signals.readyChanged.emit()
        self.assertFalse(self.pending.exists())
        self.confirmed.assert_called_once_with()

    def test_explicit_close_before_grace_defers_without_confirming_or_late_callbacks(self):
        health = self.watch()
        self.render_frame(health)
        self.now = 104
        self.closing = True
        with patch('ngs.update_health._signal_orderly_close', return_value=True) as notify:
            self.signals.aboutToQuit.emit()
            notify.assert_called_once_with()
        self.now = 500
        self.signals.readyChanged.emit()
        self.window.frameSwapped.emit()
        self.application.processEvents()
        self.assertTrue(self.pending.exists())
        self.assertFalse(health.confirmed.exists())
        self.confirmed.assert_not_called()

    def test_unexpected_qt_shutdown_does_not_claim_an_intentional_close(self):
        self.watch()
        with patch('ngs.update_health._signal_orderly_close') as notify:
            self.signals.aboutToQuit.emit()
            notify.assert_not_called()
        self.window.frameSwapped.emit()
        self.application.processEvents()
        self.assertTrue(self.pending.exists())
        self.confirmed.assert_not_called()

    def test_changed_token_cannot_be_confirmed_by_a_queued_readiness_signal(self):
        health = self.watch()
        self.render_frame(health)
        self.now = 110
        later = dict(self.marker, token='cd' * 16)
        self.pending.write_text(json.dumps(later))
        with self.assertLogs(level='ERROR'):
            self.signals.readyChanged.emit()
        self.assertEqual(json.loads(self.pending.read_text()), later)
        self.confirmed.assert_not_called()

    def test_wrong_version_or_missing_pending_installs_no_qt_monitor(self):
        self.marker['version'] = '2026.10.20.130826'
        self.pending.write_text(json.dumps(self.marker))
        with self.assertLogs(level='ERROR'):
            self.assertIsNone(self.watch())
        self.assertFalse(hasattr(self.window, '_ngs_startup_health'))
        self.pending.unlink()
        self.assertIsNone(self.watch())
        self.assertFalse(hasattr(self.window, '_ngs_startup_health'))


if __name__ == '__main__':
    unittest.main()
