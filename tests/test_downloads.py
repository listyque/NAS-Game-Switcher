import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ngs.catalog import CatalogCache, atomic_write
from ngs.downloads import DownloadQueue
from ngs.install import BLOCK, Cancelled, LocalLibrary, build_manifest, manifest_bytes
from ngs.portable import PortableStorage


class DownloadTests(unittest.TestCase):
    def test_cleanup_outbox_survives_restart_and_lost_reply_without_touching_files(self):
        self.queue.forget_row('sample')
        card = dict(self.card, delivery='on_request')
        card.pop('manifest')
        settings = self.storage.read_settings(); settings['tactic_url'] = self.cache.origin+'/tactic'
        self.storage.write_settings(settings)
        for installers in (False, True):
            selected = dict(card, id='setup-sample', kind='installer', game_id='sample') if installers else card
            job = self.queue.add(selected, self.source, self.cache.origin)
            for suffix in ('plan', 'blocks'):
                (self.storage.state/('stream-'+job['token']+'-'+suffix+'.json')).write_text('fixture')
            self.queue.forget_row(selected['id'])
            self.assertTrue(self.queue.cleanup_due())
            self.assertFalse(list(self.storage.state.glob('stream-'+job['token']+'-*.json')))
        self.queue.close(); self.queue = DownloadQueue(self.storage); self.addCleanup(self.queue.close)
        with patch('ngs.api.MetadataApi') as remote:
            remote.return_value.call.side_effect = ConnectionError('NAS deleted files but reply was lost')
            self.queue.clean_transfers(threading.Event())
            self.assertEqual(remote.return_value.call.call_count, 1)
            self.assertTrue(self.queue.cleanup_due())  # The other completed job remains eligible.
            with patch('ngs.downloads.time.time', return_value=time.time()+120):
                remote.return_value.call.side_effect = None
                remote.return_value.call.return_value = dict(state='released')
                self.queue.clean_transfers(threading.Event())
            actions = [call.args[1]['action'] for call in remote.return_value.call.call_args_list]
            self.assertIn('release', actions); self.assertIn('installer_release', actions)
        self.assertFalse(self.queue.cleanup_due())
        self.assertEqual((self.source/'sample/a.exe').read_bytes(), b'test game')

    def test_pause_never_schedules_server_release_but_cancel_does(self):
        self.queue.forget_row('sample')
        card = dict(self.card, delivery='on_request'); card.pop('manifest')
        settings = self.storage.read_settings(); settings['tactic_url'] = self.cache.origin+'/tactic'
        self.storage.write_settings(settings)
        self.queue.add(card, self.source, self.cache.origin)
        self.queue.set_state('sample', 'paused')
        self.assertFalse(self.queue.cleanup_due())
        self.queue.discard('sample')
        self.assertTrue(self.queue.cleanup_due())

    def test_network_wait_survives_restart_and_manual_pause_does_not_auto_resume(self):
        with patch('ngs.transfer.run_transfer', side_effect=ConnectionResetError('Network lost')):
            with self.assertRaises(ConnectionError): self.queue.run('sample', self.cache, isolated=True)
        job = self.queue.get('sample')
        self.assertEqual(job['state'], 'queued')
        self.assertGreater(job['retry_at'], time.time())
        self.queue.close()
        self.queue = DownloadQueue(self.storage)
        self.addCleanup(self.queue.close)
        self.assertEqual(self.queue.get('sample'), job)
        self.queue.set_state('sample', 'paused')
        with patch('ngs.transfer.run_transfer', side_effect=ValueError('Invalid checksum')):
            with self.assertRaises(ValueError): self.queue.run('sample', self.cache, isolated=True)
        self.assertEqual(self.queue.get('sample')['state'], 'paused')
        self.queue.set_state('sample', 'queued')
        self.assertEqual(self.queue.get('sample')['retry_at'], 0)
        with patch('ngs.transfer.run_transfer', side_effect=PermissionError('Disk access denied')):
            with self.assertRaises(PermissionError): self.queue.run('sample', self.cache, isolated=True)
        self.assertEqual(self.queue.get('sample')['state'], 'error')

    def test_isolated_worker_preserves_network_error_type_for_automatic_retry(self):
        from types import SimpleNamespace
        from ngs.transfer import run_transfer
        def finished_worker(command, **unused):
            folder = Path(command[-1]).parent
            (folder / 'result.json').write_text(json.dumps(dict(status='error', retryable=True, message='Connection lost')))
            return SimpleNamespace(poll=lambda: 1)
        with patch('ngs.transfer.subprocess.Popen', side_effect=finished_worker):
            with self.assertRaises(ConnectionError):
                run_transfer(self.storage, 'sample', self.cache.origin, threading.Event(), lambda *args: None)

    def test_byte_statistics_survive_pause_restart_and_resume(self):
        self.queue.forget_row("sample")
        card = dict(self.card, delivery="on_request")
        card.pop("manifest")
        self.queue.add(card, self.source, self.cache.origin)
        def pause(storage, cache, card, stage, token, cancel, progress):
            progress("download_size", -1, 5000, "")
            progress("io", 345, 0, "")
            progress("unpack", 1000, 5000, "")
            raise Cancelled()
        with patch("ngs.on_demand.download_stream", side_effect=pause):
            with self.assertRaises(Cancelled): self.queue.run("sample", self.cache)
        job = self.queue.get("sample")
        self.assertEqual((job["completed_bytes"], job["total_bytes"], job["network_bytes"], job["network_total"]), (1000, 5000, 345, -1))
        self.queue.close()
        self.queue = DownloadQueue(self.storage)
        self.addCleanup(self.queue.close)
        self.assertEqual(self.queue.get("sample"), job)
        self.queue.set_state("sample", "queued")
        def paused_again(storage, cache, card, stage, token, cancel, progress):
            progress("download_size", -1, 5000, "")
            progress("io", 200, 0, "")
            progress("unpack", 2000, 5000, "")
            raise Cancelled()
        with patch("ngs.on_demand.download_stream", side_effect=paused_again):
            with self.assertRaises(Cancelled): self.queue.run("sample", self.cache)
        resumed = self.queue.get("sample")
        self.assertEqual((resumed["completed_bytes"], resumed["network_bytes"]), (2000, 545))

    def test_installer_download_is_separate_and_recovers_without_registering_a_game(self):
        self.queue.forget_row("sample")
        card = dict(self.card, id="setup-sample", kind="installer", game_id="sample")
        document = build_manifest(self.source, card)
        data = manifest_bytes(document)
        digest = hashlib.sha256(data).hexdigest()
        card["manifest"] = dict(path="/assets/ngs_launcher/setup.json", size=len(data), sha256=digest)
        atomic_write(self.cache.folder / (digest + ".manifest.json"), data)
        self.queue.add(card, self.source, self.cache.origin)
        result = self.queue.run(card["id"], self.cache)
        folder = self.storage.games / ".ngs-installers" / card["name"]
        self.assertTrue((folder / "large.bin").is_file())
        self.assertEqual(LocalLibrary(self.storage).reconcile(), [])
        self.assertEqual(list((self.storage.state / "installs").iterdir()), [])
        installers = LocalLibrary(self.storage, installers=True)
        self.assertEqual([r["card"]["id"] for r in installers.reconcile()], [card["id"]])
        self.assertEqual(installers.launch_spec(card["id"])[0], folder / "a.exe")
        self.queue.add(card, self.source, self.cache.origin)
        self.assertEqual(self.queue.run(card["id"], self.cache)["location"], result["location"])
        self.assertEqual(self.queue.jobs(), [])
        (folder / "a.exe").write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "EXE изменился"):
            installers.launch_spec(card["id"])

    def test_progress_backlog_does_not_delay_pause(self):
        cancel, seen = threading.Event(), []
        original = subprocess.Popen
        def noisy_worker(command, **kwargs):
            path = Path(command[-1]).parent / "events.jsonl"
            path.write_text((json.dumps(["copy", 1, 2, "busy"]) + "\n") * 20000, encoding="utf8")
            return original([sys.executable, "-B", "-c", "import time; time.sleep(30)"], **kwargs)
        def progress(*args):
            seen.append(args)
            if not cancel.is_set():
                self.queue.set_state("sample", "pausing")
                cancel.set()
        start = time.monotonic()
        with patch("ngs.transfer.subprocess.Popen", side_effect=noisy_worker), patch("ngs.transfer.STOP_SECONDS", .1):
            with self.assertRaises(Cancelled):
                self.queue.run("sample", self.cache, cancel, progress, isolated=True)
        self.assertLess(time.monotonic() - start, 5)
        self.assertLessEqual(len(seen), 256)
        self.assertEqual(self.queue.get("sample")["state"], "paused")

    def test_compiled_worker_uses_shipped_exe_even_when_sys_executable_is_missing(self):
        from ngs import transfer
        runtime = self.root / "Moved NGS/runtime"
        with patch.dict(transfer.__dict__, {"__compiled__": True, "__file__": str(runtime / "ngs/transfer.py")}), \
             patch.object(transfer.sys, "executable", str(runtime / "python.exe")):
            self.assertEqual(transfer.worker_command(), [str(runtime / "NGS.exe")])

    def test_isolated_transfer_copies_verifies_and_cleans_its_temporary_files(self):
        phases = set()
        result = self.queue.run("sample", self.cache, isolated=True,
                                progress=lambda phase, *args: phases.add(phase))
        self.assertTrue(result["managed"])
        self.assertEqual(self.queue.jobs(), [])
        self.assertTrue({"prepare", "copy", "verify"} <= phases)
        self.assertEqual(list(self.storage.temp.glob("transfer-*")), [])
        self.assertEqual(hashlib.sha256((self.storage.games / "Sample/large.bin").read_bytes()).hexdigest(),
                         self.manifest["files"][1]["sha256"])

    def test_stalled_process_times_out_without_losing_partial_and_can_resume(self):
        stage = self.pause()
        partial = (stage / "payload/large.bin").stat().st_size
        self.queue.set_state("sample", "queued")
        original, children = subprocess.Popen, []
        def stalled(command, **kwargs):
            child = original([sys.executable, "-B", "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child
        with patch("ngs.transfer.subprocess.Popen", side_effect=stalled), patch("ngs.transfer.STALL_SECONDS", .3):
            with self.assertRaisesRegex(TimeoutError, "Нет прогресса"):
                self.queue.run("sample", self.cache, isolated=True)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.queue.get("sample")["state"], "error")
        self.assertEqual((stage / "payload/large.bin").stat().st_size, partial)
        self.queue.set_state("sample", "queued")
        self.queue.run("sample", self.cache, isolated=True)
        self.assertEqual(self.queue.jobs(), [])

    def test_pause_terminates_a_blocked_process_within_bounded_time(self):
        stage = self.pause()
        partial = (stage / "payload/large.bin").stat().st_size
        self.queue.set_state("sample", "queued")
        original, children, cancel = subprocess.Popen, [], threading.Event()
        def stalled(command, **kwargs):
            child = original([sys.executable, "-B", "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            self.queue.set_state("sample", "pausing")
            cancel.set()
            return child
        start = time.monotonic()
        with patch("ngs.transfer.subprocess.Popen", side_effect=stalled), patch("ngs.transfer.STOP_SECONDS", .1):
            with self.assertRaises(Cancelled):
                self.queue.run("sample", self.cache, cancel=cancel, isolated=True)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.queue.get("sample")["state"], "paused")
        self.assertEqual((stage / "payload/large.bin").stat().st_size, partial)

    def test_device_candidates_find_matching_exe_and_stay_pinned_after_pause_and_restart(self):
        missing, wrong = self.root / "offline NAS", self.root / "wrong NAS"
        (wrong / "sample").mkdir(parents=True)
        (wrong / "sample/a.exe").write_bytes(b"wrong exe")  # Same length, different SHA256.
        self.queue.forget_row("sample")
        card = dict(self.card, source_root=str(self.root / "publisher-only NAS"))
        candidates = [str(missing), str(wrong), str(self.source)]
        self.queue.add(card, missing, self.cache.origin, source_candidates=candidates)
        stage = self.pause()
        partial = (stage / "payload/large.bin").stat().st_size
        job = self.queue.get("sample")
        self.assertEqual(job["source"], str(self.source))
        self.assertEqual(job["source_candidates"], [])
        self.queue.close()
        self.queue = DownloadQueue(PortableStorage(self.storage.root))
        self.queue.set_state("sample", "queued")
        with patch("ngs.downloads.hash_file", side_effect=AssertionError("A pinned root must not be selected again")):
            self.queue.run("sample", self.cache)
        self.assertGreater(partial, 0)
        self.assertEqual((wrong / "sample/a.exe").read_bytes(), b"wrong exe")
        self.assertEqual(self.queue.jobs(), [])
        self.assertTrue((self.storage.games / "Sample/a.exe").is_file())

    def test_missing_candidates_leave_no_payload_and_can_retry(self):
        missing = self.root / "missing NAS"
        self.queue.forget_row("sample")
        self.queue.add(self.card, missing, self.cache.origin,
                       source_candidates=[str(missing), str(self.root / "another missing NAS")])
        with self.assertRaisesRegex(ValueError, "настроенных каталогах NAS"):
            self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.get("sample")["state"], "error")
        self.assertIsNone(self.queue.get("sample")["stage_identity"])
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])
        self.queue.update("sample", state="queued", source_candidates=[str(self.source)])
        self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.jobs(), [])

    def test_pause_during_candidate_selection_preserves_unstarted_job(self):
        self.queue.forget_row("sample")
        candidates = [str(self.source), str(self.root / "other NAS")]
        self.queue.add(self.card, self.source, self.cache.origin, source_candidates=candidates)
        cancel = threading.Event()
        def pause(*args):
            self.queue.set_state("sample", "pausing")
            cancel.set()
            raise Cancelled()
        with patch("ngs.downloads.hash_file", side_effect=pause), self.assertRaises(Cancelled):
            self.queue.run("sample", self.cache, cancel)
        job = self.queue.get("sample")
        self.assertEqual(job["state"], "paused")
        self.assertEqual(job["source_candidates"], candidates)
        self.assertIsNone(job["stage_identity"])
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])
        self.queue.set_state("sample", "queued")
        self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.jobs(), [])

    def test_two_jobs_keep_separate_destinations_and_sources_after_restart_and_default_change(self):
        self.queue.forget_row("sample")
        first, second = self.root / "disk one/Games", self.root / "disk two/Custom Games"
        self.queue.add(self.card, self.source, self.cache.origin, str(first))
        source_two = self.root / "second NAS"
        shutil.copytree(self.source / "sample", source_two / "sample")
        other = dict(self.card, id="another", source_root=str(source_two))
        data = manifest_bytes(build_manifest(source_two, other))
        digest = hashlib.sha256(data).hexdigest()
        other["manifest"] = dict(path="/assets/ngs_launcher/another.json", size=len(data), sha256=digest)
        atomic_write(self.cache.folder / (digest + ".manifest.json"), data)
        self.queue.add(other, source_two, self.cache.origin, str(second))
        self.pause()
        self.storage.set_games_directory(str(self.root / "default changed"))
        self.queue.close()
        self.queue = DownloadQueue(PortableStorage(self.storage.root))
        self.assertEqual(self.queue.get("sample")["source"], str(self.source))
        self.assertEqual(self.queue.get("another")["source"], str(source_two))
        self.queue.set_state("sample", "queued")
        self.queue.run("sample", self.cache)
        self.queue.run("another", self.cache)
        self.assertTrue((first / "sample/a.exe").is_file())
        self.assertTrue((second / "Sample/a.exe").is_file())
        self.assertFalse((self.storage.games / "Sample").exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = PortableStorage(self.root / "launcher")
        self.source = self.root / "nas"
        game = self.source / "sample"
        game.mkdir(parents=True)
        (game / "a.exe").write_bytes(b"test game")
        (game / "large.bin").write_bytes(bytes(range(251)) * (BLOCK * 3 // 251 + 2))
        self.card = json.loads((Path(__file__).resolve().parents[1] / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = {"path": "/assets/ngs_launcher/test.jpg", "sha256": "0" * 64, "size": 1}
        self.card.update(id="sample", name="Sample", source_folder="sample", executable="a.exe", revision=1, cover=image, background=image)
        self.manifest = build_manifest(self.source, self.card)
        data = manifest_bytes(self.manifest)
        digest = hashlib.sha256(data).hexdigest()
        self.card["manifest"] = {"path": "/assets/ngs_launcher/sample.json", "size": len(data), "sha256": digest}
        self.cache = CatalogCache(self.storage, "http://offline-test/tactic")
        atomic_write(self.cache.folder / (digest + ".manifest.json"), data)
        self.queue = DownloadQueue(self.storage)
        self.addCleanup(lambda: self.queue.close())
        self.queue.add(self.card, self.source, self.cache.origin)

    def pause(self):
        cancel = threading.Event()
        def progress(phase, done, total, name):
            if phase == "copy" and name == "large.bin" and done >= BLOCK:
                self.queue.set_state("sample", "pausing")
                cancel.set()
        with self.assertRaises(Cancelled):
            self.queue.run("sample", self.cache, cancel, progress)
        job = self.queue.get("sample")
        self.assertEqual(job["state"], "paused")
        _, stage = self.queue.stage(job)
        return stage

    def test_pause_restart_resumes_at_offset_and_skips_finished_files(self):
        source_before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in (self.source / "sample").iterdir()}
        stage = self.pause()
        partial = (stage / "payload/large.bin").stat().st_size
        self.assertGreater(partial, 0)
        self.assertLess(partial, self.manifest["files"][1]["size"])
        self.assertFalse((self.storage.games / "Sample").exists())
        self.assertEqual(LocalLibrary(self.storage).reconcile(), [])
        self.queue.close()
        self.queue = DownloadQueue(PortableStorage(self.storage.root))
        self.assertEqual(self.queue.get("sample")["state"], "paused")
        self.queue.set_state("sample", "pausing")
        with self.assertRaises(Cancelled): self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.get("sample")["state"], "paused")
        self.queue.set_state("sample", "queued")
        original_open = Path.open
        reads, offsets = [], []
        class Reader:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def seek(self, offset):
                offsets.append(offset)
                return self.stream.seek(offset)
            def read(self, size):
                data = self.stream.read(size)
                reads.append(len(data))
                return data
        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == self.source / "sample/large.bin" and args and args[0] == "rb":
                return Reader(stream)
            if path == self.source / "sample/a.exe":
                self.fail("A fully verified file must not be read again over the network")
            return stream
        with patch.object(Path, "open", opened), patch.object(self.cache, "fetch", side_effect=OSError("offline")):
            receipt = self.queue.run("sample", self.cache)
        self.assertEqual(offsets, [partial])
        self.assertEqual(sum(reads), self.manifest["files"][1]["size"] - partial)
        self.assertTrue(receipt["managed"])
        self.assertEqual(self.queue.jobs(), [])
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])
        self.assertEqual(source_before, {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in (self.source / "sample").iterdir()})

    def test_hard_exit_mid_file_recovers_running_job(self):
        self.queue.close()
        script = """
import os, sys
from pathlib import Path
from ngs.portable import PortableStorage
from ngs.downloads import DownloadQueue
from ngs.catalog import CatalogCache
storage = PortableStorage(Path(sys.argv[1]))
queue = DownloadQueue(storage)
def progress(phase, done, total, name):
    if phase == 'copy' and name == 'large.bin':
        os._exit(73)
queue.run('sample', CatalogCache(storage, 'http://offline-test/tactic'), progress=progress)
"""
        subprocess.run([sys.executable, "-B", "-c", script, str(self.storage.root)], check=False, timeout=20)
        self.queue = DownloadQueue(self.storage)
        self.assertEqual(self.queue.get("sample")["state"], "queued")
        _, stage = self.queue.stage(self.queue.get("sample"))
        self.assertGreater((stage / "payload/large.bin").stat().st_size, 0)
        self.queue.run("sample", self.cache)
        self.assertEqual(len(LocalLibrary(self.storage).reconcile()), 1)

    def test_missing_source_and_space_keep_partial_and_can_retry_new_source(self):
        stage = self.pause()
        partial = (stage / "payload/large.bin").stat().st_size
        self.queue.set_state("sample", "queued")
        with patch("ngs.install.shutil.disk_usage", return_value=type("Space", (), {"free": 1})()):
            with self.assertRaises(OSError): self.queue.run("sample", self.cache)
        self.assertEqual((stage / "payload/large.bin").stat().st_size, partial)
        self.queue.set_state("sample", "queued", source=self.root / "missing source")
        with self.assertRaises(OSError): self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.get("sample")["state"], "error")
        other = self.root / "other source"
        shutil.copytree(self.source, other)
        self.queue.set_state("sample", "queued", source=other)
        self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.jobs(), [])

    def test_corrupt_partial_is_rejected_and_only_that_file_is_repaired(self):
        stage = self.pause()
        target = stage / "payload/large.bin"
        with target.open("r+b") as stream: stream.write(b"bad")
        self.queue.set_state("sample", "queued")
        with self.assertRaises(ValueError): self.queue.run("sample", self.cache)
        self.assertEqual(target.stat().st_size, 0)
        self.assertFalse((self.storage.games / "Sample").exists())
        self.queue.set_state("sample", "queued")
        self.queue.run("sample", self.cache)
        self.assertEqual(hashlib.sha256((self.storage.games / "Sample/large.bin").read_bytes()).hexdigest(), self.manifest["files"][1]["sha256"])

    def test_cancel_discards_only_owned_partial_and_no_duplicates(self):
        self.assertEqual(self.queue.add(self.card, self.source, self.cache.origin)["token"], self.queue.get("sample")["token"])
        stage = self.pause()
        sentinel = self.storage.downloads / "do-not-delete.bin"
        sentinel.write_bytes(b"unrelated")
        self.queue.set_state("sample", "cancelling")
        self.queue.discard("sample")
        self.assertFalse(stage.exists())
        self.assertEqual(sentinel.read_bytes(), b"unrelated")
        self.assertTrue((self.source / "sample/large.bin").is_file())
        self.assertEqual(self.queue.jobs(), [])

    def test_stage_replacement_is_not_adopted_or_deleted(self):
        stage = self.pause()
        self.queue.update("sample", stage_identity=[-1, -1])
        self.queue.set_state("sample", "cancelling")
        with self.assertRaises(ValueError): self.queue.discard("sample")
        self.assertTrue(stage.exists())

    def test_cancel_cannot_be_overwritten_by_a_finishing_pause(self):
        cancel = threading.Event()
        update = self.queue.update
        def race(identity, **fields):
            if fields.get("state") == "paused":
                self.queue.set_state(identity, "cancelling")
            return update(identity, **fields)
        def pause(phase, done, total, name):
            if phase == "copy" and name == "large.bin":
                self.queue.set_state("sample", "pausing")
                cancel.set()
        with patch.object(self.queue, "update", race):
            with self.assertRaises(Cancelled): self.queue.run("sample", self.cache, cancel, pause)
        self.assertEqual(self.queue.jobs(), [])
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])

    def test_cancel_wins_over_concurrent_source_error(self):
        self.pause()
        self.queue.set_state("sample", "queued", source=self.root / "unavailable")
        update = self.queue.update
        def race(identity, **fields):
            if fields.get("state") == "error":
                self.queue.set_state(identity, "cancelling")
            return update(identity, **fields)
        with patch.object(self.queue, "update", race):
            with self.assertRaises(Cancelled): self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.jobs(), [])
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])

    def test_failed_cleanup_preserves_cancellation_intent(self):
        stage = self.pause()
        self.queue.set_state("sample", "cancelling")
        with patch("ngs.downloads.remove_stage", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError): self.queue.discard("sample")
        self.assertEqual(self.queue.get("sample")["state"], "error")
        self.assertTrue(self.queue.get("sample")["discard"])
        self.assertTrue(stage.exists())
        self.queue.set_state("sample", "cancelling")
        self.queue.discard("sample")
        self.assertEqual(self.queue.jobs(), [])

    def test_second_owner_and_starting_a_paused_or_cancelled_job_are_rejected(self):
        with self.assertRaises(OSError): DownloadQueue(self.storage)
        self.queue.set_state("sample", "paused")
        with self.assertRaises(Cancelled): self.queue.run("sample", self.cache)
        self.assertEqual(self.queue.get("sample")["state"], "paused")
        self.queue.set_state("sample", "cancelling")
        with self.assertRaises(Cancelled): self.queue.run("sample", self.cache)
        self.queue.discard("sample")
        self.assertEqual(self.queue.jobs(), [])

    def test_cancel_between_pending_receipt_and_commit_cleans_only_own_state(self):
        from ngs.install import check_cancel
        cancel = threading.Event()
        pending = self.storage.state / "installs/sample.pending.json"
        def checked(event):
            if pending.exists():
                self.queue.set_state("sample", "cancelling")
                cancel.set()
            check_cancel(event)
        with patch("ngs.install.check_cancel", checked):
            with self.assertRaises(Cancelled): self.queue.run("sample", self.cache, cancel)
        self.assertFalse(pending.exists())
        self.assertEqual(self.queue.jobs(), [])
        self.assertFalse((self.storage.games / "Sample").exists())
        self.assertTrue((self.source / "sample/a.exe").exists())

    def test_queue_pins_manifest_destination_and_survives_portable_move(self):
        self.pause()
        changed = dict(self.card, name="New catalog version", revision=2)
        self.queue.add(changed, self.root / "new source", self.cache.origin)
        self.assertEqual(self.queue.get("sample")["card"]["revision"], 1)
        self.queue.close()
        settings = self.storage.read_settings()
        settings["games_directory"] = "new games"
        self.storage.write_settings(settings)
        moved = self.root / "moved launcher"
        self.assertTrue(moved.is_relative_to(self.root) and self.storage.root.is_relative_to(self.root))
        self.storage.root.rename(moved)
        storage = PortableStorage(moved)
        self.queue = DownloadQueue(storage)
        self.queue.set_state("sample", "queued")
        self.queue.run("sample", CatalogCache(storage, "http://offline-test/tactic"))
        self.assertTrue((moved / "games/Sample/a.exe").is_file())
        self.assertEqual(list(storage.games.iterdir()), [])

    def test_crash_after_final_rename_recovers_without_recopy(self):
        from ngs.install import install_game
        self.queue.forget_row("sample")
        self.card["name"] = "A Readable Title"
        self.queue.add(self.card, self.source, self.cache.origin)
        install_game(self.storage, self.source, self.card, self.manifest)
        self.queue.set_state("sample", "running")
        self.queue.close()
        self.queue = DownloadQueue(self.storage)
        with patch.object(self.cache, "fetch", side_effect=OSError("offline")):
            receipt = self.queue.run("sample", self.cache)
        self.assertEqual(receipt["card"]["id"], "sample")
        self.assertEqual(self.queue.jobs(), [])

    def test_recent_launch_order_is_persistent_for_existing_and_managed_games(self):
        from ngs.install import install_game
        install_game(self.storage, self.source, self.card, self.manifest)
        local = self.root / "existing"
        local.mkdir()
        shutil.copyfile(sys.executable, local / "play.exe")
        library = LocalLibrary(self.storage)
        external = library.register_external(local, "play.exe", "Existing")["card"]["id"]
        library.mark_played(external)
        self.assertEqual(library.reconcile()[0]["card"]["id"], external)
        library.mark_played("sample")
        self.assertEqual(LocalLibrary(self.storage).reconcile()[0]["card"]["id"], "sample")


if __name__ == "__main__":
    unittest.main()
