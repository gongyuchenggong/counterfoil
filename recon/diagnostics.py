"""All Operations wording: what is wrong, why, what next.

Every function here is pure. The engine decides *when* to call them.
"""

from collections.abc import Sequence
from decimal import Decimal
from difflib import SequenceMatcher

from .config import Config
from .models import (
    CONFIRMATION,
    HIGH,
    INFO,
    INTERNAL,
    LOW,
    MEDIUM,
    BadRow,
    Record,
    fmt_duration,
    fmt_num,
    fmt_ts,
    max_severity,
)


def _mins(seconds: float) -> int:
    return round(seconds / 60)


def _value(rec: Record, name: str) -> str:
    if name == "trade_date":
        return rec.trade_date.isoformat()
    value = getattr(rec, name)
    return fmt_num(value) if name in ("price", "volume", "fee") else str(value)


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def _digits(value) -> str:
    return "".join(ch for ch in fmt_num(value) if ch.isdigit()).lstrip("0")


# ---------------------------------------------------------------- field rules


def _price(i: Record, c: Record, cfg: Config) -> tuple:
    a, b = i.price, c.price
    hi, lo = max(a, b), min(a, b)
    ratio = hi / lo
    if ratio in (10, 100, 1000):
        why = f"Exactly {fmt_num(ratio)}x apart: decimal shift."
    elif sorted(_digits(a)) == sorted(_digits(b)):
        why = "Same digits reordered: transposed digits."
    else:
        why = f"{(hi - lo) / lo * 100:.2f}% apart: wrong or stale price."
    return HIGH, why, "Check the chat log for the agreed price. Amend the booking, or ask the broker to reissue."


def _volume(i: Record, c: Record, cfg: Config) -> tuple:
    a, b = i.volume, c.volume
    hi, lo = max(a, b), min(a, b)
    diff = hi - lo
    if diff <= max(2, hi / 100):
        why = f"Only {fmt_num(diff)} apart: typing error."
    elif (hi / lo) == (hi / lo).to_integral_value():
        why = f"Exactly {fmt_num(hi / lo)}x apart: lots and units mixed up."
    else:
        why = "Far apart: partial fill or wrong size."
    return HIGH, why, "Check the chat log for the agreed size and amend the wrong side. Position is wrong until fixed."


def _fee(i: Record, c: Record, cfg: Config) -> tuple:
    a, b = i.fee, c.fee
    if a == 0 or b == 0:
        why = f"Zero on the {'booking' if a == 0 else 'confirmation'}: probably left out."
    elif abs(a - b) <= Decimal("0.01"):
        why = "At most 0.01 apart: rounding."
    else:
        per_unit = Decimal("0.0001")
        why = (
            f"Per unit: booking {fmt_num((a / i.volume).quantize(per_unit))}, "
            f"broker {fmt_num((b / c.volume).quantize(per_unit))}. Wrong commission rate or old fee schedule."
        )
    return MEDIUM, why, f"Check the commission agreed with {c.broker}. Amend the booking, or ask the broker to reissue."


def _currency(i: Record, c: Record, cfg: Config) -> tuple:
    why = f"Booked in {i.currency}, confirmed in {c.currency}: possibly a default currency left unchanged."
    nxt = f"Check the agreed currency with {i.trader} and {c.broker}, then amend the wrong side."
    return HIGH, why, nxt


def _broker(i: Record, c: Record, cfg: Config) -> tuple:
    why = f"Booked with {i.broker}, confirmed by {c.broker}. Wrong broker probably selected."
    nxt = f"Ask {i.trader} which broker executed. Amend the booking, or contact {c.broker} if the trade is not theirs."
    return HIGH, why, nxt


def _trader(i: Record, c: Record, cfg: Config) -> tuple:
    score = SequenceMatcher(None, i.trader.casefold(), c.trader.casefold()).ratio()
    if score >= cfg.rules.trader_typo_similarity:
        return (
            LOW,
            f"Names {score:.0%} similar: spelling error.",
            "Check the trader's name internally and update it. Add it to mappings.trader if it recurs.",
        )
    return (
        MEDIUM,
        "Different people: possibly booked under a colleague.",
        "Ask the desk who executed and amend the wrong side.",
    )


def _instrument(i: Record, c: Record, cfg: Config) -> tuple:
    a, b = i.instrument, c.instrument
    hints = cfg.reference.instrument_hints
    same = (
        hints.get(a) == b
        or hints.get(b) == a
        or (min(len(a), len(b)) >= 3 and (_is_subsequence(a, b) or _is_subsequence(b, a)) and a[0] == b[0])
    )
    if same:
        return (
            LOW,
            f"{a} and {b}: same product, ticker versus name.",
            f"Confirm, then add '{b}: {a}' to mappings.instrument so it matches next time.",
        )
    return (
        HIGH,
        f"{a} and {b} are different products.",
        "Check the chat log for the agreed instrument and amend the wrong side.",
    )


