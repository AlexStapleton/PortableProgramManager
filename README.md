# Portable Program Manager

Install and manage portable .exe programs.

Portable Program Manager is a Windows 11 desktop application written in Python with PySide6.
It helps you discover portable apps on GitHub, install them from GitHub repositories or direct URLs,
and manage the programs you have installed.

## Core capabilities
- Search GitHub repositories by keyword
- Inspect likely portable releases and install the best matching asset
- Install from repository URL or direct download URL
- Track installed programs in a local registry
- Launch managed programs from the GUI (optionally elevated)
- Per-program update checks with schedules (on start / interval / daily / weekly)
- System tray support (minimize / close to tray)
- Configure install root, cache folder, and optional GitHub token (stored DPAPI-encrypted)

## Install
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run
```bash
set PYTHONPATH=src
python main.py
```
Or just run `run_windows.bat`.

## Build a standalone .exe
```bash
pip install pyinstaller
pyinstaller --noconfirm PortableProgramManager.spec
```
The executable is written to `dist\PortableProgramManager.exe`.

## Notes
- Supported asset types: `.zip`, `.7z`, `.exe`, `.bat`, `.cmd` (plus `.ps1` launch targets inside archives).
- `.zip` and `.7z` archives are extracted automatically (BCJ2 `.7z` archives need 7-Zip installed).
- Settings and the program registry live under `%LOCALAPPDATA%\PortableProgramManager`.

## Project docs
- [`docs/CODE_REVIEW.md`](docs/CODE_REVIEW.md): full code review (2026-10-08) with prioritized findings
- [`DESIGN_DOCUMENT.md`](DESIGN_DOCUMENT.md): architecture and feature design
- [`ANALYSIS_FINDINGS.md`](ANALYSIS_FINDINGS.md): earlier security/performance analysis

## License
GPL-3.0. See [LICENSE](LICENSE).
