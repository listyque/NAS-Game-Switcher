"""Authenticate public asset reads using a revocable NGS profile session."""
def authorize():
    import hashlib
    import re
    import cherrypy
    from pyasm.biz import Project
    from pyasm.command import Command
    from tactic.command import PythonCmd
    from ngs_launcher_plugin.backend_players import service_session
    request, response = cherrypy.request, cherrypy.response
    status = 401
    try:
        if (Project.get().get_code() != 'ngs_launcher' or request.method != 'GET'
                or request.path_info != '/tactic/ngs_launcher/ngs-players/v1/assets' or request.query_string):
            raise ValueError('Invalid asset authorization request')
        token = request.headers.get('Authorization', '')
        if not re.fullmatch(r'Bearer [0-9a-f]{64}', token): raise ValueError('Invalid token')
        with service_session('players'):
            command = PythonCmd(code='''import json,time
from pyasm.search import Search
q=Search('ngs_launcher/player_login');q.add_filter('code',token_id);record=q.get_sobject()
login=json.loads(record.get_value('data')) if record else {}
if login.get('expires',0)<=time.time():return False
q=Search('ngs_launcher/player_profile')
for record in q.get_sobjects():
 profile=json.loads(record.get_value('data'))
 if profile.get('id')==login.get('profile'):return profile.get('enabled') is not False
return False
''', token_id=hashlib.sha256(token[7:].encode('ascii')).hexdigest())
            Command.execute_cmd(command)
            if command.get_info() is True: status = 204
    except Exception:
        pass
    response.status = status
    if status == 204: response.headers['X-NGS-Asset-Access'] = 'NGS-Assets-v1'
    response.headers['Cache-Control'] = 'no-store'
    if status != 204: response.headers['WWW-Authenticate'] = 'Bearer'
    request.hooks.attach('before_finalize', lambda: setattr(response, 'body', [b'']))
    return {}
