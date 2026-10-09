from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from ..models import ManagedProgram, UpdatePolicy


class EditProgramDialog(QDialog):
    """Dialog for editing user-controllable metadata and update policy for a managed program."""

    def __init__(self, program: ManagedProgram, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit — {program.name}")
        self.setMinimumWidth(540)

        # ------------------------------------------------------------------
        # Program section
        # ------------------------------------------------------------------
        program_box = QGroupBox("Program")
        program_form = QFormLayout(program_box)

        self.name_edit = QLineEdit(program.name)
        program_form.addRow("Name", self.name_edit)

        self.notes_edit = QTextEdit(program.notes or "")
        self.notes_edit.setFixedHeight(70)
        self.notes_edit.setPlaceholderText("Optional notes about this program")
        program_form.addRow("Notes", self.notes_edit)

        self.launch_path_edit = QLineEdit(program.launch_path or "")
        self.launch_path_edit.setPlaceholderText("Path to executable (leave blank to keep current)")
        program_form.addRow("Launch path", self.launch_path_edit)

        self.launch_args_edit = QLineEdit(program.launch_args or "")
        self.launch_args_edit.setPlaceholderText('e.g.  --minimized  or  --config "C:\\path with spaces\\cfg.ini"')
        program_form.addRow("Launch arguments", self.launch_args_edit)

        self.working_dir_edit = QLineEdit(program.working_directory_override or "")
        self.working_dir_edit.setPlaceholderText("Override working directory (leave blank for install folder)")
        program_form.addRow("Working directory", self.working_dir_edit)

        self.run_as_admin = QCheckBox("Run as administrator (requests UAC elevation on launch)")
        self.run_as_admin.setChecked(program.run_as_admin)
        program_form.addRow("", self.run_as_admin)

        # ------------------------------------------------------------------
        # Update policy section
        # ------------------------------------------------------------------
        policy = program.update_policy
        policy_box = QGroupBox("Update policy")
        policy_form = QFormLayout(policy_box)

        self.check_enabled = QCheckBox("Enable automatic update checks")
        self.check_enabled.setChecked(policy.check_enabled)
        policy_form.addRow("", self.check_enabled)

        self.update_mode = QComboBox()
        for value, label in [
            ("manual", "Manual — check only when I click"),
            ("notify_only", "Notify only — alert me, no download"),
            ("download_only", "Download only — fetch but do not install"),
            ("install_automatically", "Auto-install — apply updates silently"),
        ]:
            self.update_mode.addItem(label, value)
        self._set_combo_by_data(self.update_mode, policy.update_mode)
        self.update_mode.currentIndexChanged.connect(self._warn_auto_install)
        policy_form.addRow("Mode", self.update_mode)

        self.schedule_type = QComboBox()
        for value, label in [
            ("app_start", "On app start"),
            ("interval", "Every N hours"),
            ("daily", "Daily"),
            ("weekly", "Weekly"),
        ]:
            self.schedule_type.addItem(label, value)
        self._set_combo_by_data(self.schedule_type, policy.schedule_type)
        policy_form.addRow("Schedule", self.schedule_type)

        self.interval_hours = QSpinBox()
        self.interval_hours.setRange(1, 720)
        self.interval_hours.setValue(policy.interval_hours)
        self.interval_hours.setSuffix(" hours")
        policy_form.addRow("Interval", self.interval_hours)

        self.channel = QComboBox()
        self.channel.addItem("Stable releases only", "latest_release")
        self.channel.addItem("Include pre-releases", "prerelease")
        self._set_combo_by_data(self.channel, policy.channel)
        policy_form.addRow("Channel", self.channel)

        self.asset_override = QLineEdit(policy.asset_selection_override or "")
        self.asset_override.setPlaceholderText("Substring to match asset filename (leave blank for auto-select)")
        policy_form.addRow("Asset override", self.asset_override)

        # Enable/disable schedule controls based on check_enabled state
        self._sync_policy_controls()
        self.check_enabled.toggled.connect(self._sync_policy_controls)
        self.schedule_type.currentIndexChanged.connect(self._sync_interval_spin)

        # ------------------------------------------------------------------
        # Dialog buttons
        # ------------------------------------------------------------------
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(program_box)
        layout.addWidget(policy_box)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sync_policy_controls(self) -> None:
        enabled = self.check_enabled.isChecked()
        self.update_mode.setEnabled(enabled)
        self.schedule_type.setEnabled(enabled)
        self.channel.setEnabled(enabled)
        self.asset_override.setEnabled(enabled)
        self._sync_interval_spin()

    def _warn_auto_install(self) -> None:
        """Show a confirmation dialog when the user selects the auto-install mode.

        Auto-install silently downloads and overwrites executables, so the user
        should explicitly acknowledge the security implications.
        """
        if self.update_mode.currentData() != "install_automatically":
            return
        reply = QMessageBox.warning(
            self,
            "Security warning",
            "Auto-install mode will automatically download and replace program files "
            "without further confirmation.\n\n"
            "A compromised or hijacked GitHub repository could push a malicious "
            "release that would be installed silently.\n\n"
            "Are you sure you want to enable this?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            # Revert to manual.
            self._set_combo_by_data(self.update_mode, "manual")

    def _sync_interval_spin(self) -> None:
        enabled = self.check_enabled.isChecked() and self.schedule_type.currentData() == "interval"
        self.interval_hours.setEnabled(enabled)

    @staticmethod
    def _set_combo_by_data(combo: QComboBox, data_value: str) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == data_value:
                combo.setCurrentIndex(i)
                return

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

    def get_update_policy(self) -> UpdatePolicy:
        return UpdatePolicy(
            check_enabled=self.check_enabled.isChecked(),
            update_mode=self.update_mode.currentData(),
            schedule_type=self.schedule_type.currentData(),
            interval_hours=self.interval_hours.value(),
            channel=self.channel.currentData(),
            asset_selection_override=self.asset_override.text().strip(),
        )
