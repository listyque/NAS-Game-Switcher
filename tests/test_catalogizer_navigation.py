import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from ngs.catalogizer_ui import RemoteCatalogBridge
from ngs.portable import PortableStorage


class CatalogizerNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.storage = PortableStorage(Path(temporary.name).resolve())
        self.published = Mock()
        self.bridge = RemoteCatalogBridge(self.storage, self.published, None)
        self.addCleanup(self.bridge.stop)
        self.bridge.request = Mock()
        self.bridge.openServer('http://127.0.0.1:8888/tactic')
        self.bridge.request.reset_mock()

    def results(self):
        self.bridge.loaded(dict(stage='done', state='done', preview='ab'*16,
            rows=[dict(name='One', folder='One', state='manual'), dict(name='Two', folder='Two', state='manual')],
            result_offset=20, result_total=45, summary=dict(added=3, already=0, manual=42, error=0)))

    def publish_one(self, folder='One'):
        self.bridge.reviewFolder(folder)
        self.bridge.loaded(dict(stage='exes', folder=folder, rows=[dict(path='play.exe')]))
        self.bridge._state.update(executable='play.exe')
        self.bridge.loaded(dict(stage='review', preview='cd'*16, card=dict(id='game', name='Game')))
        self.bridge.publish()
        self.bridge.loaded(dict(stage='working', state='export', preview='cd'*16))
        self.bridge.loaded(dict(stage='done', state='done', preview='cd'*16, rows=[], card=dict(id='game')))

    def test_publication_returns_to_same_batch_page_and_reuses_exes_without_new_scan(self):
        self.results()
        self.publish_one()
        self.bridge.request.reset_mock()
        self.bridge.backToFolders()
        state = self.bridge.state
        self.assertEqual((state['stage'], state['preview'], state['result_offset'], state['result_total']), ('done', 'ab'*16, 20, 45))
        self.assertEqual([row['folder'] for row in state['rows']], ['One', 'Two'])
        self.assertTrue(state['rows'][0]['published'])
        self.assertNotIn('published', state['rows'][1])
        self.bridge.request.assert_not_called()
        self.bridge.results(40)
        self.bridge.request.assert_called_once_with(dict(action='results', preview='ab'*16, offset=40, hide_published=True))
        self.bridge.request.reset_mock()
        self.bridge.reviewFolder('One')
        self.assertEqual(self.bridge.state['stage'], 'exes')
        self.assertEqual(self.bridge.state['rows'][0]['path'], 'play.exe')
        self.bridge.request.assert_not_called()

    def test_back_before_publication_keeps_rows_filter_and_parent_token(self):
        self.results()
        before = self.bridge.state
        self.bridge.reviewFolder('Two')
        self.bridge.loaded(dict(stage='exes', folder='Two', rows=[dict(path='play.exe')]))
        self.bridge.loaded(dict(stage='matches', rows=[dict(name='Game', app_id='12')]))
        self.bridge._state['preview'] = 'ef'*16
        self.bridge.request.reset_mock()
        self.bridge.backToFolders()
        self.assertEqual(self.bridge.state['rows'], before['rows'])
        self.assertEqual(self.bridge.state['preview'], before['preview'])
        self.assertEqual(self.bridge.state['selected_index'], 1)
        self.assertEqual(self.bridge.state['hide_published'], before['hide_published'])
        self.assertFalse(self.bridge.completed)
        self.bridge.request.assert_not_called()

    def test_returned_batch_list_can_then_go_back_to_root_folders(self):
        self.results(); self.publish_one(); self.bridge.backToFolders()
        self.bridge.request.reset_mock()
        self.bridge.backToFolders()
        self.bridge.request.assert_called_once_with(dict(action='folders', offset=0, hide_published=True))

    def test_plain_folder_return_preserves_page_and_explicit_refresh_clears_cached_exes(self):
        self.bridge.loaded(dict(stage='folders', offset=20, total=45, rows=[dict(name='One', folder='One')]))
        self.publish_one()
        self.bridge.request.reset_mock(); self.bridge.backToFolders()
        self.assertEqual((self.bridge.state['stage'], self.bridge.state['offset']), ('folders', 20))
        self.assertTrue(self.bridge.state['rows'][0]['published'])
        self.assertTrue(self.bridge.exe_cache)
        self.bridge.request.assert_not_called()
        self.bridge.folders(20)
        self.assertFalse(self.bridge.exe_cache or self.bridge.completed)
        self.bridge.request.assert_called_once_with(dict(action='folders', offset=20, hide_published=True))

    def test_same_server_reopens_current_view_and_new_server_discards_cached_views(self):
        self.results(); self.publish_one(); self.bridge.backToFolders()
        before = copy.deepcopy(self.bridge.state)
        self.bridge.request.reset_mock()
        self.bridge.openServer(self.bridge.url)
        self.assertEqual(self.bridge.state, before)
        self.bridge.request.assert_not_called()
        self.bridge.openServer('http://127.0.0.1:9999/tactic')
        self.assertIsNone(self.bridge.folder_view)
        self.assertFalse(self.bridge.exe_cache or self.bridge.completed)
        self.bridge.request.assert_called_once_with(dict(action='folders', offset=0, hide_published=True))

    def test_installer_collection_keeps_other_setup_available_after_publishing_one(self):
        self.bridge.installers = True
        self.results()
        self.publish_one()
        self.bridge.backToFolders()
        self.assertFalse(self.bridge.state['rows'][0].get('published', False))
        self.bridge.reviewFolder('One')
        self.assertTrue(self.bridge.state['rows'][0]['published'])

    def test_pending_work_is_polled_and_new_batch_explicitly_discards_previous_scan(self):
        self.results(); self.publish_one(); self.bridge.backToFolders()
        self.bridge._state.update(stage='working', preview='cd'*16)
        self.bridge.request.reset_mock()
        self.bridge.openServer(self.bridge.url)
        self.bridge.request.assert_called_once_with(dict(action='status', preview='cd'*16))
        self.bridge.batch()
        self.assertIsNone(self.bridge.folder_view)
        self.assertFalse(self.bridge.exe_cache or self.bridge.completed)

    def test_back_restores_each_screen_and_selection_without_api_for_both_catalogizers(self):
        for installers in (False, True):
            with self.subTest(installers=installers):
                self.bridge.installers = installers
                self.bridge.loaded(dict(stage='folders', offset=20, total=40,
                    rows=[dict(name='One', folder='One'), dict(name='Two', folder='Two')]))
                self.bridge.reviewFolder('Two')
                self.bridge.loaded(dict(stage='exes', folder='Two', rows=[dict(path='one.exe'), dict(path='two.exe')]))
                self.bridge.choose(1)
                self.bridge.loaded(dict(stage='matches', rows=[dict(name='First', app_id='1'), dict(name='Second', app_id='2')]))
                self.bridge.search('Custom query')
                self.bridge.choose(1)
                self.bridge.loaded(dict(stage='review', preview='cd'*16, card=dict(id='game')))
                self.bridge.request.reset_mock()
                self.assertTrue(self.bridge.back())
                self.assertEqual((self.bridge.state['stage'], self.bridge.state['query'], self.bridge.state['selected_index']),
                                 ('matches', 'Custom query', 1))
                self.assertTrue(self.bridge.back())
                self.assertEqual((self.bridge.state['stage'], self.bridge.state['selected_index']), ('exes', 1))
                self.assertTrue(self.bridge.back())
                self.assertEqual((self.bridge.state['stage'], self.bridge.state['offset'], self.bridge.state['selected_index']),
                                 ('folders', 20, 1))
                self.assertFalse(self.bridge.back())
                self.assertFalse(self.bridge.navigation_views)
                self.bridge.request.assert_not_called()
                self.published.assert_not_called()

    def test_back_while_busy_or_publishing_keeps_job_and_does_not_cancel(self):
        for stage, busy in (('matches', True), ('working', False), ('batch', False)):
            with self.subTest(stage=stage):
                self.bridge._state.update(stage=stage, busy=busy, state='working', preview='ab'*16)
                self.assertTrue(self.bridge.back())
                self.assertEqual((self.bridge.state['stage'], self.bridge.state['preview']), (stage, 'ab'*16))
                self.bridge.request.assert_not_called()

    def test_new_folder_and_server_discard_previous_selection_screens(self):
        self.bridge.navigation_views['matches'] = dict(stage='matches')
        self.bridge.reviewFolder('Other')
        self.assertFalse(self.bridge.navigation_views)
        self.bridge.navigation_views['matches'] = dict(stage='matches')
        self.bridge.openServer('http://127.0.0.1:9999/tactic')
        self.assertFalse(self.bridge.navigation_views)


if __name__ == '__main__': unittest.main()
