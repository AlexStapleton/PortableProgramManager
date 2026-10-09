from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, List

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..controller import AppController, InstallOutcome, RemoveOutcome, UpdateCheckReport
from ..github_client import GitHubRepo
from ..models import ManagedProgram
from .edit_program_dialog import EditProgramDialog
from .icons import make_tray_icon
from .settings_dialog import SettingsDialog
from .workers import TaskRunner, TaskWorker

# Substrings (checked against repo name + description) that strongly suggest
# a platform target OTHER than Windows.  Terms are matched case-insensitively.
_NON_WINDOWS_TERMS = frozenset({
    # Apple
    "macos", "mac-os", "mac os", "osx", "os x", "darwin", "macbook",
    "swiftui", "cocoa", "appkit", "uikit", "xcode",
    # Mobile
    "android", "ios", "iphone", "ipad", "ipados", "watchos", "tvos",
    "react-native", "react native", "flutter",
    # Linux-only
    "linux-only", "linux only", "x11", "wayland", "gnome", "kde",
    "apt-get", "dpkg", "pacman", "snap ", "flatpak",
    "ubuntu", "debian", "fedora", "archlinux", "arch linux",
    # BSD / other
    "freebsd", "openbsd",
})

# Terms that indicate Windows support even if a non-Windows term is also present
# (e.g., "cross-platform" repos that mention Linux in description).
_WINDOWS_POSITIVE_TERMS = frozenset({
    "windows", "win32", "win64", "winget", "msvc", "mingw",
    "portable", ".exe", "powershell", "cmd.exe", "batch file",
    "cross-platform", "cross platform", "multiplatform", "multi-platform",
    "all platforms", "any platform",
})


