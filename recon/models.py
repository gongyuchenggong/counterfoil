"""Core data types: input records, rejected rows and status events."""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

INTERNAL = "internal"
CONFIRMATION = "confirmation"
SIDES = (INTERNAL, CONFIRMATION)
OTHER = {INTERNAL: CONFIRMATION, CONFIRMATION: INTERNAL}
SIDE_LETTER = {INTERNAL: "I", CONFIRMATION: "C"}
SIDE_ORDER = {INTERNAL: 0, CONFIRMATION: 1}
ID_COLUMN = {INTERNAL: "trade_id", CONFIRMATION: "confirmation_id"}
LABEL = {INTERNAL: "trade", CONFIRMATION: "confirmation"}

FIELDS = ("instrument", "price", "volume", "fee", "currency", "trader", "broker")
DATA_COLUMNS = ("inserted_at", "created_at") + FIELDS
NUMERIC_FIELDS = ("price", "volume", "fee")

# Statuses
PENDING = "PENDING"
PROCESSED = "PROCESSED"
BREAK = "BREAK"
UNCONFIRMED = "UNCONFIRMED"
UNBOOKED = "UNBOOKED"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
QUARANTINED = "QUARANTINED"
EXCEPTION_STATUSES = (BREAK, UNCONFIRMED, UNBOOKED, POSSIBLE_DUPLICATE)

# Severities
HIGH, MEDIUM, LOW, INFO = "HIGH", "MEDIUM", "LOW", "INFO"
SEVERITY_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1, INFO: 0, None: -1}


def required_columns(side: str) -> tuple:
    return (ID_COLUMN[side],) + DATA_COLUMNS


def max_severity(*values):
    return max(values, key=lambda v: SEVERITY_RANK.get(v, -1)) if values else None


def fmt_ts(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None


def parse_ts(text: str | None) -> datetime | None:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S") if text else None


def fmt_num(value: Decimal) -> str:
    """Show numbers the way people type them: 457, 245.7, 7.5."""
    return format(value.normalize(), "f")


def fmt_duration(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes} min"


def pair_label(side: str, record_id, partners=()) -> str:
    """'trade 1', or 'trade 1 / confirmation 2' when it is linked to a counterpart."""
    me = f"{LABEL[side]} {record_id}"
    return " / ".join([me, ", ".join(f"{LABEL[OTHER[side]]} {p}" for p in partners)]) if partners else me


@dataclass(frozen=True, eq=False)
class Record:
    """One validated, normalized row from either feed."""

    side: str
    record_id: int
    inserted_at: datetime
    created_at: datetime
    instrument: str  # upper case, mapping applied
    price: Decimal
    volume: Decimal
    fee: Decimal
    currency: str
    trader: str  # whitespace collapsed, mapping applied
    broker: str
    raw: dict  # original strings as received
    source_row: int = 0
    mapped: tuple = ()  # fields changed by a mapping table, e.g. ("instrument: NVDA -> NVIDIA",)

    @property
    def trade_date(self) -> date:
        return self.created_at.date()

    def cmp(self, name: str):
        """Comparison value of a field. Names compare case-insensitively."""
        if name == "trade_date":
            return self.trade_date
        value = getattr(self, name)
        if name in ("trader", "broker"):
            return value.casefold()
        return value

    @property
    def coarse_key(self) -> tuple:
        """Everything except the numeric fields. Used to index candidates."""
        return (self.trade_date, self.instrument, self.currency, self.trader.casefold(), self.broker.casefold())

    @property
    def fingerprint(self) -> tuple:
        """Business content. inserted_at is left out: a resend naturally gets a new receipt time."""
        return tuple(sorted((k, v) for k, v in self.raw.items() if k != "inserted_at"))

    @property
    def label(self) -> str:
        return f"{LABEL[self.side]} {self.record_id}"

    @property
    def order_key(self) -> tuple:
        return (self.inserted_at, SIDE_ORDER[self.side], 0, self.record_id)

    def summary(self) -> dict:
        return {
            "inserted_at": fmt_ts(self.inserted_at),
            "created_at": fmt_ts(self.created_at),
            "instrument": self.instrument,
            "price": fmt_num(self.price),
            "volume": fmt_num(self.volume),
            "fee": fmt_num(self.fee),
            "currency": self.currency,
            "trader": self.trader,
            "broker": self.broker,
        }


@dataclass(frozen=True, eq=False)
class BadRow:
    """A row that failed validation. It is quarantined, never silently dropped."""

    side: str
    source_row: int
    raw: dict
    reasons: tuple
    inserted_at: datetime | None = None

    @property
    def order_key(self) -> tuple:
        return (self.inserted_at or datetime.min, SIDE_ORDER[self.side], 1, self.source_row)

    @property
    def record_id_text(self) -> str:
        return str(self.raw.get(ID_COLUMN[self.side], "")).strip()


@dataclass
class Event:
    """One status change or notice. Append-only; never rewritten."""

    event_id: str
    seq: int
    sim_time: str | None
    kind: str  # STATUS, DATA_QUALITY, QUARANTINE, NOTE, SNAPSHOT
    side: str | None = None
    record_id: int | None = None
    status: str | None = None
    prev_status: str | None = None
    counterparty_ids: list = field(default_factory=list)
    link: str | None = None  # hard, soft, aggregate, ambiguous, manual
    severity: str | None = None
    mismatches: list = field(default_factory=list)
    what: str = ""
    why: str = ""
    next: str = ""
    note: str = ""
    resolved_by: str | None = None  # engine or manual
    pair_key: str | None = None  # groups the sides of one match or break
    late_minutes: int | None = None
    actor: str | None = None
    reason: str | None = None
    record: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    rule_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)
