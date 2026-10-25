"""Portable NGS entry point; configure writable paths before importing Qt."""

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

sys.dont_write_bytecode = True

from ngs.portable import PortableStorage


def main():
    if sys.argv[1:2] == ["--input-host"]:
        from input_host import main as input_main
        return input_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description="NGS portable launcher")
    parser.add_argument("--windowed", action="store_true", help="Development window; default is fullscreen")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--update-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--transfer-worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.transfer_worker:
        from ngs.transfer import worker_main
        return worker_main(args.transfer_worker)
    diagnostics = None
    try:
        resources = Path(__file__).resolve().parent
        root = resources.parent if "__compiled__" in globals() else resources
        storage = PortableStorage(root / "data/updates/probe" if args.update_probe else root, resources=resources)
        storage.configure_process()
        if sys.platform == "win32":
            try:
                if storage.read_settings().get("disable_performance_overlays", True) is not False:
                    import ctypes
                    from ctypes import wintypes
                    policy = wintypes.DWORD(1)
                    ctypes.WinDLL("kernel32", use_last_error=True).SetProcessMitigationPolicy(
                        6, ctypes.byref(policy), ctypes.sizeof(policy))
            except Exception:
                pass
        from ngs.diagnostics import set_production
        set_production(storage.read_settings().get('production_mode') is True)
        from ngs import crash_capture
        crash_capture.enable(storage, 'probe' if args.update_probe else 'self-test' if args.self_test else 'launcher')
        from ngs.diagnostics import RedactedFormatter, start
        logging.basicConfig(level=logging.INFO,
                            handlers=[RotatingFileHandler(storage.logs / "ngs.log", maxBytes=512_000,
                                                          backupCount=2, encoding="utf-8", delay=True)],
                            format="%(asctime)s %(levelname)s %(message)s")
        for handler in logging.getLogger().handlers:
            handler.setFormatter(RedactedFormatter('%(asctime)s %(levelname)s %(message)s'))
        diagnostics = start(storage, 'probe' if args.update_probe or args.self_test else 'launcher')
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(lambda kind, context, message: logging.warning("Qt: %s", message))
        from ngs.app import run
        code = run(storage, args.windowed, self_test=args.self_test, probe=args.update_probe)
        crash_capture.note('run() returned %r' % (code,))
        if code == 75 and "__compiled__" not in globals() and not (args.self_test or args.update_probe):
            import subprocess
            subprocess.Popen([sys.executable, "-B", *sys.argv], cwd=str(root))
            return 0
        return code
    except Exception as error:
        logging.exception("Launcher startup or shutdown failed")
        if args.self_test or args.update_probe:
            return 1
        message = "Could not start NGS.\n\n" + str(error) + "\n\nThe application folder must be writable."
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "NGS", 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)
        return 1
    finally:
        from ngs.diagnostics import _current
        if _current is not None: _current.finish()
        try:
            from ngs import crash_capture
            crash_capture.stop()
            crash_capture.note('launcher exit')
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
