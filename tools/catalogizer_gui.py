"""Windowed administration tool. No game executable is ever launched here."""

import base64
import hashlib
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QLockFile, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPixmap, QImageReader
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTextEdit, QPlainTextEdit,
    QVBoxLayout, QWidget, QTabWidget)

from ngs.install import Cancelled, check_cancel, safe_path
from ngs.scanner import scan_executables
from ngs.tactic import TacticClient
from ngs.portable import configured_paths, path_list, path_key
from ngs.metadata import METADATA_REVISION
from tools.catalogizer import metadata, prepare_import, publish_folder
from tools.catalogizer_state import ScanJournal, scan_all
from tools.catalogizer_queue import PublicationQueue
from tools.tactic_catalog import folder_key, invoke
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password
from tools.http_packages import publish_existing, server_source


STATUS = {"new": "Не проверена", "ready": "Нужно подтверждение", "not_found": "Уточните название",
          "published": "В TACTIC", "error": "Ошибка — повторить"}
PUBLICATION_STATUS = {"queued": "Queued", "running": "Публикуется", "done": "Опубликована", "error": "Ошибка публикации"}
CREDENTIALS = DEFAULT_CREDENTIALS


def row_key(row):
    return path_key(row["source_root"]) + "\0" + folder_key(row["folder"])


class Worker(QThread):
    event = Signal(object)
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation
        self.cancel = threading.Event()

    def run(self):
        try:
            self.result.emit(self.operation(self.cancel, self.event.emit))
        except Cancelled:
            self.failed.emit("Остановлено. Завершённые папки сохранены; можно продолжить сканирование.")
        except Exception as error:
            self.failed.emit(str(error))


