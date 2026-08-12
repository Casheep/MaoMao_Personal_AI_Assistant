from __future__ import annotations

import re

from .base import SkillInvocation


class SystemVolumeSkill:
    name = "system-volume"

    def match(self, text: str) -> SkillInvocation | None:
        compact = re.sub(r"[\s，。！？,.!?、~～]", "", text).lower()
        if len(compact) > 80:
            return None
        if re.search(r"每天|每日|定时|明天|后天|早上|上午|中午|下午|晚上|凌晨|\d+点|分钟后|小时后", compact):
            return None
        if re.search(r"不要|别调|别动|取消|怎么|如何|为什么", compact):
            return None
        if not re.search(r"系统音量|电脑音量|主音量|声音|静音", compact):
            return None

        arguments: dict[str, object]
        announcement: str
        percentage = re.search(r"(\d{1,3})%?", compact)
        if re.search(r"取消静音|解除静音|恢复声音|打开声音", compact):
            arguments = {"action": "unmute"}
            announcement = "好，我取消静音。"
        elif re.search(r"静音|关闭声音|关掉声音", compact):
            arguments = {"action": "mute"}
            announcement = "好，我把系统静音。"
        elif percentage and re.search(r"调到|设置为|设为|变成", compact):
            level = max(0, min(100, int(percentage.group(1))))
            arguments = {"action": "set", "level": level}
            announcement = f"好，我把系统音量调到 {level}%。"
        elif re.search(r"调高|增大|提高|大一点|加大", compact):
            level = max(1, min(100, int(percentage.group(1)))) if percentage else 10
            arguments = {"action": "up", "level": level}
            announcement = "好，我把系统音量调高一点。"
        elif re.search(r"调低|减小|降低|小一点|关小", compact):
            level = max(1, min(100, int(percentage.group(1)))) if percentage else 10
            arguments = {"action": "down", "level": level}
            announcement = "好，我把系统音量调低一点。"
        elif re.search(r"多少|当前|现在|查看|查询", compact):
            arguments = {"action": "status"}
            announcement = ""
        else:
            return None
        return SkillInvocation(
            name=self.name,
            tool_name="control_system_volume",
            arguments=arguments,
            announcement=announcement,
            reason="本地 Windows 系统音量技能",
            silent_on_success=bool(announcement),
        )


SKILL = SystemVolumeSkill()
