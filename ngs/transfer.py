"""Isolated transfer worker: bounded stop/timeout without terminating the UI thread."""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ngs.catalog import atomic_write
from ngs.install import Cancelled, safe_path
from ngs.http_retry import transient_network_error


STALL_SECONDS = 60
STOP_SECONDS = 2


def worker_command():
    # Nuitka's sys.executable can name a nonexistent python.exe beside the runtime.
    root = Path(__file__).resolve().parents[1]
    return ([str(root / "NGS.exe")] if "__compiled__" in globals() else
            [sys.executable, "-B", str(root / "launcher.py")])


def run_transfer(storage, identity, origin, cancel, progress):
    resources = storage.resources.relative_to(storage.root).as_posix()
    if resources not in (".", "runtime"):
        raise ValueError("Download resources must be inside the NGS root or runtime folder.")
    folder = Path(tempfile.mkdtemp(prefix="transfer-", dir=storage.temp))
    request = dict(root=str(storage.root), resources=resources, identity=identity, origin=origin, parent=os.getpid())
    atomic_write(folder / "request.json", json.dumps(request).encode("utf8"))
    command = worker_command()
    process = None
    try:
        with (folder / "events.jsonl").open("w+b") as events, (folder / "worker.log").open("wb") as errors:
            process = subprocess.Popen([*command, "--transfer-worker", str(folder / "request.json")],
                cwd=storage.root, stdout=subprocess.DEVNULL, stderr=errors,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            last_activity, stopping, waiting = time.monotonic(), None, False
            last_path = "Starting download process"
            while True:
                # Bound each batch so an event backlog cannot starve pause/timeout checks.
                for unused in range(64):
                    position = events.tell()
                    line = events.readline(16 * 1024)
                    if not line or not line.endswith(b"\n"):
                        events.seek(position)
                        break
                    phase, done, total, path = json.loads(line)
                    last_path = path
                    last_activity, waiting = time.monotonic(), False
                    progress(phase, done, total, path)
                if process.poll() is not None:
                    break
                now = time.monotonic()
                if cancel.is_set():
                    if stopping is None:
                        (folder / "cancel").touch()
                        stopping = now
                    elif now - stopping >= STOP_SECONDS:
                        process.kill()
                        process.wait(timeout=5)
                        raise Cancelled("Download stopped; partial files preserved.")
                elif now - last_activity >= STALL_SECONDS:
                    logging.warning("Transfer stalled; game=%s, last file=%s", identity, last_path)
                    process.kill()
                    process.wait(timeout=5)
                    raise TimeoutError("No download progress for 60 seconds. Partial files preserved; choose Retry. " + last_path)
                elif now - last_activity >= 10 and not waiting:
                    progress("waiting", 0, 0, "No response from the file operation. You can pause it.")
                    waiting = True
                time.sleep(.1)
        result_path = folder / "result.json"
        if not result_path.is_file() or result_path.stat().st_size > 128 * 1024:
            # A noisy failed worker can have a large stderr log; only retain its tail.
            with (folder / "worker.log").open("rb") as errors:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 64 * 1024))
                tail = errors.read(64 * 1024)
            atomic_write(storage.logs / "transfer-error.log", tail)
            raise OSError("The download process ended without a result. Partial files preserved.")
        result = json.loads(result_path.read_bytes())
        if result["status"] == "cancelled":
            raise Cancelled(result["message"])
        if result["status"] != "ok":
            if result.get("retryable") is True:
                raise ConnectionError(result["message"])
            raise OSError(result["message"])
        return result["receipt"]
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if folder.parent == storage.temp and folder.resolve().is_relative_to(storage.temp.resolve()):
            try:
                shutil.rmtree(folder)
            except OSError:
                # Antivirus/indexer handles must not turn a completed install into an error,
                # or replace the original network/cancellation failure with a cleanup failure.
                logging.warning("Could not remove finished transfer scratch directory: %s", folder, exc_info=True)


def worker_main(request_path):
    from ngs.catalog import CatalogCache
    from ngs.downloads import DownloadQueue
    from ngs.portable import PortableStorage
    path = Path(request_path).resolve()
    if path.name != "request.json" or path.stat().st_size > 16 * 1024:
        raise ValueError("Invalid download job.")
    request = json.loads(path.read_bytes())
    resources = request.get("resources", ".")
    if resources not in (".", "runtime"):
        raise ValueError("Invalid download resource folder.")
    storage = PortableStorage(Path(request["root"]), resources=Path(request["root"]) / resources)
    if resources == "runtime":
        safe_path(storage.root, "runtime")
    storage.configure_process()
    if path.parent.parent != storage.temp or not path.parent.name.startswith("transfer-"):
        raise ValueError("Download jobs must be inside the NGS data/tmp folder.")
    safe_path(storage.temp, path.parent.name + "/request.json")
    from ngs.diagnostics import start, event
    diagnostics = start(storage, 'transfer')
    event('transfer.start', game_id=request['identity'], origin=request['origin'], request_path=str(path))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        parent = api.OpenProcess(0x100000, False, request["parent"])
        if not parent:
            return 1
        def parent_closed():
            api.WaitForSingleObject(parent, 0xFFFFFFFF)
            os._exit(74)  # Only this disposable worker dies; the queue/payload survive.
        threading.Thread(target=parent_closed, daemon=True).start()
    class FileCancel:
        last, stopped = 0.0, False
        def is_set(self):
            now = time.monotonic()
            if now - self.last >= .1:
                self.last, self.stopped = now, (path.parent / "cancel").exists()
            return self.stopped
    queue = DownloadQueue(storage, _worker=True)
    try:
        with (path.parent / "events.jsonl").open("a", encoding="utf8") as events:
            last, previous, last_io = 0.0, "", 0.0
            network, disk = 0, 0
            def flush_io():
                nonlocal last_io
                events.write(json.dumps(["io_total", network, disk, ""]) + "\n")
                events.flush()
                last_io = time.monotonic()
            def progress(phase, done, total, name):
                nonlocal last, previous, network, disk
                now = time.monotonic()
                if phase == "io":
                    network += done
                    disk += total
                    if now - last_io >= .2:
                        flush_io()
                    return
                if phase != previous or now - last >= .1:
                    events.write(json.dumps([phase, done, total, name], ensure_ascii=False) + "\n")
                    events.flush()
                    last, previous = now, phase
            try:
                receipt = queue.run(request["identity"], CatalogCache(storage, request["origin"]), FileCancel(), progress)
            finally:
                flush_io()
        result = dict(status="ok", receipt=receipt)
    except Cancelled as error:
        result = dict(status="cancelled", message=str(error))
    except Exception as error:
        logging.exception('Transfer worker failed')
        result = dict(status="error", message=str(error), retryable=transient_network_error(error))
    finally:
        queue.close()
        if diagnostics is not None: diagnostics.finish()
    atomic_write(path.parent / "result.json", json.dumps(result, ensure_ascii=False).encode("utf8"))
    return 0 if result["status"] == "ok" else 1
