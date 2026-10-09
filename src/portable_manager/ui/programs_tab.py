"""The Managed Programs tab: the list of installed programs, their details and actions.

Blocking work always goes through ``host.start_task``; the running-process
scan is the one exception, because it is quick, must not flash the progress
bar, and runs on the global thread pool.
"""

from __future__ import annotations

import dataclasses
import html
import logging
import os
from functools import partial
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QModelIndex, QObject, QPoint, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import fsops
from ..controller import RemoveOutcome, UpdateCheckReport
from ..launcher import launch_program
from ..models import ManagedProgram
from .details_view import ProgramDetailsView
from .edit_program_dialog import EditProgramDialog
from .host import TaskHost
from .programs_model import LAST_RUN, NAME, SOURCE, STATUS, VERSION, ProgramsFilterProxy, ProgramsModel

log = logging.getLogger(__name__)

RUNNING_SCAN_INTERVAL_MS = 5000


# ---------------------------------------------------------------------------
# Background running-process scan
# ---------------------------------------------------------------------------

class _ScanSignals(QObject):
    done = Signal(object)  # dict[folder, [process labels]] or None on failure


class _RunningScan(QRunnable):
    def __init__(self, folders: list[str], signals: _ScanSignals) -> None:
        super().__init__()
        self._folders = folders
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result: Any = fsops.running_programs_by_folder(self._folders)
        except Exception:  # a failed scan must never take the UI down
            log.warning("Running-program scan failed", exc_info=True)
            result = None
        self._signals.done.emit(result)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _button(text: str, variant: str = "secondary") -> QPushButton:
    button = QPushButton(text)
    button.setProperty("variant", variant)
    return button


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _can_run(program: ManagedProgram) -> bool:
    return bool(program.launch_path) and Path(program.install_dir).is_dir()


def _run_hint(program: ManagedProgram) -> str:
    if not Path(program.install_dir).is_dir():
        return "The install folder is missing, so this program can't be started."
    if not program.launch_path:
        return "This program has no launch file yet. Use Edit to choose one."
    return "Start this program."


def _has_update(program: ManagedProgram) -> bool:
    return bool(program.update_available or program.pending_update_version)


def _project_url(program: ManagedProgram) -> str | None:
    if program.repo_full_name:
        return f"https://github.com/{program.repo_full_name}"
    if program.source_type != "direct_url" and program.homepage_url:
        if program.homepage_url.startswith(("http://", "https://")):
            return program.homepage_url
    return None


def _launch_elevated(program: ManagedProgram, progress_callback: Callable[[int, str], None] | None = None) -> None:
    """Adapter: launch_program takes no progress callback, but start_task always passes one."""
    launch_program(program)


def _ask_remove(parent: QWidget, program: ManagedProgram) -> bool | None:
    """Ask how to remove *program*. True = delete its files too, False = keep them, None = cancel."""
    msg = QMessageBox(parent)
    msg.setWindowTitle("Remove from manager")
    msg.setText(f"Remove <b>{html.escape(program.name)}</b> from the manager?")
    msg.setInformativeText(
        f"Install folder: {program.install_dir}\n\n"
        "Deleting files requires the program to be closed first."
    )
    remove_btn = msg.addButton("Remove only", QMessageBox.ActionRole)
    delete_btn = msg.addButton("Remove and delete files", QMessageBox.DestructiveRole)
    msg.addButton(QMessageBox.Cancel)
    msg.setDefaultButton(QMessageBox.Cancel)
    msg.exec()
    clicked = msg.clickedButton()
    if clicked == remove_btn:
        return False
    if clicked == delete_btn:
        return True
    return None


_HEADER_KEY = "programs/header"
_SPLITTER_KEY = "programs/splitter"


# ---------------------------------------------------------------------------
# The tab
# ---------------------------------------------------------------------------

