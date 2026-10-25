"""Initialize the data-free public NGS project once."""
import hashlib
import os
from pathlib import Path
from runtime_update import verify_seed

def main():
    seed=Path('/seed');data=Path('/opt/tactic/tactic_data')
    if verify_seed(seed,data):return
    import tacticenv
    from pyasm.security import Batch
    Batch()
    exec(compile((seed/'install.py').read_text(encoding='utf-8'),'ngs-public-bootstrap','exec'),{})
    assets=Path('/opt/tactic/assets/ngs_launcher')
    for path in (assets,*assets.rglob('*')):
        os.chown(path,10001,10001)
        os.chmod(path,0o755 if path.is_dir() else 0o644)
    marker=data/'.ngs-migrated'
    marker.write_text(hashlib.sha256((seed/'manifest.json').read_bytes()).hexdigest()+'\n')
    os.chown(marker,10001,10001);os.chmod(marker,0o600)
    print('Clean NGS project initialized with new independent keys.',flush=True)

if __name__=='__main__':main()
