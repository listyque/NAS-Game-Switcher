"""Additional, throttled owner login for the public TACTIC web interface."""
def session_allowed(token_id):
    import hashlib
    import hmac
    import json
    import time
    from pyasm.common import Environment
    from pyasm.search import Search
    security = Environment.get_security()
    if security.is_admin() or security.get_login().get_value('login') != 'ngs_players_worker':
        return False
    query = Search('ngs_launcher/player_login'); query.add_filter('code', token_id)
    record = query.get_sobject()
    session = json.loads(record.get_value('data')) if record else {}
    if session.get('purpose') != 'web-admin' or session.get('expires', 0) <= time.time(): return False
    query = Search('ngs_launcher/player_profile'); query.add_filter('code', session.get('username', ''))
    record = query.get_sobject()
    profile = json.loads(record.get_value('data')) if record else {}
    return (profile.get('id') == session.get('profile') and profile.get('enabled') is not False
            and profile.get('role', 'owner') == 'owner' and hmac.compare_digest(
                session.get('password_version', ''), hashlib.sha256(profile.get('password', '').encode()).hexdigest()))


def authorize():
    import base64
    import hashlib
    import inspect
    import json
    import re
    from http.cookies import SimpleCookie
    import cherrypy
    from pyasm.biz import Project
    from pyasm.command import Command
    from pyasm.search import Search
    from tactic.command import PythonCmd
    from ngs_launcher_plugin import backend_players as protocol
    from ngs_launcher_plugin.request_peer import client_address
    request, response = cherrypy.request, cherrypy.response
    status = 401
    try:
        if (Project.get().get_code() != 'ngs_launcher' or request.method != 'GET'
                or request.path_info != '/tactic/ngs_launcher/ngs-players/v1/admin' or request.query_string):
            raise ValueError('Invalid admin authorization request')
        cookies = SimpleCookie(); cookies.load(request.headers.get('Cookie', ''))
        cookie = cookies.get('NGSAdmin')
        token = cookie.value if cookie else ''
        with protocol.service_session('players'):
            if re.fullmatch('[0-9a-f]{64}', token):
                command = PythonCmd(code=inspect.getsource(session_allowed)+'\nreturn session_allowed(token_id)',
                                    token_id=hashlib.sha256(token.encode()).hexdigest())
                Command.execute_cmd(command)
                if command.get_info() is True: status = 204
            if status != 204:
                header = request.headers.get('Authorization', '')
                if not header.startswith('Basic ') or len(header) > 1024: raise ValueError('Owner login required')
                decoded = base64.b64decode(header[6:], validate=True).decode('utf8')
                username, password = decoded.split(':', 1)
                protocol.username(username)
                if not 8 <= len(password) <= 128: raise ValueError('Invalid password size')
                query = Search('ngs_launcher/player_config'); query.add_filter('code', 'crypto')
                crypto = json.loads(query.get_sobject().get_value('data'))
                wire, key, identity = protocol.request(crypto['public'], dict(
                    action='web-login', device='0'*32, username=username, password=password))
                # Keep plaintext credentials out of TACTIC command parameters and diagnostics.
                command = PythonCmd(code=protocol.PLAYER_ACTION_SOURCE,
                                    wire=wire.decode('ascii'), address=client_address(request))
                Command.execute_cmd(command); result = command.get_info()
                if result['status'] == 429:
                    status = 429
                elif result.get('wire'):
                    message, unused = protocol.open_message(result['wire'].encode(), key, crypto['kid'])
                    if message.get('request_id') == identity and message.get('ok') is True:
                        token = message['result']['token']
                        if not re.fullmatch('[0-9a-f]{64}', token): raise ValueError('Invalid session')
                        response.headers['X-NGS-Admin-Cookie'] = 'NGSAdmin='+token+'; Path=/; Max-Age=900; Secure; HttpOnly; SameSite=Strict'
                        status = 204
    except Exception:
        pass
    response.status = status
    if status == 204: response.headers['X-NGS-Admin-Access'] = 'NGS-Admin-v1'
    response.headers['Cache-Control'] = 'no-store'
    if status == 401: response.headers['WWW-Authenticate'] = 'Basic realm="NGS NAS owner", charset="UTF-8"'
    request.hooks.attach('before_finalize', lambda: setattr(response, 'body', [b'']))
    return {}
