def ngs_compile(source):
    import functools, sys, types
    name = "_ngs_launcher_compiled_code"
    module = sys.modules.get(name)
    if module is None:
        candidate = types.ModuleType(name)
        candidate.compile = functools.lru_cache(maxsize=16)(compile)
        module = sys.modules.setdefault(name, candidate)
    # Cache only code, never request data, credentials, security objects or query results.
    return module.compile(source, "<ngs_launcher>", "exec")

"""Bounded JWE messages for the portable player account endpoint (RFC 7516)."""
import base64
import hashlib
import json
import re
import secrets
import time
import uuid

from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

PATH = '/tactic/ngs_launcher/ngs-players/v1/accounts'
PUBLIC_PATH = '/assets/ngs_launcher/players-public.json'
DOMAIN = b'NGS-Players-key-v1\n'
MAX_WIRE = 32768


class SessionExpired(ValueError):
    pass


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def decode(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]*', value):
        raise ValueError('Invalid encoding')
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')


def key_id(public):
    return hashlib.sha256(RSA.import_key(public).public_key().export_key(format='DER')).hexdigest()


def seal(payload, key, wrapped='', kid=''):
    header = {'alg': 'RSA-OAEP-256' if wrapped else 'dir', 'enc': 'A256GCM', 'typ': 'NGS-Players', 'kid': kid}
    aad = encode(compact(header))
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(aad.encode('ascii'))
    body = compact(payload)
    if len(body) > 20000: raise ValueError('Message too large')
    data, tag = cipher.encrypt_and_digest(body)
    return '.'.join((aad, wrapped, encode(nonce), encode(data), encode(tag))).encode('ascii')


def open_message(wire, key, kid, private=False):
    if not isinstance(wire, bytes) or not 0 < len(wire) <= MAX_WIRE: raise ValueError('Invalid message size')
    parts = wire.decode('ascii').split('.')
    if len(parts) != 5: raise ValueError('Invalid JWE')
    aad, wrapped, iv, body, tag = parts
    expected = {'alg': 'RSA-OAEP-256' if private else 'dir', 'enc': 'A256GCM', 'typ': 'NGS-Players', 'kid': kid}
    if json.loads(decode(aad)) != expected or bool(wrapped) != private: raise ValueError('Invalid JWE header')
    if private:
        encrypted = decode(wrapped)
        rsa = RSA.import_key(key)
        if len(encrypted) != rsa.size_in_bytes(): raise ValueError('Invalid wrapped key')
        key = PKCS1_OAEP.new(rsa, hashAlgo=SHA256).decrypt(encrypted)
    nonce, signature = decode(iv), decode(tag)
    if len(key) != 32 or len(nonce) != 12 or len(signature) != 16: raise ValueError('Invalid JWE parameters')
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(aad.encode('ascii'))
    value = json.loads(cipher.decrypt_and_verify(decode(body), signature))
    if not isinstance(value, dict): raise ValueError('Invalid payload')
    return value, key


def request(public, payload):
    key, identity = secrets.token_bytes(32), uuid.uuid4().hex
    kid = key_id(public)
    wrapped = encode(PKCS1_OAEP.new(RSA.import_key(public), hashAlgo=SHA256).encrypt(key))
    return seal(dict(payload, request_id=identity, timestamp=int(time.time())), key, wrapped, kid), key, identity


def username(value):
    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}', value):
        raise ValueError("Username: 3–32 Latin letters, digits, periods, hyphens or underscores.")
    return value.lower()


def password_hash(password, salt):
    if not isinstance(password, str) or not 8 <= len(password) <= 128 or len(password.encode('utf8')) > 512:
        raise ValueError("The password must contain 8 to 128 characters.")
    return hashlib.scrypt(password.encode('utf8'), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64*1024*1024).hex()


