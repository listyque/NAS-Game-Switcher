import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools.windows_bundle import select_runtime


class RuntimeSelectionTests(unittest.TestCase):
    def test_basic_only_application_does_not_ship_optional_style_modules_or_pngs(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder); built = root/'built'; source = root/'source'
            (source/'qml').mkdir(parents=True)
            main = source/'qml/Main.qml'; main.write_text('import QtQuick.Controls.Basic\nItem {}')
            modules = ['QtQuick', 'QtQuick/Controls', 'QtQuick/Controls/Basic', 'QtQuick/Controls/Windows',
                       'QtQuick/Controls/FluentWinUI3', 'QtQuick/Controls/FluentWinUI3/impl', 'QtQuick/NativeStyle']
            scan = [dict(type='module', relativePath=name, path='/qt/'+name) for name in modules]
            for name in modules:
                path = built/'PySide6/qml'/name; path.mkdir(parents=True, exist_ok=True)
                (path/'qmldir').write_text('module '+name.replace('/', '.'))
                (path/'control.qml').write_text('Item {}')
                if name.endswith(('Windows', 'FluentWinUI3')): (path/'unused.png').write_bytes(b'fixture')
            (built/'NGS.exe').write_bytes(b'fixture')
            def run(command, **kwargs):
                return json.dumps(scan) if 'qmlimportscanner.exe' in command[0] else b''
            with patch('tools.windows_bundle.subprocess.check_output', side_effect=run):
                selected, report = select_runtime(built, source, root/'qt', root/'dumpbin.exe')
                self.assertFalse(any(p.suffix=='.png' for p in selected))
                self.assertIn(Path('PySide6/qml/QtQuick/Controls/Basic/control.qml'), selected)
                self.assertIn(Path('PySide6/qml/QtQuick/NativeStyle/control.qml'), selected)
                self.assertTrue(any('FluentWinUI3' in p for p in report['removed']))
                main.write_text('import QtQuick.Controls.Basic\nimport QtQuick.Controls.Windows\nItem {}')
                selected, _ = select_runtime(built, source, root/'qt', root/'dumpbin.exe')
                self.assertTrue(any(p.suffix=='.png' and 'Windows' in p.parts for p in selected))
