# Portable Program Manager — Full Code Review

**Date:** 2026-10-08
**Scope:** All source under `src/portable_manager/` (~3,700 lines), the build spec, docs and repo layout. The review was checked against the live registry in `%LOCALAPPDATA%\PortableProgramManager` and the installed apps in `~\Portable Apps`.
**Baseline:** v0.7 (the state after `ANALYSIS_FINDINGS.md`, 2026-04-07).

Legend: **[Confirmed]** means I reproduced it by running code or found evidence on this machine. Everything else comes from reading the code.
Severity: 🔴 High · 🟠 Medium · 🟡 Low

---

## Summary

| Area | 🔴 | 🟠 | 🟡 | Total |
|---|---|---|---|---|
| A. Permissions & managing installed programs | 6 | 3 | 2 | 11 |
| B. Correctness bugs | 6 | 14 | 7 | 27 |
| C. Data safety | 2 | 1 | 1 | 4 |
| D. Performance | 2 | 6 | 4 | 12 |
| E. UI / UX | 3 | 9 | 4 | 16 |
| F. Security | 0 | 2 | 2 | 4 |
| G. Build, repo & docs | 1 | 3 | 4 | 8 |
| **Total** | **20** | **38** | **24** | **82** |

**The five things causing the most pain right now:**
1. Programs that need admin won't launch unless you tick "Run as administrator" by hand (A1). `GoInterruptPolicy.exe` in your registry is one of them.
2. Updates to ZIPs that wrap their files in a versioned folder never take effect. The UI says "Updated" but you keep launching the old exe (B1).
3. Updating or removing a program while it's running, or while it has read-only files, leaves it half-replaced or half-deleted. Nothing is rolled back (A5, A6).
4. When an app has no Windows build, the manager downloads a macOS or Linux build anyway (B3). Your cache still holds a leftover 187 MB `affine-…-macos-arm64.zip` from this.
5. With "close to tray" turned off, closing the window leaves an invisible process running that has no tray icon (B6).

---

## Resolution status (v0.8.0)

All 82 findings were addressed across four pull requests. Each PR has its own tests, and each is stacked on the one before it.

