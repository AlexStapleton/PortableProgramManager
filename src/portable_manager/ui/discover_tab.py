"""The Discover tab: search GitHub for portable Windows apps and install them.

The tab never blocks the UI: searches, release lookups and installs go through
``host.start_task``. Search results are tagged with a generation number so a
slow answer for an old search cannot replace the results of a newer one.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..controller import InstallOutcome
from ..github_client import GitHubRepo
from ..models import ManagedProgram
from .host import TaskHost
from .theme import status_color


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

_ID_ROLE = int(Qt.ItemDataRole.UserRole)   # column-0 item: repository full name
_SORT_ROLE = _ID_ROLE + 1                  # numeric/date sort key, when set

COL_NAME, COL_REPO, COL_STARS, COL_RELEASE, COL_WINDOWS, COL_INSTALLED, COL_DESC = range(7)
_HEADERS = ["Name", "Repository", "Stars", "Latest release", "Windows build", "Installed", "Description"]

_EMPTY_TEXT = "Search for a program to see results here."


# ---------------------------------------------------------------------------
# Pure helpers (no widgets) -- filtering, sorting keys and display text
# ---------------------------------------------------------------------------


def looks_windows_compatible(repo: GitHubRepo) -> bool:
    """Return True unless the repo appears to target a non-Windows platform.

    After release info has been fetched, uses the definitive
    ``has_windows_release`` flag.  Before that, falls back to heuristic
    text matching on repo name + description.
    """
    # Definitive answer available after background release fetch.
    if repo.has_windows_release is not None:
        # A release exists with Windows assets -> keep; a release exists with
        # none -> hide. No release at all: we cannot judge, so keep it.
        if not repo.latest_release_at:
            return True
        return repo.has_windows_release

    # Heuristic fallback: check repo name + description for platform terms.
    haystack = f"{repo.full_name} {repo.description}".lower()
    has_non_windows = any(term in haystack for term in _NON_WINDOWS_TERMS)
    if not has_non_windows:
        return True
    # If it also mentions Windows / cross-platform, keep it.
    return any(term in haystack for term in _WINDOWS_POSITIVE_TERMS)


def best_date(repo: GitHubRepo) -> str:
    """The most meaningful date for a repo: release > pushed > updated."""
    return repo.latest_release_at or repo.pushed_at or repo.updated_at or ""


def _parse_iso(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_api_timestamp(iso_str: str | None) -> datetime:
    """Parse a GitHub API timestamp, returning the minimum date for unparseable values."""
    return _parse_iso(iso_str) or datetime.min.replace(tzinfo=timezone.utc)


def apply_filters(
    results: list[GitHubRepo],
    *,
    windows_only: bool,
    inactive_days: int | None,
    forks_with_changes_only: bool = False,
    now: datetime | None = None,
) -> list[GitHubRepo]:
    """Return the repos that pass the Windows, inactivity and fork filters, in order.

    ``inactive_days=None`` disables the inactivity filter. With
    ``forks_with_changes_only``, forks known to have no commits beyond their
    original are hidden (forks not compared yet stay visible until they are).
    """
    filtered = list(results)
    if forks_with_changes_only:
        filtered = [repo for repo in filtered if not (repo.is_fork and repo.ahead_by is not None and repo.ahead_by < 1)]
    if windows_only:
        filtered = [repo for repo in filtered if looks_windows_compatible(repo)]
    if inactive_days is not None:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=inactive_days)
        filtered = [repo for repo in filtered if parse_api_timestamp(best_date(repo)) >= cutoff]
    return filtered


def fork_label(repo: GitHubRepo) -> tuple[str, str]:
    """Repository column text and tooltip; forks show how far they are from the original."""
    if not repo.is_fork:
        return repo.full_name, repo.html_url
    if repo.ahead_by is None:
        return (
            f"{repo.full_name} · fork",
            "Fork — not compared with the original (still loading, API limit reached, "
            f"or the site can't compare forks).\n{repo.html_url}",
        )
    text = f"{repo.full_name} · fork +{repo.ahead_by} / −{repo.behind_by or 0}"
    tooltip = (
        f"Fork of {repo.parent_full_name}: {repo.ahead_by} commit(s) ahead, "
        f"{repo.behind_by or 0} behind.\n{repo.html_url}"
    )
    return text, tooltip


def format_stars(stars: int) -> str:
    """'1,204' below 10,000 and '48.2k' above, so long numbers stay short."""
    if stars < 10_000:
        return f"{stars:,}"
    text = f"{stars / 1000:.1f}".removesuffix(".0")
    return f"{text}k"


def _ago(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'} ago"


def format_relative(iso_str: str | None, now: datetime | None = None) -> str:
    """A friendly relative date such as '3 days ago' ('' when unknown)."""
    dt = _parse_iso(iso_str)
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return _ago(minutes, "minute")
    hours = minutes // 60
    if hours < 24:
        return _ago(hours, "hour")
    days = hours // 24
    if days < 7:
        return _ago(days, "day")
    if days < 30:
        return _ago(days // 7, "week")
    if days < 365:
        return _ago(days // 30, "month")
    return _ago(days // 365, "year")


def _absolute_local(iso_str: str | None) -> str:
    dt = _parse_iso(iso_str)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("Card")
    return frame


def _cell(
    text: str,
    *,
    sort_key: float | None = None,
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    tooltip: str = "",
    color: QColor | None = None,
) -> "_SortableItem":
    item = _SortableItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    item.setTextAlignment(align)
    if sort_key is not None:
        item.setData(_SORT_ROLE, sort_key)
    if tooltip:
        item.setToolTip(tooltip)
    if color is not None:
        item.setForeground(QBrush(color))
    return item


class _SortableItem(QTableWidgetItem):
    """Table item that sorts by its numeric/date sort key when it has one."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine = self.data(_SORT_ROLE)
        theirs = other.data(_SORT_ROLE)
        if mine is None or theirs is None:
            return super().__lt__(other)
        return mine < theirs


