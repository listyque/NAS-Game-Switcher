"""Consume touch pointers before Windows promotes them to absolute mouse input."""
import ctypes
import time
from ctypes import wintypes
from PySide6.QtCore import QAbstractNativeEventFilter


class _PointerInfo(ctypes.Structure):
    _fields_ = [('kind', ctypes.c_uint32), ('identity', ctypes.c_uint32), ('frame', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('device', wintypes.HANDLE), ('target', wintypes.HWND),
                ('pixel', wintypes.POINT), ('himetric', wintypes.POINT), ('raw', wintypes.POINT),
                ('raw_himetric', wintypes.POINT), ('time', wintypes.DWORD), ('history', ctypes.c_uint32),
                ('input_data', ctypes.c_int32), ('keys', wintypes.DWORD), ('performance', ctypes.c_uint64), ('change', ctypes.c_uint32)]


class _TouchInput(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG), ('source', wintypes.HANDLE),
                ('identity', wintypes.DWORD), ('flags', wintypes.DWORD), ('mask', wintypes.DWORD),
                ('time', wintypes.DWORD), ('extra', wintypes.WPARAM), ('width', wintypes.DWORD), ('height', wintypes.DWORD)]


class NativeTouchpads(QAbstractNativeEventFilter):
    def __init__(self, keyboard, windows):
        super().__init__()
        self.keyboard, self.windows = keyboard, windows
        self.contacts, self.gestures = {}, {}
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.GetPointerType.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        self.user.GetPointerType.restype = wintypes.BOOL
        self.user.GetPointerInfo.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PointerInfo)]
        self.user.GetPointerInfo.restype = wintypes.BOOL
        self.user.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user.ScreenToClient.restype = wintypes.BOOL
        self.user.GetTouchInputInfo.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.POINTER(_TouchInput), ctypes.c_int]
        self.user.GetTouchInputInfo.restype = wintypes.BOOL
        self.user.CloseTouchInputHandle.argtypes = [wintypes.HANDLE]
        self.user.GetMessageExtraInfo.restype = wintypes.LPARAM

    def clear(self):
        self.keyboard.pointer_timer.stop()
        self.contacts.clear(); self.gestures.clear()

    def nativeEventFilter(self, kind, message):
        event = wintypes.MSG.from_address(int(message))
        if event.message not in (0x240,0x245,0x246,0x247,0x24b,0x24c,0x24e,0x24f) and not 0x200 <= event.message <= 0x20e:
            return False, 0
        from shiboken6 import isValid
        window = next((w for w in self.windows if isValid(w) and w.isVisible() and int(w.winId()) == event.hWnd), None)
        if window is None or not self.keyboard.touchpadMode: return False, 0
        if event.message == 0x24b:
            return True, 3  # PA_NOACTIVATE keeps the game focused while accepting touch.
        if event.message == 0x240:
            count = event.wParam & 0xffff
            if not 0 < count <= 256: return False, 0
            touches = (_TouchInput * count)()
            try:
                if self.user.GetTouchInputInfo(event.lParam, count, touches, ctypes.sizeof(_TouchInput)):
                    for touch in touches:
                        if touch.flags & 0x80:
                            self.cancel(event.hWnd); continue
                        point = wintypes.POINT(round(touch.x / 100), round(touch.y / 100))
                        if not self.user.ScreenToClient(event.hWnd, ctypes.byref(point)):
                            self.cancel(event.hWnd); continue
                        action = 0x246 if touch.flags & 2 else 0x247 if touch.flags & 4 else 0x245
                        ratio = window.devicePixelRatio()
                        self.contact(event.hWnd, action, touch.identity, point.x / ratio, point.y / ratio, window)
                else:
                    self.cancel(event.hWnd)
            finally:
                self.user.CloseTouchInputHandle(event.lParam)
            self.keyboard.pointer_timer.start(0)
            return True, 0
        # Legacy drivers can deliver touch-promoted mouse packets alongside WM_TOUCH.
        if 0x200 <= event.message <= 0x20e and self.user.GetMessageExtraInfo() & 0xffffff80 == 0xff515780:
            self.keyboard.pointer_timer.start(0)
            return True, 0
        return self.message(event.hWnd, event.message, event.wParam), 0

    def message(self, hwnd, message, wparam):
        if message not in (0x245, 0x246, 0x247, 0x24c, 0x24e, 0x24f): return False
        from shiboken6 import isValid
        window = next((window for window in self.windows if isValid(window) and window.isVisible() and int(window.winId()) == hwnd), None)
        if window is None or not self.keyboard.touchpadMode: return False
        identity = wparam & 0xffff
        if message == 0x24c:
            owned = identity in self.contacts
            self.cancel(hwnd)
            if owned: self.keyboard.pointer_timer.start(0)
            return owned
        pointer_type = ctypes.c_uint32()
        if not self.user.GetPointerType(identity, ctypes.byref(pointer_type)) or pointer_type.value != 2: return False
        self.keyboard.pointer_timer.start(0)
        info = _PointerInfo()
        if not self.user.GetPointerInfo(identity, ctypes.byref(info)) or info.flags & 0x8000:
            self.cancel(hwnd); return True
        point = wintypes.POINT(info.pixel.x, info.pixel.y)
        if not self.user.ScreenToClient(hwnd, ctypes.byref(point)):
            self.cancel(hwnd); return True
        ratio = window.devicePixelRatio()
        self.contact(hwnd, message, identity, point.x / ratio, point.y / ratio, window)
        return True

    def cancel(self, hwnd):
        self.contacts = {key: value for key, value in self.contacts.items() if value['hwnd'] != hwnd}
        self.gestures.pop(hwnd, None)

    def contact(self, hwnd, message, identity, x, y, window):
        if message == 0x246:
            if len(self.contacts) >= 10: return
            rects = window.property('toolRects')
            if hasattr(rects, 'toVariant'): rects = rects.toVariant()
            tool = next((r['index'] for r in rects if r['x'] <= x < r['x'] + r['width'] and r['y'] <= y < r['y'] + r['height']),
                        -1 if window.property('padTop') <= y < window.property('padBottom') else -2)
            self.contacts[identity] = dict(hwnd=hwnd, x=x, y=y, tool=tool)
            gesture = self.gestures.setdefault(hwnd, dict(started=time.monotonic(), distance=0., multiple=False, cancelled=False))
            local_contacts = [c for c in self.contacts.values() if c['hwnd'] == hwnd]
            gesture['multiple'] |= len(local_contacts) > 1
            # Only one pad contact or two pad contacts have a defined action. A third
            # finger, or a toolbar contact mixed with a pad gesture, must not become
            # an unexpected right click when the last contact is released.
            gesture['cancelled'] |= len(local_contacts) > 2 or len(local_contacts) > 1 and any(c['tool'] != -1 for c in local_contacts)
            return
        contact = self.contacts.get(identity)
        gesture = self.gestures.get(hwnd)
        if not contact or contact['hwnd'] != hwnd or not gesture: return
        dx, dy = x - contact['x'], y - contact['y']
        gesture['distance'] += abs(dx) + abs(dy)
        contact.update(x=x, y=y)
        surfaces = [c for c in self.contacts.values() if c['hwnd'] == hwnd and c['tool'] == -1]
        if not gesture['cancelled'] and message in (0x245, 0x247) and contact['tool'] == -1:
            if len(surfaces) == 1 and not gesture['multiple'] and (dx or dy): self.keyboard.movePointer(dx, dy)
            elif len(surfaces) == 2 and dy: self.keyboard.scrollPointer(dy / 2)
        if message == 0x247:
            self.contacts.pop(identity, None)
            if not any(c['hwnd'] == hwnd for c in self.contacts.values()):
                self.gestures.pop(hwnd, None)
                elapsed = time.monotonic() - gesture['started']
                if not gesture['cancelled'] and gesture['distance'] < 10:
                    if contact['tool'] >= 0 and not gesture['multiple']:
                        if contact['tool'] == 1 and not window.property('rightSide') and elapsed >= .7: self.keyboard.takeScreenshot(True)
                        elif elapsed < .35: self.keyboard.padTool.emit(window.property('rightSide'), contact['tool'])
                    elif contact['tool'] == -1 and elapsed < .35: self.keyboard.clickPointer('right' if gesture['multiple'] else 'left')
