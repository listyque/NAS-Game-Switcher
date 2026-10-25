"""Windowed NAS catalogizer, with explicit CLI diagnostics for the admin PC."""

import argparse
import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from ngs.catalog import CatalogCache, atomic_write, relative_game_path
from ngs.install import build_manifest, fetch_manifest, is_link, manifest_bytes, safe_path, LocalLibrary, check_cancel
from ngs.metadata import search_title, game_metadata, metadata_reference, METADATA_REVISION
from ngs.portable import PortableStorage, path_key, configured_paths
from ngs.scanner import scan_executables, check_pe
from ngs.tactic import TacticClient
from tools.tactic_catalog import invoke, install_resolver, folder_key, merge_import
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password


def scan_folders(source, cancel=None):
    source = Path(source)
    if not source.is_absolute():
        raise ValueError("NAS folders require an absolute path")
    source = safe_path(Path(source.anchor), source.relative_to(source.anchor).as_posix())
    folders = []
    with os.scandir(source) as entries:
        for count, entry in enumerate(entries):
            if cancel is not None:
                check_cancel(cancel)
            if count >= 100_000:
                raise ValueError("Too many folders. Choose a more specific NAS root.")
            if entry.is_dir(follow_symlinks=False) and not is_link(entry.stat(follow_symlinks=False)):
                if entry.name.casefold() == "ngs":
                    marker = Path(entry.path) / "NGS-updates.json"
                    try:
                        if (marker.is_file() and not marker.is_symlink() and marker.stat().st_size < 256
                                and json.loads(marker.read_text(encoding="utf8")) == {"schema": 1, "application": "NAS Game Switcher updater"}):
                            continue
                    except (OSError, ValueError):
                        pass
                folders.append(relative_game_path(entry.name))
    return sorted(folders, key=folder_key)


def select(rows, label):
    for index, row in enumerate(rows, 1):
        print("%d. %s" % (index, label(row)))
    while rows:
        value = input("Номер (Enter — пропустить): ").strip()
        if not value:
            return None
        if value.isdigit() and 1 <= int(value) <= len(rows):
            return rows[int(value) - 1]
        print("Выбери номер из списка.")
    return None


def encoded(data):
    return {"base64": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest()}


def folder_steam_id(source, folder):
    import re
    folder = relative_game_path(folder)
    try:
        root = safe_path(Path(source), folder)
    except (OSError, ValueError):
        return None
    for name in ("steam_appid.txt", "steam_settings/steam_appid.txt", "steam_emu.ini", "steam_api.ini", "steam_api64.ini"):
        try:
            path = safe_path(root, name)
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                continue
            text = raw.decode("utf-8-sig", errors="replace").strip()
            ids = ({text} if name.endswith("steam_appid.txt") else
                   set(re.findall(r"(?im)^\s*(?:AppId|SteamAppId)\s*=\s*([1-9][0-9]{0,9})\s*(?:[;#].*)?$", text)))
            if len(ids) == 1 and re.fullmatch(r"[1-9][0-9]{0,9}", next(iter(ids))):
                return next(iter(ids))
        except (OSError, ValueError):
            continue
    return None


def metadata(client, operation, value, storage=None, source=None, folder=None):
    if (operation == "search" and source is not None and folder is not None
            and search_title(value).casefold() == search_title(folder).casefold()):
        app_id = folder_steam_id(source, folder)
        if app_id:
            rows = metadata(client, "search", app_id)
            if rows:
                return rows
    cache_file = None
    if operation == "details" and storage is not None:
        reference = metadata_reference(value)
        key = hashlib.sha256(reference.encode("utf8")).hexdigest()
        cache_file = safe_path(storage.cache, "catalogizer-metadata-%d-%s.json" % (METADATA_REVISION, key))
        if cache_file.is_file() and 0 <= time.time() - cache_file.stat().st_mtime < 86400 and cache_file.stat().st_size <= 32 * 1024 * 1024:
            try:
                result = json.loads(cache_file.read_bytes())
                if result["provider_id"] != reference or not isinstance(result["card"], dict):
                    raise ValueError("Invalid metadata cache")
                for context in ("cover", "background"):
                    image = result[context]
                    if len(image["base64"]) > 12 * 1024 * 1024:
                        raise ValueError("Oversized metadata cache image")
                    data = base64.b64decode(image["base64"], validate=True)
                    if not 0 < len(data) <= 8 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != image["sha256"]:
                        raise ValueError("Invalid metadata cache digest")
                return result
            except (ValueError, OSError, KeyError, TypeError):
                pass
    try:
        result = invoke(client, "metadata_" + operation, {"value": value})
    except (RuntimeError, OSError):
        print("Сервер не получил метаданные. Повторяю запрос оформления с этого ПК.")
        result = game_metadata(operation, value)
    if cache_file is not None:
        atomic_write(cache_file, json.dumps(result, ensure_ascii=False).encode("utf8"))
    return result


