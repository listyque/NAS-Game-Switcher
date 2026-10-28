"""Signed, project-only metadata protocol; these keys are not TACTIC tickets."""

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from ngs.tactic import TacticClient, _NoRedirect
from ngs.metadata import metadata_reference
from ngs.remote_catalog import validate_catalog_request
from ngs.diagnostics_protocol import validate_diagnostic_request
from ngs.backups import (
    DOWNLOAD_CHUNK,
    MAX_BACKUPS_PER_DEVICE,
    MAX_BACKUP_BYTES,
    MAX_DEVICES_PER_PROFILE,
    UPLOAD_CHUNK,
    clean_device_name,
    validate_backup_request,
)


API_PATH = "/tactic/ngs_launcher/ngs-api/v1/metadata"
MAX_BODY = 2048
MAX_RESPONSE = 64 * 1024


class ApiRejected(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class ApiCallError(RuntimeError):
    def __init__(self, status, message, *, reason=None):
        self.status = status
        self.reason = reason
        super().__init__(message)


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"operation", "value"}:
        raise ValueError("Expected only operation and value")
    operation, value = payload["operation"], payload["value"]
    if operation == 'updates':
        if not isinstance(value, dict) or set(value) != {'action'} or value['action'] not in ('check', 'refresh', 'status'):
            raise ValueError('Invalid update check request')
        return operation, value
    if operation == 'diagnostics': return operation, validate_diagnostic_request(value)
    if operation == 'backup': return operation, validate_backup_request(value)
    if operation in ("catalog", "transfer"):
        if operation == "transfer" and (not isinstance(value, dict) or value.get("action") not in (
                "estimate", "download", "transfer", "repair", "release", "installer_estimate", "installer_download", "installer_transfer", "installer_repair", "installer_release")):
            raise ValueError("Unsupported transfer action")
        return operation, validate_catalog_request(value)
    if operation not in ("search", "resolve") or not isinstance(value, str):
        raise ValueError("Only search and resolve are supported")
    if operation == "search":
        if not 1 <= len(value.strip()) <= 160 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid game title")
    else:
        metadata_reference(value)
    return operation, value


def validate_headers(headers, now):
    device, stamp, nonce, signature = (headers.get("X-NGS-" + key, "") for key in ("Device", "Time", "Nonce", "Signature"))
    if (not re.fullmatch(r"[0-9a-f]{32}", device) or not re.fullmatch(r"[0-9]{10,12}", stamp)
            or not re.fullmatch(r"[0-9a-f]{32}", nonce) or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or abs(int(stamp) - now) > 90):
        raise ApiRejected(401, "Invalid or expired request")
    return device, stamp, nonce, signature


def request_signature(secret, device, stamp, nonce, body):
    message = "NGS-Metadata-v1\nrequest\nPOST\n" + API_PATH + "\n" + device + "\n" + stamp + "\n" + nonce + "\n" + hashlib.sha256(body).hexdigest()
    return hmac.new(bytes.fromhex(secret), message.encode("ascii"), hashlib.sha256).hexdigest()


def response_signature(secret, nonce, status, body):
    message = "NGS-Metadata-v1\nresponse\n" + nonce + "\n" + str(status) + "\n" + hashlib.sha256(body).hexdigest()
    return hmac.new(bytes.fromhex(secret), message.encode("ascii"), hashlib.sha256).hexdigest()


def claim_state(state, nonce, operation, now):
    if not isinstance(state, dict) or state.get("schema", 1) != 1:
        raise ValueError("Invalid device state")
    recent = state.get("recent", [])
    if (not isinstance(recent, list) or len(recent) > 1000 or any(not isinstance(row, list) or len(row) != 3
            or type(row[0]) is not int or not isinstance(row[1], str) or row[2] not in ("search", "resolve", "catalog", "transfer") for row in recent)):
        raise ValueError("Invalid device history")
    recent = [row for row in recent if row[0] >= now - 600]
    diagnostic = state.get('diagnostic_recent', [])
    if (not isinstance(diagnostic, list) or len(diagnostic) > 180 or any(not isinstance(row, list) or len(row) != 2
            or type(row[0]) is not int or not isinstance(row[1], str) for row in diagnostic)):
        raise ValueError('Invalid diagnostic history')
    diagnostic = [row for row in diagnostic if row[0] >= now - 600]
    backup = state.get('backup_recent', [])
    if (not isinstance(backup, list) or len(backup) > 240 or any(not isinstance(row, list) or len(row) != 2
            or type(row[0]) is not int or not isinstance(row[1], str) for row in backup)):
        raise ValueError('Invalid backup history')
    backup = [row for row in backup if row[0] >= now - 600]
    updates = state.get('update_recent', [])
    if (not isinstance(updates, list) or len(updates) > 360 or any(not isinstance(row, list) or len(row) != 2
            or type(row[0]) is not int or not isinstance(row[1], str) for row in updates)):
        raise ValueError('Invalid update history')
    updates = [row for row in updates if row[0] >= now - 600]
    if any(row[1] == nonce for row in recent + diagnostic + backup + updates):
        raise ApiRejected(409, "Repeated request")
    if operation == 'diagnostics':
        if len(diagnostic) >= 180: raise ApiRejected(429, 'Diagnostic request limit reached')
        # Uploads do not acquire the metadata/transfer lease or spend its rate budget.
        return dict(state, schema=1, diagnostic_recent=diagnostic + [[now, nonce]])
    if operation == 'backup':
        if len(backup) >= 240: raise ApiRejected(429, 'Backup request limit reached')
        return dict(state, schema=1, backup_recent=backup + [[now, nonce]])
    if operation == 'updates':
        if len(updates) >= 360: raise ApiRejected(429, 'Request limit reached; retry later')
        return dict(state, schema=1, update_recent=updates + [[now, nonce]])
    if state.get("busy_until", 0) > now:
        raise ApiRejected(409, "Another metadata request is running")
    group = ("search", "resolve") if operation in ("search", "resolve") else (operation,)
    count = sum(row[2] in group for row in recent)
    if count >= (480 if operation == "transfer" else 240 if operation == "catalog" else 60) or (operation == "resolve" and sum(row[2] == "resolve" for row in recent) >= 20):
        raise ApiRejected(429, "Request limit reached; retry later")
    return dict(state, schema=1, recent=recent + [[now, nonce, operation]], busy_nonce=nonce, busy_until=now + 180,
                diagnostic_recent=diagnostic)


