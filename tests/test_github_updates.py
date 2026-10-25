import json
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from ngs.github_updates import GitHubReleases,checked_url,OFFICIAL_UPDATE_URL,ReleaseRedirect


class GitHubUpdatesTests(unittest.TestCase):
    def test_preview_release_is_used_and_drafts_are_ignored(self):
        source=GitHubReleases(threading.Event())
        manifest='https://github.com/listyque/NAS-Game-Switcher/releases/download/preview/updates.json'
        rows=[dict(draft=True,assets=[dict(name='updates.json',browser_download_url='https://evil.invalid/feed')]),
              dict(draft=False,prerelease=True,assets=[dict(name='updates.json',browser_download_url=manifest)])]
        envelope=dict(payload='signed',signature='signature')
        with patch.object(source,'read',side_effect=[json.dumps(rows).encode(),json.dumps(envelope).encode()]) as read:
            self.assertEqual(source.manifest(),envelope)
            self.assertEqual(read.call_args.args[0],manifest)

    def test_urls_cannot_escape_repository_https_or_allowed_cdn(self):
        good='https://github.com/listyque/NAS-Game-Switcher/releases/download/v1/NGS.zip'
        self.assertEqual(checked_url(good),good)
        self.assertEqual(checked_url('https://release-assets.githubusercontent.com/asset?signature=x'),'https://release-assets.githubusercontent.com/asset?signature=x')
        for bad in ('http://github.com/listyque/NAS-Game-Switcher/releases/download/v1/file',
                    'https://github.com.evil.invalid/file','https://user:pass@github.com/listyque/NAS-Game-Switcher/releases/download/v1/file',
                    'https://github.com/another/repository/releases/download/v1/file',
                    good+'/../other',good+'/%2e%2e/other','https://api.github.com/repos/another/repo/releases'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):checked_url(bad)

    def test_unsigned_asset_size_or_name_cannot_replace_signed_package(self):
        source=GitHubReleases(threading.Event())
        release=dict(filename='NGS-windows-x64-20261025-120000.zip',size=100)
        url='https://github.com/listyque/NAS-Game-Switcher/releases/download/v1/'+release['filename']
        source._releases=[dict(draft=False,assets=[dict(name=release['filename'],size=100,browser_download_url=url)])]
        self.assertEqual(source.package_path(release),url.removeprefix('https://github.com'))
        source._releases[0]['assets'][0]['size']=101
        with self.assertRaises(ValueError):source.package_path(release)

    def test_missing_manifest_means_no_public_release_yet(self):
        source=GitHubReleases(threading.Event());source._releases=[]
        self.assertIsNone(source.manifest())
