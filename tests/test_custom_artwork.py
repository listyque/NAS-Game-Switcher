import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QMetaObject, QUrl
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from ngs.metadata import custom_metadata
from ngs.catalog import ArtworkCache
from ngs.install import LocalLibrary, Cancelled
from ngs.app import LocalArtworkWorker
from ngs.store_games import import_store_games
from tests import test_manual_artwork as fixture


class CustomArtworkTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ManualArtworkTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ManualArtworkTests.tearDownClass.__func__)
    tearDown = fixture.ManualArtworkTests.tearDown
    wait = fixture.ManualArtworkTests.wait

    def setUp(self):
        fixture.ManualArtworkTests.setUp(self)
        self.source = self.root/'own cover.png'
        image = QImage(120, 180, QImage.Format.Format_RGB32)
        image.fill(QColor('#739aa4')); self.assertTrue(image.save(str(self.source)))
        self.url = QUrl.fromLocalFile(str(self.source)).toString()
        self.editor = self.window.findChild(QObject, 'localArtworkEditor')

    def open_editor(self):
        self.b._metadata = dict(query='Unknown game', local_id=self.identity, results=[], status='')
        self.b.metadataChanged.emit(); self.b.metadataRequested.emit(); QTest.qWait(200)
        self.picker.setProperty('selectedField', 4)
        self.b.action.emit('activate'); QTest.qWait(180)
        self.assertTrue(self.editor.property('visible'))

    def fill(self, title='My own game', description='My description\nSecond line'):
        self.editor.findChild(QObject, 'manualCardTitle').setProperty('text', title)
        self.editor.findChild(QObject, 'manualCardDescription').setProperty('text', description)
        self.editor.setProperty('cover', self.url)

    def save(self):
        self.editor.setProperty('page', 1); self.editor.setProperty('selectedField', 4)
        self.b.action.emit('activate'); self.wait(); QTest.qWait(200)

    def test_offline_gamepad_save_copies_cover_and_preserves_launch_and_history(self):
        with patch('ngs.app.client_metadata', side_effect=AssertionError('Custom artwork used the network')):
            self.open_editor(); self.fill(); self.save()
        self.assertFalse(self.editor.property('visible')); self.assertFalse(self.picker.property('visible'))
        self.source.unlink()
        library = LocalLibrary(self.storage); row = library.reconcile()[0]
        self.assertTrue(row['catalog_id'].startswith('custom-')); self.assertTrue(row['metadata_manual'])
        self.assertEqual(row['card']['name'], 'My own game')
        self.assertEqual(row['card']['description'], 'My description\nSecond line')
        self.assertTrue(ArtworkCache(self.storage).image_valid(row['card']['cover']))
        self.assertEqual(library.launch_arguments(self.identity), '--windowed')
        self.assertEqual(row['executable_sha256'], self.receipt['executable_sha256'])
        self.assertEqual(library.update_external_metadata(self.identity, self.old)['card']['name'], 'My own game')

    def test_cancel_keeps_receipt_and_return_to_search(self):
        before = (self.b.library.records/(self.identity+'.json')).read_bytes()
        self.open_editor(); self.fill()
        self.b.action.emit('back'); QTest.qWait(180)
        self.assertFalse(self.editor.property('visible')); self.assertTrue(self.picker.property('visible'))
        self.assertFalse(self.picker.property('covered'))
        self.assertEqual((self.b.library.records/(self.identity+'.json')).read_bytes(), before)

    def test_existing_custom_title_can_be_renamed_again_without_changing_identity(self):
        self.open_editor(); self.fill(); self.save()
        cid = self.b.library.reconcile()[0]['catalog_id']
        self.open_editor()
        self.assertEqual(self.editor.findChild(QObject, 'manualCardTitle').property('text'), 'My own game')
        self.editor.findChild(QObject, 'manualCardTitle').setProperty('text', 'Edited again')
        self.save()
        row = self.b.library.reconcile()[0]
        self.assertEqual(row['catalog_id'], cid); self.assertEqual(row['card']['name'], 'Edited again')

    def test_invalid_fields_and_images_do_not_replace_saved_card(self):
        before = (self.b.library.records/(self.identity+'.json')).read_bytes()
        self.open_editor(); self.fill(title='')
        self.save()
        self.assertTrue(self.editor.property('visible')); self.assertFalse(self.b.metadataState['custom_saving'])
        self.assertEqual((self.b.library.records/(self.identity+'.json')).read_bytes(), before)
        self.fill(); self.source.write_text('not an image')
        self.save()
        self.assertTrue(self.editor.property('visible'))
        self.assertEqual((self.b.library.records/(self.identity+'.json')).read_bytes(), before)
        QMetaObject.invokeMethod(self.editor, 'close'); QTest.qWait(180)

    def test_cover_only_without_provider_and_input_limits(self):
        cancel = threading.Event()
        card = custom_metadata(self.storage, 'Independent game', '', self.url, '', {}, cancel)
        self.assertEqual(card['cover'], card['background']); self.assertEqual(card['metadata_url'], '')
        for name, desc, source in [(' ', '', self.url), ('a'*161, '', self.url), ('Title', 'a'*3001, self.url),
                                   ('Title', '', 'https://example.com/image.png'), ('Title', '', '')]:
            with self.assertRaises(ValueError): custom_metadata(self.storage, name, desc, source, '', {}, cancel)
        self.source.write_bytes(b'x'*(8*1024*1024+1))
        with self.assertRaises(ValueError): custom_metadata(self.storage, 'Title', '', self.url, '', {}, cancel)
        cancel.set()
        with self.assertRaises(Cancelled): custom_metadata(self.storage, 'Title', '', self.url, '', {}, cancel)

    def test_editor_opens_during_a_slow_search_and_keeps_draft_until_search_stops(self):
        release = threading.Event()
        def metadata(*args, **kwargs):
            release.wait(5)
            raise Cancelled()
        try:
            with patch('ngs.app.client_metadata', side_effect=metadata):
                self.b.findMetadata(self.identity); QTest.qWait(180)
                self.assertTrue(self.b.operationBusy)
                self.picker.setProperty('selectedField', 4)
                self.b.action.emit('activate'); QTest.qWait(180)
                self.assertTrue(self.editor.property('visible'))
                self.fill(); self.editor.setProperty('page', 1)
                QMetaObject.invokeMethod(self.editor.findChild(QObject, 'manualCardSave'), 'clicked')
                self.assertTrue(self.b.metadataState['custom_status'])
                self.assertTrue(self.editor.property('visible'))
                release.set(); self.wait(); self.save()
                self.assertEqual(self.b.library.reconcile()[0]['card']['name'], 'My own game')
        finally:
            release.set(); self.wait()

    def test_automatic_lookup_skips_custom_cards_even_with_an_empty_description(self):
        card = custom_metadata(self.storage, 'Independent game', '', self.url, '', {}, threading.Event())
        row = self.b.library.update_external_metadata(self.identity, card, manual=True, rename=True)
        with patch('ngs.bulk_scan.metadata_requests') as request:
            worker = LocalArtworkWorker(self.storage, '', ArtworkCache(self.storage), self.b.library, [row], [self.old])
            worker.run(); request.assert_not_called()

    def test_store_import_keeps_custom_card_with_an_empty_description_without_network(self):
        self.b.library.forget(self.identity)
        card = custom_metadata(self.storage, 'Independent game', '', self.url, '', {}, threading.Event())
        rows = [dict(path=str(self.root/'Wrong Game'), name=card['name'], preferred_exe='play.exe', store='Steam',
                     store_id='12', metadata=card, metadata_manual=True)]
        with patch('ngs.bulk_scan.client_metadata', side_effect=AssertionError('Custom store card used the network')):
            result = import_store_games(self.storage, rows, [], threading.Event(), lambda event: None)
            self.assertEqual(result['rows'][0]['state'], 'added')
            self.assertEqual(import_store_games(self.storage, rows, [], threading.Event(), lambda event: None)['rows'][0]['state'], 'already')
        row = self.b.library.reconcile()[0]
        self.assertTrue(row['metadata_manual']); self.assertEqual(row['catalog_id'], card['id'])
