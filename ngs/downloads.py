"""Portable FIFO queue; payload lengths, not UI counters, determine resume offsets."""

from ngs.i18n import tr
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from ngs.catalog import validate_card
from ngs.install import (Cancelled, LocalLibrary, fetch_manifest, game_id, install_game,
                         installation_lock, is_link, remove_stage, safe_path, check_cancel, hash_file, require_local_source,
                         inventory, INSTALL_SPACE_RESERVE)
from ngs.portable import path_list, path_key
from ngs.packages import PACKAGE_SIZE
from ngs.diagnostics import event
from ngs.http_retry import transient_network_error


STATES = {"queued", "running", "pausing", "paused", "error", "cancelling", "stopping"}


def disk_path(path):
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def install_space(storage, directory, size, workspace, queue=None, *, cancel=None):
    if (type(size) is not int or not 0 <= size <= 2**50
            or type(workspace) is not int or not 0 <= workspace <= 2 * 1024**3):
        raise ValueError(tr("Invalid game or temporary package size."))
    path = Path(directory)
    if not path.is_absolute():
        path = storage.root / path
    reserved, pending_workspace, unknown = queue.reserved_space(path, cancel=cancel) if queue else (0, 0, False)
    free = shutil.disk_usage(disk_path(path)).free
    reserve_gb = storage.read_settings().get("free_space_reserve_gb", 0.5) if hasattr(storage, 'read_settings') else 0.5
    if type(reserve_gb) not in (int, float) or not 0.5 <= reserve_gb <= 50:
        reserve_gb = 0.5
    reserve = max(INSTALL_SPACE_RESERVE, int(reserve_gb * 1024**3))
    remaining = free - reserved - size
    allowed = not unknown and remaining >= reserve + max(workspace, pending_workspace)
    status = (tr("A queued game's size is still unknown. Wait for its download to start or remove it from the queue.") if unknown
              else "" if allowed else tr("Not enough space for the queue, temporary packages and reserve."))
    return dict(free=free, reserved=reserved, available=free-reserved, remaining=remaining,
                workspace=workspace, reserve=reserve, can_install=allowed, status=status)


