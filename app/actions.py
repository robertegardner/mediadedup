"""Shared deletion helpers.

Centralizes the "move file to trash" operation and the bulk auto-mark-and-
execute flow for exact (sha256) duplicate groups. Used by both the web
UI's per-group execute button and the ``bulk_actions`` CLI.

Design choices worth knowing:
  * Files are NEVER unlinked. They are renamed via shutil.move into a
    dated trash directory on the same mount. Operator purges manually.
  * Every action -- success or failure -- writes a row to ``action_log``
    with bytes_freed and any error message.
  * The "keeper" for an exact group is whichever member has the longest
    path (a heuristic for the most descriptive filename) with size as a
    tiebreaker. All other members are queued for deletion.
  * Bulk operations are wrapped in a single transaction per group, so a
    crash between two files in the same group doesn't leave the DB
    inconsistent. Crashes between groups can leave some groups done and
    others not; the operation is idempotent (re-running picks up where
    it left off).
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import CFG
from .db import session
from . import sources as sources_mod

log = logging.getLogger(__name__)


# Legacy trash paths -- only used as a fallback when a file row has no
# source_id (e.g. orphaned by a deleted source row). New code should always
# get its trash dir via ``_trash_root_for_file``.
LEGACY_VIDEO_TRASH = Path(CFG.video_root) / ".mediadedup-trash"
LEGACY_MUSIC_TRASH = Path(CFG.music_root) / ".mediadedup-trash"


@dataclass
class DeletionSummary:
    groups_processed: int = 0
    files_deleted: int = 0
    files_failed: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "groups_processed": self.groups_processed,
            "files_deleted": self.files_deleted,
            "files_failed": self.files_failed,
            "bytes_freed": self.bytes_freed,
            "error_sample": self.errors[:5],
        }


def _trash_root_for_file(file_row: dict) -> Path:
    """Find the right trash directory for a file row.

    Priority:
      1. The source the file belongs to (preferred -- keeps trash on same fs)
      2. Fall back to legacy global paths if source_id is NULL (orphaned)
    """
    src_id = file_row.get("source_id")
    if src_id:
        s = sources_mod.get_source(src_id)
        if s is not None:
            return Path(sources_mod.trash_dir_for(s))
    # Orphan: try to resolve by path, last resort is the legacy mount.
    all_sources = sources_mod.list_sources(enabled_only=False)
    s = sources_mod.resolve_source_for_path(file_row["path"], all_sources)
    if s is not None:
        return Path(sources_mod.trash_dir_for(s))
    return LEGACY_VIDEO_TRASH if file_row["media_type"] == "video" else LEGACY_MUSIC_TRASH


def execute_group(group_id: int, summary: DeletionSummary | None = None) -> DeletionSummary:
    """Move every member of group ``group_id`` whose action is 'delete' into
    the dated trash directory on its mount, then mark the group reviewed.

    The keeper file is left in place. Files marked 'ignore' or 'keep' are
    left in place. The group is always marked reviewed afterwards, even if
    nothing was actually moved.
    """
    summary = summary or DeletionSummary()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT f.id, f.path, f.size, f.media_type, f.source_id,
                      m.action, m.is_keeper
                 FROM dup_members m JOIN files f ON f.id = m.file_id
                WHERE m.group_id = %s""",
            (group_id,),
        )
        members = cur.fetchall()

        for m in members:
            # action is NULL for exact/perceptual/chromaprint groups until
            # auto_mark or "Save markings" runs. Fall back to is_keeper.
            action = m["action"] if m["action"] is not None else (
                "keep" if m["is_keeper"] else "delete"
            )
            if action != "delete" or m["is_keeper"]:
                continue
            src = Path(m["path"])
            if not src.exists():
                cur.execute(
                    "INSERT INTO action_log (file_id, path, action, succeeded, error) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (m["id"], str(src), "delete", False, "missing"),
                )
                cur.execute("UPDATE files SET status='missing' WHERE id=%s", (m["id"],))
                summary.files_failed += 1
                summary.errors.append(f"missing: {src}")
                continue

            trash_root = _trash_root_for_file(m)
            dst_dir = trash_root / today
            try:
                dst_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                cur.execute(
                    "INSERT INTO action_log (file_id, path, action, succeeded, error) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (m["id"], str(src), "delete", False, f"mkdir: {e}"),
                )
                summary.files_failed += 1
                summary.errors.append(f"mkdir {dst_dir}: {e}")
                continue

            dst = dst_dir / f"{m['id']}__{src.name}"
            try:
                shutil.move(str(src), str(dst))
                cur.execute(
                    "INSERT INTO action_log (file_id, path, action, succeeded, bytes_freed) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (m["id"], str(src), "delete", True, m["size"]),
                )
                cur.execute("UPDATE files SET status='deleted' WHERE id=%s", (m["id"],))
                summary.files_deleted += 1
                summary.bytes_freed += int(m["size"] or 0)
            except OSError as e:
                cur.execute(
                    "INSERT INTO action_log (file_id, path, action, succeeded, error) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (m["id"], str(src), "delete", False, str(e)),
                )
                summary.files_failed += 1
                summary.errors.append(f"move {src}: {e}")

        cur.execute("UPDATE dup_groups SET reviewed = TRUE WHERE id = %s", (group_id,))

    summary.groups_processed += 1
    return summary


