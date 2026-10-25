import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ngs.play_time import PlayTime


class PlayTimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.now, self.wall = 0.0, 1800000000
        self.path = Path(self.folder.name) / 'library.sqlite'
        self.tracker = PlayTime(self.path, lambda: self.now, lambda: self.wall + int(self.now))

    def tearDown(self): self.folder.cleanup()

    def test_local_reset_preserves_other_games_and_raw_sync_history_across_restart(self):
        tracker = self.tracker
        tracker.start('other'); self.now = 10; tracker.checkpoint(reason='completed')
        tracker.start('target'); self.now += 20; tracker.checkpoint(reason='completed')
        tracker.start('target'); self.now += 12
        session = tracker.snapshot()['session_id']
        tracker.reset(session)
        state = tracker.snapshot()
        self.assertEqual([state[k] for k in ('session_seconds','total_seconds','launches','paused_seconds','longest_seconds')], [0]*5)
        self.assertTrue(state['local_only'])
        self.assertEqual(state['session_id'], session)
        self.now += 7; tracker.checkpoint(reason='completed')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT active_ms FROM play_sessions WHERE id=?', (session,)).fetchone()[0], 19000)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM play_sessions WHERE game_id='target'").fetchone()[0], 2)
        again = PlayTime(self.path, lambda: self.now, lambda: self.wall + int(self.now))
        again.start('target'); self.now += 3
        state = again.snapshot()
        self.assertEqual((state['session_seconds'], state['total_seconds'], state['launches'], state['longest_seconds']), (3,10,1,7))
        self.assertTrue(state['local_only'])
        again.checkpoint(reason='completed'); again.start('other')
        self.assertEqual((again.snapshot()['total_seconds'], again.snapshot()['launches']), (10,2))
        self.assertFalse(again.snapshot()['local_only'])

    def test_reset_while_paused_repeats_and_failed_or_stale_reset_does_not_change_totals(self):
        tracker = self.tracker
        tracker.start('target'); self.now = 11; tracker.pause(True); self.now += 90
        session = tracker.snapshot()['session_id']
        with self.assertRaises(ValueError): tracker.reset('old-session')
        with patch('ngs.play_time.sqlite3.connect', side_effect=sqlite3.OperationalError('disk busy')):
            with self.assertRaises(sqlite3.Error): tracker.reset(session)
        self.assertEqual(tracker.snapshot()['total_seconds'], 11)
        self.assertFalse(tracker.snapshot()['local_only'])
        tracker.reset(session); self.now += 60
        self.assertEqual((tracker.snapshot()['session_seconds'], tracker.snapshot()['paused_seconds']), (0,60))
        self.assertTrue(tracker.session['paused'])
        tracker.pause(False); self.now += 4
        tracker.reset(session); self.now += 2
        self.assertEqual((tracker.snapshot()['total_seconds'], tracker.snapshot()['launches']), (2,0))
        tracker.checkpoint(reason='completed')
        with self.assertRaises(ValueError): tracker.reset(session)

    def test_multiple_sessions_exclude_freeze_sleep_and_keep_counts_after_restart(self):
        tracker = self.tracker
        tracker.start('steam-10'); self.now = 12.25
        tracker.pause(True); self.now += 3600
        tracker.pause(False); self.now += 7.75
        state = tracker.snapshot()
        self.assertEqual((state['session_seconds'], state['total_seconds'], state['paused_seconds'], state['launches']), (20,20,3600,1))
        tracker.checkpoint(reason='completed')
        tracker = PlayTime(self.path, lambda: self.now, lambda: self.wall + int(self.now))
        tracker.start('steam-10'); self.now += 5
        state = tracker.snapshot()
        self.assertEqual((state['session_seconds'],state['total_seconds'],state['longest_seconds'],state['launches']), (5,25,20,2))
        tracker.checkpoint(reason='forced')
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT active_ms, paused_ms, end_reason FROM play_sessions ORDER BY started_at').fetchall()
        self.assertEqual(rows, [(20000,3600000,'completed'),(5000,0,'forced')])

    def test_crash_recovers_only_last_checkpoint_without_counting_launcher_downtime(self):
        self.tracker.start('local-test'); self.now = 29
        self.tracker.checkpoint()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT active_ms FROM play_sessions').fetchone()[0],0)
        self.now = 31; self.tracker.checkpoint()
        self.now = 90000
        other = PlayTime(self.path, lambda: self.now, lambda: self.wall + int(self.now))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT active_ms, ended_at=updated_at, end_reason FROM play_sessions').fetchone(), (31000,1,'interrupted'))
        other.start('local-test')
        self.assertEqual((other.snapshot()['total_seconds'],other.snapshot()['launches']), (31,2))

    def test_failed_final_write_can_retry_without_counting_time_after_exit(self):
        self.tracker.start('test'); self.now = 11
        with patch('ngs.play_time.sqlite3.connect', side_effect=sqlite3.OperationalError('disk busy')):
            with self.assertRaises(sqlite3.Error): self.tracker.checkpoint(reason='completed')
        self.now = 999
        self.assertEqual(self.tracker.snapshot()['session_seconds'],11)
        self.tracker.start('test')
        self.assertEqual((self.tracker.snapshot()['total_seconds'],self.tracker.snapshot()['launches']), (11,2))

    def test_new_session_is_atomic_and_ids_do_not_mix_games(self):
        with patch('ngs.play_time.sqlite3.connect', side_effect=sqlite3.OperationalError('disk busy')):
            with self.assertRaises(sqlite3.Error): self.tracker.start('one')
        self.assertIsNone(self.tracker.session)
        self.tracker.start('one'); self.now = 3; self.tracker.checkpoint(reason='launcher_closed')
        self.tracker.start('two')
        self.assertEqual((self.tracker.snapshot()['launches'],self.tracker.snapshot()['total_seconds']), (1,0))

    def test_sleep_pause_does_not_wait_for_a_blocked_database_checkpoint(self):
        self.tracker.start('test'); self.now = 31
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        connect = sqlite3.connect
        errors = []
        def blocked(*args, **kwargs):
            entered.set(); release.wait(2)
            return connect(*args, **kwargs)
        def save():
            try: self.tracker.checkpoint()
            except Exception as error: errors.append(error)
        with patch('ngs.play_time.sqlite3.connect',side_effect=blocked):
            writer = threading.Thread(target=save); writer.start()
            self.assertTrue(entered.wait(1))
            pause = threading.Thread(target=lambda: (self.tracker.pause(True), paused.set()))
            pause.start()
            try: self.assertTrue(paused.wait(.2), 'Sleep callback waited for SQLite')
            finally: release.set(); writer.join(2); pause.join(2)
        self.assertFalse(errors)
        self.now = 999
        self.assertEqual(self.tracker.snapshot()['session_seconds'],31)