def _trade_date(i: Record, c: Record, cfg: Config) -> tuple:
    for rec, other in ((c, i), (i, c)):
        if rec.created_at - rec.inserted_at > cfg.clock_skew_tolerance:
            gap = _mins(
                abs(
                    (
                        rec.created_at.replace(year=2000, month=1, day=1)
                        - other.created_at.replace(year=2000, month=1, day=1)
                    ).total_seconds()
                )
            )
            who = "Broker" if rec.side == CONFIRMATION else "Booking"
            why = (
                f"{rec.label.capitalize()} traded {fmt_ts(rec.created_at)}, after it was received "
                f"({fmt_ts(rec.inserted_at)}): impossible. "
                f"{who} date typo likely. Time of day is {fmt_duration(gap)} from the other side."
            )
            if rec.side == CONFIRMATION:
                nxt = f"Ask {rec.broker} to reissue with trade date {other.trade_date.isoformat()}."
            else:
                nxt = f"Amend the booking's trade date to {other.trade_date.isoformat()}."
            return MEDIUM, why, nxt
    days = abs((i.trade_date - c.trade_date).days)
    return (
        MEDIUM,
        f"{days} day(s) apart: late booking after midnight, or time zones.",
        "Check the chat log and amend the wrong date.",
    )


RULES = {
    "price": _price,
    "volume": _volume,
    "fee": _fee,
    "currency": _currency,
    "broker": _broker,
    "trader": _trader,
    "instrument": _instrument,
    "trade_date": _trade_date,
}


FIELD_NAMES = {"trade_date": "trade date"}


def diagnose_field(name: str, i: Record, c: Record, cfg: Config) -> dict:
    severity, why, nxt = RULES[name](i, c, cfg)
    return {
        "field": name,
        "internal": _value(i, name),
        "confirmation": _value(c, name),
        "severity": severity,
        "why": why,
        "next": nxt,
    }


# ---------------------------------------------------------------- status messages


def break_message(i: Record, c: Record, fields: list[str], cfg: Config) -> dict:
    diags = [diagnose_field(name, i, c, cfg) for name in fields]
    parts = [
        f"{FIELD_NAMES.get(d['field'], d['field'])} differs: trade {d['internal']}, confirmation {d['confirmation']}"
        for d in diags
    ]
    what = "; ".join(parts) + (
        ". All other terms agree." if len(fields) == 1 else f". The other {8 - len(fields)} terms agree."
    )
    return {
        "severity": max_severity(*(d["severity"] for d in diags)),
        "what": what,
        "why": " ".join(d["why"] for d in diags),
        "next": " ".join(dict.fromkeys(d["next"] for d in diags)),
        "mismatches": diags,
        "headline": "; ".join(
            f"{FIELD_NAMES.get(d['field'], d['field'])} {d['internal']} vs {d['confirmation']}" for d in diags
        ),
    }


def ambiguous_message(rec: Record, options: list, cfg: Config) -> dict:
    """options: list of (candidate Record, mismatch field list)."""
    pieces, severities = [], []
    for cand, fields in options:
        i, c = (rec, cand) if rec.side == INTERNAL else (cand, rec)
        diag = break_message(i, c, fields, cfg)
        severities.append(diag["severity"])
        pieces.append(f"{cand.label} ({', '.join(fields)} differ{'s' if len(fields) == 1 else ''})")
    if len(options) == 1:
        what = f"Closest is {pieces[0]}, but another record fits equally well."
        headline = "1 possible counterpart, another record also fits, none picked"
    else:
        what = f"{len(options)} possible counterparts, equally close: " + "; ".join(pieces) + "."
        headline = f"{len(options)} possible counterparts, none picked"
    return {
        "severity": max_severity(*severities),
        "what": what,
        "why": "Cannot tell which pairing is right, so none is picked.",
        "next": "Check the chat log and link the right pair with force-match.",
        "mismatches": [],
        "headline": headline,
    }