class MetadataApi:
    def __init__(self, storage, base_url):
        from ngs.install import safe_path
        TacticClient(base_url, "ngs_launcher")
        address = urlsplit(base_url)
        self.url = urlunsplit((address.scheme, address.netloc, API_PATH, "", ""))
        from ngs.players import account_credentials
        path = safe_path(storage.config, "player-account.json")
        try:
            if path.stat().st_size > 8192: raise ValueError('Account too large')
            account = json.loads(path.read_bytes())['account']
            config = account_credentials(account, base_url)
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError("Sign in to NGS in Settings. Access will be configured automatically.") from None
        self.device, self.secret = config['device'], config['secret']
        self.opener = urllib.request.build_opener(_NoRedirect)
        from ngs.diagnostics import remember_secret
        remember_secret(self.secret)

    def call(self, operation, value):
        from ngs.diagnostics import event
        if operation == 'diagnostics': return self._call(operation, value)
        details = {k: value[k] for k in ('action', 'folder', 'executable', 'query', 'provider', 'offset', 'ack') if k in value} if isinstance(value, dict) else dict(query=value)
        started = time.monotonic()
        event('api.start', operation=operation, server=self.url, **details)
        try:
            result = self._call(operation, value)
            summary = {k: result.get(k) for k in ('stage', 'total', 'offset', 'message')} if isinstance(result, dict) else {}
            event('api.done', operation=operation, duration=time.monotonic()-started, **summary)
            return result
        except Exception as error:
            event('api.error', operation=operation, duration=time.monotonic()-started, error_type=type(error).__name__,
                  error=str(error), status=getattr(error, 'status', None))
            raise

    def _call(self, operation, value):
        payload = {"operation": operation, "value": value}
        validate_payload(payload)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        stamp, nonce = str(int(time.time())), secrets.token_hex(16)
        headers = {"Content-Type": "application/octet-stream", "Accept": "application/json", "Cache-Control": "no-store",
                   "X-NGS-Device": self.device, "X-NGS-Time": stamp, "X-NGS-Nonce": nonce,
                   "X-NGS-Signature": request_signature(self.secret, self.device, stamp, nonce, body)}
        request = urllib.request.Request(self.url, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=10 if operation in ('transfer', 'diagnostics', 'backup') else 90)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read(MAX_RESPONSE + 1)
            expected = response.headers.get('Content-Length')
            if expected is not None and 0 <= int(expected) <= MAX_RESPONSE and len(data) != int(expected):
                raise ConnectionError("The NGS API response is incomplete.")
            status = response.code
            signature = response.headers.get("X-NGS-Signature", "")
            if (len(data) > MAX_RESPONSE or not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature)
                    or not hmac.compare_digest(signature, response_signature(self.secret, nonce, status, data))):
                raise RuntimeError("The NGS API is unavailable, the key was revoked, or the response signature is invalid.")
        if status != 200:
            if status == 409:
                error = json.loads(data)
                if isinstance(error, dict) and error.get('schema') == 1 and error.get('error') == 'DeviceBusy':
                    raise ApiCallError(409, "The NGS server is temporarily processing another request.", reason='busy')
            if operation in ("catalog", "transfer", "backup") and status == 503:
                # Keep the signed HTTP status so the transfer retry policy can classify it.
                raise ApiCallError(503, "The server catalog manager is temporarily unavailable. Retrying the request.")
            if operation in ("catalog", "transfer", "backup") and status == 400:
                error = json.loads(data)
                if isinstance(error, dict) and isinstance(error.get("error"), str) and len(error["error"]) <= 500:
                    if error["error"] == "Invalid metadata request":
                        raise RuntimeError("The server catalog manager is not connected. Set up the NGS server first.")
                    raise RuntimeError(error["error"])
            if operation == "diagnostics":
                # Keep the signed server reason: a generic artwork message hid that
                # player devices were refused diagnostic storage.
                try:
                    error = json.loads(data)
                except ValueError:
                    error = {}
                details = {k: str(error.get(k))[:200] for k in ("error", "stage", "kind")
                           if isinstance(error, dict) and error.get(k)}
                raise ApiCallError(status, "Diagnostics upload rejected: HTTP %s %s"
                                   % (status, json.dumps(details, ensure_ascii=False)))
            messages = {400: "Invalid artwork request.", 401: "Your NGS session expired. Sign in again in Settings.",
                        403: "This device is not allowed to manage the NGS catalog.",
                        409: "The request is running or has already been processed. Search again.",
                        429: "Too many requests. Try again later.", 503: "The server has not received the artwork yet. Search again."}
            raise ApiCallError(status, messages.get(status, "The NGS API rejected the request."))
        document = json.loads(data)
        if not isinstance(document, dict) or document.get("schema") != 1 or "result" not in document:
            raise ValueError("Invalid NGS API response")
        return document["result"]
