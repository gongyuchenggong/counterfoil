"""Human and machine readable outputs, all rebuilt from the event log.

Because reports are derived from the stored events (not printed while running),
a resumed run produces exactly the same files as an uninterrupted one.
"""

import base64
import csv
import hashlib
import html
import json
import re
import statistics
import textwrap
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader

from .config import Config
from .models import (
    CONFIRMATION,
    EXCEPTION_STATUSES,
    INTERNAL,
    LABEL,
    OTHER,
    PROCESSED,
    SEVERITY_RANK,
    SIDES,
    Event,
    fmt_duration,
    fmt_ts,
    pair_label,
    parse_ts,
)

WIDTH = 110
RULE = "=" * WIDTH
STATUS_ORDER = ("PROCESSED", "BREAK", "UNCONFIRMED", "UNBOOKED", "POSSIBLE_DUPLICATE", "PENDING")
ROOT = Path(__file__).resolve().parent.parent
DIAGRAMS = Path(__file__).parent / "diagrams"  # made by tools/render_diagrams.py, named after their Mermaid source
TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent), autoescape=True, trim_blocks=True, lstrip_blocks=True
)


# ===================================================================== derived state
def records_seen(events: list[Event]) -> dict:
    """(side, id) -> record summary as received."""
    out = {}
    for e in events:
        if e.side and e.record_id is not None and e.record:
            out.setdefault((e.side, e.record_id), e.record)
    return out


def final_states(events: list[Event], deadline: timedelta) -> dict:
    """(side, id) -> current status and history, derived from STATUS events."""
    states: dict = {}
    for e in events:
        if e.kind != "STATUS":
            continue
        st = states.setdefault(
            (e.side, e.record_id),
            {
                "side": e.side,
                "record_id": e.record_id,
                "record": e.record,
                "first_status_at": e.sim_time,
                "exceptions": [],
            },
        )
        if e.status in EXCEPTION_STATUSES:
            st["exceptions"].append(e.status)
        st.update(
            status=e.status,
            severity=e.severity,
            link=e.link,
            partners=e.counterparty_ids,
            resolved_by=e.resolved_by,
            what=e.what,
            why=e.why,
            next=e.next,
            note=e.note,
            last_event_at=e.sim_time,
            late_minutes=e.late_minutes,
            pair_key=e.pair_key,
            details=e.details,
        )
    for st in states.values():
        st["deadline"] = parse_ts(st["record"]["inserted_at"]) + deadline
        st["sla_met"] = parse_ts(st["first_status_at"]) <= st["deadline"]
    return states


def status_totals(states: dict) -> dict:
    return {side: Counter(st["status"] for st in states.values() if st["side"] == side) for side in SIDES}


def open_cases(states: dict) -> list[dict]:
    """Open exceptions, one per case (a break pair or group counts once), most severe and oldest first."""
    cases = {}
    for st in states.values():
        if st["status"] in EXCEPTION_STATUSES:
            cases.setdefault(st["pair_key"] or (st["side"], st["record_id"]), st)
    return sorted(cases.values(), key=lambda st: (-SEVERITY_RANK[st["severity"]], st["record"]["inserted_at"]))


def lags(recs: dict, side: str, group_by: str, cfg: Config) -> tuple[dict, int]:
    """Minutes from trade time to receipt per broker or trader, and how many records were left out:
    those dated after their receipt (beyond clock skew) are data errors, not timeliness."""
    out, excluded = defaultdict(list), 0
    for (rec_side, _), rec in recs.items():
        if rec_side == side:
            lag = (parse_ts(rec["inserted_at"]) - parse_ts(rec["created_at"])).total_seconds() / 60
            if lag >= -cfg.rules.clock_skew_tolerance_minutes:
                out[rec[group_by]].append(lag)
            else:
                excluded += 1
    return out, excluded


def _as_of(events: list[Event]) -> str:
    return max((e.sim_time for e in events if e.sim_time), default="")


def _case_label(st: dict) -> str:
    return pair_label(st["side"], st["record_id"], st["partners"] if st["link"] in ("soft", "aggregate") else ())


