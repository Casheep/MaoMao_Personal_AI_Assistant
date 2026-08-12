from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._read_cache: dict[tuple[Any, ...], tuple[int, tuple[dict[str, Any], ...]]] = {}
        self._fts_available = False
        self._initialize()

    def _data_version(self) -> int:
        return int(self.connection.execute("PRAGMA data_version").fetchone()[0])

    def _invalidate_read_cache(self) -> None:
        self._read_cache.clear()

    def _commit(self) -> None:
        self.connection.commit()
        self._invalidate_read_cache()

    def _cached_rows(
        self,
        key: tuple[Any, ...],
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        version = self._data_version()
        cached = self._read_cache.get(key)
        if cached is None or cached[0] != version:
            rows = tuple(
                dict(row) for row in self.connection.execute(query, parameters).fetchall()
            )
            cached = (version, rows)
            self._read_cache[key] = cached
        return [dict(row) for row in cached[1]]

    def _initialize(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}。"
            )
        if version == SCHEMA_VERSION:
            self._fts_available = bool(
                self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories_fts'"
                ).fetchone()
            )
            return

        self.connection.executescript(
            """
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

            CREATE INDEX IF NOT EXISTS idx_messages_session_recent
            ON messages(session_id, id DESC);

            CREATE INDEX IF NOT EXISTS idx_memories_recent
            ON memories(updated_at DESC);

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
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_usage_currency_time
            ON api_usage(currency, created_at)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_usage_task_time
            ON api_usage(task_id, currency, created_at)
            """
        )
        self.connection.execute(
            """
            UPDATE api_usage
            SET created_at = replace(created_at, ' ', 'T')
            WHERE substr(created_at, 11, 1) = ' '
            """
        )
        try:
            self.connection.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content,
                    tags,
                    content='memories',
                    content_rowid='id',
                    tokenize='trigram'
                );

                CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, tags)
                    VALUES (new.id, new.content, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                    VALUES ('delete', old.id, old.content, old.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                    VALUES ('delete', old.id, old.content, old.tags);
                    INSERT INTO memories_fts(rowid, content, tags)
                    VALUES (new.id, new.content, new.tags);
                END;

                INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');
                """
            )
            self._fts_available = True
        except sqlite3.OperationalError:
            self._fts_available = False
        self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._commit()

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
        self._commit()
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
        self._commit()

    def delete_visual_action(self, shortcut_id: int) -> None:
        self.connection.execute(
            "DELETE FROM visual_action_shortcuts WHERE id=?",
            (int(shortcut_id),),
        )
        self._commit()

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
        self._commit()
        return int(cursor.lastrowid)

    def list_scheduled_tasks(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        where = "" if include_disabled else "WHERE enabled = 1"
        return self._cached_rows(
            ("scheduled_tasks", include_disabled),
            f"""
            SELECT id, command, next_run_at, repeat_rule, silent, enabled,
                   last_run_at, last_status, last_result, created_at, updated_at
            FROM scheduled_tasks {where}
            ORDER BY enabled DESC, next_run_at ASC, id DESC
            """,
        )

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
            self._commit()
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
        self._commit()

    def cancel_scheduled_task(self, task_id: int) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE scheduled_tasks
            SET enabled = 0, last_status = 'cancelled', updated_at = ?
            WHERE id = ?
            """,
            (utc_now(), int(task_id)),
        )
        self._commit()
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
        self._commit()
        return int(cursor.rowcount)

    def delete_scheduled_task(self, task_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM scheduled_tasks WHERE id = ?",
            (int(task_id),),
        )
        self._commit()
        return bool(cursor.rowcount)

    def add_message(self, session_id: str, role: str, content: str) -> None:
        self.add_messages(session_id, ((role, content),))

    def add_messages(
        self,
        session_id: str,
        messages: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    ) -> None:
        """Persist one conversation exchange with a single transaction."""
        now = utc_now()
        self.connection.executemany(
            "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            ((session_id, role, content, now) for role, content in messages),
        )
        self._commit()

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
        self._commit()
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
                self._commit()
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
            self._commit()
            return int(existing["id"])
        cursor = self.connection.execute(
            """
            INSERT INTO memories(content, tags, memory_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (content, tags, memory_key, now, now),
        )
        self._commit()
        return int(cursor.lastrowid)

    def list_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._cached_rows(
            ("memories", int(limit)),
            """
            SELECT id, content, tags, memory_key, created_at
            FROM memories ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        )

    def profile_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return stable keyed facts that should be available in every turn."""
        return self._cached_rows(
            ("profile_memories", int(limit)),
            """
            SELECT id, content, tags, memory_key
            FROM memories WHERE memory_key <> ''
            ORDER BY updated_at DESC LIMIT ?
            """,
            (limit,),
        )

    def search_memories(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        compact = "".join(query.split())
        if len(compact) < 2:
            return []
        if self._fts_available and len(compact) >= 3:
            terms = [compact[index : index + 3] for index in range(len(compact) - 2)]
            terms.extend(
                token
                for token in query.replace("，", " ").replace("。", " ").split()
                if len(token) >= 3
            )
            expression = " OR ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"'
                for term in list(dict.fromkeys(terms))[:16]
            )
            return self._cached_rows(
                ("memory_search_fts", expression, int(limit)),
                """
                SELECT memories.id, memories.content, memories.tags, memories.memory_key
                FROM memories_fts
                JOIN memories ON memories.id = memories_fts.rowid
                WHERE memories_fts MATCH ?
                ORDER BY bm25(memories_fts), memories.updated_at DESC
                LIMIT ?
                """,
                (expression, int(limit)),
            )

        candidates = [
            token
            for token in query.replace("，", " ").replace("。", " ").split()
            if len(token) >= 2
        ]
        candidates = list(dict.fromkeys([*candidates, compact]))[:8]
        clauses = " OR ".join("content LIKE ? OR tags LIKE ?" for _ in candidates)
        values: list[Any] = []
        for candidate in candidates:
            pattern = f"%{candidate}%"
            values.extend([pattern, pattern])
        values.append(limit)
        return self._cached_rows(
            ("memory_search_like", tuple(candidates), int(limit)),
            f"""
            SELECT id, content, tags, memory_key FROM memories
            WHERE {clauses} ORDER BY updated_at DESC LIMIT ?
            """,
            tuple(values),
        )

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
        self._commit()

    def usage_total(
        self,
        period: str,
        task_id: str | None = None,
        currency: str | None = None,
    ) -> float:
        now = datetime.now(timezone.utc)
        if period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            raise ValueError(f"Unsupported period: {period}")
        clauses = ["created_at >= ?"]
        params: list[Any] = [start.isoformat()]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if currency is not None:
            clauses.append("currency = ?")
            params.append(currency)
        row = self.connection.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0) AS total "
            f"FROM api_usage WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()
        return float(row["total"])

    def usage_snapshot(
        self,
        currency: str,
        task_id: str | None = None,
    ) -> dict[str, float]:
        """Read daily, monthly and optional per-task usage in one indexed query."""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        month_start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        row = self.connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN created_at >= ? THEN estimated_cost ELSE 0 END), 0)
                    AS today,
                COALESCE(SUM(estimated_cost), 0) AS month,
                COALESCE(SUM(CASE
                    WHEN created_at >= ? AND task_id = ? THEN estimated_cost ELSE 0 END), 0)
                    AS task
            FROM api_usage
            WHERE currency = ? AND created_at >= ?
            """,
            (day_start, day_start, task_id or "", currency, month_start),
        ).fetchone()
        return {key: float(row[key]) for key in ("today", "month", "task")}

    def usage_summary(self, currency: str | None = None) -> dict[str, float]:
        if currency is None:
            return {
                "today": self.usage_total("day"),
                "month": self.usage_total("month"),
            }
        snapshot = self.usage_snapshot(currency)
        return {key: snapshot[key] for key in ("today", "month")}

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
        self._commit()

    def close(self) -> None:
        self.connection.close()
