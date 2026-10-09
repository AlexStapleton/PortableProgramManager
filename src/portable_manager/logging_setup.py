"""File logging and uncaught-exception capture for the application."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "app.log"
_HANDLER_NAME = "portable-manager-file"
_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3

log = logging.getLogger(__name__)


def configure_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Send root-logger output to ``log_dir / app.log`` and capture uncaught exceptions.

    Safe to call more than once. If the log directory cannot be created or
    opened, logging falls back to a NullHandler instead of raising.

    Returns the path of the log file (even when the file could not be opened).
    """
    log_path = log_dir / LOG_FILE_NAME
    root = logging.getLogger()
    root.setLevel(level)

    if not any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        root.addHandler(_make_file_handler(log_dir, log_path))

    _install_excepthook()
    _install_threading_excepthook()
    return log_path


def _make_file_handler(log_dir: Path, log_path: Path) -> logging.Handler:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    except OSError:
        handler = logging.NullHandler()
    handler.set_name(_HANDLER_NAME)
    return handler


def _install_excepthook() -> None:
    if getattr(sys.excepthook, "_ppm_hook", False):
        return
    previous = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        # Ctrl+C is not a crash; leave it to the default handling.
        if not issubclass(exc_type, KeyboardInterrupt):
            log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        previous(exc_type, exc_value, exc_tb)

    _hook._ppm_hook = True  # type: ignore[attr-defined]
    sys.excepthook = _hook


def _install_threading_excepthook() -> None:
    if getattr(threading.excepthook, "_ppm_hook", False):
        return
    previous = threading.excepthook

    def _hook(args):  # type: ignore[no-untyped-def]
        if not issubclass(args.exc_type, SystemExit):
            thread_name = args.thread.name if args.thread else "unknown"
            log.critical(
                "Uncaught exception in thread %s",
                thread_name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        previous(args)

    _hook._ppm_hook = True  # type: ignore[attr-defined]
    threading.excepthook = _hook
