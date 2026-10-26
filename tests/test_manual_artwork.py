import copy
import hashlib
import json
import shutil
import sqlite3
import sys
import unittest
from contextlib import closing
from unittest.mock import patch

from PySide6.QtCore import QObject, QMetaObject, QBuffer, QIODevice
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from ngs.app import LocalArtworkWorker
from ngs.catalog import ArtworkCache
from ngs.install import LocalLibrary
from ngs.play_time import PlayTime
from ngs.metadata import game_metadata
from tests import test_ui


class ManualArtworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop(); test_ui.UiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.b = self.engine.bridge
        self.original = copy.deepcopy(self.b.settings)
        self.b.settings.update(local_mode=True, resolve_local_metadata=False)
        self.b.settingsChanged.emit()
        image=QImage(8,12,QImage.Format.Format_RGB32);image.fill(QColor('#419964'))
        buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly);image.save(buffer,'PNG')
        data=bytes(buffer.data());digest=hashlib.sha256(data).hexdigest()
        art=dict(path='/assets/ngs_launcher/local/'+digest+'.image',size=len(data),sha256=digest)
        ArtworkCache(self.storage).cache_image(art,data)
        self.old=dict(id='steam-12',name='Wrong Game',subtitle='Old',description='Old description',genre='Action',
                      accent='#419964',source_folder='',executable='',available_on_nas=False,revision=1,cover=art,background=art,
                      metadata_url='https://store.steampowered.com/app/12/')
        self.chosen=dict(self.old,id='steam-13',name='Correct Game',description='Correct description',metadata_url='https://store.steampowered.com/app/13/')
        folder=self.root/'Wrong Game';folder.mkdir(exist_ok=True);shutil.copyfile(sys.executable,folder/'play.exe')
        self.receipt=self.b.library.register_external(folder,'play.exe','Wrong Game',self.old)
        self.identity=self.receipt['card']['id'];self.b.library.set_launch_arguments(self.identity,'--windowed')
        self.b._installed=self.b.library.reconcile();self.b.libraryChanged.emit()
        self.picker=self.window.findChild(QObject,'metadataPicker')

    def tearDown(self):
        self.wait()
        self.picker.close();QTest.qWait(180)
        self.b.library.forget(self.identity);self.b._installed=self.b.library.reconcile()
        self.b.settings=self.original;self.b.settingsChanged.emit();self.b.libraryChanged.emit()

    def wait(self):
        for _ in range(300):
            QTest.qWait(10)
            if not self.b.operationBusy:return
        self.fail('Artwork operation did not finish')

    def metadata(self,storage,url,operation,value,cancel,progress=None,**kwargs):
        self.assertEqual(url,'')
        return [dict(app_id='13',name='Correct Game')] if operation=='search' else dict(card=copy.deepcopy(self.chosen),message='Saved')

    def preview(self):
        self.b.findMetadata(self.identity);self.wait();QTest.qWait(220)
        self.b.action.emit('activate');self.wait();QTest.qWait(180)
        self.assertTrue(self.picker.property('hasPreview'))

    def test_preview_does_not_modify_game_and_cancel_returns_to_results(self):
        before=(self.b.library.records/(self.identity+'.json')).read_bytes()
        with patch('ngs.app.client_metadata',side_effect=self.metadata):self.preview()
        self.assertEqual((self.b.library.records/(self.identity+'.json')).read_bytes(),before)
        self.assertEqual(self.picker.findChild(QObject,'metadataPreviewTitle').property('text'),'Correct Game')
        self.assertTrue(self.picker.findChild(QObject,'metadataPreviewCover').property('source').toString().startswith('file:'))
        self.b.action.emit('back');QTest.qWait(120)
        self.assertTrue(self.picker.property('visible'));self.assertFalse(self.picker.property('hasPreview'))
        self.assertFalse(self.b.applyMetadataPreview())
        self.assertEqual((self.b.library.records/(self.identity+'.json')).read_bytes(),before)

    def test_gamepad_confirmation_persists_choice_and_automatic_lookup_cannot_replace_it(self):
        clock=[0.0];play=PlayTime(self.b.library.path,clock=lambda:clock[0]);play.start(self.identity)
        clock[0]=5;play.checkpoint(force=True,reason='exited')
        with patch('ngs.app.client_metadata',side_effect=self.metadata):
            self.preview();self.b.action.emit('activate');self.wait();QTest.qWait(180)
        self.assertFalse(self.picker.property('visible'))
        library=LocalLibrary(self.storage);row=next(r for r in library.reconcile() if r['card']['id']==self.identity)
        self.assertEqual(row['catalog_id'],'steam-13');self.assertTrue(row['metadata_manual'])
        self.assertEqual(row['card']['name'],'Correct Game');self.assertEqual(library.launch_arguments(self.identity),'--windowed')
        # A stale background worker must not replace the explicitly chosen provider, even after restart.
        rejected=library.update_external_metadata(self.identity,self.old)
        self.assertEqual(rejected['card']['description'],'Correct description')
        with closing(sqlite3.connect(library.path)) as db:
            self.assertEqual(db.execute('SELECT active_ms FROM play_sessions WHERE game_id=?',(self.identity,)).fetchone()[0],5000)
        self.assertEqual(row['executable_sha256'],self.receipt['executable_sha256'])

    def test_click_confirmation_and_failed_save_keeps_the_preview(self):
        with patch('ngs.app.client_metadata',side_effect=self.metadata):self.preview()
        with patch.object(self.b.library,'update_external_metadata',side_effect=OSError('Fixture disk full')):
            QMetaObject.invokeMethod(self.picker.findChild(QObject,'metadataApply'),'clicked');self.wait()
        self.assertTrue(self.picker.property('hasPreview'));self.assertFalse(self.b.metadataState['applying'])
        self.assertIn('Fixture disk full',self.b.metadataState['status'])
        QMetaObject.invokeMethod(self.picker.findChild(QObject,'metadataApply'),'clicked');self.wait();QTest.qWait(180)
        self.assertFalse(self.picker.property('visible'))
        self.assertEqual(self.b.library.reconcile()[0]['catalog_id'],'steam-13')

    def test_resulting_choice_can_be_corrected_again_explicitly(self):
        self.b.library.update_external_metadata(self.identity,self.chosen,manual=True)
        changed=self.b.library.update_external_metadata(self.identity,self.old,manual=True)
        self.assertEqual(changed['catalog_id'],'steam-12');self.assertTrue(changed['metadata_manual'])

    def test_empty_search_can_be_corrected_and_choice_is_pinned_during_import(self):
        self.b.library.forget(self.identity)
        self.b._installed=self.b.library.reconcile();self.b.libraryChanged.emit()
        self.b._scanner.update(path=str(self.root/'Wrong Game'),exe='play.exe',stage='confirm',matches=[],store_review='')
        def metadata(storage,url,operation,value,cancel,progress=None):
            if operation=='search' and value=='Wrong Game':return []
            return self.metadata(storage,url,operation,value,cancel,progress)
        with patch('ngs.app.client_metadata',side_effect=metadata):
            self.b.findMetadata('');self.wait();QTest.qWait(200)
            self.assertEqual(self.b.metadataState['results'],[])
            self.picker.findChild(QObject,'metadataQuery').setProperty('text','Correct Game')
            self.b.action.emit('refresh');self.wait()
            self.picker.setProperty('selectedField',2)
            self.b.action.emit('activate');self.wait();QTest.qWait(180)
            self.assertTrue(self.picker.property('hasPreview'))
            self.b.action.emit('activate');self.wait();QTest.qWait(180)
        self.assertEqual(self.b._scanner['manual_metadata_id'],'steam-13')
        self.b.confirmLocalGame('Correct Game','steam-13');self.wait()
        row=self.b.library.reconcile()[0]
        self.assertTrue(row['metadata_manual']);self.assertEqual(row['catalog_id'],'steam-13')

    def test_manual_wikipedia_choice_repairs_by_its_saved_reference(self):
        card=dict(self.chosen,id='wikipedia-en-123',metadata_url='https://en.wikipedia.org/?curid=123')
        row=self.b.library.update_external_metadata(self.identity,card,manual=True)
        worker=LocalArtworkWorker(self.storage,'',ArtworkCache(self.storage),self.b.library,[row],[])
        self.assertEqual(worker._infer_reference(row,card),'wikipedia:en:123')

    def test_gog_store_link_searches_its_slug_without_requesting_an_arbitrary_url(self):
        with patch('ngs.metadata.gog_metadata',return_value=[dict(app_id='gog:12',name='Correct Game')]) as gog:
            self.assertEqual(game_metadata('search','https://www.gog.com/en/game/correct_game')[0]['app_id'],'gog:12')
            gog.assert_called_once_with('search','correct game')
