"""APScheduler glue for periodic scanner+matcher runs.

The schedule is controlled by env vars:

  DEDUP_SCHEDULE_CRON   five-field cron expression in UTC. Default: every
                        day at 04:00 UTC ("0 4 * * *"). Set to empty string
                        to disable scheduling entirely.

  DEDUP_AUTO_DELETE_EXACT
                        if "1", auto-delete exact-match groups at the end
                        of each scheduled scanner+matcher run. Default: 0.

  DEDUP_AUTO_DELETE_SIM_THRESHOLD
                        if set (e.g. "0.98"), auto-delete similarity groups
                        at or above this threshold at end of each scheduled
                        run. Default: unset (disabled).

We use BackgroundScheduler (runs in its own thread inside the web container)
rather than spawning a separate service. There is exactly one web container,
so we don't have to worry about multiple schedulers firing the same job.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import actions, orchestrator

log = logging.getLogger("scheduler")


_scheduler: BackgroundScheduler | None = None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _scheduled_run() -> None:
    """Fire by APScheduler. Runs scanner, then matcher, then optionally
    auto-deletes per env config."""
    log.info("Scheduled run starting")
    result = orchestrator.run_scanner_then_matcher(triggered_by="scheduled")
    log.info("Scheduled scan+match: %s", result)

    auto_exact = _env("DEDUP_AUTO_DELETE_EXACT", "0") == "1"
    sim_thresh_str = _env("DEDUP_AUTO_DELETE_SIM_THRESHOLD", "")
    try:
        sim_thresh = float(sim_thresh_str) if sim_thresh_str else None
    except ValueError:
        log.warning("Invalid DEDUP_AUTO_DELETE_SIM_THRESHOLD=%r; ignoring",
                    sim_thresh_str)
        sim_thresh = None

    if auto_exact:
        log.info("Auto-deleting exact-match groups (DEDUP_AUTO_DELETE_EXACT=1)")
        try:
            actions.auto_mark_exact()
            summary = actions.execute_exact_groups()
            log.info("Auto-delete exact: %s", summary.as_dict())
        except Exception:                                            # noqa: BLE001
            log.exception("Auto-delete (exact) failed")

    if sim_thresh is not None:
        log.info("Auto-deleting similarity groups >= %.3f", sim_thresh)
        try:
            actions.auto_mark_groups(
                match_types=["perceptual", "chromaprint"],
                min_similarity=sim_thresh,
            )
            summary = actions.execute_groups(
                match_types=["perceptual", "chromaprint"],
                min_similarity=sim_thresh,
            )
            log.info("Auto-delete similarity: %s", summary.as_dict())
        except Exception:                                            # noqa: BLE001
            log.exception("Auto-delete (similarity) failed")


def start() -> Optional[BackgroundScheduler]:
    """Start the scheduler if a cron expression is configured.

    Returns the scheduler instance (for tests) or None if disabled.
    """
    global _scheduler
    cron_expr = _env("DEDUP_SCHEDULE_CRON", "0 4 * * *")
    if not cron_expr:
        log.info("Scheduler disabled (DEDUP_SCHEDULE_CRON is empty)")
        return None

    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone="UTC")
    except Exception:                                                # noqa: BLE001
        log.exception("Invalid DEDUP_SCHEDULE_CRON=%r; scheduler not starting",
                      cron_expr)
        return None

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        _scheduled_run,
        trigger,
        id="scan_then_match",
        name="Scheduled scan + match",
        coalesce=True,            # one run if many fires queue up
        max_instances=1,          # never overlap
        misfire_grace_time=3600,  # if missed by less than an hour, still run
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched

    log.info("Scheduler started with cron %r (UTC); next run at %s",
             cron_expr, _next_run_at())
    return sched


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def _next_run_at() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("scan_then_match")
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.isoformat()


def schedule_info() -> dict:
    """Return the current schedule details for the dashboard."""
    cron_expr = _env("DEDUP_SCHEDULE_CRON", "0 4 * * *")
    return {
        "enabled": bool(cron_expr and _scheduler is not None),
        "cron": cron_expr or None,
        "next_run_at": _next_run_at(),
        "auto_delete_exact": _env("DEDUP_AUTO_DELETE_EXACT", "0") == "1",
        "auto_delete_sim_threshold": _env("DEDUP_AUTO_DELETE_SIM_THRESHOLD") or None,
    }
