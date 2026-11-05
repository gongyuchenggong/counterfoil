"""The engine, one scenario at a time, including cases the sample data never reaches."""

import ast
import random
from datetime import datetime, timedelta

import pytest
from conftest import (
    ROOT,
    at,
    confirmation,
    confirmation_events,
    confirmation_state,
    simulate,
    status_events,
    trade,
    trade_events,
    trade_state,
)

from recon.config import build_config
from recon.engine import FINALIZE, Engine, ManualActionError
from recon.loader import parse_row
from recon.models import (
    BREAK,
    CONFIRMATION,
    EXCEPTION_STATUSES,
    HIGH,
    INFO,
    INTERNAL,
    LOW,
    MEDIUM,
    PENDING,
    POSSIBLE_DUPLICATE,
    PROCESSED,
    UNBOOKED,
    UNCONFIRMED,
    fmt_ts,
)


# ------------------------------------------------------------------ timing
def test_late_confirmation_is_flagged_then_resolved():
    _, ev = simulate([trade(1, "10:00"), confirmation(9, "12:30")])
    seq = [(e.sim_time[11:16], e.status) for e in trade_events(ev, 1)]
    assert seq == [("12:00", UNCONFIRMED), ("12:30", PROCESSED)]
    assert trade_events(ev, 1)[-1].late_minutes == 30


def test_late_booking_is_flagged_then_resolved():
    _, ev = simulate([confirmation(9, "10:00"), trade(1, "12:20")])
    assert [e.status for e in confirmation_events(ev, 9)] == [UNBOOKED, PROCESSED]


def test_record_arriving_exactly_at_the_deadline_is_processed_first():
    _, ev = simulate([trade(1, "10:00"), confirmation(9, "12:00")])
    assert [e.status for e in trade_events(ev, 1)] == [PROCESSED]


def test_confirmation_first_within_the_window_is_normal():
    _, ev = simulate([confirmation(9, "10:00"), trade(1, "11:30")])
    assert [e.status for e in status_events(ev)] == [PROCESSED, PROCESSED]


def test_partial_matching_waits_for_the_deadline():
    _, ev = simulate([trade(1, "10:00", fee="5"), confirmation(9, "10:30", fee="6")])
    breaks = trade_events(ev, 1)
    assert breaks[0].status == BREAK and breaks[0].sim_time.endswith("12:00:00")


# ------------------------------------------------------------------ duplicates
@pytest.mark.parametrize(
    "second_trade_time, status",
    [
        ("10:00", POSSIBLE_DUPLICATE),
        ("10:05", POSSIBLE_DUPLICATE),
        ("2026-03-02 10:05:01", UNCONFIRMED),
        ("10:39", UNCONFIRMED),
    ],
)
def test_an_identical_booking_is_a_duplicate_only_when_traded_close_in_time(second_trade_time, status):
    eng, ev = simulate(
        [
            trade(1, "10:00", created="10:00"),
            confirmation(9, "10:30", created="10:00"),
            trade(2, "10:40", created=second_trade_time),
        ]
    )
    event = trade_events(ev, 2)[-1]
    assert event.status == status and event.severity == HIGH and event.details["duplicate_of"] == 1
    assert ("second real trade" in event.why) == (status == UNCONFIRMED)


def test_identical_booking_is_a_possible_duplicate():
    eng, ev = simulate(
        [
            trade(1, "10:00", created="09:59"),
            trade(2, "10:01", created="09:59"),
            confirmation(9, "10:30", created="09:59"),
        ]
    )
    assert trade_state(eng, 1).status == PROCESSED
    dup = trade_events(ev, 2)[-1]
    assert dup.status == POSSIBLE_DUPLICATE and dup.severity == HIGH
    assert dup.details["duplicate_of"] == 1 and "booked twice" in dup.why


