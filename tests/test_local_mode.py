import copy
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QObject, QMetaObject, QPointF, Qt
from PySide6.QtTest import QTest

from ngs.app import LauncherBridge, GameWorker
from ngs.install import LocalLibrary
from ngs.play_time import PlayTime
from ngs.portable import PortableStorage
from tests import test_ui


class LocalModeTests(unittest.TestCase):
    ui_local_mode = True
    ui_server = ''

    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.bridge = self.engine.bridge
        self.original = copy.deepcopy(self.bridge.settings)
        self.account = copy.deepcopy(self.bridge.players.account)
        self.bridge._demo = False

    def tearDown(self):
        for name in ('playerAccountPane', 'settingsPane', 'localScanner'):
            self.window.findChild(QObject, name).close()
        self.bridge._demo = True
        self.bridge.settings = self.original
        self.bridge.players.account = self.account
        self.storage.write_settings(self.original)
        self.bridge.settingsChanged.emit(); self.bridge.players.changed.emit()
        QTest.qWait(180)

    def test_clean_profile_initializes_sqlite_without_authentication_or_server_calls(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            storage = PortableStorage(folder, resources=self.repo)
            defaults = storage.read_settings()
            self.assertEqual(defaults['tactic_url'], '')
            self.assertTrue(defaults['local_mode'])
            defaults['auto_check_updates'] = False
            storage.write_settings(defaults)
            with patch('ngs.app.CatalogWorker.start') as catalog, \
                    patch('ngs.players.PlayerWorker.start') as player, \
                    patch('ngs.players.BackupWorker.start') as backup, \
                    patch('ngs.api.MetadataApi.call') as metadata:
                bridge = LauncherBridge(storage)
                try:
                    QTest.qWait(120)
                    self.assertTrue(bridge.localMode)
                    self.assertFalse(bridge.loginRequired)
                    self.assertFalse(bridge.players.authorized())
                    self.assertEqual(bridge.initialUi['section'], 0)
                    self.assertFalse(bridge.offline_timer.isActive())
                    bridge.retryOffline(); bridge.refreshCatalog(); bridge.refreshInstallers()
                    bridge.players.background_sync(); bridge.players.autoBackup(); bridge.schedule_backup()
                    self.assertTrue(bridge.useLocalLibrary(), bridge.settingsStatus)
                    self.assertFalse(bridge.queueDownload('unknown', 'games'))
                    for request in (catalog, player, backup, metadata): request.assert_not_called()
                    with closing(sqlite3.connect(bridge.library.path)) as db:
                        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertTrue({'installed', 'launch_options', 'play_history', 'play_sessions'} <= tables)
                finally:
                    bridge.stop()
                    bridge.download_queue.close()

    def test_fresh_local_ui_has_no_login_gate_and_opens_the_local_scanner(self):
        self.assertTrue(self.bridge.localMode)
        self.assertFalse(self.bridge.loginRequired)
        self.assertEqual(self.window.property('section'), 0)
        self.assertFalse(self.window.findChild(QObject, 'loginAccountButton').isVisible())
        with patch.object(self.bridge, 'start_operation') as operation:
            self.bridge.action.emit('toggle_installed'); QTest.qWait(220)
            scanner = self.window.findChild(QObject, 'localScanner')
            self.assertTrue(scanner.property('visible'))
            scanner.close(); QTest.qWait(180)
            self.window.setProperty('focusZone', 1)
            self.bridge.action.emit('activate'); QTest.qWait(180)
            self.assertTrue(scanner.property('visible'))
            scanner.close(); QTest.qWait(180)
            pending = [self.window.contentItem()]; button = None
            while pending:
                item = pending.pop()
                if item.objectName() == 'emptyLibraryAction-0': button = item; break
                pending.extend(item.childItems())
            self.assertIsNotNone(button)
            point = button.mapToScene(QPointF(button.width()/2, button.height()/2)).toPoint()
            QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, pos=point); QTest.qWait(250)
            self.assertTrue(scanner.property('visible'), self.messages[-10:])
        self.assertFalse(self.bridge.players.authenticated())

    def test_local_import_launch_spec_and_statistics_survive_reopening_sqlite(self):
        with tempfile.TemporaryDirectory(dir=self.root) as folder:
            game = Path(folder) / 'My local game'; game.mkdir()
            shutil.copyfile(sys.executable, game / 'play.exe')
            receipt = self.bridge.library.register_external(str(game), 'play.exe', 'My local game')
            self.bridge.library.reconcile()
            identity = receipt['card']['id']
            with patch('ngs.api.MetadataApi.call') as metadata, patch('ngs.players.PlayerTransport.call') as remote:
                worker = GameWorker(self.bridge, receipt['card'], 'launch')
                ready = []; worker.completed.connect(ready.append); worker.run()
                self.assertEqual(len(ready), 1)
                now = [0.0]
                clock = PlayTime(self.bridge.library.path, clock=lambda: now[0])
                clock.start(identity)
                session = clock.snapshot()['session_id']
                self.bridge.players.attach_current(session, identity, receipt['card']['name'])
                now[0] = 12; clock.checkpoint(force=True, reason='exited')
                reopened = LocalLibrary(self.storage)
                self.assertIn(identity, [item['card']['id'] for item in reopened.reconcile()])
                with closing(sqlite3.connect(reopened.path)) as db:
                    self.assertEqual(db.execute('SELECT active_ms FROM play_sessions WHERE id=?', (session,)).fetchone()[0], 12000)
                    self.assertIsNone(db.execute('SELECT id FROM player_outbox WHERE id=?', (session,)).fetchone())
                metadata.assert_not_called(); remote.assert_not_called()
            reopened.forget(identity)
            self.assertTrue((game / 'play.exe').is_file())

    def test_local_store_scan_does_not_construct_a_server_cache(self):
        with patch('ngs.app.installed_store_games', return_value={'rows': []}), \
                patch('ngs.app.CatalogCache', side_effect=AssertionError('NAS cache requested')):
            worker = GameWorker(self.bridge, dict(id='', name='Store scan'), 'store_scan')
            ready = []; worker.completed.connect(ready.append); worker.run()
            self.assertEqual(ready, [{'rows': []}])

    def test_server_sign_in_switches_mode_without_losing_local_data(self):
        b = self.bridge
        settings = dict(b.settingsState, tactic_url='http://127.0.0.1:9/tactic')
        self.assertTrue(b.applySettings(settings), b.settingsStatus)
        path = b.library.path
        with closing(sqlite3.connect(path)) as db:
            sessions = db.execute('SELECT COUNT(*) FROM play_sessions').fetchone()[0]
        account = dict(server='http://127.0.0.1:9', profile='a'*32, username='player', token='b'*64,
                       expires=2000000000, api=dict(device='c'*32, secret='d'*64, expires=2000000000))
        with patch.object(b, 'refreshCatalog') as refresh, patch.object(b, 'pumpDownloads'), patch.object(b.players, 'refreshBackups'):
            b.players.complete('login', account); b.players.changed.emit()
            self.assertFalse(b.localMode)
            self.assertFalse(b.loginRequired)
            self.assertTrue(b.players.authorized())
            self.assertFalse(self.storage.read_settings()['local_mode'])
            refresh.assert_called_once()
            self.assertEqual(b.library.path, path)
            self.assertTrue(b.useLocalLibrary(), b.settingsStatus)
            self.assertTrue(b.localMode)
            self.assertFalse(b.players.authorized())
            self.assertEqual(b.players.account['profile'], account['profile'])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM play_sessions').fetchone()[0], sessions)

    def test_switching_to_local_waits_for_game_and_does_not_require_valid_server_address(self):
        b = self.bridge
        b.settings.update(local_mode=False, tactic_url='invalid old address')
        b.process = SimpleNamespace(frozen=False)
        try:
            self.assertFalse(b.useLocalLibrary())
            self.assertFalse(b.localMode)
        finally: b.process = None
        self.assertTrue(b.useLocalLibrary(), b.settingsStatus)
        self.assertFalse(b.loginRequired)

    def test_saved_profile_in_local_mode_does_not_upload_history_or_backups(self):
        b = self.bridge
        b.players.account = dict(server='http://127.0.0.1:9', profile='a'*32, token='b'*64, expires=2000000000)
        with patch.object(b.players, 'start') as start, patch.object(b.players, '_start_backup_worker') as backup:
            b.players.background_sync(); b.players.sync(); b.players.autoBackup(); b.players.importHistory()
            self.assertFalse(b.players.active_account())
            start.assert_not_called(); backup.assert_not_called()

    def test_old_profile_without_mode_keeps_server_access_but_empty_profile_unlocks_local(self):
        b = self.bridge
        b.settings.pop('local_mode', None)
        b.players.account = {}
        self.assertTrue(b.localMode)
        self.assertFalse(b.loginRequired)
        b.players.account = dict(profile='a'*32)
        self.assertFalse(b.localMode)
        self.assertTrue(b.loginRequired)

    def test_disabling_automatic_artwork_cancels_the_current_lookup(self):
        import threading
        worker = SimpleNamespace(cancel=threading.Event())
        self.bridge.artwork_worker = worker
        try:
            self.assertTrue(self.bridge.applySettings(dict(self.bridge.settingsState, resolve_local_metadata=False)))
            self.assertTrue(worker.cancel.is_set())
        finally: self.bridge.artwork_worker = None
