import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject, QPointF, QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui_controls as controls_ui


class BrightnessUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): controls_ui.ControlUiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): controls_ui.ControlUiTests.tearDownClass.__func__(cls)
    setUp = controls_ui.ControlUiTests.setUp
    tearDown = controls_ui.ControlUiTests.tearDown
    expression = controls_ui.ControlUiTests.expression
    visual = controls_ui.ControlUiTests.visual

    def control(self, name):
        return self.window.findChild(QObject, name) or self.visual(self.window.contentItem(), name)

    def settle(self):
        for unused in range(300):
            QTest.qWait(10)
            if not self.bridge.system_controls.workers: return
        self.fail('Control worker did not finish')

    def test_failed_write_keeps_measured_percent_and_next_write_works_without_reopening(self):
        panel = self.window.findChild(QObject, 'systemPanel')
        state = dict(brightness=60)
        def operate(key, action, value=None, password=''):
            if action == 'set':
                if value == 80:
                    state['brightness'] = 75
                    raise OSError('WmiSetBrightness: 5')
                state['brightness'] = value
            return dict(state)
        with patch('ngs.system_controls.operate', side_effect=operate):
            self.expression(panel, 'showPanel("brightness")'); self.settle()
            QMetaObject.invokeMethod(self.control('brightnessPreset-80'), 'clicked'); self.settle()
            self.assertEqual(panel.findChild(QObject, 'systemLevelPercent').property('text'), '75%')
            self.assertTrue(panel.findChild(QObject, 'systemLevel').property('enabled'))
            self.assertIn('WmiSetBrightness', self.bridge.system_controls.panel['error'])
            QMetaObject.invokeMethod(self.control('brightnessPlus'), 'clicked'); self.settle()
            self.assertEqual(panel.findChild(QObject, 'systemLevelPercent').property('text'), '85%')
            self.assertEqual(self.bridge.system_controls.panel['error'], '')
            self.assertTrue(panel.property('visible'))

    def test_compact_presets_steps_and_controller_targets_fit(self):
        panel = self.window.findChild(QObject, 'systemPanel'); state = dict(brightness=50)
        def operate(key, action, value=None, password=''):
            if action == 'set': state['brightness'] = value
            return dict(state)
        with patch('ngs.system_controls.operate', side_effect=operate):
            self.expression(panel, 'showPanel("brightness")'); self.settle()
            for name, expected in [('brightnessPreset-20',20),('brightnessPreset-40',40),('brightnessPreset-60',60),('brightnessPreset-80',80),('brightnessPlus',90),('brightnessPlus',100),('brightnessPlus',100),('brightnessMinus',90)]:
                QMetaObject.invokeMethod(self.control(name), 'clicked'); self.settle()
                self.assertEqual(state['brightness'], expected)
            panel.setProperty('selectedField', 0); visited = set()
            for unused in range(8):
                visited.add(panel.property('selectedField')); self.bridge.action.emit('next_control')
            self.assertEqual(visited, {-10,0,-11,20,21,22,23,2})
            for width, height in ((800,500),(1280,720)):
                self.window.resize(width,height)
                for scale in (1,1.2):
                    self.bridge.settings['ui_scale']=scale; self.bridge.settingsChanged.emit(); QTest.qWait(100)
                    self.assertLessEqual(panel.property('height'), 310*scale)
                    for name in ('brightnessMinus','brightnessPlus','brightnessPreset-20','brightnessPreset-40','brightnessPreset-60','brightnessPreset-80'):
                        button=self.control(name)
                        point=button.mapToScene(QPointF(button.width(),button.height()))
                        self.assertLessEqual(point.x(), self.window.width()+1); self.assertLessEqual(point.y(),self.window.height()+1)
                        self.assertGreaterEqual(button.height(),48)
            self.assertTrue(self.window.grabWindow().save(str(self.repo/'reports/brightness-readback-ui.png')))

    def test_radio_actions_are_separate_icons_and_scrolling_clears_password_focus(self):
        panel = self.window.findChild(QObject,'systemPanel')
        rows=[dict(id='paired',name='Controller',paired=True,connected=False),dict(id='new',name='Headphones',paired=False,connected=False)]
        with patch('ngs.system_controls.operate',return_value=dict(items=rows,radio_state='on')) as operate:
            self.expression(panel,'showPanel("bluetooth")'); self.settle(); QTest.qWait(150)
            self.assertEqual(self.control('systemDeviceAction-0').property('text'),'×')
            self.assertEqual(self.control('systemDeviceAction-1').property('text'),'+')
            QMetaObject.invokeMethod(self.control('systemDeviceAction-0'),'clicked')
            self.assertEqual(panel.property('unpairId'),'paired'); self.assertNotEqual(operate.call_args.args[1],'unpair')
            panel.setProperty('unpairId','')
            QMetaObject.invokeMethod(self.control('systemDeviceAction-1'),'clicked'); self.settle()
            self.assertEqual(operate.call_args.args[:3],('bluetooth','pair','new'))
        rows=[dict(id='wifi',name='Network',connected=False,saved=True,secure=True,quality=85)]
        with patch('ngs.system_controls.operate',return_value=dict(items=rows,radio_state='on')) as operate:
            self.expression(panel,'showPanel("wifi")'); self.settle(); QTest.qWait(150)
            QMetaObject.invokeMethod(self.control('systemDeviceAction-0'),'clicked'); self.settle()
            self.assertEqual(operate.call_args.args[:3],('wifi','connect','wifi'))
            panel.setProperty('networkId','wifi')
            password=panel.findChild(QObject,'wifiPassword'); QMetaObject.invokeMethod(password,'forceActiveFocus')
            QMetaObject.invokeMethod(panel.findChild(QObject,'systemDevices'),'movementStarted')
            self.assertFalse(password.property('activeFocus')); self.assertFalse(self.bridge.keyboard.visible)
            self.assertGreater(panel.findChild(QObject,'systemDevices').property('pressDelay'),0)

    def test_redundant_refresh_buttons_are_absent(self):
        for name in ('checkUpdatesButton','refreshInstallSpace'):
            self.assertIsNone(self.window.findChild(QObject,name))
        status=self.window.findChild(QObject,'catalogRefresh')
        self.assertIn('Text',status.metaObject().className())
        self.assertNotIn('Обновить',status.property('text'))
