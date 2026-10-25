import ast
import base64
import copy
import inspect
import json
import hashlib
import io
import tempfile
import re
import sys
import textwrap
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

from ngs.api import validate_payload, claim_state, ApiRejected
from ngs.remote_catalog import validate_catalog_request
from tools.remote_catalog import provision, remote_source, remote_action, private_pem, review_artwork
from ngs.update_crypto import RsaKey
from ngs.install import validate_manifest, manifest_bytes, safe_path, inventory
from ngs.packages import transport_payload, package_key_payload, verify_transport
from tools.catalogizer import prepare_import


class RemoteCatalogTests(unittest.TestCase):
    def test_review_rejects_corrupt_input_and_foreign_preserved_art_before_reading(self):
        image = SimpleNamespace(open=Mock(side_effect=AssertionError('Unverified artwork opened')))
        pil = ModuleType('PIL'); pil.Image=image
        prepared = dict(current=None,details=dict(cover=dict(base64=base64.b64encode(b'bytes').decode(),sha256='00'*32)))
        before = copy.deepcopy(prepared)
        with patch.dict(sys.modules,{'PIL':pil}):
            with self.assertRaises(ValueError): review_artwork(prepared)
        self.assertEqual(prepared,before)
        snapshot = SimpleNamespace(get_value=lambda key: 'open_heavens_cafe' if key=='project_code' else '')
        biz,common = ModuleType('pyasm.biz'),ModuleType('pyasm.common')
        biz.Snapshot=SimpleNamespace(get_by_code=lambda code:snapshot);common.Environment=Mock()
        prepared.update(current=dict(artwork=dict(cover='manual'),import_state={}),proposed=dict(id='game'))
        with patch.dict(sys.modules,{'PIL':pil,'pyasm':ModuleType('pyasm'),'pyasm.biz':biz,'pyasm.common':common}):
            with self.assertRaisesRegex(ValueError,'preserved'): review_artwork(prepared)
        image.open.assert_not_called()

    def test_preview_client_accepts_only_bounded_jpeg_data(self):
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage, QColor
        from ngs.catalogizer_ui import CatalogRequest
        image = QImage(4,6,QImage.Format_RGB32); image.fill(QColor('red'))
        stream=QBuffer();stream.open(QIODevice.WriteOnly);self.assertTrue(image.save(stream,'JPEG'))
        source='data:image/jpeg;base64,'+base64.b64encode(bytes(stream.data())).decode()
        valid=dict(cover=source,background=source)
        stream=QBuffer();stream.open(QIODevice.WriteOnly);self.assertTrue(image.save(stream,'PNG'))
        png=base64.b64encode(bytes(stream.data())).decode()
        for artwork, allowed in ((valid,True),({},True),({'cover':'file:///private'},False),
                ({'cover':'https://provider.example/image.jpg'},False),
                ({'cover':'data:image/jpeg;base64,'+'A'*(16*1024+1)},False),
                ({'cover':'data:image/jpeg;base64,'+png},False)):
            worker = CatalogRequest(None,'http://localhost',{},None)
            loaded,failed=[],[];worker.loaded.connect(loaded.append);worker.failed.connect(failed.append)
            with patch('ngs.catalogizer_ui.MetadataApi') as api:
                api.return_value.call.return_value=dict(stage='review',artwork=artwork)
                worker.run()
            self.assertEqual(bool(loaded),allowed)
            self.assertEqual(bool(failed),not allowed)

    def test_only_bounded_relative_folders_and_reviewed_actions_are_accepted(self):
        for value in (dict(action="list",offset=0), dict(action="exes",folder="Gorky 17"),
                dict(action="search",query="Game"), dict(action="preview",folder="Game",executable="bin/play.exe",provider="12"),
                dict(action="publish",preview="ab"*16),
                dict(action="preview",folder="Game",executable="play.exe",provider="lutris:need-for-speed-iii-hot-pursuit"),
                dict(action="preview",folder="Game",executable="play.exe",provider="wikipedia:en:4877668")):
            self.assertEqual(validate_payload(dict(operation="catalog",value=value)), ("catalog",value))
        for value in (dict(action=[]),dict(action="list",offset=True), dict(action="execute_python_script",code="evil"),
                dict(action="exes",folder="../other"),dict(action="exes",folder="/mnt/other"),
                dict(action="exes",folder="nested/game"),dict(action="exes",folder=r"\\nas\games"),
                dict(action="list",offset=0,project="open_heavens_cafe"),dict(action="publish",preview="../../file"),
                dict(action="preview",folder="Game",executable="../evil.exe",provider="steam:12")):
            with self.subTest(value=value),self.assertRaises(ValueError): validate_catalog_request(value)
        state={}
        for i in range(200):
            state=claim_state(state,str(i),"catalog",10000);state["busy_until"]=0
        state=claim_state(state,"metadata","search",10000);state["busy_until"]=0
        self.assertEqual(len(state["recent"]),201)
        with self.assertRaises(ApiRejected): claim_state(state,"metadata","search",10000)

    def test_remote_script_and_embedded_helpers_are_valid_python(self):
        source=remote_source()
        ast.parse("def saved():\n"+textwrap.indent(source,"    "))
        self.assertNotIn("update-signing-key.bin",source)
        self.assertIn("ngs_compile",source)
        with RsaKey() as key:
            pem=private_pem(key.export(private=True))
            self.assertTrue(pem.startswith("-----BEGIN RSA PRIVATE KEY-----\n"))
            self.assertGreater(len(base64.b64decode("".join(pem.splitlines()[1:-1]))),1000)

    def test_deployment_and_execution_refuse_other_projects_before_any_writes(self):
        def module(name,**values):
            result=ModuleType(name);result.__dict__.update(values);return result
        db,search=Mock(),Mock()
        project=SimpleNamespace(get_code=lambda:"open_heavens_cafe")
        modules={"pyasm":module("pyasm"),"pyasm.biz":module("pyasm.biz",Project=SimpleNamespace(get=lambda:project),Schema=None),
            "pyasm.common":module("pyasm.common",Environment=SimpleNamespace(get_security=lambda:SimpleNamespace(is_admin=lambda:True)),Container=None),
            "pyasm.search":module("pyasm.search",Search=search,SearchType=None,DbContainer=db),
            "pyasm.security":module("pyasm.security",Login=None)}
        class Client:
            def script(self,code,**kwargs):
                ns={};exec("def run("+",".join(kwargs)+"):\n"+textwrap.indent(code,"    "),ns)
                return ns["run"](**kwargs)
        with patch.dict(sys.modules,modules):
            with self.assertRaises(PermissionError): provision(Client(),{},"private sentinel")
            with self.assertRaises(PermissionError): remote_action("list",dict(action="list",offset=0),"ab"*16,"")
        db.get.assert_not_called();search.assert_not_called()


