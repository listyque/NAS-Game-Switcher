"""Compact top-edge handles accepting touchscreen input only."""
import ctypes
import logging
import sys
import time
import uuid
from ctypes import wintypes as w

from PySide6.QtCore import QObject, Qt, Signal, QEvent
from PySide6.QtGui import QGuiApplication, QEventPoint, QInputDevice
from ngs.touchpad import _PointerInfo, _TouchInput


class _MouseInput(ctypes.Structure):
    _fields_ = [('point', w.POINT), ('data', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('extra', ctypes.c_size_t)]


class _PropertyKey(ctypes.Structure):
    _fields_ = [('fmtid', ctypes.c_byte * 16), ('pid', w.DWORD)]


class _PropVariant(ctypes.Structure):
    _fields_ = [('vt', w.WORD), ('r1', w.WORD), ('r2', w.WORD), ('r3', w.WORD), ('val', w.SHORT), ('pad', ctypes.c_byte * 6)]


class _Margins(ctypes.Structure):
    _fields_ = [('cxLeftWidth', ctypes.c_int), ('cxRightWidth', ctypes.c_int),
                ('cyTopHeight', ctypes.c_int), ('cyBottomHeight', ctypes.c_int)]


class _DwmBlurBehind(ctypes.Structure):
    _fields_ = [('dwFlags', w.DWORD), ('fEnable', w.BOOL),
                ('hRgnBlur', w.HRGN), ('fTransitionOnMaximized', w.BOOL)]


_DWM_CONFIGURED = set()


def enable_dwm_alpha_composition(hwnd, force=False):
    """Enable DWM per-pixel alpha composition on a frameless HWND for Vulkan/OpenGL swapchains."""
    if sys.platform != 'win32' or not hwnd:
        return False
    hwnd = int(hwnd)
    if not force and hwnd in _DWM_CONFIGURED:
        return True
    try:
        dwm = ctypes.WinDLL('dwmapi')
        gdi = ctypes.WinDLL('gdi32')
        dwm.DwmExtendFrameIntoClientArea.argtypes = [w.HWND, ctypes.POINTER(_Margins)]
        dwm.DwmExtendFrameIntoClientArea.restype = ctypes.c_long
        dwm.DwmEnableBlurBehindWindow.argtypes = [w.HWND, ctypes.POINTER(_DwmBlurBehind)]
        dwm.DwmEnableBlurBehindWindow.restype = ctypes.c_long
        gdi.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        gdi.CreateRectRgn.restype = w.HRGN
        gdi.DeleteObject.argtypes = [w.HANDLE]
        gdi.DeleteObject.restype = w.BOOL
        margins = _Margins(-1, -1, -1, -1)
        ok = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins)) >= 0
        rgn = gdi.CreateRectRgn(0, 0, -1, -1)
        try:
            blur = _DwmBlurBehind(dwFlags=3, fEnable=1, hRgnBlur=rgn, fTransitionOnMaximized=0)
            ok = (dwm.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(blur)) >= 0) or ok
        finally:
            if rgn:
                gdi.DeleteObject(rgn)
        if ok:
            _DWM_CONFIGURED.add(hwnd)
        return ok
    except Exception:
        return False


def disable_fullscreen_edge_gesture(hwnd, disable=True):
    """Set the fullscreen window preference for system edge gestures."""
    if sys.platform != 'win32' or not hwnd:
        return False
    try:
        shell = ctypes.WinDLL('shell32')
        shell.SHGetPropertyStoreForWindow.argtypes = [w.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        iid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99').bytes_le)
        pkey = _PropertyKey((ctypes.c_byte * 16).from_buffer_copy(uuid.UUID('32ce38b2-2c9a-41b1-9bc5-b3784394aa44').bytes_le), 2)
        pvar = _PropVariant(vt=11, val=-1 if disable else 0)
        store = ctypes.c_void_p()
        if shell.SHGetPropertyStoreForWindow(int(hwnd), iid, ctypes.byref(store)) < 0 or not store.value:
            return False
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        try:
            res = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))(vtable[6])(store, ctypes.byref(pkey), ctypes.byref(pvar))
            ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])(store)
            return res >= 0
        finally:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(store)
    except Exception:
        return False


