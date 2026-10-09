"""Small Windows-specific helpers shared by the app entry point and subprocess calls."""

from __future__ import annotations

import ctypes
import sys

# Process-creation flag for subprocess calls made from the windowed (no-console)
# exe, so launching a console program does not flash a console window.
CREATE_NO_WINDOW: int = 0x08000000


def is_windows() -> bool:
    """Return True when running on Windows."""
    return sys.platform == "win32"


def is_elevated() -> bool:
    """Return True when the current process has administrator rights.

    Always False on non-Windows platforms, and False if the check itself fails.
    """
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _SHELLEXECUTEINFOW(ctypes.Structure):
    from ctypes import wintypes as _w

    _fields_ = [
        ("cbSize", _w.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", _w.HWND),
        ("lpVerb", _w.LPCWSTR),
        ("lpFile", _w.LPCWSTR),
        ("lpParameters", _w.LPCWSTR),
        ("lpDirectory", _w.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", _w.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", _w.LPCWSTR),
        ("hkeyClass", _w.HKEY),
        ("dwHotKey", _w.DWORD),
        ("hIconOrMonitor", _w.HANDLE),
        ("hProcess", _w.HANDLE),
    ]


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NO_CONSOLE = 0x00008000
_SW_HIDE = 0
_INFINITE = 0xFFFFFFFF


def run_elevated_and_wait(file: str, parameters: str, timeout_ms: int = _INFINITE) -> int:
    """Run *file* with administrator rights (UAC prompt), wait for it, return its exit code.

    Raises ``OSError`` (``winerror`` 1223) if the user declines the prompt.
    """
    from ctypes import wintypes

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    info.lpFile = file
    info.lpParameters = parameters
    info.nShow = _SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)
    if not info.hProcess:
        return 0
    try:
        kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)
