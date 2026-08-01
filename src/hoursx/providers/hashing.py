"""Deterministic feature-hashing embeddings.

A dependency-free embedding backend: each token is hashed into a fixed-width
vector with a signed contribution, then L2-normalized. Quality is far below a
learned model, but it is deterministic, offline, and cheap — the default for
dev/tests and a functional floor for air-gapped deployments. Production points
the ``embed`` alias at a real provider instead.
"""

from __future__ import annotations

import hashlib
import math
import re

DIMENSIONS = 256

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


def hash_embedding(text: str, dimensions: int = DIMENSIONS) -> list[float]:
    """Embed *text* into a normalized ``dimensions``-wide vector."""
    vector = [0.0] * dimensions
    for token in _TOKEN_RE.findall(text.lower()):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        slot = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[slot] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity for equal-length vectors (0.0 on mismatch)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
