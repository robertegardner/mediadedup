"""Source management: configurable scan roots, persisted in the DB.

A "source" is a named subdirectory under ``/media`` inside the containers,
corresponding to a host bind mount that the operator manages externally (via
fstab or autofs). Sources have:

  * name: short label for UI ('whisparr', 'archive-2019', 'family-photos')
  * path: absolute path *inside the container*, e.g. /media/whisparr
  * media_type: 'video', 'audio', or 'both' -- controls which extension lists
                the scanner applies under this root
  * priority: integer used as a tiebreaker for keeper selection when dupes
              are found across sources; higher wins
  * enabled: scanner skips this source when false

Path-to-source mapping: every existing file row has a path like
``/media/whisparr/Movies/Foo.mkv``. We resolve its source by **longest-prefix
match** against configured source paths. This lets us migrate from the old
``VIDEO_ROOT/MUSIC_ROOT`` setup transparently -- we add sources named
'video' and 'music' pointing at the old paths and existing rows light up.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterator

from .db import session

log = logging.getLogger("sources")


@dataclass
class Source:
    id: int
    name: str
    path: str
    media_type: str       # 'video', 'audio', 'both'
    priority: int
    enabled: bool
    notes: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Source":
        return cls(
            id=row["id"], name=row["name"], path=row["path"],
            media_type=row["media_type"], priority=row["priority"],
            enabled=row["enabled"], notes=row.get("notes"),
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_sources(enabled_only: bool = False) -> list[Source]:
    where = "WHERE enabled = TRUE" if enabled_only else ""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM sources {where} ORDER BY priority DESC, name ASC"
        )
        return [Source.from_row(r) for r in cur.fetchall()]


def get_source(source_id: int) -> Source | None:
    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM sources WHERE id = %s", (source_id,))
        r = cur.fetchone()
        return Source.from_row(r) if r else None


def get_source_by_name(name: str) -> Source | None:
    with session() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM sources WHERE name = %s", (name,))
        r = cur.fetchone()
        return Source.from_row(r) if r else None


def create_source(name: str, path: str, media_type: str,
                  priority: int = 0, enabled: bool = True,
                  notes: str | None = None) -> Source:
    if media_type not in ("video", "audio", "both"):
        raise ValueError(f"invalid media_type: {media_type}")
    # Canonicalize: no trailing slash so longest-prefix matching is clean.
    path = path.rstrip("/") or "/"
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sources (name, path, media_type, priority, enabled, notes)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (name, path, media_type, priority, enabled, notes),
        )
        return Source.from_row(cur.fetchone())


def update_source(source_id: int, **fields) -> Source | None:
    """Update mutable fields. Allowed: name, path, media_type, priority,
    enabled, notes. Returns the updated source or None if it doesn't exist."""
    allowed = {"name", "path", "media_type", "priority", "enabled", "notes"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        return get_source(source_id)
    if "path" in fields:
        fields["path"] = fields["path"].rstrip("/") or "/"
    if "media_type" in fields and fields["media_type"] not in ("video", "audio", "both"):
        raise ValueError(f"invalid media_type: {fields['media_type']}")

    sets = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values()) + [source_id]
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE sources SET {sets} WHERE id = %s RETURNING *", params,
        )
        r = cur.fetchone()
        return Source.from_row(r) if r else None


