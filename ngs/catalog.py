"""Published TACTIC feed and a portable, last-good local cache."""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit, urlunsplit, unquote

from ngs.tactic import _NoRedirect


MAX_FEED = 2 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024


def relative_game_path(value, allow_empty=False):
    if allow_empty and value == "":
        return value
    if (not isinstance(value, str) or not value or len(value) > 512
            or re.search(r'[\x00-\x1f\\:<>"|?*]', value) or value.startswith("/")
            or any(part in ("", ".", "..") or part.endswith((" ", "."))
                   or re.fullmatch(r"(?i:con|conin\$|conout\$|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part)
                   for part in value.split("/"))):
        raise ValueError("Invalid relative game path.")
    return value


def validate_card(card):
    if not isinstance(card, dict):
        raise ValueError("Expected a game card.")
    if "kind" in card and card["kind"] != "installer":
        raise ValueError("Invalid catalog kind.")
    if "game_id" in card and (card.get("kind") != "installer" or not isinstance(card["game_id"], str)
                             or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", card["game_id"])):
        raise ValueError("Invalid installer game identity.")
    limits = {"id": 80, "name": 160, "subtitle": 200, "description": 3000,
              "genre": 100, "source_folder": 512, "executable": 512, "metadata_url": 500}
    for key, limit in limits.items():
        value = card.get(key)
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c != "\n" for c in value):
            raise ValueError("Invalid card field: " + key)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", card["id"]) or not card["name"].strip():
        raise ValueError("Invalid game identity.")
    if not isinstance(card.get("accent"), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", card["accent"]):
        raise ValueError("Invalid accent color.")
    available = card.get("available_on_nas", True)
    root = card.get("source_root", "")
    if (not isinstance(root, str) or len(root) > 512 or any(ord(c) < 32 for c in root)
            or (root and (not PureWindowsPath(root).is_absolute() or ".." in PureWindowsPath(root).parts))
            or (root and not available)):
        raise ValueError("Invalid NAS source root")
    if type(available) is not bool or (not available and (card["source_folder"] or card["executable"] or "manifest" in card)):
        raise ValueError("Metadata-only games cannot declare NAS files")
    if "delivery" in card and (card["delivery"] != "on_request" or not available or not card["executable"]):
        raise ValueError("Invalid game delivery")
    if "total_size" in card and (type(card["total_size"]) is not int or not 0 <= card["total_size"] <= 2**50 or not available):
        raise ValueError("Invalid game size")
    relative_game_path(card["source_folder"], allow_empty=not available)
    relative_game_path(card["executable"], allow_empty=True)
    if type(card.get("revision")) is not int or card["revision"] < 1:
        raise ValueError("Invalid card revision.")
    for key, limit in (("cover", MAX_IMAGE), ("background", MAX_IMAGE), ("manifest", 32 * 1024 * 1024)):
        if key == "manifest" and key not in card:
            continue
        image = card.get(key, {})
        if (not isinstance(image, dict) or not isinstance(image.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", image["sha256"])
                or type(image.get("size")) is not int or not 0 < image["size"] <= limit):
            raise ValueError("Invalid image reference.")
        asset_path(image.get("path", ""))
    return {key: card[key] for key in (*limits, "accent", "revision", "cover", "background", "manifest", "available_on_nas", "source_root", "delivery", "total_size", "kind", "game_id") if key in card}


def asset_path(path):
    if (not isinstance(path, str) or not path.startswith("/assets/ngs_launcher/")
            or urlsplit(path).query or urlsplit(path).fragment or "%" in path
            or unquote(path) != path or "\\" in path or any(ord(c) < 33 for c in path)
            or any(p in ("", ".", "..") for p in path.split("/")[1:])):
        raise ValueError("Image must belong to the NGS asset directory.")
    return path


def validate_feed(feed):
    if (not isinstance(feed, dict) or feed.get("schema") != 1
            or feed.get("project") != "ngs_launcher" or not isinstance(feed.get("games"), list)
            or len(feed["games"]) > 1000):
        raise ValueError("Unsupported catalog format.")
    cards = [validate_card(card) for card in feed["games"]]
    if len({card["id"] for card in cards}) != len(cards):
        raise ValueError("Duplicate game identity.")
    return {"schema": 1, "project": "ngs_launcher", "games": cards}


def atomic_write(path, data):
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ngs-", delete=False) as stream:
            name = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


class CatalogCache:
    def __init__(self, storage, base_url, installers=False):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Invalid catalog server URL.")
        self.origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self.installers = installers
        self.placeholder = storage.resources / "assets/local.svg"
        self.folder = storage.cache / "catalog"
        if not self.folder.resolve().is_relative_to(storage.root):
            raise ValueError("Catalog cache must stay inside NGS.")
        self.folder.mkdir(exist_ok=True)
        # A different server must never inherit another server's offline catalog.
        key = hashlib.sha256(self.origin.encode()).hexdigest()[:20]
        self.index = self.folder / (key + ("-installers" if installers else "") + ".json")
        from ngs.asset_auth import AssetAuthorization
        self.opener = urllib.request.build_opener(_NoRedirect, AssetAuthorization(storage, self.origin))

    def image_path(self, digest):
        path = self.folder / (digest + ".image")
        if not path.resolve().is_relative_to(self.folder.resolve()):
            raise ValueError("Redirected image cache.")
        return path

    def image_valid(self, image, verify_digest=True):
        path = self.image_path(image["sha256"])
        if not path.is_file(): return False
        info = path.stat()
        if info.st_size != image["size"]: return False
        if not verify_digest: return True
        started = time.perf_counter()
        with path.open('rb') as stream:
            valid = hashlib.file_digest(stream, 'sha256').hexdigest() == image['sha256']
        elapsed = time.perf_counter() - started
        if elapsed >= .03:
            from ngs.diagnostics import event
            event('perf.image_hash', elapsed_ms=round(elapsed*1000, 1), bytes=info.st_size)
        return valid

    def load(self):
        if not self.index.exists():
            return []
        if self.index.stat().st_size > MAX_FEED:
            raise ValueError("Oversized catalog cache.")
        feed = validate_feed(json.loads(self.index.read_bytes()))
        return self.present(feed)

    def present(self, feed):
        if any((card.get("kind") == "installer") != self.installers for card in feed["games"]):
            raise ValueError("Catalog contains a different kind of content.")
        # A missing picture must not hide cached descriptions or block offline games.
        def artwork(image):
            return (self.image_path(image["sha256"]) if self.image_valid(image, verify_digest=False) else self.placeholder).as_uri()
        return [dict(card, image=artwork(card["cover"]), backgroundImage=artwork(card["background"]))
                for card in feed["games"]]

    def fetch(self, path, limit, cancel, deadline, *, missing_ok=False):
        if cancel.is_set() or time.monotonic() > deadline:
            raise TimeoutError("Catalog update cancelled or timed out.")
        from ngs.http_retry import retry_network
        return retry_network(lambda: self._fetch(path, limit, cancel, deadline, missing_ok=missing_ok),
                             cancel, deadline=deadline)

    def _fetch(self, path, limit, cancel, deadline, *, missing_ok=False):
        request = urllib.request.Request(self.origin + path, headers={"Accept": "application/json,image/*"})
        chunks, size = [], 0
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if missing_ok and status == 404: return None
            from ngs.http_retry import TRANSIENT_HTTP
            if status in TRANSIENT_HTTP:
                raise
            raise RuntimeError("Published catalog HTTP error %s." % status) from None
        with response:
            expected = getattr(response, 'headers', {}).get('Content-Length')
            while True:
                if cancel.is_set() or time.monotonic() > deadline:
                    raise TimeoutError("Catalog update cancelled or timed out.")
                chunk = response.read(64 * 1024)
                if not chunk:
                    if expected is not None and size != int(expected):
                        raise ConnectionError("The download description is incomplete.")
                    return b"".join(chunks)
                size += len(chunk)
                if size > limit:
                    raise ValueError("Catalog download exceeds its size limit.")
                chunks.append(chunk)

    def cache_artwork(self, card, cancel=None, deadline=None, progress=None):
        cancel = cancel or threading.Event()
        deadline = deadline or time.monotonic() + 45
        for index, key in enumerate(("cover", "background")):
            image = card[key]
            if not self.image_valid(image):
                if progress:
                    progress("metadata_artwork", index, 2, "Loading cover from NAS…" if key == "cover" else "Loading background from NAS…")
                data = self.fetch(image["path"], MAX_IMAGE, cancel, deadline)
                self.cache_image(image, data)
            if progress:
                progress("metadata_artwork", index + 1, 2, "Cover saved. Loading background…" if key == "cover" else "Images saved.")

    def cache_image(self, image, data):
        if len(data) != image["size"] or hashlib.sha256(data).hexdigest() != image["sha256"]:
            raise ValueError("Catalog image checksum mismatch.")
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QImageReader
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        if (reader.format() not in (b"png", b"jpeg", b"webp") or not size.isValid()
                or max(size.width(), size.height()) > 8192 or size.width() * size.height() > 16_000_000
                or reader.read().isNull()):
            raise ValueError("Invalid catalog image.")
        atomic_write(self.image_path(image["sha256"]), data)


    def refresh(self, cancel=None):
        cancel = cancel or threading.Event()
        started = time.perf_counter()
        cpu = time.thread_time()
        deadline = time.monotonic() + 45
        name = "installers" if self.installers else "catalog"
        feed = validate_feed(json.loads(self.fetch("/assets/ngs_launcher/" + name + ".json", MAX_FEED, cancel, deadline)))
        self.present(feed)
        try:
            for card in feed["games"]:
                self.cache_artwork(card, cancel, deadline)
        except Exception as error:
            from ngs.http_retry import transient_network_error
            # Keep the first validated feed even if the first artwork download loses its connection.
            if not self.index.exists() and not cancel.is_set() and transient_network_error(error):
                atomic_write(self.index, json.dumps(feed, ensure_ascii=False).encode("utf-8"))
            raise
        if cancel.is_set():
            raise TimeoutError("Catalog update cancelled.")
        atomic_write(self.index, json.dumps(feed, ensure_ascii=False).encode("utf-8"))
        from ngs.diagnostics import event
        event('perf.catalog_refresh', elapsed_ms=round((time.perf_counter()-started)*1000, 1), cpu_ms=round((time.thread_time()-cpu)*1000, 1), games=len(feed['games']))
        # shortcut: old artwork revisions stay cached; add a disk budget before bulk import in step 5.
        return self.present(feed)
