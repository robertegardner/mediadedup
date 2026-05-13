"""Walk configured sources, register files in the DB, and enqueue
fingerprinting jobs. Idempotent -- safe to re-run after additions/removals.

Run as a one-shot tool:
    docker compose --profile tools run --rm scanner

or from inside a long-running container via ``app.scanner.main()`` /
``main_for_source_ids(...)``.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from redis import Redis
from rq import Queue

from .config import AUDIO_EXTS, CFG, VIDEO_EXTS
from .db import session
from .sources import (
    Source,
    bootstrap_legacy_env,
    enabled_sources_with_check,
    list_sources,
)

log = logging.getLogger("scanner")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _exts_for(media_type: str) -> set[str]:
    """Return the set of file extensions this source's media_type matches."""
    if media_type == "video":
        return VIDEO_EXTS
    if media_type == "audio":
        return AUDIO_EXTS
    # 'both' scans both extension sets.
    return VIDEO_EXTS | AUDIO_EXTS


def _classify(path: str) -> str:
    """Return 'video' or 'audio' based on extension. Caller has already
    verified the extension is in one of the sets."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    return "audio"


def walk(root: str, exts: set[str]) -> Iterator[tuple[str, os.stat_result]]:
    root_p = Path(root)
    if not root_p.exists():
        log.warning("Source root does not exist: %s", root)
        return
    for dirpath, dirnames, filenames in os.walk(root_p, followlinks=False):
        # Skip dotdirs (NFS metadata, .Trash-1000, .mediadedup-trash, etc.)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError as e:
                log.warning("stat failed %s: %s", full, e)
                continue
            if not st.st_size:
                continue
            yield full, st


def upsert_source(source: Source) -> tuple[int, int]:
    """Walk one source and upsert its files. Returns (new, updated).

    For ``media_type='both'`` sources, individual files are classified by
    extension into 'video' or 'audio' rows.
    """
    exts = _exts_for(source.media_type)
    new_count = 0
    upd_count = 0
    with session() as conn, conn.cursor() as cur:
        for full, st in walk(source.path, exts):
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            media_type = _classify(full)
            cur.execute(
                "SELECT id, size, mtime FROM files WHERE path = %s", (full,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """INSERT INTO files (path, media_type, size, mtime,
                                          status, source_id)
                       VALUES (%s, %s, %s, %s, 'pending', %s)
                       RETURNING id""",
                    (full, media_type, st.st_size, mtime, source.id),
                )
                new_count += 1
            elif row["size"] != st.st_size or row["mtime"] != mtime:
                cur.execute(
                    """UPDATE files
                          SET size=%s, mtime=%s, status='pending',
                              source_id = COALESCE(source_id, %s),
                              sha256=NULL, phashes=NULL, chromaprint=NULL,
                              error=NULL, fingerprinted_at=NULL, attempts=0
                        WHERE id=%s""",
                    (st.st_size, mtime, source.id, row["id"]),
                )
                upd_count += 1
            else:
                # Unchanged content -- still backfill source_id if missing.
                cur.execute(
                    "UPDATE files SET source_id = %s "
                    "WHERE id = %s AND source_id IS NULL",
                    (source.id, row["id"]),
                )
    return new_count, upd_count


def _source_path_looks_alive(source_path: str, sample_limit: int = 5) -> tuple[bool, str]:
    """Quick sanity check: does the source path contain visible files?

    Returns (looks_alive, reason). True means the path is mounted and lists
    at least one entry. False blocks the missing-detection pass so we don't
    incorrectly mark every file missing when an SMB mount hiccups.
    """
    if not os.path.isdir(source_path):
        return False, f"source path {source_path!r} is not a directory"
    try:
        # We only need to know there's SOMETHING under the source. Looking at
        # a few top-level entries is cheap; walking deep would be slow on a
        # network share.
        entries = []
        with os.scandir(source_path) as it:
            for entry in it:
                # Skip our own trash dir -- it's always present once we've
                # done any deletions and shouldn't count as a "live" signal
                if entry.name == ".mediadedup-trash":
                    continue
                entries.append(entry.name)
                if len(entries) >= sample_limit:
                    break
    except OSError as e:
        return False, f"scandir failed on {source_path!r}: {e}"
    if not entries:
        return False, f"{source_path!r} exists but contains no entries"
    return True, f"saw {len(entries)} entries"


# Guard: percentage of the source we're willing to mark missing in one pass.
# Higher than this and we abort with a noisy log entry. Tuned so a bulk
# delete via the dashboard (always fewer than ~50% of files at once) still
# works, but a stale-mount mishap (which flips 100%) does not.
_MASS_MISSING_PCT_THRESHOLD = 0.10

# Guard: minimum seconds since process start before mark_missing will run.
# Lets the autofs/CIFS mount stabilize after a container restart.
_MARK_MISSING_STARTUP_GRACE_SECS = 60.0
_PROCESS_START = time.monotonic()


def mark_missing_for_source(source: Source) -> int:
    """Flag files under this source that no longer exist on disk.

    Three defensive guards prevent the historical "mass-missing" bug where
    a stale or not-yet-ready mount caused every file in the source to be
    marked missing in a single scanner pass:

    1. STARTUP GRACE: refuse to run within the first 60s of container life,
       giving autofs/CIFS time to mount.
    2. PATH-ALIVE CHECK: refuse to run if the source's top-level directory
       lists no entries (mount almost certainly broken).
    3. PERCENTAGE CAP: refuse to mark more than 10% of the source's files
       missing in one pass. Log loudly and bail. Real deletions of >10% of
       a library don't happen by accident; if you want that, manually
       UPDATE the rows.
    """
    secs_since_start = time.monotonic() - _PROCESS_START
    if secs_since_start < _MARK_MISSING_STARTUP_GRACE_SECS:
        log.info("Skipping mark_missing for source %r: only %.1fs since "
                 "start (need %.0f) -- letting mounts settle",
                 source.name, secs_since_start,
                 _MARK_MISSING_STARTUP_GRACE_SECS)
        return 0

    alive, reason = _source_path_looks_alive(source.path)
    if not alive:
        log.warning("REFUSING mark_missing for source %r: %s. "
                    "This protects against stale-mount mass-missing events.",
                    source.name, reason)
        return 0

    # Two-phase: first collect candidates that would be marked missing,
    # then apply the percentage cap, then commit. This means a flaky mount
    # that flips status mid-walk can't half-corrupt the DB.
    candidates_to_mark: list[int] = []
    total_files_in_source = 0
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM files WHERE source_id = %s",
            (source.id,),
        )
        total_files_in_source = int((cur.fetchone() or {}).get("n") or 0)

        cur.execute(
            """SELECT id, path FROM files
                WHERE source_id = %s
                  AND status NOT IN ('missing','deleted')""",
            (source.id,),
        )
        for r in cur.fetchall():
            try:
                exists = os.path.exists(r["path"])
            except OSError as e:
                # An OSError mid-walk strongly suggests the mount just went
                # away. Abort the whole pass rather than chase nondeterminism.
                log.warning("mark_missing aborted for source %r: stat error "
                            "on %r: %s", source.name, r["path"], e)
                return 0
            if not exists:
                candidates_to_mark.append(r["id"])

    if not candidates_to_mark:
        return 0

    # Percentage cap. ``total_files_in_source`` covers ALL statuses; a
    # source where most files are already 'missing' or 'deleted' shouldn't
    # itself protect against flipping the remainder, so we also compare to
    # the number of currently-non-missing files (a cleaner denominator).
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS n FROM files
                WHERE source_id = %s
                  AND status NOT IN ('missing','deleted')""",
            (source.id,),
        )
        live_files = int((cur.fetchone() or {}).get("n") or 0)

    if live_files == 0:
        return 0

    pct_to_mark = len(candidates_to_mark) / live_files
    if pct_to_mark > _MASS_MISSING_PCT_THRESHOLD:
        log.warning(
            "REFUSING mark_missing for source %r: would flip %d/%d "
            "(%.1f%%) of live files to 'missing', exceeds %.0f%% safety cap. "
            "Likely a stale mount, not a real disk deletion. If you really "
            "want this, run the UPDATE manually.",
            source.name, len(candidates_to_mark), live_files,
            pct_to_mark * 100, _MASS_MISSING_PCT_THRESHOLD * 100,
        )
        return 0

    # All guards passed. Commit the marks.
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE files SET status='missing' WHERE id = ANY(%s)",
            (candidates_to_mark,),
        )
    log.info("mark_missing for source %r: %d files flagged",
             source.name, len(candidates_to_mark))
    return len(candidates_to_mark)


