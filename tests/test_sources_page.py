from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit, QMessageBox

from portable_manager.models import AppSettings, default_sources
from portable_manager.ui import settings_dialog, source_dialog
from portable_manager.ui.settings_dialog import SettingsDialog, select_data
from portable_manager.ui.source_dialog import SourceDialog


@pytest.fixture(autouse=True)
def _qapp(qapp):
    """Widgets need the session QApplication from conftest."""
    return qapp


@pytest.fixture
def warnings(monkeypatch):
    """Record QMessageBox.warning calls instead of showing them."""
    calls: list[str] = []

    def fake_warning(parent, title, text, *args, **kwargs):
        calls.append(text)
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "warning", fake_warning)
    return calls


def make_settings(tmp_path: Path, github_token: str = "") -> AppSettings:
    return AppSettings(
        install_root=str(tmp_path / "installs"),
        download_cache=str(tmp_path / "cache"),
        github_token=github_token,
    )


class FakeProvider:
    """Stands in for a real provider; records what the connection test asked of it."""

    def __init__(self, remaining: int | None = 4999, error: Exception | None = None, name: str = "Fake") -> None:
        self.name = name
        self.rate_limit_remaining = remaining
        self.error = error
        self.queries: list[tuple[str, int]] = []
        self.closed = False

    def search_repositories(self, query, limit=20, **kwargs):
        self.queries.append((query, limit))
        if self.error is not None:
            raise self.error
        return []

    def close(self) -> None:
        self.closed = True


def patch_provider(monkeypatch, provider):
    """Make build_provider return *provider* (or None). Returns the configs it was given."""
    seen: list = []

    def fake_build(config, cache=None):
        seen.append(config)
        return provider

    monkeypatch.setattr(source_dialog, "build_provider", fake_build)
    return seen


def accept_with(monkeypatch, *, name=None, kind=None, address=None, token=None) -> None:
    """Replace SourceDialog.exec: type the given values into the dialog, then accept it."""

    def fake_exec(self):
        if name is not None:
            self.name_edit.setText(name)
        if kind is not None:
            select_data(self.type_combo, kind)
        if address is not None:
            self.address_edit.setText(address)
        if token is not None:
            self.token_edit.setText(token)
        return QDialog.Accepted

    monkeypatch.setattr(SourceDialog, "exec", fake_exec)


def select(dialog: SettingsDialog, row: int) -> None:
    dialog.sources_table.selectRow(row)


def cell(dialog: SettingsDialog, row: int, column: int) -> str:
    return dialog.sources_table.item(row, column).text()


def checked(dialog: SettingsDialog, row: int, column: int) -> bool:
    return dialog.sources_table.item(row, column).checkState() == Qt.CheckState.Checked


# ----------------------------------------------------------------------
# Table
# ----------------------------------------------------------------------


def test_table_lists_the_three_defaults(tmp_path):
    dialog = SettingsDialog(make_settings(tmp_path))

    assert dialog.sources_table.rowCount() == 3
    assert [cell(dialog, r, 0) for r in range(3)] == ["GitHub", "GitLab", "Codeberg"]
    assert [cell(dialog, r, 1) for r in range(3)] == [
        "GitHub",
        "GitLab",
        "Gitea · Forgejo · Codeberg",
    ]
    assert [cell(dialog, r, 2) for r in range(3)] == [
        "https://github.com",
        "https://gitlab.com",
        "https://codeberg.org",
    ]
    assert [cell(dialog, r, 3) for r in range(3)] == ["—", "—", "—"]
    assert all(checked(dialog, r, 4) and checked(dialog, r, 5) for r in range(3))


def test_token_column_shows_set_when_a_token_is_stored(tmp_path):
    dialog = SettingsDialog(make_settings(tmp_path, github_token="ghp_x"))
    assert cell(dialog, 0, 3) == "✓ set"
    assert cell(dialog, 1, 3) == "—"


def test_sources_tab_is_present_and_buttons_need_a_selection(tmp_path):
    dialog = SettingsDialog(make_settings(tmp_path))
    assert dialog.tabs.tabText(2) == "Sources"
    assert dialog.add_source_button.isEnabled()
    assert not dialog.edit_source_button.isEnabled()
    assert not dialog.remove_source_button.isEnabled()
    assert not dialog.test_source_button.isEnabled()


# ----------------------------------------------------------------------
# Add / edit / remove
# ----------------------------------------------------------------------


