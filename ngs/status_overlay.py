"""System controls shared by the portable and installed top-edge panel."""
import ctypes
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, Property, Signal, Slot
from ngs.system_controls import SystemControls
from ngs.system_status import SystemStatus


class StatusOverlay(QObject):
    changed = Signal()
    action = Signal(str)
    requested = Signal(str)
    closed = Signal()
    systemStatusChanged = Signal()
    systemSettingsFocus = Signal(bool)
    edgeContact = Signal(str)
    edgeDragChanged = Signal()
    launcherActionRequested = Signal(str)

    def __init__(self, keyboard, parent):
        super().__init__(parent)
        self._keyboard = keyboard
        self.system_status, self.system_controls = SystemStatus(self), SystemControls(self)
        self.system_status.changed.connect(self.systemStatusChanged)
        self.shown, self.screen_name, self.last_request = False, '', 0
        self.remote, self.parked = None, None
        self.sleeping, self.edge_enabled = False, False
        self.dialog = False
        self.closing = False
        self._drag_progress = 0.0
        self.systemSettingsFocus.connect(self.dialog_focus)
        from ngs.edge_swipe import EdgeGesture
        self.edge_gesture = EdgeGesture()
        options = keyboard.options
        self._enabled = options['overlay_enabled']
        self._edge_swipe_enabled = options['edge_swipe_enabled']
        self._edge_auto_hide = options['edge_auto_hide']
        keyboard.changed.connect(self.settings_changed)

    @Property(bool, notify=changed)
    def enabled(self): return self._enabled

    def settings_changed(self):
        options = self._keyboard.options
        values = (options['overlay_enabled'], options['edge_swipe_enabled'], options['edge_auto_hide'])
        if values == (self._enabled, self._edge_swipe_enabled, self._edge_auto_hide): return
        self._enabled, self._edge_swipe_enabled, self._edge_auto_hide = values
        if not self.edgeSwipeEnabled:
            self.edge_gesture.clear()
            self.setEdgeDrag('', 0.0)
        if not self._enabled:
            self.edge_gesture.clear()
            if self.remote and self.shown: self.remote.status(False, self.screen_name)
            self.finishHide()
        self.changed.emit()

    @Property(QObject, constant=True)
    def keyboard(self): return self._keyboard

    @Property(QObject, constant=True)
    def systemControls(self): return self.system_controls

    @Property('QVariantMap', notify=systemStatusChanged)
    def systemState(self): return self.system_status.state

    @Property('QVariantMap', constant=True)
    def inputHints(self): return dict(device='touch', back='Esc')

    @Property(bool, notify=changed)
    def visible(self): return self.enabled and self.shown and not self.sleeping and not self.dialog and not self.closing

    @Property(bool, notify=changed)
    def suspended(self): return self.sleeping or self.dialog or self.remoteHost

    @Property(bool, notify=changed)
    def exiting(self): return self.closing

    @Property(bool, notify=changed)
    def remoteHost(self): return self.remote is not None

    @Property(bool, notify=changed)
    def edgeEnabled(self): return self.edgeSwipeEnabled and self.edge_enabled and not self.remoteHost and not self.shown and not self.sleeping

    @Property(bool, notify=changed)
    def edgeWindowVisible(self): return self.edgeSwipeEnabled and self.edge_enabled and not self.remoteHost and not self.sleeping

    @Property(bool, notify=changed)
    def edgeSwipeEnabled(self): return self.enabled and self._edge_swipe_enabled

    @Property(bool, notify=changed)
    def edgeAutoHide(self): return self._edge_auto_hide

    @Property(float, notify=edgeDragChanged)
    def dragProgress(self): return self._drag_progress

    @Slot(str, float)
    def setEdgeDrag(self, screen_name, progress):
        clamped = max(0.0, min(2.2, float(progress or 0.0))) if self.edgeSwipeEnabled and not self.sleeping else 0.0
        if screen_name and not self.shown:
            self.screen_name = screen_name
        if abs(clamped - self._drag_progress) > 0.004 or (clamped == 0.0 and self._drag_progress != 0.0):
            self._drag_progress = clamped
            self.edgeDragChanged.emit()

    @Slot(str, int, float, float, bool)
    def edgeTouch(self, screen_name, identity, x, y, down):
        if not self.edgeSwipeEnabled or self.sleeping:
            self.setEdgeDrag(screen_name, 0.0)
            return
        if down and identity not in self.edge_gesture.contacts: self.edgeContact.emit(screen_name)
        recognized = self.edge_gesture.feed(identity, x, y, down, (0, 0, 100000, 100000, 1), time.monotonic())
        if down and self.edge_gesture.drag_start and self.edge_gesture.drag_start[0] == identity:
            sy = self.edge_gesture.drag_start[2]
            self.setEdgeDrag(screen_name, max(0.0, y / 96.0) if y - sy >= 4.0 or self._drag_progress > 0 else 0.0)
        elif not down:
            if not self.shown and self._drag_progress < 0.35:
                self.setEdgeDrag(screen_name, 0.0)
            elif self.shown and self._drag_progress < 0.45 and time.monotonic() - self.last_request > 0.25:
                self.hide()
            else:
                self.setEdgeDrag(screen_name, 0.0)
        if recognized and not self.shown:
            self.request(screen_name)

    @Slot()
    def cancelEdgeTouch(self):
        self.edge_gesture.clear()
        if not self.shown:
            self.setEdgeDrag('', 0.0)

    @Property(str, notify=changed)
    def screenName(self): return self.screen_name

    @Slot()
    def refreshSystemStatus(self): self.system_status.refresh()

    @Slot(str)
    def triggerLauncherAction(self, name):
        if name not in ('downloads', 'settings'): return
        self.hide()
        self.launcherActionRequested.emit(name)

    def dialog_focus(self, opening):
        self.dialog = opening
        self.changed.emit()

    @Slot(str)
    def request(self, screen_name):
        if not self.enabled or self.sleeping: return
        if time.monotonic() - self.last_request < .6: return
        self.last_request = time.monotonic()
        self.requested.emit(screen_name)

    def show(self, screen_name, *, from_remote=False):
        if not self.enabled or self.sleeping: return
        self.closing = False
        self.screen_name, self.shown = screen_name, True
        if self.remote:
            if not from_remote: self.remote.status(True, screen_name)
        else: self.refreshSystemStatus()
        self.changed.emit()

    @Slot()
    def hide(self):
        self._drag_progress = 0.0
        self.edgeDragChanged.emit()
        if self.suspended:
            self.finishHide()
        elif self.shown and not self.closing:
            self.closing = True
            self.changed.emit()

    @Slot()
    def finishHide(self):
        if not self.shown and not self.closing: return
        self.closing = False
        self.shown = False
        self._drag_progress = 0.0
        self.edge_gesture.clear()
        self.edgeDragChanged.emit()
        self.changed.emit()
        self.closed.emit()

    def close(self):
        self.closing = False
        if self.remote: self.remote.status(False, self.screen_name)
        self.shown = False; self.edge_enabled = False; self.changed.emit()
        self.system_controls.close(); self.system_status.close()


def park_fullscreen_game(bridge):
    """Only our foreground game job may be parked for an exclusive surface."""
    process = bridge.process
    if process is None or bridge.gameFrozen or bridge._terminating: return None
    user = process.window_api()
    foreground = user.GetForegroundWindow()
    if not any(hwnd == foreground for hwnd, _ in process.windows()): return None
    rect = wintypes.RECT()
    class Monitor(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('rect', wintypes.RECT), ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HMONITOR
    user.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(Monitor)]
    info = Monitor(); info.size = ctypes.sizeof(info)
    if not user.GetWindowRect(foreground, ctypes.byref(rect)) or not user.GetMonitorInfoW(user.MonitorFromWindow(foreground, 2), ctypes.byref(info)): return None
    monitor = info.rect
    if rect.left <= monitor.left and rect.top <= monitor.top and rect.right >= monitor.right and rect.bottom >= monitor.bottom:
        process.park_windows()
        return process
    return None