class CatalogizerWindow(QMainWindow):
    def __init__(self, storage):
        super().__init__()
        self.storage, self.worker, self.client = storage, None, None
        self.rows, self.table_indices, self.details, self.prepared = {}, {}, None, None
        self.preview_records, self.preview_edits = [], {}
        self.record_cache = None
        self.active_folder, self.closing = "", False
        self.active_source = ""
        self.publication_queue = PublicationQueue(storage)
        self.publication_worker, self.publication_active = None, None
        self.publication_jobs = self.publication_queue.jobs()
        self.publication_paused = any(j["state"] == "queued" for j in self.publication_jobs)
        self.setWindowTitle("NGS · Каталогизатор")
        self.resize(1360, 900)
        self.setMinimumSize(1000, 680)
        host = QWidget()
        self.setCentralWidget(host)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Библиотека на NAS")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(QLabel("Все папки → поиск игр и EXE → проверка → публикация в TACTIC"))
        settings = QFormLayout()
        source_row = QHBoxLayout()
        config = storage.read_settings()
        self.connected_url, self.login_password = config["tactic_url"].rstrip("/"), ""
        self.configured_sources = configured_paths(config, "games_sources", "games_source", "")
        self.legacy_source = config.get("legacy_games_source", config["games_source"])
        self.source = QPlainTextEdit("\n".join(self.configured_sources))
        self.source.setFixedHeight(64)
        self.source.setPlaceholderText("По одной папке NAS на строку")
        browse = QPushButton("Добавить папку…")
        browse.clicked.connect(self.browse)
        source_row.addWidget(self.source, 1)
        source_row.addWidget(browse)
        self.url = QLineEdit(config["tactic_url"])
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Не сохраняется; пусто — доступ с этого административного ПК")
        settings.addRow("Папки игр · по одной на строку", source_row)
        settings.addRow("TACTIC · ngs_launcher", self.url)
        settings.addRow("Пароль администратора", self.password)
        layout.addLayout(settings)
        actions = QHBoxLayout()
        self.scan = QPushButton("Сканировать все папки")
        self.full = QPushButton("Полный скан")
        self.retry = QPushButton("Пересканировать папку")
        self.stop = QPushButton("Остановить")
        self.stop.setEnabled(False)
        self.scan.clicked.connect(lambda: self.start_scan())
        self.full.clicked.connect(lambda: self.start_scan(full=True))
        self.full.setToolTip("Повторить поиск игры и EXE для всех папок, включая обработанные. Карточки не удаляются.")
        self.retry.clicked.connect(lambda: self.start_scan(full=True, only=self.active_folder))
        self.stop.clicked.connect(self.cancel)
        for button in (self.scan, self.full, self.retry, self.stop):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(QLabel("Обычный скан пропускает сохранённые результаты. Полный заново проверяет каждую папку."))
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фильтр по папке, игре или статусу")
        self.filter.textChanged.connect(self.filter_rows)
        left_layout.addWidget(self.filter)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Папка NAS", "Status", "Игра / первый вариант", "Source"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.select_row)
        left_layout.addWidget(self.table)
        self.counts = QLabel()
        left_layout.addWidget(self.counts)
        self.tabs = QTabWidget()
        self.tabs.addTab(left, "Папки NAS")
        queue_page = QWidget()
        queue_layout = QVBoxLayout(queue_page)
        queue_layout.addWidget(QLabel("Подтвердите игру и EXE, добавьте в очередь и переходите к следующей папке."))
        self.queue_table = QTableWidget(0, 3)
        self.queue_table.setHorizontalHeaderLabels(["Game", "Status", "Папка NAS"])
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.queue_table.verticalHeader().hide()
        self.queue_table.itemSelectionChanged.connect(self.update_queue_actions)
        queue_layout.addWidget(self.queue_table)
        self.queue_message = QLabel("Ошибка публикации не останавливает остальные игры.")
        self.queue_message.setWordWrap(True)
        queue_layout.addWidget(self.queue_message)
        queue_actions = QHBoxLayout()
        self.queue_review = QPushButton("Перепроверить карточку")
        self.queue_remove = QPushButton("Убрать из очереди")
        self.queue_review.clicked.connect(self.review_publication)
        self.queue_remove.clicked.connect(self.remove_publication)
        queue_actions.addWidget(self.queue_review)
        queue_actions.addWidget(self.queue_remove)
        queue_layout.addLayout(queue_actions)
        self.tabs.addTab(queue_page, "Очередь публикации")
        splitter.addWidget(self.tabs)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        right = QWidget()
        detail_layout = QVBoxLayout(right)
        self.folder_label = QLabel("Выберите папку слева")
        self.folder_label.setWordWrap(True)
        detail_layout.addWidget(self.folder_label)
        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Название игры для поиска")
        self.search = QPushButton("Search")
        self.search.clicked.connect(self.search_game)
        self.query.returnPressed.connect(self.search_game)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.search)
        detail_layout.addLayout(search_row)
        self.matches = QComboBox()
        self.matches.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.matches.setMinimumContentsLength(20)
        self.matches.currentIndexChanged.connect(self.invalidate)
        detail_layout.addWidget(self.matches)
        detail_layout.addWidget(QLabel("EXE для запуска · можно ввести путь внутри папки игры"))
        self.exes = QComboBox()
        self.exes.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.exes.setMinimumContentsLength(20)
        self.exes.setEditable(True)
        self.exes.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.exes.currentTextChanged.connect(self.exe_changed)
        self.exes.lineEdit().setPlaceholderText("Выберите или найдите EXE игры")
        detail_layout.addWidget(self.exes)
        exe_row = QHBoxLayout()
        self.find_exes = QPushButton("Найти EXE")
        self.pick_exe = QPushButton("Выбрать EXE…")
        self.find_exes.clicked.connect(self.find_executables)
        self.pick_exe.clicked.connect(self.choose_executable)
        exe_row.addWidget(self.find_exes)
        exe_row.addWidget(self.pick_exe)
        exe_row.addStretch()
        detail_layout.addLayout(exe_row)
        self.load = QPushButton("Показать карточку выбранной игры")
        self.load.clicked.connect(self.load_game)
        detail_layout.addWidget(self.load)
        self.cover = QLabel("Превью появится после выбора игры")
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setWordWrap(True)
        self.cover.setFixedWidth(136)
        self.cover.setMinimumHeight(170)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Title")
        self.description = QTextEdit()
        self.description.setPlaceholderText("Описание игры")
        self.description.setMinimumHeight(140)
        preview = QHBoxLayout()
        preview.addWidget(self.cover)
        text = QVBoxLayout()
        text.addWidget(self.name)
        text.addWidget(self.description, 1)
        preview.addLayout(text, 1)
        detail_layout.addLayout(preview)
        self.note = QLabel()
        self.note.setWordWrap(True)
        detail_layout.addWidget(self.note)
        self.rehash = QCheckBox("Перечитать все файлы при публикации (SHA256)")
        self.rehash.setToolTip("Иначе используются сохранённые хэши файлов с прежними размером и временем изменения.")
        self.publish = QPushButton("Добавить в очередь публикации")
        self.publish.setObjectName("primary")
        self.publish.setToolTip("Нажатие подтверждает выбранную игру, EXE и внесённые правки.")
        self.name.textChanged.connect(self.update_actions)
        self.publish.clicked.connect(self.publish_game)
        self.packages = QPushButton("Подготовить HTTP-пакеты опубликованной игры")
        self.packages.clicked.connect(self.prepare_packages)
        scroll.setWidget(right)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.addWidget(scroll, 1)
        self.publish_hint = QLabel()
        self.publish_hint.setWordWrap(True)
        self.publish_hint.setStyleSheet("color: #e7bc80;")
        for field in (self.rehash, self.publish_hint, self.publish, self.packages):
            review_layout.addWidget(field)
        splitter.addWidget(review)
        splitter.setSizes([720, 540])
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.status = QLabel("Готова к сканированию. Изменения на сервере — только после подтверждения.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        publication_bar = QHBoxLayout()
        self.publication_status = QLabel("Очередь публикации пуста")
        self.publication_status.setWordWrap(True)
        self.queue_pause = QPushButton("Приостановить очередь")
        self.queue_pause.clicked.connect(self.toggle_publications)
        publication_bar.addWidget(self.publication_status, 1)
        publication_bar.addWidget(self.queue_pause)
        layout.addLayout(publication_bar)
        self.publication_progress = QProgressBar()
        self.publication_progress.setRange(0, 100)
        layout.addWidget(self.publication_progress)
        self.source.textChanged.connect(self.update_actions)
        self.url.textChanged.connect(self.update_actions)
        self.browse_button = browse
        self.journals = {path_key(p): ScanJournal(storage, p, self.url.text()) for p in self.configured_sources}
        for journal in self.journals.values():
            for row in journal.rows.values():
                self.upsert(row)
        for job in self.publication_jobs:
            if job.get("journal_pending"):
                self.apply_publication(job)
            elif job["pending_export"]:
                self.mark_publication_commit(job)
        self.refresh_publications()
        self.update_actions()

    @property
    def journal(self):
        return self.journals.get(path_key(self.active_source)) or next(iter(self.journals.values()))

    def source_paths(self):
        return path_list([p for p in self.source.toPlainText().splitlines() if p.strip()], "NAS", True)

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, "Папки игр на NAS", self.configured_sources[0])
        if path:
            self.source.appendPlainText(path)

    def same_source(self):
        try:
            return ([path_key(p) for p in self.source_paths()] == [path_key(p) for p in self.configured_sources]
                    and self.url.text().strip().rstrip("/") == self.connected_url)
        except ValueError:
            return False

    def update_actions(self, *args):
        busy = self.worker is not None
        same = hasattr(self, "journals") and self.same_source()
        queued = any(j["state"] in ("queued", "running") for j in self.publication_jobs)
        active_queued = any(j["state"] in ("queued", "running") and j["url"] == self.connected_url
                            and path_key(j["source"]) == path_key(self.active_source) and j["folder"] == self.active_folder
                            for j in self.publication_jobs)
        for field in (self.source, self.url, self.password, self.browse_button, self.scan, self.full):
            field.setEnabled(not busy and not queued)
        # A resumed queue on another PC may still need its password supplied in memory.
        self.password.setEnabled(not busy and self.publication_worker is None)
        self.table.setEnabled(not busy)
        for field in (self.query, self.search, self.matches, self.exes, self.find_exes, self.pick_exe, self.load, self.retry):
            field.setEnabled(not busy and same and not active_queued and bool(self.active_folder))
        self.retry.setEnabled(self.retry.isEnabled() and not queued)
        self.load.setEnabled(self.load.isEnabled() and bool(self.matches.currentData()))
        for field in (self.name, self.description, self.rehash):
            field.setEnabled(not busy and not active_queued and self.details is not None)
        self.publish.setEnabled(not busy and not active_queued and same and self.prepared is not None
                                and bool(self.prepared["proposed"]["executable"]) and bool(self.name.text().strip()))
        row = self.journal.rows.get(folder_key(self.active_folder), {})
        self.packages.setEnabled(not busy and not queued and same and server_source(self.active_source)
                                 and row.get("status") == "published" and bool(row.get("game_id")))
        self.rehash.setVisible(not server_source(self.active_source))
        reason = ("Игра уже в очереди публикации." if active_queued else
                  "Дождитесь завершения текущей операции." if busy else
                  "Сначала выберите игру и загрузите её карточку." if self.details is None else
                  "Не выбран EXE. Нажмите «Найти EXE» или «Выбрать EXE…»." if not self.selected_executable() else
                  "Проверьте путь EXE внутри папки игры." if self.prepared is None else
                  "Введите название игры." if not self.name.text().strip() else
                  "Сервер и NAS должны совпадать с загруженным каталогом.")
        self.publish_hint.setText(reason)
        self.publish_hint.setVisible(not self.publish.isEnabled())
        self.update_queue_actions()

    def launch(self, operation, success):
        if self.worker:
            return
        self.take_password()
        self.worker = Worker(operation, self)
        self.worker.event.connect(self.on_event)
        self.succeeded = False
        def complete(result):
            self.succeeded = True
            success(result)
        self.worker.result.connect(complete)
        self.worker.failed.connect(self.show_error)
        self.worker.finished.connect(self.finished)
        self.progress.setRange(0, 0)
        self.stop.setEnabled(True)
        self.update_actions()
        self.worker.start()

    def prepare_packages(self):
        row = self.journal.rows.get(folder_key(self.active_folder), {})
        identity = row.get("game_id")
        if not identity or not self.packages.isEnabled():
            return
        def operation(cancel, emit):
            client = self.connect_client()
            record = next((r for r in invoke(client, "inspect")["records"] if r["card"]["id"] == identity), None)
            if record is None:
                raise ValueError("Опубликованная игра не найдена. Обновите каталог.")
            def advanced(phase, done, total, text):
                emit(dict(kind="progress", done=done, total=total, text=text))
            return publish_existing(client, self.storage, record, cancel, advanced)
        def complete(result):
            self.record_cache = None
            self.status.setText("HTTP-пакеты готовы: %s · пакетов %d" % (result["card"]["name"], result["chunks"]))
        self.launch(operation, complete)

    def take_password(self):
        if self.password.text():
            self.login_password = self.password.text()
            self.password.clear()
            self.client = None

    def finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if self.succeeded else 0)
        self.stop.setEnabled(False)
        self.update_actions()
        if self.closing:
            QTimer.singleShot(0, self.close)
        else:
            self.start_publication()

    def cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.stop.setEnabled(False)
            self.status.setText("Останавливаю… Текущий сетевой запрос должен завершиться.")

    def show_error(self, message):
        row = self.journal.rows.get(folder_key(self.active_folder))
        if row and row.get("pending_export"):
            self.upsert(row)
            self.invalidate()
            message += " Загрузите карточку заново и повторите публикацию для завершения экспорта."
        self.status.setText(message)

    def connect_client(self):
        if self.client is None:
            self.client = TacticClient(self.connected_url, "ngs_launcher")
        if not self.client.ticket:
            self.client = admin_client(self.connected_url, CREDENTIALS, password=self.login_password)
        return self.client

    def start_scan(self, full=False, only=None):
        if self.worker or any(j["state"] in ("queued", "running") for j in self.publication_jobs) or (only is not None and not only):
            return
        try:
            sources, url = self.source_paths(), self.url.text().strip().rstrip("/")
            TacticClient(url, "ngs_launcher")
            changed = not self.same_source()
            if changed:
                self.journals = {path_key(p): ScanJournal(self.storage, p, url) for p in sources}
                self.configured_sources = sources
                self.client = None
            self.connected_url = url
            self.login_password = self.password.text()
            self.password.clear()
            settings = self.storage.read_settings()
            self.legacy_source = settings.get("legacy_games_source", settings["games_source"])
            settings.update(games_source=sources[0], games_sources=sources, legacy_games_source=self.legacy_source, tactic_url=url)
            self.storage.write_settings(settings)
            self.invalidate()
            targets = [self.journal] if only else list(self.journals.values())
            if not only:
                self.active_folder, self.active_source = "", ""
                self.table.setRowCount(0)
                self.rows, self.table_indices = {}, {}
            self.status.setText("Подключаюсь к TACTIC и читаю список папок…")
            def operation(cancel, emit):
                client = self.connect_client()
                records = invoke(client, "inspect")["records"]
                total = dict(scanned=0, skipped=0, errors=0, total=0, source_errors=[])
                for journal in targets:
                    check_cancel(cancel)
                    try:
                        result = scan_all(client, journal, records, cancel, emit, full, only, self.legacy_source)
                        for key in ("scanned", "skipped", "errors", "total"):
                            total[key] += result[key]
                    except (ValueError, OSError, RuntimeError) as error:
                        total["errors"] += 1
                        total["source_errors"].append(journal.source + ": " + str(error))
                return total
            def complete(result):
                if self.table.currentRow() < 0 and self.table.rowCount():
                    self.table.selectRow(next((self.table_indices[k] for k, row in self.rows.items()
                                               if row["status"] == "ready"), 0))
                else:
                    self.select_row()
                self.status.setText(
                    "Скан завершён · проверено %d · пропущено %d · ошибок %d. Выберите папку для подтверждения."
                    % (result["scanned"], result["skipped"], result["errors"])
                    + ("\n" + "\n".join(result["source_errors"]) if result["source_errors"] else ""))
            self.launch(operation, complete)
        except (ValueError, OSError, RuntimeError) as error:
            self.show_error(str(error))

    def on_event(self, event):
        if event["kind"] == "folders":
            journal = self.journals[path_key(event["source_root"])]
            for folder in event["folders"]:
                self.upsert(journal.rows.get(folder_key(folder), dict(folder=folder, status="new", source_root=journal.source)))
        elif event["kind"] == "row":
            self.upsert(event["row"])
        elif event["kind"] == "commit":
            self.stop.setEnabled(False)
            self.status.setText("Публикую карточку и обновляю каталог. Завершаю запись…")
        else:
            self.progress.setRange(0, max(1, event["total"]))
            self.progress.setValue(event["done"])
            self.status.setText(event["text"])

    def upsert(self, row):
        key = row_key(row)
        if key not in self.table_indices:
            self.table_indices[key] = self.table.rowCount()
            self.table.insertRow(self.table.rowCount())
        self.rows[key] = row
        index = self.table_indices[key]
        state = STATUS[row["status"]]
        if row.get("pending_export"):
            state = "Публикация не завершена"
        publication = next((j for j in self.publication_jobs if j["url"] == self.connected_url
                            and path_key(j["source"]) == path_key(row["source_root"]) and j["folder"] == row["folder"]), None)
        if publication and publication["state"] != "done":
            state = PUBLICATION_STATUS[publication["state"]]
        name = row.get("name") or (row["matches"][0]["name"] if row.get("matches") else "")
        for column, value in enumerate((row["folder"], state, name, row["source_root"])):
            item = QTableWidgetItem(value)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(value)
            if column == 1:
                item.setForeground(QColor("#9be6ad" if row["status"] == "published" else "#d7dce7"))
            self.table.setItem(index, column, item)
        self.filter_rows()
        self.counts.setText("Всего %d · к проверке %d · в TACTIC %d · ошибок %d" % (
            len(self.rows), sum(r["status"] in ("ready", "not_found") for r in self.rows.values()),
            sum(r["status"] == "published" for r in self.rows.values()),
            sum(r["status"] == "error" for r in self.rows.values())))

    def filter_rows(self):
        query = self.filter.text().casefold()
        for row in range(self.table.rowCount()):
            self.table.setRowHidden(row, bool(query) and not any(
                query in self.table.item(row, col).text().casefold()
                for col in range(4) if self.table.item(row, col)))

    def select_row(self):
        index = self.table.currentRow()
        item = self.table.item(index, 0)
        if item is None:
            self.active_folder = ""
            self.invalidate()
            return
        row = self.rows[item.data(Qt.ItemDataRole.UserRole)]
        self.active_folder = row["folder"]
        self.active_source = row["source_root"]
        self.invalidate()
        self.folder_label.setText(row["source_root"] + "\n" + row["folder"])
        self.query.setText(row.get("query", row["folder"]))
        self.matches.clear()
        stale = row["status"] in ("ready", "not_found") and row.get("metadata_revision", 0) < METADATA_REVISION
        self.matches.setPlaceholderText("Обновляю сохранённый результат поиска…" if stale else "Ничего не найдено. Уточните название.")
        for match in ([] if stale else row.get("matches", [])):
            provider = ("Epic Games" if match["app_id"].startswith("epic:") else
                        "GOG" if match["app_id"].startswith("gog:") else
                        "Lutris" if match["app_id"].startswith("lutris:") else
                        "Wikipedia" if match["app_id"].startswith("wikipedia:") else "Steam " + match["app_id"])
            self.matches.addItem(match["name"] + " · " + provider, match["app_id"])
        self.matches.setCurrentIndex(0 if self.matches.count() else -1)
        self.fill_executables(row.get("executables", []))
        self.note.setText(row.get("error") or row.get("warning") or (
            "Карточка уже опубликована. Для повторной проверки нажмите «Пересканировать папку»."
            if row["status"] == "published" else "Выберите правильную игру и её EXE, затем загрузите карточку."))
        self.update_actions()
        if stale:
            key = row_key(row)
            QTimer.singleShot(0, lambda: self.refresh_saved_match(key))

    def refresh_saved_match(self, key):
        row = self.rows.get(key)
        if (not self.worker and row and self.same_source() and row["folder"] == self.active_folder
                and path_key(row["source_root"]) == path_key(self.active_source)
                and row.get("metadata_revision", 0) < METADATA_REVISION and self.search.isEnabled()):
            self.status.setText("Обновляю устаревший результат поиска: " + row["folder"])
            self.search_game()

    def fill_executables(self, candidates, selected=""):
        self.exes.blockSignals(True)
        self.exes.clear()
        self.exes.setEditText("")
        for exe in candidates:
            self.exes.addItem(exe["path"] + (" · служебный EXE" if exe["utility"] else ""), exe["path"])
            if exe["utility"]:
                self.exes.setItemData(self.exes.count() - 1, "Служебный EXE — проверьте выбор", Qt.ItemDataRole.ToolTipRole)
        self.exes.setCurrentIndex(0 if self.exes.count() else -1)
        if selected:
            self.exes.setCurrentText(selected)
        self.exes.blockSignals(False)
        self.exe_changed()

    def choose_executable(self):
        if not self.pick_exe.isEnabled():
            return
        try:
            root = safe_path(Path(self.journal.source), self.active_folder)
            filename, _ = QFileDialog.getOpenFileName(self, "EXE внутри папки выбранной игры", str(root),
                "Windows EXE (*.exe)", options=QFileDialog.Option.DontUseNativeDialog)
            if not filename:
                return
            selected = safe_path(root, Path(filename).relative_to(root).as_posix())
            if selected.suffix.lower() != ".exe" or not selected.is_file():
                raise ValueError("Выберите EXE внутри папки этой игры.")
            self.exes.setEditText(selected.relative_to(root).as_posix())
        except (ValueError, OSError) as error:
            self.status.setText("EXE не выбран: " + str(error))

    def find_executables(self):
        if not self.find_exes.isEnabled():
            return
        journal, folder, selected = self.journal, self.active_folder, self.selected_executable()
        self.status.setText("Ищу EXE только внутри выбранной игры…")
        def operation(cancel, emit):
            result = scan_executables(safe_path(Path(journal.source), folder), cancel, allow_network=True)
            check_cancel(cancel)
            from ngs.game_db import match_known_game
            best = match_known_game(folder, result["candidates"], source=journal.source).get("executable")
            if best:
                result["candidates"].sort(key=lambda item: 0 if item["path"].casefold() == best.casefold() else (2 if item.get("utility") else 1))
            journal.save(dict(journal.rows[folder_key(folder)], executables=result["candidates"],
                              warning="Поиск EXE неполный: есть ограничения или недоступные папки."
                                      if result["limited"] or result["skipped"] else ""))
            return result
        def complete(result):
            self.table.blockSignals(True)
            self.upsert(journal.rows[folder_key(folder)])
            self.table.blockSignals(False)
            self.fill_executables(result["candidates"], selected)
            self.status.setText("Найдено EXE: %d. Проверьте файл запуска." % len(result["candidates"])
                                if result["candidates"] else "EXE в папке игры не найден. Проверьте полноту файлов на NAS.")
        self.launch(operation, complete)

    def invalidate(self, *args):
        self.details, self.prepared = None, None
        self.preview_records, self.preview_edits = [], {}
        self.cover.clear()
        self.cover.setText("Превью появится после выбора игры")
        self.name.clear()
        self.description.clear()
        self.update_actions()

    def selected_executable(self):
        text = self.exes.currentText().strip()
        return self.exes.currentData() if self.exes.currentIndex() >= 0 and text == self.exes.itemText(self.exes.currentIndex()) else text

    def exe_changed(self, *args):
        if self.details is not None:
            try:
                self.prepared = prepare_import(self.preview_records, self.active_folder, self.details,
                    self.selected_executable(), edits=self.preview_edits,
                    source=self.journal.source, legacy_source=self.legacy_source)
            except ValueError as error:
                self.prepared = None
                self.status.setText(str(error))
        self.update_actions()

    def search_game(self):
        if self.worker or not self.active_folder or not self.same_source():
            return
        folder, query = self.active_folder, self.query.text().strip()
        if not query:
            return
        def operation(cancel, emit):
            matches = metadata(self.connect_client(), "search", query, source=self.journal.source, folder=folder)
            check_cancel(cancel)
            row = dict(self.journal.rows[folder_key(folder)], matches=matches, query=query,
                       status="ready" if matches else "not_found", error="", metadata_revision=METADATA_REVISION)
            self.journal.save(row)
            return row
        def complete(row):
            self.upsert(row)
            self.select_row()
            self.status.setText("Найдено вариантов: %d" % len(row["matches"]))
        self.launch(operation, complete)

    def load_game(self, edits=None):
        if self.worker or not self.matches.currentData():
            self.status.setText("Выберите игру для просмотра карточки.")
            return
        app_id, folder, exe = self.matches.currentData(), self.active_folder, self.selected_executable()
        def operation(cancel, emit):
            client = self.connect_client()
            emit(dict(kind="progress", done=0, total=2, text="Получаю оформление выбранной игры…"))
            details = metadata(client, "details", app_id, storage=self.storage)
            check_cancel(cancel)
            emit(dict(kind="progress", done=1, total=2, text="Сверяю актуальную карточку и ручные правки в TACTIC…"))
            cached = self.record_cache
            if not cached or cached[1] != self.connected_url or time.monotonic() - cached[0] >= 10:
                cached = (time.monotonic(), self.connected_url, invoke(client, "inspect")["records"])
            records = cached[2]
            prepared = prepare_import(records, folder, details, exe,
                                  edits=edits if isinstance(edits, dict) else None,
                                  source=self.journal.source, legacy_source=self.legacy_source)
            return dict(prepared=prepared, records=records, record_cache=cached)
        def complete(result):
            prepared = result["prepared"]
            self.record_cache = result["record_cache"]
            self.preview_records, self.preview_edits = result["records"], prepared["edits"]
            self.prepared, self.details = prepared, prepared["details"]
            self.name.setText(prepared["merged"]["name"])
            self.description.setPlainText(prepared["merged"]["description"])
            image = self.details["cover"]
            data = base64.b64decode(image["base64"], validate=True)
            pixmap = QPixmap()
            buffer = QBuffer()
            buffer.setData(QByteArray(data))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer)
            size = reader.size()
            bounded = (size.isValid() and max(size.width(), size.height()) <= 8192
                       and size.width() * size.height() <= 16_000_000 and bytes(reader.format()).lower() in (b"png", b"jpeg", b"webp"))
            if (bounded and len(data) <= 8 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == image["sha256"]
                    and pixmap.loadFromData(data)):
                self.cover.setPixmap(pixmap.scaled(130, 185, Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))
            self.note.setText(("Сохранены ручные поля: " + ", ".join(prepared["preserved"]) + ". "
                              if prepared["preserved"] else "Название и описание можно исправить. ")
                             + "Превью — от источника оформления; ручные изображения в TACTIC сохраняются при публикации.")
            self.status.setText("Проверьте игру и EXE. «Добавить в очередь» подтверждает выбранную карточку."
                               if prepared["proposed"]["executable"] else "Карточка загружена. Выберите или введите EXE для публикации.")
        self.launch(operation, complete)

    def publish_game(self):
        if not self.publish.isEnabled():
            return
        prepared = self.prepared
        edits = dict(prepared["edits"])
        for field, value in (("name", self.name.text().strip()), ("description", self.description.toPlainText().strip())):
            if value != prepared["merged"][field]:
                edits[field] = value
        prepared = dict(prepared, edits=edits)
        try:
            row = self.journal.rows[folder_key(self.active_folder)]
            self.publication_queue.add(self.journal.source, self.connected_url, prepared, self.rehash.isChecked(),
                                       pending_export=row.get("pending_export", False))
        except (ValueError, OSError) as error:
            self.show_error(str(error))
            return
        self.take_password()
        self.invalidate()
        self.refresh_publications()
        self.status.setText("Добавлена в очередь: " + prepared["merged"]["name"] + ". Можно проверить следующую игру.")
        self.start_publication()

    def selected_publication(self):
        item = self.queue_table.item(self.queue_table.currentRow(), 0)
        return next((j for j in self.publication_jobs if item and j["position"] == item.data(Qt.ItemDataRole.UserRole)), None)

    def update_queue_actions(self):
        if not hasattr(self, "queue_remove"):
            return
        job = self.selected_publication()
        self.queue_remove.setEnabled(bool(job and job["state"] != "running" and not job["pending_export"]))
        self.queue_review.setEnabled(bool(job and job["state"] in ("error", "done") and self.worker is None))
        self.queue_message.setText(job["error"] if job and job["error"] else
                                   "Ошибка публикации не останавливает остальные игры. Для повтора перепроверьте карточку и подтвердите EXE.")

    def refresh_publications(self):
        selected = self.selected_publication()
        self.publication_jobs = self.publication_queue.jobs()
        self.queue_table.blockSignals(True)
        self.queue_table.setRowCount(len(self.publication_jobs))
        for index, job in enumerate(self.publication_jobs):
            for column, value in enumerate((job["name"], PUBLICATION_STATUS[job["state"]], job["source"] + " / " + job["folder"])):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, job["position"])
                item.setToolTip(job["error"] or value)
                self.queue_table.setItem(index, column, item)
            if selected and selected["position"] == job["position"]:
                self.queue_table.selectRow(index)
        self.queue_table.blockSignals(False)
        waiting = sum(j["state"] == "queued" for j in self.publication_jobs)
        failed = sum(j["state"] == "error" for j in self.publication_jobs)
        self.tabs.setTabText(1, "Очередь публикации (%d)" % (waiting + bool(self.publication_worker)))
        self.queue_pause.setText("Продолжить очередь" if self.publication_paused else "Приостановить очередь")
        self.queue_pause.setEnabled(bool(waiting or self.publication_worker))
        if self.publication_worker is None:
            self.publication_status.setText("Публикации: ожидают %d · ошибок %d%s" % (
                waiting, failed, " · очередь приостановлена" if self.publication_paused and waiting else ""))
        for row in list(self.rows.values()):
            self.upsert(row)
        self.update_actions()

    def remove_publication(self):
        job = self.selected_publication()
        if job and self.queue_remove.isEnabled():
            self.publication_queue.remove(job)
            self.refresh_publications()

    def review_publication(self):
        job = self.selected_publication()
        if not job or not self.queue_review.isEnabled():
            return
        key = path_key(job["source"]) + "\0" + folder_key(job["folder"])
        if job["url"] != self.connected_url or key not in self.table_indices:
            self.status.setText("Сначала выберите сохранённый сервер и NAS, затем просканируйте папки.")
            return
        self.table.selectRow(self.table_indices[key])
        self.select_row()
        self.tabs.setCurrentIndex(0)
        if job["state"] == "error":
            self.record_cache = None
            prepared = self.publication_queue.prepared(job["position"])
            reference = prepared["details"]["provider_id"].removeprefix("steam:")
            index = self.matches.findData(reference)
            if index < 0:
                self.matches.addItem(prepared["proposed"]["name"], reference)
                index = self.matches.count() - 1
            self.matches.setCurrentIndex(index)
            self.exes.setEditText(prepared["proposed"]["executable"])
            self.load_game(prepared["edits"])

    def toggle_publications(self):
        self.publication_paused = not self.publication_paused
        if self.publication_paused and self.publication_worker:
            self.publication_worker.cancel.set()
        self.take_password()
        self.refresh_publications()
        self.start_publication()

    def mark_publication_commit(self, job):
        journal = self.journals.get(path_key(job["source"])) if job["url"] == self.connected_url else None
        if journal and folder_key(job["folder"]) in journal.rows:
            journal.save(dict(journal.rows[folder_key(job["folder"])], pending_export=True))
            self.upsert(journal.rows[folder_key(job["folder"])])

    def apply_publication(self, job):
        self.record_cache = None
        journal = self.journals.get(path_key(job["source"])) if job["url"] == self.connected_url else None
        if journal:
            row = dict(journal.rows.get(folder_key(job["folder"]), {}), source_root=journal.source, folder=job["folder"],
                       status="published", game_id=job["result"]["game"], name=job["result"]["card"]["name"],
                       pending_export=False, error="")
            journal.save(row)
            self.upsert(row)
            job["journal_pending"] = False
            self.publication_queue.save(job)

    def start_publication(self):
        if self.closing or self.publication_paused or self.publication_worker or self.worker:
            return
        job = next((j for j in self.publication_jobs if j["state"] == "queued"), None)
        if not job:
            return
        try:
            if job["url"] != self.connected_url or path_key(job["source"]) not in self.journals:
                raise ValueError("Сервер или NAS задания не совпадает с текущими настройками. Перепроверьте карточку.")
            prepared = self.publication_queue.prepared(job["position"])
            if (prepared["proposed"]["source_folder"] != job["folder"]
                    or prepared["proposed"].get("source_root", job["source"]) != job["source"]):
                raise ValueError("Источник задания публикации изменён.")
        except (ValueError, OSError, KeyError, TypeError) as error:
            job.update(state="error", error=str(error))
            self.publication_queue.save(job)
            self.refresh_publications()
            QTimer.singleShot(0, self.start_publication)
            return
        self.take_password()
        password, url, source = self.login_password, job["url"], job["source"]
        job.update(state="running", error="")
        self.publication_queue.save(job)
        self.publication_active = job
        def operation(cancel, emit):
            client = self.connect_client() if url == self.connected_url else admin_client(url, CREDENTIALS, password=password)
            last = [0.0]
            def progress(phase, done, total, path):
                if time.monotonic() - last[0] > .15:
                    emit(dict(kind="progress", done=int(done / max(1, total) * 1000), total=1000,
                              text="Подготовка %.1f%% · %s" % (done / max(1, total) * 100, path)))
                    last[0] = time.monotonic()
            def before_commit():
                job["pending_export"] = True
                self.publication_queue.save(job)
                emit({"kind": "commit"})
            result = publish_folder(client, self.storage, source, prepared,
                job["full_hash"], cancel, progress, before_commit, retry_export=job["pending_export"])
            job.update(state="done", result=result, pending_export=False, journal_pending=True)
            self.publication_queue.save(job)
            return job
        self.publication_worker = Worker(operation, self)
        self.publication_worker.event.connect(self.publication_event)
        self.publication_worker.result.connect(self.apply_publication)
        self.publication_worker.failed.connect(self.publication_failed)
        self.publication_worker.finished.connect(self.publication_finished)
        self.publication_status.setText("Публикуется: " + job["name"])
        self.publication_progress.setRange(0, 0)
        self.refresh_publications()
        self.publication_worker.start()

    def publication_event(self, event):
        if event["kind"] == "commit":
            self.mark_publication_commit(self.publication_active)
            self.publication_status.setText("Завершаю запись: " + self.publication_active["name"])
        else:
            self.publication_progress.setRange(0, max(1, event["total"]))
            self.publication_progress.setValue(event["done"])
            self.publication_status.setText(self.publication_active["name"] + " · " + event["text"])

    def publication_failed(self, message):
        job = self.publication_active
        stopped = self.publication_worker.cancel.is_set() and not job["pending_export"]
        job.update(state="queued" if stopped else "error", error="" if stopped else message)
        self.publication_queue.save(job)

    def publication_finished(self):
        worker, self.publication_worker = self.publication_worker, None
        worker.deleteLater()
        self.publication_progress.setRange(0, 100)
        self.publication_progress.setValue(100 if self.publication_active["state"] == "done" else 0)
        self.publication_active = None
        self.refresh_publications()
        QTimer.singleShot(0, self.close if self.closing else self.start_publication)

    def closeEvent(self, event):
        if self.worker or self.publication_worker:
            self.closing = True
            self.publication_paused = True
            if self.publication_worker:
                self.publication_worker.cancel.set()
            self.cancel()
            event.ignore()
        else:
            event.accept()


