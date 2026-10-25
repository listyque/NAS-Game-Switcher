import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote
from ngs.install import install_game, build_manifest
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from ngs.appearance import colors, ACCENTS
from ngs.startup_screen import StartupScreen
from tests import test_ui


class StartupAccentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ngs.app import create_window
        cls.boot_phases=Mock()
        with patch.object(test_ui,'create_window',side_effect=lambda storage,**kwargs:
                          create_window(storage,**kwargs,startup_screen=cls.boot_phases)):
            test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def test_chosen_caption_artwork_and_installed_badge_follow_theme_in_list_and_grid(self):
        bridge, window = self.engine.bridge, self.window
        previous = dict(bridge.settings)
        games, installed = bridge._games, bridge._installed
        source=self.root/'accent-source';(source/'fixture').mkdir(parents=True)
        (source/'fixture/play.exe').write_bytes(b'fixture')
        card=json.loads((Path(__file__).resolve().parents[1]/'examples/black-mesa.json').read_text(encoding='utf8'))
        art=dict(path='/assets/ngs_launcher/fixture.svg',size=1,sha256='0'*64)
        card.update(id='accent-card',source_folder='fixture',executable='play.exe',revision=1,cover=art,background=art)
        install_game(self.storage,source,card,build_manifest(source,card))
        bridge._installed=bridge.library.reconcile();bridge.libraryChanged.emit();bridge.catalog_loaded([card])
        try:
            window.setProperty('section',1); window.setProperty('focusZone',2); window.setProperty('selectedIndex',0)
            for view in ('list','grid'):
                bridge.setGamesView(view)
                for theme in ('dark','light'):
                    for accent in ACCENTS:
                        bridge.setAppearance(theme,accent); QTest.qWait(300)
                        items=window.findChild(QObject,'gameGrid' if view=='grid' else 'gameCards')
                        delegate=items.property('currentItem');self.assertIsNotNone(delegate)
                        self.assertTrue(delegate.property('chosen'))
                        palette=bridge.themeColors
                        self.assertEqual(delegate.findChild(QObject,'cardCaption-0').property('color'),QColor(palette['accent']))
                        self.assertEqual(delegate.findChild(QObject,'cardArtworkTitle-0').property('color'),QColor(palette['accent']))
                        badge=delegate.findChild(QObject,'cardStatus-0')
                        self.assertTrue(badge.isVisible())
                        self.assertEqual(badge.property('color'),QColor(palette['accentText']))
                        self.assertEqual(badge.parentItem().property('color'),QColor(palette['accent']))
        finally:
            bridge.library.uninstall(card['id'])
            bridge._installed=installed;bridge.libraryChanged.emit();bridge.catalog_loaded(games)
            bridge.settings=previous;bridge.save();bridge.settingsChanged.emit()

    def test_splash_palette_logo_status_and_visible_animation_then_cleanup(self):
        previous=self.storage.read_settings()
        try:
            for theme in ('dark','light'):
                for accent in ACCENTS:
                    settings=dict(previous,theme=theme,accent_color=accent);self.storage.write_settings(settings)
                    palette=colors(settings)
                    cover=StartupScreen(self.storage,True)
                    try:
                        self.assertEqual(cover.window.color(),QColor(palette['background']))
                        self.assertEqual(cover.window.findChild(QObject,'startupTitle').property('color'),QColor(palette['accent']))
                        self.assertIn(palette['accent'],unquote(cover.window.property('logoSource').toString()))
                        self.assertTrue(cover.window.findChild(QObject,'startupPulse').property('running'))
                        content=cover.window.contentItem().childItems()[0]
                        self.assertEqual({item.objectName() for item in content.childItems()}, {'startupLogo','startupTitle','startupStatus'})
                        frames=[]
                        def presented():frames.append(True)
                        cover.window.frameSwapped.connect(presented,Qt.ConnectionType.QueuedConnection)
                        cover.set_status('Подготавливаем интерфейс')
                        cover.window.frameSwapped.disconnect(presented)
                        self.assertTrue(frames, 'Phase status must reach a frame before the next blocking stage')
                        self.assertEqual(cover.window.findChild(QObject,'startupStatus').property('text'),'Подготавливаем интерфейс')
                        QTest.qWait(80);first=cover.window.grabWindow()
                        QTest.qWait(300);second=cover.window.grabWindow()
                        self.assertNotEqual(hashlib.sha256(first.constBits()).digest(),hashlib.sha256(second.constBits()).digest())
                        main=Mock();main.property.return_value=False;main.opacity.return_value=0
                        cover.attach(main)
                        self.assertEqual(cover.window.property('statusText'),'Загружаем обложки')
                        cover.presented();self.assertFalse(cover.done)
                        main.property.return_value=True;cover.presented();main.opacity.return_value=1;cover.presented()
                        self.assertTrue(cover.done);self.assertFalse(cover.window.isVisible())
                        self.assertFalse(cover.timeout.isActive())
                    finally:
                        cover.close();QTest.qWait(10)
        finally:self.storage.write_settings(previous)

    def test_create_window_reports_real_boot_phases(self):
        self.assertEqual([call.args[0] for call in self.boot_phases.set_status.call_args_list],
                         ['Загружаем библиотеку','Подготавливаем интерфейс'])


if __name__=='__main__':unittest.main()
