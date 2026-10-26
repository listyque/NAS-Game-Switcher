import copy
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject, QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui


class SettingsRestartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.bridge = self.engine.bridge
        self.saved = copy.deepcopy(self.bridge.settings)
        self.bridge._restart_enabled = True
        self.bridge.settings.update(graphics_api=self.bridge._boot_graphics[0],
                                    disable_performance_overlays=self.bridge._boot_graphics[1],
                                    production_mode=self.bridge._boot_production)
        self.storage.write_settings(self.bridge.settings)
        self.restart = patch.object(self.bridge, 'restartLauncher').start()

    def tearDown(self):
        self.bridge.restart_timer.stop()
        self.bridge._restart_enabled = False
        self.bridge._restart_pending = False
        self.bridge._launching = False
        self.bridge.process = None
        self.bridge.settings = self.saved
        self.storage.write_settings(self.saved)
        self.bridge.settingsChanged.emit()
        patch.stopall()

    def wait_restart(self):
        QTest.qWait(200)
        self.restart.assert_called_once_with()

    def test_quick_graphics_and_overlay_changes_restart_after_save(self):
        b = self.bridge
        for api, overlays in [('software', b._boot_graphics[1]), b._boot_graphics,
                              (b._boot_graphics[0], not b._boot_graphics[1])]:
            b.setGraphicsOptions(*b._boot_graphics)
            self.restart.reset_mock()
            self.assertTrue(b.setGraphicsOptions(api, overlays))
            if (api, overlays) == b._boot_graphics:
                QTest.qWait(200); self.restart.assert_not_called()
            else:
                self.assertEqual(self.storage.read_settings()['graphics_api'], api)
                self.assertEqual(self.storage.read_settings()['disable_performance_overlays'], overlays)
                self.wait_restart()

    def test_full_apply_restarts_for_production_and_graphics(self):
        b = self.bridge
        for patch_values in ({'production_mode': not b._boot_production}, {'graphics_api': 'software'}):
            b.settings.update(production_mode=b._boot_production, graphics_api=b._boot_graphics[0])
            self.restart.reset_mock()
            self.assertTrue(b.applySettings(dict(b.settingsState, **patch_values)), b.settingsStatus)
            self.wait_restart()

    def test_game_and_pending_launch_defer_then_retry_without_touching_game(self):
        b = self.bridge
        game = Mock()
        b.process = game
        self.assertTrue(b.setQuickOptions({'production_mode': not b._boot_production}))
        QTest.qWait(200); self.restart.assert_not_called(); game.assert_not_called()
        self.assertIn('после выхода из игры', b.settingsStatus)
        from ngs.app import LauncherBridge
        with patch.object(self.app, 'exit') as exit_app:
            self.assertFalse(LauncherBridge.restartLauncher(b))
            exit_app.assert_not_called()
        b.process = None; b._launching = True; b.runningChanged.emit()
        QTest.qWait(200); self.restart.assert_not_called()
        b._launching = False; b.runningChanged.emit()
        self.wait_restart()
        self.assertFalse(game.mock_calls)

    def test_reverting_to_boot_settings_cancels_pending_restart(self):
        b = self.bridge
        b.setQuickOptions({'production_mode': not b._boot_production})
        b.setQuickOptions({'production_mode': b._boot_production})
        QTest.qWait(200); self.restart.assert_not_called()
        b.process = Mock()
        b.setGraphicsOptions('software', not b._boot_graphics[1])
        b.setGraphicsOptions(*b._boot_graphics)
        b.process = None; b.runningChanged.emit()
        QTest.qWait(200); self.restart.assert_not_called()

    def test_unchanged_live_options_and_invalid_or_failed_saves_do_not_restart(self):
        b = self.bridge
        self.assertTrue(b.setQuickOptions({'backdrop_blur': False, 'window_mode': 'windowed'}))
        self.assertTrue(b.setGraphicsOptions(*b._boot_graphics))
        self.assertFalse(b.setGraphicsOptions('unknown', True))
        self.assertFalse(b.setQuickOptions({'production_mode': 'yes'}))
        with patch.object(self.storage, 'write_settings', side_effect=OSError('fixture write failed')):
            self.assertFalse(b.setQuickOptions({'production_mode': not b._boot_production}))
            self.assertFalse(b.setGraphicsOptions('software', False))
        QTest.qWait(200); self.restart.assert_not_called()
        self.assertFalse(b._restart_pending)

    def test_production_toggle_a_and_touch_each_schedule_restart(self):
        b = self.bridge
        pane = self.window.findChild(QObject, 'settingsPane')
        QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(250)
        button = self.window.findChild(QObject, 'settingsProductionMode')
        try:
            for touch in (False, True):
                b.setQuickOptions({'production_mode': b._boot_production})
                QTest.qWait(150); self.restart.reset_mock()
                if touch:
                    button.setProperty('checked', not b._boot_production)
                    QMetaObject.invokeMethod(button, 'clicked')
                else:
                    b.set_input_device('gamepad')
                    pane.setProperty('selectedField', 38); QTest.qWait(350)
                    b.action.emit('activate')
                self.restart.assert_not_called()
                self.assertEqual(self.storage.read_settings()['production_mode'], b._boot_production)
                QMetaObject.invokeMethod(pane, 'save')
                self.assertEqual(self.storage.read_settings()['production_mode'], not b._boot_production)
                self.wait_restart()
        finally:
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(180)
