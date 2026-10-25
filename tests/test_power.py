import ctypes
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ngs.power import PowerNotifications, PowerSetting, DISPLAY_STATUS


class PowerTests(unittest.TestCase):
    def monitor(self, suspend, resume):
        with patch("ngs.power.sys", SimpleNamespace(platform="test")):
            return PowerNotifications(suspend, resume)

    def display(self, monitor, state, guid=DISPLAY_STATUS, length=4):
        setting = PowerSetting((ctypes.c_ubyte * 16).from_buffer_copy(guid), length, state)
        return monitor.notify(None, 0x8013, ctypes.byref(setting))

    @unittest.skipUnless(sys.platform == "win32", "Windows power subscriptions")
    def test_native_sleep_and_display_subscriptions_close_twice(self):
        monitor = PowerNotifications(Mock(), Mock())
        try:
            self.assertTrue(monitor.handle)
            self.assertTrue(monitor.display_handle)
        finally:
            monitor.close()
            monitor.close()
        self.assertFalse(monitor.handle)
        self.assertFalse(monitor.display_handle)

    @unittest.skipUnless(sys.platform == "win32", "Windows power subscriptions")
    def test_display_subscription_failure_closes_sleep_subscription(self):
        from ctypes import wintypes
        api = Mock()
        def register(flags, subscription, handle):
            ctypes.cast(handle, ctypes.POINTER(wintypes.HANDLE)).contents.value = 123
            return 0
        api.PowerRegisterSuspendResumeNotification.side_effect = register
        api.PowerSettingRegisterNotification.return_value = 5
        with patch("ngs.power.ctypes.WinDLL", return_value=api):
            with self.assertRaises(OSError):
                PowerNotifications(Mock(), Mock())
        api.PowerUnregisterSuspendResumeNotification.assert_called_once()
        self.assertEqual(api.PowerUnregisterSuspendResumeNotification.call_args.args[0].value, 123)
        api.PowerSettingUnregisterNotification.assert_not_called()

    def test_suspend_is_synchronous_resume_waits_for_user_and_error_is_reported(self):
        suspend, resume = Mock(), Mock()
        monitor = self.monitor(suspend, resume)
        changes, errors = [], []
        monitor.changed.connect(lambda: changes.append(True))
        monitor.failed.connect(errors.append)
        try:
            monitor.notify(None, 4, None)
            self.assertTrue(monitor.sleeping)
            suspend.assert_called_once()
            self.assertEqual(changes, [True])
            resume.assert_not_called()
            monitor.notify(None, 18, None)
            self.assertFalse(monitor.sleeping)
            resume.assert_called_once()
            self.assertEqual(suspend.call_count, 1)
            suspend.side_effect = OSError("access denied")
            with self.assertLogs(level="ERROR"):
                self.assertEqual(monitor.notify(None, 4, None), 0)
            self.assertIn("access denied", errors[0])
            self.assertEqual(len(changes), 1)
        finally:
            monitor.close()
            monitor.close()

    def test_modern_standby_freezes_on_display_off_and_background_wake_keeps_screen_hidden(self):
        suspend, resume = Mock(), Mock()
        monitor = self.monitor(suspend, resume)
        try:
            self.display(monitor, 1)  # Initial registration snapshot.
            self.display(monitor, 2)  # Dimming alone is not sleep.
            self.display(monitor, 1, guid=bytes(16))
            self.display(monitor, 0, length=1)
            self.display(monitor, 9)
            suspend.assert_not_called()
            self.display(monitor, 0)
            suspend.assert_called_once()
            self.assertTrue(monitor.sleeping)
            self.display(monitor, 0)
            suspend.assert_called_once()
            monitor.notify(None, 18, None)  # Automatic maintenance wake, display still off.
            monitor.notify(None, 7, None)
            resume.assert_not_called()
            self.assertTrue(monitor.sleeping)
            self.display(monitor, 1)
            self.assertFalse(monitor.sleeping)
            resume.assert_called_once()
            monitor.notify(None, 7, None)
            resume.assert_called_once()
        finally:
            monitor.close()

    def test_initial_off_snapshot_does_not_freeze_and_failure_remains_asleep_until_display_on(self):
        suspend, resume = Mock(), Mock()
        monitor = self.monitor(suspend, resume)
        errors = []
        monitor.failed.connect(errors.append)
        try:
            self.display(monitor, 0)
            suspend.assert_not_called()
            self.display(monitor, 1)
            resume.assert_not_called()
            suspend.side_effect = OSError("denied")
            with self.assertLogs(level="ERROR"):
                self.display(monitor, 0)
            self.assertTrue(monitor.sleeping)
            self.assertIn("denied", errors[0])
            self.display(monitor, 1)
            resume.assert_called_once()
        finally:
            monitor.close()
