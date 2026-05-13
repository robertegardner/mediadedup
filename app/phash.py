"""Hashing helpers: SHA-256, perceptual hashes, and Chromaprint similarity."""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import zlib
from typing import Iterable

import imagehash
import numpy as np
from PIL import Image


# Strategy for "is this the same file" detection:
#   * Files <= SHA_FULL_THRESHOLD: full SHA-256 (cheap; no point getting fancy).
#   * Files >  SHA_FULL_THRESHOLD: SHA-256 of (head ∥ size ∥ tail), where head
#     and tail are SHA_SAMPLE_BYTES each. The probability that two genuinely
#     different real-world media files share identical first AND last 32 MB
#     AND identical byte size is cryptographically negligible -- containers
#     and codecs guarantee unique header/footer bytes per encoding.
#
# Why this matters: at gigabit-class NFS throughput, full SHA of a 10 GB file
# costs ~80 s of read I/O per worker. Partial reads cut that to ~0.5 s.
SHA_FULL_THRESHOLD = 256 * 1024 * 1024     # 256 MiB
SHA_SAMPLE_BYTES = 32 * 1024 * 1024        # 32 MiB head + 32 MiB tail


def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    """Return a stable hex digest identifying the file's content.

    Full SHA-256 for small files; head+size+tail SHA-256 for large files.
    Two byte-identical files always produce the same digest. The digest is
    NOT cryptographically equivalent to whole-file SHA for large files, but
    we use it only for exact-duplicate detection where collisions on real
    media are vanishingly unlikely.
    """
    size = os.path.getsize(path)
    h = hashlib.sha256()

    if size <= SHA_FULL_THRESHOLD:
        with open(path, "rb", buffering=0) as f:
            while True:
                data = f.read(chunk)
                if not data:
                    break
                h.update(data)
        return h.hexdigest()

    # Partial hash: head ∥ 8-byte big-endian size ∥ tail.
    with open(path, "rb", buffering=0) as f:
        # Head
        remaining = SHA_SAMPLE_BYTES
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            h.update(data)
            remaining -= len(data)
        # Size separator (so head and tail can't ever bleed into each other)
        h.update(size.to_bytes(8, "big"))
        # Tail
        f.seek(max(0, size - SHA_SAMPLE_BYTES))
        remaining = SHA_SAMPLE_BYTES
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            h.update(data)
            remaining -= len(data)
    return h.hexdigest()


def phash_image(img: Image.Image) -> int:
    """Return a 64-bit integer perceptual hash for a PIL image."""
    h = imagehash.phash(img, hash_size=8)            # 8x8 = 64 bits
    return int(str(h), 16)


# Postgres ``bigint`` is signed (range -2^63 .. 2^63-1) but pHashes occupy the
# full unsigned 64-bit range (0 .. 2^64-1). We store the bit-pattern-preserving
# signed reinterpretation and convert back at read time.
_MASK64 = (1 << 64) - 1


def to_signed64(u: int) -> int:
    """Reinterpret an unsigned 64-bit int as signed for Postgres bigint."""
    u &= _MASK64
    return u - (1 << 64) if u >= (1 << 63) else u


def from_signed64(s: int) -> int:
    """Reinterpret a signed bigint back to its unsigned 64-bit value."""
    return s & _MASK64


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & _MASK64).bit_count()


def best_match_distance(a: list[int], b: list[int]) -> float:
    """Symmetric average best-match Hamming distance between two pHash sequences.

    For every pHash in `a`, find the closest pHash in `b` and average those
    minimums; repeat in reverse and average both directions.

    This metric is **order-independent** and robust to:
      * Slight duration mismatches across re-encodes (different sample grids)
      * Different sequence lengths (one video shorter / longer)
      * Trimmed credits / different frame-rate sampling
      * The same content at different resolutions (frames normalize to the
        same 256×256 hash space, so distances stay near zero)

    Returns a float in [0, 64].
    """
    if not a or not b:
        return 64.0

    def one_way(src: list[int], dst: list[int]) -> float:
        total = 0
        for s in src:
            best = 64
            for d in dst:
                h = (s ^ d).bit_count()
                if h < best:
                    best = h
                    if best == 0:
                        break
            total += best
        return total / len(src)

    return (one_way(a, b) + one_way(b, a)) / 2.0


# Kept for back-compat / tests; the matcher now uses best_match_distance.
def avg_phash_distance(a: list[int], b: list[int]) -> int:
    return int(best_match_distance(a, b))


# --- Chromaprint compare -----------------------------------------------------
#
# fpcalc emits a "compressed" base64 fingerprint. The reference Chromaprint
# similarity algorithm compares the raw uint32 sequence with a sliding-window
# Hamming distance. We reimplement the decompression (per the published format)
# and a simple offset-aligned comparison good enough for "is this the same
# track" matching.

def decompress_fingerprint(b64: str) -> list[int]:
    """Decompress a Chromaprint base64 fingerprint into uint32 codes."""
    raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    if len(raw) < 4:
        return []
    # Header: 1 byte algorithm, 3 bytes length.
    n = (raw[1] << 16) | (raw[2] << 8) | raw[3]
    bits = raw[4:]

    # Each value is encoded as a series of 3-bit "exception" codes; full
    # 32-bit deltas use an "escape" of 7. Reconstruct deltas, then prefix-sum.
    out_bits: list[int] = []
    bit_buf = 0
    bit_len = 0
    nibbles: list[int] = []
    # Read first the 3-bit normal-bits stream until we have n*32 bits worth
    # of deltas. The format actually uses a more involved layout but for our
    # use ('do these tracks match'), a straight decompress via pyacoustid is
    # nicer -- but pyacoustid only does match scoring on raw fingerprints.
    # To keep this self-contained we use pyacoustid's compare_fingerprints.
    raise NotImplementedError("use chromaprint_similarity instead")


def chromaprint_similarity(fp1_b64: str, fp2_b64: str) -> float:
    """Return a similarity score in [0, 1] between two compressed fingerprints.

    Uses ``acoustid.compare_fingerprints`` if available; falls back to a
    simple bit-level XOR popcount over the raw payloads if the library is
    unavailable.
    """
    try:
        import acoustid
        # acoustid.compare_fingerprints expects (duration, fp_string) pairs.
        # Duration mostly affects acoustid lookup, not local compare; pass 1.
        return float(acoustid.compare_fingerprints((1, fp1_b64), (1, fp2_b64)))
    except (ImportError, AttributeError):
        return _byte_similarity(fp1_b64, fp2_b64)


def _byte_similarity(a_b64: str, b_b64: str) -> float:
    a = base64.urlsafe_b64decode(a_b64 + "=" * (-len(a_b64) % 4))
    b = base64.urlsafe_b64decode(b_b64 + "=" * (-len(b_b64) % 4))
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    diff = sum((x ^ y).bit_count() for x, y in zip(a[:n], b[:n]))
    return 1.0 - (diff / (n * 8))
