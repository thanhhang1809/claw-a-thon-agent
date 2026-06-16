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
# NOTE: Teams/Outlook render HTML qua engine rất hạn chế — KHÔNG hỗ trợ flexbox,
# box-shadow, linear-gradient, span-background (Outlook desktop). Toàn bộ layout
# dưới đây dùng <table> + thuộc tính bgcolor (bền với dark-mode auto-invert).
# Status hiển thị bằng CHỮ MÀU đậm thay vì pill (pill rớt nền là vô hình ở dark mode).

# Status -> màu chữ (độ sáng trung bình: đọc tốt trên cả nền trắng lẫn nền tối)
_STATUS_COLOR = {
    "Ready for testing": "#2563eb",
    "InTest":            "#16a34a",
    "InReview":          "#9333ea",
    "InDev":             "#ea580c",
    "In Progress":       "#ea580c",
    "Walkthrough":       "#ca8a04",
    "Blocked":           "#dc2626",
    "New":               "#6b7280",
    "Open":              "#6b7280",
    "Done":              "#16a34a",
}

def _status_label(status_canonical, display_text=None):
    color = _STATUS_COLOR.get(status_canonical, "#6b7280")
    label = display_text or status_canonical or "—"
    return (f'<span style="font-size:12px;font-weight:700;color:{color};'
            f'white-space:nowrap">{label}</span>')


def _accent_block(bg, border, inner, text_color="#374151"):
    """Khối nhấn (escalation/risk) — table 1 ô để bgcolor sống được ở dark mode."""
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;margin:6px 0">'
        f'<tr><td class="acc" bgcolor="{bg}" style="background:{bg};border-left:4px solid {border};'
        f'padding:10px 14px;font-size:13px;color:{text_color};line-height:1.5">{inner}</td></tr></table>'
    )


