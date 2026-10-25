import unittest
import ctypes
from unittest.mock import Mock

from ngs.gamepad import ButtonRepeater, GamepadInput, _State, _Vibration, actions_for_state


class GamepadTests(unittest.TestCase):
    def test_haptics_are_bounded_stop_original_controller_and_stop_on_sleep(self):
        gamepad = GamepadInput()
        gamepad.connected = True; gamepad.index = 2
        gamepad.vibration_timer = Mock()
        values = []
        def state(index, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(_Vibration)).contents
            values.append((index, value.left, value.right)); return 0
        gamepad.set_state = state
        gamepad.vibrate(1000)
        gamepad.vibration_timer.start.assert_called_with(40)
        gamepad.index = 1
        gamepad.stop_vibration()
        self.assertEqual(values, [(2, 7000, 10000), (2, 0, 0)])
        gamepad.vibrate(); gamepad.set_active(False)
        self.assertEqual(values[-1], (1, 0, 0))
        gamepad.connected = False
        count = len(values); gamepad.vibrate()
        self.assertEqual(len(values), count)

    def test_deadzone_and_opposite_directions(self):
        self.assertEqual(actions_for_state(0, 9000, -5000), set())
        self.assertEqual(actions_for_state(0, 12000, 0, deadzone=10000), {"right"})
        self.assertEqual(actions_for_state(0, 18000, 0, deadzone=22000), set())
        self.assertEqual(actions_for_state(0, 21000, 17000), {"right"})
        self.assertEqual(actions_for_state(0, 0, -20000), {"down"})
        self.assertEqual(actions_for_state(0x0004 | 0x0008, 0, 0), set())
        self.assertEqual(actions_for_state(0x1000 | 0x0200, 0, 0), {"activate", "next_section"})
        self.assertEqual(actions_for_state(0x8000, 0, 0), {"refresh"})
        self.assertEqual(actions_for_state(0x4000, 0, 0), {"toggle_installed"})
        gamepad = GamepadInput()
        gamepad.configure(vibration=False, deadzone=22000, repeat_speed="fast")
        self.assertFalse(gamepad.vibration_enabled)
        self.assertEqual(gamepad.deadzone, 22000)
        self.assertEqual((gamepad.repeater.initial_delay, gamepad.repeater.repeat_interval), (0.28, 0.085))

    def test_hold_repeats_navigation_but_never_activation(self):
        repeat = ButtonRepeater()
        self.assertEqual(set(repeat.update({"right", "activate"}, 0)), {"right", "activate"})
        self.assertEqual(repeat.update({"right", "activate"}, 0.2), [])
        self.assertEqual(repeat.update({"right", "activate"}, 0.41), ["right"])
        self.assertEqual(repeat.update({"right", "activate"}, 0.45), [])
        self.assertEqual(repeat.update({"right", "activate"}, 0.55), ["right"])
        repeat.update(set(), 1)
        self.assertEqual(repeat.update({"activate"}, 1.1), ["activate"])

    def test_triggers_move_controls_once_and_respect_capture_and_neutral(self):
        gamepad = GamepadInput()
        gamepad.timer = Mock()
        values = {'left_trigger': 255, 'right_trigger': 0}
        def read(index, pointer):
            if index:
                return 1167
            pad = ctypes.cast(pointer, ctypes.POINTER(_State)).contents.gamepad
            for key, value in values.items():
                setattr(pad, key, value)
            return 0
        gamepad.get_state = read
        actions = []
        gamepad.action.connect(actions.append)
        gamepad.set_active(True)
        self.assertEqual(actions, [])
        values['left_trigger'] = 0; gamepad.poll()
        values['right_trigger'] = 127; gamepad.poll()
        self.assertEqual(actions, [])
        values['right_trigger'] = 128; gamepad.poll(); gamepad.poll()
        self.assertEqual(actions, ['next_control'])
        values['right_trigger'] = 0; gamepad.poll()
        values['left_trigger'] = 128; gamepad.poll()
        self.assertEqual(actions, ['next_control', 'previous_control'])
        values['right_trigger'] = 128; gamepad.poll()
        self.assertEqual(actions, ['next_control', 'previous_control'])
        gamepad.button_handler = lambda buttons: True
        values['left_trigger'] = 0; gamepad.poll()
        self.assertEqual(actions, ['next_control', 'previous_control'])
        gamepad.button_handler = lambda buttons: False
        values['right_trigger'] = 0
        gamepad.set_active(False, background=True)
        values['right_trigger'] = 255; gamepad.poll()
        self.assertEqual(actions, ['next_control', 'previous_control'])
        gamepad.set_active(False)

    def test_hotplug_and_focus_require_releasing_held_buttons(self):
        gamepad = GamepadInput()
        gamepad.timer = Mock()
        state = {"buttons": 0x1000, "connected": True}
        calls = []
        def read(index, pointer):
            calls.append(index)
            if not state["connected"] or index != 2:
                return 1167
            ctypes.cast(pointer, ctypes.POINTER(_State)).contents.gamepad.buttons = state["buttons"]
            return 0
        gamepad.get_state = read
        actions = []
        gamepad.action.connect(actions.append)
        gamepad.set_active(True)
        self.assertTrue(gamepad.connected)
        self.assertEqual(gamepad.index, 2)
        self.assertEqual(actions, [])
        state["buttons"] = 0
        gamepad.poll()
        state["buttons"] = 0x1000
        gamepad.poll()
        self.assertEqual(actions, ["activate"])
        gamepad.set_active(False)
        calls_before = len(calls)
        gamepad.poll()
        self.assertEqual(len(calls), calls_before)
        gamepad.set_active(True)
        self.assertEqual(actions, ["activate"])
        state["buttons"] = 0
        gamepad.poll()
        state["buttons"] = 0x1000
        gamepad.poll()
        self.assertEqual(actions, ["activate", "activate"])
        state["connected"] = False
        gamepad.poll()
        self.assertFalse(gamepad.connected)
        gamepad.timer.setInterval.assert_called_with(2000)

    def test_raw_buttons_capture_triggers_and_background_never_routes_navigation(self):
        gamepad = GamepadInput()
        gamepad.timer = Mock()
        state = {'buttons': 0xc0, 'left_trigger': 0, 'right_trigger': 0}
        def read(index, pointer):
            if index:
                return 1167
            pad = ctypes.cast(pointer, ctypes.POINTER(_State)).contents.gamepad
            for key, value in state.items():
                setattr(pad, key, value)
            return 0
        gamepad.get_state = read
        raw, actions = [], []
        gamepad.button_handler = lambda buttons: raw.append(buttons) or True
        gamepad.action.connect(actions.append)
        gamepad.set_active(False, background=True)
        self.assertEqual(raw, [])  # Held at startup/reconnect must not fire a shortcut.
        state['buttons'] = 0; gamepad.poll()
        state.update(buttons=0x1000, left_trigger=128, right_trigger=255); gamepad.poll()
        self.assertEqual(raw, [0, 0x31000])
        self.assertEqual(actions, [])
        state.update(buttons=0, left_trigger=0, right_trigger=0)
        gamepad.set_active(True, background=True)
        state['buttons'] = 0x2000; gamepad.poll()
        self.assertEqual(raw[-1], 0x2000)
        self.assertEqual(actions, [])  # Captured B does not also close a dialog.
        gamepad.button_handler = lambda buttons: False
        state['buttons'] = 0; gamepad.poll()
        state['buttons'] = 0x1000; gamepad.poll()
        self.assertEqual(actions, ['activate'])
        gamepad.set_active(False)
        self.assertFalse(gamepad.background)
        gamepad.timer.stop.assert_called()
