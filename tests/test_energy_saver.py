import ctypes
import unittest
from unittest.mock import Mock, patch

from ngs import energy_saver as energy


class EnergySaverTests(unittest.TestCase):
    def test_read_is_read_only_and_unknown_layout_disables_control(self):
        dll = Mock()
        with patch.object(energy.ctypes, 'WinDLL', return_value=dll), patch.object(energy, '_query', return_value=(2, 17)), patch.object(energy, '_effective', return_value=False):
            self.assertEqual(energy.energy_saver()['energy_saver'], False)
            dll.NtUpdateWnfStateData.assert_not_called()
        with patch.object(energy.ctypes, 'WinDLL', return_value=dll), patch.object(energy, '_query', side_effect=OSError('unknown payload')):
            self.assertFalse(energy.energy_saver()['energy_saver_available'])
            with self.assertRaisesRegex(OSError, 'unknown payload'): energy.energy_saver(True)
        dll.NtUpdateWnfStateData.assert_not_called()

    def test_fixed_override_uses_compare_stamp_and_waits_for_effective_confirmation(self):
        dll = Mock(); dll.NtUpdateWnfStateData.return_value = 0
        with patch.object(energy.ctypes, 'WinDLL', return_value=dll), patch.object(energy, '_query', return_value=(2, 17)), patch.object(energy, '_effective', side_effect=[False, False, True]), patch.object(energy.time, 'sleep'):
            self.assertTrue(energy.energy_saver(True)['energy_saver'])
        args = dll.NtUpdateWnfStateData.call_args.args
        self.assertEqual(args[0]._obj.value, energy.OVERRIDE); self.assertEqual(args[1]._obj.value, 1)
        self.assertEqual(args[2:], (4, None, None, 17, True)); dll.NtUpdateWnfStateData.assert_called_once()

    def test_denial_is_error_and_unconfirmed_request_restores_previous_override(self):
        dll = Mock(); dll.NtUpdateWnfStateData.return_value = ctypes.c_long(0xc0000022).value
        with patch.object(energy.ctypes, 'WinDLL', return_value=dll), patch.object(energy, '_query', return_value=(2, 17)), patch.object(energy, '_effective', return_value=False):
            with self.assertRaisesRegex(OSError, 'C0000022'): energy.energy_saver(True)
        dll.NtUpdateWnfStateData.reset_mock(); dll.NtUpdateWnfStateData.return_value = 0
        with patch.object(energy.ctypes, 'WinDLL', return_value=dll), patch.object(energy, '_query', side_effect=[(0, 17), (1, 18)]), patch.object(energy, '_effective', return_value=False), patch.object(energy.time, 'sleep'):
            with self.assertRaisesRegex(OSError, 'не включила'): energy.energy_saver(True)
        calls = dll.NtUpdateWnfStateData.call_args_list
        self.assertEqual([c.args[1]._obj.value for c in calls], [1, 0]); self.assertEqual(calls[-1].args[-2:], (18, True))

    def test_another_users_concurrent_override_is_not_restored_over(self):
        dll = Mock(); dll.NtUpdateWnfStateData.return_value = 0
        for observed in (1, 2):
            dll.NtUpdateWnfStateData.reset_mock()
            with patch.object(energy.ctypes, 'WinDLL', return_value=dll), patch.object(energy, '_query', side_effect=[(0, 17), (observed, 19)]), patch.object(energy, '_effective', return_value=False), patch.object(energy.time, 'sleep'):
                with self.assertRaises(OSError): energy.energy_saver(True)
            dll.NtUpdateWnfStateData.assert_called_once()

    def test_invalid_value_never_calls_windows(self):
        with patch.object(energy.ctypes, 'WinDLL') as dll:
            for value in (1, 'on', {}, float('nan')):
                with self.assertRaises(ValueError): energy.energy_saver(value)
            dll.assert_not_called()

    def test_query_checks_exact_size_and_value(self):
        dll = Mock()
        def query(name, kind, scope, stamp, out, size):
            stamp._obj.value = 12; out._obj.value = 2; size._obj.value = 4
            return 0
        dll.NtQueryWnfStateData.side_effect = query
        self.assertEqual(energy._query(dll), (2, 12))
        def invalid(*args):
            query(*args); args[-1]._obj.value = 0
            return 0
        dll.NtQueryWnfStateData.side_effect = invalid
        with self.assertRaises(OSError): energy._query(dll)
