"""Durable stores. Replay mode needs this contract from a backend; live mode needs PostgreSQL's extras.

    load_checkpoint / load_events / load_manual_actions
    commit_step(events, dirty_states, checkpoint)      -- one input step
    commit_action(action, events, dirty_states, cp)    -- one Operations decision
    reset(keep_actions)                                -- forget results and, if asked, Operations decisions
    close()

Event ids are deterministic and existing ids are skipped, so a replay after a crash never counts twice.
"""

import json
import os
from pathlib import Path

from .models import CONFIRMATION, INTERNAL, OTHER, Event, required_columns

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


# ===================================================================== files
class FileStore:
    def __init__(self, out_dir: str | Path):
        self.dir = Path(out_dir)
        self.state_dir = self.dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "status_events.jsonl"
        self.checkpoint_path = self.state_dir / "checkpoint.json"
        self.actions_path = self.state_dir / "ops_actions.jsonl"
        self._ids: set | None = None

    # ---- helpers
    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        """Read a JSON-lines file. A torn last line (crash mid-write) is cut off, not trusted."""
        if not path.exists():
            return []
        rows, good_bytes = [], 0
        with path.open("rb") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                    good_bytes += len(line)
                except json.JSONDecodeError:
                    break
        if good_bytes != path.stat().st_size:
            with path.open("rb+") as fh:
                fh.truncate(good_bytes)
        return rows

    @staticmethod
    def _append_jsonl(path: Path, rows: list[dict]) -> None:
        if not rows:
            return
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _write_atomic(path: Path, data) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, default=str, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _known_ids(self) -> set:
        if self._ids is None:
            self._ids = {row["event_id"] for row in self._read_jsonl(self.events_path)}
        return self._ids

    # ---- contract
    def reset(self, keep_actions: bool = True) -> None:
        for path in (self.events_path, self.checkpoint_path):
            path.unlink(missing_ok=True)
        if not keep_actions:
            self.actions_path.unlink(missing_ok=True)
        self._ids = None

    def load_checkpoint(self) -> dict | None:
        if not self.checkpoint_path.exists():
            return None
        return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

    def load_events(self) -> list[Event]:
        return [Event.from_dict(row) for row in self._read_jsonl(self.events_path)]

    def load_manual_actions(self) -> list[dict]:
        return self._read_jsonl(self.actions_path)

    def commit_step(self, events: list[Event], dirty: list[dict], checkpoint: dict) -> None:
        # Order matters: events first, checkpoint last. A crash in between only means the
        # step is replayed, and its events are recognised by id and skipped.
        # (dirty is used by PostgreSQL; in file mode the current status is derived from the events.)
        ids = self._known_ids()
        new = [e.to_dict() for e in events if e.event_id not in ids]
        self._append_jsonl(self.events_path, new)
        ids.update(row["event_id"] for row in new)
        self._write_atomic(self.checkpoint_path, checkpoint)

    def commit_action(self, action: dict, events: list[Event], dirty: list[dict], checkpoint: dict) -> None:
        self._append_jsonl(self.actions_path, [action])
        self.commit_step(events, dirty, checkpoint)

    def close(self) -> None:
        pass


# ===================================================================== PostgreSQL
WRITER_LOCK_KEY = 7274100101  # one process at a time may write reconciliation results
SCHEMA_LOCK_KEY = 7274100102  # serializes schema setup when several processes connect at once
LANDING_TABLE = {INTERNAL: "internal_trades", CONFIRMATION: "broker_confirmations"}


def _insert(table: str, columns: str, on_conflict: str = "") -> str:
    """INSERT with named parameters, one per column."""
    names = columns.split()
    return f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(f'%({n})s' for n in names)}) {on_conflict}"


def _overwrite(columns: str) -> str:
    sets = ", ".join(f"{n} = EXCLUDED.{n}" for n in columns.split())
    return f"DO UPDATE SET {sets}, updated_at = now()"


