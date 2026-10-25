"""Text-focus notifications only: never read field names, values, or keystrokes."""
import ctypes
import logging
import os
import queue
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication, QInputMethodQueryEvent, QWindow
from ngs.system_status import _call, _guid, _pointer


class _Variant(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ushort), ('reserved', ctypes.c_ushort * 3), ('data', ctypes.c_ubyte * 16)]


_handlers = []  # shortcut: keep callback objects until exit; COM may call after removal during shutdown.


class _FocusHandler:
    def __init__(self, changed):
        self.closed, self.references = False, 1
        p, hr = ctypes.c_void_p, ctypes.c_long
        query = ctypes.WINFUNCTYPE(hr, p, p, ctypes.POINTER(p))
        count = ctypes.WINFUNCTYPE(wintypes.ULONG, p)
        focus = ctypes.WINFUNCTYPE(hr, p, p)
        def interface(this, identity, result):
            if not result: return -2147467261
            result[0] = None
            if identity and ctypes.string_at(identity, 16) in (bytes(_guid('00000000-0000-0000-c000-000000000046')), bytes(_guid('c270f6b5-5c69-4290-9745-7a7f97169468'))):
                result[0] = self.pointer.value; self.references += 1; return 0
            return -2147467262
        def add(this): self.references += 1; return self.references
        def release(this): self.references = max(0, self.references - 1); return self.references
        def notify(this, sender):
            if not self.closed:
                try: changed()
                except RuntimeError: pass
            return 0
        self.callbacks = [query(interface), count(add), count(release), focus(notify)]
        self.table = (p * 4)(*[ctypes.cast(fn, p).value for fn in self.callbacks])
        class Interface(ctypes.Structure): _fields_ = [('table', ctypes.POINTER(p))]
        self.instance = Interface(self.table)
        self.pointer = p(ctypes.addressof(self.instance))
        _handlers.append(self)


def focused_edit(automation, ole, own_pid=None, cancel=None, point=None):
    with _pointer() as element:
        if point is None:
            _call(automation, 8, (ctypes.POINTER(ctypes.c_void_p),), (ctypes.byref(element),))
        else:
            _call(automation, 7, (wintypes.POINT, ctypes.POINTER(ctypes.c_void_p)),
                  (wintypes.POINT(*point), ctypes.byref(element)))
        if not element: return False, None
        def value(identity):
            if cancel is not None and cancel.is_set(): raise OSError('Focus query stopped')
            variant = _Variant()
            try:
                _call(element, 10, (ctypes.c_int, ctypes.POINTER(_Variant)), (identity, ctypes.byref(variant)))
                if variant.type == 3: return ctypes.c_int.from_buffer(variant.data).value
                if variant.type == 11: return bool(ctypes.c_short.from_buffer(variant.data).value)
                return None
            finally: ole.VariantClear(ctypes.byref(variant))
        if own_pid is not None and value(30002) == own_pid: return None, None
        # Inspect edit metadata, including password fields, without ever requesting Value/Text.
        control, enabled, focused, readonly = value(30003), value(30010), value(30008), value(30046)
        editable = enabled is True and focused is True and readonly is not True and (control == 50004 or value(30149) is True)
        identity = ctypes.c_void_p()
        if cancel is not None and cancel.is_set(): return None, None
        _call(element, 4, (ctypes.POINTER(ctypes.c_void_p),), (ctypes.byref(identity),))
        try:
            lower, upper = ctypes.c_long(), ctypes.c_long()
            if not identity or ole.SafeArrayGetLBound(identity, 1, ctypes.byref(lower)) < 0 or ole.SafeArrayGetUBound(identity, 1, ctypes.byref(upper)) < 0:
                return False, None
            if not 0 <= upper.value - lower.value < 64: return False, None
            values = ctypes.c_void_p()
            if ole.SafeArrayAccessData(identity, ctypes.byref(values)) < 0: return False, None
            try: return editable, tuple(ctypes.cast(values, ctypes.POINTER(ctypes.c_int))[i] for i in range(upper.value - lower.value + 1))
            finally: ole.SafeArrayUnaccessData(identity)
        finally:
            if identity: ole.SafeArrayDestroy(identity)


class TouchTap:
    def __init__(self, distance, duration):
        self.distance, self.duration, self.start = distance, duration, None

    def feed(self, message, x, y, now):
        if message == 0x201: self.start = (x, y, now)
        if self.start is None: return False
        sx, sy, started = self.start
        if abs(x - sx) > self.distance or abs(y - sy) > self.distance or now - started > self.duration:
            self.start = None
        if message != 0x202: return False
        accepted = self.start is not None
        self.start = None
        return accepted


