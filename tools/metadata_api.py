"""Deploy and pair the restricted NGS entry inside its own TACTIC project only."""

import argparse
import hashlib
import hmac
import inspect
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ngs import api
from ngs.catalog import atomic_write
from ngs.install import safe_path
from ngs.portable import PortableStorage
from ngs.tactic import TacticClient
from tools.tactic_catalog import install_resolver
from tools.tactic_project import DEFAULT_CREDENTIALS, admin_client, admin_password
from tools.server_code import packed_script


OWNER = "NGS restricted metadata entry v1; managed from D:/code/NGS"


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


def claim_device(device, stamp, nonce, signature, body, operation, protocol):
    import hmac, json, time
    from pyasm.biz import Project
    from pyasm.search import Search, DbContainer
    if Project.get().get_code() != "ngs_launcher":
        raise PermissionError("NGS project required")
    namespace = {}
    exec(protocol, namespace)
    sql = DbContainer.get(Project.get().get_project_db_resource())
    sql.do_query("SELECT pg_advisory_xact_lock(1313297220)")
    query = Search("ngs_launcher/api_device")
    query.add_filter("code", device)
    obj = query.get_sobject()
    if (not obj or obj.get_value("enabled") is not True or not hmac.compare_digest(signature,
            namespace["request_signature"](obj.get_value("secret"), device, stamp, nonce, body.encode("utf8")))):
        return {"status": 401}
    try:
        state = namespace["claim_state"](json.loads(obj.get_value("state") or "{}"), nonce, operation, int(time.time()))
    except namespace["ApiRejected"] as error:
        if error.status == 409 and str(error) == 'Another metadata request is running':
            return {"status": 409, "reason": "busy"}
        return {"status": error.status}
    obj.set_value("state", json.dumps(state, sort_keys=True))
    # Nonces and leases must be durable, but must never become undoable catalog edits.
    obj.commit(triggers="none", log_transaction=False)
    return {"status": 200}


def release_device(device, nonce):
    import json
    from pyasm.biz import Project
    from pyasm.search import Search, DbContainer
    if Project.get().get_code() != "ngs_launcher":
        raise PermissionError("NGS project required")
    sql = DbContainer.get(Project.get().get_project_db_resource())
    sql.do_query("SELECT pg_advisory_xact_lock(1313297220)")
    query = Search("ngs_launcher/api_device"); query.add_filter("code", device)
    obj = query.get_sobject()
    state = json.loads(obj.get_value("state") or "{}") if obj else {}
    if state.get("busy_nonce") == nonce:
        state.update(busy_until=0, busy_nonce="")
        obj.set_value("state", json.dumps(state, sort_keys=True)); obj.commit(triggers="none", log_transaction=False)


