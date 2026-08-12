from __future__ import annotations

import re

from .base import SkillInvocation


class CloseCurrentBrowserSkill:
    name = "close-current-browser"

    def match(self, text: str) -> SkillInvocation | None:
        compact = re.sub(r"[\s，。！？,.!?、~～]", "", text).lower()
        if len(compact) > 70:
            return None
        if re.search(r"不要|别关|不关闭|取消|先别|怎么关|如何关|为什么", compact):
            return None
        if re.search(r"然后|接着|之后|并且|顺便|标签页|网页", compact):
            return None

        browser = r"(?:当前|现在|这个|我的)?(?:的)?(?:浏览器|edge|chrome|firefox|火狐)(?:窗口)?"
        close = r"(?:关掉|关闭|关了|退出|关一下)"
        if not re.search(rf"(?:把|将|帮我|请|麻烦|给我|能不能|可以)?{close}{browser}|(?:把|将)?{browser}{close}", compact):
            return None
        return SkillInvocation(
            name=self.name,
            tool_name="close_browser",
            arguments={},
            announcement="好，我关掉当前浏览器。",
            reason="本地关闭当前浏览器技能",
        )


SKILL = CloseCurrentBrowserSkill()
