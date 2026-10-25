import errno
import ssl
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from ngs.http_retry import retry_network
from ngs.install import Cancelled


class NetworkRetryTests(unittest.TestCase):
    def test_recovers_and_exhausts_three_retries(self):
        events = []
        operation = Mock(side_effect=[ConnectionResetError(), TimeoutError(), 'ok'])
        with patch('ngs.http_retry.RETRY_DELAYS', (0, 0, 0)):
            self.assertEqual(retry_network(operation, threading.Event(), lambda *args: events.append(args)), 'ok')
            self.assertEqual(operation.call_count, 3)
            self.assertEqual(len(events), 2)
            self.assertIn('повтор 2 из 3', events[-1][3])
            operation = Mock(side_effect=TimeoutError('offline'))
            with self.assertRaises(TimeoutError): retry_network(operation, threading.Event())
            self.assertEqual(operation.call_count, 4)

    def test_permanent_errors_and_disk_failures_are_not_retried(self):
        for error in (ValueError('checksum'), PermissionError(), OSError(errno.ENOSPC, 'full'),
                      ssl.SSLCertVerificationError(), urllib.error.URLError(ssl.SSLCertVerificationError()),
                      urllib.error.HTTPError('http://nas', 403, 'Forbidden', {}, None)):
            with self.subTest(error=type(error).__name__):
                operation = Mock(side_effect=error)
                with self.assertRaises(type(error)): retry_network(operation, threading.Event())
                self.assertEqual(operation.call_count, 1)

    def test_cancel_interrupts_backoff_without_another_request(self):
        cancel, operation = threading.Event(), Mock(side_effect=ConnectionResetError())
        with patch('ngs.http_retry.time.sleep', side_effect=lambda seconds: cancel.set()):
            with self.assertRaises(Cancelled): retry_network(operation, cancel)
        self.assertEqual(operation.call_count, 1)

    def test_deadline_bounds_backoff_and_prevents_late_requests(self):
        operation = Mock(side_effect=ConnectionResetError())
        start = time.monotonic()
        with self.assertRaises(ConnectionResetError):
            retry_network(operation, threading.Event(), deadline=start + .02)
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(operation.call_count, 1)
        with self.assertRaises(TimeoutError):
            retry_network(operation, threading.Event(), deadline=start)
        self.assertEqual(operation.call_count, 1)
