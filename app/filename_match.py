"""Filename-based duplicate detection.

Cheap pre-pass that groups videos by similar filenames before any fingerprint
work runs. Catches the largest source of duplicates in libraries fed by
automated downloaders: re-downloads of the same scene with different release
group, container, or resolution.

Two-stage approach:

  1. Normalize each filename to a content-identity key by stripping:
     - sequential numeric suffixes like ``(1)`` or ``_1``
     - resolution markers (1080p, 720p, 4K, etc.)
     - container/codec/source tags (MP4, MKV, H264, WEB-DL, XXX, etc.)
     - release group tags (-P2P, -WRB, [XvX], etc.)
     - parenthesised/bracketed asides
     - extra punctuation and whitespace

     Files with identical normalized keys form an exact filename group.

  2. For files that didn't match by exact key but live in the same parent
     directory, do a TOKEN-SET comparison: extract a normalized bag of
     content tokens (date + actor + scene-title words) and group when the
     Jaccard similarity exceeds a threshold. Catches:
       - same scene, different release groups whose normalized keys differ
       - word-order differences ("Foo Bar" vs "Bar Foo")
       - one file having more descriptive tokens than the other

Resolution comes from the filename. A file labeled 2160p outranks 1080p,
which outranks 720p, etc. Unknown resolution sorts last.

This module exposes one public function: ``find_filename_matches()`` which
populates dup_groups with ``match_type='filename'``. The dashboard treats
filename groups the same as exact/perceptual/chromaprint groups.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .db import session

log = logging.getLogger("filename_match")


# ---------------------------------------------------------------------------
# Resolution parsing
# ---------------------------------------------------------------------------
# Higher number = higher quality. We use these as keeper-selection scores and
# for the "delete only when resolution is the differentiator" rule.

RESOLUTION_RANK: dict[str, int] = {
    "4320p": 4320, "8k": 4320,
    "2160p": 2160, "4k": 2160, "uhd": 2160,
    "1440p": 1440, "2k": 1440, "qhd": 1440,
    "1080p": 1080, "fhd": 1080,
    "720p": 720, "hd": 720,
    "576p": 576,
    "540p": 540,
    "480p": 480, "sd": 480,
    "360p": 360,
    "240p": 240,
}

# Match resolution as a standalone token. Bounded by non-word so 21080p does
# not match as 1080p.
_RES_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in RESOLUTION_RANK) + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def extract_resolution(name: str) -> tuple[str | None, int]:
    """Return (label, rank). If no resolution found, returns (None, 0)."""
    m = _RES_RE.search(name)
    if not m:
        return None, 0
    tag = m.group(1).lower()
    return tag, RESOLUTION_RANK[tag]


# ---------------------------------------------------------------------------
# Filename normalization
# ---------------------------------------------------------------------------

# Tags we strip wholesale. They're the noise in scene-release naming.
_NOISE_TOKENS = {
    # Containers / codecs
    "mp4", "mkv", "avi", "mov", "webm", "wmv", "flv", "m4v", "ts",
    "h264", "h265", "x264", "x265", "hevc", "avc", "aac", "mp3",
    # Sources
    "web", "webdl", "web-dl", "webrip", "bluray", "brrip", "bdrip",
    "hdtv", "dvdrip", "remux",
    # Content tags
    "xxx", "rip", "siterip", "complete", "uncensored", "censored",
    "remastered", "extended", "directors", "cut",
}

# Release-group-style trailing tags: -GROUPNAME, -GROUPNAME[XvX], etc.
# These almost never carry content identity.
_RELEASE_GROUP_RE = re.compile(
    r"-[a-z0-9]{2,15}(?:\[[a-z0-9]{2,10}\])?$",
    re.IGNORECASE,
)

# Bracketed annotations -- [XvX], [XC], [whatever]
_BRACKET_RE = re.compile(r"[\[\(\{][^\]\)\}]*[\]\)\}]")

def _basename_no_ext(path: str) -> str:
    base = os.path.basename(path)
    base = os.path.splitext(base)[0]
    return base


def normalize(path: str) -> str:
    """Produce a content-identity key for one filepath.

    Two files with identical normalize() output are very likely the same
    underlying scene. Two files with different output are NOT necessarily
    different; phase-2 token comparison catches near-misses.
    """
    name = _basename_no_ext(path).lower()

    # Strip bracketed asides anywhere in the name. Do this early so the
    # release-group regex sees a clean trailing slot.
    name = _BRACKET_RE.sub(" ", name)

    # Strip release-group tag if it's at the end.
    name = _RELEASE_GROUP_RE.sub("", name)

    # Strip resolution markers.
    name = _RES_RE.sub(" ", name)

    # Normalize separators to spaces so we can token-split.
    name = re.sub(r"[._\-]+", " ", name)

    # Token-level filter: drop noise tokens
    tokens = [t for t in name.split() if t and t not in _NOISE_TOKENS]

    # Drop pure-numeric tokens that are clearly noise:
    # - single-digit tokens (sequential counters)
    # - 1-2 digit tokens (scene numbers, "Vol 1", "Pt 2") UNLESS they appear
    #   to be part of a date sequence like 25 03 12 (three short numbers in
    #   a row). We preserve dates because the user wants different-date
    #   versions of the same scene treated as distinct.
    def _is_date_neighbor(idx: int) -> bool:
        """True if this short number is part of a YY MM DD sequence."""
        # Look at this token and its neighbors. Window of 3 short-numeric.
        nbr = []
        for k in range(max(0, idx - 2), min(len(tokens), idx + 3)):
            t = tokens[k]
            if t.isdigit() and 1 <= len(t) <= 2:
                nbr.append(k)
        # If at least 3 consecutive indices are short-numerics, treat as date.
        if len(nbr) < 3:
            return False
        # Are 3+ of them consecutive?
        run = 1
        for i in range(1, len(nbr)):
            if nbr[i] == nbr[i - 1] + 1:
                run += 1
                if run >= 3:
                    return True
            else:
                run = 1
        return False

    kept = []
    for i, t in enumerate(tokens):
        if t.isdigit() and len(t) <= 2 and not _is_date_neighbor(i):
            continue
        kept.append(t)
    tokens = kept

    rejoined = " ".join(tokens)

    # Collapse whitespace
    rejoined = re.sub(r"\s+", " ", rejoined).strip()
    return rejoined


# ---------------------------------------------------------------------------
# Token-set similarity (phase 2)
# ---------------------------------------------------------------------------

# Common surname-like words that show up in scene titles but don't identify
# content. Kept short -- the date and actor names do the work.
_STOPWORDS = {
    "and", "the", "with", "of", "in", "on", "a", "an", "to", "for",
    "from", "by", "feat", "ft", "vs", "or", "her", "his", "him", "she",
    "he", "it", "they", "this", "that", "those", "these",
}


def content_tokens(path: str) -> set[str]:
    """Bag-of-content-tokens for token-set comparison."""
    normalized = normalize(path)
    return {t for t in normalized.split() if t and t not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# Threshold for phase-2 grouping. Tuned conservative -- groups 25, 27, 47 in
# Bob's sample all overlap by >0.85 in normalized tokens. Group 54 (the
# scrambled one) is around 0.5 and SHOULD NOT auto-match.
TOKEN_SET_THRESHOLD = 0.80


# ---------------------------------------------------------------------------
# Keeper scoring
# ---------------------------------------------------------------------------

@dataclass
class FileRow:
    id: int
    path: str
    size: int
    source_priority: int = 0
    resolution_label: str | None = None
    resolution_rank: int = 0
    width: int = 0
    height: int = 0
    bitrate: int = 0


def keeper_score(f: FileRow) -> tuple:
    """Sort key for keeper selection. Higher tuple wins.

    Tiebreakers in order:
      1. Source priority (configured per-source weight)
      2. Resolution rank from the FILENAME (cheap)
      3. Actual pixel area from ffprobe if we have it (more accurate)
      4. Bitrate
      5. Size
      6. Negative id (older id = lower-numbered = wins ties deterministically)
    """
    return (
        f.source_priority,
        f.resolution_rank,
        f.width * f.height,
        f.bitrate,
        f.size,
        -f.id,
    )


def resolution_only_differs(files: Iterable[FileRow]) -> bool:
    """True if the only meaningful difference among these files' filenames is
    their resolution tag. Used to decide whether auto-delete is safe.

    Comparison: for each file, strip the resolution tag and see if the
    remaining normalized name is identical across the group. If yes,
    they're the same content at different resolutions -- safe to keep
    only the highest.
    """
    keys = set()
    for f in files:
        # Re-normalize but with the resolution token already removed by
        # normalize(). The normalize function strips resolutions, so files
        # of the same content at different res should already produce the
        # same normalized key. If they don't, something else differs.
        keys.add(normalize(f.path))
        if len(keys) > 1:
            return False
    return True


# ---------------------------------------------------------------------------
# Public entry point: scan files, build filename groups
# ---------------------------------------------------------------------------

def find_filename_matches(
    min_group_size: int = 2,
    media_type: str = "video",
    only_status: tuple[str, ...] = ("done", "pending", "processing"),
) -> dict:
    """Build dup_groups with match_type='filename'.

    Returns a stats dict for the orchestrator. Idempotent -- existing
    'filename' groups are wiped and rebuilt from the current file table.

    Args:
        min_group_size: minimum members for a group to be recorded (default 2)
        media_type: 'video' or 'audio'; default video
        only_status: which file statuses to include. Default excludes
                     'deleted' and 'missing' so we don't re-mark files
                     that are already gone.
    """
    log.info("Filename matching: media_type=%s statuses=%s",
             media_type, only_status)

    with session() as conn, conn.cursor() as cur:
        # Pull all candidate files. We need width/height/bitrate where
        # available for better keeper scoring, but the filename group works
        # without them.
        placeholders = ",".join(["%s"] * len(only_status))
        cur.execute(
            f"""SELECT f.id, f.path, f.size,
                       COALESCE(s.priority, -1) AS source_priority,
                       COALESCE(f.width, 0) AS width,
                       COALESCE(f.height, 0) AS height,
                       COALESCE(f.bitrate, 0) AS bitrate
                  FROM files f
                  LEFT JOIN sources s ON s.id = f.source_id
                 WHERE f.media_type = %s
                   AND f.status IN ({placeholders})
                """,
            (media_type, *only_status),
        )
        rows = cur.fetchall()

    log.info("Filename matching: %d candidate files", len(rows))
    if not rows:
        return {"candidates": 0, "groups": 0, "members": 0}

    files: list[FileRow] = []
    for r in rows:
        label, rank = extract_resolution(r["path"])
        files.append(FileRow(
            id=r["id"], path=r["path"], size=r["size"],
            source_priority=r["source_priority"],
            resolution_label=label, resolution_rank=rank,
            width=r["width"], height=r["height"], bitrate=r["bitrate"],
        ))

    # Phase 1: exact-key grouping
    by_key: dict[str, list[FileRow]] = defaultdict(list)
    for f in files:
        key = normalize(f.path)
        if key:
            by_key[key].append(f)

    exact_groups = [g for g in by_key.values() if len(g) >= min_group_size]
    log.info("Phase 1 (exact-key): %d groups covering %d files",
             len(exact_groups), sum(len(g) for g in exact_groups))

    # Phase 2: token-set grouping among singletons whose normalized key
    # didn't already match anyone. Bucket by parent directory to keep the
    # comparison cheap -- we only compare files in the same folder. This
    # is the common case for Whisparr-style libraries where re-downloads
    # land in the same series folder.
    singletons = [g[0] for g in by_key.values() if len(g) == 1]
    by_dir: dict[str, list[FileRow]] = defaultdict(list)
    for f in singletons:
        by_dir[os.path.dirname(f.path)].append(f)

    # Also bucket by the parent of the parent, so files placed in
    # per-scene subdirectories ("Slayed/Delicious Duo .../...mp4" vs
    # "Slayed.25.12.23.../...mp4") can still match.
    by_grandparent: dict[str, list[FileRow]] = defaultdict(list)
    for f in singletons:
        gp = os.path.dirname(os.path.dirname(f.path))
        by_grandparent[gp].append(f)

    # Token-set match within each bucket. Union-find for cluster building.
    parent: dict[int, int] = {f.id: f.id for f in singletons}
    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Pre-compute tokens once per file
    tokens_for: dict[int, set[str]] = {f.id: content_tokens(f.path) for f in singletons}

    # CRITICAL: phase 2's all-pairs comparison is O(n^2) per bucket. On a
    # library where many files live directly under one root directory (e.g.
    # /media/whisparr/file1.mp4, file2.mp4, ...) a single bucket can hold
    # 10000+ files, producing 50M+ pair comparisons. To stay tractable we:
    #
    #  1. Cap absolute bucket size. Buckets above this size get SUB-BUCKETED
    #     by the file's first content token (typically the studio name), which
    #     is the strongest predictor of relatedness anyway.
    #  2. Skip a bucket entirely if even after sub-bucketing a slice is still
    #     too big -- those files won't cluster meaningfully by name anyway,
    #     and the fingerprint matcher will catch real dupes among them later.
    MAX_BUCKET_SIZE = 200          # below this, all-pairs is fine
    MAX_SUB_BUCKET_SIZE = 400      # absolute ceiling after sub-bucketing
    PAIR_COMPARE_BUDGET = 2_000_000  # hard cap across all buckets

    def _subdivide(big_bucket: list[FileRow]) -> Iterable[list[FileRow]]:
        """Split a large bucket into sub-buckets keyed by the file's first
        alphabetic content token. Numbers (dates, sequence numbers) make
        poor sub-bucket keys because they cluster many files together; the
        first letter-bearing token is typically the studio/source name and
        is highly discriminative."""
        by_first: dict[str, list[FileRow]] = defaultdict(list)
        for fr in big_bucket:
            tokens = tokens_for.get(fr.id) or set()
            # Prefer the first alphabetic token. Falls back to first numeric
            # token if there are no letters; '_empty_' if neither exists.
            alpha = sorted(t for t in tokens if any(c.isalpha() for c in t))
            if alpha:
                key = alpha[0]
            elif tokens:
                key = sorted(tokens)[0]
            else:
                key = "_empty_"
            by_first[key].append(fr)
        return by_first.values()

    def _all_buckets() -> Iterable[list[FileRow]]:
        """Walk dir + grandparent buckets, sub-dividing oversized ones."""
        for bucket in list(by_dir.values()) + list(by_grandparent.values()):
            if len(bucket) < 2:
                continue
            if len(bucket) <= MAX_BUCKET_SIZE:
                yield bucket
                continue
            log.info("Sub-bucketing bucket of %d files by first content token",
                     len(bucket))
            for sub in _subdivide(bucket):
                if len(sub) < 2:
                    continue
                if len(sub) > MAX_SUB_BUCKET_SIZE:
                    log.warning(
                        "Skipping sub-bucket of %d files (above %d ceiling) -- "
                        "these will need the fingerprint matcher to find dupes",
                        len(sub), MAX_SUB_BUCKET_SIZE,
                    )
                    continue
                yield sub

    pair_compares = 0
    pair_hits = 0
    aborted_for_budget = False
    for bucket in _all_buckets():
        if aborted_for_budget:
            break
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                pair_compares += 1
                if pair_compares > PAIR_COMPARE_BUDGET:
                    log.warning(
                        "Phase 2 hit %d-compare budget; aborting further "
                        "comparisons. %d pairs matched so far.",
                        PAIR_COMPARE_BUDGET, pair_hits,
                    )
                    aborted_for_budget = True
                    break
                ta, tb = tokens_for[a.id], tokens_for[b.id]
                if jaccard(ta, tb) >= TOKEN_SET_THRESHOLD:
                    union(a.id, b.id)
                    pair_hits += 1
            if aborted_for_budget:
                break

    log.info("Phase 2 (token-set): %d pair compares, %d above threshold%s",
             pair_compares, pair_hits,
             " (aborted at budget)" if aborted_for_budget else "")

    # Collect token-set clusters
    by_root: dict[int, list[FileRow]] = defaultdict(list)
    by_id = {f.id: f for f in singletons}
    for fid in parent:
        root = find(fid)
        by_root[root].append(by_id[fid])
    token_groups = [g for g in by_root.values() if len(g) >= min_group_size]
    log.info("Phase 2 produced %d clusters covering %d files",
             len(token_groups), sum(len(g) for g in token_groups))

    all_groups = exact_groups + token_groups
    if not all_groups:
        return {"candidates": len(rows), "groups": 0, "members": 0}

    # Persist to dup_groups / dup_members
    inserted_members = 0
    with session() as conn, conn.cursor() as cur:
        # Wipe prior filename groups for this media_type -- idempotent rebuild.
        # Scoped to media_type so a double-call (video then audio) doesn't
        # clobber the first pass's results.
        cur.execute(
            "DELETE FROM dup_members WHERE group_id IN "
            "(SELECT id FROM dup_groups WHERE match_type='filename' AND media_type=%s)",
            (media_type,),
        )
        cur.execute(
            "DELETE FROM dup_groups WHERE match_type='filename' AND media_type=%s",
            (media_type,),
        )

        for grp in all_groups:
            grp_sorted = sorted(grp, key=keeper_score, reverse=True)
            keeper = grp_sorted[0]
            # Similarity score: 1.0 for exact-key, jaccard for token-set.
            # Compute by recomputing the smallest pairwise jaccard in the
            # group (worst-case match strength).
            keys = {normalize(f.path) for f in grp}
            if len(keys) == 1:
                sim = 1.0
            else:
                # Token-set group: average pairwise jaccard
                tokens_list = [tokens_for.get(f.id) or content_tokens(f.path)
                               for f in grp]
                pair_sims = []
                for i in range(len(tokens_list)):
                    for j in range(i + 1, len(tokens_list)):
                        pair_sims.append(jaccard(tokens_list[i], tokens_list[j]))
                sim = sum(pair_sims) / len(pair_sims) if pair_sims else 1.0

            # Resolution-only diff is the auto-deletable case. We tag the
            # group via notes column on dup_groups so the dashboard can
            # surface it differently from "names differ in scene/title".
            res_only = resolution_only_differs(grp)
            note = "resolution_only" if res_only else "name_differs"

            cur.execute(
                """INSERT INTO dup_groups
                       (media_type, match_type, similarity, reviewed, notes)
                   VALUES (%s, 'filename', %s, FALSE, %s)
                   RETURNING id""",
                (media_type, float(sim), note),
            )
            gid = cur.fetchone()["id"]

            for f in grp:
                cur.execute(
                    """INSERT INTO dup_members
                           (group_id, file_id, is_keeper, action)
                       VALUES (%s, %s, %s, %s)""",
                    (gid, f.id, f.id == keeper.id,
                     "keep" if f.id == keeper.id else "delete"),
                )
                inserted_members += 1

    log.info("Wrote %d filename groups with %d members total",
             len(all_groups), inserted_members)

    return {
        "candidates": len(rows),
        "groups": len(all_groups),
        "exact_key_groups": len(exact_groups),
        "token_set_groups": len(token_groups),
        "members": inserted_members,
    }
