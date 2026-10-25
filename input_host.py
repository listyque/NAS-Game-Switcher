"""Entry point for the signed, protected UIAccess component."""
import argparse
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def main(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--pipe', required=True)
    parser.add_argument('--parent', required=True, type=int)
    parser.add_argument('--data-root', required=True, type=Path)
    parser.add_argument('--portable', action='store_true')
    args = parser.parse_args(arguments)
    if not args.pipe.startswith('ngs-input-') or len(args.pipe) != 42 or args.parent <= 0 or not args.data_root.is_absolute():
        raise SystemExit('Invalid input host session')
    resources = Path(__file__).resolve().parent
    if '__compiled__' in globals():
        # UIAccess must never load QML or plugins from the caller's writable environment.
        for key in tuple(os.environ):
            if key.startswith(('QT_', 'QML_', 'QML2_', 'QSG_')): os.environ.pop(key, None)
        os.environ['QML_DISABLE_DISK_CACHE'] = '1'
        if not args.portable:
            from ctypes import wintypes
            import ctypes
            kernel, security = ctypes.WinDLL('kernel32'), ctypes.WinDLL('advapi32')
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            security.OpenProcessToken.argtypes = [wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
            security.GetTokenInformation.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            token, value, size = wintypes.HANDLE(), wintypes.DWORD(), wintypes.DWORD()
            if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)): return 1
            try:
                if not security.GetTokenInformation(token, 26, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)) or not value.value:
                    return 1
            finally: kernel.CloseHandle(token)
            if not resources.is_relative_to(Path(os.environ['ProgramFiles']) / 'NGS Input'): return 1
    from ngs.portable import PortableStorage
    storage = PortableStorage(args.data_root / 'data/input-host', resources=resources)
    storage.configure_process()
    if '__compiled__' in globals(): os.environ['QML_DISABLE_DISK_CACHE'] = '1'
    launcher_settings = {}
    try:
        import json
        settings_path = args.data_root / 'data/config/settings.json'
        if settings_path.is_file():
            loaded = json.loads(settings_path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                launcher_settings = loaded
    except Exception:
        pass
    if sys.platform == 'win32' and launcher_settings.get('disable_performance_overlays', True) is not False:
        try:
            import ctypes
            from ctypes import wintypes
            policy = wintypes.DWORD(1)
            ctypes.WinDLL('kernel32', use_last_error=True).SetProcessMitigationPolicy(
                6, ctypes.byref(policy), ctypes.sizeof(policy))
        except Exception:
            pass
    from ngs.diagnostics import set_production
    set_production(launcher_settings.get('production_mode') is True)
    import logging
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(storage.logs / 'input-host.log',maxBytes=512000,backupCount=2,encoding='utf8',delay=True)])
    from ngs import crash_capture
    crash_capture.enable(storage, 'launcher')
    logging.info('NGS Input start pid=%s parent=%s', os.getpid(), args.parent)
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(lambda kind,context,message: logging.warning('Qt: %s',message))
    from ngs.input_host import run_host
    return run_host(storage, args.pipe, args.parent, launcher_settings=launcher_settings)


if __name__ == '__main__':
    raise SystemExit(main())
