"""Diagnostic CLI -- run via:

    docker compose --profile tools run --rm doctor

Reports:
  1. GPU access (nvidia-smi)
  2. ffmpeg's hwaccels & CUDA decoders
  3. Live CUDA decode probe
  4. Database state (file counts, size, fingerprint coverage, recent failures)
  5. Histogram of pHash distances among same-duration video pairs

Use #5 to pick a sensible VIDEO_PHASH_THRESHOLD. If you have known dupes and
the distance histogram shows them at e.g. d=11, raise the threshold to 12.
"""
from __future__ import annotations

import os
import subprocess
from collections import Counter, defaultdict

from .config import CFG
from .db import session
from .ffmpeg_utils import probe_gpu
from .phash import best_match_distance, from_signed64


HR = "─" * 72


def section(title: str) -> None:
    print(f"\n{HR}\n  {title}\n{HR}")


def gpu_check() -> None:
    section("1. nvidia-smi (host GPU visible to container)")
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,utilization.gpu",
             "--format=csv"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            print(r.stdout.strip())
        else:
            print(f"FAILED (rc={r.returncode}):\n{r.stderr}")
            print("\n  → The container does not have GPU access. Verify on the host:")
            print("    docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi")
    except FileNotFoundError:
        print("nvidia-smi not found inside the container.")
        print("→ The NVIDIA Container Toolkit isn't injecting the driver.")
        print("  On the host, run:")
        print("    sudo nvidia-ctk runtime configure --runtime=docker && "
              "sudo systemctl restart docker")
    except subprocess.TimeoutExpired:
        print("nvidia-smi timed out.")

    section("2. ffmpeg -hwaccels (compiled-in hardware accelerators)")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-hwaccels"],
                       capture_output=True, text=True, timeout=10)
    print(r.stdout.strip() or r.stderr.strip())
    has_cuda = "cuda" in r.stdout.lower()
    print(f"\n→ CUDA hwaccel listed: {'YES' if has_cuda else 'NO'}")

    section("3. NVDEC runtime libraries injected by the Container Toolkit")
    # `nvidia-smi` working only proves the `compute` capability is enabled.
    # NVDEC also needs the `video` capability, which exposes libnvcuvid.so.
    # We check for it directly because this is by far the most common cause
    # of "GPU is visible but decode fails".
    libs_to_check = ["libnvcuvid", "libnvidia-encode", "libcuda"]
    try:
        r = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=5)
        ld_output = r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        ld_output = ""
    for lib in libs_to_check:
        present = lib in ld_output
        marker = "✓" if present else "✗"
        print(f"  {marker} {lib}")
    if "libnvcuvid" not in ld_output:
        print("\n  → libnvcuvid is MISSING. NVDEC cannot work without it.")
        print("    The most likely cause: the 'video' driver capability isn't")
        print("    being injected. Compose already sets")
        print("    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility — but the")
        print("    nvidia-container-toolkit on the host may need updating:")
        print("      sudo apt-get update && sudo apt-get install --only-upgrade")
        print("        nvidia-container-toolkit && sudo systemctl restart docker")
        print("    Then recreate the containers:")
        print("      docker compose down && docker compose up -d")

    section("4. CUDA-aware filters (informational)")
    print("  hwdownload is required; scale_cuda is NOT — the worker scales")
    print("  on CPU after hwdownload, which is universally available and")
    print("  trivially cheap compared to NVDEC decode.\n")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                       capture_output=True, text=True, timeout=10)
    for filt, required in [("hwdownload", True), ("hwupload_cuda", False),
                           ("scale_cuda", False), ("scale_npp", False)]:
        present = any(filt in line for line in r.stdout.splitlines())
        if present:
            marker = "✓"
        elif required:
            marker = "✗ REQUIRED — MISSING"
        else:
            marker = "—  (not used)"
        print(f"  {marker:25s} {filt}")

    section("5. Live NVDEC decode probe (real H.264 → CUDA → CPU roundtrip)")
    ok, msg = probe_gpu()
    if ok:
        print(f"PASS — {msg}")
    else:
        print("FAIL\n")
        print("ffmpeg output:")
        for line in msg.splitlines():
            print(f"    {line}")
        print("\n  Common interpretations of the error above:")
        print("  - 'Cannot load libnvcuvid' / 'CUDA_ERROR_NO_DEVICE' / 'No such file':")
        print("      → Toolkit isn't injecting NVDEC libs. See section 3.")
        print("  - 'Driver does not support the required nvenc API version':")
        print("      → Host driver too old for the ffmpeg in the container.")
        print("        Update the host driver (`sudo ubuntu-drivers autoinstall`).")
        print("  - 'Generic error in an external library' or 'Invalid argument':")
        print("      → Often a driver/runtime version mismatch. Update the")
        print("        nvidia-container-toolkit on the host:")
        print("          sudo apt-get update && sudo apt-get install --only-upgrade \\")
        print("            nvidia-container-toolkit && sudo systemctl restart docker")


