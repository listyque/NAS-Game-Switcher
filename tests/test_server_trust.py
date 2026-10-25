import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ngs.server_trust import server_key_path


class ServerTrustTests(unittest.TestCase):
    def test_explicit_local_pin_is_bound_to_origin(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory);config=root/'config';config.mkdir()
            settings={'tactic_url':'http://my-nas:8890/tactic'}
            storage=SimpleNamespace(config=config,resources=root,read_settings=lambda:settings)
            fallback=root/'assets/update-public-key.bin'
            self.assertEqual(server_key_path(storage),fallback)
            blob=b'RSA1'+b'\0'*279
            (config/'server-public-key.bin').write_bytes(blob)
            trust=dict(schema=1,server='http://my-nas:8890',public=base64.b64encode(blob).decode())
            (config/'server-trust.json').write_text(json.dumps(trust))
            self.assertEqual(server_key_path(storage),config/'server-public-key.bin')
            settings['tactic_url']='http://another-nas:8890/tactic'
            self.assertEqual(server_key_path(storage),fallback)
            settings['tactic_url']='http://my-nas:8890/tactic'
            (config/'server-public-key.bin').write_bytes(b'changed')
            with self.assertRaises(ValueError):server_key_path(storage)

    def test_unknown_fields_and_oversized_file_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory)
            storage=SimpleNamespace(config=root,resources=root,read_settings=lambda:{'tactic_url':'http://nas/tactic'})
            file=root/'server-trust.json'
            for value in [json.dumps(dict(schema=1,server='http://nas',public='',download='http://evil')), ' '*2049]:
                file.write_text(value)
                with self.assertRaises(ValueError):server_key_path(storage)
