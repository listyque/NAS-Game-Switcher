"""Build a per-user installer from one verified portable release; preserve data."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return str(value).replace('$', '$$').replace('"', '$\\"')


def build(folder, compiler):
    folder = folder.resolve()
    if not folder.is_relative_to(ROOT / 'dist/windows') or not (folder / 'runtime/release.json').is_file():
        raise ValueError('A built portable release in dist/windows is required')
    files = sorted(p for p in folder.rglob('*') if p.is_file())
    if any(p.is_symlink() or p.is_junction() or 'data' in p.relative_to(folder).parts for p in files):
        raise ValueError('Linked files or user data in release')
    version = json.loads((folder / 'runtime/release.json').read_text())['version']
    work = ROOT / 'build' / ('installer-' + version)
    work.mkdir(parents=True, exist_ok=True)
    output = ROOT / 'dist/installer' / ('NGS-Setup-' + version + '.exe')
    output.parent.mkdir(parents=True, exist_ok=True)
    script = r'''
Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
Name "NAS Game Switcher"
OutFile "@OUTPUT@"
InstallDir "$PROFILE\Games\NGS"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NGS" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "@ICON@"
UninstallIcon "@ICON@"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "Russian"
!macro CheckPath PREFIX
Function ${PREFIX}CheckPath
  Exch $0
  loop:
    System::Call 'kernel32::GetFileAttributesW(w r0)i.r1'
    IntCmp $1 -1 parent
    IntOp $1 $1 & 0x400
    IntCmp $1 0 parent
    Abort
  parent:
    ${GetParent} "$0" $2
    StrCmp $2 "" done
    StrCmp $2 $0 done
    StrCpy $0 $2
    Goto loop
  done:
    Pop $0
FunctionEnd
!macroend
!insertmacro CheckPath ""
!insertmacro CheckPath "un."
Function .onVerifyInstDir
  Push $INSTDIR
  Call CheckPath
FunctionEnd
Section "NGS (required)" Main
  SectionIn RO
  Push $INSTDIR
  Call CheckPath
  SetShellVarContext current
  IfFileExists "$INSTDIR\NGS.exe" 0 unlocked
  ClearErrors
  FileOpen $0 "$INSTDIR\NGS.exe" a
  IfErrors locked
  FileClose $0
  Goto unlocked
  locked:
    MessageBox MB_OK|MB_ICONEXCLAMATION "Close NGS before installing an update." /SD IDOK
    Abort
  unlocked:
@FILES@
  Push "$INSTDIR\Uninstall.exe"
  Call CheckPath
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\NGS"
  CreateShortcut "$SMPROGRAMS\NGS\NGS.lnk" "$INSTDIR\NGS.exe"
  CreateShortcut "$DESKTOP\NGS.lnk" "$INSTDIR\NGS.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NGS" "DisplayName" "NAS Game Switcher"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NGS" "DisplayVersion" "@VERSION@"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NGS" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NGS" "InstallLocation" "$INSTDIR"
SectionEnd
Section /o "NGS Input over games (requires Windows permission)" Input
  IfSilent input_done
  ExecWait '$\"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe$\" -NoProfile -ExecutionPolicy Bypass -File $\"$INSTDIR\runtime\input-installer\Install.ps1$\" -DataRoot $\"$INSTDIR\data$\"' $0
  StrCmp $0 0 input_done
  MessageBox MB_OK|MB_ICONEXCLAMATION "NGS is installed. NGS Input was not installed; you can install it later in NGS Settings." /SD IDOK
  input_done:
SectionEnd
Section "Uninstall"
  SetShellVarContext current
@DELETE@
  Delete "$SMPROGRAMS\NGS\NGS.lnk"
  RMDir "$SMPROGRAMS\NGS"
  Delete "$DESKTOP\NGS.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NGS"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
'''
    install, remove = [], []
    previous = None
    for path in files:
        relative = path.relative_to(folder)
        parent = str(relative.parent)
        if parent != previous:
            install.append('  Push "$INSTDIR' + ('' if parent == '.' else '\\' + quote(parent)) + '"')
            install.append('  Call CheckPath')
            install.append('  SetOutPath "$INSTDIR' + ('' if parent == '.' else '\\' + quote(parent)) + '"')
            previous = parent
        install.extend(['  Push "$INSTDIR\\' + quote(relative) + '"', '  Call CheckPath',
                        '  File "' + quote(path) + '"'])
        remove.extend(['  Push "$INSTDIR\\' + quote(relative.parent) + '"',
                       '  Call un.CheckPath', '  Delete "$INSTDIR\\' + quote(relative) + '"'])
    directories = sorted({p.parent for p in files if p.parent != folder}, key=lambda p: len(p.parts), reverse=True)
    directories = {parent for p in directories for parent in [p, *p.parents] if parent != folder and parent.is_relative_to(folder)}
    remove.extend('  RMDir "$INSTDIR\\' + quote(p.relative_to(folder)) + '"' for p in sorted(directories, key=lambda p: len(p.parts), reverse=True))
    for key, value in dict(OUTPUT=quote(output), ICON=quote(ROOT/'assets/ngs.ico'), VERSION=version,
                           FILES='\n'.join(install), DELETE='\n'.join(remove)).items():
        script = script.replace('@'+key+'@', value)
    source = work/'NGS.nsi'
    source.write_text(script, encoding='utf-8-sig', newline='\n')
    subprocess.run([str(compiler), '/V2', str(source)], check=True)
    digest = hashlib.file_digest(output.open('rb'), 'sha256').hexdigest()
    output.with_suffix('.exe.sha256').write_text(digest+'  '+output.name+'\n', encoding='ascii')
    report = dict(file=str(output), version=version, sha256=digest, files=len(files),
                  data_preserved=True, input_opt_in=True, unsigned=True)
    (work/'verified.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps(report, indent=2), flush=True)
    return output


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('folder', type=Path)
    parser.add_argument('--compiler', type=Path, default=ROOT/'data/tools/nsis-3.13/nsis-3.13/makensis.exe')
    args=parser.parse_args()
    build(args.folder,args.compiler)