def test_add_gitea_source_is_slugged_and_saved(tmp_path, monkeypatch):
    accept_with(
        monkeypatch, name="Forgejo Home", kind="gitea", address="https://git.example.org/", token="tok_one"
    )
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.add_source_button.click()

    assert dialog.sources_table.rowCount() == 4
    assert cell(dialog, 3, 0) == "Forgejo Home"
    assert cell(dialog, 3, 1) == "Gitea · Forgejo · Codeberg"
    assert cell(dialog, 3, 2) == "https://git.example.org"
    assert cell(dialog, 3, 3) == "✓ set"
    added = dialog.get_settings().sources[-1]
    assert (added.id, added.kind, added.base_url, added.token) == (
        "forgejo-home",
        "gitea",
        "https://git.example.org",
        "tok_one",
    )


def test_added_source_ids_stay_unique(tmp_path, monkeypatch):
    dialog = SettingsDialog(make_settings(tmp_path))

    accept_with(monkeypatch, name="Forgejo Home", kind="gitea", address="https://git.example.org")
    dialog.add_source_button.click()
    accept_with(monkeypatch, name="Forgejo Home", kind="gitea", address="https://git2.example.org")
    dialog.add_source_button.click()
    accept_with(monkeypatch, name="GitHub", kind="github", address="https://ghe.example.com")
    dialog.add_source_button.click()

    ids = [source.id for source in dialog.get_settings().sources]
    assert ids == ["github", "gitlab", "codeberg", "forgejo-home", "forgejo-home-2", "github-2"]
    assert len(set(ids)) == len(ids)


def test_edit_keeps_id_and_updates_name_and_token(tmp_path, monkeypatch):
    accept_with(monkeypatch, name="GitLab Work", token="glpat-abc")
    dialog = SettingsDialog(make_settings(tmp_path))
    select(dialog, 1)
    dialog.edit_source_button.click()

    assert cell(dialog, 1, 0) == "GitLab Work"
    assert cell(dialog, 1, 3) == "✓ set"
    assert dialog.get_settings().sources[1].id == "gitlab"


def test_double_click_row_edits_it(tmp_path, monkeypatch):
    accept_with(monkeypatch, name="Renamed")
    dialog = SettingsDialog(make_settings(tmp_path))
    dialog.sources_table.doubleClicked.emit(dialog.sources_table.model().index(2, 0))

    assert cell(dialog, 2, 0) == "Renamed"


def test_remove_non_github_source(tmp_path):
    base = make_settings(tmp_path)
    dialog = SettingsDialog(base)
    select(dialog, 2)  # Codeberg
    assert dialog.remove_source_button.isEnabled()
    dialog.remove_source_button.click()

    assert dialog.sources_table.rowCount() == 2
    assert [s.id for s in dialog.get_settings().sources] == ["github", "gitlab"]
    assert len(base.sources) == 3  # the original settings are untouched


def test_github_source_cannot_be_removed(tmp_path):
    dialog = SettingsDialog(make_settings(tmp_path))
    select(dialog, 0)

    assert not dialog.remove_source_button.isEnabled()
    assert dialog.remove_source_button.toolTip()
    dialog.remove_source_button.click()  # disabled, so nothing happens
    assert dialog.sources_table.rowCount() == 3


# ----------------------------------------------------------------------
# Enabled / Include in search / GitHub token
# ----------------------------------------------------------------------


def test_checkboxes_are_saved_to_sources(tmp_path):
    base = make_settings(tmp_path)
    dialog = SettingsDialog(base)
    dialog.sources_table.item(1, 4).setCheckState(Qt.CheckState.Unchecked)  # GitLab: Enabled
    dialog.sources_table.item(1, 5).setCheckState(Qt.CheckState.Unchecked)  # GitLab: Include in search
    dialog.sources_table.item(0, 4).setCheckState(Qt.CheckState.Unchecked)  # GitHub: Enabled

    sources = dialog.get_settings().sources
    assert (sources[1].enabled, sources[1].searchable) == (False, False)
    assert sources[0].enabled is False
    assert (sources[2].enabled, sources[2].searchable) == (True, True)
    # Edits are made on a copy of the list.
    assert base.sources[1].enabled is True
    assert base.sources[1].searchable is True


def test_github_token_edit_is_mirrored_into_github_token(tmp_path, monkeypatch):
    base = make_settings(tmp_path)
    accept_with(monkeypatch, token="ghp_new")
    dialog = SettingsDialog(base)
    select(dialog, 0)
    dialog.edit_source_button.click()

    settings = dialog.get_settings()
    assert settings.sources[0].token == "ghp_new"
    assert settings.github_token == "ghp_new"
    assert cell(dialog, 0, 3) == "✓ set"
    # The original settings object was not changed.
    assert base.github_token == ""
    assert base.sources[0].token == ""


