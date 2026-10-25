"""Core Audio mixer. COM pointers stay on the caller's initialized worker thread."""
from ngs.i18n import tr
import ctypes
from pathlib import Path

from ngs.system_status import _call, _guid, _pointer, _volume


P = ctypes.c_void_p


class PropertyKey(ctypes.Structure):
    _fields_ = [('fmtid', ctypes.c_byte * 16), ('pid', ctypes.c_uint32)]


class PropVariant(ctypes.Structure):
    _fields_ = [('vt', ctypes.c_ushort), ('reserved', ctypes.c_ushort * 3), ('value', ctypes.c_byte * 16)]


def _create(clsid, iid, output):
    ole = ctypes.WinDLL('ole32')
    ole.CoCreateInstance.argtypes = [P, P, ctypes.c_uint, P, P]
    code = ole.CoCreateInstance(_guid(clsid), None, 23, _guid(iid), ctypes.byref(output))
    if code < 0: raise OSError(tr("Windows audio interface unavailable: %08X") % (code & 0xffffffff))


def _text(pointer, index):
    ole = ctypes.WinDLL('ole32'); ole.CoTaskMemFree.argtypes = [P]
    value = P()
    _call(pointer, index, (P,), (ctypes.byref(value),))
    try: return ctypes.wstring_at(value) if value.value else ''
    finally: ole.CoTaskMemFree(value)


def _name(device):
    ole = ctypes.WinDLL('ole32'); ole.PropVariantClear.argtypes = [P]
    with _pointer() as store:
        _call(device, 4, (ctypes.c_uint, P), (0, ctypes.byref(store)))
        key = PropertyKey(_guid('a45c254e-df1c-4efd-8020-67d146a850e0'), 14)
        value = PropVariant()
        try:
            _call(store, 5, (P, P), (ctypes.byref(key), ctypes.byref(value)))
            address = ctypes.cast(ctypes.byref(value, 8), ctypes.POINTER(P)).contents
            return ctypes.wstring_at(address) if value.vt == 31 and address.value else tr("Audio device")
        finally: ole.PropVariantClear(ctypes.byref(value))


def _process_name(pid):
    if not pid: return tr("System sounds")
    kernel = ctypes.WinDLL('kernel32')
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]; kernel.OpenProcess.restype = P
    kernel.QueryFullProcessImageNameW.argtypes = [P, ctypes.c_uint, ctypes.c_wchar_p, P]
    kernel.CloseHandle.argtypes = [P]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: return tr("Application · PID %d") % pid
    try:
        text, size = ctypes.create_unicode_buffer(32768), ctypes.c_uint(32768)
        return Path(text.value).stem if kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)) else 'PID %d' % pid
    finally: kernel.CloseHandle(handle)


def _switch(enumerator, identity):
    previous = {}
    for role in (0, 1):
        with _pointer() as device:
            _call(enumerator, 4, (ctypes.c_int, ctypes.c_int, P), (0, role, ctypes.byref(device)))
            previous[role] = _text(device, 5)
    with _pointer() as policy:
        # Windows exposes no public setter; use the same PolicyConfig ABI as EarTrumpet.
        _create('870af99c-171d-4f9e-af0d-e63df40c2bc9', 'f8679f50-850a-41cf-9c72-430f290290c8', policy)
        changed = []
        try:
            for role in (0, 1):
                _call(policy, 13, (ctypes.c_wchar_p, ctypes.c_int), (identity, role)); changed.append(role)
        except OSError as error:
            failures = []
            for role in reversed(changed):
                try: _call(policy, 13, (ctypes.c_wchar_p, ctypes.c_int), (previous[role], role))
                except OSError as restore_error: failures.append(str(restore_error))
            if failures: raise OSError(tr("The audio device disconnected and Windows could not restore the previous output. Refresh the list. ") + '; '.join(failures)) from error
            raise


