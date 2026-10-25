"""Signed portable releases: HTTP metadata/package, local staged replacement."""

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath

from ngs.catalog import CatalogCache, atomic_write
from ngs.install import check_cancel, safe_path, hash_file
from ngs.packages import download_chunk
from ngs.update_crypto import RsaKey


FEED_PATH = "/assets/ngs_launcher/updates.json"
RELEASE_PREFIX = "/assets/ngs_launcher/releases/"
VERSION_PATTERN = r"20[0-9]{2}\.[01][0-9]\.[0-3][0-9]\.[0-2][0-9][0-5][0-9][0-5][0-9]"


def current_version(storage):
    path = storage.resources / "release.json"
    if not path.is_file():
        return "0.0.0.0"
    version = json.loads(path.read_text(encoding="utf8"))["version"]
    if not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError("Invalid installed release version")
    return version


def version_key(value):
    return tuple(map(int, value.split(".")))


def validate_release(release):
    if (not isinstance(release, dict) or set(release) !=
            {"schema", "version", "platform", "filename", "sha256", "size", "unpacked_size", "notes"}
            or type(release["schema"]) is not int or release["schema"] != 1 or release["platform"] != "windows-x64"
            or not isinstance(release["version"], str) or not re.fullmatch(VERSION_PATTERN, release["version"])
            or not isinstance(release["filename"], str)
            or not re.fullmatch(r"NGS-windows-x64-[0-9]{8}-[0-9]{6}\.zip", release["filename"])
            or not isinstance(release["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", release["sha256"])
            or any(type(release[k]) is not int or not 0 < release[k] <= 1024**3 for k in ("size", "unpacked_size"))
            or not isinstance(release["notes"], str) or len(release["notes"]) > 4000):
        raise ValueError("Invalid update description.")
    return release


def verify_envelope(envelope, public_key):
    if (not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}
            or not isinstance(envelope["payload"], str) or len(envelope["payload"].encode("utf8")) > 16000
            or not isinstance(envelope["signature"], str) or len(envelope["signature"]) > 400):
        raise ValueError("Invalid update signature format.")
    with RsaKey(public_key) as key:
        key.verify(envelope["payload"].encode("utf8"), base64.b64decode(envelope["signature"], validate=True))
    return validate_release(json.loads(envelope["payload"]))


def release_path(release):
    return RELEASE_PREFIX + validate_release(release)["sha256"] + ".zip"


def check_update(storage, base_url, cancel):
    from ngs.github_updates import OFFICIAL_UPDATE_URL, GitHubReleases
    if base_url == OFFICIAL_UPDATE_URL:
        envelope = GitHubReleases(cancel).manifest()
        if envelope is None:
            return None
    else:
        cache = CatalogCache(storage, base_url)
        envelope = json.loads(cache.fetch(FEED_PATH, 20000, cancel, time.monotonic() + 15))
    release = verify_envelope(envelope, (storage.resources / "assets/update-public-key.bin").read_bytes())
    return envelope if version_key(release["version"]) > version_key(current_version(storage)) else None


def clean_directory(storage, relative):
    if relative not in {"data/updates/" + name for name in ("prepared", "backup", "failed-runtime", "probe")}:
        raise ValueError("Only updater-owned directories can be cleaned")
    path = safe_path(storage.root, relative)
    if path.exists():
        for parent, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                safe_path(path, (Path(parent) / name).relative_to(path).as_posix())
        shutil.rmtree(path)


def archive_entries(archive, release):
    prefix = release["filename"][:-4]
    result, seen, total = [], set(), 0
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        if (item.orig_filename != item.filename or "\\" in item.filename or ":" in item.filename or path.is_absolute() or ".." in path.parts
                or not path.parts or path.parts[0] != prefix or item.flag_bits & 1
                or stat.S_ISLNK(item.external_attr >> 16)):
            raise ValueError("Unsafe path in update package.")
        parts = path.parts[1:]
        if not parts and item.is_dir():
            continue
        if (not parts or (parts[0] != "runtime" and parts != ("NGS.exe",))
                or any(p.rstrip(" .") != p or p.casefold().split(".")[0] in
                       {"con", "prn", "aux", "nul", *["com%d" % i for i in range(1, 10)], *["lpt%d" % i for i in range(1, 10)]}
                       for p in parts)
                or any(p.casefold() in ("data", "api-device.json") or p.startswith(".env") for p in parts)):
            raise ValueError("The update contains files outside NGS.exe/runtime.")
        relative = "/".join(parts)
        if relative.casefold() in seen:
            raise ValueError("Duplicate path in update package.")
        seen.add(relative.casefold())
        total += item.file_size
        result.append((item, relative))
    if (len(result) > 20000 or total != release["unpacked_size"] or not
            {"ngs.exe", "runtime/ngs.exe", "runtime/release.json", "runtime/update.ps1"} <= seen):
        raise ValueError("Incomplete update package or incorrect size.")
    return result


