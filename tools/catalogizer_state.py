"""Durable, incremental NAS discovery; publication always needs a separate confirmation."""

import hashlib
import json
import os
import threading
from pathlib import Path

from ngs.catalog import atomic_write, relative_game_path
from ngs.game_db import match_known_game
from ngs.install import Cancelled, check_cancel, safe_path
from ngs.metadata import search_title, METADATA_REVISION
from ngs.portable import path_key
from ngs.scanner import scan_executables
from tools.catalogizer import metadata, scan_folders
from tools.tactic_catalog import folder_key


class ScanJournal:
    def __init__(self, storage, source, url):
        self.source = str(Path(source))
        identity = os.path.normcase(self.source) + "\0" + url.rstrip("/") + "/ngs_launcher"
        key = hashlib.sha256(identity.encode("utf8")).hexdigest()[:24]
        self.path = safe_path(storage.state, "catalogizer-" + key + ".json")
        self.rows = {}
        self.lock = threading.RLock()
        if self.path.exists():
            if self.path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Журнал каталогизатора слишком большой. Исходный файл сохранён.")
            document = json.loads(self.path.read_bytes())
            if not isinstance(document, dict) or document.get("schema") != 1 or not isinstance(document.get("rows"), dict):
                raise ValueError("Повреждён журнал каталогизатора. Исходный файл сохранён.")
            for key, row in document["rows"].items():
                if not isinstance(row, dict) or key != folder_key(relative_game_path(row["folder"])) or row.get("status") not in (
                        "ready", "not_found", "published", "error", "new"):
                    raise ValueError("Некорректная запись журнала каталогизатора.")
            self.rows = document["rows"]
            for row in self.rows.values():
                row["source_root"] = self.source

    def save(self, row):
        with self.lock:
            row = dict(row, source_root=self.source)
            rows = dict(self.rows, **{folder_key(row["folder"]): row})
            atomic_write(self.path, json.dumps({"schema": 1, "rows": rows}, ensure_ascii=False).encode("utf8"))
            self.rows = rows


def scan_all(client, journal, records, cancel, emit, full=False, only=None, legacy_source=None):
    folders = [relative_game_path(only)] if only else scan_folders(journal.source, cancel)
    emit({"kind": "folders", "folders": folders, "replace": only is None, "source_root": journal.source})
    summary = dict(scanned=0, skipped=0, errors=0, total=len(folders))
    for index, folder in enumerate(folders):
        check_cancel(cancel)
        key = folder_key(folder)
        old = dict(journal.rows.get(key, {}), source_root=journal.source)
        bound = [r for r in records if folder_key(r["card"]["source_folder"]) == key
                 and path_key(r["card"].get("source_root") or legacy_source or journal.source) == path_key(journal.source)
                 and r.get("available_on_nas") and r.get("published", {}).get("manifest")]
        # Existing server imports bootstrap a new local journal, but never hide unfinished exports.
        if not full and not old.get("status") and len(bound) == 1:
            old = dict(folder=folder, status="published", game_id=bound[0]["card"]["id"],
                       name=bound[0]["card"]["name"], source_root=journal.source)
            journal.save(old)
        retry_metadata = (not full and old.get("status") in ("ready", "not_found")
                          and old.get("metadata_revision", 0) < METADATA_REVISION)
        if not full and old.get("status") in ("ready", "not_found", "published") and not old.get("pending_export") and not retry_metadata:
            summary["skipped"] += 1
            emit({"kind": "row", "row": old})
        else:
            emit({"kind": "progress", "done": index, "total": len(folders), "text": "Проверка: " + folder})
            try:
                query = old.get("query") or search_title(bound[0]["card"]["name"] if len(bound) == 1 else folder)
                matches = metadata(client, "search", query, source=journal.source, folder=folder)
                if retry_metadata and not matches and search_title(folder) != query:
                    query = search_title(folder)
                    matches = metadata(client, "search", query, source=journal.source, folder=folder)
                check_cancel(cancel)
                exes = (dict(candidates=old["executables"], limited=bool(old.get("warning")), skipped=0)
                        if retry_metadata and "executables" in old
                        else scan_executables(safe_path(Path(journal.source), folder), cancel, allow_network=True))
                check_cancel(cancel)
                candidates_list = list(exes["candidates"])
                if len(candidates_list) > 1:
                    matched_info = match_known_game(folder, candidates_list, source=journal.source)
                    best_exe = matched_info.get("executable")
                    if best_exe:
                        candidates_list.sort(key=lambda item: 0 if item["path"].casefold() == best_exe.casefold() else (2 if item.get("utility") else 1))
                row = dict(old, folder=folder, query=query, matches=matches, executables=candidates_list,
                           status="ready" if matches else "not_found", error="", metadata_revision=METADATA_REVISION,
                           warning="Поиск EXE неполный: есть ограничения или недоступные папки."
                                   if exes["limited"] or exes["skipped"] else "")
                summary["scanned"] += 1
            except Cancelled:
                raise
            except (ValueError, OSError, RuntimeError, KeyError) as error:
                row = dict(old, folder=folder, status="error", error=str(error))
                summary["errors"] += 1
            journal.save(row)
            emit({"kind": "row", "row": row})
        emit({"kind": "progress", "done": index + 1, "total": len(folders),
              "text": "Проверено %d из %d · пропущено %d · ошибок %d" % (
                  index + 1, len(folders), summary["skipped"], summary["errors"])})
    return summary
