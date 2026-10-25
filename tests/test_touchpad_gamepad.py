import ctypes
import unittest
from unittest.mock import Mock
from PySide6.QtCore import QObject, QPoint, QUrl
from PySide6.QtQml import QQmlComponent
from PySide6.QtTest import QTest
from ngs.gamepad import GAMEPAD_BUTTONS, GamepadInput, _State
from ngs.input_host import ControllerGate, HostAppearance
from tests import test_ui


class TouchpadGamepadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def exercise(self, owner, gamepad):
        keyboard = self.engine.bridge.keyboard
        left = owner.findChild(QObject, 'leftTouchpadWindow')
        right = owner.findChild(QObject, 'rightTouchpadWindow')
        keys = owner.findChild(QObject, 'virtualKeyboardWindow')
        previous = keyboard.backend, gamepad.get_state, gamepad.set_state
        keyboard.backend = Mock()
        keyboard.backend.foreground.return_value = 42
        keyboard.backend.physical_down.return_value = False
        state = [0, 0, 0]
        def get_state(index, output):
            pad = ctypes.cast(output, ctypes.POINTER(_State)).contents.gamepad
            pad.buttons = state[0] & 0xffff
            pad.left_trigger = 255 if state[0] & 0x10000 else 0
            pad.right_trigger = 255 if state[0] & 0x20000 else 0
            pad.left_x, pad.left_y = state[1:]
            return 0
        gamepad.get_state, gamepad.set_state = get_state, None
        try:
            keyboard.show(); keyboard.setTouchpads(True); QTest.qWait(50)
            gamepad.set_active(True); gamepad.poll()
            for buttons, x, y in [(b, 0, 0) for b in GAMEPAD_BUTTONS] + [(0, -22000, 0), (0, 22000, 0), (0, 0, -22000), (0, 0, 22000)]:
                with self.subTest(buttons=buttons, x=x, y=y):
                    state[:] = buttons, x, y
                    gamepad.poll(); self.app.processEvents()
                    self.assertTrue(left.isVisible() and right.isVisible())
                    self.assertFalse(keys.isVisible())
                    self.assertFalse(left.property('controlsOpen'))
                    state[:] = 0, 0, 0; gamepad.poll()
            keyboard.backend.mouse.assert_not_called()
            keyboard.backend.send.assert_not_called()
            # Both pads still accept a real Qt touch tap on their toolbar.
            device = QTest.createTouchDevice()
            for window, index in ((left, 0), (right, 2)):
                rect = next(r for r in window.property('toolRects').toVariant() if r['index'] == index)
                point = QPoint(round(rect['x']+rect['width']/2), round(rect['y']+rect['height']/2))
                QTest.touchEvent(window, device).press(0, point, window).commit(); QTest.qWait(20)
                QTest.touchEvent(window, device).release(0, point, window).commit(); QTest.qWait(30)
                self.assertFalse(left.isVisible() or right.isVisible())
                if index == 0:
                    self.assertFalse(keyboard.touchpadMode)
                    self.assertTrue(keys.isVisible())
                    keyboard.setTouchpads(True); QTest.qWait(30)
                else: self.assertFalse(keyboard.visible)
            keyboard.show(); QTest.qWait(30)
            state[:] = 0x2000, 0, 0; gamepad.poll()
            self.assertFalse(keyboard.visible)  # B still closes the actual keyboard.
        finally:
            gamepad.set_active(False); keyboard.hide()
            keyboard.backend, gamepad.get_state, gamepad.set_state = previous

    def test_launcher_touchpads_survive_all_gamepad_buttons_and_stick(self):
        self.exercise(self.window, self.engine.gamepad)

    def test_input_host_touchpads_survive_all_gamepad_buttons_and_stick(self):
        keyboard = self.engine.bridge.keyboard
        appearance = HostAppearance()
        component = QQmlComponent(self.engine, QUrl.fromLocalFile(str(self.repo/'qml/InputHost.qml')))
        host = component.createWithInitialProperties(dict(keyboard=keyboard, appearance=appearance, statusOverlay=self.engine.bridge.status_overlay))
        self.assertIsNotNone(host, component.errors())
        gamepad = GamepadInput()
        gamepad.button_handler = ControllerGate(keyboard)
        gamepad.action.connect(keyboard.action)
        try: self.exercise(host, gamepad)
        finally:
            gamepad.set_active(False); host.deleteLater(); self.app.processEvents()