class WindowsTouchTaps:
    def __init__(self, notify):
        from ngs.edge_swipe import _MouseInput
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        self.user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback, wintypes.HINSTANCE, wintypes.DWORD]
        self.user.SetWindowsHookExW.restype = wintypes.HHOOK
        self.user.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self.user.CallNextHookEx.restype = ctypes.c_ssize_t
        self.user.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self.user.WindowFromPoint.argtypes = [wintypes.POINT]; self.user.WindowFromPoint.restype = wintypes.HWND
        self.user.GetDpiForWindow.argtypes = [wintypes.HWND]; self.user.GetDpiForWindow.restype = wintypes.UINT
        hints = QGuiApplication.styleHints()
        self.gesture = TouchTap(hints.startDragDistance(), hints.mousePressAndHoldInterval() / 1000)
        def receive(code, message, data):
            try:
                if code >= 0 and message in (0x200, 0x201, 0x202):
                    packet = _MouseInput.from_address(data)
                    if packet.extra & 0xffffff80 == 0xff515780:
                        if message == 0x201:
                            hwnd = self.user.WindowFromPoint(packet.point)
                            self.gesture.distance = hints.startDragDistance() * (self.user.GetDpiForWindow(hwnd) or 96) / 96
                        if self.gesture.feed(message, packet.point.x, packet.point.y, time.monotonic()):
                            notify((packet.point.x, packet.point.y))
                    elif message in (0x201, 0x202): self.gesture.start = None
            except Exception: logging.debug('Touch tap observer unavailable', exc_info=True)
            return self.user.CallNextHookEx(self.hook, code, message, data)
        self.callback = callback(receive)
        self.hook = self.user.SetWindowsHookExW(14, self.callback, None, 0)
        if not self.hook: raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.hook: self.user.UnhookWindowsHookEx(self.hook); self.hook = None
        self.gesture.start = None


