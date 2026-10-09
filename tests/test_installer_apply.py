"""Install/update behaviour of PortableInstaller without any network access."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from portable_manager.errors import InstallError
from portable_manager.github_client import GitHubRelease, GitHubReleaseAsset
from portable_manager.installer import PortableInstaller, normalize_dir
from portable_manager.models import AppSettings, ManagedProgram


@pytest.fixture
def installer(tmp_path):
    settings = AppSettings(install_root=str(tmp_path / "apps"), download_cache=str(tmp_path / "cache"))
    return PortableInstaller(settings, github=None)  # type: ignore[arg-type]


def _zip(tmp_path: Path, version: str, extra: dict[str, str] | None = None) -> Path:
    path = tmp_path / f"app-{version}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"App-{version}/App.exe", version)
        zf.writestr(f"App-{version}/data/defaults.ini", f"defaults {version}")
        for name, text in (extra or {}).items():
            zf.writestr(f"App-{version}/{name}", text)
    return path


def test_update_of_versioned_zip_points_at_new_exe(installer, tmp_path):
    """Regression for B1: the second extraction used to land in App-2.0/ beside the old files."""
    install_dir = Path(installer.settings.install_root) / "App"
    first = installer._materialize_asset(_zip(tmp_path, "1.0"), install_dir)
    assert first == install_dir / "App.exe"
    (install_dir / "data" / "user.ini").write_text("mine")

    second = installer._materialize_asset(_zip(tmp_path, "2.0"), install_dir)

    assert second == install_dir / "App.exe"
    assert second.read_text() == "2.0"
    assert sorted(p.name for p in install_dir.iterdir()) == ["App.exe", "data"]
    # B2: user files in a folder the archive also ships must survive.
    assert (install_dir / "data" / "user.ini").read_text() == "mine"
    assert (install_dir / "data" / "defaults.ini").read_text() == "defaults 2.0"
    leftovers = [p.name for p in install_dir.parent.iterdir() if p.name.startswith(".ppm_")]
    assert leftovers == []


def test_bad_zip_leaves_existing_install_untouched(installer, tmp_path):
    install_dir = Path(installer.settings.install_root) / "App"
    installer._materialize_asset(_zip(tmp_path, "1.0"), install_dir)
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(InstallError):
        installer._materialize_asset(bad, install_dir)
    assert (install_dir / "App.exe").read_text() == "1.0"
    assert not [p for p in install_dir.parent.iterdir() if p.name.startswith(".ppm_")]


def test_zip_slip_is_rejected(installer, tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../outside.txt", "x")
    install_dir = Path(installer.settings.install_root) / "Evil"
    try:
        installer._materialize_asset(evil, install_dir)
    except InstallError:
        pass
    assert not (tmp_path / "outside.txt").exists()
    assert not (Path(installer.settings.install_root) / "outside.txt").exists()


def test_single_file_asset(installer, tmp_path):
    exe = tmp_path / "tool-1.2.exe"
    exe.write_text("bin")
    install_dir = Path(installer.settings.install_root) / "Tool"
    launch = installer._materialize_asset(exe, install_dir)
    assert launch == install_dir / "tool-1.2.exe" and launch.read_text() == "bin"


def test_choose_install_dir_avoids_other_programs(installer):
    root = Path(installer.settings.install_root)
    taken = {normalize_dir(root / "Tool"): "url::tool::aaa", normalize_dir(root / "Tool (2)"): "url::tool::bbb"}
    assert installer._choose_install_dir("Tool", "url::tool::ccc", taken) == root / "Tool (3)"
    # Reinstalling the same program keeps its folder.
    assert installer._choose_install_dir("Tool", "url::tool::aaa", taken) == root / "Tool"


def test_download_and_install_release_removes_superseded_exe(installer, tmp_path, monkeypatch):
    install_dir = Path(installer.settings.install_root) / "Tool"
    install_dir.mkdir(parents=True)
    (install_dir / "tool-1.2.exe").write_text("old")
    (install_dir / "settings.ini").write_text("keep")
    program = ManagedProgram(
        program_id="p", name="Tool", source_type="github_repo", source_value="o/tool",
        install_dir=str(install_dir), launch_path=str(install_dir / "tool-1.2.exe"),
        installed_asset_name="tool-1.2.exe", installed_hash="sha256:old", version="v1.2",
    )
    new_exe = tmp_path / "dl" / "tool-1.3.exe"
    new_exe.parent.mkdir()
    new_exe.write_text("new")
    monkeypatch.setattr(installer, "_download", lambda *a, **k: (new_exe, "newhash"))
    release = GitHubRelease(1, "v1.3", "v1.3", "", "", False, [])
    asset = GitHubReleaseAsset("tool-1.3.exe", "https://x/tool-1.3.exe", 3, "")

    updated = installer.download_and_install_release(program, release, asset)

    assert Path(updated.launch_path) == install_dir / "tool-1.3.exe"
    assert updated.version == "v1.3"
    assert sorted(p.name for p in install_dir.iterdir()) == ["settings.ini", "tool-1.3.exe"]


def test_ensure_install_root_writable_is_checked(installer, tmp_path, monkeypatch):
    from portable_manager import fsops

    def deny(path):
        raise InstallError("administrator rights")

    monkeypatch.setattr(fsops, "ensure_writable_dir", deny)

    class _Repo:
        name = "App"
        full_name = "o/App"

    class _GH:
        def get_repo(self, _):
            return _Repo()

        def get_latest_release(self, *_a, **_k):
            return GitHubRelease(1, "v1", "v1", "", "", False, [GitHubReleaseAsset("app-win64.zip", "https://x", 1, "")])

    installer.github = _GH()
    monkeypatch.setattr(installer, "_download", lambda *a, **k: pytest.fail("must not download"))
    with pytest.raises(InstallError, match="administrator"):
        installer.install_from_repo("o/App")


def test_digest_from_github_client_is_enforced():
    """Contract between GitHubClient (stores bare hex) and the installer's integrity check."""
    from portable_manager.github_client import GitHubClient
    from portable_manager.installer import _expected_sha256

    hexdigest = "ab" * 32
    release = GitHubClient._release_from_api({
        "id": 1, "tag_name": "v1", "assets": [
            {"name": "a.zip", "browser_download_url": "https://x/a.zip", "size": 1, "digest": f"sha256:{hexdigest.upper()}"},
        ],
    })
    assert _expected_sha256(release.assets[0]) == hexdigest


