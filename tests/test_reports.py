"""What Operations reads: the saved reports must not change by accident, and the dashboard must be safe to publish.

After a deliberate change to a report, regenerate the saved files and review the diff:
    UPDATE_EXPECTED=1 python -m pytest tests/test_reports.py
"""

import base64
import hashlib
import os
import re

from conftest import TEST_DATA, simulate, trade

from recon.cli import main
from recon.config import build_config
from recon.loader import parse_row
from recon.models import INTERNAL
from recon.reports import build_dashboard, build_text_report, final_status_rows


def _comparable(text: str) -> str:
    return re.sub(r"rules-\S+", "RULES", text)  # a config change alone must not fail this test


def test_reports_stay_exactly_as_saved(tmp_path):
    """Edge-case feeds reach what the sample data never does: aggregation, ambiguity, duplicates, manual decisions."""
    args = [
        "--out",
        str(tmp_path),
        "--internal",
        str(TEST_DATA / "edge_internal.csv"),
        "--confirmations",
        str(TEST_DATA / "edge_confirmations.csv"),
    ]
    assert main(["run", "--fresh", "--quiet", *args]) == 0
    assert main(["accept", "--side", "internal", "--id", "9001", "--reason", "group", "--actor", "yg", *args]) == 0
    assert (
        main(["force-match", "--trade", "9004", "--confirmation", "9104", "--reason", "chat", "--actor", "yg", *args])
        == 0
    )
    for name in ("recon_output.txt", "final_status.csv", "alerts.txt"):
        produced, saved = _comparable((tmp_path / name).read_text()), TEST_DATA / f"edge_{name}"
        if os.environ.get("UPDATE_EXPECTED"):
            saved.write_text(produced)
        assert produced == saved.read_text(), name


def test_dashboard_is_self_contained_and_carries_the_docs(cfg, items):
    _, events = simulate(items, cfg)
    page = build_dashboard(events, cfg)
    assert not re.search(r"""<script[^>]+src=|<link[^>]+href=["']http|<img|src=["']http""", page)
    assert page.count('<tr class="row" data-view="open"') == 11
    assert page.count('<tr class="row" data-view="match"') == 40
    assert "Records to check (2)" in page
    assert (
        len(set(re.findall(r'class="tile-label"><svg[^>]*data-icon="(\w+)"', page))) == 5
    )  # no two key figures share a symbol
    assert "Median lag (min)" not in page  # the chart and its tooltip carry those figures
    assert "2 open exceptions" in page
    assert 'id="docs"' in page
    assert page.count('class="diagram scroll"') == 6 and "language-mermaid" not in page
    assert 'data-icon="sun"' in page and 'data-icon="moon"' in page
    for figure in ("80%", "40 of 50 trades fully confirmed", "7 HIGH · 2 MEDIUM · 2 LOW", "98 / 98"):
        assert figure in page, figure
    assert re.search(r'Rejected rows<span class="verdict">Good</span></div>\s*<div class="tile-value">0<', page)
    tiles = re.findall(r'class="tile depth-(\d)" style="--hue:(\d+)"', page)
    assert [depth for depth, _ in tiles] == ["3", "5", "1", "2", "1"]  # the deeper, the more serious
    assert len({hue for _, hue in tiles}) == 5  # no two tiles can ever share a colour
    assert "<li>All other terms agree.</li>" in page  # long messages render as bullets


def test_every_diagram_has_its_drawing_and_no_drawing_is_left_over():
    root = TEST_DATA.parent.parent
    sources = re.findall(r"```mermaid\n(.*?)```", (root / "DESIGN.md").read_text(), flags=re.S)
    keys = {hashlib.sha256(source.encode()).hexdigest()[:12] for source in sources}
    assert {p.stem for p in (root / "recon" / "diagrams").glob("*.svg")} == keys  # else: tools/render_diagrams.py


def test_the_page_carries_the_run_instructions_and_the_text_report(cfg, items):
    _, events = simulate(items, cfg)
    page = build_dashboard(events, cfg)
    assert 'id="howto"' in page and "uv sync --locked --all-extras" in page and 'href="DESIGN.md"' not in page
    report = re.search(r'href="data:text/plain;charset=utf-8;base64,([^"]+)" download="recon_output.txt"', page)[1]
    assert base64.b64decode(report).decode() == build_text_report(events, cfg)
    ids = re.findall(r'\sid="([^"]+)"', re.sub(r"<svg.*?</svg>", "", page, flags=re.S))
    assert len(ids) == len(set(ids))  # the two documents share one page, so a link must have one target


def test_report_answers_the_assignment_first(cfg, items):
    _, events = simulate(items, cfg)
    report = build_text_report(events, cfg)
    assert "Fully confirmed : 40 of 50 trades" in report and "Not confirmed   : 10 trades" in report
    assert "Needs attention : 11 open cases (7 HIGH, 2 MEDIUM, 2 LOW)" in report
    assert report.index("Needs attention") < report.index("PROCESSED trade")


def test_rejected_rows_say_why_everywhere(tmp_path):
    cfg = build_config()
    bad = parse_row(
        INTERNAL,
        {
            "trade_id": "7",
            "inserted_at": "2026-03-02 10:00:00",
            "created_at": "2026-03-02 10:00:00",
            "instrument": "APPLE",
            "price": "-5",
            "volume": "1",
            "fee": "1",
            "currency": "USD",
            "trader": "A",
            "broker": "B",
        },
        3,
        cfg,
    )
    _, events = simulate([bad, trade(1, "10:00", cfg=cfg)], cfg)
    why = "Cannot be reconciled until fixed."
    assert why in " ".join(build_text_report(events, cfg).split())
    assert [r["why"] for r in final_status_rows(events, cfg.deadline) if r["status"] == "QUARANTINED"] == [why]
    assert why in build_dashboard(events, cfg)


def test_dashboard_escapes_text_from_the_feeds():
    cfg = build_config()
    evil = "<script>window.pwned=1</script>"
    _, events = simulate([trade(1, "10:00", trader=evil, cfg=cfg)], cfg)
    page = build_dashboard(events, cfg)
    assert evil not in page and "&lt;script&gt;" in page


def test_accepted_trade_without_a_counterpart_is_labelled_cleanly(tmp_path):
    cfg = build_config()
    eng, events = simulate([trade(1, "10:00", cfg=cfg)], cfg)
    events += eng.apply_manual({"action": "accept", "side": INTERNAL, "record_id": 1, "actor": "yg", "reason": "x"})
    page = build_dashboard(events, cfg)
    assert 'aria-expanded="false">trade 1</button>' in page
