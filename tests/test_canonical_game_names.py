import json
import unittest
from unittest.mock import patch
from ngs import install
from tests import test_readable_games


class CanonicalGameNamesTests(unittest.TestCase):
    setUp = test_readable_games.ReadableGamesTests.setUp
    legacy = test_readable_games.ReadableGamesTests.legacy

    def test_catalog_title_replaces_repack_name_after_id_change(self):
        cases = ('source', 'metadata', 'title')
        for index, match in enumerate(cases):
            with self.subTest(match=match):
                identity = 'old-'+str(index)
                receipt = self.legacy(name='Black.Mesa-InsaneRamZes', identity=identity)
                library = install.LocalLibrary(self.storage)
                card = dict(self.card, id='steam-362890')
                if match != 'source': card['source_folder'] = 'new-folder'
                if match != 'metadata': card['metadata_url'] = ''
                else:
                    record = library.records/(identity+'.json')
                    receipt['card']['metadata_url']='https://store.steampowered.com/app/362890/Old_Slug/'
                    record.write_bytes(json.dumps(receipt).encode())
                    card['metadata_url']='https://store.steampowered.com/app/362890/'
                self.assertEqual(library.organize([card]), dict(changed=1, errors=[]))
                updated = json.loads((library.records/(identity+'.json')).read_bytes())
                self.assertEqual(updated['card']['name'], 'Black Mesa')
                self.assertEqual(updated['card']['id'], identity)
                self.assertNotIn('InsaneRamZes', updated['location'])
                self.assertTrue(library.launch_spec(identity)[0].is_file())

    def test_ambiguous_source_titles_do_not_guess_or_rename(self):
        receipt = self.legacy(name='Raw release title')
        library = install.LocalLibrary(self.storage)
        cards = [dict(self.card, id='new-'+str(i), name=name, metadata_url='')
                 for i, name in enumerate(('Original Game', 'Different Game'))]
        result = library.organize(cards)
        self.assertEqual(result['changed'], 0)
        self.assertEqual(len(result['errors']), 1)
        self.assertTrue(library.install_path(receipt).is_dir())

    def test_source_from_another_root_cannot_supply_game_title(self):
        self.legacy(name='Existing title')
        library = install.LocalLibrary(self.storage)
        record=library.records/'sample.json';receipt=json.loads(record.read_bytes())
        receipt['card']['source_root']=r'\\NAS\GamesA';record.write_bytes(json.dumps(receipt).encode())
        foreign=dict(self.card,id='foreign',name='Wrong Game',source_root=r'\\NAS\GamesB',metadata_url='')
        self.assertEqual(library.organize([foreign]), dict(changed=1,errors=[]))
        self.assertEqual(json.loads(record.read_bytes())['card']['name'],'Existing title')

    def test_downloaded_installer_uses_catalog_title_without_changing_identity_or_files(self):
        card=dict(self.card,id='setup-old',kind='installer',game_id='steam-362890',name='Black Mesa by xatab')
        manifest=dict(self.manifest,game_id=card['id'])
        with patch.object(install,'game_folder_name',side_effect=lambda c:c['id']):
            original=install.install_game(self.storage,self.source,card,manifest)
        library=install.LocalLibrary(self.storage,installers=True)
        current=dict(card,id='setup-current',name='Black Mesa')
        self.assertEqual(library.organize([current]),dict(changed=1,errors=[]))
        updated=json.loads((library.records/'setup-old.json').read_bytes())
        self.assertEqual(updated['card']['id'],'setup-old')
        self.assertEqual(updated['card']['game_id'],'steam-362890')
        self.assertEqual(updated['card']['name'],'Black Mesa')
        self.assertEqual(library.install_path(updated).name,'Black Mesa')
        self.assertEqual(library.launch_spec('setup-old')[0].read_bytes(),(self.source/'sample/play.exe').read_bytes())
        self.assertFalse(library.install_path(original).exists())


if __name__=='__main__': unittest.main()