def test_github_token_from_settings_is_kept_in_both_places(tmp_path):
    dialog = SettingsDialog(make_settings(tmp_path, github_token="ghp_keep"))
    settings = dialog.get_settings()
    assert settings.github_token == "ghp_keep"
    assert settings.source("github").token == "ghp_keep"


# ----------------------------------------------------------------------
# Connection tests
# ----------------------------------------------------------------------


def test_sources_tab_test_reports_success(tmp_path, monkeypatch):
    provider = FakeProvider(remaining=4999, name="GitHub")
    seen = patch_provider(monkeypatch, provider)
    dialog = SettingsDialog(make_settings(tmp_path, github_token="ghp_secret123"))
    select(dialog, 0)
    dialog.test_source_button.click()

    assert dialog.sources_result.text() == "✓ Connected to GitHub · 4999 requests left"
    assert "ghp_secret123" not in dialog.sources_result.text()
    assert provider.queries == [("test", 1)]
    assert provider.closed
    assert seen[0].token == "ghp_secret123"


def test_sources_tab_test_reports_failure_without_the_token(tmp_path, monkeypatch):
    patch_provider(monkeypatch, FakeProvider(error=Exception("boom")))
    dialog = SettingsDialog(make_settings(tmp_path, github_token="ghp_secret123"))
    select(dialog, 0)
    dialog.test_source_button.click()

    assert dialog.sources_result.text() == "✗ boom"
    assert "ghp_secret123" not in dialog.sources_result.text()


def test_error_text_masks_the_token(tmp_path, monkeypatch):
    patch_provider(monkeypatch, FakeProvider(error=Exception("rejected token ghp_secret123")))
    dialog = SettingsDialog(make_settings(tmp_path, github_token="ghp_secret123"))
    select(dialog, 0)
    dialog.test_source_button.click()

    assert dialog.sources_result.text() == "✗ rejected token ***"


def test_sources_tab_test_reports_unavailable_kind(tmp_path, monkeypatch):
    patch_provider(monkeypatch, None)
    dialog = SettingsDialog(make_settings(tmp_path))
    select(dialog, 1)
    dialog.test_source_button.click()

    assert dialog.sources_result.text() == "✗ This source type isn't available in this build"


def test_dialog_test_connection_success_without_request_count(tmp_path, monkeypatch):
    patch_provider(monkeypatch, FakeProvider(remaining=None))
    dialog = SourceDialog(others=[])
    dialog.name_edit.setText("Codeberg")
    select_data(dialog.type_combo, "gitea")
    dialog.address_edit.setText("https://codeberg.org")
    dialog.test_button.click()

    assert dialog.test_result.text() == "✓ Connected to Codeberg"


def test_dialog_test_connection_failure_hides_token(tmp_path, monkeypatch):
    patch_provider(monkeypatch, FakeProvider(error=Exception("401 for tok_SECRET")))
    dialog = SourceDialog(others=[])
    dialog.address_edit.setText("https://gitlab.com")
    select_data(dialog.type_combo, "gitlab")
    dialog.token_edit.setText("tok_SECRET")
    dialog.test_button.click()

    assert dialog.test_result.text().startswith("✗ ")
    assert "tok_SECRET" not in dialog.test_result.text()


def test_dialog_test_refuses_a_non_https_address(monkeypatch):
    seen = patch_provider(monkeypatch, FakeProvider())
    dialog = SourceDialog(others=[])
    dialog.address_edit.setText("http://git.example.org")
    dialog.test_button.click()

    assert dialog.test_result.text() == "✗ Enter an https:// address first."
    assert seen == []


# ----------------------------------------------------------------------
# SourceDialog validation and ids
# ----------------------------------------------------------------------


def test_dialog_requires_a_name(warnings):
    dialog = SourceDialog(others=[])
    dialog.name_edit.setText("   ")
    dialog.address_edit.setText("https://gitlab.com")
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1


@pytest.mark.parametrize("address", ["gitlab.com", "http://gitlab.com", "https://"])
def test_dialog_requires_an_https_address_with_a_host(warnings, address):
    dialog = SourceDialog(others=[])
    dialog.name_edit.setText("Work")
    dialog.address_edit.setText(address)
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1


def test_dialog_warns_about_a_duplicate_address_for_the_same_type(warnings):
    dialog = SourceDialog(others=default_sources())
    dialog.name_edit.setText("Mirror")
    select_data(dialog.type_combo, "github")
    dialog.address_edit.setText("https://GitHub.com/")
    dialog._validate_and_accept()

    assert dialog.result() != QDialog.Accepted
    assert len(warnings) == 1
    assert "GitHub" in warnings[0]


