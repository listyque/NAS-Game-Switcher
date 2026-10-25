import os
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QT_QUICK_BACKEND']='software'
import unittest
from pathlib import Path
from PySide6.QtCore import QObject,Property,Signal,QUrl,QMetaObject,QEvent,qInstallMessageHandler
from PySide6.QtGui import QGuiApplication,QFont,QFontDatabase
from PySide6.QtQml import QQmlComponent,QQmlEngine,QQmlExpression
from PySide6.QtTest import QTest
from ngs.appearance import colors

class Theme(QObject):
    action=Signal(str)
    @Property('QVariantMap',constant=True)
    def themeColors(self):return colors({})

class UpdateActionTests(unittest.TestCase):
    qml_directory=Path(__file__).resolve().parents[1]/'qml'
    @classmethod
    def setUpClass(cls):
        cls.app=QGuiApplication.instance() or QGuiApplication([])
        QFontDatabase.addApplicationFont(str(Path(__file__).resolve().parents[1]/'assets/fonts/Manrope.ttf'))
        cls.app.setFont(QFont('Manrope'))
    def setUp(self):
        self.messages=[];self.old_handler=qInstallMessageHandler(lambda kind,context,text:self.messages.append(text))
        self.engine=QQmlEngine();self.theme=Theme();self.engine.rootContext().setContextProperty('uiTheme',self.theme)
        self.component=component=QQmlComponent(self.engine)
        component.setData(b'''import QtQuick
import QtQuick.Window
Window {
    width: 800; height: 500; visible: true
    QtObject {
        id: fake; objectName: "fakeBridge"
        property var inputHints: ({activate:"A", back:"B", refresh:"Y"})
        property var updatesState: ({available:false, busy:false, can_install:false, current:"1", version:"", server:"test", notes:"", status:"", progress:0})
        property int checks: 0
        property int installs: 0
        property int cancels: 0
        function checkUpdates() { checks++ }
        function installUpdate() { installs++ }
        function cancelUpdate() { cancels++ }
    }
    UpdatesPane { objectName: "updatesPane"; bridge: fake; uiScale: 1 }
}''',QUrl.fromLocalFile(str(self.qml_directory/'UpdateActionHarness.qml')))
        self.window=component.create();self.assertIsNotNone(self.window,'\n'.join(e.toString() for e in component.errors()))
        self.fake=self.window.findChild(QObject,'fakeBridge');self.pane=self.window.findChild(QObject,'updatesPane')
        self.button=self.window.findChild(QObject,'installUpdateButton');self.back=self.window.findChild(QObject,'updatesBackButton')
        QMetaObject.invokeMethod(self.pane,'open');QTest.qWait(200)
    def tearDown(self):
        self.window.setProperty('visible',False);self.window.deleteLater();self.app.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.engine.deleteLater();self.app.sendPostedEvents(None,QEvent.Type.DeferredDelete);self.app.processEvents()
        qInstallMessageHandler(self.old_handler)
        self.assertFalse([s for s in self.messages if any(t in s for t in ('ReferenceError','TypeError','Binding loop','Cannot assign'))])
    def evaluate(self,code):
        e=QQmlExpression(QQmlEngine.contextForObject(self.pane),self.pane,code);result=e.evaluate()[0]
        self.assertFalse(e.hasError(),e.error().toString());return result
    def state(self,**changes):
        value=self.fake.property('updatesState')
        if hasattr(value,'toVariant'):value=value.toVariant()
        value.update(changes);self.fake.setProperty('updatesState',value);QTest.qWait(10)
    def test_no_update_click_and_gamepad_check_instead_of_install(self):
        self.assertEqual(self.pane.property('selectedField'),1)
        self.assertEqual(self.button.property('text'),'Проверить обновления · A')
        self.assertTrue(self.button.property('enabled'))
        before=self.fake.property('checks');QMetaObject.invokeMethod(self.button,'clicked');self.evaluate('handleAction("activate")')
        self.assertEqual(self.fake.property('checks'),before+2);self.assertEqual(self.fake.property('installs'),0)
        self.assertFalse(self.window.findChild(QObject,'updateActionIcon').property('download'))
    def test_available_update_installs_and_busy_or_blocked_state_cannot_run_it(self):
        self.state(available=True,can_install=True)
        self.assertEqual(self.button.property('text'),'Скачать и обновить · A')
        self.assertTrue(self.window.findChild(QObject,'updateActionIcon').property('download'))
        QMetaObject.invokeMethod(self.button,'clicked');self.assertEqual(self.fake.property('installs'),1)
        self.state(can_install=False);self.assertFalse(self.button.property('enabled'))
        self.evaluate('handleAction("activate")');self.assertEqual(self.fake.property('installs'),1)
        self.state(available=False,busy=True);self.assertFalse(self.button.property('enabled'))
        before=self.fake.property('checks');self.evaluate('handleAction("activate")');self.assertEqual(self.fake.property('checks'),before)
        self.assertEqual(self.back.property('text'),'Отменить · B')
        self.pane.setProperty('selectedField',2);self.evaluate('handleAction("activate")');self.assertEqual(self.fake.property('cancels'),1)
    def test_keyboard_hints_icons_and_navigation_fit_short_screen(self):
        self.fake.setProperty('inputHints',dict(activate='Enter',back='Esc',refresh='F5'))
        self.assertIn('Enter',self.button.property('text'));self.assertIn('Esc',self.back.property('text'))
        self.assertIsNotNone(self.window.findChild(QObject,'updatesBackIcon'))
        self.evaluate('handleAction("right")');self.assertEqual(self.pane.property('selectedField'),2)
        self.evaluate('handleAction("left")');self.assertEqual(self.pane.property('selectedField'),1)
        for available in (False,True):
            self.state(available=available,can_install=available)
            for button in (self.button,self.back):
                self.assertGreaterEqual(button.property('height'),48)
                self.assertLessEqual(button.property('x')+button.property('width'),720)
