"""Portable account token and an idempotent outbox; TACTIC owns shared totals."""
from ngs.i18n import tr
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from PySide6.QtCore import QObject, Property, QThread, QTimer, Signal, Slot

from ngs import player_protocol as protocol
from ngs.backups import clean_device_name, count_local_games, default_device_name, download_backup, list_backups, restore_backup, upload_backup
from ngs.catalog import atomic_write
from ngs.diagnostics import remember_secret
from ngs.install import safe_path
from ngs.tactic import _NoRedirect
from ngs.update_crypto import RsaKey
from ngs.server_trust import server_key_path, connect_server
from ngs.http_retry import transient_network_error


def server_origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(tr("Invalid server address."))
    return parsed.scheme + '://' + parsed.netloc.lower()


def account_credentials(account, url):
    if (not isinstance(account, dict) or account.get('server') != server_origin(url)
            or not re.fullmatch('[0-9a-f]{32}', account.get('profile', ''))
            or not re.fullmatch('[0-9a-f]{64}', account.get('token', ''))
            or type(account.get('expires')) is not int or account['expires'] <= time.time()):
        raise ValueError(tr("Sign in to NGS first."))
    api = account.get('api', {})
    if (not isinstance(api, dict) or set(api) != {'device', 'secret', 'expires'}
            or not re.fullmatch('[0-9a-f]{32}', api['device']) or not re.fullmatch('[0-9a-f]{64}', api['secret'])
            or type(api['expires']) is not int or api['expires'] <= time.time()):
        raise ValueError(tr("Connecting NGS access after sign-in…"))
    return api


def saved_account(value):
    """Validate persisted fields without requiring an unexpired server session."""
    if not isinstance(value, dict) or set(value) != {'device', 'account'}:
        raise ValueError('Invalid saved profile document')
    device, account = value['device'], value['account']
    if not isinstance(device, str) or not re.fullmatch('[0-9a-f]{32}', device) or not isinstance(account, dict):
        raise ValueError('Invalid saved profile identity')
    if not account: return device, {}
    required = {'server', 'profile', 'username', 'token', 'expires'}
    if set(account) not in (required, required | {'api'}):
        raise ValueError('Invalid saved profile fields')
    if (not isinstance(account['server'], str) or len(account['server']) > 2048
            or server_origin(account['server']) != account['server']
            or not isinstance(account['profile'], str) or not re.fullmatch('[0-9a-f]{32}', account['profile'])
            or not isinstance(account['token'], str) or not re.fullmatch('[0-9a-f]{64}', account['token'])
            or type(account['expires']) is not int or not 0 <= account['expires'] < 2**63):
        raise ValueError('Invalid saved profile values')
    protocol.username(account['username'])
    if 'api' in account:
        api = account['api']
        if (not isinstance(api, dict) or set(api) != {'device', 'secret', 'expires'}
                or not isinstance(api['device'], str) or not re.fullmatch('[0-9a-f]{32}', api['device'])
                or not isinstance(api['secret'], str) or not re.fullmatch('[0-9a-f]{64}', api['secret'])
                or type(api['expires']) is not int or not 0 <= api['expires'] < 2**63):
            raise ValueError('Invalid saved API credentials')
    return device, account


def game_identity(receipt, fallback):
    card = receipt.get('card', {})
    identity = receipt.get('catalog_id') or card.get('id', '')
    if identity and not identity.startswith(('local-', 'setup-')) and re.fullmatch('[a-z0-9][a-z0-9_-]{0,95}', identity):
        return identity, card.get('name') or fallback
    name = card.get('name') or fallback
    normalized = ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', name).casefold()))
    # shortcut: cards without a provider ID match by title; attach catalog metadata to disambiguate namesakes.
    return 'title-' + hashlib.sha256(normalized.encode('utf8')).hexdigest(), name


def session_title(name, fallback):
    """The displayed title stays untouched; the wire protocol requires one short line."""
    return ' '.join(''.join(c if ord(c) >= 32 else ' ' for c in name).split())[:256] or fallback


