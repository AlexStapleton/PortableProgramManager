from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from ..models import AppSettings


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)

        self.install_root = QLineEdit(settings.install_root)
        self.download_cache = QLineEdit(settings.download_cache)
        self.github_token = QLineEdit(settings.github_token)
        self.github_token.setEchoMode(QLineEdit.Password)

        self.search_result_limit = QSpinBox()
        self.search_result_limit.setRange(5, 50)
        self.search_result_limit.setValue(settings.search_result_limit)

        self.default_update_mode = QComboBox()
        self.default_update_mode.addItems(["manual", "notify_only", "download_only", "install_automatically"])
        self.default_update_mode.setCurrentText(settings.default_update_mode)
        self.default_update_mode.currentTextChanged.connect(self._warn_auto_install_default)

        self.default_update_schedule_type = QComboBox()
        self.default_update_schedule_type.addItems(["app_start", "interval", "daily", "weekly"])
        self.default_update_schedule_type.setCurrentText(settings.default_update_schedule_type)

        self.default_update_interval_hours = QSpinBox()
        self.default_update_interval_hours.setRange(1, 720)
        self.default_update_interval_hours.setValue(settings.default_update_interval_hours)

        self.auto_open_folder = QCheckBox("Open install folder after install")
        self.auto_open_folder.setChecked(settings.auto_open_folder_after_install)

        self.run_update_check_on_startup = QCheckBox("Run update checks for eligible apps on startup")
        self.run_update_check_on_startup.setChecked(settings.run_update_check_on_startup)

        self.show_system_notifications = QCheckBox("Enable system notifications (future use)")
        self.show_system_notifications.setChecked(settings.show_system_notifications)

        self.minimize_to_tray = QCheckBox("Minimize to system tray instead of taskbar")
        self.minimize_to_tray.setChecked(settings.minimize_to_tray)

        self.close_to_tray = QCheckBox("Close to system tray (keep running in background)")
        self.close_to_tray.setChecked(settings.close_to_tray)

        form = QFormLayout()
        form.addRow("Install root", self.install_root)
        form.addRow("Download cache", self.download_cache)
        form.addRow("GitHub token (optional)", self.github_token)
        form.addRow("Search result limit", self.search_result_limit)
        form.addRow("Default update mode", self.default_update_mode)
        form.addRow("Default update schedule", self.default_update_schedule_type)
        form.addRow("Default interval hours", self.default_update_interval_hours)
        form.addRow("", self.auto_open_folder)
        form.addRow("", self.run_update_check_on_startup)
        form.addRow("", self.show_system_notifications)
        form.addRow("", self.minimize_to_tray)
        form.addRow("", self.close_to_tray)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _warn_auto_install_default(self, text: str) -> None:
        """Warn the user when selecting install_automatically as the global default."""
        if text != "install_automatically":
            return
        reply = QMessageBox.warning(
            self,
            "Security warning",
            "Setting the default update mode to 'install_automatically' means newly "
            "added programs will silently download and replace executables whenever an "
            "update is found.\n\n"
            "A compromised GitHub repository could push a malicious release that "
            "would be installed without your confirmation.\n\n"
            "Are you sure you want this as the default?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            self.default_update_mode.setCurrentText("manual")

    def _validate_and_accept(self) -> None:
        """Validate required fields before closing the dialog."""
        errors: list[str] = []
        if not self.install_root.text().strip():
            errors.append("Install root is required.")
        if not self.download_cache.text().strip():
            errors.append("Download cache is required.")
        if errors:
            QMessageBox.warning(self, "Validation Error", "\n".join(errors))
            return
        self.accept()

    def get_settings(self) -> AppSettings:
        return AppSettings(
            install_root=self.install_root.text().strip(),
            download_cache=self.download_cache.text().strip(),
            github_token=self.github_token.text().strip(),
            auto_open_folder_after_install=self.auto_open_folder.isChecked(),
            search_result_limit=self.search_result_limit.value(),
            default_update_mode=self.default_update_mode.currentText(),
            default_update_schedule_type=self.default_update_schedule_type.currentText(),
            default_update_interval_hours=self.default_update_interval_hours.value(),
            run_update_check_on_startup=self.run_update_check_on_startup.isChecked(),
            show_system_notifications=self.show_system_notifications.isChecked(),
            minimize_to_tray=self.minimize_to_tray.isChecked(),
            close_to_tray=self.close_to_tray.isChecked(),
        )
