# Portable Program Manager - Design Document

## Document status
- Project: Portable Program Manager
- Platform: Windows 11
- Language: Python
- UI stack: PySide6 (Qt Widgets)
- Current implementation status: functional MVP with due-check scheduler, edit-program dialog, threaded background jobs, run-as-admin, .7z extraction, ZIP Slip protection, missing-folder detection, and remove-with-delete
- Last updated: 2026-03-04
- Last code review: 2026-03-03 (see §15)

## Changelog
- v0.7:
  - Added **Remove and delete files** option: the remove-program dialog now has three buttons — "Remove only", "Remove and delete files", and Cancel. `controller.remove_program(delete_files=bool)` calls `shutil.rmtree` outside the lock after removing the registry entry.
  - Added **Stable / Pre-release channel selector** in the Discover tab. A `QComboBox` next to the Install button lets the user choose the GitHub release channel before installing. The channel is stored on `UpdatePolicy.channel` so future update checks use the same channel.
  - Added **.7z extraction** via `py7zr` (pure Python). Extraction falls back to the system `7z.exe` (`_find_7zip()`, `_extract_7z()`) when py7zr encounters a BCJ2 compression filter it does not support.
  - Added **ZIP Slip protection** (`_safe_extract_zip()`): each ZIP entry's resolved path is checked with `Path.is_relative_to(install_dir)` before extraction.
  - Added **single-subdir flattening** (`_flatten_single_subdir()`): after ZIP or .7z extraction, if the install directory contains exactly one subdirectory and no loose files, its contents are promoted to the parent level. Handles the common GitHub versioned-wrapper pattern (e.g. `AppName-v1.2.3/`).
  - Added **missing install folder detection**: on startup the app scans all managed programs and shows a count of those whose `install_dir` no longer exists. In the Managed Programs table, rows with missing folders are colored red and show `[folder missing]` in the install-dir column. `_open_folder()` guards against missing directories with a warning instead of crashing.
  - Fixed **`.bat`/`.cmd` execution on Windows**: `subprocess.Popen` with `shell=False` cannot run batch files directly. These are now launched with `["cmd", "/c", launch_path, ...]`.
  - Added **PowerShell script support**: `.ps1` files are launched via `["powershell", "-ExecutionPolicy", "Bypass", "-File", launch_path, ...]`. `SCRIPT_EXTENSIONS = {".ps1"}` is a new lower-priority fallback tier in `guess_launch_executable`.
  - Added **Run as Administrator**: `ManagedProgram.run_as_admin: bool = False` field; `run_program()` invokes `ctypes.windll.shell32.ShellExecuteW(..., "runas", ...)` when set, triggering the UAC prompt. The Edit Program dialog now includes a run-as-admin checkbox.
  - Added `py7zr>=0.20` to `requirements.txt`.
- v0.6:
  - Implemented `AppController.is_check_due()` with schedule-type rules for `app_start`, `interval`, `daily`, and `weekly`.
  - Implemented `AppController.check_due_updates(include_app_start)` that filters programs to only those whose schedule is currently due.
  - Replaced the unconditional bulk startup check with a schedule-aware due check; `app_start` programs are included at startup only.
  - Added a 30-minute periodic `QTimer` in `MainWindow` that re-runs due checks for `interval`/`daily`/`weekly` programs while the app is open.
  - Manual "Check All" button still checks all enabled programs regardless of schedule.
- v0.5:
  - Added Edit Program dialog covering display name, notes, launch path, launch arguments, working directory override, and full update policy editing.
  - Added `edit_program()` controller method with lock-safe in-place mutation and persistence.
  - Added search filter and sort controls to the Discover tab (Windows-only heuristic filter, inactivity threshold filter, sort by stars or recently updated).
  - Fixed premature TaskWorker garbage-collection crash by keeping Python references until `finished` fires.
  - Connected `returnPressed` on the search input for keyboard convenience.
  - Added defensive None-guards for `repo.updated_at` from the GitHub API.