def db_stats() -> None:
    section("6. Database state")
    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT media_type, status, COUNT(*) AS n, COALESCE(SUM(size),0) AS sz
                 FROM files GROUP BY 1,2 ORDER BY 1,2"""
        )
        rows = cur.fetchall()
        if not rows:
            print("  (no files registered — has the scanner run?)")
        else:
            print(f"  {'media':<6} {'status':<12} {'count':>8}  {'size':>10}")
            for r in rows:
                gb = float(r["sz"]) / 1e9
                print(f"  {r['media_type']:<6} {r['status']:<12} {r['n']:>8}  {gb:>8.2f} GB")

        cur.execute(
            """SELECT COUNT(*) AS n FROM files
                WHERE media_type='video' AND status='done'
                  AND phashes IS NOT NULL AND array_length(phashes,1) > 0"""
        )
        nv = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM files WHERE media_type='audio' AND chromaprint IS NOT NULL"
        )
        na = cur.fetchone()["n"]
        print(f"\n  videos with phashes:    {nv}")
        print(f"  audio with chromaprint: {na}")

        cur.execute(
            "SELECT path, error FROM files WHERE status='failed' "
            "ORDER BY id DESC LIMIT 5"
        )
        fails = cur.fetchall()
        if fails:
            print("\n  Recent failures (last 5):")
            for r in fails:
                err = (r["error"] or "").splitlines()[0][:140]
                print(f"    {r['path']}\n      → {err}")

        cur.execute("SELECT COUNT(*) AS n FROM dup_groups")
        ng = cur.fetchone()["n"]
        print(f"\n  duplicate groups in DB: {ng}")
        if ng == 0 and (nv > 1 or na > 1):
            print("  (have you run `docker compose --profile tools run --rm matcher`?)")


def phash_distribution() -> None:
    section("7. pHash distance distribution among same-duration video pairs")
    print("Use this to pick a sensible VIDEO_PHASH_THRESHOLD.")
    print("Same content at different resolutions typically lands at d ≤ 12.")
    print(f"Current threshold: {CFG.video_phash_threshold} (out of 64)")
    print(f"Current duration tolerance: ±{CFG.video_duration_tolerance}s\n")

    with session() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, path, duration, phashes
                 FROM files
                WHERE media_type='video' AND status='done'
                  AND phashes IS NOT NULL AND array_length(phashes,1) > 0
                ORDER BY duration"""
        )
        rows = cur.fetchall()

    if len(rows) < 2:
        print(f"  Not enough fingerprinted videos ({len(rows)}) to compare.")
        return

    # Postgres stores phashes as signed bigint; reinterpret as unsigned 64-bit.
    for r in rows:
        r["phashes"] = [from_signed64(p) for p in (r["phashes"] or [])]

    by_dur: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_dur[int(round(r["duration"] or 0))].append(r)

    tol = CFG.video_duration_tolerance
    pairs: list[tuple[float, dict, dict]] = []
    seen: set[tuple[int, int]] = set()
    for dk in sorted(by_dur):
        cands: list[dict] = []
        for delta in range(-tol, tol + 1):
            cands.extend(by_dur.get(dk + delta, []))
        for i, a in enumerate(cands):
            for b in cands[i + 1:]:
                key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if key in seen:
                    continue
                seen.add(key)
                d = best_match_distance(a["phashes"], b["phashes"])
                pairs.append((d, a, b))

    if not pairs:
        print("  No video pairs share similar duration — nothing to compare.")
        print("  → Either you only have unique-length videos, or the duration")
        print("    tolerance window is too small. Try raising VIDEO_DURATION_TOLERANCE.")
        return

    pairs.sort(key=lambda x: x[0])
    print(f"  {len(pairs)} candidate pairs analyzed.\n")
    print("  Closest pairs (lowest distance = most similar):")
    for d, a, b in pairs[:10]:
        a_name = os.path.basename(a["path"])
        b_name = os.path.basename(b["path"])
        marker = "✓" if d <= CFG.video_phash_threshold else " "
        print(f"    {marker} d={d:5.2f}  {a_name}")
        print(f"            {b_name}")

    print("\n  Distribution (each '#' ≈ 1 pair):")
    buckets: Counter = Counter()
    for d, _, _ in pairs:
        buckets[int(round(d))] += 1
    max_count = max(buckets.values()) if buckets else 1
    scale = max(1, max_count // 50)
    cumulative = 0
    threshold_marked = False
    for d in range(0, 65):
        n = buckets.get(d, 0)
        cumulative += n
        if not n and d > max(buckets.keys(), default=0):
            break
        bar = "#" * (n // scale)
        marker = ""
        if not threshold_marked and d == CFG.video_phash_threshold:
            marker = "  ← current threshold"
            threshold_marked = True
        print(f"    d={d:2d}  ({n:5d}  cum {cumulative:5d})  {bar}{marker}")


def main() -> None:
    print("Media Dedup — diagnostic report")
    print(f"Config: GPU requested = {CFG.video_use_gpu}, "
          f"phash threshold = {CFG.video_phash_threshold}, "
          f"duration tolerance = ±{CFG.video_duration_tolerance}s")
    gpu_check()
    db_stats()
    phash_distribution()
    print()


if __name__ == "__main__":
    main()
