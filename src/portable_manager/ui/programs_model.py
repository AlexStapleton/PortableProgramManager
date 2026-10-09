"""Table model, filter proxy and display helpers for the Managed Programs tab.

The model holds copies of :class:`ManagedProgram` plus the live state the
controller does not know about (running processes, permission problems), and
turns them into the status text and colour shown in the table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from PySide6.QtCore import QAbstractTableModel, QFileInfo, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QIcon
from PySide6.QtWidgets import QFileIconProvider

from ..installer import normalize_dir
from ..models import ManagedProgram, UpdatePolicy
from .icons import make_window_icon
from .theme import status_color

NAME, VERSION, STATUS, LAST_RUN, SOURCE = range(5)
COLUMN_TITLES = ("Name", "Version", "Status", "Last run", "Source")

# Qt.UserRole returns the program id on every column; this role returns a
# Python value the proxy's lessThan compares directly.
PROGRAM_ID_ROLE = Qt.ItemDataRole.UserRole
SORT_ROLE = Qt.ItemDataRole.UserRole + 1


# ---------------------------------------------------------------------------
# Time, source and policy wording
# ---------------------------------------------------------------------------

def parse_iso(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp into an aware UTC datetime (None if unusable)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _relative_or_none(moment: datetime, now: datetime) -> str | None:
    seconds = (now - moment).total_seconds()
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = int(seconds // 86400)
    if days < 7:
        return f"{days}d ago"
    return None


def relative_time(value: str | None, now: datetime | None = None) -> str:
    """'Just now', '5m ago', '3h ago', '2d ago', or the local date for older values."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    now = now or datetime.now(timezone.utc)
    relative = _relative_or_none(moment, now)
    return relative if relative is not None else moment.astimezone().strftime("%Y-%m-%d")


def exact_time(value: str | None) -> str:
    moment = parse_iso(value)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else ""


