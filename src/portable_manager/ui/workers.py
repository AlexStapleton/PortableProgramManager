from __future__ import annotations

import re
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class WorkerSignals(QObject):
    started = Signal()
    finished = Signal()
    result = Signal(object)
    error = Signal(str)
    progress = Signal(int, str)


class TaskWorker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        self.signals.started.emit()
        try:
            self.kwargs["progress_callback"] = self.signals.progress.emit
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # pragma: no cover - signal boundary
            self.signals.error.emit(_sanitize_error(exc))
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


def _sanitize_error(exc: Exception) -> str:
    """Convert an exception to a user-facing string, stripping sensitive data.

    ``requests`` exceptions may carry a ``PreparedRequest`` whose headers
    include the ``Authorization`` token.  We redact that before display.
    """
    msg = str(exc)
    # Redact auth header values and anything shaped like a GitHub token, but
    # leave ordinary prose ("Set a GitHub token in Settings") alone.
    msg = re.sub(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 [REDACTED]", msg)
    msg = re.sub(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", "[REDACTED]", msg)
    return msg


class TaskRunner(QObject):
    def __init__(self, max_threads: int = 4) -> None:
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(2, max_threads))

    def start(self, worker: TaskWorker) -> None:
        self.pool.start(worker)
