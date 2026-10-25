import json
import ctypes
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ngs.process import GameProcess


@unittest.skipUnless(sys.platform == "win32", "Windows process tracking")
class ProcessTests(unittest.TestCase):
    def test_foreground_does_not_activate_a_reused_parked_handle_outside_the_job(self):
        from unittest.mock import Mock
        process = GameProcess.__new__(GameProcess)
        process._lock = __import__('threading').RLock()
        process._parked_windows = {77: (7, False)}
        process.restore_windows = Mock(side_effect=process._parked_windows.clear)
        process.windows = Mock(return_value=[])
        process.members = Mock(return_value=[7])
        user = Mock()
        def reused(hwnd, pid):
            pid._obj.value = 99
        user.GetWindowThreadProcessId.side_effect = reused
        process.window_api = Mock(return_value=user)
        with self.assertRaisesRegex(OSError, 'Окно игры пока не найдено'):
            process.foreground()
        user.BringWindowToTop.assert_not_called()
        user.SetForegroundWindow.assert_not_called()

    def test_foreground_skips_disabled_owner_and_noactivate_helper_for_modal_dialog(self):
        from unittest.mock import Mock, patch
        process = GameProcess.__new__(GameProcess)
        process._lock = __import__('threading').RLock()
        process._parked_windows = {}
        process.restore_windows = Mock()
        process.windows = Mock(return_value=[(1, 7), (2, 7), (3, 7)])
        user = Mock()
        user.IsWindowEnabled.side_effect = lambda hwnd: hwnd != 1
        user.GetWindowLongW.side_effect = lambda hwnd, index: 0x08000000 if hwnd == 2 else 0
        user.GetWindow.return_value = 1
        user.GetWindowRect.return_value = False
        user.IsIconic.return_value = False
        user.IsWindowVisible.return_value = True
        user.SetForegroundWindow.return_value = True
        process.window_api = Mock(return_value=user)
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'):
            process.foreground()
        user.SetForegroundWindow.assert_called_once_with(3)
        self.assertEqual(process._foreground_window, (3, 7))

    def test_automatic_foreground_follows_main_window_without_stealing_manual_focus(self):
        from unittest.mock import Mock, patch
        process = GameProcess.__new__(GameProcess)
        process._lock = __import__('threading').RLock()
        process._parked_windows = {}
        process.restore_windows = Mock()
        process.windows = Mock(return_value=[(1, 7), (2, 8)])
        process._foregrounded = True
        process._foreground_window = (1, 7)
        user = Mock()
        user.IsWindowEnabled.return_value = True
        user.GetWindowLongW.return_value = 0
        user.GetWindow.return_value = 0
        user.IsIconic.return_value = False
        user.IsWindowVisible.return_value = True
        def bounds(hwnd, pointer):
            pointer._obj.right = 800 if hwnd == 2 else 200
            pointer._obj.bottom = 600 if hwnd == 2 else 100
            return True
        user.GetWindowRect.side_effect = bounds
        user.SetForegroundWindow.return_value = True
        process.window_api = Mock(return_value=user)
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'):
            user.GetForegroundWindow.return_value = 99
            self.assertFalse(process.foreground(automatic=True))
            user.SetForegroundWindow.assert_not_called()
            user.BringWindowToTop.assert_not_called()
            self.assertEqual(process._foreground_window, (1, 7))
            user.GetForegroundWindow.return_value = 1
            user.SetForegroundWindow.return_value = False
            with self.assertRaises(OSError): process.foreground(automatic=True)
            self.assertFalse(process._foregrounded)
            user.SetForegroundWindow.assert_called_once_with(2)
            user.SetForegroundWindow.reset_mock()
            user.SetForegroundWindow.return_value = True
            self.assertTrue(process.foreground(automatic=True))
            self.assertTrue(process._foregrounded)
            user.SetForegroundWindow.assert_called_once_with(2)
            self.assertEqual(process._foreground_window, (2, 8))
            process.windows.return_value.append((3, 7))
            user.GetForegroundWindow.return_value = 2
            self.assertTrue(process.foreground(automatic=True))
            user.SetForegroundWindow.assert_called_once()
            user.GetForegroundWindow.return_value = 99
            self.assertTrue(process.foreground())
            self.assertEqual(user.SetForegroundWindow.call_count, 2)

    def test_automatic_foreground_follows_replacement_after_splash_disappeared(self):
        from unittest.mock import Mock, patch
        process = GameProcess.__new__(GameProcess)
        process._lock = __import__('threading').RLock()
        process._parked_windows = {}
        process.restore_windows = Mock()
        process.windows = Mock(return_value=[(2, 8)])
        process._foregrounded = True
        process._foreground_window = (1, 7)
        user = Mock()
        user.IsWindowEnabled.return_value = True
        user.GetWindowLongW.return_value = 0
        user.GetWindow.return_value = 0
        user.GetWindowRect.return_value = False
        user.IsIconic.return_value = False
        user.IsWindowVisible.return_value = True
        user.GetForegroundWindow.return_value = 2
        user.SetForegroundWindow.return_value = True
        process.window_api = Mock(return_value=user)
        with patch('ngs.edge_swipe.disable_fullscreen_edge_gesture'):
            self.assertTrue(process.foreground(automatic=True))
        self.assertEqual(process._foreground_window, (2, 8))

    def test_trim_only_frozen_game_releases_working_set_and_keeps_contents(self):
        import hashlib
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            fixture = root / 'memory.py'
            fixture.write_text("import pathlib,time,hashlib\np=pathlib.Path('.')\ndata=bytearray(b'x'*(64*1024*1024))\n"
                "(p/'ready').write_text('yes')\nwhile not (p/'stop').exists(): time.sleep(.02)\n"
                "(p/'hash').write_text(hashlib.sha256(data).hexdigest())\n", encoding='utf8')
            process = GameProcess(Path(sys.executable), root, ['-B', str(fixture)])
            try:
                deadline = time.monotonic()+5
                while not (root/'ready').exists() and time.monotonic()<deadline: time.sleep(.02)
                self.assertTrue((root/'ready').exists())
                with self.assertRaises(OSError): process.trim_working_set()
                process.freeze()
                result = process.trim_working_set()
                self.assertTrue(process.frozen)
                self.assertGreaterEqual(result['processes'],1)
                self.assertEqual(result['failed'],0)
                self.assertGreater(result['before'],32*1024*1024)
                self.assertLess(result['after'],result['before'])
                (root/'stop').write_text('yes');process.thaw()
                deadline=time.monotonic()+5
                while process.active() and time.monotonic()<deadline: time.sleep(.02)
                self.assertEqual((root/'hash').read_text(),hashlib.sha256(b'x'*(64*1024*1024)).hexdigest())
            finally:
                process.thaw()
                if process.active(): process.terminate()
                deadline=time.monotonic()+3
                while process.active() and time.monotonic()<deadline: time.sleep(.02)
                process.close()

    def test_launch_arguments_roundtrip_windows_quotes_and_remain_literal(self):
        import subprocess
        from ngs.process import split_launch_arguments
        expected = ['-windowed', '-name', 'Игрок "один"', r'C:\Games\Test dir' + chr(92), '', '&', '|', '%TEMP%', '>file']
        self.assertEqual(split_launch_arguments(subprocess.list2cmdline(expected)), expected)
        self.assertEqual(split_launch_arguments('  \t '), [])
        for value in (None, '-x\0bad', '-x\n-y', 'x'*8193, 'a '*65, 'x'*2049):
            with self.subTest(value=str(value)[:20]), self.assertRaises(ValueError):
                split_launch_arguments(value)

    def test_custom_arguments_reach_real_process_without_a_shell(self):
        import subprocess
        from ngs.process import split_launch_arguments
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            fixture = root / 'argv.py'
            fixture.write_text("import json,sys,pathlib\npathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]),encoding='utf8')", encoding='utf8')
            expected = ['-windowed', '-name', 'Игрок один', r'C:\path with spaces' + chr(92), '&', '|', '%TEMP%', '>sentinel']
            process = GameProcess(Path(sys.executable), root, ['-B', str(fixture), *split_launch_arguments(subprocess.list2cmdline(expected))])
            try:
                deadline = time.monotonic() + 5
                while process.active() and time.monotonic() < deadline: time.sleep(.02)
                self.assertFalse(process.active())
                self.assertEqual(process.exit_code(), 0)
                self.assertEqual(json.loads((root / 'argv.json').read_text(encoding='utf8')), expected)
                self.assertFalse((root / 'sentinel').exists())
            finally: process.close()
            with self.assertRaisesRegex(ValueError, 'длинная строка'):
                GameProcess(Path(sys.executable), root, ['x' * 32767])

    def test_freeze_parks_only_owned_topmost_window_and_thaw_restores_it(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            fixture = root / "window.py"
            fixture.write_text('''
import ctypes,json,time
from ctypes import wintypes
from pathlib import Path
user=ctypes.WinDLL('user32',use_last_error=True)
user.CreateWindowExW.argtypes=[wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,
    ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p]
user.CreateWindowExW.restype=wintypes.HWND
user.PeekMessageW.argtypes=[ctypes.POINTER(wintypes.MSG),wintypes.HWND,wintypes.UINT,wintypes.UINT,wintypes.UINT]
user.TranslateMessage.argtypes=[ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.argtypes=[ctypes.POINTER(wintypes.MSG)]
user.DispatchMessageW.restype=ctypes.c_ssize_t
user.DestroyWindow.argtypes=[wintypes.HWND]
window=user.CreateWindowExW(0x08000088,'STATIC','NGS owned test window',0x90000000,-30000,-30000,320,200,None,None,None,None)
if not window: raise ctypes.WinError(ctypes.get_last_error())
Path('window.json').write_text(json.dumps({'hwnd':window}))
message=wintypes.MSG()
with Path('window.beat').open('ab',buffering=0) as beat:
    while not Path('window.stop').exists():
        while user.PeekMessageW(ctypes.byref(message),None,0,0,1):
            user.TranslateMessage(ctypes.byref(message));user.DispatchMessageW(ctypes.byref(message))
        beat.write(b'x');time.sleep(.01)
user.DestroyWindow(window)
''', encoding="utf8")
            process = GameProcess(Path(sys.executable), root, [str(fixture)])
            try:
                marker = root / "window.json"
                for _ in range(100):
                    if marker.exists() and (root / "window.beat").exists(): break
                    time.sleep(.02)
                hwnd = json.loads(marker.read_text())["hwnd"]
                user = process.window_api()
                self.assertTrue(user.IsWindowVisible(hwnd))
                process.park_windows()
                for _ in range(30):
                    if user.IsIconic(hwnd): break
                    time.sleep(.01)
                self.assertTrue(user.IsIconic(hwnd))
                self.assertFalse(process.frozen)
                process.restore_windows()
                for _ in range(30):
                    if not user.IsIconic(hwnd): break
                    time.sleep(.01)
                self.assertFalse(user.IsIconic(hwnd))
                process.freeze()
                self.assertTrue(process.frozen)
                for unused in range(20):
                    if user.IsIconic(hwnd) or not user.IsWindowVisible(hwnd): break
                    time.sleep(.01)
                self.assertTrue(user.IsIconic(hwnd) or not user.IsWindowVisible(hwnd))
                self.assertIn(hwnd, process._parked_windows)
                before = (root / "window.beat").stat().st_size
                process.park_windows()  # Repeated wake foreground attempts must not block a suspended thread.
                time.sleep(.08)
                self.assertEqual((root / "window.beat").stat().st_size, before)
                process.thaw()
                for _ in range(50):
                    if user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd): break
                    time.sleep(.02)
                self.assertFalse(user.IsIconic(hwnd))
                self.assertTrue(user.IsWindowVisible(hwnd))
                self.assertEqual(process._parked_windows, {})
                process.freeze()
                for unused in range(20):
                    if user.IsIconic(hwnd) or not user.IsWindowVisible(hwnd): break
                    time.sleep(.01)
                self.assertTrue(user.IsIconic(hwnd) or not user.IsWindowVisible(hwnd))
                process.close()  # Closing NGS also restores windows and releases the suspension.
                for _ in range(50):
                    if user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd): break
                    time.sleep(.02)
                self.assertFalse(user.IsIconic(hwnd))
                self.assertTrue(user.IsWindowVisible(hwnd))
            finally:
                if process.job:
                    process.api.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                    process.api.TerminateJobObject(process.job, 0)
                    import _winapi
                    _winapi.WaitForSingleObject(process.process, 5000)
                    process.close()
                else:
                    (root / "window.stop").touch()
                    time.sleep(.15)

    def test_freeze_tree_thaw_and_closing_state_handles_resume(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            beat = "import sys,time,pathlib\np=pathlib.Path(sys.argv[1]);f=p.open('ab',buffering=0)\nwhile not p.with_suffix('.stop').exists():\n f.write(b'x');time.sleep(.02)"
            parent = root / "parent.py"
            parent.write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c'," + repr(beat)
                              + ",sys.argv[1]])\n" + beat.replace("sys.argv[1]", "sys.argv[2]"), encoding="utf8")
            markers = [root / "child.beat", root / "parent.beat"]
            process = GameProcess(Path(sys.executable), root, [str(parent), *map(str, markers)])
            try:
                for unused in range(100):
                    if all(p.exists() and p.stat().st_size > 2 for p in markers):
                        break
                    time.sleep(.03)
                self.assertTrue(all(p.exists() for p in markers))
                from ngs.power import PowerNotifications
                monitor = PowerNotifications(process.freeze, lambda: None)
                try:
                    monitor.callback(None, 4, None)
                finally:
                    monitor.close()
                self.assertTrue(process.frozen)
                before = [p.stat().st_size for p in markers]
                process.freeze()  # Duplicate sleep notifications must not add another suspension.
                time.sleep(.15)
                self.assertEqual([p.stat().st_size for p in markers], before)
                process.thaw()
                time.sleep(.12)
                self.assertTrue(all(p.stat().st_size > old for p, old in zip(markers, before)))
                from unittest.mock import patch
                with patch.object(process, "members", side_effect=[process.members(), OSError("query failed")]):
                    with self.assertRaisesRegex(OSError, "query failed"):
                        process.freeze()
                self.assertFalse(process.frozen)
                before = [p.stat().st_size for p in markers]
                time.sleep(.1)
                self.assertTrue(all(p.stat().st_size > old for p, old in zip(markers, before)))
                process.freeze()
                before = [p.stat().st_size for p in markers]
                process.close()
                time.sleep(.12)
                self.assertTrue(all(p.stat().st_size > old for p, old in zip(markers, before)))
            finally:
                if process.job:
                    process.api.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                    process.api.TerminateJobObject(process.job, 0)
                    process.close()
                else:
                    # The test closed its job deliberately; open only the two known fixture processes.
                    for marker in markers:
                        marker.with_suffix('.stop').touch()
                time.sleep(.1)

    def test_short_lived_parent_keeps_child_tracked_and_quotes_arguments(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            marker = root / "child.json"
            child = "import json,sys,time;time.sleep(.5);open(sys.argv[1],'w').write(json.dumps(sys.argv[2:]))"
            parent = root / "parent.py"
            parent.write_text("import subprocess,sys\nsubprocess.Popen([sys.executable,'-c'," + repr(child)
                              + ",*sys.argv[1:]])\n", encoding="utf8")
            arguments = ["space and кириллица", 'a"b', "trailing\\", "& calc.exe"]
            process = GameProcess(Path(sys.executable), root, [str(parent), str(marker), *arguments])
            try:
                time.sleep(.15)
                self.assertTrue(process.active())
                for unused in range(100):
                    if not process.active():
                        break
                    time.sleep(.03)
                self.assertFalse(process.active())
                self.assertEqual(process.exit_code(), 0)
                self.assertEqual(json.loads(marker.read_text()), arguments)
            finally:
                process.close()

    def test_failed_process_creation_closes_job_and_reports_error(self):
        with self.assertRaises(OSError):
            GameProcess(Path("Z:/ngs-missing-game.exe"), Path.cwd(), [])

    def test_force_termination_kills_frozen_owned_tree_and_leaves_other_job_running(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            root = Path(folder)
            beat = "import time,pathlib,sys;p=pathlib.Path(sys.argv[1]);f=p.open('ab',buffering=0)\nwhile not p.with_suffix('.stop').exists():\n f.write(b'x');time.sleep(.02)"
            parent = root / 'parent.py'
            parent.write_text("import subprocess,sys\nsubprocess.Popen([sys.executable,'-c'," + repr(beat) + ",sys.argv[1]])\n" + beat.replace('sys.argv[1]','sys.argv[2]'),encoding='utf8')
            owned = GameProcess(Path(sys.executable),root,[str(parent),str(root/'child.beat'),str(root/'parent.beat')])
            other = GameProcess(Path(sys.executable),root,['-c',beat,str(root/'other.beat')])
            try:
                for _ in range(100):
                    if all((root/name).exists() for name in ('child.beat','parent.beat','other.beat')): break
                    time.sleep(.03)
                self.assertGreaterEqual(len(owned.members()),2)
                owned.freeze(); self.assertTrue(owned.frozen)
                before = (root/'other.beat').stat().st_size
                owned.terminate()
                for _ in range(100):
                    if not owned.active(): break
                    time.sleep(.02)
                self.assertFalse(owned.active())
                self.assertTrue(other.active())
                time.sleep(.08)
                self.assertGreater((root/'other.beat').stat().st_size,before)
            finally:
                for process in (owned,other):
                    if process.job:
                        process.terminate()
                        import _winapi
                        _winapi.WaitForSingleObject(process.process, 5000)
                        process.close()

    def test_windowless_background_helper_is_terminated_after_windowed_main_exits(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            helper = "import time,pathlib,sys;p=pathlib.Path(sys.argv[1]);p.write_text('up')\nwhile not p.with_suffix('.stop').exists(): time.sleep(.05)"
            main = root / "main.py"
            main.write_text('''
import ctypes,subprocess,sys,time
from ctypes import wintypes
from pathlib import Path
subprocess.Popen([sys.executable,'-c',''' + repr(helper) + ''',sys.argv[1]])
user=ctypes.WinDLL('user32',use_last_error=True)
user.CreateWindowExW.argtypes=[wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,
    ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p]
user.CreateWindowExW.restype=wintypes.HWND
user.DestroyWindow.argtypes=[wintypes.HWND]
window=user.CreateWindowExW(0x08000088,'STATIC','NGS main window',0x90000000,-30000,-30000,320,200,None,None,None,None)
Path('window.ready').write_text('yes')
while not Path('window.close').exists(): time.sleep(.02)
user.DestroyWindow(window)
''', encoding="utf8")
            process = GameProcess(Path(sys.executable), root, [str(main), str(root / "helper.up")])
            try:
                for _ in range(100):
                    if (root / "window.ready").exists() and (root / "helper.up").exists():
                        break
                    time.sleep(.02)
                self.assertTrue(process.active())
                self.assertTrue(process._had_windows)
                (root / "window.close").write_text("yes")
                for _ in range(40):
                    if not process._main_alive():
                        break
                    time.sleep(.02)
                # While remaining process is a normal game child (not a known crash/helper exe), active() stays True.
                time.sleep(1.6)
                self.assertTrue(process.active())
                with patch.object(process, "_member_image_name", return_value="unitycrashhandler64.exe"):
                    deadline = time.monotonic() + 5
                    while process.active() and time.monotonic() < deadline:
                        time.sleep(.05)
                self.assertFalse(process.active())
                self.assertEqual(process.members(), [])
                for _ in range(30):
                    try:
                        (root / "helper.up").unlink(missing_ok=True)
                        break
                    except PermissionError:
                        time.sleep(.02)
            finally:
                if process.job:
                    process.terminate()
                    process.close()


if __name__ == "__main__":
    unittest.main()
