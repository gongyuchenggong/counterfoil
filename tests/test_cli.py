"""The command line, end to end, the way people use it."""

import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, simulate, trade

from recon.cli import Printer, main
from recon.models import INTERNAL


def test_python_dash_m_recon_starts():
    done = subprocess.run([sys.executable, "-m", "recon", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0 and "reconciliation engine" in done.stdout


def test_run_writes_every_report_and_resumes_after_a_crash(tmp_path, capsys):
    out = ["--out", str(tmp_path)]
    assert main(["run", "--fresh", *out]) == 0
    assert "PROCESSED trade 9779, matched with confirmation 85290" in capsys.readouterr().out
    for name in ("recon_output.txt", "final_status.csv", "status_events.jsonl", "dashboard.html", "alerts.txt"):
        assert (tmp_path / name).exists(), name
    clean = (tmp_path / "recon_output.txt").read_text()

    assert main(["run", "--fresh", "--quiet", "--crash-after", "40", *out]) == 3
    assert main(["run", "--quiet", *out]) == 0
    assert (tmp_path / "recon_output.txt").read_text() == clean


def test_accept_force_match_and_note_commands(tmp_path, capsys):
    out = ["--out", str(tmp_path)]
    main(["run", "--fresh", "--quiet", *out])
    # No counterpart: this used to crash the console output after saving the decision.
    accept = ["accept", "--side", "confirmation", "--id", "69795"]
    assert main([*accept, "--reason", "cancelled", "--actor", "yg", *out]) == 0
    assert "accepted manually by yg" in (tmp_path / "recon_output.txt").read_text()
    assert main([*accept, "--reason", "again", *out]) == 2
    assert "already PROCESSED" in capsys.readouterr().err
    assert main(["force-match", "--trade", "2596", "--confirmation", "72141", "--reason", "phone", *out]) == 0
    assert main(["note", "--side", "internal", "--id", "6054", "--text", "promised by 15:00", *out]) == 0
    assert main(["report", *out]) == 0


def test_user_errors_give_a_message_not_a_traceback(tmp_path, capsys):
    out = ["--out", str(tmp_path)]
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("rules:\n  tolerances:\n    fees: 0.5\n")
    assert main(["run", "--config", str(bad_config), *out]) == 2
    bad_feed = tmp_path / "bad.csv"
    bad_feed.write_text("trade_id,inserted_at\n1,2026-03-02 10:00:00\n")
    assert main(["run", "--internal", str(bad_feed), *out]) == 2
    assert main(["accept", "--side", "internal", "--id", "1", "--reason", "x", *out]) == 2
    assert capsys.readouterr().err.count("error:") == 3
    with pytest.raises(SystemExit, match="--store postgres"):
        main(["run", "--source", "postgres", *out])


def test_postgres_commands_including_a_decision_during_live_mode(pg_dsn, fresh_store, tmp_path, capsys):
    args = ["--store", "postgres", "--dsn", pg_dsn, "--out", str(tmp_path)]

    assert main(["load-db", *args]) == 0
    assert "Loaded 50 new internal rows and 48 new confirmation rows" in capsys.readouterr().out
    assert main(["load-db", *args]) == 0  # a second load inserts nothing
    assert "Loaded 0 new internal rows and 0 new confirmation rows" in capsys.readouterr().out

    live = ["live", *args, "--interval", "0", "--cycles", "1", "--report-every", "0", "--quiet"]
    assert main([*live, "--fresh"]) == 0
    assert main(["accept", "--side", "confirmation", "--id", "69795", "--reason", "x", "--wait", "0", *args]) == 0
    assert main(live) == 0
    assert fresh_store.conn.execute("select status from ops_requests").fetchall() == [("applied",)]
    assert main(["report", *args]) == 0


def test_console_survives_accepting_a_record_without_a_counterpart(capsys):
    eng, _ = simulate([trade(1, "10:00")])
    events = eng.apply_manual(
        {"action": "accept", "side": INTERNAL, "record_id": 1, "actor": "yg", "reason": "cancelled"}
    )
    Printer(False)(events)
    assert "Accepted by yg" in capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        ["run", "--internal", "does-not-exist.csv"],
        ["run", "--config", "does-not-exist.yaml"],
    ],
)
def test_missing_files_give_a_message_not_a_traceback(command, tmp_path, capsys):
    assert main([*command, "--out", str(tmp_path)]) == 2
    assert "does-not-exist" in capsys.readouterr().err


def test_broken_config_gives_a_message_not_a_traceback(tmp_path, capsys):
    broken = tmp_path / "broken.yaml"
    broken.write_text("rules: [\n")
    assert main(["run", "--config", str(broken), "--out", str(tmp_path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_postgres_without_its_driver_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)  # import psycopg now raises ImportError
    with pytest.raises(SystemExit, match="uv sync --extra postgres"):
        main(["run", "--store", "postgres", "--dsn", "host=x", "--out", str(tmp_path)])


def test_unreachable_database_gives_a_message_not_a_traceback(tmp_path):
    pytest.importorskip("psycopg")
    with pytest.raises(SystemExit, match="cannot reach PostgreSQL"):
        main(["run", "--store", "postgres", "--dsn", "host=/nonexistent", "--out", str(tmp_path)])


def test_readme_operations_commands_work_in_order_and_can_be_undone(tmp_path):
    out = ["--out", str(tmp_path)]
    assert main(["run", "--fresh", "--quiet", *out]) == 0
    clean = (tmp_path / "recon_output.txt").read_text()
    assert main(["accept", "--side", "internal", "--id", "2028", "--reason", "typo", "--actor", "yg", *out]) == 0
    assert (
        main(["force-match", "--trade", "2596", "--confirmation", "72141", "--reason", "fee", "--actor", "yg", *out])
        == 0
    )
    assert main(["note", "--side", "internal", "--id", "6054", "--text", "soon", "--actor", "yg", *out]) == 0
    assert main(["run", "--fresh", "--quiet", *out]) == 0
    assert (tmp_path / "recon_output.txt").read_text() != clean  # decisions survive --fresh
    shutil.rmtree(tmp_path / "state")
    assert main(["run", "--quiet", *out]) == 0
    assert (tmp_path / "recon_output.txt").read_text() == clean  # and removing the state undoes them
