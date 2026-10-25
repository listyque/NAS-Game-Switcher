import copy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QPointF, QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui


class UpdateNotesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        test_ui.UiTests.tearDownClass.__func__(cls)

    def test_full_length_notes_scroll_with_touch_and_controller_and_keep_actions_visible(self):
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge._update)
        size = window.size()
        pane = window.findChild(QObject, 'updatesPane')
        notes = ('Изменения выпуска: запуск, кэш, ПРОД МОД.\n' + 'https://example.test/' + 'a' * 180 + '\n') * 30
        notes = notes[:3980] + '\nКОНЕЦ <plain text>'
        try:
            with patch.object(bridge, 'checkUpdates'):
                for width, height in ((800, 500), (1280, 720), (1920, 1200)):
                    with self.subTest(size=(width, height)):
                        window.resize(width, height)
                        bridge._update.update(available=True, current="2026.10.24.193621", busy=False,
                                              version='2099.01.01.000000', notes=notes,
                                              status='Длинное сообщение состояния. ' * 30)
                        bridge.updatesChanged.emit()
                        QMetaObject.invokeMethod(pane, 'open')
                        QTest.qWait(250)
                        label = window.findChild(QObject, 'updateNotes')
                        scroll = window.findChild(QObject, 'updatesScroll')
                        flick = scroll.property('contentItem')
                        self.assertEqual(label.property('text'), notes)
                        self.assertFalse(label.property('truncated'))
                        self.assertGreater(label.property('height'), 110 * pane.property('uiScale'))
                        self.assertFalse(window.findChild(QObject, 'updateStatus').property('truncated'))
                        self.assertAlmostEqual(flick.property('contentY'), 0, delta=1)
                        selected = pane.property('selectedField')
                        for _ in range(80):
                            bridge.action.emit('down')
                        QTest.qWait(30)
                        end = flick.property('contentHeight') - scroll.property('availableHeight')
                        self.assertAlmostEqual(flick.property('contentY'), end, delta=1)
                        self.assertEqual(pane.property('selectedField'), selected)
                        tail = label.mapToScene(QPointF(0, label.height()))
                        viewport = flick.mapToScene(QPointF(0, 0))
                        self.assertLessEqual(tail.y(), viewport.y() + flick.height() + 1)
                        for name in ('installUpdateButton', 'updatesBackButton'):
                            button = window.findChild(QObject, name)
                            position = button.mapToScene(QPointF(0, 0))
                            self.assertGreaterEqual(position.y(), 0)
                            self.assertLessEqual(position.y() + button.height(), window.height())
                        bridge.action.emit('right')
                        self.assertEqual(pane.property('selectedField'), 2)
                        bridge.action.emit('left')
                        self.assertEqual(pane.property('selectedField'), 1)
                        for _ in range(80):
                            bridge.action.emit('up')
                        self.assertAlmostEqual(flick.property('contentY'), 0, delta=1)
                        device = QTest.createTouchDevice()
                        start = flick.mapToScene(QPointF(flick.width() / 2, flick.height() * .85)).toPoint()
                        finish = flick.mapToScene(QPointF(flick.width() / 2, flick.height() * .2)).toPoint()
                        QTest.touchEvent(window, device).press(0, start, window).commit()
                        QTest.qWait(30)
                        QTest.touchEvent(window, device).move(0, (start + finish) / 2, window).commit()
                        QTest.qWait(30)
                        QTest.touchEvent(window, device).move(0, finish, window).commit()
                        QTest.touchEvent(window, device).release(0, finish, window).commit()
                        QTest.qWait(200)
                        self.assertGreater(flick.property('contentY'), 0)
                        QMetaObject.invokeMethod(pane, 'close')
                        QTest.qWait(180)
        finally:
            QMetaObject.invokeMethod(pane, 'close')
            bridge._update = previous
            bridge.updatesChanged.emit()
            window.resize(size)
            QTest.qWait(200)


if __name__ == '__main__':
    unittest.main()
