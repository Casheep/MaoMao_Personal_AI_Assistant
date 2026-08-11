from __future__ import annotations

import re
from datetime import datetime

from .base import SkillInvocation


def _chinese_number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


class ScheduledTasksSkill:
    name = "scheduled-tasks"

    def match(self, text: str) -> SkillInvocation | None:
        compact = re.sub(r"[\s，。！？,.!?、~～]", "", text).lower()
        if not re.search(r"每天|每日", compact):
            return None
        if not re.search(r"星穹铁道|星铁|新铁|星帖", compact):
            return None
        match = re.search(
            r"(凌晨|早上|上午|中午|下午|晚上|傍晚)?"
            r"([零〇一二两三四五六七八九十\d]{1,3})点"
            r"(?:(半)|([零〇一二两三四五六七八九十\d]{1,3})分?)?",
            compact,
        )
        if match is None:
            return None
        period, hour_text, half, minute_text = match.groups()
        hour = _chinese_number(hour_text)
        minute = 30 if half else (_chinese_number(minute_text) if minute_text else 0)
        if hour is None or minute is None or not 0 <= minute <= 59:
            return None
        if period in {"下午", "晚上", "傍晚"} and 1 <= hour <= 11:
            hour += 12
        elif period == "中午" and 1 <= hour <= 10:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        if not 0 <= hour <= 23:
            return None
        run_at = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        silent = "静默" in compact
        return SkillInvocation(
            name=self.name,
            tool_name="create_scheduled_task",
            arguments={
                "command": "帮我过星铁日常",
                "run_at": run_at.strftime("%Y-%m-%d %H:%M"),
                "repeat": "daily",
                "silent": silent,
            },
            announcement="",
            reason="本地每日星铁定时任务解析",
            silent_on_success=False,
            continue_listening=True,
        )


SKILL = ScheduledTasksSkill()
