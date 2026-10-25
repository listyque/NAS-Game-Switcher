import ctypes
import unittest
from unittest.mock import Mock, patch, call

from ngs.touchpad import NativeTouchpads, _PointerInfo, _TouchInput


class TouchpadTests(unittest.TestCase):
    def tearDown(self): self.pad.clear()

    def setUp(self):
        self.keyboard = Mock(touchpadMode=True)
        self.window = Mock()
        self.window.isVisible.return_value = True
        self.window.winId.return_value = 42
        self.window.width.return_value = 400
        self.window.devicePixelRatio.return_value = 2
        self.window.property.side_effect = lambda name: {'padTop': 45, 'padBottom': 300, 'rightSide': False,
            'toolRects': [dict(index=i,x=6+i*130,y=6,width=126,height=36) for i in range(3)] + [dict(index=3,x=6,y=310,width=388,height=36),dict(index=5,x=354,y=270,width=40,height=34)]}[name]
        with patch('ngs.touchpad.ctypes.WinDLL', return_value=Mock(), create=True):
            self.pad = NativeTouchpads(self.keyboard, [self.window])

    def test_first_contact_never_moves_cursor_and_each_stroke_is_relative(self):
        self.pad.contact(42, 0x246, 1, 100, 150, self.window)
        self.keyboard.movePointer.assert_not_called()
        self.pad.contact(42, 0x245, 1, 120, 155, self.window)
        self.pad.contact(42, 0x247, 1, 120, 155, self.window)
        self.pad.contact(42, 0x246, 2, 300, 250, self.window)
        self.pad.contact(42, 0x245, 2, 305, 245, self.window)
        self.pad.contact(42, 0x247, 2, 305, 245, self.window)
        self.assertEqual(self.keyboard.movePointer.call_args_list, [call(20, 5), call(5, -5)])
        self.keyboard.clickPointer.assert_not_called()
        self.assertEqual(self.pad.contacts, {})

    def test_single_tap_left_two_finger_tap_right_and_scroll_without_cursor_move(self):
        self.pad.contact(42, 0x246, 1, 100, 150, self.window)
        self.pad.contact(42, 0x247, 1, 100, 150, self.window)
        self.keyboard.clickPointer.assert_called_once_with('left')
        self.pad.contact(42, 0x246, 2, 100, 150, self.window)
        self.pad.contact(42, 0x246, 3, 150, 150, self.window)
        self.pad.contact(42, 0x247, 2, 100, 150, self.window)
        self.pad.contact(42, 0x247, 3, 150, 150, self.window)
        self.keyboard.clickPointer.assert_called_with('right')
        self.pad.contact(42, 0x246, 4, 100, 150, self.window)
        self.pad.contact(42, 0x246, 5, 150, 150, self.window)
        self.pad.contact(42, 0x245, 4, 100, 198, self.window)
        self.pad.contact(42, 0x245, 5, 150, 198, self.window)
        self.assertEqual(self.keyboard.scrollPointer.call_args_list, [call(24), call(24)])
        self.pad.contact(42, 0x247, 4, 100, 198, self.window)
        self.pad.contact(42, 0x247, 5, 150, 198, self.window)
        self.keyboard.movePointer.assert_not_called()
        self.assertEqual(self.keyboard.clickPointer.call_count, 2)

    def test_native_touch_is_consumed_before_promotion_mouse_and_other_windows_pass(self):
        state = dict(kind=2, x=200, y=300, valid=True)
        def kind(identity, pointer): ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32)).contents.value = state['kind']; return True
        def info(identity, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(_PointerInfo)).contents
            value.pixel.x, value.pixel.y = state['x'], state['y']
            return state['valid']
        self.pad.user.GetPointerType.side_effect = kind
        self.pad.user.GetPointerInfo.side_effect = info
        self.pad.user.ScreenToClient.return_value = True
        self.assertFalse(self.pad.message(99, 0x246, 1))
        self.assertTrue(self.pad.message(42, 0x246, 1))
        self.keyboard.movePointer.assert_not_called()
        self.keyboard.pointer_timer.start.assert_called_once_with(0)
        state.update(x=220, y=310)
        self.assertTrue(self.pad.message(42, 0x245, 1))
        self.keyboard.movePointer.assert_called_once_with(10, 5)
        self.assertTrue(self.pad.message(42, 0x24c, 1))
        self.assertEqual(self.pad.contacts, {})
        self.assertEqual(self.keyboard.pointer_timer.start.call_count,3)
        state['kind'] = 4
        self.assertFalse(self.pad.message(42, 0x246, 2))
        state.update(kind=2, valid=False)
        self.assertTrue(self.pad.message(42, 0x246, 3))

    def test_toolbar_taps_do_not_move_cursor_and_canceled_contacts_do_not_click(self):
        self.pad.contact(42, 0x246, 1, 20, 20, self.window)
        self.pad.contact(42, 0x247, 1, 20, 20, self.window)
        self.keyboard.padTool.emit.assert_called_once_with(False, 0)
        self.keyboard.clickPointer.assert_not_called()
        self.pad.contact(42, 0x246, 2, 200, 150, self.window)
        self.pad.cancel(42)
        self.pad.contact(42, 0x247, 2, 200, 150, self.window)
        self.keyboard.clickPointer.assert_not_called()

    def test_footer_and_adjustments_are_tools_and_empty_edges_do_not_click_or_move(self):
        for identity,x,y,tool in ((1,100,325,3),(2,370,280,5)):
            self.pad.contact(42,0x246,identity,x,y,self.window)
            self.pad.contact(42,0x247,identity,x,y,self.window)
            self.keyboard.padTool.emit.assert_called_with(False,tool)
        self.pad.contact(42,0x246,3,1,2,self.window)
        self.pad.contact(42,0x245,3,3,2,self.window)
        self.pad.contact(42,0x247,3,3,2,self.window)
        self.keyboard.movePointer.assert_not_called(); self.keyboard.clickPointer.assert_not_called()

    def test_pointer_activation_keeps_foreground_and_other_windows_pass(self):
        from ctypes import wintypes
        event = wintypes.MSG()
        event.hWnd, event.message = 42, 0x24b
        self.assertEqual(self.pad.nativeEventFilter(None, ctypes.addressof(event)), (True, 3))
        event.hWnd = 99
        self.assertEqual(self.pad.nativeEventFilter(None, ctypes.addressof(event)), (False, 0))

    def test_legacy_touch_is_relative_and_closes_each_native_handle(self):
        from ctypes import wintypes
        state = dict(x=20000,y=30000,flags=2)
        def read(handle,count,values,size):
            self.assertEqual(size,ctypes.sizeof(_TouchInput))
            self.assertEqual(count,1)
            values[0].identity=9; values[0].x=state['x']; values[0].y=state['y']; values[0].flags=state['flags']
            return True
        self.pad.user.GetTouchInputInfo.side_effect=read
        self.pad.user.ScreenToClient.return_value=True
        event=wintypes.MSG(); event.hWnd=42; event.message=0x240; event.wParam=1; event.lParam=123
        self.assertEqual(self.pad.nativeEventFilter(None,ctypes.addressof(event)),(True,0))
        self.keyboard.movePointer.assert_not_called()
        state.update(x=22000,y=31000,flags=1)
        self.assertEqual(self.pad.nativeEventFilter(None,ctypes.addressof(event)),(True,0))
        self.keyboard.movePointer.assert_called_once_with(10,5)
        state['flags']=4
        self.pad.nativeEventFilter(None,ctypes.addressof(event))
        self.assertFalse(self.pad.contacts)
        self.assertEqual(self.pad.user.CloseTouchInputHandle.call_args_list,[call(123)]*3)
        self.keyboard.clickPointer.assert_not_called()

    def test_touch_promoted_mouse_is_eaten_but_physical_and_own_mouse_are_untouched(self):
        from ctypes import wintypes
        event=wintypes.MSG(); event.hWnd=42
        self.pad.user.GetMessageExtraInfo.return_value=0xff515780
        for message in (0x200,0x201,0x202):
            event.message=message
            self.assertEqual(self.pad.nativeEventFilter(None,ctypes.addressof(event)),(True,0))
        self.keyboard.clickPointer.assert_not_called(); self.keyboard.movePointer.assert_not_called()
        for extra in (0,0x4e47534b,0xff515700):
            self.pad.user.GetMessageExtraInfo.return_value=extra
            self.assertEqual(self.pad.nativeEventFilter(None,ctypes.addressof(event)),(False,0))

    def test_window_creation_messages_never_request_a_native_window_again(self):
        from ctypes import wintypes
        event=wintypes.MSG(); event.hWnd=42
        for message in (0x1,0x81,0x18,0x47):
            event.message=message
            self.assertEqual(self.pad.nativeEventFilter(None,ctypes.addressof(event)),(False,0))
        self.window.winId.assert_not_called()

    def test_native_print_screen_hold_captures_full_screen_once_without_a_short_tap(self):
        with patch('ngs.touchpad.time.monotonic',side_effect=[1.,1.8]):
            self.pad.contact(42,0x246,1,200,20,self.window)
            self.pad.contact(42,0x247,1,200,20,self.window)
        self.keyboard.takeScreenshot.assert_called_once_with(True)
        self.keyboard.padTool.emit.assert_not_called(); self.keyboard.clickPointer.assert_not_called()
