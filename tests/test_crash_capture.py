"""Crash evidence: minidump summary, native traceback carry-over and watchdog pause."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ngs import crash_capture
from ngs.portable import PortableStorage


def synthetic_minidump(path, code=0xC0000005, base=0x7FF600000000, fault=0x1234, frames=(0x2000, 0x3000)):
    """Header, module list, exception, thread list, AMD64 context and stack memory."""
    blobs = bytearray()
    offset = 32 + 12 * 4  # header + four directory entries

    def add(data):
        nonlocal blobs
        rva = offset + len(blobs)
        blobs += data
        return rva

    name = 'C:\\NGS\\runtime\\NGS.exe'.encode('utf-16-le')
    name_rva = add(struct.pack('<I', len(name)) + name)
    module = struct.pack('<QIIII', base, 0x100000, 0, 0, name_rva)
    module += struct.pack('<13I', 0xFEEF04BD, 0, (2026 << 16) | 10, (21 << 16) | 1, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    module += b'\0' * (108 - len(module))
    modules_rva = add(struct.pack('<I', 1) + module)
    stack_start = 0x1000
    stack = struct.pack('<4Q', 0x55, base + frames[0], 0x77, base + frames[1])
    stack_rva = add(stack)
    context = bytearray(0x4D0)
    struct.pack_into('<Q', context, 0x98, stack_start)
    struct.pack_into('<Q', context, 0xF8, base + fault)
    context_rva = add(bytes(context))
    thread = struct.pack('<IIIIQQIIII', 7, 0, 0, 0, 0, stack_start, len(stack), stack_rva, len(context), context_rva)
    threads_rva = add(struct.pack('<I', 1) + thread)
    exception = struct.pack('<IIIIQQII', 7, 0, code, 0, 0, base + fault, 2, 0) + struct.pack('<15Q', 1, 0x10, *([0] * 13))
    exception += struct.pack('<II', len(context), context_rva)
    exception_rva = add(exception)
    system = struct.pack('<HHHBBIII', 9, 0, 0, 8, 1, 10, 0, 26100) + b'\0' * 32
    system_rva = add(system)
    header = struct.pack('<IIIIIIQ', 0x504D444D, 0xA793, 4, 32, 0, 1761076800, 0)
    directory = (struct.pack('<III', 4, 112, modules_rva) + struct.pack('<III', 6, len(exception), exception_rva)
                 + struct.pack('<III', 3, 52, threads_rva) + struct.pack('<III', 7, len(system), system_rva))
    Path(path).write_bytes(header + directory + bytes(blobs))


class CrashCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.storage = PortableStorage(self.tmp.name)
        self.addCleanup(crash_capture._state.update, file=None, path=None, start_offset=0, watchdog=None)

    def tearDown(self):
        stream = crash_capture._state.get('file')
        if stream is not None:
            stream.close()

    def test_minidump_summary_names_exception_module_and_stack(self):
        path = Path(self.tmp.name) / 'NGS.exe.42.dmp'
        synthetic_minidump(path)
        summary = crash_capture.summarize_minidump(path)
        self.assertEqual(summary['exception_code'], '0xC0000005')
        self.assertEqual(summary['exception_name'], 'ACCESS_VIOLATION')
        self.assertEqual(summary['exception_module'], 'NGS.exe+0x1234')
        self.assertEqual(summary['access'], 'write 0x10')
        self.assertEqual(summary['stack_candidates'], ['NGS.exe+0x2000', 'NGS.exe+0x3000'])
        self.assertEqual(summary['os'], '10.0.26100')
        self.assertIn('NGS.exe 2026.10.21.1', summary['modules'])

    def test_previous_fatal_traceback_and_new_dump_are_reported_once(self):
        logs = Path(self.storage.logs)
        (logs / 'native-crash.log').write_text(
            '=== 2026-10-21T12:00:00Z start pid=5 mode=launcher version=x python=3.13\n'
            '12:00:01 game.started game_id=a\nWindows fatal exception: access violation\n\nThread 0x01 (most recent call first):\n',
            encoding='utf-8')
        crash_capture.enable(self.storage)
        dumps = Path(self.tmp.name) / 'local' / 'CrashDumps'
        dumps.mkdir(parents=True)
        synthetic_minidump(dumps / 'NGS.exe.99.dmp')
        (dumps / 'other.exe.1.dmp').write_bytes(b'ignored')
        events = []
        with patch.dict(os.environ, LOCALAPPDATA=str(dumps.parent)), patch.object(crash_capture, '_windows_events', lambda *a: None):
            crash_capture.collect_previous(self.storage, lambda name, **fields: events.append((name, fields)))
            names = [name for name, fields in events]
            self.assertIn('process.native_traceback', names)
            native = dict(events[names.index('process.native_traceback')][1])
            self.assertTrue(native['abnormal_end'])
            self.assertIn('Windows fatal exception', native['text'])
            dump = dict(events[names.index('process.crash_dump')][1])
            self.assertEqual(dump['file'], 'NGS.exe.99.dmp')
            self.assertEqual(dump['exception_module'], 'NGS.exe+0x1234')
            self.assertTrue((logs / 'crashdumps' / 'NGS.exe.99.dmp').is_file())
            events.clear()
            crash_capture.collect_previous(self.storage, lambda name, **fields: events.append((name, fields)))
            self.assertEqual(events, [])
        state = json.loads((logs / 'crash-state.json').read_text(encoding='utf-8'))
        self.assertEqual(len(state['dumps']), 1)

    def test_clean_previous_exit_is_not_reported(self):
        logs = Path(self.storage.logs)
        (logs / 'native-crash.log').write_text(
            '=== 2026-10-21T12:00:00Z start pid=5 mode=launcher version=x python=3.13\n12:00:09 launcher exit\n', encoding='utf-8')
        crash_capture.enable(self.storage)
        events = []
        with patch.dict(os.environ, LOCALAPPDATA=str(Path(self.tmp.name) / 'none')), patch.object(crash_capture, '_windows_events', lambda *a: None):
            crash_capture.collect_previous(self.storage, lambda name, **fields: events.append((name, fields)))
        self.assertEqual(events, [])

    def test_watchdog_pauses_during_sleep_and_reports_stalls(self):
        from PySide6.QtCore import QCoreApplication, QObject
        app = QCoreApplication.instance() or QCoreApplication([])
        crash_capture.enable(self.storage)
        parent = QObject()
        watchdog = crash_capture.start_watchdog(parent)
        self.assertIsNotNone(watchdog)
        self.assertTrue(watchdog.timer.isActive())
        crash_capture.set_sleeping(True)
        self.assertFalse(watchdog.timer.isActive())
        crash_capture.set_sleeping(False)
        self.assertTrue(watchdog.timer.isActive())
        events = []
        with patch('ngs.diagnostics.event', lambda name, **fields: events.append((name, fields))):
            watchdog.last -= 30
            watchdog.kick()
        self.assertEqual(events[0][0], 'ui.stall')
        self.assertGreaterEqual(events[0][1]['seconds'], 19)
        crash_capture.stop()
        del app


if __name__ == '__main__':
    unittest.main()
