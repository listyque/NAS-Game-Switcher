import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from ngs.api import ApiRejected, MAX_BODY, claim_state, validate_payload
from ngs.diagnostics import SessionLog, remember_secret
from ngs.portable import PortableStorage
from tools.diagnostics import diagnostic_action, provision_diagnostics


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.storage = PortableStorage(self.tmp.name)
        self.device = 'ab' * 16
        self.private = Path(self.tmp.name) / 'private-server'
        self.admin = False
        def mod(name, **fields):
            module = ModuleType(name); module.__dict__.update(fields); return module
        project = SimpleNamespace(get_code=lambda: 'ngs_launcher', get_database_name=lambda: 'ngs_launcher',
            get_value=lambda key: 'NGS handheld game catalog; managed from D:/code/NGS', get_project_db_resource=lambda: 'ngs_launcher')
        security = SimpleNamespace(is_admin=lambda: self.admin, is_logged_in=lambda: True, get_user_name=lambda: 'ngs_catalog_worker')
        owner = self
        self.enabled_devices = {self.device}
        class Search:
            def __init__(self, kind):
                owner.assertIn(kind, ('ngs_launcher/catalog_config', 'ngs_launcher/api_device')); self.kind = kind
            def add_filter(self, key, value):
                owner.assertEqual(key, 'code'); self.code = value
                if self.kind == 'ngs_launcher/catalog_config': owner.assertEqual(value, 'service')
            def get_sobject(self):
                if self.kind == 'ngs_launcher/api_device':
                    return SimpleNamespace(get_value=lambda key: True) if self.code in owner.enabled_devices else None
                # Catalogizer membership deliberately does not include the uploading device.
                return SimpleNamespace(get_value=lambda key: 'NGS remote catalogizer v1' if key == 'description' else json.dumps(dict(devices=['cd' * 16])))
        self.modules = {'pyasm': mod('pyasm'), 'pyasm.biz': mod('pyasm.biz', Project=SimpleNamespace(get=lambda: project)),
            'pyasm.common': mod('pyasm.common', Environment=SimpleNamespace(get_security=lambda: security)),
            'pyasm.search': mod('pyasm.search', Search=Search, DbContainer=SimpleNamespace(get=lambda db: SimpleNamespace(do_query=lambda q: None)))}
        self.mock_modules = patch.dict(sys.modules, self.modules); self.mock_modules.start(); self.addCleanup(self.mock_modules.stop)
        real_path = Path
        self.mock_path = patch('pathlib.Path', side_effect=lambda p: self.private if p == '/opt/tactic/tactic_data/ngs_launcher/diagnostics' else real_path(p))
        self.mock_path.start(); self.addCleanup(self.mock_path.stop)
        self.admin = True; provision_diagnostics(); self.admin = False

    def archive(self, session='cd'*16):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('events.jsonl', b'{"event":"queue.test"}\n' * 100)
            archive.writestr('session.json', json.dumps(dict(session=session)))
        data = raw.getvalue()
        return data, dict(session=session, sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    def test_signed_limits_nonce_and_transfer_lease_are_independent(self):
        now = int(time.time())
        state = claim_state({}, 'transfer', 'transfer', now)
        original = dict(state)
        state = claim_state(state, 'diag', 'diagnostics', now)
        self.assertEqual(state['busy_nonce'], 'transfer'); self.assertEqual(state['busy_until'], original['busy_until'])
        self.assertEqual(state['recent'], original['recent'])
        for op in ('transfer', 'diagnostics'):
            with self.assertRaises(ApiRejected): claim_state(state, 'diag', op, now)
        for i in range(179): state = claim_state(state, str(i), 'diagnostics', now)
        with self.assertRaises(ApiRejected) as error: claim_state(state, 'more', 'diagnostics', now)
        self.assertEqual(error.exception.status, 429)
        state['busy_until'] = 0
        self.assertEqual(claim_state(state, 'new-transfer', 'transfer', now)['busy_nonce'], 'new-transfer')
        data, value = self.archive()
        payload = dict(operation='diagnostics', value=dict(value, action='append', offset=0, data=base64.b64encode(data[:1024]).decode()))
        self.assertLess(len(json.dumps(payload).encode()), MAX_BODY)
        validate_payload(payload)
        for field, invalid in (('session','../escape'), ('size', True), ('sha256', 'g'*64), ('offset', -1), ('data', 'x'*1369)):
            with self.subTest(field=field), self.assertRaises(ValueError): validate_payload(dict(payload, value=dict(payload['value'], **{field: invalid})))

    def test_private_upload_resumes_exact_bytes_finishes_and_refuses_foreign_device(self):
        data, value = self.archive()
        self.assertEqual(diagnostic_action(dict(value, action='status'), self.device), dict(offset=0, complete=False))
        for offset in range(0, len(data), 1024):
            request = dict(value, action='append', offset=offset, data=base64.b64encode(data[offset:offset+1024]).decode())
            result = diagnostic_action(request, self.device)
            self.assertEqual(diagnostic_action(request, self.device), result)
        self.assertEqual(diagnostic_action(dict(value, action='finish'), self.device)['sha256'], value['sha256'])
        self.assertTrue(diagnostic_action(dict(value, action='status'), self.device)['complete'])
        self.assertEqual(next((self.private / self.device).glob('*.zip')).read_bytes(), data)
        with self.assertRaises(PermissionError): diagnostic_action(dict(value, action='status'), 'ef'*16)
        self.admin = True
        with self.assertRaises(PermissionError): diagnostic_action(dict(value, action='status'), self.device)

    def test_changed_or_unsafe_archives_never_become_complete(self):
        data, value = self.archive()
        request = dict(value, action='append', offset=0, data=base64.b64encode(data[:1024]).decode())
        diagnostic_action(request, self.device)
        with self.assertRaises(ValueError): diagnostic_action(dict(request, data=base64.b64encode(b'X'*1024).decode()), self.device)
        with self.assertRaises(ValueError): diagnostic_action(dict(value, action='finish'), self.device)
        self.assertFalse(list(self.private.rglob('*.zip')))

    def test_session_rotation_redacts_secrets_and_preserves_downloads(self):
        remember_secret('unique-device-secret')
        log = SessionLog(self.storage)
        for i in range(150): log.write('test', password='hidden', error='https://name:password@nas/?ticket=abc unique-device-secret', index=i, text='x'*900)
        fixture = self.storage.downloads / ('queue-'+'ef'*16); fixture.mkdir(); (fixture/'payload').write_bytes(b'game files')
        log.pack()
        archives = list(log.archives.glob('*.zip'))
        self.assertGreater(len(archives), 1)
        combined = ''
        for path in archives:
            self.assertLessEqual(path.stat().st_size, 128*1024)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {'events.jsonl', 'session.json'})
                combined += archive.read('events.jsonl').decode()
        for secret in ('unique-device-secret', 'name:password', 'ticket=abc', 'hidden'): self.assertNotIn(secret, combined)
        self.assertEqual((fixture/'payload').read_bytes(), b'game files')
        log.finish()

    def test_outbox_recovers_lost_reply_without_duplicate_or_changed_bytes(self):
        log = SessionLog(self.storage); log.write('queue.test', destination=str(self.storage.games)); log.pack()
        calls = []
        owner = self
        class Api:
            url = 'https://nas/tactic/ngs_launcher/ngs-api/v1/metadata'
            def call(self, operation, value):
                owner.assertEqual(operation, 'diagnostics'); calls.append(value['action'])
                result = diagnostic_action(value, owner.device)
                if len(calls) == 2: raise ConnectionError('Reply lost after commit')
                return result
        with self.assertRaises(ConnectionError): log.upload(Api())
        self.assertFalse(list(log.archives.glob('*.sent')))
        old = log.archives / ('00'*16 + '-' + hashlib.sha256(b'old').hexdigest() + '.zip')
        old.write_bytes(b'old')
        import os; os.utime(old, (1000, 1000))
        order = []
        class OrderedApi:
            url = Api.url
            def call(self, op, val):
                if val['action'] == 'status': order.append(val['session'])
                raise RuntimeError('stop')
        with self.assertRaises(RuntimeError): log.upload(OrderedApi())
        self.assertEqual(order[:1], [log.session])
        old.unlink()
        log.upload(Api())
        self.assertTrue(list(log.archives.glob('*.sent')))
        self.assertEqual(len(list(self.private.rglob('*.zip'))), 1)
        self.assertEqual(next(log.archives.glob('*.zip')).read_bytes(), next(self.private.rglob('*.zip')).read_bytes())
        log.finish()

    def test_queue_snapshot_keeps_database_and_payloads_exact_and_reports_large_identity(self):
        from ngs.downloads import DownloadQueue
        card = json.loads((Path(__file__).parents[1] / 'examples/black-mesa.json').read_bytes())
        image = dict(path='/assets/ngs_launcher/test.png', size=1, sha256='0'*64)
        card.update(id='snapshot-fixture', revision=1, cover=image, background=image, delivery='on_request')
        queue = DownloadQueue(self.storage)
        self.addCleanup(queue.close)
        job = queue.add(card, self.storage.root/'offline', 'https://nas')
        context, stage = queue.stage(job, create=True)
        (stage/'partial').write_bytes(b'keep unfinished game')
        identity = [2**80+17, 2**120+29]
        queue.update(card['id'], state='paused', stage_identity=identity, completed_bytes=123)
        before = queue.path.read_bytes()
        log = SessionLog(self.storage, 'snapshot-check')
        log.snapshot(); log.pack()
        rows = []
        for path in log.archives.glob('*.zip'):
            with zipfile.ZipFile(path) as archive: rows.extend(json.loads(row) for row in archive.read('events.jsonl').decode().splitlines())
        saved = next(row for row in rows if row['event'] == 'queue.job')
        self.assertEqual(saved['stage_identity'], identity)
        self.assertEqual(saved['state'], 'paused')
        self.assertTrue(next(row for row in rows if row['event'] == 'queue.folder')['registered'])
        self.assertEqual(queue.path.read_bytes(), before)
        self.assertEqual((stage/'partial').read_bytes(), b'keep unfinished game')
        log.finish()

    def test_old_completed_archives_are_pruned_but_foreign_files_and_pending_game_remain(self):
        import os
        data, value = self.archive()
        folder = self.private / self.device
        folder.mkdir()
        old = folder / ('12'*16+'-'+'34'*32+'.zip')
        old.write_bytes(b'old owned diagnostics')
        old.with_suffix('.json').write_text(json.dumps(dict(device=self.device, session='12'*16, sha256='34'*32)))
        os.utime(old, (time.time()-15*86400,)*2)
        foreign = folder/'foreign.zip'; foreign.write_bytes(b'foreign file')
        for offset in range(0, len(data), 1024):
            diagnostic_action(dict(value, action='append', offset=offset, data=base64.b64encode(data[offset:offset+1024]).decode()), self.device)
        diagnostic_action(dict(value, action='finish'), self.device)
        self.assertFalse(old.exists()); self.assertFalse(old.with_suffix('.json').exists())
        self.assertEqual(foreign.read_bytes(), b'foreign file')


if __name__ == '__main__': unittest.main()
