"""Build the small public Docker deployment kit without user data."""
from datetime import datetime, timezone
import hashlib
import json
import re
import tarfile
from pathlib import Path
import zipfile

FOLDER = Path(__file__).resolve().parent


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def selected_files(root, suffixes):
    for path in sorted(root.rglob('*')):
        if '__pycache__' in path.parts or not path.is_file() or path.suffix not in suffixes:
            continue
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Link in deployment source')
        yield path


def create(out, entries):
    out.parent.mkdir(parents=True,exist_ok=True)
    staging = out.with_suffix('.building.zip')
    names = [name for name, _ in entries]
    assert len(names) == len(set(names)) and all(not n.startswith('/') and '..' not in n.split('/') for n in names)
    with zipfile.ZipFile(staging, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for name, value in entries:
            if isinstance(value, bytes):
                archive.writestr(name, value)
            else:
                if value.is_symlink() or not value.resolve().is_relative_to(FOLDER):
                    raise ValueError('Bundle input escapes server directory')
                archive.write(value, name)
    with zipfile.ZipFile(staging) as archive:
        assert archive.testzip() is None
    staging.replace(out)
    sha = digest(out)
    out.with_name(out.name+'.sha256').write_text(sha+'  '+out.name+'\n', encoding='ascii')
    return dict(file=str(out), bytes=out.stat().st_size, sha256=sha, entries=len(names), crc_verified=True)


def verify_images():
    with tarfile.open(FOLDER/'images/ngs-images.tar') as archive:
        exported={tag for image in json.load(archive.extractfile('manifest.json')) for tag in image.get('RepoTags',[])}
    required=set(re.findall(r'^    image: (\S+)$',(FOLDER/'compose.yaml').read_text(encoding='utf8'),re.MULTILINE))
    if not required or not required <= exported:
        raise ValueError('Offline image archive cannot resolve Compose images: '+str(sorted(required-exported)))
    return sorted(required)


def main():
    out=FOLDER.parent/'dist/server';out.mkdir(parents=True,exist_ok=True)
    files=[(name,FOLDER/name) for name in ['LICENSE','README.md','start.sh','compose.yaml','compose.sh','backup.sh','.env.example']]
    for directory in ['bootstrap','public']:
        files += [(p.relative_to(FOLDER).as_posix(),p) for p in sorted((FOLDER/directory).rglob('*')) if p.is_file()]
    files += [('client/index.html',FOLDER/'dsm/index.html')]
    manifest=json.loads((FOLDER/'bootstrap/manifest.json').read_text())
    assert manifest['personal_data'] is False and not manifest['files'] and set(manifest['hashes'])=={'install.py'}
    for directory in ['secrets','backups','empty-games','empty-installers']:
        files.append((directory+'/',b''))
    result=create(out/'NGS-Docker-0.1.0-preview.2.zip',[('NGS-Server/'+name,value) for name,value in files])
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':main()
