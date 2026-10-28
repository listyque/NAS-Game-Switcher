import ast
import inspect
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET

from tools.tactic_admin import TABLES, OWNER, admin_summary, build_plan, configure_admin


class TacticAdminTests(unittest.TestCase):
    def setUp(self):
        self.project='ngs_launcher';self.admin=True;self.commits=[]
        self.rows={}
        owner=self
        class Row:
            def __init__(self,kind,**values):self.kind=kind;self.values=values
            def get_value(self,key):return self.values.get(key,'')
            def set_value(self,key,value):self.values[key]=value
            def get_data(self):return dict(self.values)
            def get_search_types(self,hierarchy=False):return [n.get('name') for n in ET.fromstring(self.values['schema']).findall('search_type')]
            def commit(self):
                owner.commits.append(self.kind)
                if self not in owner.rows.setdefault(self.kind,[]):owner.rows[self.kind].append(self)
        self.Row=Row
        class Search:
            def __init__(self,kind):self.kind=kind;self.filters={}
            def add_filter(self,key,value):self.filters[key]=value
            def get_sobjects(self):return [o for o in owner.rows.get(self.kind,[]) if all(o.get_value(k)==v for k,v in self.filters.items())]
            def get_sobject(self):return next(iter(self.get_sobjects()),None)
        def schema(code,description,xml):
            obj=Row('sthpw/schema',code=code,description=description,schema=xml);obj.commit();return obj
        def module(name,**values):
            result=ModuleType(name);result.__dict__.update(values);return result
        previous=[]
        def query(sql):
            for rows in previous:
                rows[:]=[None]*len(rows)
            result=[(0,None)]
            previous.append(result)
            return result
        project=SimpleNamespace(get_code=lambda:self.project,get_database_name=lambda:self.project,
            get_value=lambda k:'NGS handheld game catalog; managed from D:/code/NGS',get_project_db_resource=lambda:'ngs_launcher')
        self.modules={
            'pyasm':module('pyasm'),
            'pyasm.biz':module('pyasm.biz',Project=SimpleNamespace(get=lambda:project),Schema=SimpleNamespace(create=schema)),
            'pyasm.common':module('pyasm.common',Environment=SimpleNamespace(get_security=lambda:SimpleNamespace(is_admin=lambda:self.admin))),
            'pyasm.search':module('pyasm.search',Search=Search,SearchType=SimpleNamespace(create=lambda kind:Row(kind)),
                WidgetDbConfig=SimpleNamespace(clear_cache=lambda:None),
                DbContainer=SimpleNamespace(get=lambda resource:SimpleNamespace(do_query=query)))
        }
        self.rows['sthpw/search_object']=[Row('sthpw/search_object',table_name=t,search_type='ngs_launcher/'+t,
            namespace='ngs_launcher',database='{project}') for t in TABLES]

    def test_plan_has_native_views_for_all_tables_and_only_real_relations(self):
        plan=build_plan()
        for t in TABLES:
            self.assertEqual({c['view'] for c in plan['configs'] if c['search_type']=='ngs_launcher/'+t},{'definition','table','edit'})
        for config in plan['configs']:
            root=ET.fromstring(config['config'])
            self.assertIsNotNone(root.find(config['view']))
            names=[e.get('name') for e in root.findall('.//element')]
            self.assertEqual(len(names),len(set(names)))
            self.assertFalse(set(names)&{'secret','password','data','state_private','published_data','import_state'})
        root=ET.fromstring(plan['schema'])
        self.assertEqual(len(root.findall('search_type')),12)
        self.assertFalse(any(c.get('to')=='ngs_launcher/player_profile' for c in root.findall('connect')))
        ast.parse(inspect.getsource(configure_admin))

    def test_connection_route_contains_no_inline_template_or_html_in_xml_options(self):
        link = next(item for item in build_plan()['links'] if item['name'] == 'ngs_connect')
        self.assertEqual(link['options'], {'class_name': 'ngs_launcher_plugin.connection_widget.ConnectionWdg'})
        # HashPanelWdg embeds these values directly in a new XML display definition.
        xml = '<element><display class="%s">%s</display></element>' % (
            link['options']['class_name'], ''.join('<%s>%s</%s>' % (key, value, key)
                                                 for key, value in link['options'].items() if key != 'class_name'))
        self.assertEqual(ET.fromstring(xml).find('display').get('class'), link['options']['class_name'])

    def test_existing_inline_connection_handler_migrates_once_and_keeps_other_nodes(self):
        plan = build_plan()
        with patch.dict(sys.modules, self.modules):
            configure_admin(plan, True)
            menu = next(row for row in self.rows['config/widget_config'] if row.get_value('search_type') == 'SideBarWdg' and row.get_value('view') == 'definition')
            root = ET.fromstring(menu.get_value('config'))
            display = root.find('.//element[@name="ngs_connect"]/display')
            display.find('class_name').text = 'tactic.ui.panel.CustomLayoutWdg'
            ET.SubElement(display, 'html').text = '<%\nbody_html="example"\n%>${body_html}'
            menu.set_value('config', ET.tostring(root, encoding='unicode'))
            old_names = [e.get('name') for e in root.find('definition')]
            configure_admin(plan, True)
            root = ET.fromstring(menu.get_value('config'))
            self.assertEqual([e.get('name') for e in root.find('definition')], old_names)
            display = root.find('.//element[@name="ngs_connect"]/display')
            self.assertEqual(display.findtext('class_name'), 'ngs_launcher_plugin.connection_widget.ConnectionWdg')
            self.assertIsNone(display.find('html'))
            commits = len(self.commits); configure_admin(plan, True)
            self.assertEqual(len(self.commits), commits)

    def test_summary_whitelists_scalars_and_escapes_markup(self):
        with patch.dict(sys.modules,self.modules):
            summary=admin_summary(dict(__search_type__='ngs_launcher/player_profile',
                data=json.dumps(dict(id='public-id',username='<img onerror=x>',password='hidden-password',salt='hidden-salt'))))
            self.assertIn('&lt;img',summary);self.assertNotIn('<img',summary)
            self.assertNotIn('hidden-',summary)
            summary=admin_summary(dict(__search_type__='ngs_launcher/catalog_config',
                data=json.dumps(dict(private_key='secret',state=dict(password='secret')))))
            self.assertNotIn('secret',summary)
            self.assertEqual(admin_summary(dict(__search_type__='ngs_launcher/player_play',data='bad')),'Некорректный JSON')

    def test_install_preserves_old_views_and_reapply_preserves_user_edits(self):
        old=self.Row('config/widget_config',search_type='ngs_launcher/game',view='table',config='<config><table><element name="name"/></table></config>')
        self.rows['config/widget_config']=[old]
        plan=build_plan()
        with patch.dict(sys.modules,self.modules):
            self.assertIn('backup',configure_admin(plan))
            self.assertEqual(self.commits,[])
            result=configure_admin(plan,True)
            self.assertEqual(result['schema_nodes'],12)
            self.assertEqual(old.get_value('config'),'<config><table><element name="name"/></table></config>')
            own=next(o for o in self.rows['config/widget_config'] if o.get_value('description')==OWNER)
            own.set_value('config','user-customized')
            count=len(self.commits)
            self.assertEqual(configure_admin(plan,True)['added_configs'],[])
            self.assertEqual(len(self.commits),count)
            self.assertEqual(own.get_value('config'),'user-customized')

    def test_connection_is_added_to_highest_priority_existing_menus_without_losing_custom_items(self):
        plan = build_plan()
        with patch.dict(sys.modules, self.modules):
            configure_admin(plan, True)
            menus = [r for r in self.rows['config/widget_config'] if r.get_value('search_type') == 'SideBarWdg']
            expected = {}
            for menu in menus:
                view = menu.get_value('view')
                root = ET.fromstring(menu.get_value('config')); node = root.find(view)
                node.remove(next(e for e in node if e.get('name') == 'ngs_connect'))
                ET.SubElement(node, 'element', name='custom_saved_link', title='My saved view')
                expected[view] = [e.get('name') for e in node]
                menu.set_value('config', ET.tostring(root, encoding='unicode'))
                lower = self.Row('config/widget_config', search_type='SideBarWdg', view=view, priority=-100,
                                 config='<config><%s><element name="ngs_connect"/></%s></config>' % (view, view))
                self.rows['config/widget_config'].append(lower)
                legacy = self.Row('config/widget_config', search_type='SideBarWdg', view=view, priority=None,
                                  config='<config><%s><element name="legacy_custom_link"/></%s></config>' % (view, view))
                self.rows['config/widget_config'].append(legacy)
            result = configure_admin(plan, True)
            self.assertEqual(set(result['added_configs']), {'SideBarWdg:definition', 'SideBarWdg:project_view'})
            for menu in menus:
                view = menu.get_value('view'); root = ET.fromstring(menu.get_value('config'))
                self.assertEqual(menu.get_value('priority'), 100)
                self.assertEqual([e.get('name') for e in root.find(view)], expected[view] + ['ngs_connect'])
                self.assertEqual(root.find('.//element[@name="custom_saved_link"]').get('title'), 'My saved view')
            legacy_menus = [r for r in self.rows['config/widget_config'] if r.get_value('search_type') == 'SideBarWdg' and r.get_data().get('priority') is None]
            for menu in legacy_menus:
                root = ET.fromstring(menu.get_value('config'))
                self.assertEqual([e.get('name') for e in root.find(menu.get_value('view'))], ['legacy_custom_link', 'ngs_connect'])
            commits = len(self.commits)
            self.assertEqual(configure_admin(plan, True)['added_configs'], [])
            self.assertEqual(len(self.commits), commits)

    def test_wrong_project_non_admin_and_changed_mapping_refuse_writes(self):
        for project,admin in [('open_heavens_cafe',True),('ngs_launcher',False)]:
            self.project,self.admin=project,admin
            with patch.dict(sys.modules,self.modules),self.assertRaises(PermissionError):configure_admin(build_plan(),True)
            self.assertEqual(self.commits,[])
        self.project,self.admin='ngs_launcher',True
        self.rows['sthpw/search_object'][0].set_value('database','foreign')
        with patch.dict(sys.modules,self.modules),self.assertRaises(ValueError):configure_admin(build_plan(),True)
        self.assertEqual(self.commits,[])


if __name__=='__main__':unittest.main()