def mixer(action='read', value=None):
    if action not in ('read', 'output', 'session_set', 'session_mute'): raise ValueError(tr("Invalid mixer command."))
    if action == 'output' and (not isinstance(value, str) or not 1 <= len(value) <= 2048): raise ValueError(tr("Invalid audio device."))
    if action.startswith('session_'):
        if (not isinstance(value, dict) or set(value) != {'endpoint', 'session', 'value'}
                or any(not isinstance(value[k], str) or not 1 <= len(value[k]) <= 2048 for k in ('endpoint', 'session'))
                or (type(value['value']) is not bool if action == 'session_mute' else
                    type(value['value']) not in (int, float) or not 0 <= value['value'] <= 100)):
            raise ValueError(tr("Invalid application command."))
    with _pointer() as enumerator, _pointer() as collection, _pointer() as active:
        _create('bcde0395-e52f-467c-8e3d-c4579291692e', 'a95664d2-9614-4f35-a746-de8db63617e6', enumerator)
        _call(enumerator, 3, (ctypes.c_int, ctypes.c_uint, P), (0, 1, ctypes.byref(collection)))
        count = ctypes.c_uint(); _call(collection, 3, (P,), (ctypes.byref(count),))
        outputs = []
        for index in range(min(count.value, 64)):
            with _pointer() as device:
                _call(collection, 4, (ctypes.c_uint, P), (index, ctypes.byref(device)))
                outputs.append(dict(id=_text(device, 5), name=_name(device)))
        if action == 'output':
            if value not in {d['id'] for d in outputs}: raise ValueError(tr("The device is no longer available. Refresh the list."))
            _switch(enumerator, value)
        if not outputs: return dict(outputs=[], sessions=[], endpoint='', volume=-1, muted=False)
        _call(enumerator, 4, (ctypes.c_int, ctypes.c_int, P), (0, 1, ctypes.byref(active)))
        endpoint = _text(active, 5)
        if action.startswith('session_') and value['endpoint'] != endpoint: raise ValueError(tr("The audio output changed. Refresh the mixer."))
        for output in outputs: output['selected'] = output['id'] == endpoint
        sessions, changed = [], False
        with _pointer() as manager, _pointer() as entries:
            _call(active, 3, (P, ctypes.c_uint, P, P), (_guid('77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f'), 23, None, ctypes.byref(manager)))
            _call(manager, 5, (P,), (ctypes.byref(entries),))
            _call(entries, 3, (P,), (ctypes.byref(count),))
            for index in range(min(count.value, 256)):
                with _pointer() as control, _pointer() as control2, _pointer() as volume:
                    _call(entries, 4, (ctypes.c_int, P), (index, ctypes.byref(control)))
                    state = ctypes.c_int(); _call(control, 3, (P,), (ctypes.byref(state),))
                    if state.value == 2: continue
                    _call(control, 0, (P, P), (_guid('bfb7ff88-7239-4fc9-8fa2-07c950be9c6d'), ctypes.byref(control2)))
                    identity = _text(control2, 13)
                    _call(control, 0, (P, P), (_guid('87ce5498-68d6-44e5-9215-6da47ef883d8'), ctypes.byref(volume)))
                    if action.startswith('session_') and value['session'] == identity:
                        if action == 'session_set': _call(volume, 3, (ctypes.c_float, P), (value['value']/100, None))
                        else: _call(volume, 5, (ctypes.c_int, P), (int(value['value']), None))
                        changed = True
                    level, muted, pid = ctypes.c_float(), ctypes.c_int(), ctypes.c_uint()
                    _call(volume, 4, (P,), (ctypes.byref(level),)); _call(volume, 6, (P,), (ctypes.byref(muted),))
                    _call(control2, 14, (P,), (ctypes.byref(pid),))
                    sessions.append(dict(id=identity, name=_process_name(pid.value), volume=round(level.value*100), muted=bool(muted.value)))
        if action.startswith('session_') and not changed: raise ValueError(tr("The application stopped playing audio. Refresh the mixer."))
        return dict(outputs=outputs, endpoint=endpoint, sessions=sessions, **_volume())
