import copy
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QColor

from ngs.catalog import CatalogCache, MAX_IMAGE, asset_path, relative_game_path, validate_feed
from ngs.portable import PortableStorage
from tools.tactic_catalog import catalog_action, contract_source


class CatalogTests(unittest.TestCase):
    def test_first_feed_survives_connection_loss_while_downloading_first_picture(self):
        with patch.object(self.cache, 'fetch', side_effect=[json.dumps(self.feed).encode(), ConnectionError('Offline')]):
            with self.assertRaises(ConnectionError): self.cache.refresh()
        self.assertEqual(self.cache.load()[0]['id'], self.card['id'])
        self.assertTrue(self.cache.load()[0]['image'].endswith('/assets/local.svg'))

    def test_missing_picture_never_discards_offline_catalog_and_refresh_repairs_it(self):
        with patch.object(self.cache.opener, "open", side_effect=self.responses()):
            self.cache.refresh()
        self.cache.image_path(self.card['cover']['sha256']).unlink()
        with patch.object(self.cache.opener, "open", side_effect=AssertionError('Offline load must not use network')):
            saved = self.cache.load()
        self.assertEqual(saved[0]['id'], self.card['id'])
        self.assertEqual(saved[0]['description'], self.card['description'])
        self.assertTrue(saved[0]['image'].endswith('/assets/local.svg'))
        with patch.object(self.cache.opener, "open", side_effect=self.responses()):
            repaired = self.cache.refresh()
        self.assertTrue(repaired[0]['image'].endswith('.image'))

    def test_short_reference_and_temporary_http_error_are_retried(self):
        import time, urllib.error
        def response(data, length):
            value = io.BytesIO(data)
            value.headers = {'Content-Length': str(length)}
            return value
        responses = [urllib.error.HTTPError('http://nas', 503, 'Busy', {}, io.BytesIO()),
                     response(b'part', 8), response(b'complete', 8)]
        with patch.object(self.cache.opener, 'open', side_effect=responses) as opened, patch('ngs.http_retry.RETRY_DELAYS', (0, 0, 0)):
            self.assertEqual(self.cache.fetch('/assets/ngs_launcher/block.json', 8192, threading.Event(), time.monotonic() + 10), b'complete')
        self.assertEqual(opened.call_count, 3)

    def test_missing_lookahead_is_optional_but_other_http_errors_are_not_ignored(self):
        import time,urllib.error
        for status in (404,403):
            error=urllib.error.HTTPError('http://nas',status,'Unavailable',{},io.BytesIO())
            with patch.object(self.cache.opener,'open',side_effect=error):
                if status==404:
                    self.assertIsNone(self.cache.fetch('/assets/ngs_launcher/block.json',8192,threading.Event(),time.monotonic()+10,missing_ok=True))
                else:
                    with self.assertRaisesRegex(RuntimeError,'403'):
                        self.cache.fetch('/assets/ngs_launcher/block.json',8192,threading.Event(),time.monotonic()+10,missing_ok=True)

    def test_relative_paths_keep_drive_unc_control_reserved_and_component_guards(self):
        for value in ('game/data/file.bin', 'Игра/звук/123.wem', '...hidden/ok', 'COM0.txt', 'CONsole.exe', 'a'*512):
            with self.subTest(value=value): self.assertEqual(relative_game_path(value), value)
        invalid=['', '/a', '//server/share', 'C:a', 'C:/a', 'a\\b', '.', '..', 'a//b', 'a/',
                 'a/./b', 'a/../b', 'a./b', 'a /b', 'a'*513]
        invalid += ['a'+chr(i)+'b' for i in range(32)]
        invalid += ['a'+c+'b' for c in '\\:<>"|?*']
        invalid += ['dir/'+name+ext+'/file' for name in ('con','CoNIN$','conout$','PRN','AUX','NUL','COM1','LPT9','COM¹','LPT³')
                    for ext in ('','.txt','.a.b')]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError): relative_game_path(value)
        self.assertEqual(relative_game_path('',allow_empty=True),'')

    def test_installer_feed_has_own_cache_and_cannot_replace_the_game_catalog(self):
        installers = CatalogCache(self.storage, self.cache.origin, installers=True)
        card = dict(self.card, kind="installer", game_id=self.card["id"])
        feed = dict(self.feed, games=[card])
        with patch.object(installers, "fetch", return_value=json.dumps(feed).encode()), patch.object(installers, "cache_artwork"):
            self.assertEqual(installers.refresh()[0]["kind"], "installer")
        self.assertNotEqual(installers.index, self.cache.index)
        self.assertFalse(self.cache.index.exists())
        with patch.object(self.cache, "fetch", return_value=json.dumps(feed).encode()):
            with self.assertRaises(ValueError): self.cache.refresh()
        self.assertFalse(self.cache.index.exists())

    def test_installer_kind_and_game_binding_are_validated(self):
        from ngs.catalog import validate_card
        for extra in (dict(kind="other"), dict(kind=True), dict(game_id="game"),
                      dict(kind="installer", game_id="../game"), dict(kind="installer", game_id=123)):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_card(dict(self.card, **extra))

    def test_published_size_survives_validation_and_rejects_invalid_values(self):
        for size in (0, 123, 2**50):
            feed = copy.deepcopy(self.feed)
            feed['games'][0]['total_size'] = size
            self.assertEqual(validate_feed(feed)['games'][0]['total_size'], size)
        for size in (-1, True, '123', 1.5, None, 2**50 + 1):
            feed = copy.deepcopy(self.feed)
            feed['games'][0]['total_size'] = size
            with self.assertRaises(ValueError): validate_feed(feed)

    def test_artwork_reports_download_and_cache_completion_without_duplicate_fetches(self):
        events = []
        with patch.object(self.cache.opener, "open", return_value=io.BytesIO(self.image)) as opened:
            self.cache.cache_artwork(self.card, progress=lambda *args: events.append(args))
        opened.assert_called_once()
        self.assertEqual([event[1] for event in events], [0, 1, 2])
        self.assertTrue(all(event[0] == "metadata_artwork" and event[2] == 2 for event in events))
        events.clear()
        with patch.object(self.cache.opener, "open") as opened:
            self.cache.cache_artwork(self.card, progress=lambda *args: events.append(args))
        opened.assert_not_called()
        self.assertEqual([event[1] for event in events], [1, 2])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.folder.cleanup)
        self.storage = PortableStorage(self.folder.name)
        self.cache = CatalogCache(self.storage, "http://nas:8888/tactic")
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("#ffb169"))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        self.image = bytes(buffer.data())
        self.card = json.loads((Path(__file__).resolve().parents[1] / "examples/black-mesa.json").read_text(encoding="utf8"))
        reference = {"sha256": hashlib.sha256(self.image).hexdigest(), "size": len(self.image),
                     "path": "/assets/ngs_launcher/game/black-mesa/cover/cover_v001.png"}
        self.card.update(revision=1, cover=reference, background=reference.copy())
        self.feed = {"schema": 1, "project": "ngs_launcher", "games": [self.card]}

    def responses(self, feed=None, image=None):
        return [io.BytesIO(json.dumps(self.feed if feed is None else feed).encode()),
                io.BytesIO(self.image if image is None else image)]

    def test_refresh_reuses_images_and_offline_cache_is_portable(self):
        with patch.object(self.cache.opener, "open", side_effect=self.responses()) as opened:
            games = self.cache.refresh()
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(games[0]["id"], "black-mesa")
        self.assertTrue(games[0]["image"].startswith("file:///"))
        self.assertNotIn(str(self.storage.root), self.cache.index.read_text(encoding="utf8"))
        with patch.object(self.cache.opener, "open", side_effect=[self.responses()[0]]) as opened:
            self.assertEqual(self.cache.refresh(), games)
        self.assertEqual(opened.call_count, 1)
        with patch.object(self.cache.opener, "open", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                self.cache.refresh()
            self.assertEqual(self.cache.load(), games)
        moved = Path(self.folder.name) / "moved"
        moved.mkdir()
        (self.storage.root / "data").rename(moved / "data")
        loaded = CatalogCache(PortableStorage(moved), "http://nas:8888/tactic").load()
        self.assertIn("/moved/", loaded[0]["image"])
        self.assertEqual(CatalogCache(PortableStorage(moved), "http://other-nas/tactic").load(), [])

    def test_failed_refresh_preserves_old_catalog_and_empty_publication_clears_it(self):
        with patch.object(self.cache.opener, "open", side_effect=self.responses()):
            old = self.cache.refresh()
        changed = copy.deepcopy(self.feed)
        changed["games"][0]["cover"]["sha256"] = "1" * 64
        before = self.cache.index.read_bytes()
        with patch.object(self.cache.opener, "open", side_effect=self.responses(changed, b"corrupted")):
            with self.assertRaises(ValueError):
                self.cache.refresh()
        self.assertEqual(self.cache.index.read_bytes(), before)
        self.assertEqual(self.cache.load(), old)
        empty = dict(self.feed, games=[])
        with patch.object(self.cache.opener, "open", side_effect=[self.responses(empty)[0]]):
            self.assertEqual(self.cache.refresh(), [])
        self.assertEqual(self.cache.load(), [])

    def test_rejects_untrusted_feed_types_duplicates_and_external_image_urls(self):
        for path in ("https://other/image.jpg", "/assets/open_heavens_cafe/image.jpg", "/assets/ngs_launcher/../secret",
                     "/assets/ngs_launcher/%2e%2e/secret", "/assets/ngs_launcher/a?token=x", "/assets/ngs_launcher/a\n"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                asset_path(path)
        for path in ("../a", "C:/a", "a/../b", "a\\b", "//nas/a", "a/NUL.exe", "a/ads:x", "a/hi?"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_game_path(path)
        for change in ({"name": []}, {"accent": []}, {"revision": True}, {"cover": {"sha256": []}},
                       {"source_folder": "../cafe"}, {"cover": dict(self.card["cover"], size=MAX_IMAGE + 1)}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_feed(dict(self.feed, games=[dict(self.card, **change)]))
        with self.assertRaises(ValueError):
            validate_feed(dict(self.feed, games=[self.card, self.card]))
        with self.assertRaises(ValueError):
            validate_feed(dict(self.feed, project="open_heavens_cafe"))

    def test_digest_valid_but_non_image_and_oversized_responses_are_rejected(self):
        feed = copy.deepcopy(self.feed)
        data = b"this is not a picture"
        feed["games"][0]["cover"].update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        with patch.object(self.cache.opener, "open", side_effect=self.responses(feed, data)):
            with self.assertRaises(ValueError):
                self.cache.refresh()
        self.assertFalse(self.cache.index.exists())
        with patch.object(self.cache.opener, "open", return_value=io.BytesIO(b"x" * 20)):
            with self.assertRaises(ValueError):
                self.cache.fetch("/assets/ngs_launcher/catalog.json", 10, threading.Event(), float("inf"))
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(self.cache.opener, "open") as opened:
            with self.assertRaises(TimeoutError):
                self.cache.refresh(cancelled)
        opened.assert_not_called()

    def test_server_and_client_use_the_same_validation_contract(self):
        namespace = {}
        exec(contract_source(), namespace)
        self.assertEqual(namespace["validate_feed"](self.feed), validate_feed(self.feed))
        with self.assertRaises(ValueError):
            namespace["validate_feed"](dict(self.feed, project="cafe"))

    def test_admin_tool_refuses_other_projects_before_any_write(self):
        def module(name, **attributes):
            obj = ModuleType(name)
            obj.__dict__.update(attributes)
            return obj
        for admin, project in ((False, "ngs_launcher"), (True, "open_heavens_cafe")):
            with self.subTest(admin=admin, project=project):
                protected = SimpleNamespace(get_code=lambda: project)
                env = SimpleNamespace(get_security=lambda: SimpleNamespace(is_admin=lambda: admin))
                modules = {
                    "pyasm": module("pyasm"),
                    "pyasm.biz": module("pyasm.biz", Project=SimpleNamespace(get=lambda: protected), Schema=None, Snapshot=None),
                    "pyasm.common": module("pyasm.common", Environment=env, Container=None),
                    "pyasm.search": module("pyasm.search", Search=None, SearchType=None, DbContainer=None, WidgetDbConfig=None),
                    "tactic_client_lib": module("tactic_client_lib", TacticServerStub=None),
                }
                with patch.dict("sys.modules", modules), self.assertRaises(PermissionError):
                    catalog_action("install", {}, "")


if __name__ == "__main__":
    unittest.main()
