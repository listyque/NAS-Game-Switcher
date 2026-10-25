import copy
import unittest
from PySide6.QtCore import QObject,QMetaObject,QPointF
from PySide6.QtTest import QTest
from tests import test_ui


class EdgeSettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def test_gamepad_settings_are_independent_and_explain_windows_swipes(self):
        bridge=self.engine.bridge;keyboard=bridge.keyboard;original=copy.deepcopy(bridge.settings)
        pane=self.window.findChild(QObject,'settingsPane')
        try:
            QMetaObject.invokeMethod(pane,'open');QTest.qWait(240)
            master=pane.findChild(QObject,'settingsOverlayEnabled')
            edge=pane.findChild(QObject,'settingsEdgeSwipeEnabled')
            auto=pane.findChild(QObject,'settingsEdgeAutoHide')
            warning=pane.findChild(QObject,'settingsEdgeSwipeWarning')
            self.assertIn('«Пуска»',warning.property('text'))
            self.assertIn('полноэкранное',warning.property('text'))
            for index,key,control in ((41,'edge_auto_hide',auto),(40,'edge_swipe_enabled',edge)):
                pane.setProperty('selectedField',index);bridge.action.emit('activate');QTest.qWait(40)
                self.assertFalse(keyboard.options[key] or control.property('checked'))
            self.assertTrue(keyboard.options['overlay_enabled'])
            self.assertFalse(auto.property('enabled'))
            pane.setProperty('selectedField',20);bridge.action.emit('activate');QTest.qWait(40)
            self.assertFalse(master.property('checked') or edge.property('enabled'))
            self.assertTrue(keyboard.enabled)
            QMetaObject.invokeMethod(pane,'close');QTest.qWait(200)
            QMetaObject.invokeMethod(pane,'open');QTest.qWait(200)
            self.assertFalse(edge.property('checked') or auto.property('checked') or master.property('checked'))
            pane.setProperty('selectedField',20);bridge.action.emit('activate')
            self.assertTrue(edge.property('enabled'))
            self.assertFalse(edge.property('checked'))
            for size in ((800,500),(1280,800),(1920,1200)):
                self.window.resize(*size);pane.setProperty('selectedField',40);QTest.qWait(120)
                self.assertGreater(warning.width(),200)
                self.assertGreater(warning.implicitHeight(),30)
                QMetaObject.invokeMethod(pane,'revealSelected');QTest.qWait(30)
                body=pane.findChild(QObject,'settingsDetails')
                position=warning.mapToItem(body,QPointF(0,0))
                self.assertGreaterEqual(position.y(),-1)
                self.assertLessEqual(position.y()+warning.height(),body.height()+1)
            self.window.resize(1280,800);QTest.qWait(200)
            QMetaObject.invokeMethod(pane,'revealSelected');QTest.qWait(100)
            self.assertTrue(self.window.grabWindow().save(str(self.repo/'reports/edge-options-20261025/settings.png')))
            self.assertFalse(any('ReferenceError' in m or 'TypeError' in m or 'Binding loop' in m for m in self.messages),self.messages)
        finally:
            QMetaObject.invokeMethod(pane,'close')
            bridge.settings=original;bridge.save();bridge.settingsChanged.emit();keyboard.changed.emit()
