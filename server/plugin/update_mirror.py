"""Prefetch signed NGS releases once a day, shared by all TACTIC workers."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import threading
import time
import urllib.request

from .update_transport import GitHubReleases, validate_release

DAY = 86400


def verified(envelope, public):
    if (not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'}
            or not isinstance(envelope['payload'], str) or len(envelope['payload'].encode()) > 16000
            or not isinstance(envelope['signature'], str) or len(envelope['signature']) > 400):
        raise ValueError('Invalid update signature format')
    try:
        from Cryptodome.PublicKey import RSA
        from Cryptodome.Signature import pkcs1_15
        from Cryptodome.Hash import SHA256
    except ImportError:
        from Crypto.PublicKey import RSA
        from Crypto.Signature import pkcs1_15
        from Crypto.Hash import SHA256
    if len(public) != 283 or struct.unpack('<6I', public[:24]) != (0x31415352, 2048, 3, 256, 0, 0):
        raise ValueError('Invalid pinned update authority')
    key = RSA.construct((int.from_bytes(public[27:], 'big'), int.from_bytes(public[24:27], 'big')))
    pkcs1_15.new(key).verify(SHA256.new(envelope['payload'].encode()),
                           base64.b64decode(envelope['signature'], validate=True))
    return validate_release(json.loads(envelope['payload']))


def safe(root, name):
    path = root / name
    if root.is_symlink() or root.resolve() != root or path.is_symlink() or path.resolve().parent != root:
        raise ValueError('Redirected NGS update cache')
    return path


def atomic(path, data):
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.ngs-update-', delete=False) as stream:
            name = Path(stream.name)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if name: name.unlink(missing_ok=True)


class Mirror:
    def __init__(self, assets, private, public):
        self.assets, self.private, self.public = Path(assets), Path(private), public
        for root in (self.assets, self.private):
            if root.is_symlink() or root.resolve() != root: raise ValueError('Redirected NGS update cache')
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.releases = safe(self.assets, 'releases')
        self.releases.mkdir(exist_ok=True)
        self.state_path = safe(self.private, 'state.json')

    def state(self):
        if not self.state_path.exists(): return {}
        if self.state_path.stat().st_size > 4096: raise ValueError('Oversized update state')
        return json.loads(self.state_path.read_bytes())

    def write_state(self, **values):
        atomic(self.state_path, json.dumps(values).encode())

    def envelope(self):
        feed = safe(self.assets, 'updates.json')
        if not feed.exists(): return None
        if feed.stat().st_size > 20000: raise ValueError('Oversized update feed')
        envelope = json.loads(feed.read_bytes())
        release = verified(envelope, self.public)
        path = safe(self.releases, release['sha256'] + '.zip')
        if not path.is_file() or path.stat().st_size != release['size']:
            raise ValueError('The cached update package is missing')
        return envelope

    def request(self, force=False, start=True):
        import fcntl
        lock = safe(self.private, 'worker.lock').open('ab')
        try:
            try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                state = self.state()
                return dict(state='checking', done=state.get('done', 0), total=state.get('total', 0))
            state = self.state()
            age = time.time() - state.get('attempted_at', 0)
            if start and age >= (60 if force else DAY):
                self.write_state(state='checking', attempted_at=time.time(), done=0, total=0)
                threading.Thread(target=self.run, args=(lock,), name='ngs-update-mirror', daemon=True).start()
                lock = None
                return dict(state='checking', done=0, total=0)
            if state.get('state') == 'checking':
                return dict(state='error')  # A worker died; the next explicit check can retry.
            if state.get('state') == 'error': return dict(state='error')
            return dict(state='ready', envelope=self.envelope())
        finally:
            if lock: lock.close()

    def run(self, lock):
        attempted = time.time()
        try:
            source = GitHubReleases(threading.Event())
            envelope = source.manifest()
            current = self.envelope()
            if envelope is not None:
                release = verified(envelope, self.public)
                old = verified(current, self.public) if current else None
                if not old or tuple(map(int, release['version'].split('.'))) > tuple(map(int, old['version'].split('.'))):
                    target = safe(self.releases, release['sha256'] + '.zip')
                    if target.exists():
                        with target.open('rb') as stream:
                            digest = hashlib.sha256()
                            while chunk := stream.read(4 * 1024 * 1024): digest.update(chunk)
                            if target.stat().st_size != release['size'] or digest.hexdigest() != release['sha256']:
                                raise ValueError('Immutable cached update differs')
                    else:
                        if shutil.disk_usage(self.releases).free < release['size'] + 512 * 1024**2:
                            raise OSError('Not enough NAS space for the update')
                        self.download(source, release, target, attempted)
                    # Re-read under the publication lock: a local transition may have arrived during the download.
                    self.activate(envelope, release)
                elif release['version'] == old['version'] and release != old:
                    raise ValueError('An immutable release version changed')
            self.write_state(state='ready', attempted_at=attempted)
        except Exception as error:
            self.write_state(state='error', attempted_at=attempted, error=type(error).__name__)
        finally:
            lock.close()

    def activate(self, envelope, release):
        import fcntl
        with safe(self.releases, '.feed.lock').open('ab') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self.envelope()
            old = verified(current, self.public) if current else None
            if not old or tuple(map(int, release['version'].split('.'))) > tuple(map(int, old['version'].split('.'))):
                atomic(safe(self.assets, 'updates.json'), json.dumps(envelope, ensure_ascii=False).encode())
            elif release['version'] == old['version'] and release != old:
                raise ValueError('An immutable release version changed')

    def download(self, source, release, target, attempted):
        partial = safe(self.releases, release['sha256'] + '.mirror-part')
        digest, done, last = hashlib.sha256(), 0, 0
        try:
            request = urllib.request.Request(source.origin + source.package_path(release),
                                             headers={'User-Agent': 'NAS-Game-Switcher', 'Accept': 'application/octet-stream'})
            with source.opener.open(request, timeout=30) as response, partial.open('wb') as output:
                length = response.headers.get('Content-Length')
                if length is not None and int(length) != release['size']: raise ValueError('Update size mismatch')
                deadline = time.monotonic() + 1800
                while chunk := response.read(1024 * 1024):
                    done += len(chunk)
                    if done > release['size'] or time.monotonic() > deadline: raise ValueError('Update download limit exceeded')
                    output.write(chunk); digest.update(chunk)
                    if time.monotonic() - last >= 1:
                        self.write_state(state='checking', attempted_at=attempted, done=done, total=release['size'])
                        last = time.monotonic()
                output.flush(); os.fsync(output.fileno())
            if done != release['size'] or digest.hexdigest() != release['sha256']:
                raise ValueError('Update SHA256 mismatch')
            os.chmod(partial, 0o644)
            os.link(partial, target)
        finally:
            partial.unlink(missing_ok=True)


def request_check(force=False, start=True):
    from pyasm.biz import Project
    from pyasm.common import Environment
    if Project.get().get_code() != 'ngs_launcher' or Project.get().get_database_name() != 'ngs_launcher':
        raise PermissionError('NGS project required')
    return Mirror(Path(Environment.get_asset_dir()).resolve() / 'ngs_launcher',
                  Path(Environment.get_tmp_dir()).resolve().parent / 'private/ngs-update-mirror',
                  Path(__file__).with_name('update-public-key.bin').read_bytes()).request(force, start)


def main():
    import fcntl
    mirror = Mirror(Path('/opt/tactic/assets/ngs_launcher'),
                    Path('/opt/tactic/tactic_data/private/ngs-update-mirror'),
                    Path(__file__).with_name('update-public-key.bin').read_bytes())
    with safe(mirror.private, 'scheduler.lock').open('ab') as lock:
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        while True:
            mirror.request()
            time.sleep(60)


if __name__ == '__main__': main()