def session_row(row, now):
    fields = {'id', 'game', 'name', 'started_at', 'updated_at', 'ended_at', 'active_ms', 'paused_ms'}
    if not isinstance(row, dict) or set(row) != fields: raise ValueError('Invalid session fields')
    if not re.fullmatch('[0-9a-f]{32}', row['id']) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,95}', row['game']):
        raise ValueError('Invalid session identity')
    if not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 256 or any(ord(c) < 32 for c in row['name']):
        raise ValueError('Invalid game name')
    for field in ('started_at', 'updated_at', 'active_ms', 'paused_ms'):
        if type(row[field]) is not int or row[field] < 0: raise ValueError('Invalid session counter')
    if not 946684800 <= row['started_at'] <= row['updated_at'] <= now + 300: raise ValueError('Invalid session dates')
    if row['ended_at'] is not None and (type(row['ended_at']) is not int or not row['started_at'] <= row['ended_at'] <= row['updated_at']):
        raise ValueError('Invalid session end')
    if row['active_ms'] + row['paused_ms'] > (row['updated_at'] - row['started_at'] + 300) * 1000:
        raise ValueError('Invalid session duration')
    return row

def service_session(service="metadata"):
    from contextlib import contextmanager
    import copy
    from pyasm.common import Environment
    from pyasm.biz import Project
    from pyasm.search import Search
    from pyasm.security import Security, Ticket
    from tactic_client_lib import TacticServerStub
    @contextmanager
    def scope():
        previous = Environment.get_security()
        previous_project = Project.get_project_code()
        server = TacticServerStub.get(protocol="local")
        if server.get_protocol() != "local":
            raise PermissionError("Only in-process TACTIC execution is allowed")
        saved = {name: copy.deepcopy(getattr(server, name, None)) for name in ("ticket", "login_ticket", "login", "handoff_dir")}
        try:
            if service not in ("metadata", "catalog", "players"):
                raise PermissionError("Unknown NGS service")
            login = {"metadata": "ngs_metadata_worker", "catalog": "ngs_catalog_worker", "players": "ngs_players_worker"}[service]
            query = Search("ngs_launcher/api_service"); query.add_filter("code", service)
            credential = query.get_sobject()
            if not credential or credential.get_value("login") != login:
                raise PermissionError("NGS service account is not configured")
            security = Security()
            query = Search("sthpw/ticket"); query.add_filter("login", login)
            query.add_order_by("timestamp desc"); query.set_limit(1)
            candidate = query.get_sobject()
            ticket = Ticket.get_by_valid_key(candidate.get_value("ticket")) if candidate else None
            if ticket:
                security.login_with_ticket(ticket.get_value("ticket"))
            else:
                security.login_user(login, credential.get_value("password"))
            Project.set_project(previous_project)
            if not security.is_logged_in() or security.is_admin():
                raise PermissionError("NGS worker must be authenticated and non-administrative")
            Environment.set_security(security)
            server.set_ticket(security.get_ticket_key())
            yield
        finally:
            for name, value in saved.items(): setattr(server, name, value)
            Environment.set_security(previous)
            Project.set_project(previous_project)
    return scope()