- v0.4:
  - Implemented Qt background workers for GitHub search, install-from-repo, install-from-URL, single-app update checks, bulk update checks, and update installs.
  - Added progress reporting in the status bar for download and update jobs.
  - Added thread-safe controller access patterns for managed-program state persistence.
  - Added a background-task runner module and refactored the UI to keep the window responsive during long-running operations.
  - Tested threaded controller flows with mocked GitHub responses and re-ran package compile checks.
- v0.3:
  - Implemented update metadata in the managed-program model.
  - Implemented manual **Check Selected**, **Check All**, and **Install Update** actions.
  - Added installed-app filtering in the Managed Programs tab.
  - Added update-related columns in the managed-app table.
  - Expanded settings to include default update behavior and startup update-check option.
  - Added compatibility handling so older program-registry JSON can still load.
  - Tested controller update-check and update-install flows with mocked GitHub responses.
- v0.2: Expanded the design from MVP notes into a fuller product plan. Added core feature definitions, improvement roadmap, source-model direction, and a detailed design for per-program scheduled update checks.
- v0.1: Created initial MVP architecture, GitHub search flow, repository install flow, direct URL install flow, settings persistence, managed program registry, and launch actions.

## 1. Product goal
Build a Windows 11 desktop application that acts as a lightweight portable program manager. The user should be able to:
- Search GitHub for programs
- Install from a GitHub repository or a direct URL
- Manage installed portable programs in a GUI
- Launch managed programs from the GUI
- Configure installation behavior through settings
- Check for updates for managed programs and later automate those checks per app

The design intent is to create a practical portable-app manager rather than a full package manager. That means the app focuses on file download, extraction, bookkeeping, launching, and updates for self-contained applications, while avoiding system-wide MSI-style installation logic.

## 2. Product principles

### 2.1 Portable first
The application should prefer release assets that can be downloaded and run without registry-heavy installation or machine-wide setup.

### 2.2 User control over automation
The app should automate common discovery and update flows, but it should still let the user override launch path, asset choice, install folder, and update schedule when automatic logic is wrong.

### 2.3 Clear trust boundaries
The app should make it obvious what came from GitHub, what was auto-selected, what was downloaded, and whether authenticity checks were performed.

### 2.4 Windows 11 desktop quality
The application should feel like a real Windows desktop application: responsive UI, dark mode, progress reporting, settings, dialogs, notifications, and sensible file/folder actions.

### 2.5 Incremental architecture
The MVP is intentionally lightweight, but the internal structure should support later migration to SQLite, threaded jobs, additional sources, and richer metadata without a rewrite.

## 3. Technical decisions

### 3.1 Why PySide6
PySide6 gives a modern native desktop toolkit for Windows, supports Qt Widgets and richer interfaces, and is practical for packaging into a Windows executable later.

### 3.2 Why local JSON persistence for MVP
The MVP stores settings and the managed-program registry in JSON files under Local AppData. This keeps implementation simple and inspectable while the app model is still stabilizing. A future version can migrate to SQLite without changing the top-level UI behavior.

### 3.3 Why GitHub API first
The discovery model uses the GitHub REST API. Search happens against repositories, and installation uses the repository's latest release when possible.

### 3.4 Why source abstraction should come early
Even while GitHub is the only supported discovery source today, the application should internally treat the origin of an install as a pluggable source. That allows later support for direct manifests, vendor feeds, JSON indexes, or curated package catalogs without reworking the UI.

## 4. Current architecture

### 4.1 Current files
- `main.py`: application entry point
- `src/portable_manager/app.py`: bootstraps Qt and theme
- `src/portable_manager/models.py`: typed dataclasses for settings, managed programs, and update policy
- `src/portable_manager/storage.py`: settings and registry persistence
- `src/portable_manager/github_client.py`: GitHub API search and release helpers
- `src/portable_manager/installer.py`: download, extract, launch detection, and update install logic
- `src/portable_manager/controller.py`: application orchestration between UI and services
- `src/portable_manager/ui/main_window.py`: main GUI
- `src/portable_manager/ui/settings_dialog.py`: settings editor
- `src/portable_manager/ui/theme.py`: dark Fusion-based stylesheet
- `src/portable_manager/ui/workers.py`: Qt background worker signals, TaskWorker (QRunnable), and TaskRunner (QThreadPool wrapper)
- `src/portable_manager/ui/edit_program_dialog.py`: dialog for editing program name, notes, launch path, launch args, working directory, update policy, and run-as-admin flag

