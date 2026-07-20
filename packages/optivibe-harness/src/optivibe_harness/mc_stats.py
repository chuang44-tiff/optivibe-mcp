"""mc_stats.py — Monte-Carlo / trial statistics (stdlib only).

Wilson score confidence interval for a success rate, plus percentile and
percentile-rank helpers used to summarize trial outcomes. Uses ``statistics``
and ``math`` only — no numpy.

Live ZOS-API integration: N/A this tier (pure numeric helpers; no backend).
"""
import math
from dataclasses import dataclass
from statistics import NormalDist


@dataclass(frozen=True)
class WilsonInterval:
    """A Wilson score interval for a binomial success rate."""

    lower: float
    upper: float
    point: float
    n: int
    confidence: float


@dataclass(frozen=True)
class TrialSummary:
    """Summary of a batch of trials: count, successes, rate, and its interval."""

    n: int
    successes: int
    rate: float
    ci: WilsonInterval


def _clamp01(x: float) -> float:
    """Clamp ``x`` into the closed unit interval ``[0, 1]``."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> WilsonInterval:
    """Wilson score interval for ``successes`` out of ``n`` at ``confidence``.

    The critical value is derived from ``statistics.NormalDist().inv_cdf`` so any
    confidence level works (not just a hardcoded z=1.96). Bounds are clamped to
    ``[0, 1]``.

    Raises ``ValueError`` if ``n == 0``, ``successes < 0``, ``n < 0``,
    ``successes > n``, or ``confidence`` is not strictly inside ``(0, 1)``.
    """
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"confidence must be in (0, 1), got {confidence!r}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n!r}")
    if n == 0:
        raise ValueError("n must be > 0 for a Wilson interval")
    if successes < 0:
        raise ValueError(f"successes must be >= 0, got {successes!r}")
    if successes > n:
        raise ValueError(f"successes ({successes}) must be <= n ({n})")

    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5)) / denom

    lower = _clamp01(center - half)
    upper = _clamp01(center + half)
    point = _clamp01(p)
    return WilsonInterval(lower=lower, upper=upper, point=point, n=n, confidence=confidence)


def percentile(data, q: float) -> float:
    """The ``q``-th percentile of ``data`` (``q`` in ``[0, 100]``).

    Uses linear interpolation between closest ranks (numpy's default
    ``linear``/``"inclusive"`` semantics). The input is COPIED before sorting,
    so the caller's sequence is never mutated.

    Raises ``ValueError`` on empty ``data``, ``q`` outside ``[0, 100]``, or any
    non-finite value (``nan``/``inf``) in ``data`` or ``q``.
    """
    if not math.isfinite(q):
        raise ValueError(f"q must be finite, got {q!r}")
    if not (0.0 <= q <= 100.0):
        raise ValueError(f"q must be in [0, 100], got {q!r}")
    ordered = sorted(data)  # COPY-then-sort: never mutate the caller's sequence
    if not ordered:
        raise ValueError("percentile() arg is an empty sequence")
    if not all(math.isfinite(x) for x in ordered):
        raise ValueError("percentile() data contains a non-finite value (nan/inf)")

    if len(ordered) == 1:
        return float(ordered[0])

    rank = (q / 100.0) * (len(ordered) - 1)
    low = int(rank)  # floor
    high = low + 1
    if high >= len(ordered):
        return float(ordered[-1])
    frac = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * frac)


def percentile_rank(data, value) -> float:
    """Percentile rank of ``value`` within ``data`` (weak ``<=``), in ``[0, 100]``.

    Returns the percentage of data points that are ``<= value``.

    Raises ``ValueError`` on empty ``data`` or any non-finite value
    (``nan``/``inf``) in ``data`` or ``value``.
    """
    n = len(data)
    if n == 0:
        raise ValueError("percentile_rank() arg is an empty sequence")
    if not math.isfinite(value):
        raise ValueError(f"value must be finite, got {value!r}")
    if not all(math.isfinite(x) for x in data):
        raise ValueError("percentile_rank() data contains a non-finite value (nan/inf)")
    count = sum(1 for x in data if x <= value)
    return 100.0 * count / n
