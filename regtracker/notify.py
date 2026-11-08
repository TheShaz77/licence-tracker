"""Change alerts.

Two channels, used independently:
- GitHub issue (default): the day's changes are posted as an issue in the
  repo, and GitHub emails the repo owner about it. Needs only the workflow's
  built-in GITHUB_TOKEN with `issues: write` permission.
- SMTP email (optional): if SMTP_* secrets are set.
- Telegram (optional): if the TELEGRAM_BOT_TOKEN secret is set. Sends new
  licences / registrations only (configurable in config.yaml, `telegram:`).
"""
from __future__ import annotations

import html
import os
import smtplib
from collections import Counter
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

ISSUE_ROW_LIMIT = 300        # keeps the issue under GitHub's 65,536-character limit
BODY_LIMIT = 60000


def smtp_enabled() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))


def github_enabled() -> bool:
    return (os.environ.get("ALERTS_VIA_GITHUB", "true").lower() != "false"
            and bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY")))


def telegram_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


def enabled() -> bool:
    return smtp_enabled() or github_enabled() or telegram_enabled()


def _subject(events: list, problems: list) -> str:
    counts = Counter(e["event_label"] for e in events)
    s = f"Licence tracker: {len(events)} change(s)"
    if counts:
        s += f" ({', '.join(f'{n} {k.lower()}' for k, n in counts.most_common(3))})"
    if problems and not events:
        s = "Licence tracker: a register could not be read"
    return s


def _md(v) -> str:
    return str(v or "").replace("|", "\\|").replace("\n", " ").strip()


def build_issue(events: list, problems: list, sheet_url: str, today: str) -> tuple:
    title = _subject(events, problems).replace("Licence tracker:", f"Licence changes {today}:")
    lines = []
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if owner:
        lines.append(f"cc @{owner}\n")
    lines.append(f"**{len(events)}** licence change(s) detected.")
    if sheet_url:
        lines.append(f"[Open the Google Sheet]({sheet_url})")
    if problems:
        lines.append("\n**Registers that couldn't be read today** (they keep yesterday's data):")
        lines += [f"- {_md(p)}" for p in problems]
    if events:
        lines.append("\n| Event | Country | Regulator | Firm | Licence type | Before | After | Ref. |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for e in events[:ISSUE_ROW_LIMIT]:
            name = _md(e.get("name"))
            if e.get("register_url"):
                name = f"[{name}]({e['register_url']})"
            lines.append("| " + " | ".join([
                _md(e.get("event_label")), _md(e.get("jurisdiction")), _md(e.get("regulator")), name,
                _md(e.get("licence_type")), _md(e.get("old")), _md(e.get("new")), _md(e.get("reference"))]) + " |")
        if len(events) > ISSUE_ROW_LIMIT:
            lines.append(f"\n…and {len(events) - ISSUE_ROW_LIMIT} more on the sheet's Changes tab.")
    run = os.environ.get("GITHUB_RUN_ID")
    if run:
        lines.append(f"\n<sub>[Run log]({os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                     f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run})</sub>")
    body = "\n".join(lines)
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n\n…(truncated; see the sheet's Changes tab)"
    return title, body


def send_github_issue(events: list, problems: list, sheet_url: str, today: str, log):
    title, body = build_issue(events, problems, sheet_url, today)
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues"
    r = requests.post(url, json={"title": title, "body": body}, timeout=60, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code == 403:
        raise RuntimeError("GitHub refused to create the alert issue: add 'issues: write' under "
                           "'permissions:' in .github/workflows/daily.yml")
    if r.status_code == 410:
        raise RuntimeError("Issues are switched off for this repo: Settings → General → Features → tick Issues")
    r.raise_for_status()
    log.info("Alert posted as GitHub issue %s", r.json().get("html_url"))


def build(events: list, problems: list, sheet_url: str) -> tuple:
    subject = _subject(events, problems)
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


def send(events: list, problems: list, log, today: str = "", telegram_config: dict | None = None):
    sheet_url = f"https://docs.google.com/spreadsheets/d/{os.environ['SHEET_ID']}" if os.environ.get("SHEET_ID") else ""
    if not today:
        import datetime as dt
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    errors = []
    if github_enabled():
        try:
            send_github_issue(events, problems, sheet_url, today, log)
        except Exception as e:
            errors.append(e)
    if smtp_enabled():
        try:
            send_email(events, problems, sheet_url, log)
        except Exception as e:
            errors.append(e)
    if telegram_enabled():
        try:
            send_telegram(events, sheet_url, log, telegram_config)
        except Exception as e:
            errors.append(e)
    if errors:
        raise errors[0]


def send_email(events: list, problems: list, sheet_url: str, log):
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


# ---------------------------------------------------------------- Telegram

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TG_LIMIT = 3900          # Telegram allows 4096 characters per message
TG_MAX_LINES = 150       # beyond this, point to the sheet instead


def flag(cc: str) -> str:
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc)


def is_new_registration(e: dict) -> bool:
    """New licences, registrations and applications, plus a PSP moving from
    applicant to registered (the Bank of Canada keeps the same record)."""
    if e.get("event") == "NEW_LICENCE":
        return True
    return e.get("event") == "TYPE_CHANGED" and "registered" in (e.get("event_label") or "").lower()


def telegram_filter(events: list, cfg: dict | None) -> list:
    cfg = cfg or {}
    kinds = set(cfg.get("events") or [])
    countries = {c.upper() for c in (cfg.get("countries") or [])}
    out = []
    for e in events:
        if kinds:
            if e.get("event") not in kinds:
                continue
        elif not is_new_registration(e):
            continue
        if countries and (e.get("jurisdiction") or "").upper() not in countries:
            continue
        out.append(e)
    return out


def telegram_messages(events: list, sheet_url: str) -> list:
    """HTML messages, grouped by country, each under Telegram's size limit."""
    esc = html.escape
    by_cc = {}
    for e in events:
        by_cc.setdefault(e.get("jurisdiction") or "", []).append(e)
    head = f"<b>🆕 {len(events)} new licence{'s' if len(events) != 1 else ''} / registration{'s' if len(events) != 1 else ''}</b>"
    lines, shown = [], 0
    for cc in sorted(by_cc, key=lambda c: (-len(by_cc[c]), c)):
        if shown >= TG_MAX_LINES:
            break
        lines.append(f"\n{flag(cc)} <b>{esc(cc or 'Other')}</b> ({len(by_cc[cc])})")
        for e in by_cc[cc]:
            if shown >= TG_MAX_LINES:
                break
            what = e.get("event_label") if e.get("event_label") not in ("New licence", "") else e.get("licence_type")
            name = esc(e.get("name") or "")
            link = e.get("register_url") or ""
            name = f'<a href="{esc(link)}">{name}</a>' if link.startswith("http") else name
            reg = esc(e.get("regulator") or "")
            lines.append(f"• {name} — {esc(what or '')}" + (f" · {reg}" if reg else ""))
            shown += 1
    tail = []
    if shown < len(events):
        tail.append(f"\n…and {len(events) - shown} more.")
    if sheet_url:
        tail.append(f'<a href="{esc(sheet_url)}">Open the sheet</a> (Changes tab)')
    msgs, cur = [], head
    for l in lines + tail:
        if len(cur) + len(l) + 1 > TG_LIMIT:
            msgs.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + l
    if cur:
        msgs.append(cur)
    return msgs


def telegram_chat_id(token: str, log) -> str:
    """TELEGRAM_CHAT_ID if set; otherwise the chat that last messaged the bot
    (send your bot any message once), remembered in data/state/telegram.json."""
    import json
    from pathlib import Path
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"].strip()
    store = Path(os.environ.get("REGTRACKER_DATA", "data")) / "state" / "telegram.json"
    saved = json.loads(store.read_text()).get("chat_id", "") if store.exists() else ""
    r = requests.get(TELEGRAM_API.format(token=token, method="getUpdates"), timeout=30)
    r.raise_for_status()
    chats = [u.get("message", {}).get("chat", {}).get("id") for u in r.json().get("result", [])
             if u.get("message", {}).get("chat", {}).get("type") == "private"]
    chat = str(chats[-1]) if chats else saved
    if chat and chat != saved:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"chat_id": chat}))
        requests.post(TELEGRAM_API.format(token=token, method="sendMessage"), timeout=30, json={
            "chat_id": chat, "parse_mode": "HTML",
            "text": "✅ <b>Licence tracker connected.</b>\nYou'll get a message here whenever a check finds new "
                    "licences or registrations."})
        log.info("Telegram: connected to your chat and sent a confirmation message")
    if not chat:
        log.warning("Telegram: no chat yet. Open your bot in Telegram and send it any message (e.g. 'hi').")
    return chat


def send_telegram(events: list, sheet_url: str, log, cfg: dict | None = None):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat = telegram_chat_id(token, log)
    wanted = telegram_filter(events, cfg)
    if not chat or not wanted:
        log.info("Telegram: %d new licence(s)/registration(s) to send", len(wanted))
        return
    for text in telegram_messages(wanted, sheet_url):
        r = requests.post(TELEGRAM_API.format(token=token, method="sendMessage"), timeout=30,
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.status_code != 200:
            raise RuntimeError(f"Telegram: {r.status_code} {r.text[:200]}")
    log.info("Telegram: sent %d new licence(s)/registration(s)", len(wanted))


def telegram_connect(log) -> str:
    """Run every time: picks up your 'hi' to the bot before Telegram forgets it (24 hours)."""
    try:
        return telegram_chat_id(os.environ["TELEGRAM_BOT_TOKEN"].strip(), log)
    except Exception as e:
        log.warning("Telegram: could not connect (%s)", e)
        return ""
