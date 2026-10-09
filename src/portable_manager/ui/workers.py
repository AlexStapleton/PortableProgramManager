from __future__ import annotations

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
    # Strip anything that looks like a Bearer/token/Basic auth header value.
    import re
    msg = re.sub(r"(Bearer|token|Basic)\s+\S+", r"\1 [REDACTED]", msg, flags=re.IGNORECASE)
    return msg


class TaskRunner(QObject):
    def __init__(self, max_threads: int = 4) -> None:
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(2, max_threads))

    def start(self, worker: TaskWorker) -> None:
        self.pool.start(worker)