class RemoteStateTests(unittest.TestCase):
    def test_release_requires_owned_transfer_and_works_without_source_mount(self):
        from unittest.mock import Mock
        preview = self.preview()
        obj = self.objects[preview]
        values = json.loads(obj.get_value('data'))
        values.update(state='transfer', job='package-'+'cd'*16, installers=False)
        obj.set_value('data', json.dumps(values))
        sql = SimpleNamespace(do_query=Mock(), do_update=Mock())
        self.modules['pyasm.search'].DbContainer.get = lambda resource: sql
        release = Mock(return_value=dict(state='released'))
        self.helpers['package_action'] = release
        self.helpers['GAMES_ROOT'] = str(self.root/'unmounted')
        self.assertEqual(self.call('release', preview), dict(state='released'))
        self.assertEqual(release.call_args.args[:2], ('stream_release', dict(job=values['job'])))
        sql.do_update.assert_called_once()
        release.reset_mock(); sql.do_update.reset_mock()
        config = json.loads(self.objects['service'].get_value('data'))
        config['devices'].append('ef'*16)
        self.objects['service'].set_value('data', json.dumps(config))
        with self.assertRaises(PermissionError): self.call('release', preview, 'ef'*16)
        with self.assertRaises(PermissionError): self.call('installer_release', preview)
        release.assert_not_called(); sql.do_update.assert_not_called()
        self.assertEqual(self.call('release', '01'*16), dict(state='released'))

    def test_stopped_transfer_restarts_same_job_only_three_times_and_preserves_identity(self):
        preview = self.preview()
        obj = self.objects[preview]
        values = json.loads(obj.get_value('data'))
        values.update(state='transfer', card=dict(self.prepared['proposed'], revision=1, delivery='on_request'), job='package-test', installers=False)
        obj.set_value('data', json.dumps(values))
        calls = []
        def packages(action, payload, helpers, service):
            calls.append((action, payload))
            return dict(state='error', error='WorkerStopped') if action == 'stream_status' else dict(state='running')
        self.helpers['package_action'] = packages
        for attempt in range(3):
            self.call('transfer', preview, ack=4)
            self.assertEqual([action for action, _ in calls[-3:]], ['stream_status', 'stream_build', 'stream_status'])
            self.assertEqual(calls[-1][1], dict(job='package-test', ack=4))
        before = len(calls)
        self.assertEqual(self.call('transfer', preview, ack=4), dict(state='error', error='WorkerStopped'))
        self.assertEqual(len(calls), before + 1)
        after = json.loads(obj.get_value('data'))
        self.assertEqual(after.pop('worker_restarts'), 3)
        self.assertEqual(after, values)
        with self.assertRaises(PermissionError): self.call('transfer', preview, device='ff'*16, ack=4)
        self.helpers['package_action'] = Mock(return_value=dict(state='error', error='SourceChanged'))
        values['worker_restarts'] = 0; obj.set_value('data', json.dumps(values))
        self.assertEqual(self.call('transfer', preview, ack=4)['error'], 'SourceChanged')
        self.helpers['package_action'].assert_called_once()

    def test_manual_transfer_retry_resets_worker_restart_budget_without_new_job(self):
        from ngs.portable import path_key
        preview = self.preview(); obj = self.objects[preview]
        card = dict(self.prepared['proposed'], revision=1, delivery='on_request')
        self.published.append(dict(card=card))
        values = dict(device=self.device, state='transfer', card=card, job='package-test', worker_restarts=3, installers=False)
        obj.set_value('data', json.dumps(values))
        self.helpers['path_key'] = path_key
        self.call('download', preview, game=card['id'], revision=1)
        self.assertEqual(self.package_calls, ['stream_build', 'stream_status'])
        values.pop('worker_restarts')
        self.assertEqual(json.loads(obj.get_value('data')), values)

    def test_provider_search_does_not_hold_the_publication_lock(self):
        self.assertEqual(self.call('search',query='Game')['stage'],'matches')
        self.assertEqual(self.queries,[])
        preview=self.preview()
        self.call('publish',preview)
        self.assertEqual(self.queries,['SELECT pg_advisory_xact_lock(1313297223)'])

    def test_collection_with_an_unpublished_setup_stays_visible_and_is_not_skipped_by_batch(self):
        first = dict(self.card, kind="installer", game_id="steam-12", source_folder="Game", executable="One/Setup.exe")
        records = [dict(card=first, published=first, import_state={})]
        original = self.helpers["catalog_action"]
        self.helpers["catalog_action"] = lambda action, *args, **kw: dict(records=records) if action == "inspect" else original(action, *args, **kw)
        found = dict(candidates=[dict(path="One/Setup.exe", utility=True), dict(path="Two/Setup.exe", utility=True)], limited=False, skipped=0)
        self.helpers.update(INSTALLERS_ROOT=str(self.root), scan_folders=lambda root: ["Game"], scan_executables=Mock(return_value=found))
        rows = self.call("installer_folders", offset=0, hide_published=True)["rows"]
        self.assertEqual([row["folder"] for row in rows], ["Game"])
        self.assertFalse(rows[0]["published"])
        batch = self.call("installer_batch")
        result = self.call("installer_status", batch["preview"])
        self.assertEqual(result["rows"][0]["state"], "manual")
        second = dict(first, id="setup-two", game_id="steam-13", name="Other game", executable="Two/Setup.exe")
        records.append(dict(card=second, published=second, import_state={}))
        self.assertEqual(self.call("installer_folders", offset=0, hide_published=True)["rows"], [])
        self.assertTrue(self.call("installer_folders", offset=0, hide_published=False)["rows"][0]["published"])
        exes = self.call("installer_exes", folder="Game")["rows"]
        self.assertTrue(all(entry["published"] for entry in exes))
        self.assertIn("Other game", exes[1]["status"])
        found["limited"] = True
        self.assertTrue(self.call("installer_folders", offset=0, hide_published=True)["rows"])
        self.assertEqual(self.package_calls, [])

    def test_installer_publication_uses_separate_identity_and_feed_without_building_packages(self):
        from tools.catalogizer import prepare_import
        self.helpers.update(INSTALLERS_ROOT=str(self.root), prepare_import=prepare_import, check_pe=Mock())
        original = self.helpers["catalog_action"]
        exports = []
        def catalog(action, payload, helpers, service):
            if action == "metadata_details":
                return dict(provider_id="steam:12", card=dict(self.card), cover=dict(base64="fixture"))
            if action in ("export", "activate"):
                exports.append((action, dict(payload)))
            return original(action, payload, helpers, service)
        self.helpers["catalog_action"] = catalog
        review = self.call("installer_preview", folder="Game", executable="play.exe", provider="12")
        self.assertEqual(review["card"]["kind"], "installer")
        self.assertEqual(review["card"]["game_id"], "steam-12")
        preview = review["preview"]
        self.call("installer_publish", preview)
        self.assertTrue(self.published[0]["card"]["id"].startswith("setup-"))
        self.assertEqual(self.published[0]["card"]["source_root"], r"\\NAS\Files\games_installs")
        self.assertEqual(self.published[0]["card"]["total_size"], 3)
        self.call("installer_status", preview)
        self.call("installer_status", preview)
        self.assertEqual([action for action, payload in exports], ["export", "activate"])
        self.assertTrue(all(payload["kind"] == "installer" for action, payload in exports))
        self.assertEqual(self.package_calls, [])

    def test_missing_installer_mount_returns_empty_state_and_never_uses_games(self):
        self.helpers["INSTALLERS_ROOT"] = str(self.root / "not-mounted")
        value = self.call("installer_folders", offset=0, hide_published=True)
        self.assertFalse(value["connected"])
        self.assertEqual(value["rows"], [])
        self.assertEqual(self.package_calls, [])
        with self.assertRaisesRegex(ValueError, "не подключена"):
            self.call("installer_batch")

    def test_installer_session_cannot_resume_a_game_session(self):
        self.helpers["INSTALLERS_ROOT"] = str(self.root)
        preview = self.preview()
        with self.assertRaises(PermissionError):
            self.call("installer_status", preview)
        self.assertEqual(self.call("status", preview)["stage"], "review")

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        (self.root / 'Game').mkdir()
        (self.root / 'Game/play.exe').write_bytes(b'MZ!')
        self.device='ab'*16
        self.root_key=RsaKey();self.signer=RsaKey()
        self.addCleanup(self.root_key.close);self.addCleanup(self.signer.close)
        public=self.signer.export()
        certificate=dict(public=base64.b64encode(public).decode(),signature=base64.b64encode(self.root_key.sign(package_key_payload(public))).decode())
        self.objects={};self.session_commits=[];self.queries=[];owner=self
        class Object:
            def __init__(self,values=None):self.values=values or {}
            def get_value(self,key):return self.values.get(key)
            def set_value(self,key,value):self.values[key]=value
            def get_code(self):return self.values['code']
            def commit(self,**kwargs):
                owner.objects[self.get_code()]=self
                owner.session_commits.append(kwargs)
        self.objects['service']=Object(dict(code='service',description='NGS remote catalogizer v1',
            data=json.dumps(dict(devices=[self.device],certificate=certificate,private='private sentinel'))))
        class Search:
            def __init__(self,kind):self.kind=kind;self.filters={}
            def add_filter(self,key,value):self.filters[key]=value
            def get_sobject(self):
                if self.kind=='ngs_launcher/api_device' and self.filters.get('code') in json.loads(owner.objects['service'].get_value('data'))['devices']:
                    return Object(dict(enabled=True,state=json.dumps(dict(account={'profile':'a'*32}))))
                return owner.objects.get(self.filters.get('code')) if self.kind=='ngs_launcher/catalog_config' else None
        def mod(name,**values):
            result=ModuleType(name);result.__dict__.update(values);return result
        security=SimpleNamespace(is_logged_in=lambda:True,is_admin=lambda:False,get_user_name=lambda:'ngs_catalog_worker')
        project=SimpleNamespace(get_code=lambda:'ngs_launcher',get_database_name=lambda:'ngs_launcher',get_project_db_resource=lambda:'ngs_launcher',
            get_value=lambda key:'NGS handheld game catalog; managed from D:/code/NGS')
        self.modules={'pyasm':mod('pyasm'),'pyasm.biz':mod('pyasm.biz',Project=SimpleNamespace(get=lambda:project)),
            'pyasm.common':mod('pyasm.common',Environment=SimpleNamespace(get_security=lambda:security)),
            'pyasm.search':mod('pyasm.search',Search=Search,SearchType=SimpleNamespace(create=lambda kind:Object()),
                DbContainer=SimpleNamespace(get=lambda resource:SimpleNamespace(do_query=owner.queries.append)))}
        self.card=dict(id='steam-12',name='Game',subtitle='',description='Description',genre='',accent='#d9ff82',metadata_url='https://store.steampowered.com/app/12/')
        self.prepared=dict(current=None,proposed=dict(self.card,source_folder='Game',executable='play.exe',source_root=r'\\NAS\Files\games'),
            merged=self.card,details=dict(provider_id='steam:12',card=self.card,cover=dict(base64='fixture')),edits={},preserved=[])
        document=dict(schema=1,game_id='steam-12',source_folder='Game',executable='play.exe',working_directory='',arguments=[],directories=[],
            files=[dict(path='play.exe',size=3,sha256=hashlib.sha256(b'MZ!').hexdigest())],total_size=3)
        transport=dict(schema=1,manifest_sha256=hashlib.sha256(manifest_bytes(document)).hexdigest(),chunks=[dict(size=3,sha256=hashlib.sha256(b'MZ!').hexdigest(),
            path='/assets/ngs_launcher/packages/'+hashlib.sha256(b'MZ!').hexdigest()+'.bin')])
        self.package_result=dict(document=document,transport=transport)
        self.published=[];self.package_calls=[];self.ready=False;self.conflict=False
        def catalog(action,payload,helpers,service):
            self.assertTrue(service)
            if action=='inspect':
                if not self.published:return dict(records=[])
                previous=self.published[-1]
                published=dict(previous['card'])
                if previous.get('manifest'): published['manifest']=dict(sha256=previous['manifest']['sha256'])
                return dict(records=[dict(card=previous['card'],published=published,token='ab'*32,import_state={})])
            if action=='import':
                self.published.append(payload)
                payload['cover']['bytes']=b'image bytes added by native checkin'
                return dict(card=payload['card'])
            if action=='export':return dict(snapshot='NGS-ONLY',sha256='ab'*32)
            if action=='activate' and self.conflict:raise ValueError('A newer export exists')
            return {}
        def packages(action,payload,helpers,service):
            self.assertTrue(service);self.package_calls.append(action)
            if action=='queue':return dict(job='package-test')
            if action=='status':return dict(state='ready',result=self.package_result) if self.ready else dict(state='running',done=1,total=3)
            return dict(state='running')
        self.helpers=dict(re=re,GAMES_ROOT=str(self.root),validate_catalog_request=validate_catalog_request,
            review_artwork=Mock(return_value=dict(cover='data:image/jpeg;base64,fixture',background='data:image/jpeg;base64,fixture')),
            safe_path=safe_path, inventory=Mock(wraps=inventory), PACKAGE_SIZE=256 * 1024 * 1024,
            catalog_action=catalog,package_action=packages,validate_manifest=validate_manifest,transport_payload=transport_payload,
            server_sign=lambda private,data:self.signer.sign(data))

    def call(self,action,preview=None,device=None,**fields):
        original_open=open
        def opened(path,*args,**kwargs):
            if path=='/proc/self/mountinfo':return io.StringIO('0 0 0 / '+str(self.root)+' ro - x x x\n')
            return original_open(path,*args,**kwargs)
        with patch.dict(sys.modules,self.modules),patch('tools.remote_catalog.exec',side_effect=lambda source,ns:ns.update(self.helpers),create=True),patch('builtins.open',side_effect=opened):
            value=dict(action=action,**fields)
            if preview is not None:value['preview']=preview
            return remote_action(action,value,device or self.device,'trusted fixed helpers')

    def preview(self):
        obj=type(self.objects['service'])(dict(code='cd'*16,description='NGS remote catalogizer v1',data=json.dumps(dict(device=self.device,created=__import__('time').time(),state='review',prepared=self.prepared))))
        obj.commit();return obj.get_code()

    def test_publication_measures_size_once_without_reading_or_packaging_contents(self):
        preview=self.preview()
        self.session_commits.clear()
        self.assertEqual(self.call('status',preview)['stage'],'review')
        self.assertIn('cover',self.call('status',preview)['artwork'])
        self.assertEqual(self.package_calls,[])
        self.assertEqual(self.call('publish',preview)['stage'],'working')
        self.call('publish',preview)
        self.assertEqual(self.package_calls,[])
        self.assertEqual(self.published[0]['card']['delivery'],'on_request')
        self.assertEqual(self.published[0]['card']['total_size'],3)
        self.assertNotIn('prepared',json.loads(self.objects[preview].get_value('data')))
        self.helpers['inventory'].assert_called_once()
        self.assertNotIn('manifest',self.published[0])
        self.assertEqual(self.call('status',preview)['state'],'activate')
        self.conflict=True
        self.assertEqual(self.call('status',preview)['state'],'export')
        self.conflict=False
        self.call('status',preview)
        finished = self.call('status',preview)
        self.assertEqual(finished['state'],'done')
        self.assertEqual(finished['card']['id'], self.published[-1]['card']['id'])
        self.assertEqual(len(self.published),1)
        self.assertTrue(all(kwargs==dict(triggers='none',log_transaction=False) for kwargs in self.session_commits))

    def test_provider_failure_is_reported_as_metadata_failure_without_publishing(self):
        for failure in (RuntimeError('Provider unavailable'), OSError('Network unavailable')):
            self.helpers['catalog_action'] = Mock(side_effect=failure)
            with self.assertRaisesRegex(ValueError, 'Источники оформления'):
                self.call('search', query='Unknown Game')
            self.assertEqual(self.published, [])
            self.assertEqual(self.package_calls, [])

    def test_legacy_preparation_already_in_progress_can_finish(self):
        preview=self.preview()
        values=json.loads(self.objects[preview].get_value('data'))
        values.update(state='working',job='package-test')
        self.objects[preview].set_value('data',json.dumps(values))
        self.assertEqual(self.call('status',preview)['done'],1)
        self.ready=True
        result=self.call('status',preview)
        self.assertEqual(result['state'],'export')
        data=base64.b64decode(self.published[0]['manifest']['base64'])
        verify_transport(json.loads(data),self.root_key.export())
        self.assertNotIn('private sentinel',json.dumps(result))

    def test_filter_and_bulk_import_skip_published_and_leave_ambiguous_games_for_review(self):
        from ngs.bulk_scan import title_key
        old = dict(self.prepared['proposed'], id='existing', name='Existing', source_folder='Existing',
                   metadata_url='https://store.steampowered.com/app/42/')
        self.published.append(dict(card=old))
        folders = ['Existing', 'Good', 'Ambiguous', 'Unknown']
        for folder in folders: (self.root / folder).mkdir()
        original = self.helpers['catalog_action']
        def catalog(action, payload, helpers, service):
            if action == 'metadata_search':
                return [dict(name='Good',app_id='12')] if payload['value']=='Good' else [dict(name='Other',app_id='13')]
            if action == 'metadata_details':
                details = copy.deepcopy(self.prepared['details']); details['card']['name']='Good'; return details
            return original(action,payload,helpers,service)
        self.helpers.update(catalog_action=catalog, safe_path=safe_path, scan_folders=lambda root:folders,
            scan_executables=lambda path:dict(candidates=[dict(path='play.exe',utility=False)] * (2 if path.name=='Ambiguous' else 1), limited=False, skipped=0),
            prepare_import=prepare_import,title_key=title_key,search_title=lambda value:value,check_pe=lambda path:None,
            inventory=Mock(wraps=inventory))
        filtered=self.call('folders',offset=0,hide_published=True)
        self.assertEqual([row['folder'] for row in filtered['rows']],folders[1:])
        self.assertEqual(self.call('folders',offset=0,hide_published=False)['total'],4)
        started=self.call('batch');preview=started['preview']
        for unused in range(12):
            result=self.call('status',preview)
            if result['state']=='done':break
        self.assertEqual(result['summary'],dict(added=1,already=1,manual=2,error=0),result)
        self.assertEqual(result['selected_id'], self.published[-1]['card']['id'])
        self.assertEqual([row['folder'] for row in result['rows']],['Ambiguous','Unknown'])
        self.assertEqual(len(self.call('results',preview,offset=0,hide_published=False)['rows']),4)
        self.assertEqual(self.package_calls,[])
        self.assertEqual(self.published[-1]['card']['delivery'],'on_request')
        self.assertEqual(self.published[-1]['card']['total_size'],0)
        self.helpers['inventory'].assert_called_once()

    def test_known_games_db_resolves_abbreviated_folder_and_multi_exe_with_official_title(self):
        from ngs.bulk_scan import title_key
        from ngs.game_db import match_known_game, select_best_executable, folder_store_hint
        from ngs.metadata import search_title, search_queries
        folders = ['plucky', 'Diablo 2 Resurrected']
        for folder in folders: (self.root / folder).mkdir()
        original = self.helpers['catalog_action']
        def catalog(action, payload, helpers, service):
            if action == 'metadata_search':
                if payload['value'] == 'The Plucky Squire':
                    return [dict(name='The Plucky Squire', app_id='steam:1627570')]
                if payload['value'] == 'Diablo II: Resurrected':
                    return [dict(name='Diablo II: Resurrected', app_id='lutris:diablo-ii-resurrected')]
                return []
            if action == 'metadata_details':
                details = copy.deepcopy(self.prepared['details'])
                if '1627570' in str(payload['value']):
                    details['card']['id'] = 'steam-1627570'
                    details['card']['name'] = 'The Plucky Squire'
                    details['provider_id'] = 'steam:1627570'
                else:
                    details['card']['id'] = 'lutris-d2r'
                    details['card']['name'] = 'Diablo II: Resurrected'
                    details['card']['metadata_url'] = 'https://lutris.net/games/diablo-ii-resurrected/'
                    details['provider_id'] = 'lutris:diablo-ii-resurrected'
                return details
            return original(action, payload, helpers, service)
        def scan_exes(path):
            if path.name == 'plucky':
                return dict(candidates=[
                    dict(path='Storybook/Binaries/Win64/Storybook-Win64-Shipping.exe', size=90000000, utility=False),
                    dict(path='Engine/Binaries/Win64/CrashReportClient.exe', size=1000000, utility=True),
                ], limited=False, skipped=0)
            return dict(candidates=[
                dict(path='Diablo II Resurrected Launcher.exe', size=2000000, utility=False),
                dict(path='D2R.exe', size=45000000, utility=False),
                dict(path='BlizzardError.exe', size=800000, utility=True),
            ], limited=False, skipped=0)
        self.helpers.update(
            catalog_action=catalog, safe_path=safe_path, scan_folders=lambda root: folders,
            scan_executables=scan_exes, prepare_import=prepare_import, title_key=title_key,
            search_title=search_title, search_queries=search_queries,
            match_known_game=match_known_game, select_best_executable=select_best_executable,
            folder_store_hint=folder_store_hint, check_pe=lambda path: None,
            inventory=Mock(wraps=inventory))
        exes_d2r = self.call('exes', folder='Diablo 2 Resurrected')
        self.assertEqual(exes_d2r['rows'][0]['path'], 'D2R.exe')
        started = self.call('batch'); preview = started['preview']
        for unused in range(10):
            result = self.call('status', preview)
            if result['state'] == 'done': break
        self.assertEqual(result['summary'], dict(added=2, already=0, manual=0, error=0), result)
        names_and_exes = {(item['card']['name'], item['card']['executable'], item['card']['source_folder']) for item in self.published}
        self.assertIn(('The Plucky Squire', 'Storybook/Binaries/Win64/Storybook-Win64-Shipping.exe', 'plucky'), names_and_exes)
        self.assertIn(('Diablo II: Resurrected', 'D2R.exe', 'Diablo 2 Resurrected'), names_and_exes)

    def test_estimate_uses_published_size_without_visiting_game_folder(self):
        from ngs.portable import path_key
        self.helpers['path_key'] = path_key
        card = dict(self.prepared['proposed'], revision=1, delivery='on_request', total_size=987654321)
        self.published.append(dict(card=card))
        self.helpers['inventory'].side_effect = AssertionError('Repeated folder scan')
        result = self.call('estimate', game=card['id'], revision=1)
        self.assertEqual(result['total_size'],987654321)
        self.assertEqual(result['workspace'],5*(self.helpers['PACKAGE_SIZE']+1024*1024))
        self.helpers['inventory'].assert_not_called()

    def test_unknown_device_or_foreign_preview_cannot_start_or_cancel_jobs(self):
        preview=self.preview()
        with self.assertRaises(PermissionError):self.call('publish',preview,'ef'*16)
        config=json.loads(self.objects['service'].get_value('data'));config['devices'].append('ef'*16)
        self.objects['service'].set_value('data',json.dumps(config))
        with self.assertRaises(PermissionError):self.call('cancel',preview,'ef'*16)
        self.assertEqual(self.package_calls,[])
        self.assertTrue(self.call('cancel',preview)['cancelled'])
        self.assertNotIn('prepared',json.loads(self.objects[preview].get_value('data')))
        self.ready=True;self.call('status',preview)
        self.assertEqual(self.published,[])

    def test_other_source_cannot_be_silently_served_from_mounted_folder(self):
        from ngs.portable import path_key
        self.helpers['path_key']=path_key
        self.published.append(dict(card=dict(self.prepared['proposed'],revision=1,delivery='on_request',source_root=r'\\OtherNAS\games')))
        with self.assertRaisesRegex(ValueError,'Источник'):
            self.call('estimate',game=self.prepared['proposed']['id'],revision=1)
        self.assertEqual(self.package_calls,[])

    def test_independent_server_uses_its_own_source_identity(self):
        from ngs.portable import path_key
        self.helpers['path_key']=path_key
        service=self.objects['service']
        settings=json.loads(service.get_value('data'))
        settings['source_roots']=dict(games=r'\\NGS\Games')
        service.set_value('data',json.dumps(settings))
        self.published.append(dict(card=dict(self.prepared['proposed'],revision=1,
            delivery='on_request',source_root=r'\\NGS\Games',total_size=3)))
        self.assertEqual(self.call('estimate',game=self.card['id'],revision=1)['total_size'],3)
        self.published[-1]['card']['source_root']=r'\\OtherNAS\games'
        with self.assertRaisesRegex(ValueError,'Источник'):
            self.call('estimate',game=self.card['id'],revision=1)


if __name__=="__main__":unittest.main()
