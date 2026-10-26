"""Fullscreen launcher with an offline-first published TACTIC catalog."""

import ctypes
from ctypes import wintypes
import logging
from ngs.diagnostics import event, note_activity
import json
import math
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Property, Signal, Slot, QEvent, Qt, QUrl, QThread, QTimer
from PySide6.QtGui import QGuiApplication, QFont, QFontDatabase, QIcon, QDesktopServices
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickGraphicsConfiguration, QQuickWindow, QSGRendererInterface

GRAPHICS_APIS = ("d3d11", "d3d12", "vulkan", "opengl", "software")
ANIMATION_SPEEDS = ("normal", "fast", "off")
DEFAULT_SECTIONS = ("last", "library", "catalog", "installers")
WINDOW_MODES = ("fullscreen", "windowed")
REPEAT_SPEEDS = ("normal", "fast", "slow")
RESERVE_CHOICES = (0.5, 2.0, 5.0, 10.0)
DEADZONE_CHOICES = (10000, 16000, 22000)
_SAVED_OVERLAY_ENV = {}


def configure_graphics_environment(storage, settings_storage=None):
    target_storage = settings_storage or storage
    try:
        settings = target_storage.read_settings()
    except Exception:
        settings = {}
    api = settings.get("graphics_api", "d3d11")
    if api not in GRAPHICS_APIS:
        api = "d3d11"
    boot_guard = storage.state / "graphics-boot.json"
    if settings_storage is None and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        if api != "d3d11" and boot_guard.is_file():
            try:
                prev = json.loads(boot_guard.read_text(encoding="utf-8"))
                if isinstance(prev, dict) and prev.get("api") == api and not prev.get("ready"):
                    logging.warning("Previous launch with graphics_api=%s did not reach first frame; falling back to d3d11", api)
                    api = "d3d11"
                    settings = dict(settings, graphics_api="d3d11")
                    target_storage.write_settings(settings)
            except Exception:
                pass
        try:
            if api != "d3d11":
                boot_guard.write_text(json.dumps({"api": api, "ready": False}), encoding="utf-8")
            else:
                boot_guard.unlink(missing_ok=True)
        except Exception:
            pass
    disable_overlays = settings.get("disable_performance_overlays", True) is not False

    if disable_overlays:
        for key, val in (
            ("VK_LOADER_LAYERS_DISABLE", "~all~"),
            ("DISABLE_VULKAN_OBS_CAPTURE", "1"),
            ("MANGOHUD", "0"),
            ("NODEVICE_SELECT", "1"),
            ("DXGI_DEBUG_OSD", "0"),
            ("RTSSHooksCompatibility", "1"),
        ):
            if key not in _SAVED_OVERLAY_ENV:
                _SAVED_OVERLAY_ENV[key] = os.environ.get(key)
            os.environ[key] = val
        if sys.platform == "win32":
            try:
                # ProcessExtensionPointDisablePolicy (6) blocks SetWindowsHookEx / AppInit_DLLs
                # global overlay hooks (RTSS / Afterburner / Discord / Fraps) in NGS.exe only;
                # child game processes do not inherit this mitigation policy.
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                policy = wintypes.DWORD(1)
                kernel.SetProcessMitigationPolicy(6, ctypes.byref(policy), ctypes.sizeof(policy))
            except Exception:
                pass

    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or os.environ.get("QT_QUICK_BACKEND") == "software":
        return

    os.environ["QSG_RENDER_LOOP"] = "basic" if api in ("d3d12", "vulkan", "opengl") else "threaded"
    pipeline = storage.cache / "shaders" / (f"pipeline-{api}.cache" if api != "d3d11" else "pipeline.cache")
    if api == "software":
        os.environ.pop("QSG_RHI_BACKEND", None)
        os.environ["QT_QUICK_BACKEND"] = "software"
        os.environ.pop("QSG_RHI_PIPELINE_CACHE_SAVE", None)
        os.environ.pop("QSG_RHI_PIPELINE_CACHE_LOAD", None)
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Software)
    else:
        os.environ.pop("QT_QUICK_BACKEND", None)
        os.environ["QSG_RHI_BACKEND"] = api
        os.environ["QSG_RHI_PIPELINE_CACHE_SAVE"] = str(pipeline)
        if pipeline.is_file():
            os.environ["QSG_RHI_PIPELINE_CACHE_LOAD"] = str(pipeline)
        else:
            os.environ.pop("QSG_RHI_PIPELINE_CACHE_LOAD", None)
        qt_api = {
            "d3d11": QSGRendererInterface.GraphicsApi.Direct3D11,
            "d3d12": QSGRendererInterface.GraphicsApi.Direct3D12,
            "vulkan": QSGRendererInterface.GraphicsApi.Vulkan,
            "opengl": QSGRendererInterface.GraphicsApi.OpenGL,
        }[api]
        QQuickWindow.setGraphicsApi(qt_api)
        if api in ("opengl", "vulkan"):
            from PySide6.QtGui import QSurfaceFormat
            fmt = QSurfaceFormat.defaultFormat()
            fmt.setAlphaBufferSize(8)
            QSurfaceFormat.setDefaultFormat(fmt)


def clean_child_environment():
    if not _SAVED_OVERLAY_ENV:
        return None
    saved = {}
    for key, original in _SAVED_OVERLAY_ENV.items():
        saved[key] = os.environ.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original
    return saved


def restore_launcher_environment(saved):
    if not saved:
        return
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val

from ngs.gamepad import GamepadInput
from ngs.catalog import CatalogCache
from ngs.i18n import tr
from ngs.install import LocalLibrary, Cancelled, fetch_manifest, sort_installs, game_folder_name, check_cancel
from ngs.process import GameProcess
from ngs.play_time import PlayTime
from ngs.players import PlayerAccounts
from ngs.power import PowerNotifications
from ngs.scanner import browse_folders, scan_executables, matching_cards
from ngs.store_games import installed_store_games, import_store_games, has_store_artwork
from ngs.downloads import DownloadQueue, install_space
from ngs.packages import package_workspace, PACKAGE_SIZE
from ngs.tactic import TacticClient
from ngs.metadata import client_metadata, search_title
from ngs.portable import configured_paths, path_list, path_key, game_sources
from ngs.updates import current_version, check_update, prepare_update, start_installer
from ngs.bulk_scan import LocalScanJournal, scan_collection
from ngs import startup
from ngs import crash_capture
from ngs.catalogizer_ui import RemoteCatalogBridge
from ngs.system_status import SystemStatus, SETTINGS as SYSTEM_SETTINGS
from ngs.desktop import DesktopControls, WindowsWindowEvents
from ngs.virtual_keyboard import VirtualKeyboard
from ngs import appearance


DEMO_GAMES = [
    {"id": "orbital", "name": "ORBITAL", "subtitle": tr("At the edge of the known world"),
     "genre": tr("Space exploration"), "accent": "#d9ff82", "art": "orbital.svg",
     "description": tr("The last signal leads beyond familiar orbits. Find your way among silent planets and discover what lies beyond the horizon.")},
    {"id": "afterlight", "name": "AFTERLIGHT", "subtitle": tr("The light that remains"),
     "genre": tr("Adventure"), "accent": "#ffb98e", "art": "afterlight.svg",
     "description": tr("Travel through valleys in the evening light. Among ruins and quiet mountains, a journey home begins.")},
    {"id": "monolith", "name": "MONOLITH", "subtitle": tr("Every door has another side"),
     "genre": tr("Puzzle"), "accent": "#b9b0ff", "art": "monolith.svg",
     "description": tr("Impossible architecture and forgotten mechanisms. Explore a space that changes with every discovery.")},
    {"id": "tide", "name": "DEEP TIDE", "subtitle": tr("Beneath the silence"),
     "genre": tr("Exploration"), "accent": "#8eece0", "art": "tide.svg",
     "description": tr("Dive into depths where light grows scarce. Ancient structures await those willing to go further.")},
    {"id": "wild", "name": "WILD NORTH", "subtitle": tr("Where the roads end"),
     "genre": tr("Open world"), "accent": "#c4dfb5", "art": "wild.svg",
     "description": tr("Northern forests, cold lakes and distant peaks. On this journey, you choose your own direction.")},
]


class CatalogWorker(QThread):
    loaded = Signal(list)
    failed = Signal()

    def __init__(self, cache, parent):
        super().__init__(parent)
        self.cache = cache
        self.cancel = threading.Event()

    def run(self):
        try:
            self.loaded.emit(self.cache.refresh(self.cancel))
        except Exception as error:
            # Network errors can include credentials in URLs supplied by an HTTP proxy.
            logging.exception("Catalog refresh failed")
            from ngs.http_retry import transient_network_error
            if transient_network_error(error) and not self.cancel.is_set():
                try:
                    cards = self.cache.load()
                    if cards: self.loaded.emit(cards)
                except (ValueError, OSError):
                    pass
            self.failed.emit()


class TransferCleanupWorker(QThread):
    def __init__(self, bridge):
        super().__init__(bridge)
        self.queue = bridge.download_queue
        self.cancel = threading.Event()

    def run(self):
        try: self.queue.clean_transfers(self.cancel)
        except Exception:
            logging.exception('Transfer cleanup deferred')


class LocalArtworkWorker(QThread):
    updated = Signal(bool)

    def __init__(self, storage, url, cache, library, receipts, catalog_cards, parent=None):
        super().__init__(parent)
        self.storage = storage
        self.url = url
        self.cache = cache
        self.library = library
        self.receipts = list(receipts)
        self.catalog_cards = list(catalog_cards or [])
        self.cancel = threading.Event()

    def _infer_reference(self, receipt, card):
        import re
        from ngs.metadata import metadata_reference
        cid = str(receipt.get("catalog_id") or "")
        for prefix, pfx_out in (("steam-", ""), ("gog-", "gog:"), ("epic-", "epic:"), ("lutris-", "lutris:"),
                                ('wikipedia-en-', 'wikipedia:en:'), ('wikipedia-ru-', 'wikipedia:ru:')):
            if cid.startswith(prefix) and len(cid) > len(prefix):
                try:
                    reference = pfx_out + cid[len(prefix):]
                    metadata_reference(reference)
                    return reference
                except ValueError:
                    pass
        murl = str(card.get("metadata_url") or "")
        m = re.search(r"store\.steampowered\.com/app/([1-9][0-9]{0,9})", murl)
        if m:
            return m.group(1)
        return ""

    def run(self):
        from ngs.bulk_scan import metadata_requests, title_key
        changed = False
        upgraded = False
        metadata = None
        for receipt in self.receipts:
            if self.cancel.is_set():
                return
            try:
                card = receipt.get("card") if isinstance(receipt, dict) else None
                if not isinstance(card, dict) or not card.get("id"):
                    continue
                if receipt.get('metadata_manual') and str(receipt.get('catalog_id', '')).startswith('custom-'):
                    continue
                cover = card.get("cover")
                bg = card.get("background")
                has_valid_dicts = isinstance(cover, dict) and isinstance(bg, dict)
                pending_paths = has_valid_dicts and (
                    str(cover.get("path", "")).startswith("/assets/ngs_launcher/pending/")
                    or str(bg.get("path", "")).startswith("/assets/ngs_launcher/pending/")
                )
                if has_valid_dicts and not pending_paths and card.get('description'):
                    if self.cache.image_valid(cover) and self.cache.image_valid(bg):
                        continue
                    try:
                        self.cache.cache_artwork(card, self.cancel, time.monotonic() + 30)
                        changed = True
                        self.updated.emit(False)
                        continue
                    except Exception:
                        pass
                if receipt.get("kind") != "external":
                    continue
                cid = str(receipt.get("catalog_id") or "")
                tkey = title_key(card.get("name", ""))
                matched = next(
                    (
                        c for c in self.catalog_cards
                        if isinstance(c, dict)
                        and isinstance(c.get("cover"), dict)
                        and isinstance(c.get("background"), dict)
                        and ((cid and c.get("id") == cid) or (tkey and title_key(c.get("name", "")) == tkey))
                    ),
                    None,
                )
                resolved_card = None
                if matched is not None and (not receipt.get('metadata_manual') or matched['id'] == cid):
                    try:
                        self.cache.cache_artwork(matched, self.cancel, time.monotonic() + 30)
                        resolved_card = matched
                    except Exception:
                        resolved_card = None
                if resolved_card is None:
                    if metadata is None:
                        metadata = metadata_requests(self.storage, self.url, self.cancel, lambda _info: None)
                    ref = self._infer_reference(receipt, card)
                    if ref:
                        try:
                            resolved_card = metadata("resolve", ref, card.get("name", ""))["card"]
                        except Exception:
                            resolved_card = None
                    if resolved_card is None and not receipt.get('metadata_manual'):
                        query = search_title(card.get("name", ""))
                        if query:
                            matches = metadata("search", query, card.get("name", ""))
                            exact = [m for m in matches if title_key(m.get("name", "")) == tkey]
                            pick = exact[0] if len(exact) == 1 else None
                            if pick:
                                resolved_card = metadata("resolve", pick["app_id"], card.get("name", ""))["card"]
                if resolved_card is not None and not self.cancel.is_set():
                    self.library.update_external_metadata(card["id"], resolved_card)
                    changed = True
                    upgraded = True
                    self.updated.emit(True)
            except Exception:
                continue
        if changed and not self.cancel.is_set():
            self.updated.emit(upgraded)


class GameWorker(QThread):
    progressed = Signal(str, float, str)
    ioUpdated = Signal(float, float)
    scanEvent = Signal(dict)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, bridge, card, mode):
        super().__init__(bridge)
        self.bridge, self.card, self.mode = bridge, card, mode
        self.cancel = threading.Event()
        self.last_update = 0
        self.phase = ""
        self.io_bytes = [0, 0]
        self.last_io = 0

    def progress(self, phase, done, total, path):
        now = time.monotonic()
        if phase in ("io", "io_total"):
            self.io_bytes = ([self.io_bytes[0] + done, self.io_bytes[1] + total]
                             if phase == "io" else [done, total])
            if phase == "io_total" or now - self.last_io >= .2:
                self.ioUpdated.emit(*self.io_bytes)
                self.last_io = now
            return
        if phase != self.phase or now - self.last_update >= 0.1 or done == total:
            self.phase, self.last_update = phase, now
            self.progressed.emit(phase, done / max(1, total), path)

    def run(self):
        try:
            if self.mode == "launch":
                result = self.bridge.library.launch_spec(self.card["id"], self.cancel)
            elif self.mode == "installer_launch":
                result = self.bridge.installer_library.launch_spec(self.card["id"], self.cancel)
            elif self.mode == "remove":
                result = self.bridge.library.uninstall(self.card["id"], self.cancel, self.progress)
            elif self.mode == "organize":
                result = self.bridge.library.organize(self.card["cards"], self.cancel, self.progress)
                packages = self.bridge.installer_library.organize(self.card["installers"], self.cancel, self.progress)
                result = dict(changed=result["changed"] + packages["changed"], errors=result["errors"] + packages["errors"])
            elif self.mode == "forget":
                result = self.bridge.library.forget(self.card["id"])
            elif self.mode == "browse":
                result = browse_folders(self.card["path"], self.cancel, self.card.get("allow_network", False))
            elif self.mode == "scan":
                result = scan_executables(self.card["path"], self.cancel)
            elif self.mode == "store_scan":
                from ngs.catalog import ArtworkCache
                result = installed_store_games(self.cancel)
                cache = ArtworkCache(self.bridge.storage) if self.bridge.localMode else CatalogCache(self.bridge.storage, self.bridge.settings['tactic_url'])
                locations = {path_key(self.bridge.library.install_path(row)):row for row in self.bridge.library.reconcile()}
                for row in result['rows']:
                    existing = locations.get(path_key(row['path']))
                    if existing:
                        missing = existing.get('kind') == 'external' and not existing.get('metadata_manual') and not has_store_artwork(existing['card'], cache)
                        row.update(state='artwork' if missing else 'already', game_id=existing['card']['id'],
                                   status=tr("No artwork · get cover and description") if missing else tr("Already in My games"))
            elif self.mode == "store_import":
                result = import_store_games(self.bridge.storage, self.card['rows'], self.card['cards'], self.cancel, self.scanEvent.emit,
                                            url=self.card['url'], offline=self.card.get('offline', False))
            elif self.mode == "scan_many":
                result = scan_collection(self.bridge.storage, self.card["path"], self.card["cards"],
                                         self.card["url"], self.cancel, self.scanEvent.emit,
                                         **({"offline": True} if self.card.get("offline") else {}))
            elif self.mode == "import":
                result = self.bridge.library.register_external(self.card["path"], self.card["exe"],
                         self.card["name"], self.card.get("metadata"), self.cancel)
                if self.card.get('metadata_manual') and self.card.get('metadata'):
                    result = self.bridge.library.update_external_metadata(result['card']['id'], self.card['metadata'], manual=True)
            elif self.mode in ("metadata_search", "metadata_resolve"):
                result = client_metadata(self.bridge.storage, self.card["url"],
                                         self.mode.split("_")[1], self.card["value"], self.cancel, progress=self.progress,
                                         **({"offline": True} if self.card.get("offline") else {}))
                if self.cancel.is_set():
                    raise Cancelled()
                if self.mode == "metadata_resolve" and self.card.get("local_id") and not self.card.get('preview_only'):
                    self.bridge.library.update_external_metadata(self.card["local_id"], result["card"], manual=True)
            elif self.mode == 'metadata_apply':
                check_cancel(self.cancel)
                if self.card.get('local_id'):
                    self.bridge.library.update_external_metadata(self.card['local_id'], self.card['metadata'], manual=True)
                result = dict(card=self.card['metadata'], message=tr('Artwork saved.'))
            elif self.mode == 'metadata_custom':
                from ngs.metadata import custom_metadata
                card = custom_metadata(self.bridge.storage, self.card['name'], self.card['description'],
                                       self.card['cover'], self.card['background'], self.card['seed'], self.cancel)
                if self.card.get('local_id'):
                    self.bridge.library.update_external_metadata(self.card['local_id'], card, manual=True, rename=True)
                result = dict(card=card, message=tr('Artwork saved.'))
            elif self.mode == "discard":
                result = self.bridge.download_queue.discard(self.card["id"])
            else:
                job = self.bridge.download_queue.get(self.card["id"])
                cache = self.bridge.cache
                if cache is None or cache.origin != job["origin"]:
                    cache = CatalogCache(self.bridge.storage, job["origin"])
                result = self.bridge.download_queue.run(self.card["id"], cache, self.cancel, self.progress, isolated=True)
            self.completed.emit(result)
        except Cancelled:
            self.failed.emit(tr("Removal interrupted. Retry removal to finish cleaning up.") if self.mode == "remove"
                             else tr("Operation canceled. Game files unchanged.") if self.mode in ("browse", "scan", "import")
                             else tr("Library scan stopped. Added games are saved; you can resume the scan.") if self.mode in ("scan_many", 'store_scan', 'store_import')
                             else tr("Library partially organized. You can repeat the operation.") if self.mode == "organize"
                             else tr("Artwork search canceled.") if self.mode.startswith("metadata_")
                             else tr("Download stopped. Queue state saved."))
        except Exception as error:
            logging.exception("Game operation failed")
            self.failed.emit(str(error))


class InstallPreviewWorker(QThread):
    loaded = Signal(dict)

    def __init__(self, storage, card, cache, directory, serial, parent, queue=None):
        super().__init__(parent)
        self.storage, self.card, self.cache = storage, card, cache
        self.directory, self.serial = directory, serial
        self.queue = queue
        self.cancel = threading.Event()

    def run(self):
        result = {}
        try:
            if "total_size" in self.card:
                size = self.card["total_size"]
                if type(size) is not int or not 0 <= size <= 2**50:
                    raise ValueError("Invalid game size")
                result["size"] = size
                workspace = PACKAGE_SIZE + 1024 * 1024 if self.card.get("delivery") == "on_request" or self.card.get("manifest") else 0
                if self.card.get("delivery") == "on_request": workspace *= 5
            elif self.card.get("delivery") == "on_request":
                from ngs.api import MetadataApi
                estimate = MetadataApi(self.storage, self.cache.origin).call("transfer",
                    dict(action="installer_estimate" if self.card.get("kind") == "installer" else "estimate",
                         game=self.card["id"], revision=self.card["revision"]))
                if (not isinstance(estimate, dict) or type(estimate.get("total_size")) is not int
                        or not 0 <= estimate["total_size"] <= 2**50 or type(estimate.get("workspace")) is not int
                        or not 0 <= estimate["workspace"] <= 2 * 1024**3):
                    raise ValueError("Invalid install estimate")
                result["size"] = estimate["total_size"]
                workspace = max(estimate["workspace"], 5 * (PACKAGE_SIZE + 1024 * 1024))
            else:
                document = fetch_manifest(self.cache, self.card, self.cancel)
                result["size"] = document["total_size"]
                workspace = package_workspace(document)
            result.update(install_space(self.storage, self.directory, result["size"], workspace,
                                        self.queue, cancel=self.cancel))
        except Exception as error:
            logging.exception("Install preview failed")
            result.update(can_install=False, status=tr("Could not check the size or drive. Check the connection and choose Refresh."))
        if not self.cancel.is_set():
            self.loaded.emit(result)


class UpdateWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)
    progressed = Signal(float, str)

    def __init__(self, storage, url, envelope, parent):
        super().__init__(parent)
        self.storage, self.url, self.envelope = storage, url, envelope
        self.cancel = threading.Event()

    def run(self):
        try:
            result = (prepare_update(self.storage, self.envelope, self.cancel, self.progressed.emit, base_url=self.url) if self.envelope
                      else check_update(self.storage, self.url, self.cancel))
            if not self.cancel.is_set():
                self.loaded.emit(result)
        except Cancelled:
            self.failed.emit(tr("Update canceled. Application files preserved."))
        except Exception as error:
            logging.exception("Update failed")
            self.failed.emit(str(error) if isinstance(error, (ValueError, OSError)) and not hasattr(error, "url")
                             else tr("Could not check for or download an update. Check the NGS server address and connection."))


class GameMemoryWorker(QThread):
    result = Signal(object)
    def __init__(self, process, parent):
        super().__init__(parent); self.process = process
    def run(self):
        try: self.result.emit(self.process.trim_working_set())
        except OSError as error: self.result.emit(tr("Could not trim game memory: ") + str(error))


