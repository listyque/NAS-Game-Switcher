import json
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject,QMetaObject
from PySide6.QtTest import QTest
from ngs.install import install_game,build_manifest
from tests import test_ui


class HiddenArtworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):test_ui.UiTests.tearDownClass.__func__(cls)

    def test_closed_details_do_not_load_large_images_or_resolve_game_paths(self):
        bridge,window=self.engine.bridge,self.window
        source=self.root/'hidden-artwork-source';(source/'fixture').mkdir(parents=True)
        (source/'fixture/play.exe').write_bytes(b'fixture')
        example=json.loads((test_ui.Path(__file__).resolve().parents[1]/'examples/black-mesa.json').read_text(encoding='utf8'))
        art=dict(path='/assets/ngs_launcher/fixture.svg',size=1,sha256='0'*64)
        cards=[]
        for index,name in enumerate(('orbital.svg','local.svg','ngs.svg')):
            card=dict(example,id='hidden-art-'+str(index),name='Hidden art '+str(index),source_folder='fixture',executable='play.exe',revision=1,cover=art,background=art)
            install_game(self.storage,source,card,build_manifest(source,card))
            url=(self.repo/'assets'/name).as_uri();cards.append(dict(card,image=url,backgroundImage=url))
        original=bridge._games,bridge._installed
        bridge._installed=bridge.library.reconcile();bridge.libraryChanged.emit();bridge.catalog_loaded(cards)
        window.setProperty('section',1);window.setProperty('selectedIndex',0);QTest.qWait(250)
        details=window.findChild(QObject,'detailsPopup')
        cover=window.findChild(QObject,'detailsCover');banner=window.findChild(QObject,'detailsBanner')
        try:
            self.assertFalse(details.property('visible'))
            self.assertTrue(cover.property('source').isEmpty(),'Closed details still request a full-resolution cover')
            self.assertTrue(banner.property('source').isEmpty(),'Closed details still request a full-resolution background')
            with patch.object(bridge.library,'install_path',wraps=bridge.library.install_path) as path:
                for index in (1,2,0):
                    window.setProperty('selectedIndex',index);QTest.qWait(60)
                    self.assertTrue(cover.property('source').isEmpty());self.assertTrue(banner.property('source').isEmpty())
                self.assertEqual(path.call_count,0,'Closed EXE details touch the filesystem on selection')
                window.setProperty('detailsMore',True);QMetaObject.invokeMethod(details,'open');QTest.qWait(300)
                self.assertEqual(cover.property('source').toString(),cards[0]['image'])
                self.assertEqual(banner.property('source').toString(),cards[0]['backgroundImage'])
                self.assertGreater(path.call_count,0)
                self.assertIn('play.exe',window.findChild(QObject,'detailsExecutable').property('text'))
                window.setProperty('selectedIndex',1);QTest.qWait(120)
                self.assertEqual(cover.property('source').toString(),cards[1]['image'])
                QMetaObject.invokeMethod(details,'close');QTest.qWait(300)
                self.assertTrue(cover.property('source').isEmpty());self.assertTrue(banner.property('source').isEmpty())
                path.reset_mock();window.setProperty('selectedIndex',2);QTest.qWait(120)
                self.assertEqual(path.call_count,0)
        finally:
            details.setProperty('visible',False);window.setProperty('detailsMore',False)
            for card in cards:bridge.library.uninstall(card['id'])
            bridge._installed=original[1];bridge.libraryChanged.emit();bridge.catalog_loaded(original[0])


if __name__=='__main__':unittest.main()
