"""The interface the main window offers to the tabs and dialogs it hosts.

Tabs never run blocking work themselves: they hand a callable to
:meth:`TaskHost.start_task`, which runs it on a worker thread, shows progress
in the status bar, and calls ``on_result`` back on the UI thread.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QWidget

from ..controller import AppController


class TaskHost(Protocol):
    controller: AppController

    def start_task(
        self,
        fn: Callable[..., Any],
        *args: Any,
        start_message: str,
        on_result: Callable[[Any], None],
        error_prefix: str,
        lock_widgets: list[QWidget] | None = None,
        silent_errors: bool = False,
    ) -> None:
        """Run ``fn(*args, progress_callback=...)`` in the background.

        ``fn`` must accept a ``progress_callback`` keyword argument. Widgets in
        ``lock_widgets`` are disabled until the task finishes. Errors are shown
        in a message box (or only logged when ``silent_errors``).
        """

    def set_status(self, message: str) -> None:
        """Show a transient message in the status bar."""

    def warn(self, message: str) -> None:
        """Show a warning dialog (and keep the message in the status bar)."""

    def ui_state(self) -> QSettings:
        """Per-user store for UI layout (column widths, splitter sizes, last tab...)."""

    def refresh_programs(self) -> None:
        """Ask the Programs tab to reload from the controller (after installs etc.)."""
