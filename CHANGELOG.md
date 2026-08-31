# Changelog

All notable changes to MaoMao Personal AI Assistant are recorded here.

## [v0.0.3_beta2] - 2026-08-31

- Added a Windows UI Automation fast path that executes explicit clicks and text entry locally only when the foreground window exposes one unique exact control match.
- Added a compact, relevance-ordered UIA context and pre-attached visual overview so explicit screen observations no longer require a model request just to ask for a screenshot.
- Preserved the existing screen-control confirmations, skill switches and action audit trail for UIA operations, while treating interface text as untrusted data and excluding current input values.
- Split screen diagnostics into UIA snapshot, matching, execution, capture, image-save and image-encoding timings, and exposed per-model and tool timing breakdowns in response metadata.
- Added adaptive foreground-window JPEG profiles: 896-pixel overview, 1152-pixel balanced and 1536-pixel detail, with the full desktop retained only as a safe fallback.
- Routed routine screen observations through one non-thinking Kimi K2.6 vision call while reserving K3 detail for small text, OCR, charts and precision work.
- Added on-demand region zoom from the transient in-memory source frame, allowing a model to inspect one high-resolution ROI instead of pre-slicing or repeatedly uploading the whole screen.
- Moved adaptive capture, ROI encoding and task-scoped source-frame ownership into a dedicated screen session, removing an extra full-resolution image copy and releasing the source during task cleanup.
- Added a longer silent preroll when a fresh audio output stream opens so sleeping speakers can wake before the first phoneme, while retaining the shorter normal inter-utterance padding on an active stream.
- Exposed scheduled-task management through the scheduled-task skill settings available from favorites, and changed startup recovery to mark elapsed occurrences as missed instead of running them late.
- Added elapsed processing time to each assistant response record and the latest result shown for each scheduled task.
- Styled both user and MaoMao sender names with the existing blue accent in conversation history while retaining the secondary color for system records.
- Removed the obsolete speech pause, resume and stop controls; normal follow-up-listening timeouts now return silently to ready, and “never mind”/“没事了” variants end continuous listening like a goodbye.

## [v0.0.3_beta1] - 2026-08-12

- Added an arrow-free purple creator tab in the central column; expanding it yields the message-input space, reduces conversation history and keeps audio controls and both sidebars available.
- Added Kimi K3-only generators for composing existing tools into reusable workflows and creating Python-backed skills.
- Upgraded the runtime baseline to Python 3.12 and moved code-skill creation onto the official Kimi Agent SDK harness with bounded repair attempts, task-scoped temporary file tools and a bounded Shell, without subagents.
- Added structured draft previews, local persistence, generated-skill discovery, enablement, favorites and deletion.
- Added tool-bound workflow validation and static Python checks for imports, top-level behavior, declared permissions and dynamic execution.
- Added saved-code hash verification, repeated static validation, per-run confirmation and time-limited child-process execution for generated code skills.
- Kept wake-word listening and voice interaction in the background without restoring or focusing a minimized window.

## [v0.0.2_beta3] - 2026-08-12

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

- Added rounded popup, selection, checkbox, progress and text-input surfaces throughout the Qt Quick interface.
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

- Local memory, wake words, continuous conversation, computer control and scheduled tasks.
- Kimi and MiMo text routing, local/API ASR, and local/API TTS choices.