def metadata_web(protocol, claim_source, release_source, diagnostics_source='', account_source='', backup_source=''):
    import hashlib, hmac, json, logging, re, time
    import cherrypy
    from pyasm.biz import Project
    from pyasm.search import Search
    from pyasm.command import Command
    from tactic.command import PythonCmd
    ns = {}; exec(protocol, ns)
    request = cherrypy.request
    key, nonce, claimed, identity = None, "", False, None
    stage = "authentication"
    status, result = 503, {"error": "Unavailable"}
    try:
        project = Project.get()
        if (project.get_code() != "ngs_launcher" or project.get_database_name() != "ngs_launcher"
                or project.get_value("description") != "NGS handheld game catalog; managed from D:/code/NGS"):
            raise ns["ApiRejected"](404, "Not found")
        if request.path_info != ns["API_PATH"] or request.query_string:
            raise ns["ApiRejected"](404, "Not found")
        if request.method != "POST":
            raise ns["ApiRejected"](405, "POST required")
        length = request.headers.get("Content-Length", "")
        if not length.isdigit() or not 0 < int(length) <= ns["MAX_BODY"] or request.headers.get("Transfer-Encoding"):
            raise ns["ApiRejected"](413, "Request too large")
        if request.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
            raise ns["ApiRejected"](415, "Unsupported content type")
        now = int(time.time())
        device, stamp, nonce, signature = ns["validate_headers"](request.headers, now)
        query = Search("ngs_launcher/api_device"); query.add_filter("code", device)
        obj = query.get_sobject()
        if not obj or obj.get_value("enabled") is not True:
            raise ns["ApiRejected"](401, "Invalid device")
        secret = obj.get_value("secret")
        body = request.body.read(ns["MAX_BODY"] + 1)
        if len(body) != int(length):
            raise ns["ApiRejected"](400, "Invalid body size")
        if not hmac.compare_digest(signature, ns["request_signature"](secret, device, stamp, nonce, body)):
            raise ns["ApiRejected"](401, "Invalid signature")
        key = secret
        try:
            text = body.decode("utf8")
            operation, value = ns["validate_payload"](json.loads(text))
        except (ValueError, UnicodeError):
            raise ns["ApiRejected"](400, "Invalid metadata request")
        account = json.loads(obj.get_value('state') or '{}').get('account', {})
        if not account or not account_source:
            raise ns['ApiRejected'](401, "Sign in to NGS in Settings.")
        with service_session('players'):
            cmd = PythonCmd(code=account_source, account=account, operation=operation)
            Command.execute_cmd(cmd)
            if cmd.get_info() is not True:
                if operation == 'catalog':
                    raise ns['ApiRejected'](403, "Only the server owner can use the catalog manager.")
                raise ns['ApiRejected'](401, "Your NGS session has expired. Sign in again.")
        stage = "claim"
        if operation in ("catalog", "transfer", "diagnostics", "backup"):
            access = Search("ngs_launcher/catalog_config"); access.add_filter("code", "service")
            config = access.get_sobject()
            if not config or config.get_value("description") != "NGS remote catalogizer v1":
                raise ns["ApiRejected"](403, "Catalogizer access required")
        context = service_session("catalog") if operation in ("catalog", "transfer", "diagnostics", "backup") else service_session()
        context.__enter__()
        identity = context
        cmd = PythonCmd(code=claim_source, device=device, stamp=stamp, nonce=nonce, signature=signature,
                        body=text, operation=operation, protocol=protocol)
        Command.execute_cmd(cmd)
        claim = cmd.get_info()
        if claim["status"] != 200:
            raise ns["ApiRejected"](claim["status"], "DeviceBusy" if claim.get('reason') == 'busy' else "Request rejected")
        claimed = True
        stage = "metadata"
        cmd = (PythonCmd(code=diagnostics_source, payload=value, device=device) if operation == 'diagnostics' else PythonCmd(code=backup_source, payload=value, device=device, account=account) if operation == 'backup' else
               PythonCmd(script_path="ngs/catalog", action=value["action"], payload=value, device=device)
               if operation in ("catalog", "transfer") else PythonCmd(script_path="ngs/metadata", operation=operation, value=value))
        Command.execute_cmd(cmd)
        result = cmd.get_info()
        if operation == "catalog" and value["action"] in ("publish", "installer_publish", "status", "installer_status"):
            # Keep separate commits: activate must see a successfully committed export snapshot.
            for unused in range(2):
                if result.get("stage") not in ("working", "batch") or result.get("state") not in ("export", "activate"):
                    break
                payload = dict(action="installer_status" if value["action"].startswith("installer_") else "status", preview=value["preview"])
                cmd = PythonCmd(script_path="ngs/catalog", action=payload["action"], payload=payload, device=device)
                Command.execute_cmd(cmd)
                result = cmd.get_info()
        result, status = {"result": result}, 200
    except ns["ApiRejected"] as error:
        status, result = error.status, {"error": str(error)}
    except ValueError as error:
        if key and locals().get("operation") in ("catalog", "transfer", "diagnostics", "backup"):
            status, result = 400, {"error": str(error)[:500]}
        else:
            logging.warning("NGS metadata validation failed")
            status, result = 503, {"error": "Metadata unavailable"}
    except Exception as error:
        logging.warning("NGS metadata request failed (%s)", type(error).__name__)
        status, result = 503, {"error": "Metadata unavailable", "stage": stage, "kind": type(error).__name__}
    finally:
        if claimed:
            try:
                cmd = PythonCmd(code=release_source, device=device, nonce=nonce)
                Command.execute_cmd(cmd)
            except Exception:
                logging.warning("NGS metadata device lease will expire")
        if identity:
            identity.__exit__(None, None, None)
    raw = json.dumps(dict(result, schema=1), ensure_ascii=False, separators=(",", ":")).encode("utf8")
    if len(raw) > ns["MAX_RESPONSE"]:
        raw, status = b'{"schema":1,"error":"Response too large"}', 503
    signed = ns["response_signature"](key, nonce, status, raw) if key else ""
    def finalize():
        response = cherrypy.response
        response.status = status
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Content-Length"] = str(len(raw))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if signed: response.headers["X-NGS-Signature"] = signed
        response.body = [raw]
    request.hooks.attach("before_finalize", finalize, priority=90)


