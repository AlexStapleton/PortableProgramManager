from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from portable_manager import fsops
from portable_manager.errors import InstallError, ProgramInUseError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# apply_staged_tree
# ---------------------------------------------------------------------------

def test_apply_to_missing_dest_is_a_rename(tmp_path):
    staging = fsops.make_staging_dir(tmp_path)
    _write(staging / "App.exe", "v1")
    dest = tmp_path / "App"
    fsops.apply_staged_tree(staging, dest)
    assert _tree(dest) == {"App.exe": "v1"}
    assert not staging.exists()


def test_merge_replaces_files_and_keeps_user_data(tmp_path):
    dest = tmp_path / "App"
    _write(dest / "App.exe", "v1")
    _write(dest / "data" / "defaults.ini", "old defaults")
    _write(dest / "data" / "user.ini", "my settings")  # not in the new archive
    staging = fsops.make_staging_dir(tmp_path)
    _write(staging / "App.exe", "v2")
    _write(staging / "data" / "defaults.ini", "new defaults")
    _write(staging / "lib" / "new.dll", "dll")

    fsops.apply_staged_tree(staging, dest)

    assert _tree(dest) == {
        "App.exe": "v2",
        "data/defaults.ini": "new defaults",
        "data/user.ini": "my settings",
        "lib/new.dll": "dll",
    }
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(fsops.BACKUP_PREFIX)]


def test_merge_overwrites_read_only_files(tmp_path):
    dest = tmp_path / "App"
    ro = _write(dest / "App.exe", "v1")
    os.chmod(ro, stat.S_IREAD)
    staging = fsops.make_staging_dir(tmp_path)
    _write(staging / "App.exe", "v2")
    fsops.apply_staged_tree(staging, dest)
    assert (dest / "App.exe").read_text() == "v2"


def test_merge_handles_file_dir_type_swap(tmp_path):
    dest = tmp_path / "App"
    _write(dest / "plugins", "was a file")
    staging = fsops.make_staging_dir(tmp_path)
    _write(staging / "plugins" / "a.dll", "a")
    fsops.apply_staged_tree(staging, dest)
    assert _tree(dest) == {"plugins/a.dll": "a"}


@pytest.mark.skipif(os.name != "nt", reason="Windows file locking semantics")
def test_merge_rolls_back_when_a_file_is_locked(tmp_path):
    dest = tmp_path / "App"
    _write(dest / "a.txt", "old a")
    locked = _write(dest / "z_log.txt", "old log")
    staging = fsops.make_staging_dir(tmp_path)
    _write(staging / "a.txt", "new a")
    _write(staging / "new.txt", "new file")
    _write(staging / "z_log.txt", "new log")

    # An open handle without FILE_SHARE_DELETE blocks rename/replace on Windows.
    with open(locked, "r", encoding="utf-8"):
        with pytest.raises(ProgramInUseError):
            fsops.apply_staged_tree(staging, dest)

    assert _tree(dest) == {"a.txt": "old a", "z_log.txt": "old log"}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(fsops.BACKUP_PREFIX)]


def test_flatten_single_subdir(tmp_path):
    staging = fsops.make_staging_dir(tmp_path)
    _write(staging / "App-1.0" / "App.exe", "x")
    _write(staging / "App-1.0" / "App-1.0" / "nested.txt", "same name as wrapper")
    fsops.flatten_single_subdir(staging)
    assert _tree(staging) == {"App.exe": "x", "App-1.0/nested.txt": "same name as wrapper"}


def test_flatten_leaves_multiple_entries_alone(tmp_path):
    staging = fsops.make_staging_dir(tmp_path)
    _write(staging / "a" / "x.txt", "x")
    _write(staging / "readme.txt", "r")
    fsops.flatten_single_subdir(staging)
    assert _tree(staging) == {"a/x.txt": "x", "readme.txt": "r"}


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------

def test_stage_and_delete_removes_read_only_tree(tmp_path):
    folder = tmp_path / "App"
    ro = _write(folder / "sub" / "ro.txt", "x")
    os.chmod(ro, stat.S_IREAD)
    trash = fsops.stage_for_deletion(folder, "App")
    assert not folder.exists() and trash.exists()
    assert fsops.delete_staged(trash) is None
    assert not trash.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file locking semantics")
def test_stage_for_deletion_fails_cleanly_when_in_use(tmp_path):
    folder = tmp_path / "App"
    f = _write(folder / "log.txt", "x")
    with open(f, "r", encoding="utf-8"):
        with pytest.raises(ProgramInUseError):
            fsops.stage_for_deletion(folder, "App")
    assert (folder / "log.txt").exists()


def test_cleanup_stale_temp_dirs(tmp_path):
    for name in (".ppm_stage_x", ".ppm_backup_y", ".ppm_trash_z"):
        _write(tmp_path / name / "f.txt", "x")
    _write(tmp_path / "RealApp" / "app.exe", "keep")
    assert fsops.cleanup_stale_temp_dirs(tmp_path) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["RealApp"]


def test_unsafe_delete_reasons(tmp_path):
    root = tmp_path / "Portable Apps"
    app = root / "App"
    app.mkdir(parents=True)
    assert fsops.unsafe_delete_reason(app, root, []) is None
    assert "install root" in fsops.unsafe_delete_reason(root, root, [])
    assert "drive root" in fsops.unsafe_delete_reason(Path(tmp_path.anchor), root, [])
    assert "user profile" in fsops.unsafe_delete_reason(Path.home(), root, [])
    assert "another managed program" in fsops.unsafe_delete_reason(app, root, [str(app)])
    assert "another managed program" in fsops.unsafe_delete_reason(app, root, [str(app / "Nested")])
    assert "another managed program" in fsops.unsafe_delete_reason(app / "Sub", root, [str(app)])


def test_ensure_writable_dir_creates_folder(tmp_path):
    target = tmp_path / "a" / "b"
    fsops.ensure_writable_dir(target)
    assert target.is_dir() and not list(target.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="needs a protected Windows folder")
def test_ensure_writable_dir_explains_permission_error():
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    from portable_manager.winutil import is_elevated

    if is_elevated():
        pytest.skip("running elevated")
    with pytest.raises(InstallError, match="administrator rights"):
        fsops.ensure_writable_dir(windows)


def test_find_running_processes_ignores_unrelated_folders(tmp_path):
    assert fsops.find_running_processes(tmp_path) == []
    assert fsops.find_running_processes(tmp_path / "missing") == []


def test_find_running_processes_sees_the_python_interpreter():
    import sys

    import psutil

    # On Windows a venv python.exe is a redirector; the real process runs from the base install.
    found = fsops.find_running_processes(Path(psutil.Process().exe()).parent)
    assert any(str(os.getpid()) in item for item in found)


def test_is_within():
    assert fsops.is_within(r"C:\A\B\c.exe", r"C:\a\b")
    assert fsops.is_within(r"C:\A\B", r"C:\A\B")
    assert not fsops.is_within(r"C:\A\BC\c.exe", r"C:\A\B")
