"""Launcher UI state for the server-side catalogizer, using only the signed API."""
from ngs.i18n import tr
import hashlib
import base64
import json
import logging
from copy import deepcopy

from PySide6.QtCore import QObject, Property, QThread, QTimer, Signal, Slot, QBuffer, QByteArray, QIODevice, QUrl, QSize, Qt
from PySide6.QtGui import QImageReader

from ngs.api import MetadataApi
from ngs.catalog import atomic_write
from ngs.install import safe_path
from ngs.remote_catalog import validate_catalog_request


class CatalogRequest(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, storage, url, value, parent):
        super().__init__(parent)
        self.storage, self.url, self.value = storage, url, value

    def run(self):
        try:
            result = self.fetch(MetadataApi(self.storage, self.url))
            artwork = result.get("artwork", {}) if isinstance(result, dict) else {}
            if not isinstance(artwork, dict) or set(artwork) - {"cover", "background"}:
                raise ValueError(tr("Invalid artwork preview"))
            for context, source in artwork.items():
                prefix = "data:image/jpeg;base64,"
                if not isinstance(source, str) or not source.startswith(prefix) or len(source) > len(prefix) + 16 * 1024:
                    raise ValueError(tr("Invalid artwork preview"))
                raw = base64.b64decode(source[len(prefix):], validate=True)
                buffer = QBuffer(); buffer.setData(QByteArray(raw)); buffer.open(QIODevice.ReadOnly)
                reader = QImageReader(buffer)
                size = reader.size()
                if (reader.format() != b"jpeg" or not size.isValid() or size.width() > (240 if context == "cover" else 640)
                        or size.height() > 360 or reader.read().isNull()):
                    raise ValueError(tr("Invalid preview image"))
            self.loaded.emit(result)
        except Exception as error:
            logging.exception("Remote catalogizer failed")
            self.failed.emit(str(error))

    def fetch(self, api): return api.call('catalog', self.value)