class EdgeGesture:
    def __init__(self): self.start = None; self.drag_start = None; self.contacts = set(); self.last = 0

    def clear(self): self.start = None; self.drag_start = None; self.contacts.clear()

    def feed(self, identity, x, y, down, area, now):
        if now - self.last > 1.4: self.clear()
        self.last = now
        if not down:
            self.contacts.discard(identity)
            if self.drag_start and self.drag_start[0] == identity: self.drag_start = None
            if not self.start or self.start[0] != identity: return False
        else:
            first = identity not in self.contacts
            self.contacts.add(identity)
            if len(self.contacts) > 2:
                self.start = None; self.drag_start = None
                return False
            left, top, width, height, scale = area
            if first and len(self.contacts) == 1:
                self.start = (identity, x, y, now) if left <= x < left + width and top <= y <= top + 32 * scale else None
                self.drag_start = self.start
        if not self.start: return False
        if self.start[0] != identity: return False
        scale = area[4]
        _, sx, sy, started = self.start
        if now - started > 1.4 or abs(x - sx) > 80 * scale or y < sy - 12 * scale:
            self.start = None; self.drag_start = None
        elif y - sy >= 36 * scale:
            self.start = None
            return True
        if not down: self.start = None; self.drag_start = None
        return False


class _Dispatch(QObject):
    recognized = Signal(str)
    dismissed = Signal()
    observed = Signal(str, int, int, bool)
    traced = Signal(dict)


