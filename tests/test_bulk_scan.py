import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ngs.bulk_scan import LocalScanJournal, scan_collection, metadata_requests
from ngs.install import Cancelled, LocalLibrary
from ngs.portable import PortableStorage


class BulkScanTests(unittest.TestCase):
    def test_shared_metadata_limits_stop_on_cancel_and_network_failure_uses_offline_cache(self):
        cancel=threading.Event()
        request=metadata_requests(self.storage,self.url,cancel,lambda event:None)
        with patch('ngs.bulk_scan.client_metadata',side_effect=[ConnectionError('Offline'),[]]) as remote:
            with self.assertRaises(ConnectionError):request('search','One')
            self.assertEqual(request('search','Two'),[])
            self.assertTrue(remote.call_args.kwargs['offline'])
        request=metadata_requests(self.storage,self.url,cancel,lambda event:None)
        with patch('ngs.bulk_scan.client_metadata',return_value={}) as remote, patch.object(cancel,'wait',side_effect=lambda seconds:cancel.set()):
            for unused in range(18):request('resolve','12')
            with self.assertRaises(Cancelled):request('resolve','13')
            self.assertEqual(remote.call_count,18)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = PortableStorage(self.root / "NGS")
        self.games = self.root / "Games"
        self.games.mkdir()
        self.url = "http://bulk-test/tactic"
        self.cancel = threading.Event()
        image = dict(path="/assets/ngs_launcher/art.jpg", size=1, sha256="0" * 64)
        self.card = dict(id="known-game", name="Known Game", subtitle="", description="Description", genre="",
                         accent="#d9ff82", source_folder="Known Game", executable="play.exe",
                         metadata_url="https://store.steampowered.com/app/12/", revision=1, cover=image, background=image)

    def game(self, name, exes=("play.exe",)):
        folder = self.games / name
        folder.mkdir()
        data = bytearray(68)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 60, 64)
        data[64:] = b"PE\0\0"
        for name in exes:
            (folder / name).write_bytes(data)
        (folder / "save.dat").write_bytes(b"keep this save")
        return folder

    def test_catalog_matches_import_without_api_and_repeat_skips_manual_edits(self):
        folder = self.game("Known.Game", ("play.exe", "setup.exe"))
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
        with patch("ngs.bulk_scan.client_metadata", side_effect=AssertionError("The catalog is already available")):
            result = scan_collection(self.storage, self.games, [self.card], self.url, self.cancel, lambda event: None)
            self.assertEqual(result["summary"]["added"], 1)
            LocalLibrary(self.storage).register_external(folder, "play.exe", "My manual title", self.card)
            again = scan_collection(self.storage, self.games, [self.card], self.url, self.cancel, lambda event: None)
        self.assertEqual(again["summary"]["already"], 1)
        receipts = LocalLibrary(self.storage).reconcile()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(result["rows"][0]["game_id"], receipts[0]["card"]["id"])
        self.assertEqual(receipts[0]["card"]["name"], "My manual title")
        self.assertIs(receipts[0]["managed"], False)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()})

    def test_new_metadata_is_automatic_but_ambiguous_exes_and_cards_stay_manual(self):
        self.game("New.Game")
        self.game("Two Exes", ("one.exe", "two.exe"))
        self.game("Uncertain Card")
        card = dict(self.card, id="steam-222", name="New Game", source_folder="", executable="", available_on_nas=False)
        def metadata(storage, url, operation, value, cancel, progress=None):
            if operation == "resolve":
                self.assertEqual(value, "222")
                return dict(card=card)
            if value == "New Game":
                return [dict(app_id="222", name="New Game™")]
            return [dict(app_id="333", name="Another edition")]
        with patch("ngs.bulk_scan.client_metadata", side_effect=metadata) as remote:
            result = scan_collection(self.storage, self.games, [], self.url, self.cancel, lambda event: None)
        self.assertEqual(result["summary"], dict(added=1, already=0, manual=2, error=0, total=3))
        self.assertEqual(remote.call_count, 3)
        rows = LocalScanJournal(self.storage, self.url).rows(self.games)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["state"] == "manual" for row in rows), 2)
        self.assertEqual(LocalScanJournal(self.storage, "http://another/tactic").rows(self.games), [])

    def test_cancel_keeps_completed_import_and_resume_does_not_repeat_it(self):
        self.game("Known Game")
        self.game("Later Game")
        def stop(event):
            if event.get("row", {}).get("state") == "added":
                self.cancel.set()
        with self.assertRaises(Cancelled):
            scan_collection(self.storage, self.games, [self.card], self.url, self.cancel, stop)
        self.assertEqual(len(LocalLibrary(self.storage).reconcile()), 1)
        self.cancel.clear()
        with patch("ngs.bulk_scan.client_metadata", return_value=[]):
            result = scan_collection(self.storage, self.games, [self.card], self.url, self.cancel, lambda event: None)
        self.assertEqual(result["summary"]["already"], 1)
        self.assertEqual(result["summary"]["manual"], 1)

    def test_failed_provider_does_not_block_catalog_matches_and_disk_root_is_rejected(self):
        self.game("AAA unknown")
        self.game("Known Game")
        with patch("ngs.bulk_scan.client_metadata", side_effect=RuntimeError("API disconnected")):
            result = scan_collection(self.storage, self.games, [self.card], self.url, self.cancel, lambda event: None)
        self.assertEqual(result["summary"]["added"], 1)
        self.assertEqual(result["summary"]["error"], 1)
        with self.assertRaises(ValueError):
            scan_collection(self.storage, Path(self.root.anchor), [], self.url, self.cancel, lambda event: None)
