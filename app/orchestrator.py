"""Orchestrator for scanner + matcher runs.

Provides:
  * Manual one-shot triggers for scanner / matcher, called by the web UI buttons.
  * Background scheduler (APScheduler) for periodic runs.
  * Run history (in-DB) for the dashboard.
  * Mutual exclusion so two scanners or a scanner + matcher never overlap --
    they both touch the same `files` rows and you'd race state.

Why in-process and not docker-compose-run-spawning?
  - Compose's CLI isn't available from inside a container without giving the
    container the Docker socket and the compose binary; both are footguns.
  - In-process is faster (no container spawn), uses the same DB pool, gives
    us live status updates without polling the Docker API.
  - The downside is that a crash in scanner/matcher takes down the web
    container -- but each run is wrapped in try/except so this is just a
    log line, not a real risk.

Why not RQ?
  - Scanner+matcher are slow-but-rare housekeeping tasks, conceptually
    different from the high-throughput per-file fingerprinting queue.
    Putting them on the same queue means they'd queue behind 14000 file
    jobs. Run them in dedicated threads instead.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator

from .db import session

log = logging.getLogger("orchestrator")


# In-memory state. Single web container means we don't need cross-process
# locks for the "is anything running" check.
_run_lock = threading.Lock()
_current_run: dict | None = None     # {"kind": "scanner"|"matcher", "started_at": dt, "id": int}


# ---------------------------------------------------------------------------
# Schema for run history
# ---------------------------------------------------------------------------
# Created lazily on first use; doesn't need to be in init.sql since this
# module guards every access. We use the same "create if not exists" pattern
# as ensure_schema so this is safe to call from multiple services.

_ORCH_TABLE = """
CREATE TABLE IF NOT EXISTS orchestrator_runs (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('scanner', 'matcher')),
    triggered_by TEXT NOT NULL CHECK (triggered_by IN ('manual', 'scheduled')),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    succeeded    BOOLEAN,
    error        TEXT,
    stats        JSONB
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_runs_kind_started
    ON orchestrator_runs (kind, started_at DESC);
"""


def ensure_schema() -> None:
    """Create the orchestrator_runs table if missing."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(_ORCH_TABLE)


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------

def _start_run(kind: str, triggered_by: str) -> int:
    """Insert a row into orchestrator_runs and return its id."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orchestrator_runs (kind, triggered_by) "
            "VALUES (%s, %s) RETURNING id",
            (kind, triggered_by),
        )
        return cur.fetchone()["id"]


def _finish_run(run_id: int, succeeded: bool, error: str | None, stats: dict | None) -> None:
    import json
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE orchestrator_runs
                  SET finished_at = NOW(),
                      succeeded = %s,
                      error = %s,
                      stats = %s::jsonb
                WHERE id = %s""",
            (succeeded, error[:2000] if error else None,
             json.dumps(stats) if stats else None, run_id),
        )


@contextmanager
def _claim_runner(kind: str, triggered_by: str) -> Iterator[int | None]:
    """Acquire the run-lock and record the start of a run.

    Yields the run_id if we acquired the lock, or None if another run is
    already in progress (caller should skip).
    """
    global _current_run
    acquired = _run_lock.acquire(blocking=False)
    if not acquired:
        # Someone else is running.
        yield None
        return
    try:
        run_id = _start_run(kind, triggered_by)
        _current_run = {
            "kind": kind, "started_at": datetime.now(timezone.utc),
            "id": run_id, "triggered_by": triggered_by,
        }
        try:
            yield run_id
        finally:
            _current_run = None
    finally:
        _run_lock.release()


# ---------------------------------------------------------------------------
# Run implementations
# ---------------------------------------------------------------------------

def _run_one(kind: str, triggered_by: str, work_fn: Callable[[], dict | None]) -> dict:
    """Wrap a work function with run-tracking + error handling.

    Returns a dict describing the outcome (consumed by the UI).
    """
    with _claim_runner(kind, triggered_by) as run_id:
        if run_id is None:
            log.info("Skipping %s run (%s) -- another job is already in progress",
                     kind, triggered_by)
            return {"started": False, "reason": "already_running"}

        log.info("Starting %s run #%s (%s)", kind, run_id, triggered_by)
        t0 = time.monotonic()
        try:
            stats = work_fn() or {}
            elapsed = time.monotonic() - t0
            stats["elapsed_secs"] = round(elapsed, 1)
            _finish_run(run_id, succeeded=True, error=None, stats=stats)
            log.info("%s run #%s done in %.1fs: %s", kind, run_id, elapsed, stats)
            return {"started": True, "run_id": run_id, "succeeded": True, "stats": stats}
        except Exception as e:                                       # noqa: BLE001
            elapsed = time.monotonic() - t0
            log.exception("%s run #%s failed after %.1fs", kind, run_id, elapsed)
            _finish_run(run_id, succeeded=False, error=str(e),
                        stats={"elapsed_secs": round(elapsed, 1)})
            return {"started": True, "run_id": run_id, "succeeded": False, "error": str(e)}


