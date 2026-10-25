"""Built-in display brightness through local WMI, without launching a shell."""
from ngs.i18n import tr
import ctypes
import time
from contextlib import contextmanager

from ngs.system_status import _call, _guid, _pointer

P = ctypes.c_void_p


class Variant(ctypes.Structure):
    _fields_ = [('vt', ctypes.c_ushort), ('reserved', ctypes.c_ushort * 3), ('data', ctypes.c_byte * 16)]


@contextmanager
def _bstr(text):
    ole = ctypes.WinDLL('oleaut32')
    ole.SysAllocString.argtypes = [ctypes.c_wchar_p]; ole.SysAllocString.restype = P
    ole.SysFreeString.argtypes = [P]
    value = ole.SysAllocString(text)
    if not value: raise MemoryError('BSTR allocation failed')
    try: yield value
    finally: ole.SysFreeString(value)


def _array(array):
    ole = ctypes.WinDLL('oleaut32')
    for name in ('SafeArrayGetLBound', 'SafeArrayGetUBound'):
        getattr(ole, name).argtypes = [P, ctypes.c_uint, P]
    ole.SafeArrayGetDim.argtypes = [P]; ole.SafeArrayGetDim.restype = ctypes.c_uint
    ole.SafeArrayGetVartype.argtypes = [P, P]
    ole.SafeArrayAccessData.argtypes = [P, P]; ole.SafeArrayUnaccessData.argtypes = [P]
    lower, upper, kind, data = ctypes.c_long(), ctypes.c_long(), ctypes.c_ushort(), P()
    if not array.value or ole.SafeArrayGetDim(array) != 1: raise OSError(tr("Invalid WMI brightness levels."))
    for function, args in ((ole.SafeArrayGetLBound, (array, 1, ctypes.byref(lower))),
                           (ole.SafeArrayGetUBound, (array, 1, ctypes.byref(upper))),
                           (ole.SafeArrayGetVartype, (array, ctypes.byref(kind)))):
        if function(*args) < 0: raise OSError(tr("Could not read WMI brightness levels."))
    count = upper.value - lower.value + 1
    element = {2: ctypes.c_short, 3: ctypes.c_long, 17: ctypes.c_ubyte, 19: ctypes.c_uint}.get(kind.value)
    if element is None or not 0 <= count <= 256: raise OSError(tr("Invalid WMI brightness levels."))
    if ole.SafeArrayAccessData(array, ctypes.byref(data)) < 0: raise OSError(tr("Could not open WMI brightness levels."))
    try: return list(ctypes.cast(data, ctypes.POINTER(element * count)).contents)
    finally: ole.SafeArrayUnaccessData(array)


def _get(item, name):
    value = Variant()
    ole = ctypes.WinDLL('oleaut32'); ole.VariantClear.argtypes = [P]
    try:
        _call(item, 4, (ctypes.c_wchar_p, ctypes.c_long, P, P, P), (name, 0, ctypes.byref(value), None, None))
        address = ctypes.cast(ctypes.byref(value, 8), ctypes.POINTER(P)).contents
        if value.vt in (0, 1): return None
        if value.vt & 0x2000: return _array(address)
        if value.vt == 8: return ctypes.wstring_at(address) if address.value else ''
        scalar = {2: ctypes.c_short, 3: ctypes.c_long, 17: ctypes.c_ubyte, 19: ctypes.c_uint}.get(value.vt)
        if scalar is not None: return ctypes.cast(ctypes.byref(value, 8), ctypes.POINTER(scalar)).contents.value
        raise OSError(tr("Unsupported WMI brightness property type: ") + name)
    finally: ole.VariantClear(ctypes.byref(value))


@contextmanager
def _connection():
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [P, ctypes.c_uint]
    ole.CoCreateInstance.argtypes = [P, P, ctypes.c_uint, P, P]
    ole.CoSetProxyBlanket.argtypes = [P, ctypes.c_uint, ctypes.c_uint, P, ctypes.c_uint, ctypes.c_uint, P, ctypes.c_uint]
    if ole.CoInitializeEx(None, 0) < 0: raise OSError(tr("Brightness COM interface unavailable."))
    try:
        with _pointer() as locator, _pointer() as service, _bstr(r'ROOT\WMI') as namespace:
            code = ole.CoCreateInstance(_guid('4590f811-1d3a-11d0-891f-00aa004b2e24'), None, 1,
                                        _guid('dc12a687-737f-11cf-884d-00aa004b2e24'), ctypes.byref(locator))
            if code < 0: raise OSError(tr("WMI brightness unavailable: %08X") % (code & 0xffffffff))
            _call(locator, 3, (P, P, P, P, ctypes.c_long, P, P, P),
                  (namespace, None, None, None, 0x80, None, None, ctypes.byref(service)))
            if ole.CoSetProxyBlanket(service, 10, 0, None, 3, 3, None, 0) < 0:
                raise OSError(tr("Local WMI control unavailable."))
            yield service
    finally: ole.CoUninitialize()