class PlayerTransport:
    def __init__(self, origin, pinned):
        self.origin, self.pinned = origin, pinned
        self.opener = build_opener(_NoRedirect)
        self.public = None

    def read(self, request):
        try:
            with self.opener.open(request, timeout=12) as response:
                body = response.read(protocol.MAX_WIRE + 1)
                if len(body) > protocol.MAX_WIRE: raise ValueError(tr("The profile response is too large."))
                return body
        except OSError as error:
            if not transient_network_error(error): raise
            raise ConnectionError(tr("Profile server offline. Sessions saved; they will upload automatically when connected.")) from None

    def call(self, value):
        if self.public is None:
            descriptor = json.loads(self.read(Request(self.origin + protocol.PUBLIC_PATH)))
            if set(descriptor) != {'payload', 'signature'}: raise ValueError(tr("Invalid server key."))
            payload, signature = protocol.decode(descriptor['payload']), protocol.decode(descriptor['signature'])
            with RsaKey(self.pinned.read_bytes()) as key:
                key.verify(protocol.DOMAIN + payload, signature)
            info = json.loads(payload)
            if set(info) != {'schema', 'public', 'kid'} or info['schema'] != 1 or info['kid'] != protocol.key_id(info['public']):
                raise ValueError(tr("Invalid server key."))
            self.public = info['public']
        wire, key, identity = protocol.request(self.public, value)
        body = self.read(Request(self.origin + protocol.PATH, data=wire, headers={'Content-Type': 'application/jose'}))
        response, unused = protocol.open_message(body, key, protocol.key_id(self.public))
        if response.get('request_id') != identity or type(response.get('ok')) is not bool:
            raise ValueError(tr("The profile response does not match the request."))
        if not response['ok']:
            error = protocol.SessionExpired if response.get('code') == 'auth' else ValueError
            raise error(response.get('error', tr("Request rejected.")))
        return response['result']


