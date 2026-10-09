# Portable Program Manager — Deep Analysis Findings

**Date:** 2026-04-07
**Version Analyzed:** v0.7

---

## Security Findings (16 issues)

### CRITICAL / HIGH

| # | Finding | Location | Status |
|---|---------|----------|--------|
| S1 | **GitHub token stored in plaintext JSON** — any local process can read it | `models.py:94`, `storage.py:61` | FIXED — DPAPI encryption via `credential_store.py` |
| S2 | **`ShellExecuteW` receives raw unsanitized `launch_args`** — shell metacharacter injection when running as admin | `installer.py:545-546` | FIXED — args parsed with shlex then quoted per-token |
| S3 | **Symlink entries skipped in 7z validation** — on Win11 with Developer Mode, symlinks can write outside install dir | `installer.py:413-414` | FIXED — symlinks now rejected outright |

### MEDIUM

| # | Finding | Location | Status |
|---|---------|----------|--------|
| S4 | HTTP downloads allowed (MITM risk) | `installer.py:126` | FIXED — only HTTPS allowed |
| S5 | No download integrity verification against known hashes | `installer.py:214-255` | FIXED — identical-hash detection rejects replayed downloads |
| S6 | 7z validation failure is non-fatal — silent bypass of path traversal protection | `installer.py:422-428` | FIXED — now fails closed (raises InstallError) |
| S7 | TOCTOU race in zip extraction path check | `installer.py:484-497` | FIXED — extract to staging dir, validate, then move |
| S8 | Auto-install update mode design risk (silent code execution surface) | `edit_program_dialog.py:73` | FIXED — confirmation dialog warns user of security implications before enabling. Note: the auto-install execution path is not yet implemented in the controller; the UI option and stored policy value are placeholders for a future feature. |
| S9 | PowerShell `-ExecutionPolicy Bypass` circumvents security boundary | `installer.py:557-561` | FIXED — changed to RemoteSigned |
| S10 | DLL hijacking via working directory set to install dir | `installer.py:535` | WONTFIX — inherent to Windows DLL search order |
| S11 | py7zr has path traversal history + skip-on-failure undermines protection | `installer.py:422-428` | FIXED — covered by S3+S6 (symlinks rejected, fail closed) |

### LOW

| # | Finding | Location | Status |
|---|---------|----------|--------|
| S12 | API path injection via unsanitized `full_name` | `github_client.py:154` | FIXED — regex validation on all API methods |
| S13 | URL-derived filename not sanitized for Windows reserved names (CON, PRN, etc.) | `installer.py:130` | FIXED — `_sanitize_filename()` applied |
| S14 | Token potentially in exception messages | `github_client.py:63`, `workers.py:31` | FIXED — `_sanitize_error()` redacts auth tokens |
| S15 | Unrestricted redirect following (SSRF risk) | `installer.py:226` | FIXED — max 10 redirects, final URL must be HTTPS |
| S16 | No restrictive ACLs on settings/registry files | `storage.py:100-108` | FIXED — `icacls` restricts settings.json to current user |

---

## Performance Findings (18 issues)

### HIGH

| # | Finding | Location | Status |
|---|---------|----------|--------|
| P1 | **`save_programs` called excessively** — 20x full JSON serialize+fsync during bulk update checks | `controller.py` (multiple sites) | TODO |
| P2 | **`fetch_release_dates` makes N sequential HTTP requests** — ~4s for 20 repos | `controller.py:57-73` | TODO |
| P3 | **`check_for_updates_all` is sequential** — 40s+ for 20 programs | `controller.py:267-283` | TODO |
| P4 | **RLock held during `shutil.rmtree`** — blocks entire controller during potentially long delete | `controller.py:113-127` | TODO |

### MEDIUM

| # | Finding | Location | Status |
|---|---------|----------|--------|
| P5 | `install_available_update` fetches same release twice (redundant API call) | `controller.py:359,378` | TODO |
| P6 | `run_program` blocks UI thread with disk I/O and fsync | `main_window.py:607` | TODO |
| P7 | `Path.is_dir()` check per program on UI thread during table refresh | `main_window.py:821` | TODO |
| P8 | `os.fsync` on every atomic write is expensive for non-critical data | `storage.py:107` | TODO |
| P9 | Full JSON serialization of all programs on every save | `storage.py:78-79` | TODO |
| P10 | `list_programs` shares mutable objects across threads (thread-safety bug) | `controller.py:158-160` | TODO |
| P11 | Downloads bypass authenticated session (miss connection pooling + rate limit benefits) | `installer.py:226` | TODO |

### LOW

| # | Finding | Location | Status |
|---|---------|----------|--------|
| P12 | `get_program` deep-copies for read-only access | `controller.py:154-156` | TODO |
| P13 | New `QTableWidgetItem` created for every cell on every refresh | `main_window.py:1172-1175` | TODO |
| P14 | Window icon created twice at startup | `app.py:30`, `main_window.py:88` | TODO |
| P15 | `py7zr` imported eagerly at module level (~50-100ms startup cost) | `installer.py:13` | TODO |
| P16 | Old GitHubClient session not closed on replacement | `controller.py:37-40` | TODO |

---

## Security Priority Fixes

### Fix 1: Encrypt GitHub token at rest
- Use `keyring` library or Windows DPAPI instead of plaintext JSON
- Set restrictive ACLs on `settings.json`

### Fix 2: Reject symlinks in 7z archives & fail closed on validation failure
- Don't skip symlinks — reject archives that contain them
- If `_validate_7z_members` can't read the archive index, refuse extraction instead of proceeding

### Fix 3: Reject HTTP URLs
- Only allow HTTPS in `install_from_url`

### Fix 4: Sanitize `launch_args` for ShellExecuteW
- Quote/validate each argument before passing to the shell

---

## Performance Priority Fixes

### Fix 1: Batch `save_programs`
- Add a dirty flag; bulk operations write to disk once at the end

### Fix 2: Parallelize HTTP requests
- Use `ThreadPoolExecutor(max_workers=5)` in `fetch_release_dates` and `check_for_updates_all`

### Fix 3: Release RLock before `shutil.rmtree`
- Do registry update under lock, delete files outside it

### Fix 4: Offload `run_program` to background worker
- Prevent fsync from blocking the UI thread
