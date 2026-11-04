"""Drive the engine over time: replay of an ordered input, or live polling of PostgreSQL.

Restart model, both modes: the engine is deterministic, so its state is rebuilt by replaying the
inputs and recorded Operations decisions without writing anything, then processing continues.
Events carry deterministic ids and the stores skip ids they already hold, so nothing counts twice.
"""

import hashlib
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import islice

from .config import Config
from .engine import FINALIZE, Engine, ManualActionError
from .loader import parse_row
from .models import SIDES, BadRow, Event, fmt_ts


class SimulatedCrash(RuntimeError):
    """Raised by --crash-after to prove that a restart neither panics nor double counts."""


class RunStateMismatch(RuntimeError):
    """The stored checkpoint does not belong to these inputs or these rules."""


def _check_state(checkpoint: dict, cfg: Config, mode: str) -> None:
    """The stored results must come from the same kind of run and the same rules."""
    stored = checkpoint.get("mode", "replay")
    if stored != mode:
        raise RunStateMismatch(f"this store holds a {stored} run. Use --fresh to start a {mode} run.")
    if checkpoint["rule_version"] != cfg.rule_version:
        raise RunStateMismatch(
            f"stored results were produced by {checkpoint['rule_version']}, current rules are "
            f"{cfg.rule_version}. Use --fresh to rebuild everything under the new rules."
        )


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _actions_by_step(actions: list[dict]) -> dict:
    grouped = defaultdict(list)
    for a in sorted(actions, key=lambda a: a["action_id"]):
        grouped[a["after_step"]].append(a)
    return grouped


# ===================================================================== replay mode
def _item_token(item) -> str:
    if isinstance(item, BadRow):
        return f"bad|{item.side}|{item.source_row}|{sorted(item.raw.items())}"
    return f"rec|{item.side}|{item.record_id}|{item.inserted_at}|{item.fingerprint}"


def input_digests(items: list) -> list[str]:
    """digests[k] fingerprints the first k inputs, so a checkpoint can prove the inputs did not change."""
    h = hashlib.sha256()
    out = [h.hexdigest()]
    for item in items:
        h.update(_item_token(item).encode())
        out.append(h.hexdigest())
    return out


def _check_inputs(checkpoint: dict, items: list, digests: list[str]) -> None:
    done = int(checkpoint["steps"])
    if done > len(items) + 1 or checkpoint["input_digest"] != digests[min(done, len(items))]:
        raise RunStateMismatch("the input files changed since the last run. Use --fresh to rebuild.")


def _steps(engine: Engine, items: list, actions: list[dict]):
    """Feed every input and then FINALIZE, applying recorded decisions where they happened."""
    grouped = _actions_by_step(actions)
    for step, item in enumerate([*items, FINALIZE], start=1):
        events = engine.step(item)
        for action in grouped.get(step, ()):
            events += engine.apply_manual(action)
        yield step, item, events, engine.pop_dirty()


def run(
    cfg: Config, items: list, store, *, fresh: bool = False, crash_after: int | None = None, on_events=None
) -> Engine:
    """Process every input in inserted_at order, resuming from the store's checkpoint if there is one."""
    checkpoint = store.load_checkpoint()
    if fresh or not checkpoint:  # events with no checkpoint cannot be traced to these inputs and rules
        # A live run's decisions are tied to its own step numbers, which mean nothing here.
        store.reset(keep_actions=not checkpoint or checkpoint.get("mode") != "live")
        checkpoint = None
    digests = input_digests(items)
    done = 0
    if checkpoint:
        _check_state(checkpoint, cfg, "replay")
        _check_inputs(checkpoint, items, digests)
        done = int(checkpoint["steps"])

    engine = Engine(cfg)
    for step, item, events, dirty in _steps(engine, items, store.load_manual_actions()):
        if step <= done:
            continue  # replaying: state only, no writes
        store.commit_step(
            events,
            dirty,
            {
                "steps": step,
                "finalized": item is FINALIZE,
                "rule_version": cfg.rule_version,
                "input_digest": digests[min(step, len(items))],
                "seq": engine.seq,
                "clock": fmt_ts(engine.clock),
                "updated_at": _now_text(),
            },
        )
        if on_events:
            on_events(events)
        if crash_after is not None and step >= crash_after and item is not FINALIZE:
            raise SimulatedCrash(f"simulated crash after step {step}")
    return engine