### 4.2 Logical layers
- UI layer: windows, dialogs, tables, actions, user prompts
- Controller layer: coordinates user actions, validation, service calls, and refreshes
- Service layer: GitHub search, asset selection, download, extraction, update checks
- Persistence layer: settings and managed apps
- Background job layer: implemented for core I/O-bound actions via Qt worker threads

## 5. Data model status

### 5.1 Implemented `ManagedProgram` fields
Implemented now:
- stable program id
- display name
- source type and source value
- install directory
- launch path
- installed version
- installed asset name
- repository reference
- homepage URL
- notes
- source id / source kind
- installed asset URL / asset size / installed hash (field present in model; hash computation not yet wired up)
- launch arguments
- working directory override
- tags / pinned flag
- run as administrator flag
- installed timestamp / last run timestamp
- update policy object (fields: check_enabled, update_mode, schedule_type, interval_hours, channel, asset_selection_override, notify_on_available_update)
- latest upstream version / latest upstream published timestamp
- update-available flag
- update-available asset name
- last checked / last update found / last update attempt
- last update status
- last error message / last error timestamp

### 5.2 Implemented `AppSettings` fields
Implemented now:
- install root
- download cache
- GitHub token
- auto-open folder after install
- search result limit
- default update mode
- default update schedule type
- default update interval hours
- run update checks on startup
- show system notifications flag placeholder

### 5.3 Remaining data-model ideas
Not yet implemented in code:
- checksum / signature metadata (`installed_hash` field exists in the model but is never populated)
- release-id history (`GitHubRelease.release_id` is already fetched from the API but is not persisted to `ManagedProgram`; storing it would enable more reliable version comparison)
- rollback history
- per-app event log
- per-app custom asset regex UI editor
- rich status enum normalization for notifications and dashboard views

## 6. Core features - status and design

### 6.1 Discovery and search
Status: **Implemented**

Current:
- Search GitHub repositories by keyword
- Run GitHub search work in a background thread
- Display repository metadata in a results table
- Open repository page in browser

Next improvements:
- Filters for language, stars, topics, and archived status
- Better release visibility directly from search results
- Show whether a repo has releases before install attempt
- Surface portable confidence score based on asset names and release patterns
- Allow saving favorites or watchlist entries without installing

### 6.2 Install flows
Status: **Implemented**, with some gaps

Current:
- Install from GitHub repo URL
- Install from direct asset URL
- Run download and install work in background threads
- Show status-bar progress updates during downloads and install preparation
- Extract ZIP assets with ZIP Slip protection (`_safe_extract_zip`)
- Extract `.7z` assets via py7zr; falls back to system `7z.exe` for BCJ2-compressed archives
- Flatten single versioned wrapper subdirectory after extraction (`_flatten_single_subdir`)
- Detect likely executable (native executables preferred; PowerShell scripts as fallback)
- Warn in the activity log when no executable is detected after extraction
- Register managed app
- Reinstall/update from a newer GitHub release via the same installer pipeline
- Choose stable or pre-release channel before installing from the Discover tab

Still needed:
- Install preview dialog before download
- User-selectable asset when multiple plausible assets exist
- Optional checksum verification before registration
- Install history

### 6.3 Managed programs view
Status: **Implemented**, expanded in v0.3, v0.5, and v0.7

Current:
- Table of installed apps
- Filter box for installed apps
- Run selected app
- Open selected install folder (guards against missing directory with a warning)
- Edit selected program (name, notes, launch path, launch args, working directory, update policy, run-as-admin)
- Remove managed entry with optional deletion of the install folder (3-button confirmation dialog)
- Missing install folder detection: rows colored red with `[folder missing]` label; startup scan shows count
- Program-details pane
- Columns for version, latest upstream version, update status, last checked, and update policy summary

