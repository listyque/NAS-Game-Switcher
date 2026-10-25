"""Regressions for the isolated NGS audit; no native input is injected."""
import ctypes
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QDateTime, QEvent, QMetaObject, QObject, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

from ngs.auto_keyboard import AutoKeyboard
from ngs.edge_swipe import EdgeGesture, EdgeWindows
from ngs.input_host import Channel, HostAppearance, InputHostClient, InputHostOutdated, MAX_MESSAGE, input_host_current
from ngs.status_overlay import StatusOverlay
from ngs.touchpad import NativeTouchpads
from ngs.virtual_keyboard import VirtualKeyboard


class SocketFixture(QObject):
    readyRead = Signal()
    disconnected = Signal()

    def __init__(self):
        super().__init__()
        self.data, self.aborted = b'', False

    def setReadBufferSize(self, size): pass
    def readAll(self):
        value, self.data = self.data, b''
        return value
    def state(self): return QLocalSocket.LocalSocketState.ConnectedState
    def write(self, data): return len(data)
    def flush(self): pass
    def abort(self): self.aborted = True


class AuditInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        cls.repo = Path(__file__).resolve().parents[1]

    def test_coalesced_valid_ipc_frames_are_bounded_individually(self):
        socket, receive = SocketFixture(), Mock()
        channel = Channel(socket, receive)
        first = json.dumps({'kind': 'fixture', 'value': 'x' * (MAX_MESSAGE // 2)}).encode() + b'\n'
        second = json.dumps({'kind': 'fixture', 'value': 'y' * (MAX_MESSAGE // 2)}).encode() + b'\n'
        self.assertGreater(len(first + second), MAX_MESSAGE)
        socket.data = first + second
        channel.read()
        self.assertFalse(socket.aborted)
        self.assertEqual(receive.call_count, 2)
        self.assertFalse(channel.buffer)
        socket.data = b'x' * MAX_MESSAGE
        channel.read()
        self.assertTrue(socket.aborted)

    def test_oversized_complete_ipc_frame_is_rejected_before_dispatch(self):
        socket, receive = SocketFixture(), Mock()
        channel = Channel(socket, receive)
        socket.data = json.dumps({'value': 'x' * MAX_MESSAGE}).encode() + b'\n'
        channel.read()
        self.assertTrue(socket.aborted)
        receive.assert_not_called()

    def test_unchanged_input_context_is_not_resent_and_in_place_edits_are_seen(self):
        bridge = SimpleNamespace(settings={'keyboard_options': {'enabled': True}},
                                 themeColors={'text': '#ffffff'}, gameFrozen=False, powerError=False,
                                 desktop_controls=Mock(capture=False))
        bridge.desktop_controls.values.return_value = {}
        keyboard = SimpleNamespace(steps=[], timer=Mock())
        keyboard.timer.isActive.return_value = False
        window = Mock()
        window.screen.return_value.name.return_value = 'fixture-screen'
        window.isActive.return_value = False
        client = SimpleNamespace(channel=Mock(), syncing=False, last_context=None, bridge=bridge,
                                 keyboard=keyboard, window=window, fallback=Mock())
        for _ in range(100): InputHostClient.sync(client)
        self.assertEqual(client.channel.send.call_count, 1)
        bridge.settings['keyboard_options']['enabled'] = False
        InputHostClient.sync(client)
        self.assertEqual(client.channel.send.call_count, 2)
        self.assertFalse(client.last_context['settings']['keyboard_options']['enabled'])
        self.assertFalse(client.syncing)

    def test_input_component_freshness_is_checked_once_before_launch(self):
        bridge, window = Mock(), Mock()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, ProgramFiles=folder):
            with patch('ngs.input_host.input_host_current', return_value=False) as check, \
                 patch('ngs.input_host.launch_host') as launch:
                with self.assertRaises(InputHostOutdated): InputHostClient(bridge, window)
                check.assert_called_once()
                launch.assert_not_called()
            with patch('ngs.input_host.input_host_current', return_value=True) as check, \
                 patch('ngs.input_host.QLocalServer') as server, \
                 patch('ngs.input_host.launch_host', return_value=17) as launch:
                server.return_value.listen.return_value = True
                client = InputHostClient(bridge, window)
                try:
                    check.assert_called_once()
                    launch.assert_called_once()
                finally: client.close()

    def test_neutral_gamepad_avoids_foreground_work_without_skipping_held_key_release(self):
        parent = QObject()
        parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        keyboard.backend = Mock()
        keyboard.backend.foreground.return_value = 42
        keyboard.pad_enabled = True
        try:
            for _ in range(100): self.assertTrue(keyboard.gamepad_input(0))
            keyboard.backend.foreground.assert_not_called()
            keyboard.backend.key_event.assert_not_called()
            keyboard.pad_down, keyboard.pad_target = ['W'], 42
            keyboard.gamepad_input(0)
            keyboard.backend.key_event.assert_called_once_with('W', False)
            self.assertFalse(keyboard.pad_down)
            keyboard.backend.reset_mock()
            keyboard.pad_down, keyboard.pad_target = ['A'], 42
            keyboard.backend.foreground.return_value = 43
            keyboard.gamepad_input(0)
            keyboard.backend.key_event.assert_called_once_with('A', False)
            self.assertFalse(keyboard.pad_down)
        finally: keyboard.close()

    def test_disconnected_status_command_falls_back_without_escaping_a_qt_slot(self):
        client = SimpleNamespace(channel=Mock(), fallback=Mock())
        client.channel.send.side_effect = OSError('pipe disconnected')
        InputHostClient.status(client, True, 'fixture-screen')
        client.fallback.assert_called_once_with('pipe disconnected')

    def test_input_component_javascript_change_requires_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = root / 'resources/input-installer/payload'
            (payload / 'qml').mkdir(parents=True)
            (payload / 'NGSInput.exe').write_bytes(b'executable fixture')
            (payload / 'qml/InputHost.qmlc').write_bytes(b'bytecode fixture')
            (payload / 'qml/FocusNavigation.js').write_text('function next() { return 1 }')
            installed = root / 'installed'
            shutil.copytree(payload, installed)
            storage = SimpleNamespace(resources=root / 'resources')
            self.assertTrue(input_host_current(storage, installed / 'NGSInput.exe'))
            (installed / 'qml/FocusNavigation.js').write_text('function next() { return 0 }')
            self.assertFalse(input_host_current(storage, installed / 'NGSInput.exe'))
            (installed / 'qml/FocusNavigation.js').unlink()
            self.assertFalse(input_host_current(storage, installed / 'NGSInput.exe'))

    def test_focus_notifications_do_not_queue_uia_when_keyboard_is_hidden_or_manual(self):
        parent = QObject()
        parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        power = SimpleNamespace(sleeping=False, changed=Mock(), resumed=Mock())
        auto = AutoKeyboard(keyboard, power, native=False)
        try:
            with patch.object(auto, 'post') as post:
                for _ in range(100): auto.request()
                post.assert_not_called()
                keyboard.show()
                auto.request()
                post.assert_not_called()
                auto.auto_shown = True
                auto.request()
                post.assert_called_once()
        finally:
            auto.close()
            keyboard.close()

    def test_failed_layout_reads_cannot_leave_fast_verification_timer_running(self):
        for response in ((0, 0), None, OSError('layout unavailable')):
            with self.subTest(response=response):
                parent = QObject()
                parent.settings = {}
                keyboard = VirtualKeyboard(parent)
                keyboard.backend = Mock()
                keyboard.shown = True
                keyboard.backend.next_layout.return_value = (17, 0x409)
                if isinstance(response, OSError): keyboard.backend.current_layout.side_effect = response
                else: keyboard.backend.current_layout.return_value = response
                try:
                    keyboard.switchLayout()
                    self.assertEqual(keyboard.layout_timer.interval(), 60)
                    for _ in range(8): keyboard.refreshLayout()
                    self.assertIsNone(keyboard.layout_pending)
                    self.assertEqual(keyboard.layout_timer.interval(), 500)
                    self.assertIn('не приняло', keyboard.message)
                finally: keyboard.close()

    def test_idle_handle_keeps_real_geometry_and_accepts_lower_one_finger_start(self):
        parent = QObject()
        parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard, parent)
        overlay.edge_enabled = True
        engine = QQmlEngine()
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(self.repo / 'qml/EdgeSwipeWindow.qml')))
        window = component.createWithInitialProperties({'controller': overlay})
        self.assertIsNotNone(window, str(component.errors()))
        try:
            self.assertEqual((window.width(), window.height()), (480, 32))
            QTest.qWait(5300)
            self.assertLess(window.property('reveal'), .01)
            self.assertLess(window.findChild(QObject, 'topEdgeHandlePill').property('opacity'), .01)
            self.assertEqual(window.height(), 32)
            # Test the actual input bound produced by the QML, at three scales.
            for ratio in (1., 1.5, 2.):
                for start in (8, 15, 24, 31):
                    with self.subTest(ratio=ratio, start=start):
                        manager = EdgeWindows.__new__(EdgeWindows)
                        target = Mock()
                        target.devicePixelRatio.return_value = ratio
                        target.screen.return_value.name.return_value = 'fixture-screen'
                        manager.controller, manager.user, manager.dispatch = Mock(edgeEnabled=True), Mock(), Mock()
                        manager.handles = {17: target}
                        manager.gestures = {}
                        target.width.return_value = window.width()
                        target.height.return_value = window.height()
                        manager.user.ScreenToClient.return_value = True
                        manager.contact(17, 1, round(60 * ratio), round(start * ratio), True)
                        manager.contact(17, 1, round(60 * ratio), round((start + 100) * ratio), False)
                        manager.dispatch.recognized.emit.assert_called_once_with('fixture-screen')
            overlay.edgeContact.emit(window.screen().name())
            QTest.qWait(220)
            self.assertGreater(window.property('reveal'), .99)
            self.assertEqual(window.height(), 32)
        finally:
            window.hide()
            window.deleteLater()
            engine.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            overlay.close()
            keyboard.close()

    def test_third_finger_or_toolbar_mix_cancels_touchpad_click_and_scroll(self):
        keyboard, window = Mock(), Mock()
        window.property.side_effect = lambda key: {'toolRects': [{'index': 0, 'x': 0, 'y': 0, 'width': 200, 'height': 40}],
                                                 'padTop': 40, 'padBottom': 300, 'rightSide': False}[key]
        for contacts in (((1, 80, 100), (2, 100, 100), (3, 120, 100)), ((1, 80, 100), (2, 100, 20))):
            pad = NativeTouchpads.__new__(NativeTouchpads)
            pad.keyboard, pad.contacts, pad.gestures = keyboard, {}, {}
            for identity, x, y in contacts: pad.contact(17, 0x246, identity, x, y, window)
            for identity, x, y in reversed(contacts): pad.contact(17, 0x247, identity, x, y, window)
            self.assertFalse(pad.contacts)
            self.assertFalse(pad.gestures)
        keyboard.clickPointer.assert_not_called()
        keyboard.scrollPointer.assert_not_called()
        keyboard.movePointer.assert_not_called()
        keyboard.padTool.emit.assert_not_called()

    def test_hidden_background_keeps_last_art_and_loads_latest_without_hidden_animation(self):
        engine = QQmlEngine()
        window = QQuickWindow()
        window.resize(100, 100)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(self.repo / 'qml/GameBackdrop.qml')))
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'fixture.svg'
            image.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>')
            next_image = Path(folder) / 'next.svg'
            next_image.write_text(image.read_text().replace('red', 'blue'))
            item = component.createWithInitialProperties({'source': QUrl.fromLocalFile(str(image)), 'width': 100, 'height': 100})
            self.assertIsNotNone(item, str(component.errors()))
            item.setParentItem(window.contentItem())
            window.show()
            try:
                QTest.qWait(30)
                self.assertTrue(item.property('displayedSource').toString())
                item.setProperty('source', QUrl())
                self.assertEqual(item.property('displayedSource'), QUrl.fromLocalFile(str(image)))
                pan_motion = next(child for child in item.findChildren(QObject) if child.property('duration') == 700)
                item.setProperty('pan', 20)
                QTest.qWait(30)
                self.assertTrue(pan_motion.property('running'))
                item.setProperty('visible', False)
                QTest.qWait(20)
                self.assertFalse(pan_motion.property('running'))
                self.assertEqual(item.property('pan'), 20)
                item.setProperty('source', QUrl.fromLocalFile(str(next_image)))
                QTest.qWait(50)
                self.assertFalse(item.property('transitioning'))
                self.assertEqual(item.property('displayedSource'), QUrl.fromLocalFile(str(image)))
                item.setProperty('visible', True)
                QTest.qWait(520)
                self.assertEqual(item.property('displayedSource'), QUrl.fromLocalFile(str(next_image)))
                self.assertFalse(item.property('transitioning'))
            finally:
                window.hide()
                item.deleteLater()
                window.deleteLater()
                engine.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_clock_panel_refreshes_on_reopen_and_has_no_hidden_timer(self):
        parent = QObject()
        parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard, parent)
        appearance, engine = HostAppearance(), QQmlEngine()
        engine.rootContext().setContextProperty('uiTheme', appearance)
        component = QQmlComponent(engine)
        component.setData(b'''import QtQuick
import QtQuick.Controls.Basic
Window {
    id: root; required property QtObject bridge
    width: 1000; height: 800; visible: true
    SystemPanel { bridge: root.bridge }
}''', QUrl.fromLocalFile(str(self.repo / 'qml/audit-clock-fixture.qml')))
        window = component.createWithInitialProperties({'bridge': overlay})
        self.assertIsNotNone(window, str(component.errors()))
        panel = window.findChild(QObject, 'systemPanel')
        timer = panel.findChild(QObject, 'systemClockTick')
        try:
            panel.setProperty('kind', 'clock')
            self.assertFalse(timer.property('running'))
            panel.setProperty('clockTime', QDateTime.fromString('2000-01-01T00:00:00', 'yyyy-MM-ddTHH:mm:ss'))
            QMetaObject.invokeMethod(panel, 'open')
            QTest.qWait(30)
            self.assertTrue(timer.property('running'))
            self.assertGreater(timer.property('interval'), 0)
            self.assertLessEqual(timer.property('interval'), 60000)
            text = panel.findChild(QObject, 'systemPanelDescription').property('text')
            self.assertIn(str(QDateTime.currentDateTime().date().year()), text)
            QMetaObject.invokeMethod(panel, 'close')
            QTest.qWait(250)
            self.assertFalse(timer.property('running'))
            panel.setProperty('kind', 'brightness')
            QMetaObject.invokeMethod(panel, 'open')
            QTest.qWait(30)
            self.assertFalse(timer.property('running'))
        finally:
            window.hide()
            window.deleteLater()
            engine.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            overlay.close()
            keyboard.close()

    def test_overlay_clock_updates_at_minute_boundary_only_while_visible(self):
        parent = QObject()
        parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard, parent)
        appearance, engine = HostAppearance(), QQmlEngine()
        engine.rootContext().setContextProperty('uiTheme', appearance)
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(self.repo / 'qml/StatusBarWindow.qml')))
        window = component.createWithInitialProperties({'bridge': overlay})
        self.assertIsNotNone(window, str(component.errors()))
        timer = window.findChild(QObject, 'overlayClockMinuteTimer')
        status_timer = window.findChild(QObject, 'overlaySystemStatusTimer')
        try:
            self.assertFalse(timer.property('running'))
            self.assertEqual(status_timer.property('interval'), 30000)
            window.setProperty('clock', 'stale')
            with patch.object(overlay, 'refreshSystemStatus'):
                overlay.show(window.screen().name())
                QTest.qWait(30)
            self.assertTrue(timer.property('running'))
            self.assertGreater(timer.property('interval'), 0)
            self.assertLessEqual(timer.property('interval'), 60000)
            self.assertEqual(window.property('clock'), QDateTime.currentDateTime().toString('hh:mm'))
            actions = []
            overlay.launcherActionRequested.connect(actions.append)
            with patch.object(overlay, 'refreshSystemStatus'):
                for name in ('downloads', 'settings'):
                    overlay.show(window.screen().name())
                    window.setProperty('selectedIndex', window.property('statusKeys').toVariant().index(name))
                    overlay.action.emit('activate')
            self.assertEqual(actions, ['downloads', 'settings'])
            overlay.sleeping = True
            overlay.changed.emit()
            self.assertFalse(timer.property('running'))
        finally:
            window.hide()
            window.deleteLater()
            engine.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            overlay.close()
            keyboard.close()

    def test_hidden_download_queue_defers_model_work_until_shown(self):
        appearance, engine = HostAppearance(), QQmlEngine()
        engine.rootContext().setContextProperty('uiTheme', appearance)
        component = QQmlComponent(engine)
        component.setData(b'''import QtQuick
import QtQuick.Controls.Basic
Window {
    width: 1000; height: 800; visible: true
    QtObject {
        id: fixture; objectName: "fixtureBridge"
        property var inputHints: ({activate: "A", toggle: "Y"})
        property var downloadJobs: []
        property var downloadStatistics: ({network: 0, disk: 0, history: []})
        property string operationStatus: ""
        property bool downloadActive: false
        property bool operationBusy: false
        property var installedIds: []
        property string operationId: ""
        property real operationProgress: 0
        signal downloadsChanged()
        signal action(string action)
    }
    DownloadQueue { objectName: "queue"; anchors.fill: parent; bridge: fixture; visible: false }
}''', QUrl.fromLocalFile(str(self.repo / 'qml/audit-queue-fixture.qml')))
        window = component.create()
        self.assertIsNotNone(window, str(component.errors()))
        bridge, queue = window.findChild(QObject, 'fixtureBridge'), window.findChild(QObject, 'queue')
        model = queue.findChild(QObject, 'downloadQueueModel')
        job = dict(id='fixture', name='Fixture', state='running', message='', progress=.1,
                   destination='fixture-folder', card={'kind': 'game', 'total_size': 100})
        try:
            bridge.setProperty('downloadJobs', [job])
            QTest.qWait(20)
            self.assertTrue(queue.property('jobsDirty'))
            self.assertEqual(model.property('count'), 0)
            queue.setProperty('visible', True)
            QTest.qWait(20)
            self.assertFalse(queue.property('jobsDirty'))
            self.assertEqual(model.property('count'), 1)
            queue.setProperty('visible', False)
            bridge.setProperty('downloadJobs', [job, dict(job, id='second')])
            QTest.qWait(20)
            self.assertEqual(model.property('count'), 1)
            queue.setProperty('visible', True)
            QTest.qWait(20)
            self.assertEqual(model.property('count'), 2)
        finally:
            window.hide()
            window.deleteLater()
            engine.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
