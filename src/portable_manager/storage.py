from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, List

from .credential_store import decrypt_token, encrypt_token, is_encrypted_token
from .models import AppSettings, ManagedProgram

log = logging.getLogger(__name__)

APP_DIR_NAME = "PortableProgramManager"
SETTINGS_FILE = "settings.json"
REGISTRY_FILE = "programs.json"
# One last-good copy of the registry, refreshed before every save.
REGISTRY_BACKUP_FILE = "programs.json.bak"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


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
            if not isinstance(payload, dict):
                raise ValueError("settings file does not contain a JSON object")
            # Decrypt the GitHub token (handles both DPAPI-encrypted and legacy plaintext).
            token = payload.get("github_token")
            payload["github_token"] = decrypt_token(token) if isinstance(token, str) and token else ""
            settings = AppSettings.from_dict(payload)
            if isinstance(token, str) and token and not is_encrypted_token(token):
                # Written before tokens were encrypted: re-save so it's protected at rest.
                log.info("Encrypting a GitHub token that was stored in plain text")
                self.save_settings(settings)
            return settings
        except (KeyError, TypeError, ValueError) as exc:
            # JSONDecodeError and UnicodeDecodeError are ValueError subclasses.
            # Corrupted settings file — back up the bad file and start fresh.
            log.warning("Settings file %s is unusable, resetting to defaults: %s",
                        self.settings_path.name, exc, exc_info=True)
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

    def load_programs(self) -> List[ManagedProgram]:
        if not self.registry_path.exists():
            self.save_programs([])
            return []

        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            # JSONDecodeError and UnicodeDecodeError are ValueError subclasses.
            log.warning("Program registry %s is not valid JSON: %s", self.registry_path.name, exc)
            return self._recover_programs()
        if not isinstance(payload, list):
            log.warning("Program registry %s does not contain a JSON list", self.registry_path.name)
            return self._recover_programs()
        # Individual bad entries are skipped and reported; the file itself is left untouched.
        return self._convert_entries(payload)

    def save_programs(self, programs: List[ManagedProgram]) -> None:
        serialized = [program.to_dict() for program in programs]
        self._backup_registry()
        self._atomic_write(self.registry_path, json.dumps(serialized, indent=2))

    def _recover_programs(self) -> List[ManagedProgram]:
        """Handle an unreadable registry: restore the rolling backup if it is usable, else start empty."""
        recovered = self._read_backup_registry(self.base_dir / REGISTRY_BACKUP_FILE)
        self._backup_corrupt_file(self.registry_path)
        if recovered is None:
            log.warning("No usable registry backup found; starting with an empty program list")
            self.save_programs([])
            return []
        log.warning("Restored %d program(s) from %s", len(recovered), REGISTRY_BACKUP_FILE)
        self.save_programs(recovered)
        return recovered

    def _read_backup_registry(self, path: Path) -> List[ManagedProgram] | None:
        """Parse the rolling backup. Returns None if it is missing, unreadable or not a list."""
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Registry backup %s is unreadable: %s", path.name, exc)
            return None
        if not isinstance(payload, list):
            log.warning("Registry backup %s does not contain a JSON list", path.name)
            return None
        return self._convert_entries(payload)

    def _convert_entries(self, entries: list[Any]) -> List[ManagedProgram]:
        """Convert raw registry entries one at a time, keeping the valid ones.

        Invalid entries are logged with their index and error, and their raw
        JSON is written to a ``programs.rejected-<timestamp>.json`` file so
        nothing is silently lost.
        """
        programs: List[ManagedProgram] = []
        rejected: list[Any] = []
        for index, entry in enumerate(entries):
            try:
                programs.append(ManagedProgram.from_dict(entry))
            except Exception as exc:
                log.warning("Skipping invalid program entry #%d in %s: %s",
                            index, REGISTRY_FILE, exc)
                rejected.append(entry)
        if rejected:
            self._write_rejected_entries(rejected)
        return programs

    def _write_rejected_entries(self, rejected: list[Any]) -> None:
        target = self._unique_path(self.base_dir, f"programs.rejected-{_timestamp()}", ".json")
        try:
            self._atomic_write(target, json.dumps(rejected, indent=2))
        except OSError as exc:
            log.warning("Could not write rejected program entries to %s: %s", target.name, exc)
            return
        log.warning("Wrote %d rejected program entr%s to %s",
                    len(rejected), "y" if len(rejected) == 1 else "ies", target.name)

    def _backup_registry(self) -> None:
        """Keep one last-good copy of programs.json before it is overwritten.

        Only a file that still parses as a JSON list is copied, so a corrupt
        file never replaces a good backup.
        """
        if not self.registry_path.exists():
            return
        try:
            text = self.registry_path.read_text(encoding="utf-8")
            if not text.strip() or not isinstance(json.loads(text), list):
                return
            shutil.copy2(self.registry_path, self.base_dir / REGISTRY_BACKUP_FILE)
        except ValueError:
            return  # Not valid JSON — leave the existing backup alone.
        except OSError as exc:
            log.warning("Could not back up %s: %s", REGISTRY_FILE, exc)

    @staticmethod
    def _backup_corrupt_file(path: Path) -> Path | None:
        """Rename a corrupt file to ``<name>.corrupt-<timestamp>`` so the user can inspect it.

        Existing backups are never overwritten; a numeric suffix is added instead.
        """
        try:
            backup = Storage._unique_path(path.parent, f"{path.name}.corrupt-{_timestamp()}")
            path.rename(backup)
        except OSError as exc:
            log.warning("Could not back up corrupt file %s: %s", path.name, exc, exc_info=True)
            return None
        log.warning("Moved corrupt file %s to %s", path.name, backup.name)
        return backup

    @staticmethod
    def _unique_path(directory: Path, name: str, suffix: str = "") -> Path:
        """Return ``<name><suffix>`` in *directory*, or ``<name>-1<suffix>``, ``<name>-2<suffix>``... if taken."""
        candidate = directory / f"{name}{suffix}"
        counter = 1
        while candidate.exists():
            candidate = directory / f"{name}-{counter}{suffix}"
            counter += 1
        return candidate

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