def protocol_source():
    from ngs.catalog import relative_game_path
    return ("import base64,hashlib,hmac,re\nfrom pathlib import PureWindowsPath,PurePosixPath\nAPI_PATH=" + repr(api.API_PATH) + "\nMAX_BODY=2048\nMAX_RESPONSE=65536\nMAX_BACKUP_BYTES=" + repr(api.MAX_BACKUP_BYTES) + "\nUPLOAD_CHUNK=" + repr(api.UPLOAD_CHUNK) + "\nDOWNLOAD_CHUNK=" + repr(api.DOWNLOAD_CHUNK) + "\nMAX_BACKUPS_PER_DEVICE=" + repr(api.MAX_BACKUPS_PER_DEVICE) + "\nMAX_DEVICES_PER_PROFILE=" + repr(api.MAX_DEVICES_PER_PROFILE) + "\n"
            + "\n".join(inspect.getsource(fn) for fn in (api.ApiRejected, relative_game_path, api.clean_device_name, api.validate_catalog_request, api.validate_diagnostic_request, api.validate_backup_request, api.metadata_reference, api.validate_payload, api.validate_headers,
                                                      api.request_signature, api.response_signature, api.claim_state)))


def api_script_source():
    from tools.diagnostics import diagnostic_action
    from tools.player_accounts import account_api_allowed
    protocol = protocol_source()
    from tools.backups import BACKUP_MARKER, BACKUP_ROOT, backup_action
    backup_code = "import base64,re\nBACKUP_ROOT=" + repr(BACKUP_ROOT) + "\nBACKUP_MARKER=" + repr(BACKUP_MARKER) + "\nMAX_BACKUP_BYTES=" + repr(api.MAX_BACKUP_BYTES) + "\nUPLOAD_CHUNK=" + repr(api.UPLOAD_CHUNK) + "\nDOWNLOAD_CHUNK=" + repr(api.DOWNLOAD_CHUNK) + "\nMAX_BACKUPS_PER_DEVICE=" + repr(api.MAX_BACKUPS_PER_DEVICE) + "\nMAX_DEVICES_PER_PROFILE=" + repr(api.MAX_DEVICES_PER_PROFILE) + "\n" + inspect.getsource(api.clean_device_name) + "\n" + inspect.getsource(api.validate_backup_request) + "\n" + inspect.getsource(backup_action) + "\nreturn backup_action(payload,device,account)"
    # The script has no client-controlled kwargs. Embed only reviewed server code.
    return packed_script(inspect.getsource(service_session) + "\n" + inspect.getsource(metadata_web), "metadata_web(" + repr(protocol) + ","
              + repr(inspect.getsource(claim_device) + "\nreturn claim_device(device,stamp,nonce,signature,body,operation,protocol)") + ","
              + repr(inspect.getsource(release_device) + "\nreturn release_device(device,nonce)") + ','
              + repr('import re\n' + inspect.getsource(api.validate_diagnostic_request) + '\n' + inspect.getsource(diagnostic_action)
                     + '\nreturn diagnostic_action(payload,device)') + ','
              + repr(inspect.getsource(account_api_allowed) + '\nreturn account_api_allowed(account, operation)') + ',' + repr(backup_code) + ")")


