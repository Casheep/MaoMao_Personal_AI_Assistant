from __future__ import annotations

from typing import Any


def speaker_preroll_ms(request: dict[str, Any], stream: Any) -> int:
    """Return a longer silent preroll whenever a fresh output stream must wake."""
    normal_ms = max(0, int(request.get("leading_silence_ms", 220)))
    first_ms = max(
        normal_ms,
        int(request.get("first_playback_silence_ms", 800)),
    )
    try:
        stream_active = stream is not None and bool(stream.active)
    except Exception:
        stream_active = False
    return normal_ms if stream_active else first_ms
