from __future__ import annotations

import os
from pathlib import Path

import pytest

from portable_manager import launcher
from portable_manager.errors import InstallError
from portable_manager.models import ManagedProgram


def _program(tmp_path: Path, launch_name: str = "App.exe", **kw) -> ManagedProgram:
    install = tmp_path / "App"
    install.mkdir(exist_ok=True)
    launch = install / launch_name
    launch.write_text("x")
    return ManagedProgram(
        program_id="p", name="App", source_type="github_repo", source_value="o/r",
        install_dir=str(install), launch_path=str(launch), **kw,
    )


def test_exe_args_are_passed_through_verbatim(tmp_path):
    args = '--config "C:\\path with spaces\\cfg.ini" --flag=a&b'
    spec = launcher.build_launch_spec(_program(tmp_path, launch_args=args))
    assert spec.arguments == args
    assert spec.verb == "open"
    assert spec.file.endswith("App.exe")
    assert spec.cwd == str(tmp_path / "App")


def test_run_as_admin_uses_runas(tmp_path):
    spec = launcher.build_launch_spec(_program(tmp_path, run_as_admin=True))
    assert spec.verb == "runas"


def test_ps1_runs_through_powershell_even_when_elevated(tmp_path):
    spec = launcher.build_launch_spec(_program(tmp_path, "tool.ps1", run_as_admin=True, launch_args="-X 1"))
    assert spec.file.lower().endswith("powershell.exe")
    assert '-File "' in spec.arguments and spec.arguments.endswith("-X 1")
    assert "RemoteSigned" in spec.arguments
    assert spec.verb == "runas"


def test_working_directory_override_and_fallback(tmp_path):
    other = tmp_path / "work"
    other.mkdir()
    assert launcher.build_launch_spec(_program(tmp_path, working_directory_override=str(other))).cwd == str(other)
    bad = launcher.build_launch_spec(_program(tmp_path, working_directory_override=str(tmp_path / "nope")))
    assert bad.cwd == str(tmp_path / "App")


def test_missing_launch_path_message(tmp_path):
    program = _program(tmp_path)
    program.launch_path = None
    with pytest.raises(InstallError, match="Edit"):
        launcher.build_launch_spec(program)


def test_missing_file_mentions_antivirus(tmp_path):
    program = _program(tmp_path)
    Path(program.launch_path).unlink()
    with pytest.raises(InstallError, match="Protection history"):
        launcher.launch_program(program)


def test_missing_folder_message(tmp_path):
    program = _program(tmp_path)
    program.install_dir = str(tmp_path / "gone")
    program.launch_path = str(tmp_path / "gone" / "App.exe")
    with pytest.raises(InstallError, match="no longer exists"):
        launcher.launch_program(program)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(1223, "declined"), (5, "Protection history"), (225, "potential threat"), (1155, "doesn't know"), (2, "find")],
)
def test_os_errors_are_translated(code, expected):
    exc = OSError(0, "boom")
    exc.winerror = code
    spec = launcher.LaunchSpec("C:/x/App.exe", "", "C:/x", "open")
    assert expected in str(launcher._translate_os_error(exc, spec))


@pytest.mark.skipif(os.name != "nt", reason="ShellExecute")
def test_launch_uses_shell_execute(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher.os, "startfile", lambda *a: calls.append(a))
    launcher.launch_program(_program(tmp_path, launch_args="-a"))
    assert calls == [(str(tmp_path / "App" / "App.exe"), "open", "-a", str(tmp_path / "App"))]


@pytest.mark.skipif(os.name != "nt", reason="ShellExecute")
def test_launch_translates_cancelled_uac(tmp_path, monkeypatch):
    def _cancel(*_a):
        exc = OSError(0, "cancelled")
        exc.winerror = 1223
        raise exc

    monkeypatch.setattr(launcher.os, "startfile", _cancel)
    with pytest.raises(InstallError, match="declined"):
        launcher.launch_program(_program(tmp_path, run_as_admin=True))


def test_error_sanitizer_keeps_prose_and_redacts_tokens():
    from portable_manager.ui.workers import _sanitize_error

    prose = "rate limit exceeded. Set a GitHub token in Settings to increase your rate limit."
    assert _sanitize_error(Exception(prose)) == prose
    secret = "ghp_" + "a" * 36
    msg = _sanitize_error(Exception(f"Authorization: Bearer {secret} failed; token {secret}"))
    assert secret not in msg and "[REDACTED]" in msg