Still needed:
- Sort controls beyond table defaults
- Context menu actions
- Pinning and tags UI
- Missing executable badge and repair actions

### 6.4 Settings and configuration
Status: **Implemented**, expanded in v0.3

Current:
- Install root
- Cache folder
- GitHub token
- Search result limit
- Auto-open folder after install
- Default update mode
- Default update schedule
- Default update interval
- Startup update-check toggle

Still needed:
- Notification preferences with actual notification service
- Import/export settings
- Backup/restore registry
- Preferred asset/channel defaults per source

### 6.5 Launch and runtime actions
Status: **Implemented**, expanded in v0.5 and v0.7

Current:
- Run installed program
- Record last-run timestamp
- Launch arguments field editable via Edit Program dialog
- Working-directory override field editable via Edit Program dialog
- Run as administrator: `ManagedProgram.run_as_admin` flag; when set, `ShellExecuteW("runas", ...)` triggers a UAC elevation prompt. Toggleable via the Edit Program dialog.
- Correct execution dispatch by file type: `.bat`/`.cmd` via `cmd /c`, `.ps1` via PowerShell with bypass policy, `.exe` directly

Still needed:
- Launch history log
- Open executable location action

### 6.6 Updates and maintenance
Status: **In progress**

Implemented now:
- Manual check for updates for a selected app
- Bulk check for updates for all eligible apps
- Install available update for selected app
- Run update checks and update installs in background threads
- Persist update metadata and errors in the registry
- Prefer matching the previously installed asset name during update checks
- Schedule-aware startup update check (evaluates per-app schedule type and last-checked timestamp)
- In-app periodic update check every 30 minutes for interval/daily/weekly programs
- Per-app update policy editor in the Edit Program dialog (check_enabled, mode, schedule, interval, channel, asset override)

Not yet implemented:
- Notify-only / download-only / auto-install behavior execution paths
- Failure backoff
- Rollback support
- Repair flow for broken installs

## 7. Feature status matrix

| Feature | Status | Notes |
|---|---|---|
| Modern desktop GUI | Implemented | PySide6 with custom dark stylesheet |
| GitHub repository search | Implemented | Uses GitHub repository search endpoint |
| Install from GitHub repo URL | Implemented | Extracts `owner/repo` from URL and installs latest portable release asset |
| Install from direct URL | Implemented | Supports direct asset download for common portable asset types |
| Managed programs list | Implemented | Expanded with update-oriented columns and filtering |
| Run installed programs | Implemented | Launches stored executable path via subprocess |
| Settings menu | Implemented | Includes install behavior and default update settings |
| Local persistence | Implemented | JSON files in Local AppData |
| Launch path auto-detection for ZIPs | Implemented | Walks extracted files and selects likely executable |
| Open repository in browser | Implemented | Useful during discovery workflow |
| Remove managed entry | Implemented | 3-button dialog: remove only, remove and delete files, or cancel |
| Manual per-app update check | Implemented | Uses GitHub latest-release data and stored install metadata |
| Bulk update check | Implemented | Checks all programs with enabled update policy |
| Install update for selected app | Implemented | Reuses the installer pipeline to apply a newer release |
| Update metadata persistence | Implemented | Stores status, timestamps, upstream version, and errors |
| Startup-triggered update checks | Implemented | Optional setting now launches after window setup; work runs in the background |
| Installed-app filtering | Implemented | Filters by name, source, version, status, or notes |
| Delete installed files from disk | Implemented | Available as "Remove and delete files" option in the remove dialog |
| Verify checksums or signatures | Not yet implemented | Planned security improvement |
| .7z extraction | Implemented | Extracts via py7zr; falls back to system 7z.exe for BCJ2-compressed archives |
| ZIP Slip protection | Implemented | `_safe_extract_zip` validates each entry path before extraction |
| Single-subdir flattening | Implemented | `_flatten_single_subdir` promotes contents from versioned wrapper directories |
| Missing install folder detection | Implemented | Startup scan; red row highlighting; safe open-folder guard |
| Run as administrator | Implemented | ShellExecuteW "runas" verb; toggleable per program in Edit dialog |
| Pre-release channel support | Implemented | Channel selector in Discover tab; stored in UpdatePolicy for future checks |
| Authenticated GitHub rate-limit UX | Partial | Optional token supported, but no rate-limit dashboard yet |
| Background download/update progress UI | Implemented | Search, install, and update jobs now run in Qt worker threads with progress updates in the status bar |
| Portable app metadata editing UI | In progress | Model fields exist, but editor dialog is not built yet |
| Per-program scheduled update checks | In progress | Data model exists; scheduler execution is not built yet |
| Bulk update dashboard | Not yet implemented | Current UI only offers table columns and toolbar actions |
| Missing executable repair flow | Not yet implemented | Depends on maintenance actions and better state tracking |
| Rollback to previous version | Not yet implemented | Requires history and cached version retention |

