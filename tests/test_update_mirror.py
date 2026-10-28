import base64
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Hash import SHA256
from server.plugin.update_mirror import Mirror, verified
from server.plugin import update_transport


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.assets = self.root / 'assets'; self.assets.mkdir()
        self.key = RSA.generate(2048)
        self.public = struct.pack('<6I', 0x31415352, 2048, 3, 256, 0, 0) + self.key.e.to_bytes(3, 'big') + self.key.n.to_bytes(256, 'big')
        self.data = b'signed release package'
        self.release = dict(schema=1, version='2026.10.28.120000', platform='windows-x64',
                            filename='NGS-windows-x64-20261028-120000.zip', size=len(self.data),
                            unpacked_size=100, sha256=hashlib.sha256(self.data).hexdigest(), notes='')
        self.envelope = self.sign(self.release)
        self.mirror = Mirror(self.assets, self.root / 'private', self.public)

    def tearDown(self): self.tmp.cleanup()

    def sign(self, release):
        payload = json.dumps(release)
        return dict(payload=payload, signature=base64.b64encode(pkcs1_15.new(self.key).sign(SHA256.new(payload.encode()))).decode())

    def source(self, data=None):
        source = Mock(origin='https://github.com')
        source.manifest.return_value = self.envelope
        source.package_path.return_value = '/listyque/NAS-Game-Switcher/releases/download/test/package.zip'
        response = io.BytesIO(self.data if data is None else data)
        response.headers = {'Content-Length': str(len(self.data))}
        source.opener.open.return_value = response
        return source

    @unittest.skipUnless(__import__('os').name == 'posix', 'Publication locks are checked on the actual Linux NAS')
    def test_only_verified_complete_package_activates_feed_and_never_downgrades(self):
        self.assertIsNone(self.mirror.envelope())
        lock = Mock()
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=self.source()): self.mirror.run(lock)
        lock.close.assert_called_once()
        self.assertEqual(self.mirror.state()['state'], 'ready')
        self.assertEqual(self.mirror.envelope(), self.envelope)
        self.assertEqual(next(self.mirror.releases.glob('*.zip')).read_bytes(), self.data)
        old = dict(self.release, version='2026.10.27.120000')
        source = self.source(); source.manifest.return_value = self.sign(old)
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=source): self.mirror.run(Mock())
        source.package_path.assert_not_called()
        self.assertEqual(self.mirror.envelope(), self.envelope)

    @unittest.skipUnless(__import__('os').name == 'posix', 'Publication locks are checked on the actual Linux NAS')
    def test_corrupt_download_or_invalid_signature_never_advertises_and_keeps_previous_feed(self):
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=self.source()): self.mirror.run(Mock())
        previous = (self.assets / 'updates.json').read_bytes()
        self.release.update(version='2026.10.29.120000', sha256=hashlib.sha256(b'other expected bytes').hexdigest())
        self.envelope = self.sign(self.release)
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=self.source()): self.mirror.run(Mock())
        self.assertEqual(self.mirror.state()['state'], 'error')
        self.assertEqual((self.assets / 'updates.json').read_bytes(), previous)
        self.assertEqual(len(list(self.mirror.releases.glob('*.zip'))), 1)
        self.assertFalse(list(self.mirror.releases.glob('*.mirror-part')))
        self.envelope['payload'] = json.dumps(dict(self.release, notes='tampered'))
        source = self.source()
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=source): self.mirror.run(Mock())
        source.package_path.assert_not_called()
        self.assertEqual((self.assets / 'updates.json').read_bytes(), previous)

    @unittest.skipUnless(__import__('os').name == 'posix', 'Server flock is checked on the actual Linux NAS')
    def test_daily_manual_and_parallel_requests_share_one_download_and_survive_restart(self):
        started, finish = threading.Event(), threading.Event()
        source = self.source()
        def manifest(): started.set(); finish.wait(5); return self.envelope
        source.manifest.side_effect = manifest
        def wait():
            for unused in range(500):
                state = self.mirror.request(start=False)
                if state['state'] != 'checking': return state
                time.sleep(.01)
            self.fail('Mirror did not finish')
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=source):
            try:
                self.assertEqual(self.mirror.request()['state'], 'checking')
                self.assertTrue(started.wait(2))
                restarted = Mirror(self.assets, self.root / 'private', self.public)
                self.assertEqual(restarted.request(force=True)['state'], 'checking')
                source.manifest.assert_called_once()
                self.assertFalse((self.assets / 'updates.json').exists())
            finally: finish.set()
            self.assertEqual(wait()['state'], 'ready')
            self.assertEqual(restarted.request()['envelope'], self.envelope)
            source.manifest.assert_called_once()
            self.mirror.write_state(state='ready', attempted_at=time.time() - 61)
            self.assertEqual(self.mirror.request(force=True)['state'], 'checking')
            self.assertEqual(wait()['state'], 'ready')
            self.assertEqual(source.manifest.call_count, 2)
            self.mirror.write_state(state='ready', attempted_at=time.time() - 86401)
            self.mirror.request()
            self.assertEqual(wait()['state'], 'ready')
            self.assertEqual(source.manifest.call_count, 3)

    def test_contract_is_generated_from_client_and_rejects_foreign_downloads(self):
        import inspect
        from ngs import updates, github_updates
        self.assertEqual(inspect.getsource(update_transport.validate_release), inspect.getsource(updates.validate_release))
        self.assertEqual(inspect.getsource(update_transport.checked_url), inspect.getsource(github_updates.checked_url))
        self.assertEqual(verified(self.envelope, self.public), self.release)
        with self.assertRaises(ValueError): verified(self.envelope, self.public[:-1])
        with self.assertRaises(ValueError): update_transport.checked_url('https://evil.invalid/package.zip')

    @unittest.skipUnless(__import__('os').name == 'posix', 'Publication locks are checked on the actual Linux NAS')
    def test_transition_published_during_prefetch_is_not_replaced(self):
        transition = self.sign(dict(self.release, version='2026.10.30.120000'))
        download = self.mirror.download
        def publish_during_download(*args):
            download(*args)
            (self.assets/'updates.json').write_text(json.dumps(transition))
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=self.source()), \
             patch.object(self.mirror, 'download', side_effect=publish_during_download):
            self.mirror.run(Mock())
        self.assertEqual(self.mirror.state()['state'], 'ready')
        self.assertEqual(self.mirror.envelope(), transition)

    @unittest.skipUnless(__import__('os').name == 'posix', 'Publication locks are checked on the actual Linux NAS')
    def test_complete_cache_recovers_without_redownload_and_corruption_is_rejected(self):
        target = self.mirror.releases / (self.release['sha256'] + '.zip')
        target.write_bytes(self.data)
        source = self.source()
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=source): self.mirror.run(Mock())
        source.package_path.assert_not_called()
        self.assertEqual(self.mirror.envelope(), self.envelope)
        (self.assets/'updates.json').unlink()
        target.write_bytes(b'corrupt')
        with patch('server.plugin.update_mirror.GitHubReleases', return_value=source): self.mirror.run(Mock())
        self.assertEqual(self.mirror.state()['state'], 'error')
        self.assertFalse((self.assets/'updates.json').exists())
