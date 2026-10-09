# Portable Program Manager — Improvement Design Document

## Document Status
- **Scope:** Product, architecture, UX, reliability, and implementation roadmap improvements
- **Overall Status:** Active planning
- **Document Owner:** User / project maintainer
- **Last Updated:** 2026-03-04

---

## Status Legend
- **Not Started** — No implementation work has begun
- **Planned** — Agreed direction, not yet implemented
- **In Progress** — Partial implementation underway
- **Implemented** — Built and present in codebase
- **Deferred** — Valuable, but intentionally postponed
- **Needs Review** — Implemented but should be validated or refined

## Priority Legend
- **P0** — Critical for correctness, trust, or core product stability
- **P1** — High-value feature or major UX/reliability improvement
- **P2** — Important enhancement with moderate impact
- **P3** — Nice-to-have or longer-term improvement

---

# 1. Product Goals

## 1.1 Purpose
The Portable Program Manager should provide a reliable, user-friendly Windows application for discovering, installing, launching, updating, and managing portable applications, primarily from GitHub releases.

## 1.2 Core Product Outcomes
- Make installing portable applications easy and understandable
- Reduce incorrect installs caused by ambiguous release assets
- Improve trust and safety when downloading executables
- Make managed applications easy to monitor, repair, update, and remove
- Build a strong foundation for future sources, automation, and recovery workflows

## 1.3 Current Assessment
The current project already has a strong modular foundation, background worker support, installer logic, and a reasonably rich managed-program data model. The biggest opportunities are now in trust, install correctness, lifecycle recovery, and long-term persistence architecture.

---

# 2. Improvement Roadmap Summary

| ID | Improvement | Status | Priority | Category |
|---|---|---:|---:|---|
| F-01 | Install preview and manual asset selection | Planned | P0 | Core UX / Correctness |
| F-02 | Checksum and trust verification | Planned | P0 | Security / Trust |
| F-03 | Repair / relink / redetect actions | Planned | P1 | Recovery / Reliability |
| F-04 | Transactional install and update staging | Planned | P0 | Reliability |
| F-05 | Rollback support | Planned | P1 | Reliability |
| F-06 | Cancellation support for background jobs | Planned | P1 | Performance / UX |
| F-07 | SQLite migration from JSON registry | Planned | P1 | Architecture / Persistence |
| F-08 | Per-program history and event log | Planned | P1 | Observability |
| F-09 | Notification system improvements | Planned | P2 | UX |
| F-10 | Managed Programs tab enhancement | Planned | P1 | UX |
| F-11 | Update matching logic improvements | Planned | P0 | Correctness |
| F-12 | Network resilience improvements | Planned | P1 | Reliability |
| F-13 | Main window UI modularization | Planned | P1 | Maintainability |
| F-14 | Service layer refactor | Planned | P2 | Architecture |
| F-15 | Watchlist / favorites without install | Planned | P2 | Product |
| F-16 | Import / export / backup | Planned | P2 | Portability |
| F-17 | Portable confidence scoring in search | Planned | P2 | Discovery UX |
| F-18 | Automated test coverage expansion | Planned | P0 | Engineering Quality |

---

# 3. Detailed Improvement Specifications

## F-01. Install Preview and Manual Asset Selection
- **Status:** Planned
- **Priority:** P0
- **Category:** Core UX / Correctness

### Problem
The current installer relies heavily on heuristics to choose the correct downloadable asset. This can fail when repositories publish multiple Windows assets, separate installer and portable builds, or mixed architectures.

### Goal
Add an install preview step that makes the selected release asset transparent and user-adjustable before download begins.

### Proposed Behavior
Before installation starts, show a dialog containing:
- Repository name
- Release title/tag
- Published date
- List of available assets
- File size
- Asset type hints (portable ZIP, installer EXE, MSI, etc.)
- Architecture hints (x64, x86, arm64 if detectable)
- The system’s recommended asset
- A plain-language explanation of why that asset was selected

Allow the user to:
- Accept the recommended asset
- Manually choose another asset
- Cancel installation

