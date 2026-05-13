"""FastAPI web UI for reviewing duplicate groups and queueing deletions.

Routes
------
GET  /                           - dashboard
GET  /groups?type=&match=        - list groups, filtered
GET  /groups/{id}                - show one group with side-by-side detail
POST /groups/{id}/mark           - set keeper / actions for members
POST /groups/{id}/execute        - move marked-for-delete files to trash
POST /groups/{id}/review         - mark group as reviewed
GET  /thumbs/{file_id}.jpg       - serve a video thumbnail
GET  /jobs                       - rq queue stats
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis import Redis
from rq import Queue
from rq.registry import FailedJobRegistry, StartedJobRegistry

from .config import CFG
from .db import ensure_schema, session
from . import actions as actions_mod

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Media Dedup")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Static directory is optional. Mount only if it exists so the app starts in
# installs that don't ship one. No current template references /static/.
_static_dir = BASE_DIR / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.on_event("startup")
def _migrate() -> None:
    try:
        ensure_schema()
    except Exception:
        log.exception("ensure_schema failed at startup")
    # Run-history table for scanner/matcher orchestration.
    try:
        from . import orchestrator as _orch
        _orch.ensure_schema()
    except Exception:
        log.exception("orchestrator schema setup failed")
    # Settings table for runtime-tunable config (LLM endpoint, etc.)
    try:
        from . import settings as _settings
        _settings.ensure_schema()
    except Exception:
        log.exception("settings schema setup failed")
    # LLM match log table
    try:
        from . import llm_match as _llm
        _llm.ensure_schema()
    except Exception:
        log.exception("llm_match schema setup failed")
    # Start the background reaper -- self-heals stuck 'processing' rows.
    try:
        from . import reaper
        reaper.start_in_background()
    except Exception:
        log.exception("reaper failed to start")
    # Start the periodic scanner+matcher scheduler.
    try:
        from . import scheduler as _sched
        _sched.start()
    except Exception:
        log.exception("scheduler failed to start")


@app.on_event("shutdown")
def _shutdown() -> None:
    try:
        from . import scheduler as _sched
        _sched.shutdown()
    except Exception:
        log.exception("scheduler shutdown failed")

# Trash root inside one of the mounts -- using the video mount avoids a
# cross-device move when the videos are huge. Audio trash piggybacks on the
# music mount.
# Trash directory paths now live in app/actions.py (shared with the bulk CLI).


def _humanize_bytes(b: int | None) -> str:
    if b is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(b)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{b}"


def _humanize_duration(s: float | None) -> str:
    if not s:
        return "—"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


templates.env.filters["bytes"] = _humanize_bytes
templates.env.filters["duration"] = _humanize_duration


def _redis() -> Redis:
    return Redis.from_url(CFG.redis_url)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT media_type, status, COUNT(*) AS n, COALESCE(SUM(size),0) AS total_size
                 FROM files GROUP BY media_type, status"""
        )
        file_stats = cur.fetchall()

        cur.execute(
            """SELECT g.media_type, g.match_type,
                      COUNT(DISTINCT g.id) AS groups,
                      COUNT(m.file_id) AS members
                 FROM dup_groups g LEFT JOIN dup_members m ON m.group_id = g.id
                GROUP BY g.media_type, g.match_type"""
        )
        group_stats = cur.fetchall()

        # Wasted space = sum(member sizes) - keeper size, per group.
        cur.execute(
            """WITH gs AS (
                  SELECT g.id, g.media_type,
                         SUM(f.size) AS total,
                         MAX(f.size) FILTER (WHERE m.is_keeper) AS keeper
                    FROM dup_groups g
                    JOIN dup_members m ON m.group_id = g.id
                    JOIN files f ON f.id = m.file_id
                   GROUP BY g.id, g.media_type)
               SELECT media_type,
                      COALESCE(SUM(total - COALESCE(keeper,0)),0) AS wasted
                 FROM gs GROUP BY media_type"""
        )
        wasted = {r["media_type"]: r["wasted"] for r in cur.fetchall()}

    redis = _redis()
    queue = Queue("dedup", connection=redis)
    started = StartedJobRegistry("dedup", connection=redis)
    failed = FailedJobRegistry("dedup", connection=redis)
    job_stats = {
        "queued": queue.count,
        "started": started.count,
        "failed": failed.count,
    }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "file_stats": file_stats,
            "group_stats": group_stats,
            "wasted": wasted,
            "job_stats": job_stats,
        },
    )


# ---------------------------------------------------------------------------
# Group list
# ---------------------------------------------------------------------------

