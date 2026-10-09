from __future__ import annotations

import copy
import dataclasses
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import fsops
from ..errors import InstallError
from ..models import AppSettings, SourceConfig
from .theme import status_color

# (label, stored value) pairs shared with the program edit dialog.
UPDATE_MODE_CHOICES = [
    ("Show it in the list", "manual"),
    ("Notify me", "notify_only"),
    ("Download it, then ask", "download_only"),
    ("Install it automatically", "install_automatically"),
]
SCHEDULE_CHOICES = [
    ("When the app starts", "app_start"),
    ("Every N hours", "interval"),
    ("Daily", "daily"),
    ("Weekly", "weekly"),
]
THEME_CHOICES = [
    ("Match Windows", "system"),
    ("Light", "light"),
    ("Dark", "dark"),
]
# (label, SourceConfig.kind) pairs for a source's type, shared with the source dialog.
SOURCE_TYPE_CHOICES = [
    ("GitHub", "github"),
    ("GitLab", "gitlab"),
    ("Gitea / Forgejo / Codeberg", "gitea"),
]
SOURCE_TYPE_NAMES = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea · Forgejo · Codeberg"}
SOURCE_HEADERS = ["Name", "Type", "Address", "Token", "Enabled", "Include in search"]
_GITHUB_REMOVE_TIP = "GitHub is built in, so it can't be removed. Clear Enabled to stop using it."


# ----------------------------------------------------------------------
# Small helpers shared with edit_program_dialog
# ----------------------------------------------------------------------


def hbox(*widgets: QWidget) -> QWidget:
    """A row of widgets with no margins."""
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    return holder


def subtle_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "subtle")
    label.setWordWrap(True)
    return label


def make_button(text: str, variant: str = "secondary", slot=None) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("variant", variant)
    if slot is not None:
        button.clicked.connect(slot)
    return button


def fill_combo(combo: QComboBox, choices: list[tuple[str, str]]) -> None:
    for label, value in choices:
        combo.addItem(label, value)


