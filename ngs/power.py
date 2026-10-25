"""Suspend the launched game's job before Windows sleeps, including while NGS is hidden."""

from ngs.i18n import tr
import ctypes
import logging
import sys
import threading
import uuid
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal


DISPLAY_STATUS = uuid.UUID("2b84c20e-ad23-4ddf-93db-05ffbd7efca5").bytes_le


class PowerSetting(ctypes.Structure):
    _fields_ = [("guid", ctypes.c_ubyte * 16), ("length", wintypes.DWORD), ("value", wintypes.DWORD)]


class PowerNotifications(QObject):
    changed = Signal()
    resumed = Signal()
    failed = Signal(str)

    def __init__(self, suspend, resume):
        super().__init__()
        self.suspend = suspend
        self.resumed.connect(resume)
        self.handle = wintypes.HANDLE()
        self.display_handle = wintypes.HANDLE()
        self.sleeping = False
        self._display_off = None
        self._lock = threading.RLock()
        if sys.platform != "win32":
            return
        self.api = ctypes.WinDLL("powrprof", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p)
        class Subscription(ctypes.Structure):
            _fields_ = [("callback", callback_type), ("context", ctypes.c_void_p)]
        self.callback = callback_type(self.notify)
        self.subscription = Subscription(self.callback, None)
        self.api.PowerRegisterSuspendResumeNotification.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
        self.api.PowerRegisterSuspendResumeNotification.restype = wintypes.DWORD
        self.api.PowerUnregisterSuspendResumeNotification.argtypes = [wintypes.HANDLE]
        self.api.PowerUnregisterSuspendResumeNotification.restype = wintypes.DWORD
        self.api.PowerSettingRegisterNotification.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                              ctypes.POINTER(wintypes.HANDLE)]
        self.api.PowerSettingRegisterNotification.restype = wintypes.DWORD
        self.api.PowerSettingUnregisterNotification.argtypes = [wintypes.HANDLE]
        self.api.PowerSettingUnregisterNotification.restype = wintypes.DWORD
        error = self.api.PowerRegisterSuspendResumeNotification(2, ctypes.byref(self.subscription), ctypes.byref(self.handle))
        if error:
            raise ctypes.WinError(error)
        self.display_guid = (ctypes.c_ubyte * 16).from_buffer_copy(DISPLAY_STATUS)
        error = self.api.PowerSettingRegisterNotification(ctypes.byref(self.display_guid), 2,
                                                         ctypes.byref(self.subscription), ctypes.byref(self.display_handle))
        if error:
            self.close()
            raise ctypes.WinError(error)

    def notify(self, context, event, setting):
        try:
            with self._lock:
                if event == 0x8013 and setting:
                    value = ctypes.cast(setting, ctypes.POINTER(PowerSetting)).contents
                    if bytes(value.guid) != DISPLAY_STATUS or value.length != ctypes.sizeof(wintypes.DWORD) or value.value not in (0, 1, 2):
                        return 0
                    previous = self._display_off
                    self._display_off = value.value == 0
                    # Registration reports the current display state; that is not a button press.
                    if previous is None:
                        return 0
                    if self._display_off and not previous:
                        event = 4
                    elif previous and not self._display_off:
                        event = 7
                    else:
                        return 0
                if event == 4:
                    self.sleeping = True
                    logging.info("Power: freezing game before sleep/display off")
                    # Finish the freeze in the native callback, even when the Qt window is hidden.
                    self.suspend()
                    self.changed.emit()
                elif event in (7, 18) and self.sleeping and not self._display_off:
                    self.sleeping = False
                    logging.info("Power: display awake; waiting for manual game thaw")
                    self.resumed.emit()
        except Exception as error:
            logging.exception("Game sleep protection failed")
            self.failed.emit(tr("Could not freeze the game before sleep: ") + str(error))
        return 0

    def close(self):
        if self.display_handle:
            self.api.PowerSettingUnregisterNotification(self.display_handle)
            self.display_handle = wintypes.HANDLE()
        if self.handle:
            self.api.PowerUnregisterSuspendResumeNotification(self.handle)
            self.handle = wintypes.HANDLE()
