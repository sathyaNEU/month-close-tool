"""Counter-based randomness: every draw is a pure function of the seed and a stable key.

A :class:`Stream` is Python's Mersenne Twister seeded from SHA-256 of ``(seed, *key)``, one per entity and
day (or per other stable key), so no stream is ever consumed in partition or thread order. Only
``random()`` is used underneath (its sequence for a given seed is stable across Python versions, unlike
``choice`` and ``randrange``), and every helper here is built on it.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def key_bytes(seed: int, *key: object) -> bytes:
    return hashlib.sha256("|".join([str(seed), *(str(k) for k in key)]).encode("utf-8")).digest()


def rank(seed: int, *key: object) -> str:
    """A stable pseudo-random sort key (hex SHA-256), used to pick exact break quotas."""
    return key_bytes(seed, *key).hex()


class Stream:
    def __init__(self, seed: int, *key: object) -> None:
        self._r = random.Random(int.from_bytes(key_bytes(seed, *key)[:16], "big"))  # noqa: S311  # nosec B311 (test data)

    def uniform(self) -> float:
        return self._r.random()  # spark-ban: allow SB001 seeded Python stream, not Spark

    def below(self, n: int) -> int:
        """An integer in [0, n)."""
        return min(int(self._r.random() * n), n - 1)  # spark-ban: allow SB001 seeded Python stream, not Spark

    def between(self, low: int, high: int) -> int:
        """An integer in [low, high], inclusive."""
        return low + self.below(high - low + 1)

    def chance(self, p: float) -> bool:
        return self._r.random() < p  # spark-ban: allow SB001 seeded Python stream, not Spark

    def pick(self, items: Sequence[T]) -> T:
        return items[self.below(len(items))]

    def weighted(self, items: Sequence[T], weights: Sequence[float]) -> T:
        total = sum(weights)
        x = self._r.random() * total  # spark-ban: allow SB001 seeded Python stream, not Spark
        running = 0.0
        for item, weight in zip(items, weights, strict=True):
            running += weight
            if x < running:
                return item
        return items[-1]

    def lognormal(self, mu: float, sigma: float) -> float:
        # Box-Muller from two uniforms, so only random() is consumed.
        u1 = max(self._r.random(), 1e-12)  # spark-ban: allow SB001 seeded Python stream, not Spark
        u2 = self._r.random()  # spark-ban: allow SB001 seeded Python stream, not Spark
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        return math.exp(mu + sigma * z)

    def token(self, length: int = 16) -> str:
        """Upper-case letters and digits, for opaque provider references."""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(alphabet[self.below(len(alphabet))] for _ in range(length))
