from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from .github_client import GitHubClient, GitHubRepo
from .installer import InstallError, PortableInstaller
from .models import AppSettings, ManagedProgram, UpdatePolicy
from .storage import Storage

ProgressCallback = Callable[[int, str], None] | None


@dataclass
class InstallOutcome:
    """Returned by controller install methods to carry both the program and
    any advisory flags the UI should act on."""
    program: ManagedProgram
    is_likely_installer: bool = False


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
            self.github = GitHubClient(token=self.settings.github_token)
            self.installer = PortableInstaller(self.settings, self.github)

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
        result = installer.install_from_repo(repo_full_name, channel=channel, progress_callback=progress_callback)
        self._upsert_program(result.program)
        return InstallOutcome(program=result.program, is_likely_installer=result.is_likely_installer)

    def install_from_url(
        self,
        url: str,
        display_name: str | None = None,
        progress_callback: ProgressCallback = None,
    ) -> InstallOutcome:
        with self._lock:
            installer = self.installer
        result = installer.install_from_url(url, display_name=display_name, progress_callback=progress_callback)
        self._upsert_program(result.program)
        return InstallOutcome(program=result.program, is_likely_installer=result.is_likely_installer)

    def run_program(self, program_id: str) -> ManagedProgram:
        # Take a snapshot of the program under the lock, then release before
        # launching so a UAC prompt doesn't block the entire controller.
        with self._lock:
            program = self._get_program_no_lock(program_id)
            snapshot = copy.deepcopy(program)
        PortableInstaller.run_program(snapshot)
        with self._lock:
            program = self._get_program_no_lock(program_id)
            program.last_run_at = datetime.now(timezone.utc).isoformat()
            self.storage.save_programs(self.programs)
            return program

    def remove_program(self, program_id: str, delete_files: bool = False) -> None:
        with self._lock:
            removed = self._program_index.pop(program_id, None)
            install_dir = removed.install_dir if removed else None
            self.programs = [p for p in self.programs if p.program_id != program_id]
            self.storage.save_programs(self.programs)
            # Delete inside the lock so another thread can't re-install to the
            # same directory between the registry save and the file deletion.
            if delete_files and install_dir:
                try:
                    shutil.rmtree(Path(install_dir), ignore_errors=False)
                except OSError as exc:
                    raise InstallError(
                        f"Program was removed from the registry, but its files "
                        f"could not be deleted: {exc}"
                    )

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
        with self._lock:
            return list(self.programs)

    def update_settings(self, settings: AppSettings) -> None:
        with self._lock:
            self.settings = settings
            self.storage.save_settings(settings)
            self.github = GitHubClient(token=self.settings.github_token)
            self.installer = PortableInstaller(self.settings, self.github)
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
            return program

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
        return program

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

        # Read all state needed for the download under the lock.
        with self._lock:
            program = self._get_program_no_lock(program_id)
            repo_full_name = program.repo_full_name
            channel = program.update_policy.channel
            asset_hint = (
                program.installed_asset_name
                or program.asset_name
                or program.update_policy.asset_selection_override
            )

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
            installer.download_and_install_release(program, release, asset, progress_callback=progress_callback)
            with self._lock:
                # Re-lookup: program reference may be stale after the download.
                program = self._get_program_no_lock(program_id)
                program.last_checked_at = now
                program.last_update_attempt_at = now
                program.last_update_status = "updated"
                program.last_error_message = None
                program.last_error_at = None
                self.storage.save_programs(self.programs)
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
            progress_callback(100, f"Updated {program.name}")
        return program

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

    def _get_program_no_lock(self, program_id: str) -> ManagedProgram:
        try:
            return self._program_index[program_id]
        except KeyError:
            raise KeyError(f"Program not found: {program_id}")