## 8. Implemented workflows

### 8.1 Search GitHub
1. User enters a search term.
2. App calls the GitHub repository search API.
3. Results are displayed in a table with repository name, stars, update date, and description.
4. User can install the selected result or open the repository page.

### 8.2 Install from repository URL
1. User pastes a GitHub repository URL.
2. App extracts `owner/repo`.
3. App fetches the latest release.
4. App chooses the most likely portable asset using filename heuristics.
5. App downloads the asset into the cache folder.
6. App copies or extracts the asset into the configured install root.
7. App registers the installation in the managed-program registry.

### 8.3 Install from direct asset URL
1. User pastes a direct downloadable asset URL.
2. App validates that the URL points to a supported portable file type.
3. App downloads the file.
4. App copies or extracts it into the install folder.
5. App creates a managed entry.
6. If the URL is GitHub-hosted and repository ownership can be inferred, the app stores the repo reference for later update checks.

### 8.4 Launch installed app
1. User selects an installed program.
2. App resolves the saved launch path.
3. App starts the program with its install directory as working directory.
4. App records the last-run timestamp.

### 8.5 Manual update check
1. User selects a managed program.
2. User clicks **Check Selected**.
3. App loads the repository reference and current install metadata.
4. App fetches the latest release from GitHub.
5. App compares the remote version and best-matching asset against the installed metadata.
6. App stores `last_checked_at`, `latest_upstream_version`, and status.
7. UI shows either **Up to date**, **Update available**, **Unsupported source**, or **Check failed**.

### 8.6 Install available update
1. User selects a managed program.
2. User clicks **Install Update**.
3. App re-runs the update check to avoid stale state.
4. App selects the best matching portable asset, favoring the previously installed asset name.
5. App downloads and installs the new artifact into the existing install folder.
6. App updates the program's version, asset metadata, and update status.

## 9. Detailed design: per-program scheduled update checks
This remains the most important roadmap feature after the manual update foundation.

### 9.1 User goal
Each managed portable application should optionally have its own update-check policy instead of relying only on a single global update schedule.

Examples:
- Check a browser utility every day.
- Check a rarely changing media tool once a week.
- Disable automatic checks for an archived or manually managed tool.
- Notify for some apps, but auto-install updates for others.

### 9.2 Proposed user-facing behavior
Each managed program should eventually get an Update Policy section available from either:
- an Edit Program dialog, or
- a side panel/details pane in the main window

Per-program controls to build next:
- Enable update checks
- Mode: Manual / Notify only / Download only / Auto-install
- Schedule: On app start / Every N hours / Daily / Weekly
- Channel: Stable latest release / Include prereleases
- Optional asset rule override
- Last checked timestamp
- Last update found timestamp
- Current installed version
- Latest upstream version
- Status message and last error

### 9.3 Current architecture progress against the design
Implemented pieces:
- Update policy data exists in the model.
- Settings can define defaults for new apps.
- Controller can perform manual check/update actions.
- UI can show update state and trigger checks.

Still needed:
- `SchedulerService` (formal service layer; current timer is inline in the window)
- `NotificationService`
- backoff and duplicate-job suppression

