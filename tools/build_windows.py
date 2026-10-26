"""Build a clean standalone client; never copy working state or credentials."""

import json
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main():
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
        raise SystemExit("Use the validated release toolchain: Python 3.13 x64, MSVC and requirements-build.txt")
    from importlib.metadata import version
    if version("Nuitka") != "4.1.1" or version("PySide6") != "6.11.2":
        raise SystemExit("Install requirements-build.txt; Nuitka 4.3 crashes in this application")
    from ngs.portable import PortableStorage
    repo = Path(__file__).resolve().parents[1]
    (repo / "reports").mkdir(exist_ok=True)
    from tools.compile_translations import compile_catalog
    compile_catalog(repo)
    storage = PortableStorage(repo)
    storage.configure_process()
    os.environ["NUITKA_CACHE_DIR"] = str(storage.cache / "nuitka")
    os.environ["CLCACHE_DIR"] = str(storage.cache / "clcache")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = repo / "build" / ("windows-" + stamp)
    target = repo / "dist/windows" / ("NGS-windows-x64-" + stamp)
    if target.exists() or work.exists() or not work.resolve().is_relative_to(repo):
        raise SystemExit("A fresh build directory inside NGS is required")
    work.mkdir(parents=True)
    command = [sys.executable, "-B", "-m", "nuitka", "--mode=standalone", "--enable-plugin=pyside6",
               "--include-qt-plugins=qml", "--noinclude-qt-translations", "--msvc=latest", "--jobs=4",
               "--lto=no", "--windows-console-mode=disable", "--assume-yes-for-downloads",
               "--python-flag=isolated", "--python-flag=dont_write_bytecode", "--output-filename=NGS.exe",
               "--output-dir=" + str(work), "--report=" + str(work / "compilation.xml"),
               "--include-data-dir=" + str(repo / "qml") + "=qml",
               "--include-data-dir=" + str(repo / "assets") + "=assets"]
    command.extend(("--nofollow-import-to=PySide6.QtWidgets",
                    "--nofollow-import-to=concurrent.futures.process",
                    "--windows-icon-from-ico=" + str(repo / "assets/ngs.ico")))
    # Match Qt's deploy exclusions for modules absent from this application's QML imports.
    for module in ("WebEngine", "Quick3D", "Charts", "Graphs", "Sensors", "WebView", "QuickEffectMaker"):
        command.extend(("--noinclude-dlls=Qt6" + module + "*", "--noinclude-dlls=*/qml/Qt" + module + "/*"))
    command.append(str(repo / "launcher.py"))
    (work / "command.json").write_text(json.dumps(command, indent=2), encoding="utf8")
    subprocess.run(command, cwd=repo, check=True)
    built = work / "launcher.dist"
    if not (built / "NGS.exe").is_file():
        raise RuntimeError("Nuitka did not produce NGS.exe")
    import PySide6
    from tools.windows_bundle import windows_toolchain, select_runtime, copy_runtime, build_bootstrap, compile_qml
    package = Path(PySide6.__file__).parent
    toolchain = windows_toolchain()
    selected, audit = select_runtime(built, repo, package, toolchain[0] / "dumpbin.exe")
    runtime = copy_runtime(built, target, selected)
    audit["qml_compilation"] = compile_qml(runtime, package)
    shutil.copyfile(repo / "assets/update-public-key.bin", runtime / "assets/update-public-key.bin")
    (runtime / "release.json").write_text(json.dumps(dict(schema=1, version=datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y.%m.%d.%H%M%S"))), encoding="utf8")
    shutil.copyfile(repo / "tools/update.ps1", runtime / "update.ps1")
    # Device testing: one double-click packs logs, crash dumps and Windows fault events.
    for name in ("Collect-NgsCrash.cmd", "Collect-NgsCrash.ps1"):
        shutil.copyfile(repo / "tools" / name, runtime / name)
    input_packages = sorted(p for p in (repo / 'dist/input').glob('NGS-Input-*') if p.is_dir())
    if input_packages:
        input_package = input_packages[-1]
        installer = runtime / 'input-installer'
        shutil.copytree(input_package / 'payload', installer / 'payload')
        for name in ('Install.ps1','install_input_host.ps1','NGSInput.cat','publisher.cer'):
            shutil.copyfile(input_package / name, installer / name)
    build_bootstrap(repo, work, target, toolchain)
    audit["folder"] = str(target)
    (repo / "reports/windows-dependencies.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf8")
    (runtime / "READ-ME.txt").write_text(
        "NAS Game Switcher · Windows 11 x64\n\n"
        "Extract the whole folder to a writable location and run NGS.exe. Python is included.\n"
        "Keep the data folder: it contains your settings, library, queue and cache.\n"
        "Settings: F2 or the menu. Toggle NGS: F9. Language: Settings → Display.\n"
        "My games works without a NAS or account. Add game folders or scan installed store games.\n"
        "Covers, backgrounds and descriptions load directly from public game providers and stay cached offline.\n"
        "NAS is optional: Settings → Users and account → Connect a NAS server, then register or sign in.\n"
        "For local HTTP, copy its pairing code from the NAS administration logs. Use trusted HTTPS over the Internet.\n"
        "NAS downloads do not require SMB on the client. Updates come from the official GitHub repository.\n"
        "NGS Input is optional and requires Windows permission to install.\n\n"
        "Русский\n"
        "«Мои игры» работают без NAS и аккаунта. Добавьте папку игры или найдите установленные игры из магазинов.\n"
        "Обложки, фоны и описания загружаются напрямую из игровых источников и сохраняются для работы без сети.\n"
        "NAS подключается по желанию: Настройки → Пользователи и аккаунт → Подключить NAS.\n"
        "При переносе и обновлении сохраняйте папку data целиком.\n",
        encoding="utf-8-sig")
    from tools.windows_bundle import copy_licenses
    copy_licenses(repo,runtime)
    from tools.check_windows import check_build
    report = check_build(target, repo)
    (repo / "reports/windows-portable.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    archive = shutil.make_archive(str(target), "zip", target.parent, target.name)
    with open(archive, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    Path(archive + ".sha256").write_text(digest + "  " + Path(archive).name + "\n", encoding="utf8")
    print(json.dumps(dict(folder=str(target), archive=archive, sha256=digest), ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
