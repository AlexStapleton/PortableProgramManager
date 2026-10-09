from __future__ import annotations


class InstallError(RuntimeError):
    """A user-facing failure while installing, updating, launching or removing a program."""


class ProgramInUseError(InstallError):
    """The program (or a file inside its folder) is in use, so the operation was not started."""

    def __init__(self, message: str, process_names: list[str] | None = None) -> None:
        super().__init__(message)
        self.process_names = process_names or []


class AlreadyUpToDateError(InstallError):
    """The downloaded update is byte-identical to what is already installed."""
