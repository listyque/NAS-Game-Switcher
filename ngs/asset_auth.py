"""Authorize NAS assets without sending a profile token to other origins."""
import json
import re
import time
from urllib.parse import urlsplit
from urllib.request import BaseHandler

from ngs.install import safe_path


class AssetAuthorization(BaseHandler):
    def __init__(self, storage, origin):
        self.storage, self.origin = storage, origin

    def https_request(self, request):
        address = urlsplit(request.full_url)
        origin = address.scheme + '://' + address.netloc.lower()
        if address.scheme != 'https' or origin != self.origin or not address.path.startswith('/assets/ngs_launcher/'):
            return request
        try:
            path = safe_path(self.storage.config, 'player-account.json')
            if path.stat().st_size > 32768: return request
            account = json.loads(path.read_bytes())['account']
            token = account.get('token', '')
            if (account.get('server') == origin and re.fullmatch('[0-9a-f]{64}', token)
                    and type(account.get('expires')) is int and account['expires'] > time.time()):
                request.add_unredirected_header('Authorization', 'Bearer ' + token)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return request
