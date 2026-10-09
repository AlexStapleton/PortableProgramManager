from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse

import requests

from . import fsops
from .errors import InstallError, ProgramInUseError
from .github_client import USER_AGENT, GitHubClient, GitHubRelease, GitHubReleaseAsset
from .models import AppSettings, ManagedProgram, UpdatePolicy

__all__ = ["InstallError", "ProgramInUseError", "InstallResult", "PortableInstaller"]

log = logging.getLogger(__name__)

PORTABLE_EXTENSIONS = {".exe", ".zip", ".7z", ".bat", ".cmd"}
ARCHIVE_EXTENSIONS = {".zip", ".7z"}
EXECUTABLE_EXTENSIONS = {".exe", ".bat", ".cmd"}
SCRIPT_EXTENSIONS = {".ps1"}  # runnable via powershell, lower priority than native executables
ProgressCallback = Callable[[int, str], None] | None

# Windows reserved device names that cannot be used as file/directory names.
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

# Name tokens that mark an executable as a setup/installer (see looks_like_installer).
_INSTALLER_TOKENS = frozenset({"setup", "installer", "install"})
_INSTALLER_STEM_RE = re.compile(r"^(setup|install(er)?)[\W_]*$", re.IGNORECASE)

# Files that are never the main program of a portable app (updaters, uninstallers,
# crash reporters, runtime redistributables...). Matched with search() against the stem.
_HELPER_STEM_RES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"^unins\d*$", r"uninst", r"uninstall", r"^setup", r"install",
    r"^update(r)?$", r"updater", r"autoupdate", r"crash", r"helper", r"redist",
    r"vc_?redist", r"dotnet", r"elevat", r"notification", r"squirrel",
    r"createdump", r"^7za?$", r"^cmd$", r"^vcruntime",
))

# OS / architecture markers used by pick_portable_asset.
_NON_WINDOWS_MARKERS = (
    "macos", "mac-os", "osx", "darwin", "linux", "ubuntu", "debian", "fedora",
    "freebsd", "android", "-mac-", "-mac.", "_mac_", "_mac.", ".app.",
    ".dmg", ".deb", ".rpm", ".appimage", ".apk", ".pkg",
)
_NON_WINDOWS_IOS_RE = re.compile(r"(?<![a-z0-9])ios-")
_ARCH_ARM64_RE = re.compile(r"(?<![a-z0-9])(?:arm64|aarch64)(?![0-9])")
_ARCH_X64_RE = re.compile(r"(?:(?<![a-z0-9])(?:x64|x86[-_]?64|amd64)(?![0-9])|win64)")
_ARCH_X86_RE = re.compile(r"(?<![a-z0-9])(?:x86(?![-_]?64)|win32|i[3-6]86|ia32)(?![0-9])")
_WIN_TOKEN_RE = re.compile(r"(?<![a-z])win(?![a-z])|windows")
_INSTALLER_NAME_RE = re.compile(r"setup|installer|install|(?<![a-z])msi(?![a-z])")
_VERSION_RE = re.compile(r"\d+(\.\d+)*")

# Keeps console programs we call (7z.exe) from flashing a window in the GUI build.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class InstallResult:
    program: ManagedProgram
    downloaded_file: Path
    is_likely_installer: bool = False