def aggregate_message(single: Record, parts: list[Record], alternatives: int) -> dict:
    labels = ", ".join(p.label for p in parts)
    vols = " + ".join(fmt_num(p.volume) for p in parts)
    fees = " + ".join(fmt_num(p.fee) for p in parts)
    many = "trades" if single.side == CONFIRMATION else "confirmations"
    what = (
        f"{len(parts)} {many} ({labels}) add up to {single.label}: volume {vols} = {fmt_num(single.volume)}, "
        f"fees {fees} = {fmt_num(single.fee)}, weighted price rounds to {fmt_num(single.price)}."
    )
    if single.side == CONFIRMATION:
        why = "Broker likely aggregated fills into one confirmation."
    else:
        why = "Broker likely split one trade into several confirmations."
    if alternatives:
        why += f" {alternatives} other grouping(s) of the same size also fit."
    return {
        "severity": MEDIUM,
        "what": what,
        "why": why,
        "next": "Confirm the grouping with the trader, then accept it or ask the broker to reissue.",
        "headline": f"aggregation: {len(parts)} records add up to {single.label}",
        "mismatches": [
            {
                "field": "volume",
                "internal": "",
                "confirmation": "",
                "severity": MEDIUM,
                "why": "aggregation",
                "next": "",
            }
        ],
    }


def _identical_text(rec: Record, dup: Record, partner: Record | None) -> str:
    gap = _mins(abs((rec.created_at - dup.created_at).total_seconds()))
    booked_gap = _mins(abs((rec.inserted_at - dup.inserted_at).total_seconds()))
    matched = f", matched with {partner.label}" if partner is not None else ""
    when = "in the same minute" if gap == 0 else f"{fmt_duration(gap)} apart"
    return f"Identical to {dup.label}{matched}: traded {when}, booked {fmt_duration(booked_gap)} apart."


def unconfirmed_message(
    rec: Record,
    cfg: Config,
    manual_hit: Record | None = None,
    duplicate_of: Record | None = None,
    dup_partner: Record | None = None,
) -> dict:
    what = (
        f"No confirmation {cfg.deadline_label} after booking, and none within "
        f"{cfg.rules.partial_match.max_mismatches} differences."
    )
    why = f"{rec.broker} has not sent it, wrong broker booked, or trade not done."
    nxt = f"Chase {rec.broker} and ask {rec.trader} if the trade was done."
    headline = f"no confirmation from {rec.broker} after {cfg.deadline_label}"
    if duplicate_of is not None:  # outside the duplicate window: the data cannot say which it is
        gap = fmt_duration(_mins(abs((rec.created_at - duplicate_of.created_at).total_seconds())))
        what += f" {_identical_text(rec, duplicate_of, dup_partner)}"
        why = f"Same terms, {gap} apart: second real trade, or a duplicate booking."
        nxt = f"Ask {rec.trader} how many were done. If two, chase {rec.broker}; if one, cancel the duplicate."
        headline += f"; identical to {duplicate_of.label}, {gap} apart"
    if manual_hit is not None:
        why += f" Exactly matches {manual_hit.label}, already resolved manually."
    return {"severity": HIGH, "what": what, "why": why, "next": nxt, "mismatches": [], "headline": headline}


def unbooked_message(
    rec: Record,
    cfg: Config,
    duplicate_of: Record | None = None,
    dup_partner: Record | None = None,
    manual_hit: Record | None = None,
) -> dict:
    what = f"No booking {cfg.deadline_label} after the confirmation arrived."
    headline = f"no booking by {rec.trader} after {cfg.deadline_label}"
    if duplicate_of is None:
        why = f"{rec.trader} may have forgotten it, or booked it elsewhere."
        nxt = f"Contact {rec.trader} now: position may be wrong. Book it, or link it with force-match."
    else:
        headline += f"; identical to {duplicate_of.label} (resend or second trade?)"
        partner = f" (matched with {dup_partner.label})" if dup_partner is not None else ""
        what += f" Identical confirmation {duplicate_of.label}{partner} is already matched."
        same_time = duplicate_of.created_at == rec.created_at
        close = abs(duplicate_of.inserted_at - rec.inserted_at) <= cfg.duplicate_window
        lean = (
            "Same trade time, received close together: leans resend."
            if same_time and close
            else "Different trade times: leans second real trade."
        )
        why = (
            f"Broker resend, or a second trade left unbooked: the data cannot tell. {lean} "
            "Stays HIGH: a missing booking means a wrong position."
        )
        nxt = (
            f"Ask {rec.trader} how many trades were done. If two, book the second. "
            f"If one, ask {rec.broker} to cancel {rec.label}."
        )
    if manual_hit is not None:
        why += f" Exactly matches {manual_hit.label}, already resolved manually."
    return {"severity": HIGH, "what": what, "why": why, "next": nxt, "mismatches": [], "headline": headline}


