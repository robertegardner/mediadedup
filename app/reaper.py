"""Background reaper for stuck rows.

Periodically resets any file row that's been ``status='processing'`` for
longer than ``REAPER_STUCK_THRESHOLD_SECS`` back to ``pending``, then
enqueues a fresh job for each so workers pick them up automatically. This
is a self-healing mechanism for the case where a worker dies mid-job
(NFS/SMB hangs, OOM kill, ungraceful container shutdown) and leaves a DB
row that nothing will ever reclaim.

Threading model: runs in a single daemon thread inside the web container
process. There's only one web container, so we don't need cross-instance
locking.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from redis import Redis
from rq import Queue

from .config import CFG
from .db import session

log = logging.getLogger("reaper")


REAPER_STUCK_THRESHOLD_SECS = int(os.environ.get("REAPER_STUCK_THRESHOLD_SECS", 1800))
REAPER_INTERVAL_SECS = int(os.environ.get("REAPER_INTERVAL_SECS", 120))


def _reap_once() -> int:
    """Reset stuck rows and enqueue them.

    Returns the number of rows reaped.
    """
    redis = Redis.from_url(CFG.redis_url)
    queue = Queue("dedup", connection=redis)

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE files
                  SET status='pending',
                      error = CASE
                          WHEN error IS NULL OR error = ''
                               THEN 'reaped after stuck processing'
                          ELSE error || E'\\nreaped after stuck processing'
                      END,
                      processing_started_at = NULL
                WHERE status = 'processing'
                  AND processing_started_at IS NOT NULL
                  AND processing_started_at < NOW() - (%s || ' seconds')::interval
            RETURNING id""",
            (REAPER_STUCK_THRESHOLD_SECS,),
        )
        ids = [r["id"] for r in cur.fetchall()]

    for fid in ids:
        try:
            queue.enqueue(
                "app.worker.process_file",
                fid,
                job_id=f"file-{fid}-reap-{int(time.time())}",
                job_timeout="30m",
                result_ttl=3600,
                failure_ttl=86400,
            )
        except Exception:                                            # noqa: BLE001
            log.exception("failed to re-enqueue file id=%s", fid)
    return len(ids)


def _loop() -> None:
    log.info("reaper started: threshold=%ds, interval=%ds",
             REAPER_STUCK_THRESHOLD_SECS, REAPER_INTERVAL_SECS)
    while True:
        try:
            n = _reap_once()
            if n:
                log.warning("Reaped and re-enqueued %d stuck row(s).", n)
        except Exception:                                            # noqa: BLE001
            log.exception("reaper tick failed")
        time.sleep(REAPER_INTERVAL_SECS)


def start_in_background() -> threading.Thread:
    t = threading.Thread(target=_loop, name="reaper", daemon=True)
    t.start()
    return t