class EdgeWindows(QObject):
    """A touch-only strip keeps pointer-native apps from swallowing the initial edge contact."""
    def __init__(self, engine, controller, resources):
        super().__init__()
        from PySide6.QtCore import QUrl
        from PySide6.QtQml import QQmlComponent
        self.engine, self.controller, self.windows = engine, controller, []
        self.handles, self.gestures, self.backings = {}, {}, {}
        self.backing_policy = {}
        self.subclasses = set()
        self.trace_until, self.trace_count = time.monotonic()+180, 0
        self.trace_moves, self.trace_contacts = set(), set()
        self.scroll_links = []
        self.status_window = None
        self.dispatch = _Dispatch(controller)
        self.dispatch.recognized.connect(self.request, Qt.ConnectionType.QueuedConnection)
        self.dispatch.dismissed.connect(self.controller.hide, Qt.ConnectionType.QueuedConnection)
        self.dispatch.observed.connect(self.report_input, Qt.ConnectionType.QueuedConnection)
        self.dispatch.traced.connect(self.report_trace, Qt.ConnectionType.QueuedConnection)
        self.changed = lambda: None
        self.closed = False
        self.component = QQmlComponent(engine, QUrl.fromLocalFile(str(resources / 'qml/EdgeSwipeWindow.qml')))
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.comctl = ctypes.WinDLL('comctl32', use_last_error=True)
        subclass = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        self.subclass_callback = subclass(self.window_message)
        self.comctl.SetWindowSubclass.argtypes = [w.HWND, subclass, ctypes.c_size_t, ctypes.c_size_t]
        self.comctl.SetWindowSubclass.restype = w.BOOL
        self.comctl.RemoveWindowSubclass.argtypes = [w.HWND, subclass, ctypes.c_size_t]
        self.comctl.RemoveWindowSubclass.restype = w.BOOL
        self.comctl.DefSubclassProc.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self.comctl.DefSubclassProc.restype = ctypes.c_ssize_t
        self.user.RegisterTouchWindow.argtypes = [w.HWND, w.ULONG]
        self.user.RegisterTouchWindow.restype = w.BOOL
        self.user.UnregisterTouchWindow.argtypes = [w.HWND]
        self.user.UnregisterTouchWindow.restype = w.BOOL
        self.user.GetPointerInfo.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PointerInfo)]
        self.user.GetPointerInfo.restype = w.BOOL
        self.user.GetPointerInfoHistory.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_PointerInfo)]
        self.user.GetPointerInfoHistory.restype = w.BOOL
        self.user.ScreenToClient.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
        self.user.ScreenToClient.restype = w.BOOL
        self.user.GetTouchInputInfo.argtypes = [w.HANDLE, w.UINT, ctypes.POINTER(_TouchInput), ctypes.c_int]
        self.user.GetTouchInputInfo.restype = w.BOOL
        self.user.CloseTouchInputHandle.argtypes = [w.HANDLE]
        self.user.CloseTouchInputHandle.restype = w.BOOL
        self.user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
        self.user.GetWindowRect.restype = w.BOOL
        self.user.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
        self.user.ClientToScreen.restype = w.BOOL
        self.user.WindowFromPoint.argtypes = [w.POINT]
        self.user.WindowFromPoint.restype = w.HWND
        self.user.GetForegroundWindow.restype = w.HWND
        self.user.GetCapture.restype = w.HWND
        self.user.GetMessageExtraInfo.restype = w.LPARAM
        self.user.GetMessageTime.restype = w.LONG
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetTickCount64.restype = ctypes.c_ulonglong
        self.tick_count = kernel.GetTickCount64
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.GetWindowThreadProcessId.restype = w.DWORD
        app = QGuiApplication.instance()
        app.screenAdded.connect(self.add); app.screenRemoved.connect(self.remove)
        for screen in app.screens(): self.add(screen)
        app.aboutToQuit.connect(self.close)
        controller.destroyed.connect(self.close)
        controller.changed.connect(self.update_enabled)
        self.update_enabled()
        logging.info('Top-edge touch-only capture ready: windows=%s', len(self.handles))

    def subclass_window(self, hwnd):
        if QGuiApplication.platformName() != 'windows' or hwnd in self.subclasses: return
        if not self.comctl.SetWindowSubclass(hwnd, self.subclass_callback, id(self), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        self.subclasses.add(hwnd)

    def unsubclass_window(self, hwnd):
        if hwnd in self.subclasses:
            self.comctl.RemoveWindowSubclass(hwnd, self.subclass_callback, id(self))
            self.subclasses.discard(hwnd)

    def window_message(self, hwnd, message, wparam, lparam, subclass_id, data):
        # WM_TOUCH sent directly to an HWND bypasses Qt's application native filter.
        try:
            if message == 0x82:  # WM_NCDESTROY: remove before Qt releases this HWND.
                self.unsubclass_window(hwnd)
                self.gestures.pop(hwnd, None)
            elif not self.closed:
                self.trace_native(hwnd, message, wparam, lparam)
                handled, result = self.message(hwnd, message, wparam, lparam)
                if handled: return result
        except Exception:
            logging.exception('Cannot process top-edge window input')
            if message == 0x240 and hwnd in self.handles: return 0  # The touch handle was already closed in message().
        return self.comctl.DefSubclassProc(hwnd, message, wparam, lparam)

    def tracing(self):
        return (not self.closed and time.monotonic() < getattr(self, 'trace_until', 0)
                and self.trace_count < 256 and not self.controller.keyboard.bridge.settings.get('production_mode'))

    def trace_input(self, window, stage, x=0, y=0, pointer=0, flags=0, decision='native', timestamp=0):
        if not self.tracing(): return
        self.trace_count += 1
        try:
            clock = int(self.tick_count()) & 0xffffffff
            age = (clock - timestamp) & 0xffffffff if timestamp else -1
            if age > 60000: age = -1
            hwnd = int(window.winId())
            ratio = window.devicePixelRatio()
            point = w.POINT(round(x*ratio), round(y*ratio))
            self.user.ClientToScreen(hwnd, ctypes.byref(point))
            target = self.user.WindowFromPoint(point)
            target_pid, foreground_pid = w.DWORD(), w.DWORD()
            if target: self.user.GetWindowThreadProcessId(target, ctypes.byref(target_pid))
            foreground = self.user.GetForegroundWindow()
            if foreground: self.user.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
            capture = self.user.GetCapture()
            data = dict(role='handle' if hwnd in self.handles else 'launcher', stage=stage, decision=decision,
                        pointer=int(pointer), flags=int(flags), x=round(x), y=round(y), clock_ms=clock, age_ms=age,
                        width=window.width(), height=window.height(), scale=round(ratio*100),
                        target_pid=int(target_pid.value), target_self=int(target==hwnd),
                        foreground_pid=int(foreground_pid.value), capture=0 if not capture else 1 if capture==hwnd else 2,
                        state=sum(bit for bit, value in ((1,self.controller.enabled),(2,self.controller.edgeEnabled),
                            (4,self.controller.remoteHost),(8,self.controller.shown),(16,self.controller.sleeping)) if value))
            self.dispatch.traced.emit(data)
        except Exception:
            # Diagnostics must never skip or consume delivery of the original input.
            logging.debug('Top-edge trace snapshot unavailable', exc_info=True)

    def report_trace(self, data):
        if not self.closed:
            from ngs.diagnostics import event
            event('input.edge_trace', **data)

    def trace_native(self, hwnd, message, wparam, lparam):
        if not self.tracing(): return
        window = self.handles.get(hwnd) or self.backings.get(hwnd)
        if window is None: return
        try:
            ratio = window.devicePixelRatio()
            stage, identity, flags, point, timestamp = '', 0, 0, w.POINT(), 0
            if message in (0x245,0x246,0x247,0x24c):
                identity = wparam & 0xffff
                info = _PointerInfo()
                if self.user.GetPointerInfo(identity, ctypes.byref(info)) and info.kind == 2:
                    point, flags, timestamp = info.pixel, info.flags, info.time
                    self.user.ScreenToClient(hwnd, ctypes.byref(point))
                    stage = {0x245:'pointer_move',0x246:'pointer_down',0x247:'pointer_up',0x24c:'pointer_cancel'}[message]
            elif message in (0x200,0x201,0x202) and self.user.GetMessageExtraInfo() & 0xffffff80 == 0xff515780:
                point = w.POINT(ctypes.c_short(lparam & 0xffff).value, ctypes.c_short((lparam >> 16) & 0xffff).value)
                stage = {0x200:'mouse_touch_move',0x201:'mouse_touch_down',0x202:'mouse_touch_up'}[message]
                timestamp = self.user.GetMessageTime() & 0xffffffff
            elif message == 0x240:
                key = (hwnd, 0, 'legacy')
                if key not in self.trace_moves:
                    self.trace_moves.add(key)
                    self.trace_input(window,'legacy_touch',flags=wparam & 0xffff, timestamp=self.user.GetMessageTime() & 0xffffffff)
                return
            elif message == 0x6:
                self.trace_input(window,'snapshot',window.width()/2,8,flags=wparam & 0xffff)
                return
            if not stage: return
            key = (hwnd,identity,'mouse' if stage.startswith('mouse') else 'pointer')
            if stage.endswith('_down'):
                self.trace_moves.discard(key)
                if hwnd in self.handles and not 0 <= point.y/ratio <= 160: return
                self.trace_contacts.add(key)
            elif key not in self.trace_contacts: return
            elif stage.endswith('_move'):
                if key in self.trace_moves: return
                self.trace_moves.add(key)
            else:
                self.trace_moves.discard(key); self.trace_contacts.discard(key)
            self.trace_input(window,stage,point.x/ratio,point.y/ratio,identity,flags,timestamp=timestamp)
        except Exception:
            logging.debug('Top-edge native trace unavailable', exc_info=True)

    def trace_qt(self, window, event, decision):
        if not self.tracing(): return
        stage = {QEvent.Type.TouchBegin:'qt_begin',QEvent.Type.TouchUpdate:'qt_move',
                 QEvent.Type.TouchEnd:'qt_end',QEvent.Type.TouchCancel:'qt_cancel'}.get(event.type())
        if not stage: return
        if stage == 'qt_cancel':
            self.trace_input(window,stage,decision=decision)
            return
        for point in event.points():
            if point.state() == QEventPoint.State.Stationary: continue
            key = (int(window.winId()),point.id(),'qt')
            if point.state() == QEventPoint.State.Pressed:
                self.trace_contacts.add(key)
            if key not in self.trace_contacts: continue
            if stage == 'qt_move':
                if key in self.trace_moves: continue
                self.trace_moves.add(key)
            else: self.trace_moves.discard(key)
            self.trace_input(window,stage,point.position().x(),point.position().y(),point.id(),point.state().value,decision,timestamp=event.timestamp())
            if point.state() == QEventPoint.State.Released: self.trace_contacts.discard(key)

    def report_input(self, source, flags, contacts, enabled):
        if self.closed: return
        logging.info('Top-edge input: source=%s flags=0x%x contacts=%s enabled=%s', source, flags, contacts, enabled)

    def trace_scroll(self, window, grid):
        if not self.tracing(): return
        dragging = grid.property('dragging')
        self.trace_input(window, 'grid_drag' if dragging else 'grid_end', grid.property('contentX'), grid.property('contentY'))
        if dragging:
            once = Qt.ConnectionType(Qt.ConnectionType.QueuedConnection.value | Qt.ConnectionType.SingleShotConnection.value)
            window.frameSwapped.connect(lambda: self.trace_input(window, 'grid_frame'), once)

    def attach_backing(self, window):
        if window is None: return
        grid = window.findChild(QObject, 'gameGrid') if isinstance(window, QObject) else None
        if grid is not None and not any(item is grid for item, callback in self.scroll_links):
            callback = lambda: self.trace_scroll(window, grid)
            grid.draggingChanged.connect(callback)
            self.scroll_links.append((grid, callback))
        def sync():
            from shiboken6 import isValid
            if self.closed or not isValid(window): return
            for h, current in list(self.backings.items()):
                if current is window:
                    disable_fullscreen_edge_gesture(h, False)
                    self.backing_policy.pop(h, None)
                    self.unsubclass_window(h)
                    window.removeEventFilter(self)
                    self.gestures.pop(h, None)
                    self.backings.pop(h, None)
            if window.isVisible():
                hwnd = int(window.winId())
                self.backings[hwnd] = window
                window.installEventFilter(self)
                self.subclass_window(hwnd)
                disabled = bool(self.controller.edgeSwipeEnabled and not self.controller.sleeping)
                if disable_fullscreen_edge_gesture(hwnd, disabled): self.backing_policy[hwnd] = disabled
                if self.tracing(): self.trace_input(window,'snapshot',window.width()/2,8)
        window._ngs_edge_backing = sync
        window.visibleChanged.connect(sync)
        sync()

    def update_enabled(self):
        if self.closed: return
        disabled = bool(self.controller.edgeSwipeEnabled and not self.controller.sleeping)
        for hwnd in self.backings:
            if self.backing_policy.get(hwnd) != disabled and disable_fullscreen_edge_gesture(hwnd, disabled):
                self.backing_policy[hwnd] = disabled
        if not disabled:
            self.gestures.clear()
            self.controller.cancelEdgeTouch()

    def _point_inside_status(self, x, y):
        sw = getattr(self, 'status_window', None)
        if sw is None: return True
        try:
            from shiboken6 import isValid
            if not isValid(sw) or not sw.isVisible(): return True
            rect = w.RECT()
            if self.user.GetWindowRect(int(sw.winId()), ctypes.byref(rect)):
                return rect.left <= x < rect.right and rect.top <= y < rect.bottom
        except Exception:
            pass
        return True

    def add(self, screen):
        if self.closed: return
        window = self.component.createWithInitialProperties(dict(controller=self.controller))
        if window is None: raise OSError(str(self.component.errors()))
        from PySide6.QtQml import QQmlEngine
        from PySide6.QtQuick import QQuickGraphicsConfiguration
        QQmlEngine.setObjectOwnership(window, QQmlEngine.ObjectOwnership.CppOwnership)
        window.setPersistentSceneGraph(False)
        window.setPersistentGraphics(False)
        graphics = QQuickGraphicsConfiguration()
        graphics.setAutomaticPipelineCache(False)
        window.setGraphicsConfiguration(graphics)
        window.setScreen(screen)
        window._ngs_screen = screen
        self.windows.append(window)
        def on_visible(): self.visibility(window)
        window._ngs_on_visible = on_visible
        window.visibleChanged.connect(on_visible)
        self.visibility(window)

    def visibility(self, window):
        for handle, current in list(self.handles.items()):
            if current is window:
                if QGuiApplication.platformName() == 'windows': self.user.UnregisterTouchWindow(handle)
                self.unsubclass_window(handle)
                self.handles.pop(handle)
                self.gestures.pop(handle, None)
        if window.isVisible():
            handle = int(window.winId())
            self.handles[handle] = window
            self.subclass_window(handle)
            disable_fullscreen_edge_gesture(handle)
            enable_dwm_alpha_composition(handle, force=True)
            if QGuiApplication.platformName() == 'windows':
                # Native WM_TOUCH needs registration; do not wait for palm/gesture recognition.
                registered = self.user.RegisterTouchWindow(handle, 3)
                logging.info('Top-edge touch registration: ready=%s flags=3', bool(registered))
                if not registered: logging.warning('Cannot register top-edge touch: %s', ctypes.get_last_error())
                try:
                    from ngs.process import set_topmost
                    set_topmost(handle, True)
                except OSError:
                    pass
            logging.info('Top-edge target visible: screen=%s height_dip=%s dpr=%s',
                         window.screen().name(), window.height(), window.devicePixelRatio())
            if self.tracing(): self.trace_input(window,'snapshot',window.width()/2,8)
        self.changed()

    def remove(self, screen):
        for window in self.windows[:]:
            if window._ngs_screen is screen:
                self.windows.remove(window); window.hide(); window.deleteLater()
        self.changed()

    def request(self, screen):
        if not self.closed and self.controller.edgeEnabled:
            self.dispatch.observed.emit('recognized', 0, 1, True)
            self.controller.request(screen)

    def contact(self, hwnd, identity, x, y, down):
        window = self.handles.get(hwnd)
        gesture = self.gestures.get(hwnd)
        active = gesture is not None and identity in gesture.contacts
        if window is None or (not self.controller.edgeEnabled and (not active or not getattr(self.controller, 'enabled', True) or getattr(self.controller, 'remoteHost', False) or getattr(self.controller, 'sleeping', False))):
            self.controller.setEdgeDrag('', 0.0)
            return
        screen_name = window.screen().name()
        ratio = window.devicePixelRatio()
        point = w.POINT(round(x), round(y))
        if not self.user.ScreenToClient(hwnd, ctypes.byref(point)):
            self.gestures.pop(hwnd, None)
            self.controller.setEdgeDrag(screen_name, 0.0)
            return
        gesture = self.gestures.setdefault(hwnd, EdgeGesture())
        first = down and identity not in gesture.contacts
        now, area, px, py = time.monotonic(), (0, 0, window.width(), window.height(), .65), point.x / ratio, point.y / ratio
        recognized = gesture.feed(identity, min(max(px, 0.), window.width() - 1.), min(max(py, 0.), 4.), True, area, now) if first else gesture.feed(identity, px, py, down, area, now)
        if first and py > 4.: recognized = gesture.feed(identity, px, py, down, area, now) or recognized
        if first:
            self.controller.edgeContact.emit(screen_name)
            logging.info('Top-edge native contact started: accepted=%s screen=%s', bool(gesture.start or recognized), screen_name)
        if down and gesture.drag_start and gesture.drag_start[0] == identity:
            sy, dp = gesture.drag_start[2], getattr(self.controller, 'dragProgress', 0.0)
            if py - sy >= 4.0 * 0.65 or (isinstance(dp, (int, float)) and dp > 0):
                self.controller.setEdgeDrag(screen_name, max(0.0, py / 96.0))
        elif not down:
            if getattr(self.controller, 'shown', False) is True and getattr(self.controller, 'dragProgress', 0.0) < 0.45 and time.monotonic() - getattr(self.controller, 'last_request', 0.0) > 0.25:
                self.dispatch.dismissed.emit()
            self.controller.setEdgeDrag(screen_name, 0.0)
        if recognized:
            logging.info('Top-edge swipe recognized on %s', screen_name)
            self.dispatch.recognized.emit(screen_name)

    def pointer_samples(self, identity, info):
        count = ctypes.c_uint32(min(max(int(getattr(info, 'history', 0) or 0), 0), 64))
        if count.value > 1 and hasattr(self.user, 'GetPointerInfoHistory'):
            items = (_PointerInfo * count.value)()
            try:
                if self.user.GetPointerInfoHistory(identity, ctypes.byref(count), items) and count.value > 0:
                    return [(items[i].pixel.x, items[i].pixel.y) for i in range(count.value - 1, -1, -1)]
            except Exception:
                pass
        return [(info.pixel.x, info.pixel.y)]

    def eventFilter(self, window, event):
        handled, decision = self.filter_backing_touch(window, event)
        if decision:
            try: self.trace_qt(window, event, decision)
            except Exception: logging.debug('Top-edge Qt trace unavailable', exc_info=True)
        return handled

    def filter_backing_touch(self, window, event):
        kind = event.type()
        if self.closed or kind not in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return False, ''
        hwnd = next((h for h, backing in self.backings.items() if backing is window), None)
        if hwnd is None: return False, 'unattached'
        gesture = self.gestures.get(hwnd)
        active = bool(gesture and gesture.contacts)
        if kind == QEvent.Type.TouchCancel:
            self.gestures.pop(hwnd, None)
            if active:
                self.controller.setEdgeDrag('', 0.0)
                self.dispatch.observed.emit('backing_cancel', 0, 0, bool(self.controller.edgeEnabled))
                event.accept()
            return active, 'cancel'
        if event.pointingDevice().type() != QInputDevice.DeviceType.TouchScreen: return False, 'device'
        if not self.controller.edgeSwipeEnabled or self.controller.sleeping or (self.controller.shown and not active): return False, 'disabled_or_shown'
        screen = window.screen()
        if screen is None: return False, 'no_screen'
        geom = screen.geometry()
        width = min(480, geom.width())
        left, top = geom.x() + (geom.width()-width)/2, geom.y()
        points = event.points()
        if not active:
            if not (self.controller.edgeEnabled or self.controller.remoteHost): return False, 'disabled'
            first = next((p for p in points if p.state() == QEventPoint.State.Pressed), None)
            if first is None: return False, 'no_press'
            pos = first.globalPosition()
            if not (left <= pos.x() < left+width and top <= pos.y() < top+32): return False, 'outside'
            gesture = self.gestures[hwnd] = EdgeGesture()
        # Own the touch before QML can synthesize a mouse press on the page below.
        for point in points:
            state = point.state()
            if state == QEventPoint.State.Stationary: continue
            down = state != QEventPoint.State.Released
            pos = point.globalPosition()
            first = down and point.id() not in gesture.contacts
            recognized = gesture.feed(point.id(), pos.x(), pos.y(), down, (left, top, width, 32, 1), time.monotonic())
            if first: self.controller.edgeContact.emit(screen.name())
            if first or not down:
                self.dispatch.observed.emit('backing_down' if down else 'backing_up', state.value, len(gesture.contacts), bool(self.controller.edgeEnabled))
            if down and gesture.drag_start and gesture.drag_start[0] == point.id():
                if pos.y()-gesture.drag_start[2] >= 4 or self.controller.dragProgress > 0:
                    self.controller.setEdgeDrag(screen.name(), max(0.0, (pos.y()-top)/96.0))
            elif not down and not gesture.contacts:
                self.controller.setEdgeDrag(screen.name(), 0.0)
            if recognized: self.controller.request(screen.name())
        if not gesture.contacts: self.gestures.pop(hwnd, None)
        event.accept()
        return True, 'owned'

    def backing_message(self, hwnd, message, wparam):
        identity = wparam & 0xffff
        if message == 0x246 and getattr(self.controller, 'shown', False) is True and not getattr(self.controller, 'closing', False):
            info = _PointerInfo()
            if self.user.GetPointerInfo(identity, ctypes.byref(info)) and info.kind == 2:
                if not self._point_inside_status(info.pixel.x, info.pixel.y):
                    self.dispatch.dismissed.emit()
        return False, 0

    def message(self, hwnd, message, wparam, lparam):
        if message not in (0x84, 0x21, 0x240, 0x245, 0x246, 0x247, 0x24b, 0x24c, 0x2cc):
            return False, 0
        is_handle = hwnd in self.handles
        if not is_handle and hwnd not in getattr(self, 'backings', ()):
            return False, 0
        if message == 0x2cc: return True, 0x10001  # Do not delay raw touch for Windows hold/flick recognition.
        if not is_handle:
            return self.backing_message(hwnd, message, wparam)
        if message == 0x84:
            # This is a visible control, not an invisible strip over other window titles.
            return True, 1
        if message in (0x21, 0x24b):
            self.dispatch.observed.emit('activate', message, 1, bool(self.controller.edgeEnabled))
            return True, 3  # Accept touch without activating the strip.
        if message == 0x24c:
            gesture = self.gestures.get(hwnd)
            if gesture is None or (wparam & 0xffff) not in gesture.contacts: return False, 0
            self.dispatch.observed.emit('capture_lost', 0, len(gesture.contacts) if gesture else 0, bool(self.controller.edgeEnabled))
            self.gestures.pop(hwnd, None)
            if not getattr(self.controller, 'shown', False):
                self.controller.setEdgeDrag('', 0.0)
            return True, 0
        if message == 0x240:
            count = wparam & 0xffff
            try:
                if not 0 < count <= 256:
                    self.gestures.pop(hwnd, None)
                else:
                    contacts = (_TouchInput * count)()
                    if self.user.GetTouchInputInfo(lparam, count, contacts, ctypes.sizeof(_TouchInput)):
                        for contact in contacts:
                            if contact.flags & (2 | 4):
                                self.dispatch.observed.emit('touch_palm' if contact.flags & 0x80 else 'touch_down' if contact.flags & 2 else 'touch_up', int(contact.flags), count, bool(self.controller.edgeEnabled))
                            if contact.flags & 0x40: continue  # Pen input must not alter a finger gesture.
                            if contact.flags & 0x80:
                                self.gestures.pop(hwnd, None)
                                continue
                            self.contact(hwnd, contact.identity, contact.x / 100, contact.y / 100, not contact.flags & 4)
                    else:
                        self.gestures.pop(hwnd, None)
                        self.dispatch.observed.emit('touch_error', ctypes.get_last_error(), count, bool(self.controller.edgeEnabled))
                        logging.warning('Cannot read top-edge touch: %s', ctypes.get_last_error())
            finally:
                self.user.CloseTouchInputHandle(lparam)
            return True, 0
        if message in (0x245, 0x246, 0x247):
            identity = wparam & 0xffff
            info = _PointerInfo()
            if not self.user.GetPointerInfo(identity, ctypes.byref(info)):
                if message in (0x246, 0x247):
                    self.dispatch.observed.emit('pointer_error', ctypes.get_last_error(), 1, bool(self.controller.edgeEnabled))
                gesture = self.gestures.get(hwnd)
                if gesture and identity in gesture.contacts: self.gestures.pop(hwnd, None)
                return False, 0
            if info.kind != 2: return False, 0
            if message in (0x246, 0x247) or info.flags & 0x8000:
                self.dispatch.observed.emit('pointer_cancel' if info.flags & 0x8000 else 'pointer_down' if message == 0x246 else 'pointer_up', int(info.flags), 1, bool(self.controller.edgeEnabled))
            if info.flags & 0x8000:
                self.gestures.pop(hwnd, None)
                self.controller.setEdgeDrag('', 0.0)
            elif message != 0x245 or info.flags & 4:
                samples = self.pointer_samples(identity, info)
                for index, (px, py) in enumerate(samples):
                    self.contact(hwnd, info.identity, px, py, message != 0x247 or index < len(samples) - 1)
            return True, 0
        return False, 0

    def close(self):
        if self.closed: return
        self.closed = True
        app = QGuiApplication.instance()
        for hwnd in list(self.subclasses): self.unsubclass_window(hwnd)
        windows, self.windows = self.windows, []
        if app and QGuiApplication.platformName() == 'windows':
            for handle in self.handles: self.user.UnregisterTouchWindow(handle)
        from shiboken6 import isValid
        for grid, callback in self.scroll_links:
            if isValid(grid): grid.draggingChanged.disconnect(callback)
        self.scroll_links.clear()
        for hwnd, window in self.backings.items():
            if isValid(window):
                disable_fullscreen_edge_gesture(hwnd, False)
                window.removeEventFilter(self)
        self.backing_policy.clear()
        self.handles.clear(); self.gestures.clear(); self.backings.clear()
        for window in windows:
            if isValid(window):
                window.hide(); window.destroy()
