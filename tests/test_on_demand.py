import base64
import hashlib
import json
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ngs import install
from ngs.api import API_PATH, request_signature, response_signature, validate_payload
from ngs.catalog import CatalogCache
from ngs.downloads import DownloadQueue
from ngs.packages import package_key_payload, verify_stream_block, transport_payload
from ngs.portable import PortableStorage
from ngs.update_crypto import RsaKey
from tools.http_packages import build_packages, helper_source
from tools.stream_packages import stream_worker, stream_action, repair_stream_block


class OnDemandTests(unittest.TestCase):
    def test_producer_death_resumes_same_plan_and_exact_files_without_redownloading_acknowledged_block(self):
        original, crashed = self.ns['build_packages'], []
        def interrupted(*args, **kwargs):
            pipeline = kwargs['pipeline']
            def probe(event, value):
                if event == 'capacity' and value['index'] == 2 and not crashed:
                    deadline = time.monotonic() + 5
                    while int((self.folder / 'ack').read_text()) < 0 and time.monotonic() < deadline: time.sleep(.01)
                    self.assertEqual((self.folder / 'ack').read_text(), '0')
                    crashed.append((self.folder / 'plan.json').read_bytes())
                    # Model process death: no exception-state write, but the OS releases its lock.
                    raise SystemExit()
                return pipeline(event, value)
            return original(*args, **dict(kwargs, pipeline=probe))
        self.ns['build_packages'] = interrupted
        api, recovered = self.api, []
        def recover(operation, value):
            result = api(operation, value)
            if result.get('error') == 'WorkerStopped':
                self.thread.join(2); self.assertFalse(self.thread.is_alive())
                self.assertEqual((self.folder / 'ack').read_text(), str(value['ack']))
                self.thread = None; self.done.clear()
                result = api(operation, dict(action='download'))
                result = api(operation, value)
                recovered.append(True)
            return result
        self.api = recover
        with patch('ngs.on_demand.WORKER_POLL_SECONDS', .02): receipt = self.run_queue()
        self.assertTrue(receipt['managed'])
        self.assertEqual(len(recovered), 1)
        self.assertEqual((self.folder / 'plan.json').read_bytes(), crashed[0])
        self.assertEqual(len([p for p in self.requests if p.endswith('.bin')]), 5)
        for path in (self.source / 'Game').rglob('*'):
            if path.is_file(): self.assertEqual(path.read_bytes(), (self.storage.games / 'Game' / path.relative_to(self.source / 'Game')).read_bytes())

    def test_isolated_worker_checks_server_while_prefetch_waits_for_next_package(self):
        release, stalled = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.ns['build_packages']
        def delayed(*args, **kwargs):
            pipeline = kwargs['pipeline']
            def probe(event, value):
                if event == 'capacity' and value['index'] == 1:
                    stalled.set()
                    if not release.wait(12): raise TimeoutError('Client did not poll stalled producer')
                return pipeline(event, value)
            return original(*args, **dict(kwargs, pipeline=probe))
        self.ns['build_packages'] = delayed
        api, polls = self.api, []
        def recovered(operation, value):
            if value['action'] == 'transfer' and value['ack'] == 0:
                polls.append(value['ack'])
                if len(polls) > 1: release.set()
            return api(operation, value)
        self.api = recovered
        receipt = self.queue.run('game', self.cache, isolated=True)
        self.assertTrue(stalled.is_set() and release.is_set())
        self.assertGreaterEqual(len(polls), 2)
        self.assertTrue(receipt['managed'])
        self.assertEqual(self.queue.jobs(), [])
        for path in (self.source / 'Game').rglob('*'):
            if path.is_file(): self.assertEqual(path.read_bytes(), (self.storage.games / 'Game' / path.relative_to(self.source / 'Game')).read_bytes())

    def test_live_worker_is_not_declared_dead_due_to_old_progress_timestamp(self):
        self.api('transfer', dict(action='download'))
        deadline = time.monotonic() + 5
        while not (self.folder / 'block-1.json').exists() and time.monotonic() < deadline: time.sleep(.01)
        path = self.folder / 'stream.json'
        state = json.loads(path.read_bytes()); state['updated'] = 0
        self.ns['atomic_write'](path, json.dumps(state).encode())
        self.assertEqual(self.api('transfer', dict(action='transfer', ack=-1))['state'], 'running')

    def test_missing_worker_is_detected_without_waiting_for_progress_timeout(self):
        state = dict(state='running', updated=time.time(), done=123, total=456)
        (self.folder / 'stream.json').write_text(json.dumps(state))
        result = self.api('transfer', dict(action='transfer', ack=-1))
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['error'], 'WorkerStopped')
        self.assertEqual((self.folder / 'ack').read_text(), '-1')

    def check_ack_without_next_block(self, isolated=False):
        original, seen = self.api, []
        def delayed(operation, value):
            result = original(operation, value)
            if value["action"] == "transfer" and value["ack"] >= 0:
                ack = value["ack"]
                if ack not in seen: result.pop("block", None)
                seen.append(ack)
            return result
        self.api = delayed
        receipt = self.queue.run("game", self.cache, isolated=True) if isolated else self.run_queue()
        self.assertTrue(receipt["managed"])
        last = len(list(self.folder.glob("block-*.json"))) - 1
        self.assertEqual([ack for ack in seen if ack < last], list(range(last)))

    def test_ack_response_without_next_block_uses_prefetch_without_polling_again(self):
        self.check_ack_without_next_block()

    def test_isolated_worker_handles_ack_response_without_ready_block(self):
        self.check_ack_without_next_block(isolated=True)

    def test_low_disk_keeps_two_published_blocks_and_one_temporary(self):
        ready, original = threading.Event(), self.ns["build_packages"]
        def observed(*args, **kwargs):
            pipeline = kwargs["pipeline"]
            def probe(event, value):
                if event == "capacity" and value["index"] == 2: ready.set()
                return pipeline(event, value)
            return original(*args, **dict(kwargs, pipeline=probe))
        self.ns["build_packages"] = observed
        disk = patch("shutil.disk_usage", side_effect=[SimpleNamespace(free=10*1024**3)] + [SimpleNamespace(free=513*1024**2)]*10000)
        disk.start(); self.addCleanup(disk.stop)
        self.api("transfer", dict(action="download"))
        self.assertTrue(ready.wait(5))
        self.assertEqual(int((self.folder / "ack").read_text()), -1)
        self.assertTrue((self.folder / "block-1.json").exists())
        self.assertFalse((self.folder / "block-2.json").exists())
        self.assertEqual(len(list((self.folder / "chunks").glob("*.bin"))), 2)
        self.assertEqual(len(list((self.folder / "chunks").glob(".package-*"))), 1)
        first = json.loads((self.folder / "block-0.json").read_bytes())["chunk"]
        path = self.folder / "chunks" / (first["sha256"] + ".bin")
        write = self.ns["atomic_write"]
        def acknowledged(target, data):
            if target == self.folder / "ack" and data == b"0": self.assertFalse(path.exists())
            return write(target, data)
        self.ns["atomic_write"] = acknowledged
        self.api("transfer", dict(action="transfer", ack=0))
        for unused in range(200):
            if (self.folder / "block-2.json").exists(): break
            time.sleep(.01)
        self.assertTrue((self.folder / "block-2.json").exists())
        self.assertLessEqual(len(list((self.folder / "chunks").glob("*.bin"))), 2)
        foreign = self.folder/'chunks/foreign.bin'; foreign.write_bytes(b'keep')
        self.stop()
        self.assertTrue(self.done.is_set())
        self.assertEqual(list((self.folder / "chunks").glob(".package-*")), [])
        self.assertEqual(list((self.folder/'chunks').glob('*.bin')), [foreign])

    def test_runtime_rejects_insufficient_lookahead_space_before_payload_download(self):
        self.api('transfer', dict(action='download'))
        deadline = time.monotonic()+5
        while not (self.folder/'manifest.json').exists() and time.monotonic()<deadline: time.sleep(.01)
        self.assertTrue((self.folder/'manifest.json').is_file())
        with patch('ngs.on_demand.shutil.disk_usage', return_value=SimpleNamespace(free=512*1024**2)):
            with self.assertRaisesRegex(OSError, 'Недостаточно места'):
                self.run_queue()
        self.assertFalse(any(path.endswith('.bin') for path in self.requests))
        self.assertEqual(self.queue.get('game')['state'], 'error')

    def test_restarted_producer_does_not_cache_already_acknowledged_blocks(self):
        ready, original = threading.Event(), self.ns["build_packages"]
        (self.folder / "ack").write_text("1")
        def observed(*args, **kwargs):
            pipeline = kwargs["pipeline"]
            def probe(event, value):
                if event == "capacity" and value["index"] == 4: ready.set()
                return pipeline(event, value)
            return original(*args, **dict(kwargs, pipeline=probe))
        self.ns["build_packages"] = observed
        self.api("transfer", dict(action="download"))
        self.assertTrue(ready.wait(5))
        for number in (0, 1):
            chunk = json.loads((self.folder / ("block-%d.json" % number)).read_bytes())["chunk"]
            self.assertFalse((self.folder / "chunks" / (chunk["sha256"] + ".bin")).exists())
        self.assertLessEqual(len(list((self.folder / "chunks").glob("*.bin"))), 3)

    def test_next_block_download_starts_while_current_block_is_being_written(self):
        (self.source/'Game/z-extra').write_bytes(bytes(range(251))*4000)
        checked=[]
        def progress(phase,done,total,label):
            if phase!='unpack' or checked: return
            checked.append(True)
            deadline=time.monotonic()+2
            while len([p for p in self.requests if p.endswith('.bin')])<5 and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertGreaterEqual(len([p for p in self.requests if p.endswith('.bin')]),5,
                                    'Network is idle while the current block is unpacked')
            time.sleep(.05)
            self.assertEqual(len([p for p in self.requests if p.endswith('.bin')]),5,
                             'Lookahead exceeded four packets while the first is unpacked')
            self.assertEqual((self.folder/'ack').read_text(),'-1')
            _, stage = self.queue.stage(self.queue.get('game'))
            self.assertLessEqual(len(list((stage/'http').glob('*.part'))),5)
        from ngs.packages import copy_packages
        def observed(*args, **kwargs):
            values = list(args)
            report = values[5]
            def unpacked(*event):
                report(*event)
                progress(*event)
            values[5] = unpacked
            return copy_packages(*values, **kwargs)
        with patch('ngs.on_demand.copy_packages', side_effect=observed): self.run_queue()
        self.assertTrue(checked)

    def test_server_packs_entire_requested_game_without_waiting_for_ack(self):
        self.api('transfer', dict(action='download'))
        deadline = time.monotonic()+5
        while not (self.folder/'manifest.json').exists() and time.monotonic()<deadline: time.sleep(.01)
        self.assertTrue((self.folder/'manifest.json').is_file())
        self.assertEqual((self.folder/'ack').read_text(), '-1')
        self.assertEqual(len(list((self.folder/'chunks').glob('*.bin'))), 5)

    def test_received_stream_rejects_wrong_file_size_without_rereading_data(self):
        original = install.install_game
        def corrupted(*args, **kwargs):
            (kwargs['stage']/'payload/zero').write_bytes(b'foreign data')
            return original(*args, **kwargs)
        with patch('ngs.install.copy_packages', side_effect=AssertionError('Second unpack pass')), \
             patch('ngs.downloads.install_game', side_effect=corrupted):
            with self.assertRaisesRegex(ValueError, 'Размер записанных файлов'):
                self.run_queue()
        self.assertFalse((self.storage.games/'Game').exists())
        self.assertEqual(self.queue.get('game')['state'], 'error')

    def test_bad_lookahead_plan_is_rejected_before_fetching_its_payload(self):
        self.http_mode='bad-lookahead'
        with self.assertRaises(ValueError): self.run_queue()
        self.assertEqual(self.queue.get('game')['state'],'error')
        self.assertEqual(len([p for p in self.requests if p.endswith('.bin')]),1)

    def test_prefetch_network_error_keeps_previous_block_and_resumes_its_partial(self):
        self.http_mode='truncate-next'
        with self.assertRaises(OSError): self.run_queue()
        self.assertEqual((self.folder/'ack').read_text(),'0')
        _,stage=self.queue.stage(self.queue.get('game'))
        part,=list((stage/'http').glob('*.part'))
        offset=part.stat().st_size
        self.assertGreater(offset,0)
        self.http_mode='normal';self.queue.set_state('game','queued')
        receipt=self.run_queue()
        self.assertTrue(receipt['managed'])
        self.assertIn('bytes=%d-'%offset,self.ranges)

    def test_older_server_without_lookahead_keeps_sequential_resume_compatible(self):
        original=self.api
        def legacy(*args):
            result=original(*args);result.pop('next_block_path',None);return result
        with patch.object(self,'api',side_effect=legacy): receipt=self.run_queue()
        self.assertTrue(receipt['managed'])
        self.assertFalse(any('/block-' in path for path in self.requests))

    def test_isolated_prefetch_continues_while_ack_response_is_waiting(self):
        original=self.api
        def gated(operation,value):
            if value['action'].removeprefix('installer_')=='transfer' and value['ack']==0:
                self.assertTrue(self.next_started.wait(2),'Compiled worker has no concurrent packet download')
            return original(operation,value)
        with patch.object(self,'api',side_effect=gated):
            receipt=self.queue.run('game',self.cache,isolated=True)
        self.assertTrue(receipt['managed'])

    def test_installer_uses_signed_http_and_downloads_all_files_without_a_game_receipt(self):
        self.queue.forget_row("game")
        self.card.update(id="setup-game", kind="installer", game_id="game")
        self.job.set_value("published_data", json.dumps(dict(card=self.card)))
        self.queue.add(self.card, self.root / "offline", self.cache.origin)
        with patch("ngs.downloads.require_local_source", side_effect=AssertionError("SMB used")):
            receipt = self.queue.run("setup-game", self.cache, isolated=True)
        folder = self.storage.games / ".ngs-installers/Game"
        self.assertEqual(install.LocalLibrary(self.storage).reconcile(), [])
        self.assertEqual(install.LocalLibrary(self.storage, installers=True).reconcile()[0]["card"], self.card)
        for path in (self.source / "Game").rglob("*"):
            if path.is_file():
                self.assertEqual((folder / path.relative_to(self.source / "Game")).read_bytes(), path.read_bytes())
        self.assertEqual(receipt["file_count"], 302)
        self.assertTrue(self.calls)
        self.assertTrue(all(action.startswith("installer_") for action in self.calls))
        self.assertEqual(self.queue.jobs(), [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = PortableStorage(self.root / "launcher")
        self.source = self.root / "source"; (self.source / "Game/nested").mkdir(parents=True)
        exe = bytearray(128); exe[:2] = b"MZ"; struct.pack_into("<I", exe, 60, 64); exe[64:68] = b"PE\0\0"
        (self.source / "Game/play.exe").write_bytes(exe)
        for index in range(300):
            (self.source / ("Game/nested/%04d" % index)).write_bytes(bytes(range(251)) * 8)
        (self.source / "Game/zero").touch()
        self.assets = self.root / "assets"; self.assets.mkdir()
        self.folder = self.assets / "package_jobs" / ("package-" + "ab" * 16)
        self.folder.mkdir(parents=True); (self.folder / "ack").write_text("-1")
        image = dict(path="/assets/ngs_launcher/test.png", sha256="0" * 64, size=1)
        self.card = dict(id="game", name="Game", subtitle="", description="", genre="", accent="#abcdef", revision=1,
            source_folder="Game", executable="play.exe", metadata_url="", cover=image, background=image, delivery="on_request")
        self.root_key, self.key = RsaKey(), RsaKey()
        self.addCleanup(self.root_key.close); self.addCleanup(self.key.close)
        public = self.key.export()
        self.certificate = dict(public=base64.b64encode(public).decode(),
            signature=base64.b64encode(self.root_key.sign(package_key_payload(public))).decode())
        (self.storage.root / "assets").mkdir()
        (self.storage.root / "assets/update-public-key.bin").write_bytes(self.root_key.export())
        self.ns = {}; exec(helper_source(), self.ns)
        atomic_write = self.ns["atomic_write"]
        def write(path, data):
            # Emulate the NAS's POSIX rename while Windows fixture readers briefly hold a file open.
            for attempt in range(20):
                try: return atomic_write(path, data)
                except PermissionError:
                    if attempt == 19: raise
                    time.sleep(.01)
        self.ns["atomic_write"] = write
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            for attempt in range(20):
                try: return read_text(path, *args, **kwargs)
                except PermissionError:
                    if path != self.folder / "ack" or attempt == 19: raise
                    time.sleep(.01)
        # Windows can deny a new reader during replace; the real POSIX NAS never does.
        reader = patch.object(Path, "read_text", read)
        reader.start(); self.addCleanup(reader.stop)
        original = self.ns["safe_path"]
        self.ns["safe_path"] = lambda root, relative="": original(self.source if str(root).replace("\\", "/") in ("/mnt/ngs-games", "/mnt/ngs-installers") else root, relative)
        def writer(stream):
            try:
                from compression.zstd import ZstdCompressor
                compressor = ZstdCompressor(level=1)
            except ImportError:
                from zstandard import ZstdCompressor
                compressor = ZstdCompressor(level=1).compressobj()
            class Writer:
                def write(self, data): stream.write(compressor.compress(data))
                def finish(self): stream.write(compressor.flush())
                def close(self): pass
            return Writer()
        self.ns.update(zstd_writer=writer, PACKAGE_SIZE=128 * 1024,
            transport_payload=transport_payload,
            server_sign=lambda private, data: self.key.sign(data),
            build_packages=lambda root, assets, card, launch, helpers, **kw:
                build_packages(self.source, assets, card, launch, helpers, chunk_size=128 * 1024, **kw),
            repair_stream_block=repair_stream_block)
        self.done = threading.Event(); self.thread = None; self.archive = False
        self.ns['stream_worker_alive'] = lambda path: self.thread is not None and self.thread.is_alive()
        values = dict(state='running', published_data=json.dumps(dict(card=self.card)))
        self.job = SimpleNamespace(get_value=values.get, set_value=lambda key, value:values.update({key:value}), commit=lambda:None)
        self.calls, self.requests, self.before_done, self.cached = [], [], False, 0
        self.http_mode, self.ranges, self.http_rate = "normal", [], 0
        self.lost_api_reply, self.dropped_payload, self.api_nonces = False, False, []
        self.api_reply_drops, self.lost_ack_reply = 0, False
        self.api_busy_values = []
        self.next_started=threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                self.assert_valid_request(raw)
                operation,value = validate_payload(json.loads(raw))
                owner.api_nonces.append(self.headers['X-NGS-Nonce'])
                if (owner.http_mode == 'busy-api' and value['action'].endswith('transfer')
                        and value.get('ack') == 0 and len(owner.api_busy_values) < 2):
                    owner.api_busy_values.append(value)
                    data = json.dumps(dict(schema=1,error='DeviceBusy')).encode()
                    self.send_response(409)
                    self.send_header('Content-Length',str(len(data)))
                    self.send_header('X-NGS-Signature',response_signature('cd'*32,self.headers['X-NGS-Nonce'],409,data))
                    self.end_headers(); self.wfile.write(data)
                    return
                result = owner.api(operation,value)
                if owner.http_mode == 'recover' and value['action'] == 'download' and not owner.lost_api_reply:
                    owner.lost_api_reply = True
                    owner.api_reply_drops += 1
                    self.close_connection = True
                    return
                if owner.http_mode == 'recover' and value['action'] == 'transfer' and value.get('ack') == 0 and not owner.lost_ack_reply:
                    owner.lost_ack_reply = True
                    self.close_connection = True
                    return
                data = json.dumps(dict(schema=1,result=result)).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(data)))
                self.send_header('X-NGS-Signature',response_signature('cd'*32,self.headers['X-NGS-Nonce'],200,data))
                self.end_headers()
                if owner.http_mode == 'recover' and value['action'] == 'download' and owner.api_reply_drops == 1:
                    owner.api_reply_drops += 1
                    data = data[:len(data)//2]
                    self.close_connection = True
                self.wfile.write(data)
            def assert_valid_request(self, raw):
                owner.assertEqual(self.path,API_PATH)
                owner.assertEqual(self.headers['X-NGS-Signature'],request_signature('cd'*32,'ab'*16,
                    self.headers['X-NGS-Time'],self.headers['X-NGS-Nonce'],raw))
            def do_GET(self):
                owner.requests.append(self.path)
                owner.ranges.append(self.headers.get("Range"))
                path = install.safe_path(owner.assets, self.path.removeprefix("/assets/ngs_launcher/"))
                if not path.is_file(): self.send_error(404); return
                if path.suffix == ".bin" and not owner.done.is_set(): owner.before_done = True
                raw = path.read_bytes()
                if owner.http_mode=='bad-lookahead' and path.name=='block-1.json':
                    raw=json.dumps(dict(json.loads(raw),plan_sha256='ff'*32)).encode()
                offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                self.send_response(206 if offset else 200)
                if offset: self.send_header("Content-Range", "bytes %d-%d/%d" % (offset, len(raw)-1, len(raw)))
                self.send_header("Content-Length", str(len(raw)-offset)); self.end_headers()
                body = raw[offset:]
                next_block=owner.folder/'block-1.json'
                if next_block.exists() and path.name==json.loads(next_block.read_bytes())['chunk']['sha256']+'.bin':
                    owner.next_started.set()
                truncate_next=(owner.http_mode=='truncate-next' and next_block.exists()
                               and path.name==json.loads(next_block.read_bytes())['chunk']['sha256']+'.bin')
                recover_drop = owner.http_mode == 'recover' and not owner.dropped_payload
                if path.suffix == ".bin" and (owner.http_mode == "truncate" or truncate_next or recover_drop):
                    owner.dropped_payload = True
                    self.wfile.write(body[:len(body)//2]); self.wfile.flush()
                    self.close_connection = True
                    return
                if path.suffix == '.bin' and owner.http_rate:
                    started=time.monotonic()
                    for position in range(0,len(body),512*1024):
                        self.wfile.write(body[position:position+512*1024]);self.wfile.flush()
                        time.sleep(max(0,min(position+512*1024,len(body))/owner.http_rate-(time.monotonic()-started)))
                else: self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cache = CatalogCache(self.storage, "http://127.0.0.1:%d/tactic" % self.server.server_port)
        (self.storage.config/'player-account.json').write_text(json.dumps(dict(account=dict(
            server=self.cache.origin,profile='a'*32,token='b'*64,expires=2000000000,
            api=dict(device='ab'*16,secret='cd'*32,expires=2000000000)))))
        self.queue = DownloadQueue(self.storage); self.addCleanup(self.queue.close)
        self.queue.add(self.card, self.root / "offline", self.cache.origin)
        self.addCleanup(self.stop)

    def stop(self):
        (self.folder / "cancel").touch()
        if self.thread: self.thread.join(10)

    def api(self, operation, value):
        self.assertEqual(operation, "transfer"); self.calls.append(value["action"])
        value = dict(value, action=value["action"].removeprefix("installer_"))
        if value["action"] == "download":
            if self.thread is None:
                self.thread = threading.Thread(target=stream_worker, args=(self.folder, self.card, {'_stream_archive':True} if self.archive else {}, self.ns, "private",
                    self.certificate, SimpleNamespace(close=self.done.set)))
                self.thread.start()
            for unused in range(100):
                if (self.folder / "stream.json").exists(): break
                time.sleep(.01)
        if value["action"] == "repair":
            repair_stream_block(self.folder, value["index"], self.card, self.ns)
            return dict(state="repaired")
        # The Linux NAS allows reads during atomic rename; Windows can briefly deny opening that file.
        for attempt in range(20):
            try:
                state = stream_action(self.job, dict(action="stream_status", ack=value.get("ack", -1)), self.folder, self.ns)
                break
            except PermissionError:
                if attempt == 19: raise
                time.sleep(.01)
        self.cached = max(self.cached, len(list((self.folder / "chunks").glob("*.bin"))))
        return state

    def run_queue(self, cancel=None, progress=lambda *args: None):
        with patch("ngs.on_demand.MetadataApi") as api, patch("ngs.downloads.require_local_source", side_effect=AssertionError("SMB used")):
            api.return_value.call.side_effect = self.api
            return self.queue.run("game", self.cache, cancel, progress)

    def test_first_block_arrives_before_end_cache_is_bounded_and_install_is_exact(self):
        job = self.queue.get('game')
        _, stage = self.queue.stage(job)
        events, statistics = [], []
        original = install.install_game
        def observed(*args, **kwargs):
            statistics.append(self.queue.get('game'))
            return original(*args, **kwargs)
        with patch('ngs.install.copy_packages', side_effect=AssertionError('Second unpack pass')), \
             patch('ngs.install.hash_file', side_effect=AssertionError('Game data reread after streaming')), \
             patch('ngs.downloads.install_game', side_effect=observed):
            receipt = self.run_queue(progress=lambda *args: events.append(args))
        self.assertTrue(receipt["managed"])
        self.assertLessEqual(self.cached, 5)
        self.assertFalse(any(event[0] == "resume" for event in events))
        self.assertEqual(list((self.folder / "chunks").glob("*.bin")), [])
        for item in (self.source / "Game").rglob("*"):
            if item.is_file():
                self.assertEqual(item.read_bytes(), (self.storage.games / "Game" / item.relative_to(self.source / "Game")).read_bytes())
        self.assertEqual(self.queue.jobs(), [])
        self.assertFalse(stage.exists())
        self.assertFalse(list(self.storage.state.glob('stream-'+job['token']+'-*.json')))
        settings = self.storage.read_settings(); settings['tactic_url'] = self.cache.origin+'/tactic'
        self.storage.write_settings(settings)
        self.assertTrue(self.queue.cleanup_due())
        self.assertEqual(len([r for r in self.requests if r.endswith(".bin")]), 5)
        self.assertEqual({event[3] for event in events if event[0] == "unpack"},
                         {"Пакет %d из 5" % number for number in range(1, 6)})
        blocks = [json.loads(path.read_bytes())["chunk"] for path in self.folder.glob("block-*.json")]
        self.assertEqual(sum(event[1] for event in events if event[0] == "io"), sum(block["size"] for block in blocks))
        self.assertEqual(sum(event[2] for event in events if event[0] == "io"), sum(block["raw_size"] for block in blocks))
        self.assertEqual(statistics[0]["network_bytes"], sum(block["size"] for block in blocks))
        self.assertEqual(statistics[0]["network_total"], sum(block["size"] for block in blocks))
        self.assertEqual(statistics[0]["completed_bytes"], sum(block["raw_size"] for block in blocks))
        self.assertEqual(statistics[0]["total_bytes"], sum(block["raw_size"] for block in blocks))

    def test_isolated_worker_recovers_lost_api_reply_and_payload_automatically(self):
        self.http_mode = 'recover'
        events = []
        def progress(*args):
            events.append(args)
            if args[0] == 'waiting' and 'повтор' in args[3]: self.assertEqual(self.queue.get('game')['state'], 'running')
        receipt = self.queue.run('game', self.cache, isolated=True, progress=progress)
        self.assertTrue(receipt['managed'])
        self.assertTrue(self.lost_api_reply and self.lost_ack_reply and self.dropped_payload)
        self.assertEqual(self.calls.count('download'), 3)
        self.assertEqual(len(set(self.api_nonces)), len(self.api_nonces))
        self.assertTrue(any(value for value in self.ranges))
        self.assertTrue(any(event[0] == 'waiting' and 'повтор' in event[3] for event in events))
        self.assertEqual(self.queue.jobs(), [])
        for item in (self.source / 'Game').rglob('*'):
            if item.is_file(): self.assertEqual(item.read_bytes(), (self.storage.games / 'Game' / item.relative_to(self.source / 'Game')).read_bytes())

    def test_pause_during_retry_stops_without_another_request(self):
        self.http_mode = 'truncate'
        cancel = threading.Event()
        def progress(phase, *args):
            if phase == 'waiting': cancel.set()
        with self.assertRaises(install.Cancelled): self.run_queue(cancel=cancel, progress=progress)
        self.assertEqual(self.queue.get('game')['state'], 'paused')
        self.assertEqual(len([path for path in self.requests if path.endswith('.bin')]), 1)

    def test_network_failure_in_real_worker_keeps_partial_and_resumes_using_range(self):
        self.http_mode = "truncate"
        start = time.monotonic()
        with self.assertRaises(OSError): self.queue.run("game", self.cache, isolated=True)
        self.assertLess(time.monotonic() - start, 15)
        job = self.queue.get("game")
        self.assertEqual(job["state"], "queued")
        self.assertGreater(job["retry_at"], time.time())
        self.assertEqual(job["card"]["name"], self.card["name"])
        _, stage = self.queue.stage(job)
        part, = (stage / "http").glob("*.part")
        saved = part.stat().st_size
        self.assertGreater(saved, 0)
        self.assertEqual(job["network_bytes"], saved)
        self.assertGreater(job["total_bytes"], 0)
        self.http_mode = "normal"
        self.queue.set_state("game", "queued")
        events = []
        receipt = self.queue.run("game", self.cache, isolated=True, progress=lambda *args: events.append(args))
        self.assertTrue(receipt["managed"])
        self.assertIn("bytes=%d-" % saved, self.ranges)
        self.assertEqual(self.queue.jobs(), [])
        totals = [event for event in events if event[0] == "io_total"]
        blocks = [json.loads(path.read_bytes())["chunk"] for path in self.folder.glob("block-*.json")]
        self.assertEqual(totals[-1][1], sum(block["size"] for block in blocks) - saved)
        self.assertEqual(totals[-1][2], sum(block["raw_size"] for block in blocks))

    def test_isolated_worker_uses_file_cancellation_and_real_signed_http(self):
        receipt = self.queue.run('game', self.cache, isolated=True)
        self.assertTrue(receipt['managed'])
        self.assertEqual(receipt['file_count'],302)
        self.assertEqual(self.queue.jobs(),[])
        self.assertFalse(list(self.storage.state.glob('stream-*.json')))

    def test_pause_resume_and_repair_acknowledged_block(self):
        cancel = threading.Event()
        def progressed(phase, done, total, label):
            if phase == "unpack" and done > 128 * 1024: cancel.set()
        with self.assertRaises(install.Cancelled): self.run_queue(cancel, progressed)
        self.assertEqual(self.queue.get("game")["state"], "paused")
        # Repair needs a block already acknowledged and removed from the NAS cache.
        first = json.loads((self.folder / 'block-0.json').read_bytes())['chunk']
        self.assertGreaterEqual(int((self.folder / 'ack').read_text()), 0)
        self.assertFalse((self.folder / 'chunks' / (first['sha256'] + '.bin')).exists())
        _, stage = self.queue.stage(self.queue.get("game"))
        path = stage / "payload/nested/0000"
        raw = path.read_bytes(); path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        self.queue.set_state("game", "queued")
        self.run_queue()
        self.assertIn("repair", self.calls)
        self.assertEqual((self.storage.games / "Game/nested/0000").read_bytes(), raw)

    def test_block_signature_binds_plan_index_codec_and_bytes(self):
        self.api("transfer", dict(action="download"))
        for unused in range(1000):
            state = self.api("transfer", dict(action="transfer", ack=-1))
            if state.get("block"): break
            time.sleep(.01)
        message = state["block"]
        verify_stream_block(message, state["plan"]["sha256"], 0, self.root_key.export())
        for field, value in (("plan_sha256", "ff" * 32), ("index", 1)):
            with self.assertRaises(ValueError): verify_stream_block(dict(message, **{field: value}), state["plan"]["sha256"], 0, self.root_key.export())
        changed = dict(message, chunk=dict(message["chunk"], raw_sha256="ff" * 32))
        with self.assertRaises(ValueError): verify_stream_block(changed, state["plan"]["sha256"], 0, self.root_key.export())
