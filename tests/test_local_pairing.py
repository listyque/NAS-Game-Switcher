import hashlib,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ngs.server_trust import connect_server,server_key_path
from ngs.update_crypto import RsaKey
from ngs import player_protocol


class Response:
    def __init__(self,data):self.data=data
    def read(self,limit):return self.data[:limit]
    def __enter__(self):return self
    def __exit__(self,*unused):pass


class PairingTests(unittest.TestCase):
    def test_local_pin_needs_exact_out_of_band_code_and_valid_descriptor(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary,RsaKey() as authority:
            root=Path(temporary);(root/'assets').mkdir();(root/'config').mkdir()
            (root/'assets/update-public-key.bin').write_bytes(b'unrelated maintainer authority')
            storage=SimpleNamespace(config=root/'config',resources=root,read_settings=lambda:{'tactic_url':'http://127.0.0.1/tactic'})
            public=authority.export();fingerprint=hashlib.sha256(public).hexdigest()
            profile=player_protocol.RSA.generate(2048).public_key().export_key().decode()
            payload=json.dumps(dict(schema=1,public=profile,kid=player_protocol.key_id(profile))).encode()
            wire=json.dumps(dict(payload=player_protocol.encode(payload),signature=player_protocol.encode(authority.sign(player_protocol.DOMAIN+payload)))).encode()
            opener=SimpleNamespace(open=lambda request,timeout:Response(public if request.full_url.endswith('.bin') else wire))
            with patch('urllib.request.build_opener',return_value=opener):
                with self.assertRaises(ValueError):connect_server(storage,'http://127.0.0.1')
                self.assertEqual(list(storage.config.iterdir()),[])
                with self.assertRaises(ValueError):connect_server(storage,'http://127.0.0.1','0'*64)
                self.assertEqual(list(storage.config.iterdir()),[])
                pinned=connect_server(storage,'http://127.0.0.1',fingerprint)
                self.assertEqual(pinned.read_bytes(),public)
                self.assertEqual(server_key_path(storage,'http://127.0.0.1'),pinned)
                before={p.name:p.read_bytes() for p in storage.config.iterdir()}
                with self.assertRaises(ValueError):connect_server(storage,'http://127.0.0.1','0'*64)
                self.assertEqual(before,{p.name:p.read_bytes() for p in storage.config.iterdir()})

    def test_public_http_and_invalid_code_are_rejected(self):
        storage=SimpleNamespace(config=Path('.'),resources=Path('.'))
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',80))]):
            with self.assertRaises(ValueError):connect_server(storage,'http://example.com','0'*64)
        for code in ('','0'*63,'g'*64,'0'*129):
            with self.subTest(code=len(code)),self.assertRaises(ValueError):connect_server(storage,'http://127.0.0.1',code)