class LauncherBridge(QObject):
    action = Signal(str)
    gamepadChanged = Signal()
    inputChanged = Signal()
    quitRequested = Signal()
    gamesChanged = Signal()
    catalogStatusChanged = Signal()
    libraryChanged = Signal()
    operationChanged = Signal()
    gameStarted = Signal()
    gameLaunchRequested = Signal(str)
    gameReturned = Signal()
    filterChanged = Signal()
    scannerChanged = Signal()
    localGameAdded = Signal(str)
    gameAdded = Signal(int, str)
    downloadsChanged = Signal()
    downloadStatisticsChanged = Signal()
    settingsChanged = Signal()
    metadataChanged = Signal()
    metadataRequested = Signal()
    metadataApplied = Signal()
    installRequested = Signal(str, str)
    installPreviewChanged = Signal()
    updatesChanged = Signal()
    downloadEnqueued = Signal()
    minimizeRequested = Signal()
    freezeChanged = Signal()
    runningChanged = Signal()
    gamePanelRequested = Signal()
    gameLibraryRequested = Signal()
    powerResumed = Signal()
    displayChanged = Signal()
    systemStatusChanged = Signal()
    systemSettingsFocus = Signal(bool)
    inputHostInstalled = Signal()
    installersChanged = Signal()

    def __init__(self, storage, parent=None, demo=False, *, self_test=False):
        super().__init__(parent)
        self.storage = storage
        self._demo = demo
        self._authorized = False
        self.desktop_controls = DesktopControls(self)
        self.desktop_controls.keyboardInput.connect(lambda: self.set_input_device("keyboard"))
        self.virtual_keyboard = VirtualKeyboard(self)
        self.system_status = SystemStatus(self)
        from ngs.system_controls import SystemControls
        self.system_controls = SystemControls(self)
        self.system_status.changed.connect(self.systemStatusChanged)
        self.catalogizer = RemoteCatalogBridge(storage, lambda: self.catalog_published(1), self)
        self.installer_catalogizer = RemoteCatalogBridge(storage, lambda: self.catalog_published(3), self, installers=True)
        self._published_selection = {}
        self._published_refresh = set()
        self.settings = storage.read_settings()
        boot_api = self.settings.get("graphics_api", "d3d11")
        self._boot_graphics = (
            boot_api if boot_api in GRAPHICS_APIS else "d3d11",
            self.settings.get("disable_performance_overlays", True) is not False,
        )
        self._boot_production = self.settings.get("production_mode") is True
        self._boot_language = self.settings.get("language", "en") if self.settings.get("language", "en") in ("en", "ru") else "en"
        self._restart_enabled = not demo and not self_test
        self._restart_pending = False
        self._restarting = False
        self._settings_status = tr("Changes apply to new jobs. Installed games are not moved.")
        self._autostart_supported = startup.supported(storage.root)
        try:
            self._autostart = startup.enabled(storage.root) if self._autostart_supported else False
        except OSError:
            self._autostart = False
        self._gamepad_connected = False
        self._input_device = "keyboard"
        self._hide_installed = self.settings.get("hide_installed", False) is True
        self.library = LocalLibrary(storage)
        self.installer_library = LocalLibrary(storage, installers=True)
        self._installer_packages = self.installer_library.reconcile()
        self._installer_import = None
        self._installer_games = []
        self.installer_cache = None
        self.installer_worker = None
        self._installer_status = tr("The installer catalog is not connected yet.")
        self._installed = self.library.reconcile(self.librarySort)
        self._local_cards = None
        self.libraryChanged.connect(self.invalidate_local_cards)
        self.download_queue = DownloadQueue(storage)
        self.settingsChanged.connect(self.downloadsChanged.emit)
        self.cleanup_worker = None
        self.play_time = PlayTime(self.library.path)
        self.players = PlayerAccounts(storage, self.library, self)
        self.players.changed.connect(self.runningChanged)
        self.players.changed.connect(self.account_changed)
        self.players.backupRestored.connect(self.on_backup_restored)
        self.artwork_worker = None
        self._restoring_backup = False
        self.backup_timer = QTimer(self)
        self.backup_timer.setSingleShot(True)
        self.backup_timer.setInterval(8000)
        self.backup_timer.timeout.connect(self.players.autoBackup)
        self.desktop_controls.changed.connect(self.schedule_backup)
        self.virtual_keyboard.changed.connect(self.schedule_backup)
        self._running_id = self._running_name = self._game_control_error = self._statistics_error = ""
        self._terminating = False
        self._launching = self._launch_animation_done = self._launch_presented = False
        self._pending_launch = None
        self._auto_foreground = False
        self._foreground_attempts = 0
        self._foreground_windows = frozenset()
        self._memory_worker = None
        self._memory_status = ""
        queue = self.download_queue
        self.destroyed.connect(lambda: queue.close())
        self._progress_saved = 0
        self.game_worker = None
        self.update_worker = None
        self._update_envelope = None
        self._update_install_pending = False
        self._update = dict(current=current_version(storage), available=False, busy=False, progress=0.0,
                            version="", notes="", status=tr("Choose Check for updates to look for a new version."))
        self._first_start_pending = (storage.data / "updates/first-start.json").exists()
        result_path = storage.data / "updates/result.json"
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf8"))
                if not isinstance(result, dict):
                    raise ValueError("Invalid update result")
                version, message = result.get("version", ""), result.get("message", "")
                if not isinstance(version, str) or not isinstance(message, str):
                    raise ValueError("Invalid update result fields")
                if result.get("status") == "installed":
                    self._update["status"] = tr("Updated to version ") + version
                elif result.get("status") == "pending_first_start":
                    self._update["status"] = tr("Update installed · checking first launch…")
                else:
                    self._update["status"] = tr("The previous update was not installed: ") + message
            except (ValueError, OSError):
                pass
        self.preview_worker = None
        self._preview_serial = 0
        self._preview_pending = None
        self._preview_card = None
        self._preview_cache = None
        self._install_preview = {"busy": False, "can_install": False, "status": ""}
        self._scanner = {"stage": "browse", "path": "", "parent": "", "folders": [], "candidates": [],
                         "exe": "", "name": "", "matches": [], "status": tr("Choose a game folder or your library's root folder."),
                         "batch_root": "", "batch_rows": [], "batch_review": ""}
        self._scanner.update(store_games=[], store_review='', store_scan=False, store_name='')
        self._metadata = {"query": "", "results": [], "status": tr("Choose a game in the search results."), "local_id": ""}
        self._metadata_preview = None
        self.process = None
        self._sleeping = False
        self._power_error = ""
        self._refresh_rate = 0.0
        self._operation_status = tr("Choose a game in the catalog to download it to this device.")
        self._operation_progress = 0.0
        self._operation_game = ""
        self._operation_name = ""
        self._io_bytes = [0, 0]
        self._io_sample = [0, 0]
        self._io_time = time.monotonic()
        self._io_active = False
        self._ui_active = True
        self._download_statistics = dict(game="", network=0.0, disk=0.0, history=[])
        self.download_timer = QTimer(self)
        self.download_timer.setInterval(1000)
        self.download_timer.timeout.connect(self.sample_download_io)
        # Sampling is needed only while the visible launcher displays a transfer.
        # The download worker continues normally when the UI is hidden.
        self._closing = False
        self.restart_timer = QTimer(self)
        self.restart_timer.setSingleShot(True)
        self.restart_timer.setInterval(120)
        self.restart_timer.timeout.connect(lambda: self.restartLauncher())
        self.runningChanged.connect(self.restart_when_idle)
        self.action.connect(lambda action: event('ui.action', action=action))
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(3600000)
        self.update_timer.timeout.connect(self.checkUpdates)
        self._automatic_updates = (not demo and self._update['current'] != '0.0.0.0'
                                   and self.settings.get("auto_check_updates", True) is not False)
        if self._automatic_updates: self.update_timer.start(3500)
        self.process_timer = QTimer(self)
        self.process_timer.setInterval(1000)
        self.process_timer.timeout.connect(self.check_game)
        self.worker = None
        self.cache = None
        self._catalog_online = None
        self._catalog_refresh_at = self._installer_refresh_at = 0
        self.offline_timer = QTimer(self)
        self.offline_timer.setInterval(60000)
        self.offline_timer.timeout.connect(self.retryOffline)
        if not demo and not self.localMode: self.offline_timer.start()
        self._games = []
        self._catalog_status = tr("Refreshing catalog…")
        if demo:
            self._games = [dict(game, image=QUrl.fromLocalFile(str(storage.resources / "assets" / game["art"])).toString())
                           for game in DEMO_GAMES]
            self._catalog_status = tr("Demo catalog")
        elif self.localMode:
            self._catalog_online = False
            self._catalog_status = tr("Local library · no server required")
        else:
            try:
                if self.settings.get("tactic_project") != "ngs_launcher":
                    raise ValueError("Only the NGS project is supported.")
                self.cache = CatalogCache(storage, self.settings["tactic_url"])
                self.installer_cache = CatalogCache(storage, self.settings["tactic_url"], installers=True)
                try:
                    self._installer_games = self.installer_cache.load()
                except (ValueError, OSError):
                    pass
                self._games = self.cache.load()
                if self._games:
                    self._catalog_status = tr("Cached catalog · refreshing…")
            except (ValueError, OSError, KeyError) as error:
                logging.warning("Catalog cache unavailable (%s)", type(error).__name__)
                self._catalog_status = tr("Could not read the catalog · retry")
            QTimer.singleShot(0, self.refreshCatalog)
        if not demo:
            if self.settings.get("auto_start_downloads", True) is not False:
                QTimer.singleShot(0, self.pumpDownloads)
            QTimer.singleShot(0, self.pumpCleanup)
            QTimer.singleShot(0, self.ensure_local_artwork)

    @Property("QVariantList", notify=gamesChanged)
    def catalogGames(self):
        return [card for card in self._games if card.get("available_on_nas", True)]

    @Property("QVariantList", notify=gamesChanged)
    def games(self):
        installed = set(self.installedIds)
        return [card for card in self.catalogGames
                if not self._hide_installed or card["id"] not in installed]

    @Property(bool, notify=settingsChanged)
    def localMode(self):
        account = getattr(getattr(self, 'players', None), 'account', {})
        return self.settings.get('local_mode', not bool(account)) is True

    @Slot(result=bool)
    def useLocalLibrary(self):
        saved = self.applySettings(dict(self.settingsState, local_mode=True))
        if not saved: self.players.failure(self.settingsStatus)
        return saved

    @Property(bool, notify=runningChanged)
    def loginRequired(self):
        return not self._demo and not self.localMode and not self.players.authorized()

    def account_changed(self):
        authorized = not self.localMode and self.players.authorized()
        changed = authorized != self._authorized
        self._authorized = authorized
        if self.localMode:
            self.offline_timer.stop()
            self.backup_timer.stop()
            self.catalogizer.timer.stop()
            self.installer_catalogizer.timer.stop()
            self._catalog_online = False
        elif not self._demo and not self._closing and not self._sleeping and not self.offline_timer.isActive():
            self.offline_timer.start()
        if changed and authorized and not self._demo:
            if self.cache is None:
                try:
                    self.cache = CatalogCache(self.storage, self.settings['tactic_url'])
                    self.installer_cache = CatalogCache(self.storage, self.settings['tactic_url'], installers=True)
                except (ValueError, OSError) as error:
                    self._catalog_status = str(error)
                    self.catalogStatusChanged.emit()
                    return
            self.refreshCatalog(); self.pumpDownloads()

    @Property("QVariantList", notify=installersChanged)
    def installerGames(self):
        cards = list(self._installer_games)
        ids = {card["id"] for card in cards}
        for receipt in self._installer_packages:
            if receipt["card"]["id"] not in ids:
                card = receipt["card"]
                cards.extend(self.installer_cache.present(dict(games=[card])) if self.installer_cache else [card])
        return cards

    @Property("QVariantList", notify=installersChanged)
    def installerIds(self):
        return [r["card"]["id"] for r in self._installer_packages]

    @Property(str, notify=installersChanged)
    def installerStatus(self): return self._installer_status

    @Property(QObject, constant=True)
    def installerCatalogizer(self): return self.installer_catalogizer

    @Slot(str, result=bool)
    def selectInstallerForImport(self, identity):
        self._installer_import = None
        if not identity:
            return True
        receipt = next((r for r in self._installer_packages if r["card"]["id"] == identity), None)
        if not receipt:
            return False
        card = dict(receipt["card"])
        card["id"] = card.pop("game_id", card["id"])
        matches = [game for game in self._games if game["id"] == card["id"] or (
            card.get("metadata_url") and game.get("metadata_url", "").rstrip("/") == card["metadata_url"].rstrip("/"))]
        if len(matches) == 1:
            card = dict(matches[0])
        for key in ("kind", "delivery", "manifest"):
            card.pop(key, None)
        self._installer_import = card
        self._scanner.update(path="", stage="browse", matches=[], candidates=[], batch_review="")
        self.scannerChanged.emit()
        return True

    @Slot(str)
    def performInstallerAction(self, identity):
        if (self.operationBusy and not self.downloadActive) or self.process is not None:
            return
        if identity in self.installerIds:
            if self.operationBusy:
                return
            self.start_operation(dict(id=identity, name=next(r["card"]["name"] for r in self._installer_packages if r["card"]["id"] == identity)), "installer_launch")
        else:
            card = next((c for c in self._installer_games if c["id"] == identity), None)
            if card and identity not in self.downloadIds:
                self.installRequested.emit(identity, card["name"])

    @Property("QVariantMap", notify=systemStatusChanged)
    def systemState(self):
        return self.system_status.state

    @Property(QObject, constant=True)
    def systemControls(self):
        return self.system_controls

    @Property(QObject, constant=True)
    def desktop(self):
        return self.desktop_controls

    @Property(QObject, constant=True)
    def keyboard(self):
        return self.virtual_keyboard

    @Slot()
    def refreshSystemStatus(self):
        self.system_status.refresh()

    @Slot(str, result=bool)
    def openSystemSettings(self, key):
        if key not in SYSTEM_SETTINGS:
            return False
        logging.info("Opening system settings: %s", key)
        self.systemSettingsFocus.emit(True)
        if QDesktopServices.openUrl(QUrl(SYSTEM_SETTINGS[key])):
            return True
        self.systemSettingsFocus.emit(False)
        self._operation_status = tr("Could not open Windows settings.")
        self.operationChanged.emit()
        return False

    @Property("QVariantMap", notify=settingsChanged)
    def themeColors(self):
        return appearance.colors(self.settings)

    @Slot(str, str, result=bool)
    def setAppearance(self, theme, accent):
        try:
            if theme not in ('dark', 'light') or accent not in appearance.ACCENTS:
                raise ValueError(tr("Invalid theme or accent color."))
            updated = dict(self.settings, theme=theme, accent_color=accent)
            self.storage.write_settings(updated)
            self.settings = updated
            self._settings_status = tr("Artwork saved.")
            self.settingsChanged.emit()
            self.schedule_backup()
            return True
        except (ValueError, OSError) as error:
            self._settings_status = str(error)
            self.settingsChanged.emit()
            return False

    @Slot(str, bool, result=bool)
    def setGraphicsOptions(self, graphics_api, disable_overlays):
        try:
            api = str(graphics_api or 'd3d11').strip().lower()
            if api not in GRAPHICS_APIS:
                raise ValueError(tr("Invalid graphics API."))
            disable = bool(disable_overlays)
            updated = dict(self.settings, graphics_api=api, disable_performance_overlays=disable)
            self.storage.write_settings(updated)
            self.settings = updated
            self._settings_status = tr("Graphics settings saved.")
            self.schedule_settings_restart()
            self.settingsChanged.emit()
            self.schedule_backup()
            return True
        except (ValueError, OSError) as error:
            self._settings_status = str(error)
            self.settingsChanged.emit()
            return False

    @Slot(str, result=bool)
    def setLanguage(self, selected):
        if selected not in ('en', 'ru'):
            return False
        if selected == self.settingsState['language']:
            return True
        try:
            updated = dict(self.settings, language=selected)
            self.storage.write_settings(updated)
        except OSError:
            self._settings_status = tr('Could not save the interface language. Check access to the NGS folder.')
            self.settingsChanged.emit()
            return False
        self.settings = updated
        self._sync_runtime_options()
        self._settings_status = tr('Settings saved.')
        self.schedule_settings_restart()
        self.settingsChanged.emit()
        self.schedule_backup()
        return True

    def schedule_settings_restart(self):
        state = self.settingsState
        self._restart_pending = ((state['graphics_api'], state['disable_performance_overlays']) != self._boot_graphics
                                 or state['production_mode'] != self._boot_production)
        self.restart_timer.stop()
        if self._restart_pending:
            self._settings_status = (tr("Saved. NGS will restart automatically after the game closes.")
                                     if self.process is not None or self._launching else
                                     tr("Saved. Restarting NGS to apply settings…"))
            self.restart_when_idle()

    def restart_when_idle(self):
        if (self._restart_enabled and self._restart_pending and not self._closing
                and self.process is None and not self._launching and not self.restart_timer.isActive()):
            self.restart_timer.start()

    @Slot(result=bool)
    def restartLauncher(self):
        if self._closing or getattr(self, '_restarting', False):
            return False
        if self.process is not None or self._launching:
            self._settings_status = tr("Saved. NGS will restart automatically after the game closes.")
            self.settingsChanged.emit()
            return False
        self._restarting = True
        self._closing = True
        try:
            self.virtual_keyboard.close()
        except Exception:
            pass
        engine = self.parent()
        if engine is not None:
            if getattr(engine, 'input_host', None) is not None:
                try:
                    engine.input_host.close()
                except Exception:
                    pass
            if getattr(engine, 'edge_windows', None) is not None:
                try:
                    engine.edge_windows.close()
                except Exception:
                    pass
        if getattr(self, 'status_overlay', None) is not None:
            try:
                self.status_overlay.close()
            except Exception:
                pass
        app = QGuiApplication.instance()
        if app is not None:
            app.exit(75)
        return True

    @Property("QVariantMap", notify=settingsChanged)
    def settingsState(self):
        result = {key: self.settings.get(key, default) for key, default in
                  (("games_source", ""), ("games_directory", "games"), ("tactic_url", ""), ("resolve_local_metadata", True), ("ui_scale", 1.0))}
        api = self.settings.get("graphics_api", "d3d11")
        anim = self.settings.get("animation_speed", "normal")
        if anim not in ANIMATION_SPEEDS:
            anim = "normal"
        def_sec = self.settings.get("default_section", "last")
        if def_sec not in DEFAULT_SECTIONS:
            def_sec = "last"
        win_mode = self.settings.get("window_mode", "fullscreen")
        if win_mode not in WINDOW_MODES:
            win_mode = "fullscreen"
        reserve = self.settings.get("free_space_reserve_gb", 0.5)
        reserve = float(reserve) if type(reserve) in (int, float) and float(reserve) in RESERVE_CHOICES else 0.5
        deadzone = self.settings.get("gamepad_deadzone", 16000)
        deadzone = int(deadzone) if type(deadzone) is int and deadzone in DEADZONE_CHOICES else 16000
        repeat_speed = self.settings.get("gamepad_repeat_speed", "normal")
        if repeat_speed not in REPEAT_SPEEDS:
            repeat_speed = "normal"
        known_url = self.settings.get("known_games_url", "")
        result.update(local_mode=self.localMode,
                      language=self.settings.get('language') if self.settings.get('language') in ('en', 'ru') else 'en',
                      production_mode=self.settings.get('production_mode') is True, theme='light' if self.settings.get('theme') == 'light' else 'dark',
                      accent_color=self.settings.get('accent_color') if isinstance(self.settings.get('accent_color'), str) and self.settings.get('accent_color') in appearance.ACCENTS else 'green',
                      graphics_api=api if api in GRAPHICS_APIS else "d3d11",
                      disable_performance_overlays=self.settings.get("disable_performance_overlays", True) is not False,
                      animation_speed=anim,
                      animation_scale=0.0 if anim == "off" else 0.55 if anim == "fast" else 1.0,
                      backdrop_blur=self.settings.get("backdrop_blur", True) is not False,
                      default_section=def_sec,
                      window_mode=win_mode,
                      auto_backup=self.settings.get("auto_backup", True) is not False,
                      auto_start_downloads=self.settings.get("auto_start_downloads", True) is not False,
                      free_space_reserve_gb=reserve,
                      gamepad_vibration=self.settings.get("gamepad_vibration", True) is not False,
                      gamepad_deadzone=deadzone,
                      gamepad_repeat_speed=repeat_speed,
                      auto_check_updates=self.settings.get("auto_check_updates", True) is not False,
                      minimize_to_tray=self.settings.get("minimize_to_tray", True) is not False,
                      freeze_on_sleep=self.settings.get("freeze_on_sleep", True) is not False,
                      auto_trim_game_memory=self.settings.get("auto_trim_game_memory", False) is True,
                      known_games_url=known_url.strip() if isinstance(known_url, str) else "")
        result.update(games_sources=configured_paths(self.settings, "games_sources", "games_source", ""),
                      games_directories=self.installDirectories, autostart=self._autostart,
                      autostart_supported=self._autostart_supported)
        scale = result["ui_scale"]
        if type(scale) not in (int, float) or not math.isfinite(scale) or not 0.8 <= scale <= 1.2:
            result["ui_scale"] = 1.0
        return result

    @Property(float, notify=settingsChanged)
    def animationScale(self):
        return self.settingsState["animation_scale"]

    @Property(bool, notify=settingsChanged)
    def backdropBlur(self):
        return self.settingsState["backdrop_blur"]

    @Slot("QVariantMap", result=bool)
    def setQuickOptions(self, patch):
        try:
            updated = dict(self.settings)
            for key in ("animation_speed", "backdrop_blur", "default_section", "window_mode",
                        "auto_backup", "auto_start_downloads", "free_space_reserve_gb",
                        "gamepad_vibration", "gamepad_deadzone", "gamepad_repeat_speed",
                        "auto_check_updates", "minimize_to_tray", "freeze_on_sleep",
                        "auto_trim_game_memory", "production_mode"):
                if key == "production_mode" and key in patch and type(patch[key]) is not bool:
                    raise ValueError(tr("Production mode must be enabled or disabled."))
                if key in patch:
                    updated[key] = patch[key]
            self.storage.write_settings(updated)
            self.settings = updated
            self._sync_runtime_options()
            self._settings_status = tr("Settings saved.")
            self.schedule_settings_restart()
            self.settingsChanged.emit()
            self.schedule_backup()
            return True
        except (ValueError, OSError) as error:
            self._settings_status = str(error)
            self.settingsChanged.emit()
            return False

    def _sync_runtime_options(self):
        state = self.settingsState
        if state['language'] != self._boot_language:
            from ngs.i18n import configure
            configure(self.storage, state['language'])
            self._boot_language = state['language']
            if hasattr(self.parent(), 'retranslate'): self.parent().retranslate()
            self.inputChanged.emit()
        if self.artwork_worker is not None and state.get('resolve_local_metadata', True) is not True:
            self.artwork_worker.cancel.set()
        if not self._demo:
            from ngs.diagnostics import configure_production
            configure_production(self.storage, state['production_mode'])
            if not state['production_mode']: crash_capture.start_watchdog(self.parent())
        self._automatic_updates = (not self._demo and self._update['current'] != '0.0.0.0'
                                   and state["auto_check_updates"])
        if not self._automatic_updates:
            self.update_timer.stop()
        elif not self._sleeping and not self._closing and not self.update_timer.isActive():
            self.update_timer.start(3500)
        engine = self.parent()
        if engine is not None and getattr(engine, "gamepad", None) is not None:
            engine.gamepad.configure(
                vibration=state["gamepad_vibration"],
                deadzone=state["gamepad_deadzone"],
                repeat_speed=state["gamepad_repeat_speed"],
            )

    @Property(float, notify=displayChanged)
    def displayRefreshRate(self):
        return self._refresh_rate

    @Slot(float)
    def set_refresh_rate(self, rate):
        self._refresh_rate = rate
        self.displayChanged.emit()

    @Property("QStringList", notify=settingsChanged)
    def installDirectories(self):
        return configured_paths(self.settings, "games_directories", "games_directory", "games")

    @Slot(str, result=str)
    def directoryLabel(self, path):
        value = Path(path)
        return str(value if value.is_absolute() else self.storage.root / value)

    @Slot(QUrl, result=str)
    def folderPath(self, url):
        return url.toLocalFile()

    @Property(str, notify=settingsChanged)
    def settingsStatus(self):
        return self._settings_status

    @Slot(result=bool)
    def installInputHost(self):
        focus_requested = False
        try:
            if self.process:
                raise ValueError(tr("Close the game before installing the keyboard component."))
            previous = getattr(self, '_input_installer', None)
            if previous is not None:
                raise ValueError(tr("The NGS Input installer is already open."))
            from ngs.input_host import start_input_installer
            self.virtual_keyboard.hide()
            self.systemSettingsFocus.emit(True)
            focus_requested = True
            self._input_installer = start_input_installer(self.storage)
            if self.virtual_keyboard.remote:
                self.virtual_keyboard.remote.close()
            self._settings_status = tr("Confirm NGS Input installation in Windows. The new keyboard connects automatically.")
            self.settingsChanged.emit()
            QTimer.singleShot(250, self.finishInputHostInstall)
            return True
        except (OSError, ValueError) as error:
            if focus_requested: self.systemSettingsFocus.emit(False)
            self._settings_status = str(error)
            self.settingsChanged.emit()
            return False

    @Slot()
    def finishInputHostInstall(self):
        process = getattr(self, '_input_installer', None)
        if process is None: return
        code = process.poll()
        if code is None:
            QTimer.singleShot(250, self.finishInputHostInstall)
            return
        self._input_installer = None
        self.systemSettingsFocus.emit(False)
        if code == 0:
            self._settings_status = tr("NGS Input updated. Connecting the new keyboard…")
            self.inputHostInstalled.emit()
        else:
            self._settings_status = tr("NGS Input was not installed: installation was canceled or failed.")
        self.settingsChanged.emit()

    @Slot()
    def refreshAutostart(self):
        prev_auto, prev_sup, prev_status = self._autostart, self._autostart_supported, self._settings_status
        self._autostart_supported = startup.supported(self.storage.root)
        try:
            self._autostart = startup.enabled(self.storage.root) if self._autostart_supported else False
        except OSError as error:
            self._settings_status = tr("Could not check startup settings: ") + str(error)
        if (self._autostart, self._autostart_supported, self._settings_status) != (prev_auto, prev_sup, prev_status):
            self.settingsChanged.emit()

    @Slot(bool, result=bool)
    def setAutostart(self, enable):
        try:
            startup.set_enabled(self.storage.root, enable)
            self._autostart = startup.enabled(self.storage.root)
            self._settings_status = (tr("Startup enabled: NGS will open when you sign in to Windows.")
                                     if self._autostart else tr("Startup is disabled for this copy of NGS."))
            self.settingsChanged.emit()
            return True
        except (ValueError, OSError) as error:
            self._settings_status = tr("Could not change startup settings: ") + str(error)
            self.settingsChanged.emit()
            return False

    @Slot("QVariantMap", result=bool)
    def applySettings(self, values):
        old_paths = self.storage.games, self.storage.game_staging
        old_autostart = self._autostart
        startup_changed = False
        try:
            local_mode = values.get('local_mode', self.localMode)
            if type(local_mode) is not bool:
                raise ValueError(tr('Local library mode must be enabled or disabled.'))
            if local_mode != self.localMode and self.artwork_worker is not None:
                self.artwork_worker.cancel.set()
            if local_mode != self.localMode and (self.process is not None or self.game_worker is not None
                    or self.players.worker is not None or self.players.backup_worker is not None
                    or self.worker is not None or self.installer_worker is not None
                    or self.preview_worker is not None or self.cleanup_worker is not None
                    or self.catalogizer.state['busy'] or self.installer_catalogizer.state['busy']):
                raise ValueError(tr('Close the game and wait for active operations before switching library mode.'))
            scale = values.get("ui_scale", self.settings.get("ui_scale", 1.0))
            if type(scale) not in (int, float) or not math.isfinite(scale) or not 0.8 <= scale <= 1.2:
                raise ValueError(tr("Interface scale must be between 80 and 120%."))
            theme = values.get('theme', self.settingsState['theme'])
            accent = values.get('accent_color', self.settingsState['accent_color'])
            if theme not in ('dark', 'light') or not isinstance(accent, str) or accent not in appearance.ACCENTS:
                raise ValueError(tr("Choose a light or dark theme and an available accent color."))
            graphics_api = values.get('graphics_api', self.settingsState['graphics_api'])
            if not isinstance(graphics_api, str) or graphics_api not in GRAPHICS_APIS:
                raise ValueError(tr("Choose an available graphics API."))
            disable_overlays = values.get('disable_performance_overlays', self.settingsState['disable_performance_overlays']) is not False
            paths = {}
            for plural, singular, default in (("games_sources", "games_source", ""),
                                               ("games_directories", "games_directory", "games")):
                if plural == "games_sources" and plural not in values and singular not in values:
                    paths[plural] = configured_paths(self.settings, plural, singular, default)
                    continue
                value = values.get(plural)
                if value is None or (singular in values and (not value or values[singular] != value[0])):
                    value = [values[singular]]
                paths[plural] = path_list(value, plural, plural == "games_sources")
            source, directory = paths["games_sources"][0], paths["games_directories"][0]
            url = str(values["tactic_url"]).strip()
            if not local_mode: TacticClient(url, "ngs_launcher")
            for path in paths["games_directories"]:
                self.storage.install_directories(path)
            self.storage.set_games_directory(directory)
            address_changed = url != self.settings.get("tactic_url")
            cache = self.cache
            if address_changed:
                try: cache = CatalogCache(self.storage, url) if url else None
                except ValueError:
                    if not local_mode: raise
                    cache = None
            try:
                games = self._games
                if address_changed: games = cache.load() if cache is not None else []
            except (ValueError, OSError):
                games = []
            state_defaults = self.settingsState
            language = values.get('language', state_defaults['language'])
            if language not in ('en', 'ru'):
                raise ValueError(tr('Choose English or Russian.'))
            keyboard_changes = values.get('keyboard_options', {})
            if not isinstance(keyboard_changes, dict) or any(
                key not in ('enabled', 'overlay_enabled', 'edge_swipe_enabled', 'edge_auto_hide') or type(value) is not bool
                for key, value in keyboard_changes.items()
            ):
                raise ValueError(tr('Keyboard and quick settings options must be enabled or disabled.'))
            autostart = values.get('autostart', old_autostart)
            if type(autostart) is not bool:
                raise ValueError(tr('Startup must be enabled or disabled.'))
            if "production_mode" in values and type(values["production_mode"]) is not bool:
                raise ValueError(tr("Production mode must be enabled or disabled."))
            anim = values.get("animation_speed", state_defaults["animation_speed"])
            if anim not in ANIMATION_SPEEDS:
                anim = state_defaults["animation_speed"]
            def_sec = values.get("default_section", state_defaults["default_section"])
            if def_sec not in DEFAULT_SECTIONS:
                def_sec = state_defaults["default_section"]
            win_mode = values.get("window_mode", state_defaults["window_mode"])
            if win_mode not in WINDOW_MODES:
                win_mode = state_defaults["window_mode"]
            reserve = values.get("free_space_reserve_gb", state_defaults["free_space_reserve_gb"])
            reserve = float(reserve) if type(reserve) in (int, float) and float(reserve) in RESERVE_CHOICES else state_defaults["free_space_reserve_gb"]
            deadzone = values.get("gamepad_deadzone", state_defaults["gamepad_deadzone"])
            deadzone = int(deadzone) if type(deadzone) is int and deadzone in DEADZONE_CHOICES else state_defaults["gamepad_deadzone"]
            repeat_speed = values.get("gamepad_repeat_speed", state_defaults["gamepad_repeat_speed"])
            if repeat_speed not in REPEAT_SPEEDS:
                repeat_speed = state_defaults["gamepad_repeat_speed"]
            known_url = str(values.get("known_games_url", state_defaults["known_games_url"]) or "").strip()
            if known_url and not (known_url.startswith("http://") or known_url.startswith("https://")):
                raise ValueError(tr("The game matching database URL must start with http:// or https://."))
            updated = dict(self.settings, games_source=source, games_directory=directory, tactic_url=url,
                           **paths,
                           legacy_games_source=(self.settings.get("legacy_games_source", self.settings.get("games_source", source))
                                                if "games_sources" in values or "games_source" not in values else source),
                           resolve_local_metadata=values.get("resolve_local_metadata", True) is True, ui_scale=scale,
                           theme=theme, accent_color=accent,
                           graphics_api=graphics_api, disable_performance_overlays=disable_overlays,
                           animation_speed=anim,
                           backdrop_blur=values.get("backdrop_blur", state_defaults["backdrop_blur"]) is not False,
                           default_section=def_sec,
                           window_mode=win_mode,
                           auto_backup=values.get("auto_backup", state_defaults["auto_backup"]) is not False,
                           auto_start_downloads=values.get("auto_start_downloads", state_defaults["auto_start_downloads"]) is not False,
                           free_space_reserve_gb=reserve,
                           gamepad_vibration=values.get("gamepad_vibration", state_defaults["gamepad_vibration"]) is not False,
                           gamepad_deadzone=deadzone,
                           gamepad_repeat_speed=repeat_speed,
                           auto_check_updates=values.get("auto_check_updates", state_defaults["auto_check_updates"]) is not False,
                           minimize_to_tray=values.get("minimize_to_tray", state_defaults["minimize_to_tray"]) is not False,
                           freeze_on_sleep=values.get("freeze_on_sleep", state_defaults["freeze_on_sleep"]) is not False,
                           auto_trim_game_memory=values.get("auto_trim_game_memory", state_defaults["auto_trim_game_memory"]) is True,
                           production_mode=values.get("production_mode", state_defaults["production_mode"]) is True,
                           known_games_url=known_url, language=language, local_mode=local_mode,
                           keyboard_options=self.virtual_keyboard.option_values(**keyboard_changes))
            if autostart != old_autostart:
                startup.set_enabled(self.storage.root, autostart)
                startup_changed = True
                if startup.enabled(self.storage.root) != autostart:
                    raise OSError(tr('Could not change startup settings.'))
            self.storage.write_settings(updated)
            startup_changed = False
            self._autostart = autostart
            self.settings = updated
            self._sync_runtime_options()
            self.virtual_keyboard.changed.emit()
            if address_changed:
                self._catalog_online = None
                self._catalog_refresh_at = self._installer_refresh_at = 0
                self._update_envelope = None
                self._update.update(available=False, version='', notes='')
                if self.update_worker: self.update_worker.cancel.set()
                elif self._automatic_updates: self.update_timer.start(1)
                if self.worker:
                    self.worker.cancel.set()
                self.cache, self._games = cache, games
                self.installer_cache = CatalogCache(self.storage, url, installers=True) if cache is not None else None
                try:
                    self._installer_games = self.installer_cache.load() if self.installer_cache is not None else []
                except (ValueError, OSError):
                    self._installer_games = []
                self.installersChanged.emit()
                if self.installer_worker:
                    self.installer_worker.cancel.set()
                self.gamesChanged.emit()
                if self.worker is None:
                    self.refreshCatalog()
            self._settings_status = tr("Settings saved.")
            self.schedule_settings_restart()
            event('settings.saved', server=url, games_directories=paths['games_directories'], ui_scale=scale)
            self.settingsChanged.emit()
            self.updatesChanged.emit()
            self.players.changed.emit()
            self.schedule_backup()
            return True
        except (KeyError, ValueError, OSError) as error:
            self.storage.games, self.storage.game_staging = old_paths
            if startup_changed and self._autostart == old_autostart:
                try:
                    startup.set_enabled(self.storage.root, old_autostart)
                except OSError as rollback_error:
                    error = OSError(f'{error}; {rollback_error}')
                self.refreshAutostart()
            self._settings_status = str(error)
            self.settingsChanged.emit()
            return False

    @Property(bool, notify=filterChanged)
    def hideInstalled(self):
        return self._hide_installed

    @Slot(bool)
    def setHideInstalled(self, hidden):
        if self._hide_installed == hidden:
            return
        self._hide_installed = hidden
        self.settings["hide_installed"] = hidden
        self.save()
        self.filterChanged.emit()
        self.gamesChanged.emit()
        self.schedule_backup()

    def local_card(self, receipt):
        card = receipt["card"].copy()
        if "total_size" in receipt:
            card["total_size"] = receipt["total_size"]
        card["available"] = receipt.get("available", True)
        card["external"] = receipt.get("kind") == "external"
        for key, target in (("cover", "image"), ("background", "backgroundImage")):
            path = self.storage.cache / "catalog" / (card.get(key, {}).get("sha256", "missing") + ".image")
            card[target] = path.as_uri() if path.is_file() else (self.storage.resources / "assets/local.svg").as_uri()
        return card

    def invalidate_local_cards(self):
        self._local_cards = None

    @Property("QVariantList", notify=libraryChanged)
    def localGames(self):
        if self._local_cards is None:
            self._local_cards = [self.local_card(receipt) for receipt in self._installed]
        return self._local_cards

    @Property(str, notify=libraryChanged)
    def librarySort(self):
        value = self.settings.get("library_sort", "recent")
        return value if value in ("recent", "added", "name", "genre") else "recent"

    @Property(str, notify=settingsChanged)
    def gamesView(self):
        return "grid" if self.settings.get("games_view") == "grid" else "ribbon"

    @Slot(str, result=bool)
    def setGamesView(self, view):
        if view not in ("grid", "ribbon"):
            return False
        updated = dict(self.settings, games_view=view)
        try:
            self.storage.write_settings(updated)
        except OSError:
            self._operation_status = tr("Could not save the library view. Check access to the NGS folder.")
            self.operationChanged.emit()
            return False
        self.settings = updated
        self.settingsChanged.emit()
        self.schedule_backup()
        return True

    @Slot(str, result=bool)
    def setLibrarySort(self, order):
        if order not in ("recent", "added", "name", "genre"):
            return False
        updated = dict(self.settings, library_sort=order)
        try:
            self.storage.write_settings(updated)
        except OSError:
            self._operation_status = tr("Could not save the sort order. Check access to the NGS folder.")
            self.operationChanged.emit()
            return False
        self.settings = updated
        self._installed = sort_installs(self._installed, order)
        self.libraryChanged.emit()
        self.schedule_backup()
        return True

    @Property("QStringList", notify=libraryChanged)
    def installedIds(self):
        return list({identity for receipt in self._installed if not receipt.get("removing")
                     for identity in (receipt["card"]["id"], receipt.get("catalog_id", receipt["card"]["id"])) if identity})

    @Property("QStringList", notify=libraryChanged)
    def playedIds(self):
        return list({identity for receipt in self._installed if receipt.get("played_at")
                     for identity in (receipt["card"]["id"], receipt.get("catalog_id", "")) if identity})

    @Property("QStringList", notify=libraryChanged)
    def unavailableIds(self):
        available = {identity for r in self._installed if r.get("available", True)
                     for identity in (r["card"]["id"], r.get("catalog_id", ""))}
        return [identity for r in self._installed if not r.get("available", True)
                for identity in (r["card"]["id"], r.get("catalog_id", "")) if identity and identity not in available]

    @Property("QStringList", notify=libraryChanged)
    def externalIds(self):
        return [r["card"]["id"] for r in self._installed if r.get("kind") == "external"]

    @Property("QStringList", notify=libraryChanged)
    def removableIds(self):
        return [r["card"]["id"] for r in self._installed if r.get("managed") is True]

    @Property("QStringList", notify=libraryChanged)
    def removingIds(self):
        return [r["card"]["id"] for r in self._installed if r.get("removing")]

    @Slot(str, result=str)
    def installLocation(self, identity):
        receipt = next((r for r in self._installed if r["card"]["id"] == identity), None)
        if not receipt:
            return ""
        try:
            return str(self.library.install_path(receipt))
        except (OSError, ValueError):
            return receipt.get("location", "")

    @Slot(str, result=str)
    def executableLocation(self, identity):
        from ngs.install import safe_path
        receipt = next((r for r in self._installed if r["card"]["id"] == identity), None)
        if not receipt:
            return ""
        try:
            return str(safe_path(self.library.install_path(receipt), receipt["executable"]))
        except (OSError, ValueError, KeyError):
            return ""

    @Slot(str)
    def revealGame(self, identity):
        path = self.executableLocation(identity)
        if not path or not Path(path).is_file():
            self._operation_status = tr("The game folder or executable is unavailable.")
            self.operationChanged.emit()
            return
        # A shell item identifier keeps commas and quotes in paths out of Explorer's command parser.
        from ngs.process import reveal_file
        try:
            reveal_file(path)
        except OSError as error:
            self._operation_status = tr("Could not open the folder: ") + str(error)
            self.operationChanged.emit()

    def launch_options_identity(self, identity):
        receipt = next((r for r in self._installed if identity == r["card"]["id"]), None)
        receipt = receipt or next((r for r in self._installed if identity == r.get("catalog_id")), None)
        if receipt:
            return receipt["card"]["id"]
        if any(g["id"] == identity for g in self._games):
            return identity
        raise ValueError(tr("The game was not found. Refresh the library."))

    @Slot(str, result="QVariantMap")
    def launchOptions(self, identity):
        try:
            identity = self.launch_options_identity(identity)
            return dict(arguments=self.library.launch_arguments(identity), ready=True, status="")
        except (ValueError, OSError, sqlite3.Error) as error:
            return dict(arguments="", ready=False, status=tr("Could not read settings: ") + str(error))

    @Slot(str, str, result=bool)
    def setLaunchArguments(self, identity, value):
        try:
            if self.process is not None or self._closing:
                raise ValueError(tr("Close the game before changing launch options."))
            self.library.set_launch_arguments(self.launch_options_identity(identity), value)
            self._operation_status = tr("Launch options saved.")
            self.schedule_backup()
            self.operationChanged.emit()
            return True
        except (ValueError, OSError, sqlite3.Error) as error:
            self._operation_status = tr("Could not save settings: ") + str(error)
            self.operationChanged.emit()
            return False

    @Property(bool, notify=runningChanged)
    def launchingGame(self):
        return self._launching

    @Slot(str, result='QVariantMap')
    def gameStatistics(self, identity):
        try:
            return dict(self.play_time.totals(self.launch_options_identity(identity)), status='')
        except (ValueError, OSError, sqlite3.Error) as error:
            return dict(total_seconds=0, launches=0, longest_seconds=0, status=tr('Could not read statistics: ') + str(error))

    @Property(bool, notify=freezeChanged)
    def gameFrozen(self):
        return self.process is not None and self.process.frozen is True

    @Property("QVariantMap", notify=runningChanged)
    def runningState(self):
        return dict(self.players.snapshot(self.play_time.snapshot()), running=self.process is not None, id=self._running_id,
                    name=self._running_name or self._operation_name, frozen=self.gameFrozen,
                    closing=self._terminating, memory_busy=self._memory_worker is not None, memory_status=self._memory_status,
                    error=self._game_control_error or self._statistics_error)

    @Slot(str, result=bool)
    def resetGameStatistics(self, session_id):
        if self.process is None or self._terminating or self._sleeping or self._closing: return False
        try:
            self.play_time.reset(session_id)
            self._statistics_error = ""
        except (OSError, sqlite3.Error, ValueError) as error:
            self._statistics_error = tr("Could not reset statistics: ") + str(error)
            self.runningChanged.emit()
            return False
        self.runningChanged.emit()
        return True

    @Slot(str, result=bool)
    def resetLocalGameStatistics(self, identity):
        if self.process is not None or self._closing: return False
        try:
            self.play_time.reset_game(self.launch_options_identity(identity))
        except (OSError, sqlite3.Error, ValueError) as error:
            self._operation_status = tr('Could not reset statistics: ') + str(error)
            self.operationChanged.emit()
            return False
        self.runningChanged.emit()
        return True

    @Property(QObject, constant=True)
    def playerAccount(self): return self.players

    @Property('QStringList', notify=runningChanged)
    def runningIds(self):
        if self.process is None: return []
        receipt = next((r for r in self._installed if r['card']['id'] == self._running_id), {})
        return list({value for value in (self._running_id, receipt.get('catalog_id', '')) if value})

    @Property('QStringList', notify=freezeChanged)
    def frozenIds(self):
        if not self.gameFrozen: return []
        return self.runningIds

    @Slot()
    def showGameLibrary(self):
        if not self.gameFrozen or self._sleeping or self._memory_worker is not None: return
        try:
            self.process.park_windows()
            self.gameLibraryRequested.emit()
        except OSError as error:
            self._game_control_error = tr("Could not return to the launcher: ") + str(error)
            self.runningChanged.emit()

    @Slot()
    def trimGameMemory(self):
        if not self.gameFrozen or self._memory_worker is not None or self._terminating: return
        worker = GameMemoryWorker(self.process, self)
        self._memory_worker = worker
        self._memory_status = tr("Trimming frozen game memory…")
        worker.result.connect(self.memory_trimmed)
        worker.finished.connect(self.memory_finished)
        worker.start(); self.runningChanged.emit()

    def memory_trimmed(self, result):
        if isinstance(result, str): self._memory_status = result
        else:
            self._memory_status = tr("Game working set: %d → %d MiB. Unfreezing may require reloading data.") % (result['before']//1048576, result['after']//1048576)
            if result['failed']: self._memory_status += tr(" Could not trim %d processes.") % result['failed']
        self.runningChanged.emit()

    def memory_finished(self):
        self._memory_worker.deleteLater(); self._memory_worker = None; self.runningChanged.emit()

    def update_running(self, force=False, reason=None):
        try:
            had_session = self.play_time.session is not None
            self.play_time.checkpoint(force=force, reason=reason)
            if reason is not None: self.players.sync()
            if had_session: self._statistics_error = ""
        except Exception as error:
            message = tr("Could not save statistics: ") + str(error)
            if message != self._statistics_error: logging.exception(message)
            self._statistics_error = message
        # Do not re-evaluate hidden QML statistics (and their database reads)
        # every second. State transitions and the next activation still notify.
        if force or reason is not None or self._ui_active:
            self.runningChanged.emit()

    @Slot()
    def toggleGameFreeze(self):
        self._auto_foreground = False
        if self.process is None or self._terminating or self._memory_worker is not None: return
        try:
            if self.gameFrozen: self.process.thaw()
            else:
                self._memory_status = ''
                self.process.freeze()
            self.play_time.pause(self.gameFrozen)
            self._game_control_error = ""
            self.frozen_updated()
            if self.gameFrozen and self.settings.get("auto_trim_game_memory", False) is True:
                QTimer.singleShot(50, self.trimGameMemory)
            self.gamePanelRequested.emit()
        except OSError as error:
            self.play_time.pause(self.gameFrozen)
            self._game_control_error = tr("Could not change the frozen state: ") + str(error)
            self.update_running(force=True)
            self.freezeChanged.emit()

    @Slot()
    def minimizeGame(self, show_panel=True):
        self._auto_foreground = False
        if self.process is None or self._terminating: return
        try:
            self.process.park_windows()
            self._game_control_error = ""
            if show_panel: self.gamePanelRequested.emit()
        except OSError as error:
            self._game_control_error = tr("Could not minimize the game: ") + str(error)
        self.runningChanged.emit()

    @Slot(result=bool)
    def forceCloseGame(self):
        if self.process is None or self._terminating or self._memory_worker is not None: return False
        try:
            self.process.terminate()
            self._terminating = True
            self._game_control_error = ""
            self.runningChanged.emit()
            return True
        except OSError as error:
            self._game_control_error = tr("Could not close the game: ") + str(error)
            self.runningChanged.emit()
            return False

    def freeze_for_sleep(self):
        if self._launching:
            self.cancelOperation()
        self._auto_foreground = False
        self._sleeping = True
        process = self.process
        crash_capture.mark('power.sleep', game=self._running_id if process is not None else None)
        if process is not None:
            self.play_time.pause(True)
            if self.settings.get("freeze_on_sleep", True) is not False:
                process.freeze()
                if self.settings.get("auto_trim_game_memory", False) is True:
                    QTimer.singleShot(50, self.trimGameMemory)

    @Slot()
    def frozen_updated(self):
        self._power_error = ""
        self.play_time.pause(self.gameFrozen or self._sleeping)
        self.update_running(force=True)
        self.sync_power_timers()
        self.freezeChanged.emit()

    @Slot()
    def resume_play_clock(self):
        self._sleeping = False
        crash_capture.mark('power.wake', game=self._running_id if self.process is not None else None)
        self.play_time.pause(self.gameFrozen)
        self.update_running(force=True)
        self.sync_power_timers()

    @Property(str, notify=freezeChanged)
    def powerError(self):
        return self._power_error

    @Slot()
    def dismissPowerError(self):
        self._power_error = ""
        self.freezeChanged.emit()

    @Slot(str)
    def power_failed(self, message):
        self._power_error = message
        # A failed native freeze can still be followed by display sleep.
        self.sync_power_timers()
        self.freezeChanged.emit()
        self._operation_status = message
        self.operationChanged.emit()

    @Slot()
    def thawGame(self):
        if self._memory_worker is not None: return
        if self.process is None or self._terminating:
            return
        try:
            self.process.thaw()
            self.play_time.pause(False)
            self.update_running(force=True)
            self._power_error = self._game_control_error = ""
        except OSError as error:
            self.play_time.pause(self.gameFrozen)
            self.power_failed(tr("Could not return to the game: ") + str(error))
            return
        self.freezeChanged.emit()
        self._auto_foreground = False
        self.gameStarted.emit()
        try:
            self.process.foreground()
        except OSError as error:
            self.power_failed(tr("Could not return to the game: ") + str(error))

    @Slot()
    def returnToGame(self):
        self.thawGame()

    @Slot(str)
    def removeGame(self, identity):
        if self.game_worker is not None or self.process is not None or identity not in self.removableIds + self.externalIds:
            return
        receipt = next(r for r in self._installed if r["card"]["id"] == identity)
        self.start_operation(receipt["card"], "forget" if identity in self.externalIds else "remove")

    @Slot(str)
    def forgetGame(self, identity):
        if self.operationBusy or self.process is not None or identity in self.removingIds:
            return
        receipt = next((r for r in self._installed if r["card"]["id"] == identity), None)
        if receipt:
            self.start_operation(receipt["card"], "forget")

    @Property("QVariantMap", notify=scannerChanged)
    def scannerState(self):
        return self._scanner

    @Slot(str)
    @Slot(str, bool)
    def browseLocalFolder(self, path, allow_network=False):
        if self.operationBusy or self.process is not None:
            return
        self._scanner = dict(self._scanner, stage="browse", store_review='', store_scan=False, store_name='', status=tr("Reading folders…"))
        self.scannerChanged.emit()
        self.start_operation({"id": "", "name": tr("Choose folder", "Выбор папки"), "path": path, "allow_network": allow_network}, "browse")

    @Slot(str)
    def scanLocalFolder(self, path):
        if self.operationBusy or self.process is not None:
            return
        self._scanner = dict(self._scanner, stage="browse", status=tr("Searching for executables in the selected folder…"))
        self.scannerChanged.emit()
        self.start_operation({"id": "", "name": tr("Local game search"), "path": path}, "scan")

    @Slot()
    def scanStoreGames(self):
        if self.operationBusy or self.process is not None: return
        self._installer_import = None
        self._scanner.update(stage='stores', store_scan=True, store_review='', store_name='', last_added='',
                             status=tr("Searching installed Steam / GOG / Epic Games Store / Battle.net games…"))
        self.scannerChanged.emit()
        self.start_operation(dict(id='', name=tr("Store game scan")), 'store_scan')

    @Slot()
    @Slot(str)
    def importStoreGames(self, path=''):
        if self.operationBusy or self.process is not None or not self._scanner['store_games']: return
        self._scanner.update(store_scan=True, store_review='', last_added='')
        rows = [row for row in self._scanner['store_games'] if not path or row['path'] == path]
        self.start_operation(dict(id='', name=tr("Adding store games"), rows=rows, cards=self._games,
                                 url='' if self.localMode else self.settings['tactic_url'], offline=not self.localMode and self._catalog_online is False), 'store_import')

    @Slot(str)
    def reviewStoreGame(self, path):
        if self.operationBusy: return
        row = next((row for row in self._scanner['store_games'] if row['path'] == path), None)
        if not row or row['state'] in ('added', 'already'): return
        if row.get('game_id'):
            self.importStoreGames(path)
            return
        self._scanner.update(stage='exes', path=path, store_review=path, store_name=row['name'], batch_review='',
            candidates=row['candidates'] or ([dict(path=row['preferred_exe'], utility=False)] if row['preferred_exe'] else []),
            status=tr("Choose an executable for “%s”. Store files will be preserved.") % row['name'])
        self.scannerChanged.emit()
        if not self._scanner['candidates']: self.scanLocalFolder(path)

    @Slot()
    def showStoreGames(self):
        if not self.operationBusy:
            self._scanner.update(stage='stores', store_review='', store_name='')
            self.scannerChanged.emit()

    @Slot(str)
    def scanLocalCollection(self, path):
        if self.operationBusy or self.process is not None or not path:
            return
        self._scanner = dict(self._scanner, stage="batch", path=path, batch_root=path, batch_review="",
                             store_scan=False, store_review='', store_name='',
                             last_added="",
                             batch_rows=LocalScanJournal(self.storage, self.settings["tactic_url"]).rows(path),
                             status=tr("Checking the library folder…"))
        self.scannerChanged.emit()
        self.start_operation({"id": "", "name": tr("Library scan"), "path": path,
                              "cards": [dict(card) for card in self._games], "url": "" if self.localMode else self.settings["tactic_url"],
                              "offline": not self.localMode and self._catalog_online is False}, "scan_many")

    @Slot(dict)
    def collection_progressed(self, event):
        field = 'store_games' if self._scanner.get('store_scan') else 'batch_rows'
        rows = self._scanner[field].copy()
        if "row" in event:
            row = event["row"]
            if row.get("state") in ("added", "artwork") and row.get("game_id"):
                self._scanner["last_added"] = row["game_id"]
            index = next((index for index, existing in enumerate(rows) if path_key(existing['path']) == path_key(row['path'])), None)
            if index is None: rows.append(row)
            else: rows[index] = row
        self._scanner = dict(self._scanner, **{field: rows}, status=event["status"])
        self._operation_status = event["status"]
        if event.get("total"):
            self._operation_progress = event["done"] / event["total"]
        self.scannerChanged.emit()
        self.operationChanged.emit()

    @Slot(str)
    def reviewLocalGame(self, path):
        if self.operationBusy:
            return
        row = next((row for row in self._scanner["batch_rows"] if row["path"] == path), None)
        if not row or row["state"] in ("added", "already"):
            return
        self._scanner = dict(self._scanner, stage="exes", path=path, batch_review=path,
                             candidates=row["candidates"], status=tr("Choose the game's executable, then confirm the artwork."))
        self.scannerChanged.emit()
        if not row["candidates"]:
            self.scanLocalFolder(path)

    @Slot()
    def showCollectionResults(self):
        if not self.operationBusy:
            self._scanner = dict(self._scanner, stage="batch", path=self._scanner["batch_root"], batch_review="")
            self.scannerChanged.emit()

    @Slot(str)
    def selectLocalExecutable(self, executable):
        if self.operationBusy or not any(c["path"] == executable for c in self._scanner["candidates"]):
            return
        matches = [self._installer_import] if self._installer_import else matching_cards(self._scanner["path"], executable, self._games)
        self._scanner = dict(self._scanner, stage="confirm", exe=executable, matches=matches,
                             manual_metadata_id='',
                             name=self._installer_import["name"] if self._installer_import else self._scanner.get('store_name') or Path(self._scanner["path"]).name,
                             status=tr("Confirm the title and executable. Files are preserved."))
        self.scannerChanged.emit()
        if not matches and not self._scanner.get('store_review') and self.settings.get("resolve_local_metadata", True):
            self.findMetadata("")

    @Property("QVariantMap", notify=metadataChanged)
    def metadataState(self):
        return self._metadata

    @Slot(str)
    def findMetadata(self, identity):
        if self.operationBusy or self.process is not None:
            return
        receipt = next((r for r in self._installed if r["card"]["id"] == identity and r.get("kind") == "external"), None)
        if identity and receipt is None:
            return
        query = search_title(receipt["card"]["name"] if receipt else self._scanner.get('store_name') or Path(self._scanner["path"]).name)
        self._metadata_preview = None
        self._metadata = {"query": query, "results": [], "local_id": identity, "status": tr("Searching…")}
        self.metadataChanged.emit()
        self.metadataRequested.emit()
        self.searchMetadata(query)

    @Slot(str)
    def searchMetadata(self, query):
        if self.operationBusy:
            return
        self._metadata_preview = None
        self._metadata = dict(self._metadata, query=query, results=[], cancelling=False, selected_name="", preview={}, applying=False,
                              status=tr("Searching providers · Steam / GOG / Epic / Lutris / Wikipedia…"))
        self.metadataChanged.emit()
        self.start_operation({"id": "", "name": query, "value": query, "url": "" if self.localMode else self.settings["tactic_url"],
                              "offline": not self.localMode and self._catalog_online is False}, "metadata_search")

    @Slot(str)
    def resolveMetadata(self, app_id):
        self._resolve_metadata(app_id, False)

    @Slot(str)
    def previewMetadata(self, app_id):
        self._resolve_metadata(app_id, True)

    def _resolve_metadata(self, app_id, preview_only):
        if self.operationBusy or not any(r["app_id"] == app_id for r in self._metadata["results"]):
            return
        name = next(r["name"] for r in self._metadata["results"] if r["app_id"] == app_id)
        self._metadata_preview = None
        self._metadata = dict(self._metadata, cancelling=False, selected_name=name, preview={}, applying=False, status=tr("Loading description and images…"))
        self.metadataChanged.emit()
        self.start_operation({"id": "", "name": self._metadata["query"], "value": app_id,
                              "preview_only": preview_only,
                              "local_id": self._metadata["local_id"], "url": "" if self.localMode else self.settings["tactic_url"],
                              "offline": not self.localMode and self._catalog_online is False}, "metadata_resolve")

    @Slot()
    def clearMetadataPreview(self):
        if self.operationBusy: return
        self._metadata_preview = None
        self._metadata = dict(self._metadata, preview={}, applying=False, status=tr('Choose a game in the search results.'))
        self.metadataChanged.emit()

    @Slot(result=bool)
    def applyMetadataPreview(self):
        if self.operationBusy or self._metadata_preview is None: return False
        if self.loginRequired:
            self._metadata = dict(self._metadata, status=tr('Sign in to NGS in Settings.'))
            self.metadataChanged.emit()
            return False
        self._metadata = dict(self._metadata, applying=True, status=tr('Saving artwork…'))
        self.metadataChanged.emit()
        self.start_operation(dict(id='', name=self._metadata_preview['name'], metadata=dict(self._metadata_preview),
                                  local_id=self._metadata['local_id']), 'metadata_apply')
        return self.game_worker is not None

    @Slot(result=bool)
    def editCustomMetadata(self):
        if self.game_worker:
            if self.game_worker.mode not in ('metadata_search', 'metadata_resolve'): return False
            self.cancelOperation()
        receipt = next((r for r in self._installed if r['card']['id'] == self._metadata['local_id']), None)
        card = receipt['card'] if receipt else next(iter(self._scanner['matches']), {})
        self._metadata_custom_seed = dict(card, id=receipt.get('catalog_id') or card['id']) if receipt else dict(card)
        artwork = self.local_card(dict(card=card)) if card else {}
        import uuid
        self._metadata = dict(self._metadata, custom_status='', custom_saving=False,
                              custom=dict(preview=uuid.uuid4().hex, card=dict(card, name=card.get('name') or self._scanner.get('name') or self._metadata['query']),
                                          artwork=dict(cover=artwork.get('image', ''), background=artwork.get('backgroundImage', ''))))
        self.metadataChanged.emit()
        return True

    @Slot(str, str, str, str)
    def saveCustomMetadata(self, name, description, cover, background):
        if self.operationBusy or self.loginRequired:
            self._metadata = dict(self._metadata, custom_status=tr('Wait for the current operation to finish, then try saving again.'))
            self.metadataChanged.emit()
            return
        if not self._metadata.get('custom'): return
        self._metadata = dict(self._metadata, custom_saving=True, custom_status=tr('Saving artwork…'))
        self.metadataChanged.emit()
        self.start_operation(dict(id='', name=name, description=description, cover=cover, background=background,
                                  seed=dict(self._metadata_custom_seed), local_id=self._metadata['local_id']), 'metadata_custom')

    @Slot(str, str)
    def confirmLocalGame(self, name, catalog_id):
        if self.operationBusy or self.process is not None or self._scanner["stage"] != "confirm":
            return
        card = next((g for g in self._scanner["matches"] if g["id"] == catalog_id), None)
        if self._scanner.get('store_review'):
            row = next(row for row in self._scanner['store_games'] if row['path'] == self._scanner['store_review'])
            row = dict(row, name=name, preferred_exe=self._scanner['exe'], metadata=dict(card) if card else None,
                       metadata_manual=bool(card and card['id'] == self._scanner.get('manual_metadata_id')))
            self._scanner.update(store_scan=True, last_added='')
            self.start_operation(dict(id='', name=name, rows=[row], cards=self._games,
                                     url='' if self.localMode else self.settings['tactic_url'], offline=not self.localMode and self._catalog_online is False), 'store_import')
            return
        self.start_operation({"id": "", "name": name, "path": self._scanner["path"],
                              "exe": self._scanner["exe"], "metadata": dict(card) if card else None,
                              'metadata_manual': bool(card and card['id'] == self._scanner.get('manual_metadata_id'))}, "import")

    @Slot()
    def refreshLocalLibrary(self):
        if not self.operationBusy:
            self._installer_packages = self.installer_library.reconcile()
            self.installersChanged.emit()
            self._installed = self.library.reconcile(self.librarySort)
            self.libraryChanged.emit()
            self.gamesChanged.emit()
            self.ensure_local_artwork()

    @Property(bool, notify=operationChanged)
    def operationBusy(self):
        return self.game_worker is not None or self._launching

    @Property(bool, notify=operationChanged)
    def downloadActive(self):
        return self.game_worker is not None and self.game_worker.mode in ("install", "discard")

    @Property("QVariantList", notify=downloadsChanged)
    def downloadJobs(self):
        rows = []
        for job in self.download_queue.jobs():
            folder = Path(job["games"])
            if not folder.is_absolute():
                folder = self.storage.root / folder
            if job["card"].get("kind") == "installer":
                folder = folder / ".ngs-installers"
            # Windows file IDs may be 128-bit; only presentation fields can cross QVariant.
            visible = {key: job[key] for key in ('state', 'message', 'progress', 'discard', 'completed_bytes', 'total_bytes', 'network_bytes', 'network_total') if key in job}
            rows.append(dict(visible, card=job['card'], id=job["card"]["id"], name=job["card"]["name"], destination=str(folder / game_folder_name(job["card"])),
                     state="running" if self.downloadActive and job["card"]["id"] == self._operation_game and job["state"] == "queued" else job["state"],
                     progress=self._operation_progress if self.downloadActive and job["card"]["id"] == self._operation_game
                     else job["progress"]))
        return rows

    @Property("QVariantMap", notify=downloadStatisticsChanged)
    def downloadStatistics(self):
        return self._download_statistics

    @Slot(float, float)
    def download_io_updated(self, network, disk):
        note_activity()
        self._io_bytes = [network, disk]

    def set_ui_active(self, active):
        active = bool(active)
        if active == self._ui_active:
            return
        self._ui_active = active
        self.sync_download_timer()
        if active:
            self.runningChanged.emit()

    def sync_download_timer(self):
        sampling = (self._io_active and self._ui_active and not self._sleeping
                    and not self._closing and self.game_worker is not None
                    and not self.game_worker.cancel.is_set())
        if sampling and not self.download_timer.isActive():
            # A hidden interval is not a one-second throughput sample.
            self._io_sample, self._io_time = self._io_bytes[:], time.monotonic()
            self.download_timer.start()
        elif not sampling:
            self.download_timer.stop()

    def sync_power_timers(self):
        # The hang watchdog must not report a suspended process; it stays armed while closing.
        crash_capture.set_sleeping(self._sleeping)
        if self._sleeping or self._closing:
            self.update_timer.stop()
            self.offline_timer.stop()
            self.process_timer.stop()
            self.players.timer.stop()
        else:
            if not self.players.timer.isActive():
                self.players.timer.start()
            if self._automatic_updates and not self.update_timer.isActive():
                self.update_timer.start(3500)
            if not self.localMode and not self._demo and not self.offline_timer.isActive():
                self.offline_timer.start()
            if self.process is not None and not self.process_timer.isActive():
                self.process_timer.start()
        self.sync_download_timer()

    @Slot()
    def sample_download_io(self):
        now = time.monotonic()
        active = self._io_active and self.game_worker is not None and not self.game_worker.cancel.is_set()
        rates = [(value - previous) / max(.001, now - self._io_time) if active else 0.0
                 for value, previous in zip(self._io_bytes, self._io_sample)]
        self._io_sample, self._io_time = self._io_bytes[:], now
        if active or self._download_statistics["network"] or self._download_statistics["disk"]:
            history = (self._download_statistics["history"] + [rates])[-60:]
            self._download_statistics = dict(self._download_statistics, network=rates[0], disk=rates[1], history=history)
            self.downloadStatisticsChanged.emit()
            self.downloadsChanged.emit()
        self.sync_download_timer()

    @Property("QStringList", notify=downloadsChanged)
    def downloadIds(self):
        return [job["card"]["id"] for job in self.download_queue.jobs()]

    @Slot()
    def pumpCleanup(self):
        if self.localMode: return
        if self._closing or self._sleeping or self.cleanup_worker is not None or self.game_worker is not None:
            return
        if not self.download_queue.cleanup_due(): return
        self.cleanup_worker = TransferCleanupWorker(self)
        self.cleanup_worker.finished.connect(self.cleanup_finished)
        self.cleanup_worker.start()

    @Slot()
    def cleanup_finished(self):
        self.cleanup_worker.deleteLater()
        self.cleanup_worker = None

    @Slot()
    def pumpDownloads(self):
        if self._closing or self.operationBusy or self.process is not None:
            return
        jobs = self.download_queue.jobs()
        job = next((j for j in jobs if j["state"] == "cancelling"), None)
        if (self.localMode or self.loginRequired) and not job: return
        job = job or next((j for j in jobs if j["state"] == "queued" and j.get("retry_at", 0) <= time.time()), None)
        if job:
            self.start_operation(job["card"], "discard" if job["state"] == "cancelling" else "install")

    @Slot(str)
    def pauseDownload(self, identity):
        job = self.download_queue.get(identity)
        if not job or job["state"] not in ("queued", "running"):
            return
        active = self.downloadActive and self._operation_game == identity
        self.download_queue.set_state(identity, "pausing" if active else "paused", tr("Stopping…") if active else tr("Paused"))
        if active:
            self.game_worker.cancel.set()
        self.downloadsChanged.emit()

    @Slot(str)
    def resumeDownload(self, identity):
        job = self.download_queue.get(identity)
        if not job or job["state"] not in ("paused", "error") or (self.downloadActive and self._operation_game == identity):
            return
        self.download_queue.set_state(identity, "cancelling" if job.get("discard") else "queued", tr("Retry operation"),
                                      source=job["source"])
        self.downloadsChanged.emit()
        self.pumpDownloads()

    @Slot(str)
    def cancelDownload(self, identity):
        if not self.download_queue.get(identity):
            return
        self.download_queue.set_state(identity, "cancelling", tr("Canceling and removing partial files…"))
        if self.downloadActive and self._operation_game == identity:
            self.game_worker.cancel.set()
        self.downloadsChanged.emit()
        self.pumpDownloads()

    @Property(str, notify=operationChanged)
    def operationStatus(self):
        return self._operation_status

    @Property(float, notify=operationChanged)
    def operationProgress(self):
        return self._operation_progress

    @Property(str, notify=operationChanged)
    def operationGame(self):
        return self._operation_name

    @Property(str, notify=operationChanged)
    def operationId(self):
        return self._operation_game

    @Slot(str)
    def performGameAction(self, identity):
        if self.process is not None:
            if identity in self.runningIds or identity in self.frozenIds:
                self.returnToGame()
            else:
                self._operation_status = tr("Close the current game first, or return to the frozen game.")
                self.operationChanged.emit()
            return
        if self.game_worker is not None and not self.downloadActive:
            return
        if identity in self.removingIds:
            self._operation_status = tr("Finish removing this game first.")
            self.operationChanged.emit()
            return
        installed = next((r["card"] for r in self._installed if r.get("available", True)
                          and identity in (r["card"]["id"], r.get("catalog_id", ""))), None)
        if identity in self.unavailableIds:
            self._operation_status = tr("The folder or executable is unavailable. Connect the drive and refresh My games (Y / R).")
            self.operationChanged.emit()
            return
        card = installed or next((g for g in self._games if g["id"] == identity), None)
        if not card:
            return
        if not installed and (not (card.get("manifest") or card.get("delivery") == "on_request") or self.cache is None):
            self._operation_status = tr("No file manifest has been published for this game yet.")
            self.operationChanged.emit()
            return
        if installed:
            if not self.operationBusy:
                self.start_operation(card, "launch")
        else:
            if identity in self.downloadIds:
                return
            self.installRequested.emit(identity, card["name"])

    @Property("QVariantMap", notify=installPreviewChanged)
    def installPreview(self):
        return self._install_preview

    @Slot(str, str)
    def previewInstall(self, identity, directory):
        self.cancelInstallPreview()
        card = next((g for g in self._games + self._installer_games if g["id"] == identity), None)
        self._install_preview = dict(busy=False, can_install=False, game_id=identity, directory=directory,
                                    kind=card.get("kind", "game") if card else "game",
                                    status=tr("Choose an available installation folder."))
        if (not self.localMode and not self.loginRequired and not self._closing and card and self.cache is not None
                and path_key(directory) in {path_key(p) for p in self.installDirectories}):
            self._preview_card = dict(card)
            self._preview_cache = self.cache
            if "total_size" in card:
                self._install_preview["size"] = card["total_size"]
            self._install_preview.update(busy=True, status=tr("Checking free space…") if "total_size" in card else tr("Fetching game size and checking storage…"))
            self._preview_pending = (dict(card), self.cache, directory, self._preview_serial)
            self.start_preview()
        self.installPreviewChanged.emit()

    @Slot()
    def cancelInstallPreview(self):
        self._preview_serial += 1
        self._preview_pending = None
        self._preview_card = None
        self._preview_cache = None
        self._install_preview.update(busy=False, can_install=False)
        if self.preview_worker:
            self.preview_worker.cancel.set()
        self.installPreviewChanged.emit()

    def start_preview(self):
        if self.preview_worker is not None or self._preview_pending is None or self._closing:
            return
        card, cache, directory, serial = self._preview_pending
        self._preview_pending = None
        self.preview_worker = InstallPreviewWorker(self.storage, card, cache, directory, serial, self, self.download_queue)
        self.preview_worker.loaded.connect(self.preview_loaded)
        self.preview_worker.finished.connect(self.preview_finished)
        self.preview_worker.start()

    @Slot(dict)
    def preview_loaded(self, result):
        worker = self.sender()
        if worker.serial == self._preview_serial and worker.cache is self.cache and not self._closing:
            self._install_preview.update(result, busy=False)
            self.installPreviewChanged.emit()

    @Slot()
    def preview_finished(self):
        self.preview_worker.deleteLater()
        self.preview_worker = None
        self.start_preview()

    @Property(str, notify=settingsChanged)
    def updateSource(self):
        from ngs.github_updates import OFFICIAL_UPDATE_URL
        return self.settings.get('update_url') or OFFICIAL_UPDATE_URL

    @Property("QVariantMap", notify=updatesChanged)
    def updatesState(self):
        return dict(self._update, server=self.updateSource,
                    can_install=self._update["available"] and not self._update["busy"]
                    and not self._closing and not self._first_start_pending
                    and self.process is None and self._update["current"] != "0.0.0.0")

    @Property(QObject, constant=True)
    def remoteCatalogizer(self): return self.catalogizer

    @Slot()
    def checkUpdates(self):
        if self._automatic_updates: self.update_timer.start(3600000)
        if self.update_worker is not None or self._closing or self._sleeping:
            if self._sleeping: self.update_timer.stop()
            return
        self._update.update(busy=True, status=tr("Checking for updates…"), progress=0.0)
        event('update.check', server=self.updateSource, current=self._update['current'])
        self.update_worker = UpdateWorker(self.storage, self.updateSource, None, self)
        self.connect_update_worker()

    @Slot()
    def installUpdate(self):
        if not self.updatesState["can_install"] or self.update_worker is not None:
            return
        self._update.update(busy=True, status=tr("Downloading update…"), progress=0.0)
        self.update_worker = UpdateWorker(self.storage, self.updateSource, self._update_envelope, self)
        self.connect_update_worker()

    def connect_update_worker(self):
        self.update_worker.loaded.connect(self.update_loaded)
        self.update_worker.failed.connect(self.update_failed)
        self.update_worker.progressed.connect(self.update_progress)
        self.update_worker.finished.connect(self.update_finished)
        self.updatesChanged.emit()
        self.update_worker.start()

    @Slot(object)
    def update_loaded(self, result):
        if self.update_worker.cancel.is_set() or self._closing or self.update_worker.url != self.updateSource:
            return
        if self.update_worker.envelope is None:
            self._update_envelope = result
            release = json.loads(result["payload"]) if result else {}
            self._update.update(available=bool(result), version=release.get("version", ""), notes=release.get("notes", ""),
                                status=tr("A new version is available.") if result else tr("You have the latest version."))
        else:
            self._update_install_pending = True
            self._update["status"] = tr("Package verified. Restarting NGS…")
        self.updatesChanged.emit()

    @Slot(str)
    def update_failed(self, message):
        event('update.error', error=message)
        self._update_install_pending = False
        self._update["status"] = message
        self.updatesChanged.emit()

    @Slot(float, str)
    def update_progress(self, fraction, message):
        self._update.update(progress=fraction, status=message)
        self.updatesChanged.emit()

    @Slot()
    def update_finished(self):
        changed_address = self.update_worker.url != self.updateSource
        cancelled = self.update_worker.cancel.is_set()
        self.update_worker.deleteLater()
        self.update_worker = None
        self._update["busy"] = False
        if cancelled:
            self._update["status"] = tr("Update canceled. Application files preserved.")
        self.updatesChanged.emit()
        if changed_address and not self._closing and self._automatic_updates: self.update_timer.start(1)
        if self._update_install_pending and not cancelled and not self._closing:
            self._update_install_pending = False
            try:
                if self.process is not None:
                    raise ValueError(tr("Close the game before installing an update."))
                start_installer(self.storage)
                self.quit()
            except (ValueError, OSError) as error:
                self.update_failed(str(error))

    @Slot()
    def cancelUpdate(self):
        self._update_install_pending = False
        if self.update_worker:
            self.update_worker.cancel.set()
            self._update["status"] = tr("Canceling update…")
            self.updatesChanged.emit()

    @Slot(str, str, result=bool)
    def queueDownload(self, identity, directory):
        if self.localMode or self.loginRequired: return False
        try:
            card = next((g for g in self._games + self._installer_games if g["id"] == identity), None)
            if self._closing or self.process or not card or self.cache is None or identity in self.installedIds:
                return False
            if path_key(directory) not in {path_key(p) for p in self.installDirectories}:
                raise ValueError(tr("Choose an installation folder from Settings."))
            preview = self._install_preview
            if (preview.get("game_id") != identity or path_key(preview.get("directory", "")) != path_key(directory)
                    or not preview.get("can_install") or preview.get("busy") or self._preview_card != card
                    or self._preview_cache is not self.cache):
                raise ValueError(tr("Check the game size and free space in the selected folder first."))
            sources = game_sources(self.settings, card)
            self.download_queue.add(card, sources[0], self.cache.origin, directory, source_candidates=sources,
                                    space=dict(size=preview["size"], workspace=preview["workspace"]))
            self.downloadsChanged.emit()
            self.downloadEnqueued.emit()
            self.pumpDownloads()
            return True
        except (ValueError, OSError) as error:
            self._install_preview.update(can_install=False, status=str(error))
            self.installPreviewChanged.emit()
            self.operation_failed(str(error))
            return False

    @Slot()
    def minimize(self):
        self.minimizeRequested.emit()

    @Slot(result=bool)
    def organizeLibrary(self):
        if self.operationBusy or self.process is not None or self._closing or self._sleeping:
            return False
        self.start_operation(dict(id="", name=tr("Organize library"), cards=list(self._games), installers=list(self._installer_games)), "organize")
        return True

    def start_operation(self, card, mode):
        if self.localMode and mode == 'install':
            self._operation_status = tr('Connect a NAS server to download games. Local games work without it.')
            self.operationChanged.emit()
            return
        if self.loginRequired and mode not in ('discard', 'organize'):
            self._operation_status = tr("Sign in to NGS in Settings.")
            self.operationChanged.emit()
            return
        if self.artwork_worker is not None and mode in ('launch', 'installer_launch', 'metadata_resolve', 'metadata_apply', 'metadata_custom', 'remove', 'forget', 'organize', 'import', 'store_import', 'scan_many'):
            self.artwork_worker.cancel.set()
        event('operation.start', mode=mode, game_id=card['id'], name=card['name'],
              folder=card.get('path', card.get('folder')), executable=card.get('exe', card.get('executable')))
        self._operation_game, self._operation_name = card["id"], card["name"]
        self._operation_progress = 0
        if mode == "install":
            self._operation_progress = self.download_queue.get(card["id"])["progress"]
            if self._download_statistics["game"] != card["id"]:
                self._download_statistics = dict(game=card["id"], network=0.0, disk=0.0, history=[])
            self._io_bytes, self._io_sample, self._io_time = [0, 0], [0, 0], time.monotonic()
            self._io_active = True
            self.downloadStatisticsChanged.emit()
        self._operation_status = {"launch": tr("Checking executable…"), "installer_launch": tr("Checking installer…"), "install": tr("Fetching HTTP package details…"),
                                  "organize": tr("Organizing local game copies…"), "remove": tr("Removing local game copy…"), "forget": tr("Removing library entry…"),
                                  "browse": tr("Reading folders…"), "scan": tr("Searching for executables…"), "import": tr("Checking selected executable…"),
                                  "metadata_search": tr("Searching for game artwork…"), "metadata_resolve": tr("Loading game artwork…"), 'metadata_apply': tr('Saving artwork…'), 'metadata_custom': tr('Saving artwork…'),
                                  "discard": tr("Removing partial game copy…"), "scan_many": tr("Searching for games in the library folder…"),
                                  "store_scan": tr("Searching installed store games…"), "store_import": tr("Adding discovered games…")}[mode]
        if mode == "import":
            self._scanner = dict(self._scanner, status=self._operation_status)
            self.scannerChanged.emit()
        self.game_worker = GameWorker(self, dict(card), mode)
        self.game_worker.progressed.connect(self.operation_progressed)
        self.game_worker.ioUpdated.connect(self.download_io_updated)
        self.game_worker.scanEvent.connect(self.collection_progressed)
        self.game_worker.completed.connect(self.operation_completed)
        self.game_worker.failed.connect(self.operation_failed)
        self.game_worker.finished.connect(self.operation_finished)
        self.sync_download_timer()
        self.operationChanged.emit()
        self.downloadsChanged.emit()
        if mode == "launch":
            self._launching = True
            self._launch_animation_done = self._launch_presented = False
            self._pending_launch = None
            self._running_id, self._running_name = card["id"], card["name"]
            self._game_control_error = self._statistics_error = ""
            self.runningChanged.emit()
            self.gameLaunchRequested.emit(card["id"])
        self.game_worker.start()

    @Slot(str, float, str)
    def operation_progressed(self, phase, fraction, path):
        note_activity()
        if phase.startswith("metadata_"):
            if not self._metadata.get("cancelling"):
                self._metadata = dict(self._metadata, status=path)
                self._operation_status = path
                self.metadataChanged.emit()
                self.operationChanged.emit()
            return
        label = {"copy": tr("Download", "Загрузка"), "unpack": tr("Extracting packages"), "verify": tr("Checking local files"), "remove": tr("Removal"),
                 "organize": tr("Organizing library"), "resume": tr("Checking partial download"), "prepare": tr("Checking file list"),
                 "manifest": tr("Fetching manifest"), "source": tr("Choosing NAS folder"), "waiting": tr("Waiting for a response")}[phase]
        if self.downloadActive:
            if phase in ("copy", "unpack", "verify"):
                self._operation_progress = max(self._operation_progress,
                    .8 + fraction * .2 if phase == "verify" else fraction * .8)
            if time.monotonic() - self._progress_saved > 1:
                job = self.download_queue.get(self._operation_game)
                if job:
                    try:
                        self.download_queue.update(self._operation_game, progress=self._operation_progress)
                    except (OSError, sqlite3.Error):
                        logging.warning("Could not checkpoint queue progress")
                self._progress_saved = time.monotonic()
        else:
            self._operation_progress = fraction
        self._operation_status = ("%s · %s" % (label, path) if phase in ("manifest", "source", "waiting")
                                  else "%s · %.1f%% · %s" % (label, fraction * 100, path))
        self.operationChanged.emit()

    @Slot(object)
    def operation_completed(self, result):
        mode = self.game_worker.mode
        event('operation.done', mode=mode, game_id=self._operation_game)
        if mode in ('store_scan', 'store_import'):
            rows = result['rows']
            if mode == 'store_import':
                updated = {path_key(row['path']):row for row in rows}
                rows = [updated.get(path_key(row['path']), row) for row in self._scanner['store_games']]
            self._scanner.update(stage='stores', store_games=rows, store_review='', store_name='',
                status=tr("Found: %d. Choose a game or add all; files are preserved.") % len(result['rows']))
            if mode == 'store_import':
                self._scanner['status'] = tr("With artwork: %d · without artwork: %d. Retry or choose artwork manually in My games.") % (
                    sum(row['state'] == 'added' for row in result['rows']), sum(row['state'] == 'artwork' for row in result['rows']))
            if result.get('warnings'): self._scanner['status'] += tr(" Some store entries could not be read.")
            self.scannerChanged.emit()
            if mode == 'store_import':
                self._installed = self.library.reconcile(self.librarySort)
                self.libraryChanged.emit(); self.gamesChanged.emit()
                if self._scanner.get('last_added'): self.gameAdded.emit(0, self._scanner['last_added'])
        elif mode == "scan_many":
            self._installed = self.library.reconcile(self.librarySort)
            self.libraryChanged.emit()
            self.gamesChanged.emit()
            self._scanner = dict(self._scanner, stage="batch", batch_rows=result["rows"],
                                 status=self._scanner["status"] + tr(". Review uncertain matches in the list."))
            self.scannerChanged.emit()
            if self._scanner.get("last_added"):
                self.gameAdded.emit(0, self._scanner["last_added"])
        elif mode in ("metadata_search", "metadata_resolve", 'metadata_apply', 'metadata_custom'):
            if mode == "metadata_search":
                self._metadata = dict(self._metadata, results=result, status=tr("Confirm the correct game.") if result else tr("Nothing found. Try a more specific title."))
            elif mode == 'metadata_resolve' and self.game_worker.card.get('preview_only'):
                self._metadata_preview = dict(result['card'])
                self._metadata = dict(self._metadata, preview=self.local_card(dict(card=result['card'])), applying=False,
                                      status=tr('Review the cover and description, then confirm your choice.'))
            else:
                self._metadata_preview = None
                self._metadata = dict(self._metadata, status=result["message"], preview={}, applying=False, custom_saving=False)
                if self.game_worker.card.get("local_id"):
                    self._installed = self.library.reconcile(self.librarySort)
                    self.libraryChanged.emit()
                else:
                    self._scanner = dict(self._scanner, matches=[result["card"]], name=result["card"]["name"], status=result["message"],
                                         manual_metadata_id=result['card']['id'])
                    self.scannerChanged.emit()
                self.metadataApplied.emit()
            self._operation_status = self._metadata["status"]
            self.metadataChanged.emit()
        elif mode in ("browse", "scan"):
            if mode == "browse":
                self._scanner = dict(self._scanner, **result, stage="browse", status=tr("Choose the game folder and select Find executables."))
            else:
                status = tr("Choose the executable. Installers and utilities are marked.")
                if not result["candidates"]:
                    status = tr("No executables found. Choose another folder.")
                if result["limited"] or result["skipped"]:
                    status += tr(" The search is incomplete because of depth, size or access limits.")
                self._scanner = dict(self._scanner, **result, stage="exes", status=status)
            self._operation_status = self._scanner["status"]
            self.scannerChanged.emit()
        elif mode == "organize":
            self._installed = self.library.reconcile(self.librarySort)
            self._installer_packages = self.installer_library.reconcile()
            self.installersChanged.emit()
            self.libraryChanged.emit()
            self.gamesChanged.emit()
            self._operation_progress = 1
            self._operation_status = tr("Organized: %d.") % result["changed"]
            if result["errors"]:
                self._operation_status += tr(" Failed: ") + "; ".join(result["errors"])
            self._settings_status = self._operation_status
            self.settingsChanged.emit()
        elif mode == "installer_launch":
            executable, unused_cwd, unused_arguments = result
            # ShellExecute via Qt keeps the normal interactive wizard, including Windows UAC.
            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(executable))):
                self._operation_status = tr("Installer opened. After installation, choose Add an installed game.")
                self.minimizeRequested.emit()
            else:
                self._operation_status = tr("Could not open the installer. Package files are preserved.")
        elif mode == "launch":
            if self._launching:
                self._pending_launch = result
                self.launch_game()
        else:
            self._installer_packages = self.installer_library.reconcile()
            self.installersChanged.emit()
            self._installed = self.library.reconcile(self.librarySort)
            self.libraryChanged.emit()
            self.gamesChanged.emit()
            self._operation_progress = 1
            self._operation_status = ({"remove": tr("Local game copy removed."),
                                       "forget": tr("Library entry removed. Game files preserved."),
                                       "discard": tr("Download canceled. Partial files removed.")}.get(mode)
                                      or tr("The game is verified and ready to play. Open My games."))
            if mode == "install" and result["card"].get("kind") == "installer":
                self._operation_status = tr("Package downloaded. Open Installers to run the normal installation.")
            if mode == "import":
                if self._scanner.get('store_review'):
                    for row in self._scanner['store_games']:
                        if row['path'] == self._scanner['store_review']:
                            row.update(state='added', status=tr("Added to My games", "Добавлена в «Моих играх»"), game_id=result['card']['id'])
                    self._scanner.update(stage='stores', store_review='', store_name='', status=tr("Game added. You can choose another game."))
                    self.scannerChanged.emit()
                elif self._scanner.get("batch_review"):
                    rows = self._scanner["batch_rows"].copy()
                    for row in rows:
                        if row["path"] == self._scanner["batch_review"]:
                            row.update(state="added", status=tr("Added to My games", "Добавлена в «Мои игры»"), candidates=[], name=result["card"]["name"])
                            LocalScanJournal(self.storage, self.settings["tactic_url"]).save(self._scanner["batch_root"], row)
                    self._scanner = dict(self._scanner, stage="batch", path=self._scanner["batch_root"],
                                         batch_rows=rows, batch_review="", status=tr("Game added. You can review the remaining games."))
                    self.scannerChanged.emit()
                else:
                    self.localGameAdded.emit(result["card"]["id"])
            if mode in ("import", "install"):
                self.gameAdded.emit(3 if result["card"].get("kind") == "installer" else 0, result["card"]["id"])
        if mode in ("organize", "store_import", "scan_many", "metadata_resolve", 'metadata_apply', 'metadata_custom', "import", "install", "remove", "forget"):
            self.schedule_backup()
        self.operationChanged.emit()
        self.downloadsChanged.emit()

    @Slot(str)
    def launchPresentationReady(self, identity):
        if self._launching and identity == self._running_id:
            self._launch_animation_done = True

    @Slot()
    def launch_frame_presented(self):
        if self._launch_animation_done:
            self._launch_animation_done = False
            self._launch_presented = True
            self.launch_game()

    def launch_game(self):
        if (not self._launching or not self._launch_presented or self._pending_launch is None
                or self._closing or self._sleeping):
            return
        result, self._pending_launch = self._pending_launch, None
        event('game.launch_presented', game_id=self._running_id)
        saved_env = clean_child_environment()
        try:
            self.process = GameProcess(*result)
            self._memory_status = ''
        except (OSError, ValueError) as error:
            self.operation_failed(tr("Could not start the game: ") + str(error))
            return
        finally:
            restore_launcher_environment(saved_env)
        self._launching = False
        self._auto_foreground = True
        self._foreground_attempts = 0
        self._foreground_windows = frozenset()
        self._terminating = False
        self._game_control_error = self._statistics_error = ""
        try:
            self.play_time.start(self._running_id)
            self.players.attach_current(self.play_time.snapshot()['session_id'], self._running_id, self._running_name)
            self.play_time.pause(self.gameFrozen or self._sleeping)
        except (OSError, sqlite3.Error, ValueError) as error:
            self._statistics_error = tr("Could not start recording statistics: ") + str(error)
            logging.warning(self._statistics_error)
        self.runningChanged.emit()
        try:
            self.library.mark_played(self._operation_game)
            self._installed = self.library.reconcile(self.librarySort)
            self.libraryChanged.emit()
        except (OSError, sqlite3.Error):
            logging.warning("Could not save recent launch")
        self._operation_status = tr("Game started.")
        self.process_timer.start()
        QTimer.singleShot(350, self.check_game)
        crash_capture.mark('game.started', game_id=self._running_id, name=self._running_name)
        self.gameStarted.emit()
        self.operationChanged.emit()

    @Slot(str)
    def operation_failed(self, message):
        if self._launching:
            self._launching = self._launch_animation_done = self._launch_presented = False
            self._pending_launch = None
            self.runningChanged.emit()
        event('operation.error', game_id=self._operation_game, error=message)
        self._operation_status = message
        if self.game_worker and self.game_worker.mode == "organize":
            self._settings_status = message
            self.settingsChanged.emit()
            self.schedule_backup()
        self._io_active = False
        self.sample_download_io()
        if self.game_worker and self.game_worker.mode.startswith("metadata_"):
            prefix = tr('Could not save artwork. ') if self.game_worker.mode in ('metadata_apply', 'metadata_custom') else tr("Could not retrieve artwork. Refine the search or add the game without it. ")
            self._metadata = dict(self._metadata, applying=False, custom_saving=False, status=prefix + message,
                                  custom_status=prefix + message if self.game_worker.mode == 'metadata_custom' else self._metadata.get('custom_status', ''))
            self.metadataChanged.emit()
        if self.game_worker and self.game_worker.mode in ("browse", "scan", "import", "scan_many", 'store_scan', 'store_import'):
            self._scanner = dict(self._scanner, status=message)
            self.scannerChanged.emit()
        try:
            if self.downloadActive:
                self.download_queue.update(self._operation_game, progress=self._operation_progress)
                # A failed transfer cannot add an installation; unavailable drives must not block its error UI.
            else:
                self._installed = self.library.reconcile(self.librarySort)
                if self.game_worker and self.game_worker.mode == "organize":
                    self._installer_packages = self.installer_library.reconcile()
                    self.installersChanged.emit()
        except (OSError, sqlite3.Error):
            logging.warning("Could not checkpoint local state after failure")
        self.libraryChanged.emit()
        self.gamesChanged.emit()
        if self.game_worker and self.game_worker.mode in ("scan_many", 'store_import') and self._scanner.get("last_added"):
            self.gameAdded.emit(0, self._scanner["last_added"])
        self.operationChanged.emit()
        self.downloadsChanged.emit()

    @Slot()
    def operation_finished(self):
        self._io_active = False
        self.sample_download_io()
        self.game_worker.deleteLater()
        self.game_worker = None
        self.operationChanged.emit()
        self.downloadsChanged.emit()
        if self._closing:
            self.quitRequested.emit()
        else:
            self.pumpCleanup()
            QTimer.singleShot(0, self.pumpDownloads)
            self.ensure_local_artwork()

    @Slot()
    def cancelOperation(self):
        if self._launching:
            self._launching = self._launch_animation_done = self._launch_presented = False
            self._pending_launch = None
            self._operation_status = tr("Game launch canceled.")
            self.runningChanged.emit()
            self.operationChanged.emit()
        if self.game_worker:
            if self.game_worker.mode == "install":
                self.cancelDownload(self._operation_game)
                return
            self.game_worker.cancel.set()
            self._operation_status = tr("Stopping operation…")
            if self.game_worker.mode.startswith("metadata_"):
                self._metadata = dict(self._metadata, cancelling=True,
                                      status=tr("Canceling artwork search. Waiting for the current request to finish…"))
                self.metadataChanged.emit()
            if self.game_worker.mode in ("browse", "scan", "import", "scan_many", 'store_scan', 'store_import'):
                self._scanner = dict(self._scanner, status=self._operation_status)
                self.scannerChanged.emit()
            self.operationChanged.emit()

    @Slot()
    def check_game(self):
        if self.process is None:
            return
        try:
            if self.process.active():
                if not self.gameFrozen and not self._sleeping and not self._terminating:
                    try:
                        from ngs.edge_swipe import disable_fullscreen_edge_gesture
                        seen_edge = getattr(self.process, '_edge_configured', None)
                        if seen_edge is None:
                            seen_edge = set()
                            self.process._edge_configured = seen_edge
                        game_windows = self.process.windows()
                        for hwnd, _ in game_windows:
                            if hwnd not in seen_edge:
                                disable_fullscreen_edge_gesture(hwnd)
                                seen_edge.add(hwnd)
                        windows_changed = frozenset(game_windows) != self._foreground_windows
                        if windows_changed:
                            self._foreground_windows = frozenset(game_windows)
                            self._foreground_attempts = 0
                            event('game.windows', game_id=self._running_id, windows=game_windows,
                                  automatic=self._auto_foreground)
                        target = getattr(self.process, '_foreground_window', None)
                        needs_focus = (not self.process._foregrounded or windows_changed or
                                       (target is not None and target not in game_windows))
                        if self._auto_foreground and needs_focus and game_windows and self._foreground_attempts < 10:
                            self._foreground_attempts += 1
                            try:
                                focused = self.process.foreground(automatic=True)
                            except OSError as error:
                                self._game_control_error = tr("Could not focus the game: ") + str(error)
                                event('game.foreground_failed', game_id=self._running_id,
                                      attempt=self._foreground_attempts, error=str(error))
                            else:
                                if focused is False:
                                    self._auto_foreground = False
                                else:
                                    self.process._foregrounded = True
                                self._foreground_attempts = 0
                                self._game_control_error = ""
                    except OSError as error:
                        self._game_control_error = tr("Could not check game windows: ") + str(error)
                self.update_running()
                return
            code = self.process.exit_code()
            self._operation_status = tr("Game closed.") if code == 0 else tr("The game exited with code %s.") % code
        except OSError as error:
            self._operation_status = tr("Could not check the game process: ") + str(error)
            self._game_control_error = self._operation_status
            self.update_running()
            self.operationChanged.emit()
            return
        self.update_running(reason="forced" if self._terminating else "completed" if code == 0 else "crashed")
        crash_capture.mark('game.exited', game_id=self._running_id, code=code, forced=self._terminating,
                           had_windows=bool(getattr(self.process, '_had_windows', False)))
        self.process_timer.stop()
        self.process.close()
        self.process = None
        self._terminating = False
        self.runningChanged.emit()
        self.freezeChanged.emit()
        self.gameReturned.emit()
        self.operationChanged.emit()
        self.pumpDownloads()

        self.pumpCleanup()

    @Property(str, notify=catalogStatusChanged)
    def catalogStatus(self):
        return self._catalog_status

    def catalog_published(self, section):
        remote = self.installer_catalogizer if section == 3 else self.catalogizer
        cache = self.installer_cache if section == 3 else self.cache
        identity = remote.state.get("selected_id", "")
        if identity:
            self._published_selection[section] = (cache, identity)
        # An in-flight refresh may still return the feed from before publication.
        if (self.installer_worker if section == 3 else self.worker) is not None:
            self._published_refresh.add(section)
        (self.refreshInstallers if section == 3 else self.refreshCatalog)()

    def select_published_game(self, section, cache, cards):
        pending = self._published_selection.get(section)
        if pending and pending[0] is not cache:
            self._published_selection.pop(section)
        elif pending and any(card["id"] == pending[1] for card in cards):
            self._published_selection.pop(section)
            if section == 1 and pending[1] in self.installedIds:
                self.setHideInstalled(False)
            self.gameAdded.emit(section, pending[1])

    @Slot()
    def retryOffline(self):
        if self._closing or self._sleeping or self.localMode: return
        now = time.monotonic()
        if now >= self._catalog_refresh_at:
            self.refreshCatalog()
        elif now >= self._installer_refresh_at:
            self.refreshInstallers()
        self.pumpDownloads()
        self.pumpCleanup()

    @Slot()
    def refreshInstallers(self):
        if self.localMode or self.loginRequired: return
        if self._closing or self.installer_cache is None or self.installer_worker is not None:
            return
        self._installer_status = tr("Refreshing installer catalog…")
        self.installersChanged.emit()
        self.installer_worker = CatalogWorker(self.installer_cache, self)
        self.installer_worker.loaded.connect(self.installers_loaded)
        self.installer_worker.failed.connect(self.installers_failed)
        self.installer_worker.finished.connect(self.installers_finished)
        self.installer_worker.start()

    @Slot(list)
    def installers_loaded(self, cards):
        if self.sender().cache is not self.installer_cache:
            return
        self._installer_games = cards
        self._installer_refresh_at = time.monotonic() + 300
        self._installer_status = tr("Installer catalog updated") if cards else tr("No installers yet. Configure the server source folder and add packages through the catalog manager.")
        self.installersChanged.emit()
        self.select_published_game(3, self.installer_cache, cards)

    @Slot()
    def installers_failed(self):
        if self.sender().cache is self.installer_cache:
            self._installer_refresh_at = time.monotonic() + 60
            self._installer_status = (tr("Offline · cached installer catalog") if self.installerGames
                                      else tr("The installer catalog is unavailable. Configure the installer source folder on your server."))
            self.installersChanged.emit()

    @Slot()
    def installers_finished(self):
        changed = self.installer_worker.cache is not self.installer_cache
        self.installer_worker.deleteLater()
        self.installer_worker = None
        refresh = 3 in self._published_refresh
        self._published_refresh.discard(3)
        if (changed or refresh) and not self._closing:
            self.refreshInstallers()

    @Slot()
    def refreshCatalog(self):
        if self.localMode or self.loginRequired: return
        self.refreshInstallers()
        if self._closing or self.cache is None or self.worker is not None:
            return
        self._catalog_status = tr("Refreshing catalog…")
        self.catalogStatusChanged.emit()
        self.worker = CatalogWorker(self.cache, self)
        self.worker.loaded.connect(self.catalog_loaded)
        self.worker.failed.connect(self.catalog_failed)
        self.worker.finished.connect(self.catalog_finished)
        self.worker.start()

    @Slot(list)
    def catalog_loaded(self, games):
        sender = self.sender()
        if isinstance(sender, CatalogWorker) and sender.cache is not self.cache:
            return
        self._catalog_online = True
        self._catalog_refresh_at = time.monotonic() + 300
        if games != self._games:
            self._games = games
            self.gamesChanged.emit()
        self.libraryChanged.emit()
        self.select_published_game(1, self.cache, games)
        self._catalog_status = tr("Catalog updated") if games else tr("No published games in the catalog yet")
        self.catalogStatusChanged.emit()
        self.ensure_local_artwork()

    @Slot()
    def catalog_failed(self):
        sender = self.sender()
        if isinstance(sender, CatalogWorker) and sender.cache is not self.cache:
            return
        self._catalog_online = False
        self._catalog_refresh_at = time.monotonic() + 60
        self._catalog_status = (tr("Offline · cached catalog") if self._games else tr("Catalog unavailable · retry"))
        self.catalogStatusChanged.emit()

    @Slot()
    def catalog_finished(self):
        changed_address = self.worker.cache is not self.cache
        self.worker.deleteLater()
        self.worker = None
        refresh = 1 in self._published_refresh
        self._published_refresh.discard(1)
        if (changed_address or refresh) and not self._closing:
            self.refreshCatalog()

    def schedule_backup(self):
        if (not self.localMode and not self._demo and not self._closing and not self._restoring_backup
                and self.settings.get("auto_backup", True) is not False):
            self.backup_timer.start(8000)

    @Slot(bool)
    def _on_local_artwork_updated(self, upgraded=False):
        if upgraded:
            self._installed = self.library.reconcile(self.librarySort)
            self.schedule_backup()
        self.libraryChanged.emit()
        self.gamesChanged.emit()

    def ensure_local_artwork(self):
        if (self._demo or (not self.localMode and (not self.cache or self.loginRequired))
                or self.artwork_worker is not None or self.game_worker is not None or self.process is not None
                or self._closing or self._sleeping or self.settings.get('resolve_local_metadata', True) is not True):
            return
        from ngs.catalog import ArtworkCache
        cache = ArtworkCache(self.storage) if self.localMode else self.cache
        receipts = [r for r in self._installed if isinstance(r, dict) and isinstance(r.get("card"), dict)]
        if not receipts:
            return
        needs_work = False
        for r in receipts:
            if r.get('metadata_manual') and str(r.get('catalog_id', '')).startswith('custom-'):
                continue
            card = r["card"]
            cover, bg = card.get("cover"), card.get("background")
            if not isinstance(cover, dict) or not isinstance(bg, dict):
                if r.get("kind") == "external":
                    needs_work = True
                    break
                continue
            if (str(cover.get("path", "")).startswith("/assets/ngs_launcher/pending/")
                    or str(bg.get("path", "")).startswith("/assets/ngs_launcher/pending/")
                    or not card.get('description')
                    or not cache.image_valid(cover, verify_digest=False)
                    or not cache.image_valid(bg, verify_digest=False)):
                needs_work = True
                break
        if not needs_work:
            return
        worker = LocalArtworkWorker(
            self.storage,
            "" if self.localMode else self.settings.get("tactic_url", ""),
            cache,
            self.library,
            receipts,
            self._games,
            self,
        )
        self.artwork_worker = worker
        worker.updated.connect(self._on_local_artwork_updated)
        def cleanup():
            if self.artwork_worker is worker:
                self.artwork_worker = None
            worker.deleteLater()
            if worker.cancel.is_set() and not self._closing:
                QTimer.singleShot(0, self.ensure_local_artwork)
        worker.finished.connect(cleanup)
        worker.start()

    @Slot(dict)
    def on_backup_restored(self, info):
        self._restoring_backup = True
        try:
            self.backup_timer.stop()
            self.settings = self.storage.read_settings()
            self._hide_installed = self.settings.get("hide_installed", False) is True
            try:
                self.storage.set_games_directory(self.settings.get("games_directory", "games"))
            except Exception:
                pass
            self._installed = self.library.reconcile(self.librarySort)
            self._installer_packages = self.installer_library.reconcile()
            self.desktop_controls.apply()
            self.desktop_controls.changed.emit()
            self.virtual_keyboard.changed.emit()
            self.filterChanged.emit()
            self.settingsChanged.emit()
            self.installersChanged.emit()
            self.libraryChanged.emit()
            self.gamesChanged.emit()
            self.ensure_local_artwork()
        finally:
            self._restoring_backup = False

    def stop(self):
        crash_capture.stop()
        from ngs.players import _LINGERING_WORKERS
        def _wait(worker, ms=3000):
            if worker is not None and not worker.wait(ms):
                worker.setParent(None)
                _LINGERING_WORKERS.append(worker)
        if hasattr(self, 'status_overlay'): self.status_overlay.close()
        if self._memory_worker: _wait(self._memory_worker)
        self.backup_timer.stop()
        self.restart_timer.stop()
        if self.artwork_worker:
            self.artwork_worker.cancel.set()
            _wait(self.artwork_worker)
        self.update_timer.stop()
        self.offline_timer.stop()
        self.download_timer.stop()
        if self.cleanup_worker:
            self.cleanup_worker.cancel.set()
            _wait(self.cleanup_worker)
        self.desktop_controls.close()
        self.virtual_keyboard.close()
        self.system_status.close()
        self.system_controls.close()
        self.catalogizer.stop()
        self.installer_catalogizer.stop()
        if self.installer_worker:
            self.installer_worker.cancel.set()
            _wait(self.installer_worker)
        self.cancelUpdate()
        if self.update_worker:
            _wait(self.update_worker)
        self.cancelInstallPreview()
        if self.preview_worker:
            _wait(self.preview_worker)
        if self.game_worker:
            if self.game_worker.mode == "install":
                self.download_queue.set_state(self._operation_game, "stopping" if self._closing else "pausing", tr("Saved before NGS stopped"))
            self.game_worker.cancel.set()
            _wait(self.game_worker, 2500)
        if self.worker:
            self.worker.cancel.set()
            _wait(self.worker)
        self.process_timer.stop()
        if self.process:
            self.update_running(reason="launcher_closed")
            self.process.close()
        self.players.stop()

    @Property("QVariantMap", constant=True)
    def initialUi(self):
        ui = self.settings.get("ui", {})
        if not isinstance(ui, dict):
            ui = {}
        default_sec = self.settings.get("default_section", "last")
        section_override = {"library": 0, "catalog": 1, "installers": 3}.get(default_sec)
        section = section_override if section_override is not None else ui.get("section", 1)
        section = section if isinstance(section, int) and 0 <= section <= 3 else 1
        if self.localMode: section = 0
        return dict(self.sectionUi(section), section=section)

    @Slot(int, result="QVariantMap")
    def sectionUi(self, section):
        ui = self.settings.get("ui", {})
        if not isinstance(ui, dict):
            ui = {}
        selections = ui.get("selections", {})
        state = selections.get(str(section), {}) if isinstance(selections, dict) else {}
        if not state and ui.get("section", 1) == section:
            state = ui  # Read the selection saved by older versions.
        if not isinstance(state, dict):
            state = {}
        collection = self.localGames if section == 0 else self.installerGames if section == 3 else self.games
        index = state.get("game_index", 0)
        index = max(0, min(index, len(collection) - 1)) if isinstance(index, int) else 0
        index = next((i for i, game in enumerate(collection) if game["id"] == state.get("game_id")), index)
        scroll = state.get("scroll_x", 0)
        grid_scroll = state.get("scroll_y", 0)
        return {"game_index": index, "game_id": collection[index]["id"] if collection else "",
                "scroll_x": scroll if isinstance(scroll, (int, float)) and math.isfinite(scroll) else 0,
                "scroll_y": grid_scroll if isinstance(grid_scroll, (int, float)) and math.isfinite(grid_scroll) else 0}

    @Property(str, notify=downloadsChanged)
    def freeSpace(self):
        try:
            reserved, _, unknown = self.download_queue.reserved_space(self.storage.games, scan=False)
            if unknown:
                return tr("Checking storage · queue size unknown")
            free = shutil.disk_usage(self.storage.games).free
            label = tr("≈ %.1f GiB available") % (max(0, free-reserved) / 1024**3)
            return label + (tr(" · %.1f GiB queued") % (reserved / 1024**3) if reserved else "")
        except (OSError, ValueError):
            return tr("The installation drive is unavailable")

    @Property(bool, notify=gamepadChanged)
    def gamepadConnected(self):
        return self._gamepad_connected

    def set_gamepad_connected(self, connected):
        self._gamepad_connected = connected
        self.gamepadChanged.emit()

    @Property("QVariantMap", notify=inputChanged)
    def inputHints(self):
        keys = ("activate", "back", "refresh", "toggle", "sections", "settings", "minimize", "menu")
        labels = (("Enter", "Esc", "R", "X", "Q / E", "F2", "F9", "F10") if self._input_device == "keyboard"
                  else ("A", "B", "Y", "X", "LB / RB", "Y", "X", "Start"))
        navigation = (("Q", "E", "Shift+Tab", "Tab") if self._input_device == "keyboard"
                      else ("LB", "RB", "LT", "RT"))
        return dict(zip(keys, labels), device=self._input_device,
                    **dict(zip(("previous_section", "next_section", "previous_control", "next_control"), navigation)))

    def set_input_device(self, device):
        if device != self._input_device:
            self._input_device = device
            self.inputChanged.emit()

    @Slot(int, int)
    def rememberUi(self, section, index):
        if section not in (0, 1, 2, 3):
            return
        ui = self.settings.get("ui", {})
        if not isinstance(ui, dict):
            ui = {}
        selections = ui.get("selections", {})
        if not isinstance(selections, dict):
            selections = {}
        # Migrate before visiting another tab replaces the legacy active selection.
        previous = ui.get("section", 1)
        if previous in (0, 1) and str(previous) not in selections:
            selections[str(previous)] = {key: ui[key] for key in ("game_id", "game_index") if key in ui}
        ui = self.settings["ui"] = dict(ui, section=section, selections=selections)
        ui.pop("game_index", None)
        ui.pop("game_id", None)
        if section == 2:
            return
        collection = self.localGames if section == 0 else self.installerGames if section == 3 else self.games
        if 0 <= index < len(collection) or index == 0:
            state = selections.get(str(section), {})
            if not isinstance(state, dict):
                state = {}
            selections[str(section)] = state
            state["game_index"] = index
            if collection:
                state["game_id"] = collection[index]["id"]

    @Slot(int, float)
    def rememberScroll(self, section, offset):
        if section in (0, 1, 3) and math.isfinite(offset):
            ui = self.settings.get("ui", {})
            state = ui.get("selections", {}).get(str(section))
            if isinstance(state, dict):
                state["scroll_x"] = offset

    @Slot(int, float)
    def rememberGridScroll(self, section, offset):
        if section in (0, 1, 3) and math.isfinite(offset):
            ui = self.settings.get("ui", {})
            selections = ui.get("selections", {}) if isinstance(ui, dict) else {}
            state = selections.get(str(section)) if isinstance(selections, dict) else None
            if isinstance(state, dict):
                state["scroll_y"] = max(0, offset)

    def save(self):
        self.storage.write_settings(self.settings)

    @Slot()
    def quit(self):
        crash_capture.stop()
        crash_capture.mark('app.quit_requested', worker=self.game_worker.mode if self.game_worker else None,
                           game=self.process is not None)
        self.virtual_keyboard.close()
        if self.process:
            self.process.thaw()
        self._closing = True
        if self.game_worker:
            if self.game_worker.mode == "install":
                self.download_queue.set_state(self._operation_game, "stopping", tr("Will resume when NGS starts"))
                self.game_worker.cancel.set()
            else:
                self.cancelOperation()
        else:
            self.quitRequested.emit()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Close:
            if self.game_worker:
                event.ignore()
                self.quit()
                return True
            self._closing = True  # An explicit close, before aboutToQuit/health acknowledgement.
            if getattr(self.parent(), 'edge_windows', None): self.parent().edge_windows.close()
            if getattr(self, 'status_overlay', None): self.status_overlay.close()
            if QGuiApplication.instance(): QGuiApplication.instance().quit()
        if event.type() == QEvent.Type.KeyPress:
            self.set_input_device('keyboard')
            if event.key() == Qt.Key.Key_F and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                self.action.emit("search")
                return True
            focus = QGuiApplication.focusObject()
            if focus and (focus.inherits("QQuickTextInput") or focus.inherits("QQuickTextEdit")) and event.key() != Qt.Key.Key_Escape:
                return False
            mapping = {
                Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right", Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
                Qt.Key.Key_Return: "activate", Qt.Key.Key_Enter: "activate", Qt.Key.Key_Space: "activate",
                Qt.Key.Key_Escape: "back", Qt.Key.Key_Q: "previous_section", Qt.Key.Key_E: "next_section",
                Qt.Key.Key_Tab: "next_control", Qt.Key.Key_Backtab: "previous_control", Qt.Key.Key_F10: "menu",
                Qt.Key.Key_R: "refresh",
                Qt.Key.Key_X: "toggle_installed",
                Qt.Key.Key_F2: "settings",
                Qt.Key.Key_F9: "minimize",
            }
            action = mapping.get(event.key())
            if action and not (event.modifiers() & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ControlModifier)):
                self.action.emit(action)
                return True
        if (event.type() == QEvent.Type.MouseButtonPress
                and event.source() == Qt.MouseEventSource.MouseEventNotSynthesized):
            self.set_input_device('keyboard')
        return super().eventFilter(watched, event)


