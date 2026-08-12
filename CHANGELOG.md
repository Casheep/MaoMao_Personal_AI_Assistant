# Changelog

All notable changes to MaoMao Personal AI Assistant are recorded here.

## [v0.0.2] - 2026-08-12

- Replaced the legacy desktop interfaces with a single Qt Quick/PySide6 GUI and consistent rounded controls.
- Added text and voice conversation, wake words, continuous conversation, tray controls, skills, permissions, schedules and integration settings.
- Added persistent, validated and reorderable custom skill categories plus search across skills, favorites and skill assignment.
- Added responsive compact layouts and threaded OpenGL rendering for smooth Windows resizing without Direct3D swap-chain gaps.
- Deferred model, audio-device, NumPy, wake-template and optional dependency loading to shorten startup work.
- Optimized wake-voice MFCC/DTW processing, real-time audio buffering, versioned SQLite migrations, FTS5 memory retrieval, caching and batched writes.
- Added safe action confirmation, startup component installation, content-free timing diagnostics and public-package privacy checks.
- Removed inactive legacy GUI code and dependencies and made Qt Quick the sole graphical entrypoint.
