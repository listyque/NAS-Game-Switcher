import json
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QMetaObject, QPointF
from PySide6.QtTest import QTest

from tests import test_ui


class PlayerUiTests(unittest.TestCase):
    def test_connection_link_requires_confirmation_and_never_saves_trust_by_pasting(self):
        pane = self.window.findChild(QObject, 'playerAccountPane')
        account = self.engine.bridge.players
        saved_settings = account.storage.read_settings()
        pins = set(account.storage.config.glob('server-trust*'))
        link = 'https://other.example/tactic/ngs_launcher/#ngs-key=' + 'ab' * 32
        QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(180)
        try:
            pane.findChild(QObject, 'playerServerAddress').setProperty('text', link)
            with patch.object(account, 'login') as login, patch.object(account, 'loginWithPairing') as paired:
                QMetaObject.invokeMethod(pane, 'submit')
                self.assertEqual(pane.property('selected'), 1)
                self.assertIn('other.example', pane.findChild(QObject, 'playerConnectionPreview').property('text'))
                self.assertEqual(account.storage.read_settings(), saved_settings)
                self.assertEqual(set(account.storage.config.glob('server-trust*')), pins)
                pane.findChild(QObject, 'playerCancelConnectionLink').clicked.emit()
                self.assertEqual(pane.findChild(QObject, 'playerServerAddress').property('text'), link)
                QMetaObject.invokeMethod(pane, 'reviewConnectionLink')
                pane.findChild(QObject, 'playerAcceptConnectionLink').clicked.emit()
                self.assertEqual(pane.findChild(QObject, 'playerServerAddress').property('text'), 'https://other.example/tactic')
                self.assertEqual(pane.findChild(QObject, 'playerPairingCode').property('text'), 'ab' * 32)
                self.assertTrue(pane.findChild(QObject, 'playerPairingCode').property('visible'))
                self.assertEqual(account.storage.read_settings(), saved_settings)
                self.assertEqual(set(account.storage.config.glob('server-trust*')), pins)
                login.assert_not_called(); paired.assert_not_called()
        finally:
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(180)

    def test_registration_mode_validates_fields_and_keeps_result_visible(self):
        pane = self.window.findChild(QObject, 'playerAccountPane')
        account = self.engine.bridge.players
        QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(200)
        try:
            QMetaObject.invokeMethod(pane, 'register')
            self.assertTrue(pane.property('registering'))
            confirm = pane.findChild(QObject, 'playerConfirmPassword')
            self.assertTrue(confirm.property('visible'))
            with patch.object(account, 'login') as login:
                pane.findChild(QObject, 'playerServerAddress').setProperty('text', '')
                QMetaObject.invokeMethod(pane, 'submit')
                self.assertIn('https://', pane.property('formError'))
                pane.findChild(QObject, 'playerServerAddress').setProperty('text', account.origin())
                pane.findChild(QObject, 'playerUsername').setProperty('text', 'a')
                QMetaObject.invokeMethod(pane, 'submit')
                self.assertTrue(pane.property('formError'))
                pane.findChild(QObject, 'playerUsername').setProperty('text', 'valid-player')
                pane.findChild(QObject, 'playerPassword').setProperty('text', '12345678')
                QMetaObject.invokeMethod(pane, 'submit')
                self.assertIn('12', pane.property('formError'))
                pane.findChild(QObject, 'playerPassword').setProperty('text', 'long-test-password')
                confirm.setProperty('text', 'different-password')
                QMetaObject.invokeMethod(pane, 'submit')
                self.assertTrue(pane.property('formError'))
                self.assertEqual(pane.property('selected'), 4 if pane.findChild(QObject, 'playerPairingCode').property('visible') else 3)
                login.assert_not_called()
            status = pane.findChild(QObject, 'playerSyncStatus')
            scroll = pane.findChild(QObject, 'playerAccountScroll')
            for width, height in ((1280, 720), (1280, 800)):
                self.window.resize(width, height); QTest.qWait(50)
                self.window.grabWindow()
                point = status.mapToScene(QPointF(0, 0))
                self.assertGreater(point.y(), scroll.mapToScene(QPointF(0, 0)).y() + scroll.height())
                self.assertLessEqual(point.y() + status.height(), height)
            QMetaObject.invokeMethod(pane, 'register')
            self.assertFalse(pane.property('registering'))
            self.assertFalse(confirm.property('visible'))
        finally:
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(200)

    def test_login_and_registration_submit_and_clear_secrets_only_when_started(self):
        pane = self.window.findChild(QObject, 'playerAccountPane')
        account = self.engine.bridge.players
        for registering in (False, True):
            QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(180)
            if registering: QMetaObject.invokeMethod(pane, 'register')
            pane.findChild(QObject, 'playerUsername').setProperty('text', 'valid-player')
            pane.findChild(QObject, 'playerPassword').setProperty('text', 'long-test-password')
            pane.findChild(QObject, 'playerConfirmPassword').setProperty('text', 'long-test-password')
            try:
                with patch('ngs.players.PlayerWorker.start') as start:
                    QMetaObject.invokeMethod(pane, 'submit')
                    start.assert_called_once()
                    self.assertEqual(account.worker.action, 'register' if registering else 'login')
                    self.assertEqual(account.worker.values['password'], 'long-test-password')
                    self.assertEqual(pane.findChild(QObject, 'playerPassword').property('text'), '')
                    self.assertEqual(pane.findChild(QObject, 'playerConfirmPassword').property('text'), '')
                    self.assertEqual(pane.property('formError'), '')
            finally:
                if account.worker is not None: account.finished()
                QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(180)

    def test_account_http_failure_is_not_reported_as_statistics_sync(self):
        from urllib.error import HTTPError
        from ngs.players import PlayerWorker
        import io
        account = self.engine.bridge.players
        for action in ('login', 'register'):
            failed = []
            worker = PlayerWorker(account, action, dict(username='player', password='long-test-password'))
            worker.failed.connect(lambda message, offline: failed.append(message))
            with patch('ngs.players.connect_server', side_effect=HTTPError(account.origin(), 404, 'missing', {}, io.BytesIO())):
                worker.run()
            self.assertEqual(len(failed), 1)
            self.assertNotIn('Retrying automatically', failed[0])
            self.assertNotIn('синхрон', failed[0].lower())
            self.assertEqual(worker.values, {})

    def test_profile_spinner_stops_when_closed_during_background_sync(self):
        account = self.engine.bridge.players
        pane = self.window.findChild(QObject, 'playerAccountPane')
        QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(220)
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
            with patch.object(bridge, 'refreshCatalog'), patch.object(bridge, 'pumpDownloads'), patch('ngs.players.PlayerTransport.call', remote), patch('ngs.players.connect_server', return_value=self.storage.config/'fixture-pin'):
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
            with patch('ngs.players.PlayerTransport.call', remote), patch('ngs.players.connect_server', return_value=self.storage.config/'fixture-pin'):
                QMetaObject.invokeMethod(pane,'open'); QTest.qWait(250)
                self.assertTrue(window.property('modalVisible'))
                pane.findChild(QObject,'playerUsername').setProperty('text','player')
                pane.findChild(QObject,'playerPassword').setProperty('text','test-password-long')
                submit_index = 4 if pane.findChild(QObject,'playerPairingCode').property('visible') else 3
                for index in range(1, submit_index + 1):
                    bridge.action.emit('down'); self.assertEqual(pane.property('selected'),index)
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
