from __future__ import annotations

from pathlib import Path

import pytest
import requests
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMessageBox

from portable_manager import fsops
from portable_manager.errors import InstallError
from portable_manager.models import AppSettings, ManagedProgram, UpdatePolicy
from portable_manager.ui.edit_program_dialog import EditProgramDialog
from portable_manager.ui.settings_dialog import SettingsDialog, cache_refusal


@pytest.fixture(scope="module", autouse=True)
def qapp():
    """One QApplication for the whole module (widgets need it)."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def warnings(monkeypatch):
    """Record QMessageBox.warning calls instead of showing them."""
    calls: list[str] = []

    def fake_warning(parent, title, text, *args, **kwargs):
        calls.append(text)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "warning", fake_warning)
    return calls


def make_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        install_root=str(tmp_path / "installs"),
        download_cache=str(tmp_path / "cache"),
        github_token="ghp_example",
        auto_open_folder_after_install=False,
        search_result_limit=33,
        default_update_mode="download_only",
        default_update_schedule_type="interval",
        default_update_interval_hours=6,
        run_update_check_on_startup=True,
        show_system_notifications=False,
        minimize_to_tray=False,
        close_to_tray=False,
        theme="dark",
    )


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def rate_limit_payload(remaining: int, limit: int) -> dict:
    return {"resources": {"core": {"remaining": remaining, "limit": limit}}}


# ----------------------------------------------------------------------
# SettingsDialog
# ----------------------------------------------------------------------


def test_settings_round_trip(tmp_path):
    settings = make_settings(tmp_path)
    dialog = SettingsDialog(settings)
    assert dialog.get_settings() == settings


def test_settings_combos_map_to_values(tmp_path, warnings):
    dialog = SettingsDialog(make_settings(tmp_path))

    dialog.theme.setCurrentIndex(1)  # Light
    dialog.default_update_mode.setCurrentIndex(1)  # Notify me
    dialog.default_update_schedule_type.setCurrentIndex(2)  # Daily

    settings = dialog.get_settings()
    assert settings.theme == "light"
    assert settings.default_update_mode == "notify_only"
    assert settings.default_update_schedule_type == "daily"
    assert warnings == []


def test_interval_spin_only_enabled_for_interval(tmp_path):
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.default_update_schedule_type.setCurrentIndex(1)  # Every N hours
    assert dialog.default_update_interval_hours.isEnabled()
    dialog.default_update_schedule_type.setCurrentIndex(2)  # Daily
    assert not dialog.default_update_interval_hours.isEnabled()


def test_auto_install_warning_reverts_when_declined(tmp_path, monkeypatch):
    asked: list[str] = []

    def decline(parent, title, text, *args, **kwargs):
        asked.append(title)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "warning", decline)
    dialog = SettingsDialog(make_settings(tmp_path))  # starts at download_only
    dialog.default_update_mode.setCurrentIndex(3)  # Install it automatically

    assert len(asked) == 1
    assert dialog.get_settings().default_update_mode == "download_only"


def test_auto_install_kept_when_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.default_update_mode.setCurrentIndex(3)
    assert dialog.get_settings().default_update_mode == "install_automatically"


def test_validation_rejects_unwritable_install_root(tmp_path, monkeypatch, warnings):
    def deny(path):
        raise InstallError("Windows doesn't allow writing here")

    monkeypatch.setattr(fsops, "ensure_writable_dir", deny)
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1
    assert "Windows doesn't allow writing here" in warnings[0]


def test_validation_accepts_good_folders(tmp_path, warnings):
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog._validate_and_accept()

    assert dialog.result() == QDialog.Accepted
    assert warnings == []
    assert (tmp_path / "installs").is_dir()


def test_validation_requires_folders(tmp_path, warnings):
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.install_root.setText("")
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1


def test_clear_cache_deletes_contents(tmp_path):
    cache = tmp_path / "cache"
    (cache / "nested").mkdir(parents=True)
    (cache / "one.zip").write_bytes(b"x" * 2048)
    (cache / "nested" / "two.zip").write_bytes(b"y" * 1024)

    dialog = SettingsDialog(make_settings(tmp_path))
    assert dialog.clear_cache_button.text() == "Clear cache (3.0 KB)"

    dialog.clear_cache_button.click()

    assert cache.is_dir()
    assert list(cache.iterdir()) == []
    assert dialog.clear_cache_button.text() == "Clear cache (0 B)"


def test_clear_cache_refuses_empty_path(tmp_path):
    keep = tmp_path / "keep.txt"
    keep.write_text("do not delete")

    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.download_cache.setText("")
    dialog.clear_cache_button.click()

    assert keep.exists()
    assert "nothing was cleared" in dialog.cache_status.text()


def test_cache_refusal_rules(tmp_path):
    assert cache_refusal("") is not None
    assert cache_refusal(str(Path.home())) is not None
    assert cache_refusal(str(Path(Path.home().anchor))) is not None
    # Refuse when the cache folder would contain the install folder.
    assert cache_refusal(str(tmp_path / "cache"), str(tmp_path / "cache" / "installs")) is not None
    assert cache_refusal(str(tmp_path / "cache"), str(tmp_path / "installs")) is None


def test_token_test_reports_valid_token(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse(200, rate_limit_payload(4998, 5000))

    monkeypatch.setattr(requests, "get", fake_get)
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.github_token.setText("ghp_example")
    dialog.test_token_button.click()

    assert dialog.token_result.text() == "✓ Valid — 4,998 of 5,000 requests left this hour"
    assert seen["url"].endswith("/rate_limit")
    assert seen["headers"]["Authorization"] == "Bearer ghp_example"
    assert "ghp_example" not in dialog.token_result.text()


def test_token_test_reports_rejected_token(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(401))
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.test_token_button.click()

    assert dialog.token_result.text() == "✗ GitHub rejected this token (HTTP 401)."


def test_token_test_reports_network_failure(tmp_path, monkeypatch):
    def offline(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", offline)
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.test_token_button.click()

    assert dialog.token_result.text().startswith("Couldn't reach GitHub")


def test_token_show_toggle(tmp_path):
    dialog = SettingsDialog(make_settings(tmp_path))
    assert dialog.github_token.echoMode() == QLineEdit.EchoMode.Password
    dialog.show_token.setChecked(True)
    assert dialog.github_token.echoMode() == QLineEdit.EchoMode.Normal
    dialog.show_token.setChecked(False)
    assert dialog.github_token.echoMode() == QLineEdit.EchoMode.Password


# ----------------------------------------------------------------------
# EditProgramDialog
# ----------------------------------------------------------------------


@pytest.fixture
def program(tmp_path) -> ManagedProgram:
    install = tmp_path / "apps" / "Tool"
    install.mkdir(parents=True)
    (install / "tool.exe").write_bytes(b"MZ")
    return ManagedProgram(
        program_id="p1",
        name="Tool",
        source_type="github",
        source_value="owner/tool",
        install_dir=str(install),
        launch_path=str(install / "tool.exe"),
        launch_args='--config "C:\\path with spaces\\settings.ini"',
        working_directory_override=str(install),
        notes="Some notes",
        pinned=True,
        run_as_admin=True,
        update_policy=UpdatePolicy(
            check_enabled=True,
            update_mode="notify_only",
            schedule_type="interval",
            interval_hours=12,
            channel="prerelease",
            asset_selection_override="win64",
            notify_on_available_update=False,
        ),
    )


def test_edit_round_trip(program):
    dialog = EditProgramDialog(program)

    assert dialog.get_name() == "Tool"
    assert dialog.get_notes() == "Some notes"
    assert dialog.get_launch_path() == program.launch_path
    assert dialog.get_launch_args() == program.launch_args
    assert dialog.get_working_directory() == program.working_directory_override
    assert dialog.get_run_as_admin() is True
    assert dialog.get_pinned() is True
    assert dialog.get_update_policy() == program.update_policy


def test_pinned_and_notify_flags_round_trip(program):
    dialog = EditProgramDialog(program)
    dialog.pinned_check.setChecked(False)
    dialog.notify_on_update.setChecked(True)

    assert dialog.get_pinned() is False
    assert dialog.get_update_policy().notify_on_available_update is True


def test_update_controls_disabled_when_checks_off(program):
    dialog = EditProgramDialog(program)
    assert dialog.update_mode.isEnabled()
    dialog.check_enabled.setChecked(False)
    assert not dialog.update_mode.isEnabled()
    assert not dialog.interval_hours.isEnabled()
    assert dialog.get_update_policy().check_enabled is False


def test_invalid_launch_path_keeps_dialog_open(program, warnings, tmp_path):
    dialog = EditProgramDialog(program)
    dialog.launch_path_edit.setText(str(tmp_path / "missing.exe"))
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1


def test_launch_path_with_wrong_extension_rejected(program, warnings, tmp_path):
    readme = tmp_path / "readme.txt"
    readme.write_text("hi")
    dialog = EditProgramDialog(program)
    dialog.launch_path_edit.setText(str(readme))
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1


def test_blank_launch_path_asks_and_saves_as_none(program, monkeypatch):
    questions: list[str] = []

    def yes(parent, title, text, *args, **kwargs):
        questions.append(text)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", yes)
    dialog = EditProgramDialog(program)
    dialog.launch_path_edit.setText("")
    dialog._validate_and_accept()

    assert len(questions) == 1
    assert dialog.result() == QDialog.Accepted
    assert dialog.get_launch_path() is None


def test_blank_launch_path_declined_keeps_dialog_open(program, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    dialog = EditProgramDialog(program)
    dialog.launch_path_edit.setText("")
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted


def test_name_is_required(program, warnings):
    dialog = EditProgramDialog(program)
    dialog.name_edit.setText("   ")
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1


def test_auto_install_warning_reverts_when_declined(program, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.No)
    dialog = EditProgramDialog(program)  # starts at notify_only
    dialog.update_mode.setCurrentIndex(3)  # Install it automatically

    assert dialog.get_update_policy().update_mode == "notify_only"


def test_auto_install_kept_when_accepted(program, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    dialog = EditProgramDialog(program)
    dialog.update_mode.setCurrentIndex(3)

    assert dialog.get_update_policy().update_mode == "install_automatically"
