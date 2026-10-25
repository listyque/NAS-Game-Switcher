import json
import time
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject, QMetaObject
from PySide6.QtTest import QTest
from ngs import install
from tests import test_ui


class ReadableGamesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def test_organize_button_a_and_touch_rename_all_managed_games_and_refresh_cards(self):
        bridge = self.engine.bridge
        pane = self.window.findChild(QObject, 'settingsPane')
        before = bridge._games, bridge._installed, bridge._installer_games, bridge._installer_packages
        source = self.root/'source'; source.mkdir()
        for identity in ('first', 'second'):
            folder = source/identity; folder.mkdir(); (folder/'play.exe').write_bytes(b'fixture')
        cards = []
        for identity in ('first', 'second'):
            card = json.loads((self.repo/'examples/black-mesa.json').read_text(encoding='utf8'))
            card.update(id=identity, name='Game '+identity, source_folder=identity, executable='play.exe')
            art = dict(path='/assets/ngs_launcher/fixture.png', size=1, sha256='0'*64)
            card.update(revision=1, cover=art, background=art)
            cards.append(card)
            with patch.object(install, 'game_folder_name', side_effect=lambda card: card['id']):
                install.install_game(self.storage, source, card, install.build_manifest(source, card))
        package = dict(cards[0], id='setup-old', kind='installer', game_id=cards[0]['id'], name='Game first by xatab')
        manifest = install.build_manifest(source, package)
        install.install_game(self.storage, source, package, manifest)
        bridge._installer_games = [dict(package, id='setup-current', name='Game first')]
        bridge._installer_packages = bridge.installer_library.reconcile()
        bridge._games = cards; bridge._installed = bridge.library.reconcile(); bridge.libraryChanged.emit()
        bridge.installersChanged.emit()
        try:
            QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(200)
            bridge.set_input_device('gamepad')
            pane.setProperty('page', 2); pane.setProperty('focusSide', 1); pane.setProperty('selectedField', 39)
            QTest.qWait(450)
            button = self.window.findChild(QObject, 'organizeLibraryButton')
            self.assertEqual(button.property('fieldIndex'), 39)
            self.assertEqual(button.property('text'), 'Привести в порядок')
            self.assertTrue(button.property('enabled'))
            bridge.action.emit('activate')
            for _ in range(400):
                if not bridge.operationBusy: break
                QTest.qWait(10); time.sleep(.001)
            self.assertFalse(bridge.operationBusy)
            self.assertIn('Приведено в порядок: 3', bridge.settingsStatus)
            for card in cards:
                self.assertTrue((self.storage.games/card['name']/'play.exe').is_file())
                self.assertFalse((self.storage.games/card['id']).exists())
            self.assertEqual({c['name'] for c in bridge.localGames}, {'Game first', 'Game second'})
            updated = bridge._installer_packages[0]
            self.assertEqual(updated['card']['id'], 'setup-old')
            self.assertEqual(updated['card']['name'], 'Game first')
            self.assertEqual(bridge.installer_library.install_path(updated).name, 'Game first (2)')
            self.assertTrue(bridge.installer_library.launch_spec('setup-old')[0].is_file())
            self.assertFalse((self.storage.games / 'Game first by xatab').exists())
            QMetaObject.invokeMethod(button, 'clicked')
            for _ in range(400):
                if not bridge.operationBusy: break
                QTest.qWait(10); time.sleep(.001)
            self.assertIn('Приведено в порядок: 0', bridge.settingsStatus)
            from types import SimpleNamespace
            bridge._installer_packages = []
            bridge.game_worker = SimpleNamespace(mode='organize')
            try:
                bridge.operation_failed('Операция прервана после переименования')
                self.assertEqual(bridge._installer_packages[0]['card']['name'], 'Game first')
            finally:
                bridge.game_worker = None
            bridge._launching = True; bridge.operationChanged.emit(); QTest.qWait(20)
            self.assertFalse(button.property('enabled')); self.assertFalse(bridge.organizeLibrary())
            bridge._launching = False; bridge.operationChanged.emit()
            bridge.process = Mock()
            self.assertFalse(bridge.organizeLibrary())
            bridge.process = None
        finally:
            bridge.process = None; bridge._launching = False
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(180)
            for card in cards:
                bridge.library.uninstall(card['id'])
            bridge.installer_library.uninstall('setup-old')
            bridge._games, bridge._installed, bridge._installer_games, bridge._installer_packages = before
            bridge.libraryChanged.emit(); bridge.installersChanged.emit()
