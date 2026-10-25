"""Temporary crash and hang evidence for device testing.

Collected locally under data/logs and summarised into the signed session log:

* breadcrumbs in native-crash.log survive a sudden native termination;
* a UI watchdog logs GUI thread stalls as ``ui.stall`` events;
* small Windows Error Reporting minidumps (enabled by the elevated NGS Input
  installer) are summarised without a debugger: exception code, faulting
  module + offset and a heuristic list of return addresses on the crashing
  thread stack;
* Application Error/Hang events for NGS processes from the Windows event log.

Nothing here changes application behaviour; every failure is swallowed.
"""
import json
import logging
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

NATIVE_LOG = 'native-crash.log'
NATIVE_LOG_LIMIT = 512 * 1024
NATIVE_REPORT_LIMIT = 24 * 1024
DUMP_IMAGES = ('NGS.exe', 'NGSInput.exe')
DUMP_KEEP = 3
DUMP_COPY_LIMIT = 64 * 1024 * 1024
WATCHDOG_TIMEOUT = 15
WATCHDOG_PERIOD_MS = 5_000
STALL_REPORT_SECONDS = 3.0

_lock = threading.Lock()
_state = dict(file=None, path=None, start_offset=0, watchdog=None)


def _release_version(storage):
    try:
        path = Path(storage.resources) / 'release.json'
        if path.is_file() and path.stat().st_size <= 4096:
            return str(json.loads(path.read_bytes()).get('version', ''))[:64]
    except (OSError, ValueError, AttributeError):
        pass
    return ''


def enable(storage, mode='launcher'):
    """Open a bounded, append-only breadcrumb log that survives native crashes."""
    from ngs.diagnostics import _production
    if _production: return None
    try:
        name = NATIVE_LOG if mode == 'launcher' else 'native-crash-%s.log' % re.sub('[^a-z0-9-]', '', mode)[:24]
        path = Path(storage.logs) / name
        if path.is_symlink():
            return None
        if path.is_file() and path.stat().st_size > NATIVE_LOG_LIMIT:
            os.replace(path, path.with_suffix('.log.1'))
        start = path.stat().st_size if path.is_file() else 0
        stream = open(path, 'a', encoding='utf-8', errors='replace')
        stream.write('=== %s start pid=%d mode=%s version=%s python=%s\n' % (
            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), os.getpid(), mode,
            _release_version(storage), sys.version.split()[0]))
        stream.flush()
        # Nuitka 4.1.1 recycles compiled frames without coordination with
        # faulthandler's Windows VEH / C timer thread; rely on out-of-process
        # WER LocalDumps + Windows Event Log + breadcrumbs instead.
        _state.update(file=stream, path=path, start_offset=start)
        return stream
    except Exception:
        logging.exception('Crash capture could not be enabled')
        return None


def note(text):
    """Append a timestamped breadcrumb to native-crash.log (survives a later native crash)."""
    from ngs.diagnostics import _production
    if _production: return
    stream = _state.get('file')
    if stream is None:
        return
    try:
        line = '%s %s\n' % (time.strftime('%H:%M:%S', time.gmtime()), str(text)[:300])
        with _lock:
            stream.write(line)
            stream.flush()
    except Exception:
        pass


def mark(event_name, **fields):
    """Session event plus a breadcrumb in native-crash.log (shown if the run ends abnormally)."""
    from ngs.diagnostics import _production
    if _production: return
    try:
        from ngs.diagnostics import event
        event(event_name, **fields)
    except Exception:
        pass
    note((event_name + ' ' + ' '.join('%s=%s' % item for item in fields.items()))[:300])


class _Watchdog:
    """Ticked from the GUI thread; records stalls when the event loop is blocked."""

    def __init__(self, parent):
        from PySide6.QtCore import QTimer
        self.timer = QTimer(parent)
        self.timer.setInterval(WATCHDOG_PERIOD_MS)
        self.timer.timeout.connect(self.kick)
        self.paused = False
        self.last = None

    def start(self):
        from ngs.diagnostics import _production
        if _production: return
        self.last = time.monotonic()
        self.timer.start()

    def kick(self):
        if self.paused:
            return
        now = time.monotonic()
        if self.last is not None:
            gap = now - self.last - WATCHDOG_PERIOD_MS / 1000
            if gap >= STALL_REPORT_SECONDS:
                from ngs.diagnostics import event
                event('ui.stall', seconds=round(gap, 1), dumped=gap >= WATCHDOG_TIMEOUT - WATCHDOG_PERIOD_MS / 1000)
                note('ui.stall %.1fs' % gap)
        self.last = now

    def set_paused(self, paused):
        paused = bool(paused)
        if paused == self.paused:
            return
        self.paused = paused
        if paused:
            self.timer.stop()
            note('watchdog paused (sleep)')
        else:
            note('watchdog resumed')
            self.start()


