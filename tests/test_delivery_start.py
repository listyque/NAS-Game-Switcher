import os,subprocess,tempfile,pathlib,unittest,json
@unittest.skipUnless(os.name=='posix','Docker start scripts require Linux')
class StartScriptTests(unittest.TestCase):
 def test_public_start_enables_only_gateway_and_cannot_execute_env(self):
  with tempfile.TemporaryDirectory() as directory:
   folder=pathlib.Path(directory)
   for name in ('start.sh','compose.sh'):(folder/name).write_bytes(pathlib.Path('/work/server',name).read_bytes())
   (folder/'migration').mkdir();(folder/'migration/manifest.json').write_text('{}')
   (folder/'.env').write_text('NGS_PUBLIC_DOMAIN=nas.example.org\nNGS_BIND_IP=0.0.0.0\nNGS_GAMES_PATH=$(touch SHOULD_NOT_EXIST)\n')
   commands=folder/'bin';commands.mkdir();docker=commands/'docker'
   docker.write_text('#!/bin/sh\nif [ "$1" = compose ] && [ "$2" != version ]; then printf "%s:%s" "$COMPOSE_PROFILES" "$NGS_BIND_IP" > "$TEST_RESULT"; fi\nexit 0\n');docker.chmod(0o755)
   result=folder/'result'
   done=subprocess.run(['sh',str(folder/'start.sh')],env=dict(os.environ,PATH=str(commands)+':'+os.environ['PATH'],TEST_RESULT=str(result)),capture_output=True)
   self.assertEqual(done.returncode,0,done.stderr.decode())
   self.assertEqual(result.read_text(),'public:127.0.0.1')
   self.assertFalse((folder/'SHOULD_NOT_EXIST').exists())
   self.assertEqual(len((folder/'secrets/ngs_proxy_token').read_text().strip()),64)
   self.assertEqual((folder/'secrets/ngs_proxy_token').stat().st_mode&0o777,0o600)
   (folder/'.env').write_text('NGS_PUBLIC_DOMAIN=bad;touch PWN\n')
   result.unlink()
   done=subprocess.run(['sh',str(folder/'start.sh')],env=dict(os.environ,PATH=str(commands)+':'+os.environ['PATH'],TEST_RESULT=str(result)),capture_output=True)
   self.assertNotEqual(done.returncode,0)
   self.assertFalse(result.exists())
if __name__=='__main__':unittest.main()
