"""
Module 1C - Teams Sender (gửi report QE qua email tới kênh Teams).
Mỗi channel = 1 địa chỉ email "post to channel" của Teams. Agent gửi email
multipart (text/plain + text/html) qua Gmail SMTP; Teams nhận và post vào kênh.
(Bản Power Automate Flow giữ ở _post_flow/CHANNEL_FLOW_ENV cho tương lai nếu
xin được quyền webhook.)
"""
from __future__ import annotations
import os
import json
import smtplib
import requests
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import paths  # noqa: F401 — nạp .env
from engine.models import (
    DailyReport, Ticket, RuleResult,
    CH_QE, CH_DEV_MS, CH_DEV_CRM, dev_channels_for,
)

LEVEL_NAME = {-1: "📋 CHECKLIST", 0: "⚪ DATA", 1: "🔴 LEVEL 1 — Violent", 2: "🟠 LEVEL 2 — Risk", 3: "🟡 LEVEL 3 — Commit Risk"}
LEVEL_COLOR = {-1: "#1565c0", 0: "#757575", 1: "#d32f2f", 2: "#e65100", 3: "#f9a825"}
LEVEL_DOT = {-1: "🔹", 0: "⚪", 1: "🔴", 2: "🟠", 3: "🟡"}
# Thứ tự hiển thị theo mức nghiêm trọng (L1 trước → checklist cuối) để phần
# preview (Teams cắt ngắn) hiện cái quan trọng nhất trước.
_LEVEL_SORT = {1: 0, 2: 1, 3: 2, 0: 3, -1: 4}
def _ordered_levels(by_level):
    return sorted(by_level, key=lambda l: _LEVEL_SORT.get(l, 9))
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
CHANNEL_FLOW_ENV = {   # (dự phòng) Power Automate Flow webhook — chưa dùng
    CH_QE:      "TEAMS_FLOW_QE",
    CH_DEV_MS:  "TEAMS_FLOW_DEV_MS",
    CH_DEV_CRM: "TEAMS_FLOW_DEV_CRM",
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
# Routing: DailyReport → dict[channel → list[group]]
# Mỗi group = {"level": int, "rule_id": str, "label": str, "rows": [Row,...]}
# Row = {"ticket": Ticket, "reasons": [str,...]}
# Nhóm: theo level (DATA/L1/L2/L3) rồi theo rule_id — giống ảnh mẫu.
# Simple filters (start/complete/sandbox/blocked) là các "rule_id" ảo.
# ---------------------------------------------------------------------------
# pseudo rule_id cho simple filters
SIMPLE_RULES = {
    "CHECK_TEST_START":    ("📋 Test Start Today", -1),
    "CHECK_TEST_COMPLETE": ("✅ Test Complete Today", -1),
    "CHECK_SANDBOX":       ("📦 Sandbox Tomorrow", -1),
    "CHECK_BLOCKED":       ("⛔ Blocked", -1),
}


def route(report: DailyReport) -> dict[str, list]:
    # buckets[channel][(level, rule_id)] = {label, rows: {tid: (ticket, [reasons])}}
    buckets: dict[str, dict] = defaultdict(lambda: defaultdict(
        lambda: {"label": "", "level": 0, "rows": {}}))

    def add(channels, level, rule_id, label, ticket, reason=None):
        for ch in channels:
            g = buckets[ch][(level, rule_id)]
            g["label"] = label
            g["level"] = level
            tid = ticket.id
            if tid not in g["rows"]:
                g["rows"][tid] = (ticket, [])
            if reason:
                g["rows"][tid][1].append(reason)

    # --- simple filters (checklist thông tin, level -1) ---
    for t in report.need_start_today:
        add([CH_QE] + dev_channels_for(t.component), -1, "CHECK_TEST_START",
            "📋 Test Start Today", t)
    for t in report.need_complete_today:
        add([CH_QE] + dev_channels_for(t.component), -1, "CHECK_TEST_COMPLETE",
            "✅ Test Complete Today", t)
    for t in report.sandbox_tomorrow:
        add(dev_channels_for(t.component), -1, "CHECK_SANDBOX",
            "📦 Sandbox Tomorrow", t)
    for t in report.blocked:
        add(dev_channels_for(t.component), -1, "CHECK_BLOCKED",
            "⛔ Blocked", t)

    # --- rule results theo level ---
    for results, lvl in [(report.level0, 0), (report.level1, 1),
                         (report.level2, 2), (report.level3, 3)]:
        for r in results:
            # L0 (DATA): luôn gửi QE; L1-3 theo cờ Person 2 set
            if lvl == 0:
                chans = [CH_QE] + dev_channels_for(r.ticket.component)
            else:
                chans = ([CH_QE] if r.send_qe else []) + \
                        (dev_channels_for(r.ticket.component) if r.send_dev else [])
            add(chans, lvl, r.rule_id, r.rule_name or r.rule_id, r.ticket, r.reason)

    # Flatten -> sort theo level, rồi rule_id
    out: dict[str, list] = {}
    for ch, groups in buckets.items():
        glist = []
        for (level, rule_id), g in groups.items():
            rows = [{"ticket": t, "reasons": reasons}
                    for t, reasons in g["rows"].values()]
            glist.append({"level": level, "rule_id": rule_id,
                          "label": g["label"], "rows": rows})
        glist.sort(key=lambda x: (x["level"], x["rule_id"]))
        out[ch] = glist
    return out


# ---------------------------------------------------------------------------
# HTML builder — bảng nhóm theo LEVEL -> rule_id, cột có Status
# ---------------------------------------------------------------------------
def _ticket_link(t: Ticket) -> str:
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    if base:
        return (f'<a href="{base}/browse/{t.id}" style="color:#1565c0;'
                f'font-family:monospace;font-weight:700;text-decoration:none">{t.id}</a>')
    return f'<span style="font-family:monospace;font-weight:700">{t.id}</span>'


def _rule_table(group: dict) -> str:
    """Một rule = 1 bảng: Ticket | Status | Assignee | QE PIC."""
    th = ('style="text-align:left;padding:6px 10px;font-size:11px;color:#666;'
          'background:#f0f0f0;border-bottom:1px solid #ddd;white-space:nowrap"')
    td = 'style="padding:6px 10px;font-size:13px;border-bottom:1px solid #eee;vertical-align:top"'

    rows_html = ""
    for row in group["rows"]:
        t = row["ticket"]
        rows_html += (
            f'<tr>'
            f'<td {td}>{_ticket_link(t)}'
            f'<div style="font-size:11px;color:#777;margin-top:2px;max-width:260px">{t.title or ""}</div></td>'
            f'<td {td}><span style="font-size:12px;color:#555">{t.status or "—"}</span></td>'
            f'<td {td}>{t.assignee or "—"}{" <b style=color:#d32f2f>[NoQE]</b>" if t.no_qe else ""}</td>'
            f'<td {td}>{t.qe_pic or "—"}</td>'
            f'</tr>'
        )

    n = len(group["rows"])
    heading = group["label"]  # checklist: nhãn; rule: rule_name (fallback rule_id)
    return (
        f'<div style="font-size:12px;color:#444;font-weight:600;margin:10px 0 4px">'
        f'{heading} <span style="color:#999;font-weight:400">({n} ticket)</span></div>'
        f'<table style="border-collapse:collapse;width:100%;background:#fff;'
        f'border:1px solid #e0e0e0;border-radius:4px;overflow:hidden">'
        f'<tr><th {th}>Ticket</th><th {th}>Status</th><th {th}>Assignee</th>'
        f'<th {th}>QE PIC</th></tr>'
        f'{rows_html}</table>'
    )


def _level_blocks(groups: list[dict]) -> str:
    """Render các khối theo level → rule (trả về chuỗi HTML)."""
    by_level: dict[int, list] = defaultdict(list)
    for g in groups:
        by_level[g["level"]].append(g)
    out = ""
    for level in _ordered_levels(by_level):
        color = LEVEL_COLOR.get(level, "#757575")
        total = sum(len(g["rows"]) for g in by_level[level])
        suffix = "ticket" if level == -1 else "vi phạm"
        out += (
            f'<div style="background:{color};color:#fff;padding:9px 14px;'
            f'border-radius:6px;font-weight:700;font-size:13px;margin:18px 0 8px">'
            f'{LEVEL_NAME.get(level, "?")} &nbsp;'
            f'<span style="opacity:.85;font-weight:400">— {total} {suffix}</span></div>'
        )
        for g in by_level[level]:
            out += _rule_table(g)
    return out


def _filter_groups_by_team(groups: list[dict], team_component: str) -> list[dict]:
    """Giữ lại trong mỗi group những row thuộc team (component khớp hoặc None=cả hai)."""
    result = []
    for g in groups:
        rows = [r for r in g["rows"]
                if r["ticket"].component == team_component or r["ticket"].component is None]
        if rows:
            result.append({**g, "rows": rows})
    return result


def _team_split_blocks(groups: list[dict]) -> str:
    """Chia QE Daily thành 2 nhóm MS và CRM, mỗi nhóm là các khối level→rule."""
    from engine.models import COMPONENT_MS, COMPONENT_CRM
    out = ""
    for team_name, comp, color in [("🟢 MS — Marketing Solutions", COMPONENT_MS, "#2e7d32"),
                                    ("🟣 CRM", COMPONENT_CRM, "#6a1b9a")]:
        tgroups = _filter_groups_by_team(groups, comp)
        total = sum(len(g["rows"]) for g in tgroups)
        out += (
            f'<div style="background:{color};color:#fff;padding:11px 16px;'
            f'border-radius:8px;font-weight:800;font-size:15px;margin:24px 0 6px">'
            f'{team_name} <span style="opacity:.85;font-weight:400">— {total} ticket</span></div>'
        )
        out += _level_blocks(tgroups) if tgroups else \
            '<div style="padding:8px 14px;color:#999;font-style:italic">(không có)</div>'
    return out


def _build_html(channel_title: str, date_str: str, groups: list[dict],
                team_split: bool = False, page: int = None, total_pages: int = None) -> str:
    part_badge = ""
    if total_pages and total_pages > 1:
        part_badge = (f'<span style="background:rgba(255,255,255,.25);border-radius:10px;'
                      f'padding:2px 10px;font-size:12px;font-weight:600;margin-left:8px">'
                      f'Phần {page}/{total_pages}</span>')
    body = _team_split_blocks(groups) if team_split else _level_blocks(groups)

    return f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
             background:#f5f5f5;margin:0;padding:20px">
<div style="max-width:760px;margin:0 auto">
  <div style="background:#1565c0;border-radius:10px 10px 0 0;padding:20px 24px;color:#fff">
    <div style="font-size:18px;font-weight:700">🛡️ QE Watchdog — {channel_title}{part_badge}</div>
    <div style="font-size:13px;opacity:.85;margin-top:4px">📅 {date_str}</div>
  </div>
  <div style="background:#fff;border-radius:0 0 10px 10px;padding:18px 24px;
              border:1px solid #e0e0e0;border-top:none">
    {body}
    <div style="margin-top:20px;padding-top:12px;border-top:1px solid #f0f0f0;
                font-size:11px;color:#aaa">Generated by QE Watchdog Agent • {date_str}</div>
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
        out.append("=" * 60)
        out.append(f"### CHANNEL: {CHANNEL_TITLE[ch]}")
        out.append("=" * 60)
        groups = routed.get(ch)
        if not groups:
            out.append("(no messages)\n")
            continue
        cur_level = None
        for g in groups:
            if g["level"] != cur_level:
                cur_level = g["level"]
                total = sum(len(x["rows"]) for x in groups if x["level"] == cur_level)
                suffix = "ticket" if cur_level == -1 else "vi phạm"
                out.append(f"\n{LEVEL_NAME.get(cur_level,'?')} — {total} {suffix}")
            out.append(f"  [{g['label']}]  ({len(g['rows'])} ticket)")
            for row in g["rows"]:
                t = row["ticket"]
                out.append(f"    {t.id:12} {(t.title or '')[:45]}")
                out.append(f"    {'':12} status={t.status or '—':18} "
                           f"assignee={t.assignee or '—':10} QE={t.qe_pic or '—'}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# pagination — tách 1 channel thành nhiều email khi data quá dài
# (Gmail clip email > ~102KB; Teams "post via email" cũng giới hạn).
# Mỗi trang gói tối đa MAX_ROWS dòng; group lớn hơn sẽ bị chia nhỏ.
# ---------------------------------------------------------------------------
def _max_rows_per_email() -> int:
    try:
        return max(5, int(os.getenv("REPORT_MAX_ROWS_PER_EMAIL", "60")))
    except ValueError:
        return 60


def _max_bytes_per_email() -> int:
    # ngưỡng kích thước HTML/email mỗi part (mặc định ~45KB — an toàn dưới mức
    # Gmail clip ~102KB; còn chừa chỗ cho wrapper + header)
    try:
        return max(10000, int(os.getenv("REPORT_MAX_BYTES_PER_EMAIL", "45000")))
    except ValueError:
        return 45000


def _group_bytes(group: dict) -> int:
    """Ước lượng kích thước HTML render của 1 group (để chia theo dung lượng)."""
    return len(_rule_table(group).encode("utf-8"))


def _split_group(group: dict, max_rows: int, max_bytes: int) -> list[dict]:
    """Chia 1 group quá lớn (theo dòng HOẶC theo bytes) thành nhiều group con."""
    rows = group["rows"]
    if len(rows) <= max_rows and _group_bytes(group) <= max_bytes:
        return [group]
    # ước lượng bytes/row để giới hạn cả theo dung lượng
    per_row = max(1, _group_bytes(group) // max(1, len(rows)))
    chunk = max(1, min(max_rows, max_bytes // per_row))
    return [{**group, "rows": rows[i:i + chunk]} for i in range(0, len(rows), chunk)]


def _paginate(groups: list[dict], max_rows: int, max_bytes: int = None) -> list[list[dict]]:
    """Pack groups vào các trang — mỗi trang ≤ max_rows dòng VÀ ≤ max_bytes.
    'Data quá lớn' (nhiều dòng hoặc nặng) sẽ tự tách thành nhiều part/email."""
    if max_bytes is None:
        max_bytes = _max_bytes_per_email()
    pages: list[list[dict]] = []
    cur: list[dict] = []
    cur_rows = cur_bytes = 0
    for g in groups:
        for sub in _split_group(g, max_rows, max_bytes):
            n = len(sub["rows"]); b = _group_bytes(sub)
            if cur and (cur_rows + n > max_rows or cur_bytes + b > max_bytes):
                pages.append(cur)
                cur, cur_rows, cur_bytes = [], 0, 0
            cur.append(sub)
            cur_rows += n
            cur_bytes += b
    if cur:
        pages.append(cur)
    return pages or [[]]


# ---------------------------------------------------------------------------
# Plain-text bản song song HTML — Teams "post via email" sanitize HTML bảng
# rất mạnh, cần text/plain để data luôn hiển thị.
# ---------------------------------------------------------------------------
def _build_text(channel_title: str, date_str: str, groups: list[dict]) -> str:
    by_level: dict[int, list] = defaultdict(list)
    for g in groups:
        by_level[g["level"]].append(g)
    # dòng tóm tắt lên đầu — để preview (Teams cắt ngắn) vẫn thấy con số chính
    cnt = lambda lv: sum(len(g["rows"]) for g in by_level.get(lv, []))
    summary = f"🔴 {cnt(1)} · 🟠 {cnt(2)} · 🟡 {cnt(3)} · ⚪ {cnt(0)} vi phạm"
    lines = [f"QE WATCHDOG — {channel_title}  ({date_str})",
             f"TÓM TẮT: {summary}  — xem 'Email gốc' để xem đầy đủ", ""]
    for level in _ordered_levels(by_level):
        total = sum(len(g["rows"]) for g in by_level[level])
        suffix = "ticket" if level == -1 else "vi phạm"
        lines.append(f"== {LEVEL_NAME.get(level, '?')} — {total} {suffix} ==")
        for g in by_level[level]:
            heading = g["label"] if g["level"] == -1 else g["rule_id"]
            lines.append(f"[{heading}] ({len(g['rows'])} ticket)")
            for row in g["rows"]:
                t = row["ticket"]
                lines.append(f"  - {t.id} | {t.status or '—'} | "
                             f"assignee: {t.assignee or '—'} | QE: {t.qe_pic or '—'}")
                for r in row["reasons"]:
                    lines.append(f"      • {r}")
        lines.append("")
    return "\n".join(lines)


# (dự phòng) gửi qua Power Automate Flow webhook — dùng khi xin được quyền
def _post_flow(flow_url: str, payload: dict) -> None:
    resp = requests.post(flow_url, json=payload, timeout=30)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# QE Daily — khi report quá dài thì tách ticket theo component (MS / CRM)
# thành các email riêng (mỗi component còn quá dài sẽ tiếp tục phân trang).
# ---------------------------------------------------------------------------
def _too_long(groups: list[dict], max_rows: int, max_bytes: int = None) -> bool:
    """report có vượt ngưỡng 1 email (theo dòng HOẶC dung lượng) không."""
    if max_bytes is None:
        max_bytes = _max_bytes_per_email()
    total_rows = sum(len(g["rows"]) for g in groups)
    total_bytes = sum(_group_bytes(g) for g in groups)
    return total_rows > max_rows or total_bytes > max_bytes


def _component_partitions(groups: list[dict]) -> list[tuple[str, list[dict]]]:
    """Chia QE Daily thành email riêng theo team: MS và CRM.
    Dùng cùng filter với _team_split_blocks (component khớp HOẶC None=cả hai),
    nên ticket dùng chung (component None) xuất hiện ở cả 2 email — đúng ngữ
    nghĩa 'liên quan cả hai team'. Trả về [(nhãn, subgroups), ...] phần có data."""
    from engine.models import COMPONENT_MS, COMPONENT_CRM
    out = []
    for label, comp in [("MS", COMPONENT_MS), ("CRM", COMPONENT_CRM)]:
        sub = _filter_groups_by_team(groups, comp)
        if sub:
            out.append((label, sub))
    return out


def _send_channel_pages(to_email: str, channel_title: str, date_str: str,
                        groups: list[dict], team_split: bool,
                        gmail_user: str, gmail_pass: str, max_rows: int) -> int:
    """Phân trang 1 (sub)report rồi gửi từng part. Trả về số email đã gửi."""
    pages = _paginate(groups, max_rows)
    total = len(pages)
    for idx, page_groups in enumerate(pages, 1):
        levels = {g["level"] for g in page_groups}
        prefix = "🔴" if 1 in levels else ("🟠" if 2 in levels else "📋")
        part = f" (Phần {idx}/{total})" if total > 1 else ""
        subject = f"{prefix} QE Watchdog {date_str} — {channel_title}{part}"
        html = _build_html(channel_title, date_str, page_groups,
                           team_split=team_split,
                           page=idx if total > 1 else None,
                           total_pages=total if total > 1 else None)
        text = _build_text(channel_title, date_str, page_groups)
        _send_one(to_email, subject, text, html, gmail_user, gmail_pass)
        print(f"[sent] {subject} → {to_email}")
    return total


# ---------------------------------------------------------------------------
# send — gửi report qua EMAIL tới địa chỉ "post to channel" của Teams.
# multipart/alternative: text/plain (luôn hiển thị) + text/html (đẹp).
# ---------------------------------------------------------------------------
def _send_one(to: str, subject: str, text: str, html: str,
              from_addr: str, password: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
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

    max_rows = _max_rows_per_email()

    for ch, groups in routed.items():
        if not groups:
            continue
        to_email = os.getenv(CHANNEL_EMAIL_ENV[ch])
        if not to_email:
            print(f"[skip] {CHANNEL_EMAIL_ENV[ch]} chưa set — bỏ qua {ch}")
            continue

        # QE Daily: nếu report quá dài cho 1 email → tách ticket theo component
        # (MS / CRM) thành các email RIÊNG; mỗi component còn dài sẽ tự phân trang.
        if ch == CH_QE and _too_long(groups, max_rows):
            for label, sub_groups in _component_partitions(groups):
                _send_channel_pages(to_email, f"{CHANNEL_TITLE[ch]} · {label}",
                                    date_str, sub_groups, team_split=False,
                                    gmail_user=gmail_user, gmail_pass=gmail_pass,
                                    max_rows=max_rows)
            continue

        # Vừa 1 email (hoặc kênh Dev): gửi như cũ (QE Daily vẫn hiện MS+CRM gộp)
        _send_channel_pages(to_email, CHANNEL_TITLE[ch], date_str, groups,
                            team_split=(ch == CH_QE),
                            gmail_user=gmail_user, gmail_pass=gmail_pass,
                            max_rows=max_rows)
