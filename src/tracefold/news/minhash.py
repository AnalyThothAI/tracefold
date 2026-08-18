"""Deterministic MinHash + LSH banding for near-duplicate Event lookup."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from typing import Final

MINHASH_VERSION: Final = "news_minhash_v1"
NUM_PERMUTATIONS: Final = 128
BANDS: Final = 32  # 32 bands x 4 rows: P(candidate | J=0.55) ~= 0.95, P(J=0.30) ~= 0.23 (filtered by exact Jaccard)
ROWS: Final = NUM_PERMUTATIONS // BANDS
_MERSENNE: Final = (1 << 61) - 1
_MAX_HASH: Final = (1 << 32) - 1
_SEED: Final = 0x5EED_2026


def _params() -> tuple[tuple[int, int], ...]:
    out = []
    state = _SEED
    for _ in range(NUM_PERMUTATIONS):
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        a = (state >> 3) % _MERSENNE or 1
        state = (state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        b = (state >> 3) % _MERSENNE
        out.append((a, b))
    return tuple(out)


_PARAMS = _params()


def _token_hash(token: str) -> int:
    return int(struct.unpack("<I", hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest())[0])


def minhash_signature(tokens: Iterable[str]) -> tuple[int, ...]:
    hashes = [_token_hash(t) for t in tokens]
    if not hashes:
        return tuple([_MAX_HASH] * NUM_PERMUTATIONS)
    signature = []
    for a, b in _PARAMS:
        signature.append(min(((a * h + b) % _MERSENNE) & _MAX_HASH for h in hashes))
    return tuple(signature)


def band_keys(signature: tuple[int, ...]) -> tuple[str, ...]:
    """Return one hex key per band; equal keys imply a probable near-duplicate."""

    keys = []
    for band in range(BANDS):
        chunk = signature[band * ROWS : (band + 1) * ROWS]
        digest = hashlib.blake2b(struct.pack(f"<{ROWS}I", *chunk), digest_size=8).hexdigest()
        keys.append(digest)
    return tuple(keys)


def estimate_jaccard(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
    if len(sig_a) != len(sig_b) or not sig_a:
        return 0.0
    return sum(1 for a, b in zip(sig_a, sig_b, strict=True) if a == b) / len(sig_a)


__all__ = ["BANDS", "MINHASH_VERSION", "NUM_PERMUTATIONS", "ROWS", "band_keys", "estimate_jaccard", "minhash_signature"]
