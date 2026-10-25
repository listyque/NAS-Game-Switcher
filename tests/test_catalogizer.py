import json
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ngs.install import Cancelled
from ngs.portable import PortableStorage
from tools.catalogizer import prepare_import, publish_folder
from tools.catalogizer_state import ScanJournal, scan_all


class CatalogizerTests(unittest.TestCase):
    def test_installer_collection_keeps_each_game_separate_including_a_shared_setup(self):
        card = dict(id="setup-one", kind="installer", game_id="steam-1", name="First game", subtitle="", genre="",
                    accent="#abcdef", description="First description", source_folder="Dilogy", executable="One/Setup.exe",
                    metadata_url="https://store.steampowered.com/app/1/")
        current = dict(card=card, token="first-token", published=card, import_state=dict(provider_id="steam:1"))
        second = dict(card=dict(card, id="setup-two", game_id="steam-2", name="Second game",
                                metadata_url="https://store.steampowered.com/app/2/"), provider_id="steam:2")
        for executable in ("Two/Setup.exe", "One/Setup.exe"):
            result = prepare_import([current], "Dilogy", second, executable)
            self.assertIsNone(result["current"])
            self.assertEqual(result["proposed"]["id"], "setup-two")
            self.assertEqual(result["merged"]["name"], "Second game")
            self.assertEqual(current["card"], card)
        first = dict(card=dict(card, id="new-generated-id"), provider_id="steam:1")
        result = prepare_import([current], "Dilogy", first, "One/Setup.exe")
        self.assertEqual(result["current"]["token"], "first-token")
        self.assertEqual(result["proposed"]["id"], "setup-one")
        corrupted = dict(current, card=dict(card, game_id="steam-2"))
        with self.assertRaisesRegex(ValueError, "другой игре"):
            prepare_import([corrupted], "Dilogy", first, "One/Setup.exe")

    def test_manual_installer_binding_uses_the_selected_exe(self):
        card = dict(id="setup-first", kind="installer", game_id="nas-first", name="First game", subtitle="", genre="",
                    accent="#abcdef", description="", source_folder="Dilogy", executable="One/Setup.exe", metadata_url="")
        current = dict(card=card, import_state=dict(provider_id="manual"))
        details = dict(card=dict(card, id="setup-second", game_id="nas-second"), provider_id="manual")
        self.assertIsNone(prepare_import([current], "Dilogy", details, "Two/Setup.exe")["current"])
        self.assertEqual(prepare_import([current], "Dilogy", details, "One/Setup.exe")["proposed"]["id"], "setup-first")

    def test_manual_cards_do_not_bind_unrelated_folders_or_other_catalog_kinds(self):
        card = dict(id="manual-alpha", name="Alpha", subtitle="", genre="", description="", accent="#abcdef",
                    source_folder="Alpha", executable="setup.exe", metadata_url="")
        current = dict(card=card, import_state=dict(provider_id="manual"))
        details = dict(provider_id="manual", card=dict(card, id="manual-beta", name="Beta"))
        prepared = prepare_import([current], "Beta", details, "setup.exe")
        self.assertIsNone(prepared["current"])
        self.assertEqual(prepared["proposed"]["id"], "manual-beta")
        current["card"] = dict(card, kind="installer", metadata_url="https://example.test/game")
        details["card"]["metadata_url"] = "https://example.test/game"
        self.assertIsNone(prepare_import([current], "Beta", details, "play.exe")["current"])

    def test_new_providers_refresh_old_results_without_another_exe_walk(self):
        from ngs.metadata import METADATA_REVISION
        for folder, status, query in (('Alpha','ready','Alpha'), ('Beta','not_found','Beta old version'), ('Gamma','published','Gamma')):
            self.journal.save(dict(folder=folder,status=status,query=query,executables=[],metadata_revision=1))
        with patch('tools.catalogizer_state.metadata',return_value=[]) as search, patch('tools.catalogizer_state.scan_executables') as exes:
            self.assertEqual(self.run_scan()['scanned'],2)
            exes.assert_not_called()
            self.assertEqual(self.run_scan()['skipped'],3)
            self.assertEqual(search.call_count,3)
        self.assertEqual(self.journal.rows['beta']['query'],'Beta')
        self.assertEqual(self.journal.rows['alpha']['metadata_revision'],METADATA_REVISION)
        self.assertEqual(self.journal.rows['gamma']['metadata_revision'],1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.storage = PortableStorage(self.temp.name)
        self.source = Path(self.temp.name).resolve() / "nas"
        self.source.mkdir()
        for folder in ("Alpha", "Beta", "Gamma"):
            (self.source / folder).mkdir()
        self.journal = ScanJournal(self.storage, self.source, "http://nas/tactic")
        self.events = []

    def run_scan(self, full=False, cancel=None, records=None):
        return scan_all(None, self.journal, records or [], cancel or threading.Event(), self.events.append, full)

    def test_all_folders_are_durable_skipped_after_restart_and_full_scan_repeats(self):
        with patch("tools.catalogizer_state.metadata", return_value=[{"app_id": "12", "name": "Game"}]) as search, \
                patch("tools.catalogizer_state.scan_executables", return_value=dict(
                    candidates=[dict(path="play.exe", utility=False, size=10)], limited=False, skipped=0)) as exes:
            self.assertEqual(self.run_scan(), dict(scanned=3, skipped=0, errors=0, total=3))
            self.journal = ScanJournal(self.storage, self.source, "http://nas/tactic/")
            self.assertEqual(self.run_scan()["skipped"], 3)
            self.assertEqual(search.call_count, 3)
            self.assertEqual(exes.call_count, 3)
            self.assertEqual(self.run_scan(full=True)["scanned"], 3)
            self.assertEqual(search.call_count, 6)
            self.assertEqual(self.journal.rows["alpha"]["executables"][0]["path"], "play.exe")
            (self.source / "Delta").mkdir()
            result = self.run_scan()
            self.assertEqual((result["scanned"], result["skipped"]), (1, 3))

    def test_errors_retry_not_found_is_cached_and_other_origins_have_separate_journals(self):
        with patch("tools.catalogizer_state.metadata", side_effect=[[], OSError("offline"), []]), \
                patch("tools.catalogizer_state.scan_executables", return_value=dict(candidates=[], limited=False, skipped=0)):
            self.assertEqual(self.run_scan()["errors"], 1)
        with patch("tools.catalogizer_state.metadata", return_value=[]) as search, \
                patch("tools.catalogizer_state.scan_executables", return_value=dict(candidates=[], limited=False, skipped=0)):
            self.assertEqual(self.run_scan()["scanned"], 1)
            search.assert_called_once()
        self.assertEqual(ScanJournal(self.storage, self.source, "http://other/tactic").rows, {})
        other = self.source / "other"; other.mkdir()
        self.assertEqual(ScanJournal(self.storage, other, "http://nas/tactic").rows, {})

    def test_cancel_keeps_completed_folder_without_marking_interrupted_one(self):
        cancel = threading.Event()
        def stop_after_first(event):
            if event["kind"] == "row":
                cancel.set()
        with patch("tools.catalogizer_state.metadata", return_value=[]), \
                patch("tools.catalogizer_state.scan_executables", return_value=dict(candidates=[], limited=False, skipped=0)):
            with self.assertRaises(Cancelled):
                scan_all(None, self.journal, [], cancel, stop_after_first)
        self.assertEqual(list(ScanJournal(self.storage, self.source, "http://nas/tactic").rows), ["alpha"])

    def test_existing_published_server_import_is_skipped_but_full_rechecks_it(self):
        records = [dict(card=dict(id="alpha", name="Alpha", source_folder="ALPHA"),
                        available_on_nas=True, published=dict(manifest={"sha256": "a" * 64}))]
        with patch("tools.catalogizer_state.metadata", return_value=[]) as search, \
                patch("tools.catalogizer_state.scan_executables", return_value=dict(candidates=[], limited=False, skipped=0)):
            self.assertEqual(self.run_scan(records=records)["skipped"], 1)
            self.assertEqual(search.call_count, 2)
            self.run_scan(full=True, records=records)
            self.assertEqual(search.call_count, 5)
            self.assertEqual(self.journal.rows["alpha"]["query"], "Alpha")
        self.assertEqual(self.journal.rows["alpha"]["game_id"], "alpha")

    def test_journal_refuses_corruption_and_keeps_original(self):
        self.journal.path.write_text('{"schema":99,"rows":{}}')
        with self.assertRaises(ValueError): ScanJournal(self.storage, self.source, "http://nas/tactic")
        self.assertEqual(json.loads(self.journal.path.read_text())["schema"], 99)

    def test_default_module_entry_opens_window_instead_of_cli_prompt(self):
        from tools.catalogizer import main
        with patch("sys.argv", ["catalogizer"]), patch("tools.catalogizer.PortableStorage") as storage, \
                patch("tools.catalogizer_gui.run", return_value=0) as window, patch("tools.catalogizer.TacticClient") as remote:
            storage.return_value.read_settings.return_value = dict(games_source=str(self.source), tactic_url="http://nas/tactic")
            self.assertEqual(main(), 0)
            window.assert_called_once_with(storage.return_value)
            remote.assert_not_called()

    def test_shared_identity_check_preserves_metadata_only_id_and_rejects_second_folder(self):
        card = dict(id="steam-12", name="Name", subtitle="", genre="", accent="#abcdef", description="Provider",
                    source_folder="", executable="", metadata_url="https://store.steampowered.com/app/12/")
        details = dict(card=card, provider_id="steam:12")
        current = dict(card=dict(card, id="legacy-id", description="Manual"), token="token", published={},
                       import_state={"provider_id": "steam:12"})
        result = prepare_import([current], "Alpha", details, "play.exe")
        self.assertEqual(result["proposed"]["id"], "legacy-id")
        self.assertEqual(result["merged"]["description"], "Manual")
        current["card"]["source_folder"] = "Other"
        with self.assertRaisesRegex(ValueError, "другой папке"):
            prepare_import([current], "Alpha", details, "play.exe")

    def test_publication_cancel_before_commit_and_resume_export_after_commit(self):
        exe = self.source / "Alpha/play.exe"
        data = bytearray(68); data[:2] = b"MZ"; struct.pack_into("<I", data, 60, 64); data[64:] = b"PE\0\0"
        exe.write_bytes(data)
        card = dict(id="steam-12", name="Name", subtitle="", genre="", accent="#abcdef", description="Text",
                    source_folder="", executable="", metadata_url="https://store.steampowered.com/app/12/")
        prepared = prepare_import([], "Alpha", dict(card=card, provider_id="steam:12"), "play.exe")
        cancel = threading.Event()
        def stop(*args): cancel.set()
        with patch("tools.catalogizer.invoke") as remote:
            with self.assertRaises(Cancelled):
                publish_folder(None, self.storage, self.source, prepared, cancel=cancel, progress=stop)
            remote.assert_not_called()
        cancel.clear()
        imported = dict(status="unchanged", game="steam-12", revision=1, card=prepared["proposed"])
        with patch("tools.catalogizer.invoke", side_effect=[imported, {"snapshot": "export"}, {}]) as remote:
            result = publish_folder(None, self.storage, self.source, prepared, cancel=cancel,
                                    before_commit=lambda: cancel.set(), retry_export=True)
            self.assertEqual(result, imported)
            self.assertEqual([c.args[1] for c in remote.call_args_list], ["import", "export", "activate"])


class CatalogizerUiTests(unittest.TestCase):
    def test_published_game_can_prepare_packages_without_metadata_or_exe_rescan(self):
        from PySide6.QtTest import QTest
        from tools.catalogizer_gui import CatalogizerWindow
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            settings = storage.read_settings()
            settings.update(games_source=r"\\NAS\Files\games", tactic_url="http://nas/tactic")
            storage.write_settings(settings)
            window = CatalogizerWindow(storage)
            window.active_source = settings["games_source"]
            window.active_folder = "Sample"
            window.journal.save(dict(folder="Sample", source_root=settings["games_source"], status="published", game_id="sample", name="Sample"))
            window.update_actions()
            self.assertTrue(window.packages.isEnabled())
            self.assertFalse(window.rehash.isVisible())
            record = {"card": {"id": "sample"}, "published": {"name": "Sample"}}
            with patch.object(window, "connect_client", return_value=object()), \
                    patch("tools.catalogizer_gui.invoke", return_value={"records": [record]}), \
                    patch("tools.catalogizer_gui.publish_existing", return_value={"card": {"name": "Sample"}, "chunks": 2}) as prepare:
                window.packages.click()
                deadline = time.monotonic() + 5
                while window.worker and time.monotonic() < deadline: QTest.qWait(10)
                self.assertIsNone(window.worker)
                self.assertEqual(prepare.call_count, 1)
                self.assertIn("HTTP", window.status.text())
            window.close()

    def test_two_nas_roots_keep_same_named_folders_separate_and_skip_both(self):
        from PySide6.QtTest import QTest
        from tools.catalogizer_gui import CatalogizerWindow
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            sources = [Path(root).resolve() / "NAS one", Path(root).resolve() / "NAS two"]
            for source in sources: (source / "Same folder").mkdir(parents=True)
            settings = storage.read_settings()
            settings.update(games_source=str(sources[0]), games_sources=list(map(str, sources)))
            storage.write_settings(settings)
            def wait(window):
                deadline = time.monotonic() + 5
                while (window.worker or window.publication_worker) and time.monotonic() < deadline: QTest.qWait(10)
                self.assertIsNone(window.worker)
                self.assertIsNone(window.publication_worker)
            with patch("tools.catalogizer_gui.TacticClient", return_value=unittest.mock.Mock(ticket="ticket")), \
                    patch("tools.catalogizer_gui.invoke", return_value={"records": []}), \
                    patch("tools.catalogizer_state.metadata", return_value=[]) as search, \
                    patch("tools.catalogizer_state.scan_executables", return_value=dict(candidates=[], limited=False, skipped=0)):
                window = CatalogizerWindow(storage)
                window.scan.click(); wait(window)
                self.assertEqual(window.table.rowCount(), 2)
                self.assertEqual({r["source_root"] for r in window.rows.values()}, set(map(str, sources)))
                window.table.selectRow(1)
                self.assertEqual(window.journal.source, str(sources[1]))
                window.scan.click(); wait(window)
                self.assertEqual(search.call_count, 2)
                window.full.click(); wait(window)
                self.assertEqual(search.call_count, 4)
                window.close()

    @classmethod
    def setUpClass(cls):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QT_QUICK_BACKEND"] = "software"
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_close_is_responsive_and_waits_for_worker_to_stop(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from tools.catalogizer_gui import CatalogizerWindow
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            window = CatalogizerWindow(PortableStorage(root))
            window.show()
            def slow_operation(cancel, emit):
                while not cancel.wait(.005): pass
                raise Cancelled()
            window.launch(slow_operation, lambda result: None)
            QTimer.singleShot(20, window.close)
            deadline = time.monotonic() + 5
            while window.worker and time.monotonic() < deadline: QTest.qWait(10)
            self.assertIsNone(window.worker)
            self.assertFalse(window.isVisible())

    def test_window_batch_scan_cached_restart_explicit_confirmation_and_background_work(self):
        from PySide6.QtTest import QTest
        from tools.catalogizer_gui import CatalogizerWindow
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            source = Path(root).resolve() / "nas"; source.mkdir()
            (source / "Alpha").mkdir()
            settings = storage.read_settings(); settings.update(games_source=str(source), tactic_url="http://nas/tactic")
            storage.write_settings(settings)
            card = dict(id="steam-12", name="Alpha", subtitle="", genre="", accent="#abcdef", description="Text",
                        source_folder="", executable="", metadata_url="https://store.steampowered.com/app/12/")
            import base64, hashlib
            image = b"not an image"
            details = dict(card=card, provider_id="steam:12", cover=dict(
                base64=base64.b64encode(image).decode(), sha256=hashlib.sha256(image).hexdigest()))
            client = unittest.mock.Mock(ticket="in-memory")
            def wait(window):
                deadline = time.monotonic() + 5
                while (window.worker or window.publication_worker) and time.monotonic() < deadline: QTest.qWait(10)
                self.assertIsNone(window.worker)
                self.assertIsNone(window.publication_worker)
            with patch("tools.catalogizer_gui.TacticClient", return_value=client), \
                    patch("tools.catalogizer_gui.invoke", return_value={"records": []}), \
                    patch("tools.catalogizer_state.metadata", return_value=[{"app_id": "12", "name": "Alpha"}]) as search, \
                    patch("tools.catalogizer_state.scan_executables", return_value=dict(candidates=[
                        dict(path="play.exe", size=10, utility=False)], limited=False, skipped=0)), \
                    patch("tools.catalogizer_gui.metadata", return_value=details), \
                    patch("tools.catalogizer_gui.publish_folder", return_value=dict(game="steam-12", card=card)) as publish:
                window = CatalogizerWindow(storage)
                window.show()
                window.scan.click()
                self.assertIsNotNone(window.worker)
                QTest.qWait(1)
                self.assertTrue(window.isVisible())
                wait(window)
                self.assertEqual(window.table.rowCount(), 1)
                window.table.selectRow(0)
                window.load.click(); wait(window)
                self.assertEqual(window.name.text(), "Alpha")
                self.assertTrue(window.publish.isEnabled())
                window.name.setText("Manual title")
                self.assertTrue(window.publish.isEnabled())
                window.name.clear()
                self.assertFalse(window.publish.isEnabled())
                window.name.setText("Manual title")
                window.publish.click(); wait(window)
                self.assertEqual(publish.call_args.args[3]["edits"], {"name": "Manual title"})
                self.assertEqual(next(iter(window.rows.values()))["status"], "published")
                window.close()
                window = CatalogizerWindow(storage)
                window.show()
                self.assertEqual(window.table.rowCount(), 1)
                window.scan.click(); wait(window)
                self.assertEqual(search.call_count, 1)
                window.full.click(); wait(window)
                self.assertEqual(search.call_count, 2)
                window.close()


if __name__ == "__main__":
    unittest.main()
