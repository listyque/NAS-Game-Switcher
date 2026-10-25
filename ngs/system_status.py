"""Native Windows status snapshots and radio controls, without shell processes."""
from ngs.i18n import tr
import ctypes
import sys
import time
import uuid
from contextlib import contextmanager

from PySide6.QtCore import QObject, QThread, Signal, Slot


EMPTY_STATUS = dict(wifi="unknown", bluetooth="unknown", battery=-1, has_battery=True,
                    plugged=False, charging=False, volume=-1, muted=False)
SETTINGS = {"clock": "ms-settings:dateandtime", "wifi": "ms-settings:network-wifi",
            "bluetooth": "ms-settings:bluetooth", "battery": "ms-settings:powersleep",
            "volume": "ms-settings:sound", "typing": "ms-settings:typing"}


def _guid(value):
    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)


def _call(pointer, index, types=(), args=()):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])
    result = method(pointer, *args)
    if result < 0:
        raise OSError("Windows status HRESULT %08X" % (result & 0xffffffff))
    return result


@contextmanager
def _pointer():
    value = ctypes.c_void_p()
    try:
        yield value
    finally:
        if value.value:
            _call(value, 2)


def _radios(combase):
    p = ctypes.c_void_p
    combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.POINTER(p)]
    combase.WindowsDeleteString.argtypes = [p]
    combase.RoGetActivationFactory.argtypes = [p, p, ctypes.POINTER(p)]
    name = p()
    text = "Windows.Devices.Radios.Radio"
    if combase.WindowsCreateString(text, len(text), ctypes.byref(name)) < 0:
        raise OSError("WindowsCreateString failed")
    try:
        with _pointer() as factory, _pointer() as operation, _pointer() as info, _pointer() as radios:
            iid = _guid("5fb6a12e-67cb-46ae-aae9-65919f86eff4")
            if combase.RoGetActivationFactory(name, iid, ctypes.byref(factory)) < 0:
                raise OSError("Radio status unavailable")
            _call(factory, 6, (p,), (ctypes.byref(operation),))
            _call(operation, 0, (p, p), (_guid("00000036-0000-0000-c000-000000000046"), ctypes.byref(info)))
            status = ctypes.c_int()
            deadline = time.monotonic() + 1
            while True:
                _call(info, 7, (p,), (ctypes.byref(status),))
                if status.value != 0:
                    break
                if time.monotonic() >= deadline:
                    _call(info, 9)
                    raise OSError("Radio snapshot timed out")
                time.sleep(.02)
            if status.value != 1:
                raise OSError("Radio snapshot failed")
            _call(operation, 8, (p,), (ctypes.byref(radios),))
            count = ctypes.c_uint()
            _call(radios, 7, (p,), (ctypes.byref(count),))
            states = {1: [], 3: []}
            for index in range(min(count.value, 64)):
                with _pointer() as radio:
                    _call(radios, 6, (ctypes.c_uint, p), (index, ctypes.byref(radio)))
                    kind, state = ctypes.c_int(), ctypes.c_int()
                    _call(radio, 11, (p,), (ctypes.byref(kind),))
                    _call(radio, 9, (p,), (ctypes.byref(state),))
                    if kind.value in states:
                        states[kind.value].append(state.value)
            return {key: ("on" if 1 in states[kind] else "absent" if not states[kind]
                          else "unknown" if 0 in states[kind] else "off")
                    for key, kind in (("wifi", 1), ("bluetooth", 3))}
    finally:
        combase.WindowsDeleteString(name)


def _wait_radio(operation, timeout=2):
    p = ctypes.c_void_p
    with _pointer() as info:
        _call(operation, 0, (p, p), (_guid("00000036-0000-0000-c000-000000000046"), ctypes.byref(info)))
        status, deadline = ctypes.c_int(), time.monotonic() + timeout
        while True:
            _call(info, 7, (p,), (ctypes.byref(status),))
            if status.value == 1: return
            if status.value != 0: raise OSError(tr("Windows rejected changing the adapter state."))
            if time.monotonic() >= deadline:
                _call(info, 9)
                raise OSError(tr("The adapter timed out."))
            time.sleep(.01)