def build_email_html(result, tickets, jql, scan_date, rule_names=None):
    # Build lookup: ticket key -> full ticket dict (for status & summary)
    ticket_map = {t["key"]: t for t in tickets}
    # rule_names: {rule_id -> human-readable name}; fallback = id itself
    rule_names = rule_names or {}

    by_lvl = defaultdict(list)
    for v in result["violations"]:
        by_lvl[v["level"]].append(v)

    counts = {lvl: len(by_lvl.get(lvl, [])) for lvl in LEVEL_META}
    total_violations = sum(counts.values())

    # ── Ticket link helper ───────────────────────────────────────────────────
    def ticket_link(key):
        if key.startswith("AGG:"):
            return (f'<span class="t1" style="font-family:monospace;font-size:13px;font-weight:700;'
                    f'color:#555555">{key}</span>')
        return (f'<a class="lnk" href="{JIRA_BASE_URL}/{key}" target="_blank" '
                f'style="font-family:monospace;font-size:13px;font-weight:700;'
                f'color:#1565c0;text-decoration:none">{key}</a>')

    # ── Violations ──────────────────────────────────────────────────────────
    # NOTE: class="th"/"td"/... để @media dark override (xem <style> dark block dưới).
    TH = ('font-size:10px;color:#9ca3af;letter-spacing:.5px;'
          'font-weight:700;padding:8px 12px;text-align:left;border-bottom:1px solid #e5e7eb')
    TD = 'padding:9px 12px;vertical-align:top;border-bottom:1px solid #f3f4f6'

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
                    ti            = ticket_map.get(v["ticket"], {})
                    status_canon  = ti.get("status") or ""
                    status_raw    = ti.get("status_raw") or status_canon
                    summary       = ti.get("summary") or ""
                    assignee      = v.get("assignee") or "—"
                    qe_pic        = v.get("qe_pic")   or "—"

                    ticket_cell = (
                        f'<div>{ticket_link(v["ticket"])}</div>'
                        + (f'<div class="t2" style="font-size:11px;color:#6b7280;margin-top:3px">'
                           f'{summary}</div>' if summary else "")
                        + (f'<div class="t2" style="font-size:11px;color:#9ca3af;margin-top:2px;'
                           f'font-style:italic">{v["msg"]}</div>' if v.get("msg") else "")
                    )

                    trows.append(
                        f'<tr>'
                        f'<td class="td bd" style="{TD}">{ticket_cell}</td>'
                        f'<td class="td bd" style="{TD};white-space:nowrap">{_status_label(status_canon, status_raw)}</td>'
                        f'<td class="td bd t1" style="{TD};font-size:12px;color:#374151;white-space:nowrap">{assignee}</td>'
                        f'<td class="td bd t1" style="{TD};font-size:12px;color:#374151;white-space:nowrap">{qe_pic}</td>'
                        f'</tr>'
                    )

                rule_display = rule_names.get(rule_id, rule_id)
                # Rule header: table 2 ô (tên trái / count phải) thay cho flexbox
                rule_blocks.append(
                    f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                    f'class="bd" style="border:1px solid #e5e7eb;border-collapse:collapse;margin-bottom:12px">'
                    f'<tr><td class="sub bd" bgcolor="#f9fafb" style="background:#f9fafb;'
                    f'border-bottom:1px solid #e5e7eb;border-left:4px solid {color};padding:8px 12px">'
                    f'<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
                    f'<td style="font-size:13px;font-weight:700;color:{color};line-height:1.4">'
                    f'{rule_display}</td>'
                    f'<td align="right" style="font-size:11px;font-weight:700;color:{color};'
                    f'white-space:nowrap;vertical-align:top">{len(viols)} ticket</td>'
                    f'</tr></table></td></tr>'
                    f'<tr><td style="padding:0">'
                    f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                    f'class="card" bgcolor="#ffffff" style="background:#ffffff;border-collapse:collapse">'
                    f'<tr>'
                    f'<th class="th bd" style="{TH}">Ticket</th>'
                    f'<th class="th bd" style="{TH}">Status</th>'
                    f'<th class="th bd" style="{TH}">Assignee</th>'
                    f'<th class="th bd" style="{TH}">QE PIC</th>'
                    f'</tr>'
                    f'{"".join(trows)}'
                    f'</table></td></tr></table>'
                )

            # Level section: header row (bgcolor=màu level) + body row
            # Header bar (bgcolor=màu level) giữ nguyên ở cả light/dark — chữ trắng luôn rõ
            level_sections.append(
                f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="border-collapse:collapse;margin-bottom:22px">'
                f'<tr><td bgcolor="{color}" style="background:{color};color:#ffffff;'
                f'padding:11px 16px;font-size:14px;font-weight:700">'
                f'{icon} {tag} — {len(by_lvl[lvl])} vi phạm</td></tr>'
                f'<tr><td class="sub2 bd" bgcolor="#fafafa" style="background:#fafafa;padding:12px 12px 1px;'
                f'border:1px solid #e5e7eb;border-top:none">'
                f'{"".join(rule_blocks)}'
                f'</td></tr></table>'
            )

        violations_html = (
            f'<div class="t1 bd" style="margin:22px 0 10px;padding-bottom:8px;border-bottom:2px solid #f3f4f6;'
            f'font-size:14px;font-weight:700;color:#111827">Chi tiết vi phạm</div>'
            f'{"".join(level_sections)}'
        )
    else:
        violations_html = (
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:18px 0">'
            f'<tr><td class="ok" bgcolor="#ecfdf5" style="background:#ecfdf5;border:1px solid #a7f3d0;'
            f'padding:20px;text-align:center;color:#065f46;font-size:15px;font-weight:700">'
            f'🎉 Không có vi phạm hôm nay!</td></tr></table>'
        )

    # ── Escalations ─────────────────────────────────────────────────────────
    esc_html = ""
    if result["escalations"]:
        esc_items = "".join(_accent_block("#fef2f2", "#dc2626", e) for e in result["escalations"])
        esc_html = (
            f'<div style="margin:22px 0 8px;font-size:14px;font-weight:700;color:#dc2626">'
            f'🚨 Escalations</div>{esc_items}'
        )

    # ── Risk-resolved ────────────────────────────────────────────────────────
    risk_resolved_html = ""
    if result.get("risk_resolved"):
        rr_items = "".join(
            _accent_block("#fffbeb", "#f59e0b", f"⚠️ {r}") for r in result["risk_resolved"]
        )
        risk_resolved_html = (
            f'<div style="margin:22px 0 6px;font-size:14px;font-weight:700;color:#b45309">'
            f'⚠️ Risk: rule cleared but ticket still active ({len(result["risk_resolved"])})</div>'
            f'<div style="font-size:12px;color:#9ca3af;margin-bottom:8px">'
            f'Các rule này không còn fire, nhưng ticket vẫn có vi phạm khác hôm nay.</div>'
            f'{rr_items}'
        )

    # ── Notes (repeat today) ─────────────────────────────────────────────────
    notes_html = ""
    if result.get("repeat_today"):
        note_rows = "".join(
            f'<tr>'
            f'<td class="td bd" style="{TD};white-space:nowrap">{ticket_link(n["ticket"])}</td>'
            f'<td class="td bd t2" style="{TD};font-family:monospace;font-size:12px;color:#6b7280">{n["rule"]}</td>'
            f'<td class="td bd t1" style="{TD};font-size:12px;color:#374151">{n["msg"]}</td>'
            f'</tr>'
            for n in result["repeat_today"]
        )
        notes_html = (
            f'<div class="t2" style="margin:22px 0 6px;font-size:14px;font-weight:700;color:#6b7280">'
            f'📌 Tái phạm từ lần scan trước ({len(result["repeat_today"])})</div>'
            f'<div class="t2" style="font-size:12px;color:#9ca3af;margin-bottom:8px">'
            f'Các ticket đã vi phạm rule này lần scan trước và vẫn chưa xử lý.</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'class="card bd" bgcolor="#ffffff" style="background:#ffffff;border-collapse:collapse;'
            f'border:1px solid #e5e7eb">'
            f'<tr>'
            f'<th class="th bd" style="{TH}">Ticket</th>'
            f'<th class="th bd" style="{TH}">Rule</th>'
            f'<th class="th bd" style="{TH}">Chi tiết</th>'
            f'</tr>{note_rows}</table>'
        )

    subject_prefix = "🔴" if counts[1] > 0 else ("🟠" if counts[2] > 0 else "✅")
    header_bg = "#c62828" if counts[1] > 0 else ("#e65100" if counts[2] > 0 else "#1b5e20")

    html = f"""<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- Hỗ trợ cả light & dark; Teams mobile (webview) đọc @media bên dưới -->
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<style>
  :root {{ color-scheme: light dark; }}
  /* DARK MODE — chỉ Teams/Outlook mobile (webview) hỗ trợ; desktop bỏ qua, ra light.
     Inline style thắng <style> nên phải dùng !important để override. */
  @media (prefers-color-scheme: dark) {{
    body, .page         {{ background:#161616 !important; }}
    .card               {{ background:#242424 !important; }}
    .sub                {{ background:#2d2d2d !important; }}
    .sub2               {{ background:#1d1d1d !important; }}
    .t1                 {{ color:#e7e9ee !important; }}
    .t2                 {{ color:#9aa3b2 !important; }}
    .lnk                {{ color:#6db1ff !important; }}
    .th                 {{ color:#9aa3b2 !important; }}
    .bd                 {{ border-color:#3a3a3a !important; }}
    .acc                {{ background:#2a2a2a !important; color:#e7e9ee !important; }}
    .ok                 {{ background:#14271d !important; color:#86efac !important;
                           border-color:#14532d !important; }}
  }}
</style>
</head>
<body class="page" style="margin:0;padding:0;background:#f3f4f6;
             font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<table class="page" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f3f4f6"
       style="background:#f3f4f6">
<tr><td align="center" style="padding:24px 12px">

  <table width="700" cellpadding="0" cellspacing="0" border="0"
         style="max-width:700px;width:100%;border-collapse:collapse">

    <!-- Header (bgcolor=màu mức cao nhất; chữ trắng luôn rõ ở cả 2 chế độ) -->
    <tr><td bgcolor="{header_bg}" style="background:{header_bg};padding:20px 26px">
      <div style="font-size:18px;font-weight:800;color:#ffffff;letter-spacing:-.3px">
        🛡️ QE Watchdog — Daily Report</div>
      <div style="font-size:13px;color:#ffffff;opacity:.85;margin-top:4px">📅 {scan_date}</div>
    </td></tr>

    <!-- Body -->
    <tr><td class="card bd" bgcolor="#ffffff" style="background:#ffffff;padding:18px 26px 24px;
            border:1px solid #e5e7eb;border-top:none">
      {esc_html}
      {violations_html}
      {risk_resolved_html}
      {notes_html}
      <div class="t2 bd" style="margin-top:26px;padding-top:14px;border-top:1px solid #f3f4f6;
                  font-size:11px;color:#cbd5e1;text-align:right">
        Generated by QE Watchdog Agent &bull; {scan_date}
      </div>
    </td></tr>

  </table>

</td></tr>
</table>
</body></html>"""

    subject = f"{subject_prefix} QE Watchdog {scan_date} — {total_violations} vi phạm"
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
        rule_names = {r["id"]: r["name"] for r in eng.cfg.get("rules", []) if "id" in r and "name" in r}
        subject, html, _ = build_email_html(result, tickets, jql, args.date, rule_names=rule_names)
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
