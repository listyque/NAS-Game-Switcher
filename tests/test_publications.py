import base64
import hashlib
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from ngs.install import Cancelled
from ngs.metadata import METADATA_REVISION
from ngs.portable import PortableStorage
from tools.catalogizer import prepare_import
from tools.catalogizer_queue import PublicationQueue
from tools.catalogizer_state import ScanJournal


def details(number):
    name = ("Alpha", "Beta", "Gamma")[int(number) - 1]
    image = b"invalid preview is ignored"
    return dict(provider_id="steam:" + str(number), card=dict(
        id="steam-" + str(number), name=name, subtitle="", genre="", accent="#abcdef", description="Description",
        source_folder="", executable="", available_on_nas=False, metadata_url="https://store.steampowered.com/app/" + str(number)),
        cover=dict(base64=base64.b64encode(image).decode(), sha256=hashlib.sha256(image).hexdigest()))


class PublicationQueueTests(unittest.TestCase):
    def test_snapshots_fifo_duplicate_bindings_and_interrupted_commit_need_new_confirmation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            source = str(Path(root).resolve() / "nas")
            queue = PublicationQueue(storage)
            prepared = prepare_import([], "Alpha", details(1), "play.exe", {"name": "Manual"}, source)
            first = queue.add(source, "http://nas/tactic", prepared)
            self.assertEqual(first["name"], "Manual")
            prepared["edits"]["name"] = "Changed after confirmation"
            self.assertEqual(queue.prepared(first["position"])["edits"]["name"], "Manual")
            with self.assertRaisesRegex(ValueError, "уже в очереди"):
                queue.add(source.upper(), "http://nas/tactic/", prepared)
            with self.assertRaises(ValueError):
                queue.add(source, "http://nas/tactic", prepare_import([], "Another", details(1), "play.exe", source=source))
            second = queue.add(source, "http://nas/tactic", prepare_import([], "Beta", details(2), "play.exe", source=source))
            first.update(state="running")
            queue.save(first)
            recovered = PublicationQueue(storage)
            self.assertEqual([j["state"] for j in recovered.jobs()], ["queued", "queued"])
            first.update(pending_export=True)
            recovered.save(first)
            recovered = PublicationQueue(storage)
            first = recovered.jobs()[0]
            self.assertEqual(first["state"], "error")
            with self.assertRaises(ValueError): recovered.remove(first)
            replacement = recovered.add(source, "http://nas/tactic", prepared)
            self.assertEqual([j["position"] for j in recovered.jobs()], [second["position"], replacement["position"]])
            self.assertTrue(replacement["pending_export"])

    def test_separate_roots_completed_payload_cleanup_and_validation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            queue = PublicationQueue(storage)
            for number in (1, 2):
                source = str(Path(root).resolve() / ("NAS " + str(number)))
                prepared = prepare_import([], "Same folder", details(number), "play.exe", source=source)
                queue.add(source, "http://nas/tactic", prepared)
            job = queue.jobs()[0]
            job.update(state="done", journal_pending=True)
            queue.save(job)
            self.assertTrue(PublicationQueue(storage).prepared(job["position"]))
            job["journal_pending"] = False
            queue.save(job)
            with self.assertRaises(ValueError): queue.prepared(job["position"])
            queue.remove(queue.jobs()[1])
            self.assertEqual(len(queue.jobs()), 1)
            for source, url in (("relative", "http://nas/tactic"), (source, "http://admin:secret@nas/tactic")):
                with self.assertRaises(ValueError): queue.add(source, url, prepared)


class PublicationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate):
        from PySide6.QtTest import QTest
        deadline = time.monotonic() + 6
        while not predicate() and time.monotonic() < deadline: QTest.qWait(10)
        self.assertTrue(predicate(), "Background operation did not reach the expected state")

    @contextmanager
    def fixture(self):
        from tools.catalogizer_gui import CatalogizerWindow
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            source = str(Path(root).resolve() / "nas")
            Path(source).mkdir()
            settings = storage.read_settings()
            settings.update(games_source=source, games_sources=[source], tactic_url="http://nas/tactic")
            storage.write_settings(settings)
            journal = ScanJournal(storage, source, settings["tactic_url"])
            for number in (1, 2, 3):
                name = details(number)["card"]["name"]
                journal.save(dict(folder=name, query=name, status="ready", matches=[dict(app_id=str(number), name=name)],
                                  executables=[dict(path="play.exe", utility=False, size=100)], metadata_revision=METADATA_REVISION))
            clients = []
            def client(*args):
                result = Mock(ticket="test session")
                clients.append(result)
                return result
            with patch("tools.catalogizer_gui.TacticClient", side_effect=client), \
                    patch("tools.catalogizer_gui.admin_password", return_value="test password"), \
                    patch("tools.catalogizer_gui.invoke", return_value={"records": []}), \
                    patch("tools.catalogizer_gui.metadata", side_effect=lambda client, op, value, **kwargs: details(value)):
                window = CatalogizerWindow(storage)
                window.show()
                try:
                    yield window, storage, clients
                finally:
                    window.close()
                    self.wait(lambda: window.worker is None and window.publication_worker is None)

    def load(self, window, index):
        window.table.selectRow(index)
        window.load.click()
        self.wait(lambda: window.worker is None)
        self.assertIsNotNone(window.prepared)

    def enqueue(self, window):
        self.assertTrue(window.publish.isEnabled())
        window.publish.click()

    def test_preview_without_exe_and_changing_launch_path_does_not_download_card_again(self):
        with self.fixture() as (window, storage, _), patch('tools.catalogizer_gui.metadata', return_value=details(1)) as lookup:
            window.table.selectRow(0)
            window.exes.clear()
            window.load.click()
            self.wait(lambda: window.worker is None)
            self.assertEqual(window.name.text(),'Alpha')
            self.assertIsNotNone(window.details)
            self.assertFalse(window.publish.isEnabled())
            self.assertFalse(hasattr(window,'confirm'))
            self.assertEqual(window.publication_queue.jobs(),[])
            window.name.setText('Manual title before choosing EXE')
            window.exes.setEditText('bin/play.exe')
            self.assertTrue(window.publish.isEnabled())
            self.assertEqual(window.prepared['proposed']['executable'],'bin/play.exe')
            window.exes.setEditText('../outside.exe')
            self.assertFalse(window.publish.isEnabled())
            self.assertEqual(window.name.text(),'Manual title before choosing EXE')
            window.exes.setEditText('other.exe')
            self.assertTrue(window.publish.isEnabled())
            self.assertEqual(lookup.call_count,1)
            preview = prepare_import([], 'Alpha', details(1), '', source=window.configured_sources[0])
            with self.assertRaises(ValueError):
                window.publication_queue.add(window.configured_sources[0],window.connected_url,preview)

    def test_repeated_preview_reuses_recent_records_but_expires_and_refreshes_after_publication(self):
        with self.fixture() as (window, storage, _), patch('tools.catalogizer_gui.invoke',return_value={'records':[]}) as inspect:
            self.load(window,0)
            window.load.click()
            self.wait(lambda: window.worker is None)
            self.assertEqual(inspect.call_count,1)
            at,url,records=window.record_cache
            window.record_cache=(at-11,url,records)
            window.load.click()
            self.wait(lambda: window.worker is None)
            self.assertEqual(inspect.call_count,2)

    def test_old_soundtrack_result_refreshes_on_selection_without_an_exe_scan(self):
        with self.fixture() as (window, storage, _):
            journal=window.journal
            old=dict(journal.rows['alpha'], metadata_revision=METADATA_REVISION-1,
                     query='TMNT Shredders Revenge',matches=[dict(app_id='3216650',name='Old Soundtrack')])
            journal.save(old)
            window.upsert(dict(old,source_root=journal.source))
            game=dict(app_id='1361510',name="Teenage Mutant Ninja Turtles: Shredder's Revenge")
            with patch('tools.catalogizer_gui.metadata',return_value=[game]) as lookup, \
                    patch('tools.catalogizer_gui.scan_executables') as exes:
                window.table.selectRow(0)
                self.assertNotEqual(window.matches.currentData(),'3216650')
                self.wait(lambda: window.worker is None and window.matches.currentData()=='1361510')
                lookup.assert_called_once_with(window.client,'search','TMNT Shredders Revenge',source=journal.source,folder='Alpha')
                exes.assert_not_called()
                self.assertEqual(journal.rows['alpha']['metadata_revision'],METADATA_REVISION)
                self.assertEqual(window.publication_queue.jobs(),[])

    def test_exe_rescan_and_picker_keep_card_edits_and_reject_files_outside_game(self):
        from PySide6.QtWidgets import QFileDialog
        with self.fixture() as (window, storage, _):
            self.load(window,0)
            window.exes.clear()
            window.name.setText('Keep manual name')
            self.assertIn('Не выбран EXE',window.publish_hint.text())
            empty=dict(candidates=[],limited=False,skipped=0)
            with patch('tools.catalogizer_gui.scan_executables',return_value=empty):
                window.find_exes.click()
                self.wait(lambda: window.worker is None)
            self.assertFalse(window.publish.isEnabled())
            self.assertIn('EXE в папке игры не найден',window.status.text())
            self.assertEqual(window.name.text(),'Keep manual name')
            root=Path(window.journal.source)/'Alpha'
            root.mkdir()
            selected=root/'bin'/'game.exe'
            selected.parent.mkdir()
            selected.write_bytes(b'test fixture')
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(selected),'')):
                window.pick_exe.click()
            self.assertEqual(window.selected_executable(),'bin/game.exe')
            self.assertTrue(window.publish.isEnabled())
            self.assertEqual(window.name.text(),'Keep manual name')
            window.exes.clear()
            window.exes.setEditText('')
            outside=Path(window.journal.source)/'unrelated.exe'
            outside.write_bytes(b'outside fixture')
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(outside),'')):
                window.pick_exe.click()
            self.assertFalse(window.publish.isEnabled())
            self.assertEqual(window.selected_executable(),'')
            self.assertIn('EXE не выбран',window.status.text())
            self.assertTrue(outside.is_file())

    def test_review_next_game_during_fifo_publication_failure_continues_and_edits_stay_frozen(self):
        started, release = threading.Event(), threading.Event()
        order, seen = [], []
        def publish(client, storage, source, prepared, full_hash, cancel, progress, before_commit, retry_export):
            folder = prepared["proposed"]["source_folder"]
            order.append(folder)
            seen.append((client, prepared, full_hash, retry_export))
            if folder == "Alpha":
                started.set()
                while not release.wait(.01):
                    if cancel.is_set(): raise Cancelled()
            before_commit()
            if folder == "Beta": raise OSError("Test export failure")
            card = dict(prepared["proposed"], **prepared["edits"])
            return dict(game=card["id"], card=card, status="imported")
        with self.fixture() as (window, storage, clients), patch("tools.catalogizer_gui.publish_folder", side_effect=publish):
            self.load(window, 0)
            window.name.setText("Manual Alpha")
            window.rehash.setChecked(True)
            self.enqueue(window)
            self.wait(started.is_set)
            self.assertTrue(window.table.isEnabled())
            self.assertFalse(window.scan.isEnabled())
            self.assertFalse(window.load.isEnabled())
            self.load(window, 1)
            window.description.setPlainText("Manual Beta")
            self.enqueue(window)
            self.assertEqual(order, ["Alpha"])
            self.load(window, 2)
            window.name.setText("Gamma in review")
            release.set()
            self.wait(lambda: window.publication_worker is None and len(order) == 2)
            self.assertEqual(window.name.text(), "Gamma in review")
            self.assertIsNotNone(window.prepared)
            self.enqueue(window)
            self.wait(lambda: window.publication_worker is None)
            self.assertEqual(order, ["Alpha", "Beta", "Gamma"])
            self.assertEqual([j["state"] for j in window.publication_queue.jobs()], ["done", "error", "done"])
            self.assertEqual(seen[0][1]["edits"], {"name": "Manual Alpha"})
            self.assertTrue(seen[0][2])
            self.assertEqual(seen[1][1]["edits"], {"description": "Manual Beta"})
            self.assertIsNot(seen[0][0], seen[1][0])
            self.assertIsNot(seen[0][0], clients[0])
            rows = ScanJournal(storage, window.configured_sources[0], window.connected_url).rows
            self.assertEqual(rows["alpha"]["status"], "published")
            self.assertTrue(rows["beta"]["pending_export"])
            self.assertEqual(rows["gamma"]["status"], "published")
            window.queue_table.selectRow(1)
            window.queue_review.click()
            self.wait(lambda: window.worker is None)
            self.assertEqual(window.description.toPlainText(), "Manual Beta")
            self.assertTrue(window.publish.isEnabled())
            self.assertEqual(len(window.publication_queue.jobs()), 3)  # Reviewing never enqueues implicitly.

    def test_pause_close_restore_and_finish_commit_before_exit(self):
        from tools.catalogizer_gui import CatalogizerWindow
        started, committing, finish = threading.Event(), threading.Event(), threading.Event()
        def interrupted(client, storage, source, prepared, full_hash, cancel, progress, before_commit, retry_export):
            started.set()
            cancel.wait(5)
            raise Cancelled()
        with self.fixture() as (window, storage, _), patch("tools.catalogizer_gui.publish_folder", side_effect=interrupted):
            self.load(window, 0)
            self.enqueue(window)
            self.wait(started.is_set)
            self.load(window, 1)
            self.enqueue(window)
            window.queue_pause.click()
            self.wait(lambda: window.publication_worker is None)
            self.assertEqual([j["state"] for j in window.publication_queue.jobs()], ["queued", "queued"])
            window.close()
            restored = CatalogizerWindow(storage)
            restored.show()
            self.assertTrue(restored.publication_paused)
            self.assertIsNone(restored.publication_worker)
            def committed(client, storage, source, prepared, full_hash, cancel, progress, before_commit, retry_export):
                before_commit()
                committing.set()
                finish.wait(5)
                return dict(game=prepared["proposed"]["id"], card=prepared["proposed"], status="imported")
            with patch("tools.catalogizer_gui.publish_folder", side_effect=committed):
                try:
                    restored.queue_pause.click()
                    self.wait(committing.is_set)
                    restored.close()
                    self.assertTrue(restored.isVisible())
                    self.assertIsNotNone(restored.publication_worker)
                    finish.set()
                    self.wait(lambda: restored.publication_worker is None and not restored.isVisible())
                finally:
                    finish.set()
                    restored.close()
                    self.wait(lambda: restored.publication_worker is None)
            jobs = PublicationQueue(storage).jobs()
            self.assertEqual([j["state"] for j in jobs], ["done", "queued"])
            self.assertFalse(jobs[0].get("journal_pending"))
            self.assertFalse(jobs[0]["pending_export"])
            self.assertTrue(PublicationQueue(storage).prepared(jobs[1]["position"]))

    def test_real_file_hashing_and_import_export_run_sequentially_without_writing_sources(self):
        from tools.catalogizer import publish_folder
        from ngs.install import validate_manifest
        calls, manifests = [], []
        def remote(client, action, payload=None):
            calls.append(action)
            if action == "import":
                self.assertIsNone(payload["expected"])
                self.assertIs(payload["confirmed"], True)
                manifest = json.loads(base64.b64decode(payload["manifest"]["base64"]))
                validate_manifest(manifest)
                manifests.append(manifest)
                return dict(game=payload["card"]["id"], card=payload["card"], status="imported")
            if action == "export": return {"snapshot": "test export"}
            self.assertEqual(action, "activate")
            self.assertEqual(payload, {"snapshot": "test export"})
            return {}
        with self.fixture() as (window, storage, _), \
                patch("tools.catalogizer_gui.publish_folder", side_effect=publish_folder), \
                patch("tools.catalogizer.invoke", side_effect=remote):
            original = {}
            for folder in ("Alpha", "Beta"):
                root = Path(window.configured_sources[0]) / folder
                root.mkdir()
                exe = bytearray(68)
                exe[:2] = b"MZ"
                struct.pack_into("<I", exe, 60, 64)
                exe[64:] = b"PE\0\0"
                (root / "play.exe").write_bytes(exe)
                (root / "data.bin").write_bytes((folder * 1000).encode())
                original.update({p: p.read_bytes() for p in root.iterdir()})
            window.publication_paused = True
            for index in (0, 1):
                self.load(window, index)
                self.enqueue(window)
            self.assertEqual(calls, [])
            window.queue_pause.click()
            self.wait(lambda: all(j["state"] == "done" for j in window.publication_queue.jobs()) and window.publication_worker is None)
            self.assertEqual(calls, ["import", "export", "activate"] * 2)
            self.assertEqual([m["source_folder"] for m in manifests], ["Alpha", "Beta"])
            self.assertEqual(len(list(storage.cache.glob('manifest-hashes-*.json'))), 2)
            self.assertTrue(all(p.read_bytes() == content for p, content in original.items()))


if __name__ == "__main__":
    unittest.main()
