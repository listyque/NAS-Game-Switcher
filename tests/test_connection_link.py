import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from ngs.server_trust import connection_link
from server.plugin.connection import connection_panel


class ConnectionLinkTests(unittest.TestCase):
    def test_link_is_only_an_address_and_public_fingerprint(self):
        for server in ('https://nas.example', 'http://192.168.1.2:8890/tactic/', 'https://[::1]:8443'):
            link = server + '#ngs-key=' + 'AB' * 32
            parsed = connection_link(link)
            self.assertTrue(parsed['server'].endswith('/tactic'))
            self.assertEqual(parsed['fingerprint'], 'ab' * 32)

    def test_malformed_links_and_credentials_are_rejected(self):
        valid = 'https://nas.example/tactic/ngs_launcher/#ngs-key=' + 'a' * 64
        links = ['https://nas.example/connect', 'ngs://connect?server=https://nas.example',
                 valid + '&token=secret', valid + '&ngs-key=' + 'b' * 64, 'x' * 2049]
        for server in ('https://user:password@nas.example', 'file:///tmp/key', 'https://nas.example/wrong',
                       'https://nas.example?token=x', 'http://nas.example:99999', 'http://nas.example:\n80'):
            links.append(server + '#ngs-key=' + 'a' * 64)
        for fingerprint in ('', 'f' * 63, 'x' * 64):
            links.append('https://nas.example/#ngs-key=' + fingerprint)
        for link in links:
            with self.subTest(link=link), self.assertRaises(ValueError): connection_link(link)

    def test_admin_panel_reads_only_public_key_and_rejects_other_users_projects_and_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'ngs_launcher').mkdir()
            public = b'RSA1' + b'\x01' * 279
            key = root / 'ngs_launcher/server-public-key.bin'; key.write_bytes(public)
            project, admin = ['ngs_launcher'], [True]
            biz = ModuleType('pyasm.biz'); biz.Project = SimpleNamespace(get=lambda: SimpleNamespace(get_code=lambda: project[0]))
            common = ModuleType('pyasm.common')
            common.Environment = SimpleNamespace(get_security=lambda: SimpleNamespace(is_admin=lambda: admin[0]), get_asset_dir=lambda: root)
            with patch.dict(sys.modules, {'pyasm': ModuleType('pyasm'), 'pyasm.biz': biz, 'pyasm.common': common}):
                panel = connection_panel()
                self.assertIn(hashlib.sha256(public).hexdigest(), panel)
                self.assertIn('/tactic/ngs_launcher/#ngs-key=', panel)
                self.assertNotIn('token=', panel)
                self.assertNotIn('password=', panel)
                admin[0] = False
                with self.assertRaises(PermissionError): connection_panel()
                admin[0] = True; project[0] = 'other'
                with self.assertRaises(PermissionError): connection_panel()
                project[0] = 'ngs_launcher'; key.write_bytes(b'corrupt')
                with self.assertRaises(ValueError): connection_panel()