def duplicate_message(rec: Record, dup: Record, partner: Record | None) -> dict:
    """Identical terms within the duplicate window. Further apart, see unconfirmed_message."""
    whole_minutes = int(abs((rec.created_at - dup.created_at).total_seconds()) // 60)
    traded = "in the same minute" if whole_minutes == 0 else f"{whole_minutes} min apart"
    return {
        "severity": HIGH,
        "what": f"No confirmation. {_identical_text(rec, dup, partner)}",
        "why": "Same terms, almost the same time: likely booked twice.",
        "next": f"Ask {rec.trader} to confirm, then cancel the duplicate.",
        "mismatches": [],
        "headline": f"identical to {dup.label}, traded {traded}",
    }


def match_message(
    rec: Record,
    other: Record,
    cfg: Config,
    *,
    resolved: str | None = None,
    late_minutes: int | None = None,
    tolerance_fields: Sequence[str] = (),
    manual_by: str | None = None,
    reason: str | None = None,
) -> dict:
    """`resolved` is the exception status this match clears, if any."""
    notes = []
    if resolved:
        late = ""
        if late_minutes and late_minutes > 0:
            late = f" {fmt_duration(late_minutes)} after the {cfg.deadline_label} deadline"
        who = "confirmation" if other.side == CONFIRMATION else "booking"
        notes.append(f"resolved: {rec.label} was {resolved}; the {who} arrived{late}")
    if tolerance_fields and not manual_by:
        notes.append(f"matched within tolerance on {', '.join(tolerance_fields)}")
    if rec.mapped:
        notes.append("matched via mapping table (" + "; ".join(rec.mapped) + ")")
    if manual_by:
        notes.append(f"manual force-match by {manual_by}: {reason}")
    return {
        "severity": INFO if resolved or manual_by else None,
        "what": f"Matched with {other.label}.",
        "note": "; ".join(notes),
        "headline": f"matched with {other.label}",
    }


def accepted_message(actor: str, prev_status: str, reason: str) -> dict:
    return {
        "severity": INFO,
        "what": f"Accepted by {actor} (was {prev_status}).",
        "note": f"manual accept by {actor}: {reason}",
    }


def note_message(actor: str, text: str) -> dict:
    return {"severity": INFO, "what": f"Note by {actor}: {text}", "why": "", "next": ""}


def manual_hit_message(rec: Record) -> dict:
    return {
        "severity": INFO,
        "what": f"{rec.label} exactly matches this manually resolved record.",
        "why": "Manual decisions take priority: record unchanged.",
        "next": "Check the decision still holds; re-link with force-match if not.",
    }


def resend_message(rec: Record) -> dict:
    return {
        "severity": INFO,
        "what": f"{rec.label} delivered again, identical (row {rec.source_row}).",
        "why": "Feed re-delivery.",
        "next": "No action: ignored, nothing counted twice.",
    }


def conflict_message(existing: Record, rec: Record) -> dict:
    changes = [
        f"{k}: {existing.raw.get(k)} -> {v}"
        for k, v in rec.raw.items()
        if existing.raw.get(k) != v and k != "inserted_at"
    ]
    return {
        "severity": HIGH,
        "what": f"{rec.label} arrived again, changed (row {rec.source_row}): " + "; ".join(changes) + ".",
        "why": "Likely an amendment or system error. Not applied.",
        "next": "Quarantined. Ask the data owner which version is valid.",
    }


def bad_row_message(row: BadRow) -> dict:
    return {
        "severity": HIGH,
        "what": f"Row {row.source_row} of the {row.side} feed rejected: {'; '.join(row.reasons)}.",
        "why": "Cannot be reconciled until fixed.",
        "next": "Quarantined. Ask the data owner to correct and resend.",
    }


def clock_skew_message(rec: Record, cfg: Config, partner: Record | None = None) -> dict:
    """States what is known. The cause is not known: the data has no way to separate clock drift from a typo."""
    secs = int((rec.created_at - rec.inserted_at).total_seconds())
    what = f"Trade time is {secs}s after receipt, within the {cfg.rules.clock_skew_tolerance_minutes} min tolerance."
    if partner is not None:
        apart = abs((rec.created_at - partner.created_at).total_seconds()) / 60
        what += f" {partner.label.capitalize()} was timed {apart:.1f} min away."
    return {
        "severity": INFO,
        "what": what,
        "why": "Clock drift or a mistyped time: the data cannot tell.",
        "next": "No action on the match. Check the time if it repeats.",
    }


def future_dated_message(rec: Record) -> dict:
    gap = _mins((rec.created_at - rec.inserted_at).total_seconds())
    return {
        "severity": MEDIUM,
        "what": f"Trade time {fmt_ts(rec.created_at)} is {fmt_duration(gap)} after receipt "
        f"({fmt_ts(rec.inserted_at)}).",
        "why": "A trade cannot be dated after its receipt: date probably wrong.",
        "next": "Trade date is suspect: watch for a related BREAK.",
    }
