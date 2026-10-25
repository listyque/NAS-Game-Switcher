"""Portable session history; frozen/sleep time never counts as play time."""
import sqlite3
import threading
import time
import uuid
from contextlib import closing

from ngs.install import game_id
from ngs.diagnostics import event


class PlayTime:
    def __init__(self, path, clock=time.monotonic, wall=time.time):
        self.path, self.clock, self.wall = path, clock, wall
        self.lock = threading.RLock()
        self.session = None
        self.reset_info = None
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS play_sessions (id TEXT PRIMARY KEY, game_id TEXT NOT NULL, "
                       "started_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, ended_at INTEGER, "
                       "active_ms INTEGER NOT NULL, paused_ms INTEGER NOT NULL, end_reason TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS play_sessions_game ON play_sessions(game_id)")
            db.execute("CREATE TABLE IF NOT EXISTS play_resets (game_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                       "active_ms INTEGER NOT NULL, paused_ms INTEGER NOT NULL, started_at INTEGER NOT NULL)")
            # Repair only impossible dates produced by older wall-clock-based checkpoints.
            # Session IDs, start dates and monotonic counters stay unchanged for server deduplication.
            db.execute("UPDATE play_sessions SET "
                       "updated_at=MAX(started_at,updated_at,COALESCE(ended_at,started_at),started_at+(active_ms+paused_ms+999)/1000), "
                       "ended_at=CASE WHEN ended_at IS NULL THEN NULL ELSE MAX(ended_at,started_at+(active_ms+paused_ms+999)/1000) END "
                       "WHERE updated_at<started_at OR ended_at<started_at OR ended_at>updated_at "
                       "OR active_ms+paused_ms>(updated_at-started_at+300)*1000")
            # A lost launcher cannot know how long its detached game kept running.
            db.execute("UPDATE play_sessions SET ended_at=updated_at, end_reason='interrupted' WHERE ended_at IS NULL")

    def start(self, identity):
        if self.session is not None and self.session.get('end_reason'):
            self.checkpoint(force=True)
        with self.lock:
            if self.session is not None:
                raise ValueError("The previous game session has not ended yet.")
        identity, now = game_id(identity), int(self.wall())
        session = dict(id=uuid.uuid4().hex, game_id=identity, started_at=now, updated_at=now,
                       active_ms=0, paused_ms=0, paused=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            reset = db.execute("SELECT r.session_id,r.active_ms,r.paused_ms,r.started_at,p.rowid,p.active_ms "
                               "FROM play_resets r JOIN play_sessions p ON p.id=r.session_id "
                               "WHERE r.game_id=? AND p.game_id=r.game_id", (identity,)).fetchone()
            previous = db.execute("SELECT COUNT(*), COALESCE(SUM(active_ms),0), COALESCE(MAX(active_ms),0) "
                                  "FROM play_sessions WHERE game_id=? AND rowid>?", (identity, reset[4] if reset else 0)).fetchone()
            if reset:
                elapsed = max(0, reset[5] - reset[1])
                previous = (previous[0], previous[1] + elapsed, max(previous[2], elapsed))
            db.execute("INSERT INTO play_sessions VALUES (?, ?, ?, ?, NULL, 0, 0, '')",
                       (session['id'], identity, now, now))
        with self.lock:
            self.previous = previous
            self.reset_info = reset[:4] if reset else None
            self.session = session
            self.last = self.saved = self.clock()
            self.fraction = 0.0
            event('play.start', game_id=identity, session_id=session['id'])

    def advance(self):
        if self.session.get('end_reason'): return
        now = self.clock()
        delta = max(0, now - self.last) * 1000 + self.fraction
        elapsed = int(delta)
        self.fraction = delta - elapsed
        self.session['paused_ms' if self.session['paused'] else 'active_ms'] += elapsed
        self.last = now

    def pause(self, paused):
        with self.lock:
            if self.session is None: return
            self.advance()
            if self.session['paused'] != paused:
                self.session['paused'] = paused
                event('play.pause' if paused else 'play.resume', game_id=self.session['game_id'],
                      session_id=self.session['id'])

    def checkpoint(self, force=False, reason=None):
        with self.lock:
            if self.session is None: return
            self.advance()
            elapsed = (self.session['active_ms'] + self.session['paused_ms'] + 999) // 1000
            # NTP/user clock corrections must not create end < start or an impossible duration.
            now = max(int(self.wall()), self.session['updated_at'], self.session['started_at'] + elapsed)
            self.session['updated_at'] = now
            if reason is not None and not self.session.get('end_reason'):
                self.session.update(end_reason=reason, end_at=now)
            reason = self.session.get('end_reason')
            if not force and reason is None and self.clock() - self.saved < 30: return
            row, stamp = dict(self.session), self.clock()
        # Disk waits must never hold the counter lock needed by the native sleep callback.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE play_sessions SET updated_at=?, ended_at=?, active_ms=?, paused_ms=?, end_reason=? WHERE id=?",
                       (now, row.get('end_at'), row['active_ms'], row['paused_ms'], reason or '', row['id']))
        with self.lock:
            self.saved = stamp
            if reason is not None:
                event('play.end', game_id=row['game_id'], session_id=row['id'], active_ms=row['active_ms'],
                      paused_ms=row['paused_ms'], reason=reason)
                self.session = None

    def reset(self, session_id):
        with self.lock:
            if self.session is None or self.session['id'] != session_id or self.session.get('end_reason'):
                raise ValueError("The game session changed. Open statistics again.")
            self.advance()
            row = dict(self.session)
            reset = (session_id, row['active_ms'], row['paused_ms'], int(self.wall()))
        # Keep raw sessions and acknowledged counters monotonic for profile synchronization.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO play_resets VALUES (?,?,?,?,?)", (row['game_id'], *reset))
        with self.lock:
            self.reset_info = reset
            self.previous = (0, 0, 0)
        event('play.reset_local', game_id=row['game_id'], session_id=session_id)

    def snapshot(self):
        with self.lock:
            if self.session is None:
                return dict(session_id='', session_seconds=0, total_seconds=0, launches=0, paused_seconds=0, longest_seconds=0, started_at=0)
            self.advance()
            count, total, longest = self.previous
            row = self.session
            reset = self.reset_info if self.reset_info and self.reset_info[0] == row['id'] else None
            active = max(0, row['active_ms'] - (reset[1] if reset else 0))
            paused = max(0, row['paused_ms'] - (reset[2] if reset else 0))
            return dict(session_id=row['id'], session_seconds=active // 1000, total_seconds=(total + active) // 1000,
                        launches=count + (0 if reset else 1), paused_seconds=paused // 1000,
                        longest_seconds=max(longest, active) // 1000, started_at=reset[3] if reset else row['started_at'],
                        local_only=self.reset_info is not None)