class AutoKeyboard(QObject):
    result = Signal(int, object, object)
    focusChanged = Signal()
    tapped = Signal(object)

    def __init__(self, keyboard, power, native=True):
        super().__init__(keyboard)
        self.keyboard, self.power = keyboard, power
        self.auto_shown = False
        self.internal = False
        self.serial = 0
        self.tap_serial = None
        self.touch_listener = None
        self.qt_gesture = TouchTap(QGuiApplication.styleHints().startDragDistance(), QGuiApplication.styleHints().mousePressAndHoldInterval() / 1000)
        self.stopped = threading.Event()
        self.requests = queue.Queue(maxsize=1)
        self.thread = None
        self.listener = None
        self.native, self.enabled = native, None
        self.result.connect(self.accept)
        self.focusChanged.connect(self.request, Qt.ConnectionType.QueuedConnection)
        self.tapped.connect(self.request, Qt.ConnectionType.QueuedConnection)
        keyboard.visibilityRequested.connect(self.visibility_requested)
        keyboard.changed.connect(self.update)
        power.changed.connect(self.update); power.resumed.connect(self.update)
        self.app = QGuiApplication.instance()
        if native:
            self.app.focusObjectChanged.connect(self.qt_focus)
            self.app.installEventFilter(self)
        self.update()

    def update(self):
        enabled = self.keyboard.enabled and self.keyboard.options['auto_show'] and not self.power.sleeping and not self.stopped.is_set() and not getattr(self.keyboard.bridge, 'gameFrozen', False)
        if enabled != self.enabled:
            self.enabled = enabled
            if self.native:
                if self.touch_listener: self.touch_listener.close(); self.touch_listener = None
                if enabled:
                    try: self.touch_listener = WindowsTouchTaps(self.tapped.emit)
                    except OSError: logging.debug('Touch tap observer unavailable', exc_info=True)
                if self.thread is not None: self.post(-1)
                elif enabled:
                    self.post(-1)
                    self.thread = threading.Thread(target=self.work, name='NGS edit focus', daemon=True)
                    self.thread.start()
        if not enabled:
            self.serial += 1
            self.tap_serial = None; self.qt_gesture.start = None

    def visibility_requested(self, visible):
        if not self.internal:
            self.serial += 1
            self.tap_serial = None
            if self.auto_shown:
                self.auto_shown = False
                if self.thread is not None: self.post(-1)

    def accept(self, serial, editable, identity):
        if serial != self.serial or self.stopped.is_set() or self.power.sleeping or not self.enabled or not self.keyboard.options['auto_show']:
            return
        if editable is None:
            if self.tap_serial == serial: self.tap_serial = None
            return
        tapped = self.tap_serial == serial
        if tapped:
            self.tap_serial = None
        if tapped and editable and identity and not self.keyboard.visible:
            self.internal = True
            try:
                self.app.inputMethod().hide()
                self.keyboard.show()
                self.auto_shown = True
                if self.thread is not None: self.post(-1)
            finally: self.internal = False
        elif not tapped and not editable and self.auto_shown and self.keyboard.visible:
            self.internal = True
            try:
                self.keyboard.hide(); self.auto_shown = False
                if self.thread is not None: self.post(-1)
            finally: self.internal = False

    def qt_focus(self, obj):
        if not self.enabled or self.tap_serial is not None or not self.auto_shown or not self.keyboard.visible: return
        self.serial += 1
        if obj is None: return
        event = QInputMethodQueryEvent(Qt.InputMethodQuery.ImEnabled)
        self.app.sendEvent(obj, event)
        self.accept(self.serial, bool(event.value(Qt.InputMethodQuery.ImEnabled)) and obj.property('readOnly') is not True, ('qt', id(obj)))

    def qt_tap(self, window, point):
        if not self.enabled: return
        obj = self.app.focusObject()
        if obj is None or not hasattr(obj, 'window') or obj.window() is not window: return
        if not obj.isVisible() or not obj.isEnabled(): return
        if not obj.contains(obj.mapFromScene(point)): return
        event = QInputMethodQueryEvent(Qt.InputMethodQuery.ImEnabled)
        self.app.sendEvent(obj, event)
        if not event.value(Qt.InputMethodQuery.ImEnabled) or obj.property('readOnly') is True: return
        self.serial += 1; self.tap_serial = self.serial
        self.accept(self.serial, True, ('qt', id(obj)))

    def eventFilter(self, watched, event):
        kind = event.type()
        if self.enabled and isinstance(watched, QWindow) and kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            points = event.points()
            if kind == QEvent.Type.TouchCancel or len(points) != 1:
                self.qt_gesture.start = None
            else:
                point = points[0].position()
                message = 0x201 if kind == QEvent.Type.TouchBegin else 0x202 if kind == QEvent.Type.TouchEnd else 0x200
                if self.qt_gesture.feed(message, point.x(), point.y(), time.monotonic()): self.qt_tap(watched, point)
        return False

    def request(self, point=None):
        if self.stopped.is_set() or self.power.sleeping or not self.enabled or not self.keyboard.options['auto_show']: return
        # Focus changes can only hide a keyboard opened by a tap. Avoid an expensive
        # cross-process UIA query when it is hidden or was opened manually.
        if point is None and (self.tap_serial is not None or not self.auto_shown or not self.keyboard.visible): return
        self.serial += 1
        if point is not None: self.tap_serial = self.serial
        self.post((self.serial, point))

    def post(self, value):
        try: self.requests.get_nowait()
        except queue.Empty: pass
        self.requests.put_nowait(value)

    def work(self):
        ole = ctypes.WinDLL('ole32'); arrays = ctypes.WinDLL('oleaut32')
        p = ctypes.c_void_p
        ole.CoInitializeEx.argtypes = [p, ctypes.c_uint]
        ole.CoCreateInstance.argtypes = [p, p, ctypes.c_uint, p, ctypes.POINTER(p)]
        arrays.VariantClear.argtypes = [ctypes.POINTER(_Variant)]
        arrays.SafeArrayGetLBound.argtypes = arrays.SafeArrayGetUBound.argtypes = [p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
        arrays.SafeArrayAccessData.argtypes = [p, ctypes.POINTER(p)]
        arrays.SafeArrayUnaccessData.argtypes = arrays.SafeArrayDestroy.argtypes = [p]
        initialized = ole.CoInitializeEx(None, 0) >= 0
        try:
            if not initialized: return
            with _pointer() as automation:
                if ole.CoCreateInstance(_guid('e22ad333-b25f-460c-83d0-0581107395c9'), None, 1,
                                        _guid('34723aff-0c9d-49d0-9896-7ab52df8cd8a'), ctypes.byref(automation)) < 0: return
                _call(automation, 59, (wintypes.BOOL,), (False,))  # UIAutomation2.AutoSetFocus
                _call(automation, 61, (wintypes.DWORD,), (500,))   # ConnectionTimeout
                _call(automation, 63, (wintypes.DWORD,), (500,))   # TransactionTimeout
                handler = _FocusHandler(self.focusChanged.emit)
                self.listener = handler
                listening = False
                try:
                    while not self.stopped.is_set():
                        want_listen = bool(self.enabled and self.auto_shown and self.keyboard.visible)
                        if want_listen and not listening:
                            _call(automation, 39, (p,p), (None,handler.pointer)); listening = True
                        elif not want_listen and listening:
                            _call(automation, 40, (p,), (handler.pointer,)); listening = False
                        request = self.requests.get()
                        if request is None: return
                        if request == -1: continue
                        serial, point = request
                        if serial != self.serial or not self.enabled: continue
                        try: editable, identity = focused_edit(automation, arrays, os.getpid(), self.stopped, point)
                        except OSError: editable, identity = None, None
                        if not self.stopped.is_set(): self.result.emit(serial, editable, identity)
                finally:
                    handler.closed = True
                    if listening: _call(automation, 40, (p,), (handler.pointer,))
                    _call(handler.pointer, 2)
        except (OSError, RuntimeError): logging.debug('Keyboard focus provider unavailable', exc_info=True)
        finally:
            if initialized: ole.CoUninitialize()

    def close(self):
        if self.stopped.is_set(): return
        self.stopped.set(); self.serial += 1
        self.tap_serial = None; self.qt_gesture.start = None
        if self.touch_listener: self.touch_listener.close(); self.touch_listener = None
        if self.native:
            self.app.removeEventFilter(self)
            self.app.focusObjectChanged.disconnect(self.qt_focus)
        if self.listener: self.listener.closed = True
        self.post(None)
        if self.thread: self.thread.join(1.5)