# ===================================================================== text report
def _rec_line(side: str, rid, rec: dict) -> str:
    label = f"{LABEL[side]} {rid}"
    return (
        f"{label:<20}{rec['instrument']} {rec['volume']} @ {rec['price']} {rec['currency']}, fee {rec['fee']}, "
        f"{rec['trader']}, {rec['broker']}, traded {rec['created_at']}"
    )


def _wrap(label: str, text: str) -> list[str]:
    if not text:
        return []
    return textwrap.wrap(text, width=WIDTH, initial_indent=f"    {label:<5}: ", subsequent_indent=" " * 12)


def _explain(e: Event) -> list[str]:
    return _wrap("What", e.what) + _wrap("Why", e.why) + _wrap("Next", e.next)


def build_text_report(events: list[Event], cfg: Config) -> str:
    counts = _input_counts(events)
    minutes = cfg.rules.status_deadline_minutes
    lines = [
        RULE,
        "BROKER TRADES RECONCILIATION",
        RULE,
        f"Rules            : {cfg.rule_version}",
        f"Inputs           : {counts[INTERNAL]} internal trades, {counts[CONFIRMATION]} broker "
        f"confirmations, {counts['bad']} rejected rows",
        "Processing order : by inserted_at, one record at a time, as they arrive",
        f"Status deadline  : every record gets a definitive status within {minutes} "
        "minutes of becoming visible; it can be updated later",
        "Time in brackets : the engine's clock when the status was produced",
        RULE,
        "",
    ]
    recs = records_seen(events)
    lines += _final_summary(events, cfg, recs) + [RULE, "STATUS MESSAGES, IN TIME ORDER", RULE, ""]
    by_pair = defaultdict(list)
    for e in events:
        if e.pair_key:
            by_pair[(e.pair_key, e.sim_time, e.status)].append(e)
    printed = set()
    for e in events:
        if e.kind == "SNAPSHOT":
            lines += _snapshot_block(e)
        elif e.kind == "STATUS" and e.pair_key:
            key = (e.pair_key, e.sim_time, e.status)
            if key not in printed:
                printed.add(key)
                lines += _pair_block(by_pair[key])
        else:
            lines += _single_block(e, recs)
    return "\n".join(lines) + "\n"


def _pair_block(group: list[Event]) -> list[str]:
    group = sorted(group, key=lambda e: (e.side != INTERNAL, e.record_id))
    first = group[0]
    t = f"[{first.sim_time}]"
    if first.status == PROCESSED:
        if first.link == "manual" and first.pair_key.startswith("A"):
            who = " and ".join(f"{LABEL[e.side]} {e.record_id}" for e in group)
            return [f"{t} PROCESSED {who}: accepted manually by {first.actor} ({first.reason})", ""]
        internal = next(e for e in group if e.side == INTERNAL)
        conf = next(e for e in group if e.side == CONFIRMATION)
        notes = [e.note for e in group if e.note]
        line = f"{t} PROCESSED trade {internal.record_id}, matched with confirmation {conf.record_id}"
        return [f"{line}  ({'; '.join(dict.fromkeys(notes))})", ""] if notes else [line]
    # BREAK pair or aggregate group
    out = [f"{t} {first.status}  severity={first.severity}" + ("  [aggregation]" if first.link == "aggregate" else "")]
    out += ["    " + _rec_line(e.side, e.record_id, e.record) for e in group]
    return out + _explain(first) + [""]


def _single_block(e: Event, recs: dict) -> list[str]:
    t = f"[{e.sim_time or 'start'}]"
    if e.kind == "QUARANTINE" and e.record_id is None:
        out = [f"{t} QUARANTINED  severity={e.severity}  {e.side} feed, row {e.details.get('source_row')}"]
        return out + _explain(e) + [""]
    head = {"STATUS": e.status, "DATA_QUALITY": "DATA QUALITY", "QUARANTINE": "QUARANTINED"}.get(e.kind, e.kind)
    out = [
        f"{t} {head}  severity={e.severity}" if e.severity else f"{t} {head}",
        "    " + _rec_line(e.side, e.record_id, e.record),
    ]
    if e.kind == "STATUS" and e.link == "ambiguous":
        other = OTHER[e.side]
        out.append("    candidates:")
        out += [
            "    " + _rec_line(other, rid, recs[(other, rid)]) for rid in e.counterparty_ids if (other, rid) in recs
        ]
    details = e.details or {}
    dup = details.get("duplicate_of") or details.get("identical_confirmation")
    if dup is not None and (e.side, dup) in recs:
        out += ["    identical to:", "    " + _rec_line(e.side, dup, recs[(e.side, dup)])]
    return out + _explain(e) + [""]