def set_radio(key, enabled):
    if key not in ('wifi', 'bluetooth') or type(enabled) is not bool:
        raise ValueError(tr("Invalid adapter state."))
    p = ctypes.c_void_p
    combase = ctypes.WinDLL('combase')
    combase.RoInitialize.argtypes = [ctypes.c_uint]
    if combase.RoInitialize(1) < 0: raise OSError(tr("Adapter controls are unavailable."))
    try:
        combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, p]
        combase.WindowsDeleteString.argtypes = [p]
        combase.RoGetActivationFactory.argtypes = [p, p, p]
        text, name = 'Windows.Devices.Radios.Radio', p()
        if combase.WindowsCreateString(text, len(text), ctypes.byref(name)) < 0: raise OSError('WindowsCreateString failed')
        try:
            with _pointer() as factory, _pointer() as access, _pointer() as operation, _pointer() as radios:
                code = combase.RoGetActivationFactory(name, _guid('5fb6a12e-67cb-46ae-aae9-65919f86eff4'), ctypes.byref(factory))
                if code < 0: raise OSError('Radio control unavailable')
                _call(factory, 9, (p,), (ctypes.byref(access),)); _wait_radio(access)
                permission = ctypes.c_int(); _call(access, 8, (p,), (ctypes.byref(permission),))
                if permission.value != 1: raise OSError(tr("Windows denied adapter control: %s") % permission.value)
                _call(factory, 6, (p,), (ctypes.byref(operation),)); _wait_radio(operation)
                _call(operation, 8, (p,), (ctypes.byref(radios),))
                count, matched = ctypes.c_uint(), False
                _call(radios, 7, (p,), (ctypes.byref(count),))
                for index in range(min(count.value, 64)):
                    with _pointer() as radio, _pointer() as change:
                        _call(radios, 6, (ctypes.c_uint, p), (index, ctypes.byref(radio)))
                        kind, state = ctypes.c_int(), ctypes.c_int()
                        _call(radio, 11, (p,), (ctypes.byref(kind),))
                        if kind.value != (1 if key == 'wifi' else 3): continue
                        matched = True
                        target = 1 if enabled else 2
                        _call(radio, 6, (ctypes.c_int, p), (target, ctypes.byref(change))); _wait_radio(change)
                        _call(change, 8, (p,), (ctypes.byref(permission),))
                        if permission.value != 1: raise OSError(tr("Windows rejected changing the adapter state."))
                        deadline = time.monotonic() + 1
                        while True:
                            _call(radio, 9, (p,), (ctypes.byref(state),))
                            if state.value == target: break
                            if time.monotonic() >= deadline: raise OSError(tr("Windows did not confirm the adapter state change."))
                            time.sleep(.01)
                if not matched: raise OSError(tr("No adapter detected."))
                return dict(items=[], radio_state='on' if enabled else 'off', message=tr("Adapter state changed"))
        finally: combase.WindowsDeleteString(name)
    finally: combase.RoUninitialize()


def _volume(value=None, mute=None):
    p = ctypes.c_void_p
    ole = ctypes.WinDLL("ole32")
    ole.CoCreateInstance.argtypes = [p, p, ctypes.c_uint, p, ctypes.POINTER(p)]
    with _pointer() as enumerator, _pointer() as device, _pointer() as endpoint:
        if ole.CoCreateInstance(_guid("bcde0395-e52f-467c-8e3d-c4579291692e"), None, 23,
                                _guid("a95664d2-9614-4f35-a746-de8db63617e6"), ctypes.byref(enumerator)) < 0:
            raise OSError("Audio endpoint unavailable")
        _call(enumerator, 4, (ctypes.c_int, ctypes.c_int, p), (0, 1, ctypes.byref(device)))
        _call(device, 3, (p, ctypes.c_uint, p, p),
              (_guid("5cdf2c82-841e-4546-9722-0cf74078229a"), 23, None, ctypes.byref(endpoint)))
        if value is not None:
            _call(endpoint, 7, (ctypes.c_float, p), (value / 100, None))
        if mute is not None:
            _call(endpoint, 14, (ctypes.c_int, p), (int(mute), None))
        level, muted = ctypes.c_float(), ctypes.c_int()
        _call(endpoint, 9, (p,), (ctypes.byref(level),))
        _call(endpoint, 15, (p,), (ctypes.byref(muted),))
        return dict(volume=max(0, min(100, round(level.value * 100))), muted=bool(muted.value))


def read_power():
    result = dict(EMPTY_STATUS)
    if sys.platform != "win32":
        return result
    class Power(ctypes.Structure):
        _fields_ = [(key, ctypes.c_ubyte) for key in ("ac", "flag", "percent", "reserved")] + [
            ("life", ctypes.c_uint32), ("full", ctypes.c_uint32)]
    power = Power()
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetSystemPowerStatus.argtypes = [ctypes.POINTER(Power)]
    if kernel.GetSystemPowerStatus(ctypes.byref(power)):
        result.update(battery=power.percent if power.percent <= 100 else -1,
                      has_battery=power.flag != 128, plugged=power.ac == 1,
                      charging=power.flag != 255 and bool(power.flag & 8))
    return result


def read_status(progress=None):
    result = read_power()
    if progress is not None:
        progress({key: result[key] for key in ('battery', 'has_battery', 'plugged', 'charging')})
    if sys.platform != "win32":
        return result
    combase = ctypes.WinDLL("combase")
    combase.RoInitialize.argtypes = [ctypes.c_uint]
    if combase.RoInitialize(1) < 0:
        return result
    try:
        for read in (lambda: _radios(combase), _volume):
            try:
                part = read()
                result.update(part)
                if progress is not None: progress(part)
            except OSError:
                pass  # Unknown is distinct from off, absent hardware, or zero volume.
    finally:
        combase.RoUninitialize()
    return result


class _StatusWorker(QThread):
    loaded = Signal(dict)

    def run(self):
        self.loaded.emit(read_status(self.loaded.emit))


class SystemStatus(QObject):
    changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.state = dict(EMPTY_STATUS)
        self.worker = None
        parent.destroyed.connect(self.close)

    @Slot()
    def refresh(self):
        if self.worker is not None:
            return
        self.worker = _StatusWorker(self)
        self.worker.loaded.connect(self.apply)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    @Slot(dict)
    def apply(self, state):
        state = dict(self.state, **state)
        if self.state != state:
            self.state = state
            self.changed.emit()

    @Slot()
    def finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()

    @Slot()
    def close(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            if not worker.wait(3000):
                worker.setParent(None)
                from ngs.players import _LINGERING_WORKERS
                _LINGERING_WORKERS.append(worker)