### Benefits
- Greatly reduces incorrect installs
- Builds user trust
- Makes asset selection auditable and understandable
- Improves supportability when installs fail

### Implementation Notes
- Reuse current asset scoring logic as the “recommended choice” engine
- Surface the scoring output as user-readable reasons
- Persist chosen asset metadata in the managed registry

### Risks / Considerations
- More UI complexity during install flow
- Need to preserve smooth “one-click install” feel for simple cases

### Suggested Next Steps
1. Add a release asset DTO for UI presentation
2. Refactor asset scoring to return both score and explanation
3. Build selection dialog
4. Route install flow through dialog before download

---

## F-02. Checksum and Trust Verification
- **Status:** Planned
- **Priority:** P0
- **Category:** Security / Trust

### Problem
The app downloads and runs executables from GitHub, but trust visibility is limited. Users need stronger assurance about what was downloaded.

### Goal
Add integrity verification and trust visibility to downloaded assets.

### Proposed Behavior
- Compute SHA-256 for each downloaded asset
- Store the hash in the registry/database
- Detect and parse checksum files from release assets when available
- Attempt automatic checksum verification
- Show trust state in the UI:
  - Verified checksum
  - Downloaded but unverified
  - No checksum found
  - Verification failed

### Benefits
- Improves user confidence
- Helps diagnose tampering or mismatch problems
- Establishes a stronger base for future trust features

### Implementation Notes
- Add hash fields to persisted model
- Add optional checksum-file parsing utilities
- Display trust state in managed details pane and install summary

### Risks / Considerations
- Release checksum formats vary widely
- Some projects will not publish checksums

### Suggested Next Steps
1. Add SHA-256 hashing utility
2. Persist hash metadata
3. Parse common checksum file formats
4. Add trust indicators to UI

---

## F-03. Repair / Relink / Redetect Actions
- **Status:** Planned
- **Priority:** P1
- **Category:** Recovery / Reliability

### Problem
Portable apps are often moved, renamed, or partially deleted outside the manager. Detection exists, but recovery actions should be easier.

### Goal
Provide first-class recovery tools for broken managed entries.

### Proposed Actions
- Repair launch path
- Redetect executable
- Reopen asset selection
- Relink install folder
- Re-download current version
- Validate file presence

### Benefits
- Reduces broken entry frustration
- Makes the app resilient to manual user changes
- Lowers support burden

### Suggested Next Steps
1. Add a validation routine for managed entries
2. Build repair menu in details/context menu
3. Implement guided redetection flow

---

## F-04. Transactional Install and Update Staging
- **Status:** Planned
- **Priority:** P0
- **Category:** Reliability

### Problem
If an install or update fails mid-process, the final install directory may be left in a partially broken state.

### Goal
Make installs and updates more atomic and failure-resistant.

### Proposed Behavior
- Download into temp workspace
- Extract into staging directory
- Validate output
- Only then move into final install location
- Update registry only after successful completion

### Benefits
- Safer installs
- Cleaner failure recovery
- Better support for future rollback

### Suggested Next Steps
1. Add staging directory manager
2. Define validation rules prior to final swap
3. Wrap install/update flow in transactional orchestration

---

## F-05. Rollback Support
- **Status:** Planned
- **Priority:** P1
- **Category:** Reliability

### Problem
Updates can occasionally break a previously working portable app, with no easy way to return to a prior state.

### Goal
Allow users to roll back to the previous installed version when an update fails functionally.

### Proposed Behavior
- Preserve previous install directory during update
- Store prior version metadata
- Offer “Rollback” action after update or from management UI

### Benefits
- Stronger update confidence
- Safer experimentation with new releases

### Suggested Next Steps
1. Define retained backup layout
2. Persist prior-version metadata
3. Add rollback action and UI safeguards

---

## F-06. Cancellation Support for Background Jobs
- **Status:** Planned
- **Priority:** P1
- **Category:** Performance / UX

### Problem
Long-running downloads or multi-app update checks cannot be cleanly cancelled.

### Goal
Allow users to cancel in-flight jobs gracefully.

### Proposed Behavior
- Add cancellation tokens to workers
- Add cancel button in active job area
- Clean up partial temp files on cancellation
- Track job state transitions clearly