class PortableInstaller:
    def __init__(self, settings: AppSettings, github: GitHubClient) -> None:
        self.settings = settings
        self.github = github

    def install_from_repo(
        self,
        repo_full_name: str,
        channel: str = "latest_release",
        progress_callback: ProgressCallback = None,
        taken_dirs: dict[str, str] | None = None,
    ) -> InstallResult:
        if progress_callback:
            progress_callback(5, f"Loading repository {repo_full_name}")
        repo = self.github.get_repo(repo_full_name)
        release = self.github.get_latest_release(repo_full_name, include_prereleases=channel == "prerelease")
        if not release:
            raise InstallError("This repository does not have a latest release.")

        asset = self.pick_portable_asset(release.assets)
        if not asset:
            raise InstallError("No portable release asset was found. Try installing from a direct asset URL.")

        program_id = f"repo::{repo.full_name.lower()}"
        install_dir = self._choose_install_dir(self._safe_name(repo.name), program_id, taken_dirs)
        fsops.ensure_writable_dir(Path(self.settings.install_root))
        downloaded_file, downloaded_hash = self._download(
            asset.download_url, asset.name, progress_callback=progress_callback,
            expected_sha256=_expected_sha256(asset),
        )
        try:
            launch_path = self._materialize_asset(
                downloaded_file, install_dir, progress_callback=progress_callback, name_hints=[repo.name],
            )
        finally:
            self._discard_download(downloaded_file)

        is_installer = self.looks_like_installer(launch_path, install_dir)
        # If the asset is an installer/setup, force update policy to manual
        # since the installed program likely manages its own updates.
        if is_installer:
            update_policy = UpdatePolicy(
                check_enabled=False,
                update_mode="manual",
                schedule_type="app_start",
                interval_hours=self.settings.default_update_interval_hours,
                channel=channel,
            )
        else:
            update_policy = UpdatePolicy(
                update_mode=self.settings.default_update_mode,
                schedule_type=self.settings.default_update_schedule_type,
                interval_hours=self.settings.default_update_interval_hours,
                channel=channel,
            )

        program = ManagedProgram(
            program_id=program_id,
            name=repo.name,
            source_type="github_repo",
            source_value=repo.full_name,
            install_dir=str(install_dir),
            launch_path=str(launch_path) if launch_path else None,
            version=release.tag_name,
            asset_name=asset.name,
            repo_full_name=repo.full_name,
            homepage_url=repo.homepage or repo.html_url,
            notes=repo.description,
            source_id=f"github:{repo.full_name.lower()}",
            source_kind="repository",
            installed_asset_url=asset.download_url,
            installed_asset_name=asset.name,
            installed_asset_size=asset.size,
            installed_hash=f"sha256:{downloaded_hash}",
            update_policy=update_policy,
            latest_upstream_version=release.tag_name,
            latest_upstream_published_at=release.published_at,
            last_checked_at=release.published_at,
            last_update_status="installed",
        )
        if progress_callback:
            progress_callback(100, f"Installed {program.name}")
        return InstallResult(program=program, downloaded_file=downloaded_file, is_likely_installer=is_installer)

    def install_from_url(
        self,
        url: str,
        display_name: str | None = None,
        progress_callback: ProgressCallback = None,
        taken_dirs: dict[str, str] | None = None,
    ) -> InstallResult:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise InstallError("Only https:// URLs are supported. HTTP downloads are rejected to prevent man-in-the-middle attacks.")
        if not parsed.netloc:
            raise InstallError("The URL is missing a hostname.")
        file_name = self._sanitize_filename(Path(parsed.path).name)
        if not file_name or file_name == "download":
            raise InstallError("Could not infer a valid file name from the URL.")

        suffix = Path(file_name).suffix.lower()
        if suffix not in PORTABLE_EXTENSIONS:
            raise InstallError("URL does not point to a supported portable asset type (.exe, .zip, .7z, .bat, .cmd).")

        program_name = self._safe_name(display_name or Path(file_name).stem)
        program_id = f"url::{program_name.lower()}::{hashlib.sha256(url.encode()).hexdigest()[:16]}"
        install_dir = self._choose_install_dir(program_name, program_id, taken_dirs)
        fsops.ensure_writable_dir(Path(self.settings.install_root))
        downloaded_file, downloaded_hash = self._download(url, file_name, progress_callback=progress_callback)
        try:
            launch_path = self._materialize_asset(
                downloaded_file, install_dir, progress_callback=progress_callback, name_hints=[program_name],
            )
        finally:
            self._discard_download(downloaded_file)

        repo_full_name = self.github.infer_repo_from_asset_url(url)
        is_installer = self.looks_like_installer(launch_path, install_dir)
        if is_installer:
            update_policy = UpdatePolicy(
                check_enabled=False,
                update_mode="manual",
            )
        else:
            update_policy = UpdatePolicy(
                check_enabled=bool(repo_full_name),
                update_mode=self.settings.default_update_mode,
                schedule_type=self.settings.default_update_schedule_type,
                interval_hours=self.settings.default_update_interval_hours,
            )

        program = ManagedProgram(
            program_id=program_id,
            name=program_name,
            source_type="direct_url",
            source_value=url,
            install_dir=str(install_dir),
            launch_path=str(launch_path) if launch_path else None,
            asset_name=file_name,
            repo_full_name=repo_full_name,
            homepage_url=url,
            source_id=f"url:{url}",
            source_kind="direct_asset",
            installed_asset_url=url,
            installed_asset_name=file_name,
            installed_hash=f"sha256:{downloaded_hash}",
            update_policy=update_policy,
            last_update_status="installed",
        )
        if progress_callback:
            progress_callback(100, f"Installed {program.name}")
        return InstallResult(program=program, downloaded_file=downloaded_file, is_likely_installer=is_installer)

    def download_and_install_release(
        self,
        program: ManagedProgram,
        release: GitHubRelease,
        asset: GitHubReleaseAsset,
        progress_callback: ProgressCallback = None,
    ) -> ManagedProgram:
        """Download *asset* and apply it over *program*'s folder.

        *program* should be a snapshot (not the controller's live object); it is
        updated in place and returned so the caller can commit it under its lock.
        """
        install_dir = Path(program.install_dir)
        downloaded_file, downloaded_hash = self._download(
            asset.download_url, asset.name, progress_callback=progress_callback,
            expected_sha256=_expected_sha256(asset),
        )
        try:
            # Verify the download differs from what is already installed.
            # An identical hash may indicate a replayed or stale download.
            new_hash = f"sha256:{downloaded_hash}"
            if program.installed_hash and program.installed_hash == new_hash:
                log.warning(
                    "Update download for %s has identical hash to the installed version (%s); skipping.",
                    program.name, new_hash,
                )
                raise InstallError(
                    "The downloaded update is identical to the currently installed version "
                    "(same SHA-256 hash). The update was skipped."
                )
            name_hints = [
                hint for hint in (
                    program.name,
                    Path(program.launch_path).stem if program.launch_path else "",
                    program.repo_full_name.split("/")[-1] if program.repo_full_name else "",
                )
                if hint
            ]
            launch_path = self._materialize_asset(
                downloaded_file, install_dir, progress_callback=progress_callback,
                name_hints=name_hints, previous_launch_relpath=_launch_relpath(program),
            )
        finally:
            self._discard_download(downloaded_file)

        self._remove_superseded_single_file(program, asset.name, launch_path)
        program.launch_path = str(launch_path) if launch_path else program.launch_path
        program.installed_hash = new_hash
        program.version = release.tag_name or program.version
        program.asset_name = asset.name
        program.installed_asset_url = asset.download_url
        program.installed_asset_name = asset.name
        program.installed_asset_size = asset.size
        program.latest_upstream_version = release.tag_name
        program.latest_upstream_published_at = release.published_at
        program.update_available = False
        program.update_available_asset_name = None
        program.last_update_status = "updated"
        program.last_error_message = None
        if progress_callback:
            progress_callback(100, f"Updated {program.name}")
        return program

    @staticmethod
    def _remove_superseded_single_file(program: ManagedProgram, new_asset_name: str, new_launch: Path | None) -> None:
        """A single-file asset whose name carries the version (tool-1.2.exe → tool-1.3.exe)
        would otherwise leave every old copy behind."""
        old_name = program.installed_asset_name or ""
        if (
            not old_name
            or old_name == new_asset_name
            or Path(old_name).suffix.lower() in ARCHIVE_EXTENSIONS
            or Path(new_asset_name).suffix.lower() in ARCHIVE_EXTENSIONS
        ):
            return
        old_file = Path(program.install_dir) / old_name
        if new_launch is not None and fsops.is_within(old_file, new_launch):
            return
        try:
            if old_file.is_file():
                fsops.clear_readonly(old_file)
                old_file.unlink()
        except OSError:
            log.info("Couldn't remove superseded file %s", old_file, exc_info=True)

    def _choose_install_dir(self, base_name: str, program_id: str, taken_dirs: dict[str, str] | None) -> Path:
        """Pick ``<install root>/<name>``, adding `` (2)``, `` (3)``… when another
        managed program already owns that folder.

        *taken_dirs* maps ``os.path.normcase``-normalised absolute folder paths
        to the id of the program that owns them.
        """
        root = Path(self.settings.install_root)
        taken = taken_dirs or {}
        candidate = root / base_name
        counter = 2
        while (owner := taken.get(normalize_dir(candidate))) is not None and owner != program_id:
            candidate = root / f"{base_name} ({counter})"
            counter += 1
        return candidate

    def _download(
        self,
        url: str,
        file_name: str,
        progress_callback: ProgressCallback = None,
        expected_sha256: str = "",
    ) -> tuple[Path, str]:
        """Download *url* into a fresh folder in the download cache.

        Returns ``(path, sha256_hex)``. Every download gets its own folder so two
        concurrent downloads of identically named assets can't collide. Call
        :meth:`_discard_download` when done with the file.

        If *expected_sha256* is given and the download doesn't match it, the
        download folder is removed and :class:`InstallError` is raised.
        """
        download_dir = Path(self.settings.download_cache) / f"dl_{uuid.uuid4().hex[:12]}"
        download_dir.mkdir(parents=True, exist_ok=True)
        destination = download_dir / file_name
        if progress_callback:
            progress_callback(10, f"Starting download: {file_name}")
        sha256 = hashlib.sha256()
        total = downloaded = 0
        encoded = False
        try:
            with requests.Session() as session:
                session.headers["User-Agent"] = USER_AGENT
                # Limit redirects to prevent abuse and validate the final URL.
                session.max_redirects = 10
                with session.get(url, stream=True, timeout=(15, 120)) as response:
                    response.raise_for_status()
                    # Validate that the final URL (after redirects) still uses HTTPS.
                    final_url = response.url
                    if not final_url.startswith("https://"):
                        raise InstallError(
                            f"Download aborted: server redirected to a non-HTTPS URL ({final_url[:80]})."
                        )
                    total = int(response.headers.get("Content-Length", "0") or 0)
                    # requests transparently decompresses gzip/deflate, so the byte
                    # count can't be compared with Content-Length in that case.
                    encoded = bool(response.headers.get("Content-Encoding"))
                    with destination.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 256):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            sha256.update(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total > 0:
                                percent = 10 + int((downloaded / total) * 70)
                                progress_callback(
                                    min(percent, 80),
                                    f"Downloading {file_name} ({_format_mb(downloaded)} of {_format_mb(total)})",
                                )
            if total > 0 and not encoded and downloaded != total:
                raise InstallError(
                    f"Download size mismatch for {file_name}: expected {total} bytes, got {downloaded}."
                )
            if downloaded == 0:
                raise InstallError(f"Downloaded file {file_name} is empty (0 bytes).")
            expected = _normalise_sha256(expected_sha256)
            if expected and expected != sha256.hexdigest():
                raise InstallError(
                    f"Integrity check failed for {file_name}: the download doesn't match the SHA-256 "
                    "published by GitHub. Nothing was installed."
                )
        except BaseException:
            # Remove the incomplete file so it doesn't get mistaken for a valid download.
            shutil.rmtree(download_dir, ignore_errors=True)
            raise
        if progress_callback:
            progress_callback(85, f"Download complete: {file_name}")
        return destination, sha256.hexdigest()

    def _discard_download(self, path: Path) -> None:
        """Remove a downloaded file (and its private folder) from the cache."""
        parent = path.parent
        if parent.name.startswith("dl_") and fsops.is_within(parent, self.settings.download_cache):
            shutil.rmtree(parent, ignore_errors=True)
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # non-critical — stale cache file is harmless

    def _materialize_asset(
        self,
        downloaded_file: Path,
        install_dir: Path,
        progress_callback: ProgressCallback = None,
        name_hints: Sequence[str] = (),
        previous_launch_relpath: str | None = None,
    ) -> Path | None:
        """Unpack/copy *downloaded_file* into *install_dir* and return the launch file.

        The new content is prepared in a staging folder next to *install_dir*
        and then applied in one all-or-nothing step (see :mod:`fsops`). The
        launch file is chosen from the *new* content only, so an update can't
        end up pointing at a file left over from the previous version.

        *name_hints* are passed to :meth:`guess_launch_executable`. When
        *previous_launch_relpath* (the old launch file, relative to *install_dir*)
        still exists in the new content, that file is used instead of guessing.
        """
        suffix = downloaded_file.suffix.lower()
        if progress_callback:
            progress_callback(88, f"Preparing {downloaded_file.name}")

        staging = fsops.make_staging_dir(install_dir.parent)
        try:
            if suffix == ".zip":
                try:
                    with zipfile.ZipFile(downloaded_file, "r") as archive:
                        self._safe_extract_zip(archive, staging)
                except zipfile.BadZipFile as exc:
                    raise InstallError(f"Corrupt or invalid ZIP archive: {exc}")
            elif suffix == ".7z":
                self._extract_7z(downloaded_file, staging)
            else:
                shutil.copy2(downloaded_file, staging / downloaded_file.name)

            if suffix in ARCHIVE_EXTENSIONS:
                fsops.flatten_single_subdir(staging)
                if not any(staging.iterdir()):
                    raise InstallError(f"The archive {downloaded_file.name} is empty.")
                if progress_callback:
                    progress_callback(93, f"Extracted {downloaded_file.name}")
                launch_in_staging = _previous_launch_in_staging(staging, previous_launch_relpath)
                if launch_in_staging is None:
                    launch_in_staging = self.guess_launch_executable(staging, name_hints)
            else:
                launch_in_staging = staging / downloaded_file.name

            relative_launch = launch_in_staging.relative_to(staging) if launch_in_staging else None
            if progress_callback:
                progress_callback(96, f"Installing into {install_dir}")
            fsops.apply_staged_tree(staging, install_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return install_dir / relative_launch if relative_launch else None

    @staticmethod
    def guess_launch_executable(install_dir: Path, name_hints: Sequence[str] = ()) -> Path | None:
        """Pick the file most likely to be the program's main executable.

        Executables come before scripts. Within each group: helpers (updaters,
        uninstallers, crash reporters...) last, then files whose name matches a
        hint in *name_hints*, then the shallowest, then the largest file.
        """
        candidates: list[Path] = []
        for root, _, files in os.walk(install_dir):
            for file_name in files:
                path = Path(root) / file_name
                suffix = path.suffix.lower()
                if suffix in EXECUTABLE_EXTENSIONS or suffix in SCRIPT_EXTENSIONS:
                    candidates.append(path)
        if not candidates:
            return None

        hints = [key for key in (_normalise_name(h) for h in name_hints) if key]

        def _sort_key(p: Path) -> tuple:
            stem_key = _normalise_name(p.stem)
            if hints and stem_key in hints:
                hint_rank = 2
            elif hints and any(stem_key and (stem_key in h or h in stem_key) for h in hints):
                hint_rank = 1
            else:
                hint_rank = 0
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            return (
                p.suffix.lower() in SCRIPT_EXTENSIONS,
                any(rx.search(p.stem) for rx in _HELPER_STEM_RES),
                -hint_rank,
                len(p.relative_to(install_dir).parts),
                -size,
                len(p.name),
            )

        return min(candidates, key=_sort_key)

    @staticmethod
    def looks_like_installer(launch_path: Path | None, install_dir: Path) -> bool:
        """Return True if the resolved launch executable looks like a setup/installer.

        Heuristics, on name *tokens* (so ``Updater`` or ``uninstall`` don't count):
        - The file name has a token ``setup``, ``installer`` or ``install``
          (``MyAppSetup`` -> my, app, setup).
        - The install folder holds only one .exe and its name has such a token.
        """
        if not launch_path:
            return False
        if _is_installer_stem(launch_path.stem):
            return True
        # If there's only one .exe in the entire install dir, check its name too.
        exes = list(install_dir.rglob("*.exe"))
        if len(exes) == 1 and _is_installer_stem(exes[0].stem):
            return True
        return False

    @staticmethod
    def pick_portable_asset(
        assets: list[GitHubReleaseAsset],
        asset_name_hint: str | None = None,
        machine: str | None = None,
    ) -> GitHubReleaseAsset | None:
        """Choose the Windows build to install, or None if there isn't one.

        Non-Windows assets are dropped, not just penalised. Architecture is a
        strong preference: x64 machines get x64 builds, then unmarked, then
        x86; arm64 machines get arm64, then x64, then unmarked, then x86.
        *machine* defaults to the running machine (``platform.machine()``).
        """
        if not assets:
            return None

        machine_key = (machine if machine is not None else platform.machine()).lower()
        if machine_key in ("arm64", "aarch64"):
            arch_order = ("arm64", "x64", "", "x86")
        elif machine_key in ("amd64", "x86_64", "x64"):
            arch_order = ("x64", "", "x86")
        else:
            arch_order = ("x86", "")
        arch_weight = {arch: (len(arch_order) - idx) * 10_000 for idx, arch in enumerate(arch_order)}

        preferred_patterns = [".zip", "portable", "win64", "win-x64", "windows", "x86_64", ".exe"]
        hint_key = _normalise_name_for_hint(asset_name_hint or "")
        hint_lower = (asset_name_hint or "").lower().strip()
        scored: list[tuple[int, GitHubReleaseAsset]] = []
        for asset in assets:
            name = asset.name.lower()
            suffix = Path(name).suffix.lower()
            if suffix not in PORTABLE_EXTENSIONS:
                continue
            # Drop builds for other operating systems outright.
            if any(marker in name for marker in _NON_WINDOWS_MARKERS) or _NON_WINDOWS_IOS_RE.search(name):
                continue
            arch = _asset_arch(name)
            if arch not in arch_weight:
                continue  # e.g. an arm64-only build on an x64 machine

            score = arch_weight[arch]
            for idx, pattern in enumerate(preferred_patterns[::-1], start=1):
                if pattern in name:
                    score += idx * 10
            if _WIN_TOKEN_RE.search(name):
                score += 30
            if hint_lower:
                if name == hint_lower:
                    score += 1000
                elif hint_lower in name or name in hint_lower:
                    score += 250
                elif hint_key and hint_key == _normalise_name_for_hint(name):
                    # Same asset, new version: "tool-1.2-win64.zip" -> "tool-1.3-win64.zip".
                    score += 500
            if _INSTALLER_NAME_RE.search(name):
                score -= 100
            scored.append((score, asset))

        if not scored:
            return None

        scored.sort(key=lambda item: (item[0], -item[1].size), reverse=True)
        return scored[0][1]

    @staticmethod
    def _find_7zip() -> str | None:
        """Return the path to the 7z command-line executable, or None if not found."""
        found = shutil.which("7z") or shutil.which("7za")
        if found:
            return found
        for candidate in [
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ]:
            if Path(candidate).is_file():
                return candidate
        return None

    @staticmethod
    def _validate_7z_members(archive_path: Path, dest: Path) -> None:
        """Check that no member in the .7z archive would escape *dest*.

        Mirrors the Zip Slip protection applied to ZIP files.  Symlink entries
        are rejected because on Windows 11 with Developer Mode enabled,
        symlinks can be created without admin privileges and could be used to
        write outside the install directory.
        """
        import py7zr  # imported lazily: rarely needed and slow to import

        resolved_dest = dest.resolve()
        try:
            with py7zr.SevenZipFile(archive_path, "r") as archive:
                for entry in archive.list():
                    name = entry.filename
                    if entry.is_symlink:
                        raise InstallError(
                            f"7z extraction aborted: archive contains a symlink entry {name!r}. "
                            "Symlinks in archives are a security risk and are not supported."
                        )
                    target = (dest / name).resolve()
                    if target != resolved_dest and not target.is_relative_to(resolved_dest):
                        raise InstallError(
                            f"7z extraction aborted: entry {name!r} would escape the install directory."
                        )
        except InstallError:
            raise
        except Exception as exc:
            # Fail closed: if we can't read the index we can't guarantee safety.
            raise InstallError(
                f"7z extraction aborted: could not validate archive member paths for "
                f"{archive_path.name}. The archive may use an unsupported header format. "
                f"Detail: {exc}"
            ) from exc

    @staticmethod
    def _extract_7z(archive_path: Path, dest: Path) -> None:
        """Extract a .7z archive into *dest* (a fresh staging folder).

        Uses the installed 7-Zip when available (much faster, and it supports
        every filter), otherwise the pure-Python py7zr.
        """
        # Validate member paths before extraction to prevent path traversal.
        PortableInstaller._validate_7z_members(archive_path, dest)

        seven_zip = PortableInstaller._find_7zip()
        if seven_zip:
            proc = subprocess.run(
                [seven_zip, "x", str(archive_path), f"-o{dest}", "-y", "-bd"],
                capture_output=True,
                text=True,
                creationflags=_NO_WINDOW,
            )
            if proc.returncode != 0:
                raise InstallError(f"7-Zip extraction failed: {(proc.stderr or proc.stdout).strip()}")
        else:
            import py7zr

            try:
                with py7zr.SevenZipFile(archive_path, "r") as archive:
                    archive.extractall(path=dest)
            except Exception as exc:
                exc_str = str(exc)
                if "not supported" in exc_str or "bcj" in exc_str.lower():
                    raise InstallError(
                        "This archive uses a compression filter (BCJ2) that the built-in extractor "
                        "does not support. Install 7-Zip from https://7-zip.org and try again."
                    ) from exc
                raise InstallError(f"Failed to extract .7z archive: {exc}") from exc

        # Post-extraction validation — verify nothing escaped.
        resolved_dest = dest.resolve()
        for root, dirs, files in os.walk(dest):
            for name in files + dirs:
                target = Path(root, name).resolve()
                if not target.is_relative_to(resolved_dest):
                    raise InstallError(
                        f"7z extraction produced a path outside the install directory: {target}"
                    )

    @staticmethod
    def _safe_extract_zip(archive: zipfile.ZipFile, dest: Path) -> None:
        """Extract a ZIP archive into *dest* (a fresh staging folder), rejecting
        entries that would escape it (Zip Slip). Symlink entries are skipped."""
        resolved_dest = dest.resolve()
        for member in archive.infolist():
            # Skip symlinks — external_attr high 16 bits encode Unix mode;
            # 0o120000 (0xA000) is the symlink flag.
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if unix_mode and (unix_mode & 0o170000) == 0o120000:
                continue
            target = (dest / member.filename).resolve()
            if target != resolved_dest and not target.is_relative_to(resolved_dest):
                raise InstallError(
                    f"ZIP extraction aborted: entry {member.filename!r} would escape the install directory."
                )
            archive.extract(member, dest)

        # Post-extraction validation: verify all resolved paths are still inside *dest*.
        for root, dirs, files in os.walk(dest):
            for name in files + dirs:
                resolved = Path(root, name).resolve()
                if not resolved.is_relative_to(resolved_dest):
                    raise InstallError(
                        f"ZIP extraction aborted: extracted path {resolved} escapes the install directory."
                    )

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = "".join(ch for ch in value if ch.isalnum() or ch in {" ", "-", "_"}).strip()
        return safe or "Program"

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Sanitize a filename derived from a URL or archive entry.

        Strips null bytes, replaces dangerous characters, rejects Windows
        reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9), and
        truncates excessively long names.
        """
        # Remove null bytes and control characters.
        name = "".join(ch for ch in name if ord(ch) >= 32 and ch != "\x7f")
        # Replace characters illegal in Windows filenames.
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        # Strip leading/trailing dots and spaces (Windows ignores trailing dots).
        name = name.strip(". ")
        if not name:
            return "download"
        # Check for reserved device names (with or without extension).
        stem = Path(name).stem.upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            name = f"_{name}"
        # Truncate to a safe length (255 is the NTFS limit for a single component).
        if len(name) > 200:
            suffix = Path(name).suffix
            name = name[: 200 - len(suffix)] + suffix
        return name


def normalize_dir(path: Path | str) -> str:
    """Key used to compare install folders (absolute, case-insensitive on Windows)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _normalise_name(value: str) -> str:
    """Lowercase letters and digits only, for comparing program/file names."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _normalise_name_for_hint(value: str) -> str:
    """Like :func:`_normalise_name`, but ignoring version numbers (``tool-1.2`` == ``tool-1.3``)."""
    return re.sub(r"[^a-z0-9]", "", _VERSION_RE.sub("", value.lower()))


def _asset_arch(name_lower: str) -> str:
    """Classify a lowercase asset name as ``arm64``, ``x64``, ``x86`` or ``""`` (unmarked)."""
    if _ARCH_ARM64_RE.search(name_lower):
        return "arm64"
    if _ARCH_X64_RE.search(name_lower):
        return "x64"
    if _ARCH_X86_RE.search(name_lower):
        return "x86"
    return ""


_CAMEL_LOWER_UPPER_RE = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")


def _name_tokens(name: str) -> list[str]:
    """Split a file stem into lowercase words: ``MyAppSetup`` -> ``[my, app, setup]``."""
    spaced = _CAMEL_LOWER_UPPER_RE.sub(r"\1 \2", name)
    spaced = _CAMEL_ACRONYM_RE.sub(r"\1 \2", spaced)
    return [token.lower() for token in re.split(r"[^A-Za-z0-9]+", spaced) if token]


def _is_installer_stem(stem: str) -> bool:
    if _INSTALLER_STEM_RE.match(stem):
        return True
    return any(token in _INSTALLER_TOKENS for token in _name_tokens(stem))


def _expected_sha256(asset: GitHubReleaseAsset) -> str:
    """The hex SHA-256 GitHub publishes for *asset*, or "" when there isn't one."""
    digest = str(getattr(asset, "digest", "") or "").strip()
    algorithm, sep, value = digest.partition(":")
    if sep and algorithm.lower() == "sha256":
        return value.strip()
    return ""


def _normalise_sha256(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:"):]
    return value


def _launch_relpath(program: ManagedProgram) -> str | None:
    """The program's current launch file, relative to its install folder (or None)."""
    if not program.launch_path or not program.install_dir:
        return None
    try:
        return str(Path(program.launch_path).relative_to(Path(program.install_dir)))
    except ValueError:
        return None


def _previous_launch_in_staging(staging: Path, relpath: str | None) -> Path | None:
    """The previous launch file if the new content still ships it at the same place."""
    if not relpath:
        return None
    candidate = staging / relpath
    if not fsops.is_within(candidate, staging) or not candidate.is_file():
        return None
    if candidate.suffix.lower() not in EXECUTABLE_EXTENSIONS | SCRIPT_EXTENSIONS:
        return None
    return candidate


def _format_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"
