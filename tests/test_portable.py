import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ngs.portable import PortableStorage, configured_paths, path_list, game_source, game_sources


class PortableTests(unittest.TestCase):
    def test_device_sources_override_publisher_path_and_legacy_source_only_affects_priority(self):
        first, second = str(self.root / "nas-one"), str(self.root / "nas-two")
        settings = dict(games_source=first, legacy_games_source=first, games_sources=[second, first])
        self.assertEqual(game_source(settings, {}), first)
        self.assertEqual(game_source(settings, {"source_root": second}), second)
        self.assertEqual(game_sources(settings, {}), [first, second])
        self.assertEqual(game_sources(settings, {"source_root": second}), [second, first])
        with patch.object(Path, "stat", side_effect=AssertionError("NAS probes must run in the worker")):
            self.assertEqual(game_sources(settings, {"source_root": r"\\NAS\Files\games"}), [second, first])
        settings["games_sources"] = [second]
        self.assertEqual(game_source(settings, {}), second)
        self.assertEqual(game_source(settings, {"source_root": str(self.root / "unapproved")}), second)
        for custom in (r"\\NAS\Files\games", "Z:/games", str(self.root / "different share")):
            changed = dict(games_source=first, legacy_games_source=first, games_sources=[custom])
            self.assertEqual(game_source(changed, {"source_root": first}), custom)
        self.assertEqual(configured_paths({"games_source": first}, "games_sources", "games_source", ""), [first])
        self.assertEqual(path_list([first, first.upper(), second], "NAS", True), [first, second])
        for value in ([], ["relative"], [first + "\n"]):
            with self.assertRaises(ValueError): path_list(value, "NAS", True)

    def test_custom_destination_does_not_change_default_and_plural_config_survives_restart(self):
        target = self.root / "arbitrary disk folder" / "Games"
        default = self.storage.games
        games, staging = self.storage.install_directories(str(target))
        self.assertEqual(games, target)
        self.assertTrue(target.is_dir())
        self.assertEqual(self.storage.games, default)
        self.assertEqual(staging, self.storage.downloads)
        settings = self.storage.read_settings()
        settings["games_directories"] = [str(target), "games"]
        self.storage.write_settings(settings)
        self.assertEqual(PortableStorage(self.root).games, target)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.storage = PortableStorage(self.root)

    def test_runtime_paths_and_default_config_stay_inside_program(self):
        original_temp = tempfile.tempdir
        try:
            with patch.dict(os.environ):
                self.storage.configure_process()
                for key in ("TEMP", "TMP", "TMPDIR", "QML_DISK_CACHE_PATH", "QSG_RHI_PIPELINE_CACHE_SAVE"):
                    self.assertTrue(Path(os.environ[key]).is_relative_to(self.root))
                self.assertEqual(Path(tempfile.gettempdir()), self.storage.temp)
                with tempfile.NamedTemporaryFile() as temporary:
                    self.assertTrue(Path(temporary.name).is_relative_to(self.root))
        finally:
            tempfile.tempdir = original_temp
        settings = self.storage.read_settings()
        self.assertEqual(settings["games_source"], r"\\NAS\Files\games")
        self.assertEqual(settings["animation_speed"], "normal")
        self.assertTrue(settings["backdrop_blur"])
        self.assertEqual(settings["default_section"], "last")
        self.assertEqual(settings["window_mode"], "fullscreen")
        self.assertEqual(settings["free_space_reserve_gb"], 0.5)
        self.assertEqual(settings["gamepad_deadzone"], 16000)
        self.assertEqual(settings["gamepad_repeat_speed"], "normal")
        self.assertTrue((self.root / "data/config/settings.json").is_file())

    def test_resources_are_separate_from_portable_state_and_relative_games(self):
        runtime = self.root / "runtime"
        storage = PortableStorage(self.root, resources=runtime)
        self.assertEqual(storage.resources, runtime)
        self.assertEqual(storage.data, self.root / "data")
        self.assertEqual(storage.games, self.root / "games")
        self.assertFalse((runtime / "data").exists())

    def test_interrupted_write_preserves_previous_config(self):
        self.storage.write_settings({"ui": {"section": 1}})
        before = (self.storage.config / "settings.json").read_bytes()
        with patch("ngs.portable.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.storage.write_settings({"ui": {"section": 2}})
        self.assertEqual((self.storage.config / "settings.json").read_bytes(), before)
        self.assertEqual(list(self.storage.config.glob("*.tmp")), [])

    def test_disconnected_installation_drive_keeps_portable_state_available(self):
        drive = next((Path(chr(letter) + ":/") for letter in range(ord("Z"), ord("D"), -1)
                      if not Path(chr(letter) + ":/").exists()), None)
        if drive is None:
            self.skipTest("No unused drive letter")
        settings = self.storage.read_settings()
        settings["games_directory"] = str(drive / "NGSGames")
        self.storage.write_settings(settings)
        other = PortableStorage(self.root)
        self.assertEqual(other.games, drive / "NGSGames")
        self.assertEqual(other.game_staging, other.games / ".ngs-downloads")
        self.assertTrue(other.state.is_dir())
        self.assertFalse(other.games.exists())

    def test_corrupt_settings_are_never_silently_overwritten(self):
        path = self.storage.config / "settings.json"
        for content in (b'{bad json', b'[]'):
            path.write_bytes(content)
            with self.assertRaises(ValueError):
                self.storage.read_settings()
            self.assertEqual(path.read_bytes(), content)

    def test_folder_can_move_without_changing_stored_paths(self):
        self.storage.write_settings({"ui": {"section": 2, "game_index": 3}})
        moved = self.root / "moved NGS"
        self.storage.data.rename(moved)
        new_root = self.root / "portable copy"
        new_root.mkdir()
        moved.rename(new_root / "data")
        other = PortableStorage(new_root)
        self.assertEqual(other.read_settings()["ui"], {"section": 2, "game_index": 3})
        self.assertTrue(other.logs.is_relative_to(new_root))
        self.assertNotIn(str(self.root), json.dumps(other.read_settings()))
