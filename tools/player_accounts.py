"""Install only the isolated ngs_launcher player account service."""
import inspect
import json
from pathlib import Path

from ngs import player_protocol
from ngs.catalog import atomic_write
from ngs.portable import PortableStorage
from ngs.tactic import TacticClient
from ngs.update_crypto import RsaKey
from tools.metadata_api import OWNER as API_OWNER, service_session
from tools.publish_update import signing_key
from tools.server_code import packed_script
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password

OWNER = 'NGS player accounts v1; managed from D:/code/NGS'


def account_api_allowed(account, operation=None):
    import json, time
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import Search
    if (Project.get().get_code() != 'ngs_launcher' or Environment.get_security().is_admin()
            or Environment.get_security().get_login().get_value('login') != 'ngs_players_worker'):
        raise PermissionError('NGS players worker required')
    def read(table, code):
        q = Search('ngs_launcher/' + table); q.add_filter('code', code); obj = q.get_sobject()
        return json.loads(obj.get_value('data')) if obj else {}
    login = read('player_login', account.get('login', ''))
    if (login.get('expires', 0) <= int(time.time()) or login.get('profile') != account.get('profile')
            or login.get('apis', {}).get(account.get('client_device'), {}).get('device') != account.get('device')):
        return False
    profile = read('player_profile', account.get('username', ''))
    if profile.get('id') != login['profile'] or profile.get('enabled') is False:
        return False
    # Profiles created before self-service registration belonged to trusted NAS operators.
    return operation != 'catalog' or profile.get('role', 'owner') == 'owner'


def grant_account_device(grant):
    import json, re
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import Search, SearchType, DbContainer
    if (Project.get().get_code() != 'ngs_launcher' or Environment.get_security().is_admin()
            or Environment.get_security().get_login().get_value('login') != 'ngs_catalog_worker'):
        raise PermissionError('NGS catalog worker required')
    if (not re.fullmatch('[0-9a-f]{32}', grant['device']) or not re.fullmatch('[0-9a-f]{64}', grant['secret'])
            or not re.fullmatch('[0-9a-f]{64}', grant['login'])): raise ValueError('Invalid account grant')
    DbContainer.get(Project.get().get_project_db_resource()).do_query('SELECT pg_advisory_xact_lock(1313297220)')
    q = Search('ngs_launcher/api_device'); q.add_filter('code', grant['device']); obj = q.get_sobject()
    state = json.loads(obj.get_value('state') or '{}') if obj else {}
    previous = state.get('account', {})
    if obj and (previous.get('profile') != grant['profile'] or previous.get('client_device') != grant['client_device']):
        raise ValueError('Account device belongs to another owner')
    if grant.get('revoke') and (not obj or previous.get('login') != grant['login']): return {}
    if obj and obj.get_value('enabled') is not True and not state.get('account_logout'):
        raise ValueError("This device's access was revoked by the administrator.")
    if not obj: obj = SearchType.create('ngs_launcher/api_device')
    state.update(account={k: grant[k] for k in ('login', 'profile', 'username', 'client_device', 'device', 'expires')},
                 account_logout=bool(grant.get('revoke')))
    for key, value in dict(code=grant['device'], name='NGS · ' + grant['username'], secret=grant['secret'], enabled=not grant.get('revoke', False),
                           state=json.dumps(state, separators=(',', ':'))).items(): obj.set_value(key, value)
    obj.commit(triggers='none', log_transaction=False)
    return {}


