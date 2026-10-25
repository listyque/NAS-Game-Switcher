"""Build the separate input windows. Signing is a distinct, explicit step."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime


def main():
    from importlib.metadata import version
    if sys.platform != 'win32' or sys.version_info[:2] != (3,13) or version('Nuitka') != '4.1.1' or version('PySide6') != '6.11.2':
        raise SystemExit('Use Python 3.13 and requirements-build.txt')
    from ngs.portable import PortableStorage
    from tools.windows_bundle import compile_qml, windows_toolchain, select_runtime
    import PySide6
    repo = Path(__file__).resolve().parents[1]
    from tools.compile_translations import compile_catalog
    compile_catalog(repo)
    storage = PortableStorage(repo); storage.configure_process()
    os.environ['NUITKA_CACHE_DIR'] = str(storage.cache / 'nuitka')
    os.environ['CLCACHE_DIR'] = str(storage.cache / 'clcache')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    work = repo / ('build/input-host-' + stamp); work.mkdir(parents=True)
    qml = work / 'qml'; qml.mkdir()
    for name in ('InputHost','KeyboardWindow','TouchpadWindow','KeyboardSurface','KeyboardGlyph','FluidButton','FluidSurface','FocusFrame',
                 'StatusBarWindow','EdgeSwipeWindow','SystemPanel','StatusIcon','MotionPopup','MotionTransition','FluidComboBox','ActivityIndicator'):
        shutil.copyfile(repo / ('qml/' + name + '.qml'), qml / (name + '.qml'))
    shutil.copyfile(repo / 'qml/FocusNavigation.js', qml / 'FocusNavigation.js')
    sources = [repo / 'input_host.py', *[repo / ('ngs/' + name + '.py') for name in
        ('input_host','virtual_keyboard','keyboard_layouts','gamepad','desktop','process','portable','appearance','touchpad','status_overlay','edge_swipe','system_status','system_controls','crash_capture','diagnostics','i18n')],
        *[repo / 'qml' / path.name for path in qml.iterdir()],
        repo / 'translations/ngs_ru.ts', repo / 'assets/i18n/ngs_ru.qm']
    hashes = {path.relative_to(repo).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    command = [sys.executable,'-B','-m','nuitka','--mode=standalone','--enable-plugin=pyside6',
               '--include-qt-plugins=qml','--noinclude-qt-translations','--msvc=latest','--jobs=4','--lto=no',
               '--windows-console-mode=disable','--windows-uac-uiaccess','--assume-yes-for-downloads',
               '--python-flag=isolated','--python-flag=dont_write_bytecode','--output-filename=NGSInput.exe',
               '--output-dir=' + str(work),'--report=' + str(work / 'compilation.xml'),
               '--include-data-dir=' + str(qml) + '=qml',
               '--include-data-files=' + str(repo / 'assets/i18n/ngs_ru.qm') + '=assets/i18n/ngs_ru.qm',
               '--include-data-files=' + str(repo / 'assets/fonts/Manrope.ttf') + '=assets/fonts/Manrope.ttf',
               '--nofollow-import-to=PySide6.QtWidgets', str(repo / 'input_host.py')]
    (work / 'command.json').write_text(json.dumps(command,indent=2),encoding='utf8')
    subprocess.run(command,cwd=repo,check=True)
    if any(hashlib.sha256(path.read_bytes()).hexdigest() != hashes[path.relative_to(repo).as_posix()] for path in sources):
        raise RuntimeError('Input sources changed during compilation; rebuild the component')
    package = repo / ('dist/input/NGS-Input-' + stamp)
    payload = package / 'payload'
    built = work / 'input_host.dist'
    selected, audit = select_runtime(built,work,Path(PySide6.__file__).parent,windows_toolchain()[0] / 'dumpbin.exe')
    for relative in selected:
        destination = payload / relative; destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(built / relative,destination)
    qml_report = compile_qml(payload,Path(PySide6.__file__).parent)
    from tools.windows_bundle import copy_licenses
    copy_licenses(repo,payload)
    (package / 'qml.json').write_text(json.dumps(qml_report,indent=2),encoding='utf8')
    (package / 'source.json').write_text(json.dumps(hashes,indent=2),encoding='utf8')
    (package / 'dependencies.json').write_text(json.dumps(audit,indent=2),encoding='utf8')
    for name in ('sign_input_host.ps1','install_input_host.ps1'):
        shutil.copyfile(repo / ('tools/' + name),package / name)
    (package / 'Install.ps1').write_text((repo / 'tools/launch_input_install.ps1').read_text(encoding='utf-8-sig'),encoding='utf-8-sig')
    shutil.copyfile(repo / 'tools/Install-Input.cmd',package / 'Install.cmd')
    (package / 'README.txt').write_text(
        'NGS Input · keyboard, touchpads and quick controls over games.\n'
        'Unsigned source builds cannot be installed as UIAccess. Sign the EXE and file catalog with your code-signing certificate.\n'
        'Official releases: run Install.cmd and approve Windows installation and the verified NGS publisher.\n'
        'Only the component in Program Files\\NGS Input is installed. Settings remain in the NGS data folder.\n'
        'Restart NGS after installation. The component connects automatically.\n'
        'Private signing keys are never included.\n',encoding='utf-8-sig')
    print(str(package),flush=True)


if __name__ == '__main__': main()
