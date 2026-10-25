import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from PySide6.QtCore import QUrl,QEvent
from PySide6.QtGui import QGuiApplication,QWindow,QInputDevice
from PySide6.QtQml import QQmlEngine,QQmlComponent
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from ngs.virtual_keyboard import VirtualKeyboard
from ngs.status_overlay import StatusOverlay
from ngs.edge_swipe import EdgeWindows
from tests.test_desktop_controls import Bridge


class EdgeOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QGuiApplication.instance() or QGuiApplication([])
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.bridge=Bridge(Path(self.folder.name));self.keyboard=VirtualKeyboard(self.bridge)
        self.keyboard.backend=Mock();self.addCleanup(self.keyboard.close)
        self.overlay=StatusOverlay(self.keyboard,self.bridge);self.overlay.edge_enabled=True
        self.addCleanup(self.overlay.close)

    def test_handle_off_preserves_manual_menu_and_settings_survive_master_toggle(self):
        self.assertTrue(self.overlay.edgeSwipeEnabled and self.overlay.edgeAutoHide)
        self.overlay.edgeTouch('screen',1,5,0,True)
        self.keyboard.setEdgeAutoHide(False);self.keyboard.setEdgeSwipeEnabled(False)
        self.assertFalse(self.overlay.edgeEnabled or self.overlay.edgeWindowVisible or self.overlay.edge_gesture.contacts)
        requested=Mock();self.overlay.requested.connect(requested)
        self.overlay.edgeTouch('screen',1,5,110,True)
        self.overlay.setEdgeDrag('screen',1.)
        self.assertEqual(self.overlay.dragProgress,0.)
        requested.assert_not_called()
        self.overlay.request('screen');requested.assert_called_once()
        self.overlay.show('screen');self.assertTrue(self.overlay.visible)
        self.keyboard.setOverlayEnabled(False)
        self.assertFalse(self.overlay.shown or self.overlay.visible)
        self.overlay.show('screen');self.assertFalse(self.overlay.shown)
        self.keyboard.setOverlayEnabled(True)
        self.assertFalse(self.overlay.edgeSwipeEnabled or self.overlay.edgeAutoHide)
        self.assertTrue(self.keyboard.enabled)
        saved=self.bridge.storage.read_settings()['keyboard_options']
        self.assertIs(saved['edge_swipe_enabled'],False);self.assertIs(saved['edge_auto_hide'],False)
        reloaded=VirtualKeyboard(Bridge(Path(self.folder.name)))
        try:self.assertFalse(reloaded.options['edge_swipe_enabled'] or reloaded.options['edge_auto_hide'])
        finally:reloaded.close()

    def test_fullscreen_policy_tracks_switches_and_releases_on_close(self):
        engine=QQmlEngine();window=QWindow()
        # Offscreen HWNDs must never reach native shell APIs in this logic test.
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture',return_value=True) as policy:
            edges=EdgeWindows(engine,self.overlay,Path(__file__).resolve().parents[1])
            try:
                edges.attach_backing(window);window.show();self.app.processEvents()
                hwnd=int(window.winId());self.assertTrue(edges.backing_policy[hwnd])
                policy.reset_mock();self.keyboard.setEdgeAutoHide(False);policy.assert_not_called()
                self.keyboard.setEdgeSwipeEnabled(False);self.assertFalse(edges.backing_policy[hwnd]);policy.assert_called_with(hwnd,False)
                self.assertEqual(edges.message(hwnd,0x2cc,0,0),(True,0x10001))
                event=Mock();event.type.return_value=QEvent.Type.TouchBegin
                event.pointingDevice.return_value.type.return_value=QInputDevice.DeviceType.TouchScreen
                self.assertEqual(edges.filter_backing_touch(window,event),(False,'disabled_or_shown'))
                self.keyboard.setEdgeSwipeEnabled(True);policy.assert_called_with(hwnd,True)
                self.keyboard.setOverlayEnabled(False);policy.assert_called_with(hwnd,False)
                self.keyboard.setOverlayEnabled(True);policy.assert_called_with(hwnd,True)
                self.overlay.sleeping=True;self.overlay.changed.emit();policy.assert_called_with(hwnd,False)
                self.overlay.sleeping=False;self.overlay.changed.emit();policy.assert_called_with(hwnd,True)
                edges.close();policy.assert_called_with(hwnd,False)
            finally:edges.close();window.hide();engine.deleteLater();self.app.processEvents()

    def test_late_helper_swipe_cannot_reopen_menu_after_handle_is_disabled(self):
        from types import SimpleNamespace
        from ngs.input_host import InputHostClient
        client=SimpleNamespace(bridge=SimpleNamespace(status_overlay=self.overlay))
        self.overlay.remote=client
        try:
            self.keyboard.setEdgeSwipeEnabled(False)
            InputHostClient.receive(client,dict(kind='statusSwipe',screen='screen'))
            self.assertFalse(self.overlay.shown)
            self.keyboard.setEdgeSwipeEnabled(True)
            InputHostClient.receive(client,dict(kind='statusSwipe',screen='screen'))
            self.assertTrue(self.overlay.shown)
        finally:self.overlay.remote=None

    def test_qml_autohide_can_be_disabled_and_menu_switch_hides_handle(self):
        engine=QQmlEngine();component=QQmlComponent(engine,QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1]/'qml/EdgeSwipeWindow.qml')))
        window=component.createWithInitialProperties(dict(controller=self.overlay))
        self.assertIsNotNone(window,component.errors())
        self.assertIsInstance(window,QQuickWindow)
        try:
            QTest.qWait(30)
            self.keyboard.setEdgeAutoHide(False);QTest.qWait(250)
            self.assertEqual(window.property('reveal'),1.)
            QTest.qWait(4100);self.assertEqual(window.property('reveal'),1.)
            self.keyboard.setEdgeAutoHide(True);QTest.qWait(4300)
            self.assertEqual(window.property('reveal'),0.)
            self.keyboard.setEdgeSwipeEnabled(False);self.assertFalse(window.isVisible())
            self.keyboard.setEdgeSwipeEnabled(True);self.assertTrue(window.isVisible())
            self.keyboard.setOverlayEnabled(False);self.assertFalse(window.isVisible())
        finally:window.hide();window.destroy();window.deleteLater();self.app.processEvents()
