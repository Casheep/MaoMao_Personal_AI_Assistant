from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from ..budget import BudgetManager
from ..database import Database
from ..secrets import redact_secret


@dataclass(frozen=True)
class KimiResponse:
    message: dict[str, Any]
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost: float


class KimiClient:
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
        self.client = httpx.Client(
            base_url=config["api"]["base_url"].rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(config["api"]["timeout_seconds"]),
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
        max_tokens = (
            int(self.config["models"]["k3_max_output_tokens"])
            if model == "kimi-k3"
            else int(self.config["models"]["k2_max_output_tokens"])
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_completion_tokens": max_tokens,
            "stream": False,
            "prompt_cache_key": self.session_id,
        }
        if model == "kimi-k3":
            payload["reasoning_effort"] = reasoning if reasoning in {"low", "high", "max"} else "low"
        else:
            payload["thinking"] = {
                "type": "enabled" if reasoning == "enabled" else "disabled"
            }

        try:
            response = self.client.post("/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            safe_error = redact_secret(str(exc))
            if isinstance(exc, httpx.HTTPStatusError):
                safe_body = redact_secret(exc.response.text[:1000])
                safe_error = f"{safe_error}; response={safe_body}"
            raise RuntimeError(f"Kimi API 调用失败：{safe_error}") from exc

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        details = usage.get("prompt_tokens_details") or {}
        cached_tokens = int(
            details.get("cached_tokens", usage.get("cached_prompt_tokens", 0)) or 0
        )
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
            raise RuntimeError("Kimi API 返回内容缺少 choices[0].message。")
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


def new_task_id() -> str:
    return uuid.uuid4().hex
