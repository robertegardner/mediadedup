"""LLM-based filename clustering.

Runs as a third matching pass after the deterministic filename matcher and
the fingerprint matcher. Targets files that:
  * Have status 'done' or 'pending'
  * Are NOT already a member of any dup_group
  * Live alongside other unmatched files in the same parent directory

For each parent-directory bucket, sends the filenames to an Ollama-hosted LLM
with a prompt asking the model to group filenames that represent the same
underlying content. Parses the JSON response and writes groups with
match_type='llm'.

Design choices:

* The LLM never sees file contents, only filenames. Cheap per call.
* Batches are bounded (default 30 filenames per call) so the model doesn't
  have to keep too much in its working memory. Larger buckets are split
  into multiple calls and merged via union-find.
* Every LLM call's prompt + response is logged to ``llm_match_log`` so the
  clustering decisions are auditable. If a clustering decision is wrong, you
  can grep the log to find the prompt.
* Auto-delete defaults OFF for LLM matches -- they're surfaced for review
  unless you flip the setting. Higher risk of false positives than the
  deterministic matchers.
* The Ollama endpoint, model name, batch size, and aggressiveness are all
  in the ``settings`` table -- configurable from the web UI without
  rebuilding the image.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import httpx

from .db import session
from . import settings as settings_mod

log = logging.getLogger("llm_match")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: dict[str, str] = {
    "llm.ollama_url": "http://localhost:11434",
    "llm.model": "qwen3:8b",
    "llm.batch_size": "30",
    "llm.timeout_seconds": "120",
    "llm.bucket_by": "parent_dir",   # 'parent_dir' | 'grandparent_dir' | 'all'
    "llm.min_bucket_size": "2",      # don't bother LLM-calling solo files
    "llm.max_buckets": "200",        # safety cap so we don't run all night
    "llm.confidence_threshold": "0.80",  # discard groups the LLM rated below this
}


def _get_setting(key: str) -> str:
    val = settings_mod.get(key)
    if val is None or val == "":
        return DEFAULT_SETTINGS.get(key, "")
    return val


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a media library deduplication assistant.

You will be given a numbered list of video filenames. Two files belong in the
same duplicate group ONLY IF they represent THE SAME SINGLE VIDEO that has been
downloaded more than once with different filenames.

Two files are duplicates when they share:
- Same source/studio AND same shoot date AND same scene title
- Same content but different resolution markers (1080p vs 720p)
- Same content but different release group tags (-WRB vs -P2P)
- Same content but different container (.mp4 vs .mkv)
- Same content but trivial filename variations (case, separators, sequential
  suffix like " (1)" or "_1")

CRITICAL: Files are NOT duplicates merely because they share:
- The same performer name (a performer has many different videos)
- The same studio or source (a studio releases many scenes)
- The same series or "pack" name (a "MegaPACK" or "SiteRip" contains hundreds
  of DIFFERENT videos, often numbered sequentially like 00001, 00002, 00003)
- The same naming PREFIX (the prefix identifies the COLLECTION, not the
  specific video)
- Different dates with the same performer (different shoot dates = different
  scenes, NOT duplicates)

NEGATIVE EXAMPLES — these are NOT duplicates, do NOT group them:
  "PerformerPack_00001.mp4", "PerformerPack_00002.mp4", "PerformerPack_00003.mp4"
  -> These are three different videos in a collection. NOT duplicates.

  "performer - 2024-01-15 - Some scene.mp4", "performer - 2024-02-20 - Some scene.mp4"
  -> Same performer, different shoot dates. NOT duplicates.

  "Studio.25.03.12.Scene.A.mp4", "Studio.25.03.13.Scene.B.mp4"
  -> Same studio, different dates and scenes. NOT duplicates.

POSITIVE EXAMPLES — these ARE duplicates, group them:
  "Studio.25.03.12.Foo.XXX.1080p-WRB.mp4", "Studio.25.03.12.Foo.XXX.720p-P2P.mp4"
  -> Same studio, same date, same scene, different resolution and release group. DUPLICATES.

  "DungeonSex.25.07.25.Bambi.Blitz.XXX.1080p.MP4-FETiSH.mp4", "dungeonsex.25.07.25.bambi.blitz.1.mp4"
  -> Same studio, same date, same performer, lowercase variant. DUPLICATES.

When in doubt, DO NOT group. False positives delete unique content. It is
far better to miss a duplicate than to incorrectly mark distinct scenes as the
same.

Respond with ONLY a JSON object of this exact shape, no commentary:

{
  "groups": [
    {
      "members": [<list of integer file numbers from the input>],
      "confidence": <float 0.0 to 1.0>,
      "reason": "<one short sentence>"
    }
  ]
}

Only include groups with 2 or more members. Files that don't belong in any
group must be omitted entirely. Use confidence >= 0.90 only when you can
identify the same shoot date AND same scene title. Otherwise omit the group.
If no duplicates are found, return: {"groups": []}"""


