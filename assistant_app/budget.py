from __future__ import annotations

from dataclasses import dataclass

from .database import Database


PRICING_CNY_PER_MILLION = {
    "kimi-k2.6": {"cached_input": 1.10, "input": 6.50, "output": 27.00},
    "kimi-k3": {"cached_input": 2.00, "input": 20.00, "output": 100.00},
    "mimo-v2.5-pro": {"cached_input": 0.025, "input": 3.00, "output": 6.00},
    "mimo-v2.5": {"cached_input": 0.02, "input": 1.00, "output": 2.00},
}


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetStatus:
    today: float
    month: float
    warning: bool


class BudgetManager:
    def __init__(self, database: Database, config: dict) -> None:
        self.database = database
        self.currency = str(config["currency"])
        self.currency_symbol = str(config["currency_symbol"])
        self.daily_warning = float(config["daily_warning"])
        self.daily_hard = float(config["daily_hard_limit"])
        self.monthly_hard = float(config["monthly_hard_limit"])
        self.per_task_hard = float(config["per_task_hard_limit"])

    @staticmethod
    def estimate_cost(
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> float:
        prices = PRICING_CNY_PER_MILLION.get(model)
        if prices is None:
            raise ValueError(f"没有 {model} 的本地价格配置。")
        cached = max(0, min(input_tokens, cached_input_tokens))
        uncached = max(0, input_tokens - cached)
        return (
            cached * prices["cached_input"]
            + uncached * prices["input"]
            + output_tokens * prices["output"]
        ) / 1_000_000

    def check_before_call(self, task_id: str) -> BudgetStatus:
        today = self.database.usage_total("day", currency=self.currency)
        month = self.database.usage_total("month", currency=self.currency)
        task = self.database.usage_total("day", task_id=task_id, currency=self.currency)
        if today >= self.daily_hard:
            raise BudgetExceeded(
                f"今日 API 费用已达到硬上限 {self.currency_symbol}{self.daily_hard:.2f}。"
            )
        if month >= self.monthly_hard:
            raise BudgetExceeded(
                f"本月 API 费用已达到硬上限 {self.currency_symbol}{self.monthly_hard:.2f}。"
            )
        if task >= self.per_task_hard:
            raise BudgetExceeded(
                f"当前任务费用已达到上限 {self.currency_symbol}{self.per_task_hard:.2f}。"
            )
        return BudgetStatus(today=today, month=month, warning=today >= self.daily_warning)