def _query(service, query):
    with _bstr('WQL') as language, _bstr(query) as text, _pointer() as entries:
        _call(service, 20, (P, P, ctypes.c_long, P, P), (language, text, 0x30, None, ctypes.byref(entries)))
        deadline = time.monotonic() + 5
        received = 0
        while received < 64:
            with _pointer() as item:
                count = ctypes.c_uint()
                code = _call(entries, 4, (ctypes.c_long, ctypes.c_uint, P, P), (1000, 1, ctypes.byref(item), ctypes.byref(count)))
                if count.value:
                    received += 1
                    yield item
                elif code != 0x40004: return
                if code == 0x40004 and time.monotonic() >= deadline:
                    raise OSError(tr("The brightness driver timed out."))
        raise OSError(tr("Too many WMI monitors."))


def _monitor(service, identity=None):
    for item in _query(service, 'SELECT * FROM WmiMonitorBrightness WHERE Active=TRUE'):
        name = _get(item, 'InstanceName')
        if identity is None or name == identity:
            current = _get(item, 'CurrentBrightness')
            if type(current) is not int or not 0 <= current <= 100: raise OSError(tr("The driver returned an invalid brightness value."))
            return dict(identity=name, brightness=current, levels=_get(item, 'Level') or [])
    raise OSError(tr("The built-in display driver does not provide brightness control."))


def _set(service, identity, target):
    for item in _query(service, 'SELECT * FROM WmiMonitorBrightnessMethods'):
        if _get(item, 'InstanceName') != identity: continue
        with _pointer() as klass, _pointer() as definition, _pointer() as parameters, _pointer() as completion, _pointer() as output:
            # GetMethod is legal only on a class definition, never a monitor instance.
            with _bstr('WmiMonitorBrightnessMethods') as name:
                _call(service, 6, (P, ctypes.c_long, P, P, P), (name, 0, None, ctypes.byref(klass), None))
            _call(klass, 19, (ctypes.c_wchar_p, ctypes.c_long, P, P), ('WmiSetBrightness', 0, ctypes.byref(definition), None))
            _call(definition, 15, (ctypes.c_long, P), (0, ctypes.byref(parameters)))
            for name, number, kind in (('Timeout', 0, 3), ('Brightness', target, 17)):
                value = Variant(vt=kind)
                ctypes.cast(ctypes.byref(value, 8), ctypes.POINTER(ctypes.c_long if kind == 3 else ctypes.c_ubyte)).contents.value = number
                _call(parameters, 5, (ctypes.c_wchar_p, ctypes.c_long, P, ctypes.c_long), (name, 0, ctypes.byref(value), 0))
            with _bstr(_get(item, '__RELPATH')) as path, _bstr('WmiSetBrightness') as method:
                _call(service, 24, (P, P, ctypes.c_long, P, P, P, P),
                      (path, method, 0x10, None, parameters, None, ctypes.byref(completion)))
            deadline = time.monotonic() + 2
            completed = False
            while True:
                if not completed and completion.value:
                    code = _call(completion, 3, (ctypes.c_long, P), (0, ctypes.byref(output)))
                    if code != 0x40004 and output.value:
                        status = _get(output, 'ReturnValue')
                        if status: raise OSError('WmiSetBrightness: %s' % status)
                        completed = True
                # Some drivers apply the level without completing the method reply.
                actual = _monitor(service, identity)['brightness']
                if actual == target: return actual
                if time.monotonic() >= deadline: raise OSError(tr("The driver did not confirm brightness %s%%.") % target)
                time.sleep(.02)
    raise OSError(tr("The selected built-in display cannot be controlled."))


def brightness(value=None):
    if value is not None and (type(value) not in (int, float) or not 0 <= value <= 100):
        raise ValueError(tr("Brightness: 0–100%."))
    with _connection() as service:
        monitor = _monitor(service)
        if value is None: return dict(brightness=monitor['brightness'])
        levels = sorted(set(monitor['levels']))
        if any(type(level) is not int or not 0 <= level <= 100 for level in levels): raise OSError(tr("Invalid WMI brightness levels."))
        target = min(levels, key=lambda level: abs(level-value)) if levels else round(value)
        return dict(brightness=target if monitor['brightness'] == target else _set(service, monitor['identity'], target))
