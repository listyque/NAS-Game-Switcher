import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ngs.app import InstallPreviewWorker
from ngs.install import INSTALL_SPACE_RESERVE


class InstallPreviewTests(unittest.TestCase):
    def test_catalog_size_checks_only_local_disk_without_http_or_manifest(self):
        size = 3 * 1024**3
        worker = InstallPreviewWorker(SimpleNamespace(root=Path.cwd()),
            dict(delivery='on_request', total_size=size), None, 'games', 1, None)
        results = []; worker.loaded.connect(results.append)
        with patch('ngs.api.MetadataApi', side_effect=AssertionError('Network used')), \
             patch('ngs.app.fetch_manifest', side_effect=AssertionError('Manifest fetched')), \
             patch('ngs.app.shutil.disk_usage', return_value=SimpleNamespace(free=10 * 1024**3)):
            worker.run()
        self.assertEqual(results[0]['size'], size)
        self.assertTrue(results[0]['can_install'])
        self.assertEqual(results[0]['remaining'], 7 * 1024**3)

    def test_http_install_requires_workspace_for_one_block(self):
        document = {"total_size": 1024, "http": {"chunks": [{"size": 256 * 1024 * 1024}]}}
        worker = InstallPreviewWorker(SimpleNamespace(root=Path.cwd()), {}, None, "games", 1, None)
        results = []; worker.loaded.connect(results.append)
        with patch("ngs.app.fetch_manifest", return_value=document), patch("ngs.app.shutil.disk_usage",
                return_value=SimpleNamespace(free=1024 + INSTALL_SPACE_RESERVE)):
            worker.run()
        self.assertFalse(results[0]["can_install"])

    def test_on_request_reserves_current_and_four_prefetched_packets(self):
        from ngs.packages import PACKAGE_SIZE
        size = 3*1024**3
        workspace = 5*(PACKAGE_SIZE+1024*1024)
        for delta, allowed in ((-1, False), (0, True)):
            worker = InstallPreviewWorker(SimpleNamespace(root=Path.cwd()),
                dict(delivery='on_request', total_size=size), None, 'games', 1, None)
            results = []; worker.loaded.connect(results.append)
            with patch('ngs.app.shutil.disk_usage', return_value=SimpleNamespace(
                    free=size+workspace+INSTALL_SPACE_RESERVE+delta)):
                worker.run()
            self.assertEqual(results[0]['can_install'], allowed)

    def test_size_free_remaining_and_reserve_without_creating_destination(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as name:
            root = Path(name).resolve()
            for free, allowed in ((10 * 1024**3, True), (3 * 1024**3, False),
                                  (3 * 1024**3 + INSTALL_SPACE_RESERVE, True)):
                worker = InstallPreviewWorker(SimpleNamespace(root=root), {}, None, "new/Games", 1, None)
                result = []
                worker.loaded.connect(result.append)
                with patch("ngs.app.fetch_manifest", return_value={"total_size": 3 * 1024**3}), \
                     patch("ngs.app.shutil.disk_usage", return_value=SimpleNamespace(free=free)) as disk:
                    worker.run()
                disk.assert_called_once_with(root)
                self.assertEqual(result[0]["size"], 3 * 1024**3)
                self.assertEqual(result[0]["free"], free)
                self.assertEqual(result[0]["remaining"], free - 3 * 1024**3)
                self.assertEqual(result[0]["can_install"], allowed)
                self.assertFalse((root / "new").exists())

    def test_unavailable_disk_and_manifest_failure_never_allow_download(self):
        worker = InstallPreviewWorker(SimpleNamespace(root=Path.cwd()), {}, None, "games", 1, None)
        result = []
        worker.loaded.connect(result.append)
        with patch("ngs.app.fetch_manifest", return_value={"total_size": 123}), \
             patch("ngs.app.shutil.disk_usage", side_effect=OSError("disconnected")):
            worker.run()
        self.assertEqual(result[0]["size"], 123)
        self.assertNotIn("remaining", result[0])
        self.assertFalse(result[0]["can_install"])
        with patch("ngs.app.fetch_manifest", side_effect=ValueError("bad manifest")):
            worker.run()
        self.assertFalse(result[1]["can_install"])
        self.assertNotIn("size", result[1])
        worker.cancel.set()
        with patch("ngs.app.fetch_manifest", side_effect=ValueError("cancelled")):
            worker.run()
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