@app.get("/groups", response_class=HTMLResponse)
def list_groups(
    request: Request,
    type: str | None = None,
    match: str | None = None,
    reviewed: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> HTMLResponse:
    where = ["1=1"]
    params: list = []
    if type in ("video", "audio"):
        where.append("g.media_type = %s")
        params.append(type)
    if match in ("exact", "perceptual", "chromaprint"):
        where.append("g.match_type = %s")
        params.append(match)
    if reviewed == "yes":
        where.append("g.reviewed = TRUE")
    elif reviewed == "no":
        where.append("g.reviewed = FALSE")
    where_sql = " AND ".join(where)

    page = max(1, page)
    per_page = max(1, min(200, per_page))
    offset = (page - 1) * per_page

    with session() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM dup_groups g WHERE {where_sql}", params)
        total = cur.fetchone()["n"]

        cur.execute(
            f"""SELECT g.id, g.media_type, g.match_type, g.similarity, g.reviewed,
                       COUNT(m.file_id) AS n_members,
                       SUM(f.size) AS total_size,
                       MAX(f.size) FILTER (WHERE m.is_keeper) AS keeper_size
                  FROM dup_groups g
                  JOIN dup_members m ON m.group_id = g.id
                  JOIN files f ON f.id = m.file_id
                 WHERE {where_sql}
                 GROUP BY g.id
                 ORDER BY (SUM(f.size) - COALESCE(MAX(f.size) FILTER (WHERE m.is_keeper),0)) DESC
                 LIMIT %s OFFSET %s""",
            [*params, per_page, offset],
        )
        groups = cur.fetchall()

    return templates.TemplateResponse(
        "groups.html",
        {
            "request": request,
            "groups": groups,
            "total": total,
            "page": page,
            "per_page": per_page,
            "filters": {"type": type, "match": match, "reviewed": reviewed},
        },
    )


# ---------------------------------------------------------------------------
# Group detail
# ---------------------------------------------------------------------------

@app.get("/groups/{group_id}", response_class=HTMLResponse)
def group_detail(request: Request, group_id: int) -> HTMLResponse:
    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM dup_groups WHERE id = %s", (group_id,))
        group = cur.fetchone()
        if not group:
            raise HTTPException(404)

        cur.execute(
            """SELECT f.*, m.is_keeper, m.action
                 FROM files f
                 JOIN dup_members m ON m.file_id = f.id
                WHERE m.group_id = %s
                ORDER BY m.is_keeper DESC, f.size DESC""",
            (group_id,),
        )
        members = cur.fetchall()

    return templates.TemplateResponse(
        "group.html",
        {"request": request, "group": group, "members": members},
    )


@app.post("/groups/{group_id}/mark")
def mark_members(
    group_id: int,
    keeper_id: int = Form(...),
    actions: list[str] = Form(default=[]),
) -> RedirectResponse:
    """Persist keeper choice and per-member action.

    ``actions`` arrives as a list of "<file_id>:<action>" strings.
    """
    parsed: dict[int, str] = {}
    for token in actions:
        try:
            fid, action = token.split(":", 1)
            if action in ("keep", "delete", "ignore"):
                parsed[int(fid)] = action
        except ValueError:
            continue

    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT file_id FROM dup_members WHERE group_id = %s", (group_id,))
        member_ids = [r["file_id"] for r in cur.fetchall()]
        if keeper_id not in member_ids:
            raise HTTPException(400, "keeper not in group")

        cur.execute(
            "UPDATE dup_members SET is_keeper = (file_id = %s) WHERE group_id = %s",
            (keeper_id, group_id),
        )
        for fid in member_ids:
            action = parsed.get(fid, "keep" if fid == keeper_id else "delete")
            cur.execute(
                "UPDATE dup_members SET action = %s WHERE group_id = %s AND file_id = %s",
                (action, group_id, fid),
            )

    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@app.post("/groups/{group_id}/execute")
def execute_group(group_id: int) -> RedirectResponse:
    """Move every member marked ``delete`` into the trash directory.

    We never call ``unlink`` directly. Files land under
    ``<mount>/.mediadedup-trash/<YYYYMMDD>/<file_id>__<basename>`` so they can
    be restored or purged by the operator with `rm -rf`.
    """
    actions_mod.execute_group(group_id)
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


# --- Bulk auto-delete for exact (sha256) duplicate groups -------------------

@app.get("/api/exact_preview")
def exact_preview(media_type: str | None = None) -> dict:
    """How many exact-match dupes would auto-deletion remove? Per media_type."""
    return actions_mod.preview_exact_groups(media_type)


@app.get("/api/similarity_preview")
def similarity_preview(
    threshold: float = 0.95,
    media_type: str | None = None,
    match_types: str | None = None,
) -> dict:
    """How many similarity-matched groups (perceptual + chromaprint) at or
    above ``threshold`` would auto-deletion remove?

    ``match_types`` is an optional comma-separated subset of
    'perceptual,chromaprint'. Defaults to both.
    """
    mts = (
        [m.strip() for m in match_types.split(",") if m.strip()]
        if match_types else ["perceptual", "chromaprint"]
    )
    rows = actions_mod.preview_groups(
        match_types=mts,
        media_type=media_type if media_type in ("video", "audio") else None,
        min_similarity=threshold,
    )
    # JSON keys can't be tuples; flatten to "media:match" → row.
    return {f"{mt}:{match}": row for (mt, match), row in rows.items()}


@app.post("/exact/auto_delete")
def auto_delete_exact(media_type: str | None = Form(default=None)) -> RedirectResponse:
    """Mark + execute every unreviewed exact group in one shot.

    Confirmation is enforced in the UI (a JS confirm() before the POST).
    Filtering by media_type ('video' / 'audio') keeps it scoped if the
    operator wants to limit blast radius.
    """
    mt = media_type if media_type in ("video", "audio") else None
    actions_mod.auto_mark_exact(mt)
    summary = actions_mod.execute_exact_groups(mt)
    log.info("auto_delete_exact: %s", summary.as_dict())
    return RedirectResponse("/", status_code=303)


@app.post("/similarity/auto_delete")
def auto_delete_similarity(
    threshold: float = Form(...),
    media_type: str | None = Form(default=None),
    match_types: str | None = Form(default=None),
) -> RedirectResponse:
    """Mark + execute every unreviewed similarity-match group at or above
    ``threshold``.

    Refuses thresholds below 0.85 to prevent foot-shooting -- if you really
    need that low, run the CLI which has no floor.
    """
    if threshold < 0.85 or threshold > 1.0:
        raise HTTPException(
            400,
            f"threshold must be between 0.85 and 1.0 (got {threshold}). "
            f"For looser matches, use the CLI.",
        )

    mts = (
        [m.strip() for m in match_types.split(",") if m.strip()]
        if match_types else ["perceptual", "chromaprint"]
    )
    mt = media_type if media_type in ("video", "audio") else None

    n = actions_mod.auto_mark_groups(
        match_types=mts, media_type=mt, min_similarity=threshold,
    )
    log.info("auto_delete_similarity: marked %d groups (threshold=%.3f, "
             "media=%s, match_types=%s)", n, threshold, mt, mts)
    summary = actions_mod.execute_groups(
        match_types=mts, media_type=mt, min_similarity=threshold,
    )
    log.info("auto_delete_similarity: %s", summary.as_dict())
    return RedirectResponse("/", status_code=303)


@app.post("/groups/{group_id}/review")
def mark_reviewed(group_id: int) -> RedirectResponse:
    with session() as conn, conn.cursor() as cur:
        cur.execute("UPDATE dup_groups SET reviewed = TRUE WHERE id = %s", (group_id,))
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


# ---------------------------------------------------------------------------
# Static thumbnails (served from the shared volume)
# ---------------------------------------------------------------------------

@app.get("/thumbs/{file_id}.jpg")
def thumb(file_id: int) -> FileResponse:
    shard = f"{file_id % 100:02d}"
    p = Path(CFG.thumbs_dir) / shard / f"{file_id}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Job stats (lightweight JSON)
# ---------------------------------------------------------------------------

@app.get("/jobs")
def jobs() -> dict:
    redis = _redis()
    queue = Queue("dedup", connection=redis)
    failed = FailedJobRegistry("dedup", connection=redis)
    started = StartedJobRegistry("dedup", connection=redis)
    return {
        "queued": queue.count,
        "started": started.count,
        "failed": failed.count,
    }


# ---------------------------------------------------------------------------
# Live activity (polled by the dashboard)
# ---------------------------------------------------------------------------

@app.get("/api/activity")
def activity() -> dict:
    """Snapshot of what the system is doing right now.

    Polled every few seconds by the dashboard. Cheap queries only -- no
    heavy joins. The biggest cost is the per-status COUNT.
    """
    now = datetime.now(timezone.utc)
    out: dict = {"now": now.isoformat()}

    with session() as conn, conn.cursor() as cur:
        # Per-(media_type, status) counts -- drives both the in-flight tile
        # and the long-running progress bars.
        cur.execute(
            """SELECT media_type, status, COUNT(*) AS n
                 FROM files GROUP BY 1,2"""
        )
        by_status: dict[str, dict[str, int]] = {
            "video": {"pending": 0, "processing": 0, "done": 0,
                      "failed": 0, "missing": 0, "deleted": 0},
            "audio": {"pending": 0, "processing": 0, "done": 0,
                      "failed": 0, "missing": 0, "deleted": 0},
        }
        for r in cur.fetchall():
            by_status.setdefault(r["media_type"], {})[r["status"]] = r["n"]
        out["by_status"] = by_status

        # Currently in flight -- one row per file. processing_started_at lets
        # us show how long each has been running, which is the single most
        # informative thing in the panel.
        cur.execute(
            """SELECT id, path, media_type, size, processing_started_at, attempts
                 FROM files
                WHERE status = 'processing'
                ORDER BY processing_started_at NULLS LAST, id"""
        )
        in_flight = []
        for r in cur.fetchall():
            started_at = r["processing_started_at"]
            elapsed_s = (now - started_at).total_seconds() if started_at else None
            in_flight.append({
                "id": r["id"],
                "path": r["path"],
                "name": r["path"].rsplit("/", 1)[-1],
                "media_type": r["media_type"],
                "size": int(r["size"] or 0),
                "elapsed_s": int(elapsed_s) if elapsed_s is not None else None,
                "attempts": r["attempts"],
            })
        out["in_flight"] = in_flight

        # Last 10 done -- shows the worker is making progress and roughly
        # how fast.
        cur.execute(
            """SELECT id, path, media_type, size,
                      processing_started_at, fingerprinted_at
                 FROM files
                WHERE status = 'done' AND fingerprinted_at IS NOT NULL
                ORDER BY fingerprinted_at DESC
                LIMIT 10"""
        )
        recent_done = []
        for r in cur.fetchall():
            took = None
            if r["processing_started_at"] and r["fingerprinted_at"]:
                took = (r["fingerprinted_at"] - r["processing_started_at"]).total_seconds()
            recent_done.append({
                "id": r["id"],
                "name": r["path"].rsplit("/", 1)[-1],
                "media_type": r["media_type"],
                "size": int(r["size"] or 0),
                "took_s": round(took, 1) if took is not None else None,
                "completed_at": r["fingerprinted_at"].isoformat()
                                if r["fingerprinted_at"] else None,
            })
        out["recent_done"] = recent_done

        # Last 5 failures -- one short error line each.
        cur.execute(
            """SELECT id, path, media_type, error
                 FROM files
                WHERE status = 'failed'
                ORDER BY id DESC
                LIMIT 5"""
        )
        recent_failed = []
        for r in cur.fetchall():
            err = (r["error"] or "").splitlines()[0][:200]
            recent_failed.append({
                "id": r["id"],
                "name": r["path"].rsplit("/", 1)[-1],
                "media_type": r["media_type"],
                "error": err,
            })
        out["recent_failed"] = recent_failed

        # Throughput over the last 5 minutes.
        cur.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes
                 FROM files
                WHERE status = 'done'
                  AND fingerprinted_at >= NOW() - INTERVAL '5 minutes'"""
        )
        tp = cur.fetchone()
        n5 = int(tp["n"] or 0)
        b5 = int(tp["bytes"] or 0)
        out["throughput"] = {
            "files_5min": n5,
            "bytes_5min": b5,
            "files_per_min": round(n5 / 5.0, 1),
            "mb_per_sec": round(b5 / 1024.0 / 1024.0 / 300.0, 1),
        }

    redis = _redis()
    queue = Queue("dedup", connection=redis)
    started = StartedJobRegistry("dedup", connection=redis)
    failed = FailedJobRegistry("dedup", connection=redis)
    out["queue"] = {
        "queued": queue.count,
        "started": started.count,
        "failed": failed.count,
    }

    return out


# ---------------------------------------------------------------------------
# Orchestrator: manual runs, status, schedule
# ---------------------------------------------------------------------------

@app.post("/api/run/scanner")
def trigger_scanner() -> dict:
    """Trigger a scanner run in a background thread; return immediately.

    Returns 200 + JSON. UI polls /api/run/status to track progress.
    """
    import threading
    from . import orchestrator
    t = threading.Thread(
        target=orchestrator.run_scanner, args=("manual",),
        name="manual-scanner", daemon=True,
    )
    t.start()
    return {"ok": True, "kind": "scanner", "started": True}


@app.post("/api/run/matcher")
def trigger_matcher() -> dict:
    """Trigger a matcher run in a background thread; return immediately."""
    import threading
    from . import orchestrator
    t = threading.Thread(
        target=orchestrator.run_matcher, args=("manual",),
        name="manual-matcher", daemon=True,
    )
    t.start()
    return {"ok": True, "kind": "matcher", "started": True}


@app.post("/api/run/filename_matcher")
def trigger_filename_matcher() -> dict:
    """Run just the filename matcher. Fast -- no GPU/IO heavy work."""
    import threading
    from . import orchestrator
    t = threading.Thread(
        target=orchestrator.run_filename_matcher, args=("manual",),
        name="manual-filename-matcher", daemon=True,
    )
    t.start()
    return {"ok": True, "kind": "filename_matcher", "started": True}


@app.post("/api/run/scan_match")
def trigger_scan_match() -> dict:
    """Trigger a scanner + matcher pair (same as the scheduler does)."""
    import threading
    from . import orchestrator
    t = threading.Thread(
        target=orchestrator.run_scanner_then_matcher, args=("manual",),
        name="manual-scan-match", daemon=True,
    )
    t.start()
    return {"ok": True, "kind": "scan_match", "started": True}


@app.get("/api/run/status")
def run_status() -> dict:
    """Current run (if any) + recent history + schedule info."""
    from . import orchestrator, scheduler as _sched
    return {
        "current": orchestrator.current_run(),
        "recent": orchestrator.recent_runs(limit=10),
        "schedule": _sched.schedule_info(),
    }


# ---------------------------------------------------------------------------
# Sources: configurable scan roots
# ---------------------------------------------------------------------------

@app.get("/api/sources")
def api_sources() -> dict:
    """List all configured sources with their disk-existence status.

    Returns shape suitable for the dashboard sources card.
    """
    from . import sources as sources_mod
    out = []
    for s in sources_mod.list_sources(enabled_only=False):
        path_exists = os.path.isdir(s.path)
        # File-count summary scoped to this source.
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT status, COUNT(*) AS n,
                          COALESCE(SUM(size),0) AS bytes
                     FROM files
                    WHERE source_id = %s
                    GROUP BY status""",
                (s.id,),
            )
            counts = {r["status"]: {"n": r["n"], "bytes": int(r["bytes"])}
                      for r in cur.fetchall()}
        out.append({
            "id": s.id, "name": s.name, "path": s.path,
            "media_type": s.media_type, "priority": s.priority,
            "enabled": s.enabled, "notes": s.notes,
            "path_exists": path_exists,
            "file_counts": counts,
        })
    return {"sources": out}