def test_duplicate_confirmation_stays_unbooked_and_high():
    _, ev = simulate(
        [
            trade(1, "10:00", created="09:58"),
            confirmation(9, "10:10", created="09:58"),
            confirmation(10, "10:12", created="09:58"),
        ]
    )
    e = confirmation_events(ev, 10)[-1]
    assert e.status == UNBOOKED and e.severity == HIGH
    assert "identical confirmation" in e.what.lower() and "resend" in e.why
    assert "how many trades" in e.next


def test_duplicate_confirmation_with_a_different_trade_time_leans_to_a_second_trade():
    _, ev = simulate(
        [
            trade(1, "10:00", created="09:58"),
            confirmation(9, "10:10", created="09:58"),
            confirmation(10, "10:45", created="10:40"),
        ]
    )
    e = confirmation_events(ev, 10)[-1]
    assert e.status == UNBOOKED and e.severity == HIGH and "second real trade" in e.why


def test_several_exact_candidates_pick_the_closest_trade_time():
    eng, _ = simulate(
        [
            trade(1, "10:00", created="09:00"),
            trade(2, "10:01", created="09:50"),
            confirmation(9, "10:30", created="09:52"),
        ]
    )
    assert confirmation_state(eng, 9).partners == (2,)


def test_equal_trade_times_fall_back_to_first_in_first_out():
    eng, _ = simulate(
        [
            trade(5, "10:00", created="09:50"),
            trade(2, "10:01", created="09:50"),
            confirmation(9, "10:30", created="09:50"),
        ]
    )
    assert confirmation_state(eng, 9).partners == (5,)


# ------------------------------------------------------------------ breaks
def test_price_break_is_high_with_decimal_shift():
    _, ev = simulate([trade(1, "10:00", price="101.50"), confirmation(9, "10:10", price="10.15")])
    e = trade_events(ev, 1)[-1]
    assert e.status == BREAK and e.severity == HIGH and "decimal shift" in e.why
    assert e.counterparty_ids == [9] and e.link == "soft"


def test_two_differences_still_pair_and_take_the_highest_severity():
    _, ev = simulate([trade(1, "10:00", fee="6", volume="11"), confirmation(9, "10:10")])
    e = trade_events(ev, 1)[-1]
    assert e.status == BREAK and e.severity == HIGH and {m["field"] for m in e.mismatches} == {"fee", "volume"}


def test_three_differences_are_not_paired():
    eng, _ = simulate([trade(1, "10:00", fee="6", volume="11", price="99"), confirmation(9, "10:10")])
    assert trade_state(eng, 1).status == UNCONFIRMED and confirmation_state(eng, 9).status == UNBOOKED


def test_equally_good_candidates_are_not_guessed():
    eng, ev = simulate([confirmation(9, "10:00"), trade(1, "10:05", fee="6"), trade(2, "10:06", volume="11")])
    e = confirmation_events(ev, 9)[-1]
    assert e.status == BREAK and e.link == "ambiguous" and sorted(e.counterparty_ids) == [1, 2]
    assert confirmation_state(eng, 9).link is None


def test_exact_match_replaces_a_soft_link():
    eng, ev = simulate([trade(1, "10:00", fee="6"), confirmation(9, "10:10", fee="5"), trade(2, "12:30", fee="5")])
    assert confirmation_state(eng, 9).status == PROCESSED and confirmation_state(eng, 9).partners == (2,)
    assert [e.status for e in trade_events(ev, 1)] == [BREAK, UNCONFIRMED]


def test_strictly_better_partial_match_takes_over_a_soft_link():
    # M2 arrives after M1's deadline, so M1 and C9 are already soft-linked (2 differences) when
    # M2 (1 difference) gets its turn at 14:30.
    eng, ev = simulate([trade(1, "10:00", fee="6", volume="11"), confirmation(9, "10:10"), trade(2, "12:30", fee="7")])
    assert confirmation_state(eng, 9).partners == (2,)
    assert [e.status for e in trade_events(ev, 1)] == [BREAK, UNCONFIRMED]