def create_window(storage, demo=False, *, self_test=False, startup_screen=None):
    from ngs.i18n import configure
    configure(storage)
    started = time.monotonic()
    if startup_screen is not None: startup_screen.set_status(tr("Loading library"))
    font_id = QFontDatabase.addApplicationFontFromData((storage.resources / "assets/fonts/Manrope.ttf").read_bytes())
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        raise RuntimeError(tr("Could not read assets/fonts/Manrope.ttf"))
    QGuiApplication.instance().setFont(QFont(families[0], 11))
    engine = QQmlApplicationEngine()
    engine.setOfflineStoragePath(str(storage.state / "qml"))
    bridge_started = time.perf_counter()
    bridge = LauncherBridge(storage, engine, demo=demo, self_test=self_test)
    event('perf.startup_phase', stage='bridge', elapsed_ms=round((time.perf_counter()-bridge_started)*1000, 1))
    engine.rootContext().setContextProperty('uiTheme', bridge)
    def update_palette(): appearance.apply_palette(QGuiApplication.instance(), bridge.themeColors)
    bridge.settingsChanged.connect(update_palette)
    update_palette()
    if startup_screen is not None: startup_screen.set_status(tr("Preparing interface"))
    engine.setInitialProperties({"bridge": bridge})
    engine.load(QUrl.fromLocalFile(str(storage.resources / "qml" / "Main.qml")))
    event('perf.startup_phase', stage='main_qml', elapsed_ms=round((time.monotonic()-started)*1000, 1))
    roots = engine.rootObjects()
    if not roots:
        raise RuntimeError(tr("Could not load the interface. See data/logs/ngs.log for details."))
    window = roots[0]
    from PySide6.QtQml import QQmlEngine
    QQmlEngine.setObjectOwnership(window, QQmlEngine.ObjectOwnership.CppOwnership)
    def window_destroyed():
        crash_capture.mark('window.destroyed', object_name='launcherWindow')
    window.destroyed.connect(window_destroyed)
    from ngs.status_overlay import StatusOverlay, park_fullscreen_game
    bridge.status_overlay = StatusOverlay(bridge.virtual_keyboard, bridge)
    engine.setInitialProperties(dict(bridge=bridge.status_overlay))
    engine.load(QUrl.fromLocalFile(str(storage.resources / 'qml/StatusBarWindow.qml')))
    roots = engine.rootObjects()
    if len(roots) != 2: raise RuntimeError(tr("Could not load the status bar."))
    status_window = roots[1]
    def show_status_bar(screen_name):
        if bridge._sleeping or bridge._closing or not bridge.status_overlay.enabled: return
        screen = next((s for s in QGuiApplication.screens() if s.name() == screen_name), QGuiApplication.primaryScreen())
        status_window.setScreen(screen)
        bridge.status_overlay.show(screen.name())
        logging.info('Top-edge panel opened: remote=%s', bridge.status_overlay.remoteHost)
    def status_closed():
        parked, bridge.status_overlay.parked = bridge.status_overlay.parked, None
        if parked is bridge.process and parked and not bridge._sleeping and not bridge._closing and not bridge.gameFrozen:
            try: parked.foreground()
            except OSError: logging.exception('Could not restore game after status bar')
    bridge.status_overlay.requested.connect(show_status_bar)
    bridge.status_overlay.closed.connect(status_closed)
    QGuiApplication.instance().aboutToQuit.connect(bridge.status_overlay.close)
    previous_screen = [None]
    def watch_screen(screen):
        bridge.virtual_keyboard.set_screen(screen)
        if previous_screen[0] is not None:
            try:
                previous_screen[0].refreshRateChanged.disconnect(bridge.set_refresh_rate)
            except (RuntimeError, TypeError):
                pass  # A disconnected screen may already have been destroyed by Qt.
        previous_screen[0] = screen
        if screen is not None:
            screen.refreshRateChanged.connect(bridge.set_refresh_rate)
        bridge.set_refresh_rate(screen.refreshRate() if screen is not None else 0.0)
    window.screenChanged.connect(watch_screen)
    watch_screen(window.screen())
    # Release graphics when Windows hides NGS; the in-game curtain itself is static.
    active_graphics_api = bridge.settingsState.get("graphics_api", "d3d11")
    is_software = (active_graphics_api == "software"
                   or os.environ.get("QT_QUICK_BACKEND") == "software"
                   or os.environ.get("QT_QPA_PLATFORM") == "offscreen")
    cache_prefix = "" if active_graphics_api == "d3d11" else (active_graphics_api + "-")
    def configure_graphics(target, filename):
        target.setPersistentSceneGraph(False)
        target.setPersistentGraphics(False)
        if is_software:
            return
        graphics = QQuickGraphicsConfiguration()
        graphics.setAutomaticPipelineCache(False)
        pipeline = storage.cache / "shaders" / (cache_prefix + filename)
        graphics.setPipelineCacheSaveFile(str(pipeline))
        if pipeline.is_file():
            graphics.setPipelineCacheLoadFile(str(pipeline))
        target.setGraphicsConfiguration(graphics)
    configure_graphics(window, 'pipeline.cache')
    overlay_windows = [window.findChild(QObject, name) for name in ('virtualKeyboardWindow', 'leftTouchpadWindow', 'rightTouchpadWindow', 'keyboardSettingsWindow')]
    overlay_windows.append(status_window)
    for w in overlay_windows:
        if w is not None: QQmlEngine.setObjectOwnership(w, QQmlEngine.ObjectOwnership.CppOwnership)
    engine._ngs_windows = [window, *overlay_windows]
    def current_launcher_window():
        nonlocal window
        from shiboken6 import isValid
        if not isValid(engine): return None
        current = next((root for root in engine.rootObjects()
                        if isValid(root) and root.objectName() == 'launcherWindow'), None)
        if current is None:
            if not getattr(engine, '_ngs_window_missing', False):
                crash_capture.mark('window.unavailable', game=bridge.process is not None)
                engine._ngs_window_missing = True
            gamepad.set_active(False, background=any('buttons' in value for value in bridge.desktop_controls.values().values()))
            bridge.virtual_keyboard.release_keys()
            return None
        engine._ngs_window_missing = False
        if current is not window:
            crash_capture.mark('window.reference_changed', previous_valid=isValid(window))
            window = current
            engine._ngs_windows[0] = current
        return current
    for index, overlay in enumerate(overlay_windows):
        configure_graphics(overlay, 'keyboard-pipeline.cache' if not index else 'keyboard-settings.cache' if index == 3 else 'touchpad-%d.cache' % index)
    bridge.virtual_keyboard.attach_touchpads(overlay_windows[1:3])
    overlay_timer = QTimer(engine)
    overlay_timer.setSingleShot(True)
    def all_overlays():
        from shiboken6 import isValid
        return [overlay for overlay in overlay_windows if isValid(overlay)]
    def all_stackable_overlays():
        from shiboken6 import isValid
        edges = engine.edge_windows.windows if getattr(engine, 'edge_windows', None) else []
        ordered = (overlay_windows + edges) if not bridge.status_overlay.shown else (edges + overlay_windows)
        return [overlay for overlay in ordered if isValid(overlay)]
    def request_overlay_restack():
        if not power.sleeping and any(overlay.isVisible() for overlay in all_stackable_overlays()) and not overlay_timer.isActive():
            overlay_timer.start(0)
    overlay_events = WindowsWindowEvents(request_overlay_restack) if QGuiApplication.platformName() == 'windows' else None
    def update_overlay_events():
        stackable_visible = not power.sleeping and any(overlay.isVisible() for overlay in all_stackable_overlays())
        if overlay_events:
            try:
                overlay_events.set_enabled(stackable_visible)
            except OSError:
                logging.exception('Cannot watch foreign windows for keyboard stacking')
        if stackable_visible:
            request_overlay_restack()
        else:
            overlay_timer.stop()
    def restack_overlays():
        if power.sleeping: return
        visible = [overlay for overlay in all_stackable_overlays() if overlay.isVisible()]
        if overlay_events:
            overlay_events.restack([int(overlay.winId()) for overlay in visible])
        else:
            for overlay in visible: overlay.raise_()
    overlay_timer.timeout.connect(restack_overlays)
    periodic_restack = QTimer(engine)
    periodic_restack.setInterval(800)
    periodic_restack.timeout.connect(request_overlay_restack)
    def sync_periodic_restack():
        should_run = not power.sleeping and any(overlay.isVisible() for overlay in all_overlays())
        if should_run and not periodic_restack.isActive(): periodic_restack.start()
        elif not should_run and periodic_restack.isActive(): periodic_restack.stop()
    for overlay in overlay_windows:
        overlay.visibleChanged.connect(update_overlay_events)
        overlay.visibleChanged.connect(sync_periodic_restack)
    bridge.status_overlay.changed.connect(request_overlay_restack)
    bridge.status_overlay.changed.connect(sync_periodic_restack)
    bridge.desktop_controls.windowStackChanged.connect(request_overlay_restack)
    window.installEventFilter(bridge)
    gamepad = GamepadInput(engine)
    state_cfg = bridge.settingsState
    gamepad.configure(
        vibration=state_cfg["gamepad_vibration"],
        deadzone=state_cfg["gamepad_deadzone"],
        repeat_speed=state_cfg["gamepad_repeat_speed"],
    )
    bridge.virtual_keyboard.hapticRequested.connect(lambda duration: gamepad.vibrate(duration) if duration else gamepad.stop_vibration())
    QGuiApplication.instance().aboutToQuit.connect(gamepad.stop_vibration)
    previous_buttons = [0]
    def route_buttons(buttons):
        window = current_launcher_window()
        if window is None:
            bridge.desktop_controls.gamepad_input(buttons)
            return True
        from shiboken6 import isValid
        if not isValid(window): return False
        if buttons and buttons != previous_buttons[0]:
            bridge.set_input_device("gamepad")
        previous_buttons[0] = buttons
        if bridge.desktop_controls.gamepad_input(buttons):
            bridge.virtual_keyboard.release_keys()
            return True
        if bridge.status_overlay.shown: return False
        return bridge.virtual_keyboard.gamepad_input(buttons, allowed=not window.isActive() and not power.sleeping and not bridge.gameFrozen and not bridge.powerError)
    gamepad.button_handler = route_buttons
    def route_gamepad(action):
        bridge.set_input_device("gamepad")
        if bridge.desktop_controls.capture:
            return
        elif bridge.status_overlay.shown and not bridge.virtual_keyboard.visible:
            if not bridge.status_overlay.remote: bridge.status_overlay.action.emit(action)
        elif bridge.virtual_keyboard.visible:
            if not bridge.virtual_keyboard.remoteHost: bridge.virtual_keyboard.action.emit(action)
        else:
            bridge.action.emit(action)
    gamepad.action.connect(route_gamepad)
    gamepad.connectionChanged.connect(bridge.set_gamepad_connected)
    gamepad.connectionChanged.connect(bridge.desktop_controls.gamepad_connection)
    gamepad.connectionChanged.connect(bridge.virtual_keyboard.disconnected)
    game_panel_active = [False]
    pending_game_panel = [None]
    def update_input():
        window = current_launcher_window()
        if window is None: return
        from shiboken6 import isValid
        if not isValid(window): return
        bridge.set_ui_active(not power.sleeping and window.isVisible() and window.isActive()
                             and window.visibility() != window.Visibility.Minimized)
        frozen_panel = (bridge.gameFrozen or bool(bridge.powerError)) and not window.property('frozenLibraryVisible')
        gamepad.set_active(not power.sleeping and (bridge.status_overlay.shown or bridge.virtual_keyboard.visible or ((not window.property("gameInProgress") or game_panel_active[0] or frozen_panel)
                          and (window.isActive() or frozen_panel) and window.visibility() not in
                           (window.Visibility.Hidden, window.Visibility.Minimized))),
                           background=not power.sleeping and (bridge.virtual_keyboard.padEnabled or any('buttons' in value for value in bridge.desktop_controls.values().values())))
    bridge.virtual_keyboard.changed.connect(update_input)
    bridge.status_overlay.changed.connect(update_input)
    bridge.desktop_controls.changed.connect(update_input)
    window.visibilityChanged.connect(update_input)
    bridge.quitRequested.connect(window.close)
    minimizing = [False]
    minimize_timer = QTimer(engine)
    minimize_timer.setSingleShot(True)
    minimize_timer.setInterval(350)
    def clear_minimizing():
        minimizing[0] = False
    minimize_timer.timeout.connect(clear_minimizing)
    def minimize():
        window = current_launcher_window()
        if window is None: return
        bridge._auto_foreground = False
        if bridge.launchingGame:
            bridge.cancelOperation()
        from shiboken6 import isValid
        if not isValid(window): return
        pending_game_panel[0] = None
        front_timer.stop()
        minimizing[0] = True
        minimize_timer.start()
        bridge.virtual_keyboard.hide()
        bridge.status_overlay.hide()
        if QGuiApplication.platformName() == "windows":
            from ngs.process import set_topmost
            try:
                set_topmost(int(window.winId()), False)
            except OSError:
                pass
        if bridge.desktop_controls.trayAvailable and bridge.settings.get("minimize_to_tray", True) is not False:
            window.hide()
            return
        # showMinimized clears FullScreen; retain it for Windows taskbar/Alt+Tab restoration.
        window.setWindowStates(window.windowStates() | Qt.WindowState.WindowMinimized)
        if QGuiApplication.platformName() == "windows" and not self_test:
            import ctypes
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user.ShowWindow(int(window.winId()), 6)
    bridge.minimizeRequested.connect(minimize)
    previous_visibility = [None]
    returning_to_game = [False]
    return_to_game_timer = QTimer(engine)
    return_to_game_timer.setSingleShot(True)
    return_to_game_timer.setInterval(400)
    def clear_returning_to_game():
        returning_to_game[0] = False
    return_to_game_timer.timeout.connect(clear_returning_to_game)
    def background_for_game():
        pending_game_panel[0] = None
        window = current_launcher_window()
        if window is None: return
        game_panel_active[0] = False
        returning_to_game[0] = True
        return_to_game_timer.start()
        if not window.property("gameInProgress"):
            previous_visibility[0] = window.visibility()
        window.setProperty("gameInProgress", True)
        update_topmost()
        # Keep the rendered statistics behind the game while its windows are being created.
        if not bridge._auto_foreground:
            window.hide()
        update_input()
    def return_from_game():
        pending_game_panel[0] = None
        window = current_launcher_window()
        if window is None: return
        game_panel_active[0] = False
        returning_to_game[0] = False
        return_to_game_timer.stop()
        window.setProperty("gameInProgress", False)
        if previous_visibility[0] == window.Visibility.FullScreen:
            window.showFullScreen()
        else:
            window.showNormal()
        update_topmost()
        window.raise_()
        window.requestActivate()
        request_overlay_restack()
        if getattr(engine, 'input_host', None) is not None:
            engine.input_host.request_restack()
        update_input()
    window.frameSwapped.connect(bridge.launch_frame_presented, Qt.ConnectionType.QueuedConnection)
    bridge.gameStarted.connect(background_for_game)
    bridge.gameReturned.connect(return_from_game)
    bridge.gameLibraryRequested.connect(return_from_game)
    front_attempts = [0]
    front_timer = QTimer(engine)
    front_timer.setInterval(150)
    def foreground_frozen():
        window = current_launcher_window()
        if window is None: return
        if power.sleeping or window.property('frozenLibraryVisible') or not (bridge.gameFrozen or bridge.powerError):
            front_timer.stop()
            return
        if bridge.gameFrozen:
            bridge.process.park_windows()
        if previous_visibility[0] == window.Visibility.FullScreen:
            window.showFullScreen()
        else:
            window.showNormal()
        if QGuiApplication.platformName() == "windows":
            from ngs.process import set_topmost
            set_topmost(int(window.winId()), True)
        window.raise_()
        window.requestActivate()
        front_attempts[0] += 1
        if front_attempts[0] >= 10:
            front_timer.stop()
    front_timer.timeout.connect(foreground_frozen)
    def show_frozen_state():
        if power.sleeping: pending_game_panel[0] = None
        window = current_launcher_window()
        if window is None: return
        from shiboken6 import isValid
        if not isValid(window): return
        if power.sleeping:
            bridge.desktop_controls.cancel_presses()
            bridge.virtual_keyboard.hide()
            bridge.virtual_keyboard.release_keys()
            front_timer.stop()
            gamepad.set_active(False)
            if bridge.gameFrozen or bridge.powerError:
                window.hide()
            return  # Activating a window during display-off can interrupt Modern Standby.
        frozen = bridge.gameFrozen or bool(bridge.powerError)
        if not frozen:
            update_topmost()
        if frozen:
            window.setProperty('frozenLibraryVisible', False)
            front_attempts[0] = 0
            foreground_frozen()
            front_timer.start()
        else:
            front_timer.stop()
        update_input()
    power = PowerNotifications(bridge.freeze_for_sleep, bridge.powerResumed.emit)
    bridge.powerResumed.connect(bridge.resume_play_clock)
    system_settings_open = [False]
    last_visibility = [window.Visibility.FullScreen]
    def update_topmost():
        window = current_launcher_window()
        if window is None: return
        if power.sleeping or QGuiApplication.platformName() != "windows":
            return
        from ngs.process import set_topmost
        visible = window.visibility() not in (window.Visibility.Hidden, window.Visibility.Minimized) and not minimizing[0] and not returning_to_game[0]
        active = window.isActive() or self_test or bridge.gameFrozen or bool(bridge.powerError)
        set_topmost(int(window.winId()), visible and active and not system_settings_open[0] and
                    ((not window.property("gameInProgress") and bridge.process is None)
                     or bridge.gameFrozen or bool(bridge.powerError)))
        request_overlay_restack()
    def activate_visible_launcher():
        window = current_launcher_window()
        if window is None: return
        from shiboken6 import isValid
        if not isValid(window) or minimizing[0] or returning_to_game[0]: return
        if (not power.sleeping and not system_settings_open[0] and window.isVisible() and
                window.visibility() not in (window.Visibility.Hidden, window.Visibility.Minimized) and
                (not window.property("gameInProgress") or game_panel_active[0] or bridge.gameFrozen or bridge.powerError)):
            window.raise_()
            window.requestActivate()
            request_overlay_restack()
            if getattr(engine, 'input_host', None) is not None:
                engine.input_host.request_restack()
    def system_settings_focus(opening):
        # Settings must be visible above NGS while it owns keyboard/controller focus.
        system_settings_open[0] = opening
        update_topmost()
        if not opening:
            activate_visible_launcher()
    bridge.systemSettingsFocus.connect(system_settings_focus)
    def active_changed():
        window = current_launcher_window()
        if window is None: return
        from shiboken6 import isValid
        if not isValid(window): return
        if bridge.process is not None and window.isActive() and not returning_to_game[0]:
            bridge._auto_foreground = False
        if bridge.process is not None and window.isActive() and not window.property("frozenLibraryVisible"):
            window.setProperty("gameInProgress", True)
        if window.property("gameInProgress"):
            game_panel_active[0] = window.isActive()
        update_topmost()
        if window.isActive():
            bridge.virtual_keyboard.release_keys()
            if pending_game_panel[0] is not None: window.update()
        request_overlay_restack()
        if window.isActive() and system_settings_open[0] and getattr(bridge, "_input_installer", None) is None:
            system_settings_open[0] = False
            update_topmost()
            bridge.refreshSystemStatus()
            logging.info("Returned from system settings; launcher input restored")
        update_input()
    window.activeChanged.connect(active_changed)
    def finish_game_panel():
        process = pending_game_panel[0]
        if process is None: return
        window = current_launcher_window()
        if (window is None or bridge.process is not process or power.sleeping or bridge._sleeping
                or bridge._closing or returning_to_game[0]):
            pending_game_panel[0] = None
            return
        if not window.isVisible() or not window.isActive() or window.visibility() == window.Visibility.Minimized:
            return
        pending_game_panel[0] = None
        bridge.minimizeGame(False)
        event('game.panel_presented', game_id=bridge._running_id)
    window.frameSwapped.connect(finish_game_panel, Qt.ConnectionType.QueuedConnection)
    def show_game_panel():
        window = current_launcher_window()
        if window is None: return
        if power.sleeping: return
        minimizing[0] = False
        minimize_timer.stop()
        returning_to_game[0] = False
        return_to_game_timer.stop()
        bridge._auto_foreground = False
        pending_game_panel[0] = bridge.process
        event('game.panel_requested', game_id=bridge._running_id)
        window.setProperty('frozenLibraryVisible', False)
        window.setProperty('gameInProgress', True)
        game_panel_active[0] = True
        if last_visibility[0] == window.Visibility.FullScreen: window.showFullScreen()
        else: window.showNormal()
        window.raise_()
        window.requestActivate()
        update_topmost()
        if getattr(engine, 'input_host', None) is not None:
            engine.input_host.request_restack()
        update_input()
        window.update()
    bridge.gamePanelRequested.connect(show_game_panel)
    def desktop_action(action):
        window = current_launcher_window()
        if window is None:
            if bridge.process is not None and not bridge.gameFrozen and not power.sleeping:
                bridge._auto_foreground = False
                try: bridge.process.foreground()
                except OSError: logging.exception('Cannot restore game without launcher window')
            return
        if power.sleeping:
            return
        hidden = window.visibility() in (window.Visibility.Hidden, window.Visibility.Minimized) or not window.isVisible()
        if action == 'toggle':
            if bridge.process is not None and not bridge.gameFrozen and not bridge.powerError:
                if not hidden and window.isActive():
                    bridge.returnToGame()
                    return
                action = 'show'
            else:
                action = 'show' if (hidden or (not self_test and not demo and not window.isActive())) else 'desktop'
        elif action == 'desktop' and hidden and not bridge.desktop_controls.values().get('show') and bridge.desktop_controls.values().get('desktop', {}).get('toggle'):
            action = 'show'
        if action == 'desktop':
            bridge.minimizeGame(False)
            minimize()
        elif action == 'keyboard':
            bridge.virtual_keyboard.toggle()
        elif action in ('show', 'menu'):
            minimizing[0] = False
            minimize_timer.stop()
            returning_to_game[0] = False
            return_to_game_timer.stop()
            bridge.virtual_keyboard.hide()
            system_settings_open[0] = False
            if window.property("gameInProgress") or bridge.process is not None:
                show_game_panel()
                return
            if last_visibility[0] == window.Visibility.FullScreen:
                window.showFullScreen()
            else:
                window.showNormal()
            activate_visible_launcher()
            update_topmost()
            if action == 'menu':
                bridge.action.emit('menu')
    bridge.desktop_controls.action.connect(desktop_action)
    def status_launcher_action(action):
        window = current_launcher_window()
        if window is None: return
        if power.sleeping: return
        bridge.status_overlay.parked = None
        if window.property("gameInProgress"):
            return_from_game()
        else:
            desktop_action('show')
        bridge.action.emit(action)
    bridge.status_overlay.launcherActionRequested.connect(status_launcher_action)
    editor_previous_visibility = [None]
    def edit_pad():
        window = current_launcher_window()
        if window is None: return
        if power.sleeping: return
        editor_previous_visibility[0] = window.visibility()
        desktop_action('show')
        window.raise_()
        window.requestActivate()
    bridge.virtual_keyboard.editRequested.connect(edit_pad)
    bridge.virtual_keyboard.layoutEditRequested.connect(lambda draft: edit_pad())
    def editor_closed():
        window = current_launcher_window()
        if window is None: return
        previous = editor_previous_visibility[0]
        editor_previous_visibility[0] = None
        if power.sleeping or previous is None or getattr(bridge, '_closing', False): return
        if previous in (window.Visibility.Hidden, window.Visibility.Minimized): minimize()
        update_topmost()
        if bridge.process is not None and not bridge.gameFrozen and not bridge.powerError:
            try: bridge.process.foreground()
            except OSError: logging.info('The game window is no longer available after keyboard editing')
    bridge.virtual_keyboard.editorClosed.connect(editor_closed)
    from ngs.auto_keyboard import AutoKeyboard
    auto_keyboard = AutoKeyboard(bridge.virtual_keyboard, power, native=not demo and QGuiApplication.platformName() == 'windows')
    bridge.freezeChanged.connect(auto_keyboard.update)
    QGuiApplication.instance().aboutToQuit.connect(auto_keyboard.close)
    engine.destroyed.connect(auto_keyboard.close)
    engine.auto_keyboard = auto_keyboard
    engine.input_host = None
    def connect_input_host(auto_update=False):
        window = current_launcher_window()
        if window is None: return
        import os
        previous, engine.input_host = engine.input_host, None
        if previous:
            power.changed.disconnect(previous.sync)
            power.resumed.disconnect(previous.sync)
            QGuiApplication.instance().aboutToQuit.disconnect(previous.close)
            previous.close(); previous.deleteLater()
        installed_input = Path(os.environ['ProgramFiles']) / 'NGS Input/NGSInput.exe'
        if installed_input.is_file():
            from ngs.input_host import InputHostClient, InputHostOutdated
            try:
                # The constructor verifies the complete protected payload once.
                # A separate preflight here used to hash the same files twice.
                engine.input_host = InputHostClient(bridge, window, engine)
                power.changed.connect(engine.input_host.sync)
                power.resumed.connect(engine.input_host.sync)
                QGuiApplication.instance().aboutToQuit.connect(engine.input_host.close)
            except InputHostOutdated as error:
                if auto_update:
                    bridge.installInputHost()
                else:
                    bridge._settings_status = str(error)
                    bridge.settingsChanged.emit()
            except (OSError, ValueError) as error:
                logging.exception('Cannot start installed input windows; keeping portable keyboard')
                bridge._settings_status = str(error)
                bridge.settingsChanged.emit()
        if engine.input_host is None:
            from ngs.input_host import InputHostClient
            try:
                engine.input_host = InputHostClient(bridge, window, engine, portable=True)
                power.changed.connect(engine.input_host.sync)
                power.resumed.connect(engine.input_host.sync)
                QGuiApplication.instance().aboutToQuit.connect(engine.input_host.close)
            except (OSError, ValueError):
                logging.info('Portable D3D11 overlay helper unavailable; keeping in-process overlays')
    if not demo and not self_test and QGuiApplication.platformName() == 'windows':
        bridge.inputHostInstalled.connect(connect_input_host)
    def connect_initial_input_host():
        if not bridge._closing:
            connect_input_host(auto_update=not bridge._first_start_pending)
    initial_input_timer = QTimer(engine)
    initial_input_timer.setSingleShot(True)
    initial_input_timer.setInterval(0)
    initial_input_timer.timeout.connect(connect_initial_input_host)
    first_frame_seen = [False]
    def first_frame():
        if first_frame_seen[0]: return
        window = current_launcher_window()
        if window is None: return
        first_frame_seen[0] = True
        try: (storage.state / "graphics-boot.json").unlink(missing_ok=True)
        except Exception: pass
        event('ui.first_frame', elapsed_ms=round((time.monotonic()-started)*1000, 1))
        crash_capture.note('first frame')
        if not self_test:
            crash_capture.start_watchdog(window)
        try:
            from PySide6.QtCore import qVersion
            screens = [dict(name=s.name(), geometry=[s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height()],
                            dpr=s.devicePixelRatio(), hz=s.refreshRate()) for s in QGuiApplication.screens()]
            renderer = window.rendererInterface()
            event('ui.environment', qt=qVersion(), platform=QGuiApplication.platformName(),
                  graphics_api=str(renderer.graphicsApi()) if renderer else None, screens=screens,
                  window_screen=window.screen().name() if window.screen() else '',
                  input_host=engine.input_host is not None if hasattr(engine, 'input_host') else None)
        except Exception as error:
            event('ui.environment_error', error=str(error))
        if not demo and not self_test and QGuiApplication.platformName() == 'windows' and not bridge._closing:
            initial_input_timer.start()
    window.frameSwapped.connect(first_frame, Qt.ConnectionType.QueuedConnection)
    bridge.desktop_controls.attach(window, native=not demo)
    activate_launcher_timer = QTimer(engine)
    activate_launcher_timer.setSingleShot(True)
    activate_launcher_timer.setInterval(0)
    activate_launcher_timer.timeout.connect(activate_visible_launcher)
    tray_minimize_timer = QTimer(engine)
    tray_minimize_timer.setSingleShot(True)
    tray_minimize_timer.setInterval(0)
    tray_minimize_timer.timeout.connect(minimize)
    def visibility_changed():
        window = current_launcher_window()
        if window is None: return
        from shiboken6 import isValid
        if not isValid(window): return
        crash_capture.mark('window.visibility', state=getattr(window.visibility(), 'name', str(window.visibility())))
        if window.visibility() in (window.Visibility.Windowed, window.Visibility.FullScreen):
            last_visibility[0] = window.visibility()
            if not minimizing[0]:
                activate_launcher_timer.start()
        elif (window.visibility() == window.Visibility.Minimized
              and bridge.desktop_controls.trayAvailable
              and bridge.settings.get("minimize_to_tray", True) is not False
              and not minimizing[0]):
            tray_minimize_timer.start()
        update_topmost()
    window.visibilityChanged.connect(visibility_changed)
    def sync_window_mode():
        window = current_launcher_window()
        if window is None: return
        from shiboken6 import isValid
        if demo or self_test or not isValid(window) or power.sleeping or minimizing[0] or not window.isVisible() or window.property("gameInProgress"):
            return
        if window.visibility() in (window.Visibility.Hidden, window.Visibility.Minimized):
            return
        want_windowed = bridge.settings.get("window_mode") == "windowed"
        if want_windowed and window.visibility() == window.Visibility.FullScreen:
            window.showNormal()
        elif not want_windowed and window.visibility() == window.Visibility.Windowed:
            window.showFullScreen()
    bridge.settingsChanged.connect(sync_window_mode)
    bridge.freezeChanged.connect(show_frozen_state)
    bridge.powerResumed.connect(show_frozen_state)
    power.changed.connect(bridge.frozen_updated)
    power.failed.connect(bridge.power_failed)
    power.changed.connect(update_overlay_events)
    power.resumed.connect(update_overlay_events)
    power.changed.connect(sync_periodic_restack)
    power.resumed.connect(sync_periodic_restack)
    engine.destroyed.connect(power.close)
    if overlay_events:
        engine.destroyed.connect(overlay_events.close)
        QGuiApplication.instance().aboutToQuit.connect(overlay_events.close)
    QGuiApplication.instance().aboutToQuit.connect(bridge.virtual_keyboard.close)
    QGuiApplication.instance().aboutToQuit.connect(lambda: crash_capture.mark('app.about_to_quit'))
    # Keep the Python wrappers alive for the complete QML engine lifetime.
    engine.bridge, engine.gamepad, engine.power = bridge, gamepad, power
    engine.freeze_front_timer = front_timer
    engine.overlay_events = overlay_events
    engine.edge_windows = None
    if not demo and not self_test and QGuiApplication.platformName() == 'windows':
        from ngs.edge_swipe import EdgeWindows
        bridge.status_overlay.edge_enabled = True
        engine.edge_windows = EdgeWindows(engine, bridge.status_overlay, storage.resources)
        engine.edge_windows.status_window = status_window
        engine.edge_windows.attach_backing(window)
        def on_edge_windows_changed():
            update_overlay_events()
            sync_periodic_restack()
        engine.edge_windows.changed = on_edge_windows_changed
        on_edge_windows_changed()
    def status_power():
        bridge.status_overlay.sleeping = power.sleeping
        bridge.status_overlay.changed.emit()
        if power.sleeping: bridge.status_overlay.hide()
    power.changed.connect(status_power)
    power.resumed.connect(status_power)
    return engine, window


