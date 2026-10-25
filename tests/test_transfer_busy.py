import threading
import time
import unittest
from unittest.mock import patch

from ngs.api import ApiCallError
from ngs.install import Cancelled
from tests import test_on_demand as fixtures


class TransferBusyTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OnDemandTests('test_first_block_arrives_before_end_cache_is_bounded_and_install_is_exact')
        self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_legacy_transfer_retries_the_pinned_ack(self):
        original=self.f.api;attempts=[];events=[]
        def request(operation,value):
            if value['action']=='transfer' and value.get('ack')==0:
                attempts.append(dict(value))
                if len(attempts)==1:raise ApiCallError(409,'Busy',reason='busy')
            return original(operation,value)
        self.f.api=request
        receipt=self.f.run_queue(progress=lambda *args:events.append(args))
        self.assertTrue(receipt['managed'])
        self.assertGreaterEqual(len(attempts),2)
        self.assertEqual(attempts[0],attempts[1])
        self.assertTrue(any(e[0]=='waiting' and 'Сервер занят' in e[3] for e in events))

    def test_pause_interrupts_busy_wait_without_another_request(self):
        cancel=threading.Event();calls=[]
        def request(operation,value):
            calls.append(value);raise ApiCallError(409,'Busy',reason='busy')
        self.f.api=request
        def progress(phase,*args):
            if phase=='waiting':cancel.set()
        started=time.monotonic()
        with self.assertRaises(Cancelled):self.f.run_queue(cancel,progress)
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.f.queue.get('game')['state'],'paused')

    def test_busy_wait_is_bounded_and_keeps_queue(self):
        def request(*args):raise ApiCallError(409,'Busy',reason='busy')
        self.f.api=request
        started=time.monotonic()
        with patch('ngs.on_demand.API_BUSY_WAIT_SECONDS',.03),self.assertRaises(ApiCallError):self.f.run_queue()
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(self.f.queue.get('game')['state'],'error')

    def test_replay_and_permissions_are_not_retried(self):
        for status,reason in [(409,None),(403,None),(401,None)]:
            calls=[]
            def request(*args):
                calls.append(args);raise ApiCallError(status,'Rejected',reason=reason)
            self.f.api=request
            self.f.queue.set_state('game','queued')
            with self.assertRaises(ApiCallError):self.f.run_queue()
            self.assertEqual(len(calls),1)
