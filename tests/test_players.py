import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from Crypto.PublicKey import RSA

from ngs import player_protocol as protocol
from ngs.play_time import PlayTime
from ngs.players import PlayerStore, game_identity, server_origin


class PlayerProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = RSA.generate(2048)
        cls.public = cls.key.public_key().export_key()
        cls.private = cls.key.export_key()

    def test_standard_jwe_roundtrip_and_bound_response(self):
        wire, key, identity = protocol.request(self.public, {'password': 'not-visible-on-http'})
        self.assertNotIn(b'not-visible', wire)
        value, received = protocol.open_message(wire, self.private, protocol.key_id(self.public), private=True)
        self.assertEqual(value['request_id'], identity)
        self.assertEqual(received, key)
        response = protocol.seal({'request_id': identity, 'ok': True}, key, kid=protocol.key_id(self.public))
        self.assertEqual(protocol.open_message(response, key, protocol.key_id(self.public))[0]['request_id'], identity)

    def test_jwe_tampering_wrong_key_and_algorithm_are_rejected(self):
        wire, key, unused = protocol.request(self.public, {'password': 'some-password'})
        pieces = wire.split(b'.')
        pieces[3] = protocol.encode(bytes([protocol.decode(pieces[3].decode())[0] ^ 1]) + protocol.decode(pieces[3].decode())[1:]).encode()
        with self.assertRaises(ValueError): protocol.open_message(b'.'.join(pieces), self.private, protocol.key_id(self.public), True)
        pieces = wire.split(b'.'); header = json.loads(protocol.decode(pieces[0].decode())); header['alg'] = 'none'
        pieces[0] = protocol.encode(protocol.compact(header)).encode()
        with self.assertRaises(ValueError): protocol.open_message(b'.'.join(pieces), self.private, protocol.key_id(self.public), True)
        with self.assertRaises(ValueError): protocol.open_message(wire, self.private, 'wrong-key-id', True)
        with self.assertRaises(ValueError): protocol.open_message(b'x' * 32769, key, '')

    def test_passwords_have_salted_scrypt_and_validation(self):
        password = 'long-русский-password'
        a = protocol.password_hash(password, '01'*16)
        self.assertEqual(a, protocol.password_hash(password, '01'*16))
        self.assertNotEqual(a, protocol.password_hash(password, '02'*16))
        with self.assertRaises(ValueError): protocol.password_hash('short', '01'*16)
        self.assertEqual(protocol.username('Player-One'), 'player-one')
        with self.assertRaises(ValueError): protocol.username('../admin')

    def test_session_dates_counters_and_fields_are_bounded(self):
        now = int(time.time())
        row = dict(id=uuid.uuid4().hex, game='steam-1', name='Game', started_at=now-10, updated_at=now,
                   ended_at=None, active_ms=10000, paused_ms=0)
        self.assertEqual(protocol.session_row(row, now), row)
        for change in ({'active_ms': -1}, {'active_ms': True}, {'active_ms': 999999999}, {'game': "sql';--"},
                       {'ended_at': now+1}, {'updated_at': now+500}, {'extra': 1}):
            with self.assertRaises((ValueError, TypeError)): protocol.session_row(dict(row, **change), now)