def test_mutual_best_prefers_the_closer_record_when_both_are_present():
    eng, ev = simulate([trade(1, "10:00", fee="6", volume="11"), confirmation(9, "10:10"), trade(2, "10:30", fee="7")])
    assert confirmation_state(eng, 9).partners == (2,)
    assert [e.status for e in trade_events(ev, 1)] == [UNCONFIRMED]


def test_trades_adding_up_to_one_confirmation_are_a_medium_break():
    eng, ev = simulate(
        [
            trade(1, "10:00", volume="60", price="100", fee="6"),
            trade(2, "10:05", volume="40", price="101", fee="4"),
            confirmation(9, "10:30", volume="100", price="100.4", fee="10"),
        ]
    )
    c = confirmation_state(eng, 9)
    assert c.status == BREAK and c.link == "aggregate" and sorted(c.partners) == [1, 2]
    e = confirmation_events(ev, 9)[-1]
    assert e.severity == MEDIUM and "aggregated" in e.why


def test_confirmations_adding_up_to_one_trade_are_linked():
    eng, _ = simulate(
        [
            trade(1, "10:00", volume="100", price="100.4", fee="10"),
            confirmation(8, "10:20", volume="60", price="100", fee="6"),
            confirmation(9, "10:30", volume="40", price="101", fee="4"),
        ]
    )
    m = trade_state(eng, 1)
    assert m.link == "aggregate" and sorted(m.partners) == [8, 9]


def test_aggregation_can_be_switched_off():
    cfg = build_config({"rules": {"aggregation": {"enabled": False}}})
    eng, _ = simulate(
        [
            trade(1, "10:00", volume="60", price="100", fee="6", cfg=cfg),
            trade(2, "10:05", volume="40", price="101", fee="4", cfg=cfg),
            confirmation(9, "10:30", volume="100", price="100.4", fee="10", cfg=cfg),
        ],
        cfg=cfg,
    )
    assert confirmation_state(eng, 9).link != "aggregate"


# ------------------------------------------------------------------ configuration
def test_mapping_table_matches_and_says_so():
    cfg = build_config({"mappings": {"instrument": {"NVDA": "NVIDIA"}}})
    eng, ev = simulate(
        [trade(1, "10:00", instrument="NVIDIA", cfg=cfg), confirmation(9, "10:10", instrument="NVDA", cfg=cfg)], cfg=cfg
    )
    assert confirmation_state(eng, 9).status == PROCESSED
    assert "mapping" in confirmation_events(ev, 9)[-1].note


def test_without_mapping_the_alias_is_a_low_break():
    eng, _ = simulate([trade(1, "10:00", instrument="NVIDIA"), confirmation(9, "10:10", instrument="NVDA")])
    assert trade_state(eng, 1).status == BREAK and trade_state(eng, 1).severity == LOW


def test_tolerance_matches_and_says_so():
    cfg = build_config({"rules": {"tolerances": {"fee": 0.01}}})
    eng, ev = simulate([trade(1, "10:00", fee="5", cfg=cfg), confirmation(9, "10:10", fee="5.01", cfg=cfg)], cfg=cfg)
    assert trade_state(eng, 1).status == PROCESSED and "tolerance" in trade_events(ev, 1)[-1].note


# ------------------------------------------------------------------ data quality
def test_resend_with_identical_content_is_ignored():
    eng, ev = simulate([trade(1, "10:00"), confirmation(9, "10:10"), trade(1, "10:20", created="10:00")])
    assert trade_state(eng, 1).status == PROCESSED
    assert [e.kind for e in ev if e.record_id == 1 and e.kind != "STATUS"] == ["DATA_QUALITY"]


def test_resend_with_different_content_is_quarantined():
    eng, ev = simulate([trade(1, "10:00"), confirmation(9, "10:10"), trade(1, "10:20", created="10:00", fee="9")])
    q = [e for e in ev if e.kind == "QUARANTINE"]
    assert len(q) == 1 and q[0].severity == HIGH and "fee: 5 -> 9" in q[0].what
    assert trade_state(eng, 1).status == PROCESSED