class _GatedButton(QPushButton):
    """A button that stays disabled while its gate is False.

    The host disables widgets when a task starts and re-enables them when it
    ends, which would also re-enable a button that has no selection. This
    button remembers what the tab asked for and combines it with the gate.
    """

    def __init__(self, text: str, gate: Callable[[], bool], parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._requested = True
        self._gate = gate

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt naming
        self._requested = bool(enabled)
        self.refresh()

    def refresh(self) -> None:
        """Re-evaluate the gate (call after anything the gate depends on changes)."""
        QPushButton.setEnabled(self, self._requested and self._gate())


# ---------------------------------------------------------------------------
# The tab
# ---------------------------------------------------------------------------


class DiscoverTab(QWidget):
    """Search GitHub, filter the results, and install a program from them."""

    program_installed = Signal(object)  # the installed ManagedProgram

    def __init__(self, host: TaskHost, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._host = host
        self._controller = host.controller
        self.search_results: list[GitHubRepo] = []   # all results of the current search
        self._visible: list[GitHubRepo] = []         # rows currently in the table
        self._search_generation = 0                  # bumped by every search; stale answers are dropped
        self._shown_query: str | None = None         # query whose results are in search_results

        self._build_ui()
        self._sync_sort_indicator()
        self._sync_action_buttons()
        self._update_stack()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)  # same as the Programs tab
        root.setSpacing(12)

        heading = QLabel("Discover")
        heading.setProperty("role", "heading")
        root.addWidget(heading)

        subtitle = QLabel("Search GitHub, GitLab, Codeberg and your own sources for portable Windows apps, or install from a link.")
        subtitle.setProperty("role", "subtle")
        root.addWidget(subtitle)

        root.addWidget(self._build_search_card(), 1)
        root.addWidget(self._build_link_card())
        root.addWidget(self._build_activity_section())

    def _build_search_card(self) -> QFrame:
        card = _card()
        layout = QVBoxLayout(card)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search — e.g. markdown editor portable")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(self._search)
        self.search_button = QPushButton("Search")
        self.search_button.setProperty("variant", "primary")
        self.search_button.clicked.connect(self._search)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Sort by"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Most stars", "stars")
        self.sort_combo.addItem("Recently updated", "updated")
        self.sort_combo.setToolTip("Changing the sort searches GitHub again if there is a query.")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        filter_row.addWidget(self.sort_combo)

        filter_row.addSpacing(16)

        self.windows_only_check = QCheckBox("Windows builds only")
        self.windows_only_check.setChecked(True)
        self.windows_only_check.setToolTip(
            "Hide repositories that appear to target non-Windows platforms.\n"
            "Checks the repo name and description for platform indicators\n"
            "(e.g. macos, android, linux-only, ubuntu, flatpak, etc.), and the\n"
            "latest release's downloads once they are known.\n"
            "Cross-platform repos that also mention Windows are kept."
        )
        self.windows_only_check.toggled.connect(self._on_filter_changed)
        filter_row.addWidget(self.windows_only_check)

        filter_row.addSpacing(16)

        self.hide_inactive_check = QCheckBox("Hide projects inactive for more than")
        self.hide_inactive_check.setToolTip("Hide repositories not updated within the given number of days.")
        self.hide_inactive_check.toggled.connect(self._on_filter_changed)
        filter_row.addWidget(self.hide_inactive_check)

        self.inactive_days_spin = QSpinBox()
        self.inactive_days_spin.setRange(30, 3650)
        self.inactive_days_spin.setValue(365)
        self.inactive_days_spin.setSuffix(" days")
        self.inactive_days_spin.setEnabled(False)
        self.inactive_days_spin.valueChanged.connect(self._on_filter_changed)
        self.hide_inactive_check.toggled.connect(self.inactive_days_spin.setEnabled)
        filter_row.addWidget(self.inactive_days_spin)

        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        fork_row = QHBoxLayout()
        self.include_forks_check = QCheckBox("Include forks")
        self.include_forks_check.setToolTip(
            "Also search forks: copies of a project that someone else maintains.\n"
            "GitHub hides them from search unless asked. Changing this searches again."
        )
        self.include_forks_check.toggled.connect(self._on_include_forks_changed)
        fork_row.addWidget(self.include_forks_check)
        fork_row.addSpacing(16)
        self.forks_with_changes_check = QCheckBox("Only forks with changes")
        self.forks_with_changes_check.setChecked(True)
        self.forks_with_changes_check.setEnabled(False)
        self.forks_with_changes_check.setToolTip(
            "Hide forks that have no commits of their own (at least 1 commit ahead of the original)."
        )
        self.forks_with_changes_check.toggled.connect(self._on_filter_changed)
        fork_row.addWidget(self.forks_with_changes_check)
        fork_row.addSpacing(16)
        fork_row.addWidget(QLabel("Search in"))
        self.source_combo = QComboBox()
        self.source_combo.setToolTip(
            "Which sites to search. Add or change sites in Settings → Sources."
        )
        self.reload_sources()
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        fork_row.addWidget(self.source_combo)
        fork_row.addStretch(1)
        layout.addLayout(fork_row)

        self._stack = QStackedWidget()
        self._empty_label = QLabel()
        self._empty_label.setProperty("role", "subtle")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._stack.addWidget(self._empty_label)
        self._stack.addWidget(self._build_results_table())
        layout.addWidget(self._stack, 1)

        action_row = QHBoxLayout()
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("Stable release", "latest_release")
        self.channel_combo.addItem("Pre-release", "prerelease")
        self.channel_combo.setToolTip("Which release channel to install from")
        self.install_button = _GatedButton("Install", gate=self._has_selection)
        self.install_button.setProperty("variant", "primary")
        self.install_button.setToolTip("Install the selected repository (or double-click a row).")
        self.install_button.clicked.connect(self._install_selected)
        self.open_button = _GatedButton("Open project page", gate=self._has_selection)
        self.open_button.setProperty("variant", "secondary")
        self.open_button.clicked.connect(self._open_selected_page)
        action_row.addWidget(self.channel_combo)
        action_row.addWidget(self.install_button)
        action_row.addWidget(self.open_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        return card

    def _build_results_table(self) -> QTableWidget:
        table = QTableWidget(0, len(_HEADERS))
        self.results_table = table
        table.setHorizontalHeaderLabels(_HEADERS)
        table.verticalHeader().hide()
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)  # colours come from the theme palette
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._show_row_menu)
        table.itemSelectionChanged.connect(self._sync_action_buttons)
        table.activated.connect(self._on_row_activated)

        header = table.horizontalHeader()
        for column in range(len(_HEADERS)):
            mode = QHeaderView.ResizeMode.Stretch if column == COL_DESC else QHeaderView.ResizeMode.ResizeToContents
            header.setSectionResizeMode(column, mode)

        table.setSortingEnabled(True)
        return table

    def _build_link_card(self) -> QFrame:
        card = _card()
        layout = QVBoxLayout(card)
        title = QLabel("Install from a link")
        title.setProperty("role", "subtle")
        layout.addWidget(title)

        row = QHBoxLayout()
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText(
            "Paste a GitHub project link or a direct download link (.zip, .7z, .exe)"
        )
        self.link_input.setClearButtonEnabled(True)
        self.link_input.returnPressed.connect(self._install_from_link)
        self.link_button = QPushButton("Install from link")
        self.link_button.setProperty("variant", "secondary")
        self.link_button.clicked.connect(self._install_from_link)
        row.addWidget(self.link_input, 1)
        row.addWidget(self.link_button)
        layout.addLayout(row)
        return card

    def _build_activity_section(self) -> QWidget:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)

        self.log_toggle = QToolButton()
        self.log_toggle.setText("Activity ▸")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setChecked(False)
        self.log_toggle.setAutoRaise(True)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.log_toggle.toggled.connect(self._on_log_toggled)
        layout.addWidget(self.log_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.activity_log = QPlainTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setMaximumBlockCount(500)
        self.activity_log.setMaximumHeight(140)
        self.activity_log.setPlaceholderText("Searches and installs will be listed here.")
        self.activity_log.setVisible(False)
        layout.addWidget(self.activity_log)
        return section

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def focus_search(self) -> None:
        """Put the keyboard focus in the search box, ready to type a query."""
        self.search_input.setFocus()
        self.search_input.selectAll()

    # ------------------------------------------------------------------
    # Search and filters
    # ------------------------------------------------------------------

    def _search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            self._host.warn("Enter a search term.")
            return
        self._search_generation += 1
        generation = self._search_generation
        self._host.start_task(
            self._controller.search_github,
            query,
            self.sort_combo.currentData(),
            self.include_forks_check.isChecked(),
            self.source_combo.currentData(),
            start_message=f"Searching for '{query}'...",
            on_result=partial(self._on_search_completed, generation, query),
            error_prefix="Search failed",
            lock_widgets=self._locked_widgets(),
        )

    def _on_search_completed(self, generation: int, query: str, results: list[GitHubRepo]) -> None:
        if generation != self._search_generation:
            return  # a newer search has started since this one was sent
        self.search_results = list(results)
        self._shown_query = query
        self._populate(self._visible_after_filters())
        self._append_log(f"Search for '{query}' returned {len(results)} repositories{self._hidden_suffix()}.")
        self._report_counts()
        if results:
            self._host.start_task(
                self._controller.fetch_release_dates,
                results,
                start_message="Fetching latest release dates...",
                on_result=partial(self._on_release_dates_fetched, generation),
                error_prefix="Release date fetch failed",
            )

    def _on_release_dates_fetched(self, generation: int, repos: list[GitHubRepo]) -> None:
        """Replace the results with the enriched copies, if they are still current."""
        if generation != self._search_generation:
            return
        if [r.full_name for r in repos] != [r.full_name for r in self.search_results]:
            return
        self.search_results = list(repos)
        self._populate(self._visible_after_filters())
        self._report_counts()
        uncompared = sum(1 for r in repos if r.is_fork and r.ahead_by is None)
        if uncompared:
            message = (
                f"{uncompared} fork(s) couldn't be compared with the original, so they are shown "
                "unfiltered. Either the site's API limit was reached (add a token in Settings → "
                "Sources) or the site can't compare forks (Codeberg/Gitea servers often can't)."
            )
            self._host.set_status(message)
            self._append_log(message)

    def _on_filter_changed(self, *_: object) -> None:
        """Re-apply the filters to the current results without a new search."""
        if not self.search_results:
            return
        self._populate(self._visible_after_filters())
        self._report_counts()

    def reload_sources(self) -> None:
        """Refill "Search in" from the user's sources (call after Settings changes)."""
        current = self.source_combo.currentData() if self.source_combo.count() else None
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("All my sources", None)
        settings = getattr(self._controller, "settings", None)
        for source in getattr(settings, "sources", None) or []:
            if source.enabled:
                self.source_combo.addItem(source.name, source.id)
        index = self.source_combo.findData(current)
        self.source_combo.setCurrentIndex(max(0, index))
        self.source_combo.blockSignals(False)

    def _on_source_changed(self, *_: object) -> None:
        if self.search_input.text().strip():
            self._search()

    def _source_name(self, source_id: str) -> str:
        names = getattr(self._controller, "source_names", None)
        return (names() if callable(names) else {}).get(source_id, source_id)

    def _on_include_forks_changed(self, checked: bool) -> None:
        """Forks come from a different GitHub query, so search again."""
        self.forks_with_changes_check.setEnabled(checked)
        if self.search_input.text().strip():
            self._search()

    def _on_sort_changed(self, *_: object) -> None:
        """'Recently updated' asks GitHub for activity order, so it searches again."""
        self._sync_sort_indicator()
        query = self.search_input.text().strip()
        if query:
            self._search()
        elif self.search_results:
            self._populate(self._visible_after_filters())

    def _sync_sort_indicator(self) -> None:
        column = COL_RELEASE if self.sort_combo.currentData() == "updated" else COL_STARS
        self.results_table.horizontalHeader().setSortIndicator(column, Qt.SortOrder.DescendingOrder)

    def _visible_after_filters(self) -> list[GitHubRepo]:
        return apply_filters(
            self.search_results,
            windows_only=self.windows_only_check.isChecked(),
            inactive_days=self.inactive_days_spin.value() if self.hide_inactive_check.isChecked() else None,
            forks_with_changes_only=(
                self.include_forks_check.isChecked() and self.forks_with_changes_check.isChecked()
            ),
        )

    def _hidden_suffix(self) -> str:
        hidden = len(self.search_results) - len(self._visible)
        return f" ({hidden} hidden by filters)" if hidden else ""

    def _report_counts(self) -> None:
        self._host.set_status(
            f"Showing {len(self._visible)} of {len(self.search_results)} results{self._hidden_suffix()}."
        )

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _populate(self, repos: list[GitHubRepo]) -> None:
        table = self.results_table
        selected = self._selected_repo()
        selected_name = selected.full_name if selected else None

        self._visible = list(repos)
        table.setSortingEnabled(False)  # rows would move while they are being filled
        table.setRowCount(len(repos))
        for row, repo in enumerate(repos):
            self._fill_row(row, repo)
        table.setSortingEnabled(True)  # re-sorts by the header's current indicator

        if selected_name:
            for row in range(table.rowCount()):
                item = table.item(row, COL_NAME)
                if item is not None and item.data(_ID_ROLE) == selected_name:
                    table.selectRow(row)
                    break
        self._update_stack()
        self._sync_action_buttons()

    def _fill_row(self, row: int, repo: GitHubRepo) -> None:
        table = self.results_table
        name = _cell(repo.name, tooltip=repo.html_url)
        name.setData(_ID_ROLE, repo.full_name)
        table.setItem(row, COL_NAME, name)
        repo_text, repo_tip = fork_label(repo)
        if repo.source_id and repo.source_id != "github":
            repo_text = f"{self._source_name(repo.source_id)} · {repo_text}"
        table.setItem(row, COL_REPO, _cell(repo_text, tooltip=repo_tip))

        table.setItem(row, COL_STARS, _cell(
            format_stars(repo.stars),
            sort_key=int(repo.stars),
            align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        ))

        table.setItem(row, COL_RELEASE, self._release_cell(repo))
        table.setItem(row, COL_WINDOWS, self._windows_cell(repo))

        installed = self._find_installed(repo.full_name, repo.source_id) is not None
        table.setItem(row, COL_INSTALLED, _cell(
            "✓" if installed else "",
            sort_key=1 if installed else 0,
            align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            tooltip="Already installed" if installed else "",
            color=status_color("ok") if installed else None,
        ))

        table.setItem(row, COL_DESC, _cell(
            repo.description or "",
            tooltip=repo.description or "No description",
        ))

    def _release_cell(self, repo: GitHubRepo) -> _SortableItem:
        # The sort key is the same date as the "Recently updated" order.
        sort_key = parse_api_timestamp(best_date(repo)).timestamp()
        align = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        if repo.latest_release_at:
            return _cell(
                format_relative(repo.latest_release_at),
                sort_key=sort_key,
                align=align,
                tooltip=f"Latest release: {_absolute_local(repo.latest_release_at)}",
            )
        if repo.has_windows_release is None:
            return _cell("…", sort_key=sort_key, align=align, color=status_color("muted"),
                         tooltip="Checking releases...")
        return _cell(
            "—",
            sort_key=sort_key,
            align=align,
            color=status_color("muted"),
            tooltip=f"No GitHub release published yet. Last pushed: {_absolute_local(repo.pushed_at) or 'unknown'}",
        )

    @staticmethod
    def _windows_cell(repo: GitHubRepo) -> _SortableItem:
        if repo.has_windows_release is None:
            return _cell("…", sort_key=-1, color=status_color("muted"),
                         align=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         tooltip="Checking the latest release...")
        align = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        if not repo.latest_release_at:
            return _cell("—", sort_key=0, color=status_color("muted"), align=align,
                         tooltip="No GitHub release published, so there is no download to check")
        if repo.has_windows_release:
            return _cell("✓ Yes", sort_key=2, color=status_color("ok"), align=align,
                         tooltip="The latest release has a Windows download")
        return _cell("✗ No", sort_key=1, color=status_color("error"), align=align,
                     tooltip="The latest release has no Windows download")

    def _update_stack(self) -> None:
        if self._visible:
            self._stack.setCurrentWidget(self.results_table)
            return
        if self.search_results:
            count = len(self.search_results)
            noun = "result is" if count == 1 else "results are"
            text = (
                f"All {count} {noun} hidden by your filters.\n"
                "Turn off a filter above to see them."
            )
        elif self._shown_query:
            text = f"No repositories matched '{self._shown_query}'.\nTry different search words."
        else:
            text = _EMPTY_TEXT
        self._empty_label.setText(text)
        self._stack.setCurrentWidget(self._empty_label)

    def _selected_repo(self) -> GitHubRepo | None:
        items = self.results_table.selectedItems()
        if not items:
            return None
        name_item = self.results_table.item(items[0].row(), COL_NAME)
        if name_item is None:
            return None
        full_name = name_item.data(_ID_ROLE)
        return next((repo for repo in self._visible if repo.full_name == full_name), None)

    def _has_selection(self) -> bool:
        return self._selected_repo() is not None

    def _sync_action_buttons(self) -> None:
        self.install_button.refresh()
        self.open_button.refresh()

    def _locked_widgets(self) -> list[QWidget]:
        return [self.search_button, self.install_button, self.link_button]

    def _show_row_menu(self, pos: Any) -> None:
        item = self.results_table.itemAt(pos)
        if item is None:
            return
        self.results_table.selectRow(item.row())
        repo = self._selected_repo()
        if repo is None:
            return
        menu = QMenu(self)
        install = menu.addAction("Install")
        install.setEnabled(self.install_button.isEnabled())
        install.triggered.connect(self._install_selected)
        open_page = menu.addAction("Open project page")
        open_page.triggered.connect(self._open_selected_page)
        copy_url = menu.addAction("Copy repository URL")
        copy_url.triggered.connect(lambda: self._copy_repo_url(repo))
        menu.exec(self.results_table.viewport().mapToGlobal(pos))

    def _copy_repo_url(self, repo: GitHubRepo) -> None:
        QApplication.clipboard().setText(repo.html_url)
        self._host.set_status(f"Copied {repo.html_url}")

    def _on_row_activated(self, _index: object) -> None:
        # Double-click or Enter on a row. Ignored while an install is running
        # (the install button is disabled then).
        if not self.install_button.isEnabled():
            return
        self._install_selected()

    def _open_selected_page(self) -> None:
        repo = self._selected_repo()
        if repo is None:
            self._host.warn("Select a search result first.")
            return
        QDesktopServices.openUrl(QUrl(repo.html_url))

    # ------------------------------------------------------------------
    # Installs
    # ------------------------------------------------------------------

    def _install_selected(self) -> None:
        repo = self._selected_repo()
        if repo is None:
            self._host.warn("Select a search result first.")
            return
        self._install_repo(repo.full_name, repo.source_id or "github")

    def _find_installed(self, repo_full_name: str, source_id: str = "github") -> ManagedProgram | None:
        try:
            return self._controller.find_managed_program(repo_full_name=repo_full_name, source_id=source_id)
        except TypeError:  # controllers without multi-source support
            return self._controller.find_managed_program(repo_full_name=repo_full_name)

    def _install_repo(self, repo_full_name: str, source_id: str = "github") -> None:
        existing = self._find_installed(repo_full_name, source_id)
        if existing is not None and not self._confirm_reinstall(existing):
            return
        self._host.start_task(
            self._controller.install_from_repo,
            repo_full_name,
            self.channel_combo.currentData(),
            source_id,
            start_message=f"Installing {repo_full_name}...",
            on_result=partial(self._on_install_completed, f"Installed from {repo_full_name}"),
            error_prefix="Install failed",
            lock_widgets=self._locked_widgets(),
        )

    def _install_from_link(self) -> None:
        value = self.link_input.text().strip()
        if not value:
            self._host.warn("Paste a project link (GitHub, GitLab, Codeberg...) or a direct download link.")
            return

        match_url = getattr(self._controller, "match_url", None)
        if callable(match_url):
            match = match_url(value)
            source_id, repo_full_name = match if match else ("github", None)
        else:
            source_id, repo_full_name = "github", self._controller.github.extract_repo_full_name(value)
        if repo_full_name:
            self._install_repo(repo_full_name, source_id)
            return

        existing = self._controller.find_managed_program(url=value)
        if existing is not None and not self._confirm_reinstall(existing):
            return
        self._host.start_task(
            self._controller.install_from_url,
            value,
            start_message="Installing from direct link...",
            on_result=partial(self._on_install_completed, "Installed from direct link"),
            error_prefix="Install from link failed",
            lock_widgets=self._locked_widgets(),
        )

    def _confirm_reinstall(self, existing: ManagedProgram) -> bool:
        version = existing.version or existing.latest_upstream_version or "unknown version"
        reply = QMessageBox.question(
            self,
            "Already installed",
            f"{existing.name} is already installed (version {version}).\n\n"
            "Reinstall it with the latest release? Your settings for it are kept.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _on_install_completed(self, log_prefix: str, outcome: InstallOutcome) -> None:
        program = outcome.program
        self._host.refresh_programs()
        self._append_log(f"{log_prefix}: {program.name}.")
        if self.search_results:
            self._populate(self._visible_after_filters())  # refresh the "Installed" marks
        self.program_installed.emit(program)

        if outcome.is_likely_installer:
            self._host.set_status(
                f"Installed {program.name} — this looks like a setup/installer, not a portable app."
            )
            self._append_log(
                f"Note: {program.name} appears to be a setup/installer. "
                "You will need to run it manually to complete installation. "
                "Automatic update checks have been disabled for this program."
            )
            self._show_installer_dialog(program)
            return

        if program.launch_path:
            self._host.set_status(f"Installed {program.name}.")
        else:
            self._host.set_status(
                f"Installed {program.name} — no executable detected. Use Edit to set the launch path."
            )
            self._append_log(
                f"Warning: no executable found in {program.install_dir}. "
                "Set the launch path via the Edit dialog."
            )
            QMessageBox.information(
                self,
                "No program found",
                f"<b>{program.name}</b> was installed, but no program to start was found in its folder."
                "<br><br>Open it in <b>Managed Programs</b> and use <b>Edit</b> to choose the program to start.",
            )
        if self._controller.settings.auto_open_folder_after_install:
            self._open_folder(program.install_dir)

    def _show_installer_dialog(self, program: ManagedProgram) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Setup / Installer Detected")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<b>{program.name}</b> looks like a setup program (an installer) rather than a "
            "portable application.<br><br>"
            "Run the installer to finish setting it up, or open the folder to look at the files.<br><br>"
            "Automatic update checks have been <b>turned off</b> for this program, because the "
            "installed application probably updates itself. You can change this later in Edit."
        )
        run_button = None
        if program.launch_path:
            run_button = box.addButton("Run installer now", QMessageBox.AcceptRole)
        open_button = box.addButton("Open folder", QMessageBox.ActionRole)
        close_button = box.addButton("Close", QMessageBox.RejectRole)
        box.setDefaultButton(close_button)
        box.setEscapeButton(close_button)
        box.exec()

        clicked = box.clickedButton()
        if run_button is not None and clicked == run_button:
            self._host.start_task(
                self._controller.run_program,
                program.program_id,
                start_message=f"Starting {program.name}...",
                on_result=lambda started: self._host.set_status(f"Started {started.name}."),
                error_prefix=f"Couldn't start {program.name}",
            )
        elif clicked == open_button:
            self._open_folder(program.install_dir)

    def _open_folder(self, path: str) -> None:
        if not Path(path).is_dir():
            self._host.warn(f"Folder not found:\n{path}")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            self._host.warn(f"Could not open folder: {exc}")

    # ------------------------------------------------------------------
    # Activity log
    # ------------------------------------------------------------------

    def _on_log_toggled(self, expanded: bool) -> None:
        self.log_toggle.setText("Activity ▾" if expanded else "Activity ▸")
        self.activity_log.setVisible(expanded)

    def _append_log(self, message: str) -> None:
        self.activity_log.appendPlainText(f"{datetime.now():%H:%M}  {message}")
