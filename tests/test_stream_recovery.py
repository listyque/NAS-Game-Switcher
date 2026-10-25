import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ngs.catalog import atomic_write
from ngs.install import safe_path
from tools.stream_packages import stream_action, stream_worker_alive, release_stream


class StreamRecoveryTests(unittest.TestCase):
    def test_release_is_scoped_and_waits_for_the_producer_lock(self):
        folder = self.folder/'package_jobs'/('package-'+'ab'*16)
        folder.mkdir(parents=True)
        (folder/'worker.lock').touch()
        helpers = dict(safe_path=safe_path)
        with patch('shutil.rmtree.avoids_symlink_attacks', True), patch('shutil.rmtree') as remove:
            self.fcntl.flock.side_effect = BlockingIOError()
            self.assertEqual(release_stream(folder, helpers), dict(state='releasing'))
            remove.assert_not_called()
            self.assertTrue((folder/'cancel').is_file())
            self.fcntl.flock.side_effect = None
            self.assertEqual(release_stream(folder, helpers), dict(state='released'))
            remove.assert_called_once_with(folder)
        for foreign in (folder.parent, self.folder/'open_heavens_cafe', folder.parent/'package-foreign'):
            with self.assertRaises(ValueError): release_stream(foreign, helpers)
        missing = folder.parent/('package-'+'cd'*16)
        self.assertEqual(release_stream(missing, helpers), dict(state='released'))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=Mock())
        module = patch.dict(sys.modules, fcntl=self.fcntl)
        module.start(); self.addCleanup(module.stop)

    def test_liveness_uses_exclusive_lock_and_never_creates_missing_lock(self):
        path = self.folder / 'worker.lock'
        self.assertFalse(stream_worker_alive(path))
        self.assertFalse(path.exists())
        path.touch()
        self.fcntl.flock.side_effect = BlockingIOError()
        self.assertTrue(stream_worker_alive(path))
        self.fcntl.flock.side_effect = None; self.fcntl.flock.reset_mock()
        self.assertFalse(stream_worker_alive(path))
        self.assertEqual([call.args[1] for call in self.fcntl.flock.call_args_list], [6, 8])
        self.fcntl.flock.side_effect = PermissionError()
        with self.assertRaises(PermissionError): stream_worker_alive(path)

    def test_restart_cleans_only_own_unpublished_temp_and_keeps_plan_ack_and_cached_block(self):
        chunks = self.folder / 'chunks'; chunks.mkdir()
        temporary = chunks / '.package-abcdefgh'; temporary.write_bytes(b'partial')
        keep = {self.folder / 'ack': b'2', self.folder / 'plan.json': b'pinned',
                chunks / ('ab'*32 + '.bin'): b'cached', chunks / 'unrelated': b'foreign'}
        for path, data in keep.items(): path.write_bytes(data)
        values = dict(published_data=json.dumps(dict(card={}, launch={})))
        job = SimpleNamespace(get_value=values.get, set_value=lambda key, value: values.update({key:value}),
                              commit=Mock(), get_code=lambda:'test')
        worker = Mock(side_effect=lambda *args: args[-1].close())
        helpers = dict(safe_path=safe_path, atomic_write=atomic_write, stream_worker=worker)
        payload = dict(action='stream_build', private='fixture', certificate={})
        # Run synchronously so this fixture can inspect the real transferred lock handle.
        class Thread:
            def __init__(self, target, args, **kwargs): self.target, self.args = target, args
            def start(self): self.target(*self.args)
        with patch('threading.Thread', Thread):
            self.assertEqual(stream_action(job, payload, self.folder, helpers)['state'], 'running')
        self.assertFalse(temporary.exists())
        for path, data in keep.items(): self.assertEqual(path.read_bytes(), data)
        self.assertTrue(worker.call_args.args[-1].closed)
        # A failed start must release its lock for the next request.
        opened, original = [], Path.open
        def capture(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == 'worker.lock': opened.append(result)
            return result
        with patch.object(Path, 'open', capture), patch('threading.Thread.start', side_effect=RuntimeError('start failed')):
            with self.assertRaisesRegex(RuntimeError, 'start failed'): stream_action(job, payload, self.folder, helpers)
        self.assertTrue(opened[-1].closed)
        for path, data in keep.items(): self.assertEqual(path.read_bytes(), data)
        temporary = chunks / '.package-foreign-file'; temporary.write_bytes(b'foreign')
        with patch.object(Path, 'open', capture):
            with self.assertRaisesRegex(ValueError, 'Foreign temporary'): stream_action(job, payload, self.folder, helpers)
        self.assertTrue(opened[-1].closed)
        self.assertEqual(temporary.read_bytes(), b'foreign')


if __name__ == '__main__': unittest.main()
