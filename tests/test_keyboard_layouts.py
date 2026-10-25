import copy
import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from ngs.auto_keyboard import AutoKeyboard, focused_edit
from ngs.keyboard_layouts import validate_layout
from ngs.virtual_keyboard import KEYS, MODIFIERS, VirtualKeyboard, WindowsKeyboard
from tests.test_desktop_controls import Bridge


class Power(QObject):
    changed = Signal()
    resumed = Signal()
    sleeping = False


class KeyboardLayoutTests(unittest.TestCase):
    def test_keyboard_toggle_hides_and_blocks_all_open_paths_but_keeps_other_settings(self):
        self.keyboard.setHeight(43)
        self.keyboard.setAutoShow(True)
        self.keyboard.show(); self.assertTrue(self.keyboard.visible)
        self.keyboard.setEnabled(False)
        self.assertFalse(self.keyboard.visible)
        self.assertFalse(self.keyboard.layout_timer.isActive())
        for show in (self.keyboard.show,self.keyboard.toggle,self.keyboard.pickKeys):
            show(); self.assertFalse(self.keyboard.visible)
        self.assertFalse(self.keyboard.picking)
        self.assertTrue(self.keyboard.options['overlay_enabled'])
        self.assertTrue(self.keyboard.options['auto_show'])
        self.assertEqual(self.keyboard.options['height_percent'],43)
        self.assertIs(self.bridge.storage.read_settings()['keyboard_options']['enabled'],False)
        reloaded=VirtualKeyboard(Bridge(Path(self.folder.name)))
        try:
            self.assertFalse(reloaded.enabled)
            self.assertTrue(reloaded.options['overlay_enabled'])
            reloaded.show();self.assertFalse(reloaded.visible)
        finally:reloaded.close()
        self.keyboard.setEnabled(True); self.keyboard.show()
        self.assertTrue(self.keyboard.visible)

    def test_disabling_keyboard_stops_auto_show_and_ignores_queued_focus_result(self):
        power=Power(); auto=AutoKeyboard(self.keyboard,power,native=False)
        try:
            serial=auto.serial
            self.keyboard.setEnabled(False)
            self.assertFalse(auto.enabled)
            auto.accept(serial,True,('field',1))
            auto.accept(auto.serial,True,('field',2))
            self.assertFalse(self.keyboard.visible)
            self.keyboard.setEnabled(True)
            self.assertTrue(auto.enabled)
            auto.accept(auto.serial,True,('field',3))
            self.assertFalse(self.keyboard.visible)
            auto.request((100,100)); auto.accept(auto.serial,True,('field',3))
            self.assertTrue(self.keyboard.visible)
        finally:auto.close()

    def test_failed_input_toggle_does_not_change_availability_or_hide_keyboard(self):
        self.keyboard.show()
        with patch.object(self.bridge,'save',side_effect=OSError('Disk full')):
            self.keyboard.setEnabled(False)
            self.keyboard.setOverlayEnabled(False)
        self.assertTrue(self.keyboard.enabled and self.keyboard.options['overlay_enabled'])
        self.assertTrue(self.keyboard.visible)
        self.assertIn('Disk full',self.keyboard.status)

    def test_overlay_toggle_closes_remote_panel_and_blocks_stale_swipe_without_disabling_keyboard(self):
        from ngs.status_overlay import StatusOverlay
        overlay=StatusOverlay(self.keyboard,self.bridge);overlay.edge_enabled=True
        remote=Mock();overlay.remote=remote
        requests=Mock();closed=Mock();overlay.requested.connect(requests);overlay.closed.connect(closed)
        try:
            overlay.show('screen-1');self.assertTrue(overlay.visible)
            self.keyboard.setOverlayEnabled(False)
            self.assertFalse(overlay.visible or overlay.shown or overlay.edgeEnabled)
            remote.status.assert_called_with(False,'screen-1')
            closed.assert_called_once()
            overlay.request('screen-1');overlay.show('screen-1')
            requests.assert_not_called();self.assertFalse(overlay.shown)
            self.assertTrue(self.keyboard.enabled)
            self.assertIs(self.bridge.storage.read_settings()['keyboard_options']['overlay_enabled'],False)
            overlay.remote=None;self.keyboard.setOverlayEnabled(True)
            self.assertTrue(overlay.edgeEnabled)
            overlay.request('screen-1');requests.assert_called_once_with('screen-1')
        finally:overlay.close()

    def test_resolution_change_updates_keyboard_area_without_changing_the_saved_profile(self):
        from PySide6.QtCore import QRect
        class Screen(QObject):
            geometryChanged=Signal(QRect)
            availableGeometryChanged=Signal(QRect)
            logicalDotsPerInchChanged=Signal(float)
            rectangle=QRect(0,0,1280,800)
            def geometry(self): return self.rectangle
            def devicePixelRatio(self): return 1.
        screen=Screen(); self.keyboard.set_screen(screen)
        previous=copy.deepcopy(self.bridge.settings)
        changes=[]; self.keyboard.changed.connect(lambda: changes.append(True))
        screen.rectangle=QRect(0,0,640,480)
        screen.geometryChanged.emit(screen.rectangle)
        self.assertEqual(self.keyboard.screenArea,dict(x=0,y=0,width=640,height=480))
        self.assertEqual(changes,[True])
        self.assertEqual(self.bridge.settings,previous)

    @classmethod
    def setUpClass(cls): cls.app = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.bridge = Bridge(Path(self.folder.name))
        self.keyboard = VirtualKeyboard(self.bridge)
        self.keyboard.backend = Mock()
        self.layout = dict(id='', name='Моя', rows=[[
            dict(key='A', width=1, states=dict(base='ф', shift='Ф', altgr='€', shift_altgr='😀')),
            dict(key='Shift', width=1.5, states={}), dict(key='AltGr', width=1, states={}),
            dict(key='Ctrl', width=1, states={}), dict(key='@settings', width=1, states={}), dict(key='@hide', width=1, states={})]])

    def tearDown(self):
        self.keyboard.close(); self.folder.cleanup()

    def test_percent_and_view_persist_and_reject_invalid_geometry(self):
        self.keyboard.setHeight(41)
        self.assertEqual(self.keyboard.options['height_percent'], 41)
        self.assertEqual(self.bridge.storage.read_settings()['keyboard_options']['views']['touch']['height_percent'], 41)
        for value in (-1, 0, 19, 76, float('inf'), float('nan')): self.keyboard.setHeight(value)
        self.assertEqual(self.keyboard.options['height_percent'], 41)
        self.bridge.settings['keyboard_options']['views']['touch']['height_percent'] = {'bad': True}
        self.assertEqual(self.keyboard.options['height_percent'], 40)

    def test_views_keep_independent_geometry_and_legacy_values_without_cross_writes(self):
        self.bridge.settings['keyboard_options'] = dict(height_percent=44, keyboard_opacity=.8, opacity=.4)
        self.keyboard.setView('navigation'); nav_height = min(35,self.keyboard.options['height_min']+2)
        nav_default = self.keyboard.options['height_percent']
        profiles = [('touch',35,.7),('full',55,.9),('numpad',25,.3),('small',30,.8),('split',45,.6),('traditional',50,.7),('gamepad',40,.9),('navigation',nav_height,.8)]
        for view, height, opacity in profiles:
            self.keyboard.setView(view)
            self.assertEqual(self.keyboard.options['height_percent'], nav_default if view == 'navigation' else 44)
            self.assertEqual(self.keyboard.options['keyboard_opacity'], .8)
            self.keyboard.setHeight(height); self.keyboard.setKeyboardOpacity(opacity)
        self.keyboard.setTouchpadHeight(60); self.keyboard.setOpacity(.25); self.keyboard.setPointerSensitivity(1.75)
        self.assertTrue(self.keyboard.saveLayout(self.layout))
        custom = self.keyboard.options['view']; self.keyboard.setHeight(65); self.keyboard.setKeyboardOpacity(.6)
        restored = VirtualKeyboard(self.bridge)
        try:
            for view, height, opacity in [*profiles,(custom,65,.6)]:
                restored.setView(view)
                self.assertEqual(restored.options['height_percent'],height)
                self.assertEqual(restored.options['keyboard_opacity'],opacity)
                self.assertEqual(restored.options['opacity'],.25)
                self.assertEqual(restored.options['touchpad_height_percent'],60)
                self.assertEqual(restored.options['pointer_sensitivity'],1.75)
            previous = copy.deepcopy(self.bridge.settings)
            with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
                restored.setHeight(20); restored.setKeyboardOpacity(0); restored.setPointerSensitivity(3)
            self.assertEqual(self.bridge.settings,previous)
            for invalid in (float('nan'), float('inf'), -.1, 4): restored.setPointerSensitivity(invalid)
            for invalid in (float('nan'), float('inf'), 0, 90): restored.setTouchpadHeight(invalid)
            self.assertEqual(self.bridge.settings,previous)
        finally: restored.close()

    def test_themes_are_independent_preserved_and_do_not_overwrite_on_save_failure(self):
        self.keyboard.setTheme('black', True)
        for theme in self.keyboard.themes:
            self.keyboard.setTheme(theme['id'], False)
            self.assertEqual(self.keyboard.palette['id'], theme['id'])
            self.assertEqual(self.keyboard.padPalette['id'], 'black')
        restored = VirtualKeyboard(self.bridge)
        try:
            self.assertEqual(restored.padPalette['panel'], '#000000')
            self.assertEqual(restored.palette['id'], 'terracotta')
            previous = copy.deepcopy(self.bridge.settings)
            self.keyboard.setTheme('unknown', False)
            with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
                self.keyboard.setTheme('light', True)
            self.assertEqual(self.bridge.settings, previous)
            for invalid in (None, [], {'theme':'bad'}):
                self.bridge.settings['keyboard_options'] = invalid
                self.assertEqual(self.keyboard.palette['id'], 'dark')
                self.assertEqual(self.keyboard.padPalette['id'], 'dark')
        finally: restored.close()

    def test_keyboard_theme_text_is_readable_in_all_key_states(self):
        def luminance(value):
            channels = [int(value[i:i+2],16)/255 for i in (1,3,5)]
            return sum((v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4)*w for v,w in zip(channels,(.2126,.7152,.0722)))
        for theme in self.keyboard.themes:
            for text, surface in [('text',state) for state in ('key','hover','pressed','held')] + [('panel_text','panel')]:
                a,b = sorted((luminance(theme[text]),luminance(theme[surface])))
                self.assertGreaterEqual((b+.05)/(a+.05),4.5,(theme['id'],surface))

    def test_touchpad_sensitivity_scales_motion_and_keeps_fractional_input(self):
        from PySide6.QtCore import QRect
        screen = Mock(); screen.geometry.return_value = QRect(0,0,1280,800); screen.devicePixelRatio.return_value = 2
        self.keyboard.set_screen(screen); self.keyboard.show(); self.keyboard.setTouchpads(True)
        self.keyboard.setPointerSensitivity(.25)
        self.keyboard.movePointer(.5,.5); self.keyboard.movePointer(.5,.5); self.keyboard.movePointer(.5,.5)
        self.keyboard.backend.mouse.assert_not_called()
        self.keyboard.movePointer(.5,.5); self.keyboard.backend.mouse.assert_called_once_with(1,1,'')
        self.keyboard.setPointerSensitivity(2)
        self.keyboard.movePointer(10,-5); self.keyboard.backend.mouse.assert_called_with(40,-20,'')

    def test_print_screen_uses_balanced_extended_input_and_is_available_in_custom_layouts(self):
        backend = WindowsKeyboard.__new__(WindowsKeyboard); backend.user = Mock()
        backend.user.GetAsyncKeyState.return_value = 0
        packets = []
        def send(count,batch,size):
            packets.append([(item.keyboard.vk,item.keyboard.flags) for item in batch]); return count
        backend.user.SendInput.side_effect = send
        backend.send(['PrintScreen']); backend.key_event('PrintScreen',True); backend.key_event('PrintScreen',False)
        self.assertEqual(packets, [[(44,1),(44,3)],[(44,1)],[(44,3)]])
        self.layout['rows'][0].extend([dict(key='@touchpads',width=1,states={}),dict(key='PrintScreen',width=1,states={})])
        self.assertTrue(self.keyboard.saveLayout(self.layout))
        self.assertIn('@touchpads',self.keyboard.layoutKeyNames)
        self.keyboard.pressCustom(0,7)
        self.assertTrue(self.keyboard.screenshot_timer.isActive())
        self.keyboard.screenshot_timer.stop(); self.keyboard.finish_screenshot()
        self.keyboard.backend.send.assert_called_with(['PrintScreen'])
        self.keyboard.modifiers = ['AltGr']; self.keyboard.pressCustom(0,7)
        self.assertTrue(self.keyboard.screenshot_timer.isActive())
        self.keyboard.screenshot_timer.stop(); self.keyboard.finish_screenshot()
        self.keyboard.backend.send.assert_called_with(['Ctrl','Alt','PrintScreen'])

    def test_screenshot_hides_before_shortcut_and_cancels_on_show_hide_or_close(self):
        from PySide6.QtTest import QTest
        self.keyboard.show(); self.keyboard.press('PrintScreen')
        self.assertFalse(self.keyboard.visible); self.keyboard.backend.send.assert_not_called()
        QTest.qWait(360); self.keyboard.backend.send.assert_called_once_with(['PrintScreen'])
        self.keyboard.backend.send.reset_mock()
        for cancel in (self.keyboard.show,self.keyboard.hide,self.keyboard.close):
            self.keyboard.show(); self.keyboard.takeScreenshot(False); cancel()
            QTest.qWait(360)
        self.keyboard.backend.send.assert_not_called()
        picked = []; self.keyboard.chordPicked.connect(picked.append)
        self.keyboard.pickKeys(); self.keyboard.takeScreenshot(True)
        self.assertEqual(picked,['PrintScreen']); self.assertFalse(self.keyboard.screenshot_timer.isActive())

    def test_voice_hides_panels_before_win_h_and_cancels_when_reopened(self):
        self.keyboard.show(); self.keyboard.setTouchpads(True); self.keyboard.voiceInput()
        self.assertFalse(self.keyboard.visible); self.assertTrue(self.keyboard.screenshotPending)
        self.keyboard.backend.send.assert_not_called()
        self.keyboard.screenshot_timer.stop(); self.keyboard.finish_screenshot()
        self.keyboard.backend.send.assert_called_once_with(['Win','H'])
        self.keyboard.backend.send.reset_mock()
        self.keyboard.show(); self.keyboard.voiceInput(); self.keyboard.show()
        self.keyboard.finish_screenshot(); self.keyboard.backend.send.assert_not_called()
        self.assertIn('@voice',self.keyboard.layoutKeyNames)

    def test_right_modifiers_use_extended_packets_and_protected_chords_stay_blocked(self):
        backend = WindowsKeyboard.__new__(WindowsKeyboard); backend.user = Mock()
        backend.user.GetAsyncKeyState.return_value = 0
        packets = []
        def send(count,batch,size):
            packets.append([(item.keyboard.vk,item.keyboard.flags) for item in batch]); return count
        backend.user.SendInput.side_effect = send
        backend.send(['RightCtrl','RightAlt','A'])
        self.assertEqual(packets[0],[(163,1),(165,1),(65,0),(65,2),(165,3),(163,3)])
        backend.key_event('RightCtrl',False); self.assertEqual(packets[-1],[(163,3)])
        self.assertFalse(self.keyboard.savePad(0x1000,'RightCtrl+RightAlt+Delete'))
        self.assertTrue(self.keyboard.savePad(0x1000,'RightCtrl+C'))
        self.assertTrue(self.keyboard.saveLayout(self.layout))
        self.keyboard.press('RightShift'); self.keyboard.pressCustom(0,0)
        self.keyboard.backend.text.assert_called_with('Ф')
        self.keyboard.press('RightShift'); self.keyboard.press('RightCtrl'); self.keyboard.pressCustom(0,0)
        self.keyboard.backend.send.assert_called_with(['RightShift','RightCtrl','A'])
        self.keyboard.press('Ctrl'); self.assertNotIn('Ctrl',self.keyboard.held)
        self.keyboard.press('RightShift'); self.keyboard.press('Shift')
        self.assertTrue(self.keyboard.shiftLocked)
        self.keyboard.press('Shift')
        self.assertNotIn('Shift',self.keyboard.held); self.assertNotIn('RightShift',self.keyboard.held)

    def test_shift_is_consumed_by_any_input_key_and_preserves_other_modifiers(self):
        for shift in ('Shift', 'RightShift'):
            for key in ('A', '1', 'Space', 'Backspace', 'Enter', 'Left', 'F1'):
                self.keyboard.clearModifiers()
                self.keyboard.press('Ctrl'); self.keyboard.press(shift); self.keyboard.press(key)
                self.keyboard.backend.send.assert_called_with(['Ctrl', shift, key])
                self.assertEqual(self.keyboard.held, ['Ctrl'])
                self.keyboard.press('B')
                self.keyboard.backend.send.assert_called_with(['Ctrl', 'B'])

    def test_double_shift_locks_until_next_shift_and_slow_second_tap_turns_it_off(self):
        for shift in ('Shift', 'RightShift'):
            self.keyboard.clearModifiers()
            with patch('ngs.virtual_keyboard.time.monotonic', side_effect=[10., 10.3, 11.]):
                self.keyboard.press(shift); self.keyboard.press(shift)
                self.assertTrue(self.keyboard.shiftLocked)
                for key in ('A', 'Space', 'Backspace'):
                    self.keyboard.press(key)
                    self.keyboard.backend.send.assert_called_with([shift, key])
                    self.assertIn('Shift', self.keyboard.held)
                self.keyboard.press(shift)
                self.assertFalse(self.keyboard.shiftLocked)
                self.assertEqual(self.keyboard.held, [])
            with patch('ngs.virtual_keyboard.time.monotonic', side_effect=[20., 20.8]):
                self.keyboard.press(shift); self.keyboard.press(shift)
                self.assertFalse(self.keyboard.shiftLocked)
                self.assertEqual(self.keyboard.held, [])

    def test_custom_shift_layers_consume_once_lock_and_clear_on_hide(self):
        self.assertTrue(self.keyboard.saveLayout(self.layout))
        self.keyboard.pressCustom(0,1); self.keyboard.pressCustom(0,0)
        self.keyboard.backend.text.assert_called_with('Ф')
        self.assertNotIn('Shift', self.keyboard.held)
        self.keyboard.pressCustom(0,0); self.keyboard.backend.text.assert_called_with('ф')
        self.keyboard.pressCustom(0,2)
        self.keyboard.pressCustom(0,1); self.keyboard.pressCustom(0,1)
        self.assertTrue(self.keyboard.shiftLocked)
        for _ in range(2):
            self.keyboard.pressCustom(0,0); self.keyboard.backend.text.assert_called_with('😀')
            self.assertIn('Shift', self.keyboard.held)
        self.keyboard.hide(); self.assertFalse(self.keyboard.shiftLocked)
        self.assertEqual(self.keyboard.held, [])
        self.keyboard.press('Shift'); self.keyboard.press('Shift'); self.keyboard.clearModifiers()
        self.assertFalse(self.keyboard.shiftLocked)
        self.keyboard.press('Shift'); self.keyboard.press('A')
        self.assertNotIn('Shift', self.keyboard.held)

    def test_failed_input_keeps_one_shot_shift_for_retry(self):
        self.keyboard.press('Shift')
        self.keyboard.backend.send.side_effect = OSError('input failed')
        self.keyboard.press('A'); self.assertIn('Shift', self.keyboard.held)
        self.keyboard.backend.send.side_effect = None
        self.keyboard.press('A'); self.assertNotIn('Shift', self.keyboard.held)

    def test_full_screen_capture_uses_clipboard_without_keys_or_files_and_reports_failure(self):
        from PySide6.QtGui import QPixmap, QColor
        picture = QPixmap(40,20); picture.fill(QColor('cyan'))
        screen = Mock(); screen.grabWindow.return_value = picture; self.keyboard.screen = screen
        with patch('ngs.virtual_keyboard.QGuiApplication.clipboard') as clipboard:
            self.keyboard.show(); self.keyboard.takeScreenshot(True)
            self.assertFalse(self.keyboard.visible); screen.grabWindow.assert_not_called()
            self.keyboard.screenshot_timer.stop(); self.keyboard.finish_screenshot()
            screen.grabWindow.assert_called_once_with(0); clipboard.return_value.setPixmap.assert_called_once_with(picture)
            self.keyboard.backend.send.assert_not_called()
            screen.grabWindow.return_value = QPixmap()
            self.keyboard.takeScreenshot(True); self.keyboard.screenshot_timer.stop(); self.keyboard.finish_screenshot()
            self.assertTrue(self.keyboard.visible); self.assertIn('снимок',self.keyboard.status)
            self.assertEqual(clipboard.return_value.setPixmap.call_count,1)

    def test_touch_suppressed_cursor_is_restored_without_moving_or_overriding_game_hiding(self):
        from ngs.virtual_keyboard import _CursorInfo
        backend = WindowsKeyboard.__new__(WindowsKeyboard); backend.user = Mock()
        state = {'flags':1}; packets = []
        def cursor(pointer):
            info = ctypes.cast(pointer,ctypes.POINTER(_CursorInfo)).contents
            self.assertEqual(info.size,ctypes.sizeof(_CursorInfo)); info.flags = state['flags']; return True
        def send(count,batch,size):
            packets.extend((item.mouse.x,item.mouse.y,item.mouse.flags) for item in batch); return count
        backend.user.GetCursorInfo.side_effect = cursor; backend.user.SendInput.side_effect = send
        for flags in (1,0,2):
            state['flags'] = flags; backend.restore_pointer()
        self.assertEqual(packets,[(0,0,1)])
        self.keyboard.restore_pointer(); self.keyboard.backend.restore_pointer.assert_not_called()
        self.keyboard.show(); self.keyboard.setTouchpads(True); self.keyboard.restore_pointer()
        self.keyboard.backend.restore_pointer.assert_called_once()
        self.keyboard.hide(); self.keyboard.restore_pointer()
        self.keyboard.backend.restore_pointer.assert_called_once()

    def test_cursor_restore_is_coalesced_after_touch_and_canceled_when_panels_hide(self):
        from PySide6.QtTest import QTest
        self.keyboard.show(); self.keyboard.setTouchpads(True)
        self.keyboard.pointer_timer.start(0); self.keyboard.pointer_timer.start(0)
        QTest.qWait(10); self.keyboard.backend.restore_pointer.assert_called_once()
        self.assertFalse(self.keyboard.pointer_timer.isActive())
        self.keyboard.pointer_timer.start(0); self.keyboard.hide(); QTest.qWait(10)
        self.keyboard.backend.restore_pointer.assert_called_once()

    def test_save_and_edit_replace_atomically_and_keep_other_settings(self):
        self.keyboard.setHeight(43)
        self.assertTrue(self.keyboard.saveLayout(self.layout))
        saved = self.keyboard.customLayout
        self.assertEqual(len(saved['id']), 32)
        self.assertEqual(self.keyboard.options['height_percent'], 43)
        self.assertEqual(self.bridge.storage.read_settings()['keyboard_layouts'], [saved])
        saved['name'] = 'Переименована'; self.assertTrue(self.keyboard.saveLayout(saved))
        self.assertEqual(len(self.keyboard.layouts), 1)
        previous = copy.deepcopy(self.bridge.settings)
        with patch.object(self.bridge, 'save', side_effect=OSError('read only')):
            saved['name'] = 'Не сохранена'
            self.assertFalse(self.keyboard.saveLayout(saved))
        self.assertEqual(self.bridge.settings, previous)

    def test_layer_input_and_ctrl_chord_are_distinct_and_balanced(self):
        self.assertTrue(self.keyboard.saveLayout(self.layout))
        for modifiers, expected in (([], 'ф'), (['Shift'], 'Ф'), (['AltGr'], '€'), (['Shift','AltGr'], '😀')):
            self.keyboard.modifiers = modifiers
            self.keyboard.pressCustom(0, 0)
            self.keyboard.backend.text.assert_called_with(expected)
        self.keyboard.modifiers = ['Ctrl']; self.keyboard.pressCustom(0,0)
        self.keyboard.backend.send.assert_called_with(['Ctrl','A'])
        self.keyboard.modifiers = []; self.keyboard.pressCustom(0,2)
        self.assertIn('AltGr', self.keyboard.held)
        self.keyboard.pressCustom(0,2); self.assertNotIn('AltGr',self.keyboard.held)
        before = self.keyboard.backend.text.call_count
        self.keyboard.pressCustom(-1,0); self.keyboard.pressCustom(12,0)
        self.assertEqual(self.keyboard.backend.text.call_count,before)

    def test_layout_validation_rejects_poisoned_geometry_and_service_removal(self):
        for width in (True, .1, 9, float('nan'), float('inf')):
            value = copy.deepcopy(self.layout); value['rows'][0][0]['width'] = width
            with self.assertRaises(ValueError): validate_layout(value, (*KEYS,*MODIFIERS))
        for text in ('\x00', '\n', '\ud800', 'x'*17):
            value = copy.deepcopy(self.layout); value['rows'][0][0]['states']['base'] = text
            with self.assertRaises(ValueError): validate_layout(value, (*KEYS,*MODIFIERS))
        value = copy.deepcopy(self.layout); value['rows'][0].pop()
        self.assertFalse(self.keyboard.saveLayout(value)); self.assertFalse(self.keyboard.layouts)
        value = copy.deepcopy(self.layout); value['rows'][0][1]['states'] = {'base':'x'}
        self.assertFalse(self.keyboard.saveLayout(value))

    def test_unsupported_saved_data_is_not_silently_deleted(self):
        self.bridge.settings['keyboard_layouts'] = [{'future-schema':True}]
        previous=copy.deepcopy(self.bridge.settings)
        self.assertFalse(self.keyboard.saveLayout(self.layout))
        self.assertEqual(self.bridge.settings, previous)

    def test_missing_base_uses_physical_key_and_caps_does_not_shift_digits(self):
        self.layout['rows'][0][0]['states']={'shift':'Z'}
        self.assertTrue(self.keyboard.saveLayout(self.layout))
        self.keyboard.pressCustom(0,0); self.keyboard.backend.send.assert_called_with(['A'])
        self.keyboard.locked=['CapsLock']; self.keyboard.pressCustom(0,0)
        self.keyboard.backend.text.assert_called_with('Z')
        value=copy.deepcopy(self.keyboard.customLayout); value['rows'][0][0].update(key='1',states={'base':'1','shift':'!'})
        self.assertTrue(self.keyboard.saveLayout(value))
        self.keyboard.pressCustom(0,0); self.keyboard.backend.text.assert_called_with('1')

    def test_unicode_input_uses_utf16_packets_and_releases_partial_input(self):
        backend = WindowsKeyboard.__new__(WindowsKeyboard); backend.user = Mock()
        batches = []
        def send(count, batch, size):
            batches.append([(item.keyboard.vk,item.keyboard.scan,item.keyboard.flags) for item in batch]); return count
        backend.user.SendInput.side_effect = send
        backend.text('я😀')
        self.assertEqual(batches[0], [(0,0x44f,4),(0,0x44f,6),(0,0xd83d,4),(0,0xd83d,6),(0,0xde00,4),(0,0xde00,6)])
        backend.user.SendInput.side_effect = lambda count,batch,size: count-1
        with self.assertRaises(OSError): backend.text('a')
        self.assertTrue(all(item.keyboard.flags == 6 for item in backend.user.SendInput.call_args.args[1]))

    def test_auto_focus_needs_a_new_tap_after_manual_hide_and_sleep(self):
        power = Power(); auto = AutoKeyboard(self.keyboard,power,native=False)
        def focus(editable, identity):
            auto.serial += 1; auto.accept(auto.serial, editable, identity)
        def tap(identity):
            auto.request((100,100)); auto.accept(auto.serial,True,identity)
        try:
            focus(True,('field',1)); self.assertFalse(self.keyboard.visible)
            tap(('field',1)); self.assertTrue(self.keyboard.visible)
            old = auto.serial
            self.keyboard.hide(); auto.accept(old,True,('field',2)); self.assertFalse(self.keyboard.visible)
            focus(True,('field',1)); self.assertFalse(self.keyboard.visible)
            focus(True,('field',2)); self.assertFalse(self.keyboard.visible)
            tap(('field',2)); self.assertTrue(self.keyboard.visible)
            focus(False,('button',1)); self.assertFalse(self.keyboard.visible)
            self.keyboard.show(); focus(False,('button',2)); self.assertTrue(self.keyboard.visible)
            self.keyboard.hide(); power.sleeping=True; power.changed.emit()
            auto.accept(auto.serial,True,('field',3)); self.assertFalse(self.keyboard.visible)
            power.sleeping=False; self.keyboard.setAutoShow(False)
            auto.accept(auto.serial,True,('field',4)); self.assertFalse(self.keyboard.visible)
        finally: auto.close()

    def test_uia_reads_only_edit_metadata_and_releases_runtime_identity(self):
        from contextlib import contextmanager
        @contextmanager
        def element(): yield ctypes.c_void_p(1)
        ole = Mock(); data=(ctypes.c_int*3)(1,2,3)
        properties={30003:50004,30010:True,30008:True,30046:False}
        seen=[]
        def native(pointer,index,types=(),args=()):
            if index==10:
                identity,output=args; seen.append(identity); variant=output._obj
                if type(properties[identity]) is bool:
                    variant.type=11; ctypes.c_short.from_buffer(variant.data).value=-1 if properties[identity] else 0
                else: variant.type=3; ctypes.c_int.from_buffer(variant.data).value=properties[identity]
            if index==4: args[0]._obj.value=2
        ole.SafeArrayGetLBound.side_effect=lambda pointer,dimension,result: setattr(result._obj,'value',0) or 0
        ole.SafeArrayGetUBound.side_effect=lambda pointer,dimension,result: setattr(result._obj,'value',2) or 0
        ole.SafeArrayAccessData.side_effect=lambda pointer,result: setattr(result._obj,'value',ctypes.addressof(data)) or 0
        with patch('ngs.auto_keyboard._pointer',element),patch('ngs.auto_keyboard._call',side_effect=native):
            self.assertEqual(focused_edit(1,ole),(True,(1,2,3)))
            properties[30046]=True
            self.assertEqual(focused_edit(1,ole),(False,(1,2,3)))
        self.assertEqual(set(seen),{30003,30010,30008,30046})
        self.assertEqual(ole.VariantClear.call_count,8)
        self.assertEqual(ole.SafeArrayDestroy.call_count,2)

    def test_uia_tap_queries_the_element_under_the_finger_without_reading_text(self):
        from contextlib import contextmanager
        @contextmanager
        def element(): yield ctypes.c_void_p(1)
        ole = Mock()
        ole.SafeArrayGetLBound.return_value = -1
        calls = []
        def native(pointer,index,types=(),args=()):
            calls.append((index,args))
            if index == 10:
                identity, output = args
                self.assertIn(identity, (30002,30003,30010,30008,30046,30149))
        with patch('ngs.auto_keyboard._pointer',element), patch('ngs.auto_keyboard._call',side_effect=native):
            focused_edit(1,ole,point=(125,240))
        self.assertEqual(calls[0][0],7)
        self.assertEqual((calls[0][1][0].x,calls[0][1][0].y),(125,240))


if __name__ == '__main__': unittest.main()
