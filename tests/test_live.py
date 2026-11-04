"""Live mode on a real PostgreSQL server: polling, decisions during the day, failures (skipped without a server)."""

from datetime import datetime, timedelta
from functools import cache

import pytest
from conftest import SAMPLE_CONFIRMATIONS, SAMPLE_INTERNAL, expected_statuses

from recon.cli import main
from recon.loader import read_raw
from recon.models import CONFIRMATION, INTERNAL
from recon.runner import LiveRunner, RunStateMismatch, run
from recon.store import AnotherWriterRunning, PostgresStore

psycopg = pytest.importorskip("psycopg")


def _insert_sql():
    return (
        "INSERT INTO internal_trades (trade_id, inserted_at, created_at, instrument, price, volume, fee, "
        "currency, trader, broker, source_file, source_row) VALUES (%s, %s, %s, 'APPLE', '100', '10', '5', "
        "'USD', 'Elsa Snow', 'Moonshot Capital', 'terminal', %s)"
    )


def test_row_committed_out_of_id_order_is_not_skipped(cfg, pg_dsn, fresh_store):
    """Terminal A gets id 1 but commits after terminal B (id 2) has been read.
    A cursor 'id > last seen' would skip id 1 forever; the re-read window must catch it."""
    runner = LiveRunner(
        cfg, fresh_store, now_fn=lambda: datetime(2026, 3, 2, 12, 0), grace=timedelta(seconds=30)
    ).start()

    slow = psycopg.connect(pg_dsn)  # terminal A: transaction stays open
    slow.execute(_insert_sql(), ["101", "2026-03-02 10:00:00", "2026-03-02 09:59:00", 1])
    fast = psycopg.connect(pg_dsn, autocommit=True)  # terminal B: commits at once
    fast.execute(_insert_sql(), ["102", "2026-03-02 10:01:00", "2026-03-02 10:00:00", 2])

    runner.poll_once()
    assert (INTERNAL, 102) in runner.engine.states
    assert (INTERNAL, 101) not in runner.engine.states  # not visible yet: A has not committed

    slow.commit()  # A commits after B's row was already read
    runner.poll_once()
    assert (INTERNAL, 101) in runner.engine.states
    ids = [r[0] for r in fresh_store.read_landing(INTERNAL)]
    assert sorted(ids) == [1, 2]  # A really did hold the lower id
    slow.close()
    fast.close()


def test_rows_are_not_processed_twice_across_polls(cfg, fresh_store):
    runner = LiveRunner(cfg, fresh_store, now_fn=lambda: datetime(2026, 3, 2, 12, 0)).start()
    with fresh_store.conn.transaction():
        fresh_store.conn.execute(_insert_sql(), ["201", "2026-03-02 10:00:00", "2026-03-02 09:59:00", 1])
    for _ in range(3):  # the same row sits inside every re-read window
        runner.poll_once()
    steps = [s for s in fresh_store.load_steps() if s["kind"] == "record"]
    assert len(steps) == 1


# ------------------------------------------------------------------ running all day
AFTER_ALL_DEADLINES = datetime(2026, 3, 8, 3, 0)


@cache
def _rows_with_arrival_time(path):
    return [(n, r, datetime.strptime(r["inserted_at"], "%Y-%m-%d %H:%M:%S")) for n, r in read_raw(path)]


def _load_feeds(store, until=None):
    """Deliver the sample rows that have arrived by `until` (all of them by default)."""
    for side, path in ((INTERNAL, SAMPLE_INTERNAL), (CONFIRMATION, SAMPLE_CONFIRMATIONS)):
        rows = [(n, r) for n, r, arrived in _rows_with_arrival_time(path) if until is None or arrived <= until]
        store.load_landing(side, rows, path.name)


def _current(store):
    rows = store.conn.execute("select side, record_id, status from recon_current_status").fetchall()
    return {(s, r): st for s, r, st in rows}


