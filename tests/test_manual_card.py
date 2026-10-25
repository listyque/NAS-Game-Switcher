import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtGui import QGuiApplication, QImage, QColor
from ngs.api import MAX_BODY, validate_payload
from ngs.catalogizer_ui import CustomCardRequest, custom_artwork
from tools.remote_catalog import custom_card_chunk


class ManualCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QGuiApplication.instance() or QGuiApplication([])

    def setUp(self):
        self.values = dict(state='review', prepared=dict(details=dict(provider_id='manual'), edits={}, merged={}))

    def blocks(self, raw, kind='metadata'):
        for offset in range(0, len(raw), 960):
            value = dict(action='custom', preview='ab'*16, kind=kind, total=len(raw), offset=offset,
                sha256=hashlib.sha256(raw).hexdigest(), data=base64.b64encode(raw[offset:offset+960]).decode())
            validate_payload(dict(operation='catalog', value=value))
            self.assertLessEqual(len(json.dumps(dict(operation='catalog', value=value)).encode()), MAX_BODY)
            yield value

    def test_chunked_unicode_metadata_is_bounded_idempotent_and_does_not_publish(self):
        edits = dict(name='Тест 😀', description='😀'*3000)
        blocks = list(self.blocks(json.dumps(edits, ensure_ascii=False).encode()))
        for index, block in enumerate(blocks):
            self.assertEqual(custom_card_chunk(self.values, block), index == len(blocks)-1)
        self.assertEqual(self.values['prepared']['edits'], edits)
        self.assertEqual(self.values['state'], 'review')
        self.assertFalse(self.values['custom_uploads'])
        self.assertTrue(custom_card_chunk(self.values, blocks[-1]))

    def test_bad_digest_order_metadata_and_nonmanual_draft_are_rejected(self):
        raw = json.dumps(dict(name='Game', description='Description')).encode()
        block = next(self.blocks(raw))
        for bad in (dict(block, sha256='00'*32), dict(block, offset=1)):
            with self.assertRaises(ValueError): custom_card_chunk(copy.deepcopy(self.values), bad)
        for bad in ({'name':'Game','source_folder':'other'}, {'name':'','description':''}, {'name':'Game','description':'x'*3001}):
            with self.assertRaises(ValueError):
                for part in self.blocks(json.dumps(bad).encode()): custom_card_chunk(copy.deepcopy(self.values), part)
        self.values['prepared']['details']['provider_id'] = 'steam:1'
        with self.assertRaises(ValueError): custom_card_chunk(self.values, block)

    def test_images_are_resized_and_uploads_stay_on_the_same_preview_and_catalog_kind(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder).resolve()/'cover.png'
            image = QImage(1200, 1800, QImage.Format.Format_RGB32); image.fill(QColor('red'))
            self.assertTrue(image.save(str(path)))
            for kind, limit in (('cover',48*1024), ('background',96*1024)):
                raw = custom_artwork(path.as_uri(), kind)
                self.assertLessEqual(len(raw), limit)
                self.assertFalse(QImage.fromData(raw, 'JPEG').isNull())
            with self.assertRaises(ValueError): custom_artwork('https://example.test/image.jpg', 'cover')
            for installers in (False, True):
                api = Mock()
                api.call.return_value = dict(complete=True, stage='review')
                worker = CustomCardRequest(None, '', ('ab'*16, dict(name='Game',description='Text'),
                    dict(cover=path.as_uri(),background=''), installers), None)
                self.assertEqual(worker.fetch(api)['stage'], 'review')
                for call in api.call.call_args_list:
                    operation, value = call.args
                    self.assertEqual(value['preview'], 'ab'*16)
                    self.assertEqual(value['action'], 'installer_custom' if installers else 'custom')
                    self.assertLessEqual(len(json.dumps(dict(operation=operation,value=value)).encode()), MAX_BODY)
