"""Windows-only recovery tests using inert fixture files and no app restart.

Run with a Windows Python interpreter. These tests never launch either fixture
NGS.exe and never use the installed application's folders or signed releases.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Actual Windows PowerShell recovery is required')
class UpdateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'data/updates'
        self.base.mkdir(parents=True)
        (self.root / 'data/state').mkdir()
        (self.root / 'data/config').mkdir()
        (self.root / 'runtime').mkdir()
        (self.root / 'NGS.exe').write_bytes(b'inert new outer fixture')
        (self.root / 'runtime/version').write_bytes(b'new runtime fixture')
        self.backup = self.base / 'backup'
        (self.backup / 'runtime').mkdir(parents=True)
        (self.backup / 'NGS.exe').write_bytes(b'inert old outer fixture')
        (self.backup / 'runtime/version').write_bytes(b'old runtime fixture')
        self.profile = self.root / 'data/config/profile.json'
        self.profile.write_bytes(b'{"keep":"player fixture"}')
        self.partial = self.root / 'data/state/incomplete-game.part'
        self.partial.write_bytes(b'keep unfinished game fixture')
        self.marker = dict(schema=1, version='2026.10.21.130826', token='ab' * 16)
        (self.base / 'first-start.json').write_text(json.dumps(self.marker))
        self.script = self.base / 'apply.ps1'
        self.script.write_bytes((Path(__file__).parents[1] / 'tools/update.ps1').read_bytes())

    def apply(self, recover=True):
        executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        args = [str(executable), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', str(self.script), '-Root', str(self.root), '-NoRestart']
        if recover:
            args.append('-Recover')
        process = subprocess.run(args, env=dict(os.environ, NGS_UPDATE_TEST_NO_RESTART='1'),
                                 capture_output=True, timeout=30)
        result_path = self.base / 'result.json'
        self.assertTrue(result_path.exists(), process.stderr.decode(errors='replace'))
        result = json.loads(result_path.read_text(encoding='utf-8-sig'))
        self.assertEqual(self.profile.read_bytes(), b'{"keep":"player fixture"}')
        self.assertEqual(self.partial.read_bytes(), b'keep unfinished game fixture')
        return process, result

    def test_pending_first_start_restores_both_versions_without_restarting(self):
        process, result = self.apply()
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual((self.root / 'NGS.exe').read_bytes(), b'inert old outer fixture')
        self.assertEqual((self.root / 'runtime/version').read_bytes(), b'old runtime fixture')
        self.assertEqual((self.base / 'failed-runtime/version').read_bytes(), b'new runtime fixture')
        self.assertFalse((self.base / 'first-start.json').exists())
        self.assertFalse((self.base / 'transaction.json').exists())

    def test_missing_backup_preserves_pending_and_reports_error(self):
        (self.backup / 'NGS.exe').unlink()
        process, result = self.apply()
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result['status'], 'error')
        self.assertTrue((self.base / 'first-start.json').exists())
        self.assertEqual((self.root / 'runtime/version').read_bytes(), b'new runtime fixture')
        self.assertFalse((self.base / 'failed-runtime').exists())

    def test_partial_recovery_finishes_after_runtime_was_already_restored(self):
        (self.root / 'runtime').rename(self.base / 'failed-runtime')
        (self.backup / 'runtime').rename(self.root / 'runtime')
        (self.base / 'transaction.json').write_text(json.dumps(dict(schema=1, recovery=True)))
        process, result = self.apply()
        self.assertEqual(process.returncode, 0, result)
        self.assertEqual((self.root / 'NGS.exe').read_bytes(), b'inert old outer fixture')
        self.assertEqual((self.root / 'runtime/version').read_bytes(), b'old runtime fixture')
        self.assertFalse((self.base / 'transaction.json').exists())

    def test_interrupted_transaction_without_launcher_backup_never_reports_restored(self):
        (self.backup / 'NGS.exe').unlink()
        (self.base / 'transaction.json').write_text(json.dumps(dict(schema=1, recovery=True)))
        process, result = self.apply()
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result['status'], 'error')
        self.assertTrue((self.base / 'transaction.json').exists())
        self.assertTrue((self.base / 'first-start.json').exists())
        self.assertEqual((self.root / 'runtime/version').read_bytes(), b'new runtime fixture')

    def test_recovery_error_preserves_every_copy_for_retry(self):
        failed = self.base / 'failed-runtime'
        failed.mkdir()
        (failed / 'keep').write_bytes(b'earlier evidence')
        process, result = self.apply()
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result['status'], 'error')
        self.assertTrue((self.base / 'transaction.json').exists())
        self.assertEqual((failed / 'keep').read_bytes(), b'earlier evidence')
        self.assertFalse((failed / 'runtime').exists())
        self.assertEqual((self.root / 'runtime/version').read_bytes(), b'new runtime fixture')
        self.assertEqual((self.backup / 'runtime/version').read_bytes(), b'old runtime fixture')

    def test_second_apply_cannot_roll_back_an_unconfirmed_but_running_version(self):
        process, result = self.apply(recover=False)
        self.assertEqual(process.returncode, 1, result)
        self.assertEqual(result['status'], 'error')
        self.assertTrue((self.base / 'first-start.json').exists())
        self.assertFalse((self.base / 'transaction.json').exists())
        self.assertEqual((self.root / 'runtime/version').read_bytes(), b'new runtime fixture')
        self.assertEqual((self.backup / 'runtime/version').read_bytes(), b'old runtime fixture')


if __name__ == '__main__':
    unittest.main()
