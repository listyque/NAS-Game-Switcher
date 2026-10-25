"""Private, device-scoped NGS session archives; never stored under public assets."""
from ngs.diagnostics_protocol import validate_diagnostic_request


def provision_diagnostics():
    import json
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment
    project = Project.get()
    if (not Environment.get_security().is_admin() or project.get_code() != 'ngs_launcher'
            or project.get_database_name() != 'ngs_launcher'
            or project.get_value('description') != 'NGS handheld game catalog; managed from D:/code/NGS'):
        raise PermissionError('NGS administrator required')
    root = Path('/opt/tactic/tactic_data/ngs_launcher/diagnostics')
    for path in (root, *root.parents):
        if path.is_symlink(): raise ValueError('Redirected diagnostic storage')
    marker = dict(schema=1, project='ngs_launcher', purpose='private-diagnostic-archives')
    owner = root / 'owner.json'
    if owner.is_symlink(): raise ValueError('Redirected diagnostic owner')
    if root.exists() and not owner.is_file() and any(root.iterdir()):
        raise ValueError('Foreign diagnostic storage')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if owner.exists():
        if owner.stat().st_size > 1024 or json.loads(owner.read_bytes()) != marker:
            raise ValueError('Foreign diagnostic owner')
    else:
        with owner.open('xb') as stream: stream.write(json.dumps(marker).encode())
    root.chmod(0o700)
    return dict(project='ngs_launcher', private_storage=True)


def diagnostic_action(payload, device):
    import base64, hashlib, io, json, os, re, time, zipfile
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import DbContainer, Search
    validate_diagnostic_request(payload)
    project, security = Project.get(), Environment.get_security()
    if (project.get_code() != 'ngs_launcher' or project.get_database_name() != 'ngs_launcher'
            or project.get_value('description') != 'NGS handheld game catalog; managed from D:/code/NGS'
            or security.is_admin() or not security.is_logged_in() or security.get_user_name() != 'ngs_catalog_worker'
            or not isinstance(device, str) or not re.fullmatch('[0-9a-f]{32}', device)):
        raise PermissionError('NGS diagnostic access required')
    query = Search('ngs_launcher/catalog_config'); query.add_filter('code', 'service')
    config = query.get_sobject()
    if not config or config.get_value('description') != 'NGS remote catalogizer v1':
        raise PermissionError('Diagnostic device not authorized')
    # Any enabled, signed-in player device may store its own bounded diagnostics.
    # Catalogizer membership is not required: handhelds signed in through a player
    # account were rejected here (HTTP 503) and never delivered crash evidence.
    query = Search('ngs_launcher/api_device'); query.add_filter('code', device)
    registered = query.get_sobject()
    if not registered or registered.get_value('enabled') is not True:
        raise PermissionError('Diagnostic device not authorized')
    DbContainer.get(project.get_project_db_resource()).do_query('SELECT pg_advisory_xact_lock(1313297224)')
    root = Path('/opt/tactic/tactic_data/ngs_launcher/diagnostics')
    for path in (root, *root.parents):
        if path.is_symlink(): raise ValueError('Redirected diagnostic storage')
    owner = root / 'owner.json'
    if (owner.is_symlink() or not owner.is_file() or owner.stat().st_size > 1024 or json.loads(owner.read_bytes()) !=
            dict(schema=1, project='ngs_launcher', purpose='private-diagnostic-archives')):
        raise ValueError('Diagnostic storage is not provisioned')
    folder = root / device
    if folder.is_symlink(): raise ValueError('Redirected diagnostic device')
    folder.mkdir(exist_ok=True, mode=0o700)
    name = payload['session'] + '-' + payload['sha256']
    marker, partial, complete = (folder / (name + suffix) for suffix in ('.json', '.part', '.zip'))
    for path in (marker, partial, complete):
        if path.is_symlink() or path.exists() and not path.is_file(): raise ValueError('Foreign diagnostic file')
    expected = dict(schema=1, device=device, session=payload['session'], sha256=payload['sha256'], size=payload['size'])
    if marker.exists():
        if marker.stat().st_size > 1024 or json.loads(marker.read_bytes()) != expected:
            raise ValueError('Diagnostic identity changed')
    elif partial.exists() or complete.exists(): raise ValueError('Unowned diagnostic file')
    if complete.exists():
        if complete.stat().st_size != payload['size']: raise ValueError('Diagnostic archive changed')
        return dict(offset=payload['size'], complete=True, sha256=payload['sha256'])
    if partial.exists() and partial.stat().st_size > payload['size']: raise ValueError('Oversized diagnostic upload')
    offset = partial.stat().st_size if partial.exists() else 0
    if payload['action'] == 'status': return dict(offset=offset, complete=False)
    if payload['action'] == 'append':
        raw = base64.b64decode(payload['data'], validate=True)
        if payload['offset'] > offset: raise ValueError('Diagnostic upload skipped bytes')
        if not marker.exists():
            # Stale, incomplete diagnostics are safe to retry from the local immutable archive.
            candidates = []
            for old in folder.glob('*.json'):
                if not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{64}\\.json', old.name) or old.is_symlink(): continue
                old_part, old_zip = old.with_suffix('.part'), old.with_suffix('.zip')
                if old_part.is_symlink() or old_part.exists() and not old_part.is_file() or old_zip.exists() or old.stat().st_size > 1024: continue
                saved = json.loads(old.read_bytes())
                if saved.get('device') == device and old.stem == saved.get('session', '') + '-' + saved.get('sha256', ''):
                    if old.stat().st_mtime < time.time() - 86400:
                        old_part.unlink(missing_ok=True); old.unlink()
                    else:
                        candidates.append((old.stat().st_mtime, old, old_part))
            for _, old, old_part in sorted(candidates)[:max(0, len(list(folder.glob('*.part'))) - 7)]:
                old_part.unlink(missing_ok=True); old.unlink()
            if len(list(folder.glob('*.part'))) >= 8: raise ValueError('Too many incomplete diagnostic archives')
            with marker.open('xb') as stream: stream.write(json.dumps(expected).encode())
        with partial.open('r+b' if partial.exists() else 'w+b') as stream:
            stream.seek(payload['offset'])
            if payload['offset'] < offset:
                if stream.read(len(raw)) != raw: raise ValueError('Repeated diagnostic bytes changed')
            else:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        return dict(offset=partial.stat().st_size, complete=False)
    if offset != payload['size'] or hashlib.sha256(partial.read_bytes()).hexdigest() != payload['sha256']:
        raise ValueError('Diagnostic archive SHA256 mismatch')
    with zipfile.ZipFile(io.BytesIO(partial.read_bytes())) as archive:
        entries = archive.infolist()
        if (len(entries) != 2 or {i.filename for i in entries} != {'events.jsonl', 'session.json'}
                or sum(i.file_size for i in entries) > 2 * 1024 * 1024 or any(i.flag_bits & 1 for i in entries)
                or archive.testzip() is not None): raise ValueError('Invalid diagnostic archive')
        if json.loads(archive.read('session.json')).get('session') != payload['session']:
            raise ValueError('Diagnostic session changed')
    os.replace(partial, complete)
    # Retention is confined to completed, marked archives owned by this device.
    files = sorted(folder.glob('*.zip'), key=lambda p: p.stat().st_mtime, reverse=True)
    for index, old in enumerate(files):
        if old == complete or index < 128 and old.stat().st_mtime >= time.time() - 14 * 86400: continue
        if not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{64}\\.zip', old.name) or old.is_symlink(): continue
        receipt = old.with_suffix('.json')
        if not receipt.is_file() or receipt.is_symlink() or receipt.stat().st_size > 1024: continue
        saved = json.loads(receipt.read_bytes())
        if saved.get('device') == device and old.stem == saved.get('session', '') + '-' + saved.get('sha256', ''):
            old.unlink(); receipt.unlink()
    return dict(offset=payload['size'], complete=True, sha256=payload['sha256'])


