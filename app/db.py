"""Database helpers using psycopg3."""
from __future__ import annotations

import contextlib
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import CFG


def connect() -> psycopg.Connection:
    """Open a new psycopg connection. Caller is responsible for closing."""
    return psycopg.connect(CFG.db_dsn, row_factory=dict_row, autocommit=False)


@contextlib.contextmanager
def session() -> Iterator[psycopg.Connection]:
    """Context manager that commits on clean exit and rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Idempotent column-existence migrations. Each entry is (table, column,
# definition). On startup we check whether the column exists and only ALTER
# when it doesn't -- avoids a lock-storm when many services start at once.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("files", "processing_started_at", "TIMESTAMPTZ"),
    ("files", "source_id", "BIGINT"),
]

# DDL run before column migrations. These all use IF NOT EXISTS, so they're
# no-ops on subsequent runs.
_TABLE_MIGRATIONS: list[str] = [
    """CREATE TABLE IF NOT EXISTS sources (
        id          BIGSERIAL PRIMARY KEY,
        name        TEXT UNIQUE NOT NULL,
        path        TEXT UNIQUE NOT NULL,
        media_type  TEXT NOT NULL CHECK (media_type IN ('video', 'audio', 'both')),
        priority    INTEGER NOT NULL DEFAULT 0,
        enabled     BOOLEAN NOT NULL DEFAULT TRUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        notes       TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled)",
    "CREATE INDEX IF NOT EXISTS idx_files_source_id ON files(source_id)",
    # Allow match_type='filename' (added when the filename-match pass landed).
    # The original constraint name follows Postgres's default
    # ``<table>_<column>_check`` pattern. We DROP IF EXISTS so a fresh install
    # using the new init.sql constraint stays untouched.
    "ALTER TABLE dup_groups DROP CONSTRAINT IF EXISTS dup_groups_match_type_check",
    """ALTER TABLE dup_groups
        ADD CONSTRAINT dup_groups_match_type_check
        CHECK (match_type IN ('exact', 'perceptual', 'chromaprint', 'filename', 'llm'))""",
]

# Stable arbitrary key for our cross-process advisory lock. Picking a
# constant here so all services agree.
_MIGRATION_LOCK_KEY = 0x4D454449_44455550   # ascii "MEDIDEUP" truncated


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s AND column_name = %s""",
        (table, column),
    )
    return cur.fetchone() is not None


def ensure_schema() -> None:
    """Apply missing-column migrations. Safe to call from any number of
    services concurrently.

    Strategy:
      1. Open a connection. Set a 10-second statement timeout so we never
         hang forever waiting on a DB lock.
      2. Take a session-level advisory lock so only one service runs the
         migration at a time. Others wait briefly here, not on ALTER TABLE.
      3. For each migration, check column existence first; only ALTER if
         actually missing. After the first service migrates, every other
         service sees the column and skips the ALTER entirely (no lock
         taken on `files`).
      4. Release the advisory lock and close.

    On any failure we log and return; callers proceed without crashing
    (a missing column will surface as a clear query error later).
    """
    import logging
    log = logging.getLogger("db.migrate")

    try:
        conn = psycopg.connect(CFG.db_dsn, row_factory=dict_row, autocommit=True)
    except Exception:
        log.exception("ensure_schema: could not connect")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '10s'")
            try:
                cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
            except Exception:
                log.exception("ensure_schema: could not acquire advisory lock")
                return

            try:
                # Tables first, so column migrations can target newly-added tables.
                for ddl in _TABLE_MIGRATIONS:
                    cur.execute(ddl)
                for table, column, ddl in _COLUMN_MIGRATIONS:
                    if _column_exists(cur, table, column):
                        continue
                    log.info("ensure_schema: adding %s.%s %s", table, column, ddl)
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except Exception:
                log.exception("ensure_schema: migration failed")
            finally:
                try:
                    cur.execute("SELECT pg_advisory_unlock(%s)",
                                (_MIGRATION_LOCK_KEY,))
                except Exception:
                    pass
    finally:
        conn.close()
