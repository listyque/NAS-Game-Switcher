import ctypes
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from ngs import brightness as screen


class BrightnessTests(unittest.TestCase):
    def test_invalid_values_never_open_wmi(self):
        with patch.object(screen, '_connection') as connect:
            for value in (-1, 101, True, '50', float('nan')):
                with self.subTest(value=value), self.assertRaises(ValueError): screen.brightness(value)
            connect.assert_not_called()

    def test_nearest_level_targets_same_monitor_and_same_level_never_writes(self):
        monitor = dict(identity='internal', brightness=50, levels=[0, 25, 50, 75, 100])
        with patch.object(screen, '_connection') as connect, patch.object(screen, '_monitor', return_value=monitor), patch.object(screen, '_set', return_value=75) as write:
            self.assertEqual(screen.brightness(65), dict(brightness=75))
            write.assert_called_once_with(connect.return_value.__enter__.return_value, 'internal', 75)
        with patch.object(screen, '_connection'), patch.object(screen, '_monitor', return_value=monitor), patch.object(screen, '_set') as write:
            self.assertEqual(screen.brightness(50), dict(brightness=50)); write.assert_not_called()

    def test_wmi_method_uses_zero_transition_timeout_and_checks_provider_result(self):
        @contextmanager
        def pointer(): yield ctypes.c_void_p(1)
        @contextmanager
        def bstr(text): yield text
        def get(item, name): return {'InstanceName':'internal', '__RELPATH':'own-monitor', 'ReturnValue':0}[name]
        writes = {}; reply = dict(code=0, output=1, status=0)
        def call(item, index, types=(), args=()):
            if index == 6:
                self.assertEqual(args[0], 'WmiMonitorBrightnessMethods')
                args[3]._obj.value = 42
            if index == 19:
                # An instance must reject GetMethod (the real 8004101E regression).
                self.assertEqual(item.value, 42, 'GetMethod requires the class returned by GetObject')
            if index == 5:
                variant = args[2]._obj
                writes[args[0]] = (variant.vt, ctypes.cast(ctypes.byref(variant, 8), ctypes.POINTER(ctypes.c_long)).contents.value)
            if index == 3:
                self.assertEqual(args[0], 0, 'Do not block on a reply before checking actual brightness')
                args[1]._obj.value = reply['output']
                return reply['code']
            return 0
        with patch.object(screen, '_pointer', pointer), patch.object(screen, '_bstr', bstr), patch.object(screen, '_query', return_value=[ctypes.c_void_p(2)]), patch.object(screen, '_get', get), patch.object(screen, '_call', side_effect=call) as calls, patch.object(screen, '_monitor', return_value=dict(brightness=75)) as actual:
            screen._set(ctypes.c_void_p(3), 'internal', 75)
            self.assertEqual(writes, dict(Timeout=(3, 0), Brightness=(17, 75)))
            method = next(c for c in calls.call_args_list if c.args[1] == 24)
            self.assertEqual(method.args[3][:3], ('own-monitor', 'WmiSetBrightness', 0x10))
            for code, output in ((0x40004, 0), (0, 0)):
                reply.update(code=code, output=output)
                self.assertEqual(screen._set(ctypes.c_void_p(3), 'internal', 75), 75)
                self.assertEqual(actual.call_args.args[1], 'internal')
            with patch.object(screen, '_monitor', side_effect=[dict(brightness=50), dict(brightness=75)]), patch.object(screen.time, 'sleep'):
                self.assertEqual(screen._set(ctypes.c_void_p(3), 'internal', 75), 75)
            with patch.object(screen, '_monitor', return_value=dict(brightness=50)), patch.object(screen.time, 'monotonic', side_effect=[0, 3]):
                with self.assertRaisesRegex(OSError, 'не подтвердил'): screen._set(ctypes.c_void_p(3), 'internal', 75)
            with patch.object(screen, '_monitor', side_effect=OSError('monitor lost')):
                with self.assertRaisesRegex(OSError, 'monitor lost'): screen._set(ctypes.c_void_p(3), 'internal', 75)
            reply.update(code=0, output=1)
            with patch.object(screen, '_get', side_effect=lambda item, name: 5 if name == 'ReturnValue' else get(item, name)):
                with self.assertRaisesRegex(OSError, 'WmiSetBrightness: 5'): screen._set(ctypes.c_void_p(3), 'internal', 50)

    def test_cold_enumeration_retries_positive_timeout_but_honors_deadline(self):
        @contextmanager
        def pointer(): yield ctypes.c_void_p(1)
        @contextmanager
        def bstr(text): yield text
        attempt = []
        def call(item, index, types=(), args=()):
            if index != 4: return 0
            attempt.append(1)
            if len(attempt) == 1: return 0x40004
            args[3]._obj.value = 1
            return 0
        with patch.object(screen, '_pointer', pointer), patch.object(screen, '_bstr', bstr), patch.object(screen, '_call', side_effect=call), patch.object(screen.time, 'monotonic', return_value=0):
            self.assertIsNotNone(next(screen._query(None, 'monitor')))
            self.assertEqual(len(attempt), 2)
        with patch.object(screen, '_pointer', pointer), patch.object(screen, '_bstr', bstr), patch.object(screen, '_call', side_effect=lambda item,index,*unused: 0x40004 if index==4 else 0), patch.object(screen.time, 'monotonic', side_effect=[0, 6]):
            with self.assertRaisesRegex(OSError, 'не ответил вовремя'): next(screen._query(None, 'monitor'))

    def test_variant_layout_is_windows_x64_compatible(self):
        self.assertEqual(ctypes.sizeof(screen.Variant), 24)
        self.assertEqual(screen.Variant.data.offset, 8)
