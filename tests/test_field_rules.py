"""How one field is compared, and how a difference is explained to Operations."""

from difflib import SequenceMatcher

from conftest import confirmation, trade

from recon import diagnostics as dx
from recon.config import build_config
from recon.matcher import aggregates_to, mismatches
from recon.models import HIGH, LOW, MEDIUM

CFG = build_config()


def pair(i=None, c=None):
    return trade(1, "10:00", **(i or {})), confirmation(2, "10:05", **(c or {}))


def test_mismatches_include_trade_date():
    i, c = pair(c={"created": "2026-03-03 10:00:00", "fee": "6"})
    assert mismatches(i, c, CFG) == ["fee", "trade_date"]


def test_tolerance_turns_a_difference_into_a_match():
    i, c = pair(c={"fee": "5.01"})
    assert mismatches(i, c, CFG) == ["fee"]
    assert mismatches(i, c, build_config({"rules": {"tolerances": {"fee": 0.01}}})) == []


def test_names_compare_case_insensitively():
    i, c = pair(c={"trader": "ELSA SNOW"})
    assert mismatches(i, c, CFG) == []


def test_price_decimal_shift_and_transposition():
    d = dx.diagnose_field("price", *pair(i={"price": "101.50"}, c={"price": "10.15"}), CFG)
    assert d["severity"] == HIGH and "decimal shift" in d["why"]
    d = dx.diagnose_field("price", *pair(i={"price": "12.45"}, c={"price": "12.54"}), CFG)
    assert "transposed digits" in d["why"]


def test_volume_typo_and_multiplier():
    assert "typing error" in dx.diagnose_field("volume", *pair(i={"volume": "100"}, c={"volume": "101"}), CFG)["why"]
    assert "lots and units" in dx.diagnose_field("volume", *pair(i={"volume": "1"}, c={"volume": "100"}), CFG)["why"]


def test_fee_difference_explanations():
    assert "left out" in dx.diagnose_field("fee", *pair(c={"fee": "0"}), CFG)["why"]
    assert "rounding" in dx.diagnose_field("fee", *pair(c={"fee": "5.01"}), CFG)["why"]
    d = dx.diagnose_field("fee", *pair(c={"fee": "7.5"}), CFG)
    assert d["severity"] == MEDIUM and "commission rate" in d["why"]


def test_trader_spelling_error_versus_different_person():
    assert (
        dx.diagnose_field("trader", *pair(i={"trader": "Donald Duck"}, c={"trader": "Donlad Duck"}), CFG)["severity"]
        == LOW
    )
    assert (
        dx.diagnose_field("trader", *pair(i={"trader": "Donald Duck"}, c={"trader": "Peter Pan"}), CFG)["severity"]
        == MEDIUM
    )


def test_instrument_alias_versus_different_product():
    assert (
        dx.diagnose_field("instrument", *pair(i={"instrument": "NVIDIA"}, c={"instrument": "NVDA"}), CFG)["severity"]
        == LOW
    )
    assert (
        dx.diagnose_field("instrument", *pair(i={"instrument": "TESLA"}, c={"instrument": "APPLE"}), CFG)["severity"]
        == HIGH
    )


def test_trade_date_typo_is_explained():
    i = trade(1, "2026-03-06 11:02:00", created="2026-03-06 11:00:00")
    c = confirmation(2, "2026-03-06 11:50:00", created="2026-03-07 11:03:00")
    d = dx.diagnose_field("trade_date", i, c, CFG)
    assert "typo" in d["why"] and "2026-03-06" in d["next"]


def test_aggregation_uses_volume_weighted_price():
    single = confirmation(9, "10:00", volume="100", price="100.4", fee="10")
    parts = [trade(1, "10:00", volume="60", price="100", fee="6"), trade(2, "10:00", volume="40", price="101", fee="4")]
    assert aggregates_to(single, parts, CFG)
    assert not aggregates_to(confirmation(9, "10:00", volume="100", price="100.5", fee="10"), parts, CFG)


def test_messages_follow_the_configured_deadline():
    assert build_config().deadline_label == "2h"
    cfg = build_config({"rules": {"status_deadline_minutes": 90}})
    assert "90 min after" in dx.unconfirmed_message(trade(1, "10:00"), cfg)["what"]
    assert "90 min after" in dx.unbooked_message(confirmation(2, "10:00"), cfg)["what"]


def test_tie_headline_reads_correctly_for_one_candidate_and_for_several():
    i, c = pair(c={"fee": "9"})
    assert dx.ambiguous_message(c, [(i, ["fee"])], CFG)["headline"] == (
        "1 possible counterpart, another record also fits, none picked"
    )
    assert dx.ambiguous_message(c, [(i, ["fee"]), (i, ["fee"])], CFG)["headline"] == (
        "2 possible counterparts, none picked"
    )


def test_limits_are_inclusive():
    similarity = SequenceMatcher(None, "donald duck", "donlad duck").ratio()
    cfg = build_config({"rules": {"trader_typo_similarity": similarity}})
    i, c = pair(i={"trader": "Donald Duck"}, c={"trader": "Donlad Duck"})
    assert dx.diagnose_field("trader", i, c, cfg)["severity"] == LOW
    five_minutes_ahead = confirmation(2, "10:00", created="2026-03-02 10:05:00")
    assert "typo" not in dx.diagnose_field("trade_date", trade(1, "10:00"), five_minutes_ahead, CFG)["why"]


def test_a_match_one_minute_after_the_deadline_says_so():
    i, c = pair()
    note = dx.match_message(i, c, CFG, resolved="UNCONFIRMED", late_minutes=1)["note"]
    assert "1 min after the 2h deadline" in note


def test_a_trader_name_typo_is_fixed_internally_not_with_the_broker():
    i, c = pair(i={"trader": "Donald Duck"}, c={"trader": "Donlad Duck"})
    next_step = dx.diagnose_field("trader", i, c, CFG)["next"]
    assert "internally" in next_step and c.broker not in next_step


def test_a_currency_difference_is_checked_with_both_people_and_guesses_no_side():
    i, c = pair(c={"currency": "EUR"})
    d = dx.diagnose_field("currency", i, c, CFG)
    assert d["severity"] == HIGH and i.trader in d["next"] and c.broker in d["next"]
    assert "looks wrong" not in d["why"]