### 9.4 Scheduling model options
Recommended early implementation remains:
- Check due work when the app starts.
- Run a lightweight in-app timer every X minutes while the app stays open.
- Mark checks due based on per-app policy and last-checked timestamp.

Current implementation:
- Startup-triggered due check: evaluates per-app schedule type and `last_checked_at` before queueing checks.
- In-app 30-minute periodic timer re-evaluates due programs while the app is open.
- `app_start`-scheduled programs are included only at startup, not during periodic checks.

### 9.5 Due-check logic (implemented in v0.6)
`AppController.is_check_due(program)` evaluates each managed program with `check_enabled = true`:
- `app_start`: always due; callers pass `include_app_start=True` at startup only.
- `interval`: due when `now >= last_checked_at + interval_hours`.
- `daily`: due when today's local date is later than the date of the last check.
- `weekly`: due when `now >= last_checked_at + 7 days`.
- Programs with no `last_checked_at` are always considered due.

`check_due_updates(include_app_start)` collects all due program IDs under the lock, then calls `check_for_updates` for each.

Still not implemented:
- no concurrent duplicate-job suppression
- startup grace period before first check
- backoff after repeated failures

## 10. Important implementation details

### 10.1 Portable asset selection heuristic
Implemented now:
- prefers names suggesting `.zip`, `portable`, `win64`, `windows`, or `.exe`
- penalizes likely installers such as `installer` or `msi`
- during update checks, boosts the previously installed asset name when possible

### 10.2 Executable detection after extraction
After a ZIP or .7z archive is extracted and single-subdir flattening is applied, the installer scans the install tree for executable candidates. It prefers native executables (`.exe`, `.bat`, `.cmd`) over PowerShell scripts (`.ps1`), avoids candidates whose names contain "setup", and prefers candidates closer to the root. If no executable is found, the activity log shows a warning.

### 10.2a Extraction details
- **ZIP**: extracted via `zipfile.ZipFile` using `_safe_extract_zip`, which validates each member's resolved path is within `install_dir` (ZIP Slip defense) before calling `archive.extract`.
- **.7z**: extracted via `py7zr.SevenZipFile`. If py7zr raises a "not supported" or "bcj" error (indicating a BCJ2 compression filter), the installer falls back to the system `7z.exe` command found via `_find_7zip()`. If neither is available, a clear error message instructs the user to install 7-Zip.
- **Single-subdir flattening**: after any archive extraction, if `install_dir` contains exactly one subdirectory and no loose files, all contents are moved up one level and the empty wrapper is removed.

### 10.2b Launch dispatch by file type
`run_program()` dispatches by `Path(launch_path).suffix.lower()`:
- `.bat`, `.cmd` → `subprocess.Popen(["cmd", "/c", launch_path, ...])` — `CreateProcess` cannot run batch files directly.
- `.ps1` → `subprocess.Popen(["powershell", "-ExecutionPolicy", "Bypass", "-File", launch_path, ...])`.
- all other types (`.exe`, etc.) → `subprocess.Popen([launch_path, ...])`.
- When `program.run_as_admin` is True, all types are launched via `ShellExecuteW(None, "runas", launch_path, params, cwd, 1)` regardless of extension.

### 10.3 Persistence model
Settings and managed programs are stored as human-readable JSON. Backward compatibility with older registry entries is preserved by supplying defaults for newly added update fields.

### 10.4 Recommended next internal abstractions
To support growth without controller bloat, add:
- `source_adapters/` for GitHub and future sources
- `services/update_service.py`
- `services/scheduler_service.py`
- `services/job_runner.py`
- `services/notification_service.py`
- `history.py` or `event_log.py` for install/update history

## 11. Testing performed for v0.4
Implemented tests performed in the container:
- Python compile check across the package
- Controller test with mocked GitHub release responses
- Threaded-operation controller test: search, install, check-for-updates, and install-update flows accept progress callbacks and complete successfully with mocked services
- Update-check flow test: detects update availability
- Update-install flow test: applies updated version metadata
- Unsupported-source test for direct URL installs without inferred repository
- Backward-compatibility test: older `programs.json` payloads still load into the expanded model
- Python compile check after worker-thread refactor across `main.py`, `src/portable_manager/*.py`, and `src/portable_manager/ui/*.py`

