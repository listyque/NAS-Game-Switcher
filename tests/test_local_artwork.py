import base64
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QColor
from ngs.catalog import ArtworkCache, validate_card
from ngs.install import Cancelled, LocalLibrary
from ngs.metadata import client_metadata, metadata_fetch, provider_metadata
from ngs.portable import PortableStorage
from ngs.store_games import import_store_games


class LocalArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = PortableStorage(self.root/'NGS')
        self.storage.read_settings()
        self.cancel = threading.Event()
        image = QImage(8, 12, QImage.Format.Format_RGB32); image.fill(QColor('#238856'))
        buffer = QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, 'PNG')); self.data = bytes(buffer.data())
        art = dict(base64=base64.b64encode(self.data).decode(), sha256=hashlib.sha256(self.data).hexdigest())
        self.raw = dict(provider_id='steam:12', cover=art, background=art,
            card=dict(id='steam-12', name='Known Game', subtitle='A real description', description='Game description',
                      genre='Action', accent='#238856', source_folder='', executable='', available_on_nas=False,
                      metadata_url='https://store.steampowered.com/app/12/'))

    def provider(self, operation, value):
        return [dict(app_id='12', name='Known Game')] if operation=='search' else copy.deepcopy(self.raw)

    def resolve(self):
        return client_metadata(self.storage, '', 'resolve', '12', self.cancel)

    def test_direct_search_resolve_and_persisted_images_work_without_nas(self):
        with patch('ngs.metadata.game_metadata', side_effect=self.provider) as provider, \
                patch('ngs.api.MetadataApi', side_effect=AssertionError('NAS called')) as nas:
            rows = client_metadata(self.storage, '', 'search', 'Known Game', self.cancel)
            result = self.resolve(); card = validate_card(result['card'])
            self.assertEqual(rows, [dict(app_id='12', name='Known Game')])
            self.assertTrue(ArtworkCache(self.storage).image_valid(card['cover']))
            self.assertEqual(card['description'], 'Game description')
            self.assertFalse(card['available_on_nas'])
            self.assertEqual(len(list((self.storage.cache/'catalog').glob('*.image'))), 1)
            provider.reset_mock()
            self.assertEqual(self.resolve()['card'], card)
            provider.assert_not_called(); nas.assert_not_called()
            recreated = PortableStorage(self.storage.root)
            self.assertEqual(client_metadata(recreated, '', 'resolve', '12', self.cancel, offline=True)['card'], card)

    def test_expired_metadata_falls_back_without_changing_cache_age_or_hiding_bad_images(self):
        with patch('ngs.metadata.game_metadata', side_effect=self.provider): self.resolve()
        path = next(self.storage.cache.glob('local-metadata-*.json'))
        value = json.loads(path.read_bytes()); value['saved'] = 0
        path.write_text(json.dumps(value)); before = path.read_bytes()
        with patch('ngs.metadata.game_metadata', side_effect=ConnectionError('No Internet')):
            self.assertEqual(self.resolve()['card']['name'], 'Known Game')
        self.assertEqual(path.read_bytes(), before)
        bad = copy.deepcopy(self.raw); bad['cover']['sha256'] = '0'*64
        with patch('ngs.metadata.game_metadata', return_value=bad):
            with self.assertRaisesRegex(ValueError, 'checksum'): self.resolve()
        self.assertEqual(path.read_bytes(), before)

    def test_deleted_or_corrupt_artwork_is_downloaded_again(self):
        with patch('ngs.metadata.game_metadata', side_effect=self.provider) as provider:
            card = self.resolve()['card']; path = ArtworkCache(self.storage).image_path(card['cover']['sha256'])
            path.write_bytes(b'x'*len(self.data))
            self.resolve()
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(path.read_bytes(), self.data)

    def test_cancelled_provider_does_not_save_metadata_or_images(self):
        def cancel(operation, value): self.cancel.set(); return copy.deepcopy(self.raw)
        with patch('ngs.metadata.game_metadata', side_effect=cancel):
            with self.assertRaises(Cancelled): self.resolve()
        self.assertFalse(list(self.storage.cache.glob('local-metadata-*.json')))
        self.assertFalse(list((self.storage.cache/'catalog').glob('*.image')))

    def test_provider_fetch_observes_cancellation_before_any_request(self):
        self.cancel.set()
        with patch('urllib.request.build_opener') as opener:
            with self.assertRaises(InterruptedError):
                metadata_fetch('https://store.steampowered.com/api/test', 512, cancel=self.cancel)
            opener.assert_not_called()

    def test_invalid_picture_and_oversized_base64_are_rejected(self):
        bad = copy.deepcopy(self.raw); bad['cover'] = dict(base64=base64.b64encode(b'not an image').decode(), sha256=hashlib.sha256(b'not an image').hexdigest())
        with patch('ngs.metadata.game_metadata', return_value=bad):
            with self.assertRaisesRegex(ValueError, 'image'): self.resolve()
        with patch('ngs.metadata.game_metadata', side_effect=self.provider), patch('ngs.catalog.MAX_IMAGE', 8):
            with self.assertRaisesRegex(ValueError, 'size'): self.resolve()
        self.assertFalse(list(self.storage.cache.glob('local-metadata-*.json')))

    def test_store_import_writes_description_and_cover_to_sqlite_and_keeps_game_files(self):
        folder = self.root/'Known Game'; folder.mkdir(); shutil.copyfile(sys.executable, folder/'play.exe')
        rows = [dict(store='Steam', store_id='12', name='Known Game', path=str(folder), preferred_exe='play.exe', candidates=[], state='found')]
        before = (folder/'play.exe').read_bytes()
        with patch('ngs.metadata.game_metadata', side_effect=self.provider), \
                patch('ngs.api.MetadataApi', side_effect=AssertionError('NAS called')):
            result = import_store_games(self.storage, rows, [], self.cancel, lambda event: None)
        self.assertEqual(result['rows'][0]['state'], 'added')
        library = LocalLibrary(self.storage); receipt = library.reconcile()[0]
        self.assertEqual(receipt['card']['description'], 'Game description')
        self.assertTrue(ArtworkCache(self.storage).image_valid(receipt['card']['cover']))
        with closing(sqlite3.connect(library.path)) as db:
            self.assertIn('Game description', db.execute('SELECT receipt FROM installed').fetchone()[0])
        self.assertEqual((folder/'play.exe').read_bytes(), before)

    def test_ambiguous_provider_search_keeps_the_game_for_manual_artwork_selection(self):
        folder = self.root/'Known Game'; folder.mkdir(); shutil.copyfile(sys.executable, folder/'play.exe')
        rows = [dict(store='Epic Games Store', name='Known Game', path=str(folder), preferred_exe='play.exe', candidates=[], state='found')]
        matches = [dict(app_id=str(i), name='Known Game') for i in (12, 13)]
        with patch('ngs.metadata.game_metadata', return_value=matches) as provider:
            result = import_store_games(self.storage, rows, [], self.cancel, lambda event: None)
        self.assertEqual(result['rows'][0]['state'], 'artwork')
        self.assertEqual(len(LocalLibrary(self.storage).reconcile()), 1)
        self.assertEqual(provider.call_args.args[0], 'search')

    def test_language_has_a_separate_metadata_cache(self):
        with patch('ngs.metadata.game_metadata', side_effect=self.provider) as provider:
            self.resolve()
            settings = self.storage.read_settings(); settings['language']='ru'; self.storage.write_settings(settings)
            self.resolve()
            self.assertEqual(provider.call_count, 2)

    def test_background_enrichment_preserves_local_identity_launch_options_and_playtime(self):
        from ngs.app import LocalArtworkWorker
        from ngs.play_time import PlayTime
        folder = self.root/'Known Game'; folder.mkdir(); shutil.copyfile(sys.executable, folder/'play.exe')
        library = LocalLibrary(self.storage)
        receipt = library.register_external(folder, 'play.exe', 'Known Game')
        identity = receipt['card']['id']; library.set_launch_arguments(identity, '--windowed')
        clock = [0.0]; play = PlayTime(library.path, clock=lambda: clock[0])
        play.start(identity); clock[0]=5; play.checkpoint(force=True, reason='exited')
        worker = LocalArtworkWorker(self.storage, '', ArtworkCache(self.storage), library, library.reconcile(), [])
        with patch('ngs.metadata.game_metadata', side_effect=self.provider), patch('ngs.api.MetadataApi', side_effect=AssertionError('NAS called')):
            worker.run()
        updated = library.reconcile()[0]
        self.assertEqual(updated['card']['id'], identity)
        self.assertEqual(updated['catalog_id'], 'steam-12')
        self.assertEqual(updated['card']['description'], 'Game description')
        self.assertEqual(library.launch_arguments(identity), '--windowed')
        with closing(sqlite3.connect(library.path)) as db:
            self.assertEqual(db.execute('SELECT active_ms FROM play_sessions WHERE game_id=?', (identity,)).fetchone()[0], 5000)

    def test_collection_scan_uses_public_metadata_and_preserves_ambiguous_executables(self):
        from ngs.bulk_scan import scan_collection
        folder = self.root/'Games'; folder.mkdir()
        game = folder/'Known Game'; game.mkdir(); shutil.copyfile(sys.executable, game/'play.exe')
        ambiguous = folder/'Ambiguous'; ambiguous.mkdir()
        for name in ('first.exe','second.exe'): shutil.copyfile(sys.executable, ambiguous/name)
        with patch('ngs.metadata.game_metadata', side_effect=self.provider), patch('ngs.api.MetadataApi', side_effect=AssertionError('NAS called')):
            result = scan_collection(self.storage, folder, [], '', self.cancel, lambda event:None)
        self.assertEqual(result['summary']['added'], 1)
        self.assertEqual(result['summary']['manual'], 1)
        receipt = LocalLibrary(self.storage).reconcile()[0]
        self.assertEqual(receipt['card']['description'], 'Game description')