def update_scripts(client):
    from tools.remote_catalog import remote_source, OWNER as CATALOG_OWNER
    from tools.backups import BACKUP_MARKER, BACKUP_ROOT, provision_backups
    client.script("BACKUP_ROOT=" + repr(BACKUP_ROOT) + "\nBACKUP_MARKER=" + repr(BACKUP_MARKER) + "\n" + inspect.getsource(provision_backups) + "\nreturn provision_backups()")
    result = client.script('''
from pyasm.biz import Project
from pyasm.common import Environment
from pyasm.search import Search
import hashlib
p=Project.get()
if (not Environment.get_security().is_admin() or p.get_code()!='ngs_launcher' or p.get_database_name()!='ngs_launcher'
    or p.get_value('description')!='NGS handheld game catalog; managed from D:/code/NGS'):
    raise PermissionError('NGS only')
q=Search('config/custom_script');q.add_filter('folder','ngs');q.add_filter('title','api');obj=q.get_sobject()
if not obj or obj.get_value('description')!=owner: raise ValueError('Owned NGS API is not installed')
q=Search('config/custom_script');q.add_filter('folder','ngs');q.add_filter('title','catalog');catalog=q.get_sobject()
if catalog and catalog.get_value('description')!=catalog_owner: raise ValueError('Foreign NGS catalogizer script')
if obj.get_value('script')!=source:
    obj.set_value('script',source);obj.commit()
if catalog and catalog.get_value('script')!=catalog_source:
    catalog.set_value('script',catalog_source);catalog.commit()
return {'project':'ngs_launcher','script':'ngs/api','sha256':hashlib.sha256(source.encode('utf8')).hexdigest()}
''', source=api_script_source(), owner=OWNER, catalog_source=remote_source(), catalog_owner=CATALOG_OWNER)
    install_resolver(client)
    return result


