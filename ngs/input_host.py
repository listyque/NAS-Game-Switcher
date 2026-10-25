"""Optional installed UIAccess windows; IPC never accepts keyboard or mouse input."""
from ngs.i18n import tr
import ctypes
import copy
import hashlib
import json
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QObject, Property, QTimer, Signal, QUrl, Qt
from PySide6.QtNetwork import QLocalServer, QLocalSocket

EDGE_INPUT_SOURCES = {'pointer_error', 'touch_error', 'activate', 'pointer_down', 'pointer_up', 'pointer_cancel', 'touch_down', 'touch_up',
                      'touch_palm', 'mouse_down', 'mouse_up', 'capture_lost', 'backing_down', 'backing_up',
                      'backing_cancel', 'recognized', 'trace'}

SETTINGS = ('keyboard_options', 'keyboard_layouts', 'keyboard_pad', 'keyboard_macros', 'production_mode', 'language')
COMMANDS = {'show': (), 'hide': (), 'pickKeys': (), 'clearModifiers': (), 'cancel': (), 'finishEditor': (),
            'setTouchpads': (bool,), 'setPadEnabled': (bool,)}
MAX_MESSAGE = 2 * 1024 * 1024


def validate_edge_trace(data):
    fields = {'role','stage','decision','pointer','flags','x','y','width','height','scale',
              'target_pid','target_self','foreground_pid','capture','state','clock_ms','age_ms'}
    stages = {'pointer_down','pointer_move','pointer_up','pointer_cancel','mouse_touch_down','mouse_touch_move',
              'mouse_touch_up','legacy_touch','snapshot','qt_begin','qt_move','qt_end','qt_cancel','grid_drag','grid_end','grid_frame'}
    decisions = {'native','owned','unattached','cancel','device','disabled_or_shown','no_screen','disabled','no_press','outside'}
    if (not isinstance(data,dict) or set(data)!=fields or data.get('role') not in ('handle','launcher')
            or not isinstance(data.get('stage'),str) or data['stage'] not in stages
            or not isinstance(data.get('decision'),str) or data['decision'] not in decisions
            or any(type(data[k]) is not int or not -1000000 <= data[k] <= 0xffffffff for k in fields-{'role','stage','decision'})
            or not 0<=data['clock_ms']<=0xffffffff or not -1<=data['age_ms']<=60000
            or data['target_self'] not in (0,1) or data['capture'] not in (0,1,2) or not 0<=data['state']<32):
        raise ValueError(tr("Invalid edge gesture trace."))
    return data


def input_host_current(storage, executable):
    import time
    started = time.perf_counter()
    payload = storage.resources / 'input-installer/payload'
    qml = sorted((payload / 'qml').rglob('*.qmlc'))
    if not qml: return False
    scripts = sorted(path for path in (payload / 'qml').rglob('*') if path.suffix in ('.js', '.jsc'))
    try:
        for relative in [Path('NGSInput.exe'), *[path.relative_to(payload) for path in (*qml, *scripts)]]:
            with (payload / relative).open('rb') as bundled, (executable.parent / relative).open('rb') as installed:
                if hashlib.file_digest(bundled, 'sha256').digest() != hashlib.file_digest(installed, 'sha256').digest():
                    return False
        return True
    except FileNotFoundError:
        return False
    finally:
        from ngs.diagnostics import event
        event('perf.input_payload', elapsed_ms=round((time.perf_counter()-started)*1000, 1), expected_files=1+len(qml)+len(scripts))


