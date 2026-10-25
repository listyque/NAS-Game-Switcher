import base64
import json
import unittest
from unittest.mock import patch

from ngs.metadata import game_metadata, lutris_metadata, wikipedia_metadata, metadata_fetch
from tools.tactic_catalog import contract_source


class CommunityMetadataTests(unittest.TestCase):
    title = 'Need for Speed III: Hot Pursuit'
    slug = 'need-for-speed-iii-hot-pursuit'

    def test_store_miss_falls_back_to_lutris_without_confusing_other_hot_pursuit_games(self):
        correct = dict(app_id='lutris:' + self.slug, name=self.title)
        wrong = dict(app_id='lutris:need-for-speed-hot-pursuit', name='Need for Speed: Hot Pursuit')
        with patch('ngs.metadata.steam_metadata', return_value=[]), patch('ngs.metadata.gog_metadata', return_value=[]), \
             patch('ngs.metadata.epic_metadata', return_value=[]), \
             patch('ngs.metadata.lutris_metadata', return_value=[wrong, correct]) as lutris, \
             patch('ngs.metadata.wikipedia_metadata') as wikipedia:
            self.assertEqual(game_metadata('search', 'Need For Speed III Hot Pursuit'), [correct])
            lutris.assert_called_once()
            wikipedia.assert_not_called()
        ns = {}; exec(contract_source(), ns)
        for provider in ('steam_metadata', 'gog_metadata', 'epic_metadata'):
            ns[provider] = lambda *args: []
        ns['lutris_metadata'] = lambda *args: [wrong, correct]
        self.assertEqual(ns['game_metadata']('search', self.title), [correct])

    def test_unavailable_stores_still_allow_wikipedia_and_direct_links_skip_stores(self):
        row = dict(app_id='wikipedia:en:4877668', name=self.title)
        with patch('ngs.metadata.steam_metadata', side_effect=OSError('offline')), \
             patch('ngs.metadata.gog_metadata', return_value=[]), patch('ngs.metadata.epic_metadata', return_value=[]), \
             patch('ngs.metadata.lutris_metadata', return_value=[]), patch('ngs.metadata.wikipedia_metadata', return_value=[row]):
            self.assertEqual(game_metadata('search', self.title), [row])
        for url, target in (('https://lutris.net/games/' + self.slug + '/', 'lutris_metadata'),
                            ('https://en.wikipedia.org/wiki/Need_for_Speed_III:_Hot_Pursuit', 'wikipedia_metadata')):
            with patch('ngs.metadata.' + target, return_value=[row]) as provider, patch('ngs.metadata.steam_metadata') as steam:
                self.assertEqual(game_metadata('search', url), [row])
                provider.assert_called_once(); steam.assert_not_called()

    def test_lutris_downloads_cover_and_banner_without_using_any_installer(self):
        cover = 'https://lutris.net/media/igdb/cover_big/co2097.jpg'
        banner = 'https://lutris.net/games/banner/' + self.slug + '.jpg'
        product = dict(name=self.title, slug=self.slug, description='<p>Racing description</p>', year=1998,
                       coverart=cover, banner_url=banner, genres=[dict(name='Racing')],
                       installers=[dict(script='never execute this')])
        def fetch(url, limit, **kwargs):
            if '/api/games/' in url: return json.dumps(product).encode()
            return b'banner' if url == banner else b'cover'
        with patch('ngs.metadata.metadata_fetch', side_effect=fetch) as remote:
            result = lutris_metadata('details', 'lutris:' + self.slug)
        self.assertEqual(result['card']['name'], self.title)
        self.assertEqual(result['card']['description'], 'Racing description')
        self.assertFalse(result['card']['available_on_nas'])
        self.assertEqual(base64.b64decode(result['cover']['base64']), b'cover')
        self.assertEqual(base64.b64decode(result['background']['base64']), b'banner')
        self.assertTrue(any('t_cover_big_2x/co2097.jpg' in call.args[0] for call in remote.call_args_list))
        self.assertNotIn('installers', result)
        product['slug'] = 'other-game'
        with patch('ngs.metadata.metadata_fetch', return_value=json.dumps(product).encode()), self.assertRaises(ValueError):
            lutris_metadata('details', 'lutris:' + self.slug)

    def test_wikipedia_filters_articles_and_downloads_image_once_with_source_attribution(self):
        page = dict(id=4877668, title=self.title, description='1998 racing video game')
        noise = [dict(id=1, title='Need for Speed', description='Racing video game franchise'),
                 dict(id=2, title='A person', description='Actor')]
        with patch('ngs.metadata.metadata_fetch', return_value=json.dumps(dict(pages=noise + [page])).encode()):
            self.assertEqual(wikipedia_metadata('search', self.title), [dict(app_id='wikipedia:en:4877668', name=self.title)])
        image = 'https://upload.wikimedia.org/wikipedia/en/a/a6/cover.jpg'
        details = dict(pageid=4877668, ns=0, title=self.title, description=page['description'],
                       extract='Game description', original=dict(source=image))
        def fetch(url, limit, **kwargs):
            return b'image' if url == image else json.dumps(dict(query=dict(pages=[details]))).encode()
        with patch('ngs.metadata.metadata_fetch', side_effect=fetch) as remote:
            result = wikipedia_metadata('details', 'wikipedia:en:4877668')
        self.assertEqual(result['cover'], result['background'])
        self.assertEqual(sum(call.args[0] == image for call in remote.call_args_list), 1)
        self.assertIn('CC BY-SA 4.0', result['card']['description'])
        self.assertIn(result['card']['metadata_url'], result['card']['description'])
        details['description'] = 'Actor'
        with patch('ngs.metadata.metadata_fetch', side_effect=fetch), self.assertRaises(ValueError):
            wikipedia_metadata('details', 'wikipedia:en:4877668')

    def test_new_hosts_do_not_allow_subdomains_http_or_redirects(self):
        for url in ('https://lutris.net.evil.test/api/games', 'http://lutris.net/api/games',
                    'https://upload.wikimedia.org.evil.test/image.jpg', 'https://images.igdb.com:444/image.jpg'):
            with self.subTest(url=url), self.assertRaises(ValueError): metadata_fetch(url, 100)


if __name__ == '__main__':
    unittest.main()
