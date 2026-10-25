"""Portable session events and resumable, signed uploads to private NGS storage."""
import base64
from contextlib import closing
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
import zipfile


_current = None
_production = False
_secrets = set()
_sensitive = re.compile(r'password|passwd|secret|ticket|authorization|cookie|api.?key|access.?token', re.I)
UPLOAD_INITIAL_DELAY_SECONDS = 10
UPLOAD_BATCH_DELAY_SECONDS = 300
UPLOAD_RETRY_DELAY_SECONDS = 600


def redact(value):
    if isinstance(value, dict):
        return {str(k): '[redacted]' if _sensitive.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)): return [redact(v) for v in value[:256]]
    if isinstance(value, Path): value = str(value)
    if isinstance(value, str):
        for secret in tuple(_secrets): value = value.replace(secret, '[redacted]')
        value = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', value, flags=re.I)
        value = re.sub(r'(?i)(\b(?:password|passwd|secret|ticket|authorization|cookie|api[_-]?key|access[_-]?token)\b["\s:=]+)([^\s,;&"\'}]+)', r'\1[redacted]', value)
        value = re.sub(r'(?i)\b(Bearer|Basic)\s+\S+', r'\1 [redacted]', value)
        return value[:12000]
    return value if value is None or isinstance(value, (bool, int, float)) else redact(str(value))


def event(event_name, **fields):
    if _production: return
    if _current is not None: _current.write(event_name, **fields)


def note_activity():
    """Wake batching for received worker progress without another log write.

    Isolated transfer workers have their own in-memory events. Their existing
    IPC progress reaches the GUI and can notify its uploader through this hook.
    No filesystem scan, timer or network request is performed here.
    """
    if not _production and _current is not None:
        _current.activity.set()


def remember_secret(value):
    if isinstance(value, str) and len(value) >= 8: _secrets.add(value)


class RedactedFormatter(logging.Formatter):
    def format(self, record): return redact(super().format(record))


