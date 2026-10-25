"""Portable shortcut assignments and the Windows notification-area icon."""
from ngs.i18n import tr
import ctypes
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QEvent, QObject, Property, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from ngs.gamepad import GAMEPAD_BUTTONS, GAMEPAD_MASK
from ngs.virtual_keyboard import NGS_INPUT_EXTRA


ACTIONS = {"desktop": 'Desktop / minimize NGS', "show": 'Show NGS', "keyboard": 'Show / hide keyboard'}
WM_TRAY = 0x8000 + 74
HOLD_SECONDS = .7
MASK_EXTRA = 0x4e47534d


def binding(vk=None, mods=None, buttons=None, hold=False, toggle=False):
    if type(hold) is not bool or type(toggle) is not bool:
        raise ValueError(tr("Invalid press mode."))
    options = dict(**({'hold': True} if hold else {}), **({'toggle': True} if toggle else {}))
    if buttons is not None:
        if vk is not None or mods is not None or type(buttons) is not int or not buttons or buttons & ~GAMEPAD_MASK:
            raise ValueError(tr("Could not recognize gamepad buttons."))
        return dict(buttons=buttons, **options)
    if type(vk) is not int or type(mods) is not int or not 0 <= vk <= 254 or mods & ~15 or vk == 0 and mods != 11:
        raise ValueError(tr("Could not recognize the key."))
    if vk in (16, 17, 18, 91, 92, 123, 160, 161, 162, 163, 164, 165):
        raise ValueError(tr("Choose a key with modifiers; F12 is reserved by Windows."))
    if mods & 8 and vk == 76 or vk == 46 and mods & 3 == 3:
        raise ValueError(tr("Win+L and Ctrl+Alt+Delete are protected by Windows; choose another shortcut."))
    return dict(vk=vk, mods=mods, **options)


def physical(value):
    return {key: item for key, item in value.items() if key not in ('hold', 'toggle')}


def hooked(value, values):
    return 'vk' in value


def label(value):
    if not value:
        return tr("Not assigned")
    if value.get('hold'):
        return label(physical(value)) + tr(" · hold 0.7 s")
    if 'buttons' in value:
        return tr("Gamepad: ") + '+'.join(name for bit, name in GAMEPAD_BUTTONS.items() if value['buttons'] & bit)
    vk = value['vk']
    names = {8: 'Backspace', 9: 'Tab', 13: 'Enter', 27: 'Esc', 32: 'Space', 33: 'PageUp', 34: 'PageDown',
             35: 'End', 36: 'Home', 37: '←', 38: '↑', 39: '→', 40: '↓', 45: 'Insert', 46: 'Delete'}
    key = chr(vk) if 48 <= vk <= 90 else 'F' + str(vk - 111) if 112 <= vk <= 135 else names.get(vk, 'VK %02X' % vk)
    return '+'.join([name for bit, name in ((8, 'Win'), (2, 'Ctrl'), (1, 'Alt'), (4, 'Shift')) if value['mods'] & bit] + ([key] if vk else []))


class _NotifyIcon(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('hwnd', wintypes.HWND), ('id', wintypes.UINT),
                ('flags', wintypes.UINT), ('message', wintypes.UINT), ('icon', wintypes.HICON),
                ('tip', wintypes.WCHAR * 128), ('state', wintypes.DWORD), ('state_mask', wintypes.DWORD),
                ('info', wintypes.WCHAR * 256), ('version', wintypes.UINT), ('title', wintypes.WCHAR * 64),
                ('info_flags', wintypes.DWORD), ('guid', ctypes.c_byte * 16), ('balloon', wintypes.HICON)]


