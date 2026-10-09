"""AppController behaviour with a fake GitHub client and a fake installer (no network)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from portable_manager import controller as controller_mod
from portable_manager import fsops
from portable_manager.controller import AppController
from portable_manager.errors import AlreadyUpToDateError, InstallError, ProgramInUseError
from portable_manager.github_client import GitHubRelease, GitHubReleaseAsset, GitHubRepo
from portable_manager.installer import PortableInstaller
from portable_manager.models import AppSettings, ManagedProgram, UpdatePolicy


def _release(tag: str, *names: str) -> GitHubRelease:
    assets = [GitHubReleaseAsset(n, f"https://example.test/{tag}/{n}", 10, "") for n in names]
    return GitHubRelease(1, tag, tag, "", "2026-10-01T00:00:00Z", False, assets)


class FakeGitHub:
    def __init__(self) -> None:
        self.releases: dict[str, GitHubRelease | None] = {}
        self.rate_limit_remaining: int | None = None
        self.calls = 0
        self.info_calls: list[str] = []

    def get_latest_release(self, full_name, include_prereleases=False):
        self.calls += 1
        return self.releases.get(full_name)

    def get_latest_release_info(self, full_name):
        self.info_calls.append(full_name)
        return "2026-10-01T00:00:00Z", True

    def close(self):
        pass


class FakeInstaller(PortableInstaller):
    def __init__(self, settings):
        super().__init__(settings, github=None)  # type: ignore[arg-type]
        self.installed: list[tuple[str, object]] = []
        self.identical = False

    def download_and_install_release(self, program, release, asset, progress_callback=None, prefetched=None):
        if self.identical:
            raise AlreadyUpToDateError("same bytes")
        self.installed.append((release.tag_name, prefetched))
        program.version = release.tag_name
        program.asset_name = program.installed_asset_name = asset.name
        program.latest_upstream_version = release.tag_name
        program.update_available = False
        program.update_available_asset_name = None
        return program

    def _download(self, url, file_name, progress_callback=None, expected_sha256=""):
        folder = Path(self.settings.download_cache) / "dl_fake"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / file_name
        path.write_text("payload")
        return path, "abc123"


@pytest.fixture
def ctl(tmp_storage, tmp_path, monkeypatch):
    tmp_storage.save_settings(AppSettings(install_root=str(tmp_path / "apps"), download_cache=str(tmp_path / "cache")))
    c = AppController(tmp_storage)
    c.github = FakeGitHub()
    c.installer = FakeInstaller(c.settings)
    monkeypatch.setattr(fsops, "find_running_processes", lambda folder: [])
    return c


def _add(c: AppController, tmp_path: Path, name="Tool", version="v1.0", mode="manual", **kw) -> ManagedProgram:
    install = tmp_path / "apps" / name
    install.mkdir(parents=True, exist_ok=True)
    (install / f"{name}.exe").write_text("x")
    program = ManagedProgram(
        program_id=f"repo::o/{name.lower()}", name=name, source_type="github_repo", source_value=f"o/{name}",
        install_dir=str(install), launch_path=str(install / f"{name}.exe"), version=version,
        repo_full_name=f"o/{name}", asset_name=f"{name}-win64.zip", installed_asset_name=f"{name}-win64.zip",
        update_policy=UpdatePolicy(update_mode=mode), **kw,
    )
    c._upsert_program(program)
    return program


def test_check_reports_new_update_once(ctl, tmp_path):
    _add(ctl, tmp_path)
    ctl.github.releases["o/Tool"] = _release("v1.1", "Tool-win64.zip")
    first = ctl.check_for_updates_all()
    assert [p.name for p in first.newly_available] == ["Tool"]
    assert first.available_count == 1
    second = ctl.check_for_updates_all()
    assert second.newly_available == [] and second.available_count == 1


def test_release_without_windows_build_is_not_an_update(ctl, tmp_path):
    _add(ctl, tmp_path)
    ctl.github.releases["o/Tool"] = _release("v2.0", "Tool-macos.dmg")
    program = ctl.check_for_updates("repo::o/tool")
    assert program.update_available is False
    assert program.last_update_status == "no_compatible_asset"


def test_check_failure_is_recorded_not_raised(ctl, tmp_path):
    _add(ctl, tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    ctl.github.get_latest_release = boom
    report = ctl.check_for_updates_all()
    assert report.checked[0].last_update_status == "check_failed"
    assert report.errors and "network down" in report.errors[0]


def test_bulk_check_saves_once(ctl, tmp_path, monkeypatch):
    for n in ("A", "B", "C"):
        _add(ctl, tmp_path, name=n)
        ctl.github.releases[f"o/{n}"] = _release("v1.0", f"{n}-win64.zip")
    saves = []
    original = ctl.storage.save_programs
    monkeypatch.setattr(ctl.storage, "save_programs", lambda progs: (saves.append(1), original(progs)))
    ctl.check_for_updates_all()
    assert len(saves) == 1


def test_install_update_fetches_release_once(ctl, tmp_path):
    _add(ctl, tmp_path)
    ctl.github.releases["o/Tool"] = _release("v1.1", "Tool-win64.zip")
    updated = ctl.install_available_update("repo::o/tool")
    assert updated.version == "v1.1" and updated.last_update_status == "updated"
    assert ctl.github.calls == 1


def test_install_update_refuses_running_program(ctl, tmp_path, monkeypatch):
    _add(ctl, tmp_path)
    ctl.github.releases["o/Tool"] = _release("v1.1", "Tool-win64.zip")
    monkeypatch.setattr(fsops, "find_running_processes", lambda folder: ["Tool.exe (42)"])
    with pytest.raises(ProgramInUseError, match="Close Tool"):
        ctl.install_available_update("repo::o/tool")
    assert ctl.installer.installed == []


def test_identical_download_marks_program_current(ctl, tmp_path):
    _add(ctl, tmp_path)
    ctl.github.releases["o/Tool"] = _release("v1.0.1", "Tool-win64.zip")
    ctl.installer.identical = True
    program = ctl.install_available_update("repo::o/tool")
    assert program.version == "v1.0.1"
    assert program.update_available is False and program.last_update_status == "no_update"


def test_download_only_mode_stages_then_install_reuses_it(ctl, tmp_path):
    _add(ctl, tmp_path, mode="download_only")
    ctl.github.releases["o/Tool"] = _release("v1.1", "Tool-win64.zip")
    report = ctl.check_for_updates_all()
    assert [p.name for p in report.downloaded] == ["Tool"]
    staged = ctl.get_program("repo::o/tool")
    assert staged.pending_update_version == "v1.1" and Path(staged.pending_update_file).is_file()
    assert not (Path(ctl.settings.download_cache) / "dl_fake").exists()

    ctl.install_available_update("repo::o/tool")
    tag, prefetched = ctl.installer.installed[-1]
    assert tag == "v1.1" and prefetched is not None and prefetched[0] == Path(staged.pending_update_file)
    done = ctl.get_program("repo::o/tool")
    assert done.pending_update_file is None and not Path(staged.pending_update_file).exists()


def test_auto_install_mode_installs_or_defers(ctl, tmp_path, monkeypatch):
    _add(ctl, tmp_path, mode="install_automatically")
    ctl.github.releases["o/Tool"] = _release("v1.1", "Tool-win64.zip")
    monkeypatch.setattr(fsops, "find_running_processes", lambda folder: ["Tool.exe (1)"])
    deferred = ctl.check_for_updates_all()
    assert deferred.installed == [] and "running" in deferred.deferred[0]

    monkeypatch.setattr(fsops, "find_running_processes", lambda folder: [])
    report = ctl.check_for_updates_all()
    assert [p.version for p in report.installed] == ["v1.1"]
    assert ctl.get_program("repo::o/tool").version == "v1.1"


def test_reinstall_keeps_user_customisations(ctl, tmp_path):
    original = _add(ctl, tmp_path)
    ctl.edit_program(
        original.program_id, name="My Tool", notes="mine", launch_path=original.launch_path,
        launch_args="--fast", working_directory_override=None,
        update_policy=UpdatePolicy(update_mode="install_automatically"), run_as_admin=True,
    )
    fresh = copy.deepcopy(original)
    fresh.name, fresh.notes, fresh.launch_args, fresh.version = "Tool", "repo description", "", "v2.0"
    ctl._upsert_program(fresh)
    kept = ctl.get_program(original.program_id)
    assert (kept.name, kept.notes, kept.launch_args, kept.run_as_admin) == ("My Tool", "mine", "--fast", True)
    assert kept.update_policy.update_mode == "install_automatically"
    assert kept.version == "v2.0"


def test_find_managed_program(ctl, tmp_path):
    _add(ctl, tmp_path)
    assert ctl.find_managed_program(repo_full_name="O/TOOL").name == "Tool"
    assert ctl.find_managed_program(repo_full_name="o/other") is None


def test_fetch_release_dates_respects_rate_limit_budget(ctl):
    repos = [GitHubRepo(f"o/r{i}", f"r{i}", "o", "", "", "", 0, "", "main") for i in range(10)]
    ctl.github.rate_limit_remaining = 9  # keep 5 in reserve -> only 4 lookups
    enriched = ctl.fetch_release_dates(repos)
    assert len(ctl.github.info_calls) == 4
    assert sum(1 for r in enriched if r.has_windows_release) == 4
    assert all(r.has_windows_release is None for r in repos)  # originals untouched


def test_remove_with_delete(ctl, tmp_path):
    program = _add(ctl, tmp_path)
    outcome = ctl.remove_program(program.program_id, delete_files=True)
    assert outcome.deleted_files and not outcome.warnings
    assert not Path(program.install_dir).exists()
    assert ctl.list_programs() == []


def test_remove_refuses_running_program(ctl, tmp_path, monkeypatch):
    program = _add(ctl, tmp_path)
    monkeypatch.setattr(fsops, "find_running_processes", lambda folder: ["Tool.exe (7)"])
    with pytest.raises(ProgramInUseError):
        ctl.remove_program(program.program_id, delete_files=True)
    assert Path(program.install_dir).exists() and len(ctl.list_programs()) == 1


def test_remove_refuses_shared_folder(ctl, tmp_path):
    a = _add(ctl, tmp_path, name="Tool")
    b = copy.deepcopy(a)
    b.program_id, b.name = "url::other", "Other"
    ctl._upsert_program(b)
    with pytest.raises(InstallError, match="another managed program"):
        ctl.remove_program(a.program_id, delete_files=True)
    assert Path(a.install_dir).exists() and len(ctl.list_programs()) == 2


def test_list_programs_returns_copies(ctl, tmp_path):
    _add(ctl, tmp_path)
    ctl.list_programs()[0].name = "mutated"
    assert ctl.get_program("repo::o/tool").name == "Tool"
