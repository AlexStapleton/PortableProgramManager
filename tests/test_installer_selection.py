"""Asset choice, launch-file choice, installer detection and download integrity.

No network access: downloads are served by a fake ``requests.Session``.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
import requests

from portable_manager.errors import InstallError
from portable_manager.github_client import GitHubRelease, GitHubReleaseAsset
from portable_manager.installer import PortableInstaller, _asset_arch, _expected_sha256
from portable_manager.models import AppSettings, ManagedProgram


def _asset(name: str, size: int = 100, digest: str = "") -> GitHubReleaseAsset:
    asset = GitHubReleaseAsset(name, f"https://example.com/{name}", size, "application/octet-stream")
    if digest:
        setattr(asset, "digest", digest)  # added by GitHub client support; may not exist yet
    return asset


def _pick(names_and_sizes, machine: str, hint: str | None = None):
    assets = [_asset(name, size) for name, size in names_and_sizes]
    chosen = PortableInstaller.pick_portable_asset(assets, asset_name_hint=hint, machine=machine)
    return chosen.name if chosen else None


def _touch(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def installer(tmp_path):
    settings = AppSettings(install_root=str(tmp_path / "apps"), download_cache=str(tmp_path / "cache"))
    return PortableInstaller(settings, github=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# pick_portable_asset
# ---------------------------------------------------------------------------

def test_release_with_only_macos_and_linux_builds_picks_nothing():
    names = [
        ("tool-1.0-macos.zip", 200_000_000),
        ("tool-1.0-darwin-arm64.zip", 1),
        ("Tool-1.0.dmg", 1),
        ("tool_1.0_amd64.deb", 1),
        ("tool-1.0-linux.zip", 1),
        ("tool-1.0-ubuntu.zip", 1),
        ("tool-1.0-freebsd.zip", 1),
        ("Tool-1.0.AppImage", 1),
        ("tool-1.0-ios-universal.zip", 1),
        ("tool-1.0-android.apk", 1),
        ("tool-1.0-mac-x64.zip", 1),
        ("tool.app.zip", 1),
    ]
    assert _pick(names, machine="amd64") is None


def test_mixed_release_never_picks_the_bigger_non_windows_build():
    names = [("tool-1.0-macos.zip", 187_000_000), ("tool-1.0-linux.zip", 190_000_000), ("tool-1.0-win64.zip", 5_000_000)]
    assert _pick(names, machine="amd64") == "tool-1.0-win64.zip"


def test_windows_words_are_not_mistaken_for_other_systems():
    assert _pick([("tool-windows-x64.zip", 1)], machine="amd64") == "tool-windows-x64.zip"
    assert _pick([("tool-win.zip", 1)], machine="amd64") == "tool-win.zip"


def test_arm64_machine_prefers_arm64_and_x64_machine_drops_it():
    names = [("tool-1.0-win64.zip", 10), ("tool-1.0-arm64.zip", 10)]
    assert _pick(names, machine="arm64") == "tool-1.0-arm64.zip"
    assert _pick(names, machine="amd64") == "tool-1.0-win64.zip"
    assert _pick([("tool-1.0-arm64.zip", 10)], machine="amd64") is None


def test_arm64_machine_falls_back_to_x64_before_x86():
    names = [("tool-x86.zip", 10), ("tool-x64.zip", 10)]
    assert _pick(names, machine="arm64") == "tool-x64.zip"


def test_x64_machine_prefers_x64_then_unmarked_then_x86():
    assert _pick([("tool-x86.zip", 10), ("tool.zip", 10)], machine="amd64") == "tool.zip"
    assert _pick([("tool-x86.zip", 10)], machine="amd64") == "tool-x86.zip"
    assert _pick([("tool-x86.zip", 10), ("tool-x64.zip", 10)], machine="amd64") == "tool-x64.zip"


def test_x86_64_is_not_misread_as_x86():
    assert _asset_arch("tool-x86_64.zip") == "x64"
    assert _asset_arch("tool-x86-64.zip") == "x64"
    assert _asset_arch("tool-x86.zip") == "x86"
    assert _asset_arch("tool-i686.zip") == "x86"
    assert _asset_arch("tool-win32.zip") == "x86"
    assert _asset_arch("tool-aarch64.zip") == "arm64"
    assert _asset_arch("tool.zip") == ""
    # On a 32-bit machine the x86_64 build cannot run, so the x86 build is the one to pick.
    names = [("tool-x86_64.zip", 10), ("tool-x86.zip", 10)]
    assert _pick(names, machine="x86") == "tool-x86.zip"
    assert _pick(names, machine="amd64") == "tool-x86_64.zip"


def test_hint_with_old_version_picks_new_version_of_same_asset():
    names = [("tool-1.3-win64-debug.zip", 9_000), ("tool-1.3-win64.zip", 100)]
    assert _pick(names, machine="amd64", hint="tool-1.2-win64.zip") == "tool-1.3-win64.zip"


def test_exact_hint_name_still_wins():
    names = [("tool-1.3-win64.zip", 100), ("tool-1.2-win64.zip", 100)]
    assert _pick(names, machine="amd64", hint="tool-1.2-win64.zip") == "tool-1.2-win64.zip"


def test_installer_named_asset_loses_to_portable_one():
    names = [("tool-1.0-setup.exe", 9_000), ("tool-1.0-portable.zip", 100)]
    assert _pick(names, machine="amd64") == "tool-1.0-portable.zip"
    # ...but it is still a candidate when it is the only Windows asset.
    assert _pick([("tool-1.0-setup.exe", 9_000)], machine="amd64") == "tool-1.0-setup.exe"


def test_no_assets_or_no_portable_extensions_returns_none():
    assert PortableInstaller.pick_portable_asset([], machine="amd64") is None
    assert _pick([("tool-1.0-win64.msi", 1), ("tool-1.0-win64.tar.gz", 1)], machine="amd64") is None


# ---------------------------------------------------------------------------
# guess_launch_executable
# ---------------------------------------------------------------------------

def test_squirrel_layout_picks_root_program(tmp_path):
    root = tmp_path / "SCSKiller"
    _touch(root / "Update.exe", 2_000)
    _touch(root / "SCSKiller.exe", 500)
    _touch(root / "app-1.0" / "SCSKiller.exe", 500)
    _touch(root / "app-1.0" / "vc_redist.x64.exe", 9_000)
    expected = root / "SCSKiller.exe"
    assert PortableInstaller.guess_launch_executable(root, ["SCSKiller"]) == expected
    assert PortableInstaller.guess_launch_executable(root) == expected


def test_uninstaller_and_crash_helper_are_not_the_program(tmp_path):
    _touch(tmp_path / "unins000.exe", 9_000)
    _touch(tmp_path / "App.exe", 100)
    _touch(tmp_path / "crashpad_handler.exe", 9_999)
    assert PortableInstaller.guess_launch_executable(tmp_path) == tmp_path / "App.exe"


def test_hint_match_beats_size(tmp_path):
    _touch(tmp_path / "Other.exe", 9_000)
    _touch(tmp_path / "MyApp.exe", 100)
    assert PortableInstaller.guess_launch_executable(tmp_path, ["MyApp"]) == tmp_path / "MyApp.exe"


def test_partial_hint_match_beats_size(tmp_path):
    _touch(tmp_path / "Other.exe", 9_000)
    _touch(tmp_path / "MyAppLauncher.exe", 100)
    assert PortableInstaller.guess_launch_executable(tmp_path, ["my-app"]) == tmp_path / "MyAppLauncher.exe"


def test_shallower_wins_over_larger_without_hints(tmp_path):
    _touch(tmp_path / "small.exe", 10)
    _touch(tmp_path / "sub" / "big.exe", 9_999)
    assert PortableInstaller.guess_launch_executable(tmp_path) == tmp_path / "small.exe"


def test_larger_file_wins_at_the_same_depth(tmp_path):
    _touch(tmp_path / "a" / "small.exe", 10)
    _touch(tmp_path / "b" / "big.exe", 9_999)
    assert PortableInstaller.guess_launch_executable(tmp_path) == tmp_path / "b" / "big.exe"


def test_scripts_only_when_there_is_no_executable(tmp_path):
    _touch(tmp_path / "start.ps1", 1_000)
    assert PortableInstaller.guess_launch_executable(tmp_path) == tmp_path / "start.ps1"
    _touch(tmp_path / "Update.exe", 10)
    assert PortableInstaller.guess_launch_executable(tmp_path) == tmp_path / "Update.exe"


def test_empty_folder_has_no_launch_file(tmp_path):
    _touch(tmp_path / "readme.txt")
    assert PortableInstaller.guess_launch_executable(tmp_path) is None


# ---------------------------------------------------------------------------
# materialize: previous launch file and name hints
# ---------------------------------------------------------------------------

def _zip_with(path: Path, entries: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return path


def test_update_prefers_previous_launch_file_when_still_shipped(installer, tmp_path):
    install_dir = Path(installer.settings.install_root) / "Tool"
    # Two root entries so the archive isn't flattened into its only sub-folder.
    v1 = _zip_with(tmp_path / "v1.zip", {"bin/Launcher.exe": "v1", "readme.txt": "r"})
    first = installer._materialize_asset(v1, install_dir)
    assert first == install_dir / "bin" / "Launcher.exe"

    # v2 has a bigger, shallower exe; the previous launch file should still win.
    v2 = _zip_with(tmp_path / "v2.zip", {"Main.exe": "m" * 5_000, "bin/Launcher.exe": "v2"})
    second = installer._materialize_asset(v2, install_dir, previous_launch_relpath="bin/Launcher.exe")
    assert second == install_dir / "bin" / "Launcher.exe"
    assert second.read_text() == "v2"


def test_update_guesses_again_when_previous_launch_file_is_gone(installer, tmp_path):
    install_dir = Path(installer.settings.install_root) / "Tool"
    installer._materialize_asset(
        _zip_with(tmp_path / "v1.zip", {"bin/Launcher.exe": "v1", "readme.txt": "r"}), install_dir,
    )
    v2 = _zip_with(tmp_path / "v2.zip", {"Main.exe": "m" * 5_000})
    second = installer._materialize_asset(v2, install_dir, previous_launch_relpath="bin/Launcher.exe")
    assert second == install_dir / "Main.exe"


def test_previous_launch_path_cannot_point_outside_the_new_content(installer, tmp_path):
    install_dir = Path(installer.settings.install_root) / "Tool"
    archive = _zip_with(tmp_path / "v1.zip", {"Main.exe": "m"})
    launch = installer._materialize_asset(archive, install_dir, previous_launch_relpath="../../etc/evil.exe")
    assert launch == install_dir / "Main.exe"


def test_name_hints_are_used_for_archives(installer, tmp_path):
    install_dir = Path(installer.settings.install_root) / "Tool"
    archive = _zip_with(tmp_path / "v1.zip", {"Main.exe": "m" * 5_000, "bin/Launcher.exe": "l"})
    launch = installer._materialize_asset(archive, install_dir, name_hints=["Launcher"])
    assert launch == install_dir / "bin" / "Launcher.exe"


# ---------------------------------------------------------------------------
# looks_like_installer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Updater.exe", "uninstall.exe", "AutoUpdate.exe", "unins000.exe"])
def test_updaters_and_uninstallers_are_not_installers(tmp_path, name):
    launch = _touch(tmp_path / name)
    assert PortableInstaller.looks_like_installer(launch, tmp_path) is False


@pytest.mark.parametrize("name", ["MyAppSetup.exe", "setup.exe", "App-Installer.exe", "install.exe"])
def test_setup_and_installer_names_are_installers(tmp_path, name):
    launch = _touch(tmp_path / name)
    assert PortableInstaller.looks_like_installer(launch, tmp_path) is True


def test_single_exe_folder_uses_the_same_name_rules(tmp_path):
    _touch(tmp_path / "App_setup.exe")
    assert PortableInstaller.looks_like_installer(tmp_path / "launcher.bat", tmp_path) is True


def test_single_exe_named_updater_is_not_an_installer(tmp_path):
    _touch(tmp_path / "AutoUpdate.exe")
    assert PortableInstaller.looks_like_installer(tmp_path / "launcher.bat", tmp_path) is False


def test_no_launch_file_is_not_an_installer(tmp_path):
    assert PortableInstaller.looks_like_installer(None, tmp_path) is False


def test_expected_sha256_reads_github_digest():
    assert _expected_sha256(_asset("a.zip", digest="sha256:abc123")) == "abc123"
    assert _expected_sha256(_asset("a.zip")) == ""
    assert _expected_sha256(_asset("a.zip", digest="md5:abc123")) == ""


# ---------------------------------------------------------------------------
# download integrity
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: bytes, url: str) -> None:
        self._body = body
        self.url = url
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 1024):
        for start in range(0, len(self._body), 4):
            yield self._body[start:start + 4]


class _FakeSession:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers: dict[str, str] = {}
        self.max_redirects = 30

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, stream=False, timeout=None):
        return _FakeResponse(self._body, url)


@pytest.fixture
def served(monkeypatch):
    """Serve *body* from every download. Returns a setter for the body."""
    state = {"body": b""}
    monkeypatch.setattr(requests, "Session", lambda: _FakeSession(state["body"]))

    def set_body(body: bytes) -> None:
        state["body"] = body

    return set_body


def test_download_with_matching_digest_succeeds(installer, served):
    body = b"portable build bytes"
    served(body)
    digest = hashlib.sha256(body).hexdigest()

    path, actual = installer._download("https://example.com/tool.exe", "tool.exe", expected_sha256=digest.upper())

    assert actual == digest
    assert path.read_bytes() == body
    installer._discard_download(path)


def test_download_accepts_prefixed_digest(installer, served):
    body = b"portable build bytes"
    served(body)
    digest = hashlib.sha256(body).hexdigest()

    path, _ = installer._download("https://example.com/tool.exe", "tool.exe", expected_sha256=f"sha256:{digest}")
    installer._discard_download(path)


def test_download_without_expected_digest_is_not_checked(installer, served):
    served(b"anything")
    path, _ = installer._download("https://example.com/tool.exe", "tool.exe")
    assert path.read_bytes() == b"anything"
    installer._discard_download(path)


def test_digest_mismatch_raises_and_removes_download(installer, served):
    served(b"tampered bytes")
    cache = Path(installer.settings.download_cache)

    with pytest.raises(InstallError) as excinfo:
        installer._download("https://example.com/tool.exe", "tool.exe", expected_sha256="0" * 64)

    assert str(excinfo.value).startswith("Integrity check failed for tool.exe")
    assert "Nothing was installed." in str(excinfo.value)
    assert list(cache.glob("dl_*")) == []


def test_digest_mismatch_during_install_leaves_install_dir_alone(installer, served, monkeypatch, tmp_path):
    served(b"tampered bytes")
    install_dir = Path(installer.settings.install_root) / "Tool"
    _touch(install_dir / "Tool.exe", 3)

    class _Repo:
        name = "Tool"
        full_name = "o/Tool"
        homepage = None
        html_url = "https://github.com/o/Tool"
        description = ""

    class _GH:
        def get_repo(self, _):
            return _Repo()

        def get_latest_release(self, *_a, **_k):
            asset = _asset("Tool-win64.exe", digest="sha256:" + "0" * 64)
            return GitHubRelease(1, "v2", "v2", "", "", False, [asset])

    installer.github = _GH()
    with pytest.raises(InstallError, match="Integrity check failed"):
        installer.install_from_repo("o/Tool")
    assert (install_dir / "Tool.exe").read_bytes() == b"xxx"
    assert list(Path(installer.settings.download_cache).glob("dl_*")) == []
