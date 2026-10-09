from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from ..models import ManagedProgram, UpdatePolicy
from .settings_dialog import (
    SCHEDULE_CHOICES,
    UPDATE_MODE_CHOICES,
    confirm_auto_install,
    fill_combo,
    hbox,
    make_button,
    select_data,
    subtle_label,
)

LAUNCH_SUFFIXES = (".exe", ".bat", ".cmd", ".ps1")
LAUNCH_FILTER = "Programs (*.exe *.bat *.cmd *.ps1);;All files (*)"


class EditProgramDialog(QDialog):
    """Dialog for editing a managed program's details, launch settings and update policy."""

    def __init__(self, program: ManagedProgram, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit — {program.name}")
        self.setMinimumWidth(560)
        self._policy = program.update_policy
        self._install_dir = program.install_dir or ""

        layout = QVBoxLayout(self)

        title = QLabel(program.name)
        title_font = title.font()
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)
        layout.addWidget(subtle_label(self._install_dir))

        layout.addWidget(self._build_program_group(program))
        layout.addWidget(self._build_updates_group())

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setDefault(True)
        buttons.button(QDialogButtonBox.Ok).setProperty("variant", "primary")
        buttons.button(QDialogButtonBox.Cancel).setProperty("variant", "secondary")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def _build_program_group(self, program: ManagedProgram) -> QGroupBox:
        box = QGroupBox("Program")
        form = QFormLayout(box)

        self.name_edit = QLineEdit(program.name)
        form.addRow("Name", self.name_edit)

        self.notes_edit = QPlainTextEdit(program.notes or "")
        self.notes_edit.setPlaceholderText("Optional notes about this program")
        self.notes_edit.setFixedHeight(84)
        form.addRow("Notes", self.notes_edit)

        self.launch_path_edit = QLineEdit(program.launch_path or "")
        self.launch_path_edit.setPlaceholderText("Executable or script to run (.exe, .bat, .cmd, .ps1)")
        form.addRow(
            "Launch file",
            hbox(self.launch_path_edit, make_button("Browse…", slot=self._browse_launch_file)),
        )

        self.launch_args_edit = QLineEdit(program.launch_args or "")
        self.launch_args_edit.setPlaceholderText(
            r'e.g.  --minimized  or  --config "C:\path with spaces\settings.ini"'
        )
        form.addRow("Launch arguments", self.launch_args_edit)

        self.working_dir_edit = QLineEdit(program.working_directory_override or "")
        self.working_dir_edit.setPlaceholderText("Default: the program's folder")
        form.addRow(
            "Working folder",
            hbox(self.working_dir_edit, make_button("Browse…", slot=self._browse_working_dir)),
        )

        self.run_as_admin = QCheckBox("Always run as administrator (asks for permission with a UAC prompt)")
        self.run_as_admin.setChecked(program.run_as_admin)
        form.addRow(self.run_as_admin)

        self.pinned_check = QCheckBox("Pin to the tray menu for quick launch")
        self.pinned_check.setChecked(program.pinned)
        form.addRow(self.pinned_check)
        return box

    def _build_updates_group(self) -> QGroupBox:
        policy = self._policy
        box = QGroupBox("Updates")
        form = QFormLayout(box)

        self.check_enabled = QCheckBox("Check for updates")
        self.check_enabled.setChecked(policy.check_enabled)
        form.addRow(self.check_enabled)

        self.update_mode = QComboBox()
        fill_combo(self.update_mode, UPDATE_MODE_CHOICES)
        select_data(self.update_mode, policy.update_mode)
        self._update_mode_prev = self.update_mode.currentData()
        self.update_mode.currentIndexChanged.connect(self._on_update_mode_changed)
        form.addRow("When an update is found", self.update_mode)

        self.schedule_type = QComboBox()
        fill_combo(self.schedule_type, SCHEDULE_CHOICES)
        select_data(self.schedule_type, policy.schedule_type)
        self.interval_hours = QSpinBox()
        self.interval_hours.setRange(1, 720)
        self.interval_hours.setSuffix(" hours")
        self.interval_hours.setValue(policy.interval_hours)
        form.addRow("Check", hbox(self.schedule_type, self.interval_hours))

        self.channel = QComboBox()
        self.channel.addItem("Stable releases", "latest_release")
        self.channel.addItem("Include pre-releases", "prerelease")
        select_data(self.channel, policy.channel)
        form.addRow("Release channel", self.channel)

        self.asset_override = QLineEdit(policy.asset_selection_override or "")
        self.asset_override.setPlaceholderText("Optional — part of the file name to pick, e.g. win64-portable")
        form.addRow("Prefer asset named", self.asset_override)

        self.notify_on_update = QCheckBox("Show a notification when an update is found")
        self.notify_on_update.setChecked(policy.notify_on_available_update)
        form.addRow(self.notify_on_update)

        self.check_enabled.toggled.connect(self._sync_update_controls)
        self.schedule_type.currentIndexChanged.connect(self._sync_interval_spin)
        self._sync_update_controls()
        return box

    # ------------------------------------------------------------------
    # Control state
    # ------------------------------------------------------------------

    def _sync_update_controls(self) -> None:
        enabled = self.check_enabled.isChecked()
        for widget in (self.update_mode, self.schedule_type, self.channel, self.asset_override, self.notify_on_update):
            widget.setEnabled(enabled)
        self._sync_interval_spin()

    def _sync_interval_spin(self) -> None:
        enabled = self.check_enabled.isChecked() and self.schedule_type.currentData() == "interval"
        self.interval_hours.setEnabled(enabled)

    def _on_update_mode_changed(self, index: int) -> None:
        value = self.update_mode.itemData(index)
        if value == "install_automatically" and self._update_mode_prev != value:
            if not confirm_auto_install(self, for_default=False):
                select_data(self.update_mode, self._update_mode_prev)
                return
        self._update_mode_prev = value

    # ------------------------------------------------------------------
    # Browse buttons
    # ------------------------------------------------------------------

    def _browse_launch_file(self) -> None:
        start = self._install_dir if os.path.isdir(self._install_dir) else ""
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose launch file", start, LAUNCH_FILTER)
        if chosen:
            self.launch_path_edit.setText(chosen)

    def _browse_working_dir(self) -> None:
        start = self._install_dir if os.path.isdir(self._install_dir) else ""
        chosen = QFileDialog.getExistingDirectory(self, "Choose working folder", start)
        if chosen:
            self.working_dir_edit.setText(chosen)

    # ------------------------------------------------------------------
    # Accept
    # ------------------------------------------------------------------

    def _validate_and_accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Edit program", "The program needs a name.")
            return

        launch = self.launch_path_edit.text().strip()
        if launch:
            path = Path(launch)
            if not path.is_file() or path.suffix.lower() not in LAUNCH_SUFFIXES:
                QMessageBox.warning(
                    self,
                    "Edit program",
                    "The launch file must be an existing .exe, .bat, .cmd or .ps1 file:\n\n" + launch,
                )
                return
        else:
            reply = QMessageBox.question(
                self,
                "No launch file",
                "No launch file is set, so Run will be unavailable. Save anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        working = self.working_dir_edit.text().strip()
        if working and not Path(working).is_dir():
            QMessageBox.warning(
                self,
                "Edit program",
                "The working folder must be an existing folder:\n\n" + working,
            )
            return
        self.accept()

    # ------------------------------------------------------------------
    # Result extraction
    # ------------------------------------------------------------------

    def get_name(self) -> str:
        return self.name_edit.text().strip() or "Unnamed"

    def get_notes(self) -> str:
        return self.notes_edit.toPlainText().strip()

    def get_launch_path(self) -> str | None:
        value = self.launch_path_edit.text().strip()
        return value if value else None

    def get_launch_args(self) -> str:
        return self.launch_args_edit.text().strip()

    def get_working_directory(self) -> str | None:
        value = self.working_dir_edit.text().strip()
        return value if value else None

    def get_run_as_admin(self) -> bool:
        return self.run_as_admin.isChecked()

    def get_pinned(self) -> bool:
        return self.pinned_check.isChecked()

    def get_update_policy(self) -> UpdatePolicy:
        return dataclasses.replace(
            self._policy,
            check_enabled=self.check_enabled.isChecked(),
            update_mode=self.update_mode.currentData(),
            schedule_type=self.schedule_type.currentData(),
            interval_hours=self.interval_hours.value(),
            channel=self.channel.currentData(),
            asset_selection_override=self.asset_override.text().strip(),
            notify_on_available_update=self.notify_on_update.isChecked(),
        )