@app.post("/api/sources")
def api_create_source(
    name: str = Form(...),
    path: str = Form(...),
    media_type: str = Form(...),
    priority: int = Form(default=0),
    enabled: bool = Form(default=True),
    notes: str | None = Form(default=None),
) -> dict:
    from . import sources as sources_mod
    try:
        s = sources_mod.create_source(
            name=name.strip(), path=path.strip(),
            media_type=media_type, priority=priority,
            enabled=enabled, notes=notes,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    # Backfill on creation so file rows under this path light up immediately.
    try:
        sources_mod.backfill_source_ids(only_null=True)
    except Exception:
        log.exception("backfill after create failed")
    return {"ok": True, "source": {
        "id": s.id, "name": s.name, "path": s.path,
        "media_type": s.media_type, "priority": s.priority,
        "enabled": s.enabled,
    }}


@app.post("/api/sources/{source_id}/update")
def api_update_source(
    source_id: int,
    name: str | None = Form(default=None),
    path: str | None = Form(default=None),
    media_type: str | None = Form(default=None),
    priority: int | None = Form(default=None),
    enabled: bool | None = Form(default=None),
    notes: str | None = Form(default=None),
) -> dict:
    from . import sources as sources_mod
    try:
        s = sources_mod.update_source(
            source_id, name=name, path=path, media_type=media_type,
            priority=priority, enabled=enabled, notes=notes,
        )
    except Exception as e:
        raise HTTPException(400, str(e))
    if s is None:
        raise HTTPException(404, "source not found")
    return {"ok": True}


@app.post("/api/sources/{source_id}/delete")
def api_delete_source(source_id: int) -> dict:
    from . import sources as sources_mod
    if not sources_mod.delete_source(source_id):
        raise HTTPException(404, "source not found")
    return {"ok": True}


@app.post("/api/sources/backfill")
def api_backfill_sources(only_null: bool = Form(default=True)) -> dict:
    """Re-resolve source_id for existing file rows (longest-prefix match).

    Pass only_null=false to re-resolve even rows that already have a source.
    Useful after renaming or shuffling source paths.
    """
    from . import sources as sources_mod
    n = sources_mod.backfill_source_ids(only_null=only_null)
    return {"ok": True, "rows_updated": n}


# ---------------------------------------------------------------------------
# Per-source scanning
# ---------------------------------------------------------------------------

@app.post("/api/run/scanner_for")
def trigger_scanner_for(source_ids: str = Form(...)) -> dict:
    """Run scanner against a comma-separated list of source ids.

    Example: source_ids=3,7,9. Pass 'all' (or empty) to scan every enabled
    source -- same behavior as /api/run/scanner.
    """
    import threading
    from . import orchestrator

    ids: list[int] | None
    raw = source_ids.strip()
    if not raw or raw.lower() == "all":
        ids = None
    else:
        try:
            ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "source_ids must be comma-separated ints or 'all'")

    def _run():
        orchestrator.run_scanner_for(ids, triggered_by="manual")

    t = threading.Thread(target=_run, name="manual-scanner-for", daemon=True)
    t.start()
    return {"ok": True, "kind": "scanner", "source_ids": ids, "started": True}