### Benefits
- Better responsiveness
- Better user control
- Cleaner failure/cancel handling

### Suggested Next Steps
1. Standardize job lifecycle model
2. Add cancelable worker abstraction
3. Expose job state in UI

---

## F-07. SQLite Migration from JSON Registry
- **Status:** Planned
- **Priority:** P1
- **Category:** Architecture / Persistence

### Problem
JSON persistence is simple, but long-term it is less robust for concurrent writes, richer history, and structured queries.

### Goal
Move managed data and app state to SQLite while keeping import/export compatibility.

### Proposed Behavior
- On upgrade, migrate JSON data into SQLite automatically
- Keep JSON export/import available as backup portability option
- Use SQLite for primary runtime persistence

### Benefits
- Safer writes
- Better structure for history, logs, and advanced filtering
- Easier schema evolution

### Suggested Next Steps
1. Define schema
2. Write migration path
3. Build compatibility layer for old registry loading

---

## F-08. Per-Program History and Event Log
- **Status:** Planned
- **Priority:** P1
- **Category:** Observability

### Problem
There is limited visibility into the lifecycle of each managed app.

### Goal
Maintain an event trail for installs, updates, failures, launches, repairs, and removals.

### Event Examples
- Installed
- Updated
- Update available
- Update failed
- Launch attempted
- Launch failed
- Folder missing
- Repaired
- Removed

### Benefits
- Easier troubleshooting
- Better user transparency
- Good foundation for future analytics

### Suggested Next Steps
1. Define event schema
2. Record controller actions to event stream
3. Add history UI panel

---

## F-09. Notification System Improvements
- **Status:** Planned
- **Priority:** P2
- **Category:** UX

### Goal
Expand notifications beyond simple status messaging.

### Proposed Features
- Windows toast notifications
- Preferences for notification categories
- Actions for opening app or jumping to affected program

### Suggested Next Steps
1. Finalize notification policy options
2. Add toast adapter
3. Connect to update/install events

---

## F-10. Managed Programs Tab Enhancement
- **Status:** Planned
- **Priority:** P1
- **Category:** UX

### Goal
Turn the managed list into a true operations console.

### Proposed Features
- Right-click context menus
- Status badges
- Bulk actions
- Persistent sorting/filtering
- Better detail panel summaries

### Suggested Next Steps
1. Add richer item state model
2. Implement context menu actions
3. Persist list view state

---

## F-11. Update Matching Logic Improvements
- **Status:** Planned
- **Priority:** P0
- **Category:** Correctness

### Problem
Simple version/tag comparison can miss edge cases or produce incorrect update availability decisions.

### Goal
Improve release identity tracking and update precision.

### Proposed Improvements
- Persist release ID
- Persist asset identity where available
- Distinguish newer release vs changed asset vs prerelease divergence

### Benefits
- Fewer false update prompts
- Better release tracking accuracy

### Suggested Next Steps
1. Expand persisted release metadata
2. Adjust update comparison algorithm
3. Add test cases for edge scenarios

---

## F-12. Network Resilience Improvements
- **Status:** Planned
- **Priority:** P1
- **Category:** Reliability

### Goal
Handle GitHub/network failures gracefully.

### Proposed Features
- Retry with backoff for transient failures
- Rate-limit detection
- Clear user-facing error states
- Lightweight caching of recent metadata

### Suggested Next Steps
1. Centralize network error mapping
2. Add retry policy
3. Add rate-limit aware messaging

---

## F-13. Main Window UI Modularization
- **Status:** Planned
- **Priority:** P1
- **Category:** Maintainability

### Problem
The main window implementation is very large and will become harder to maintain as features grow.

### Goal
Split the UI into focused modules.

### Proposed Modules
- discover tab
- managed tab
- dialogs
- tray controller
- status widgets

### Benefits
- Easier patching
- Lower change risk
- Better readability and testability

### Suggested Next Steps
1. Identify clean UI boundaries
2. Extract one tab at a time
3. Preserve existing signals/controller hooks

---

