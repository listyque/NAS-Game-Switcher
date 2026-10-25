"""Windows process-tree lifetime without killing games when NGS exits."""

from ngs.i18n import tr
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes


def split_launch_arguments(value):
    if (not isinstance(value, str) or len(value) > 8192 or
            any(ord(c) < 32 and c != "\t" for c in value)):
        raise ValueError(tr("Launch options: up to 8192 characters without line breaks."))
    if not value.strip():
        return []
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL("kernel32")
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW("NGS.exe " + value, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = [argv[i] for i in range(1, count.value)]
        if len(result) > 64 or any(len(arg) > 2048 for arg in result):
            raise ValueError(tr("Up to 64 arguments, each up to 2048 characters."))
        return result
    finally:
        kernel.LocalFree(argv)


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
        (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_ORPHAN_HELPER_TOKENS = (
    "crashhandler",
    "unitycrashhandler",
    "crashreport",
    "crashsender",
    "bsndrpt",
    "bugsplat",
    "cefsubprocess",
    "crs-handler",
    "werfault",
)


class GameProcess:
    def __init__(self, executable, cwd, arguments):
        if sys.platform != "win32":
            raise OSError(tr("Game launching is supported on Windows."))
        import _winapi
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.c_void_p]
        self.api.QueryInformationJobObject.restype = wintypes.BOOL
        self.api.ResumeThread.argtypes = [wintypes.HANDLE]
        self.api.ResumeThread.restype = wintypes.DWORD
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.job = self.api.CreateJobObjectW(None, None)
        self.process = None
        self.pid = None
        self._lock = threading.RLock()
        self._states = []
        self._parked_windows = {}
        self._started_at = time.monotonic()
        self._foregrounded = False
        self._foreground_window = None
        self._had_windows = False
        self._window_pids = set()
        self._windowless_since = None
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        thread = None
        try:
            command = subprocess.list2cmdline([str(executable), *arguments])
            if len(command.encode("utf-16-le")) >= 65534:
                raise ValueError(tr("The game command line is too long."))
            self.process, thread, self.pid, _ = _winapi.CreateProcess(
                str(executable), command, None, None, False,
                0x00000004 | 0x00000400 | 0x08000000, os.environ.copy(), str(cwd), subprocess.STARTUPINFO())
            # Assign before resuming so a short-lived launcher cannot escape tree tracking.
            if not self.api.AssignProcessToJobObject(self.job, self.process):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                user = ctypes.WinDLL("user32", use_last_error=True)
                user.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
                user.AllowSetForegroundWindow.restype = wintypes.BOOL
                user.AllowSetForegroundWindow(self.pid)
                user.AllowSetForegroundWindow(0xFFFFFFFF)
            except Exception:
                pass
            if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if self.process is not None:
                _winapi.TerminateProcess(self.process, 1)  # Startup failed before any game code ran.
            self.close()
            raise
        finally:
            if thread is not None:
                _winapi.CloseHandle(thread)

    def _main_alive(self):
        if self.process is None:
            return False
        import _winapi
        try:
            return _winapi.WaitForSingleObject(self.process, 0) == 0x102
        except OSError:
            return False

    def _member_image_name(self, pid):
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.api.QueryFullProcessImageNameW.restype = wintypes.BOOL
        handle = self.api.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if self.api.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            self.api.CloseHandle(handle)

    def _only_orphan_helpers_alive(self, alive_members):
        if not alive_members:
            return True
        for pid in alive_members:
            if pid == self.pid or pid in self._window_pids:
                return False
            name = self._member_image_name(pid)
            if not name or not any(token in name for token in _ORPHAN_HELPER_TOKENS):
                return False
        return True

    def active(self):
        if not self.job:
            return False
        info = _Accounting()
        if not self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.active == 0:
            return False
        if self.frozen:
            return True
        try:
            wins = self.windows()
        except OSError:
            return True
        if wins:
            self._had_windows = True
            self._window_pids.update(pid for _, pid in wins)
            self._windowless_since = None
            return True
        if self._had_windows and not self._main_alive():
            try:
                alive_members = set(self.members())
            except OSError:
                return True
            if not (self._window_pids & alive_members) and self._only_orphan_helpers_alive(alive_members):
                now = time.monotonic()
                if self._windowless_since is None:
                    self._windowless_since = now
                elif now - self._windowless_since >= 1.5:
                    # Clean up orphaned background helpers (e.g. UnityCrashHandler64) after all game windows and main process exit.
                    try:
                        self.terminate()
                        for _ in range(30):
                            if not self.members():
                                break
                            time.sleep(0.02)
                    except OSError:
                        pass
                    return False
            else:
                self._windowless_since = None
        else:
            self._windowless_since = None
        return True

    @property
    def frozen(self):
        return bool(self._states)

    def members(self):
        # Open only job members; never follow process names or a stale parent PID.
        class Pids(ctypes.Structure):
            _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                        ("pids", ctypes.c_size_t * 256)]
        info = Pids()
        if not self.api.QueryInformationJobObject(self.job, 3, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.count > 256:
            raise OSError(tr("Too many game processes to freeze."))
        return list(info.pids[:info.count])

    def freeze(self):
        with self._lock:
            if self._states or not self.job:
                return
            # Windows 11 state-change objects undo our suspension when their handles close, even after a crash.
            native = ctypes.WinDLL("ntdll")
            if not hasattr(native, "NtCreateProcessStateChange"):
                raise OSError(tr("Freezing requires Windows 11."))
            create = native.NtCreateProcessStateChange
            create.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD]
            create.restype = wintypes.LONG
            change = native.NtChangeProcessState
            change.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
            change.restype = wintypes.LONG
            self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.api.OpenProcess.restype = wintypes.HANDLE
            self.api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
            self.api.IsProcessInJob.restype = wintypes.BOOL
            seen = set()
            deadline = time.monotonic() + 0.8
            try:
                self.park_windows()
                while True:
                    pending = set(self.members()) - seen
                    if not pending:
                        return
                    if time.monotonic() > deadline:
                        raise OSError(tr("Could not freeze the game process tree before sleep."))
                    for pid in pending:
                        if time.monotonic() > deadline:
                            raise OSError(tr("Could not freeze the game process tree before sleep."))
                        handle = self.api.OpenProcess(0x0200 | 0x0800 | 0x1000, False, pid)
                        if not handle:
                            if pid not in self.members():
                                continue
                            raise ctypes.WinError(ctypes.get_last_error())
                        state = wintypes.HANDLE()
                        try:
                            belongs = wintypes.BOOL()
                            if not self.api.IsProcessInJob(handle, self.job, ctypes.byref(belongs)) or not belongs:
                                raise OSError(tr("The process no longer belongs to the game."))
                            status = create(ctypes.byref(state), 1, None, handle, 0)
                            if status >= 0:
                                status = change(state, handle, 0, None, 0, 0)
                            if status < 0:
                                raise OSError(tr("Windows rejected freezing the process: NTSTATUS %08X") % (status & 0xFFFFFFFF))
                            self._states.append(state.value)
                            state = wintypes.HANDLE()
                            seen.add(pid)
                        finally:
                            if state:
                                self.api.CloseHandle(state)
                            self.api.CloseHandle(handle)
            except BaseException:
                self.thaw()
                raise

    def thaw(self):
        with self._lock:
            while self._states:
                if not self.api.CloseHandle(wintypes.HANDLE(self._states[-1])):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._states.pop()
            self.restore_windows()

    def restore_windows(self):
        with self._lock:
            user = self.window_api()
            members = set(self.members()) if self._parked_windows else set()
            for hwnd, (pid, maximized) in list(self._parked_windows.items()):
                current = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(current))
                if current.value == pid and pid in members:
                    user.ShowWindowAsync(hwnd, 3 if maximized else 9)
                self._parked_windows.pop(hwnd)

    def trim_working_set(self):
        """Trim only suspended members opened and rechecked against our job."""
        class Memory(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                ('peak', 'working', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
        with self._lock:
            if not self.frozen: raise OSError(tr("Freeze the game first."))
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
            api.IsProcessInJob.restype = wintypes.BOOL
            api.K32EmptyWorkingSet.argtypes = [wintypes.HANDLE]; api.K32EmptyWorkingSet.restype = wintypes.BOOL
            api.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            api.K32GetProcessMemoryInfo.restype = wintypes.BOOL
            before = after = failed = trimmed = 0
            for pid in self.members():
                handle = api.OpenProcess(0x1000 | 0x100, False, pid)  # QUERY_LIMITED_INFORMATION | SET_QUOTA
                if not handle:
                    failed += 1; continue
                try:
                    belongs = wintypes.BOOL()
                    if not api.IsProcessInJob(handle, self.job, ctypes.byref(belongs)) or not belongs:
                        failed += 1; continue
                    memory = Memory(); memory.cb = ctypes.sizeof(memory)
                    if not api.K32GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
                        failed += 1; continue
                    initial = memory.working
                    if not api.K32EmptyWorkingSet(handle): failed += 1; continue
                    before += initial; trimmed += 1
                    after += memory.working if api.K32GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb) else initial
                finally: self.api.CloseHandle(handle)
            if not trimmed: raise OSError(tr("Windows did not allow trimming this game's process memory."))
            return dict(before=before, after=after, processes=trimmed, failed=failed)

    def window_api(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        user.IsWindowEnabled.argtypes = [wintypes.HWND]
        user.IsWindowEnabled.restype = wintypes.BOOL
        user.IsIconic.argtypes = [wintypes.HWND]
        user.IsIconic.restype = wintypes.BOOL
        user.IsZoomed.argtypes = [wintypes.HWND]
        user.IsZoomed.restype = wintypes.BOOL
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.ShowWindow.restype = wintypes.BOOL
        user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user.ShowWindowAsync.restype = wintypes.BOOL
        user.BringWindowToTop.argtypes = [wintypes.HWND]
        user.BringWindowToTop.restype = wintypes.BOOL
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.SetForegroundWindow.restype = wintypes.BOOL
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetWindow.restype = wintypes.HWND
        user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongW.restype = wintypes.LONG
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
        user.GetWindowRect.restype = wintypes.BOOL
        return user

    def windows(self):
        user = self.window_api()
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL
        members, windows = set(self.members()), []
        def visit(hwnd, unused):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in members and user.IsWindowVisible(hwnd):
                windows.append((hwnd, pid.value))
            return True
        user.EnumWindows(callback_type(visit), 0)
        return windows

    def park_windows(self):
        with self._lock:
            user = self.window_api()
            for hwnd, pid in self.windows():
                if not user.IsIconic(hwnd):
                    if hwnd not in self._parked_windows:
                        self._parked_windows[hwnd] = (pid, bool(user.IsZoomed(hwnd)))
                    # SW_FORCEMINIMIZE also works when the owning game thread is suspended.
                    user.ShowWindow(hwnd, 11)

    def _window_priority(self, user, hwnd):
        ex_style = user.GetWindowLongW(hwnd, -20)
        is_tool = bool(ex_style & 0x00000080)  # WS_EX_TOOLWINDOW
        has_owner = bool(user.GetWindow(hwnd, 4))  # GW_OWNER
        rect = _Rect()
        area = 0
        if user.GetWindowRect(hwnd, ctypes.byref(rect)):
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        return (not is_tool, not has_owner, bool(user.IsIconic(hwnd)) or area > 0, area)

    def foreground(self, *, automatic=False):
        with self._lock:
            parked = list(self._parked_windows)
        self.restore_windows()
        user = self.window_api()
        seen = {}
        for hwnd, pid in self.windows():
            seen[hwnd] = pid
        for hwnd in parked:
            if hwnd not in seen:
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in set(self.members()):
                    seen[hwnd] = pid.value
        candidates = sorted(((hwnd, pid) for hwnd, pid in seen.items()
                             if user.IsWindowEnabled(hwnd) and not user.GetWindowLongW(hwnd, -20) & 0x08000000),
                            key=lambda item: self._window_priority(user, item[0]), reverse=True)
        if not candidates:
            raise OSError(tr("The game window has not appeared yet. Wait for startup to finish."))
        previous = getattr(self, '_foreground_window', None)
        if automatic and previous is not None:
            current = user.GetForegroundWindow()
            # Respect a user's switch away while the previous game window still exists.
            if previous in seen.items() and current and current not in seen:
                return False
            if candidates[0] == previous:
                return True
        self._foregrounded = False
        # Falling back to the old splash would mask a failed automatic handoff.
        for hwnd, pid in (candidates[:1] if automatic else candidates):
            try:
                from ngs.edge_swipe import disable_fullscreen_edge_gesture
                disable_fullscreen_edge_gesture(hwnd)
            except Exception:
                pass
            if user.IsIconic(hwnd) or not user.IsWindowVisible(hwnd):
                user.ShowWindowAsync(hwnd, 9)
            user.BringWindowToTop(hwnd)
            if user.SetForegroundWindow(hwnd) or user.GetForegroundWindow() == hwnd:
                self._foreground_window = (hwnd, pid)
                self._foregrounded = True
                return True
        raise OSError(tr("Windows did not allow the game to come to the foreground. Try returning again."))

    def terminate(self):
        with self._lock:
            if not self.job:
                raise OSError(tr("NGS is no longer tracking this game."))
            self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            self.api.TerminateJobObject.restype = wintypes.BOOL
            if not self.api.TerminateJobObject(self.job, 1):
                raise ctypes.WinError(ctypes.get_last_error())

    def exit_code(self):
        import _winapi
        return _winapi.GetExitCodeProcess(self.process)

    def close(self):
        import _winapi
        with self._lock:
            self.thaw()
            if self.process is not None:
                _winapi.CloseHandle(self.process)
                self.process = None
            if self.job:
                _winapi.CloseHandle(self.job)
                self.job = None


def reveal_file(path):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell.ILCreateFromPathW.restype = ctypes.c_void_p
    shell.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
    shell.SHOpenFolderAndSelectItems.restype = wintypes.LONG
    shell.ILFree.argtypes = [ctypes.c_void_p]
    item = shell.ILCreateFromPathW(str(path))
    if not item:
        raise OSError(tr("Could not locate the file in Explorer."))
    try:
        result = shell.SHOpenFolderAndSelectItems(item, 0, None, 0)
        if result < 0:
            raise OSError(tr("Explorer rejected the request: HRESULT %08X") % (result & 0xFFFFFFFF))
    finally:
        shell.ILFree(item)


def set_topmost(hwnd, enabled):
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    api.SetWindowPos.restype = wintypes.BOOL
    if not api.SetWindowPos(hwnd, -1 if enabled else -2, 0, 0, 0, 0, 0x0013):
        raise ctypes.WinError(ctypes.get_last_error())


def send_window_to_back(hwnd):
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    api.SetWindowPos.restype = wintypes.BOOL
    api.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0013)
    if not api.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0013):
        raise ctypes.WinError(ctypes.get_last_error())