Not yet tested end-to-end here:
- live GitHub API calls
- real Windows launching behavior
- real PySide6 UI interactions
- large-download handling

## 12. Known gaps and risks

### 12.1 Functional gaps (pre-existing)
- Search, install, and update flows now run off the UI thread, but persistence remains JSON-based and long jobs still need cancellation support.
- Some GitHub repositories do not publish releases, so install-from-search and update checks can fail even when a repo exists.
- Some projects label assets inconsistently, which can make automatic portable-asset selection imperfect.
- There is no checksum or signature verification yet (`installed_hash` field exists in the model but is never populated).
- There is not yet a task-cancellation model for in-flight downloads or update checks.
- `.7z` archives using unsupported compression filters other than BCJ2 will fail with an error message but without a fallback path.
- Version comparison is intentionally simple and may need release-id-aware improvements. Note that `GitHubRelease.release_id` is already fetched from the API but is not persisted to `ManagedProgram`, so adding it is a straightforward data-plumbing step.
- The startup update check now evaluates per-app schedule type via `check_due_updates(include_app_start=True)`, so only programs that are actually due are checked. The previous unfiltered bulk-check behavior has been resolved.

### 12.2 Code-quality gaps identified in v0.4 review
- **`datetime.utcnow()` deprecation**: Used in `models.py` (`utc_now_iso`) and throughout `controller.py`. `datetime.utcnow()` is deprecated in Python 3.12 and removed in Python 3.14. Replace with `datetime.now(timezone.utc)` and update the ISO string helper accordingly.
- **`launch_args` splitting**: `installer.py` splits `program.launch_args` with `.split()`, which breaks arguments that contain spaces (e.g., file paths). Should use `shlex.split()` instead.
- **Double progress_callback(100) on install**: Both `PortableInstaller` and `AppController` emit `progress_callback(100, …)` at the end of install flows, so the 100% signal fires twice. The controller's redundant call should be removed.
- **`check_for_updates` mutates program outside the lock**: `controller.py` fetches a program under `_lock` but then mutates its fields outside the lock. `get_program` returns the actual object (not a copy), so the UI thread reading via `list_programs()` shallow-copy can observe a partially updated program mid-mutation. The mutation should be done under the lock, or the method should operate on a local copy and then swap it in.
- **`program_id` collision for URL installs**: Direct-URL installs derive `program_id` from `f"url::{program_name.lower()}"`, where `program_name` comes from the URL's filename stem. Two unrelated URLs sharing the same filename (e.g., `tool.zip` from two different hosts) will silently clobber each other in the registry.
- **Forward-compatibility fragility in `from_dict`**: `ManagedProgram.from_dict` and `UpdatePolicy.from_dict` pass the full JSON payload dict directly to the dataclass constructor (`cls(**payload)`). If a registry written by a future version contains a new field, loading it in an older version raises `TypeError` and crashes. Filtering unknown keys from the payload before construction would make loading more resilient.
- **`GitHubClient` User-Agent version mismatch**: The User-Agent header is hardcoded as `"PortableProgramManager/0.2"` in `github_client.py` while the implementation is at v0.4.

## 13. Potential improvement features

### 13.1 Reliability and maintenance
- Job cancellation for downloads and update checks
- Concurrent job dashboard / queue visibility
- Repair broken install action
- Delete-from-disk uninstall flow
- Retry and backoff policy for repeated update failures

### 13.2 Update-management improvements
- Per-app edit dialog for update policy
- Scheduled interval/daily/weekly checks
- Notify-only / download-only / auto-install behavior
- Update history view
- Rollback to previous cached version
- Version-channel selection per app
- Prerelease handling UI

### 13.3 Security and trust
- Checksum verification when checksums are published
- Signature verification where upstream supports it
- Trust notes in install/update results
- Better visibility into downloaded URLs and selected assets

