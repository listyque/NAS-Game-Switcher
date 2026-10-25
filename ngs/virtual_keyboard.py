"""A non-activating keyboard: bounded input batches and cancellable key macros."""
from ngs.i18n import tr
import ctypes
import math
import re
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from ngs.gamepad import GAMEPAD_BUTTONS
from ngs.keyboard_layouts import validate_layout, layer_name


MODIFIERS = {'Ctrl': 17, 'Shift': 16, 'Alt': 18, 'Win': 91, 'RightShift': 161, 'RightCtrl': 163, 'RightAlt': 165}
MODIFIER_ALIASES = {'RightShift':'Shift', 'RightCtrl':'Ctrl', 'RightAlt':'Alt'}
KEYBOARD_VIEWS = [dict(id=key, menu_id=menu_id, name=name, note=note) for key, menu_id, name, note in (
    ('touch', 11, 'Normal', 'Straight rows'), ('small', 30, 'Compact', 'Floating window'),
    ('split', 31, 'Two-thumb', 'Split'), ('traditional', 32, 'Traditional', 'Five rows'),
    ('gamepad', 33, 'Gamepad', 'Button hints'), ('full', 12, 'Full', 'Navigation block'),
    ('navigation', 34, 'Navigation', 'Single row'), ('numpad', 13, 'NumPad', 'Numbers and navigation'))]
VIEW_IDS = {row['id'] for row in KEYBOARD_VIEWS}
INPUT_THEMES = [dict(id=key, name=name, panel=panel, key=button, edge=edge, text=text, muted=muted,
                     hover=hover, pressed=pressed, held=held, accent=accent, panel_text='#ffffff' if key == 'contrast' else text,
                     hint=text if key == 'contrast' else muted) for key, name, panel, button, edge, text, muted, hover, pressed, held, accent in (
    ('dark', 'Dark', '#202024', '#363639', '#262629', '#f2f2f4', '#b6b6c2', '#454549', '#505054', '#475e76', '#b8cce7'),
    ('light', 'Light', '#e7e9ed', '#fafafa', '#c8cbd1', '#202124', '#53565c', '#eef0f4', '#d4d9e0', '#b4d3f4', '#245c98'),
    ('black', 'Black', '#000000', '#181818', '#080808', '#ffffff', '#c4c4c4', '#282828', '#383838', '#3a526b', '#b8d9ff'),
    ('contrast', 'High contrast', '#000000', '#ffffff', '#c0c0c0', '#000000', '#ffffff', '#eeeeee', '#cccccc', '#ffdf60', '#ffff00'),
    ('blue', 'Blue', '#152a40', '#28445f', '#193048', '#f5f9ff', '#bdcfdf', '#365975', '#42647e', '#457397', '#b4d9fa'),
    ('lilac', 'Lilac', '#dfd9eb', '#f4eefb', '#b8accb', '#30263e', '#62536f', '#e8dff3', '#d7cae9', '#c5addf', '#634480'),
    ('mint', 'Mint', '#d3e7dc', '#eff9f2', '#a3c3b3', '#153b2c', '#426453', '#deeee5', '#bfdccc', '#a2cdb5', '#276748'),
    ('terracotta', 'Terracotta', '#622d22', '#883f2f', '#683024', '#fff5ee', '#f1cdbd', '#9b4e3a', '#a05842', '#854d36', '#ffcfad'))]
NGS_INPUT_EXTRA = 0x4e47534b
KEYS = dict(Esc=27, Tab=9, Backspace=8, Enter=13, Space=32, CapsLock=20, NumLock=144,
            Left=37, Up=38, Right=39, Down=40, Home=36, End=35, PageUp=33, PageDown=34,
            Insert=45, Delete=46, Minus=189, Equal=187, Backtick=192, LeftBracket=219, RightBracket=221,
            Backslash=220, Semicolon=186, Quote=222, Comma=188, Period=190, Slash=191,
            Add=107, Subtract=109, Multiply=106, Divide=111, Decimal=110, NumEnter=13, Menu=93, PrintScreen=44, Pause=19, ScrollLock=145)
KEYS.update({chr(i): i for i in range(65, 91)})
KEYS.update({str(i): i + 48 for i in range(10)})
KEYS.update({'Num' + str(i): i + 96 for i in range(10)})
KEYS.update({'F' + str(i): i + 111 for i in range(1, 25)})
KEY_NAMES = {key.casefold(): key for key in (*MODIFIERS, *KEYS)}

PAD_PRESETS = {
    'wasd': dict(name='WASD · action', description='D-pad — WASD movement; A — Space, X — E, Y — R, LB — Shift.',
                 keys=['W', 'S', 'A', 'D', 'Esc', 'Tab', 'C', 'V', 'Shift', 'F', 'Space', 'Ctrl', 'E', 'R', 'Alt', 'Enter']),
    'arrows': dict(name='Arrow keys · menus and classic games', description='D-pad — arrow keys; A — Enter, B — Esc, X — Space, Y — Backspace.',
                   keys=['Up', 'Down', 'Left', 'Right', 'Esc', 'Tab', 'Home', 'End', 'Shift', 'Ctrl', 'Enter', 'Esc', 'Space', 'Backspace', 'Alt', 'Tab']),
    'racing': dict(name='Racing · WASD', description='D-pad — throttle, brake and steering; A — Space, X/Y — Q/E, LB — Shift.',
                   keys=['W', 'S', 'A', 'D', 'Esc', 'Tab', 'Tab', 'F', 'Shift', 'C', 'Space', 'R', 'Q', 'E', 'Z', 'Enter']),
}


def chord(value):
    parts = [KEY_NAMES.get(part.strip().casefold(), part.strip()) for part in value.split('+')] if isinstance(value, str) else []
    if not 1 <= len(parts) <= 5 or any(part not in MODIFIERS for part in parts[:-1]) or parts[-1] not in KEYS:
        raise ValueError(tr("Unknown shortcut: ") + str(value)[:60])
    if len(set(parts[:-1])) != len(parts[:-1]):
        raise ValueError(tr("A modifier is listed twice."))
    return parts


