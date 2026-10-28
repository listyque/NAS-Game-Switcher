import json
import unittest
from PySide6.QtCore import QObject, QMetaObject
from PySide6.QtQml import QQmlExpression, QQmlEngine
from PySide6.QtTest import QTest
from tests import test_ui


class InstallerStatesTests(unittest.TestCase):
    ui_language = 'en'

    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def evaluate(self, code):
        expression = QQmlExpression(QQmlEngine.contextForObject(self.window), self.window, code)
        result, unused = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return result

    def item(self, name):
        pending = [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name: return item
            pending.extend(item.childItems())
        self.fail('Missing visual item: ' + name)

    def setUp(self):
        self.bridge = self.engine.bridge
        self.original = self.bridge._installer_games, self.bridge._installer_packages, self.bridge._installed
        self.original_view = self.bridge.gamesView
        self.cards = [dict(self.bridge._games[0], id='setup-' + name, game_id='game-' + name,
                           name=name, kind='installer', delivery='on_request', metadata_url='https://store.example/' + name)
                      for name in ('empty', 'downloaded', 'installed', 'game-only')]
        self.bridge._installer_games = self.cards
        self.bridge._installer_packages = [dict(card=self.cards[index]) for index in (1, 2)]
        self.bridge._installed = [dict(card=dict(self.cards[index], id='local-' + str(index)),
                                       catalog_id=self.cards[index]['game_id'], available=True, kind='external') for index in (2, 3)]
        self.bridge.libraryChanged.emit(); self.bridge.installersChanged.emit()
        self.evaluate('filterStates=({});searchQueries=({})')
        self.window.setProperty('section', 3); self.window.setProperty('focusZone', 2)
        QTest.qWait(150)

    def tearDown(self):
        QMetaObject.invokeMethod(self.window.findChild(QObject, 'detailsPopup'), 'close')
        QMetaObject.invokeMethod(self.window.findChild(QObject, 'gameFiltersPane'), 'close')
        self.bridge._installer_games, self.bridge._installer_packages, self.bridge._installed = self.original
        self.bridge.setGamesView(self.original_view)
        self.evaluate('filterStates=({});searchQueries=({})')
        self.bridge.libraryChanged.emit(); self.bridge.installersChanged.emit()
        self.window.resize(1280, 800); QTest.qWait(150)

    def test_package_and_game_are_independent_and_updates_preserve_source_cards(self):
        self.assertEqual([(c['package_downloaded'], c['game_installed']) for c in self.bridge.installerGames],
                         [(False, False), (True, False), (True, True), (False, True)])
        self.assertNotIn('game_installed', self.cards[2])
        self.bridge._installed[0]['available'] = False
        self.bridge.libraryChanged.emit()
        self.assertFalse(self.bridge.installerGames[2]['game_installed'])
        self.assertTrue(self.bridge.installerGames[2]['package_downloaded'])
        self.bridge._installed[0]['available'] = True
        self.bridge._installed[0]['catalog_id'] = 'other-provider-id'
        self.bridge._installed[0]['card']['metadata_url'] += '/'
        self.assertTrue(self.bridge.installerGames[2]['game_installed'])
        self.bridge._installed[0]['removing'] = True
        self.assertFalse(self.bridge.installerGames[2]['game_installed'])

    def test_badges_details_and_actions_match_all_four_combinations(self):
        for index, (state, action, downloaded, installed) in enumerate((
                ('Not downloaded', 'Download package', False, False), ('Downloaded', 'Install', True, False),
                ('Game installed', 'Reinstall', True, True), ('Game installed', 'Download package', False, True))):
            self.evaluate('selectedIndex=' + str(index)); QTest.qWait(40)
            self.assertEqual(self.window.property('primaryActionText'), action)
            self.assertEqual(self.window.property('installerGameInstalled'), installed)
            self.assertEqual(self.window.property('installerReady'), downloaded)
            for view in ('ribbon', 'grid'):
                self.bridge.setGamesView(view); QTest.qWait(80)
                label = self.item('cardStatus-' + str(index))
                self.assertEqual(label.property('text'), state)
                self.assertTrue(label.parent().property('visible'))
            QMetaObject.invokeMethod(self.window, 'openDetails'); QTest.qWait(180)
            text = self.window.findChild(QObject, 'detailsInstallerStatus').property('text')
            self.assertEqual(text, ('Package downloaded' if downloaded else 'Package not downloaded') + ' · ' + ('Game installed' if installed else 'Game not installed'))
            self.assertEqual(self.window.findChild(QObject, 'detailsAddInstallerGame').property('visible'), downloaded and not installed)
            self.window.findChild(QObject, 'detailsPopup').close(); QTest.qWait(150)

    def test_download_and_install_filters_remain_separate_and_gamepad_reaches_apply(self):
        for mode, expected in (('downloaded', 'downloaded,installed'), ('not_downloaded', 'empty,game-only'),
                               ('installed', 'installed,game-only'), ('missing', 'empty,downloaded')):
            self.evaluate('applyFilters(' + json.dumps(mode) + ', "")')
            self.assertEqual(self.evaluate('displayGames.map(g=>g.name).join(",")'), expected)
        self.evaluate('applyFilters("all", "");openFilters()'); QTest.qWait(200)
        pane = self.window.findChild(QObject, 'gameFiltersPane')
        self.assertEqual(pane.property('statusCount'), 5)
        pane.setProperty('selectedField', 3); self.bridge.action.emit('activate')
        self.assertEqual(pane.property('statusIndex'), 3)
        pane.setProperty('selectedField', 6); self.bridge.action.emit('activate'); QTest.qWait(180)
        self.assertFalse(pane.property('visible'))
        self.assertEqual(self.evaluate('displayGames.map(g=>g.name).join(",")'), 'installed,game-only')
        self.bridge._installer_packages = []
        self.bridge.installersChanged.emit()
        self.assertTrue(self.bridge.installerGames[2]['game_installed'])
        self.assertFalse(self.bridge.installerGames[2]['package_downloaded'])
