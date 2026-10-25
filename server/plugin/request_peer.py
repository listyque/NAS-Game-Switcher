"""Trust a forwarded client IP only when it is vouched for by our gateway."""
import hmac
import ipaddress
from pathlib import Path


def client_address(request):
    try:
        secret = Path('/run/ngs-secrets/ngs_proxy_token').read_text().strip()
        provided = request.headers.get('X-NGS-Proxy-Token', '')
        if len(secret) == 64 and hmac.compare_digest(secret, provided):
            return str(ipaddress.ip_address(request.headers.get('X-NGS-Client-IP', '')))
    except (OSError, ValueError):
        pass
    return request.remote.ip
