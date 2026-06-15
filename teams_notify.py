#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Teams Notify — gửi QA Watchdog daily report vào Teams channel qua email.

Dùng:
  python3 teams_notify.py --snapshot jql_snapshot.json --date 2026-06-13
  python3 teams_notify.py --live --jql 'project = PC AND status not in (DONE,Cancelled)'
  python3 teams_notify.py --snapshot jql_snapshot.json --dry-run   # chỉ in email, không gửi
"""
import argparse, json, os, smtplib, sys
from collections import defaultdict
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
try:
    import urllib.request as _urllib_req
    _HAS_URLLIB = True
except ImportError:
    _HAS_URLLIB = False

# Auto-load .env
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from jira_adapter import normalize_issue, fetch_tickets, SCAN_JQL
from engine import RuleEngine

LEVEL_META = {
    0: ("DATA",  "⚪", "#9e9e9e"),
    1: ("L1",    "🔴", "#d32f2f"),
    2: ("L2",    "🟠", "#e65100"),
    3: ("L3",    "🟡", "#f9a825"),
}

JIRA_BASE_URL = "https://jira.zalopay.vn/browse"


# ---------------------------------------------------------------- fetch
def get_tickets(args):
    if args.live:
        jql = args.jql or SCAN_JQL
        return fetch_tickets(jql), jql
    snap = json.load(open(args.snapshot, encoding="utf-8"))
    issues = snap["issues"] if isinstance(snap, dict) else snap
    jql = snap.get("jql", "(snapshot)") if isinstance(snap, dict) else "(snapshot)"
    return [normalize_issue(i) for i in issues], jql


# ---------------------------------------------------------------- email builder
def build_email_html(result, tickets, jql, scan_date):
    by_lvl = defaultdict(list)
    for v in result["violations"]:
        by_lvl[v["level"]].append(v)

    counts = {lvl: len(by_lvl.get(lvl, [])) for lvl in LEVEL_META}
    total_violations = sum(counts.values())

    # Summary cards
    def stat(value, label, color="#1a1a2e"):
        return (
            f'<td style="text-align:center;padding:12px 16px;'
            f'background:#fff;border-radius:8px;border:1px solid #e0e0e0">'
            f'<div style="font-size:26px;font-weight:700;color:{color}">{value}</div>'
            f'<div style="font-size:11px;color:#777;margin-top:2px">{label}</div></td>'
        )

    stats_html = (
        f'<table cellspacing="8" style="margin:16px 0"><tr>'
        f'{stat(len(tickets), "Ticket quét")}'
        f'{stat(counts[1], "🔴 Level 1", "#d32f2f")}'
        f'{stat(counts[2], "🟠 Level 2", "#e65100")}'
        f'{stat(counts[0], "⚪ Data", "#757575")}'
        f'{stat(len(result["escalations"]), "🚨 Escalate", "#b71c1c")}'
        f'</tr></table>'
    )

    # Violations grouped by level → rule
    def ticket_link(key):
        if key.startswith("AGG:"):
            return (f'<span style="font-family:monospace;font-weight:600;color:#555">'
                    f'{key}</span>')
        return (f'<a href="{JIRA_BASE_URL}/{key}" target="_blank" '
                f'style="font-family:monospace;font-weight:600;color:#1565c0;'
                f'text-decoration:none">{key}</a>')

    violations_html = ""
    if by_lvl:
        level_sections = []
        for lvl in sorted(by_lvl):
            tag, icon, color = LEVEL_META[lvl]
            by_rule = defaultdict(list)
            for v in by_lvl[lvl]:
                by_rule[v["rule"]].append(v)

            rule_blocks = []
            for rule_id, viols in by_rule.items():
                trows = []
                for v in viols:
                    assignee = v.get("assignee") or "—"
                    qe_pic   = v.get("qe_pic")   or "—"
                    trows.append(
                        f'<tr style="border-bottom:1px solid #f5f5f5">'
                        f'<td style="padding:7px 10px;white-space:nowrap">{ticket_link(v["ticket"])}</td>'
                        f'<td style="padding:7px 10px;font-size:12px;color:#555;white-space:nowrap">{assignee}</td>'
                        f'<td style="padding:7px 10px;font-size:12px;color:#555;white-space:nowrap">{qe_pic}</td>'
                        f'<td style="padding:7px 10px;font-size:12px;color:#444">{v["msg"]}</td>'
                        f'</tr>'
                    )
                rule_blocks.append(
                    f'<div style="margin-bottom:10px">'
                    f'<div style="background:#f5f5f5;padding:7px 12px;border-radius:4px 4px 0 0;'
                    f'border-left:3px solid {color}">'
                    f'<span style="font-family:monospace;font-size:12px;font-weight:700;color:{color}">'
                    f'{rule_id}</span>'
                    f'<span style="color:#888;font-size:11px;margin-left:8px">{len(viols)} ticket</span>'
                    f'</div>'
                    f'<table style="width:100%;border-collapse:collapse;background:#fff;'
                    f'border:1px solid #e0e0e0;border-top:none;border-radius:0 0 4px 4px">'
                    f'<tr style="background:#fafafa">'
                    f'<th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;text-transform:uppercase">Ticket</th>'
                    f'<th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;text-transform:uppercase">Assignee</th>'
                    f'<th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;text-transform:uppercase">QE PIC</th>'
                    f'<th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;text-transform:uppercase">Chi tiết</th>'
                    f'</tr>'
                    f'{"".join(trows)}'
                    f'</table></div>'
                )

            level_sections.append(
                f'<div style="margin-bottom:20px">'
                f'<div style="background:{color};color:#fff;padding:10px 14px;'
                f'border-radius:6px 6px 0 0;font-size:13px;font-weight:700">'
                f'{icon} {tag} — {len(by_lvl[lvl])} vi phạm</div>'
                f'<div style="padding:12px;background:#fafafa;border:1px solid #e0e0e0;'
                f'border-top:none;border-radius:0 0 6px 6px">'
                f'{"".join(rule_blocks)}'
                f'</div></div>'
            )

        violations_html = (
            f'<h3 style="margin:24px 0 8px;font-size:14px;color:#333">Chi tiết vi phạm</h3>'
            f'{"".join(level_sections)}'
        )
    else:
        violations_html = (
            '<div style="background:#e8f5e9;border-radius:8px;padding:16px;'
            'color:#2e7d32;margin:16px 0">Không có vi phạm hôm nay! 🎉</div>'
        )

    # Escalations
    esc_html = ""
    if result["escalations"]:
        esc_items = "".join(
            f'<div style="margin:6px 0;padding:10px 14px;background:#ffebee;'
            f'border-left:4px solid #c62828;border-radius:4px">{e}</div>'
            for e in result["escalations"]
        )
        esc_html = f'<h3 style="margin:24px 0 8px;font-size:14px;color:#c62828">🚨 Escalations</h3>{esc_items}'

    # Risk-resolved: rule cleared today but ticket still has other active violations
    risk_resolved_html = ""
    if result.get("risk_resolved"):
        rr_items = "".join(
            f'<div style="margin:4px 0;padding:8px 12px;background:#fff8e1;'
            f'border-left:4px solid #f9a825;border-radius:4px;font-size:13px">'
            f'⚠️ {r}</div>'
            for r in result["risk_resolved"]
        )
        risk_resolved_html = (
            f'<h3 style="margin:24px 0 8px;font-size:14px;color:#f57f17">'
            f'⚠️ Risk: rule cleared but ticket still active ({len(result["risk_resolved"])})</h3>'
            f'<div style="font-size:12px;color:#888;margin-bottom:8px">'
            f'These rules no longer fire, but the ticket has other violations today.</div>'
            f'{rr_items}'
        )

    # Notes — violations carried over from previous scan
    notes_html = ""
    if result.get("repeat_today"):
        note_rows = "".join(
            f'<tr style="border-bottom:1px solid #f5f5f5">'
            f'<td style="padding:7px 10px;white-space:nowrap">{ticket_link(n["ticket"])}</td>'
            f'<td style="padding:7px 10px;font-family:monospace;font-size:12px;color:#555">{n["rule"]}</td>'
            f'<td style="padding:7px 10px;font-size:12px;color:#444">{n["msg"]}</td>'
            f'</tr>'
            for n in result["repeat_today"]
        )
        notes_html = (
            f'<h3 style="margin:24px 0 8px;font-size:14px;color:#555">📌 Notes — Tái phạm từ lần scan trước'
            f' ({len(result["repeat_today"])})</h3>'
            f'<div style="font-size:12px;color:#888;margin-bottom:8px">'
            f'Các ticket dưới đây đã vi phạm rule này từ lần scan trước và vẫn chưa xử lý.</div>'
            f'<table style="width:100%;border-collapse:collapse;background:#fff;'
            f'border:1px solid #e0e0e0;border-radius:4px">'
            f'<tr style="background:#fafafa">'
            f'<th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;text-transform:uppercase">Ticket</th>'
            f'<th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;text-transform:uppercase">Rule</th>'
            f'<th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;text-transform:uppercase">Chi tiết</th>'
            f'</tr>{note_rows}</table>'
        )

    subject_prefix = "🔴" if counts[1] > 0 else ("🟠" if counts[2] > 0 else "✅")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
             background:#f5f5f5;margin:0;padding:20px">
<div style="max-width:680px;margin:0 auto;background:#f5f5f5">

  <!-- Header -->
  <div style="background:#1565c0;border-radius:10px 10px 0 0;
              padding:20px 24px;color:#fff">
    <div style="font-size:18px;font-weight:700">🛡️ QA Watchdog — Daily Report</div>
    <div style="font-size:13px;opacity:.85;margin-top:4px">Ngày scan: {scan_date}</div>
    <div style="font-size:11px;opacity:.65;margin-top:2px;word-break:break-all">
      JQL: {jql}</div>
  </div>

  <!-- Body -->
  <div style="background:#fff;border-radius:0 0 10px 10px;
              padding:20px 24px;border:1px solid #e0e0e0;border-top:none">
    {stats_html}
    {esc_html}
    {violations_html}
    {risk_resolved_html}
    {notes_html}
    <div style="margin-top:24px;padding-top:16px;border-top:1px solid #f0f0f0;
                font-size:11px;color:#aaa">
      Generated by QA Watchdog Agent • {scan_date}
    </div>
  </div>
</div>
</body></html>"""

    subject = f"{subject_prefix} QA Watchdog {scan_date} — {total_violations} vi phạm"
    return subject, html, subject_prefix


