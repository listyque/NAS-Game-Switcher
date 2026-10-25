"""On-demand Windows controls. Hardware work never runs on the QML thread."""
from ngs.i18n import tr
import base64
import ctypes
import json
import os
import queue
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from xml.sax.saxutils import escape

from PySide6.QtCore import QObject, Property, QThread, Signal, Slot
from PySide6.QtQml import QJSValue
from ngs.system_status import _guid, _volume, read_power, set_radio


POWERSHELL = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
function Await($operation, $type, $timeout=12000) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
    $task = $method.MakeGenericMethod($type).Invoke($null, @($operation))
    if (!$task.Wait($timeout)) { throw 'Windows did not respond in time' }
    return $task.Result
}
function DevicesJson($devices) {
    return (@{items=@($devices | Where-Object Name | Select-Object -First 64 | ForEach-Object { @{id=$_.Id; name=$_.Name; paired=$_.Pairing.IsPaired; connected=[bool]$_.Properties['System.Devices.Aep.IsConnected']} })} | ConvertTo-Json -Compress -Depth 4)
}
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [Windows.Devices.Enumeration.DeviceInformation,Windows.Devices.Enumeration,ContentType=WindowsRuntime] | Out-Null
    $query = 'System.Devices.Aep.ProtocolId:="{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}" OR System.Devices.Aep.ProtocolId:="{bb7bb05e-5972-42b5-94fc-76eaa7084d49}"'
    if ($request.action -eq 'read') {
        $devices = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($query, [string[]]@('System.Devices.Aep.IsConnected'), [Windows.Devices.Enumeration.DeviceInformationKind]::AssociationEndpoint)) ([Windows.Devices.Enumeration.DeviceInformationCollection])
    } elseif ($request.action -eq 'scan') {
        $source = @'
using System;
using System.Collections.Generic;
using System.Threading;
using Windows.Devices.Enumeration;
using Windows.Foundation;
public static class NgsBluetoothDiscovery {
    static readonly Dictionary<string, DeviceInformation> devices = new Dictionary<string, DeviceInformation>();
    static DeviceWatcher watcher;
    public static void Start(string query) {
        watcher = DeviceInformation.CreateWatcher(query,
            new [] { "System.Devices.Aep.IsConnected" }, DeviceInformationKind.AssociationEndpoint);
        TypedEventHandler<DeviceWatcher, DeviceInformation> added = (sender, value) => { lock(devices) devices[value.Id] = value; };
        TypedEventHandler<DeviceWatcher, DeviceInformationUpdate> updated = (sender, value) => { lock(devices) { DeviceInformation item; if(devices.TryGetValue(value.Id, out item)) item.Update(value); } };
        TypedEventHandler<DeviceWatcher, DeviceInformationUpdate> removed = (sender, value) => { lock(devices) devices.Remove(value.Id); };
        watcher.Added += added; watcher.Updated += updated; watcher.Removed += removed;
        watcher.Start();
    }
    public static DeviceInformation[] Snapshot() {
        lock(devices) { var result = new DeviceInformation[devices.Count]; devices.Values.CopyTo(result, 0); return result; }
    }
    public static void Stop() {
        if(watcher != null && (watcher.Status == DeviceWatcherStatus.Started || watcher.Status == DeviceWatcherStatus.EnumerationCompleted)) watcher.Stop();
    }
}
'@
        $framework = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()
        $compiler = New-Object Microsoft.CSharp.CSharpCodeProvider
        $parameters = New-Object System.CodeDom.Compiler.CompilerParameters
        $parameters.GenerateInMemory = $true
        $parameters.ReferencedAssemblies.AddRange([string[]]@("${framework}System.Runtime.WindowsRuntime.dll", "${framework}System.Runtime.InteropServices.WindowsRuntime.dll", "${framework}System.Runtime.dll", 'System.dll', 'System.Core.dll', "$env:SystemRoot\System32\WinMetadata\Windows.Devices.winmd", "$env:SystemRoot\System32\WinMetadata\Windows.Foundation.winmd"))
        try { $compiled = $compiler.CompileAssemblyFromSource($parameters, $source) } finally { $compiler.Dispose() }
        if ($compiled.Errors.HasErrors) { throw (($compiled.Errors | ForEach-Object ToString) -join '; ') }
        $compiled.CompiledAssembly | Out-Null
        try {
            [NgsBluetoothDiscovery]::Start($query)
            $timer = [Diagnostics.Stopwatch]::StartNew()
            $previous = ''
            while ($timer.ElapsedMilliseconds -lt 8000) {
                $devices = [NgsBluetoothDiscovery]::Snapshot()
                $json = DevicesJson $devices
                if ($json -ne $previous) { [Console]::WriteLine($json); $previous = $json }
                Start-Sleep -Milliseconds 250
            }
            $devices = [NgsBluetoothDiscovery]::Snapshot()
        } finally { [NgsBluetoothDiscovery]::Stop() }
    }
    if ($request.action -eq 'pair' -or $request.action -eq 'unpair') {
        $device = Await ([Windows.Devices.Enumeration.DeviceInformation]::CreateFromIdAsync([string]$request.value, [string[]]@('System.Devices.Aep.IsConnected'), [Windows.Devices.Enumeration.DeviceInformationKind]::AssociationEndpoint)) ([Windows.Devices.Enumeration.DeviceInformation])
        if (!$device) { throw 'Device is no longer available' }
        if ($request.action -eq 'pair') {
            $result = Await ($device.Pairing.PairAsync()) ([Windows.Devices.Enumeration.DevicePairingResult]) 30000
            if ($result.Status.ToString() -notin @('Paired', 'AlreadyPaired')) { throw ('Pairing: ' + $result.Status) }
        } else {
            $result = Await ($device.Pairing.UnpairAsync()) ([Windows.Devices.Enumeration.DeviceUnpairingResult])
            if ($result.Status.ToString() -notin @('Unpaired', 'AlreadyUnpaired')) { throw ('Unpairing: ' + $result.Status) }
        }
        $devices = @($device)
    }
    [Console]::WriteLine((DevicesJson $devices))
} catch { @{error=$_.Exception.Message} | ConvertTo-Json -Compress }
'''


def powershell(request, progress=None):
    command = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    encoded = base64.b64encode(POWERSHELL.encode('utf-16le')).decode('ascii')
    args = [str(command), '-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-EncodedCommand', encoded]
    if request.get('action') == 'scan': return _discovery(args, request, progress)
    try:
        result = subprocess.run(args, input=json.dumps(request), encoding='utf-8', capture_output=True,
            timeout=55 if request.get('action') == 'pair' else 35, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise OSError(tr("Windows did not respond to the device control request.")) from error
    return _device_result(result.stdout)


def _device_result(line):
    value = json.loads(line.lstrip('\ufeff'))
    if not isinstance(value, dict): raise OSError(tr("Invalid Windows response."))
    if value.get('error'): raise OSError(str(value['error'])[:400])
    if not isinstance(value.get('items'), list) or len(value['items']) > 64:
        raise OSError(tr("Invalid Windows device list."))
    return value


def _discovery(args, request, progress):
    # One reader drains stdout; the worker keeps a deadline even if Windows stops responding.
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW) as process:
        messages = queue.Queue(maxsize=256)
        def read():
            try:
                for line in process.stdout:
                    if len(line) > 262144: break
                    try: messages.put_nowait(line)
                    except queue.Full: break
            finally:
                try: messages.put(None, timeout=1)
                except queue.Full: pass
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            process.stdin.write(json.dumps(request)); process.stdin.close()
            deadline, result = time.monotonic() + 20, None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise OSError(tr("Bluetooth: Windows did not finish scanning within 20 seconds."))
                try: line = messages.get(timeout=remaining)
                except queue.Empty: raise OSError(tr("Bluetooth: Windows did not finish scanning within 20 seconds."))
                if line is None: break
                result = _device_result(line)
                if progress: progress(result)
            if process.wait(timeout=max(.01, deadline-time.monotonic())) or result is None:
                raise OSError(tr("Bluetooth: scan ended without a Windows response."))
            return dict(result, message=tr("Bluetooth scan complete · found: ") + str(len(result['items'])))
        except subprocess.TimeoutExpired as error:
            raise OSError(tr("Bluetooth: Windows did not finish scanning within 20 seconds.")) from error
        finally:
            if process.poll() is None: process.kill()
            process.wait(); reader.join(timeout=2)


class SSID(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32), ('bytes', ctypes.c_ubyte * 32)]


class Interface(ctypes.Structure):
    _fields_ = [('guid', ctypes.c_ubyte * 16), ('description', ctypes.c_wchar * 256), ('state', ctypes.c_int)]


class Network(ctypes.Structure):
    _fields_ = [('profile', ctypes.c_wchar * 256), ('ssid', SSID), ('bss', ctypes.c_int),
        ('bss_count', ctypes.c_uint32), ('connectable', ctypes.c_int), ('reason', ctypes.c_uint32),
        ('phy_count', ctypes.c_uint32), ('phys', ctypes.c_int * 8), ('more', ctypes.c_int),
        ('quality', ctypes.c_uint32), ('security', ctypes.c_int), ('auth', ctypes.c_int),
        ('cipher', ctypes.c_int), ('flags', ctypes.c_uint32), ('reserved', ctypes.c_uint32)]


class WifiNotification(ctypes.Structure):
    _fields_ = [('source', ctypes.c_uint32), ('code', ctypes.c_uint32), ('guid', ctypes.c_ubyte * 16),
                ('size', ctypes.c_uint32), ('data', ctypes.c_void_p)]


def _wifi_scan(dll, handle, guid):
    done, result = threading.Event(), []
    identity = ctypes.string_at(guid, 16)
    callback_type = ctypes.WINFUNCTYPE(None, ctypes.POINTER(WifiNotification), ctypes.c_void_p)
    @callback_type
    def notified(pointer, context):
        notice = pointer.contents
        if notice.source == 8 and bytes(notice.guid) == identity and notice.code in (7, 8):
            result.append(notice.code); done.set()
    dll.WlanRegisterNotification.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                           callback_type, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    code = dll.WlanRegisterNotification(handle, 8, False, notified, None, None, None)
    if code: raise OSError('Wi-Fi: ' + ctypes.FormatError(code).strip())
    try:
        code = dll.WlanScan(handle, guid, None, None, None)
        if code == 5:
            raise OSError(tr("Windows denied Wi-Fi scanning. Allow location access for desktop apps in Windows Settings → Privacy → Location."))
        if code: raise OSError('Wi-Fi: ' + ctypes.FormatError(code).strip())
        if not done.wait(4): return tr("The Wi-Fi driver did not confirm scan completion. Showing available results; try refreshing.")
        if result[0] == 8: raise OSError(tr("The Wi-Fi driver reported a network scan error."))
        return ''
    finally:
        dll.WlanRegisterNotification(handle, 0, False, callback_type(), None, None, None)


def wifi(action='read', target=None, password='', progress=None):
    p = ctypes.c_void_p
    dll = ctypes.WinDLL('wlanapi')
    dll.WlanOpenHandle.argtypes = [ctypes.c_uint32, p, p, p]
    dll.WlanEnumInterfaces.argtypes = [p, p, p]
    dll.WlanGetAvailableNetworkList.argtypes = [p, p, ctypes.c_uint32, p, p]
    dll.WlanScan.argtypes = [p, p, p, p, p]
    dll.WlanConnect.argtypes = [p, p, p, p]
    dll.WlanDisconnect.argtypes = [p, p, p]
    dll.WlanCloseHandle.argtypes = [p, p]
    dll.WlanFreeMemory.argtypes = [p]
    def checked(code):
        if code == 5:
            raise OSError(tr("Windows denied Wi-Fi access. Check desktop app permissions in Windows Settings → Privacy → Location."))
        if code: raise OSError('Wi-Fi: ' + ctypes.FormatError(code).strip())
    handle, version, interfaces = p(), ctypes.c_uint32(), p()
    checked(dll.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)))
    try:
        checked(dll.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)))
        count = ctypes.c_uint32.from_address(interfaces.value).value
        items = []
        message = ''
        for i in range(min(count, 16)):
            adapter = Interface.from_address(interfaces.value + 8 + i * ctypes.sizeof(Interface))
            guid = ctypes.byref(adapter.guid)
            if action == 'scan':
                message = _wifi_scan(dll, handle, guid) or message
            networks = p()
            checked(dll.WlanGetAvailableNetworkList(handle, guid, 0, None, ctypes.byref(networks)))
            try:
                size = ctypes.c_uint32.from_address(networks.value).value
                for n in range(min(size, 256)):
                    net = Network.from_address(networks.value + 8 + n * ctypes.sizeof(Network))
                    raw = bytes(net.ssid.bytes)[:min(net.ssid.length, 32)]
                    if not raw: continue
                    identity = bytes(adapter.guid).hex() + ':' + raw.hex()
                    item = dict(id=identity, name=raw.decode('utf8', 'replace'), quality=net.quality,
                        connected=bool(net.flags & 1), saved=bool(net.profile), secure=bool(net.security))
                    if identity == target and action in ('connect', 'disconnect'):
                        if action == 'disconnect': checked(dll.WlanDisconnect(handle, guid, None))
                        else:
                            profile, mode = net.profile, 0
                            if not profile:
                                if net.security and (net.auth not in (4, 7) or net.cipher != 4):
                                    raise OSError(tr("This network requires an enterprise or WPA3 Windows profile. Saved profiles are supported."))
                                if net.security and not (8 <= len(password) <= 63):
                                    raise ValueError(tr("Wi-Fi password: 8 to 63 characters."))
                                security = ('<authentication>WPA2PSK</authentication><encryption>AES</encryption>' if net.auth == 7 else '<authentication>WPAPSK</authentication><encryption>AES</encryption>') if net.security else '<authentication>open</authentication><encryption>none</encryption>'
                                key = '<sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>' + escape(password) + '</keyMaterial></sharedKey>' if net.security else ''
                                profile = '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1"><name>NGS-' + raw.hex() + '</name><SSIDConfig><SSID><hex>' + raw.hex() + '</hex></SSID></SSIDConfig><connectionType>ESS</connectionType><connectionMode>manual</connectionMode><MSM><security><authEncryption>' + security + '<useOneX>false</useOneX></authEncryption>' + key + '</security></MSM></WLANProfile>'
                                mode = 1  # Temporary XML profile: credentials stay in Windows, never in NGS settings/logs.
                            class Parameters(ctypes.Structure):
                                _fields_ = [('mode', ctypes.c_int), ('profile', ctypes.c_wchar_p), ('ssid', p), ('bssid', p), ('bss', ctypes.c_int), ('flags', ctypes.c_uint32)]
                            args = Parameters(mode, profile, None, None, 1, 0)
                            checked(dll.WlanConnect(handle, guid, ctypes.byref(args), None))
                        return dict(items=[], message=tr("Request sent. Refresh the list to check the connection."))
                    items.append(item)
            finally: dll.WlanFreeMemory(networks)
            if progress: progress(dict(items=sorted(items, key=lambda v: (not v['connected'], -v['quality']))))
        if target: raise OSError(tr("The network is no longer available. Refresh the list."))
        return dict(items=sorted(items, key=lambda v: (not v['connected'], -v['quality'])),
                    message=tr("No Wi-Fi adapter detected.") if not count else message or (tr("Wi-Fi scan complete · networks: ") + str(len(items)) if action == 'scan' else ''))
    finally:
        if interfaces.value: dll.WlanFreeMemory(interfaces)
        dll.WlanCloseHandle(handle, None)


def power_plans(selected=None):
    p = ctypes.c_void_p
    dll, kernel = ctypes.WinDLL('powrprof'), ctypes.WinDLL('kernel32')
    dll.PowerGetActiveScheme.argtypes = [p, p]
    dll.PowerEnumerate.argtypes = [p, p, p, ctypes.c_uint32, ctypes.c_uint32, p, p]
    dll.PowerReadFriendlyName.argtypes = [p, p, p, p, p, p]
    dll.PowerSetActiveScheme.argtypes = [p, p]
    kernel.LocalFree.argtypes = [p]; kernel.LocalFree.restype = p
    active = p()
    code = dll.PowerGetActiveScheme(None, ctypes.byref(active))
    if code: raise OSError(ctypes.FormatError(code))
    try: current = ctypes.string_at(active, 16)
    finally: kernel.LocalFree(active)
    items = []
    for index in range(64):
        guid, size = (ctypes.c_ubyte * 16)(), ctypes.c_uint32(16)
        code = dll.PowerEnumerate(None, None, None, 16, index, guid, ctypes.byref(size))
        if code == 259: break
        if code: raise OSError(ctypes.FormatError(code))
        identity = str(uuid.UUID(bytes_le=bytes(guid)))
        size = ctypes.c_uint32()
        code = dll.PowerReadFriendlyName(None, guid, None, None, None, ctypes.byref(size))
        if code not in (0, 234) or size.value > 4096: raise OSError(tr("Could not read the power plan."))
        name = ctypes.create_unicode_buffer(max(1, size.value // 2))
        code = dll.PowerReadFriendlyName(None, guid, None, None, name, ctypes.byref(size))
        if code: raise OSError(ctypes.FormatError(code))
        items.append(dict(id=identity, name=name.value, connected=bytes(guid) == current))
    if selected is not None:
        if selected not in {item['id'] for item in items}: raise ValueError(tr("This power plan is no longer available."))
        code = dll.PowerSetActiveScheme(None, _guid(selected))
        if code: raise OSError(ctypes.FormatError(code))
        for item in items: item['connected'] = item['id'] == selected
    return dict(items=items)


def operate(key, action, value=None, password='', progress=None):
    if sys.platform != 'win32': raise OSError(tr("Device controls are available on Windows."))
    if key == 'wifi' and action in ('read', 'scan', 'connect', 'disconnect'):
        return wifi(action, value, password, progress=progress) if progress else wifi(action, value, password)
    if key in ('wifi', 'bluetooth') and action == 'toggle' and type(value) is bool:
        return set_radio(key, value)
    if key == 'bluetooth' and action in ('read', 'scan', 'pair', 'unpair'):
        if action not in ('read', 'scan') and (not isinstance(value, str) or len(value) > 2048): raise ValueError(tr("Invalid device."))
        request = dict(key=key, action=action, value=value)
        return powershell(request, progress=progress) if progress else powershell(request)
    if key == 'brightness' and action in ('read', 'set'):
        if action == 'set' and (type(value) not in (int, float) or not 0 <= value <= 100): raise ValueError(tr("Brightness: 0–100%."))
        from ngs.brightness import brightness
        return brightness(value if action == 'set' else None)
    if key == 'volume' and (action in ('read', 'output', 'session_set', 'session_mute') or action == 'set' and type(value) in (int, float) and 0 <= value <= 100 or action == 'mute' and type(value) is bool):
        combase = ctypes.WinDLL('combase')
        if combase.RoInitialize(1) < 0: raise OSError(tr("The audio service is unavailable."))
        try:
            from ngs.audio import mixer
            if action in ('set', 'mute'):
                _volume(value=value if action == 'set' else None, mute=value if action == 'mute' else None)
                return mixer()
            return mixer(action, value)
        finally: combase.RoUninitialize()
    if key == 'battery' and action in ('read', 'plan', 'energy_saver'):
        from ngs.energy_saver import energy_saver
        if action == 'energy_saver' and type(value) is not bool: raise ValueError(tr("Energy saver: enable or disable."))
        return dict(read_power(), **power_plans(value if action == 'plan' else None),
                    **energy_saver(value if action == 'energy_saver' else None))
    if key == 'clock' and action == 'read': return read_power()
    raise ValueError(tr("Invalid control command."))


class ControlWorker(QThread):
    loaded = Signal(dict)
    def __init__(self, request, parent):
        super().__init__(parent); self.request = request
    def run(self):
        from ngs.diagnostics import event
        key, action, value, password = self.request
        started = time.monotonic()
        event('system.request', key=key, action=action, value=value)
        try:
            if key in ('wifi', 'bluetooth') and action == 'scan':
                if key == 'wifi': self.loaded.emit(operate(key, 'read'))
                result = operate(*self.request, progress=self.loaded.emit)
            else: result = operate(*self.request)
            event('system.done', key=key, action=action, devices=len(result.get('items', [])), elapsed_ms=round((time.monotonic()-started)*1000, 1))
            self.loaded.emit(dict(result, error=''))
        except (OSError, ValueError) as error:
            event('system.error', key=key, action=action, error=str(error))
            result = dict(error=str(error))
            if key == 'brightness':
                result['brightness'] = -1
                if action == 'set':
                    try: result.update(operate(key, 'read'))
                    except (OSError, ValueError): pass
            self.loaded.emit(result)


class SystemControls(QObject):
    changed = Signal()
    def __init__(self, parent):
        super().__init__(parent); self.workers = {}; self.pending = {}; self.states = {}; self.closing = False
        self.empty = dict(key='', busy=False, error='', items=[], outputs=[], sessions=[], endpoint='', brightness=-1, volume=-1, muted=False, radio_state='', energy_saver=None, energy_saver_available=False, energy_saver_message='')
        self.state = dict(self.empty)
        parent.destroyed.connect(self.close)
    @Property('QVariantMap', notify=changed)
    def panel(self): return self.state
    @Slot(str, str, 'QVariant', str)
    def request(self, key, action='read', value=None, password=''):
        if self.closing or key not in ('wifi', 'bluetooth', 'brightness', 'battery', 'volume', 'clock'): return
        if not isinstance(password, str) or len(password) > 63: return
        if isinstance(value, QJSValue): value = value.toVariant()
        if self.state['key'] != key:
            self.state = self.states.setdefault(key, dict(self.empty, key=key))
            self.changed.emit()
        request = (key, action, value, password)
        if key in self.workers:
            queue = self.pending.setdefault(key, [])
            if action in ('read', 'scan') and (self.workers[key].request[1] == action or any(item[1] == action for item in queue)): return
            # Keep the newest slider value, but never drop a different device/session action.
            same_slider = queue and action in ('set', 'session_set') and queue[-1][1] == action
            if same_slider and action == 'session_set':
                same_slider = all(isinstance(v, dict) for v in (value, queue[-1][2])) and all(value.get(k) == queue[-1][2].get(k) for k in ('endpoint', 'session'))
            if same_slider: queue[-1] = request
            else: queue.append(request)
            return
        self.start((key, action, value, password))
    def start(self, request):
        key, action, value, password = request
        state = self.states.setdefault(key, dict(self.empty, key=key))
        state.update(busy=True, action=action, error='', message='')
        if key == 'bluetooth' and action == 'pair':
            # Windows requires its own consent dialog even for custom Bluetooth pairing.
            self.parent().systemSettingsFocus.emit(True)
        if key == self.state['key']: self.changed.emit()
        worker = ControlWorker(request, self)
        self.workers[key] = worker
        def on_loaded(result): self.apply(key, result)
        def on_finished(): self.finished(key)
        worker._ngs_callbacks = (on_loaded, on_finished)
        worker.loaded.connect(on_loaded)
        worker.finished.connect(on_finished)
        worker.start()
    def apply(self, key, result):
        self.states[key].update(result)
        if key == self.state['key']: self.changed.emit()
    def finished(self, key):
        worker = self.workers.pop(key)
        worker.deleteLater()
        if key == 'bluetooth' and worker.request[1] == 'pair':
            self.parent().systemSettingsFocus.emit(False)
        queue = self.pending.get(key, [])
        if queue and not self.closing:
            request = queue.pop(0)
            self.start(request)
            return
        self.pending.pop(key, None)
        self.states[key].update(busy=False)
        if key == self.state['key']: self.changed.emit()
        if key != 'brightness': self.parent().refreshSystemStatus()
    @Slot()
    def close(self):
        self.closing = True; self.pending.clear()
        for worker in list(self.workers.values()):
            if not worker.wait(1500):
                worker.setParent(None)
                from ngs.players import _LINGERING_WORKERS
                _LINGERING_WORKERS.append(worker)

