import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject, QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui
from ngs.i18n import configure, tr


class LanguageUiTests(unittest.TestCase):
    ui_language = 'en'
    ui_server = ''
    ui_local_mode = True

    @classmethod
    def setUpClass(cls):
        test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def test_default_english_and_russian_catalog_preserve_keyboard_layouts(self):
        bridge = self.engine.bridge
        self.assertEqual(bridge.settingsState['language'], 'en')
        self.assertEqual(tr('Play'), 'Play')
        self.assertEqual(self.window.findChild(QObject,'installedFilter').property('text'), 'Filters')
        self.assertEqual(bridge.keyboard.views[0]['name'], 'Normal')
        pane = self.window.findChild(QObject,'settingsPane')
        QMetaObject.invokeMethod(pane,'open'); QTest.qWait(200)
        choice = pane.findChild(QObject,'settingsLanguage')
        self.assertEqual(choice.property('label'), 'Language')
        self.assertEqual(choice.property('currentIndex'), 0)
        original = copy.deepcopy(bridge.settings)
        try:
            bridge.settings['language']='ru';bridge.storage.write_settings(bridge.settings)
            configure(bridge.storage);self.engine.retranslate();QTest.qWait(100)
            self.assertEqual(tr('Play'),'Играть')
            self.assertEqual(choice.property('label'),'Язык')
            self.assertEqual(bridge.keyboard.views[0]['name'],'Обычная')
            self.assertEqual(self.window.findChild(QObject,'installedFilter').property('text'),'Фильтры')
            self.assertEqual(bridge.keyboard.options.get('view'),original.get('keyboard_options',{}).get('view',bridge.keyboard.options.get('view')))
        finally:
            bridge.settings=original;bridge.save();configure(bridge.storage);self.engine.retranslate()
            QMetaObject.invokeMethod(pane,'close');QTest.qWait(180)
        self.assertFalse(any('ReferenceError' in m or 'TypeError' in m or 'Binding loop' in m for m in self.messages),self.messages)

    def test_language_changes_live_while_the_game_keeps_running(self):
        bridge=self.engine.bridge;original=copy.deepcopy(bridge.settings)
        self.assertFalse(bridge.setLanguage('invalid'))
        previous_enabled=bridge._restart_enabled
        try:
            bridge._restart_enabled=True;bridge.process=SimpleNamespace(frozen=False)
            with patch.object(bridge,'restartLauncher',return_value=True) as restart:
                self.assertTrue(bridge.setLanguage('ru'));QTest.qWait(180)
                self.assertEqual(bridge.storage.read_settings()['language'],'ru')
                self.assertFalse(bridge._restart_pending);restart.assert_not_called()
                self.assertEqual(self.window.findChild(QObject,'installedFilter').property('text'),'Фильтры')
                bridge.process=None;bridge.runningChanged.emit();QTest.qWait(180)
                restart.assert_not_called()
            self.assertTrue(bridge.setLanguage('en'));bridge._restart_pending=False
            with patch.object(bridge.storage,'write_settings',side_effect=OSError('read only')):
                self.assertFalse(bridge.setLanguage('ru'))
            self.assertEqual(bridge.settingsState['language'],'en')
        finally:
            bridge.process=None;bridge.restart_timer.stop();bridge._restart_pending=False
            bridge._restart_enabled=previous_enabled;bridge.settings=original;bridge.save();bridge._sync_runtime_options()

    def test_settings_apply_retranslates_without_recreating_window_or_game(self):
        bridge=self.engine.bridge;original=copy.deepcopy(bridge.settings)
        pane=self.window.findChild(QObject,'settingsPane');game=SimpleNamespace(frozen=False)
        try:
            bridge.process=game
            QMetaObject.invokeMethod(pane,'open');QTest.qWait(180)
            pane.findChild(QObject,'settingsLanguage').setProperty('currentIndex',1)
            self.assertEqual(tr('Play'),'Play')
            with patch.object(bridge,'restartLauncher') as restart:
                QMetaObject.invokeMethod(pane,'save');QTest.qWait(200)
                self.assertEqual(tr('Play'),'Играть')
                self.assertEqual(self.window.findChild(QObject,'installedFilter').property('text'),'Фильтры')
                self.assertIs(bridge.process,game);self.assertFalse(bridge._restart_pending);restart.assert_not_called()
        finally:
            bridge.process=None;bridge.settings=original;bridge.save();bridge._sync_runtime_options()
            QMetaObject.invokeMethod(pane,'close');QTest.qWait(180)
