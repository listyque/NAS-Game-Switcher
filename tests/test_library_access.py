import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject, QMetaObject, Qt, QPointF, QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from tests import test_ui
from ngs.play_time import PlayTime


class LibraryAccessTests(unittest.TestCase):
    ui_language='en'
    ui_local_mode=False
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop();test_ui.UiTests.tearDownClass.__func__(cls)

    def tearDown(self):
        self.window.findChild(QObject,'detailsPopup').close();QTest.qWait(180)

    def test_keyboard_hints_are_visible_from_start_and_keypress_replaces_gamepad_labels(self):
        b=self.engine.bridge
        b.set_input_device('gamepad')
        self.window.setProperty('section',1);self.window.setProperty('focusZone',2)
        QTest.keyClick(self.window,Qt.Key.Key_Right);QTest.qWait(250)
        self.assertEqual(b.inputHints['activate'],'Enter')
        self.assertTrue(self.window.findChild(QObject,'acceptHint').property('visible'))
        self.assertIn('F10',self.window.findChild(QObject,'detailsHint').property('text'))

    def test_keyboard_and_info_button_open_details_without_launching(self):
        b=self.engine.bridge
        self.window.setProperty('section',1);self.window.setProperty('focusZone',0)
        with patch.object(b,'performGameAction') as launch:
            QTest.keyClick(self.window,Qt.Key.Key_F10);QTest.qWait(200)
            self.assertTrue(self.window.findChild(QObject,'detailsPopup').property('visible'))
            self.assertTrue(self.window.findChild(QObject,'gameStatistics').property('visible'))
            launch.assert_not_called()
            self.window.findChild(QObject,'detailsPopup').close();QTest.qWait(200)
            view=self.window.findChild(QObject,'gameCards')
            item=view.property('currentItem')
            button=item.findChild(QObject,'cardInfo-'+str(item.property('index')))
            self.assertIsNotNone(button)
            point=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
            QTest.mouseClick(self.window,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point);QTest.qWait(200)
            self.assertTrue(self.window.findChild(QObject,'detailsPopup').property('visible'));launch.assert_not_called()
            self.assertFalse(self.window.property('launchPending'))

    def test_touch_promoted_mouse_keeps_hints_and_real_mouse_selects_keyboard_hints(self):
        b=self.engine.bridge
        point=QPointF(1,1)
        for source,expected in ((Qt.MouseEventSource.MouseEventSynthesizedBySystem,'gamepad'),
                                (Qt.MouseEventSource.MouseEventNotSynthesized,'keyboard')):
            b.set_input_device('gamepad')
            event=QMouseEvent(QEvent.Type.MouseButtonPress,point,point,point,Qt.MouseButton.LeftButton,
                              Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,source)
            b.eventFilter(self.window,event)
            self.assertEqual(b.inputHints['device'],expected)

    def test_finished_and_running_totals_respect_local_reset(self):
        b=self.engine.bridge;now=[0.0]
        tracker=PlayTime(b.library.path,clock=lambda:now[0])
        identity='local-statistics-test'
        self.assertEqual(tracker.totals(identity)['launches'],0)
        tracker.start(identity);now[0]=12
        self.assertEqual(tracker.totals(identity)['total_seconds'],12)
        tracker.checkpoint(reason='completed')
        self.assertEqual(tracker.totals(identity)['total_seconds'],12)
        tracker.start(identity);now[0]=20
        tracker.reset(tracker.snapshot()['session_id']);now[0]=23
        tracker.checkpoint(reason='completed')
        self.assertEqual(tracker.totals(identity)['total_seconds'],3)
        self.assertEqual(tracker.totals(identity)['longest_seconds'],3)
        self.assertEqual(tracker.totals(identity)['launches'],0)
        tracker.start(identity);now[0]=25;tracker.checkpoint(reason='completed')
        self.assertEqual(tracker.totals(identity)['total_seconds'],5)
        self.assertEqual(tracker.totals(identity)['launches'],1)

    def test_idle_reset_keeps_raw_history_and_other_games(self):
        tracker=PlayTime(self.engine.bridge.library.path)
        for identity in ('idle-reset-test','idle-reset-other'):
            tracker.start(identity);tracker.checkpoint(reason='completed')
        tracker.reset_game('idle-reset-test')
        self.assertEqual(tracker.totals('idle-reset-test')['launches'],0)
        self.assertEqual(tracker.totals('idle-reset-other')['launches'],1)
        tracker.start('idle-reset-test');tracker.checkpoint(reason='completed')
        self.assertEqual(tracker.totals('idle-reset-test')['launches'],1)