def _snapshot_block(e: Event) -> list[str]:
    d = e.details
    title = f"END OF DAY {d['day']}" + ("  (end of data)" if d["final"] else "") + f"  as of {e.sim_time}"
    out = ["-" * WIDTH, title]
    for side in SIDES:
        c = d["counts"].get(side, {})
        parts = [f"{s} {c[s]}" for s in STATUS_ORDER if c.get(s)]
        out.append(f"  {LABEL[side] + 's':<14}: {sum(c.values()):>3} seen  |  " + "  ".join(parts))
    quarantined = f"  (quarantined rows: {d['quarantined_rows']})" if d["quarantined_rows"] else ""
    out.append(f"  Open records: {len(d['open'])}{quarantined}")
    for item in d["open"]:
        who = f"{LABEL[item['side']]} {item['record_id']}"
        out.append(
            f"    {item['severity']:<7}{item['status']:<20}{who:<20} open {fmt_duration(item['age_minutes']):>7}"
            f"   {item.get('headline') or item['what']}"
        )
    return out + ["-" * WIDTH, ""]


def _lag_stats(values: list[float], cfg: Config) -> str:
    over = sum(v > cfg.rules.status_deadline_minutes for v in values)
    return (
        f"median {statistics.median(values):6.0f} min   max {max(values):5.0f} min   "
        f"over {cfg.deadline_label} {over:>2}"
    )


def _answer(states: dict) -> list[str]:
    """The assignment's three questions: what is confirmed, what is not, what needs attention."""
    trades = status_totals(states)[INTERNAL]
    confirmed, total = trades[PROCESSED], sum(trades.values())
    cases = open_cases(states)
    severities = Counter(st["severity"] for st in cases)
    split = ", ".join(f"{severities[s]} {s}" for s in ("HIGH", "MEDIUM", "LOW") if severities[s])
    return [
        f"  Fully confirmed : {confirmed} of {total} trades",
        f"  Not confirmed   : {total - confirmed} trades",
        f"  Needs attention : {len(cases)} open cases" + (f" ({split})" if split else ""),
        "",
    ]


def _status_table(states: dict) -> list[str]:
    totals = status_totals(states)
    out = [f"  {'Status':<22}{'Internal trades':>16}{'Confirmations':>16}"]
    for status in STATUS_ORDER:
        if totals[INTERNAL][status] or totals[CONFIRMATION][status]:
            out.append(f"  {status:<22}{totals[INTERNAL][status]:>16}{totals[CONFIRMATION][status]:>16}")
    return [*out, f"  {'Total':<22}{sum(totals[INTERNAL].values()):>16}{sum(totals[CONFIRMATION].values()):>16}", ""]


def _deadline_section(events: list[Event], states: dict, cfg: Config) -> list[str]:
    met = sum(st["sla_met"] for st in states.values())
    out = [
        f"  Status deadline check : {met} of {len(states)} records got a definitive status within "
        f"{cfg.rules.status_deadline_minutes} minutes of becoming visible."
    ]
    out += [
        f"    MISSED: {LABEL[st['side']]} {st['record_id']} first status at {st['first_status_at']}"
        for st in states.values()
        if not st["sla_met"]
    ]
    resolved = [
        st for st in states.values() if st["status"] == PROCESSED and st["exceptions"] and st["resolved_by"] == "engine"
    ]
    if resolved:
        out.append(
            "  Resolved after an exception: "
            + ", ".join(f"{LABEL[st['side']]} {st['record_id']} (was {st['exceptions'][-1]})" for st in resolved)
        )
    manual = {
        (e.details or {}).get("action_id") for e in events if e.resolved_by == "manual" or e.kind == "NOTE" and e.actor
    }
    return [*out, f"  Manual actions recorded : {len(manual - {None})}", ""]


