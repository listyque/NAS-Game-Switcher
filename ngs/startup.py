"""Opt-in Windows logon registration; application data remains portable."""

import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "NGS.NASGameSwitcher"


def supported(root):
    return sys.platform == "win32" and (Path(root) / "NGS.exe").is_file()


def command(root):
    return '"' + str(Path(root).resolve() / "NGS.exe") + '"'


def enabled(root):
    if not supported(root):
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, kind = winreg.QueryValueEx(key, VALUE_NAME)
        return kind == winreg.REG_SZ and value == command(root)
    except FileNotFoundError:
        return False


def set_enabled(root, enable):
    if not supported(root):
        raise ValueError("Automatic startup is available in the Windows NGS.exe build.")
    import winreg
    if enable:
        value = command(root)
        if len(value.encode("utf-16-le")) // 2 > 260:
            raise ValueError("Move NGS to a shorter folder path (up to 258 characters) to enable automatic startup.")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
                value, kind = winreg.QueryValueEx(key, VALUE_NAME)
                # Another portable copy may own the registration now.
                if kind == winreg.REG_SZ and value == command(root):
                    winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
