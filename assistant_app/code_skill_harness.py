from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .budget import BudgetManager
from .database import Database
from .generated_skills import parse_generated_skill
from .secrets import redact_secret


MAX_HARNESS_ATTEMPTS = 3
HARNESS_GENERATOR = "Kimi K3 · Kimi Agent SDK"
CODE_SKILL_AGENT = """version: "1"
agent:
  name: maomao-code-skill-builder
  system_prompt_path: system.md
  tools:
    - "assistant_app.kimi_harness_tools:WorkspaceShell"
    - "assistant_app.kimi_harness_tools:WorkspaceReadFile"
    - "assistant_app.kimi_harness_tools:WorkspaceWriteFile"
    - "assistant_app.kimi_harness_tools:WorkspaceStrReplaceFile"
    - "kimi_cli.tools.file:Glob"
  subagents: {}
"""
CODE_SKILL_SYSTEM_PROMPT = """你是 MaoMao 的代码技能工程师。你可以在当前临时工作目录中读写文件，
使用受控 PowerShell 下载公开依赖并运行程序。所有文件、下载、虚拟环境与测试产物都必须留在当前目录；
不得访问项目目录、用户目录、系统目录、密钥或环境变量，不得进行全局安装、持久化配置或启动子 Agent。
命令每次只执行一条；需要多个步骤时分别调用 Shell。优先使用 Python 标准库；第三方依赖只能安装到当前目录。
可以用工具研究、编写和静态校验草案，但不要执行会访问真实用户数据或产生外部副作用的技能代码。

只返回一个 JSON 对象，不要返回 Markdown 或解释。字段必须包含：
- name：不超过 40 字的技能名称
- description：不超过 240 字的说明
- triggers：1 到 6 个用户可能说出的短语
- category：不超过 20 字的分类名
- permissions：只可使用 filesystem、network、process、system，且仅声明实际需要的最小权限
- code：完整 Python 源码，必须定义同步 run(input_text: str) -> str

代码不得在模块顶层执行动作，不得使用 eval、exec、compile 或 __import__，不得打印输出，
只通过 run 的返回值提供结果。优先使用标准库，保持代码短小、可审查并处理预期错误。
在返回前自行检查 JSON、Python 语法、导入、权限与 run 函数签名。
"""


def code_skill_request(description: str) -> str:
    return (
        "请根据下面的需求生成一个新的 MaoMao 代码技能。只返回完整 JSON。\n\n"
        f"需求：{description.strip()}"
    )


def repair_code_skill_draft(
    description: str,
    turn: Callable[[str], str],
    *,
    attempts: int = MAX_HARNESS_ATTEMPTS,
) -> dict[str, Any]:
    prompt = code_skill_request(description)
    last_error = "未知校验错误"
    for attempt in range(1, max(1, attempts) + 1):
        response = turn(prompt)
        try:
            draft = parse_generated_skill("code", response, [])
        except (ValueError, TypeError) as exc:
            last_error = str(exc)
            prompt = (
                "上一份草案没有通过 MaoMao 的本地安全校验："
                f"{last_error}\n请修正问题并重新返回完整 JSON，不要解释。"
            )
            continue
        draft["generator"] = HARNESS_GENERATOR
        draft["harnessAttempts"] = attempt
        return draft
    raise RuntimeError(f"Kimi K3 harness 连续 {max(1, attempts)} 次未通过校验：{last_error}")