def _open_section(states: dict) -> list[str]:
    cases = open_cases(states)
    open_records = sum(st["status"] in EXCEPTION_STATUSES for st in states.values())
    out = [f"  Open exceptions: {len(cases)} cases covering {open_records} records"]
    for st in cases:
        headline = (st["details"] or {}).get("headline") or st["what"]
        out.append(f"    {st['severity']:<7}{st['status']:<20}{_case_label(st):<34}{headline}")
    return [*out, ""]


def _lag_section(recs: dict, cfg: Config) -> list[str]:
    out = []
    for title, side, group_by, noun in (
        ("Confirmation lag by broker (received minus trade time)", CONFIRMATION, "broker", "confirmations"),
        ("Booking lag by trader (booked minus trade time)", INTERNAL, "trader", "bookings"),
    ):
        out.append(f"  {title}")
        by_group, excluded = lags(recs, side, group_by, cfg)
        for name, values in sorted(by_group.items()):
            out.append(f"    {name:<26}{len(values):>3} {noun:<16}{_lag_stats(values, cfg)}")
        if excluded:
            out.append(f"    ({excluded} dated after receipt left out; see records to check)")
    return [*out, ""]


def _notices(events: list[Event]) -> list[Event]:
    return [e for e in events if e.kind in ("DATA_QUALITY", "QUARANTINE")]


def _notice_label(e: Event) -> str:
    if e.record_id is not None:
        return f"{LABEL[e.side]} {e.record_id}"
    return f"{e.side} row {e.details.get('source_row')}"


def _notices_section(events: list[Event]) -> list[str]:
    notices = _notices(events)
    out = [f"  Records to check: {len(notices)}"]
    for e in notices:
        out.append(f"    {e.severity:<7}{_notice_label(e):<22}{e.what[: WIDTH - 33]}")
    return [*out, ""]


def _final_summary(events: list[Event], cfg: Config, recs: dict) -> list[str]:
    states = final_states(events, cfg.deadline)
    return [
        RULE,
        f"SUMMARY  as of {_as_of(events)}",
        RULE,
        "",
        *_answer(states),
        *_status_table(states),
        *_deadline_section(events, states, cfg),
        *_open_section(states),
        *_lag_section(recs, cfg),
        *_notices_section(events),
    ]


# ===================================================================== CSV and JSON lines
CSV_RECORD_COLUMNS = (
    "instrument",
    "volume",
    "price",
    "currency",
    "fee",
    "trader",
    "broker",
    "created_at",
    "inserted_at",
)
CSV_COLUMNS = (
    "side",
    "record_id",
    "status",
    "severity",
    "link",
    "counterparty_ids",
    "resolved_by",
    *CSV_RECORD_COLUMNS,
    "deadline",
    "first_status_at",
    "sla_met",
    "last_event_at",
    "previous_exceptions",
    "late_minutes",
    "what",
    "why",
    "next",
)


def final_status_rows(events: list[Event], deadline: timedelta) -> list[dict]:
    states = final_states(events, deadline)
    rows = []
    for (side, rid), st in sorted(states.items(), key=lambda kv: (kv[1]["record"]["inserted_at"], kv[0])):
        rows.append(
            {
                "side": side,
                "record_id": rid,
                "status": st["status"],
                "severity": st["severity"],
                "link": st["link"],
                "counterparty_ids": " ".join(map(str, st["partners"] or [])),
                "resolved_by": st["resolved_by"],
                **{k: st["record"][k] for k in CSV_RECORD_COLUMNS},
                "deadline": fmt_ts(st["deadline"]),
                "first_status_at": st["first_status_at"],
                "sla_met": st["sla_met"],
                "last_event_at": st["last_event_at"],
                "previous_exceptions": " ".join(dict.fromkeys(st["exceptions"])) if st["status"] == PROCESSED else "",
                "late_minutes": st["late_minutes"],
                "what": st["what"],
                "why": st["why"],
                "next": st["next"],
            }
        )
    for e in events:
        if e.kind == "QUARANTINE" and e.record_id is None:
            raw = e.details.get("raw", {})
            rows.append(
                {
                    "side": e.side,
                    "record_id": e.details.get("record_id_text", ""),
                    "status": "QUARANTINED",
                    "severity": e.severity,
                    "what": e.what,
                    "why": e.why,
                    "next": e.next,
                    **{k: raw.get(k, "") for k in CSV_RECORD_COLUMNS},
                }
            )
    return rows


