import json
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ngs.install import Cancelled, LocalLibrary
from ngs.portable import PortableStorage
from ngs.store_games import installed_store_games, import_store_games, manifest, registry_rows, store_game_card


class StoreGamesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.registry = {}
        image=dict(path='/assets/ngs_launcher/art.png',sha256=hashlib.sha256(b'fixture').hexdigest(),size=7)
        self.card=dict(id='steam-12',name='Known title',subtitle='Subtitle',description='Game description',genre='Action',accent='#d9ff82',source_folder='',executable='',metadata_url='https://store.steampowered.com/app/12/',revision=1,cover=image,background=image,available_on_nas=False)
        self.addCleanup(patch.stopall)
        patch('ngs.store_games.registry_rows', side_effect=lambda key: self.registry.get(key, [])).start()
        patch.dict(os.environ, {'ProgramData':str(self.root/'Data'), 'ProgramFiles':str(self.root/'Programs'),
            'ProgramFiles(x86)':str(self.root/'Programs32')}).start()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value,str) else json.dumps(value), encoding='utf8')

    def game(self, name):
        folder=self.root/name;folder.mkdir(parents=True)
        shutil.copyfile(sys.executable,folder/'play.exe')
        return folder

    def test_discovers_four_stores_extra_steam_library_and_epic_fallback_without_scanning_games(self):
        steam=self.root/'Steam'; library=self.root/'Other library'
        self.registry[r'Software\Valve\Steam']=[dict(steampath=str(steam))]
        self.write(steam/'steamapps/libraryfolders.vdf','"LibraryFolders" { "TimeNextStatsReport" "99999" "0" { "path" '+json.dumps(str(library))+' } }')
        game=library/'steamapps/common/Example';game.mkdir(parents=True)
        self.write(library/'steamapps/appmanifest_123.acf','"AppState" { "appid" "123" "name" "Steam Example" "installdir" "Example" "StateFlags" "4" "empty" "" }')
        gog=self.game('GOG Example')
        self.registry[r'Software\GOG.com\Games']=[dict(key='456',path=str(gog),gamename='GOG title')]
        self.write(gog/'goggame-456.info',dict(name='GOG Example',playTasks=[dict(isPrimary=True,type='FileTask',path='play.exe')]))
        epic=self.game('Epic Example');extra=self.game('Epic fallback')
        self.write(self.root/'Data/Epic/EpicGamesLauncher/Data/Manifests/a.item',dict(AppName='epic-id',DisplayName='Epic title',InstallLocation=str(epic),LaunchExecutable='play.exe'))
        self.write(self.root/'Data/Epic/UnrealEngineLauncher/LauncherInstalled.dat',dict(InstallationList=[
            dict(AppName='epic-id',InstallLocation=str(epic)),dict(AppName='extra',InstallLocation=str(extra))]))
        battle=self.game('Battle Example')
        self.registry[r'Software\Microsoft\Windows\CurrentVersion\Uninstall']=[dict(key='battle',displayname='Battle Example',installlocation=str(battle),
            uninstallstring='"C:\\Battle.net\\Battle.net.exe" --uid=demo --uninstall',displayicon=str(battle/'play.exe')+',0')]
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('ngs.store_games.scan_executables',side_effect=AssertionError('Discovery walked a game')):
            result=installed_store_games()
        self.assertEqual(len(result['rows']),5)
        self.assertEqual({r['store'] for r in result['rows']},{'Steam','GOG','Epic Games Store','Battle.net'})
        self.assertEqual(next(r for r in result['rows'] if r['store']=='GOG')['preferred_exe'],'play.exe')
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_missing_stale_corrupt_unsafe_incomplete_and_client_entries_are_skipped(self):
        good=self.game('Good')
        manifests=self.root/'Data/Epic/EpicGamesLauncher/Data/Manifests'
        self.write(manifests/'good.item',dict(DisplayName='Good',InstallLocation=str(good),LaunchExecutable='play.exe'))
        self.write(manifests/'bad.item','{')
        self.write(manifests/'array.item',[])
        self.write(manifests/'missing.item',dict(InstallLocation=str(self.root/'Missing')))
        self.write(manifests/'unsafe.item',dict(InstallLocation=str(good),LaunchExecutable='../outside.exe'))
        self.write(manifests/'pending.item',dict(InstallLocation=str(self.game('Pending')),bIsIncompleteInstall=True))
        self.registry[r'Software\GOG.com\Games']=[dict(key='invalid',path=r'\\server\share')]
        self.registry[r'Software\Microsoft\Windows\CurrentVersion\Uninstall']=[dict(displayname='Battle.net',publisher='Blizzard',installlocation=str(good))]
        result=installed_store_games()
        self.assertEqual([r['name'] for r in result['rows']],['Good'])
        self.assertTrue(result['warnings'])
        stopped=threading.Event();stopped.set()
        with self.assertRaises(Cancelled):installed_store_games(stopped)

    def test_bulk_import_keeps_files_deduplicates_and_leaves_ambiguous_exes_for_review(self):
        storage=PortableStorage(self.root/'NGS');known=self.game('Known');ambiguous=self.game('Ambiguous')
        shutil.copyfile(sys.executable,ambiguous/'second.exe')
        rows=[dict(name='Known title',store='Epic Games Store',path=str(known),preferred_exe='play.exe',candidates=[],state='found'),
              dict(name='Ambiguous',store='Steam',path=str(ambiguous),preferred_exe='',candidates=[],state='found')]
        before={p:p.read_bytes() for folder in (known,ambiguous) for p in folder.iterdir()}
        events=[]
        result=import_store_games(storage,rows,[self.card],threading.Event(),events.append)
        self.assertEqual([r['state'] for r in result['rows']],['added','manual'])
        library=LocalLibrary(storage)
        self.assertEqual(len(library.reconcile()),1)
        repeated=import_store_games(storage,rows[:1],[self.card],threading.Event(),events.append)
        self.assertEqual(repeated['rows'][0]['state'],'already')
        library.forget(result['rows'][0]['game_id'])
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_vdf_empty_values_escaping_and_size_bounds(self):
        path=self.root/'a.vdf'
        self.write(path,'"root" { "empty" "" "path" "D:\\\\Games" // comment\n "next" "ok" }')
        self.assertEqual(manifest(path,vdf=True),{'root':{'empty':'','path':r'D:\Games','next':'ok'}})
        self.write(path,'"root" { "bad" }')
        with self.assertRaises(ValueError):manifest(path,vdf=True)
        self.write(path,'x'*(2*1024*1024+1))
        with self.assertRaises(ValueError):manifest(path)

    def test_store_ids_resolve_directly_and_other_stores_require_unique_exact_title(self):
        from unittest.mock import Mock
        for store, identity, reference in (('Steam','12','12'),('GOG','42','gog:42')):
            request=Mock(return_value=dict(card=self.card))
            card, message=store_game_card(dict(store=store,store_id=identity,name='Different localized title'),[],request)
            self.assertEqual(card['description'],'Game description')
            request.assert_called_once_with('resolve',reference,'Different localized title')
        for store in ('Epic Games Store','Battle.net'):
            request=Mock(side_effect=[[dict(app_id='12',name='Known title™')],dict(card=self.card)])
            card, message=store_game_card(dict(store=store,store_id='not-a-public-id',name='Known title'),[],request)
            self.assertEqual(card,self.card)
            self.assertEqual(request.call_args_list[0].args[:2],('search','Known title'))
        request=Mock(return_value=[dict(app_id='12',name='Known title'),dict(app_id='13',name='Known title')])
        self.assertIsNone(store_game_card(dict(store='Battle.net',name='Known title'),[],request)[0])
        request=Mock(side_effect=RuntimeError('Invalid signature'))
        with self.assertRaisesRegex(RuntimeError,'signature'):
            store_game_card(dict(store='Steam',store_id='12',name='Known title'),[],request)
        request=Mock(return_value=dict(card=self.card))
        store_game_card(dict(store='Steam',store_id='12',name='Known title'),
                        [dict(self.card,id='steam-99',metadata_url='https://store.steampowered.com/app/99/')],request)
        request.assert_called_once_with('resolve','12','Known title')
        request=Mock(side_effect=Cancelled())
        with self.assertRaises(Cancelled):
            store_game_card(dict(store='GOG',store_id='42',name='Known title'),[],request)

    def test_offline_import_then_artwork_retry_keeps_identity_arguments_and_custom_name(self):
        from ngs.catalog import CatalogCache
        storage=PortableStorage(self.root/'NGS');folder=self.game('Folder title')
        rows=[dict(name='My custom title',store='Steam',store_id='12',path=str(folder),preferred_exe='play.exe',candidates=[],state='found')]
        before={p:p.read_bytes() for p in folder.iterdir()}
        with patch('ngs.api.MetadataApi',side_effect=AssertionError('Offline used the network')):
            first=import_store_games(storage,rows,[],threading.Event(),lambda event:None,url='http://nas/tactic',offline=True)
        self.assertEqual(first['rows'][0]['state'],'artwork')
        identity=first['rows'][0]['game_id'];library=LocalLibrary(storage)
        library.set_launch_arguments(identity,'-windowed -test')
        cache=CatalogCache(storage,'http://nas/tactic');cache.image_path(self.card['cover']['sha256']).write_bytes(b'fixture')
        with patch('ngs.bulk_scan.client_metadata',return_value=dict(card=self.card)) as request:
            finished=import_store_games(storage,rows,[],threading.Event(),lambda event:None,url='http://nas/tactic')
            self.assertEqual(request.call_args.args[2:4],('resolve','12'))
        self.assertEqual(finished['rows'][0]['state'],'added')
        self.assertEqual(finished['rows'][0]['game_id'],identity)
        receipts=library.reconcile();self.assertEqual(len(receipts),1)
        self.assertEqual(receipts[0]['card']['name'],'My custom title')
        self.assertEqual(receipts[0]['card']['description'],'Game description')
        self.assertEqual(library.launch_arguments(identity),'-windowed -test')
        with patch('ngs.bulk_scan.client_metadata',side_effect=AssertionError('Overwriting existing artwork')):
            self.assertEqual(import_store_games(storage,rows,[],threading.Event(),lambda event:None,url='http://nas/tactic')['rows'][0]['state'],'already')
        self.assertEqual({p:p.read_bytes() for p in before},before)
