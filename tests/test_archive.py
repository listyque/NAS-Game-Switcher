import hashlib
import io
import json
import tarfile
import threading
import time
import unittest
from unittest.mock import patch

from ngs import install
from ngs.archive import unpack_archive
from tests import test_on_demand as fixtures


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OnDemandTests('test_first_block_arrives_before_end_cache_is_bounded_and_install_is_exact')
        self.f.setUp();self.addCleanup(self.f.doCleanups);self.f.archive=True

    def check_files(self,receipt):
        self.assertTrue(receipt['managed'])
        for path in (self.f.source/'Game').rglob('*'):
            if path.is_file():self.assertEqual(path.read_bytes(),(self.f.storage.games/'Game'/path.relative_to(self.f.source/'Game')).read_bytes())
        self.assertEqual(self.f.queue.jobs(),[])

    def test_signed_archive_installs_without_any_inventory_or_file_hash_pass(self):
        with patch.dict(self.f.ns,inventory=lambda *a,**kw:self.fail('NAS inventory')), \
             patch('ngs.install.inventory',side_effect=AssertionError('Client inventory')), \
             patch('ngs.install.hash_file',side_effect=AssertionError('Client file rehash')):
            receipt=self.f.run_queue()
        self.check_files(receipt)
        plan=json.loads((self.f.folder/'plan.json').read_bytes())
        final=json.loads((self.f.folder/'manifest.json').read_bytes())
        self.assertLess((self.f.folder/'plan.json').stat().st_size,2048)
        self.assertNotIn('files',plan['document']);self.assertNotIn('files',final)
        self.assertEqual(receipt['file_count'],302)
        self.assertEqual(list((self.f.folder/'chunks').glob('*.bin')),[])

    def test_independent_server_key_follows_queued_origin_after_settings_change(self):
        import base64
        public = self.f.root_key.export()
        (self.f.storage.config/'server-public-key.bin').write_bytes(public)
        (self.f.storage.config/'server-trust.json').write_text(json.dumps(dict(
            schema=1, server=self.f.cache.origin, public=base64.b64encode(public).decode())))
        (self.f.storage.resources/'assets/update-public-key.bin').write_bytes(b'wrong vendor key')
        settings = self.f.storage.read_settings()
        settings['tactic_url'] = 'http://another-server:8890/tactic/'
        with patch.object(self.f.storage, 'read_settings', return_value=settings):
            self.check_files(self.f.run_queue())

    def test_pause_mid_packet_replays_only_uncommitted_packet_and_resumes(self):
        cancel=threading.Event()
        def progress(phase,done,total,label):
            if phase=='unpack' and done>128*1024:cancel.set()
        with self.assertRaises(install.Cancelled):self.f.run_queue(cancel,progress)
        job=self.f.queue.get('game');self.assertEqual(job['state'],'paused')
        _,stage=self.f.queue.stage(job)
        state=json.loads((stage/'archive-state.json').read_bytes())
        self.assertGreaterEqual(state['index'],0)
        self.assertTrue((stage/'archive-extracting.json').exists())
        self.f.queue.set_state('game','queued')
        self.check_files(self.f.run_queue())
        chunks=len(list(self.f.folder.glob('block-*.json')))
        self.assertLessEqual(len([p for p in self.f.requests if p.endswith('.bin')]),chunks+1)

    def test_isolated_archive_worker_uses_signed_http(self):
        self.check_files(self.f.queue.run('game',self.f.cache,isolated=True))

    def test_busy_catalog_request_retries_same_ack_with_fresh_signed_nonce(self):
        self.f.http_mode='busy-api'
        self.check_files(self.f.queue.run('game',self.f.cache,isolated=True))
        self.assertEqual(len(self.f.api_busy_values),2)
        self.assertEqual(self.f.api_busy_values[0],self.f.api_busy_values[1])
        self.assertEqual(len(self.f.api_nonces),len(set(self.f.api_nonces)))

    def test_installer_archive_keeps_the_complete_set_out_of_game_library(self):
        self.f.queue.forget_row('game')
        self.f.card.update(id='setup-game',kind='installer',game_id='game')
        self.f.job.set_value('published_data',json.dumps(dict(card=self.f.card)))
        self.f.queue.add(self.f.card,self.f.root/'offline',self.f.cache.origin)
        receipt=self.f.queue.run('setup-game',self.f.cache,isolated=True)
        self.assertEqual(receipt['file_count'],302)
        self.assertEqual(install.LocalLibrary(self.f.storage).reconcile(),[])
        folder=self.f.storage.games/'.ngs-installers/Game'
        for path in (self.f.source/'Game').rglob('*'):
            if path.is_file():self.assertEqual(path.read_bytes(),(folder/path.relative_to(self.f.source/'Game')).read_bytes())

    def test_large_file_segments_and_unicode_names_are_exact(self):
        path=self.f.source/('Game/длинная папка/'+('очень длинное имя '*9)+'.bin')
        path.parent.mkdir();path.write_bytes(bytes(range(251))*5000)
        self.check_files(self.f.run_queue())

    def test_crash_after_durable_checkpoint_reconciles_old_extraction_marker(self):
        from ngs.catalog import atomic_write
        crashed=[]
        def write(path,data):
            atomic_write(path,data)
            if path.name=='archive-state.json' and not crashed:
                crashed.append(True);raise SystemExit()
        with patch('ngs.catalog.atomic_write',side_effect=write):
            with self.assertRaises(SystemExit):self.f.run_queue()
        self.f.queue.set_state('game','queued')
        self.check_files(self.f.run_queue())

    def test_archive_prefetch_is_bounded_to_current_and_four_next_packets(self):
        from ngs.archive import unpack_archive
        checked=[]
        def unpack(*args,**kwargs):
            if not checked:
                checked.append(True);deadline=time.monotonic()+3
                while len([p for p in self.f.requests if p.endswith('.bin')])<5 and time.monotonic()<deadline:time.sleep(.01)
                time.sleep(.05)
                self.assertEqual(len([p for p in self.f.requests if p.endswith('.bin')]),5)
                self.assertEqual((self.f.folder/'ack').read_text(),'-1')
                _,stage=self.f.queue.stage(self.f.queue.get('game'))
                self.assertEqual(len(list((stage/'http').glob('*.part'))),5)
            return unpack_archive(*args,**kwargs)
        with patch('ngs.archive.unpack_archive',side_effect=unpack):self.check_files(self.f.run_queue())
        self.assertTrue(checked)

    def test_stopped_producer_resumes_same_packets_without_rereading_payload(self):
        original=self.f.ns['build_archives'];crashed=[]
        def builder(*args,**kwargs):
            pipeline=kwargs['pipeline']
            def probe(event,value):
                if event=='capacity' and value['index']==2 and not crashed:
                    deadline=time.monotonic()+5
                    while int((self.f.folder/'ack').read_text())<0 and time.monotonic()<deadline:time.sleep(.01)
                    self.assertGreaterEqual(int((self.f.folder/'ack').read_text()),0)
                    crashed.append((self.f.folder/'plan.json').read_bytes());raise SystemExit()
                return pipeline(event,value)
            return original(*args,**dict(kwargs,pipeline=probe))
        self.f.ns['build_archives']=builder
        api=self.f.api;recovered=[]
        def recovery(operation,value):
            result=api(operation,value)
            if result.get('error')=='WorkerStopped':
                self.f.thread.join(2);self.assertFalse(self.f.thread.is_alive())
                self.f.thread=None;self.f.done.clear();api(operation,dict(action='download'))
                result=api(operation,value);recovered.append(True)
            return result
        self.f.api=recovery
        with patch('ngs.on_demand.WORKER_POLL_SECONDS',.02):self.check_files(self.f.run_queue())
        self.assertEqual(len(recovered),1)
        self.assertEqual((self.f.folder/'plan.json').read_bytes(),crashed[0])
        first=json.loads((self.f.folder/'block-0.json').read_bytes())['chunk']['path']
        self.assertEqual(self.f.requests.count(first),1)

    def test_bad_prefetched_signature_never_fetches_its_payload(self):
        self.f.http_mode='bad-lookahead'
        with self.assertRaises(ValueError):self.f.run_queue()
        self.assertFalse((self.f.storage.games/'Game').exists())
        self.assertEqual(len([p for p in self.f.requests if p.endswith('.bin')]),1)

    def test_archive_negotiation_is_optional_and_restricted_to_download(self):
        from ngs.remote_catalog import validate_catalog_request
        request=dict(action='download',game='game',revision=1,preview='ab'*16)
        self.assertEqual(validate_catalog_request(request),request)
        self.assertEqual(validate_catalog_request(dict(request,archive='tar-zstd'))['archive'],'tar-zstd')
        for value in ('tar','zip',True,[],None):
            with self.assertRaises(ValueError):validate_catalog_request(dict(request,archive=value))
        with self.assertRaises(ValueError):validate_catalog_request(dict(action='transfer',preview='ab'*16,ack=-1,archive='tar-zstd'))

    def test_file_changed_during_packaging_fails_without_publishing_installation(self):
        path=self.f.source/'Game/aaa-large';path.write_bytes(b'data'*100_000)
        original=self.f.ns['build_archives'];changed=[]
        def build(*args,**kwargs):
            report=kwargs['progress']
            def progress(done,total):
                if not changed:
                    path.write_bytes(b'changed');changed.append(True)
                report(done,total)
            return original(*args,**dict(kwargs,progress=progress))
        self.f.ns['build_archives']=build
        with self.assertRaises(RuntimeError):self.f.run_queue()
        self.assertFalse((self.f.storage.games/'Game').exists())

    def test_network_error_keeps_partial_and_range_resume(self):
        self.f.http_mode='truncate'
        with self.assertRaises(OSError):self.f.run_queue()
        _,stage=self.f.queue.stage(self.f.queue.get('game'))
        part,=list((stage/'http').glob('*.part'));offset=part.stat().st_size
        self.f.http_mode='normal';self.f.queue.set_state('game','queued')
        self.check_files(self.f.run_queue())
        self.assertIn('bytes=%d-'%offset,self.f.ranges)

    def test_missing_cached_packet_is_repaired_exactly(self):
        original=self.f.api;removed=[]
        def api(operation,value):
            result=original(operation,value)
            if result.get('block') and not removed:
                chunk=result['block']['chunk']
                (self.f.folder/'chunks'/(chunk['sha256']+'.bin')).unlink();removed.append(chunk)
            return result
        self.f.api=api
        self.check_files(self.f.run_queue())
        self.assertIn('repair',self.f.calls)
        self.assertEqual(json.loads((self.f.folder/'block-0.json').read_bytes())['chunk'],removed[0])

    def test_extraction_rejects_paths_links_and_incorrect_raw_sha(self):
        try:
            from compression.zstd import compress
        except ImportError:
            from zstandard import ZstdCompressor
            compress=ZstdCompressor(level=1).compress
        payload=self.f.root/'extract';payload.mkdir()
        for index,(name,kind,bad_sha) in enumerate((('../escape','file',False),('safe','link',False),('safe','file',True))):
            raw=io.BytesIO()
            with tarfile.open(fileobj=raw,mode='w',format=tarfile.PAX_FORMAT) as archive:
                member=tarfile.TarInfo(name)
                if kind=='link':member.type=tarfile.SYMTYPE;member.linkname='../escape';archive.addfile(member)
                else:
                    member.size=4;member.pax_headers={'NGS.offset':'0','NGS.size':'4'}
                    archive.addfile(member,io.BytesIO(b'data'))
            data=raw.getvalue();part=self.f.root/('bad-%d.part'%index);part.write_bytes(compress(data))
            chunk=dict(raw_size=len(data),raw_sha256='0'*64 if bad_sha else hashlib.sha256(data).hexdigest())
            with self.assertRaises(ValueError):unpack_archive(part,chunk,payload,threading.Event(),lambda *a:None,0,4)
        self.assertFalse((self.f.root/'escape').exists())