def read_latest_diagnostics(device):
    """Administrative retrieval stays on the existing TACTIC connection, never a public URL."""
    import base64, hashlib, json, re
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment
    project = Project.get()
    if (not Environment.get_security().is_admin() or project.get_code() != 'ngs_launcher'
            or project.get_database_name() != 'ngs_launcher'
            or project.get_value('description') != 'NGS handheld game catalog; managed from D:/code/NGS'
            or not isinstance(device, str) or not re.fullmatch('[0-9a-f]{32}', device)):
        raise PermissionError('NGS administrator required')
    root = Path('/opt/tactic/tactic_data/ngs_launcher/diagnostics')
    folder = root / device
    for path in (folder, root, *root.parents):
        if path.is_symlink(): raise ValueError('Redirected diagnostic storage')
    owner = root / 'owner.json'
    if (owner.is_symlink() or not owner.is_file() or owner.stat().st_size > 1024 or json.loads(owner.read_bytes()) !=
            dict(schema=1, project='ngs_launcher', purpose='private-diagnostic-archives')):
        raise ValueError('Foreign diagnostic storage')
    files = sorted(folder.glob('*.zip'), key=lambda p:p.stat().st_mtime, reverse=True)
    for path in files:
        if not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{64}\\.zip', path.name) or path.is_symlink() or not path.is_file(): continue
        marker = path.with_suffix('.json')
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 1024: continue
        saved = json.loads(marker.read_bytes())
        if saved.get('device') != device or path.stem != saved.get('session','') + '-' + saved.get('sha256',''): continue
        if not 0 < path.stat().st_size <= 128*1024 or saved.get('size') != path.stat().st_size: raise ValueError('Diagnostic archive changed')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != saved['sha256']: raise ValueError('Diagnostic hash changed')
        return dict(name=path.name, sha256=saved['sha256'], data=base64.b64encode(data).decode('ascii'))
    return None