def _scanner_work(source_ids: list[int] | None = None) -> dict:
    """Call into scanner and return stats. ``source_ids=None`` means all
    enabled sources."""
    from . import scanner
    stats = scanner.main_for_source_ids(source_ids)
    # Plus a status snapshot.
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) AS n FROM files GROUP BY status"
        )
        stats["by_status"] = {r["status"]: r["n"] for r in cur.fetchall()}
    return stats


def _matcher_work() -> dict:
    """Run filename matcher first (cheap), then fingerprint matcher."""
    from . import filename_match, matcher

    fname_stats = {}
    try:
        log.info("Running filename matcher (phase 1)")
        video_stats = filename_match.find_filename_matches(media_type="video")
        audio_stats = filename_match.find_filename_matches(media_type="audio")
        fname_stats = {"video": video_stats, "audio": audio_stats}
        log.info("Filename matcher done: %s", fname_stats)
    except Exception:
        log.exception("filename matcher failed; continuing to fingerprint matcher")

    log.info("Running fingerprint matcher (phase 2)")
    matcher.main()

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT media_type, match_type, COUNT(*) AS n
                 FROM dup_groups GROUP BY 1,2 ORDER BY 1,2"""
        )
        groups = [
            {"media_type": r["media_type"], "match_type": r["match_type"], "n": r["n"]}
            for r in cur.fetchall()
        ]
    return {"filename": fname_stats, "groups": groups}


def _filename_match_work() -> dict:
    """Run just the filename matcher, no fingerprint pass.

    Used by the dedicated "Run filename matcher" button so the operator can
    surface name-matched dupes without waiting for the slow fingerprint
    matcher to complete.
    """
    from . import filename_match
    video_stats = filename_match.find_filename_matches(media_type="video")
    audio_stats = filename_match.find_filename_matches(media_type="audio")
    return {"video": video_stats, "audio": audio_stats}


def _llm_match_work() -> dict:
    """Run the LLM matcher on top of existing groups."""
    from . import llm_match
    stats = llm_match.find_llm_matches(media_type="video")
    return stats


def run_scanner(triggered_by: str = "manual") -> dict:
    return _run_one("scanner", triggered_by, _scanner_work)


def run_scanner_for(source_ids: list[int] | None,
                    triggered_by: str = "manual") -> dict:
    """Run scanner against a specific set of sources, or all if None."""
    return _run_one("scanner", triggered_by, lambda: _scanner_work(source_ids))


def run_matcher(triggered_by: str = "manual") -> dict:
    return _run_one("matcher", triggered_by, _matcher_work)


def run_filename_matcher(triggered_by: str = "manual") -> dict:
    """Standalone run of just the filename matcher -- cheap, no GPU/IO."""
    return _run_one("matcher", triggered_by, _filename_match_work)


def run_llm_matcher(triggered_by: str = "manual") -> dict:
    """Standalone run of the LLM matcher.

    Tracks via the same orchestrator_runs table as scanner/matcher. Reuses
    'matcher' kind because a separate kind would require a CHECK constraint
    migration and the runs are conceptually matchers.
    """
    return _run_one("matcher", triggered_by, _llm_match_work)


def run_scanner_then_matcher(triggered_by: str = "scheduled",
                             source_ids: list[int] | None = None) -> dict:
    """Convenience wrapper used by the scheduler -- scan, then match.

    These are sequential by design (matcher should run against the freshly-
    scanned data) but each gets its own run-tracking row.
    """
    s = run_scanner_for(source_ids, triggered_by)
    if not s.get("started"):
        return {"scanner": s, "matcher": {"started": False, "reason": "scanner_skipped"}}
    m = run_matcher(triggered_by)
    return {"scanner": s, "matcher": m}


# ---------------------------------------------------------------------------
# Status reporting for the dashboard
# ---------------------------------------------------------------------------

def current_run() -> dict | None:
    """Return info about the run in progress, or None if idle.

    Pure read; doesn't acquire the lock.
    """
    cr = _current_run
    if cr is None:
        return None
    elapsed = (datetime.now(timezone.utc) - cr["started_at"]).total_seconds()
    return {
        "kind": cr["kind"],
        "started_at": cr["started_at"].isoformat(),
        "triggered_by": cr["triggered_by"],
        "elapsed_secs": int(elapsed),
        "run_id": cr["id"],
    }


def recent_runs(limit: int = 10) -> list[dict]:
    """Most-recent N runs of any kind, for the dashboard activity feed."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, kind, triggered_by, started_at, finished_at,
                      succeeded, error, stats
                 FROM orchestrator_runs
                ORDER BY id DESC
                LIMIT %s""",
            (limit,),
        )
        out = []
        for r in cur.fetchall():
            elapsed = None
            if r["finished_at"]:
                elapsed = (r["finished_at"] - r["started_at"]).total_seconds()
            out.append({
                "id": r["id"],
                "kind": r["kind"],
                "triggered_by": r["triggered_by"],
                "started_at": r["started_at"].isoformat(),
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "elapsed_secs": int(elapsed) if elapsed is not None else None,
                "succeeded": r["succeeded"],
                "error": r["error"],
                "stats": r["stats"],
            })
        return out
