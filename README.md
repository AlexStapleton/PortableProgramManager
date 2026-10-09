# Portable Program Manager

Install and manage portable .exe programs.

Portable Program Manager is a Windows 10/11 desktop app (Python + PySide6). It finds portable apps on GitHub, installs them from a GitHub project or a direct download link, keeps them up to date, and launches them from one place or from the system tray.

## Features
- **Several sites:** GitHub, GitLab and Codeberg are built in. You can add your own GitHub Enterprise, GitLab, Gitea or Forgejo server in **Settings → Sources**.
- **Discover:** search one site or all of them at once. Results show stars, latest release date, whether there's a Windows build, and whether you already have it.
  - Optionally include **forks** and show only forks with changes of their own (e.g. `+124 / −4` commits ahead of / behind the original).
- **Install** from a project page or release file on any of your sources, or any direct `https://` download (`.zip`, `.7z`, `.exe`, `.bat`, `.cmd`).
  - Only Windows builds for your CPU are picked.
  - Downloads are checked against the SHA-256 that GitHub publishes.
- **Safe updates:** new files are staged, then merged into the program folder all-or-nothing, with rollback.
  - Your own files (settings, saves, plugins) are kept.
  - Programs that are still running are never half-updated.
- **Update modes per program:**
  - show in the list
  - notify me
  - download, then ask
  - install automatically
- **Schedules:** check on app start, every N hours, daily or weekly.
- **Launching:**
  - Programs start the way Explorer starts them, so apps that need administrator rights get a UAC prompt.
  - You can also force "Run as administrator", pass custom arguments, and run `.ps1` scripts.
- **Safe removal:** "Remove and delete files" refuses while the program is running and never deletes system or shared folders.
- **Permission repair** for programs installed by v0.7 and earlier, whose files could become unreadable by your own account (see below).
- **Tray app:**
  - The tray menu launches your pinned and recent programs.
  - Notifications report found, downloaded and installed updates.
  - Only one copy of the app runs at a time.
- **Follows the Windows light/dark theme**, or force one in Settings.

## Run from source
```bash
run_windows.bat
```
Or step by step:
```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
set PYTHONPATH=src
python main.py
```

## Build the standalone .exe
```bash
python -m pip install -r requirements-dev.txt
python tools\make_version_info.py
python -m PyInstaller --noconfirm --clean PortableProgramManager.spec
```
The result is `dist\PortableProgramManager.exe`.

GitHub Actions builds the exe on every push to `main` (download it from the run's **Artifacts**), and on a version tag such as `v0.9.0` it also publishes a GitHub Release with the exe and its SHA-256 attached. You can also start a build by hand from **Actions → Build exe → Run workflow**. Run `tools\make_version_info.py` again after changing `__version__` in `src/portable_manager/__init__.py`.

## Tests
```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```
Tests never touch your real settings or programs; they use temporary folders. GitHub Actions runs them on every push.

## Where things are stored
- Settings, the program list (`programs.json` plus a rolling `programs.json.bak`) and the log file (`app.log`) live in `%LOCALAPPDATA%\PortableProgramManager`. The About dialog has an "Open log folder" button.
- Programs are installed under the install folder you choose in Settings (default: `%USERPROFILE%\Portable Apps`).
- The optional GitHub token is encrypted with Windows DPAPI for your account.

## Troubleshooting
- **"Some of X's files can't be opened by your Windows account":** versions up to 0.7 could leave installed files readable only by administrators, especially if the manager was run as administrator. Right-click the program and choose **Repair permissions**. Windows may ask for administrator approval.
- **A program won't start and the message mentions antivirus:** Windows Security may have quarantined it. Check *Windows Security → Virus & threat protection → Protection history*.
- **Update checks fail with "rate limit":** GitHub allows 60 requests per hour without a token. Add one in **Settings → Sources → GitHub**; it raises the limit to 5,000. If you use the GitHub CLI, the **Use my GitHub CLI login** button fills it in. Answers that haven't changed are cached, and GitHub doesn't count those cached checks against the limit when you're using a token.
- **Forks show "not compared":** the site's API limit was reached, or the site can't compare forks (Codeberg/Gitea servers often can't).

## Project docs
- [`docs/CODE_REVIEW.md`](docs/CODE_REVIEW.md): the full code review (82 findings) and how each was resolved
- [`DESIGN_DOCUMENT.md`](DESIGN_DOCUMENT.md): architecture and feature design
- [`ANALYSIS_FINDINGS.md`](ANALYSIS_FINDINGS.md): earlier security/performance analysis

## License
GPL-3.0. See [LICENSE](LICENSE).
