import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ngs.app import InstallPreviewWorker
from ngs.downloads import DownloadQueue, install_space
from ngs.portable import PortableStorage

GIB = 1024**3


class QueueSpaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = PortableStorage(Path(temporary.name)/'launcher')
        self.queue = DownloadQueue(self.storage)
        self.addCleanup(lambda: self.queue.close())
        self.card = json.loads((Path(__file__).resolve().parents[1]/'examples/black-mesa.json').read_text(encoding='utf8'))
        art = dict(path='/assets/ngs_launcher/fixture.svg', size=1, sha256='0'*64)
        self.card.update(id='first', revision=1, delivery='on_request', cover=art, background=art)
        self.card.pop('manifest', None)
        self.source = str(self.storage.root.parent/'nas')

    def add(self, identity, size, directory=None, workspace=0):
        return self.queue.add(dict(self.card, id=identity), self.source, 'http://offline', directory,
                              space=dict(size=size, workspace=workspace))

    def test_same_volume_reserves_all_directories_and_installer_kinds(self):
        other = self.storage.root.parent/'Other games'
        with patch('ngs.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=20*GIB)):
            self.add('first', 5*GIB)
            self.queue.add(dict(self.card, id='setup-first', kind='installer', game_id='first'),
                           self.source, 'http://offline', str(other), space=dict(size=4*GIB, workspace=0))
            result = install_space(self.storage, self.storage.games, 12*GIB, 0, self.queue)
        self.assertEqual((result['free'], result['reserved'], result['available'], result['remaining']),
                         (20*GIB, 9*GIB, 11*GIB, -GIB))
        self.assertFalse(result['can_install'])

    def test_actual_partial_bytes_release_reserve_not_progress_and_cancel_releases_rest(self):
        self.add('first', 1000)
        job = self.queue.get('first')
        _, stage = self.queue.stage(job, create=True)
        payload = stage/'payload'; payload.mkdir(); (payload/'partial.bin').write_bytes(b'x'*400)
        self.queue.update('first', completed_bytes=1000)
        for state in ('running', 'paused', 'error', 'cancelling'):
            self.queue.set_state('first', state)
            self.assertEqual(self.queue.reserved_space(self.storage.games)[0], 600)
        (payload/'partial.bin').unlink()
        self.assertEqual(self.queue.reserved_space(self.storage.games)[0], 1000)
        self.queue.discard('first')
        self.assertEqual(self.queue.reserved_space(self.storage.games), (0, 0, False))

    def test_estimated_size_and_workspace_survive_restart_without_changing_old_rows(self):
        self.add('first', 6*GIB, workspace=100)
        self.queue.set_state('first', 'paused')
        self.queue.close(); self.queue = DownloadQueue(self.storage)
        job = self.queue.get('first')
        self.assertNotIn('total_size', job['card'])
        self.assertEqual(self.queue.reserved_space(self.storage.games), (6*GIB, 100, False))

    def test_unknown_legacy_job_blocks_new_admission_until_worker_learns_size(self):
        self.queue.add(self.card, self.source, 'http://offline')
        with patch('ngs.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=20*GIB)):
            result = install_space(self.storage, self.storage.games, GIB, 0, self.queue)
            self.assertFalse(result['can_install']); self.assertIn('unknown', result['status'])
            with self.assertRaisesRegex(ValueError, 'unknown'): self.add('second', GIB)
            self.queue.update('first', total_bytes=3*GIB)
            self.assertTrue(install_space(self.storage, self.storage.games, GIB, 0, self.queue)['can_install'])

    def test_separate_volumes_even_with_same_drive_anchor_do_not_share_reservations(self):
        self.add('first', 9*GIB)
        target = self.storage.root.parent/'mounted-volume'/'new-games'
        def volume(path):
            return SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=2 if 'mounted-volume' in str(path) else 1))
        with patch('ngs.downloads.disk_path', side_effect=volume):
            self.assertEqual(self.queue.reserved_space(target), (0, 0, False))

    def test_add_rechecks_queue_and_disk_after_preview_and_does_not_insert_on_failure(self):
        with patch('ngs.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=10*GIB)):
            old_preview = install_space(self.storage, self.storage.games, 6*GIB, 0, self.queue)
            self.assertTrue(old_preview['can_install'])
            self.add('first', 5*GIB)
            with self.assertRaisesRegex(ValueError, 'Not enough'): self.add('second', 6*GIB)
            self.assertEqual([job['card']['id'] for job in self.queue.jobs()], ['first'])
            self.queue.discard('first')
        with patch('ngs.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=5*GIB)):
            with self.assertRaisesRegex(ValueError, 'Not enough'): self.add('second', 6*GIB)
        self.assertEqual(self.queue.jobs(), [])

    def test_fifo_workspace_uses_peak_and_configured_reserve_once(self):
        settings = self.storage.read_settings(); settings['free_space_reserve_gb'] = 2; self.storage.write_settings(settings)
        with patch('ngs.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=20*GIB)):
            self.add('first', 4*GIB, workspace=GIB)
            self.add('second', 4*GIB, workspace=GIB)
        for delta, expected in ((-1, False), (0, True)):
            with patch('ngs.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=15*GIB+delta)):
                result = install_space(self.storage, self.storage.games, 4*GIB, GIB, self.queue)
            self.assertEqual(result['can_install'], expected)
            self.assertEqual(result['reserve'], 2*GIB)

    def test_stage_replacement_and_cancelled_inventory_never_release_reservation(self):
        self.add('first', GIB)
        job = self.queue.get('first'); _, stage = self.queue.stage(job, create=True)
        (stage/'payload').mkdir(); (stage/'payload/a').write_bytes(b'x')
        cancel = threading.Event(); cancel.set()
        from ngs.install import Cancelled
        with self.assertRaises(Cancelled): self.queue.reserved_space(self.storage.games, cancel=cancel)
        self.queue.update('first', stage_identity=[-1,-1])
        with self.assertRaisesRegex(ValueError, 'ownership'): self.queue.reserved_space(self.storage.games)

    def test_unknown_catalog_estimate_is_persisted_when_enqueuing(self):
        worker = InstallPreviewWorker(self.storage, self.card, SimpleNamespace(origin='http://offline'),
                                      str(self.storage.games), 1, None, self.queue)
        results = []; worker.loaded.connect(results.append)
        with patch('ngs.api.MetadataApi') as api, patch('ngs.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=20*GIB)):
            api.return_value.call.return_value = dict(total_size=7*GIB, workspace=0)
            worker.run()
            self.assertTrue(results[0]['can_install'])
            self.queue.add(self.card, self.source, 'http://offline', space=results[0])
        self.assertEqual(self.queue.reserved_space(self.storage.games)[0], 7*GIB)


if __name__ == '__main__': unittest.main()
