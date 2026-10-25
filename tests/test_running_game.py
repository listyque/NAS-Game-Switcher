import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, QPointF, QMetaObject
from PySide6.QtTest import QTest
from tests import test_ui
from ngs.play_time import PlayTime


class RunningGameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.bridge = self.engine.bridge
        self.original = self.bridge.play_time
        self.now = 0.0
        self.bridge.play_time = PlayTime(self.bridge.library.path, lambda: self.now)
        self.bridge.play_time.start('running-fixture')
        self.bridge._running_id, self.bridge._running_name = 'running-fixture', 'Игра для проверки'
        self.bridge._terminating = False
        self.bridge._auto_foreground = False
        self.process = Mock(frozen=False, _foregrounded=True, _foreground_window=None, _edge_configured=None)
        self.process.windows.return_value = []
        self.process.freeze.side_effect = lambda: setattr(self.process,'frozen',True)
        self.process.thaw.side_effect = lambda: setattr(self.process,'frozen',False)
        self.process.active.return_value = True
        self.process.exit_code.return_value = 0
        self.bridge.process = self.process
        self.bridge.runningChanged.emit()
        self.bridge.gameStarted.emit()
        self.bridge.desktop_controls.action.emit('show')
        self.panel = self.window.findChild(QObject,'playingBackground')
        self.frozen = self.window.findChild(QObject,'frozenDashboard')
        QTest.qWait(100)

    def tearDown(self):
        self.engine.power.sleeping = False
        self.bridge.play_time.checkpoint(reason='test_finished')
        self.bridge.play_time = self.original
        self.bridge.process = None
        self.bridge.process_timer.stop()
        self.bridge._terminating = False
        self.bridge._game_control_error = self.bridge._statistics_error = self.bridge._power_error = ''
        self.bridge.freezeChanged.emit(); self.bridge.gameReturned.emit(); self.bridge.runningChanged.emit()
        with closing(sqlite3.connect(self.bridge.library.path)) as db, db:
            db.execute("DELETE FROM play_resets WHERE game_id='running-fixture'")
            db.execute("DELETE FROM play_sessions WHERE game_id='running-fixture'")
            db.execute("DELETE FROM play_history WHERE game_id='running-fixture'")
        QTest.qWait(100)

    def visual(self, item, name):
        if item.objectName() == name: return item
        for child in item.childItems():
            result = self.visual(child,name)
            if result: return result

    def test_launch_keeps_statistics_behind_game_without_desktop_gap(self):
        from PySide6.QtGui import QGuiApplication
        self.bridge._auto_foreground = True
        self.bridge._foreground_attempts = 0
        self.bridge._foreground_windows = frozenset()
        self.process._foregrounded = False
        with patch.object(QGuiApplication, 'platformName', return_value='windows'), \
                patch('ngs.process.set_topmost') as topmost:
            self.bridge.gameStarted.emit()
            self.assertTrue(self.window.isVisible())
            self.assertTrue(self.panel.property('visible'))
            self.assertFalse(self.engine.gamepad.active)
            self.assertTrue(all(not call.args[1] for call in topmost.call_args_list))
        self.bridge.check_game()
        self.assertTrue(self.window.isVisible())
        self.process.windows.return_value = [(101, 7)]
        self.process.foreground.side_effect = OSError('not ready')
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'):
            self.bridge.check_game()
            self.assertTrue(self.window.isVisible())
            self.process.foreground.side_effect = None
            self.process.foreground.return_value = True
            self.bridge.check_game()
        self.assertTrue(self.window.isVisible(), 'Statistics stay behind windowed games and splash screens')
        with patch.object(self.window, 'isActive', return_value=False):
            self.window.activeChanged.emit()
            self.assertFalse(self.engine.gamepad.active)

    def test_minimize_ngs_button_reaches_desktop_and_preserves_game_session(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen):
                if frozen: self.bridge.toggleGameFreeze()
                self.bridge.desktop_controls.action.emit('show')
                QTest.qWait(30)
                panel = self.frozen if frozen else self.panel
                panel.setProperty('selectedAction', 0)
                for _ in range(3 if frozen else 2): self.bridge.action.emit('down')
                self.assertEqual(panel.property('selectedAction'), 6 if frozen else 4)
                button = self.visual(panel, 'minimizeNgsButton')
                self.assertIsNotNone(button)
                self.assertTrue(button.property('enabled'))
                session = self.bridge.play_time.session
                self.process.park_windows.reset_mock()
                self.process.foreground.reset_mock()
                self.bridge.action.emit('activate')
                self.assertIn(self.window.visibility(), (self.window.Visibility.Hidden, self.window.Visibility.Minimized))
                self.assertIs(self.bridge.play_time.session, session)
                self.process.park_windows.assert_not_called()
                self.process.foreground.assert_not_called()
                self.process.terminate.assert_not_called()
                self.assertEqual(self.process.frozen, frozen)
                QTest.qWait(450)
                self.assertIn(self.window.visibility(), (self.window.Visibility.Hidden, self.window.Visibility.Minimized))

    def test_reset_statistics_requires_confirmation_and_preserves_process_and_session(self):
        for frozen in (False, True):
            with self.subTest(frozen=frozen):
                if frozen: self.bridge.toggleGameFreeze()
                panel = self.frozen if frozen else self.panel
                self.now += 23
                self.bridge.update_running(force=True)
                session = self.bridge.runningState['session_id']
                index = 7 if frozen else 5
                button = self.visual(panel, 'resetGameStatisticsButton')
                self.assertIsNotNone(button)
                QMetaObject.invokeMethod(button, 'clicked')
                self.assertTrue(panel.property('confirming'))
                self.assertTrue(panel.property('confirmReset'))
                self.bridge.action.emit('activate')
                self.assertGreater(self.bridge.runningState['total_seconds'] + self.bridge.runningState['paused_seconds'], 0)
                panel.setProperty('selectedAction', index); self.bridge.action.emit('activate')
                self.bridge.action.emit('back')
                self.assertFalse(panel.property('confirming'))
                self.process.freeze.reset_mock(); self.process.thaw.reset_mock(); self.process.foreground.reset_mock()
                panel.setProperty('selectedAction', index); self.bridge.action.emit('activate')
                self.bridge.action.emit('right'); self.bridge.action.emit('activate')
                state = self.bridge.runningState
                self.assertEqual([state[k] for k in ('total_seconds','session_seconds','launches','longest_seconds','paused_seconds')], [0]*5)
                self.assertTrue(state['local_only'])
                self.assertEqual(state['session_id'], session)
                self.assertEqual(self.process.frozen, frozen)
                self.process.terminate.assert_not_called(); self.process.freeze.assert_not_called()
                self.process.thaw.assert_not_called(); self.process.foreground.assert_not_called()

    def test_reset_rejects_stale_session_and_reports_write_failure(self):
        self.now = 15
        self.bridge.update_running(force=True)
        session = self.bridge.runningState['session_id']
        self.assertFalse(self.bridge.resetGameStatistics('another-session'))
        self.assertEqual(self.bridge.runningState['total_seconds'], 15)
        with patch.object(self.bridge.play_time, 'reset', side_effect=sqlite3.OperationalError('disk busy')):
            self.assertFalse(self.bridge.resetGameStatistics(session))
        self.assertIn('disk busy', self.bridge.runningState['error'])
        self.assertEqual(self.bridge.runningState['total_seconds'], 15)

    def test_toggle_calls_inactive_visible_dashboard_instead_of_desktop(self):
        self.window.showNormal()
        self.process.park_windows.reset_mock()
        self.bridge._auto_foreground = True
        with patch.object(self.window, 'isActive', return_value=False), \
                patch.object(self.window, 'requestActivate'):
            self.bridge.desktop_controls.action.emit('toggle')
            QTest.qWait(30)
            self.assertTrue(self.window.isVisible())
            self.assertNotIn(self.window.visibility(), (self.window.Visibility.Minimized, self.window.Visibility.Hidden))
            self.assertTrue(self.panel.property('visible'))
            self.process.park_windows.assert_not_called()
            self.assertFalse(self.bridge._auto_foreground)

    def test_show_parks_game_only_after_dashboard_activation_and_frame(self):
        self.process.park_windows.reset_mock()
        with patch.object(self.window, 'isActive', return_value=False) as active, \
                patch.object(self.window, 'requestActivate'):
            self.bridge.desktop_controls.action.emit('show')
            self.window.frameSwapped.emit()
            QTest.qWait(30)
            self.process.park_windows.assert_not_called()
            active.return_value = True
            self.window.activeChanged.emit()
            self.window.frameSwapped.emit()
            QTest.qWait(30)
            self.process.park_windows.assert_called_once_with()
            self.window.frameSwapped.emit()
            QTest.qWait(30)
            self.process.park_windows.assert_called_once_with()

    def test_toggle_from_dashboard_returns_to_game_without_desktop_action(self):
        self.process.park_windows.reset_mock()
        self.process.foreground.reset_mock()
        with patch.object(self.window, 'isActive', return_value=True):
            self.bridge.desktop_controls.action.emit('toggle')
        self.process.park_windows.assert_not_called()
        self.process.foreground.assert_called_once_with()
        self.assertFalse(self.window.isVisible())

    def test_return_cancels_pending_dashboard_frame_before_later_activation(self):
        self.process.park_windows.reset_mock()
        with patch.object(self.window, 'isActive', return_value=False) as active, \
                patch.object(self.window, 'requestActivate'):
            self.bridge.desktop_controls.action.emit('show')
            self.bridge.returnToGame()
            QTest.qWait(450)
            self.window.showNormal()
            active.return_value = True
            self.window.activeChanged.emit()
            self.window.frameSwapped.emit()
            QTest.qWait(30)
        self.process.park_windows.assert_not_called()

    def test_pending_dashboard_cannot_park_after_sleep_close_or_process_replacement(self):
        original = self.process
        with patch.object(self.window, 'isActive', return_value=False) as active, \
                patch.object(self.window, 'requestActivate'):
            for cancel in ('sleep', 'close', 'replacement'):
                with self.subTest(cancel=cancel):
                    self.bridge.process = original
                    active.return_value = False
                    original.park_windows.reset_mock()
                    self.bridge.desktop_controls.action.emit('show')
                    try:
                        if cancel == 'sleep':
                            self.engine.power.sleeping = True
                            self.bridge.freezeChanged.emit()
                            self.engine.power.sleeping = False
                        elif cancel == 'close':
                            self.bridge._closing = True
                        else:
                            self.bridge.process = Mock(frozen=False)
                        active.return_value = True
                        self.window.frameSwapped.emit()
                        QTest.qWait(10)
                        original.park_windows.assert_not_called()
                        if cancel == 'replacement': self.bridge.process.park_windows.assert_not_called()
                    finally:
                        self.bridge.process = original
                        self.bridge._closing = self.engine.power.sleeping = False
                        self.bridge.gameStarted.emit()

    def test_game_handoff_and_service_button_recover_stale_python_window_reference(self):
        import gc, types, sys
        from PySide6.QtQuick import QQuickWindow
        from shiboken6 import delete, isValid
        gc.collect()
        callbacks = [obj for obj in gc.get_objects() if isinstance(obj, types.FunctionType)
                     and obj.__qualname__ in ('create_window.<locals>.background_for_game', 'create_window.<locals>.current_launcher_window')]
        cells = [obj.__closure__[obj.__code__.co_freevars.index('window')] for obj in callbacks
                 if 'window' in obj.__code__.co_freevars]
        cell = next(cell for cell in cells if cell.cell_contents is self.window)
        stale = QQuickWindow(); delete(stale)
        self.assertFalse(isValid(stale))
        original = self.window
        errors = []
        original.setProperty('gameInProgress', False)
        try:
            cell.cell_contents = stale
            with patch.object(sys, 'excepthook', side_effect=lambda *error: errors.append(error)):
                self.bridge.gameStarted.emit()
                self.assertFalse(original.isVisible())
                self.assertTrue(original.property('gameInProgress'))
                self.assertFalse(self.engine.gamepad.active)
                self.bridge.desktop_controls.action.emit('show')
                QTest.qWait(30)
                self.assertTrue(original.isVisible())
                self.assertTrue(self.panel.property('visible'))
            self.assertFalse(errors)
            self.assertTrue(isValid(original))
        finally:
            cell.cell_contents = original

    def test_missing_authoritative_window_stops_ui_gamepad_and_service_returns_to_game(self):
        with patch.object(self.engine, 'rootObjects', return_value=[]), patch('ngs.app.crash_capture.mark') as marked:
            self.bridge.desktop_controls.action.emit('show')
            self.bridge.desktop_controls.action.emit('show')
            self.assertFalse(self.engine.gamepad.active)
            self.assertEqual(self.process.foreground.call_count, 2)
            missing = [call for call in marked.call_args_list if call.args == ('window.unavailable',)]
            self.assertEqual(len(missing), 1)
        self.bridge.desktop_controls.action.emit('show')
        self.assertFalse(self.engine._ngs_window_missing)

    def test_process_creation_waits_for_animation_and_frame_even_after_spec_worker_finishes(self):
        bridge = self.bridge
        bridge.play_time.checkpoint(reason='test_finished')
        bridge.process = None
        bridge._launching = True
        bridge._launch_animation_done = bridge._launch_presented = False
        bridge.game_worker = SimpleNamespace(mode='launch', deleteLater=Mock())
        with patch('ngs.app.GameProcess', return_value=self.process) as create:
            bridge.operation_completed((Path('fixture.exe'), Path.cwd(), []))
            create.assert_not_called()
            bridge.operation_finished()
            self.assertTrue(bridge.operationBusy)
            bridge.launchPresentationReady('another-game')
            self.assertFalse(bridge._launch_animation_done)
            bridge.launchPresentationReady('running-fixture')
            create.assert_not_called()
            bridge.launch_frame_presented()
            create.assert_called_once()
            self.assertFalse(bridge.launchingGame)
            self.assertTrue(self.window.isVisible())
            bridge.launch_frame_presented()
            create.assert_called_once()

    def test_cancel_sleep_and_close_invalidate_prepared_launch(self):
        bridge = self.bridge
        bridge.play_time.checkpoint(reason='test_finished')
        bridge.process = None
        for action in ('cancel', 'sleep', 'close'):
            with self.subTest(action=action), patch('ngs.app.GameProcess') as create:
                bridge._launching = True
                bridge._launch_presented = False
                bridge._pending_launch = (Path('fixture.exe'), Path.cwd(), [])
                if action == 'cancel':
                    bridge.cancelOperation()
                elif action == 'sleep':
                    bridge.freeze_for_sleep()
                else:
                    bridge._closing = True
                bridge.launchPresentationReady('running-fixture')
                bridge.launch_frame_presented()
                create.assert_not_called()
                bridge._sleeping = bridge._closing = False
                bridge.cancelOperation()

    def test_auto_focus_retries_failure_then_follows_splash_replacement_without_replaying_launch(self):
        bridge = self.bridge
        bridge._auto_foreground = True
        bridge._foreground_attempts = 0
        self.process._foregrounded = False
        self.process.windows.return_value = [(101, 7)]
        self.process.foreground.side_effect = OSError('focus denied')
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'), patch.object(bridge, 'gameStarted') as started:
            bridge.check_game()
            self.assertFalse(self.process._foregrounded)
            self.assertIn('focus denied', bridge.runningState['error'])
            self.process.foreground.side_effect = None
            bridge.check_game()
            self.assertTrue(self.process._foregrounded)
            self.assertEqual(self.process.foreground.call_count, 2)
            self.process._foreground_window = (101, 7)
            self.process.windows.return_value = [(202, 9)]
            bridge.check_game()
            self.assertEqual(self.process.foreground.call_count, 3)
            started.emit.assert_not_called()
            bridge.minimizeGame(False)
            bridge.check_game()
            self.assertEqual(self.process.foreground.call_count, 3)

    def test_auto_focus_rechecks_new_main_window_while_splash_stays_visible(self):
        bridge = self.bridge
        bridge._auto_foreground = True
        bridge._foreground_attempts = 0
        bridge._foreground_windows = frozenset([(101, 7)])
        self.process._foregrounded = True
        self.process._foreground_window = (101, 7)
        self.process.windows.return_value = [(101, 7), (202, 9)]
        def foreground(**options):
            if self.process.foreground.call_count == 1:
                self.process._foregrounded = False
                raise OSError('main window focus denied')
            self.process._foreground_window = (202, 9)
            self.process._foregrounded = True
            return True
        self.process.foreground.side_effect = foreground
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'), patch('ngs.app.event') as logged:
            bridge.check_game()
            self.process.foreground.assert_called_once_with(automatic=True)
            self.assertIn('main window focus denied', bridge.runningState['error'])
            bridge.check_game()
            self.assertEqual(self.process.foreground.call_count, 2)
            self.assertEqual(bridge.runningState['error'], '')
            bridge.check_game()
            self.assertEqual(self.process.foreground.call_count, 2)
            changes = [call for call in logged.call_args_list if call.args == ('game.windows',)]
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].kwargs['windows'], [(101, 7), (202, 9)])

    def test_auto_focus_stops_when_process_reports_user_switched_away(self):
        bridge = self.bridge
        bridge._auto_foreground = True
        bridge._foreground_attempts = 0
        self.process._foregrounded = True
        self.process._foreground_window = (101, 7)
        self.process.windows.return_value = [(101, 7), (202, 9)]
        self.process.foreground.return_value = False
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'):
            bridge.check_game()
            self.assertFalse(bridge._auto_foreground)
            self.process.windows.return_value = [(303, 9)]
            bridge.check_game()
            self.process.foreground.assert_called_once()

    def test_auto_focus_failure_is_bounded_and_manual_return_still_works(self):
        bridge = self.bridge
        bridge._auto_foreground = True
        bridge._foreground_attempts = 0
        self.process._foregrounded = False
        self.process.windows.return_value = [(101, 7)]
        self.process.foreground.side_effect = OSError('focus denied')
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'):
            for _ in range(12):
                bridge.check_game()
        self.assertEqual(self.process.foreground.call_count, 10)
        self.process.windows.return_value = [(202, 9)]
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'):
            bridge.check_game()
        self.assertEqual(self.process.foreground.call_count, 11)
        self.process.foreground.side_effect = None
        bridge.returnToGame()
        self.assertEqual(self.process.foreground.call_count, 12)

    def test_top_panel_opens_and_closes_without_parking_game_or_showing_launcher(self):
        overlay = self.bridge.status_overlay
        self.process.park_windows.reset_mock(); self.process.foreground.reset_mock()
        self.bridge.gameStarted.emit()
        self.assertFalse(self.window.isVisible())
        with patch.object(overlay, 'refreshSystemStatus'):
            overlay.last_request = 0
            overlay.request(self.window.screen().name())
            QTest.qWait(350)
            self.assertTrue(overlay.visible)
            self.assertFalse(self.window.isVisible())
            overlay.hide()
            QTest.qWait(250)
            self.assertFalse(overlay.shown)
        self.process.park_windows.assert_not_called()
        self.process.foreground.assert_not_called()
        self.assertFalse(self.window.isVisible())

    def test_service_show_waits_for_a_frame_and_explicit_desktop_preserves_session(self):
        session=self.bridge.play_time.session
        self.process.park_windows.reset_mock()
        self.bridge.desktop_controls.action.emit('show')
        self.process.park_windows.assert_not_called()
        with patch.object(self.window, 'isActive', return_value=True):
            self.window.frameSwapped.emit()
            QTest.qWait(30)
        self.process.park_windows.assert_called_once_with()
        self.assertTrue(self.panel.property('visible'))
        self.assertIs(self.bridge.play_time.session,session)
        self.process.park_windows.reset_mock()
        self.bridge.desktop_controls.action.emit('desktop')
        self.process.park_windows.assert_called_once_with()
        self.process.terminate.assert_not_called(); self.process.freeze.assert_not_called()
        self.assertIs(self.bridge.play_time.session,session)
        self.bridge.desktop_controls.action.emit('show')
        self.bridge.returnToGame()
        self.process.foreground.assert_called_once_with()

    def test_frozen_game_returns_to_library_badge_and_resumes_same_session(self):
        bridge, window = self.bridge, self.window
        installed = bridge._installed
        try:
            bridge._installed = [{'card': {'id':'running-fixture','name':'Игра'}, 'catalog_id':'steam-1'}]
            bridge.toggleGameFreeze();QTest.qWait(100)
            self.assertIn('running-fixture',bridge.frozenIds)
            self.assertIn('steam-1',bridge.frozenIds)
            session = bridge.runningState['session_id']
            self.frozen.setProperty('selectedAction',4);bridge.action.emit('activate');QTest.qWait(100)
            self.assertTrue(self.process.frozen)
            self.assertFalse(window.property('gameInProgress'))
            self.assertFalse(window.findChild(QObject,'frozenScreen').property('visible'))
            self.assertTrue(window.property('frozenLibraryVisible'))
            bridge.action.emit('next_section')
            self.assertFalse(window.property('gameInProgress'))
            bridge.performGameAction('steam-1');QTest.qWait(100)
            self.assertFalse(self.process.frozen)
            self.assertEqual(bridge.runningState['session_id'],session)
            self.assertEqual(bridge.frozenIds,[])
        finally:
            bridge._installed=installed
            window.setProperty('frozenLibraryVisible',False)

    def test_memory_cleanup_runs_off_ui_keeps_freeze_and_reports_result(self):
        self.bridge.toggleGameFreeze();QTest.qWait(50)
        self.process.trim_working_set.return_value = dict(before=100*1048576,after=5*1048576,failed=0)
        self.frozen.setProperty('selectedAction',5);self.bridge.action.emit('activate')
        for unused in range(100):
            QTest.qWait(10)
            if self.bridge._memory_worker is None: break
        self.assertIsNone(self.bridge._memory_worker)
        self.assertTrue(self.bridge.gameFrozen)
        self.assertIn('100 → 5',self.bridge.runningState['memory_status'])
        self.process.trim_working_set.assert_called_once()
        self.process.trim_working_set.side_effect=OSError('denied')
        self.bridge.trimGameMemory()
        for unused in range(100):
            QTest.qWait(10)
            if self.bridge._memory_worker is None: break
        self.assertTrue(self.bridge.gameFrozen)
        self.assertIn('denied',self.bridge.runningState['memory_status'])

    def test_frozen_extra_controls_fit_and_dpad_reaches_last_row(self):
        original = self.bridge.settings['ui_scale']
        self.bridge.toggleGameFreeze()
        try:
            for w,h in ((800,500),(1280,720),(1920,1080)):
                for scale in (.8,1,1.2):
                    self.window.resize(w,h);self.bridge.settings['ui_scale']=scale;self.bridge.settingsChanged.emit();QTest.qWait(30)
                    for name in ('backToLauncherButton','trimGameMemoryButton','minimizeNgsButton','resetGameStatisticsButton'):
                        button = self.visual(self.frozen,name)
                        start = button.mapToItem(self.frozen,QPointF())
                        self.assertGreaterEqual(start.y(),0)
                        self.assertLessEqual(start.y()+button.height(),self.frozen.height()+1)
                        self.assertGreaterEqual(button.height(),48)
            self.frozen.setProperty('selectedAction',0)
            for action in ('down','down','right'): self.bridge.action.emit(action)
            self.assertEqual(self.frozen.property('selectedAction'),5)
        finally:
            self.window.resize(1280,800);self.bridge.settings['ui_scale']=original;self.bridge.settingsChanged.emit()

    def test_controller_freeze_thaw_minimize_return_and_live_statistics(self):
        self.assertTrue(self.panel.property('visible'))
        self.panel.setProperty('selectedAction',1); self.bridge.action.emit('up')
        self.assertEqual(self.panel.property('selectedAction'),1)
        self.panel.setProperty('selectedAction',2); self.bridge.action.emit('down')
        self.assertEqual(self.panel.property('selectedAction'),4)
        self.panel.setProperty('selectedAction',0)
        self.now = 12; self.bridge.check_game()
        self.assertEqual(self.bridge.runningState['session_seconds'],12)
        self.bridge.action.emit('right'); self.bridge.action.emit('activate')
        self.assertTrue(self.bridge.gameFrozen)
        self.assertTrue(self.window.findChild(QObject,'frozenScreen').property('visible'))
        self.now += 500; self.bridge.check_game()
        self.assertEqual((self.bridge.runningState['session_seconds'],self.bridge.runningState['paused_seconds']), (12,500))
        self.bridge.action.emit('right'); self.bridge.action.emit('activate')
        self.assertFalse(self.bridge.gameFrozen)
        self.process.foreground.assert_not_called()
        self.now += 8; self.bridge.check_game()
        self.assertEqual(self.bridge.runningState['session_seconds'],20)
        self.panel.setProperty('selectedAction',2); self.bridge.action.emit('activate')
        self.process.park_windows.assert_called()
        self.panel.setProperty('selectedAction',0); self.bridge.action.emit('activate')
        self.process.foreground.assert_called_once()
        self.assertEqual(self.bridge.runningState['launches'],1)
        self.assertFalse(self.engine.gamepad.active)

    def test_failed_creation_is_not_counted_and_successful_creation_starts_one_session(self):
        self.bridge.play_time.checkpoint(reason='test_finished')
        self.bridge.process = None
        self.bridge._operation_game, self.bridge._operation_name = 'running-fixture','Игра'
        self.bridge.game_worker = SimpleNamespace(mode='launch')
        def count():
            with closing(sqlite3.connect(self.bridge.library.path)) as db:
                return db.execute("SELECT COUNT(*) FROM play_sessions WHERE game_id='running-fixture'").fetchone()[0]
        try:
            before = count()
            self.bridge._launching = self.bridge._launch_presented = True
            with patch('ngs.app.GameProcess',side_effect=OSError('start denied')):
                self.bridge.operation_completed((Path('fixture.exe'),Path.cwd(),[]))
            self.assertEqual(count(),before)
            self.bridge._launching = self.bridge._launch_presented = True
            with patch('ngs.app.GameProcess',return_value=self.process):
                self.bridge.operation_completed((Path('fixture.exe'),Path.cwd(),[]))
            self.assertEqual(count(),before+1)
            self.bridge.thawGame()
            self.assertEqual(count(),before+1)
            self.process.active.return_value = False
            self.bridge.check_game()
        finally:
            self.bridge.game_worker = None

    def test_force_close_requires_separate_confirmation_and_targets_current_job(self):
        self.panel.setProperty('selectedAction',3); self.bridge.action.emit('activate')
        self.assertTrue(self.panel.property('confirming'))
        self.bridge.action.emit('activate')
        self.process.terminate.assert_not_called()
        self.panel.setProperty('selectedAction',3); self.bridge.action.emit('activate')
        self.bridge.action.emit('back'); self.assertFalse(self.panel.property('confirming'))
        self.panel.setProperty('selectedAction',3); self.bridge.action.emit('activate')
        self.bridge.action.emit('right'); self.now = 9; self.bridge.action.emit('activate')
        self.process.terminate.assert_called_once()
        self.assertTrue(self.bridge.runningState['closing'])
        self.bridge.forceCloseGame(); self.process.terminate.assert_called_once()
        self.process.active.return_value = False; self.bridge.check_game()
        self.assertFalse(self.window.property('gameInProgress'))
        with closing(sqlite3.connect(self.bridge.library.path)) as db:
            self.assertEqual(db.execute("SELECT active_ms,end_reason FROM play_sessions WHERE game_id='running-fixture'").fetchone(),(9000,'forced'))

    def test_control_failures_are_visible_and_failed_process_probe_keeps_tracking(self):
        self.process.terminate.side_effect = OSError('denied')
        self.assertFalse(self.bridge.forceCloseGame())
        self.assertIn('denied',self.bridge.runningState['error'])
        self.assertFalse(self.bridge.runningState['closing'])
        self.process.active.side_effect = OSError('probe denied')
        self.bridge.check_game()
        self.assertIs(self.bridge.process,self.process)
        self.process.close.assert_not_called()
        self.process.active.side_effect = None

    def test_buttons_and_stats_fit_handheld_scales_and_mouse_can_confirm(self):
        original = self.bridge.settings['ui_scale']
        try:
            for w,h in ((800,500),(1280,720),(1920,1080)):
                for scale in (.8,1,1.2):
                    self.window.resize(w,h); self.bridge.settings['ui_scale']=scale; self.bridge.settingsChanged.emit(); QTest.qWait(30)
                    for name in ('thawGameButton','freezeGameButton','minimizeGameButton','forceCloseGameButton','minimizeNgsButton','resetGameStatisticsButton'):
                        button = self.visual(self.panel,name)
                        self.assertIsNotNone(button)
                        start = button.mapToItem(self.panel,QPointF())
                        self.assertGreaterEqual(start.y(),0)
                        self.assertLessEqual(start.y()+button.height(),self.panel.height()+1)
                        self.assertGreaterEqual(button.height(),48)
            self.window.resize(1280,800)
            self.bridge.settings['ui_scale']=1; self.bridge.settingsChanged.emit(); self.now=1234; self.bridge.check_game(); QTest.qWait(100)
            self.assertTrue(self.window.grabWindow().save(str(self.repo/'reports/running-game-dashboard.png')))
            button=self.visual(self.panel,'forceCloseGameButton')
            QMetaObject.invokeMethod(button,'clicked')
            self.assertTrue(self.panel.property('confirming'))
            self.process.terminate.assert_not_called()
        finally:
            self.bridge.settings['ui_scale']=original; self.bridge.settingsChanged.emit(); self.window.resize(1280,800)

    def test_unfrozen_running_game_restores_dashboard_on_activation_and_never_sets_topmost(self):
        from PySide6.QtGui import QGuiApplication
        installed = self.bridge._installed
        try:
            self.bridge._installed = [{'card': {'id': 'running-fixture', 'name': 'Игра'}, 'catalog_id': 'steam-1'}]
            self.window.setProperty('gameInProgress', False)
            self.window.setProperty('frozenLibraryVisible', False)
            with patch.object(QGuiApplication, 'platformName', return_value='windows'), \
                    patch('ngs.process.set_topmost') as topmost, \
                    patch('ngs.process.send_window_to_back'):
                self.window.activeChanged.emit()
                self.assertTrue(self.window.property('gameInProgress'))
                self.assertTrue(self.panel.property('visible'))
                topmost.assert_called_with(int(self.window.winId()), False)
                self.bridge.performGameAction('running-fixture')
                self.process.foreground.assert_called_once()
        finally:
            self.bridge._installed = installed

