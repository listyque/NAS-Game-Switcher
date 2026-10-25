"""Sign a verified portable package for GitHub Releases; never uploads private keys."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import zipfile
from ngs.update_crypto import RsaKey
from ngs.updates import archive_entries,validate_release,verify_envelope


def package_release(archive,private_key,notes,output):
    folder=archive.with_suffix('')
    version=json.loads((folder/'runtime/release.json').read_text())['version']
    report=json.loads((Path(__file__).resolve().parents[1]/'reports/windows-portable.json').read_text())
    if Path(report['folder']).resolve()!=folder.resolve() or len(report['runs'])<2 or not all(r.get('ok') for r in report['runs']):
        raise ValueError('Run the portable checks on this exact release first')
    with archive.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
    with zipfile.ZipFile(archive) as package:
        release=validate_release(dict(schema=1,version=version,platform='windows-x64',filename=archive.name,
            size=archive.stat().st_size,unpacked_size=sum(p.file_size for p in package.infolist()),sha256=sha,notes=notes))
        archive_entries(package,release)
    public=(folder/'runtime/assets/update-public-key.bin').read_bytes()
    payload=json.dumps(release,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    with RsaKey(private_key.read_bytes(),private=True) as key:
        if key.export()!=public:raise ValueError('The release key does not match the client authority')
        envelope=dict(payload=payload,signature=base64.b64encode(key.sign(payload.encode())).decode())
    verify_envelope(envelope,public)
    output.write_text(json.dumps(envelope,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    return release


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path);parser.add_argument('--private-key',type=Path,required=True)
    parser.add_argument('--notes',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(package_release(args.archive,args.private_key,args.notes.read_text(encoding='utf8'),args.output),indent=2))
