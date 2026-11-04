"""Reading and validating the feeds: format is normalized, meaning never changes."""

from datetime import datetime
from decimal import Decimal

import pytest
from conftest import SAMPLE_CONFIRMATIONS, SAMPLE_INTERNAL, confirmation, trade

from recon.config import build_config
from recon.loader import SchemaError, load_csv, parse_row, parse_timestamp
from recon.models import CONFIRMATION, INTERNAL, BadRow, Record


def test_timestamps_without_leading_zero_are_read():
    assert parse_timestamp("2026-03-02 9:51:31") == datetime(2026, 3, 2, 9, 51, 31)


def test_numbers_compare_by_value_not_by_text():
    a = trade(1, "10:00", price="457")
    b = confirmation(2, "10:00", price="457.0")
    assert isinstance(a, Record) and a.price == b.price == Decimal("457")


def test_whitespace_and_case_are_normalized_but_not_meaning():
    r = trade(1, "10:00", instrument="  apple ", trader="  Elsa   Snow ", currency="usd")
    assert (r.instrument, r.trader, r.currency) == ("APPLE", "Elsa Snow", "USD")


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("volume", "-10", "volume must be positive"),
        ("volume", "0", "volume must be positive"),
        ("fee", "1e30", "out of range"),
        ("price", "1e-400", "out of range"),
        ("price", "NaN", "not a finite number"),
        ("inserted_at", "9999-12-31 23:59:59", "implausible"),
        ("trade_id", str(2**63), "out of range"),
        ("trade_id", "1e3", "not an integer"),
        ("price", "0", "price must be positive"),
        ("fee", "-1", "fee must not be negative"),
        ("currency", "XYZ", "unknown currency"),
        ("price", "abc", "price is not a number"),
        ("inserted_at", "yesterday", "unreadable timestamp"),
        ("trader", "", "missing trader"),
    ],
)
def test_bad_rows_are_quarantined_with_a_reason(field, value, reason):
    raw = {
        "trade_id": "1",
        "inserted_at": "2026-03-02 10:00:00",
        "created_at": "2026-03-02 09:59:00",
        "instrument": "APPLE",
        "price": "100",
        "volume": "10",
        "fee": "5",
        "currency": "USD",
        "trader": "Elsa Snow",
        "broker": "Moonshot Capital",
    }
    raw[field] = value
    row = parse_row(INTERNAL, raw, 7, build_config())
    assert isinstance(row, BadRow)
    assert any(reason in r for r in row.reasons), row.reasons


def test_missing_column_fails_fast(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("trade_id,inserted_at\n1,2026-03-02 10:00:00\n")
    with pytest.raises(SchemaError, match="missing column"):
        load_csv(path, INTERNAL, build_config())


def test_mapping_table_is_applied_and_recorded():
    cfg = build_config({"mappings": {"instrument": {"NVDA": "NVIDIA"}}})
    r = confirmation(9, "10:00", instrument="nvda", cfg=cfg)
    assert r.instrument == "NVIDIA" and r.mapped == ("instrument: NVDA -> NVIDIA",)


def test_sample_files_load_cleanly(cfg):
    internal = load_csv(SAMPLE_INTERNAL, INTERNAL, cfg)
    confirmations = load_csv(SAMPLE_CONFIRMATIONS, CONFIRMATION, cfg)
    assert len(internal) == 50 and len(confirmations) == 48
    assert not [r for r in internal + confirmations if isinstance(r, BadRow)]
