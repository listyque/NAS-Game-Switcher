"""Read-only, bounded discovery of existing local Windows games."""

import hashlib
import ctypes
import os
import re
import string
import struct
import threading
from pathlib import Path

from ngs.catalog import relative_game_path, validate_card
from ngs.install import check_cancel, game_id, is_link, safe_path


MAX_ENTRIES = 100_000
MAX_EXES = 300
MAX_DEPTH = 10
UTILITY = re.compile(
    r"setup|install|unins|redist|crash|report|benchmark|diagnostic|update|"
    r"crs-(?:handler|uploader|video)|unitycrashhandler|bsndrpt|bugsplat|prereq|"
    r"dxsetup|vcredist|vc_redist|easyanticheat|eaclaunch|start_protected_game|"
    r"activationui|cleanup|touchup|cefsharp|unrealcefsubprocess|epicwebhelper|"
    r"msedgewebview2|notification_helper|dgvoodoocpl|integritychecker|gamingrepair|"
    r"nvremixbridge|codinstallcleaner|blizzardbrowser|psoconsolidator|patchminidump|"
    r"language\s*selector|localization|^7za\.exe$|^(?:_crack|crack|_original\s*files|_commonredist|"
    r"__installer|support|tools|redist|sdk|artbookost|advguide|mod\s+plugy|webview2)/|/(?:_crack|crack|_original\s*files|_commonredist|"
    r"__installer|support|tools|redist|sdk|artbookost|advguide|mod\s+plugy|webview2)/",
    re.I,
)


def local_folder(value):
    path = Path(value)
    if not path.is_absolute() or str(path).startswith("\\\\") or ".." in path.parts:
        raise ValueError("Choose an absolute path on a local drive.")
    # Walk from the drive root: resolving first would hide a junction in an ancestor.
    root = Path(path.anchor)
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(str(root)) not in (2, 3, 6):
        raise ValueError("Choose a folder on a local drive rather than a network share.")
    relative = path.relative_to(root).as_posix()
    folder = safe_path(root, "" if relative == "." else relative)
    if not folder.is_dir():
        raise ValueError("The folder is unavailable. Connect the drive or choose another folder.")
    return folder


def browse_folders(value, cancel=None, allow_network=False):
    cancel = cancel or threading.Event()
    if not value:
        drives = [Path(letter + ":/") for letter in string.ascii_uppercase]
        return {"path": "", "parent": "", "folders": [{"name": str(p), "path": str(p)} for p in drives
                if ctypes.windll.kernel32.GetDriveTypeW(str(p)) in (2, 3, 6) and p.is_dir()]}
    if allow_network:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("An absolute path is required to select a folder.")
        relative = path.relative_to(path.anchor).as_posix()
        folder = safe_path(Path(path.anchor), "" if relative == "." else relative)
    else:
        folder = local_folder(value)
    folders = []
    with os.scandir(folder) as entries:
        for index, entry in enumerate(entries):
            check_cancel(cancel)
            if index >= MAX_ENTRIES:
                raise ValueError("The folder is too large. Choose a more specific path.")
            try:
                if entry.is_dir(follow_symlinks=False) and not is_link(entry.stat(follow_symlinks=False)):
                    folders.append({"name": entry.name, "path": entry.path})
            except OSError:
                continue
    return {"path": str(folder), "parent": str(folder.parent) if folder.parent != folder else "",
            "folders": sorted(folders, key=lambda f: f["name"].casefold())}