def macro_steps(sequence):
    if not isinstance(sequence, str) or not 1 <= len(sequence) <= 2000:
        raise ValueError(tr("Enter a sequence of up to 2000 characters."))
    steps, duration = [], 0
    for item in sequence.split(';'):
        item = item.strip()
        if re.fullmatch(r'wait:\d{1,4}', item):
            delay = int(item[5:])
            if delay > 2000:
                raise ValueError(tr("Each pause can last up to 2000 ms."))
            steps.append(delay)
            duration += delay
        else:
            steps.append(chord(item))
            duration += 50
    if len(steps) > 100 or duration > 30000:
        raise ValueError(tr("A macro can have up to 100 steps and last 30 seconds."))
    return steps


def pad_chord(value):
    modifier = KEY_NAMES.get(value.strip().casefold()) if isinstance(value, str) else None
    return [modifier] if modifier in MODIFIERS else chord(value)


class _KeyboardInput(ctypes.Structure):
    _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('extra', wintypes.WPARAM)]


class _MouseInput(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG), ('data', wintypes.DWORD),
                ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', wintypes.WPARAM)]


class _InputUnion(ctypes.Union):
    _fields_ = [('keyboard', _KeyboardInput), ('mouse', _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', wintypes.DWORD), ('data', _InputUnion)]


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('flags', wintypes.DWORD),
                *[(name, wintypes.HWND) for name in ('active', 'focus', 'capture', 'menu', 'move', 'caret')], ('rect', wintypes.RECT)]


class _CursorInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('flags', wintypes.DWORD), ('cursor', wintypes.HANDLE), ('position', wintypes.POINT)]


