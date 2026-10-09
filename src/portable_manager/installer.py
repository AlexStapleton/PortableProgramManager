from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import zipfile

import py7zr
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from .github_client import GitHubClient, GitHubRelease, GitHubReleaseAsset
from .models import AppSettings, ManagedProgram, UpdatePolicy

PORTABLE_EXTENSIONS = {".exe", ".zip", ".7z", ".bat", ".cmd"}
EXECUTABLE_EXTENSIONS = {".exe", ".bat", ".cmd"}
SCRIPT_EXTENSIONS = {".ps1"}  # runnable via powershell, lower priority than native executables
ProgressCallback = Callable[[int, str], None] | None

# Windows reserved device names that cannot be used as file/directory names.
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

_INSTALLER_HINTS = {"setup", "install", "installer", "unins", "update"}

# Characters that are special to cmd.exe and must cause quoting.
_CMD_SHELL_META = set('&|<>^()%!"')


def _quote_args_for_shell(args: list[str]) -> str:
    """Join pre-parsed argument tokens into a safely quoted command string
    suitable for the *params* argument of ``ShellExecuteW``.

    Each token is wrapped in double quotes and internal double-quotes are
    escaped with a backslash (the Windows convention).  This prevents shell
    metacharacters like ``&``, ``|``, ``>``, ``^`` from being interpreted.
    """
    quoted: list[str] = []
    for arg in args:
        # Always quote to neutralise any shell metacharacters.
        escaped = arg.replace('"', '\\"')
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)


@dataclass
class InstallResult:
    program: ManagedProgram
    downloaded_file: Path
    is_likely_installer: bool = False


class InstallError(RuntimeError):
    pass


