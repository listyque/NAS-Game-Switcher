"""Mouse fallbacks are intentionally excluded from the touch-only edge."""
import unittest
from pathlib import Path
from unittest.mock import Mock
from PySide6.QtCore import QObject, QPoint, QPointF, QEvent, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtQml import QQmlEngine, QQmlComponent
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from ngs.status_overlay import StatusOverlay
from ngs.virtual_keyboard import VirtualKeyboard
from tests import test_edge_swipe


class EdgeMouseExclusionTests(unittest.TestCase):
    def test_pen_mouse_touchpad_pointer_and_legacy_pen_leave_finger_alone(self):
        manager = test_edge_swipe.EdgeSwipeTests().native_edges()
        manager.contact(17,1,-1800,3,True)
        start = manager.gestures[17].start
        for kind in (1,3,4,5):
            def info(identity, output):
                output._obj.kind = kind
                return True
            manager.user.GetPointerInfo.side_effect = info
            for message in (0x246,0x245,0x247):
                self.assertEqual(manager.message(17,message,2,0),(False,0))
        self.assertEqual(manager.message(17,0x24c,2,0),(False,0))
        def pen(handle,count,items,size):
            items[0].flags = 0x42
            items[0].identity = 2
            return True
        manager.user.GetTouchInputInfo.side_effect = pen
        manager.message(17,0x240,1,123)
        manager.user.CloseTouchInputHandle.assert_called_once_with(123)
        self.assertEqual(manager.gestures[17].start,start)
        manager.contact(17,1,-1800,126,False)
        manager.dispatch.recognized.emit.assert_called_once()

    def test_qml_mouse_sources_do_nothing_and_single_touch_still_opens(self):
        app = QGuiApplication.instance() or QGuiApplication([])
        parent = QObject();parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard,parent);overlay.edge_enabled=True
        engine = QQmlEngine()
        component = QQmlComponent(engine,QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]/'qml/EdgeSwipeWindow.qml')))
        window = component.createWithInitialProperties(dict(controller=overlay))
        self.assertIsNotNone(window,component.errors())
        requested=Mock();overlay.requested.connect(requested)
        def mouse():
            for source in Qt.MouseEventSource:
                for kind,y in ((QEvent.Type.MouseButtonPress,3),(QEvent.Type.MouseMove,100),(QEvent.Type.MouseButtonRelease,100)):
                    button=Qt.MouseButton.NoButton if kind==QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
                    buttons=Qt.MouseButton.NoButton if kind==QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
                    event=QMouseEvent(kind,QPointF(240,y),QPointF(240,y),QPointF(240,y),button,buttons,Qt.KeyboardModifier.NoModifier,source)
                    app.sendEvent(window,event)
        try:
            QTest.qWait(30)
            mouse();requested.assert_not_called()
            device=QTest.createTouchDevice()
            QTest.touchEvent(window,device).press(0,QPoint(240,3),window).commit()
            QTest.touchEvent(window,device).move(0,QPoint(240,12),window).commit()
            app.processEvents()
            self.assertTrue(overlay.edge_gesture.contacts)
            mouse();requested.assert_not_called()
            QTest.touchEvent(window,device).move(0,QPoint(240,100),window).commit()
            QTest.touchEvent(window,device).release(0,QPoint(240,100),window).commit()
            app.processEvents()
            requested.assert_called_once()
            self.assertFalse(overlay.edge_gesture.contacts)
        finally:
            window.hide();window.destroy();engine.deleteLater()
            overlay.close();keyboard.close()


if __name__ == '__main__': unittest.main()