def player_action(wire, address):
    import hashlib, hmac, json, re, secrets, time, uuid
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import Search, SearchType, DbContainer
    p = Project.get()
    if (p.get_code() != 'ngs_launcher' or p.get_database_name() != 'ngs_launcher'
            or Environment.get_security().is_admin() or Environment.get_security().get_login().get_value('login') != 'ngs_players_worker'):
        raise PermissionError('NGS players worker required')
    sql = DbContainer.get(p.get_project_db_resource())
    sql.do_query('SELECT pg_advisory_xact_lock(1313297225)')
    def find(table, code):
        q = Search('ngs_launcher/' + table); q.add_filter('code', code); return q.get_sobject()
    def data(obj): return json.loads(obj.get_value('data')) if obj else {}
    def save(table, code, value, **fields):
        obj = find(table, code) or SearchType.create('ngs_launcher/' + table)
        for k, v in dict(fields, code=code, data=json.dumps(value, separators=(',', ':'))).items(): obj.set_value(k, v)
        obj.commit(triggers='none', log_transaction=False)
        return obj
    now = int(time.time())
    sql.do_update("DELETE FROM player_gate WHERE (data::json->>'expires')::bigint < %d" % now)
    sql.do_update("DELETE FROM player_login WHERE (data::json->>'expires')::bigint < %d" % now)
    # These records replace expired windows; untrusted callers cannot grow a nonce list indefinitely.
    def gate(code, limit, window):
        obj = find('player_gate', code); old = data(obj)
        if not obj and sql.do_query('SELECT COUNT(*) FROM player_gate')[0][0] >= 10000: return False
        stamps = [t for t in old.get('times', []) if t > now - window]
        if len(stamps) >= limit: return False
        stamps.append(now); save('player_gate', code, {'times': stamps, 'expires': now + window})
        return True
    ip = hashlib.sha256(address.encode('utf8')).hexdigest()
    if not gate('ip-' + ip, 120, 60): return {'status': 429}
    config = data(find('player_config', 'crypto'))
    key, identity, grant = None, '', None
    try:
        value, key = open_message(wire.encode('ascii'), config['private'], config['kid'], private=True)
        identity = value.get('request_id', '')
        if not re.fullmatch('[0-9a-f]{32}', identity) or type(value.get('timestamp')) is not int or abs(now-value['timestamp']) > 90:
            raise ValueError('Expired request')
        replay_code = 'replay-' + ip
        old = data(find('player_gate', replay_code))
        nonces = {k: t for k, t in old.get('nonces', {}).items() if t > now - 180}
        if identity in nonces: raise ValueError('Repeated request')
        if len(nonces) >= 400: raise ValueError('Too many requests')
        nonces[identity] = now; save('player_gate', replay_code, {'nonces': nonces, 'expires': now+180})
        operation, device = value.get('action'), value.get('device', '')
        if not re.fullmatch('[0-9a-f]{32}', device): raise ValueError('Invalid device')
        common = {'request_id', 'timestamp', 'action', 'device'}
        if operation == 'register':
            if set(value) != common | {'username', 'password'}: raise ValueError('Invalid registration fields')
            policy = data(find('player_config', 'registration'))
            if policy.get('enabled') is not True: raise ValueError("The server owner has disabled registration.")
            if not gate('register-' + ip, 3, 3600) or not gate('registrations', 100, 86400):
                raise ValueError("Too many registrations. Try again later.")
            name = username(value['username'])
            if name in ('admin', 'owner') or find('player_profile', name):
                raise ValueError("Could not create the profile. Choose another name or sign in.")
            if not isinstance(value['password'], str) or len(value['password']) < 12:
                raise ValueError("Registration requires a password of 12–128 characters.")
            if sql.do_query('SELECT COUNT(*) FROM player_profile')[0][0] >= 1000:
                raise ValueError("The server profile limit has been reached. Contact its owner.")
            salt = secrets.token_hex(16)
            profile = dict(id=uuid.uuid4().hex, username=name, salt=salt,
                           password=password_hash(value['password'], salt), role='player', enabled=True)
            save('player_profile', name, profile)
            token = secrets.token_hex(32)
            save('player_login', hashlib.sha256(token.encode()).hexdigest(),
                 dict(profile=profile['id'], device=device, expires=now + 30*86400))
            result = dict(profile=profile['id'], username=name, token=token, expires=now + 30*86400)
        elif operation in ('login', 'web-login'):
            if set(value) != common | {'username', 'password'}: raise ValueError('Invalid login fields')
            name = username(value['username'])
            if not gate('login-' + hashlib.sha256((ip+name).encode()).hexdigest(), 10, 900):
                raise ValueError("Too many sign-in attempts. Wait 15 minutes.")
            account = find('player_profile', name); profile = data(account)
            salt = profile.get('salt', '00' * 16)
            hashed = password_hash(value['password'], salt)
            if (not profile or profile.get('enabled') is False or not hmac.compare_digest(hashed, profile['password'])
                    or operation == 'web-login' and profile.get('role', 'owner') != 'owner'):
                raise ValueError("Incorrect username or password.")
            token = secrets.token_hex(32)
            token_id = hashlib.sha256(token.encode()).hexdigest()
            expires = now + (900 if operation == 'web-login' else 30*86400)
            save('player_login', token_id, dict(profile=profile['id'], device=device, expires=expires,
                 username=name, purpose='web-admin' if operation == 'web-login' else 'player',
                 password_version=hashlib.sha256(profile['password'].encode()).hexdigest()))
            result = dict(profile=profile['id'], username=name, token=token, expires=expires)
        else:
            if operation not in ('sync', 'pull', 'logout', 'authorize'): raise ValueError('Unknown action')
            token = value.get('token', '')
            if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token): raise ValueError("Sign in to your profile.")
            token_id = hashlib.sha256(token.encode()).hexdigest()
            login = data(find('player_login', token_id))
            if not login or login['expires'] <= now: raise SessionExpired("Sign in to NGS again.")
            profile = login['profile']
            if operation == 'authorize':
                if set(value) != common | {'token'}: raise ValueError('Invalid authorization fields')
                profiles = [data(x) for x in Search('ngs_launcher/player_profile').get_sobjects()]
                owner = next((x for x in profiles if x.get('id') == profile), {})
                if not owner or owner.get('enabled') is False: raise SessionExpired("Sign in to NGS again.")
                apis = login.setdefault('apis', {})
                if device not in apis:
                    if len(apis) >= 256: raise ValueError("Sign in again to start a new NGS session.")
                    identity_api = hashlib.sha256(('NGS-account-api-v1\0' + profile + device).encode()).hexdigest()[:32]
                    apis[device] = dict(device=identity_api, secret=secrets.token_hex(32))
                    save('player_login', token_id, login)
                result = dict(apis[device], expires=login['expires'])
                grant = dict(apis[device], login=token_id, profile=profile, username=owner['username'],
                             client_device=device, expires=login['expires'])
            elif operation == 'logout':
                if set(value) != common | {'token'}: raise ValueError('Invalid logout fields')
                login['expires'] = 0; save('player_login', token_id, login); result = {}
                if login.get('apis', {}).get(device):
                    profiles = [data(x) for x in Search('ngs_launcher/player_profile').get_sobjects()]
                    owner = next((x for x in profiles if x.get('id') == profile), {})
                    grant = dict(login['apis'][device], login=token_id, profile=profile, username=owner.get('username', ''),
                                 client_device=device, expires=0, revoke=True)
            else:
                if set(value) != common | {'token', 'rows', 'offset'}: raise ValueError('Invalid sync fields')
                rows, offset = value['rows'], value['offset']
                if (not isinstance(rows, list) or len(rows) > 16 or type(offset) is not int or not 0 <= offset <= 100000
                        or operation == 'pull' and rows): raise ValueError('Invalid sync size')
                checked = [session_row(r, now) for r in rows]
                # Validate the entire batch before changing session records.
                for row in checked:
                    old = find('player_play', row['id'])
                    if old and (old.get_value('profile') != profile or old.get_value('device') != device
                                or old.get_value('game') != row['game'] or data(old)['started_at'] != row['started_at']):
                        raise ValueError('Session belongs to another profile or device')
                for row in checked:
                    old = data(find('player_play', row['id']))
                    if old:
                        row = dict(row, active_ms=max(row['active_ms'], old['active_ms']), paused_ms=max(row['paused_ms'], old['paused_ms']),
                                   updated_at=max(row['updated_at'], old['updated_at']), ended_at=old['ended_at'] or row['ended_at'])
                    save('player_play', row['id'], row, profile=profile, device=device, game=row['game'])
                # profile is a generated hex ID, never SQL supplied by the client.
                if not re.fullmatch('[0-9a-f]{32}', profile): raise ValueError('Invalid stored profile')
                aggregate = ("SELECT game,COUNT(*),SUM((data::json->>'active_ms')::bigint),MAX((data::json->>'active_ms')::bigint) "
                             "FROM player_play WHERE profile='%s'" % profile)
                if operation == 'sync':
                    games = sorted({r['game'] for r in checked})
                    aggregate += " AND game IN (%s)" % ','.join("'%s'" % g for g in games) if games else ' AND false'
                aggregate += ' GROUP BY game ORDER BY game'
                if operation == 'pull': aggregate += ' LIMIT 50 OFFSET %d' % offset
                totals = [dict(game=r[0], launches=int(r[1]), active_ms=int(r[2]), longest_ms=int(r[3])) for r in sql.do_query(aggregate)]
                result = {'totals': totals, 'acked': [r['id'] for r in checked], 'more': operation == 'pull' and len(totals) == 50}
        payload = {'request_id': identity, 'ok': True, 'result': result}
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        if key is None: return {'status': 400}
        # Only deliberate validation messages are exposed; never echo payloads or crypto failures.
        message = str(error)
        if not message or not any('\u0400' <= c <= '\u04ff' for c in message): message = "Profile request rejected."
        payload = {'request_id': identity, 'ok': False, 'error': message}
        if isinstance(error, SessionExpired): payload['code'] = 'auth'
    response = {'status': 200, 'wire': seal(payload, key, kid=config['kid']).decode('ascii')}
    if grant is not None and payload['ok']: response['grant'] = grant
    return response


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