def enqueue_pending() -> int:
    """Push every pending file's id onto the RQ work queue."""
    redis = Redis.from_url(CFG.redis_url)
    q = Queue("dedup", connection=redis)
    enqueued = 0
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM files WHERE status = 'pending' ORDER BY size DESC"
        )
        for row in cur.fetchall():
            q.enqueue(
                "app.worker.process_file", row["id"],
                job_id=f"file-{row['id']}",
                job_timeout="30m",
                result_ttl=3600, failure_ttl=86400,
            )
            enqueued += 1
    return enqueued


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def scan_sources(sources: Iterable[Source]) -> dict:
    """Walk the provided sources and return a per-source stats dict.

    Does NOT enqueue jobs; caller decides (the wrapper ``main`` enqueues
    after walking, the orchestrator may want to defer this).
    """
    stats: dict[str, dict] = {}
    for s in sources:
        log.info("Scanning source %r at %s (media_type=%s)",
                 s.name, s.path, s.media_type)
        new, upd = upsert_source(s)
        missing = mark_missing_for_source(s)
        stats[s.name] = {"new": new, "updated": upd, "missing": missing,
                         "path": s.path}
        log.info("Source %r: %d new, %d updated, %d missing",
                 s.name, new, upd, missing)
    return stats


def main_for_source_ids(source_ids: list[int] | None = None) -> dict:
    """Scan a specific list of sources by id, or all enabled sources if None.

    Returns ``{"sources": <per-source-stats>, "enqueued": <int>}``.
    """
    bootstrap_legacy_env()

    if source_ids:
        all_srcs = {s.id: s for s in list_sources(enabled_only=False)}
        sources = []
        for sid in source_ids:
            s = all_srcs.get(sid)
            if s is None:
                log.warning("scan: unknown source id %s; skipping", sid)
                continue
            if not os.path.isdir(s.path):
                log.warning("scan: source %r path missing: %s; skipping",
                            s.name, s.path)
                continue
            sources.append(s)
    else:
        sources = list(enabled_sources_with_check())

    if not sources:
        log.warning("No sources to scan. Configure one via the web UI or "
                    "set VIDEO_ROOT/MUSIC_ROOT for legacy bootstrap.")
        return {"sources": {}, "enqueued": 0}

    per_source = scan_sources(sources)
    n = enqueue_pending()
    log.info("Enqueued %d files for fingerprinting", n)
    return {"sources": per_source, "enqueued": n}


def main() -> None:
    main_for_source_ids(None)


if __name__ == "__main__":
    main()
