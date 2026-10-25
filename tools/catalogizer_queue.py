"""Confirmed publication snapshots; SQLite keeps queued artwork off the UI heap."""

import hashlib
import json
import sqlite3
from contextlib import closing

from ngs.catalog import relative_game_path
from ngs.install import game_id, safe_path
from ngs.portable import path_key, path_list
from ngs.tactic import TacticClient
from tools.tactic_catalog import folder_key


STATES = {"queued", "running", "done", "error"}


class PublicationQueue:
    def __init__(self, storage):
        self.path = safe_path(storage.state, "catalogizer-publications.sqlite")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS publications (position INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "identity TEXT UNIQUE NOT NULL, job TEXT NOT NULL, prepared TEXT NOT NULL)")
        for job in self.jobs():
            if job["state"] == "running":
                job.update(state="error" if job["pending_export"] else "queued",
                           error="Запись могла завершиться. Перепроверьте карточку и подтвердите её заново."
                                 if job["pending_export"] else "")
                self.save(job)

    def jobs(self):
        with closing(sqlite3.connect(self.path)) as db:
            result = []
            for position, raw in db.execute("SELECT position, job FROM publications ORDER BY position"):
                job = json.loads(raw)
                if (len(raw) > 1024 * 1024 or not isinstance(job, dict) or job.get("schema") != 1
                        or job.get("state") not in STATES):
                    raise ValueError("Повреждена очередь публикаций. Исходная база сохранена.")
                result.append(dict(job, position=position))
            return result

    def prepared(self, position):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT prepared FROM publications WHERE position=?", (position,)).fetchone()
        if not row or not row[0] or len(row[0]) > 32 * 1024 * 1024:
            raise ValueError("Не найдены подтверждённые данные публикации.")
        prepared = json.loads(row[0])
        if not isinstance(prepared, dict) or not all(k in prepared for k in ("proposed", "merged", "details", "current", "edits")):
            raise ValueError("Повреждены подтверждённые данные публикации.")
        return prepared

    def save(self, job):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE publications SET job=? WHERE position=?",
                       (json.dumps(job, ensure_ascii=False), job["position"]))
            if job["state"] == "done" and not job.get("journal_pending"):
                db.execute("UPDATE publications SET prepared='' WHERE position=?", (job["position"],))

    def add(self, source, url, prepared, full_hash=False, pending_export=False):
        source = path_list([source], "NAS", True)[0]
        url = url.rstrip("/")
        TacticClient(url, "ngs_launcher")
        card = prepared["proposed"]
        folder = relative_game_path(card["source_folder"])
        relative_game_path(card["executable"])
        identity = hashlib.sha256((url + "\0" + path_key(source) + "\0" + folder_key(folder)).encode("utf8")).hexdigest()
        snapshot = json.dumps(prepared, ensure_ascii=False)
        if len(snapshot.encode("utf8")) > 32 * 1024 * 1024:
            raise ValueError("Данные публикации слишком большие.")
        job = dict(schema=1, state="queued", source=source, url=url, folder=folder,
                   name=prepared["edits"].get("name", prepared["merged"]["name"]),
                   game_id=game_id(card["id"]), provider_id=prepared["details"]["provider_id"],
                   full_hash=bool(full_hash), pending_export=bool(pending_export), error="")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for position, old_identity, raw in db.execute("SELECT position, identity, job FROM publications"):
                old = json.loads(raw)
                if old["state"] in ("queued", "running") and old["url"] == url and (
                        old_identity == identity or old["game_id"] == job["game_id"] or old["provider_id"] == job["provider_id"]):
                    raise ValueError("Эта папка или игра уже в очереди публикации.")
                if old_identity == identity:
                    job["pending_export"] |= old["pending_export"]
                    db.execute("DELETE FROM publications WHERE position=?", (position,))
            position = db.execute("INSERT INTO publications (identity,job,prepared) VALUES (?,?,?)",
                                  (identity, json.dumps(job, ensure_ascii=False), snapshot)).lastrowid
        return dict(job, position=position)

    def remove(self, job):
        if job["state"] == "running" or job["pending_export"]:
            raise ValueError("Начатую запись нужно завершить через повторную проверку карточки.")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM publications WHERE position=?", (job["position"],))