def deploy(client):
    script = api_script_source()
    html = '''<%
import cherrypy
from pyasm.widget import WebLoginWdg2
from tactic.command import PythonCmd
body_html=''
if cherrypy.request.path_info == '/tactic/ngs_launcher/ngs-api/v1/metadata':
    PythonCmd(script_path='ngs/api').get_results()
elif cherrypy.request.path_info == '/tactic/ngs_launcher/ngs-players/v1/accounts':
    PythonCmd(script_path='ngs/players').get_results()
elif cherrypy.request.path_info == '/tactic/ngs_launcher/ngs-players/v1/admin':
    from ngs_launcher_plugin.admin_access import authorize
    authorize()
elif cherrypy.request.path_info == '/tactic/ngs_launcher/ngs-players/v1/assets':
    from ngs_launcher_plugin.asset_access import authorize
    authorize()
else:
    body_html=WebLoginWdg2(allow_change_admin=False,hide_back_btn=True).get_buffer_display()
%>
${body_html}'''
    widget = '<element index="false" admin="false"><display class="tactic.ui.panel.CustomLayoutWdg"><html><![CDATA[' + html + ']]></html></display></element>'
    result = client.script('''
from pyasm.biz import Project
from pyasm.common import Environment,Container
from pyasm.search import Search,SearchType,DbContainer
from pyasm.security import Login
import secrets,json
p=Project.get()
if (not Environment.get_security().is_admin() or p.get_code()!='ngs_launcher' or p.get_database_name()!='ngs_launcher'
    or p.get_value('description')!='NGS handheld game catalog; managed from D:/code/NGS'): raise PermissionError('NGS only')
def find(kind,field,value):
    q=Search(kind);q.add_filter(field,value);return q.get_sobject()
url=find('config/url','url','/login')
q=Search('config/custom_script');q.add_filter('folder','ngs');q.add_filter('title','api');script=q.get_sobject()
for obj in (url,script):
    if obj and obj.get_value('description')!=owner: raise ValueError('Entry belongs to another owner')
sql=DbContainer.get(p.get_project_db_resource())
registry=find('sthpw/search_object','search_type','ngs_launcher/api_device')
columns={r[0] for r in sql.do_query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='api_device'")}
expected={'id','code','name','secret','enabled','state'}
if bool(registry)!=bool(columns) or (registry and (registry.get_value('description')!=owner or columns!=expected
    or registry.get_value('table_name')!='api_device' or registry.get_value('namespace')!='ngs_launcher')):
    raise ValueError('Conflicting NGS device table')
if not registry:
    registry=SearchType(SearchType.SEARCH_TYPE)
    values=dict(search_type='ngs_launcher/api_device',namespace='ngs_launcher',database='{project}',table_name='api_device',
        schema='public',class_name='pyasm.search.SObject',title='NGS API devices',description=owner)
    for k,v in values.items(): registry.set_value(k,v)
    registry.commit()
    sql.do_update("CREATE TABLE api_device (id serial PRIMARY KEY,code varchar(32) UNIQUE NOT NULL,name varchar(160) NOT NULL,secret varchar(64) NOT NULL,enabled boolean NOT NULL DEFAULT true,state text NOT NULL DEFAULT '{}')")
    (Container.get('DatabaseImpl:table_info') or {}).pop(str(p.get_project_db_resource()),None)
service_registry=find('sthpw/search_object','search_type','ngs_launcher/api_service')
service_columns={r[0] for r in sql.do_query("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='api_service'")}
if bool(service_registry)!=bool(service_columns) or (service_registry and (service_registry.get_value('description')!=owner
        or service_columns!={'id','code','login','password'})):
    raise ValueError('Conflicting NGS service table')
if not service_registry:
    service_registry=SearchType(SearchType.SEARCH_TYPE)
    for k,v in dict(search_type='ngs_launcher/api_service',namespace='ngs_launcher',database='{project}',table_name='api_service',
            schema='public',class_name='pyasm.search.SObject',title='NGS internal service credential',description=owner).items():
        service_registry.set_value(k,v)
    service_registry.commit()
    sql.do_update('CREATE TABLE api_service (id serial PRIMARY KEY,code varchar(32) UNIQUE NOT NULL,login varchar(64) NOT NULL,password varchar(128) NOT NULL)')
    (Container.get('DatabaseImpl:table_info') or {}).pop(str(p.get_project_db_resource()),None)
group_name='ngs_launcher_metadata_worker'
rules=('<rules><rule group="project" code="*" access="deny"/>'
       '<rule group="project" code="ngs_launcher" access="allow"/>'
       '<rule group="search_type" default="deny"/><rule group="sobject" default="deny"/>'
       +''.join('<rule group="search_type" code="%s" access="%s"/><rule group="sobject" search_type="%s" project="ngs_launcher" access="%s"/>'%(kind,level,kind,level)
          for kind,level in (('ngs_launcher/game','edit'),('ngs_launcher/api_device','edit'),('config/custom_script','view'),
                             ('sthpw/snapshot','edit'),('sthpw/file','edit'),('config/naming','view')))
       +'<rule group="api_method" default="deny"/><rule group="api_cmd" default="deny"/></rules>')
group=find('sthpw/login_group','login_group',group_name)
if group and (group.get_value('description')!=owner or group.get_value('project_code')!='ngs_launcher' or group.get_value('access_rules')!=rules):
    raise ValueError('NGS worker group already customised')
if not group:
    group=SearchType.create('sthpw/login_group')
    for k,v in dict(code=group_name,login_group=group_name,description=owner,project_code='ngs_launcher',access_level='none',
                   is_default=False,access_rules=rules).items(): group.set_value(k,v)
    group.commit()
credential=find('ngs_launcher/api_service','code','metadata')
worker=find('sthpw/login','login','ngs_metadata_worker')
if bool(credential)!=bool(worker): raise ValueError('NGS worker name is already used or its credential is missing')
if not worker:
    password=secrets.token_hex(32)
    worker=Login.create('ngs_metadata_worker',password,groups=[group_name],display_name='NGS metadata worker')
    credential=SearchType.create('ngs_launcher/api_service')
    for k,v in dict(code='metadata',login='ngs_metadata_worker',password=password).items(): credential.set_value(k,v)
    credential.commit()
elif credential.get_value('login')!='ngs_metadata_worker' or set(worker.get_sub_group_names())!={group_name}:
    raise ValueError('NGS worker account has changed; refusing to overwrite')
if not script: script=SearchType.create('config/custom_script')
for k,v in dict(folder='ngs',title='api',description=owner,language='python',script=source).items(): script.set_value(k,v)
script.commit()
if not url: url=SearchType.create('config/url')
for k,v in dict(code='ngs_api_login',url='/login',description=owner,widget=widget).items(): url.set_value(k,v)
url.commit()
return {'project':'ngs_launcher','entry':'/ngs-api/v1/metadata','global_changes':False}
''', timeout=90, source=script, widget=widget, owner=OWNER)
    install_resolver(client)
    return result


