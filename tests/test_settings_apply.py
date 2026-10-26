import copy
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, QMetaObject, QPointF
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest

from tests import test_ui


class SettingsApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def qml(self, item, code):
        expression = QQmlExpression(QQmlEngine.contextForObject(item), item, code)
        value, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return value

    def setUp(self):
        self.bridge = self.engine.bridge
        self.original = copy.deepcopy(self.bridge.settings)
        self.original_autostart = self.bridge._autostart
        self.pane = self.window.findChild(QObject, 'settingsPane')
        self.pane.close(); QTest.qWait(180)
        self.pane.open(); QTest.qWait(220)
        self.assertFalse(self.pane.property('dirty'))

    def tearDown(self):
        self.pane.close(); QTest.qWait(180)
        self.bridge.settings = self.original
        self.bridge._autostart = self.original_autostart
        self.bridge._restart_enabled = False
        self.bridge._restart_pending = False
        self.bridge.restart_timer.stop()
        self.bridge.process = None
        self.storage.write_settings(self.original)
        self.bridge._sync_runtime_options()
        self.bridge.settingsChanged.emit()
        self.bridge.virtual_keyboard.changed.emit()

    def test_different_input_methods_only_edit_until_apply(self):
        before = copy.deepcopy(self.storage.read_settings())
        self.pane.setProperty('selectedField', 38)
        self.bridge.action.emit('activate')
        keyboard = self.pane.findChild(QObject, 'settingsKeyboardEnabled')
        keyboard.setProperty('checked', not keyboard.property('checked'))
        QMetaObject.invokeMethod(keyboard, 'clicked')
        self.pane.findChild(QObject, 'settingsTheme').setProperty('currentIndex', 1)
        self.pane.findChild(QObject, 'settingsKnownGamesUrl').setProperty('text', 'https://example.com/games.json')
        self.assertTrue(self.pane.property('dirty'))
        self.assertEqual(self.storage.read_settings(), before)
        self.bridge.settingsChanged.emit(); self.bridge.virtual_keyboard.changed.emit()
        self.assertEqual(self.pane.findChild(QObject, 'settingsTheme').property('currentIndex'), 1)
        self.assertTrue(self.qml(self.pane, 'save()'))
        saved = self.storage.read_settings()
        self.assertEqual(saved['theme'], 'light')
        self.assertEqual(saved['production_mode'], not before.get('production_mode', False))
        self.assertEqual(saved['keyboard_options']['enabled'], keyboard.property('checked'))
        self.assertEqual(saved['known_games_url'], 'https://example.com/games.json')
        self.assertFalse(self.pane.property('dirty'))
        self.assertFalse(self.pane.findChild(QObject, 'settingsSave').property('enabled'))

    def test_back_preserves_draft_then_explicit_exit_choices(self):
        before = self.storage.read_settings()
        self.pane.setProperty('selectedField', 38); self.bridge.action.emit('activate')
        self.bridge.action.emit('back'); QTest.qWait(380)
        self.assertEqual(self.pane.property('page'), -1)
        self.assertTrue(self.pane.property('dirty'))
        self.assertTrue(self.pane.findChild(QObject, 'settingsSave').property('visible'))
        self.bridge.action.emit('back'); QTest.qWait(220)
        dialog = self.pane.findChild(QObject, 'settingsUnsavedDialog')
        self.assertTrue(dialog.property('visible'))
        self.bridge.action.emit('activate'); QTest.qWait(180)  # Default: keep editing.
        self.assertFalse(dialog.property('visible'))
        self.assertTrue(self.pane.property('dirty'))
        self.assertEqual(self.storage.read_settings(), before)
        self.bridge.action.emit('back'); QTest.qWait(180)
        self.pane.setProperty('unsavedAction', 1); self.bridge.action.emit('activate'); QTest.qWait(220)
        self.assertFalse(self.pane.property('visible'))
        self.assertEqual(self.storage.read_settings(), before)
        self.pane.open(); QTest.qWait(220)
        self.assertFalse(self.pane.property('dirty'))
        self.assertEqual(self.pane.findChild(QObject, 'settingsProductionMode').property('checked'), before.get('production_mode', False))

    def test_account_does_not_silently_save_other_sections(self):
        before = self.storage.read_settings()
        self.pane.findChild(QObject, 'settingsServer').setProperty('text', 'http://127.0.0.1:19/tactic')
        self.qml(self.pane, 'openChild("account")'); QTest.qWait(200)
        self.assertTrue(self.pane.findChild(QObject, 'settingsUnsavedDialog').property('visible'))
        self.assertFalse(self.window.findChild(QObject, 'playerAccountPane').property('visible'))
        self.assertEqual(self.storage.read_settings(), before)
        self.qml(self.pane, 'finishLeave(false)'); QTest.qWait(220)
        account = self.window.findChild(QObject, 'playerAccountPane')
        self.assertTrue(account.property('visible'))
        self.assertEqual(self.storage.read_settings(), before)
        account.close(); QTest.qWait(180)

    def test_failed_apply_keeps_draft_and_exit_prompt(self):
        before = self.storage.read_settings()
        self.pane.findChild(QObject, 'settingsKnownGamesUrl').setProperty('text', 'invalid://games')
        self.qml(self.pane, 'requestLeave("close")'); QTest.qWait(180)
        self.qml(self.pane, 'finishLeave(true)')
        self.assertTrue(self.pane.property('visible'))
        self.assertTrue(self.pane.property('dirty'))
        self.assertTrue(self.pane.findChild(QObject, 'settingsUnsavedDialog').property('visible'))
        self.assertEqual(self.storage.read_settings(), before)
        self.assertIn('http', self.bridge.settingsStatus)

    def test_apply_in_exit_prompt_changes_language_without_restart(self):
        self.bridge._restart_enabled = True
        self.bridge.process = Mock()
        self.pane.findChild(QObject, 'settingsLanguage').setProperty('currentIndex', 0)
        with patch.object(self.bridge, 'restartLauncher') as restart:
            self.qml(self.pane, 'requestLeave("close")'); QTest.qWait(180)
            self.qml(self.pane, 'finishLeave(true)'); QTest.qWait(220)
            self.assertFalse(self.pane.property('visible'))
            self.assertEqual(self.storage.read_settings()['language'], 'en')
            restart.assert_not_called()
            self.bridge.process = None; self.bridge.runningChanged.emit(); QTest.qWait(200)
            restart.assert_not_called()

    def test_backend_validation_and_startup_rollback_preserve_saved_values(self):
        before = copy.deepcopy(self.storage.read_settings())
        values = dict(self.bridge.settingsState, language='en', autostart=not self.bridge._autostart,
                      keyboard_options=dict(enabled=False))
        with patch('ngs.app.startup.set_enabled') as set_startup, \
                patch('ngs.app.startup.enabled', return_value=values['autostart']), \
                patch.object(self.storage, 'write_settings', side_effect=OSError('Fixture disk full')):
            self.assertFalse(self.bridge.applySettings(values))
            self.assertEqual(set_startup.call_args_list[0].args[1], values['autostart'])
            self.assertEqual(set_startup.call_args_list[-1].args[1], self.original_autostart)
        self.assertEqual(self.storage.read_settings(), before)
        self.assertEqual(self.bridge.settings, self.original)
        with patch('ngs.app.startup.set_enabled') as set_startup:
            self.assertFalse(self.bridge.applySettings(dict(values, keyboard_options=dict(enabled='no'))))
            set_startup.assert_not_called()
        self.assertEqual(self.storage.read_settings(), before)

    def test_focus_stays_inside_the_button_in_clipped_lists(self):
        categories = self.pane.findChild(QObject, 'settingsCategories')
        for size in ((1280, 800), (900, 600), (1920, 1080)):
            self.window.resize(*size)
            for index in range(6):
                self.pane.setProperty('menuIndex', index)
                self.qml(categories, 'positionViewAtIndex(currentIndex, ListView.Contain)')
                QTest.qWait(280)
                self.window.grabWindow()
                item = self.qml(categories, 'currentItem')
                frame = next(f for f in item.findChildren(QObject, 'focusFrame') if f.isVisible())
                start = frame.mapToItem(item, QPointF(0, 0))
                end = frame.mapToItem(item, QPointF(frame.width(), frame.height()))
                self.assertGreaterEqual(start.x(), -0.1)
                self.assertGreaterEqual(start.y(), -0.1)
                self.assertLessEqual(end.x(), item.width() + 0.1)
                self.assertLessEqual(end.y(), item.height() + 0.1)
                # Also include the button transform and its clipped ListView ancestor.
                start = frame.mapToItem(categories, QPointF(0, 0))
                end = frame.mapToItem(categories, QPointF(frame.width(), frame.height()))
                self.assertGreaterEqual(start.x(), -0.1)
                self.assertLessEqual(end.x(), categories.width() + 0.1)
                self.assertGreaterEqual(start.y(), -0.1)
                self.assertLessEqual(end.y(), categories.height() + 0.1)
        self.window.resize(1280, 800)
