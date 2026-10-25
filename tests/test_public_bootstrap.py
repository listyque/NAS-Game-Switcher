import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from server.container.runtime_update import verify_seed, refresh_plugin, backup_databases

class PublicBootstrapTests(unittest.TestCase):
    def test_retry_and_foreign_database_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';data.mkdir();seed=root/'seed';seed.mkdir()
            (seed/'install.py').write_text('pass')
            manifest=dict(schema=1,project='ngs_launcher',version='5.0.0.a02',kind='clear',personal_data=False,
                          files=[],hashes={'install.py':hashlib.sha256(b'pass').hexdigest()})
            (seed/'manifest.json').write_text(json.dumps(manifest))
            (data/'.initialized').touch()
            with self.assertRaises(ValueError):verify_seed(seed,data)
            (data/'.initialized').unlink()
            self.assertFalse(verify_seed(seed,data))
            (data/'.initialized').touch()
            self.assertFalse(verify_seed(seed,data))
            fingerprint=hashlib.sha256((seed/'manifest.json').read_bytes()).hexdigest()
            (data/'.ngs-migrated').write_text(fingerprint)
            self.assertTrue(verify_seed(seed,data))
            (seed/'install.py').write_text('changed')
            with self.assertRaises(ValueError):verify_seed(seed,data)

    def test_owned_plugin_refresh_and_backup_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.mkdir();target=root/'target';target.mkdir()
            (source/'owned.py').write_text('new');(target/'owned.py').write_text('old')
            (target/'user.data').write_text('keep')
            refresh_plugin(source,target)
            self.assertEqual((target/'owned.py').read_text(),'new')
            self.assertEqual((target/'user.data').read_text(),'keep')
            with patch('server.container.runtime_update.subprocess.run') as run:
                run.return_value.returncode=1
                with self.assertRaises(RuntimeError):backup_databases(root/'backups','private','a'*64)
            self.assertFalse(list((root/'backups').rglob('complete.json')))

if __name__=='__main__':unittest.main()
