import ctypes
import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from ngs.input_host import Channel, ControllerGate, MAX_MESSAGE, command_arguments, pipe_pid


class FakeSocket(QObject):
    readyRead = Signal()
    disconnected = Signal()

    def __init__(self):
        super().__init__(); self.data, self.output, self.aborted = b'', [], False
    def setReadBufferSize(self, size): pass
    def readAll(self):
        data, self.data = self.data, b''; return data
    def state(self): return QLocalSocket.LocalSocketState.ConnectedState
    def write(self, data): self.output.append(data); return len(data)
    def flush(self): pass
    def abort(self): self.aborted = True


class InputHostTests(unittest.TestCase):
    def test_portable_host_uses_launcher_worker_without_uiaccess_or_elevation(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from ngs.input_host import InputHostClient
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge, window = Mock(), Mock()
            bridge.storage = SimpleNamespace(resources=root/'runtime', root=root)
            with patch('ngs.transfer.worker_command', return_value=['fixture-NGS.exe']), \
                 patch('ngs.input_host.subprocess.Popen') as launch, patch('ngs.input_host.launch_host') as elevated:
                client = InputHostClient(bridge, window, portable=True)
                try:
                    command = launch.call_args.args[0]
                    self.assertEqual(command[:2], ['fixture-NGS.exe', '--input-host'])
                    self.assertEqual(command[-1], '--portable')
                    self.assertIn('--pipe', command)
                    self.assertIn(str(root), command)
                    self.assertEqual(launch.call_args.kwargs['cwd'], str(root/'runtime'))
                    elevated.assert_not_called()
                finally:
                    client.deadline.stop(); client.server.close(); client.deleteLater()

    def test_edge_diagnostics_preserve_only_valid_lifecycle_metadata(self):
        from ngs.input_host import InputHostClient
        from types import SimpleNamespace
        from unittest.mock import patch
        client = SimpleNamespace(portable=True, bridge=SimpleNamespace(process=object()))
        message = dict(kind='edgeInput', source='pointer_down', flags=0x12006, contacts=1, enabled=True)
        with patch('ngs.diagnostics.event') as event:
            InputHostClient.receive(client, message)
            event.assert_called_once_with('input.edge', source='pointer_down', flags=0x12006,
                                          contacts=1, enabled=True, portable=True, game_running=True)
            event.reset_mock()
            for field, bad in (('source', 'key'), ('source', {}), ('flags', True), ('flags', -1),
                               ('contacts', 257), ('contacts', 1.0), ('enabled', 1)):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    InputHostClient.receive(client, dict(message, **{field: bad}))
            event.assert_not_called()

    def test_launcher_dispatches_input_host_arguments_before_normal_ui_startup(self):
        from unittest.mock import patch
        import launcher
        arguments = ['--pipe', 'ngs-input-fixture', '--portable']
        with patch('sys.argv', ['NGS.exe', '--input-host', *arguments]), \
             patch('input_host.main', return_value=23) as run:
            self.assertEqual(launcher.main(), 23)
        run.assert_called_once_with(arguments)

    @unittest.skipUnless(os.name == 'nt', 'Windows installed input component')
    def test_startup_updates_only_existing_outdated_component_and_never_retries_in_same_run(self):
        import tempfile
        from unittest.mock import patch
        from PySide6.QtCore import QEvent
        from PySide6.QtTest import QTest
        from ngs.app import create_window
        from ngs.input_host import InputHostOutdated
        from ngs.portable import PortableStorage
        repo = Path(__file__).resolve().parents[1]
        for installed, current, result in ((False, False, 0), (True, True, 0), (True, False, 0), (True, False, 1)):
            with self.subTest(installed=installed, current=current, result=result), tempfile.TemporaryDirectory(dir=repo/'data/tmp') as temporary:
                root = Path(temporary)
                executable = root/'programs/NGS Input/NGSInput.exe'
                if installed:
                    executable.parent.mkdir(parents=True)
                    executable.write_bytes(b'fixture')
                storage = PortableStorage(root/'client', resources=repo)
                settings = storage.read_settings()
                settings.update(tactic_url='http://127.0.0.1:9/tactic')
                storage.write_settings(settings)
                installer, client = Mock(), Mock()
                installer.poll.return_value = None
                def connect_component(*unused, **options):
                    if not options.get("portable") and not current and connect.call_count == 1: raise InputHostOutdated('NGS Input устарел')
                    return client
                with patch.dict(os.environ, ProgramFiles=str(root/'programs')), \
                     patch.object(QGuiApplication, 'platformName', return_value='windows'), \
                     patch('ngs.process.set_topmost'), \
                     patch('ngs.auto_keyboard.AutoKeyboard'), \
                     patch('ngs.edge_swipe.EdgeWindows', return_value=Mock(windows=[])), \
                     patch('ngs.desktop.DesktopControls.attach'), \
                     patch('ngs.input_host.start_input_installer', return_value=installer) as launch, \
                     patch('ngs.input_host.InputHostClient', side_effect=connect_component) as connect:
                    engine, window = create_window(storage)
                    bridge = engine.bridge
                    try:
                        window.frameSwapped.emit()
                        QTest.qWait(60)
                        if installed and not current:
                            launch.assert_called_once_with(storage)
                            self.assertEqual(connect.call_count, 2)
                            self.assertEqual(connect.call_args_list[0].args, (bridge, window, engine))
                            connect.assert_called_with(bridge, window, engine, portable=True)
                            installer.poll.return_value = result
                            bridge.finishInputHostInstall()
                            if result == 0:
                                self.assertEqual(connect.call_count, 3)
                                connect.assert_called_with(bridge, window, engine)
                                # A later reconnect must not trigger another installer.
                                bridge.inputHostInstalled.emit()
                            else:
                                self.assertEqual(connect.call_count, 2)
                                connect.assert_called_with(bridge, window, engine, portable=True)
                                self.assertIn('not installed', bridge.settingsStatus)
                            launch.assert_called_once()
                        else:
                            launch.assert_not_called()
                            connect.assert_called_once()
                            if installed: connect.assert_called_with(bridge, window, engine)
                            else: connect.assert_called_with(bridge, window, engine, portable=True)
                    finally:
                        bridge._input_installer = None
                        if engine.input_host:
                            engine.input_host.close()
                        bridge.stop()
                        bridge.download_queue.close()
                        engine.gamepad.set_active(False)
                        window.close()
                        engine.deleteLater()
                        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                        self.app.processEvents()

    def test_old_component_cannot_replace_updated_keyboard_even_when_only_qml_changed(self):
        import shutil, tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from ngs.input_host import InputHostClient, InputHostOutdated, input_host_current
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            root = Path(temporary).resolve()
            payload = root / 'runtime/input-installer/payload'
            installed = root / 'program-files/NGS Input'
            (payload / 'qml').mkdir(parents=True)
            (payload / 'NGSInput.exe').write_bytes(b'fixture executable')
            (payload / 'qml/KeyboardWindow.qmlc').write_bytes(b'new keyboard')
            (payload / 'qml/TouchpadWindow.qmlc').write_bytes(b'new touchpad')
            shutil.copytree(payload, installed)
            storage = SimpleNamespace(resources=root / 'runtime')
            executable = installed / 'NGSInput.exe'
            self.assertTrue(input_host_current(storage, executable))
            for relative in ('NGSInput.exe','qml/KeyboardWindow.qmlc','qml/TouchpadWindow.qmlc'):
                original = (installed / relative).read_bytes()
                (installed / relative).write_bytes(b'old version')
                self.assertFalse(input_host_current(storage, executable))
                with patch.dict(os.environ, {'ProgramFiles':str(installed.parent)}), patch('ngs.input_host.launch_host') as launch:
                    with self.assertRaisesRegex(InputHostOutdated,'NGS Input is outdated'):
                        InputHostClient(SimpleNamespace(storage=storage), None)
                    launch.assert_not_called()
                (installed / relative).write_bytes(original)
            (installed / 'qml/TouchpadWindow.qmlc').unlink()
            self.assertFalse(input_host_current(storage, executable))
            self.assertFalse(input_host_current(SimpleNamespace(resources=root / 'missing'),executable))

    def test_component_update_keeps_previous_files_and_rolls_back_failed_swap(self):
        if os.name != 'nt': self.skipTest('Windows installer')
        import subprocess, tempfile
        repo = Path(__file__).resolve().parents[1]
        source = (repo/'tools/install_input_host.ps1').read_text(encoding='utf-8-sig')
        begin = source.index('$inputBackup = $null')
        end = source.index("Write-Output ('Installed ", begin)
        commit = source[begin:end]
        with tempfile.TemporaryDirectory(dir=repo/'tests', prefix='input-swap-') as temporary:
            root = Path(temporary).resolve()
            script = root/'check.ps1'
            script.write_text("$ErrorActionPreference='Stop'\n$testRoot='" + str(root).replace("'","''") + "'\n" + '''
function Assert-InputSignature([string]$Path) { if (!(Test-Path -LiteralPath $Path)) { throw 'Missing fixture executable' } }
''' + "$commit=[scriptblock]::Create(@'\n" + commit + "\n'@)\n" + '''
foreach ($case in @('fresh','update','rollback')) {
    $inputProgramFiles=Join-Path $testRoot $case
    [IO.Directory]::CreateDirectory($inputProgramFiles) | Out-Null
    $inputDestination=Join-Path $inputProgramFiles 'NGS Input'
    $inputStage=Join-Path $inputProgramFiles 'stage'
    $inputUpdating=$case -ne 'fresh'
    if ($inputUpdating) {
        [IO.Directory]::CreateDirectory($inputDestination) | Out-Null
        [IO.File]::WriteAllText((Join-Path $inputDestination 'NGSInput.exe'),'old fixture')
    }
    if ($case -ne 'rollback') {
        [IO.Directory]::CreateDirectory($inputStage) | Out-Null
        [IO.File]::WriteAllText((Join-Path $inputStage 'NGSInput.exe'),'new fixture')
    }
    $failed=$false
    try { & $commit } catch { $failed=$true }
    $actual=[IO.File]::ReadAllText((Join-Path $inputDestination 'NGSInput.exe'))
    if ($case -eq 'rollback') {
        if (!$failed -or $actual -ne 'old fixture') { throw 'Failed update did not restore the previous component' }
    } else {
        if ($failed -or $actual -ne 'new fixture') { throw 'Component swap failed' }
        if ($case -eq 'update') {
            $backup=@(Get-ChildItem -LiteralPath $inputProgramFiles -Directory | Where-Object Name -like 'NGS-Input-previous-*')
            if ($backup.Count -ne 1 -or [IO.File]::ReadAllText((Join-Path $backup[0].FullName 'NGSInput.exe')) -ne 'old fixture') { throw 'Previous component was not preserved' }
        }
    }
}
'fresh/update/rollback verified'
''',encoding='utf-8-sig')
            result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script)],cwd=repo,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('fresh/update/rollback verified',result.stdout)

    def test_installer_stays_in_resources_and_passes_portable_data_without_shell(self):
        import tempfile
        from unittest.mock import patch
        from ngs.input_host import start_input_installer
        from ngs.portable import PortableStorage
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix='Input [test] ') as temporary:
            root = Path(temporary).resolve()
            resources = root / 'runtime'; resources.mkdir()
            storage = PortableStorage(root, resources=resources)
            with patch('ngs.input_host.subprocess.Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'does not include'):
                    start_input_installer(storage)
                launch.assert_not_called()
                script = resources / 'input-installer/Install.ps1'
                script.parent.mkdir(); script.write_text('# fixture')
                with patch.dict(os.environ, {'PSModulePath':'incompatible PowerShell modules'}):
                    self.assertIs(start_input_installer(storage), launch.return_value)
                arguments, = launch.call_args.args
                self.assertEqual(arguments[-4:], ['-File', str(script), '-DataRoot', str(storage.data)])
                self.assertEqual(launch.call_args.kwargs['cwd'], root)
                self.assertTrue(Path(launch.call_args.kwargs['env']['PSModuleAnalysisCachePath']).is_relative_to(storage.cache))
                self.assertNotIn('shell', launch.call_args.kwargs)
                self.assertNotIn('psmodulepath',{key.casefold() for key in launch.call_args.kwargs['env']})

    def test_close_detaches_remote_before_socket_destruction_and_can_repeat(self):
        from types import SimpleNamespace
        from ngs.input_host import InputHostClient
        keyboard = SimpleNamespace(remote=None, shown=True, pad_enabled=True, changed=Mock())
        client = SimpleNamespace(channel=SimpleNamespace(socket=Mock()), keyboard=keyboard, deadline=Mock(), server=Mock(),
                                 bridge=SimpleNamespace(status_overlay=Mock()))
        keyboard.remote = client
        socket = client.channel.socket
        socket.disconnectFromServer.side_effect = lambda: self.assertIsNone(keyboard.remote)
        InputHostClient.close(client)
        InputHostClient.close(client)
        socket.disconnectFromServer.assert_called_once()
        self.assertFalse(keyboard.shown); self.assertFalse(keyboard.pad_enabled)
        self.assertIsNone(client.channel)

    def test_helper_detach_notifies_edge_owner_and_stale_client_cannot_detach_new_helper(self):
        from types import SimpleNamespace
        from ngs.input_host import InputHostClient
        for method in (InputHostClient.close, lambda client: InputHostClient.fallback(client,'Fixture disconnect')):
            keyboard=SimpleNamespace(remote=None,shown=False,pad_enabled=False,changed=Mock())
            overlay=SimpleNamespace(remote=None,hide=Mock(),changed=Mock())
            client=SimpleNamespace(channel=None,keyboard=keyboard,bridge=SimpleNamespace(status_overlay=overlay),deadline=Mock(),server=Mock())
            keyboard.remote=client;overlay.remote=client
            method(client)
            self.assertIsNone(overlay.remote)
            overlay.changed.emit.assert_called_once_with()
            replacement=object();overlay.remote=replacement
            method(client)
            self.assertIs(overlay.remote,replacement)
            overlay.changed.emit.assert_called_once_with()

    @classmethod
    def setUpClass(cls): cls.app = QGuiApplication.instance() or QGuiApplication([])

    def test_host_open_notification_updates_state_without_echo_or_stale_owner(self):
        from types import SimpleNamespace
        from ngs.input_host import InputHostClient
        from ngs.status_overlay import StatusOverlay
        from ngs.virtual_keyboard import VirtualKeyboard
        parent = QObject(); parent.settings = {}
        keyboard = VirtualKeyboard(parent)
        overlay = StatusOverlay(keyboard, parent)
        client = SimpleNamespace(bridge=SimpleNamespace(status_overlay=overlay), status=Mock())
        overlay.remote = client
        try:
            InputHostClient.receive(client, dict(kind='statusSwipe', screen='fixture-screen'))
            self.assertTrue(overlay.shown)
            self.assertEqual(overlay.screenName, 'fixture-screen')
            client.status.assert_not_called()
            InputHostClient.receive(client, dict(kind='statusClosed'))
            self.assertFalse(overlay.shown)
            client.status.assert_not_called()
            overlay.show('fixture-screen')
            client.status.assert_called_once_with(True, 'fixture-screen')
            overlay.finishHide()
            overlay.remote = object()
            InputHostClient.receive(client, dict(kind='statusSwipe', screen='fixture-screen'))
            self.assertFalse(overlay.shown)
        finally:
            overlay.remote = None
            overlay.close(); keyboard.close()

    def test_pipe_commands_cannot_inject_keys_mouse_text_or_run_macros(self):
        for name in ('send','press','text','key_event','mouse','shortcut','runMacro','voiceInput','takeScreenshot','__getattribute__'):
            with self.assertRaises(ValueError): command_arguments(name,[])
        self.assertEqual(command_arguments('show',[]),[])
        self.assertEqual(command_arguments('setTouchpads',[True]),[True])
        for values in ([1],['true'],[],[True,False]):
            with self.assertRaises(ValueError): command_arguments('setTouchpads',values)

    def test_channel_handles_fragmented_json_acknowledges_save_and_bounds_input(self):
        socket, receive = FakeSocket(), Mock(return_value='saved')
        channel = Channel(socket,receive); errors = []; channel.failed.connect(errors.append)
        socket.data = b'{"kind":"save","id":1'; channel.read(); receive.assert_not_called()
        socket.data = b'}\n'; channel.read()
        receive.assert_called_once_with(dict(kind='save',id=1))
        self.assertEqual(json.loads(socket.output[0]),dict(reply=1,result='saved'))
        for payload in (b'[]\n',b'{"reply":3}\n',b'x'*(MAX_MESSAGE+1)):
            socket = FakeSocket(); channel = Channel(socket,receive); channel.failed.connect(errors.append)
            socket.data = payload; channel.read(); self.assertTrue(socket.aborted)
        self.assertEqual(len(errors),3)
        with self.assertRaises(ValueError): channel.send(dict(large='x'*MAX_MESSAGE))

    def test_host_gamepad_keeps_service_chord_and_release_out_of_virtual_input(self):
        keyboard=Mock(); gate=ControllerGate(keyboard); gate.allowed=True; gate.shortcuts=[0x3000]
        self.assertTrue(gate(0x3000)); self.assertTrue(gate(0x1000)); self.assertTrue(gate(0))
        keyboard.gamepad_input.assert_not_called(); self.assertEqual(keyboard.release_keys.call_count,3)
        gate(0x1000); keyboard.gamepad_input.assert_called_once_with(0x1000,allowed=True)
        gate.allowed=False; gate(0); keyboard.gamepad_input.assert_called_with(0,allowed=False)

    @unittest.skipUnless(os.name == 'nt','Windows named pipes')
    def test_native_pipe_reports_both_peers_by_pid(self):
        import uuid
        server, client = QLocalServer(), QLocalSocket()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.assertTrue(server.listen('ngs-input-test-' + uuid.uuid4().hex))
        try:
            client.connectToServer(server.serverName()); self.assertTrue(client.waitForConnected(1000))
            if not server.hasPendingConnections(): server.waitForNewConnection(1000)
            accepted = server.nextPendingConnection(); self.assertIsNotNone(accepted)
            self.assertEqual(pipe_pid(client,False),os.getpid())
            self.assertEqual(pipe_pid(accepted,True),os.getpid())
        finally: client.abort(); server.close()

    def test_install_scripts_parse_without_running_or_modifying_trust(self):
        if os.name != 'nt': self.skipTest('Windows PowerShell')
        import subprocess
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',
            "$errorsFound=@(); Get-ChildItem -Path tools/*_input_host.ps1,tools/launch_input_install.ps1 | ForEach-Object { "
            "$tokens=$null; $parseErrors=$null; [Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$parseErrors) | Out-Null; "
            "if ($parseErrors) { $errorsFound += $parseErrors } }; if ($errorsFound.Count) { $errorsFound; exit 1 }"],cwd=repo,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout + result.stderr)
