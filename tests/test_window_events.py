import unittest
from unittest.mock import Mock, call, patch

from ngs.desktop import WindowsWindowEvents


class WindowEventsTests(unittest.TestCase):
    def setUp(self):
        self.user, self.changed = Mock(), Mock()
        with patch('ngs.desktop.ctypes.WinDLL', return_value=self.user):
            self.events = WindowsWindowEvents(self.changed)

    def test_restack_is_stable_after_own_reorder_and_restores_after_foreign_raise(self):
        order = [20, 10]
        self.user.GetTopWindow.side_effect = lambda unused: order[0]
        self.user.GetWindow.side_effect = lambda hwnd, unused: order[order.index(hwnd)+1] if order.index(hwnd)+1 < len(order) else 0
        self.user.IsWindowVisible.return_value = True
        self.user.GetWindowLongW.return_value = 8
        def raise_window(hwnd, enabled):
            order.remove(hwnd); order.insert(0, hwnd)
        with patch('ngs.process.set_topmost', side_effect=raise_window) as raised, \
                patch('ngs.edge_swipe.enable_dwm_alpha_composition'):
            self.assertTrue(self.events.restack([10]))
            self.assertEqual(order, [10, 20])
            self.assertFalse(self.events.restack([10]))
            raised.assert_called_once_with(10, True)
            self.events.hooks = [41]
            self.user.GetDesktopWindow.return_value = 1
            self.events.notify(41, 0x8004, 1, -4, 0, 999, 1)
            self.changed.assert_called_once_with()
            self.assertFalse(self.events.restack([10]))
            order[:] = [20, 10]
            self.assertTrue(self.events.restack([10]))
            self.assertEqual(raised.call_count, 2)
            self.user.SetForegroundWindow.assert_not_called()

    def test_restack_tracks_overlay_order_and_releases_guard_on_failure(self):
        self.user.GetTopWindow.return_value = 0
        self.user.GetWindowLongW.return_value = 8
        with patch('ngs.process.set_topmost') as raised, patch('ngs.edge_swipe.enable_dwm_alpha_composition'):
            self.events.restack([10, 20])
            self.events.restack([20, 10])
            self.assertEqual(raised.call_args_list, [call(10, True), call(20, True), call(20, True), call(10, True)])
        self.events._stack_state = None
        with patch('ngs.process.set_topmost', side_effect=OSError('fixture failure')) as raised, \
                patch('ngs.edge_swipe.enable_dwm_alpha_composition'):
            with self.assertRaises(OSError): self.events.restack([10])
            raised.side_effect = None
            self.assertTrue(self.events.restack([10]))
        self.assertFalse(self.events._restacking)
        self.events.hooks = [41]
        self.events._restacking = True
        self.events.notify(41, 0x8004, 1, -4, 0, 999, 1)
        self.changed.assert_not_called()

    def test_hooks_only_run_while_enabled_and_skip_own_process(self):
        self.user.SetWinEventHook.side_effect = [41, 42, 43, 44]
        self.events.set_enabled(True)
        self.events.set_enabled(True)
        self.assertEqual(self.user.SetWinEventHook.call_count, 2)
        self.assertEqual([(c.args[0:3], c.args[4:]) for c in self.user.SetWinEventHook.call_args_list],
                         [((3, 7, None), (0, 0, 2)), ((0x8002, 0x8004, None), (0, 0, 2))])
        self.events.set_enabled(False)
        self.events.close()
        self.assertEqual(self.user.UnhookWinEvent.call_args_list, [call(41), call(42)])
        self.events.set_enabled(True)
        self.assertEqual(self.events.hooks, [43, 44])
        self.events.close()

    def test_only_foreground_menus_and_top_level_window_events_trigger_restack(self):
        self.events.hooks = [41]
        self.user.GetAncestor.return_value = 99
        self.events.notify(41, 3, 99, -4, 0, 1, 0)
        self.events.notify(41, 6, 99, -4, 0, 1, 0)
        self.events.notify(41, 0x8002, 99, 0, 0, 1, 0)
        self.events.notify(41, 0x8004, 99, 0, 0, 1, 0)
        self.assertEqual(self.changed.call_count, 4)
        for hwnd, obj, child in ((0, 0, 0), (99, -4, 0), (99, 0, 1)):
            self.events.notify(41, 0x8004, hwnd, obj, child, 1, 0)
        self.user.GetAncestor.return_value = 10
        self.events.notify(41, 0x8002, 99, 0, 0, 1, 0)
        self.user.GetDesktopWindow.return_value = 1
        self.events.notify(41, 0x8004, 1, -4, 0, 1, 0)
        self.events.close()
        self.events.notify(41, 3, 99, 0, 0, 1, 0)
        self.assertEqual(self.changed.call_count, 5)

    def test_partial_registration_failure_releases_the_previous_hook(self):
        self.user.SetWinEventHook.side_effect = [41, 0]
        with self.assertRaises(OSError):
            self.events.set_enabled(True)
        self.user.UnhookWinEvent.assert_called_once_with(41)
        self.assertEqual(self.events.hooks, [])


if __name__ == '__main__':
    unittest.main()