def test_restart_during_the_day_gives_the_same_result(cfg, pg_dsn, fresh_store):
    """Rows land over time; the runner polls on a clock, is killed halfway, and restarts."""
    clock = {"now": datetime(2026, 3, 2, 9, 0)}
    store, restarted = fresh_store, False
    runner = LiveRunner(cfg, store, now_fn=lambda: clock["now"]).start()
    while clock["now"] < datetime(2026, 3, 8, 2, 0):
        _load_feeds(store, until=clock["now"] - timedelta(seconds=10))
        runner.poll_once()
        if not restarted and clock["now"] >= datetime(2026, 3, 5, 12, 0):
            store.close()  # the process dies
            store = PostgresStore(pg_dsn)
            runner = LiveRunner(cfg, store, now_fn=lambda: clock["now"]).start()
            restarted = True
        clock["now"] += timedelta(minutes=5)

    events = store.load_events()
    assert len({e.event_id for e in events}) == len(events)
    assert _current(store) == expected_statuses()
    store.close()


def test_decision_made_during_the_day_goes_through_the_running_engine(cfg, pg_dsn, fresh_store):
    _load_feeds(fresh_store)
    runner = LiveRunner(cfg, fresh_store, now_fn=lambda: AFTER_ALL_DEADLINES).start()
    runner.cycle()
    assert runner.engine.states[(INTERNAL, 2028)].status == "BREAK"

    ops_desk = PostgresStore(pg_dsn)  # a different process: it only submits a request
    request_id = ops_desk.submit_request(
        {
            "action": "accept",
            "side": INTERNAL,
            "record_id": 2028,
            "actor": "yg",
            "reason": "broker misspelt the trader name",
        }
    )
    assert ops_desk.request_status(request_id)["status"] == "pending"

    runner.cycle()  # the running engine applies it
    assert ops_desk.request_status(request_id)["status"] == "applied"
    assert runner.engine.states[(INTERNAL, 2028)].resolved_by == "manual"
    assert runner.engine.states[(CONFIRMATION, 18519)].resolved_by == "manual"
    assert _current(ops_desk)[(INTERNAL, 2028)] == "PROCESSED"
    events_before = len(ops_desk.load_events())
    ops_desk.close()

    fresh_store.close()  # restart: the decision is replayed, nothing is redone
    store = PostgresStore(pg_dsn)
    restarted = LiveRunner(cfg, store, now_fn=lambda: AFTER_ALL_DEADLINES).start()
    assert restarted.engine.states[(INTERNAL, 2028)].resolved_by == "manual"
    restarted.cycle()
    assert len(store.load_events()) == events_before
    store.close()


def test_invalid_request_is_rejected_with_the_reason(cfg, pg_dsn, fresh_store):
    _load_feeds(fresh_store)
    runner = LiveRunner(cfg, fresh_store, now_fn=lambda: AFTER_ALL_DEADLINES).start()
    runner.cycle()
    request_id = fresh_store.submit_request(
        {"action": "accept", "side": INTERNAL, "record_id": 404, "actor": "yg", "reason": "typo"}
    )
    runner.cycle()
    status = fresh_store.request_status(request_id)
    assert status["status"] == "rejected" and "no internal record with id 404" in status["result"]
    assert fresh_store.load_manual_actions() == []


def test_second_instance_is_refused(cfg, pg_dsn, fresh_store):
    LiveRunner(cfg, fresh_store, now_fn=lambda: AFTER_ALL_DEADLINES).start()
    other = PostgresStore(pg_dsn)
    with pytest.raises(AnotherWriterRunning):
        LiveRunner(cfg, other, now_fn=lambda: AFTER_ALL_DEADLINES).start()
    fresh_store.close()  # first instance stops: the lock is released
    LiveRunner(cfg, other, now_fn=lambda: AFTER_ALL_DEADLINES).start()
    other.close()


def test_writer_lock_is_kept_when_an_outage_outlasts_the_first_retry(pg_dsn, fresh_store):
    fresh_store.acquire_writer_lock()
    connect = fresh_store._connect

    def database_down():
        raise psycopg.OperationalError("database is down")

    fresh_store._connect = database_down
    with pytest.raises(psycopg.OperationalError):
        fresh_store.reconnect()
    fresh_store._connect = connect
    fresh_store.reconnect()

    other = PostgresStore(pg_dsn)
    with pytest.raises(AnotherWriterRunning):
        other.acquire_writer_lock()
    other.close()


