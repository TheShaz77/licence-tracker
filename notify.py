"""Optional email digest of the day's changes (SMTP, e.g. Gmail app password)."""
from __future__ import annotations

import html
import os
import smtplib
from collections import Counter
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def enabled() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))


def build(events: list, problems: list, sheet_url: str) -> tuple:
    counts = Counter(e["event_label"] for e in events)
    subject = f"Licence tracker: {len(events)} change(s)" + (
        f" ({', '.join(f'{n} {k.lower()}' for k, n in counts.most_common(3))})" if counts else "")
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(e.get(k, '')))}</td>" for k in
                         ("event_label", "jurisdiction", "regulator", "name", "licence_type", "old", "new", "reference"))
        + "</tr>" for e in events[:500])
    more = f"<p>…and {len(events) - 500} more in the sheet.</p>" if len(events) > 500 else ""
    probs = "".join(f"<li>{html.escape(p)}</li>" for p in problems)
    body = f"""<html><body style="font-family:Arial,sans-serif;font-size:13px">
<p><b>{len(events)}</b> licence change(s) detected.</p>
{f'<p>Sources needing attention:</p><ul>{probs}</ul>' if probs else ''}
<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse">
<tr style="background:#eee"><th>Event</th><th>Jurisdiction</th><th>Regulator</th><th>Firm</th>
<th>Licence type</th><th>Before</th><th>After</th><th>Ref.</th></tr>{rows}</table>{more}
{f'<p><a href="{html.escape(sheet_url)}">Open the full list</a></p>' if sheet_url else ''}
</body></html>"""
    return subject, body


def send(events: list, problems: list, log):
    sheet_url = f"https://docs.google.com/spreadsheets/d/{os.environ['SHEET_ID']}" if os.environ.get("SHEET_ID") else ""
    subject, body = build(events, problems, sheet_url)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(body, "html", "utf-8"))
    port = int(os.environ.get("SMTP_PORT") or 587)
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=60) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.sendmail(msg["From"], [a.strip() for a in os.environ["EMAIL_TO"].split(",")], msg.as_string())
    log.info("Email digest sent to %s", os.environ["EMAIL_TO"])