def custom_artwork(source, kind):
    url = QUrl(source)
    if not url.isLocalFile(): raise ValueError(tr("Choose an image on this device."))
    from pathlib import Path
    path = Path(url.toLocalFile())
    if not 0 < path.stat().st_size <= 8*1024*1024: raise ValueError(tr("The image must not exceed 8 MiB."))
    reader = QImageReader(str(path)); size = reader.size()
    if reader.format() not in (b'jpeg', b'png', b'webp') or not size.isValid() or max(size.width(), size.height()) > 8192:
        raise ValueError(tr("Choose a PNG, JPEG or WebP image no larger than 8192 pixels."))
    bounds = QSize(600, 900) if kind == 'cover' else QSize(1280, 720)
    reader.setAutoTransform(True); reader.setScaledSize(size.scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull(): raise ValueError(tr("Could not read the image."))
    limit = 48*1024 if kind == 'cover' else 96*1024
    while True:
        for quality in (90, 80, 70, 60, 50):
            stream = QBuffer(); stream.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(stream, 'JPEG', quality): raise ValueError(tr("Could not prepare the image."))
            raw = bytes(stream.data())
            if len(raw) <= limit: return raw
        image = image.scaled(max(1, image.width()*3//4), max(1, image.height()*3//4),
                             Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


class CustomCardRequest(CatalogRequest):
    progress = Signal(int, int)

    def fetch(self, api):
        preview, edits, images, installers = self.value
        items = [('metadata', json.dumps(edits, ensure_ascii=False).encode('utf8'))]
        items += [(kind, custom_artwork(source, kind)) for kind, source in images.items() if source]
        result = None
        total = sum(len(raw) for kind, raw in items); done = 0
        for kind, raw in items:
            digest = hashlib.sha256(raw).hexdigest()
            for offset in range(0, len(raw), 1280):
                if self.isInterruptionRequested(): raise InterruptedError(tr("Saving artwork stopped."))
                value = dict(action='installer_custom' if installers else 'custom', preview=preview, kind=kind,
                    offset=offset, total=len(raw), sha256=digest, data=base64.b64encode(raw[offset:offset+1280]).decode('ascii'))
                validate_catalog_request(value)
                result = api.call('catalog', value)
                self.progress.emit(done + min(len(raw), offset + 1280), total)
            done += len(raw)
        if not result or result.get('complete') is not True: raise ValueError(tr("The server did not confirm the artwork."))
        return result


class RemoteCatalogBridge(QObject):
    changed = Signal()
    customSaved = Signal()

    def __init__(self, storage, published, parent, installers=False):
        super().__init__(parent)
        self.storage, self.published = storage, published
        self.installers = installers
        self.worker, self.url, self.pending = None, "", None
        self.request_failed = False
        self.folder_view, self.exe_cache, self.completed = None, {}, set()
        self.navigation_views = {}
        self._state = dict(stage="folders", rows=[], busy=False, status="", folder="", executable="", query="",
                           preview="", card={}, offset=0, total=0, done=0, state="", hide_published=True,
                           result_offset=0, result_total=-1, artwork={}, manual=False)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(5000)
        self.timer.timeout.connect(self.poll)
        self.closing = False

    @Property("QVariantMap", notify=changed)
    def state(self): return dict(self._state)

    @Property(bool, constant=True)
    def installerMode(self): return self.installers

    @Slot(str)
    def openServer(self, url):
        if self.worker is not None: return
        if url == self.url and self.pending is not None:
            if self._state["stage"] in ("working", "batch"): self.poll()
            else: self.changed.emit()
            return
        self.folder_view, self.exe_cache, self.completed = None, {}, set()
        self.navigation_views.clear()
        self.url = url
        digest = hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:20]
        self.pending = safe_path(self.storage.state, ("installer-catalogizer-" if self.installers else "server-catalogizer-") + digest + ".json")
        try:
            if self.pending.is_file():
                if self.pending.stat().st_size > 2048: raise ValueError("Invalid saved catalogizer job")
                saved = json.loads(self.pending.read_bytes())
                if saved.get("schema") != 1 or saved.get("server") != url: raise ValueError("Catalogizer server changed")
                value = validate_catalog_request(dict(action="status", preview=saved["preview"]))
                self._state.update(preview=value["preview"], stage="working")
                self.request(value); return
            self.folders(0)
        except (ValueError, OSError, KeyError) as error: self.failed(str(error))

    def request(self, value):
        if self.worker is not None or self.closing: return
        try: validate_catalog_request(value)
        except ValueError as error: self.failed(str(error)); return
        self.timer.stop()
        self.request_failed = False
        if value["action"] in ("folders", "exes", "search", "preview"):
            self._state["artwork"] = {}
        self._state.update(busy=True, status=tr("The server is processing the request…"))
        if self.installers:
            value = dict(value, action="installer_" + value["action"])
        self.worker = CatalogRequest(self.storage, self.url, value, self)
        self.worker.loaded.connect(self.loaded); self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.finished)
        self.changed.emit(); self.worker.start()

    @Slot(int)
    def folders(self, offset=0):
        if self.worker is not None: return
        self.folder_view, self.exe_cache, self.completed = None, {}, set()
        self.navigation_views.clear()
        self._state.update(result_total=-1, folder="", executable="")
        self.request(dict(action="folders", offset=max(0, offset), hide_published=self._state["hide_published"]))

    @Slot()
    def backToFolders(self):
        if self.worker is not None: return
        self.navigation_views.clear()
        if self.folder_view is None: self.folders(0); return
        hidden = self._state['hide_published']
        self._state = deepcopy(self.folder_view)
        self.folder_view = None
        self._state.update(hide_published=hidden, busy=False)
        self._state['status'] = tr("List saved. Choose another game.")
        self.markCompleted()
        self.changed.emit()

    @Slot(result=bool)
    def back(self):
        if self.worker is not None or self._state['busy']: return True
        stage = self._state['stage']
        if stage in ('working', 'batch') and self._state['state'] not in ('error', 'cancelled', 'done'):
            self._state['status'] = tr("Games are still being added. Choose Stop adding games to stop, or Close to hide this window.")
            self.changed.emit()
            return True
        target = {'review': 'matches', 'matches': 'exes'}.get(stage)
        if target in self.navigation_views:
            hidden = self._state['hide_published']
            self._state = deepcopy(self.navigation_views[target])
            self._state.update(busy=False, hide_published=hidden)
            self.changed.emit()
        elif stage == 'folders': return False
        else: self.backToFolders()
        return True

    @Slot(bool)
    def setHidePublished(self, hidden):
        if self.worker is not None: return
        self._state["hide_published"] = hidden
        self.changed.emit()
        if self._state["stage"] == "folders": self.folders(0)
        elif self._state["stage"] == "done" and self._state["result_total"] >= 0: self.results(0)

    @Slot(int)
    def results(self, offset=0):
        self.request(dict(action="results", preview=self._state["preview"], offset=max(0, offset), hide_published=self._state["hide_published"]))

    @Slot()
    def batch(self):
        if self.worker is not None: return
        self.folder_view, self.exe_cache, self.completed = None, {}, set()
        self.navigation_views.clear()
        self.request(dict(action="batch"))

    @Slot(str)
    def reviewFolder(self, folder):
        if self.worker is not None: return
        self.navigation_views.clear()
        if self._state['stage'] == 'folders' or (self._state['stage'] == 'done' and self._state['result_total'] >= 0):
            self.folder_view = deepcopy(self._state)
            rows = [row for row in self._state['rows'] if not self._state['hide_published'] or not row.get('published')]
            self.folder_view['selected_index'] = next((i for i, row in enumerate(rows) if row['folder'] == folder), 0)
        self._state.update(folder=folder, query=folder, executable="", preview="", card={}, state="")
        self._state["result_total"] = -1
        if folder in self.exe_cache: self.loaded(deepcopy(self.exe_cache[folder]))
        else: self.request(dict(action="exes", folder=folder))

    @Slot(int)
    def choose(self, index):
        rows = self._state["rows"]
        if self.worker is not None or not 0 <= index < len(rows): return
        row, stage = rows[index], self._state["stage"]
        self._state['selected_index'] = index
        if stage == "folders":
            self.reviewFolder(row["folder"])
        elif stage == "exes":
            self._state.update(executable=row["path"])
            self.search(self._state["query"][:160])
        elif stage == "matches": self.preview(row["app_id"])

    @Slot(str)
    def search(self, query):
        if self._state['stage'] == 'exes': self.navigation_views['exes'] = deepcopy(self._state)
        self._state.update(query=query)
        self.request(dict(action="search", query=query))

    @Slot(str)
    def preview(self, provider):
        if self._state['stage'] == 'matches': self.navigation_views['matches'] = deepcopy(self._state)
        self._state['manual'] = provider == 'manual'
        self.request(dict(action="preview", folder=self._state["folder"], executable=self._state["executable"], provider=provider))

    @Slot(str, str, str, str)
    def saveCustomCard(self, name, description, cover, background):
        if self.worker is not None or self._state['stage'] != 'review' or not self._state.get('manual'): return
        if (not name.strip() or len(name) > 160 or len(description) > 3000
                or any(ord(c) < 32 and c != '\n' for c in name + description)):
            self.failed(tr("Enter a title of up to 160 characters and a description of up to 3,000 characters.")); return
        self._state.update(busy=True, status=tr("Saving custom artwork…"))
        self.worker = CustomCardRequest(self.storage, self.url,
            (self._state['preview'], dict(name=name.strip(), description=description), dict(cover=cover, background=background), self.installers), self)
        self.worker.loaded.connect(self.loaded); self.worker.loaded.connect(self.customSaved)
        self.worker.progress.connect(self.customProgress)
        self.worker.failed.connect(self.failed); self.worker.finished.connect(self.finished)
        self.changed.emit(); self.worker.start()

    @Slot(int, int)
    def customProgress(self, done, total):
        self._state['status'] = tr("Saving artwork: %d%% · %d / %d KiB") % (done*100//max(1,total), (done+1023)//1024, (total+1023)//1024)
        self.changed.emit()

    @Slot()
    def publish(self):
        if self.worker is not None: return
        try:
            value = validate_catalog_request(dict(action="publish", preview=self._state["preview"]))
            atomic_write(self.pending, json.dumps(dict(schema=1, server=self.url, preview=value["preview"])).encode())
            self.request(value)
        except (ValueError, OSError) as error: self.failed(str(error))

    @Slot()
    def cancel(self): self.request(dict(action="cancel", preview=self._state["preview"]))

    @Slot()
    def poll(self):
        if self._state["preview"]: self.request(dict(action="status", preview=self._state["preview"]))

    @Slot(object)
    def loaded(self, result):
        if self.closing: return
        if not isinstance(result, dict) or result.get("stage") not in ("folders", "exes", "matches", "review", "working", "batch", "done"):
            self.failed(tr("Invalid NGS catalog manager response")); return
        just_finished = self._state["stage"] != "done"
        if self._state['stage'] != result['stage']: self._state['selected_index'] = 0
        if result['stage'] == 'exes': self.exe_cache[result['folder']] = deepcopy(result)
        self._state.update(result)
        self._state["selected_id"] = result.get("selected_id") or result.get("card", {}).get("id", "")
        stage, state = result["stage"], result.get("state", "")
        self._state["artwork"] = result.get("artwork", {}) if stage == "review" else {}
        statuses = dict(folders=tr("Choose the game folder on the server."), exes=tr("Confirm the game executable; utility files are marked."),
                        matches=tr("Choose artwork or add the game without it."), review=tr("Review the game details and confirm publishing."),
                        working=tr("Publishing game details. Game files are not being packaged now."),
                        batch=tr("Adding unambiguous matches: %d of %d. Others remain for manual review.") % (result.get("done", 0), result.get("total", 0)),
                        done=tr("Done. Catalog updated; packages will be prepared when downloading."))
        self._state["status"] = statuses[stage]
        if self.installers and result.get("connected") is False:
            self._state["status"] = tr("The installer source folder is not configured on the server.")
        if state == "export": self._state["status"] = tr("Publishing the catalog without packaging game files…")
        if state == "activate": self._state["status"] = tr("Applying the published catalog…")
        if state == "error": self._state["status"] = tr("Preparation incomplete: ") + result.get("error", tr("Error"))
        if result.get("cancelled") or state == "cancelled": self._state["status"] = tr("Preparation stopped. Original games preserved.")
        if stage == "batch" and self.pending:
            atomic_write(self.pending, json.dumps(dict(schema=1, server=self.url, preview=result["preview"])).encode())
        if stage == "done" and result.get("summary"):
            self._state["status"] = tr("Added: %d · already in catalog: %d · manual review: %d · errors: %d") % tuple(
                result["summary"][status] for status in ("added", "already", "manual", "error"))
        if stage == "done" and state in ("done", "cancelled"):
            self.pending.unlink(missing_ok=True)
            if state == "done" and just_finished:
                if self._state['folder'] and 'summary' not in result:
                    folder, executable = self._state['folder'], self._state['executable']
                    self.completed.add(folder)
                    for row in self.exe_cache.get(folder, {}).get('rows', []):
                        if row['path'] == executable: row.update(published=True, status=tr("Added"))
                self.published()
        self.markCompleted()
        self.changed.emit()

    def markCompleted(self):
        if not (self._state['stage'] == 'folders' or self._state['stage'] == 'done' and self._state['result_total'] >= 0): return
        for row in self._state['rows']:
            if row.get('folder') in self.completed:
                row['status'] = tr("Added") if not self.installers else tr("Package added; you can choose another installer")
                if not self.installers: row.update(state='added', published=True)

    @Slot(str)
    def failed(self, message):
        self.request_failed = True
        self._state["status"] = message
        self.changed.emit()

    @Slot()
    def finished(self):
        self.worker.deleteLater(); self.worker = None
        self._state["busy"] = False; self.changed.emit()
        if self._state["stage"] in ("working", "batch") and not self.closing and not self.request_failed:
            self.timer.setInterval(5000 if self._state.get("state") == "working" else 500)
            self.timer.start()

    def stop(self):
        self.closing = True; self.timer.stop()
        if self.worker is not None:
            self.worker.requestInterruption()
            if not self.worker.wait(1500):
                self.worker.setParent(None)
                from ngs.players import _LINGERING_WORKERS
                _LINGERING_WORKERS.append(self.worker)
