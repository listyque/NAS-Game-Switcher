"""Exercise the bundled plugin's actual embedded contract without a TACTIC server."""
import ast
import base64
import hashlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def plugin(name):
    path = ROOT / 'server/plugin' / name
    source = path.read_text(encoding='utf8')
    tree = ast.parse(source)
    embedded = next(node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and 'def validate_catalog_request' in node.value)
    namespace = {}
    exec(compile(source, str(path), 'exec'), namespace)
    contract = {}
    exec(embedded, contract)
    return namespace, contract, embedded


class PluginContractTests(unittest.TestCase):
    def setUp(self):
        self.api, self.api_contract, unused = plugin('backend_api.py')
        self.catalog, self.catalog_contract, self.helpers = plugin('backend_catalog.py')
        self.raw = json.dumps(dict(name='Custom title', description='Описание'), ensure_ascii=False).encode()
        self.value = dict(action='custom', preview='a' * 32, kind='metadata', total=len(self.raw), offset=0,
                          sha256=hashlib.sha256(self.raw).hexdigest(), data=base64.b64encode(self.raw).decode())

    def test_both_bundled_contracts_accept_custom_and_installer_custom_and_reject_invalid_blocks(self):
        for contract in (self.api_contract, self.catalog_contract):
            validate = contract['validate_catalog_request']
            for action in ('custom', 'installer_custom'):
                self.assertEqual(validate(dict(self.value, action=action))['action'], action)
            for change in ({'unexpected': True}, {'preview': '../elsewhere'}, {'kind': 'executable'},
                           {'total': 16385}, {'offset': -1}, {'sha256': 'invalid'}, {'data': 'not base64!'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    validate(dict(self.value, **change))

    def backend_fixture(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ngs-plugin-contract-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.device, self.preview = 'b' * 32, 'a' * 32
        self.admin, self.project_code = False, 'ngs_launcher'
        self.commits, self.queries = [], []
        owner = self

        class Record:
            def __init__(self, **values):
                self.values = values

            def get_value(self, key):
                return self.values.get(key)

            def get_code(self):
                return self.values['code']

            def set_value(self, key, value):
                self.values[key] = value

            def commit(self, **kwargs):
                owner.commits.append(kwargs)

        self.service = Record(code='service', description='NGS remote catalogizer v1', data='{}')
        self.record = Record(code=self.preview, description='NGS remote catalogizer v1')

        class Search:
            def __init__(self, kind):
                self.kind, self.code = kind, None

            def add_filter(self, key, value):
                self.code = value

            def get_sobject(self):
                if self.kind == 'ngs_launcher/api_device':
                    return Record(enabled=True, state=json.dumps({'account': {'profile': 'c' * 32}}))
                if self.kind == 'ngs_launcher/catalog_config':
                    return owner.service if self.code == 'service' else owner.record if self.code == owner.preview else None
                raise AssertionError('Unexpected search: ' + self.kind)

        def module(name, **values):
            result = types.ModuleType(name)
            result.__dict__.update(values)
            return result

        project = types.SimpleNamespace(get_code=lambda: self.project_code, get_database_name=lambda: self.project_code,
                                        get_project_db_resource=lambda: self.project_code,
                                        get_value=lambda key: 'NGS handheld game catalog; managed from D:/code/NGS')
        security = types.SimpleNamespace(is_logged_in=lambda: True, is_admin=lambda: self.admin,
                                         get_user_name=lambda: 'ngs_catalog_worker')
        self.modules = {
            'pyasm': module('pyasm'),
            'pyasm.biz': module('pyasm.biz', Project=types.SimpleNamespace(get=lambda: project)),
            'pyasm.common': module('pyasm.common', Environment=types.SimpleNamespace(get_security=lambda: security)),
            'pyasm.search': module('pyasm.search', Search=Search,
                                  SearchType=types.SimpleNamespace(create=lambda kind: Record()),
                                  DbContainer=types.SimpleNamespace(get=lambda resource: types.SimpleNamespace(
                                      do_query=self.queries.append))),
        }
        stream = io.BytesIO()
        Image.new('RGB', (4, 4), '#142c36').save(stream, format='PNG')
        raw = stream.getvalue()
        artwork = dict(base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
        self.state = dict(device=self.device, installers=False, state='review', prepared=dict(current=None, edits={},
                          merged={'name': 'Original'}, details=dict(provider_id='manual', cover=artwork, background=artwork)))
        self.record.set_value('data', json.dumps(self.state))
        self.helpers += '\nGAMES_ROOT = ' + repr(str(self.root)) + '\nINSTALLERS_ROOT = GAMES_ROOT\n'

    def call(self, payload, device=None):
        original_open = open

        def opened(path, *args, **kwargs):
            if path == '/proc/self/mountinfo':
                return io.StringIO('0 0 0 / ' + str(self.root) + ' ro - fixture fixture fixture\n')
            return original_open(path, *args, **kwargs)

        with patch.dict('sys.modules', self.modules), patch('builtins.open', side_effect=opened):
            return self.catalog['remote_action'](payload['action'], payload, device or self.device, self.helpers)

    def test_actual_bundled_backend_saves_manual_card_and_stays_in_review_for_both_catalog_kinds(self):
        self.backend_fixture()
        for installers in (False, True):
            self.state['installers'] = installers
            self.record.set_value('data', json.dumps(self.state))
            payload = dict(self.value, action='installer_custom' if installers else 'custom')
            response = self.call(payload)
            self.assertTrue(response['complete'])
            self.assertEqual(response['stage'], 'review')
            self.assertEqual(response['card']['name'], 'Custom title')
            self.assertEqual(set(response['artwork']), {'cover', 'background'})
            saved = json.loads(self.record.get_value('data'))
            self.assertEqual(saved['prepared']['edits'], json.loads(self.raw))
            self.assertEqual(saved['state'], 'review')
            self.assertEqual(saved['installers'], installers)
            self.assertNotIn('published', saved)
        self.assertEqual(len(self.commits), 2)
        self.assertTrue(all(item == dict(triggers='none', log_transaction=False) for item in self.commits))

    def test_project_worker_preview_ownership_and_catalog_kind_guards_still_reject(self):
        self.backend_fixture()
        initial = self.record.get_value('data')
        for scenario in ('admin', 'project', 'device', 'kind'):
            self.admin = scenario == 'admin'
            self.project_code = 'another_project' if scenario == 'project' else 'ngs_launcher'
            payload = dict(self.value, action='installer_custom') if scenario == 'kind' else self.value
            with self.subTest(scenario=scenario), self.assertRaises(PermissionError):
                self.call(payload, 'd' * 32 if scenario == 'device' else None)
            self.assertEqual(self.record.get_value('data'), initial)
        self.assertEqual(self.commits, [])

