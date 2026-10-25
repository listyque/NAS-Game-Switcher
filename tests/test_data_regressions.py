"""Regression tests for the isolated data audit; no services or games are launched."""
import io
import hashlib
import json
import os
import shlex
import sqlite3
import struct
import tempfile
import threading
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ngs.api import ApiCallError, MetadataApi, response_signature
from ngs.bulk_scan import metadata_requests
from ngs.downloads import DownloadQueue
from ngs.http_retry import transient_network_error
from ngs.install import Cancelled, LocalLibrary
from ngs.on_demand import _download_stream
from ngs.play_time import PlayTime
from ngs.portable import PortableStorage
from ngs.store_games import installed_store_games, import_store_games


class DataRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ngs-data-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def api(self, status=503, valid_signature=True):
        client = MetadataApi.__new__(MetadataApi)
        client.url = 'http://test.invalid/tactic/ngs_launcher/ngs-api/v1/metadata'
        client.device, client.secret = 'a' * 32, 'b' * 64
        calls = []

        def response(request, timeout):
            calls.append(json.loads(request.data))
            body = b'{"schema":1,"error":"Metadata unavailable"}'
            result = io.BytesIO(body)
            result.code = status
            signature = response_signature(client.secret, request.get_header('X-ngs-nonce'), status, body)
            result.headers = {'X-NGS-Signature': signature if valid_signature else '0' * 64}
            return result

        client.opener = SimpleNamespace(open=response)
        return client, calls

    def stream_failure(self, client, cancel=None, progress=lambda *args: None):
        key = self.root / 'public-key'
        key.write_bytes(b'unused-before-transfer-error')
        storage = SimpleNamespace(state=self.root, config=self.root, resources=self.root)
        with patch('ngs.on_demand.MetadataApi', return_value=client), \
                patch('ngs.server_trust.server_key_path', return_value=key), \
                patch('ngs.http_retry.RETRY_DELAYS', (0, 0, 0)):
            return _download_stream(storage, SimpleNamespace(origin='http://test.invalid'),
                                    dict(id='game', revision=1), self.root, 'c' * 32,
                                    cancel or threading.Event(), progress, None)

    def test_signed_503_uses_bounded_retries_and_requeues_the_download(self):
        client, calls = self.api()
        with self.assertRaises(ConnectionError) as caught:
            self.stream_failure(client)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(value == calls[0] for value in calls))
        self.assertTrue(transient_network_error(caught.exception))
        queue = DownloadQueue.__new__(DownloadQueue)
        queue.path = self.root / 'queue.sqlite'
        with closing(sqlite3.connect(queue.path)) as db, db:
            db.execute('CREATE TABLE queue (game_id TEXT PRIMARY KEY, job TEXT)')
            db.execute('INSERT INTO queue VALUES (?,?)', ('game', json.dumps(dict(state='running', token='c' * 32))))
        queue.failed_transfer('game', caught.exception)
        with closing(sqlite3.connect(queue.path)) as db:
            state = json.loads(db.execute('SELECT job FROM queue').fetchone()[0])
        self.assertEqual(state['state'], 'queued')
        self.assertGreater(state['retry_at'], 0)

    def test_forged_503_and_permission_failures_are_not_retried(self):
        for status, signed in ((503, False), (403, True), (401, True), (409, True)):
            with self.subTest(status=status, signed=signed):
                client, calls = self.api(status, signed)
                with self.assertRaises(RuntimeError) as caught:
                    self.stream_failure(client)
                self.assertEqual(len(calls), 1)
                self.assertFalse(transient_network_error(caught.exception))

    def test_cancel_during_503_retry_stops_before_the_next_request(self):
        client, calls = self.api()
        stopped = threading.Event()
        with self.assertRaises(Cancelled):
            self.stream_failure(client, stopped, lambda *args: stopped.set())
        self.assertEqual(len(calls), 1)

    def game(self, name='game'):
        folder = self.root / name
        (folder / 'DOSBOX').mkdir(parents=True)
        raw = bytearray(68)
        raw[:2] = b'MZ'
        struct.pack_into('<I', raw, 60, 64)
        raw[64:] = b'PE\0\0'
        (folder / 'DOSBOX/DOSBox.exe').write_bytes(raw)
        (folder / 'other.exe').write_bytes(raw)
        (folder / 'dosbox-game.conf').write_text('[autoexec]\nGAME.EXE\n')
        return folder

    def store_setup(self, task=None):
        folder = self.game()
        task = task or dict(isPrimary=True, type='FileTask', path='DOSBOX/DOSBox.exe',
                            arguments='-conf "dosbox-game.conf" -noconsole', workingDir='')
        (folder / 'goggame-42.info').write_text(json.dumps(dict(name='DOS game', playTasks=[task])))
        registry = {r'Software\GOG.com\Games': [dict(key='42', path=str(folder), gamename='DOS game')]}
        self.addCleanup(patch.stopall)
        patch('ngs.store_games.registry_rows', side_effect=lambda key: registry.get(key, [])).start()
        patch.dict(os.environ, {'ProgramData': str(self.root / 'Data'), 'ProgramFiles': str(self.root / 'Programs'),
                                'ProgramFiles(x86)': str(self.root / 'Programs32')}).start()
        patch('ngs.install.installation_lock', side_effect=lambda storage: nullcontext()).start()
        if os.name != 'nt':
            # Only the Windows command-line parsing boundary is substituted on Linux.
            # Quoting semantics themselves are covered by the native process tests.
            patch('ngs.process.split_launch_arguments', side_effect=shlex.split).start()
        return folder, PortableStorage(self.root / 'launcher')

    def test_gog_arguments_and_working_directory_reach_the_launch_command(self):
        folder, storage = self.store_setup()
        found = installed_store_games()
        self.assertFalse(found['warnings'])
        result = import_store_games(storage, found['rows'], [], threading.Event(), lambda event: None, offline=True)
        self.assertEqual(result['rows'][0]['state'], 'artwork')
        library = LocalLibrary(storage)
        receipt = library.reconcile()[0]
        identity = receipt['card']['id']
        library.set_launch_arguments(identity, '-windowed "wide screen"')
        executable, cwd, arguments = library.launch_spec(identity)
        self.assertEqual(executable, folder / 'DOSBOX/DOSBox.exe')
        self.assertEqual(cwd, folder)
        self.assertEqual(arguments, ['-conf', 'dosbox-game.conf', '-noconsole', '-windowed', 'wide screen'])
        # Reconfirmation after an EXE update must retain store-supplied launch defaults.
        library.register_external(folder, 'DOSBOX/DOSBox.exe', 'DOS game')
        library.reconcile()
        self.assertEqual(library.launch_spec(identity), (executable, cwd, arguments))

    def test_explicit_different_exe_does_not_inherit_the_gog_task(self):
        folder, storage = self.store_setup()
        row = dict(installed_store_games()['rows'][0], preferred_exe='other.exe')
        import_store_games(storage, [row], [], threading.Event(), lambda event: None, offline=True)
        library = LocalLibrary(storage)
        receipt = library.reconcile()[0]
        self.assertEqual(library.launch_spec(receipt['card']['id']), (folder / 'other.exe', folder, []))

    def test_unsafe_gog_working_directory_is_not_silently_ignored(self):
        self.store_setup(dict(isPrimary=True, type='FileTask', path='DOSBOX/DOSBox.exe',
                              arguments='', workingDir='../outside'))
        found = installed_store_games()
        self.assertEqual(found['rows'], [])
        self.assertTrue(found['warnings'])

    def test_external_launch_argument_bounds_are_preserved(self):
        folder, storage = self.store_setup()
        library = LocalLibrary(storage)
        for arguments in (['bad\0arg'], ['x'] * 65, ['x' * 2049], 'not a list'):
            with self.subTest(arguments=repr(arguments)[:60]), self.assertRaises(ValueError):
                library.register_external(folder, 'DOSBOX/DOSBox.exe', 'DOS game', arguments=arguments)
        with self.assertRaises(ValueError):
            library.register_external(folder, 'DOSBOX/DOSBox.exe', 'DOS game', working_directory='../outside')

    def test_reconcile_writes_only_changed_receipts(self):
        folder, storage = self.store_setup()
        library = LocalLibrary(storage)
        receipt = library.register_external(folder, 'DOSBOX/DOSBox.exe', 'DOS game')
        library.reconcile()
        with closing(sqlite3.connect(library.path)) as db, db:
            db.execute('CREATE TABLE writes (id TEXT)')
            db.execute('CREATE TRIGGER record_updates AFTER UPDATE ON installed BEGIN INSERT INTO writes VALUES(new.game_id); END')
        library.reconcile()
        library.reconcile()
        with closing(sqlite3.connect(library.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM writes').fetchone()[0], 0)
        receipt['card']['name'] = 'Renamed game'
        (library.records / (receipt['card']['id'] + '.json')).write_text(json.dumps(receipt))
        self.assertEqual(library.reconcile()[0]['card']['name'], 'Renamed game')
        with closing(sqlite3.connect(library.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM writes').fetchone()[0], 1)

    def test_offline_metadata_reads_never_wait_for_network_rate_limits(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.side_effect = AssertionError('Offline cache waited for a network quota')
        with patch('ngs.bulk_scan.client_metadata', return_value=[]) as request, \
                patch('ngs.bulk_scan.time.monotonic', return_value=1):
            metadata = metadata_requests(None, 'http://test.invalid', cancel, lambda event: None, offline=True)
            for unused in range(100):
                self.assertEqual(metadata('resolve', '42'), [])
        self.assertEqual(request.call_count, 100)
        self.assertTrue(all(call.kwargs.get('offline') is True for call in request.call_args_list))
        cancel.wait.assert_not_called()

    def test_copy_package_progress_keeps_the_package_count_between_files(self):
        from ngs.packages import copy_packages
        payload = self.root / 'payload'
        payload.mkdir()
        parts = [b'abc', b'def']
        chunks = [dict(path='/unused', size=len(data), sha256=hashlib.sha256(data).hexdigest()) for data in parts]
        content = {chunk['sha256']: data for chunk, data in zip(chunks, parts)}
        document = dict(total_size=6, files=[dict(path='game.bin', size=6)], http=dict(chunks=chunks))
        events = []

        def fetch(cache, chunk, target, cancel, progress, received):
            target.write_bytes(content[chunk['sha256']])
            received(chunk['size'])

        copy_packages(None, document, payload, self.root, threading.Event(),
                      lambda *args: events.append(args), fetch_chunk=fetch)
        self.assertEqual((payload / 'game.bin').read_bytes(), b'abcdef')
        labels = [event[3] for event in events if event[0] == 'files']
        self.assertEqual(labels, ['Пакет 1 из 2', 'Пакет 2 из 2'])

    def test_transfer_scratch_cleanup_does_not_replace_success_or_network_failure(self):
        from ngs.transfer import run_transfer
        storage = PortableStorage(self.root / 'launcher')
        for result in (dict(status='ok', receipt={'game': 'installed'}),
                       dict(status='error', retryable=True, message='connection lost')):
            def process(command, **kwargs):
                folder = Path(command[-1]).parent
                (folder / 'result.json').write_text(json.dumps(result))
                return SimpleNamespace(poll=lambda: 0)

            with patch('ngs.transfer.subprocess.Popen', side_effect=process), \
                    patch('ngs.transfer.shutil.rmtree', side_effect=PermissionError('indexer is reading the log')), \
                    self.assertLogs(level='WARNING'):
                if result['status'] == 'ok':
                    self.assertEqual(run_transfer(storage, 'game', 'http://test.invalid', threading.Event(),
                                                  lambda *args: None), result['receipt'])
                else:
                    with self.assertRaisesRegex(ConnectionError, 'connection lost'):
                        run_transfer(storage, 'game', 'http://test.invalid', threading.Event(), lambda *args: None)

    def test_failed_worker_reads_only_the_bounded_log_tail(self):
        from ngs.transfer import run_transfer
        storage = PortableStorage(self.root / 'launcher')
        raw = b'x' * (128 * 1024) + b'last error'
        original = Path.read_bytes

        def read_bytes(path):
            if path.name == 'worker.log':
                raise AssertionError('An unbounded read of the entire worker log was attempted')
            return original(path)

        def process(command, **kwargs):
            kwargs['stderr'].write(raw)
            kwargs['stderr'].flush()
            return SimpleNamespace(poll=lambda: 0)

        with patch('ngs.transfer.subprocess.Popen', side_effect=process), patch.object(Path, 'read_bytes', read_bytes):
            with self.assertRaisesRegex(OSError, 'без результата'):
                run_transfer(storage, 'game', 'http://test.invalid', threading.Event(), lambda *args: None)
        self.assertEqual((storage.logs / 'transfer-error.log').read_bytes(), raw[-64 * 1024:])

    def test_valid_cached_manifest_is_verified_without_rewriting_it(self):
        from ngs.install import fetch_manifest, manifest_bytes
        document = dict(schema=1, game_id='game', source_folder='Game', executable='play.exe',
                        working_directory='', arguments=[], directories=[], total_size=1,
                        files=[dict(path='play.exe', size=1, sha256=hashlib.sha256(b'x').hexdigest())])
        raw = manifest_bytes(document)
        digest = hashlib.sha256(raw).hexdigest()
        cache = SimpleNamespace(folder=self.root, fetch=Mock(return_value=raw))
        card = dict(id='game', source_folder='Game', executable='play.exe',
                    manifest=dict(path='/assets/ngs_launcher/game.json', size=len(raw), sha256=digest))
        target = self.root / (digest + '.manifest.json')
        target.write_bytes(raw)
        with patch('ngs.install.atomic_write', side_effect=AssertionError('Rewrote a valid cache entry')):
            self.assertEqual(fetch_manifest(cache, card, threading.Event()), document)
        cache.fetch.assert_not_called()
        target.write_bytes(b'!' * len(raw))
        self.assertEqual(fetch_manifest(cache, card, threading.Event()), document)
        cache.fetch.assert_called_once()
        self.assertEqual(target.read_bytes(), raw)

    def test_cancelled_manifest_build_does_not_walk_the_game_tree(self):
        from ngs.install import build_manifest
        (self.root / 'Game').mkdir()
        cancel = threading.Event()
        cancel.set()
        with patch('ngs.install.os.scandir', side_effect=AssertionError('Cancelled inventory still walked files')):
            with self.assertRaises(Cancelled):
                build_manifest(self.root, dict(source_folder='Game'), cancel)


class SessionDateRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ngs-clock-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'library.sqlite'
        self.tick, self.wall = 0.0, 1800000000
        self.tracker = PlayTime(self.path, lambda: self.tick, lambda: self.wall)

    def row(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute('SELECT * FROM play_sessions').fetchone())

    def test_backward_wall_clock_keeps_monotonic_duration_and_valid_dates(self):
        self.tracker.start('game')
        self.tick = 60.5
        self.wall -= 3600
        self.tracker.checkpoint(reason='completed')
        row = self.row()
        self.assertEqual(row['active_ms'], 60500)
        self.assertEqual(row['started_at'], 1800000000)
        self.assertEqual(row['updated_at'], 1800000061)
        self.assertEqual(row['ended_at'], row['updated_at'])

    def test_legacy_impossible_dates_are_repaired_without_changing_identity_or_counters(self):
        self.tracker.start('game')
        before = self.row()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE play_sessions SET updated_at=started_at-60, ended_at=started_at-60, active_ms=60000')
        PlayTime(self.path, lambda: self.tick, lambda: self.wall)
        after = self.row()
        self.assertEqual((after['id'], after['started_at']), (before['id'], before['started_at']))
        self.assertEqual(after['active_ms'], 60000)
        self.assertEqual(after['updated_at'], before['started_at'] + 60)
        self.assertEqual(after['ended_at'], after['updated_at'])

    def test_future_session_does_not_block_an_ordinary_session(self):
        from ngs.players import PlayerStore
        from ngs.player_protocol import session_row
        store = PlayerStore(self.path)
        account = dict(server='http://test.invalid', profile='a' * 32)
        self.tracker.start('game')
        future = self.tracker.snapshot()['session_id']
        store.attach(future, account, {'card': {'id': 'game', 'name': 'Game'}}, 'Game')
        self.tick = 10
        self.wall += 10
        self.tracker.checkpoint(reason='completed')
        self.wall -= 3600
        self.tracker.start('game')
        ordinary = self.tracker.snapshot()['session_id']
        store.attach(ordinary, account, {'card': {'id': 'game', 'name': 'Game'}}, 'Game')
        self.tick += 10
        self.wall += 10
        self.tracker.checkpoint(reason='completed')
        pending = store.pending(account, now=self.wall)
        self.assertEqual([row['id'] for row in pending], [ordinary])
        self.assertEqual(store.deferred_count(account, now=self.wall), 1)
        self.assertEqual(store.pending_count(account), 2)
        session_row(pending[0], self.wall)
        later = store.pending(account, now=1800000100)
        self.assertEqual({row['id'] for row in later}, {future, ordinary})
        for row in later:
            session_row(row, 1800000100)

    def test_multiline_title_and_legacy_outbox_name_are_valid_on_the_wire(self):
        from ngs.players import PlayerStore
        from ngs.player_protocol import session_row
        store = PlayerStore(self.path)
        account = dict(server='http://test.invalid', profile='a' * 32)
        self.tracker.start('game')
        identity = self.tracker.snapshot()['session_id']
        store.attach(identity, account, {'card': {'id': 'game', 'name': 'Game\nDeluxe'}}, 'Game')
        self.assertEqual(store.pending(account, now=self.wall)[0]['name'], 'Game Deluxe')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE player_outbox SET name=?', ('Game\t\nDeluxe',))
        row = store.pending(account, now=self.wall)[0]
        self.assertEqual(row['name'], 'Game Deluxe')
        session_row(row, self.wall)