def _match_type_filter(match_types: list[str] | None, params: list) -> str:
    """Return SQL fragment + extends params for filtering by match_type."""
    if not match_types:
        return ""
    placeholders = ",".join(["%s"] * len(match_types))
    params.extend(match_types)
    return f" AND g.match_type IN ({placeholders})"


def _media_filter(media_type: str | None, params: list) -> str:
    if media_type not in ("video", "audio"):
        return ""
    params.append(media_type)
    return " AND g.media_type = %s"


def _similarity_filter(min_similarity: float | None, params: list) -> str:
    if min_similarity is None:
        return ""
    params.append(min_similarity)
    return " AND g.similarity >= %s"


# Keeper-selection SQL per match type. Different match types have different
# notions of "best copy":
#   * exact: any will do; longest path is a good readability heuristic.
#   * perceptual (video): higher resolution × bitrate is the better encode;
#     fall back to size, then file id.
#   * chromaprint (audio): higher bitrate then larger size; fall back to id.
#
# Source priority (joined in as ``s.priority``) is the OUTER tiebreaker for
# every match type: files from higher-priority sources are always preferred
# as keepers. NULL priority (no source assigned) sorts last.
_KEEPER_TIEBREAKERS: dict[str, str] = {
    "exact": "LENGTH(f.path) DESC, f.size DESC, f.id ASC",
    "perceptual": (
        "(COALESCE(f.width,0) * COALESCE(f.height,0)) DESC, "
        "COALESCE(f.bitrate,0) DESC, f.size DESC, f.id ASC"
    ),
    "chromaprint": "COALESCE(f.bitrate,0) DESC, f.size DESC, f.id ASC",
    # Filename matches use the same logic as perceptual: prefer higher
    # resolution × bitrate, then size, then lowest id. The filename matcher
    # also pre-marked keepers when it inserted dup_members, so auto-mark
    # using this order_by mostly just confirms that selection.
    "filename": (
        "(COALESCE(f.width,0) * COALESCE(f.height,0)) DESC, "
        "COALESCE(f.bitrate,0) DESC, f.size DESC, f.id ASC"
    ),
    # LLM matches use the same tiebreakers as filename matches.
    "llm": (
        "(COALESCE(f.width,0) * COALESCE(f.height,0)) DESC, "
        "COALESCE(f.bitrate,0) DESC, f.size DESC, f.id ASC"
    ),
}


def _keeper_order_by(match_type: str) -> str:
    tb = _KEEPER_TIEBREAKERS.get(match_type, _KEEPER_TIEBREAKERS["exact"])
    # Source priority first (NULLS LAST so unassigned files lose ties).
    return f"COALESCE(s.priority, -1) DESC, {tb}"