def scan_executables(value, cancel=None, allow_network=False):
    cancel = cancel or threading.Event()
    def checked_folder(value):
        if not allow_network:
            return local_folder(value)
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("NAS folders require an absolute path")
        return safe_path(Path(path.anchor), path.relative_to(path.anchor).as_posix())
    root = checked_folder(value)
    if root == Path(root.anchor):
        raise ValueError("Choose a single game's folder rather than an entire drive.")
    pending, candidates, count, skipped, limited = [(root, 0)], [], 0, 0, False
    while pending:
        folder, depth = pending.pop()
        check_cancel(cancel)
        try:
            # Recheck each directory before opening it; junctions are never followed.
            checked_folder(folder)
            with os.scandir(folder) as entries:
                for entry in entries:
                    check_cancel(cancel)
                    count += 1
                    if count > MAX_ENTRIES or len(candidates) >= MAX_EXES:
                        limited = True
                        pending.clear()
                        break
                    try:
                        # POSIX DirEntry already knows symlinks/types; only EXE sizes need stat.
                        info = entry.stat(follow_symlinks=False) if os.name == "nt" or entry.name.lower().endswith(".exe") else None
                        if entry.is_symlink() or (info is not None and is_link(info)):
                            skipped += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH:
                                pending.append((Path(entry.path), depth + 1))
                            else:
                                limited = True
                        elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".exe"):
                            relative = relative_game_path(Path(entry.path).relative_to(root).as_posix())
                            candidates.append({"path": relative, "utility": bool(UTILITY.search(relative) or UTILITY.search(entry.name)), "size": info.st_size})
                    except (OSError, ValueError):
                        skipped += 1
        except OSError:
            skipped += 1
    candidates.sort(key=lambda e: (e["utility"], e["path"].count("/"), e["path"].casefold()))
    return {"path": str(root), "candidates": candidates, "limited": limited, "skipped": skipped}


def normalized_name(value):
    return "".join(c for c in value.casefold() if c.isalnum())


def matching_cards(folder, executable, cards):
    names = {normalized_name(Path(folder).name), normalized_name(Path(executable).stem)}
    names -= {"", "game", "launcher", "start", "play", "win64", "win32"}
    return [card for card in cards if names & {normalized_name(card["name"]),
            normalized_name(Path(card.get("source_folder", "")).name)}]


def validate_external(receipt):
    if not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("kind") != "external" or receipt.get("managed") is not False:
        raise ValueError("Invalid external game record")
    card = receipt["card"]
    if not isinstance(card, dict):
        raise ValueError("Invalid local game card")
    game_id(card["id"])
    if not card["id"].startswith("local-"):
        raise ValueError("Invalid external game identity")
    for key, limit in (("name", 160), ("subtitle", 200), ("description", 3000), ("genre", 100)):
        if not isinstance(card[key], str) or len(card[key]) > limit or "\0" in card[key]:
            raise ValueError("Invalid local game description")
    if not card["name"].strip() or not re.fullmatch(r"#[0-9a-fA-F]{6}", card["accent"]):
        raise ValueError("Invalid local game card")
    if receipt.get("catalog_id"):
        game_id(receipt["catalog_id"])
    if 'metadata_manual' in receipt and type(receipt['metadata_manual']) is not bool:
        raise ValueError('Invalid artwork selection flag')
    if receipt.get("catalog_id") or any(key in card for key in ("cover", "background", "manifest")):
        validate_card(card)
    location = receipt["location"]
    if not isinstance(location, str) or not location or "\0" in location or ".." in Path(location).parts or location.startswith("\\\\"):
        raise ValueError("Invalid external game location")
    if not Path(location).is_absolute():
        relative_game_path(location)
    if not relative_game_path(receipt["executable"]).lower().endswith(".exe"):
        raise ValueError("Select an EXE")
    relative_game_path(receipt["working_directory"], allow_empty=True)
    arguments = receipt["arguments"]
    if (not isinstance(arguments, list) or len(arguments) > 64
            or any(not isinstance(arg, str) or len(arg) > 2048 or "\0" in arg for arg in arguments)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["executable_sha256"])):
        raise ValueError("Invalid external launch specification")
    return receipt


def check_pe(executable):
    with executable.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("The file is not a Windows executable.")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset > executable.stat().st_size - 4:
            raise ValueError("Corrupt executable.")
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("The file is not a Windows executable.")


def external_identity(folder):
    return "local-" + hashlib.sha256(os.path.normcase(str(folder)).encode("utf8")).hexdigest()[:24]
