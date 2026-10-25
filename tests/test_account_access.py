import json
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from tools.player_accounts import account_api_allowed, grant_account_device


class AccountAccessTests(unittest.TestCase):
    def setUp(self):
        self.rows = {}
        self.worker = 'ngs_catalog_worker'
        owner = self
        class Record:
            def __init__(self, kind, values=None): self.kind, self.values = kind, values or {}
            def get_value(self, key): return self.values.get(key)
            def set_value(self, key, value): self.values[key] = value
            def commit(self, **kwargs): owner.rows[self.kind, self.values['code']] = self
        class Search:
            def __init__(self, kind): self.kind, self.code = kind, None
            def add_filter(self, key, value): self.code = value
            def get_sobject(self): return owner.rows.get((self.kind, self.code))
        def module(name, **values):
            result = ModuleType(name); result.__dict__.update(values); return result
        project = SimpleNamespace(get_code=lambda: 'ngs_launcher', get_project_db_resource=lambda: 'ngs_launcher')
        security = SimpleNamespace(is_admin=lambda: False, get_login=lambda: SimpleNamespace(get_value=lambda k: self.worker))
        self.modules = {
            'pyasm': module('pyasm'),
            'pyasm.biz': module('pyasm.biz', Project=SimpleNamespace(get=lambda: project)),
            'pyasm.common': module('pyasm.common', Environment=SimpleNamespace(get_security=lambda: security)),
            'pyasm.search': module('pyasm.search', Search=Search, SearchType=SimpleNamespace(create=lambda kind: Record(kind)),
                                  DbContainer=SimpleNamespace(get=lambda resource: SimpleNamespace(do_query=lambda sql: None))),
        }
        self.record = Record
        self.grant = dict(device='a'*32, secret='b'*64, login='c'*64, profile='d'*32, username='player',
                          client_device='e'*32, expires=int(time.time())+1000)
        self.save('player_profile', 'player', dict(id='d'*32))
        self.save('player_login', 'c'*64, dict(profile='d'*32, device='e'*32, expires=self.grant['expires'], apis={'e'*32:dict(device='a'*32)}))

    def save(self, table, code, data):
        obj = self.record('ngs_launcher/'+table, dict(code=code, data=json.dumps(data))); obj.commit()

    def allowed(self):
        self.worker = 'ngs_players_worker'
        account = json.loads(self.rows['ngs_launcher/api_device', 'a'*32].get_value('state'))['account']
        try: return account_api_allowed(account)
        finally: self.worker = 'ngs_catalog_worker'

    def test_expiry_profile_disable_and_invalid_session_api_revoke_access(self):
        with patch.dict('sys.modules', self.modules):
            grant_account_device(self.grant)
            self.assertTrue(self.allowed())
            for change in (dict(expires=0), dict(profile='f'*32), dict(apis={'e'*32:dict(device='f'*32)})):
                login = dict(profile='d'*32, device='e'*32, expires=self.grant['expires'], apis={'e'*32:dict(device='a'*32)})
                self.save('player_login', 'c'*64, dict(login, **change))
                self.assertFalse(self.allowed())
            self.save('player_login', 'c'*64, login)
            self.save('player_login', 'c'*64, dict(login, device='f'*32))
            self.assertTrue(self.allowed())
            self.save('player_profile', 'player', dict(id='d'*32, enabled=False))
            self.assertFalse(self.allowed())

    def test_relogin_preserves_device_but_rotates_secret_and_stale_logout_cannot_revoke_it(self):
        with patch.dict('sys.modules', self.modules):
            grant_account_device(self.grant)
            grant_account_device(dict(self.grant, revoke=True))
            obj = self.rows['ngs_launcher/api_device', 'a'*32]
            self.assertFalse(obj.get_value('enabled'))
            new = dict(self.grant, login='f'*64, secret='1'*64)
            grant_account_device(new)
            grant_account_device(dict(self.grant, revoke=True))
            self.assertTrue(obj.get_value('enabled'))
            self.assertEqual(obj.get_value('secret'), '1'*64)
            with self.assertRaises(ValueError): grant_account_device(dict(new, profile='2'*32))
            obj.set_value('enabled', False)
            with self.assertRaises(ValueError): grant_account_device(new)

    def test_self_registered_player_cannot_edit_catalog_but_owner_and_legacy_profiles_can(self):
        with patch.dict('sys.modules', self.modules):
            grant_account_device(self.grant)
            account=json.loads(self.rows['ngs_launcher/api_device','a'*32].get_value('state'))['account']
            self.worker='ngs_players_worker'
            for role,expected in [('player',False),('owner',True),('unknown',False)]:
                self.save('player_profile','player',dict(id='d'*32,role=role))
                self.assertTrue(account_api_allowed(account,'transfer'))
                self.assertEqual(account_api_allowed(account,'catalog'),expected)
            self.save('player_profile','player',dict(id='d'*32))
            self.assertTrue(account_api_allowed(account,'catalog'))

    def test_workers_cannot_cross_their_existing_permissions(self):
        with patch.dict('sys.modules', self.modules):
            with self.assertRaises(PermissionError): account_api_allowed({})
            self.worker = 'ngs_players_worker'
            with self.assertRaises(PermissionError): grant_account_device(self.grant)
