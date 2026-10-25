import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui


class OnboardingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def test_server_address_and_registration_unlock_client_without_admin_pairing(self):
        bridge,window=self.engine.bridge,self.window
        account=bridge.players;pane=window.findChild(QObject,'playerAccountPane')
        previous=dict(bridge.settings),dict(account.account),bridge._demo
        seen=[]
        def remote(transport,value):
            seen.append(dict(value))
            if value['action']=='register':return dict(profile='a'*32,username='new-player',token='b'*64,expires=2000000000)
            if value['action']=='authorize':return dict(device='c'*32,secret='d'*64,expires=2000000000)
            return dict(acked=[],totals=[],more=False)
        with patch.object(bridge,'refreshCatalog'),patch.object(bridge,'refreshInstallers'),patch.object(bridge,'pumpDownloads'),patch('ngs.players.PlayerTransport.call',remote),patch('ngs.players.connect_server',return_value=None):
            try:
                account.account={};account.save();bridge._demo=False;account.changed.emit()
                QMetaObject.invokeMethod(pane,'open');QTest.qWait(220)
                server=pane.findChild(QObject,'playerServerAddress');register=pane.findChild(QObject,'playerRegister')
                self.assertTrue(register.property('visible'))
                for size in ((800,500),(1280,800),(1920,1200)):
                    window.resize(*size);QTest.qWait(35)
                    self.assertGreater(server.width(),200);self.assertGreaterEqual(register.height(),54)
                server.setProperty('text','http://127.0.0.1:9')
                pane.findChild(QObject,'playerUsername').setProperty('text','new-player')
                password=pane.findChild(QObject,'playerPassword');password.setProperty('text','a-long-private-password')
                self.assertTrue(QMetaObject.invokeMethod(pane,'register'))
                deadline=time.monotonic()+8
                while account.worker is not None and time.monotonic()<deadline:
                    QTest.qWait(10);time.sleep(.001)
                self.assertIsNone(account.worker)
                self.assertTrue(account.state['logged_in'],account.state)
                self.assertFalse(bridge.loginRequired)
                self.assertEqual(bridge.storage.read_settings()['tactic_url'],'http://127.0.0.1:9/tactic')
                self.assertEqual(seen[0]['action'],'register')
                self.assertEqual(password.property('text'),'')
                self.assertFalse((bridge.storage.config/'api-device.json').exists())
            finally:
                account.account=previous[1];account.save();bridge._demo=previous[2]
                bridge.settings=previous[0];bridge.save();bridge.settingsChanged.emit();account.changed.emit()
                QMetaObject.invokeMethod(pane,'close');QTest.qWait(200)
