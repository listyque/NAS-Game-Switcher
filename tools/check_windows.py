"""Run only the generated client, from a foreign CWD and a relocated clean copy."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def check_build(folder, repo, *, background=True):
    from ngs.portable import PortableStorage
    from tools.handheld_check import seed_catalog
    folder, repo = Path(folder).resolve(), Path(repo).resolve()
    if not folder.is_relative_to(repo / "dist") or not (folder / "NGS.exe").is_file():
        raise ValueError("Choose an NGS build inside dist")
    files = [p for p in folder.rglob("*") if p.is_file()]
    if (folder / "data").exists() or (folder / "tools").exists() or any(
            p.name in ("api-device.json", "update-signing-key.bin") or p.name.startswith(".env") for p in files):
        raise ValueError("The distribution contains working state or credentials")
    if {p.name for p in folder.iterdir()} != {"NGS.exe", "runtime"} or not (folder / "runtime/NGS.exe").is_file():
        raise ValueError("Expected only NGS.exe and runtime in the distribution root")
    for name in ("release.json", "update.ps1", "assets/update-public-key.bin"):
        if not (folder / "runtime" / name).is_file():
            raise ValueError("The updater is missing a required resource: " + name)
    qml_sources = sorted((folder / "runtime/qml").rglob("*.qml"))
    if not qml_sources or any(not source.with_suffix(".qmlc").is_file() or
                              not source.with_suffix(".qmlc").read_bytes().startswith(b"qv4cdata") for source in qml_sources):
        raise ValueError("Every application QML file must have precompiled bytecode")
    import ctypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ExtractIconExW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    shell.ExtractIconExW.restype = ctypes.c_uint
    icons = {name: shell.ExtractIconExW(str(folder / name), -1, None, None, 0)
             for name in ("NGS.exe", "runtime/NGS.exe")}
    if not all(0 < count < 0xffffffff for count in icons.values()):
        raise ValueError("Missing embedded executable icon")
    outer = PortableStorage(repo)
    outer.configure_process()
    result = dict(folder=str(folder), files=len(files), size_mib=round(sum(p.stat().st_size for p in files) / 1024**2, 1),
                  icon_groups=icons, precompiled_qml=[p.name for p in qml_sources], runs=[],
                  background=background, native_desktop_checked=not background)
    with tempfile.TemporaryDirectory(prefix="standalone-", dir=outer.temp) as temporary:
        base = Path(temporary)
        stage = base / "NGS portable"
        shutil.copytree(folder, stage)
        if background:
            import PySide6
            offscreen = Path(PySide6.__file__).parent / "plugins/platforms/qoffscreen.dll"
            shutil.copyfile(offscreen, stage / "runtime/PySide6/qt-plugins/platforms/qoffscreen.dll")
        # In this disposable copy, successful UI loading must come from shipped bytecode.
        for source in (stage / "runtime/qml").rglob("*.qml"):
            source.write_text("NGS self-test: invalid QML source", encoding="utf8")
        storage = PortableStorage(stage)
        settings = storage.read_settings()
        settings.update(tactic_url="http://127.0.0.1:9/tactic", games_directory="Custom games", hide_installed=True)
        storage.write_settings(settings)
        seed_catalog(repo, storage, 5)
        outside = base / "foreign working directory"
        outside.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "QT_", "QML_"))
               and key != "VIRTUAL_ENV"}
        env["PATH"] = os.environ["SystemRoot"] + "/System32"
        env["APPDATA"], env["LOCALAPPDATA"] = str(base / "profile/roaming"), str(base / "profile/local")
        if background:
            env.update(QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
        for run in range(2):
            if run:
                moved = base / "Перенесённый NGS"
                if not all(path.resolve().is_relative_to(outer.temp) for path in (stage, moved)):
                    raise ValueError("Relocation must stay inside the test directory")
                stage.rename(moved)
                stage = moved
            import ctypes
            # Native focus/tray checks require an awake display, including unattended builds.
            execution = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
            execution.argtypes = [ctypes.c_uint32]; execution.restype = ctypes.c_uint32
            previous_execution = None if background else execution(0x80000003)
            if not background and not previous_execution:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                command = [str(stage / "NGS.exe"), "--self-test"]
                with subprocess.Popen(command, cwd=outside, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as probe:
                    try:
                        stdout, stderr = probe.communicate(timeout=45)
                    except subprocess.TimeoutExpired:
                        subprocess.run(["taskkill", "/PID", str(probe.pid), "/T", "/F"], capture_output=True, timeout=10)
                        probe.communicate(timeout=10)
                        raise
                    completed = subprocess.CompletedProcess(command, probe.returncode, stdout, stderr)
            finally:
                if previous_execution is not None: execution(previous_execution)
            if completed.returncode:
                log = stage / "data/logs/ngs.log"
                if log.is_file():
                    shutil.copyfile(log, repo / "reports/windows-exe-failed.log")
                diagnostic = stage / "data/logs/portable-check.json"
                if diagnostic.is_file():
                    shutil.copyfile(diagnostic, repo / "reports/windows-exe-failed.json")
                (repo / "reports/windows-exe-stderr.txt").write_bytes(completed.stderr)
                (repo / "reports/windows-exe-stdout.txt").write_bytes(completed.stdout)
                raise RuntimeError("The EXE self-test failed (exit %s); see reports/windows-exe-failed.log" % completed.returncode)
            report = json.loads((stage / "data/logs/portable-check.json").read_text(encoding="utf8"))
            if not all(report.get(key) for key in ("ok", "compiled", "fullscreen", "scene_visible", "settings_opened",
                                                    "hide_installed", "static_background_for_game", "restored", "minimize_restored")):
                raise ValueError("The compiled UI did not pass its self-test")
            for value in (report["root"], report["data"], report["temp"], report["executable"],
                          report["resources"], *report["python_paths"], *report["modules"].values()):
                if not value or not Path(value).resolve().is_relative_to(stage):
                    raise ValueError("The client used a path outside its portable folder: " + str(value))
            if Path(report["root"]).resolve() != stage or Path(report["resources"]).resolve() != stage / "runtime":
                raise ValueError("The client confused its portable root and runtime resources")
            current = json.loads((stage / "data/config/settings.json").read_text(encoding="utf8"))
            if current["games_directory"] != "Custom games" or not current["hide_installed"]:
                raise ValueError("Settings were not preserved")
            for name in ("library.sqlite", "downloads.sqlite"):
                if not (stage / "data/state" / name).is_file():
                    raise ValueError("Missing local state: " + name)
            messages = (stage / "data/logs/ngs.log").read_text(encoding="utf8")
            if any(term in messages for term in ("ReferenceError", "TypeError", "Cannot assign", "Binding loop", "is not installed")):
                raise ValueError("Qt reported a runtime error")
            if list(outside.iterdir()) or any((base / "profile").rglob("*")):
                raise ValueError("The client wrote into the foreign CWD/profile trap")
            report["relocated"] = bool(run)
            report["qml_bytecode_loaded"] = True
            exit_log = stage / "data/logs/process-exit.jsonl"
            if exit_log.is_file():
                native_exit = json.loads(exit_log.read_text(encoding="utf8").splitlines()[-1])
                if native_exit["exit_code"] != 0 or native_exit["exit_hex"] != "0x00000000":
                    raise ValueError("The native bootstrap recorded an unsuccessful self-test")
                report["bootstrap_exit_recorded"] = True
            result["runs"].append(report)
            shutil.copyfile(stage / "data/logs/portable-check.png", repo / "reports" / ("windows-portable-%d.png" % run))
    result["limits"] = "Clean PATH/Python environment on the build PC; a separate clean Windows/handheld was not tested"
    if background:
        result["limits"] += "; offscreen software rendering with a test-only Qt plugin; native focus/tray/UIAccess were not checked"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    rendering = parser.add_mutually_exclusive_group()
    rendering.add_argument("--background", dest="background", action="store_true", default=True,
                           help="Offscreen checks without desktop windows or focus changes (default)")
    rendering.add_argument("--native", dest="background", action="store_false",
                           help="Native desktop checks; require the user's prior approval")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    result = check_build(args.folder, repo, background=args.background)
    report = repo / "reports/windows-portable.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
