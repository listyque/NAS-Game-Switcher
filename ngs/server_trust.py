"""Server authority pins, provisioned locally or through verified HTTPS."""
import base64
import json
import hashlib
from urllib.parse import urlsplit


def connection_link(value):
    """Parse public enrollment information without connecting or changing trust."""
    import re
    from urllib.parse import parse_qs
    from ngs.i18n import tr
    try:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value.strip())
        fields = parse_qs(parsed.fragment, strict_parsing=True, max_num_fields=1)
        if set(fields) != {'ngs-key'} or len(fields['ngs-key']) != 1: raise ValueError
        fingerprint = fields['ngs-key'][0].lower()
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.path not in ('', '/', '/tactic', '/tactic/', '/tactic/ngs_launcher', '/tactic/ngs_launcher/')
                or any(ord(c) <= 32 for c in value.strip()) or not re.fullmatch('[0-9a-f]{64}', fingerprint)):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535: raise ValueError
        return dict(server=parsed.scheme + '://' + parsed.netloc.lower() + '/tactic', fingerprint=fingerprint)
    except ValueError:
        raise ValueError(tr('Invalid NGS connection link. Copy the complete link from your NAS administrator.')) from None


def server_key_path(storage, origin=None):
    from ngs.install import safe_path
    parsed = urlsplit(origin or storage.read_settings()['tactic_url'])
    origin = parsed.scheme + '://' + parsed.netloc.lower()
    identity = hashlib.sha256(origin.encode()).hexdigest()[:32]
    path = safe_path(storage.config, 'server-trust-' + identity + '.json')
    key = safe_path(storage.config, 'server-public-' + identity + '.bin')
    if not path.exists():
        path = safe_path(storage.config, 'server-trust.json')
        key = safe_path(storage.config, 'server-public-key.bin')
    fallback = storage.resources / 'assets/update-public-key.bin'
    if not path.exists():
        return fallback
    if path.stat().st_size > 2048:
        raise ValueError('Invalid local server trust file')
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or set(data) != {'schema', 'server', 'public'} or data['schema'] != 1:
        raise ValueError('Invalid local server trust file')
    if data['server'] != origin:
        return fallback
    blob = base64.b64decode(data['public'], validate=True)
    if len(blob) != 283 or blob[:4] != b'RSA1' or not key.is_file() or key.read_bytes() != blob:
        raise ValueError('Local server authority does not match its key')
    return key


def connect_server(storage, origin, expected_fingerprint=None):
    """Authenticate a NAS through verified TLS or an operator-supplied key fingerprint."""
    from urllib.request import Request, build_opener
    from urllib.error import HTTPError
    from ngs.catalog import atomic_write
    from ngs.tactic import _NoRedirect
    from ngs.update_crypto import RsaKey
    from ngs import player_protocol
    import hmac
    import re
    parsed = urlsplit(origin)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Invalid server origin')
    if parsed.scheme == 'http':
        import ipaddress
        import socket
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_private
                                or ipaddress.ip_address(row[4][0]).is_unspecified for row in addresses):
            raise ValueError('Internet connections require trusted HTTPS; HTTP pairing is limited to local networks')
    expected = None
    if expected_fingerprint is not None:
        if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) > 128:
            raise ValueError('Invalid server pairing code')
        expected = re.sub(r'[\s-]', '', expected_fingerprint).lower()
        if not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('The server pairing code must contain 64 hexadecimal characters')
    pinned = server_key_path(storage, origin)
    fallback = storage.resources / 'assets/update-public-key.bin'
    if pinned != fallback:
        if expected and not hmac.compare_digest(hashlib.sha256(pinned.read_bytes()).hexdigest(), expected):
            raise ValueError('The pairing code differs from this server\'s saved identity; no trust was changed')
        return pinned
    opener = build_opener(_NoRedirect)
    try:
        with opener.open(Request(origin + '/assets/ngs_launcher/server-public-key.bin'), timeout=12) as response:
            public = response.read(284)
    except HTTPError as error:
        error.close()
        # Older NAS packages publish only the profile key signed by the bundled authority.
        if error.code != 404 or not fallback.is_file(): raise
        public = fallback.read_bytes()
    if len(public) != 283 or public[:4] != b'RSA1': raise ValueError("Invalid NGS server key.")
    if expected:
        if not hmac.compare_digest(hashlib.sha256(public).hexdigest(), expected):
            raise ValueError('The server pairing code does not match; no trust was changed')
    elif parsed.scheme != 'https' and (not fallback.is_file() or public != fallback.read_bytes()):
        raise ValueError('This local server requires a pairing code. Copy it from its NAS administration logs, or use trusted HTTPS.')
    with opener.open(Request(origin + player_protocol.PUBLIC_PATH), timeout=12) as response:
        wire = response.read(32769)
    if len(wire) > 32768: raise ValueError("The NGS server response is too large.")
    descriptor = json.loads(wire)
    if set(descriptor) != {'payload', 'signature'}: raise ValueError("Invalid NGS profile key.")
    payload = player_protocol.decode(descriptor['payload'])
    with RsaKey(public) as key:
        key.verify(player_protocol.DOMAIN + payload, player_protocol.decode(descriptor['signature']))
    info = json.loads(payload)
    if (set(info) != {'schema','public','kid'} or info['schema'] != 1
            or info['kid'] != player_protocol.key_id(info['public'])):
        raise ValueError("Invalid NGS profile key.")
    identity = hashlib.sha256(origin.encode()).hexdigest()[:32]
    from ngs.install import safe_path
    target = safe_path(storage.config, 'server-public-' + identity + '.bin')
    atomic_write(target, public)
    atomic_write(safe_path(storage.config, 'server-trust-' + identity + '.json'), json.dumps(
        dict(schema=1,server=origin,public=base64.b64encode(public).decode())).encode('utf8'))
    return server_key_path(storage, origin)
