import copy
import unittest
from unittest.mock import patch
from tests import test_ui
from PySide6.QtCore import QObject, QPointF, QMetaObject, Qt
from PySide6.QtQml import QQmlEngine, QQmlExpression
from PySide6.QtTest import QTest


class ControlUiTests(unittest.TestCase):
    def test_input_options_toggle_independently_from_touch_and_gamepad_and_persist(self):
        pane=self.window.findChild(QObject,'settingsPane')
        keyboard=self.bridge.keyboard
        original_enabled=keyboard.enabled;original_overlay=keyboard.options['overlay_enabled']
        try:
            keyboard.setEnabled(True);keyboard.setOverlayEnabled(True)
            pane.open();QTest.qWait(220);pane.setProperty('selectedField',19);QTest.qWait(400)
            self.expression(pane,'revealSelected()');QTest.qWait(150)
            toggle=pane.findChild(QObject,'settingsKeyboardEnabled')
            point=toggle.mapToScene(QPointF(toggle.width()/2,toggle.height()/2)).toPoint()
            QTest.mouseClick(self.window,Qt.MouseButton.LeftButton,pos=point);QTest.qWait(120)
            self.assertTrue(keyboard.enabled)
            self.assertTrue(self.expression(pane,'save()'))
            self.assertFalse(keyboard.enabled or toggle.property('checked'))
            self.assertTrue(keyboard.options['overlay_enabled'])
            keyboard.show();self.assertFalse(keyboard.visible)
            pane.setProperty('selectedField',20);self.bridge.action.emit('activate');QTest.qWait(120)
            self.assertTrue(keyboard.options['overlay_enabled'])
            self.assertTrue(self.expression(pane,'save()'))
            overlay_toggle=pane.findChild(QObject,'settingsOverlayEnabled')
            self.assertFalse(keyboard.options['overlay_enabled'] or overlay_toggle.property('checked'))
            self.bridge.status_overlay.show('screen');self.assertFalse(self.bridge.status_overlay.visible)
            pane.close();QTest.qWait(220);pane.open();QTest.qWait(220)
            self.assertFalse(toggle.property('checked') or overlay_toggle.property('checked'))
            saved=self.bridge.storage.read_settings()['keyboard_options']
            self.assertIs(saved['enabled'],False);self.assertIs(saved['overlay_enabled'],False)
            pane.setProperty('selectedField',19);self.bridge.action.emit('activate')
            self.assertTrue(self.expression(pane,'save()'))
            self.assertTrue(keyboard.enabled and toggle.property('checked'))
            self.assertFalse(keyboard.options['overlay_enabled'])
            pane.setProperty('selectedField',20);self.bridge.action.emit('activate')
            self.assertTrue(self.expression(pane,'save()'))
            self.assertTrue(keyboard.options['overlay_enabled'] and overlay_toggle.property('checked'))
            self.assertTrue(toggle.property('checked'))
        finally:
            keyboard.setEnabled(original_enabled);keyboard.setOverlayEnabled(original_overlay);pane.close()

    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.bridge = self.engine.bridge
        self.bridge.system_controls.closing = False
        self.original = copy.deepcopy(self.bridge.settings)
        self.window.setProperty('section', 1); self.window.setProperty('focusZone', 2)
        self.window.setProperty('selectedIndex', 0)

    def tearDown(self):
        for name in ('settingsPane', 'systemPanel', 'detailsPopup', 'localScanner', 'exitPopup', 'padPresetPicker', 'padKeyboardEditor', 'keyboardLayoutEditor'):
            self.window.findChild(QObject, name).setProperty('visible', False)
        self.bridge.virtual_keyboard.hide()
        self.bridge.system_controls.close(); self.app.processEvents()
        self.bridge.settings = self.original; self.bridge.settingsChanged.emit(); self.bridge.save()
        self.window.resize(1280, 800); QTest.qWait(180)

    def expression(self, obj, text):
        expression = QQmlExpression(QQmlEngine.contextForObject(obj), obj, text)
        result, _ = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return result

    def visual(self, item, name):
        if item.objectName() == name: return item
        for child in item.childItems():
            found = self.visual(child, name)
            if found is not None: return found

    def test_combo_all_themes_and_accents_are_visible_and_touch_selectable(self):
        self.bridge.action.emit('settings'); QTest.qWait(400)
        pane = self.window.findChild(QObject, 'settingsPane')
        for name, count in [('settingsTheme', 2), ('settingsAccent', 5)]:
            choice = pane.findChild(QObject, name)
            self.expression(choice, 'popup.open()'); QTest.qWait(420)
            self.assertGreater(self.expression(choice, 'popup.height'), 40 * count)
            rows = self.expression(choice, 'popup.contentItem.contentItem').childItems()
            delegates = [item for item in rows if item.property('index') is not None]
            self.assertEqual(len(delegates), count)
            for index, row in enumerate(delegates):
                self.assertEqual(row.property('index'), index)
                self.assertGreater(row.height(), 30)
                self.assertTrue(row.isVisible())
            QMetaObject.invokeMethod(delegates[-1], 'clicked'); QTest.qWait(200)
            self.assertEqual(choice.property('currentIndex'), count - 1)
        self.assertTrue(pane.property('dirty'))
        self.assertTrue(self.expression(pane, 'save()'))
        self.assertEqual(self.bridge.settings['theme'], 'light')
        self.assertEqual(self.bridge.settings['accent_color'], 'rose')

    def test_status_panels_stay_in_ngs_brightness_slider_and_errors(self):
        controls = self.bridge.system_controls
        panel = self.window.findChild(QObject, 'systemPanel')
        def respond(key, action, value=None, password="", progress=None):
            return dict(items=[dict(id='network', name='Test network', secure=False, saved=False, connected=False, quality=85)] if key == 'wifi' else [], brightness=65, volume=35, muted=False)
        with patch('ngs.system_controls.operate', side_effect=respond) as operated, patch('ngs.app.QDesktopServices.openUrl') as external:
            for index, key in enumerate(self.window.property('statusKeys').toVariant()):
                self.window.setProperty('focusZone', -1); self.window.setProperty('statusIndex', index)
                self.bridge.action.emit('activate'); QTest.qWait(430)
                self.assertTrue(panel.property('visible')); self.assertEqual(panel.property('kind'), key)
                self.assertFalse(controls.panel['busy'])
                expected = 'scan' if key in ('wifi', 'bluetooth') else 'read'
                self.assertEqual(operated.call_args.args[:2], (key, expected))
                if key == 'brightness':
                    self.bridge.action.emit('right'); QTest.qWait(300)
                    self.assertEqual(operated.call_args.args[:3], ('brightness', 'set', 70))
                self.bridge.action.emit('back'); QTest.qWait(180)
                self.assertFalse(panel.property('visible'))
            external.assert_not_called()
        with patch('ngs.system_controls.operate', side_effect=OSError('Нет драйвера яркости')):
            self.expression(panel, 'showPanel("brightness")'); QTest.qWait(430)
            self.assertIn('Нет драйвера', controls.panel['error'])

    def test_audio_mixer_controls_target_session_and_output_with_gamepad(self):
        controls = self.bridge.system_controls; panel = self.window.findChild(QObject, 'systemPanel')
        state = dict(items=[], outputs=[dict(id='speakers', name='Динамики'), dict(id='headphones', name='Наушники')],
                     endpoint='speakers', sessions=[dict(id='music', name='Музыка', volume=40, muted=False),
                                                    dict(id='game', name='Игра', volume=70, muted=True)], volume=80, muted=False)
        with patch('ngs.system_controls.operate', return_value=state) as operated, patch('ngs.app.QDesktopServices.openUrl') as external:
            self.expression(panel, 'showPanel("volume")'); QTest.qWait(430)
            self.bridge.action.emit('down'); self.assertEqual(panel.property('selectedField'), 1000)
            self.bridge.action.emit('right'); QTest.qWait(350)
            self.assertEqual(operated.call_args.args[:3], ('volume', 'session_set', dict(endpoint='speakers', session='music', value=45)))
            self.bridge.action.emit('down'); self.bridge.action.emit('activate'); QTest.qWait(350)
            self.assertEqual(operated.call_args.args[:3], ('volume', 'session_mute', dict(endpoint='speakers', session='music', value=True)))
            panel.setProperty('selectedField', -2); self.bridge.action.emit('activate'); QTest.qWait(150)
            self.bridge.action.emit('down'); self.bridge.action.emit('activate'); QTest.qWait(350)
            self.assertEqual(operated.call_args.args[:3], ('volume', 'output', 'headphones'))
            self.assertTrue(panel.property('visible')); external.assert_not_called()
            self.assertEqual(panel.findChild(QObject, 'audioOutput').property('currentText'), 'Динамики')
            panel.setProperty('selectedField', -2); self.bridge.action.emit('activate'); QTest.qWait(150)
            self.bridge.action.emit('down'); self.assertEqual(panel.property('outputCursor'), 1)
            self.bridge.action.emit('back'); QTest.qWait(150)
            self.assertEqual(panel.findChild(QObject, 'audioOutput').property('currentText'), 'Динамики')
            self.window.grabWindow().save(str(self.repo / 'reports/status-panel-mixer-populated.png'))

    def test_brightness_panel_is_compact_and_anchored_under_its_button(self):
        panel = self.window.findChild(QObject, 'systemPanel')
        with patch('ngs.system_controls.operate', return_value=dict(items=[], brightness=60)):
            self.window.setProperty('focusZone', -1)
            keys = self.window.property('statusKeys').toVariant(); self.window.setProperty('statusIndex', keys.index('brightness'))
            self.bridge.action.emit('activate'); QTest.qWait(430)
            self.assertLess(panel.property('height'), self.window.height() * 0.55)
            self.assertLess(panel.property('y'), 110)
            self.assertGreater(panel.property('x'), self.window.width() * 0.3)

    def test_energy_saver_toggles_from_our_panel_and_shows_confirmed_state(self):
        panel = self.window.findChild(QObject, 'systemPanel')
        def respond(key, action, value, password):
            return dict(items=[], energy_saver=value if action == 'energy_saver' else False, energy_saver_available=True)
        with patch('ngs.system_controls.operate', side_effect=respond) as operated, patch('ngs.app.QDesktopServices.openUrl') as external:
            self.expression(panel, 'showPanel("battery")'); QTest.qWait(430)
            toggle = panel.findChild(QObject, 'energySaverToggle'); self.assertTrue(toggle.property('enabled'))
            self.assertIn('выключена', toggle.property('text'))
            self.bridge.action.emit('activate'); QTest.qWait(300)
            self.assertEqual(operated.call_args.args[:3], ('battery', 'energy_saver', True))
            self.assertIn('включена', toggle.property('text')); external.assert_not_called()
            self.window.grabWindow().save(str(self.repo / 'reports/status-panel-energy-saver.png'))

    def test_top_row_triggers_follow_pixels_and_add_is_a_separate_reachable_action(self):
        self.window.setProperty('section', 0)
        self.window.setProperty('focusZone', 2)
        entries = []
        for _ in range(11):
            self.bridge.action.emit('next_control')
            name = 'status-' + self.window.property('statusKeys').toVariant()[self.window.property('statusIndex')] if self.window.property('focusZone') == -1 else {9:'gameSearchButton', 10:'gameViewButton', 2:'downloadsButton', 4:'minimizeButton', 5:'menuButton'}[self.window.property('navigationIndex')]
            button = self.window.findChild(QObject, name) or next(item for item in self.window.findChild(QObject, 'systemStatusBar').childItems() if item.objectName() == name)
            entries.append(button.mapToScene(QPointF()).x())
        self.assertEqual(entries, sorted(entries))
        self.window.setProperty('focusZone', 0); self.window.setProperty('navigationIndex', 8)
        self.bridge.action.emit('right')
        self.assertEqual(self.window.property('navigationIndex'), 3)
        with patch.object(self.bridge, 'browseLocalFolder') as browse:
            self.bridge.action.emit('activate'); QTest.qWait(400)
            self.assertTrue(self.window.findChild(QObject, 'localScanner').property('visible'))
            browse.assert_called_once()

    def test_start_opens_details_and_dpad_follows_the_actual_rows(self):
        self.bridge.action.emit('menu'); QTest.qWait(420)
        pane = self.window.findChild(QObject, 'detailsPopup')
        self.assertTrue(pane.property('visible'))
        self.assertNotEqual(self.window.property('detailsAction'), 1)
        self.window.setProperty('detailsAction', 8)
        self.bridge.action.emit('activate')
        self.assertTrue(self.window.property('detailsMore'))
        self.window.setProperty('detailsAction', 5)
        self.bridge.action.emit('down')
        self.assertNotEqual(self.window.property('detailsAction'), 5)
        self.bridge.action.emit('up')
        self.assertEqual(self.window.property('detailsAction'), 5)

    def test_a_launches_once_after_tile_motion_without_opening_details(self):
        previous = self.bridge._installed
        try:
            card = dict(self.bridge._games[0])
            self.bridge._installed = [dict(card=card, kind='external')]; self.bridge.libraryChanged.emit()
            self.window.setProperty('section', 0); self.window.setProperty('selectedIndex', 0)
            self.window.setProperty('focusZone', 2); QTest.qWait(100)
            with patch.object(self.bridge, 'performGameAction') as launch:
                self.bridge.action.emit('activate'); QTest.qWait(80)
                tile = self.window.findChild(QObject, 'launchTile')
                self.assertTrue(tile.property('visible')); self.assertGreater(tile.property('progress'), 0)
                self.assertFalse(self.window.findChild(QObject, 'detailsPopup').property('visible'))
                self.bridge.action.emit('activate'); launch.assert_not_called()
                QTest.qWait(330)
                launch.assert_called_once_with(card['id'])
                self.assertFalse(tile.property('visible'))
        finally:
            self.bridge._installed = previous; self.bridge.libraryChanged.emit()

    def test_status_panel_layout_including_password_and_pair_confirmation_fits(self):
        panel = self.window.findChild(QObject, 'systemPanel')
        with patch('ngs.system_controls.operate', return_value=dict(items=[], brightness=50, volume=50)):
            for size in ((1280, 720), (1280, 800), (1920, 1080)):
                self.window.resize(*size)
                for scale in (1, 1.2):
                    self.bridge.settings['ui_scale'] = scale; self.bridge.settingsChanged.emit()
                    for kind in ('wifi', 'bluetooth', 'battery', 'brightness', 'volume'):
                        self.expression(panel, 'showPanel("' + kind + '")'); QTest.qWait(370)
                        panel.setProperty('networkId', 'test' if kind == 'wifi' else '')
                        panel.setProperty('unpairId', 'test' if kind == 'bluetooth' else '')
                        QTest.qWait(30)
                        content = panel.property('contentItem')
                        for item in content.childItems():
                            if not item.isVisible(): continue
                            start = item.mapToScene(QPointF())
                            end = item.mapToScene(QPointF(item.width(), item.height()))
                            self.assertGreaterEqual(start.x(), 0)
                            self.assertGreaterEqual(start.y(), 0)
                            self.assertLessEqual(end.x(), size[0] + 1)
                            self.assertLessEqual(end.y(), size[1] + 1)
                        if size == (1280, 800) and scale == 1.2:
                            self.window.grabWindow().save(str(self.repo / ('reports/status-panel-' + kind + '.png')))
                        self.bridge.action.emit('back'); QTest.qWait(160)

    def test_status_switch_discards_old_result_and_pairing_releases_focus_only_until_result(self):
        import threading
        controls = self.bridge.system_controls
        entered, release = threading.Event(), threading.Event()
        handoff = []
        self.bridge.systemSettingsFocus.connect(handoff.append)
        def operation(key, action, value, password):
            if key == 'wifi':
                entered.set(); release.wait(3)
                return dict(items=[dict(id='old', name='Old network')])
            if action == 'pair': raise OSError('Pairing was declined')
            return dict(items=[], brightness=75)
        try:
            with patch('ngs.system_controls.operate', side_effect=operation):
                controls.request('wifi', 'read', None, '')
                for _ in range(100):
                    QTest.qWait(5)
                    if entered.is_set(): break
                self.assertTrue(entered.is_set())
                controls.request('brightness', 'read', None, '')
                self.assertEqual(controls.panel['key'], 'brightness')
                release.set()
                for _ in range(100):
                    QTest.qWait(5)
                    if not controls.panel['busy']: break
                self.assertEqual(controls.panel['items'], [])
                self.assertEqual(controls.panel['brightness'], 75)
                controls.request('bluetooth', 'pair', 'test-device', '')
                self.assertEqual(handoff[-1], True)
                for _ in range(100):
                    QTest.qWait(5)
                    if not controls.panel['busy']: break
                self.assertEqual(handoff[-1], False)
                self.assertIn('declined', controls.panel['error'])
        finally:
            release.set(); controls.close(); self.app.processEvents()
            self.bridge.systemSettingsFocus.disconnect(handoff.append)

    def test_keyboard_zero_keeps_service_key_and_separate_settings_usable(self):
        keyboard = self.bridge.keyboard
        keys = self.window.findChild(QObject, 'virtualKeyboardWindow')
        keyboard.show(); QTest.qWait(300)
        for view in ('touch', 'full', 'numpad'):
            keyboard.setView(view); QTest.qWait(20)
            names = keys.property('toolKeys').toVariant() + [name for row in keys.property('rows').toVariant() for name in row]
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(names.count('@settings'), 1); self.assertEqual(names.count('@hide'), 1)
        service = self.visual(keys.contentItem(), 'keyboardViewMenuButton')
        QMetaObject.invokeMethod(service,'clicked'); QTest.qWait(50)
        settings = keys.findChild(QObject,'keyboardSettingsWindow')
        self.assertTrue(settings.isVisible(), self.messages[-8:])
        self.assertTrue(settings.flags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertEqual(settings.opacity(),1)
        keyboard.setKeyboardOpacity(0); QTest.qWait(30)
        self.assertEqual(keys.opacity(),1); self.assertEqual(keys.property('contentOpacity'),0)
        service = self.visual(keys.contentItem(),'keyboardViewMenuButton')
        self.assertGreaterEqual(service.property('opacity'),.45)
        slider = keys.findChild(QObject,'keyboardOpacitySlider')
        self.assertEqual(slider.property('from'),0); self.assertGreater(slider.width(),200)
        keyboard.action.emit('back'); self.assertFalse(settings.isVisible())
        self.assertTrue(keyboard.visible)

    def test_keyboard_height_percent_is_exact_and_independent_of_dpi_and_launcher_scale(self):
        from PySide6.QtCore import QRect
        from unittest.mock import Mock
        keyboard = self.bridge.keyboard
        keys = self.window.findChild(QObject,'virtualKeyboardWindow')
        previous_screen = keyboard.screen
        sizes = []
        try:
            for width,height in ((800,500),(1280,720),(1280,800),(1707,1067),(1920,1080)):
                screen=Mock(); screen.geometry.return_value=QRect(-width,0,width,height); screen.devicePixelRatio.return_value=1.5
                keyboard.set_screen(screen); keyboard.show(); QTest.qWait(280)
                for view in ('touch','full','numpad'):
                    keyboard.setView(view)
                    for percent in (25,40,55,70):
                        keyboard.setHeight(percent)
                        heights=[]
                        for scale in (.8,1,1.2):
                            self.bridge.settings['ui_scale']=scale; self.bridge.settingsChanged.emit(); QTest.qWait(10)
                            self.assertAlmostEqual(keys.height(),height*percent/100,delta=.6)
                            self.assertEqual(keys.width(),width)
                            self.assertAlmostEqual(keys.y()+keys.height(),height,delta=1)
                            self.assertEqual(len(keys.property('rows').toVariant()),5 if view=='full' else 4)
                            heights.append(keys.height())
                        self.assertEqual(len(set(heights)),1)
                        sizes.append(dict(width=width,height=height,view=view,percent=percent,panel_height=keys.height()))
            screen.geometry.return_value=QRect(0,0,1280,800); keyboard.set_screen(screen); keyboard.setView('touch'); keyboard.setHeight(40)
            keyboard.language='RU'; keyboard.locked=[]; keyboard.modifiers=[]; keyboard.changed.emit(); QTest.qWait(300)
            self.assertEqual(self.expression(keys,'keyLabel("Q")'),'й')
            self.assertTrue(keys.grabWindow().save(str(self.repo/'reports/keyboard-touch-russian.png')))
            keyboard.language='EN'; keyboard.changed.emit(); QTest.qWait(80)
            self.assertEqual(self.expression(keys,'keyLabel("Q")'),'q')
            self.assertTrue(keys.grabWindow().save(str(self.repo/'reports/keyboard-touch-english.png')))
            import json
            (self.repo/'reports/keyboard-touch-geometry.json').write_text(json.dumps(sizes,indent=2),encoding='utf8')
            for width,height in ((1280,720),(1280,800)):
                self.window.resize(width,height)
                keyboard.setHeight(40); QTest.qWait(20)
                self.assertGreater(keys.property('keySide'),40)
        finally:
            keyboard.hide(); keyboard.set_screen(previous_screen)

    def test_keyboard_service_menu_controller_height_and_swipe_hide(self):
        from unittest.mock import Mock
        keyboard=self.bridge.keyboard; keys=self.window.findChild(QObject,'virtualKeyboardWindow')
        previous=keyboard.backend; keyboard.backend=Mock()
        try:
            keyboard.setView('touch'); keyboard.show(); QTest.qWait(45)
            self.assertGreater(keys.property('reveal'),0); self.assertLess(keys.property('reveal'),1)
            QTest.qWait(260)
            keyboard.action.emit('next_control'); keyboard.action.emit('activate'); QTest.qWait(30)
            self.assertTrue(keys.property('menuOpen'))
            keyboard.action.emit('next_control')
            percent=keyboard.options['height_percent']; keyboard.action.emit('right')
            self.assertEqual(keyboard.options['height_percent'],percent+1)
            keyboard.action.emit('back'); self.assertFalse(keys.property('menuOpen'))
            device=QTest.createTouchDevice()
            touch=QTest.touchEvent(keys,device)
            touch.press(0, QPointF(90,35).toPoint(),keys).commit()
            touch.move(0,QPointF(90,65).toPoint(),keys).commit(); QTest.qWait(15)
            touch.move(0,QPointF(90,145).toPoint(),keys).commit(); QTest.qWait(30)
            touch.release(0,QPointF(90,145).toPoint(),keys).commit(); QTest.qWait(300)
            self.assertFalse(keyboard.visible); self.assertFalse(keys.isVisible())
            keyboard.backend.send.assert_not_called()
        finally: keyboard.backend=previous

    def test_keyboard_windows11_keys_print_screen_quick_pads_and_native_footer(self):
        from unittest.mock import Mock
        from PySide6.QtCore import QRect
        from ngs.touchpad import NativeTouchpads
        keyboard = self.bridge.keyboard; keys = self.window.findChild(QObject,'virtualKeyboardWindow')
        left = self.window.findChild(QObject,'leftTouchpadWindow')
        previous_backend, previous_screen = keyboard.backend, keyboard.screen
        keyboard.backend = Mock()
        screen = Mock(); screen.geometry.return_value = QRect(0,0,1280,800); screen.devicePixelRatio.return_value = 1
        try:
            keyboard.set_screen(screen); keyboard.show()
            for view,height,opacity in (('touch',40,.95),('full',50,.8),('numpad',35,.65)):
                keyboard.setView(view); keyboard.setHeight(height); keyboard.setKeyboardOpacity(opacity)
            for view,height,opacity in (('touch',40,.95),('full',50,.8),('numpad',35,.65)):
                keyboard.setView(view); QTest.qWait(320)
                self.assertEqual(keys.height(),800*height/100)
                self.assertAlmostEqual(keys.property('contentOpacity'),opacity)
                rows = keys.property('rows').toVariant(); tools = keys.property('toolKeys').toVariant(); flat = tools + sum(rows,[])
                self.assertEqual(flat.count('PrintScreen'),1); self.assertEqual(flat.count('@touchpads'),1)
                self.assertEqual(len(flat),len(set(flat)))
                for key in ('PrintScreen','@touchpads'):
                    if key in tools: button = self.visual(keys.contentItem(),'keyboardTool-' + key)
                    else:
                        row = next(i for i,items in enumerate(rows) if key in items); column = rows[row].index(key)
                        button = self.visual(keys.contentItem(),f'keyboardKey-{row}-{column}')
                    self.assertGreaterEqual(self.expression(button,'background.radius'),3)
                    if key == 'PrintScreen':
                        QTest.mouseClick(keys,Qt.MouseButton.LeftButton,pos=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint())
                        self.assertFalse(keyboard.visible)
                        self.assertFalse(keys.isVisible())
                        QTest.qWait(360)
                        keyboard.backend.send.assert_called_with(['PrintScreen'])
                        keyboard.show(); QTest.qWait(300)
                self.assertTrue(keys.grabWindow().save(str(self.repo/f'reports/keyboard-win11-{view}.png')))
            keyboard.setView('touch'); QTest.qWait(60)
            self.expression(keys,'pressKey("@symbols",0,0)')
            self.assertIn('PrintScreen',keys.property('toolKeys').toVariant() + sum(keys.property('rows').toVariant(),[]))
            self.expression(keys,'pressKey("@letters",0,0)')
            self.expression(keys,'pressKey("@touchpads",0,0)'); QTest.qWait(60)
            self.assertTrue(left.isVisible()); self.assertFalse(keys.isVisible())
            self.assertFalse(left.property('controlsOpen'))
            self.assertEqual(len(left.property('toolRects').toVariant()),4)
            footer = left.findChild(QObject,'touchpadSensitivity')
            QTest.mouseClick(left,Qt.MouseButton.LeftButton,pos=footer.mapToScene(QPointF(footer.width()/2,footer.height()/2)).toPoint())
            self.assertTrue(left.property('controlsOpen'))
            with patch('ngs.touchpad.ctypes.WinDLL',return_value=Mock(),create=True): native = NativeTouchpads(keyboard,[left])
            for tool,field,expected in ((5,'pointer_sensitivity',1.25),(7,'touchpad_height_percent',51),(9,'opacity',.55)):
                rect = next(r for r in left.property('toolRects').toVariant() if r['index'] == tool)
                x,y = rect['x']+rect['width']/2,rect['y']+rect['height']/2
                native.contact(42,0x246,tool,x,y,left); native.contact(42,0x247,tool,x,y,left)
                QTest.qWait(20)
                self.assertAlmostEqual(keyboard.options[field],expected)
            keyboard.backend.mouse.assert_not_called()
            self.assertTrue(left.grabWindow().save(str(self.repo/'reports/touchpad-win11-controls.png')))
            self.expression(left,'activateTool(3)')
            self.assertFalse(left.property('controlsOpen'))
            keyboard.action.emit('activate')
            self.assertFalse(left.property('controlsOpen'))
            keyboard.padTool.emit(False, 3)
            self.assertTrue(left.property('controlsOpen'))
            self.expression(left,'activateTool(0)'); QTest.qWait(300)
            self.assertFalse(keyboard.touchpadMode)
            self.assertEqual(keys.height(),320); self.assertAlmostEqual(keys.property('contentOpacity'),.95)
            keyboard.setTouchpads(True); QTest.qWait(30)
            self.assertFalse(left.property('controlsOpen'))
            self.assertTrue(left.grabWindow().save(str(self.repo/'reports/touchpad-win11.png')))
            bad = [m for m in self.messages if 'KeyboardSurface.qml' in m or 'TouchpadWindow.qml' in m or 'KeyboardWindow.qml' in m]
            self.assertEqual(bad,[])
        finally:
            keyboard.hide(); keyboard.backend = previous_backend; keyboard.set_screen(previous_screen)

    def test_reference_rows_keep_compact_enter_aligned_navigation_and_mic(self):
        from unittest.mock import Mock
        keyboard = self.bridge.keyboard; keys = self.window.findChild(QObject,'virtualKeyboardWindow')
        previous = keyboard.backend; keyboard.backend = Mock()
        try:
            keyboard.show(); self.bridge.set_input_device('touch')
            for language in ('EN','RU'):
                keyboard.language=language; keyboard.changed.emit()
                for view in ('touch','full'):
                    keyboard.setView(view); QTest.qWait(300)
                    rows=keys.property('rows').toVariant(); controls=[]
                    for r,row in enumerate(rows):
                        buttons=[self.visual(keys.contentItem(),f'keyboardKey-{r}-{c}') for c in range(len(row))]
                        controls.append(buttons)
                        for c,button in enumerate(buttons):
                            self.assertGreater(button.width(),0)
                            self.assertEqual(self.expression(button,'background.border.width'),0)
                            self.assertIsNone(self.expression(button,'background.gradient'))
                            if c: self.assertGreaterEqual(button.x(),buttons[c-1].x()+buttons[c-1].width()+keys.property('gap')-.1)
                        if view=='full':
                            self.assertAlmostEqual(buttons[-3].x()+buttons[-3].width()+16,buttons[-2].x(),delta=.1)
                            self.assertAlmostEqual(buttons[-1].x()+buttons[-1].width(),keys.property('keysWidth'),delta=.1)
                    enter_row=next(r for r,row in enumerate(rows) if 'Enter' in row)
                    enter=controls[enter_row][rows[enter_row].index('Enter')]
                    self.assertLessEqual(enter.width()+keys.property('gap'),(controls[enter_row][1].width()+keys.property('gap'))*(2.21 if view=='full' else 1.51))
                    self.assertEqual(sum(row.count('@voice') for row in rows),1)
                    if view=='touch':
                        q=controls[0][rows[0].index('Q')]; point=q.mapToScene(QPointF(q.width()/2,q.height()/2)).toPoint()
                        keyboard.backend.send.reset_mock()
                        QTest.mousePress(keys,Qt.MouseButton.LeftButton,pos=point); QTest.qWait(540)
                        QTest.mouseRelease(keys,Qt.MouseButton.LeftButton,pos=point)
                        keyboard.backend.send.assert_called_once_with(['1'])
                    self.assertTrue(keys.grabWindow().save(str(self.repo/f'reports/keyboard-reference-{view}-{language.lower()}.png')))
            self.assertFalse([m for m in self.messages if 'KeyboardWindow.qml' in m or 'KeyboardGlyph.qml' in m])
        finally: keyboard.backend=previous

    def test_all_reference_views_keep_keys_accessible_and_default_rows_even(self):
        from unittest.mock import Mock
        from PySide6.QtCore import QRect
        keyboard = self.bridge.keyboard; keys = self.window.findChild(QObject,'virtualKeyboardWindow')
        previous_backend, previous_screen = keyboard.backend, keyboard.screen
        keyboard.backend = Mock()
        try:
            keyboard.show(); self.bridge.set_input_device('touch')
            for width in (600,1280):
                keyboard.screen = Mock(); keyboard.screen.geometry.return_value = QRect(0,0,width,800)
                for language in ('EN','RU'):
                    keyboard.language = language
                    for view in keyboard.views:
                        keyboard.setView(view['id']); keyboard.changed.emit(); QTest.qWait(40)
                        rows = keys.property('rows').toVariant(); controls = []
                        for r,row in enumerate(rows):
                            buttons = [self.visual(keys.contentItem(),f'keyboardKey-{r}-{c}') for c in range(len(row))]
                            controls.append(buttons)
                            for c,button in enumerate(buttons):
                                self.assertGreater(button.width(),0,(view,language,width,r,c))
                                self.assertGreater(button.height(),0)
                                self.assertGreaterEqual(button.x(),-.1)
                                self.assertLessEqual(button.x()+button.width(),keys.property('keysWidth')+.1,(view,language,width,r,c))
                                if c: self.assertGreaterEqual(button.x(),buttons[c-1].x()+buttons[c-1].width()+keys.property('gap')-.1)
                        all_keys = sum(rows,[])
                        for key in ('Esc','Tab','Ctrl','Win','Alt','@voice'):
                            self.assertIn(key, all_keys,(view,key))
                        self.assertIn('@touchpads',keys.property('toolKeys').toVariant())
                        self.assertTrue('PrintScreen' in all_keys or 'PrintScreen' in keys.property('toolKeys').toVariant())
                        if view['id'] == 'touch':
                            for buttons in controls:
                                self.assertAlmostEqual(buttons[0].x(),0,delta=.1)
                                self.assertAlmostEqual(buttons[-1].x()+buttons[-1].width(),keys.property('keysWidth'),delta=.1)
                            letters = [controls[r][c].width() for r,row in enumerate(rows[:3]) for c,key in enumerate(row) if len(key)==1 and key.isalpha()]
                            self.assertAlmostEqual(min(letters),max(letters),delta=.1)
                        elif view['id'] == 'split':
                            for r,buttons in enumerate(controls):
                                cut = 5 if r == 3 else 6
                                self.assertLessEqual(buttons[cut-1].x()+buttons[cut-1].width(),keys.property('splitHalfWidth')+.1)
                                self.assertGreaterEqual(buttons[cut].x(),keys.property('keysWidth')-keys.property('splitHalfWidth')-.1)
                            self.assertEqual(all_keys.count('Space'),2)
                            for r,row in enumerate(rows):
                                for c,key in enumerate(row):
                                    if key == 'Space':
                                        button=controls[r][c]; point=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
                                        keyboard.backend.send.reset_mock(); QTest.mouseClick(keys,Qt.MouseButton.LeftButton,pos=point)
                                        keyboard.backend.send.assert_called_once_with(['Space'])
                        elif view['id'] == 'small':
                            self.assertLess(keys.width(),width)
                        elif view['id'] == 'navigation':
                            self.assertEqual(len(rows),1)
                            self.assertLess(keys.height(),200)
                        if width == 1280:
                            self.assertTrue(keys.grabWindow().save(str(self.repo/f"reports/keyboard-views-{view['id']}-{language.lower()}.png")))
                        keyboard.action.emit('next_section')
                        ids = [item['id'] for item in keyboard.views]
                        self.assertEqual(keyboard.options['view'],ids[(ids.index(view['id'])+1)%len(ids)])
            self.assertFalse([m for m in self.messages if 'KeyboardWindow.qml' in m or 'KeyboardGlyph.qml' in m])
        finally:
            keyboard.hide(); keyboard.backend = previous_backend; keyboard.screen = previous_screen; keyboard.changed.emit()

    def test_keyboard_theme_cards_keep_black_touchpads_independent(self):
        keyboard = self.bridge.keyboard; keys=self.window.findChild(QObject,'virtualKeyboardWindow')
        left=self.window.findChild(QObject,'leftTouchpadWindow')
        keyboard.show(); keyboard.setView('touch'); QTest.qWait(300)
        keys.setProperty('menuOpen',True)
        self.expression(keys,'chooseMenu(40)'); self.app.processEvents()
        settings=keys.findChild(QObject,'keyboardSettingsWindow')
        light=self.visual(settings.contentItem(),'keyboardMenu-201')
        QMetaObject.invokeMethod(light,'clicked'); self.app.processEvents()
        self.assertEqual(keyboard.palette['id'],'light')
        self.expression(keys,'chooseMenu(41)'); self.app.processEvents()
        black=self.visual(settings.contentItem(),'keyboardMenu-202')
        QMetaObject.invokeMethod(black,'clicked'); self.app.processEvents()
        self.assertEqual(keyboard.padPalette['id'],'black')
        self.expression(keys,'chooseMenu(40)'); self.expression(keys,'chooseMenu(205)'); self.app.processEvents()
        self.assertEqual(keyboard.palette['id'],'lilac'); self.assertEqual(keyboard.padPalette['id'],'black')
        self.assertTrue(self.expression(keys,'menuSelected(205)'))
        self.assertFalse([m for m in self.messages if 'overrides a member' in m and any(name in m for name in ('KeyboardWindow','KeyboardSurface','TouchpadWindow'))])
        self.assertTrue(settings.grabWindow().save(str(self.repo/'reports/keyboard-themes.png')))
        keyboard.setTouchpads(True); self.app.processEvents()
        self.assertEqual(left.property('inputColors')['panel'],'#000000')

    def test_small_keyboard_can_be_dragged_without_hiding_or_sending_keys(self):
        from unittest.mock import Mock
        keyboard=self.bridge.keyboard; keys=self.window.findChild(QObject,'virtualKeyboardWindow')
        previous=keyboard.backend; keyboard.backend=Mock()
        try:
            keyboard.setTouchpads(False); keyboard.setView('small'); keyboard.show(); QTest.qWait(300)
            before=(keys.x(),keys.y())
            device=QTest.createTouchDevice(); touch=QTest.touchEvent(keys,device)
            touch.press(0,QPointF(5,25).toPoint(),keys).commit()
            touch.move(0,QPointF(45,35).toPoint(),keys).commit(); QTest.qWait(25)
            touch.move(0,QPointF(70,35).toPoint(),keys).commit(); QTest.qWait(25)
            touch.release(0,QPointF(70,35).toPoint(),keys).commit()
            self.assertNotEqual((keys.x(),keys.y()),before)
            self.assertTrue(keyboard.visible)
            self.assertGreaterEqual(keys.x(),keyboard.screenArea['x'])
            self.assertLessEqual(keys.x()+keys.width(),keyboard.screenArea['x']+keyboard.screenArea['width'])
            keyboard.backend.send.assert_not_called()
        finally: keyboard.backend=previous

    def test_small_keyboard_drag_tracks_screen_position_without_feedback(self):
        keyboard=self.bridge.keyboard; keys=self.window.findChild(QObject,'virtualKeyboardWindow')
        keyboard.setTouchpads(False); keyboard.setView('small'); keyboard.setHeight(40)
        keys.setProperty('floatX',0); keys.setProperty('floatY',-80)
        keyboard.show(); QTest.qWait(300)
        self.assertTrue(keys.isVisible(),(keyboard.touchpadMode,keyboard.remoteHost,keys.property('presenting')))
        device=QTest.createTouchDevice(); touch=QTest.touchEvent(keys,device)
        global_point=keys.position()+QPointF(5,25).toPoint()
        def send(kind, point):
            getattr(touch,kind)(0,point-keys.position(),keys).commit(); QTest.qWait(25)
        send('press',global_point)
        global_point+=QPointF(40,-20).toPoint(); send('move',global_point)
        handlers=[child for child in keys.findChildren(QObject) if 'DragHandler' in child.metaObject().className() and child.property('enabled')]
        self.assertTrue(any(child.property('active') for child in handlers),(keys.property('menuOpen'),keys.property('customMode'),[(child.metaObject().className(),child.property('enabled'),child.property('active')) for child in handlers]))
        previous=keys.position()
        for _ in range(10):
            global_point+=QPointF(8,-3).toPoint(); send('move',global_point)
            self.assertEqual(keys.position()-previous,QPointF(8,-3).toPoint(),(keys.position(),keys.size(),keys.property('floatX'),keys.property('floatY')))
            previous=keys.position()
        for _ in range(5):
            send('move',global_point)
            self.assertEqual(keys.position(),previous)
        send('release',global_point)

    def test_settings_explains_input_installer_and_requires_explicit_confirmation(self):
        from unittest.mock import Mock
        pane = self.window.findChild(QObject, 'settingsPane')
        button = pane.findChild(QObject, 'installInputHostButton')
        dialog = pane.findChild(QObject, 'inputInstallDialog')
        confirm = pane.findChild(QObject, 'inputInstallConfirm')
        previous = getattr(self.bridge, '_input_installer', None)
        try:
            self.bridge._input_installer = None
            pane.open(); QTest.qWait(250)
            with patch('ngs.input_host.start_input_installer', return_value=Mock()) as launch:
                launch.return_value.poll.return_value = None
                QMetaObject.invokeMethod(button, 'clicked')
                QTest.qWait(200)
                self.assertTrue(dialog.property('visible'))
                self.assertIn('Установить / обновить', button.property('text'))
                launch.assert_not_called()
                self.bridge.action.emit('activate')
                QTest.qWait(200)
                self.assertFalse(dialog.property('visible'))
                launch.assert_not_called()
                self.assertEqual(pane.property('childPage'), '')
                pane.setProperty('selectedField', 14)
                self.bridge.action.emit('activate'); QTest.qWait(200)
                self.bridge.action.emit('back'); QTest.qWait(200)
                self.assertFalse(dialog.property('visible'))
                self.assertEqual(pane.property('selectedField'), 14)
                launch.assert_not_called()
                self.bridge.action.emit('activate'); QTest.qWait(200)
                self.bridge.action.emit('down')
                self.bridge.action.emit('activate')
                QTest.qWait(200)
                launch.assert_called_once_with(self.bridge.storage)
                self.assertIn('автоматически', self.bridge.settingsStatus)
                pane.setProperty('selectedField', 14)
                self.bridge.action.emit('activate'); QTest.qWait(200)
                QMetaObject.invokeMethod(confirm, 'clicked'); QTest.qWait(200)
                self.assertEqual(launch.call_count, 1)
                self.assertIn('уже открыт', self.bridge.settingsStatus)
                launch.return_value.poll.return_value = 0
                with patch.object(self.bridge,'inputHostInstalled') as installed:
                    self.bridge.finishInputHostInstall()
                    installed.emit.assert_called_once_with()
                QMetaObject.invokeMethod(button, 'clicked'); QTest.qWait(200)
                QMetaObject.invokeMethod(confirm, 'clicked'); QTest.qWait(200)
                self.assertEqual(launch.call_count, 2)
        finally:
            self.bridge._input_installer = previous
            self.bridge.systemSettingsFocus.emit(False)
            pane.close()

    def test_failed_input_install_does_not_reconnect_or_claim_success(self):
        from unittest.mock import Mock
        previous=getattr(self.bridge,'_input_installer',None)
        try:
            self.bridge._input_installer=Mock()
            self.bridge._input_installer.poll.return_value=1
            with patch.object(self.bridge,'inputHostInstalled') as installed:
                self.bridge.finishInputHostInstall()
                installed.emit.assert_not_called()
            self.assertIsNone(self.bridge._input_installer)
            self.assertIn('не установлен',self.bridge.settingsStatus)
        finally: self.bridge._input_installer=previous

    def test_keyboard_toolbar_has_large_separate_touch_targets(self):
        from PySide6.QtCore import QRect
        keyboard = self.bridge.keyboard
        keys = self.window.findChild(QObject, 'virtualKeyboardWindow')
        previous_screen = keyboard.screen
        screen = type('Screen', (), {'geometry': lambda self: QRect(0, 0, 600, 800)})()
        try:
            keyboard.screen = screen
            keyboard.setView('touch'); keyboard.show(); QTest.qWait(300)
            tools = [self.visual(keys.contentItem(), name) for name in ('keyboardViewMenuButton', 'keyboardTool-@view',
                     'keyboardTool-@touchpads', 'keyboardTool-PrintScreen', 'keyboardTool-@language', 'keyboardTool-@hide')]
            self.assertTrue(all(tools))
            for tool in tools:
                self.assertGreaterEqual(tool.width(), 48)
                self.assertGreaterEqual(tool.height(), 48)
            for left, right in zip(tools, tools[1:]):
                self.assertLessEqual(left.x() + left.width(), right.x())
            self.assertLessEqual(tools[-1].x() + tools[-1].width(), keys.width())
            point = tools[0].mapToScene(QPointF(tools[0].width()/2, tools[0].height()/2)).toPoint()
            QTest.mouseClick(keys, Qt.MouseButton.LeftButton, pos=point)
            self.assertTrue(keys.property('menuOpen'))
        finally:
            keyboard.hide(); keyboard.screen = previous_screen; keyboard.changed.emit()

    def test_keyboard_hide_releases_window_without_waiting_for_reveal_animation(self):
        keyboard = self.bridge.keyboard
        keys = self.window.findChild(QObject, 'virtualKeyboardWindow')
        keyboard.setView('touch')
        keyboard.show()
        QTest.qWait(400)
        self.assertTrue(keys.property('visible'))
        keyboard.hide()
        self.app.processEvents()
        self.assertFalse(keys.property('visible'))

    def test_input_host_owns_windows_syncs_portable_settings_and_falls_back_on_disconnect(self):
        import os, subprocess, sys
        from ngs.input_host import InputHostClient
        keyboard = self.bridge.keyboard; local = self.window.findChild(QObject,'virtualKeyboardWindow')
        process = None; client = None
        logpath = self.repo/'reports/input-host-source.txt'
        probe = self.bridge.storage.temp/'input-host-probe.json'
        probe.unlink(missing_ok=True)
        script = '''import json,sys
from pathlib import Path
import ngs.virtual_keyboard as module
from PySide6.QtCore import QTimer
Base=module.VirtualKeyboard
class Probe(Base):
 def __init__(self,bridge):
  super().__init__(bridge); QTimer.singleShot(1000,self.check)
 def check(self):
  if self.screen is None: QTimer.singleShot(100,self.check); return
  self.setView('touch'); self.setHeight(47); self.setKeyboardOpacity(.71)
  self.show(); self.setTouchpads(True)
  from PySide6.QtGui import QGuiApplication
  QTimer.singleShot(300,lambda: Path(sys.argv[1]).write_text(json.dumps(dict(remote=self.remoteHost,visible=[w.objectName() for w in QGuiApplication.allWindows() if w.isVisible()],native=self.nativeTouch))))
module.VirtualKeyboard=Probe
import input_host
sys.argv=[sys.argv[0],*sys.argv[2:]]
raise SystemExit(input_host.main())
'''
        # Keep the probe filename outside argv: the entry point parses only its own arguments.
        script = script.replace('Path(sys.argv[1])','Path(' + repr(str(probe)) + ')')
        script = script.replace('sys.argv=[sys.argv[0],*sys.argv[2:]]','')
        try:
            with logpath.open('w',encoding='utf8') as log:
                def launch(executable,arguments):
                    nonlocal process
                    process=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-B','-c',script,*arguments],cwd=self.repo,
                                             env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QT_QUICK_BACKEND='software',PYTHONIOENCODING='utf8'),stdout=log,stderr=log)
                    return process.pid
                with patch('ngs.input_host.input_host_current',return_value=True), patch('ngs.input_host.launch_host',side_effect=launch):
                    client=InputHostClient(self.bridge,self.window)
                for _ in range(80):
                    QTest.qWait(100)
                    if probe.exists() or process.poll() is not None: break
                self.assertIsNone(process.poll(),logpath.read_text(encoding='utf8') + keyboard.status)
                self.assertTrue(probe.exists(),logpath.read_text(encoding='utf8'))
                import json
                result=json.loads(probe.read_text())
                self.assertFalse(result['remote'])
                self.assertIn('leftTouchpadWindow',result['visible']); self.assertIn('rightTouchpadWindow',result['visible'])
                self.assertTrue(keyboard.remoteHost); self.assertFalse(local.isVisible()); self.assertTrue(keyboard.touchpadMode)
                self.assertEqual(keyboard.options['height_percent'],47); self.assertAlmostEqual(keyboard.options['keyboard_opacity'],.71)
                saved=self.bridge.storage.read_settings()
                self.assertEqual(saved['keyboard_options']['views']['touch']['height_percent'],47)
                self.assertEqual(saved.get('tactic_url'),self.original.get('tactic_url'))
                before=copy.deepcopy(self.bridge.settings)
                with patch.object(self.bridge,'save',side_effect=OSError('read only')):
                    self.assertEqual(client.receive(dict(kind='save',settings=dict(keyboard_options={}))), 'read only')
                self.assertEqual(self.bridge.settings,before)
                with self.assertRaises(ValueError): client.receive(dict(kind='save',settings=dict(tactic_url='foreign')))
                client.close()
                for _ in range(30):
                    QTest.qWait(50)
                    if process.poll() is not None: break
                self.assertIsNotNone(process.poll())
                self.assertFalse(keyboard.remoteHost); self.assertFalse(keyboard.visible)
                keyboard.show(); QTest.qWait(300); self.assertTrue(local.isVisible())
        finally:
            if client: client.close(); client.deleteLater()
            if process and process.poll() is None: process.terminate(); process.wait(timeout=5)
            probe.unlink(missing_ok=True)

    def test_print_screen_touch_hold_captures_after_overlays_disappear_without_short_click(self):
        from unittest.mock import Mock
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QPixmap, QColor
        keyboard = self.bridge.keyboard; keys = self.window.findChild(QObject,'virtualKeyboardWindow')
        previous_backend, previous_screen = keyboard.backend, keyboard.screen
        keyboard.backend = Mock()
        screen = Mock(); screen.geometry.return_value = QRect(0,0,1280,800); screen.devicePixelRatio.return_value = 1
        picture = QPixmap(80,40); picture.fill(QColor('cyan'))
        def capture(window):
            self.assertEqual(window,0)
            self.assertFalse(keys.isVisible())
            for name in ('leftTouchpadWindow','rightTouchpadWindow','keyboardSettingsWindow'):
                self.assertFalse(self.window.findChild(QObject,name).isVisible())
            return picture
        screen.grabWindow.side_effect = capture
        try:
            keyboard.set_screen(screen); keyboard.setView('touch'); keyboard.show(); QTest.qWait(300)
            button = self.visual(keys.contentItem(),'keyboardTool-PrintScreen')
            self.assertTrue(self.expression(button,'!background.gradient'))
            self.assertEqual(self.expression(button,'background.border.width'),0)
            point = button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
            device = QTest.createTouchDevice()
            with patch('ngs.virtual_keyboard.QGuiApplication.clipboard') as clipboard:
                QTest.touchEvent(keys,device).press(0,point,keys).commit()
                QTest.qWait(750)
                self.assertFalse(keyboard.visible); screen.grabWindow.assert_not_called()
                QTest.touchEvent(keys,device).release(0,point,keys).commit(); QTest.qWait(400)
                screen.grabWindow.assert_called_once_with(0)
                clipboard.return_value.setPixmap.assert_called_once_with(picture)
                keyboard.backend.send.assert_not_called()
                self.assertFalse(keys.isVisible())
        finally:
            keyboard.hide(); keyboard.backend = previous_backend; keyboard.set_screen(previous_screen)

    def test_layout_editor_changes_geometry_and_layers_then_uses_saved_layout(self):
        keyboard=self.bridge.keyboard; keys=self.window.findChild(QObject,'virtualKeyboardWindow')
        previous=keyboard.backend
        from unittest.mock import Mock
        keyboard.backend=Mock()
        try:
            keyboard.setView('touch'); keyboard.show(); QTest.qWait(300)
            self.expression(keys,'editCurrentLayout()'); QTest.qWait(320)
            editor=self.window.findChild(QObject,'keyboardLayoutEditor')
            self.assertTrue(editor.property('visible'))
            self.assertGreater(len(editor.property('draft').toVariant()['rows']),0)
            editor.findChild(QObject,'layoutBase').setProperty('text','ф')
            editor.findChild(QObject,'layoutShift').setProperty('text','Ф')
            editor.findChild(QObject,'layoutAltGr').setProperty('text','€')
            editor.findChild(QObject,'layoutShiftAltGr').setProperty('text','😀')
            self.expression(editor,'resizeKey(.25)')
            self.expression(editor,'moveKey(1,0)')
            self.assertEqual(editor.property('columnIndex'),1)
            self.assertFalse(keyboard.layouts)
            QMetaObject.invokeMethod(editor.findChild(QObject,'layoutSave'),'clicked'); QTest.qWait(350)
            self.assertFalse(editor.property('visible')); self.assertEqual(len(keyboard.layouts),1)
            saved=keyboard.customLayout; self.assertEqual(saved['rows'][0][1]['states']['altgr'],'€')
            self.assertGreater(saved['rows'][0][1]['width'],1)
            keyboard.show(); QTest.qWait(280)
            self.assertTrue(keys.property('customMode'))
            custom_view=keyboard.options['view']
            self.expression(keys,'pressKey("@symbols",0,0)')
            self.assertFalse(keys.property('customMode'))
            self.expression(keys,'pressKey("@letters",0,0)')
            self.assertTrue(keys.property('customMode')); self.assertEqual(keyboard.options['view'],custom_view)
            keyboard.modifiers=['Shift','AltGr']; keyboard.pressCustom(0,1)
            keyboard.backend.text.assert_called_with('😀')
            keyboard.clearModifiers()
            self.assertTrue(self.window.grabWindow().save(str(self.repo/'reports/keyboard-layout-editor-closed.png')))
            keyboard.editLayout(saved); QTest.qWait(320)
            self.assertTrue(self.window.grabWindow().save(str(self.repo/'reports/keyboard-layout-editor.png')))
            keyboard.action.emit('back')
            self.bridge.action.emit('back'); QTest.qWait(300)
        finally:
            keyboard.backend=previous
            self.window.findChild(QObject,'keyboardLayoutEditor').setProperty('visible',False)

    def test_layout_editor_typing_stays_above_keyboard_and_cancel_keeps_saved_data(self):
        from unittest.mock import Mock
        keyboard=self.bridge.keyboard; previous=keyboard.backend; keyboard.backend=Mock()
        keys=self.window.findChild(QObject,'virtualKeyboardWindow')
        editor=self.window.findChild(QObject,'keyboardLayoutEditor')
        try:
            keyboard.setView('touch'); keyboard.setHeight(40); keyboard.show(); QTest.qWait(300)
            self.expression(keys,'editCurrentLayout()'); QTest.qWait(320)
            field=editor.findChild(QObject,'layoutBase')
            QMetaObject.invokeMethod(field,'forceActiveFocus')
            self.engine.auto_keyboard.qt_focus(field); QTest.qWait(300)
            self.assertTrue(keyboard.visible); self.assertTrue(editor.property('typing'))
            point=field.mapToScene(QPointF())
            self.assertLessEqual(self.window.y()+point.y()+field.height(), keys.y()+1)
            keyboard.hide(); QTest.qWait(300)
            self.engine.auto_keyboard.qt_focus(field)
            self.assertFalse(keyboard.visible)
            self.bridge.action.emit('back'); QTest.qWait(300)
            self.assertFalse(editor.property('visible')); self.assertFalse(keyboard.layouts)
        finally:
            keyboard.backend=previous; editor.setProperty('visible',False)

    def test_pad_preset_preview_cancel_apply_and_explanations_from_controller(self):
        keyboard = self.bridge.keyboard
        keyboard.savePad(0x1000, 'Ctrl+C')
        before = copy.deepcopy(self.bridge.settings['keyboard_pad'])
        editor = self.window.findChild(QObject, 'padKeyboardEditor')
        picker = editor.findChild(QObject, 'padPresetPicker')
        keyboard.editPad(); QTest.qWait(400)
        self.assertTrue(editor.property('visible'))
        editor.setProperty('keyName', 'Space')
        self.assertEqual(editor.findChild(QObject, 'padKeyChoice').property('currentText'), 'Пробел')
        editor.setProperty('keyName', 'C')
        editor.setProperty('selectedField', 11); self.bridge.action.emit('activate'); QTest.qWait(400)
        self.assertTrue(picker.property('visible'))
        self.bridge.action.emit('right')
        self.assertEqual(editor.property('presetIndex'), 1)
        self.assertEqual(self.bridge.settings['keyboard_pad'], before)
        self.bridge.action.emit('back'); QTest.qWait(180)
        self.assertEqual(self.bridge.settings['keyboard_pad'], before)
        self.bridge.action.emit('activate'); QTest.qWait(400)
        self.bridge.action.emit('down'); self.bridge.action.emit('activate'); QTest.qWait(200)
        self.assertFalse(picker.property('visible'))
        self.assertEqual(self.bridge.settings['keyboard_pad']['1'], 'Up')
        self.assertFalse(keyboard.padEnabled)
        for field, fragment in ((0, 'L3/R3'), (1, 'Основная'), (2, 'Ctrl+Shift'), (6, 'только'), (7, 'только'), (8, 'скрытой'), (11, 'WASD')):
            editor.setProperty('selectedField', field)
            self.assertIn(fragment, editor.property('fieldHelp'))
        editor.setProperty('selectedField', 0)
        self.bridge.action.emit('next_control')
        self.assertEqual(editor.property('selectedField'), 1)
        self.bridge.action.emit('previous_control')
        self.assertEqual(editor.property('selectedField'), 0)
        editor.setProperty('selectedField', 11); self.bridge.action.emit('activate'); QTest.qWait(400)
        self.assertTrue(self.window.grabWindow().save(str(self.repo / 'reports/pad-preset-preview.png')))
        self.bridge.action.emit('back'); QTest.qWait(180)
        self.bridge.action.emit('back'); QTest.qWait(180)

    def test_pad_preset_buttons_fit_at_handheld_scales(self):
        editor = self.window.findChild(QObject, 'padKeyboardEditor')
        self.bridge.keyboard.editPad(); QTest.qWait(400)
        for size in ((1280, 720), (1280, 800), (1920, 1080)):
            self.window.resize(*size)
            for scale in (1, 1.2):
                self.bridge.settings['ui_scale'] = scale; self.bridge.settingsChanged.emit()
                editor.setProperty('selectedField', 11); self.bridge.action.emit('activate'); QTest.qWait(400)
                for name in ('applyPadPreset', 'cancelPadPreset'):
                    item = editor.findChild(QObject, name)
                    start, end = item.mapToScene(QPointF()), item.mapToScene(QPointF(item.width(), item.height()))
                    self.assertGreaterEqual(start.x(), 0); self.assertGreaterEqual(start.y(), 0)
                    self.assertLessEqual(end.x(), size[0]); self.assertLessEqual(end.y(), size[1])
                self.bridge.action.emit('back'); QTest.qWait(180)
        self.bridge.action.emit('back'); QTest.qWait(180)