def test_invalid_row_is_quarantined_not_dropped():
    bad = parse_row(
        INTERNAL,
        {
            "trade_id": "7",
            "inserted_at": "2026-03-02 10:00:00",
            "created_at": "2026-03-02 10:00:00",
            "instrument": "APPLE",
            "price": "100",
            "volume": "-5",
            "fee": "5",
            "currency": "USD",
            "trader": "A",
            "broker": "B",
        },
        3,
        build_config(),
    )
    eng = Engine(build_config())
    ev = eng.step(bad)
    assert ev[0].kind == "QUARANTINE" and ev[0].details["source_row"] == 3


def test_clock_skew_is_a_notice_not_a_break():
    _, ev = simulate(
        [trade(1, "10:00", created="09:59"), confirmation(9, "2026-03-02 10:10:00", created="2026-03-02 10:10:40")]
    )
    notices = [e for e in ev if e.kind == "DATA_QUALITY"]
    assert notices and notices[0].severity == INFO


# ------------------------------------------------------------------ manual decisions
def _break_then_accept():
    eng = Engine(build_config())
    for r in (trade(1, "10:00", fee="6"), confirmation(9, "10:10", fee="5")):
        eng.step(r)
    eng.advance_clock(at("12:30"))
    eng.drain()
    eng.apply_manual(
        {
            "action": "accept",
            "side": INTERNAL,
            "record_id": 1,
            "actor": "yg",
            "reason": "fee agreed with broker",
            "action_id": 1,
        }
    )
    return eng


def test_accept_resolves_both_sides():
    eng = _break_then_accept()
    for st in (trade_state(eng, 1), confirmation_state(eng, 9)):
        assert st.status == PROCESSED and st.resolved_by == "manual"


def test_manual_decision_wins_over_later_data():
    eng = _break_then_accept()
    ev = eng.step(confirmation(10, "13:00", fee="6")) + eng.step(FINALIZE)
    assert trade_state(eng, 1).partners == (9,) and trade_state(eng, 1).resolved_by == "manual"
    assert any(e.kind == "NOTE" and e.record_id == 1 for e in ev)
    late = confirmation_events(ev, 10)[-1]
    assert late.status == UNBOOKED and "resolved manually" in late.why


def test_invalid_manual_actions_are_refused():
    eng = _break_then_accept()
    with pytest.raises(ManualActionError):
        eng.apply_manual({"action": "accept", "side": INTERNAL, "record_id": 1})
    with pytest.raises(ManualActionError):
        eng.apply_manual({"action": "force-match", "trade_id": 1, "confirmation_id": 9})
    with pytest.raises(ManualActionError):
        eng.apply_manual({"action": "note", "side": INTERNAL, "record_id": 404, "text": "x"})


def test_force_match_breaks_a_soft_link_and_re_evaluates_the_partner():
    eng = Engine(build_config())
    for r in (trade(1, "10:00", fee="6"), confirmation(9, "10:10", fee="5"), confirmation(10, "10:20", volume="50")):
        eng.step(r)
    eng.advance_clock(at("13:00"))
    eng.drain()
    ev = eng.apply_manual(
        {
            "action": "force-match",
            "trade_id": 1,
            "confirmation_id": 10,
            "actor": "yg",
            "reason": "volume confirmed by phone",
            "action_id": 1,
        }
    )
    assert trade_state(eng, 1).partners == (10,) and trade_state(eng, 1).link == "manual"
    assert confirmation_state(eng, 9).status == UNBOOKED
    assert any(e.record_id == 9 and e.status == UNBOOKED for e in ev)


# ------------------------------------------------------------------ day boundaries
def test_midnight_snapshot_lists_what_is_still_open():
    _, ev = simulate([trade(1, "2026-03-02 22:30:00"), confirmation(9, "2026-03-03 09:00:00", instrument="META")])
    snaps = [e for e in ev if e.kind == "SNAPSHOT"]
    assert [s.details["day"] for s in snaps] == ["2026-03-02", "2026-03-03"]
    assert snaps[0].details["open"] == []  # still inside its window at midnight
    assert [o["record_id"] for o in snaps[1].details["open"]] == [1, 9]
    first_unconfirmed = next(e for e in ev if e.status == UNCONFIRMED)
    assert snaps[0].seq < first_unconfirmed.seq  # the deadline at 00:30 comes after the snapshot