def player_web(action_source, grant_source):
    import cherrypy, json
    from pyasm.biz import Project
    from pyasm.command import Command
    from tactic.command import PythonCmd
    request = cherrypy.request
    result = {'status': 400}
    try:
        p = Project.get()
        if (p.get_code() != 'ngs_launcher' or p.get_database_name() != 'ngs_launcher'
                or p.get_value('description') != 'NGS handheld game catalog; managed from D:/code/NGS'):
            raise ValueError('Wrong project')
        length = request.headers.get('Content-Length', '')
        if (request.path_info != PATH or request.query_string or request.method != 'POST'
                or request.headers.get('Content-Type', '').split(';')[0] != 'application/jose'
                or not length.isdigit() or not 0 < int(length) <= MAX_WIRE or request.headers.get('Transfer-Encoding')):
            raise ValueError('Invalid request')
        wire = request.body.read(MAX_WIRE + 1)
        if len(wire) != int(length): raise ValueError('Invalid length')
        with service_session('players'):
            from ngs_launcher_plugin.request_peer import client_address
            cmd = PythonCmd(code=action_source, wire=wire.decode('ascii'), address=client_address(request))
            Command.execute_cmd(cmd); result = cmd.get_info()
        if result.get('grant'):
            with service_session('catalog'):
                cmd = PythonCmd(code=grant_source, grant=result['grant']); Command.execute_cmd(cmd)
    except Exception:
        # No credentials, ciphertext or TACTIC exception pages in the response or logs.
        result = {'status': 503}
    cherrypy.response.status = result['status']
    cherrypy.response.headers['Content-Type'] = 'application/jose'
    cherrypy.response.headers['Cache-Control'] = 'no-store'
    body = result.get('wire', '').encode('ascii')
    def response_hook(): cherrypy.response.body = [body]
    cherrypy.request.hooks.attach('before_finalize', response_hook)
    return {}

