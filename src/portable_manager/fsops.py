"""Filesystem operations for installing, updating and removing managed programs.

Everything here is written so that a failure leaves the user's install folder
either fully old or fully new, never half-replaced:

- New content is always prepared in a *staging* folder next to the install
  folder (same volume, so moves are cheap renames).
- :func:`apply_staged_tree` merges staging into the install folder file by
  file, backing up every file it replaces and rolling back on any error.
  Files the archive doesn't contain (user settings, saves, plugins) are kept.
- :func:`stage_for_deletion` renames the folder aside first; Windows refuses
  that rename while anything inside is in use, so removal either starts
  cleanly or not at all.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import stat
import uuid
from pathlib import Path
from typing import Iterable

from .errors import InstallError, ProgramInUseError

log = logging.getLogger(__name__)

# Prefixes of the hidden helper folders created next to install folders.
STAGING_PREFIX = ".ppm_stage_"
BACKUP_PREFIX = ".ppm_backup_"
TRASH_PREFIX = ".ppm_trash_"
_TEMP_PREFIXES = (STAGING_PREFIX, BACKUP_PREFIX, TRASH_PREFIX, ".ppm_extract_")

# Windows error codes that mean "something has this file open".
_IN_USE_WINERRORS = {5, 32, 33}  # ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_within(child: Path | str, parent: Path | str) -> bool:
    """True if *child* is *parent* or lies inside it (case-insensitive on Windows)."""
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def clear_readonly(path: Path | str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _is_in_use_error(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _IN_USE_WINERRORS


def rmtree_robust(path: Path) -> None:
    """``shutil.rmtree`` that also deletes read-only files (common in portable apps)."""

    def _onexc(func, failed_path, exc):
        if isinstance(exc, FileNotFoundError):
            return
        if isinstance(exc, PermissionError):
            clear_readonly(failed_path)
            func(failed_path)
            return
        raise exc

    shutil.rmtree(path, onexc=_onexc)


# ---------------------------------------------------------------------------
# Permission / usage checks
# ---------------------------------------------------------------------------

def ensure_writable_dir(path: Path) -> None:
    """Create *path* if needed and prove we can write to it.

    Raises :class:`InstallError` with an actionable message when Windows
    denies access, which typically happens for folders under Program Files.
    """
    # A single explicit create: tempfile's helpers retry PermissionError up to
    # 10,000 times on Windows (os.access can't see ACLs), which looks like a hang.
    probe = path / f".ppm_write_test_{uuid.uuid4().hex[:8]}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        os.unlink(probe)
    except PermissionError as exc:
        raise InstallError(
            f"Windows doesn't allow this app to write to:\n{path}\n\n"
            "Folders such as Program Files need administrator rights. Choose an install "
            "folder inside your user profile in Settings → Install root."
        ) from exc
    except OSError as exc:
        raise InstallError(f"The folder {path} can't be used: {exc}") from exc


def find_running_processes(folder: Path) -> list[str]:
    """Return ``"name (pid)"`` for every running process whose executable lives in *folder*.

    Processes we can't inspect (elevated ones, when we're not elevated) are
    skipped; the rename/backup steps still catch those as "in use".
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return []
    if not folder.exists():
        return []
    found: list[str] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = proc.info.get("exe")
        except (psutil.Error, OSError):
            continue
        if exe and is_within(exe, folder):
            found.append(f"{proc.info.get('name') or Path(exe).name} ({proc.info['pid']})")
    return found


def running_programs_by_folder(folders: Iterable[Path | str]) -> dict[str, list[str]]:
    """One process scan for many folders: ``{normalised folder: ["name (pid)", ...]}``.

    Only folders with at least one running process appear in the result.
    Keys use :func:`os.path.normcase` + ``abspath`` (same as ``installer.normalize_dir``).
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return {}
    wanted = {_norm(f) for f in folders}
    if not wanted:
        return {}
    found: dict[str, list[str]] = {}
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = proc.info.get("exe")
        except (psutil.Error, OSError):
            continue
        if not exe:
            continue
        for folder in wanted:
            if is_within(exe, folder):
                found.setdefault(folder, []).append(f"{proc.info.get('name') or Path(exe).name} ({proc.info['pid']})")
    return found


def in_use_message(action: str, program_name: str, processes: list[str]) -> str:
    if processes:
        listed = ", ".join(processes[:5]) + (" …" if len(processes) > 5 else "")
        return f"Close {program_name} before you {action} it. Still running: {listed}."
    return (
        f"Some files of {program_name} are in use, so it can't be {action}d right now. "
        "Close the program (and any Explorer window or terminal opened in its folder), then try again."
    )


def unsafe_delete_reason(
    path: Path,
    install_root: Path | None = None,
    other_program_dirs: Iterable[Path | str] = (),
) -> str | None:
    """Return why deleting *path* would be dangerous, or ``None`` if it's fine."""
    try:
        resolved = path.resolve()
    except OSError as exc:
        return f"the path can't be resolved ({exc})"
    target = _norm(resolved)

    if resolved == Path(resolved.anchor) or len(resolved.parts) < 3:
        return "it is a drive root or a top-level folder"

    home = Path.home().resolve()
    if target == _norm(home) or is_within(home, resolved):
        return "it is your user profile folder or one of its parents"

    protected = [os.environ.get(name) for name in (
        "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "PUBLIC",
    )]
    protected += [str(home / name) for name in ("Desktop", "Documents", "Downloads", "OneDrive")]
    for item in filter(None, protected):
        if target == _norm(item):
            return f"it is a protected system or user folder ({item})"
    system_root = os.environ.get("SystemRoot")
    if system_root and is_within(resolved, system_root):
        return "it is inside the Windows folder"

    if install_root is not None and target == _norm(install_root):
        return "it is the install root that holds all your programs"

    for other in other_program_dirs:
        if target == _norm(other) or is_within(other, resolved) or is_within(resolved, other):
            return f"another managed program uses this folder ({other})"
    return None