def auto_mark_groups(
    match_types: list[str] | None = None,
    media_type: str | None = None,
    min_similarity: float | None = None,
) -> int:
    """For every unreviewed group matching the filters, pick a keeper and
    mark every other member ``delete``. Returns number of groups updated.

    Args:
      match_types: list like ['exact'], ['perceptual'], ['perceptual','chromaprint'].
                   None means all types.
      media_type:  'video', 'audio', or None for both.
      min_similarity: if set, only groups with g.similarity >= this value.

    Keeper rules vary by match_type -- see ``_KEEPER_ORDER_BY``.
    """
    params: list = []
    where_match = _match_type_filter(match_types, params)
    where_media = _media_filter(media_type, params)
    where_sim = _similarity_filter(min_similarity, params)

    n = 0
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT g.id AS group_id, g.match_type
                  FROM dup_groups g
                 WHERE g.reviewed = FALSE
                   {where_match}{where_media}{where_sim}""",
            params,
        )
        groups = cur.fetchall()

        for grp in groups:
            gid = grp["group_id"]
            order_by = _keeper_order_by(grp["match_type"])
            cur.execute(
                f"""SELECT f.id, f.path, f.size
                      FROM dup_members m
                      JOIN files f ON f.id = m.file_id
                      LEFT JOIN sources s ON s.id = f.source_id
                     WHERE m.group_id = %s
                     ORDER BY {order_by}""",
                (gid,),
            )
            members = cur.fetchall()
            if len(members) < 2:
                continue
            keeper_id = members[0]["id"]
            cur.execute(
                "UPDATE dup_members SET is_keeper = (file_id = %s) WHERE group_id = %s",
                (keeper_id, gid),
            )
            cur.execute(
                "UPDATE dup_members SET action = CASE WHEN file_id = %s "
                "THEN 'keep' ELSE 'delete' END WHERE group_id = %s",
                (keeper_id, gid),
            )
            n += 1
    return n


def execute_groups(
    match_types: list[str] | None = None,
    media_type: str | None = None,
    min_similarity: float | None = None,
) -> DeletionSummary:
    """Execute deletions for every unreviewed group matching the filters.

    Call ``auto_mark_groups`` with the same filters first.
    """
    params: list = []
    where_match = _match_type_filter(match_types, params)
    where_media = _media_filter(media_type, params)
    where_sim = _similarity_filter(min_similarity, params)

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT g.id AS group_id
                  FROM dup_groups g
                 WHERE g.reviewed = FALSE
                   {where_match}{where_media}{where_sim}
                 ORDER BY g.id""",
            params,
        )
        group_ids = [r["group_id"] for r in cur.fetchall()]

    summary = DeletionSummary()
    for gid in group_ids:
        try:
            execute_group(gid, summary)
        except Exception as e:                                       # noqa: BLE001
            log.exception("execute_group(%s) failed", gid)
            summary.errors.append(f"group {gid}: {e}")
            summary.files_failed += 1
    return summary


def preview_groups(
    match_types: list[str] | None = None,
    media_type: str | None = None,
    min_similarity: float | None = None,
) -> dict:
    """Summarize what auto-deletion would do, without changing anything.

    Returns a dict keyed by (media_type, match_type) tuples for granularity:
        {('video', 'exact'): {groups, files_to_delete, bytes_to_free}, ...}
    """
    params: list = []
    where_match = _match_type_filter(match_types, params)
    where_media = _media_filter(media_type, params)
    where_sim = _similarity_filter(min_similarity, params)

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT g.media_type,
                       g.match_type,
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
                 WHERE g.reviewed = FALSE
                   {where_match}{where_media}{where_sim}
                 GROUP BY g.media_type, g.match_type
                 ORDER BY g.media_type, g.match_type""",
            params,
        )
        return {(r["media_type"], r["match_type"]): dict(r) for r in cur.fetchall()}


# --- Back-compat shims ------------------------------------------------------
# Older code paths (web UI's existing "exact" buttons, original CLI) still
# call the *_exact variants. Keep them working by delegating.

def auto_mark_exact(media_type: str | None = None) -> int:
    return auto_mark_groups(match_types=["exact"], media_type=media_type)


def execute_exact_groups(media_type: str | None = None) -> DeletionSummary:
    return execute_groups(match_types=["exact"], media_type=media_type)


def preview_exact_groups(media_type: str | None = None) -> dict:
    rows = preview_groups(match_types=["exact"], media_type=media_type)
    # Old shape: {media_type: {...}}; new shape: {(media_type, match_type): {...}}
    return {mt: row for (mt, _match), row in rows.items()}
