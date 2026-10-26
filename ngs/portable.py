"""All application-owned writable files stay beside the launcher."""

import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_TACTIC_URL = ""


def path_key(value):
    import ntpath, unicodedata
    return unicodedata.normalize("NFC", ntpath.normpath(str(value))).casefold()


def path_list(value, label, absolute=False):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError(label + ": specify 1 to 32 folders.")
    result, seen = [], set()
    for item in value:
        if (not isinstance(item, str) or not item.strip() or len(item) > 512
                or any(ord(c) < 32 for c in item) or ".." in Path(item).parts
                or (absolute and not Path(item).is_absolute())):
            raise ValueError(label + ": invalid path; NAS folders require an absolute path.")
        item = item.strip()
        key = path_key(item)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def configured_paths(settings, plural, singular, default):
    return path_list(settings.get(plural, [settings.get(singular, default)]), plural, plural == "games_sources")


def game_sources(settings, card):
    sources = configured_paths(settings, "games_sources", "games_source", r"\\NAS\Games")
    wanted = card.get("source_root") or settings.get("legacy_games_source", settings.get("games_source", sources[0]))
    # A publisher's UNC name is a hint; only this device's configured paths may be used.
    return sorted(sources, key=lambda p: path_key(p) != path_key(wanted))


def game_source(settings, card):
    return game_sources(settings, card)[0]


class PortableStorage:
    def __init__(self, root, resources=None):
        self.root = Path(root).resolve()
        self.resources = Path(resources).resolve() if resources is not None else self.root
        self.data = self.root / "data"
        self.config = self.data / "config"
        self.cache = self.data / "cache"
        self.temp = self.data / "tmp"
        self.logs = self.data / "logs"
        self.state = self.data / "state"
        self.downloads = self.data / "downloads"
        for folder in (self.config, self.cache / "qml", self.cache / "shaders",
                       self.temp, self.logs, self.state / "qml", self.downloads):
            # Refuse redirected writable directories, including Windows junctions.
            if not folder.resolve().is_relative_to(self.root):
                raise OSError("Application data must stay inside its folder: " + str(folder))
            folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self.temp):
            pass
        self.set_games_directory(configured_paths(self.read_settings(), "games_directories", "games_directory", "games")[0])

    def set_games_directory(self, configured):
        self.games, self.game_staging = self.install_directories(configured)

    def install_directories(self, configured):
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError("games_directory must contain a game folder path.")
        games = Path(configured)
        if not games.is_absolute():
            games = self.root / games
            if not games.resolve().is_relative_to(self.root):
                raise ValueError("External game folders require an absolute games_directory path.")
        games = games.resolve()
        if games == self.root or games.is_relative_to(self.data) or self.root.is_relative_to(games):
            raise ValueError("The game folder must be separate from the NGS root and data folder.")
        if not Path(games.anchor).exists():
            # Keep the launcher and saved queue usable while the configured drive is disconnected.
            return games, games / ".ngs-downloads"
        games.mkdir(parents=True, exist_ok=True)
        # Unfinished game files stay on the selected disk, allowing an atomic final rename.
        staging = self.downloads
        if games.stat().st_dev != self.downloads.stat().st_dev:
            staging = games / ".ngs-downloads"
            if staging.is_symlink() or staging.is_junction():
                raise ValueError("The partial download folder must not be a link.")
            staging.mkdir(exist_ok=True)
        return games, staging

    def configure_process(self):
        for key in ("TEMP", "TMP", "TMPDIR"):
            os.environ[key] = str(self.temp)
        tempfile.tempdir = str(self.temp)
        os.environ["QML_DISK_CACHE_PATH"] = str(self.cache / "qml")
        os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
        os.environ["QSG_RENDER_LOOP"] = "threaded"
        # Use elapsed time rather than the primary monitor's Hz when pacing animations on another display.
        os.environ["QSG_USE_SIMPLE_ANIMATION_DRIVER"] = "1"
        os.environ["QSG_RHI_PIPELINE_CACHE_SAVE"] = str(self.cache / "shaders" / "pipeline.cache")
        pipeline = self.cache / "shaders" / "pipeline.cache"
        if pipeline.is_file():
            os.environ["QSG_RHI_PIPELINE_CACHE_LOAD"] = str(pipeline)
        else:
            os.environ.pop("QSG_RHI_PIPELINE_CACHE_LOAD", None)
        sys.dont_write_bytecode = True

    def read_settings(self):
        path = self.config / "settings.json"
        if path.exists():
            try:
                settings = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise ValueError("Corrupt data/config/settings.json. The original file is preserved.") from None
            if not isinstance(settings, dict):
                raise ValueError("settings.json must contain an object. The original file is preserved.")
            return settings
        settings = {
            "tactic_url": DEFAULT_TACTIC_URL,
            "tactic_project": "ngs_launcher",
            "language": "en",
            "local_mode": True,
            "update_url": "https://api.github.com/repos/listyque/NAS-Game-Switcher/releases",
            "games_source": r"\\NAS\Games",
            "games_directory": "games",
            "hide_installed": False,
            "library_sort": "recent",
            "resolve_local_metadata": True,
            "ui_scale": 1.0,
            "animation_speed": "normal",
            "backdrop_blur": True,
            "default_section": "last",
            "window_mode": "fullscreen",
            "production_mode": False,
            "auto_backup": True,
            "auto_start_downloads": True,
            "free_space_reserve_gb": 0.5,
            "gamepad_vibration": True,
            "gamepad_deadzone": 16000,
            "gamepad_repeat_speed": "normal",
            "auto_check_updates": True,
            "minimize_to_tray": True,
            "freeze_on_sleep": True,
            "auto_trim_game_memory": False,
            "known_games_url": "",
            "ui": {"section": 1, "game_index": 0},
        }
        self.write_settings(settings)
        return settings

    def write_settings(self, settings):
        path = self.config / "settings.json"
        # Unique local staging keeps a failed write from destroying the previous config.
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.config,
                                             prefix=".settings-", suffix=".tmp", delete=False) as output:
                name = output.name
                json.dump(settings, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            if name and Path(name).exists():
                Path(name).unlink()
