import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from ngs import app
from ngs.portable import PortableStorage


class OverlaySettingsTests(unittest.TestCase):
    def check_environment(self, disabled, original):
        keys = ('VK_LOADER_LAYERS_DISABLE', 'DISABLE_VULKAN_OBS_CAPTURE', 'MANGOHUD',
                'NODEVICE_SELECT', 'DXGI_DEBUG_OSD', 'RTSSHooksCompatibility')
        inherited = {key: 'user-value' for key in keys} if original else {}
        with tempfile.TemporaryDirectory() as directory:
            storage = PortableStorage(directory)
            storage.write_settings(dict(disable_performance_overlays=disabled))
            kernel = Mock()
            with patch.dict(os.environ, dict(inherited, QT_QPA_PLATFORM='offscreen'), clear=True), \
                    patch.dict(app._SAVED_OVERLAY_ENV, {}, clear=True), \
                    patch.object(app.ctypes, 'WinDLL', return_value=kernel):
                before = dict(os.environ)
                app.configure_graphics_environment(storage)
                kernel.GetModuleHandleW.assert_not_called()
                kernel.GetProcAddress.assert_not_called()
                if disabled:
                    kernel.SetProcessMitigationPolicy.assert_called_once()
                    self.assertEqual(kernel.SetProcessMitigationPolicy.call_args.args[0], 6)
                    self.assertEqual(os.environ['RTSSHooksCompatibility'], '1')
                    self.assertEqual(os.environ['VK_LOADER_LAYERS_DISABLE'], '~all~')
                    launcher = dict(os.environ)
                    saved = app.clean_child_environment()
                    self.assertEqual(dict(os.environ), before)
                    app.restore_launcher_environment(saved)
                    self.assertEqual(dict(os.environ), launcher)
                else:
                    kernel.SetProcessMitigationPolicy.assert_not_called()
                    self.assertEqual(dict(os.environ), before)
                    self.assertIsNone(app.clean_child_environment())

    def test_input_host_never_changes_shared_rtss_flags(self):
        from ngs.input_host import run_host
        for disabled in (False, True):
            with self.subTest(disabled=disabled), \
                    patch.dict(os.environ, QT_QPA_PLATFORM='offscreen'), \
                    patch('PySide6.QtGui.QGuiApplication'), \
                    patch('ngs.i18n.configure'), \
                    patch('ngs.input_host.QLocalSocket') as socket, \
                    patch.object(app.ctypes, 'WinDLL') as windows_dll:
                socket.return_value.waitForConnected.return_value = False
                self.assertEqual(run_host(None, 'unused-pipe', 0,
                                          launcher_settings=dict(disable_performance_overlays=disabled)), 1)
                windows_dll.assert_not_called()

    def test_allowed_overlays_leave_user_environment_and_hooks_alone(self):
        for original in (False, True):
            with self.subTest(original=original):
                self.check_environment(False, original)

    def test_hidden_overlays_never_pass_ngs_restrictions_to_games(self):
        for original in (False, True):
            with self.subTest(original=original):
                self.check_environment(True, original)


if __name__ == '__main__':
    unittest.main()
