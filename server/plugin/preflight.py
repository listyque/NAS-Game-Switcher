from pyasm.biz import Project
from pyasm.common import Environment
from pyasm.search import DbContainer
if not Environment.get_security().is_admin() or Project.get().get_code()!='ngs_launcher':
    raise PermissionError('Select the empty ngs_launcher project before installing NGS')
database=DbContainer.get(Project.get().get_project_db_resource())
for name in ['game','catalog','api_device','api_service','catalog_config','player_profile','player_play','player_login','player_config','app_release']:
    if database.do_query("SELECT to_regclass('public."+name+"')")[0][0]:
        if database.do_query('SELECT 1 FROM "'+name+'" LIMIT 1'):
            raise ValueError('NGS is already configured; this first-install plugin will not reset it')