def device_action(client, action, values):
    return client.script('''
import re
from pyasm.biz import Project
from pyasm.common import Environment
from pyasm.search import Search,SearchType
if (not Environment.get_security().is_admin() or Project.get().get_code()!='ngs_launcher'
        or Project.get().get_database_name()!='ngs_launcher'): raise PermissionError('NGS only')
if not re.fullmatch('[0-9a-f]{32}',values.get('code','')): raise ValueError('Invalid device ID')
q=Search('ngs_launcher/api_device');q.add_filter('code',values['code']);obj=q.get_sobject()
if action=='pair':
    if obj or not re.fullmatch('[0-9a-f]{64}',values.get('secret','')) or not 1<=len(values.get('name',''))<=160:
        raise ValueError('Invalid or duplicate NGS device')
    obj=SearchType.create('ngs_launcher/api_device')
    for k,v in dict(values,enabled=True,state='{}').items(): obj.set_value(k,v)
elif action=='revoke':
    if not obj: raise ValueError('Device not found')
    obj.set_value('enabled',False)
else: raise ValueError('Unsupported device action')
obj.commit()
return {'device':obj.get_code(),'enabled':obj.get_value('enabled')}
''', action=action, values=values)


def pair(client, storage, base_url, name):
    path = safe_path(storage.config, "api-device.json")
    if path.exists():
        raise ValueError("Ключ устройства уже существует; он не перезаписан.")
    device, secret = secrets.token_hex(16), secrets.token_hex(32)
    parsed = urlsplit(base_url)
    server = urlunsplit((parsed.scheme, parsed.netloc, api.API_PATH, "", ""))
    device_action(client, "pair", {"code": device, "secret": secret, "name": name})
    try:
        atomic_write(path, json.dumps({"schema": 1, "server": server, "device": device, "secret": secret}, indent=2).encode("utf8"))
    except Exception:
        device_action(client, "revoke", {"code": device})
        raise
    return {"device": device, "config": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--update-scripts", action="store_true", help="Update only existing owned NGS API/resolver scripts")
    parser.add_argument("--pair", metavar="NAME")
    parser.add_argument("--revoke", metavar="DEVICE_ID")
    parser.add_argument("--credentials-env", type=Path, default=DEFAULT_CREDENTIALS)
    args = parser.parse_args()
    if not any((args.install, args.update_scripts, args.pair, args.revoke)): parser.error("Choose --install, --update-scripts, --pair or --revoke")
    storage = PortableStorage(Path(__file__).resolve().parents[1]); storage.configure_process()
    url = storage.read_settings()["tactic_url"]
    client = admin_client(url, args.credentials_env)
    if args.install: print(deploy(client))
    if args.update_scripts: print(update_scripts(client))
    if args.pair: print(pair(client, storage, url, args.pair))
    if args.revoke: print(device_action(client, "revoke", {"code": args.revoke}))


if __name__ == "__main__":
    main()
