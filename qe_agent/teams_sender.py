"""
Module 1C - Teams Sender (gửi qua email đến Teams channel).
Mỗi channel = 1 Teams channel email address (Forward to email).
"""
from __future__ import annotations
import os
import smtplib
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .models import (
    DailyReport, Ticket, RuleResult,
    CH_QE, CH_DEV_MS, CH_DEV_CRM, dev_channels_for,
)

# Load .env từ thư mục cha (project root)
_env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_file):
    for _ln in open(_env_file, encoding="utf-8"):
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#") and "=" in _ln:
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

LEVEL_NAME = {1: "🔴 LEVEL 1 — Violent", 2: "🟠 LEVEL 2 — Risk", 3: "🟡 LEVEL 3 — Commit Risk"}
LEVEL_COLOR = {1: "#d32f2f", 2: "#e65100", 3: "#f9a825"}
SECTION_COLOR = {
    "📋 Test Start Today":    "#1565c0",
    "✅ Test Complete Today":  "#2e7d32",
    "📦 Sandbox Tomorrow":    "#e65100",
    "⛔ Blocked":             "#b71c1c",
}

CHANNEL_EMAIL_ENV = {
    CH_QE:      "TEAMS_EMAIL_QE",
    CH_DEV_MS:  "TEAMS_EMAIL_DEV_MS",
    CH_DEV_CRM: "TEAMS_EMAIL_DEV_CRM",
}
CHANNEL_TITLE = {
    CH_QE: "QE Daily", CH_DEV_MS: "Dev — MS", CH_DEV_CRM: "Dev — CRM",
}


# ---------------------------------------------------------------------------
def _mention(name, no_qe=False):
    if not name:
        return "(unassigned)"
    return f"@{name} [NoQE]" if no_qe else f"@{name}"


# ---------------------------------------------------------------------------
# Routing: DailyReport → dict[channel → list[(header, [(ticket, extra)])]
# Tickets vi phạm nhiều rule trong cùng level → gộp thành 1 block, merge reasons.
# ---------------------------------------------------------------------------
def route(report: DailyReport) -> dict[str, list]:
    # buckets[channel][header][ticket_id] = (ticket, [reasons])
    buckets: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))

    def add(channels, header, ticket, reason: str | None = None):
        for ch in channels:
            tid = ticket.id
            if tid not in buckets[ch][header]:
                buckets[ch][header][tid] = (ticket, [])
            if reason:
                buckets[ch][header][tid][1].append(reason)

    for t in report.need_start_today:
        add([CH_QE] + dev_channels_for(t.component), "📋 Test Start Today", t)
    for t in report.need_complete_today:
        add([CH_QE] + dev_channels_for(t.component), "✅ Test Complete Today", t)
    for t in report.sandbox_tomorrow:
        add(dev_channels_for(t.component), "📦 Sandbox Tomorrow", t)
    for t in report.blocked:
        add(dev_channels_for(t.component), "⛔ Blocked", t)

    for results, lvl in [(report.level1, 1), (report.level2, 2), (report.level3, 3)]:
        for r in results:
            chans = ([CH_QE] if r.send_qe else []) + \
                    (dev_channels_for(r.ticket.component) if r.send_dev else [])
            add(chans, LEVEL_NAME[lvl], r.ticket, r.reason)

    # Flatten: dict[ticket_id -> (ticket, reasons)] → list[(ticket, extra)]
    out: dict[str, list] = {}
    for ch, by_header in buckets.items():
        sections = []
        for header, ticket_map in by_header.items():
            items = [
                (t, {"Reasons": reasons} if reasons else None)
                for t, reasons in ticket_map.values()
            ]
            sections.append((header, items))
        out[ch] = sections
    return out


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------
def _ticket_html(t: Ticket, extra: dict | None = None) -> str:
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    if base:
        key_html = (f'<a href="{base}/browse/{t.id}" style="color:#1565c0;'
                    f'font-family:monospace;font-weight:700;text-decoration:none">{t.id}</a>')
    else:
        key_html = f'<span style="font-family:monospace;font-weight:700">{t.id}</span>'

    rows = [
        ("Status",      t.status or "—"),
        ("Story Point", str(t.story_point) if t.story_point is not None else "—"),
        ("QC PIC",      _mention(t.qe_pic)),
        ("Assignee",    _mention(t.assignee, t.no_qe)),
    ]
    if t.component:
        rows.append(("Component", t.component))

    facts_html = "".join(
        f'<tr>'
        f'<td style="color:#888;font-size:12px;padding:3px 10px;white-space:nowrap">{k}</td>'
        f'<td style="font-size:13px;padding:3px 10px;color:#333">{v}</td>'
        f'</tr>'
        for k, v in rows
    )

    # Render reasons as numbered list if multiple, single line if one
    reasons_html = ""
    reasons = (extra or {}).get("Reasons", [])
    if reasons:
        if len(reasons) == 1:
            reasons_html = (
                f'<div style="margin-top:8px;padding:6px 10px;background:#fff8f8;'
                f'border-left:3px solid #d32f2f;border-radius:3px;font-size:13px">'
                f'{reasons[0]}</div>'
            )
        else:
            items = "".join(
                f'<li style="margin:3px 0">{r}</li>' for r in reasons
            )
            reasons_html = (
                f'<div style="margin-top:8px;padding:6px 10px;background:#fff8f8;'
                f'border-left:3px solid #d32f2f;border-radius:3px;font-size:13px">'
                f'<ol style="margin:0;padding-left:18px">{items}</ol>'
                f'</div>'
            )

    return (
        f'<div style="margin:6px 0;padding:10px 14px;background:#fff;'
        f'border:1px solid #e0e0e0;border-radius:6px">'
        f'<div style="font-weight:600;margin-bottom:6px">🔸 {key_html} — {t.title}</div>'
        f'<table style="border-collapse:collapse">{facts_html}</table>'
        f'{reasons_html}'
        f'</div>'
    )