def _build_prompt(filenames: list[str]) -> str:
    lines = [f"{i + 1}. {name}" for i, name in enumerate(filenames)]
    return "Filenames to analyze:\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema for audit log
# ---------------------------------------------------------------------------

_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS llm_match_log (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model        TEXT,
    bucket_key   TEXT,
    n_files      INTEGER,
    prompt       TEXT,
    response     TEXT,
    error        TEXT,
    elapsed_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_log_created ON llm_match_log (created_at DESC);
"""


def ensure_schema() -> None:
    with session() as conn, conn.cursor() as cur:
        cur.execute(_AUDIT_TABLE)


def _log_call(model: str, bucket_key: str, prompt: str, response: str | None,
              error: str | None, elapsed_ms: int, n_files: int) -> None:
    try:
        with session() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO llm_match_log
                       (model, bucket_key, n_files, prompt, response, error, elapsed_ms)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (model, bucket_key, n_files,
                 prompt[:50000], (response or "")[:50000],
                 error, elapsed_ms),
            )
    except Exception:                                                # noqa: BLE001
        log.exception("Could not write llm_match_log row")


# ---------------------------------------------------------------------------
# Ollama API call
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict | None:
    """Lift the first JSON object out of an LLM response.

    Models sometimes wrap JSON in markdown fences, sometimes prefix with
    'Here is the result:', sometimes append commentary. Be lenient:
    find the first { ... } that parses.
    """
    if not text:
        return None

    # Strip leading comment lines (we prepend "# ollama_meta: ..." for the
    # audit log, but those lines are not JSON). Drop any line starting with '#'.
    text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )

    # Strip <think>...</think> or <thinking>...</thinking> blocks that some
    # reasoning-style models emit even when asked for JSON.
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>",
                  "", text, flags=re.DOTALL | re.IGNORECASE)

    # Strip markdown code fences.
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)

    # Try parsing the whole thing.
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass

    # Find the outermost balanced { ... } block.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _call_ollama(filenames: list[str], bucket_key: str) -> list[dict]:
    """Call Ollama, return the parsed groups list. Returns [] on error.

    Each returned dict has shape:
       {"members": [int, ...], "confidence": float, "reason": str}
    where members are 1-indexed into the input filenames list.

    Uses Ollama's JSON-schema structured-output mode so the model is
    constrained to producing valid JSON of the expected shape -- this is
    much more reliable than ``format: "json"`` (which only guarantees
    well-formed JSON but says nothing about its shape).
    """
    url = _get_setting("llm.ollama_url").rstrip("/") + "/api/chat"
    model = _get_setting("llm.model")
    timeout = float(_get_setting("llm.timeout_seconds") or "120")

    user_prompt = _build_prompt(filenames)

    # JSON schema describing what we want back. Ollama (>=0.5) and most
    # backing models honor this when passed as ``format``. Older models
    # fall through to the lenient JSON extraction in _extract_json.
    response_schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "members": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0, "maximum": 1.0,
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["members", "confidence", "reason"],
                },
            },
        },
        "required": ["groups"],
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "format": response_schema,
        "stream": False,
        "options": {
            "temperature": 0.0,   # deterministic for repeatable results
            "num_predict": 2048,  # response is small; cap keeps things fast
            "num_ctx": 8192,      # plenty of headroom for 30 long filenames
        },
        # Server-side timeout. Independent of our HTTP timeout. Keeps the
        # model from hanging Ollama for hours if it gets stuck.
        "keep_alive": "10m",
    }

    t0 = time.monotonic()
    response_text: str | None = None
    error_text: str | None = None
    metadata: dict = {}

    # One retry on empty response -- sometimes a model hiccups on the first
    # call after a context shift. Second call usually succeeds.
    attempts = 0
    while attempts < 2:
        attempts += 1
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            response_text = (data.get("message") or {}).get("content") or ""
            # Ollama metadata is useful for debugging "why empty?". Keys
            # commonly present: done_reason, total_duration, eval_count,
            # prompt_eval_count.
            metadata = {
                k: data.get(k)
                for k in ("done_reason", "eval_count",
                          "prompt_eval_count", "total_duration")
                if k in data
            }
            if response_text.strip():
                break  # got something useful
            log.warning(
                "Ollama returned empty response on attempt %d (bucket=%r) "
                "metadata=%s; retrying once",
                attempts, bucket_key, metadata,
            )
        except httpx.HTTPError as e:
            error_text = f"HTTP error: {e}"
            log.warning("Ollama call failed (attempt %d): %s", attempts, e)
            break  # don't retry HTTP errors
        except Exception as e:                                       # noqa: BLE001
            error_text = f"unexpected: {e}"
            log.exception("Ollama call failed unexpectedly")
            break

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    # Append metadata as a comment line so the audit log captures it
    # without changing the schema. The parsing code ignores anything
    # before the first '{'.
    audit_response = response_text or ""
    if metadata and not error_text:
        audit_response = f"# ollama_meta: {metadata}\n{audit_response}"
    _log_call(model, bucket_key, user_prompt, audit_response,
              error_text, elapsed_ms, len(filenames))

    if error_text or not response_text:
        return []

    parsed = _extract_json(response_text)
    if not parsed or not isinstance(parsed.get("groups"), list):
        log.warning("Ollama response not parseable (bucket=%s): %s",
                    bucket_key, response_text[:200])
        return []

    return parsed["groups"]


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

@dataclass
class FileRow:
    id: int
    path: str
    size: int
    source_priority: int
    width: int
    height: int
    bitrate: int


def _bucket_files(files: list[FileRow], strategy: str) -> dict[str, list[FileRow]]:
    """Group files into buckets the LLM will be asked about together.

    strategies:
      'parent_dir'     - immediate parent directory
      'grandparent_dir' - one level up (handles per-scene subdirs)
      'all'            - everything in one bucket (only sane for small libraries)
    """
    out: dict[str, list[FileRow]] = defaultdict(list)
    for f in files:
        if strategy == "all":
            key = "_all_"
        elif strategy == "grandparent_dir":
            key = os.path.dirname(os.path.dirname(f.path)) or "/"
        else:
            key = os.path.dirname(f.path) or "/"
        out[key].append(f)
    return out


# ---------------------------------------------------------------------------
# Keeper scoring (same logic as filename_match, for consistency)
# ---------------------------------------------------------------------------

def _keeper_score(f: FileRow) -> tuple:
    return (
        f.source_priority,
        f.width * f.height,
        f.bitrate,
        f.size,
        -f.id,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def find_llm_matches(media_type: str = "video") -> dict:
    """Run the LLM clustering pass.

    Returns a stats dict for the orchestrator.
    Idempotent: wipes existing match_type='llm' groups before rebuilding.
    """
    ensure_schema()

    model = _get_setting("llm.model")
    batch_size = int(_get_setting("llm.batch_size") or "30")
    bucket_by = _get_setting("llm.bucket_by") or "parent_dir"
    min_bucket = int(_get_setting("llm.min_bucket_size") or "2")
    max_buckets = int(_get_setting("llm.max_buckets") or "200")
    conf_threshold = float(_get_setting("llm.confidence_threshold") or "0.80")

    log.info("LLM matcher: model=%s batch_size=%d bucket_by=%s",
             model, batch_size, bucket_by)

    # Find candidate files: fingerprinted or queued, NOT already in any group.
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT f.id, f.path, f.size,
                      COALESCE(s.priority, -1) AS source_priority,
                      COALESCE(f.width, 0) AS width,
                      COALESCE(f.height, 0) AS height,
                      COALESCE(f.bitrate, 0) AS bitrate
                 FROM files f
                 LEFT JOIN sources s ON s.id = f.source_id
                WHERE f.media_type = %s
                  AND f.status IN ('done', 'pending', 'processing')
                  AND NOT EXISTS (
                        SELECT 1 FROM dup_members m
                         WHERE m.file_id = f.id)""",
            (media_type,),
        )
        rows = cur.fetchall()

    log.info("LLM matcher: %d candidate files (not in any existing group)", len(rows))
    if not rows:
        return {"candidates": 0, "groups": 0, "members": 0,
                "buckets_examined": 0, "llm_calls": 0}

    files = [FileRow(r["id"], r["path"], r["size"], r["source_priority"],
                     r["width"], r["height"], r["bitrate"]) for r in rows]

    buckets = _bucket_files(files, bucket_by)
    buckets_to_call = {k: v for k, v in buckets.items() if len(v) >= min_bucket}
    log.info("LLM matcher: %d total buckets, %d above min_bucket=%d",
             len(buckets), len(buckets_to_call), min_bucket)

    if len(buckets_to_call) > max_buckets:
        log.warning("LLM matcher: %d buckets exceeds max_buckets=%d, truncating "
                    "(largest buckets first)",
                    len(buckets_to_call), max_buckets)
        sorted_keys = sorted(buckets_to_call,
                             key=lambda k: len(buckets_to_call[k]),
                             reverse=True)[:max_buckets]
        buckets_to_call = {k: buckets_to_call[k] for k in sorted_keys}

    # Build groupings via union-find across all buckets + batch calls.
    parent: dict[int, int] = {f.id: f.id for f in files}
    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Track per-cluster confidence for the similarity column.
    confidence_for: dict[int, float] = {}

    llm_call_count = 0
    by_id: dict[int, FileRow] = {f.id: f for f in files}

    for bucket_key, bucket_files in buckets_to_call.items():
        # Split into batches of batch_size
        for i in range(0, len(bucket_files), batch_size):
            batch = bucket_files[i:i + batch_size]
            llm_call_count += 1
            # Send just the basenames to keep tokens down -- the LLM doesn't
            # need the full path, and basenames are typically more
            # information-dense per token.
            basenames = [os.path.basename(f.path) for f in batch]
            groups = _call_ollama(basenames, bucket_key)

            for grp in groups:
                members = grp.get("members") or []
                confidence = float(grp.get("confidence") or 0.0)
                if confidence < conf_threshold:
                    continue
                if len(members) < 2:
                    continue
                # Sanity cap: legitimate duplicate groups are typically 2-5
                # files. A group >50% of the batch is almost always the LLM
                # being fooled by a shared naming prefix (MegaPACK pattern).
                # We also hard-cap at 8 files regardless of batch size, since
                # finding a real 8-way duplicate is unusual and the cost of a
                # false positive (deleting 7 unique files) is severe.
                size_ratio = len(members) / max(len(batch), 1)
                if size_ratio > 0.50 or len(members) > 8:
                    log.warning(
                        "Skipping suspicious LLM group: %d/%d files of batch (%.0f%%) "
                        "in bucket=%r reason=%r",
                        len(members), len(batch), size_ratio * 100,
                        bucket_key, grp.get("reason", "")[:120],
                    )
                    continue
                # Map 1-indexed batch position -> file id
                valid_ids: list[int] = []
                for m in members:
                    try:
                        pos = int(m) - 1
                    except (TypeError, ValueError):
                        continue
                    if 0 <= pos < len(batch):
                        valid_ids.append(batch[pos].id)
                if len(valid_ids) < 2:
                    continue
                first = valid_ids[0]
                for other in valid_ids[1:]:
                    union(first, other)
                root = find(first)
                # Keep the highest confidence we've seen for this cluster
                confidence_for[root] = max(confidence_for.get(root, 0.0),
                                           confidence)

    # Collect non-trivial clusters
    clusters: dict[int, list[int]] = defaultdict(list)
    for fid in parent:
        clusters[find(fid)].append(fid)
    clusters = {root: ids for root, ids in clusters.items() if len(ids) >= 2}
    log.info("LLM matcher: built %d clusters from %d LLM calls",
             len(clusters), llm_call_count)

    # Persist
    inserted_members = 0
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM dup_members WHERE group_id IN "
            "(SELECT id FROM dup_groups WHERE match_type='llm')"
        )
        cur.execute("DELETE FROM dup_groups WHERE match_type='llm'")

        for root, ids in clusters.items():
            members = [by_id[fid] for fid in ids]
            keeper = max(members, key=_keeper_score)
            conf = confidence_for.get(root, 0.0)
            cur.execute(
                """INSERT INTO dup_groups
                       (media_type, match_type, similarity, reviewed, notes)
                   VALUES (%s, 'llm', %s, FALSE, %s)
                   RETURNING id""",
                (media_type, conf, "llm_clustered"),
            )
            gid = cur.fetchone()["id"]
            for m in members:
                cur.execute(
                    """INSERT INTO dup_members
                           (group_id, file_id, is_keeper, action)
                       VALUES (%s, %s, %s, %s)""",
                    (gid, m.id, m.id == keeper.id,
                     "keep" if m.id == keeper.id else "delete"),
                )
                inserted_members += 1

    return {
        "candidates": len(rows),
        "buckets_examined": len(buckets_to_call),
        "llm_calls": llm_call_count,
        "groups": len(clusters),
        "members": inserted_members,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Health check from the web UI ("Test connection")
# ---------------------------------------------------------------------------

def check_ollama(override_url: str | None = None) -> dict:
    """Probe an Ollama endpoint and return status.

    Used by the settings UI's "Test connection" button. If ``override_url``
    is supplied (typically from the form before the user has clicked Save),
    probe that URL instead of the stored setting -- this lets the user test
    a new URL without having to save first and reload.
    """
    url = (override_url or _get_setting("llm.ollama_url") or "").rstrip("/")
    model = _get_setting("llm.model")
    out: dict = {"url": url, "model": model}
    if not url:
        out["reachable"] = False
        out["error"] = "no URL configured"
        return out
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{url}/api/tags")
            r.raise_for_status()
            data = r.json()
            available = [m.get("name", "") for m in (data.get("models") or [])]
            out["reachable"] = True
            out["available_models"] = available
            out["model_present"] = any(
                m == model or m.startswith(model + ":") for m in available
            )
    except Exception as e:                                           # noqa: BLE001
        out["reachable"] = False
        out["error"] = str(e)
    return out
