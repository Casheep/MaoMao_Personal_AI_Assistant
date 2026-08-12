from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteDecision:
    model: str
    reasoning: str
    score: int
    reasons: tuple[str, ...]


COMPLEX_PATTERNS = (
    r"深入分析|仔细分析|深度思考|认真想|复杂|详细方案",
    r"调试|报错|故障|重构|代码库|项目|数据库|架构",
    r"长文档|论文|报告|合同|研究|比较.*方案",
)
MAX_PATTERNS = (r"最强模型|最高推理|尽最大努力|K3\s*max",)
SCREEN_PATTERNS = (r"屏幕|截图|界面|按钮|窗口|点击|鼠标",)
VISUAL_PATTERNS = (
    r"图片|图像|照片|相片|截图|截屏|看图|识图|视觉|画面|二维码|图标|OCR",
    r"屏幕|界面|按钮|窗口|点击|鼠标",
    r"\b(?:image|photo|picture|screenshot|screen|visual|ocr)\b",
)
MULTI_STEP_PATTERNS = (r"然后|接着|再把|完成后|依次|所有步骤",)
FAST_PATTERNS = (r"快速回答|简单回答|不用思考|日常模式",)


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def choose_route(text: str, config: dict, previous_tool_failed: bool = False) -> RouteDecision:
    mode = str(config.get("model_mode", "auto")).lower()
    if mode == "k2.6":
        return RouteDecision(
            model="kimi-k2.6",
            reasoning="disabled",
            score=0,
            reasons=("手动锁定 K2.6",),
        )
    if mode == "k3":
        return RouteDecision(
            model="kimi-k3",
            reasoning="low",
            score=99,
            reasons=("手动锁定 K3",),
        )
    if mode == "mimo-pro":
        return RouteDecision(
            model="mimo-v2.5-pro",
            reasoning="enabled",
            score=99,
            reasons=("手动锁定 MiMo V2.5 Pro",),
        )
    if mode == "mimo-omni":
        return RouteDecision(
            model="mimo-v2.5",
            reasoning="disabled",
            score=0,
            reasons=("手动锁定 MiMo V2.5",),
        )

    score = 0
    reasons: list[str] = []

    if _matches(MAX_PATTERNS, text):
        return RouteDecision(
            model="kimi-k3",
            reasoning="max",
            score=99,
            reasons=("用户明确要求最强推理",),
        )
    if _matches(VISUAL_PATTERNS, text):
        return RouteDecision(
            model="kimi-k3",
            reasoning="low",
            score=max(6, int(config.get("k3_low_threshold", 6))),
            reasons=("涉及图像或视觉操作",),
        )
    if _matches(FAST_PATTERNS, text):
        return RouteDecision(
            model="kimi-k2.6",
            reasoning="disabled",
            score=0,
            reasons=("用户要求快速模式",),
        )
    if _matches(COMPLEX_PATTERNS, text):
        score += 4
        reasons.append("复杂推理或专业任务")
    if _matches(SCREEN_PATTERNS, text):
        score += 2
        reasons.append("涉及屏幕或界面")
    if _matches(MULTI_STEP_PATTERNS, text):
        score += 2
        reasons.append("包含多步骤指令")
    if len(text) > 500:
        score += 2
        reasons.append("输入较长")
    if "```" in text:
        score += 2
        reasons.append("包含代码块")
    if previous_tool_failed:
        score += 3
        reasons.append("之前的工具调用失败")

    high = int(config["k3_high_threshold"])
    low = int(config["k3_low_threshold"])
    if score >= high:
        return RouteDecision("kimi-k3", "high", score, tuple(reasons))
    if score >= low:
        return RouteDecision("kimi-k3", "low", score, tuple(reasons))
    if score >= 3:
        return RouteDecision("kimi-k2.6", "enabled", score, tuple(reasons))
    return RouteDecision("kimi-k2.6", "disabled", score, tuple(reasons or ["日常任务"]))