def run(storage):
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("NGS Catalogizer")
    lock = QLockFile(str(safe_path(storage.state, "catalogizer.lock")))
    if not lock.tryLock(0):
        QMessageBox.information(None, "NGS", "Каталогизатор из этой папки уже открыт.")
        return 1
    font_id = QFontDatabase.addApplicationFont(str(storage.root / "assets/fonts/Manrope.ttf"))
    if font_id >= 0:
        app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { background: #141923; color: #e8edf6; }
        QLabel#title { font-size: 26px; font-weight: 700; }
        QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QTableWidget { background: #1c2433; border: 1px solid #344155;
            border-radius: 5px; padding: 6px; selection-background-color: #3d596e; }
        QPushButton { background: #293548; border: 1px solid #40516b; border-radius: 6px; padding: 9px 13px; }
        QPushButton:hover { background: #3b4c65; } QPushButton#primary { background: #416d54; }
        QPushButton:disabled, QLineEdit:disabled { color: #737e90; }
        QHeaderView::section { background: #293548; border: 0; padding: 8px; }
        QCheckBox { padding: 4px; } QCheckBox::indicator { width: 18px; height: 18px;
            border: 1px solid #63748c; border-radius: 3px; background: #1c2433; }
        QCheckBox::indicator:checked { background: #9be6ad; border-color: #9be6ad; }
        QProgressBar { border: 1px solid #344155; border-radius: 4px; text-align: center; }
        QProgressBar::chunk { background: #629b7b; } QSplitter::handle { background: #344155; }
    """)
    window = CatalogizerWindow(storage)
    window.show()
    return app.exec()
