from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..tool_types import ToolResult


class LocalToolHandlers:
    """Deterministic time, schedule, memory and filesystem tools."""

    def _resolve_safe_path(self, raw: str) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = self.paths.root / path
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in self.safe_roots):
            raise PermissionError("路径不在允许范围内。")
        return resolved

    def _tool_get_current_time(self, _: dict[str, Any]) -> ToolResult:
        return ToolResult(True, datetime.now().astimezone().isoformat())

    def _tool_create_scheduled_task(self, arguments: dict[str, Any]) -> ToolResult:
        command = str(arguments.get("command") or "").strip()
        run_at = str(arguments.get("run_at") or "").strip()
        repeat_rule = str(arguments.get("repeat") or "once").strip().lower()
        silent = bool(arguments.get("silent", False))
        if not command:
            return ToolResult(False, "定时任务内容不能为空。")
        if len(command) > 2000:
            return ToolResult(False, "定时任务内容不能超过 2000 个字符。")
        if repeat_rule not in {"once", "daily"}:
            return ToolResult(False, "重复方式只能是 once 或 daily。")
        try:
            scheduled = datetime.strptime(run_at, "%Y-%m-%d %H:%M")
        except ValueError:
            return ToolResult(False, "执行时间格式必须是 YYYY-MM-DD HH:MM。")
        confirmation = {
            "command": command,
            "run_at": scheduled.strftime("%Y-%m-%d %H:%M"),
            "repeat": repeat_rule,
            "silent": silent,
        }
        if not self.confirmation_callback("create_scheduled_task", confirmation):
            return ToolResult(False, "用户取消了定时任务。")
        task_id = self.database.create_scheduled_task(
            command,
            scheduled,
            repeat_rule,
            silent=silent,
        )
        task = next(
            item
            for item in self.database.list_scheduled_tasks()
            if int(item["id"]) == task_id
        )
        first_run = datetime.fromisoformat(str(task["next_run_at"]))
        repeat_label = "每天" if repeat_rule == "daily" else "仅一次"
        mode = "静默" if silent else "普通"
        return ToolResult(
            True,
            f"已创建定时任务 #{task_id}：{repeat_label}，{mode}执行，首次时间 {first_run:%Y-%m-%d %H:%M}。",
        )

    def _tool_list_scheduled_tasks(self, _: dict[str, Any]) -> ToolResult:
        tasks = self.database.list_scheduled_tasks(include_disabled=True)
        if not tasks:
            return ToolResult(True, "目前没有定时任务。")
        status_names = {
            "pending": "等待执行",
            "running": "执行中",
            "succeeded": "上次成功",
            "failed": "上次失败",
            "cancelled": "已取消",
        }
        lines = []
        for task in tasks:
            repeat_label = "每天" if task["repeat_rule"] == "daily" else "仅一次"
            mode = "静默" if task["silent"] else "普通"
            enabled = "启用" if task["enabled"] else "停用"
            status = status_names.get(str(task["last_status"]), str(task["last_status"]))
            lines.append(
                f"#{task['id']}｜{repeat_label}｜{task['next_run_at']}｜{mode}｜{enabled}｜"
                f"{task['command']}｜{status}"
            )
        return ToolResult(True, "\n".join(lines))

    def _tool_cancel_scheduled_task(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            task_id = int(arguments.get("task_id"))
        except (TypeError, ValueError):
            return ToolResult(False, "定时任务编号无效。")
        if not self.database.cancel_scheduled_task(task_id):
            return ToolResult(False, f"没有找到定时任务 #{task_id}。")
        return ToolResult(True, f"已停用定时任务 #{task_id}。")

    def _tool_save_memory(self, arguments: dict[str, Any]) -> ToolResult:
        content = str(arguments["content"]).strip()
        tags = str(arguments.get("tags") or "").strip()
        memory_key = str(arguments.get("memory_key") or "").strip().lower()
        if not content:
            return ToolResult(False, "记忆内容不能为空。")
        if len(content) > 2000:
            return ToolResult(False, "单条记忆不能超过 2000 个字符。")
        if memory_key and not re.fullmatch(r"[a-z0-9_.-]{1,80}", memory_key):
            return ToolResult(False, "记忆键只能包含小写字母、数字、点、横线或下划线。")
        memory_id = self.database.remember(content, tags, memory_key)
        return ToolResult(True, f"已保存到本地长期记忆 #{memory_id}。")

    def _tool_list_memories(self, arguments: dict[str, Any]) -> ToolResult:
        limit = max(1, min(50, int(arguments.get("limit") or 20)))
        return ToolResult(
            True,
            json.dumps(self.database.list_memories(limit), ensure_ascii=False),
        )

    def _tool_list_directory(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve_safe_path(str(arguments["path"]))
        if not path.is_dir():
            return ToolResult(False, "目标不是目录。")
        entries = [
            {"name": item.name, "type": "directory" if item.is_dir() else "file"}
            for item in sorted(
                path.iterdir(), key=lambda candidate: (not candidate.is_dir(), candidate.name.lower())
            )[:100]
        ]
        return ToolResult(True, json.dumps(entries, ensure_ascii=False))

    def _tool_search_files(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments["query"]).strip().lower()
        if len(query) < 2:
            return ToolResult(False, "搜索词至少需要两个字符。")
        root_value = str(arguments.get("root") or "").strip()
        roots = [self._resolve_safe_path(root_value)] if root_value else self.safe_roots
        matches: list[str] = []
        for root in roots:
            if not root.exists():
                continue
            try:
                for item in root.rglob("*"):
                    if query in item.name.lower():
                        matches.append(str(item))
                    if len(matches) >= 50:
                        break
            except (PermissionError, OSError):
                continue
            if len(matches) >= 50:
                break
        return ToolResult(True, json.dumps(matches, ensure_ascii=False))

    def _tool_read_text_file(self, arguments: dict[str, Any]) -> ToolResult:
        path = self._resolve_safe_path(str(arguments["path"]))
        if not path.is_file():
            return ToolResult(False, "文件不存在。")
        if path.stat().st_size > 2_000_000:
            return ToolResult(False, "文件超过 2MB，首版不读取。")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8-sig")
        return ToolResult(True, content[:10_000])
