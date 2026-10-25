"""Opt-in Docker regression: a legacy HTML 200 is never authorization."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen


@unittest.skipUnless(os.environ.get('NGS_DOCKER_TEST')=='1','Requires local disposable Docker containers')
class GatewayTests(unittest.TestCase):
    def test_unmarked_200_and_forged_success_headers_cannot_authorize(self):
        root=Path(__file__).resolve().parents[1]
        name='ngs-deny-test-'+uuid.uuid4().hex[:10]
        app,gateway=name+'-app',name+'-gateway'
        def docker(*args):return subprocess.check_output(['docker',*args],stderr=subprocess.STDOUT,text=True).strip()
        with tempfile.TemporaryDirectory(dir=root/'data/tmp',prefix='gateway-deny-') as temporary:
            path=Path(temporary)/'Caddyfile'
            path.write_text((root/'server/public/Caddyfile').read_text(encoding='utf8').replace('{$NGS_PUBLIC_DOMAIN} {','http://:80 {'),encoding='utf8',newline='\n')
            try:
                docker('network','create',name)
                code="from http.server import HTTPServer,BaseHTTPRequestHandler\nclass Handler(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200);self.end_headers();self.wfile.write(b'legacy login HTML')\n def log_message(self,*args):pass\nHTTPServer(('0.0.0.0',8081),Handler).serve_forever()"
                docker('run','-d','--rm','--name',app,'--network',name,'--network-alias','app','--entrypoint','python3','ghcr.io/listyque/nas-game-switcher/server:0.1.0-preview.1','-c',code)
                docker('run','-d','--rm','--name',gateway,'--network',name,'-p','127.0.0.1::80','-e','NGS_PROXY_TOKEN='+'a'*64,'-e','NGS_PUBLIC_DOMAIN=nas.example',
                       '--mount','type=bind,source='+str(path)+',target=/etc/caddy/Caddyfile,readonly','caddy:2.11.7-alpine')
                address='http://'+docker('port',gateway,'80/tcp')
                for attempt in range(50):
                    try:
                        with urlopen(address+'/assets/ngs_launcher/server-public-key.bin',timeout=2) as response:self.assertEqual(response.status,200)
                        break
                    except URLError:
                        if attempt==49:raise
                        time.sleep(.1)
                for endpoint,headers in [('/tactic/ngs_launcher/',{'X-NGS-Admin-Access':'NGS-Admin-v1'}),
                        ('/assets/ngs_launcher/catalog.json',{'Authorization':'Bearer '+'b'*64,'X-NGS-Asset-Access':'NGS-Assets-v1'})]:
                    with self.assertRaises(HTTPError) as failure:urlopen(Request(address+endpoint,headers=headers),timeout=5)
                    self.assertEqual(failure.exception.code,401)
                    failure.exception.close()
            finally:
                for container in (gateway,app):subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                subprocess.run(['docker','network','rm',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
