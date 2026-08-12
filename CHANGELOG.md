# Changelog

All notable changes to MaoMao Personal AI Assistant are recorded here.

## [v0.0.2_beta3] - 2026-08-12

- Initialized the next beta development version from `v0.0.2_beta2`.
- Folded the broad base-ability category into the computer-and-browser category.
- Added persistent custom skill categories with validation, reordering, confirmed deletion and per-skill assignment.
- Added independent search with clear controls and result counts to the skill library, favorites panel and skill-assignment list.
- Centered the skill-category grid with two stable equal-width columns.
- Collapsed both sidebars by default in compact layouts and allowed one drawer at a time for narrow or high-DPI windows.
- Kept preload and pause-all controls inside the audio card by switching to a compact control row when space is constrained.
- Kept sidebars closed after resizing back from a compact window instead of reopening saved panels automatically.
- Switched Windows Qt Quick rendering from Direct3D 11 to threaded OpenGL to avoid black uncommitted swap-chain areas during fast live resizing.
- Deferred NumPy, saved wake-voice templates and installer-only standard-library modules until their first background use, shortening the measured Qt startup import path without changing audio data handling.
- Deferred construction of the skill-category manager until it is first opened.
- Split category state, category management and reusable search controls into focused modules.

## [v0.0.2_beta2] - 2026-08-12

- Initialized the next beta development version from `v0.0.2_beta1`.
- Added rounded popup, selection, checkbox, progress and text-input surfaces throughout the Qt Quick interface.
- Added a local committed-source and Git-history archive helper for post-commit snapshots.
- Corrected the developer attribution to `casheep` across application metadata and interfaces.
- Deferred conversation, HTTP and audio-device dependencies until their features are first used.
- Removed repeated wake-template calibration at startup and optimized MFCC framing, DTW memory use and real-time audio buffering.
- Added versioned database initialization, query indexes and a single-query usage snapshot for budget checks.
- Added FTS5 trigram memory search, automatically invalidated read caches and single-transaction conversation writes.
- Split deterministic local tools into a dedicated handler module, cached tool schemas and deferred optional screenshot imports.
- Added rotating, content-free local timing diagnostics for runtime, model, context, tool and speech operations.
- Separated runtime construction from the CLI and removed the inactive legacy GUI implementations and dependencies.
- Made generated beta JSON/key files UTF-8 without BOM while retaining tolerant runtime config loading.

## [v0.0.2_beta1] - 2026-08-12

- Completed the Qt Quick/PySide6 desktop interface migration and made it the sole GUI entrypoint.
- Added a categorized permission and learned-action manager.
- Added spoken yes/no confirmation for tool permission dialogs.
- Added safe visual action shortcuts that validate the foreground app and window.
- Added an About page and masked API key settings page.
- Added voice capture, wake words, continuous conversation, tray controls, schedules and integration settings.

## [v0.0.1_beta1] - 2026-08-11

- Initial private development release.
- Local memory, wake words, continuous conversation, computer control and scheduled tasks.
- Kimi and MiMo text routing, local/API ASR, and local/API TTS choices.
