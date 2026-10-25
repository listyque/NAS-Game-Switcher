import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ngs.catalog import CatalogCache, MAX_FEED
from ngs.portable import PortableStorage
from tools.handheld_check import memory_mib, seed_catalog


class HandheldCheckTests(unittest.TestCase):
    def test_offline_fixture_loads_1000_cards_within_feed_limit_and_uses_own_cache(self):
        actual = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=actual / "tests") as folder:
            repo = Path(folder)
            for path in ("assets/orbital.svg", "examples/black-mesa.json"):
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(actual / path, target)
            storage = PortableStorage(repo)
            info = seed_catalog(repo, storage, 1000)
            cache = CatalogCache(storage, "http://127.0.0.1:9/tactic")
            cards = cache.load()
            self.assertEqual(len(cards), 1000)
            self.assertEqual(len({card["id"] for card in cards}), 1000)
            self.assertLessEqual(cache.index.stat().st_size, MAX_FEED)
            self.assertEqual(info["artwork"], "demo")
            self.assertTrue(all(card["image"].startswith(storage.cache.as_uri()) for card in cards))
            # A later check can reuse public artwork without touching the source feed.
            before = cache.index.read_bytes()
            other = PortableStorage(repo / "other")
            info = seed_catalog(repo, other, 5)
            self.assertEqual(info["artwork"], "cached")
            self.assertEqual(len(CatalogCache(other, "http://127.0.0.1:9/tactic").load()), 5)
            self.assertEqual(cache.index.read_bytes(), before)

    @unittest.skipUnless(sys.platform == "win32", "Windows process memory")
    def test_memory_counter_reads_current_process(self):
        self.assertGreater(memory_mib(), 0)


if __name__ == "__main__":
    unittest.main()
