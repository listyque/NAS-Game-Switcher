import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,QMetaObject
from PySide6.QtQml import QQmlEngine,QQmlExpression
from PySide6.QtTest import QTest
from tests import test_ui as fixture

class SettingsDepthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixture.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):fixture.UiTests.tearDownClass.__func__(cls)
    def run_qml(self,item,code):
        expression=QQmlExpression(QQmlEngine.contextForObject(item),item,code);expression.evaluate()
        self.assertFalse(expression.hasError(),expression.error().toString())
    def test_both_catalogizers_open_the_correct_mode_and_return_to_the_same_row(self):
        bridge=self.engine.bridge;pane=self.window.findChild(QObject,'settingsPane')
        child=self.window.findChild(QObject,'catalogizerPane')
        with patch.object(bridge.remoteCatalogizer,'request') as games, patch.object(bridge.installerCatalogizer,'request') as installers:
            bridge.action.emit('settings');QTest.qWait(220)
            self.run_qml(pane,'enterSection(5)');QTest.qWait(400)
            for clicked in (False,True):
                for field,name,expected,kind in ((9,'openCatalogizerButton',bridge.remoteCatalogizer,'catalogizer'),
                        (18,'openInstallerCatalogizerButton',bridge.installerCatalogizer,'installerCatalogizer')):
                    games.reset_mock();installers.reset_mock()
                    pane.setProperty('selectedField',field)
                    if clicked:QMetaObject.invokeMethod(pane.findChild(QObject,name),'clicked')
                    else:bridge.action.emit('activate')
                    QTest.qWait(400)
                    self.assertTrue(child.property('visible'))
                    self.assertEqual(child.property('bridge'),expected)
                    self.assertEqual(pane.property('childPage'),kind)
                    self.assertTrue(child.property('navigationPath'))
                    (installers if field==9 else games).assert_not_called()
                    self.assertEqual(expected.url,bridge.settingsState['tactic_url'])
                    self.assertIsNotNone(expected.pending)
                    bridge.action.emit('back');QTest.qWait(400)
                    self.assertFalse(child.property('visible'))
                    self.assertEqual(pane.property('childPage'),'')
                    self.assertEqual(pane.property('page'),5)
                    self.assertEqual(pane.property('selectedField'),field)
            QMetaObject.invokeMethod(pane,'close');QTest.qWait(220)
    def test_one_back_collapses_each_section_including_focused_text(self):
        bridge=self.engine.bridge;pane=self.window.findChild(QObject,'settingsPane')
        bridge.action.emit('settings');QTest.qWait(220)
        for index in range(6):
            self.run_qml(pane,'enterSection('+str(index)+')');QTest.qWait(400)
            if index==0:pane.findChild(QObject,'settingsServer').forceActiveFocus()
            bridge.action.emit('back')
            self.assertEqual(pane.property('page'),-1);self.assertTrue(pane.property('visible'))
            self.assertEqual(pane.property('focusSide'),0);QTest.qWait(400)
        bridge.action.emit('back');QTest.qWait(220);self.assertFalse(pane.property('visible'))
    def test_children_return_to_the_origin_and_pad_editor_keeps_bindings(self):
        bridge=self.engine.bridge;window=self.window;pane=window.findChild(QObject,'settingsPane')
        catalog=bridge.remoteCatalogizer
        with patch('ngs.app.check_update',side_effect=ConnectionError('Fixture offline')),patch.object(catalog,'request'):
            bridge.action.emit('settings');QTest.qWait(220)
            for kind,index,field,name in [('bindings',3,10,'bindingsPane'),('updates',4,8,'updatesPane'),
                    ('catalogizer',5,9,'catalogizerPane'),('account',0,13,'playerAccountPane')]:
                self.run_qml(pane,'enterSection('+str(index)+')');QTest.qWait(400)
                pane.setProperty('selectedField',field);bridge.action.emit('activate');QTest.qWait(450)
                child=window.findChild(QObject,name)
                self.assertTrue(pane.property('visible'));self.assertEqual(pane.property('childPage'),kind)
                self.assertTrue(child.property('visible'));self.assertTrue(child.property('navigationPath'))
                self.assertEqual(pane.property('contentItem').property('opacity'),0)
                if kind=='bindings':
                    child.setProperty('selectedField',12);bridge.action.emit('activate');QTest.qWait(450)
                    pad=window.findChild(QObject,'padKeyboardEditor')
                    self.assertTrue(pad.property('visible'));self.assertTrue(child.property('visible'))
                    self.assertEqual(child.property('opacity'),0)
                    bridge.action.emit('back');QTest.qWait(450)
                    self.assertFalse(pad.property('visible'));self.assertTrue(child.property('visible'))
                    self.assertEqual(child.property('selectedField'),12);self.assertEqual(child.property('page'),1)
                bridge.action.emit('back');QTest.qWait(450)
                self.assertFalse(child.property('visible'));self.assertTrue(pane.property('visible'))
                self.assertEqual(pane.property('childPage'),'');self.assertEqual(pane.property('page'),index)
                self.assertEqual(pane.property('selectedField'),field)
                self.assertEqual(pane.property('contentItem').property('opacity'),1)
                bridge.action.emit('back');QTest.qWait(400)
                self.assertEqual(pane.property('page'),-1)
            bridge.action.emit('back');QTest.qWait(220)
            standalone=window.findChild(QObject,'bindingsPane')
            QMetaObject.invokeMethod(standalone,'open');QTest.qWait(220)
            bridge.action.emit('back');QTest.qWait(250)
            self.assertFalse(pane.property('visible'));self.assertFalse(standalone.property('visible'))
        errors=[m for m in self.messages if any(term in m for term in ('ReferenceError','TypeError','Binding loop','Cannot assign'))]
        self.assertEqual(errors,[])
    def test_back_during_update_check_cancels_and_returns_in_one_press(self):
        import threading
        entered=threading.Event();cancelled=threading.Event()
        def check(storage,url,cancel):
            entered.set()
            if not cancel.wait(5):raise RuntimeError('Fixture timed out')
            cancelled.set();raise InterruptedError('Fixture cancelled')
        bridge=self.engine.bridge;pane=self.window.findChild(QObject,'settingsPane')
        with patch('ngs.app.check_update',side_effect=check):
            bridge.action.emit('settings');QTest.qWait(220)
            self.run_qml(pane,'enterSection(4)');QTest.qWait(400)
            pane.setProperty('selectedField',8);bridge.action.emit('activate')
            self.assertTrue(entered.wait(1))
            bridge.action.emit('back');QTest.qWait(450)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.window.findChild(QObject,'updatesPane').property('visible'))
            self.assertTrue(pane.property('visible'));self.assertEqual(pane.property('page'),4)
            self.assertEqual(pane.property('selectedField'),8)
            self.assertIsNone(bridge.update_worker)
        QMetaObject.invokeMethod(pane,'close');QTest.qWait(220)
