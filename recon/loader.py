"""Read, validate and normalize the two feeds.

Normalization changes format only (whitespace, case, number precision).
Anything that would change meaning is left for the matcher to report.
"""

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import Config, norm_text
from .models import (
    CONFIRMATION,
    ID_COLUMN,
    INTERNAL,
    NUMERIC_FIELDS,
    BadRow,
    Record,
    required_columns,
)

TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")
# One absurd value must be rejected, not crash the run: beyond these, arithmetic outgrows decimal or date limits.
MAX_ID = 2**63 - 1  # BIGINT in PostgreSQL
MAX_DIGITS_BEFORE_POINT, MAX_DECIMALS = 12, 8
YEARS = range(1900, 2201)


class SchemaError(ValueError):
    """The file itself is unusable (missing columns). Fail fast with a clear message."""


def parse_timestamp(text: str) -> datetime:
    value = text.strip()
    for fmt in TS_FORMATS:
        try:
            # strptime accepts hours without a leading zero, e.g. 9:51:31
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if parsed.year not in YEARS:
            raise ValueError(f"implausible timestamp '{text}'")
        return parsed
    raise ValueError(f"unreadable timestamp '{text}'")


def _decimal(text: str, name: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{name} is not a number: '{text}'") from None
    if not value.is_finite():
        raise ValueError(f"{name} is not a finite number: '{text}'")
    if value.adjusted() >= MAX_DIGITS_BEFORE_POINT or value.as_tuple().exponent < -MAX_DECIMALS:
        raise ValueError(f"{name} is out of range: '{text}'")
    return value


def _attempt(reasons: list[str], fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        reasons.append(str(exc))
        return None


def _timestamp(raw: dict, name: str, reasons: list[str]) -> datetime | None:
    return _attempt(reasons, parse_timestamp, raw[name]) if raw.get(name, "").strip() else None


def _record_id(side: str, raw: dict, reasons: list[str]) -> int | None:
    id_text = raw.get(ID_COLUMN[side], "").strip()
    if not id_text:
        return None
    try:
        value = int(id_text)
    except ValueError:
        reasons.append(f"{ID_COLUMN[side]} is not an integer: '{id_text}'")
        return None
    if abs(value) > MAX_ID:
        reasons.append(f"{ID_COLUMN[side]} is out of range: '{id_text}'")
        return None
    return value


def _numbers(raw: dict, reasons: list[str]) -> dict:
    numbers = {
        name: _attempt(reasons, _decimal, raw[name], name) for name in NUMERIC_FIELDS if raw.get(name, "").strip()
    }
    if numbers.get("price") is not None and numbers["price"] <= 0:
        reasons.append(f"price must be positive: {raw['price']}")
    if numbers.get("volume") is not None and numbers["volume"] <= 0:
        reasons.append(f"volume must be positive (no side field to explain a sign): {raw['volume']}")
    if numbers.get("fee") is not None and numbers["fee"] < 0:
        reasons.append(f"fee must not be negative: {raw['fee']}")
    return numbers


def _apply_mappings(raw: dict, cfg: Config) -> tuple[dict, tuple]:
    """Normalized instrument, trader and broker, and what the Operations mapping tables changed."""
    terms, mapped = {}, []
    instrument = norm_text(raw["instrument"]).upper()
    target = cfg.mappings.instrument.get(instrument)
    if target and target != instrument:
        mapped.append(f"instrument: {instrument} -> {target}")
    terms["instrument"] = target or instrument
    for name in ("trader", "broker"):
        value = norm_text(raw[name])
        target = getattr(cfg.mappings, name).get(value.casefold())
        if target and target.casefold() != value.casefold():
            mapped.append(f"{name}: {value} -> {target}")
            value = target
        terms[name] = value
    return terms, tuple(mapped)


def parse_row(side: str, row: dict, source_row: int, cfg: Config) -> Record | BadRow:
    raw = {k: ("" if v is None else str(v)) for k, v in row.items() if k is not None}
    reasons = [f"missing {col}" for col in required_columns(side) if not raw.get(col, "").strip()]
    inserted_at = _timestamp(raw, "inserted_at", reasons)
    created_at = _timestamp(raw, "created_at", reasons)
    record_id = _record_id(side, raw, reasons)
    numbers = _numbers(raw, reasons)
    currency = norm_text(raw.get("currency", "")).upper()
    if currency and currency not in cfg.reference.currencies:
        reasons.append(f"unknown currency '{raw['currency']}'")
    if reasons:
        return BadRow(side=side, source_row=source_row, raw=raw, reasons=tuple(reasons), inserted_at=inserted_at)

    terms, mapped = _apply_mappings(raw, cfg)
    return Record(
        side=side,
        record_id=record_id,
        inserted_at=inserted_at,
        created_at=created_at,
        currency=currency,
        **numbers,
        **terms,
        raw={k: raw[k] for k in required_columns(side)},
        source_row=source_row,
        mapped=mapped,
    )


def read_raw(path: str | Path, side: str | None = None) -> list[tuple[int, dict]]:
    """Raw rows exactly as in the file. Row numbers count the header as line 1, so they match what
    a person sees in Excel. Given a side, the file must have that feed's columns."""
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        reader.fieldnames = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in required_columns(side) if c not in reader.fieldnames] if side else []
        if missing:
            raise SchemaError(f"{path.name}: missing column(s) {', '.join(missing)}")
        return [(line, {k: v for k, v in row.items() if k is not None}) for line, row in enumerate(reader, start=2)]


def load_csv(path: str | Path, side: str, cfg: Config) -> list[Record | BadRow]:
    return [parse_row(side, row, line, cfg) for line, row in read_raw(path, side)]


def load_feeds(internal_path, confirmation_path, cfg: Config) -> list[Record | BadRow]:
    """Both feeds merged and ordered by inserted_at, as if rows arrived one by one."""
    items = load_csv(internal_path, INTERNAL, cfg) + load_csv(confirmation_path, CONFIRMATION, cfg)
    return sorted(items, key=lambda item: item.order_key)
