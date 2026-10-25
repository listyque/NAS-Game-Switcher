import os
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject
from PySide6.QtGui import QGuiApplication
from ngs.edge_swipe import EdgeGesture, EdgeWindows
from ngs.touchpad import _PointerInfo
from ngs.status_overlay import StatusOverlay, park_fullscreen_game
from ngs.virtual_keyboard import VirtualKeyboard


class EdgeSwipeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows shell window property')
    def test_system_gestures_can_be_restored_without_changing_game_default(self):
        import uuid
        from ctypes import wintypes as w
        from ngs.edge_swipe import disable_fullscreen_edge_gesture, _PropertyKey
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
        user.CreateWindowExW.restype = w.HWND
        user.DestroyWindow.argtypes = [w.HWND]
        hwnd = user.CreateWindowExW(0x08000000, 'STATIC', '', 0, 0, 0, 1, 1, None, None, None, None)
        self.assertTrue(hwnd)
        try:
            shell = ctypes.WinDLL('shell32')
            shell.SHGetPropertyStoreForWindow.argtypes = [w.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            shell.SHGetPropertyStoreForWindow.restype = ctypes.c_long
            iid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99').bytes_le)
            key = _PropertyKey((ctypes.c_byte * 16).from_buffer_copy(uuid.UUID('32ce38b2-2c9a-41b1-9bc5-b3784394aa44').bytes_le), 2)
            for disabled in (True, False, True):
                self.assertTrue(disable_fullscreen_edge_gesture(hwnd) if disabled else disable_fullscreen_edge_gesture(hwnd, False))
                store = ctypes.c_void_p()
                self.assertGreaterEqual(shell.SHGetPropertyStoreForWindow(hwnd, iid, ctypes.byref(store)), 0)
                table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                try:
                    value = ctypes.create_string_buffer(32)
                    get = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey), ctypes.c_void_p)(table[5])
                    self.assertGreaterEqual(get(store, ctypes.byref(key), value), 0)
                    props = ctypes.WinDLL('propsys')
                    props.PropVariantToBoolean.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.BOOL)]
                    props.PropVariantToBoolean.restype = ctypes.c_long
                    actual = w.BOOL()
                    try:
                        self.assertGreaterEqual(props.PropVariantToBoolean(value, ctypes.byref(actual)), 0)
                        self.assertEqual(bool(actual.value), disabled)
                    finally:
                        ole = ctypes.WinDLL('ole32')
                        ole.PropVariantClear.argtypes = [ctypes.c_void_p]
                        ole.PropVariantClear(value)
                finally:
                    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(store)
        finally:
            user.DestroyWindow(hwnd)

    def test_disabling_edge_clears_touch_without_installing_mouse_observers(self):
        manager = self.native_edges()
        manager.controller.enabled = False
        manager.controller.edgeSwipeEnabled = False
        manager.gestures[17] = EdgeGesture()
        manager.update_enabled()
        self.assertFalse(manager.gestures)
        manager.controller.cancelEdgeTouch.assert_called_once()
        manager.user.SetWindowsHookExW.assert_not_called()

    def test_native_trace_reports_lifecycle_but_not_pointer_movements(self):
        manager = self.native_edges()
        state = dict(y=3)
        def info(identity, pointer):
            value = pointer._obj
            value.kind, value.identity, value.flags = 2, identity, 4
            value.pixel.x, value.pixel.y = -1800, state['y']
            return True
        manager.user.GetPointerInfo.side_effect = info
        manager.message(17, 0x24b, 3, 0)
        manager.message(17, 0x246, 3, 0)
        state['y'] = 10
        for _ in range(20): manager.message(17, 0x245, 3, 0)
        manager.message(17, 0x247, 3, 0)
        manager.message(17, 0x24c, 3, 0)
        self.assertEqual([call.args for call in manager.dispatch.observed.emit.call_args_list],
                         [('activate', 0x24b, 1, True), ('pointer_down', 4, 1, True),
                          ('pointer_up', 4, 1, True)])
        manager.dispatch.observed.emit.reset_mock()
        manager.message(99, 0x246, 3, 0)
        manager.dispatch.observed.emit.assert_not_called()

    def test_edge_lifecycle_dispatch_is_queued_through_a_qobject(self):
        from ngs.edge_swipe import _Dispatch
        from PySide6.QtCore import Qt
        app = QGuiApplication.instance() or QGuiApplication([])
        dispatcher = _Dispatch()
        received = Mock()
        dispatcher.observed.connect(received, Qt.ConnectionType.QueuedConnection)
        dispatcher.observed.emit('pointer_down', 4, 1, True)
        received.assert_not_called()
        app.processEvents()
        received.assert_called_once_with('pointer_down', 4, 1, True)

    def native_edges(self):
        class Source(ctypes.Structure): _fields_ = [('device', ctypes.c_int), ('origin', ctypes.c_int)]
        window = Mock()
        window.devicePixelRatio.return_value = 1.5
        window.width.return_value = 1280; window.height.return_value = 32
        window.screen.return_value.name.return_value = 'portrait-device'
        manager = EdgeWindows.__new__(EdgeWindows)
        manager.handles, manager.gestures = {17:window}, {}
        manager.backings, manager.backing_policy = {}, {}
        manager.scroll_links = []
        manager.closed = False
        manager.subclasses = set(); manager.comctl = Mock(); manager.subclass_callback = Mock()
        manager.controller, manager.dispatch, manager.user = Mock(edgeEnabled=True, edgeSwipeEnabled=True, sleeping=False), Mock(), Mock()
        manager.Source = Source
        def client(hwnd, point): point._obj.x += 1920; return True
        manager.user.ScreenToClient.side_effect = client
        manager.user.GetMessageExtraInfo.return_value = 0
        manager.user.GetCurrentInputMessageSource.return_value = False
        return manager

    def test_visible_target_registers_low_latency_touch_and_unregisters_when_hidden(self):
        manager = self.native_edges()
        window = manager.handles[17]
        manager.handles = {}
        manager.changed = Mock()
        window.winId.return_value = 17
        window.isVisible.return_value = True
        with patch.object(QGuiApplication, 'platformName', return_value='windows'), \
             patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'), \
             patch('ngs.edge_swipe.enable_dwm_alpha_composition'), patch('ngs.process.set_topmost'):
            manager.visibility(window)
            manager.user.RegisterTouchWindow.assert_called_once_with(17, 3)
            self.assertIs(manager.handles[17], window)
            window.isVisible.return_value = False
            manager.visibility(window)
            manager.visibility(window)
            manager.user.UnregisterTouchWindow.assert_called_once_with(17)
            self.assertFalse(manager.handles)

    def test_mouse_messages_cannot_start_or_cancel_touch_recognition(self):
        manager = self.native_edges()
        manager.contact(17, 1, -1800, 3, True)
        start = manager.gestures[17].start
        for signature in (0, 0xff515780, 0xff515700, 0x4e47534d):
            manager.user.GetMessageExtraInfo.return_value = signature
            for message in range(0x200, 0x20f):
                self.assertEqual(manager.message(17, message, 0, 0), (False, 0))
        self.assertEqual(manager.gestures[17].start, start)
        manager.user.GetMessageExtraInfo.assert_not_called()
        manager.contact(17, 1, -1800, 126, False)
        manager.dispatch.recognized.emit.assert_called_once_with('portrait-device')

    def test_pointer_messages_cross_strip_boundary_without_any_qt_touch_events(self):
        manager = self.native_edges()
        state = dict(kind=2, flags=4, x=-1800, y=6)
        def info(identity, pointer):
            value = pointer._obj
            value.kind, value.identity, value.flags = state['kind'], identity, state['flags']
            value.pixel.x, value.pixel.y = state['x'], state['y']
            return True
        manager.user.GetPointerInfo.side_effect = info
        self.assertEqual(manager.message(17, 0x24b, 3, 0), (True, 3))
        self.assertEqual(manager.message(17, 0x246, 3, 0), (True, 0))
        state.update(y=126)
        self.assertEqual(manager.message(17, 0x245, 3, 0), (True, 0))
        manager.dispatch.recognized.emit.assert_called_once_with('portrait-device')
        self.assertEqual(manager.message(17, 0x247, 3, 0), (True, 0))
        manager.message(17, 0x24c, 3, 0)
        self.assertFalse(manager.gestures[17].contacts)
        state.update(kind=3)
        self.assertEqual(manager.message(17, 0x246, 4, 0), (False, 0))
        self.assertEqual(manager.message(99, 0x246, 4, 0), (False, 0))

    def test_legacy_touch_cleans_handle_and_ignores_promoted_mouse_duplicate(self):
        manager = self.native_edges()
        state = dict(flags=2, y=600)
        def touch(handle, count, contacts, size):
            contacts[0].identity, contacts[0].x = 9, -180000
            contacts[0].flags, contacts[0].y = state['flags'], state['y']
            return True
        manager.user.GetTouchInputInfo.side_effect = touch
        for flags, y in ((2,600),(1,12600),(4,12600)):
            state.update(flags=flags,y=y)
            self.assertEqual(manager.message(17, 0x240, 1, 1234), (True, 0))
        manager.dispatch.recognized.emit.assert_called_once_with('portrait-device')
        self.assertEqual(manager.user.CloseTouchInputHandle.call_count, 3)
        manager.user.GetMessageExtraInfo.return_value = 0xff515780
        self.assertEqual(manager.message(17, 0x200, 0, 0), (False, 0))
        self.assertEqual(manager.message(17, 0x84, 0, 0), (True, 1))
        manager.user.GetMessageExtraInfo.return_value = 0
        self.assertEqual(manager.message(17, 0x200, 0, 0), (False, 0))
        self.assertEqual(manager.message(17, 0x84, 0, 0), (True, 1))
        self.assertEqual(manager.message(17, 0x240, 0, 4321), (True, 0))
        manager.user.CloseTouchInputHandle.assert_called_with(4321)

    def test_visible_handle_accepts_unknown_first_touch_and_mouse_without_source_guessing(self):
        manager = self.native_edges()
        self.assertEqual(manager.message(17, 0x84, 0, 0), (True, 1))
        manager.user.GetCurrentInputMessageSource.assert_not_called()
        self.assertEqual(manager.message(17, 0x2cc, 0, 0), (True, 0x10001))
        def pointer(identity, output):
            value = output._obj
            value.kind, value.identity, value.flags = 2, identity, 0x12006
            value.pixel.x, value.pixel.y = -1800, 2
            return True
        manager.user.GetPointerInfo.side_effect = pointer
        manager.message(17, 0x246, 7, 0)
        manager.controller.edgeContact.emit.assert_called_once_with('portrait-device')
        manager.contact(17, 7, -1800, 120, False)
        manager.dispatch.recognized.emit.assert_called_once_with('portrait-device')
        manager.dispatch.recognized.emit.reset_mock()
        manager.contact(17, 15, -1800, 60, True)
        manager.dispatch.recognized.emit.assert_called_once_with('portrait-device')

    def test_native_cancellation_invalid_read_and_three_contacts_cannot_open_panel(self):
        manager = self.native_edges()
        manager.contact(17, 1, -1800, 3, True)
        manager.contact(17, 2, -1750, 3, True)
        manager.contact(17, 3, -1700, 3, True)
        manager.contact(17, 1, -1800, 200, True)
        manager.dispatch.recognized.emit.assert_not_called()
        manager.message(17, 0x24c, 1, 0)
        manager.user.GetPointerInfo.return_value = False
        self.assertEqual(manager.message(17, 0x245, 1, 0), (False, 0))
        manager.user.GetTouchInputInfo.return_value = False
        manager.message(17, 0x240, 1, 5)
        manager.user.CloseTouchInputHandle.assert_called_once_with(5)
        self.assertFalse(manager.gestures)
        manager.controller.edgeEnabled = False
        manager.request('portrait-device')
        manager.controller.request.assert_not_called()

    def test_top_edge_direction_distance_timeout_and_dpi(self):
        area = (-1920, 0, 1920, 1080, 1.5)
        for x, y, dx, dy, delay, expected in ((-400, 5, 10, 110, .3, True),
                (-400, 60, 0, 200, .3, False), (-400, 5, 130, 120, .3, False),
                (-400, 5, 0, 45, .3, False), (-400, 5, 0, 120, 1.5, False)):
            gesture = EdgeGesture()
            self.assertFalse(gesture.feed(3, x, y, True, area, 10))
            self.assertEqual(gesture.feed(3, x + dx, y + dy, True, area, 10 + delay), expected)
            if expected: self.assertFalse(gesture.feed(3, x, y + 300, True, area, 10 + delay + .1))

    def test_second_finger_does_not_cancel_or_drive_the_first_finger_swipe(self):
        area = (0, 0, 1280, 800, 1)
        for release_second in (False, True):
            gesture = EdgeGesture()
            self.assertFalse(gesture.feed(1, 100, 2, True, area, 10))
            self.assertFalse(gesture.feed(2, 150, 3, True, area, 10.01))
            self.assertFalse(gesture.feed(2, 150, 150, not release_second, area, 10.1))
            self.assertTrue(gesture.feed(1, 100, 100, True, area, 10.2))
            self.assertFalse(gesture.feed(2, 150, 200, True, area, 10.3))
            self.assertFalse(gesture.feed(1, 100, 200, False, area, 10.4))

    def test_two_finger_native_packets_open_once_and_taps_do_not_open(self):
        for swipe in (False, True):
            manager = self.native_edges()
            manager.contact(17, 11, -1800, 3, True)
            manager.contact(17, 12, -1750, 3, True)
            manager.contact(17, 12, -1750, 10, False)
            manager.contact(17, 11, -1800, 150 if swipe else 10, False)
            if swipe: manager.dispatch.recognized.emit.assert_called_once_with('portrait-device')
            else: manager.dispatch.recognized.emit.assert_not_called()
            self.assertFalse(manager.gestures[17].contacts)

    def test_qt_contacts_keep_their_identity_and_cancel_cannot_finish_a_swipe(self):
        app = QGuiApplication.instance() or QGuiApplication([])
        parent = QObject(); parent.settings = {}
        overlay = StatusOverlay(VirtualKeyboard(parent), parent)
        overlay.edge_enabled = True
        requested = Mock(); overlay.requested.connect(requested)
        overlay.edgeTouch('screen-1', 4, 100, 2, True)
        overlay.edgeTouch('screen-1', 5, 150, 2, True)
        overlay.edgeTouch('screen-1', 5, 150, 100, True)
        requested.assert_not_called()
        overlay.cancelEdgeTouch()
        overlay.edgeTouch('screen-1', 4, 100, 100, False)
        requested.assert_not_called()
        overlay.edgeTouch('screen-1', 6, 100, 2, True)
        overlay.edgeTouch('screen-1', 7, 150, 2, True)
        overlay.edgeTouch('screen-1', 6, 100, 100, False)
        requested.assert_called_once_with('screen-1')
        overlay.close()

    def test_release_and_lost_contact_allow_next_gesture(self):
        area = (0, 0, 1280, 800, 1)
        gesture = EdgeGesture()
        for start in (10, 11, 15):
            self.assertFalse(gesture.feed(1, 100, 2, True, area, start))
            self.assertTrue(gesture.feed(1, 100, 90, True, area, start + .2))
            if start == 10: gesture.feed(1, 100, 90, False, area, start + .3)

    def test_fast_flick_uses_final_position_when_driver_has_no_move_packet(self):
        gesture = EdgeGesture()
        area = (0, 0, 1280, 800, 1)
        self.assertFalse(gesture.feed(1, 100, 2, True, area, 10))
        self.assertTrue(gesture.feed(1, 103, 100, False, area, 10.05))
        self.assertFalse(gesture.contacts)
        self.assertFalse(gesture.feed(1, 103, 100, False, area, 10.06))
        self.assertFalse(gesture.feed(1, 100, 2, True, area, 11))
        self.assertFalse(gesture.feed(1, 100, 30, False, area, 11.05))

    def test_remote_panel_does_not_read_hardware_in_portable_process(self):
        app = QGuiApplication.instance() or QGuiApplication([])
        parent = QObject(); parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard, parent)
        remote = Mock(); overlay.remote = remote
        with patch.object(overlay, 'refreshSystemStatus') as read:
            overlay.show('screen-1')
            remote.status.assert_called_once_with(True, 'screen-1')
            read.assert_not_called()
            self.assertTrue(overlay.visible and overlay.remoteHost)
            overlay.hide()
            self.assertFalse(overlay.visible)
            overlay.close()

    def test_close_waits_for_motion_reopen_cancels_restore_and_sleep_is_immediate(self):
        from PySide6.QtTest import QTest
        app = QGuiApplication.instance() or QGuiApplication([])
        parent = QObject(); parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard, parent)
        overlay.edge_enabled = True
        closed = Mock(); overlay.closed.connect(closed)
        with patch.object(overlay, 'refreshSystemStatus'):
            overlay.show('screen-1'); overlay.hide()
            self.assertFalse(overlay.visible)
            self.assertTrue(overlay.shown and overlay.closing)
            self.assertFalse(overlay.edgeEnabled)
            closed.assert_not_called()
            QTest.qWait(60); overlay.show('screen-1'); QTest.qWait(220)
            self.assertTrue(overlay.visible)
            closed.assert_not_called()
            overlay.hide(); QTest.qWait(250)
            self.assertTrue(overlay.shown, 'Only the hidden window may complete an animated close')
            overlay.finishHide()
            self.assertFalse(overlay.shown)
            self.assertTrue(overlay.edgeEnabled)
            closed.assert_called_once()
            overlay.show('screen-1'); overlay.sleeping = True; overlay.hide()
            self.assertFalse(overlay.shown or overlay.closing)
            self.assertEqual(closed.call_count, 2)
            overlay.close()

    def test_foreign_window_or_frozen_game_cannot_be_parked(self):
        process = Mock()
        process.window_api.return_value.GetForegroundWindow.return_value = 100
        process.windows.return_value = [(200, 9)]
        bridge = SimpleNamespace(process=process, gameFrozen=False, _terminating=False)
        self.assertIsNone(park_fullscreen_game(bridge))

    def test_only_owned_foreground_fullscreen_is_parked(self):
        process = Mock(); user = process.window_api.return_value
        user.GetForegroundWindow.return_value = 100; process.windows.return_value = [(100, 9)]
        def rectangle(hwnd, rect):
            rect._obj.left = rect._obj.top = 0; rect._obj.right = 1920; rect._obj.bottom = 1080
            return True
        def monitor(handle, info):
            info._obj.rect.right = 1920; info._obj.rect.bottom = 1080
            return True
        user.GetWindowRect.side_effect = rectangle; user.GetMonitorInfoW.side_effect = monitor
        bridge = SimpleNamespace(process=process, gameFrozen=False, _terminating=False)
        self.assertIs(park_fullscreen_game(bridge), process)
        process.park_windows.assert_called_once()
        process.park_windows.reset_mock()
        def windowed(hwnd, rect):
            rectangle(hwnd, rect); rect._obj.right = 900; return True
        user.GetWindowRect.side_effect = windowed
        self.assertIsNone(park_fullscreen_game(bridge))
        process.park_windows.assert_not_called()
        process.park_windows.assert_not_called()
        bridge.gameFrozen = True
        self.assertIsNone(park_fullscreen_game(bridge))


    def test_handle_pointer_history_and_launcher_backing_attachment(self):
        manager = self.native_edges()
        backing = Mock()
        backing.devicePixelRatio.return_value = 1.5
        geom = Mock(); geom.x.return_value = 0; geom.y.return_value = 0; geom.width.return_value = 1280; geom.height.return_value = 800
        backing.screen.return_value.geometry.return_value = geom
        backing.screen.return_value.name.return_value = 'portrait-device'
        manager.backings = {55: backing}
        def info(identity, pointer):
            v = pointer._obj
            v.kind, v.identity, v.flags, v.history = 2, identity, 4, 2
            v.pixel.x, v.pixel.y = 960, 120
            return True
        def history(identity, count_ptr, array):
            count_ptr._obj.value = 2
            array[0].pixel.x, array[0].pixel.y = 960, 120
            array[1].pixel.x, array[1].pixel.y = 960, 12
            return True
        manager.user.GetPointerInfo.side_effect = info
        manager.user.GetPointerInfoHistory.side_effect = history
        manager.user.ScreenToClient.side_effect = lambda hwnd, point: True
        self.assertEqual(manager.message(17, 0x246, 9, 0), (True, 0))
        manager.dispatch.recognized.emit.assert_called_once_with('portrait-device')
        manager.dispatch.recognized.emit.reset_mock()
        def history_left(identity, count_ptr, array):
            count_ptr._obj.value = 2
            array[0].pixel.x, array[0].pixel.y = 50, 120
            array[1].pixel.x, array[1].pixel.y = 50, 12
            return True
        manager.user.GetPointerInfoHistory.side_effect = history_left
        self.assertEqual(manager.message(55, 0x246, 10, 0), (False, 0))
        manager.dispatch.recognized.emit.assert_not_called()
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture') as disable_edge:
            window = Mock(isVisible=Mock(return_value=True))
            window.winId.return_value = 77
            manager.attach_backing(window)
            disable_edge.assert_called_once_with(77, True)
            self.assertIs(manager.backings[77], window)

    def test_outside_touch_and_drag_progress_and_close_button_navigation(self):
        from pathlib import Path
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent, QQmlEngine
        from ngs.input_host import HostAppearance
        app = QGuiApplication.instance() or QGuiApplication([])
        parent = QObject(); parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard, parent)
        overlay.edge_enabled = True
        overlay.requested.connect(overlay.show)
        repo = Path(__file__).resolve().parents[1]
        appearance, engine = HostAppearance(), QQmlEngine()
        engine.rootContext().setContextProperty('uiTheme', appearance)
        status_comp = QQmlComponent(engine, QUrl.fromLocalFile(str(repo / 'qml/StatusBarWindow.qml')))
        status_win = status_comp.createWithInitialProperties({'bridge': overlay})
        edge_comp = QQmlComponent(engine, QUrl.fromLocalFile(str(repo / 'qml/EdgeSwipeWindow.qml')))
        edge_win = edge_comp.createWithInitialProperties({'controller': overlay})
        self.assertIsNotNone(status_win, str(status_comp.errors()))
        self.assertIsNotNone(edge_win, str(edge_comp.errors()))
        try:
            from PySide6.QtTest import QTest
            pill = edge_win.findChild(QObject, 'topEdgeHandlePill')
            bar = status_win.findChild(QObject, 'globalStatusBar')
            self.assertIsNotNone(pill)
            self.assertIsNotNone(bar)
            overlay.edgeTouch('screen-1', 1, 100, 2, True)
            overlay.edgeTouch('screen-1', 1, 100, 48, True)
            QTest.qWait(10)
            self.assertGreater(overlay.dragProgress, 0.4)
            self.assertEqual(pill.property('opacity'), 0)
            self.assertGreater(bar.property('y'), -50)
            self.assertTrue(status_win.isVisible())
            self.assertTrue(edge_win.isVisible())
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.edgeTouch('screen-1', 1, 100, 96, True)
            self.assertTrue(overlay.shown)
            self.assertGreater(overlay.dragProgress, 0.8)
            overlay.edgeTouch('screen-1', 1, 100, 150, True)
            QTest.qWait(50)
            self.assertGreater(status_win.property('handleStretch'), 10)
            badge = status_win.findChild(QObject, 'statusSelectionBadge')
            self.assertLessEqual(badge.property('y') + badge.property('height'), status_win.height())
            overlay.edgeTouch('screen-1', 1, 100, 96, False)
            self.assertEqual(overlay.dragProgress, 0.0)
            self.assertTrue(overlay.shown)
            self.assertEqual(pill.property('opacity'), 0)
            self.assertTrue(edge_win.isVisible(), 'Handle HWND must remain alive to avoid DWM flicker')
            close_btn = status_win.findChild(QObject, 'globalStatusClose')
            badge_text = status_win.findChild(QObject, 'statusSelectionText')
            self.assertIsNotNone(close_btn)
            self.assertIsNotNone(badge)
            self.assertTrue(badge.property('visible'))
            self.assertIn('Дата и время', badge_text.property('text'))
            overlay.action.emit('left')
            self.assertEqual(status_win.property('selectedIndex'), 8)
            self.assertTrue(close_btn.property('highlighted'))
            self.assertEqual(badge_text.property('text'), 'Закрыть панель')
            overlay.action.emit('activate')
            self.assertTrue(overlay.closing)
            overlay.finishHide()
            QTest.qWait(250)
            self.assertGreater(edge_win.property('reveal'), 0.9)
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.show('screen-1')
            QTest.qWait(280)
            self.assertAlmostEqual(status_win.property('reveal'), 1.0, places=2)
            status_win.setProperty('pullOffset', -75.0)
            y_before_hide = bar.property('y')
            overlay.hide()
            self.assertFalse(status_win.property('presenting'))
            self.assertEqual(status_win.property('pullOffset'), 0.0)
            for _ in range(30):
                self.assertLessEqual(bar.property('y'), y_before_hide + 1.0,
                                     'Panel must never jump downward/blink after swipe-up dismiss')
                if not status_win.isVisible():
                    break
                QTest.qWait(10)
            self.assertFalse(status_win.isVisible())
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.show('screen-1')
            manager = self.native_edges()
            manager.controller = overlay
            manager.status_window = Mock(isVisible=Mock(return_value=True), winId=Mock(return_value=77))
            def status_rect(hwnd, rect):
                rect._obj.left, rect._obj.top, rect._obj.right, rect._obj.bottom = 300, 0, 900, 110
                return True
            manager.user.GetWindowRect.side_effect = status_rect
            manager.backings = {55: Mock()}
            state = dict(x=500, y=50)
            def info(identity, output):
                output._obj.kind = 2
                output._obj.pixel.x, output._obj.pixel.y = state['x'], state['y']
                return True
            manager.user.GetPointerInfo.side_effect = info
            manager.message(55, 0x246, 1, 0)
            manager.dispatch.dismissed.emit.assert_not_called()
            state.update(x=100,y=300)
            manager.message(55, 0x246, 1, 0)
            manager.dispatch.dismissed.emit.assert_called_once()
            from ngs.desktop import WindowsWindowEvents
            events = WindowsWindowEvents.__new__(WindowsWindowEvents)
            events.hooks = []
            events.callback = Mock()
            events.user = Mock()
            events.user.SetWinEventHook.side_effect = [101, 102, 201, 202]
            events.set_enabled(True, reorder=False)
            self.assertEqual([c.args[:2] for c in events.user.SetWinEventHook.call_args_list], [(3, 7), (0x8002, 0x8003)])
            events.user.SetWinEventHook.reset_mock()
            events.set_enabled(True, reorder=True)
            self.assertEqual([c.args[:2] for c in events.user.SetWinEventHook.call_args_list], [(3, 7), (0x8002, 0x8004)])
        finally:
            status_win.hide(); status_win.deleteLater()
            edge_win.hide(); edge_win.deleteLater()
            engine.deleteLater()
            overlay.close(); keyboard.close()
