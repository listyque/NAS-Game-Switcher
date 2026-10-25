import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_QUICK_BACKEND'] = 'software'

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent, QWindow
from PySide6.QtCore import QEvent

from ngs.desktop import DesktopControls, WindowsDesktop, WindowsKeyCapture, _KeyboardEvent, _NativeEvents, WM_TRAY, binding
from ngs.portable import PortableStorage
from ngs.virtual_keyboard import VirtualKeyboard, WindowsKeyboard, _Input, _GuiThreadInfo, chord, macro_steps


class Bridge(QObject):
    def __init__(self, root):
        super().__init__()
        self.storage = PortableStorage(root)
        self.settings = self.storage.read_settings()

    def save(self):
        self.storage.write_settings(self.settings)


class DesktopTests(unittest.TestCase):
    def test_pad_presets_replace_atomically_release_keys_and_stay_disabled(self):
        keyboard = self.keyboard
        keyboard.backend.physical_down.return_value = False
        self.assertTrue(keyboard.savePad(0x1000, 'Ctrl+C'))
        keyboard.setPadEnabled(True); keyboard.gamepad_input(0); keyboard.gamepad_input(0x1000)
        self.assertEqual(keyboard.pad_down, ['Ctrl', 'C'])
        self.assertEqual({preset['id'] for preset in keyboard.padPresets}, {'wasd', 'arrows', 'racing'})
        self.assertEqual(self.bridge.settings['keyboard_pad'], {'4096': 'Ctrl+C'})
        for preset in keyboard.padPresets:
            self.assertTrue(keyboard.applyPadPreset(preset['id']))
            self.assertFalse(keyboard.padEnabled)
            self.assertEqual(keyboard.pad_down, [])
            expected = {str(row['button']): row['keys'] for row in preset['rows']}
            self.assertEqual(self.bridge.storage.read_settings()['keyboard_pad'], expected)
            self.assertEqual(len(expected), 16)
        previous = dict(self.bridge.settings['keyboard_pad'])
        self.assertFalse(keyboard.applyPadPreset('not-a-preset'))
        self.assertEqual(self.bridge.settings['keyboard_pad'], previous)
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            self.assertFalse(keyboard.applyPadPreset('arrows'))
        self.assertEqual(self.bridge.settings['keyboard_pad'], previous)
        self.assertEqual(self.bridge.storage.read_settings()['keyboard_pad'], previous)

    def test_pad_preset_does_not_replace_bindings_if_owned_key_cannot_be_released(self):
        keyboard = self.keyboard
        keyboard.savePad(0x1000, 'W'); keyboard.pad_down = ['W']
        keyboard.backend.key_event.side_effect = OSError('release failed')
        with patch.object(self.bridge, 'save') as save:
            self.assertFalse(keyboard.applyPadPreset('wasd'))
            save.assert_not_called()
        self.assertEqual(self.bridge.settings['keyboard_pad'], {'4096': 'W'})
        self.assertFalse(keyboard.padEnabled)
        keyboard.backend.key_event.side_effect = None; keyboard.release_keys()
    def test_input_hints_ignore_injection_modifiers_and_service_shortcuts(self):
        import ctypes
        from ngs.desktop import _InputMessageSource, MASK_EXTRA
        from ngs.virtual_keyboard import NGS_INPUT_EXTRA
        controls = self.controls
        native = controls.backend = Mock(hwnd=42, taskbar_message=9999)
        user = native.user
        received = []
        controls.keyboardInput.connect(lambda: received.append('keyboard'))
        state = dict(origin=1, extra=0, mods=set())
        def source(pointer):
            ctypes.cast(pointer, ctypes.POINTER(_InputMessageSource)).contents.origin = state['origin']
            return True
        user.GetCurrentInputMessageSource.side_effect = source
        user.GetMessageExtraInfo.side_effect = lambda: state['extra']
        user.GetKeyState.side_effect = lambda vk: 0x8000 if vk in state['mods'] else 0
        controls.native_message(42, 0x100, 39, 0)
        self.assertEqual(received, ['keyboard'])
        received.clear()
        for origin, extra in ((2, 0), (4, 0), (0, 0), (1, NGS_INPUT_EXTRA), (1, MASK_EXTRA)):
            state.update(origin=origin, extra=extra)
            controls.native_message(42, 0x100, 39, 0)
        state.update(origin=1, extra=0)
        for vk in (16, 17, 18, 91, 92): controls.native_message(42, 0x100, vk, 0)
        state['mods'] = {91}
        controls.native_message(42, 0x100, 68, 0)  # OEM Win+D
        state['mods'] = {91, 17}
        controls.native_message(42, 0x100, 79, 0)  # OEM Win+Ctrl+O
        self.bridge.settings['shortcuts'] = {'show': dict(vk=119, mods=6)}
        state['mods'] = {17, 16}
        controls.native_message(42, 0x100, 119, 0)
        self.assertEqual(received, [])
        controls.native_message(42, 0x100, 67, 0)  # An ordinary keyboard shortcut still counts.
        self.assertEqual(received, ['keyboard'])

    @classmethod
    def setUpClass(cls):
        PortableStorage(Path(__file__).resolve().parents[1]).configure_process()
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'data/tmp')
        self.bridge = Bridge(Path(self.temporary.name))
        self.controls = DesktopControls(self.bridge)
        self.keyboard = VirtualKeyboard(self.bridge)
        self.keyboard.backend = Mock()
        self.keyboard.backend.foreground.return_value = 42

    def tearDown(self):
        self.controls.close()
        self.keyboard.close()
        self.temporary.cleanup()

    def test_pad_keyboard_holds_shared_modifiers_background_neutral_and_focus_release(self):
        keyboard, backend = self.keyboard, self.keyboard.backend
        backend.physical_down.return_value = False
        self.assertTrue(keyboard.savePad(0x1000, 'Ctrl+W'))
        self.assertTrue(keyboard.savePad(0x2000, 'Ctrl+S'))
        self.assertTrue(keyboard.savePad(0x100, 'Shift'))
        keyboard.setPadEnabled(True)
        keyboard.hide()  # Mapping survives a hidden keyboard.
        self.assertTrue(keyboard.padEnabled)
        keyboard.gamepad_input(0x1000)
        backend.key_event.assert_not_called()
        keyboard.gamepad_input(0)
        keyboard.gamepad_input(0x1000); keyboard.gamepad_input(0x1000)
        self.assertEqual(backend.key_event.call_args_list, [unittest.mock.call('Ctrl', True), unittest.mock.call('W', True)])
        keyboard.gamepad_input(0x3000)
        backend.key_event.assert_called_with('S', True)
        keyboard.gamepad_input(0x2000)
        backend.key_event.assert_called_with('W', False)
        self.assertIn('Ctrl', keyboard.pad_down)
        backend.foreground.return_value = 43
        keyboard.check_target()
        self.assertEqual(keyboard.pad_down, [])
        self.assertEqual(backend.key_event.call_args_list[-2:], [unittest.mock.call('S', False), unittest.mock.call('Ctrl', False)])
        count = backend.key_event.call_count
        keyboard.gamepad_input(0x2000)
        self.assertEqual(backend.key_event.call_count, count)
        keyboard.gamepad_input(0); keyboard.gamepad_input(0x100)
        backend.key_event.assert_called_with('Shift', True)
        keyboard.disconnected(False)
        backend.key_event.assert_called_with('Shift', False)
        keyboard.gamepad_input(0); keyboard.gamepad_input(0x1000, allowed=False)
        self.assertEqual(keyboard.pad_down, [])
        self.assertTrue(keyboard.padEnabled)
        keyboard.setPadEnabled(False)
        self.assertEqual(VirtualKeyboard(self.bridge).padEnabled, False)
        self.assertEqual(self.bridge.storage.read_settings()['keyboard_pad']['256'], 'Shift')

    def test_pad_keyboard_failed_input_save_and_physical_modifiers_are_safe(self):
        keyboard, backend = self.keyboard, self.keyboard.backend
        backend.physical_down.side_effect = lambda key: key == 'Ctrl'
        self.assertTrue(keyboard.savePad(0x1000, 'Ctrl+A'))
        for button, value in ((123, 'A'), (0x1000, 'Ctrl+NoKey'), (0x1000, 'Win+L'), (0x1000, 'Ctrl+Alt+Delete')):
            self.assertFalse(keyboard.savePad(button, value))
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            self.assertFalse(keyboard.savePad(0x1000, 'Shift+A'))
            keyboard.setSize(2)
        self.assertEqual(keyboard.options['size'], 1)
        self.assertEqual(next(row['keys'] for row in keyboard.padRows if row['button'] == 0x1000), 'Ctrl+A')
        keyboard.setPadEnabled(True); keyboard.gamepad_input(0); keyboard.gamepad_input(0x1000)
        self.assertEqual(keyboard.pad_down, ['A'])
        keyboard.gamepad_input(0)
        self.assertEqual(backend.key_event.call_args_list, [unittest.mock.call('A', True), unittest.mock.call('A', False)])
        backend.key_event.reset_mock()
        def fail(key, down):
            if down: raise OSError('UIPI rejected')
        backend.key_event.side_effect = fail
        keyboard.gamepad_input(0x1000)
        self.assertFalse(keyboard.padEnabled)
        self.assertEqual(keyboard.pad_down, [])
        backend.key_event.assert_called_with('A', False)

    def test_touchpad_relative_input_dpi_fraction_and_pick_without_injection(self):
        keyboard = self.keyboard
        keyboard.screen = Mock()
        keyboard.screen.devicePixelRatio.return_value = 2
        keyboard.show(); keyboard.setTouchpads(True)
        keyboard.movePointer(.2, -.2); keyboard.movePointer(.4, -.4)
        keyboard.backend.mouse.assert_called_once_with(1, -1, '')
        keyboard.movePointer(float('nan'), 1); keyboard.movePointer(5000, 1)
        self.assertEqual(keyboard.backend.mouse.call_count, 1)
        keyboard.clickPointer('left'); keyboard.backend.mouse.assert_called_with(0, 0, 'left')
        keyboard.hide(); keyboard.movePointer(10, 10)
        self.assertEqual(keyboard.backend.mouse.call_count, 2)
        picked = []
        keyboard.chordPicked.connect(picked.append)
        keyboard.pickKeys(); keyboard.press('Ctrl'); keyboard.press('C')
        self.assertEqual(picked, ['Ctrl+C'])
        self.assertFalse(keyboard.visible)
        keyboard.backend.send.assert_not_called()

    def test_overlay_restack_notification_is_limited_to_launcher_window(self):
        controls = self.controls
        controls.backend = Mock(hwnd=42, taskbar_message=9999)
        changed = []
        controls.windowStackChanged.connect(lambda: changed.append(True))
        self.assertFalse(controls.native_message(99, 0x47, 0, 0))
        self.assertFalse(controls.native_message(42, 0x47, 0, 0))
        self.assertEqual(changed, [True])

    def test_native_held_key_mouse_layout_and_partial_click_release(self):
        backend = WindowsKeyboard.__new__(WindowsKeyboard)
        backend.user = Mock()
        batches = []
        def send(count, batch, size):
            batches.append([(item.type, item.keyboard.vk, item.keyboard.flags) if item.type == 1 else (item.type, item.mouse.x, item.mouse.y, item.mouse.flags) for item in batch])
            return count
        backend.user.SendInput.side_effect = send
        backend.key_event('Up', True); backend.key_event('Up', False)
        backend.mouse(3, -5)
        backend.mouse(button='right')
        self.assertEqual(batches, [[(1, 38, 1)], [(1, 38, 3)], [(0, 3, -5, 1)], [(0, 0, 0, 8), (0, 0, 0, 16)]])
        backend.user.SendInput.side_effect = lambda count, batch, size: count - 1
        with self.assertRaises(OSError): backend.mouse(button='left')
        self.assertEqual(backend.user.SendInput.call_args.args[1][0].mouse.flags, 4)

    def test_rejected_key_release_disables_mapping_and_retries_owned_keys(self):
        keyboard, backend = self.keyboard, self.keyboard.backend
        backend.physical_down.return_value = False
        keyboard.savePad(0x1000, 'W'); keyboard.setPadEnabled(True)
        keyboard.gamepad_input(0); keyboard.gamepad_input(0x1000)
        backend.key_event.side_effect = OSError('release rejected')
        backend.foreground.return_value = 43
        keyboard.check_target()
        self.assertFalse(keyboard.padEnabled)
        self.assertEqual(keyboard.pad_down, ['W'])
        self.assertTrue(keyboard.pad_watch.isActive())
        keyboard.setPadEnabled(True)
        self.assertFalse(keyboard.padEnabled)
        backend.key_event.side_effect = None
        keyboard.check_target()
        self.assertEqual(keyboard.pad_down, [])
        self.assertFalse(keyboard.pad_watch.isActive())

    def test_layout_switch_posts_to_focused_child_preserves_full_handle_and_checks_result(self):
        import ctypes
        backend = WindowsKeyboard.__new__(WindowsKeyboard)
        backend.user = Mock()
        backend.user.GetForegroundWindow.return_value = 42
        backend.user.GetWindowThreadProcessId.return_value = 7
        en, ru = 0x4090409, 0xf000000004190419
        backend.user.GetKeyboardLayout.return_value = en
        def layouts(count, values): values[0], values[1] = en, ru; return 2
        backend.user.GetKeyboardLayoutList.side_effect = layouts
        def focus(thread, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(_GuiThreadInfo)).contents
            value.active, value.focus = 42, 84; return True
        backend.user.GetGUIThreadInfo.side_effect = focus
        self.assertEqual(backend.next_layout(), (42, ru))
        backend.user.PostMessageW.assert_called_once_with(84, 0x50, 0, ru)
        keyboard = self.keyboard
        keyboard.backend.current_layout.return_value = (42, en)
        keyboard.backend.next_layout.return_value = (42, ru)
        keyboard.show(); self.assertEqual(keyboard.layout, 'EN')
        keyboard.switchLayout(); keyboard.refreshLayout()
        self.assertEqual(keyboard.layout, 'EN')
        keyboard.backend.current_layout.return_value = (42, ru)
        keyboard.refreshLayout()
        self.assertEqual(keyboard.layout, 'RU'); self.assertIsNone(keyboard.layout_pending)
        keyboard.backend.send.assert_not_called()
        keyboard.backend.next_layout.return_value = (42, en)
        keyboard.switchLayout()
        for _ in range(10): keyboard.refreshLayout()
        self.assertIn('не приняло', keyboard.status)
        self.assertEqual(keyboard.layout, 'RU')
        backend.user.GetKeyboardLayoutList.side_effect = lambda count, values: 1
        with self.assertRaises(OSError): backend.next_layout()

    def test_keyboard_opacity_is_separate_saved_and_haptics_can_be_disabled(self):
        keyboard = self.keyboard
        feedback = []
        keyboard.hapticRequested.connect(feedback.append)
        keyboard.press('Ctrl'); keyboard.press('A')
        self.assertEqual(feedback, [18, 18])
        keyboard.setHaptics(False); keyboard.press('B')
        self.assertEqual(feedback, [18, 18, 0])
        keyboard.setOpacity(.35); keyboard.setKeyboardOpacity(.7)
        self.assertEqual(keyboard.options['opacity'], .35)
        self.assertEqual(keyboard.options['keyboard_opacity'], .7)
        keyboard.setKeyboardOpacity(-.1)
        self.assertEqual(keyboard.options['keyboard_opacity'], .7)
        restored = VirtualKeyboard(self.bridge)
        self.assertEqual(restored.options['keyboard_opacity'], .7)
        self.assertFalse(restored.options['haptics'])

    def test_capture_persists_only_explicit_binding_and_rejects_conflicts(self):
        self.assertTrue(all(row['binding'] == 'Не назначено' for row in self.controls.rows))
        self.controls.beginCapture('show')
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F8, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(self.controls.eventFilter(self.bridge, event))
        self.assertEqual(self.controls.capturing, '')
        self.assertEqual(self.bridge.storage.read_settings()['shortcuts'], {'show': {'vk': 119, 'mods': 6}})
        self.assertFalse(self.controls.assign('keyboard', dict(vk=119, mods=6)))
        self.assertNotIn('keyboard', self.controls.values())
        self.assertEqual(binding(68, 8), {'vk': 68, 'mods': 8})
        self.assertEqual(binding(79, 10), {'vk': 79, 'mods': 10})
        for vk, mods in ((123, 0), (76, 8), (46, 3), (True, 0), (20, -1)):
            with self.assertRaises(ValueError): binding(vk, mods)
        self.controls.beginCapture('keyboard')
        self.controls.capture_timeout()
        self.assertEqual(self.controls.capturing, '')
        self.assertIn('OneXConsole', self.controls.status)
        self.controls.clear('show')
        self.assertEqual(self.bridge.storage.read_settings()['shortcuts'], {})

    def test_update_probe_does_not_create_tray_icon_or_global_registrations(self):
        window = QWindow()
        with patch('ngs.desktop.QGuiApplication.platformName', return_value='windows'), patch('ngs.desktop.WindowsDesktop') as native:
            self.controls.attach(window, native=False)
            native.assert_not_called()
            self.assertFalse(self.controls.trayAvailable)
            self.assertIsNone(self.controls.backend)
        window.destroy()

    def test_gamepad_capture_waits_for_opener_release_and_saves_chord_on_release(self):
        received = []
        self.controls.action.connect(received.append)
        self.controls.beginCapture('show')
        for buttons in (0x1000, 0x1000, 0, 0x40, 0xc0, 0x80):
            self.assertTrue(self.controls.gamepad_input(buttons))
            self.assertEqual(self.controls.capturing, 'show')
        self.controls.gamepad_input(0)
        self.assertEqual(self.controls.capturing, '')
        self.assertEqual(self.bridge.storage.read_settings()['shortcuts']['show'], {'buttons': 0xc0})
        self.assertIn('L3+R3', self.controls.rows[1]['binding'])
        self.assertEqual(received, [])
        self.assertTrue(self.controls.gamepad_input(0xc0))
        self.assertEqual(received, [])  # Must release before the new assignment can fire.
        self.controls.gamepad_input(0)
        self.controls.gamepad_input(0x40)
        self.controls.gamepad_input(0xc0)
        self.controls.gamepad_input(0xc0)
        self.controls.gamepad_input(0x80)
        self.assertEqual(received, ['show'])
        self.controls.gamepad_input(0)
        self.controls.gamepad_input(0xc0)
        self.assertEqual(received, ['show', 'show'])
        self.assertFalse(self.controls.assign('keyboard', {'buttons': 0xc0}))
        for value in (True, 0, -1, 0x400, 0x800, 0x40000):
            with self.assertRaises(ValueError): binding(buttons=value)
        with self.assertRaises(ValueError): binding(vk=65, mods=0, buttons=1)
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            self.assertFalse(self.controls.assign('show', {'buttons': 0x10000}))
        self.assertEqual(self.controls.values()['show'], {'buttons': 0xc0})

    def test_oem_keyboard_capture_queues_events_and_cleans_up_on_cancel_save_and_close(self):
        import ctypes
        hook = WindowsKeyCapture.__new__(WindowsKeyCapture)
        hook.user = Mock()
        hook.held = set()
        hook.shortcuts = None
        hook.suppressed = {}
        hook.ended = set()
        hook.recording = {}
        hook.release_notify = None
        hook.modifier_press = None
        hook.modifier_blocked = set()
        hook.notify = Mock()
        hook.keyboard_notify = Mock()
        def key(vk, down=True, flags=0):
            event = _KeyboardEvent(vk=vk, flags=flags)
            return hook.process(0, 0x100 if down else 0x101, ctypes.addressof(event))
        key(162); key(164)
        self.assertEqual(key(119, flags=0x10), 1)  # OEM software may synthesize keys.
        key(119); key(119, False); key(164, False); key(162, False)
        hook.notify.assert_called_once_with(119, 3)
        hook.keyboard_notify.assert_not_called()
        key(65); key(65, False)
        hook.keyboard_notify.assert_called_once_with(65, 0)
        key(91); key(68); key(68, False); key(91, False)
        hook.keyboard_notify.assert_called_once_with(65, 0)
        self.controls.native_filter = _NativeEvents(self.controls)
        with patch('ngs.desktop.WindowsKeyCapture') as capture:
            self.controls.beginCapture('show')
            self.controls.keyCaptured.emit(119, 3)
            self.assertEqual(self.controls.capturing, 'show')
            self.app.processEvents()
            self.assertEqual(self.controls.values()['show'], {'vk': 119, 'mods': 3})
            capture.return_value.close.assert_called_once()
            capture.return_value.reset_mock()
            self.controls.beginCapture('keyboard'); self.controls.capture_key(27, 0)
            capture.return_value.close.assert_called_once()
            capture.return_value.reset_mock()
            self.controls.beginCapture('keyboard'); self.controls.close()
            capture.return_value.close.assert_called_once()
        self.controls.native_filter = None

    def test_system_hook_only_intercepts_assigned_win_chords_and_balances_keyup(self):
        import ctypes
        hook = WindowsKeyCapture.__new__(WindowsKeyCapture)
        hook.user = Mock()
        hook.user.CallNextHookEx.return_value = 0
        hook.held = set()
        hook.shortcuts = {(68, 8), (79, 10)}
        hook.suppressed = {}
        hook.ended = set()
        hook.recording = {}
        hook.release_notify = None
        hook.modifier_press = None
        hook.modifier_blocked = set()
        hook.notify = Mock()
        hook.mask_win_menu = Mock()
        def key(vk, down=True):
            event = _KeyboardEvent(vk=vk)
            return hook.process(0, 0x100 if down else 0x101, ctypes.addressof(event))
        self.assertEqual(key(65), 0)
        self.assertEqual(key(65, False), 0)
        key(91)
        self.assertEqual(key(68), 1)
        self.assertEqual(key(68), 1)
        self.assertEqual(hook.notify.call_count, 1)
        self.assertEqual(hook.notify.call_args.args[:3], (68, 8, True))
        key(91, False)
        self.assertEqual(key(68, False), 1)
        self.assertEqual(hook.notify.call_args.args[:3], (68, 8, False))
        key(91); key(162); self.assertEqual(key(79), 1)
        self.assertEqual(hook.notify.call_args.args[:3], (79, 10, True))
        self.assertEqual(hook.mask_win_menu.call_count, 2)
        from ngs.virtual_keyboard import NGS_INPUT_EXTRA
        event = _KeyboardEvent(vk=79, extra=NGS_INPUT_EXTRA)
        before = hook.notify.call_count
        self.assertEqual(hook.process(0, 0x100, ctypes.addressof(event)), 0)
        self.assertEqual(hook.notify.call_count, before)

    def test_win_menu_mask_is_balanced_and_partial_send_is_cleaned_up(self):
        from ngs.desktop import MASK_EXTRA
        hook = WindowsKeyCapture.__new__(WindowsKeyCapture)
        hook.user, hook.mask_error = Mock(), False
        calls = []
        def send(count, batch, size):
            calls.append([(item.keyboard.vk, item.keyboard.flags, item.keyboard.extra) for item in batch])
            return count
        hook.user.SendInput.side_effect = send
        hook.mask_win_menu()
        self.assertEqual(calls, [[(0xe8, 0, MASK_EXTRA), (0xe8, 2, MASK_EXTRA)]])
        self.assertFalse(hook.mask_error)
        hook.user.SendInput.side_effect = [1, 1]
        hook.mask_win_menu()
        self.assertTrue(hook.mask_error)
        self.assertEqual(hook.user.SendInput.call_args.args[0], 1)

    def test_aokzoe_modifier_only_turbo_records_and_intercepts_first_released_modifier(self):
        import ctypes
        hook = WindowsKeyCapture.__new__(WindowsKeyCapture)
        hook.user = Mock()
        hook.user.CallNextHookEx.return_value = 0
        hook.held, hook.suppressed, hook.recording = set(), {}, {}
        hook.ended = set()
        hook.modifier_press, hook.modifier_blocked = None, set()
        hook.shortcuts = None
        hook.notify, hook.release_notify, hook.mask_win_menu = Mock(), Mock(), Mock()
        def key(vk, down=True):
            event = _KeyboardEvent(vk=vk)
            return hook.process(0, 0x100 if down else 0x101, ctypes.addressof(event))
        key(162); key(91)
        self.assertEqual(key(164), 1)
        hook.notify.assert_called_once_with(0, 11)
        self.assertEqual(key(162, False), 0)  # Ctrl was forwarded: its release must reach Windows.
        hook.release_notify.assert_called_once()
        self.assertEqual(hook.release_notify.call_args.args[:2], (0, 11))
        key(91, False)
        self.assertEqual(key(164, False), 1)  # Alt down was suppressed, so suppress Alt up too.
        self.assertEqual(hook.modifier_blocked, set())
        self.controls.beginCapture('show')
        self.controls.capture_key(0, 11)
        self.assertEqual(self.controls.values()['show'], {'vk': 0, 'mods': 11})
        self.assertEqual(self.controls.rows[1]['binding'], 'Win+Ctrl+Alt')
        hook.shortcuts = {(0, 11)}; hook.notify.reset_mock()
        key(162); key(91); key(164); key(91, False); key(162, False); key(164, False)
        self.assertEqual(hook.notify.call_count, 2)
        self.assertEqual(hook.notify.call_args_list[0].args[:3], (0, 11, True))
        self.assertEqual(hook.notify.call_args_list[1].args[:3], (0, 11, False))

    def test_short_and_hold_share_gamepad_button_without_double_action(self):
        received = []
        self.controls.action.connect(received.append)
        self.assertTrue(self.controls.assign('show', {'buttons': 0x40}))
        self.controls.beginCapture('keyboard', True)
        for value in (0, 0x40, 0): self.controls.gamepad_input(value)
        self.assertEqual(self.controls.values()['keyboard'], {'buttons': 0x40, 'hold': True})
        self.assertIn('0,7', self.controls.rows[2]['binding'])
        self.assertFalse(self.controls.assign('desktop', {'buttons': 0x40, 'hold': True}))
        with patch('ngs.desktop.time.monotonic', return_value=10):
            self.controls.gamepad_input(0); self.controls.gamepad_input(0x40)
        self.assertEqual(received, [])
        with patch('ngs.desktop.time.monotonic', return_value=10.8):
            self.controls.check_holds(); self.controls.check_holds()
            self.controls.gamepad_input(0)
        self.assertEqual(received, ['keyboard'])
        with patch('ngs.desktop.time.monotonic', return_value=20): self.controls.gamepad_input(0x40)
        with patch('ngs.desktop.time.monotonic', return_value=20.2): self.controls.gamepad_input(0)
        self.assertEqual(received, ['keyboard', 'show'])
        with patch('ngs.desktop.time.monotonic', return_value=30): self.controls.gamepad_input(0x40)
        self.controls.cancel_presses()
        with patch('ngs.desktop.time.monotonic', return_value=40): self.controls.check_holds()
        self.assertEqual(received, ['keyboard', 'show'])
        self.controls.gamepad_input(0)
        with patch('ngs.desktop.time.monotonic', return_value=50): self.controls.gamepad_input(0x40)
        self.controls.gamepad_connection(False)
        with patch('ngs.desktop.time.monotonic', return_value=51): self.controls.check_holds()
        self.assertEqual(received, ['keyboard', 'show'])
        self.assertFalse(self.controls.hold_timer.isActive())

    def test_win_keyboard_hold_uses_native_event_times_and_survives_save_failure(self):
        received = []
        self.controls.action.connect(received.append)
        short, long = dict(vk=79, mods=10), dict(vk=79, mods=10, hold=True)
        self.assertTrue(self.controls.assign('show', short))
        self.assertTrue(self.controls.assign('keyboard', long))
        self.controls.keyboard_state(79, 10, True, 10)
        self.controls.keyboard_state(79, 10, False, 10.1)
        self.assertEqual(received, ['show'])
        self.controls.keyboard_state(79, 10, True, 20)
        self.controls.keyboard_state(79, 10, False, 21)
        self.assertEqual(received, ['show', 'keyboard'])
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            self.assertFalse(self.controls.assign('keyboard', dict(vk=68, mods=8, hold=True)))
        self.assertEqual(self.controls.values()['keyboard'], long)
        self.bridge._closing = True
        self.controls.keyboard_state(79, 10, True, 30)
        self.controls.keyboard_state(79, 10, False, 31)
        self.assertEqual(received, ['show', 'keyboard'])

    def test_hold_recording_reports_firmware_pulse_instead_of_saving_unusable_hold(self):
        self.controls.beginCapture('show', True)
        self.controls.capture_key(68, 8)
        self.assertEqual(self.controls.capturing, 'show')
        self.controls.capture_release(68, 8, .02)
        self.assertNotIn('show', self.controls.values())
        self.assertIn('20 мс', self.controls.status)
        self.assertIn('OneXConsole', self.controls.status)
        self.controls.capture_key(68, 8)
        self.controls.capture_release(68, 8, .8)
        self.assertEqual(self.controls.capturing, '')
        self.assertEqual(self.controls.values()['show'], dict(vk=68, mods=8, hold=True))

    def test_toggle_is_optional_persistent_and_not_a_different_chord(self):
        received = []
        self.controls.action.connect(received.append)
        self.controls.setToggle('desktop', True)
        self.assertNotIn('desktop', self.controls.values())
        self.assertTrue(self.controls.assign('desktop', dict(vk=119, mods=6)))
        self.controls.fire_action('desktop')
        self.controls.setToggle('desktop', True)
        self.assertEqual(self.bridge.storage.read_settings()['shortcuts']['desktop'], dict(vk=119, mods=6, toggle=True))
        self.assertTrue(self.controls.rows[0]['toggle'])
        self.assertFalse(self.controls.assign('show', dict(vk=119, mods=6)))
        self.controls.backend = Mock(hwnd=321, taskbar_message=0xc123, tray=True)
        self.controls.keyboard_state(119,6,True,1.)
        self.controls.keyboard_state(119,6,False,1.1)
        self.controls.native_message(321, 0x312, 1, 0)
        self.controls.native_message(321, WM_TRAY, 1, 0x202)
        self.assertEqual(received, ['desktop', 'toggle', 'show'])
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            self.controls.setToggle('desktop', False)
        self.assertTrue(self.controls.values()['desktop']['toggle'])
        self.assertEqual(self.controls.backend.configure_shortcuts.call_args.args[0], self.controls.values())
        self.assertTrue(self.controls.assign('desktop', dict(buttons=0x40, hold=True)))
        self.assertEqual(self.controls.values()['desktop'], dict(buttons=0x40, hold=True, toggle=True))
        self.controls.setToggle('desktop', False)
        self.assertEqual(self.controls.values()['desktop'], dict(buttons=0x40, hold=True))
        for option in ('yes', 1, None):
            with self.assertRaises(ValueError): binding(buttons=0x40, toggle=option)

    def test_toggle_hold_fires_once_and_short_press_remains_separate(self):
        received = []
        self.controls.action.connect(received.append)
        self.assertTrue(self.controls.assign('show', dict(buttons=0x40)))
        self.assertTrue(self.controls.assign('desktop', dict(buttons=0x40, hold=True, toggle=True)))
        with patch('ngs.desktop.time.monotonic', return_value=10):
            self.controls.gamepad_input(0); self.controls.gamepad_input(0x40)
        with patch('ngs.desktop.time.monotonic', return_value=10.2): self.controls.gamepad_input(0)
        self.assertEqual(received, ['show'])
        with patch('ngs.desktop.time.monotonic', return_value=20): self.controls.gamepad_input(0x40)
        with patch('ngs.desktop.time.monotonic', return_value=21):
            self.controls.check_holds(); self.controls.check_holds()
            self.controls.gamepad_input(0x40); self.controls.gamepad_input(0)
        self.assertEqual(received, ['show', 'toggle'])
        self.assertTrue(self.controls.assign('desktop', dict(vk=68, mods=8, hold=True)))
        self.controls.keyboard_state(68, 8, True, 30)
        self.controls.keyboard_state(68, 8, True, 30.5)
        self.controls.keyboard_state(68, 8, False, 30.8)
        self.assertEqual(received, ['show', 'toggle', 'toggle'])

    def test_hold_capture_ignores_autorepeated_release(self):
        self.controls.beginCapture('desktop', True)
        with patch('ngs.desktop.time.monotonic', return_value=10):
            self.controls.eventFilter(self.bridge, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F8, Qt.KeyboardModifier.NoModifier))
        self.controls.eventFilter(self.bridge, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_F8, Qt.KeyboardModifier.NoModifier, '', True))
        self.assertIsNotNone(self.controls.capture_candidate)
        with patch('ngs.desktop.time.monotonic', return_value=10.8):
            self.controls.eventFilter(self.bridge, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_F8, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.controls.values()['desktop'], dict(vk=119, mods=0, hold=True))

    def test_failed_registration_or_save_preserves_previous_binding(self):
        original = dict(vk=120, mods=3)
        self.assertTrue(self.controls.assign('show', original))
        native = self.controls.backend = Mock()
        native.configure_shortcuts.side_effect = [ValueError('conflict'), None]
        self.assertFalse(self.controls.assign('show', dict(vk=121, mods=3)))
        self.assertEqual(self.controls.values()['show'], original)
        self.assertEqual(native.configure_shortcuts.call_args.args[0], {'show': original})
        native.configure_shortcuts.side_effect = None
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            self.assertFalse(self.controls.assign('show', dict(vk=122, mods=3)))
        self.assertEqual(self.controls.values()['show'], original)
        self.assertEqual(native.configure_shortcuts.call_args.args[0], {'show': original})
        self.assertEqual(self.bridge.storage.read_settings()['shortcuts']['show'], original)

    def test_short_and_hold_reserve_one_native_chord_and_restore_it_on_conflict(self):
        backend = WindowsDesktop.__new__(WindowsDesktop)
        backend.user, backend.hwnd = Mock(), 99
        backend.user.RegisterHotKey.return_value = True
        backend.key_hook, backend.shortcut_values = None, {}
        values = {'show': dict(vk=119, mods=6), 'keyboard': dict(vk=119, mods=6, hold=True)}
        with patch('ngs.desktop.WindowsKeyCapture'):
            backend.configure_shortcuts(values, Mock())
            backend.user.RegisterHotKey.assert_called_once_with(99, 2, 0x4006, 119)
            self.assertEqual(backend.key_hook.shortcuts, {(119, 6)})
            backend.user.RegisterHotKey.side_effect = [False, True]
            with self.assertRaises(ValueError):
                backend.configure_shortcuts(dict(values, desktop=dict(vk=120, mods=6)), Mock())
            self.assertEqual(backend.shortcut_values, values)
            self.assertEqual(backend.user.RegisterHotKey.call_args.args, (99, 2, 0x4006, 119))

    def test_plain_service_chord_is_hooked_while_native_reservation_still_detects_conflicts(self):
        backend=WindowsDesktop.__new__(WindowsDesktop)
        backend.user,backend.hwnd,backend.key_hook,backend.shortcut_values=Mock(),99,None,{}
        backend.user.RegisterHotKey.return_value=True
        values={'show':dict(vk=119,mods=6)}
        with patch('ngs.desktop.WindowsKeyCapture') as hook:
            backend.configure_shortcuts(values,Mock())
            self.assertEqual(hook.call_args.kwargs['shortcuts'],{(119,6)})
            backend.user.RegisterHotKey.assert_called_once_with(99,2,0x4006,119)

    def test_failed_system_hook_install_restores_previous_native_shortcut_and_settings(self):
        original = dict(vk=119, mods=6)
        self.assertTrue(self.controls.assign('show', original))
        native = self.controls.backend = Mock()
        native.register.return_value = True
        native.configure_shortcuts.side_effect = [OSError('hook refused'), None]
        self.assertFalse(self.controls.assign('show', dict(vk=68, mods=8)))
        self.assertEqual(self.controls.values()['show'], original)
        self.assertEqual(self.bridge.storage.read_settings()['shortcuts']['show'], original)
        self.assertEqual(native.configure_shortcuts.call_args.args[0], {'show': original})

    def test_unavailable_startup_binding_does_not_block_other_assignments_or_get_lost(self):
        self.assertTrue(self.controls.assign('show', dict(vk=119, mods=6)))
        self.controls.errors['show'] = 'taken at startup'
        native = self.controls.backend = Mock()
        self.assertTrue(self.controls.assign('keyboard', dict(vk=68, mods=8)))
        self.assertEqual(native.configure_shortcuts.call_args.args[0], {'keyboard': dict(vk=68, mods=8)})
        self.assertEqual(self.controls.values()['show'], dict(vk=119, mods=6))
        self.assertIn('show', self.controls.errors)

    def test_apply_reloads_restored_shortcuts_into_native_backend(self):
        native = self.controls.backend = Mock()
        self.bridge.settings['shortcuts'] = {'desktop': dict(vk=119, mods=6, toggle=True)}
        self.controls.apply()
        self.assertEqual(native.configure_shortcuts.call_args.args[0], {'desktop': dict(vk=119, mods=6, toggle=True)})
        self.assertEqual(self.controls.errors, {})

    def test_native_messages_route_while_hidden_and_capture_does_not_fire_actions(self):
        native = self.controls.backend = Mock(hwnd=321, taskbar_message=0xc123, tray=True)
        received = []
        self.controls.action.connect(received.append)
        self.assertTrue(self.controls.native_message(321, 0x312, 2, 0))
        self.assertTrue(self.controls.native_message(321, WM_TRAY, 1, 0x202))
        self.assertEqual(received, ['show', 'show'])
        self.assertFalse(self.controls.native_message(999, 0x312, 2, 0))
        self.controls.assign('show', dict(vk=119, mods=6))
        self.controls.beginCapture('show')
        self.controls.native_message(321, 0x312, 2, 0)
        self.assertEqual(received, ['show', 'show'])
        self.controls.native_message(321, 0xc123, 0, 0)
        native.restore_tray.assert_called_once()

    def test_modifier_chords_numpad_macros_cancel_and_target_change(self):
        notifications = []
        self.keyboard.macrosChanged.connect(lambda: notifications.append(True))
        self.keyboard.show()
        self.keyboard.press('Ctrl'); self.keyboard.press('Shift'); self.keyboard.press('A')
        self.keyboard.backend.send.assert_called_with(['Ctrl', 'Shift', 'A'])
        self.keyboard.shortcut('ctrl+c')
        self.keyboard.backend.send.assert_called_with(['Ctrl', 'C'])
        self.assertEqual(self.keyboard.held, ['Ctrl'])
        self.keyboard.clearModifiers(); self.keyboard.press('Num7')
        self.keyboard.backend.send.assert_called_with(['Num7'])
        self.assertEqual(notifications, [])
        self.assertTrue(self.keyboard.saveMacro('Copy paste', 'Ctrl+C; wait:200; Ctrl+V'))
        self.assertEqual(notifications, [True])
        self.keyboard.runMacro('Copy paste')
        self.keyboard.backend.send.assert_called_with(['Ctrl', 'C'])
        self.keyboard.timer.stop(); self.keyboard.next_step()
        self.assertTrue(self.keyboard.running)
        self.keyboard.backend.foreground.return_value = 43
        self.keyboard.timer.stop(); self.keyboard.next_step()
        self.assertFalse(self.keyboard.running)
        self.assertIn('изменилось', self.keyboard.status)
        self.keyboard.backend.foreground.return_value = 42
        self.keyboard.runMacro('Copy paste'); self.keyboard.hide()
        self.assertFalse(self.keyboard.running)
        self.assertEqual(self.keyboard.held, [])
        restored = VirtualKeyboard(self.bridge)
        self.assertEqual(restored.macros[0]['name'], 'Copy paste')
        restored.cancel()

    def test_macro_validation_and_failed_save_preserve_data(self):
        self.assertEqual(chord('ctrl+shift+a'), ['Ctrl', 'Shift', 'A'])
        self.assertEqual(macro_steps('Ctrl+C; wait:20; Enter')[1], 20)
        for sequence in ('', 'Ctrl+NoSuchKey', 'Ctrl+Ctrl+A', 'wait:2001', ';', 'A;' * 101, 'wait:2000;' * 16 + 'A'):
            with self.assertRaises(ValueError): macro_steps(sequence)
        self.assertTrue(self.keyboard.saveMacro('test', 'Ctrl+A'))
        previous = self.keyboard.macros
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            self.assertFalse(self.keyboard.saveMacro('test', 'Ctrl+C'))
            self.keyboard.removeMacro('test')
        self.assertEqual(self.keyboard.macros, previous)

    def test_sendinput_has_correct_layout_balances_keys_and_preserves_physical_shift(self):
        import ctypes
        self.assertEqual(ctypes.sizeof(_Input), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        backend = WindowsKeyboard.__new__(WindowsKeyboard)
        backend.user = Mock()
        backend.user.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk == 16 else 0
        calls = []
        def send(count, batch, size):
            from ngs.virtual_keyboard import NGS_INPUT_EXTRA
            self.assertTrue(all(value.keyboard.extra == NGS_INPUT_EXTRA for value in batch))
            calls.append([(value.keyboard.vk, value.keyboard.flags) for value in batch])
            return count
        backend.user.SendInput.side_effect = send
        backend.send(['Ctrl', 'Shift', 'NumEnter'])
        self.assertEqual(calls[0], [(17, 0), (13, 1), (13, 3), (17, 2)])
        backend.user.SendInput.side_effect = None
        backend.user.SendInput.return_value = 0
        with self.assertRaises(OSError): backend.send(['Ctrl', 'A'])
        self.assertEqual(backend.user.SendInput.call_args.args[1][0].keyboard.flags, 2)


if __name__ == '__main__':
    unittest.main()