# ------------------------------------------------------------------ at scale
def test_random_records_keep_every_promise():
    """About 2,700 random records: nothing stays pending, every deadline is met, nothing is matched twice."""
    rng = random.Random(7)
    cfg = build_config()
    start = datetime(2026, 3, 2, 8, 0)
    records = []
    for rid in range(1, 1501):
        trade_time = start + timedelta(minutes=rng.randint(0, 3 * 24 * 60))
        terms = {
            "instrument": rng.choice(["APPLE", "TESLA", "META"]),
            "price": str(rng.choice([100, 101, 250.5])),
            "volume": str(rng.choice([10, 20, 50, 100])),
            "fee": str(rng.choice([5, 7.5, 10])),
            "trader": rng.choice(["Elsa Snow", "Peter Pan"]),
            "broker": rng.choice(["Moonshot Capital", "Lazy Panda Securities"]),
            "cfg": cfg,
        }
        if rng.random() < 0.9:
            records.append(
                trade(
                    rid,
                    fmt_ts(trade_time + timedelta(minutes=rng.randint(0, 150))),
                    created=fmt_ts(trade_time),
                    **terms,
                )
            )
        if rng.random() < 0.9:
            if rng.random() < 0.1:
                terms["fee"] = "12.5"
            records.append(
                confirmation(
                    rid,
                    fmt_ts(trade_time + timedelta(minutes=rng.randint(0, 200))),
                    created=fmt_ts(trade_time + timedelta(minutes=rng.randint(0, 20))),
                    **terms,
                )
            )
    eng, events = simulate(records, cfg)
    states = eng.states.values()
    assert all(s.status != PENDING and s.first_status_at <= s.deadline for s in states)
    assert any(s.status in EXCEPTION_STATUSES for s in states) and any(s.status == PROCESSED for s in states)
    assert len({e.event_id for e in events}) == len(events)
    matched = [s for s in states if s.link == "hard"]
    assert matched
    for s in matched:
        partner = eng.states[(CONFIRMATION if s.rec.side == INTERNAL else INTERNAL, s.partners[0])]
        assert partner.partners == (s.rid,) and partner.link == "hard"


def test_a_tie_is_looked_at_again_when_a_candidate_is_matched_elsewhere():
    eng, ev = simulate(
        [
            confirmation(9, "10:00"),
            trade(1, "10:05", fee="6"),
            trade(2, "10:06", volume="11"),
            confirmation(10, "12:30", fee="6"),
        ]
    )
    assert trade_state(eng, 1).partners == (10,)  # trade 1 is gone, so trade 2 is the only answer left
    assert confirmation_state(eng, 9).partners == (2,) and trade_state(eng, 2).partners == (9,)
    assert confirmation_events(ev, 9)[-1].pair_key == "B-2-9"


def test_decisions_that_cannot_apply_are_refused():
    eng = Engine(build_config())
    eng.step(trade(1, "10:00"))
    with pytest.raises(ManualActionError, match="still inside"):
        eng.apply_manual({"action": "accept", "side": INTERNAL, "record_id": 1})
    with pytest.raises(ManualActionError, match="unknown action"):
        eng.apply_manual({"action": "delete"})


def test_the_core_touches_no_file_database_or_network():
    """What the architecture diagram promises: the core only computes, so replay and live share it."""
    outside = {"csv", "jinja2", "json", "markdown", "os", "pathlib", "psycopg", "smtplib", "socket", "urllib", "yaml"}
    for name in ("engine", "matcher", "diagnostics"):
        tree = ast.parse((ROOT / "recon" / f"{name}.py").read_text())
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and not n.level}
        assert not imported & outside, name