# ---------------------------------------------------------------------------
# Filename-match groups
# ---------------------------------------------------------------------------
# Filename groups carry a ``notes`` column on dup_groups that says either
# 'resolution_only' (every member is the same content at different resolution)
# or 'name_differs' (titles differ in ways the normalizer treats as content).
# The dashboard surfaces these separately so the resolution-only ones can be
# auto-deleted aggressively while name_differs cases are reviewed.

@app.get("/api/filename_preview")
def filename_preview() -> dict:
    """Return counts of filename groups split by safety category."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT g.notes AS bucket,
                      COUNT(DISTINCT g.id) AS groups,
                      SUM(per_group.dups) AS files_to_delete,
                      SUM(per_group.bytes_to_free) AS bytes_to_free
                 FROM dup_groups g
                 JOIN LATERAL (
                       SELECT COUNT(*) - 1 AS dups,
                              SUM(f.size) - MAX(f.size) AS bytes_to_free
                         FROM dup_members m JOIN files f ON f.id = m.file_id
                        WHERE m.group_id = g.id
                 ) per_group ON TRUE
                WHERE g.match_type = 'filename'
                  AND g.reviewed = FALSE
                GROUP BY g.notes"""
        )
        return {
            (r["bucket"] or "unknown"): {
                "groups": int(r["groups"] or 0),
                "files_to_delete": int(r["files_to_delete"] or 0),
                "bytes_to_free": int(r["bytes_to_free"] or 0),
            }
            for r in cur.fetchall()
        }