def _build_html(channel_title: str, date_str: str,
                sections: list[tuple[str, list]]) -> str:
    sections_html = ""
    for header, items in sections:
        color = LEVEL_COLOR.get(
            next((k for k, v in LEVEL_NAME.items() if v == header), None),
            SECTION_COLOR.get(header, "#1565c0")
        )
        tickets_html = "".join(_ticket_html(t, extra) for t, extra in items)
        sections_html += (
            f'<div style="margin-bottom:16px">'
            f'<div style="background:{color};color:#fff;padding:9px 14px;'
            f'border-radius:6px 6px 0 0;font-weight:700;font-size:13px">'
            f'{header} &nbsp;<span style="opacity:.8;font-weight:400">({len(items)} ticket)</span>'
            f'</div>'
            f'<div style="padding:8px;background:#fafafa;border:1px solid #e0e0e0;'
            f'border-top:none;border-radius:0 0 6px 6px">{tickets_html}</div>'
            f'</div>'
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
             background:#f5f5f5;margin:0;padding:20px">
<div style="max-width:680px;margin:0 auto">
  <div style="background:#1565c0;border-radius:10px 10px 0 0;
              padding:20px 24px;color:#fff">
    <div style="font-size:18px;font-weight:700">🛡️ QA Watchdog — {channel_title}</div>
    <div style="font-size:13px;opacity:.85;margin-top:4px">📅 {date_str}</div>
  </div>
  <div style="background:#fff;border-radius:0 0 10px 10px;
              padding:20px 24px;border:1px solid #e0e0e0;border-top:none">
    {sections_html}
    <div style="margin-top:20px;padding-top:12px;border-top:1px solid #f0f0f0;
                font-size:11px;color:#aaa">
      Generated by QA Watchdog Agent • {date_str}
    </div>
  </div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# render_text — preview không cần Teams (dùng cho --dry-run)
# ---------------------------------------------------------------------------
def render_text(report: DailyReport) -> str:
    routed = route(report)
    out = []
    for ch in [CH_QE, CH_DEV_MS, CH_DEV_CRM]:
        out.append("=" * 55)
        out.append(f"### CHANNEL: {CHANNEL_TITLE[ch]}")
        out.append("=" * 55)
        sections = routed.get(ch)
        if not sections:
            out.append("(no messages)\n")
            continue
        for header, items in sections:
            out.append(f"\n[{header}]")
            for t, extra in items:
                out.append(f"  🔸 {t.id} — {t.title}")
                out.append(f"    Status={t.status}  SP={t.story_point}  "
                           f"QE={t.qe_pic or '—'}  Assignee={t.assignee}")
                reasons = (extra or {}).get("Reasons", [])
                for i, r in enumerate(reasons, 1):
                    prefix = f"    {i}." if len(reasons) > 1 else "    Reason:"
                    out.append(f"{prefix} {r}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------
def _send_one(to: str, subject: str, html: str, from_addr: str, password: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(from_addr, password)
        s.sendmail(from_addr, to, msg.as_string())


def send(report: DailyReport, dry_run: bool = False) -> None:
    routed = route(report)
    date_str = report.report_date.isoformat()

    if dry_run:
        print(render_text(report))
        return

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_pass:
        raise RuntimeError("Thiếu GMAIL_USER / GMAIL_APP_PASSWORD trong .env")

    for ch, sections in routed.items():
        if not sections:
            continue
        to_email = os.getenv(CHANNEL_EMAIL_ENV[ch])
        if not to_email:
            print(f"[skip] {CHANNEL_EMAIL_ENV[ch]} chưa set — bỏ qua {ch}")
            continue

        has_l1 = any(h == LEVEL_NAME[1] for h, _ in sections)
        has_l2 = any(h == LEVEL_NAME[2] for h, _ in sections)
        prefix = "🔴" if has_l1 else ("🟠" if has_l2 else "📋")
        subject = f"{prefix} QA Watchdog {date_str} — {CHANNEL_TITLE[ch]}"

        html = _build_html(CHANNEL_TITLE[ch], date_str, sections)
        _send_one(to_email, subject, html, gmail_user, gmail_pass)
        print(f"[sent] {subject} → {to_email}")
