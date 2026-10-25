import hashlib,json,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_delivery_auth as fixture
from server.plugin.admin_access import session_allowed
from server.plugin.request_peer import client_address


class PublicAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fixture.RegistrationTests.setUpClass.__func__(cls)
    def setUp(self):fixture.RegistrationTests.setUp(self)
    save=fixture.RegistrationTests.save
    request=fixture.RegistrationTests.request

    def test_owner_only_web_session_expires_and_revokes_on_password_or_role_change(self):
        self.save('player_config','registration',dict(enabled=True))
        self.assertTrue(self.request()['ok'])
        self.assertFalse(self.request('web-login')['ok'])
        account=json.loads(self.records['ngs_launcher/player_profile','new-player'].get_value('data'))
        account['role']='owner';self.save('player_profile','new-player',account)
        result=self.request('web-login');self.assertTrue(result['ok'],result)
        token_id=hashlib.sha256(result['result']['token'].encode()).hexdigest()
        self.assertLessEqual(result['result']['expires']-time.time(),900)
        with patch.dict('sys.modules',self.modules):
            self.assertTrue(session_allowed(token_id))
            original=account['password'];account['password']='changed'
            self.save('player_profile','new-player',account)
            self.assertFalse(session_allowed(token_id))
            account.update(password=original,role='player');self.save('player_profile','new-player',account)
            self.assertFalse(session_allowed(token_id))
            account.update(role='owner',enabled=False);self.save('player_profile','new-player',account)
            self.assertFalse(session_allowed(token_id))
            account['enabled']=True;self.save('player_profile','new-player',account)
            record=self.records['ngs_launcher/player_login',token_id]
            row=json.loads(record.get_value('data'));row['expires']=0
            self.save('player_login',token_id,row)
            self.assertFalse(session_allowed(token_id))

    def test_forged_forwarded_address_is_ignored(self):
        request=SimpleNamespace(remote=SimpleNamespace(ip='192.0.2.3'),headers={'X-NGS-Client-IP':'198.51.100.4'})
        with patch('pathlib.Path.read_text',return_value='a'*64):
            self.assertEqual(client_address(request),'192.0.2.3')
            request.headers['X-NGS-Proxy-Token']='a'*64
            self.assertEqual(client_address(request),'198.51.100.4')
            request.headers['X-NGS-Client-IP']='bad address'
            self.assertEqual(client_address(request),'192.0.2.3')
