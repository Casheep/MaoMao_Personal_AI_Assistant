from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    name: str
    description: str
    category: str
    tools: tuple[str, ...] = ()
    default_enabled: bool = True
    project_url: str = ""


SKILL_CATALOG: tuple[SkillDefinition, ...] = (
    SkillDefinition("wake-word", "唤醒词", "用一个或多个自定义词唤醒猫猫，并在需要时打断播报。", "语音与对话"),
    SkillDefinition("continuous-conversation", "连续对话", "让猫猫回答后继续听你说话，直到识别到告别语。", "语音与对话"),
    SkillDefinition("local-apps", "本地应用", "启动或切换本地应用，并管理允许猫猫打开的应用。", "电脑与浏览器", ("open_app", "add_app_to_allowlist", "list_allowed_apps")),
    SkillDefinition("close-current-browser", "关闭当前浏览器", "关闭当前可见的浏览器窗口，不经过通用模型。", "电脑与浏览器", ("close_browser",)),
    SkillDefinition(
        "browser-navigation",
        "浏览器导航",
        "在当前浏览器中打开网站；明确要求时可打开 ChatGPT 网页提问。",
        "电脑与浏览器",
        ("open_url", "ask_chatgpt"),
    ),
    SkillDefinition("web-research", "网页搜索", "搜索网页、打开搜索结果并读取其中的正文信息。", "电脑与浏览器", ("search_web", "open_search_result")),
    SkillDefinition(
        "screen-inspection",
        "屏幕观察",
        "优先读取当前窗口的结构化控件，并使用自适应概览与按需高清局部理解界面。",
        "屏幕与视觉",
        ("inspect_screen", "inspect_screen_region"),
    ),
    SkillDefinition("screenshot-retention", "截图保存", "在你明确要求时把当前屏幕截图长期保存在本机。", "屏幕与视觉", ("save_screenshot",)),
    SkillDefinition("screen-control", "屏幕操作", "在获得确认后优先按控件名称点击或输入，必要时使用屏幕坐标。", "屏幕与视觉", ("click_screen", "type_text")),
    SkillDefinition(
        "visual-action-shortcuts",
        "常用动作学习",
        "记住成功执行过的界面动作，下次遇到相同窗口时直接复用。",
        "屏幕与视觉",
        ("run_visual_shortcut",),
    ),
    SkillDefinition("local-memory", "本地记忆", "在这台电脑上记住并读取你的长期偏好与稳定信息。", "记忆与文件", ("save_memory", "list_memories")),
    SkillDefinition("file-access", "本地文件", "列出、搜索和读取你允许访问的本地文件。", "记忆与文件", ("list_directory", "search_files", "read_text_file")),
    SkillDefinition(
        "desk-lamp",
        "小米智能家居",
        "连接米家设备，让猫猫在局域网内控制家具并读取设备状态。",
        "设备与自动化",
        ("setup_xiaomi_token", "control_desk_lamp", "get_desk_lamp_status"),
        False,
    ),
    SkillDefinition("system-volume", "系统音量", "读取、静音或调整 Windows 的主音量。", "设备与自动化", ("control_system_volume",)),
    SkillDefinition(
        "starrail-dailies",
        "星铁日常",
        "调用三月七小助手，自动执行星穹铁道完整日常。",
        "设备与自动化",
        ("run_starrail_dailies",),
        False,
        "https://github.com/moesnow/March7thAssistant",
    ),
    SkillDefinition(
        "scheduled-tasks",
        "定时任务",
        "按本机时间执行一次性或每日任务；未运行期间错过的时间不会补跑。",
        "设备与自动化",
        ("create_scheduled_task", "list_scheduled_tasks", "cancel_scheduled_task"),
    ),
    SkillDefinition("current-time", "当前时间", "读取这台电脑当前的日期和时间。", "电脑与浏览器", ("get_current_time",)),
)


SKILL_CATEGORY_ORDER: tuple[str, ...] = (
    "语音与对话",
    "电脑与浏览器",
    "屏幕与视觉",
    "记忆与文件",
    "设备与自动化",
)

SKILLS_BY_ID = {skill.id: skill for skill in SKILL_CATALOG}
TOOL_SKILLS = {
    tool_name: skill.id
    for skill in SKILL_CATALOG
    for tool_name in skill.tools
}


def is_skill_enabled(config: dict[str, Any] | None, skill_id: str) -> bool:
    definition = SKILLS_BY_ID.get(skill_id)
    default = definition.default_enabled if definition is not None else True
    if not isinstance(config, dict):
        return default
    values = config.get("skills", config)
    if not isinstance(values, dict):
        return default
    return bool(values.get(skill_id, default))


def skill_values(config: dict[str, Any] | None) -> dict[str, bool]:
    return {
        skill.id: is_skill_enabled(config, skill.id)
        for skill in SKILL_CATALOG
    }
