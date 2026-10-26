import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ngs.install import Cancelled, LocalLibrary, install_game, build_manifest
from ngs.portable import PortableStorage
from ngs.scanner import browse_folders, scan_executables, matching_cards, validate_external


class ScannerTests(unittest.TestCase):
    def test_server_scan_skips_stat_for_non_executables_and_still_rejects_links(self):
        original = scan_executables(self.game)
        actual_scandir = os.scandir
        linked = [False]
        class Entry:
            def __init__(self, entry): self.entry=entry
            def __getattr__(self, name): return getattr(self.entry, name)
            def stat(self, **kwargs):
                if not self.name.lower().endswith('.exe'):
                    raise AssertionError('Unnecessary stat of game data or directory')
                return self.entry.stat(**kwargs)
            def is_symlink(self): return self.name=='save.dat' and linked[0]
        class Entries:
            def __init__(self, path): self.entries=actual_scandir(path)
            def __enter__(self): return (Entry(e) for e in self.entries)
            def __exit__(self, *args): self.entries.close()
        with patch('ngs.scanner.os', SimpleNamespace(name='posix',scandir=Entries)):
            self.assertEqual(scan_executables(self.game), original)
            linked[0]=True
            self.assertEqual(scan_executables(self.game)['skipped'], 1)

    def test_external_launch_options_survive_metadata_reimport_without_changing_receipt_or_game(self):
        before = self.snapshot()
        added = self.add()
        identity = added['card']['id']
        original = (self.library.records / (identity + '.json')).read_bytes()
        self.library.reconcile()
        self.library.set_launch_arguments(identity, '-windowed -name "Local player"')
        self.assertEqual(self.library.launch_spec(identity)[2], ['-windowed', '-name', 'Local player'])
        self.assertEqual((self.library.records / (identity + '.json')).read_bytes(), original)
        self.library.update_external_metadata(identity, self.card)
        self.add(card=self.card)
        self.library.reconcile()
        self.assertEqual(self.library.launch_spec(identity)[2], ['-windowed', '-name', 'Local player'])
        self.assertEqual(self.snapshot(), before)

    def test_nested_launchers_share_one_record_and_same_catalog_game_cannot_be_added_twice(self):
        original = self.add(card=self.card)
        shutil.copyfile(self.game / 'bin/play.exe', self.game / 'bin/alternate.exe')
        nested = self.library.register_external(self.game / 'bin', 'alternate.exe', 'Other launcher', self.card)
        self.assertEqual(nested['card']['id'], original['card']['id'])
        self.assertEqual(self.library.install_path(nested), self.game)
        self.assertEqual(nested['executable'], 'bin/alternate.exe')
        self.assertEqual(len(self.library.reconcile()), 1)
        other = self.root / 'Another copy'
        other.mkdir()
        shutil.copyfile(self.game / 'bin/play.exe', other / 'play.exe')
        with self.assertRaisesRegex(ValueError, 'уже в библиотеке|already in the library'):
            self.library.register_external(other, 'play.exe', 'Same game', self.card)
        self.assertEqual(len(self.library.reconcile()), 1)
        self.assertTrue((other / 'play.exe').is_file())

    def test_managed_game_can_be_forgotten_explicitly_and_connected_again_without_touching_files(self):
        receipt = install_game(self.storage, self.root, self.card, build_manifest(self.root, self.card))
        folder = self.library.install_path(receipt)
        self.library.forget(self.card['id'])
        connected = self.library.register_external(folder, 'bin/play.exe', 'Kept game', self.card)
        self.assertFalse(connected['managed'])
        before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        self.library.forget(connected['card']['id'])
        self.assertEqual({p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()}, before)

    def test_settings_directory_picker_reads_folders_without_game_import(self):
        before = self.snapshot()
        self.assertEqual([p["name"] for p in browse_folders(str(self.game), allow_network=True)["folders"]], ["bin"])
        for path in ("relative", str(self.game / "..")):
            with self.assertRaises(ValueError):
                browse_folders(path, allow_network=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.library.reconcile(), [])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = PortableStorage(self.root / "launcher")
        self.library = LocalLibrary(self.storage)
        self.game = self.root / "Existing Game"
        (self.game / "bin").mkdir(parents=True)
        shutil.copyfile(sys.executable, self.game / "bin/play.exe")
        (self.game / "setup.exe").write_bytes(b"not a game")
        (self.game / "save.dat").write_bytes(b"precious save")
        self.card = json.loads((Path(__file__).resolve().parents[1] / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = {"path": "/assets/ngs_launcher/test.jpg", "sha256": "0" * 64, "size": 1}
        self.card.update(id="existing-game", name="Existing Game", source_folder="Existing Game",
                         executable="bin/play.exe", revision=1, cover=image, background=image)

    def snapshot(self):
        return {p.relative_to(self.game).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.game.rglob("*") if p.is_file()}

    def add(self, **kwargs):
        return self.library.register_external(self.game, "bin/play.exe", "Existing Game", **kwargs)

    def test_enrich_existing_external_card_keeps_files_and_launch_spec(self):
        before = self.snapshot()
        original = self.add()
        card = dict(self.card, source_folder='', executable='', available_on_nas=False, name='Beautiful name')
        updated = self.library.update_external_metadata(original['card']['id'], card)
        for key in ('location', 'executable', 'executable_sha256', 'arguments', 'working_directory', 'kind', 'managed'):
            self.assertEqual(updated[key], original[key])
        self.assertEqual(updated['catalog_id'], card['id'])
        self.assertEqual(updated['card']['name'], 'Beautiful name')
        self.assertEqual(self.snapshot(), before)
        validate_external(updated)
        custom = self.library.register_external(self.game, 'bin/play.exe', 'My custom name', card)
        again = self.library.update_external_metadata(custom['card']['id'], dict(card, name='Provider changed'))
        self.assertEqual(again['card']['name'], 'My custom name')

    def test_scan_confirm_reimport_and_forget_never_change_game(self):
        before = self.snapshot()
        listing = browse_folders(self.game)
        self.assertEqual(Path(listing["parent"]), self.game.parent)
        self.assertEqual([f["name"] for f in listing["folders"]], ["bin"])
        found = scan_executables(self.game)
        self.assertEqual([e["path"] for e in found["candidates"]], ["bin/play.exe", "setup.exe"])
        self.assertTrue(found["candidates"][1]["utility"])
        self.assertEqual(self.library.reconcile(), [])  # A scan is not an import.
        added = self.add()
        again = self.add()
        self.assertEqual(added["card"]["id"], again["card"]["id"])
        from_parent = self.library.register_external(self.game.parent, "Existing Game/bin/play.exe", "Existing Game")
        self.assertEqual(from_parent["card"]["id"], added["card"]["id"])
        self.assertEqual(len(self.library.reconcile()), 1)
        exe, cwd, args = self.library.launch_spec(added["card"]["id"])
        self.assertEqual((exe, cwd, args), (self.game / "bin/play.exe", self.game / "bin", []))
        with self.assertRaises(ValueError):
            self.library.uninstall(added["card"]["id"])
        self.library.forget_external(added["card"]["id"])
        self.assertEqual(self.library.reconcile(), [])
        with self.assertRaises(ValueError):
            self.library.launch_spec(added["card"]["id"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.storage.games.iterdir()), [])

    def test_missing_disk_stays_visible_and_updated_exe_requires_confirmation(self):
        added = self.add()
        identity = added["card"]["id"]
        old = self.game
        moved = self.root / "unplugged"
        self.assertTrue(old.is_relative_to(self.root) and moved.is_relative_to(self.root))
        old.rename(moved)
        self.assertFalse(self.library.reconcile()[0]["available"])
        with self.assertRaises((ValueError, OSError)):
            self.library.launch_spec(identity)
        moved.rename(old)
        self.assertTrue(self.library.reconcile()[0]["available"])
        with (self.game / "bin/play.exe").open("ab") as stream:
            stream.write(b"new game update")
        with self.assertRaisesRegex(ValueError, "Подтверди|Confirm"):
            self.library.launch_spec(identity)
        self.assertEqual(self.add()["card"]["id"], identity)
        self.library.reconcile()
        self.assertEqual(self.library.launch_spec(identity)[0], old / "bin/play.exe")

    @unittest.skipUnless(sys.platform == "win32", "Windows EXE launch")
    def test_registered_external_exe_really_starts_without_nas(self):
        from ngs.process import GameProcess
        # A harmless native program proves process startup without requiring an installed game or Python DLLs.
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32/where.exe", self.game / "probe.exe")
        added = self.library.register_external(self.game, "probe.exe", "Local executable probe")
        self.library.reconcile()
        process = GameProcess(*self.library.launch_spec(added["card"]["id"]))
        try:
            deadline = time.monotonic() + 5
            while process.active() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(process.active())
            self.assertEqual(process.exit_code(), 2)  # WHERE with no arguments reports invalid syntax.
        finally:
            process.close()
        self.assertTrue((self.game / "probe.exe").is_file())

    def test_new_external_game_moves_ahead_of_last_launch(self):
        first = self.add()
        self.library.mark_played(first['card']['id'])
        another = self.root / 'another game'
        another.mkdir()
        shutil.copyfile(sys.executable, another / 'game.exe')
        added = self.library.register_external(another, 'game.exe', 'Another')
        self.assertGreater(added['added_at'], first['added_at'])
        self.assertEqual(self.library.reconcile()[0]['card']['id'], added['card']['id'])
        self.library.mark_played(first['card']['id'])
        self.assertEqual(self.library.reconcile()[0]['card']['id'], first['card']['id'])

    def test_external_and_relative_paths_survive_moving_launcher_without_duplicates(self):
        external = self.add()
        internal = self.storage.root / "old game"
        internal.mkdir()
        shutil.copyfile(sys.executable, internal / "game.exe")
        local = self.library.register_external(internal, "game.exe", "Inside")
        moved = self.root / "moved launcher"
        self.assertTrue(self.storage.root.is_relative_to(self.root) and moved.is_relative_to(self.root))
        self.storage.root.rename(moved)
        library = LocalLibrary(PortableStorage(moved))
        self.assertEqual(len(library.reconcile()), 2)
        self.assertEqual(library.launch_spec(external["card"]["id"])[0], self.game / "bin/play.exe")
        self.assertEqual(library.launch_spec(local["card"]["id"])[0], moved / "old game/game.exe")
        self.assertEqual(library.register_external(moved / "old game", "game.exe", "Inside")["card"]["id"], local["card"]["id"])
        self.assertEqual(len(library.reconcile()), 2)

    def test_catalog_matching_is_optional_and_ambiguous_matches_are_preserved(self):
        matches = matching_cards(self.game, "bin/play.exe", [self.card])
        self.assertEqual(matches, [self.card])
        other = dict(self.card, id="another-edition")
        self.assertEqual(len(matching_cards(self.game, "bin/play.exe", [self.card, other])), 2)
        self.assertEqual(matching_cards("D:/unrecognized", "launcher.exe", [self.card]), [])
        record = self.add(card=self.card)
        self.assertEqual(record["catalog_id"], self.card["id"])
        self.assertEqual(record["card"]["cover"], self.card["cover"])
        validate_external(record)

    def test_invalid_paths_exes_and_cancel_leave_no_record(self):
        for exe in ("../bin/play.exe", "bin/play.exe/../../setup.exe", "C:/evil.exe", "save.dat", "missing.exe", "setup.exe"):
            with self.subTest(exe=exe), self.assertRaises((ValueError, OSError)):
                self.library.register_external(self.game, exe, "Test")
        for folder in ("relative", "\\\\server\\games", str(Path(self.game.anchor))):
            with self.subTest(folder=folder), self.assertRaises((ValueError, OSError)):
                scan_executables(folder)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            scan_executables(self.game, cancel)
        with self.assertRaises(Cancelled):
            self.add(cancel=cancel)
        self.assertEqual(self.library.reconcile(), [])

    def test_search_limits_are_visible_and_junctions_are_not_followed(self):
        with patch("ngs.scanner.MAX_ENTRIES", 1):
            self.assertTrue(scan_executables(self.game)["limited"])
        with patch("ngs.scanner.MAX_DEPTH", 0):
            self.assertTrue(scan_executables(self.game)["limited"])
        # NTFS junctions need no symlink privilege and must never lead discovery outside the selected folder.
        if sys.platform == "win32":
            import _winapi
            outside = self.root / "outside"
            outside.mkdir()
            (outside / "hidden.exe").write_bytes(b"outside")
            link = self.game / "linked"
            _winapi.CreateJunction(str(outside), str(link))
            try:
                result = scan_executables(self.game)
                self.assertNotIn("linked/hidden.exe", [e["path"] for e in result["candidates"]])
                self.assertGreater(result["skipped"], 0)
                with self.assertRaises(ValueError):
                    self.library.register_external(link, "hidden.exe", "Unsafe")
            finally:
                link.rmdir()  # Removes only the junction, never its target.

    def test_managed_games_cannot_be_converted_or_forgotten(self):
        manifest = build_manifest(self.root, self.card)
        receipt = install_game(self.storage, self.root, self.card, manifest)
        self.library.reconcile()
        destination = self.storage.games / self.card["name"]
        with self.assertRaises(ValueError):
            self.library.register_external(destination, "bin/play.exe", "No conversion")
        with self.assertRaises(ValueError):
            self.library.forget_external(self.card["id"])
        self.assertTrue((destination / "bin/play.exe").is_file())
        self.assertEqual(self.library.reconcile()[0]["managed"], True)
        malformed = dict(receipt, kind="external", managed=False)
        with self.assertRaises(ValueError):
            validate_external(malformed)

    def test_invalid_external_records_do_not_become_launchable(self):
        record = self.add()
        for change in ({"managed": True}, {"executable": "../play.exe"}, {"location": "../outside"},
                       {"arguments": ["bad\0argument"]}, {"executable_sha256": "bogus"}):
            altered = copy.deepcopy(record)
            altered.update(change)
            marker = self.library.records / (record["card"]["id"] + ".json")
            marker.write_text(json.dumps(altered), encoding="utf8")
            self.assertEqual(self.library.reconcile(), [])
            with self.assertRaises((ValueError, OSError)):
                self.library.launch_spec(record["card"]["id"])
        for invalid in ([], None, "wrong record", dict(record, card=dict(record["card"], cover=42))):
            marker.write_text(json.dumps(invalid), encoding="utf8")
            self.assertEqual(self.library.reconcile(), [])
            with self.assertRaises((ValueError, OSError)):
                self.library.launch_spec(record["card"]["id"])


if __name__ == "__main__":
    unittest.main()
