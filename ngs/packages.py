"""Signed HTTP blocks, resumed independently and unpacked only into owned staging."""

import base64
import bisect
import hashlib
from itertools import accumulate
import json
import os
import re
import urllib.error
import urllib.request


PACKAGE_SIZE = 256 * 1024 * 1024
PACKAGE_PREFIX = "/assets/ngs_launcher/packages/"


def transport_payload(transport):
    unsigned = {key: value for key, value in transport.items() if key != "signature"}
    return b"NGS-Game-Packages-v1\n" + json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf8")


def package_key_payload(public):
    return b"NGS-Game-Packages-Key-v1\n" + public


def validate_transport(transport, document):
    fields = {"schema", "manifest_sha256", "chunks", "signature"}
    if (not isinstance(transport, dict) or set(transport) not in (fields, fields | {"key"})
            or type(transport["schema"]) is not int or transport["schema"] not in (1, 2)
            or not isinstance(transport["signature"], str) or len(transport["signature"]) != 344):
        raise ValueError("Invalid HTTP package description.")
    if "key" in transport:
        key = transport["key"]
        if (not isinstance(key, dict) or set(key) != {"public", "signature"}
                or any(not isinstance(key[k], str) for k in key)
                or len(key["public"]) != 380 or len(key["signature"]) != 344):
            raise ValueError("Invalid HTTP package key certificate.")
    core = {key: value for key, value in document.items() if key != "http"}
    digest = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()
    if transport["manifest_sha256"] != digest:
        raise ValueError("HTTP packages belong to a different file list.")
    chunks = transport["chunks"]
    if not isinstance(chunks, list) or len(chunks) > 100_000:
        raise ValueError("Invalid HTTP package list.")
    total = 0
    for chunk in chunks:
        compressed = transport["schema"] == 2
        if (not isinstance(chunk, dict) or set(chunk) != ({"path", "size", "sha256", "raw_size", "raw_sha256", "codec"} if compressed else {"path", "size", "sha256"})
                or type(chunk["size"]) is not int or not 0 < chunk["size"] <= PACKAGE_SIZE + (1024 * 1024 if compressed else 0)
                or not isinstance(chunk["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", chunk["sha256"])
                or (not compressed and chunk["path"] != PACKAGE_PREFIX + chunk["sha256"] + ".bin")):
            raise ValueError("Invalid HTTP package.")
        if compressed and (chunk["codec"] != "zstd" or type(chunk["raw_size"]) is not int or not 0 < chunk["raw_size"] <= PACKAGE_SIZE
                or not isinstance(chunk["raw_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", chunk["raw_sha256"])
                or not isinstance(chunk["path"], str) or not re.fullmatch(r"/assets/ngs_launcher/package_jobs/package-[0-9a-f]{32}/chunks/" + chunk["sha256"] + r"\.bin", chunk["path"])):
            raise ValueError("Invalid compressed HTTP package.")
        total += chunk["raw_size"] if compressed else chunk["size"]
    if total != document.get('archive_size', document["total_size"]):
        raise ValueError("HTTP package sizes do not match the game size.")
    return transport


def verify_transport(document, public_key):
    from ngs.update_crypto import RsaKey
    transport = validate_transport(document["http"], document)
    if "key" in transport:
        public = base64.b64decode(transport["key"]["public"], validate=True)
        with RsaKey(public_key) as root:
            root.verify(package_key_payload(public), base64.b64decode(transport["key"]["signature"], validate=True))
        public_key = public
    with RsaKey(public_key) as key:
        key.verify(transport_payload(transport), base64.b64decode(transport["signature"], validate=True))


def verify_stream_block(message, plan, number, public_key):
    if (not isinstance(message, dict) or set(message) != {"schema", "plan_sha256", "index", "chunk", "key", "signature"}
            or message["schema"] != 1 or type(message["index"]) is not int or message["index"] != number
            or message["plan_sha256"] != plan):
        raise ValueError("The package belongs to another download job.")
    core = dict(schema=1, total_size=message["chunk"].get("raw_size"))
    transport = dict(schema=2, chunks=[message["chunk"]], key=message["key"], signature=message["signature"],
        manifest_sha256=hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    validate_transport(transport, core)
    from ngs.update_crypto import RsaKey
    public = base64.b64decode(message["key"]["public"], validate=True)
    with RsaKey(public_key) as root:
        root.verify(package_key_payload(public), base64.b64decode(message["key"]["signature"], validate=True))
    with RsaKey(public) as key:
        key.verify(transport_payload(message), base64.b64decode(message["signature"], validate=True))
    return message["chunk"]


def package_workspace(document):
    return max((chunk["size"] for chunk in document.get("http", {}).get("chunks", [])), default=0)


def download_chunk(cache, chunk, target, cancel, progress, received=lambda size: None):
    from ngs.http_retry import retry_network
    return retry_network(lambda: _download_chunk(cache, chunk, target, cancel, progress, received), cancel, progress)


def _download_chunk(cache, chunk, target, cancel, progress, received):
    from ngs.install import BLOCK, check_cancel
    offset = target.stat().st_size if target.exists() else 0
    if offset > chunk["size"]:
        raise ValueError("The saved HTTP package exceeds its expected size.")
    digest = hashlib.sha256()
    if offset:
        with target.open("rb") as source:
            while data := source.read(BLOCK):
                check_cancel(cancel)
                digest.update(data)
                progress("resume", source.tell(), offset, "Checking saved HTTP package")
    if offset < chunk["size"]:
        check_cancel(cancel)
        headers = {"Accept": "application/octet-stream", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        request = urllib.request.Request(cache.origin + chunk["path"], headers=headers)
        try:
            response = cache.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            from ngs.http_retry import TRANSIENT_HTTP
            if status in TRANSIENT_HTTP:
                raise
            raise OSError("HTTP package unavailable (HTTP %s)." % status) from None
        with response:
            expected = chunk["size"] - offset
            if (response.code != (206 if offset else 200)
                    or response.headers.get("Content-Length") != str(expected)
                    or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                    or (offset and response.headers.get("Content-Range") !=
                        "bytes %d-%d/%d" % (offset, chunk["size"] - 1, chunk["size"]))):
                raise ValueError("The server returned an invalid HTTP package range.")
            with target.open("ab" if target.exists() else "xb") as output:
                try:
                    while offset < chunk["size"]:
                        check_cancel(cancel)
                        data = response.read1(min(BLOCK, chunk["size"] - offset))
                        if not data:
                            raise ConnectionError("HTTP package incomplete; partial data saved for resuming.")
                        output.write(data)
                        received(len(data))
                        digest.update(data)
                        offset += len(data)
                        progress("copy", offset, chunk["size"], "Downloading HTTP package")
                    if response.read(1):
                        raise ValueError("The HTTP package exceeds its declared size.")
                finally:
                    output.flush()
                    os.fsync(output.fileno())
    if digest.hexdigest() != chunk["sha256"]:
        # The path belongs to this manifest; discard a corrupt prefix so Retry can recover.
        with target.open("wb"):
            pass
        raise ValueError("HTTP package SHA256 mismatch; corrupt partial data discarded.")


def copy_packages(cache, document, payload, stage, cancel, progress, start=0, allowed_parts=(), number=1, count=None, fetch_chunk=None, file_ends=None):
    from ngs.install import BLOCK, check_cancel, safe_path
    folder = safe_path(stage, "http")
    folder.mkdir(exist_ok=True)
    allowed = {chunk["sha256"] + ".part" for chunk in document["http"]["chunks"]}
    for path in folder.iterdir():
        safe_path(folder, path.name)
        if (path.name not in allowed and path.name not in allowed_parts) or not path.is_file():
            raise ValueError("Unrelated files were found in the HTTP download folder.")
    ends = file_ends if file_ends is not None else list(accumulate(item['size'] for item in document['files']))
    if start == 0:
        for item in document["files"]:
            if not item["size"]:
                target = safe_path(payload, item["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb"):
                    pass

    def slices(start, size):
        index = bisect.bisect_right(ends, start)
        while size:
            item = document["files"][index]
            offset = start - (ends[index] - item["size"])
            count = min(size, item["size"] - offset)
            if count:
                yield item, offset, count
                start += count
                size -= count
            index += 1

    def saved_chunk(start, chunk):
        digest, done = hashlib.sha256(), 0
        raw_size = chunk.get("raw_size", chunk["size"])
        for item, offset, count in slices(start, raw_size):
            target = safe_path(payload, item["path"])
            if not target.is_file() or not offset + count <= target.stat().st_size <= item["size"]:
                return False
            with target.open("rb") as source:
                source.seek(offset)
                while count:
                    check_cancel(cancel)
                    data = source.read(min(BLOCK, count))
                    if not data:
                        return False
                    digest.update(data)
                    count -= len(data)
                    done += len(data)
                    progress("resume", done, raw_size, "Checking saved HTTP package")
        return digest.hexdigest() == chunk.get("raw_sha256", chunk["sha256"])

    for index, chunk in enumerate(document["http"]["chunks"], number):
        check_cancel(cancel)
        part = safe_path(folder, chunk["sha256"] + ".part")
        label = "Package %d of %d" % (index, count if count is not None else len(document["http"]["chunks"]))
        raw_size = chunk.get("raw_size", chunk["size"])
        if not saved_chunk(start, chunk):
            def advanced(phase, done, total, name):
                progress(phase, start + done * raw_size // max(1, total) if phase == "copy" else done,
                         document["total_size"] if phase == "copy" else total,
                         label + ' · ' + name if phase == 'waiting' else label)
            (fetch_chunk or download_chunk)(cache, chunk, part, cancel, advanced,
                                           received=lambda size: progress("io", size, 0, label))
            from ngs.compression import open_compressed
            with (open_compressed(part) if chunk.get("codec") == "zstd" else part.open("rb")) as incoming:
                unpacked = 0
                raw_digest = hashlib.sha256()
                for item, offset, remaining in slices(start, raw_size):
                    target = safe_path(payload, item["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("r+b" if target.exists() else "xb") as output:
                        output.seek(offset)
                        try:
                            while remaining:
                                check_cancel(cancel)
                                data = incoming.read(min(BLOCK, remaining))
                                if not data:
                                    raise ValueError("The local HTTP package is truncated.")
                                output.write(data)
                                progress("io", 0, len(data), label)
                                raw_digest.update(data)
                                remaining -= len(data)
                                unpacked += len(data)
                                progress("unpack", start + unpacked, document["total_size"], label)
                            if output.tell() == item["size"]:
                                output.truncate()
                        finally:
                            output.flush()
                            os.fsync(output.fileno())
                if incoming.read(1) or unpacked != raw_size or raw_digest.hexdigest() != chunk.get("raw_sha256", chunk["sha256"]):
                    raise ValueError("Extracted HTTP package integrity check failed.")
        if part.exists():
            safe_path(folder, part.name).unlink()
        start += raw_size
        progress("files", start, document["total_size"], label)
        progress("copy", start, document["total_size"], label)
