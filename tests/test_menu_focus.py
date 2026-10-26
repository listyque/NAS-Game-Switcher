import copy
import unittest
from PySide6.QtCore import QObject,QPointF,QPoint,Qt,QMetaObject
from PySide6.QtGui import QColor
from PySide6.QtQml import QQmlExpression,QQmlEngine
from PySide6.QtTest import QTest
from tests import test_ui


class MenuFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):
        cls.engine.bridge.stop()
        test_ui.UiTests.tearDownClass.__func__(cls)

    def value(self,item,code):
        expression=QQmlExpression(QQmlEngine.contextForObject(item),item,code)
        result,unused=expression.evaluate()
        self.assertFalse(expression.hasError(),expression.error().toString())
        return result

    def assert_white_frame(self,item):
        frames=[f for f in item.findChildren(QObject,'focusFrame') if f.property('selected') and f.isVisible()]
        self.assertTrue(frames,item.objectName())
        for frame in frames:
            self.assertEqual(self.value(frame,'border.color'),QColor('#ffffff'))
            self.assertGreaterEqual(self.value(frame,'border.width'),3)

    def test_menu_settings_and_dropdown_selection_have_white_frames_in_both_themes(self):
        bridge=self.engine.bridge;original=copy.deepcopy(bridge.settings)
        menu=self.window.findChild(QObject,'exitPopup');pane=self.window.findChild(QObject,'settingsPane')
        try:
            for theme in ('dark','light'):
                bridge.settings['theme']=theme;bridge.settingsChanged.emit()
                QMetaObject.invokeMethod(self.window,'openMenu');QTest.qWait(260)
                self.assert_white_frame(menu.findChild(QObject,'menuSettings'))
                bridge.action.emit('down');QTest.qWait(80)
                self.assert_white_frame(menu.findChild(QObject,'menuMinimize'))
                quit_button=menu.findChild(QObject,'menuQuit');quit_button.setProperty('hoverEnabled',True)
                QTest.mouseMove(self.window,quit_button.mapToScene(QPointF(quit_button.width()/2,quit_button.height()/2)).toPoint());QTest.qWait(80)
                self.assert_white_frame(quit_button)
                QMetaObject.invokeMethod(menu,'close');QTest.qWait(200)
                QMetaObject.invokeMethod(pane,'open');QTest.qWait(220)
                pane.setProperty('menuIndex',0);pane.setProperty('focusSide',0);QTest.qWait(100)
                self.assert_white_frame(self.value(pane.findChild(QObject,'settingsCategories'),'currentItem'))
                pane.setProperty('selectedField',20);QMetaObject.invokeMethod(pane,'revealSelected');QTest.qWait(200)
                self.assert_white_frame(pane.findChild(QObject,'settingsOverlayEnabled'))
                output=self.repo/f'reports/menu-focus-20261025/settings-{theme}.png';output.parent.mkdir(parents=True,exist_ok=True)
                self.assertTrue(self.window.grabWindow().save(str(output)))
                pane.setProperty('selectedField',12);QMetaObject.invokeMethod(pane,'revealSelected');QTest.qWait(200)
                bridge.action.emit('activate');QTest.qWait(180)
                choice=pane.findChild(QObject,'settingsAccent')
                if choice is None:choice=pane.findChild(QObject,'settingsAccentColor')
                self.assertIsNotNone(choice)
                frames=[f for f in pane.findChildren(QObject,'focusFrame') if f.property('selected') and f.isVisible()]
                self.assertGreaterEqual(len(frames),2)
                self.assert_white_frame(self.value(choice,'popup.contentItem.currentItem'))
                bridge.action.emit('back');QMetaObject.invokeMethod(pane,'close');QTest.qWait(200)
                QTest.mouseMove(self.window,QPoint(0,0))
            self.assertFalse(any('TypeError' in m or 'ReferenceError' in m or 'Binding loop' in m for m in self.messages),self.messages)
        finally:
            QMetaObject.invokeMethod(menu,'close');QMetaObject.invokeMethod(pane,'close')
            bridge.settings=original;bridge.settingsChanged.emit();bridge.save()

    def test_installed_label_is_gone_but_useful_statuses_remain_in_both_views(self):
        bridge=self.engine.bridge;installed=bridge._installed;view=bridge.gamesView
        try:
            game=bridge._games[0];receipt=dict(card=game,kind='external',played_at=1,available=True)
            bridge._installed=[receipt];bridge.libraryChanged.emit()
            self.window.setProperty('section',1);self.window.setProperty('selectedIndex',0)
            for mode in ('ribbon','grid'):
                bridge.setGamesView(mode);QTest.qWait(250)
                cards=self.window.findChild(QObject,'gameGrid' if mode=='grid' else 'gameCards')
                item=cards.property('currentItem');self.assertIsNotNone(item)
                status=item.findChild(QObject,'cardStatus-0')
                self.assertEqual(status.property('text'),'');self.assertFalse(status.parentItem().isVisible())
                receipt['available']=False;bridge.libraryChanged.emit();QTest.qWait(40)
                self.assertEqual(status.property('text'),'НЕДОСТУПНА');self.assertTrue(status.parentItem().isVisible())
                receipt.update(available=True,removing=True);bridge.libraryChanged.emit();QTest.qWait(40)
                self.assertEqual(status.property('text'),'УДАЛЕНИЕ…');self.assertTrue(status.parentItem().isVisible())
                receipt.update(removing=False,played_at=0);bridge.libraryChanged.emit();QTest.qWait(40)
                self.assertEqual(status.property('text'),'НОВАЯ');self.assertTrue(status.parentItem().isVisible())
                receipt['played_at']=1;bridge.libraryChanged.emit()
        finally:bridge._installed=installed;bridge.libraryChanged.emit();bridge.setGamesView(view)
