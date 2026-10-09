from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import List

from .credential_store import decrypt_token, encrypt_token
from .models import AppSettings, ManagedProgram

log = logging.getLogger(__name__)

APP_DIR_NAME = "PortableProgramManager"
SETTINGS_FILE = "settings.json"
REGISTRY_FILE = "programs.json"


class Storage:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or self._default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.base_dir / SETTINGS_FILE
        self.registry_path = self.base_dir / REGISTRY_FILE

    @staticmethod
    def _default_base_dir() -> Path:
        appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            return Path(appdata) / APP_DIR_NAME
        return Path.home() / "AppData" / "Local" / APP_DIR_NAME

    def _default_settings(self) -> AppSettings:
        return AppSettings(
            install_root=str((Path.home() / "Portable Apps").resolve()),
            download_cache=str((self.base_dir / "cache").resolve()),
        )

    def load_settings(self) -> AppSettings:
        if not self.settings_path.exists():
            settings = self._default_settings()
            self.save_settings(settings)
            return settings

        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            # Decrypt the GitHub token (handles both DPAPI-encrypted and legacy plaintext).
            if payload.get("github_token"):
                payload["github_token"] = decrypt_token(payload["github_token"])
            return AppSettings.from_dict(payload)
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError) as exc:
            # Corrupted settings file — back up the bad file and start fresh.
            self._backup_corrupt_file(self.settings_path)
            settings = self._default_settings()
            self.save_settings(settings)
            return settings

    def save_settings(self, settings: AppSettings) -> None:
        try:
            Path(settings.install_root).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # Invalid or inaccessible path — save settings anyway
        try:
            Path(settings.download_cache).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        data = settings.to_dict()
        # Encrypt the GitHub token before writing to disk.
        if data.get("github_token"):
            data["github_token"] = encrypt_token(data["github_token"])
        self._atomic_write(self.settings_path, json.dumps(data, indent=2))
        # Restrict file permissions so only the current user can read the token.
        self._restrict_file_acl(self.settings_path)

    def load_programs(self) -> List[ManagedProgram]:
        if not self.registry_path.exists():
            self.save_programs([])
            return []

        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return [ManagedProgram.from_dict(item) for item in payload]
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError) as exc:
            # Corrupted programs file — back up the bad file and start with empty list.
            self._backup_corrupt_file(self.registry_path)
            self.save_programs([])
            return []

    def save_programs(self, programs: List[ManagedProgram]) -> None:
        serialized = [program.to_dict() for program in programs]
        self._atomic_write(self.registry_path, json.dumps(serialized, indent=2))

    @staticmethod
    def _backup_corrupt_file(path: Path) -> None:
        """Rename a corrupt file with a .corrupt suffix so the user can inspect it."""
        try:
            backup = path.with_suffix(path.suffix + ".corrupt")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
        except OSError:
            pass

    @staticmethod
    def _restrict_file_acl(path: Path) -> None:
        """On Windows, restrict the file ACL so only the current user can read/write it.

        Uses ``icacls`` to remove inherited permissions and grant access only
        to the current user.  Failures are logged but not raised — the
        application should still function even if ACL hardening fails.
        """
        if platform.system() != "Windows":
            return
        try:
            username = os.environ.get("USERNAME", "")
            if not username:
                return
            # Disable inheritance and remove all inherited ACEs, then grant
            # full control only to the current user.
            subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", f"{username}:(F)"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            log.debug("Could not restrict ACL on %s", path, exc_info=True)

    @staticmethod
    def _atomic_write(target: Path, data: str) -> None:
        """Write *data* to *target* atomically via a temp file + rename.

        On Windows ``os.replace`` is atomic at the filesystem level for NTFS,
        so a crash mid-write will leave either the old file or the new file
        intact — never a half-written file.
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent), suffix=".tmp", prefix=target.stem + "_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, str(target))
        except BaseException:
            # Clean up the temp file if the rename failed.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
