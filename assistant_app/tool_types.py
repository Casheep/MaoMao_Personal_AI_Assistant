from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolResult:
    success: bool
    content: str
    image_path: Path | None = None
