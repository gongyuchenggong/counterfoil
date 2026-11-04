"""The assignment's own data: every result, every deadline, no look-ahead."""

from datetime import datetime

import pytest
from conftest import expected_rows, simulate, status_events, trade_events

from recon.engine import Engine
from recon.reports import build_text_report, final_states


def test_matches_the_expected_results_table(cfg, items):
    eng, events = simulate(items, cfg)
    first = {}
    for e in status_events(events):
        first.setdefault((e.side, e.record_id), e.sim_time)
    expected = expected_rows()
    assert len(expected) == len(eng.states) == 98
    for row in expected:
        st = eng.states[(row["side"], int(row["record_id"]))]
        where = f"{row['side']} {row['record_id']}"
        assert st.status == row["final_status"], where
        if row["counterparty_id"]:
            assert st.partners == (int(row["counterparty_id"]),), where
        if row["duplicate_of"]:
            dup = trade_events(events, st.rid)[-1]
            assert dup.details["duplicate_of"] == int(row["duplicate_of"]), where
        assert first[(row["side"], int(row["record_id"]))] == row["first_status_at"], where


def test_every_record_gets_a_status_within_two_hours(cfg, items):
    _, events = simulate(items, cfg)
    states = final_states(events, cfg.deadline)
    assert len(states) == 98 and all(st["sla_met"] for st in states.values())


@pytest.mark.parametrize(
    "cut",
    [
        "2026-03-02 16:30:00",
        "2026-03-03 11:30:00",
        "2026-03-04 15:00:00",
        "2026-03-05 13:00:00",
        "2026-03-06 18:00:00",
        "2026-03-07 20:30:00",
    ],
)
def test_output_before_a_cutoff_ignores_later_data(cfg, items, cut):
    """Output before time T must not depend on anything that arrives after T."""
    t = datetime.strptime(cut, "%Y-%m-%d %H:%M:%S")
    _, full = simulate(items, cfg)
    eng = Engine(cfg)
    partial = []
    for item in (i for i in items if i.inserted_at < t):
        partial += eng.step(item)
    eng.advance_clock(t)
    partial += eng.drain()

    def before_cut(events):
        return [e.to_dict() for e in events if e.sim_time < cut]

    assert before_cut(full)  # the comparison is not vacuous
    assert before_cut(partial) == before_cut(full)


def test_report_mentions_the_required_processed_format(cfg, items):
    _, events = simulate(items, cfg)
    text = build_text_report(events, cfg)
    assert "PROCESSED trade 9779, matched with confirmation 85290" in text
    assert "98 of 98 records got a definitive status" in text


def test_a_small_timing_notice_states_facts_not_a_guessed_cause(cfg, items):
    _, events = simulate(items, cfg)
    notice = next(e for e in events if e.kind == "DATA_QUALITY" and e.record_id == 94820)
    assert "49s after receipt" in notice.what and "Trade 9133 was timed 35.8 min away" in notice.what
    assert notice.why == "Clock drift or a mistyped time: the data cannot tell."