# ---------------------------------------------------------------- plain text builder
def build_plain_text(result, tickets, scan_date):
    by_lvl = defaultdict(list)
    for v in result["violations"]:
        by_lvl[v["level"]].append(v)

    counts = {lvl: len(by_lvl.get(lvl, [])) for lvl in LEVEL_META}
    total = sum(counts.values())

    lines = [
        f"QA WATCHDOG — Daily Report — {scan_date}",
        "=" * 50,
        f"Ticket quét: {len(tickets)} | Vi phạm: {total}",
        f"🔴 L1: {counts[1]}  🟠 L2: {counts[2]}  🟡 L3: {counts[3]}  ⚪ Data: {counts[0]}",
        "",
    ]

    for lvl in sorted(by_lvl):
        tag, icon, _ = LEVEL_META[lvl]
        lines.append(f"--- {icon} {tag} ({len(by_lvl[lvl])}) ---")
        by_rule = defaultdict(list)
        for v in by_lvl[lvl]:
            by_rule[v["rule"]].append(v)
        for rule_id, viols in by_rule.items():
            lines.append(f"  [{rule_id}] ({len(viols)} ticket)")
            for v in viols:
                assignee = v.get("assignee") or "—"
                qe_pic   = v.get("qe_pic")   or "—"
                lines.append(f"    {v['ticket']}  assignee={assignee}  qe_pic={qe_pic}")
                lines.append(f"      {v['msg']}")
        lines.append("")

    if result["escalations"]:
        lines.append("--- 🚨 ESCALATIONS ---")
        for e in result["escalations"]:
            lines.append(f"  {e}")
        lines.append("")

    if result.get("risk_resolved"):
        lines.append("--- ⚠️ RISK: RULE CLEARED BUT TICKET STILL ACTIVE ---")
        for r in result["risk_resolved"]:
            lines.append(f"  {r}")
        lines.append("")

    lines.append(f"Generated by QA Watchdog Agent • {scan_date}")
    return "\n".join(lines)


