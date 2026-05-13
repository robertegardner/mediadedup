"""FFmpeg / ffprobe wrappers.

Frame extraction uses NVDEC ('-hwaccel cuda') when available, falling back to
CPU decode if the input codec is unsupported on the GPU. Frames are downloaded
back to system memory and decoded via Pillow for hashing -- the GPU only
accelerates the heavy decode + scale step.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    bitrate: int | None
    sample_rate: int | None
    channels: int | None


def ffprobe(path: str) -> MediaInfo:
    """Return container/stream metadata via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=60)
    except subprocess.CalledProcessError as e:
        raise FFmpegError(f"ffprobe failed: {e.stderr.decode(errors='replace')}") from e
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"ffprobe timed out for {path}") from e

    data = json.loads(out)
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []

    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def _i(d: dict | None, k: str) -> int | None:
        if not d:
            return None
        v = d.get(k)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return MediaInfo(
        duration=float(fmt["duration"]) if fmt.get("duration") else None,
        width=_i(v, "width"),
        height=_i(v, "height"),
        video_codec=v.get("codec_name") if v else None,
        audio_codec=a.get("codec_name") if a else None,
        bitrate=_i(fmt, "bit_rate"),
        sample_rate=_i(a, "sample_rate"),
        channels=_i(a, "channels"),
    )


def extract_frames(
    path: str,
    n_frames: int,
    duration: float | None,
    use_gpu: bool = True,
    target_size: int = 256,
) -> Iterator[Image.Image]:
    """Yield ``n_frames`` PIL images sampled evenly across the file.

    Frames are emitted as PNG-encoded bytes from ffmpeg over stdout. If the
    GPU decode path fails (unsupported codec, hybrid HEVC, etc.) we silently
    retry once on CPU.
    """
    if not duration or duration <= 0:
        # ffprobe couldn't read it -- try a single frame from the start.
        yield from _extract_via_ffmpeg(path, [0.0], use_gpu, target_size)
        return

    # Skip the first and last 5% of the file to avoid black/credits frames.
    pad = max(0.5, duration * 0.05)
    start, end = pad, max(pad + 0.5, duration - pad)
    if n_frames == 1:
        timestamps = [(start + end) / 2]
    else:
        step = (end - start) / (n_frames - 1)
        timestamps = [start + i * step for i in range(n_frames)]

    try:
        yield from _extract_via_ffmpeg(path, timestamps, use_gpu, target_size)
    except FFmpegError:
        if use_gpu:
            log.warning("GPU decode failed for %s, falling back to CPU", path)
            yield from _extract_via_ffmpeg(path, timestamps, False, target_size)
        else:
            raise


def _extract_via_ffmpeg(
    path: str,
    timestamps: list[float],
    use_gpu: bool,
    target_size: int,
) -> Iterator[Image.Image]:
    """Extract requested frames using whichever strategy is faster for the
    file size.

    For SMALL files, a single ffmpeg invocation with the ``select`` filter
    saves N-1 process spawns + container parses. Worth it for files where
    the whole content reads in a fraction of a second.

    For LARGE files, ``select`` is a TRAP: it forces ffmpeg to scan the
    entire file looking for matching presentation timestamps, even though
    the actual frames it wants are at known offsets. We tried this on
    SMB/NFS and it turns 16 cheap ~10 MB keyframe-aligned seeks into one
    full sequential read of the entire file (~4 GB at 80 MB/s = 55 s of
    pure I/O instead of 5 s).

    The per-frame strategy uses ``-ss <ts> -i <path>`` (pre-seek, before
    -i), which is keyframe-aligned demuxer seeking and reads only the
    region near each timestamp. For a 4 GB file with 16 frames, that's
    ~16 × 50 MB of reads vs. one × 4000 MB. Roughly an order of magnitude
    faster on bulk files served over the network.

    The 100 MB cutoff is arbitrary but well above where the per-process
    fixed overhead (~50 ms × 16 = 0.8 s) would dominate.
    """
    if not timestamps:
        return

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0

    use_multi = 0 < size < 100 * 1024 * 1024

    if use_multi:
        try:
            yield from _extract_multi_frame(path, timestamps, use_gpu, target_size)
            return
        except FFmpegError:
            # Fall through to per-frame on failure.
            pass

    for ts in timestamps:
        try:
            img = _extract_single_frame(path, ts, use_gpu, target_size)
            if img is not None:
                yield img
        except FFmpegError:
            continue


