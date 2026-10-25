"""Configure native administration only inside the existing NGS project."""
import argparse
import inspect
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from ngs.portable import DEFAULT_TACTIC_URL
from tools.tactic_project import admin_client


OWNER = 'NGS native administration v1'
TABLES = {
    'game': ('Игры и установщики', 'name genre status available_on_nas revision source_folder executable code'),
    'catalog': ('Опубликованные каталоги', 'name code description timestamp'),
    'app_release': ("NGS updates", 'name code timestamp description ngs_summary'),
    'package_job': ('Передачи и пакеты', 'game_id state timestamp description code ngs_summary'),
    'player_profile': ('Профили игроков', 'code ngs_summary'),
    'player_play': ('Игровые сессии', 'game profile device code ngs_summary'),
    'player_login': ('Входы игроков', 'code ngs_summary'),
    'api_device': ('Устройства API', 'name code enabled ngs_summary'),
    'catalog_config': ('Каталогизатор и задания', 'code description ngs_summary'),
    'api_service': ('Служебные учётные записи', 'code login'),
    'player_config': ('Настройки профилей', 'code ngs_summary'),
    'player_gate': ('Ограничения входа', 'code ngs_summary'),
}
LABELS = dict(name="Title", code='Код', description='Описание', timestamp='Дата', genre="Genre",
    status='Статус', available_on_nas='Есть на NAS', revision='Ревизия', source_folder='Папка источника',
    executable='EXE', game_id="Game", game="Game", state="Status", profile='Профиль', device='Устройство',
    enabled='Разрешено', login='Логин службы', ngs_summary='Подробности', subtitle='Подзаголовок',
    accent='Цвет', metadata_url='Источник оформления', cover_snapshot='Обложка', background_snapshot='Фон')