def delete_source(source_id: int) -> bool:
    """Remove a source. Existing ``files.source_id`` references go NULL
    automatically because we don't put a hard FK constraint -- file history
    survives source deletion. The operator can re-add the source later and
    re-resolve via ``backfill_source_ids``."""
    with session() as conn, conn.cursor() as cur:
        cur.execute("UPDATE files SET source_id = NULL WHERE source_id = %s",
                    (source_id,))
        cur.execute("DELETE FROM sources WHERE id = %s", (source_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Path-to-source resolution (longest-prefix match)
# ---------------------------------------------------------------------------

def resolve_source_for_path(path: str, sources: list[Source] | None = None) -> Source | None:
    """Return the configured source that contains ``path``, or None.

    Longest-prefix match: if both /media/a and /media/a/sub are configured,
    a file at /media/a/sub/foo.mkv resolves to /media/a/sub.
    """
    if sources is None:
        sources = list_sources(enabled_only=False)
    norm = path.rstrip("/")
    best: Source | None = None
    best_len = -1
    for s in sources:
        sp = s.path.rstrip("/")
        # Require either an exact match or a path component boundary.
        if norm == sp or norm.startswith(sp + "/"):
            if len(sp) > best_len:
                best = s
                best_len = len(sp)
    return best


def backfill_source_ids(only_null: bool = True) -> int:
    """Walk existing file rows and populate ``source_id`` from their path.

    Cheap: one SQL pass + Python prefix match. Run after adding/changing
    sources. Returns the number of rows updated.
    """
    sources = list_sources(enabled_only=False)
    if not sources:
        return 0
    n = 0
    with session() as conn, conn.cursor() as cur:
        sql = "SELECT id, path FROM files"
        if only_null:
            sql += " WHERE source_id IS NULL"
        cur.execute(sql)
        rows = cur.fetchall()
        for row in rows:
            s = resolve_source_for_path(row["path"], sources)
            if s is None:
                continue
            cur.execute(
                "UPDATE files SET source_id = %s WHERE id = %s",
                (s.id, row["id"]),
            )
            n += 1
    return n


# ---------------------------------------------------------------------------
# Walking the sources at scan time
# ---------------------------------------------------------------------------

def enabled_sources_with_check() -> Iterator[Source]:
    """Yield enabled sources whose path exists on disk inside the container.

    Sources whose path is missing are logged and skipped (typical when an
    autofs mount hasn't been triggered yet, or the host mount is down).
    """
    for s in list_sources(enabled_only=True):
        if not os.path.exists(s.path):
            log.warning("Source %r path does not exist inside container: %s",
                        s.name, s.path)
            continue
        if not os.path.isdir(s.path):
            log.warning("Source %r path is not a directory: %s", s.name, s.path)
            continue
        yield s


# ---------------------------------------------------------------------------
# Trash-directory helper (per-source)
# ---------------------------------------------------------------------------

def trash_dir_for(source: Source) -> str:
    """Return the canonical trash directory for files originating from
    ``source``. Files moved here stay on the same filesystem, so the move
    is a cheap rename."""
    return os.path.join(source.path, ".mediadedup-trash")


# ---------------------------------------------------------------------------
# Bootstrap from legacy env vars
# ---------------------------------------------------------------------------

def bootstrap_legacy_env() -> None:
    """First-run convenience: if no sources are configured but the old
    VIDEO_ROOT / MUSIC_ROOT env vars are set, create matching sources so
    existing installs continue to work without manual intervention."""
    existing = list_sources(enabled_only=False)
    if existing:
        return  # User has configured at least one source; don't touch.

    video_root = os.environ.get("VIDEO_ROOT", "/media/video")
    music_root = os.environ.get("MUSIC_ROOT", "/media/music")

    bootstrapped: list[str] = []
    if video_root and os.path.isdir(video_root):
        try:
            create_source(name="video", path=video_root, media_type="video",
                          priority=0, notes="Bootstrapped from VIDEO_ROOT")
            bootstrapped.append(f"video → {video_root}")
        except Exception:                                            # noqa: BLE001
            log.exception("could not bootstrap video source")
    if music_root and os.path.isdir(music_root):
        try:
            create_source(name="music", path=music_root, media_type="audio",
                          priority=0, notes="Bootstrapped from MUSIC_ROOT")
            bootstrapped.append(f"music → {music_root}")
        except Exception:                                            # noqa: BLE001
            log.exception("could not bootstrap music source")
    if bootstrapped:
        log.info("Bootstrapped sources from env: %s", ", ".join(bootstrapped))
        try:
            n = backfill_source_ids()
            if n:
                log.info("Backfilled source_id on %d existing rows", n)
        except Exception:                                            # noqa: BLE001
            log.exception("source_id backfill failed")
