import json
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QMetaObject
from PySide6.QtTest import QTest

from tests import test_ui


class PlayerUiTests(unittest.TestCase):
    def test_profile_spinner_stops_when_closed_during_background_sync(self):
        account = self.engine.bridge.players
        pane = self.window.findChild(QObject, 'playerAccountPane')
        spinner = pane.findChild(QObject, 'playerSyncActivity')
        loader = spinner.findChild(QObject, 'activityLoader')
        previous = account.worker
        try:
            account.worker = object()
            account.changed.emit()
            self.assertFalse(spinner.property('running'))
            self.assertIsNone(loader.property('item'))
            QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(220)
            self.assertTrue(spinner.property('running'))
            self.assertIsNotNone(loader.property('item'))
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(220)
            self.assertTrue(account.state['busy'])
            self.assertFalse(spinner.property('running'))
            self.assertIsNone(loader.property('item'))
            QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(220)
            self.assertTrue(spinner.property('running'))
            account.worker = None; account.changed.emit()
            self.assertFalse(spinner.property('running'))
            self.assertIsNone(loader.property('item'))
        finally:
            account.worker = previous; account.changed.emit()
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(220)

    def test_settings_login_unlocks_launcher_without_key_file_and_logout_locks_it(self):
        from ngs.api import MetadataApi
        bridge, window = self.engine.bridge, self.window
        account = bridge.players
        pane = window.findChild(QObject, 'settingsPane')
        previous = bridge._demo, dict(account.account)
        def remote(transport, value):
            if value['action'] == 'login': return dict(profile='a'*32, username='player', token='b'*64, expires=2000000000)
            if value['action'] == 'authorize': return dict(device='c'*32, secret='d'*64, expires=2000000000)
            if value['action'] == 'logout': return {}
            return dict(acked=[], totals=[], more=False)
        try:
            with patch.object(bridge, 'refreshCatalog'), patch.object(bridge, 'pumpDownloads'), patch('ngs.players.PlayerTransport.call', remote):
                bridge._demo = False; account.account = {}; account.save(); account.changed.emit()
                self.assertTrue(bridge.loginRequired)
                bridge.start_operation(dict(id='test', name='Test'), 'launch')
                self.assertIsNone(bridge.game_worker)
                bridge.action.emit('activate'); QTest.qWait(220)
                profile = window.findChild(QObject, 'playerAccountPane')
                self.assertTrue(profile.property('visible'))
                profile.findChild(QObject, 'playerUsername').setProperty('text', 'player')
                password = profile.findChild(QObject, 'playerPassword'); password.setProperty('text', 'test-password-long')
                QMetaObject.invokeMethod(profile, 'submit'); self.wait_account()
                self.assertEqual(password.property('text'), '')
                self.assertFalse(bridge.loginRequired)
                self.assertEqual(MetadataApi(bridge.storage, bridge.settings['tactic_url']).device, 'c'*32)
                self.assertFalse((bridge.storage.config/'api-device.json').exists())
                account.logout(); self.wait_account()
                self.assertTrue(bridge.loginRequired)
        finally:
            bridge._demo, account.account = previous; account.save(); account.changed.emit()
            QMetaObject.invokeMethod(window.findChild(QObject, 'playerAccountPane'), 'close')
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(220)

    def test_offline_profile_and_cached_catalog_keep_navigation_and_retry_in_background(self):
        import time
        bridge, window = self.engine.bridge, self.window
        account = bridge.players
        previous_account = dict(account.account)
        previous = bridge._catalog_online, bridge._catalog_refresh_at, bridge._installer_refresh_at
        pane = window.findChild(QObject, 'playerAccountPane')
        try:
            account.account = dict(server=account.origin(), profile='a'*32, username='player', token='b'*64, expires=2000000000, api=dict(device='c'*32,secret='d'*64,expires=2000000000))
            account.save(); account.changed.emit()
            QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(200)
            pane.setProperty('selected', 2)
            with patch('ngs.players.PlayerTransport.call', side_effect=ConnectionError('Offline')):
                account.sync(); self.wait_account()
            self.assertTrue(account.state['logged_in'])
            self.assertTrue(account.state['offline'])
            self.assertEqual(pane.property('selected'), 2)
            self.assertIn('Ждут отправки:', pane.findChild(QObject, 'playerSyncStatus').property('text'))
            games = bridge.games
            bridge.catalog_failed()
            self.assertEqual(bridge.games, games)
            bridge._catalog_refresh_at = 0
            with patch.object(bridge, 'refreshCatalog') as refresh, patch.object(bridge, 'pumpDownloads') as pump:
                bridge.retryOffline(); refresh.assert_called_once(); pump.assert_called_once()
            bridge.catalog_loaded(bridge._games)
            self.assertTrue(bridge._catalog_online)
            bridge._installer_refresh_at = time.monotonic() + 300
            with patch.object(bridge, 'refreshCatalog') as refresh, patch.object(bridge, 'refreshInstallers') as installers:
                bridge.retryOffline(); refresh.assert_not_called(); installers.assert_not_called()
            response = dict(acked=[], totals=[], more=False)
            with patch('ngs.players.PlayerTransport.call', return_value=response):
                account.sync(); self.wait_account()
            self.assertFalse(account.state['offline'])
        finally:
            account.account = previous_account; account.save(); account.changed.emit()
            bridge._catalog_online, bridge._catalog_refresh_at, bridge._installer_refresh_at = previous
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(200)

    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def wait_account(self):
        for unused in range(200):
            QTest.qWait(20)
            if self.engine.bridge.players.worker is None: return
        self.fail('Account worker did not finish')

    def test_gamepad_login_secure_portable_token_and_geometry(self):
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject,'playerAccountPane')
        account = bridge.players
        origin = account.origin()
        def remote(self, value):
            if value['action'] == 'login':
                self_test.assertEqual(value['password'],'test-password-long')
                return dict(profile='a'*32,username='player',token='b'*64,expires=2000000000)
            if value['action'] == 'authorize': return dict(device='c'*32,secret='d'*64,expires=2000000000)
            if value['action'] == 'logout': return {}
            return dict(acked=[r['id'] for r in value.get('rows',[])],totals=[],more=False)
        self_test = self
        try:
            with patch('ngs.players.PlayerTransport.call', remote):
                QMetaObject.invokeMethod(pane,'open'); QTest.qWait(250)
                self.assertTrue(window.property('modalVisible'))
                pane.findChild(QObject,'playerUsername').setProperty('text','player')
                pane.findChild(QObject,'playerPassword').setProperty('text','test-password-long')
                bridge.action.emit('down'); self.assertEqual(pane.property('selected'),1)
                bridge.action.emit('down'); self.assertEqual(pane.property('selected'),2)
                bridge.action.emit('down'); self.assertEqual(pane.property('selected'),3)
                bridge.action.emit('activate')
                self.assertEqual(pane.findChild(QObject,'playerPassword').property('text'),'')
                self.wait_account()
                self.assertTrue(account.state['logged_in'])
                saved = account.path.read_text(encoding='utf8')
                self.assertNotIn('test-password-long',saved)
                self.assertEqual(json.loads(saved)['account']['server'],origin)
                self.assertEqual(json.loads(saved)['account']['token'],'b'*64)
                for scale in (1.0,1.2):
                    previous = pane.property('uiScale'); pane.setProperty('uiScale',scale)
                    QTest.qWait(50)
                    history = pane.findChild(QObject,'playerImportHistory')
                    self.assertGreater(history.property('width'),200)
                    self.assertLess(history.property('y')+history.property('height'),pane.property('height'))
                    pane.setProperty('uiScale',previous)
                account.logout(); self.wait_account()
                self.assertFalse(account.state['logged_in'])
                account.account = dict(server=origin,profile='a'*32,username='player',token='b'*64,expires=2000000000, api=dict(device='c'*32,secret='d'*64,expires=2000000000))
                account.save();account.changed.emit()
                with patch('ngs.players.PlayerTransport.call',side_effect=RuntimeError('expired or offline')):
                    account.logout();self.wait_account()
                self.assertFalse(account.state['logged_in'])
                self.assertEqual(json.loads(account.path.read_text())['account'],{})
                bridge.action.emit('back'); QTest.qWait(250)
                self.assertFalse(pane.property('visible'))
        finally:
            account.account = {}; account.save()
            QMetaObject.invokeMethod(pane,'close'); QTest.qWait(250)