def start_watchdog(parent):
    if _state.get('file') is None or _state.get('watchdog') is not None:
        return None
    try:
        watchdog = _Watchdog(parent)
        watchdog.start()
        _state['watchdog'] = watchdog
        return watchdog
    except Exception:
        logging.exception('UI watchdog could not start')
        return None


def set_sleeping(sleeping):
    watchdog = _state.get('watchdog')
    if watchdog is not None:
        watchdog.set_paused(sleeping)


def stop():
    watchdog = _state.get('watchdog')
    if watchdog is not None:
        watchdog.timer.stop()


# --- Minidump summary -----------------------------------------------------

EXCEPTION_NAMES = {
    0xC0000005: 'ACCESS_VIOLATION', 0xC0000409: 'STACK_BUFFER_OVERRUN/FAIL_FAST', 0xC00000FD: 'STACK_OVERFLOW',
    0xC0000374: 'HEAP_CORRUPTION', 0xC000001D: 'ILLEGAL_INSTRUCTION', 0xC0000094: 'INT_DIVIDE_BY_ZERO',
    0x80000003: 'BREAKPOINT', 0xE06D7363: 'CPP_EXCEPTION', 0xC0000602: 'FAIL_FAST_EXCEPTION',
    0xC0000420: 'ASSERTION_FAILURE', 0xC000041D: 'FATAL_USER_CALLBACK_EXCEPTION', 0x40000015: 'ABORT (STATUS_FATAL_APP_EXIT)',
}


def _string(data, rva):
    if not 0 < rva <= len(data) - 4:
        return ''
    (length,) = struct.unpack_from('<I', data, rva)
    raw = data[rva + 4: rva + 4 + min(length, 1024)]
    return raw.decode('utf-16-le', 'replace')