def _program_for(install_dir: Path, **kw) -> ManagedProgram:
    return ManagedProgram(
        program_id="p", name="Tool", source_type="github_repo", source_value="o/tool",
        install_dir=str(install_dir), launch_path=str(install_dir / "Tool.exe"), **kw,
    )


def test_prefetched_update_is_used_and_kept_for_caller(installer, tmp_path, monkeypatch):
    install_dir = Path(installer.settings.install_root) / "Tool"
    install_dir.mkdir(parents=True)
    (install_dir / "Tool.exe").write_text("old")
    pending = tmp_path / "pending" / "Tool.exe"
    pending.parent.mkdir()
    pending.write_text("new")
    monkeypatch.setattr(installer, "_download", lambda *a, **k: pytest.fail("must not download"))
    release = GitHubRelease(1, "v2", "v2", "", "", False, [])
    asset = GitHubReleaseAsset("Tool.exe", "https://x/Tool.exe", 3, "")

    updated = installer.download_and_install_release(
        _program_for(install_dir, installed_asset_name="Tool.exe", installed_hash="sha256:old"),
        release, asset, prefetched=(pending, "newhash"),
    )
    assert (install_dir / "Tool.exe").read_text() == "new"
    assert updated.version == "v2" and updated.installed_hash == "sha256:newhash"


def test_identical_update_raises_already_up_to_date(installer, tmp_path, monkeypatch):
    from portable_manager.errors import AlreadyUpToDateError

    install_dir = Path(installer.settings.install_root) / "Tool"
    install_dir.mkdir(parents=True)
    dl = tmp_path / "dl_x" / "Tool.exe"
    dl.parent.mkdir()
    dl.write_text("same")
    monkeypatch.setattr(installer, "_download", lambda *a, **k: (dl, "samehash"))
    with pytest.raises(AlreadyUpToDateError):
        installer.download_and_install_release(
            _program_for(install_dir, installed_hash="sha256:samehash"),
            GitHubRelease(1, "v2", "v2", "", "", False, []),
            GitHubReleaseAsset("Tool.exe", "https://x/Tool.exe", 4, ""),
        )