def _extract_multi_frame(
    path: str,
    timestamps: list[float],
    use_gpu: bool,
    target_size: int,
) -> Iterator[Image.Image]:
    """One ffmpeg call → N PNGs concatenated on stdout."""
    # Build a select expression: "eq(t,1.0)+eq(t,5.0)+eq(t,10.0)..."
    # ffmpeg sends frames whose presentation timestamp matches any of these.
    # The ±0.05s tolerance handles framerate quantization (a 30fps video has
    # 33ms between frames, so an exact equality on a non-integer ts would
    # match nothing).
    eps = 0.05
    expr = "+".join(
        f"between(t,{ts - eps:.3f},{ts + eps:.3f})" for ts in timestamps
    )
    select_filter = f"select='{expr}'"

    cmd: list[str] = ["ffmpeg", "-v", "error", "-nostdin"]
    if use_gpu:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    cmd += ["-i", path]
    if use_gpu:
        cmd += [
            "-vf",
            f"{select_filter},hwdownload,format=nv12,"
            f"scale={target_size}:{target_size}:force_original_aspect_ratio=decrease",
        ]
    else:
        cmd += [
            "-vf",
            f"{select_filter},"
            f"scale={target_size}:{target_size}:force_original_aspect_ratio=decrease",
        ]
    cmd += [
        "-vsync", "vfr",                # don't pad missed frames
        "-fps_mode", "passthrough",     # ffmpeg 5+ name for the same thing
        "-frames:v", str(len(timestamps) * 2),  # cap output, we'll take what we get
        "-f", "image2pipe", "-c:v", "png",
        "pipe:1",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"multi-frame ffmpeg timed out for {path}") from e

    if proc.returncode != 0 or not proc.stdout:
        raise FFmpegError(
            f"multi-frame ffmpeg rc={proc.returncode} for {path}: "
            f"{proc.stderr.decode(errors='replace')[:400]}"
        )

    # Split the concatenated PNG stream. PNG signature: 89 50 4E 47 0D 0A 1A 0A.
    sig = b"\x89PNG\r\n\x1a\n"
    buf = proc.stdout
    starts = []
    pos = 0
    while True:
        i = buf.find(sig, pos)
        if i < 0:
            break
        starts.append(i)
        pos = i + 1

    if not starts:
        raise FFmpegError("multi-frame ffmpeg produced no PNG signatures")

    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(buf)
        try:
            yield Image.open(io.BytesIO(buf[s:e])).convert("RGB")
        except Exception as ex:
            log.warning("Could not decode frame %d from %s: %s", i, path, ex)
            continue


def probe_gpu() -> tuple[bool, str]:
    """Verify NVDEC is actually functional in this container.

    Returns ``(success, diagnostic_message)``. The message contains either a
    short success summary or the actual ffmpeg stderr from whichever step
    failed -- which is what you need to debug things.

    Strategy: generate a tiny real H.264 file via libx264 (CPU encode), then
    decode it via ``-hwaccel cuda``. This actually exercises libnvcuvid /
    NVDEC, unlike a lavfi-based probe.
    """
    import tempfile

    # Step 1: software-encode a 0.5s 128x128 H.264 clip. libx264 ships in
    # the jrottenberg/ffmpeg base image. If this fails, ffmpeg itself is
    # broken and we have bigger problems than NVDEC.
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        sample = tf.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=c=black:s=128x128:r=10:d=0.5",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             sample],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return False, f"sample encode failed: {r.stderr.strip()}"

        # Step 2: real NVDEC decode + hwdownload roundtrip. We deliberately
        # avoid scale_cuda here because some ffmpeg-with-CUDA builds ship
        # without it (the worker uses CPU swscale for the trivial downscale
        # step), and we want the probe to mirror what the worker actually
        # does.
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin",
             "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
             "-i", sample,
             "-vf", "hwdownload,format=nv12,scale=64:64",
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return True, "NVDEC decode + hwdownload roundtrip succeeded"
        return False, f"NVDEC decode failed (rc={r.returncode}):\n{r.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "ffmpeg probe timed out"
    except FileNotFoundError as e:
        return False, f"binary missing: {e}"
    finally:
        try:
            os.unlink(sample)
        except OSError:
            pass