def start_input_installer(storage):
    from ngs.updates import powershell
    from ngs.install import safe_path
    script = safe_path(storage.resources, 'input-installer/Install.ps1')
    if not script.is_file():
        raise ValueError(tr("This build does not include the NGS Input installer. Update NGS through Settings."))
    environment = {key:value for key,value in os.environ.items() if key.casefold() != 'psmodulepath'}
    environment['PSModuleAnalysisCachePath'] = str(storage.cache / 'powershell/ModuleAnalysisCache')
    (storage.cache / 'powershell').mkdir(exist_ok=True)
    return subprocess.Popen([str(powershell()), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                            '-File', str(script), '-DataRoot', str(storage.data)], cwd=storage.root, env=environment,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def command_arguments(name, values):
    if name not in COMMANDS or not isinstance(values, list) or len(values) != len(COMMANDS[name]):
        raise ValueError(tr("Invalid input host command."))
    if any(type(value) is not kind for value, kind in zip(values, COMMANDS[name])):
        raise ValueError(tr("Invalid input host parameters."))
    return values


def pipe_pid(socket, client):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = kernel.GetNamedPipeClientProcessId if client else kernel.GetNamedPipeServerProcessId
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    function.restype = wintypes.BOOL
    identity = wintypes.ULONG()
    if not function(int(socket.socketDescriptor()), ctypes.byref(identity)):
        raise ctypes.WinError(ctypes.get_last_error())
    return identity.value


class ControllerGate:
    def __init__(self, keyboard):
        self.keyboard, self.shortcuts, self.neutral, self.allowed = keyboard, [], False, False

    def __call__(self, buttons):
        if any(buttons & mask == mask for mask in self.shortcuts): self.neutral = True
        if self.neutral:
            self.keyboard.release_keys(); self.neutral = bool(buttons); return True
        return self.keyboard.gamepad_input(buttons, allowed=self.allowed)


class Channel(QObject):
    """Bounded JSON messages on a user-only local pipe, with synchronous save acknowledgements."""
    failed = Signal(str)

    def __init__(self, socket, receive, parent=None):
        super().__init__(parent)
        self.socket, self.receive = socket, receive
        self.buffer, self.serial, self.replies = bytearray(), 0, {}
        socket.setReadBufferSize(MAX_MESSAGE + 1)
        socket.readyRead.connect(self.read)
        socket.disconnected.connect(self.on_disconnected)

    def on_disconnected(self):
        self.failed.emit(tr("The input host disconnected."))

    def send(self, message):
        data = json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(data) > MAX_MESSAGE: raise ValueError(tr("The input host message is too large."))
        if self.socket.state() != QLocalSocket.LocalSocketState.ConnectedState or self.socket.write(data) != len(data):
            raise OSError(tr("The input host is not connected."))
        self.socket.flush()

    def read(self):
        try:
            self.buffer.extend(bytes(self.socket.readAll()))
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n'); self.buffer = bytearray(rest)
                # The pipe is a byte stream: several individually valid messages may
                # arrive in one read. Apply the bound to a frame, not to the batch.
                if len(line) + 1 > MAX_MESSAGE: raise ValueError(tr("Input host message size limit exceeded."))
                message = json.loads(line)
                if not isinstance(message, dict): raise ValueError(tr("Invalid input host message."))
                if 'reply' in message:
                    if type(message['reply']) is not int or message['reply'] != self.serial:
                        raise ValueError(tr("Unexpected input host response."))
                    self.replies[message['reply']] = message
                else:
                    result = self.receive(message)
                    if 'id' in message: self.send(dict(reply=message['id'], result=result))
            if len(self.buffer) >= MAX_MESSAGE: raise ValueError(tr("Input host message size limit exceeded."))
        except (ValueError, OSError, RecursionError, TypeError, KeyError) as error:
            self.failed.emit(str(error)); self.socket.abort()

    def call(self, message):
        self.serial += 1
        identity = self.serial
        self.send(dict(message, id=identity))
        from PySide6.QtCore import QElapsedTimer
        deadline = QElapsedTimer(); deadline.start()
        while identity not in self.replies and deadline.elapsed() < 3000:
            if not self.socket.waitForReadyRead(max(1, 3000 - deadline.elapsed())): break
            self.read()
        reply = self.replies.pop(identity, None)
        if reply is None: raise OSError(tr("The input host did not confirm saving."))
        return reply.get('result')


def launch_host(executable, arguments):
    class ExecuteInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('mask', wintypes.ULONG), ('window', wintypes.HWND),
                    ('verb', wintypes.LPCWSTR), ('file', wintypes.LPCWSTR), ('parameters', wintypes.LPCWSTR),
                    ('directory', wintypes.LPCWSTR), ('show', ctypes.c_int), ('instance', wintypes.HINSTANCE),
                    ('idlist', ctypes.c_void_p), ('class_name', wintypes.LPCWSTR), ('class_key', wintypes.HKEY),
                    ('hotkey', wintypes.DWORD), ('icon', wintypes.HANDLE), ('process', wintypes.HANDLE)]
    shell, kernel = ctypes.WinDLL('shell32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]; shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]; kernel.GetProcessId.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    info = ExecuteInfo(size=ctypes.sizeof(ExecuteInfo), mask=0x40 | 0x400, verb='open', file=str(executable),
                       parameters=subprocess.list2cmdline(arguments), directory=str(executable.parent), show=0)
    if not shell.ShellExecuteExW(ctypes.byref(info)): raise ctypes.WinError(ctypes.get_last_error())
    try: return kernel.GetProcessId(info.process)
    finally: kernel.CloseHandle(info.process)


