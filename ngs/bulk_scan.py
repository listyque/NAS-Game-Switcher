"""Import a local collection automatically; keep uncertain games for review."""

from ngs.i18n import tr
import json
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path

from ngs.game_db import fetch_remote_known_games, match_known_game, select_best_executable
from ngs.install import Cancelled, LocalLibrary, check_cancel, safe_path
from ngs.metadata import client_metadata, search_title
from ngs.portable import path_key
from ngs.scanner import browse_folders, local_folder, scan_executables


def title_key(value):
    value = value.replace("™", "").replace("®", "").replace("©", "")
    value = unicodedata.normalize("NFKD", search_title(value).casefold())
    return "".join(c for c in value if c.isalnum() and not unicodedata.combining(c))


class LocalScanJournal:
    def __init__(self, storage, url):
        self.path = safe_path(storage.state, "local-scans.sqlite")
        self.server = url.rstrip("/")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS scans (server TEXT, folder TEXT, root TEXT, row TEXT, "
                       "PRIMARY KEY(server, folder))")

    def rows(self, root):
        with closing(sqlite3.connect(self.path)) as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT row FROM scans WHERE server=? AND root=? ORDER BY folder", (self.server, path_key(root)))]

    def save(self, root, row):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO scans VALUES (?, ?, ?, ?) ON CONFLICT(server, folder) "
                       "DO UPDATE SET root=excluded.root, row=excluded.row",
                       (self.server, path_key(row["path"]), path_key(root), json.dumps(row, ensure_ascii=False)))


def metadata_requests(storage, url, cancel, emit, *, offline=False):
    recent = []
    def metadata(operation, value, label=""):
        nonlocal offline
        # Cached offline reads do not consume the server's request budget.
        if offline:
            check_cancel(cancel)
            return client_metadata(storage, url, operation, value, cancel, offline=True,
                                   progress=lambda phase, done, total, message: emit(dict(status=(label + " · " if label else "") + message)))
        # Respect the existing device limits: 60 requests / 20 resolves per minute.
        for attempt in range(2):
            while True:
                check_cancel(cancel)
                now = time.monotonic()
                recent[:] = [(stamp, op) for stamp, op in recent if now - stamp < 61]
                if len(recent) < 55 and (operation != "resolve" or sum(op == "resolve" for _, op in recent) < 18):
                    break
                emit(dict(status=tr("Waiting for the API rate limit; games already added are available in the library.")))
                cancel.wait(.5)
            recent.append((time.monotonic(), operation))
            try:
                return client_metadata(storage, url, operation, value, cancel,
                                       progress=lambda phase, done, total, message: emit(dict(status=(label + " · " if label else "") + message)),
                                       **({"offline": True} if offline else {}))
            except ConnectionError:
                offline = True
                raise
            except RuntimeError as error:
                if attempt or tr("too many requests") not in str(error).casefold():
                    raise
                emit(dict(status=tr("API rate limit: search resumes in a minute.")))
                if cancel.wait(61):
                    check_cancel(cancel)
        raise RuntimeError(tr("The API is temporarily unavailable."))
    return metadata


