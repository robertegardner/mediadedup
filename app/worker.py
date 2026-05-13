"""RQ job: fingerprint a single file (called as ``app.worker.process_file``).

A file row is moved through ``pending -> processing -> done|failed``. We
collect:
  * SHA-256 of the raw bytes (cheap and catches exact duplicates)
  * ffprobe-derived metadata
  * For videos: a sequence of perceptual hashes from N evenly-spaced frames,
    decoded on the GPU when possible; plus a single mid-file thumbnail JPEG
    written to the shared `thumbs` volume for the web UI.
  * For audio: a Chromaprint fingerprint via ``fpcalc``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import CFG
from .db import ensure_schema, session
from .ffmpeg_utils import (
    FFmpegError,
    chromaprint,
    extract_frames,
    ffprobe,
    probe_gpu,
    save_thumbnail,
)
from .phash import phash_image, sha256_file, to_signed64

log = logging.getLogger("worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# Apply any in-place schema migrations (idempotent). Safe to run from every
# worker on startup -- ADD COLUMN IF NOT EXISTS is a no-op when the column
# already exists.
try:
    ensure_schema()
except Exception:                                                    # noqa: BLE001
    log.exception("ensure_schema failed -- continuing, but new columns "
                  "may be missing")


# Run a one-shot CUDA probe at worker startup so we know whether NVDEC is
# actually wired up. If the user asked for GPU but it doesn't work, log loudly
# once instead of silently falling back per-file (which hides the problem).
_GPU_OK: bool = False
if CFG.video_use_gpu:
    _GPU_OK, _msg = probe_gpu()
    if _GPU_OK:
        log.info("GPU probe: %s", _msg)
    else:
        log.warning(
            "GPU probe FAILED. Falling back to CPU for all video frame "
            "extraction. Run `docker compose --profile tools run --rm "
            "doctor` for the full diagnostic. ffmpeg said:\n%s",
            _msg,
        )
USE_GPU: bool = CFG.video_use_gpu and _GPU_OK


def _claim(file_id: int) -> dict | None:
    """Atomically move a row to 'processing' and return its current state."""
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE files
               SET status='processing',
                   attempts = attempts + 1,
                   processing_started_at = NOW()
               WHERE id = %s AND status IN ('pending','failed')
               RETURNING id, path, media_type, size""",
            (file_id,),
        )
        return cur.fetchone()


def _set_done(file_id: int, fields: dict) -> None:
    cols = ", ".join(f"{k}=%s" for k in fields)
    vals = list(fields.values())
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE files SET status='done', error=NULL, "
            f"fingerprinted_at=%s, {cols} WHERE id = %s",
            [datetime.now(timezone.utc), *vals, file_id],
        )


def _set_failed(file_id: int, err: str) -> None:
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE files SET status='failed', error=%s WHERE id=%s",
            (err[:2000], file_id),
        )


def _thumb_path(file_id: int) -> Path:
    # Two-level shard so directories don't grow unbounded.
    shard = f"{file_id % 100:02d}"
    return Path(CFG.thumbs_dir) / shard / f"{file_id}.jpg"


class StalledIOError(RuntimeError):
    """Raised when a blocking I/O call exceeds its watchdog timeout.

    We can't actually cancel a kernel-level NFS/SMB read that's stuck in
    iowait -- the syscall keeps blocking until the server replies or the
    mount is reset. But we can stop *waiting* for it: the watchdog thread
    abandons the call so the worker can return failure for this job and
    move on to the next one. The orphaned thread will eventually exit
    when the kernel call returns (or be torn down with the process).
    """


def _with_timeout(fn, timeout_s: float, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` in a daemon thread; raise if it overshoots.

    Use only for I/O that's safe to abandon -- we leak the thread until the
    underlying syscall returns, but the worker becomes available again.
    """
    import threading

    result: dict = {}

    def target() -> None:
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as e:                                   # noqa: BLE001
            result["exc"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise StalledIOError(
            f"I/O call {fn.__name__} exceeded {timeout_s:.0f}s; abandoning"
        )
    if "exc" in result:
        raise result["exc"]
    return result["value"]


# Per-call timeouts. Tuned for an SMB share that occasionally hiccups but
# isn't a black hole: a 10-min wait should accommodate even a 30+ GB SHA at
# very low throughput, while still giving us a way out of a wedged mount.
SHA_TIMEOUT_SECS = int(os.environ.get("SHA_TIMEOUT_SECS", 600))
STAT_TIMEOUT_SECS = int(os.environ.get("STAT_TIMEOUT_SECS", 30))


def process_file(file_id: int) -> str:
    row = _claim(file_id)
    if not row:
        return "skipped"
    path = row["path"]
    media_type = row["media_type"]
    log.info("Processing #%s [%s] %s", file_id, media_type, path)

    try:
        # Stat can hang on a stale share; wrap it.
        try:
            exists = _with_timeout(
                lambda: Path(path).exists(), STAT_TIMEOUT_SECS,
            )
        except StalledIOError as e:
            log.warning("Stat hung for %s: %s", path, e)
            _set_failed(file_id, str(e))
            return "failed"

        if not exists:
            with session() as conn, conn.cursor() as cur:
                cur.execute("UPDATE files SET status='missing' WHERE id=%s", (file_id,))
            return "missing"

        # SHA can hang or take very long on bulk reads; wrap with watchdog.
        try:
            sha = _with_timeout(sha256_file, SHA_TIMEOUT_SECS, path)
        except StalledIOError as e:
            log.warning("SHA stalled for %s: %s", path, e)
            _set_failed(file_id, str(e))
            return "failed"

        info = ffprobe(path)

        common: dict = {
            "sha256": sha,
            "duration": info.duration,
            "bitrate": info.bitrate,
        }

        if media_type == "video":
            common.update({
                "width": info.width,
                "height": info.height,
                "video_codec": info.video_codec,
                "audio_codec": info.audio_codec,
            })
            phashes: list[int] = []
            for img in extract_frames(
                path,
                n_frames=CFG.video_phash_frames,
                duration=info.duration,
                use_gpu=USE_GPU,
            ):
                phashes.append(phash_image(img))
            # Postgres bigint is signed; phashes are unsigned 64-bit, so
            # convert at the storage boundary. See app/phash.py.
            common["phashes"] = (
                [to_signed64(p) for p in phashes] if phashes else None
            )

            # Best-effort thumbnail for the UI.
            try:
                save_thumbnail(path, _thumb_path(file_id), USE_GPU, info.duration)
            except Exception as e:                                    # noqa: BLE001
                log.warning("thumbnail failed for %s: %s", path, e)

        else:  # audio
            common.update({
                "audio_codec": info.audio_codec,
                "sample_rate": info.sample_rate,
                "channels": info.channels,
            })
            fp, dur = chromaprint(path)
            common["chromaprint"] = fp
            common["chromaprint_dur"] = dur

        _set_done(file_id, common)
        return "ok"

    except FFmpegError as e:
        log.warning("ffmpeg failure on %s: %s", path, e)
        _set_failed(file_id, str(e))
        return "failed"
    except Exception as e:                                            # noqa: BLE001
        log.exception("Unhandled error processing %s", path)
        _set_failed(file_id, f"{e!r}\n{traceback.format_exc()}")
        return "failed"
