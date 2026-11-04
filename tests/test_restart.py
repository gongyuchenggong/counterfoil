"""Power goes out, everything restarts: no panic, no double counting."""

import pytest

from recon.config import build_config
from recon.models import CONFIRMATION, INTERNAL
from recon.runner import RunStateMismatch, SimulatedCrash, apply_action, run
from recon.store import FileStore, PostgresStore


def _events(folder):
    return [e.to_dict() for e in FileStore(folder).load_events()]


def _clean(cfg, items, tmp_path):
    """The events of one uninterrupted run: what every recovery must end up with."""
    run(cfg, items, FileStore(tmp_path / "clean"))
    return _events(tmp_path / "clean")


@pytest.mark.parametrize("crash_at", [1, 20, 57, 98])
def test_file_store_resume_after_crash(cfg, items, tmp_path, crash_at):
    clean = _clean(cfg, items, tmp_path)
    store = FileStore(tmp_path / "crashy")
    with pytest.raises(SimulatedCrash):
        run(cfg, items, store, crash_after=crash_at)
    run(cfg, items, FileStore(tmp_path / "crashy"))  # a new process picks up
    assert _events(tmp_path / "crashy") == clean


def test_running_twice_adds_nothing(cfg, items, tmp_path):
    run(cfg, items, FileStore(tmp_path))
    before = _events(tmp_path)
    run(cfg, items, FileStore(tmp_path))
    assert _events(tmp_path) == before


def test_crash_between_events_and_checkpoint(cfg, items, tmp_path):
    """Events of step k are written, then the process dies before the checkpoint moves."""

    class DiesBeforeCheckpoint(FileStore):
        def commit_step(self, events, dirty, checkpoint):
            if checkpoint["steps"] == 30:
                ids = self._known_ids()
                self._append_jsonl(self.events_path, [e.to_dict() for e in events if e.event_id not in ids])
                raise SimulatedCrash("power cut")
            super().commit_step(events, dirty, checkpoint)

    clean = _clean(cfg, items, tmp_path)
    with pytest.raises(SimulatedCrash):
        run(cfg, items, DiesBeforeCheckpoint(tmp_path / "torn"))
    run(cfg, items, FileStore(tmp_path / "torn"))
    assert _events(tmp_path / "torn") == clean


def test_torn_last_line_is_repaired(cfg, items, tmp_path):
    clean = _clean(cfg, items, tmp_path)
    store = FileStore(tmp_path / "half")
    with pytest.raises(SimulatedCrash):
        run(cfg, items, store, crash_after=50)
    with store.events_path.open("a") as fh:
        fh.write('{"event_id": "I-123-1", "seq": 9')  # half a line, as after a power cut
    run(cfg, items, FileStore(tmp_path / "half"))
    assert _events(tmp_path / "half") == clean


def test_events_without_a_checkpoint_are_rebuilt_under_the_current_rules(cfg, items, tmp_path):
    """A fresh clone has the committed event log but not output/state. A config edit must not mix in old results."""
    run(cfg, items, FileStore(tmp_path))
    (tmp_path / "state" / "checkpoint.json").unlink()
    changed = build_config({"rules": {"tolerances": {"fee": 2.5}}})
    run(changed, items, FileStore(tmp_path))
    assert {e.rule_version for e in FileStore(tmp_path).load_events()} == {changed.rule_version}


def test_changed_rules_or_inputs_are_refused(cfg, items, tmp_path):
    store = FileStore(tmp_path)
    run(cfg, items, store)
    with pytest.raises(RunStateMismatch, match="rules"):
        run(build_config({"rules": {"tolerances": {"fee": 0.5}}}), items, store)
    with pytest.raises(RunStateMismatch, match="input"):
        run(cfg, items[:-1] + items[-1:][::-1] + items[:1], store)


def test_a_decision_on_changed_inputs_is_refused(cfg, items, tmp_path):
    store = FileStore(tmp_path)
    run(cfg, items, store)
    with pytest.raises(RunStateMismatch, match="input"):
        apply_action(cfg, items[:-1], store, {"action": "note", "side": INTERNAL, "record_id": 2028, "text": "x"})


def test_manual_actions_survive_a_fresh_rebuild(cfg, items, tmp_path):
    store = FileStore(tmp_path)
    run(cfg, items, store)
    apply_action(
        cfg,
        items,
        store,
        {"action": "accept", "side": INTERNAL, "record_id": 2028, "actor": "yg", "reason": "broker typo"},
    )
    engine = run(cfg, items, store, fresh=True)  # results rebuilt, decision replayed
    assert engine.states[(INTERNAL, 2028)].resolved_by == "manual"
    assert engine.states[(CONFIRMATION, 18519)].resolved_by == "manual"


# ------------------------------------------------------------------ PostgreSQL
def test_postgres_resumes_after_a_crash(cfg, items, pg_dsn, fresh_store, tmp_path):
    clean = _clean(cfg, items, tmp_path)
    store = fresh_store
    with pytest.raises(SimulatedCrash):
        run(cfg, items, store, crash_after=45)
    store.close()
    store = PostgresStore(pg_dsn)
    run(cfg, items, store)
    run(cfg, items, store)  # third run: nothing new
    assert [e.to_dict() for e in store.load_events()] == clean
    store.close()


def test_database_itself_refuses_a_second_match(cfg, items, fresh_store):
    psycopg = pytest.importorskip("psycopg")
    run(cfg, items, fresh_store)
    links = fresh_store.conn.execute("select count(*) from recon_links where link_type = 'hard'").fetchone()[0]
    assert links == 40
    with pytest.raises(psycopg.errors.UniqueViolation), fresh_store.conn.transaction():
        fresh_store.conn.execute("insert into recon_links values (9779, 19012, 'hard', null)")