def scan_collection(storage, root, cards, url, cancel, emit, *, offline=False):
    root = local_folder(root)
    if root == Path(root.anchor):
        raise ValueError(tr("Choose your games folder rather than an entire drive."))
    journal, library = LocalScanJournal(storage, url), LocalLibrary(storage)
    installed = library.reconcile()
    locations = {path_key(library.install_path(receipt)) for receipt in installed}
    folders = [folder for folder in browse_folders(root, cancel)["folders"]
               if folder["name"].casefold() not in (".ngs-installers", ".ngs-downloads")]
    summary = dict(added=0, already=0, manual=0, error=0, total=len(folders))
    metadata = metadata_requests(storage, url, cancel, emit, offline=offline)
    extra_known = []
    extra_file = safe_path(storage.cache, "known-games-extra.json")
    custom_known_url = storage.read_settings().get("known_games_url", "").strip()
    try:
        if extra_file.is_file() and extra_file.stat().st_size <= 4 * 1024 * 1024:
            doc = json.loads(extra_file.read_bytes())
            extra_known = doc.get("games", []) if isinstance(doc, dict) else []
            if not offline and custom_known_url and time.time() - doc.get("synced_at", 0) > 3600:
                remote_entries = fetch_remote_known_games(custom_known_url)
                if remote_entries:
                    by_n = {(g.get("n") or "").casefold(): g for g in extra_known if isinstance(g, dict)}
                    for item in remote_entries:
                        by_n[item["n"].casefold()] = item
                    extra_known = list(by_n.values())[:5000]
                    from ngs.catalog import atomic_write
                    atomic_write(extra_file, json.dumps({"schema": 1, "synced_at": time.time(), "games": extra_known}, ensure_ascii=False).encode("utf-8"))
        elif not offline and custom_known_url:
            remote_entries = fetch_remote_known_games(custom_known_url)
            from ngs.catalog import atomic_write
            extra_known = remote_entries[:5000]
            atomic_write(extra_file, json.dumps({"schema": 1, "synced_at": time.time(), "games": extra_known}, ensure_ascii=False).encode("utf-8"))
    except Exception:
        pass
    for card_item in cards:
        if card_item.get("kind") != "installer" and card_item.get("name") and card_item.get("executable"):
            extra_known.append({"n": card_item["name"], "e": [card_item["executable"]], "a": [Path(card_item.get("source_folder", "")).name] if card_item.get("source_folder") else []})
    for index, folder in enumerate(folders):
        check_cancel(cancel)
        path = Path(folder["path"])
        row = dict(path=str(path), name=folder["name"], state="pending", status=tr("Searching for executables…"), candidates=[])
        emit(dict(row=row, done=index, total=len(folders), status=tr("Checking: ") + row["name"]))
        try:
            if path_key(path) in locations:
                row.update(state="already", status=tr("Already in My games"))
            elif path == storage.root or storage.root.is_relative_to(path):
                row.update(state="manual", status=tr("NGS folder skipped"))
            else:
                found = scan_executables(path, cancel)
                candidates = [item for item in found["candidates"] if not item["utility"]]
                matched_info = match_known_game(row["name"], found["candidates"], extra_entries=extra_known, source=root)
                best_exe_path = matched_info.get("executable") if matched_info.get("matched") or len(candidates) <= 1 else select_best_executable(row["name"], found["candidates"])
                if best_exe_path:
                    found["candidates"].sort(key=lambda item: 0 if item["path"].casefold() == best_exe_path.casefold() else (2 if item.get("utility") else 1))
                row["candidates"] = found["candidates"]
                query_keys = {title_key(row["name"])}
                if matched_info.get("matched") and matched_info.get("name"):
                    query_keys.add(title_key(matched_info["name"]))
                matched = [card for card in cards if query_keys & {
                    title_key(card["name"]), title_key(Path(card.get("source_folder", "")).name)}]
                card = matched[0] if len(matched) == 1 else None
                preferred = [item for item in candidates if card and item["path"].casefold() == card.get("executable", "").casefold()]
                executable = (preferred[0] if len(preferred) == 1 else
                              next((item for item in candidates if best_exe_path and item["path"].casefold() == best_exe_path.casefold()), None) or
                              (candidates[0] if len(candidates) == 1 else None))
                if found["limited"] or found["skipped"] or not executable:
                    row.update(state="manual", status=tr("Choose an executable manually") if candidates else tr("Review the folder and executable"))
                elif len(matched) > 1:
                    row.update(state="manual", status=tr("Multiple matches: choose a game"))
                else:
                    if card is None:
                        prov = matched_info.get("provider_id") or ""
                        if prov and prov != "manual":
                            try:
                                emit(dict(status=tr("Saving artwork: ") + (matched_info.get("name") or row["name"])))
                                resolve_val = prov[6:] if prov.startswith("steam:") else prov
                                resolved = metadata("resolve", resolve_val, row["name"])
                                if isinstance(resolved, dict) and isinstance(resolved.get("card"), dict):
                                    card = resolved["card"]
                            except Exception:
                                card = None
                        if card is None:
                            search_q = (matched_info["name"] if matched_info.get("matched") and matched_info.get("name") else search_title(row["name"]))
                            emit(dict(status=tr("Finding artwork: ") + search_q))
                            matches = metadata("search", search_q, row["name"])
                            exact = [match for match in matches if title_key(match["name"]) in query_keys]
                            if not exact and len(matches) == 1 and matched_info.get("matched"):
                                exact = matches
                            if len(exact) == 1:
                                emit(dict(status=tr("Saving artwork: ") + exact[0]["name"]))
                                card = metadata("resolve", exact[0]["app_id"], row["name"])["card"]
                            else:
                                row.update(state="manual", status=tr("Confirm the match manually") if matches else tr("No artwork found"))
                    if card:
                        receipt = library.register_external(path, executable["path"], card["name"], card, cancel)
                        locations.add(path_key(path))
                        row.update(state="added", status=tr("Added to My games", "Добавлена в «Мои игры»"), name=receipt["card"]["name"], candidates=[], game_id=receipt["card"]["id"])
        except Cancelled:
            journal.save(root, row)
            raise
        except ConnectionError as error:
            offline = True
            row.update(state="manual", status=str(error))
        except (ValueError, OSError, RuntimeError, KeyError) as error:
            row.update(state="error", status=str(error))
        journal.save(root, row)
        summary[row["state"]] += 1
        emit(dict(row=row, done=index + 1, total=len(folders), status=tr("Added %d · already present %d · manual %d · errors %d") % (
            summary["added"], summary["already"], summary["manual"], summary["error"])))
    return dict(summary=summary, rows=journal.rows(root), root=str(root))