def summarize_minidump(path, max_frames=32):
    """Return a small JSON-able summary of a Windows minidump (x64)."""
    with open(path, 'rb') as stream:
        data = stream.read(256 * 1024 * 1024)
    signature, version, count, directory = struct.unpack_from('<IIII', data, 0)
    if signature != 0x504D444D:
        raise ValueError('Not a minidump')
    (stamp,) = struct.unpack_from('<I', data, 20)
    streams = {}
    for index in range(min(count, 256)):
        kind, size, rva = struct.unpack_from('<III', data, directory + 12 * index)
        streams.setdefault(kind, (size, rva))
    result = dict(dump_time_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp)))
    modules = []
    if 4 in streams:
        size, rva = streams[4]
        (number,) = struct.unpack_from('<I', data, rva)
        for index in range(min(number, 1024)):
            base = rva + 4 + 108 * index
            image, image_size, unused_checksum, unused_stamp, name_rva = struct.unpack_from('<QIIII', data, base)
            ms, ls = struct.unpack_from('<II', data, base + 24 + 8)
            name = _string(data, name_rva)
            modules.append(dict(name=name.replace('/', '\\').rsplit('\\', 1)[-1], path=name, base=image, size=image_size,
                                version='%d.%d.%d.%d' % (ms >> 16, ms & 0xffff, ls >> 16, ls & 0xffff) if ms or ls else ''))

    def where(address):
        for module in modules:
            if module['base'] <= address < module['base'] + module['size']:
                return '%s+0x%x' % (module['name'], address - module['base'])
        return None

    if 7 in streams:
        size, rva = streams[7]
        arch, unused_level, unused_revision, cpus, unused_product, major, minor, build = struct.unpack_from('<HHHBBIII', data, rva)
        result['os'] = '%d.%d.%d' % (major, minor, build)
        result['cpus'] = cpus
    thread_id = None
    context = None
    if 6 in streams:
        size, rva = streams[6]
        thread_id, unused_align, code, flags, unused_record, address, params = struct.unpack_from('<IIIIQQI', data, rva)
        info = struct.unpack_from('<15Q', data, rva + 40)
        ctx_size, ctx_rva = struct.unpack_from('<II', data, rva + 40 + 120)
        context = (ctx_size, ctx_rva)
        result.update(exception_code='0x%08X' % code, exception_name=EXCEPTION_NAMES.get(code, ''),
                      exception_flags=flags, exception_address='0x%x' % address,
                      exception_module=where(address) or '?', thread_id=thread_id,
                      exception_parameters=['0x%x' % value for value in info[:min(params, 4)]])
        if code == 0xC0000005 and params >= 2:
            result['access'] = ('read', 'write', '?', '?', '?', '?', '?', '?', 'execute')[min(info[0], 8)] + ' 0x%x' % info[1]
    if thread_id is not None and 3 in streams and context is not None:
        size, rva = streams[3]
        (number,) = struct.unpack_from('<I', data, rva)
        for index in range(min(number, 4096)):
            entry = rva + 4 + 48 * index
            tid, = struct.unpack_from('<I', data, entry)
            if tid != thread_id:
                continue
            stack_start, stack_size, stack_rva = struct.unpack_from('<QII', data, entry + 24)
            ctx_size, ctx_rva = context
            frames = []
            if ctx_size >= 0x100 and ctx_rva + 0x100 <= len(data):
                rsp, = struct.unpack_from('<Q', data, ctx_rva + 0x98)
                rip, = struct.unpack_from('<Q', data, ctx_rva + 0xF8)
                result['rip'] = where(rip) or '0x%x' % rip
                begin = max(0, rsp - stack_start)
                stack = data[stack_rva + begin: stack_rva + stack_size]
                previous = None
                for offset in range(0, len(stack) - 7, 8):
                    value, = struct.unpack_from('<Q', stack, offset)
                    location = where(value)
                    # Heuristic: values inside loaded images; skip immediate duplicates.
                    if location and location != previous:
                        frames.append(location)
                        previous = location
                        if len(frames) >= max_frames:
                            break
            result['stack_candidates'] = frames
            break
    interesting = re.compile(r'(?i)^(ngs|ngsinput|python3\d*|qt6\w*|pyside6\w*|shiboken6\w*|d3d\w*|dxgi|nv\w*|amd\w*|atiu\w*|igd\w*|'
                             r'ucrtbase|vcruntime\w*|msvcp\w*|ntdll|kernelbase|kernel32|user32|win32u|combase|uiautomationcore|'
                             r'hid|xinput\w*|windows\.ui\.\w*|inputhost|textinputframework)\.(dll|exe|pyd)$')
    result['modules'] = ['%s %s' % (m['name'], m['version']) for m in modules if interesting.match(m['name'])][:60]
    result['module_count'] = len(modules)
    return result


# --- Collection at the next start ------------------------------------------

def _crash_state_path(storage):
    return Path(storage.logs) / 'crash-state.json'


def _load_state(storage):
    try:
        path = _crash_state_path(storage)
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 64 * 1024:
            value = json.loads(path.read_bytes())
            if isinstance(value, dict):
                return value
    except (OSError, ValueError):
        pass
    return {}


def _save_state(storage, value):
    try:
        path = _crash_state_path(storage)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        pass


def _native_sources(storage):
    path = _state.get('path')
    if path is not None:
        yield 'native_offset', path, _state.get('start_offset', 0), 'launcher'
    try:
        helper = Path(storage.root) / 'data' / 'input-host' / 'data' / 'logs' / NATIVE_LOG
        if helper.is_file() and not helper.is_symlink():
            yield 'input_native_offset', helper, helper.stat().st_size, 'input-host'
    except (OSError, AttributeError):
        pass