class InputHostOutdated(OSError):
    """The installed component differs from the complete bundled payload."""


class InputHostClient(QObject):
    def __init__(self, bridge, window, parent=None, *, portable=False):
        super().__init__(parent)
        if not portable:
            executable = Path(os.environ['ProgramFiles']) / 'NGS Input/NGSInput.exe'
            if not input_host_current(bridge.storage, executable):
                raise InputHostOutdated(tr("NGS Input is outdated. The new built-in keyboard is active. To use it above Start, open Settings → Controllers and input → Install / update NGS Input."))
        self.portable = portable
        self.bridge, self.keyboard, self.window = bridge, bridge.virtual_keyboard, window
        self.channel, self.running, self.syncing = None, False, False
        self.last_context = None
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.accept)
        import uuid
        name = 'ngs-input-' + uuid.uuid4().hex
        if not self.server.listen(name): raise OSError(self.server.errorString())
        args = ['--pipe', name, '--parent', str(os.getpid()), '--data-root', str(bridge.storage.root)]
        if portable:
            from ngs.transfer import worker_command
            self._proc = subprocess.Popen([*worker_command(), '--input-host', *args, '--portable'],
                                          cwd=str(bridge.storage.resources), creationflags=subprocess.CREATE_NO_WINDOW)
            self.pid = self._proc.pid
        else:
            self._proc = None
            # Only the protected standard installation can acquire UIAccess.
            self.pid = launch_host(executable, args)
        self.deadline = QTimer(self); self.deadline.setSingleShot(True); self.deadline.setInterval(10000)
        self.deadline.timeout.connect(self.on_deadline)
        self.deadline.start()
        bridge.settingsChanged.connect(self.sync)
        self.keyboard.changed.connect(self.sync)
        bridge.inputChanged.connect(self.sync)
        bridge.freezeChanged.connect(self.sync)
        bridge.desktop_controls.changed.connect(self.sync)
        self.bridge.status_overlay.edgeDragChanged.connect(self.sync_drag)
        window.activeChanged.connect(self.sync)
        window.visibilityChanged.connect(self.sync)
        window.screenChanged.connect(self.sync)

    def sync_drag(self):
        if self.channel and not self.syncing and self.bridge.status_overlay.remote is self:
            try:
                self.channel.send(dict(kind='statusDrag', screen=self.bridge.status_overlay.screenName,
                                       progress=self.bridge.status_overlay.dragProgress))
            except (ValueError, OSError) as error:
                self.fallback(str(error))

    def on_deadline(self):
        self.fallback(tr("The input host did not start."))

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            try:
                if self.channel or pipe_pid(socket, True) != self.pid:
                    socket.abort(); socket.deleteLater(); continue
            except OSError:
                socket.abort(); socket.deleteLater(); continue
            self.channel = Channel(socket, self.receive, self)
            self.last_context = None
            from ngs import crash_capture
            crash_capture.mark('input_host.connected', pid=self.pid)
            self.channel.failed.connect(self.fallback)
            self.deadline.stop(); self.server.close()
            self.keyboard.release_keys(); self.keyboard.cancel()
            self.keyboard.layout_timer.stop(); self.keyboard.pointer_timer.stop()
            if self.keyboard.native_touchpads: self.keyboard.native_touchpads.clear()
            shown, touchpads, enabled = self.keyboard.shown, self.keyboard.touchpads, self.keyboard.pad_enabled
            self.keyboard.remote = self; self.keyboard.changed.emit()
            self.bridge.status_overlay.remote = self
            self.bridge.status_overlay.changed.emit()
            if not getattr(self, 'portable', False):
                self.bridge._settings_status = tr("NGS Input connected. Using the installed keyboard.")
            self.bridge.settingsChanged.emit()
            self.sync()
            if shown:
                self.command('show')
                if touchpads: self.command('setTouchpads', True)
            if enabled: self.command('setPadEnabled', True)

    def sync(self, *unused):
        from shiboken6 import isValid
        if not self.channel or self.syncing or not isValid(self.window): return
        self.syncing = True
        try:
            context = dict(kind='context', settings={key:self.bridge.settings.get(key) for key in SETTINGS if key in self.bridge.settings},
                                   screen=self.window.screen().name() if self.window.screen() else '',
                                   sleeping=bool(getattr(self.bridge, '_sleeping', False)), colors=self.bridge.themeColors,
                                   external_running=bool(self.keyboard.steps or self.keyboard.timer.isActive()),
                                   pad_allowed=not self.window.isActive() and not self.bridge.gameFrozen and not self.bridge.powerError and not self.bridge.desktop_controls.capture,
                                   service_buttons=[value['buttons'] for value in self.bridge.desktop_controls.values().values() if 'buttons' in value],
                                   device=getattr(self.bridge, '_input_device', 'touch'))
            if context != self.last_context:
                self.channel.send(context)
                # Settings can be edited in place. Retaining their references would
                # hide the next change and leave the helper with stale settings.
                self.last_context = copy.deepcopy(context)
        except (ValueError, OSError) as error: self.fallback(str(error))
        finally: self.syncing = False

    def status(self, visible, screen):
        if self.channel:
            try: self.channel.send(dict(kind='status', visible=visible, screen=screen))
            except (ValueError, OSError) as error: self.fallback(str(error))

    def request_restack(self):
        if self.channel and not self.syncing:
            try: self.channel.send(dict(kind='restack'))
            except (ValueError, OSError) as error: self.fallback(str(error))

    def command(self, name, *values):
        command_arguments(name, list(values))
        if self.channel:
            try: self.channel.send(dict(kind='command', name=name, values=values))
            except (ValueError, OSError) as error: self.fallback(str(error))

    def receive(self, message):
        kind = message.get('kind')
        if kind == 'state':
            names = {'shown':bool, 'touchpads':bool, 'pad_enabled':bool, 'picking':bool, 'language':str, 'message':str, 'running':bool}
            if any(type(message.get(key)) is not typ for key,typ in names.items()): raise ValueError(tr("Invalid keyboard state."))
            if (message['shown'], message['touchpads'], message['picking']) != (self.keyboard.shown, self.keyboard.touchpads, self.keyboard.picking):
                from ngs.diagnostics import event
                event('input_host.keyboard_state', shown=message['shown'], touchpads=message['touchpads'],
                      picking=message['picking'], message=message['message'][:120])
            for key in names:
                if key == 'running': self.running = message[key]
                else: setattr(self.keyboard,key,message[key])
            self.syncing = True
            try: self.keyboard.changed.emit()
            finally: self.syncing = False
        elif kind == 'edgeInput':
            if (not isinstance(message.get('source'), str) or message['source'] not in EDGE_INPUT_SOURCES
                    or type(message.get('flags')) is not int
                    or not 0 <= message['flags'] <= 0x7fffffff or type(message.get('contacts')) is not int
                    or not 0 <= message['contacts'] <= 256 or type(message.get('enabled')) is not bool):
                raise ValueError(tr("Invalid edge gesture diagnostics."))
            from ngs.diagnostics import event
            if message['source'] == 'trace':
                event('input.edge_trace', **validate_edge_trace(message.get('trace')), portable=bool(self.portable), game_running=self.bridge.process is not None)
            else:
                event('input.edge', source=message['source'], flags=message['flags'], contacts=message['contacts'],
                      enabled=message['enabled'], portable=bool(self.portable), game_running=self.bridge.process is not None)
        elif kind == 'statusClosed': self.bridge.status_overlay.hide()
        elif kind == 'statusAction' and message.get('action') in ('downloads', 'settings'):
            self.bridge.status_overlay.triggerLauncherAction(message['action'])
        elif kind == 'statusSwipe':
            if not isinstance(message.get('screen'),str) or len(message['screen']) > 256: raise ValueError(tr("Invalid gesture display."))
            import logging
            logging.info('Top-edge swipe received from NGS Input')
            overlay = self.bridge.status_overlay
            if overlay.remote is self and overlay.edgeSwipeEnabled:
                overlay.show(message['screen'], from_remote=True)
        elif kind == 'statusDrag':
            if not isinstance(message.get('screen'), str) or len(message['screen']) > 256 or type(message.get('progress')) not in (int, float):
                raise ValueError(tr("Invalid gesture progress."))
            self.syncing = True
            try: self.bridge.status_overlay.setEdgeDrag(message['screen'], float(message['progress']))
            finally: self.syncing = False
        elif kind == 'save':
            settings = message.get('settings')
            if not isinstance(settings, dict) or set(settings) - set(SETTINGS): raise ValueError(tr("Invalid keyboard settings."))
            if 'language' in settings and settings['language'] not in ('en', 'ru'): raise ValueError('Invalid interface language')
            if 'production_mode' in settings and type(settings['production_mode']) is not bool: raise ValueError(tr("Invalid logging mode."))
            previous = self.bridge.settings
            self.bridge.settings = dict(previous, **settings)
            self.syncing = True
            try:
                self.bridge.save(); self.keyboard.macrosChanged.emit(); self.keyboard.changed.emit(); return ''
            except OSError as error:
                self.bridge.settings = previous; return str(error)
            finally: self.syncing = False
        elif kind == 'editPad': self.keyboard.editPad()
        elif kind == 'editLayout': self.keyboard.editLayout(message['draft'])
        elif kind == 'picked':
            from ngs.virtual_keyboard import chord
            self.keyboard.chordPicked.emit('+'.join(chord(message['chord'])))
        elif kind == 'typing': self.keyboard.windowsTypingSettings()
        elif kind == 'visibility' and type(message.get('value')) is bool:
            self.keyboard.visibilityRequested.emit(message['value'])
        else: raise ValueError(tr("Unknown input host message."))

    def fallback(self, reason):
        import logging
        logging.warning('Input host unavailable: %s',reason)
        from ngs import crash_capture
        crash_capture.mark('input_host.fallback', reason=str(reason)[:200], connected=self.channel is not None)
        channel, self.channel = self.channel, None
        if channel: channel.socket.abort()
        self.deadline.stop(); self.server.close()
        if self.bridge.status_overlay.remote is self:
            self.bridge.status_overlay.remote = None
            self.bridge.status_overlay.hide()
            self.bridge.status_overlay.changed.emit()
        if self.keyboard.remote is self:
            self.keyboard.remote = None; self.keyboard.shown = False; self.keyboard.pad_enabled = False
            self.keyboard.modifiers = []; self.keyboard.message = reason
            self.keyboard.changed.emit()

    def close(self):
        channel, self.channel = self.channel, None
        from ngs import crash_capture
        crash_capture.mark('input_host.close', connected=channel is not None)
        if self.bridge.status_overlay.remote is self:
            self.bridge.status_overlay.remote = None
            self.bridge.status_overlay.hide()
            self.bridge.status_overlay.changed.emit()
        if self.keyboard.remote is self:
            self.keyboard.remote = None; self.keyboard.shown = False; self.keyboard.pad_enabled = False
            self.keyboard.changed.emit()
        if channel: channel.socket.disconnectFromServer()
        self.deadline.stop(); self.server.close()