PLAYER_ACTION_SOURCE = "\"\"\"Bounded JWE messages for the portable player account endpoint (RFC 7516).\"\"\"\nimport base64\nimport hashlib\nimport json\nimport re\nimport secrets\nimport time\nimport uuid\n\nfrom Crypto.Cipher import AES, PKCS1_OAEP\nfrom Crypto.Hash import SHA256\nfrom Crypto.PublicKey import RSA\n\nPATH = '/tactic/ngs_launcher/ngs-players/v1/accounts'\nPUBLIC_PATH = '/assets/ngs_launcher/players-public.json'\nDOMAIN = b'NGS-Players-key-v1\\n'\nMAX_WIRE = 32768\n\n\nclass SessionExpired(ValueError):\n    pass\n\n\ndef encode(value):\n    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')\n\n\ndef decode(value):\n    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]*', value):\n        raise ValueError('Invalid encoding')\n    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)\n\n\ndef compact(value):\n    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')\n\n\ndef key_id(public):\n    return hashlib.sha256(RSA.import_key(public).public_key().export_key(format='DER')).hexdigest()\n\n\ndef seal(payload, key, wrapped='', kid=''):\n    header = {'alg': 'RSA-OAEP-256' if wrapped else 'dir', 'enc': 'A256GCM', 'typ': 'NGS-Players', 'kid': kid}\n    aad = encode(compact(header))\n    nonce = secrets.token_bytes(12)\n    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)\n    cipher.update(aad.encode('ascii'))\n    body = compact(payload)\n    if len(body) > 20000: raise ValueError('Message too large')\n    data, tag = cipher.encrypt_and_digest(body)\n    return '.'.join((aad, wrapped, encode(nonce), encode(data), encode(tag))).encode('ascii')\n\n\ndef open_message(wire, key, kid, private=False):\n    if not isinstance(wire, bytes) or not 0 < len(wire) <= MAX_WIRE: raise ValueError('Invalid message size')\n    parts = wire.decode('ascii').split('.')\n    if len(parts) != 5: raise ValueError('Invalid JWE')\n    aad, wrapped, iv, body, tag = parts\n    expected = {'alg': 'RSA-OAEP-256' if private else 'dir', 'enc': 'A256GCM', 'typ': 'NGS-Players', 'kid': kid}\n    if json.loads(decode(aad)) != expected or bool(wrapped) != private: raise ValueError('Invalid JWE header')\n    if private:\n        encrypted = decode(wrapped)\n        rsa = RSA.import_key(key)\n        if len(encrypted) != rsa.size_in_bytes(): raise ValueError('Invalid wrapped key')\n        key = PKCS1_OAEP.new(rsa, hashAlgo=SHA256).decrypt(encrypted)\n    nonce, signature = decode(iv), decode(tag)\n    if len(key) != 32 or len(nonce) != 12 or len(signature) != 16: raise ValueError('Invalid JWE parameters')\n    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)\n    cipher.update(aad.encode('ascii'))\n    value = json.loads(cipher.decrypt_and_verify(decode(body), signature))\n    if not isinstance(value, dict): raise ValueError('Invalid payload')\n    return value, key\n\n\ndef request(public, payload):\n    key, identity = secrets.token_bytes(32), uuid.uuid4().hex\n    kid = key_id(public)\n    wrapped = encode(PKCS1_OAEP.new(RSA.import_key(public), hashAlgo=SHA256).encrypt(key))\n    return seal(dict(payload, request_id=identity, timestamp=int(time.time())), key, wrapped, kid), key, identity\n\n\ndef username(value):\n    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}', value):\n        raise ValueError(\"Username: 3–32 Latin letters, digits, periods, hyphens or underscores.\")\n    return value.lower()\n\n\ndef password_hash(password, salt):\n    if not isinstance(password, str) or not 8 <= len(password) <= 128 or len(password.encode('utf8')) > 512:\n        raise ValueError(\"The password must contain 8 to 128 characters.\")\n    return hashlib.scrypt(password.encode('utf8'), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64*1024*1024).hex()\n\n\ndef session_row(row, now):\n    fields = {'id', 'game', 'name', 'started_at', 'updated_at', 'ended_at', 'active_ms', 'paused_ms'}\n    if not isinstance(row, dict) or set(row) != fields: raise ValueError('Invalid session fields')\n    if not re.fullmatch('[0-9a-f]{32}', row['id']) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,95}', row['game']):\n        raise ValueError('Invalid session identity')\n    if not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 256 or any(ord(c) < 32 for c in row['name']):\n        raise ValueError('Invalid game name')\n    for field in ('started_at', 'updated_at', 'active_ms', 'paused_ms'):\n        if type(row[field]) is not int or row[field] < 0: raise ValueError('Invalid session counter')\n    if not 946684800 <= row['started_at'] <= row['updated_at'] <= now + 300: raise ValueError('Invalid session dates')\n    if row['ended_at'] is not None and (type(row['ended_at']) is not int or not row['started_at'] <= row['ended_at'] <= row['updated_at']):\n        raise ValueError('Invalid session end')\n    if row['active_ms'] + row['paused_ms'] > (row['updated_at'] - row['started_at'] + 300) * 1000:\n        raise ValueError('Invalid session duration')\n    return row\n\ndef player_action(wire, address):\n    import hashlib, hmac, json, re, secrets, time, uuid\n    from pyasm.biz import Project\n    from pyasm.common import Environment\n    from pyasm.search import Search, SearchType, DbContainer\n    p = Project.get()\n    if (p.get_code() != 'ngs_launcher' or p.get_database_name() != 'ngs_launcher'\n            or Environment.get_security().is_admin() or Environment.get_security().get_login().get_value('login') != 'ngs_players_worker'):\n        raise PermissionError('NGS players worker required')\n    sql = DbContainer.get(p.get_project_db_resource())\n    sql.do_query('SELECT pg_advisory_xact_lock(1313297225)')\n    def find(table, code):\n        q = Search('ngs_launcher/' + table); q.add_filter('code', code); return q.get_sobject()\n    def data(obj): return json.loads(obj.get_value('data')) if obj else {}\n    def save(table, code, value, **fields):\n        obj = find(table, code) or SearchType.create('ngs_launcher/' + table)\n        for k, v in dict(fields, code=code, data=json.dumps(value, separators=(',', ':'))).items(): obj.set_value(k, v)\n        obj.commit(triggers='none', log_transaction=False)\n        return obj\n    now = int(time.time())\n    sql.do_update(\"DELETE FROM player_gate WHERE (data::json->>'expires')::bigint < %d\" % now)\n    sql.do_update(\"DELETE FROM player_login WHERE (data::json->>'expires')::bigint < %d\" % now)\n    # These records replace expired windows; untrusted callers cannot grow a nonce list indefinitely.\n    def gate(code, limit, window):\n        obj = find('player_gate', code); old = data(obj)\n        if not obj and sql.do_query('SELECT COUNT(*) FROM player_gate')[0][0] >= 10000: return False\n        stamps = [t for t in old.get('times', []) if t > now - window]\n        if len(stamps) >= limit: return False\n        stamps.append(now); save('player_gate', code, {'times': stamps, 'expires': now + window})\n        return True\n    ip = hashlib.sha256(address.encode('utf8')).hexdigest()\n    if not gate('ip-' + ip, 120, 60): return {'status': 429}\n    config = data(find('player_config', 'crypto'))\n    key, identity, grant = None, '', None\n    try:\n        value, key = open_message(wire.encode('ascii'), config['private'], config['kid'], private=True)\n        identity = value.get('request_id', '')\n        if not re.fullmatch('[0-9a-f]{32}', identity) or type(value.get('timestamp')) is not int or abs(now-value['timestamp']) > 90:\n            raise ValueError('Expired request')\n        replay_code = 'replay-' + ip\n        old = data(find('player_gate', replay_code))\n        nonces = {k: t for k, t in old.get('nonces', {}).items() if t > now - 180}\n        if identity in nonces: raise ValueError('Repeated request')\n        if len(nonces) >= 400: raise ValueError('Too many requests')\n        nonces[identity] = now; save('player_gate', replay_code, {'nonces': nonces, 'expires': now+180})\n        operation, device = value.get('action'), value.get('device', '')\n        if not re.fullmatch('[0-9a-f]{32}', device): raise ValueError('Invalid device')\n        common = {'request_id', 'timestamp', 'action', 'device'}\n        if operation == 'register':\n            if set(value) != common | {'username', 'password'}: raise ValueError('Invalid registration fields')\n            policy = data(find('player_config', 'registration'))\n            if policy.get('enabled') is not True: raise ValueError(\"The server owner has disabled registration.\")\n            if not gate('register-' + ip, 3, 3600) or not gate('registrations', 100, 86400):\n                raise ValueError(\"Too many registrations. Try again later.\")\n            name = username(value['username'])\n            if name in ('admin', 'owner') or find('player_profile', name):\n                raise ValueError(\"Could not create the profile. Choose another name or sign in.\")\n            if not isinstance(value['password'], str) or len(value['password']) < 12:\n                raise ValueError(\"Registration requires a password of 12–128 characters.\")\n            if sql.do_query('SELECT COUNT(*) FROM player_profile')[0][0] >= 1000:\n                raise ValueError(\"The server profile limit has been reached. Contact its owner.\")\n            salt = secrets.token_hex(16)\n            profile = dict(id=uuid.uuid4().hex, username=name, salt=salt,\n                           password=password_hash(value['password'], salt), role='player', enabled=True)\n            save('player_profile', name, profile)\n            token = secrets.token_hex(32)\n            save('player_login', hashlib.sha256(token.encode()).hexdigest(),\n                 dict(profile=profile['id'], device=device, expires=now + 30*86400))\n            result = dict(profile=profile['id'], username=name, token=token, expires=now + 30*86400)\n        elif operation in ('login', 'web-login'):\n            if set(value) != common | {'username', 'password'}: raise ValueError('Invalid login fields')\n            name = username(value['username'])\n            if not gate('login-' + hashlib.sha256((ip+name).encode()).hexdigest(), 10, 900):\n                raise ValueError(\"Too many sign-in attempts. Wait 15 minutes.\")\n            account = find('player_profile', name); profile = data(account)\n            salt = profile.get('salt', '00' * 16)\n            hashed = password_hash(value['password'], salt)\n            if (not profile or profile.get('enabled') is False or not hmac.compare_digest(hashed, profile['password'])\n                    or operation == 'web-login' and profile.get('role', 'owner') != 'owner'):\n                raise ValueError(\"Incorrect username or password.\")\n            token = secrets.token_hex(32)\n            token_id = hashlib.sha256(token.encode()).hexdigest()\n            expires = now + (900 if operation == 'web-login' else 30*86400)\n            save('player_login', token_id, dict(profile=profile['id'], device=device, expires=expires,\n                 username=name, purpose='web-admin' if operation == 'web-login' else 'player',\n                 password_version=hashlib.sha256(profile['password'].encode()).hexdigest()))\n            result = dict(profile=profile['id'], username=name, token=token, expires=expires)\n        else:\n            if operation not in ('sync', 'pull', 'logout', 'authorize'): raise ValueError('Unknown action')\n            token = value.get('token', '')\n            if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token): raise ValueError(\"Sign in to your profile.\")\n            token_id = hashlib.sha256(token.encode()).hexdigest()\n            login = data(find('player_login', token_id))\n            if not login or login['expires'] <= now: raise SessionExpired(\"Sign in to NGS again.\")\n            profile = login['profile']\n            if operation == 'authorize':\n                if set(value) != common | {'token'}: raise ValueError('Invalid authorization fields')\n                profiles = [data(x) for x in Search('ngs_launcher/player_profile').get_sobjects()]\n                owner = next((x for x in profiles if x.get('id') == profile), {})\n                if not owner or owner.get('enabled') is False: raise SessionExpired(\"Sign in to NGS again.\")\n                apis = login.setdefault('apis', {})\n                if device not in apis:\n                    if len(apis) >= 256: raise ValueError(\"Sign in again to start a new NGS session.\")\n                    identity_api = hashlib.sha256(('NGS-account-api-v1\\0' + profile + device).encode()).hexdigest()[:32]\n                    apis[device] = dict(device=identity_api, secret=secrets.token_hex(32))\n                    save('player_login', token_id, login)\n                result = dict(apis[device], expires=login['expires'])\n                grant = dict(apis[device], login=token_id, profile=profile, username=owner['username'],\n                             client_device=device, expires=login['expires'])\n            elif operation == 'logout':\n                if set(value) != common | {'token'}: raise ValueError('Invalid logout fields')\n                login['expires'] = 0; save('player_login', token_id, login); result = {}\n                if login.get('apis', {}).get(device):\n                    profiles = [data(x) for x in Search('ngs_launcher/player_profile').get_sobjects()]\n                    owner = next((x for x in profiles if x.get('id') == profile), {})\n                    grant = dict(login['apis'][device], login=token_id, profile=profile, username=owner.get('username', ''),\n                                 client_device=device, expires=0, revoke=True)\n            else:\n                if set(value) != common | {'token', 'rows', 'offset'}: raise ValueError('Invalid sync fields')\n                rows, offset = value['rows'], value['offset']\n                if (not isinstance(rows, list) or len(rows) > 16 or type(offset) is not int or not 0 <= offset <= 100000\n                        or operation == 'pull' and rows): raise ValueError('Invalid sync size')\n                checked = [session_row(r, now) for r in rows]\n                # Validate the entire batch before changing session records.\n                for row in checked:\n                    old = find('player_play', row['id'])\n                    if old and (old.get_value('profile') != profile or old.get_value('device') != device\n                                or old.get_value('game') != row['game'] or data(old)['started_at'] != row['started_at']):\n                        raise ValueError('Session belongs to another profile or device')\n                for row in checked:\n                    old = data(find('player_play', row['id']))\n                    if old:\n                        row = dict(row, active_ms=max(row['active_ms'], old['active_ms']), paused_ms=max(row['paused_ms'], old['paused_ms']),\n                                   updated_at=max(row['updated_at'], old['updated_at']), ended_at=old['ended_at'] or row['ended_at'])\n                    save('player_play', row['id'], row, profile=profile, device=device, game=row['game'])\n                # profile is a generated hex ID, never SQL supplied by the client.\n                if not re.fullmatch('[0-9a-f]{32}', profile): raise ValueError('Invalid stored profile')\n                aggregate = (\"SELECT game,COUNT(*),SUM((data::json->>'active_ms')::bigint),MAX((data::json->>'active_ms')::bigint) \"\n                             \"FROM player_play WHERE profile='%s'\" % profile)\n                if operation == 'sync':\n                    games = sorted({r['game'] for r in checked})\n                    aggregate += \" AND game IN (%s)\" % ','.join(\"'%s'\" % g for g in games) if games else ' AND false'\n                aggregate += ' GROUP BY game ORDER BY game'\n                if operation == 'pull': aggregate += ' LIMIT 50 OFFSET %d' % offset\n                totals = [dict(game=r[0], launches=int(r[1]), active_ms=int(r[2]), longest_ms=int(r[3])) for r in sql.do_query(aggregate)]\n                result = {'totals': totals, 'acked': [r['id'] for r in checked], 'more': operation == 'pull' and len(totals) == 50}\n        payload = {'request_id': identity, 'ok': True, 'result': result}\n    except (ValueError, TypeError, KeyError, UnicodeError) as error:\n        if key is None: return {'status': 400}\n        # Only deliberate validation messages are exposed; never echo payloads or crypto failures.\n        message = str(error)\n        if not message or not any('\\u0400' <= c <= '\\u04ff' for c in message): message = \"Profile request rejected.\"\n        payload = {'request_id': identity, 'ok': False, 'error': message}\n        if isinstance(error, SessionExpired): payload['code'] = 'auth'\n    response = {'status': 200, 'wire': seal(payload, key, kid=config['kid']).decode('ascii')}\n    if grant is not None and payload['ok']: response['grant'] = grant\n    return response\n\nreturn player_action(wire, address)"

