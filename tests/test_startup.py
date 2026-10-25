import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ngs import startup


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Мои программы NGS"
        self.root.mkdir()
        (self.root / "NGS.exe").write_bytes(b"fixture")
        self.registry = MagicMock()
        self.registry.REG_SZ = 1
        self.registry.QueryValueEx.side_effect = FileNotFoundError
        self.addCleanup(patch.stopall)
        patch.dict("sys.modules", winreg=self.registry).start()
        patch.object(startup.sys, "platform", "win32").start()

    def test_registers_quoted_portable_bootstrap_for_current_user_and_removes_only_it(self):
        self.assertFalse(startup.enabled(self.root))
        startup.set_enabled(self.root, True)
        expected = '"' + str(self.root / "NGS.exe") + '"'
        self.registry.CreateKeyEx.assert_called_once_with(
            self.registry.HKEY_CURRENT_USER, startup.RUN_KEY, 0, self.registry.KEY_SET_VALUE)
        self.registry.SetValueEx.assert_called_once_with(
            self.registry.CreateKeyEx.return_value.__enter__.return_value,
            startup.VALUE_NAME, 0, 1, expected)
        self.registry.QueryValueEx.side_effect = None
        self.registry.QueryValueEx.return_value = expected, 1
        self.assertTrue(startup.enabled(self.root))
        startup.set_enabled(self.root, False)
        self.registry.DeleteValue.assert_called_once_with(
            self.registry.OpenKey.return_value.__enter__.return_value, startup.VALUE_NAME)

    def test_disabling_does_not_delete_another_copy_or_missing_registration(self):
        startup.set_enabled(self.root, False)
        self.registry.QueryValueEx.side_effect = None
        self.registry.QueryValueEx.return_value = r'"D:\Other NGS\NGS.exe"', 1
        self.assertFalse(startup.enabled(self.root))
        startup.set_enabled(self.root, False)
        self.registry.DeleteValue.assert_not_called()
        self.registry.SetValueEx.assert_not_called()

    def test_refuses_missing_exe_and_long_command_without_writing_registry(self):
        with self.assertRaises(ValueError):
            startup.set_enabled(self.root / "missing", True)
        with patch.object(startup, "command", return_value="x" * 261):
            with self.assertRaises(ValueError):
                startup.set_enabled(self.root, True)
        self.registry.SetValueEx.assert_not_called()

    def test_access_denied_is_reported_and_non_windows_is_unsupported(self):
        self.registry.OpenKey.side_effect = PermissionError("denied")
        with self.assertRaises(PermissionError):
            startup.enabled(self.root)
        with patch.object(startup.sys, "platform", "linux"):
            self.assertFalse(startup.supported(self.root))
            self.assertFalse(startup.enabled(self.root))
            with self.assertRaises(ValueError):
                startup.set_enabled(self.root, True)
