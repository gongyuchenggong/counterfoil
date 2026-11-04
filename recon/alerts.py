"""Get the Operations messages to people, without flooding them.

Urgent severities go out at once, one alert per case, with a follow-up when the case resolves, so nobody
keeps chasing a trade that has settled. Everything else waits for the end-of-day digest.

Alerts are derived from the event log, like the reports, and sent through a ledger of what already went
out (an outbox). A restart or a --fresh rebuild therefore never re-sends, and a failed delivery is retried
at the next refresh. A crash between sending and recording can repeat one alert, never lose one.
"""

import json
import os
import smtplib
import sys
import textwrap
from collections import Counter
from dataclasses import dataclass
from email.message import EmailMessage
from functools import partial
from pathlib import Path
from urllib.request import Request, urlopen

from .config import Config
from .models import EXCEPTION_STATUSES, LABEL, PROCESSED, Event, pair_label


@dataclass(frozen=True)
class Alert:
    id: str  # stable across rebuilds, so the ledger recognises it
    at: str | None
    severity: str
    title: str
    body: str
    digest: bool = False


# ===================================================================== what to say
def _label(e: Event) -> str:
    if e.record_id is None:
        return f"{e.side} feed, row {e.details.get('source_row')}"
    return pair_label(e.side, e.record_id, e.counterparty_ids if e.link in ("soft", "aggregate") else ())


def _explain(e: Event) -> str:
    return "\n".join(f"{name}: {text}" for name, text in (("What", e.what), ("Why", e.why), ("Next", e.next)) if text)


def _digest(e: Event) -> Alert:
    # One line per case: the two sides of a break are one problem, as on the dashboard.
    items = list({i.get("pair_key") or (i["side"], i["record_id"]): i for i in e.details["open"]}.values())
    counts = Counter(item["severity"] for item in items)
    summary = ", ".join(f"{counts[s]} {s}" for s in ("HIGH", "MEDIUM", "LOW") if counts[s]) or "all clear"
    body = "\n".join(
        f"{i['severity']} {i['status']} {LABEL[i['side']]} {i['record_id']}: {i.get('headline') or i['what']}"
        for i in items
    )
    noun = "exception" if len(items) == 1 else "exceptions"
    return Alert(
        f"digest:{e.details['day']}",
        e.sim_time,
        "INFO",
        f"End of day {e.details['day']}: {len(items)} open {noun} ({summary})",
        body,
        digest=True,
    )


def build_alerts(events: list[Event], cfg: Config) -> list[Alert]:
    urgent = cfg.alerts.immediate
    alerts, alerted = {}, set()
    for e in events:
        record = (e.side, e.record_id)
        case = e.pair_key or f"{e.side}-{e.record_id}"
        if e.kind == "SNAPSHOT":
            alert = _digest(e)
        elif e.kind == "QUARANTINE" and e.severity in urgent:
            alert = Alert(e.event_id, e.sim_time, e.severity, f"QUARANTINED {_label(e)}", _explain(e))
        elif e.kind == "STATUS" and e.status in EXCEPTION_STATUSES and e.severity in urgent:
            alerted.add(record)
            headline = (e.details or {}).get("headline") or e.what
            alert = Alert(
                f"{case}:{e.status}", e.sim_time, e.severity, f"{e.status} {_label(e)}: {headline}", _explain(e)
            )
        elif e.kind == "STATUS" and e.status == PROCESSED and record in alerted:
            alerted.discard(record)
            alert = Alert(f"{case}:RESOLVED", e.sim_time, "INFO", f"RESOLVED {_label(e)}", e.note or e.what)
        else:
            continue
        alerts.setdefault(alert.id, alert)  # the two sides of a case share one alert
    return list(alerts.values())


def write_alerts(alerts: list[Alert], path: Path) -> Path:
    """What Operations receives, readable without any channel configured."""
    lines = [f"{len(alerts)} alerts. Slack gets urgent alerts and resolutions; email gets the end-of-day digest.", ""]
    for a in alerts:
        lines.append(f"[{a.at}] {a.severity}  {a.title}")
        lines += [
            textwrap.fill(line, width=110, initial_indent="    ", subsequent_indent="        ")
            for line in a.body.splitlines()
        ]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ===================================================================== how to send it
def _send_slack(url: str, alert: Alert) -> None:
    text = f"*{alert.title}*\n{alert.body}"
    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):  # Slack's control characters
        text = text.replace(char, entity)
    request = Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
    urlopen(request, timeout=10).close()


def _send_email(host: str, sender: str, to: str, alert: Alert) -> None:
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = f"[recon] {alert.title}", sender, to
    message.set_content(alert.body)
    with smtplib.SMTP(host, timeout=10) as smtp:
        smtp.send_message(message)


def channels_from_env(env=os.environ) -> list[tuple]:
    """(name, send, wants). Settings come from the environment because a webhook URL is a secret.
    A new channel is one send function and one more entry. With no Slack, email carries everything,
    so nothing urgent is lost."""
    channels = []
    slack = env.get("RECON_SLACK_WEBHOOK_URL")
    if slack:
        channels.append(("slack", partial(_send_slack, slack), lambda a: not a.digest))
    if env.get("RECON_SMTP_HOST") and env.get("RECON_ALERT_TO"):
        send = partial(
            _send_email, env["RECON_SMTP_HOST"], env.get("RECON_ALERT_FROM", "recon@localhost"), env["RECON_ALERT_TO"]
        )
        channels.append(("email", send, (lambda a: a.digest) if slack else (lambda a: True)))
    return channels


def dispatch(alerts: list[Alert], channels: list[tuple], ledger: Path) -> None:
    """Send what has not gone out yet. A failing channel never stops the reconciliation;
    it waits for the next refresh."""
    sent = set(ledger.read_text().splitlines()) if ledger.exists() else set()
    for name, send, wants in channels:
        for alert in alerts:
            key = f"{name} {alert.id}"
            if key in sent or not wants(alert):
                continue
            try:
                send(alert)
            except (OSError, ValueError) as exc:
                print(f"[alerts] {name} failed ({type(exc).__name__}: {exc}); will retry", file=sys.stderr)
                break
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a") as fh:
                fh.write(key + "\n")
