import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl, QEvent, QMetaObject, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtQml import QQmlComponent
from PySide6.QtTest import QTest

from ngs.app import create_window, portable_check
from ngs.portable import PortableStorage


class UiTests(unittest.TestCase):
    def test_global_status_opens_with_a_single_touch_from_idle_handle(self):
        from unittest.mock import patch
        overlay = self.engine.bridge.status_overlay
        status = self.engine.rootObjects()[1]
        previous_enabled = overlay.edge_enabled
        overlay.edge_enabled = True
        component = QQmlComponent(self.engine,QUrl.fromLocalFile(str(self.repo/'qml/EdgeSwipeWindow.qml')))
        edge = component.createWithInitialProperties(dict(controller=overlay))
        self.assertIsNotNone(edge, component.errors())
        device = QTest.createTouchDevice()
        try:
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.last_request = 0
                QTest.qWait(2800)
                self.assertEqual(edge.height(), 32)
                x = round(edge.width() / 2)
                QTest.touchEvent(edge,device).press(0,QPoint(x,24),edge).commit(); QTest.qWait(20)
                QTest.touchEvent(edge,device).move(0,QPoint(x+2,124),edge).commit(); QTest.qWait(30)
                QTest.touchEvent(edge,device).release(0,QPoint(x+2,124),edge).commit()
                self.assertTrue(overlay.shown)
                self.assertTrue(status.isVisible())
        finally:
            edge.hide(); edge.deleteLater(); overlay.sleeping = True; overlay.changed.emit(); overlay.finishHide()
            overlay.sleeping = False; overlay.edge_enabled = previous_enabled
            overlay.changed.emit(); QTest.qWait(220)

    def test_global_status_animates_both_directions_and_finishes_before_game_restore(self):
        from unittest.mock import patch
        overlay = self.engine.bridge.status_overlay
        status = self.engine.rootObjects()[1]
        bar = status.findChild(QObject, 'globalStatusBar')
        closed = []
        callback = lambda: closed.append(status.isVisible())
        overlay.closed.connect(callback)
        try:
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.show(self.window.screen().name())
                QTest.qWait(45)
                self.assertTrue(status.isVisible())
                first = status.property('reveal')
                self.assertGreater(first, 0); self.assertLess(first, 1)
                first_y = bar.property('y')
                QTest.qWait(320)
                self.assertAlmostEqual(status.property('reveal'), 1)
                self.assertGreater(bar.property('y'), first_y)
                overlay.hide(); QTest.qWait(45)
                self.assertTrue(status.isVisible())
                self.assertGreater(status.property('reveal'), 0)
                self.assertLess(status.property('reveal'), 1)
                self.assertFalse(closed)
                for unused in range(150):
                    # finishHide is intentionally deferred until the window is hidden.
                    if not status.isVisible() and closed: break
                    QTest.qWait(10)
                self.assertFalse(status.isVisible(), (status.property('reveal'), status.property('presenting'), overlay.shown, overlay.closing))
                self.assertEqual(closed, [False])
        finally:
            overlay.closed.disconnect(callback); overlay.sleeping = True; overlay.changed.emit(); overlay.finishHide()
            overlay.sleeping = False; overlay.changed.emit(); QTest.qWait(30)

    def test_global_status_swipe_up_uses_one_finger_and_tracks_it(self):
        from unittest.mock import patch
        overlay = self.engine.bridge.status_overlay
        status = self.engine.rootObjects()[1]
        bar = status.findChild(QObject, 'globalStatusBar')
        device = QTest.createTouchDevice()
        try:
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.show(self.window.screen().name()); QTest.qWait(350)
                x = round(status.width() / 2)
                y = round(bar.property('y') + bar.property('height') - 7)
                QTest.touchEvent(status, device).press(0,QPoint(x,y),status).commit(); QTest.qWait(25)
                for distance in (15,35,55):
                    QTest.touchEvent(status, device).move(0,QPoint(x,y-distance),status).commit(); QTest.qWait(30)
                self.assertLess(status.property('pullOffset'), -24)
                QTest.touchEvent(status, device).release(0,QPoint(x,y-55),status).commit(); QTest.qWait(30)
                self.assertFalse(status.property('presenting'))
                for unused in range(150):
                    if not status.isVisible(): break
                    QTest.qWait(10)
                self.assertFalse(status.isVisible(), (status.property('reveal'), status.property('presenting'), overlay.shown, overlay.closing))
        finally:
            overlay.sleeping = True; overlay.changed.emit(); overlay.finishHide()
            overlay.sleeping = False; overlay.changed.emit(); QTest.qWait(30)

    def test_global_status_reopens_during_close_and_sleep_releases_window_immediately(self):
        from unittest.mock import patch
        overlay = self.engine.bridge.status_overlay
        status = self.engine.rootObjects()[1]
        closed = []
        callback = lambda: closed.append(True)
        overlay.closed.connect(callback)
        try:
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.show(self.window.screen().name()); QTest.qWait(320)
                overlay.hide(); QTest.qWait(35)
                overlay.show(self.window.screen().name()); QTest.qWait(320)
                self.assertTrue(status.isVisible())
                self.assertAlmostEqual(status.property('reveal'),1)
                self.assertFalse(closed)
                overlay.sleeping = True; overlay.changed.emit(); overlay.hide()
                self.assertFalse(status.isVisible())
                self.assertFalse(overlay.shown)
                self.assertEqual(closed,[True])
        finally:
            overlay.closed.disconnect(callback); overlay.sleeping = True; overlay.changed.emit(); overlay.finishHide()
            overlay.sleeping = False; overlay.changed.emit(); QTest.qWait(30)


    def test_radio_scan_shows_activity_with_cached_results_and_frees_hidden_spinner(self):
        from unittest.mock import patch
        from PySide6.QtCore import Q_ARG, Signal
        class Worker(QObject):
            loaded = Signal(dict)
            finished = Signal()
            def __init__(self, request, parent): super().__init__(parent); self.request = request
            def start(self): pass
            def wait(self): pass
        service = self.engine.bridge.system_controls
        panel = self.window.findChild(QObject, 'systemPanel')
        activity = panel.findChild(QObject, 'systemPanelActivity')
        loader = activity.findChild(QObject, 'activityLoader')
        status = panel.findChild(QObject, 'systemPanelStatus')
        previous_status = dict(self.engine.bridge.systemState)
        try:
            with patch('ngs.system_controls.ControlWorker', Worker), patch.object(self.engine.bridge.system_status, 'refresh'):
                self.engine.bridge.system_status.apply(dict(wifi='on'))
                service.request('bluetooth', 'read')
                service.workers['bluetooth'].loaded.emit(dict(radio_state='off'))
                service.workers['bluetooth'].finished.emit()
                QMetaObject.invokeMethod(panel, 'showPanel', Q_ARG('QVariant', 'wifi'))
                QTest.qWait(200)
                worker = service.workers['wifi']
                self.assertEqual(worker.request[1], 'scan')
                worker.loaded.emit(dict(items=[dict(id='one', name='Current network', connected=True, quality=80, secure=True)]))
                QTest.qWait(30)
                self.assertTrue(activity.property('visible'))
                self.assertIsNotNone(loader.property('item'))
                self.assertIn('Ищем сети', status.property('text'))
                self.assertIn('Найдено: 1', status.property('text'))
                QMetaObject.invokeMethod(panel, 'close'); QTest.qWait(220)
                self.assertIsNone(loader.property('item'), 'A background scan must not keep rendering')
                worker.loaded.emit(dict(items=[], message='Поиск завершён', error=''))
                worker.finished.emit()
                self.assertFalse(service.panel['busy'])
        finally:
            for key, worker in list(service.workers.items()):
                if isinstance(worker, Worker): worker.finished.emit()
            QMetaObject.invokeMethod(panel, 'close'); QTest.qWait(220)
            self.engine.bridge.system_status.apply(previous_status)

    def test_brightness_drag_applies_before_release_and_finishes_latest_value(self):
        import time
        from unittest.mock import patch
        from PySide6.QtCore import QMetaObject
        bridge, window = self.engine.bridge, self.window
        service = bridge.system_controls
        panel = window.findChild(QObject, 'systemPanel')
        slider = panel.findChild(QObject, 'systemLevel')
        writes = []
        def operate(key, action, value=None, password=''):
            if action == 'set':
                writes.append(value)
                time.sleep(.1)
            return dict(brightness=value if action == 'set' else 50)
        def settle():
            for unused in range(200):
                QTest.qWait(10)
                if not service.workers: return
            self.fail('System control worker did not finish')
        try:
            with patch('ngs.system_controls.operate', side_effect=operate):
                panel.setProperty('kind','brightness')
                QMetaObject.invokeMethod(panel,'open')
                service.request('brightness','read'); settle()
                self.assertTrue(slider.property('enabled'))
                slider.setProperty('pressed',True)
                for value in (55,60,75):
                    slider.setProperty('value',value)
                    QMetaObject.invokeMethod(slider,'moved'); QTest.qWait(25)
                for unused in range(20):
                    if writes: break
                    QTest.qWait(10)
                self.assertTrue(writes, 'The display must change while the finger is still down')
                self.assertTrue(slider.property('pressed'))
                self.assertTrue(slider.property('enabled'), 'A pending driver call must not disable the brightness slider')
                slider.setProperty('value',85); QMetaObject.invokeMethod(slider,'moved')
                slider.setProperty('pressed',False)
                QMetaObject.invokeMethod(panel,'close'); settle()
                self.assertEqual(writes[-1],85)
                self.assertEqual(service.states['brightness']['brightness'],85)
        finally:
            slider.setProperty('pressed',False)
            QMetaObject.invokeMethod(panel,'close')
            QTest.qWait(220)

    def test_transfer_cleanup_runs_in_background_and_respects_sleep_and_shutdown(self):
        from unittest.mock import patch
        bridge = self.engine.bridge
        previous = bridge._closing, bridge._sleeping
        try:
            with patch.object(bridge.download_queue, 'cleanup_due', return_value=True), \
                 patch.object(bridge.download_queue, 'clean_transfers') as clean:
                bridge.pumpCleanup()
                self.assertIsNotNone(bridge.cleanup_worker)
                for unused in range(100):
                    QTest.qWait(10)
                    if bridge.cleanup_worker is None: break
                self.assertIsNone(bridge.cleanup_worker)
                clean.assert_called_once()
                bridge._sleeping = True; bridge.pumpCleanup()
                bridge._sleeping = False; bridge._closing = True; bridge.pumpCleanup()
                clean.assert_called_once()
        finally:
            bridge._closing, bridge._sleeping = previous

    def test_fluid_feedback_press_pad_release_and_modal_motion_settle(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        original = copy.deepcopy(bridge.settings)
        pane = window.findChild(QObject, 'settingsPane')
        button = pane.findChild(QObject, 'settingsSave')
        try:
            bridge.action.emit('settings'); QTest.qWait(60)
            self.assertGreater(pane.property('scale'), .96)
            self.assertLess(pane.property('opacity'), 1)
            QTest.qWait(360)
            pane.setProperty('selectedField', 11); pane.setProperty('selectedField', 6); QTest.qWait(300)
            button.setProperty('down', True); QTest.qWait(35)
            self.assertGreater(button.property('compression'), .1)
            self.assertLess(button.property('compression'), 1)
            button.setProperty('down', False); QTest.qWait(480)
            self.assertAlmostEqual(button.property('compression'), 0, delta=.001)
            bridge.action.emit('activate')
            self.assertTrue(button.property('padPressed'))
            QTest.qWait(40)
            self.assertGreater(button.property('compression'), .2)
            QTest.qWait(520)
            self.assertFalse(button.property('padPressed'))
            self.assertAlmostEqual(button.property('compression'), 0, delta=.001)
            self.assertAlmostEqual(pane.property('scale'), 1, delta=.001)
            self.assertTrue(button.property('visible'))
            bridge.action.emit('menu'); QTest.qWait(50)
            self.assertTrue(pane.property('visible')); self.assertLess(pane.property('opacity'), 1)
            self.assertFalse(pane.property('enabled'))
            window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 5)
            bridge.action.emit('activate')
            menu = window.findChild(QObject, 'exitPopup')
            self.assertFalse(menu.property('visible'))
            self.assertTrue(pane.property('visible')); self.assertTrue(pane.property('enabled'))
            bridge.action.emit('back')
            bridge.action.emit('settings'); QTest.qWait(420)
            self.assertTrue(pane.property('enabled'))
            for width, height in ((1280, 720), (1920, 1080)):
                window.resize(width, height); bridge.settings['ui_scale'] = 1.2; bridge.settingsChanged.emit(); QTest.qWait(80)
                self.assertAlmostEqual(pane.property('y'), (height - pane.property('height')) / 2, delta=.5)
            bridge.action.emit('back'); QTest.qWait(180); self.assertFalse(pane.property('visible'))
        finally:
            pane.setProperty('visible', False); bridge.settings = original; bridge.settingsChanged.emit(); bridge.save()
            window.resize(1280, 800)
            window.setProperty('section', 1); window.setProperty('selectedIndex', 0); window.setProperty('focusZone', 2)
            QTest.qWait(350)

    def test_settings_slide_and_icons_survive_back_reversal_and_reopen(self):
        pane=self.window.findChild(QObject,'settingsPane')
        menu=pane.findChild(QObject,'settingsCategories');body=pane.findChild(QObject,'settingsDetails')
        motion=pane.findChild(QObject,'settingsNavigationMotion')
        def iconItem(name, item=None):
            item=item if item is not None else pane.property('contentItem')
            if item.objectName()==name:return item
            for child in item.childItems():
                found=iconItem(name,child)
                if found is not None:return found
        def settled():
            for _ in range(100):
                if not motion.property('running'):break
                QTest.qWait(10)
            self.assertFalse(motion.property('running'))
        try:
            self.engine.bridge.action.emit('settings');QTest.qWait(250)
            home=menu.x()
            pane.setProperty('menuIndex',1);self.engine.bridge.action.emit('activate')
            self.assertEqual(pane.property('page'),1)
            self.assertTrue(motion.property('running'))
            QTest.qWait(60)
            self.assertGreater(pane.property('navigationProgress'),0)
            self.assertLess(pane.property('navigationProgress'),1)
            self.assertLess(menu.x(),home)
            settled()
            icon=iconItem('settingsIcon-1')
            self.assertIsNotNone(icon)
            self.assertTrue(icon.property('visible'));self.assertGreater(icon.width(),20)
            self.assertEqual(body.property('opacity'),1)
            self.engine.bridge.action.emit('back')
            self.assertEqual(pane.property('page'),-1)
            self.assertEqual(pane.property('displayedPage'),1)
            self.assertTrue(body.property('visible'));self.assertFalse(body.property('enabled'))
            QTest.qWait(60)
            self.assertGreater(pane.property('navigationProgress'),0)
            self.assertLess(pane.property('navigationProgress'),1)
            pane.setProperty('menuIndex',4);self.engine.bridge.action.emit('activate');settled()
            self.assertEqual(pane.property('displayedPage'),4)
            self.assertEqual(pane.property('page'),4)
            self.engine.bridge.action.emit('back');settled()
            self.assertEqual(pane.property('displayedPage'),-1)
            self.assertFalse(body.property('visible'));self.assertAlmostEqual(menu.x(),home)
            pane.setProperty('menuIndex',2);self.engine.bridge.action.emit('activate')
            QTest.qWait(30);QMetaObject.invokeMethod(pane,'close')
            self.assertFalse(motion.property('running'))
            QTest.qWait(200);self.engine.bridge.action.emit('settings');QTest.qWait(220)
            self.assertEqual(pane.property('navigationProgress'),0)
            self.assertEqual(pane.property('displayedPage'),-1)
            self.assertTrue(iconItem('settingsIcon-0').property('visible'))
        finally:
            QMetaObject.invokeMethod(pane,'close');QTest.qWait(200)

    def test_ps5_settings_sections_navigation_back_and_all_fields_fit(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge.settings)
        pane = window.findChild(QObject, 'settingsPane')
        try:
            bridge.action.emit('settings'); QTest.qWait(280)
            self.assertEqual(pane.property('page'), -1)
            self.assertEqual((pane.property('width'), pane.property('height')), (window.width(), window.height()))
            fields = pane.property('fields').toVariant()
            reached = set()
            for section in range(6):
                pane.setProperty('menuIndex', section)
                bridge.action.emit('activate')
                self.assertEqual(pane.property('page'), section)
                navigation=pane.findChild(QObject,'settingsNavigationMotion')
                for _ in range(100):
                    if not navigation.property('running'):break
                    QTest.qWait(10)
                self.assertFalse(navigation.property('running'))
                enabled = {i for i, field in enumerate(fields) if field.property('visible') and field.property('enabled')}
                visited = {pane.property('selectedField')}
                for unused in range(len(fields)):
                    bridge.action.emit('down'); visited.add(pane.property('selectedField'))
                self.assertEqual(visited, enabled)
                reached.update(visited)
                for size in ((800, 500), (1280, 720), (1920, 1080)):
                    window.resize(*size)
                    for scale in (1, 1.2):
                        bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit(); QTest.qWait(50)
                        for index in enabled:
                            pane.setProperty('selectedField', index); QMetaObject.invokeMethod(pane, 'revealSelected')
                            field = fields[index]
                            start = field.mapToScene(QPointF()); end = field.mapToScene(QPointF(field.width(), field.height()))
                            self.assertGreaterEqual(start.x(), 0); self.assertGreaterEqual(start.y(), 0)
                            self.assertLessEqual(end.x(), window.width()); self.assertLessEqual(end.y(), window.height())
                            if index not in (6, 7): self.assertLess(end.y(), fields[6].mapToScene(QPointF()).y(), (size, scale, index))
                bridge.action.emit('back'); self.assertEqual(pane.property('focusSide'), 0)
                self.assertEqual(pane.property('page'), -1)
            self.assertEqual(reached, set(range(len(fields))) - {5, 15, 16, 17})
            pane.setProperty('selectedField', 11); bridge.action.emit('activate')
            original = pane.findChild(QObject, 'settingsTheme').property('currentIndex')
            bridge.action.emit('down'); bridge.action.emit('back')
            self.assertEqual(pane.findChild(QObject, 'settingsTheme').property('currentIndex'), original)
            self.assertEqual(pane.property('page'), 1)
            bridge.action.emit('back'); bridge.action.emit('back'); QTest.qWait(180)
            self.assertFalse(pane.property('visible'))
        finally:
            pane.setProperty('visible', False); bridge.settings = previous; bridge.settingsChanged.emit(); bridge.save()
            window.resize(1280, 800); QTest.qWait(200)

    def test_header_badges_follow_download_states_and_available_update(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge._update), window.property('section'), window.property('focusZone')
        badges = [window.findChild(QObject, name) for name in ('activeDownloadsBadge', 'failedDownloadsBadge', 'updateAvailableBadge')]
        identities = []
        try:
            window.showNormal(); window.setProperty('section', 1); window.setProperty('focusZone', 2)
            image = dict(path='/assets/ngs_launcher/test.jpg', size=1, sha256='0' * 64)
            for index, state in enumerate(('queued', 'running', 'error', 'paused')):
                card = dict(id='header-badge-' + str(index), name='Badge game', subtitle='', description='', genre='',
                            accent='#d9ff82', source_folder='Game', executable='play.exe', metadata_url='', revision=1,
                            cover=image, background=image, delivery='on_request', total_size=1)
                bridge.download_queue.add(card, self.root / 'offline-badge-source', bridge.settings['tactic_url'])
                identities.append(card['id'])
                bridge.download_queue.set_state(card['id'], state)
            bridge.downloadsChanged.emit()
            bridge._update.update(available=True, version='2099.01.01.000000'); bridge.updatesChanged.emit()
            QTest.qWait(100)
            self.assertEqual([badge.property('count') for badge in badges], [2, 1, 1])
            self.assertTrue(all(badge.property('visible') for badge in badges))
            self.assertTrue(badges[1].property('error'))
            window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 2)
            self.assertIn('активных: 2', window.property('selectionHelp'))
            self.assertIn('с ошибками: 1', window.property('selectionHelp'))
            window.setProperty('navigationIndex', 5)
            self.assertIn('доступно обновление', window.property('selectionHelp'))
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/downloads-header-badges.png')))
            bridge.download_queue.set_state(identities[0], 'paused')
            bridge.download_queue.set_state(identities[1], 'error')
            bridge.download_queue.set_state(identities[2], 'paused')
            bridge.downloadsChanged.emit(); QTest.qWait(40)
            self.assertFalse(badges[0].property('visible'))
            self.assertEqual(badges[1].property('count'), 1)
            for identity in identities: bridge.download_queue.forget_row(identity)
            bridge.downloadsChanged.emit()
            bridge._update['available'] = False; bridge.updatesChanged.emit(); QTest.qWait(40)
            self.assertFalse(any(badge.property('visible') for badge in badges))
        finally:
            for identity in identities: bridge.download_queue.forget_row(identity)
            bridge._update = previous[0]; bridge.updatesChanged.emit(); bridge.downloadsChanged.emit()
            window.setProperty('section', previous[1]); window.setProperty('focusZone', previous[2])
            QTest.qWait(100)

    def test_downloads_is_service_control_and_header_has_top_inset(self):
        bridge, window = self.engine.bridge, self.window
        previous = (window.property('section'), window.property('focusZone'),
                    window.property('navigationIndex'), bridge.settings['ui_scale'])
        navigation = window.findChild(QObject, 'headerNavigation')
        utility = window.findChild(QObject, 'utilityNavigation')
        button = window.findChild(QObject, 'downloadsButton')
        pill = window.findChild(QObject, 'navigationPill')
        try:
            window.showNormal()
            self.assertEqual(button.parentItem(), utility)
            tabs = [item for item in navigation.childItems() if item.property('targetSection') is not None]
            self.assertEqual([tab.property('text') for tab in tabs], ['Мои игры', 'Каталог', 'Установщики'])
            for width, height in ((800, 500), (1280, 800), (1920, 1080)):
                window.resize(width, height)
                for scale in (.8, 1, 1.2):
                    bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit()
                    window.setProperty('section', 1); window.setProperty('focusZone', 2)
                    QTest.qWait(80)
                    self.assertGreaterEqual(utility.mapToScene(QPointF()).y(), 8 * window.property('uiScale') - .01)
                    self.assertLess(button.mapToScene(QPointF(0, button.height())).y(), navigation.mapToScene(QPointF()).y())
                    self.assertGreater(button.mapToScene(QPointF()).x(), 0)
                    self.assertLess(button.mapToScene(QPointF(button.width(), button.height())).x(), width)
                    self.assertLess(tabs[-1].mapToScene(QPointF(tabs[-1].width(), tabs[-1].height())).x(), width)
            window.resize(1280, 800); bridge.settings['ui_scale'] = 1; bridge.settingsChanged.emit()
            window.setProperty('section', 1); window.setProperty('focusZone', 2)
            for action, expected in (('next_section', 3), ('next_section', 0), ('next_section', 1),
                                     ('previous_section', 0), ('previous_section', 3), ('previous_section', 1)):
                bridge.action.emit(action)
                self.assertEqual(window.property('section'), expected)
            QTest.qWait(150)
            point = button.mapToScene(QPointF(button.width()/2, button.height()/2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point); QTest.qWait(360)
            self.assertEqual(window.property('section'), 2)
            self.assertFalse(pill.property('visible'))
            self.assertTrue(window.findChild(QObject, 'downloadsPage').property('enabled'))
            window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 2)
            self.assertIn('Загрузки', window.findChild(QObject, 'selectionHelp').property('text'))
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/downloads-service-header.png')))
            bridge.action.emit('next_section')
            self.assertEqual(window.property('section'), 3)
            self.assertTrue(pill.property('visible'))
            window.setProperty('navigationIndex', 2); window.setProperty('focusZone', 0)
            bridge.action.emit('activate')
            self.assertEqual(window.property('section'), 2)
            bridge.action.emit('previous_section')
            self.assertEqual(window.property('section'), 1)
        finally:
            window.setProperty('section', previous[0]); window.setProperty('focusZone', previous[1])
            window.setProperty('navigationIndex', previous[2]); bridge.settings['ui_scale'] = previous[3]
            bridge.settingsChanged.emit(); window.resize(1280, 800); QTest.qWait(120)

    def test_theme_accents_settings_controller_and_smooth_grid_rows(self):
        import copy
        from unittest.mock import patch
        from ngs.appearance import ACCENTS
        from PySide6.QtGui import QColor
        bridge, window = self.engine.bridge, self.window
        original = copy.deepcopy(bridge.settings), bridge._games
        pane = window.findChild(QObject, 'settingsPane')
        grid = window.findChild(QObject, 'gameGrid')
        try:
            saved = self.storage.read_settings()
            self.assertFalse(bridge.setAppearance('unknown', 'blue'))
            self.assertEqual(self.storage.read_settings(), saved)
            with patch.object(self.storage, 'write_settings', side_effect=OSError('read only')):
                self.assertFalse(bridge.setAppearance('light', 'blue'))
                self.assertEqual(bridge.settings, original[0])
            games = [dict(bridge._games[0], id='animation-' + str(i), name='Game ' + str(i)) for i in range(60)]
            bridge.catalog_loaded(games)
            bridge.settings['games_view'] = 'grid'; bridge.settingsChanged.emit()
            window.resize(1280, 800); window.setProperty('section', 1); window.setProperty('selectedIndex', 0)
            window.setProperty('focusZone', 2); QTest.qWait(320)
            start = grid.property('contentY')
            window.setProperty('selectedIndex', grid.property('columns') * 3)
            QTest.qWait(55)
            middle = grid.property('contentY')
            animation = window.findChild(QObject, 'gridScrollAnimation')
            self.assertTrue(animation.property('running'))
            self.assertGreater(middle, start)
            self.assertLess(middle, animation.property('to'))
            QTest.qWait(270)
            self.assertAlmostEqual(grid.property('contentY'), animation.property('to'), delta=1)
            window.setProperty('selectedIndex', grid.property('columns') * 6); QTest.qWait(55)
            target = animation.property('to')
            window.setProperty('section', 2); window.setProperty('section', 1); QTest.qWait(100)
            self.assertAlmostEqual(grid.property('contentY'), target, delta=1)
            bridge.action.emit('settings'); QTest.qWait(350)
            pane.setProperty('selectedField', 11)
            bridge.action.emit('activate'); bridge.action.emit('down'); bridge.action.emit('activate')
            self.assertEqual(bridge.settingsState['theme'], 'light')
            pane.setProperty('selectedField', 12); bridge.action.emit('activate'); bridge.action.emit('down'); bridge.action.emit('activate')
            self.assertEqual(bridge.settingsState['accent_color'], 'blue')
            for theme in ('light', 'dark'):
                for accent in ACCENTS:
                    self.assertTrue(bridge.setAppearance(theme, accent))
                    QTest.qWait(30)
                    self.assertEqual(window.color(), QColor(bridge.themeColors['background']))
                    self.assertEqual(self.app.palette().color(self.app.palette().ColorRole.Highlight), QColor(bridge.themeColors['accent']))
                QTest.qWait(380)
                self.assertTrue(window.grabWindow().save(str(self.repo / ('reports/theme-' + theme + '-settings.png'))))
            bridge.action.emit('back'); QTest.qWait(200)
            for theme in ('light', 'dark'):
                bridge.setAppearance(theme, 'blue'); QTest.qWait(600)
                self.assertTrue(window.grabWindow().save(str(self.repo / ('reports/theme-' + theme + '-grid.png'))))
            self.assertEqual(self.storage.read_settings()['theme'], 'dark')
            self.assertEqual(self.storage.read_settings()['accent_color'], 'blue')
        finally:
            pane.setProperty('visible', False)
            bridge.settings, bridge._games = original
            bridge.settingsChanged.emit(); bridge.gamesChanged.emit(); bridge.save()
            window.setProperty('section', 1); window.setProperty('selectedIndex', 0); QTest.qWait(300)

    def test_game_list_wraps_at_edges_and_scrolls_to_selection(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        previous = bridge._games, bridge._installed, bridge._installer_games, copy.deepcopy(bridge.settings)
        queries = window.property("searchQueries")
        if not isinstance(queries, dict): queries = queries.toVariant()
        section, index = window.property("section"), window.property("selectedIndex")
        try:
            bridge.setHideInstalled(False)
            for count in (15, 1, 0):
                games = [dict(previous[0][0], id=f"wrap-{i}", name=f"Game {i}") for i in range(count)]
                bridge._installed = [dict(card=game, kind="external") for game in games]
                bridge._installer_games = games
                bridge.catalog_loaded(games); bridge.libraryChanged.emit(); bridge.installersChanged.emit()
                for view in ("ribbon", "grid"):
                    bridge.settings["games_view"] = view; bridge.settingsChanged.emit()
                    for target in (0, 1, 3):
                        window.setProperty("searchQueries", {})
                        window.setProperty("section", target); window.setProperty("focusZone", 2)
                        window.setProperty("selectedIndex", max(0, count - 1))
                        bridge.action.emit("right"); QTest.qWait(40)
                        self.assertEqual(window.property("selectedIndex"), 0)
                        cards = window.findChild(QObject, "gameGrid" if view == "grid" else "gameCards")
                        if count:
                            item = cards.property("currentItem")
                            self.assertIsNotNone(item)
                            position = item.y() if view == "grid" else item.x()
                            self.assertGreaterEqual(position, cards.property("contentY" if view == "grid" else "contentX") - 1)
                        bridge.action.emit("left"); QTest.qWait(40)
                        self.assertEqual(window.property("selectedIndex"), max(0, count - 1))
                        if view == "grid" and count:
                            bridge.action.emit("down")
                            self.assertEqual(window.property("selectedIndex"), (count - 1) % cards.property("columns"))
                            bridge.action.emit("up")
                            self.assertEqual(window.property("focusZone"), 0)
                        if count > 1:
                            window.setProperty("searchQueries", {str(target): "Game 1"})
                            window.setProperty("focusZone", 2)
                            last = len(window.property("displayGames").toVariant()) - 1
                            window.setProperty("selectedIndex", last)
                            bridge.action.emit("right")
                            self.assertEqual(window.property("selectedIndex"), 0)
                            bridge.action.emit("left")
                            self.assertEqual(window.property("selectedIndex"), last)
        finally:
            bridge._games, bridge._installed, bridge._installer_games, bridge.settings = previous
            bridge.gamesChanged.emit(); bridge.libraryChanged.emit(); bridge.installersChanged.emit(); bridge.settingsChanged.emit()
            window.setProperty("searchQueries", queries)
            window.setProperty("section", section); window.setProperty("selectedIndex", index)
            QTest.qWait(100)

    def test_keyboard_three_bounded_sizes_touchpads_and_visual_pad_editor(self):
        import copy
        from unittest.mock import Mock
        from PySide6.QtCore import QRect, QMetaObject
        bridge, window = self.engine.bridge, self.window
        keyboard = bridge.keyboard
        previous = copy.deepcopy(bridge.settings), keyboard.backend, keyboard.screen
        keys = window.findChild(QWindow, 'virtualKeyboardWindow')
        left = window.findChild(QWindow, 'leftTouchpadWindow')
        right = window.findChild(QWindow, 'rightTouchpadWindow')
        editor = window.findChild(QObject, 'padKeyboardEditor')
        try:
            keyboard.backend = Mock()
            keyboard.backend.foreground.return_value = 42
            keyboard.backend.physical_down.return_value = False
            screen = Mock(); screen.geometry.return_value = QRect(-1280, 30, 1280, 800); screen.devicePixelRatio.return_value = 2
            keyboard.set_screen(screen); keyboard.setTouchpads(False); keyboard.show()
            QTest.qWait(300)
            heights = []
            keyboard.setView('touch')
            for size in range(3):
                keyboard.setSize(size); QTest.qWait(60)
                heights.append(keys.height())
                self.assertEqual(keys.width(), 1280); self.assertEqual(keys.x(), -1280)
                self.assertEqual(keys.y() + keys.height(), 830)
                rows = keys.property('rows').toVariant()
                columns = max(map(len, rows))
                self.assertEqual(len(rows), 4)
                self.assertTrue(all(0 < len(row) <= columns for row in rows), rows)
                self.assertEqual(len([key for row in rows for key in row]), len(set(key for row in rows for key in row)))
                def visual(item, name):
                    if item.objectName() == name: return item
                    for child in item.childItems():
                        found = visual(child, name)
                        if found is not None: return found
                first = visual(keys.contentItem(), 'keyboardKey-0-0')
                last = visual(keys.contentItem(), 'keyboardKey-0-' + str(len(rows[0]) - 1))
                self.assertGreater(first.width(), 35)
                self.assertAlmostEqual(keys.height() / 800, (.36, .44, .52)[size], delta=.01)
                self.assertAlmostEqual(first.mapToScene(QPointF()).x(), keys.width() - last.mapToScene(QPointF(last.width(), 0)).x(), delta=1)
                self.assertTrue(keys.grabWindow().save(str(self.repo / ('reports/keyboard-size-%d.png' % size))))
            self.assertLess(heights[0], heights[1]); self.assertLess(heights[1], heights[2])
            opacity = keys.findChild(QObject, 'keyboardOpacitySlider')
            QMetaObject.invokeMethod(keys, 'toggleMenu'); QTest.qWait(60)
            keyboard_settings = keys.findChild(QWindow, 'keyboardSettingsWindow')
            point = opacity.mapToScene(QPointF(opacity.width() * .4, opacity.height()/2)).toPoint()
            QTest.mousePress(keyboard_settings, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(keyboard_settings, point - QPoint(12, 0)); QTest.qWait(20)
            self.assertLess(keys.property('contentOpacity'), .9)
            self.assertEqual(keys.opacity(), 1)
            QTest.mouseRelease(keyboard_settings, Qt.MouseButton.LeftButton, pos=point - QPoint(12, 0))
            self.assertLess(keyboard.options['keyboard_opacity'], .9)
            self.assertEqual(self.storage.read_settings()['keyboard_options']['views'][keyboard.options['view']]['keyboard_opacity'], keyboard.options['keyboard_opacity'])
            keyboard.setKeyboardOpacity(1)
            self.assertIsNone(keys.findChild(QObject, 'macroRow'))
            keyboard.setTouchpads(True); QTest.qWait(60)
            self.assertFalse(keys.isVisible()); self.assertTrue(left.isVisible()); self.assertTrue(right.isVisible())
            self.assertEqual(left.x(), -1280); self.assertEqual(right.x() + right.width(), 0)
            self.assertEqual(left.y() + left.height(), 830)
            self.assertTrue(left.flags() & Qt.WindowType.WindowDoesNotAcceptFocus)
            device = QTest.createTouchDevice()
            QTest.touchEvent(left, device).press(0, QPoint(100, 150), left).commit()
            QTest.touchEvent(left, device).move(0, QPoint(120, 160), left).commit()
            QTest.touchEvent(left, device).release(0, QPoint(120, 160), left).commit()
            QTest.qWait(30)
            keyboard.backend.mouse.assert_called_with(40, 20, '')
            keyboard.backend.send.assert_not_called()
            bridge.action.emit('back')  # Main input is independent of a touch overlay.
            keyboard.action.emit('back'); QTest.qWait(40)
            self.assertTrue(left.isVisible()); self.assertTrue(right.isVisible())
            keyboard.padTool.emit(True, 2)
            self.assertFalse(left.isVisible()); self.assertFalse(right.isVisible())
            window.findChild(QObject, 'exitPopup').setProperty('visible', False)
            keyboard.editPad(); QTest.qWait(100)
            self.assertTrue(editor.property('visible'))
            diagram = window.findChild(QObject, 'padKeyboardDiagram')
            self.assertIsNotNone(diagram)
            editor.setProperty('selectedField', 0)
            editor.setProperty('padIndex', next(i for i, row in enumerate(keyboard.padRows) if row['button'] == 0x1000))
            self.engine.gamepad.action.emit('right')
            self.assertEqual(keyboard.padRows[editor.property('padIndex')]['button'], 0x2000)
            self.assertEqual(editor.property('selectedField'), 0)
            a_button = visual(window.contentItem(), 'padDiagram-4096')
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=a_button.mapToScene(QPointF(a_button.width()/2, a_button.height()/2)).toPoint())
            self.assertEqual(keyboard.padRows[editor.property('padIndex')]['button'], 0x1000)
            self.assertEqual(editor.property('selectedField'), 1)
            rows = keyboard.padRows; pad_index = next(i for i, row in enumerate(rows) if row['button'] == 0x1000)
            editor.setProperty('padIndex', pad_index); editor.setProperty('keyName', 'C'); editor.setProperty('modifiers', ['Ctrl'])
            editor.setProperty('selectedField', 6)
            self.engine.gamepad.action.emit('activate')
            self.assertEqual(self.storage.read_settings()['keyboard_pad']['4096'], 'Ctrl+C')
            editor.setProperty('selectedField', 10); self.engine.gamepad.action.emit('activate'); QTest.qWait(50)
            self.assertTrue(keys.isVisible())
            keyboard.press('Shift'); keyboard.press('S'); QTest.qWait(50)
            self.assertEqual(editor.property('assignment'), 'Shift+S')
            keyboard.backend.send.assert_not_called()
            editor.setProperty('selectedField', 6); self.engine.gamepad.action.emit('activate')
            editor.setProperty('selectedField', 8); self.engine.gamepad.action.emit('activate')
            self.assertTrue(keyboard.padEnabled)
            self.assertTrue(self.engine.gamepad.background)
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/pad-keyboard-editor.png')))
            self.engine.gamepad.action.emit('back'); QTest.qWait(180)
            self.assertFalse(editor.property('visible'))
            self.assertTrue(keyboard.padEnabled)
        finally:
            keyboard.setPadEnabled(False); keyboard.hide(); keyboard.setTouchpads(False)
            editor.setProperty('visible', False)
            bridge.settings, keyboard.backend = previous[:2]; bridge.settingsChanged.emit(); keyboard.changed.emit()
            keyboard.set_screen(previous[2]); bridge.save()
            keys.setProperty('symbols', False)
            window.setProperty('focusZone', 2)

    def test_added_game_is_selected_and_revealed_despite_search_and_sort(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        original, installed, installers = bridge._games, bridge._installed, bridge._installer_games
        settings, queries = copy.deepcopy(bridge.settings), window.property("searchQueries")
        if not isinstance(queries, dict): queries = queries.toVariant()
        section, index = window.property("section"), window.property("selectedIndex")
        try:
            games = [dict(original[0], id=f"added-{i}", name=f"Game {i}") for i in range(40)]
            bridge._installed = [dict(card=card, kind="external") for card in games]
            bridge._installer_games = games
            bridge.catalog_loaded(games)
            bridge.libraryChanged.emit(); bridge.installersChanged.emit()
            for view in ("ribbon", "grid"):
                bridge.settings["games_view"] = view; bridge.settingsChanged.emit()
                for target in (0, 1, 3):
                    window.setProperty("searchQueries", {str(target): "does not exist", "2": "keep"})
                    window.setProperty("section", 2)
                    bridge.gameAdded.emit(target, "added-39")
                    QTest.qWait(300)
                    self.assertEqual(window.property("section"), target)
                    self.assertEqual(window.property("selected")["id"], "added-39")
                    self.assertEqual(window.property("searchQuery"), "")
                    self.assertEqual(bridge.sectionUi(target)["game_id"], "added-39")
                    self.assertEqual(window.property("searchQueries").toVariant()["2"], "keep")
                    cards = window.findChild(QObject, "gameGrid" if view == "grid" else "gameCards")
                    item = cards.property("currentItem")
                    self.assertIsNotNone(item)
                    self.assertGreaterEqual(item.y() if view == "grid" else item.x(), cards.property("contentY" if view == "grid" else "contentX") - 1)
                    self.assertLessEqual((item.y() + item.height()) if view == "grid" else (item.x() + item.width()),
                                         cards.property("contentY" if view == "grid" else "contentX") + (cards.height() if view == "grid" else cards.width()) + 1)
                    # A normal refresh preserves subsequent user selection instead of selecting the added game again.
                    window.setProperty("selectedIndex", 4)
                    bridge.libraryChanged.emit(); bridge.gamesChanged.emit(); bridge.installersChanged.emit()
                    self.assertEqual(window.property("selected")["id"], "added-4")
            bridge.gameAdded.emit(0, "missing-id")
            self.assertEqual(window.property("section"), 3)
        finally:
            bridge._installed, bridge._installer_games = installed, installers
            bridge.settings = settings
            bridge.catalog_loaded(original)
            bridge.libraryChanged.emit(); bridge.installersChanged.emit(); bridge.settingsChanged.emit()
            window.setProperty("searchQueries", queries)
            window.setProperty("section", section); window.setProperty("selectedIndex", index)
            QTest.qWait(200)

    def test_catalogizer_selects_published_id_after_refresh_including_filtered_bulk_result(self):
        import copy
        from unittest.mock import Mock, patch
        from ngs.app import CatalogWorker
        bridge, window = self.engine.bridge, self.window
        games, installed, installers = bridge._games, bridge._installed, bridge._installer_games
        settings = copy.deepcopy(bridge.settings)
        states = copy.deepcopy(bridge.catalogizer._state), copy.deepcopy(bridge.installer_catalogizer._state)
        pending = bridge.catalogizer.pending, bridge.installer_catalogizer.pending
        section, index = window.property("section"), window.property("selectedIndex")
        try:
            card = dict(games[0], id="published-selection")
            bridge._installed = [dict(card=card, kind="external")]
            bridge.setHideInstalled(True)
            with patch.object(bridge, "refreshCatalog") as refresh, patch.object(bridge, "refreshInstallers"):
                # Publication while an older feed request is still in flight must request a fresh feed once.
                bridge.worker = Mock(cache=bridge.cache)
                bridge.catalogizer.pending = self.storage.state / "selection-preview.json"
                bridge.catalogizer._state.update(stage="batch", selected_id="old-selection")
                bridge.catalogizer.loaded(dict(stage="done", state="done", selected_id=card["id"], rows=[], summary=dict(added=1, already=0, manual=0, error=0)))
                refresh.assert_called_once()
                bridge.catalog_loaded(games)
                self.assertIn(1, bridge._published_selection)
                bridge.catalog_finished()
                self.assertEqual(refresh.call_count, 2)
                bridge.catalog_loaded([games[0], card])
                self.assertEqual(window.property("section"), 1)
                self.assertEqual(window.property("selected")["id"], card["id"])
                self.assertFalse(bridge.hideInstalled)
                self.assertNotIn(1, bridge._published_selection)
                bridge.installer_catalogizer.pending = self.storage.state / "selection-installer-preview.json"
                bridge.installer_catalogizer._state.update(stage="working", selected_id="old-selection")
                bridge.installer_catalogizer.loaded(dict(stage="done", state="done", card=card))
                sender = CatalogWorker(bridge.installer_cache, bridge)
                sender.loaded.connect(bridge.installers_loaded)
                sender.loaded.emit([games[0], card]); sender.deleteLater()
                self.assertEqual(window.property("section"), 3)
                self.assertEqual(window.property("selected")["id"], card["id"])
                self.assertNotIn(3, bridge._published_selection)
        finally:
            bridge.worker = None
            bridge._published_selection.clear(); bridge._published_refresh.clear()
            bridge.catalogizer._state, bridge.installer_catalogizer._state = states
            bridge.catalogizer.pending, bridge.installer_catalogizer.pending = pending
            bridge._installed, bridge._installer_games = installed, installers
            bridge.settings = settings
            bridge.catalog_loaded(games)
            bridge.libraryChanged.emit(); bridge.installersChanged.emit(); bridge.settingsChanged.emit()
            window.setProperty("section", section); window.setProperty("selectedIndex", index)
            QTest.qWait(200)

    def test_shoulders_switch_sections_triggers_focus_one_utility_row(self):
        bridge, window = self.engine.bridge, self.window
        previous = window.property('section'), window.property('focusZone'), bridge._input_device, bridge.settings['ui_scale']
        try:
            bridge.set_input_device('gamepad')
            window.setProperty('section', 1); window.setProperty('focusZone', 2)
            selected = window.property('selectedIndex')
            for index in range(6):
                self.engine.gamepad.action.emit('next_control')
                self.assertEqual(window.property('focusZone'), -1)
                self.assertEqual(window.property('statusIndex'), index)
                self.assertEqual(window.property('section'), 1)
                self.assertEqual(window.property('selectedIndex'), selected)
            for control in (9, 10, 2, 4, 5):
                self.engine.gamepad.action.emit('next_control')
                self.assertEqual(window.property('focusZone'), 0)
                self.assertEqual(window.property('navigationIndex'), control)
            self.engine.gamepad.action.emit('next_control')
            self.assertEqual(window.property('focusZone'), -1)
            self.assertEqual(window.property('statusIndex'), 0)
            self.engine.gamepad.action.emit('previous_control')
            self.assertEqual(window.property('navigationIndex'), 5)
            menu = window.findChild(QObject, 'exitPopup')
            self.assertFalse(menu.property('visible'))
            self.engine.gamepad.action.emit('activate'); QTest.qWait(120)
            self.assertTrue(menu.property('visible'))
            self.engine.gamepad.action.emit('next_control')
            self.assertEqual(window.property('navigationIndex'), 5)
            self.engine.gamepad.action.emit('back'); QTest.qWait(180)
            self.engine.gamepad.action.emit('next_section')
            self.assertEqual(window.property('section'), 3)
            self.engine.gamepad.action.emit('previous_section')
            self.assertEqual(window.property('section'), 1)
            self.assertEqual(window.property('selectedIndex'), selected)
            for device, labels in (('gamepad', ('LB', 'RB', 'LT', 'RT')), ('keyboard', ('Q', 'E', 'Shift+Tab', 'Tab'))):
                bridge.set_input_device(device)
                for name, label in zip(('previousSectionHint', 'nextSectionHint', 'previousControlHint', 'nextControlHint'), labels):
                    self.assertEqual(window.findChild(QObject, name).property('text'), label)
            window.requestActivate(); window.setProperty('focusZone', 2)
            QTest.keyClick(window, Qt.Key.Key_Tab)
            self.assertEqual(window.property('focusZone'), -1)
            self.assertEqual(window.property('statusIndex'), 0)
            QTest.keyClick(window, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(window.property('focusZone'), 0)
            self.assertEqual(window.property('navigationIndex'), 5)
            for scale in (1, 1.2):
                bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit()
                for size in ((1280, 720), (1280, 800), (1920, 1080)):
                    window.resize(*size); QTest.qWait(80)
                    bar = window.findChild(QObject, 'systemStatusBar')
                    controls = [next(item for item in bar.childItems() if item.objectName() == 'status-' + key) for key in ('clock', 'wifi', 'bluetooth', 'battery', 'brightness', 'volume')]
                    controls += [window.findChild(QObject, key) for key in ('gameSearchButton', 'gameViewButton', 'downloadsButton', 'minimizeButton', 'menuButton')]
                    for left, right in zip(controls, controls[1:]):
                        self.assertAlmostEqual(left.mapToScene(QPointF()).y(), right.mapToScene(QPointF()).y(), delta=1)
                        self.assertLess(left.mapToScene(QPointF(left.width(), 0)).x(), right.mapToScene(QPointF()).x())
                    self.assertGreater(controls[0].mapToScene(QPointF()).x(), 0)
                    self.assertLess(controls[-1].mapToScene(QPointF(controls[-1].width(), 0)).x(), size[0])
            bridge.set_input_device('gamepad')
            window.resize(1280, 800); window.setProperty('focusZone', -1); window.setProperty('statusIndex', 1)
            QTest.qWait(150)
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/utility-navigation.png')))
        finally:
            window.findChild(QObject, 'exitPopup').setProperty('visible', False)
            window.setProperty('section', previous[0]); window.setProperty('focusZone', previous[1])
            bridge.set_input_device(previous[2]); bridge.settings['ui_scale'] = previous[3]; bridge.settingsChanged.emit()
            window.resize(1280, 800)

    def test_grid_search_controller_navigation_selection_and_portable_preferences(self):
        import copy
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        original = bridge._games, bridge._installed, copy.deepcopy(bridge.settings), bridge._input_device
        grid = window.findChild(QObject, 'gameGrid')
        search = window.findChild(QObject, 'gameSearchPane')
        field = window.findChild(QObject, 'gameSearchInput')
        details = window.findChild(QObject, 'detailsPopup')
        def submit(query):
            field.setProperty('text', query)
            search.setProperty('selectedField', 1)
            self.engine.gamepad.action.emit('activate'); QTest.qWait(180)
        try:
            games = [dict(bridge._games[0], id='grid-' + str(i), name='Game ' + str(i)) for i in range(40)]
            games[24]['name'] = 'Mafia: Definitive Edition™'
            games[25]['name'] = 'Жизнь после'
            bridge.catalog_loaded(games)
            bridge._installed = [dict(card=dict(games[i]), kind='external') for i in range(30)]
            bridge.libraryChanged.emit()
            bridge.setHideInstalled(False)
            window.showNormal(); window.resize(1280, 800)
            window.setProperty('section', 1); window.setProperty('selectedIndex', 17)
            window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 10)
            self.engine.gamepad.action.emit('activate'); QTest.qWait(180)
            self.assertTrue(window.property('gridMode'))
            self.assertEqual(window.property('selected')['id'], 'grid-17')
            self.assertEqual(grid.property('count'), 40)
            window.setProperty('focusZone', 2)
            self.engine.gamepad.action.emit('down')
            self.assertEqual(window.property('selectedIndex'), 17 + grid.property('columns'))
            self.engine.gamepad.action.emit('up')
            self.assertEqual(window.property('selectedIndex'), 17)
            self.engine.gamepad.action.emit('menu'); QTest.qWait(200)
            self.assertTrue(details.property('visible'))
            self.engine.gamepad.action.emit('back'); QTest.qWait(180)
            self.engine.gamepad.action.emit('next_section'); self.engine.gamepad.action.emit('previous_section')
            self.assertEqual(window.property('selectedIndex'), 17)
            for width, height in ((1280, 720), (1280, 800), (1920, 1200)):
                window.resize(width, height)
                for scale in (1, 1.2):
                    bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit(); QTest.qWait(100)
                    item = grid.property('currentItem')
                    self.assertIsNotNone(item)
                    self.assertGreater(grid.property('columns'), 3)
                    point = item.mapToScene(QPointF(item.width(), item.height()))
                    self.assertLessEqual(point.x(), window.width())
                    self.assertLessEqual(point.y(), window.height() - 20)
            window.resize(1280, 800); bridge.settings['ui_scale'] = 1; bridge.settingsChanged.emit()
            window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 9)
            self.engine.gamepad.action.emit('activate'); QTest.qWait(180)
            self.assertTrue(search.property('visible'))
            self.assertTrue(bridge.virtual_keyboard.visible)
            self.engine.gamepad.action.emit('back')  # Hide only the screen keyboard.
            self.assertFalse(bridge.virtual_keyboard.visible)
            window.requestActivate(); QTest.qWait(30)  # Offscreen Qt ignores WindowDoesNotAcceptFocus.
            submit('mAfIa - definitive edition')
            self.assertEqual(grid.property('count'), 1)
            self.assertEqual(window.property('selected')['id'], 'grid-24')
            self.assertEqual(bridge.sectionUi(1)['game_index'], 24)
            self.assertIn('Найдено: 1', window.findChild(QObject, 'selectionHelp').property('text'))
            self.engine.gamepad.action.emit('previous_section')
            self.assertEqual(grid.property('count'), 30)
            self.engine.gamepad.action.emit('next_section')
            self.assertEqual(grid.property('count'), 1)
            self.assertEqual(window.property('selected')['id'], 'grid-24')
            QTest.keyClick(window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier); QTest.qWait(180)
            self.assertTrue(search.property('visible'))
            self.assertFalse(bridge.virtual_keyboard.visible)
            field.setProperty('text', 'не существует')
            self.assertTrue(field.property('activeFocus'), (self.app.focusObject(), search.property('selectedField'), field.property('focus')))
            QTest.keyClick(window, Qt.Key.Key_Return); QTest.qWait(180)
            self.assertEqual(grid.property('count'), 0)
            self.assertTrue(window.findChild(QObject, 'gridEmptyMessage').property('visible'))
            self.assertEqual(bridge.sectionUi(1)['game_id'], 'grid-24')
            self.engine.gamepad.action.emit('back'); QTest.qWait(100)
            self.assertEqual(grid.property('count'), 40)
            self.assertEqual(window.property('selected')['id'], 'grid-24')
            bridge.save()
            self.assertEqual(self.storage.read_settings()['games_view'], 'grid')
            self.assertGreaterEqual(bridge.sectionUi(1)['scroll_y'], 0)
            with patch.object(self.storage, 'write_settings', side_effect=OSError('read only')):
                self.assertFalse(bridge.setGamesView('ribbon'))
                self.assertEqual(bridge.gamesView, 'grid')
            self.assertFalse(bridge.setGamesView('invalid'))
            window.setProperty('selectedIndex', 0); QTest.qWait(80)
            grid.setProperty('contentY', grid.property('cellHeight') * 3)
            from PySide6.QtCore import QMetaObject
            QMetaObject.invokeMethod(grid, 'movementEnded')
            scroll = grid.property('contentY')
            self.engine.gamepad.action.emit('next_section'); self.engine.gamepad.action.emit('previous_section')
            QTest.qWait(150)
            self.assertEqual(window.property('selectedIndex'), 0)
            self.assertAlmostEqual(grid.property('contentY'), scroll, delta=1)
            window.setProperty('selectedIndex', 24); QTest.qWait(100)
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/game-grid.png')))
            self.assertTrue(bridge.setGamesView('ribbon')); QTest.qWait(150)
            self.assertEqual(window.property('selected')['id'], 'grid-24')
        finally:
            bridge.virtual_keyboard.hide(); search.setProperty('visible', False); details.setProperty('visible', False)
            window.setProperty('searchQueries', {})
            bridge.settings = original[2]; bridge._installed = original[1]
            bridge.catalog_loaded(original[0]); bridge.libraryChanged.emit(); bridge.settingsChanged.emit()
            bridge.set_input_device(original[3]); window.setProperty('section', 1); window.setProperty('focusZone', 2)
            window.resize(1280, 800); bridge.save(); QTest.qWait(250)

    def test_selected_header_icons_explain_their_action_without_mouse_hover(self):
        bridge, window = self.engine.bridge, self.window
        previous = (bridge.hideInstalled, bridge.settings.get('ui_scale', 1), bridge._input_device,
                    window.property('section'), window.property('focusZone'), window.property('navigationIndex'))
        help_label = window.findChild(QObject, 'selectionHelp')
        settings_pane = window.findChild(QObject, 'settingsPane')
        try:
            window.showNormal(); QTest.mouseMove(window, QPoint(1, 1))
            bridge.setHideInstalled(False)
            window.setProperty('section', 1); window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 1)
            self.engine.gamepad.action.emit('right')
            self.assertEqual(window.property('navigationIndex'), 6)
            self.assertTrue(help_label.property('visible'))
            self.assertIn('Фильтр каталога', help_label.property('text'))
            self.assertIn('A — скрыть уже установленные игры', help_label.property('text'))
            self.assertFalse(window.findChild(QObject, 'installedFilter').property('hovered'))
            self.assertFalse(bridge.hideInstalled)
            self.engine.gamepad.action.emit('activate')
            self.assertTrue(bridge.hideInstalled)
            self.assertIn('A — показать все', help_label.property('text'))
            QTest.keyClick(window, Qt.Key.Key_Left); QTest.keyClick(window, Qt.Key.Key_Right)
            self.assertIn('Enter — показать все', help_label.property('text'))
            for unused in range(12):
                if window.property('navigationIndex') == 5: break
                self.engine.gamepad.action.emit('next_control')
            self.assertEqual(window.property('navigationIndex'), 5)
            self.assertIn('Настройки', help_label.property('text'))
            self.assertIn('A — открыть', help_label.property('text'))
            self.engine.gamepad.action.emit('activate'); QTest.qWait(200)
            self.assertTrue(settings_pane.property('visible'))
            self.assertFalse(help_label.property('visible'))
            self.engine.gamepad.action.emit('back'); QTest.qWait(200)
            window.setProperty('section', 0); window.setProperty('navigationIndex', 0)
            self.engine.gamepad.action.emit('right')
            self.assertIn('Сортировка игр:', help_label.property('text'))
            window.setProperty('statusIndex', 0)
            self.engine.gamepad.action.emit('up'); self.engine.gamepad.action.emit('right')
            self.assertIn('Подключение Wi-Fi', help_label.property('text'))
            for width, height in ((1280, 720), (1280, 800), (1920, 1200)):
                window.resize(width, height)
                for scale in (1, 1.2):
                    bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit()
                    window.setProperty('section', 1); window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 6)
                    QTest.qWait(50)
                    self.assertLessEqual(help_label.property('implicitWidth'), help_label.property('width'))
                    point = help_label.mapToScene(QPointF(help_label.width(), help_label.height()))
                    self.assertLessEqual(point.y(), window.height())
            window.resize(1280, 800)
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/selected-header-help.png')))
            self.engine.gamepad.action.emit('down')
            self.assertFalse(help_label.property('visible'))
            self.assertTrue(window.findChild(QObject, 'acceptHint').property('visible'))
        finally:
            settings_pane.setProperty('visible', False)
            bridge.setHideInstalled(previous[0]); bridge.settings['ui_scale'] = previous[1]; bridge.settingsChanged.emit()
            bridge.set_input_device(previous[2])
            window.setProperty('section', previous[3]); window.setProperty('focusZone', previous[4]); window.setProperty('navigationIndex', previous[5])
            window.resize(1280, 800); QTest.qWait(200)

    def test_hints_follow_actual_input_across_screens_without_idle_or_touch_flips(self):
        bridge, window = self.engine.bridge, self.window
        original = window.property('section'), window.property('focusZone'), bridge._input_device
        settings = window.findChild(QObject, 'settingsPane')
        menu = window.findChild(QObject, 'exitPopup')
        roots = ['settingsPane', 'directoryPicker', 'localScanner', 'metadataPicker', 'downloadsPage',
                 'installDestination', 'updatesPane', 'catalogizerPane', 'bindingsPane', 'virtualKeyboardWindow']
        def check(device, accept, back, sections):
            self.assertEqual(bridge.inputHints['device'], device)
            for name in roots:
                self.assertEqual(window.findChild(QObject, name).property('hints')['device'], device, name)
            self.assertEqual(window.findChild(QObject, 'acceptHint').property('text'), accept + '   Выбрать')
            self.assertEqual(window.findChild(QObject, 'backHint').property('text'), back + '   Назад')
            self.assertEqual(window.findChild(QObject, 'sectionsHint').property('text'), sections + '   Разделы')
            self.assertEqual(window.findChild(QObject, 'settingsBack').property('text'), 'Назад · ' + back)
        try:
            bridge.set_input_device('gamepad')
            window.showNormal(); window.setProperty('section', 1); window.setProperty('focusZone', 2)
            check('gamepad', 'A', 'B', 'LB / RB')
            QTest.keyClick(window, Qt.Key.Key_F2); QTest.qWait(230)
            self.assertTrue(settings.property('visible'))
            check('keyboard', 'Enter', 'Esc', 'Q / E')
            self.engine.gamepad.connectionChanged.emit(True)
            self.engine.gamepad.button_handler(0)
            QTest.qWait(50)
            check('keyboard', 'Enter', 'Esc', 'Q / E')
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
            check('keyboard', 'Enter', 'Esc', 'Q / E')
            self.engine.gamepad.action.emit('down')
            check('gamepad', 'A', 'B', 'LB / RB')
            # Typing in an editor changes hints before the text-input early return.
            settings.setProperty('selectedField', 0)
            field = settings.findChild(QObject, 'settingsDestination')
            field.forceActiveFocus()
            before = field.property('text')
            QTest.keyClick(window, Qt.Key.Key_Z)
            check('keyboard', 'Enter', 'Esc', 'Q / E')
            field.setProperty('text', before); field.setProperty('focus', False)
            # Raw pad input consumed by the binding editor must switch hints too.
            self.engine.gamepad.button_handler(0x40)
            check('gamepad', 'A', 'B', 'LB / RB')
            self.engine.gamepad.button_handler(0)
            self.engine.gamepad.action.emit('menu'); QTest.qWait(200)
            selected_before = window.property('selectedIndex')
            QTest.keyClick(window, Qt.Key.Key_Right)
            window.setProperty('selectedIndex', selected_before)
            QMetaObject.invokeMethod(window, 'openMenu'); QTest.qWait(200)
            self.assertTrue(menu.property('visible'))
            self.assertEqual(window.findChild(QObject, 'menuSettings').property('text'), 'Настройки · F2')
            self.engine.gamepad.action.emit('down')
            self.assertEqual(window.findChild(QObject, 'menuSettings').property('text'), 'Настройки · Y')
            self.assertEqual(window.findChild(QObject, 'menuMinimize').property('text'), 'Свернуть · X')
        finally:
            settings.setProperty('visible', False); menu.setProperty('visible', False)
            bridge.set_input_device(original[2]); bridge.set_gamepad_connected(False)
            window.setProperty('section', original[0]); window.setProperty('focusZone', original[1])
            QTest.qWait(200)

    def test_download_queue_with_128_bit_file_identity_remains_visible_and_keeps_backend_identity(self):
        import json
        bridge, window = self.engine.bridge, self.window
        card = json.loads((self.repo / 'examples/black-mesa.json').read_text(encoding='utf8'))
        image = dict(path='/assets/ngs_launcher/test.png', size=1, sha256='0'*64)
        card.update(id='large-file-identity', revision=1, cover=image, background=image, delivery='on_request')
        original = [2**80 + 17, 2**120 + 29]
        try:
            bridge.download_queue.add(card, self.root/'offline', 'http://offline-test')
            bridge.download_queue.set_state(card['id'], 'paused', 'Saved partial files')
            bridge.download_queue.update(card['id'], stage_identity=original)
            bridge.downloadsChanged.emit()
            window.setProperty('section', 2); window.setProperty('focusZone', 1)
            QTest.qWait(80)
            view = window.findChild(QObject, 'downloadQueueList')
            self.assertEqual(view.property('count'), 1)
            jobs = window.findChild(QObject, 'downloadsPage').property('jobs')
            if hasattr(jobs, 'toVariant'): jobs = jobs.toVariant()
            self.assertEqual(jobs[0]['id'], card['id'])
            self.assertNotIn('stage_identity', bridge.downloadJobs[0])
            self.assertEqual(bridge.download_queue.get(card['id'])['stage_identity'], original)
        finally:
            bridge.download_queue.forget_row(card['id']); bridge.downloadsChanged.emit()

    def test_download_details_show_pinned_destination_and_large_byte_totals(self):
        import json, re
        bridge, window = self.engine.bridge, self.window
        target = self.root / "Игры с пробелами" / "Выбранная папка"
        previous_paths = self.storage.games, self.storage.game_staging
        previous_scale = bridge.settings.get('ui_scale', 1)
        identities = []
        try:
            for kind in ("game", "installer"):
                card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
                art = dict(path="/assets/ngs_launcher/test.png", size=1, sha256="0" * 64)
                card.update(id="details-" + kind, revision=1, cover=art, background=art, delivery="on_request", total_size=5 * 1024**3)
                if kind == "installer": card['kind'] = kind
                identities.append(card['id'])
                bridge.download_queue.add(card, self.root / "offline", "http://offline-test", str(target))
                bridge.download_queue.set_state(card['id'], 'paused', 'Частичные файлы сохранены')
                bridge.download_queue.update(card['id'], completed_bytes=3 * 1024**3, total_bytes=5 * 1024**3,
                                             network_bytes=2 * 1024**3, network_total=-1)
            self.storage.set_games_directory('another-default')
            bridge.downloadsChanged.emit()
            window.setProperty('section', 2); window.setProperty('focusZone', 1)
            pane = window.findChild(QObject, 'downloadsPage')
            view = window.findChild(QObject, 'downloadQueueList')
            for width, height in ((1280, 720), (1280, 800), (1920, 1200)):
                window.resize(width, height)
                for scale in (.8, 1, 1.2):
                    bridge.settings['ui_scale'] = scale; bridge.settingsChanged.emit()
                    pane.setProperty('currentIndex', 0)
                    for index, identity in enumerate(identities):
                        if index: bridge.action.emit('down')
                        QTest.qWait(80)
                        row = next(item for item in view.property('contentItem').childItems() if item.property('identity') == identity)
                        controls = {item.objectName(): item for item in row.childItems()}
                        expected = target / ('.ngs-installers' if index else '') / identity
                        self.assertIn(str(expected), controls['queueDestination-' + identity].property('text'))
                        self.assertEqual(row.property('completedBytes'), 3 * 1024**3)
                        self.assertIn('5120', re.sub(r'\s', '', controls['queueFileSize-' + identity].property('text')))
                        self.assertIn('уточняется', controls['queueNetworkSize-' + identity].property('text'))
                        button = controls['queuePrimary-' + identity]
                        for unused in range(100):
                            point = button.mapToScene(QPointF(button.width(), button.height()))
                            if point.y() <= window.height() and point.x() <= window.width(): break
                            QTest.qWait(10)
                        self.assertLessEqual(point.y(), window.height())
                        self.assertLessEqual(point.x(), window.width())
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/download-details-ui.png')))
        finally:
            for identity in identities: bridge.download_queue.forget_row(identity)
            self.storage.games, self.storage.game_staging = previous_paths
            bridge.settings['ui_scale'] = previous_scale; bridge.settingsChanged.emit()
            bridge.downloadsChanged.emit(); window.setProperty('section', 1); window.resize(1280, 800)
            QTest.qWait(100)

    def test_installer_section_opens_wizard_then_requires_confirmed_game_folder(self):
        import json, struct
        from types import SimpleNamespace
        from unittest.mock import patch
        from ngs.install import build_manifest, install_game
        bridge, window = self.engine.bridge, self.window
        previous = bridge._installer_games, bridge._installer_packages, list(bridge._installed), list(bridge._games)
        source = self.root / "installer-source"
        folder = source / "Setup"
        folder.mkdir(parents=True, exist_ok=True)
        pe = bytearray(68); pe[:2] = b"MZ"; struct.pack_into("<I", pe, 60, 64); pe[64:] = b"PE\0\0"
        (folder / "setup.exe").write_bytes(pe)
        (folder / "data.bin").write_bytes(b"full installer payload")
        card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
        art = dict(path="/assets/ngs_launcher/fixture.png", size=1, sha256="0" * 64)
        card.update(id="setup-ui-test", game_id="provider-game-id", kind="installer", source_folder="Setup",
                    executable="setup.exe", revision=1, cover=art, background=art)
        destination = bridge.storage.games / ".ngs-installers"
        destination.mkdir(exist_ok=True)
        context = SimpleNamespace(root=bridge.storage.root, resources=bridge.storage.resources, state=bridge.storage.state,
                                  games=destination, game_staging=bridge.storage.game_staging)
        install_game(context, source, card, build_manifest(source, card))
        bridge._installer_packages = bridge.installer_library.reconcile()
        bridge._installer_games = [dict(card, image=bridge._games[0]["image"], backgroundImage=bridge._games[0]["image"])]
        ready = dict(card, id="installed-ui-test")
        ready.pop("kind"); ready.pop("game_id")
        bridge._games = bridge._games + [ready]
        scanner = window.findChild(QObject, "localScanner")
        imported = None
        try:
            bridge.installersChanged.emit()
            window.setProperty("section", 3); window.setProperty("selectedIndex", 0)
            QTest.qWait(200)
            self.assertTrue(window.property("installerReady"))
            self.assertEqual(window.findChild(QObject, "gameActionButton").property("text"), "Установить")
            self.assertTrue(window.findChild(QObject, "addInstallerGame").property("visible"))
            self.assertTrue(window.grabWindow().save(str(self.repo / "reports/installers-ui.png")))
            with patch("ngs.app.QDesktopServices.openUrl", return_value=True) as opened:
                bridge.performInstallerAction(card["id"])
                for unused in range(200):
                    QTest.qWait(10)
                    if not bridge.operationBusy: break
                opened.assert_called_once()
                self.assertTrue(opened.call_args.args[0].toLocalFile().endswith("setup.exe"))
            self.assertEqual(bridge.library.reconcile(), previous[2])
            window.showNormal(); window.setProperty("focusZone", 1); window.setProperty("heroAction", 3)
            with patch.object(bridge, "browseLocalFolder") as browse:
                bridge.action.emit("activate"); QTest.qWait(200)
                self.assertTrue(scanner.property("visible")); browse.assert_called_once()
            installed = self.root / "wizard-installed-game"
            installed.mkdir(exist_ok=True); (installed / "play.exe").write_bytes(pe)
            bridge._scanner.update(path=str(installed), stage="exes", candidates=[dict(path="play.exe")])
            with patch.object(bridge, "findMetadata") as metadata:
                bridge.selectLocalExecutable("play.exe")
                metadata.assert_not_called()
            self.assertEqual(bridge.scannerState["matches"][0]["id"], "installed-ui-test")
            bridge.confirmLocalGame("Installed game", "installed-ui-test")
            for unused in range(200):
                QTest.qWait(10)
                if not bridge.operationBusy: break
            imported = next(r for r in bridge.library.reconcile() if r.get("catalog_id") == "installed-ui-test")
            self.assertEqual(window.property("section"), 0)
            self.assertEqual(window.property("selected")["id"], imported["card"]["id"])
            self.assertFalse(imported["managed"])
            self.assertEqual(imported["kind"], "external")
            self.assertNotIn("kind", imported["card"])
            bridge.library.forget(imported["card"]["id"])
            self.assertEqual((installed / "play.exe").read_bytes(), pe)
            imported = None
        finally:
            if imported: bridge.library.forget(imported["card"]["id"])
            bridge.installer_library.forget(card["id"])
            bridge._installer_games, bridge._installer_packages, bridge._installed, bridge._games = previous
            bridge._installer_import = None
            bridge.installersChanged.emit(); bridge.libraryChanged.emit()
            scanner.close(); window.showNormal(); window.setProperty("section", 1)
            QTest.qWait(200)

    def test_keyboard_width_work_area_and_ui_scale_use_logical_pixels(self):
        from unittest.mock import Mock
        from PySide6.QtCore import QRect
        bridge, keyboard = self.engine.bridge, self.engine.bridge.keyboard
        keys = self.window.findChild(QWindow, 'virtualKeyboardWindow')
        previous = bridge.settings.get('ui_scale', 1), keyboard.screen
        try:
            keyboard.show()
            QTest.qWait(300)
            for x,y,width,height in ((0,0,1280,800),(-1920,40,1920,1040),(0,0,800,600)):
                screen = Mock()
                screen.geometry.return_value = QRect(x,y,width,height)
                keyboard.set_screen(screen)
                for scale in (.8, 1, 1.2):
                    bridge.settings['ui_scale'] = scale
                    bridge.settingsChanged.emit(); QTest.qWait(60)
                    self.assertAlmostEqual(keys.width(), width, delta=2)
                    self.assertGreaterEqual(keys.x(), x)
                    self.assertGreaterEqual(keys.y(), y)
                    self.assertLessEqual(keys.x()+keys.width(), x+width)
                    self.assertLessEqual(keys.y()+keys.height(), y+height,
                                         (x, y, width, height, scale, keys.geometry(), keys.frameMargins()))
        finally:
            keyboard.hide()
            bridge.settings['ui_scale'] = previous[0]
            bridge.settingsChanged.emit()
            keyboard.set_screen(previous[1])

    def test_toggle_binding_editor_restores_same_selection_with_short_and_hold(self):
        import copy
        from PySide6.QtCore import QMetaObject
        from unittest.mock import Mock, patch
        bridge, window = self.engine.bridge, self.window
        controls = bridge.desktop
        previous = copy.deepcopy(bridge.settings), controls.backend
        pane = window.findChild(QObject, 'bindingsPane')
        try:
            bridge.settings['shortcuts'] = {}
            controls.backend = Mock(tray=True)
            self.assertTrue(controls.assign('desktop', dict(buttons=0x40)))
            QMetaObject.invokeMethod(pane, 'open'); QTest.qWait(150)
            pane.setProperty('selectedField', 1)
            self.engine.gamepad.action.emit('down')
            self.assertEqual(pane.property('selectedField'), 13)
            self.engine.gamepad.action.emit('activate')
            self.assertTrue(controls.values()['desktop']['toggle'])
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(150)
            for tray in (True, False):
                for fullscreen in (False, True):
                    controls.backend.tray = tray
                    if fullscreen: window.showFullScreen()
                    else: window.showNormal()
                    QTest.qWait(40)
                    window.setProperty('section', 1); window.setProperty('selectedIndex', 1)
                    identity = window.property('selected')['id']
                    before = window.visibility()
                    for hold in (False, True):
                        self.assertTrue(controls.assign('desktop', dict(buttons=0x40, hold=hold)))
                        for start in (10, 20):
                            with patch('ngs.desktop.time.monotonic', return_value=start):
                                controls.gamepad_input(0); controls.gamepad_input(0x40)
                            if hold:
                                with patch('ngs.desktop.time.monotonic', return_value=start + .8):
                                    controls.check_holds(); controls.check_holds()
                            controls.gamepad_input(0); QTest.qWait(40)
                            if start == 10:
                                self.assertEqual(window.visibility(), window.Visibility.Hidden if tray else window.Visibility.Minimized)
                            else:
                                self.assertEqual(window.visibility(), before)
                                self.assertEqual(window.property('selected')['id'], identity)
                                self.assertEqual(window.property('section'), 1)
            controls.setToggle('desktop', False)
            controls.fire_action('desktop'); controls.fire_action('desktop'); QTest.qWait(40)
            self.assertEqual(window.visibility(), window.Visibility.Minimized)
            controls.action.emit('show'); QTest.qWait(40)
            self.engine.power.sleeping = True
            controls.action.emit('toggle'); QTest.qWait(40)
            self.assertEqual(window.visibility(), before)
        finally:
            self.engine.power.sleeping = False
            controls.cancel_presses(); controls.cancelCapture()
            QMetaObject.invokeMethod(pane, 'close')
            bridge.settings, controls.backend = previous
            bridge.save(); controls.changed.emit()
            window.showNormal(); window.resize(1280, 800)
            window.setProperty('section', 1); window.setProperty('focusZone', 2)
            QTest.qWait(200)

    def test_bindings_editor_capture_tray_restore_and_keyboard_gamepad(self):
        import copy
        from unittest.mock import Mock
        bridge, window = self.engine.bridge, self.window
        controls, keyboard = bridge.desktop, bridge.keyboard
        previous = copy.deepcopy(bridge.settings), controls.backend, keyboard.backend
        pane = window.findChild(QObject, 'bindingsPane')
        settings = window.findChild(QObject, 'settingsPane')
        keys = window.findChild(QWindow, 'virtualKeyboardWindow')
        try:
            keyboard.backend = Mock()
            keyboard.backend.foreground.return_value = 42
            bridge.action.emit('settings'); QTest.qWait(220)
            settings.setProperty('selectedField', 10)
            self.engine.gamepad.action.emit('activate'); QTest.qWait(220)
            self.assertTrue(pane.property('visible'))
            self.engine.gamepad.action.emit('down'); self.engine.gamepad.action.emit('down')
            self.engine.gamepad.action.emit('activate')
            self.assertEqual(controls.capturing, 'show')
            QTest.keyClick(window, Qt.Key.Key_F8, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(controls.capturing, '')
            self.assertEqual(self.storage.read_settings()['shortcuts']['show'], dict(vk=119, mods=6))
            pane.setProperty('selectedField', 11)
            self.engine.gamepad.action.emit('activate')
            self.assertTrue(pane.property('holdCapture'))
            pane.setProperty('selectedField', 0)
            self.engine.gamepad.action.emit('activate')
            for buttons in (0, 0x2000, 0): self.engine.gamepad.button_handler(buttons)
            self.assertEqual(controls.values()['desktop'], {'buttons': 0x2000, 'hold': True})
            self.assertTrue(pane.property('visible'))
            controls.clear('desktop')
            pane.setProperty('selectedField', 11); self.engine.gamepad.action.emit('activate')
            self.assertFalse(pane.property('holdCapture'))
            controls.beginCapture('keyboard')
            for buttons in (0x1000, 0, 0x2000, 0):
                self.assertTrue(self.engine.gamepad.button_handler(buttons))
            self.assertEqual(controls.capturing, '')
            self.assertTrue(pane.property('visible'))
            self.assertEqual(self.storage.read_settings()['shortcuts']['keyboard'], {'buttons': 0x2000})
            controls.clear('keyboard')
            self.assertTrue(keyboard.saveMacro('Test macro', 'Ctrl+C; wait:200; Ctrl+V'))
            pane.setProperty('selectedField', 9)
            self.engine.gamepad.action.emit('activate'); QTest.qWait(100)
            self.assertTrue(keyboard.visible)
            self.assertTrue(keys.isVisible())
            self.assertTrue(keys.flags() & Qt.WindowType.WindowDoesNotAcceptFocus)
            keyboard.setView('touch')
            def select_key(name):
                rows = keys.property('rows').toVariant()
                row = next(i for i, names in enumerate(rows) if name in names)
                keys.setProperty('selectedRow', row); keys.setProperty('selectedColumn', rows[row].index(name))
            select_key('Ctrl')
            self.engine.gamepad.action.emit('activate')
            self.assertEqual(keyboard.held, ['Ctrl'])
            select_key('A')
            self.engine.gamepad.action.emit('activate')
            keyboard.backend.send.assert_called_with(['Ctrl', 'A'])
            for unused in range(len(keyboard.views)):
                self.engine.gamepad.action.emit('next_section')
                if keyboard.options['view'] == 'numpad': break
            select_key('Num7')
            self.engine.gamepad.action.emit('activate')
            keyboard.backend.send.assert_called_with(['Ctrl', 'Num7'])
            keyboard.clearModifiers()
            keyboard.runMacro('Test macro')
            keyboard.backend.send.assert_called_with(['Ctrl', 'C'])
            for unused in range(4):
                if not pane.property('visible'): break
                self.engine.gamepad.action.emit('back'); QTest.qWait(90)
            self.assertFalse(keyboard.running)
            self.assertFalse(pane.property('visible'))
            controls.backend = Mock(tray=True)
            self.assertTrue(controls.assign('show', {'buttons': 0xc0}))
            window.showFullScreen(); QTest.qWait(40)
            bridge.minimize(); QTest.qWait(40)
            self.assertEqual(window.visibility(), window.Visibility.Hidden)
            self.assertFalse(self.engine.gamepad.active)
            self.assertTrue(self.engine.gamepad.background)
            self.engine.gamepad.button_handler(0)
            self.engine.gamepad.button_handler(0xc0); QTest.qWait(40)
            self.assertEqual(window.visibility(), window.Visibility.FullScreen)
            window.setWindowStates(window.windowStates() | Qt.WindowState.WindowMinimized); QTest.qWait(40)
            self.assertFalse(window.isVisible())
            controls.action.emit('show'); QTest.qWait(40)
            self.assertEqual(window.visibility(), window.Visibility.FullScreen)
            bridge.minimize(); controls.action.emit('keyboard'); QTest.qWait(40)
            self.assertFalse(window.isVisible())
            self.assertTrue(keys.isVisible())
            self.assertTrue(self.engine.gamepad.active)
            self.engine.gamepad.action.emit('back'); QTest.qWait(40)
            self.assertFalse(keys.isVisible())
            self.assertFalse(self.engine.gamepad.active)
            controls.action.emit('show'); QTest.qWait(40)
            bridge.action.emit('settings'); QTest.qWait(180)
            settings.setProperty('selectedField', 10); bridge.action.emit('activate'); QTest.qWait(120)
            self.assertTrue(window.grabWindow().save(str(self.repo/'reports/bindings-editor.png')))
            keyboard.show(); QTest.qWait(80)
            self.assertTrue(keys.grabWindow().save(str(self.repo/'reports/virtual-keyboard.png')))
        finally:
            controls.cancelCapture(); keyboard.hide()
            pane.setProperty('holdCapture', False)
            pane.setProperty('visible', False); settings.setProperty('visible', False)
            bridge.settings, controls.backend, keyboard.backend = previous
            bridge.save(); controls.changed.emit(); keyboard.changed.emit(); keyboard.macrosChanged.emit()
            window.showNormal(); window.resize(1280, 800)
            window.setProperty('section', 1); window.setProperty('focusZone', 2)
            QTest.qWait(200)

    def test_launch_options_controller_edit_alias_launch_cancel_reset_and_write_error(self):
        import json, sys
        from unittest.mock import Mock, patch
        from PySide6.QtCore import QMetaObject
        bridge, window = self.engine.bridge, self.window
        original = bridge._games.copy()
        game = self.root / 'launch-options-game'
        game.mkdir()
        shutil.copyfile(sys.executable, game / 'play.exe')
        card = json.loads((self.repo / 'examples/black-mesa.json').read_text(encoding='utf8'))
        image = dict(path='/assets/ngs_launcher/test.jpg', sha256='0'*64, size=1)
        card.update(id='launch-options-test', source_folder='launch-options-test', executable='play.exe', revision=1, cover=image, background=image)
        receipt = bridge.library.register_external(game, 'play.exe', 'Launch options', card)
        identity = receipt['card']['id']
        before = (game / 'play.exe').read_bytes(), (bridge.library.records / (identity + '.json')).read_bytes()
        pane = window.findChild(QObject, 'launchOptionsPane')
        field = window.findChild(QObject, 'launchArgumentsField')
        line = '-windowed -name "Игрок один"'
        def settled():
            for unused in range(200):
                QTest.qWait(20)
                if not bridge.operationBusy: return
            self.fail(bridge.operationStatus)
        try:
            bridge.catalog_loaded([dict(card, image='', backgroundImage='')])
            bridge.refreshLocalLibrary()
            window.setProperty('section', 1)
            window.setProperty('selectedIndex', 0)
            QTest.qWait(300)
            QMetaObject.invokeMethod(window, 'openDetails')
            QTest.qWait(250)
            bridge.action.emit('right')
            bridge.action.emit('right')
            self.assertEqual(window.property('detailsAction'), 8)
            bridge.action.emit('activate')
            self.assertEqual(window.property('detailsAction'), 5)
            bridge.action.emit('activate')
            QTest.qWait(300)
            self.assertTrue(pane.property('visible'))
            self.assertTrue(pane.property('ready'))
            self.assertTrue(window.property('modalVisible'))
            field.setProperty('text', line)
            bridge.action.emit('down')
            bridge.action.emit('activate')
            QTest.qWait(200)
            self.assertFalse(pane.property('visible'))
            self.assertEqual(bridge.library.launch_spec(identity)[2], ['-windowed', '-name', 'Игрок один'])
            self.assertEqual(bridge.launchOptions(card['id'])['arguments'], line)
            fake = Mock(); fake.active.return_value = True
            with patch('ngs.app.GameProcess', return_value=fake) as launched:
                bridge.performGameAction(card['id'])
                settled()
                self.assertEqual(launched.call_args.args, (game / 'play.exe', game, ['-windowed', '-name', 'Игрок один']))
                fake.active.return_value = False
                bridge.check_game()
            QMetaObject.invokeMethod(window, 'openLaunchOptions')
            QTest.qWait(200)
            self.assertEqual(field.property('text'), line)
            field.setProperty('text', '-unsaved')
            bridge.action.emit('back')
            QTest.qWait(200)
            self.assertEqual(bridge.library.launch_arguments(identity), line)
            QMetaObject.invokeMethod(window, 'openLaunchOptions')
            QTest.qWait(200)
            field.setProperty('text', '')
            bridge.action.emit('down'); bridge.action.emit('activate')
            QTest.qWait(200)
            self.assertEqual(bridge.library.launch_spec(identity)[2], [])
            QMetaObject.invokeMethod(window, 'openLaunchOptions')
            QTest.qWait(200)
            field.setProperty('text', line)
            with patch.object(bridge.library, 'set_launch_arguments', side_effect=PermissionError('fixture write error')):
                bridge.action.emit('down'); bridge.action.emit('activate')
            self.assertTrue(pane.property('visible'))
            self.assertIn('fixture write error', pane.property('status'))
            self.assertEqual(field.property('text'), line)
            self.assertEqual(bridge.library.launch_arguments(identity), '')
            window.resize(800, 500)
            QTest.qWait(250)
            save = window.findChild(QObject, 'saveLaunchArguments')
            point = save.mapToScene(QPointF(save.width(), save.height()))
            self.assertLessEqual(point.x(), window.width()); self.assertLessEqual(point.y(), window.height())
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/launch-options-800x500.png')))
            self.assertFalse(bridge.launchOptions('unknown-game')['ready'])
            self.assertFalse(bridge.setLaunchArguments('unknown-game', '-x'))
            self.assertEqual(((game / 'play.exe').read_bytes(), (bridge.library.records / (identity + '.json')).read_bytes()), before)
        finally:
            pane.close()
            bridge.stop()
            bridge.library.forget_external(identity)
            bridge.refreshLocalLibrary()
            bridge.catalog_loaded(original)
            window.resize(1280, 800)
            window.setProperty('section', 1); window.setProperty('selectedIndex', 0)
            QTest.qWait(250)

    def test_tab_confirmation_keeps_header_focus_until_down_and_after_menu(self):
        bridge, window = self.engine.bridge, self.window
        menu = window.findChild(QObject, 'settingsPane')
        navigation = window.findChild(QObject, 'headerNavigation')
        try:
            for section in (2, 0, 1, 3):
                control_index = 8 if section == 3 else section
                window.setProperty('focusZone', 0)
                window.setProperty('navigationIndex', control_index)
                for unused in range(2):
                    self.engine.gamepad.action.emit('activate')
                    self.assertEqual(window.property('section'), section)
                    self.assertEqual(window.property('focusZone'), 0)
                    self.assertEqual(window.property('navigationIndex'), control_index)
                    tab = window.findChild(QObject, 'downloadsButton') if section == 2 else next(item for item in navigation.childItems() if item.property('targetSection') == section)
                    self.assertTrue(tab.property('highlighted'))
                self.engine.gamepad.action.emit('right')
                self.assertEqual(window.property('section'), section)
                self.assertEqual(window.property('focusZone'), 0)
                self.assertNotEqual(window.property('navigationIndex'), control_index)
                window.setProperty('navigationIndex', 5)
                self.engine.gamepad.action.emit('activate')
                self.assertTrue(menu.property('visible'))
                self.engine.gamepad.action.emit('back')
                QTest.qWait(200)
                self.assertFalse(menu.property('visible'))
                self.assertTrue(window.findChild(QObject, 'menuButton').property('highlighted'))
                while window.property('focusZone') <= 0:
                    self.engine.gamepad.action.emit('down')
                self.assertEqual(window.property('focusZone'), 1)
        finally:
            menu.setProperty('visible', False)
            window.setProperty('section', 1)
            window.setProperty('focusZone', 2)

    def test_header_tabs_require_confirmation_and_sort_keeps_selected_game(self):
        import copy
        from unittest.mock import patch
        from ngs.install import sort_installs
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge.settings), bridge._installed
        menu = window.findChild(QObject, 'librarySortMenu')
        try:
            bridge._installed = [dict(kind='external', added_at=i+1, card=dict(bridge._games[0], id='sort-'+str(i),
                                  name=name, genre=genre)) for i,name,genre in [(0,'Zebra','Action'),(1,'Alpha','Puzzle'),(2,'Beta','')]]
            bridge.settings['library_sort'] = 'recent'
            bridge._installed = sort_installs(bridge._installed)
            bridge.libraryChanged.emit()
            window.setProperty('section',0)
            window.setProperty('selectedIndex',1)
            identity = window.property('selected')['id']
            window.setProperty('focusZone',0)
            window.setProperty('navigationIndex',0)
            self.engine.gamepad.action.emit('right')
            self.assertEqual(window.property('navigationIndex'),1)
            self.assertEqual(window.property('section'),0)
            navigation = window.findChild(QObject,'headerNavigation')
            tab = next(item for item in navigation.childItems() if item.property('index') == 1)
            self.assertTrue(tab.property('highlighted'))
            self.engine.gamepad.action.emit('activate')
            self.assertEqual(window.property('section'),1)
            self.assertEqual(window.property('focusZone'),0)
            self.assertTrue(tab.property('highlighted'))
            bridge.action.emit('left')
            self.assertEqual(window.property('section'),1)
            bridge.action.emit('activate')
            self.assertEqual(window.property('section'),0)
            self.assertEqual(window.property('focusZone'),0)
            window.setProperty('selectedIndex',next(i for i,g in enumerate(bridge.localGames) if g['id']==identity))
            window.setProperty('focusZone',0)
            window.setProperty('navigationIndex',0)
            bridge.action.emit('right')
            self.assertEqual(window.property('navigationIndex'),1)
            window.setProperty('navigationIndex',7)
            bridge.action.emit('activate'); QTest.qWait(200)
            self.assertTrue(menu.property('visible'))
            bridge.action.emit('down'); bridge.action.emit('down')
            with patch.object(bridge.library,'reconcile',side_effect=AssertionError('Sorting rescanned the disk')):
                bridge.action.emit('activate')
            self.assertEqual(bridge.librarySort,'name')
            self.assertEqual([g['name'] for g in bridge.localGames],['Alpha','Beta','Zebra'])
            self.assertEqual(window.property('selected')['id'],identity)
            self.assertEqual(self.storage.read_settings()['library_sort'],'name')
            QTest.qWait(180)
            self.assertFalse(menu.property('visible'))
            self.assertFalse(bridge.setLibrarySort('unsafe'))
            control = window.findChild(QObject,'librarySortButton')
            QTest.qWait(350)
            point=control.mapToScene(QPointF(control.width()/2,control.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.MouseButton.LeftButton,pos=point); QTest.qWait(100)
            self.assertTrue(menu.property('visible'))
            genre=next(item for item in menu.property('contentItem').childItems() if item.objectName()=='librarySort-genre')
            point=genre.mapToScene(QPointF(genre.width()/2,genre.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.MouseButton.LeftButton,pos=point)
            self.assertEqual(bridge.librarySort,'genre')
            self.assertEqual([g['name'] for g in bridge.localGames],['Zebra','Alpha','Beta'])
            window.setProperty('navigationIndex',7)
            window.setProperty('focusZone',0)
            bridge.action.emit('activate'); QTest.qWait(100)
            bridge.action.emit('down')
            self.assertEqual(window.property('sortAction'),4)
            bridge.action.emit('activate')
            QTest.qWait(180)
            self.assertFalse(menu.property('visible'))
            self.assertTrue(window.grabWindow().save(str(self.repo/'reports/library-sort.png')))
        finally:
            menu.setProperty('visible',False)
            bridge.settings, bridge._installed = previous
            bridge.libraryChanged.emit(); bridge.save()
            window.setProperty('section',1); window.setProperty('focusZone',2)
            QTest.qWait(300)

    def test_status_bar_gamepad_shortcuts_unknowns_and_game_pause(self):
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        previous = dict(bridge.systemState)
        old_scale = bridge.settings.get('ui_scale',1)
        polling = patch.object(bridge.system_status,'refresh')
        polling.start()
        bridge.system_status.close(); QGuiApplication.processEvents()
        timer = window.findChild(QObject,'systemStatusTimer')
        bar = window.findChild(QObject,'systemStatusBar')
        def status_button(key):
            return next(item for item in bar.childItems() if item.objectName()=='status-'+key)
        try:
            bridge.system_status.apply(dict(previous,wifi='on',bluetooth='off',battery=42,has_battery=True,plugged=False,volume=37,muted=False))
            window.setProperty('section',1); window.setProperty('focusZone',0); window.setProperty('navigationIndex',1)
            bridge.action.emit('up')
            self.assertEqual(window.property('focusZone'),-1)
            with patch('ngs.app.QDesktopServices.openUrl',return_value=True) as opened, patch('ngs.system_controls.operate', return_value=dict(items=[])):
                for index,key in enumerate(window.property('statusKeys').toVariant()):
                    window.setProperty('statusIndex',index)
                    bridge.action.emit('activate')
                    QTest.qWait(40)
                    panel = window.findChild(QObject, 'systemPanel')
                    self.assertTrue(panel.property('visible'))
                    self.assertEqual(panel.property('kind'), key)
                    bridge.action.emit('back'); QTest.qWait(180)
                opened.assert_not_called()
                self.assertFalse(bridge.openSystemSettings('file:///bad'))
                wifi=status_button('wifi')
                self.assertEqual(wifi.property('text'),'Wi-Fi: вкл.')
                point=wifi.mapToScene(QPointF(wifi.width()/2,wifi.height()/2)).toPoint()
                QTest.mouseClick(window,Qt.MouseButton.LeftButton,pos=point)
                self.assertEqual(window.findChild(QObject, 'systemPanel').property('kind'), 'wifi')
                bridge.action.emit('back'); QTest.qWait(180)
                opened.assert_not_called()
            bridge.system_status.apply(dict(previous,wifi='unknown',bluetooth='absent',volume=-1,battery=-1,has_battery=True))
            self.assertIn('?',status_button('wifi').property('text'))
            self.assertIn('нет',status_button('bluetooth').property('text'))
            bridge.settings['ui_scale'] = 1.2
            bridge.settingsChanged.emit()
            for width,height in ((1280,720),(1280,800),(1920,1080)):
                window.resize(width,height); QTest.qWait(60)
                for key in ('clock','wifi','bluetooth','battery','volume'):
                    button=status_button(key)
                    top=button.mapToScene(QPointF(0,0));end=button.mapToScene(QPointF(button.width(),button.height()))
                    self.assertGreaterEqual(top.x(),0)
                    self.assertLessEqual(end.x(),width)
                    self.assertLess(end.y(),window.findChild(QObject,'headerNavigation').mapToScene(QPointF(0,0)).y())
                if height == 800:
                    self.assertTrue(window.grabWindow().save(str(self.repo/'reports/status-bar-120.png')))
            window.setProperty('gameInProgress',True)
            self.assertFalse(timer.property('running'))
            window.setProperty('gameInProgress',False)
        finally:
            polling.stop()
            bridge.systemSettingsFocus.emit(False)
            bridge.settings['ui_scale'] = old_scale
            bridge.settingsChanged.emit()
            bridge.system_status.apply(previous)
            window.setProperty('gameInProgress',False)
            window.resize(1280,800); window.setProperty('focusZone',2)
    def test_install_destination_buttons_follow_horizontal_gamepad_navigation(self):
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject, 'installDestination')
        folders = pane.findChild(QObject, 'destinationFolders')
        previous = bridge._install_preview
        try:
            with patch.object(bridge, 'previewInstall'), patch.object(bridge, 'queueDownload') as queued:
                pane.setProperty('gameId', 'navigation-test')
                pane.setProperty('visible', True); QTest.qWait(150)
                bridge._install_preview = dict(game_id='navigation-test', directory=folders.property('currentText'),
                    busy=False, can_install=True, status='Ready')
                bridge.installPreviewChanged.emit()
                pane.setProperty('selectedField', 1)
                self.engine.gamepad.action.emit('right'); self.assertEqual(pane.property('selectedField'), 3)
                self.engine.gamepad.action.emit('left'); self.assertEqual(pane.property('selectedField'), 1)
                queued.assert_not_called()
                bridge._install_preview['busy'] = True; bridge.installPreviewChanged.emit()
                pane.setProperty('selectedField', 1)
                self.engine.gamepad.action.emit('right'); self.assertEqual(pane.property('selectedField'), 3)
        finally:
            pane.setProperty('visible', False); bridge._install_preview = previous; bridge.installPreviewChanged.emit()

    def test_manual_catalog_card_editor_returns_to_review_and_saves_without_publishing(self):
        import copy
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        remote = bridge.catalogizer
        previous = copy.deepcopy(remote._state)
        pane = window.findChild(QObject, 'catalogizerPane')
        editor = window.findChild(QObject, 'manualCardEditor')
        original = pane.property('bridge')
        try:
            pane.setProperty('bridge', remote); pane.setProperty('visible', True)
            remote._state.update(stage='review', busy=False, manual=True, preview='ab'*16,
                card=dict(name='Manual',description='Original'), artwork={})
            remote.changed.emit(); pane.setProperty('actionIndex', 1)
            self.engine.gamepad.action.emit('activate'); QTest.qWait(100)
            self.assertTrue(editor.property('visible'))
            editor.findChild(QObject, 'manualCardTitle').setProperty('text', 'Custom title')
            editor.findChild(QObject, 'manualCardDescription').setProperty('text', 'Custom description')
            QTest.qWait(100)
            self.assertTrue(window.grabWindow().save(str(self.repo/'reports/manual-card-editor.png')))
            editor.setProperty('selectedField', 4)
            with patch.object(remote, 'saveCustomCard') as save, patch.object(remote, 'publish') as publish:
                self.engine.gamepad.action.emit('activate')
                save.assert_called_once_with('Custom title', 'Custom description', '', '')
                publish.assert_not_called()
            self.engine.gamepad.action.emit('back'); QTest.qWait(200)
            self.assertFalse(editor.property('visible'))
            self.assertTrue(pane.property('visible'))
            self.assertEqual(remote.state['stage'], 'review')
        finally:
            editor.setProperty('visible', False); pane.setProperty('visible', False); pane.setProperty('bridge', original)
            remote._state = previous; remote.changed.emit()

    def test_catalog_size_is_visible_before_copy_and_preview_works_offline(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from ngs.catalog import CatalogCache
        bridge, window = self.engine.bridge, self.window
        previous, previous_cache = list(bridge._games), bridge.cache
        popup = window.findChild(QObject, 'installDestination')
        details = window.findChild(QObject, 'detailsPopup')
        card = dict(previous[0], delivery='on_request', total_size=3 * 1024**3)
        try:
            bridge.cache = CatalogCache(self.storage, 'http://127.0.0.1:9/tactic')
            bridge.catalog_loaded([card])
            window.setProperty('section', 1); window.setProperty('selectedIndex', 0)
            QTest.qWait(350)
            label = window.findChild(QObject, 'heroGameSize')
            self.assertIn('3,00 ГиБ', label.property('text'))
            details.setProperty('visible', True); QTest.qWait(50)
            self.assertIn('3,00 ГиБ', window.findChild(QObject, 'detailsGameSize').property('text'))
            details.setProperty('visible', False)
            with patch('ngs.api.MetadataApi', side_effect=AssertionError('Network used')), \
                 patch('ngs.app.fetch_manifest', side_effect=AssertionError('Manifest used')), \
                 patch('ngs.app.shutil.disk_usage', return_value=SimpleNamespace(free=10 * 1024**3)):
                bridge.performGameAction(card['id'])
                self.assertEqual(bridge.installPreview['size'], card['total_size'])
                for unused in range(200):
                    QTest.qWait(10)
                    if not bridge.installPreview['busy']: break
                self.assertTrue(bridge.installPreview['can_install'])
                self.assertIn('3,00 ГиБ', window.findChild(QObject, 'installGameSize').property('text'))
                self.assertTrue(window.findChild(QObject, 'confirmDestination').property('enabled'))
                QTest.qWait(350)
                self.assertTrue(window.grabWindow().save(str(self.repo/'reports/catalog-size-preview.png')))
        finally:
            popup.setProperty('visible', False); details.setProperty('visible', False)
            bridge.cancelInstallPreview()
            for unused in range(200):
                QTest.qWait(10)
                if bridge.preview_worker is None: break
            bridge.cache = previous_cache; bridge.catalog_loaded(previous)
            window.setProperty('selectedIndex', 0)

    def test_download_rates_use_separate_byte_counters_and_stop_on_pause(self):
        from types import SimpleNamespace
        from threading import Event
        from unittest.mock import patch
        bridge = self.engine.bridge
        previous = bridge._download_statistics
        bridge.download_timer.stop()
        try:
            bridge.game_worker = SimpleNamespace(mode="install", cancel=Event())
            bridge._io_active = True
            bridge._io_time = 10
            bridge._io_sample = [0, 0]
            bridge._download_statistics = dict(game="test", network=0.0, disk=0.0, history=[])
            bridge.download_io_updated(2 * 1048576, 8 * 1048576)
            with patch("ngs.app.time.monotonic", return_value=11): bridge.sample_download_io()
            self.assertEqual(bridge.downloadStatistics["network"], 2 * 1048576)
            self.assertEqual(bridge.downloadStatistics["disk"], 8 * 1048576)
            bridge.game_worker.cancel.set()
            with patch("ngs.app.time.monotonic", return_value=12): bridge.sample_download_io()
            self.assertEqual(bridge.downloadStatistics["network"], 0)
            self.assertEqual(bridge.downloadStatistics["disk"], 0)
            bridge.game_worker.cancel.clear()
            for second in range(13, 80):
                with patch("ngs.app.time.monotonic", return_value=second): bridge.sample_download_io()
            self.assertEqual(len(bridge.downloadStatistics["history"]), 60)
            bridge.downloadStatisticsChanged.emit()
            QTest.qWait(30)
            self.assertIsNotNone(self.window.findChild(QObject, "downloadSpeedGraph"))
        finally:
            bridge.game_worker = None
            bridge._io_active = False
            bridge._download_statistics = previous
            bridge.downloadStatisticsChanged.emit()
            bridge.download_timer.start()

    def test_catalogizer_b_returns_stepwise_and_status_updates_preserve_gamepad_focus(self):
        from unittest.mock import Mock, patch
        from ngs.catalogizer_ui import RemoteCatalogBridge
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject, 'catalogizerPane')
        original = pane.property('bridge')
        try:
            for installers in (False, True):
                remote = RemoteCatalogBridge(self.storage, Mock(), window, installers=installers)
                remote.request = Mock()
                pane.setProperty('bridge', remote); pane.setProperty('visible', True)
                remote.loaded(dict(stage='folders', rows=[dict(name='One', folder='One')]))
                bridge.action.emit('activate')
                remote.loaded(dict(stage='exes', folder='One', rows=[dict(path='one.exe'), dict(path='two.exe')]))
                bridge.action.emit('down'); bridge.action.emit('activate')
                remote.loaded(dict(stage='matches', rows=[dict(name='Game', app_id='1')]))
                bridge.action.emit('activate')
                remote.loaded(dict(stage='review', preview='ab'*16, card=dict(name='Game')))
                with patch.object(remote, 'publish') as publish:
                    bridge.action.emit('refresh'); publish.assert_not_called()
                remote.request.reset_mock()
                for stage, index in (('matches', 0), ('exes', 1), ('folders', 0)):
                    self.engine.gamepad.action.emit('back')
                    self.assertTrue(pane.property('visible'))
                    self.assertEqual(remote.state['stage'], stage)
                    self.assertEqual(pane.property('currentIndex'), index)
                remote.request.assert_not_called()
                pane.setProperty('actionIndex', -1)
                bridge.action.emit('right'); self.assertEqual(pane.property('actionIndex'), 0)
                bridge.action.emit('right'); self.assertEqual(pane.property('actionIndex'), 1)
                remote.changed.emit()
                self.assertEqual(pane.property('actionIndex'), 1)
                remote._state['busy'] = True; remote.changed.emit()
                self.engine.gamepad.action.emit('back')
                self.assertTrue(pane.property('visible'))
                remote._state['busy'] = False; remote.changed.emit()
                self.engine.gamepad.action.emit('back'); QTest.qWait(250)
                self.assertFalse(pane.property('visible'))
                remote.stop(); remote.deleteLater()
        finally:
            pane.setProperty('visible', False); pane.setProperty('bridge', original)

    def test_catalogizer_filter_bulk_button_and_manual_result_work_from_gamepad(self):
        import copy
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        remote = bridge.catalogizer
        previous = copy.deepcopy(remote._state)
        pane = window.findChild(QObject, 'catalogizerPane')
        try:
            pane.setProperty('visible', True)
            remote._state.update(stage='folders', rows=[], busy=False, hide_published=True)
            remote.changed.emit(); QTest.qWait(50)
            with patch.object(remote, 'batch') as batch, patch.object(remote, 'setHidePublished') as hidden:
                pane.setProperty('actionIndex', 0)
                for unused in range(3): bridge.action.emit('right')
                bridge.action.emit('activate'); batch.assert_called_once()
                bridge.action.emit('toggle_installed'); hidden.assert_called_once_with(False)
            remote._state.update(stage='done', rows=[dict(name='Check', folder='Check', state='manual')],
                                 result_total=1, summary=dict(added=3,already=1,manual=1,error=0))
            remote.changed.emit(); QTest.qWait(50)
            self.assertEqual(pane.property('actionIndex'), -1)
            with patch.object(remote, 'reviewFolder') as review:
                bridge.action.emit('activate'); review.assert_called_once_with('Check')
        finally:
            remote._state = previous; remote.changed.emit()
            pane.setProperty('visible', False); QTest.qWait(50)

    def test_catalogizer_publication_returns_to_batch_selection_without_rescanning(self):
        from unittest.mock import Mock
        from ngs.catalogizer_ui import RemoteCatalogBridge
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject, 'catalogizerPane')
        remote = RemoteCatalogBridge(self.storage, Mock(), bridge)
        remote.request = Mock()
        remote.openServer('http://127.0.0.1:8888/tactic')
        previous = pane.property('bridge')
        try:
            pane.setProperty('bridge', remote)
            QMetaObject.invokeMethod(pane, 'open')
            remote.loaded(dict(stage='done', state='done', preview='ab'*16, rows=[dict(name=f, folder=f, state='manual') for f in ('One','Two','Three')],
                               result_offset=20, result_total=45, summary=dict(added=0, already=0, manual=45, error=0)))
            QTest.qWait(50)
            bridge.action.emit('down'); bridge.action.emit('activate')
            remote.request.assert_called_with(dict(action='exes', folder='Two'))
            remote.loaded(dict(stage='exes', folder='Two', rows=[dict(path='play.exe')]))
            remote._state['executable'] = 'play.exe'
            remote.loaded(dict(stage='review', preview='cd'*16, card=dict(id='two', name='Two')))
            remote.loaded(dict(stage='working', state='export', preview='cd'*16))
            remote.loaded(dict(stage='done', state='done', preview='cd'*16, rows=[], card=dict(id='two')))
            remote.request.reset_mock(); QTest.qWait(50)
            bridge.action.emit('activate'); QTest.qWait(50)
            self.assertEqual((remote.state['stage'], remote.state['preview'], remote.state['result_offset']), ('done', 'ab'*16, 20))
            self.assertEqual(pane.property('currentIndex'), 1)
            self.assertEqual([r['folder'] for r in pane.property('displayRows').toVariant()], ['One', 'Three'])
            remote.request.assert_not_called()
            QMetaObject.invokeMethod(pane, 'close'); QTest.qWait(200)
            remote.openServer(remote.url)
            self.assertEqual(remote.state['preview'], 'ab'*16)
            remote.request.assert_not_called()
        finally:
            remote.stop(); remote.timer.stop()
            pane.setProperty('visible', False); pane.setProperty('bridge', previous)
            remote.deleteLater(); QTest.qWait(50)

    def test_catalogizer_keeps_actions_inside_at_every_stage_and_scale(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        remote = bridge.catalogizer
        previous = copy.deepcopy(remote._state), copy.deepcopy(bridge.settings)
        pane = window.findChild(QObject, "catalogizerPane")
        actions = pane.findChild(QObject, "catalogizerActions")
        rows = pane.findChild(QObject, "catalogizerRows")
        try:
            window.showNormal()
            pane.setProperty("visible", True)
            QTest.qWait(420)
            for width, height in ((960, 540), (1280, 720), (1280, 800), (1920, 1080)):
                window.resize(width, height)
                for scale in (.8, 1, 1.2):
                    bridge.settings["ui_scale"] = scale
                    bridge.settingsChanged.emit()
                    for stage in ("folders", "exes", "matches", "review", "working", "batch", "done"):
                        bottoms = []
                        for busy in (False, True):
                            remote._state = dict(previous[0], stage=stage, busy=busy,
                                rows=[dict(name="Длинное название игры " * 8)] * 20,
                                folder="Папка игры " * 15, executable="game.exe",
                                status="Подготовка пакетов и публикация каталога. " * 20,
                                card=dict(name="Название игры " * 8, description="Описание игры. " * 500))
                            remote.changed.emit()
                            QTest.qWait(40)
                            bottoms.append(actions.mapToScene(QPointF(0, actions.height())).y())
                            with self.subTest(size=(width, height), scale=scale, stage=stage, busy=busy):
                                buttons = [item for item in actions.childItems() if item.property("text")]
                                self.assertTrue(buttons)
                                for button in buttons:
                                    start = button.mapToScene(QPointF(0, 0))
                                    end = button.mapToScene(QPointF(button.width(), button.height()))
                                    limit = actions.mapToScene(QPointF(0, 0)).y() + actions.height()
                                    self.assertGreaterEqual(start.x(), 0)
                                    self.assertLessEqual(end.x(), window.width())
                                    self.assertLessEqual(end.y(), limit + 1)
                                    self.assertLessEqual(end.y(), window.height() - pane.property("padding") + 1,
                                        dict(popup_scale=pane.property('scale'), popup_y=pane.property('y'), popup_height=pane.property('height'),
                                             available=pane.property('availableHeight'), content_height=pane.property('contentItem').height(),
                                             actions_y=actions.y(), actions_height=actions.height(), button_height=button.height(), compression=button.property('compression')))
                                if stage in ("folders", "exes", "matches"):
                                    self.assertGreater(rows.height(), 40 * pane.property("uiScale"))
                                    self.assertLess(rows.mapToScene(QPointF(0, rows.height())).y(),
                                                    actions.mapToScene(QPointF(0, 0)).y())
                        self.assertAlmostEqual(*bottoms, delta=1)
                    if (width, height, scale) == (1280, 720, 1.2):
                        remote._state.update(stage="matches", busy=True)
                        remote.changed.emit(); QTest.qWait(80)
                        self.assertTrue(window.grabWindow().save(str(self.repo / "reports/catalogizer-layout-720.png")))
        finally:
            remote._state, bridge.settings = previous
            remote.changed.emit(); bridge.settingsChanged.emit()
            pane.setProperty("visible", False)
            window.resize(1280, 800)
            QTest.qWait(100)

    def test_bc_remote_catalogizer_gamepad_flow_and_signed_api_without_smb(self):
        import copy, base64
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage, QColor
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        remote = bridge.catalogizer
        previous = copy.deepcopy(remote._state)
        pane = window.findChild(QObject, "catalogizerPane")
        settings = window.findChild(QObject, "settingsPane")
        calls = []
        artwork = {}
        for context, size, color in (('cover',(160,240),'#e06040'),('background',(640,360),'#40a0e0')):
            image=QImage(*size,QImage.Format_RGB32);image.fill(QColor(color))
            stream=QBuffer();stream.open(QIODevice.WriteOnly);self.assertTrue(image.save(stream,'JPEG'))
            artwork[context]='data:image/jpeg;base64,'+base64.b64encode(bytes(stream.data())).decode()
        def reply(operation,value):
            self.assertEqual(operation,"catalog");calls.append(value)
            action=value["action"]
            if action=="folders":return dict(stage="folders",offset=0,total=1,rows=[dict(name="Game",folder="Game")])
            if action=="exes":return dict(stage="exes",folder="Game",rows=[dict(path="play.exe",utility=False)])
            if action=="search":return dict(stage="matches",rows=[dict(name="Game",app_id="12")])
            if action=="preview":return dict(stage="review",preview="ab"*16,card=dict(name="Game",description="Description"),artwork=artwork)
            if action=="publish":return dict(stage="working",preview="ab"*16,state="working")
            return dict(stage="done",state="done",preview="ab"*16)
        def settled():
            for unused in range(300):
                QTest.qWait(10)
                if remote.worker is None:return
            self.fail("Catalogizer worker did not finish")
        try:
            with patch("ngs.catalogizer_ui.MetadataApi") as api,patch.object(remote,"published") as published:
                api.return_value.call.side_effect=reply
                bridge.action.emit("settings");QTest.qWait(360)
                settings.setProperty('selectedField', 9)
                self.assertEqual(settings.property("selectedField"),9)
                bridge.action.emit("activate");settled()
                self.assertTrue(pane.property("visible"))
                for stage in ("exes","matches","review"):
                    bridge.action.emit("activate");settled()
                    self.assertEqual(remote.state["stage"],stage)
                    QTest.qWait(40)
                    actions = pane.findChild(QObject,"catalogizerActions")
                    buttons = [item for item in actions.childItems() if item.property("text")]
                    self.assertTrue(buttons)
                    for button in buttons:
                        end = button.mapToScene(QPointF(button.property("width"),button.property("height")))
                        self.assertLessEqual(end.y(),window.height())
                for context, ratio in (('cover',2/3),('background',16/9)):
                    image=pane.findChild(QObject,'catalogizerPreview-'+context)
                    for unused in range(100):
                        if image.property('paintedWidth')>0:break
                        QTest.qWait(10)
                    self.assertGreater(image.property('paintedWidth'),0)
                    self.assertAlmostEqual(image.property('paintedWidth')/image.property('paintedHeight'),ratio,delta=.01)
                self.assertGreater(pane.findChild(QObject,'catalogizerPreview-background').property('paintedWidth'),
                                   2 * pane.findChild(QObject,'catalogizerPreview-cover').property('paintedWidth'))
                QTest.qWait(300)
                self.assertTrue(window.grabWindow().save(str(self.repo/'reports/catalogizer-artwork-preview.png')))
                bridge.action.emit("activate");settled()
                self.assertTrue(remote.pending.is_file())
                remote.timer.stop();remote.poll();settled()
                self.assertEqual(remote.state["stage"],"done")
                self.assertEqual(remote.state['artwork'],{})
                self.assertFalse(remote.pending.exists());published.assert_called_once()
                self.assertEqual([value["action"] for value in calls],["folders","exes","search","preview","publish","status"])
                self.assertTrue(window.grabWindow().save(str(self.repo/"reports/remote-catalogizer.png")))
        finally:
            settled();remote.timer.stop();pane.setProperty("visible",False);settings.setProperty("visible",False)
            if remote.pending:remote.pending.unlink(missing_ok=True)
            remote._state=previous;remote.changed.emit();QTest.qWait(200)

    def test_launch_flight_starts_worker_immediately_keeps_art_and_clears_on_failure(self):
        import sys, threading
        from unittest.mock import Mock, patch
        bridge, window = self.engine.bridge, self.window
        folder = self.root / 'flight-game'; folder.mkdir()
        shutil.copyfile(sys.executable, folder / 'play.exe')
        receipt = bridge.library.register_external(folder, 'play.exe', 'Flight game')
        bridge._installed = bridge.library.reconcile(); bridge.libraryChanged.emit()
        identity = receipt['card']['id']
        previous_running = bridge._running_id, bridge._running_name
        bridge._running_id,bridge._running_name='previous-game','Previous game'
        bridge.runningChanged.emit()
        window.setProperty('section', 0)
        window.setProperty('selectedIndex', next(i for i,c in enumerate(bridge.localGames) if c['id']==identity))
        QTest.qWait(400)
        selected = window.property('selected')
        motion = window.findChild(QObject, 'launchMotion')
        entered, release = threading.Event(), threading.Event()
        fake = Mock(frozen=False, _foregrounded=True, _foreground_window=None, _edge_configured=None); fake.active.return_value = True; fake.exit_code.return_value = 0
        fake.windows.return_value = []
        def specification(*args):
            entered.set()
            if not release.wait(3): raise OSError('Fixture launch gate timed out')
            return folder/'play.exe', folder, []
        try:
            def create_process(*args):
                self.assertFalse(motion.property('running'))
                self.assertFalse(window.property('launchPending'))
                self.assertTrue(window.isVisible())
                self.assertEqual(window.findChild(QObject,'runningGameTitle').property('text'), 'Flight game')
                self.assertEqual(window.findChild(QObject,'playingBackground').property('contentReveal'), 1)
                self.assertTrue(bridge._launch_presented)
                return fake
            with patch.object(bridge.library, 'launch_spec', side_effect=specification), patch('ngs.app.GameProcess', side_effect=create_process) as opened:
                QMetaObject.invokeMethod(window, 'runSelected')
                self.assertTrue(entered.wait(1), 'Launch waited for the visual effect')
                self.assertTrue(motion.property('running'))
                self.assertTrue(window.property('launchPending'))
                self.assertEqual(window.findChild(QObject,'runningGameTitle').property('text'),'Flight game')
                opened.assert_not_called()
                QMetaObject.invokeMethod(window, 'runSelected')
                release.set()
                for _ in range(150):
                    QTest.qWait(10)
                    if not bridge.operationBusy and not window.property('launchPending'): break
                opened.assert_called_once()
                self.assertTrue(window.property('gameInProgress'))
                self.assertFalse(window.property('launchPending'))
                self.assertFalse(motion.property('running'))
                self.assertEqual(window.findChild(QObject,'runningGameTitle').property('text'),'Flight game')
                self.assertEqual(window.findChild(QObject,'runningGameCover').property('source').toString(), selected['image'])
                window.setProperty('section',1); window.setProperty('selectedIndex',0)
                QTest.qWait(100)
                self.assertEqual(window.findChild(QObject,'runningGameTitle').property('text'),'Flight game')
                self.assertEqual(window.findChild(QObject,'runningGameCover').property('source').toString(), selected['image'])
                window.setProperty('launchPending',True)
                motion.setProperty('running',True);motion.setProperty('paused',True)
                bridge.gamePanelRequested.emit()
                self.assertFalse(window.property('launchPending'))
                self.assertFalse(motion.property('running'))
                fake.active.return_value = False; bridge.check_game()
            window.setProperty('section',0)
            window.setProperty('selectedIndex',next(i for i,c in enumerate(bridge.localGames) if c['id']==identity))
            with patch.object(bridge.library,'launch_spec',side_effect=OSError('Fixture missing EXE')):
                QMetaObject.invokeMethod(window,'runSelected')
                for _ in range(150):
                    QTest.qWait(10)
                    if not bridge.operationBusy: break
                self.assertFalse(window.property('launchPending'))
                self.assertFalse(motion.property('running'))
                self.assertFalse(window.property('gameInProgress'))
                self.assertIn('Fixture missing EXE', bridge.operationStatus)
        finally:
            release.set()
            if bridge.process is not None:
                fake.active.return_value=False; bridge.check_game()
            bridge.library.forget(identity)
            bridge._running_id,bridge._running_name=previous_running;bridge.runningChanged.emit()
            bridge._installed=bridge.library.reconcile();bridge.libraryChanged.emit()
            window.setProperty('section',1);window.setProperty('selectedIndex',0)
            QTest.qWait(200)

    def test_running_summary_icons_hints_shortcuts_and_close_confirmation(self):
        from unittest.mock import Mock
        bridge, window = self.engine.bridge, self.window
        previous_device = bridge._input_device
        fake = Mock(frozen=True)
        def item(root, name):
            if root.objectName() == name: return root
            for child in root.childItems():
                found = item(child, name)
                if found is not None: return found
        try:
            bridge.process = fake; bridge.runningChanged.emit(); bridge.gameStarted.emit(); bridge.freezeChanged.emit()
            QTest.qWait(550)
            page = window.findChild(QObject, 'frozenDashboard')
            title = page.findChild(QObject, 'frozenGameTitle')
            poster = page.findChild(QObject, 'frozenGameCover')
            stats = page.findChild(QObject, 'runningStatistics')
            self.assertIsNone(page.findChild(QObject, 'frozenGameBackground'))
            self.assertGreater(title.mapToScene(QPointF()).x(), poster.mapToScene(QPointF()).x() + poster.width())
            self.assertGreater(stats.mapToScene(QPointF()).y(), title.mapToScene(QPointF()).y() + title.height())
            for device, accept, toggle, minimize in [('keyboard','Enter','X','R'), ('gamepad','A','X','Y')]:
                bridge.set_input_device(device); page.setProperty('selectedAction', 0)
                self.assertEqual(item(page, 'runningActionHint-0').property('text'), accept)
                self.assertEqual(item(page, 'runningActionHint-1').property('text'), toggle)
                self.assertEqual(item(page, 'runningActionHint-2').property('text'), minimize)
                for index in range(6):
                    icon = item(page, 'runningActionIcon-'+str(index))
                    self.assertTrue(icon.isVisible())
                    self.assertTrue(icon.property('keyName').startswith('@'))
                page.setProperty('selectedAction', 5)
                self.assertEqual(item(page, 'runningActionHint-5').property('text'), accept)
            bridge.action.emit('toggle_installed')
            self.assertEqual(page.property('selectedAction'), 1, (page.property('confirming'), bridge.runningState, window.findChild(QObject,'frozenScreen').property('visible')))
            fake.thaw.assert_called_once()
            parked = fake.park_windows.call_count
            bridge.action.emit('refresh'); self.assertGreater(fake.park_windows.call_count, parked)
            parked = fake.park_windows.call_count
            bridge._memory_worker = object(); bridge.runningChanged.emit()
            bridge.action.emit('toggle_installed'); bridge.action.emit('refresh')
            self.assertEqual(fake.thaw.call_count, 1); self.assertEqual(fake.park_windows.call_count, parked)
            bridge._memory_worker = None; bridge.runningChanged.emit()
            page.setProperty('selectedAction', 3); bridge.action.emit('activate')
            self.assertTrue(page.property('confirming')); fake.terminate.assert_not_called()
            bridge.action.emit('toggle_installed'); bridge.action.emit('refresh')
            self.assertEqual(fake.thaw.call_count, 1); self.assertEqual(fake.park_windows.call_count, parked)
            bridge.action.emit('back'); self.assertFalse(page.property('confirming'))
            page.setProperty('selectedAction', 3); bridge.action.emit('activate')
            bridge.action.emit('right'); bridge.action.emit('activate'); fake.terminate.assert_called_once()
        finally:
            bridge._memory_worker = None; bridge._terminating = False
            bridge.process = None; bridge.freezeChanged.emit(); bridge.gameReturned.emit()
            bridge.set_input_device(previous_device); QTest.qWait(200)

    def test_details_uses_wide_art_and_keeps_both_edges_visible(self):
        from PySide6.QtGui import QColor, QImage, QPainter
        bridge, window = self.engine.bridge, self.window
        previous = bridge._games.copy()
        popup = window.findChild(QObject, "detailsPopup")
        banner = window.findChild(QObject, "detailsBanner")
        wide, cover = self.root / 'wide-banner.png', self.root / 'portrait-cover.png'
        picture = QImage(1920, 620, QImage.Format.Format_RGB32)
        picture.fill(QColor('#223355'))
        painter = QPainter(picture)
        painter.fillRect(0, 0, 60, 620, QColor('#f04040'))
        painter.fillRect(1860, 0, 60, 620, QColor('#40e060'))
        painter.end()
        self.assertTrue(picture.save(str(wide)))
        picture = QImage(300, 450, QImage.Format.Format_RGB32)
        picture.fill(QColor('#ffdd44'))
        self.assertTrue(picture.save(str(cover)))
        card = dict(previous[0], image=cover.as_uri(), backgroundImage=wide.as_uri())
        try:
            window.setProperty('section', 1)
            bridge.catalog_loaded([card])
            window.setProperty('selectedIndex', 0)
            window.setProperty('focusZone', 1)
            window.setProperty('heroAction', 1)
            bridge.action.emit('activate')
            QTest.qWait(450)
            self.assertTrue(popup.property('visible'))
            self.assertEqual(Path(banner.property('source').toLocalFile()), wide)
            origin = banner.mapToScene(QPointF())
            self.assertGreaterEqual(origin.x(), 18)
            self.assertGreaterEqual(origin.y(), 18)
            self.assertLessEqual(origin.x() + banner.width(), popup.property('width') - 18)
            self.assertGreater(banner.width(), window.width() * .5)
            self.assertTrue(banner.property('clip'))
            corner = banner.mapToScene(QPointF(-10, -10))
            pixel = window.grabWindow().pixelColor(round(corner.x()), round(corner.y()))
            self.assertEqual(pixel.alpha(), 255)
            self.assertGreater(abs(pixel.red()-240)+abs(pixel.green()-64)+abs(pixel.blue()-64), 120)
            frame = window.grabWindow()
            width = banner.property('paintedWidth')
            left = (banner.width() - width) / 2
            for x, expected in ((left + width / 64, '#f04040'), (left + width * 63 / 64, '#40e060')):
                point = banner.mapToScene(QPointF(x, banner.height() / 2))
                pixel = frame.pixelColor(round(point.x() * frame.devicePixelRatio()),
                                         round(point.y() * frame.devicePixelRatio()))
                expected_color = QColor(expected)
                for channel in ('red', 'green', 'blue'):
                    self.assertAlmostEqual(getattr(pixel, channel)(), getattr(expected_color, channel)(), delta=1)
            bridge.catalog_loaded([dict(card, backgroundImage='')])
            QMetaObject.invokeMethod(popup, 'open')
            QTest.qWait(300)
            self.assertEqual(Path(banner.property('source').toLocalFile()), cover)
        finally:
            popup.setProperty('visible', False)
            bridge.catalog_loaded(previous)
            window.setProperty('selectedIndex', 0)
            window.setProperty('focusZone', 2)
            QTest.qWait(250)

    def test_metadata_loading_shows_elapsed_phases_and_cancel_instead_of_silent_wait(self):
        import copy, json, threading
        from unittest.mock import patch
        from ngs.install import Cancelled
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge._metadata), copy.deepcopy(bridge._scanner)
        picker = window.findChild(QObject, "metadataPicker")
        loading = window.findChild(QObject, "metadataLoading")
        spinner = window.findChild(QObject, "metadataSpinner")
        status = window.findChild(QObject, "metadataLoadingStatus")
        first, second = threading.Event(), threading.Event()
        card = json.loads((self.repo / 'examples/black-mesa.json').read_text(encoding='utf8'))
        image = dict(path='/assets/ngs_launcher/test.jpg', sha256='0' * 64, size=1)
        reference = 'lutris:known-game'
        card.update(id='lutris-known-game', name='Known Game', source_folder='', executable='', available_on_nas=False,
                    revision=1, cover=image, background=image)
        def metadata(storage, url, operation, value, cancel, progress=None):
            if operation == 'search':
                return [dict(app_id=reference, name=card['name'])]
            self.assertEqual(value, reference)
            progress('metadata_server', 0, 0, 'TACTIC receives description and images')
            if not first.wait(8): raise TimeoutError('fixture not released')
            if cancel.is_set(): raise Cancelled()
            progress('metadata_artwork', 0, 2, 'Saving cover from NAS')
            if not second.wait(8): raise TimeoutError('fixture not released')
            if cancel.is_set(): raise Cancelled()
            return dict(card=card, message='Saved')
        def until(predicate):
            for unused in range(400):
                QTest.qWait(10)
                if predicate(): return
            self.fail('Metadata UI did not update')
        try:
            bridge._scanner = dict(bridge._scanner, path=str(self.root / 'Known Game'), stage='confirm')
            with patch('ngs.app.client_metadata', side_effect=metadata):
                bridge.findMetadata('')
                until(lambda: not bridge.operationBusy)
                QTest.qWait(350)
                bridge.action.emit('activate')
                until(lambda: 'TACTIC receives' in status.property('text'))
                self.assertTrue(loading.property('visible'))
                self.assertTrue(spinner.property('running'))
                QTest.qWait(1100)
                self.assertGreaterEqual(picker.property('elapsedSeconds'), 1)
                back = window.findChild(QObject, 'metadataBack')
                point = back.mapToScene(QPointF(back.width() / 2, back.height() / 2)).toPoint()
                self.assertGreaterEqual(point.y(), 0)
                self.assertLess(point.y(), window.height())
                self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/metadata-loading.png')))
                bridge.action.emit('back')
                self.assertIn('Отменяем', status.property('text'))
                self.assertFalse(back.property('enabled'))
                first.set()
                until(lambda: not bridge.operationBusy)
                self.assertFalse(spinner.property('running'))
                self.assertEqual(bridge.scannerState['matches'], previous[1]['matches'])
                first.clear()
                bridge.resolveMetadata(reference)
                until(lambda: 'TACTIC receives' in status.property('text'))
                self.assertEqual(picker.property('elapsedSeconds'), 0)
                first.set()
                until(lambda: 'Saving cover' in status.property('text'))
                second.set()
                until(lambda: not bridge.operationBusy)
                QTest.qWait(200)
                self.assertFalse(picker.property('visible'))
                self.assertFalse(spinner.property('running'))
                self.assertEqual(bridge.scannerState['matches'][0]['name'], card['name'])
        finally:
            first.set(); second.set()
            until(lambda: not bridge.operationBusy)
            picker.setProperty('visible', False)
            bridge._metadata, bridge._scanner = previous
            bridge.metadataChanged.emit(); bridge.scannerChanged.emit()
            QTest.qWait(200)

    def test_d_autostart_gamepad_touch_errors_and_reopening(self):
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject, "settingsPane")
        toggle = pane.findChild(QObject, "settingsAutostart")
        previous = bridge._autostart, bridge._settings_status
        state = [False]
        def register(root, enable):
            self.assertEqual(root, self.storage.root)
            state[0] = enable
        try:
            with patch("ngs.app.startup.supported", return_value=True), \
                 patch("ngs.app.startup.enabled", side_effect=lambda root: state[0]), \
                 patch("ngs.app.startup.set_enabled", side_effect=register) as write:
                bridge.action.emit("settings")
                QTest.qWait(350)
                for unused in range(4):
                    self.engine.gamepad.action.emit("down")
                self.engine.gamepad.action.emit("activate")
                self.assertEqual(pane.property("selectedField"), 5)
                self.engine.gamepad.action.emit("activate")
                self.assertTrue(toggle.property("checked"), (write.call_args_list, state, bridge.settingsStatus,
                                                             toggle.property("enabled")))
                write.assert_called_once_with(self.storage.root, True)
                QTest.qWait(80)
                point = toggle.mapToScene(QPointF(toggle.width() / 2, toggle.height() / 2)).toPoint()
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
                self.assertFalse(toggle.property("checked"))
                self.assertFalse(state[0])
                write.side_effect = PermissionError("denied")
                self.engine.gamepad.action.emit("activate")
                self.assertFalse(toggle.property("checked"))
                self.assertIn("denied", bridge.settingsStatus)
                bridge.action.emit("menu")
                QTest.qWait(200)
                state[0] = True
                bridge.action.emit("settings")
                QTest.qWait(350)
                self.assertTrue(toggle.property("checked"))
        finally:
            pane.setProperty("visible", False)
            bridge._autostart, bridge._settings_status = previous
            bridge.settingsChanged.emit()
            QTest.qWait(200)

    def test_xb_bulk_collection_controller_import_and_manual_remainder(self):
        import copy, json, struct
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        previous, settings = bridge._games.copy(), copy.deepcopy(bridge.settings)
        previous_scanner = copy.deepcopy(bridge._scanner)
        known_before = set(bridge.externalIds)
        collection = self.root / "bulk collection"
        collection.mkdir()
        data = bytearray(68); data[:2] = b"MZ"; struct.pack_into("<I", data, 60, 64); data[64:] = b"PE\0\0"
        for name, exes in (("Bulk Known", ["play.exe"]), ("Needs Review", ["one.exe", "two.exe"])):
            folder = collection / name; folder.mkdir()
            for exe in exes:
                (folder / exe).write_bytes(data)
        card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = dict(path="/assets/ngs_launcher/art.jpg", size=1, sha256="0" * 64)
        card.update(id="bulk-known", name="Bulk Known", source_folder="Bulk Known", executable="play.exe",
                    cover=image, background=image, revision=1)
        scanner = window.findChild(QObject, "localScanner")
        def settled():
            for _ in range(500):
                QTest.qWait(10)
                if not bridge.operationBusy: return
            self.fail(bridge.operationStatus)
        try:
            bridge.settings["resolve_local_metadata"] = False
            bridge.catalog_loaded([dict(card, image="", backgroundImage="")])
            scanner.setProperty("visible", True)
            bridge.browseLocalFolder(str(collection)); settled()
            scanner.setProperty("focusZone", 2); scanner.setProperty("actionIndex", 3)
            with patch("ngs.bulk_scan.client_metadata", side_effect=AssertionError("No API needed for known/ambiguous folders")):
                bridge.action.emit("activate"); settled()
            self.assertEqual(bridge.scannerState["stage"], "batch")
            self.assertEqual(len(bridge.localGames), 1)
            self.assertEqual(window.property("selected")["id"], bridge.localGames[0]["id"])
            rows = bridge.scannerState["batch_rows"]
            self.assertEqual({row["state"] for row in rows}, {"added", "manual"})
            scanner.setProperty("currentIndex", 0)
            scanner.setProperty("focusZone", 1)
            bridge.action.emit("activate")
            self.assertEqual(bridge.scannerState["stage"], "exes")
            bridge.action.emit("activate")
            self.assertEqual(bridge.scannerState["stage"], "confirm")
            bridge.action.emit("activate"); settled()
            self.assertEqual(bridge.scannerState["stage"], "batch")
            self.assertTrue(scanner.property("visible"))
            self.assertEqual(len(bridge.localGames), 2)
            self.assertEqual(window.property("selected")["name"], "Needs Review")
            self.assertTrue(all(row["state"] == "added" for row in bridge.scannerState["batch_rows"]))
            scanner.setProperty("showAllResults", True)
            QTest.qWait(250)
            self.assertTrue(window.grabWindow().save(str(self.repo / "reports/local-library-bulk.png")))
        finally:
            settled(); scanner.setProperty("visible", False)
            for identity in set(bridge.externalIds) - known_before:
                bridge.library.forget_external(identity)
            bridge._installed = bridge.library.reconcile()
            bridge.settings = settings
            bridge._scanner = previous_scanner
            bridge.scannerChanged.emit()
            bridge.libraryChanged.emit(); bridge.settingsChanged.emit()
            bridge.catalog_loaded(previous)
            window.setProperty("section", 1); window.setProperty("focusZone", 2)
            QTest.qWait(350)

    @staticmethod
    def inline_transfer(storage, identity, origin, cancel, progress):
        from ngs.catalog import CatalogCache
        from ngs.downloads import DownloadQueue
        return DownloadQueue(storage, _worker=True).run(identity, CatalogCache(storage, origin), cancel, progress)

    def wait_for_preview(self, bridge):
        for unused in range(300):
            QTest.qWait(10)
            if not bridge.installPreview.get("busy") and bridge.preview_worker is None:
                return
        self.fail("Install preview did not finish")

    def enqueue_download(self, bridge, identity):
        directory = bridge.installDirectories[0]
        bridge.previewInstall(identity, directory)
        self.wait_for_preview(bridge)
        self.assertTrue(bridge.installPreview.get("can_install"), bridge.installPreview)
        self.assertTrue(bridge.queueDownload(identity, directory))

    def test_update_check_error_keeps_known_badge_and_pinned_envelope(self):
        import copy, json
        from unittest.mock import patch
        bridge = self.engine.bridge
        old = copy.deepcopy(bridge._update), bridge._update_envelope
        envelope = dict(payload=json.dumps(dict(version='2099.01.01.000000', notes='Available')), signature='test')
        try:
            bridge._update.update(available=True, version='2099.01.01.000000')
            bridge._update_envelope = envelope
            with patch('ngs.app.check_update', side_effect=ConnectionError('offline')):
                bridge.checkUpdates()
                self.assertTrue(bridge.updatesState['available'])
                for unused in range(300):
                    QTest.qWait(10)
                    if bridge.update_worker is None: break
                self.assertIsNone(bridge.update_worker)
            self.assertTrue(bridge.updatesState['available'])
            self.assertEqual(bridge._update_envelope, envelope)
            self.assertEqual(bridge.update_timer.interval(), 300000)
        finally:
            bridge._update, bridge._update_envelope = old
            bridge.updatesChanged.emit()

    def test_bb_updates_gamepad_check_install_and_cancel(self):
        import copy, json, threading
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge._update)
        previous_settings = copy.deepcopy(bridge.settings)
        pane = window.findChild(QObject, "settingsPane")
        updates = window.findChild(QObject, "updatesPane")
        release = dict(version="2026.10.16.180000", notes="Описание выпуска")
        envelope = dict(payload=json.dumps(release), signature="test")
        started, finish = threading.Event(), threading.Event()
        def wait_worker():
            for unused in range(300):
                QTest.qWait(10)
                if bridge.update_worker is None: return
            self.fail("Update worker did not finish")
        try:
            bridge._update["current"] = "2026.10.15.120000"
            with patch("ngs.app.check_update", return_value=envelope), \
                 patch("ngs.app.prepare_update", return_value=release) as prepare, \
                 patch("ngs.app.start_installer") as launch, patch.object(bridge, "quit") as quit:
                bridge.action.emit("settings")
                QTest.qWait(260)
                pane.setProperty('selectedField', 8)
                self.assertEqual(pane.property("selectedField"), 8)
                bridge.action.emit("activate")
                QTest.qWait(250)
                wait_worker()
                self.assertTrue(updates.property("visible"))
                self.assertTrue(bridge.updatesState["available"])
                self.assertIn(release["version"], window.findChild(QObject, "updateAvailableVersion").property("text"))
                self.assertTrue(window.findChild(QObject, "installUpdateButton").property("enabled"))
                self.assertIsNone(window.findChild(QObject, "updatesDirectory"))
                self.assertIn(bridge.settings["tactic_url"], window.findChild(QObject, "updateHttpServer").property("text"))
                if updates.property("selectedField") != 1: bridge.action.emit("left")
                self.assertEqual(updates.property("selectedField"), 1)
                bridge.action.emit("activate")
                wait_worker()
                prepare.assert_called_once()
                launch.assert_called_once_with(self.storage)
                quit.assert_called_once()
                def pending(*args):
                    started.set(); finish.wait(2)
                    return envelope
                with patch("ngs.app.check_update", side_effect=pending):
                    bridge.action.emit("refresh")
                    QTest.qWait(50)
                    self.assertTrue(started.is_set())
                    self.assertIsNone(window.findChild(QObject, "checkUpdatesButton"))
                    bridge.action.emit("back")
                    finish.set()
                    wait_worker()
                    self.assertIn("отменено", bridge.updatesState["status"])
                    self.assertEqual(launch.call_count, 1)
                bridge.action.emit("back")
                QTest.qWait(200)
                self.assertFalse(updates.property("visible"))
        finally:
            finish.set()
            bridge.cancelUpdate()
            wait_worker()
            updates.setProperty("visible", False)
            pane.setProperty("visible", False)
            bridge._update, bridge._update_envelope = previous, None
            bridge.settings = previous_settings
            bridge.save()
            bridge.updatesChanged.emit()
            window.setProperty("section", 1)
            window.setProperty("focusZone", 2)
            window.setProperty("selectedIndex", 0)
            QTest.qWait(300)

    def test_ba_install_space_requires_confirmation_and_refreshes_each_destination(self):
        import copy, json, threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from ngs.catalog import CatalogCache
        bridge, window = self.engine.bridge, self.window
        previous, original, cache = copy.deepcopy(bridge.settings), bridge._games.copy(), bridge.cache
        picker = window.findChild(QObject, "installDestination")
        confirm = window.findChild(QObject, "confirmDestination")
        card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = dict(path="/assets/ngs_launcher/test.jpg", size=1, sha256="0" * 64)
        card.update(id="space-preview", revision=1, cover=image, background=image,
                    manifest=dict(path="/assets/ngs_launcher/files.json", size=1, sha256="1" * 64))
        release = threading.Event()
        def disk(path):
            free = 80 if Path(path) == self.storage.games else 20
            return SimpleNamespace(free=free * 1024**3)
        try:
            bridge.settings["games_directories"] = ["games"]
            bridge.settingsChanged.emit()
            bridge.cache = CatalogCache(self.storage, previous["tactic_url"])
            bridge.catalog_loaded([dict(card, image="", backgroundImage="")])
            with patch.object(bridge, "pumpDownloads"), \
                 patch("ngs.app.fetch_manifest", return_value={"total_size": 30 * 1024**3}), \
                 patch("ngs.app.shutil.disk_usage", side_effect=disk):
                bridge.performGameAction(card["id"])
                self.assertTrue(picker.property("visible"))
                self.assertEqual(bridge.downloadJobs, [])
                self.wait_for_preview(bridge)
                self.assertEqual(bridge.installPreview["size"], 30 * 1024**3)
                self.assertEqual(bridge.installPreview["remaining"], 50 * 1024**3)
                self.assertIn("30,00", window.findChild(QObject, "installGameSize").property("text"))
                self.assertIn("80,00", window.findChild(QObject, "installFreeSpace").property("text"))
                self.assertIn("50,00", window.findChild(QObject, "installRemainingSpace").property("text"))
                self.assertTrue(confirm.property("enabled"))
                bridge.settings["games_directories"] = ["games", "Second Games"]
                bridge.settingsChanged.emit()
                bridge.action.emit("right")
                self.assertFalse(confirm.property("enabled"))
                self.wait_for_preview(bridge)
                self.assertEqual(bridge.installPreview["remaining"], -10 * 1024**3)
                self.assertFalse(confirm.property("enabled"))
                self.assertIn("Недостаточно", window.findChild(QObject, "installSpaceStatus").property("text"))
                self.assertFalse(bridge.queueDownload(card["id"], "Second Games"))
                self.assertEqual(bridge.downloadJobs, [])
                self.assertFalse((self.storage.root / "Second Games").exists())
                with patch("ngs.app.shutil.disk_usage", side_effect=OSError("disconnected")):
                    bridge.action.emit("refresh")
                    self.wait_for_preview(bridge)
                    self.assertFalse(confirm.property("enabled"))
                    self.assertEqual(window.findChild(QObject, "installFreeSpace").property("text"), "—")
                bridge.action.emit("left")
                self.wait_for_preview(bridge)
                self.assertTrue(confirm.property("enabled"))
                previous_card = bridge._games[0]
                bridge._games[0] = dict(previous_card, executable="changed.exe")
                self.assertFalse(bridge.queueDownload(card["id"], "games"))
                self.assertFalse(confirm.property("enabled"))
                self.assertIn("Сначала проверь", window.findChild(QObject, "installSpaceStatus").property("text"))
                bridge._games[0] = previous_card
                # The changed game must not inherit the previous game's delayed size result.
                def delayed(*args):
                    release.wait(2)
                    return {"total_size": 1}
                with patch("ngs.app.fetch_manifest", side_effect=delayed):
                    bridge.previewInstall(card["id"], "Second Games")
                    QTest.qWait(20)
                    bridge.previewInstall(card["id"], "games")
                    self.assertFalse(confirm.property("enabled"))
                    release.set()
                    self.wait_for_preview(bridge)
                    self.assertEqual(bridge.installPreview["directory"], "games")
                    self.assertEqual(bridge.installPreview["remaining"], 80 * 1024**3 - 1)
                bridge.action.emit("refresh")
                self.wait_for_preview(bridge)
                bridge.settings["ui_scale"] = 1.2
                bridge.settingsChanged.emit()
                window.resize(1280, 720)
                QTest.qWait(450)
                end = confirm.mapToScene(QPointF(confirm.width(), confirm.height()))
                self.assertLessEqual(end.y(), window.height())
                self.assertTrue(window.grabWindow().save(str(self.repo / "reports/install-space.png")))
                bridge.action.emit("activate")
                bridge.action.emit("activate")
                self.assertEqual(len(bridge.downloadJobs), 1)
                self.assertEqual(bridge.download_queue.get(card["id"])["games"], "games")
        finally:
            release.set()
            bridge.cancelInstallPreview()
            self.wait_for_preview(bridge)
            bridge.download_queue.discard(card["id"])
            bridge.downloadsChanged.emit()
            picker.setProperty("visible", False)
            bridge.settings, bridge.cache = previous, cache
            bridge.settingsChanged.emit()
            bridge.catalog_loaded(original)
            window.resize(1280, 800)
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(250)

    def test_h_portrait_selection_stays_inside_edges_and_dims_neighbors(self):
        bridge, window = self.engine.bridge, self.window
        previous = dict(bridge.settings)
        original = bridge._games.copy()
        cards = window.findChild(QObject, "gameCards")
        try:
            bridge.catalog_loaded([dict(original[i % len(original)], id="edge-%d" % i) for i in range(40)])
            window.setProperty("section", 1)
            window.setProperty("focusZone", 2)
            for width, height, scale in ((1280, 800, 1), (1280, 720, 1.2), (1920, 1200, .8)):
                window.resize(width, height)
                bridge.settings["ui_scale"] = scale
                bridge.settingsChanged.emit()
                for index in (0, 6, 12, 39, 38, 1, 0):
                    window.setProperty("selectedIndex", index)
                    QTest.qWait(360)
                    item = cards.property("currentItem")
                    self.assertEqual(item.property("index"), index)
                    self.assertGreaterEqual(item.property("x"), cards.property("contentX") - .5)
                    self.assertLessEqual(item.property("x") + item.property("width"),
                                         cards.property("contentX") + cards.property("width") + .5)
                    poster = item.findChild(QObject, "cardPoster-%d" % index)
                    self.assertAlmostEqual(poster.property("width") / poster.property("height"), 2 / 3, delta=.01)
                    self.assertAlmostEqual(item.findChild(QObject, "cardGlow-%d" % index).property("opacity"), 1)
                    self.assertAlmostEqual(item.findChild(QObject, "cardDim-%d" % index).property("opacity"), 0)
                bridge.action.emit("right")
                bridge.action.emit("right")
                bridge.action.emit("right")
                bridge.action.emit("left")
                QTest.qWait(360)
                item = cards.property("currentItem")
                self.assertEqual(item.property("index"), 2)
                self.assertGreaterEqual(item.property("x"), cards.property("contentX") - .5)
                neighbor = next(child for child in cards.property("contentItem").childItems()
                                if child.property("index") == 1)
                self.assertAlmostEqual(neighbor.findChild(QObject, "cardGlow-1").property("opacity"), 0)
                self.assertAlmostEqual(neighbor.findChild(QObject, "cardDim-1").property("opacity"), .2)
        finally:
            bridge.settings = previous
            bridge.settingsChanged.emit()
            bridge.catalog_loaded(original)
            window.resize(1280, 800)
            window.setProperty("selectedIndex", 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(500)

    def test_cards_release_focus_and_b_menu_contains_only_three_actions(self):
        bridge, window = self.engine.bridge, self.window
        previous_view = bridge.gamesView
        menu = window.findChild(QObject, 'exitPopup')
        settings = window.findChild(QObject, 'settingsPane')
        try:
            window.setProperty('section', 1); window.setProperty('selectedIndex', 0)
            identity = window.property('selected')['id']
            for mode in ('ribbon', 'grid'):
                bridge.setGamesView(mode); window.setProperty('focusZone', 2); QTest.qWait(500)
                view = window.findChild(QObject, 'gameGrid' if mode=='grid' else 'gameCards')
                card = view.property('currentItem'); poster = card.findChild(QObject, 'cardPoster-0')
                glow = card.findChild(QObject, 'cardGlow-0')
                self.assertTrue(card.property('chosen')); self.assertAlmostEqual(poster.property('scale'), 1)
                for zone in (0, -1, 1):
                    window.setProperty('focusZone', zone)
                    self.assertFalse(card.property('chosen'))
                    QTest.qWait(450)
                    self.assertAlmostEqual(poster.property('scale'), .96)
                    self.assertAlmostEqual(glow.property('opacity'), 0)
                    self.assertEqual(window.property('selected')['id'], identity)
                    window.setProperty('focusZone', 2); QTest.qWait(450)
                    self.assertTrue(card.property('chosen')); self.assertAlmostEqual(poster.property('scale'), 1)
                QMetaObject.invokeMethod(window, 'openMenu'); QTest.qWait(350)
                self.assertFalse(card.property('chosen'))
                self.assertEqual(window.property('menuItems').toVariant(), ['Settings','Minimize','Quit'])
                self.assertIsNone(window.findChild(QObject, 'menuContinue'))
                bridge.action.emit('back'); QTest.qWait(300)
                self.assertTrue(card.property('chosen'))
            window.setProperty('focusZone', 0)
            bridge.action.emit('back'); QTest.qWait(250)
            self.assertTrue(menu.property('visible'))
            for expected in (1, 2, 0):
                bridge.action.emit('down'); self.assertEqual(window.property('menuAction'), expected)
            bridge.action.emit('up'); self.assertEqual(window.property('menuAction'), 2)
            bridge.action.emit('back'); QTest.qWait(250)
            self.assertFalse(menu.property('visible'))
            QMetaObject.invokeMethod(window, 'openMenu'); bridge.action.emit('activate'); QTest.qWait(250)
            self.assertTrue(settings.property('visible'))
            self.assertEqual(window.property('selected')['id'], identity)
        finally:
            menu.setProperty('visible', False); settings.setProperty('visible', False)
            bridge.setGamesView(previous_view); window.setProperty('focusZone', 2); QTest.qWait(300)

    def test_i_menu_buttons_match_and_shading_covers_full_window(self):
        bridge, window = self.engine.bridge, self.window
        menu = window.findChild(QObject, "exitPopup")
        self.assertEqual(window.findChild(QObject, "projectSubtitle").property("text"), "NAS Game Switcher")
        try:
            QMetaObject.invokeMethod(window, "openMenu")
            QTest.qWait(500)
            self.assertTrue(menu.property("visible"))
            self.assertAlmostEqual(window.findChild(QObject, "foregroundContent").property("depth"), 1)
            buttons = [window.findChild(QObject, name) for name in
                       ("menuSettings", "menuMinimize", "menuQuit")]
            self.assertTrue(all(button.property("width") == buttons[0].property("width") for button in buttons))
            for name in ("sceneHorizontalShade", "sceneVerticalShade"):
                shade = window.findChild(QObject, name)
                start = shade.mapToScene(QPointF(0, 0))
                end = shade.mapToScene(QPointF(shade.property("width"), shade.property("height")))
                self.assertAlmostEqual(start.x(), 0, delta=.5)
                self.assertAlmostEqual(start.y(), 0, delta=.5)
                self.assertAlmostEqual(end.x(), window.width(), delta=.5)
                self.assertAlmostEqual(end.y(), window.height(), delta=.5)
            self.assertTrue(window.grabWindow().save(str(self.repo / "reports/menu-full-background.png")))
        finally:
            menu.setProperty("visible", False)
            QTest.qWait(400)

    def test_d_dpad_reaches_header_settings_scale_and_back(self):
        bridge, window = self.engine.bridge, self.window
        previous = dict(bridge.settings)
        pane = window.findChild(QObject, "settingsPane")
        try:
            window.setProperty("section", 1)
            window.setProperty("focusZone", 2)
            for key in (Qt.Key.Key_Up, Qt.Key.Key_Up):
                QTest.keyClick(window, key)
            for unused in range(12):
                if window.findChild(QObject, 'menuButton').property('highlighted'):
                    break
                self.engine.gamepad.action.emit("next_control")
            self.assertEqual(window.property("navigationIndex"), 5)
            self.assertTrue(window.findChild(QObject, "menuButton").property("highlighted"))
            self.engine.gamepad.action.emit("activate")
            QTest.qWait(350)
            self.assertTrue(pane.property("visible"))
            self.assertIsNotNone(pane.findChild(QObject, "settingsBackTop"))
            self.assertEqual(bridge.displayRefreshRate, window.screen().refreshRate())
            self.assertNotIn("NaN", pane.findChild(QObject, "settingsRefreshRate").property("text"))
            bridge.set_refresh_rate(120)
            self.assertIn("120", pane.findChild(QObject, "settingsRefreshRate").property("text"))
            bridge.set_refresh_rate(window.screen().refreshRate())
            self.engine.gamepad.action.emit("down")
            self.engine.gamepad.action.emit("activate")
            self.engine.gamepad.action.emit("down")
            self.engine.gamepad.action.emit("down")
            self.assertEqual(pane.property("selectedField"), 21)
            self.engine.gamepad.action.emit("down")
            self.engine.gamepad.action.emit("down")
            self.assertEqual(pane.property("selectedField"), 4)
            self.engine.gamepad.action.emit("right")
            self.engine.gamepad.action.emit("down")
            self.engine.gamepad.action.emit("down")
            self.engine.gamepad.action.emit("left")
            pane.setProperty('selectedField', 6)
            self.engine.gamepad.action.emit("activate")
            self.assertEqual(self.storage.read_settings()["ui_scale"], 1.05)
            self.assertAlmostEqual(window.property("uiScale"), min(window.width() / 1440, window.height() / 900) * 1.05)
            self.engine.gamepad.action.emit("menu")
            QTest.qWait(200)
            self.assertFalse(pane.property("visible"))
            QMetaObject.invokeMethod(window, "openMenu")
            bridge.action.emit("activate")
            QTest.qWait(350)
            self.assertTrue(pane.property("visible"))
            bridge.action.emit("back")
            QTest.qWait(200)
            saved = self.storage.read_settings()
            for invalid in (0.79, 1.21, float("nan"), True, "large"):
                self.assertFalse(bridge.applySettings(dict(bridge.settingsState, ui_scale=invalid)))
                self.assertEqual(self.storage.read_settings(), saved)
        finally:
            pane.setProperty("visible", False)
            bridge.settings = previous
            bridge.settingsChanged.emit()
            bridge.save()
            window.setProperty("section", 1)
            window.setProperty("focusZone", 2)
            QTest.qWait(250)

    def test_e_freeze_callback_restores_launcher_and_requires_explicit_thaw(self):
        from unittest.mock import Mock
        from ngs.power import PowerSetting, DISPLAY_STATUS
        import ctypes
        import threading
        bridge, window = self.engine.bridge, self.window
        frozen = window.findChild(QObject, "frozenScreen")
        process = Mock(frozen=False)
        process.freeze.side_effect = lambda: setattr(process, "frozen", True)
        process.thaw.side_effect = lambda: setattr(process, "frozen", False)
        try:
            bridge.process = process
            bridge.gameStarted.emit()
            self.assertFalse(window.isVisible())
            self.assertTrue(window.property("gameInProgress"))
            self.assertTrue(window.findChild(QObject, "playingBackground").property("visible"))
            self.assertFalse(window.findChild(QObject, "launcherScene").property("visible"))
            self.assertFalse(self.engine.gamepad.active)
            self.engine.power._display_off = False
            off = PowerSetting((ctypes.c_ubyte * 16).from_buffer_copy(DISPLAY_STATUS), 4, 0)
            callback = threading.Thread(target=self.engine.power.callback, args=(None, 0x8013, ctypes.addressof(off)))
            callback.start(); callback.join(2)
            self.assertFalse(callback.is_alive())
            QTest.qWait(250)

            self.assertTrue(bridge.gameFrozen)
            self.assertFalse(window.isVisible())
            self.engine.power.notify(None, 4, None)
            self.engine.power.notify(None, 18, None)
            QTest.qWait(100)
            self.assertFalse(window.isVisible())
            process.thaw.assert_not_called()
            on = PowerSetting((ctypes.c_ubyte * 16).from_buffer_copy(DISPLAY_STATUS), 4, 1)
            self.engine.power.callback(None, 0x8013, ctypes.addressof(on))
            QTest.qWait(100)
            self.assertTrue(window.isVisible())
            self.assertTrue(frozen.property("visible"))
            self.assertTrue(self.engine.gamepad.active)
            self.assertTrue(window.grabWindow().save(str(self.repo / "reports/frozen-new.png")))
            self.engine.power.callback(None, 0x8013, ctypes.addressof(off))
            QTest.qWait(100)
            self.assertFalse(window.isVisible())
            self.assertFalse(self.engine.gamepad.active)
            self.engine.power.callback(None, 0x8013, ctypes.addressof(on))
            QTest.qWait(100)
            self.assertTrue(window.isVisible())
            self.engine.power.notify(None, 18, None)
            QTest.qWait(100)
            process.thaw.assert_not_called()
            before = window.property("section")
            bridge.action.emit("next_section")
            bridge.action.emit("back")
            self.assertEqual(window.property("section"), before)
            bridge.action.emit("activate")
            QTest.qWait(100)
            process.thaw.assert_called_once()
            process.foreground.assert_called_once()
            self.assertFalse(bridge.gameFrozen)
            self.assertFalse(window.isVisible())
            self.assertTrue(window.property("gameInProgress"))
            self.assertFalse(self.engine.gamepad.active)
            process.freeze.side_effect = OSError("fixture freeze denied")
            with self.assertLogs(level="ERROR"):
                self.engine.power.callback(None, 0x8013, ctypes.addressof(off))
            QTest.qWait(100)
            self.assertIn("fixture freeze denied", bridge.powerError)
            self.assertFalse(window.isVisible())
            self.engine.power.callback(None, 0x8013, ctypes.addressof(on))
            QTest.qWait(100)
            self.assertTrue(window.isVisible())
            self.assertTrue(frozen.property("visible"))
            bridge.action.emit("activate")
            self.assertEqual(bridge.powerError, "")
            process.thaw.assert_called_once()
        finally:
            self.engine.power.sleeping = False
            bridge.process = None
            bridge.freezeChanged.emit()
            bridge.gameReturned.emit()
            QTest.qWait(250)

    def test_status_settings_release_topmost_and_restore_controller_on_return(self):
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        previous = window.property('section'), window.property('focusZone'), window.property('statusIndex')
        try:
            self.engine.power.sleeping = False
            window.showNormal()
            QTest.qWait(30)
            window.setProperty('section', 1)
            window.setProperty('focusZone', -1)
            window.setProperty('statusIndex', 1)
            with patch.object(QGuiApplication, 'platformName', return_value='windows'), \
                    patch('ngs.process.set_topmost') as topmost, \
                    patch('ngs.app.QDesktopServices.openUrl', return_value=True) as opened, \
                    patch.object(window, 'isActive', return_value=True) as active, \
                    patch.object(window, 'requestActivate') as activate:
                self.assertTrue(bridge.openSystemSettings('wifi'))
                self.assertEqual(opened.call_args.args[0].toString(), 'ms-settings:network-wifi')
                topmost.assert_called_with(int(window.winId()), False)
                active.return_value = False
                window.activeChanged.emit()
                self.assertFalse(self.engine.gamepad.active)
                window.visibilityChanged.emit(window.visibility())
                QTest.qWait(30)
                topmost.assert_called_with(int(window.winId()), False)
                activate.assert_not_called()
                active.return_value = True
                window.activeChanged.emit()
                topmost.assert_called_with(int(window.winId()), True)
                self.assertTrue(self.engine.gamepad.active)
                self.assertEqual(window.property('focusZone'), -1)
                bridge.action.emit('right')
                self.assertEqual(window.property('statusIndex'), 2)
                topmost.reset_mock()
                self.assertFalse(bridge.openSystemSettings('file:///bad'))
                topmost.assert_not_called()
                opened.return_value = False
                self.assertFalse(bridge.openSystemSettings('bluetooth'))
                self.assertEqual([call.args[1] for call in topmost.call_args_list], [False, True])
                activate.assert_called_once()
                activate.reset_mock(); topmost.reset_mock()
                self.engine.power.sleeping = True
                self.assertFalse(bridge.openSystemSettings('volume'))
                activate.assert_not_called()
                topmost.assert_not_called()
        finally:
            self.engine.power.sleeping = False
            window.activeChanged.emit()
            for name, value in zip(('section', 'focusZone', 'statusIndex'), previous):
                window.setProperty(name, value)

    def test_launcher_topmost_tracks_visibility_and_game_lifetime(self):
        from unittest.mock import Mock, patch
        bridge, window = self.engine.bridge, self.window
        try:
            window.hide()
            with patch.object(QGuiApplication, "platformName", return_value="windows"), \
                    patch("ngs.process.set_topmost") as topmost:
                window.showNormal()
                QTest.qWait(30)
                topmost.assert_called_with(int(window.winId()), True)
                bridge.minimizeRequested.emit()
                QTest.qWait(400)
                topmost.assert_called_with(int(window.winId()), False)
                window.showNormal()
                QTest.qWait(30)
                topmost.assert_called_with(int(window.winId()), True)
                bridge.process = Mock(frozen=False)
                bridge.gameStarted.emit()
                topmost.assert_called_with(int(window.winId()), False)
                bridge.process = None
                bridge.gameReturned.emit()
                topmost.assert_called_with(int(window.winId()), True)
                topmost.reset_mock()
                self.engine.power.sleeping = True
                window.hide()
                QTest.qWait(30)
                topmost.assert_not_called()
        finally:
            self.engine.power.sleeping = False
            bridge.process = None
            bridge.gameReturned.emit()
            QTest.qWait(50)

    def test_e_wake_sets_topmost_after_show_and_drops_it_before_returning_game_focus(self):
        from unittest.mock import Mock, patch
        bridge, window = self.engine.bridge, self.window
        process = Mock(frozen=False)
        process.thaw.side_effect = lambda: setattr(process, "frozen", False)
        events = []
        process.foreground.side_effect = lambda: events.append("game")
        def topmost(hwnd, enabled):
            if enabled:
                self.assertTrue(window.isVisible(), "Fullscreen restoration must precede native topmost")
            events.append("topmost" if enabled else "normal")
        try:
            bridge.process = process
            bridge.gameStarted.emit()
            window.activeChanged.emit()
            self.assertFalse(self.engine.gamepad.active)
            process.frozen = True
            self.engine.power.sleeping = True
            bridge.freezeChanged.emit()
            self.assertFalse(window.isVisible())
            with patch.object(QGuiApplication, "platformName", return_value="windows"), \
                    patch("ngs.process.set_topmost", side_effect=topmost):
                self.engine.power.sleeping = False
                bridge.powerResumed.emit()
                QTest.qWait(330)
                self.assertGreaterEqual(events.count("topmost"), 2)
                self.assertTrue(window.property("gameInProgress"))
                process.park_windows.assert_called()
                bridge.thawGame()
                self.assertLess(events.index("normal"), events.index("game"))
                self.assertFalse(self.engine.freeze_front_timer.isActive())
                self.assertFalse(self.engine.gamepad.active)
                self.assertFalse(window.isVisible())
                QTest.qWait(200)
                self.assertFalse(window.findChild(QObject, "frozenScreen").property("visible"))
                self.assertTrue(window.property("gameInProgress"))
        finally:
            self.engine.power.sleeping = False
            bridge.process = None
            bridge.freezeChanged.emit()
            bridge.gameReturned.emit()
            QTest.qWait(200)
            if window.isActive():
                self.assertTrue(self.engine.gamepad.active)


    def test_f_scaling_keeps_selected_card_and_settings_controls_on_screen(self):
        bridge, window = self.engine.bridge, self.window
        previous = dict(bridge.settings)
        pane = window.findChild(QObject, "settingsPane")
        cards = window.findChild(QObject, "gameCards")
        backdrop = window.findChild(QObject, "gameBackdrop")
        try:
            window.showNormal()
            window.setProperty("section", 1)
            window.setProperty("focusZone", 2)
            for width, height in ((1280, 720), (1280, 800), (1920, 1200)):
                window.resize(width, height)
                for scale in (.8, 1, 1.2):
                    bridge.settings["ui_scale"] = scale
                    bridge.settingsChanged.emit()
                    QTest.qWait(500)
                    current = cards.property("currentItem")
                    self.assertLess(current.property("width"), current.property("height"))
                    if width == 1280 and height == 800 and scale == 1.2:
                        self.assertTrue(window.grabWindow().save(str(self.repo / "reports/ui-scale-120.png")))
                    point = cards.mapToScene(QPointF(0, cards.property("height")))
                    self.assertLess(point.y(), window.height() - 20)
                    bridge.action.emit("settings")
                    QTest.qWait(320)
                    back = pane.findChild(QObject, "settingsBack")
                    point = back.mapToScene(QPointF(0, back.property("height")))
                    self.assertLessEqual(point.y(), window.height())
                    if width == 1280 and height == 800 and scale == 1.2:
                        self.assertTrue(window.grabWindow().save(str(self.repo / "reports/settings-new.png")))
                    bridge.action.emit("back")
                    QTest.qWait(180)
            window.setProperty("section", 2)
            QTest.qWait(900)
            self.assertTrue(backdrop.property("displayedSource").toString().endswith("downloads.svg"))
            self.assertEqual(backdrop.property("pan"), 0)
            self.assertTrue(window.grabWindow().save(str(self.repo / "reports/downloads-new.png")))
        finally:
            pane.setProperty("visible", False)
            bridge.settings = previous
            bridge.settingsChanged.emit()
            bridge.save()
            window.resize(1280, 800)
            window.setProperty("section", 1)
            window.setProperty("focusZone", 2)
            QTest.qWait(500)

    def test_catalog_filter_button_opens_panel_with_gamepad_and_touch(self):
        bridge, window = self.engine.bridge, self.window
        control = window.findChild(QObject, 'installedFilter')
        pane = window.findChild(QObject, 'gameFiltersPane')
        window.setProperty('section', 1)
        window.setProperty('focusZone', 0)
        window.setProperty('navigationIndex', 3)
        bridge.action.emit('right')
        self.assertEqual(window.property('navigationIndex'), 6)
        bridge.action.emit('activate'); QTest.qWait(200)
        self.assertTrue(pane.property('visible'))
        bridge.action.emit('back'); QTest.qWait(200)
        self.assertFalse(pane.property('visible'))
        device = QTest.createTouchDevice()
        point = control.mapToScene(QPointF(control.width()/2,control.height()/2)).toPoint()
        QTest.touchEvent(window,device).press(0,point,window).commit()
        QTest.touchEvent(window,device).release(0,point,window).commit(); QTest.qWait(200)
        self.assertTrue(pane.property('visible'))
        bridge.action.emit('back'); QTest.qWait(200)
        self.assertEqual(control.property('text'), 'Фильтры')
        self.assertEqual(window.property('section'), 1)

    def test_catalogizer_header_opens_correct_mode_from_x_click_and_gamepad(self):
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        button = window.findChild(QObject, 'addLocalGameButton')
        pane = window.findChild(QObject, 'catalogizerPane')
        previous = window.property('section'), window.property('focusZone'), bridge._input_device
        hidden = bridge.hideInstalled
        try:
            window.showNormal()
            for section, remote in ((1, bridge.catalogizer), (3, bridge.installer_catalogizer)):
                with self.subTest(section=section), patch.object(remote, 'openServer') as opened:
                    window.setProperty('section', section)
                    bridge.set_input_device('gamepad')
                    self.assertTrue(button.property('visible'))
                    self.assertEqual(button.property('text'), 'X Каталогизатор')
                    for trigger in ('keyboard', 'gamepad', 'activate', 'click'):
                        with self.subTest(trigger=trigger):
                            window.setProperty('focusZone', 2)
                            if trigger == 'keyboard':
                                QTest.keyClick(window, Qt.Key.Key_X)
                            elif trigger == 'gamepad':
                                self.engine.gamepad.action.emit('toggle_installed')
                            elif trigger == 'activate':
                                window.setProperty('focusZone', 0)
                                window.setProperty('navigationIndex', 3)
                                self.assertTrue(button.property('highlighted'))
                                self.assertIn('Каталогизатор', window.property('selectionHelp'))
                                self.engine.gamepad.action.emit('activate')
                            else:
                                point = button.mapToScene(QPointF(button.width()/2, button.height()/2)).toPoint()
                                QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
                            QTest.qWait(200)
                            self.assertTrue(pane.property('visible'))
                            self.assertIs(pane.property('bridge'), remote)
                            opened.assert_called_once_with(bridge.settingsState['tactic_url'])
                            self.assertEqual(bridge.hideInstalled, hidden)
                            opened.reset_mock()
                            bridge.action.emit('back'); QTest.qWait(200)
                            self.assertFalse(pane.property('visible'))
            window.setProperty('section', 2)
            self.assertFalse(button.property('visible'))
        finally:
            pane.setProperty('visible', False)
            pane.setProperty('bridge', bridge.catalogizer)
            window.setProperty('section', previous[0]); window.setProperty('focusZone', previous[1])
            bridge.set_input_device(previous[2])

    def test_header_logo_actions_fit_and_add_game_opens_from_gamepad(self):
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        previous = dict(bridge.settings)
        scanner = window.findChild(QObject, 'localScanner')
        navigation = window.findChild(QObject, 'headerNavigation')
        logo = window.findChild(QObject, 'projectLogo')
        add = window.findChild(QObject, 'addLocalGameButton')
        minimize = window.findChild(QObject, 'minimizeButton')
        menu = window.findChild(QObject, 'menuButton')
        try:
            self.assertIsNone(window.findChild(QObject, 'settingsButton'))
            self.assertTrue(logo.property('source').toString().endswith('ngs.svg'))
            self.assertGreater(logo.property('implicitWidth'), 0)
            self.assertEqual(menu.property('text'), 'Настройки')
            window.showNormal()
            for section in (0,1,2,3):
                window.setProperty('section', section)
                self.assertEqual(add.property('visible'), section != 2)
                window.setProperty('focusZone', 0)
                window.setProperty('navigationIndex', 5)
                self.engine.gamepad.action.emit('previous_control')
                self.assertEqual(window.property('navigationIndex'), 4)
                self.engine.gamepad.action.emit('previous_control')
                self.assertEqual(window.property('navigationIndex'), 2)
            window.setProperty('section',0)
            self.assertTrue(add.property('visible'))
            for width,height in ((1280,720),(1280,800),(1920,1200)):
                window.resize(width,height)
                for scale in (1,1.2):
                    bridge.settings['ui_scale'] = scale
                    bridge.settingsChanged.emit()
                    QTest.qWait(150)
                    for left,right in ((navigation,add),(add,minimize),(minimize,menu)):
                        left_edge = left.mapToScene(QPointF(left.width(), left.height()))
                        right_edge = right.mapToScene(QPointF())
                        if left_edge.y() > right_edge.y() and right_edge.y() + right.height() > left.mapToScene(QPointF()).y():
                            self.assertLess(left_edge.x(), right_edge.x())
                    self.assertLess(menu.mapToScene(QPointF(menu.width(),menu.height())).x(),window.width())
            window.resize(1280,800)
            window.setProperty('section',0)
            window.setProperty('focusZone',0)
            window.setProperty('navigationIndex',3)
            self.assertTrue(add.property('highlighted'))
            with patch.object(bridge,'browseLocalFolder') as browse:
                self.engine.gamepad.action.emit('activate')
                QTest.qWait(350)
                self.assertTrue(scanner.property('visible'))
                browse.assert_called_once()
                bridge.action.emit('back')
                QTest.qWait(200)
            self.assertTrue(window.grabWindow().save(str(self.repo/'reports/header-layout-120.png')))
        finally:
            scanner.setProperty('visible',False)
            bridge.settings = previous
            bridge.settingsChanged.emit()
            window.resize(1280,800)
            window.setProperty('section',1)
            window.setProperty('focusZone',2)
            QTest.qWait(300)

    def test_game_actions_stay_visible_and_stationary_during_selection_in_both_tabs(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge.settings), bridge._installed, bridge._games
        actions = window.findChild(QObject, 'heroActions')
        try:
            bridge._installed = [dict(card=card, kind='external') for card in bridge._games]
            bridge.libraryChanged.emit()
            for section in (0, 1):
                window.setProperty('section', section)
                window.setProperty('selectedIndex', 0)
                window.setProperty('focusZone', 2)
                QTest.qWait(550)
                buttons = [window.findChild(QObject, name) for name in
                           ('gameActionButton', 'detailsButton')]
                buttons = [button for button in buttons if button.property('visible')]
                before = [button.mapToScene(QPointF(0, 0)) for button in buttons]
                self.assertEqual(len(buttons), 2)
                bridge.action.emit('right')
                self.assertEqual(window.property('selectedIndex'), 1)
                for delay in (40, 75, 100, 130):
                    QTest.qWait(delay)
                    for button, position in zip(buttons, before):
                        with self.subTest(section=section, delay=delay, button=button.objectName()):
                            self.assertEqual(button.mapToScene(QPointF(0, 0)), position)
                            item = button
                            while item is not None:
                                self.assertAlmostEqual(item.opacity(), 1)
                                item = item.parentItem()
        finally:
            bridge._installed = previous[1]
            bridge.settings = previous[0]
            bridge.libraryChanged.emit(); bridge.settingsChanged.emit()
            window.setProperty('section', 1)
            window.setProperty('selectedIndex', 0)
            QTest.qWait(550)

    def test_card_physical_size_grows_twenty_percent_with_ui_scale(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge.settings)
        cards = window.findChild(QObject, 'gameCards')
        title = window.findChild(QObject, 'heroTitle')
        try:
            window.showNormal()
            window.setProperty('section', 1)
            window.setProperty('selectedIndex', 0)
            for width, height in ((1280, 720), (1280, 800), (1920, 1080)):
                window.resize(width, height)
                sizes = []
                for scale in (1, 1.2):
                    bridge.settings['ui_scale'] = scale
                    bridge.settingsChanged.emit()
                    QTest.qWait(550)
                    item = cards.property('currentItem')
                    poster = item.findChild(QObject, 'cardPoster-0')
                    sizes.append(poster.mapToScene(QPointF(poster.width(), poster.height()))
                                 - poster.mapToScene(QPointF(0, 0)))
                    self.assertGreaterEqual(sizes[-1].y(), 330 * window.property('uiScale'))
                    navigation = window.findChild(QObject, 'headerNavigation')
                    self.assertGreaterEqual(title.mapToScene(QPointF()).y(), navigation.mapToScene(QPointF(0, navigation.height())).y() - 1)
                    self.assertLess(title.mapToScene(QPointF(0, 0)).y(), height * .17)
                    self.assertAlmostEqual(cards.mapToScene(QPointF(0, cards.height())).y(),
                                           height - 70 * window.property('uiScale'), delta=1)
                    if scale == 1.2 and height == 800:
                        self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/compact-layout-120.png')))
                    if scale == 1 and height == 800:
                        self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/larger-default-cards.png')))
                self.assertAlmostEqual(sizes[1].x() / sizes[0].x(), 1.2, delta=.01)
                self.assertAlmostEqual(sizes[1].y() / sizes[0].y(), 1.2, delta=.01)
        finally:
            bridge.settings = previous
            bridge.settingsChanged.emit()
            window.resize(1280, 800)
            QTest.qWait(200)

    def test_hero_text_never_overlaps_at_larger_ui_scales(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        previous = copy.deepcopy(bridge.settings), bridge._games.copy()
        title = window.findChild(QObject, 'heroTitle')
        subtitle = window.findChild(QObject, 'heroSubtitle')
        description = window.findChild(QObject, 'heroDescription')
        actions = window.findChild(QObject, 'heroActions')
        try:
            bridge.catalog_loaded([dict(previous[1][0], name='Need for Speed III: Hot Pursuit',
                                        subtitle='Гонки · 1998', description='Подробное описание игры. ' * 50)])
            window.showNormal()
            window.setProperty('section', 1)
            window.setProperty('selectedIndex', 0)
            for width, height in ((1280, 720), (1280, 800), (1920, 1080)):
                window.resize(width, height)
                for scale in (1, 1.2):
                    bridge.settings['ui_scale'] = scale
                    bridge.settingsChanged.emit()
                    QTest.qWait(500)
                    with self.subTest(width=width, height=height, scale=scale):
                        for upper, lower in ((title, subtitle), (subtitle, description), (description, actions)):
                            bottom = upper.mapToScene(QPointF(0, upper.property('height'))).y()
                            top = lower.mapToScene(QPointF(0, 0)).y()
                            self.assertLess(bottom, top)
                        self.assertGreaterEqual(description.property('lineCount'), 1)
                        self.assertGreaterEqual(description.property('height'), description.property('contentHeight'))
                        cards = window.findChild(QObject, 'gameCards')
                        for name in ('catalogHeading', 'catalogRefresh'):
                            item = window.findChild(QObject, name)
                            if item.property('visible'):
                                self.assertLessEqual(item.mapToScene(QPointF(0, item.height())).y(),
                                                     cards.mapToScene(QPointF(0, 0)).y())
                        if width == 1280 and height == 800 and scale == 1.2:
                            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/hero-scale-120.png')))
        finally:
            bridge.settings = previous[0]
            bridge.settingsChanged.emit()
            bridge.catalog_loaded(previous[1])
            window.resize(1280, 800)
            window.setProperty('section', 1)
            QTest.qWait(500)

    def test_g_dpad_folder_picker_and_reveal_confirmed_executable(self):
        import sys
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject, "settingsPane")
        picker = pane.findChild(QObject, "directoryPicker")
        game = self.root / "Reveal, & игра"
        game.mkdir(exist_ok=True)
        shutil.copyfile(sys.executable, game / "play.exe")
        receipt = bridge.library.register_external(game, "play.exe", "Reveal fixture")
        identity = receipt["card"]["id"]
        try:
            bridge._installed = bridge.library.reconcile()
            bridge.libraryChanged.emit()
            window.setProperty("section", 0)
            window.setProperty("selectedIndex", next(i for i, card in enumerate(bridge.localGames) if card["id"] == identity))
            window.setProperty("focusZone", 2)
            bridge.action.emit("menu")
            QTest.qWait(300)
            self.assertTrue(window.findChild(QObject, "revealGameButton").property("visible"))
            self.assertEqual(bridge.executableLocation(identity), str(game / "play.exe"))
            with patch("ngs.process.reveal_file") as reveal:
                bridge.action.emit("right")
                bridge.action.emit("right")
                self.assertEqual(window.property("detailsAction"), 3)
                bridge.action.emit("activate")
                reveal.assert_called_once_with(str(game / "play.exe"))
                bridge.revealGame("missing")
                self.assertEqual(reveal.call_count, 1)
            bridge.action.emit("back")
            QTest.qWait(250)
            bridge.action.emit("settings")
            QTest.qWait(350)
            source = pane.findChild(QObject, "settingsDestination")
            source.setProperty("text", str(game))
            pane.setProperty("selectedField", 0)
            bridge.action.emit("down")
            self.assertEqual(pane.property('selectedField'), 1)
            bridge.action.emit("activate")
            for unused in range(100):
                QTest.qWait(20)
                if not bridge.operationBusy:
                    break
            self.assertTrue(picker.property("visible"))
            bridge.action.emit("down")
            self.assertEqual(picker.property("actionIndex"), 0)
            bridge.action.emit("activate")
            QTest.qWait(200)
            self.assertFalse(picker.property("visible"))
            self.assertEqual(source.property("text"), str(game) + "\n" + str(game))
            bridge.action.emit("back")
        finally:
            picker.setProperty("visible", False)
            pane.setProperty("visible", False)
            window.findChild(QObject, "detailsPopup").setProperty("visible", False)
            bridge.library.forget_external(identity)
            bridge._installed = bridge.library.reconcile()
            bridge.libraryChanged.emit()
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(300)

    def test_a_minimize_button_keyboard_and_controller_menu_keep_queue_alive(self):
        bridge, window = self.engine.bridge, self.window
        before = bridge.download_queue.jobs()
        try:
            window.showFullScreen()
            self.assertIsNotNone(window.findChild(QObject, "minimizeButton"))
            QTest.keyClick(window, Qt.Key.Key_F9)
            QTest.qWait(100)
            self.assertEqual(window.visibility(), window.Visibility.Minimized)
            self.assertFalse(window.findChild(QObject, "launcherScene").property("visible"))
            self.assertFalse(self.engine.gamepad.active)
            self.assertFalse(bridge._closing)
            self.assertEqual(bridge.download_queue.jobs(), before)
            window.setWindowStates(window.windowStates() & ~Qt.WindowState.WindowMinimized)
            QTest.qWait(100)
            self.assertEqual(window.visibility(), window.Visibility.FullScreen)
            self.assertTrue(window.findChild(QObject, "launcherScene").property("visible"))
            QMetaObject.invokeMethod(window, "openMenu")
            bridge.action.emit("toggle_installed")
            QTest.qWait(100)
            self.assertEqual(window.visibility(), window.Visibility.Minimized)
            self.assertFalse(bridge._closing)
            window.setWindowStates(window.windowStates() & ~Qt.WindowState.WindowMinimized)
            QTest.qWait(100)
            self.assertEqual(window.visibility(), window.Visibility.FullScreen)
            window.showNormal()
            window.resize(1280, 800)
            bridge.minimize()
            QTest.qWait(100)
            self.assertEqual(window.visibility(), window.Visibility.Minimized)
            window.setWindowStates(window.windowStates() & ~Qt.WindowState.WindowMinimized)
            QTest.qWait(100)
            self.assertEqual(window.visibility(), window.Visibility.Windowed)
            self.assertEqual((window.width(), window.height()), (1280, 800))
            self.assertEqual(bridge.download_queue.jobs(), before)
        finally:
            window.showNormal()
            QTest.qWait(250)

    def test_b_multiple_destinations_picker_cancel_confirm_and_pinned_resume(self):
        import copy, json, hashlib
        from unittest.mock import patch
        from ngs.catalog import CatalogCache, atomic_write
        from ngs.install import manifest_bytes
        bridge, window = self.engine.bridge, self.window
        previous, original, cache = copy.deepcopy(bridge.settings), bridge._games.copy(), bridge.cache
        old_paths = self.storage.games, self.storage.game_staging
        sources = [str(self.root / "NAS one"), str(self.root / "NAS two")]
        directories = [str(self.root / "disk one" / "Games"), str(self.root / "disk two" / "Custom folder")]
        card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = dict(path="/assets/ngs_launcher/test.jpg", size=1, sha256="0" * 64)
        card.update(id="choose-folder", name="Choose folder", source_root=sources[1], revision=1,
                    cover=image, background=image, manifest=dict(path="/assets/ngs_launcher/files.json", size=1, sha256="1" * 64))
        data = manifest_bytes(dict(schema=1, game_id=card["id"], source_folder=card["source_folder"],
            executable=card["executable"], files=[dict(path=card["executable"], size=1, sha256="0" * 64)], total_size=1))
        card["manifest"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        (self.storage.cache / "catalog").mkdir(exist_ok=True)
        atomic_write(self.storage.cache / "catalog" / (card["manifest"]["sha256"] + ".manifest.json"), data)
        picker = window.findChild(QObject, "installDestination")
        try:
            self.assertTrue(bridge.applySettings(dict(games_sources=sources, games_directories=directories,
                                                      tactic_url=previous["tactic_url"], resolve_local_metadata=False)))
            bridge.cache = CatalogCache(self.storage, previous["tactic_url"])
            bridge.catalog_loaded([dict(card, image="", backgroundImage="")])
            with patch.object(bridge, "pumpDownloads"):
                bridge.performGameAction(card["id"])
                QTest.qWait(250)
                self.assertTrue(picker.property("visible"))
                self.assertEqual(bridge.downloadJobs, [])
                bridge.action.emit("back")
                QTest.qWait(200)
                self.assertFalse(picker.property("visible"))
                self.assertEqual(bridge.downloadJobs, [])
                bridge.performGameAction(card["id"])
                bridge.action.emit("right")
                self.wait_for_preview(bridge)
                bridge.action.emit("activate")
                bridge.action.emit("activate")
                QTest.qWait(220)
                self.assertFalse(picker.property("visible"))
                job = bridge.download_queue.get(card["id"])
                self.assertEqual(bridge.download_queue.context(job).games, Path(directories[1]))
                self.assertEqual(job["source"], sources[1])
                self.assertEqual(job["source_candidates"], [sources[1], sources[0]])
                self.assertEqual(window.property("section"), 2)
                self.assertTrue(bridge.applySettings(dict(games_sources=[sources[0]], games_directories=[directories[0]],
                                                          tactic_url=previous["tactic_url"])))
                bridge.pauseDownload(card["id"])
                bridge.resumeDownload(card["id"])
                after = bridge.download_queue.get(card["id"])
                self.assertEqual((after["source"], after["games"], after["staging"]),
                                 (job["source"], job["games"], job["staging"]))
                self.assertFalse(bridge.queueDownload(card["id"], str(self.root / "unapproved")))
                bridge.download_queue.discard(card["id"])
        finally:
            bridge.download_queue.discard(card["id"])
            bridge.downloadsChanged.emit()
            picker.setProperty("visible", False)
            bridge.settings, bridge.cache = previous, cache
            self.storage.games, self.storage.game_staging = old_paths
            self.storage.write_settings(previous)
            bridge.settingsChanged.emit()
            bridge.catalog_loaded(original)
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(250)

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        cls.root = Path(cls.folder.name)
        cls.repo = Path(__file__).resolve().parents[1]
        for name in ("qml", "assets"):
            shutil.copytree(cls.repo / name, cls.root / name)
        cls.previous_env = os.environ.copy()
        cls.previous_temp = tempfile.tempdir
        cls.storage = PortableStorage(cls.root)
        settings = cls.storage.read_settings()
        settings['language'] = getattr(cls, 'ui_language', 'ru')
        settings['tactic_url'] = getattr(cls, 'ui_server', 'http://127.0.0.1:9/tactic')
        cls.storage.write_settings(settings)
        cls.storage.configure_process()
        cls.messages = []
        cls.previous_handler = qInstallMessageHandler(lambda kind, context, text: cls.messages.append(text))
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        try:
            cls.engine, cls.window = create_window(cls.storage, demo=True)
        except RuntimeError:
            raise RuntimeError("\n".join(cls.messages)) from None
        cls.window.show()
        cls.window.resize(1280, 800)
        QTest.qWait(650)

    @classmethod
    def tearDownClass(cls):
        cls.window.hide()
        cls.engine.gamepad.set_active(False)
        cls.engine.deleteLater()
        cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(80)
        cls.app.processEvents()
        qInstallMessageHandler(cls.previous_handler)
        os.environ.clear()
        os.environ.update(cls.previous_env)
        tempfile.tempdir = cls.previous_temp
        cls.folder.cleanup()

    def test_render_and_unified_keyboard_pointer_gamepad_navigation(self):
        window = self.window
        self.assertEqual(window.property("section"), 1)
        self.assertEqual(window.property("selectedIndex"), 0)
        image = window.grabWindow()
        self.assertFalse(image.isNull())
        self.assertGreater(image.width(), 700)
        report = self.repo / "reports"
        report.mkdir(exist_ok=True)
        self.assertTrue(image.save(str(report / "prototype.png")))

        QTest.keyClick(window, Qt.Key.Key_Right)
        self.assertEqual(window.property("selectedIndex"), 1)
        self.engine.bridge.action.emit("right")
        self.assertEqual(window.property("selectedIndex"), 2)
        QTest.keyClick(window, Qt.Key.Key_F10)
        QTest.qWait(250)
        details = window.findChild(QObject, "detailsPopup")
        self.assertTrue(details.property("visible"))
        self.engine.bridge.action.emit("right")
        self.assertEqual(window.property("selectedIndex"), 2)
        QTest.keyClick(window, Qt.Key.Key_Escape)
        QTest.qWait(260)
        self.assertFalse(details.property("visible"))

        from PySide6.QtCore import QMetaObject
        QMetaObject.invokeMethod(window.findChild(QObject, "gameCards"), "positionViewAtBeginning")
        QTest.qWait(100)
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=QPoint(140, 605))
        self.assertEqual(window.property("selectedIndex"), 0)
        window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 2)
        self.engine.bridge.action.emit("activate")
        self.assertEqual(window.property("section"), 2)
        window.setProperty("focusZone", 1)
        self.engine.bridge.action.emit("activate")
        self.assertEqual(window.property("section"), 1)
        QMetaObject.invokeMethod(window, "openMenu")
        QTest.qWait(100)
        menu = window.findChild(QObject, "exitPopup")
        self.assertTrue(menu.property("visible"))
        self.engine.bridge.action.emit("activate")
        QTest.qWait(200)
        self.assertFalse(menu.property("visible"))
        actions = []
        self.engine.bridge.action.connect(actions.append)
        QTest.keyClick(window, Qt.Key.Key_R)
        self.assertEqual(actions[-1], "refresh")
        self.engine.bridge.action.disconnect(actions.append)
        self.engine.bridge.save()
        ui = self.storage.read_settings()["ui"]
        self.assertEqual(ui["section"], 1)
        self.assertEqual(ui["selections"]["1"]["game_index"], 0)
        self.assertEqual(ui["selections"]["1"]["game_id"], "orbital")
        failures = [message for message in self.messages if any(
            term in message for term in ("ReferenceError", "TypeError", "Cannot assign", "Binding loop", "failed to load")
        )]
        self.assertEqual(failures, [])

    def test_fullscreen_and_restore(self):
        self.assertFalse(self.window.isPersistentGraphics())
        self.assertFalse(self.window.isPersistentSceneGraph())
        self.window.showFullScreen()
        QTest.qWait(70)
        self.assertEqual(self.window.visibility(), QWindow.Visibility.FullScreen)
        self.window.showNormal()
        self.window.resize(1280, 800)
        QTest.qWait(80)

    def test_c_portable_report_checks_settings_paths_ssl_and_runtime_modules(self):
        import json
        from unittest.mock import patch
        previous_view = self.engine.bridge.gamesView
        try:
            self.engine.bridge.setGamesView('grid')
            self.window.showFullScreen()
            with patch("ngs.app.check_update", return_value=None):
                passed = portable_check(self.storage, self.window)
                for unused in range(100):
                    QTest.qWait(10)
                    if self.engine.bridge.update_worker is None: break
                self.assertIsNone(self.engine.bridge.update_worker)
            report = json.loads((self.storage.logs / "portable-check.json").read_text(encoding="utf8"))
            self.assertTrue(passed, report)
            self.assertFalse(report["compiled"])
            self.assertTrue(report["fullscreen"])
            self.assertTrue(report["settings_opened"])
            self.assertTrue(report["static_background_for_game"])
            self.assertTrue(report["restored"])
            self.assertTrue(report["minimize_restored"])
            self.assertTrue(report['system_status_checked'])
            self.assertTrue(report['input_hints_checked'])
            self.assertTrue(report['header_help_checked'])
            self.assertTrue(report['grid_search_checked'])
            self.assertEqual(self.engine.bridge.gamesView, 'grid')
            self.assertTrue(report['system_settings_handoff_checked'])
            self.assertTrue(report['tabs_require_confirmation'])
            self.assertEqual(Path(report["root"]), self.storage.root)
            self.assertEqual(Path(report["temp"]), self.storage.temp)
            self.assertTrue(all(report["modules"].values()))
        finally:
            self.engine.bridge.setGamesView(previous_view)
            self.window.showNormal()
            self.window.resize(1280, 800)
            QTest.qWait(300)

    def test_motion_tracks_rapid_input_and_settles(self):
        window = self.window
        self.assertIsNone(window.findChild(QObject, "selectionFrame"))
        cards = window.findChild(QObject, "gameCards")
        backdrop = window.findChild(QObject, "gameBackdrop")
        self.engine.bridge.action.emit("right")
        self.assertEqual(window.property("selectedIndex"), 1)
        QTest.qWait(100)
        self.assertEqual(cards.property("currentIndex"), 1)
        self.engine.bridge.action.emit("left")
        for unused in range(4):
            self.engine.bridge.action.emit("right")
            QTest.qWait(25)
        self.assertEqual(window.property("selectedIndex"), 4)
        QTest.qWait(1150)
        self.assertEqual(window.property("heroGame")["id"], "wild")
        self.assertTrue(backdrop.property("displayedSource").toString().endswith("wild.svg"))
        self.assertFalse(backdrop.property("transitioning"))
        self.assertAlmostEqual(window.property("heroOpacity"), 1)
        self.assertAlmostEqual(window.property("heroReveal"), 1)
        item = cards.property("currentItem")
        self.assertLess(item.property("width"), item.property("height"))
        settled_x = item.property("x")
        QTest.qWait(100)
        self.assertAlmostEqual(item.property("x"), settled_x, delta=0.01)
        window.setProperty("selectedIndex", 0)
        QTest.qWait(1150)

    def test_touch_navigation_and_dialogs_at_handheld_resolutions(self):
        window = self.window
        device = QTest.createTouchDevice()
        try:
            for width, height in ((1280, 800), (1280, 720), (1920, 1200)):
                window.resize(width, height)
                window.setProperty("section", 1)
                window.setProperty("selectedIndex", 0)
                QTest.qWait(800)
                button = window.findChild(QObject, "menuButton")
                center = button.mapToScene(QPointF(button.property("width") / 2, button.property("height") / 2))
                point = QPoint(round(center.x()), round(center.y()))
                self.assertTrue(0 <= point.x() < width and 0 <= point.y() < height)
                QTest.touchEvent(window, device).press(0, point, window).commit()
                QTest.touchEvent(window, device).release(0, point, window).commit()
                QTest.qWait(400)
                self.assertFalse(window.findChild(QObject, 'exitPopup').property('visible'))
                pane = window.findChild(QObject, "settingsPane")
                self.assertTrue(pane.property("visible"), (width, height))
                self.engine.bridge.action.emit("back")
                QTest.qWait(300)
                self.assertFalse(pane.property("visible"))
                self.engine.bridge.action.emit("right")
                self.assertEqual(window.property("selectedIndex"), 1)
        finally:
            window.resize(1280, 800)
            window.setProperty("selectedIndex", 0)
            QTest.qWait(700)

    def test_large_catalog_keeps_last_background_on_screen_and_navigation_working(self):
        bridge, window = self.engine.bridge, self.window
        original = bridge._games.copy()
        try:
            bridge.catalog_loaded([dict(original[i % len(original)], id="large-%d" % i) for i in range(1000)])
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 999)
            QTest.qWait(1200)
            backdrop = window.findChild(QObject, "gameBackdrop")
            self.assertLessEqual(abs(backdrop.property("pan")), 20)
            self.assertEqual(window.property("heroGame")["id"], "large-999")
            self.assertFalse(backdrop.property("transitioning"))
            bridge.action.emit("left")
            self.assertEqual(window.property("selectedIndex"), 998)
            QTest.qWait(900)
            self.assertEqual(window.property("heroGame")["id"], "large-998")
            self.assertLessEqual(abs(backdrop.property("pan")), 20)
            window.showFullScreen()
            QTest.qWait(100)
            bridge.gameStarted.emit()
            QTest.qWait(100)
            bridge.gameReturned.emit()
            QTest.qWait(900)
            cards = window.findChild(QObject, "gameCards")
            item = cards.property("currentItem")
            self.assertEqual(item.property("index"), 998)
            self.assertGreaterEqual(item.property("x"), cards.property("contentX") - 2)
            self.assertLessEqual(item.property("x") + item.property("width"),
                                 cards.property("contentX") + cards.property("width") + 2,
                                 {key: cards.property(key) for key in ("contentX", "contentWidth", "width", "count", "visible")})
        finally:
            window.showNormal()
            window.resize(1280, 800)
            bridge.catalog_loaded(original)
            window.setProperty("selectedIndex", 0)
            QTest.qWait(900)

    def test_motion_recovers_from_empty_and_missing_background(self):
        component = QQmlComponent(self.engine, QUrl.fromLocalFile(str(self.root / "qml/GameBackdrop.qml")))
        backdrop = component.createWithInitialProperties({
            "source": QUrl.fromLocalFile(str(self.root / "assets/orbital.svg")), "width": 100, "height": 100,
        })
        self.assertIsNotNone(backdrop)
        backdrop.setParent(self.engine)
        QTest.qWait(80)
        backdrop.setProperty("source", QUrl())
        self.assertFalse(backdrop.property("transitioning"))
        backdrop.setProperty("source", QUrl.fromLocalFile(str(self.root / "missing-background.svg")))
        QTest.qWait(80)
        self.assertFalse(backdrop.property("transitioning"))
        self.assertTrue(backdrop.property("displayedSource").toString().endswith("orbital.svg"))
        backdrop.setProperty("source", QUrl.fromLocalFile(str(self.root / "assets/monolith.svg")))
        QTest.qWait(650)
        self.assertFalse(backdrop.property("transitioning"))
        self.assertTrue(backdrop.property("displayedSource").toString().endswith("monolith.svg"))
        backdrop.deleteLater()

    def test_y_tab_selections_scroll_refresh_and_saved_state(self):
        import copy
        bridge, window = self.engine.bridge, self.window
        original, installed = bridge._games, bridge._installed
        settings = copy.deepcopy(bridge.settings)
        old_section, old_index = window.property("section"), window.property("selectedIndex")
        cards = window.findChild(QObject, "gameCards")
        try:
            bridge.catalog_loaded([dict(original[0], id=f"catalog-{i}") for i in range(30)])
            bridge._installed = [{"card": dict(original[0], id=f"local-{i}"), "kind": "external"} for i in range(30)]
            bridge.libraryChanged.emit()
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 17)
            QTest.qWait(120)
            catalog_x = cards.property("contentX")
            window.setProperty("section", 0)
            window.setProperty("selectedIndex", 9)
            QTest.qWait(120)
            local_x = cards.property("contentX")
            for section, action in ((3, 'previous_section'), (2, ''), (1, 'previous_section'), (2, ''),
                                    (3, 'next_section'), (0, 'next_section'), (3, 'previous_section'),
                                    (2, ''), (1, 'previous_section'), (0, 'previous_section')):
                if action: bridge.action.emit(action)
                else:
                    window.setProperty('focusZone', 0); window.setProperty('navigationIndex', 2)
                    bridge.action.emit('activate')
                self.assertEqual(window.property("section"), section)
                QTest.qWait(120)
                if section in (0, 1):
                    self.assertEqual(window.property("selectedIndex"), 9 if section == 0 else 17)
                    self.assertAlmostEqual(cards.property("contentX"), local_x if section == 0 else catalog_x, delta=1,
                                           msg=(section, cards.property("currentIndex"), bridge.settings["ui"]))
            # A catalog refresh must leave the active local selection untouched.
            bridge.catalog_loaded(list(reversed(bridge._games)))
            self.assertEqual(window.property("selectedIndex"), 9)
            bridge._installed.reverse()
            bridge.libraryChanged.emit()
            self.assertEqual(window.property("selectedIndex"), 20)
            self.assertEqual(window.property("selected")["id"], "local-9")
            window.setProperty("section", 2)
            bridge.catalog_loaded(list(reversed(bridge._games)))
            bridge._installed.reverse()
            bridge.libraryChanged.emit()
            bridge.save()
            bridge.settings = self.storage.read_settings()
            self.assertEqual(bridge.initialUi["section"], 2)
            self.assertEqual(bridge.sectionUi(0)["game_index"], 9)
            self.assertEqual(bridge.sectionUi(1)["game_index"], 17)
            window.setProperty("section", 0)
            self.assertEqual(window.property("selectedIndex"), 9)
            window.setProperty("section", 1)
            self.assertEqual(window.property("selectedIndex"), 17)
            bridge.catalog_loaded(bridge._games[:3])
            self.assertEqual(window.property("selectedIndex"), 2)
            window.setProperty("section", 0)
            bridge._installed = bridge._installed[:3]
            bridge.libraryChanged.emit()
            self.assertEqual(window.property("selectedIndex"), 2)
            bridge._installed = []
            bridge.libraryChanged.emit()
            self.assertEqual(window.property("selectedIndex"), 0)
            window.setProperty("section", 1)

            bridge.settings["ui"] = {"section": 1, "game_index": 1, "game_id": "catalog-1"}
            bridge.rememberUi(2, 0)
            self.assertEqual(bridge.sectionUi(1)["game_index"], 1)
            self.assertNotIn("2", bridge.settings["ui"]["selections"])
            for invalid in (None, [], {"section": "bad"}, {"selections": {"1": "bad"}}):
                bridge.settings["ui"] = invalid
                self.assertEqual(bridge.initialUi["game_index"], 0)
                bridge.rememberUi(1, 0)
        finally:
            bridge._installed = installed
            bridge.catalog_loaded(original)
            bridge.settings = settings
            bridge.libraryChanged.emit()
            window.setProperty("section", old_section)
            window.setProperty("selectedIndex", old_index)
            bridge.save()
            QTest.qWait(300)

    def test_z_background_update_does_not_block_navigation(self):
        import threading
        from types import SimpleNamespace
        bridge = self.engine.bridge
        original = bridge.games.copy()
        started = threading.Event()
        def slow_refresh(cancel):
            started.set()
            cancel.wait(2)
            raise OSError("simulated network outage")
        bridge.cache = SimpleNamespace(refresh=slow_refresh)
        try:
            self.window.setProperty('section', 1)
            self.window.setProperty('focusZone', 2)
            self.window.setProperty('selectedIndex', 0)
            bridge.refreshCatalog()
            QTest.qWait(50)
            self.assertTrue(started.is_set())
            QTest.keyClick(self.window, Qt.Key.Key_Right)
            self.assertEqual(self.window.property("selectedIndex"), 1)
            self.assertTrue(bridge.worker.isRunning())
        finally:
            bridge.stop()
            QTest.qWait(50)
            bridge.cache = None
        self.assertEqual(bridge.games, original)
        self.assertIn("сохранённый", bridge.catalogStatus)
        self.window.setProperty("selectedIndex", 0)
        QTest.qWait(600)

    def test_s_download_queue_pause_error_retry_and_recent_launch_sort(self):
        import hashlib
        import json
        import time
        from unittest.mock import Mock, patch
        from ngs.app import GameWorker
        from ngs.catalog import CatalogCache, atomic_write
        from ngs.install import build_manifest, manifest_bytes
        bridge, window = self.engine.bridge, self.window
        original, previous_cache = bridge._games.copy(), bridge.cache
        previous_source = bridge.settings["games_source"]
        source = self.root / "queue-nas"
        cache = CatalogCache(self.storage, "http://queue-test/tactic")
        cards = []
        for identity in ("queue-one", "queue-two"):
            folder = source / identity
            folder.mkdir(parents=True)
            (folder / "a.exe").write_bytes(b"game executable fixture")
            (folder / "large.bin").write_bytes(b"game data" * 250_000)
            card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
            image = {"path": "/assets/ngs_launcher/test.jpg", "sha256": "0" * 64, "size": 1}
            card.update(id=identity, name=identity, source_folder=identity, executable="a.exe", revision=1, cover=image, background=image)
            data = manifest_bytes(build_manifest(source, card))
            digest = hashlib.sha256(data).hexdigest()
            card["manifest"] = {"path": "/assets/ngs_launcher/" + identity + ".json", "size": len(data), "sha256": digest}
            atomic_write(self.storage.cache / "catalog" / (digest + ".manifest.json"), data)
            cards.append(dict(card, image="", backgroundImage=""))
        bridge.cache = cache
        bridge.settings["games_source"] = str(source)
        def settled():
            for unused in range(1000):
                QTest.qWait(20)
                if not bridge.operationBusy and not any(j["state"] in ("queued", "cancelling") for j in bridge.downloadJobs):
                    return
            self.fail(bridge.operationStatus)
        def partial_ready(identity):
            for unused in range(500):
                QTest.qWait(10)
                job = bridge.download_queue.get(identity)
                _, stage = bridge.download_queue.stage(job)
                file = stage / "payload/large.bin"
                if file.exists() and file.stat().st_size > 0:
                    return file
            self.fail("No partial payload")
        normal_progress = GameWorker.progress
        def slow_progress(worker, *args):
            normal_progress(worker, *args)
            if args[0] == "copy": time.sleep(.008)
        try:
            bridge.catalog_loaded(cards)
            with patch("ngs.install.BLOCK", 32 * 1024), patch.object(GameWorker, "progress", slow_progress), \
                    patch("ngs.transfer.run_transfer", side_effect=self.inline_transfer):
                self.enqueue_download(bridge, "queue-one")
                self.enqueue_download(bridge, "queue-two")
                bridge.performGameAction("queue-one")
                self.assertEqual(len(bridge.downloadJobs), 2)
                bridge.pauseDownload("queue-two")
                partial = partial_ready("queue-one")
                window.setProperty("section", 2)
                window.setProperty("focusZone", 1)
                bridge.action.emit("activate")
                settled()
                self.assertEqual([j["state"] for j in bridge.downloadJobs], ["paused", "paused"])
                import gc
                gc.collect(); QTest.qWait(50)
                queue_list = window.findChild(QObject, "downloadQueueList")
                def queue_control(name):
                    return next((item for row in queue_list.property("contentItem").childItems()
                                 for item in row.childItems() if item.objectName() == name), None)
                for identity in ("queue-one", "queue-two"):
                    label = queue_control("queueName-" + identity)
                    self.assertIsNotNone(label, self.messages[-10:])
                    self.assertIn(identity, label.property("text"))
                    self.assertEqual(queue_control("queuePrimary-" + identity).property("text"), "Продолжить · " + bridge.inputHints['activate'])
                first_label = queue_control("queueName-queue-one")
                self.assertTrue(partial.is_file())
                self.assertNotIn("queue-one", bridge.installedIds)
                QTest.qWait(350)
                self.assertTrue(window.grabWindow().save(str(self.repo / "reports/download-queue-paused.png")))
                queue_view = window.findChild(QObject, "downloadsPage")
                queue_view.setProperty("currentIndex", 1)
                bridge.action.emit("activate")
                second_partial = partial_ready("queue-two")
                bridge.action.emit("activate")
                settled()
                with patch("ngs.downloads.remove_stage", side_effect=PermissionError("busy")):
                    bridge.action.emit("toggle_installed")
                    settled()
                self.assertTrue(bridge.download_queue.get("queue-two")["discard"])
                self.assertTrue(second_partial.exists())
                bridge.action.emit("activate")
                settled()
                self.assertIsNone(bridge.download_queue.get("queue-two"))
                self.assertFalse(second_partial.exists())
                self.enqueue_download(bridge, "queue-two")
                bridge.pauseDownload("queue-two")
                settled()
                queue_view.setProperty("currentIndex", 0)
                # A failed source keeps its partial; the next queued game still runs.
                bridge.download_queue.update("queue-one", source=str(source / "unavailable"))
                bridge.action.emit("activate")
                bridge.resumeDownload("queue-two")
                settled()
                self.assertEqual(bridge.download_queue.get("queue-one")["state"], "error")
                gc.collect(); QTest.qWait(50)
                self.assertEqual(queue_control("queueName-queue-one"), first_label)
                self.assertIn("queue-one", queue_control("queueName-queue-one").property("text"))
                self.assertEqual(queue_control("queuePrimary-queue-one").property("text"), "Продолжить · " + bridge.inputHints['activate'])
                self.assertTrue(partial.is_file())
                self.assertIn("queue-two", bridge.installedIds)
                self.assertNotIn("queue-one", bridge.installedIds)
                self.assertEqual(window.property("section"), 0)
                self.assertEqual(window.property("selected")["id"], "queue-two")
                window.setProperty("section", 2)
                window.setProperty("focusZone", 1)
                bridge.download_queue.update("queue-one", source=str(source))
                bridge.action.emit("activate")
                settled()
                self.assertEqual(bridge.downloadJobs, [])
                self.assertIn("queue-one", bridge.installedIds)
            # Only successfully created game processes move a game to the front.
            fake = Mock(_foregrounded=True, _foreground_window=None, _edge_configured=None)
            fake.windows.return_value = []
            fake.active.return_value = True
            fake.exit_code.return_value = 0
            with patch("ngs.app.GameProcess", return_value=fake):
                bridge.performGameAction("queue-one")
                settled()
                self.assertEqual(bridge.localGames[0]["id"], "queue-one")
                fake.active.return_value = False
                bridge.check_game()
            with patch("ngs.app.GameProcess", side_effect=OSError("launch failed")):
                bridge.performGameAction("queue-two")
                settled()
                self.assertEqual(bridge.localGames[0]["id"], "queue-one")
                self.assertNotIn("queue-two", bridge.playedIds)
            fake.active.return_value = True
            with patch("ngs.app.GameProcess", return_value=fake):
                bridge.performGameAction("queue-two")
                settled()
                self.assertEqual(bridge.localGames[0]["id"], "queue-two")
                self.assertIn("queue-two", bridge.playedIds)
                fake.active.return_value = False
                bridge.check_game()
            self.assertEqual(bridge.library.reconcile()[0]["card"]["id"], "queue-two")
        finally:
            bridge.settings["games_source"] = previous_source
            for identity in ("queue-one", "queue-two"):
                bridge.cancelDownload(identity)
            settled()
            for receipt in bridge.library.reconcile():
                if receipt["card"]["id"] in ("queue-one", "queue-two"):
                    bridge.library.uninstall(receipt["card"]["id"])
            bridge._installed = bridge.library.reconcile()
            bridge.libraryChanged.emit()
            bridge.cache = previous_cache
            bridge.catalog_loaded(original)
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(600)

    def test_t_settings_restart_preserves_transfer_and_next_start_resumes_automatically(self):
        import hashlib
        import json
        import time
        from unittest.mock import patch
        from ngs.app import LauncherBridge, GameWorker
        from ngs.catalog import CatalogCache, atomic_write
        from ngs.install import build_manifest, manifest_bytes
        storage = PortableStorage(self.root / "restart-launcher")
        source = self.root / "restart-source"
        (source / "game").mkdir(parents=True)
        (source / "game/play.exe").write_bytes(b"fixture" * 400_000)
        settings = storage.read_settings()
        publisher_source = r"\\NAS\Files\games"
        settings.update(tactic_url="http://127.0.0.1:1/tactic", games_source=str(source),
                        legacy_games_source=publisher_source)
        storage.write_settings(settings)
        cache = CatalogCache(storage, settings["tactic_url"])
        card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = {"path": "/assets/ngs_launcher/test.jpg", "sha256": "0" * 64, "size": 1}
        card.update(id="restart-game", source_folder="game", executable="play.exe", revision=1, cover=image, background=image,
                    source_root=publisher_source)
        data = manifest_bytes(build_manifest(source, card))
        digest = hashlib.sha256(data).hexdigest()
        card["manifest"] = {"path": "/assets/ngs_launcher/restart.json", "size": len(data), "sha256": digest}
        atomic_write(cache.folder / (digest + ".manifest.json"), data)
        bridges = []
        def wait_for(condition):
            for unused in range(1000):
                QTest.qWait(10)
                if condition(): return
            self.fail("Launcher recovery timed out")
        original_progress = GameWorker.progress
        def slow(worker, *args):
            original_progress(worker, *args)
            if args[0] == "copy": time.sleep(.008)
        try:
            first = LauncherBridge(storage, demo=True)
            bridges.append(first)
            first.cache = cache
            first.catalog_loaded([card])
            with patch("ngs.install.BLOCK", 32 * 1024), patch.object(GameWorker, "progress", slow), \
                    patch("ngs.transfer.run_transfer", side_effect=self.inline_transfer):
                self.enqueue_download(first, "restart-game")
                self.assertEqual(first.download_queue.get("restart-game")["source"], str(source))
                def has_partial():
                    job = first.download_queue.get("restart-game")
                    _, stage = first.download_queue.stage(job)
                    target = stage / "payload/play.exe"
                    return target.exists() and target.stat().st_size > 0
                wait_for(has_partial)
                with patch.object(self.app, 'exit') as exit_app:
                    first._restart_enabled = True
                    self.assertTrue(first.setQuickOptions({'production_mode': True}))
                    wait_for(lambda: exit_app.called)
                    exit_app.assert_called_once_with(75)
                    first.stop()
                    wait_for(lambda: not first.operationBusy)
            self.assertEqual(first.download_queue.get("restart-game")["state"], "stopping")
            self.assertEqual(first.localGames, [])
            first.download_queue.close()
            atomic_write(storage.config/'player-account.json', json.dumps(dict(device='a'*32, account=dict(
                server=cache.origin, profile='b'*32, username='player', token='c'*64, expires=2000000000,
                api=dict(device='d'*32, secret='e'*64, expires=2000000000)))).encode())
            second = LauncherBridge(PortableStorage(storage.root))
            bridges.append(second)
            wait_for(lambda: not second.operationBusy and "restart-game" in second.installedIds)
            self.assertEqual(second.downloadJobs, [])
            self.assertEqual(hashlib.sha256((storage.games / "Black Mesa/play.exe").read_bytes()).hexdigest(),
                             hashlib.sha256((source / "game/play.exe").read_bytes()).hexdigest())
        finally:
            for bridge in bridges:
                bridge._closing = True
                bridge.stop()
                bridge.download_queue.close()
                bridge.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QTest.qWait(80)

    def test_store_discovery_and_import_work_offline_from_gamepad_and_keep_files(self):
        import copy
        import shutil
        import sys
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject, 'localScanner')
        previous = copy.deepcopy(bridge._scanner)
        previous_online = bridge._catalog_online
        bridge._catalog_online = False
        folder = self.root/'Store fixture'; folder.mkdir(exist_ok=True)
        shutil.copyfile(sys.executable, folder/'play.exe')
        before=(folder/'play.exe').read_bytes()
        rows=[dict(store='GOG', name='Store fixture', path=str(folder), store_id='123', preferred_exe='play.exe',state='found',status='GOG',candidates=[])]
        def settled():
            for unused in range(200):
                QTest.qWait(10)
                if not bridge.operationBusy:return
            self.fail('Store operation did not finish')
        try:
            bridge._scanner.update(stage='browse', path=str(folder), folders=[])
            bridge.scannerChanged.emit()
            pane.setProperty('visible',True); pane.setProperty('focusZone',-1)
            self.engine.gamepad.action.emit('left')
            self.engine.gamepad.action.emit('right')
            self.assertFalse(bridge.operationBusy)
            self.assertEqual(bridge.scannerState['path'],str(folder))
            with patch('ngs.app.installed_store_games',return_value=dict(rows=rows,warnings=[])), \
                 patch('ngs.api.MetadataApi',side_effect=AssertionError('Store discovery used network')):
                self.engine.gamepad.action.emit('activate'); settled()
                self.assertEqual(bridge.scannerState['stage'],'stores')
                self.assertEqual(len(bridge.scannerState['store_games']),1)
                self.assertTrue(pane.findChild(QObject,'scannerStoresButton').property('visible'))
                self.assertEqual(pane.findChild(QObject,'scannerConfirm').property('text'),'Добавить все')
                self.engine.gamepad.action.emit('activate')
                self.assertEqual(bridge.scannerState['stage'],'exes')
                self.engine.gamepad.action.emit('activate')
                self.assertEqual(bridge.scannerState['name'],'Store fixture')
                self.assertEqual(bridge.scannerState['stage'],'confirm')
                bridge.virtual_keyboard.hide()
                self.engine.gamepad.action.emit('back')
                self.assertEqual(bridge.scannerState['stage'],'exes', self.messages[-8:])
                self.engine.gamepad.action.emit('back')
                self.assertEqual(bridge.scannerState['stage'],'stores')
                pane.setProperty('focusZone',2);pane.setProperty('actionIndex',1)
                self.engine.gamepad.action.emit('activate');settled()
                self.assertEqual(bridge.scannerState['store_games'][0]['state'],'artwork')
                identity=bridge.scannerState['store_games'][0]['game_id']
                self.assertEqual(window.property('selected')['id'],identity)
                self.assertEqual((folder/'play.exe').read_bytes(),before)
                bridge.library.forget(identity)
        finally:
            bridge.virtual_keyboard.hide()
            pane.setProperty('visible',False)
            bridge._scanner=previous;bridge.scannerChanged.emit()
            bridge._catalog_online = previous_online
            bridge._installed=bridge.library.reconcile();bridge.libraryChanged.emit()

    def test_store_import_fetches_artwork_for_single_bulk_and_existing_placeholder(self):
        import copy, hashlib, sys
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        pane = window.findChild(QObject, 'localScanner')
        previous = copy.deepcopy(bridge._scanner), bridge._catalog_online
        bridge._catalog_online = True
        folders = [self.root/'Steam artwork fixture', self.root/'GOG artwork fixture']
        rows=[]; identities=[]
        for folder, store, identity in zip(folders, ('Steam','GOG'), ('12','42')):
            folder.mkdir(exist_ok=True);shutil.copyfile(sys.executable,folder/'play.exe')
            rows.append(dict(store=store,name=folder.name,path=str(folder),store_id=identity,preferred_exe='play.exe',state='found',status=store,candidates=[]))
        before={folder:(folder/'play.exe').read_bytes() for folder in folders}
        old = bridge.library.register_external(folders[1],'play.exe','My custom GOG title')
        identities.append(old['card']['id']);bridge.library.set_launch_arguments(identities[0],'-windowed')
        data=(self.repo/'assets/ngs.ico').read_bytes();digest=hashlib.sha256(data).hexdigest()
        image=dict(path='/assets/ngs_launcher/store-fixture.ico',sha256=digest,size=len(data))
        from ngs.catalog import CatalogCache
        CatalogCache(bridge.storage,bridge.settings['tactic_url']).image_path(digest).write_bytes(data)
        cards={ref:dict(id=provider,name=folder.name,subtitle='From store',description='Full store description',genre='Action',accent='#d9ff82',source_folder='',executable='',metadata_url='https://store.steampowered.com/app/12/',revision=1,cover=image,background=image,available_on_nas=False)
               for ref,provider,folder in zip(('12','gog:42'),('steam-12','gog-42'),folders)}
        def metadata(storage,url,operation,value,cancel,progress=None,**kwargs):
            self.assertEqual(operation,'resolve')
            return dict(card=cards[value])
        def settled():
            for unused in range(800):
                QTest.qWait(10)
                if not bridge.operationBusy:return
            self.fail((bridge.operationStatus,remote.call_args_list,CatalogCache(bridge.storage,bridge.settings['tactic_url']).image_valid(image),bridge.scannerState['store_games']))
        try:
            pane.setProperty('visible',True)
            with patch('ngs.app.installed_store_games',return_value=dict(rows=rows,warnings=[])),patch('ngs.bulk_scan.client_metadata',side_effect=metadata) as remote:
                bridge.scanStoreGames();settled()
                self.assertEqual(bridge.scannerState['store_games'][1]['state'],'artwork')
                pane.setProperty('currentIndex',0);pane.setProperty('focusZone',1)
                self.engine.gamepad.action.emit('activate');self.engine.gamepad.action.emit('activate')
                bridge.virtual_keyboard.hide();pane.setProperty('focusZone',2);pane.setProperty('actionIndex',1)
                self.engine.gamepad.action.emit('activate');settled()
                self.assertEqual(len(bridge.scannerState['store_games']),2)
                identity=bridge.scannerState['store_games'][0]['game_id'];identities.append(identity)
                receipt=next(item for item in bridge.library.reconcile() if item['card']['id']==identity)
                self.assertEqual(receipt['card']['description'],'Full store description')
                self.assertEqual(receipt['card']['cover'],image)
                self.assertNotIn('local.svg',next(card for card in bridge.localGames if card['id']==identity)['image'])
                pane.setProperty('focusZone',2);pane.setProperty('actionIndex',1)
                self.engine.gamepad.action.emit('activate');settled()
                old_after=next(item for item in bridge.library.reconcile() if item['card']['id']==identities[0])
                self.assertEqual(old_after['card']['description'],'Full store description')
                self.assertEqual(old_after['card']['name'],'My custom GOG title')
                self.assertEqual(bridge.library.launch_arguments(identities[0]),'-windowed')
                self.assertEqual(remote.call_count,2)
                self.assertEqual({folder:(folder/'play.exe').read_bytes() for folder in folders},before)
        finally:
            bridge.virtual_keyboard.hide();pane.setProperty('visible',False)
            QTest.qWait(350)
            for identity in set(identities):bridge.library.forget(identity)
            bridge._scanner,bridge._catalog_online=previous;bridge.scannerChanged.emit()
            bridge._installed=bridge.library.reconcile();bridge.libraryChanged.emit()

    def test_x_local_scanner_folder_exe_confirmation_and_safe_forget(self):
        import sys
        import json
        from unittest.mock import Mock, patch
        bridge, window = self.engine.bridge, self.window
        original = bridge._games.copy()
        game = self.root / "existing library/Existing Game"
        game.mkdir(parents=True)
        shutil.copyfile(sys.executable, game / "play.exe")
        (game / "setup.exe").write_bytes(b"installer fixture")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in game.iterdir()}
        scanner = window.findChild(QObject, "localScanner")
        details = window.findChild(QObject, "detailsPopup")
        def settled():
            for unused in range(500):
                QTest.qWait(20)
                if not bridge.operationBusy:
                    return
            self.fail(bridge.operationStatus)
        def click(name):
            item = window.findChild(QObject, name)
            point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
        try:
            card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
            image = {"path": "/assets/ngs_launcher/test.jpg", "sha256": "0" * 64, "size": 1}
            card.update(id="existing-game", name="Existing Game", source_folder="Existing Game",
                        executable="play.exe", revision=1, cover=image, background=image)
            bridge.catalog_loaded([dict(card, image="", backgroundImage="")])
            window.setProperty("section", 0)
            bridge.action.emit("toggle_installed")
            settled()
            QTest.qWait(350)
            self.assertTrue(scanner.property("visible"))
            field = window.findChild(QObject, "scannerFolderPath")
            field.setProperty("text", "")
            field.setProperty("focus", True)
            QTest.keyClick(window, Qt.Key.Key_E)
            self.assertEqual(window.property("section"), 0)
            self.assertEqual(field.property("text"), "e")
            field.setProperty("text", str(game.parent))
            click("scannerOpenPath")
            settled()
            # Controller enters the chosen game folder, Y scans, A selects an EXE.
            self.assertEqual(len(bridge.scannerState["folders"]), 1)
            bridge.action.emit("down")
            bridge.action.emit("activate")
            settled()
            self.assertEqual(Path(bridge.scannerState["path"]), game)
            bridge.action.emit("refresh")
            settled()
            self.assertEqual(bridge.scannerState["stage"], "exes")
            self.assertEqual(bridge.localGames, [])
            bridge.action.emit("activate")
            self.assertEqual(bridge.scannerState["stage"], "confirm")
            self.assertEqual(bridge.scannerState["exe"], "play.exe")
            self.assertEqual(len(bridge.scannerState["matches"]), 1)
            QTest.qWait(350)
            self.assertTrue(window.grabWindow().save(str(self.repo / "reports/local-scanner-confirm.png")))
            self.assertEqual(bridge.localGames, [])  # Selection alone must not import.
            click("scannerConfirm")
            settled()
            QTest.qWait(350)
            self.assertFalse(scanner.property("visible"))
            self.assertEqual(window.property("section"), 0)
            self.assertEqual(len(bridge.localGames), 1)
            identity = bridge.localGames[0]["id"]
            self.assertEqual(window.property("selected")["id"], identity)
            self.assertIn("existing-game", bridge.installedIds)
            self.assertIn(identity, bridge.externalIds)
            self.assertNotIn(identity, bridge.removableIds)
            bridge.setHideInstalled(True)
            self.assertEqual(bridge.games, [])
            bridge.setHideInstalled(False)
            # The catalog card launches the registered EXE, without needing a NAS manifest.
            fake = Mock(_foregrounded=True, _foreground_window=None, _edge_configured=None)
            fake.windows.return_value = []
            fake.active.return_value = True
            fake.exit_code.return_value = 0
            with patch("ngs.app.GameProcess", return_value=fake) as opened:
                bridge.performGameAction("existing-game")
                settled()
                self.assertEqual(opened.call_args.args, (game / "play.exe", game, []))
                self.assertFalse(window.isVisible())
                self.assertTrue(window.property("gameInProgress"))
                self.assertFalse(window.findChild(QObject, "launcherScene").property("visible"))
                fake.active.return_value = False
                bridge.check_game()
                self.assertTrue(window.isVisible())
            # Missing EXE stays visible and cannot become a NAS download.
            (game / "play.exe").rename(game / "missing.exe")
            bridge.refreshLocalLibrary()
            self.assertIn(identity, bridge.unavailableIds)
            self.assertFalse(window.property("canAct"))
            self.assertEqual(len(bridge.localGames), 1)
            bridge.performGameAction(identity)
            self.assertFalse(bridge.operationBusy)
            self.assertIn("недоступны", bridge.operationStatus)
            QMetaObject.invokeMethod(window, 'openDetails'); QTest.qWait(300)
            click('detailsMore'); QTest.qWait(100)
            remove = window.findChild(QObject, "removeGameButton")
            self.assertTrue(remove.property("visible"))
            self.assertTrue(remove.property("enabled"))
            self.assertEqual(remove.property("text"), "Убрать из библиотеки")
            QTest.qWait(600)
            click("removeGameButton")
            QTest.qWait(300)
            self.assertTrue(window.findChild(QObject, "removeDialog").property("visible"))
            self.assertFalse(window.property("removeSelected"))
            bridge.action.emit("activate")  # Cancel is selected before destructive confirmation.
            QTest.qWait(250)
            self.assertIn(identity, bridge.externalIds)
            self.assertTrue((game / "missing.exe").exists())
            (game / "missing.exe").rename(game / "play.exe")
            bridge.refreshLocalLibrary()
            QTest.qWait(600)
            self.assertTrue(window.grabWindow().save(str(self.repo / "reports/local-scanner-library.png")))
            window.setProperty("focusZone", 2)
            bridge.action.emit("menu")
            QTest.qWait(300)
            self.assertTrue(details.property("visible"))
            self.assertEqual(window.findChild(QObject, "removeGameButton").property("text"), "Убрать из библиотеки")
            bridge.action.emit("back")
            QTest.qWait(250)
            QMetaObject.invokeMethod(window, 'openDetails'); QTest.qWait(300)
            click('detailsMore'); QTest.qWait(100)
            click("removeGameButton")
            QTest.qWait(300)
            self.assertTrue(window.property("removeExternal"))
            self.assertFalse(window.findChild(QObject, "deleteGameConfirm").property("visible"))
            self.assertFalse(window.findChild(QObject, "deleteGameConfirm").property("enabled"))
            bridge.action.emit("right")
            bridge.action.emit("activate")
            settled()
            self.assertEqual(bridge.localGames, [])
            self.assertNotIn("existing-game", bridge.installedIds)
            self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in game.iterdir()}, before)
        finally:
            scanner.close()
            details.close()
            window.findChild(QObject, "removeDialog").close()
            settled()
            for identity in bridge.externalIds:
                bridge.library.forget_external(identity)
            bridge._installed = bridge.library.reconcile()
            bridge.libraryChanged.emit()
            bridge.setHideInstalled(False)
            bridge.catalog_loaded(original)
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(600)

    def test_y_forget_managed_game_from_controller_keeps_files_and_saves(self):
        from ngs.install import install_game, build_manifest
        from PySide6.QtCore import QMetaObject
        import json
        bridge, window = self.engine.bridge, self.window
        source = self.root / 'forget-ui-source'
        folder = source / 'ui-keep'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'play.exe').write_bytes(b'never executed')
        card = json.loads((self.repo / 'examples/black-mesa.json').read_text(encoding='utf8'))
        image = dict(path='/assets/ngs_launcher/test.jpg', size=1, sha256='0'*64)
        card.update(id='ui-keep', source_folder='ui-keep', executable='play.exe', revision=1, cover=image, background=image)
        target = self.storage.games / 'ui-keep'
        previous_size = window.size()
        try:
            install_game(self.storage, source, card, build_manifest(source, card))
            (target / 'save.dat').write_bytes(b'precious save')
            bridge.refreshLocalLibrary()
            window.setProperty('section', 0)
            window.setProperty('selectedIndex', next(i for i, row in enumerate(bridge.localGames) if row['id']=='ui-keep'))
            QTest.qWait(250)
            QMetaObject.invokeMethod(window, 'openRemove')
            QTest.qWait(300)
            self.assertFalse(window.property('removeSelected'))
            self.assertTrue(window.findChild(QObject, 'forgetGameConfirm').property('visible'))
            self.assertTrue(window.findChild(QObject, 'deleteGameConfirm').property('visible'))
            window.resize(800, 500)
            QTest.qWait(200)
            dialog = window.findChild(QObject, 'removeDialog')
            self.assertLessEqual(dialog.property('height'), window.height() - 20)
            button = window.findChild(QObject, 'deleteGameConfirm')
            bottom = button.mapToScene(QPointF(button.width(), button.height()))
            self.assertLessEqual(bottom.y(), window.height())
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/library-removal-800x500.png')))
            before = {p.relative_to(target): (p.read_bytes(), p.stat().st_mtime_ns) for p in target.rglob('*') if p.is_file()}
            bridge.action.emit('right')
            bridge.action.emit('activate')
            for unused in range(200):
                QTest.qWait(20)
                if not bridge.operationBusy: break
            self.assertFalse(bridge.operationBusy)
            self.assertNotIn('ui-keep', bridge.installedIds)
            self.assertIn('Файлы игры сохранены', bridge.operationStatus)
            self.assertEqual({p.relative_to(target): (p.read_bytes(), p.stat().st_mtime_ns) for p in target.rglob('*') if p.is_file()}, before)
        finally:
            window.findChild(QObject, 'removeDialog').close()
            bridge.stop()
            if target.exists():
                self.assertTrue(target.resolve().is_relative_to(self.root.resolve()))
                shutil.rmtree(target)
            bridge.refreshLocalLibrary()
            window.resize(previous_size)
            window.setProperty('section', 1)
            window.setProperty('selectedIndex', 0)

    def test_y_copy_launch_hide_and_return_with_controller_actions(self):
        import json
        import hashlib
        import shutil
        from unittest.mock import Mock, patch
        from ngs.catalog import CatalogCache
        from ngs.install import build_manifest, manifest_bytes
        bridge, window = self.engine.bridge, self.window
        original, previous_cache = bridge.games.copy(), bridge.cache
        previous_source = bridge.settings["games_source"]
        source = self.root / "nas"
        folder = source / "ui-sample"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "play.exe").write_bytes(b"test executable fixture")
        card = json.loads((self.repo / "examples/black-mesa.json").read_text(encoding="utf8"))
        image = {"path": "/assets/ngs_launcher/test.jpg", "sha256": "0" * 64, "size": 1}
        card.update(id="ui-sample", source_folder="ui-sample", executable="play.exe", revision=1, cover=image, background=image)
        data = manifest_bytes(build_manifest(source, card))
        card["manifest"] = {"path": "/assets/ngs_launcher/manifest.json", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        bridge.cache = CatalogCache(self.storage, "http://ui-test/tactic")
        bridge.settings["games_source"] = str(source)
        fake = Mock(_foregrounded=True, _foreground_window=None, _edge_configured=None)
        fake.windows.return_value = []
        fake.active.return_value = True
        fake.exit_code.return_value = 0
        try:
            bridge.catalog_loaded([dict(card, image="", backgroundImage="")])
            window.setProperty("section", 1)
            window.setProperty("focusZone", 2)
            with patch.object(bridge.cache, "fetch", return_value=data):
                bridge.action.emit("activate")
                QTest.qWait(250)
                self.wait_for_preview(bridge)
                self.assertEqual(window.property("section"), 1)
                self.assertEqual(bridge.downloadJobs, [])
                bridge.action.emit("activate")
                bridge.action.emit("activate")
                self.assertEqual(window.property("section"), 2)
                self.assertTrue(bridge.operationBusy)
                for unused in range(500):
                    QTest.qWait(20)
                    if not bridge.operationBusy:
                        break
            self.assertFalse(bridge.operationBusy, bridge.operationStatus)
            self.assertIn("ui-sample", bridge.installedIds)
            self.assertEqual(window.property("selected")["id"], "ui-sample")
            self.assertEqual((self.storage.games / "Black Mesa/play.exe").read_bytes(), b"test executable fixture")
            self.assertEqual(window.property("section"), 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(600)
            self.assertTrue(window.property("selectedInstalled"))
            item = window.findChild(QObject, "gameCards").property("currentItem")
            self.assertEqual(item.findChild(QObject, "cardStatus-0").property("text"), "НОВАЯ")
            QMetaObject.invokeMethod(window, 'openDetails'); QTest.qWait(300)
            QMetaObject.invokeMethod(window.findChild(QObject, 'detailsMore'), 'clicked'); QTest.qWait(100)
            remove = window.findChild(QObject, "removeGameButton")
            self.assertTrue(remove.property("visible"))
            self.assertEqual(remove.property("text"), "Убрать из библиотеки")
            point = remove.mapToScene(QPointF(remove.width() / 2, remove.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
            QTest.qWait(300)
            self.assertTrue(window.findChild(QObject, "removeDialog").property("visible"))
            self.assertFalse(window.property("removeExternal"))
            bridge.action.emit("activate")
            QTest.qWait(250)
            self.assertTrue((self.storage.games / "Black Mesa/play.exe").exists())
            with patch("ngs.app.GameProcess", return_value=fake) as opened:
                bridge.action.emit("activate")
                QTest.qWait(380)
                for unused in range(500):
                    QTest.qWait(20)
                    if not bridge.operationBusy:
                        break
                opened.assert_called_once()
                self.assertFalse(window.isVisible())
                self.assertTrue(window.property("gameInProgress"))
                self.assertFalse(window.findChild(QObject, "launcherScene").property("visible"))
                self.assertFalse(self.engine.gamepad.active)
                running = window.findChild(QObject, 'runningGameTitle')
                self.assertTrue(running.property('visible'))
                self.assertEqual(running.property('text'), card['name'])
                self.assertTrue(window.property('gameInProgress'))
                fake.active.return_value = False
                bridge.check_game()
                self.assertFalse(running.property('visible'))
                self.assertTrue(window.isVisible())
                self.assertEqual(window.property("section"), 0)
                self.assertEqual(window.property("selected")["id"], "ui-sample")
                fake.close.assert_called_once()
            QTest.qWait(250)
            window.setProperty("section", 1)
            window.setProperty('focusZone', 0)
            window.setProperty('navigationIndex', 6)
            bridge.action.emit("activate")
            self.assertTrue(bridge.hideInstalled, str({"section": window.property("section"),
                "details": window.findChild(QObject, "detailsPopup").property("visible"),
                "scanner": window.findChild(QObject, "localScanner").property("visible"),
                "messages": self.messages[-8:]}))
            self.assertEqual(bridge.games, [])
            self.assertTrue(self.storage.read_settings()["hide_installed"])
            bridge.action.emit("activate")
            self.assertEqual(len(bridge.games), 1)
            QTest.qWait(600)
            item = window.findChild(QObject, "gameCards").property("currentItem")
            self.assertEqual(item.findChild(QObject, "cardStatus-0").property("text"), "")
            self.assertFalse(item.findChild(QObject, "cardStatus-0").parentItem().isVisible())
            window.setProperty("focusZone", 2)
            bridge.action.emit("menu")
            QTest.qWait(250)
            window.setProperty("detailsAction", 8)
            bridge.action.emit("activate")
            self.assertTrue(window.property("detailsMore"))
            window.setProperty("detailsAction", 2)
            bridge.action.emit("activate")
            QTest.qWait(300)
            dialog = window.findChild(QObject, "removeDialog")
            self.assertTrue(dialog.property("visible"))
            bridge.action.emit("activate")
            QTest.qWait(100)
            self.assertIn("ui-sample", bridge.installedIds)
            bridge.action.emit("menu")
            QTest.qWait(250)
            window.setProperty("detailsAction", 8)
            bridge.action.emit("activate")
            self.assertTrue(window.property("detailsMore"))
            window.setProperty("detailsAction", 2)
            bridge.action.emit("activate")
            QTest.qWait(300)
            bridge.action.emit("right")
            bridge.action.emit("right")  # Keep-files removal precedes explicit file deletion.
            bridge.action.emit("activate")
            for unused in range(500):
                QTest.qWait(20)
                if not bridge.operationBusy:
                    break
            self.assertNotIn("ui-sample", bridge.installedIds)
            self.assertNotIn("ui-sample", bridge.removingIds)
            self.assertFalse((self.storage.games / "Black Mesa").exists())
            self.assertTrue((folder / "play.exe").is_file())
        finally:
            bridge.stop()
            target = self.storage.games / "Black Mesa"
            self.assertTrue(target.resolve().is_relative_to(self.root.resolve()))
            if target.exists():
                shutil.rmtree(target)
            bridge._installed = bridge.library.reconcile()
            bridge.libraryChanged.emit()
            bridge.setHideInstalled(False)
            bridge.cache = previous_cache
            bridge.settings["games_source"] = previous_source
            bridge.catalog_loaded(original)
            window.setProperty("section", 1)
            window.setProperty("selectedIndex", 0)
            window.setProperty("focusZone", 2)
            QTest.qWait(600)

    def test_zz_catalog_replacement_keeps_identity_and_handles_empty(self):
        bridge, window = self.engine.bridge, self.window
        original = bridge.games.copy()
        try:
            window.setProperty("selectedIndex", 1)
            bridge.catalog_loaded(list(reversed(original)))
            self.assertEqual(window.property("selectedIndex"), 3)
            QTest.qWait(600)
            self.assertEqual(window.property("heroGame")["id"], "afterlight")
            bridge.catalog_loaded([])
            for action in ("left", "right", "activate", "back"):
                bridge.action.emit(action)
            QTest.qWait(600)
            self.assertEqual(window.property("selectedIndex"), 0)
            self.assertFalse(window.findChild(QObject, "detailsPopup").property("visible"))
            self.assertFalse(window.property("hasGames"))
            self.assertEqual(window.property("heroGame").toVariant()["id"], "")
            self.assertFalse(window.grabWindow().isNull())
        finally:
            bridge.catalog_loaded(original)
            window.setProperty("focusZone", 2)
            QTest.qWait(600)
        long_card = dict(original[0], description=("Подробное описание игры. " * 100))
        try:
            bridge.catalog_loaded([long_card])
            bridge.action.emit("menu")
            QTest.qWait(300)
            scroll = window.findChild(QObject, "detailsScroll").property("contentItem")
            self.assertGreater(scroll.property("contentHeight"), scroll.property("height"))
            window.setProperty("detailsAction", 7)
            bridge.action.emit("down")
            self.assertGreater(scroll.property("contentY"), 0)
            bridge.action.emit("up")
            self.assertEqual(scroll.property("contentY"), 0)
            bridge.action.emit("back")
            QTest.qWait(250)
        finally:
            bridge.catalog_loaded(original)
            QTest.qWait(600)
        failures = [message for message in self.messages if any(term in message for term in (
            "ReferenceError", "TypeError", "Cannot assign", "Binding loop", "failed to load"))]
        self.assertEqual(failures, [])

    def test_zzz_settings_shortcut_save_controller_and_validation(self):
        bridge, window = self.engine.bridge, self.window
        previous = dict(bridge.settings)
        old_paths = self.storage.games, self.storage.game_staging
        self.assertIsNone(window.findChild(QObject, 'settingsButton'))
        pane = window.findChild(QObject, 'settingsPane')
        bridge.action.emit('settings')
        QTest.qWait(350)
        self.assertTrue(pane.property('visible'))
        try:
            values = dict(bridge.settingsState, games_source=str(self.root / 'settings-nas'),
                          games_directory=str(self.root / 'custom-games'), resolve_local_metadata=False)
            self.assertTrue(bridge.applySettings(values))
            self.assertEqual(self.storage.games, self.root / 'custom-games')
            self.assertEqual(self.storage.read_settings()['games_directory'], values['games_directory'])
            self.assertTrue(bridge.setGraphicsOptions('vulkan', False))
            self.assertEqual(self.storage.read_settings()['graphics_api'], 'vulkan')
            self.assertTrue(bridge.applySettings(dict(values, graphics_api='vulkan', disable_performance_overlays=False)))
            self.assertIn('Перезапускаю NGS', bridge.settingsStatus)
            saved = self.storage.read_settings()
            self.assertFalse(bridge.applySettings(dict(values, games_directory='data/cache')))
            self.assertEqual(self.storage.read_settings(), saved)
            self.assertEqual(self.storage.games, self.root / 'custom-games')
            self.assertFalse(bridge.applySettings(dict(values, tactic_url='http://user:pass@server/tactic')))
            self.assertEqual(self.storage.read_settings(), saved)
            self.assertIsNone(pane.findChild(QObject, 'settingsSource'))
            destination_field = pane.findChild(QObject, 'settingsDestination')
            roots = self.storage.read_settings().get('games_sources', [self.storage.read_settings()['games_source']])
            targets = [str(self.root / 'arbitrary games folder'), str(self.root / 'another disk')]
            destination_field.setProperty('text', '\n'.join(targets))
            from PySide6.QtCore import QMetaObject
            self.assertTrue(QMetaObject.invokeMethod(pane, 'save'))
            self.assertEqual(self.storage.read_settings()['games_sources'], roots)
            self.assertEqual(self.storage.read_settings()['games_directories'], targets)
            self.assertEqual(self.storage.games, Path(targets[0]))
            bridge.action.emit('back')
            QTest.qWait(200)
            self.assertFalse(pane.property('visible'))
            QMetaObject.invokeMethod(window, "openMenu")
            bridge.action.emit('refresh')
            QTest.qWait(350)
            self.assertTrue(pane.property('visible'))
            QTest.keyClick(window, Qt.Key.Key_Escape)
            QTest.qWait(200)
            self.assertFalse(pane.property('visible'))
            self.assertTrue(window.grabWindow().save(str(self.repo / 'reports/settings-preview.png')))
        finally:
            bridge.settings = previous
            self.storage.games, self.storage.game_staging = old_paths
            self.storage.write_settings(previous)
            bridge.settingsChanged.emit()
            pane.setProperty('visible', False)

    def test_zzzz_existing_external_game_metadata_picker_confirms_result(self):
        import sys
        import json
        from unittest.mock import patch
        bridge, window = self.engine.bridge, self.window
        folder = self.root / 'Unknown.Local.Game'
        folder.mkdir(exist_ok=True)
        shutil.copyfile(sys.executable, folder / 'play.exe')
        receipt = bridge.library.register_external(folder, 'play.exe', folder.name)
        identity = receipt['card']['id']
        bridge._installed = bridge.library.reconcile()
        bridge.libraryChanged.emit()
        card = json.loads((self.repo / 'examples/black-mesa.json').read_text(encoding='utf8'))
        image = {'path': '/assets/ngs_launcher/test.jpg', 'sha256': '0' * 64, 'size': 1}
        card.update(id='steam-12', name='Beautiful Game', source_folder='', executable='', available_on_nas=False,
                    revision=1, cover=image, background=image)
        def metadata(storage, url, operation, value, cancel, progress=None):
            return [{'app_id':'12', 'name':'Beautiful Game'}] if operation == 'search' else {'card':card, 'message':'From TACTIC'}
        def settled():
            for i in range(500):
                QTest.qWait(10)
                if not bridge.operationBusy: return
            self.fail('Metadata operation timed out')
        picker = window.findChild(QObject, 'metadataPicker')
        try:
            with patch('ngs.app.client_metadata', side_effect=metadata):
                bridge.findMetadata(identity)
                settled()
                self.assertTrue(picker.property('visible'))
                self.assertEqual(bridge.metadataState['results'][0]['app_id'], '12')
                self.assertEqual(bridge.library.reconcile()[0]['card']['name'], folder.name)
                bridge.action.emit('activate')
                settled()
                QTest.qWait(250)
                self.assertFalse(picker.property('visible'))
                updated = next(r for r in bridge.library.reconcile() if r['card']['id'] == identity)
                self.assertEqual(updated['card']['name'], 'Beautiful Game')
                self.assertEqual(updated['executable_sha256'], receipt['executable_sha256'])
                self.assertFalse(updated['card']['available_on_nas'])
                self.assertNotIn(identity, bridge.playedIds)
                self.assertNotIn('steam-12', bridge.playedIds)
                bridge.library.mark_played(identity)
                bridge._installed = bridge.library.reconcile()
                bridge.libraryChanged.emit()
                self.assertIn(identity, bridge.playedIds)
                self.assertIn('steam-12', bridge.playedIds)
                self.assertTrue((folder / 'play.exe').is_file())
                from types import SimpleNamespace
                import threading
                unrelated = SimpleNamespace(mode='install', cancel=threading.Event())
                try:
                    bridge.game_worker = unrelated
                    picker.setProperty('visible', True)
                    bridge.action.emit('back')
                    QTest.qWait(200)
                    self.assertFalse(picker.property('visible'))
                    self.assertFalse(unrelated.cancel.is_set())
                finally:
                    bridge.game_worker = None
        finally:
            bridge.library.forget_external(identity)
            bridge._installed = bridge.library.reconcile()
            bridge.libraryChanged.emit()
            picker.setProperty('visible', False)

    def test_zzzzz_server_change_ignores_old_worker_and_keeps_previous_installs(self):
        import threading
        from ngs.app import LauncherBridge, CatalogWorker
        from ngs.catalog import CatalogCache
        from unittest.mock import patch
        storage = PortableStorage(self.root / 'changed-server')
        bridge = LauncherBridge(storage, demo=True)
        old = CatalogWorker(CatalogCache(storage, 'http://old-server/tactic'), bridge)
        bridge.cache = old.cache
        bridge.worker = old
        old.loaded.connect(bridge.catalog_loaded)
        old.failed.connect(bridge.catalog_failed)
        old.finished.connect(bridge.catalog_finished)
        try:
            with patch.object(bridge, 'refreshCatalog') as refresh:
                values = dict(bridge.settingsState, tactic_url='http://new-server/tactic')
                self.assertTrue(bridge.applySettings(values))
                self.assertTrue(old.cancel.is_set())
                old.loaded.emit([{'id':'stale-card'}])
                old.failed.emit()
                self.assertEqual(bridge._games, [])
                self.assertNotIn('stale-card', str(bridge.games))
                old.finished.emit()
                refresh.assert_called_once()
                self.assertEqual(storage.read_settings()['tactic_url'], 'http://new-server/tactic')
        finally:
            bridge.stop()
            bridge.download_queue.close()
            bridge.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
