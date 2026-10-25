"""Back up an initialized public server before replacing its owned plugin code."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def plugin_digest(folder):
    digest=hashlib.sha256()
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():raise ValueError('Linked plugin source rejected')
        if path.is_file() and '__pycache__' not in path.parts:
            digest.update(path.relative_to(folder).as_posix().encode()+b'\0'+path.read_bytes())
    return digest.hexdigest()


def verify_seed(seed, data):
    manifest=seed/'manifest.json'
    if any(path.is_symlink() for path in (manifest,seed/'install.py')):
        raise ValueError('Linked bootstrap rejected')
    info=json.loads(manifest.read_text())
    if (info.get('schema')!=1 or info.get('project')!='ngs_launcher' or info.get('version')!='5.0.0.a02'
            or info.get('kind')!='clear' or info.get('personal_data') is not False or info.get('files')
            or set(info.get('hashes',{}))!={'install.py'}
            or hashlib.sha256((seed/'install.py').read_bytes()).hexdigest()!=info['hashes']['install.py']):
        raise ValueError('Only the data-free public bootstrap is accepted')
    fingerprint=hashlib.sha256(manifest.read_bytes()).hexdigest()
    marker=data/'.ngs-migrated'
    if marker.is_symlink():raise ValueError('Linked bootstrap marker rejected')
    if marker.exists() and marker.read_text().strip()!=fingerprint:
        raise ValueError('This server uses another bootstrap; existing data was not changed')
    pending=data/'.ngs-bootstrap-started'
    if pending.is_symlink():raise ValueError('Linked bootstrap marker rejected')
    if not marker.exists():
        if pending.exists() and pending.read_text().strip()!=fingerprint:
            raise ValueError('Another bootstrap was started; existing data was not changed')
        if (data/'.initialized').exists() and not pending.exists():
            raise ValueError('Initialized server has no matching public bootstrap marker')
        if not pending.exists():
            with pending.open('x') as stream:stream.write(fingerprint+'\n')
            os.chmod(pending,0o600)
    return marker.exists()


def backup_databases(folder, password, fingerprint):
    if folder.is_symlink():raise ValueError('Linked backup folder rejected')
    folder.mkdir(parents=True,exist_ok=True);os.chmod(folder,0o700)
    destination=folder/('before-update-'+str(time.time_ns())+'-'+fingerprint[:12])
    destination.mkdir(mode=0o700)
    env=dict(os.environ,PGPASSWORD=password)
    try:
        for name in ('sthpw','ngs_launcher'):
            path=destination/(name+'.dump')
            with path.open('xb') as output:
                os.chmod(path,0o600)
                result=subprocess.run(['/usr/lib/postgresql/16/bin/pg_dump','-h','db','-U','postgres','-Fc',name],env=env,stdout=output,stderr=subprocess.PIPE)
            if result.returncode:raise RuntimeError('Database backup failed; runtime update stopped')
    except Exception:
        for name in ('sthpw','ngs_launcher'):(destination/(name+'.dump')).unlink(missing_ok=True)
        destination.rmdir()
        raise
    (destination/'complete.json').write_text(json.dumps(dict(schema=1,plugin=fingerprint,databases=['sthpw','ngs_launcher'])))
    return destination


def refresh_plugin(source, target):
    if target.is_symlink():raise ValueError('Linked installed plugin rejected')
    target.mkdir(parents=True,exist_ok=True)
    for path in sorted(source.rglob('*')):
        if not path.is_file():continue
        relative=path.relative_to(source)
        if '__pycache__' in relative.parts:continue
        destination=target/relative
        if any(p.is_symlink() for p in [destination,*list(destination.parents)[:len(relative.parts)-1]]):
            raise ValueError('Linked installed plugin file rejected')
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent,prefix='.ngs-',delete=False) as output:
            temporary=Path(output.name);output.write(path.read_bytes());output.flush();os.fsync(output.fileno())
        try:os.chmod(temporary,0o644);os.replace(temporary,destination)
        finally:temporary.unlink(missing_ok=True)
    return target


def main():
    data=Path('/opt/tactic/tactic_data');source=Path('/opt/ngs/plugin')
    initialized=verify_seed(Path('/seed'),data)
    fingerprint=plugin_digest(source);marker=data/'.ngs-runtime'
    target=data/'plugins/ngs_launcher_plugin'
    if marker.is_symlink() or not target.resolve().is_relative_to(data.resolve()):
        raise ValueError('Linked runtime path rejected')
    if marker.is_file() and marker.read_text().strip()==fingerprint:return
    if initialized:
        backup_databases(Path('/backups'),Path('/run/ngs-secrets/postgres_password').read_text().strip(),fingerprint)
    refresh_plugin(source,target)
    for path in [target,*target.rglob('*')]:os.chown(path,10001,10001)
    marker.write_text(fingerprint+'\n');os.chown(marker,10001,10001);os.chmod(marker,0o600)


if __name__=='__main__':main()