def test_same_address_with_another_type_is_allowed(warnings):
    dialog = SourceDialog(others=default_sources())
    dialog.name_edit.setText("Mirror")
    select_data(dialog.type_combo, "gitlab")
    dialog.address_edit.setText("https://github.com")
    dialog._validate_and_accept()

    assert dialog.result() == QDialog.Accepted
    assert warnings == []


def test_editing_a_source_does_not_clash_with_itself(warnings):
    github, *others = default_sources()
    dialog = SourceDialog(source=github, others=others)
    dialog._validate_and_accept()

    assert dialog.result() == QDialog.Accepted
    assert warnings == []


def test_new_source_id_is_slugged_and_unique():
    dialog = SourceDialog(others=default_sources())
    dialog.name_edit.setText("GitHub")
    assert dialog.get_config().id == "github-2"
    dialog.name_edit.setText("Work GitLab!!")
    assert dialog.get_config().id == "work-gitlab"


def test_editing_keeps_the_existing_id():
    gitlab = default_sources()[1]
    dialog = SourceDialog(source=gitlab, others=[])
    dialog.name_edit.setText("Renamed GitLab")
    config = dialog.get_config()
    assert (config.id, config.name) == ("gitlab", "Renamed GitLab")


def test_address_is_stripped_of_trailing_slashes():
    dialog = SourceDialog(others=[])
    dialog.name_edit.setText("Forge")
    select_data(dialog.type_combo, "gitea")
    dialog.address_edit.setText("  https://forge.example.org//  ")
    assert dialog.get_config().base_url == "https://forge.example.org"


def test_address_placeholder_follows_the_type():
    dialog = SourceDialog(others=[])
    assert dialog.address_edit.placeholderText() == "https://github.com"
    select_data(dialog.type_combo, "gitlab")
    assert dialog.address_edit.placeholderText() == "https://gitlab.com"
    select_data(dialog.type_combo, "gitea")
    assert dialog.address_edit.placeholderText() == "https://codeberg.org"


def test_token_help_names_the_token_for_each_type():
    dialog = SourceDialog(others=[])
    assert "fine-grained token with public read access" in dialog.token_help.text()
    select_data(dialog.type_combo, "gitlab")
    assert "read_api" in dialog.token_help.text()
    select_data(dialog.type_combo, "gitea")
    assert "read:repository" in dialog.token_help.text()


def test_gh_cli_button_only_shows_for_github_when_gh_is_installed(monkeypatch):
    monkeypatch.setattr(settings_dialog, "find_gh_cli", lambda: "C:/tools/gh.exe")
    dialog = SourceDialog(others=[])
    assert dialog.gh_cli_button.isVisibleTo(dialog)
    select_data(dialog.type_combo, "gitlab")
    assert not dialog.gh_cli_button.isVisibleTo(dialog)

    monkeypatch.setattr(settings_dialog, "find_gh_cli", lambda: None)
    fresh = SourceDialog(others=[])
    assert not fresh.gh_cli_button.isVisibleTo(fresh)


def test_gh_cli_login_fills_the_token(monkeypatch):
    monkeypatch.setattr(settings_dialog, "find_gh_cli", lambda: "gh.exe")
    monkeypatch.setattr(settings_dialog, "read_gh_cli_token", lambda: ("gho_abc", ""))
    dialog = SourceDialog(others=[])
    dialog.gh_cli_button.click()

    assert dialog.token_edit.text() == "gho_abc"
    assert dialog.test_result.text().startswith("✓ Copied the token")


def test_gh_cli_login_failure_is_shown(monkeypatch):
    message = 'The GitHub CLI isn\'t signed in. Run "gh auth login" first.'
    monkeypatch.setattr(settings_dialog, "find_gh_cli", lambda: "gh.exe")
    monkeypatch.setattr(settings_dialog, "read_gh_cli_token", lambda: ("", message))
    dialog = SourceDialog(others=[])
    dialog.gh_cli_button.click()

    assert dialog.token_edit.text() == ""
    assert dialog.test_result.text() == message


def test_token_show_toggle():
    dialog = SourceDialog(others=[])
    assert dialog.token_edit.echoMode() == QLineEdit.EchoMode.Password
    dialog.show_token.setChecked(True)
    assert dialog.token_edit.echoMode() == QLineEdit.EchoMode.Normal
    dialog.show_token.setChecked(False)
    assert dialog.token_edit.echoMode() == QLineEdit.EchoMode.Password