def select_data(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


def confirm_auto_install(parent: QWidget, for_default: bool) -> bool:
    """Ask the user to acknowledge the risk of installing updates without confirmation."""
    if for_default:
        text = (
            "Setting the default update action to 'Install it automatically' means newly "
            "installed programs will silently download and replace executables whenever an "
            "update is found.\n\n"
            "A compromised GitHub repository could push a malicious release that "
            "would be installed without your confirmation.\n\n"
            "Are you sure you want this as the default?"
        )
    else:
        text = (
            "Auto-install mode will automatically download and replace program files "
            "without further confirmation.\n\n"
            "A compromised or hijacked GitHub repository could push a malicious "
            "release that would be installed silently.\n\n"
            "Are you sure you want to enable this?"
        )
    reply = QMessageBox.warning(
        parent,
        "Security warning",
        text,
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    return reply == QMessageBox.Yes


# ----------------------------------------------------------------------
# Download cache helpers
# ----------------------------------------------------------------------


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    size = float(num_bytes)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} TB"


def folder_size_bytes(folder: Path) -> int:
    """Total size of the regular files under *folder* (symlinks are not followed)."""
    total = 0
    if not folder.is_dir():
        return total
    try:
        for path in folder.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def cache_refusal(cache_text: str, install_text: str = "") -> str | None:
    """Return why the cache folder must not be cleared, or ``None`` if it may be."""
    text = cache_text.strip()
    if not text:
        return "No download cache folder is set, so nothing was cleared."
    folder = Path(text).resolve()
    if folder.parent == folder or folder == Path.home().resolve():
        return f"Refusing to clear {folder}: it is a drive root or your home folder."
    install = install_text.strip()
    if install:
        install_path = Path(install).resolve()
        if install_path == folder or folder in install_path.parents:
            return "Refusing to clear the download cache because it contains the install folder."
    return None


def clear_folder_contents(folder: Path) -> list[str]:
    """Delete everything inside *folder* but keep the folder itself.

    Returns one message per item that could not be removed.
    """
    errors: list[str] = []
    try:
        children = list(folder.iterdir())
    except OSError as exc:
        return [str(exc)]
    for child in children:
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as exc:
            errors.append(f"{child.name}: {exc.strerror or exc}")
    return errors


# ----------------------------------------------------------------------
# Rate-limit reply text (GitHub's /rate_limit answer)
# ----------------------------------------------------------------------


def describe_rate_limit(response, has_token: bool) -> tuple[str, str]:
    """Turn a ``/rate_limit`` response into (message, status kind)."""
    if response.status_code == 401:
        return "✗ GitHub rejected this token (HTTP 401).", "error"
    if response.status_code != 200:
        return f"✗ GitHub answered HTTP {response.status_code}.", "error"
    try:
        core = response.json()["resources"]["core"]
        remaining = int(core["remaining"])
        limit = int(core["limit"])
    except (ValueError, KeyError, TypeError):
        return "✗ GitHub sent an unexpected reply.", "error"
    left = f"{remaining:,} of {limit:,} requests left this hour"
    if has_token:
        return f"✓ Valid — {left}", "ok"
    return f"No token entered. Without one, {left}.", "muted"


def _checkbox_item(checked: bool) -> QTableWidgetItem:
    item = QTableWidgetItem()
    item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    return item


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self._base = settings
        # Edited on a copy, so cancelling leaves the original settings untouched.
        self._sources: list[SourceConfig] = copy.deepcopy(settings.sources)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(740)  # wide enough for the six columns of the Sources table

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_general_page(settings), "General")
        self.tabs.addTab(self._build_updates_page(settings), "Updates")
        self.tabs.addTab(self._build_sources_page(), "Sources")
        self.tabs.addTab(self._build_appearance_page(settings), "Appearance && tray")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        ok_button.setDefault(True)
        ok_button.setProperty("variant", "primary")
        buttons.button(QDialogButtonBox.Cancel).setProperty("variant", "secondary")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

        self._refresh_cache_button()

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _build_general_page(self, settings: AppSettings) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.install_root = QLineEdit(settings.install_root)
        form.addRow(
            "Install folder",
            hbox(self.install_root, make_button("Browse…", slot=lambda: self._browse_folder(self.install_root))),
        )
        form.addRow(subtle_label("Changing this only affects new installs; existing programs stay where they are."))

        self.download_cache = QLineEdit(settings.download_cache)
        self.download_cache.editingFinished.connect(self._refresh_cache_button)
        self.clear_cache_button = make_button("Clear cache", slot=self._clear_cache)
        self.cache_status = subtle_label()
        form.addRow(
            "Download cache",
            hbox(self.download_cache, make_button("Browse…", slot=self._browse_cache)),
        )
        form.addRow("", hbox(self.clear_cache_button, self.cache_status))

        self.search_result_limit = QSpinBox()
        self.search_result_limit.setRange(5, 50)
        self.search_result_limit.setValue(settings.search_result_limit)
        form.addRow("Results per search", self.search_result_limit)

        self.auto_open_folder = QCheckBox("Open the install folder after installing")
        self.auto_open_folder.setChecked(settings.auto_open_folder_after_install)
        form.addRow(self.auto_open_folder)
        return page

    def _build_updates_page(self, settings: AppSettings) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.run_update_check_on_startup = QCheckBox(
            "Check for updates automatically, following each program's schedule"
        )
        self.run_update_check_on_startup.setChecked(settings.run_update_check_on_startup)
        form.addRow(self.run_update_check_on_startup)

        self.default_update_mode = QComboBox()
        fill_combo(self.default_update_mode, UPDATE_MODE_CHOICES)
        select_data(self.default_update_mode, settings.default_update_mode)
        self._update_mode_prev = self.default_update_mode.currentData()
        self.default_update_mode.currentIndexChanged.connect(self._on_update_mode_changed)
        form.addRow("Default action when an update is found", self.default_update_mode)

        self.default_update_schedule_type = QComboBox()
        fill_combo(self.default_update_schedule_type, SCHEDULE_CHOICES)
        select_data(self.default_update_schedule_type, settings.default_update_schedule_type)
        form.addRow("Default schedule", self.default_update_schedule_type)

        self.default_update_interval_hours = QSpinBox()
        self.default_update_interval_hours.setRange(1, 720)
        self.default_update_interval_hours.setSuffix(" hours")
        self.default_update_interval_hours.setValue(settings.default_update_interval_hours)
        form.addRow("Every", self.default_update_interval_hours)
        self.default_update_schedule_type.currentIndexChanged.connect(self._sync_interval_spin)
        self._sync_interval_spin()

        form.addRow(
            subtle_label(
                "These defaults apply to newly installed programs. Each program can change its own settings."
            )
        )
        return page

    def _build_sources_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            subtle_label(
                "Sites the app can search, install from and check for updates. Add a self-hosted GitLab, "
                "Gitea or Forgejo server, or GitHub Enterprise."
            )
        )

        self.sources_table = QTableWidget(0, len(SOURCE_HEADERS))
        self.sources_table.setHorizontalHeaderLabels(SOURCE_HEADERS)
        self.sources_table.verticalHeader().hide()
        self.sources_table.setShowGrid(False)
        self.sources_table.setAlternatingRowColors(True)  # colours come from the theme palette
        self.sources_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sources_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sources_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        header = self.sources_table.horizontalHeader()
        for column in range(len(SOURCE_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # Name takes the spare width
        self.sources_table.setMinimumHeight(180)
        self.sources_table.itemChanged.connect(self._on_source_item_changed)
        self.sources_table.itemSelectionChanged.connect(self._update_source_buttons)
        self.sources_table.doubleClicked.connect(self._on_source_double_clicked)
        layout.addWidget(self.sources_table)

        self.add_source_button = make_button("Add…", variant="primary", slot=self._add_source)
        self.edit_source_button = make_button("Edit…", slot=self._edit_source)
        self.remove_source_button = make_button("Remove", variant="danger", slot=self._remove_source)
        self.test_source_button = make_button("Test", slot=self._test_selected_source)
        layout.addWidget(
            hbox(self.add_source_button, self.edit_source_button, self.remove_source_button, self.test_source_button)
        )

        self.sources_result = QLabel()
        self.sources_result.setWordWrap(True)
        layout.addWidget(self.sources_result)

        self._refresh_sources_table()
        return page

    def _build_appearance_page(self, settings: AppSettings) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.theme = QComboBox()
        fill_combo(self.theme, THEME_CHOICES)
        select_data(self.theme, settings.theme)
        form.addRow("Theme", self.theme)

        self.minimize_to_tray = QCheckBox("Minimize to the system tray")
        self.minimize_to_tray.setChecked(settings.minimize_to_tray)
        self.close_to_tray = QCheckBox("Keep running in the tray when the window is closed")
        self.close_to_tray.setChecked(settings.close_to_tray)
        self.show_system_notifications = QCheckBox("Show notifications")
        self.show_system_notifications.setChecked(settings.show_system_notifications)
        form.addRow(self.minimize_to_tray)
        form.addRow(self.close_to_tray)
        form.addRow(self.show_system_notifications)
        return page

    # ------------------------------------------------------------------
    # General page helpers
    # ------------------------------------------------------------------

    def _browse_folder(self, edit: QLineEdit) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text().strip())
        if chosen:
            edit.setText(chosen)

    def _browse_cache(self) -> None:
        self._browse_folder(self.download_cache)
        self._refresh_cache_button()

    def _refresh_cache_button(self) -> None:
        text = self.download_cache.text().strip()
        size = folder_size_bytes(Path(text)) if text else 0
        self.clear_cache_button.setText(f"Clear cache ({format_size(size)})")

    def _clear_cache(self) -> None:
        cache_text = self.download_cache.text().strip()
        refusal = cache_refusal(cache_text, self.install_root.text())
        if refusal:
            self.cache_status.setText(refusal)
            return
        folder = Path(cache_text)
        freed = folder_size_bytes(folder)
        errors = clear_folder_contents(folder)
        self._refresh_cache_button()
        if errors:
            self.cache_status.setText("Some files could not be removed: " + "; ".join(errors[:3]))
        else:
            self.cache_status.setText(f"Cleared {format_size(freed)}.")

    # ------------------------------------------------------------------
    # Updates page helpers
    # ------------------------------------------------------------------

    def _sync_interval_spin(self) -> None:
        self.default_update_interval_hours.setEnabled(
            self.default_update_schedule_type.currentData() == "interval"
        )

    def _on_update_mode_changed(self, index: int) -> None:
        value = self.default_update_mode.itemData(index)
        if value == "install_automatically" and self._update_mode_prev != value:
            if not confirm_auto_install(self, for_default=True):
                select_data(self.default_update_mode, self._update_mode_prev)
                return
        self._update_mode_prev = value

    # ------------------------------------------------------------------
    # Sources page helpers
    # ------------------------------------------------------------------

    def _refresh_sources_table(self, select: int | None = None) -> None:
        """Rebuild the table from ``self._sources`` (row N is always source N)."""
        table = self.sources_table
        table.blockSignals(True)
        try:
            table.setRowCount(0)
            for row, source in enumerate(self._sources):
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(source.name))
                table.setItem(row, 1, QTableWidgetItem(SOURCE_TYPE_NAMES.get(source.kind, source.kind)))
                table.setItem(row, 2, QTableWidgetItem(source.base_url))
                table.setItem(row, 3, QTableWidgetItem("✓ set" if source.token else "—"))
                table.setItem(row, 4, _checkbox_item(source.enabled))
                table.setItem(row, 5, _checkbox_item(source.searchable))
        finally:
            table.blockSignals(False)
        if select is not None and 0 <= select < table.rowCount():
            table.selectRow(select)
        self._update_source_buttons()

    def _selected_row(self) -> int:
        rows = self.sources_table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _update_source_buttons(self) -> None:
        row = self._selected_row()
        has_row = 0 <= row < len(self._sources)
        self.edit_source_button.setEnabled(has_row)
        self.test_source_button.setEnabled(has_row)
        is_github = has_row and self._sources[row].id == "github"
        self.remove_source_button.setEnabled(has_row and not is_github)
        if is_github:
            tip = _GITHUB_REMOVE_TIP
        elif has_row:
            tip = "Remove the selected source from the list."
        else:
            tip = "Select a source to remove it."
        self.remove_source_button.setToolTip(tip)

    def _on_source_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() not in (4, 5) or not (0 <= item.row() < len(self._sources)):
            return
        checked = item.checkState() == Qt.CheckState.Checked
        source = self._sources[item.row()]
        if item.column() == 4:
            source.enabled = checked
        else:
            source.searchable = checked

    def _on_source_double_clicked(self, index) -> None:
        if index.column() >= 4:  # the checkbox columns toggle instead
            return
        self.sources_table.selectRow(index.row())
        self._edit_source()

    def _add_source(self) -> None:
        from .source_dialog import SourceDialog  # imported here: source_dialog imports this module

        dialog = SourceDialog(others=list(self._sources), parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._sources.append(dialog.get_config())
        self._refresh_sources_table(select=len(self._sources) - 1)

    def _edit_source(self) -> None:
        from .source_dialog import SourceDialog  # imported here: source_dialog imports this module

        row = self._selected_row()
        if not (0 <= row < len(self._sources)):
            return
        others = [source for i, source in enumerate(self._sources) if i != row]
        dialog = SourceDialog(source=self._sources[row], others=others, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        self._sources[row] = dialog.get_config()
        self._refresh_sources_table(select=row)

    def _remove_source(self) -> None:
        row = self._selected_row()
        if not (0 <= row < len(self._sources)) or self._sources[row].id == "github":
            return
        del self._sources[row]
        self._refresh_sources_table(select=min(row, len(self._sources) - 1))

    def _test_selected_source(self) -> None:
        from .source_dialog import check_source_connection  # imported here: source_dialog imports this module

        row = self._selected_row()
        if not (0 <= row < len(self._sources)):
            return
        message, kind = check_source_connection(self._sources[row])
        self.sources_result.setText(message)
        self.sources_result.setStyleSheet(f"color: {status_color(kind).name()};")

    # ------------------------------------------------------------------
    # Accept / result
    # ------------------------------------------------------------------

    def _validate_and_accept(self) -> None:
        """Check the folders before closing: they must be filled in and writable."""
        install_root = self.install_root.text().strip()
        cache = self.download_cache.text().strip()

        errors: list[str] = []
        if not install_root:
            errors.append("Install folder is required.")
        if not cache:
            errors.append("Download cache folder is required.")
        if not errors:
            for folder in (install_root, cache):
                try:
                    fsops.ensure_writable_dir(Path(folder))
                except InstallError as exc:
                    errors.append(str(exc))
        if errors:
            QMessageBox.warning(self, "Settings", "\n\n".join(errors))
            return
        self.accept()

    def get_settings(self) -> AppSettings:
        sources = copy.deepcopy(self._sources)
        github = next((source for source in sources if source.id == "github"), None)
        # The GitHub source owns the token; github_token mirrors it.
        github_token = github.token if github is not None else self._base.github_token
        return dataclasses.replace(
            self._base,
            sources=sources,
            install_root=self.install_root.text().strip(),
            download_cache=self.download_cache.text().strip(),
            github_token=github_token,
            auto_open_folder_after_install=self.auto_open_folder.isChecked(),
            search_result_limit=self.search_result_limit.value(),
            default_update_mode=self.default_update_mode.currentData(),
            default_update_schedule_type=self.default_update_schedule_type.currentData(),
            default_update_interval_hours=self.default_update_interval_hours.value(),
            run_update_check_on_startup=self.run_update_check_on_startup.isChecked(),
            show_system_notifications=self.show_system_notifications.isChecked(),
            minimize_to_tray=self.minimize_to_tray.isChecked(),
            close_to_tray=self.close_to_tray.isChecked(),
            theme=self.theme.currentData(),
        )


def find_gh_cli() -> str | None:
    """Path of the GitHub CLI (``gh``), or None if it isn't installed."""
    return shutil.which("gh")


def read_gh_cli_token() -> tuple[str, str]:
    """Return ``(token, "")`` from ``gh auth token``, or ``("", error message)``."""
    gh = find_gh_cli()
    if not gh:
        return "", "The GitHub CLI (gh) isn't installed."
    try:
        proc = subprocess.run(
            [gh, "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"Couldn't run the GitHub CLI: {exc}"
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        return "", "The GitHub CLI isn't signed in. Run \"gh auth login\" first."
    return token, ""