class ProgramsTab(QWidget):
    discover_requested = Signal()

    def __init__(self, host: TaskHost, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        self._permission_problems: dict[str, list[str]] = {}
        self._scan_busy = False
        self._scan_signals = _ScanSignals()
        self._scan_signals.done.connect(self._on_scan_done)

        self._model = ProgramsModel(self)
        self._proxy = ProgramsFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        self._running_timer = QTimer(self)
        self._running_timer.setInterval(RUNNING_SCAN_INTERVAL_MS)
        self._running_timer.timeout.connect(self._start_running_scan)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_filter)

        self._build_ui()
        self._restore_state()
        self.refresh()

    @property
    def _controller(self):
        return self._host.controller

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        heading = QLabel("Programs")
        heading.setProperty("role", "heading")
        layout.addWidget(heading)

        self._count_label = QLabel()
        self._count_label.setProperty("role", "subtle")
        layout.addWidget(self._count_label)

        # Toolbar: filter, then the actions.
        self._toolbar = QWidget()
        toolbar = QHBoxLayout(self._toolbar)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter programs…")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setMinimumWidth(220)
        self._filter_edit.setMaximumWidth(360)
        self._filter_edit.textChanged.connect(lambda _text: self._filter_timer.start())
        toolbar.addWidget(self._filter_edit)
        toolbar.addStretch(1)

        self._run_button = _button("Run", "primary")
        self._run_button.clicked.connect(lambda: self._with_selected(self.run_program))
        self._update_button = _button("Update")
        self._update_button.setToolTip("Install the newer release of the selected program.")
        self._update_button.clicked.connect(lambda: self._with_selected(self.install_update))
        self._check_button = _button("Check for updates")
        self._check_button.clicked.connect(lambda: self._with_selected(self._check_one))
        self._check_all_button = _button("Check all")
        self._check_all_button.setToolTip("Check every managed program for a newer release.")
        self._check_all_button.clicked.connect(self._check_all)
        self._more_button = _button("More ▾")
        self._more_button.clicked.connect(self._show_more_menu)
        for button in (self._run_button, self._update_button, self._check_button, self._check_all_button, self._more_button):
            toolbar.addWidget(button)
        layout.addWidget(self._toolbar)

        # Actions shared by the More menu, the context menu and the keyboard.
        self._act_open_folder = QAction("Open folder", self)
        self._act_open_folder.triggered.connect(lambda: self._with_selected(self._open_folder_for))
        self._act_edit = QAction("Edit…", self)
        self._act_edit.setShortcut(QKeySequence(Qt.Key_F2))
        self._act_edit.triggered.connect(lambda: self._with_selected(self._edit))
        self._act_repair = QAction("Repair permissions", self)
        self._act_repair.setToolTip(
            "Give your Windows account normal access to this program's files again\n"
            "(fixes installs made by older versions; may ask for administrator approval)."
        )
        self._act_repair.triggered.connect(lambda: self._with_selected(self._repair))
        self._act_remove = QAction("Remove…", self)
        self._act_remove.setShortcut(QKeySequence(QKeySequence.Delete))
        self._act_remove.triggered.connect(lambda: self._with_selected(self._remove))
        self._act_refresh = QAction("Refresh", self)
        self._act_refresh.setShortcut(QKeySequence(Qt.Key_F5))
        self._act_refresh.triggered.connect(self.refresh)
        self._act_filter = QAction("Find", self)
        self._act_filter.setShortcut(QKeySequence(QKeySequence.Find))
        self._act_filter.triggered.connect(self.focus_filter)
        for action in (self._act_edit, self._act_remove, self._act_refresh, self._act_filter):
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            self.addAction(action)

        self._more_menu = QMenu(self)
        self._more_menu.addAction(self._act_open_folder)
        self._more_menu.addAction(self._act_edit)
        self._more_menu.addAction(self._act_repair)
        self._more_menu.addSeparator()
        self._more_menu.addAction(self._act_remove)

        # Table of programs.
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().hide()
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setIconSize(QSize(20, 20))
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.activated.connect(self._on_activated)

        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in ((NAME, 280), (VERSION, 110), (LAST_RUN, 120), (SOURCE, 260)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            header.resizeSection(column, width)
        header.setSectionResizeMode(STATUS, QHeaderView.Stretch)
        self._table.sortByColumn(NAME, Qt.AscendingOrder)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        self._details = ProgramDetailsView()
        self._details.setMinimumHeight(120)

        self._splitter = QSplitter(Qt.Vertical)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._table)
        self._splitter.addWidget(self._details)
        self._splitter.setSizes([560, 440])

        list_page = QWidget()
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.addWidget(self._splitter)

        self._stack = QStackedWidget()
        self._stack.addWidget(list_page)
        self._stack.addWidget(self._build_empty_page())
        layout.addWidget(self._stack, 1)

    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)

        title = QLabel("No programs yet")
        title.setProperty("role", "empty-title")
        title.setAlignment(Qt.AlignCenter)
        # Local rule: the app stylesheet's QWidget font-size would override setFont().
        title.setStyleSheet("font-size: 16pt; font-weight: 700;")
        layout.addWidget(title)

        hint = QLabel("Find portable apps on GitHub or paste a download link to get started.")
        hint.setProperty("role", "subtle")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(12)
        find = _button("Find programs", "primary")
        find.clicked.connect(lambda: self.discover_requested.emit())
        layout.addWidget(find, 0, Qt.AlignCenter)
        layout.addStretch(1)
        return page

    def _restore_state(self) -> None:
        try:
            settings = self._host.ui_state()
            header_state = settings.value(_HEADER_KEY)
            if header_state:
                self._table.horizontalHeader().restoreState(QByteArray(header_state))
            sizes = settings.value(_SPLITTER_KEY)
            if sizes:
                values = [int(v) for v in sizes]
                if len(values) == 2 and all(v > 0 for v in values):
                    self._splitter.setSizes(values)
        except (TypeError, ValueError):
            log.debug("Ignoring unreadable Programs tab layout", exc_info=True)
        self._table.horizontalHeader().sectionResized.connect(self._save_header_state)
        self._table.horizontalHeader().sortIndicatorChanged.connect(self._save_header_state)
        self._splitter.splitterMoved.connect(self._save_splitter_state)

    def _save_header_state(self, *_args: Any) -> None:
        self._host.ui_state().setValue(_HEADER_KEY, self._table.horizontalHeader().saveState())

    def _save_splitter_state(self, *_args: Any) -> None:
        self._host.ui_state().setValue(_SPLITTER_KEY, [int(v) for v in self._splitter.sizes()])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload every program from the controller, keeping the selection."""
        programs = self._controller.list_programs()
        selected = self.selected_program_id()
        self._model.set_programs(programs)
        self._stack.setCurrentIndex(0 if programs else 1)
        self._toolbar.setVisible(bool(programs))
        self._restore_selection(selected)
        self._update_count_label()
        self._refresh_selection_state()
        self._start_running_scan()

    def selected_program_id(self) -> str | None:
        selection = self._table.selectionModel()
        if selection is None:
            return None
        rows = selection.selectedRows()
        if not rows:
            return None
        return rows[0].data(Qt.UserRole)

    def selected_program(self) -> ManagedProgram | None:
        program_id = self.selected_program_id()
        if program_id is None:
            return None
        try:
            return self._controller.get_program(program_id)
        except KeyError:
            return None

    def set_permission_problems(self, problems: dict[str, list[str]]) -> None:
        """Programs whose files the user can't open (program id -> paths)."""
        self._permission_problems = dict(problems)
        self._apply_permission_problems()
        self._refresh_selection_state()

    def focus_filter(self) -> None:
        self._filter_edit.setFocus()
        self._filter_edit.selectAll()

    def run_program(self, program_id: str) -> None:
        """Start a program (also used by the tray menu)."""
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        if not _can_run(program):
            self._host.warn(_run_hint(program))
            return
        self._host.start_task(
            self._controller.run_program,
            program_id,
            start_message=f"Starting {program.name}...",
            on_result=self._on_program_started,
            error_prefix=f"Couldn't start {program.name}",
        )

    def install_update(self, program_id: str) -> None:
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        self._host.start_task(
            self._controller.install_available_update,
            program_id,
            start_message=f"Installing update for {program.name}...",
            on_result=self._on_update_installed,
            error_prefix="Update install failed",
        )

    # ------------------------------------------------------------------
    # Filtering, selection and enablement
    # ------------------------------------------------------------------

    def _apply_filter(self) -> None:
        self._proxy.set_filter_text(self._filter_edit.text())
        self._update_count_label()
        self._refresh_selection_state()

    def _update_count_label(self) -> None:
        total = self._model.rowCount()
        parts = []
        if self._proxy.filter_text():
            parts.append(f"{self._proxy.rowCount()} of {_plural(total, 'program')} shown")
        else:
            parts.append(_plural(total, "program"))
        updates = self._model.update_count()
        if updates:
            parts.append(f"{_plural(updates, 'update')} available")
        self._count_label.setText(" · ".join(parts))

    def _restore_selection(self, program_id: str | None) -> None:
        if program_id is None:
            return
        row = self._model.row_of(program_id)
        if row < 0:
            self._table.clearSelection()
            return
        proxy_index = self._proxy.mapFromSource(self._model.index(row, NAME))
        if proxy_index.isValid():
            self._table.selectRow(proxy_index.row())

    def _on_selection_changed(self, *_args: Any) -> None:
        self._refresh_selection_state()

    def _refresh_selection_state(self) -> None:
        program = self.selected_program()
        self._update_details(program)
        self._update_actions(program)

    def _update_details(self, program: ManagedProgram | None) -> None:
        if program is None:
            self._details.show_program(None)
            return
        self._details.show_program(
            program,
            running=self._model.running_for(program.program_id),
            permission_problem=self._model.is_permission_problem(program.program_id),
        )

    def _update_actions(self, program: ManagedProgram | None) -> None:
        has = program is not None
        can_run = has and _can_run(program)
        has_update = has and _has_update(program)
        has_repo = has and bool(program.repo_full_name)
        in_problems = has and self._model.is_permission_problem(program.program_id)

        self._run_button.setEnabled(can_run)
        self._run_button.setToolTip(_run_hint(program) if has else "Select a program first.")
        self._update_button.setVisible(has_update)
        self._update_button.setEnabled(has_update)
        self._check_button.setEnabled(has_repo)
        if has and not has_repo:
            self._check_button.setToolTip("Only programs installed from a GitHub release can be checked.")
        else:
            self._check_button.setToolTip("Check the selected program for a newer release.")
        self._check_all_button.setEnabled(self._model.rowCount() > 0)
        self._more_button.setEnabled(has)
        self._act_open_folder.setEnabled(has)
        self._act_edit.setEnabled(has)
        self._act_remove.setEnabled(has)
        self._act_repair.setEnabled(in_problems)

    def _apply_permission_problems(self) -> None:
        self._model.set_permission_problems(set(self._permission_problems))

    # ------------------------------------------------------------------
    # Context and More menus, keyboard
    # ------------------------------------------------------------------

    def _show_more_menu(self) -> None:
        origin = self._more_button.mapToGlobal(QPoint(0, self._more_button.height()))
        self._more_menu.popup(origin)

    def _on_context_menu(self, pos: QPoint) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        self._table.selectRow(index.row())
        program = self.selected_program()
        if program is None:
            return
        menu = self._build_context_menu(program)
        menu.exec(self._table.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _build_context_menu(self, program: ManagedProgram) -> QMenu:
        pid = program.program_id
        menu = QMenu(self)

        def add(text: str, fn: Callable[..., Any], *args: Any, enabled: bool = True) -> QAction:
            action = menu.addAction(text)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False: fn(*args))
            return action

        add("Run", self.run_program, pid, enabled=_can_run(program))
        add("Run as administrator", self._run_as_admin, pid, enabled=_can_run(program))
        menu.addSeparator()
        if _has_update(program):
            add("Install update", self.install_update, pid)
        add("Check for updates", self._check_one, pid, enabled=bool(program.repo_full_name))
        menu.addSeparator()
        add("Open folder", self._open_folder, program.install_dir)
        add("Copy folder path", self._copy_path, program.install_dir)
        url = _project_url(program)
        if url:
            add("Open project page", QDesktopServices.openUrl, QUrl(url))
        menu.addSeparator()
        add("Edit…", self._edit, pid)
        add("Repair permissions", self._repair, pid)
        menu.addSeparator()
        add("Remove…", self._remove, pid)
        return menu

    def _on_activated(self, index: QModelIndex) -> None:
        program_id = index.data(Qt.UserRole)
        if program_id:
            self.run_program(program_id)

    def _with_selected(self, fn: Callable[[str], Any]) -> None:
        program_id = self.selected_program_id()
        if program_id is None:
            self._host.warn("Select a managed program first.")
            return
        fn(program_id)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_program_started(self, program: ManagedProgram) -> None:
        self.refresh()
        self._host.set_status(f"Started {program.name}.")

    def _run_as_admin(self, program_id: str) -> None:
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        if not _can_run(program):
            self._host.warn(_run_hint(program))
            return
        # A one-off elevated launch of a copy; the stored program keeps run_as_admin unchanged.
        elevated = dataclasses.replace(program, run_as_admin=True)
        self._host.start_task(
            _launch_elevated,
            elevated,
            start_message=f"Starting {program.name} as administrator...",
            on_result=partial(self._on_admin_started, program.name),
            error_prefix=f"Couldn't start {program.name}",
        )

    def _on_admin_started(self, name: str, _result: Any) -> None:
        self._host.set_status(f"Started {name} as administrator.")

    def _check_one(self, program_id: str) -> None:
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        self._host.start_task(
            self._controller.check_for_updates,
            program_id,
            start_message=f"Checking updates for {program.name}...",
            on_result=self._on_single_checked,
            error_prefix="Update check failed",
        )

    def _on_single_checked(self, program: ManagedProgram) -> None:
        self.refresh()
        if program.update_available:
            self._host.set_status(
                f"Update available for {program.name}: {program.latest_upstream_version or 'new release'}"
            )
        else:
            self._host.set_status(f"No update found for {program.name}.")

    def _check_all(self) -> None:
        # The main window owns bulk checks so tray notifications fire the same way
        # for button clicks, the toolbar and scheduled checks.
        shared = getattr(self._host, "check_all_updates", None)
        if callable(shared):
            shared()
            return
        self._host.start_task(
            self._controller.check_for_updates_all,
            start_message="Checking all managed programs for updates...",
            on_result=self._on_bulk_checked,
            error_prefix="Bulk update check failed",
        )

    def _on_bulk_checked(self, report: UpdateCheckReport) -> None:
        parts = [f"Checked {len(report.checked)} program(s). {report.available_count} update(s) available."]
        if report.installed:
            parts.append(f"Installed {len(report.installed)}.")
        if report.downloaded:
            parts.append(f"{len(report.downloaded)} downloaded and ready to install.")
        if report.deferred:
            parts.append(f"{len(report.deferred)} postponed (program running).")
        if report.errors:
            parts.append(f"{len(report.errors)} check(s) failed; see each program's details.")
        for message in report.errors:
            log.info("Update check problem: %s", message)
        self.refresh()
        self._host.refresh_programs()
        self._host.set_status(" ".join(parts))

    def _on_update_installed(self, program: ManagedProgram) -> None:
        self.refresh()
        self._host.set_status(f"Updated {program.name} to {program.version or 'latest release'}.")

    def _edit(self, program_id: str) -> None:
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        dialog = EditProgramDialog(program, self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._controller.edit_program(
                program_id=program_id,
                name=dialog.get_name(),
                notes=dialog.get_notes(),
                launch_path=dialog.get_launch_path(),
                launch_args=dialog.get_launch_args(),
                working_directory_override=dialog.get_working_directory(),
                update_policy=dialog.get_update_policy(),
                run_as_admin=dialog.get_run_as_admin(),
            )
        except ValueError as exc:
            self._host.warn(str(exc))
            return
        pinned = getattr(dialog, "get_pinned", lambda: program.pinned)()
        set_pinned = getattr(self._controller, "set_pinned", None)
        if set_pinned is not None and pinned != program.pinned:
            set_pinned(program_id, pinned)
        self.refresh()
        self._host.set_status(f"Saved changes to {dialog.get_name()}.")

    def _repair(self, program_id: str) -> None:
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        self._host.start_task(
            self._controller.repair_permissions,
            program_id,
            start_message=f"Repairing permissions for {program.name}...",
            on_result=self._on_repaired,
            error_prefix="Repair failed",
        )

    def _on_repaired(self, program: ManagedProgram) -> None:
        self._permission_problems.pop(program.program_id, None)
        self._apply_permission_problems()
        self.refresh()
        self._host.set_status(f"Permissions repaired for {program.name}.")

    def _remove(self, program_id: str) -> None:
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        delete_files = _ask_remove(self, program)
        if delete_files is None:
            return
        self._host.start_task(
            self._controller.remove_program,
            program_id,
            delete_files,
            start_message=f"Removing {program.name}...",
            on_result=partial(self._on_program_removed, program_id),
            error_prefix="Remove failed",
        )

    def _on_program_removed(self, program_id: str, outcome: RemoveOutcome) -> None:
        self._permission_problems.pop(program_id, None)
        self._apply_permission_problems()
        self.refresh()
        if outcome.deleted_files:
            self._host.set_status(f"Removed {outcome.program_name} and deleted its install folder.")
        else:
            self._host.set_status(f"Removed {outcome.program_name} from manager.")
        for warning in outcome.warnings:
            self._host.warn(warning)

    def _open_folder_for(self, program_id: str) -> None:
        try:
            program = self._controller.get_program(program_id)
        except KeyError:
            self._host.warn("The selected program could not be found.")
            return
        self._open_folder(program.install_dir)

    def _open_folder(self, path: str) -> None:
        if not Path(path).is_dir():
            self._host.warn(f"Folder not found:\n{path}")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            self._host.warn(f"Could not open folder: {exc}")

    def _copy_path(self, path: str) -> None:
        QGuiApplication.clipboard().setText(path)
        self._host.set_status("Copied folder path.")

    # ------------------------------------------------------------------
    # Running-process scan (every 5 s while visible, and on refresh)
    # ------------------------------------------------------------------

    def _start_running_scan(self) -> None:
        if self._scan_busy:
            return
        self._scan_busy = True
        QThreadPool.globalInstance().start(_RunningScan(self._model.install_dirs(), self._scan_signals))

    def _on_scan_done(self, running: Any) -> None:
        self._scan_busy = False
        if running is None:
            return
        if self._model.set_running(running):
            self._refresh_selection_state()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._running_timer.start()
        self._start_running_scan()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._running_timer.stop()
