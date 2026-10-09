"""Launching managed programs the way Explorer does.

Programs are started through ``ShellExecuteEx`` (``os.startfile``) rather than
``CreateProcess`` (``subprocess``). The difference matters for permissions:
CreateProcess refuses programs whose manifest says ``requireAdministrator``
(``ERROR_ELEVATION_REQUIRED``, 740), while ShellExecute shows the UAC prompt
for them automatically. The ``runas`` verb forces elevation for programs that
need admin rights without declaring it.

Launch arguments are passed through as the raw command-line string. Windows
programs parse their own command line, so re-tokenising and re-quoting it
only corrupts quoted paths.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import InstallError
from .models import ManagedProgram

log = logging.getLogger(__name__)

# Windows error codes we translate into actionable messages.
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_VIRUS_INFECTED = 225
_ERROR_VIRUS_DELETED = 226
_ERROR_NO_ASSOCIATION = 1155
_ERROR_CANCELLED = 1223

_AV_HINT = (
    "This usually means antivirus (for example Windows Security) blocked or quarantined it. "
    "Check Windows Security → Virus & threat protection → Protection history, and restore or "
    "allow the file if you trust it."
)

_dll_dir_lock = threading.Lock()

if getattr(sys, "frozen", False):
    # Tell any PyInstaller-built program we launch to ignore our bootloader's
    # environment variables instead of mistaking them for its own.
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"


@dataclass(frozen=True)
class LaunchSpec:
    file: str
    arguments: str
    cwd: str
    verb: str  # "open" or "runas"


def _powershell_exe() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell.exe"


def build_launch_spec(program: ManagedProgram) -> LaunchSpec:
    """Work out exactly what to hand to ShellExecute. Pure function; no I/O besides path checks."""
    if not program.launch_path:
        raise InstallError("This program doesn't have a launch file yet. Use Edit to choose one.")
    launch = Path(program.launch_path)

    cwd = program.working_directory_override or program.install_dir
    if not cwd or not Path(cwd).is_dir():
        cwd = str(launch.parent)

    args = (program.launch_args or "").strip()
    verb = "runas" if program.run_as_admin else "open"

    if launch.suffix.lower() == ".ps1":
        ps_args = f'-NoProfile -ExecutionPolicy RemoteSigned -File "{launch}"'
        return LaunchSpec(_powershell_exe(), f"{ps_args} {args}".strip(), cwd, verb)
    # .exe, .bat and .cmd: ShellExecute runs batch files through cmd.exe itself.
    return LaunchSpec(str(launch), args, cwd, verb)


def _check_launch_file(program: ManagedProgram) -> None:
    launch = Path(program.launch_path or "")
    try:
        exists = launch.is_file()
    except PermissionError:
        raise InstallError(f"Windows denied access to {launch}.\n\n{_AV_HINT}")
    if exists:
        try:
            with open(launch, "rb"):
                pass
        except PermissionError:
            raise InstallError(f"Windows denied access to {launch}.\n\n{_AV_HINT}")
        except OSError as exc:
            if getattr(exc, "winerror", None) in (_ERROR_VIRUS_INFECTED, _ERROR_VIRUS_DELETED):
                raise InstallError(f"Windows Security blocked {launch.name} as a potential threat.\n\n{_AV_HINT}")
        return
    if Path(program.install_dir).is_dir():
        raise InstallError(
            f"The program file is missing:\n{launch}\n\n"
            "If it was there after installing, antivirus may have quarantined it. "
            f"{_AV_HINT}\n\nOtherwise, use Edit to choose the right launch file."
        )
    raise InstallError(
        f"The install folder no longer exists:\n{program.install_dir}\n\n"
        "It may have been moved or deleted. Reinstall the program or remove it from the manager."
    )


def _translate_os_error(exc: OSError, spec: LaunchSpec) -> InstallError:
    code = getattr(exc, "winerror", None)
    name = Path(spec.file).name
    if code == _ERROR_CANCELLED:
        return InstallError("Launch cancelled: the administrator (UAC) prompt was declined.")
    if code == _ERROR_ACCESS_DENIED:
        return InstallError(f"Windows denied access when starting {name}.\n\n{_AV_HINT}")
    if code in (_ERROR_VIRUS_INFECTED, _ERROR_VIRUS_DELETED):
        return InstallError(f"Windows Security blocked {name} as a potential threat.\n\n{_AV_HINT}")
    if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        return InstallError(f"Couldn't find {spec.file}.")
    if code == _ERROR_NO_ASSOCIATION:
        return InstallError(f"Windows doesn't know how to open {name}. Choose an .exe, .bat, .cmd or .ps1 file.")
    return InstallError(f"Couldn't start {name}: {exc.strerror or exc}")


class _ComApartment:
    """ShellExecute may use shell extensions, which expect COM on the calling thread."""

    def __enter__(self) -> "_ComApartment":
        self._uninit = False
        if sys.platform == "win32":
            hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)  # APARTMENTTHREADED | DISABLE_OLE1DDE
            self._uninit = hr in (0, 1)  # S_OK / S_FALSE need a matching CoUninitialize
        return self

    def __exit__(self, *exc) -> None:
        if self._uninit:
            ctypes.windll.ole32.CoUninitialize()


class _ChildDllSearchPath:
    """A frozen (PyInstaller) build adds its own folder to the DLL search path, and
    child processes inherit that. Clear it while starting the child so a launched
    program can't pick up our Qt/Python DLLs, then restore it."""

    def __enter__(self) -> "_ChildDllSearchPath":
        self._restore = getattr(sys, "_MEIPASS", None) if sys.platform == "win32" else None
        if self._restore:
            _dll_dir_lock.acquire()
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        return self

    def __exit__(self, *exc) -> None:
        if self._restore:
            ctypes.windll.kernel32.SetDllDirectoryW(self._restore)
            _dll_dir_lock.release()


def launch_program(program: ManagedProgram) -> None:
    """Start *program*. Blocks while a UAC prompt is open, so call it off the UI thread."""
    _check_launch_file(program)
    spec = build_launch_spec(program)
    log.info("Launching %s: %s %s (verb=%s, cwd=%s)", program.name, spec.file, spec.arguments, spec.verb, spec.cwd)
    if sys.platform != "win32":  # pragma: no cover - the app targets Windows
        raise InstallError("Launching programs is only supported on Windows.")
    try:
        with _ComApartment(), _ChildDllSearchPath():
            os.startfile(spec.file, spec.verb, spec.arguments, spec.cwd)
    except OSError as exc:
        log.warning("Launch of %s failed", program.name, exc_info=True)
        raise _translate_os_error(exc, spec) from exc
