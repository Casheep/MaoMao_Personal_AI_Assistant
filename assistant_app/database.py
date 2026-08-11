from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                memory_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL,
                estimated_cost REAL NOT NULL,
                currency TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                success INTEGER NOT NULL,
                result_summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                next_run_at TEXT NOT NULL,
                repeat_rule TEXT NOT NULL DEFAULT 'once',
                silent INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS visual_action_shortcuts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                app_name TEXT NOT NULL,
                window_title TEXT NOT NULL DEFAULT '',
                x_ratio REAL NOT NULL,
                y_ratio REAL NOT NULL,
                screen_left INTEGER NOT NULL,
                screen_top INTEGER NOT NULL,
                screen_width INTEGER NOT NULL,
                screen_height INTEGER NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(description, app_name)
            );

            CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
            ON scheduled_tasks(enabled, next_run_at);

            CREATE INDEX IF NOT EXISTS idx_visual_action_shortcuts_recent
            ON visual_action_shortcuts(app_name, updated_at DESC);
            """
        )
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(api_usage)").fetchall()
        }
        if "estimated_cost_usd" in columns:
            self.connection.executescript(
                """
                ALTER TABLE api_usage RENAME TO api_usage_legacy;
                CREATE TABLE api_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL,
                    estimated_cost REAL NOT NULL,
                    currency TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO api_usage(
                    id, session_id, task_id, model, input_tokens,
                    cached_input_tokens, output_tokens, estimated_cost,
                    currency, created_at
                )
                SELECT
                    id, session_id, task_id, model, input_tokens,
                    cached_input_tokens, output_tokens, estimated_cost_usd,
                    'USD', created_at
                FROM api_usage_legacy;
                DROP TABLE api_usage_legacy;
                """
            )
        memory_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "memory_key" not in memory_columns:
            self.connection.execute(
                "ALTER TABLE memories ADD COLUMN memory_key TEXT NOT NULL DEFAULT ''"
            )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_memory_key
            ON memories(memory_key) WHERE memory_key <> ''
            """
        )
        self.connection.commit()

    def remember_visual_action(
        self,
        description: str,
        app_name: str,
        window_title: str,
        x_ratio: float,
        y_ratio: float,
        screen_bounds: tuple[int, int, int, int],
    ) -> int:
        """Store a successful semantic click so it can be reused without vision."""
        description = " ".join(str(description).split()).strip()
        app_name = str(app_name or "unknown").strip().lower()
        if not description or app_name == "unknown":
            return 0
        left, top, width, height = screen_bounds
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO visual_action_shortcuts(
                description, app_name, window_title, x_ratio, y_ratio,
                screen_left, screen_top, screen_width, screen_height,
                use_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(description, app_name) DO UPDATE SET
                window_title=excluded.window_title,
                x_ratio=excluded.x_ratio,
                y_ratio=excluded.y_ratio,
                screen_left=excluded.screen_left,
                screen_top=excluded.screen_top,
                screen_width=excluded.screen_width,
                screen_height=excluded.screen_height,
                updated_at=excluded.updated_at
            """,
            (
                description,
                app_name,
                str(window_title)[:240],
                min(1.0, max(0.0, float(x_ratio))),
                min(1.0, max(0.0, float(y_ratio))),
                int(left),
                int(top),
                max(1, int(width)),
                max(1, int(height)),
                now,
                now,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM visual_action_shortcuts WHERE description=? AND app_name=?",
            (description, app_name),
        ).fetchone()
        self.connection.commit()
        return int(row["id"]) if row is not None else 0

    def list_visual_actions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM visual_action_shortcuts
            ORDER BY updated_at DESC LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def visual_action(self, shortcut_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM visual_action_shortcuts WHERE id=?",
            (int(shortcut_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_visual_action_used(self, shortcut_id: int) -> None:
        self.connection.execute(
            """
            UPDATE visual_action_shortcuts
            SET use_count=use_count+1, updated_at=? WHERE id=?
            """,
            (utc_now(), int(shortcut_id)),
        )
        self.connection.commit()

    def delete_visual_action(self, shortcut_id: int) -> None:
        self.connection.execute(
            "DELETE FROM visual_action_shortcuts WHERE id=?",
            (int(shortcut_id),),
        )
        self.connection.commit()

    @staticmethod
    def _local_schedule_time(value: str | datetime) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.replace(microsecond=0)

    def create_scheduled_task(
        self,
        command: str,
        run_at: str | datetime,
        repeat_rule: str = "once",
        silent: bool = False,
    ) -> int:
        command = command.strip()
        if not command:
            raise ValueError("定时任务内容不能为空。")
        if repeat_rule not in {"once", "daily"}:
            raise ValueError("重复方式只能是 once 或 daily。")
        scheduled = self._local_schedule_time(run_at)
        now_local = datetime.now().replace(microsecond=0)
        if scheduled <= now_local:
            if repeat_rule != "daily":
                raise ValueError("一次性任务的执行时间必须晚于当前时间。")
            while scheduled <= now_local:
                scheduled += timedelta(days=1)
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO scheduled_tasks(
                command, next_run_at, repeat_rule, silent, enabled,
                last_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, 'pending', ?, ?)
            """,
            (
                command,
                scheduled.isoformat(timespec="seconds"),
                repeat_rule,
                int(silent),
                now,
                now,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_scheduled_tasks(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        where = "" if include_disabled else "WHERE enabled = 1"
        rows = self.connection.execute(
            f"""
            SELECT id, command, next_run_at, repeat_rule, silent, enabled,
                   last_run_at, last_status, last_result, created_at, updated_at
            FROM scheduled_tasks {where}
            ORDER BY enabled DESC, next_run_at ASC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def due_scheduled_tasks(
        self,
        now: str | datetime | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        current = self._local_schedule_time(now or datetime.now()).isoformat(timespec="seconds")
        rows = self.connection.execute(
            """
            SELECT id, command, next_run_at, repeat_rule, silent, enabled,
                   last_run_at, last_status, last_result
            FROM scheduled_tasks
            WHERE enabled = 1 AND next_run_at <= ?
            ORDER BY next_run_at ASC LIMIT ?
            """,
            (current, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_scheduled_task(
        self,
        task_id: int,
        now: str | datetime | None = None,
    ) -> dict[str, Any] | None:
        current = self._local_schedule_time(now or datetime.now())
        current_text = current.isoformat(timespec="seconds")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT id, command, next_run_at, repeat_rule, silent, enabled
                FROM scheduled_tasks
                WHERE id = ? AND enabled = 1 AND next_run_at <= ?
                """,
                (int(task_id), current_text),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return None
            claimed = dict(row)
            scheduled = self._local_schedule_time(str(row["next_run_at"]))
            repeat_rule = str(row["repeat_rule"])
            enabled = 0
            next_run = scheduled
            if repeat_rule == "daily":
                enabled = 1
                while next_run <= current:
                    next_run += timedelta(days=1)
            self.connection.execute(
                """
                UPDATE scheduled_tasks
                SET enabled = ?, next_run_at = ?, last_run_at = ?,
                    last_status = 'running', last_result = '', updated_at = ?
                WHERE id = ?
                """,
                (
                    enabled,
                    next_run.isoformat(timespec="seconds"),
                    current_text,
                    utc_now(),
                    int(task_id),
                ),
            )
            self.connection.commit()
            claimed["scheduled_for"] = scheduled.isoformat(timespec="seconds")
            claimed["enabled"] = enabled
            claimed["next_run_at"] = next_run.isoformat(timespec="seconds")
            return claimed
        except Exception:
            self.connection.rollback()
            raise

    def complete_scheduled_task(
        self,
        task_id: int,
        success: bool,
        result: str = "",
    ) -> None:
        self.connection.execute(
            """
            UPDATE scheduled_tasks
            SET last_status = ?, last_result = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "succeeded" if success else "failed",
                str(result)[:2000],
                utc_now(),
                int(task_id),
            ),
        )
        self.connection.commit()

    def cancel_scheduled_task(self, task_id: int) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE scheduled_tasks
            SET enabled = 0, last_status = 'cancelled', updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), int(task_id)),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def recover_interrupted_scheduled_tasks(self) -> int:
        cursor = self.connection.execute(
            """
            UPDATE scheduled_tasks
            SET last_status = 'failed',
                last_result = '猫猫上次运行中途退出，任务状态已恢复。',
                updated_at = ?
            WHERE last_status = 'running'
            """,
            (utc_now(),),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def delete_scheduled_task(self, task_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM scheduled_tasks WHERE id = ?",
            (int(task_id),),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def add_message(self, session_id: str, role: str, content: str) -> None:
        self.connection.execute(
            "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, utc_now()),
        )
        self.connection.commit()

    def recent_messages(self, session_id: str, limit: int) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT role, content FROM messages
            WHERE session_id = ? ORDER BY id DESC LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_messages(self, session_id: str) -> int:
        """Delete one conversation session without touching memories or logs."""
        cursor = self.connection.execute(
            "DELETE FROM messages WHERE session_id = ?",
            (session_id,),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def remember(self, content: str, tags: str = "", memory_key: str = "") -> int:
        now = utc_now()
        content = content.strip()
        tags = tags.strip()
        memory_key = memory_key.strip().lower()
        if memory_key:
            existing = self.connection.execute(
                "SELECT id FROM memories WHERE memory_key = ?", (memory_key,)
            ).fetchone()
            if existing is not None:
                self.connection.execute(
                    """
                    UPDATE memories SET content = ?, tags = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (content, tags, now, int(existing["id"])),
                )
                self.connection.commit()
                return int(existing["id"])
        existing = self.connection.execute(
            "SELECT id, tags FROM memories WHERE content = ?", (content,)
        ).fetchone()
        if existing is not None:
            self.connection.execute(
                """
                UPDATE memories SET tags = ?, memory_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (tags or str(existing["tags"]), memory_key, now, int(existing["id"])),
            )
            self.connection.commit()
            return int(existing["id"])
        cursor = self.connection.execute(
            """
            INSERT INTO memories(content, tags, memory_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (content, tags, memory_key, now, now),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, content, tags, memory_key, created_at
            FROM memories ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def profile_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return stable keyed facts that should be available in every turn."""
        rows = self.connection.execute(
            """
            SELECT id, content, tags, memory_key
            FROM memories WHERE memory_key <> ''
            ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_memories(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        compact = "".join(query.split())
        candidates: list[str] = []
        for token in query.replace("，", " ").replace("。", " ").split():
            if len(token) >= 2:
                candidates.append(token)
        if len(compact) >= 4:
            candidates.extend(compact[index : index + 2] for index in range(len(compact) - 1))
        candidates = list(dict.fromkeys(candidates))[:12]
        if not candidates:
            return []
        clauses = " OR ".join("content LIKE ? OR tags LIKE ?" for _ in candidates)
        values: list[Any] = []
        for candidate in candidates:
            pattern = f"%{candidate}%"
            values.extend([pattern, pattern])
        values.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT id, content, tags, memory_key FROM memories
            WHERE {clauses} ORDER BY updated_at DESC LIMIT ?
            """,
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def log_usage(
        self,
        session_id: str,
        task_id: str,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        cost: float,
        currency: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO api_usage(
                session_id, task_id, model, input_tokens, cached_input_tokens,
                output_tokens, estimated_cost, currency, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                task_id,
                model,
                input_tokens,
                cached_input_tokens,
                output_tokens,
                cost,
                currency,
                utc_now(),
            ),
        )
        self.connection.commit()

    def usage_total(
        self,
        period: str,
        task_id: str | None = None,
        currency: str | None = None,
    ) -> float:
        if period == "day":
            time_clause = "date(created_at) = date('now')"
        elif period == "month":
            time_clause = "strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"
        else:
            raise ValueError(f"Unsupported period: {period}")
        params: list[Any] = []
        if task_id is not None:
            time_clause += " AND task_id = ?"
            params.append(task_id)
        if currency is not None:
            time_clause += " AND currency = ?"
            params.append(currency)
        row = self.connection.execute(
            f"SELECT COALESCE(SUM(estimated_cost), 0) AS total FROM api_usage WHERE {time_clause}",
            params,
        ).fetchone()
        return float(row["total"])

    def usage_summary(self, currency: str | None = None) -> dict[str, float]:
        return {
            "today": self.usage_total("day", currency=currency),
            "month": self.usage_total("month", currency=currency),
        }

    def log_action(
        self,
        session_id: str,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        result_summary: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO action_log(
                session_id, task_id, tool_name, arguments_json,
                success, result_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                task_id,
                tool_name,
                json.dumps(arguments, ensure_ascii=False),
                int(success),
                result_summary[:1000],
                utc_now(),
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