### 13.4 Discovery improvements
- Search filters
- release availability badge
- portable-confidence score
- favorites/watchlist
- curated sources beyond GitHub

### 13.5 Power-user features
- Launch arguments editor
- Working-directory override editor
- Tags and pinning UI
- Import/export registry
- Backup/restore managed apps metadata
- Packaging to standalone Windows executable

## 14. Recommended next implementation order
1. ~~Fix code-quality gaps from §12.2~~ (Done in v0.5)
2. ~~Add a small Edit Program dialog for update policy, launch args, and notes.~~ (Done in v0.5)
3. ~~Implement due-check evaluation for `app_start` and `interval` schedules.~~ (Done in v0.6)
4. ~~Add delete-from-disk uninstall option.~~ (Done in v0.7)
5. ~~Add run-as-administrator support.~~ (Done in v0.7)
6. ~~Add .7z extraction and ZIP Slip protection.~~ (Done in v0.7)
7. Persist `GitHubRelease.release_id` into `ManagedProgram` and use it for version comparison.
8. Add in-app notifications and better failure messages.
9. Add task cancellation and job queue visibility.
10. Add rollback support and history.

## 15. Code review findings (v0.4)

This section records the results of a full review of the v0.4 codebase against this design document, performed on 2026-03-03.

### 15.1 Design document vs code alignment
The codebase is well-aligned with the design document overall. The following discrepancies were found:

| Item | Finding |
|---|---|
| `workers.py` | Not listed in §4.1 file inventory |
| `ManagedProgram.installed_hash` | Field exists in `models.py` but was absent from §5.1 field list and §5.3 gap list |
| `ManagedProgram.latest_upstream_published_at` | Field exists in `models.py` but not listed explicitly in §5.1 |
| `UpdatePolicy.notify_on_available_update` | Field exists in `models.py` but not listed in §5.1 or surfaced in the settings dialog |
| `GitHubRelease.release_id` | Fetched from the API but not persisted to `ManagedProgram`; §14 mentions improving version comparison with release IDs but did not note this partial availability |
| §6.6 Not yet implemented | "Per-app policy editor in the GUI" was listed twice (duplicate removed) |
| User-Agent version | `github_client.py` still uses `"PortableProgramManager/0.2"` |

### 15.2 Bugs and code quality issues found
All items are documented in §12.2. Priority order for fixing:

1. **High – correctness**: `check_for_updates` mutates program object outside the lock. Low probability of visible corruption in practice due to Python's GIL, but it violates the thread-safety contract the controller is designed to provide.
2. **High – correctness**: `launch_args.split()` will misparse arguments with spaces. Any user with a launch argument containing a path (e.g., `--config "C:\Users\name\my config.ini"`) will get a broken invocation.
3. **Medium – deprecation**: `datetime.utcnow()` raises a `DeprecationWarning` on Python 3.12 and is scheduled for removal. Fix before upgrading to Python 3.14.
4. **Medium – registry integrity**: `program_id` collision for URL installs could silently overwrite an existing entry. Consider incorporating a hash of the source URL into the ID.
5. **Medium – robustness**: `from_dict` forward-compatibility: loading a registry from a newer version crashes. Filter unknown keys defensively.
6. **Low – noise**: Double `progress_callback(100)` emission on install. Cosmetically harmless but unexpected for callers.
7. **Low – housekeeping**: User-Agent string out of date.

### 15.3 Positive observations
- Layering between UI, controller, service, and persistence is clean and consistently respected.
- Thread-safe controller design with `RLock` is correct for all paths except the one noted above.
- `ManagedProgram.from_dict` backward compatibility (missing keys get dataclass defaults) works correctly for the upgrade direction.
- Asset selection heuristics (`pick_portable_asset`) are well-structured and the scoring logic correctly prefers `.zip` > `portable` > `win64` > `windows` > `.exe`.
- `guess_launch_executable` correctly deprioritises setup-looking executables and those deeper in the directory tree.
- Progress reporting from 10% (download start) through 70% (stream) to 85% (complete) to 95% (extract) to 100% gives a smooth UX without over-engineering.
