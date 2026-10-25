import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.check_windows import check_build
from tools.windows_bundle import dependency_closure, compile_qml


class BuildChecks(unittest.TestCase):
    def test_qml_javascript_import_is_kept_without_being_a_qt_module(self):
        import json
        from tools.windows_bundle import select_runtime
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as name:
            root = Path(name)
            built = root / 'built'
            (built / 'qml').mkdir(parents=True)
            (built / 'NGS.exe').write_bytes(b'MZ')
            (built / 'qml/FocusNavigation.js').write_text('function next() {}')
            scan = [dict(type='javascript', path=str(built / 'qml/FocusNavigation.js'), name='FocusNavigation.js')]
            for module in ('QtQuick', 'QtQuick/Controls/Basic'):
                folder = built / 'PySide6/qml' / module
                folder.mkdir(parents=True)
                (folder / 'qmldir').write_text('module ' + module)
                scan.append(dict(type='module', path=str(folder), relativePath=module))
            def output(command, **kwargs):
                return json.dumps(scan) if '-rootPath' in command else b''
            with patch('tools.windows_bundle.subprocess.check_output', side_effect=output):
                selected, audit = select_runtime(built, root, root / 'qt', root / 'dumpbin.exe')
            self.assertIn(Path('qml/FocusNavigation.js'), selected)
            self.assertEqual(set(audit['qml_modules']), {str(Path('QtQuick')), str(Path('QtQuick/Controls/Basic'))})

    @unittest.skipUnless(sys.platform == "win32", "Windows release compiler")
    def test_precompiled_qml_loads_after_relocation_without_source_parsing(self):
        import PySide6
        qt = Path(PySide6.__file__).parent
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as name:
            root = Path(name)
            runtime = root / "original/runtime"
            (runtime / "qml").mkdir(parents=True)
            source = runtime / "qml/Main.qml"
            source.write_text('import QtQml\nQtObject { property int answer: 6 * 7 }', encoding="utf8")
            with patch.dict(os.environ, TEMP=str(root), TMP=str(root)):
                report = compile_qml(runtime, qt)
            self.assertEqual(report["mode"], "bytecode")
            self.assertEqual(list(report["files"]), ["qml/Main.qml"])
            self.assertEqual(list(runtime.rglob("*.aotstats")), [])
            source.write_text("Invalid QML source", encoding="utf8")
            moved = root / "Перенесённый NGS"
            (root / "original").rename(moved)
            code = """
import sys
from pathlib import Path
from ngs.portable import PortableStorage
storage=PortableStorage(Path(sys.argv[1]));storage.configure_process()
from PySide6.QtCore import QCoreApplication,QUrl
from PySide6.QtQml import QQmlEngine,QQmlComponent
app=QCoreApplication([]);engine=QQmlEngine()
c=QQmlComponent(engine,QUrl.fromLocalFile(str(storage.root/'runtime/qml/Main.qml')))
o=c.create()
sys.exit(0 if o is not None and o.property('answer')==42 else 43)
"""
            completed = subprocess.run([sys.executable, "-B", "-c", code, str(moved)], capture_output=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            (moved / "runtime/qml/Main.qmlc").unlink()
            completed = subprocess.run([sys.executable, "-B", "-c", code, str(moved)], capture_output=True, timeout=20)
            self.assertEqual(completed.returncode, 43, completed.stderr)

    @unittest.skipUnless(sys.platform == "win32", "Windows release compiler")
    def test_invalid_qml_stops_build_instead_of_shipping_uncompiled_source(self):
        import PySide6
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as name:
            root = Path(name)
            with self.assertRaisesRegex(ValueError, "No application QML"):
                compile_qml(root, Path(PySide6.__file__).parent)
            (root / "qml").mkdir()
            (root / "qml/Main.qml").write_text("Invalid QML source", encoding="utf8")
            with patch.dict(os.environ, TEMP=str(root), TMP=str(root)):
                with self.assertRaises(subprocess.CalledProcessError):
                    compile_qml(root, Path(PySide6.__file__).parent)

    def test_native_dependencies_follow_transitive_and_delayed_imports_with_cycles(self):
        graph = {"app.exe": ["Python313.DLL", "kernel32.dll"], "python313.dll": ["Qt6Core.dll"],
                 "qt6core.dll": ["python313.dll"], "unused.dll": []}
        result = dependency_closure(["app.exe"], {p: p for p in graph}, graph.__getitem__)
        self.assertEqual(set(result), {"app.exe", "python313.dll", "qt6core.dll"})
        self.assertEqual(result["qt6core.dll"], "python313.dll")

    def test_rejects_working_state_credentials_and_folders_outside_dist_before_launch(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as name:
            repo = Path(name)
            folder = repo / "dist/NGS"
            folder.mkdir(parents=True)
            (folder / "NGS.exe").write_bytes(b"Not an executable")
            with self.assertRaises(ValueError):
                check_build(repo, repo)
            for path in ("data/config/settings.json", "nested/api-device.json", ".env"):
                target = folder / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("private", encoding="utf8")
                with self.assertRaises(ValueError):
                    check_build(folder, repo)
                target.unlink()
                if (folder / "data/config").is_dir():
                    (folder / "data/config").rmdir()
                    (folder / "data").rmdir()


if __name__ == "__main__":
    unittest.main()
