import base64
import io
import json
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.request import Request

from Crypto.PublicKey import RSA
from ngs import player_protocol as protocol
from ngs.asset_auth import AssetAuthorization
from ngs.portable import PortableStorage
from ngs.server_trust import connect_server,server_key_path
from ngs.update_crypto import RsaKey
from tests import test_player_sync_regressions as server_fixture


class RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):server_fixture.PlayerClockServerTests.setUpClass.__func__(cls)
    def setUp(self):server_fixture.PlayerClockServerTests.setUp(self)
    save=server_fixture.PlayerClockServerTests.save

    def request(self,action='register',**values):
        payload=dict(action=action,device=self.device,username='new-player',password='a-long-private-password')
        payload.update(values)
        wire,key,identity=protocol.request(self.public,payload)
        with patch.dict('sys.modules',self.modules):result=self.action(wire.decode(),'192.0.2.4')
        self.assertEqual(result['status'],200)
        response=protocol.open_message(result['wire'].encode(),key,self.kid)[0]
        self.assertEqual(response['request_id'],identity)
        return response

    def test_register_creates_player_with_salted_password_then_login_works(self):
        self.save('player_config','registration',dict(enabled=True))
        registered=self.request();self.assertTrue(registered['ok'],registered)
        record=json.loads(self.records['ngs_launcher/player_profile','new-player'].get_value('data'))
        self.assertEqual(record['role'],'player')
        self.assertNotIn('a-long-private-password',json.dumps(record))
        self.assertEqual(record['password'],protocol.password_hash('a-long-private-password',record['salt']))
        logged_in=self.request('login');self.assertTrue(logged_in['ok'])
        self.assertEqual(logged_in['result']['profile'],registered['result']['profile'])
        self.assertNotEqual(logged_in['result']['token'],registered['result']['token'])

    def test_closed_registration_weak_password_and_injected_role_do_not_create_accounts(self):
        self.assertFalse(self.request()['ok'])
        self.save('player_config','registration',dict(enabled=True))
        self.assertFalse(self.request(password='12345678')['ok'])
        self.assertFalse(self.request(role='owner')['ok'])
        self.assertFalse(self.request(username='admin')['ok'])
        self.assertNotIn(('ngs_launcher/player_profile','new-player'),self.records)

    def test_duplicates_and_registration_rate_limit(self):
        self.save('player_config','registration',dict(enabled=True))
        self.assertTrue(self.request()['ok'])
        self.assertFalse(self.request()['ok'])
        self.assertTrue(self.request(username='another-player')['ok'])
        self.assertFalse(self.request(username='rate-limited-player')['ok'])


class ServerConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.storage=PortableStorage(self.temp.name)
        self.origin='https://nas.example'
        self.player=RSA.generate(2048).public_key().export_key().decode()
        self.opener=Mock()

    def fixture(self):
        with RsaKey() as key:
            public=key.export()
            payload=protocol.compact(dict(schema=1,public=self.player,kid=protocol.key_id(self.player)))
            descriptor=json.dumps(dict(payload=protocol.encode(payload),signature=protocol.encode(key.sign(protocol.DOMAIN+payload)))).encode()
        self.opener.open.side_effect=[io.BytesIO(public),io.BytesIO(descriptor)]
        return public

    def test_https_bootstrap_pins_only_matching_key_and_keeps_other_origins(self):
        public=self.fixture()
        with patch('urllib.request.build_opener',return_value=self.opener):
            pinned=connect_server(self.storage,self.origin)
            self.assertEqual(pinned.read_bytes(),public)
            self.assertEqual(connect_server(self.storage,self.origin),pinned)
            self.assertEqual(self.opener.open.call_count,2)
            other=self.fixture();other_pin=connect_server(self.storage,'https://second.example')
        self.assertNotEqual(pinned,other_pin)
        self.assertEqual(other_pin.read_bytes(),other)
        self.assertEqual(server_key_path(self.storage,self.origin).read_bytes(),public)
        self.assertFalse((self.storage.config/'api-device.json').exists())

    def test_tls_error_corrupt_descriptor_and_http_never_install_a_pin(self):
        with patch('urllib.request.build_opener',return_value=self.opener):
            self.opener.open.side_effect=ssl.SSLCertVerificationError('untrusted certificate')
            with self.assertRaises(ssl.SSLCertVerificationError):connect_server(self.storage,self.origin)
            connect_server(self.storage,'http://nas.local:8890')
            self.assertEqual(self.opener.open.call_count,1)
            public=self.fixture()
            self.opener.open.side_effect=[io.BytesIO(public),io.BytesIO(b'{"payload":"AA","signature":"AA"}')]
            with self.assertRaises(ValueError):connect_server(self.storage,self.origin)
        self.assertFalse(list(self.storage.config.glob('server-trust-*.json')))

    def test_asset_token_only_goes_to_selected_https_nas_and_not_redirects(self):
        path=self.storage.config/'player-account.json'
        path.write_text(json.dumps(dict(account=dict(server=self.origin,token='a'*64,expires=int(time.time())+1000))))
        auth=AssetAuthorization(self.storage,self.origin)
        request=auth.https_request(Request(self.origin+'/assets/ngs_launcher/catalog.json'))
        self.assertEqual(request.get_header('Authorization'),'Bearer '+'a'*64)
        self.assertNotIn('Authorization',request.headers)
        for url in ('https://other.example/assets/ngs_launcher/catalog.json',self.origin+'/tactic/', 'http://nas.example/assets/ngs_launcher/catalog.json'):
            self.assertIsNone(auth.https_request(Request(url)).get_header('Authorization'))
