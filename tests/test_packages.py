import base64
import copy
import hashlib
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch, Mock

from ngs import install
from ngs.catalog import CatalogCache, atomic_write
from ngs.downloads import DownloadQueue
from ngs.packages import download_chunk, transport_payload, verify_transport, package_key_payload
from ngs.portable import PortableStorage
from ngs.update_crypto import RsaKey
from tools.http_packages import build_packages, helper_source, server_source, package_action


class PackageTests(unittest.TestCase):
    def test_writeback_hint_failure_keeps_fsync_and_never_publishes_on_disk_failure(self):
        import ctypes
        import os
        (self.source / "Sample/large.bin").write_bytes(b"x" * (9 * 1024**2))
        namespace = {}; exec(helper_source(), namespace)
        class Encoder:
            def __init__(self, output): self.output = output
            def write(self, data): self.output.write(data)
            def finish(self): pass
            def close(self): pass
        namespace['zstd_writer'] = Encoder
        for hint_result, disk_failure in ((0, False), (-1, False), (-1, True)):
            with self.subTest(hint_result=hint_result, disk_failure=disk_failure):
                events = []
                hint = Mock(side_effect=lambda *args: events.append('hint') or hint_result)
                def synced(fd):
                    events.append('fsync')
                    if disk_failure: raise OSError('disk failure')
                    os.fsync(fd)
                fake_os = SimpleNamespace(name='posix', link=os.link, fsync=synced, fstat=os.fstat)
                folder = self.root / ('hint-%d-%s' % (hint_result, disk_failure)); folder.mkdir()
                def pipeline(event, value):
                    if event != 'plan': events.append(event)
                with patch.dict(sys.modules, os=fake_os), patch.object(ctypes, 'CDLL', return_value=SimpleNamespace(sync_file_range=hint)):
                    if disk_failure:
                        with self.assertRaisesRegex(OSError, 'disk failure'):
                            build_packages(self.source, folder, self.card, {}, namespace, chunk_size=16*1024**2, pipeline=pipeline)
                    else:
                        build_packages(self.source, folder, self.card, {}, namespace, chunk_size=16*1024**2, pipeline=pipeline)
                self.assertEqual(events, ['hint', 'fsync'] + ([] if disk_failure else ['capacity', 'chunk']))
                self.assertFalse(list((folder / 'chunks').glob('.package-*')))
                if disk_failure: self.assertFalse(list((folder / 'chunks').glob('*.bin')))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = PortableStorage(self.root / "launcher")
        self.source = self.root / "nas"
        folder = self.source / "Sample"
        (folder / "nested").mkdir(parents=True)
        (folder / "empty-directory").mkdir()
        exe = bytearray(128)
        exe[:2] = b"MZ"; struct.pack_into("<I", exe, 60, 64); exe[64:68] = b"PE\0\0"
        (folder / "a.exe").write_bytes(exe)
        (folder / "b.bin").write_bytes(bytes(range(251)) * 2100)
        (folder / "nested/c.bin").write_bytes(b"last file")
        (folder / "zero").touch()
        (folder / "@eaDir").mkdir(); (folder / "@eaDir/ignored").write_bytes(b"NAS metadata")
        self.assets = self.root / "assets"; self.assets.mkdir()
        image = dict(path="/assets/ngs_launcher/test.jpg", sha256="0" * 64, size=1)
        self.card = dict(id="sample", name="Sample", subtitle="", description="", genre="", accent="#abcdef",
                         source_folder="Sample", executable="a.exe", metadata_url="", revision=1, cover=image, background=image)
        namespace = {}; exec(helper_source(), namespace)
        result = build_packages(self.source, self.assets, self.card, {}, namespace, chunk_size=128 * 1024)
        self.document = result["document"]
        with RsaKey() as key:
            (self.storage.root / "assets").mkdir()
            self.public = key.export()
            (self.storage.root / "assets/update-public-key.bin").write_bytes(self.public)
            result["transport"]["signature"] = base64.b64encode(key.sign(transport_payload(result["transport"]))).decode()
        self.document["http"] = result["transport"]
        self.mode, self.requests, self.drops = "normal", [], 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Range")))
                if owner.mode == "redirect":
                    self.send_response(302); self.send_header("Location", "/outside"); self.end_headers(); return
                path = owner.assets / "packages" / self.path.rsplit("/", 1)[-1]
                if not path.is_file():
                    self.send_error(404); return
                data = path.read_bytes()
                offset = int(self.headers["Range"].split("=")[1].split("-")[0]) if self.headers.get("Range") else 0
                partial = offset and owner.mode != "ignore-range"
                self.send_response(206 if partial else 200)
                body = data[offset:] if partial else data
                if partial:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (offset, len(data)-1, len(data)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if owner.mode == "corrupt": body = bytes([body[0] ^ 255]) + body[1:]
                truncate = owner.mode == 'truncate' or owner.mode == 'drop-twice' and owner.drops < 2
                if truncate:
                    body = body[:len(body)//2]
                    owner.drops += 1
                self.wfile.write(body)
                if truncate: self.connection.shutdown(socket.SHUT_WR)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cache = CatalogCache(self.storage, "http://127.0.0.1:%d/tactic" % self.server.server_port)
        data = install.manifest_bytes(self.document)
        digest = hashlib.sha256(data).hexdigest()
        self.card["manifest"] = dict(path="/assets/ngs_launcher/sample.json", size=len(data), sha256=digest)
        atomic_write(self.cache.folder / (digest + ".manifest.json"), data)
        self.queue = DownloadQueue(self.storage); self.addCleanup(self.queue.close)
        self.queue.add(self.card, self.root / "unavailable SMB", self.cache.origin,
                       source_candidates=[str(self.root / "unavailable SMB"), str(self.root / "also unavailable")])

    def test_signed_blocks_install_without_smb_and_preserve_source(self):
        verify_transport(self.document, self.public)
        _, stage = self.queue.stage(self.queue.get("sample"), create=True)
        (stage / "payload").mkdir()
        (stage / "payload/zero").write_bytes(b"corrupt saved zero-length file")
        receipt = self.queue.run("sample", self.cache)
        self.assertTrue(receipt["managed"])
        self.assertEqual(self.queue.jobs(), [])
        for item in self.document["files"]:
            self.assertEqual((self.storage.games / "Sample" / item["path"]).read_bytes(),
                             (self.source / "Sample" / item["path"]).read_bytes())
        self.assertFalse((self.storage.games / "Sample/@eaDir").exists())
        self.assertTrue((self.storage.games / "Sample/empty-directory").is_dir())
        self.assertEqual(len(self.requests), len(self.document["http"]["chunks"]))
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])

    def test_range_resume_and_pause_preserve_owned_partial(self):
        job = self.queue.get("sample")
        context, stage = self.queue.stage(job, create=True)
        folder = stage / "http"; folder.mkdir()
        chunk = self.document["http"]["chunks"][0]
        raw = (self.assets / "packages" / (chunk["sha256"] + ".bin")).read_bytes()
        part = folder / (chunk["sha256"] + ".part"); part.write_bytes(raw[:12345])
        cancel = threading.Event()
        def stop(phase, *args):
            if phase == "copy": cancel.set()
        with self.assertRaises(install.Cancelled): self.queue.run("sample", self.cache, cancel, stop)
        self.assertEqual(self.requests[0][1], "bytes=12345-")
        self.assertGreater(part.stat().st_size, 12345)
        self.assertEqual(part.read_bytes(), raw[:part.stat().st_size])
        self.assertEqual(self.queue.get("sample")["state"], "paused")
        self.queue.set_state("sample", "queued")
        self.queue.run("sample", self.cache)
        self.assertTrue((self.storage.games / "Sample/a.exe").is_file())

    def test_connection_drops_resume_automatically_from_each_saved_offset(self):
        self.mode = 'drop-twice'
        chunk = self.document['http']['chunks'][0]
        target = self.storage.temp / 'retry.part'
        events, received = [], []
        with patch('ngs.http_retry.RETRY_DELAYS', (0, 0, 0)):
            download_chunk(self.cache, chunk, target, threading.Event(), lambda *args: events.append(args), received=received.append)
        first = chunk['size'] // 2
        second = first + (chunk['size'] - first) // 2
        self.assertEqual([value for path, value in self.requests], [None, 'bytes=%d-' % first, 'bytes=%d-' % second])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), chunk['sha256'])
        self.assertEqual(sum(received), chunk['size'])
        self.assertEqual(len([event for event in events if event[0] == 'waiting']), 2)

    def test_corruption_redirect_and_ignored_range_do_not_install(self):
        chunk = self.document["http"]["chunks"][0]
        target = self.storage.temp / "test.part"
        prefix = (self.assets / "packages" / (chunk["sha256"] + ".bin")).read_bytes()[:40]
        for mode in ("corrupt", "redirect", "ignore-range", "truncate"):
            with self.subTest(mode=mode):
                self.mode = mode
                if target.exists(): target.unlink()
                if mode == "ignore-range": target.write_bytes(prefix)
                with self.assertRaises((OSError, ValueError, RuntimeError)):
                    download_chunk(self.cache, chunk, target, threading.Event(), lambda *args: None)
                self.assertFalse((self.storage.games / "Sample").exists())
                if mode == "corrupt": self.assertEqual(target.stat().st_size, 0)
                if mode == "ignore-range": self.assertEqual(target.read_bytes(), prefix)
                self.assertFalse(any(path == "/outside" for path, _ in self.requests))
        self.mode = "normal"
        if target.exists(): target.unlink()
        target.touch()
        download_chunk(self.cache, chunk, target, threading.Event(), lambda *args: None)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), chunk["sha256"])

    def test_unsigned_changed_paths_and_changed_core_are_rejected(self):
        for change in ("signature", "path", "files"):
            document = copy.deepcopy(self.document)
            if change == "signature": document["http"]["signature"] = base64.b64encode(b"x" * 256).decode()
            if change == "path": document["http"]["chunks"][0]["path"] = "/assets/other/game.bin"
            if change == "files": document["files"][0]["sha256"] = "a" * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_transport(document, self.public)
        with RsaKey() as other, self.assertRaises(ValueError): verify_transport(self.document, other.export())
        cache = CatalogCache(self.storage, "http://different-server/tactic")
        with self.assertRaisesRegex(ValueError, "Сервер загрузки"):
            self.queue.run("sample", cache)
        self.assertIsNone(self.queue.get("sample")["stage_identity"])
        self.queue.update("sample", state="queued", games=str(self.source), source=str(self.source), source_candidates=[])
        with self.assertRaisesRegex(ValueError, "не должны пересекаться"):
            self.queue.run("sample", self.cache)
        self.assertIsNone(self.queue.get("sample")["stage_identity"])
        self.assertTrue((self.source / "Sample/a.exe").is_file())

    def test_cancel_removes_only_own_staging_and_space_accounts_for_block(self):
        self.mode = "corrupt"
        with self.assertRaises(ValueError): self.queue.run("sample", self.cache)
        job = self.queue.get("sample")
        _, stage = self.queue.stage(job)
        foreign = self.storage.game_staging / "old-unrelated"; foreign.mkdir()
        self.queue.discard("sample")
        self.assertFalse(stage.exists()); self.assertTrue(foreign.exists())
        self.assertTrue((self.source / "Sample/a.exe").is_file())
        self.queue.add(self.card, self.root / "offline", self.cache.origin)
        enough_without_block = self.document["total_size"] + install.INSTALL_SPACE_RESERVE
        with patch("ngs.install.shutil.disk_usage", return_value=SimpleNamespace(free=enough_without_block)):
            with self.assertRaises(OSError): self.queue.run("sample", self.cache)

    def test_isolated_http_worker_uses_signed_manifest_and_origin(self):
        runtime = self.storage.root / "runtime"
        (runtime / "assets").mkdir(parents=True)
        (runtime / "assets/update-public-key.bin").write_bytes(self.public)
        self.storage.resources = runtime
        self.queue.run("sample", self.cache, isolated=True)
        self.assertTrue((self.storage.games / "Sample/b.bin").is_file())
        self.assertEqual(self.queue.jobs(), [])
        self.assertEqual(list(self.storage.temp.glob("transfer-*")), [])

    def test_packaging_rejects_change_and_touches_only_its_temporary_file(self):
        namespace = {}; exec(helper_source(), namespace)
        stop = threading.Event()
        def advanced(*args): stop.set()
        with self.assertRaises(namespace["Cancelled"]):
            build_packages(self.source, self.assets, self.card, {}, namespace, chunk_size=128*1024,
                           cancel=stop, progress=advanced)
        self.assertEqual(list((self.assets / "packages").glob(".package-*")), [])
        changed = False
        def mutate(*args):
            nonlocal changed
            if not changed:
                (self.source / "Sample/new-file").write_bytes(b"added during packaging")
                changed = True
        with self.assertRaisesRegex(ValueError, "изменилась"):
            build_packages(self.source, self.assets, self.card, {}, namespace, chunk_size=128*1024, progress=mutate)
        self.assertTrue(server_source(r"\\NAS\Files\games"))
        self.assertFalse(server_source(r"\\NAS\Files\other"))


