from __future__ import annotations

import json
from typing import Any

import httpx

from ..budget import BudgetManager
from ..database import Database
from ..secrets import redact_secret
from .kimi import KimiClient, KimiResponse


class MiMoClient:
    def __init__(
        self,
        api_key: str,
        config: dict[str, Any],
        database: Database,
        budget: BudgetManager,
        session_id: str,
    ) -> None:
        self.config = config
        self.database = database
        self.budget = budget
        self.session_id = session_id
        mimo_config = config.get("mimo_api", {})
        self.client = httpx.Client(
            base_url=str(mimo_config.get("base_url", "https://api.xiaomimimo.com/v1")).rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(mimo_config.get("timeout_seconds", config["api"]["timeout_seconds"])),
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        reasoning: str,
        task_id: str,
    ) -> KimiResponse:
        self.budget.check_before_call(task_id)
        mimo_config = self.config.get("mimo_api", {})
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_completion_tokens": int(mimo_config.get("max_output_tokens", 5000)),
            "stream": False,
            "thinking": {
                "type": "enabled" if reasoning not in {"disabled", "off"} else "disabled"
            },
        }
        try:
            response = self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            safe_error = redact_secret(str(exc))
            if isinstance(exc, httpx.HTTPStatusError):
                safe_error += f"; response={redact_secret(exc.response.text[:1000])}"
            raise RuntimeError(f"MiMo API 调用失败：{safe_error}") from exc

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        details = usage.get("prompt_tokens_details") or {}
        cached_tokens = int(details.get("cached_tokens", 0) or 0)
        cost = self.budget.estimate_cost(model, input_tokens, cached_tokens, output_tokens)
        self.database.log_usage(
            self.session_id,
            task_id,
            model,
            input_tokens,
            cached_tokens,
            output_tokens,
            cost,
            self.budget.currency,
        )
        choices = body.get("choices") or []
        if not choices or "message" not in choices[0]:
            raise RuntimeError("MiMo API 返回内容缺少 choices[0].message。")
        return KimiResponse(
            message=choices[0]["message"],
            model=model,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            cost=cost,
        )

    def close(self) -> None:
        self.client.close()


class HybridModelClient:
    """One agent-facing client with Kimi-first automatic failover to MiMo."""

    def __init__(
        self,
        kimi: KimiClient,
        mimo: MiMoClient,
        config: dict[str, Any],
    ) -> None:
        self.kimi = kimi
        self.mimo = mimo
        self.config = config
        self.budget = kimi.budget

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        reasoning: str,
        task_id: str,
    ) -> KimiResponse:
        if model.startswith("mimo-"):
            return self.mimo.chat(messages, tools, model, reasoning, task_id)
        try:
            return self.kimi.chat(messages, tools, model, reasoning, task_id)
        except RuntimeError:
            if str(self.config.get("routing", {}).get("model_mode", "auto")) != "auto":
                raise
            fallback = "mimo-v2.5-pro" if model == "kimi-k3" else "mimo-v2.5"
            return self.mimo.chat(messages, tools, fallback, reasoning, task_id)

    def close(self) -> None:
        self.kimi.close()
        self.mimo.close()
