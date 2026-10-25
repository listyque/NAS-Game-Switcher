import base64
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import time
import uuid
import zipfile

from ngs.catalog import atomic_write
from ngs.install import game_id, safe_path
from ngs.portable import path_list

MAX_BACKUP_BYTES = 2 * 1024 * 1024
UPLOAD_CHUNK = 1024
DOWNLOAD_CHUNK = 24 * 1024
MAX_BACKUPS_PER_DEVICE = 25
MAX_DEVICES_PER_PROFILE = 32
SQLITE_FILES = ("library.sqlite", "installers.sqlite", "local-scans.sqlite")


def clean_device_name(value):
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:64] or "Windows"


def default_device_name():
    try:
        return clean_device_name(platform.node())
    except Exception:
        return "Windows"


def validate_backup_request(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid backup request")
    action = value.get("action")
    if action == "list":
        if set(value) != {"action"}:
            raise ValueError("Invalid backup list fields")
        return {"action": "list"}

    if action not in ("upload", "finish", "download"):
        raise ValueError("Invalid backup action")

    target_device = value.get("target_device")
    backup_id = value.get("backup_id")
    if not isinstance(target_device, str) or not re.fullmatch(r"[0-9a-f]{32}", target_device):
        raise ValueError("Invalid target device")
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise ValueError("Invalid backup id")

    if action == "download":
        if set(value) != {"action", "target_device", "backup_id", "offset"}:
            raise ValueError("Invalid backup download fields")
        offset = value.get("offset")
        if type(offset) is not int or not 0 <= offset <= MAX_BACKUP_BYTES:
            raise ValueError("Invalid backup offset")
        return {
            "action": "download",
            "target_device": target_device,
            "backup_id": backup_id,
            "offset": offset,
        }

    device_name = value.get("device_name")
    digest = value.get("sha256")
    size = value.get("size")
    if not isinstance(device_name, str) or not 1 <= len(device_name.strip()) <= 64 or any(ord(c) < 32 for c in device_name):
        raise ValueError("Invalid device name")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid backup digest")
    if type(size) is not int or not 1 <= size <= MAX_BACKUP_BYTES:
        raise ValueError("Invalid backup size")

    if action == "upload":
        if set(value) != {"action", "target_device", "device_name", "backup_id", "sha256", "size", "offset", "data"}:
            raise ValueError("Invalid backup upload fields")
        offset = value.get("offset")
        encoded = value.get("data")
        if type(offset) is not int or not 0 <= offset < size:
            raise ValueError("Invalid backup upload offset")
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= 1400:
            raise ValueError("Invalid backup chunk")
        try:
            chunk = base64.b64decode(encoded.encode("ascii"), validate=True)
        except Exception as error:
            raise ValueError("Invalid backup base64") from error
        if not 1 <= len(chunk) <= UPLOAD_CHUNK or offset + len(chunk) > size:
            raise ValueError("Backup chunk exceeds size")
        return {
            "action": "upload",
            "target_device": target_device,
            "device_name": clean_device_name(device_name),
            "backup_id": backup_id,
            "sha256": digest,
            "size": size,
            "offset": offset,
            "data": encoded,
        }

    if set(value) != {"action", "target_device", "device_name", "backup_id", "sha256", "size", "games_count"}:
        raise ValueError("Invalid backup finish fields")
    games_count = value.get("games_count")
    if type(games_count) is not int or not 0 <= games_count <= 50000:
        raise ValueError("Invalid backup games count")
    return {
        "action": "finish",
        "target_device": target_device,
        "device_name": clean_device_name(device_name),
        "backup_id": backup_id,
        "sha256": digest,
        "size": size,
        "games_count": games_count,
    }


def _snapshot_sqlite_bytes(source_path, temp_dir):
    if not source_path.is_file():
        return None
    temp_path = safe_path(temp_dir, f"backup-{uuid.uuid4().hex}-{source_path.name}")
    try:
        with closing(sqlite3.connect(source_path)) as src, closing(sqlite3.connect(temp_path)) as dst:
            src.backup(dst)
        with closing(sqlite3.connect(temp_path)) as dst:
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.execute("VACUUM")
        data = temp_path.read_bytes()
        return data if len(data) <= MAX_BACKUP_BYTES else None
    except Exception:
        return None
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                Path(str(temp_path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass


def count_local_games(storage):
    ids = set()
    installs_dir = safe_path(storage.state, "installs")
    if installs_dir.is_dir():
        for item in installs_dir.glob("*.json"):
            if not item.name.endswith(".pending.json") and item.is_file():
                ids.add(item.stem)
    db_path = safe_path(storage.state, "library.sqlite")
    if db_path.is_file():
        try:
            with closing(sqlite3.connect(db_path)) as db:
                for (gid,) in db.execute("SELECT game_id FROM installed"):
                    if isinstance(gid, str) and gid:
                        ids.add(gid)
        except Exception:
            pass
    return len(ids)


def pack_backup(storage, device_id, device_name=""):
    device_name = clean_device_name(device_name or default_device_name())
    settings = storage.read_settings()
    export_settings = {
        key: settings[key]
        for key in (
            "games_directory",
            "games_directories",
            "games_source",
            "games_sources",
            "ui_scale",
            "theme",
            "accent_color",
            "graphics_api",
            "disable_performance_overlays",
            "hide_installed",
            "library_sort",
            "games_view",
            "resolve_local_metadata",
            "desktop_shortcuts",
            "keyboard_options",
            "keyboard_pad",
            "gamepad_keyboard",
            "custom_keyboard_layouts",
            "tactic_url",
            "tactic_project",
        )
        if key in settings
    }
    installs = {}
    for folder_name in ("installs", "installers"):
        folder = safe_path(storage.state, folder_name)
        if not folder.is_dir():
            continue
        for marker in sorted(folder.glob("*.json")):
            if marker.name.endswith(".pending.json") or not marker.is_file():
                continue
            try:
                if marker.stat().st_size > 64 * 1024:
                    continue
                parsed = json.loads(marker.read_bytes())
                if isinstance(parsed, dict) and isinstance(parsed.get("card"), dict):
                    installs[f"{folder_name}/{marker.name}"] = json.dumps(
                        parsed, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf8")
            except Exception:
                continue

    games_count = sum(1 for key in installs if key.startswith("installs/"))
    meta = {
        "schema": 1,
        "device": str(device_id or ""),
        "device_name": device_name,
        "created_at": int(time.time()),
        "games_count": games_count,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("backup.json", json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf8"))
        archive.writestr(
            "settings.json",
            json.dumps(export_settings, ensure_ascii=False, indent=2).encode("utf8"),
        )
        for rel_path, raw in installs.items():
            archive.writestr(rel_path, raw)
        for sqlite_name in SQLITE_FILES:
            raw_db = _snapshot_sqlite_bytes(safe_path(storage.state, sqlite_name), storage.temp)
            if raw_db:
                archive.writestr(f"state/{sqlite_name}", raw_db)

    zip_bytes = buffer.getvalue()
    if len(zip_bytes) > MAX_BACKUP_BYTES:
        raise ValueError("The backup exceeds the 2 MiB limit.")
    return zip_bytes, meta


def _merge_restored_sqlite(target_path, source_bytes, temp_dir):
    temp_path = safe_path(temp_dir, f"restore-{uuid.uuid4().hex}-{target_path.name}")
    try:
        atomic_write(temp_path, source_bytes)
        with closing(sqlite3.connect(temp_path)) as src:
            check = src.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise ValueError("Corrupt SQLite database in backup")
            tables = {
                row[0]
                for row in src.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if not target_path.is_file():
                with closing(sqlite3.connect(target_path)) as dst:
                    src.backup(dst)
                return

            with closing(sqlite3.connect(target_path)) as dst, dst:
                if "installed" in tables:
                    dst.execute("CREATE TABLE IF NOT EXISTS installed (game_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
                    rows = src.execute("SELECT game_id, receipt FROM installed").fetchall()
                    dst.executemany(
                        "INSERT INTO installed VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET receipt=excluded.receipt",
                        rows,
                    )
                if "play_history" in tables:
                    dst.execute("CREATE TABLE IF NOT EXISTS play_history (game_id TEXT PRIMARY KEY, played_at INTEGER NOT NULL)")
                    rows = src.execute("SELECT game_id, played_at FROM play_history").fetchall()
                    dst.executemany(
                        "INSERT INTO play_history VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET played_at=MAX(play_history.played_at, excluded.played_at)",
                        rows,
                    )
                if "launch_options" in tables:
                    dst.execute("CREATE TABLE IF NOT EXISTS launch_options (game_id TEXT PRIMARY KEY, arguments TEXT NOT NULL)")
                    rows = src.execute("SELECT game_id, arguments FROM launch_options").fetchall()
                    dst.executemany(
                        "INSERT INTO launch_options VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET arguments=excluded.arguments",
                        rows,
                    )
                if "scans" in tables:
                    dst.execute("CREATE TABLE IF NOT EXISTS scans (server TEXT, folder TEXT, root TEXT, row TEXT, PRIMARY KEY (server, folder))")
                    rows = src.execute("SELECT server, folder, root, row FROM scans").fetchall()
                    dst.executemany(
                        "INSERT INTO scans VALUES (?, ?, ?, ?) ON CONFLICT(server, folder) DO UPDATE SET root=excluded.root, row=excluded.row",
                        rows,
                    )
                if "play_sessions" in tables:
                    dst.execute(
                        "CREATE TABLE IF NOT EXISTS play_sessions ("
                        "session_id TEXT PRIMARY KEY, game_id TEXT NOT NULL, title TEXT NOT NULL, "
                        "started_at INTEGER NOT NULL, ended_at INTEGER NOT NULL, seconds INTEGER NOT NULL, "
                        "active INTEGER NOT NULL DEFAULT 0)"
                    )
                    rows = src.execute(
                        "SELECT session_id, game_id, title, started_at, ended_at, seconds, active FROM play_sessions"
                    ).fetchall()
                    dst.executemany(
                        "INSERT INTO play_sessions VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET "
                        "ended_at=MAX(play_sessions.ended_at, excluded.ended_at), "
                        "seconds=MAX(play_sessions.seconds, excluded.seconds)",
                        [(r[0], r[1], r[2], r[3], r[4], r[5], 0) for r in rows],
                    )
                if "player_totals" in tables:
                    dst.execute(
                        "CREATE TABLE IF NOT EXISTS player_totals ("
                        "game_id TEXT PRIMARY KEY, title TEXT NOT NULL, seconds INTEGER NOT NULL, "
                        "launches INTEGER NOT NULL, last_played INTEGER NOT NULL)"
                    )
                    rows = src.execute(
                        "SELECT game_id, title, seconds, launches, last_played FROM player_totals"
                    ).fetchall()
                    dst.executemany(
                        "INSERT INTO player_totals VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(game_id) DO UPDATE SET "
                        "title=excluded.title, "
                        "seconds=MAX(player_totals.seconds, excluded.seconds), "
                        "launches=MAX(player_totals.launches, excluded.launches), "
                        "last_played=MAX(player_totals.last_played, excluded.last_played)",
                        rows,
                    )
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                Path(str(temp_path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass


def _restore_settings_document(storage, incoming):
    if not isinstance(incoming, dict):
        return False
    current = storage.read_settings()
    merged = dict(current)

    for key in (
        "ui_scale",
        "theme",
        "accent_color",
        "graphics_api",
        "disable_performance_overlays",
        "hide_installed",
        "library_sort",
        "games_view",
        "resolve_local_metadata",
        "desktop_shortcuts",
        "keyboard_options",
        "keyboard_pad",
        "gamepad_keyboard",
        "custom_keyboard_layouts",
    ):
        if key in incoming:
            merged[key] = incoming[key]

    for plural, singular, absolute in (
        ("games_sources", "games_source", True),
        ("games_directories", "games_directory", False),
    ):
        try:
            raw_list = incoming.get(plural)
            if not isinstance(raw_list, list) and isinstance(incoming.get(singular), str):
                raw_list = [incoming[singular]]
            if isinstance(raw_list, list) and raw_list:
                cleaned = path_list([str(item) for item in raw_list if isinstance(item, str)], plural, absolute)
                merged[plural] = cleaned
                merged[singular] = cleaned[0]
        except Exception:
            pass

    storage.write_settings(merged)
    return True


def restore_backup(storage, zip_bytes, *, apply_settings=True, apply_library=True):
    if not isinstance(zip_bytes, (bytes, bytearray)) or not 1 <= len(zip_bytes) <= MAX_BACKUP_BYTES:
        raise ValueError("Invalid backup archive.")
    restored_games = 0
    restored_settings = False
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as archive:
        names = archive.namelist()
        if "backup.json" not in names or len(names) > 10000:
            raise ValueError("The backup archive contains no metadata.")
        meta = json.loads(archive.read("backup.json"))
        if not isinstance(meta, dict) or meta.get("schema") != 1:
            raise ValueError("Unsupported backup archive version.")

        if apply_settings and "settings.json" in names:
            raw_settings = archive.read("settings.json")
            if len(raw_settings) <= 256 * 1024:
                restored_settings = _restore_settings_document(storage, json.loads(raw_settings))

        if apply_library:
            for folder_name in ("installs", "installers"):
                safe_path(storage.state, folder_name).mkdir(parents=True, exist_ok=True)
            for name in names:
                if name.startswith(("installs/", "installers/")) and name.endswith(".json"):
                    parts = name.split("/")
                    if len(parts) != 2 or parts[1].endswith(".pending.json"):
                        continue
                    try:
                        expected_id = game_id(parts[1][:-5])
                    except ValueError:
                        continue
                    raw = archive.read(name)
                    if len(raw) > 64 * 1024:
                        continue
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict) or not isinstance(parsed.get("card"), dict):
                        continue
                    if parsed["card"].get("id") != expected_id:
                        continue
                    target = safe_path(safe_path(storage.state, parts[0]), parts[1])
                    atomic_write(target, json.dumps(parsed, ensure_ascii=False).encode("utf8"))
                    if parts[0] == "installs":
                        restored_games += 1

            for sqlite_name in SQLITE_FILES:
                arc_name = f"state/{sqlite_name}"
                if arc_name in names:
                    raw_db = archive.read(arc_name)
                    if 0 < len(raw_db) <= MAX_BACKUP_BYTES:
                        _merge_restored_sqlite(safe_path(storage.state, sqlite_name), raw_db, storage.temp)

    return {
        "meta": meta,
        "restored_games": restored_games,
        "restored_settings": restored_settings,
    }


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Backup operation canceled.")


def list_backups(api, cancel=None):
    _check_cancel(cancel)
    return api.call("backup", {"action": "list"})


def upload_backup(api, storage, target_device, device_name="", cancel=None):
    if not re.fullmatch(r"[0-9a-f]{32}", str(target_device or "")):
        raise ValueError("Invalid backup device ID.")
    device_name = clean_device_name(device_name or default_device_name())
    zip_bytes, meta = pack_backup(storage, target_device, device_name)
    digest = hashlib.sha256(zip_bytes).hexdigest()
    backup_id = uuid.uuid4().hex
    total = len(zip_bytes)
    offset = 0
    while offset < total:
        _check_cancel(cancel)
        chunk = zip_bytes[offset:offset + UPLOAD_CHUNK]
        response = api.call(
            "backup",
            {
                "action": "upload",
                "target_device": target_device,
                "device_name": device_name,
                "backup_id": backup_id,
                "sha256": digest,
                "size": total,
                "offset": offset,
                "data": base64.b64encode(chunk).decode("ascii"),
            },
        )
        next_offset = response.get("offset")
        if type(next_offset) is not int or next_offset <= offset:
            raise RuntimeError("The backup server returned an invalid offset.")
        offset = next_offset

    _check_cancel(cancel)
    return api.call(
        "backup",
        {
            "action": "finish",
            "target_device": target_device,
            "device_name": device_name,
            "backup_id": backup_id,
            "sha256": digest,
            "size": total,
            "games_count": int(meta.get("games_count", 0)),
        },
    )


def download_backup(api, target_device, backup_id, cancel=None):
    if not re.fullmatch(r"[0-9a-f]{32}", str(target_device or "")):
        raise ValueError("Invalid device ID.")
    if not re.fullmatch(r"[0-9a-f]{32}", str(backup_id or "")):
        raise ValueError("Invalid backup revision ID.")
    chunks = bytearray()
    offset = 0
    expected_size = None
    expected_sha = None
    while True:
        _check_cancel(cancel)
        response = api.call(
            "backup",
            {
                "action": "download",
                "target_device": target_device,
                "backup_id": backup_id,
                "offset": offset,
            },
        )
        size = response.get("size")
        digest = response.get("sha256")
        encoded = response.get("data", "")
        if type(size) is not int or not 1 <= size <= MAX_BACKUP_BYTES:
            raise RuntimeError("The server returned an invalid backup size.")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("The server returned an invalid backup checksum.")
        if expected_size is None:
            expected_size = size
            expected_sha = digest
        elif expected_size != size or expected_sha != digest:
            raise RuntimeError("The backup changed during download.")
        chunk = base64.b64decode(encoded.encode("ascii"), validate=True) if encoded else b""
        if not chunk:
            raise RuntimeError("The server returned an empty backup chunk.")
        chunks.extend(chunk)
        offset += len(chunk)
        if len(chunks) > expected_size:
            raise RuntimeError("The downloaded backup exceeded its expected size.")
        if response.get("eof") or offset >= expected_size:
            break

    raw = bytes(chunks)
    if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise RuntimeError("Downloaded backup checksum mismatch.")
    return raw
