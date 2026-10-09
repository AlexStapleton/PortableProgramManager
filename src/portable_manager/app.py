from __future__ import annotations

import ctypes
import logging
import platform
import sys

from PySide6.QtWidgets import QApplication

from . import __version__
from .controller import AppController
from .logging_setup import configure_logging
from .single_instance import SingleInstance, default_instance_key
from .storage import Storage
from .ui.icons import make_window_icon
from .ui.main_window import MainWindow
from .ui.theme import APP_STYLESHEET
from .winutil import is_elevated, is_windows

log = logging.getLogger(__name__)

# Unique ID that tells Windows to group our taskbar button under its own
# identity rather than under the generic "python.exe" entry.  Must be set
# before QApplication is created so it takes effect on the first window.
_APP_USER_MODEL_ID = "PortableProgramManager.App"


def _set_app_user_model_id() -> None:
    if not is_windows():
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _APP_USER_MODEL_ID
        )
    except Exception:
        log.debug("Could not set AppUserModelID", exc_info=True)


def main() -> int:
    _set_app_user_model_id()

    # Storage first so logs can go to its base directory.
    storage = Storage()
    configure_logging(storage.base_dir)
    log.info(
        "Starting Portable Program Manager %s (Python %s, elevated=%s)",
        __version__,
        platform.python_version(),
        is_elevated(),
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Portable Program Manager")
    app.setApplicationVersion(__version__)
    # Set the icon on QApplication so every window (dialogs, etc.) inherits it
    # and the taskbar thumbnail uses the correct icon.
    app.setWindowIcon(make_window_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    # Keep the process alive when the main window is hidden to the system tray.
    app.setQuitOnLastWindowClosed(False)

    # Only one instance may own programs.json; a second launch wakes the first.
    instance = SingleInstance(default_instance_key())
    if not instance.try_acquire():
        log.info("Another instance is already running; asking it to activate")
        return 0
    app.aboutToQuit.connect(instance.release)

    controller = AppController(storage)
    window = MainWindow(controller)
    if is_elevated():
        window.setWindowTitle(window.windowTitle() + " (Administrator)")
    window.show()

    def _bring_to_front() -> None:
        window.showNormal()
        window.raise_()
        window.activateWindow()

    instance.activation_requested.connect(_bring_to_front)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
