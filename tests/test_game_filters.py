import copy
import unittest
from PySide6.QtCore import QObject, QPointF, QMetaObject
from PySide6.QtQml import QQmlExpression, QQmlEngine
from PySide6.QtTest import QTest
from tests import test_ui


class GameFiltersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def value(self, item, code):
        expression = QQmlExpression(QQmlEngine.contextForObject(item), item, code)
        result, unused = expression.evaluate()
        self.assertFalse(expression.hasError(), expression.error().toString())
        return result

    def setUp(self):
        self.bridge = self.engine.bridge
        self.saved = (copy.deepcopy(self.bridge.settings), self.bridge._games, self.bridge._installed,
                      self.bridge._installer_games, self.bridge._installer_packages)
        self.games = [dict(self.bridge._games[0], id=identity, name=name, genre=genre)
                      for identity, name, genre in [('a', 'Alpha', 'Action'), ('b', 'Beta', 'Puzzle'), ('c', 'Gamma', 'Action')]]
        self.bridge._games = self.games
        self.bridge._installed = [dict(card=dict(self.games[0], id='local-a'), catalog_id='a', played_at=1, available=True, kind='external'),
                                  dict(card=dict(self.games[1], id='local-b'), catalog_id='b', available=False, kind='external')]
        self.bridge.setHideInstalled(False)
        self.bridge.gamesChanged.emit(); self.bridge.libraryChanged.emit()
        self.value(self.window, 'filterStates = ({}); searchQueries = ({})')
        self.window.setProperty('section', 1)
        self.window.setProperty('focusZone', 0)
        self.pane = self.window.findChild(QObject, 'gameFiltersPane')
        QTest.qWait(100)

    def tearDown(self):
        QMetaObject.invokeMethod(self.pane, 'close')
        self.bridge.settings, self.bridge._games, self.bridge._installed, self.bridge._installer_games, self.bridge._installer_packages = self.saved
        self.bridge.setHideInstalled(self.bridge.settings.get('hide_installed', False))
        self.value(self.window, 'filterStates = ({}); searchQueries = ({})')
        self.bridge.gamesChanged.emit(); self.bridge.libraryChanged.emit(); self.bridge.installersChanged.emit(); self.bridge.settingsChanged.emit()
        self.bridge.save(); self.window.resize(1280, 800)
        QTest.qWait(100)

    def ids(self):
        return self.value(self.window, 'displayGames.map(game => game.id).join(",")')

    def apply(self, mode='all', genre=''):
        import json
        self.value(self.window, 'applyFilters(' + json.dumps(mode) + ',' + json.dumps(genre) + ')')
        QTest.qWait(50)

    def test_filters_combine_with_search_and_remain_separate_between_sections(self):
        self.apply('installed', 'Action'); self.assertEqual(self.ids(), 'a')
        self.assertEqual(self.window.property('selected')['id'], 'a')
        self.apply('missing'); self.assertEqual(self.ids(), 'c')
        self.assertTrue(self.bridge.hideInstalled)
        self.assertEqual(self.value(self.window, 'filterGenres.join(",")'), 'Action,Puzzle')
        self.apply('all', 'Puzzle'); self.assertEqual(self.ids(), 'b')
        self.assertFalse(self.bridge.hideInstalled)
        self.value(self.window, 'applySearch("Beta")'); self.apply('all', 'Action')
        self.assertEqual(self.ids(), '')
        self.value(self.window, 'applySearch("")'); self.apply(); self.assertEqual(self.ids(), 'a,b,c')
        self.window.setProperty('section', 0)
        self.apply('unplayed'); self.assertEqual(self.ids(), 'local-b')
        self.apply('unavailable'); self.assertEqual(self.ids(), 'local-b')
        self.apply('all', 'Action'); self.assertEqual(self.ids(), 'local-a')
        self.window.setProperty('section', 1); self.assertEqual(self.ids(), 'a,b,c')
        self.window.setProperty('section', 0); self.assertEqual(self.ids(), 'local-a')
        self.bridge._installer_games = self.games
        self.bridge._installer_packages = [dict(card=self.games[0])]
        self.bridge.installersChanged.emit(); self.window.setProperty('section', 3)
        self.apply('installed'); self.assertEqual(self.ids(), 'a')
        self.apply('missing', 'Action'); self.assertEqual(self.ids(), 'c')

    def test_added_game_clears_filters_to_reveal_its_card(self):
        self.window.setProperty('section', 0)
        self.apply('all', 'Action')
        self.assertEqual(self.ids(), 'local-a')
        self.value(self.window, 'selectAddedGame(0, "local-b")')
        QTest.qWait(150)
        self.assertEqual(self.window.property('selected')['id'], 'local-b')
        self.assertEqual(self.window.property('activeFilterCount'), 0)
        self.assertEqual(self.ids(), 'local-a,local-b')

    def test_panel_gamepad_touch_cancel_reset_and_layout_in_both_themes(self):
        button = self.window.findChild(QObject, 'installedFilter')
        genre = self.pane.findChild(QObject, 'gameFilterGenre')
        for theme in ('dark', 'light'):
            self.bridge.settings['theme'] = theme; self.bridge.settingsChanged.emit()
            for view in ('grid', 'ribbon'):
                self.bridge.setGamesView(view)
                self.window.setProperty('navigationIndex', 6)
                self.bridge.action.emit('activate'); QTest.qWait(180)
                self.assertTrue(self.pane.property('visible')); self.assertTrue(self.window.property('modalVisible'))
                self.bridge.action.emit('right'); self.bridge.action.emit('right'); self.bridge.action.emit('activate')
                self.assertEqual(self.pane.property('mode'), 'missing')
                self.assertEqual(self.pane.property('resultCount'), 1)
                self.assertEqual(self.ids(), 'a,b,c')  # Draft selection cannot change the library.
                self.bridge.action.emit('down'); self.bridge.action.emit('activate')
                self.assertTrue(self.value(genre, 'popup.visible'))
                self.bridge.action.emit('down'); self.bridge.action.emit('activate'); QTest.qWait(180)
                self.assertFalse(self.value(genre, 'popup.visible'))
                self.assertEqual(self.pane.property('genre'), 'Action')
                self.assertTrue(self.window.grabWindow().save(str(self.repo/f'reports/game-filters-20261025/{theme}-{view}-panel.png')))
                self.bridge.action.emit('down'); self.bridge.action.emit('activate'); QTest.qWait(160)
                self.assertFalse(self.pane.property('visible')); self.assertEqual(self.ids(), 'c')
                self.assertEqual(self.window.property('activeFilterCount'), 2)
                self.bridge.action.emit('activate'); QTest.qWait(160)
                self.bridge.action.emit('right'); self.bridge.action.emit('activate'); self.bridge.action.emit('back'); QTest.qWait(160)
                self.assertEqual(self.ids(), 'c')  # Cancel keeps the applied filters.
                device = QTest.createTouchDevice()
                point = button.mapToScene(QPointF(button.width()/2, button.height()/2)).toPoint()
                QTest.touchEvent(self.window,device).press(0,point,self.window).commit()
                QTest.touchEvent(self.window,device).release(0,point,self.window).commit(); QTest.qWait(160)
                self.assertTrue(self.pane.property('visible'))
                QMetaObject.invokeMethod(self.pane.findChild(QObject,'resetGameFilters'), 'clicked'); QTest.qWait(160)
                self.assertEqual(self.ids(), 'a,b,c'); self.assertEqual(self.window.property('activeFilterCount'), 0)
                for size in ((800,500),(1280,800),(1920,1200)):
                    self.window.resize(*size)
                    for scale in (1,1.2):
                        self.bridge.settings['ui_scale']=scale; self.bridge.settingsChanged.emit(); QTest.qWait(80)
                        self.window.grabWindow()
                        self.assertFalse(button.findChild(QObject,'gameFiltersCaption').property('truncated'))
                        add=self.window.findChild(QObject,'addLocalGameButton')
                        self.assertGreater(button.mapToScene(QPointF()).x(),add.mapToScene(QPointF(add.width(),0)).x())
                        self.assertLessEqual(button.mapToScene(QPointF(button.width(),0)).x(),self.window.width())
                self.window.resize(1280,800); self.bridge.settings['ui_scale']=1; self.bridge.settingsChanged.emit()
        self.assertFalse(any('TypeError' in m or 'ReferenceError' in m or 'Binding loop' in m for m in self.messages),self.messages)