def write_csv(events: list[Event], path: Path, deadline: timedelta) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)  # csv writes None as an empty field
        writer.writeheader()
        writer.writerows(final_status_rows(events, deadline))


def write_jsonl(events: list[Event], path: Path) -> None:
    with Path(path).open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e.to_dict(), default=str, ensure_ascii=False) + "\n")


# ===================================================================== HTML dashboard
def _lag_chart(rows: list[dict], deadline_min: int) -> dict:
    """Positions, as percentages of the plot width, of each broker's median and slowest confirmation."""
    top = max(max((r["max"] for r in rows), default=0), deadline_min) * 1.08
    pct = lambda v: f"{v / top * 100:.1f}"
    return {
        "bars": [{**r, "bar": pct(r["median"]), "ring": pct(r["max"])} for r in rows],
        "ticks": [(pct(t), t) for t in range(0, int(top) + 1, 30 if top <= 240 else 60)],
        "deadline": pct(deadline_min),
        "deadline_min": deadline_min,
    }


def _diagram(match: re.Match) -> str:
    """Mermaid source -> its pre-rendered drawing, so the page needs no library and makes no outside requests.
    A diagram edited but not yet rendered shows its source; a test catches that, the reports still get written."""
    key = hashlib.sha256(html.unescape(match[1]).encode()).hexdigest()[:12]
    drawing = DIAGRAMS / f"{key}.svg"
    if not drawing.exists():
        return match[0]
    return f'<div class="diagram scroll" tabindex="0">{drawing.read_text(encoding="utf-8")}</div>'


def _document(name: str) -> tuple[str, str]:
    """A Markdown file as HTML and its table of contents, so the page and the document can never disagree."""
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"], extension_configs={"toc": {"toc_depth": "2-2"}})
    page = md.convert((ROOT / name).read_text(encoding="utf-8"))
    page = page.replace('href="DESIGN.md"', 'href="#docs"')  # the file is not next to the page; its panel is
    page = re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>', _diagram, page, flags=re.S)
    page = page.replace("<table>", '<div class="scroll" tabindex="0"><table>').replace("</table>", "</table></div>")
    return page.replace("<pre>", '<pre tabindex="0">'), md.toc  # wide content scrolls, by keyboard too


def _scale(value: float, steps: list) -> tuple[str, int]:
    """The (verdict, depth) of the first step the value reaches. Steps run from the highest limit down."""
    return next((verdict, depth) for limit, verdict, depth in steps if value >= limit)


def _tiles(states: dict, cases: list[dict], severities: Counter, rejected: int, deadline_min: int) -> list[dict]:
    """Key figures. Each keeps its own hue and symbol; depth 1 (close to the page colour) to 5 says how
    serious it is. The limits are a starting point for Operations to review."""
    records = len(states)
    trades = status_totals(states)[INTERNAL]
    confirmed, total = trades[PROCESSED], sum(trades.values())
    met = sum(st["sla_met"] for st in states.values())
    late = sum(
        st["status"] == PROCESSED and bool(st["exceptions"]) and st["resolved_by"] == "engine" for st in states.values()
    )
    rate = confirmed / total if total else 0
    high = severities["HIGH"]
    open_state = (
        _scale(high, [(5, "Act", 5), (2, "Act", 4), (1, "Act", 3)])
        if high
        else ("Watch", 3 if severities["MEDIUM"] else 2)
        if cases
        else ("Good", 1)
    )
    breakdown = " · ".join(f"{severities[s]} {s}" for s in ("HIGH", "MEDIUM", "LOW") if severities[s]) or "none"
    return [
        dict(zip(("label", "value", "sub", "state", "hue", "icon"), tile)) | dict(zip(("verdict", "depth"), tile[3]))
        for tile in (
            (
                "Match rate",
                f"{rate:.0%}" if total else "n/a",
                f"{confirmed} of {total} trades fully confirmed",
                _scale(
                    rate, [(0.95, "Good", 1), (0.90, "Good", 2), (0.80, "Watch", 3), (0.60, "Act", 4), (0, "Act", 5)]
                ),
                212,
                "target",
            ),
            ("Open exceptions", len(cases), breakdown, open_state, 350, "flag"),
            (
                "Status deadline met",
                f"{met} / {records}",
                f"every record had a status within {deadline_min} min",
                ("Good", 1) if met == records else ("Act", 5),
                165,
                "clock",
            ),
            (
                "Resolved late",
                late,
                "flagged first, matched after the deadline",
                _scale(late, [(6, "Watch", 3), (1, "Watch", 2), (0, "Good", 1)]),
                42,
                "hourglass",
            ),
            (
                "Rejected rows",
                rejected,
                "quarantined for the data owner",
                _scale(rejected, [(5, "Act", 5), (1, "Act", 4), (0, "Good", 1)]),
                268,
                "ban",
            ),
        )
    ]


