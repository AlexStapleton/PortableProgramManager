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