class WindowsKeyboard:
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        self.user.SendInput.restype = wintypes.UINT
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetKeyState.argtypes = [ctypes.c_int]
        self.user.GetKeyState.restype = ctypes.c_short
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        self.user.GetKeyboardLayout.restype = wintypes.HANDLE
        self.user.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
        self.user.GetKeyboardLayoutList.restype = ctypes.c_int
        self.user.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
        self.user.GetGUIThreadInfo.restype = wintypes.BOOL
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.PostMessageW.restype = wintypes.BOOL
        self.user.GetCursorInfo.argtypes = [ctypes.POINTER(_CursorInfo)]
        self.user.GetCursorInfo.restype = wintypes.BOOL

    def locks(self):
        return [key for key in ('CapsLock', 'NumLock') if self.user.GetKeyState(KEYS[key]) & 1]

    def foreground(self):
        return self.user.GetForegroundWindow()

    def current_layout(self):
        hwnd = self.foreground()
        thread = self.user.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
        return hwnd, self.user.GetKeyboardLayout(thread) if thread else 0

    def next_layout(self):
        hwnd, current = self.current_layout()
        if not hwnd or not current: raise OSError(tr("No active window for changing keyboard language."))
        values = (wintypes.HANDLE * 64)()
        count = self.user.GetKeyboardLayoutList(len(values), values)
        if count < 2: raise OSError(tr("Only one keyboard layout is installed in Windows."))
        layouts = list(values[:count])
        index = (layouts.index(current) + 1) % len(layouts) if current in layouts else 0
        info = _GuiThreadInfo(size=ctypes.sizeof(_GuiThreadInfo))
        thread = self.user.GetWindowThreadProcessId(hwnd, None)
        target = info.focus if self.user.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.active == hwnd and info.focus else hwnd
        if self.foreground() != hwnd: raise OSError(tr("The input window changed. Try changing the language again."))
        if not self.user.PostMessageW(target, 0x50, 0, layouts[index]): raise ctypes.WinError(ctypes.get_last_error())
        return hwnd, layouts[index]

    def send(self, parts):
        modifiers = [MODIFIERS[name] for name in parts[:-1] if not self.user.GetAsyncKeyState(MODIFIERS[name]) & 0x8000]
        key = parts[-1]
        vk = KEYS[key]
        extended = key in ('Left', 'Right', 'Up', 'Down', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Delete', 'Divide', 'NumEnter', 'PrintScreen')
        events = [(value, int(value in (163, 165))) for value in modifiers] + [(vk, int(extended)), (vk, int(extended) | 2)] + [(value, 2 | int(value in (163, 165))) for value in reversed(modifiers)]
        batch = (_Input * len(events))(*[_Input(type=1, keyboard=_KeyboardInput(vk=value, flags=flags, extra=NGS_INPUT_EXTRA)) for value, flags in events])
        if self.user.SendInput(len(batch), batch, ctypes.sizeof(_Input)) != len(batch):
            release = (_Input * (len(modifiers) + 1))(*[_Input(type=1, keyboard=_KeyboardInput(vk=value, flags=flags, extra=NGS_INPUT_EXTRA))
                for value, flags in [(vk, int(extended) | 2)] + [(value, 2 | int(value in (163, 165))) for value in reversed(modifiers)]])
            self.user.SendInput(len(release), release, ctypes.sizeof(_Input))
            raise OSError(tr("Windows rejected input. Check whether the target application is running as administrator."))

    def text(self, text):
        data = text.encode('utf-16-le')
        units = [int.from_bytes(data[i:i+2], 'little') for i in range(0, len(data), 2)]
        batch = (_Input * (len(units) * 2))(*[_Input(type=1, keyboard=_KeyboardInput(scan=value, flags=flags, extra=NGS_INPUT_EXTRA))
                                             for value in units for flags in (4, 6)])
        if self.user.SendInput(len(batch), batch, ctypes.sizeof(_Input)) != len(batch):
            releases = (_Input * len(units))(*[_Input(type=1, keyboard=_KeyboardInput(scan=value, flags=6, extra=NGS_INPUT_EXTRA)) for value in units])
            self.user.SendInput(len(releases), releases, ctypes.sizeof(_Input))
            raise OSError(tr("Windows rejected text input. Check the target application's permissions."))

    def key_event(self, key, down):
        vk = MODIFIERS.get(key, KEYS.get(key))
        extended = key in ('Left', 'Right', 'Up', 'Down', 'Home', 'End', 'PageUp', 'PageDown', 'Insert', 'Delete', 'Divide', 'NumEnter', 'PrintScreen', 'Win', 'RightCtrl', 'RightAlt')
        batch = (_Input * 1)(_Input(type=1, keyboard=_KeyboardInput(vk=vk, flags=int(extended) | (0 if down else 2), extra=NGS_INPUT_EXTRA)))
        if self.user.SendInput(1, batch, ctypes.sizeof(_Input)) != 1:
            raise OSError(tr("Windows rejected key input. Check the target application's permissions."))

    def physical_down(self, key):
        return bool(self.user.GetAsyncKeyState(MODIFIERS.get(key, KEYS.get(key))) & 0x8000)

    def mouse(self, dx=0, dy=0, button=''):
        flags = {'': [1], 'left': [2, 4], 'right': [8, 16]}[button]
        batch = (_Input * len(flags))(*[_Input(type=0, mouse=_MouseInput(x=dx, y=dy, flags=flag, extra=NGS_INPUT_EXTRA)) for flag in flags])
        if self.user.SendInput(len(batch), batch, ctypes.sizeof(_Input)) != len(batch):
            if button:
                release = (_Input * 1)(_Input(type=0, mouse=_MouseInput(flags=flags[-1], extra=NGS_INPUT_EXTRA)))
                self.user.SendInput(1, release, ctypes.sizeof(_Input))
            raise OSError(tr("Windows rejected mouse input. Check the target application's permissions."))

    def restore_pointer(self):
        info = _CursorInfo(size=ctypes.sizeof(_CursorInfo))
        if not self.user.GetCursorInfo(ctypes.byref(info)): raise ctypes.WinError(ctypes.get_last_error())
        # Touch suppresses the cursor even when the relative pointer has stopped.
        if info.flags & 2: self.mouse()


    def scroll(self, delta):
        batch = (_Input * 1)(_Input(type=0, mouse=_MouseInput(data=delta & 0xffffffff, flags=0x800, extra=NGS_INPUT_EXTRA)))
        if self.user.SendInput(1, batch, ctypes.sizeof(_Input)) != 1: raise OSError(tr("Windows rejected scrolling."))


class VirtualKeyboard(QObject):
    changed = Signal()
    macrosChanged = Signal()
    action = Signal(str)
    editRequested = Signal()
    layoutEditRequested = Signal('QVariantMap')
    editorClosed = Signal()
    visibilityRequested = Signal(bool)
    chordPicked = Signal(str)
    hapticRequested = Signal(int)
    padTool = Signal(bool, int)

    def __init__(self, bridge):
        super().__init__(bridge)
        self.bridge = bridge
        self.backend = WindowsKeyboard() if sys.platform == 'win32' else None
        self.remote = None
        self.external_running = False
        self.shown = False
        self.modifiers = []
        self.shift_locked = False
        self.shift_tap = None
        self.locked = []
        self.message = ''
        self.steps = []
        self.target = None
        self.screen = None
        self.pad_enabled = False
        self.pad_neutral = True
        self.pad_target = None
        self.pad_down = []
        self.touchpads = False
        self.native_touchpads = None
        self.scroll_fraction = 0.
        self.picking = False
        self.editor = None
        self.language = '?'
        self.layout_pending = None
        self.layout_attempts = 0
        self.layout_timer = QTimer(self)
        self.layout_timer.timeout.connect(self.refreshLayout)
        self.pointer_fraction = [0., 0.]
        self.pointer_timer = QTimer(self)
        self.pointer_timer.setSingleShot(True)
        self.pointer_timer.timeout.connect(self.restore_pointer)
        self.pad_watch = QTimer(self)
        self.pad_watch.setInterval(50)
        self.pad_watch.timeout.connect(self.check_target)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.next_step)
        self.screenshot_request = None
        self.screenshot_timer = QTimer(self)
        self.screenshot_timer.setSingleShot(True)
        self.screenshot_timer.setInterval(320)  # Let Windows repaint after the overlays disappear.
        self.screenshot_timer.timeout.connect(self.finish_screenshot)
        self.changed.connect(self.apply_enabled)

    @Property(bool, notify=changed)
    def enabled(self): return self.options['enabled']

    def apply_enabled(self):
        if not self.enabled and not self.touchpads and (self.shown or self.picking): self.hide()

    @Slot(bool)
    def setEnabled(self, enabled): self.save_options(enabled=enabled)

    @Slot(bool)
    def setOverlayEnabled(self, enabled): self.save_options(overlay_enabled=enabled)

    @Slot(bool)
    def setEdgeSwipeEnabled(self, enabled): self.save_options(edge_swipe_enabled=enabled)

    @Slot(bool)
    def setEdgeAutoHide(self, enabled): self.save_options(edge_auto_hide=enabled)

    def set_screen(self, screen):
        if self.screen is not None:
            for signal in (self.screen.geometryChanged, self.screen.availableGeometryChanged, self.screen.logicalDotsPerInchChanged):
                try: signal.disconnect(self.display_changed)
                except (RuntimeError, TypeError): pass
        self.screen = screen
        if screen is not None:
            for signal in (screen.geometryChanged, screen.availableGeometryChanged, screen.logicalDotsPerInchChanged):
                signal.connect(self.display_changed)
        self.display_changed()

    @Slot()
    def display_changed(self):
        self.changed.emit()

    @Property('QVariantMap', notify=changed)
    def screenArea(self):
        from shiboken6 import isValid
        screen = self.screen if self.screen is not None and isValid(self.screen) else QGuiApplication.primaryScreen()
        if screen is None or not isValid(screen):
            return dict(x=0, y=0, width=1280, height=720)
        area = screen.geometry()
        return dict(x=area.x(), y=area.y(), width=area.width(), height=area.height())

    @Property(bool, notify=changed)
    def visible(self):
        return self.shown and (self.enabled or self.touchpads)

    @Property(bool, notify=changed)
    def remoteHost(self):
        return self.remote is not None

    @Property(bool, notify=changed)
    def screenshotPending(self):
        return self.screenshot_request is not None

    @Property('QStringList', notify=changed)
    def held(self):
        return list(dict.fromkeys([*self.modifiers, *self.locked, *[MODIFIER_ALIASES[key] for key in self.modifiers if key in MODIFIER_ALIASES]]))

    @Property(bool, notify=changed)
    def shiftLocked(self):
        return self.shift_locked

    @Property(str, notify=changed)
    def status(self):
        return self.message

    @Property(bool, notify=changed)
    def running(self):
        return bool(self.steps or self.timer.isActive() or self.external_running or self.remote and self.remote.running)

    @Property('QVariantMap', notify=changed)
    def options(self):
        values = self.bridge.settings.get('keyboard_options', {})
        if not isinstance(values, dict): values = {}
        size, opacity, haptics = values.get('size', 1), values.get('opacity', .5), values.get('haptics', True)
        keyboard_opacity = values.get('keyboard_opacity', 1.)
        view = values.get('view', 'touch')
        if not isinstance(view, str) or not (view in VIEW_IDS or any(view == 'custom-' + row['id'] for row in self.layouts)): view = 'touch'
        views = values.get('views', {})
        profile = views.get(view, {}) if isinstance(views, dict) else {}
        if not isinstance(profile, dict): profile = {}
        nav_minimum = max(8, min(35, math.ceil(12400 / max(1,self.screenArea['height'])))) if view == 'navigation' else 12
        minimum, maximum, default = (nav_minimum, 35, max(12,nav_minimum)) if view == 'navigation' else (20, 75, 32 if view == 'small' else 40)
        height = profile.get('height_percent', values.get('height_percent', (36, 44, 52)[size] if 'size' in values and type(size) is int and 0 <= size <= 2 else default))
        keyboard_opacity = profile.get('keyboard_opacity', keyboard_opacity)
        pad_height, sensitivity = values.get('touchpad_height_percent', 46), values.get('pointer_sensitivity', 1.)
        return dict(size=size if type(size) is int and 0 <= size <= 2 else 1,
                    opacity=opacity if type(opacity) in (int, float) and .15 <= opacity <= 1 else .5,
                    haptics=haptics if type(haptics) is bool else True,
                    view=view,
                    touchpad_height_percent=round(pad_height) if type(pad_height) in (int, float) and 20 <= pad_height <= 75 else 46,
                    pointer_sensitivity=sensitivity if type(sensitivity) in (int, float) and .25 <= sensitivity <= 3 else 1.,
                    height_percent=round(height) if type(height) in (int, float) and minimum <= height <= maximum else default,
                    height_min=minimum, height_max=maximum,
                    enabled=values.get('enabled', True) is True,
                    overlay_enabled=values.get('overlay_enabled', True) is True,
                    edge_swipe_enabled=values.get('edge_swipe_enabled', True) is True,
                    edge_auto_hide=values.get('edge_auto_hide', True) is True,
                    auto_show=values.get('auto_show', True) is True,
                    keyboard_opacity=keyboard_opacity if type(keyboard_opacity) in (float, int) and 0 <= keyboard_opacity <= 1 else 1.)

    @Slot(float)
    def setKeyboardOpacity(self, value):
        if 0 <= value <= 1: self.save_options(keyboard_opacity=value)

    def option_values(self, **changes):
        raw = self.bridge.settings.get('keyboard_options', {})
        values = dict(raw) if isinstance(raw, dict) else {}
        profile_changes = {key: changes.pop(key) for key in ('height_percent', 'keyboard_opacity') if key in changes}
        if profile_changes:
            views = values.get('views', {})
            views = dict(views) if isinstance(views, dict) else {}
            view = self.options['view']
            profile = views.get(view, {})
            views[view] = dict(profile if isinstance(profile, dict) else {}, **profile_changes)
            values['views'] = views
        return dict(values, **changes)

    def save_options(self, **changes):
        return self.save_value('keyboard_options', self.option_values(**changes))

    @Property(str, notify=changed)
    def layout(self):
        return self.language

    @Slot(bool)
    def setHaptics(self, enabled):
        if self.save_options(haptics=enabled) and not enabled:
            self.hapticRequested.emit(0)

    @Slot()
    def feedback(self):
        if self.options['haptics']: self.hapticRequested.emit(18)

    @Slot()
    def switchLayout(self):
        if not self.backend or self.picking: return
        try:
            value = self.backend.next_layout()
            if not isinstance(value, tuple) or len(value) != 2: raise OSError(tr("Could not change the keyboard layout."))
            self.layout_pending = value
            self.layout_attempts = 0
            self.layout_timer.start(60)
            self.message = ''
        except OSError as error:
            self.message = str(error)
        self.changed.emit()

    @Slot()
    def refreshLayout(self):
        if not self.shown or self.touchpads:
            self.layout_timer.stop(); return
        if not self.backend: return
        try: value = self.backend.current_layout()
        except OSError: value = None
        valid = isinstance(value, tuple) and len(value) == 2 and type(value[1]) is int and bool(value[1])
        changed = False
        if valid:
            import locale
            language = locale.windows_locale.get(value[1] & 0xffff, hex(value[1] & 0xffff)).split('_')[0].upper()
            changed = language != self.language
            self.language = language
        if self.layout_pending:
            # Failed/empty reads count too. Otherwise a lost foreground window can
            # leave the temporary 60 ms verification timer running indefinitely.
            self.layout_attempts += 1
            if valid and (value == self.layout_pending or value[0] != self.layout_pending[0]) or self.layout_attempts >= 8:
                self.message = '' if value == self.layout_pending else tr("The application did not accept the language change. Check Windows keyboard layouts.")
                self.layout_pending = None
                self.layout_timer.setInterval(500)
                changed = True
        if changed: self.changed.emit()

    @Property(bool, notify=changed)
    def touchpadMode(self):
        return self.touchpads

    @Property(bool, notify=changed)
    def nativeTouch(self):
        return self.native_touchpads is not None

    def attach_touchpads(self, windows):
        if sys.platform == 'win32' and QGuiApplication.platformName() == 'windows':
            from ngs.touchpad import NativeTouchpads
            self.native_touchpads = NativeTouchpads(self, windows)
            QGuiApplication.instance().installNativeEventFilter(self.native_touchpads)
            self.changed.emit()

    @Property(bool, notify=changed)
    def padEnabled(self):
        return self.pad_enabled

    @Property('QStringList', constant=True)
    def keyNames(self):
        return [*KEYS, *MODIFIERS]

    @Property('QStringList', constant=True)
    def padNames(self):
        return list(GAMEPAD_BUTTONS.values())

    @Property('QVariantList', notify=changed)
    def padRows(self):
        values = self.bridge.settings.get('keyboard_pad', {})
        if not isinstance(values, dict): values = {}
        return [dict(button=mask, name=name, keys=values.get(str(mask), '') if isinstance(values.get(str(mask), ''), str) else '') for mask, name in GAMEPAD_BUTTONS.items()]

    @Property('QVariantList', constant=True)
    def padPresets(self):
        return [dict(id=key, name=tr(value['name']), description=tr(value['description']),
                     rows=[dict(button=button, name=name, keys=assignment)
                           for (button, name), assignment in zip(GAMEPAD_BUTTONS.items(), value['keys'], strict=True)])
                for key, value in PAD_PRESETS.items()]

    @Slot(str, result=bool)
    def applyPadPreset(self, identity):
        preset = next((preset for preset in self.padPresets if preset['id'] == identity), None)
        if preset is None:
            self.message = tr("Choose an existing preset."); self.changed.emit(); return False
        self.setPadEnabled(False)
        if self.pad_down: return False
        values = {str(row['button']): '+'.join(pad_chord(row['keys'])) for row in preset['rows']}
        return self.save_value('keyboard_pad', values)

    def save_value(self, key, value):
        previous = self.bridge.settings.get(key)
        try:
            self.bridge.settings[key] = value
            self.bridge.save()
            self.message = ''
            return True
        except OSError as error:
            if previous is None: self.bridge.settings.pop(key, None)
            else: self.bridge.settings[key] = previous
            self.message = str(error)
            return False
        finally:
            self.changed.emit()

    @Slot(int)
    def setSize(self, value):
        if value in (0, 1, 2): self.save_options(size=value, height_percent=(36, 44, 52)[value])

    @Slot(float)
    def setHeight(self, value):
        if self.options['height_min'] <= value <= self.options['height_max']:
            self.save_options(height_percent=round(value))

    @Slot(bool)
    def setAutoShow(self, enabled):
        self.save_options(auto_show=enabled)

    @Slot()
    def windowsTypingSettings(self):
        self.hide()
        self.bridge.openSystemSettings('typing')

    @Slot(str)
    def setView(self, value):
        if isinstance(value, str) and (value in VIEW_IDS or any(value == 'custom-' + row['id'] for row in self.layouts)):
            self.save_options(view=value)

    @Property('QVariantList', constant=True)
    def views(self):
        return [dict(row, name=tr(row["name"]), note=tr(row["note"])) for row in KEYBOARD_VIEWS]

    @Property('QVariantList', constant=True)
    def themes(self):
        return [dict(row, name=tr(row["name"])) for row in INPUT_THEMES]

    @Property('QVariantMap', notify=changed)
    def palette(self):
        options = self.bridge.settings.get('keyboard_options', {})
        value = options.get('theme', 'dark') if isinstance(options, dict) else 'dark'
        return next((dict(theme) for theme in INPUT_THEMES if theme['id'] == value), dict(INPUT_THEMES[0]))

    @Property('QVariantMap', notify=changed)
    def padPalette(self):
        options = self.bridge.settings.get('keyboard_options', {})
        value = options.get('touchpad_theme', 'dark') if isinstance(options, dict) else 'dark'
        return next((dict(theme) for theme in INPUT_THEMES if theme['id'] == value), dict(INPUT_THEMES[0]))

    @Slot(str, bool)
    def setTheme(self, value, touchpads):
        if any(theme['id'] == value for theme in INPUT_THEMES):
            self.save_options(**{'touchpad_theme' if touchpads else 'theme': value})

    @Property('QVariantList', notify=changed)
    def layouts(self):
        values = self.bridge.settings.get('keyboard_layouts', [])
        result = []
        if isinstance(values, list):
            for value in values[:12]:
                try:
                    item = validate_layout(value, (*KEYS, *MODIFIERS))
                    if item['id'] and not any(old['id'] == item['id'] for old in result): result.append(item)
                except ValueError: pass
        return result

    @Property('QVariantMap', notify=changed)
    def customLayout(self):
        options = self.bridge.settings.get('keyboard_options', {})
        view = options.get('view') if isinstance(options, dict) else None
        return next((row for row in self.layouts if view == 'custom-' + row['id']), {})

    @Property('QStringList', constant=True)
    def layoutKeyNames(self):
        return [*KEYS, *MODIFIERS, 'AltGr', '@settings', '@hide', '@language', '@symbols', '@letters', '@touchpads', '@voice']

    @Slot('QVariantMap')
    def editLayout(self, draft):
        try:
            if self.editor: raise ValueError(tr("Close the current editor first."))
            draft = validate_layout(draft, (*KEYS, *MODIFIERS))
            self.setPadEnabled(False)
            self.hide()
            self.editor = 'layout'
            self.layoutEditRequested.emit(draft)
        except ValueError as error:
            self.message = str(error); self.changed.emit()

    @Slot('QVariantMap', result=bool)
    def saveLayout(self, value):
        import uuid
        try:
            value = validate_layout(value, (*KEYS, *MODIFIERS))
            rows = self.layouts
            raw = self.bridge.settings.get('keyboard_layouts', [])
            if not isinstance(raw, list) or len(rows) != len(raw):
                raise ValueError(tr("The saved layout is corrupt or uses another format. Existing data was not overwritten."))
            index = next((i for i, row in enumerate(rows) if row['id'] == value['id']), len(rows))
            if index == len(rows) and len(rows) >= 12: raise ValueError(tr("You can save up to 12 layouts."))
            if not value['id']: value['id'] = uuid.uuid4().hex
            if index == len(rows): rows.append(value)
            else: rows[index] = value
            previous = dict(self.bridge.settings)
            options = self.option_values(view='custom-' + value['id'])
            views = options.get('views', {})
            views = dict(views) if isinstance(views, dict) else {}
            views.setdefault('custom-' + value['id'], {key: self.options[key] for key in ('height_percent', 'keyboard_opacity')})
            self.bridge.settings.update(keyboard_layouts=rows, keyboard_options=dict(options, views=views))
            try: self.bridge.save()
            except (OSError, ValueError):
                self.bridge.settings = previous
                raise
            self.message = ''; self.changed.emit(); return True
        except (ValueError, OSError) as error:
            self.message = str(error); self.changed.emit(); return False

    @Slot(int, int)
    def pressCustom(self, row, column):
        if self.running: return
        try:
            item = self.customLayout['rows'][row][column] if row >= 0 and column >= 0 else None
            if item is None: return
            key = item['key']
            if key in MODIFIERS:
                self.press(key); return
            if key == 'AltGr':
                self.shift_tap = None
                if key in self.modifiers: self.modifiers.remove(key)
                else: self.modifiers.append(key)
                self.feedback(); self.changed.emit(); return
            if key == 'PrintScreen' and not item['states']:
                self.press(key); return
            if key in MODIFIERS or key in ('CapsLock', 'NumLock') or not item['states'] or any(k in self.held for k in ('Ctrl', 'Alt', 'Win')):
                if 'AltGr' in self.modifiers:
                    self.feedback()
                    self.send([*['Ctrl', 'Alt'], *[m for m in self.modifiers if m not in ('AltGr', 'Ctrl', 'Alt')], key])
                    self.changed.emit()
                else: self.press(key)
                return
            base = item['states'].get('base', key if len(key) == 1 and key.isalpha() else '')
            layer = layer_name(self.held, 'CapsLock' in self.locked and len(base) == 1 and base.isalpha())
            if layer not in item['states'] and 'base' not in item['states']:
                if 'AltGr' in self.modifiers:
                    self.feedback()
                    self.send(['Ctrl', 'Alt', *[m for m in self.modifiers if m not in ('AltGr','Ctrl','Alt')], key])
                    self.changed.emit()
                else: self.press(key)
                return
            text = item['states'].get(layer, item['states'].get('base', ''))
            if text and not self.running:
                self.feedback()
                if not self.backend: raise OSError(tr("Input is supported only on Windows."))
                self.backend.text(text)
            self.consume_shift()
            self.message = ''; self.changed.emit()
        except (KeyError, IndexError): return
        except (ValueError, OSError) as error:
            self.message = str(error); self.changed.emit()

    @Slot(float)
    def setOpacity(self, value):
        if .15 <= value <= 1: self.save_options(opacity=value)

    @Slot(float)
    def setTouchpadHeight(self, value):
        if 20 <= value <= 75: self.save_options(touchpad_height_percent=round(value))

    @Slot(float)
    def setPointerSensitivity(self, value):
        if .25 <= value <= 3:
            if self.save_options(pointer_sensitivity=value): self.pointer_fraction = [0., 0.]

    @Slot(bool)
    def setTouchpads(self, enabled):
        if self.remote:
            self.remote.command('setTouchpads', enabled); return
        self.pointer_timer.stop()
        if self.native_touchpads: self.native_touchpads.clear()
        self.touchpads = enabled
        self.pointer_fraction = [0., 0.]
        self.scroll_fraction = 0.
        if enabled: self.layout_timer.stop()
        elif self.shown: self.refreshLayout(); self.layout_timer.start(500)
        self.changed.emit()

    @Slot(float, float)
    def movePointer(self, dx, dy):
        import math
        if not self.shown or not self.touchpads or not all(math.isfinite(v) and abs(v) <= 4096 for v in (dx, dy)): return
        ratio = self.screen.devicePixelRatio() if self.screen else 1
        values = [self.pointer_fraction[i] + delta * ratio * self.options['pointer_sensitivity'] for i, delta in enumerate((dx, dy))]
        movement = [int(v) for v in values]
        self.pointer_fraction = [v - n for v, n in zip(values, movement)]
        if any(movement): self.mouse_input(*movement)

    @Slot(float)
    def scrollPointer(self, dy):
        import math
        if not self.shown or not self.touchpads or not math.isfinite(dy) or abs(dy) > 4096: return
        self.scroll_fraction += dy / 48
        steps = int(self.scroll_fraction)
        if steps:
            self.scroll_fraction -= steps
            try:
                if not self.backend: raise OSError(tr("Scrolling is supported on Windows."))
                self.backend.scroll(steps * 120)
            except OSError as error:
                self.message = str(error); self.changed.emit()

    @Slot(str)
    def clickPointer(self, button):
        if self.shown and self.touchpads and button in ('left', 'right') and self.mouse_input(button=button): self.feedback()

    def mouse_input(self, dx=0, dy=0, button=''):
        try:
            if not self.backend: raise OSError(tr("Mouse input is supported on Windows."))
            self.backend.mouse(dx, dy, button)
            return True
        except OSError as error:
            self.message = str(error)
            self.changed.emit()
            return False

    def restore_pointer(self):
        if self.shown and self.touchpads and self.backend:
            try: self.backend.restore_pointer()
            except OSError as error:
                self.message = str(error); self.changed.emit()

    @Slot(int, str, result=bool)
    def savePad(self, button, value):
        try:
            if button not in GAMEPAD_BUTTONS: raise ValueError(tr("Choose a gamepad button."))
            parts = pad_chord(value) if value.strip() else []
            normalized = {MODIFIER_ALIASES.get(key,key) for key in parts}
            if parts and (parts[-1] == 'Delete' and {'Ctrl', 'Alt'} <= normalized or 'Win' in parts and parts[-1] == 'L'):
                raise ValueError(tr("This system shortcut is unavailable."))
            values = {str(row['button']): row['keys'] for row in self.padRows if row['keys']}
            if parts: values[str(button)] = '+'.join(parts)
            else: values.pop(str(button), None)
            self.release_keys()
            return self.save_value('keyboard_pad', values)
        except ValueError as error:
            self.message = str(error); self.changed.emit(); return False

    @Slot(bool)
    def setPadEnabled(self, enabled):
        if self.remote:
            self.remote.command('setPadEnabled', enabled); return
        self.release_keys()
        if self.pad_down: enabled = False
        if enabled and not any(row['keys'] for row in self.padRows):
            self.message = tr("Assign keys to gamepad buttons first.")
            enabled = False
        self.pad_enabled = enabled
        self.changed.emit()

    @Slot()
    def editPad(self):
        if self.editor:
            self.message = tr("Close the current editor first."); self.changed.emit(); return
        self.setPadEnabled(False)
        self.editor = 'pad'
        self.editRequested.emit()

    @Slot()
    def finishEditor(self):
        self.hide()
        self.editor = None
        if self.remote: self.remote.command('finishEditor')
        self.editorClosed.emit()

    @Slot()
    def pickKeys(self):
        if not self.enabled: return
        if self.remote:
            self.remote.command('pickKeys'); return
        self.touchpads = False
        self.show()
        self.picking = True

    def release_keys(self):
        failed = []
        for key in reversed(self.pad_down):
            try: self.backend.key_event(key, False)
            except OSError as error:
                failed.insert(0, key); self.message = str(error)
        self.pad_down = failed
        self.pad_target = None
        self.pad_neutral = True
        if failed:
            was_enabled = self.pad_enabled
            self.pad_enabled = False
            self.pad_watch.start()
            if was_enabled: self.changed.emit()
        else: self.pad_watch.stop()

    def check_target(self):
        if self.pad_down and (self.pad_target is None or not self.backend or self.backend.foreground() != self.pad_target):
            self.release_keys()

    def gamepad_input(self, buttons, allowed=True):
        if self.remote: return self.pad_enabled
        if not self.pad_enabled or not allowed:
            if self.pad_down: self.release_keys()
            self.pad_neutral = bool(buttons)
            return False
        self.check_target()
        if self.pad_neutral:
            self.pad_neutral = bool(buttons)
            return True
        # No held input needs focus checks or layout expansion while the pad is neutral.
        if not buttons and not self.pad_down: return True
        if not self.backend or not self.backend.foreground(): return True
        target = self.backend.foreground()
        wanted = []
        try:
            for row in self.padRows:
                if row['keys'] and buttons & row['button']:
                    for key in pad_chord(row['keys']):
                        if key not in wanted: wanted.append(key)
            # Shared modifiers remain down until the last assigned button releases them.
            for key in reversed(self.pad_down[:]):
                if key not in wanted:
                    self.backend.key_event(key, False); self.pad_down.remove(key)
            for key in wanted:
                if key not in self.pad_down and not self.backend.physical_down(key):
                    if self.backend.foreground() != target:
                        self.release_keys(); return True
                    self.pad_down.append(key); self.backend.key_event(key, True)
            self.pad_target = target if self.pad_down else None
            self.check_target()
            if self.pad_down: self.pad_watch.start()
            else: self.pad_watch.stop()
        except (ValueError, OSError) as error:
            self.message = str(error); self.pad_enabled = False; self.release_keys(); self.changed.emit()
        return True

    def disconnected(self, connected):
        if not connected: self.release_keys()

    @Slot()
    def close(self):
        self.pad_enabled = False
        self.release_keys()
        self.hide()
        if self.native_touchpads:
            QGuiApplication.instance().removeNativeEventFilter(self.native_touchpads)
            self.native_touchpads = None
            self.changed.emit()

    @Property('QVariantList', notify=macrosChanged)
    def macros(self):
        values = self.bridge.settings.get('keyboard_macros', [])
        result = []
        if isinstance(values, list):
            for value in values[:20]:
                if isinstance(value, dict) and set(value) == {'name', 'sequence'} and isinstance(value['name'], str) and 1 <= len(value['name'].strip()) <= 40:
                    try:
                        macro_steps(value['sequence'])
                        result.append(dict(value))
                    except ValueError:
                        pass
        return result

    @Slot()
    def toggle(self):
        self.set_visible(not self.shown)

    @Slot()
    def show(self):
        if not self.enabled: return
        self.touchpads = False
        self.set_visible(True)

    @Slot()
    def hide(self):
        self.set_visible(False)

    def set_visible(self, value):
        if value and not self.enabled and not self.touchpads: return
        if self.remote:
            self.visibilityRequested.emit(value)
            self.remote.command('show' if value else 'hide'); return
        self.screenshot_timer.stop(); self.screenshot_request = None
        self.pointer_timer.stop()
        self.visibilityRequested.emit(value)
        self.shown = value
        if not value: self.picking = False
        self.cancel()
        if not value and self.native_touchpads: self.native_touchpads.clear()
        self.modifiers = []
        self.shift_locked = False; self.shift_tap = None
        locks = self.backend.locks() if self.backend else []
        self.locked = locks if isinstance(locks, list) else []
        self.message = ''
        self.layout_pending = None
        if value and not self.touchpads: self.refreshLayout(); self.layout_timer.start(500)
        else: self.layout_timer.stop()
        self.changed.emit()

    @Slot(str)
    def press(self, key):
        if self.running:
            return
        if key == 'PrintScreen' and not self.picking:
            self.takeScreenshot(False); return
        self.feedback()
        if key in MODIFIERS:
            equivalent = [name for name in self.modifiers if MODIFIER_ALIASES.get(name,name) == MODIFIER_ALIASES.get(key,key)]
            if key in ('Shift', 'RightShift'):
                now = time.monotonic()
                if equivalent and not self.shift_locked and self.shift_tap is not None and now - self.shift_tap <= .5:
                    self.shift_locked = True; self.shift_tap = None
                elif equivalent:
                    self.modifiers = [name for name in self.modifiers if name not in equivalent]
                    self.shift_locked = False; self.shift_tap = None
                else:
                    self.modifiers.append(key)
                    self.shift_locked = False; self.shift_tap = now
            elif equivalent:
                self.modifiers = [name for name in self.modifiers if name not in equivalent]
            else:
                self.modifiers.append(key)
            if key not in ('Shift', 'RightShift'): self.shift_tap = None
        else:
            if self.picking:
                value = '+'.join([*self.modifiers, key])
                self.hide()
                self.chordPicked.emit(value)
                return
            if self.send([*self.modifiers, key]) and key in ('CapsLock', 'NumLock'):
                if key in self.locked:
                    self.locked.remove(key)
                else:
                    self.locked.append(key)
        self.changed.emit()

    @Slot(bool)
    def takeScreenshot(self, full_screen):
        if self.running or self.screenshot_timer.isActive(): return
        if self.picking:
            self.press('PrintScreen'); return
        screen = self.screen or QGuiApplication.primaryScreen()
        modifiers = ['Ctrl', 'Alt', *[key for key in self.modifiers if key not in ('AltGr', 'Ctrl', 'Alt')]] if 'AltGr' in self.modifiers else self.modifiers
        parts = [*modifiers, 'PrintScreen']
        self.feedback(); self.hide()
        self.screenshot_request = (full_screen, screen, parts)
        self.screenshot_timer.start()
        self.changed.emit()

    @Slot()
    def voiceInput(self):
        if self.running or self.screenshot_timer.isActive(): return
        self.feedback(); self.hide()
        self.screenshot_request = (False, None, ['Win', 'H'])
        self.screenshot_timer.start(); self.changed.emit()

    def finish_screenshot(self):
        request, self.screenshot_request = self.screenshot_request, None
        if request is None or self.shown or getattr(self.bridge, '_sleeping', False) or getattr(self.bridge, '_closing', False): return
        full_screen, screen, parts = request
        try:
            if full_screen:
                picture = screen.grabWindow(0) if screen else None
                if picture is None or picture.isNull(): raise OSError(tr("Could not take a screenshot."))
                QGuiApplication.clipboard().setPixmap(picture)
                self.message = ''
            elif not self.send(parts): raise OSError(self.message)
        except (OSError, RuntimeError) as error:
            self.show(); self.message = str(error)
        self.changed.emit()

    def send(self, parts):
        try:
            chord('+'.join(parts))
            if not self.backend:
                raise OSError(tr("Keyboard input is supported on Windows."))
            self.backend.send(parts)
            self.consume_shift()
            self.message = ''
            return True
        except (ValueError, OSError) as error:
            self.message = str(error)
            return False

    def consume_shift(self):
        self.shift_tap = None
        if not self.shift_locked:
            self.modifiers = [key for key in self.modifiers if key not in ('Shift', 'RightShift')]

    @Slot(str)
    def shortcut(self, value):
        if not self.running:
            try:
                self.send(chord(value))
            except ValueError as error:
                self.message = str(error)
            self.changed.emit()

    @Slot()
    def clearModifiers(self):
        if self.remote:
            self.remote.command('clearModifiers'); return
        self.modifiers = []
        self.shift_locked = False; self.shift_tap = None
        self.changed.emit()

    @Slot(str, str, result=bool)
    def saveMacro(self, name, sequence):
        previous = self.bridge.settings.get('keyboard_macros')
        try:
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise ValueError(tr("Macro names must contain 1 to 40 characters."))
            macro_steps(sequence)
            values = self.macros
            existing = next((i for i, item in enumerate(values) if item['name'] == name.strip()), None)
            if existing is None:
                if len(values) >= 20:
                    raise ValueError(tr("You can save up to 20 macros."))
                values.append(dict(name=name.strip(), sequence=sequence.strip()))
            else:
                values[existing] = dict(name=name.strip(), sequence=sequence.strip())
            self.bridge.settings['keyboard_macros'] = values
            self.bridge.save()
            self.macrosChanged.emit()
            self.message = tr("Macro saved.")
            return True
        except (ValueError, OSError) as error:
            if previous is None:
                self.bridge.settings.pop('keyboard_macros', None)
            else:
                self.bridge.settings['keyboard_macros'] = previous
            self.message = str(error)
            return False
        finally:
            self.changed.emit()

    @Slot(str)
    def removeMacro(self, name):
        previous = self.bridge.settings.get('keyboard_macros', [])
        try:
            self.bridge.settings['keyboard_macros'] = [item for item in self.macros if item['name'] != name]
            self.bridge.save()
            self.macrosChanged.emit()
            self.message = tr("Macro deleted.")
        except OSError as error:
            self.bridge.settings['keyboard_macros'] = previous
            self.message = str(error)
        self.changed.emit()

    @Slot(str)
    def runMacro(self, name):
        self.cancel()
        value = next((item for item in self.macros if item['name'] == name), None)
        if value and self.backend:
            self.steps = macro_steps(value['sequence'])
            self.target = self.backend.foreground()
            if not self.target:
                self.steps = []
                self.message = tr("No active window for input.")
            else:
                self.next_step()
        self.changed.emit()

    def next_step(self):
        if not self.backend or self.backend.foreground() != self.target:
            self.cancel()
            self.message = tr("The input window changed. Macro stopped.")
        elif self.steps:
            step = self.steps.pop(0)
            if isinstance(step, int):
                self.timer.start(step)
            elif self.send(step):
                self.timer.start(50)
            else:
                self.cancel()
        self.changed.emit()

    @Slot()
    def cancel(self):
        if self.remote:
            self.remote.command('cancel'); return
        self.timer.stop()
        self.steps = []
        self.changed.emit()