def _native_report(storage, state, write):
    for key, path, end, source in _native_sources(storage):
        begin = state.get(key, 0)
        if not isinstance(begin, int) or begin > end:
            begin = 0  # rotated
        if end <= begin:
            state[key] = end
            continue
        start = max(begin, end - NATIVE_REPORT_LIMIT)
        with open(path, 'rb') as stream:
            stream.seek(start)
            text = stream.read(end - start).decode('utf-8', 'replace')
        state[key] = end
        last_block = text.rsplit('=== ', 1)[-1]
        abnormal = source == 'launcher' and '=== ' in text and 'launcher exit' not in last_block
        if not abnormal and not re.search(r'Fatal Python error|Windows fatal exception|Timeout \(|Current thread|Thread 0x|ui\.stall', text):
            continue
        # Chunks stay below the session-log redact limit; header lines carry pid/version/time.
        for index in range(0, len(text), 8000):
            write('process.native_traceback', source=source, abnormal_end=abnormal, part=index // 8000, text=text[index:index + 8000])


def _dump_folders():
    folders = []
    local = os.environ.get('LOCALAPPDATA')
    if local:
        folders.append(Path(local) / 'CrashDumps')
    return folders


def _dump_report(storage, state, write):
    seen = set(state.get('dumps', []))
    found = []
    pattern = re.compile(r'^(%s)\.\d+\.dmp$' % '|'.join(re.escape(name) for name in DUMP_IMAGES), re.I)
    for folder in _dump_folders():
        try:
            for path in folder.iterdir():
                if pattern.match(path.name) and path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    key = '%s:%d:%d' % (path.name, int(stat.st_mtime), stat.st_size)
                    if key not in seen:
                        found.append((stat.st_mtime, key, path, stat.st_size))
        except OSError:
            continue
    found.sort(reverse=True)
    copies = Path(storage.logs) / 'crashdumps'
    for mtime, key, path, size in found[:DUMP_KEEP]:
        try:
            summary = summarize_minidump(path)
        except Exception as error:
            summary = dict(parse_error='%s: %s' % (type(error).__name__, error))
        write('process.crash_dump', file=path.name, bytes=size,
              modified_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(mtime)), **summary)
        if size <= DUMP_COPY_LIMIT:
            try:
                copies.mkdir(exist_ok=True)
                if not copies.is_symlink():
                    shutil.copy2(path, copies / path.name)
            except OSError:
                pass
    for mtime, key, path, size in found:
        seen.add(key)
    state['dumps'] = sorted(seen)[-64:]
    try:
        kept = sorted(copies.glob('*.dmp'), key=lambda p: p.stat().st_mtime, reverse=True) if copies.is_dir() else []
        for old in kept[DUMP_KEEP:]:
            old.unlink()
    except OSError:
        pass


def _windows_events(state, write):
    if sys.platform != 'win32':
        return
    query = ("*[System[(EventID=1000 or EventID=1001 or EventID=1002 or EventID=1026) and "
             "TimeCreated[timediff(@SystemTime) <= 259200000]]]")
    try:
        output = subprocess.run(['wevtutil', 'qe', 'Application', '/q:' + query, '/f:xml', '/c:200', '/rd:true'],
                                capture_output=True, timeout=20, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError):
        return
    text = output.stdout.decode('utf-8', 'replace')
    seen = set(state.get('events', []))
    reported = 0
    for block in re.findall(r'<Event .*?</Event>', text, re.S):
        if not re.search(r'(?i)\bNGS(Input)?\.exe\b', block):
            continue
        record = re.search(r'<EventRecordID>(\d+)</EventRecordID>', block)
        if not record or record.group(1) in seen:
            continue
        seen.add(record.group(1))
        identity = re.search(r'<EventID[^>]*>(\d+)</EventID>', block)
        created = re.search(r"SystemTime='([^']+)'", block)
        values = [re.sub(r'<[^>]+>', '', value).strip() for value in re.findall(r'<Data[^>]*>(.*?)</Data>', block, re.S)]
        write('process.windows_fault', event_id=int(identity.group(1)) if identity else None,
              time_utc=created.group(1) if created else None, data=[value[:300] for value in values[:14]])
        reported += 1
        if reported >= 10:
            break
    state['events'] = sorted(seen, key=int)[-128:]


def collect_previous(storage, write):
    """Summarise crash evidence left by previous runs. Runs on a background thread."""
    state = _load_state(storage)
    for step in (_native_report, _dump_report):
        try:
            step(storage, state, write)
        except Exception as error:
            write('process.crash_collect_error', step=step.__name__, error='%s: %s' % (type(error).__name__, error))
    try:
        _windows_events(state, write)
    except Exception as error:
        write('process.crash_collect_error', step='windows_events', error='%s: %s' % (type(error).__name__, error))
    _save_state(storage, state)
