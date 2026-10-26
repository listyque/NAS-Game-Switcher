import base64
import copy
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from ngs.catalog import validate_card
from ngs.metadata import steam_metadata, search_title, client_metadata
from ngs.portable import PortableStorage
from tools.catalogizer import scan_folders, metadata
from tools.tactic_catalog import IMPORT_FIELDS, catalog_action, contract_source, merge_import, record_token


class MetadataTests(unittest.TestCase):
    def test_expired_metadata_works_offline_without_refreshing_its_age_or_masking_security_errors(self):
        import time
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            rows = [dict(app_id='12', name='Known Game')]
            with patch('ngs.api.MetadataApi') as remote:
                remote.return_value.call.return_value = rows
                client_metadata(storage, 'http://nas/tactic', 'search', 'Known Game', threading.Event())
                cached = next(storage.cache.glob('local-metadata-*.json'))
                cached.write_text(json.dumps(dict(saved=time.time()-86400*30, result=rows)))
                before = cached.read_bytes()
                remote.reset_mock()
                self.assertEqual(client_metadata(storage, 'http://nas/tactic', 'search', 'Known Game', threading.Event(), offline=True), rows)
                remote.assert_not_called()
                remote.return_value.call.side_effect = ConnectionError('Network lost')
                self.assertEqual(client_metadata(storage, 'http://nas/tactic', 'search', 'Known Game', threading.Event()), rows)
                self.assertEqual(cached.read_bytes(), before)
                remote.return_value.call.side_effect = RuntimeError('Invalid signature')
                with self.assertRaisesRegex(RuntimeError, 'signature'):
                    client_metadata(storage, 'http://nas/tactic', 'search', 'Known Game', threading.Event())
                with self.assertRaises(ConnectionError):
                    client_metadata(storage, 'http://other/tactic', 'search', 'Known Game', threading.Event(), offline=True)

    def setUp(self):
        import sys
        name='_ngs_launcher_provider_cache'
        previous=sys.modules.pop(name,None)
        def restore():
            sys.modules.pop(name,None)
            if previous is not None: sys.modules[name]=previous
        self.addCleanup(restore)

    def test_server_provider_cache_reuses_success_but_rechecks_limits_expiry_and_failures(self):
        import urllib.error
        from ngs.metadata import metadata_fetch
        url='https://store.steampowered.com/api/test-cache'
        opened=unittest.mock.Mock()
        opened.open.side_effect=lambda *a,**k:io.BytesIO(b'public-data')
        with patch('urllib.request.build_opener',return_value=opened),patch('time.monotonic',return_value=100):
            self.assertEqual(metadata_fetch(url,32),b'public-data')
            self.assertEqual(metadata_fetch(url,32),b'public-data')
            self.assertEqual(opened.open.call_count,1)
            with self.assertRaisesRegex(ValueError,'size limit'): metadata_fetch(url,2)
            with self.assertRaisesRegex(ValueError,'Untrusted'): metadata_fetch('http://127.0.0.1/private',32)
            self.assertEqual(opened.open.call_count,1)
        with patch('urllib.request.build_opener',return_value=opened),patch('time.monotonic',return_value=401):
            self.assertEqual(metadata_fetch(url,32),b'public-data')
            self.assertEqual(opened.open.call_count,2)
            opened.open.side_effect=urllib.error.URLError('unavailable')
            for _ in range(2):
                with self.assertRaises(urllib.error.URLError): metadata_fetch(url+'-error',32)
            self.assertEqual(opened.open.call_count,4)

    def test_server_provider_cache_has_entry_and_byte_limits(self):
        import sys
        from ngs.metadata import metadata_fetch
        opened=SimpleNamespace(open=lambda *a,**k:io.BytesIO(b'x'))
        with patch('urllib.request.build_opener',return_value=opened):
            for i in range(35): metadata_fetch('https://store.steampowered.com/api/cache?i='+str(i),32)
        items=sys.modules['_ngs_launcher_provider_cache'].items
        self.assertEqual(len(items),32)
        large=b'x'*(6*1024*1024)
        with patch('urllib.request.build_opener',return_value=SimpleNamespace(open=lambda *a,**k:io.BytesIO(large))):
            for i in range(3): metadata_fetch('https://shared.akamai.steamstatic.com/cache-'+str(i),8*1024*1024)
        self.assertLessEqual(sum(len(data) for saved,data in items.values()),16*1024*1024)

    def test_client_search_cache_reuses_success_and_revalidates_expired_corrupt_or_other_server_data(self):
        import time
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            rows = [dict(app_id="12", name="Known Game")]
            with patch("ngs.api.MetadataApi") as remote:
                remote.return_value.call.return_value = rows
                for _ in range(2):
                    self.assertEqual(client_metadata(storage, "http://nas/tactic", "search", "Known Game", threading.Event()), rows)
                self.assertEqual(remote.return_value.call.call_count, 1)
                cached = next(storage.cache.glob("local-metadata-*.json"))
                cached.write_text(json.dumps(dict(saved=time.time(), result=[dict(app_id="bad", name="Broken")])))
                client_metadata(storage, "http://nas/tactic", "search", "Known Game", threading.Event())
                self.assertEqual(remote.return_value.call.call_count, 2)
                cached.write_text(json.dumps(dict(saved=time.time() - 1000, result=rows)))
                client_metadata(storage, "http://nas/tactic", "search", "Known Game", threading.Event())
                self.assertEqual(remote.return_value.call.call_count, 3)
                client_metadata(storage, "http://other/tactic", "search", "Known Game", threading.Event())
                self.assertEqual(remote.return_value.call.call_count, 4)

    def test_game_folder_appid_handles_delisted_titles_and_respects_manual_search(self):
        from tools.catalogizer import folder_steam_id
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            game=Path(root).resolve()/'Project CARS 3';game.mkdir()
            (game/'steam_emu.ini').write_text('[Game]\nAppId=958400\nUserName=private fixture\n')
            self.assertEqual(folder_steam_id(game.parent,game.name),'958400')
            row=dict(app_id='958400',name='Project CARS 3')
            with patch('tools.catalogizer.invoke',return_value=[row]) as remote:
                self.assertEqual(metadata(None,'search',game.name,source=game.parent,folder=game.name),[row])
                remote.assert_called_once_with(None,'metadata_search',{'value':'958400'})
            with patch('tools.catalogizer.invoke',return_value=[]) as remote,patch('tools.catalogizer.folder_steam_id') as hint:
                self.assertEqual(metadata(None,'search','Different title',source=game.parent,folder=game.name),[])
                hint.assert_not_called()
                remote.assert_called_once_with(None,'metadata_search',{'value':'Different title'})
            with patch('tools.catalogizer.invoke',side_effect=[[],[row]]) as remote:
                self.assertEqual(metadata(None,'search',game.name,source=game.parent,folder=game.name),[row])
                self.assertEqual([c.args[2]['value'] for c in remote.call_args_list],['958400',game.name])

    def test_game_folder_appid_is_bounded_unambiguous_and_read_only(self):
        from tools.catalogizer import folder_steam_id
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            game=Path(root).resolve()/'Game';game.mkdir()
            config=game/'steam_emu.ini'
            for text in ('AppId=12\nAppId=13','AppId=12\n'+'x'*65536,'AppId=0','AppId=12/../../private'):
                config.write_text(text)
                self.assertIsNone(folder_steam_id(game.parent,game.name))
                self.assertEqual(config.read_text(),text)
            (game/'steam_appid.txt').write_text('958400\n')
            self.assertEqual(folder_steam_id(game.parent,game.name),'958400')
            with self.assertRaises(ValueError):folder_steam_id(game.parent,'../Game')
            self.assertIsNone(folder_steam_id(game.parent/'missing','Game'))

    def test_details_cache_avoids_network_and_rejects_corrupt_expired_or_other_provider_data(self):
        from tools.catalogizer import metadata
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage = PortableStorage(root)
            data = b'image'
            image = dict(base64=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest())
            details = dict(provider_id='steam:12', card=dict(name='Game'), cover=image, background=image)
            with patch('tools.catalogizer.invoke', return_value=details) as server:
                self.assertEqual(metadata(None,'details','12',storage),details)
                self.assertEqual(metadata(None,'details','12',storage),details)
                self.assertEqual(server.call_count,1)
                cached = next(storage.cache.glob('catalogizer-metadata-*.json'))
                broken = copy.deepcopy(details); broken['cover']['sha256']='0'*64
                cached.write_text(json.dumps(broken))
                self.assertEqual(metadata(None,'details','12',storage),details)
                self.assertEqual(server.call_count,2)
                import os,time
                os.utime(cached,(time.time()-90000,time.time()-90000))
                self.assertEqual(metadata(None,'details','12',storage),details)
                self.assertEqual(server.call_count,3)
                broken = dict(details,provider_id='steam:13')
                cached.write_text(json.dumps(broken))
                self.assertEqual(metadata(None,'details','12',storage),details)
                self.assertEqual(server.call_count,4)

    def test_catalogizer_can_retry_provider_from_pc_without_writing_tactic(self):
        with patch('tools.catalogizer.invoke', side_effect=RuntimeError('405')) as invoke, patch('tools.catalogizer.game_metadata', return_value=[]) as provider:
            self.assertEqual(metadata(None, 'search', 'Game'), [])
        invoke.assert_called_once_with(None, 'metadata_search', {'value': 'Game'})
        provider.assert_called_once_with('search', 'Game')

    def test_title_search_validation_and_provider_cleanup(self):
        self.assertEqual(search_title('The.Last.of.Us.Part.II.Remastered.v1.2-Repack'), 'The Last of Us Part II Remastered')
        opener = SimpleNamespace(open=lambda *a, **k: io.BytesIO(json.dumps({'items': [
            {'id': 12, 'name': '<b>Game &amp; II</b>', 'type': 'app'}, {'id': 13, 'name': 'DLC', 'type': 'bundle'}]}).encode()))
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(steam_metadata('search', 'Game II'), [{'app_id': '12', 'name': 'Game & II'}])
            for operation, value in (('search', ''), ('search', 'a\n'), ('details', '../12'), ('details', 0)):
                with self.assertRaises(ValueError): steam_metadata(operation, value)

    def test_provider_refuses_arbitrary_image_hosts_and_oversized_responses(self):
        details = {'12': {'success': True, 'data': {'type': 'game', 'name': 'Game', 'header_image': 'http://127.0.0.1/private'}}}
        def response(request, **kwargs):
            if '/api/appdetails?' in request.full_url:
                return io.BytesIO(json.dumps(details).encode())
            raise OSError('no library image')
        opened = SimpleNamespace(open=unittest.mock.Mock(side_effect=response))
        with patch('urllib.request.build_opener', return_value=opened), self.assertRaisesRegex(ValueError, 'Untrusted'):
            steam_metadata('details', '12')
        self.assertTrue(all('127.0.0.1' not in call.args[0].full_url for call in opened.open.call_args_list))
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=lambda *a, **k: io.BytesIO(b'x' * (2 * 1024 * 1024 + 1)))):
            with self.assertRaisesRegex(ValueError, 'size limit'): steam_metadata('search', 'game')

    def test_three_way_merge_preserves_manual_and_legacy_fields(self):
        proposed = {key: key for key in IMPORT_FIELDS}
        first, state, preserved = merge_import(None, proposed, {}, {})
        self.assertEqual(first, proposed)
        current = dict(first, name='Manual name')
        updated = dict(proposed, name='Provider name', description='New description')
        merged, state2, preserved = merge_import(current, updated, state, {})
        self.assertEqual(merged['name'], 'Manual name')
        self.assertEqual(merged['description'], 'New description')
        self.assertIn('name', preserved)
        locked, state3, _ = merge_import(merged, updated, state2, {'description': 'My description'})
        again, _, _ = merge_import(locked, dict(updated, description='Overwritten?'), state3, {})
        self.assertEqual(again['description'], 'My description')
        legacy, _, fields = merge_import(current, updated, {}, {})
        self.assertEqual(legacy['name'], 'Manual name')
        self.assertEqual(legacy['description'], current['description'])
        self.assertEqual(set(fields), set(IMPORT_FIELDS) - {'source_folder', 'executable'})
        self.assertNotEqual(record_token(current), record_token(merged))
        with self.assertRaises(ValueError): merge_import(None, updated, {}, {'status': 'published'})

    def test_nas_scan_only_lists_first_level_folders(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            root = Path(root).resolve()
            (root / 'One' / 'nested').mkdir(parents=True)
            (root / 'two').mkdir()
            (root / 'readme.txt').write_text('ignore')
            self.assertEqual(scan_folders(root), ['One', 'two'])
            updates = root / 'NGS'; updates.mkdir()
            marker = updates / 'NGS-updates.json'
            marker.write_text('{"schema":1,"application":"NAS Game Switcher updater"}')
            self.assertEqual(scan_folders(root), ['One', 'two'])
            marker.write_text('not json')
            self.assertEqual(scan_folders(root), ['NGS', 'One', 'two'])

    def test_client_uses_signed_server_api_without_local_steam_or_pending_sync(self):
        card = dict(id='steam-12', name='Game', subtitle='', genre='', description='Description', accent='#d9ff82',
                    source_folder='', executable='', metadata_url='https://store.steampowered.com/app/12/', available_on_nas=False,
                    revision=1,cover={'path':'/assets/ngs_launcher/c.jpg','sha256':'0'*64,'size':1},
                    background={'path':'/assets/ngs_launcher/b.jpg','sha256':'1'*64,'size':1})
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            storage=PortableStorage(root)
            with patch('ngs.api.MetadataApi') as remote, patch('ngs.metadata.steam_metadata') as steam, patch('ngs.catalog.CatalogCache.cache_artwork') as artwork:
                remote.return_value.call.return_value={'card':card,'status':'published'}
                events = []
                result=client_metadata(storage,'http://nas/tactic','resolve','12',threading.Event(), progress=lambda *args: events.append(args))
                remote.return_value.call.assert_called_once_with('resolve','12')
                steam.assert_not_called();artwork.assert_called_once()
                self.assertEqual([event[0] for event in events], ['metadata_server', 'metadata_artwork'])
                self.assertFalse(result['card']['available_on_nas'])
                self.assertFalse((storage.state/'metadata-pending').exists())
                remote.return_value.call.side_effect=RuntimeError('API offline')
                with self.assertRaises(RuntimeError): client_metadata(storage,'http://nas/tactic','search','Game',threading.Event())
                steam.assert_not_called()


class ServerImportTests(unittest.TestCase):
    def test_on_request_delivery_survives_import_and_later_metadata_publication(self):
        payload = self.payload()
        payload['available_on_nas'] = True
        payload['card'].update(source_folder='Game', executable='play.exe', delivery='on_request', total_size=123456)
        first = self.call('import', payload)
        self.assertEqual(first['card']['delivery'], 'on_request')
        self.assertEqual(first['card']['total_size'], 123456)
        self.assertNotIn('manifest', first['card'])
        second = self.call('publish', dict(game=first['game']))
        self.assertEqual(second['card']['delivery'], 'on_request')
        self.assertEqual(second['card']['total_size'], 123456)
        payload = self.payload(self.record()['token'])
        payload['available_on_nas'] = True
        payload['card'].update(source_folder='Game', executable='play.exe', total_size=789012)
        changed = self.call('import', payload)
        self.assertEqual(changed['card']['total_size'], 789012)
        self.assertGreater(changed['revision'], second['revision'])

    def test_readonly_inspection_does_not_wait_for_publication_lock_but_writes_still_lock(self):
        self.call('import',self.payload())
        self.assertTrue(self.queries)
        self.queries.clear()
        self.record()
        self.assertEqual(self.queries,[])
        self.call('import',self.payload(self.record()['token']))
        self.assertTrue(any('pg_advisory_xact_lock' in q for q in self.queries))

    def test_epic_gog_bindings_reuse_card_and_reject_duplicates(self):
        first=self.call('import',self.payload())
        for provider, url in (('epic:alan-wake-2','https://store.epicgames.com/p/alan-wake-2'),
                              ('gog:123','https://www.gog.com/game/game'),
                              ('lutris:game','https://lutris.net/games/game/'),
                              ('wikipedia:en:123','https://en.wikipedia.org/?curid=123')):
            payload=self.payload(self.record()['token'])
            payload['provider_id']=provider;payload['card']['metadata_url']=url
            changed=self.call('import',payload)
            self.assertEqual(changed['game'],first['game'])
        self.assertEqual(set(self.record()['import_state']['provider_ids']),{'steam:12','epic:alan-wake-2','gog:123','lutris:game','wikipedia:en:123'})
        for reference in ('12','epic:alan-wake-2','gog:123','lutris:game','wikipedia:en:123'):
            self.assertEqual(self.call('metadata_resolve',{'value':reference})['card'],changed['card'])
        duplicate=copy.deepcopy(payload);duplicate['card']['id']='gog-123';duplicate['expected']=None
        with self.assertRaisesRegex(ValueError,'уже связана|already linked'): self.call('import',duplicate)

    def test_same_folder_name_in_different_nas_roots_is_not_a_duplicate(self):
        from tools.catalogizer import encoded
        def payload(identity, root, provider):
            result = self.payload()
            result.update(available_on_nas=True, provider_id=provider)
            result["card"].update(id=identity, source_root=str(root), source_folder="Same folder", executable="play.exe",
                                  metadata_url="https://store.steampowered.com/app/" + provider.split(":")[1] + "/")
            manifest = dict(schema=1, game_id=identity, source_folder="Same folder", executable="play.exe",
                            files=[dict(path="play.exe", size=4, sha256=hashlib.sha256(b"game").hexdigest())],
                            directories=[], total_size=4, arguments=[], working_directory="")
            result["manifest"] = encoded(json.dumps(manifest).encode())
            return result
        first = self.call("import", payload("first", self.root / "nas-one", "steam:12"))
        second = self.call("import", payload("second", self.root / "nas-two", "steam:13"))
        self.assertNotEqual(first["card"]["source_root"], second["card"]["source_root"])
        records = self.call("inspect", {})["records"]
        self.assertEqual({r["card"]["source_root"] for r in records}, {str(self.root / "nas-one"), str(self.root / "nas-two")})
        with self.assertRaisesRegex(ValueError, "уже связана|already linked"):
            self.call("import", payload("duplicate", self.root / "nas-one", "steam:14"))
        obj = self.objects["first"]
        published = json.loads(obj.get_value("published_data"))
        original_manifest = published["manifest"]
        published.pop("source_root")
        obj.set_value("published_data", json.dumps(published))
        state = json.loads(obj.get_value("import_state")); state.pop("source_root")
        obj.set_value("import_state", json.dumps(state))
        count = len(self.checked)
        rebound = payload("first", self.root / "nas-one", "steam:12")
        rebound["expected"] = next(r["token"] for r in self.call("inspect", {})["records"] if r["card"]["id"] == "first")
        updated = self.call("import", rebound)
        self.assertEqual(updated["card"]["manifest"], original_manifest)
        self.assertEqual(len(self.checked), count)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.assets = self.root / 'assets'; self.assets.mkdir()
        (self.assets / 'ngs_launcher').mkdir()
        self.objects, self.snapshots, self.checked = {}, {}, []
        self.queries = []
        owner = self
        class Object:
            def __init__(self, kind, values=None): self.kind, self.values = kind, values or {}
            def get_value(self, key): return self.values.get(key, -1 if key == 'id' else None)
            def get_code(self): return self.get_value('code')
            def get_search_key(self): return self.get_code()
            def set_value(self, key, value): self.values[key] = value
            def commit(self):
                self.values.setdefault('id', len(owner.objects) + 1)
                owner.objects[self.get_code()] = self
        self.Object = Object
        class Search:
            def __init__(self, kind): self.kind, self.filters = kind, []
            def add_filter(self, key, value): self.filters.append((key, value))
            def add_order_by(self, key): pass
            def get_sobjects(self): return [o for o in owner.objects.values() if o.kind == self.kind and all(o.get_value(k) == v for k,v in self.filters)]
            def get_sobject(self): return next(iter(self.get_sobjects()), None)
        def checkin(key, context, name, **kwargs):
            data = (self.root / 'upload' / name).read_bytes()
            code = 'SNAPSHOT%04d' % (len(self.snapshots) + 1)
            path = self.assets / 'ngs_launcher' / (code + Path(name).suffix)
            path.write_bytes(data)
            obj = Object('snapshot', dict(code=code, project_code='ngs_launcher', search_type='ngs_launcher/game', search_code=key, context=context))
            obj.get_lib_path_by_type = lambda kind: str(path)
            self.snapshots[code] = obj
            self.checked.append(code)
            return {'code': code}
        def module(name, **kwargs):
            mod = ModuleType(name); mod.__dict__.update(kwargs); return mod
        project = Object('project', dict(code='ngs_launcher', description='NGS handheld game catalog; managed from D:/code/NGS'))
        project.get_database_name = lambda: 'ngs_launcher'
        project.get_project_db_resource = lambda: 'ngs_launcher'
        environment = SimpleNamespace(get_security=lambda: SimpleNamespace(is_admin=lambda: True),
            get_asset_dir=lambda: str(self.assets), get_upload_dir=lambda: str(self.root / 'upload'))
        image = SimpleNamespace(format='PNG', size=(4,4), width=4, height=4, verify=lambda: None)
        self.modules = {
            'pyasm': module('pyasm'),
            'pyasm.biz': module('pyasm.biz', Project=SimpleNamespace(get=lambda: project), Schema=None,
                Snapshot=SimpleNamespace(get_by_code=lambda code: self.snapshots.get(code))),
            'pyasm.common': module('pyasm.common', Environment=environment, Container=None),
            'pyasm.search': module('pyasm.search', Search=Search, SearchType=SimpleNamespace(create=lambda kind: Object(kind)),
                DbContainer=SimpleNamespace(get=lambda resource: SimpleNamespace(do_query=lambda sql: self.queries.append(sql))), WidgetDbConfig=None),
            'tactic_client_lib': module('tactic_client_lib', TacticServerStub=SimpleNamespace(get=lambda **kw: SimpleNamespace(simple_checkin=checkin))),
            'PIL': module('PIL', Image=SimpleNamespace(open=lambda stream: image)),
        }
        self.card = {key: '' for key in IMPORT_FIELDS}
        self.card.update(id='steam-12', name='Game', description='Original', accent='#d9ff82')
        data = b'image'
        self.art = {'base64': base64.b64encode(data).decode(), 'sha256': hashlib.sha256(data).hexdigest()}

    def call(self, action, payload):
        with patch.dict('sys.modules', self.modules): return catalog_action(action, copy.deepcopy(payload), contract_source())

    def payload(self, expected=None):
        return dict(card=self.card.copy(), provider_id='steam:12', confirmed=True, available_on_nas=False,
                    expected=expected, edits={}, cover=self.art.copy(), background=self.art.copy())

    def record(self): return self.call('inspect', {})['records'][0]

    def test_reimport_is_idempotent_and_manual_fields_survive(self):
        result = self.call('import', self.payload())
        self.assertEqual(result['revision'], 1)
        self.assertFalse(result['card']['available_on_nas'])
        self.assertEqual(len(self.checked), 2)
        same = self.call('import', self.payload(self.record()['token']))
        self.assertEqual(same['status'], 'unchanged')
        self.assertEqual(len(self.checked), 2)
        self.objects['steam-12'].set_value('description', 'My manual description')
        changed = self.call('import', self.payload(self.record()['token']))
        self.assertEqual(changed['card']['description'], 'My manual description')
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(len(self.checked), 2)

    def test_stale_revision_duplicates_and_invalid_manifest_do_not_publish(self):
        self.call('import', self.payload())
        before = self.record()
        self.objects['steam-12'].set_value('name', 'Changed concurrently')
        with self.assertRaisesRegex(ValueError, 'TACTIC'): self.call('import', self.payload(before['token']))
        duplicate = self.payload(); duplicate['card']['id'] = 'another-id'
        with self.assertRaisesRegex(ValueError, 'уже связана|already linked'): self.call('import', duplicate)
        bad = self.payload(self.record()['token']); bad['card']['description'] = 'Replacement'
        bad['manifest'] = {'base64': base64.b64encode(b'{}').decode(), 'sha256': hashlib.sha256(b'{}').hexdigest()}
        with self.assertRaises(ValueError): self.call('import', bad)
        self.assertEqual(self.record()['published'], before['published'])
        self.assertEqual(len(self.checked), 2)

    def test_legacy_artwork_and_untracked_manual_edits_are_kept(self):
        self.call('import', self.payload())
        obj = self.objects['steam-12']
        obj.set_value('import_state', '')
        payload = self.payload(self.record()['token'])
        payload['card']['description'] = 'Provider replacement'
        self.assertEqual(self.call('import', payload)['card']['description'], 'Original')
        self.assertEqual(len(self.checked), 2)
        self.assertEqual(self.record()['import_state']['artwork'], {})
        path = self.assets / 'ngs_launcher/manual.png'
        path.write_bytes(b'manual image')
        snapshot = self.Object('snapshot', dict(code='MANUAL', project_code='ngs_launcher', search_type='ngs_launcher/game',
                                              search_code='steam-12', context='cover'))
        snapshot.get_lib_path_by_type = lambda kind: str(path)
        self.snapshots['MANUAL'] = snapshot
        obj.set_value('cover_snapshot', 'MANUAL')
        updated = self.call('import', self.payload(self.record()['token']))
        self.assertEqual(updated['card']['cover']['sha256'], hashlib.sha256(b'manual image').hexdigest())
        self.assertEqual(len(self.checked), 2)


if __name__ == '__main__':
    unittest.main()
