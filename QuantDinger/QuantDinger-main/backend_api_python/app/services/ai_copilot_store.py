"""Persistence helpers for AI Copilot sessions, messages, and memories."""

import json
import re
from datetime import date, datetime, timezone
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def row_to_dict(row: Any) -> dict:
    return dict(row or {})


def json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def get_user_memories(cur, user_id: int, limit: int = 12) -> list[dict]:
    try:
        cur.execute(
            """
            SELECT id, category, title, content, confidence, updated_at
            FROM qd_ai_user_memories
            WHERE user_id = ? AND is_active = TRUE
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (user_id, int(limit)),
        )
        return [row_to_dict(r) for r in (cur.fetchall() or [])]
    except Exception as exc:
        logger.debug("Failed to load user memories: %s", exc)
        return []


def detect_memory_candidates(message: str, language: str) -> list[dict]:
    text = (message or "").strip()
    if len(text) < 8:
        return []
    lower = text.lower()
    zh = (language or "").lower().startswith("zh")
    markers = [
        "我偏好", "我的偏好", "我喜欢", "我不喜欢", "不要", "不希望", "风险偏好", "交易周期",
        "timeframe", "risk profile", "i prefer", "i like", "i don't want", "avoid", "do not",
    ]
    if not any(m.lower() in lower for m in markers):
        return []
    title = "交易偏好" if zh else "Trading preference"
    if any(m in lower for m in ("不要", "不希望", "avoid", "don't want", "do not")):
        title = "交易限制" if zh else "Trading constraint"
    return [{
        "category": "preference",
        "title": title,
        "content": text[:500],
        "confidence": 75,
    }]


def ensure_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS qd_ai_copilot_sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title VARCHAR(160),
            context_symbol VARCHAR(64),
            context_market VARCHAR(32),
            context_strategy_id INTEGER,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS qd_ai_copilot_messages (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            attachments_json TEXT,
            actions_json TEXT,
            report_json TEXT,
            report_target_json TEXT,
            report_error TEXT,
            report_error_tone VARCHAR(32),
            intent VARCHAR(48),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS qd_ai_copilot_tool_calls (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            tool_name VARCHAR(64) NOT NULL,
            status VARCHAR(24) NOT NULL,
            input_json TEXT,
            output_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS qd_ai_user_memories (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            category VARCHAR(48) NOT NULL DEFAULT 'preference',
            title VARCHAR(160) NOT NULL,
            content TEXT NOT NULL,
            source VARCHAR(48) DEFAULT 'copilot',
            confidence INTEGER DEFAULT 70,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    for ddl in (
        "ALTER TABLE qd_ai_copilot_messages ADD COLUMN IF NOT EXISTS actions_json TEXT",
        "ALTER TABLE qd_ai_copilot_messages ADD COLUMN IF NOT EXISTS report_json TEXT",
        "ALTER TABLE qd_ai_copilot_messages ADD COLUMN IF NOT EXISTS report_target_json TEXT",
        "ALTER TABLE qd_ai_copilot_messages ADD COLUMN IF NOT EXISTS report_error TEXT",
        "ALTER TABLE qd_ai_copilot_messages ADD COLUMN IF NOT EXISTS report_error_tone VARCHAR(32)",
    ):
        try:
            cur.execute(ddl)
        except Exception:
            try:
                cur.execute(ddl.replace(" IF NOT EXISTS", ""))
            except Exception:
                pass
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_qd_ai_copilot_sessions_user ON qd_ai_copilot_sessions(user_id, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_qd_ai_copilot_messages_session ON qd_ai_copilot_messages(session_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_qd_ai_user_memories_user ON qd_ai_user_memories(user_id, is_active, updated_at)",
    ):
        try:
            cur.execute(ddl)
        except Exception:
            pass


def title_from_message(message: str) -> str:
    title = re.sub(r"\s+", " ", (message or "").strip())
    return title[:60] if title else "New Copilot Chat"


def get_session(cur, user_id: int, session_id: int | None) -> dict | None:
    if not session_id:
        return None
    cur.execute(
        "SELECT * FROM qd_ai_copilot_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    )
    row = cur.fetchone()
    return row_to_dict(row) if row else None


def create_session(cur, user_id: int, title: str, context: dict) -> int:
    context_symbol = str((context or {}).get("symbol") or "")[:64]
    context_market = str((context or {}).get("market") or "")[:32]
    context_strategy_id = (context or {}).get("strategy_id")
    cur.execute(
        """
        INSERT INTO qd_ai_copilot_sessions
        (user_id, title, context_symbol, context_market, context_strategy_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, NOW(), NOW())
        RETURNING id
        """,
        (user_id, title, context_symbol, context_market, context_strategy_id),
    )
    row = cur.fetchone()
    return int(row["id"] if isinstance(row, dict) else row[0])


def insert_message(
    cur,
    *,
    session_id: int,
    user_id: int,
    role: str,
    content: str,
    attachments: list[dict] | None = None,
    actions: list[dict] | None = None,
    report: dict | None = None,
    report_target: dict | None = None,
    report_error: str | None = None,
    report_error_tone: str | None = None,
    intent: str | None = None,
) -> int:
    cur.execute(
        """
        INSERT INTO qd_ai_copilot_messages
        (session_id, user_id, role, content, attachments_json, actions_json,
         report_json, report_target_json, report_error, report_error_tone, intent, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
        RETURNING id
        """,
        (
            session_id,
            user_id,
            role,
            content or "",
            json_dumps(attachments or []),
            json_dumps(actions or []),
            json_dumps(report) if report else None,
            json_dumps(report_target) if report_target else None,
            report_error,
            report_error_tone,
            intent,
        ),
    )
    row = cur.fetchone()
    return int(row["id"] if isinstance(row, dict) else row[0])


def load_recent_messages(cur, session_id: int, limit: int = 12) -> list[dict]:
    cur.execute(
        """
        SELECT role, content, attachments_json, actions_json, report_json,
               report_target_json, report_error, report_error_tone, intent, created_at
        FROM qd_ai_copilot_messages
        WHERE session_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, int(limit)),
    )
    rows = [row_to_dict(r) for r in (cur.fetchall() or [])]
    return list(reversed(rows))