@app.post("/filename/auto_delete")
def auto_delete_filename(
    bucket: str = Form(...),  # 'resolution_only' or 'name_differs' or 'all'
) -> RedirectResponse:
    """Auto-delete filename-match groups in the chosen bucket.

    Strongly recommend bucket='resolution_only' for unattended use. Pick
    'name_differs' only if you've spot-checked the groups and trust the
    normalizer's clustering on your library.
    """
    from . import actions as _actions

    if bucket not in ("resolution_only", "name_differs", "all"):
        raise HTTPException(400, "bucket must be resolution_only / name_differs / all")

    # Collect target group ids based on bucket
    with session() as conn, conn.cursor() as cur:
        if bucket == "all":
            cur.execute(
                "SELECT id FROM dup_groups "
                "WHERE match_type='filename' AND reviewed=FALSE"
            )
        else:
            cur.execute(
                "SELECT id FROM dup_groups "
                "WHERE match_type='filename' AND reviewed=FALSE AND notes=%s",
                (bucket,),
            )
        gids = [r["id"] for r in cur.fetchall()]

    # The filename matcher already wrote keeper/action per member when it
    # built the groups, so we don't need to re-mark -- just execute.
    summary = _actions.DeletionSummary()
    for gid in gids:
        try:
            _actions.execute_group(gid, summary)
        except Exception as e:  # noqa: BLE001
            log.exception("filename execute_group(%s) failed", gid)
            summary.errors.append(f"group {gid}: {e}")
            summary.files_failed += 1
    log.info("auto_delete_filename bucket=%s: %s", bucket, summary.as_dict())
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Settings (key/value, used by LLM matcher and future config)
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def api_settings_get(prefix: str = "") -> dict:
    """Return all settings, or those matching ``prefix``. Includes defaults
    overlaid where no value is set so the UI shows sensible defaults."""
    from . import settings as settings_mod
    from . import llm_match as llm_mod

    settings_mod.ensure_schema()
    stored = settings_mod.get_many(prefix or "")
    out: dict[str, str] = {}
    # Overlay LLM defaults so the UI sees them even on first run
    for k, v in llm_mod.DEFAULT_SETTINGS.items():
        if not prefix or k.startswith(prefix):
            out[k] = stored.get(k, v)
    # Include any non-LLM keys the user set
    for k, v in stored.items():
        if k not in out:
            out[k] = v
    return {"settings": out}