## F-14. Service Layer Refactor
- **Status:** Planned
- **Priority:** P2
- **Category:** Architecture

### Goal
Move orchestration logic out of the controller into focused services.

### Candidate Services
- install service
- update service
- launch service
- notification service
- history service

### Suggested Next Steps
1. Identify current controller responsibilities
2. Extract install/update first
3. Introduce clear service interfaces

---

## F-15. Watchlist / Favorites Without Install
- **Status:** Planned
- **Priority:** P2
- **Category:** Product

### Goal
Allow users to track apps without installing them.

### Proposed Features
- Save repo as watchlist item
- Monitor release activity
- Notify on updates
- Pin favorites

### Benefits
- Expands the product beyond installed-app management
- Helps discovery workflows

---

## F-16. Import / Export / Backup
- **Status:** Planned
- **Priority:** P2
- **Category:** Portability

### Goal
Allow users to back up and move app configuration and managed metadata between systems.

### Proposed Features
- Export registry/settings
- Import registry/settings
- Reconstruct entries from install root where possible

---

## F-17. Portable Confidence Scoring in Search
- **Status:** Planned
- **Priority:** P2
- **Category:** Discovery UX

### Goal
Help users quickly judge whether a search result is likely to be a good portable-app candidate.

### Proposed Factors
- Has releases
- Has Windows assets
- Portable keyword match
- Avoids installer-only releases
- Recent activity
- Optional trust/checksum signals

---

## F-18. Automated Test Coverage Expansion
- **Status:** Planned
- **Priority:** P0
- **Category:** Engineering Quality

### Goal
Improve confidence in install/update behavior and future refactors.

### High-Value Test Areas
- Asset picking
- Update matching
- ZIP Slip protection
- Path flattening
- Transactional staging behavior
- Rollback logic
- Migration from JSON to SQLite
- Launch command construction

### Suggested Next Steps
1. Build fixture release metadata samples
2. Add tests around current heuristics before changing behavior
3. Expand coverage as reliability features land

---

# 4. Recommended Implementation Sequence

## Phase 1 — Correctness and Trust
1. F-01 Install preview and manual asset selection
2. F-02 Checksum and trust verification
3. F-11 Update matching logic improvements
4. F-18 Automated test coverage expansion

## Phase 2 — Reliability and Recovery
5. F-04 Transactional install and update staging
6. F-05 Rollback support
7. F-03 Repair / relink / redetect actions
8. F-12 Network resilience improvements

## Phase 3 — UX and Workflow Improvements
9. F-06 Cancellation support for background jobs
10. F-10 Managed Programs tab enhancement
11. F-08 Per-program history and event log
12. F-09 Notification system improvements

## Phase 4 — Architecture and Long-Term Product Expansion
13. F-13 Main window UI modularization
14. F-07 SQLite migration from JSON registry
15. F-14 Service layer refactor
16. F-15 Watchlist / favorites without install
17. F-16 Import / export / backup
18. F-17 Portable confidence scoring in search

---

# 5. Immediate Recommendation

## Best Next Feature to Implement
**F-01 Install preview and manual asset selection**

### Why this should be first
- Improves correctness immediately
- Reduces user confusion and failed installs
- Builds trust in the manager’s decisions
- Fits directly into the app’s core purpose
- Can be implemented without requiring a full storage migration first

### Definition of Done
- Installer shows release asset selection UI before download
- User can accept recommended asset or choose another
- Chosen asset metadata is persisted
- Existing “best asset” logic is preserved as a default path
- Tests cover at least several multi-asset edge cases

---

# 6. Open Questions
- Should the install preview appear for all installs, or only when multiple plausible assets exist?
- Should “one-click install” remain available as a setting?
- How aggressively should checksum parsing try to infer matching files from release assets?
- Should rollback retain only one previous version, or multiple?
- Should watchlist items and installed items live in the same registry table/model?

---

# 7. Conclusion
The project is already beyond prototype stage and has a solid modular base. The next wave of improvements should focus on **trust, install correctness, recovery, and long-term maintainability**. The most impactful next milestone is to make install decisions transparent and user-controllable, then follow with stronger integrity checks and safer lifecycle management.

