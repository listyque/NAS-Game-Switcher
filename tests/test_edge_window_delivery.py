"""Actual HWND delivery (no system input injection or visible windows)."""
import ctypes
import os
import unittest
from ctypes import wintypes as w
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlEngine
from PySide6.QtQuick import QQuickWindow
from ngs.edge_swipe import EdgeWindows
from ngs.status_overlay import StatusOverlay
from ngs.virtual_keyboard import VirtualKeyboard


@unittest.skipUnless(os.name == 'nt', 'Windows HWND delivery')
class EdgeWindowDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        self.parent = QObject(); self.parent.settings = {}
        self.overlay = StatusOverlay(VirtualKeyboard(self.parent), self.parent)
        self.engine = QQmlEngine()
        self.edges = EdgeWindows(self.engine, self.overlay, Path(__file__).resolve().parents[1])
        self.window = self.edges.windows[0]
        self.window.create()
        self.hwnd = int(self.window.winId())
        self.edges.handles[self.hwnd] = self.window
        self.edges.subclass_window(self.hwnd)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self.user.SendMessageW.restype = ctypes.c_ssize_t
        self.user.IsWindowVisible.argtypes = [w.HWND]
        self.assertFalse(self.user.IsWindowVisible(self.hwnd))

    def tearDown(self):
        self.edges.close()
        self.overlay.close()
        self.engine.deleteLater()
        self.app.processEvents()

    def test_sent_mouse_reaches_hwnd_while_application_native_filter_misses_it(self):
        for message in (0x200, 0x201, 0x202):
            with self.subTest(message=message):
                calls = []
                def receive(hwnd, msg, wp, lp):
                    if msg == message:
                        calls.append((hwnd, wp, lp))
                        return True, 73
                    return False, 0
                with patch.object(self.edges, 'message', side_effect=receive):
                    self.edges.unsubclass_window(self.hwnd)
                    self.user.SendMessageW(self.hwnd, message, 0, 0)
                    self.assertEqual(calls, [])
                    self.edges.subclass_window(self.hwnd)
                    self.assertEqual(self.user.SendMessageW(self.hwnd, message, 0, 0), 73)
                    self.assertEqual(calls, [(self.hwnd, 0, 0)])

    def test_touch_callback_single_finger_opens_once_and_releases_legacy_handle_once(self):
        requested = Mock(); self.overlay.requested.connect(requested)
        self.overlay.edge_enabled = True  # Keep QML windows hidden in this delivery test.
        state = dict(y=3, flags=2)
        origin = w.POINT()
        self.assertTrue(self.user.ClientToScreen(self.hwnd, ctypes.byref(origin)))
        ratio = self.window.devicePixelRatio()
        def touch(handle, count, items, size):
            self.assertEqual(count, 1)
            items[0].identity = 7
            items[0].x = round((origin.x + 100 * ratio) * 100)
            items[0].y = round((origin.y + state['y'] * ratio) * 100)
            items[0].flags = state['flags']
            return True
        with patch.object(self.edges.user, 'GetTouchInputInfo', side_effect=touch), patch.object(self.edges.user, 'CloseTouchInputHandle', return_value=True) as close:
            for y, flags in ((3,2), (12,1), (28,1), (70,1), (90,4)):
                state.update(y=y,flags=flags)
                self.edges.window_message(self.hwnd, 0x240, 1, 123, id(self.edges), 0)
                self.app.processEvents()
            self.assertEqual(close.call_count, 5)
        requested.assert_called_once_with(self.window.screen().name())
        self.assertFalse(self.edges.gestures[self.hwnd].contacts)
        self.assertEqual(self.overlay.dragProgress, 0)

    def test_activation_is_declined_without_eating_input_and_destroy_removes_callback(self):
        for message in (0x21,0x24b):
            self.assertEqual(self.user.SendMessageW(self.hwnd,message,0,0),3)
        self.window.destroy()
        self.assertNotIn(self.hwnd,self.edges.subclasses)

    def test_launcher_declines_native_hold_and_flick_and_still_forwards_touch(self):
        backing = QQuickWindow()
        backing.create(); hwnd = int(backing.winId())
        baseline = self.user.SendMessageW(hwnd, 0x2cc, 0, 0)
        self.edges.backings[hwnd] = backing; self.edges.subclass_window(hwnd)
        try:
            self.assertEqual(self.user.SendMessageW(hwnd, 0x2cc, 0, 0), 0x10001)
            for message in (0x240, 0x245, 0x246, 0x247):
                with self.subTest(message=message):
                    self.assertEqual(self.edges.message(hwnd, message, 0, 0), (False, 0))
            self.edges.unsubclass_window(hwnd)
            self.assertEqual(self.user.SendMessageW(hwnd, 0x2cc, 0, 0), baseline)
        finally: backing.destroy()

    def test_close_removes_callback_from_backing_and_handle(self):
        backing = QQuickWindow()
        backing.setTitle('Still owned by Qt')
        backing.create()
        hwnd = int(backing.winId())
        self.edges.backings[hwnd] = backing
        self.edges.subclass_window(hwnd)
        try:
            self.edges.close()
            self.assertFalse(self.edges.subclasses)
            with patch.object(self.edges,'message') as message:
                self.assertEqual(self.user.SendMessageW(hwnd,0x0e,0,0),len('Still owned by Qt'))
                message.assert_not_called()
        finally: backing.destroy()


if __name__ == '__main__': unittest.main()
