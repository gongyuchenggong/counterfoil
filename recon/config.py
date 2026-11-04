"""Rules and reference data from config.yaml, validated on load.

A misspelt key or a wrong type is refused instead of silently falling back to a default.
The rule_version hash is stamped on every status event, so an audit can tell which rules produced it.
"""

import hashlib
from datetime import timedelta
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, PositiveInt, field_validator

# Bump when matching or diagnosis logic changes in code.
RULES_CODE_VERSION = "1.4"


def norm_text(value) -> str:
    """Trim and collapse inner whitespace. Formatting only, never meaning."""
    return " ".join(str(value).split())


def _upper_table(table) -> dict:
    return {norm_text(k).upper(): norm_text(v).upper() for k, v in (table or {}).items()}


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PartialMatch(Section):
    max_mismatches: int = 2
    date_window_days: int = 1


class Aggregation(Section):
    enabled: bool = True
    max_group_size: int = 5
    max_candidates: int = 12


class Tolerances(Section):
    price: Decimal = Decimal(0)
    volume: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)


class Rules(Section):
    status_deadline_minutes: PositiveInt = 120
    partial_match: PartialMatch = PartialMatch()
    aggregation: Aggregation = Aggregation()
    tolerances: Tolerances = Tolerances()
    duplicate_window_minutes: int = 5
    clock_skew_tolerance_minutes: int = 5
    trader_typo_similarity: float = 0.8


class Mappings(Section):
    """Aliases confirmed by Operations. Keys are normalized: upper case for instruments, casefold for names."""

    instrument: dict[str, str] = {}
    trader: dict[str, str] = {}
    broker: dict[str, str] = {}

    _instrument = field_validator("instrument", mode="before")(_upper_table)

    @field_validator("trader", "broker", mode="before")
    @classmethod
    def _names(cls, table) -> dict:
        return {norm_text(k).casefold(): norm_text(v) for k, v in (table or {}).items()}


class Reference(Section):
    instrument_hints: dict[str, str] = {}
    currencies: tuple[str, ...] = ("USD", "EUR", "GBP")

    _hints = field_validator("instrument_hints", mode="before")(_upper_table)

    @field_validator("currencies", mode="before")
    @classmethod
    def _upper_currencies(cls, codes) -> tuple:
        return tuple(str(code).upper() for code in codes or ())


class Alerts(Section):
    immediate: tuple[Literal["HIGH", "MEDIUM", "LOW"], ...] = ("HIGH",)


class Config(Section):
    rules: Rules = Rules()
    mappings: Mappings = Mappings()
    reference: Reference = Reference()
    alerts: Alerts = Alerts()

    @cached_property
    def rule_version(self) -> str:
        # Alert routing changes who is told, not what the status is, so it stays out of the hash.
        digest = hashlib.sha256(self.model_dump_json(exclude={"alerts"}).encode()).hexdigest()[:8]
        return f"rules-{RULES_CODE_VERSION}+cfg-{digest}"

    @property
    def deadline(self) -> timedelta:
        return timedelta(minutes=self.rules.status_deadline_minutes)

    @property
    def deadline_label(self) -> str:
        minutes = self.rules.status_deadline_minutes
        return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes} min"

    @property
    def duplicate_window(self) -> timedelta:
        return timedelta(minutes=self.rules.duplicate_window_minutes)

    @property
    def clock_skew_tolerance(self) -> timedelta:
        return timedelta(minutes=self.rules.clock_skew_tolerance_minutes)


def build_config(data: dict | None = None) -> Config:
    return Config.model_validate(data or {})


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as fh:
        return build_config(yaml.safe_load(fh))
