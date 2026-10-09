from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import pytest

from portable_manager.logging_setup import LOG_FILE_NAME, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """Remove handlers and restore hooks so tests do not leak state."""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    hooks_before = (sys.excepthook, threading.excepthook)
    yield
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level_before)
    sys.excepthook, threading.excepthook = hooks_before


def _file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def test_writes_log_file_in_directory(tmp_path):
    log_dir = tmp_path / "logs"

    path = configure_logging(log_dir)
    logging.getLogger("test.logging").info("hello from test")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert path == log_dir / LOG_FILE_NAME
    assert log_dir.is_dir()
    text = path.read_text(encoding="utf-8")
    assert "hello from test" in text
    assert "INFO" in text


def test_is_idempotent(tmp_path):
    configure_logging(tmp_path)
    count_after_first = len(logging.getLogger().handlers)
    file_handlers_after_first = len(_file_handlers())

    configure_logging(tmp_path)

    assert len(logging.getLogger().handlers) == count_after_first
    assert len(_file_handlers()) == file_handlers_after_first == 1


def test_unusable_path_does_not_raise(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "logs"

    path = configure_logging(target)

    assert path == target / LOG_FILE_NAME
    assert _file_handlers() == []
    assert any(isinstance(h, logging.NullHandler) for h in logging.getLogger().handlers)


def test_unusable_path_is_idempotent(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    configure_logging(blocker / "logs")
    count = len(logging.getLogger().handlers)
    configure_logging(blocker / "logs")

    assert len(logging.getLogger().handlers) == count


def test_excepthook_logs_and_chains_to_previous(tmp_path):
    calls = []
    sys.excepthook = lambda *args: calls.append(args)

    configure_logging(tmp_path)
    configure_logging(tmp_path)  # second call must not wrap the hook again
    error = ValueError("boom in main thread")
    sys.excepthook(ValueError, error, None)

    assert len(calls) == 1
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "CRITICAL" in text
    assert "boom in main thread" in text


def test_threading_excepthook_logs_and_chains_to_previous(tmp_path):
    calls = []
    threading.excepthook = lambda args: calls.append(args)

    configure_logging(tmp_path)
    args = SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=RuntimeError("boom in worker"),
        exc_traceback=None,
        thread=None,
    )
    threading.excepthook(args)

    assert calls == [args]
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "CRITICAL" in text
    assert "boom in worker" in text
