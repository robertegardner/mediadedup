"""Key-value settings persisted to the database.

Used for runtime-tunable knobs that don't belong in `.env` because they
should change without rebuilding the image. Currently consumed by the LLM
matcher; other modules can use it freely.

Schema is created on demand. Values are TEXT -- callers handle their own
type conversion.
"""
from __future__ import annotations

import logging
from typing import Optional

from .db import session

log = logging.getLogger("settings")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def ensure_schema() -> None:
    with session() as conn, conn.cursor() as cur:
        cur.execute(_SCHEMA)


def get(key: str) -> Optional[str]:
    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            r = cur.fetchone()
            return r["value"] if r else None
    except Exception:                                                # noqa: BLE001
        log.exception("settings.get(%r) failed", key)
        return None


def set(key: str, value: Optional[str]) -> None:                     # noqa: A001
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO settings (key, value)
               VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value,
                       updated_at = NOW()""",
            (key, value),
        )


def get_many(prefix: str) -> dict[str, str]:
    """Return all settings with keys starting with ``prefix``."""
    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM settings WHERE key LIKE %s",
                (prefix + "%",),
            )
            return {r["key"]: r["value"] or "" for r in cur.fetchall()}
    except Exception:                                                # noqa: BLE001
        log.exception("settings.get_many(%r) failed", prefix)
        return {}


def set_many(items: dict[str, str]) -> None:
    if not items:
        return
    with session() as conn, conn.cursor() as cur:
        for k, v in items.items():
            cur.execute(
                """INSERT INTO settings (key, value)
                   VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE
                       SET value = EXCLUDED.value, updated_at = NOW()""",
                (k, v),
            )
