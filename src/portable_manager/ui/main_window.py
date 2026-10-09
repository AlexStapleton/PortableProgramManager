"""Main window: hosts the Programs and Discover tabs, runs background tasks,
owns the tray icon and the scheduled update checks."""

from __future__ import annotations

import logging
import os
from functools import partial
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QStatusBar,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QToolBar,
    QWidget,
)

from ..controller import AppController, UpdateCheckReport
from . import theme
from .discover_tab import DiscoverTab
from .icons import make_tray_icon
from .programs_tab import ProgramsTab
from .settings_dialog import SettingsDialog
from .workers import TaskRunner, TaskWorker

log = logging.getLogger(__name__)

_UPDATE_TIMER_MS = 30 * 60 * 1000
_TRAY_LAUNCH_LIMIT = 15


class MainWindow(QMainWindow):
    """The application shell. Implements :class:`ui.host.TaskHost` for its tabs."""

    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self.controller = controller
        self.runner = TaskRunner(max_threads=4)
        self._active_jobs = 0
        self._workers: list[TaskWorker] = []  # Keep workers alive until Qt is done with them
        self._quitting = False          # set True before QApplication.quit() to bypass close-to-tray
        self._tray_notified = False     # show balloon notification only on first tray hide
        self._ui_state = QSettings("PortableProgramManager", "ui")

        self.setWindowTitle("Portable Program Manager")
        self.setMinimumSize(760, 520)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._build_status_widgets()

        self.programs_tab = ProgramsTab(self)
        self.discover_tab = DiscoverTab(self)
        self.programs_tab.discover_requested.connect(self._show_discover)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.programs_tab, "Programs")
        self.tabs.addTab(self.discover_tab, "Discover")
        self.setCentralWidget(self.tabs)

        self._build_toolbar()
        self._build_shortcuts()
        self._build_tray_icon()
        self._restore_layout()

        self._check_timer = QTimer(self)
        self._check_timer.setInterval(_UPDATE_TIMER_MS)
        self._check_timer.timeout.connect(self._check_due_updates_periodic)
        self._apply_update_schedule(startup=True)

        self.set_status("Ready.")
        # Housekeeping after the window is visible.
        QTimer.singleShot(1500, self._scan_permission_problems)
        QTimer.singleShot(3000, self._cleanup_stale_folders)

    # ------------------------------------------------------------------
    # TaskHost API (used by the tabs)
    # ------------------------------------------------------------------

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
        worker = TaskWorker(fn, *args)
        # Keep a Python reference so the GC cannot collect worker/worker.signals
        # while Qt's thread pool still holds the C++ QRunnable pointer.
        self._workers.append(worker)
        locked = lock_widgets or []
        # Disable the triggering widgets immediately, so a fast double-click
        # can't queue the same job twice before the worker starts.
        for widget in locked:
            widget.setEnabled(False)
        self._active_jobs += 1
        if not silent_errors:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)
            self.set_status(start_message)
        worker.signals.progress.connect(partial(self._on_task_progress, silent_errors))
        worker.signals.result.connect(on_result)
        if silent_errors:
            worker.signals.error.connect(partial(self._log_task_error, error_prefix))
        else:
            worker.signals.error.connect(partial(self._on_task_error, error_prefix))
        worker.signals.finished.connect(partial(self._on_task_finished, locked, worker))
        self.runner.start(worker)

    def set_status(self, message: str) -> None:
        self.status.showMessage(message, 10_000)

    def warn(self, message: str) -> None:
        QMessageBox.warning(self, "Portable Program Manager", message)
        # Keep the message in the status bar for a while so the user can re-read it.
        self.status.showMessage(message.splitlines()[0], 30_000)

    def ui_state(self) -> QSettings:
        return self._ui_state

    def refresh_programs(self) -> None:
        self.programs_tab.refresh()

    # Backwards-compatible names used by older code paths.
    _start_task = start_task
    _set_status = set_status
    _warn = warn

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_status_widgets(self) -> None:
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedWidth(220)
        self.progress_bar.setVisible(False)
        self.status.addPermanentWidget(self.progress_bar)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main toolbar")
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)
        style = self.style()

        self.check_all_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload), "Check for updates", self)
        self.check_all_action.setToolTip("Check every program for updates (Ctrl+U)")
        self.check_all_action.triggered.connect(self._check_all_program_updates)
        toolbar.addAction(self.check_all_action)

        open_root = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon), "Open install folder", self)
        open_root.triggered.connect(self._open_install_root)
        toolbar.addAction(open_root)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        settings_action = QAction(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "Settings", self)
        settings_action.setToolTip("Settings (Ctrl+,)")
        settings_action.triggered.connect(self._open_settings)
        toolbar.addAction(settings_action)

    def _build_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._open_settings)
        QShortcut(QKeySequence("Ctrl+U"), self, activated=self._check_all_program_updates)
        QShortcut(QKeySequence("Ctrl+1"), self, activated=lambda: self.tabs.setCurrentWidget(self.programs_tab))
        QShortcut(QKeySequence("Ctrl+2"), self, activated=self._show_discover)

    def _restore_layout(self) -> None:
        geometry = self._ui_state.value("window/geometry")
        if isinstance(geometry, QByteArray) and self.restoreGeometry(geometry):
            pass
        else:
            # First run: size to the screen instead of a fixed 1400×900 that
            # overflows small laptop screens.
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                self.resize(int(available.width() * 0.75), int(available.height() * 0.8))
                frame = self.frameGeometry()
                frame.moveCenter(available.center())
                self.move(frame.topLeft())
            else:
                self.resize(1200, 800)
        try:
            self.tabs.setCurrentIndex(int(self._ui_state.value("window/tab", 0)))
        except (TypeError, ValueError):
            pass

    def _save_layout(self) -> None:
        self._ui_state.setValue("window/geometry", self.saveGeometry())
        self._ui_state.setValue("window/tab", self.tabs.currentIndex())

    def _show_discover(self) -> None:
        self.tabs.setCurrentWidget(self.discover_tab)
        self.discover_tab.focus_search()

    # ------------------------------------------------------------------
    # Update checks
    # ------------------------------------------------------------------

    def _apply_update_schedule(self, startup: bool = False) -> None:
        """Start/stop scheduled checks to match the current settings (no restart needed)."""
        if self.controller.settings.run_update_check_on_startup:
            if not self._check_timer.isActive():
                self._check_timer.start()
            if startup and self.controller.list_programs():
                QTimer.singleShot(2000, self._check_due_updates_on_startup)
        else:
            self._check_timer.stop()

    def _check_due_updates_on_startup(self) -> None:
        self.start_task(
            self.controller.check_due_updates,
            True,  # include programs scheduled for "app start"
            start_message="Checking scheduled updates...",
            on_result=self._on_bulk_update_check_completed,
            error_prefix="Scheduled update check failed",
            silent_errors=True,  # background work: never pop dialogs
        )

    def _check_due_updates_periodic(self) -> None:
        if self._active_jobs > 0:
            return  # something is running; try again on the next tick
        self.start_task(
            self.controller.check_due_updates,
            False,
            start_message="Checking scheduled updates...",
            on_result=self._on_bulk_update_check_completed,
            error_prefix="Scheduled update check failed",
            silent_errors=True,
        )

    def _check_all_program_updates(self) -> None:
        self.start_task(
            self.controller.check_for_updates_all,
            start_message="Checking all programs for updates...",
            on_result=self._on_bulk_update_check_completed,
            error_prefix="Update check failed",
        )

    def _on_bulk_update_check_completed(self, report: UpdateCheckReport) -> None:
        self.refresh_programs()
        parts = [f"Checked {len(report.checked)} program(s). {report.available_count} update(s) available."]
        if report.installed:
            parts.append(f"Installed {len(report.installed)}.")
        if report.downloaded:
            parts.append(f"{len(report.downloaded)} downloaded and ready to install.")
        if report.deferred:
            parts.append(f"{len(report.deferred)} postponed (program running).")
        if report.errors:
            parts.append(f"{len(report.errors)} check(s) failed; see each program's details.")
        self.set_status(" ".join(parts))
        for message in report.errors:
            log.info("Update check problem: %s", message)
        self._notify_update_report(report)

    def _notify_update_report(self, report: UpdateCheckReport) -> None:
        """Tray notifications for what the update modes did (manual mode stays silent)."""
        if not self.controller.settings.show_system_notifications or not self._tray_available():
            return
        lines: list[str] = []
        acted = {p.program_id for p in report.installed} | {p.program_id for p in report.downloaded}
        for program in report.installed:
            lines.append(f"Updated {program.name} to {program.version or 'the latest version'}.")
        for program in report.downloaded:
            lines.append(" ".join(filter(None, [program.name, program.pending_update_version, "is downloaded and ready to install."])))
        for program in report.newly_available:
            policy = program.update_policy
            if program.program_id in acted or policy.update_mode == "manual" or not policy.notify_on_available_update:
                continue
            lines.append(" ".join(filter(None, [program.name, program.latest_upstream_version, "is available."])))
        lines.extend(report.deferred)
        if not lines:
            return
        shown = lines[:4] + ([f"…and {len(lines) - 4} more"] if len(lines) > 4 else [])
        title = "Program updates" if len(lines) > 1 else "Program update"
        self._tray_icon.showMessage(title, "\n".join(shown), QSystemTrayIcon.MessageIcon.Information, 8000)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def _scan_permission_problems(self) -> None:
        self.start_task(
            self.controller.find_permission_problems,
            start_message="Checking program folders...",
            on_result=self._on_permission_scan,
            error_prefix="Permission check failed",
            silent_errors=True,
        )

    def _on_permission_scan(self, problems: dict) -> None:
        self.programs_tab.set_permission_problems(problems)
        if not problems:
            return
        names = []
        for program_id in problems:
            try:
                names.append(self.controller.get_program(program_id).name)
            except KeyError:
                pass
        if not names:
            return
        log.warning("Programs with permission problems: %s", problems)
        self.warn(
            "Your Windows account can't open some files of: " + ", ".join(names) + ".\n\n"
            "This comes from installs made by an older version of this app. Right-click the "
            "program and choose \"Repair permissions\" to fix it."
        )

    def _cleanup_stale_folders(self) -> None:
        self.start_task(
            self.controller.cleanup_stale_temp_dirs,
            start_message="Tidying up temporary folders...",
            on_result=lambda _count: None,
            error_prefix="Cleanup failed",
            silent_errors=True,
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.controller.settings, self)
        if dialog.exec() != QDialog.Accepted:
            return
        old_theme = self.controller.settings.theme
        self.controller.update_settings(dialog.get_settings())
        if self.controller.settings.theme != old_theme:
            theme.apply_theme(QApplication.instance(), self.controller.settings.theme)
        self._apply_update_schedule()
        self.refresh_programs()
        self.set_status("Settings saved.")

    def _open_install_root(self) -> None:
        path = self.controller.settings.install_root
        if not Path(path).is_dir():
            self.warn(f"Folder not found:\n{path}")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            self.warn(f"Could not open folder: {exc}")

    # ------------------------------------------------------------------
    # Background task plumbing
    # ------------------------------------------------------------------

    def _on_task_progress(self, silent: bool, value: int, message: str) -> None:
        if silent:
            return
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(max(0, min(100, value)))
        self.set_status(message)

    def _log_task_error(self, prefix: str, error_message: str) -> None:
        log.warning("%s: %s", prefix, error_message)

    def _on_task_error(self, prefix: str, error_message: str) -> None:
        log.warning("%s: %s", prefix, error_message)
        self.warn(f"{prefix}:\n\n{error_message}")

    def _on_task_finished(self, lock_widgets: list[QWidget], worker: TaskWorker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass  # already removed (duplicate finished signal)
        self._active_jobs = max(0, self._active_jobs - 1)
        for widget in lock_widgets:
            widget.setEnabled(True)
        if self._active_jobs == 0 and self.progress_bar.isVisible():
            self.progress_bar.setValue(100)
            QTimer.singleShot(700, self._reset_progress_bar)

    def _reset_progress_bar(self) -> None:
        if self._active_jobs == 0:
            self.progress_bar.setVisible(False)
            self.progress_bar.setValue(0)

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _build_tray_icon(self) -> None:
        self._tray_icon = QSystemTrayIcon(make_tray_icon(), self)
        self._tray_icon.setToolTip("Portable Program Manager")

        self._tray_menu = QMenu(self)
        self._tray_show_action = self._tray_menu.addAction("Hide")
        self._tray_show_action.triggered.connect(self._toggle_window_visibility)
        self._tray_menu.addSeparator()
        self._tray_launch_menu = self._tray_menu.addMenu("Launch")
        check_action = self._tray_menu.addAction("Check for updates")
        check_action.triggered.connect(self._check_all_program_updates)
        self._tray_menu.addSeparator()
        quit_action = self._tray_menu.addAction("Quit")
        quit_action.triggered.connect(self._quit_app)
        self._tray_menu.aboutToShow.connect(self._update_tray_menu)

        self._tray_icon.setContextMenu(self._tray_menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.messageClicked.connect(self._show_from_tray)
        self._tray_icon.show()

    def _update_tray_menu(self) -> None:
        """Refresh the Show/Hide label and the quick-launch list just before the menu opens."""
        self._tray_show_action.setText("Hide" if self.isVisible() else "Show")
        menu = self._tray_launch_menu
        menu.clear()
        programs = [p for p in self.controller.list_programs() if p.launch_path]
        # Pinned first, then most recently run, then by name.
        programs.sort(key=lambda p: (not p.pinned, -(_timestamp(p.last_run_at)), p.name.lower()))
        for program in programs[:_TRAY_LAUNCH_LIMIT]:
            label = f"★ {program.name}" if program.pinned else program.name
            action = menu.addAction(label)
            action.triggered.connect(partial(self.programs_tab.run_program, program.program_id))
        if not programs:
            empty = menu.addAction("No programs yet")
            empty.setEnabled(False)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Left-click or double-click on the tray icon toggles the window."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_window_visibility()

    def _toggle_window_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self._hide_to_tray()
        else:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _hide_to_tray(self) -> None:
        """Hide the window to the system tray, optionally showing a first-time balloon."""
        self.hide()
        if not self._tray_notified:
            self._tray_notified = True
            self._tray_icon.showMessage(
                "Portable Program Manager",
                "Still running in the background. Click the tray icon to open it, "
                "or right-click it to launch your programs.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    @staticmethod
    def _tray_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def _confirm_quit_with_active_jobs(self) -> bool:
        """Ask before quitting while an install/update/remove is still running."""
        if self._active_jobs == 0:
            return True
        reply = QMessageBox.question(
            self,
            "Quit Portable Program Manager",
            "A task is still running (for example an install or update). Quitting now can "
            "leave it unfinished.\n\nQuit anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _quit_app(self) -> None:
        """Cleanly quit the application from the tray menu."""
        if not self._confirm_quit_with_active_jobs():
            return
        self._quitting = True
        self._save_layout()
        self._check_timer.stop()
        self._tray_icon.hide()
        QApplication.instance().quit()

    # ------------------------------------------------------------------
    # Window event overrides
    # ------------------------------------------------------------------

    def changeEvent(self, event: QEvent) -> None:
        """Intercept minimize to send the window to the tray instead."""
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self.controller.settings.minimize_to_tray
            and self._tray_available()
        ):
            # Defer the hide so Qt finishes processing the state change first.
            QTimer.singleShot(0, self._hide_to_tray)

    def closeEvent(self, event: QEvent) -> None:
        """Send to tray on close unless the user explicitly chose Quit."""
        self._save_layout()
        if not self._quitting and self.controller.settings.close_to_tray and self._tray_available():
            event.ignore()
            self._hide_to_tray()
            return
        if not self._quitting and not self._confirm_quit_with_active_jobs():
            event.ignore()
            return
        # Stop the periodic timer before tearing down the window so no
        # callbacks fire after widgets have been destroyed.
        self._quitting = True
        self._check_timer.stop()
        self._tray_icon.hide()
        event.accept()
        # The app keeps running with no windows (quitOnLastWindowClosed is off
        # for tray mode), so quit explicitly; otherwise an invisible process lingers.
        QApplication.instance().quit()


def _timestamp(iso: str | None) -> float:
    from datetime import datetime

    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