# ---------------------------------------------------------------------------
# Staging, merging and deleting
# ---------------------------------------------------------------------------

def make_staging_dir(parent: Path) -> Path:
    """Create an empty hidden staging folder in *parent* (same volume as the install)."""
    staging = parent / f"{STAGING_PREFIX}{uuid.uuid4().hex[:12]}"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
    except PermissionError as exc:
        # Not tempfile.mkdtemp: it retries PermissionError thousands of times on Windows.
        raise InstallError(
            f"Windows doesn't allow this app to write to:\n{parent}\n\n"
            "Choose an install folder inside your user profile in Settings → Install root."
        ) from exc
    return staging


def flatten_single_subdir(folder: Path) -> None:
    """If *folder* holds exactly one sub-folder and nothing else, lift its contents up.

    Handles the usual GitHub release layout ``AppName-v1.2.3/...``. Only ever
    called on a fresh staging folder, so there's nothing to collide with.
    """
    entries = list(folder.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return
    wrapper = entries[0]
    # Rename the wrapper first so a child with the same name can't collide with it.
    temp = folder / f".ppm_wrap_{uuid.uuid4().hex[:8]}"
    wrapper.rename(temp)
    for item in list(temp.iterdir()):
        item.rename(folder / item.name)
    temp.rmdir()


def apply_staged_tree(staging: Path, dest: Path) -> None:
    """Move everything in *staging* into *dest* as one all-or-nothing step.

    - *dest* missing → a single rename.
    - *dest* present → file-by-file merge. Each replaced file is first moved
      to a backup folder; on any error all changes are rolled back and an
      :class:`InstallError` (or :class:`ProgramInUseError`) is raised.
      Files that exist only in *dest* are left alone.
    """
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, dest)
        return
    if not dest.is_dir():
        raise InstallError(f"Install path exists but is not a folder: {dest}")

    backup_root = dest.parent / f"{BACKUP_PREFIX}{dest.name}_{uuid.uuid4().hex[:8]}"
    replaced: list[tuple[Path, Path]] = []  # (target, backup)
    added: list[Path] = []                  # new files with no previous version
    created_dirs: list[Path] = []

    def _backup(target: Path) -> Path:
        backup = backup_root / target.relative_to(dest)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            clear_readonly(target)
        os.replace(target, backup)
        return backup

    try:
        for root, dirs, files in os.walk(staging):
            rel = Path(root).relative_to(staging)
            for name in dirs:
                target = dest / rel / name
                if target.exists() and not target.is_dir():
                    replaced.append((target, _backup(target)))
                if not target.exists():
                    target.mkdir()
                    created_dirs.append(target)
            for name in files:
                source = Path(root) / name
                target = dest / rel / name
                if target.is_dir():
                    replaced.append((target, _backup(target)))
                elif target.exists():
                    replaced.append((target, _backup(target)))
                else:
                    added.append(target)
                os.replace(source, target)
    except OSError as exc:
        _rollback(replaced, added, created_dirs)
        shutil.rmtree(backup_root, ignore_errors=True)
        if _is_in_use_error(exc):
            raise ProgramInUseError(
                f"A file is in use and couldn't be replaced ({exc.filename or exc}). "
                "No changes were made. Close the program and try again."
            ) from exc
        raise InstallError(f"Couldn't apply the new files, so no changes were made: {exc}") from exc

    # Success: drop the backups. A file from a still-running process may refuse
    # to go; leave it for cleanup_stale_temp_dirs() instead of failing.
    try:
        if backup_root.exists():
            rmtree_robust(backup_root)
    except OSError:
        log.info("Backup folder %s will be removed later", backup_root, exc_info=True)


