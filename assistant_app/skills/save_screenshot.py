from __future__ import annotations

import re

from .base import SkillInvocation


class SaveScreenshotSkill:
    name = "screenshot-retention"

    def match(self, text: str) -> SkillInvocation | None:
        compact = re.sub(r"[\s，。！？,.!?、~～]", "", text).lower()
        if len(compact) > 70 or re.search(r"不要|别保存|不保存|取消|怎么|如何", compact):
            return None
        if not re.search(r"截图|截屏|屏幕图片|屏幕画面", compact):
            return None
        if not re.search(r"保存|保留|留下|存下来|长期", compact):
            return None
        return SkillInvocation(
            name=self.name,
            tool_name="save_screenshot",
            arguments={},
            announcement="好，我把当前截图长期保存下来。",
            reason="本地截图保存技能",
        )


SKILL = SaveScreenshotSkill()
