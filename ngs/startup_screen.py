"""A rendered startup cover, released only after the library has painted."""
from ngs.i18n import tr
import time
from urllib.parse import quote
from ngs.appearance import colors
from PySide6.QtCore import QEventLoop, QTimer, Qt, QUrl
from shiboken6 import isValid
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine


class StartupScreen:
    def __init__(self, storage, windowed=False):
        from ngs.i18n import configure
        configure(storage)
        self.started = time.perf_counter()
        self.engine = QQmlApplicationEngine()
        self.main = None
        self.timeout = QTimer(); self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.recover)
        self.done = False
        settings = storage.read_settings()
        palette = colors(settings)
        logo = (storage.resources / 'assets/ngs.svg').read_text(encoding='utf8').replace('#d9ff82', palette['accent'])
        self.engine.setInitialProperties(dict(colors=palette, logoSource=QUrl('data:image/svg+xml,' + quote(logo))))
        self.engine.load(QUrl.fromLocalFile(str(storage.resources / 'qml/StartupWindow.qml')))
        if not self.engine.rootObjects(): raise RuntimeError(tr("Could not open the NGS startup screen."))
        self.window = self.engine.rootObjects()[0]
        QQmlEngine.setObjectOwnership(self.window, QQmlEngine.ObjectOwnership.CppOwnership)
        loop = QEventLoop()
        self.window.frameSwapped.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
        self.window.closing.connect(loop.quit)
        timeout = QTimer(); timeout.setSingleShot(True); timeout.timeout.connect(loop.quit); timeout.start(2000)
        if windowed or settings.get('window_mode') == 'windowed': self.window.show()
        else: self.window.showFullScreen()
        loop.exec()
        self.window.frameSwapped.disconnect(loop.quit)
        self.cancelled = not self.window.isVisible()

    def set_status(self, text):
        if self.done: return
        self.window.setProperty('statusText', text)
        loop = QEventLoop()
        self.window.frameSwapped.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
        self.window.closing.connect(loop.quit)
        timer = QTimer(); timer.setSingleShot(True); timer.timeout.connect(loop.quit); timer.start(150)
        self.window.update()
        loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        timer.stop()
        if isValid(self.window):
            self.window.frameSwapped.disconnect(loop.quit)
            self.window.closing.disconnect(loop.quit)

    def attach(self, window):
        self.main = window
        window.setOpacity(0)
        window.frameSwapped.connect(self.presented, Qt.ConnectionType.QueuedConnection)
        window.closing.connect(self.close)
        self.timeout.start(10000)
        window.update()
        self.set_status(tr("Loading artwork"))

    def recover(self):
        # A missing image/driver signal must not leave an invisible launcher blocking the device.
        if self.main is not None:
            self.main.setOpacity(1)
        self.close()

    def presented(self):
        if self.done or self.main is None or not self.main.property('startupReady'): return
        if self.main.opacity() < 1:
            self.main.setOpacity(1)
            self.main.update()
            return
        from ngs.diagnostics import event
        event('startup.ready', elapsed_ms=round((time.perf_counter() - self.started) * 1000, 1))
        self.close()

    def close(self, *unused):
        if self.done: return
        self.done = True
        self.timeout.stop()
        if self.main is not None:
            self.main.frameSwapped.disconnect(self.presented)
        self.window.close()
        self.engine.deleteLater()
