import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ngs.portable import PortableStorage
from ngs.update_health import CLOSE_EVENT_VARIABLE, FirstStartHealth, watch_first_start


class UpdateHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = PortableStorage(self.root, resources=self.root / 'runtime')
        self.storage.resources.mkdir()
        self.version = '2026.10.21.130826'
        (self.storage.resources / 'release.json').write_text(json.dumps(dict(version=self.version)))
        self.base = self.storage.data / 'updates'
        self.base.mkdir()
        self.pending = self.base / 'first-start.json'
        self.marker = dict(schema=1, version=self.version, token='ab' * 16)
        self.now = 0

    def health(self):
        self.pending.write_text(json.dumps(self.marker))
        return FirstStartHealth(self.storage, clock=lambda: self.now)

    def test_later_start_does_not_create_timer_or_modify_update_files(self):
        app, window = Mock(), Mock()
        self.assertIsNone(watch_first_start(self.storage, window, app))
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertFalse(window.mock_calls)
        self.assertFalse(app.mock_calls)

    def test_hidden_probe_or_elapsed_time_without_first_frame_cannot_confirm(self):
        health = self.health()
        self.now = 120
        self.assertFalse(health.confirm())
        self.assertTrue(self.pending.exists())
        self.assertFalse(health.confirmed.exists())

    def test_real_frame_grace_and_readiness_are_all_required(self):
        health = self.health()
        self.now = 100
        health.first_frame()
        self.now = 105
        health.first_frame()  # Further frames must not restart the grace period.
        self.assertFalse(health.confirm())
        self.now = 110
        self.assertFalse(health.confirm(False))
        self.assertTrue(health.confirm(True))
        self.assertFalse(self.pending.exists())
        self.assertEqual(json.loads(health.confirmed.read_text()), self.marker)
        self.assertEqual(json.loads((self.base / 'result.json').read_text())['status'], 'installed')
        self.assertFalse(health.confirm())

    def test_deliberate_close_defers_without_accepting_and_checks_current_identity(self):
        health = self.health()
        with patch('ngs.update_health._signal_orderly_close', return_value=True) as notify:
            self.assertTrue(health.intentional_close())
            notify.assert_called_once_with()
        self.assertEqual(json.loads(self.pending.read_text()), self.marker)
        self.assertFalse(health.confirmed.exists())
        self.pending.write_text(json.dumps(dict(self.marker, token='cd' * 16)))
        with patch('ngs.update_health._signal_orderly_close') as notify:
            with self.assertRaisesRegex(ValueError, 'transaction changed'):
                health.intentional_close()
            notify.assert_not_called()

    def test_native_close_event_uses_pointer_sized_handle_and_closes_it(self):
        from ctypes import wintypes
        from ngs.update_health import _signal_orderly_close
        api = Mock()
        api.OpenEventW.return_value = 2 ** 40 + 17
        api.SetEvent.return_value = True
        name = r'Local\NGS.FirstStart.1234.56789'
        with patch.dict(os.environ, {CLOSE_EVENT_VARIABLE: name}), \
                patch('ngs.update_health.sys.platform', 'win32'), \
                patch('ctypes.WinDLL', return_value=api, create=True):
            self.assertTrue(_signal_orderly_close())
        self.assertIs(api.OpenEventW.restype, wintypes.HANDLE)
        api.OpenEventW.assert_called_once_with(0x0002, False, name)
        api.SetEvent.assert_called_once_with(2 ** 40 + 17)
        api.CloseHandle.assert_called_once_with(2 ** 40 + 17)

    def test_invalid_inherited_event_name_is_not_opened(self):
        from ngs.update_health import _signal_orderly_close
        with patch.dict(os.environ, {CLOSE_EVENT_VARIABLE: r'Global\unrelated'}), \
                patch('ngs.update_health.sys.platform', 'win32'), \
                patch('ctypes.WinDLL', create=True) as dll:
            self.assertFalse(_signal_orderly_close())
            dll.assert_not_called()

    def test_other_runtime_version_never_accepts_pending_install(self):
        self.marker['version'] = '2026.10.20.130826'
        with self.assertRaisesRegex(ValueError, 'version'):
            self.health()
        self.assertTrue(self.pending.exists())

    def test_replaced_transaction_is_preserved_not_acknowledged(self):
        health = self.health()
        health.first_frame()
        self.now = 10
        later = dict(self.marker, token='cd' * 16)
        self.pending.write_text(json.dumps(later))
        with self.assertRaisesRegex(ValueError, 'transaction changed'):
            health.confirm()
        self.assertEqual(json.loads(self.pending.read_text()), later)
        self.assertFalse(health.confirmed.exists())

    def test_invalid_markers_are_preserved(self):
        for changes in (dict(schema=True), dict(token='../outside'), dict(extra=True)):
            with self.subTest(changes=changes):
                marker = dict(self.marker, **changes)
                self.pending.write_text(json.dumps(marker))
                with self.assertRaises(ValueError):
                    FirstStartHealth(self.storage)
                self.assertEqual(json.loads(self.pending.read_text()), marker)

    def test_health_commit_survives_status_file_io_failure(self):
        health = self.health()
        health.first_frame()
        self.now = 10
        with self.assertLogs(level='ERROR'), \
                patch('ngs.update_health.atomic_write', side_effect=OSError('disk unavailable')):
            self.assertTrue(health.confirm())
        self.assertFalse(self.pending.exists())
        self.assertTrue(health.confirmed.is_file())

    def test_redirected_marker_is_refused_without_touching_target(self):
        target = self.root / 'foreign.json'
        target.write_text(json.dumps(self.marker))
        try:
            self.pending.symlink_to(target)
        except OSError:
            self.skipTest('Creating a symbolic link is not permitted')
        with self.assertRaises(ValueError):
            FirstStartHealth(self.storage)
        self.assertEqual(json.loads(target.read_text()), self.marker)

    def test_new_update_cannot_delete_backup_of_an_unconfirmed_launch(self):
        from ngs.updates import prepare_update
        assets = self.storage.resources / 'assets'
        assets.mkdir()
        (assets / 'update-public-key.bin').write_bytes(b'fixture')
        self.pending.write_text(json.dumps(self.marker))
        (self.base / 'owner.json').write_text(json.dumps(dict(schema=1, application='NAS Game Switcher updater')))
        backup = self.base / 'backup'
        backup.mkdir()
        (backup / 'keep').write_bytes(b'previous runtime')
        with patch('ngs.updates.verify_envelope', return_value=dict(version='2026.10.22.130826')), \
                patch('ngs.updates.clean_directory') as clean:
            with self.assertRaisesRegex(ValueError, 'не подтверждён'):
                prepare_update(self.storage, {}, Mock(), base_url='http://127.0.0.1/tactic')
            clean.assert_not_called()
        self.assertEqual((backup / 'keep').read_bytes(), b'previous runtime')


if __name__ == '__main__':
    unittest.main()
