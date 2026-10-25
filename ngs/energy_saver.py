"""The Windows shell's energy-saver override, without opening Settings or changing plans."""
from ngs.i18n import tr
import ctypes
import sys
import time
import winreg


OVERRIDE = 0x41c6013da3bc3075  # WNF_PO_ENERGY_SAVER_OVERRIDE, DWORD: automatic/force on/force off.
P = ctypes.c_void_p


def _query(dll):
    name, stamp, value, size = ctypes.c_ulonglong(OVERRIDE), ctypes.c_ulong(), ctypes.c_uint(), ctypes.c_ulong(4)
    code = dll.NtQueryWnfStateData(ctypes.byref(name), None, None, ctypes.byref(stamp), ctypes.byref(value), ctypes.byref(size))
    if code < 0: raise OSError(tr("Energy saver state unavailable: %08X") % (code & 0xffffffff))
    if size.value != 4 or value.value not in (0, 1, 2): raise OSError(tr("This version of the energy saver switch is not supported yet."))
    return value.value, stamp.value


def _effective():
    if sys.getwindowsversion().build >= 26100:
        # On 24H2 the old Win32/WinRT status can stay off even while the shell enables Energy Saver.
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Power') as key:
                value, kind = winreg.QueryValueEx(key, 'EnergySaverState')
            if kind == winreg.REG_DWORD and value in (1, 2): return value == 1
        except OSError:
            pass
        return None
    status = (ctypes.c_ubyte * 12)()
    kernel = ctypes.WinDLL('kernel32'); kernel.GetSystemPowerStatus.argtypes = [P]
    if kernel.GetSystemPowerStatus(status) and status[3] in (0, 1): return bool(status[3])
    return None


def energy_saver(enabled=None):
    if enabled is not None and type(enabled) is not bool: raise ValueError(tr("Energy saver: enable or disable."))
    dll = ctypes.WinDLL('ntdll')
    dll.NtQueryWnfStateData.argtypes = [P, P, P, P, P, P]; dll.NtQueryWnfStateData.restype = ctypes.c_long
    dll.NtUpdateWnfStateData.argtypes = [P, P, ctypes.c_ulong, P, P, ctypes.c_ulong, ctypes.c_int]
    dll.NtUpdateWnfStateData.restype = ctypes.c_long
    try:
        previous, stamp = _query(dll)
        current = _effective()
        if current is None: raise OSError(tr("Windows does not provide a verified energy saver state."))
    except OSError as error:
        if enabled is not None: raise
        return dict(energy_saver=None, energy_saver_available=False, energy_saver_message=str(error))
    if enabled is None or current == enabled:
        return dict(energy_saver=current, energy_saver_available=True, energy_saver_message='')
    name, target = ctypes.c_ulonglong(OVERRIDE), ctypes.c_uint(1 if enabled else 2)
    # shortcut: private shell ABI; recheck against the shell if Windows changes this payload.
    code = dll.NtUpdateWnfStateData(ctypes.byref(name), ctypes.byref(target), 4, None, None, stamp, True)
    if code < 0: raise OSError(tr("Windows rejected changing energy saver: %08X") % (code & 0xffffffff))
    own_stamp = (stamp + 1) & 0xffffffff
    for _ in range(15):
        current = _effective()
        if current is enabled: return dict(energy_saver=current, energy_saver_available=True, energy_saver_message='')
        time.sleep(.1)
    observed, stamp = _query(dll)
    if observed == target.value and stamp == own_stamp:
        restore = ctypes.c_uint(previous)
        code = dll.NtUpdateWnfStateData(ctypes.byref(name), ctypes.byref(restore), 4, None, None, stamp, True)
        if code < 0: raise OSError(tr("Windows did not confirm the change and rejected restoring the previous state: %08X") % (code & 0xffffffff))
    raise OSError(tr("Windows did not apply the selected energy saver state. It may be unavailable while plugged in or restricted by policy."))