def friendly_time(value: str | None, now: datetime | None = None) -> str:
    """Relative time with the exact local time in brackets, e.g. '3h ago (2026-10-09 14:05)'."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    now = now or datetime.now(timezone.utc)
    relative = _relative_or_none(moment, now)
    exact = moment.astimezone().strftime("%Y-%m-%d %H:%M")
    return exact if relative is None else f"{relative} ({exact})"


def source_label(program: ManagedProgram) -> str:
    if program.source_type == "github_repo":
        return f"GitHub · {program.repo_full_name or program.source_value}"
    if program.source_type == "direct_url":
        host = urlparse(program.source_value or "").hostname or program.source_value
        return f"Direct download · {host}"
    return (program.source_type or "Unknown").replace("_", " ").capitalize()


_SCHEDULE_WORDS = {
    "app_start": "Checks when the app starts",
    "daily": "Checks daily",
    "weekly": "Checks weekly",
}
_MODE_WORDS = {
    "notify_only": "notifies you",
    "download_only": "downloads, you install",
    "install_automatically": "installs automatically",
}


def policy_summary(policy: UpdatePolicy) -> str:
    """Update policy in plain words, e.g. 'Checks daily · notifies you'."""
    if not policy.check_enabled:
        return "Update checks are off"
    if policy.update_mode == "manual":
        return "Checks only when you click Check"
    if policy.schedule_type == "interval":
        hours = policy.interval_hours
        schedule = f"Checks every {hours} hour{'s' if hours != 1 else ''}"
    else:
        schedule = _SCHEDULE_WORDS.get(policy.schedule_type, "Checks automatically")
    mode = _MODE_WORDS.get(policy.update_mode, "")
    return f"{schedule} · {mode}" if mode else schedule


# ---------------------------------------------------------------------------
# Status text, colour and priority
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatusInfo:
    text: str
    kind: str  # a theme.status_color kind
    tooltip: str = ""


def program_status(
    program: ManagedProgram,
    *,
    folder_missing: bool,
    running: Iterable[str] = (),
    permission_problem: bool = False,
) -> StatusInfo:
    """The one status line a program shows, most urgent condition first."""
    running = list(running)
    latest = program.latest_upstream_version
    error = program.last_error_message or ""
    if permission_problem:
        return StatusInfo(
            "Needs permission repair",
            "error",
            "Some files can't be opened by your Windows account. Use Repair permissions to fix it.",
        )
    if folder_missing:
        return StatusInfo("Folder missing", "error", f"Install folder not found:\n{program.install_dir}")
    if running:
        return StatusInfo("Running", "running", "Running: " + ", ".join(running))
    pending = program.pending_update_version
    if pending and (not latest or pending == latest):
        return StatusInfo("Update downloaded — ready to install", "update", f"Version {pending} is downloaded.")
    if program.update_available:
        return StatusInfo(f"Update available ({latest})" if latest else "Update available", "update")
    status = program.last_update_status
    if status == "check_failed":
        return StatusInfo("Check failed", "error", error)
    if status == "update_failed":
        return StatusInfo("Update failed", "error", error)
    if status == "no_compatible_asset":
        return StatusInfo("No Windows build in latest release", "warning", error)
    if status == "no_update":
        return StatusInfo("Up to date", "ok")
    if status == "updated":
        return StatusInfo("Updated", "ok")
    if status == "installed":
        return StatusInfo("Installed", "ok")
    if status == "unsupported_source":
        return StatusInfo("Updates not tracked", "muted", error)
    if status == "not_yet_checked":
        return StatusInfo("Not checked yet", "muted")
    return StatusInfo((status or "Unknown").replace("_", " ").capitalize(), "muted")


def _version_key(text: str) -> tuple:
    """Natural sort key: '1.10' sorts after '1.9'."""
    parts = [part for part in re.split(r"(\d+)", text.lower()) if part]
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class ProgramsModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._programs: list[ManagedProgram] = []
        self._missing: set[str] = set()          # program ids whose install folder is gone
        self._permission: set[str] = set()       # program ids with unreadable files
        self._running_raw: dict[str, list[str]] = {}
        self._running: dict[str, list[str]] = {}  # program id -> running process labels
        self._icons: dict[str, QIcon] = {}
        self._file_icons: QFileIconProvider | None = None
        self._generic_icon: QIcon | None = None

    # -- updates from the tab ------------------------------------------------

    def set_programs(self, programs: Iterable[ManagedProgram]) -> None:
        self.beginResetModel()
        self._programs = list(programs)
        self._missing = {p.program_id for p in self._programs if not Path(p.install_dir).is_dir()}
        self._running = self._compute_running(self._running_raw)
        self.endResetModel()

    def set_running(self, running: dict[str, list[str]]) -> bool:
        """Apply a ``fsops.running_programs_by_folder`` result. Returns True if anything changed."""
        new = self._compute_running(running)
        self._running_raw = dict(running)
        if new == self._running:
            return False
        self._running = new
        self._emit_status_changed()
        return True

    def set_permission_problems(self, program_ids: set[str]) -> None:
        ids = set(program_ids)
        if ids == self._permission:
            return
        self._permission = ids
        self._emit_status_changed()

    def _compute_running(self, running: dict[str, list[str]]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for program in self._programs:
            processes = running.get(normalize_dir(program.install_dir))
            if processes:
                found[program.program_id] = list(processes)
        return found

    def _emit_status_changed(self) -> None:
        if self._programs:
            self.dataChanged.emit(
                self.index(0, STATUS),
                self.index(len(self._programs) - 1, STATUS),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.ToolTipRole],
            )

    # -- queries used by the tab, the proxy and the details view ----------------

    def programs(self) -> list[ManagedProgram]:
        return list(self._programs)

    def program_at(self, row: int) -> ManagedProgram:
        return self._programs[row]

    def row_of(self, program_id: str) -> int:
        for row, program in enumerate(self._programs):
            if program.program_id == program_id:
                return row
        return -1

    def install_dirs(self) -> list[str]:
        return [p.install_dir for p in self._programs]

    def running_for(self, program_id: str) -> list[str]:
        return list(self._running.get(program_id, []))

    def is_missing(self, program_id: str) -> bool:
        return program_id in self._missing

    def is_permission_problem(self, program_id: str) -> bool:
        return program_id in self._permission

    def status_for_row(self, row: int) -> StatusInfo:
        program = self._programs[row]
        return program_status(
            program,
            folder_missing=program.program_id in self._missing,
            running=self._running.get(program.program_id, []),
            permission_problem=program.program_id in self._permission,
        )

    def update_count(self) -> int:
        return sum(1 for p in self._programs if p.update_available or p.pending_update_version)

    def search_text(self, row: int) -> str:
        """Lower-cased text the filter matches against for one row."""
        program = self._programs[row]
        return " | ".join(
            [
                program.name,
                program.version or "",
                program.latest_upstream_version or "",
                self.status_for_row(row).text,
                source_label(program),
                program.notes or "",
            ]
        ).lower()

    # -- QAbstractTableModel -----------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._programs)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMN_TITLES)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMN_TITLES[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._programs):
            return None
        row, column = index.row(), index.column()
        program = self._programs[row]

        if role == PROGRAM_ID_ROLE:
            return program.program_id

        if role == Qt.ItemDataRole.DisplayRole:
            if column == NAME:
                return program.name
            if column == VERSION:
                return program.version or ""
            if column == STATUS:
                return self.status_for_row(row).text
            if column == LAST_RUN:
                return relative_time(program.last_run_at)
            return source_label(program)

        if role == SORT_ROLE:
            if column == NAME:
                return program.name.lower()
            if column == VERSION:
                return _version_key(program.version or "")
            if column == STATUS:
                return self.status_for_row(row).text.lower()
            if column == LAST_RUN:
                return parse_iso(program.last_run_at) or _EPOCH
            return source_label(program).lower()

        if role == Qt.ItemDataRole.DecorationRole and column == NAME:
            return self._icon_for(program)

        if role == Qt.ItemDataRole.ToolTipRole:
            if column == NAME:
                if program.program_id in self._missing:
                    return f"{program.install_dir}\n(folder missing)"
                return program.install_dir
            if column == VERSION and program.latest_upstream_version:
                return f"Latest release: {program.latest_upstream_version}"
            if column == STATUS:
                return self.status_for_row(row).tooltip or None
            if column == LAST_RUN:
                return exact_time(program.last_run_at) or None
            if column == SOURCE:
                return program.source_value
            return None

        if role == Qt.ItemDataRole.ForegroundRole:
            if column == STATUS:
                return QBrush(status_color(self.status_for_row(row).kind))
            if program.program_id in self._missing:
                return QBrush(status_color("muted"))
            return None

        return None

    # -- icons -------------------------------------------------------------------

    def _icon_for(self, program: ManagedProgram) -> QIcon:
        key = program.launch_path or ""
        cached = self._icons.get(key)
        if cached is not None:
            return cached
        icon: QIcon | None = None
        if key and Path(key).is_file():
            if self._file_icons is None:
                self._file_icons = QFileIconProvider()
            candidate = self._file_icons.icon(QFileInfo(key))
            if not candidate.isNull():
                icon = candidate
        if icon is None:
            if self._generic_icon is None:
                self._generic_icon = make_window_icon()
            icon = self._generic_icon
        self._icons[key] = icon
        return icon


class ProgramsFilterProxy(QSortFilterProxyModel):
    """Case-insensitive text filter plus natural sorting on every column."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._needle = ""
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setDynamicSortFilter(True)

    def filter_text(self) -> str:
        return self._needle

    def set_filter_text(self, text: str) -> None:
        needle = text.strip().lower()
        if needle == self._needle:
            return
        self.beginFilterChange()
        self._needle = needle
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._needle:
            return True
        model = self.sourceModel()
        if not isinstance(model, ProgramsModel):
            return True
        return self._needle in model.search_text(source_row)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a = left.data(SORT_ROLE)
        b = right.data(SORT_ROLE)
        if a is None or b is None:
            return False
        try:
            return a < b
        except TypeError:
            return False