def powershell():
    return Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def prepare_update(storage, envelope, cancel, progress=lambda *args: None, base_url=None):
    if storage.resources != storage.root / "runtime" or current_version(storage) == "0.0.0.0":
        raise ValueError("Installing updates is available in the packaged Windows build.")
    public = (storage.resources / "assets/update-public-key.bin").read_bytes()
    release = verify_envelope(envelope, public)
    if version_key(release["version"]) <= version_key(current_version(storage)):
        raise ValueError("This version is already installed.")
    from ngs.github_updates import OFFICIAL_UPDATE_URL, GitHubReleases
    selected_source = base_url or storage.read_settings().get('update_url') or storage.read_settings()['tactic_url']
    cache = GitHubReleases(cancel) if selected_source == OFFICIAL_UPDATE_URL else CatalogCache(storage, selected_source)
    base = safe_path(storage.root, "data/updates")
    base.mkdir(exist_ok=True)
    owner = safe_path(base, "owner.json")
    marker = {"schema": 1, "application": "NAS Game Switcher updater"}
    if owner.exists():
        if json.loads(owner.read_text(encoding="utf8")) != marker:
            raise ValueError("The update folder does not belong to NGS.")
    elif any(base.iterdir()):
        raise ValueError("Unrelated files found in data/updates. They are preserved.")
    else:
        atomic_write(owner, json.dumps(marker).encode("utf8"))
    if (base / "transaction.json").exists():
        raise ValueError("The previous update requires recovery. Restart NGS.")
    if safe_path(base, "first-start.json").exists():
        raise ValueError("The previous update's first launch is not confirmed yet. Wait for NGS to finish starting.")
    if shutil.disk_usage(storage.root).free < release["size"] + release["unpacked_size"] + 512 * 1024**2:
        raise OSError("Not enough space for the update package and extraction.")
    for name in ("prepared", "backup", "failed-runtime", "probe"):
        clean_directory(storage, "data/updates/" + name)
    package = safe_path(base, "package.zip")
    partial = safe_path(base, release["sha256"] + ".part")
    try:
        if not (package.is_file() and package.stat().st_size == release["size"]
                and hash_file(package, cancel) == release["sha256"]):
            download_progress = 0
            def advanced(phase, done, total, name):
                nonlocal download_progress
                if phase == 'copy': download_progress = done / max(1, total) * .8
                progress(download_progress, name if phase == 'waiting' else
                         "Downloading update…" if phase == "copy" else "Checking partial update download…")
            path = cache.package_path(release) if isinstance(cache, GitHubReleases) else release_path(release)
            download_chunk(cache, dict(path=path, size=release["size"], sha256=release["sha256"]),
                           partial, cancel, advanced)
            os.replace(partial, package)
        prepared = safe_path(base, "prepared")
        prepared.mkdir()
        with zipfile.ZipFile(package) as archive:
            entries = archive_entries(archive, release)
            for index, (item, relative) in enumerate(entries):
                check_cancel(cancel)
                target = safe_path(prepared, relative)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as incoming, target.open("wb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, 1024**2)
                progress(.8 + .2 * (index + 1) / len(entries), "Verifying and extracting update…")
        if json.loads((prepared / "runtime/release.json").read_text(encoding="utf8"))["version"] != release["version"]:
            raise ValueError("The package version does not match the published version.")
        if (prepared / "runtime/assets/update-public-key.bin").read_bytes() != public:
            raise ValueError("The update signing key changed unexpectedly.")
        for name in ("NGS.exe", "runtime/NGS.exe"):
            with (prepared / name).open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise ValueError("The package contains no Windows executable.")
        check_cancel(cancel)
        (storage.cache / "powershell").mkdir(exist_ok=True)
        shutil.copyfile(storage.resources / "update.ps1", safe_path(base, "apply.ps1"))
        atomic_write(safe_path(base, "pending.json"), json.dumps(envelope, ensure_ascii=False).encode("utf8"))
        return release
    except BaseException:
        clean_directory(storage, "data/updates/prepared")
        raise


def start_installer(storage):
    script = safe_path(storage.root, "data/updates/apply.ps1")
    if not script.is_file():
        raise ValueError("The update is not prepared.")
    env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    env["PSModuleAnalysisCachePath"] = str(storage.cache / "powershell/ModuleAnalysisCache")
    installer = subprocess.Popen([str(powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                      "-WindowStyle", "Hidden", "-File", str(script), "-Root", str(storage.root),
                      "-WaitPid", str(os.getpid()), "-ParentPid", str(os.getppid())],
                     cwd=storage.root, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    # Pass activation permission through the updater to the replacement launcher.
    import ctypes
    from ctypes import wintypes
    allow = ctypes.WinDLL("user32").AllowSetForegroundWindow
    allow.argtypes, allow.restype = [wintypes.DWORD], wintypes.BOOL
    allow(installer.pid)