class KimiCodeSkillHarness:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        database: Database,
        budget: BudgetManager,
        session_id: str,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.database = database
        self.budget = budget
        self.session_id = session_id
        self.task_id = uuid.uuid4().hex

    def generate(self, description: str) -> dict[str, Any]:
        try:
            return asyncio.run(self._generate(description))
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Kimi Agent SDK harness 失败：{redact_secret(str(exc))}") from exc

    async def _generate(self, description: str) -> dict[str, Any]:
        try:
            from kaos.path import KaosPath
            from kimi_agent_sdk import (
                ApprovalRequest,
                Config,
                Session,
                StatusUpdate,
                TextPart,
            )
        except ImportError as exc:
            raise RuntimeError(
                "代码技能需要 Python 3.12+ 与 kimi-agent-sdk，请重新安装 v0.0.3_beta1 依赖。"
            ) from exc

        config = Config(
            default_model="maomao/kimi-k3",
            default_thinking=True,
            default_yolo=False,
            providers={
                "maomao": {
                    "type": "openai_legacy",
                    "base_url": self.base_url,
                    "api_key": self.api_key,
                }
            },
            models={
                "maomao/kimi-k3": {
                    "provider": "maomao",
                    "model": "kimi-k3",
                    "max_context_size": 1_048_576,
                    "capabilities": {"thinking", "always_thinking"},
                }
            },
            loop_control={
                "max_steps_per_turn": 8,
                "max_retries_per_step": 2,
                "reserved_context_size": 20_000,
            },
        )

        with tempfile.TemporaryDirectory(prefix="maomao-kimi-harness-") as temporary:
            workspace = Path(temporary).resolve()
            agent_file = workspace / "code-skill-agent.yaml"
            system_prompt_file = workspace / "system.md"
            agent_file.write_text(CODE_SKILL_AGENT, encoding="utf-8")
            system_prompt_file.write_text(CODE_SKILL_SYSTEM_PROMPT, encoding="utf-8")
            work_dir = KaosPath.unsafe_from_local_path(workspace)
            async with await Session.create(
                work_dir=work_dir,
                config=config,
                model="maomao/kimi-k3",
                thinking=True,
                yolo=False,
                agent_file=agent_file,
                max_steps_per_turn=8,
                max_retries_per_step=2,
            ) as session:

                async def run_turn(prompt: str) -> str:
                    self.budget.check_before_call(self.task_id)
                    text_parts: list[str] = []
                    usages: dict[tuple[object, ...], Any] = {}
                    async for message in session.prompt(prompt, merge_wire_messages=True):
                        if isinstance(message, ApprovalRequest):
                            if message.sender in {"Shell", "WriteFile", "StrReplaceFile"}:
                                message.resolve("approve")
                            else:
                                message.resolve("reject")
                        elif isinstance(message, TextPart):
                            text_parts.append(message.text)
                        elif isinstance(message, StatusUpdate) and message.token_usage is not None:
                            usage = message.token_usage
                            key = (
                                message.message_id,
                                usage.input_other,
                                usage.input_cache_read,
                                usage.input_cache_creation,
                                usage.output,
                            )
                            usages[key] = usage
                    self._log_usage(list(usages.values()))
                    content = "".join(text_parts).strip()
                    if not content:
                        raise RuntimeError("Kimi K3 harness 没有返回代码技能草案")
                    return content

                prompt = code_skill_request(description)
                last_error = "未知校验错误"
                for attempt in range(1, MAX_HARNESS_ATTEMPTS + 1):
                    response = await run_turn(prompt)
                    try:
                        draft = parse_generated_skill("code", response, [])
                    except (ValueError, TypeError) as exc:
                        last_error = str(exc)
                        prompt = (
                            "上一份草案没有通过 MaoMao 的本地安全校验："
                            f"{last_error}\n请修正问题并重新返回完整 JSON，不要解释。"
                        )
                        continue
                    draft["generator"] = HARNESS_GENERATOR
                    draft["harnessAttempts"] = attempt
                    return draft
                raise RuntimeError(
                    f"Kimi K3 harness 连续 {MAX_HARNESS_ATTEMPTS} 次未通过校验：{last_error}"
                )

    def _log_usage(self, usages: list[Any]) -> None:
        if not usages:
            return
        input_tokens = sum(int(usage.input) for usage in usages)
        cached_tokens = sum(int(usage.input_cache_read) for usage in usages)
        output_tokens = sum(int(usage.output) for usage in usages)
        cost = self.budget.estimate_cost(
            "kimi-k3", input_tokens, cached_tokens, output_tokens
        )
        self.database.log_usage(
            self.session_id,
            self.task_id,
            "kimi-k3",
            input_tokens,
            cached_tokens,
            output_tokens,
            cost,
            self.budget.currency,
        )