def test_survives_a_lost_database_connection(cfg, pg_dsn, fresh_store):
    clock = {"now": datetime(2026, 3, 4, 12, 0)}
    _load_feeds(fresh_store, until=clock["now"])
    runner = LiveRunner(cfg, fresh_store, now_fn=lambda: clock["now"], log=lambda msg: None).start()
    runner.cycle()

    admin = psycopg.connect(pg_dsn, autocommit=True)  # the connection is killed under the runner
    admin.execute("select pg_terminate_backend(%s)", [fresh_store.conn.info.backend_pid])
    admin.close()
    feeder = PostgresStore(pg_dsn)
    _load_feeds(feeder)  # meanwhile the rest of the data lands
    feeder.close()
    clock["now"] = AFTER_ALL_DEADLINES

    sleeps = []
    runner.run_forever(interval_seconds=0, max_cycles=3, sleep_fn=sleeps.append)
    assert sleeps[0] == 1  # backed off once, then recovered
    assert _current(fresh_store) == expected_statuses()
    events = fresh_store.load_events()
    assert len({e.event_id for e in events}) == len(events)


def test_reports_refresh_while_running(cfg, pg_dsn, fresh_store):
    clock = {"now": datetime(2026, 3, 3, 12, 0)}
    _load_feeds(fresh_store, until=clock["now"])
    runner = LiveRunner(cfg, fresh_store, now_fn=lambda: clock["now"]).start()
    reports, mono = [], {"t": 0.0}

    def one_cycle():
        runner.run_forever(
            interval_seconds=0,
            max_cycles=1,
            report_fn=lambda: reports.append(mono["t"]),
            report_interval_seconds=60,
            sleep_fn=lambda s: None,
            clock_fn=lambda: mono["t"],
        )

    one_cycle()
    assert len(reports) == 1  # first cycle processed rows: refreshed
    mono["t"] = 30.0
    one_cycle()
    assert len(reports) == 1  # nothing changed: no refresh
    _load_feeds(fresh_store)
    clock["now"] = AFTER_ALL_DEADLINES
    mono["t"] = 200.0
    one_cycle()
    assert len(reports) == 2  # new data and the interval has passed


def test_a_malformed_request_is_rejected_and_the_loop_survives(cfg, fresh_store):
    runner = LiveRunner(cfg, fresh_store, now_fn=lambda: AFTER_ALL_DEADLINES, log=lambda msg: None).start()
    request_id = fresh_store.submit_request({"action": "accept"})
    runner.run_forever(interval_seconds=0, max_cycles=2, sleep_fn=lambda s: None)
    status = fresh_store.request_status(request_id)
    assert status["status"] == "rejected" and "malformed" in status["result"]


def test_live_fresh_forgets_decisions_so_a_restart_changes_nothing(cfg, pg_dsn, fresh_store, tmp_path):
    _load_feeds(fresh_store)
    args = ["--store", "postgres", "--dsn", pg_dsn, "--out", str(tmp_path), "--source", "postgres", "--quiet"]
    fresh_store.close()
    assert main(["run", "--fresh", *args]) == 0
    assert main(["accept", "--side", "internal", "--id", "2028", "--reason", "typo", "--actor", "yg", *args[:-1]]) == 0
    assert (
        main(
            [
                "live",
                "--fresh",
                "--cycles",
                "1",
                "--interval",
                "0",
                "--store",
                "postgres",
                "--dsn",
                pg_dsn,
                "--out",
                str(tmp_path),
                "--quiet",
            ]
        )
        == 0
    )
    store = PostgresStore(pg_dsn)
    assert store.load_manual_actions() == []
    runner = LiveRunner(cfg, store, now_fn=lambda: AFTER_ALL_DEADLINES, log=lambda msg: None).start()
    assert runner.engine.states[(INTERNAL, 2028)].status != "PROCESSED"
    store.close()


def test_a_live_run_is_not_mistaken_for_a_replay(cfg, items, fresh_store):
    _load_feeds(fresh_store)
    LiveRunner(cfg, fresh_store, now_fn=lambda: AFTER_ALL_DEADLINES, log=lambda msg: None).start().cycle()
    with pytest.raises(RunStateMismatch, match="live run"):
        run(cfg, items, fresh_store)
    run(cfg, items, fresh_store, fresh=True)  # --fresh is the way out
    assert fresh_store.load_checkpoint().get("mode") != "live"