def prepare_import(records, folder, details, executable, edits=None, source=None, legacy_source=None):
    """Preview may omit EXE; the publication queue and manifest require a real launch path."""
    records = [r for r in records if r["card"].get("kind") == details["card"].get("kind")]
    folder, executable = relative_game_path(folder), relative_game_path(executable, allow_empty=True)
    installers = details["card"].get("kind") == "installer"
    provider = details["provider_id"]
    bound = [r for r in records if folder_key(r["card"]["source_folder"]) == folder_key(folder)
             and (source is None or path_key(r["card"].get("source_root") or legacy_source or source) == path_key(source))
             and (not installers or (provider == "manual" and r["import_state"].get("provider_id") == "manual"
                                     and folder_key(r["card"]["executable"]) == folder_key(executable)))]
    if len(bound) > 1:
        raise ValueError("This folder has several catalog entries. Fix the link in TACTIC first.")
    current = bound[0] if bound else None
    metadata_matches = [r for r in records if (provider != "manual" and (
                        r["import_state"].get("provider_id") == provider or provider in r["import_state"].get("provider_ids", [])))
                        or (details["card"]["metadata_url"] and r["card"]["metadata_url"].rstrip("/") == details["card"]["metadata_url"].rstrip("/"))]
    if len(metadata_matches) > 1 or (current and metadata_matches and current["card"]["id"] != metadata_matches[0]["card"]["id"]):
        raise ValueError("The folder and metadata source are linked to different entries. Fix the link in TACTIC.")
    current = current or (metadata_matches[0] if metadata_matches else None)
    if installers and current and provider != "manual" and current["card"].get("game_id") != details["card"].get("game_id"):
        raise ValueError("This installer entry belongs to another game. Create a separate entry.")
    if current and current["card"]["source_folder"] and folder_key(current["card"]["source_folder"]) != folder_key(folder):
        raise ValueError("The game is already in another NAS folder. Automatic relinking is blocked.")
    if (source and current and current["card"]["source_folder"]
            and path_key(current["card"].get("source_root") or legacy_source or source) != path_key(source)):
        raise ValueError("The game is already linked to another NAS source. Automatic transfer is blocked.")
    proposed = dict(details["card"], source_folder=folder, executable=executable, available_on_nas=True)
    if source:
        proposed["source_root"] = str(Path(source))
    if current:
        proposed["id"] = current["card"]["id"]
    edits = edits or {}
    merged, _, preserved = merge_import(current["card"] if current else None, proposed,
                                        current["import_state"] if current else {}, edits)
    return dict(current=current, proposed=proposed, merged=merged, preserved=preserved, edits=edits,
                details=details, legacy_source=legacy_source or "")


def publish_folder(client, storage, source, prepared, full_hash=False, cancel=None,
                   progress=None, before_commit=None, retry_export=False):
    cancel = cancel or threading.Event()
    current, proposed = prepared["current"], prepared["proposed"]
    folder = proposed["source_folder"]
    check_cancel(cancel)
    from tools.http_packages import prepare_remote, server_source
    remote_packages = server_source(source)
    if not remote_packages:
        check_pe(safe_path(safe_path(Path(source), folder), proposed["executable"]))
    key = hashlib.sha256((str(Path(source)) + "\0" + folder).encode("utf8")).hexdigest()
    cache_file = safe_path(storage.cache, "manifest-hashes-" + key + ".json")
    hashes = {}
    if not full_hash and cache_file.is_file() and cache_file.stat().st_size <= 32 * 1024 * 1024:
        try:
            hashes = json.loads(cache_file.read_bytes())
            if not isinstance(hashes, dict): hashes = {}
        except (ValueError, OSError):
            pass
    launch = {}
    if current and current["published"].get("manifest") and all(current["published"].get(k) == proposed[k] for k in ("source_folder", "executable")):
        previous = fetch_manifest(CatalogCache(storage, client.endpoint.split("/ngs_launcher/REST/")[0]), current["published"], cancel)
        launch = {"arguments": previous["arguments"], "working_directory": previous["working_directory"]}
    if remote_packages:
        document = prepare_remote(client, storage, proposed, launch, cancel, progress or (lambda *args: None))
    else:
        document = build_manifest(source, proposed, cancel=cancel, progress=progress or (lambda *args: None), hash_cache=hashes, **launch)
        atomic_write(cache_file, json.dumps(hashes, ensure_ascii=False).encode("utf8"))
    check_cancel(cancel)
    if before_commit:
        before_commit()
    # Once committed, finish export even if cancellation arrived during the request.
    imported = invoke(client, "import", dict(prepared["details"], card=proposed, edits=prepared["edits"], confirmed=True,
                      legacy_source=prepared.get("legacy_source", ""),
                      available_on_nas=True, expected=current["token"] if current else None,
                      manifest=encoded(manifest_bytes(document))))
    if imported["status"] != "unchanged" or retry_export:
        exported = invoke(client, "export")
        invoke(client, "activate", exported)
    return imported