def admin_summary(sobject):
    import html, json
    from pyasm.biz import Project
    from pyasm.common import Environment
    if Project.get().get_code() != 'ngs_launcher' or not Environment.get_security().is_admin():
        raise PermissionError('NGS administrator required')
    table = sobject.get('__search_type__', '').removeprefix('ngs_launcher/')
    fields = {
        'app_release': ('published_data', ('version', 'size', 'notes')),
        'package_job': ('published_data', ('id', 'name', 'revision', 'kind')),
        'player_profile': ('data', ('id', 'username')),
        'player_play': ('data', ('started_at', 'ended_at', 'active_ms', 'paused_ms', 'updated_at')),
        'player_login': ('data', ('profile', 'device', 'expires')),
        'api_device': ('state', ('busy_until',)),
        'catalog_config': ('data', ('stage', 'state', 'folder', 'game', 'message', 'error', 'done', 'total')),
        'player_gate': ('data', ('expires',)),
    }
    if table == 'player_config': return 'Ключи и служебная конфигурация скрыты'
    if table not in fields: return ''
    column, allowed = fields[table]
    try:
        data = json.loads(sobject.get(column) or '{}')
    except (ValueError, TypeError): return 'Некорректный JSON'
    if not isinstance(data, dict): return 'Некорректный JSON'
    if table == 'package_job': data = data.get('card', data)
    if not isinstance(data, dict): return 'Некорректный JSON'
    values = []
    for key in allowed:
        value = data.get(key)
        if value is None or isinstance(value, (dict, list)): continue
        if key in ('active_ms', 'paused_ms') and type(value) is int:
            value = '%d ч %02d мин' % (value // 3600000, value // 60000 % 60)
        values.append('%s: %s' % (key, html.escape(str(value)[:1000])))
    if table == 'catalog_config' and not values: return 'Служебные настройки; ключи скрыты'
    return '<br/>'.join(values)


def build_plan():
    configs = []
    def config(search_type, view, fields, editable=False):
        root = ET.Element('config')
        node = ET.SubElement(root, view, {'edit': 'true' if editable else 'false'})
        for name in fields:
            attrs = {'name': name, 'title': LABELS.get(name, name)}
            if name == 'ngs_summary': attrs['edit'] = 'false'
            element = ET.SubElement(node, 'element', attrs)
            if view == 'definition' and name == 'ngs_summary':
                display = ET.SubElement(element, 'display', {'class': 'tactic.ui.table.PythonElementWdg'})
                ET.SubElement(display, 'script_path').text = 'ngs/admin_summary'
                ET.SubElement(display, 'order_by').text = 'false'
        configs.append(dict(search_type=search_type, view=view, config=ET.tostring(root, encoding='unicode')))
    for table, (title, columns) in TABLES.items():
        fields = columns.split()
        definition = list(dict.fromkeys(fields + (['subtitle', 'accent', 'description', 'metadata_url',
                         'cover_snapshot', 'background_snapshot'] if table == 'game' else [])))
        stype = 'ngs_launcher/' + table
        config(stype, 'definition', definition)
        config(stype, 'table', fields, table in ('game', 'api_device'))
        edit = (['name', 'subtitle', 'genre', 'accent', 'description', 'source_folder', 'executable',
                 'metadata_url', 'status', 'available_on_nas'] if table == 'game' else
                ['name', 'enabled'] if table == 'api_device' else fields)
        config(stype, 'edit', edit, table in ('game', 'api_device'))
    config('config/widget_config', 'ngs_admin', ['search_type', 'view', 'title', 'login', 'priority', 'description'])
    schema = ET.Element('schema')
    for index, table in enumerate(TABLES):
        ET.SubElement(schema, 'search_type', name='ngs_launcher/' + table,
                      xpos=str(index % 4 * 270 + 80), ypos=str(index // 4 * 180 + 80))
    for child, column in [('package_job', 'game_id'), ('player_play', 'game')]:
        ET.SubElement(schema, 'connect', {'from': 'ngs_launcher/' + child, 'to': 'ngs_launcher/game',
            'relationship': 'code', 'from_col': column, 'to_col': 'code', 'type': 'search_type'})
    links = []
    for table, (title, unused) in TABLES.items():
        links.append(dict(name='ngs_games' if table == 'game' else 'ngs_' + table, title=title,
            options=dict(class_name='tactic.ui.panel.ViewPanelWdg', search_type='ngs_launcher/' + table,
                         view='table', layout='default', search_limit='50', show_insert='false')))
    links.extend([
        dict(name='ngs_schema', title='Схема проекта', options=dict(class_name='tactic.ui.tools.SchemaToolWdg')),
        dict(name='ngs_columns', title='Редактор видов и колонок', options=dict(
            class_name='tactic.ui.manager.ViewManagerWdg', search_type='ngs_launcher/game', view='table')),
        dict(name='ngs_widget_configs', title='Конфигурации виджетов', options=dict(
            class_name='tactic.ui.panel.ViewPanelWdg', search_type='config/widget_config', view='ngs_admin',
            layout='default', search_limit='50')),
    ])
    return dict(owner=OWNER, tables=list(TABLES), configs=configs, links=links,
                schema=ET.tostring(schema, encoding='unicode'),
                script=inspect.getsource(admin_summary) + '\nreturn admin_summary(sobject)')


def configure_admin(plan, apply=False):
    from xml.etree import ElementTree as ET
    from pyasm.biz import Project, Schema
    from pyasm.common import Environment
    from pyasm.search import Search, SearchType, WidgetDbConfig, DbContainer
    project = Project.get()
    if (not Environment.get_security().is_admin() or project.get_code() != 'ngs_launcher'
            or project.get_database_name() != 'ngs_launcher'
            or project.get_value('description') != 'NGS handheld game catalog; managed from D:/code/NGS'):
        raise PermissionError('Owned NGS project required')
    if plan['owner'] != 'NGS native administration v1': raise ValueError('Unknown admin plan')
    owner = plan['owner']
    sql = DbContainer.get(project.get_project_db_resource())
    types = Search('sthpw/search_object'); types.add_filter('namespace', 'ngs_launcher')
    registered = {obj.get_value('table_name'): obj for obj in types.get_sobjects()}
    if set(registered) != set(plan['tables']): raise ValueError('Review changed NGS tables before configuring administration')
    for table, obj in registered.items():
        if obj.get_value('search_type') != 'ngs_launcher/' + table or obj.get_value('database') != '{project}':
            raise ValueError('Unexpected table mapping')
    query = Search('sthpw/schema'); query.add_filter('code', 'ngs_launcher')
    schema = query.get_sobject()
    script_query = Search('config/custom_script'); script_query.add_filter('folder', 'ngs'); script_query.add_filter('title', 'admin_summary')
    script = script_query.get_sobject()
    if script and script.get_value('description') != owner: raise ValueError('Foreign admin_summary script')
    configs = Search('config/widget_config').get_sobjects()
    backup = dict(schema=schema.get_data() if schema else None,
        configs=[obj.get_data() for obj in configs], script=script.get_data() if script else None)
    if not apply: return dict(project='ngs_launcher', tables=len(registered), backup=backup)
    sql.do_query('SELECT pg_advisory_xact_lock(1313297221)')
    def fingerprint():
        result = {}
        for table in ('game', 'catalog', 'app_release', 'player_profile'):
            rows = sql.do_query('SELECT COUNT(*),md5(string_agg(md5(row_to_json(t)::text),chr(10) ORDER BY id)) FROM "%s" t' % table)
            # TACTIC clears old query result rows when the next query runs.
            result[table] = tuple(tuple(row) for row in rows)
        return result
    before = fingerprint()
    if schema is None:
        schema = Schema.create('ngs_launcher', owner, plan['schema'])
        schema.set_value('project_code', 'ngs_launcher'); schema.commit()
    else:
        root = ET.fromstring(schema.get_value('schema'))
        for element in ET.fromstring(plan['schema']):
            key = 'name' if element.tag == 'search_type' else 'from'
            if not any(old.tag == element.tag and old.get(key) == element.get(key) for old in root): root.append(element)
        value = ET.tostring(root, encoding='unicode')
        if value != schema.get_value('schema'): schema.set_value('schema', value); schema.commit()
    changed = []
    def save_config(search_type, view, xml, category=''):
        obj = next((o for o in configs if o.get_value('search_type') == search_type and o.get_value('view') == view
                    and o.get_value('description') == owner), None)
        # User edits to our installed views are authoritative on subsequent runs.
        if obj is not None: return
        obj = SearchType.create('config/widget_config')
        for key, value in dict(search_type=search_type, view=view, config=xml, description=owner,
                               category=category, login='', priority=100).items(): obj.set_value(key, value)
        obj.commit(); changed.append(search_type + ':' + view)
    for item in plan['configs']: save_config(item['search_type'], item['view'], item['config'])
    for view in ('definition', 'project_view'):
        root = ET.Element('config'); node = ET.SubElement(root, view)
        for obj in configs:
            if obj.get_value('search_type') != 'SideBarWdg' or obj.get_value('view') != view: continue
            for old in ET.fromstring(obj.get_value('config')).findall('./%s/element' % view):
                if old.get('name') not in {x.get('name') for x in node}: node.append(old)
        for link in plan['links']:
            element = next((x for x in node if x.get('name') == link['name']), None)
            if element is None: element = ET.SubElement(node, 'element', name=link['name'])
            if view == 'definition':
                element.clear(); element.attrib.update(name=link['name'], title=link['title'])
                display = ET.SubElement(element, 'display', {'class': 'LinkWdg'})
                for key, value in link['options'].items(): ET.SubElement(display, key).text = value
        save_config('SideBarWdg', view, ET.tostring(root, encoding='unicode'), 'SideBarWdg')
    if script is None:
        script = SearchType.create('config/custom_script')
        for key, value in dict(folder='ngs', title='admin_summary', description=owner, script=plan['script']).items(): script.set_value(key, value)
        script.commit()
    WidgetDbConfig.clear_cache()
    if before != fingerprint(): raise RuntimeError('Business records changed during UI configuration')
    return dict(project='ngs_launcher', schema_nodes=len(schema.get_search_types(hierarchy=False)),
                added_configs=changed, data_unchanged=True, backup=backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = admin_client(DEFAULT_TACTIC_URL).script(inspect.getsource(configure_admin) + '\nreturn configure_admin(plan,apply)',
        plan=build_plan(), apply=args.apply, timeout=90)
    Path('reports/tactic-admin-' + ('applied' if args.apply else 'preflight') + '.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k != 'backup'}, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