def ngs_entry():
    return player_web(PLAYER_ACTION_SOURCE,"def grant_account_device(grant):\n    import json, re\n    from pyasm.biz import Project\n    from pyasm.common import Environment\n    from pyasm.search import Search, SearchType, DbContainer\n    if (Project.get().get_code() != 'ngs_launcher' or Environment.get_security().is_admin()\n            or Environment.get_security().get_login().get_value('login') != 'ngs_catalog_worker'):\n        raise PermissionError('NGS catalog worker required')\n    if (not re.fullmatch('[0-9a-f]{32}', grant['device']) or not re.fullmatch('[0-9a-f]{64}', grant['secret'])\n            or not re.fullmatch('[0-9a-f]{64}', grant['login'])): raise ValueError('Invalid account grant')\n    DbContainer.get(Project.get().get_project_db_resource()).do_query('SELECT pg_advisory_xact_lock(1313297220)')\n    q = Search('ngs_launcher/api_device'); q.add_filter('code', grant['device']); obj = q.get_sobject()\n    state = json.loads(obj.get_value('state') or '{}') if obj else {}\n    previous = state.get('account', {})\n    if obj and (previous.get('profile') != grant['profile'] or previous.get('client_device') != grant['client_device']):\n        raise ValueError('Account device belongs to another owner')\n    if grant.get('revoke') and (not obj or previous.get('login') != grant['login']): return {}\n    if obj and obj.get_value('enabled') is not True and not state.get('account_logout'):\n        raise ValueError(\"This device's access was revoked by the administrator.\")\n    if not obj: obj = SearchType.create('ngs_launcher/api_device')\n    state.update(account={k: grant[k] for k in ('login', 'profile', 'username', 'client_device', 'device', 'expires')},\n                 account_logout=bool(grant.get('revoke')))\n    for key, value in dict(code=grant['device'], name='NGS · ' + grant['username'], secret=grant['secret'], enabled=not grant.get('revoke', False),\n                           state=json.dumps(state, separators=(',', ':'))).items(): obj.set_value(key, value)\n    obj.commit(triggers='none', log_transaction=False)\n    return {}\n\nreturn grant_account_device(grant)")
