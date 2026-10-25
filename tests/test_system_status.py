import unittest
from unittest.mock import Mock, patch

from ngs import system_status


class SystemStatusTests(unittest.TestCase):
    def test_native_radio_toggle_only_changes_requested_kind_and_verifies_state(self):
        dll=Mock(); dll.RoInitialize.return_value=0
        def string(text,length,out): out._obj.value=100; return 0
        def factory(name,iid,out): out._obj.value=1; return 0
        dll.WindowsCreateString.side_effect=string; dll.RoGetActivationFactory.side_effect=factory
        changed=[]
        def call(pointer,index,types=(),args=()):
            identity=pointer.value
            if index==0: args[-1]._obj.value=90
            elif identity==90 and index==7: args[0]._obj.value=1
            elif identity==1 and index==9: args[0]._obj.value=2
            elif identity==2 and index==8: args[0]._obj.value=1
            elif identity==1 and index==6: args[0]._obj.value=3
            elif identity==3 and index==8: args[0]._obj.value=4
            elif identity==4 and index==7: args[0]._obj.value=2
            elif identity==4 and index==6: args[1]._obj.value=10+args[0]
            elif identity in (10,11) and index==11: args[0]._obj.value=1 if identity==10 else 3
            elif identity in (10,11) and index==6: changed.append(identity); args[1]._obj.value=5
            elif identity==5 and index==8: args[0]._obj.value=1
            elif identity==11 and index==9: args[0]._obj.value=2
            return 0
        with patch.object(system_status.ctypes,'WinDLL',return_value=dll), patch.object(system_status,'_call',side_effect=call):
            self.assertEqual(system_status.set_radio('bluetooth',False)['radio_state'],'off')
        self.assertEqual(changed,[11]); dll.RoUninitialize.assert_called_once()

    def test_radio_async_timeout_cancels_and_does_not_claim_success(self):
        def call(pointer,index,types=(),args=()):
            if index==0: args[-1]._obj.value=2
            elif index==7: args[0]._obj.value=0
            return 0
        import ctypes
        with patch.object(system_status,'_call',side_effect=call) as calls, patch.object(system_status.time,'monotonic',side_effect=[0,3]):
            with self.assertRaisesRegex(OSError,'вовремя'): system_status._wait_radio(ctypes.c_void_p(1))
            self.assertTrue(any(row.args[1]==9 for row in calls.call_args_list))

    def test_power_is_published_before_waiting_for_radio_enumeration(self):
        parts = []
        dll = Mock(); dll.RoInitialize.return_value = 0
        def radios(combase):
            self.assertEqual(parts, [dict(battery=42,has_battery=True,plugged=False,charging=False)])
            return dict(wifi='on',bluetooth='off')
        with patch.object(system_status,'read_power',return_value=dict(system_status.EMPTY_STATUS,battery=42)), patch.object(system_status.ctypes,'WinDLL',return_value=dll), patch.object(system_status,'_radios',side_effect=radios), patch.object(system_status,'_volume',return_value=dict(volume=25,muted=False)):
            state=system_status.read_status(parts.append)
        self.assertEqual(parts[-1]['volume'],25)
        self.assertEqual(state['bluetooth'],'off')
    def test_missing_audio_does_not_turn_radios_off_or_invent_a_battery(self):
        kernel, combase = Mock(), Mock()
        def power(pointer):
            pointer._obj.ac, pointer._obj.flag, pointer._obj.percent = 1, 128, 255
            return 1
        kernel.GetSystemPowerStatus.side_effect = power
        combase.RoInitialize.return_value = 0
        with patch.object(system_status.sys, 'platform', 'win32'), patch.object(system_status.ctypes, 'WinDLL',
                side_effect=[kernel, combase]), patch.object(system_status, '_radios', return_value=dict(wifi='on', bluetooth='off')), \
                patch.object(system_status, '_volume', side_effect=OSError('No audio endpoint')):
            state = system_status.read_status()
        self.assertEqual(state['wifi'], 'on')
        self.assertEqual(state['bluetooth'], 'off')
        self.assertEqual(state['volume'], -1)
        self.assertFalse(state['has_battery'])
        self.assertFalse(state['charging'])
        self.assertTrue(state['plugged'])
        combase.RoUninitialize.assert_called_once()

    def test_unsupported_platform_is_unknown_and_does_not_load_windows_dlls(self):
        with patch.object(system_status.sys, 'platform', 'linux'), patch.object(system_status.ctypes, 'WinDLL') as dll:
            self.assertEqual(system_status.read_status(), system_status.EMPTY_STATUS)
            dll.assert_not_called()

    def test_failed_com_initialization_keeps_battery_and_unknown_radio_state(self):
        kernel, combase = Mock(), Mock()
        def power(pointer):
            pointer._obj.ac, pointer._obj.flag, pointer._obj.percent = 0, 1, 42
            return 1
        kernel.GetSystemPowerStatus.side_effect = power
        combase.RoInitialize.return_value = -1
        with patch.object(system_status.ctypes, 'WinDLL', side_effect=[kernel, combase]), \
                patch.object(system_status, '_radios') as radios:
            state = system_status.read_status()
        self.assertEqual(state['battery'], 42)
        self.assertEqual(state['wifi'], 'unknown')
        radios.assert_not_called()
        combase.RoUninitialize.assert_not_called()
