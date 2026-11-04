"""Shared fixtures and record builders."""

import csv
import os
from datetime import datetime
from pathlib import Path

import pytest

from recon.config import build_config, load_config
from recon.engine import FINALIZE, Engine
from recon.loader import load_feeds, parse_row
from recon.models import CONFIRMATION, ID_COLUMN, INTERNAL
from recon.store import PostgresStore

ROOT = Path(__file__).resolve().parent.parent
TEST_DATA = Path(__file__).parent / "data"
SAMPLE_INTERNAL = ROOT / "data" / "internal_trades.csv"
SAMPLE_CONFIRMATIONS = ROOT / "data" / "external_confirmations.csv"


@pytest.fixture(autouse=True)
def no_alert_channels(monkeypatch):
    """No test may ever send a real alert."""
    for name in ("RECON_SLACK_WEBHOOK_URL", "RECON_SMTP_HOST", "RECON_ALERT_TO"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="session")
def items(cfg):
    """The sample data, as the engine receives it."""
    return load_feeds(SAMPLE_INTERNAL, SAMPLE_CONFIRMATIONS, cfg)


@pytest.fixture(scope="session")
def pg_dsn():
    """A throwaway PostgreSQL database. Skips the test when no server is configured or reachable."""
    psycopg = pytest.importorskip("psycopg")
    admin = os.environ.get("RECON_TEST_ADMIN_DSN")
    if not admin:
        pytest.skip("set RECON_TEST_ADMIN_DSN to run the PostgreSQL tests")
    try:
        conn = psycopg.connect(admin, autocommit=True, connect_timeout=3)
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    conn.execute("DROP DATABASE IF EXISTS recon_test WITH (FORCE)")
    conn.execute("CREATE DATABASE recon_test")
    conn.close()
    yield admin.replace("dbname=postgres", "dbname=recon_test")


@pytest.fixture
def fresh_store(pg_dsn):
    """A PostgreSQL store with no results and empty landing tables."""
    store = PostgresStore(pg_dsn)
    store.reset(keep_actions=False)
    store.conn.execute("TRUNCATE internal_trades, broker_confirmations RESTART IDENTITY")
    yield store
    store.close()


def at(value: str) -> datetime:
    """'HH:MM' on 2026-03-02, or a full timestamp."""
    return datetime.strptime(value if len(value) > 5 else f"2026-03-02 {value}:00", "%Y-%m-%d %H:%M:%S")


def _record(
    side,
    record_id,
    inserted,
    created=None,
    instrument="APPLE",
    price="100",
    volume="10",
    fee="5",
    currency="USD",
    trader="Elsa Snow",
    broker="Moonshot Capital",
    cfg=None,
):
    """One record, built the way the loader builds it. Any term not given is the same on both sides."""
    raw = {
        ID_COLUMN[side]: str(record_id),
        "inserted_at": str(at(inserted)),
        "created_at": str(at(created or inserted)),
        "instrument": instrument,
        "price": str(price),
        "volume": str(volume),
        "fee": str(fee),
        "currency": currency,
        "trader": trader,
        "broker": broker,
    }
    return parse_row(side, raw, 0, cfg or build_config())


def trade(record_id, inserted, **terms):
    return _record(INTERNAL, record_id, inserted, **terms)


def confirmation(record_id, inserted, **terms):
    return _record(CONFIRMATION, record_id, inserted, **terms)


def simulate(records, cfg=None):
    """Feed records in inserted_at order, then end the input. Returns (engine, events)."""
    engine = Engine(cfg or build_config())
    events = []
    for item in sorted(records, key=lambda r: r.order_key):
        events += engine.step(item)
    return engine, events + engine.step(FINALIZE)


def trade_state(engine, record_id):
    return engine.states[(INTERNAL, record_id)]


def confirmation_state(engine, record_id):
    return engine.states[(CONFIRMATION, record_id)]


def status_events(events):
    return [e for e in events if e.kind == "STATUS"]


def trade_events(events, record_id):
    return [e for e in status_events(events) if e.side == INTERNAL and e.record_id == record_id]


def confirmation_events(events, record_id):
    return [e for e in status_events(events) if e.side == CONFIRMATION and e.record_id == record_id]


def expected_rows() -> list[dict]:
    """The sample data's expected result per record."""
    with (TEST_DATA / "sample_expected_results.csv").open() as fh:
        return list(csv.DictReader(fh))


def expected_statuses() -> dict:
    return {(r["side"], int(r["record_id"])): r["final_status"] for r in expected_rows()}
