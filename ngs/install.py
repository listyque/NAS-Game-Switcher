"""Validated manifests, HTTP packages/local copies, and the portable library."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from ngs.catalog import atomic_write, relative_game_path, validate_card
from ngs.packages import copy_packages, package_workspace, validate_transport, verify_transport


MAX_MANIFEST = 32 * 1024 * 1024
RECEIPT = ".ngs-install.json"
BLOCK = 4 * 1024 * 1024
INSTALL_SPACE_RESERVE = 512 * 1024 * 1024


class Cancelled(Exception):
    pass


def require_local_source(path):
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("HTTP packages have not been prepared for this game. SMB downloads are disabled.")
    if os.name == "nt" and path.is_absolute():
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError("HTTP packages have not been prepared for this game. Network drives are not used.")
    return path


def game_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", value):
        raise ValueError("Invalid game ID.")
    return value


def game_folder_name(card):
    name = " ".join(re.sub(r'[\x00-\x1f\\/:<>"|?*]+', ' ', card["name"]).split())[:120].strip(" .") or "Game"
    try:
        relative_game_path(name)
    except ValueError:
        name = "Game " + name
    return name


def catalog_game_name(card, cards):
    from ngs.game_db import title_key
    from ngs.portable import path_key
    def metadata_key(value):
        match = re.match(r"^(https://store\.steampowered\.com/app/[0-9]+)(?:/|$)", value)
        return match[1] if match else value.rstrip("/")
    reference = metadata_key(card.get("metadata_url", ""))
    folder, root = card.get("source_folder", ""), card.get("source_root", "")
    title = title_key(card["name"])
    for matches in (
        (c for c in cards if c["id"] == card["id"]),
        (c for c in cards if reference and metadata_key(c.get("metadata_url", "")) == reference),
        (c for c in cards if folder and path_key(c.get("source_folder", "")) == path_key(folder)
         and (not root or not c.get("source_root") or path_key(c["source_root"]) == path_key(root))),
        (c for c in cards if title and title_key(c["name"]) == title),
    ):
        names = {c["name"] for c in matches}
        if len(names) > 1:
            raise ValueError("The catalog has multiple titles for this game; review its details.")
        if names: return names.pop()
    return card["name"]


def readable_folder(card, name):
    base = game_folder_name(card)
    return name == base or re.fullmatch(re.escape(base) + r" \((?:[2-9]|[1-9][0-9]+)\)", name) is not None


def game_destination(root, card, original=None):
    root = safe_path(root)
    base = game_folder_name(card)
    number = 1
    while True:
        name = base if number == 1 else f"{base} ({number})"
        target = root / name
        if target == original or not os.path.lexists(target):
            return safe_path(root, name)
        number += 1


def install_destination(storage, card):
    library = LocalLibrary(storage, installers=card.get("kind") == "installer")
    if any(row["card"]["id"] == card["id"] for row in library.reconcile()):
        raise FileExistsError("A local copy of this game is already registered; overwriting is not allowed.")
    return game_destination(storage.games, card)


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def safe_path(root, relative=""):
    root = Path(root)
    if not root.is_absolute() or is_link(root.lstat()):
        raise ValueError("The game root must be a normal folder with an absolute path.")
    if relative:
        relative_game_path(relative)
    target = root
    for component in relative.split("/") if relative else ():
        target = target / component
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if is_link(info):
            raise ValueError("Links and reparse points are not allowed in game files: " + relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("The path escapes the game folder.")
    return target


def validate_manifest(document, card=None):
    if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1:
        raise ValueError("Unknown manifest format.")
    identity = game_id(document.get("game_id"))
    source = relative_game_path(document.get("source_folder"))
    executable = relative_game_path(document.get("executable"))
    if not executable.lower().endswith(".exe"):
        raise ValueError("The executable must be an EXE file.")
    cwd = relative_game_path(document.get("working_directory", ""), allow_empty=True)
    arguments = document.get("arguments", [])
    if (not isinstance(arguments, list) or len(arguments) > 64 or any(
            not isinstance(arg, str) or len(arg) > 2048 or "\0" in arg for arg in arguments)):
        raise ValueError("Invalid launch arguments.")
    files, directories = document.get("files"), document.get("directories", [])
    if (not isinstance(files, list) or not files or len(files) > 200_000
            or not isinstance(directories, list) or len(directories) > 200_000):
        raise ValueError("Invalid manifest file list.")
    kinds, normalized, total = {}, [], 0
    for directory in directories:
        relative_game_path(directory)
        key = directory.casefold()
        if key in kinds or key.split("/")[0] == RECEIPT:
            raise ValueError("Manifest path conflict: " + directory)
        kinds[key] = "directory"
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Invalid file record.")
        path = relative_game_path(item.get("path"))
        size, digest = item.get("size"), item.get("sha256")
        if (type(size) is not int or not 0 <= size <= 2 ** 50 or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("Invalid file size or SHA256.")
        key = path.casefold()
        if key in kinds or key.split("/")[0] == RECEIPT:
            raise ValueError("Manifest path conflict: " + path)
        kinds[key] = "file"
        normalized.append({"path": path, "size": size, "sha256": digest})
        total += size
    for path in kinds:
        parts = path.split("/")
        if any(kinds.get("/".join(parts[:i])) == "file" for i in range(1, len(parts))):
            raise ValueError("A file is used as a folder: " + path)
    if executable not in {item["path"] for item in normalized}:
        raise ValueError("The executable is missing from the manifest.")
    if cwd and cwd.casefold() not in {path for path, kind in kinds.items() if kind == "directory"}:
        raise ValueError("The working directory is missing from the manifest.")
    if type(document.get("total_size")) is not int or document["total_size"] != total:
        raise ValueError("The manifest total size does not match its file list.")
    if card and (identity != card["id"] or source != card["source_folder"] or executable != card["executable"]):
        raise ValueError("The manifest belongs to a different game or executable.")
    result = {"schema": 1, "game_id": identity, "source_folder": source, "executable": executable,
            "working_directory": cwd, "arguments": arguments, "files": normalized,
            "directories": directories, "total_size": total}
    if "http" in document:
        result["http"] = validate_transport(document["http"], result)
    return result


def manifest_bytes(document):
    data = json.dumps(validate_manifest(document), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")
    if len(data) > MAX_MANIFEST:
        raise ValueError("The manifest exceeds 32 MiB.")
    return data


def fetch_manifest(cache, card, cancel):
    reference = card.get("manifest")
    if not reference:
        raise ValueError("No file manifest has been published for this game yet.")
    target = safe_path(cache.folder, reference["sha256"] + ".manifest.json")
    data = None
    if target.is_file() and target.stat().st_size == reference["size"]:
        candidate = target.read_bytes()
        if hashlib.sha256(candidate).hexdigest() == reference["sha256"]:
            data = candidate
    downloaded = data is None
    if downloaded:
        data = cache.fetch(reference["path"], MAX_MANIFEST, cancel, time.monotonic() + 45)
        if len(data) != reference["size"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise ValueError("The manifest was corrupted during transfer.")
    document = validate_manifest(json.loads(data), card)
    if downloaded:
        atomic_write(target, data)
    return document


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("Copy canceled.")


def inventory(root, cancel=None, progress=lambda *args: None, ignore=()):
    cancel = cancel or threading.Event()
    root = Path(root)
    files, directories = {}, []
    pending, count = [""], 0
    while pending:
        check_cancel(cancel)
        folder = pending.pop()
        with os.scandir(safe_path(root, folder)) as entries:
            for entry in entries:
                check_cancel(cancel)
                if entry.name in ignore:
                    continue
                relative = relative_game_path(folder + "/" + entry.name if folder else entry.name)
                # Windows scandir already has size/mtime/reparse metadata from the directory listing.
                info = entry.stat(follow_symlinks=False)
                if is_link(info):
                    raise ValueError("A link was found in the source game: " + relative)
                if stat.S_ISDIR(info.st_mode):
                    directories.append(relative)
                    pending.append(relative)
                elif stat.S_ISREG(info.st_mode):
                    files[relative] = (info.st_size, info.st_mtime_ns)
                else:
                    raise ValueError("Unsupported file type: " + relative)
                count += 1
                progress(count, relative)
    return files, sorted(directories)


def hash_file(path, cancel, progress=lambda count: None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            check_cancel(cancel)
            chunk = stream.read(BLOCK)
            if not chunk:
                break
            digest.update(chunk)
            progress(len(chunk))
    return digest.hexdigest()


def build_manifest(source_root, card, cancel=None, progress=lambda *args: None, arguments=None, working_directory="", hash_cache=None):
    cancel = cancel or threading.Event()
    root = safe_path(Path(source_root), card["source_folder"])
    before, directories = inventory(root, cancel)
    document = {"schema": 1, "game_id": card["id"], "source_folder": card["source_folder"],
                "executable": card["executable"], "working_directory": working_directory,
                "arguments": arguments or [], "files": [{"path": p, "size": s[0], "sha256": "0" * 64}
                for p, s in sorted(before.items())], "directories": directories,
                "total_size": sum(s[0] for s in before.values())}
    validate_manifest(document, card)
    done = 0
    for item in document["files"]:
        check_cancel(cancel)
        path = safe_path(root, item["path"])
        def advanced(count):
            nonlocal done
            done += count
            progress("manifest", done, document["total_size"], item["path"])
        cached = hash_cache.get(item["path"]) if isinstance(hash_cache, dict) else None
        if (isinstance(cached, dict) and cached.get("size") == before[item["path"]][0]
                and cached.get("mtime_ns") == before[item["path"]][1]
                and isinstance(cached.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", cached["sha256"])):
            item["sha256"] = cached["sha256"]
            advanced(item["size"])
        else:
            item["sha256"] = hash_file(path, cancel, advanced)
    if inventory(root, cancel) != (before, directories):
        raise ValueError("The source game changed while building the manifest. Run the check again.")
    if isinstance(hash_cache, dict):
        hash_cache.clear()
        hash_cache.update({item["path"]: {"size": item["size"], "mtime_ns": before[item["path"]][1],
                                        "sha256": item["sha256"]} for item in document["files"]})
    return validate_manifest(document, card)


def remove_stage(storage, stage):
    # Delete only the unique staging folder created by this operation, never a game or the NAS source.
    if (stage.parent != storage.game_staging or not stage.resolve().is_relative_to(storage.game_staging.resolve())
            or is_link(stage.lstat())):
        raise ValueError("Refusing to delete an unrelated folder.")
    for base, directories, files in os.walk(stage, followlinks=False):
        if any(is_link((Path(base) / name).lstat()) for name in directories + files):
            raise ValueError("The partial folder contains a link; automatic cleanup is not allowed.")
    shutil.rmtree(stage)


@contextmanager
def installation_lock(storage):
    import msvcrt
    path = safe_path(storage.state, "install.lock")
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise OSError("Another NGS instance is already installing a game.") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def install_game(storage, source_root, card, document, cancel=None, progress=lambda *args: None, stage=None, http_cache=None, http_payload_ready=False):
    with installation_lock(storage):
        return _install_game(storage, source_root, card, document, cancel, progress, stage, http_cache, http_payload_ready)


def _install_game(storage, source_root, card, document, cancel=None, progress=lambda *args: None, stage=None, http_cache=None, http_payload_ready=False):
    card = validate_card(card)
    document = validate_manifest(document, card)
    if http_payload_ready and ('http' not in document or stage is None):
        raise ValueError("The completed HTTP copy must be inside the job's assigned folder.")
    cancel = cancel or threading.Event()
    destination = install_destination(storage, card)
    if destination.exists():
        raise FileExistsError("The local game folder already exists; overwriting is not allowed.")
    entries = len(document["files"]) + len(document["directories"])
    smb_files = document["files"]
    if "http" in document:
        if http_cache is None:
            raise ValueError("HTTP packages require a pinned download server.")
        source_root = Path(source_root)
        if (not source_root.is_absolute() or storage.games.is_relative_to(source_root)
                or source_root.is_relative_to(storage.games)):
            raise ValueError("The NAS source and installation folders must not overlap.")
        from ngs.server_trust import server_key_path
        verify_transport(document, server_key_path(storage, http_cache.origin).read_bytes())
        smb_files = []
    else:
        source = safe_path(require_local_source(Path(source_root)), document["source_folder"])
        if destination.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(storage.games.resolve()):
            raise ValueError("The source game and installation folders must not overlap.")
        progress("prepare", 0, 0, "Reading source game folder")
        source_files, source_directories = inventory(source, cancel,
            lambda count, name: progress("prepare", count, max(entries, count), name))
        # One directory listing validates metadata without a remote stat/resolve for every file.
        for item in document["files"]:
            check_cancel(cancel)
            if item["path"] not in source_files or source_files[item["path"]][0] != item["size"]:
                raise ValueError("Source file missing or changed: " + item["path"])
        source_directories = set(source_directories)
        for name in document["directories"]:
            if name not in source_directories:
                raise ValueError("Source folder missing: " + name)
    resumable = stage is not None
    if resumable:
        stage = safe_path(storage.game_staging, Path(stage).name)
        payload = safe_path(stage, "payload")
        payload.mkdir(exist_ok=True)
        existing, existing_dirs = inventory(payload, cancel,
            lambda count, name: progress("prepare", count, max(entries, count), name))
        expected = {item["path"]: item for item in document["files"]}
        allowed_dirs = set(document["directories"])
        for name in expected:
            allowed_dirs.update(p.as_posix() for p in Path(name).parents if p.as_posix() != ".")
        if any(name not in expected for name in existing) or not set(existing_dirs) <= allowed_dirs:
            raise ValueError("Unrelated files were found in the partial copy.")
        if http_payload_ready and (set(existing) != set(expected) or any(
                existing[name][0] != item['size'] for name, item in expected.items())):
            raise ValueError("Written file sizes do not match the received HTTP packages.")
        present = sum(min(size[0], expected[name]["size"]) for name, size in existing.items())
    else:
        present = 0
    workspace = 0 if http_payload_ready else package_workspace(document)
    if shutil.disk_usage(storage.games).free < document["total_size"] - present + workspace + INSTALL_SPACE_RESERVE:
        raise OSError("Not enough space: the game, HTTP package and a 512 MiB reserve are required.")
    if not resumable:
        stage = Path(tempfile.mkdtemp(prefix=card["id"] + "-", dir=storage.game_staging))
    payload = stage / "payload"
    payload.mkdir(exist_ok=resumable)
    try:
        for name in document["directories"]:
            safe_path(payload, name).mkdir(parents=True, exist_ok=True)
        if "http" in document and not http_payload_ready:
            copy_packages(http_cache, document, payload, stage, cancel, progress)
        done = 0
        for item in smb_files:
            check_cancel(cancel)
            original = safe_path(source, item["path"])
            target = safe_path(payload, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            copied = 0
            if resumable and target.exists():
                copied = target.stat().st_size
                if copied <= item["size"]:
                    with target.open("rb") as prefix:
                        while chunk := prefix.read(BLOCK):
                            check_cancel(cancel)
                            digest.update(chunk)
                            progress("resume", prefix.tell(), copied, item["path"])
                if copied > item["size"] or (copied == item["size"] and digest.hexdigest() != item["sha256"]):
                    copied = 0
                    digest = hashlib.sha256()
                    with target.open("wb"):
                        pass
                if copied == item["size"]:
                    done += copied
                    progress("copy", done, document["total_size"], item["path"])
                    continue
            done += copied
            # shortcut: blocking SMB calls use Windows timeouts; add cancellable I/O if shutdown measurements require it.
            with original.open("rb") as incoming, target.open("r+b" if resumable and target.exists() else "xb") as output:
                incoming.seek(copied)
                output.seek(copied)
                try:
                    while True:
                        check_cancel(cancel)
                        chunk = incoming.read(min(BLOCK, item["size"] - copied + 1))
                        if not chunk:
                            break
                        if copied + len(chunk) > item["size"]:
                            raise ValueError("The source file grew during copying: " + item["path"])
                        output.write(chunk)
                        digest.update(chunk)
                        copied += len(chunk)
                        done += len(chunk)
                        progress("copy", done, document["total_size"], item["path"])
                finally:
                    output.flush()
                    os.fsync(output.fileno())
            if copied != item["size"] or digest.hexdigest() != item["sha256"]:
                if resumable:
                    with target.open("wb"):
                        pass
                raise ValueError("Source file does not match the manifest: " + item["path"])
        done = 0
        for item in (() if http_payload_ready else document["files"]):
            def advanced(count):
                nonlocal done
                done += count
                progress("verify", done, document["total_size"], item["path"])
            if hash_file(safe_path(payload, item["path"]), cancel, advanced) != item["sha256"]:
                raise ValueError("Local file verification failed: " + item["path"])
        executable = next(item for item in document["files"] if item["path"] == document["executable"])
        return commit_install(storage, card, document, payload, destination, executable['sha256'],
                              len(document['files']), hashlib.sha256(manifest_bytes(document)).hexdigest(), cancel)

    finally:
        if not resumable:
            remove_stage(storage, stage)



def commit_install(storage, card, document, payload, destination, executable_sha256, file_count, manifest_sha256, cancel):
    receipt = {"schema": 1, "managed": True, "card": card, "manifest_sha256": manifest_sha256,
               "executable": document["executable"], "executable_sha256": executable_sha256,
               "working_directory": document["working_directory"], "arguments": document["arguments"],
               "total_size": document["total_size"], "file_count": file_count, "installed_at": int(time.time()), "added_at": time.time_ns()}
    receipt["location"] = (destination.relative_to(storage.root).as_posix()
                           if destination.is_relative_to(storage.root) else str(destination))
    records = safe_path(storage.state, "installers" if card.get("kind") == "installer" else "installs")
    records.mkdir(exist_ok=True)
    pending = safe_path(records, card["id"] + ".pending.json")
    receipt["directory_identity"] = [payload.stat().st_dev, payload.stat().st_ino]
    atomic_write(pending, json.dumps(receipt, ensure_ascii=False).encode("utf8"))
    check_cancel(cancel)
    safe_path(storage.games, destination.name)
    if destination.exists():
        raise FileExistsError("The game folder appeared during copying; overwriting is not allowed.")
    payload.rename(destination)
    os.replace(pending, safe_path(records, card["id"] + ".json"))
    return receipt


def sort_installs(receipts, order="recent"):
    def key(receipt):
        card = receipt["card"]
        name = card["name"].casefold()
        installed = receipt.get("installed_at", 0)
        added = receipt.get("added_at", installed * 1_000_000_000 if type(installed) is int else 0)
        added = added if type(added) is int and added > 0 else 0
        played = receipt.get("played_at", 0)
        played = played if type(played) is int and played > 0 else 0
        if order == "name":
            return (name, card["id"])
        if order == "genre":
            genre = card.get("genre", "").strip().casefold()
            return (not bool(genre), genre, name, card["id"])
        return (-(added if order == "added" else max(added, played)), name, card["id"])
    return sorted(receipts, key=key)


class LocalLibrary:
    def __init__(self, storage, installers=False):
        self.storage = storage
        self.installers = installers
        self.records = safe_path(storage.state, "installers" if installers else "installs")
        self.records.mkdir(exist_ok=True)
        self.path = safe_path(storage.state, "installers.sqlite" if installers else "library.sqlite")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS installed (game_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS play_history (game_id TEXT PRIMARY KEY, played_at INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS launch_options (game_id TEXT PRIMARY KEY, arguments TEXT NOT NULL)")

    def launch_arguments(self, identity):
        from ngs.process import split_launch_arguments
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT arguments FROM launch_options WHERE game_id=?", (game_id(identity),)).fetchone()
        value = row[0] if row else ""
        split_launch_arguments(value)
        return value

    def set_launch_arguments(self, identity, value):
        from ngs.process import split_launch_arguments
        identity = game_id(identity)
        split_launch_arguments(value)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO launch_options VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET arguments=excluded.arguments",
                       (identity, value))

    def mark_played(self, identity):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO play_history VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET played_at=excluded.played_at",
                       (game_id(identity), time.time_ns()))

    def reconcile(self, sort_order="recent"):
        started = time.perf_counter()
        receipts = {}
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db_rows = db.execute("SELECT game_id, receipt FROM installed").fetchall()
            for gid, raw_receipt in db_rows:
                try:
                    gid = game_id(gid)
                    marker = safe_path(self.records, gid + ".json")
                    if marker.exists() or safe_path(self.records, gid + ".pending.json").exists():
                        continue
                    parsed = json.loads(raw_receipt)
                    if not isinstance(parsed, dict) or parsed.get("card", {}).get("id") != gid:
                        continue
                    if (parsed.get("card", {}).get("kind") == "installer") != self.installers:
                        continue
                    if parsed.get("kind") == "external":
                        from ngs.scanner import validate_external
                        validate_external(parsed)
                        atomic_write(marker, json.dumps(parsed, ensure_ascii=False).encode("utf8"))
                except Exception:
                    continue
        except Exception:
            pass
        for marker in sorted(self.records.glob("*.json"), key=lambda p: p.name.endswith(".pending.json")):
            try:
                safe_path(self.records, marker.name)
                if not marker.is_file() or marker.stat().st_size > 64 * 1024:
                    continue
                receipt = json.loads(marker.read_bytes())
                if not isinstance(receipt, dict):
                    continue
                if (receipt.get("card", {}).get("kind") == "installer") != self.installers:
                    continue
                if receipt.get("kind") == "external":
                    from ngs.scanner import validate_external, local_folder
                    validate_external(receipt)
                    if marker.name != receipt["card"]["id"] + ".json":
                        continue
                    receipt["available"] = False
                    try:
                        folder = local_folder(self.install_path(receipt))
                        receipt["available"] = (safe_path(folder, receipt["executable"]).is_file()
                                                and safe_path(folder, receipt["working_directory"]).is_dir())
                    except (ValueError, OSError):
                        pass
                    receipts[receipt["card"]["id"]] = receipt
                    continue
                card = validate_card(receipt["card"])
                folder = self.install_path(receipt)
                if receipt.get("schema") != 1 or (folder.name != card["id"] and not readable_folder(card, folder.name)):
                    continue
                if marker.name not in (card["id"] + ".json", card["id"] + ".pending.json"):
                    continue
                if receipt.get("removing"):
                    removing = self.removal_path(receipt)
                    if removing.is_dir() or folder.is_dir():
                        receipts[card["id"]] = receipt
                    continue
                if not folder.is_dir() or is_link(folder.lstat()):
                    continue
                if marker.name.endswith(".pending.json"):
                    info = folder.stat()
                    if receipt.get("directory_identity") != [info.st_dev, info.st_ino]:
                        continue
                for key in ("manifest_sha256", "executable_sha256"):
                    if not re.fullmatch(r"[0-9a-f]{64}", receipt[key]):
                        raise ValueError("Invalid installation digest")
                executable = safe_path(folder, receipt["executable"])
                if not executable.is_file():
                    continue
                safe_path(folder, receipt["working_directory"])
                if not isinstance(receipt["arguments"], list) or any(not isinstance(a, str) or "\0" in a for a in receipt["arguments"]):
                    continue
                if marker.name.endswith(".pending.json"):
                    os.replace(marker, safe_path(self.records, card["id"] + ".json"))
                receipts[card["id"]] = receipt
            except (ValueError, OSError, KeyError, TypeError):
                continue
        with closing(sqlite3.connect(self.path)) as db, db:
            # An unplugged external drive must not erase its saved installation records.
            db.executemany("INSERT INTO installed VALUES (?, ?) ON CONFLICT(game_id) DO UPDATE SET receipt=excluded.receipt "
                           "WHERE installed.receipt != excluded.receipt", [
                (r["card"]["id"], json.dumps(r, ensure_ascii=False)) for r in receipts.values()])
            history = dict(db.execute("SELECT game_id, played_at FROM play_history"))
        for identity, receipt in receipts.items():
            receipt["played_at"] = history.get(identity, 0)
        from ngs.diagnostics import event
        event('perf.library_reconcile', elapsed_ms=round((time.perf_counter()-started)*1000, 1), games=len(receipts), installers=self.installers)
        return sort_installs(receipts.values(), sort_order)

    def organize(self, cards=(), cancel=None, progress=lambda *args: None):
        cancel = cancel or threading.Event()
        cards = list(cards)
        changed, errors = 0, []
        with installation_lock(self.storage):
            rows = self.reconcile()
            for index, receipt in enumerate(rows, 1):
                check_cancel(cancel)
                if receipt.get("kind") == "external" or receipt.get("managed") is not True or receipt.get("removing"):
                    continue
                card = receipt["card"]
                progress("organize", index - 1, len(rows), card["name"])
                try:
                    original = self.install_path(receipt).resolve()
                    if (original == self.storage.root or original.is_relative_to(self.storage.root / "data")
                            or self.storage.root.is_relative_to(original)):
                        raise ValueError("Renaming NGS or its data folders is not allowed.")
                    require_local_source(original.parent)
                    card = validate_card(dict(card, name=catalog_game_name(card, cards)))
                    destination = (original if readable_folder(card, original.name)
                                   else game_destination(original.parent, card, original))
                    if str(destination) == str(original) and card == receipt["card"]:
                        continue
                    info = original.stat()
                    if receipt.get("directory_identity") != [info.st_dev, info.st_ino]:
                        raise ValueError("The folder was replaced; renaming stopped.")
                    updated = dict(receipt, card=card)
                    updated.pop("played_at", None)
                    updated["location"] = (destination.relative_to(self.storage.root).as_posix()
                                           if destination.is_relative_to(self.storage.root) else str(destination))
                    record = safe_path(self.records, card["id"] + ".json")
                    pending = safe_path(self.records, card["id"] + ".pending.json")
                    if pending.exists():
                        previous = json.loads(pending.read_bytes())
                        if previous.get("directory_identity") != updated["directory_identity"] or previous.get("card", {}).get("id") != card["id"]:
                            raise ValueError("This game has another unfinished installation.")
                    atomic_write(pending, json.dumps(updated, ensure_ascii=False).encode("utf8"))
                    # The existing pending receipt recovers a crash after rename and before record replacement.
                    if str(destination) != str(original):
                        original.rename(destination)
                    os.replace(pending, record)
                    changed += 1
                except (OSError, ValueError, KeyError, TypeError) as error:
                    errors.append(card["name"] + ": " + str(error))
            self.reconcile()
        return dict(changed=changed, errors=errors)

    def register_external(self, folder, executable, name, card=None, cancel=None, *, arguments=None, working_directory=None):
        from ngs.scanner import local_folder, check_pe, external_identity, validate_external
        if self.installers or (card and card.get("kind") == "installer"):
            raise ValueError("Confirm the installed game folder rather than the installer entry.")
        cancel = cancel or threading.Event()
        folder = local_folder(folder)
        executable = relative_game_path(executable)
        target = safe_path(folder, executable)
        if arguments is not None and (not isinstance(arguments, list) or len(arguments) > 64
                or any(not isinstance(arg, str) or len(arg) > 2048 or "\0" in arg for arg in arguments)):
            raise ValueError("Invalid launch arguments.")
        requested_cwd = None
        if working_directory is not None:
            requested_cwd = safe_path(folder, relative_game_path(working_directory, allow_empty=True))
            if not requested_cwd.is_dir():
                raise ValueError("The game working directory is unavailable.")
        if target.suffix.lower() != ".exe" or not target.is_file():
            raise ValueError("Confirm an existing executable inside the selected folder.")
        if not isinstance(name, str) or not name.strip() or len(name) > 160 or "\0" in name:
            raise ValueError("The title must contain 1 to 160 characters.")
        check_pe(target)
        before = target.stat()
        digest = hash_file(target, cancel)
        after = target.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("The executable changed during verification. Add the game again.")
        with installation_lock(self.storage):
            identity = external_identity(folder)
            existing_local = []
            installed = self.reconcile()
            # Import must never convert a managed copy into an external reference.
            for receipt in installed:
                try:
                    existing = self.install_path(receipt)
                    if not existing.is_dir():
                        continue
                except (ValueError, OSError):
                    continue
                if receipt.get("managed") is True:
                    if folder == existing or target.is_relative_to(existing):
                        raise ValueError("This game is already installed through NGS.")
                elif receipt.get("kind") == "external":
                    if target.is_relative_to(existing) or existing.is_relative_to(folder):
                        existing_local.append(receipt)
            exact = [r for r in existing_local if safe_path(self.install_path(r), r["executable"]) == target]
            matches = exact or existing_local
            previous_launch = exact[0] if len(exact) == 1 else None
            if len(matches) > 1:
                raise ValueError("The selected folder contains multiple games. Choose a single game's folder.")
            if matches:
                receipt = matches[0]
                folder = self.install_path(receipt)
                if not target.is_relative_to(folder):
                    raise ValueError("A game inside this folder is already in the library. Choose its folder or another individual game.")
                executable = target.relative_to(folder).as_posix()
                identity = receipt["card"]["id"]
            if card:
                for receipt in installed:
                    if (receipt["card"]["id"] != identity and card["id"] in
                            (receipt["card"]["id"], receipt.get("catalog_id"))):
                        raise ValueError("This game is already in the library: " + receipt.get("location", receipt["card"]["name"]))
            metadata = (dict(validate_card(card), id=identity, name=name.strip()) if card else
                        {"id": identity, "name": name.strip(), "subtitle": "Game on this device",
                         "description": "Added from a local folder. Game files remain where they are.",
                         "genre": "Local game", "accent": "#d9ff82"})
            # Store-supplied launch parameters survive explicit EXE reconfirmation;
            # user-added arguments remain separately stored in launch_options.
            base_arguments = list(arguments) if arguments is not None else list(previous_launch["arguments"]) if previous_launch else []
            if requested_cwd is not None:
                cwd = requested_cwd.relative_to(folder).as_posix()
            elif previous_launch:
                cwd = previous_launch["working_directory"]
            else:
                cwd = Path(executable).parent.as_posix()
            receipt = {"schema": 1, "kind": "external", "managed": False, "card": metadata,
                       "metadata_name": card["name"] if card else "",
                       "catalog_id": card["id"] if card else "", "executable": executable,
                       "executable_sha256": digest, "working_directory": cwd,
                       "arguments": base_arguments, "installed_at": int(time.time()), "added_at": time.time_ns(),
                       "location": folder.relative_to(self.storage.root).as_posix()
                       if folder.is_relative_to(self.storage.root) else str(folder)}
            if receipt["working_directory"] == ".":
                receipt["working_directory"] = ""
            validate_external(receipt)
            check_cancel(cancel)
            # Only NGS state is written; the selected folder remains read-only.
            atomic_write(safe_path(self.records, identity + ".json"), json.dumps(receipt, ensure_ascii=False).encode("utf8"))
            from ngs.diagnostics import event
            event('library.add_external', game_id=identity, name=name, folder=str(folder), executable=executable)
            return receipt

    def update_external_metadata(self, identity, card):
        from ngs.scanner import validate_external
        card = validate_card(card)
        identity = game_id(identity)
        with installation_lock(self.storage):
            record = safe_path(self.records, identity + ".json")
            receipt = validate_external(json.loads(record.read_bytes()))
            if receipt["card"]["id"] != identity:
                raise ValueError("Invalid external identity")
            name = receipt["card"]["name"]
            if ((not receipt.get("catalog_id") and name != self.install_path(receipt).name)
                    or (receipt.get("metadata_name") and name != receipt["metadata_name"])):
                card = dict(card, name=name)  # Keep an explicitly named local game.
            else:
                receipt["metadata_name"] = card["name"]
            receipt["catalog_id"] = card["id"]
            receipt["card"] = dict(card, id=identity)
            validate_external(receipt)
            atomic_write(record, json.dumps(receipt, ensure_ascii=False).encode("utf8"))
            return receipt

    def forget_external(self, identity):
        return self.forget(identity, external_only=True)

    def forget(self, identity, external_only=False):
        from ngs.diagnostics import event
        event('library.forget_requested', game_id=identity, external_only=external_only)
        from ngs.scanner import validate_external
        identity = game_id(identity)
        with installation_lock(self.storage):
            record = safe_path(self.records, identity + ".json")
            receipt = json.loads(record.read_bytes())
            if external_only or receipt.get("kind") == "external":
                validate_external(receipt)
            elif receipt.get("schema") != 1 or receipt.get("managed") is not True:
                raise ValueError("Invalid installation record.")
            else:
                validate_card(receipt["card"])
            if receipt["card"]["id"] != identity:
                raise ValueError("Invalid installation identity")
            if receipt.get("removing") or safe_path(self.records, identity + ".pending.json").exists():
                raise ValueError("Finish installing or removing this game first.")
            record.unlink()
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("DELETE FROM installed WHERE game_id=?", (identity,))
            return receipt

    def install_path(self, receipt):
        location = receipt.get("location", "games/" + game_id(receipt["card"]["id"]))
        path = Path(location)
        if path.is_absolute():
            return safe_path(path.parent, path.name)
        return safe_path(self.storage.root, relative_game_path(location))

    def removal_path(self, receipt):
        original = self.install_path(receipt)
        name = receipt["removing"]["name"]
        if not re.fullmatch(r"\.ngs-removing-" + re.escape(game_id(receipt["card"]["id"])) + r"-[0-9a-f]{32}", name):
            raise ValueError("Invalid removal folder.")
        return safe_path(original.parent, name)

    def uninstall(self, identity, cancel=None, progress=lambda *args: None):
        identity = game_id(identity)
        cancel = cancel or threading.Event()
        with installation_lock(self.storage):
            record = safe_path(self.records, identity + ".json")
            receipt = json.loads(record.read_bytes())
            if receipt.get("kind") == "external" or receipt.get("managed") is not True or receipt["card"]["id"] != identity:
                raise ValueError("Only game copies installed through NGS can be deleted.")
            original = self.install_path(receipt)
            if ((original.name != identity and not readable_folder(receipt["card"], original.name))
                    or original == self.storage.root or self.storage.root.is_relative_to(original)):
                raise ValueError("Refusing to delete an unrelated folder.")
            check_cancel(cancel)
            if not receipt.get("removing"):
                inventory(original, cancel)
                info = original.stat()
                receipt["removing"] = {"name": ".ngs-removing-" + identity + "-" + uuid.uuid4().hex,
                                      "directory_identity": [info.st_dev, info.st_ino]}
                atomic_write(record, json.dumps(receipt, ensure_ascii=False).encode("utf8"))
            target = self.removal_path(receipt)
            if not target.exists() and original.exists():
                info = original.stat()
                if receipt["removing"]["directory_identity"] != [info.st_dev, info.st_ino]:
                    raise ValueError("The game folder was replaced. Removal stopped.")
                original.rename(target)
            if target.exists():
                info = target.stat()
                if receipt["removing"]["directory_identity"] != [info.st_dev, info.st_ino]:
                    raise ValueError("The removal folder was replaced.")
                files, directories = inventory(target, cancel)
                total = max(1, len(files))
                for index, name in enumerate(files, 1):
                    check_cancel(cancel)
                    path = safe_path(target, name)
                    try:
                        path.unlink()
                    except PermissionError:
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)
                        path.unlink()
                    progress("remove", index, total, name)
                for name in sorted(directories, key=lambda p: p.count("/"), reverse=True):
                    check_cancel(cancel)
                    safe_path(target, name).rmdir()
                if not target.resolve().is_relative_to(original.parent.resolve()) or target.parent != original.parent:
                    raise ValueError("Refusing to delete an unrelated directory.")
                target.rmdir()
            record.unlink()
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("DELETE FROM installed WHERE game_id=?", (identity,))
            return receipt

    def launch_spec(self, identity, cancel=None):
        identity = game_id(identity)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT receipt FROM installed WHERE game_id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("The game is not installed yet.")
        receipt = json.loads(row[0])
        if (receipt.get("card", {}).get("kind") == "installer") != self.installers:
            raise ValueError("This entry belongs to another NGS section.")
        if receipt.get("kind") == "external":
            from ngs.scanner import validate_external, local_folder
            record = safe_path(self.records, identity + ".json")
            if record.stat().st_size > 64 * 1024:
                raise ValueError("Corrupt local game record.")
            receipt = json.loads(record.read_bytes())
            validate_external(receipt)
            if receipt["card"]["id"] != identity:
                raise ValueError("Invalid external identity")
            local_folder(self.install_path(receipt))
        if receipt.get("removing"):
            raise ValueError("Game removal is incomplete. Retry removal.")
        root = self.install_path(receipt)
        executable = safe_path(root, receipt["executable"])
        cwd = safe_path(root, receipt["working_directory"])
        if not executable.is_file() or not cwd.is_dir() or executable.suffix.lower() != ".exe":
            raise ValueError("The executable or working directory is unavailable.")
        if hash_file(executable, cancel or threading.Event()) != receipt["executable_sha256"]:
            raise ValueError("The executable changed. Confirm it by adding the folder again." if receipt.get("kind") == "external"
                             else "The executable changed after installation. Launch stopped.")
        from ngs.process import split_launch_arguments
        return executable, cwd, receipt["arguments"] + split_launch_arguments(self.launch_arguments(identity))
