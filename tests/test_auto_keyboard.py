import ctypes
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QPointF, QObject, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from ngs.auto_keyboard import AutoKeyboard, TouchTap, WindowsTouchTaps
from ngs.edge_swipe import _MouseInput
from ngs.virtual_keyboard import VirtualKeyboard
from tests.test_desktop_controls import Bridge
from tests.test_keyboard_layouts import Power


class AutoKeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.keyboard = VirtualKeyboard(Bridge(Path(self.folder.name)))
        self.keyboard.backend = Mock()
        self.auto = AutoKeyboard(self.keyboard, Power(), native=False)

    def tearDown(self):
        self.auto.close(); self.keyboard.close(); self.folder.cleanup()

    def tap(self, editable=True, identity=('field',1)):
        self.auto.request((100,100)); self.auto.accept(self.auto.serial,editable,identity)

    def test_focus_notifications_never_open_keyboard(self):
        for identity in [('field',1),('field',2),('password',3)]:
            self.auto.request(); self.auto.accept(self.auto.serial,True,identity)
            self.assertFalse(self.keyboard.visible)

    def test_tap_can_reopen_the_same_dismissed_field(self):
        self.tap(); self.assertTrue(self.keyboard.visible)
        self.keyboard.hide()
        self.auto.request(); self.auto.accept(self.auto.serial,True,('field',1))
        self.assertFalse(self.keyboard.visible)
        self.tap(); self.assertTrue(self.keyboard.visible)

    def test_focus_callback_cannot_replace_an_inflight_tap(self):
        self.auto.request((100,100)); serial=self.auto.serial
        self.auto.request()
        self.assertEqual(self.auto.serial,serial)
        self.auto.accept(serial,True,('field',1))
        self.assertTrue(self.keyboard.visible)
        self.keyboard.hide()
        self.auto.request(); self.auto.accept(self.auto.serial,True,('field',2))
        self.assertFalse(self.keyboard.visible)

    def test_manual_hide_invalidates_queued_tap_result(self):
        self.auto.request((100,100)); serial=self.auto.serial
        self.keyboard.show(); self.keyboard.hide()
        self.auto.accept(serial,True,('field',1))
        self.assertFalse(self.keyboard.visible)

    def test_tap_on_non_editable_or_own_overlay_does_not_open_or_hide(self):
        self.tap(False); self.assertFalse(self.keyboard.visible)
        self.tap(); self.assertTrue(self.keyboard.visible)
        self.tap(False,('key',1)); self.assertTrue(self.keyboard.visible)
        self.tap(None,None); self.assertTrue(self.keyboard.visible)
        self.assertIsNone(self.auto.tap_serial)
        self.auto.request(); self.auto.accept(self.auto.serial,False,('button',2))
        self.assertFalse(self.keyboard.visible)

    def test_manual_keyboard_survives_focus_change(self):
        self.keyboard.show()
        self.auto.request(); self.auto.accept(self.auto.serial,False,('button',1))
        self.assertTrue(self.keyboard.visible)

    def test_drag_hold_and_release_without_press_are_not_taps(self):
        tap=TouchTap(10,.8)
        self.assertFalse(tap.feed(0x202,10,10,0))
        tap.feed(0x201,10,10,1); tap.feed(0x200,30,10,1.1)
        self.assertFalse(tap.feed(0x202,10,10,1.2))
        tap.feed(0x201,10,10,2)
        self.assertFalse(tap.feed(0x202,10,10,3))
        tap.feed(0x201,10,10,4)
        self.assertTrue(tap.feed(0x202,11,11,4.1))

    def test_native_observer_ignores_mouse_and_forwards_all_input(self):
        notify=Mock(); user=Mock()
        user.SetWindowsHookExW.return_value=123
        user.GetDpiForWindow.return_value=144
        user.CallNextHookEx.return_value=456
        with patch('ngs.auto_keyboard.ctypes.WinDLL',return_value=user):
            hook=WindowsTouchTaps(notify)
        try:
            packet=_MouseInput();packet.point.x=100;packet.point.y=100
            for extra in (0,0xff515780):
                packet.extra=extra
                for message in (0x201,0x202):
                    self.assertEqual(hook.callback(0,message,ctypes.addressof(packet)),456)
            notify.assert_called_once_with((100,100))
            self.assertEqual(hook.gesture.distance,self.app.styleHints().startDragDistance()*1.5)
        finally: hook.close()
        user.UnhookWindowsHookEx.assert_called_once_with(123)

    def test_qt_focus_and_mouse_do_not_open_but_touch_on_field_does(self):
        engine=QQmlApplicationEngine()
        engine.loadData(b'''import QtQuick
import QtQuick.Controls.Basic
Window { width: 400; height: 200; visible: true
    TextField { objectName: "field"; x: 30; y: 30; width: 200; height: 60 }
}''',QUrl('file:///auto-tap-test.qml'))
        window=engine.rootObjects()[0]; field=window.findChild(QObject,'field')
        self.app.installEventFilter(self.auto)
        try:
            window.requestActivate(); field.forceActiveFocus(); QTest.qWait(30)
            self.auto.qt_focus(field); self.assertFalse(self.keyboard.visible)
            QTest.mouseClick(window, Qt.LeftButton, pos=QPoint(60,60))
            self.auto.qt_focus(field); self.assertFalse(self.keyboard.visible)
            device=QTest.createTouchDevice()
            QTest.touchEvent(window,device).press(0,QPoint(60,60),window).commit()
            QTest.touchEvent(window,device).release(0,QPoint(60,60),window).commit();QTest.qWait(20)
            self.assertTrue(self.keyboard.visible)
            self.keyboard.hide(); self.auto.qt_tap(window,QPointF(300,150))
            self.assertFalse(self.keyboard.visible)
            field.setProperty('readOnly',True);self.auto.qt_tap(window,QPointF(60,60))
            self.assertFalse(self.keyboard.visible)
        finally:
            self.app.removeEventFilter(self.auto);window.hide();engine.deleteLater();QTest.qWait(10)


if __name__ == '__main__': unittest.main()