def apply_action(cfg: Config, items: list, store, action: dict, on_events=None) -> list[Event]:
    """Record and apply one Operations decision on top of the stored state (replay mode).
    In live mode decisions go to the running engine instead (see LiveRunner)."""
    checkpoint = store.load_checkpoint()
    if not checkpoint:
        raise RunStateMismatch("nothing to act on yet. Run the reconciliation first.")
    _check_state(checkpoint, cfg, "replay")
    _check_inputs(checkpoint, items, input_digests(items))
    actions = store.load_manual_actions()
    steps = int(checkpoint["steps"])
    engine = Engine(cfg)
    for _ in islice(_steps(engine, items, actions), steps):
        pass
    action = {
        **action,
        "action_id": max((a["action_id"] for a in actions), default=0) + 1,
        "after_step": steps,
        "recorded_at": _now_text(),
    }
    events = engine.apply_manual(action)  # raises ManualActionError before anything is stored
    store.commit_action(action, events, engine.pop_dirty(), {**checkpoint, "seq": engine.seq})
    if on_events:
        on_events(events)
    return events


# ===================================================================== live mode
class LiveRunner:
    """Polls the landing tables and reconciles on the wall clock, all day. Differences from replay:

    * Deadlines fire on schedule even when no record arrives. A watermark (now minus a grace period)
      holds rows back briefly, so a row committed a little late is still processed in inserted_at order.
    * Every input and every clock tick that changed something is logged in recon_steps, in the same
      transaction as its events. A restart replays that log and gets the identical state.
    * This process owns the state, so Operations decisions arrive as rows in ops_requests and are
      applied here. A PostgreSQL advisory lock refuses a second writer.
    * Database errors do not stop the loop: back off, reconnect, rebuild, continue.

    An ever-increasing id is NOT a safe read cursor. Ids are handed out at insert but rows become
    visible at commit, which can happen out of id order, so "id > last seen" could skip a row forever.
    Every poll therefore re-reads the rows loaded in the last `lookback` (database clock) and drops
    those already seen. Nothing is missed unless an insert transaction stays open longer than `lookback`.
    """

    def __init__(
        self,
        cfg: Config,
        store,
        now_fn=datetime.now,
        grace: timedelta = timedelta(seconds=30),
        on_events=None,
        lookback: timedelta = timedelta(minutes=5),
        log=None,
    ):
        self.cfg, self.store, self.now_fn, self.grace, self.on_events = cfg, store, now_fn, grace, on_events
        self.lookback = lookback
        self.log = log or (lambda msg: print(f"[live] {msg}", file=sys.stderr))
        self.needs_recovery = False
        self.next_action_id = 1
        self._report_pending = True  # something changed since the last report refresh
        self._last_report = None
        self.engine = Engine(cfg)
        self.step = 0
        self.seen: set = set()  # (side, ingest_id) processed or waiting in the buffer
        self.read_from = None  # database time from which the next poll re-reads
        self.buffer: list = []  # (item, side, ingest_id) waiting for the watermark

    # ---- start and restart
    def start(self) -> "LiveRunner":
        """Take the writer lock, check the stored state belongs to live mode and these rules, rebuild."""
        if not self.store.holds_writer_lock:
            self.store.acquire_writer_lock()
        checkpoint = self.store.load_checkpoint()
        if checkpoint:
            _check_state(checkpoint, self.cfg, "live")
        self.engine = self.rebuild()
        return self

    def rebuild(self) -> Engine:
        """Replay the logged steps (inputs and ticks) and recorded decisions without writing anything."""
        actions = self.store.load_manual_actions()
        engine = Engine(self.cfg)
        grouped = _actions_by_step(actions)
        self.next_action_id = max((a["action_id"] for a in actions), default=0) + 1
        read_started = self.store.db_now()  # taken before reading, so a late commit is still re-read
        landing = {s: {r[0]: r for r in self.store.read_landing(s)} for s in SIDES}
        processed, self.step = set(), 0
        for row in self.store.load_steps():
            if row["kind"] == "record":
                _, source_row, raw = landing[row["side"]][row["ingest_id"]]
                engine.step(parse_row(row["side"], raw, source_row, self.cfg))
                processed.add((row["side"], row["ingest_id"]))
            else:
                engine.advance_clock(row["tick_at"])
            for action in grouped.get(row["step"], ()):
                engine.apply_manual(action)
            engine.drain()
            engine.pop_dirty()
            self.step = row["step"]
        # Anything landed but not yet processed (for example held back by the watermark) is buffered again.
        self.buffer = []
        self.seen = set(processed)
        for side in SIDES:
            for row in landing[side].values():
                if (side, row[0]) not in processed:
                    self._buffer(side, row)
        self.read_from = read_started - self.lookback
        return engine

    def _buffer(self, side: str, row: tuple) -> None:
        ingest_id, source_row, raw = row
        self.seen.add((side, ingest_id))
        self.buffer.append((parse_row(side, raw, source_row, self.cfg), side, ingest_id))

    def _read_new_rows(self) -> None:
        poll_started = self.store.db_now()
        for side in SIDES:
            for row in self.store.read_landing(side, since=self.read_from):
                if (side, row[0]) not in self.seen:
                    self._buffer(side, row)
        self.read_from = poll_started - self.lookback

    def _checkpoint(self) -> dict:
        return {
            "steps": self.step,
            "mode": "live",
            "finalized": False,
            "rule_version": self.cfg.rule_version,
            "input_digest": None,
            "seq": self.engine.seq,
            "clock": fmt_ts(self.engine.clock),
        }

    def _publish(self, events: list) -> list:
        if self.on_events and events:
            self.on_events(events)
        return events

    # ---- one polling cycle
    def poll_once(self) -> list:
        watermark = self.now_fn() - self.grace
        self._read_new_rows()
        is_ready = lambda b: b[0].inserted_at is None or b[0].inserted_at <= watermark  # noqa: E731
        ready = sorted(filter(is_ready, self.buffer), key=lambda b: b[0].order_key)
        self.buffer = [b for b in self.buffer if not is_ready(b)]  # after a database error, rebuild() refills it
        emitted = []
        for item, side, ingest_id in ready:
            emitted += self._commit(self.engine.step(item), "record", side, ingest_id)
        eng = self.engine
        if eng.clock is not None and watermark > eng.clock:
            due = eng.heap and eng.heap[0][0] < watermark
            if due or watermark.date() > eng.current_day:
                eng.advance_clock(watermark)
                emitted += self._commit(eng.drain(), "tick", tick_at=watermark)
        return emitted

    def _commit(self, events: list, kind: str, side=None, ingest_id=None, tick_at=None) -> list:
        self.step += 1
        step_row = {"step": self.step, "kind": kind, "side": side, "ingest_id": ingest_id, "tick_at": tick_at}
        self.store.commit_step(events, self.engine.pop_dirty(), self._checkpoint(), step_row=step_row)
        return self._publish(events)

    def _apply_requests(self) -> list:
        """Apply submitted decisions. Each is stored with after_step = the current step, which is exactly
        where a restart replays it. An invalid or malformed request is rejected with the reason; the engine
        validates before it changes anything, so the state is untouched."""
        emitted = []
        for request_id, payload in self.store.pending_requests():
            action = {
                **payload,
                "action_id": self.next_action_id,
                "after_step": self.step,
                "recorded_at": _now_text(),
                "request_id": request_id,
            }
            try:
                events = self.engine.apply_manual(action)
            except (KeyError, TypeError, ValueError) as exc:  # requests can come from outside the CLI
                reason = str(exc) if isinstance(exc, ManualActionError) else f"malformed request: {exc!r}"
                self.store.reject_request(request_id, reason)
                self.log(f"request {request_id} rejected: {reason}")
                continue
            self.store.commit_action(action, events, self.engine.pop_dirty(), self._checkpoint(), request_id=request_id)
            self.next_action_id += 1
            emitted += self._publish(events)
        return emitted

    # ---- the loop
    def cycle(self) -> list:
        """One poll plus pending Operations requests. Raises database errors to the caller.
        After a failure: new connection, state rebuilt from what the database holds. Anything done
        in memory but not committed is simply redone, so nothing is lost or doubled."""
        if self.needs_recovery:
            self.store.reconnect()
            self.engine = self.rebuild()
            self.needs_recovery = False
        return self.poll_once() + self._apply_requests()

    def run_forever(
        self,
        interval_seconds: float = 5.0,
        max_cycles: int | None = None,
        report_fn=None,
        report_interval_seconds: float = 60.0,
        sleep_fn=time.sleep,
        clock_fn=time.monotonic,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        """Poll until stopped. On a database error wait 1, 2, 4 ... up to max_backoff_seconds, then
        recover. Reports refresh when something changed, at most every report_interval_seconds."""
        import psycopg

        cycles, failures = 0, 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            try:
                if self.cycle():
                    self._report_pending = True
                failures = 0
            except psycopg.Error as exc:
                failures += 1
                self.needs_recovery = True
                wait = min(max_backoff_seconds, 2 ** (failures - 1))
                self.log(f"database error ({type(exc).__name__}: {str(exc).strip()[:120]}); retrying in {wait:.0f}s")
                sleep_fn(wait)
                continue
            if (
                report_fn
                and self._report_pending
                and (self._last_report is None or clock_fn() - self._last_report >= report_interval_seconds)
            ):
                try:
                    report_fn()
                    self._last_report, self._report_pending = clock_fn(), False
                except psycopg.Error as exc:
                    self.needs_recovery = True
                    self.log(f"report refresh failed ({type(exc).__name__}); will retry")
            sleep_fn(interval_seconds)
        if report_fn and self._report_pending and not self.needs_recovery:
            report_fn()
            self._last_report, self._report_pending = clock_fn(), False