class PlayerStoreTests(unittest.TestCase):
    def test_offline_restart_lost_server_reply_and_multiple_batches_never_double_count(self):
        from unittest.mock import patch
        from PySide6.QtCore import QObject
        from ngs.players import PlayerWorker
        from ngs.portable import PortableStorage
        for unused in range(20):
            self.start(); self.elapsed += 10; self.clock.checkpoint(reason='completed')
        self.assertEqual(self.store.pending_count(self.account), 20)
        self.assertEqual(len(self.store.pending(self.account)), 16)
        self.store = PlayerStore(self.path)
        controller = QObject()
        controller.store, controller.storage = self.store, PortableStorage(self.folder.name)
        controller.account = dict(self.account, token='b'*64, expires=2000000000, api=dict(device='c'*32,secret='d'*64,expires=2000000000))
        controller.device = 'c'*32
        controller.origin = lambda: self.account['server']
        committed, drop_reply = {}, [True]
        def remote(transport, value):
            if value['action'] == 'sync':
                for row in value['rows']: committed[row['id']] = row
                if drop_reply[0]:
                    drop_reply[0] = False
                    raise ConnectionError('Reply lost after server commit')
            return dict(acked=[r['id'] for r in value['rows']], more=False,
                        totals=[dict(game='steam-1', launches=len(committed),
                                     active_ms=sum(r['active_ms'] for r in committed.values()), longest_ms=10000)])
        failed, completed = [], []
        with patch('ngs.players.PlayerTransport.call', remote):
            first = PlayerWorker(controller, 'sync', {})
            first.failed.connect(lambda message, offline: failed.append(offline))
            first.run()
            self.assertEqual(failed, [True])
            self.assertEqual(self.store.pending_count(self.account), 20)
            again = PlayerWorker(controller, 'sync', {})
            again.result.connect(completed.append); again.run()
        self.assertEqual(len(completed), 1)
        self.assertEqual(self.store.pending_count(self.account), 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT launches,active_ms FROM player_totals').fetchone(), (20, 200000))
        self.assertEqual(len(committed), 20)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.folder.name)/'library.sqlite'
        self.elapsed, self.wall = 0, int(time.time())
        self.clock = PlayTime(self.path, lambda: self.elapsed, lambda: self.wall + self.elapsed)
        self.store = PlayerStore(self.path)
        self.account = {'server':'http://nas:8888','profile':'a'*32}

    def tearDown(self): self.folder.cleanup()

    def start(self):
        self.clock.start('local-path-derived-id')
        session = self.clock.snapshot()['session_id']
        self.store.attach(session, self.account, {'catalog_id':'steam-1','card':{'name':'Game'}}, 'Game')
        return session

    def test_local_reset_does_not_reset_shared_cache_or_rewind_outbox(self):
        session = self.start(); self.elapsed = 20; self.clock.checkpoint(force=True)
        rows = self.store.pending(self.account)
        self.store.accept(self.account, dict(acked=[session], more=False, totals=[dict(game='steam-1', launches=3, active_ms=100000, longest_ms=50000)]), rows)
        self.clock.reset(session); self.elapsed += 5; self.clock.checkpoint(force=True)
        local = self.store.totals(self.account, self.clock.snapshot())
        self.assertEqual((local['total_seconds'], local['launches'], local['longest_seconds']), (5,0,5))
        pending = self.store.pending(self.account)
        self.assertEqual((pending[0]['id'], pending[0]['active_ms']), (session,25000))
        self.store.accept(self.account, dict(acked=[session], more=False, totals=[dict(game='steam-1', launches=3, active_ms=105000, longest_ms=50000)]), pending)
        self.assertEqual(self.store.totals(self.account, self.clock.snapshot())['total_seconds'], 5)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT launches,active_ms FROM player_totals').fetchone(), (3,105000))

    def test_server_total_plus_only_unsent_delta_no_double_count(self):
        session = self.start(); self.elapsed=20; self.clock.checkpoint(force=True)
        rows = self.store.pending(self.account)
        response = dict(acked=[session], more=False, totals=[dict(game='steam-1', launches=3,active_ms=100000,longest_ms=50000)])
        self.store.accept(self.account,response,rows)
        self.assertEqual(self.store.pending(self.account), [])
        snapshot = self.store.totals(self.account,self.clock.snapshot())
        self.assertEqual((snapshot['total_seconds'],snapshot['launches']), (100,3))
        self.elapsed=25
        snapshot = self.store.totals(self.account,self.clock.snapshot())
        self.assertEqual((snapshot['total_seconds'],snapshot['launches']), (105,3))
        self.clock.checkpoint(reason='completed')
        self.assertEqual(len(self.store.pending(self.account)),1)
        # Restart keeps the same session UUID and acknowledged counters.
        again = PlayerStore(self.path)
        self.assertEqual(again.pending(self.account)[0]['id'],session)

    def test_account_switch_never_reassigns_an_owned_session(self):
        session = self.start(); self.elapsed=10; self.clock.checkpoint(force=True)
        other = dict(self.account, profile='b'*32)
        self.store.attach(session,other,{'card':{'id':'steam-2','name':'Other'}}, 'Other')
        self.assertEqual(self.store.pending(other),[])
        self.assertEqual(len(self.store.pending(self.account)),1)
        self.assertEqual(self.store.legacy(),[])
        another_server = dict(self.account,server='http://another-nas')
        self.assertEqual(self.store.pending(another_server),[])

    def test_failed_response_does_not_acknowledge_or_replace_cache(self):
        session = self.start(); self.elapsed=10; self.clock.checkpoint(force=True)
        rows = self.store.pending(self.account)
        with self.assertRaises(ValueError): self.store.accept(self.account,dict(acked=['wrong'],totals=[],more=False),rows)
        self.assertEqual(self.store.pending(self.account),rows)
        response = dict(acked=[session],more=False,totals=[dict(game='steam-1',launches=1,active_ms=-1,longest_ms=0)])
        with self.assertRaises(ValueError): self.store.accept(self.account,response,rows)
        self.assertEqual(self.store.pending(self.account),rows)

    def test_legacy_history_requires_explicit_attachment(self):
        self.clock.start('steam-42'); self.elapsed=5; self.clock.checkpoint(reason='completed')
        self.assertEqual(len(self.store.legacy()),1)
        self.assertEqual(self.store.pending(self.account),[])

    def test_identity_uses_catalog_not_path_and_titles_for_unmatched_games(self):
        a = {'catalog_id':'steam-1','card':{'id':'local-aaa','name':'Game'}}
        b = {'catalog_id':'steam-1','card':{'id':'local-bbb','name':'Game'}}
        self.assertEqual(game_identity(a,'Game'),game_identity(b,'Game'))
        a['catalog_id']='';b['catalog_id']=''
        self.assertEqual(game_identity(a,'Game'),game_identity(b,'Game'))
        self.assertEqual(server_origin('http://NAS:8888/tactic'), 'http://nas:8888')
        with self.assertRaises(ValueError): server_origin('http://user:password@nas')


if __name__ == '__main__': unittest.main()
