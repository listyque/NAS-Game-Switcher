import unittest
from PySide6.QtCore import QObject, QPoint, QPointF, QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui


class GridTouchScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def test_route_trace_observes_grid_grab_and_frame_and_disconnects_on_close(self):
        from ngs.edge_swipe import EdgeWindows
        from ngs.input_host import validate_edge_trace
        window,bridge=self.window,self.engine.bridge
        old_games,old_view=bridge._games,bridge.gamesView
        edges=EdgeWindows(self.engine,bridge.status_overlay,self.repo)
        edges.attach_backing(window);records=[];edges.dispatch.traced.connect(records.append)
        grid=window.findChild(QObject,'gameGrid');device=QTest.createTouchDevice()
        try:
            bridge.catalog_loaded([dict(bridge._games[0],id=f'trace-{i}',name=f'Game {i}') for i in range(60)])
            bridge.setGamesView('grid');window.setProperty('section',1);QTest.qWait(250)
            point=grid.mapToScene(QPointF(grid.width()*.6,grid.height()*.7)).toPoint()
            self.assertGreater(point.y(),160)
            QTest.touchEvent(window,device).press(0,point,window).commit();QTest.qWait(20)
            for dy in (20,40,60,80):
                QTest.touchEvent(window,device).move(0,point-QPoint(0,dy),window).commit();QTest.qWait(20)
            window.grabWindow();QTest.qWait(60)
            QTest.touchEvent(window,device).release(0,point-QPoint(0,80),window).commit();QTest.qWait(60)
            for record in records: self.assertEqual(validate_edge_trace(record),record)
            self.assertTrue(any(r['stage']=='qt_begin' and r['y']>160 for r in records))
            stages={r['stage'] for r in records}
            self.assertTrue({'grid_drag','grid_end','grid_frame'}<=stages,stages)
            edges.close();before=len(records);grid.draggingChanged.emit();QTest.qWait(20)
            self.assertEqual(len(records),before);self.assertFalse(edges.scroll_links)
        finally:
            edges.close();QMetaObject.invokeMethod(grid,'cancelFlick')
            bridge.catalog_loaded(old_games);bridge.setGamesView(old_view)

    def test_slow_short_swipes_follow_finger_without_selecting_or_launching(self):
        window, bridge = self.window, self.engine.bridge
        original_games, original_scale, original_view = bridge._games, bridge.settings['ui_scale'], bridge.gamesView
        grid = window.findChild(QObject, 'gameGrid')
        device = QTest.createTouchDevice()
        try:
            bridge.catalog_loaded([dict(bridge._games[0], id=f'scroll-{i}', name=f'Game {i}') for i in range(60)])
            bridge.setGamesView('grid'); window.setProperty('section', 1)
            for scale in (.85, 1, 1.25):
                bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit(); QTest.qWait(250)
                for direction in (-1, 1):
                    with self.subTest(scale=scale, direction=direction):
                        QMetaObject.invokeMethod(grid, 'cancelFlick'); grid.setProperty('contentY', 400)
                        start = grid.mapToScene(QPointF(grid.width()*.6, grid.height()*.6)).toPoint()
                        origin = grid.mapFromScene(QPointF(start)).y()
                        selected = window.property('selectedIndex')
                        QTest.touchEvent(window, device).press(0, start, window).commit(); QTest.qWait(30)
                        grabbed = False
                        point = start
                        try:
                            for offset in range(4, 49, 4):
                                point = start + QPoint(0, direction*offset)
                                QTest.touchEvent(window, device).move(0, point, window).commit(); QTest.qWait(20)
                                if grid.property('dragging'):
                                    grabbed = True
                                    expected = 400 - (grid.mapFromScene(QPointF(point)).y() - origin)
                                    self.assertAlmostEqual(grid.property('contentY'), expected, delta=2)
                            self.assertTrue(grabbed)
                            QTest.qWait(80)
                        finally:
                            QTest.touchEvent(window, device).release(0, point, window).commit(); QTest.qWait(30)
                        self.assertEqual(window.property('selectedIndex'), selected)
                        self.assertFalse(window.property('gameInProgress'))
            QMetaObject.invokeMethod(grid, 'cancelFlick')
            # A completed tap must still select a tile, and controller navigation must reveal it.
            for poster in grid.property('contentItem').childItems():
                index = poster.property('index')
                if index is None: continue
                if index == window.property('selectedIndex') or not poster.isVisible(): continue
                center = poster.mapToScene(QPointF(poster.width()/2, poster.height()/2))
                local = grid.mapFromScene(center)
                if 20 < local.x() < grid.width()-20 and 20 < local.y() < grid.height()-20:
                    QTest.touchEvent(window, device).press(0, center.toPoint(), window).commit(); QTest.qWait(30)
                    QTest.touchEvent(window, device).release(0, center.toPoint(), window).commit(); QTest.qWait(300)
                    self.assertEqual(window.property('selectedIndex'), index)
                    self.engine.gamepad.action.emit('down'); QTest.qWait(300)
                    self.assertEqual(window.property('selectedIndex'), index+grid.property('columns'))
                    break
            else: self.fail('No visible tile available for tap check')
        finally:
            # Finish any contact left by a failed pre-fix assertion before tearing down Qt.
            if 'point' in locals():
                QTest.touchEvent(window, device).release(0, point, window).commit()
            QMetaObject.invokeMethod(grid, 'cancelFlick')
            bridge.settings['ui_scale'] = original_scale; bridge.settingsChanged.emit()
            bridge.catalog_loaded(original_games); bridge.setGamesView(original_view)


if __name__ == '__main__': unittest.main()