def portable_check(storage, window):
    import ctypes, json, os, ssl, sys
    from PySide6.QtCore import QMetaObject, QPointF
    from PySide6.QtGui import QImage
    from PySide6.QtQml import QQmlExpression, QQmlEngine
    popups = {}
    # Check header input before popup exit transitions can intercept it.
    bridge = window.property("bridge")
    login_gate_checked = bridge._demo or (bridge.localMode and not bridge.loginRequired and window.property('section') == 0) or (bridge.loginRequired and
        window.findChild(QObject, 'loginSettingsButton').property('visible'))
    # The standalone self-test uses synthetic games and never supplies a real account.
    bridge._demo = True
    bridge.settings['local_mode'] = False
    if bridge.cache is None:
        bridge.cache = CatalogCache(storage, bridge.settings['tactic_url'])
        bridge.installer_cache = CatalogCache(storage, bridge.settings['tactic_url'], installers=True)
        bridge._games = bridge.cache.load()
        bridge.gamesChanged.emit()
    bridge.settingsChanged.emit()
    bridge.runningChanged.emit()
    QGuiApplication.processEvents()
    original_input = bridge._input_device
    input_hints_checked = True
    for device, accept, back in (("keyboard", "Enter", "Esc"), ("gamepad", "A", "B")):
        bridge.set_input_device(device)
        QGuiApplication.processEvents()
        input_hints_checked = (input_hints_checked
            and window.findChild(QObject, "acceptHint").property("text") == accept + tr("   Select")
            and window.findChild(QObject, "settingsBack").property("text") == tr("Back · ") + back
            and window.findChild(QObject, "catalogizerPane").property("hints")["device"] == device
            and window.findChild(QObject, "virtualKeyboardWindow").property("hints")["device"] == device)
    bridge.set_input_device(original_input)
    bridge.refreshSystemStatus()
    bridge.system_status.close()
    QGuiApplication.processEvents()
    system_status_checked = bridge.system_status.worker is None and set(bridge.systemState) == {
        "wifi", "bluetooth", "battery", "has_battery", "plugged", "charging", "volume", "muted"}
    old_section, old_focus = window.property("section"), window.property("focusZone")
    window.setProperty("section", 1)
    window.setProperty("focusZone", 0)
    window.setProperty("navigationIndex", 1)
    tab_probe = dict(before=dict(section=window.property("section"), focus=window.property("focusZone"),
        modal=window.property("modalVisible"), catalogizer=window.findChild(QObject, "catalogizerPane").property("visible"),
        frozen=bridge.gameFrozen, power_error=bridge.powerError, playing=window.property("gameInProgress"),
        visible_popups=[name for name in popups if name != "destinationCombo" and window.findChild(QObject, name).property("visible")]))
    bridge.action.emit("left")
    tab_probe["left"] = dict(section=window.property("section"), navigation=window.property("navigationIndex"), focus=window.property("focusZone"))
    tabs_require_confirmation = window.property("section") == 1 and window.property("navigationIndex") == 0
    bridge.action.emit("activate")
    tab_probe["activate"] = dict(section=window.property("section"), navigation=window.property("navigationIndex"), focus=window.property("focusZone"))
    tabs_require_confirmation = (tabs_require_confirmation and window.property("section") == 0
                                 and window.property("focusZone") == 0 and window.property("navigationIndex") == 0)
    window.setProperty("section", 1)
    window.setProperty("navigationIndex", 6)
    QGuiApplication.processEvents()
    header_help = window.findChild(QObject, "selectionHelp")
    help_visible = (header_help.property('visible') if bridge.inputHints['device'] == 'gamepad'
                    else window.findChild(QObject, 'acceptHint').property('visible')
                    and 'F10' in window.findChild(QObject, 'detailsHint').property('text'))
    header_help_checked = (help_visible and tr("Game filters") in header_help.property("text")
                           and bridge.inputHints["activate"] + " — " in header_help.property("text"))
    window.setProperty("focusZone", 2)
    bridge.action.emit("next_control")
    utility_navigation_checked = window.property("focusZone") == -1 and window.property("statusIndex") == 0
    bridge.action.emit("previous_control")
    utility_navigation_checked = (utility_navigation_checked and window.property("focusZone") == 0
                                  and window.property("navigationIndex") == 5
                                  and not window.findChild(QObject, "exitPopup").property("visible"))
    previous_view = bridge.gamesView
    previous_index = window.property("selectedIndex")
    bridge.setGamesView("grid")
    window.setProperty("focusZone", 2)
    QGuiApplication.processEvents()
    grid = window.findChild(QObject, "gameGrid")
    QMetaObject.invokeMethod(grid, "forceLayout")
    grid_search_checked = bool(grid.property("visible") and grid.property("count") and grid.property("currentItem"))
    expression = QQmlExpression(QQmlEngine.contextForObject(window), window, 'applySearch("__NGS_NO_SUCH_GAME_4597__")')
    expression.evaluate()
    QGuiApplication.processEvents()
    grid_search_checked = (grid_search_checked and not expression.hasError() and grid.property("count") == 0
                           and window.findChild(QObject, "gridEmptyMessage").property("visible"))
    added_identity = bridge.games[-1]["id"]
    bridge.gameAdded.emit(1, added_identity)
    QGuiApplication.processEvents()
    added_selection_checked = (window.property("selected")["id"] == added_identity
                               and window.property("searchQuery") == ""
                               and bridge.sectionUi(1)["game_id"] == added_identity)
    bridge.action.emit("right")
    QGuiApplication.processEvents()
    selection_wrap_checked = window.property("selectedIndex") == 0 and grid.property("contentY") <= 1
    bridge.action.emit("left")
    QGuiApplication.processEvents()
    selection_wrap_checked = selection_wrap_checked and window.property("selected")["id"] == added_identity
    QQmlExpression(QQmlEngine.contextForObject(window), window, 'applySearch("")').evaluate()
    bridge.setGamesView("ribbon")
    window.setProperty("selectedIndex", previous_index)
    window.setProperty("navigationIndex", 0)
    window.setProperty("section", old_section)
    window.setProperty("focusZone", old_focus)
    bindings_hold_ui_checked = False
    for name in ("settingsPane", "directoryPicker", "localScanner", "metadataPicker", "installDestination",
                 "detailsPopup", "removeDialog", "exitPopup", "frozenScreen", "updatesPane", "catalogizerPane", "librarySortMenu", "launchOptionsPane", "bindingsPane", "gameSearchPane", "padKeyboardEditor", "padPresetPicker", "systemPanel", "playerAccountPane"):
        pane = window.findChild(QObject, name)
        if pane is None:
            raise ValueError("Portable self-test cannot find " + name)
        # The folder dialog is owned by SettingsPane and needs its parent visible.
        if name == "directoryPicker":
            QMetaObject.invokeMethod(window.findChild(QObject, "settingsPane"), "open")
        QMetaObject.invokeMethod(pane, "open")
        QGuiApplication.processEvents()
        popups[name] = bool(pane.property("visible"))
        if name == 'settingsPane':
            for identity, count in (('settingsTheme', 2), ('settingsAccent', 5), ('settingsGraphicsApi', 5)):
                combo = pane.findChild(QObject, identity)
                context = QQmlEngine.contextForObject(combo)
                QQmlExpression(context, combo, 'popup.open()').evaluate()
                QGuiApplication.processEvents()
                popups[identity + 'Choices'] = bool(combo.property('count') == count and
                    QQmlExpression(context, combo, 'popup.visible').evaluate()[0] and
                    QQmlExpression(context, combo, 'popup.height').evaluate()[0] > 40 * count * pane.property('uiScale'))
                QQmlExpression(context, combo, 'popup.close()').evaluate()
        if name == 'bindingsPane':
            pane.setProperty('holdCapture', True)
            pane.setProperty('selectedField', 0)
            QQmlExpression(QQmlEngine.contextForObject(pane), pane, 'handleAction("activate")').evaluate()
            controls = bridge.desktop_controls
            bindings_hold_ui_checked = controls.capture == 'desktop' and controls.capture_hold
            if QGuiApplication.platformName() == 'windows':
                bindings_hold_ui_checked = bindings_hold_ui_checked and bool(controls.key_capture and controls.key_capture.hook)
            controls.cancelCapture()
            pane.setProperty('holdCapture', False)
        if name == "installDestination":
            combo = window.findChild(QObject, "destinationFolders")
            context = QQmlEngine.contextForObject(combo)
            QQmlExpression(context, combo, "popup.open()").evaluate()
            QGuiApplication.processEvents()
            popups["destinationCombo"] = bool(QQmlExpression(context, combo, "popup.visible").evaluate()[0])
            QQmlExpression(context, combo, "popup.close()").evaluate()
        QMetaObject.invokeMethod(pane, "close")
        if name == "directoryPicker":
            QMetaObject.invokeMethod(window.findChild(QObject, "settingsPane"), "close")
    settings_opened = popups["settingsPane"]
    section = window.property("section")
    window.setProperty("section", 3)
    QGuiApplication.processEvents()
    installers_section_checked = bool(window.findChild(QObject, "emptyInstallers").property("visible"))
    pane = window.findChild(QObject, "catalogizerPane")
    pane.setProperty("bridge", bridge.installer_catalogizer)
    QMetaObject.invokeMethod(pane, "open")
    QGuiApplication.processEvents()
    popups["installerCatalogizer"] = bool(pane.property("visible") and pane.property("bridge").installerMode)
    QMetaObject.invokeMethod(pane, "close")
    pane.setProperty("bridge", bridge.catalogizer)
    catalog_probe = RemoteCatalogBridge(storage, lambda: None, window)
    catalog_calls = []
    catalog_probe.request = catalog_calls.append
    catalog_probe.openServer('http://127.0.0.1:9/tactic')
    try:
        pane.setProperty('bridge', catalog_probe)
        QMetaObject.invokeMethod(pane, 'open')
        catalog_probe.loaded(dict(stage='done', state='done', preview='ab'*16,
            rows=[dict(name=f, folder=f, state='manual') for f in ('One', 'Two', 'Three')],
            result_offset=20, result_total=45, summary=dict(added=0, already=0, manual=45, error=0)))
        context = QQmlEngine.contextForObject(pane)
        QQmlExpression(context, pane, 'choose(1)').evaluate()
        catalog_probe.loaded(dict(stage='exes', folder='Two', rows=[dict(path='play.exe')]))
        catalog_probe._state['executable'] = 'play.exe'
        catalog_probe.loaded(dict(stage='working', state='export', preview='cd'*16))
        catalog_probe.loaded(dict(stage='done', state='done', preview='cd'*16, rows=[]))
        catalog_calls.clear()
        QQmlExpression(context, pane, 'runAction(0)').evaluate()
        QGuiApplication.processEvents()
        catalogizer_return_checked = bool(not catalog_calls and catalog_probe.state['preview'] == 'ab'*16
            and catalog_probe.state['result_offset'] == 20 and pane.property('currentIndex') == 1
            and [row['folder'] for row in pane.property('displayRows').toVariant()] == ['One', 'Three'])
    finally:
        catalog_probe.stop()
        QMetaObject.invokeMethod(pane, 'close')
        pane.setProperty('bridge', bridge.catalogizer)
        catalog_probe.deleteLater()
    downloads_button = window.findChild(QObject, "downloadsButton")
    QMetaObject.invokeMethod(downloads_button, "clicked")
    QGuiApplication.processEvents()
    downloads_opened = bool(window.property("section") == 2 and
                            window.findChild(QObject, "downloadsPage").property("enabled") and
                            downloads_button and downloads_button.parentItem().objectName() == "utilityNavigation")
    identity = "download-ui-" + str(time.monotonic_ns())
    image = dict(path="/assets/ngs_launcher/test.jpg", size=1, sha256="0" * 64)
    card = dict(id=identity, name="Download details", subtitle="", description="", genre="", accent="#d9ff82",
                source_folder="Game", executable="play.exe", metadata_url="", revision=1, cover=image, background=image,
                delivery="on_request", total_size=5 * 1024**3)
    try:
        bridge.download_queue.add(card, storage.root / "offline-source", "http://127.0.0.1:9")
        bridge.downloadsChanged.emit()
        QGuiApplication.processEvents()
        header_badges_checked = window.findChild(QObject, "activeDownloadsBadge").property("visible")
        bridge.download_queue.set_state(identity, "error")
        bridge.downloadsChanged.emit()
        QGuiApplication.processEvents()
        header_badges_checked = header_badges_checked and window.findChild(QObject, "failedDownloadsBadge").property("visible")
        bridge.download_queue.set_state(identity, "paused")
        bridge.download_queue.update(identity, completed_bytes=3 * 1024**3, total_bytes=5 * 1024**3, network_bytes=2 * 1024**3,
                                     stage_identity=[2**80+17, 2**120+29])
        bridge.downloadsChanged.emit()
        view = window.findChild(QObject, "downloadQueueList")
        QMetaObject.invokeMethod(view, "forceLayout")
        QGuiApplication.processEvents()
        row = next(item for item in view.property("contentItem").childItems() if item.property("identity") == identity)
        labels = {item.objectName(): item for item in row.childItems()}
        download_details_checked = (row.property("completedBytes") == 3 * 1024**3 and
            str(storage.games / "Download details") in labels["queueDestination-" + identity].property("text") and
            tr("MiB") in labels["queueFileSize-" + identity].property("text"))
    finally:
        bridge.download_queue.forget_row(identity)
        bridge.downloadsChanged.emit()
    update_available = bridge._update['available']
    try:
        bridge._update['available'] = True
        bridge.updatesChanged.emit()
        QGuiApplication.processEvents()
        header_badges_checked = header_badges_checked and window.findChild(QObject, "updateAvailableBadge").property("visible")
    finally:
        bridge._update['available'] = update_available
        bridge.updatesChanged.emit()
    window.setProperty("section", section)
    formats = {}
    picture = QImage(8, 8, QImage.Format.Format_RGB32)
    picture.fill(0xffd9ff82)
    for extension in ("png", "jpg", "webp", "ico"):
        path = storage.temp / ("format-check." + extension)
        try:
            formats[extension] = picture.save(str(path)) and not QImage(str(path)).isNull()
        finally:
            path.unlink(missing_ok=True)
    formats["svg"] = not QImage(str(storage.resources / "assets/local.svg")).isNull()
    import base64
    formats["gif"] = not QImage.fromData(base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")).isNull()
    previous = window.visibility()
    topmost_states = []
    def record_topmost(states=topmost_states):
        if QGuiApplication.platformName() == "windows":
            import ctypes
            user = ctypes.WinDLL("user32")
            user.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            states.append(bool(user.GetWindowLongPtrW(int(window.winId()), -20) & 8))
    record_topmost()
    bridge.gameStarted.emit()
    QGuiApplication.processEvents()
    record_topmost()
    background = (not window.isVisible() and window.findChild(QObject, "playingBackground").property("visible")
                  and not window.findChild(QObject, "launcherScene").property("visible"))
    dashboard = window.findChild(QObject, "playingBackground")
    def dashboard_item(item, name):
        if item.objectName() == name: return item
        for child in item.childItems():
            found = dashboard_item(child, name)
            if found is not None: return found
    running_dashboard_checked = bool(window.findChild(QObject, 'runningGameTitle').property('text'))
    for name in ('thawGameButton', 'freezeGameButton', 'minimizeGameButton', 'forceCloseGameButton', 'minimizeNgsButton', 'resetGameStatisticsButton'):
        button = dashboard_item(dashboard, name)
        if button is None:
            running_dashboard_checked = False
            continue
        point = button.mapToItem(dashboard, QPointF())
        running_dashboard_checked = running_dashboard_checked and point.y() >= 0 and point.y() + button.height() <= dashboard.height() + 1
    QQmlExpression(QQmlEngine.contextForObject(dashboard), dashboard, 'handleAction("right")').evaluate()
    running_dashboard_checked = running_dashboard_checked and dashboard.property('selectedAction') == 1
    dashboard.setProperty('selectedAction', 0)
    bridge.gameReturned.emit()
    QGuiApplication.processEvents()
    record_topmost()
    bridge.minimize()
    QGuiApplication.processEvents()
    tray = bridge.desktop_controls.trayAvailable
    minimized = window.visibility() == (window.Visibility.Hidden if tray else window.Visibility.Minimized)
    record_topmost()
    if tray:
        bridge.desktop_controls.action.emit('show')
    elif QGuiApplication.platformName() == "windows":
        import ctypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.ShowWindow(int(window.winId()), 9)  # SW_RESTORE follows the Windows taskbar path.
    else:
        window.setWindowStates(window.windowStates() & ~Qt.WindowState.WindowMinimized)
    QGuiApplication.processEvents()
    minimize_restored = minimized and window.visibility() == previous
    record_topmost()
    topmost_checked = not topmost_states or topmost_states == [True, False, True, False, True]
    bridge.desktop_controls.action.emit('toggle')
    QGuiApplication.processEvents()
    toggle_restored = window.visibility() == (window.Visibility.Hidden if tray else window.Visibility.Minimized)
    bridge.desktop_controls.action.emit('toggle')
    QGuiApplication.processEvents()
    toggle_restored = toggle_restored and window.visibility() == previous
    system_settings_states = []
    bridge.systemSettingsFocus.emit(True)
    QGuiApplication.processEvents()
    record_topmost(system_settings_states)
    bridge.systemSettingsFocus.emit(False)
    QGuiApplication.processEvents()
    record_topmost(system_settings_states)
    system_settings_handoff_checked = not system_settings_states or system_settings_states == [False, True]
    keyboard_window = window.findChild(QObject, 'virtualKeyboardWindow')
    auto_keyboard = QQmlEngine.contextForObject(window).engine().auto_keyboard
    keyboard_auto_checked = (not auto_keyboard.native or not auto_keyboard.enabled or
        auto_keyboard.listener is not None and not auto_keyboard.listener.closed)
    auto_keyboard.close()
    keyboard_auto_checked = keyboard_auto_checked and (auto_keyboard.thread is None or not auto_keyboard.thread.is_alive())
    bridge.virtual_keyboard.show()
    from PySide6.QtCore import QEventLoop
    settle = QEventLoop(); QTimer.singleShot(300, settle.quit); settle.exec()
    QGuiApplication.processEvents()
    keyboard_checked = keyboard_window.isVisible() and bool(keyboard_window.flags() & Qt.WindowType.WindowDoesNotAcceptFocus)
    if QGuiApplication.platformName() == 'windows':
        user = ctypes.WinDLL('user32')
        user.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        keyboard_checked = keyboard_checked and bool(user.GetWindowLongPtrW(int(keyboard_window.winId()), -20) & 0x08000000)
    keyboard_geometry = [keyboard_window.x(), keyboard_window.y(), keyboard_window.width(), keyboard_window.height()]
    keyboard_dpr = keyboard_window.devicePixelRatio()
    old_keyboard_options = bridge.settings.get('keyboard_options')
    bridge.virtual_keyboard.setKeyboardOpacity(.65)
    QGuiApplication.processEvents()
    keyboard_opacity_checked = (abs(keyboard_window.property('contentOpacity') - .65) < .01 and keyboard_window.opacity() == 1
        and keyboard_window.findChild(QObject, 'keyboardOpacitySlider') is not None)
    bridge.virtual_keyboard.setKeyboardOpacity(0)
    QGuiApplication.processEvents()
    keyboard_opacity_checked = (keyboard_opacity_checked and keyboard_window.opacity() == 1 and
        keyboard_window.property('contentOpacity') == 0 and
        keyboard_window.findChild(QObject, 'keyboardOpacityControl').property('opacity') >= .4)
    bridge.virtual_keyboard.setKeyboardOpacity(.65)
    keyboard_overlay_checked = True
    keyboard_overlay_steps = {}
    keyboard_sizes = []
    for variant in bridge.virtual_keyboard.views:
        view = variant['id']
        bridge.virtual_keyboard.setView(view)
        low, high = bridge.virtual_keyboard.options['height_min'], bridge.virtual_keyboard.options['height_max']
        for percent in dict.fromkeys(max(low,min(high,p)) for p in (30, 40, 55, 70)):
            bridge.virtual_keyboard.setHeight(percent)
            QGuiApplication.processEvents()
            settle = QEventLoop(); QTimer.singleShot(50,settle.quit); settle.exec()
            area = bridge.virtual_keyboard.screenArea
            rows = keyboard_window.property('rows').toVariant()
            horizontal = (0 < keyboard_window.width() < area['width'] and keyboard_window.x() >= area['x']
                          and keyboard_window.x() + keyboard_window.width() <= area['x'] + area['width']) if view == 'small' else keyboard_window.width() == area['width'] and keyboard_window.x() == area['x']
            bottom = keyboard_window.y() + keyboard_window.height() - area['y'] - area['height']
            keyboard_overlay_checked = (keyboard_overlay_checked and horizontal
                and (bottom <= 1 if view == 'small' else abs(bottom) <= 1)
                and abs(keyboard_window.height() - area['height'] * percent / 100) <= 1
                and len(rows) == (5 if view in ('full','traditional') else 1 if view == 'navigation' else 4))
            keyboard_sizes.append(dict(view=view, percent=percent, height=keyboard_window.height(), screen_height=area['height']))
    pending = [keyboard_window.contentItem()]
    keyboard_overlay_steps['geometry'] = keyboard_overlay_checked
    bridge.virtual_keyboard.setTheme('black',True)
    theme_checked = True
    for theme in bridge.virtual_keyboard.themes:
        bridge.virtual_keyboard.setTheme(theme['id'],False)
        QGuiApplication.processEvents()
        theme_checked = theme_checked and keyboard_window.property('inputColors')['id'] == theme['id'] and window.findChild(QObject,'leftTouchpadWindow').property('inputColors')['id'] == 'black'
    bridge.virtual_keyboard.setTheme('dark',False)
    keyboard_overlay_checked = keyboard_overlay_checked and theme_checked
    keyboard_overlay_steps['themes'] = theme_checked
    service = None
    while pending:
        item = pending.pop()
        if item.objectName() == 'keyboardViewMenuButton': service = item; break
        pending.extend(item.childItems())
    keyboard_overlay_checked = keyboard_overlay_checked and service is not None
    if service is not None: QMetaObject.invokeMethod(service, 'clicked')
    QGuiApplication.processEvents()
    settings_window = keyboard_window.findChild(QObject, 'keyboardSettingsWindow')
    keyboard_overlay_checked = (keyboard_overlay_checked and settings_window.isVisible()
        and bool(settings_window.flags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        and keyboard_window.findChild(QObject, 'keyboardHeightSlider') is not None)
    bridge.virtual_keyboard.action.emit('back')
    keyboard_overlay_checked = keyboard_overlay_checked and not settings_window.isVisible() and bridge.virtual_keyboard.visible
    keyboard_overlay_steps['settings'] = keyboard_overlay_checked
    if QGuiApplication.platformName() == 'windows':
        from ngs.process import set_topmost
        overlay_watcher = QQmlEngine.contextForObject(window).engine().overlay_events
        keyboard_overlay_checked = keyboard_overlay_checked and len(overlay_watcher.hooks) == 2
        set_topmost(int(window.winId()), True)
        bridge.desktop_controls.windowStackChanged.emit()
        QGuiApplication.processEvents()
        QGuiApplication.processEvents()
        # Native WinEvent delivery and deferred restacking need a running event loop.
        settle = QEventLoop(); QTimer.singleShot(50, settle.quit); settle.exec()
        user.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user.GetWindow.restype = ctypes.c_void_p
        hwnd = int(window.winId())
        above = False
        for _ in range(2048):
            hwnd = user.GetWindow(hwnd, 3)
            if not hwnd: break
            if hwnd == int(keyboard_window.winId()): above = True; break
        keyboard_overlay_checked = keyboard_overlay_checked and above
        keyboard_overlay_steps['stack'] = dict(checked=keyboard_overlay_checked, above=above, hooks=len(overlay_watcher.hooks))
    bridge.virtual_keyboard.setTouchpads(True)
    QGuiApplication.processEvents()
    keyboard_overlay_checked = (keyboard_overlay_checked and not keyboard_window.isVisible()
        and all(window.findChild(QObject, name).isVisible() for name in ('leftTouchpadWindow', 'rightTouchpadWindow')))
    keyboard_overlay_steps['pads'] = keyboard_overlay_checked
    bridge.virtual_keyboard.setTouchpads(False)
    # Render the return from touchpads before testing a separate keyboard hide.
    settle = QEventLoop(); QTimer.singleShot(300, settle.quit); settle.exec()
    keyboard_overlay_checked = keyboard_overlay_checked and keyboard_window.isVisible()
    if old_keyboard_options is None: bridge.settings.pop('keyboard_options', None)
    else: bridge.settings['keyboard_options'] = old_keyboard_options
    bridge.save(); bridge.virtual_keyboard.changed.emit()
    bridge.virtual_keyboard.hide()
    settle = QEventLoop()
    checked_overlays = [window.findChild(QObject,name) for name in ('virtualKeyboardWindow','leftTouchpadWindow','rightTouchpadWindow','keyboardSettingsWindow')]
    def overlays_hidden(*unused):
        if not any(overlay.isVisible() for overlay in checked_overlays): settle.quit()
    for overlay in checked_overlays: overlay.visibleChanged.connect(overlays_hidden)
    QTimer.singleShot(1500,settle.quit)
    if any(overlay.isVisible() for overlay in checked_overlays): settle.exec()
    for overlay in checked_overlays: overlay.visibleChanged.disconnect(overlays_hidden)
    QGuiApplication.processEvents()
    if QGuiApplication.platformName() == 'windows':
        keyboard_overlay_checked = keyboard_overlay_checked and not overlay_watcher.hooks
        keyboard_overlay_steps['unhooked'] = keyboard_overlay_checked
        keyboard_overlay_steps['visible_after_hide'] = [overlay.objectName() for overlay in checked_overlays if overlay.isVisible()]
        keyboard_overlay_steps['after_hide'] = dict(shown=bridge.virtual_keyboard.visible,
            presenting=keyboard_window.property('presenting'), reveal=keyboard_window.property('reveal'))
    saved_theme = {key: bridge.settings.get(key) for key in ('theme', 'accent_color')}
    appearance_checked = True
    for theme in ('light', 'dark'):
        bridge.settings.update(theme=theme, accent_color='blue'); bridge.settingsChanged.emit()
        QGuiApplication.processEvents()
        appearance_checked = appearance_checked and window.color().name() == bridge.themeColors['background']
    for key, value in saved_theme.items():
        if value is None: bridge.settings.pop(key, None)
        else: bridge.settings[key] = value
    bridge.settingsChanged.emit()
    editor = window.findChild(QObject, 'padKeyboardEditor')
    diagram = editor.findChild(QObject, 'padKeyboardDiagram') if editor is not None else None
    diagram_checked = diagram is not None and diagram.property('buttons').property('length').toInt() >= 16
    ssl.create_default_context()
    from ngs.transfer import worker_command
    command = worker_command()
    import tempfile
    from ngs.catalog import atomic_write
    from ngs.install import build_manifest, manifest_bytes
    with tempfile.TemporaryDirectory(prefix="transfer-self-test-", dir=storage.temp) as temporary:
        probe = type(storage)(Path(temporary) / "launcher")
        source = Path(temporary) / "source"
        (source / "Game").mkdir(parents=True)
        (source / "Game/play.exe").write_bytes(b"self-test file, never executed")
        image = dict(path="/assets/ngs_launcher/test.jpg", size=1, sha256="0" * 64)
        card = dict(id="transfer-check", name="Transfer check", subtitle="", description="", genre="", accent="#d9ff82",
                    source_folder="Game", executable="play.exe", metadata_url="", revision=1, cover=image, background=image)
        data = manifest_bytes(build_manifest(source, card))
        import hashlib
        digest = hashlib.sha256(data).hexdigest()
        card["manifest"] = dict(path="/assets/ngs_launcher/files.json", size=len(data), sha256=digest)
        cache = CatalogCache(probe, "http://127.0.0.1:9/tactic")
        atomic_write(cache.folder / (digest + ".manifest.json"), data)
        queue = DownloadQueue(probe)
        try:
            queue.add(card, source, cache.origin)
            queue.run(card["id"], cache, isolated=True)
            hidden_transfer = (probe.games / card["name"] / "play.exe").read_bytes() == b"self-test file, never executed"
            probe_library = LocalLibrary(probe)
            probe_library.reconcile()
            probe_library.set_launch_arguments(card["id"], '-novid -name "Игрок один"')
            launch_options_checked = (LocalLibrary(probe).launch_spec(card["id"])[2] ==
                                      ["-novid", "-name", "Игрок один"])
            probe_library.forget(card["id"])
            forget_keeps_files = (not probe_library.reconcile() and
                (probe.games / card["name"] / "play.exe").read_bytes() == b"self-test file, never executed")
            setup = dict(card, id="setup-transfer-check", kind="installer", game_id=card["id"])
            data = manifest_bytes(build_manifest(source, setup))
            digest = hashlib.sha256(data).hexdigest()
            setup["manifest"] = dict(path="/assets/ngs_launcher/setup.json", size=len(data), sha256=digest)
            atomic_write(cache.folder / (digest + ".manifest.json"), data)
            queue.add(setup, source, cache.origin)
            queue.run(setup["id"], cache, isolated=True)
            setups = LocalLibrary(probe, installers=True)
            installer_transfer_checked = bool(setups.reconcile() and not probe_library.reconcile()
                and setups.launch_spec(setup["id"])[0] == probe.games / ".ngs-installers" / setup["name"] / "play.exe")
        finally: queue.close()
    from ngs.diagnostics import _current, SessionLog
    import zipfile
    session_log = _current or SessionLog(storage, 'self-check')
    marker = 'diagnostics-self-check-' + str(time.monotonic_ns())
    try:
        session_log.write('portable.diagnostics', probe=marker, password=marker+'-secret', file_identity=[2**80+17, 2**120+29])
        error = ValueError(marker)
        if _current is not None: sys.excepthook(type(error), error, None)
        else: session_log.capture_exception(type(error), error, None)
        session_log.pack()
        rows = []
        for path in session_log.archives.glob('*.zip'):
            with zipfile.ZipFile(path) as archive:
                rows.extend(json.loads(line) for line in archive.read('events.jsonl').decode().splitlines())
        entry = next(row for row in rows if row.get('probe') == marker)
        diagnostics_checked = (entry['password'] == '[redacted]' and entry['file_identity'] == [2**80+17, 2**120+29]
            and any(row.get('event') == 'exception' and marker in row.get('traceback', '') for row in rows))
    finally:
        if _current is None: session_log.finish()
    panel = window.findChild(QObject, 'systemPanel')
    controls = bridge.system_controls
    previous_controls = controls.state
    previous_kind = panel.property('kind')
    system_panels_checked = False
    try:
        controls.state = dict(previous_controls, key='volume', busy=False, error='', items=[],
            volume=60, muted=False, outputs=[dict(id='portable-output', name='Portable output')],
            endpoint='portable-output', sessions=[dict(id='portable-session', name='Portable session', volume=40, muted=False)],
            energy_saver=False, energy_saver_available=True, energy_saver_message='')
        panel.setProperty('kind', 'volume'); panel.setProperty('visible', True); controls.changed.emit()
        QGuiApplication.processEvents()
        system_panels_checked = (panel.findChild(QObject, 'audioSessions').property('count') == 1
            and panel.findChild(QObject, 'audioOutput').property('currentText') == 'Portable output'
            and panel.findChild(QObject, 'masterMute').property('visible'))
        panel.setProperty('kind', 'battery'); controls.state['key'] = 'battery'; controls.changed.emit()
        QGuiApplication.processEvents()
        system_panels_checked = (system_panels_checked
            and panel.findChild(QObject, 'energySaverToggle').property('visible')
            and panel.findChild(QObject, 'energySaverToggle').property('enabled'))
        panel.setProperty('kind', 'brightness'); controls.state.update(key='brightness', brightness=55); controls.changed.emit()
        QGuiApplication.processEvents()
        system_panels_checked = (system_panels_checked and panel.property('height') < window.height() * .6
            and panel.findChild(QObject, 'systemLevel').property('value') == 55)
    finally:
        panel.setProperty('visible', False); panel.setProperty('kind', previous_kind)
        controls.state = previous_controls; controls.changed.emit()
    # Exercise the replacement for the old one-click installed toggle.
    window.setProperty('section', 1); window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 6)
    bridge.action.emit('activate'); QGuiApplication.processEvents()
    filters_pane = window.findChild(QObject, 'gameFiltersPane')
    game_filters_checked = bool(filters_pane.property('visible'))
    filters_pane.setProperty('statusIndex', 2)
    filters_pane.setProperty('selectedField', 4)
    bridge.action.emit('activate'); QGuiApplication.processEvents()
    game_filters_checked = (game_filters_checked and bridge.hideInstalled
        and storage.read_settings().get('hide_installed') is True
        and window.property('installationFilter') == 'missing')
    splash = getattr(bridge.parent(), 'startup_screen', None)
    startup_screen_checked = splash is not None and splash.done and window.opacity() == 1
    report = dict(startup_screen_checked=startup_screen_checked, compiled="__compiled__" in globals(), root=str(storage.root), executable=sys.executable,
                  transfer_executable=command[0], transfer_executable_exists=Path(command[0]).is_file(),
                  hidden_transfer=hidden_transfer, forget_keeps_files=forget_keeps_files, launch_options_checked=launch_options_checked,
                  installers_section_checked=installers_section_checked, installer_transfer_checked=installer_transfer_checked,
                  download_details_checked=download_details_checked,
                  diagnostics_checked=diagnostics_checked, download_large_identity_checked=download_details_checked,
                  catalogizer_return_checked=catalogizer_return_checked,
                  system_status_checked=system_status_checked, tabs_require_confirmation=tabs_require_confirmation, tab_probe=tab_probe,
                  system_settings_handoff_checked=system_settings_handoff_checked, system_settings_states=system_settings_states,
                  keyboard_checked=keyboard_checked, keyboard_auto_checked=keyboard_auto_checked, keyboard_overlay_checked=keyboard_overlay_checked, keyboard_overlay_steps=keyboard_overlay_steps, keyboard_sizes=keyboard_sizes, bindings_hold_ui_checked=bindings_hold_ui_checked,
                  keyboard_opacity_checked=keyboard_opacity_checked, appearance_checked=appearance_checked, diagram_checked=diagram_checked,
                  input_hints_checked=input_hints_checked, system_panels_checked=bool(system_panels_checked),
                  header_help_checked=header_help_checked, game_filters_checked=game_filters_checked,
                  grid_search_checked=grid_search_checked, utility_navigation_checked=utility_navigation_checked,
                  added_selection_checked=added_selection_checked,
                  selection_wrap_checked=selection_wrap_checked,
                  tray_available=tray, tray_roundtrip=bool(tray and minimize_restored),
                  keyboard_geometry=keyboard_geometry, keyboard_dpr=keyboard_dpr, keyboard_area=bridge.virtual_keyboard.screenArea,
                  data=str(storage.data), resources=str(storage.resources), temp=os.environ.get("TEMP"), python_paths=sys.path,
                  popups=popups, downloads_opened=downloads_opened, header_badges_checked=bool(header_badges_checked), image_formats=formats,
                  screen_hz=window.screen().refreshRate(), render_loop=os.environ.get("QSG_RENDER_LOOP"),
                  elapsed_animation_driver=os.environ.get("QSG_USE_SIMPLE_ANIMATION_DRIVER"),
                  fullscreen=window.visibility() == window.Visibility.FullScreen,
                  scene_visible=window.findChild(QObject, "launcherScene").property("visible"),
                  settings_opened=settings_opened, hide_installed=window.property("bridge").hideInstalled,
                  static_background_for_game=bool(background), running_dashboard_checked=bool(running_dashboard_checked), restored=window.visibility() == previous,
                  minimize_restored=minimize_restored, toggle_restored=toggle_restored,
                  topmost_checked=topmost_checked, topmost_states=topmost_states,
                  modules={name: getattr(sys.modules.get(name), "__file__", "") for name in
                           ("PySide6", "PySide6.QtCore", "PySide6.QtQuick", "shiboken6", "_sqlite3", "_ssl")})
    report["ok"] = bool((splash is None or startup_screen_checked) and system_panels_checked and diagnostics_checked and catalogizer_return_checked and selection_wrap_checked and added_selection_checked and download_details_checked and header_badges_checked and installers_section_checked and installer_transfer_checked and hidden_transfer and forget_keeps_files and launch_options_checked and report["transfer_executable_exists"] and report["scene_visible"] and all(popups.values()) and downloads_opened and all(formats.values())
                        and background and running_dashboard_checked and report["restored"] and minimize_restored and toggle_restored and topmost_checked and system_status_checked and tabs_require_confirmation
                        and system_settings_handoff_checked and keyboard_checked and keyboard_auto_checked and keyboard_overlay_checked and keyboard_opacity_checked and appearance_checked and diagram_checked and bindings_hold_ui_checked and input_hints_checked and header_help_checked and game_filters_checked and grid_search_checked and utility_navigation_checked
                        and report["temp"] == str(storage.temp))
    from ngs import player_protocol
    crypto_key = player_protocol.RSA.generate(2048)
    crypto_wire, unused, crypto_id = player_protocol.request(crypto_key.public_key().export_key(), {'probe':True})
    crypto_payload, unused = player_protocol.open_message(crypto_wire, crypto_key.export_key(), player_protocol.key_id(crypto_key.public_key().export_key()), private=True)
    report['account_crypto_checked'] = crypto_payload['request_id'] == crypto_id and crypto_payload['probe'] is True
    report['login_gate_checked'] = bool(login_gate_checked)
    report['ok'] = report['ok'] and report['account_crypto_checked'] and login_gate_checked
    bridge.setGamesView(previous_view)
    (storage.logs / "portable-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    window.grabWindow().save(str(storage.logs / "portable-check.png"))
    return report["ok"]


def run(storage, windowed=False, self_test=False, probe=False):
    if QGuiApplication.instance() is None:
        configure_graphics_environment(storage)
    app = QGuiApplication.instance() or QGuiApplication([])
    from ngs.i18n import configure
    configure(storage)
    app.setApplicationName("NGS")
    app.setOrganizationName("NGS")
    app.setWindowIcon(QIcon(str(storage.resources / "assets/ngs.ico")))
    splash = None
    if not probe:
        from ngs.startup_screen import StartupScreen
        splash = StartupScreen(storage, windowed)
        if splash.cancelled:
            splash.close()
            return 0
    try:
        engine, window = create_window(storage, demo=probe, self_test=self_test, startup_screen=splash)
    except Exception:
        if splash is not None: splash.close()
        raise
    if splash is not None: splash.attach(window)
    engine.startup_screen = splash
    if not (self_test or probe):
        from ngs.update_health import watch_first_start
        def install_confirmed_input_host():
            if not engine.bridge._closing and (Path(os.environ["ProgramFiles"]) / "NGS Input/NGSInput.exe").is_file():
                engine.bridge.installInputHost()
        confirmed_input_timer = QTimer(engine)
        confirmed_input_timer.setSingleShot(True)
        confirmed_input_timer.setInterval(0)
        confirmed_input_timer.timeout.connect(install_confirmed_input_host)
        def startup_confirmed():
            engine.bridge._first_start_pending = False
            engine.bridge._update["status"] = tr("Updated to version ") + engine.bridge._update["current"]
            engine.bridge.updatesChanged.emit()
            if QGuiApplication.platformName() == "windows" and not engine.bridge._closing and (engine.input_host is None or getattr(engine.input_host, "portable", False)) and getattr(engine.bridge, "_input_installer", None) is None:
                confirmed_input_timer.start()
        watch_first_start(storage, window, app,
            ready=lambda: not engine.bridge._closing and (splash is None or splash.done),
            ready_changed=engine.bridge.settingsChanged, closing=lambda: engine.bridge._closing,
            confirmed=startup_confirmed)
        from ngs.diagnostics import _current
        if _current is not None: _current.start_uploads()
    if probe:
        QTimer.singleShot(100, app.quit)
    else:
        is_opengl = getattr(engine.bridge, "_boot_graphics", ("d3d11", True))[0] == "opengl"
        if is_opengl and not self_test and splash is None:
            window.setOpacity(0.0)
            def reveal_opengl_window():
                from shiboken6 import isValid
                if isValid(window) and window.opacity() < 1.0:
                    window.setOpacity(1.0)
            window.frameSwapped.connect(reveal_opengl_window, Qt.ConnectionType.QueuedConnection)
            QTimer.singleShot(600, reveal_opengl_window)
        if windowed or (not self_test and engine.bridge.settings.get("window_mode") == "windowed"):
            window.show()
        else:
            window.showFullScreen()
    logging.info("Launcher started; portable root=%s", storage.root)
    if self_test:
        def finish_check():
            try:
                app.exit(0 if portable_check(storage, window) else 1)
            except Exception:
                logging.exception("Portable self-test failed")
                app.exit(1)
        QTimer.singleShot(1800, finish_check)
    try:
        return app.exec()
    finally:
        for step in (
            engine.power.close,
            lambda: setattr(engine.bridge, '_closing', True),
            lambda: engine.gamepad.set_active(False),
            engine.bridge.stop,
            engine.bridge.download_queue.close,
            engine.bridge.save,
            lambda: window.hide() if __import__('shiboken6').isValid(window) else None,
            engine.deleteLater,
            app.processEvents,
        ):
            try:
                step()
            except Exception:
                logging.exception("Launcher shutdown step failed")
