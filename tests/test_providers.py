import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ngs.metadata import (client_metadata, epic_metadata, game_metadata, gog_metadata,
                         metadata_fetch, metadata_artwork, metadata_reference, search_queries, search_title)
from ngs.api import validate_payload
from ngs.portable import PortableStorage
from tools.metadata_api import protocol_source


class ProviderTests(unittest.TestCase):
    def test_gog_package_name_retries_without_generic_prefix_and_keeps_real_game_titles(self):
        from tools.tactic_catalog import contract_source
        game = dict(app_id='1880610', name='Deliver At All Costs')
        for title in ('game.deliver.at.all.costs-gog', 'game deliver at all costs-gog'):
            self.assertEqual(search_queries(title), ['game deliver at all costs', 'deliver at all costs'])
            ns = {}; exec(contract_source(), ns)
            ns['steam_metadata'] = unittest.mock.Mock(side_effect=[[], [game]])
            ns['gog_metadata'] = unittest.mock.Mock(side_effect=AssertionError('Unnecessary fallback'))
            self.assertEqual(ns['game_metadata']('search', title), [game])
            self.assertEqual([call.args[1] for call in ns['steam_metadata'].call_args_list], search_queries(title))
        for title in ('Game.Dev.Tycoon-GOG', 'Game.of.Thrones-GOG', 'Game-GOG'):
            full = search_title(title)
            with patch('ngs.metadata.steam_metadata', return_value=[dict(app_id='12', name=full)]) as steam:
                self.assertEqual(game_metadata('search', title)[0]['name'], full)
                steam.assert_called_once_with('search', full)
        self.assertEqual(search_title('Deliver.At.All.Costs-GOG'), 'Deliver At All Costs')
        self.assertEqual(search_title('Goggles - World of Vaporia'), 'Goggles - World of Vaporia')

    def test_installer_repack_labels_are_removed_and_collection_falls_back_to_series(self):
        from tools.tactic_catalog import contract_source
        cases = {'DOOM Eternal by xatab': ['DOOM Eternal'],
                 '[dixen18] Wolfenstein. Collection': ['Wolfenstein Collection', 'Wolfenstein'],
                 '[dixen18] Ori. Dilogy': ['Ori Dilogy', 'Ori']}
        for title, queries in cases.items():
            self.assertEqual(search_queries(title), queries)
            expected = dict(app_id='12', name=queries[-1])
            calls = []
            def search(operation, query):
                calls.append(query)
                return [expected] if query == queries[-1] else []
            ns = {}; exec(contract_source(), ns)
            ns['steam_metadata'] = search
            ns['gog_metadata'] = unittest.mock.Mock(side_effect=AssertionError('Unnecessary provider fallback'))
            self.assertEqual(ns['game_metadata']('search', title), [expected])
            self.assertEqual(calls, queries)
        self.assertEqual(search_title('[PROTOTYPE]'), '[PROTOTYPE]')
        official = dict(app_id='13', name='Halo: The Master Chief Collection')
        with patch('ngs.metadata.steam_metadata', return_value=[official]) as steam:
            self.assertEqual(game_metadata('search', official['name']), [official])
            steam.assert_called_once()

    def test_short_query_keeps_real_title_prefixes_without_fallback_or_soundtracks(self):
        from tools.tactic_catalog import contract_source
        rows=[dict(app_id='261570',name='Ori and the Blind Forest'),
              dict(app_id='1057090',name='Ori and the Will of the Wisps'),
              dict(app_id='12',name='Ori and the Will of the Wisps Soundtrack')]
        for server in (False,True):
            if server:
                ns={};exec(contract_source(),ns)
                ns['steam_metadata']=lambda *args:rows
                ns['gog_metadata']=unittest.mock.Mock(side_effect=AssertionError('Unnecessary fallback'))
                result=ns['game_metadata']('search','Ori')
                ns['gog_metadata'].assert_not_called()
            else:
                with patch('ngs.metadata.steam_metadata',return_value=rows) as steam,patch('ngs.metadata.gog_metadata') as gog:
                    result=game_metadata('search','Ori')
                    steam.assert_called_once();gog.assert_not_called()
            self.assertEqual({r['app_id'] for r in result},{'261570','1057090'})

    def test_remake_hint_retries_store_title_and_ranks_current_re4_above_2005_and_soundtrack(self):
        from tools.tactic_catalog import contract_source
        modern = dict(app_id='12', name='Resident Evil 4')
        old = dict(app_id='13', name='Resident Evil 4 (2005)')
        soundtrack = dict(app_id='14', name='Resident Evil 4 Soundtrack')
        for title in ('resident evil 4 remake', 'Resident Evil 4 Remake', 'Resident Evil 4 ремейк'):
            def fetch(operation, query):
                return [old, soundtrack, modern] if query.casefold() == 'resident evil 4' else []
            with patch('ngs.metadata.steam_metadata', side_effect=fetch) as steam:
                rows = game_metadata('search', title)
                self.assertEqual(rows[0], modern)
                self.assertNotIn(soundtrack, rows)
                self.assertEqual(len(steam.call_args_list), 2)
                self.assertEqual(steam.call_args_list[-1].args[1].casefold(), 'resident evil 4')
        ns = {}; exec(contract_source(), ns)
        ns['steam_metadata'] = fetch
        self.assertEqual(ns['game_metadata']('search', 'Resident Evil 4 remake')[0], modern)

    def test_official_remake_and_remastered_titles_are_searched_in_full_first(self):
        for title in ('FINAL FANTASY VII REMAKE INTERGRADE', 'Resident Evil 4 Ultimate Edition',
                      'The Last of Us Part II Remastered'):
            row = dict(app_id='12', name=title)
            with patch('ngs.metadata.steam_metadata', return_value=[row]) as steam:
                self.assertEqual(game_metadata('search', title)[0], row)
                steam.assert_called_once_with('search', title)

    def test_title_separators_normalize_before_search_without_losing_editions_or_part_numbers(self):
        cases = {
            'mafia - definitive edition': 'mafia definitive edition',
            'Mafia-Definitive-Edition': 'Mafia Definitive Edition',
            'Mafia — Definitive Edition': 'Mafia Definitive Edition',
            'Mafia™: “Definitive Edition”': 'Mafia Definitive Edition',
            'Mafia_-_Definitive_Edition': 'Mafia Definitive Edition',
            'Mafia II – Definitive Edition': 'Mafia II Definitive Edition',
            'DiRT.Rally.2.0—Game.of.the.Year.Edition': 'DiRT Rally 2.0 Game of the Year Edition',
            'Assassin’s.Creed:Valhalla': 'Assassins Creed Valhalla',
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(search_queries(title)[0], expected)
        self.assertEqual(search_title('Mafia - Definitive Edition'), 'Mafia - Definitive Edition')

    def test_punctuation_variants_find_same_game_in_one_query_and_keep_sequel_and_soundtrack_filters(self):
        from tools.tactic_catalog import contract_source
        first = dict(app_id='12', name='Mafia: Definitive Edition')
        second = dict(app_id='13', name='Mafia II: Definitive Edition')
        noise = [dict(app_id='14', name='Mafia II: Definitive Edition Soundtrack'),
                 dict(app_id='15', name='Mafia II: Definitive Edition DLC')]
        for title in ('mafia - definitive edition', 'Mafia: Definitive Edition', 'Mafia—Definitive Edition'):
            def fetch(operation, query):
                return [first, second] if query.casefold() == 'mafia definitive edition' else []
            with patch('ngs.metadata.steam_metadata', side_effect=fetch) as steam, \
                 patch('ngs.metadata.gog_metadata') as gog:
                self.assertEqual(game_metadata('search', title)[0], first)
                steam.assert_called_once()
                gog.assert_not_called()
        ns = {}; exec(contract_source(), ns)
        ns['steam_metadata'] = lambda *args: [first, second] + noise
        self.assertEqual(ns['game_metadata']('search', 'Mafia II - Definitive Edition'), [second])

    def test_search_retries_original_punctuation_before_removing_edition_or_subtitle(self):
        row = dict(app_id='12', name='Half-Life')
        with patch('ngs.metadata.steam_metadata', side_effect=[[], [row]]) as steam:
            self.assertEqual(game_metadata('search', 'Half-Life'), [row])
            self.assertEqual([call.args[1] for call in steam.call_args_list], ['Half Life', 'Half-Life'])

    def test_artwork_downloads_overlap_and_equal_sources_are_downloaded_once(self):
        from tools.tactic_catalog import contract_source
        sources = dict(cover=['https://cdn2.unrealengine.com/cover.jpg'],
                       background=['https://cdn2.unrealengine.com/background.jpg'])
        for server_contract in (False, True):
            barrier = threading.Barrier(2)
            def fetch(url, limit, **kwargs):
                barrier.wait(timeout=2)
                return url.encode()
            if server_contract:
                ns = {}; exec(contract_source(), ns)
                ns['metadata_fetch'] = fetch
                result = ns['metadata_artwork'](sources)
            else:
                with patch('ngs.metadata.metadata_fetch', side_effect=fetch):
                    result = metadata_artwork(sources)
            for context, urls in sources.items():
                self.assertEqual(base64.b64decode(result[context]['base64']), urls[0].encode())
                self.assertEqual(result[context]['sha256'], hashlib.sha256(urls[0].encode()).hexdigest())
        with patch('ngs.metadata.metadata_fetch', return_value=b'shared') as fetch:
            result = metadata_artwork(dict(cover=sources['cover'], background=sources['cover']))
            self.assertEqual(result['cover'], result['background'])
            fetch.assert_called_once()

    def test_artwork_fallback_has_shorter_timeout_and_never_swallows_validation_errors(self):
        with patch('ngs.metadata.metadata_fetch', side_effect=[OSError('slow preferred CDN'), b'fallback']) as fetch:
            result = metadata_artwork(dict(cover=['preferred', 'fallback']))
            self.assertEqual(base64.b64decode(result['cover']['base64']), b'fallback')
            self.assertEqual([call.kwargs['timeout'] for call in fetch.call_args_list], [4, 12])
        with patch('ngs.metadata.metadata_fetch', side_effect=ValueError('Untrusted')) as fetch:
            with self.assertRaisesRegex(ValueError, 'Untrusted'):
                metadata_artwork(dict(cover=['bad', 'fallback']))
            fetch.assert_called_once()

    def test_short_bo_title_matches_accented_full_title_without_prefix_noise_or_soundtracks(self):
        base = dict(app_id='1614440', name='Bō: Path of the Teal Lotus')
        noise = [dict(app_id=str(i), name=name) for i, name in enumerate((
            'Bodycam', 'Box', 'Botlings', 'Bō: Path of the Teal Lotus Soundtrack',
            'Bō: Path of the Teal Lotus Demo'), 1)]
        for query in ('Bo', 'Bō', 'bo', 'Bo.GOG', 'Bo.Path.of.the.Teal.Lotus'):
            with self.subTest(query=query), patch('ngs.metadata.steam_metadata', return_value=noise + [base]), \
                    patch('ngs.metadata.gog_metadata') as gog:
                self.assertEqual(game_metadata('search', query), [base])
                gog.assert_not_called()

    def test_bo_gog_fallback_and_decomposed_accents_use_same_server_contract(self):
        from tools.tactic_catalog import contract_source
        base = dict(app_id='gog:1882554410', name='Bo\u0304: Path of the Teal Lotus')
        noise = dict(app_id='12', name='Bodycam')
        for query in ('Bo', 'Bō', 'Bo Path of the Teal Lotus'):
            with self.subTest(query=query), patch('ngs.metadata.steam_metadata', return_value=[noise]), \
                    patch('ngs.metadata.gog_metadata', return_value=[base]), patch('ngs.metadata.epic_metadata') as epic:
                self.assertEqual(game_metadata('search', query), [base])
                epic.assert_not_called()
        namespace = {}
        exec(contract_source(), namespace)
        namespace['steam_metadata'] = lambda *args: [noise]
        namespace['gog_metadata'] = lambda *args: [base]
        self.assertEqual(namespace['game_metadata']('search', 'Bo'), [base])

    def test_gog_accented_title_uses_catalog_portrait_instead_of_small_logo(self):
        from urllib.parse import parse_qs, urlsplit
        product = dict(id=1882554410, title='Bō: Path of the Teal Lotus', game_type='game',
                       slug='bo_path_of_the_teal_lotus', description=dict(full='Description'),
                       images=dict(logo='https://images.gog-statics.com/small.png'))
        art = dict(id=1882554410, title=product['title'], productType='game',
                   coverVertical='https://images.gog-statics.com/portrait.jpg',
                   galaxyBackgroundImage='https://images.gog-statics.com/background.jpg')
        def fetch(url, limit, **kwargs):
            if 'catalog.gog.com' in url:
                self.assertEqual(parse_qs(urlsplit(url).query)['query'], ['bo path of the teal lotus'])
                return json.dumps(dict(products=[art])).encode()
            if 'api.gog.com' in url: return json.dumps(product).encode()
            self.assertNotIn('small.png', url)
            return url.encode()
        with patch('ngs.metadata.metadata_fetch', side_effect=fetch):
            result = gog_metadata('details', 'gog:1882554410')
            self.assertEqual(base64.b64decode(result['cover']['base64']), art['coverVertical'].encode())

    def test_direct_steam_id_and_official_link_find_delisted_game_without_store_search_or_artwork(self):
        document={'958400':{'success':True,'data':{'type':'game','name':'Project CARS 3'}}}
        for value in ('958400','https://store.steampowered.com/app/958400/Project_CARS_3/'):
            with patch('ngs.metadata.metadata_fetch',return_value=json.dumps(document).encode()) as fetch:
                self.assertEqual(game_metadata('search',value),[dict(app_id='958400',name='Project CARS 3')])
                self.assertEqual(fetch.call_count,1)
                self.assertIn('/api/appdetails?',fetch.call_args.args[0])
                self.assertIn('appids=958400',fetch.call_args.args[0])
        for data in ({'success':False},{'success':True,'data':{'type':'dlc','name':'DLC'}}):
            with patch('ngs.metadata.metadata_fetch',return_value=json.dumps({'958400':data}).encode()):
                self.assertEqual(game_metadata('search','958400'),[])

    def test_clean_release_suffixes_preserve_parts_and_meaningful_editions(self):
        cases = {
            'Baldurs.Gate.3.GOG.Rip-InsaneRamZes': 'Baldurs Gate 3',
            'The.Last.of.Us.Part.II.Remastered-InsaneRamZes': 'The Last of Us Part II Remastered',
            'PennysBigBreakaway': 'Pennys Big Breakaway', 'PortalRTX-InsaneRamZes': 'Portal RTX',
            'Syberia_The_World_Before_1.39222_(54371)_win_gog': 'Syberia The World Before',
            'Until Dawn (2015-2024)': 'Until Dawn',
        }
        for folder, title in cases.items(): self.assertEqual(search_title(folder), title)
        self.assertEqual(search_queries('DiRT Rally 2.0 Game of the Year Edition'),
                         ['DiRT Rally 2.0 Game of the Year Edition', 'DiRT Rally 2.0'])
        self.assertEqual(search_queries('Alan.Wake.2.Deluxe.Edition-InsaneRamZes'), ['Alan Wake 2 Deluxe Edition', 'Alan Wake 2'])
        with patch('ngs.metadata.steam_metadata', side_effect=[[], [{'app_id':'12','name':'ELDEN RING'}]]) as steam, \
                patch('ngs.metadata.epic_metadata') as epic, patch('ngs.metadata.gog_metadata') as gog:
            self.assertEqual(game_metadata('search','ELDEN.RING.Deluxe.Edition.Steam.Rip-InsaneRamZes')[0]['name'],'ELDEN RING')
            self.assertEqual([c.args[1] for c in steam.call_args_list], ['ELDEN RING Deluxe Edition','ELDEN RING'])
            epic.assert_not_called(); gog.assert_not_called()

    def test_epic_fallback_does_not_confuse_original_game_with_sequel(self):
        row = {'app_id':'epic:alan-wake-2','name':'Alan Wake 2'}
        with patch('ngs.metadata.steam_metadata', return_value=[{'app_id':'12','name':'Alan Wake'},
                {'app_id':'3274290','name':'Beat Saber - Monstercat Mixtape 2 - Alan Walker - Wake Up'}]), \
                patch('ngs.metadata.gog_metadata', return_value=[]), patch('ngs.metadata.epic_metadata', return_value=[row]) as epic:
            self.assertEqual(game_metadata('search','Alan Wake 2'),[row])
            epic.assert_called_once_with('search','epic:alan-wake-2')
        with patch('ngs.metadata.epic_metadata', return_value=[row]), patch('ngs.metadata.steam_metadata') as steam:
            self.assertEqual(game_metadata('search','https://store.epicgames.com/en-US/p/alan-wake-2'),[row])
            steam.assert_not_called()

    def test_tmnt_expands_abbreviation_and_filters_soundtracks_and_deluxe_upgrades(self):
        base = dict(app_id='1361510', name="Teenage Mutant Ninja Turtles: Shredder's Revenge")
        soundtrack = dict(app_id='3216650', name="TMNT: Shredder's Revenge - Radical Remixes (Original Game Soundtrack)")
        with patch('ngs.metadata.steam_metadata', return_value=[soundtrack, base]) as steam:
            self.assertEqual(game_metadata('search', 'TMNT Shredders Revenge'), [base])
            self.assertEqual(steam.call_args.args[1], 'Teenage Mutant Ninja Turtles Shredders Revenge')
        first = dict(app_id='1888930', name='The Last of Us™ Part I')
        upgrade = dict(app_id='2254450', name=first['name']+' - Upgrade to Digital Deluxe Edition')
        sequel = dict(app_id='2531310', name='The Last of Us™ Part II Remastered')
        with patch('ngs.metadata.steam_metadata', return_value=[upgrade, sequel, first]):
            self.assertEqual(game_metadata('search','The Last of Us Part I'),[first])

    def epic_product(self):
        return dict(_slug='alan-wake-2', productName='Alan Wake 2', pages=[dict(_slug='home', data=dict(
            about=dict(description='<b>Russian description</b>',shortDescription='Short'),
            hero=dict(portraitBackgroundImageUrl='https://cdn2.unrealengine.com/cover.jpg',
                      backgroundImageUrl='https://cdn2.unrealengine.com/background.jpg')))])

    def test_epic_description_artwork_and_trusted_hosts(self):
        product=self.epic_product()
        def fetch(url, *args, **kwargs):
            return b'cover' if url.endswith('/cover.jpg') else b'background' if url.endswith('/background.jpg') else json.dumps(product).encode()
        with patch('ngs.metadata.metadata_fetch', side_effect=fetch):
            result=epic_metadata('details','epic:alan-wake-2')
        self.assertEqual(result['card']['id'],'epic-alan-wake-2')
        self.assertEqual(result['card']['description'],'Russian description')
        self.assertFalse(result['card']['available_on_nas'])
        self.assertEqual(result['cover']['sha256'],hashlib.sha256(b'cover').hexdigest())
        product['pages'][0]['data']['hero']['portraitBackgroundImageUrl']='http://127.0.0.1/private'
        opener=SimpleNamespace(open=unittest.mock.Mock(return_value=io.BytesIO(json.dumps(product).encode())))
        with patch('urllib.request.build_opener',return_value=opener), self.assertRaisesRegex(ValueError,'Untrusted'):
            epic_metadata('details','epic:alan-wake-2')
        self.assertTrue(all('127.0.0.1' not in call.args[0].full_url for call in opener.open.call_args_list))
        for url in ('https://cdn2.unrealengine.com.evil.test/x','http://cdn2.unrealengine.com/x',
                    'https://user:pass@cdn2.unrealengine.com/x','https://images-1.gog-statics.com:444/x'):
            with self.assertRaises(ValueError): metadata_fetch(url,100)
        with patch('ngs.metadata.metadata_fetch',return_value=b'{}'):
            self.assertEqual(epic_metadata('search','epic:missing-game'),[])
            with self.assertRaises(ValueError): epic_metadata('details','epic:missing-game')

    def test_gog_search_and_details_keep_product_identity_and_do_not_use_galaxy(self):
        item=dict(id='1403485498',title='Mortal Kombat Trilogy',productType='pack',
                  coverVertical='https://images.gog-statics.com/cover.jpg', genres=[dict(name='Action')])
        product=dict(id=1403485498,title=item['title'],slug='mortal_kombat_trilogy',game_type='pack',
                     description=dict(lead='Short',full='<p>Description</p>'),images=dict(background='//images-1.gog-statics.com/background.jpg'))
        with patch('ngs.metadata.metadata_fetch',return_value=json.dumps(dict(products=[item])).encode()) as fetch:
            self.assertEqual(gog_metadata('search','Mortal Kombat Trilogy'),[dict(app_id='gog:1403485498',name=item['title'])])
            self.assertIn('countryCode=US',fetch.call_args.args[0])
        def fetch(url, *args, **kwargs):
            return (b'cover' if url.endswith('/cover.jpg') else b'background' if url.endswith('/background.jpg')
                    else json.dumps(product if 'api.gog.com' in url else dict(products=[item])).encode())
        with patch('ngs.metadata.metadata_fetch',side_effect=fetch):
            result=gog_metadata('details','gog:1403485498')
        self.assertEqual(result['card']['id'],'gog-1403485498')
        self.assertEqual(result['card']['description'],'Description')
        self.assertEqual(result['card']['genre'],'Action')
        self.assertEqual(base64.b64decode(result['background']['base64']),b'background')
        with patch('ngs.metadata.steam_metadata',return_value=[]), patch('ngs.metadata.gog_metadata',return_value=[
                dict(app_id='gog:12',name='Unrelated Game'),dict(app_id='gog:1403485498',name=item['title'])]), \
                patch('ngs.metadata.epic_metadata') as epic:
            self.assertEqual(game_metadata('search',item['title'])[0]['app_id'],'gog:1403485498')
            epic.assert_not_called()

    def test_protocol_accepts_only_bounded_provider_ids(self):
        ns={};exec(protocol_source(),ns)
        for value in ('12','epic:alan-wake-2','gog:1403485498','lutris:need-for-speed-iii-hot-pursuit','wikipedia:en:4877668'):
            self.assertEqual(validate_payload(dict(operation='resolve',value=value)),('resolve',value))
            self.assertEqual(ns['validate_payload'](dict(operation='resolve',value=value)),('resolve',value))
        for value in ('epic:../private','epic:https://localhost/','gog:../1','gog:0','gog:1?x','gog:'+ '1'*21,'epic:'+ 'a'*80,
                      'lutris:../private','lutris:https://localhost/','lutris:'+'a'*73,'lutris:bad?url',
                      'wikipedia:de:123','wikipedia:en:0','wikipedia:en:../1','wikipedia:en:1?url','wikipedia:en:'+'1'*13):
            with self.assertRaises(ValueError): metadata_reference(value)
            with self.assertRaises(ValueError): ns['validate_payload'](dict(operation='resolve',value=value))

    def test_gog_bundle_is_found_without_accepting_an_unrelated_game(self):
        bundle=dict(app_id='gog:1423059062',name='Mortal Kombat 1+2+3')
        with patch('ngs.metadata.steam_metadata',return_value=[]), \
                patch('ngs.metadata.gog_metadata',side_effect=[[],[bundle]]) as gog, patch('ngs.metadata.epic_metadata') as epic:
            self.assertEqual(game_metadata('search','Mortal Kombat 3'),[bundle])
            self.assertEqual([c.args[1] for c in gog.call_args_list],['Mortal Kombat 3','mortal kombat'])
            epic.assert_not_called()

    def test_handheld_does_not_fetch_from_any_provider(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            with patch('ngs.api.MetadataApi') as remote, patch('ngs.metadata.epic_metadata') as epic, patch('ngs.metadata.gog_metadata') as gog:
                rows=[dict(app_id='epic:alan-wake-2',name='Alan Wake 2'),dict(app_id='gog:1403485498',name='Mortal Kombat Trilogy')]
                remote.return_value.call.return_value=rows
                self.assertEqual(client_metadata(PortableStorage(root),'http://nas/tactic','search','Game',threading.Event()),rows)
                epic.assert_not_called();gog.assert_not_called()

    def test_nas_address_failure_is_retried_once_without_changing_network_settings(self):
        import urllib.error
        opener=SimpleNamespace(open=unittest.mock.Mock(side_effect=[
            urllib.error.URLError(OSError(99,'Cannot assign requested address')),io.BytesIO(b'{}')]))
        with patch('urllib.request.build_opener',return_value=opener):
            self.assertEqual(metadata_fetch('https://catalog.gog.com/v1/catalog',100),b'{}')
        self.assertEqual(opener.open.call_count,2)


if __name__=='__main__':unittest.main()