class DownloadQueue:
    def __init__(self, storage, _worker=False):
        import msvcrt
        self.storage = storage
        self.lock = None
        if _worker:
            self.path = safe_path(storage.state, "downloads.sqlite")
            if not self.path.is_file():
                raise ValueError(tr("The download queue was not created by NGS."))
            return
        self.lock = safe_path(storage.state, "queue.lock").open("a+b")
        try:
            if self.lock.tell() == 0:
                self.lock.write(b"\0")
                self.lock.flush()
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.lock.close()
            raise OSError(tr("This NGS folder is already open in another launcher instance.")) from None
        self.path = safe_path(storage.state, "downloads.sqlite")
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS queue (position INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "game_id TEXT UNIQUE NOT NULL, job TEXT NOT NULL)")
                db.execute('CREATE TABLE IF NOT EXISTS transfer_cleanup (token TEXT PRIMARY KEY, origin TEXT NOT NULL, '
                           'installers INTEGER NOT NULL, retry_at REAL NOT NULL DEFAULT 0)')
            for job in self.jobs():
                if job["state"] in ("running", "pausing", "stopping"):
                    job["state"] = "paused" if job["state"] == "pausing" else "queued"
                    job["message"] = tr("Restored after NGS closed.")
                    self.save(job)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock is not None and not self.lock.closed:
            self.lock.close()

    def location(self, path):
        path = Path(path)
        return path.relative_to(self.storage.root).as_posix() if path.is_relative_to(self.storage.root) else str(path)

    def resolve(self, value):
        if not isinstance(value, str) or not value or ".." in Path(value).parts:
            raise ValueError(tr("Invalid download job path."))
        path = Path(value)
        if path.is_absolute():
            return safe_path(path.parent, path.name)
        return safe_path(self.storage.root, value)

    def jobs(self):
        result = []
        with closing(sqlite3.connect(self.path)) as db:
            for identity, raw in db.execute("SELECT game_id, job FROM queue ORDER BY position"):
                job = json.loads(raw)
                if (not isinstance(job, dict) or job.get("schema") != 1 or job.get("state") not in STATES
                        or not re.fullmatch(r"[0-9a-f]{32}", job.get("token", ""))
                        or validate_card(job["card"])["id"] != game_id(identity)):
                    raise ValueError(tr("The download queue is corrupt. The original database is preserved."))
                result.append(job)
        return result

    def get(self, identity):
        return next((job for job in self.jobs() if job["card"]["id"] == identity), None)

    def save(self, job):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE queue SET job=? WHERE game_id=?", (json.dumps(job, ensure_ascii=False), job["card"]["id"]))
        event('queue.saved', game_id=job['card']['id'], state=job['state'], job_id=job['token'])

    def update(self, identity, _expected=None, **fields):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT job FROM queue WHERE game_id=?", (identity,)).fetchone()
            if row:
                job = json.loads(row[0])
                if _expected is not None and job["state"] not in ((_expected,) if isinstance(_expected, str) else _expected):
                    return None
                job.update(fields)
                db.execute("UPDATE queue SET job=? WHERE game_id=?", (json.dumps(job, ensure_ascii=False), identity))
                if 'state' in fields or 'stage_identity' in fields:
                    event('queue.changed', game_id=identity, job_id=job['token'], **fields)
                return job

    def reserved_space(self, directory, *, cancel=None, scan=True):
        volume = disk_path(directory).stat().st_dev
        reserved, workspace, unknown = 0, 0, False
        for job in self.jobs():
            if cancel is not None:
                check_cancel(cancel)
            target = self.resolve(job["games"])
            try:
                if disk_path(target).stat().st_dev != volume:
                    continue
            except OSError:
                if target.anchor.casefold() != Path(directory).anchor.casefold():
                    continue
                raise
            size = job.get("total_bytes", job["card"].get("total_size", -1))
            if type(size) is not int or not 0 <= size <= 2**50:
                unknown = True
                continue
            present = 0
            stage = safe_path(self.resolve(job["staging"]), "queue-" + job["token"])
            if stage.exists():
                info = stage.lstat()
                if is_link(info) or job.get("stage_identity") != [info.st_dev, info.st_ino]:
                    raise ValueError(tr("Could not verify ownership of the partial download."))
                payload = safe_path(stage, "payload")
                if payload.is_dir():
                    if scan:
                        files, _ = inventory(payload, cancel)
                        present = sum(item[0] for item in files.values())
                    else:
                        # The footer uses checkpoints; preview and admission inventory actual payload lengths.
                        present = max(0, job.get("completed_bytes", 0))
            reserved += max(0, size - present)
            scratch = job.get("workspace", (5 if job["card"].get("delivery") == "on_request" else 1)
                              * (PACKAGE_SIZE + 1024 * 1024))
            if type(scratch) is not int or not 0 <= scratch <= 2 * 1024**3:
                raise ValueError(tr("The temporary download package reservation is corrupt."))
            # Transfers are FIFO: only one game's temporary packets exist at a time.
            workspace = max(workspace, scratch)
        return reserved, workspace, unknown

    def add(self, card, source, origin, directory=None, source_candidates=None, *, space=None):
        card = validate_card(card)
        if not card.get("manifest") and card.get("delivery") != "on_request":
            raise ValueError(tr("No manifest has been published for this game yet."))
        existing = self.get(card["id"])
        if existing:
            return existing
        if len(self.jobs()) >= 100:
            raise ValueError(tr("The queue already contains 100 games."))
        games, staging = (self.storage.install_directories(directory) if directory is not None
                          else (self.storage.games, self.storage.game_staging))
        job = {"schema": 1, "token": uuid.uuid4().hex, "card": card, "source": str(source), "origin": origin,
               "games": self.location(games), "staging": self.location(staging),
               "stage_identity": None, "state": "queued", "message": tr("Queued"), "progress": 0.0,
               "completed_bytes": 0, "total_bytes": space["size"] if space else card.get("total_size", -1), "network_bytes": 0, "network_total": -1}
        if space is not None:
            job["workspace"] = space["workspace"]
        if source_candidates is not None:
            candidates = path_list(source_candidates, tr("NAS folders"), absolute=True)
            if path_key(candidates[0]) != path_key(source):
                raise ValueError(tr("The download source does not match the selected NAS folders."))
            if len(candidates) > 1:
                job["source_candidates"] = candidates
        if space is not None:
            # Only the lock-owning launcher admits jobs; do not lock out worker checkpoints during inventory.
            current = install_space(self.storage, games, space["size"], space["workspace"], self)
            if not current["can_install"]:
                raise ValueError(current["status"])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO queue (game_id, job) VALUES (?, ?)", (card["id"], json.dumps(job, ensure_ascii=False)))
        event('queue.added', game_id=card['id'], name=card['name'], job_id=job['token'], games=str(games),
              staging=str(staging), origin=origin, source=str(source), total_bytes=job['total_bytes'])
        return job

    def set_state(self, identity, state, message="", source=None):
        if state not in STATES:
            raise ValueError("Invalid queue state")
        fields = {"state": state, "message": message, "retry_at": 0}
        if state == "cancelling":
            fields["discard"] = True
        if source is not None:
            fields["source"] = str(source)
        self.update(identity, **fields)

    def failed_transfer(self, identity, error, *, isolated=False):
        # The parent watchdog also covers blocked disk I/O; only the worker knows it was network.
        retry = transient_network_error(error) and not (isolated and isinstance(error, TimeoutError))
        self.update(identity, _expected=("queued", "running"), state="queued" if retry else "error",
                    retry_at=time.time() + 60 if retry else 0,
                    message=tr("Offline · resuming automatically when connected. Partial files preserved.") if retry else str(error))
        self.update(identity, _expected="pausing", state="paused", retry_at=0, message=tr("Paused · partial files preserved"))

    def context(self, job):
        games, staging = self.resolve(job["games"]), self.resolve(job["staging"])
        for source in path_list(job.get("source_candidates") or [job["source"]], tr("NAS folders"), absolute=True):
            if games.is_relative_to(Path(source)) or Path(source).is_relative_to(games):
                raise ValueError(tr("The NAS source and installation folders must not overlap."))
        if (games == self.storage.root or games.is_relative_to(self.storage.data)
                or self.storage.root.is_relative_to(games)
                or staging not in (self.storage.downloads, games / ".ngs-downloads")):
            raise ValueError(tr("Invalid download job directories."))
        if job["card"].get("kind") == "installer":
            games = safe_path(games, ".ngs-installers")
            games.mkdir(exist_ok=True)
        return SimpleNamespace(root=self.storage.root, resources=self.storage.resources,
                               config=self.storage.config, read_settings=lambda: {'tactic_url':job['origin']},
                               state=self.storage.state, games=games, game_staging=staging)

    def stage(self, job, create=False):
        context = self.context(job)
        stage = safe_path(context.game_staging, "queue-" + job["token"])
        if not stage.exists():
            if not create:
                return context, stage
            stage.mkdir()
        info = stage.lstat()
        identity = [info.st_dev, info.st_ino]
        if is_link(info) or not stage.is_dir():
            raise ValueError(tr("The partial download folder was replaced with a link."))
        if job["stage_identity"] is None:
            if any(stage.iterdir()):
                raise ValueError(tr("The partial folder has no verified NGS ownership marker."))
            job["stage_identity"] = identity
            self.update(job["card"]["id"], stage_identity=identity)
        elif job["stage_identity"] != identity:
            raise ValueError(tr("The partial folder was replaced. Files preserved for review."))
        return context, stage

    def forget_row(self, identity):
        job = self.get(identity)
        event('queue.remove_record', game_id=identity, state=job.get('state') if job else None)
        with closing(sqlite3.connect(self.path)) as db, db:
            if job and job['card'].get('delivery') == 'on_request':
                db.execute('INSERT OR IGNORE INTO transfer_cleanup(token,origin,installers) VALUES(?,?,?)',
                           (job['token'], job['origin'], int(job['card'].get('kind') == 'installer')))
            db.execute("DELETE FROM queue WHERE game_id=?", (identity,))
        if job and job['card'].get('delivery') == 'on_request':
            self.clean_stream_journals(job['token'])

    def clean_stream_journals(self, token):
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
            raise ValueError('Invalid cleanup token')
        for suffix in ('plan', 'blocks'):
            safe_path(self.storage.state, 'stream-' + token + '-' + suffix + '.json').unlink(missing_ok=True)

    def clean_transfers(self, cancel):
        from ngs.api import MetadataApi
        from urllib.parse import urlsplit
        address = urlsplit(self.storage.read_settings()['tactic_url'])
        current_origin = address.scheme + '://' + address.netloc
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT token,origin,installers FROM transfer_cleanup WHERE retry_at<=? AND origin=? LIMIT 16',
                              (time.time(), current_origin)).fetchall()
        for token, origin, installers in rows:
            if cancel.is_set(): break
            try:
                self.clean_stream_journals(token)
                response = MetadataApi(self.storage, origin).call('transfer',
                    dict(action='installer_release' if installers else 'release', preview=token))
                if response != {'state': 'released'}:
                    if response == {'state': 'releasing'}: raise ConnectionError('NAS producer is stopping')
                    raise ValueError('Invalid transfer cleanup response')
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('DELETE FROM transfer_cleanup WHERE token=?', (token,))
                event('transfer.cleaned', token=token, origin=origin)
            except Exception as error:
                logging.warning('Transfer cleanup deferred (%s)', type(error).__name__)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('UPDATE transfer_cleanup SET retry_at=? WHERE token=?', (time.time()+60, token))
                break

    def cleanup_due(self):
        from urllib.parse import urlsplit
        address = urlsplit(self.storage.read_settings()['tactic_url'])
        origin = address.scheme + '://' + address.netloc
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT 1 FROM transfer_cleanup WHERE retry_at<=? AND origin=? LIMIT 1', (time.time(), origin)).fetchone() is not None

    def discard(self, identity):
        job = self.get(identity)
        if not job:
            return
        try:
            with installation_lock(self.storage):
                context, stage = self.stage(job)
                event('queue.discard_start', game_id=identity, path=str(stage), stage_identity=job['stage_identity'])
                records = safe_path(self.storage.state, "installers" if job["card"].get("kind") == "installer" else "installs")
                records.mkdir(exist_ok=True)
                pending = safe_path(records, identity + ".pending.json")
                owned_pending = False
                if pending.is_file() and pending.stat().st_size <= 64 * 1024 and (stage / "payload").is_dir():
                    receipt = json.loads(pending.read_bytes())
                    info = (stage / "payload").stat()
                    owned_pending = (receipt.get("managed") is True and receipt["card"]["id"] == identity
                                     and receipt.get("directory_identity") == [info.st_dev, info.st_ino]
                                     and LocalLibrary(self.storage).install_path(receipt).parent == context.games)
                if stage.exists():
                    remove_stage(context, stage)
                if owned_pending:
                    pending.unlink()
                self.forget_row(identity)
                event('queue.discard_done', game_id=identity, path=str(stage))
        except Exception as error:
            self.set_state(identity, "error", tr("Could not remove the partial copy: ") + str(error))
            raise

    def run(self, identity, cache, cancel=None, progress=lambda *args: None, isolated=False):
        cancel = cancel or threading.Event()
        if isolated:
            from ngs.transfer import run_transfer
            try:
                return run_transfer(self.storage, identity, cache.origin, cancel, progress)
            except Exception as error:
                if isinstance(error, Cancelled):
                    self.update(identity, _expected=("queued", "running", "pausing"), state="paused",
                                message=tr("Paused · partial files preserved"))
                else:
                    self.failed_transfer(identity, error, isolated=True)
                job = self.get(identity)
                if job and job["state"] == "cancelling":
                    self.discard(identity)
                raise
        if cancel.is_set():
            self.update(identity, _expected="pausing", state="paused", message=tr("Paused"))
            raise Cancelled(tr("Download stopped before the transfer started."))
        job = self.update(identity, _expected="queued", state="running", message=tr("Fetching manifest…"))
        if not job:
            self.update(identity, _expected="pausing", state="paused", message=tr("Paused"))
            raise Cancelled(tr("This job is no longer waiting to download."))
        totals = {key: job.get(key, default) for key, default in (
            ("completed_bytes", 0), ("total_bytes", job["card"].get("total_size", -1)),
            ("network_bytes", 0), ("network_total", -1))}
        last_checkpoint = time.monotonic()
        last_log = 0.0
        report_progress = progress
        def checkpoint():
            self.update(identity, **totals)
        def progress(phase, done, total, path):
            nonlocal last_checkpoint, last_log
            if phase == "download_size":
                totals["total_bytes"] = total
                if done >= 0:
                    totals["network_total"] = done
            elif phase in ("files", "unpack", "verify"):
                totals["total_bytes"] = total
                totals["completed_bytes"] = min(total, max(totals["completed_bytes"], total if phase == "verify" else done))
            elif phase == "io":
                totals["network_bytes"] += done
            now = time.monotonic()
            if now - last_log >= 5:
                event('transfer.progress', game_id=identity, phase=phase, path=path, **totals)
                last_log = now
            if now - last_checkpoint >= 1 or phase == "download_size":
                checkpoint(); last_checkpoint = now
            if phase not in ("download_size", "files"):
                report_progress(phase, done, total, path)
        try:
            if cache.origin != job["origin"]:
                raise ValueError(tr("The download server differs from the server assigned to this job."))
            # A rename can have completed immediately before the previous process died.
            installed = LocalLibrary(self.storage, installers=job["card"].get("kind") == "installer").reconcile()
            recovered = next((r for r in installed if r["card"]["id"] == identity and r.get("managed") is True), None)
            if recovered:
                context = self.context(job)
                if (LocalLibrary(self.storage).install_path(recovered).parent != context.games
                        or recovered["card"].get("manifest") != job["card"].get("manifest")
                        or (job["card"].get("delivery") == "on_request" and recovered["card"] != job["card"])):
                    raise FileExistsError(tr("Another copy of this game is already registered. Download files preserved."))
                context, stage = self.stage(job)
                if stage.exists():
                    with installation_lock(self.storage):
                        remove_stage(context, stage)
                self.forget_row(identity)
                return recovered
            progress("manifest", 0, 0, tr("Fetching game manifest"))
            if job["card"].get("delivery") == "on_request":
                from ngs.on_demand import download_stream
                context, stage = self.stage(job, create=True)
                document = download_stream(self.storage, cache, job["card"], stage, job["token"], cancel, progress)
            else:
                document = fetch_manifest(cache, job["card"], cancel)
            progress("download_size", sum(chunk["size"] for chunk in document.get("http", {}).get("chunks", [])),
                     document["total_size"], "")
            job = self.get(identity)
            if "http" not in document:
                require_local_source(Path(job["source"]))
            if job.get("source_candidates") and "http" not in document:
                if job["stage_identity"] is not None:
                    raise ValueError(tr("The NAS source cannot change after a download starts."))
                executable = next(item for item in document["files"] if item["path"] == document["executable"])
                selected = None
                self.update(identity, _expected="running", message=tr("Searching configured NAS folders for the game…"))
                for candidate in path_list(job["source_candidates"], tr("NAS folders"), absolute=True):
                    check_cancel(cancel)
                    progress("source", 0, 0, candidate)
                    try:
                        root = safe_path(require_local_source(Path(candidate)), document["source_folder"])
                        path = safe_path(root, document["executable"])
                        if path.is_file() and path.stat().st_size == executable["size"]:
                            if hash_file(path, cancel, lambda done: progress("source", 0, 0, str(path))) == executable["sha256"]:
                                selected = candidate
                                break
                    except (OSError, ValueError):
                        continue
                check_cancel(cancel)
                if selected is None:
                    raise ValueError(tr("No game folder with the confirmed executable was found in the configured NAS folders: ")
                                     + "; ".join(job["source_candidates"]))
                # Persist the chosen root before any partial payload is created; resumes keep it pinned.
                job = self.update(identity, _expected="running", source=selected, source_candidates=[])
                if job is None:
                    raise Cancelled(tr("Download source selection stopped."))
            context, stage = self.stage(job, create=True)
            if document.get('format') == 'tar-zstd':
                from ngs.archive import install_archive
                result = install_archive(context,job['card'],document,stage,cancel)
            else:
                result = install_game(context, Path(job["source"]), job["card"], document, cancel, progress,
                                      stage=stage, http_cache=cache if "http" in document else None,
                                      http_payload_ready=job['card'].get('delivery') == 'on_request')
            with installation_lock(self.storage):
                remove_stage(context, stage)
            self.forget_row(identity)
            return result
        except Cancelled:
            self.update(identity, _expected=("running", "pausing"), state="paused",
                        message=tr("Paused · partial files preserved"))
            requested = self.get(identity)
            if requested and requested["state"] == "cancelling":
                self.discard(identity)
            raise
        except Exception as error:
            self.failed_transfer(identity, error)
            requested = self.get(identity)
            if requested and requested["state"] == "cancelling":
                self.discard(identity)
                raise Cancelled(tr("Download canceled.")) from error
            raise
        finally:
            checkpoint()
