import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from ngs.api import claim_state, validate_payload
from ngs.backups import (
    UPLOAD_CHUNK,
    pack_backup,
    restore_backup,
    validate_backup_request,
)
from ngs.install import LocalLibrary
from ngs.players import PlayerAccounts
from ngs.portable import PortableStorage
from tools.backups import backup_action, provision_backups


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="ngs-backup-test-"))

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_validate_backup_protocol_and_claim_state(self):
        dev = "a" * 32
        bid = "b" * 32
        sha = "c" * 64
        chunk = base64.b64encode(b"hello world").decode("ascii")

        op, val = validate_payload({"operation": "backup", "value": {"action": "list"}})
        self.assertEqual(op, "backup")
        self.assertEqual(val, {"action": "list"})

        upload_req = validate_backup_request({
            "action": "upload",
            "target_device": dev,
            "device_name": "AOKZOE-A1",
            "backup_id": bid,
            "sha256": sha,
            "size": 11,
            "offset": 0,
            "data": chunk,
        })
        self.assertEqual(base64.b64decode(upload_req["data"]), b"hello world")
        # Must be idempotent when validated in metadata_web and then again in backup_action
        self.assertEqual(validate_backup_request(upload_req), upload_req)

        state = claim_state({"schema": 1}, "1" * 32, "backup", 1000)
        self.assertEqual(len(state["backup_recent"]), 1)
        self.assertNotIn("busy_until", state)

    def test_pack_and_restore_library_and_settings_on_clean_install(self):
        src_root = self.temp / "src"
        dst_root = self.temp / "dst"
        src_storage = PortableStorage(src_root)
        dst_storage = PortableStorage(dst_root)

        settings = src_storage.read_settings()
        settings.update(
            theme="light",
            accent_color="violet",
            graphics_api="vulkan",
            disable_performance_overlays=False,
            ui_scale=1.15,
            games_view="grid",
            library_sort="name",
            hide_installed=True,
        )
        src_storage.write_settings(settings)

        game_id = "local-" + "d" * 24
        receipt = {
            "schema": 1,
            "kind": "external",
            "managed": False,
            "location": str(self.temp / "ExternalGame"),
            "executable": "game.exe",
            "executable_sha256": "f" * 64,
            "working_directory": "",
            "arguments": ["-fullscreen"],
            "installed_at": 1700000000,
            "added_at": 1700000000000000000,
            "card": {
                "id": game_id,
                "name": "Test Restored Game",
                "subtitle": "Локальная игра",
                "description": "Restored from TACTIC backup",
                "genre": "Action",
                "accent": "#22c55e",
            },
        }
        installs_dir = src_storage.state / "installs"
        installs_dir.mkdir(parents=True, exist_ok=True)
        (installs_dir / f"{game_id}.json").write_text(json.dumps(receipt), encoding="utf8")

        lib = LocalLibrary(src_storage)
        lib.set_launch_arguments(game_id, "-skipintro")
        lib.mark_played(game_id)

        zip_bytes, meta = pack_backup(src_storage, "e" * 32, "MyHandheld")
        self.assertEqual(meta["games_count"], 1)
        self.assertGreater(len(zip_bytes), 100)

        restored = restore_backup(dst_storage, zip_bytes, apply_settings=True, apply_library=True)
        self.assertEqual(restored["restored_games"], 1)
        self.assertTrue(restored["restored_settings"])

        dst_settings = dst_storage.read_settings()
        self.assertEqual(dst_settings["theme"], "light")
        self.assertEqual(dst_settings["accent_color"], "violet")
        self.assertEqual(dst_settings["graphics_api"], "vulkan")
        self.assertFalse(dst_settings["disable_performance_overlays"])
        self.assertAlmostEqual(dst_settings["ui_scale"], 1.15)
        self.assertEqual(dst_settings["games_view"], "grid")
        self.assertEqual(dst_settings["library_sort"], "name")
        self.assertTrue(dst_settings["hide_installed"])

        dst_lib = LocalLibrary(dst_storage)
        self.assertEqual(dst_lib.launch_arguments(game_id), "-skipintro")
        reconciled = dst_lib.reconcile()
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["card"]["name"], "Test Restored Game")
        self.assertGreater(reconciled[0]["played_at"], 0)

        # If installs/<id>.json is deleted but library.sqlite still has the installed row,
        # reconcile() must reconstruct the external receipt file from SQLite.
        (dst_storage.state / "installs" / f"{game_id}.json").unlink()
        reconciled_again = dst_lib.reconcile()
        self.assertEqual(len(reconciled_again), 1)
        self.assertTrue((dst_storage.state / "installs" / f"{game_id}.json").is_file())

    def test_device_suggestion_highlights_without_forcing_overwrite(self):
        storage = PortableStorage(self.temp / "clean")
        lib = LocalLibrary(storage)
        accounts = PlayerAccounts(storage, lib)
        try:
            remote_dev = "f" * 32
            accounts.backup_devices = [
                {
                    "device": remote_dev,
                    "name": accounts.device_name,
                    "updated_at": 1700000500,
                    "games_count": 12,
                    "backups": [
                        {
                            "id": "1" * 32,
                            "created_at": 1700000500,
                            "size": 8192,
                            "games_count": 12,
                        }
                    ],
                }
            ]
            devices, suggested = accounts._build_device_options()
            self.assertEqual(suggested, remote_dev)
            ids = [d["device"] for d in devices]
            self.assertIn(remote_dev, ids)
            self.assertIn(accounts.device, ids)
            self.assertFalse(accounts.backup_chosen)
        finally:
            accounts.stop()

    def test_server_backup_action_upload_history_and_download(self):
        private_root = self.temp / "server-backups"
        admin = False
        device = "ab" * 16
        profile = "cd" * 16
        account = {"profile": profile, "client_device": device, "username": "listy"}
        device_state = {"account": account}

        def mod(name, **fields):
            m = ModuleType(name)
            m.__dict__.update(fields)
            return m

        project = SimpleNamespace(
            get_code=lambda: "ngs_launcher",
            get_database_name=lambda: "ngs_launcher",
            get_value=lambda key: "NGS handheld game catalog; managed from D:/code/NGS",
            get_project_db_resource=lambda: "ngs_launcher",
        )
        security = SimpleNamespace(
            is_admin=lambda: admin,
            is_logged_in=lambda: True,
            get_user_name=lambda: "ngs_catalog_worker",
        )

        class Search:
            def __init__(self, kind):
                self.kind = kind

            def add_filter(self, key, value):
                self.code = value

            def get_sobject(self):
                return SimpleNamespace(
                    get_value=lambda k: json.dumps(device_state) if k == "state" else True,
                    set_value=lambda k, v: device_state.update(json.loads(v)) if k == "state" else None,
                    commit=lambda **kw: None,
                )

        modules = {
            "pyasm": mod("pyasm"),
            "pyasm.biz": mod("pyasm.biz", Project=SimpleNamespace(get=lambda: project)),
            "pyasm.common": mod("pyasm.common", Environment=SimpleNamespace(get_security=lambda: security)),
            "pyasm.search": mod(
                "pyasm.search",
                Search=Search,
                DbContainer=SimpleNamespace(get=lambda db: SimpleNamespace(do_query=lambda q: None)),
            ),
        }
        real_path = Path
        with patch.dict(sys.modules, modules), patch(
            "pathlib.Path",
            side_effect=lambda p: private_root if p == "/opt/tactic/tactic_data/ngs_launcher/backups" else real_path(p),
        ):
            admin = True
            provision_backups()
            admin = False

            storage = PortableStorage(self.temp / "client")
            zip_bytes, meta = pack_backup(storage, device, "AOKZOE")
            sha = hashlib.sha256(zip_bytes).hexdigest()
            backup_id = "12" * 16

            offset = 0
            while offset < len(zip_bytes):
                chunk = zip_bytes[offset:offset + UPLOAD_CHUNK]
                res = backup_action(
                    {
                        "action": "upload",
                        "target_device": device,
                        "device_name": "AOKZOE",
                        "backup_id": backup_id,
                        "sha256": sha,
                        "size": len(zip_bytes),
                        "offset": offset,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                    device,
                    account,
                )
                offset = res["offset"]

            fin = backup_action(
                {
                    "action": "finish",
                    "target_device": device,
                    "device_name": "AOKZOE",
                    "backup_id": backup_id,
                    "sha256": sha,
                    "size": len(zip_bytes),
                    "games_count": 3,
                },
                device,
                account,
            )
            self.assertTrue(fin["stored"])
            self.assertEqual(device_state["last_backup"]["backup_id"], backup_id)

            listed = backup_action({"action": "list"}, device, account)
            self.assertEqual(len(listed["devices"]), 1)
            self.assertEqual(listed["devices"][0]["device"], device)
            self.assertEqual(len(listed["devices"][0]["backups"]), 1)
            self.assertEqual(listed["devices"][0]["backups"][0]["id"], backup_id)

            dl = backup_action(
                {"action": "download", "target_device": device, "backup_id": backup_id, "offset": 0},
                device,
                account,
            )
            self.assertTrue(dl["eof"])
            self.assertEqual(base64.b64decode(dl["data"]), zip_bytes)

    def test_graphics_environment_and_child_env_cleanup(self):
        import os
        from ngs.app import (
            GRAPHICS_APIS,
            clean_child_environment,
            configure_graphics_environment,
            restore_launcher_environment,
        )
        self.assertEqual(GRAPHICS_APIS, ("d3d11", "d3d12", "vulkan", "opengl", "software"))
        storage = PortableStorage(self.temp / "gfx")
        settings = storage.read_settings()
        settings.update(graphics_api="vulkan", disable_performance_overlays=True)
        storage.write_settings(settings)

        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        configure_graphics_environment(storage)
        self.assertEqual(os.environ.get("VK_LOADER_LAYERS_DISABLE"), "~all~")
        self.assertEqual(os.environ.get("MANGOHUD"), "0")

        saved = clean_child_environment()
        self.assertNotEqual(os.environ.get("VK_LOADER_LAYERS_DISABLE"), "~all~")
        restore_launcher_environment(saved)
        self.assertEqual(os.environ.get("VK_LOADER_LAYERS_DISABLE"), "~all~")

    def test_local_artwork_worker_resolves_pending_or_missing_previews_from_tactic(self):
        from ngs.app import LocalArtworkWorker
        from ngs.catalog import CatalogCache
        storage = PortableStorage(self.temp / "art")
        lib = LocalLibrary(storage)
        cache = CatalogCache(storage, "http://NAS:8890/tactic")
        game_id = "local-" + "a" * 24
        receipt = {
            "schema": 1,
            "kind": "external",
            "managed": False,
            "catalog_id": "steam-1593500",
            "location": str(self.temp / "GodOfWar"),
            "executable": "GoW.exe",
            "executable_sha256": "b" * 64,
            "working_directory": "",
            "arguments": [],
            "installed_at": 1700000000,
            "added_at": 1700000000000000000,
            "card": {
                "id": game_id,
                "name": "God of War",
                "subtitle": "Sony Interactive Entertainment",
                "description": "Kratos and Atreus",
                "genre": "Action",
                "accent": "#22c55e",
                "source_folder": "",
                "executable": "",
                "metadata_url": "https://store.steampowered.com/app/1593500",
                "revision": 1,
                "available_on_nas": False,
                "cover": {
                    "path": "/assets/ngs_launcher/pending/" + "c" * 64 + ".image",
                    "sha256": "c" * 64,
                    "size": 123,
                },
                "background": {
                    "path": "/assets/ngs_launcher/pending/" + "d" * 64 + ".image",
                    "sha256": "d" * 64,
                    "size": 456,
                },
            },
        }
        installs_dir = storage.state / "installs"
        installs_dir.mkdir(parents=True, exist_ok=True)
        (installs_dir / f"{game_id}.json").write_text(json.dumps(receipt), encoding="utf8")

        resolved_card = dict(
            receipt["card"],
            id="steam-1593500",
            cover={"path": "/assets/ngs_launcher/game/steam-1593500/cover/v001.jpg", "sha256": "1" * 64, "size": 100},
            background={"path": "/assets/ngs_launcher/game/steam-1593500/bg/v001.jpg", "sha256": "2" * 64, "size": 200},
        )
        updates = []
        with patch("ngs.bulk_scan.metadata_requests", return_value=lambda op, val, lbl="": {"card": resolved_card}):
            worker = LocalArtworkWorker(
                storage,
                "http://NAS:8890/tactic",
                cache,
                lib,
                [receipt],
                [],
            )
            worker.updated.connect(updates.append)
            worker.run()
        self.assertIn(True, updates)
        updated_receipt = json.loads((installs_dir / f"{game_id}.json").read_text(encoding="utf8"))
        self.assertEqual(
            updated_receipt["card"]["cover"]["path"],
            "/assets/ngs_launcher/game/steam-1593500/cover/v001.jpg",
        )


if __name__ == "__main__":
    unittest.main()
