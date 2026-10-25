import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QObject
from PySide6.QtTest import QTest
from ngs.catalog import CatalogCache
from tests import test_ui


class QueueSpaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): test_ui.UiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): test_ui.UiTests.tearDownClass.__func__(cls)

    def wait(self):
        for _ in range(300):
            QTest.qWait(10); time.sleep(.001)
            if self.engine.bridge.preview_worker is None and not self.engine.bridge.installPreview.get('busy'): return
        self.fail('Preview did not finish')

    def test_queue_reservation_visible_and_blocks_stale_confirmation_then_cancel_restores_space(self):
        bridge = self.engine.bridge
        picker = self.window.findChild(QObject, 'installDestination')
        before = bridge._games, bridge.cache
        card = json.loads((self.repo/'examples/black-mesa.json').read_text(encoding='utf8'))
        art = dict(path='/assets/ngs_launcher/test.svg',size=1,sha256='0'*64)
        card.update(id='reserve-first', revision=1, delivery='on_request', total_size=6*1024**3, cover=art, background=art)
        card.pop('manifest', None)
        second = dict(card, id='reserve-second')
        try:
            bridge._games = [card, second]
            bridge.settings['tactic_url']='http://127.0.0.1:9/tactic'
            bridge.cache = CatalogCache(self.storage, bridge.settings['tactic_url'])
            with patch.object(bridge, 'pumpDownloads'), patch('ngs.app.shutil.disk_usage', return_value=SimpleNamespace(free=10*1024**3)):
                bridge.performGameAction(second['id']); self.wait()
                self.assertTrue(bridge.installPreview['can_install'])
                bridge.download_queue.add(card, str(self.root/'nas'), bridge.cache.origin, space=dict(size=card['total_size'],workspace=0))
                bridge.downloadsChanged.emit(); QTest.qWait(20)
                self.assertIn('4.0', bridge.freeSpace)
                self.assertIn('6.0', self.window.findChild(QObject,'freeSpaceLabel').property('text'))
                self.assertFalse(bridge.queueDownload(second['id'], bridge.installDirectories[0]))
                self.assertIsNone(bridge.download_queue.get(second['id']))
                bridge.action.emit('refresh'); self.wait()
                self.assertEqual(bridge.installPreview['reserved'], 6*1024**3)
                self.assertIn('6,00', self.window.findChild(QObject,'installReservedSpace').property('text'))
                self.assertIn('−', self.window.findChild(QObject,'installRemainingSpace').property('text'))
                self.assertFalse(self.window.findChild(QObject,'confirmDestination').property('enabled'))
                bridge.download_queue.discard(card['id']); bridge.downloadsChanged.emit()
                bridge.action.emit('refresh'); self.wait()
                self.assertEqual(bridge.installPreview['reserved'], 0)
                self.assertTrue(self.window.findChild(QObject,'confirmDestination').property('enabled'))
                bridge.action.emit('activate'); bridge.action.emit('activate')
                self.assertEqual(bridge.download_queue.get(second['id'])['total_bytes'], second['total_size'])
                for width, height in ((960,600),(1280,800),(1920,1080)):
                    self.window.resize(width,height); QTest.qWait(80)
                    self.assertFalse(any('Error' in message or 'binding loop' in message.lower() for message in self.messages))
        finally:
            bridge.cancelInstallPreview(); self.wait(); picker.setProperty('visible',False)
            for identity in (card['id'], second['id']): bridge.download_queue.discard(identity)
            bridge._games, bridge.cache = before
            bridge.downloadsChanged.emit()


if __name__ == '__main__': unittest.main()