| PR | Scope |
|---|---|
| [#1](https://github.com/AlexStapleton/PortableProgramManager/pull/1) | Phase 1: permissions and the file-handling core |
| [#2](https://github.com/AlexStapleton/PortableProgramManager/pull/2) | Phase 2: update correctness, update modes, GitHub client, permission repair |
| [#3](https://github.com/AlexStapleton/PortableProgramManager/pull/3) | Phase 3: UI refresh |
| #4 | Phase 4: build, CI, docs and the remaining hygiene items |

### Root cause found during implementation: A12 🔴 installed files unreadable by the user's own account **[Confirmed]**
v0.7 extracted archives through `tempfile.mkdtemp()`. Since Python 3.13 that folder gets a **protected ACL** on Windows: Owner Rights, SYSTEM and Administrators only, with inheritance blocked. Files moved out of it keep that ACL. When the manager ran as administrator, the owner was the Administrators group, so the user's normal account lost all access to the program's files. That broke launching, updating and removing it.

On this machine every file of `OpenROM` was affected. This is very likely the "permissions" problem reported before the review.

- **Fix:** staging folders are now created with a plain `mkdir` (#1).
- **Repair:** the app detects affected programs at startup and offers **Repair permissions** (#2). The repair resets ACLs to inherit, and elevates via UAC only when ownership has to be taken back.

### Per-finding status
✅ = fixed · 📝 = deliberate decision, documented

| Area | Finding → resolution | PR |
|---|---|---|
| A1 | ✅ Launch via ShellExecute (`os.startfile`), so `requireAdministrator` apps get a UAC prompt | #1 |
| A2 | ✅ `.ps1` runs through `powershell.exe -File` when elevated too | #1 |
| A3 | ✅ Launch arguments passed through verbatim | #1 |
| A4 | ✅ Launch runs in a worker; the UAC prompt no longer freezes the UI | #1 |
| A5 | ✅ Staged, file-by-file merge with backup and rollback; refuses while the program is running | #1 |
| A6 | ✅ Rename-to-trash first, then registry, then delete (read-only aware, off the UI thread) | #1 |
| A7 | ✅ Unsafe-delete guard and unique install folders per program | #1 |
| A8 | ✅ Write-access check before installing and in Settings | #1, #3 |
| A9 | ✅ Antivirus and permission problems explained on launch | #1, #2 |
| A10 | ✅ `icacls` hardening removed (DPAPI protects the token); 7-Zip runs without a console window | #1 |
| A11 | ✅ "(Administrator)" shown in the title bar when elevated | #1 |
| A12 | ✅ See above | #1, #2 |
| B1, B2 | ✅ Updates extract and flatten in staging, then merge; launch file chosen from the new content; user files kept | #1 |
| B3 | ✅ Non-Windows and wrong-architecture assets dropped; "no Windows build" reported instead | #2 |
| B4 | ✅ Every download gets its own cache folder and is always cleaned up; Settings has Clear cache | #1, #3 |
| B5 | ✅ `/releases/download/` links install the exact asset | #2 |
| B6 | ✅ Closing with close-to-tray off really quits; tray availability checked | #1 |
| B7 | ✅ Token redaction only matches real header values and token shapes | #1 |
| B8 | ✅ Blank launch file asks first; Browse and validation added | #3 |
| B9 | ✅ Version recorded from the release URL's tag | #2 |
| B10 | ✅ An identical download marks the program current | #2 |
| B11 | ✅ The update timer follows the setting live; clearer label | #3 |
| B12 | ✅ All four update modes implemented, with friendly labels | #2, #3 |
| B13 | ✅ Exe ranking: helpers last, name hints, size; updates keep the previous launch file | #2 |
| B14 | ✅ Token-based installer detection (Updater/uninstall no longer count) | #2 |
| B15, B16 | ✅ Rate limits fail fast; a plain 403 isn't retried; quota-aware enrichment | #2 |
| B17 | ✅ Search generation guard; enrichment works on copies | #2, #3 |
| B18 | ✅ Widgets lock when a task is submitted | #1 |
| B19 | ✅ Reinstall keeps customisations and asks first | #2, #3 |
| B20 | ✅ Controller hands copies to the UI; updates commit from a snapshot | #1 |
| B21 | ✅ Background check errors are logged, not shown as dialogs | #3 |
| B22 | ✅ Quitting mid-task asks first | #1 |
| B23 | ✅ Type-safe `from_dict` coercion for every field | #1 |
| B24 | ✅ Content-Length check skipped for encoded responses | #1 |
| B25 | ✅ Superseded single-file exes removed after an update | #1 |
| B26 | ✅ `.7z` extracts into staging; 7-Zip preferred over py7zr | #1 |
| B27 | ✅ Download session closed and sends the app's User-Agent | #1 |
| C1 | ✅ Bad entries skipped and saved to `programs.rejected-*.json`; rolling `programs.json.bak` | #1 |
| C2 | ✅ Single-instance guard; a second launch brings the window forward | #1 |
| C3 | ✅ Timestamped corrupt backups, never overwritten | #1 |
| C4 | ✅ Rotating `app.log` plus uncaught-exception logging | #1 |
| D1, D2 | ✅ Parallel release lookups and update checks; one registry save per batch | #2 |
| D3, D5 | ✅ Remove and launch run in workers; no lock held during deletion | #1 |
| D4 | ✅ Update install fetches the release once | #2 |
| D6 | ✅ UPX disabled (antivirus false positives, slow start-up); version resource added | #4 |
| D7 | ✅ Table is now a model/view with cached icons and folder checks | #3 |
| D8 | 📝 `fsync` kept for registry integrity; the cost is now paid once per batch (D2) instead of per program | #2 |
| D9 | ✅ `py7zr` imported lazily | #1 |
| D10 | ✅ Old `GitHubClient` sessions closed | #1 |
| D11 | ✅ Window icon set once | #1 |
| D12 | 📝 The installer-detection pass still walks the folder once more after install; it runs only on install and costs milliseconds | — |
| E1–E16 | ✅ Theme that follows Windows (with dark title bar), new Programs/Discover tabs, dialogs, tray launcher, shortcuts, remembered layout, empty states, running-state detection | #3 |
| F1 | ✅ Downloads verified against GitHub's published SHA-256 | #2 |
| F2 | ✅ Removed `_quote_args_for_shell` | #1 |
| F3 | 📝 Arguments to `.bat` files are interpreted by `cmd.exe`; acceptable because only the user enters them | — |
| F4 | 📝 Mark-of-the-Web is not written; SmartScreen prompts on every launch of a managed portable app would defeat the purpose. Downloads are HTTPS-only and digest-verified instead | — |
| G1 | ✅ pytest suite (360+ tests) and a GitHub Actions workflow | #1–#4 |
| G2, G3 | ✅ README rewritten with build/run/troubleshooting; stale `main.spec` and `Install Instructions.txt` removed | #4 |
| G4 | ✅ `psutil` declared; dev requirements; version bounds | #1, #4 |
| G5 | ✅ `__version__` (0.8.0) used for the User-Agent, the About dialog and the exe version resource | #1, #4 |
| G6 | ✅ Duplicate root icon removed | #4 |
| G7 | ✅ Design doc changelog updated for v0.8 | #4 |
| G8 | ✅ `.gitignore` covers build output, venvs, caches and agent worktrees | #1 |

---

## A. Permissions & managing installed programs

### 🔴 A1. Programs that need admin fail with WinError 740 **[Confirmed]**
`installer.py:650`. Normal launches go through `subprocess.Popen` → `CreateProcess`. CreateProcess **cannot** start an exe whose manifest says `requireAdministrator`; it fails with `ERROR_ELEVATION_REQUIRED (740)`. The user then sees *"Failed to launch program: [WinError 740] The requested operation requires elevation"*.
- Evidence: `GoInterruptPolicy.exe` contains `requestedExecutionLevel level="requireAdministrator"`, and its registry entry has `run_as_admin: false`. `CoreCycler` and `ZenTimings` need admin too.
- **Fix:** launch through ShellExecuteEx (`os.startfile(path, "open", arguments, cwd)` on Python 3.10+). ShellExecute reads the manifest and shows the UAC prompt automatically, the same way Explorer does. Use `"runas"` only when the user forces elevation.

### 🔴 A2. "Run as administrator" breaks for `.ps1` launch targets
`installer.py:633`. `ShellExecuteW(..., "runas", "script.ps1", ...)` hands the script to the shell's file association, which for `.ps1` is Notepad/Edit. It fails or opens an editor. The admin path also skips the `cmd /c` and `powershell -File` handling that the non-admin path has.
- **Fix:** for `.ps1`, elevate `powershell.exe` with `-ExecutionPolicy RemoteSigned -File "<script>" <args>`.

### 🔴 A3. Launch arguments containing quotes are corrupted **[Confirmed]**
`installer.py:625`. `shlex.split(..., posix=False)` keeps the quote characters inside tokens:
`--config "C:\path with spaces\cfg.ini"` → `['--config', '"C:\\path with spaces\\cfg.ini"']`. `Popen` then escapes those quotes, so the program receives an argument that literally includes `"` characters. **The example in the Edit dialog's placeholder hits this bug.** Unbalanced quotes raise an unhandled `ValueError("No closing quotation")`.
- The S2 "fix" (`_quote_args_for_shell`) rests on a false premise. ShellExecute does not pass parameters through `cmd.exe` when the target is an `.exe`, so `& | >` were never shell-interpreted there. Wrapping every token in quotes also changes how some programs parse their flags.
- **Fix:** keep `launch_args` as the raw command-line string, because Windows programs parse their own command line. Pass it as-is to ShellExecute, or append it to `list2cmdline([exe])`.

### 🔴 A4. Launching freezes the UI during the UAC prompt
`main_window.py:607` → `controller.run_program` → `ShellExecuteW` on the GUI thread. ShellExecute with elevation blocks until the UAC dialog is answered, and an `fsync` of `programs.json` follows. The whole window stays frozen ("Not responding") until then.
- **Fix:** launch from a worker thread, and save `last_run_at` lazily.

### 🔴 A5. Updating a running program leaves a half-replaced install, with no rollback
`installer.py:571-579` and `598-604`. The update moves extracted items over the live folder one at a time. `rmtree`/`unlink` on a locked file (the running exe, a loaded DLL, an open log) raises halfway through the loop. Some files are then new and some old, and the program usually won't start. Nothing is backed up or restored, and the registry records `update_failed` against a broken folder.
- **Fix:** (1) Check whether the program is running (`psutil` is already installed as a py7zr dependency) and ask the user to close it. (2) Extract to a staging folder. (3) Apply the update as a transaction: back up each file before replacing it, and roll back on any error.

### 🔴 A6. Remove + delete files can leave orphaned, half-deleted folders
`controller.py:112-127`.
1. The registry entry is removed **before** deletion. If deletion fails, the program disappears from the manager while its files remain partly on disk.
2. `shutil.rmtree` does not clear the read-only attribute, so any read-only file causes `PermissionError: [WinError 5] Access is denied`. Read-only files are common in portable apps (git folders, bundled runtimes).
3. Files locked by a running process fail the same way, partway through.
4. The deletion runs on the UI thread while holding the controller lock.
- **Fix:** first rename the folder to a `.ppm_trash_*` sibling. The rename is atomic and fails cleanly if anything is in use, so the user gets "close X first". Then update the registry, then `rmtree` the trash folder with an `onexc` handler that clears read-only and retries. Run it all in a worker.

### 🟠 A7. No safety guard on the folder being deleted
`controller.py:122`. `rmtree(install_dir)` has no sanity check. A hand-edited or corrupted `programs.json`, or two programs pointing at the same folder, can delete unrelated data.
- **Collision case:** two direct-URL installs with the same file stem (`tool.zip` from two hosts) get the same `install_dir` (`installer.py:164-165`) but different `program_id`s. Removing one with "delete files" deletes the other's files too.
- **Fix:** refuse to delete a drive root, the home folder or its parents, the install root itself, the Windows folder, or a folder that another managed program uses.

### 🟠 A8. Install root isn't checked for write access
`storage.py:216-223` swallows `mkdir` errors, and the Settings dialog accepts any path. If the install root is under `Program Files` (or another protected folder), every install later fails mid-way with a raw `[WinError 5] Access is denied`.
- **Fix:** test write access when Settings is saved and before each install, and show a clear message ("this folder needs administrator rights, pick one in your user profile").

### 🟠 A9. Antivirus quarantine is reported as a generic failure **[Confirmed]**
Defender quarantined `~\Portable Apps\KytyPS5\launcher.exe` on Oct 4. `OpenROM\gui.exe`, installed today, currently returns *Access is denied* even to `icacls`. The manager shows "Launch path does not exist" or "Failed to launch program", which gives the user nothing to act on.
- **Fix:** when a launch hits `ERROR_ACCESS_DENIED` (5) or `ERROR_VIRUS_INFECTED` (225), or the exe vanished after install, explain that antivirus probably blocked it and point to Windows Security → Protection history.

### 🟡 A10. The `icacls` ACL hardening misbehaves
`storage.py:261-283`.
- In the frozen windowed exe, `subprocess.run(["icacls", …])` without `CREATE_NO_WINDOW` **flashes a console window** on every settings save. The `7z.exe` fallback at `installer.py:508` does the same.
- `/inheritance:r` removes SYSTEM and Administrators access, which can break backup and AV tools.
- A bare `%USERNAME%` can resolve to the wrong account on a domain-joined machine.
- Your `settings.json` currently has only inherited ACEs, so the hardening isn't actually in effect.
- DPAPI already protects the token, so this adds little. Either remove it or use the user's SID with `CREATE_NO_WINDOW`.

### 🟡 A11. No indication when the manager itself runs elevated
If the manager was started as admin, every program it launches silently inherits admin. Show "(Administrator)" in the title bar.

---

## B. Correctness bugs

### 🔴 B1. Updates to versioned-folder ZIPs never take effect **[Confirmed]**
`installer.py:325`, `584-609`, `344-365`. On first install, `App-1.0/` is flattened into the install folder. On update, the install folder is no longer empty, so the new `App-2.0/` folder lands **beside** the old files and `_flatten_single_subdir` skips it because there's more than one entry. `guess_launch_executable` then prefers the shallowest exe, which is the **old** one.
- Reproduced: after the "update", `launch_path` → `App\App.exe` still contains `1.0`, and the folder holds `['App-2.0', 'App.exe', 'data']`. The UI shows "Updated to 2.0".
- **Fix:** extract and flatten in a staging folder, then merge the result into the install folder.

### 🔴 B2. The overlay update deletes user data in matching folders
`installer.py:574-578`, `598-603`. When an archive contains a top-level folder that already exists (`data/`, `config/`, `profiles/`, `plugins/`), the code runs `rmtree` on the **whole** existing folder before moving the new one in. Settings, saves and plugins in that folder are lost. B1 currently hides this by sending updates to a subfolder; fixing B1 naïvely would expose it.
- **Fix:** merge file by file (replace files that exist in the archive, keep everything else).

### 🔴 B3. The asset picker downloads non-Windows builds **[Confirmed]**
`installer.py:413-431`. Non-Windows patterns subtract 500 points but the asset stays a candidate, so when a release has no Windows asset the "best" macOS or Linux build is downloaded. `arm64` also gets the penalty, so Windows-on-ARM builds are never picked even on ARM machines.
- Evidence: an orphaned `cache\affine-0.26.3-stable-macos-arm64.zip` (187 MB).
- **Fix:** drop assets with a negative OS score and report "No Windows build in the latest release".

### 🔴 B4. Download cache leaks on failure **[Confirmed]**
`installer.py:94-96`, `166-168`, `234-235`. `_cleanup_cached_file` runs only after a successful extraction. Any extraction or validation error leaves the full download in the cache forever (the 187 MB file above).
- **Fix:** clean up in `try/finally`, and add a "Clear cache" button showing the cache size in Settings.

### 🔴 B5. Pasted GitHub asset URLs install the wrong file **[Confirmed]**
`github_client.py:477` and `main_window.py:582`. `REPO_URL_RE.match()` matches a prefix, so `https://github.com/foo/bar/releases/download/v1.2/bar-win64.zip` is read as repo `foo/bar`. The app then installs the *latest* release with an auto-picked asset instead of the file the user pasted.
- **Fix:** treat `/releases/download/` URLs as direct assets.

### 🔴 B6. Closing the window can leave an invisible process running
`app.py:34` always calls `setQuitOnLastWindowClosed(False)`. When "close to tray" is off, `closeEvent` accepts the close and hides the tray icon (`main_window.py:1061-1066`) but never calls `quit()`. The process keeps running with no window and no tray icon until it's killed in Task Manager. The same happens with minimize-to-tray on a system with no tray (`QSystemTrayIcon.isSystemTrayAvailable()` is never checked).

### 🟠 B7. The error sanitizer corrupts messages **[Confirmed]**
`workers.py:449`. The regex `(Bearer|token|Basic)\s+\S+` matches the plain word "token". The rate-limit hint *"Set a GitHub token in Settings…"* comes out as *"Set a GitHub token [REDACTED] Settings…"*.
- **Fix:** redact only real header values (`Bearer …`, `Basic …`) and GitHub token shapes (`ghp_…`, `github_pat_…`).

### 🟠 B8. "Leave blank to keep current" actually clears the launch path
`edit_program_dialog.py:43` and `181-183` → `controller.py:146`. A blank field returns `None`, which is saved, and the program can no longer be run.

### 🟠 B9. Direct-URL installs show "Update available" immediately
`installer.py:185-202` never sets `version`, so `_is_update_available` (`controller.py:442`) treats any upstream tag as newer than `""`. The tag is right there in the URL (`/releases/download/<tag>/…`).

### 🟠 B10. An update with an identical hash is treated as a failure, every time
`installer.py:224-233`. The update is marked `update_failed`, and the next check flags it as available again. The fix is to accept the new tag as the current version and report "up to date".

### 🟠 B11. Scheduled checks depend on an unrelated toggle and ignore setting changes
`main_window.py:112-118`. The 30-minute timer is created only if *Run update checks on startup* was on **when the app launched**. Changing the setting does nothing until restart. If it's off, every per-program schedule (interval, daily, weekly) silently never runs, and the label doesn't say so.

### 🟠 B12. Three of the four update modes do nothing
`edit_program_dialog.py:70-76`. `notify_only`, `download_only` and `install_automatically` can be selected (auto-install even shows a security warning), but the controller never acts on them. "Enable system notifications (future use)" is in the same state. Either implement them (notify is cheap: a tray balloon) or hide them.

### 🟠 B13. The wrong executable gets picked as the launch target **[Confirmed]**
`installer.py:356-357`. The shortest file name wins, and only `setup` is ranked lower. That picks `Update.exe`, `unins000.exe`, `crashpad_handler.exe`, `vc_redist.x64.exe` and so on.
- Evidence: your SCSKiller entry launches `Update.exe` (a Squirrel stub) instead of `SCSKiller.exe`.
- **Fix:** prefer an exe whose name matches the repo or program name, and rank updaters, uninstallers, helpers and redistributables lower.

### 🟠 B14. Installer detection gives false positives
`installer.py:37` and `379`. The substrings `update` and `install` match `Updater.exe`, `uninstall.exe`, `AutoUpdate.exe`, and so on. A real portable app then gets flagged as an installer and loses its update checks.

### 🟠 B15. Rate-limit handling stalls everything
`github_client.py:546-595` and `controller.py:57-73`.
- Each search makes 1 search call plus up to 50 **sequential** `/releases/latest` calls. Unauthenticated users get 60 core calls per hour, so two or three searches use up the quota.
- After that, `_request` sleeps up to 60 s × 3 retries **per repo**, so a worker thread can be stuck for many minutes on "Fetching release info".
- **Fix:** fail fast when the reset is more than a few seconds away, stop the extra lookups when the remaining quota is low, and run lookups in parallel.

### 🟠 B16. A genuine 403 is retried with backoff
`github_client.py:561`. When the response has no `X-RateLimit-Remaining` header (a real permission error, an SSO-protected repo), it's treated as a rate limit and retried 3 times.

### 🟠 B17. An older search can overwrite a newer one
`main_window.py:397-422`. Nothing tracks which search a response belongs to. If you search "A" and then "B", A's results or release info can arrive later and replace B's.

### 🟠 B18. A fast double-click can start the same job twice
`main_window.py:935` and `942-948`. Buttons are disabled in the *queued* `started` signal, not when the task is submitted. A fast double-click can therefore start two installs into the same folder at once.

### 🟠 B19. Reinstalling silently overwrites a managed program and wipes your customisations
`controller.py:448-457`. Installing a repo that's already managed gives no prompt, and `_upsert_program` replaces the whole record. Custom name, notes, launch args, run-as-admin and update policy are all lost.

### 🟠 B20. Shared mutable state across threads
`controller.py:158-160`. `list_programs()` returns the live objects. `install_available_update` passes the live object to `download_and_install_release`, which **mutates it outside the lock** (`installer.py:236-248`) while the UI thread reads it.

### 🟡 B21. Background-check errors open modal dialogs while the app sits in the tray
`main_window.py:955` and `1084`. Errors from scheduled checks call `QMessageBox.warning`, so the user comes back to a stack of dialogs. Background errors should go to the log, the status bar and the row status (or a tray balloon).

### 🟡 B22. Quitting mid-install kills the job with no warning
`main_window.py:1035-1039`. Tray → Quit tears down the app while an extraction is still running, which leaves a broken install. Ask for confirmation, or wait.

### 🟡 B23. Malformed JSON can crash a dialog
`models.py:68-73` and `119-130`. Field types aren't validated. A `null` or string `interval_hours` crashes `QSpinBox.setValue` (`edit_program_dialog.py:94`). The controller only repairs these values when Settings is saved.

### 🟡 B24. Content-Length check fails on compressed responses
`installer.py:293`. When a server sends `Content-Encoding: gzip`, `requests` decompresses the stream, so the downloaded byte count never matches the header.

### 🟡 B25. Old single-file exes pile up
`installer.py:337-338`. When the asset name includes a version (`tool-1.2.exe` → `tool-1.3.exe`), each update leaves the previous exe behind.

### 🟡 B26. `.7z` extraction skips staging, and the slower extractor is tried first
`installer.py:490-494`. py7zr extracts straight into the install folder, so A5 and B1 apply to `.7z` archives too. py7zr is also much slower than `7z.exe`, which should be preferred when it's installed.

### 🟡 B27. Two different download sessions
`installer.py:266`. `_download` creates a `requests.Session` that's never closed, and it doesn't send the client's User-Agent. Some hosts reject the default `python-requests` agent.

---

## C. Data safety

### 🔴 C1. One bad registry entry wipes the whole program list
`storage.py:237-244`. If a single entry fails `from_dict`, the **whole** `programs.json` is renamed to `.corrupt` and the registry starts empty. From the user's point of view, every managed program just disappeared.
- **Fix:** skip and log only the bad entries, and keep a rolling `programs.json.bak` before each write.

### 🔴 C2. No single-instance guard
This is a tray app, so starting it again from the Start menu or a shortcut while it sits in the tray is easy. The two processes each keep their own in-memory registry, and whichever writes last wins, so installs and edits made in the other one are lost.
- **Fix:** use a `QLocalServer` lock and bring the existing window to the front instead.

### 🟠 C3. A second corruption overwrites the first backup
`storage.py:254-256`. Backups should get a timestamp.

### 🟡 C4. Logging goes nowhere
Logging is never configured, so every `log.warning` and `log.error` (DPAPI failures, ACL failures, identical-hash updates) is discarded. A rotating `app.log` in the app data folder is essential for diagnosing permission problems.

---

## D. Performance

### 🔴 D1. `fetch_release_dates` runs sequentially
`controller.py:57-73`. Twenty to fifty network round trips happen one after another on each search (≈4–10 s). Use `ThreadPoolExecutor(6)`. (Was P2.)

### 🔴 D2. `check_for_updates_all` and `check_due_updates` run sequentially
`controller.py:267-356`. Each program is checked one at a time, and each check does a full JSON write with `fsync`. Run the checks in parallel and save once at the end. (Was P1 and P3.)

### 🟠 D3. `remove_program` holds the lock during `rmtree`
`controller.py:113-127`. This blocks the whole controller and runs on the UI thread. (Was P4.)

### 🟠 D4. The same release is fetched twice during an update
`controller.py:359` and `378`: once by `install_available_update` → `check_for_updates`, and again right after. (Was P5.)

### 🟠 D5. `run_program` does disk I/O and `fsync` on the UI thread
(Was P6; see A4.)

### 🟠 D6. The onefile + UPX build is slow to start and attracts antivirus
`PortableProgramManager.spec`. The 55 MB onefile exe unpacks to `%TEMP%` on **every launch**, which means a slow cold start and a full AV scan each time. UPX-packed Qt DLLs are a well-known trigger for Defender false positives, which matters given the Defender activity on this machine. Recommend `upx=False` and a onedir build (zipped for release), or at least `upx=False`.

### 🟠 D7. The table is rebuilt from scratch on every refresh
`main_window.py:805-846`. Each refresh creates new `QTableWidgetItem`s for 10 columns, runs a `Path.is_dir()` per row on the UI thread (slow for network drives), and deep-copies the selected program. That happens on every keystroke after the debounce and after every task. (Was P7 and P13.)

### 🟠 D8. `os.fsync` on every registry write
`storage.py:300`. This is expensive for data like `last_run_at` that isn't critical. (Was P8.)

### 🟡 D9. `py7zr` is imported at module import time
It adds about 50–100 ms to every startup even though it's rarely needed. (Was P15.)

### 🟡 D10. The old `GitHubClient` session isn't closed when settings change
`controller.py:166`. (Was P16.)

### 🟡 D11. The window icon is created twice
`app.py:30` and `main_window.py:88`. (Was P14.)

### 🟡 D12. `guess_launch_executable` and `looks_like_installer` each walk the tree
They both walk the install folder, and `rglob("*.exe")` runs a second time. Walk it once.

---

## E. UI / UX

### 🔴 E1. Dark theme is half-applied
`theme.py` and `app.py:31-32`. The stylesheet runs on Fusion with the **default light palette**, so anything Fusion draws natively stays light: checkbox indicators, scrollbars, spin-box arrows, combo pop-up borders, menu highlights. The **Windows title bar stays white** over a dark app.
- **Fix:** set a dark `QPalette` that matches the stylesheet, and turn on the DWM immersive dark title bar (or follow the system light/dark setting).

### 🔴 E2. No double-click, right-click menu or keyboard shortcuts in the program list
Every action needs *select row → move to the button row → click*. Expected behaviour:
- double-click or Enter runs the program
- right-click gives Run / Run as admin / Open folder / Edit / Check / Update / Remove
- Del removes, F5 refreshes, Ctrl+F jumps to the filter

### 🔴 E3. The Managed Programs table is cramped and full of internals
`main_window.py:311-331`.
- It has 10 columns, including the raw `github_repo` source type, the full install path and notes, so it scrolls sideways even at 1400 px.
- It can't be sorted.
- Row numbers show.
- There are no program icons.
- **Suggestion:** icon + Name, Version, Status (coloured), Last run, Source (friendly name). Keep the rest in the details pane, make the table sortable, and remember column widths.

### 🟠 E4. Action buttons ignore selection and state
"Install Update" is enabled when there's no update, "Run" when there's no launch path, and "Remove" when nothing is selected. Clicking any of them just opens a warning dialog.

### 🟠 E5. "Remove" looks exactly like "Run"
Every button has the same blue style, including the destructive one. Add primary, secondary and danger styles, plus icons.

### 🟠 E6. The details pane is a raw field dump
`main_window.py:848-888`. It shows `Program ID: repo::x`, `Source ID`, `Installed Asset Name` and raw ISO timestamps. It should be a formatted view: clickable homepage and folder links, friendly dates, and the last error highlighted.

### 🟠 E7. No empty state
On first run the Managed tab shows an empty grid. It should say "No programs yet" and offer a button to open Discover.

### 🟠 E8. Window size is hard-coded
`main_window.py:89`. A fixed 1400×900 overflows common 1366×768 and 1280×800 laptops. Geometry, splitter position, the last tab and column widths are never saved.

### 🟠 E9. The Settings dialog shows internal values
`settings_dialog.py`.
- It shows raw enum strings (`manual`, `notify_only`, `app_start`, `install_automatically`).
- It has no Browse buttons for folders, and no way to show or test the token.
- It doesn't explain that changing the install root won't move existing apps.
- It mixes general, update and tray options in one long form.

### 🟠 E10. The Edit dialog has no Browse buttons or validation
There's no file picker for the launch path or working folder, and no check that the launch path exists or that its extension can run.

### 🟠 E11. The Discover tab layout could be better
- The activity log sits inside the "Install from URL" card and takes most of the vertical space.
- Search results don't mark repos you've already installed.
- `has_windows_release` is computed but never shown.
- Stars sort as text.
- There's no double-click or right-click menu.
- There's no preview of which asset will be installed (F-01 in the improvement doc).

### 🟠 E12. The "Installer detected" dialog is a dead end
`main_window.py:551-561`. It only informs. It should offer **Run installer now** and **Open folder** buttons.

### 🟡 E13. The tray menu is minimal
The tray menu only has Show/Hide and Quit. A **Launch ▸** submenu (pinned programs first) and **Check for updates** would make the tray genuinely useful. The model already has `pinned` and `tags` fields that nothing uses.

### 🟡 E14. Feedback is easy to miss
- Status messages disappear after 8 s.
- The download progress shows only KB, with no total, speed or percentage text.
- There's no way to cancel a long download.

### 🟡 E15. Can't see which programs are running
The table doesn't show which managed programs are currently running (psutil can tell). The update and remove flows need this too (A5, A6).

### 🟡 E16. The toolbar is bare
It has three text-only actions and no icons. Settings deserves a gear icon, and "Check all for updates" belongs here.

---

## F. Security (beyond the earlier S-list)

### 🟠 F1. Downloads aren't checked against GitHub's published digest
The GitHub releases API now includes a `digest` (`sha256:…`) for each asset. Comparing the download against it is cheap, real integrity checking. The current "identical hash" check (S5) doesn't verify integrity.

### 🟠 F2. `_quote_args_for_shell` adds no protection and changes behaviour
See A3. Remove it.

### 🟡 F3. Arguments to `.bat` files pass through cmd metacharacter parsing
`installer.py:642`. With `cmd /c`, any `& | >` in the arguments is interpreted. The arguments come from the user, so the risk is low, but it's worth documenting.

### 🟡 F4. Mark-of-the-Web is never written
Downloaded exes never trigger SmartScreen. That may be intended, but it should be a conscious decision and written down.

---

## G. Build, repo & docs

### 🔴 G1. No automated tests
Asset picking, launch-path guessing, flatten and overlay, version comparison, URL parsing and argument handling are all pure functions that are easy to test. Every confirmed bug above would have been caught by a small unit test.

### 🟠 G2. README is out of date
It says `.7z` isn't extracted (it is), and it doesn't mention the tray, the scheduler or the build steps.

### 🟠 G3. Build instructions point to a path that no longer exists
`Install Instructions.txt` hard-codes `I:\Coding Projects\…`, but the folder is now `Coding-Projects`. Also, `main.spec` is a stale duplicate of `PortableProgramManager.spec`.

### 🟠 G4. Dependencies are incomplete
`requirements.txt` doesn't list `psutil` (needed for the fixes above) or a dev group (`pyinstaller`, `pytest`). Nothing is pinned for reproducible builds.

### 🟡 G5. No single version string
The version is hard-coded in the User-Agent (`github_client.py:524`, "0.7") and in the docs. Add `__version__` and show it in the title bar or an About box.

### 🟡 G6. Duplicate and oversized icon assets
The root `icon_transparent.ico` duplicates `ui/icons/icon_transparent.ico`, and `PortableProgramManager_icon_transparent.png` is 585 KB but only used as a last-resort fallback.

### 🟡 G7. Docs don't match the code
`DESIGN_DOCUMENT.md` v0.7 says `rmtree` runs "outside the lock"; the code runs it inside. It also says PowerShell runs with `-ExecutionPolicy Bypass`; the code uses RemoteSigned.

### 🟡 G8. Leftover debris in the project folder
`__pycache__/` sits at the repo root, plus `build/`, `dist/` and `.venv/`. They're now covered by `.gitignore`.

---

## Suggested fix order

1. **Permissions pass** (A1–A9, B6): ShellExecute launch, in-use detection, transactional update, safe remove, write-access checks, AV messaging, single instance.
2. **Update correctness** (B1–B5, B9–B14, B19): staging and merge, Windows-only asset filter, cache cleanup, URL handling, version inference, better exe picking.
3. **UI pass** (E1–E16): palette and title bar, context menu and shortcuts, slimmer sortable table with icons, formatted details pane, friendly Settings and Edit dialogs, remembered layout, tray launcher.
4. **Performance** (D1–D12) and **data safety** (C1–C4).
5. **Tests and repo hygiene** (G1–G8), alongside each step.
