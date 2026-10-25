"""Public release transport; package trust still comes from the pinned NGS signature."""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, unquote
from ngs.install import check_cancel

REPOSITORY = 'listyque/NAS-Game-Switcher'
OFFICIAL_UPDATE_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases'
ASSET_PREFIX = '/' + REPOSITORY + '/releases/download/'


def checked_url(url):
    if not isinstance(url,str) or len(url)>8192:
        raise ValueError('Invalid GitHub update URL')
    parsed = urlsplit(url)
    path=unquote(parsed.path)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443)
            or parsed.fragment or parsed.hostname not in ('api.github.com','github.com','release-assets.githubusercontent.com')):
        raise ValueError('Untrusted GitHub update URL')
    if '\\' in path or any(p in ('.','..') for p in path.split('/')) or any(ord(c)<32 for c in url):
        raise ValueError('Unsafe GitHub update path')
    if parsed.hostname == 'api.github.com' and parsed.path != '/repos/' + REPOSITORY + '/releases':
        raise ValueError('Unexpected GitHub repository URL')
    if parsed.hostname == 'github.com' and not parsed.path.startswith(ASSET_PREFIX):
        raise ValueError('Unexpected GitHub release URL')
    return url


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        checked_url(new_url)
        return super().redirect_request(request,response,code,message,headers,new_url)


class GitHubReleases:
    origin = 'https://github.com'

    def __init__(self, cancel):
        self.cancel = cancel
        self.opener = urllib.request.build_opener(ReleaseRedirect())
        self._releases = None

    def read(self, url, limit):
        check_cancel(self.cancel)
        request=urllib.request.Request(checked_url(url),headers={'Accept':'application/vnd.github+json','User-Agent':'NAS-Game-Switcher'})
        with self.opener.open(request,timeout=12) as response:
            data=response.read(limit+1)
        check_cancel(self.cancel)
        if len(data)>limit:raise ValueError('GitHub release response exceeds its size limit')
        return data

    def releases(self):
        if self._releases is None:
            try:
                items=json.loads(self.read(OFFICIAL_UPDATE_URL+'?per_page=10',1024*1024))
            except urllib.error.HTTPError as error:
                status=error.code;error.close()
                if status==404:items=[]
                else:raise
            if not isinstance(items,list) or len(items)>10:raise ValueError('Invalid GitHub release list')
            self._releases=[item for item in items if isinstance(item,dict) and item.get('draft') is False]
        return self._releases

    def manifest(self):
        for release in self.releases():
            assets=release.get('assets',[])
            if not isinstance(assets,list) or len(assets)>100:raise ValueError('Invalid release assets')
            matches=[a for a in assets if isinstance(a,dict) and a.get('name')=='updates.json']
            if not matches:continue
            if len(matches)!=1:raise ValueError('Duplicate release manifest')
            url=matches[0].get('browser_download_url','')
            if not url.startswith(self.origin+ASSET_PREFIX):raise ValueError('Manifest is outside the NGS repository')
            return json.loads(self.read(url,20000))
        return None

    def package_path(self, signed_release):
        for release in self.releases():
            assets=release.get('assets',[])
            if not isinstance(assets,list) or len(assets)>100:raise ValueError('Invalid release assets')
            matches=[a for a in assets if isinstance(a,dict) and a.get('name')==signed_release['filename']]
            if not matches:continue
            if len(matches)!=1 or matches[0].get('size')!=signed_release['size']:raise ValueError('Release asset size mismatch')
            url=checked_url(matches[0].get('browser_download_url',''))
            if not url.startswith(self.origin+ASSET_PREFIX) or urlsplit(url).query:raise ValueError('Invalid release asset location')
            return urlsplit(url).path
        raise ValueError('The selected update is no longer listed. Check for updates again.')
