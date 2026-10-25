"""Local scheduler and real encrypted protocol regressions; no external services."""
import hashlib
import inspect
import json
import re
import sqlite3
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from Crypto.PublicKey import RSA
from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtTest import QTest

from ngs import player_protocol as protocol
from ngs.install import LocalLibrary
from ngs.play_time import PlayTime
from ngs.players import PlayerAccounts, PlayerStore, PlayerWorker
from ngs.portable import PortableStorage
from tools.player_accounts import player_action


class PlayerSchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ngs-profile-schedule-')
        self.addCleanup(self.temporary.cleanup)
        self.storage = PortableStorage(self.temporary.name)
        self.storage.read_settings = lambda: {'tactic_url': 'http://test.invalid/tactic'}
        self.library = LocalLibrary(self.storage)
        self.elapsed, self.wall = 0.0, int(time.time())
        self.tracker = PlayTime(self.library.path, lambda: self.elapsed, lambda: self.wall + self.elapsed)
        self.parent = QObject()
        self.parent.process = None
        self.account = PlayerAccounts(self.storage, self.library, self.parent)
        self.account.timer.stop()
        self.addCleanup(self.account.stop)
        self.account.account = dict(server='http://test.invalid', profile='a' * 32, username='player',
                                    token='b' * 64, expires=self.wall + 3600,
                                    api=dict(device='c' * 32, secret='d' * 64, expires=self.wall + 3600))
        self.account._pull_at = 300.0
        self.addCleanup(patch.stopall)
        patch('ngs.players.time.monotonic', side_effect=lambda: self.elapsed).start()
        patch('ngs.players.time.time', side_effect=lambda: self.wall + self.elapsed).start()

    def start_game(self):
        self.tracker.start('game')
        identity = self.tracker.snapshot()['session_id']
        self.account.store.attach(identity, self.account.account, {'card': {'id': 'game', 'name': 'Game'}}, 'Game')
        return identity

    def test_idle_minute_ticks_do_not_start_workers_or_http_before_five_minutes(self):
        with patch.object(self.account, 'start') as start, patch('ngs.players.PlayerTransport') as transport:
            for stamp in range(0, 300, 10):
                self.elapsed = stamp
                self.account.background_sync()
            start.assert_not_called()
            transport.assert_not_called()
            self.elapsed = 300
            self.account.background_sync()
            start.assert_called_once_with('sync')

    def test_manual_refresh_ignores_background_deadlines_and_busy_never_starts_a_second_worker(self):
        self.account._pull_at = self.account._retry_at = 1000
        with patch.object(self.account, 'start') as start:
            self.account.background_sync()
            start.assert_not_called()
            self.account.sync()
            start.assert_called_once_with('sync')
            start.reset_mock()
            self.account.worker = object()
            try:
                self.elapsed = 2000
                self.account.background_sync()
                start.assert_not_called()
            finally:
                self.account.worker = None

    def test_offline_and_other_sync_failures_have_bounded_backoff_without_http_between_attempts(self):
        self.account._pull_at = 0
        with patch.object(self.account, 'start') as start, patch('ngs.players.PlayerTransport') as transport:
            for delay in (60, 120, 240, 300, 300):
                self.account.worker = types.SimpleNamespace(action='sync')
                self.account.failure('Offline', True)
                self.account.worker = None
                deadline = self.elapsed + delay
                self.assertEqual(self.account._retry_at, deadline)
                for stamp in (self.elapsed, deadline - 1):
                    self.elapsed = stamp
                    self.account.background_sync()
                    start.assert_not_called()
                self.elapsed = deadline
                self.account.background_sync()
                start.assert_called_once_with('sync')
                start.reset_mock()
            transport.assert_not_called()
        self.account.complete('sync', {'synced': int(self.wall + self.elapsed), 'deferred': 0})
        self.assertFalse(self.account.offline)
        self.assertEqual((self.account._retry_at, self.account._retry_delay), (0.0, 60.0))
        self.assertEqual(self.account._pull_at, self.elapsed + 300)

    def test_local_database_failure_is_deferred_without_starting_http(self):
        with patch.object(self.account.store, 'pending', side_effect=sqlite3.OperationalError('locked')), \
                patch.object(self.account, 'start') as start, patch('ngs.players.PlayerTransport') as transport:
            self.account.background_sync()
            self.assertEqual(self.account._retry_at, 60)
            self.elapsed = 30
            self.account.background_sync()
            start.assert_not_called()
            transport.assert_not_called()

    def test_worker_setup_failure_is_deferred_without_repeated_constructor_attempts(self):
        self.account._pull_at = 0
        with patch('ngs.players.PlayerWorker', side_effect=ValueError('Invalid local trust file')) as worker, \
                patch('ngs.players.PlayerTransport') as transport:
            self.account.background_sync()
            self.assertEqual(self.account._retry_at, 60)
            self.elapsed = 30
            self.account.background_sync()
            worker.assert_called_once()
            transport.assert_not_called()
            self.assertIsNone(self.account.worker)

    def test_flush_with_an_already_acked_outbox_does_not_even_create_a_transport(self):
        worker = PlayerWorker(self.account, 'flush', {})
        result = []
        worker.result.connect(result.append)
        with patch('ngs.players.PlayerTransport') as transport:
            worker.run()
            transport.assert_not_called()
        self.assertEqual(result, [{'idle': True}])

    def test_long_running_session_flushes_each_minute_and_full_profile_only_each_five(self):
        identity = self.start_game()
        requests, completed = [], {}

        def remote(transport, value):
            requests.append(value['action'])
            for row in value['rows']:
                completed[row['id']] = dict(row)
            return dict(acked=[row['id'] for row in value['rows']], more=False,
                        totals=[dict(game='game', launches=len(completed),
                                     active_ms=sum(row['active_ms'] for row in completed.values()),
                                     longest_ms=max(row['active_ms'] for row in completed.values()))])

        def execute(action):
            worker = PlayerWorker(self.account, action, {})
            failures = []
            worker.result.connect(lambda value: self.account.complete(action, value))
            worker.failed.connect(lambda *args: failures.append(args))
            worker.run()
            self.assertEqual(failures, [])

        with patch('ngs.players.PlayerTransport.call', remote), patch.object(self.account, 'start', side_effect=execute):
            for minute in range(1, 11):
                self.elapsed = minute * 60
                self.tracker.checkpoint(force=True)
                self.account.background_sync()
                self.assertEqual(completed[identity]['active_ms'], minute * 60000)
                self.assertEqual(self.account.store.pending_count(self.account.account), 0)
        self.assertEqual(requests.count('sync'), 10)
        self.assertEqual(requests.count('pull'), 2)
        self.assertEqual(completed[identity]['ended_at'], None)
        self.assertEqual(self.account.store.totals(self.account.account, self.tracker.snapshot())['total_seconds'], 600)

    def test_automatic_flush_uses_a_real_qthread_and_finishes_without_restarting(self):
        self.start_game()
        self.elapsed = 60
        self.tracker.checkpoint(force=True)
        requests = []

        def remote(transport, value):
            requests.append(value['action'])
            return dict(acked=[row['id'] for row in value['rows']], more=False,
                        totals=[dict(game='game', launches=1, active_ms=60000, longest_ms=60000)])

        with patch('ngs.players.PlayerTransport.call', remote):
            self.account.background_sync()
            for unused in range(100):
                QTest.qWait(5)
                if self.account.worker is None:
                    break
            self.assertIsNone(self.account.worker)
            self.account.background_sync()
            QTest.qWait(10)
        self.assertEqual(requests, ['sync'])
        self.assertEqual(self.account.store.pending_count(self.account.account), 0)
        self.assertEqual(self.account._pull_at, 300)


class PlayerClockServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSA.generate(2048)
        cls.private, cls.public = cls.key.export_key(), cls.key.public_key().export_key()
        cls.kid = protocol.key_id(cls.public)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ngs-clock-server-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'library.sqlite'
        self.now, self.elapsed = int(time.time()), 0
        self.wall = self.now - 60
        self.clock = PlayTime(self.path, lambda: self.elapsed, lambda: self.wall + self.elapsed)
        self.store = PlayerStore(self.path)
        self.account = dict(server='http://test.invalid', profile='a' * 32)
        self.device, self.token = 'c' * 32, 'b' * 64
        self.records = {}
        owner = self

        class Record:
            def __init__(self, kind):
                self.kind, self.values = kind, {}

            def get_value(self, key):
                return self.values.get(key)

            def set_value(self, key, value):
                self.values[key] = value

            def commit(self, **kwargs):
                owner.records[self.kind, self.values['code']] = self

        class Search:
            def __init__(self, kind):
                self.kind, self.code = kind, None

            def add_filter(self, key, value):
                if key != 'code':
                    raise AssertionError('Unexpected query filter')
                self.code = value

            def get_sobject(self):
                return owner.records.get((self.kind, self.code))

        def query(sql):
            if sql.startswith('SELECT pg_advisory_xact_lock'):
                return []
            if sql in ('SELECT COUNT(*) FROM player_gate','SELECT COUNT(*) FROM player_profile'):
                table=sql.rsplit(' ',1)[1]
                return [(sum(kind == 'ngs_launcher/'+table for kind, unused in self.records),)]
            if not sql.startswith('SELECT game,COUNT(*),SUM('):
                raise AssertionError('Unexpected SQL: ' + sql)
            games = re.search(r"AND game IN \(([^)]+)\)", sql)
            requested = set(re.findall(r"'([^']+)'", games[1])) if games else None
            grouped = {}
            for (kind, unused), record in self.records.items():
                if kind != 'ngs_launcher/player_play' or record.get_value('profile') != self.account['profile']:
                    continue
                row = json.loads(record.get_value('data'))
                if requested is not None and row['game'] not in requested:
                    continue
                grouped.setdefault(row['game'], []).append(row)
            return [(game, len(rows), sum(row['active_ms'] for row in rows), max(row['active_ms'] for row in rows))
                    for game, rows in sorted(grouped.items())]

        def module(name, **values):
            result = types.ModuleType(name)
            result.__dict__.update(values)
            return result

        project = types.SimpleNamespace(get_code=lambda: 'ngs_launcher', get_database_name=lambda: 'ngs_launcher',
                                        get_project_db_resource=lambda: 'ngs_launcher')
        security = types.SimpleNamespace(is_admin=lambda: False,
                                         get_login=lambda: types.SimpleNamespace(get_value=lambda key: 'ngs_players_worker'))
        self.modules = {
            'pyasm': module('pyasm'),
            'pyasm.biz': module('pyasm.biz', Project=types.SimpleNamespace(get=lambda: project)),
            'pyasm.common': module('pyasm.common', Environment=types.SimpleNamespace(get_security=lambda: security)),
            'pyasm.search': module('pyasm.search', Search=Search, SearchType=types.SimpleNamespace(create=Record),
                                  DbContainer=types.SimpleNamespace(get=lambda resource: types.SimpleNamespace(
                                      do_query=query, do_update=lambda sql: None))),
        }
        self.record_type = Record
        self.save('player_config', 'crypto', dict(private=self.private.decode(), kid=self.kid))
        self.save('player_login', hashlib.sha256(self.token.encode()).hexdigest(),
                  dict(profile=self.account['profile'], device=self.device, expires=self.now + 10000))
        # Same source and protocol globals as the generated server-side script.
        namespace = dict(vars(protocol))
        exec(inspect.getsource(player_action), namespace)
        self.action = namespace['player_action']

    def save(self, table, code, value):
        record = self.record_type('ngs_launcher/' + table)
        record.set_value('code', code)
        record.set_value('data', json.dumps(value))
        record.commit()

    def send(self, rows):
        wire, key, identity = protocol.request(self.public, dict(action='sync', device=self.device, token=self.token,
                                                               rows=rows, offset=0))
        with patch.dict('sys.modules', self.modules):
            result = self.action(wire.decode(), '127.0.0.1')
        self.assertEqual(result['status'], 200)
        response = protocol.open_message(result['wire'].encode(), key, self.kid)[0]
        self.assertEqual(response['request_id'], identity)
        return response

    def test_repaired_revision_keeps_server_identity_and_stale_reply_cannot_reduce_totals(self):
        self.clock.start('game')
        identity = self.clock.snapshot()['session_id']
        self.store.attach(identity, self.account, {'card': {'id': 'game', 'name': 'Game'}}, 'Game')
        self.elapsed = 30
        self.clock.checkpoint(force=True)
        old = self.store.pending(self.account, now=self.now)[0]
        first = self.send([old])
        self.assertTrue(first['ok'])
        self.store.accept(self.account, first['result'], [old])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE play_sessions SET active_ms=60000, updated_at=started_at-3600, ended_at=started_at-3600')
        PlayTime(self.path)
        repaired = self.store.pending(self.account, now=self.now)[0]
        self.assertEqual((repaired['id'], repaired['started_at']), (old['id'], old['started_at']))
        current = self.send([repaired])
        self.assertTrue(current['ok'])
        self.store.accept(self.account, current['result'], [repaired])
        self.assertEqual(self.store.pending_count(self.account), 0)
        stale = self.send([old])
        self.assertTrue(stale['ok'])
        self.assertEqual(stale['result']['totals'], [dict(game='game', launches=1, active_ms=60000, longest_ms=60000)])
        changed_start = self.send([dict(repaired, started_at=repaired['started_at'] - 1)])
        self.assertFalse(changed_start['ok'])
        saved = json.loads(self.records['ngs_launcher/player_play', identity].get_value('data'))
        self.assertEqual(saved['started_at'], old['started_at'])
        self.assertEqual(saved['active_ms'], 60000)

    def test_more_than_one_batch_of_deferred_old_rows_does_not_starve_valid_sessions(self):
        for index in range(20):
            identity = '%032x' % (index + 1)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('INSERT INTO play_sessions VALUES(?,?,?,?,?,?,?,?)',
                           (identity, 'game', self.now - 100, self.now + 3600, self.now + 3600, 1000, 0, 'completed'))
            self.store.attach(identity, self.account, {'card': {'id': 'game', 'name': 'Game'}}, 'Game')
        self.clock.start('game')
        ordinary = self.clock.snapshot()['session_id']
        self.store.attach(ordinary, self.account, {'card': {'id': 'game', 'name': 'Game'}}, 'Game')
        self.elapsed = 10
        self.clock.checkpoint(reason='completed')
        eligible = self.store.pending(self.account, now=self.now)
        self.assertEqual([row['id'] for row in eligible], [ordinary])
        self.assertEqual(self.store.deferred_count(self.account, now=self.now), 20)
        response = self.send(eligible)
        self.assertTrue(response['ok'])
        self.store.accept(self.account, response['result'], eligible)
        self.assertEqual(self.store.pending_count(self.account), 20)
        self.assertEqual(self.store.pending(self.account, now=self.now), [])
        self.assertEqual(len(self.store.pending(self.account, now=self.now + 3600)), 16)


class PersistedPlayerAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ngs-saved-account-')
        self.addCleanup(self.temporary.cleanup)
        self.storage = PortableStorage(self.temporary.name)
        self.storage.read_settings = lambda: {'tactic_url': 'http://test.invalid/tactic'}
        self.library = LocalLibrary(self.storage)
        PlayTime(self.library.path)
        self.parent = QObject()
        self.parent.process = None
        self.path = self.storage.config / 'player-account.json'
        self.valid = dict(device='a' * 32, account=dict(server='http://test.invalid', profile='b' * 32,
                          username='player', token='c' * 64, expires=2000000000,
                          api=dict(device='d' * 32, secret='e' * 64, expires=2000000000)))

    def load(self):
        with patch('ngs.players.QTimer.singleShot'):
            account = PlayerAccounts(self.storage, self.library, self.parent)
        account.timer.stop()
        self.addCleanup(account.stop)
        return account

    def test_invalid_json_structures_and_field_types_do_not_crash_or_accept_credentials(self):
        invalid = [None, [], 1, True, 'document', {'device': [], 'account': {}},
                   dict(self.valid, account=[]), dict(self.valid, account=['server', 'profile', 'username', 'token', 'expires'])]
        for field, value in (('server', []), ('server', 'http://user:pass@test.invalid'), ('profile', 1),
                             ('profile', 'bad'), ('token', {}), ('username', None), ('expires', True),
                             ('expires', -1), ('api', []), ('api', dict(device=1, secret='e' * 64, expires=2000000000))):
            invalid.append(dict(self.valid, account=dict(self.valid['account'], **{field: value})))
        with patch('ngs.players.remember_secret') as remember, patch('ngs.players.PlayerTransport') as transport:
            for value in invalid:
                with self.subTest(value=repr(value)[:100]):
                    raw = json.dumps(value).encode()
                    self.path.write_bytes(raw)
                    account = self.load()
                    self.assertEqual(account.account, {})
                    self.assertFalse(account.authenticated())
                    self.assertFalse(account.authorized())
                    self.assertEqual(self.path.read_bytes(), raw)
            for raw in (b'{', b' ' * 8193):
                self.path.write_bytes(raw)
                account = self.load()
                self.assertEqual(account.account, {})
                self.assertEqual(self.path.read_bytes(), raw)
            remember.assert_not_called()
            transport.assert_not_called()

    def test_valid_empty_legacy_and_expired_profiles_keep_device_and_existing_file(self):
        legacy = dict(self.valid['account'])
        legacy.pop('api')
        for profile in ({}, legacy, self.valid['account'], dict(self.valid['account'], expires=0)):
            with self.subTest(profile=bool(profile)):
                raw = json.dumps(dict(self.valid, account=profile)).encode()
                self.path.write_bytes(raw)
                account = self.load()
                self.assertEqual(account.device, self.valid['device'])
                self.assertEqual(account.account, profile)
                self.assertEqual(self.path.read_bytes(), raw)
                if profile and profile['expires'] == 0:
                    self.assertFalse(account.authenticated())