INSERT_EVENT = _insert(
    "recon_status_events",
    "event_id seq sim_time kind side record_id status prev_status severity counterparty_ids link pair_key "
    "what why next note resolved_by actor reason late_minutes rule_version payload",
    "ON CONFLICT (event_id) DO NOTHING",
)
INSERT_QUARANTINE = _insert("quarantine", "event_id side source_row raw reason", "ON CONFLICT (event_id) DO NOTHING")
UPSERT_CURRENT = _insert(
    "recon_current_status",
    "side record_id status severity link partners group_id instrument trader broker created_at inserted_at "
    "deadline first_status_at resolved_by last_event_id what",
    "ON CONFLICT (side, record_id) "
    + _overwrite("status severity link partners group_id first_status_at resolved_by last_event_id what"),
)
INSERT_LINK = _insert("recon_links", "internal_id confirmation_id link_type group_id", "ON CONFLICT DO NOTHING")
UPSERT_CHECKPOINT = _insert(
    "recon_checkpoint",
    "id steps finalized rule_version input_digest mode payload",
    "ON CONFLICT (id) " + _overwrite("steps finalized rule_version input_digest mode payload"),
)
INSERT_STEP = _insert("recon_steps", "step kind side ingest_id tick_at")
INSERT_ACTION = _insert("ops_actions", "action_id action after_step actor reason payload")


class AnotherWriterRunning(RuntimeError):
    """A second process tried to write results while another one holds the writer lock."""


