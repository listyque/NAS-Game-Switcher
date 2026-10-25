"""Called while this stack's app is stopped; no database credentials in output."""
from pathlib import Path
import sys
import tarfile

name = sys.argv[1]
if not name.isalnum():
    raise ValueError('Invalid backup name')
target = Path('/backups') / name
target.mkdir(mode=0o700, exist_ok=True)
with tarfile.open(target / 'assets-and-config.tar.gz', 'x:gz', compresslevel=1) as archive:
    for path in ['/opt/tactic/assets', '/opt/tactic/tactic_data', '/opt/tactic/projects']:
        archive.add(path, arcname=path.removeprefix('/opt/tactic/'))
print('Assets and private configuration backed up.')
