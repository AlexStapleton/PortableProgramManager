from __future__ import annotations

import copy
import dataclasses
import logging
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from . import fsops
from .errors import AlreadyUpToDateError, InstallError, ProgramInUseError
from .github_client import GitHubClient, GitHubRelease, GitHubReleaseAsset, GitHubRepo, parse_release_asset_url
from .installer import PortableInstaller, normalize_dir
from .launcher import PERMISSION_PROBLEM_MESSAGE, launch_program
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
class UpdateCheckReport:
    """What a bulk update check found and did, for the UI to report."""
    checked: list[ManagedProgram] = field(default_factory=list)
    newly_available: list[ManagedProgram] = field(default_factory=list)  # found this run, not known before
    downloaded: list[ManagedProgram] = field(default_factory=list)       # "download only": staged, ready to apply
    installed: list[ManagedProgram] = field(default_factory=list)        # "install automatically": applied
    deferred: list[str] = field(default_factory=list)                    # e.g. auto-install skipped, program running
    errors: list[str] = field(default_factory=list)

    @property
    def available_count(self) -> int:
        return sum(1 for p in self.checked if p.update_available)


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


# User customisations that survive reinstalling the same program.
_USER_FIELDS = (
    "name", "notes", "launch_args", "working_directory_override", "run_as_admin",
    "pinned", "tags", "update_policy", "installed_at", "last_run_at",
)
_PENDING_FIELDS = ("pending_update_version", "pending_update_file", "pending_update_asset_name", "pending_update_hash")
_MAX_PARALLEL_REQUESTS = 6


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
        """Return copies of *repos* with latest-release date and Windows-asset flag filled in.

        Lookups run in parallel. When the API rate limit is nearly used up, the
        remaining repos are left unchecked rather than burning the last calls.
        """
        github = self._get_github()
        enriched = [dataclasses.replace(repo) for repo in repos]
        total = len(enriched)
        budget = total
        if github.rate_limit_remaining is not None:
            budget = max(0, min(total, github.rate_limit_remaining - 5))
        targets = enriched[:budget]
        done = 0
        with ThreadPoolExecutor(max_workers=_MAX_PARALLEL_REQUESTS) as pool:
            futures = {pool.submit(github.get_latest_release_info, repo.full_name): repo for repo in targets}
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    repo.latest_release_at, repo.has_windows_release = future.result()
                except Exception:
                    repo.latest_release_at, repo.has_windows_release = "", None
                done += 1
                if progress_callback:
                    progress_callback(int(done / max(total, 1) * 100), f"Fetching release info ({done}/{total})")
        if progress_callback:
            skipped = total - len(targets)
            note = f" ({skipped} skipped to stay under GitHub's rate limit)" if skipped else ""
            progress_callback(100, f"Fetched release info for {len(targets)} repos{note}")
        return enriched

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
        # A GitHub release URL carries its tag, so the installed version is known
        # (otherwise the first update check would always report an update).
        parsed = parse_release_asset_url(url)
        if parsed and not result.program.version:
            result.program.version = parsed[1]
            result.program.latest_upstream_version = parsed[1]
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
            self._raise_if_permission_problem(program)
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

    def find_permission_problems(self, progress_callback: ProgressCallback = None) -> dict[str, list[str]]:
        """Map program id → paths its user can't open (see :func:`fsops.repair_permissions`)."""
        with self._lock:
            targets = [(p.program_id, Path(p.install_dir)) for p in self.programs]
        found: dict[str, list[str]] = {}
        for program_id, folder in targets:
            problems = fsops.find_permission_problems(folder, limit=3)
            if problems:
                found[program_id] = [str(p) for p in problems]
        return found

    def repair_permissions(self, program_id: str, progress_callback: ProgressCallback = None) -> ManagedProgram:
        """Give the user's account normal access to every file of a program again."""
        with self._lock:
            snapshot = copy.deepcopy(self._get_program_no_lock(program_id))
        if progress_callback:
            progress_callback(30, f"Repairing permissions for {snapshot.name}...")
        if not fsops.repair_permissions(Path(snapshot.install_dir)):
            raise InstallError(
                f"Some files of {snapshot.name} are still not accessible. "
                "Try running Portable Program Manager as administrator once and repair again."
            )
        return snapshot

    def _raise_if_permission_problem(self, program: ManagedProgram) -> None:
        if fsops.find_permission_problems(Path(program.install_dir), limit=1):
            raise InstallError(PERMISSION_PROBLEM_MESSAGE.format(name=program.name))

    def cleanup_stale_temp_dirs(self, progress_callback: ProgressCallback = None) -> int:
        """Delete leftover staging/backup/trash folders from interrupted operations."""
        with self._lock:
            roots = {normalize_dir(self.settings.install_root): Path(self.settings.install_root)}
            for program in self.programs:
                parent = Path(program.install_dir).parent
                roots.setdefault(normalize_dir(parent), parent)
        return sum(fsops.cleanup_stale_temp_dirs(root) for root in roots.values())

    def set_pinned(self, program_id: str, pinned: bool) -> ManagedProgram:
        """Pin/unpin a program in the tray's quick-launch menu."""
        with self._lock:
            program = self._get_program_no_lock(program_id)
            program.pinned = bool(pinned)
            self.storage.save_programs(self.programs)
            return copy.deepcopy(program)

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

    def check_for_updates(
        self,
        program_id: str,
        progress_callback: ProgressCallback = None,
        *,
        save: bool = True,
    ) -> ManagedProgram:
        """Check one program against its latest GitHub release and record the result.

        Never raises for network/API problems; those are stored on the program
        as ``check_failed``. Pass ``save=False`` when batching many checks.
        """
        with self._lock:
            snapshot = copy.deepcopy(self._get_program_no_lock(program_id))

        now = datetime.now(timezone.utc).isoformat()
        if progress_callback:
            progress_callback(15, f"Checking updates for {snapshot.name}")

        if snapshot.source_type not in {"github_repo", "direct_url"} or not snapshot.repo_full_name:
            with self._lock:
                program = self._get_program_no_lock(program_id)
                program.last_update_attempt_at = now
                program.last_checked_at = now
                program.last_update_status = "unsupported_source"
                program.last_error_message = "This program does not have a GitHub repository source for update checks."
                program.last_error_at = now
                if save:
                    self.storage.save_programs(self.programs)
            if progress_callback:
                progress_callback(100, f"Update checks are not supported for {snapshot.name}")
            return self.get_program(program_id)

        try:
            release, asset = self._fetch_update_target(snapshot)
            with self._lock:
                # Re-lookup: program may have been removed while the lock was released.
                program = self._get_program_no_lock(program_id)
                program.last_update_attempt_at = now
                program.last_checked_at = now
                program.latest_upstream_version = release.tag_name
                program.latest_upstream_published_at = release.published_at
                program.last_error_message = None
                program.last_error_at = None
                newer = self._is_update_available(program, release.tag_name, asset.name if asset else None)
                if newer and asset is None:
                    program.update_available = False
                    program.update_available_asset_name = None
                    program.last_update_status = "no_compatible_asset"
                    program.last_error_message = (
                        f"Release {release.tag_name} has no Windows build this app can install."
                    )
                elif newer:
                    if not program.update_available or program.update_available_asset_name != asset.name:
                        program.last_update_found_at = now
                    program.update_available = True
                    program.update_available_asset_name = asset.name
                    program.last_update_status = "update_available"
                else:
                    program.update_available = False
                    program.update_available_asset_name = None
                    program.last_update_status = "no_update"
                if save:
                    self.storage.save_programs(self.programs)
        except KeyError:
            raise
        except Exception as exc:
            log.info("Update check for %s failed: %s", snapshot.name, exc)
            with self._lock:
                program = self._get_program_no_lock(program_id)
                program.last_update_attempt_at = now
                program.last_checked_at = now
                program.last_update_status = "check_failed"
                program.last_error_message = str(exc)
                program.last_error_at = now
                if save:
                    self.storage.save_programs(self.programs)

        if progress_callback:
            progress_callback(100, f"Finished checking {snapshot.name}")
        return self.get_program(program_id)

    def check_for_updates_all(self, progress_callback: ProgressCallback = None) -> UpdateCheckReport:
        with self._lock:
            program_ids = [program.program_id for program in self.programs if program.update_policy.check_enabled]
        if not program_ids:
            if progress_callback:
                progress_callback(100, "No managed programs are enabled for update checks")
            return UpdateCheckReport()
        return self._run_checks(program_ids, progress_callback)

    def _run_checks(self, program_ids: list[str], progress_callback: ProgressCallback) -> UpdateCheckReport:
        """Check *program_ids* in parallel, save once, then apply each program's update mode."""
        with self._lock:
            before = {
                pid: (self._program_index[pid].update_available, self._program_index[pid].latest_upstream_version)
                for pid in program_ids if pid in self._program_index
            }
        report = UpdateCheckReport()
        total = len(before)
        done = 0
        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_REQUESTS, max(total, 1))) as pool:
            futures = [pool.submit(self.check_for_updates, pid, None, save=False) for pid in before]
            for future in as_completed(futures):
                done += 1
                try:
                    report.checked.append(future.result())
                except KeyError:
                    pass  # removed while checking
                if progress_callback:
                    progress_callback(int(done / max(total, 1) * 80), f"Checking updates ({done}/{total})")
        with self._lock:
            self.storage.save_programs(self.programs)

        for program in report.checked:
            if program.update_available:
                was_available, old_version = before.get(program.program_id, (False, None))
                if not was_available or old_version != program.latest_upstream_version:
                    report.newly_available.append(program)
            if program.last_update_status == "check_failed" and program.last_error_message:
                report.errors.append(f"{program.name}: {program.last_error_message}")

        self._apply_update_modes(report, progress_callback)
        if progress_callback:
            progress_callback(
                100, f"Checked {len(report.checked)} program(s). {report.available_count} update(s) available"
            )
        return report

    def _apply_update_modes(self, report: UpdateCheckReport, progress_callback: ProgressCallback) -> None:
        """Act on found updates according to each program's update mode."""
        for program in list(report.checked):
            if not program.update_available:
                continue
            mode = program.update_policy.update_mode
            try:
                if mode == "download_only" and program.pending_update_version != program.latest_upstream_version:
                    if progress_callback:
                        progress_callback(85, f"Downloading update for {program.name}")
                    report.downloaded.append(self.download_pending_update(program.program_id))
                elif mode == "install_automatically":
                    if progress_callback:
                        progress_callback(90, f"Installing update for {program.name}")
                    report.installed.append(self.install_available_update(program.program_id))
            except ProgramInUseError:
                report.deferred.append(f"{program.name} is running, so its update will be installed later.")
            except KeyError:
                pass
            except Exception as exc:
                report.errors.append(f"{program.name}: {exc}")
        if report.installed:
            installed_ids = {p.program_id for p in report.installed}
            report.checked = [
                next((i for i in report.installed if i.program_id == p.program_id), p) if p.program_id in installed_ids else p
                for p in report.checked
            ]

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
    ) -> UpdateCheckReport:
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
        if not due_ids:
            if progress_callback:
                progress_callback(100, "No updates are due at this time")
            return UpdateCheckReport()
        return self._run_checks(due_ids, progress_callback)

    def _fetch_update_target(self, program: ManagedProgram) -> tuple[GitHubRelease, GitHubReleaseAsset | None]:
        """Fetch the latest release for *program* and pick the asset to install from it."""
        if not program.repo_full_name:
            raise InstallError("This program has no GitHub repository to update from.")
        release = self._get_github().get_latest_release(
            program.repo_full_name, include_prereleases=program.update_policy.channel == "prerelease"
        )
        if not release:
            raise InstallError("No GitHub release was found for this program.")
        hint = (
            program.update_policy.asset_selection_override
            or program.installed_asset_name
            or program.asset_name
        )
        asset = PortableInstaller.pick_portable_asset(release.assets, asset_name_hint=hint)
        return release, asset

    def _pending_dir(self, program_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", program_id)[:80]
        return Path(self.settings.download_cache) / "pending" / safe

    def download_pending_update(self, program_id: str, progress_callback: ProgressCallback = None) -> ManagedProgram:
        """Download the latest release asset now and keep it ready to apply ("download only" mode)."""
        with self._lock:
            snapshot = copy.deepcopy(self._get_program_no_lock(program_id))
            installer = self.installer
            pending_dir = self._pending_dir(program_id)
        release, asset = self._fetch_update_target(snapshot)
        if asset is None:
            raise InstallError(f"Release {release.tag_name} has no Windows build this app can install.")
        downloaded, digest = installer._download(
            asset.download_url, asset.name, progress_callback=progress_callback,
            expected_sha256=getattr(asset, "digest", ""),
        )
        try:
            shutil.rmtree(pending_dir, ignore_errors=True)
            pending_dir.mkdir(parents=True, exist_ok=True)
            target = pending_dir / asset.name
            shutil.move(str(downloaded), str(target))
        finally:
            installer._discard_download(downloaded)
        with self._lock:
            program = self._get_program_no_lock(program_id)
            program.pending_update_version = release.tag_name
            program.pending_update_file = str(target)
            program.pending_update_asset_name = asset.name
            program.pending_update_hash = digest
            self.storage.save_programs(self.programs)
            return copy.deepcopy(program)

    def _clear_pending_update_no_lock(self, program: ManagedProgram) -> None:
        if program.pending_update_file:
            shutil.rmtree(self._pending_dir(program.program_id), ignore_errors=True)
        for name in _PENDING_FIELDS:
            setattr(program, name, None)

    def install_available_update(self, program_id: str, progress_callback: ProgressCallback = None) -> ManagedProgram:
        """Install the latest release over the program, all-or-nothing.

        Uses an already-downloaded pending update when it matches the latest
        release, so "download only" mode doesn't download twice.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            snapshot = copy.deepcopy(self._get_program_no_lock(program_id))

        running = fsops.find_running_processes(Path(snapshot.install_dir))
        if running:
            raise ProgramInUseError(fsops.in_use_message("update", snapshot.name, running), running)
        self._raise_if_permission_problem(snapshot)

        if progress_callback:
            progress_callback(5, f"Looking up the latest release of {snapshot.name}")
        release, asset = self._fetch_update_target(snapshot)
        if asset is None:
            raise InstallError(f"Release {release.tag_name} has no Windows build this app can install.")
        if not self._is_update_available(snapshot, release.tag_name, asset.name):
            self.check_for_updates(program_id)
            raise InstallError(f"{snapshot.name} is already up to date ({snapshot.version or release.tag_name}).")

        prefetched: tuple[Path, str] | None = None
        if (
            snapshot.pending_update_version == release.tag_name
            and snapshot.pending_update_asset_name == asset.name
            and snapshot.pending_update_file
            and Path(snapshot.pending_update_file).is_file()
        ):
            prefetched = (Path(snapshot.pending_update_file), snapshot.pending_update_hash or "")

        with self._lock:
            installer = self.installer
        try:
            try:
                updated = installer.download_and_install_release(
                    snapshot, release, asset, progress_callback=progress_callback, prefetched=prefetched
                )
            except AlreadyUpToDateError:
                # Same bytes as what's installed: the release is just re-tagged.
                # Record it as current instead of failing (and re-flagging) forever.
                with self._lock:
                    program = self._get_program_no_lock(program_id)
                    program.version = release.tag_name
                    program.latest_upstream_version = release.tag_name
                    program.latest_upstream_published_at = release.published_at
                    program.update_available = False
                    program.update_available_asset_name = None
                    program.last_checked_at = now
                    program.last_update_attempt_at = now
                    program.last_update_status = "no_update"
                    program.last_error_message = None
                    self._clear_pending_update_no_lock(program)
                    self.storage.save_programs(self.programs)
                    return copy.deepcopy(program)
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
                self._clear_pending_update_no_lock(program)
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

    def find_managed_program(self, repo_full_name: str | None = None, url: str | None = None) -> ManagedProgram | None:
        """Return the managed program installed from this repo or URL, if any (for reinstall prompts)."""
        with self._lock:
            for program in self.programs:
                if repo_full_name and (program.repo_full_name or "").lower() == repo_full_name.lower() \
                        and program.source_type == "github_repo":
                    return copy.deepcopy(program)
                if url and program.source_value == url:
                    return copy.deepcopy(program)
        return None

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
        """Add *program*, or replace the install state of an existing one with the same id.

        On reinstall the user's own customisations (name, notes, launch
        options, update policy...) are kept, and so is a custom launch path
        that still exists.
        """
        with self._lock:
            existing = self._program_index.get(program.program_id)
            if existing is not None:
                for name in _USER_FIELDS:
                    setattr(program, name, copy.deepcopy(getattr(existing, name)))
                if existing.launch_path and existing.launch_path != program.launch_path \
                        and Path(existing.launch_path).is_file():
                    program.launch_path = existing.launch_path
                self._clear_pending_update_no_lock(existing)
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
