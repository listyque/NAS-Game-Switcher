"""Bounded retries for transient network failures; cancellation and disk errors stop."""
import http.client
import ssl
import time
import urllib.error


RETRY_DELAYS = (1, 2, 4)
TRANSIENT_HTTP = {408, 500, 502, 503, 504}


def transient_network_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in TRANSIENT_HTTP
    if isinstance(error, urllib.error.URLError):
        return not isinstance(error.reason, (ssl.SSLCertVerificationError, ValueError))
    return isinstance(error, (TimeoutError, ConnectionError, http.client.IncompleteRead,
                              http.client.RemoteDisconnected, ssl.SSLEOFError))


def retry_network(operation, cancel, progress=lambda *args: None, *, deadline=None):
    from ngs.install import check_cancel
    for attempt in range(len(RETRY_DELAYS) + 1):
        check_cancel(cancel)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("The network request timed out.")
        try:
            return operation()
        except Exception as error:
            check_cancel(cancel)
            if (not transient_network_error(error) or attempt == len(RETRY_DELAYS)
                    or deadline is not None and time.monotonic() >= deadline):
                raise
            delay = RETRY_DELAYS[attempt]
            from ngs.diagnostics import event
            event('http.retry', attempt=attempt+1, delay=delay, error_type=type(error).__name__, error=str(error))
            progress('waiting', 0, 0, "Connection interrupted · retry %d of %d in %d s" %
                     (attempt + 1, len(RETRY_DELAYS), delay))
            until = time.monotonic() + delay
            while time.monotonic() < until:
                check_cancel(cancel)
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                time.sleep(min(.1, max(0, until - time.monotonic())))
