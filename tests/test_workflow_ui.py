import copy
import unittest
from PySide6.QtCore import QObject, QMetaObject, QPointF
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest
from tests import test_ui as fixture


class WorkflowUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixture.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): fixture.UiTests.tearDownClass.__func__(cls)

    def evaluate(self, item, code):
        expression = QQmlExpression(QQmlEngine.contextForObject(item), item, code)
        result = expression.evaluate()[0]
        self.assertFalse(expression.hasError(), expression.error().toString())
        return result

    def test_header_settings_opens_categories_directly_from_click_and_gamepad(self):
        window, bridge = self.window, self.engine.bridge
        settings = window.findChild(QObject, 'settingsPane')
        menu = window.findChild(QObject, 'exitPopup')
        button = window.findChild(QObject, 'menuButton')
        try:
            for size in ((800, 500), (1280, 720)):
                window.resize(*size)
                for method in ('click', 'gamepad', 'shortcut'):
                    if method == 'click': QMetaObject.invokeMethod(button, 'clicked')
                    elif method == 'gamepad':
                        window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 5)
                        self.engine.gamepad.action.emit('activate')
                    else: bridge.action.emit('settings')
                    QTest.qWait(220)
                    self.assertTrue(settings.property('visible'), method)
                    self.assertEqual(settings.property('page'), -1)
                    self.assertFalse(menu.property('visible'), method)
                    bridge.action.emit('menu'); QTest.qWait(200)
        finally:
            QMetaObject.invokeMethod(settings, 'close'); window.resize(1280, 800)

    def test_update_badges_follow_the_path_without_marking_it_read(self):
        import json
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge._update), bridge._update_envelope
        settings = window.findChild(QObject, 'settingsPane')
        updates = window.findChild(QObject, 'updatesPane')
        version = '2099.01.01.120000'
        envelope = dict(payload=json.dumps(dict(version=version, notes='A visible update')), signature='test')
        def settle():
            for unused in range(300):
                QTest.qWait(10)
                if bridge.update_worker is None: return
            self.fail('Update check did not finish')
        try:
            with patch('ngs.app.check_update', return_value=envelope):
                bridge._update.update(current='2026.10.21.074903', available=False)
                bridge.updatesChanged.emit()
                self.assertFalse(window.findChild(QObject, 'updateAvailableBadge').property('visible'))
                bridge.checkUpdates(); settle()
                self.assertTrue(window.findChild(QObject, 'updateAvailableBadge').property('visible'))
                QMetaObject.invokeMethod(window.findChild(QObject, 'menuButton'), 'clicked'); QTest.qWait(220)
                self.assertEqual(settings.property('menuIndex'), 4)
                QTest.qWait(50)
                categories = settings.findChild(QObject, 'settingsCategories')
                category = categories.property('currentItem')
                badge = category.findChild(QObject, 'settingsSystemUpdateBadge-4')
                self.assertTrue(badge.property('visible'))
                bridge.action.emit('activate'); QTest.qWait(50)
                self.assertEqual(settings.property('page'), 4)
                self.assertTrue(badge.property('visible'))
                update_badge = settings.findChild(QObject, 'settingsUpdatesBadge')
                self.assertTrue(update_badge.property('visible'))
                settings.setProperty('selectedField', 8); bridge.action.emit('activate'); settle(); QTest.qWait(220)
                self.assertTrue(updates.property('visible'))
                self.assertTrue(window.findChild(QObject, 'updateAnnouncement').property('visible'))
                self.assertIn(version, window.findChild(QObject, 'updateAvailableVersion').property('text'))
                self.assertTrue(window.findChild(QObject, 'installUpdateButton').property('enabled'))
                self.assertTrue(window.findChild(QObject, 'updateAvailableBadge').property('visible'))
                bridge._update.update(available=False); bridge.updatesChanged.emit()
                self.assertEqual(badge.property('count'), 0)
                self.assertEqual(update_badge.property('count'), 0)
                self.assertFalse(window.findChild(QObject, 'updateAvailableBadge').property('visible'))
                self.assertFalse(window.findChild(QObject, 'updateAnnouncement').property('visible'))
        finally:
            bridge.cancelUpdate(); settle()
            QMetaObject.invokeMethod(settings, 'close'); QMetaObject.invokeMethod(updates, 'close')
            bridge._update, bridge._update_envelope = previous; bridge.updatesChanged.emit(); QTest.qWait(220)

    def test_menu_touch_targets_and_controller_routes(self):
        bridge, window = self.engine.bridge, self.window
        menu = window.findChild(QObject, 'exitPopup')
        settings = window.findChild(QObject, 'settingsPane')
        queue = bridge.download_queue.jobs()
        for size in ((800, 500), (1280, 720)):
            window.resize(*size)
            QMetaObject.invokeMethod(window, 'openMenu'); QTest.qWait(300)
            items = window.property('menuItems').toVariant()
            self.assertEqual(items, ['Settings', 'Minimize', 'Quit'])
            for item in items:
                button = window.findChild(QObject, 'menu'+item)
                self.assertGreaterEqual(button.height(), 48)
                end = button.mapToScene(QPointF(button.width(), button.height()))
                self.assertLessEqual(end.x(), size[0]); self.assertLessEqual(end.y(), size[1])
                self.assertIsNotNone(button.findChild(QObject, 'menuIcon-'+item))
            window.setProperty('menuAction', 2)
            self.assertTrue(window.findChild(QObject, 'menuQuit').property('highlighted'))
            bridge.action.emit('down'); self.assertEqual(window.property('menuAction'), 0)
            bridge.action.emit('activate'); QTest.qWait(220)
            self.assertTrue(settings.property('visible')); self.assertFalse(menu.property('visible'))
            bridge.action.emit('back'); QTest.qWait(220)
            QMetaObject.invokeMethod(window, 'openMenu'); QTest.qWait(220)
            bridge.action.emit('toggle_installed'); QTest.qWait(220)
            self.assertFalse(menu.property('visible')); self.assertFalse(bridge._closing)
            self.assertEqual(bridge.download_queue.jobs(), queue)
            window.showNormal(); window.resize(*size); QTest.qWait(100)

    def test_current_settings_section_stays_marked_inside_fields(self):
        pane = self.window.findChild(QObject, 'settingsPane')
        QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(220)
        try:
            for index in range(6):
                self.evaluate(pane, 'enterSection('+str(index)+')'); QTest.qWait(40)
                self.assertEqual(pane.property('focusSide'), 1)
                categories = pane.findChild(QObject, 'settingsCategories')
                current = categories.property('currentItem')
                self.assertTrue(current.property('currentSection'))
                self.assertEqual(pane.property('page'), index)
                self.engine.bridge.action.emit('left')
                self.assertEqual(pane.property('page'), index)
        finally:
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(200)

    def test_tool_pages_use_full_screen_and_catalogizer_keeps_real_steps(self):
        bridge = self.engine.bridge
        previous = copy.deepcopy(bridge.catalogizer._state), copy.deepcopy(bridge.settings)
        names = ('catalogizerPane', 'localScanner', 'installDestination', 'metadataPicker', 'manualCardEditor',
                 'launchOptionsPane', 'playerAccountPane', 'updatesPane', 'bindingsPane', 'directoryPicker', 'padKeyboardEditor', 'detailsPopup')
        try:
            for size in ((800, 500), (1280, 720), (1920, 1080)):
                self.window.resize(*size)
                for scale in (.8, 1, 1.2):
                    bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit(); QTest.qWait(30)
                    for name in names:
                        page = self.window.findChild(QObject, name)
                        self.assertIsNotNone(page, name)
                        self.assertEqual((page.property('x'), page.property('y'), page.property('width'), page.property('height')), (0, 0, *size), name)
            pane = self.window.findChild(QObject, 'catalogizerPane')
            for stage, current in (('folders', 0), ('exes', 1), ('matches', 2), ('review', 3), ('working', 4), ('batch', 4), ('done', 4)):
                bridge.catalogizer._state = dict(previous[0], stage=stage)
                bridge.catalogizer.changed.emit()
                self.assertEqual(pane.property('workflowStep'), current)
        finally:
            bridge.catalogizer._state, bridge.settings = previous
            bridge.catalogizer.changed.emit(); bridge.settingsChanged.emit()
            self.window.resize(1280, 800)

    def test_manual_wizard_keeps_draft_when_going_back(self):
        editor = self.window.findChild(QObject, 'manualCardEditor')
        title = editor.findChild(QObject, 'manualCardTitle')
        self.evaluate(editor, 'openEditor()'); QTest.qWait(220)
        try:
            title.setProperty('text', 'My preserved draft')
            QMetaObject.invokeMethod(editor.findChild(QObject, 'manualCardNext'), 'clicked')
            self.assertEqual(editor.property('page'), 1)
            self.assertEqual(editor.property('selectedField'), 2)
            self.evaluate(editor, 'handleAction("back")')
            self.assertEqual(editor.property('page'), 0)
            self.assertEqual(title.property('text'), 'My preserved draft')
        finally:
            QMetaObject.invokeMethod(editor, 'close'); QTest.qWait(200)

    def test_bindings_categories_keep_controller_on_visible_controls(self):
        pane = self.window.findChild(QObject, 'bindingsPane')
        QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(220)
        try:
            self.evaluate(pane, 'selectedField = -1; handleAction("activate")')
            self.assertEqual((pane.property('page'), pane.property('selectedField')), (1, 12))
            self.assertTrue(pane.findChild(QObject, 'macroName').property('visible'))
            self.evaluate(pane, 'handleAction("back")')
            QTest.qWait(220)
            self.assertFalse(pane.property('visible'))
        finally:
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(200)


if __name__ == '__main__': unittest.main()
