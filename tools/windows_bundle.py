"""Select real QML imports and their native dependencies, not the whole Qt SDK."""

import json
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path


def compile_qml(runtime, qt):
    from PySide6.QtCore import qVersion
    sources = sorted((Path(runtime) / "qml").rglob("*.qml"))
    if not sources:
        raise ValueError("No application QML files to compile")
    files = {}
    for source in sources:
        bytecode = source.with_suffix(".qmlc")
        subprocess.run([str(qt / "qmlcachegen.exe"), "--only-bytecode", "-o", str(bytecode), str(source)],
                       check=True, timeout=30)
        Path(str(bytecode) + ".aotstats").unlink(missing_ok=True)
        data = bytecode.read_bytes()
        if not data.startswith(b"qv4cdata"):
            raise ValueError("Invalid QML bytecode: " + str(bytecode))
        files[source.relative_to(runtime).as_posix()] = dict(
            source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            bytecode_sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
    return dict(qt_version=qVersion(), mode="bytecode", files=files)


def windows_toolchain():
    program_files = Path(os.environ["ProgramFiles(x86)"])
    vswhere = program_files / "Microsoft Visual Studio/Installer/vswhere.exe"
    installation = subprocess.check_output([str(vswhere), "-latest", "-products", "*",
        "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
        text=True).strip()
    versions = Path(installation) / "VC/Tools/MSVC"
    vc = max(versions.iterdir(), key=lambda p: tuple(map(int, p.name.split("."))))
    kit = program_files / "Windows Kits/10"
    sdk = max((p for p in (kit / "Include").iterdir() if (p / "um/windows.h").is_file()),
              key=lambda p: tuple(map(int, p.name.split("."))))
    binaries = vc / "bin/Hostx64/x64"
    env = dict(os.environ, INCLUDE=";".join(map(str, [vc / "include", sdk / "ucrt", sdk / "shared", sdk / "um"])),
               LIB=";".join(map(str, [vc / "lib/x64", kit / "Lib" / sdk.name / "ucrt/x64",
                                     kit / "Lib" / sdk.name / "um/x64"])))
    env["PATH"] = str(binaries) + os.pathsep + env["PATH"]
    return binaries, kit / "bin" / sdk.name / "x64/rc.exe", env


def build_bootstrap(repo, work, target, toolchain):
    binaries, rc, env = toolchain
    resource = work / "icon.rc"
    resource.write_text('1 ICON "' + (repo / "assets/ngs.ico").as_posix() + '"\n', encoding="utf8")
    subprocess.run([str(rc), "/nologo", "/fo", str(work / "icon.res"), str(resource)], env=env, check=True)
    subprocess.run([str(binaries / "cl.exe"), "/nologo", "/utf-8", "/O1", "/MT", "/guard:cf",
                   "/DUNICODE", "/D_UNICODE", str(repo / "tools/windows_bootstrap.c"),
                   "/Fo" + str(work / "bootstrap.obj"), "/Fe" + str(target / "NGS.exe"),
                   str(work / "icon.res"), "/link", "/SUBSYSTEM:WINDOWS", "/DYNAMICBASE", "/NXCOMPAT",
                   "/HIGHENTROPYVA", "/IMPLIB:" + str(work / "bootstrap.lib"), "user32.lib"], cwd=work, env=env, check=True)


def dependency_closure(seeds, available, imports):
    """Record an inclusion reason for every reachable native library."""
    reasons = {path: "entry/module/plugin" for path in seeds}
    pending = list(seeds)
    while pending:
        path = pending.pop()
        for name in imports(path):
            dependency = available.get(name.casefold())
            if dependency is not None and dependency not in reasons:
                reasons[dependency] = str(path)
                pending.append(dependency)
    return reasons


def select_runtime(built, repo, qt, dumpbin):
    scan = json.loads(subprocess.check_output([str(qt / "qmlimportscanner.exe"), "-rootPath", str(repo / "qml"),
                                              "-importPath", str(qt / "qml")], text=True))
    modules = {Path(item["relativePath"]) for item in scan if item.get("type") == 'module' and item.get("path")}
    if not {Path("QtQuick"), Path("QtQuick/Controls/Basic")}.issubset(modules):
        raise RuntimeError("The QML dependency scanner did not find the launcher imports")
    qml_root = built / "PySide6/qml"
    module_dirs = {p.parent.relative_to(qml_root) for p in qml_root.rglob("qmldir")}
    files, removed = [], []
    plugins = {"platforms/qwindows.dll", "imageformats/qjpeg.dll", "imageformats/qsvg.dll",
               "imageformats/qwebp.dll", "imageformats/qgif.dll", "imageformats/qico.dll",
               "iconengines/qsvgicon.dll", "tls/qschannelbackend.dll", "tls/qopensslbackend.dll",
               "tls/qcertonlybackend.dll"}
    for path in built.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(built)
        keep = True
        if path.is_relative_to(qml_root):
            part = path.relative_to(qml_root)
            owner = next((p for p in part.parents if p in module_dirs), None)
            keep = owner in modules and path.suffix not in (".qmltypes", ".metainfo", ".md") and "designer" not in part.parts
        elif relative.parts[:2] == ("PySide6", "qt-plugins"):
            keep = Path(*relative.parts[2:]).as_posix() in plugins
        elif relative.parts[0] == "shiboken6" and path.name.lower().startswith("msvcp140"):
            keep = not (built / path.name).is_file()
        if keep:
            files.append(relative)
        else:
            removed.append(relative)
    available = {p.name.casefold(): p for p in files if p.suffix.lower() in (".dll", ".pyd", ".exe")}
    if len(available) != sum(p.suffix.lower() in (".dll", ".pyd", ".exe") for p in files):
        raise ValueError("Ambiguous native dependency names")
    # CPython is loaded dynamically by Nuitka; QML/image/TLS plugins are loaded dynamically by Qt.
    seeds = [p for p in available.values() if p.suffix.lower() in (".pyd", ".exe")
             or p.parts[:2] in (("PySide6", "qml"), ("PySide6", "qt-plugins"))
             or p.name.casefold() == "python313.dll"]
    imports_report = {}
    def imports(relative):
        output = subprocess.check_output([str(dumpbin), "/nologo", "/dependents", str(built / relative)])
        names = re.findall(rb"^\s+([\w.+-]+\.(?:dll|pyd))\s*$", output, re.M | re.I)
        names = [name.decode("ascii") for name in names]
        for name in names:
            if name.casefold() not in available and not name.casefold().startswith(("api-ms-", "ext-ms-")):
                if not (Path(os.environ["SystemRoot"]) / "System32" / name).is_file():
                    raise ValueError("Missing native dependency: " + str(relative) + " -> " + name)
        imports_report[str(relative)] = names
        return names
    reasons = dependency_closure(seeds, available, imports)
    # Windows 11 uses Qt's D3D11 renderer; the 20 MB Mesa OpenGL software renderer is unused.
    selected = [p for p in files if p.suffix.lower() not in (".dll", ".pyd", ".exe") or p in reasons]
    removed.extend(p for p in files if p not in selected)
    return selected, dict(qml_modules=sorted(map(str, modules)),
        native_reasons={str(p): reason for p, reason in reasons.items()}, native_imports=imports_report,
        removed={str(p): (built / p).stat().st_size for p in removed})


def copy_runtime(built, target, selected):
    runtime = target / "runtime"
    for relative in selected:
        destination = runtime / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(built / relative, destination)
    return runtime


def copy_licenses(repo,runtime):
    import sys
    import PySide6
    package=Path(PySide6.__file__).parent
    licenses = runtime / "LICENSES"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(repo / "LICENSE", licenses / "NGS.txt")
    shutil.copytree(repo / "reference/qt-licenses", licenses / "Qt", dirs_exist_ok=True)
    shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python.txt")
    for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6"):
        source = package.parent / (name + "-" + PySide6.__version__ + ".dist-info") / "licenses"
        if source.is_dir():
            shutil.copytree(source, licenses / name, dirs_exist_ok=True)
    shutil.copyfile(repo / "assets/fonts/OFL.txt", licenses / "Manrope.txt")
    from importlib.metadata import distribution
    crypto = distribution("pycryptodome")
    crypto_license = next(entry for entry in crypto.files if entry.name in ("LICENSE.rst", "LICENSE.txt"))
    shutil.copyfile(crypto.locate_file(crypto_license), licenses / "PyCryptodome.txt")
    zstd = distribution("zstandard")
    for entry in zstd.files:
        if ".dist-info/licenses/" in str(entry).replace("\\", "/"):
            destination = licenses / "zstandard" / Path(str(entry).split("licenses/", 1)[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(zstd.locate_file(entry), destination)
    (licenses / "Qt.txt").write_text("Qt for Python: https://www.qt.io/licensing/\n"
        "Qt source and licenses: https://code.qt.io/cgit/qt/\n"
        "PySide source: https://code.qt.io/cgit/pyside/pyside-setup.git/\n", encoding="utf8")
