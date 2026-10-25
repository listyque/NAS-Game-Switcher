"""Xbox-compatible controllers through Windows XInput; no extra dependency."""

import ctypes
import sys
import time

from PySide6.QtCore import QObject, QTimer, Signal

GAMEPAD_BUTTONS = {0x1: '↑', 0x2: '↓', 0x4: '←', 0x8: '→', 0x10: 'Start',
                   0x20: 'Back', 0x40: 'L3', 0x80: 'R3', 0x100: 'LB', 0x200: 'RB',
                   0x1000: 'A', 0x2000: 'B', 0x4000: 'X', 0x8000: 'Y',
                   0x10000: 'LT', 0x20000: 'RT'}
GAMEPAD_MASK = sum(GAMEPAD_BUTTONS)


class _Gamepad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_uint16), ("left_trigger", ctypes.c_uint8),
                ("right_trigger", ctypes.c_uint8), ("left_x", ctypes.c_int16),
                ("left_y", ctypes.c_int16), ("right_x", ctypes.c_int16), ("right_y", ctypes.c_int16)]


class _State(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint32), ("gamepad", _Gamepad)]


class _Vibration(ctypes.Structure):
    _fields_ = [("left", ctypes.c_uint16), ("right", ctypes.c_uint16)]


class ButtonRepeater:
    def __init__(self, initial_delay=0.40, repeat_interval=0.13):
        self.held = set()
        self.next_repeat = {}
        self.initial_delay = float(initial_delay)
        self.repeat_interval = float(repeat_interval)

    def update(self, pressed, now):
        actions = []
        for action in sorted(pressed):
            if action not in self.held:
                actions.append(action)
                self.next_repeat[action] = now + self.initial_delay
            elif action in ("left", "right", "up", "down") and now >= self.next_repeat[action]:
                actions.append(action)
                self.next_repeat[action] = now + self.repeat_interval
        self.held = set(pressed)
        self.next_repeat = {action: deadline for action, deadline in self.next_repeat.items() if action in pressed}
        return actions


def actions_for_state(buttons, x, y, deadzone=16000):
    mapping = {0x0001: "up", 0x0002: "down", 0x0004: "left", 0x0008: "right",
               0x0010: "menu", 0x0020: "back", 0x0100: "previous_section",
               0x0200: "next_section", 0x1000: "activate", 0x2000: "back", 0x4000: "toggle_installed", 0x8000: "refresh",
               0x10000: "previous_control", 0x20000: "next_control"}
    actions = {action for mask, action in mapping.items() if buttons & mask}
    threshold = max(4000, min(28000, int(deadzone or 16000)))
    if abs(x) >= threshold or abs(y) >= threshold:
        if abs(x) >= abs(y):
            actions.add("right" if x > 0 else "left")
        else:
            actions.add("up" if y > 0 else "down")
    for first, second in (("left", "right"), ("up", "down"), ("previous_control", "next_control")):
        if first in actions and second in actions:
            actions.difference_update((first, second))
    return actions


class GamepadInput(QObject):
    action = Signal(str)
    connectionChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.get_state = None
        self.set_state = None
        self.vibrating_index = None
        self.vibration_timer = QTimer(self)
        self.vibration_timer.setSingleShot(True)
        self.vibration_timer.timeout.connect(self.stop_vibration)
        self.connected = False
        self.index = None
        self.active = False
        self.background = False
        self.button_handler = None
        self.wait_for_neutral = True
        self.vibration_enabled = True
        self.deadzone = 16000
        self.initial_delay = 0.40
        self.repeat_interval = 0.13
        self.repeater = ButtonRepeater(self.initial_delay, self.repeat_interval)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        # shortcut: XInput supports Xbox-compatible pads; add SDL when another pad is required.
        if sys.platform == "win32":
            for library in ("xinput1_4.dll", "xinput9_1_0.dll"):
                try:
                    self.dll = ctypes.WinDLL(library)
                    self.get_state = self.dll.XInputGetState
                    self.get_state.argtypes = [ctypes.c_uint32, ctypes.POINTER(_State)]
                    self.get_state.restype = ctypes.c_uint32
                    self.set_state = self.dll.XInputSetState
                    self.set_state.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Vibration)]
                    self.set_state.restype = ctypes.c_uint32
                    break
                except OSError:
                    pass

    def configure(self, vibration=True, deadzone=16000, repeat_speed="normal"):
        self.vibration_enabled = bool(vibration)
        if not self.vibration_enabled:
            self.stop_vibration()
        self.deadzone = max(4000, min(28000, int(deadzone or 16000)))
        delays = {"fast": (0.28, 0.085), "slow": (0.50, 0.20)}.get(repeat_speed, (0.40, 0.13))
        self.initial_delay, self.repeat_interval = delays
        self.repeater.initial_delay = self.initial_delay
        self.repeater.repeat_interval = self.repeat_interval

    def set_active(self, active, background=False):
        if not active: self.stop_vibration()
        if (active, background) == (self.active, self.background):
            return
        self.active = active
        self.background = background
        self.repeater = ButtonRepeater(self.initial_delay, self.repeat_interval)
        self.wait_for_neutral = True
        if (active or background) and self.get_state:
            self.timer.start(16 if self.connected else 2000)
            self.poll()
        else:
            self.timer.stop()

    def vibrate(self, duration=18):
        if not self.vibration_enabled or self.index is None or not self.connected or not self.set_state: return
        if self.vibrating_index is not None and self.vibrating_index != self.index: self.stop_vibration()
        value = _Vibration(7000, 10000)
        if self.set_state(self.index, ctypes.byref(value)) == 0:
            self.vibrating_index = self.index
            self.vibration_timer.start(max(8, min(int(duration), 40)))

    def stop_vibration(self):
        self.vibration_timer.stop()
        if self.vibrating_index is not None and self.set_state:
            self.set_state(self.vibrating_index, ctypes.byref(_Vibration()))
        self.vibrating_index = None

    def poll(self):
        if not (self.active or self.background) or not self.get_state:
            return
        state = _State()
        indices = range(4) if self.index is None else (self.index,)
        index = next((i for i in indices if self.get_state(i, ctypes.byref(state)) == 0), None)
        connected = index is not None
        if connected != self.connected:
            if not connected: self.stop_vibration()
            self.connected = connected
            self.connectionChanged.emit(connected)
            self.repeater = ButtonRepeater(self.initial_delay, self.repeat_interval)
            self.wait_for_neutral = True
            self.timer.setInterval(16 if connected else 2000)
        self.index = index
        if connected:
            pad = state.gamepad
            buttons = pad.buttons & GAMEPAD_MASK
            if pad.left_trigger >= 128:
                buttons |= 0x10000
            if pad.right_trigger >= 128:
                buttons |= 0x20000
            pressed = actions_for_state(buttons, pad.left_x, pad.left_y, self.deadzone)
            if self.wait_for_neutral:
                self.wait_for_neutral = bool(pressed or buttons)
                if not self.wait_for_neutral and self.button_handler:
                    self.button_handler(0)
                return
            consumed = self.button_handler(buttons) if self.button_handler else False
            if consumed or not self.active:
                self.repeater = ButtonRepeater(self.initial_delay, self.repeat_interval)
                return
            for action in self.repeater.update(pressed, time.monotonic()):
                self.action.emit(action)
