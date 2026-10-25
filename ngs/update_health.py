"""Confirm a replacement only after its first ordinary, rendered startup.

The updater's hidden import probe is deliberately not a health acknowledgement.
The native bootstrap owns recovery after an unexpected, unconfirmed exit.
No timer is installed during subsequent starts or in probe/self-test mode.
"""

import json
import logging
import os
import re
import sys
import time

from ngs.catalog import atomic_write
from ngs.install import safe_path


STARTUP_GRACE_SECONDS = 10
STARTUP_DEADLINE_SECONDS = 60
CLOSE_EVENT_VARIABLE = "NGS_FIRST_START_CLOSE_EVENT"


def _signal_orderly_close():
    """Notify only this bootstrap that the pending GUI was deliberately closed.

    The event is created anew by the native parent, is not stored on disk and
    is considered only together with runtime exit code zero. A crash during
    shutdown therefore still invokes recovery, and no stale file can exempt a
    later failed startup.
    """
    name = os.environ.get(CLOSE_EVENT_VARIABLE, "")
    if sys.platform != "win32" or not re.fullmatch(r"Local\\NGS\.FirstStart\.[0-9]+\.[0-9]+", name):
        return False
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenEventW.restype = wintypes.HANDLE
    api.SetEvent.argtypes = [wintypes.HANDLE]
    api.SetEvent.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not api.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)
    return True


class FirstStartHealth:
    def __init__(self, storage, *, clock=time.monotonic):
        self.storage, self.clock = storage, clock
        self.pending = safe_path(storage.root, "data/updates/first-start.json")
        self.confirmed = safe_path(storage.root, "data/updates/first-start-ok.json")
        self.marker = self._read_pending()
        self.frame_at = None

    def _read_pending(self):
        if not self.pending.exists():
            return None
        if not self.pending.is_file() or self.pending.stat().st_size > 2048:
            raise ValueError("Invalid first-start marker")
        marker = json.loads(self.pending.read_text(encoding="utf-8-sig"))
        if (not isinstance(marker, dict) or set(marker) != {"schema", "version", "token"}
                or type(marker["schema"]) is not int or marker["schema"] != 1
                or not isinstance(marker["token"], str) or not re.fullmatch("[0-9a-f]{32}", marker["token"])
                or not isinstance(marker["version"], str)):
            raise ValueError("Invalid first-start identity")
        from ngs.updates import current_version
        if marker["version"] != current_version(self.storage):
            raise ValueError("First-start version does not match the running runtime")
        return marker

    def first_frame(self):
        if self.marker is not None and self.frame_at is None:
            self.frame_at = self.clock()

    def intentional_close(self):
        """Leave the version pending for a later launch, without accepting it."""
        if self.marker is None:
            return False
        safe_path(self.storage.root, "data/updates/first-start.json")
        if self._read_pending() != self.marker:
            raise ValueError("First-start transaction changed before close")
        notified = _signal_orderly_close()
        from ngs.diagnostics import event
        event("update.first_start_closed", version=self.marker["version"], notified=notified)
        return notified

    def confirm(self, ready=True):
        """Return True once the matching pending launch is atomically accepted."""
        if (self.marker is None or self.frame_at is None or not ready
                or self.clock() - self.frame_at < STARTUP_GRACE_SECONDS):
            return False
        # Do not acknowledge a later transaction, a redirected path or stale state.
        if safe_path(self.storage.root, "data/updates/first-start.json") != self.pending:
            raise ValueError("First-start path changed")
        safe_path(self.storage.root, "data/updates/first-start-ok.json")
        if self._read_pending() != self.marker:
            raise ValueError("First-start transaction changed")
        # The rename is the commit point. A successful ordinary launch must not
        # roll back merely because result.json cannot be refreshed afterwards.
        os.replace(self.pending, self.confirmed)
        version = self.marker["version"]
        self.marker = None
        try:
            result = safe_path(self.storage.root, "data/updates/result.json")
            atomic_write(result, json.dumps(dict(status="installed", version=version,
                message="Update installed; ordinary startup confirmed."), ensure_ascii=False).encode("utf8"))
        except (OSError, ValueError):
            logging.exception("Startup was confirmed, but the update result could not be saved")
        from ngs.diagnostics import event
        event("update.startup_confirmed", version=version,
              elapsed_ms=round((self.clock() - self.frame_at) * 1000))
        return True


def watch_first_start(storage, window, application, *, ready=lambda: True, ready_changed=None,
                      closing=lambda: False, confirmed=lambda: None):
    """Attach a bounded GUI-thread health check; return None on ordinary later starts."""
    try:
        health = FirstStartHealth(storage)
    except (OSError, ValueError):
        logging.exception("Cannot validate the pending first startup; keeping recovery available")
        return None
    if health.marker is None:
        return None
    from PySide6.QtCore import QTimer, Qt
    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.setTimerType(Qt.TimerType.CoarseTimer)
    completed = False
    expired = False
    awaiting_frame = True

    def first_frame():
        nonlocal awaiting_frame
        if completed or health.frame_at is not None:
            return  # A queued frame can already be in flight at disconnect time.
        awaiting_frame = False
        health.first_frame()
        timer.start(round(STARTUP_GRACE_SECONDS * 1000))

    def finish():
        nonlocal completed
        if completed:
            return
        completed = True
        timer.stop()
        if ready_changed is not None:
            ready_changed.disconnect(check)
        application.aboutToQuit.disconnect(shutdown)
        timer.deleteLater()

    def shutdown():
        if completed:
            return
        try:
            if closing():
                health.intentional_close()
        except (OSError, ValueError):
            logging.exception("Cannot report the intentional early close; recovery is retained")
        finally:
            # A queued readiness change or frame must not acknowledge a GUI that
            # has already begun to quit, even if the event loop drains callbacks.
            finish()

    def check():
        nonlocal expired
        if completed or health.frame_at is None:
            return
        try:
            if health.confirm(ready()):
                try:
                    confirmed()
                finally:
                    finish()
                return
            if health.clock() - health.frame_at < STARTUP_DEADLINE_SECONDS:
                timer.start(1000)
            else:
                # A user may leave an optional InputHost UAC prompt open longer
                # than a minute. Stop polling, but let its completion signal
                # confirm the still-running GUI later instead of forcing rollback.
                timer.stop()
                if not expired:
                    logging.warning("First startup is still unconfirmed; waiting for readiness without polling")
                    expired = True
        except (OSError, ValueError):
            logging.exception("First startup could not be confirmed; recovery is retained")
            finish()

    timer.timeout.connect(check)
    window.frameSwapped.connect(first_frame, Qt.ConnectionType.QueuedConnection)
    application.aboutToQuit.connect(shutdown)
    if ready_changed is not None:
        ready_changed.connect(check)
    # Explicitly retain the callbacks for the lifetime of the window.
    window._ngs_startup_health = health, timer, first_frame, check
    return health
