import json
import ctypes
import io
import math
import unittest
from unittest.mock import Mock, patch

from ngs import system_controls as controls


class SystemControlTests(unittest.TestCase):
    def test_brightness_write_failure_keeps_fresh_actual_level_and_read_failure_invalidates_it(self):
        for read in (dict(brightness=80), OSError('read failed')):
            worker = controls.ControlWorker(('brightness', 'set', 90, ''), None)
            results = []; worker.loaded.connect(results.append)
            with patch.object(controls, 'operate', side_effect=[OSError('write failed'), read]) as operate:
                worker.run()
            self.assertEqual(results[-1]['brightness'], 80 if isinstance(read, dict) else -1)
            self.assertEqual(results[-1]['error'], 'write failed')
            self.assertEqual(operate.call_args.args, ('brightness', 'read'))
        worker = controls.ControlWorker(('brightness', 'read', None, ''), None)
        results = []; worker.loaded.connect(results.append)
        with patch.object(controls, 'operate', side_effect=OSError('no driver')): worker.run()
        self.assertEqual(results, [dict(error='no driver', brightness=-1)])

    def test_wifi_waits_for_own_scan_completion_and_unregisters_on_failure(self):
        dll = Mock()
        dll.WlanRegisterNotification.return_value = 0
        identity = (ctypes.c_ubyte * 16)(*range(16))
        def scan(*args):
            callback = dll.WlanRegisterNotification.call_args.args[3]
            wrong = controls.WifiNotification(source=8, code=7)
            callback(ctypes.pointer(wrong), None)
            own = controls.WifiNotification(source=8, code=7, guid=identity)
            callback(ctypes.pointer(own), None)
            return 0
        dll.WlanScan.side_effect = scan
        self.assertEqual(controls._wifi_scan(dll, None, ctypes.byref(identity)), '')
        self.assertEqual(dll.WlanRegisterNotification.call_args.args[1], 0)
        dll.WlanScan.side_effect = None; dll.WlanScan.return_value = 5
        with self.assertRaisesRegex(OSError, 'расположению'): controls._wifi_scan(dll, None, ctypes.byref(identity))
        self.assertEqual(dll.WlanRegisterNotification.call_args.args[1], 0)
        dll.WlanScan.return_value = 0
        with patch.object(controls.threading.Event, 'wait', return_value=False):
            self.assertIn('не подтвердил', controls._wifi_scan(dll, None, ctypes.byref(identity)))

    def test_bluetooth_stream_publishes_partial_results_and_keeps_errors(self):
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO('{"items":[]}\n{"items":[{"id":"one","name":"Device"}]}\n')
        process.wait.return_value = 0; process.poll.return_value = 0
        process.__enter__ = Mock(return_value=process); process.__exit__ = Mock(return_value=False)
        partial = []
        with patch.object(controls.subprocess, 'Popen', return_value=process):
            result = controls.powershell(dict(key='bluetooth', action='scan'), partial.append)
        self.assertEqual([len(item['items']) for item in partial], [0, 1])
        self.assertEqual(result['items'][0]['id'], 'one')
        self.assertIn('завершён', result['message'])
        process.kill.assert_not_called()
        process.stdin = io.StringIO(); process.stdout = io.StringIO('{"error":"Radio is off"}\n')
        process.poll.return_value = None
        with patch.object(controls.subprocess, 'Popen', return_value=process), self.assertRaisesRegex(OSError, 'Radio is off'):
            controls.powershell(dict(key='bluetooth', action='scan'), partial.append)
        process.kill.assert_called_once()
        process.stdin = io.StringIO(); process.stdout = io.StringIO('{"items":[]}\n')
        process.wait.side_effect = [controls.subprocess.TimeoutExpired('bluetooth', 20), 0]
        with patch.object(controls.subprocess, 'Popen', return_value=process), self.assertRaisesRegex(OSError, '20 секунд'):
            controls.powershell(dict(key='bluetooth', action='scan'), partial.append)

    def test_brightness_and_radios_use_native_backend_without_shell(self):
        with patch('ngs.brightness.brightness', return_value=dict(brightness=50)) as light, patch.object(controls, 'set_radio', return_value=dict(radio_state='on')) as radio, patch.object(controls, 'powershell') as shell:
            self.assertEqual(controls.operate('brightness','set',50)['brightness'],50)
            controls.operate('bluetooth','toggle',True)
            light.assert_called_once_with(50); radio.assert_called_once_with('bluetooth',True)
            shell.assert_not_called()

    def test_battery_never_waits_for_radios_or_audio(self):
        with patch.object(controls, 'read_power', return_value=dict(battery=65)), patch.object(controls, 'power_plans', return_value=dict(items=[])), patch('ngs.energy_saver.energy_saver', return_value={}), patch('ngs.system_status._radios', side_effect=AssertionError('Radio wait')), patch('ngs.system_status._volume', side_effect=AssertionError('Audio wait')):
            self.assertEqual(controls.operate('battery','read')['battery'],65)

    def test_panels_work_independently_and_slider_pending_keeps_latest_value(self):
        from PySide6.QtCore import QObject, Signal
        class Parent(QObject):
            systemSettingsFocus = Signal(bool)
            def refreshSystemStatus(self): pass
        class Worker(QObject):
            loaded = Signal(dict)
            finished = Signal()
            def __init__(self, request, parent): super().__init__(parent); self.request=request
            def start(self): pass
            def wait(self, *unused): pass
        parent = Parent()
        with patch.object(controls, 'ControlWorker', Worker):
            service = controls.SystemControls(parent)
            service.request('bluetooth','scan')
            bluetooth = service.workers['bluetooth']
            service.request('brightness','read')
            light = service.workers['brightness']
            for value in (20,30,90): service.request('brightness','set',value)
            self.assertEqual(service.pending['brightness'], [('brightness','set',90,'')])
            light.loaded.emit(dict(brightness=50,error='')); light.finished.emit()
            final = service.workers['brightness']
            self.assertEqual(final.request, ('brightness','set',90,''))
            final.loaded.emit(dict(brightness=90,error='')); final.finished.emit()
            self.assertEqual(service.panel['brightness'],90)
            self.assertFalse(service.panel['busy'])
            bluetooth.loaded.emit(dict(items=[dict(id='test')],error='')); bluetooth.finished.emit()
            self.assertEqual(service.panel['key'],'brightness')
            self.assertEqual(service.panel['brightness'],90)
            service.request('bluetooth','read')
            self.assertEqual(service.panel['items'],[dict(id='test')])
            service.close()
    def test_invalid_mutations_never_reach_hardware_or_shell(self):
        with patch.object(controls, 'powershell') as shell, patch.object(controls.ctypes, 'WinDLL') as dll:
            for args in [('brightness', 'set', -1), ('brightness', 'set', 101), ('brightness', 'set', True),
                         ('brightness', 'set', float('nan')), ('volume', 'set', float('inf')),
                         ('volume', 'mute', 1), ('wifi', 'toggle', 'on'), ('bluetooth', 'pair', 'x' * 2049),
                         ('volume', 'execute', 'bad')]:
                with self.subTest(args=args), self.assertRaises(ValueError): controls.operate(*args)
            shell.assert_not_called(); dll.assert_not_called()

    def test_command_arguments_are_data_even_with_quotes_or_shell_syntax(self):
        request = dict(key='bluetooth', action='pair', value='"; $(Get-Content secret); `evil')
        with patch.object(controls.subprocess, 'run', return_value=Mock(stdout='{"items":[]}')) as run:
            self.assertEqual(controls.powershell(request), dict(items=[]))
        self.assertEqual(json.loads(run.call_args.kwargs['input']), request)
        self.assertNotIn(request['value'], ' '.join(run.call_args.args[0]))
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertEqual(run.call_args.kwargs['creationflags'], controls.subprocess.CREATE_NO_WINDOW)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 55)

    def test_driver_errors_are_visible_and_not_reported_as_success(self):
        with patch.object(controls.subprocess, 'run', return_value=Mock(stdout='{"error":"No brightness driver"}')):
            with self.assertRaisesRegex(OSError, 'No brightness'): controls.powershell(dict(key='brightness', action='read'))
        with patch.object(controls.subprocess, 'run', side_effect=controls.subprocess.TimeoutExpired('powershell', 35)):
            with self.assertRaises(OSError): controls.powershell(dict(key='wifi', action='read'))

    def test_volume_and_mute_call_endpoint_once_and_release_com_on_failure(self):
        dll = Mock(); dll.RoInitialize.return_value = 0
        with patch.object(controls.ctypes, 'WinDLL', return_value=dll), patch.object(controls, '_volume', side_effect=OSError('device lost')) as volume:
            with self.assertRaisesRegex(OSError, 'device lost'): controls.operate('volume', 'set', 0)
            volume.assert_called_once_with(value=0, mute=None)
            dll.RoUninitialize.assert_called_once()

    def test_wifi_adapter_and_network_abi_sizes(self):
        import ctypes
        self.assertEqual(ctypes.sizeof(controls.SSID), 36)
        self.assertEqual(ctypes.sizeof(controls.Interface), 532)
        self.assertEqual(ctypes.sizeof(controls.Network), 628)

