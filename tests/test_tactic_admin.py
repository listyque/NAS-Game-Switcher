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