class PortableInstaller:
    def __init__(self, settings: AppSettings, github: GitHubClient) -> None:
        self.settings = settings
        self.github = github

    def install_from_repo(
        self,
        repo_full_name: str,
        channel: str = "latest_release",
        progress_callback: ProgressCallback = None,
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

        program_name = self._safe_name(repo.name)
        install_dir = Path(self.settings.install_root) / program_name
        downloaded_file, downloaded_hash = self._download(asset.download_url, Path(self.settings.download_cache), asset.name, progress_callback=progress_callback)
        launch_path = self._materialize_asset(downloaded_file, install_dir, progress_callback=progress_callback)
        self._cleanup_cached_file(downloaded_file)

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
            program_id=f"repo::{repo.full_name.lower()}",
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
        install_dir = Path(self.settings.install_root) / program_name
        downloaded_file, downloaded_hash = self._download(url, Path(self.settings.download_cache), file_name, progress_callback=progress_callback)
        launch_path = self._materialize_asset(downloaded_file, install_dir, progress_callback=progress_callback)
        self._cleanup_cached_file(downloaded_file)

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
            program_id=f"url::{program_name.lower()}::{hashlib.sha256(url.encode()).hexdigest()[:16]}",
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
        install_dir = Path(program.install_dir)
        downloaded_file, downloaded_hash = self._download(
            asset.download_url,
            Path(self.settings.download_cache),
            asset.name,
            progress_callback=progress_callback,
        )
        # Verify the download differs from what is already installed.
        # An identical hash may indicate a replayed or stale download.
        new_hash = f"sha256:{downloaded_hash}"
        if program.installed_hash and program.installed_hash == new_hash:
            self._cleanup_cached_file(downloaded_file)
            logging.getLogger(__name__).warning(
                "Update download for %s has identical hash to the installed version (%s); skipping.",
                program.name, new_hash,
            )
            raise InstallError(
                "The downloaded update is identical to the currently installed version "
                "(same SHA-256 hash). The update was skipped."
            )
        launch_path = self._materialize_asset(downloaded_file, install_dir, progress_callback=progress_callback)
        self._cleanup_cached_file(downloaded_file)
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

    def _download(self, url: str, output_dir: Path, file_name: str, progress_callback: ProgressCallback = None) -> tuple[Path, str]:
        """Download *url* into *output_dir*/*file_name*.

        Returns ``(destination_path, sha256_hex)`` so callers can record the
        hash for integrity verification.  Cleans up the partial file on failure.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / file_name
        if progress_callback:
            progress_callback(10, f"Starting download: {file_name}")
        sha256 = hashlib.sha256()
        try:
            # Limit redirects to prevent abuse and validate the final URL.
            session = requests.Session()
            session.max_redirects = 10
            with session.get(url, stream=True, timeout=120) as response:
                response.raise_for_status()
                # Validate that the final URL (after redirects) still uses HTTPS.
                final_url = response.url
                if not final_url.startswith("https://"):
                    raise InstallError(
                        f"Download aborted: server redirected to a non-HTTPS URL ({final_url[:80]})."
                    )
                total = int(response.headers.get("Content-Length", "0") or 0)
                downloaded = 0
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        sha256.update(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total > 0:
                            percent = 10 + int((downloaded / total) * 70)
                            progress_callback(min(percent, 80), f"Downloading {file_name} ({downloaded // 1024} KB)")
        except BaseException:
            # Remove the incomplete file so it doesn't get mistaken for a valid download.
            destination.unlink(missing_ok=True)
            raise
        # Verify the downloaded size matches Content-Length (if the server sent one).
        if total > 0 and downloaded != total:
            destination.unlink(missing_ok=True)
            raise InstallError(
                f"Download size mismatch for {file_name}: expected {total} bytes, got {downloaded}."
            )
        if downloaded == 0:
            destination.unlink(missing_ok=True)
            raise InstallError(f"Downloaded file {file_name} is empty (0 bytes).")
        if progress_callback:
            progress_callback(85, f"Download complete: {file_name}")
        return destination, sha256.hexdigest()

    @staticmethod
    def _cleanup_cached_file(path: Path) -> None:
        """Remove a cached download after successful installation."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # non-critical — stale cache file is harmless

    def _materialize_asset(self, downloaded_file: Path, install_dir: Path, progress_callback: ProgressCallback = None) -> Path | None:
        install_dir.mkdir(parents=True, exist_ok=True)
        suffix = downloaded_file.suffix.lower()
        if progress_callback:
            progress_callback(88, f"Preparing install folder for {downloaded_file.name}")

        if suffix == ".zip":
            try:
                with zipfile.ZipFile(downloaded_file, "r") as archive:
                    self._safe_extract_zip(archive, install_dir)
            except zipfile.BadZipFile as exc:
                raise InstallError(f"Corrupt or invalid ZIP archive: {exc}")
            self._flatten_single_subdir(install_dir)
            if progress_callback:
                progress_callback(95, f"Extracted {downloaded_file.name}")
            return self.guess_launch_executable(install_dir)

        if suffix == ".7z":
            self._extract_7z(downloaded_file, install_dir)
            self._flatten_single_subdir(install_dir)
            if progress_callback:
                progress_callback(95, f"Extracted {downloaded_file.name}")
            return self.guess_launch_executable(install_dir)

        target = install_dir / downloaded_file.name
        shutil.copy2(downloaded_file, target)
        if progress_callback:
            progress_callback(95, f"Installed {downloaded_file.name}")
        return target

    @staticmethod
    def guess_launch_executable(install_dir: Path) -> Path | None:
        exe_candidates: list[Path] = []
        script_candidates: list[Path] = []
        for root, _, files in os.walk(install_dir):
            for file_name in files:
                path = Path(root) / file_name
                suffix = path.suffix.lower()
                if suffix in EXECUTABLE_EXTENSIONS:
                    exe_candidates.append(path)
                elif suffix in SCRIPT_EXTENSIONS:
                    script_candidates.append(path)

        def _sort_key(p: Path) -> tuple:
            return ("setup" in p.name.lower(), len(p.parts), len(p.name))

        if exe_candidates:
            exe_candidates.sort(key=_sort_key)
            return exe_candidates[0]
        if script_candidates:
            script_candidates.sort(key=_sort_key)
            return script_candidates[0]
        return None

    @staticmethod
    def looks_like_installer(launch_path: Path | None, install_dir: Path) -> bool:
        """Return True if the resolved launch executable looks like a setup/installer.

        Heuristics:
        - The filename contains 'setup', 'install', etc.
        - The archive produced only one .exe and it matches the pattern.
        - The asset filename itself contained 'setup' or 'install'.
        """
        if not launch_path:
            return False
        name_lower = launch_path.stem.lower()
        if any(hint in name_lower for hint in _INSTALLER_HINTS):
            return True
        # If there's only one .exe in the entire install dir, and no other
        # executables, check the asset-level name too.
        exes = list(install_dir.rglob("*.exe"))
        if len(exes) == 1:
            sole_name = exes[0].stem.lower()
            if any(hint in sole_name for hint in _INSTALLER_HINTS):
                return True
        return False

    @staticmethod
    def pick_portable_asset(assets: list[GitHubReleaseAsset], asset_name_hint: str | None = None) -> GitHubReleaseAsset | None:
        if not assets:
            return None

        preferred_patterns = [".zip", "portable", "win64", "win-x64", "windows", "x86_64", ".exe"]
        # Patterns that indicate a non-Windows asset — heavy penalty.
        non_windows_patterns = [
            "macos", "darwin", "linux", "ubuntu", "debian", "fedora",
            "arm64", "aarch64", ".dmg", ".deb", ".rpm", ".appimage",
            ".app", "-mac-", "-mac.", "_mac_", "_mac.",
        ]
        hint_lower = (asset_name_hint or "").lower().strip()
        scored: list[tuple[int, GitHubReleaseAsset]] = []
        for asset in assets:
            name = asset.name.lower()
            suffix = Path(name).suffix.lower()
            if suffix not in PORTABLE_EXTENSIONS:
                continue
            score = 0
            for idx, pattern in enumerate(preferred_patterns[::-1], start=1):
                if pattern in name:
                    score += idx * 10
            # Penalise assets that are clearly for another OS.
            for pattern in non_windows_patterns:
                if pattern in name:
                    score -= 500
                    break
            if hint_lower:
                if name == hint_lower:
                    score += 1000
                elif hint_lower in name or name in hint_lower:
                    score += 250
            if "installer" in name or "msi" in name:
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
    def _validate_7z_members(archive_path: Path, install_dir: Path) -> None:
        """Check that no member in the .7z archive would escape *install_dir*.

        Mirrors the Zip Slip protection applied to ZIP files.  Symlink entries
        are rejected because on Windows 11 with Developer Mode enabled,
        symlinks can be created without admin privileges and could be used to
        write outside the install directory.
        """
        resolved_install = install_dir.resolve()
        try:
            with py7zr.SevenZipFile(archive_path, "r") as archive:
                for entry in archive.list():
                    name = entry.filename
                    if entry.is_symlink:
                        raise InstallError(
                            f"7z extraction aborted: archive contains a symlink entry {name!r}. "
                            "Symlinks in archives are a security risk and are not supported."
                        )
                    target = (install_dir / name).resolve()
                    if target != resolved_install and not target.is_relative_to(resolved_install):
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
    def _extract_7z(archive_path: Path, install_dir: Path) -> None:
        """Extract a .7z archive.

        Tries py7zr (pure Python) first.  Falls back to the system 7z.exe when
        py7zr reports an unsupported compression filter such as BCJ2, which is
        commonly used in real-world 7z releases.
        """
        # Validate member paths before extraction to prevent path traversal.
        PortableInstaller._validate_7z_members(archive_path, install_dir)

        try:
            with py7zr.SevenZipFile(archive_path, "r") as archive:
                archive.extractall(path=install_dir)
            return
        except Exception as exc:
            exc_str = str(exc)
            # Only fall back for unsupported-filter errors; re-raise everything else.
            if "not supported" not in exc_str and "bcj" not in exc_str.lower():
                raise InstallError(f"Failed to extract .7z archive: {exc}") from exc

        seven_zip = PortableInstaller._find_7zip()
        if not seven_zip:
            raise InstallError(
                "This archive uses a compression filter (BCJ2) that py7zr does not support. "
                "Install 7-Zip from https://7-zip.org and re-try."
            )
        proc = subprocess.run(
            [seven_zip, "x", str(archive_path), f"-o{install_dir}", "-y"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise InstallError(f"7-Zip extraction failed: {proc.stderr or proc.stdout}")

        # Post-extraction validation for the system 7z.exe path — verify nothing escaped.
        resolved_install = install_dir.resolve()
        for root, dirs, files in os.walk(install_dir):
            for name in files + dirs:
                target = Path(root, name).resolve()
                if not target.is_relative_to(resolved_install):
                    raise InstallError(
                        f"7z extraction produced a path outside the install directory: {target}"
                    )

    @staticmethod
    def _safe_extract_zip(archive: zipfile.ZipFile, install_dir: Path) -> None:
        """Extract a ZIP archive, rejecting entries that would escape install_dir (Zip Slip).

        Extraction happens into a temporary directory first.  After all
        members are extracted and every resolved path is validated, the
        contents are moved into *install_dir*.  This eliminates the TOCTOU
        window between the path check and the actual file write.

        Symlink entries are skipped rather than extracted.
        """
        import tempfile

        # Extract into a staging directory next to install_dir (same volume
        # so os.rename / shutil.move is fast).
        staging = Path(tempfile.mkdtemp(
            dir=str(install_dir.parent),
            prefix=".ppm_extract_",
        ))
        try:
            resolved_staging = staging.resolve()
            for member in archive.infolist():
                # Skip symlinks — external_attr high 16 bits encode Unix mode;
                # 0o120000 (0xA000) is the symlink flag.
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                if unix_mode and (unix_mode & 0o170000) == 0o120000:
                    continue
                target = (staging / member.filename).resolve()
                if target != resolved_staging and not target.is_relative_to(resolved_staging):
                    raise InstallError(
                        f"ZIP extraction aborted: entry {member.filename!r} would escape the install directory."
                    )
                archive.extract(member, staging)

            # Post-extraction validation: verify all resolved paths are still
            # inside the staging directory (catches symlink-based attacks that
            # could have been created during extraction).
            for root, dirs, files in os.walk(staging):
                for name in files + dirs:
                    resolved = Path(root, name).resolve()
                    if not resolved.is_relative_to(resolved_staging):
                        raise InstallError(
                            f"ZIP extraction aborted: extracted path {resolved} escapes the staging directory."
                        )

            # Move contents from staging into install_dir.
            for item in staging.iterdir():
                dest = install_dir / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.move(str(item), str(dest))
        finally:
            # Clean up staging directory.
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _flatten_single_subdir(install_dir: Path) -> None:
        """If extraction produced exactly one subdirectory and no loose files, move
        its contents up into install_dir and remove the now-empty wrapper folder.

        This handles the common GitHub release ZIP pattern where everything lives
        inside a versioned top-level folder (e.g. ``AppName-v1.2.3/``).
        """
        entries = list(install_dir.iterdir())
        if len(entries) != 1 or not entries[0].is_dir():
            return
        sub = entries[0]
        for item in list(sub.iterdir()):
            dest = install_dir / item.name
            # On collision, remove the existing target so the archive content wins.
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
        try:
            sub.rmdir()
        except OSError:
            # Should not happen now, but leave as safety net.
            shutil.rmtree(sub, ignore_errors=True)

    @staticmethod
    def run_program(program: ManagedProgram) -> None:
        if not program.launch_path:
            raise InstallError("This managed program does not have a launch path yet.")

        launch = Path(program.launch_path)
        if not launch.is_file():
            raise InstallError(f"Launch path does not exist: {program.launch_path}")

        cwd = program.working_directory_override or program.install_dir
        if not Path(cwd).is_dir():
            cwd = program.install_dir  # fall back to install dir if override is invalid

        suffix = launch.suffix.lower()
        extra_args = shlex.split(program.launch_args, posix=(platform.system() != "Windows")) if program.launch_args.strip() else []

        try:
            if program.run_as_admin:
                # ShellExecuteW passes params through the shell, so we must
                # quote each argument individually to prevent injection via
                # shell metacharacters (e.g. & | < > ^).
                params = _quote_args_for_shell(extra_args) if extra_args else None
                result = ctypes.windll.shell32.ShellExecuteW(None, "runas", program.launch_path, params, cwd, 1)
                if result <= 32:
                    if result == 5:
                        raise InstallError("Launch cancelled: administrator access was denied or the UAC prompt was dismissed.")
                    raise InstallError(f"Failed to launch as administrator (ShellExecute returned {result}).")
                return

            if suffix in {".bat", ".cmd"}:
                # CreateProcess cannot run batch files directly — cmd /c is required.
                subprocess.Popen(["cmd", "/c", program.launch_path] + extra_args, cwd=cwd, shell=False)
            elif suffix == ".ps1":
                subprocess.Popen(
                    ["powershell", "-ExecutionPolicy", "RemoteSigned", "-File", program.launch_path] + extra_args,
                    cwd=cwd,
                    shell=False,
                )
            else:
                subprocess.Popen([program.launch_path] + extra_args, cwd=cwd, shell=False)
        except FileNotFoundError:
            raise InstallError(f"Could not find executable: {program.launch_path}")
        except OSError as exc:
            raise InstallError(f"Failed to launch program: {exc}")

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