class PackageServerGuardTests(unittest.TestCase):
    def test_certified_server_key_cannot_sign_updates_or_replace_its_root_certificate(self):
        from ngs.packages import validate_transport
        with RsaKey() as root, RsaKey() as server, RsaKey() as other:
            document = dict(schema=1, files=[], total_size=0)
            transport = dict(schema=1, manifest_sha256=hashlib.sha256(json.dumps(document,sort_keys=True,separators=(",", ":")).encode()).hexdigest(), chunks=[])
            public = server.export()
            transport["key"] = dict(public=base64.b64encode(public).decode(), signature=base64.b64encode(root.sign(package_key_payload(public))).decode())
            transport["signature"] = base64.b64encode(server.sign(transport_payload(transport))).decode()
            document["http"] = transport
            verify_transport(document, root.export())
            with self.assertRaises(ValueError): verify_transport(document, other.export())
            transport["key"]["signature"] = base64.b64encode(root.sign(public)).decode()
            with self.assertRaises(ValueError): verify_transport(document, root.export())
            transport["key"]["public"] = "bad"
            with self.assertRaises(ValueError): validate_transport(transport, document)

    def test_network_sources_are_rejected_without_probing_files(self):
        with patch.object(Path, "stat", side_effect=AssertionError("Network probe")), \
             patch.object(Path, "resolve", side_effect=AssertionError("Network probe")):
            for source in (r"\\NAS\Files\games", "//NAS/Files/games"):
                with self.assertRaisesRegex(ValueError, "HTTP"):
                    install.require_local_source(Path(source))
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=4):
            with self.assertRaisesRegex(ValueError, "Сетевой диск"):
                install.require_local_source(Path("Z:/games"))

    def test_non_admin_and_other_projects_are_rejected_before_database_access(self):
        for admin, code, database, owner in ((False, "ngs_launcher", "ngs_launcher", True),
                (True, "open_heavens_cafe", "open_heavens_cafe", True),
                (True, "ngs_launcher", "other_database", True), (True, "ngs_launcher", "ngs_launcher", False)):
            project = SimpleNamespace(get_code=lambda: code, get_database_name=lambda: database,
                get_value=lambda key: "NGS handheld game catalog; managed from D:/code/NGS" if owner else "other owner")
            db, search, schema = Mock(), Mock(), Mock()
            def module(name, **values):
                value = ModuleType(name); value.__dict__.update(values); return value
            modules = {"pyasm": module("pyasm"),
                "pyasm.biz": module("pyasm.biz", Project=SimpleNamespace(get=lambda: project), Schema=schema),
                "pyasm.common": module("pyasm.common", Environment=SimpleNamespace(get_security=lambda: SimpleNamespace(is_admin=lambda: admin)), Container=Mock()),
                "pyasm.search": module("pyasm.search", Search=search, SearchType=Mock(), DbContainer=db)}
            with self.subTest(admin=admin, code=code, database=database, owner=owner), patch.dict(sys.modules, modules):
                with self.assertRaises(PermissionError): package_action("queue", {}, "")
                db.get.assert_not_called(); search.assert_not_called(); schema.assert_not_called()


if __name__ == "__main__": unittest.main()