# ---------------------------------------------------------------- send
def send_email(subject, html_body, plain_body, to_addr, from_addr, app_password, dry_run=False):
    if dry_run:
        print(f"[dry-run] Subject: {subject}")
        print(f"[dry-run] To: {to_addr}")
        print("[dry-run] Plain text body:")
        print(plain_body[:1000])
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(from_addr, app_password)
        s.sendmail(from_addr, to_addr, msg.as_string())
    print(f"[sent] {subject}")


# ---------------------------------------------------------------- Teams webhook (MessageCard)
def send_webhook(webhook_url, result, tickets, scan_date, dry_run=False):
    by_lvl = defaultdict(list)
    for v in result["violations"]:
        by_lvl[v["level"]].append(v)

    counts = {lvl: len(by_lvl.get(lvl, [])) for lvl in LEVEL_META}
    total = sum(counts.values())
    theme = "d32f2f" if counts[1] > 0 else ("e65100" if counts[2] > 0 else "43a047")

    sections = [
        {
            "facts": [
                {"name": "Ticket quét",    "value": str(len(tickets))},
                {"name": "🔴 L1 Vi phạm", "value": str(counts[1])},
                {"name": "🟠 L2 Rủi ro",  "value": str(counts[2])},
                {"name": "🟡 L3 Warning",  "value": str(counts[3])},
                {"name": "⚪ Data hygiene","value": str(counts[0])},
            ]
        }
    ]

    for lvl in sorted(by_lvl):
        tag, icon, _ = LEVEL_META[lvl]
        lines = []
        for v in by_lvl[lvl]:
            lines.append(f"**{v['ticket']}** `{v['rule']}`  \n{v['msg']}")
        sections.append({
            "title": f"{icon} {tag} — {len(by_lvl[lvl])} vi phạm",
            "text": "\n\n".join(lines),
        })

    if result["escalations"]:
        sections.append({
            "title": "🚨 Escalations",
            "text": "\n\n".join(f"• {e}" for e in result["escalations"]),
        })

    card = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": f"QA Watchdog {scan_date}",
        "themeColor": theme,
        "title": f"🛡️ QA Watchdog {scan_date} — {total} vi phạm",
        "sections": sections,
    }

    if dry_run:
        print(f"[dry-run webhook] POST → {webhook_url}")
        print(json.dumps(card, indent=2, ensure_ascii=False)[:2000])
        return

    payload = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = _urllib_req.Request(
        webhook_url, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with _urllib_req.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
    if body.strip() != "1":
        print(f"[webhook] response: {body[:200]}", file=sys.stderr)
    print(f"[sent webhook] 🛡️ QA Watchdog {scan_date} — {total} vi phạm")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="QA Watchdog — Teams Notify via Email")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--live", action="store_true")
    src.add_argument("--snapshot", metavar="FILE")
    ap.add_argument("--jql")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--rules", default="rules.yaml")
    ap.add_argument("--db", default="watchdog.db")
    ap.add_argument("--dry-run", action="store_true", help="In email ra màn hình, không gửi")
    args = ap.parse_args()

    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    gmail_user  = os.environ.get("GMAIL_USER")
    gmail_pass  = os.environ.get("GMAIL_APP_PASSWORD")
    teams_email = os.environ.get("TEAMS_CHANNEL_EMAIL")

    if not args.dry_run and not webhook_url:
        missing = [k for k, v in [
            ("GMAIL_USER", gmail_user),
            ("GMAIL_APP_PASSWORD", gmail_pass),
            ("TEAMS_CHANNEL_EMAIL", teams_email),
        ] if not v]
        if missing:
            print(f"ERROR: Thiếu env vars: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

    tickets, jql = get_tickets(args)
    print(f"[fetch] {len(tickets)} tickets", file=sys.stderr)

    eng = RuleEngine(args.rules, db_path=args.db)
    result = eng.scan(args.date, tickets)
    print(
        f"[scan] {len(result['violations'])} vi phạm | "
        f"{len(result['resolved'])} resolved | "
        f"{len(result['escalations'])} escalation",
        file=sys.stderr,
    )

    if webhook_url or args.dry_run:
        send_webhook(webhook_url or "https://dry-run.example", result, tickets,
                     args.date, dry_run=args.dry_run)
    else:
        subject, html, _ = build_email_html(result, tickets, jql, args.date)
        plain = build_plain_text(result, tickets, args.date)
        send_email(
            subject, html, plain,
            to_addr=teams_email or "dry-run@example.com",
            from_addr=gmail_user or "dry-run@example.com",
            app_password=gmail_pass or "",
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