class HostAppearance(QObject):
    changed = Signal()
    hintsChanged = Signal()
    action = Signal(str)

    def __init__(self):
        from ngs.appearance import colors
        super().__init__(); self.colors, self.device = colors({}), 'touch'

    @Property('QVariantMap', notify=changed)
    def themeColors(self): return self.colors

    @Property('QVariantMap', notify=hintsChanged)
    def hints(self): return dict(device=self.device)


def run_host(storage, pipe, parent_pid, launcher_settings=None):
    from PySide6.QtGui import QGuiApplication, QFont, QFontDatabase
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickGraphicsConfiguration, QQuickWindow, QSGRendererInterface
    from ngs.virtual_keyboard import VirtualKeyboard
    from ngs.desktop import WindowsWindowEvents
    from ngs.gamepad import GamepadInput
    from ngs.status_overlay import StatusOverlay
    os.environ['QSG_RENDER_LOOP'] = 'threaded'
    settings_dict = launcher_settings if isinstance(launcher_settings, dict) else {}
    disable_overlays = settings_dict.get('disable_performance_overlays', True) is not False
    if disable_overlays:
        for key, val in (('VK_LOADER_LAYERS_DISABLE', '~all~'), ('DISABLE_VULKAN_OBS_CAPTURE', '1'),
                         ('MANGOHUD', '0'), ('NODEVICE_SELECT', '1'), ('DXGI_DEBUG_OSD', '0'),
                         ('RTSSHooksCompatibility', '1')):
            os.environ[key] = val
    is_software = (os.environ.get('QT_QUICK_BACKEND') == 'software'
                   or os.environ.get('QT_QPA_PLATFORM') == 'offscreen')
    if not is_software:
        # Transparent DirectComposition overlay windows require Direct3D 11 on Windows 11.
        os.environ.pop('QT_QUICK_BACKEND', None)
        os.environ['QSG_RHI_BACKEND'] = 'd3d11'
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Direct3D11)
    app = QGuiApplication([]); app.setQuitOnLastWindowClosed(False)
    from ngs.i18n import configure
    configure(storage, settings_dict.get('language', 'en'))
    socket = QLocalSocket()
    socket.connectToServer(pipe)
    if not socket.waitForConnected(5000) or pipe_pid(socket, False) != parent_pid:
        return 1
    class HostBridge(QObject):
        def __init__(self):
            super().__init__(); self.settings = {'keyboard_options': {'enabled': False, 'overlay_enabled': False}}
            self._sleeping, self._closing = False, False
            self.command_active, self.pad_allowed = False, False
        def save(self):
            error = channel.call(dict(kind='save', settings=self.settings))
            if error: raise OSError(str(error))
        def openSystemSettings(self, unused): channel.send(dict(kind='typing'))
    bridge, appearance = HostBridge(), HostAppearance()
    keyboard = VirtualKeyboard(bridge)
    status_overlay = StatusOverlay(keyboard, bridge)
    engine = QQmlApplicationEngine()
    engine.setOfflineStoragePath(str(storage.state / 'qml'))
    engine.rootContext().setContextProperty('uiTheme', appearance)
    engine.setInitialProperties(dict(keyboard=keyboard, appearance=appearance, statusOverlay=status_overlay))
    engine.load(QUrl.fromLocalFile(str(storage.resources / 'qml/InputHost.qml')))
    if not engine.rootObjects(): return 1
    root = engine.rootObjects()[0]
    from ngs.edge_swipe import EdgeWindows
    status_overlay.edge_enabled = True
    edge_windows = EdgeWindows(engine, status_overlay, storage.resources)
    windows = [root.findChild(QObject,name) for name in ('virtualKeyboardWindow','leftTouchpadWindow','rightTouchpadWindow','keyboardSettingsWindow','globalStatusBarWindow')]
    edge_windows.status_window = windows[4]
    from PySide6.QtQml import QQmlEngine
    for w in [root, *windows]:
        if w is not None: QQmlEngine.setObjectOwnership(w, QQmlEngine.ObjectOwnership.CppOwnership)
    engine._ngs_windows = [root, *windows]
    for index, window in enumerate(windows):
        window.setPersistentSceneGraph(False); window.setPersistentGraphics(False)
        if not is_software:
            graphics = QQuickGraphicsConfiguration(); graphics.setAutomaticPipelineCache(False)
            pipeline = storage.cache / 'shaders' / ('input-%d.cache' % index)
            graphics.setPipelineCacheSaveFile(str(pipeline))
            if pipeline.is_file(): graphics.setPipelineCacheLoadFile(str(pipeline))
            window.setGraphicsConfiguration(graphics)
    keyboard.attach_touchpads(windows[1:3])
    fonts = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(storage.resources / 'assets/fonts/Manrope.ttf')))
    if fonts: app.setFont(QFont(fonts[0], 11))
    gamepad = GamepadInput(engine)
    controller = ControllerGate(keyboard)
    gamepad.button_handler = controller
    def route_host_action(action):
        if keyboard.visible: keyboard.action.emit(action)
        elif status_overlay.shown: status_overlay.action.emit(action)
    def route_host_haptic(duration):
        if duration: gamepad.vibrate(duration)
        else: gamepad.stop_vibration()
    gamepad.action.connect(route_host_action)
    gamepad.connectionChanged.connect(keyboard.disconnected)
    keyboard.hapticRequested.connect(route_host_haptic)
    timer = QTimer(engine); timer.setSingleShot(True)
    context_restack_timer = QTimer(engine); context_restack_timer.setSingleShot(True); context_restack_timer.setInterval(45)
    def all_windows():
        ordered = list(windows)
        if not status_overlay.shown:
            ordered.extend(edge_windows.windows)
        else:
            ordered = list(edge_windows.windows) + ordered
        return ordered
    def restack():
        if not bridge._sleeping and events:
            events.restack([int(window.winId()) for window in all_windows() if window.isVisible()])
    timer.timeout.connect(restack)
    def request():
        if not bridge._sleeping and any(w.isVisible() for w in all_windows()) and not timer.isActive(): timer.start(0)
    context_restack_timer.timeout.connect(request)
    def receive(message):
        if message.get('kind') == 'status':
            if type(message.get('visible')) is not bool or not isinstance(message.get('screen'),str) or len(message['screen']) > 256: raise ValueError(tr("Invalid status panel."))
            if message['visible'] and not bridge._sleeping:
                screen = next((s for s in app.screens() if s.name() == message['screen']), app.primaryScreen())
                root.findChild(QObject,'globalStatusBarWindow').setScreen(screen)
                status_overlay.show(screen.name())
            else: status_overlay.hide()
            request()
        elif message.get('kind') == 'statusDrag':
            if not isinstance(message.get('screen'), str) or len(message['screen']) > 256 or type(message.get('progress')) not in (int, float):
                raise ValueError(tr("Invalid gesture progress."))
            if not bridge._sleeping:
                screen = next((s for s in app.screens() if s.name() == message['screen']), app.primaryScreen())
                root.findChild(QObject, 'globalStatusBarWindow').setScreen(screen)
                bridge._syncing_drag = True
                try: status_overlay.setEdgeDrag(screen.name(), float(message['progress']))
                finally: bridge._syncing_drag = False
        elif message.get('kind') == 'command':
            name = message.get('name'); values = command_arguments(name,message.get('values'))
            bridge.command_active = True
            try:
                if not bridge._sleeping or name in ('hide','cancel','clearModifiers','finishEditor'): getattr(keyboard,name)(*values)
            finally: bridge.command_active = False
        elif message.get('kind') == 'context':
            settings = message.get('settings')
            if not isinstance(settings,dict) or set(settings) - set(SETTINGS): raise ValueError(tr("Invalid keyboard settings."))
            if type(message.get('sleeping')) is not bool or type(message.get('pad_allowed')) is not bool or not isinstance(message.get('colors'),dict): raise ValueError(tr("Invalid keyboard context."))
            if type(message.get('external_running')) is not bool: raise ValueError(tr("Invalid input state."))
            keyboard.external_running = message['external_running']
            if 'language' in settings and settings['language'] not in ('en', 'ru'): raise ValueError('Invalid interface language')
            if 'production_mode' in settings and type(settings['production_mode']) is not bool: raise ValueError(tr("Invalid logging mode."))
            from ngs.diagnostics import set_production
            set_production(settings.get('production_mode') is True)
            bridge.settings, bridge._sleeping = settings, message['sleeping']
            bridge.pad_allowed = message['pad_allowed']
            from ngs.gamepad import GAMEPAD_MASK
            shortcuts = message.get('service_buttons')
            if not isinstance(shortcuts,list) or len(shortcuts) > 3 or any(type(mask) is not int or not mask or mask & ~GAMEPAD_MASK for mask in shortcuts): raise ValueError(tr("Invalid system shortcut buttons."))
            controller.shortcuts, controller.allowed = shortcuts, not bridge._sleeping and bridge.pad_allowed
            screen = next((s for s in app.screens() if s.name() == message.get('screen')), app.primaryScreen())
            if keyboard.screen is not screen: keyboard.set_screen(screen)
            for window in windows[:4]: window.setScreen(screen)
            colors = dict(appearance.colors, **{key:value for key,value in message['colors'].items() if isinstance(value,str) and value.startswith('#') and len(value) in (7,9)})
            if colors != appearance.colors:
                appearance.colors = colors
                appearance.changed.emit()
            device = message.get('device') if message.get('device') in ('touch','mouse','gamepad','keyboard') else 'touch'
            if device != appearance.device:
                appearance.device = device
                appearance.hintsChanged.emit()
            status_overlay.sleeping = bridge._sleeping
            status_overlay.changed.emit()
            if bridge._sleeping: keyboard.setPadEnabled(False); keyboard.hide(); status_overlay.hide()
            keyboard.macrosChanged.emit()
            keyboard.changed.emit()
            if not bridge._sleeping:
                request()
                context_restack_timer.start()
        elif message.get('kind') == 'restack':
            if not bridge._sleeping:
                request()
                context_restack_timer.start()
        else: raise ValueError(tr("Unknown input host message."))
    channel = Channel(socket, receive, bridge)
    def on_edge_input(source, flags, contacts, enabled):
        if not bridge.settings.get('production_mode') and not edge_windows.closed and socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            channel.send(dict(kind='edgeInput', source=source, flags=flags, contacts=contacts, enabled=enabled))
    edge_windows.dispatch.observed.connect(on_edge_input, Qt.ConnectionType.QueuedConnection)
    def on_edge_trace(data):
        if not bridge.settings.get('production_mode') and not edge_windows.closed and socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            channel.send(dict(kind='edgeInput',source='trace',flags=0,contacts=0,enabled=status_overlay.enabled,trace=data))
    edge_windows.dispatch.traced.connect(on_edge_trace, Qt.ConnectionType.QueuedConnection)
    def on_channel_failed(unused): app.quit()
    def on_status_closed():
        if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            channel.send(dict(kind='statusClosed'))
    def on_status_action(action):
        if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            channel.send(dict(kind='statusAction', action=action))
    def on_status_swipe(screen):
        target = next((s for s in app.screens() if s.name() == screen), None)
        if target is None or bridge._sleeping or not status_overlay.enabled: return
        windows[4].setScreen(target)
        status_overlay.show(target.name())
        channel.send(dict(kind='statusSwipe', screen=target.name()))
    def on_status_drag():
        if not getattr(bridge, '_syncing_drag', False) and socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            channel.send(dict(kind='statusDrag', screen=status_overlay.screenName, progress=status_overlay.dragProgress))
    channel.failed.connect(on_channel_failed)
    status_overlay.closed.connect(on_status_closed)
    status_overlay.launcherActionRequested.connect(on_status_action)
    status_overlay.requested.connect(on_status_swipe)
    status_overlay.edgeDragChanged.connect(on_status_drag)
    last_state = None
    def publish():
        nonlocal last_state
        if socket.state() != QLocalSocket.LocalSocketState.ConnectedState: return
        state = dict(kind='state', shown=keyboard.shown, touchpads=keyboard.touchpads,
                          pad_enabled=keyboard.pad_enabled, picking=keyboard.picking, language=keyboard.language,
                          message=keyboard.message, running=keyboard.running)
        if state != last_state:
            channel.send(state)
            last_state = state
        controller.allowed = not bridge._sleeping and bridge.pad_allowed and not status_overlay.shown
        gamepad.set_active(not bridge._sleeping and (keyboard.visible or status_overlay.shown), background=not bridge._sleeping and keyboard.padEnabled)
    keyboard.changed.connect(publish)
    status_overlay.changed.connect(publish)
    def on_edit_pad(): channel.send(dict(kind='editPad'))
    def on_edit_layout(draft): channel.send(dict(kind='editLayout', draft=draft))
    def on_chord_picked(chord): channel.send(dict(kind='picked', chord=chord))
    def on_visibility_requested(value):
        if not bridge.command_active and socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            channel.send(dict(kind='visibility', value=value))
    keyboard.editRequested.connect(on_edit_pad)
    keyboard.layoutEditRequested.connect(on_edit_layout)
    keyboard.chordPicked.connect(on_chord_picked)
    keyboard.visibilityRequested.connect(on_visibility_requested)
    periodic = QTimer(engine); periodic.setInterval(800); periodic.timeout.connect(request)
    events = WindowsWindowEvents(request) if QGuiApplication.platformName() == 'windows' else None
    def update():
        active = not bridge._sleeping and any(w.isVisible() for w in windows)
        stackable = not bridge._sleeping and any(w.isVisible() for w in all_windows())
        if events: events.set_enabled(stackable)
        if active:
            if not periodic.isActive(): periodic.start()
        elif periodic.isActive(): periodic.stop()
        request()
    edge_windows.changed = update
    status_overlay.changed.connect(update)
    for window in windows: window.visibleChanged.connect(update)
    update()
    app.aboutToQuit.connect(keyboard.close)
    app.aboutToQuit.connect(status_overlay.close)
    if events: app.aboutToQuit.connect(events.close)
    app.aboutToQuit.connect(gamepad.stop_vibration)
    channel.read()
    return app.exec()