class PlayerStore:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS player_outbox (id TEXT PRIMARY KEY, server TEXT NOT NULL, profile TEXT NOT NULL, '
                       'game TEXT NOT NULL, name TEXT NOT NULL, ack_active INTEGER NOT NULL DEFAULT -1, ack_paused INTEGER NOT NULL DEFAULT -1, ack_ended INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS player_totals (server TEXT NOT NULL, profile TEXT NOT NULL, game TEXT NOT NULL, '
                       'launches INTEGER NOT NULL, active_ms INTEGER NOT NULL, longest_ms INTEGER NOT NULL, PRIMARY KEY(server,profile,game))')
            db.execute('CREATE INDEX IF NOT EXISTS player_outbox_game ON player_outbox(server,profile,game)')

    def attach(self, session, account, receipt, name):
        game, name = game_identity(receipt, name)
        # Catalog/local titles may contain line breaks; the stats protocol is single-line.
        name = session_title(name, game)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR IGNORE INTO player_outbox(id,server,profile,game,name) VALUES(?,?,?,?,?)',
                       (session, account['server'], account['profile'], game, name))

    def pending(self, account, now=None):
        # A corrected clock can leave old sessions temporarily in the future. Keep them
        # locally, but never let them block otherwise valid sessions in the same batch.
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT p.id,o.game,o.name,p.started_at,p.updated_at,p.ended_at,p.active_ms,p.paused_ms '
                              'FROM play_sessions p JOIN player_outbox o ON o.id=p.id WHERE o.server=? AND o.profile=? '
                              'AND p.started_at>=946684800 AND p.updated_at<=? '
                              'AND (p.active_ms>o.ack_active OR p.paused_ms>o.ack_paused OR p.ended_at IS NOT o.ack_ended) ORDER BY p.started_at LIMIT 16',
                              (account['server'], account['profile'], now + 300)).fetchall()
        result = [dict(zip(('id','game','name','started_at','updated_at','ended_at','active_ms','paused_ms'), r)) for r in rows]
        for row in result:
            row['name'] = session_title(row['name'], row['game'])
        return result

    def deferred_count(self, account, now=None):
        now = int(time.time()) if now is None else now
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT COUNT(*) FROM play_sessions p JOIN player_outbox o ON o.id=p.id '
                              'WHERE o.server=? AND o.profile=? AND (p.started_at<946684800 OR p.updated_at>?) '
                              'AND (p.active_ms>o.ack_active OR p.paused_ms>o.ack_paused OR p.ended_at IS NOT o.ack_ended)',
                              (account['server'], account['profile'], now + 300)).fetchone()[0]

    def pending_count(self, account):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT COUNT(*) FROM play_sessions p JOIN player_outbox o ON o.id=p.id '
                              'WHERE o.server=? AND o.profile=? AND '
                              '(p.active_ms>o.ack_active OR p.paused_ms>o.ack_paused OR p.ended_at IS NOT o.ack_ended)',
                              (account['server'], account['profile'])).fetchone()[0]

    def accept(self, account, response, rows=()):
        if set(response) != {'totals', 'acked', 'more'} or set(response['acked']) != {r['id'] for r in rows}:
            raise ValueError(tr("Invalid statistics acknowledgment."))
        with closing(sqlite3.connect(self.path)) as db, db:
            for total in response['totals']:
                if (set(total) != {'game','launches','active_ms','longest_ms'} or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,95}', total['game'])
                        or any(type(total[k]) is not int or total[k] < 0 for k in ('launches','active_ms','longest_ms'))):
                    raise ValueError(tr("Invalid server statistics."))
                db.execute('INSERT OR REPLACE INTO player_totals VALUES(?,?,?,?,?,?)',
                           (account['server'],account['profile'],total['game'],total['launches'],total['active_ms'],total['longest_ms']))
            for row in rows:
                db.execute('UPDATE player_outbox SET ack_active=?,ack_paused=?,ack_ended=? WHERE id=? AND server=? AND profile=?',
                           (row['active_ms'],row['paused_ms'],row['ended_at'],row['id'],account['server'],account['profile']))

    def totals(self, account, snapshot):
        if not snapshot.get('session_id') or snapshot.get('local_only'): return snapshot
        with closing(sqlite3.connect(self.path)) as db:
            owner = db.execute('SELECT game FROM player_outbox WHERE id=? AND server=? AND profile=?',
                               (snapshot['session_id'],account['server'],account['profile'])).fetchone()
            if not owner: return snapshot
            game = owner[0]
            cached = db.execute('SELECT launches,active_ms,longest_ms FROM player_totals WHERE server=? AND profile=? AND game=?',
                                (account['server'],account['profile'],game)).fetchone() or (0,0,0)
            rows = db.execute('SELECT p.id,p.active_ms,o.ack_active FROM play_sessions p JOIN player_outbox o ON o.id=p.id '
                              'WHERE o.server=? AND o.profile=? AND o.game=? AND (p.active_ms>o.ack_active OR p.id=?)',
                              (account['server'],account['profile'],game,snapshot['session_id'])).fetchall()
        launches, active, longest = cached
        for identity, elapsed, acknowledged in rows:
            if identity == snapshot['session_id']: elapsed = snapshot['session_seconds'] * 1000
            active += max(0, elapsed - max(0, acknowledged))
            launches += acknowledged < 0
            longest = max(longest, elapsed)
        return dict(snapshot, total_seconds=active//1000, launches=launches, longest_seconds=longest//1000)

    def legacy(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT id,game_id FROM play_sessions WHERE id NOT IN (SELECT id FROM player_outbox)').fetchall()


class PlayerWorker(QThread):
    result = Signal(object)
    failed = Signal(str, bool)
    expired = Signal()
    def __init__(self, controller, action, values):
        super().__init__(controller)
        self.store, self.account = controller.store, dict(controller.account)
        self.origin, self.pinned = controller.origin(), server_key_path(controller.storage)
        self.device, self.action, self.values = controller.device, action, values

    def run(self):
        try:
            rows = self.store.pending(self.account) if self.action == 'flush' else None
            if rows == []:
                # The outbox can be acknowledged between the timer check and worker start.
                self.result.emit({'idle': True}); return
            if self.action in ('register', 'login'):
                pairing = self.values.pop('pairing_code', None)
                self.pinned = connect_server(self.parent().storage, self.origin, expected_fingerprint=pairing)
            transport = PlayerTransport(self.origin, self.pinned)
            if self.action in ('register', 'login'):
                account = transport.call(dict(self.values, action=self.action, device=self.device))
                if (set(account) != {'profile','username','token','expires'} or not re.fullmatch('[0-9a-f]{32}', account['profile'])
                        or not re.fullmatch('[0-9a-f]{64}', account['token']) or type(account['expires']) is not int):
                    raise ValueError(tr("Invalid server profile."))
                account = dict(account, server=self.origin)
                account['api'] = transport.call(dict(action='authorize', device=self.device, token=account['token']))
                account_credentials(account, self.origin)
                self.result.emit(account); return
            auth = dict(device=self.device, token=self.account['token'])
            if self.action == 'logout':
                transport.call(dict(auth, action='logout')); self.result.emit({}); return
            try: account_credentials(self.account, self.origin)
            except (ValueError, TypeError):
                api = transport.call(dict(auth, action='authorize'))
                account_credentials(dict(self.account, api=api), self.origin)
                self.result.emit({'api': api})
            if self.action == 'import':
                with closing(sqlite3.connect(self.store.path)) as db:
                    receipts = {r[0]: json.loads(r[1]) for r in db.execute('SELECT game_id,receipt FROM installed')}
                for session, game in self.store.legacy():
                    if self.isInterruptionRequested(): return
                    receipt = receipts.get(game, {'card': {'id': game, 'name': game}})
                    self.store.attach(session, self.account, receipt, game)
            # Each acknowledged batch commits its cache and outbox together before another request.
            for unused in range(1000):
                if self.isInterruptionRequested(): return
                if rows is None: rows = self.store.pending(self.account)
                if not rows: break
                response = transport.call(dict(auth, action='sync', rows=rows, offset=0))
                self.store.accept(self.account, response, rows)
                rows = None
            if self.action != 'flush':
                offset = 0
                while not self.isInterruptionRequested():
                    response = transport.call(dict(auth, action='pull', rows=[], offset=offset))
                    self.store.accept(self.account, response)
                    if not response['more']: break
                    offset += 50
                    if offset > 100000: raise ValueError(tr("The profile is too large."))
            self.result.emit({'synced': int(time.time()), 'deferred': self.store.deferred_count(self.account)})
        except Exception as error:
            if isinstance(error, protocol.SessionExpired): self.expired.emit()
            offline = transient_network_error(error)
            self.failed.emit(str(error) if isinstance(error, (ValueError, RuntimeError, ConnectionError))
                             else tr("Could not sync the profile. Retrying automatically."), offline)
        finally:
            self.values.clear()


class BackupWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, controller, action, values):
        super().__init__(controller)
        self.storage = controller.storage
        self.tactic_url = controller.storage.read_settings()["tactic_url"]
        self.action = action
        self.values = dict(values or {})

    def run(self):
        try:
            from ngs.api import MetadataApi
            api = MetadataApi(self.storage, self.tactic_url)
            if self.action == "list":
                data = list_backups(api)
                self.result.emit({"action": "list", "data": data})
                return
            if self.action == "backup":
                saved = upload_backup(
                    api,
                    self.storage,
                    self.values["target_device"],
                    self.values.get("device_name", ""),
                )
                data = list_backups(api)
                self.result.emit({"action": "backup", "saved": saved, "data": data})
                return
            if self.action == "restore":
                raw = download_backup(
                    api,
                    self.values["target_device"],
                    self.values["backup_id"],
                )
                restored = restore_backup(
                    self.storage,
                    raw,
                    apply_settings=bool(self.values.get("apply_settings", True)),
                    apply_library=bool(self.values.get("apply_library", True)),
                )
                data = list_backups(api)
                self.result.emit({
                    "action": "restore",
                    "restored": restored,
                    "data": data,
                    "target_device": self.values["target_device"],
                    "device_name": self.values.get("device_name", ""),
                    "backup_id": self.values["backup_id"],
                })
        except Exception as error:
            self.failed.emit(str(error) if isinstance(error, (ValueError, RuntimeError, ConnectionError))
                             else tr("Could not complete the server backup operation."))


_LINGERING_WORKERS = []


class PlayerAccounts(QObject):
    changed = Signal()
    backupRestored = Signal(dict)
    def __init__(self, storage, library, parent=None):
        super().__init__(parent)
        self.storage, self.library = storage, library
        self.store = PlayerStore(library.path)
        self.path = safe_path(storage.config, 'player-account.json')
        self.backup_path = safe_path(storage.config, 'backup-device.json')
        self.backup_worker = None
        self.worker, self.account, self.device = None, {}, uuid.uuid4().hex
        self._pull_at, self._retry_at, self._retry_delay = 0.0, 0.0, 60.0
        self.offline = False
        self.message = tr("Sign in to share playtime and launch statistics across your devices.")
        try:
            with self.path.open('rb') as stream: raw = stream.read(8193)
            if len(raw) > 8192: raise ValueError('Saved profile is too large')
            self.device, self.account = saved_account(json.loads(raw))
            if self.account:
                remember_secret(self.account['token'])
                self.message = tr("Sign-in saved. Connecting NGS…")
                if 'api' in self.account: remember_secret(self.account['api']['secret'])
        except FileNotFoundError: pass
        except (OSError, ValueError, TypeError):
            self.message = tr("Could not read the saved profile. Sign in again; the original file is preserved.")
        self.device_name = default_device_name()
        self.backup_device = self.device
        self.backup_chosen = False
        self.backup_devices = []
        self.backup_status = tr("Automatic library and settings backups use the selected device branch.")
        self._load_backup_config()
        self.timer = QTimer(self); self.timer.setInterval(60000); self.timer.timeout.connect(self.background_sync); self.timer.start()
        if self.account: QTimer.singleShot(1000, self.sync)

    def origin(self): return server_origin(self.storage.read_settings()['tactic_url'])
    def active_account(self):
        if not self.account:
            return {}
        try:
            origin = self.origin()
        except (ValueError, TypeError):
            return {}
        return self.account if self.account.get('server') == origin else {}
    def save(self): atomic_write(self.path, json.dumps(dict(device=self.device, account=self.account)).encode('utf8'))

    def authenticated(self):
        account = self.active_account()
        return bool(account) and type(account.get('expires')) is int and account['expires'] > time.time()

    def authorized(self):
        try: account_credentials(self.active_account(), self.origin()); return True
        except (ValueError, TypeError): return False

    def expire(self):
        self.account['expires'] = 0
        try: self.save()
        except OSError: pass
        self.changed.emit()

    @Property('QVariantMap', notify=changed)
    def state(self):
        account = self.active_account()
        return self._state_payload(account)



    def start(self, action, values=None):
        if self.worker: return
        try:
            if action in ('login','register','logout') and self.parent().process is not None:
                raise ValueError(tr("Close the game before changing profiles."))
            if action == 'logout' and (getattr(self.parent(), 'game_worker', None)
                    or self.parent().catalogizer.state['busy'] or self.parent().installer_catalogizer.state['busy']):
                raise ValueError(tr("Wait for downloads or catalog operations to finish before signing out."))
            if action in ('login','register') and self.authenticated(): raise ValueError(tr("Sign out of the current profile first."))
            if action in ('login','register'):
                protocol.username(values['username'])
                password = values['password']
                minimum = 12 if action == 'register' else 8
                if not minimum <= len(password) <= 128 or len(password.encode('utf8')) > 512:
                    raise ValueError(tr("New accounts require 12 to 128 password characters.") if action == 'register'
                                     else tr("The password must contain 8 to 128 characters."))
            worker = PlayerWorker(self, action, values or {})
            self.worker = worker
            self.message = tr("Signing in…") if action in ('login','register') else tr("Syncing with your server…")
            worker.result.connect(self.worker_result)
            worker.failed.connect(self.failure)
            worker.expired.connect(self.expire)
            worker.finished.connect(self.finished)
            worker.start(); self.changed.emit()
        except (ValueError, OSError) as error:
            if action in ('sync', 'flush', 'import') and self.worker is None: self.defer_background_retry()
            self.failure(str(error))

    def worker_result(self, value):
        if self.worker is not None: self.complete(self.worker.action, value)

    def complete(self, action, value):
        if value == {'idle': True}: return
        self.offline = False
        if set(value) == {'api'}:
            self.account['api'] = value['api']; remember_secret(value['api']['secret'])
            try: self.save()
            except OSError: self.message = tr("Could not save sign-in data in the NGS folder.")
            self.changed.emit(); return
        self._retry_at, self._retry_delay = 0.0, 60.0
        if action in ('login','register'):
            self._pull_at = 0.0
            self.account = value; remember_secret(value['token'])
            try: self.save()
            except OSError: self.message = tr("Signed in, but the token could not be saved in the NGS folder."); return
            self.message = tr("Signed in. Loading shared statistics…")
        elif action == 'logout':
            self._pull_at = 0.0
            self.account = {}
            try: self.save()
            except OSError: self.message = tr("Signed out on the server; the local configuration could not be updated."); return
            self.message = tr("Signed out. Shared statistics are saved on your server.")
        else:
            if action != 'flush': self._pull_at = time.monotonic() + 300
            self.message = tr("Synced with your server · ") + time.strftime('%H:%M', time.localtime(value['synced']))
            if value.get('deferred'):
                self.message += tr(" · Sessions with invalid dates saved locally: %d. Check the device clock.") % value['deferred']

    def failure(self, message, offline=False):
        self.offline = offline
        if self.worker and self.worker.action in ('sync', 'flush', 'import'):
            self.defer_background_retry()
        if self.worker and self.worker.action == 'logout':
            self.account = {}
            try:
                self.save()
                message = tr("Signed out on this device. The server did not confirm token revocation; the token will expire automatically.")
            except OSError: message = tr("Could not delete the saved token from the NGS folder.")
        self.message = message; self.changed.emit()
    def finished(self):
        worker = self.worker; self.worker = None
        follow = worker.action in ('login','register') and bool(self.active_account())
        worker.deleteLater(); self.changed.emit()
        if follow:
            QTimer.singleShot(0, self.sync)
            if self.authorized():
                QTimer.singleShot(50, self.refreshBackups)
        elif worker.action in ('sync', 'import') and self.authorized() and not self.backup_devices: QTimer.singleShot(150, self.refreshBackups)

    def defer_background_retry(self):
        self._retry_at = time.monotonic() + self._retry_delay
        self._retry_delay = min(300.0, self._retry_delay * 2)

    @Slot()
    def background_sync(self):
        if self.worker or time.monotonic() < self._retry_at: return
        try:
            if not self.authenticated(): return
            # New session checkpoints are sent each minute. An unchanged profile is
            # refreshed every five minutes; the manual sync slot always refreshes now.
            if not self.authorized() or time.monotonic() >= self._pull_at:
                self.start('sync')
            elif self.store.pending(self.active_account()):
                self.start('flush')
        except (OSError, ValueError, TypeError, sqlite3.Error) as error:
            self.defer_background_retry()
            self.failure(str(error))

    @Slot(str, str, bool)
    def login(self, name, password, register): self.start('register' if register else 'login', dict(username=name.strip(), password=password))
    @Slot(str, str, bool, str)
    def loginWithPairing(self, name, password, register, pairing_code):
        self.start('register' if register else 'login', dict(username=name.strip(), password=password, pairing_code=pairing_code))
    @Slot()
    def logout(self): self.start('logout')
    @Slot()
    def sync(self):
        if self.authenticated(): self.start('sync')
        elif self.active_account():
            self.message = tr("Your NGS session expired. Sign in again in Settings.")
            self.changed.emit()
    @Slot()
    def importHistory(self):
        if not self.active_account() or self.worker: return
        if self.parent().process is not None: self.failure(tr("Close the game before transferring previous history.")); return
        self.start('import')

    def attach_current(self, session, identity, name):
        if not self.active_account(): return
        with closing(sqlite3.connect(self.library.path)) as db:
            row = db.execute('SELECT receipt FROM installed WHERE game_id=?', (identity,)).fetchone()
        self.store.attach(session, self.account, json.loads(row[0]) if row else {'card': {'id': identity, 'name': name}}, name)
        self.changed.emit()

    def snapshot(self, snapshot):
        if self.active_account(): return self.store.totals(self.account, snapshot)
        # Offline without a profile is a pending local session, not a claimed shared total.
        return snapshot

    def stop(self):
        self.timer.stop()
        for w in (self.worker, self.backup_worker):
            if w is not None:
                w.requestInterruption()
                if not w.wait(1500):
                    w.setParent(None)
                    _LINGERING_WORKERS.append(w)

    def _load_backup_config(self):
        self.backup_device = self.device
        self.device_name = default_device_name()
        self.backup_chosen = False
        try:
            if not self.backup_path.is_file() or self.backup_path.stat().st_size > 4096:
                return
            doc = json.loads(self.backup_path.read_bytes())
            if isinstance(doc, dict) and doc.get('schema') == 1:
                dev = str(doc.get('backup_device', ''))
                if re.fullmatch(r'[0-9a-f]{32}', dev):
                    self.backup_device = dev
                    self.backup_chosen = bool(doc.get('chosen', True))
                if doc.get('device_name'):
                    self.device_name = clean_device_name(doc['device_name'])
        except Exception:
            pass

    def _save_backup_config(self):
        try:
            atomic_write(
                self.backup_path,
                json.dumps(
                    {
                        'schema': 1,
                        'backup_device': self.backup_device,
                        'device_name': self.device_name,
                        'chosen': self.backup_chosen,
                    },
                    ensure_ascii=False,
                ).encode('utf8'),
            )
        except OSError:
            pass

    def _build_device_options(self):
        items = []
        seen = set()
        for entry in self.backup_devices:
            if not isinstance(entry, dict):
                continue
            dev_id = str(entry.get('device', ''))
            if not re.fullmatch(r'[0-9a-f]{32}', dev_id) or dev_id in seen:
                continue
            seen.add(dev_id)
            name = clean_device_name(entry.get('name') or dev_id[:8])
            revs = []
            for rev in entry.get('backups', []):
                if not isinstance(rev, dict) or not re.fullmatch(r'[0-9a-f]{32}', str(rev.get('id', ''))):
                    continue
                stamp = int(rev.get('created_at', 0))
                size_kb = max(1, (int(rev.get('size', 0)) + 1023) // 1024)
                games = int(rev.get('games_count', 0))
                when = time.strftime('%d.%m.%Y %H:%M', time.localtime(stamp)) if stamp > 0 else tr("no date")
                revs.append({
                    'id': rev['id'],
                    'created_at': stamp,
                    'size': int(rev.get('size', 0)),
                    'games_count': games,
                    'label': tr("{0} · games: {1} · {2} KB").format(when, games, size_kb),
                })
            updated = int(entry.get('updated_at', 0))
            games_count = int(entry.get('games_count', revs[0]['games_count'] if revs else 0))
            items.append({
                'device': dev_id,
                'name': name,
                'updated_at': updated,
                'games_count': games_count,
                'is_current': dev_id == self.device,
                'is_new': False,
                'backups': revs,
            })

        if self.device not in seen:
            items.append({
                'device': self.device,
                'name': self.device_name,
                'updated_at': 0,
                'games_count': count_local_games(self.storage),
                'is_current': True,
                'is_new': True,
                'backups': [],
            })

        local_count = count_local_games(self.storage)
        remote_with_backups = [it for it in items if it['backups']]
        if self.backup_chosen and any(it['device'] == self.backup_device for it in items):
            suggested = self.backup_device
        elif any(it['device'] == self.device and it['backups'] for it in items):
            suggested = self.device
        else:
            same_name = next(
                (it['device'] for it in remote_with_backups if it['name'].casefold() == self.device_name.casefold()),
                None,
            )
            if same_name:
                suggested = same_name
            elif local_count == 0 and remote_with_backups:
                suggested = remote_with_backups[0]['device']
            else:
                suggested = self.device

        for it in items:
            it['is_suggested'] = it['device'] == suggested
            it['is_active'] = it['device'] == self.backup_device
            short_id = it['device'][:8]
            tags = []
            if it['is_current']:
                tags.append(tr("this device"))
            if it['is_suggested']:
                tags.append(tr("recommended"))
            if it['backups']:
                tags.append(tr("backups: {0}, games: {1}").format(len(it['backups']), it['games_count']))
            else:
                tags.append(tr("new branch"))
            it['label'] = f"{it['name']} · {short_id} ({', '.join(tags)})"

        items.sort(key=lambda it: (not it['is_suggested'], not it['is_current'], -it['updated_at'], it['device']))
        return items, suggested

    def _state_payload(self, account):
        devices, suggested = self._build_device_options()
        has_remote_backups = any(bool(item.get('backups')) for item in devices)
        needs_choice = bool(
            self.authenticated()
            and not self.backup_chosen
            and has_remote_backups
            and count_local_games(self.storage) == 0
        )
        return dict(
            logged_in=self.authenticated(),
            authorized=self.authorized(),
            username=account.get('username', ''),
            busy=self.worker is not None,
            status=self.message,
            legacy=len(self.store.legacy()),
            offline=self.offline,
            pending=self.store.pending_count(account) if account else 0,
            device_id=self.device,
            device_name=self.device_name,
            backup_device=self.backup_device,
            backup_chosen=self.backup_chosen,
            suggested_device=suggested,
            backup_prompt=needs_choice,
            backup_busy=self.backup_worker is not None,
            backup_status=self.backup_status,
            devices=devices,
        )

    def _start_backup_worker(self, action, values=None, status_message=''):
        if self.backup_worker is not None:
            return False
        if not self.authorized():
            return False
        worker = BackupWorker(self, action, values or {})
        self.backup_worker = worker
        if status_message:
            self.backup_status = status_message
        worker.result.connect(self._backup_worker_result)
        worker.failed.connect(self._backup_worker_failed)
        worker.finished.connect(self._backup_worker_finished)
        worker.start()
        self.changed.emit()
        return True

    def _backup_worker_result(self, payload):
        if not isinstance(payload, dict):
            return
        action = payload.get('action')
        data = payload.get('data') or {}
        if isinstance(data.get('devices'), list):
            self.backup_devices = data['devices']
        devices, suggested = self._build_device_options()
        remote_with_backups = [it for it in devices if it.get('backups')]
        if not self.backup_chosen:
            if not remote_with_backups:
                self.backup_device = self.device
                self.backup_chosen = True
                self._save_backup_config()
            else:
                self.backup_device = suggested
        if action == 'list':
            if remote_with_backups:
                total_revs = sum(len(it.get('backups', [])) for it in remote_with_backups)
                self.backup_status = tr("Available device branches: {0} (historical revisions: {1}).").format(len(remote_with_backups), total_revs)
            else:
                self.backup_status = tr("No server backups exist for this account yet.")
        elif action == 'backup':
            saved = payload.get('saved') or {}
            stamp = time.strftime('%H:%M:%S', time.localtime(int(saved.get('created_at') or time.time())))
            games = int(saved.get('games_count', 0))
            short_dev = str(saved.get('target_device', self.backup_device))[:8]
            self.backup_status = tr("Backup saved on your server ({0}) · branch {1} · games: {2}.").format(stamp, short_dev, games)
        elif action == 'restore':
            restored = payload.get('restored') or {}
            target_dev = str(payload.get('target_device') or self.backup_device)
            dev_name = str(payload.get('device_name') or self.device_name)
            if re.fullmatch(r'[0-9a-f]{32}', target_dev):
                self.backup_device = target_dev
                self.backup_chosen = True
                if dev_name:
                    self.device_name = clean_device_name(dev_name)
                self._save_backup_config()
            games = int(restored.get('restored_games', 0))
            self.backup_status = tr("Restored from server · branch {0} · games: {1}.").format(self.backup_device[:8], games)
            self.backupRestored.emit(restored)
        self.changed.emit()

    def _backup_worker_failed(self, message):
        self.backup_status = str(message or tr("Server backup failed."))
        self.changed.emit()

    def _backup_worker_finished(self):
        worker = self.backup_worker
        self.backup_worker = None
        if worker is not None:
            worker.deleteLater()
        self.changed.emit()

    @Slot()
    def refreshBackups(self):
        if not self.authorized():
            return
        self._start_backup_worker('list', {}, tr("Loading device branches and backup history from your server…"))

    @Slot(str, str)
    def selectBackupDevice(self, device_id, device_name=''):
        dev = str(device_id or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{32}', dev):
            self.backup_status = tr("Invalid device branch ID.")
            self.changed.emit()
            return
        self.backup_device = dev
        self.backup_chosen = True
        if device_name and device_name.strip():
            self.device_name = clean_device_name(device_name)
        self._save_backup_config()
        self.backup_status = tr("Selected device branch {0} ({1}) for automatic backups.").format(dev[:8], self.device_name)
        self.changed.emit()

    @Slot(str, str)
    def backupNow(self, device_id='', device_name=''):
        if not self.authorized():
            self.backup_status = tr("Sign in to back up to your server.")
            self.changed.emit()
            return
        target = str(device_id or self.backup_device or self.device).strip().lower()
        if not re.fullmatch(r'[0-9a-f]{32}', target):
            target = self.device
        name = clean_device_name(device_name or self.device_name)
        self.backup_device = target
        self.backup_chosen = True
        self.device_name = name
        self._save_backup_config()
        self._start_backup_worker(
            'backup',
            {'target_device': target, 'device_name': name},
            tr("Backing up library and settings to your server (branch {0})…").format(target[:8]),
        )

    @Slot()
    def autoBackup(self):
        if not self.authorized() or self.backup_worker is not None:
            return
        devices, suggested = self._build_device_options()
        remote_with_backups = [it for it in devices if it.get('backups')]
        # Do not overwrite an unchosen branch on a clean install when remote backups exist.
        if not self.backup_chosen and remote_with_backups and count_local_games(self.storage) == 0:
            return
        target = self.backup_device if re.fullmatch(r'[0-9a-f]{32}', str(self.backup_device)) else self.device
        self.backup_chosen = True
        self._save_backup_config()
        self._start_backup_worker(
            'backup',
            {'target_device': target, 'device_name': self.device_name},
            tr("Automatic library and settings backup (branch {0})…").format(target[:8]),
        )

    @Slot(str, str, bool, bool)
    def restoreBackup(self, device_id, backup_id='', apply_settings=True, apply_library=True):
        if not self.authorized():
            self.backup_status = tr("Sign in to restore a backup.")
            self.changed.emit()
            return
        if self.parent() is not None and getattr(self.parent(), 'process', None) is not None:
            self.backup_status = tr("Close the running game before restoring the library.")
            self.changed.emit()
            return
        target = str(device_id or self.backup_device or '').strip().lower()
        devices, suggested = self._build_device_options()
        if not re.fullmatch(r'[0-9a-f]{32}', target):
            target = suggested
        dev_entry = next((it for it in devices if it['device'] == target), None)
        if not dev_entry or not dev_entry.get('backups'):
            self.backup_status = tr("No server backups exist for the selected device branch yet.")
            self.changed.emit()
            return
        rev_id = str(backup_id or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{32}', rev_id):
            rev_id = dev_entry['backups'][0]['id']
        self._start_backup_worker(
            'restore',
            {
                'target_device': target,
                'device_name': dev_entry.get('name') or self.device_name,
                'backup_id': rev_id,
                'apply_settings': bool(apply_settings),
                'apply_library': bool(apply_library),
            },
            tr("Restoring server backup (branch {0})…").format(target[:8]),
        )
