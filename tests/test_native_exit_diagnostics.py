import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ngs.diagnostics import SessionLog
from ngs.portable import PortableStorage


class NativeExitDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage = PortableStorage(self.temporary.name)

    def rows(self, log):
        return [json.loads(row) for row in log.active.read_bytes().splitlines()]

    def test_previous_native_exit_is_recorded_without_requiring_python_exception(self):
        saved = dict(time_utc='2026-10-21T09:34:37Z', pid=6472, exit_code=0xc0000005,
                     exit_hex='0xC0000005', elapsed_ms=4092)
        native = self.storage.logs / 'process-exit.jsonl'
        native.write_text(json.dumps(saved) + '\n')
        before = native.read_bytes()
        log = SessionLog(self.storage)
        self.addCleanup(log.finish)
        row = next(row for row in self.rows(log) if row['event'] == 'process.previous_exit')
        self.assertEqual(row['exit_code'], 0xc0000005)
        self.assertEqual(row['runtime_pid'], 6472)
        self.assertEqual(row['elapsed_ms'], 4092)
        self.assertEqual(native.read_bytes(), before)

    def test_invalid_or_extra_fields_are_not_copied_into_diagnostics(self):
        native = self.storage.logs / 'process-exit.jsonl'
        row = dict(time_utc='2026-10-21T09:34:37Z', pid=6472, exit_code=0,
                   exit_hex='0x00000000', elapsed_ms=4092)
        native.write_text('broken\n' + json.dumps(dict(row, extra='private unrelated data')) + '\n'
                          + json.dumps(dict(row, exit_code=True)) + '\n' + json.dumps(row) + '\n')
        log = SessionLog(self.storage)
        self.addCleanup(log.finish)
        captured = [value for value in self.rows(log) if value['event'] == 'process.previous_exit']
        self.assertEqual(len(captured), 1)
        self.assertNotIn('private unrelated data', log.active.read_text())

    def test_large_native_log_does_not_grow_session_without_bound(self):
        (self.storage.logs / 'process-exit.jsonl').write_bytes(b'x' * (67 * 1024))
        log = SessionLog(self.storage)
        self.addCleanup(log.finish)
        self.assertEqual([row['event'] for row in self.rows(log)],
                         ['session.start', 'process.exit_snapshot_error'])
        self.assertLess(log.active.stat().st_size, 2048)

    def test_transfer_worker_does_not_repeat_native_exit_snapshot(self):
        (self.storage.logs / 'process-exit.jsonl').write_bytes(b'x' * (67 * 1024))
        log = SessionLog(self.storage, 'transfer')
        self.addCleanup(log.finish)
        self.assertEqual([row['event'] for row in self.rows(log)], ['session.start'])

    def test_worker_activity_notifies_uploader_without_writing_another_event(self):
        from ngs import diagnostics
        log = SessionLog(self.storage)
        self.addCleanup(log.finish)
        before = log.active.read_bytes()
        log.activity.clear()
        with patch.object(diagnostics, '_current', log):
            diagnostics.note_activity()
        self.assertTrue(log.activity.is_set())
        self.assertEqual(log.active.read_bytes(), before)

    def test_diagnostic_rotation_failure_does_not_replace_exit_or_leak_hooks(self):
        previous, previous_thread = sys.excepthook, threading.excepthook
        log = SessionLog(self.storage)
        logging.getLogger().addHandler(log)
        with patch('ngs.diagnostics.os.replace', side_effect=OSError('disk unavailable')):
            log.finish()
        self.assertIs(sys.excepthook, previous)
        self.assertIs(threading.excepthook, previous_thread)
        self.assertNotIn(log, logging.getLogger().handlers)

    def test_in_flight_thread_exception_keeps_its_original_session(self):
        from ngs import diagnostics
        log = diagnostics.start(self.storage)
        self.addCleanup(log.finish)
        handler = threading.excepthook
        with patch.object(diagnostics, '_current', None):
            handler(SimpleNamespace(exc_type=RuntimeError, exc_value=RuntimeError('late worker'), exc_traceback=None))
        rows = [row for row in self.rows(log) if row['event'] == 'exception']
        self.assertEqual(len(rows), 1)
        self.assertIn('late worker', rows[0]['traceback'])

    def test_idle_uploader_does_not_schedule_itself_from_its_own_bookkeeping(self):
        log = SessionLog(self.storage)
        completed = threading.Event()
        calls = []
        def upload(api):
            calls.append('upload')
            log.write('diagnostics.uploaded', archive='fixture')
            completed.set()
        with patch('ngs.diagnostics.UPLOAD_INITIAL_DELAY_SECONDS', 0), \
                patch('ngs.diagnostics.UPLOAD_BATCH_DELAY_SECONDS', 0), \
                patch('ngs.api.MetadataApi', return_value=SimpleNamespace(secret='fixture-device-key')), \
                patch.object(log, 'snapshot'), patch.object(log, 'pack'), patch.object(log, 'upload', upload):
            try:
                log.start_uploads()
                self.assertTrue(completed.wait(2))
                threading.Event().wait(.05)
                self.assertEqual(len(calls), 1)
                completed.clear()
                log.write('queue.changed', state='paused')
                self.assertTrue(completed.wait(2))
                threading.Event().wait(.05)
                self.assertEqual(len(calls), 2)
            finally:
                log.finish()
        self.assertFalse(log.thread.is_alive())

    def test_failed_upload_still_retries_without_another_user_event(self):
        log = SessionLog(self.storage)
        completed = threading.Event()
        attempts = []
        def upload(api):
            attempts.append('upload')
            if len(attempts) == 1:
                raise ConnectionError('temporary fixture failure')
            completed.set()
        with patch('ngs.diagnostics.UPLOAD_INITIAL_DELAY_SECONDS', 0), \
                patch('ngs.diagnostics.UPLOAD_RETRY_DELAY_SECONDS', 0), \
                patch('ngs.api.MetadataApi', return_value=SimpleNamespace(secret='fixture-device-key')), \
                patch.object(log, 'snapshot'), patch.object(log, 'pack'), patch.object(log, 'upload', upload):
            try:
                log.start_uploads()
                self.assertTrue(completed.wait(2))
                self.assertEqual(len(attempts), 2)
            finally:
                log.finish()
        self.assertFalse(log.thread.is_alive())

    def test_user_event_during_an_upload_is_kept_for_the_next_batch(self):
        log = SessionLog(self.storage)
        upload_started = threading.Event()
        release_first_upload = threading.Event()
        second_uploaded = threading.Event()
        calls = []
        def upload(api):
            calls.append('upload')
            if len(calls) == 1:
                upload_started.set()
                if not release_first_upload.wait(2):
                    raise RuntimeError('Test did not release its first upload')
            else:
                second_uploaded.set()
            log.write('diagnostics.uploaded', archive='fixture')
        with patch('ngs.diagnostics.UPLOAD_INITIAL_DELAY_SECONDS', 0), \
                patch('ngs.diagnostics.UPLOAD_BATCH_DELAY_SECONDS', 0), \
                patch('ngs.api.MetadataApi', return_value=SimpleNamespace(secret='fixture-device-key')), \
                patch.object(log, 'snapshot'), patch.object(log, 'pack'), patch.object(log, 'upload', upload):
            try:
                log.start_uploads()
                self.assertTrue(upload_started.wait(2))
                log.write('queue.changed', state='downloaded')
                release_first_upload.set()
                self.assertTrue(second_uploaded.wait(2))
                threading.Event().wait(.05)
                self.assertEqual(len(calls), 2)
            finally:
                release_first_upload.set()
                log.finish()
        self.assertFalse(log.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
