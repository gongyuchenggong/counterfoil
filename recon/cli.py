"""Command line interface: python -m recon <command> [options]."""

import argparse
import os
import sys
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from .alerts import build_alerts, channels_from_env, dispatch, write_alerts
from .config import load_config
from .engine import ManualActionError
from .loader import SchemaError, load_feeds, parse_row, read_raw
from .models import CONFIRMATION, INTERNAL, LABEL, SIDES, Event
from .reports import write_reports
from .runner import LiveRunner, RunStateMismatch, SimulatedCrash, apply_action, run
from .store import AnotherWriterRunning, FileStore, PostgresStore

ROOT = Path(__file__).resolve().parent.parent
USER_ERRORS = (
    ManualActionError,
    RunStateMismatch,
    SchemaError,
    AnotherWriterRunning,
    ValidationError,
    OSError,
    UnicodeDecodeError,
    yaml.YAMLError,
)


# ---------------------------------------------------------------- helpers
def _postgres(args) -> PostgresStore:
    if not args.dsn:
        raise SystemExit("--store postgres needs --dsn (or the RECON_DSN environment variable)")
    try:
        import psycopg
    except ImportError:
        raise SystemExit("PostgreSQL needs its driver: uv sync --extra postgres") from None
    try:
        return PostgresStore(args.dsn)
    except psycopg.OperationalError as exc:
        raise SystemExit(f"cannot reach PostgreSQL: {str(exc).strip()}") from None


def _store(args):
    return _postgres(args) if args.store == "postgres" else FileStore(args.out)


def _load_landing(args, store: PostgresStore) -> tuple[int, ...]:
    """Re-loading the same file inserts nothing."""
    return tuple(
        store.load_landing(side, read_raw(path, side), Path(path).name)
        for side, path in ((INTERNAL, args.internal), (CONFIRMATION, args.confirmations))
    )


def _items(args, cfg, store) -> list:
    if args.source == "postgres":
        if not isinstance(store, PostgresStore):
            raise SystemExit("--source postgres needs --store postgres")
        items = [
            parse_row(side, raw, source_row, cfg) for side in SIDES for _, source_row, raw in store.read_landing(side)
        ]
        return sorted(items, key=lambda item: item.order_key)
    items = load_feeds(args.internal, args.confirmations, cfg)
    if isinstance(store, PostgresStore):
        _load_landing(args, store)
    return items


def _lock(store) -> None:
    """Writers to PostgreSQL take the single-writer lock first. File mode has no lock (DESIGN.md, section 5)."""
    if isinstance(store, PostgresStore) and not store.holds_writer_lock:
        store.acquire_writer_lock()


def _reports(store, cfg, args) -> list[Path]:
    """Rebuild the reports from the event log and send any alert that has not gone out yet."""
    out = Path(args.out)
    events = store.load_events()
    paths = write_reports(events, cfg, out, jsonl=isinstance(store, PostgresStore))
    alerts = build_alerts(events, cfg)
    paths.append(write_alerts(alerts, out / "alerts.txt"))
    dispatch(alerts, channels_from_env(), out / "state" / "alerts_sent.log")
    return paths


class Printer:
    """Compact live console output while processing (the full report is written at the end)."""

    def __init__(self, quiet: bool):
        self.quiet = quiet
        self.pairs: set = set()

    def __call__(self, events: list[Event]) -> None:
        if self.quiet:
            return
        for e in events:
            if e.kind == "SNAPSHOT":
                print(f"[{e.sim_time}] --- end of day {e.details['day']}: {len(e.details['open'])} open record(s) ---")
                continue
            if e.pair_key:
                key = (e.pair_key, e.sim_time, e.status)
                if key in self.pairs:
                    continue
                self.pairs.add(key)
            if e.kind == "STATUS" and e.status == "PROCESSED" and e.link in ("hard", "manual") and e.counterparty_ids:
                internal, conf = (
                    (e.record_id, e.counterparty_ids[0]) if e.side == INTERNAL else (e.counterparty_ids[0], e.record_id)
                )
                note = f"  ({e.note})" if e.note else ""
                print(f"[{e.sim_time}] PROCESSED trade {internal}, matched with confirmation {conf}{note}")
                continue
            label = f"{LABEL[e.side]} {e.record_id}" if e.record_id is not None else f"{e.side} row"
            head = e.status if e.kind == "STATUS" else e.kind
            severity = f" [{e.severity}]" if e.severity else ""
            print(f"[{e.sim_time}] {head}{severity} {label}: {e.what[:120]}")


# ---------------------------------------------------------------- commands
def cmd_run(args) -> int:
    cfg = load_config(args.config)
    with closing(_store(args)) as store:
        _lock(store)
        items = _items(args, cfg, store)
        try:
            run(cfg, items, store, fresh=args.fresh, crash_after=args.crash_after, on_events=Printer(args.quiet))
        except SimulatedCrash as exc:
            print(f"\n!! {exc}. Run the same command again (without --crash-after) to resume.")
            return 3
        print("\nWrote: " + ", ".join(map(str, _reports(store, cfg, args))))
    return 0


def cmd_report(args) -> int:
    cfg = load_config(args.config)
    with closing(_store(args)) as store:
        print("Wrote: " + ", ".join(map(str, _reports(store, cfg, args))))
    return 0


def _submit_to_live(store, action: dict, wait_seconds: float) -> int:
    """Live mode: the running engine owns the state, so the decision is handed to it as a request."""
    request_id = store.submit_request(action)
    print(f"Submitted request {request_id} to the live engine.")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        status = store.request_status(request_id)
        if status.get("status") == "applied":
            print(f"Applied as action {status['action_id']} ({status['result']}).")
            return 0
        if status.get("status") == "rejected":
            print(f"error: rejected by the live engine: {status['result']}", file=sys.stderr)
            return 2
        time.sleep(0.5)
    print("Still pending: the live engine applies it on its next poll (check ops_requests).")
    return 0