class WindowsDesktop:
    def __init__(self, hwnd, icon):
        self.hwnd = hwnd
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.GetCurrentInputMessageSource.argtypes = [ctypes.POINTER(_InputMessageSource)]
        self.user.GetCurrentInputMessageSource.restype = wintypes.BOOL
        self.user.GetMessageExtraInfo.restype = ctypes.c_ssize_t
        self.shell = ctypes.WinDLL('shell32', use_last_error=True)
        self.user.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user.LoadImageW.restype = wintypes.HANDLE
        self.user.DestroyIcon.argtypes = [wintypes.HICON]
        self.user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        self.shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NotifyIcon)]
        self.taskbar_message = self.user.RegisterWindowMessageW('TaskbarCreated')
        self.icon = self.user.LoadImageW(None, str(icon), 1, 0, 0, 0x50)
        self.notification = _NotifyIcon(size=ctypes.sizeof(_NotifyIcon), hwnd=hwnd, id=1,
                                        flags=1 | 2 | 4, message=WM_TRAY, icon=self.icon, tip='NGS — NAS Game Switcher')
        self.tray = bool(self.icon and self.shell.Shell_NotifyIconW(0, ctypes.byref(self.notification)))
        self.key_hook = None
        self.shortcut_values = {}

    def register(self, identity, value):
        if 'buttons' in value or value.get('mods', 0) & 8:
            return True
        return bool(self.user.RegisterHotKey(self.hwnd, identity, value['mods'] | 0x4000, value['vk']))

    def unregister(self, identity):
        self.user.UnregisterHotKey(self.hwnd, identity)

    def restore_tray(self):
        self.tray = bool(self.icon and self.shell.Shell_NotifyIconW(0, ctypes.byref(self.notification)))

    def configure_shortcuts(self, values, notify):
        try:
            self.apply_shortcuts(values, notify)
        except (ValueError, OSError):
            try:
                self.apply_shortcuts(self.shortcut_values, notify)
            except (ValueError, OSError) as error:
                raise OSError(tr("Could not restore the previous Windows shortcuts.")) from error
            raise
        self.shortcut_values = dict(values)

    def apply_shortcuts(self, values, notify):
        keys = {(item['vk'], item['mods']) for item in values.values() if hooked(item, values)}
        if keys and not self.key_hook:
            self.key_hook = WindowsKeyCapture(notify, shortcuts=keys)
        for identity in range(1, len(ACTIONS) + 1):
            self.unregister(identity)
        reserved = set()
        for identity, key in enumerate(ACTIONS, 1):
            item = values.get(key)
            if not item or 'vk' not in item or item['mods'] & 8:
                continue
            chord = (item['vk'], item['mods'])
            if chord not in reserved:
                if not self.register(identity, item):
                    raise ValueError(tr("Shortcut in use by Windows or another program: ") + label(item))
                reserved.add(chord)
        if keys:
            self.key_hook.shortcuts = keys
        elif self.key_hook:
            self.key_hook.close()
            self.key_hook = None

    def close(self):
        if self.key_hook:
            self.key_hook.close()
            self.key_hook = None
        self.shell.Shell_NotifyIconW(2, ctypes.byref(self.notification))
        if self.icon:
            self.user.DestroyIcon(self.icon)
            self.icon = None


class _InputMessageSource(ctypes.Structure):
    _fields_ = [('device', ctypes.c_int), ('origin', ctypes.c_int)]