@app.post("/api/settings")
async def api_settings_set(request: Request) -> dict:
    """Bulk-update settings. Body is form-encoded key=value pairs."""
    from . import settings as settings_mod
    form = await request.form()
    items: dict[str, str] = {}
    for key in form.keys():
        items[key] = str(form[key])
    if not items:
        raise HTTPException(400, "no settings provided")
    settings_mod.ensure_schema()
    settings_mod.set_many(items)
    log.info("Settings updated: %s", list(items.keys()))
    return {"ok": True, "updated": list(items.keys())}


# ---------------------------------------------------------------------------
# LLM matcher
# ---------------------------------------------------------------------------

@app.post("/api/run/llm_matcher")
def trigger_llm_matcher() -> dict:
    """Trigger an LLM matcher run in a background thread."""
    import threading
    from . import orchestrator
    t = threading.Thread(
        target=orchestrator.run_llm_matcher, args=("manual",),
        name="manual-llm-matcher", daemon=True,
    )
    t.start()
    return {"ok": True, "kind": "llm_matcher", "started": True}


@app.get("/api/llm/test")
def api_llm_test(url: str | None = None) -> dict:
    """Probe Ollama. If ``url`` is provided, probe THAT URL (used by the
    settings UI to test a URL the user is typing before they save it).
    Otherwise probe the saved setting."""
    from . import llm_match
    return llm_match.check_ollama(override_url=url)