log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self.controller = controller
        self.search_results: list[GitHubRepo] = []
        self._visible_search_results: list[GitHubRepo] = []  # filtered snapshot shown in table
        self._last_query: str = ""
        self.runner = TaskRunner(max_threads=4)
        self._active_jobs = 0
        self._workers: list[TaskWorker] = []  # Keep workers alive until Qt is done with them

        self.setWindowTitle("Portable Program Manager")
        self.resize(1400, 900)
        self._quitting = False          # set True before QApplication.quit() to bypass close-to-tray
        self._tray_notified = False     # show balloon notification only on first tray hide

        # Debounce timer for the managed-programs filter input.
        self._filter_debounce = QTimer(self)
        self._filter_debounce.setSingleShot(True)
        self._filter_debounce.setInterval(150)
        self._filter_debounce.timeout.connect(self._refresh_programs_table)

        self._build_toolbar()
        self._build_ui()
        self._refresh_programs_table()

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self._build_status_widgets()
        self._build_tray_icon()

        # Defer missing-folder check so the window appears immediately.
        self._set_status("Ready.")
        QTimer.singleShot(0, self._check_missing_folders)
        # Remove staging/backup/trash folders left behind by an interrupted operation.
        QTimer.singleShot(3000, self._cleanup_stale_folders)
        QTimer.singleShot(1500, self._scan_permission_problems)

        self._check_timer = QTimer(self)
        if self.controller.settings.run_update_check_on_startup:
            if self.controller.list_programs():
                QTimer.singleShot(300, self._check_due_updates_on_startup)
            self._check_timer.setInterval(30 * 60 * 1000)
            self._check_timer.timeout.connect(self._check_due_updates_periodic)
            self._check_timer.start()

    def _repair_selected_program(self) -> None:
        program = self._get_selected_program()
        if not program:
            self._warn("Select a managed program first.")
            return
        self._start_task(
            self.controller.repair_permissions,
            program.program_id,
            start_message=f"Repairing permissions for {program.name}...",
            on_result=lambda p: self._set_status(f"Permissions repaired for {p.name}."),
            error_prefix="Repair failed",
            lock_widgets=[self.repair_button],
        )

    def _scan_permission_problems(self) -> None:
        self._start_task(
            self.controller.find_permission_problems,
            start_message="Checking program folders...",
            on_result=self._on_permission_scan,
            error_prefix="Permission check failed",
            silent_errors=True,
        )

    def _on_permission_scan(self, problems: dict) -> None:
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
        self._warn(
            "Your Windows account can't open some files of: " + ", ".join(names) + ".\n\n"
            "This comes from installs made by an older version of this app. Select the program "
            "and click \"Repair Permissions\" to fix it."
        )

    def _cleanup_stale_folders(self) -> None:
        self._start_task(
            self.controller.cleanup_stale_temp_dirs,
            start_message="Tidying up temporary folders...",
            on_result=lambda _count: None,
            error_prefix="Cleanup failed",
            silent_errors=True,
        )

    def _check_missing_folders(self) -> None:
        """Check for missing install folders after the UI is visible.

        Reads from the table items instead of calling list_programs() again,
        since the table was just populated during __init__.
        """
        missing_count = 0
        for row in range(self.programs_table.rowCount()):
            item = self.programs_table.item(row, 7)  # install dir column
            if item and "[folder missing]" in item.text():
                missing_count += 1
        if missing_count:
            self._set_status(
                f"{missing_count} managed program(s) have missing install folders (shown in red)."
            )

    def _build_status_widgets(self) -> None:
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(280)
        self.progress_bar.setVisible(False)
        self.status.addPermanentWidget(self.progress_bar)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self._open_settings)
        toolbar.addAction(settings_action)

        choose_root_action = QAction("Open Install Root", self)
        choose_root_action.triggered.connect(self._open_install_root)
        toolbar.addAction(choose_root_action)

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self._refresh_programs_table)
        toolbar.addAction(refresh_action)

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        tabs.addTab(self._build_discover_tab(), "Discover")
        tabs.addTab(self._build_manage_tab(), "Managed Programs")
        self.setCentralWidget(tabs)

    def _build_discover_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        heading = QLabel("Find and install portable programs")
        heading.setProperty("role", "heading")
        layout.addWidget(heading)

        subheading = QLabel("Search GitHub releases or install directly from a repository URL / asset URL.")
        subheading.setProperty("role", "subtle")
        layout.addWidget(subheading)

        search_card = self._card()
        search_layout = QVBoxLayout(search_card)

        # --- Search bar ---
        top_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search GitHub, for example: media player portable windows")
        self.search_input.returnPressed.connect(self._search_github)
        self.search_button = QPushButton("Search GitHub")
        self.search_button.clicked.connect(self._search_github)
        top_row.addWidget(self.search_input, 1)
        top_row.addWidget(self.search_button)
        search_layout.addLayout(top_row)

        # --- Filter / sort controls ---
        filter_row = QHBoxLayout()

        filter_row.addWidget(QLabel("Sort:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Stars", "stars")
        self.sort_combo.addItem("Recently Updated", "updated")
        self.sort_combo.setToolTip(
            "Stars: GitHub's default (most-starred first).\n"
            "Recently Updated: re-searches GitHub sorted by last activity."
        )
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        filter_row.addWidget(self.sort_combo)

        filter_row.addSpacing(16)

        self.windows_only_check = QCheckBox("Windows only")
        self.windows_only_check.setToolTip(
            "Hide repositories that appear to target non-Windows platforms.\n"
            "Checks the repo name and description for platform indicators\n"
            "(e.g. macos, android, linux-only, ubuntu, flatpak, etc.).\n"
            "Cross-platform repos that also mention Windows are kept."
        )
        self.windows_only_check.stateChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.windows_only_check)

        filter_row.addSpacing(16)

        self.hide_inactive_check = QCheckBox("Hide inactive for more than")
        self.hide_inactive_check.setToolTip("Exclude repositories that have not been updated within the given number of days.")
        self.hide_inactive_check.stateChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.hide_inactive_check)

        self.inactive_days_spin = QSpinBox()
        self.inactive_days_spin.setRange(30, 3650)
        self.inactive_days_spin.setValue(365)
        self.inactive_days_spin.setSuffix(" days")
        self.inactive_days_spin.setEnabled(False)
        self.inactive_days_spin.setToolTip("Repositories not updated within this many days will be hidden.")
        self.hide_inactive_check.stateChanged.connect(
            lambda state: self.inactive_days_spin.setEnabled(bool(state))
        )
        self.inactive_days_spin.valueChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.inactive_days_spin)

        filter_row.addStretch(1)
        search_layout.addLayout(filter_row)

        # --- Results table ---
        self.search_results_table = QTableWidget(0, 5)
        self.search_results_table.setHorizontalHeaderLabels(["Name", "Repository", "Stars", "Latest Release", "Description"])
        self.search_results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.search_results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.search_results_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.search_results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.search_results_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.search_results_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.search_results_table.setSelectionMode(QTableWidget.SingleSelection)
        search_layout.addWidget(self.search_results_table)

        install_search_row = QHBoxLayout()
        self.install_selected_button = QPushButton("Install Selected Result")
        self.install_selected_button.clicked.connect(self._install_selected_result)
        open_repo_button = QPushButton("Open Repository")
        open_repo_button.clicked.connect(self._open_selected_repo)
        self.install_channel_combo = QComboBox()
        self.install_channel_combo.addItem("Stable release", "latest_release")
        self.install_channel_combo.addItem("Pre-release", "prerelease")
        self.install_channel_combo.setToolTip("Which release channel to install from")
        install_search_row.addWidget(self.install_channel_combo)
        install_search_row.addWidget(self.install_selected_button)
        install_search_row.addWidget(open_repo_button)
        install_search_row.addStretch(1)
        search_layout.addLayout(install_search_row)

        layout.addWidget(search_card)

        url_card = self._card()
        url_layout = QVBoxLayout(url_card)
        url_layout.addWidget(QLabel("Install from URL"))

        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste a GitHub repo URL or a direct downloadable asset URL")
        self.url_install_button = QPushButton("Install from URL")
        self.url_install_button.clicked.connect(self._install_from_url)
        url_row.addWidget(self.url_input, 1)
        url_row.addWidget(self.url_install_button)
        url_layout.addLayout(url_row)

        self.discover_log = QTextEdit()
        self.discover_log.setReadOnly(True)
        self.discover_log.setPlaceholderText("Activity log")
        url_layout.addWidget(self.discover_log)
        layout.addWidget(url_card)

        return root

    def _build_manage_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)

        heading = QLabel("Managed portable programs")
        heading.setProperty("role", "heading")
        layout.addWidget(heading)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.program_filter_input = QLineEdit()
        self.program_filter_input.setPlaceholderText("Search installed apps by name, version, source, or status")
        self.program_filter_input.textChanged.connect(self._filter_debounce.start)
        filter_row.addWidget(self.program_filter_input, 1)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Vertical)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        self.programs_table = QTableWidget(0, 10)
        self.programs_table.setHorizontalHeaderLabels(
            [
                "Name",
                "Version",
                "Latest",
                "Update Status",
                "Last Checked",
                "Policy",
                "Source",
                "Install Dir",
                "Last Run",
                "Notes",
            ]
        )
        for idx in range(10):
            mode = QHeaderView.Stretch if idx in {7, 9} else QHeaderView.ResizeToContents
            self.programs_table.horizontalHeader().setSectionResizeMode(idx, mode)
        self.programs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.programs_table.setSelectionMode(QTableWidget.SingleSelection)
        top_layout.addWidget(self.programs_table)

        actions = QHBoxLayout()
        self.run_button = QPushButton("Run Selected")
        self.run_button.clicked.connect(self._run_selected_program)
        self.open_folder_button = QPushButton("Open Folder")
        self.open_folder_button.clicked.connect(self._open_selected_program_folder)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.clicked.connect(self._edit_selected_program)
        self.check_updates_button = QPushButton("Check Selected")
        self.check_updates_button.clicked.connect(self._check_selected_program_updates)
        self.check_all_updates_button = QPushButton("Check All")
        self.check_all_updates_button.clicked.connect(self._check_all_program_updates)
        self.install_update_button = QPushButton("Install Update")
        self.install_update_button.clicked.connect(self._install_selected_program_update)
        self.remove_button = QPushButton("Remove From Manager")
        self.remove_button.clicked.connect(self._remove_selected_program)
        self.repair_button = QPushButton("Repair Permissions")
        self.repair_button.setToolTip(
            "Give your Windows account normal access to this program's files again\n"
            "(fixes installs made by older versions; may ask for administrator approval)."
        )
        self.repair_button.clicked.connect(self._repair_selected_program)
        for widget in [
            self.run_button,
            self.open_folder_button,
            self.edit_button,
            self.check_updates_button,
            self.check_all_updates_button,
            self.install_update_button,
            self.remove_button,
            self.repair_button,
        ]:
            actions.addWidget(widget)
        actions.addStretch(1)
        top_layout.addLayout(actions)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.addWidget(QLabel("Program details"))
        self.program_details = QTextEdit()
        self.program_details.setReadOnly(True)
        bottom_layout.addWidget(self.program_details)

        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.setSizes([560, 240])
        layout.addWidget(splitter)

        self.programs_table.itemSelectionChanged.connect(self._update_program_details)
        return root

    # ------------------------------------------------------------------
    # Search and filter
    # ------------------------------------------------------------------

    def _search_github(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            self._warn("Enter a search term.")
            return
        self._last_query = query
        sort = self.sort_combo.currentData()
        self._start_task(
            self.controller.search_github,
            query,
            sort,
            start_message=f"Searching GitHub for '{query}'...",
            on_result=partial(self._on_search_completed, query),
            error_prefix="Search failed",
            lock_widgets=[self.search_button, self.install_selected_button],
        )

    def _on_search_completed(self, query: str, results: list[GitHubRepo]) -> None:
        self.search_results = results
        visible = self._apply_search_filters(results)
        self._populate_search_table(visible)
        hidden = len(results) - len(visible)
        suffix = f" ({hidden} hidden by filters)" if hidden else ""
        self._append_log(f"Search for '{query}' returned {len(results)} repositories{suffix}.")
        self._set_status(f"Showing {len(visible)} of {len(results)} results.")
        # Fetch latest release dates in the background and update the table.
        if results:
            self._start_task(
                self.controller.fetch_release_dates,
                results,
                start_message="Fetching latest release dates...",
                on_result=self._on_release_dates_fetched,
                error_prefix="Release date fetch failed",
            )

    def _on_release_dates_fetched(self, repos: list[GitHubRepo]) -> None:
        """Update the search results table after release dates have been fetched."""
        # The worker returns enriched copies; ignore them if a newer search replaced the list.
        if [r.full_name for r in repos] != [r.full_name for r in self.search_results]:
            return
        self.search_results = repos
        # Re-apply filters (inactive filter now has release dates to work with).
        visible = self._apply_search_filters(self.search_results)
        self._populate_search_table(visible)
        hidden = len(self.search_results) - len(visible)
        suffix = f" ({hidden} hidden by filters)" if hidden else ""
        self._set_status(f"Showing {len(visible)} of {len(self.search_results)} results{suffix}.")

    def _on_filter_changed(self) -> None:
        """Re-apply filters to the current result set without a new API call."""
        if not self.search_results:
            return
        visible = self._apply_search_filters(self.search_results)
        self._populate_search_table(visible)
        hidden = len(self.search_results) - len(visible)
        suffix = f" ({hidden} hidden by filters)" if hidden else ""
        self._set_status(f"Showing {len(visible)} of {len(self.search_results)} results{suffix}.")

    def _on_sort_changed(self) -> None:
        """Sort change re-searches GitHub so the server returns the right ordering."""
        if self._last_query:
            self._search_github()
        elif self.search_results:
            self._on_filter_changed()

    @staticmethod
    def _best_date(repo: GitHubRepo) -> str:
        """Return the most meaningful date for a repo: release > pushed > updated."""
        return repo.latest_release_at or repo.pushed_at or repo.updated_at or ""

    @staticmethod
    def _looks_windows_compatible(repo: GitHubRepo) -> bool:
        """Return True unless the repo appears to target a non-Windows platform.

        After release info has been fetched, uses the definitive
        ``has_windows_release`` flag.  Before that, falls back to heuristic
        text matching on repo name + description.
        """
        # Definitive answer available after background release fetch.
        if repo.has_windows_release is not None:
            # If a release exists with Windows assets → keep.
            # If a release exists with NO Windows assets → hide.
            # Edge case: repo has no releases at all (latest_release_at empty)
            # but has_windows_release is False — still keep it, since we can't
            # be sure it's non-Windows (it might just not use GitHub releases).
            if not repo.latest_release_at:
                return True  # No release to judge — fall through to heuristic
            return repo.has_windows_release

        # Heuristic fallback: check repo name + description for platform terms.
        haystack = f"{repo.full_name} {repo.description}".lower()
        has_non_windows = any(term in haystack for term in _NON_WINDOWS_TERMS)
        if not has_non_windows:
            return True
        # If it also mentions Windows / cross-platform, keep it.
        has_windows = any(term in haystack for term in _WINDOWS_POSITIVE_TERMS)
        return has_windows

    def _apply_search_filters(self, results: list[GitHubRepo]) -> list[GitHubRepo]:
        """Return a filtered (and sorted) copy of *results* based on current UI controls."""
        filtered = list(results)

        if self.windows_only_check.isChecked():
            filtered = [r for r in filtered if self._looks_windows_compatible(r)]

        if self.hide_inactive_check.isChecked():
            threshold_days = self.inactive_days_spin.value()
            cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)
            filtered = [r for r in filtered if self._parse_api_timestamp(self._best_date(r)) >= cutoff]

        # Client-side re-sort only applies to "recently updated"; "stars" is already
        # returned in the correct order by the GitHub API.
        if self.sort_combo.currentData() == "updated":
            filtered.sort(key=lambda r: self._best_date(r), reverse=True)

        return filtered

    def _populate_search_table(self, results: list[GitHubRepo]) -> None:
        self._visible_search_results = results
        table = self.search_results_table
        table.setRowCount(len(results))
        for row, repo in enumerate(results):
            # Show latest release date if available, otherwise show pushed_at as fallback.
            date_str = repo.latest_release_at or repo.pushed_at or repo.updated_at or ""
            self._set_item(table, row, 0, repo.name)
            self._set_item(table, row, 1, repo.full_name)
            self._set_item(table, row, 2, str(repo.stars))
            self._set_item(table, row, 3, self._format_timestamp(date_str) if date_str else "")
            self._set_item(table, row, 4, repo.description)

    # ------------------------------------------------------------------
    # Install actions (discover tab)
    # ------------------------------------------------------------------

    def _install_selected_result(self) -> None:
        repo = self._get_selected_search_repo()
        if not repo:
            self._warn("Select a search result first.")
            return
        self._install_repo(repo.full_name)

    def _open_selected_repo(self) -> None:
        repo = self._get_selected_search_repo()
        if not repo:
            self._warn("Select a search result first.")
            return
        QDesktopServices.openUrl(QUrl(repo.html_url))

    def _install_repo(self, repo_full_name: str) -> None:
        channel = self.install_channel_combo.currentData()
        self._start_task(
            self.controller.install_from_repo,
            repo_full_name,
            channel,
            start_message=f"Installing {repo_full_name}...",
            on_result=partial(self._on_install_completed, f"Installed from {repo_full_name}"),
            error_prefix="Install failed",
            lock_widgets=[self.search_button, self.install_selected_button, self.url_install_button],
        )

    def _on_install_completed(self, log_prefix: str, outcome: InstallOutcome) -> None:
        program = outcome.program
        self._refresh_programs_table()
        self._append_log(f"{log_prefix}: {program.name}.")

        if outcome.is_likely_installer:
            self._set_status(
                f"Installed {program.name} — this looks like a setup/installer, not a portable app."
            )
            self._append_log(
                f"Note: {program.name} appears to be a setup/installer. "
                "You will need to run it manually to complete installation. "
                "Automatic update checks have been disabled for this program "
                "since the installed application likely manages its own updates."
            )
            QMessageBox.information(
                self,
                "Setup / Installer Detected",
                f"<b>{program.name}</b> appears to be a setup or installer rather than a "
                "portable application.<br><br>"
                "You will need to <b>run the installer manually</b> from the install folder "
                "to complete setup.<br><br>"
                "Automatic update checks have been <b>disabled</b> for this program since "
                "the installed application likely manages its own updates.<br><br>"
                "You can change this later via the Edit dialog.",
            )
        elif program.launch_path:
            self._set_status(f"Installed {program.name}.")
        else:
            self._set_status(
                f"Installed {program.name} — no executable detected. "
                "Use Edit to set the launch path manually."
            )
            self._append_log(
                f"Warning: no executable found in {program.install_dir}. "
                "Set the launch path via the Edit dialog."
            )
        if self.controller.settings.auto_open_folder_after_install:
            self._open_folder(program.install_dir)

    def _install_from_url(self) -> None:
        value = self.url_input.text().strip()
        if not value:
            self._warn("Paste a repository URL or asset URL.")
            return

        repo_full_name = self.controller.github.extract_repo_full_name(value)
        if repo_full_name:
            self._install_repo(repo_full_name)  # channel is read from install_channel_combo inside
            return

        self._start_task(
            self.controller.install_from_url,
            value,
            start_message="Installing from direct URL...",
            on_result=partial(self._on_install_completed, "Installed from direct URL"),
            error_prefix="Install from URL failed",
            lock_widgets=[self.search_button, self.install_selected_button, self.url_install_button],
        )

    # ------------------------------------------------------------------
    # Managed programs actions
    # ------------------------------------------------------------------

    def _run_selected_program(self) -> None:
        program = self._get_selected_program()
        if not program:
            self._warn("Select a managed program first.")
            return

        self._start_task(
            self.controller.run_program,
            program.program_id,
            start_message=f"Starting {program.name}...",
            on_result=self._on_program_started,
            error_prefix=f"Couldn't start {program.name}",
            lock_widgets=[self.run_button],
        )

    def _on_program_started(self, program: ManagedProgram) -> None:
        self._set_status(f"Started {program.name}.")
        self._refresh_programs_table()

    def _check_selected_program_updates(self) -> None:
        program = self._get_selected_program()
        if not program:
            self._warn("Select a managed program first.")
            return

        self._start_task(
            self.controller.check_for_updates,
            program.program_id,
            start_message=f"Checking updates for {program.name}...",
            on_result=self._on_single_update_check_completed,
            error_prefix="Update check failed",
            lock_widgets=[self.check_updates_button, self.check_all_updates_button, self.install_update_button],
        )

    def _on_single_update_check_completed(self, updated_program: ManagedProgram) -> None:
        self._refresh_programs_table()
        if updated_program.update_available:
            self._set_status(f"Update available for {updated_program.name}: {updated_program.latest_upstream_version or 'new release'}")
        else:
            self._set_status(f"No update found for {updated_program.name}.")

    def _check_due_updates_on_startup(self) -> None:
        """Run update checks for programs that are due according to their schedule (startup)."""
        self._start_task(
            self.controller.check_due_updates,
            True,  # include_app_start
            start_message="Checking scheduled updates...",
            on_result=self._on_bulk_update_check_completed,
            error_prefix="Scheduled update check failed",
            lock_widgets=[self.check_updates_button, self.check_all_updates_button, self.install_update_button],
        )

    def _check_due_updates_periodic(self) -> None:
        """Periodic timer callback — checks interval/daily/weekly programs that are due."""
        if self._active_jobs > 0:
            return  # A check is already running; skip this tick
        if not self.controller.list_programs():
            return
        self._start_task(
            self.controller.check_due_updates,
            False,  # exclude app_start — those only run at startup
            start_message="Checking scheduled updates...",
            on_result=self._on_bulk_update_check_completed,
            error_prefix="Scheduled update check failed",
            lock_widgets=[],  # background; don't block the UI buttons
        )

    def _check_all_program_updates(self) -> None:
        self._start_task(
            self.controller.check_for_updates_all,
            start_message="Checking all managed programs for updates...",
            on_result=self._on_bulk_update_check_completed,
            error_prefix="Bulk update check failed",
            lock_widgets=[self.check_updates_button, self.check_all_updates_button, self.install_update_button],
        )

    def _on_bulk_update_check_completed(self, report: UpdateCheckReport) -> None:
        self._refresh_programs_table()
        parts = [f"Checked {len(report.checked)} program(s). {report.available_count} update(s) available."]
        if report.installed:
            parts.append(f"Installed {len(report.installed)}.")
        if report.downloaded:
            parts.append(f"{len(report.downloaded)} downloaded and ready to install.")
        if report.deferred:
            parts.append(f"{len(report.deferred)} postponed (program running).")
        if report.errors:
            parts.append(f"{len(report.errors)} check(s) failed; see each program's details.")
        self._set_status(" ".join(parts))
        for message in report.errors:
            log.info("Update check problem: %s", message)
        self._notify_update_report(report)

    def _notify_update_report(self, report: UpdateCheckReport) -> None:
        """Tray notifications for what the update modes did (manual mode stays silent)."""
        if not self.controller.settings.show_system_notifications or not self._tray_available():
            return
        lines: list[str] = []
        installed_ids = {p.program_id for p in report.installed}
        downloaded_ids = {p.program_id for p in report.downloaded}
        for program in report.installed:
            lines.append(f"Updated {program.name} to {program.version or 'the latest version'}.")
        for program in report.downloaded:
            lines.append(f"{program.name} {program.pending_update_version or ''} is downloaded and ready to install.".replace("  ", " "))
        for program in report.newly_available:
            if program.program_id in installed_ids or program.program_id in downloaded_ids:
                continue
            policy = program.update_policy
            if policy.update_mode == "manual" or not policy.notify_on_available_update:
                continue
            lines.append(f"{program.name} {program.latest_upstream_version or ''} is available.".replace("  ", " "))
        lines.extend(report.deferred)
        if not lines:
            return
        title = "Program updates" if len(lines) > 1 else "Program update"
        shown = lines[:4] + ([f"…and {len(lines) - 4} more"] if len(lines) > 4 else [])
        self._tray_icon.showMessage(title, "\n".join(shown), QSystemTrayIcon.MessageIcon.Information, 8000)

    def _install_selected_program_update(self) -> None:
        program = self._get_selected_program()
        if not program:
            self._warn("Select a managed program first.")
            return

        self._start_task(
            self.controller.install_available_update,
            program.program_id,
            start_message=f"Installing update for {program.name}...",
            on_result=self._on_update_install_completed,
            error_prefix="Update install failed",
            lock_widgets=[self.check_updates_button, self.check_all_updates_button, self.install_update_button],
        )

    def _on_update_install_completed(self, updated_program: ManagedProgram) -> None:
        self._refresh_programs_table()
        self._set_status(f"Updated {updated_program.name} to {updated_program.version or 'latest release'}.")

    def _remove_selected_program(self) -> None:
        program = self._get_selected_program()
        if not program:
            self._warn("Select a managed program first.")
            return

        msg = QMessageBox(self)
        msg.setWindowTitle("Remove from manager")
        msg.setText(f"Remove <b>{program.name}</b> from the manager?")
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
            delete_files = False
        elif clicked == delete_btn:
            delete_files = True
        else:
            return

        self._start_task(
            self.controller.remove_program,
            program.program_id,
            delete_files,
            start_message=f"Removing {program.name}...",
            on_result=self._on_program_removed,
            error_prefix="Remove failed",
            lock_widgets=[self.remove_button, self.run_button, self.install_update_button],
        )

    def _on_program_removed(self, outcome: RemoveOutcome) -> None:
        self._refresh_programs_table()
        if outcome.deleted_files:
            self._set_status(f"Removed {outcome.program_name} and deleted its install folder.")
        else:
            self._set_status(f"Removed {outcome.program_name} from manager.")
        for warning in outcome.warnings:
            self._warn(warning)

    def _open_selected_program_folder(self) -> None:
        program = self._get_selected_program()
        if not program:
            self._warn("Select a managed program first.")
            return
        self._open_folder(program.install_dir)

    def _edit_selected_program(self) -> None:
        program = self._get_selected_program()
        if not program:
            self._warn("Select a managed program first.")
            return

        dialog = EditProgramDialog(program, self)
        if dialog.exec() != QDialog.Accepted:
            return

        self.controller.edit_program(
            program_id=program.program_id,
            name=dialog.get_name(),
            notes=dialog.get_notes(),
            launch_path=dialog.get_launch_path(),
            launch_args=dialog.get_launch_args(),
            working_directory_override=dialog.get_working_directory(),
            update_policy=dialog.get_update_policy(),
            run_as_admin=dialog.get_run_as_admin(),
        )
        self._refresh_programs_table()
        self._set_status(f"Saved changes to {dialog.get_name()}.")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.controller.settings, self)
        if dialog.exec() != QDialog.Accepted:
            return

        self.controller.update_settings(dialog.get_settings())
        self._set_status("Settings saved.")

    def _open_install_root(self) -> None:
        self._open_folder(self.controller.settings.install_root)

    # ------------------------------------------------------------------
    # Managed programs table helpers
    # ------------------------------------------------------------------

    def _filtered_programs(self) -> List[ManagedProgram]:
        value = self.program_filter_input.text().strip().lower()
        programs = self.controller.list_programs()
        if not value:
            return programs

        filtered: List[ManagedProgram] = []
        for program in programs:
            haystack = " | ".join(
                [
                    program.name,
                    program.version or "",
                    program.latest_upstream_version or "",
                    program.source_type,
                    program.source_value,
                    program.last_update_status,
                    program.notes or "",
                ]
            ).lower()
            if value in haystack:
                filtered.append(program)
        return filtered

    def _refresh_programs_table(self) -> None:
        table = self.programs_table
        selected_program_id = None
        current = self._get_selected_program(silent=True)
        if current:
            selected_program_id = current.program_id

        programs = self._filtered_programs()

        # Suppress signals during bulk update to avoid per-row selection events.
        table.blockSignals(True)
        table.setRowCount(len(programs))
        restore_row = None
        missing_color = QBrush(QColor(180, 60, 60))
        col_count = table.columnCount()
        for row, program in enumerate(programs):
            install_missing = not Path(program.install_dir).is_dir()
            install_dir_text = (
                f"{program.install_dir}  [folder missing]" if install_missing else program.install_dir
            )
            self._set_item(table, row, 0, program.name)
            self._set_item(table, row, 1, program.version or "")
            self._set_item(table, row, 2, program.latest_upstream_version or "")
            self._set_item(table, row, 3, self._format_update_status(program))
            self._set_item(table, row, 4, self._format_timestamp(program.last_checked_at))
            self._set_item(table, row, 5, self._format_update_policy(program))
            self._set_item(table, row, 6, program.source_type)
            self._set_item(table, row, 7, install_dir_text)
            self._set_item(table, row, 8, self._format_timestamp(program.last_run_at))
            self._set_item(table, row, 9, program.notes)
            table.item(row, 0).setData(Qt.UserRole, program.program_id)
            if install_missing:
                for col in range(col_count):
                    item = table.item(row, col)
                    if item:
                        item.setForeground(missing_color)
            if selected_program_id and selected_program_id == program.program_id:
                restore_row = row
        table.blockSignals(False)
        if restore_row is not None:
            table.selectRow(restore_row)
        self._update_program_details()

    def _update_program_details(self) -> None:
        program = self._get_selected_program(silent=True)
        if not program:
            self.program_details.setPlainText("Select a program to view details.")
            return

        details = [
            f"Name: {program.name}",
            f"Program ID: {program.program_id}",
            f"Source: {program.source_type}",
            f"Source Value: {program.source_value}",
            f"Source ID: {program.source_id or ''}",
            f"Repo: {program.repo_full_name or ''}",
            f"Version: {program.version or ''}",
            f"Latest Upstream Version: {program.latest_upstream_version or ''}",
            f"Update Available: {'Yes' if program.update_available else 'No'}",
            f"Update Status: {program.last_update_status}",
            f"Last Checked: {program.last_checked_at or ''}",
            f"Last Update Found: {program.last_update_found_at or ''}",
            f"Last Update Attempt: {program.last_update_attempt_at or ''}",
            f"Last Error: {program.last_error_message or ''}",
            f"Asset: {program.asset_name or ''}",
            f"Installed Asset Name: {program.installed_asset_name or ''}",
            f"Install Dir: {program.install_dir}",
            f"Launch Path: {program.launch_path or ''}",
            f"Homepage: {program.homepage_url or ''}",
            f"Installed At: {program.installed_at}",
            f"Last Run: {program.last_run_at or ''}",
            "",
            "Update Policy:",
            f"  Enabled: {'Yes' if program.update_policy.check_enabled else 'No'}",
            f"  Mode: {program.update_policy.update_mode}",
            f"  Schedule: {program.update_policy.schedule_type}",
            f"  Interval Hours: {program.update_policy.interval_hours}",
            f"  Channel: {program.update_policy.channel}",
            f"  Asset Override: {program.update_policy.asset_selection_override}",
            "",
            "Notes:",
            program.notes or "",
        ]
        self.program_details.setPlainText("\n".join(details))

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _get_selected_search_repo(self) -> GitHubRepo | None:
        row = self.search_results_table.currentRow()
        if row < 0 or row >= len(self._visible_search_results):
            return None
        return self._visible_search_results[row]

    def _get_selected_program(self, silent: bool = False) -> ManagedProgram | None:
        row = self.programs_table.currentRow()
        if row < 0:
            return None
        item = self.programs_table.item(row, 0)
        if not item:
            return None
        program_id = item.data(Qt.UserRole)
        if not program_id:
            return None
        try:
            return self.controller.get_program(program_id)
        except KeyError:
            if not silent:
                self._warn("The selected program could not be found.")
            return None

    # ------------------------------------------------------------------
    # Background task plumbing
    # ------------------------------------------------------------------

    def _start_task(
        self,
        fn: Any,
        *args: Any,
        start_message: str,
        on_result: Any,
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
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self._set_status(start_message)
        worker.signals.progress.connect(self._on_task_progress)
        worker.signals.result.connect(on_result)
        if silent_errors:
            worker.signals.error.connect(partial(self._log_task_error, error_prefix))
        else:
            worker.signals.error.connect(partial(self._on_task_error, error_prefix))
        worker.signals.finished.connect(partial(self._on_task_finished, locked, worker))
        self.runner.start(worker)

    def _log_task_error(self, prefix: str, error_message: str) -> None:
        log.warning("%s: %s", prefix, error_message)

    def _on_task_progress(self, value: int, message: str) -> None:
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(max(0, min(100, value)))
        self._set_status(message)

    def _on_task_error(self, prefix: str, error_message: str) -> None:
        log.warning("%s: %s", prefix, error_message)
        self._warn(f"{prefix}:\n\n{error_message}")

    def _on_task_finished(self, lock_widgets: list[QWidget], worker: TaskWorker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass  # already removed (duplicate finished signal)
        self._active_jobs = max(0, self._active_jobs - 1)
        for widget in lock_widgets:
            widget.setEnabled(True)
        if self._active_jobs == 0:
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
        """Create the QSystemTrayIcon and its context menu."""
        self._tray_icon = QSystemTrayIcon(make_tray_icon(), self)
        self._tray_icon.setToolTip("Portable Program Manager")

        self._tray_menu = QMenu(self)
        self._tray_show_action = self._tray_menu.addAction("Hide")
        self._tray_show_action.triggered.connect(self._toggle_window_visibility)
        self._tray_menu.addSeparator()
        quit_action = self._tray_menu.addAction("Quit")
        quit_action.triggered.connect(self._quit_app)

        # Update the Show/Hide label just before the menu appears.
        self._tray_menu.aboutToShow.connect(self._update_tray_menu)

        self._tray_icon.setContextMenu(self._tray_menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

    def _update_tray_menu(self) -> None:
        """Flip the Show/Hide action label to reflect current window visibility."""
        self._tray_show_action.setText("Hide" if self.isVisible() else "Show")

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Left-click or double-click on the tray icon toggles the window."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_window_visibility()

    def _toggle_window_visibility(self) -> None:
        if self.isVisible():
            self._hide_to_tray()
        else:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        """Restore the window from the tray."""
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
                "The application is still running in the background.\n"
                "Click the tray icon to restore it.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

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
        self._check_timer.stop()
        self._tray_icon.hide()
        QApplication.instance().quit()

    @staticmethod
    def _tray_available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

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

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------

    def _open_folder(self, path: str) -> None:
        if not Path(path).is_dir():
            self._warn(f"Folder not found:\n{path}")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            self._warn(f"Could not open folder: {exc}")

    def _append_log(self, message: str) -> None:
        self.discover_log.append(message)

    def _warn(self, message: str) -> None:
        QMessageBox.warning(self, "Portable Program Manager", message)
        # Persist error status for 30 seconds so the user can read it.
        self.status.showMessage(message, 30_000)

    def _set_status(self, message: str) -> None:
        self.status.showMessage(message, 8000)

    def _card(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Card")
        return frame

    @staticmethod
    def _parse_api_timestamp(iso_str: str | None) -> datetime:
        """Parse a GitHub API timestamp, returning epoch for unparseable values."""
        if not iso_str:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    @lru_cache(maxsize=256)
    def _format_timestamp_cached(iso_str: str, now_minute: int) -> str:
        """Cached timestamp formatting keyed on the ISO string and current minute."""
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local_dt = dt.astimezone()
            now = datetime.now(timezone.utc).astimezone()
            delta = now - local_dt
            if delta.total_seconds() < 60:
                return "Just now"
            if delta.total_seconds() < 3600:
                minutes = int(delta.total_seconds() // 60)
                return f"{minutes}m ago"
            if delta.total_seconds() < 86400:
                hours = int(delta.total_seconds() // 3600)
                return f"{hours}h ago"
            if delta.days < 7:
                return f"{delta.days}d ago"
            return local_dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return iso_str

    @staticmethod
    def _format_timestamp(iso_str: str | None) -> str:
        """Format an ISO-8601 timestamp into a human-readable local time string."""
        if not iso_str:
            return ""
        # Cache key includes the current minute so relative times stay fresh.
        now = datetime.now(timezone.utc)
        now_minute = now.year * 525960 + now.month * 43800 + now.day * 1440 + now.hour * 60 + now.minute
        return MainWindow._format_timestamp_cached(iso_str, now_minute)

    @staticmethod
    def _format_update_policy(program: ManagedProgram) -> str:
        policy = program.update_policy
        if not policy.check_enabled:
            return "Disabled"
        if policy.schedule_type == "interval":
            return f"{policy.update_mode} / every {policy.interval_hours}h"
        return f"{policy.update_mode} / {policy.schedule_type}"

    @staticmethod
    def _format_update_status(program: ManagedProgram) -> str:
        if program.update_available:
            latest = f" ({program.latest_upstream_version})" if program.latest_upstream_version else ""
            return f"Update available{latest}"
        mapping = {
            "not_yet_checked": "Not yet checked",
            "installed": "Installed",
            "no_update": "Up to date",
            "update_available": "Update available",
            "updated": "Updated",
            "check_failed": "Check failed",
            "update_failed": "Update failed",
            "unsupported_source": "Unsupported source",
        }
        return mapping.get(program.last_update_status, program.last_update_status.replace("_", " ").title())

    @staticmethod
    def _set_item(table: QTableWidget, row: int, column: int, value: str) -> None:
        item = QTableWidgetItem(value)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        table.setItem(row, column, item)