class _NativeEvents(QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def nativeEventFilter(self, kind, message):
        event = wintypes.MSG.from_address(int(message))
        return self.owner.native_message(event.hWnd, event.message, event.wParam, event.lParam), 0


class _KeyboardEvent(ctypes.Structure):
    _fields_ = [('vk', wintypes.DWORD), ('scan', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]


class WindowsKeyCapture:
    """Record keys, or intercept only explicitly configured service shortcuts."""
    keyboard_notify = None

    def __init__(self, notify, shortcuts=None, release=None, keyboard_notify=None):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        self.user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
        self.user.SetWindowsHookExW.restype = wintypes.HHOOK
        self.user.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self.user.CallNextHookEx.restype = ctypes.c_ssize_t
        self.user.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = wintypes.SHORT
        from ngs.virtual_keyboard import _Input
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        self.user.SendInput.restype = wintypes.UINT
        self.held = {vk for vk in range(1, 255) if vk not in (16, 17, 18)
                     if self.user.GetAsyncKeyState(vk) & 0x8000}
        self.mask_error = False
        self.notify = notify
        self.keyboard_notify = keyboard_notify
        self.release_notify = release
        self.recording = {}
        self.shortcuts = shortcuts
        self.suppressed = {}
        self.ended = set()
        self.modifier_press = None
        self.modifier_blocked = set()
        self.callback = callback_type(self.process)
        self.hook = self.user.SetWindowsHookExW(13, self.callback, None, 0)
        if not self.hook:
            raise ctypes.WinError(ctypes.get_last_error())

    def process(self, code, message, pointer):
        if code >= 0 and message in (0x100, 0x101, 0x104, 0x105):
            event = _KeyboardEvent.from_address(pointer)
            if event.extra in (MASK_EXTRA, NGS_INPUT_EXTRA):
                return self.user.CallNextHookEx(None, code, message, pointer)
            vk = event.vk
            down = message in (0x100, 0x104)
            repeated = vk in self.held
            if down:
                self.held.add(vk)
            else:
                self.held.discard(vk)
            mods = sum(bit for bit, keys in ((1, {18, 164, 165}), (2, {17, 162, 163}),
                                            (4, {16, 160, 161}), (8, {91, 92})) if self.held & keys)
            # AOKZOE Turbo sends Ctrl+Win+Alt without an ordinary key.
            if vk in (16, 17, 18, 91, 92, 160, 161, 162, 163, 164, 165):
                if not down:
                    now = time.monotonic()
                    for key, required in list(self.suppressed.items()):
                        if key not in self.ended and mods & required != required:
                            self.ended.add(key)
                            self.notify(key, required, False, now)
                    for key, (required, started) in list(self.recording.items()):
                        if mods & required != required:
                            self.recording.pop(key)
                            if self.release_notify:
                                self.release_notify(key, required, now - started)
                if down and not repeated and mods == 11 and (self.shortcuts is None or (0, 11) in self.shortcuts):
                    self.modifier_press = time.monotonic()
                    self.modifier_blocked.add(vk)
                    self.mask_win_menu()
                    if self.shortcuts is None:
                        self.notify(0, 11)
                    else:
                        self.notify(0, 11, True, self.modifier_press)
                    return 1
                if not down and self.modifier_press is not None and mods != 11:
                    now, started = time.monotonic(), self.modifier_press
                    self.modifier_press = None
                    if self.shortcuts is None:
                        if self.release_notify:
                            self.release_notify(0, 11, now - started)
                    else:
                        self.notify(0, 11, False, now)
                if vk in self.modifier_blocked:
                    if not down:
                        self.modifier_blocked.discard(vk)
                    return 1
            if down and vk not in (16, 17, 18, 91, 92, 160, 161, 162, 163, 164, 165):
                if self.shortcuts is not None and vk not in self.suppressed and (vk, mods) not in self.shortcuts:
                    return self.user.CallNextHookEx(None, code, message, pointer)
                if not repeated:
                    if mods & 8:
                        self.mask_win_menu()
                    # Queue to Qt: never save settings or unregister a hook inside its callback.
                    if self.shortcuts is None:
                        if self.keyboard_notify and not mods & 8 and not event.flags & 0x10:
                            self.keyboard_notify(vk, mods)
                        self.recording[vk] = (mods, time.monotonic())
                        self.notify(vk, mods)
                    else:
                        self.suppressed[vk] = mods
                        self.notify(vk, mods, True, time.monotonic())
                return 1
            if not down and vk in self.recording:
                mods, started = self.recording.pop(vk)
                if self.release_notify:
                    self.release_notify(vk, mods, time.monotonic() - started)
                return 1
            if not down and vk in self.suppressed:
                required = self.suppressed.pop(vk)
                if vk not in self.ended:
                    self.notify(vk, required, False, time.monotonic())
                self.ended.discard(vk)
                return 1
        return self.user.CallNextHookEx(None, code, message, pointer)

    def mask_win_menu(self):
        from ngs.virtual_keyboard import _Input, _KeyboardInput
        # A suppressed Win chord otherwise becomes a bare Win press and opens Start.
        batch = (_Input * 2)(*[_Input(type=1, keyboard=_KeyboardInput(vk=0xe8, flags=flags, extra=MASK_EXTRA)) for flags in (0, 2)])
        if self.user.SendInput(2, batch, ctypes.sizeof(_Input)) != 2:
            self.mask_error = True
            self.user.SendInput(1, ctypes.byref(batch[1]), ctypes.sizeof(_Input))

    def close(self):
        if self.hook:
            self.user.UnhookWindowsHookEx(self.hook)
            self.hook = None


class WindowsWindowEvents:
    """Observe foreign window stacking without injecting hooks into other processes."""

    def __init__(self, changed):
        self.changed, self.hooks = changed, []
        self._stack_state, self._restacking = None, False
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        callback = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                     wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        self.callback = callback(self.notify)
        self.user.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                             callback, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        self.user.SetWinEventHook.restype = wintypes.HANDLE
        self.user.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        self.user.UnhookWinEvent.restype = wintypes.BOOL
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.GetDesktopWindow.restype = wintypes.HWND
        self.user.GetTopWindow.argtypes = [wintypes.HWND]
        self.user.GetTopWindow.restype = wintypes.HWND
        self.user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetWindow.restype = wintypes.HWND
        self.user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.GetWindowLongW.restype = wintypes.LONG
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL

    def set_enabled(self, enabled, *, reorder=True):
        mode = (bool(enabled), bool(enabled and reorder))
        if getattr(self, '_mode', None) == mode and (bool(self.hooks) == bool(enabled)):
            return
        self.close()
        self._mode = mode
        if enabled:
            # Foreground/menus and top-level show/hide/(optional reorder); skip our own SetWindowPos.
            for first, last in ((3, 7), (0x8002, 0x8004 if reorder else 0x8003)):
                handle = self.user.SetWinEventHook(first, last, None, self.callback, 0, 0, 2)
                if not handle:
                    error = ctypes.WinError(ctypes.get_last_error())
                    self.close()
                    raise error
                self.hooks.append(handle)

    def stack_state(self, handles):
        order, seen = [], set()
        hwnd = self.user.GetTopWindow(None)
        while hwnd and hwnd not in seen and len(seen) < 4096:
            seen.add(hwnd)
            if self.user.IsWindowVisible(hwnd):
                order.append(hwnd)
            hwnd = self.user.GetWindow(hwnd, 2)  # GW_HWNDNEXT
        return tuple(order), tuple((h, self.user.GetWindowLongW(h, -20) & 8) for h in handles)

    def restack(self, handles):
        handles = tuple(handles)
        if not handles or self._restacking:
            return False
        state = self.stack_state(handles)
        if state == self._stack_state:
            return False
        from ngs.process import set_topmost
        from ngs.edge_swipe import enable_dwm_alpha_composition
        self._restacking = True
        try:
            for hwnd in handles:
                enable_dwm_alpha_composition(hwnd)
                set_topmost(hwnd, True)
        finally:
            # Desktop REORDER can be delivered after our SetWindowPos; compare the resulting order.
            self._restacking = False
        self._stack_state = self.stack_state(handles)
        return True

    def notify(self, hook, event, hwnd, object_id, child_id, thread, timestamp):
        if not self.hooks or not hwnd or self._restacking:
            return
        desktop_reorder = event == 0x8004 and hwnd == self.user.GetDesktopWindow() and object_id == -4 and not child_id
        if event >= 0x8000 and not desktop_reorder and (object_id or child_id or self.user.GetAncestor(hwnd, 2) != hwnd):
            return
        self.changed()

    def close(self):
        for handle in self.hooks:
            self.user.UnhookWinEvent(handle)
        self.hooks.clear()
        self._stack_state = None


class DesktopControls(QObject):
    windowStackChanged = Signal()
    keyboardInput = Signal()
    changed = Signal()
    action = Signal(str)
    keyCaptured = Signal(int, int)
    keyReleased = Signal(int, int, float)
    keyState = Signal(int, int, bool, float)
    physicalKeyCaptured = Signal(int, int)

    def __init__(self, bridge):
        super().__init__(bridge)
        self.bridge = bridge
        self.backend = None
        self.window = None
        self.native_filter = None
        self.capture = ''
        self.message = ''
        self.errors = {}
        self.gamepad_ready = False
        self.capture_buttons = 0
        self.gamepad_armed = False
        self.key_capture = None
        self.capture_hold = False
        self.capture_candidate = None
        self.presses = {}
        self.pad_press = None
        self.hold_timer = QTimer(self)
        self.hold_timer.setInterval(20)
        self.hold_timer.timeout.connect(self.check_holds)
        self.keyCaptured.connect(self.capture_key, Qt.ConnectionType.QueuedConnection)
        self.keyReleased.connect(self.capture_release, Qt.ConnectionType.QueuedConnection)
        self.keyState.connect(self.keyboard_state, Qt.ConnectionType.QueuedConnection)
        self.physicalKeyCaptured.connect(self.note_keyboard, Qt.ConnectionType.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(10000)
        self.timer.timeout.connect(self.capture_timeout)

    @Property('QVariantList', notify=changed)
    def rows(self):
        values = self.values()
        return [dict(action=key, name=tr(name), binding=label(values.get(key)), error=self.errors.get(key, ''),
                     assigned=key in values, toggle=bool(values.get(key, {}).get('toggle')))
                for key, name in ACTIONS.items()]

    @Property(str, notify=changed)
    def capturing(self):
        return self.capture

    @Property(str, notify=changed)
    def status(self):
        return self.message

    @Property(bool, notify=changed)
    def trayAvailable(self):
        return bool(self.backend and self.backend.tray)

    def values(self):
        result = {}
        values = self.bridge.settings.get('shortcuts', {})
        if not isinstance(values, dict):
            return result
        for key, value in values.items():
            if key in ACTIONS and isinstance(value, dict) and set(value) - {'hold', 'toggle'} in ({'vk', 'mods'}, {'buttons'}):
                try:
                    result[key] = binding(**value)
                except ValueError:
                    pass
        return result

    def apply(self):
        self.errors.clear()
        if self.backend:
            accepted = {}
            for key, item in self.values().items():
                try:
                    candidate = dict(accepted, **{key: item})
                    self.backend.configure_shortcuts(candidate, self.keyState.emit)
                    accepted = candidate
                except (ValueError, OSError) as error:
                    self.errors[key] = str(error)
        self.changed.emit()

    def attach(self, window, native=True):
        self.window = window
        window.installEventFilter(self)
        if native and sys.platform == 'win32' and QGuiApplication.platformName() == 'windows':
            self.backend = WindowsDesktop(int(window.winId()), self.bridge.storage.resources / 'assets/ngs.ico')
            self.native_filter = _NativeEvents(self)
            QCoreApplication.instance().installNativeEventFilter(self.native_filter)
            if not self.trayAvailable:
                self.message = tr("The Windows tray is unavailable; NGS will minimize to the taskbar.")
        self.apply()

    @Slot(str)
    @Slot(str, bool)
    def beginCapture(self, action, hold=False):
        if action not in ACTIONS:
            return
        keyboard = getattr(self.bridge, 'virtual_keyboard', None)
        if keyboard:
            keyboard.hide()
        self.capture = action
        self.capture_hold = hold
        self.capture_candidate = None
        self.cancel_presses()
        self.gamepad_ready = False
        self.capture_buttons = 0
        self.message = tr("Release all buttons, then press the desired button or shortcut. Release gamepad buttons to save. Choose Cancel to stop.")
        self.stop_key_capture()
        if self.native_filter:
            try:
                self.key_capture = WindowsKeyCapture(self.keyCaptured.emit, release=self.keyReleased.emit,
                                                    keyboard_notify=self.physicalKeyCaptured.emit)
            except OSError:
                self.message += tr(" Global Windows capture is unavailable; keyboard recording works inside NGS.")
        self.timer.start()
        self.changed.emit()

    def start_press(self, token, value, now):
        if getattr(self.bridge, '_closing', False):
            return
        matches = {key: item for key, item in self.values().items() if physical(item) == physical(value)}
        if not matches or token in self.presses:
            return
        short = next((key for key, item in matches.items() if not item.get('hold')), None)
        long = next((key for key, item in matches.items() if item.get('hold')), None)
        self.presses[token] = dict(short=short, long=long, start=now, fired=False)
        if long:
            self.hold_timer.start()
        elif short:
            self.presses[token]['fired'] = True
            self.fire_action(short)

    def end_press(self, token, now):
        press = self.presses.pop(token, None)
        if press and not press['fired']:
            key = press['long'] if now - press['start'] >= HOLD_SECONDS else press['short']
            if key:
                self.fire_action(key)
        if not self.presses:
            self.hold_timer.stop()

    def check_holds(self):
        now = time.monotonic()
        for press in list(self.presses.values()):
            if press['long'] and not press['fired'] and now - press['start'] >= HOLD_SECONDS:
                press['fired'] = True
                self.fire_action(press['long'])
        if all(press['fired'] for press in self.presses.values()):
            self.hold_timer.stop()

    def cancel_presses(self):
        self.presses.clear()
        self.pad_press = None
        self.hold_timer.stop()

    def fire_action(self, action):
        self.action.emit('toggle' if action in ('desktop', 'show') and
                         self.values().get(action, {}).get('toggle') else action)

    @Slot(int, int, bool, float)
    def keyboard_state(self, vk, mods, down, now):
        if self.capture:
            return
        if self.backend and self.backend.key_hook and self.backend.key_hook.mask_error:
            self.message = tr("Windows rejected suppressing the Win menu. Check the active program's permissions.")
            self.changed.emit()
        token = ('key', vk, mods)
        if down:
            self.start_press(token, dict(vk=vk, mods=mods), now)
        else:
            self.end_press(token, now)

    def gamepad_input(self, buttons):
        if self.capture:
            if not self.gamepad_ready:
                self.gamepad_ready = buttons == 0
            elif buttons:
                combined = self.capture_buttons | buttons
                if combined != self.capture_buttons:
                    self.capture_buttons = combined
                    self.message = label(dict(buttons=combined)) + tr(" · release to save")
                    self.changed.emit()
            elif self.capture_buttons:
                self.gamepad_armed = False
                self.assign(self.capture, binding(buttons=self.capture_buttons, hold=self.capture_hold))
                self.capture_buttons = 0
            return True
        if self.pad_press and buttons & self.pad_press != self.pad_press:
            previous = self.pad_press
            self.pad_press = None
            self.end_press(('pad', previous), time.monotonic())
        if not buttons:
            self.gamepad_armed = True
            return False
        matches = [(key, value['buttons']) for key, value in self.values().items() if 'buttons' in value]
        if self.gamepad_armed:
            for key, mask in matches:
                if buttons == mask:
                    self.gamepad_armed = False
                    self.pad_press = mask
                    self.start_press(('pad', mask), dict(buttons=mask), time.monotonic())
                    return True
        return not self.gamepad_armed or any(buttons & mask == buttons for _, mask in matches)

    def gamepad_connection(self, connected):
        if not connected:
            for token in list(self.presses):
                if token[0] == 'pad':
                    self.presses.pop(token)
            self.pad_press = None
            self.gamepad_armed = self.gamepad_ready = False
            self.capture_buttons = 0
            if not self.presses:
                self.hold_timer.stop()

    @Slot()
    def cancelCapture(self):
        self.capture = ''
        self.stop_key_capture()
        self.timer.stop()
        self.message = tr("Binding canceled.")
        self.changed.emit()

    def capture_timeout(self):
        self.cancelCapture()
        self.message = tr("No recognizable input received. OneXConsole may handle the button itself. Try recording short and long presses separately; F12 is unavailable.")
        self.changed.emit()

    def stop_key_capture(self):
        if self.key_capture:
            self.key_capture.close()
            self.key_capture = None

    @Slot(int, int)
    def capture_key(self, vk, mods):
        if self.capture:
            if vk == 27:
                self.cancelCapture()
            else:
                value = dict(vk=vk, mods=mods, **({'hold': True} if self.capture_hold else {}))
                if self.capture_hold:
                    try:
                        value = binding(**value)
                    except ValueError as error:
                        self.message = str(error)
                    else:
                        self.capture_candidate = value
                        self.capture_started = time.monotonic()
                        self.message = label(physical(value)) + tr(" · hold for at least 0.7 s, then release")
                    self.changed.emit()
                    return
                if not self.assign(self.capture, value):
                    self.message = tr("Windows reported ") + label(value) + '. ' + self.message
                    self.changed.emit()

    @Slot(int, int, float)
    def capture_release(self, vk, mods, duration):
        value = self.capture_candidate
        if not self.capture or not value or (value['vk'], value['mods']) != (vk, mods):
            return
        self.capture_candidate = None
        if duration >= HOLD_SECONDS:
            self.assign(self.capture, value)
        else:
            self.message = (label(physical(value)) + tr(" · signal ") + str(round(duration * 1000)) +
                            tr(" ms. A hold requires 700 ms. If holding produces only a pulse, choose Short press: the firmware or OneXConsole handles the duration."))
            self.changed.emit()

    @Slot(str)
    def clear(self, action):
        self.assign(action, None)

    @Slot(str, bool)
    def setToggle(self, action, enabled):
        value = self.values().get(action)
        if action in ('desktop', 'show') and value:
            self.assign(action, dict(value, toggle=enabled))

    def assign(self, action, value):
        if action not in ACTIONS:
            return False
        values = self.values()
        previous = self.bridge.settings.get('shortcuts')
        old_active = {key: item for key, item in values.items() if key not in self.errors}
        try:
            if value is not None:
                if 'toggle' not in value:
                    value = dict(value, toggle=values.get(action, {}).get('toggle', False))
                value = binding(**value)
                if any(physical(item) == physical(value) and bool(item.get('hold')) == bool(value.get('hold'))
                       for key, item in values.items() if key != action):
                    raise ValueError(tr("This shortcut is already assigned to another NGS action."))
            if value:
                values[action] = value
            else:
                values.pop(action, None)
            if self.backend:
                self.backend.configure_shortcuts({key: item for key, item in values.items() if key == action or key not in self.errors}, self.keyState.emit)
            self.bridge.settings['shortcuts'] = values
            self.bridge.save()
            self.errors.pop(action, None)
            self.cancel_presses()
            self.capture = ''
            self.stop_key_capture()
            self.timer.stop()
            self.message = tr("Saved: ") + (label(value) if value else tr("binding removed"))
            return True
        except (ValueError, OSError) as error:
            if previous is None:
                self.bridge.settings.pop('shortcuts', None)
            else:
                self.bridge.settings['shortcuts'] = previous
            if self.backend:
                try:
                    self.backend.configure_shortcuts(old_active, self.keyState.emit)
                except (ValueError, OSError):
                    self.errors[action] = tr("Could not restore capture for the previous bindings.")
            self.message = str(error)
            return False
        finally:
            self.changed.emit()

    def note_keyboard(self, vk, mods):
        if vk in (16, 17, 18, 91, 92, 160, 161, 162, 163, 164, 165) or mods & 8:
            return
        if any(item.get('vk') == vk and item.get('mods') == mods for item in self.values().values()):
            return
        self.keyboardInput.emit()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and not self.backend:
            modifiers = event.modifiers()
            mods = sum(bit for flag, bit in ((Qt.KeyboardModifier.AltModifier, 1), (Qt.KeyboardModifier.ControlModifier, 2),
                (Qt.KeyboardModifier.ShiftModifier, 4), (Qt.KeyboardModifier.MetaModifier, 8)) if modifiers & flag)
            key = event.key()
            if key not in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
                vk = event.nativeVirtualKey() or (key - Qt.Key.Key_F1 + 112 if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24 else key)
                self.note_keyboard(vk, mods)
        if self.capture and event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                key = event.key()
                if key == Qt.Key.Key_Escape:
                    self.cancelCapture()
                elif key not in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
                    vk = event.nativeVirtualKey()
                    if not vk:
                        vk = key if 32 <= key <= 90 else key - Qt.Key.Key_F1 + 112 if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24 else 0
                    modifiers = event.modifiers()
                    mods = sum(bit for flag, bit in ((Qt.KeyboardModifier.AltModifier, 1), (Qt.KeyboardModifier.ControlModifier, 2),
                        (Qt.KeyboardModifier.ShiftModifier, 4), (Qt.KeyboardModifier.MetaModifier, 8)) if modifiers & flag)
                    self.capture_key(vk, mods)
            elif event.type() == QEvent.Type.KeyRelease and not event.isAutoRepeat() and self.capture_candidate:
                value = self.capture_candidate
                vk = event.nativeVirtualKey() or (event.key() if 32 <= event.key() <= 90 else event.key() - Qt.Key.Key_F1 + 112)
                if vk == value['vk']:
                    self.capture_release(vk, value['mods'], time.monotonic() - self.capture_started)
            return True
        return super().eventFilter(watched, event)

    def native_message(self, hwnd, message, wparam, lparam):
        if not self.backend:
            return False
        if message == self.backend.taskbar_message:
            self.backend.restore_tray()
            if not self.backend.tray and self.window and not self.window.isVisible():
                self.action.emit('show')
            self.changed.emit()
        if hwnd != self.backend.hwnd:
            return False
        if message == 0x47:  # WM_WINDOWPOSCHANGED: activation can reorder two TOPMOST windows.
            self.windowStackChanged.emit()
        if message in (0x100, 0x104):
            source = _InputMessageSource()
            user = self.backend.user
            # OEM shortcuts and our SendInput keyboard must not masquerade as a physical keyboard.
            if (user.GetCurrentInputMessageSource(ctypes.byref(source)) and source.origin == 1
                    and user.GetMessageExtraInfo() not in (NGS_INPUT_EXTRA, MASK_EXTRA)):
                mods = sum(bit for vk, bit in ((18, 1), (17, 2), (16, 4)) if user.GetKeyState(vk) & 0x8000)
                if user.GetKeyState(91) & 0x8000 or user.GetKeyState(92) & 0x8000:
                    mods |= 8
                self.note_keyboard(wparam, mods)
        if message == WM_TRAY and lparam in (0x202, 0x203, 0x205):
            self.action.emit('menu' if lparam == 0x205 else 'show')
            return True
        if message == 0x312 and 1 <= wparam <= len(ACTIONS):
            key = list(ACTIONS)[wparam - 1]
            value = self.values().get(key)
            if self.capture:
                value = self.values().get(key)
                if value:
                    self.assign(self.capture, dict(physical(value), hold=bool(value.get('hold'))))
            elif not value or not hooked(value, self.values()):
                self.fire_action(key)
            return True
        return False

    def close(self):
        self.timer.stop()
        self.cancel_presses()
        self.capture = ''
        self.stop_key_capture()
        if self.native_filter:
            QCoreApplication.instance().removeNativeEventFilter(self.native_filter)
            self.native_filter = None
        if self.backend:
            for identity in range(1, len(ACTIONS) + 1):
                self.backend.unregister(identity)
            self.backend.close()
            self.backend = None
