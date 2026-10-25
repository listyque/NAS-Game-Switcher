import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject, QPoint, QEvent
from PySide6.QtTest import QTest
from ngs.edge_swipe import EdgeWindows
from tests import test_ui


class LauncherEdgeTouchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def test_single_touch_in_main_and_statistics_opens_local_and_remote_panel(self):
        overlay = self.engine.bridge.status_overlay
        edges = EdgeWindows(self.engine, overlay, self.repo)
        edges.attach_backing(self.window)
        device = QTest.createTouchDevice()
        screen = self.window.screen().geometry()
        start = self.window.mapFromGlobal(QPoint(screen.center().x(), screen.top()+8))
        remote = Mock()
        try:
            overlay.edge_enabled = True
            with patch.object(overlay, 'refreshSystemStatus'):
                for host in (None, remote):
                    overlay.remote = host
                    for stats in (False, True):
                        with self.subTest(remote=host is not None, statistics=stats):
                            self.window.setProperty('gameInProgress', stats); QTest.qWait(40)
                            overlay.last_request = 0
                            QTest.touchEvent(self.window, device).press(0, start, self.window).commit(); QTest.qWait(20)
                            QTest.touchEvent(self.window, device).move(0, start+QPoint(0,64), self.window).commit(); QTest.qWait(20)
                            QTest.touchEvent(self.window, device).release(0, start+QPoint(0,96), self.window).commit(); QTest.qWait(30)
                            self.assertTrue(overlay.shown)
                            if host: host.status.assert_called_with(True, self.window.screen().name())
                            else: self.assertTrue(self.engine.rootObjects()[1].isVisible())
                            overlay.finishHide(); QTest.qWait(220)
        finally:
            overlay.remote = None; overlay.finishHide(); overlay.edge_enabled = False; overlay.changed.emit()
            self.window.setProperty('gameInProgress', False); edges.close()

    def test_mouse_and_disabled_touch_pass_to_page_but_edge_touch_does_not(self):
        from PySide6.QtCore import Qt
        received = []
        class PageInput(QObject):
            def eventFilter(self, obj, event):
                received.append(event.type())
                return False
        observer = PageInput()
        self.window.installEventFilter(observer)
        overlay = self.engine.bridge.status_overlay
        edges = EdgeWindows(self.engine, overlay, self.repo); edges.attach_backing(self.window)
        overlay.edge_enabled = True
        screen = self.window.screen().geometry()
        start = self.window.mapFromGlobal(QPoint(screen.center().x(),screen.top()+8))
        device = QTest.createTouchDevice()
        try:
            with patch.object(overlay, 'refreshSystemStatus'), patch.object(overlay, 'request') as request:
                QTest.mouseClick(self.window,Qt.MouseButton.LeftButton,pos=start)
                self.assertIn(QEvent.Type.MouseButtonPress,received)
                request.assert_not_called()
                for sleeping in (True, False):
                    overlay.sleeping=sleeping; overlay.changed.emit(); received.clear()
                    QTest.touchEvent(self.window,device).press(0,start,self.window).commit(); QTest.qWait(20)
                    QTest.touchEvent(self.window,device).move(0,start+QPoint(0,96),self.window).commit()
                    QTest.touchEvent(self.window,device).release(0,start+QPoint(0,96),self.window).commit(); QTest.qWait(20)
                    self.assertEqual(QEvent.Type.TouchBegin in received,sleeping)
                request.assert_called_once()
                edges.close(); received.clear()
                QTest.touchEvent(self.window,device).press(0,start,self.window).commit(); QTest.qWait(20)
                QTest.touchEvent(self.window,device).release(0,start,self.window).commit()
                self.assertIn(QEvent.Type.TouchBegin,received)
        finally:
            overlay.sleeping=False; overlay.edge_enabled=False; overlay.changed.emit()
            edges.close(); self.window.removeEventFilter(observer)

    def test_only_compact_touch_origin_is_owned_and_cancel_releases_next_swipe(self):
        overlay = self.engine.bridge.status_overlay
        edges = EdgeWindows(self.engine, overlay, self.repo); edges.attach_backing(self.window)
        overlay.edge_enabled = True
        device = QTest.createTouchDevice()
        screen = self.window.screen().geometry()
        center = self.window.mapFromGlobal(QPoint(screen.center().x(), screen.top()+8))
        try:
            with patch.object(overlay, 'refreshSystemStatus'), patch.object(overlay, 'request') as requested:
                for point in (center-QPoint(260,0), center+QPoint(260,0), center+QPoint(0,40)):
                    QTest.touchEvent(self.window,device).press(0,point,self.window).commit()
                    QTest.touchEvent(self.window,device).move(0,point+QPoint(0,96),self.window).commit()
                    QTest.touchEvent(self.window,device).release(0,point+QPoint(0,96),self.window).commit(); QTest.qWait(30)
                requested.assert_not_called()
                QTest.touchEvent(self.window,device).press(0,center,self.window).commit(); QTest.qWait(20)
                self.app.sendEvent(self.window,QEvent(QEvent.Type.TouchCancel))
                QTest.touchEvent(self.window,device).release(0,center+QPoint(0,96),self.window).commit(); QTest.qWait(20)
                requested.assert_not_called()
                QTest.touchEvent(self.window,device).press(0,center,self.window).commit()
                QTest.touchEvent(self.window,device).move(0,center+QPoint(0,96),self.window).commit()
                QTest.touchEvent(self.window,device).release(0,center+QPoint(0,96),self.window).commit(); QTest.qWait(30)
                requested.assert_called_once()
        finally:
            overlay.edge_enabled=False; overlay.changed.emit(); edges.close()
