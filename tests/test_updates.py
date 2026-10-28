import base64
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from ngs.update_crypto import RsaKey
from ngs.updates import archive_entries, verify_envelope, validate_release, prepare_update, check_update, powershell, release_path
from ngs.install import Cancelled
from ngs.portable import PortableStorage
from tools.publish_update import release_upload, upload_release
from ngs.updates import VERSION_PATTERN


class UpdateTests(unittest.TestCase):
    def test_server_check_waits_for_cache_then_uses_only_nas_for_signed_package(self):
        from ngs.updates import check_server_update
        envelope = self.package()
        cancel = threading.Event()
        api = Mock()
        api.call.side_effect = [dict(state='checking', done=1, total=2), dict(state='ready', envelope=envelope)]
        progress = Mock()
        with patch('ngs.api.MetadataApi', return_value=api), patch.object(cancel, 'wait'), \
             patch('ngs.github_updates.GitHubReleases.manifest') as github:
            self.assertEqual(check_server_update(self.storage, self.url, cancel, True, progress), envelope)
            self.assertEqual(api.call.call_args_list[0].args, ('updates', dict(action='refresh')))
            self.assertEqual(api.call.call_args_list[1].args, ('updates', dict(action='status')))
            self.assertEqual(progress.call_args.args[0], .5)
            prepare_update(self.storage, envelope, cancel, base_url=self.url)
            github.assert_not_called()
        self.assertTrue((self.storage.data / 'updates/prepared/runtime/NGS.exe').exists())

    def test_server_check_failure_and_cancellation_do_not_fall_back_to_github(self):
        from ngs.updates import check_server_update
        api = Mock(); cancel = threading.Event()
        with patch('ngs.api.MetadataApi', return_value=api), patch('ngs.github_updates.GitHubReleases') as github:
            for state in [dict(state='error'), dict(state='checking', done=3, total=1), dict(state='unknown')]:
                api.call.return_value = state
                with self.assertRaises(ValueError): check_server_update(self.storage, self.url, cancel)
            api.call.return_value = dict(state='ready', envelope=self.sign(dict(self.release, version='2026.10.14.120000')))
            self.assertIsNone(check_server_update(self.storage, self.url, cancel))
            self.assertEqual(api.call.call_args.args, ('updates', dict(action='check')))
            cancel.set()
            with self.assertRaises(Cancelled): check_server_update(self.storage, self.url, cancel)
            github.assert_not_called()

    def test_organized_release_still_requires_exact_checks_before_any_server_access(self):
        from tools.publish_update import publish
        reports=self.storage.root/'reports';reports.mkdir()
        (reports/'windows-portable.json').write_text(json.dumps(dict(folder='different-build',runs=[])))
        with patch('tools.publish_update.TacticClient') as client:
            for relative in ['dist/windows/release.zip','dist/release.zip']:
                with self.assertRaisesRegex(ValueError,'Check this exact build'):
                    publish(self.storage.root/relative,'',self.storage,'http://invalid',None)
            for relative in ['dist/archive/windows/release.zip','dist/server/release.zip','outside/release.zip']:
                with self.assertRaisesRegex(ValueError,'Only a checked release ZIP'):
                    publish(self.storage.root/relative,'',self.storage,'http://invalid',None)
            client.assert_not_called()

    def test_installer_receives_foreground_permission_without_showing_helper(self):
        from ngs.updates import start_installer
        script = self.storage.data / "updates/apply.ps1"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# fixture")
        with patch("ngs.updates.subprocess.Popen", return_value=Mock(pid=12345)) as spawn, \
                patch("ctypes.WinDLL") as dll:
            start_installer(self.storage)
            dll.return_value.AllowSetForegroundWindow.assert_called_once_with(12345)
            self.assertEqual(spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertIn("Hidden", spawn.call_args.args[0])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.storage = PortableStorage(self.root, resources=self.root / "runtime")
        self.storage.resources.mkdir()
        (self.storage.resources / "assets").mkdir()
        (self.storage.resources / "release.json").write_text('{"version":"2026.10.15.120000"}')
        with RsaKey() as key:
            self.private, self.public = key.export(private=True), key.export()
        (self.storage.resources / "assets/update-public-key.bin").write_bytes(self.public)
        (self.storage.resources / "update.ps1").write_bytes((Path(__file__).resolve().parents[1] / "tools/update.ps1").read_bytes())
        self.share = self.root / "share"
        self.share.mkdir()
        self.requests = []
        self.update_drops = 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Range")))
                if self.path != release_path(owner.release):
                    self.send_error(404); return
                data = (owner.share / owner.release["filename"]).read_bytes()
                offset = int(self.headers["Range"].split("=")[1].split("-")[0]) if self.headers.get("Range") else 0
                self.send_response(206 if offset else 200)
                if offset:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (offset, len(data)-1, len(data)))
                self.send_header("Content-Length", str(len(data)-offset))
                self.end_headers()
                body = data[offset:]
                if owner.update_drops:
                    owner.update_drops -= 1
                    body = body[:len(body)//2]
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/tactic" % self.server.server_port
        settings = self.storage.read_settings()
        settings.update(tactic_url=self.url, update_url=self.url, updates_directory=r"\\unavailable\old\NGS",
                        games_sources=[r"\\unavailable\old\games"])
        self.storage.write_settings(settings)
        self.release = dict(schema=1, version="2026.10.16.120000", platform="windows-x64",
            filename="NGS-windows-x64-20261016-120000.zip", size=1, unpacked_size=1, sha256="0" * 64, notes="Тест подписи")

    def sign(self, release):
        payload = json.dumps(release, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with RsaKey(self.private, private=True) as key:
            return dict(payload=payload, signature=base64.b64encode(key.sign(payload.encode("utf8"))).decode())

    def test_http_path_is_derived_only_from_the_verified_release_digest(self):
        self.assertEqual(release_path(self.release), "/assets/ngs_launcher/releases/" + "0" * 64 + ".zip")
        for changes in (dict(sha256="../evil"), dict(filename="../evil.zip"), dict(url="https://outside/evil")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                release_path(dict(self.release, **changes))

    def test_prepare_uses_pinned_http_origin_and_ignores_legacy_smb_paths(self):
        settings = self.storage.read_settings()
        settings["tactic_url"] = "http://127.0.0.1:9/tactic"
        self.storage.write_settings(settings)
        envelope = self.package()
        original = Path.open
        def local_open(path, *args, **kwargs):
            self.assertFalse(str(path).startswith("\\\\"), "Updater attempted SMB")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", local_open):
            result = prepare_update(self.storage, envelope, threading.Event(), base_url=self.url)
        self.assertEqual(result["version"], self.release["version"])
        self.assertTrue((self.storage.data / "updates/prepared/runtime/NGS.exe").is_file())
        self.assertEqual(self.storage.read_settings(), settings)
        self.assertEqual(self.requests, [(release_path(self.release), None)])

    def test_interrupted_http_update_resumes_after_restart(self):
        envelope = self.package({"runtime/large.bin": os.urandom(9 * 1024**2)})
        cancel = threading.Event()
        def stop(fraction, message):
            if fraction > 0: cancel.set()
        with self.assertRaises(Cancelled): prepare_update(self.storage, envelope, cancel, stop)
        partial = self.storage.data / "updates" / (self.release["sha256"] + ".part")
        offset = partial.stat().st_size
        self.assertTrue(0 < offset < self.release["size"])
        self.assertFalse((self.storage.data / "updates/prepared").exists())
        storage = PortableStorage(self.root, resources=self.storage.resources)
        prepare_update(storage, envelope, threading.Event())
        self.assertEqual(self.requests[-1], (release_path(self.release), "bytes=%d-" % offset))
        self.assertEqual((storage.data / "updates/package.zip").read_bytes(),
                         (self.share / self.release["filename"]).read_bytes())
        self.assertFalse(partial.exists())

    def package(self, extra=None):
        path = self.share / self.release["filename"]
        prefix = path.stem + "/"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in {"NGS.exe": b"MZ-root", "runtime/NGS.exe": b"MZ-runtime", "runtime/update.ps1": b"script",
                    "runtime/release.json": json.dumps({"version": self.release["version"]}).encode(),
                    "runtime/assets/update-public-key.bin": self.public, **(extra or {})}.items():
                archive.writestr(prefix + name, data)
            self.release["unpacked_size"] = sum(p.file_size for p in archive.infolist())
        self.release.update(size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return self.sign(self.release)

    def test_update_download_retries_automatically_and_keeps_progress(self):
        envelope = self.package({'runtime/sample.bin': os.urandom(128 * 1024)})
        self.update_drops = 2
        events = []
        with patch('ngs.http_retry.RETRY_DELAYS', (0, 0, 0)):
            prepare_update(self.storage, envelope, threading.Event(), lambda *args: events.append(args))
        self.assertEqual(len(self.requests), 3)
        self.assertIsNone(self.requests[0][1])
        self.assertTrue(all(value.startswith('bytes=') for path, value in self.requests[1:]))
        self.assertEqual((self.storage.data / 'updates/package.zip').read_bytes(), (self.share / self.release['filename']).read_bytes())
        self.assertEqual(len([event for event in events if 'retry' in event[1].lower()]), 2)
        self.assertEqual([event[0] for event in events], sorted(event[0] for event in events))

    def test_signature_and_contract_reject_changes_wrong_key_and_unknown_fields(self):
        envelope = self.sign(self.release)
        self.assertEqual(verify_envelope(envelope, self.public), self.release)
        for value in (dict(envelope, payload=envelope["payload"].replace("120000", "130000")),
                      dict(envelope, signature=base64.b64encode(b"a" * 256).decode()), dict(envelope, extra=True)):
            with self.assertRaises(ValueError):
                verify_envelope(value, self.public)
        with RsaKey() as other:
            with self.assertRaises(ValueError): verify_envelope(envelope, other.export())
        with self.assertRaises(ValueError): validate_release(dict(self.release, size=True))
        with self.assertRaises(ValueError): validate_release(dict(self.release, filename="../evil.zip"))

    def test_zip_paths_and_private_data_are_rejected_before_extracting(self):
        for name in ("../escape", "runtime/../../escape", "data/settings.json", "runtime/.env",
                     "runtime/CON.txt", "runtime/a:b", "runtime/api-device.json"):
            envelope = self.package({name: b"bad"})
            with zipfile.ZipFile(self.share / self.release["filename"]) as archive:
                with self.subTest(path=name):
                    with self.assertRaises(ValueError): archive_entries(archive, self.release)
        self.package({"runtime/NGS.EXE": b"duplicate"})
        with zipfile.ZipFile(self.share / self.release["filename"]) as archive:
            with self.assertRaises(ValueError): archive_entries(archive, self.release)
        self.package({"runtime/file": b"bad"})
        with zipfile.ZipFile(self.share / self.release["filename"]) as archive:
            archive.getinfo(self.release["filename"][:-4] + "/runtime/file").orig_filename = "runtime\\file"
            with self.assertRaises(ValueError): archive_entries(archive, self.release)

    def test_prepare_preserves_settings_games_and_original_executable_and_rejects_corruption(self):
        envelope = self.package()
        (self.root / "NGS.exe").write_bytes(b"old")
        game = self.storage.games / "unchanged.bin"; game.write_bytes(b"game")
        settings = (self.storage.config / "settings.json").read_bytes()
        result = prepare_update(self.storage, envelope, threading.Event())
        self.assertEqual(result["version"], self.release["version"])
        self.assertEqual((self.root / "NGS.exe").read_bytes(), b"old")
        self.assertEqual(game.read_bytes(), b"game")
        self.assertEqual((self.storage.config / "settings.json").read_bytes(), settings)
        self.assertTrue((self.storage.data / "updates/prepared/runtime/NGS.exe").is_file())
        (self.storage.data / "updates/package.zip").unlink()
        package = self.share / self.release["filename"]
        data = bytearray(package.read_bytes()); data[len(data)//2] ^= 1; package.write_bytes(data)
        with self.assertRaises(ValueError): prepare_update(self.storage, envelope, threading.Event())
        self.assertEqual(game.read_bytes(), b"game")
        self.assertFalse((self.storage.data / "updates/prepared").exists())

    def test_check_uses_signed_tactic_feed_and_compares_versions(self):
        with patch("ngs.updates.CatalogCache.fetch", return_value=json.dumps(self.sign(self.release)).encode()):
            self.assertIsNotNone(check_update(self.storage, "http://127.0.0.1:9/tactic", threading.Event()))
        self.release["version"] = "2026.10.14.120000"
        with patch("ngs.updates.CatalogCache.fetch", return_value=json.dumps(self.sign(self.release)).encode()):
            self.assertIsNone(check_update(self.storage, "http://127.0.0.1:9/tactic", threading.Event()))

    def test_github_preview_verifies_manifest_and_prepares_the_signed_asset(self):
        import io
        from urllib.request import Request,build_opener
        from ngs.github_updates import OFFICIAL_UPDATE_URL
        envelope=self.package();cancel=threading.Event();http=build_opener()
        prefix='https://github.com/listyque/NAS-Game-Switcher/releases/download/v0.1.0-preview.1/'
        assets=[dict(name='updates.json',browser_download_url=prefix+'updates.json'),
                dict(name=self.release['filename'],size=self.release['size'],browser_download_url=prefix+self.release['filename'])]
        seen=[]
        def open_asset(request,timeout=None):
            url=request.full_url;seen.append(url)
            if url.startswith(OFFICIAL_UPDATE_URL):
                return io.BytesIO(json.dumps([dict(draft=False,prerelease=True,assets=assets)]).encode())
            if url==prefix+'updates.json':return io.BytesIO(json.dumps(envelope).encode())
            self.assertEqual(url,prefix+self.release['filename'])
            return http.open(Request('http://127.0.0.1:%d'%self.server.server_port+release_path(self.release),
                                     headers=dict(request.header_items())),timeout=timeout)
        before=(self.storage.config/'settings.json').read_bytes()
        with patch('ngs.github_updates.urllib.request.build_opener',return_value=SimpleNamespace(open=open_asset)):
            self.assertEqual(check_update(self.storage,OFFICIAL_UPDATE_URL,cancel),envelope)
            prepared=prepare_update(self.storage,envelope,cancel,base_url=OFFICIAL_UPDATE_URL)
        self.assertEqual(prepared['version'],self.release['version'])
        self.assertIn(prefix+self.release['filename'],seen)
        self.assertEqual((self.storage.config/'settings.json').read_bytes(),before)
        self.assertTrue((self.storage.data/'updates/prepared/runtime/NGS.exe').is_file())

    def test_powershell_verifies_signature_and_rolls_back_unlaunchable_runtime(self):
        envelope = self.package()
        (self.root / "NGS.exe").write_bytes(b"MZ-old-root")
        (self.storage.resources / "old.txt").write_text("old runtime")
        prepare_update(self.storage, envelope, threading.Event())
        env = dict(os.environ, NGS_UPDATE_TEST_NO_RESTART="1")
        result = subprocess.run([str(powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(self.storage.data / "updates/apply.ps1"), "-Root", str(self.root)],
            env=env, capture_output=True, timeout=30)
        report_path = self.storage.data / "updates/result.json"
        self.assertTrue(report_path.exists(), result.stderr.decode(errors="replace"))
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(report["status"], "error")
        self.assertTrue((self.storage.resources / "old.txt").is_file(), report)
        self.assertEqual((self.root / "NGS.exe").read_bytes(), b"MZ-old-root")
        self.assertFalse((self.storage.data / "updates/transaction.json").exists(), report)
        self.assertTrue((self.storage.data / "updates/failed-runtime").exists(), report)


class ReleaseUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.assets = Path(self.temp.name).resolve()
        (self.assets / "ngs_launcher").mkdir()
        self.admin, self.code = True, "ngs_launcher"
        project = SimpleNamespace(get_code=lambda: self.code, get_database_name=lambda: self.code,
            get_project_db_resource=lambda: "ngs_launcher",
            get_value=lambda key: "NGS handheld game catalog; managed from D:/code/NGS")
        def module(name, **values):
            result = ModuleType(name); result.__dict__.update(values); return result
        self.db = Mock()
        modules = {"pyasm": module("pyasm"),
            "pyasm.biz": module("pyasm.biz", Project=SimpleNamespace(get=lambda: project)),
            "pyasm.common": module("pyasm.common", Environment=SimpleNamespace(
                get_asset_dir=lambda: str(self.assets), get_security=lambda: SimpleNamespace(is_admin=lambda: self.admin))),
            "pyasm.search": module("pyasm.search", DbContainer=self.db)}
        patcher = patch.dict(sys.modules, modules); patcher.start(); self.addCleanup(patcher.stop)
        self.data = b"signed release bytes"
        self.release = dict(schema=1, version="2026.10.16.120000", platform="windows-x64",
            filename="NGS-windows-x64-20261016-120000.zip", size=len(self.data), unpacked_size=100,
            sha256=hashlib.sha256(self.data).hexdigest(), notes="Test")
        self.contract = "import re\nVERSION_PATTERN=" + repr(VERSION_PATTERN) + "\n" + inspect.getsource(validate_release)

    def call(self, action, **fields):
        return release_upload(self.release, action, self.contract, **fields)

    def test_other_project_and_non_admin_fail_before_database_or_files(self):
        for admin, code in ((False, "ngs_launcher"), (True, "open_heavens_cafe")):
            self.admin, self.code = admin, code
            with self.assertRaises(PermissionError): self.call("begin")
            self.db.get.assert_not_called()
            self.assertEqual(list((self.assets / "ngs_launcher").iterdir()), [])

    def test_owned_upload_resumes_and_published_bytes_are_immutable(self):
        self.assertEqual(self.call("begin")["offset"], 0)
        self.call("write", offset=0, data=base64.b64encode(self.data[:4]).decode())
        self.assertEqual(self.call("begin")["offset"], 4)
        with self.assertRaises(ValueError):
            self.call("write", offset=0, data=base64.b64encode(self.data[4:]).decode())
        with self.assertRaises(ValueError): self.call("finish")
        archive = self.assets / "local.zip"; archive.write_bytes(self.data)
        client = SimpleNamespace(script=lambda source, **fields: release_upload(**fields))
        state = upload_release(client, archive, self.release)
        self.assertTrue(state["ready"])
        target = self.assets / "ngs_launcher/releases" / (self.release["sha256"] + ".zip")
        self.assertEqual(target.read_bytes(), self.data)
        self.assertTrue(self.call("begin")["ready"])
        target.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "Immutable"): self.call("begin")
        self.assertEqual(target.read_bytes(), b"bad")

    def test_foreign_partial_is_preserved_and_owned_corruption_can_be_retried(self):
        folder = self.assets / "ngs_launcher/releases"; folder.mkdir()
        partial = folder / (self.release["sha256"] + ".part")
        partial.write_bytes(b"foreign")
        with self.assertRaisesRegex(ValueError, "Unowned"): self.call("begin")
        self.assertEqual(partial.read_bytes(), b"foreign")
        partial.unlink()
        self.call("begin")
        self.call("write", offset=0, data=base64.b64encode(b"x" * len(self.data)).decode())
        with self.assertRaisesRegex(ValueError, "SHA256"): self.call("finish")
        self.assertEqual(partial.stat().st_size, 0)
        self.assertEqual(self.call("begin")["offset"], 0)


if __name__ == "__main__":
    unittest.main()