class PostgresStore:
    def __init__(self, dsn: str):
        import psycopg  # imported here so file mode never needs the driver
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        self._psycopg, self._dict_row, self._Jsonb = psycopg, dict_row, Jsonb
        self.dsn = dsn
        self.holds_writer_lock = False
        self._connect()

    def _connect(self) -> None:
        # Autocommit: a single statement commits itself, several go inside conn.transaction().
        self.conn = self._psycopg.connect(self.dsn, autocommit=True)
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(%s)", [SCHEMA_LOCK_KEY])
            self.conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))

    def _rows(self, sql: str, params=None) -> list[dict]:
        with self.conn.cursor(row_factory=self._dict_row) as cur:
            return cur.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()  # closing the session also releases the writer lock

    def reconnect(self) -> None:
        """Open a fresh connection after a failure, and take the writer lock again if we held it."""
        self.close()
        self._connect()  # may fail while the database is down: holds_writer_lock stays set for the retry
        if self.holds_writer_lock:
            self.holds_writer_lock = False
            self.acquire_writer_lock()

    def acquire_writer_lock(self) -> None:
        """Session-level advisory lock: held until this connection closes, released automatically
        if the process dies. A second writer is refused instead of corrupting the results."""
        if not self.conn.execute("SELECT pg_try_advisory_lock(%s)", [WRITER_LOCK_KEY]).fetchone()[0]:
            raise AnotherWriterRunning("another reconciliation process is already writing to this database")
        self.holds_writer_lock = True

    # ---- Operations requests (live mode)
    def submit_request(self, action: dict) -> int:
        return self.conn.execute(
            "INSERT INTO ops_requests (action, payload) VALUES (%s, %s) RETURNING request_id",
            [action["action"], self._Jsonb(action)],
        ).fetchone()[0]

    def pending_requests(self) -> list[tuple[int, dict]]:
        rows = self._rows("SELECT request_id, payload FROM ops_requests WHERE status = 'pending' ORDER BY request_id")
        return [(r["request_id"], r["payload"]) for r in rows]

    def request_status(self, request_id: int) -> dict:
        rows = self._rows("SELECT status, result, action_id FROM ops_requests WHERE request_id = %s", [request_id])
        return rows[0] if rows else {}

    def reject_request(self, request_id: int, error: str) -> None:
        self.conn.execute(
            "UPDATE ops_requests SET status = 'rejected', result = %s, processed_at = now() WHERE request_id = %s",
            [error, request_id],
        )

    # ---- landing tables
    def load_landing(self, side: str, rows: list[tuple[int, dict]], source_file: str) -> int:
        """Insert raw CSV rows (row number, dict). Re-loading the same file inserts nothing."""
        columns = required_columns(side)
        sql = _insert(
            LANDING_TABLE[side],
            " ".join(columns) + " source_file source_row",
            "ON CONFLICT (source_file, source_row) DO NOTHING",
        )
        params = [
            {**{c: raw.get(c) for c in columns}, "source_file": source_file, "source_row": row_no}
            for row_no, raw in rows
        ]
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.executemany(sql, params)
            return cur.rowcount

    def read_landing(self, side: str, since=None) -> list[tuple[int, int, dict]]:
        """(ingest_id, source_row, raw dict) in ingest order: every row, or the rows loaded at or after
        `since` (database time)."""
        where = "" if since is None else "WHERE loaded_at >= %s "
        rows = self._rows(
            f"SELECT ingest_id, source_row, {', '.join(required_columns(side))} "
            f"FROM {LANDING_TABLE[side]} {where}ORDER BY ingest_id",
            None if since is None else [since],
        )
        return [(r.pop("ingest_id"), r.pop("source_row"), r) for r in rows]

    def db_now(self):
        """The database's own clock, so the live re-read window does not depend on the app server's clock."""
        return self.conn.execute("SELECT clock_timestamp()").fetchone()[0]

    # ---- contract
    def reset(self, keep_actions: bool = True) -> None:
        tables = [
            "recon_status_events",
            "recon_current_status",
            "recon_links",
            "recon_checkpoint",
            "recon_steps",
            "quarantine",
        ] + ([] if keep_actions else ["ops_actions", "ops_requests"])
        self.conn.execute(f"TRUNCATE {', '.join(tables)}")

    def load_checkpoint(self) -> dict | None:
        rows = self._rows("SELECT payload FROM recon_checkpoint WHERE id = 1")
        return rows[0]["payload"] if rows else None

    def load_events(self) -> list[Event]:
        rows = self._rows("SELECT payload FROM recon_status_events ORDER BY seq")
        return [Event.from_dict(r["payload"]) for r in rows]

    def load_manual_actions(self) -> list[dict]:
        return [r["payload"] for r in self._rows("SELECT payload FROM ops_actions ORDER BY action_id")]

    def load_steps(self) -> list[dict]:
        return self._rows("SELECT step, kind, side, ingest_id, tick_at FROM recon_steps ORDER BY step")

    def _write(self, cur, events: list[Event], dirty: list[dict], checkpoint: dict) -> None:
        payloads = [e.to_dict() for e in events]
        cur.executemany(INSERT_EVENT, [{**d, "payload": self._Jsonb(d)} for d in payloads])
        cur.executemany(
            INSERT_QUARANTINE,
            [
                {
                    "event_id": e.event_id,
                    "side": e.side,
                    "source_row": e.details.get("source_row"),
                    "raw": self._Jsonb(e.details.get("raw", {})),
                    "reason": e.what,
                }
                for e in events
                if e.kind == "QUARANTINE"
            ],
        )
        if dirty:
            cur.executemany(UPSERT_CURRENT, dirty)
            cur.execute(
                "DELETE FROM recon_links WHERE internal_id = ANY(%s) OR confirmation_id = ANY(%s)",
                [
                    [r["record_id"] for r in dirty if r["side"] == INTERNAL],
                    [r["record_id"] for r in dirty if r["side"] == CONFIRMATION],
                ],
            )
            cur.executemany(INSERT_LINK, self._link_rows(dirty))
        cur.execute(
            UPSERT_CHECKPOINT,
            {
                "id": 1,
                "finalized": False,
                "input_digest": None,
                "mode": "replay",
                **checkpoint,
                "payload": self._Jsonb(checkpoint),
            },
        )

    @staticmethod
    def _link_rows(dirty: list[dict]) -> list[dict]:
        by_key = {(r["side"], r["record_id"]): r for r in dirty}
        rows = []
        for row in dirty:
            for partner in row["partners"] if row["link"] else ():
                i, c = (row["record_id"], partner) if row["side"] == INTERNAL else (partner, row["record_id"])
                other = by_key.get((OTHER[row["side"]], partner))
                # A manually accepted group keeps its many-to-one shape; only 1:1 links must stay unique.
                group = row["link"] == "manual" and (len(row["partners"]) > 1 or (other and len(other["partners"]) > 1))
                rows.append(
                    {
                        "internal_id": i,
                        "confirmation_id": c,
                        "group_id": row["group_id"],
                        "link_type": "manual_group" if group else row["link"],
                    }
                )
        return rows

    def commit_step(
        self, events: list[Event], dirty: list[dict], checkpoint: dict, step_row: dict | None = None
    ) -> None:
        with self.conn.transaction(), self.conn.cursor() as cur:
            if step_row is not None:
                cur.execute(INSERT_STEP, step_row)
            self._write(cur, events, dirty, checkpoint)

    def commit_action(
        self, action: dict, events: list[Event], dirty: list[dict], checkpoint: dict, request_id: int | None = None
    ) -> None:
        """The decision, its events and (live mode) the request's outcome land together or not at all."""
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(INSERT_ACTION, {"actor": None, "reason": None, **action, "payload": self._Jsonb(action)})
            self._write(cur, events, dirty, checkpoint)
            if request_id is not None:
                cur.execute(
                    "UPDATE ops_requests SET status = 'applied', action_id = %s, result = %s, "
                    "processed_at = now() WHERE request_id = %s",
                    [action["action_id"], f"{len(events)} event(s)", request_id],
                )