def _rollback(replaced: list[tuple[Path, Path]], added: list[Path], created_dirs: list[Path]) -> None:
    for target in reversed(added):
        try:
            clear_readonly(target)
            target.unlink(missing_ok=True)
        except OSError:
            log.error("Rollback: couldn't remove new file %s", target, exc_info=True)
    for target, backup in reversed(replaced):
        try:
            if target.is_dir() and not backup.is_dir():
                rmtree_robust(target)
            elif target.exists() and not target.is_dir():
                clear_readonly(target)
                target.unlink()
            os.replace(backup, target)
        except OSError:
            log.error("Rollback: couldn't restore %s from %s", target, backup, exc_info=True)
    for folder in reversed(created_dirs):
        try:
            folder.rmdir()
        except OSError:
            pass


def stage_for_deletion(folder: Path, program_name: str) -> Path:
    """Rename *folder* to a hidden trash name next to it and return the new path.

    This is the commit point of a removal: Windows refuses the rename while a
    file inside is open, so we fail cleanly before deleting anything.
    """
    trash = folder.with_name(f"{TRASH_PREFIX}{folder.name}_{uuid.uuid4().hex[:8]}")
    try:
        os.rename(folder, trash)
    except OSError as exc:
        if _is_in_use_error(exc):
            raise ProgramInUseError(in_use_message("delete", program_name, [])) from exc
        raise InstallError(f"Couldn't delete {folder}: {exc}") from exc
    return trash


def delete_staged(trash: Path) -> str | None:
    """Delete a folder returned by :func:`stage_for_deletion`. Returns a warning on partial failure."""
    try:
        rmtree_robust(trash)
        return None
    except OSError as exc:
        log.warning("Couldn't fully delete %s", trash, exc_info=True)
        return (
            f"Some files couldn't be deleted yet ({exc}). They were left in {trash} "
            "and will be cleaned up automatically later."
        )


def cleanup_stale_temp_dirs(root: Path) -> int:
    """Remove leftover staging/backup/trash folders in *root*. Returns how many were removed."""
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.is_dir() and entry.name.startswith(_TEMP_PREFIXES):
            try:
                rmtree_robust(entry)
                removed += 1
            except OSError:
                log.info("Stale folder %s is still in use", entry, exc_info=True)
    return removed


# ---------------------------------------------------------------------------
# Permission repair
# ---------------------------------------------------------------------------
#
# Installs made by v0.7 and earlier extracted into tempfile.mkdtemp() folders.
# Since Python 3.13 those get a protected ACL on Windows (Owner Rights, SYSTEM,
# Administrators; inheritance blocked), and moved files keep it. When the
# manager ran elevated, the owner was the Administrators group, so the user's
# normal account lost access to its own programs. Repair resets every ACL to
# inherit from the install folder's parent again.

_ERROR_ACCESS_DENIED = 5
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_permission_problems(folder: Path, limit: int = 5) -> list[Path]:
    """Return up to *limit* items under *folder* the current user can't open.

    Only "access denied" counts; files that are merely in use are ignored.
    """
    problems: list[Path] = []

    def _denied(exc: OSError) -> bool:
        return getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED or (
            isinstance(exc, PermissionError) and getattr(exc, "winerror", None) is None
        )

    stack = [folder]
    while stack and len(problems) < limit:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            continue
        except OSError as exc:
            if _denied(exc):
                problems.append(Path(current))
            continue
        for entry in entries:
            if len(problems) >= limit:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    with open(entry.path, "rb"):
                        pass
            except OSError as exc:
                if _denied(exc):
                    problems.append(Path(entry.path))
    return problems


def _reset_acl_command(folder: Path) -> list[str]:
    return ["icacls", str(folder), "/reset", "/T", "/C", "/Q"]


def repair_permissions(folder: Path, allow_elevation: bool = True) -> bool:
    """Reset ACLs under *folder* so they inherit normally. Returns True if all items are accessible after.

    Tries without elevation first (enough when the user owns the files). If
    items remain inaccessible and *allow_elevation* is set, takes ownership
    and resets again with administrator rights, which shows a UAC prompt.
    Raises :class:`InstallError` if the user declines the prompt.
    """
    if not folder.is_dir():
        raise InstallError(f"The folder doesn't exist: {folder}")
    subprocess.run(_reset_acl_command(folder), capture_output=True, text=True, creationflags=_NO_WINDOW)
    if not find_permission_problems(folder, limit=1):
        return True
    if not allow_elevation:
        return False

    from .winutil import run_elevated_and_wait

    script = (
        f'/c takeown /F "{folder}" /R /D Y >nul 2>&1 & '
        f'icacls "{folder}" /setowner "{_current_user()}" /T /C /Q >nul 2>&1 & '
        f'icacls "{folder}" /reset /T /C /Q >nul 2>&1'
    )
    try:
        run_elevated_and_wait(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), script)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1223:
            raise InstallError("Permission repair cancelled: the administrator (UAC) prompt was declined.") from exc
        raise InstallError(f"Couldn't run the permission repair: {exc}") from exc
    return not find_permission_problems(folder, limit=1)


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain and user else user