def _extract_single_frame(
    path: str,
    ts: float,
    use_gpu: bool,
    target_size: int,
) -> Image.Image | None:
    # Accurate seeking trick: pre-seek to ~3s before the target (fast,
    # keyframe-aligned), then post-seek the residual after -i (decoded,
    # exact). Two encodes of the same content with different keyframe
    # spacing will then land on the same wall-clock frame, which is
    # critical for cross-resolution / cross-encoder pHash matching.
    preseek = max(0.0, ts - 3.0)
    postseek = ts - preseek

    cmd: list[str] = ["ffmpeg", "-v", "error", "-nostdin"]
    if use_gpu:
        # NVDEC for the heavy decode. We deliberately do NOT use scale_cuda
        # here -- some otherwise-perfectly-working ffmpeg-with-CUDA builds
        # ship without the scale_cuda filter, and the scaling step is
        # trivially cheap compared to decode. So: decode on GPU, download
        # to system memory in NV12, then let swscale do the (very fast)
        # downscale on CPU.
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    cmd += ["-ss", f"{preseek:.3f}", "-i", path]
    if postseek > 0.001:
        cmd += ["-ss", f"{postseek:.3f}"]
    if use_gpu:
        cmd += [
            "-vf",
            f"hwdownload,format=nv12,"
            f"scale={target_size}:{target_size}:force_original_aspect_ratio=decrease",
        ]
    else:
        cmd += [
            "-vf",
            f"scale={target_size}:{target_size}:force_original_aspect_ratio=decrease",
        ]
    cmd += ["-frames:v", "1", "-f", "image2", "-c:v", "png", "pipe:1"]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"ffmpeg timeout extracting {path} @ {ts:.1f}s") from e

    if proc.returncode != 0 or not proc.stdout:
        raise FFmpegError(
            f"ffmpeg rc={proc.returncode} extracting {path} @ {ts:.1f}s: "
            f"{proc.stderr.decode(errors='replace')[:400]}"
        )
    try:
        return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
    except Exception as e:
        log.warning("Could not decode frame from %s @ %.1fs: %s", path, ts, e)
        return None


def save_thumbnail(path: str, dest: Path, use_gpu: bool, duration: float | None) -> bool:
    """Save a single mid-video thumbnail (for the web UI). Best-effort."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = (duration or 10) / 2
    try:
        img = _extract_single_frame(path, ts, use_gpu, target_size=480)
    except FFmpegError:
        try:
            img = _extract_single_frame(path, ts, False, target_size=480)
        except FFmpegError:
            return False
    if img is None:
        return False
    img.save(dest, format="JPEG", quality=80)
    return True


def chromaprint(path: str) -> tuple[str, float]:
    """Return (compressed_fingerprint, duration_seconds) for an audio file."""
    if shutil.which("fpcalc") is None:
        raise FFmpegError("fpcalc binary not found (libchromaprint-tools)")
    try:
        out = subprocess.check_output(
            ["fpcalc", "-json", path],
            stderr=subprocess.PIPE,
            timeout=180,
        )
    except subprocess.CalledProcessError as e:
        raise FFmpegError(f"fpcalc failed: {e.stderr.decode(errors='replace')}") from e
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(f"fpcalc timed out for {path}") from e
    data = json.loads(out)
    fp = data.get("fingerprint")
    dur = data.get("duration")
    if not fp or not dur:
        raise FFmpegError(f"fpcalc returned no fingerprint for {path}")
    return fp, float(dur)