def import_folder(client, storage, source, folder, full_hash=False):
    records = invoke(client, "inspect")["records"]
    query = input("Поиск [%s]: " % search_title(folder)).strip() or search_title(folder)
    selected = select(metadata(client, "search", query, source=source, folder=folder), lambda row: row["name"] + " · " + row["app_id"])
    if selected is None:
        return None
    details = metadata(client, "details", selected["app_id"])
    result = scan_executables(safe_path(Path(source), folder), allow_network=True)
    if result["limited"] or result["skipped"]:
        print("Внимание: поиск EXE неполный, некоторые папки пропущены.")
    executable = select(result["candidates"], lambda row: row["path"] + (" [служебный EXE]" if row["utility"] else ""))
    if executable is None:
        return None
    edits = {}
    for field, label in (("name", "Title"), ("description", "Описание")):
        value = input(label + " вручную (Enter — оставить): ").strip()
        if value:
            edits[field] = value
    settings = storage.read_settings()
    prepared = prepare_import(records, folder, details, executable["path"], edits, source,
                              settings.get("legacy_games_source", settings["games_source"]))
    print(json.dumps(prepared["merged"], ensure_ascii=False, indent=2))
    print("Сохранены ручные / прежние поля:", ", ".join(prepared["preserved"]))
    print("Источник:", str(source), "·", folder, "·", executable["path"])
    if input("Подтвердить игру, EXE и публикацию? Введи ДА: ").strip().casefold() not in ("да", "yes"):
        return None
    last = [0.0]
    def progress(phase, done, total, path):
        if time.monotonic() - last[0] >= 1:
            print("Манифест: %.1f%% · %s" % (done / max(1, total) * 100, path))
            last[0] = time.monotonic()
    imported = publish_folder(client, storage, source, prepared, full_hash, progress=progress)
    print("TACTIC:", imported["status"], "·", imported["game"], "· ревизия", imported["revision"])
    return imported


def sync_metadata(client, storage, base_url):
    pending = safe_path(storage.state, "metadata-pending")
    if not pending.is_dir():
        return 0
    library, count = LocalLibrary(storage), 0
    for path in sorted(pending.glob("steam-*.json")):
        if path.is_symlink() or not path.resolve().is_relative_to(pending.resolve()) or path.stat().st_size > 1024:
            raise ValueError("Некорректная очередь метаданных")
        request = json.loads(path.read_bytes())
        result = invoke(client, "metadata_resolve", {"value": request["app_id"]})
        card = result["card"]
        CatalogCache(storage, base_url).cache_artwork(card)
        for receipt in library.reconcile():
            if receipt.get("kind") == "external" and (receipt.get("catalog_id") == "steam-" + str(request["app_id"])
                    or receipt["card"].get("metadata_url") == card["metadata_url"]):
                library.update_external_metadata(receipt["card"]["id"], card)
        path.unlink()
        count += 1
        print("Оформление в TACTIC:", card["name"], "· наличие на NAS:", card.get("available_on_nas", True))
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="List NAS folders without publishing or login")
    parser.add_argument("--folder", help="Review one folder, relative to the configured NAS root")
    parser.add_argument("--source", help="One configured NAS root for a CLI import")
    parser.add_argument("--sync-metadata", action="store_true", help="Publish pending local-only metadata")
    parser.add_argument("--prepare", action="store_true", help="Update only the owned NGS schema and saved resolver")
    parser.add_argument("--full-hash", action="store_true", help="Rehash every source byte instead of reusing size/mtime hashes")
    parser.add_argument("--credentials-env", type=Path, default=DEFAULT_CREDENTIALS)
    args = parser.parse_args()
    storage = PortableStorage(Path(__file__).resolve().parents[1])
    storage.configure_process()
    settings = storage.read_settings()
    sources = configured_paths(settings, "games_sources", "games_source", r"\\NAS\Files\games")
    source, url = args.source or sources[0], settings["tactic_url"]
    if path_key(source) not in {path_key(p) for p in sources}:
        raise ValueError("Сначала добавь источник NAS в настройки.")
    if not any((args.scan, args.folder, args.sync_metadata, args.prepare, args.full_hash)):
        from tools.catalogizer_gui import run
        return run(storage)
    if args.scan:
        for root in ([source] if args.source else sources):
            for folder in scan_folders(root): print(str(Path(root) / folder))
        return
    client = admin_client(url, args.credentials_env)
    if args.prepare:
        print(invoke(client, "install"))
        print(install_resolver(client))
        return
    if args.sync_metadata:
        print("Синхронизировано:", sync_metadata(client, storage, url))
        return
    folder = relative_game_path(args.folder) if args.folder else select(scan_folders(source), lambda row: row)
    if folder:
        import_folder(client, storage, source, folder, args.full_hash)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        raise SystemExit(str(error)) from None
