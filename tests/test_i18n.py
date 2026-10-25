import copy
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject, QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui
from ngs.i18n import configure, tr


class LanguageUiTests(unittest.TestCase):
    ui_language = 'en'
    ui_server = ''

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

    def test_language_is_saved_and_restart_waits_for_the_running_game(self):
        bridge=self.engine.bridge;original=copy.deepcopy(bridge.settings)
        self.assertFalse(bridge.setLanguage('invalid'))
        previous_enabled=bridge._restart_enabled
        try:
            bridge._restart_enabled=True;bridge.process=object()
            with patch.object(bridge,'restartLauncher',return_value=True) as restart:
                self.assertTrue(bridge.setLanguage('ru'));QTest.qWait(180)
                self.assertEqual(bridge.storage.read_settings()['language'],'ru')
                self.assertTrue(bridge._restart_pending);restart.assert_not_called()
                bridge.process=None;bridge.runningChanged.emit();QTest.qWait(180)
                restart.assert_called_once()
            bridge.settings=original;bridge.save();bridge._restart_pending=False
            with patch.object(bridge.storage,'write_settings',side_effect=OSError('read only')):
                self.assertFalse(bridge.setLanguage('ru'))
            self.assertEqual(bridge.settingsState['language'],'en')
        finally:
            bridge.process=None;bridge.restart_timer.stop();bridge._restart_pending=False
            bridge._restart_enabled=previous_enabled;bridge.settings=original;bridge.save()
