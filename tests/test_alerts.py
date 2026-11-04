"""Alerts: the right people hear the right thing, once."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

from conftest import confirmation, simulate, trade

from recon import alerts as al
from recon.alerts import Alert, build_alerts, channels_from_env, dispatch
from recon.config import build_config
from recon.store import FileStore


def _kinds(alerts):
    urgent = [a for a in alerts if not a.digest and not a.id.endswith(":RESOLVED")]
    return urgent, [a for a in alerts if a.id.endswith(":RESOLVED")], [a for a in alerts if a.digest]


def test_one_alert_per_urgent_case_not_one_per_event(cfg, items):
    _, events = simulate(items, cfg)
    alerts = build_alerts(events, cfg)
    urgent, resolved, digests = _kinds(alerts)
    assert (len(urgent), len(resolved), len(digests)) == (10, 3, 6)  # out of 109 events
    assert {a.severity for a in urgent} == {"HIGH"}
    assert "11 open exceptions (7 HIGH, 2 MEDIUM, 2 LOW)" in digests[-1].title  # cases, as on the dashboard
    assert len({a.id for a in alerts}) == len(alerts)


def test_resolution_follows_the_alert_that_raised_it(cfg, items):
    _, events = simulate(items, cfg)
    alerts = build_alerts(events, cfg)
    raised = next(i for i, a in enumerate(alerts) if "trade 4078" in a.title and "UNCONFIRMED" in a.title)
    cleared = next(i for i, a in enumerate(alerts) if a.title == "RESOLVED trade 4078")
    assert raised < cleared and "arrived" in alerts[cleared].body


def test_a_low_severity_break_waits_for_the_digest():
    cfg = build_config()
    _, events = simulate([trade(1, "10:00", trader="Donald Duck"), confirmation(2, "10:05", trader="Donlad Duck")], cfg)
    alerts = build_alerts(events, cfg)
    assert [a.digest for a in alerts] == [True]
    assert "LOW BREAK" in alerts[0].body


def test_policy_is_configurable_but_does_not_change_the_rule_version():
    wider = build_config({"alerts": {"immediate": ["HIGH", "LOW"]}})
    assert wider.rule_version == build_config().rule_version  # it changes who is told, not the status
    _, events = simulate(
        [trade(1, "10:00", trader="Donald Duck", cfg=wider), confirmation(2, "10:05", trader="Donlad Duck", cfg=wider)],
        wider,
    )
    assert any(not a.digest for a in build_alerts(events, wider))


def _channel(log, name="slack", fail=lambda: False):
    def send(alert):
        if fail():
            raise OSError("down")
        log.append(alert.id)

    return name, send, lambda a: True


def test_restart_and_rebuild_never_resend(cfg, items, tmp_path):
    store = FileStore(tmp_path)
    store.commit_step(simulate(items, cfg)[1], [], {"steps": 1})
    ledger, sent = tmp_path / "state" / "alerts_sent.log", []
    dispatch(build_alerts(store.load_events(), cfg), [_channel(sent)], ledger)
    assert len(sent) == 19
    dispatch(build_alerts(store.load_events(), cfg), [_channel(sent)], ledger)  # restart
    store.reset()  # run --fresh
    store.commit_step(simulate(items, cfg)[1], [], {"steps": 1})
    dispatch(build_alerts(store.load_events(), cfg), [_channel(sent)], ledger)
    assert len(sent) == 19


def test_failed_delivery_is_retried_without_repeating_the_healthy_channel(tmp_path):
    alerts = [Alert(str(i), "t", "HIGH", "title", "body") for i in range(3)]
    ledger, good, bad = tmp_path / "state" / "sent.log", [], []
    email_is_down = [True]
    channels = [_channel(good, "slack"), _channel(bad, "email", fail=lambda: email_is_down[0])]
    dispatch(alerts, channels, ledger)  # does not raise
    assert (len(good), len(bad)) == (3, 0)
    email_is_down[0] = False
    dispatch(alerts, channels, ledger)
    assert (len(good), len(bad)) == (3, 3)


def test_routing_urgent_to_slack_digest_to_email():
    both = {"RECON_SLACK_WEBHOOK_URL": "http://x", "RECON_SMTP_HOST": "h", "RECON_ALERT_TO": "ops@x"}
    urgent, digest = Alert("1", "t", "HIGH", "t", "b"), Alert("2", "t", "INFO", "t", "b", digest=True)
    routes = {name: (wants(urgent), wants(digest)) for name, _, wants in channels_from_env(both)}
    assert routes == {"slack": (True, False), "email": (False, True)}
    alone = channels_from_env({"RECON_SMTP_HOST": "h", "RECON_ALERT_TO": "ops@x"})
    assert alone[0][2](urgent) and alone[0][2](digest)  # nothing urgent is lost without Slack
    assert channels_from_env({}) == []


def test_slack_request_follows_the_webhook_format():
    received = {}

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            received["type"] = self.headers["Content-Type"]
            received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.handle_request, daemon=True).start()
    al._send_slack(f"http://127.0.0.1:{server.server_port}/hook", Alert("1", "t", "HIGH", "A & B", "x < y"))
    server.server_close()
    assert received["type"] == "application/json"
    assert received["body"] == {"text": "*A &amp; B*\nx &lt; y"}


def test_email_carries_subject_and_recipient(monkeypatch):
    smtp = MagicMock()
    monkeypatch.setattr(al.smtplib, "SMTP", smtp)
    al._send_email("mail:25", "recon@x", "ops@x", Alert("1", "t", "INFO", "End of day", "3 open"))
    smtp.assert_called_once_with("mail:25", timeout=10)
    message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
    assert message["To"] == "ops@x" and message["Subject"] == "[recon] End of day"
