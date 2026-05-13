"""Build duplicate groups from fingerprinted files.

Three independent passes, each producing rows in ``dup_groups`` /
``dup_members``:

  1. Exact: same SHA-256 (within media_type).
  2. Video perceptual: average per-frame Hamming distance over the phash
     sequence is <= ``VIDEO_PHASH_THRESHOLD``.
  3. Audio Chromaprint: similarity >= ``CHROMAPRINT_THRESHOLD``.

Existing groups are wiped and rebuilt each run so this stays consistent with
the latest data. User actions (keeper / delete markers) are intentionally
*not* preserved across rebuilds -- file ids are stable so re-marking is fast,
and we don't want stale decisions on a re-clustered set.

Run as:
    docker compose --profile tools run --rm matcher
"""
from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations

from .config import CFG
from .db import session
from .phash import best_match_distance, chromaprint_similarity, from_signed64

log = logging.getLogger("matcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def reset_groups() -> None:
    with session() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE dup_groups RESTART IDENTITY CASCADE")


def insert_group(cur, media_type: str, match_type: str, similarity: float, file_ids: list[int],
                 keeper_id: int | None = None) -> int:
    cur.execute(
        "INSERT INTO dup_groups (media_type, match_type, similarity) "
        "VALUES (%s, %s, %s) RETURNING id",
        (media_type, match_type, similarity),
    )
    gid = cur.fetchone()["id"]
    for fid in file_ids:
        cur.execute(
            "INSERT INTO dup_members (group_id, file_id, is_keeper) VALUES (%s, %s, %s)",
            (gid, fid, fid == keeper_id),
        )
    return gid


def find_exact_groups() -> int:
    """Group by sha256 within media_type."""
    n = 0
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT media_type, sha256, ARRAY_AGG(id ORDER BY id) AS ids,
                      ARRAY_AGG(size ORDER BY id) AS sizes
                 FROM files
                WHERE sha256 IS NOT NULL AND status = 'done'
                GROUP BY media_type, sha256
               HAVING COUNT(*) > 1"""
        )
        for row in cur.fetchall():
            ids = row["ids"]
            # By definition all sha256 dupes are 100% identical -- pick the
            # first id (oldest by insertion) as the suggested keeper.
            insert_group(cur, row["media_type"], "exact", 1.0, ids, keeper_id=ids[0])
            n += 1
    return n


def _bucket_videos(rows: list[dict]) -> dict[tuple[int, int], list[dict]]:
    """Group videos by approximate duration to reduce O(n^2) comparisons."""
    buckets: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for r in rows:
        dur = r.get("duration") or 0
        # 10-second buckets, plus ±1 neighbour bucketing handled at compare time.
        key = int(dur // 10), 0
        buckets[key].append(r)
    return buckets


def find_video_perceptual() -> int:
    """Cluster videos by phash similarity. Skips files already in an exact group."""
    threshold = CFG.video_phash_threshold
    tol = CFG.video_duration_tolerance
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT f.id, f.path, f.size, f.duration, f.phashes
                 FROM files f
                WHERE f.media_type = 'video'
                  AND f.status = 'done'
                  AND f.phashes IS NOT NULL
                  AND array_length(f.phashes, 1) > 0
                  AND NOT EXISTS (
                        SELECT 1 FROM dup_members m
                          JOIN dup_groups g ON g.id = m.group_id
                         WHERE m.file_id = f.id AND g.match_type = 'exact')
             ORDER BY f.duration NULLS LAST"""
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    # Postgres stores phashes as signed bigint; reinterpret as unsigned
    # 64-bit before the bitwise comparisons (see app/phash.py).
    for r in rows:
        r["phashes"] = [from_signed64(p) for p in (r["phashes"] or [])]

    log.info("Video perceptual matching against %d candidates", len(rows))

    # Bucket by integer-second duration, allow +/- 1 bucket spillover.
    by_dur: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        d = int(round(r["duration"] or 0))
        by_dur[d].append(r)
    duration_keys = sorted(by_dur.keys())

    # Union-find for cluster merging.
    parent: dict[int, int] = {r["id"]: r["id"] for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Track best (lowest) distance seen between any pair within a cluster.
    best: dict[int, float] = {r["id"]: 64.0 for r in rows}

    # Compare each item against items in its bucket and ±tol-second neighbour
    # buckets. Same content from different encoders rounds to the same second
    # almost always; tolerance catches frame-rate / trim-edge differences.
    for dk in duration_keys:
        candidates: list[dict] = []
        for delta in range(-tol, tol + 1):
            candidates.extend(by_dur.get(dk + delta, []))
        # Within candidates, pairwise compare.
        for a, b in combinations(candidates, 2):
            if a["id"] >= b["id"]:                 # ensure each pair once
                a, b = b, a
            d = best_match_distance(a["phashes"], b["phashes"])
            if d <= threshold:
                union(a["id"], b["id"])
                best[a["id"]] = min(best[a["id"]], d)
                best[b["id"]] = min(best[b["id"]], d)

    # Collect clusters of size >= 2.
    clusters: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        clusters[find(fid)].append(fid)

    by_id = {r["id"]: r for r in rows}
    n_groups = 0
    with session() as conn, conn.cursor() as cur:
        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            # Suggested keeper: highest resolution * bitrate, ties broken by size.
            def score(fid: int) -> tuple:
                r = by_id[fid]
                # Larger file size and longer paths (more descriptive) preferred.
                return (r["size"], len(r["path"]))
            keeper = max(cluster, key=score)
            avg_d = sum(best[i] for i in cluster) / len(cluster)
            sim = max(0.0, 1.0 - (avg_d / 64.0))
            insert_group(cur, "video", "perceptual", sim, sorted(cluster), keeper_id=keeper)
            n_groups += 1
    return n_groups


def find_audio_chromaprint() -> int:
    """Cluster audio by Chromaprint similarity."""
    threshold = CFG.chromaprint_threshold
    tol = CFG.video_duration_tolerance
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT f.id, f.path, f.size, f.chromaprint, f.chromaprint_dur, f.duration,
                      f.bitrate
                 FROM files f
                WHERE f.media_type = 'audio'
                  AND f.status = 'done'
                  AND f.chromaprint IS NOT NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM dup_members m
                          JOIN dup_groups g ON g.id = m.group_id
                         WHERE m.file_id = f.id AND g.match_type = 'exact')
             ORDER BY f.duration NULLS LAST"""
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    log.info("Audio chromaprint matching against %d candidates", len(rows))

    # Bucket by ±2s duration to keep comparisons local.
    by_dur: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        d = int(round(r["chromaprint_dur"] or r["duration"] or 0))
        by_dur[d].append(r)

    parent: dict[int, int] = {r["id"]: r["id"] for r in rows}
    best: dict[int, float] = {r["id"]: 0.0 for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for dk in sorted(by_dur):
        candidates: list[dict] = []
        for delta in range(-tol, tol + 1):
            candidates.extend(by_dur.get(dk + delta, []))
        for a, b in combinations(candidates, 2):
            if a["id"] >= b["id"]:
                a, b = b, a
            sim = chromaprint_similarity(a["chromaprint"], b["chromaprint"])
            if sim >= threshold:
                union(a["id"], b["id"])
                best[a["id"]] = max(best[a["id"]], sim)
                best[b["id"]] = max(best[b["id"]], sim)

    clusters: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        clusters[find(fid)].append(fid)

    by_id = {r["id"]: r for r in rows}
    n_groups = 0
    with session() as conn, conn.cursor() as cur:
        for cluster in clusters.values():
            if len(cluster) < 2:
                continue
            def score(fid: int) -> tuple:
                r = by_id[fid]
                return (r["bitrate"] or 0, r["size"])
            keeper = max(cluster, key=score)
            avg_sim = sum(best[i] for i in cluster) / len(cluster)
            insert_group(cur, "audio", "chromaprint", avg_sim, sorted(cluster), keeper_id=keeper)
            n_groups += 1
    return n_groups


def main() -> None:
    log.info("Resetting existing duplicate groups")
    reset_groups()
    n1 = find_exact_groups()
    log.info("Exact groups: %d", n1)
    n2 = find_video_perceptual()
    log.info("Video perceptual groups: %d", n2)
    n3 = find_audio_chromaprint()
    log.info("Audio chromaprint groups: %d", n3)
    log.info("Total: %d duplicate groups", n1 + n2 + n3)


if __name__ == "__main__":
    main()
