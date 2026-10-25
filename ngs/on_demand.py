"""Receive signed blocks as the NAS produces them, keeping the queue's source pinned."""
import hashlib
import json
import queue
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import accumulate

from ngs.api import MetadataApi, ApiCallError
from ngs.catalog import asset_path, atomic_write
from ngs.install import INSTALL_SPACE_RESERVE, MAX_MANIFEST, check_cancel, inventory, safe_path, validate_manifest
from ngs.packages import PACKAGE_SIZE, copy_packages, download_chunk, verify_stream_block, verify_transport
from ngs.http_retry import retry_network, TRANSIENT_HTTP

WORKER_POLL_SECONDS = 5
PREFETCH_BLOCKS = 4
API_BUSY_WAIT_SECONDS = 180


def fetch_reference(cache, reference, cancel):
    if (not isinstance(reference, dict) or set(reference) != {"path", "size", "sha256"}
            or type(reference["size"]) is not int or not 0 < reference["size"] <= MAX_MANIFEST
            or not isinstance(reference["sha256"], str) or len(reference["sha256"]) != 64):
        raise ValueError("Invalid transfer reference")
    asset_path(reference["path"])
    data = cache.fetch(reference["path"], MAX_MANIFEST, cancel, time.monotonic() + 90)
    if len(data) != reference["size"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("The download description is corrupt.")
    return json.loads(data)


def download_stream(storage, cache, card, stage, token, cancel, progress):
    stop, progress_lock = threading.Event(), threading.RLock()
    class Cancel:
        def is_set(self): return stop.is_set() or cancel.is_set()
    def report(*args):
        with progress_lock: progress(*args)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ngs-prefetch") as pool:
        try: return _download_stream(storage, cache, card, stage, token, Cancel(), report, pool)
        finally: stop.set()


def _download_stream(storage, cache, card, stage, token, cancel, progress, pool):
    if not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token):
        raise ValueError("Invalid download token")
    def wait(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            check_cancel(cancel)
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
    api = MetadataApi(storage, cache.origin)
    api_lock = threading.Lock()
    def request(value):
        try:
            with api_lock: return api.call('transfer', value)
        except ApiCallError as error:
            if error.status in TRANSIENT_HTTP:
                raise ConnectionError("Temporary NGS API error (HTTP %d)." % error.status) from error
            raise
    def call(value):
        if card.get("kind") == "installer":
            value = dict(value, action="installer_" + value["action"])
        rate_attempt, busy_attempt, busy_deadline = 0, 0, None
        while True:
            check_cancel(cancel)
            try:
                return retry_network(lambda: request(value), cancel, progress)
            except ApiCallError as error:
                if error.status == 409 and error.reason == 'busy':
                    if busy_deadline is None: busy_deadline = time.monotonic() + API_BUSY_WAIT_SECONDS
                    remaining = busy_deadline - time.monotonic()
                    if remaining <= 0: raise
                    delay = min(2 ** min(busy_attempt, 2), remaining)
                    busy_attempt += 1
                    progress('waiting', 0, 0, "Server busy with another request · retry %d in %g s" % (busy_attempt, delay))
                    wait(delay)
                    continue
                if error.status != 429 or rate_attempt == 10: raise
                rate_attempt += 1
                for unused in range(60):
                    progress("waiting", 0, 0, "API rate limit: resuming in a minute; downloaded files preserved")
                    wait(1)
    def check_server(value):
        if value.get('state') == 'error':
            if value.get('error') == 'WorkerStopped':
                raise RuntimeError("Package preparation stopped on the NAS. Partial files preserved; choose Retry.")
            raise RuntimeError("The server stopped the transfer: " + value.get('error', "Error"))
    from ngs.server_trust import server_key_path
    public = server_key_path(storage, cache.origin).read_bytes()
    pin = safe_path(storage.state, "stream-" + token + "-plan.json")
    download_request = dict(action="download", game=card["id"], revision=card["revision"], preview=token)
    if not pin.exists(): download_request['archive'] = 'tar-zstd'
    session = call(download_request)
    plan, reference, document, messages, file_ends = None, None, None, [], None
    ack, start, last_response = -1, 0, time.monotonic()
    pin = safe_path(storage.state, "stream-" + token + "-plan.json")
    journal = safe_path(storage.state, "stream-" + token + "-blocks.json")
    payload = safe_path(stage, "payload"); payload.mkdir(exist_ok=True)
    records_lock, pending = threading.Lock(), None
    ready = queue.Queue()
    slots = threading.BoundedSemaphore(PREFETCH_BLOCKS)
    known_parts = set()
    def remember(message, index):
        with records_lock:
            if index < len(messages):
                if messages[index] != message: raise ValueError("The saved package has changed.")
            elif index == len(messages):
                messages.append(message); atomic_write(journal, json.dumps(messages).encode())
            else: raise ValueError("A download package is missing.")
            known_parts.add(message['chunk']['sha256'] + '.part')
    def prefetch(path, index, offset, count):
        deadline = time.monotonic() + 300
        delay = .02
        while True:
            check_cancel(cancel)
            raw = cache.fetch(path, 8192, cancel, deadline, missing_ok=True)
            if raw is not None: break
            wait(delay); delay = min(.2, delay * 2)
        message = json.loads(raw)
        chunk = verify_stream_block(message, reference["sha256"], index, public)
        if offset + chunk["raw_size"] > document["total_size"]:
            raise ValueError("The server sent unexpected extra game data.")
        remember(message, index)
        part = safe_path(safe_path(stage, "http"), chunk["sha256"] + ".part")
        def receive(size): progress("io", size, 0, "Package %d of %d" % (index + 1, count))
        def status(phase, done, total, label):
            if phase == 'waiting': progress(phase, done, total, "Package %d of %d · %s" % (index + 1, count, label))
        try:
            download_chunk(cache, chunk, part, cancel, status, received=receive)
        except OSError as error:
            if "404" not in str(error): raise
            call(dict(action="repair", preview=token, index=index))
            download_chunk(cache, chunk, part, cancel, status, received=receive)
        info = part.stat()
        return message, (info.st_size, info.st_mtime_ns)
    def prefetch_ahead(path, index, offset, count):
        while offset < document['total_size']:
            while not slots.acquire(timeout=.1): check_cancel(cancel)
            check_cancel(cancel)
            result = prefetch(path, index, offset, count)
            ready.put(result)
            offset += result[0]['chunk']['raw_size']; index += 1
            path = path.rsplit('/', 1)[0] + '/block-%d.json' % index
    while True:
        check_cancel(cancel)
        check_server(session)
        if plan is None and session.get("plan"):
            reference = session["plan"]
            saved = dict(origin=cache.origin, card=card, plan=reference)
            if pin.is_file() and (pin.stat().st_size > 64 * 1024 or json.loads(pin.read_bytes()) != saved):
                raise ValueError("The download plan changed. Saved files were not reused.")
            plan = fetch_reference(cache, reference, cancel)
            if not isinstance(plan, dict) or set(plan) != {"document", "source_fingerprint"}:
                raise ValueError("Invalid transfer plan")
            if isinstance(plan['document'],dict) and plan['document'].get('format') == 'tar-zstd':
                from ngs.archive import receive_archive
                return receive_archive(storage,cache,card,stage,token,session,plan,reference,pin,journal,
                                       cancel,progress,pool,call,check_server)
            document = validate_manifest(plan["document"], card)
            file_ends = list(accumulate(item['size'] for item in document['files']))
            if "http" in document: raise ValueError("Unexpected plan transport")
            progress("download_size", -1, document["total_size"], "")
            atomic_write(pin, json.dumps(saved).encode())
            existing, dirs = inventory(payload, cancel)
            expected = {item["path"]: item["size"] for item in document["files"]}
            if (any(name not in expected or meta[0] > expected[name] for name, meta in existing.items())
                    or not set(dirs) <= set(document["directories"])):
                raise ValueError("Unrelated files were found in the unfinished installation.")
            remaining = document['total_size'] - sum(meta[0] for meta in existing.values())
            workspace = (PREFETCH_BLOCKS + 1) * (PACKAGE_SIZE + 1024 * 1024)
            if shutil.disk_usage(payload).free < remaining + workspace + INSTALL_SPACE_RESERVE:
                raise OSError("Not enough space for the game, prefetched HTTP packages and a 512 MiB reserve.")
            if journal.is_file():
                if journal.stat().st_size > MAX_MANIFEST: raise ValueError("Oversized block journal")
                messages = json.loads(journal.read_bytes())
                if not isinstance(messages, list) or len(messages) > 100_000: raise ValueError("Invalid block journal")
                for index, message in enumerate(messages):
                    verify_stream_block(message, reference["sha256"], index, public)
                    known_parts.add(message['chunk']['sha256'] + '.part')
            last_response = time.monotonic()
        message = session.get("block")
        verified = None
        if document and pending is not None and start < document['total_size']:
            next_poll = time.monotonic() + WORKER_POLL_SECONDS
            while True:
                check_cancel(cancel)
                try:
                    saved_message, verified = ready.get(timeout=.1)
                    slots.release()
                    break
                except queue.Empty:
                    if pending.done():
                        pending.result()
                        if ready.empty(): raise ValueError("Prefetch finished without the expected package.")
                progress("prepare", start, document["total_size"], "Waiting for the next HTTP package")
                if ready.empty() and time.monotonic() >= next_poll:
                    session = call(dict(action='transfer', preview=token, ack=ack))
                    check_server(session)
                    message = session.get('block')
                    next_poll = time.monotonic() + WORKER_POLL_SECONDS
            if message is not None and saved_message != message:
                raise ValueError("The prefetched package has changed.")
            message = saved_message
        if document and message:
            chunk = verify_stream_block(message, reference["sha256"], ack + 1, public)
            if start + chunk["raw_size"] > document["total_size"]:
                raise ValueError("The server sent unexpected extra game data.")
            remember(message, ack + 1)
            current = dict(document, http=dict(chunks=[chunk]))
            block_size = messages[0]["chunk"]["raw_size"]
            count = (document["total_size"] + block_size - 1) // block_size
            def fetch_current(cache, chunk, part, cancel, advanced, received):
                nonlocal pending
                info = part.stat() if part.exists() else None
                if verified is None or info is None or (info.st_size, info.st_mtime_ns) != verified:
                    download_chunk(cache, chunk, part, cancel, advanced, received=received)
                next_path = session.get("next_block_path")
                if pending is None and next_path is not None and start + chunk["raw_size"] < document["total_size"]:
                    expected = chunk["path"].rsplit("/chunks/", 1)[0] + "/block-%d.json" % (ack + 2)
                    if next_path != expected: raise ValueError("Invalid path for the next HTTP package.")
                    asset_path(next_path)
                    pending = pool.submit(prefetch_ahead, next_path, ack + 2, start + chunk["raw_size"], count)
            try:
                copy_packages(cache, current, payload, stage, cancel, progress, start=start,
                    number=ack + 2, count=count,
                    allowed_parts=known_parts, fetch_chunk=fetch_current, file_ends=file_ends)
            except OSError as error:
                if "404" not in str(error): raise
                call(dict(action="repair", preview=token, index=ack + 1))
                copy_packages(cache, current, payload, stage, cancel, progress, start=start,
                    number=ack + 2, count=count,
                    allowed_parts=known_parts, fetch_chunk=fetch_current, file_ends=file_ends)
            start += chunk["raw_size"]; ack += 1; last_response = time.monotonic()
        if document and start == document["total_size"] and session.get("manifest"):
            final = validate_manifest(fetch_reference(cache, session["manifest"], cancel), card)
            verify_transport(final, public)
            core = dict(final); core.pop("http")
            core["files"] = [dict(item, sha256="0" * 64) for item in core["files"]]
            if core != document or final["http"]["chunks"] != [item["chunk"] for item in messages]:
                raise ValueError("The final file list differs from the pinned download plan.")
            call(dict(action="transfer", preview=token, ack=ack))
            return final
        progress("prepare", session.get("done", 0), session.get("total", 0),
                 "The NAS is compressing the next Zstandard blocks; transfer continues as blocks become ready")
        if time.monotonic() - last_response > 300:
            raise TimeoutError("The server did not prepare the next block within 5 minutes. Partial files preserved.")
        if not message: wait(.5)
        session = call(dict(action="transfer", preview=token, ack=ack))
