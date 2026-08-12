from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, TypeVar, cast


_MAX_LOG_BYTES = 1_000_000
_WRITE_LOCK = threading.Lock()
_RESULT = TypeVar("_RESULT")


def _log_path() -> Path:
    from .config import app_paths

    return app_paths().data / "performance.jsonl"


def record_timing(event: str, elapsed_seconds: float, *, success: bool = True) -> None:
    """Append one content-free local timing event for performance diagnosis."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "elapsed_ms": round(max(0.0, elapsed_seconds) * 1000, 3),
        "success": bool(success),
    }
    try:
        path = _log_path()
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size >= _MAX_LOG_BYTES:
                path.replace(path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(line)
    except OSError:
        # Diagnostics must never make an assistant operation fail.
        return


def timed(event: str) -> Callable[[Callable[..., _RESULT]], Callable[..., _RESULT]]:
    """Measure a function without recording arguments, results or exception text."""
    def decorate(function: Callable[..., _RESULT]) -> Callable[..., _RESULT]:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> _RESULT:
            started = perf_counter()
            success = False
            try:
                result = function(*args, **kwargs)
                success = True
                return result
            finally:
                record_timing(event, perf_counter() - started, success=success)

        return cast(Callable[..., _RESULT], wrapped)

    return decorate
