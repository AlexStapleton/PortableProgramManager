from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from portable_manager.controller import InstallOutcome
from portable_manager.github_client import GitHubClient, GitHubRepo
from portable_manager.models import ManagedProgram
from portable_manager.ui import discover_tab as discover_module
from portable_manager.ui.discover_tab import (
    COL_INSTALLED,
    COL_NAME,
    COL_STARS,
    DiscoverTab,
    _EMPTY_TEXT,
    apply_filters,
    format_stars,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def iso_days_ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def make_repo(full_name: str, stars: int, description: str = "A portable Windows tool",
              pushed_days: int = 10) -> GitHubRepo:
    owner, name = full_name.split("/")
    pushed = iso_days_ago(pushed_days)
    return GitHubRepo(
        full_name=full_name,
        name=name,
        owner=owner,
        description=description,
        html_url=f"https://github.com/{full_name}",
        homepage="",
        stars=stars,
        updated_at=pushed,
        default_branch="main",
        pushed_at=pushed,
    )


def make_program(name: str, repo: str | None = None, launch_path: str | None = r"C:\Portable\app.exe",
                 version: str = "1.2.0") -> ManagedProgram:
    return ManagedProgram(
        program_id=f"id-{name}",
        name=name,
        source_type="github_repo" if repo else "direct_url",
        source_value=f"https://github.com/{repo}" if repo else f"https://example.com/{name}.zip",
        install_dir=rf"C:\Portable\{name}",
        launch_path=launch_path,
        version=version,
        repo_full_name=repo,
    )


class FakeController:
    def __init__(self) -> None:
        self.results_by_query: dict[str, list[GitHubRepo]] = {}
        self.enrichment: dict[str, dict] = {}     # full_name -> fields fetch_release_dates fills in
        self.installed: dict[str, ManagedProgram] = {}  # repo full name or URL -> installed program
        self.install_outcome: InstallOutcome | None = None
        self.settings = SimpleNamespace(auto_open_folder_after_install=False)
        self.github = GitHubClient(token="")       # real URL parsing, no network calls are made
        self.search_calls: list[tuple[str, str]] = []
        self.include_forks_calls: list[bool] = []
        self.install_repo_calls: list[tuple[str, str]] = []
        self.install_url_calls: list[str] = []
        self.run_calls: list[str] = []

    def close(self) -> None:
        self.github.close()

    def search_github(self, query, sort="stars", include_forks=False, source_id=None, progress_callback=None):
        self.search_source_calls = getattr(self, "search_source_calls", []) + [source_id]
        self.search_calls.append((query, sort))
        self.include_forks_calls.append(include_forks)
        return list(self.results_by_query.get(query, []))

    def fetch_release_dates(self, repos, progress_callback=None):
        # Like the real controller: returns enriched copies, never mutates the input.
        return [dataclasses.replace(repo, **self.enrichment.get(repo.full_name, {})) for repo in repos]

    def install_from_repo(self, repo_full_name, channel="latest_release", source_id="github", progress_callback=None):
        self.install_repo_calls.append((repo_full_name, channel))
        return self._outcome(repo_full_name)

    def install_from_url(self, url, display_name=None, progress_callback=None):
        self.install_url_calls.append(url)
        return self._outcome(url)

    def _outcome(self, key: str) -> InstallOutcome:
        if self.install_outcome is not None:
            return self.install_outcome
        return InstallOutcome(program=make_program(key.split("/")[-1], repo=None))

    def find_managed_program(self, repo_full_name=None, url=None, source_id="github"):
        return self.installed.get(repo_full_name or url)

    def run_program(self, program_id, progress_callback=None):
        self.run_calls.append(program_id)
        return make_program(program_id)


class FakeHost:
    """Runs tasks synchronously unless ``hold`` is set, in which case they wait in ``held``."""

    def __init__(self, controller: FakeController) -> None:
        self.controller = controller
        self.statuses: list[str] = []
        self.warnings: list[str] = []
        self.refresh_count = 0
        self.hold = False
        self.held: list[tuple] = []

    def start_task(self, fn, *args, start_message, on_result, error_prefix,
                   lock_widgets=None, silent_errors=False):
        self.statuses.append(start_message)
        locks = list(lock_widgets or [])
        for widget in locks:
            widget.setEnabled(False)  # the real host does this when a task starts
        job = (fn, args, on_result, error_prefix, locks)
        if self.hold:
            self.held.append(job)
        else:
            self.run(job)

    def run(self, job) -> None:
        fn, args, on_result, _error_prefix, locks = job
        try:
            result = fn(*args, progress_callback=None)
            on_result(result)
        finally:
            for widget in locks:
                widget.setEnabled(True)  # the real host re-enables when a task finishes

    def set_status(self, message: str) -> None:
        self.statuses.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def refresh_programs(self) -> None:
        self.refresh_count += 1


class RecordingBox(QMessageBox):
    """Stands in for QMessageBox in the tab module: records prompts, never blocks."""

    state: SimpleNamespace = SimpleNamespace()

    @staticmethod
    def question(parent, title, text, *args, **kwargs):
        RecordingBox.state.questions.append(text)
        return RecordingBox.state.answer

    @staticmethod
    def information(parent, title, text, *args, **kwargs):
        RecordingBox.state.infos.append(text)
        return QMessageBox.Ok

    def exec(self, *args):
        RecordingBox.state.seen_texts = [button.text() for button in self.buttons()]
        self._picked = next((b for b in self.buttons() if b.text() == RecordingBox.state.click_text), None)
        return 0

    def clickedButton(self):
        return getattr(self, "_picked", None)


@pytest.fixture
def boxes(monkeypatch):
    state = SimpleNamespace(answer=QMessageBox.No, click_text=None, seen_texts=[], questions=[], infos=[])
    RecordingBox.state = state
    monkeypatch.setattr(discover_module, "QMessageBox", RecordingBox)
    return state


@pytest.fixture
def controller():
    fake = FakeController()
    yield fake
    fake.close()


@pytest.fixture
def host(controller):
    return FakeHost(controller)


@pytest.fixture
def tab(qapp, host):
    widget = DiscoverTab(host)
    yield widget
    widget.deleteLater()


NOTES = make_repo("acme/notes", 48000, "Portable markdown notes app for Windows")
VIEWER = make_repo("acme/viewer", 900, "Lightweight image viewer, portable .exe")
TINY = make_repo("acme/tiny", 12, "Tiny tool")
MAC = make_repo("acme/mac-only", 4800, "A macOS app written in SwiftUI")


def search(tab: DiscoverTab, query: str) -> None:
    # Enter, not the button: the Search button is locked while a search runs,
    # but Enter (and the sort combo) can start another one.
    tab.search_input.setText(query)
    tab.search_input.returnPressed.emit()


def names(tab: DiscoverTab) -> list[str]:
    table = tab.results_table
    return [table.item(row, COL_NAME).text() for row in range(table.rowCount())]


def select(tab: DiscoverTab, name: str) -> None:
    table = tab.results_table
    for row in range(table.rowCount()):
        if table.item(row, COL_NAME).text() == name:
            table.selectRow(row)
            return
    raise AssertionError(f"{name} is not in the table")


def showing_table(tab: DiscoverTab) -> bool:
    return tab._stack.currentIndex() == 1


def test_empty_state_before_first_search(tab):
    assert not showing_table(tab)
    assert tab._empty_label.text() == _EMPTY_TEXT


def test_search_populates_rows(tab, controller):
    controller.results_by_query["markdown"] = [NOTES, VIEWER, TINY]
    tab.search_input.setText("markdown")
    tab.search_input.returnPressed.emit()  # Enter searches too

    assert controller.search_calls == [("markdown", "stars")]
    assert showing_table(tab)
    assert names(tab) == ["notes", "viewer", "tiny"]  # sorted by stars, highest first


def test_stale_search_result_is_ignored(tab, controller, host):
    controller.results_by_query = {"old": [VIEWER, TINY], "new": [NOTES]}
    host.hold = True
    search(tab, "old")
    search(tab, "new")
    assert len(host.held) == 2

    host.run(host.held.pop(1))   # the newer search answers first
    assert names(tab) == ["notes"]
    host.run(host.held.pop(0))   # the older search answers late: must be ignored
    assert names(tab) == ["notes"]
    assert [r.full_name for r in tab.search_results] == ["acme/notes"]


def test_stale_release_enrichment_is_ignored(tab, controller, host):
    controller.results_by_query = {"old": [VIEWER], "new": [NOTES]}
    controller.enrichment = {"acme/viewer": {"latest_release_at": iso_days_ago(1), "has_windows_release": True}}
    host.hold = True
    search(tab, "old")
    host.run(host.held.pop(0))           # old search answers; its release check is held
    search(tab, "new")
    assert len(host.held) == 2           # [old release check, new search]
    host.run(host.held.pop(1))           # new search answers; its release check is held
    host.run(host.held.pop(0))           # old release check arrives last: must be ignored
    assert [r.full_name for r in tab.search_results] == ["acme/notes"]
    assert names(tab) == ["notes"]


def test_windows_only_hides_mac_before_and_after_release_check(tab, controller, host):
    linux_plain = make_repo("acme/plain", 300, "A small utility")
    controller.results_by_query["x"] = [NOTES, MAC, linux_plain]
    controller.enrichment = {
        "acme/plain": {"latest_release_at": iso_days_ago(5), "has_windows_release": False},
        "acme/mac-only": {"latest_release_at": iso_days_ago(5), "has_windows_release": False},
    }
    host.hold = True
    search(tab, "x")
    host.run(host.held.pop(0))  # the search answers; the release check is now held

    # Before the release check the heuristic decides: the plain utility is kept.
    assert names(tab) == ["notes", "plain"]

    host.run(host.held.pop(0))  # the release check answers: no Windows download for "plain"
    assert names(tab) == ["notes"]


def test_windows_only_filter_can_be_turned_off(tab, controller):
    controller.results_by_query["x"] = [NOTES, MAC]
    search(tab, "x")
    assert names(tab) == ["notes"]
    tab.windows_only_check.setChecked(False)
    assert names(tab) == ["notes", "mac-only"]


def test_inactive_filter_hides_old_projects(tab, controller):
    fresh = make_repo("acme/fresh", 100, pushed_days=10)
    stale = make_repo("acme/stale", 200, pushed_days=400)
    controller.results_by_query["x"] = [fresh, stale]
    search(tab, "x")
    assert sorted(names(tab)) == ["fresh", "stale"]

    assert not tab.inactive_days_spin.isEnabled()
    tab.hide_inactive_check.setChecked(True)
    assert tab.inactive_days_spin.isEnabled()
    assert names(tab) == ["fresh"]

    tab.inactive_days_spin.setValue(500)
    assert sorted(names(tab)) == ["fresh", "stale"]


def test_all_hidden_shows_message(tab, controller):
    controller.results_by_query["x"] = [MAC, make_repo("acme/mac-two", 5, "macOS helper")]
    search(tab, "x")
    assert not showing_table(tab)
    assert "All 2 results are hidden by your filters." in tab._empty_label.text()

    tab.windows_only_check.setChecked(False)
    assert showing_table(tab)
    assert tab.results_table.rowCount() == 2


def test_stars_sort_numerically(tab, controller):
    controller.results_by_query["x"] = [make_repo("acme/a", 900), make_repo("acme/b", 12),
                                        make_repo("acme/c", 48000)]
    search(tab, "x")
    assert names(tab) == ["c", "a", "b"]  # default: most stars first

    tab.results_table.horizontalHeader().setSortIndicator(COL_STARS, Qt.SortOrder.AscendingOrder)
    assert names(tab) == ["b", "a", "c"]  # 12 < 900 < 48000, not alphabetical


def test_stars_formatting():
    assert format_stars(12) == "12"
    assert format_stars(1204) == "1,204"
    assert format_stars(48200) == "48.2k"
    assert format_stars(48000) == "48k"


def test_installed_marker(tab, controller):
    controller.installed["acme/notes"] = make_program("Notes", repo="acme/notes")
    controller.results_by_query["x"] = [NOTES, VIEWER]
    search(tab, "x")
    marks = {
        tab.results_table.item(row, COL_NAME).text(): tab.results_table.item(row, COL_INSTALLED).text()
        for row in range(tab.results_table.rowCount())
    }
    assert marks == {"notes": "✓", "viewer": ""}


def test_install_buttons_need_a_selection(tab, controller, host):
    controller.results_by_query["x"] = [NOTES]
    search(tab, "x")
    assert not tab.install_button.isEnabled()
    assert not tab.open_button.isEnabled()

    select(tab, "notes")
    assert tab.install_button.isEnabled()

    tab.results_table.clearSelection()
    search(tab, "x")  # the host re-enables locked buttons when a task ends
    assert not tab.install_button.isEnabled()


def test_reinstall_prompt_no_skips_install(tab, controller, boxes):
    controller.results_by_query["x"] = [NOTES]
    controller.installed["acme/notes"] = make_program("Notes", repo="acme/notes", version="2.0.1")
    search(tab, "x")
    select(tab, "notes")

    boxes.answer = QMessageBox.No
    tab.install_button.click()
    assert controller.install_repo_calls == []
    assert len(boxes.questions) == 1
    assert "2.0.1" in boxes.questions[0]
    assert "Your settings for it are kept" in boxes.questions[0]


def test_reinstall_prompt_yes_installs(tab, controller, boxes):
    controller.results_by_query["x"] = [NOTES]
    controller.installed["acme/notes"] = make_program("Notes", repo="acme/notes")
    search(tab, "x")
    select(tab, "notes")

    boxes.answer = QMessageBox.Yes
    tab.install_button.click()
    assert controller.install_repo_calls == [("acme/notes", "latest_release")]


def test_no_reinstall_prompt_for_new_program(tab, controller, boxes):
    controller.results_by_query["x"] = [NOTES]
    search(tab, "x")
    select(tab, "notes")
    tab.install_button.click()
    assert boxes.questions == []
    assert controller.install_repo_calls == [("acme/notes", "latest_release")]


def test_repo_link_installs_from_repo(tab, controller):
    tab.link_input.setText("https://github.com/acme/notes")
    tab.link_button.click()
    assert controller.install_repo_calls == [("acme/notes", "latest_release")]
    assert controller.install_url_calls == []


def test_release_download_link_installs_directly(tab, controller):
    url = "https://github.com/acme/viewer/releases/download/v1.2.0/viewer-1.2.0-win64.zip"
    tab.link_input.setText(url)
    tab.link_button.click()
    assert controller.install_url_calls == [url]
    assert controller.install_repo_calls == []


def test_direct_download_link_installs_directly(tab, controller):
    url = "https://example.com/tools/tiny.zip"
    tab.link_input.setText(url)
    tab.link_button.click()
    assert controller.install_url_calls == [url]


def test_installed_program_from_link_prompts_for_reinstall(tab, controller, boxes):
    url = "https://example.com/tools/tiny.zip"
    controller.installed[url] = make_program("Tiny", launch_path=r"C:\Portable\Tiny\tiny.exe")
    tab.link_input.setText(url)
    boxes.answer = QMessageBox.No
    tab.link_button.click()
    assert len(boxes.questions) == 1
    assert controller.install_url_calls == []


def test_installer_detected_dialog_closes(tab, controller, boxes):
    controller.install_outcome = InstallOutcome(
        program=make_program("Setup", repo="acme/setup-tool"), is_likely_installer=True
    )
    tab.link_input.setText("https://github.com/acme/setup-tool")
    boxes.click_text = "Close"
    tab.link_button.click()

    assert "Run installer now" in boxes.seen_texts
    assert "Open folder" in boxes.seen_texts
    assert "Close" in boxes.seen_texts
    assert controller.run_calls == []


def test_installer_dialog_can_run_the_installer(tab, controller, boxes):
    controller.install_outcome = InstallOutcome(
        program=make_program("Setup", repo="acme/setup-tool"), is_likely_installer=True
    )
    tab.link_input.setText("https://github.com/acme/setup-tool")
    boxes.click_text = "Run installer now"
    tab.link_button.click()
    assert controller.run_calls == ["id-Setup"]


def test_install_without_executable_asks_user_to_use_edit(tab, controller, boxes):
    controller.install_outcome = InstallOutcome(
        program=make_program("Docs", repo="acme/docs", launch_path=None)
    )
    tab.link_input.setText("https://github.com/acme/docs")
    tab.link_button.click()
    assert len(boxes.infos) == 1
    assert "Edit" in boxes.infos[0]


def test_install_completion_refreshes_and_emits_signal(tab, controller, host):
    emitted = []
    tab.program_installed.connect(emitted.append)
    tab.link_input.setText("https://github.com/acme/notes")
    tab.link_button.click()
    assert len(emitted) == 1
    assert isinstance(emitted[0], ManagedProgram)
    assert emitted[0].name == "notes"
    assert host.refresh_count == 1
    assert controller.install_repo_calls == [("acme/notes", "latest_release")]


def test_double_click_row_installs(tab, controller, host):
    controller.results_by_query["x"] = [NOTES]
    search(tab, "x")
    select(tab, "notes")
    index = tab.results_table.model().index(0, COL_NAME)
    tab.results_table.activated.emit(index)
    assert controller.install_repo_calls == [("acme/notes", "latest_release")]


def test_row_activation_ignored_while_install_runs(tab, controller, host):
    controller.results_by_query["x"] = [NOTES]
    search(tab, "x")
    select(tab, "notes")
    host.hold = True
    tab.install_button.click()           # held: install button is now disabled
    assert not tab.install_button.isEnabled()
    tab.results_table.activated.emit(tab.results_table.model().index(0, COL_NAME))
    assert len(host.held) == 1           # no second install queued
    host.hold = False
    host.run(host.held.pop(0))


def test_sort_change_searches_again_when_there_is_a_query(tab, controller):
    controller.results_by_query["x"] = [NOTES]
    search(tab, "x")
    tab.sort_combo.setCurrentIndex(1)
    assert controller.search_calls[-1] == ("x", "updated")


def test_activity_log_collapsed_by_default_and_toggles(tab, controller):
    assert tab.activity_log.isHidden()
    assert not tab.log_toggle.isChecked()

    tab.log_toggle.click()
    assert not tab.activity_log.isHidden()

    tab.log_toggle.click()
    assert tab.activity_log.isHidden()


def test_activity_log_lines_have_time(tab, controller):
    controller.results_by_query["x"] = [NOTES]
    search(tab, "x")
    lines = tab.activity_log.toPlainText().splitlines()
    assert lines
    assert re.match(r"^\d\d:\d\d  Search for 'x' returned 1 repositories", lines[0])


def test_context_menu_copies_repository_url(tab, controller):
    controller.results_by_query["x"] = [NOTES]
    search(tab, "x")
    select(tab, "notes")
    tab._copy_repo_url(NOTES)
    assert QApplication.clipboard().text() == "https://github.com/acme/notes"


# ---------------------------------------------------------------------------
# Fork filters
# ---------------------------------------------------------------------------

def _fork(full_name: str, stars: int, ahead: int | None, behind: int = 0) -> GitHubRepo:
    repo = make_repo(full_name, stars)
    repo.is_fork = True
    repo.parent_full_name = "irusanov/SMUDebugTool" if ahead is not None else ""
    repo.ahead_by = ahead
    repo.behind_by = behind if ahead is not None else None
    return repo


def test_forks_with_changes_filter_hides_unchanged_forks():
    parent = make_repo("irusanov/SMUDebugTool", 485)
    mine = _fork("AlexStapleton/SMUDebugTool", 2, ahead=124, behind=4)
    stale = _fork("Coldblackice/SMUDebugTool", 0, ahead=0, behind=13)
    pending = _fork("someone/SMUDebugTool", 0, ahead=None)
    repos = [parent, mine, stale, pending]
    shown = apply_filters(repos, windows_only=False, inactive_days=None, forks_with_changes_only=True)
    assert [r.full_name for r in shown] == ["irusanov/SMUDebugTool", "AlexStapleton/SMUDebugTool", "someone/SMUDebugTool"]
    everything = apply_filters(repos, windows_only=False, inactive_days=None, forks_with_changes_only=False)
    assert len(everything) == 4


def test_fork_label_shows_ahead_and_behind():
    from portable_manager.ui.discover_tab import fork_label

    text, tip = fork_label(_fork("AlexStapleton/SMUDebugTool", 2, ahead=124, behind=4))
    assert text == "AlexStapleton/SMUDebugTool · fork +124 / −4"
    assert "Fork of irusanov/SMUDebugTool" in tip
    assert fork_label(make_repo("irusanov/SMUDebugTool", 485))[0] == "irusanov/SMUDebugTool"


def test_include_forks_checkbox_searches_again_with_forks(tab, controller):
    controller.results_by_query["SMUDebugTool"] = [make_repo("irusanov/SMUDebugTool", 485)]
    tab.search_input.setText("SMUDebugTool")
    assert not tab.forks_with_changes_check.isEnabled()
    tab.include_forks_check.setChecked(True)
    assert controller.include_forks_calls[-1] is True
    assert tab.forks_with_changes_check.isEnabled()
    tab.include_forks_check.setChecked(False)
    assert controller.include_forks_calls[-1] is False
    assert not tab.forks_with_changes_check.isEnabled()
