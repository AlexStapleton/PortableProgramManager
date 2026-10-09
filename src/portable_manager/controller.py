from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from . import fsops
from .errors import InstallError, ProgramInUseError
from .github_client import GitHubClient, GitHubRepo
from .installer import PortableInstaller, normalize_dir
from .launcher import launch_program
from .models import AppSettings, ManagedProgram, UpdatePolicy
from .storage import Storage

ProgressCallback = Callable[[int, str], None] | None

log = logging.getLogger(__name__)


@dataclass
class InstallOutcome:
    """Returned by controller install methods to carry both the program and
    any advisory flags the UI should act on."""
    program: ManagedProgram
    is_likely_installer: bool = False


@dataclass
class RemoveOutcome:
    program_name: str
    deleted_files: bool
    warnings: list[str] = field(default_factory=list)


# Fields an update install changes; committed from the worker's snapshot.
_INSTALL_STATE_FIELDS = (
    "launch_path", "installed_hash", "version", "asset_name", "installed_asset_url",
    "installed_asset_name", "installed_asset_size", "latest_upstream_version",
    "latest_upstream_published_at", "update_available", "update_available_asset_name",
)


class AppController:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.settings = storage.load_settings()
        self.programs = storage.load_programs()
        self._program_index: dict[str, ManagedProgram] = {p.program_id: p for p in self.programs}
        self.github = GitHubClient(token=self.settings.github_token)
        self.installer = PortableInstaller(self.settings, self.github)
        self._lock = RLock()

    def reload_clients(self) -> None:
        with self._lock:
            old = self.github
            self.github = GitHubClient(token=self.settings.github_token)
            self.installer = PortableInstaller(self.settings, self.github)
        old.close()

    def _get_github(self) -> GitHubClient:
        """Return the shared GitHubClient, creating/replacing it only when the token changes."""
        with self._lock:
            return self.github

    def search_github(self, query: str, sort: str = "stars", progress_callback: ProgressCallback = None) -> list[GitHubRepo]:
        if progress_callback:
            progress_callback(10, f"Searching GitHub for '{query}'")
        with self._lock:
            limit = self.settings.search_result_limit
        results = self._get_github().search_repositories(query, limit=limit, sort=sort)
        if progress_callback:
            progress_callback(100, f"Loaded {len(results)} search results")
        return results

    def fetch_release_dates(self, repos: list[GitHubRepo], progress_callback: ProgressCallback = None) -> list[GitHubRepo]:
        """Fetch the latest release info for each repo (date + Windows asset check)."""
        github = self._get_github()
        total = len(repos)
        for i, repo in enumerate(repos):
            if progress_callback:
                progress_callback(int((i / max(total, 1)) * 100), f"Fetching release info ({i+1}/{total})")
            try:
                date, has_win = github.get_latest_release_info(repo.full_name)
                repo.latest_release_at = date
                repo.has_windows_release = has_win
            except Exception:
                repo.latest_release_at = ""
                repo.has_windows_release = None
        if progress_callback:
            progress_callback(100, f"Fetched release info for {total} repos")
        return repos

    def install_from_repo(
        self,
        repo_full_name: str,
        channel: str = "latest_release",
        progress_callback: ProgressCallback = None,
    ) -> InstallOutcome:
        with self._lock:
            installer = self.installer
            taken = self._taken_dirs_no_lock()
        result = installer.install_from_repo(
            repo_full_name, channel=channel, progress_callback=progress_callback, taken_dirs=taken
        )
        self._upsert_program(result.program)
        return InstallOutcome(program=copy.deepcopy(result.program), is_likely_installer=result.is_likely_installer)

    def install_from_url(
        self,
        url: str,
        display_name: str | None = None,
        progress_callback: ProgressCallback = None,
    ) -> InstallOutcome:
        with self._lock:
            installer = self.installer
            taken = self._taken_dirs_no_lock()
        result = installer.install_from_url(
            url, display_name=display_name, progress_callback=progress_callback, taken_dirs=taken
        )
        self._upsert_program(result.program)
        return InstallOutcome(program=copy.deepcopy(result.program), is_likely_installer=result.is_likely_installer)

    def run_program(self, program_id: str, progress_callback: ProgressCallback = None) -> ManagedProgram:
        """Launch a program. Blocks while a UAC prompt is open, so run it in a worker."""
        # Take a snapshot under the lock, then release it before launching so a
        # UAC prompt doesn't block the entire controller.
        with self._lock:
            snapshot = copy.deepcopy(self._get_program_no_lock(program_id))
        if progress_callback:
            progress_callback(50, f"Starting {snapshot.name}...")
        launch_program(snapshot)
        with self._lock:
            program = self._get_program_no_lock(program_id)
            program.last_run_at = datetime.now(timezone.utc).isoformat()
            self.storage.save_programs(self.programs)
            return copy.deepcopy(program)

    def remove_program(
        self,
        program_id: str,
        delete_files: bool = False,
        progress_callback: ProgressCallback = None,
    ) -> RemoveOutcome:
        """Remove a program from the registry and optionally delete its folder.

        With *delete_files*, the folder is first renamed aside (which fails
        cleanly if anything inside is in use); only then is the registry entry
        removed and the renamed folder deleted. Nothing is removed if the
        folder is unsafe to delete or the program is running.
        """
        with self._lock:
            program = self._get_program_no_lock(program_id)
            name = program.name
            install_dir = Path(program.install_dir)
            other_dirs = [p.install_dir for p in self.programs if p.program_id != program_id]
            install_root = Path(self.settings.install_root)

        trash: Path | None = None
        if delete_files and install_dir.exists():
            reason = fsops.unsafe_delete_reason(install_dir, install_root, other_dirs)
            if reason:
                raise InstallError(
                    f"For safety, {install_dir} was not deleted because {reason}. "
                    "Use 'Remove only' and delete the files yourself if you're sure."
                )
            if progress_callback:
                progress_callback(20, f"Checking that {name} isn't running...")
            running = fsops.find_running_processes(install_dir)
            if running:
                raise ProgramInUseError(fsops.in_use_message("delete", name, running), running)
            trash = fsops.stage_for_deletion(install_dir, name)

        with self._lock:
            removed = self._program_index.pop(program_id, None)
            if removed is not None:
                self.programs = [p for p in self.programs if p.program_id != program_id]
                self.storage.save_programs(self.programs)

        outcome = RemoveOutcome(program_name=name, deleted_files=delete_files)
        if trash is not None:
            if progress_callback:
                progress_callback(60, f"Deleting {install_dir}...")
            warning = fsops.delete_staged(trash)
            if warning:
                outcome.warnings.append(warning)
        return outcome

    def cleanup_stale_temp_dirs(self, progress_callback: ProgressCallback = None) -> int:
        """Delete leftover staging/backup/trash folders from interrupted operations."""
        with self._lock:
            roots = {normalize_dir(self.settings.install_root): Path(self.settings.install_root)}
            for program in self.programs:
                parent = Path(program.install_dir).parent
                roots.setdefault(normalize_dir(parent), parent)
        return sum(fsops.cleanup_stale_temp_dirs(root) for root in roots.values())

    def edit_program(
        self,
        program_id: str,
        name: str,
        notes: str,
        launch_path: str | None,
        launch_args: str,
        working_directory_override: str | None,
        update_policy: UpdatePolicy,
        run_as_admin: bool = False,
    ) -> ManagedProgram:
        if not name or not name.strip():
            raise ValueError("Program name cannot be empty.")
        with self._lock:
            program = self._get_program_no_lock(program_id)
            program.name = name.strip()
            program.notes = notes
            program.launch_path = launch_path
            program.launch_args = launch_args
            program.working_directory_override = working_directory_override
            program.update_policy = update_policy
            program.run_as_admin = run_as_admin
            self.storage.save_programs(self.programs)
            return program

    def get_program(self, program_id: str) -> ManagedProgram:
        with self._lock:
            return copy.deepcopy(self._get_program_no_lock(program_id))

    def list_programs(self) -> list[ManagedProgram]:
        """Return copies, so the UI never reads objects a worker is mutating."""
        with self._lock:
            return copy.deepcopy(self.programs)

    def update_settings(self, settings: AppSettings) -> None:
        with self._lock:
            self.settings = settings
            self.storage.save_settings(settings)
            old_github = self.github
            self.github = GitHubClient(token=self.settings.github_token)
            self.installer = PortableInstaller(self.settings, self.github)
            old_github.close()
            updated = False
            for program in self.programs:
                policy = program.update_policy
                # Fix up None / missing values from legacy data where JSON
                # stored an explicit null for these fields.
                if not policy.update_mode:
                    policy.update_mode = settings.default_update_mode
                    updated = True
                if not policy.schedule_type:
                    policy.schedule_type = settings.default_update_schedule_type
                    updated = True
                if not policy.interval_hours:
                    policy.interval_hours = settings.default_update_interval_hours
                    updated = True
            if updated:
                self.storage.save_programs(self.programs)

    def check_for_updates(self, program_id: str, progress_callback: ProgressCallback = None) -> ManagedProgram:
        # Read all state needed for the API call under the lock, then release it
        # before the I/O-bound GitHub request so the UI thread is not blocked.
        with self._lock:
            program = self._get_program_no_lock(program_id)
            source_type = program.source_type
            program_name = program.name
            repo_full_name = program.repo_full_name
            channel = program.update_policy.channel
            asset_hint = (
                program.installed_asset_name
                or program.asset_name
                or program.update_policy.asset_selection_override
            )

        now = datetime.now(timezone.utc).isoformat()
        if progress_callback:
            progress_callback(15, f"Checking updates for {program_name}")

        if source_type not in {"github_repo", "direct_url"} or not repo_full_name:
            with self._lock:
                program = self._get_program_no_lock(program_id)
                program.last_update_attempt_at = now
                program.last_checked_at = now
                program.last_update_status = "unsupported_source"
                program.last_error_message = "This program does not have a GitHub repository source for update checks."
                program.last_error_at = now
                self.storage.save_programs(self.programs)
            if progress_callback:
                progress_callback(100, f"Update checks are not supported for {program_name}")
            return self.get_program(program_id)

        try:
            release = self._get_github().get_latest_release(
                repo_full_name, include_prereleases=channel == "prerelease"
            )
            if not release:
                raise InstallError("No GitHub release was found for this program.")
            asset = PortableInstaller.pick_portable_asset(release.assets, asset_name_hint=asset_hint)
            with self._lock:
                # Re-lookup: program may have been removed/replaced while the lock was released.
                program = self._get_program_no_lock(program_id)
                update_available = self._is_update_available(program, release.tag_name, asset.name if asset else None)
                program.last_update_attempt_at = now
                program.last_checked_at = now
                program.latest_upstream_version = release.tag_name
                program.latest_upstream_published_at = release.published_at
                program.last_error_message = None
                program.last_error_at = None
                if update_available:
                    program.update_available = True
                    program.update_available_asset_name = asset.name if asset else None
                    program.last_update_found_at = now
                    program.last_update_status = "update_available"
                else:
                    program.update_available = False
                    program.update_available_asset_name = None
                    program.last_update_status = "no_update"
                self.storage.save_programs(self.programs)
        except KeyError:
            # Program was removed while we were checking — nothing to update.
            if progress_callback:
                progress_callback(100, f"Program {program_name} was removed during update check")
            raise
        except Exception as exc:
            with self._lock:
                try:
                    program = self._get_program_no_lock(program_id)
                except KeyError:
                    # Program was removed while we handled the error — nothing to mark.
                    raise KeyError(program_id) from exc
                program.last_update_attempt_at = now
                program.last_checked_at = now
                program.last_update_status = "check_failed"
                program.last_error_message = str(exc)
                program.last_error_at = now
                self.storage.save_programs(self.programs)

        if progress_callback:
            progress_callback(100, f"Finished checking {program_name}")
        return self.get_program(program_id)

    def check_for_updates_all(self, progress_callback: ProgressCallback = None) -> list[ManagedProgram]:
        with self._lock:
            program_ids = [program.program_id for program in self.programs if program.update_policy.check_enabled]
        results: list[ManagedProgram] = []
        total = len(program_ids)
        if total == 0:
            if progress_callback:
                progress_callback(100, "No managed programs are enabled for update checks")
            return results
        for index, program_id in enumerate(program_ids, start=1):
            if progress_callback:
                progress_callback(int(((index - 1) / total) * 100), f"Checking updates ({index}/{total})")
            results.append(self.check_for_updates(program_id))
        if progress_callback:
            available = sum(1 for item in results if item.update_available)
            progress_callback(100, f"Checked {total} programs. {available} update(s) available")
        return results

    @staticmethod
    def is_check_due(program: ManagedProgram) -> bool:
        """Return True if *program* is due for an automatic update check right now.

        Schedule rules:
        - ``app_start`` : always due (caller decides whether to include it).
        - ``interval``  : due when ``now >= last_checked_at + interval_hours``.
        - ``daily``     : due when today (local date) is later than the date of the last check.
        - ``weekly``    : due when ``now >= last_checked_at + 7 days``.
        """
        policy = program.update_policy
        if not policy.check_enabled:
            return False

        schedule = policy.schedule_type
        if schedule == "app_start":
            return True  # caller controls inclusion via include_app_start

        last_checked = program.last_checked_at
        if not last_checked:
            return True  # never been checked → always due

        try:
            last = datetime.fromisoformat(last_checked)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True  # unparseable timestamp → check to be safe

        now = datetime.now(timezone.utc)
        if schedule == "interval":
            return now >= last + timedelta(hours=policy.interval_hours)
        if schedule == "daily":
            return now.astimezone().date() > last.astimezone().date()
        if schedule == "weekly":
            return now >= last + timedelta(days=7)
        return False

    def check_due_updates(
        self,
        include_app_start: bool = False,
        progress_callback: ProgressCallback = None,
    ) -> list[ManagedProgram]:
        """Check updates for programs whose schedule indicates they are currently due.

        Args:
            include_app_start: When True (startup), programs with
                ``schedule_type='app_start'`` are included.  When False
                (periodic timer), only interval/daily/weekly programs are checked.
        """
        with self._lock:
            due_ids = [
                p.program_id
                for p in self.programs
                if p.update_policy.check_enabled
                and (p.update_policy.schedule_type != "app_start" or include_app_start)
                and self.is_check_due(p)
            ]
        results: list[ManagedProgram] = []
        total = len(due_ids)
        if total == 0:
            if progress_callback:
                progress_callback(100, "No updates are due at this time")
            return results
        for index, pid in enumerate(due_ids, start=1):
            if progress_callback:
                progress_callback(int(((index - 1) / total) * 100), f"Checking updates ({index}/{total})")
            results.append(self.check_for_updates(pid))
        if progress_callback:
            available = sum(1 for p in results if p.update_available)
            progress_callback(100, f"Checked {total} program(s). {available} update(s) available")
        return results

    def install_available_update(self, program_id: str, progress_callback: ProgressCallback = None) -> ManagedProgram:
        program = self.check_for_updates(program_id, progress_callback=progress_callback)
        now = datetime.now(timezone.utc).isoformat()
        if not program.update_available:
            if program.last_update_status == "check_failed":
                raise InstallError(program.last_error_message or "Update check failed.")
            raise InstallError("No update is currently available for this program.")

        # Work on a snapshot so the UI never sees a half-updated record.
        with self._lock:
            snapshot = copy.deepcopy(self._get_program_no_lock(program_id))
        repo_full_name = snapshot.repo_full_name
        channel = snapshot.update_policy.channel
        asset_hint = (
            snapshot.installed_asset_name
            or snapshot.asset_name
            or snapshot.update_policy.asset_selection_override
        )

        running = fsops.find_running_processes(Path(snapshot.install_dir))
        if running:
            raise ProgramInUseError(fsops.in_use_message("update", snapshot.name, running), running)

        github = self._get_github()
        release = github.get_latest_release(repo_full_name, include_prereleases=channel == "prerelease")
        if not release:
            raise InstallError("Could not load release details for update install.")

        asset = PortableInstaller.pick_portable_asset(release.assets, asset_name_hint=asset_hint)
        if not asset:
            raise InstallError("No suitable portable asset was found for the update.")

        try:
            with self._lock:
                installer = self.installer
            updated = installer.download_and_install_release(snapshot, release, asset, progress_callback=progress_callback)
            with self._lock:
                # Copy the new install state onto the live record, keeping anything
                # the user edited meanwhile (name, notes, policy...).
                program = self._get_program_no_lock(program_id)
                for field_name in _INSTALL_STATE_FIELDS:
                    setattr(program, field_name, getattr(updated, field_name))
                program.last_checked_at = now
                program.last_update_attempt_at = now
                program.last_update_status = "updated"
                program.last_error_message = None
                program.last_error_at = None
                self.storage.save_programs(self.programs)
                result = copy.deepcopy(program)
        except KeyError:
            raise
        except Exception as exc:
            with self._lock:
                try:
                    program = self._get_program_no_lock(program_id)
                except KeyError:
                    raise KeyError(program_id) from exc
                program.last_update_status = "update_failed"
                program.last_error_message = str(exc)
                program.last_error_at = now
                self.storage.save_programs(self.programs)
            raise

        if progress_callback:
            progress_callback(100, f"Updated {result.name}")
        return result

    @staticmethod
    def _normalize_version(v: str) -> str:
        """Strip common tag prefixes so ``v1.0.0`` compares equal to ``1.0.0``.

        Returns the original (lowered, stripped) string when stripping the
        prefix would leave an empty result — e.g. input ``"v"`` stays ``"v"``
        rather than becoming ``""``.
        """
        original = v.strip().lower()
        if not original:
            return original
        # Strip leading "v", "ver", "version", or "release" prefixes.
        for prefix in ("version", "release", "ver", "v"):
            if original.startswith(prefix):
                stripped = original[len(prefix):].lstrip(" .-")
                return stripped if stripped else original
        return original

    @staticmethod
    def _is_update_available(program: ManagedProgram, remote_version: str | None, remote_asset_name: str | None) -> bool:
        current_version = AppController._normalize_version(program.version or "")
        norm_remote = AppController._normalize_version(remote_version or "")
        current_asset = (program.asset_name or program.installed_asset_name or "").strip().lower()
        remote_asset = (remote_asset_name or "").strip().lower()

        if norm_remote and norm_remote != current_version:
            return True
        if remote_asset and current_asset and remote_asset != current_asset and not norm_remote:
            return True
        return False

    def _upsert_program(self, program: ManagedProgram) -> None:
        with self._lock:
            existing = self._program_index.get(program.program_id)
            if existing is not None:
                idx = self.programs.index(existing)
                self.programs[idx] = program
            else:
                self.programs.append(program)
            self._program_index[program.program_id] = program
            self.storage.save_programs(self.programs)

    def _taken_dirs_no_lock(self) -> dict[str, str]:
        return {normalize_dir(p.install_dir): p.program_id for p in self.programs}

    def _get_program_no_lock(self, program_id: str) -> ManagedProgram:
        try:
            return self._program_index[program_id]
        except KeyError:
            raise KeyError(f"Program not found: {program_id}")
