"""DSM SPK using Synology's Docker Project Worker; no privileged installer scripts."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import zipfile

SERVER=Path(__file__).resolve().parent
ROOT=SERVER.parent
DSM=SERVER/'dsm'
BUILD=ROOT/'build/dsm'


def digest(path, algorithm='sha256'):
    h=hashlib.new(algorithm)
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def preinstall_source(checksums):
    text=(SERVER/'compose.yaml').read_text(encoding='utf-8')
    text=text.replace('name: ngs-server','name: ngs-server-package')
    # DSM kernels can omit CFS quotas even when the CPU cgroup is mounted.
    text='\n'.join(line for line in text.split('\n') if not line.startswith('    cpus:'))
    # DSM may default to its package UID; both image entrypoints initialize as container root.
    text=text.replace('    restart: unless-stopped\n','    user: "0:0"\n    restart: unless-stopped\n')
    text=text.replace('      - ./client:/opt/ngs-client:ro','      - __NGS_CLIENT_MOUNT__')
    text=text.replace("'${NGS_BIND_IP:-127.0.0.1}:${NGS_PORT:-8890}:8081'", '__NGS_PUBLISHED_PORT__')
    substitutions={'__NGS_PUBLISHED_PORT__':'"$bind_ip:$port:8081"'}
    for name,remote in [('caddy','/data'),('caddy-config','/config'),('postgres','/var/lib/postgresql/data'),('tactic','/opt/tactic/tactic_data'),
            ('assets','/opt/tactic/assets'),('projects','/opt/tactic/projects')]:
        substitutions['./data/'+name+':'+remote]='"$dest/project/state/data/'+name+':'+remote+'"'
    substitutions.update({'./bootstrap:/seed:ro':'"$dest/bootstrap:/seed:ro"','./backups:/backups':'"$dest/project/state/backups:/backups"',
        '${NGS_GAMES_PATH:-./empty-games}':'"$games"','${NGS_INSTALLERS_PATH:-./empty-installers}':'"$installers"',
        './secrets/postgres_password':'"$dest/project/state/secrets/postgres_password"',
        './secrets/tactic_admin_password':'"$dest/project/state/secrets/tactic_admin_password"',
        './secrets/ngs_proxy_token':'"$dest/project/state/secrets/ngs_proxy_token"',
        './public:/etc/ngs-public:ro':'"$dest/public:/etc/ngs-public:ro"',
        '__NGS_CLIENT_MOUNT__':'"$dest/client:/opt/ngs-client:ro"'})
    expressions={}
    for i,(old,value) in enumerate(substitutions.items()):
        assert old in text,old
        token='__NGS_VALUE_'+str(i)+'__';text=text.replace(old,token)
        expressions[token]='$(yaml_quote '+value+')'
    text=text.replace('\\','\\\\').replace('$','\\$').replace('`','\\`')
    for token,value in expressions.items():text=text.replace(token,value)
    # Native worker discovery predates Compose's shorter compose.yaml filename.
    code='cat > "$stage/project/docker-compose.yaml" <<NGS_COMPOSE_EOF\n'+text+'NGS_COMPOSE_EOF'
    source=(DSM/'scripts/preinst').read_text(encoding='utf-8')
    return source.replace('__NGS_PREPARED_SHA256__',checksums).replace('# COMPOSE_FROM_CANONICAL_SOURCE',code)


def postinstall_source():
    return (DSM/'scripts/postinst').read_text(encoding='utf-8')


def add(archive, name, value, executable=False):
    info=tarfile.TarInfo(name);info.mode=0o755 if executable else 0o644
    if name in {'migration/ngs_launcher.sql','migration/shared.json'}:info.mode=0o600
    info.uid=info.gid=0;info.uname=info.gname='root';info.mtime=0
    if isinstance(value,bytes):info.size=len(value);archive.addfile(info,io.BytesIO(value))
    else:
        if value.is_symlink():raise ValueError('Linked SPK input')
        info.size=value.stat().st_size
        with value.open('rb') as stream:archive.addfile(info,stream)


def build():
    from server.package_bundle import verify_images
    verify_images()
    BUILD.mkdir(parents=True,exist_ok=True); (ROOT/'dist/dsm').mkdir(parents=True,exist_ok=True)
    (ROOT/'reports').mkdir(exist_ok=True)
    seed=SERVER/'bootstrap'
    manifest=json.loads((seed/'manifest.json').read_text(encoding='utf-8'))
    image=BUILD/'ngs-images.tar.gz'; fingerprint=digest(SERVER/'images/ngs-images.tar')
    marker=image.with_suffix('.sha256')
    if not image.exists() or not marker.exists() or marker.read_text()!=fingerprint:
        with (SERVER/'images/ngs-images.tar').open('rb') as src,image.open('wb') as out:
            with gzip.GzipFile(fileobj=out,mode='wb',compresslevel=6,mtime=0) as compressed:shutil.copyfileobj(src,compressed,1024*1024)
        marker.write_text(fingerprint)
    payload=BUILD/'package.tgz'
    entries=[('images/ngs-images.tar.gz',image),('images/SHA256SUMS',(digest(image)+'  images/ngs-images.tar.gz\n').encode()),
        ('README.txt',SERVER/'README.md'),('client/index.html',DSM/'index.html')]
    entries += [('public/'+p.name,p) for p in sorted((SERVER/'public').iterdir()) if p.is_file()]
    entries += [('ui/'+p.relative_to(DSM/'ui').as_posix(),p) for p in sorted((DSM/'ui').rglob('*')) if p.is_file()]
    for name in ['manifest.json',*manifest['hashes']]:
        if name!='manifest.json':assert digest(seed/name)==manifest['hashes'][name]
        entries.append(('bootstrap/'+name,seed/name))
    assert not manifest['files'] and manifest['personal_data'] is False
    with payload.open('wb') as output,gzip.GzipFile(fileobj=output,mode='wb',compresslevel=6,mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w|',format=tarfile.GNU_FORMAT) as tar:
            for name,value in entries:add(tar,name,value)
    checksums=digest(seed/'manifest.json')+'  bootstrap/manifest.json\n'+digest(image)+'  images/ngs-images.tar.gz'
    info={'package':'NGSServer','displayname':'NAS Game Switcher',
        'version':'0.1.0-1','os_min_ver':'7.2.1-69057','arch':'noarch','maintainer':'NGS',
        'description':'Preview NGS server for x86-64 NAS: TACTIC 5, PostgreSQL and automatic HTTPS. Independent empty database; your game folders stay read-only.',
        'install_dep_packages':'ContainerManager>=20.10.23-1437','dsmuidir':'ui','dsmappname':'com.ngs.Server',
        'checkport':'no','ctl_stop':'yes','silent_install':'no','silent_upgrade':'no',
        'silent_uninstall':'yes','extractsize':'3145728','beta':'yes','checksum':digest(payload,'md5')}
    target=ROOT/'dist/dsm/NGS-Synology-0.1.0-preview.1.spk'
    pending=target.with_suffix('.building.spk')
    with tarfile.open(pending,'w',format=tarfile.GNU_FORMAT) as tar:
        add(tar,'INFO',''.join(k+'='+json.dumps(v,ensure_ascii=False)+'\n' for k,v in info.items()).encode())
        add(tar,'package.tgz',payload)
        for directory in ['conf','scripts','WIZARD_UIFILES']:
            for p in sorted((DSM/directory).iterdir()):
                value=(preinstall_source(checksums) if p.name=='preinst' else p.read_text(encoding='utf-8').replace('__NGS_PUBLIC_BOOTSTRAP__',digest(seed/'manifest.json'))).encode()
                add(tar,directory+'/'+p.name,value,directory=='scripts')
        for name in ['LICENSE','PACKAGE_ICON.PNG','PACKAGE_ICON_256.PNG']:add(tar,name,DSM/name)
    with tarfile.open(pending) as tar:
        names=tar.getnames();assert 'conf/resource' in names and 'WIZARD_UIFILES/install_uifile' in names
        assert not any('/secrets/' in n or n.startswith('/') or '..' in n.split('/') for n in names)
        assert hashlib.sha256(tar.extractfile('package.tgz').read()).hexdigest()==digest(payload)
    pending.replace(target)
    sha=digest(target);target.with_suffix('.spk.sha256').write_text(sha+'  '+target.name+'\n')
    return dict(path=str(target),bytes=target.stat().st_size,sha256=sha,kind='public',
        package='NGSServer',docker_project='ngs-server-package',runtime_user='package',seed_files=len(manifest['files']))


if __name__=='__main__':
    from PySide6.QtGui import QImage,QPainter
    from PySide6.QtSvg import QSvgRenderer
    icon=QSvgRenderer(str(ROOT/'assets/ngs.svg'))
    assert icon.isValid()
    for size,name in [(64,'PACKAGE_ICON.PNG'),(256,'PACKAGE_ICON_256.PNG')]:
        canvas=QImage(size,size,QImage.Format.Format_ARGB32_Premultiplied);canvas.fill(0)
        painter=QPainter(canvas);icon.render(painter);painter.end()
        assert canvas.save(str(DSM/name))
    (DSM/'ui/images').mkdir(exist_ok=True)
    for size in [16,24,32,48,64,72,256]:
        canvas=QImage(size,size,QImage.Format.Format_ARGB32_Premultiplied);canvas.fill(0)
        painter=QPainter(canvas);icon.render(painter);painter.end()
        assert canvas.save(str(DSM/'ui/images'/('ngs-'+str(size)+'.png')))
    template=(DSM/'ui/config.template').read_text(encoding='utf-8')
    (DSM/'ui/config').write_bytes(template.replace('__NGS_PORT__','8890').encode())
    result=dict(packages=[build()],dsm_device_tested=False)
    (ROOT/'reports/dsm-package-build.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)
