from __future__ import annotations

import importlib
import pkgutil

from .base import LocalSkill, SkillInvocation
from .catalog import (
    SKILL_CATALOG,
    SKILL_CATEGORY_ORDER,
    SKILLS_BY_ID,
    TOOL_SKILLS,
    SkillDefinition,
    is_skill_enabled,
    skill_values,
)


def load_local_skills() -> tuple[LocalSkill, ...]:
    loaded: list[LocalSkill] = []
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_") or module_info.name == "base":
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        skill = getattr(module, "SKILL", None)
        if skill is not None and callable(getattr(skill, "match", None)):
            loaded.append(skill)
    return tuple(loaded)


LOCAL_SKILLS = load_local_skills()


def match_local_skill(text: str, config: dict | None = None) -> SkillInvocation | None:
    for skill in LOCAL_SKILLS:
        invocation = skill.match(text)
        if invocation is not None and is_skill_enabled(config, invocation.name):
            return invocation
    return None


__all__ = [
    "LOCAL_SKILLS",
    "SKILL_CATALOG",
    "SKILL_CATEGORY_ORDER",
    "SKILLS_BY_ID",
    "TOOL_SKILLS",
    "LocalSkill",
    "SkillDefinition",
    "SkillInvocation",
    "is_skill_enabled",
    "load_local_skills",
    "match_local_skill",
    "skill_values",
]
