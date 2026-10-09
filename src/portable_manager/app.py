from __future__ import annotations

import ctypes
import sys

from PySide6.QtWidgets import QApplication

from .controller import AppController
from .storage import Storage
from .ui.icons import make_window_icon
from .ui.main_window import MainWindow
from .ui.theme import APP_STYLESHEET

# Unique ID that tells Windows to group our taskbar button under its own
# identity rather than under the generic "python.exe" entry.  Must be set
# before QApplication is created so it takes effect on the first window.
_APP_USER_MODEL_ID = "PortableProgramManager.App"


def main() -> int:
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _APP_USER_MODEL_ID
        )

    app = QApplication(sys.argv)
    app.setApplicationName("Portable Program Manager")
    # Set the icon on QApplication so every window (dialogs, etc.) inherits it
    # and the taskbar thumbnail uses the correct icon.
    app.setWindowIcon(make_window_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    # Keep the process alive when the main window is hidden to the system tray.
    app.setQuitOnLastWindowClosed(False)

    controller = AppController(Storage())
    window = MainWindow(controller)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
