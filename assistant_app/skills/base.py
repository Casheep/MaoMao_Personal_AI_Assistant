from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SkillInvocation:
    name: str
    tool_name: str
    arguments: dict[str, Any]
    announcement: str
    reason: str
    silent_on_success: bool = True
    continue_listening: bool = True


class LocalSkill(Protocol):
    name: str

    def match(self, text: str) -> SkillInvocation | None: ...