class SessionLog(logging.Handler):
    def __init__(self, storage, mode='launcher'):
        super().__init__()
        from ngs.install import safe_path
        self.storage, self.mode = storage, mode
        inherited = os.environ.get('NGS_DIAGNOSTIC_SESSION', '') if mode == 'transfer' else ''
        self.session = inherited if re.fullmatch('[0-9a-f]{32}', inherited) else uuid.uuid4().hex
        if mode == 'launcher': os.environ['NGS_DIAGNOSTIC_SESSION'] = self.session
        self.folder = safe_path(storage.logs, 'sessions'); self.folder.mkdir(exist_ok=True)
        self.archives = safe_path(storage.logs, 'archives'); self.archives.mkdir(exist_ok=True)
        self.guard = threading.RLock()
        self.active = None
        self.stopped = threading.Event()
        self.resumed = threading.Event(); self.resumed.set()
        self.activity = threading.Event()
        self.thread = None
        self.previous_hook, self.previous_thread_hook = sys.excepthook, threading.excepthook
        self.metadata = dict(session=self.session, root=str(storage.root), resources=str(storage.resources))
        self.write('session.start', mode=mode, pid=os.getpid(), python=sys.version, **self.metadata)
        if mode == 'launcher':
            self.snapshot_process_exits()
            threading.Thread(target=self.collect_crashes, name='NGS crash evidence', daemon=True).start()

    def collect_crashes(self):
        """Summarise faulthandler output, WER minidumps and Windows fault events of earlier runs."""
        if self.stopped.wait(5) or _production:
            return
        try:
            from ngs import crash_capture
            crash_capture.collect_previous(self.storage, self.write)
        except Exception as error:
            self.write('process.crash_collect_error', error='%s: %s' % (type(error).__name__, error))

    def snapshot_process_exits(self):
        """Carry the previous native exit into the next signed diagnostic upload.

        The bootstrap writes after Python is gone. Read its bounded metadata once
        at ordinary startup, without polling or reading arbitrary log contents.
        """
        from ngs.install import safe_path
        try:
            path = safe_path(self.storage.logs, 'process-exit.jsonl')
            if not path.is_file():
                return
            with path.open('rb') as stream:
                raw = stream.read(66 * 1024 + 1)
            if len(raw) > 66 * 1024:
                raise ValueError('Oversized native process-exit log')
            fields = {'time_utc', 'pid', 'exit_code', 'exit_hex', 'elapsed_ms'}
            for line in raw.splitlines()[-32:]:
                try:
                    row = json.loads(line)
                    if (not isinstance(row, dict) or set(row) != fields
                            or any(type(row[key]) is not int for key in ('pid', 'exit_code', 'elapsed_ms'))
                            or not 0 < row['pid'] <= 0xffffffff or not 0 <= row['exit_code'] <= 0xffffffff
                            or row['elapsed_ms'] < 0 or not isinstance(row['time_utc'], str)
                            or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', row['time_utc'])
                            or row['exit_hex'] != '0x%08X' % row['exit_code']):
                        continue
                    self.write('process.previous_exit', runtime_pid=row['pid'],
                               **{key: value for key, value in row.items() if key != 'pid'})
                except (ValueError, TypeError):
                    continue
        except (OSError, ValueError) as error:
            self.write('process.exit_snapshot_error', error=str(error))

    def write(self, event_name, **fields):
        if _production: return
        try:
            row = redact(dict(time=time.time(), event=event_name, pid=os.getpid(), mode=self.mode) | fields)
            raw = (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf8')
            with self.guard:
                if self.active is None: self.active = self.folder / (self.session + '-' + uuid.uuid4().hex + '.active')
                if self.active.is_symlink(): raise ValueError('Redirected session log')
                if self.active.exists() and self.active.stat().st_size + len(raw) > 64 * 1024: self.rotate()
                if self.active is None: self.active = self.folder / (self.session + '-' + uuid.uuid4().hex + '.active')
                with self.active.open('ab') as stream:
                    stream.write(raw); stream.flush()
                # Upload/snapshot bookkeeping is useful evidence, but must not
                # create an endless stream of new uploads while NGS is idle.
                if threading.current_thread() is not self.thread:
                    self.activity.set()
        except Exception:
            # A full disk must not interrupt a game or recursively call this logging handler.
            pass

    def emit(self, record):
        self.write('log', level=record.levelname, message=self.format(record))

    def rotate(self):
        with self.guard:
            if self.active is not None and self.active.exists():
                os.replace(self.active, self.active.with_suffix('.jsonl'))
            self.active = None

    def capture_exception(self, kind, error, tb):
        self.write('exception', error_type=kind.__name__, traceback=''.join(traceback.format_exception(kind, error, tb)))

    def snapshot(self):
        if _production: return
        """Read queue metadata only; never scan game contents or alter their SQLite database."""
        path = self.storage.state / 'downloads.sqlite'
        self.write('queue.snapshot', database=str(path), root=str(self.storage.root), exists=path.exists())
        roots, owned = {self.storage.downloads}, set()
        if path.is_file():
            try:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                    rows = db.execute('SELECT job FROM queue ORDER BY position LIMIT 100').fetchall()
                for row, in rows:
                    job = json.loads(row)
                    details = {k: job.get(k) for k in ('state', 'message', 'source', 'origin', 'games', 'staging',
                        'stage_identity', 'completed_bytes', 'total_bytes', 'network_bytes', 'network_total')}
                    card = job.get('card', {})
                    details.update(game_id=card.get('id'), name=card.get('name'), kind=card.get('kind'), job_id=job.get('token'))
                    self.write('queue.job', **details)
                    staging = Path(job['staging'])
                    if not staging.is_absolute(): staging = self.storage.root / staging
                    roots.add(staging); owned.add('queue-' + job['token'])
            except Exception as error: self.write('queue.snapshot_error', error=str(error))
        try:
            from ngs.portable import configured_paths
            settings = self.storage.read_settings()
            for directory in configured_paths(settings, 'games_directories', 'games_directory', 'games'):
                root = Path(directory)
                if not root.is_absolute(): root = self.storage.root / root
                roots.add(root / '.ngs-downloads')
            self.write('settings.paths', **{k: settings.get(k) for k in ('tactic_url', 'games_directory', 'games_directories')})
            for root in roots:
                if not root.is_dir() or root.is_symlink(): continue
                for index, child in enumerate(root.iterdir()):
                    if index >= 256: break
                    self.write('queue.folder', path=str(child), registered=child.name in owned, redirected=child.is_symlink(),
                               kind='queue' if re.fullmatch('queue-[0-9a-f]{32}', child.name) else 'unknown')
        except Exception as error: self.write('paths.snapshot_error', error=str(error))

    def pack(self):
        if _production: return
        self.rotate()
        for path in self.folder.glob('*.active'):
            if not path.name.startswith(self.session + '-') and re.fullmatch('[0-9a-f]{32}-[0-9a-f]{32}\\.active', path.name) and not path.is_symlink():
                os.replace(path, path.with_suffix('.jsonl'))
        for path in sorted(self.folder.glob('*.jsonl'), key=lambda p: p.stat().st_mtime):
            if not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{32}\\.jsonl', path.name) or path.is_symlink(): continue
            stat = path.stat()
            if stat.st_size > 128 * 1024: continue
            raw = path.read_bytes()
            session = path.name[:32]
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                archive.writestr('events.jsonl', raw)
                archive.writestr('session.json', json.dumps(dict(schema=1, session=session, segment=path.stem, created=stat.st_mtime)))
            data = output.getvalue()
            if len(data) > 128 * 1024: continue
            target = self.archives / (session + '-' + hashlib.sha256(data).hexdigest() + '.zip')
            if target.is_symlink(): raise ValueError('Redirected diagnostic archive')
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                try: os.utime(target, (stat.st_atime, stat.st_mtime))
                except OSError: pass
            if target.read_bytes() != data: raise ValueError('Diagnostic archive changed')
            path.unlink()
        # Only our own diagnostic archives are bounded; download folders are never cleaned here.
        total = 0
        files = sorted(self.archives.glob('*.zip'), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            if path.is_symlink() or not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{64}\\.zip', path.name): continue
            total += path.stat().st_size
            if total > 64 * 1024 * 1024 or path.stat().st_mtime < time.time() - 14 * 86400:
                self.write('diagnostics.retention', archive=path.name, uploaded=path.with_suffix('.sent').exists())
                path.unlink(); path.with_suffix('.sent').unlink(missing_ok=True)

    def upload(self, api):
        if _production: return
        for path in sorted(self.archives.glob('*.zip'), key=lambda p: p.stat().st_mtime, reverse=True):
            if self.stopped.is_set(): return
            if path.is_symlink() or not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{64}\\.zip', path.name): continue
            receipt = path.with_suffix('.sent')
            if receipt.exists(): continue
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if path.stem[33:] != digest: raise ValueError('Local diagnostic archive changed')
            value = dict(session=path.name[:32], sha256=digest, size=len(data))
            result = api.call('diagnostics', dict(value, action='status'))
            while True:
                offset = result.get('offset')
                if type(offset) is not int or not 0 <= offset <= len(data): raise ValueError('Invalid diagnostic offset')
                if result.get('complete') is True:
                    if offset != len(data) or result.get('sha256') != digest: raise ValueError('Invalid diagnostic receipt')
                    break
                if self.stopped.is_set() or _production: return
                if offset == len(data):
                    result = api.call('diagnostics', dict(value, action='finish'))
                    if result.get('complete') is not True: raise ValueError('Diagnostic archive was not committed')
                else:
                    result = api.call('diagnostics', dict(value, action='append', offset=offset,
                        data=base64.b64encode(data[offset:offset + 1024]).decode('ascii')))
                    if type(result.get('offset')) is not int or result['offset'] <= offset: raise ValueError('Diagnostic upload did not advance')
            if receipt.is_symlink(): raise ValueError('Redirected diagnostic receipt')
            receipt.write_text(json.dumps(dict(sha256=digest, server=api.url, time=time.time())), encoding='utf8')
            self.write('diagnostics.uploaded', archive=path.name, bytes=len(data))

    def start_uploads(self):
        if self.thread is not None or self.stopped.is_set():
            return
        def work():
            delay, retry = UPLOAD_INITIAL_DELAY_SECONDS, True
            while True:
                self.resumed.wait()
                if self.stopped.is_set(): return
                if not retry:
                    self.activity.wait()
                if self.stopped.wait(delay):
                    return
                self.activity.clear()
                if _production: continue
                try:
                    from ngs.api import MetadataApi
                    self.snapshot(); self.pack()
                    api = MetadataApi(self.storage, self.storage.read_settings()['tactic_url'])
                    _secrets.add(api.secret)
                    self.upload(api)
                    delay, retry = UPLOAD_BATCH_DELAY_SECONDS, False
                except Exception as error:
                    self.write('diagnostics.upload_error', error_type=type(error).__name__, error=str(error))
                    delay, retry = UPLOAD_RETRY_DELAY_SECONDS, True
        self.thread = threading.Thread(target=work, name='NGS session archives', daemon=True)
        self.thread.start()

    def finish(self):
        global _current
        self.stopped.set()
        self.resumed.set()
        self.activity.set()
        if self.thread is not None: self.thread.join(11)
        self.write('session.end')
        try:
            self.rotate()
        except OSError:
            # Diagnostics must not change the launcher's exit code on a full or
            # disconnected disk; the existing .active segment remains recoverable.
            pass
        finally:
            logging.getLogger().removeHandler(self)
            sys.excepthook, threading.excepthook = self.previous_hook, self.previous_thread_hook
            if _current is self: _current = None


def start(storage, mode='launcher'):
    global _current
    set_production(storage.read_settings().get('production_mode') is True)
    if _production: return None
    _current = SessionLog(storage, mode)
    try:
        path = storage.config / 'api-device.json'
        if not path.is_symlink() and path.stat().st_size <= 2048: remember_secret(json.loads(path.read_bytes()).get('secret'))
    except (OSError, ValueError): pass
    _current.setFormatter(RedactedFormatter('%(message)s'))
    logging.getLogger().addHandler(_current)
    logging.captureWarnings(True)
    sys.excepthook = _current.capture_exception
    threading.excepthook = lambda args, session=_current: session.capture_exception(args.exc_type, args.exc_value, args.exc_traceback)
    return _current


def set_production(enabled):
    global _production
    _production = enabled is True
    logging.disable(logging.CRITICAL if _production else logging.NOTSET)
    if _current is not None:
        if _production: _current.resumed.clear()
        else: _current.resumed.set()
        _current.activity.set()
    from ngs import crash_capture
    watchdog = crash_capture._state.get('watchdog')
    if watchdog is not None:
        if _production: watchdog.timer.stop()
        elif not watchdog.paused: watchdog.start()


def configure_production(storage, enabled):
    was_production = _production
    set_production(enabled)
    if was_production and not _production:
        from ngs import crash_capture
        if crash_capture._state.get('file') is None: crash_capture.enable(storage)
        if _current is None:
            session = start(storage)
            if session is not None: session.start_uploads()