def cmd_manual(args) -> int:
    cfg = load_config(args.config)
    action = args.action(args)
    with closing(_store(args)) as store:
        if isinstance(store, PostgresStore) and (store.load_checkpoint() or {}).get("mode") == "live":
            return _submit_to_live(store, action, args.wait)
        _lock(store)
        events = apply_action(cfg, _items(args, cfg, store), store, action, on_events=Printer(False))
        _reports(store, cfg, args)
    print(f"Recorded {action['action']} ({len(events)} event(s)); reports refreshed.")
    return 0


def cmd_load_db(args) -> int:
    with closing(_postgres(args)) as store:
        new_internal, new_confirmations = _load_landing(args, store)
    print(f"Loaded {new_internal} new internal rows and {new_confirmations} new confirmation rows.")
    return 0


def cmd_live(args) -> int:
    cfg = load_config(args.config)
    with closing(_postgres(args)) as store:
        store.acquire_writer_lock()  # before --fresh, so a running instance is never wiped
        if args.fresh:
            store.reset(keep_actions=False)  # decisions are tied to the step numbers of the run they belong to
        runner = LiveRunner(
            cfg,
            store,
            grace=timedelta(seconds=args.grace),
            lookback=timedelta(seconds=args.lookback),
            on_events=Printer(args.quiet),
        ).start()
        print(
            f"Live reconciliation: polling every {args.interval}s, grace {args.grace}s, "
            f"reports every {args.report_every}s when something changed. Ctrl+C to stop."
        )
        try:
            runner.run_forever(
                args.interval,
                args.cycles,
                report_fn=lambda: _reports(store, cfg, args),
                report_interval_seconds=args.report_every,
            )
        except KeyboardInterrupt:
            print("Stopped. State is saved; start again to continue.")
            _reports(store, cfg, args)
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(ROOT / "config.yaml"), help="rules and reference data")
    common.add_argument("--out", default=str(ROOT / "output"), help="folder for reports (and file-mode state)")
    common.add_argument("--store", choices=("file", "postgres"), default="file", help="where results are kept")
    common.add_argument("--dsn", default=os.environ.get("RECON_DSN"), help="PostgreSQL connection string")
    common.add_argument("--internal", default=str(ROOT / "data" / "internal_trades.csv"))
    common.add_argument("--confirmations", default=str(ROOT / "data" / "external_confirmations.csv"))
    common.add_argument(
        "--source",
        choices=("csv", "postgres"),
        default="csv",
        help="read inputs from the CSV files or from the PostgreSQL landing tables",
    )
    manual = argparse.ArgumentParser(add_help=False)
    manual.add_argument("--actor", default=os.environ.get("USER", "ops"))
    manual.add_argument(
        "--wait",
        type=float,
        default=15.0,
        help="live mode: seconds to wait for the running engine to apply the request",
    )

    parser = argparse.ArgumentParser(prog="python -m recon", description="Broker trades reconciliation engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", parents=[common], help="reconcile, resuming from the last checkpoint if any")
    p.add_argument("--fresh", action="store_true", help="forget previous results (Operations actions are kept)")
    p.add_argument("--crash-after", type=int, help="stop abruptly after this many input steps (restart demo)")
    p.add_argument("--quiet", action="store_true", help="no console output while processing")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", parents=[common], help="rebuild text, CSV and HTML reports from stored events")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "accept", parents=[common, manual], help="accept an exception (and its linked records) as resolved"
    )
    p.add_argument("--side", choices=SIDES, required=True)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(
        func=cmd_manual,
        action=lambda a: {"action": "accept", "side": a.side, "record_id": a.id, "actor": a.actor, "reason": a.reason},
    )

    p = sub.add_parser("force-match", parents=[common, manual], help="link a trade and a confirmation by hand")
    p.add_argument("--trade", type=int, required=True)
    p.add_argument("--confirmation", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(
        func=cmd_manual,
        action=lambda a: {
            "action": "force-match",
            "trade_id": a.trade,
            "confirmation_id": a.confirmation,
            "actor": a.actor,
            "reason": a.reason,
        },
    )

    p = sub.add_parser("note", parents=[common, manual], help="attach a note to a record")
    p.add_argument("--side", choices=SIDES, required=True)
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(
        func=cmd_manual,
        action=lambda a: {"action": "note", "side": a.side, "record_id": a.id, "actor": a.actor, "text": a.text},
    )

    p = sub.add_parser(
        "load-db",
        parents=[common],
        help="demo stand-in for the feed adapters: load the CSV files into the landing tables",
    )
    p.set_defaults(func=cmd_load_db)

    p = sub.add_parser("live", parents=[common], help="poll PostgreSQL and reconcile on the wall clock")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    p.add_argument("--grace", type=float, default=30.0, help="seconds to hold rows back (watermark)")
    p.add_argument("--cycles", type=int, help="stop after this many polls")
    p.add_argument(
        "--lookback",
        type=float,
        default=300.0,
        help="seconds of recently loaded rows re-read each poll (catches out-of-order commits)",
    )
    p.add_argument(
        "--report-every",
        type=float,
        default=60.0,
        help="refresh reports at most this often (seconds) when something changed",
    )
    p.add_argument("--fresh", action="store_true", help="forget previous results and Operations actions")
    p.add_argument("--quiet", action="store_true", help="no console output while processing")
    p.set_defaults(func=cmd_live)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except USER_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
