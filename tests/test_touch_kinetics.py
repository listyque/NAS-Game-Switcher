import unittest
from PySide6.QtCore import QObject,QPoint,QPointF,QMetaObject,Q_ARG
from PySide6.QtTest import QTest
from tests import test_ui


class TouchKineticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def prepare(self,mode):
        bridge=self.engine.bridge
        bridge.catalog_loaded([dict(bridge._games[0],id=f'kinetic-{i}',name=f'Game {i}') for i in range(200)])
        bridge.setGamesView(mode);self.window.setProperty('section',1);self.window.setProperty('focusZone',2)
        QTest.qWait(250)
        view=self.window.findChild(QObject,'gameGrid' if mode=='grid' else 'gameCards')
        QMetaObject.invokeMethod(view,'cancelFlick')
        axis='Y' if mode=='grid' else 'X';view.setProperty('content'+axis,600.)
        QTest.qWait(100)
        return view,axis

    def test_long_glide_stops_under_finger_without_selecting_a_card(self):
        for mode in ('grid','ribbon'):
            with self.subTest(mode=mode):
                view,axis=self.prepare(mode)
                try:
                    # Known launch velocity isolates deceleration from synthetic event timestamps.
                    self.assertTrue(QMetaObject.invokeMethod(view,'flick',Q_ARG(float,-1200. if axis=='X' else 0.),Q_ARG(float,-1200. if axis=='Y' else 0.)))
                    QTest.qWait(650)
                    self.assertGreater(view.property('content'+axis)-600,600)
                    self.assertTrue(view.property('flicking'))
                    selected=self.window.property('selectedIndex')
                    point=view.mapToScene(QPointF(view.width()*.6,view.height()*.6)).toPoint()
                    device=QTest.createTouchDevice()
                    QTest.touchEvent(self.window,device).press(0,point,self.window).commit();QTest.qWait(25)
                    stopped=view.property('content'+axis)
                    QTest.touchEvent(self.window,device).release(0,point,self.window).commit();QTest.qWait(180)
                    self.assertAlmostEqual(view.property('content'+axis),stopped,delta=2)
                    self.assertEqual(self.window.property('selectedIndex'),selected)
                    self.assertFalse(self.window.property('gameInProgress'))
                    self.engine.gamepad.action.emit('down' if axis=='Y' else 'right');QTest.qWait(350)
                    self.assertNotEqual(self.window.property('selectedIndex'),selected)
                    item=view.property('currentItem');position=item.mapToItem(view,QPointF(0,0))
                    coordinate=position.y() if axis=='Y' else position.x()
                    self.assertGreaterEqual(coordinate,-2)
                    self.assertLess(coordinate,view.height() if axis=='Y' else view.width())
                finally:QMetaObject.invokeMethod(view,'cancelFlick')

    def test_quick_finger_swipe_has_inertia_and_second_contact_can_reverse_it(self):
        for mode in ('grid','ribbon'):
            with self.subTest(mode=mode):
                view,axis=self.prepare(mode);device=QTest.createTouchDevice()
                start=view.mapToScene(QPointF(view.width()*.7,view.height()*.8)).toPoint();point=start
                try:
                    selected=self.window.property('selectedIndex')
                    QTest.touchEvent(self.window,device).press(0,start,self.window).commit();QTest.qWait(20)
                    for distance in range(40,241,40):
                        point=start-(QPoint(0,distance) if axis=='Y' else QPoint(distance,0))
                        QTest.touchEvent(self.window,device).move(0,point,self.window).commit();QTest.qWait(10)
                    QTest.touchEvent(self.window,device).release(0,point,self.window).commit();QTest.qWait(20)
                    released=view.property('content'+axis)
                    QTest.qWait(180)
                    self.assertGreater(view.property('content'+axis)-released,20)
                    self.assertTrue(view.property('flicking'))
                    start=view.mapToScene(QPointF(view.width()*.6,view.height()*.6)).toPoint()
                    QTest.touchEvent(self.window,device).press(0,start,self.window).commit();QTest.qWait(25)
                    stopped=view.property('content'+axis)
                    for distance in (20,40,60,80,100):
                        point=start+(QPoint(0,distance) if axis=='Y' else QPoint(distance,0))
                        QTest.touchEvent(self.window,device).move(0,point,self.window).commit();QTest.qWait(20)
                    self.assertLess(view.property('content'+axis),stopped-70)
                    QTest.touchEvent(self.window,device).release(0,point,self.window).commit();QTest.qWait(40)
                    self.assertEqual(self.window.property('selectedIndex'),selected)
                    self.assertFalse(self.window.property('gameInProgress'))
                finally:
                    QTest.touchEvent(self.window,device).release(0,point,self.window).commit()
                    QMetaObject.invokeMethod(view,'cancelFlick')