def sources():
    protocol = Path(player_protocol.__file__).read_text(encoding='utf8')
    action = protocol + '\n' + inspect.getsource(player_action) + '\nreturn player_action(wire, address)'
    web = protocol + '\n' + inspect.getsource(service_session) + '\n' + inspect.getsource(player_web)
    grant = inspect.getsource(grant_account_device) + '\nreturn grant_account_device(grant)'
    web += '\nPLAYER_ACTION_SOURCE = ' + repr(action) + '\n'
    return packed_script(web, 'player_web(PLAYER_ACTION_SOURCE,%r)' % grant)


def deploy(client, storage):
    result = client.script('''
import json,secrets
from Crypto.PublicKey import RSA
from pyasm.biz import Project
from pyasm.common import Environment,Container
from pyasm.search import Search,SearchType,DbContainer
from pyasm.security import Login
p=Project.get()
if (not Environment.get_security().is_admin() or p.get_code()!='ngs_launcher' or p.get_database_name()!='ngs_launcher'
    or p.get_value('description')!='NGS handheld game catalog; managed from D:/code/NGS'): raise PermissionError('NGS only')
def find(kind,field,value):
    q=Search(kind);q.add_filter(field,value);return q.get_sobject()
sql=DbContainer.get(p.get_project_db_resource());sql.do_query('SELECT pg_advisory_xact_lock(1313297225)')
for table in ('player_profile','player_login','player_play','player_config','player_gate'):
    registry=find('sthpw/search_object','search_type','ngs_launcher/'+table)
    columns={r[0] for r in sql.do_query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='%s'"%table)}
    expected={'id','code','data'}|({'profile','device','game'} if table=='player_play' else set())
    if bool(registry)!=bool(columns) or registry and (registry.get_value('description')!=owner or columns!=expected
            or registry.get_value('namespace')!='ngs_launcher' or registry.get_value('table_name')!=table): raise ValueError('Conflicting player table')
    if not registry:
        registry=SearchType(SearchType.SEARCH_TYPE)
        for k,v in dict(search_type='ngs_launcher/'+table,namespace='ngs_launcher',database='{project}',table_name=table,
                schema='public',class_name='pyasm.search.SObject',title=table,description=owner).items(): registry.set_value(k,v)
        registry.commit()
        extra=',profile varchar(32) NOT NULL,device varchar(32) NOT NULL,game varchar(96) NOT NULL' if table=='player_play' else ''
        sql.do_update('CREATE TABLE '+table+' (id serial PRIMARY KEY,code varchar(160) UNIQUE NOT NULL,data text NOT NULL'+extra+')')
        if table=='player_play': sql.do_update('CREATE INDEX player_play_profile_game ON player_play(profile,game)')
        (Container.get('DatabaseImpl:table_info') or {}).pop(str(p.get_project_db_resource()),None)
group_name='ngs_launcher_players_worker'
kinds=['ngs_launcher/'+t for t in ('player_profile','player_login','player_play','player_config','player_gate')]
rules=('<rules><rule group="project" code="*" access="deny"/><rule group="project" code="ngs_launcher" access="allow"/>'
    '<rule group="search_type" default="deny"/><rule group="sobject" default="deny"/>'
    +''.join('<rule group="search_type" code="%s" access="%s"/><rule group="sobject" search_type="%s" project="ngs_launcher" access="%s"/>'%(k,l,k,l)
       for k,l in [(k,'edit') for k in kinds]+[('config/custom_script','view')])
    +'<rule group="api_method" default="deny"/><rule group="api_cmd" default="deny"/></rules>')
group=find('sthpw/login_group','login_group',group_name)
if group and (group.get_value('description')!=owner or group.get_value('access_rules')!=rules): raise ValueError('Conflicting player group')
if not group:
    group=SearchType.create('sthpw/login_group')
    for k,v in dict(code=group_name,login_group=group_name,description=owner,project_code='ngs_launcher',access_level='none',is_default=False,access_rules=rules).items(): group.set_value(k,v)
    group.commit()
worker=find('sthpw/login','login','ngs_players_worker');credential=find('ngs_launcher/api_service','code','players')
if bool(worker)!=bool(credential): raise ValueError('Conflicting player worker')
if not worker:
    password=secrets.token_hex(32);worker=Login.create('ngs_players_worker',password,groups=[group_name],display_name='NGS players worker')
    credential=SearchType.create('ngs_launcher/api_service')
    for k,v in dict(code='players',login='ngs_players_worker',password=password).items(): credential.set_value(k,v)
    credential.commit()
elif credential.get_value('login')!='ngs_players_worker' or set(worker.get_sub_group_names())!={group_name}: raise ValueError('Player worker changed')
config=find('ngs_launcher/player_config','code','crypto')
if not config:
    key=RSA.generate(3072);config=SearchType.create('ngs_launcher/player_config')
    value={'private':key.export_key().decode(),'public':key.public_key().export_key().decode()}
    import hashlib
    value['kid']=hashlib.sha256(key.public_key().export_key(format='DER')).hexdigest()
    config.set_value('code','crypto');config.set_value('data',json.dumps(value));config.commit(triggers='none',log_transaction=False)
else: value=json.loads(config.get_value('data'))
script=find('config/custom_script','title','players')
if script and (script.get_value('folder')!='ngs' or script.get_value('description')!=owner): raise ValueError('Conflicting player script')
if not script: script=SearchType.create('config/custom_script')
for k,v in dict(folder='ngs',title='players',description=owner,language='python',script=source).items(): script.set_value(k,v)
script.commit()
url=find('config/url','url','/login')
if not url or url.get_value('description')!=api_owner: raise ValueError('NGS login entry missing or changed')
widget=url.get_value('widget')
branch="elif cherrypy.request.path_info == '/tactic/ngs_launcher/ngs-players/v1/accounts':"+chr(10)+"    PythonCmd(script_path='ngs/players').get_results()"+chr(10)
if branch not in widget:
    marker="else:"+chr(10)+"    body_html=WebLoginWdg2"
    if widget.count(marker)!=1: raise ValueError('Unexpected NGS login widget')
    url.set_value('widget',widget.replace(marker,branch+marker));url.commit()
return {'public':value['public'],'kid':value['kid']}
''', timeout=90, owner=OWNER, api_owner=API_OWNER, source=sources())
    payload = player_protocol.compact(dict(schema=1, public=result['public'], kid=result['kid']))
    with RsaKey(signing_key(storage).read_bytes(), private=True) as key:
        signature = key.sign(player_protocol.DOMAIN + payload)
    descriptor = json.dumps({'payload': player_protocol.encode(payload), 'signature': player_protocol.encode(signature)})
    client.script('''
from pathlib import Path
from pyasm.biz import Project
from pyasm.common import Environment
if not Environment.get_security().is_admin() or Project.get().get_code()!='ngs_launcher': raise PermissionError('NGS only')
root=Path(Environment.get_asset_dir()).resolve()/'ngs_launcher';target=root/'players-public.json'
if root.is_symlink() or target.is_symlink(): raise ValueError('Redirected assets')
target.write_text(descriptor,encoding='utf8')
return {'published':True}
''', descriptor=descriptor)
    return {'project': 'ngs_launcher', 'accounts': True, 'key_id': result['kid']}


def update_script(client):
    return client.script('''
from pyasm.biz import Project
from pyasm.common import Environment
from pyasm.search import Search
if not Environment.get_security().is_admin() or Project.get().get_code()!='ngs_launcher': raise PermissionError('NGS only')
q=Search('config/custom_script');q.add_filter('folder','ngs');q.add_filter('title','players');obj=q.get_sobject()
if not obj or obj.get_value('description')!=owner: raise ValueError('Owned player service required')
obj.set_value('script',source);obj.commit()
return {'project':'ngs_launcher','script':'ngs/players'}
''', source=sources(), owner=OWNER)


def main():
    storage = PortableStorage(Path(__file__).resolve().parents[1]); storage.configure_process()
    client = admin_client(storage.read_settings()['tactic_url'])
    print(deploy(client, storage))


if __name__ == '__main__': main()
