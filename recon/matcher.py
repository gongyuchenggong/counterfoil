"""Pure comparison helpers used by the engine. No state lives here."""

from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations

from .config import Config
from .models import FIELDS, NUMERIC_FIELDS, Record

DIMENSIONS = FIELDS + ("trade_date",)


def field_equal(a: Record, b: Record, name: str, cfg: Config) -> bool:
    if name in NUMERIC_FIELDS:
        return abs(getattr(a, name) - getattr(b, name)) <= getattr(cfg.rules.tolerances, name)
    return a.cmp(name) == b.cmp(name)


def numerics_equal(a: Record, b: Record, cfg: Config) -> bool:
    return all(field_equal(a, b, name, cfg) for name in NUMERIC_FIELDS)


def within_tolerance_only(a: Record, b: Record) -> list[str]:
    """Fields that matched only thanks to a tolerance (so the output can say so)."""
    return [n for n in NUMERIC_FIELDS if getattr(a, n) != getattr(b, n)]


def mismatches(a: Record, b: Record, cfg: Config) -> list[str]:
    """Dimensions (7 fields + trade date) on which two records disagree."""
    return [name for name in DIMENSIONS if not field_equal(a, b, name, cfg)]


def created_gap_seconds(a: Record, b: Record) -> float:
    return abs((a.created_at - b.created_at).total_seconds())


def closest_to(rec: Record):
    """Sort key for candidates: closest trade time, then earliest arrival, then lowest id."""
    return lambda s: (created_gap_seconds(s.rec, rec), s.rec.inserted_at, s.rec.record_id)


def vwap(parts: list[Record]) -> Decimal:
    total = sum(p.volume for p in parts)
    return sum(p.price * p.volume for p in parts) / total


def aggregates_to(single: Record, parts: list[Record], cfg: Config) -> bool:
    """True when parts add up to single: volumes and fees sum, and the
    volume-weighted average price rounds to single's price."""
    tolerances = cfg.rules.tolerances
    if abs(sum(p.volume for p in parts) - single.volume) > tolerances.volume:
        return False
    if abs(sum(p.fee for p in parts) - single.fee) > tolerances.fee:
        return False
    exponent = single.price.as_tuple().exponent
    quantum = Decimal(1).scaleb(exponent if isinstance(exponent, int) else 0)
    rounded = vwap(parts).quantize(quantum, rounding=ROUND_HALF_UP)
    return abs(rounded - single.price) <= tolerances.price


def find_group(single: Record, pool: list, cfg: Config, must_include=None):
    """Smallest group of pool entries (objects with a .rec) that aggregates to single.

    pool is pre-filtered to the same trade date, instrument, currency, trader and broker.
    Returns (group, alternatives) where alternatives counts other valid groups of the same size.
    """
    limits = cfg.rules.aggregation
    pool = sorted(pool, key=closest_to(single))[: limits.max_candidates]
    if must_include is not None and must_include not in pool:
        return None, 0
    for size in range(2, min(limits.max_group_size, len(pool)) + 1):
        valid = []
        for combo in combinations(pool, size):
            if must_include is not None and must_include not in combo:
                continue
            if aggregates_to(single, [s.rec for s in combo], cfg):
                valid.append(combo)
        if valid:
            best = min(
                valid,
                key=lambda combo: (
                    sum(created_gap_seconds(s.rec, single) for s in combo),
                    tuple(s.rec.record_id for s in combo),
                ),
            )
            return list(best), len(valid) - 1
    return None, 0
