import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ngs.install import (Cancelled, LocalLibrary, build_manifest, install_game, validate_manifest,
                         manifest_bytes, fetch_manifest, is_link, installation_lock, sort_installs)
from ngs.portable import PortableStorage
from ngs.catalog import CatalogCache


class InstallTests(unittest.TestCase):
    def test_custom_launch_options_survive_reconcile_relocation_and_reset_without_changing_game(self):
        manifest = dict(self.manifest, arguments=['-original'])
        install_game(self.storage, self.source, self.card, manifest)
        library = LocalLibrary(self.storage)
        library.reconcile()
        folder = self.storage.games / 'Black Mesa'
        before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        receipt = (library.records / 'sample.json').read_bytes()
        line = '-novid -name "Игрок один"'
        library.set_launch_arguments('sample', line)
        library.reconcile()
        self.assertEqual(library.launch_spec('sample')[2], ['-original', '-novid', '-name', 'Игрок один'])
        self.assertEqual((library.records / 'sample.json').read_bytes(), receipt)
        self.assertEqual({p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()}, before)
        moved = self.root / 'moved launcher'
        self.storage.root.rename(moved)
        other = LocalLibrary(PortableStorage(moved))
        self.assertEqual(other.launch_arguments('sample'), line)
        self.assertEqual(other.launch_spec('sample')[2], ['-original', '-novid', '-name', 'Игрок один'])
        with self.assertRaises(ValueError): other.set_launch_arguments('sample', 'bad\0value')
        self.assertEqual(other.launch_arguments('sample'), line)
        other.set_launch_arguments('sample', '')
        self.assertEqual(other.launch_spec('sample')[2], ['-original'])

    def test_catalog_launch_options_saved_before_installation_apply_after_download(self):
        library = LocalLibrary(self.storage)
        library.set_launch_arguments('sample', '-windowed')
        install_game(self.storage, self.source, self.card, self.manifest)
        library.reconcile()
        self.assertEqual(library.launch_spec('sample')[2], ['-windowed'])

    def test_forget_managed_installation_preserves_all_files_and_does_not_return_after_restart(self):
        install_game(self.storage, self.source, self.card, self.manifest)
        library = LocalLibrary(self.storage)
        library.reconcile()
        folder = self.storage.games / 'Black Mesa'
        (folder / 'save.dat').write_bytes(b'keep saves too')
        before = {p.relative_to(folder): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in folder.rglob('*') if p.is_file()}
        with self.assertRaises(ValueError):
            library.forget_external('sample')
        library.forget('sample')
        self.assertEqual(LocalLibrary(self.storage).reconcile(), [])
        with self.assertRaises(ValueError):
            library.launch_spec('sample')
        self.assertEqual({p.relative_to(folder): (p.read_bytes(), p.stat().st_mtime_ns)
                          for p in folder.rglob('*') if p.is_file()}, before)

    def test_forget_rejects_unfinished_removal_and_pending_installation(self):
        install_game(self.storage, self.source, self.card, self.manifest)
        library = LocalLibrary(self.storage)
        record = library.records / 'sample.json'
        original = record.read_bytes()
        receipt = json.loads(original)
        receipt['removing'] = {'name': 'unfinished'}
        record.write_text(json.dumps(receipt))
        with self.assertRaises(ValueError): library.forget('sample')
        self.assertTrue(record.exists())
        record.write_bytes(original)
        (library.records / 'sample.pending.json').write_bytes(original)
        with self.assertRaises(ValueError): library.forget('sample')
        self.assertTrue(record.exists())
        self.assertTrue((self.storage.games / 'Black Mesa/play.exe').is_file())

    def test_new_installation_precedes_old_launch_and_survives_restart(self):
        install_game(self.storage, self.source, self.card, self.manifest)
        library = LocalLibrary(self.storage)
        library.mark_played('sample')
        other = dict(self.card, id='second')
        document = dict(self.manifest, game_id='second')
        install_game(self.storage, self.source, other, document)
        self.assertEqual([r['card']['id'] for r in LocalLibrary(self.storage).reconcile()], ['second', 'sample'])
        library.mark_played('sample')
        self.assertEqual(library.reconcile()[0]['card']['id'], 'sample')
        self.assertEqual(library.reconcile('added')[0]['card']['id'], 'second')

    def test_library_sort_modes_are_stable_and_support_old_receipts(self):
        rows = [dict(card=dict(id='old', name='Zebra', genre='Action'), installed_at=1, played_at=4_000_000_000),
                dict(card=dict(id='new', name='alpha', genre='Puzzle'), added_at=5_000_000_001),
                dict(card=dict(id='legacy', name='Beta', genre=''), installed_at=2)]
        before = copy.deepcopy(rows)
        for mode, expected in [('recent', ['new', 'old', 'legacy']), ('added', ['new', 'legacy', 'old']),
                               ('name', ['new', 'legacy', 'old']), ('genre', ['old', 'new', 'legacy'])]:
            with self.subTest(mode=mode):
                self.assertEqual([r['card']['id'] for r in sort_installs(rows, mode)], expected)
        self.assertEqual(rows, before)
        bad = dict(card=dict(id='bad', name='Bad'), installed_at='bad', added_at=None, played_at=None)
        self.assertEqual(sort_installs([bad] * 2), [bad] * 2)

    def test_launch_history_is_available_after_restart_without_changing_receipts(self):
        install_game(self.storage, self.source, self.card, self.manifest)
        library = LocalLibrary(self.storage)
        self.assertEqual(library.reconcile()[0]['played_at'], 0)
        record = library.records / 'sample.json'
        before = record.read_bytes()
        library.mark_played('sample')
        played = library.reconcile()[0]['played_at']
        self.assertGreater(played, 0)
        self.assertEqual(LocalLibrary(self.storage).reconcile()[0]['played_at'], played)
        self.assertEqual(record.read_bytes(), before)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "nas"
        self.source.mkdir()
        game = self.source / "sample"
        (game / "data").mkdir(parents=True)
        (game / "empty").mkdir()
        (game / "play.exe").write_bytes(b"portable executable fixture")
        (game / "data/content.bin").write_bytes(bytes(range(256)) * 16)
        card = json.loads((Path(__file__).resolve().parents[1] / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = {"path": "/assets/ngs_launcher/test.jpg", "sha256": "0" * 64, "size": 1}
        self.card = dict(card, id="sample", source_folder="sample", executable="play.exe", revision=1, cover=image, background=image)
        self.storage = PortableStorage(self.root / "app")
        self.manifest = build_manifest(self.source, self.card)

    def test_manifest_hash_cache_reuses_only_unchanged_files(self):
        import ngs.install as module
        hashes = {}
        first = build_manifest(self.source, self.card, hash_cache=hashes)
        with patch('ngs.install.hash_file', wraps=module.hash_file) as hashed:
            self.assertEqual(build_manifest(self.source, self.card, hash_cache=hashes), first)
            hashed.assert_not_called()
            (self.source / 'sample/play.exe').write_bytes(b'updated executable')
            changed = build_manifest(self.source, self.card, hash_cache=hashes)
            self.assertEqual(hashed.call_count, 1)
            self.assertNotEqual(changed, first)
        self.assertEqual(build_manifest(self.source, self.card), changed)

    def test_real_copy_verifies_and_recovers_library_after_rename_before_db_write(self):
        phases = set()
        receipt = install_game(self.storage, self.source, self.card, self.manifest,
                               progress=lambda phase, *args: phases.add(phase))
        self.assertEqual(phases, {"prepare", "copy", "verify"})
        self.assertEqual(receipt["file_count"], 2)
        self.assertEqual(receipt["location"], "games/Black Mesa")
        self.assertTrue((self.storage.games / "Black Mesa/empty").is_dir())
        self.assertEqual((self.storage.games / "Black Mesa/data/content.bin").read_bytes(),
                         (self.source / "sample/data/content.bin").read_bytes())
        library = LocalLibrary(self.storage)
        self.assertEqual(len(library.reconcile()), 1)
        self.assertEqual(library.launch_spec("sample")[0], self.storage.games / "Black Mesa/play.exe")
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])
        moved = self.root / "moved"
        self.assertTrue(moved.is_relative_to(self.root) and self.storage.root.is_relative_to(self.root))
        self.storage.root.rename(moved)
        other = LocalLibrary(PortableStorage(moved))
        self.assertEqual(len(other.reconcile()), 1)
        self.assertEqual(other.launch_spec("sample")[0], moved / "games/Black Mesa/play.exe")

    def test_custom_directory_and_changing_default_preserves_registered_games(self):
        target = self.root / "external games"
        settings = self.storage.read_settings()
        settings["games_directory"] = str(target)
        self.storage.write_settings(settings)
        other = PortableStorage(self.storage.root)
        self.assertEqual(other.games, target)
        install_game(other, self.source, self.card, self.manifest)
        library = LocalLibrary(other)
        library.reconcile()
        settings["games_directory"] = "another games"
        other.write_settings(settings)
        changed = LocalLibrary(PortableStorage(other.root))
        self.assertEqual(len(changed.reconcile()), 1)
        self.assertEqual(changed.launch_spec("sample")[0], target / "Black Mesa/play.exe")
        self.assertTrue(changed.path.is_relative_to(self.storage.root))

    def test_digest_mismatch_and_cancel_never_install_or_modify_source(self):
        damaged = copy.deepcopy(self.manifest)
        damaged["files"][0]["sha256"] = "f" * 64
        before = (self.source / "sample/data/content.bin").read_bytes()
        with self.assertRaises(ValueError):
            install_game(self.storage, self.source, self.card, damaged)
        self.assertFalse((self.storage.games / "Black Mesa").exists())
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])
        cancel = threading.Event()
        def stop_after_write(*args):
            cancel.set()
        with self.assertRaises(Cancelled):
            install_game(self.storage, self.source, self.card, self.manifest, cancel, stop_after_write)
        self.assertFalse((self.storage.games / "Black Mesa").exists())
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])
        self.assertEqual((self.source / "sample/data/content.bin").read_bytes(), before)

    def test_disk_space_existing_destination_and_tampered_exe(self):
        with patch("ngs.install.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(OSError):
                install_game(self.storage, self.source, self.card, self.manifest)
        self.assertEqual(list(self.storage.game_staging.iterdir()), [])
        install_game(self.storage, self.source, self.card, self.manifest)
        with self.assertRaises(FileExistsError):
            install_game(self.storage, self.source, self.card, self.manifest)
        library = LocalLibrary(self.storage)
        library.reconcile()
        (self.storage.games / "Black Mesa/play.exe").write_bytes(b"tampered exe")
        with self.assertRaises(ValueError):
            library.launch_spec("sample")

    def test_two_launcher_instances_cannot_install_at_the_same_time(self):
        with installation_lock(self.storage):
            with self.assertRaises(OSError):
                install_game(self.storage, self.source, self.card, self.manifest)
        install_game(self.storage, self.source, self.card, self.manifest)
        self.assertEqual(len(LocalLibrary(self.storage).reconcile()), 1)

    def test_remove_only_managed_copy_preserves_nas_and_can_install_again(self):
        install_game(self.storage, self.source, self.card, self.manifest)
        library = LocalLibrary(self.storage)
        library.reconcile()
        library.uninstall("sample")
        self.assertFalse((self.storage.games / "Black Mesa").exists())
        self.assertEqual(library.reconcile(), [])
        self.assertTrue((self.source / "sample/play.exe").is_file())
        install_game(self.storage, self.source, self.card, self.manifest)
        self.assertEqual(len(library.reconcile()), 1)
        record = library.records / "sample.json"
        receipt = json.loads(record.read_bytes())
        receipt["managed"] = False
        record.write_text(json.dumps(receipt), encoding="utf8")
        with self.assertRaises(ValueError):
            library.uninstall("sample")
        self.assertTrue((self.storage.games / "Black Mesa/play.exe").is_file())

    def test_interrupted_removal_remains_retryable_and_not_launchable(self):
        install_game(self.storage, self.source, self.card, self.manifest)
        library = LocalLibrary(self.storage)
        library.reconcile()
        cancel = threading.Event()
        with self.assertRaises(Cancelled):
            library.uninstall("sample", cancel, lambda *args: cancel.set())
        rows = library.reconcile()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get("removing"))
        with self.assertRaises(ValueError):
            library.launch_spec("sample")
        library.uninstall("sample")
        self.assertEqual(library.reconcile(), [])
        self.assertEqual(list(self.storage.games.iterdir()), [])

    def test_pending_receipt_recovers_only_its_own_completed_directory(self):
        install_game(self.storage, self.source, self.card, self.manifest)
        records = self.storage.state / "installs"
        record = records / "sample.json"
        pending = records / "sample.pending.json"
        record.rename(pending)
        library = LocalLibrary(self.storage)
        self.assertEqual(len(library.reconcile()), 1)
        self.assertTrue(record.is_file())
        self.assertFalse(pending.exists())
        receipt = json.loads(record.read_bytes())
        receipt["directory_identity"] = [-1, -1]
        pending.write_text(json.dumps(receipt), encoding="utf8")
        record.unlink()
        self.assertEqual(library.reconcile(), [])

    def test_manifest_rejects_windows_collisions_traversal_wrong_game_and_missing_exe(self):
        for path in ("../outside", "C:/outside", "a:stream", "AUX.exe", "data/CON.txt", ".ngs-install.json"):
            changed = copy.deepcopy(self.manifest)
            changed["files"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_manifest(changed)
        for second in (dict(self.manifest["files"][0], path="DATA/CONTENT.BIN"),
                       dict(self.manifest["files"][0], path="play.exe/nested.bin")):
            changed = copy.deepcopy(self.manifest)
            changed["files"].append(second)
            changed["total_size"] += second["size"]
            with self.assertRaises(ValueError):
                validate_manifest(changed)
        for change in ({"game_id": "other"}, {"executable": "missing.exe"}, {"total_size": 0}, {"schema": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_manifest(dict(self.manifest, **change), self.card)
        self.assertTrue(is_link(SimpleNamespace(st_mode=0, st_file_attributes=0x400)))

    def test_source_changed_during_manifest_build_is_rejected(self):
        changed = False
        def mutate(*args):
            nonlocal changed
            if changed:
                return
            changed = True
            with (self.source / "sample/play.exe").open("ab") as stream:
                stream.write(b"changed")
        with self.assertRaises(ValueError):
            build_manifest(self.source, self.card, progress=mutate)

    def test_manifest_download_is_checked_and_usable_offline(self):
        data = manifest_bytes(self.manifest)
        card = dict(self.card, manifest={"path": "/assets/ngs_launcher/manifest.json", "size": len(data),
                                        "sha256": hashlib.sha256(data).hexdigest()})
        cache = CatalogCache(self.storage, "http://nas/tactic")
        with patch.object(cache, "fetch", return_value=data):
            self.assertEqual(fetch_manifest(cache, card, threading.Event()), self.manifest)
        with patch.object(cache, "fetch", side_effect=OSError("offline")) as opened:
            self.assertEqual(fetch_manifest(cache, card, threading.Event()), self.manifest)
        opened.assert_not_called()
        bad = dict(card, manifest=dict(card["manifest"], sha256="f" * 64))
        with patch.object(cache, "fetch", return_value=data), self.assertRaises(ValueError):
            fetch_manifest(cache, bad, threading.Event())

    def test_manifest_cli_builds_one_game_and_keeps_output_inside_program(self):
        import io
        from tools.game_manifest import main
        settings = self.storage.read_settings()
        settings["games_source"] = str(self.source)
        self.storage.write_settings(settings)
        feed = json.dumps({"schema": 1, "project": "ngs_launcher", "games": [self.card]}).encode()
        with patch("tools.game_manifest.PortableStorage", return_value=self.storage), \
                patch.object(self.storage, "configure_process"), \
                patch("tools.game_manifest.CatalogCache.fetch", return_value=feed), \
                patch("sys.argv", ["game_manifest", "--game", "sample", "--arg=-novid"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            main()
        path = self.storage.temp / "sample-manifest.json"
        document = validate_manifest(json.loads(path.read_bytes()), self.card)
        self.assertEqual(document["arguments"], ["-novid"])
        self.assertEqual(len(document["files"]), 2)


if __name__ == "__main__":
    unittest.main()
