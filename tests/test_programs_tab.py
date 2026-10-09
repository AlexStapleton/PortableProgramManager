"""Managed Programs tab: model, filter, details, enablement and actions (no real launches)."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from portable_manager.controller import AppController, UpdateCheckReport
from portable_manager.installer import normalize_dir
from portable_manager.models import ManagedProgram, UpdatePolicy
from portable_manager.storage import Storage
from portable_manager.ui import programs_tab as tab_mod
from portable_manager.ui.details_view import ProgramDetailsView
from portable_manager.ui.programs_model import (
    LAST_RUN,
    NAME,
    PROGRAM_ID_ROLE,
    SOURCE,
    STATUS,
    VERSION,
    ProgramsFilterProxy,
    ProgramsModel,
    friendly_time,
    policy_summary,
    program_status,
    relative_time,
)
from portable_manager.ui.programs_tab import ProgramsTab


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeHost:
    """Runs tasks synchronously and records what the tab reports."""

    def __init__(self, controller: AppController, settings: QSettings) -> None:
        self.controller = controller
        self._settings = settings
        self.statuses: list[str] = []
        self.warnings: list[str] = []
        self.refreshes = 0

    def start_task(self, fn, *args, start_message, on_result, error_prefix, lock_widgets=None, silent_errors=False):
        self.statuses.append(start_message)
        try:
            result = fn(*args, progress_callback=None)
        except Exception as exc:  # noqa: BLE001 - surfaced as a warning, like the real host
            self.warnings.append(f"{error_prefix}: {exc}")
            return
        on_result(result)

    def set_status(self, message: str) -> None:
        self.statuses.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def ui_state(self) -> QSettings:
        return self._settings

    def refresh_programs(self) -> None:
        self.refreshes += 1


NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    """A timestamp relative to the real clock (the tab formats times against it)."""
    return (datetime.now(timezone.utc) - delta).isoformat()


def _add_program(controller: AppController, apps: Path, pid: str, name: str, folder: str, **kwargs) -> ManagedProgram:
    install = apps / folder
    install.mkdir(parents=True, exist_ok=True)
    exe = install / f"{folder}.exe"
    exe.write_bytes(b"MZ")
    kwargs.setdefault("launch_path", str(exe))
    program = ManagedProgram(
        program_id=pid,
        name=name,
        source_type=kwargs.pop("source_type", "github_repo"),
        source_value=kwargs.pop("source_value", "acme/tool"),
        install_dir=str(install),
        version=kwargs.pop("version", "1.0"),
        repo_full_name=kwargs.pop("repo_full_name", "acme/tool"),
        homepage_url=kwargs.pop("homepage_url", "https://github.com/acme/tool"),
        **kwargs,
    )
    controller._upsert_program(program)
    return program


@pytest.fixture
def env(qapp, tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    controller = AppController(Storage(base_dir=tmp_path / "data"))
    settings = controller.settings
    settings.install_root = str(apps)
    controller.update_settings(settings)

    _add_program(
        controller, apps, "p-notepad", "Notepad Plus", "NotepadPlus",
        version="8.6", source_value="notepad-plus/notepad", repo_full_name="notepad-plus/notepad",
        last_update_status="no_update", last_checked_at=_iso(timedelta(minutes=5)),
        last_run_at=_iso(timedelta(minutes=4)),
    )
    _add_program(
        controller, apps, "p-video", "Video Player", "VideoPlayer",
        version="2.0.4", source_value="acme/video-player", repo_full_name="acme/video-player",
        update_available=True, latest_upstream_version="v2.1.0", last_update_status="update_available",
        last_run_at=_iso(timedelta(hours=3)),
        update_policy=UpdatePolicy(update_mode="download_only", schedule_type="interval", interval_hours=6),
    )
    _add_program(
        controller, apps, "p-archive", "Archiver", "Archiver",
        version="1.9.0", source_value="acme/archiver", repo_full_name="acme/archiver",
        last_update_status="check_failed", last_error_message="GitHub API rate limit exceeded.",
        last_error_at=_iso(timedelta(minutes=20)), last_run_at=_iso(timedelta(days=3)),
    )
    _add_program(controller, apps, "p-old", "Old Tool", "OldTool", version="0.3", source_value="old/tool", repo_full_name="old/tool")
    shutil.rmtree(apps / "OldTool")  # install folder is gone
    _add_program(
        controller, apps, "p-pending", "Pending Tool", "PendingTool",
        version="2.4", source_value="pend/tool", repo_full_name="pend/tool",
        update_available=True, latest_upstream_version="3.0", pending_update_version="3.0",
        pending_update_file=str(tmp_path / "pending.zip"), last_update_status="update_available",
    )
    _add_program(
        controller, apps, "p-fresh", "Fresh Install", "FreshInstall",
        version="1.0", source_type="direct_url", source_value="https://example.com/fresh.exe",
        repo_full_name=None, homepage_url="https://example.com/fresh.exe",
        launch_path=None, last_update_status="not_yet_checked",
    )

    host = FakeHost(controller, QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat))
    tab = ProgramsTab(host)
    tab.resize(1100, 700)
    yield SimpleNamespace(tab=tab, host=host, controller=controller, apps=apps, tmp=tmp_path)
    tab.deleteLater()


def _select(tab: ProgramsTab, program_id: str) -> None:
    source = tab._model.index(tab._model.row_of(program_id), NAME)
    tab._table.selectRow(tab._proxy.mapFromSource(source).row())


def _status_text(model: ProgramsModel, program_id: str) -> str:
    return model.data(model.index(model.row_of(program_id), STATUS))


def _choose_remove(monkeypatch, label: str | None) -> list[bool]:
    """Pick a button in the remove dialog (None = Cancel). Returns a list that records dialog opens."""
    opened: list[bool] = []

    def fake_exec(self):
        opened.append(True)
        return 0

    def fake_clicked(self):
        for button in self.buttons():
            if label is not None and button.text() == label:
                return button
        return None

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", fake_clicked)
    return opened


# ---------------------------------------------------------------------------
# Model and status
# ---------------------------------------------------------------------------

def test_model_columns_and_status_texts(env):
    model = env.tab._model
    assert [model.headerData(c, Qt.Horizontal) for c in range(5)] == ["Name", "Version", "Status", "Last run", "Source"]
    assert model.rowCount() == 6

    expected = {
        "p-notepad": "Up to date",
        "p-video": "Update available (v2.1.0)",
        "p-archive": "Check failed",
        "p-old": "Folder missing",
        "p-pending": "Update downloaded — ready to install",
        "p-fresh": "Not checked yet",
    }
    for program_id, text in expected.items():
        assert _status_text(model, program_id) == text

    row = model.row_of("p-video")
    assert model.data(model.index(row, NAME)) == "Video Player"
    assert model.data(model.index(row, VERSION)) == "2.0.4"
    assert model.data(model.index(row, SOURCE)) == "GitHub · acme/video-player"
    assert model.data(model.index(row, NAME), PROGRAM_ID_ROLE) == "p-video"
    assert model.data(model.index(row, LAST_RUN)) == "3h ago"
    # Every column answers Qt.UserRole with the program id.
    assert all(model.data(model.index(row, c), Qt.UserRole) == "p-video" for c in range(5))


def test_status_priority_permission_then_missing_then_running_then_updates(env):
    controller = env.controller
    video = controller.get_program("p-video")

    assert program_status(video, folder_missing=False).text == "Update available (v2.1.0)"
    assert program_status(video, folder_missing=False, running=["VideoPlayer.exe (7)"]).text == "Running"
    assert program_status(video, folder_missing=True, running=["VideoPlayer.exe (7)"]).text == "Folder missing"
    assert program_status(video, folder_missing=True, permission_problem=True).text == "Needs permission repair"
    assert program_status(video, folder_missing=False, permission_problem=True).kind == "error"

    model = env.tab._model
    model.set_permission_problems({"p-video"})
    assert _status_text(model, "p-video") == "Needs permission repair"
    model.set_permission_problems(set())
    assert _status_text(model, "p-video") == "Update available (v2.1.0)"

    # Running state arrives keyed by normalised folder and keeps the row's selection intact.
    key = normalize_dir(video.install_dir)
    assert model.set_running({key: ["VideoPlayer.exe (42)"]}) is True
    assert _status_text(model, "p-video") == "Running"
    assert model.set_running({key: ["VideoPlayer.exe (42)"]}) is False
    model.set_running({})
    assert _status_text(model, "p-video") == "Update available (v2.1.0)"


def test_check_failed_tooltip_is_the_error(env):
    model = env.tab._model
    row = model.row_of("p-archive")
    assert model.data(model.index(row, STATUS), Qt.ToolTipRole) == "GitHub API rate limit exceeded."


def test_relative_time_and_policy_wording():
    def at(delta: timedelta) -> str:
        return (NOW - delta).isoformat()

    assert relative_time(at(timedelta(seconds=10)), NOW) == "Just now"
    assert relative_time(at(timedelta(minutes=5)), NOW) == "5m ago"
    assert relative_time(at(timedelta(hours=3)), NOW) == "3h ago"
    assert relative_time(at(timedelta(days=2)), NOW) == "2d ago"
    assert relative_time(at(timedelta(days=30)), NOW) == "2026-09-09"
    assert relative_time(None, NOW) == ""
    assert friendly_time(at(timedelta(hours=3)), NOW).startswith("3h ago (")
    assert policy_summary(UpdatePolicy(update_mode="notify_only", schedule_type="daily")) == "Checks daily · notifies you"
    assert policy_summary(UpdatePolicy(update_mode="manual")) == "Checks only when you click Check"
    assert policy_summary(UpdatePolicy(check_enabled=False)) == "Update checks are off"


# ---------------------------------------------------------------------------
# Filter and sorting
# ---------------------------------------------------------------------------

def test_filter_proxy_matches_name_and_status(env):
    model = env.tab._model
    proxy = ProgramsFilterProxy()
    proxy.setSourceModel(model)

    assert proxy.rowCount() == 6
    proxy.set_filter_text("VIDEO")  # case-insensitive name match
    assert proxy.rowCount() == 1
    proxy.set_filter_text("check failed")  # status text
    assert proxy.rowCount() == 1
    proxy.set_filter_text("acme/archiver")  # source
    assert proxy.rowCount() == 1
    proxy.set_filter_text("update available")
    assert proxy.rowCount() == 1  # only Video Player; "Update downloaded" does not contain it
    proxy.set_filter_text("2.1.0")  # latest version
    assert proxy.rowCount() == 1
    proxy.set_filter_text("no such program")
    assert proxy.rowCount() == 0
    proxy.set_filter_text("")
    assert proxy.rowCount() == 6


def test_sort_by_name_and_last_run(env):
    proxy = ProgramsFilterProxy()
    proxy.setSourceModel(env.tab._model)

    def names():
        return [proxy.index(r, NAME).data() for r in range(proxy.rowCount())]

    proxy.sort(NAME, Qt.AscendingOrder)
    assert names() == sorted(names(), key=str.lower)
    assert names()[0] == "Archiver"
    proxy.sort(NAME, Qt.DescendingOrder)
    assert names()[0] == "Video Player"

    # Last run: never-run programs sort first ascending, most recent last.
    proxy.sort(LAST_RUN, Qt.AscendingOrder)
    assert proxy.index(proxy.rowCount() - 1, NAME).data() == "Notepad Plus"


def test_tab_filter_updates_count_and_rows(env):
    tab = env.tab
    tab._filter_edit.setText("archiver")
    assert tab._filter_timer.isActive()  # debounced
    tab._apply_filter()
    assert tab._proxy.rowCount() == 1
    assert "1 of 6 programs shown" in tab._count_label.text()
    tab._filter_edit.setText("")
    tab._apply_filter()
    assert tab._proxy.rowCount() == 6
    assert tab._count_label.text() == "6 programs · 2 updates available"


# ---------------------------------------------------------------------------
# Tab state: empty page, enablement, selection
# ---------------------------------------------------------------------------

def test_empty_state_with_no_programs_and_table_with_programs(qapp, tmp_path):
    controller = AppController(Storage(base_dir=tmp_path / "empty-data"))
    host = FakeHost(controller, QSettings(str(tmp_path / "empty.ini"), QSettings.IniFormat))
    tab = ProgramsTab(host)
    assert tab._stack.currentIndex() == 1
    assert tab._count_label.text() == "0 programs"
    requested: list[bool] = []
    tab.discover_requested.connect(lambda: requested.append(True))
    find = next(b for b in tab._stack.currentWidget().findChildren(QPushButton) if b.text() == "Find programs")
    find.click()
    assert requested == [True]

    _add_program(controller, tmp_path, "p-one", "One", "One")
    tab.refresh()
    assert tab._stack.currentIndex() == 0
    assert not tab._toolbar.isHidden()
    tab.deleteLater()


def test_button_enablement(env):
    tab = env.tab
    tab.show()
    QApplication.processEvents()

    assert tab.selected_program() is None
    assert not tab._run_button.isEnabled()
    assert tab._update_button.isHidden()
    assert not tab._check_button.isEnabled()
    assert not tab._more_button.isEnabled()
    assert tab._check_all_button.isEnabled()

    _select(tab, "p-fresh")  # no launch path, direct download
    assert not tab._run_button.isEnabled()
    assert "no launch file" in tab._run_button.toolTip()
    assert not tab._check_button.isEnabled()
    assert tab._update_button.isHidden()

    _select(tab, "p-video")  # runnable, update available, GitHub repo
    assert tab._run_button.isEnabled()
    assert not tab._update_button.isHidden() and tab._update_button.isEnabled()
    assert tab._check_button.isEnabled()
    assert tab._act_repair.isEnabled() is False

    _select(tab, "p-old")  # folder missing
    assert not tab._run_button.isEnabled()
    assert "folder is missing" in tab._run_button.toolTip()

    _select(tab, "p-notepad")  # up to date
    assert tab._update_button.isHidden()


def test_repair_enabled_only_for_permission_problems(env):
    tab = env.tab
    tab.set_permission_problems({"p-notepad": ["C:/x/locked.dat"]})
    _select(tab, "p-notepad")
    assert tab._act_repair.isEnabled()
    _select(tab, "p-video")
    assert not tab._act_repair.isEnabled()
    # Context menu always offers repair.
    menu = tab._build_context_menu(env.controller.get_program("p-video"))
    assert "Repair permissions" in [a.text() for a in menu.actions()]
    menu.deleteLater()


def test_selection_is_kept_across_refresh(env):
    tab = env.tab
    _select(tab, "p-archive")
    _add_program(env.controller, env.apps, "p-zeta", "Zeta", "Zeta")
    tab.refresh()
    assert tab.selected_program_id() == "p-archive"
    assert tab.selected_program().name == "Archiver"
    assert tab._model.rowCount() == 7


def test_running_scan_result_updates_status_and_details(env):
    tab = env.tab
    _select(tab, "p-video")
    video = env.controller.get_program("p-video")
    tab._on_scan_done({normalize_dir(video.install_dir): ["VideoPlayer.exe (99)"]})
    assert _status_text(tab._model, "p-video") == "Running"
    assert "VideoPlayer.exe (99)" in tab._details.toPlainText()
    tab._on_scan_done(None)  # a failed scan changes nothing
    assert _status_text(tab._model, "p-video") == "Running"


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------

def test_details_are_friendly_and_hide_internal_fields(env):
    tab = env.tab
    _select(tab, "p-video")
    text = tab._details.toPlainText()
    for wanted in ("Installed in", "Launches", "Source", "Release channel", "Update checks", "Last checked", "Last run"):
        assert wanted in text
    assert "Checks every 6 hours · downloads, you install" in text
    assert "v2.1.0 is available" in text
    for internal in ("Program ID", "Source ID", "Installed Asset Name", "p-video"):
        assert internal not in text


def test_details_show_last_error_and_empty_state(qapp, env):
    view = ProgramDetailsView()
    view.show_program(None)
    assert "Select a program to see its details." in view.toPlainText()

    archive = env.controller.get_program("p-archive")
    view.show_program(archive, running=[], permission_problem=False)
    assert "Last update check failed" in view.toPlainText()
    assert "GitHub API rate limit exceeded." in view.toPlainText()
    view.deleteLater()


def test_details_folder_link_opens_install_folder(qapp, env, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    view = ProgramDetailsView()
    video = env.controller.get_program("p-video")
    view.show_program(video)
    view._on_anchor_clicked(QUrl("folder:open"))
    assert opened == [video.install_dir]
    view.deleteLater()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def test_run_calls_controller_and_skips_programs_that_cannot_start(env, monkeypatch):
    started: list[str] = []
    def fake_run(pid, progress_callback=None):
        started.append(pid)
        return env.controller.get_program(pid)

    monkeypatch.setattr(env.controller, "run_program", fake_run)
    env.tab.run_program("p-fresh")
    assert started == []
    assert any("no launch file" in w for w in env.host.warnings)
    env.tab.run_program("p-video")
    assert started == ["p-video"]
    assert env.host.statuses[-1] == "Started Video Player."


def test_enter_key_runs_the_selected_program(env, monkeypatch):
    started: list[str] = []

    def fake_run(pid, progress_callback=None):
        started.append(pid)
        return env.controller.get_program(pid)

    monkeypatch.setattr(env.controller, "run_program", fake_run)
    _select(env.tab, "p-notepad")
    env.tab._table.setFocus()
    QTest.keyClick(env.tab._table, Qt.Key_Return)
    assert started == ["p-notepad"]


def test_run_as_admin_launches_a_copy_with_the_flag(env, monkeypatch):
    launched = []
    monkeypatch.setattr(tab_mod, "launch_program", lambda program: launched.append(program))
    env.tab._run_as_admin("p-video")
    assert len(launched) == 1 and launched[0].run_as_admin is True
    assert env.controller.get_program("p-video").run_as_admin is False


def test_check_all_refreshes_and_reports(env, monkeypatch):
    checked = env.controller.list_programs()
    report = UpdateCheckReport(checked=checked, newly_available=[])
    monkeypatch.setattr(env.controller, "check_for_updates_all", lambda progress_callback=None: report)
    env.tab._check_all()
    assert env.host.refreshes == 1
    assert env.host.statuses[-1].startswith("Checked 6 program(s).")


def test_context_menu_items_follow_the_program(env):
    tab = env.tab
    video_menu = tab._build_context_menu(env.controller.get_program("p-video"))
    texts = [a.text() for a in video_menu.actions() if a.text()]
    for wanted in ("Run", "Run as administrator", "Install update", "Check for updates", "Open folder",
                   "Copy folder path", "Open project page", "Edit…", "Repair permissions", "Remove…"):
        assert wanted in texts
    video_menu.deleteLater()

    fresh_menu = tab._build_context_menu(env.controller.get_program("p-fresh"))
    texts = [a.text() for a in fresh_menu.actions() if a.text()]
    assert "Install update" not in texts
    assert "Open project page" not in texts
    run = next(a for a in fresh_menu.actions() if a.text() == "Run")
    assert not run.isEnabled()
    fresh_menu.deleteLater()


def test_remove_only_removes_the_program_and_keeps_files(env, monkeypatch):
    opened = _choose_remove(monkeypatch, "Remove only")
    _select(env.tab, "p-archive")
    env.tab._remove("p-archive")
    assert opened == [True]
    assert "p-archive" not in [p.program_id for p in env.controller.list_programs()]
    assert (env.apps / "Archiver").is_dir()
    assert env.tab.selected_program() is None


def test_remove_and_delete_files_deletes_the_folder(env, monkeypatch):
    _choose_remove(monkeypatch, "Remove and delete files")
    env.tab._remove("p-pending")
    assert "p-pending" not in [p.program_id for p in env.controller.list_programs()]
    assert not (env.apps / "PendingTool").exists()


def test_delete_key_opens_remove_flow_and_cancel_keeps_program(env, monkeypatch):
    opened = _choose_remove(monkeypatch, None)  # Cancel
    _select(env.tab, "p-archive")
    env.tab._table.setFocus()
    QTest.keyClick(env.tab._table, Qt.Key_Delete)
    assert opened == [True]
    assert env.controller.get_program("p-archive").name == "Archiver"


def test_repair_success_clears_the_problem(env, monkeypatch):
    env.tab.set_permission_problems({"p-notepad": ["C:/x/locked.dat"]})
    monkeypatch.setattr(env.controller, "repair_permissions", lambda pid, progress_callback=None: env.controller.get_program(pid))
    env.tab._repair("p-notepad")
    assert env.tab._model.is_permission_problem("p-notepad") is False
    assert "Permissions repaired for Notepad Plus." in env.host.statuses


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_splitter_and_header_state_persist_in_ui_state(env, qapp):
    tab = env.tab
    tab._splitter.setSizes([300, 400])
    sizes = tab._splitter.sizes()  # Qt scales them to the widget's height
    tab._save_splitter_state()
    tab._save_header_state()
    settings = env.host.ui_state()
    assert [int(v) for v in settings.value("programs/splitter")] == sizes
    assert settings.value("programs/header") is not None

    # A new tab on the same settings restores the splitter sizes.
    second = ProgramsTab(FakeHost(env.controller, settings))
    assert second._splitter.sizes() == sizes
    second.deleteLater()