def _bullets(text: str) -> list[str]:
    """One sentence or clause per bullet. Splits only after punctuation followed by a space, so 12.5 stays whole."""
    return re.split(r"(?<=[.!?;])\s+", text.strip())


def build_dashboard(events: list[Event], cfg: Config) -> str:
    states = final_states(events, cfg.deadline)
    deadline_min = cfg.rules.status_deadline_minutes
    cases = open_cases(states)
    severities = Counter(st["severity"] for st in cases)
    open_by_broker = Counter(
        st["record"]["broker"]
        for st in states.values()
        if st["side"] == CONFIRMATION and st["status"] in EXCEPTION_STATUSES
    )
    broker_lags, _ = lags(records_seen(events), CONFIRMATION, "broker", cfg)
    lag_rows = sorted(
        (
            {
                "broker": b,
                "median": statistics.median(v),
                "max": max(v),
                "n": len(v),
                "over": sum(x > deadline_min for x in v),
                "open": open_by_broker[b],
            }
            for b, v in broker_lags.items()
        ),
        key=lambda r: -r["median"],
    )
    matches = sorted(
        (st for st in states.values() if st["side"] == INTERNAL and st["status"] == PROCESSED),
        key=lambda st: st["last_event_at"],
    )
    tiles = _tiles(states, cases, severities, sum(e.kind == "QUARANTINE" for e in events), deadline_min)
    report = base64.b64encode(build_text_report(events, cfg).encode()).decode()
    return TEMPLATES.get_template("dashboard.html.j2").render(
        as_of=_as_of(events),
        rule_version=cfg.rule_version,
        deadline_label=cfg.deadline_label,
        tiles=tiles,
        totals=status_totals(states),
        statuses=STATUS_ORDER,
        internal=INTERNAL,
        confirmation=CONFIRMATION,
        cases=cases,
        case_label=_case_label,
        matches=matches,
        match_label=lambda st: pair_label(st["side"], st["record_id"], st["partners"]),
        bullets=_bullets,
        chart=_lag_chart(lag_rows, deadline_min),
        panels=[("docs", "How it works", *_document("DESIGN.md")), ("howto", "How to run", *_document("README.md"))],
        report_href=f"data:text/plain;charset=utf-8;base64,{report}",
        notices=_notices(events),
        notice_label=_notice_label,
    )


# ===================================================================== all outputs
def _input_counts(events: list[Event]) -> dict:
    counts = Counter(side for side, _ in {(e.side, e.record_id) for e in events if e.record_id is not None})
    counts["bad"] = sum(e.kind == "QUARANTINE" for e in events)  # a conflicting resend is rejected too
    return counts


def write_reports(events: list[Event], cfg: Config, out_dir: Path, jsonl: bool = False) -> list[Path]:
    """Rebuild every report. In file mode status_events.jsonl already is the store, so jsonl is off."""
    out_dir.mkdir(parents=True, exist_ok=True)
    text, table, page = out_dir / "recon_output.txt", out_dir / "final_status.csv", out_dir / "dashboard.html"
    text.write_text(build_text_report(events, cfg), encoding="utf-8")
    write_csv(events, table, cfg.deadline)
    page.write_text(build_dashboard(events, cfg), encoding="utf-8")
    paths = [text, table, page]
    if jsonl:
        paths.append(out_dir / "status_events.jsonl")
        write_jsonl(events, paths[-1])
    return paths