@app.get("/api/llm_preview")
def api_llm_preview() -> dict:
    """Counts of unreviewed LLM-match groups, for the dashboard card."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(DISTINCT g.id) AS groups,
                      SUM(per_group.dups) AS files_to_delete,
                      SUM(per_group.bytes_to_free) AS bytes_to_free,
                      AVG(g.similarity) AS avg_confidence
                 FROM dup_groups g
                 JOIN LATERAL (
                       SELECT COUNT(*) - 1 AS dups,
                              SUM(f.size) - MAX(f.size) AS bytes_to_free
                         FROM dup_members m JOIN files f ON f.id = m.file_id
                        WHERE m.group_id = g.id
                 ) per_group ON TRUE
                WHERE g.match_type = 'llm'
                  AND g.reviewed = FALSE"""
        )
        r = cur.fetchone() or {}
        return {
            "groups": int(r.get("groups") or 0),
            "files_to_delete": int(r.get("files_to_delete") or 0),
            "bytes_to_free": int(r.get("bytes_to_free") or 0),
            "avg_confidence": float(r.get("avg_confidence") or 0.0),
        }


@app.post("/llm/auto_delete")
def auto_delete_llm(
    min_confidence: float = Form(default=0.85),
) -> RedirectResponse:
    """Auto-delete unreviewed LLM-match groups whose confidence is at or
    above the given threshold. Conservative default (0.85)."""
    from . import actions as _actions

    if min_confidence < 0 or min_confidence > 1:
        raise HTTPException(400, "min_confidence must be in [0, 1]")

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM dup_groups
                WHERE match_type = 'llm'
                  AND reviewed = FALSE
                  AND similarity >= %s""",
            (min_confidence,),
        )
        gids = [r["id"] for r in cur.fetchall()]

    summary = _actions.DeletionSummary()
    for gid in gids:
        try:
            _actions.execute_group(gid, summary)
        except Exception as e:  # noqa: BLE001
            log.exception("llm execute_group(%s) failed", gid)
            summary.errors.append(f"group {gid}: {e}")
            summary.files_failed += 1
    log.info("auto_delete_llm min_confidence=%.2f: %s",
             min_confidence, summary.as_dict())
    return RedirectResponse("/", status_code=303)


@app.get("/api/llm/recent_calls")
def api_llm_recent_calls(limit: int = 20) -> dict:
    """Recent llm_match_log entries. Used by the LLM activity tile.

    Each row includes a quick-parsed ``groups_found`` count so the UI can
    show whether a call actually produced clusters, without making the
    client parse the full response JSON.
    """
    import json as _json
    rows: list[dict] = []
    with session() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT id, created_at, model, bucket_key, n_files,
                          response, error, elapsed_ms
                     FROM llm_match_log
                    ORDER BY id DESC
                    LIMIT %s""",
                (max(1, min(limit, 100)),),
            )
            for r in cur.fetchall():
                # Cheap parse to surface group count without dumping the whole
                # response into the JSON we return. We track three distinct
                # outcomes for the UI:
                #   parsed OK with groups   -> groups_found = N (>0)
                #   parsed OK, no groups    -> groups_found = 0
                #   response was empty      -> response_state = "empty"
                #   response couldn't parse -> response_state = "unparseable"
                groups_found = None
                members_found = None
                response_state = "ok"
                resp_text = r["response"] or ""
                if r["error"]:
                    response_state = "errored"
                elif not resp_text.strip():
                    response_state = "empty"
                else:
                    try:
                        from . import llm_match as _llm
                        parsed = _llm._extract_json(resp_text)
                        if parsed and isinstance(parsed.get("groups"), list):
                            grps = parsed["groups"]
                            groups_found = sum(
                                1 for g in grps
                                if isinstance(g.get("members"), list)
                                   and len(g["members"]) >= 2
                            )
                            members_found = sum(
                                len(g["members"]) for g in grps
                                if isinstance(g.get("members"), list)
                                   and len(g["members"]) >= 2
                            )
                        else:
                            response_state = "unparseable"
                    except Exception:                                # noqa: BLE001
                        response_state = "unparseable"
                rows.append({
                    "id": r["id"],
                    "created_at": r["created_at"].isoformat(),
                    "model": r["model"],
                    "bucket_key": r["bucket_key"],
                    "n_files": r["n_files"],
                    "groups_found": groups_found,
                    "members_found": members_found,
                    "response_state": response_state,
                    # First 240 chars of response for hover tooltip in UI
                    "response_preview": resp_text[:240] if resp_text else "",
                    "error": r["error"],
                    "elapsed_ms": r["elapsed_ms"],
                })
        except Exception:
            rows = []
    return {"calls": rows}


@app.get("/api/llm/progress")
def api_llm_progress() -> dict:
    """Progress summary for an in-flight LLM matcher run.

    Returns:
      running: True if an LLM matcher run is currently active
      run_id, run_started_at, run_elapsed_s: from orchestrator_runs
      calls_total, calls_during_run: total log entries / entries since this
        run started
      groups_so_far, files_so_far: rough running tallies (best-effort parse)
      last_call: the most recent call's quick stats
      eta_remaining_s: very rough -- based on average call latency × estimated
        remaining buckets (we don't know exact total without re-querying so
        this is a hint only)
    """
    from . import llm_match as _llm
    out: dict = {"running": False}
    with session() as conn, conn.cursor() as cur:
        # Is there an unfinished matcher run started by the LLM matcher?
        # We can't tell from orchestrator_runs alone -- 'matcher' kind covers
        # all matchers. Approximation: a matcher run in progress AND recent
        # log activity within the run's lifetime = LLM run.
        cur.execute(
            """SELECT id, started_at, finished_at,
                      EXTRACT(EPOCH FROM (NOW() - started_at)) AS elapsed_s
                 FROM orchestrator_runs
                WHERE kind = 'matcher' AND finished_at IS NULL
                ORDER BY id DESC LIMIT 1"""
        )
        active = cur.fetchone()
        if active:
            out["running"] = True
            out["run_id"] = active["id"]
            out["run_started_at"] = active["started_at"].isoformat()
            out["run_elapsed_s"] = float(active["elapsed_s"] or 0.0)

        # Total calls ever + calls in this run window
        cur.execute("SELECT COUNT(*) AS n FROM llm_match_log")
        out["calls_total"] = int((cur.fetchone() or {}).get("n") or 0)

        if active:
            cur.execute(
                """SELECT COUNT(*) AS n,
                          AVG(elapsed_ms) AS avg_ms,
                          SUM(n_files) AS files_seen
                     FROM llm_match_log
                    WHERE created_at >= %s""",
                (active["started_at"],),
            )
            r = cur.fetchone() or {}
            out["calls_during_run"] = int(r.get("n") or 0)
            out["avg_call_ms"] = float(r.get("avg_ms") or 0.0)
            out["files_seen_during_run"] = int(r.get("files_seen") or 0)
        else:
            out["calls_during_run"] = 0
            out["avg_call_ms"] = 0.0
            out["files_seen_during_run"] = 0

        # Latest log entry, for "last activity" timestamp
        cur.execute(
            """SELECT created_at, model, bucket_key, n_files, response, error,
                      elapsed_ms
                 FROM llm_match_log
                ORDER BY id DESC LIMIT 1"""
        )
        last = cur.fetchone()
        if last:
            groups_found = None
            try:
                parsed = _llm._extract_json(last["response"] or "")
                if parsed and isinstance(parsed.get("groups"), list):
                    groups_found = sum(
                        1 for g in parsed["groups"]
                        if isinstance(g.get("members"), list)
                           and len(g["members"]) >= 2
                    )
            except Exception:                                        # noqa: BLE001
                pass
            out["last_call"] = {
                "created_at": last["created_at"].isoformat(),
                "model": last["model"],
                "bucket_key": last["bucket_key"],
                "n_files": last["n_files"],
                "elapsed_ms": last["elapsed_ms"],
                "error": last["error"],
                "groups_found": groups_found,
            }
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - %s)) AS s",
                (last["created_at"],),
            )
            out["seconds_since_last_call"] = float(
                (cur.fetchone() or {}).get("s") or 0.0
            )

        # Rough running tally of LLM groups landed in dup_groups
        cur.execute(
            """SELECT COUNT(*) AS groups,
                      COALESCE(SUM(member_count.n), 0) AS members
                 FROM dup_groups g
                 LEFT JOIN LATERAL (
                     SELECT COUNT(*) AS n FROM dup_members m WHERE m.group_id = g.id
                 ) member_count ON TRUE
                WHERE g.match_type = 'llm'"""
        )
        r = cur.fetchone() or {}
        out["groups_landed"] = int(r.get("groups") or 0)
        out["members_landed"] = int(r.get("members") or 0)

    return out
