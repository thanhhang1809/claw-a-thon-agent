"""
qa_service.py — business logic behind the Watchdog UI.

Wraps the existing engine pieces (rule_engine, jira_adapter, jira_fetcher,
teams_sender, claude_agent) into a handful of clean functions the HTTP layer
can call, and records every run into webstore.task_runs.

Functions:
  list_snapshots()      → available *.json snapshot files
  list_rules()          → rules.yaml parsed for the UI
  run_scan(...)         → run the rule engine, return violations grouped by level
  render_report_html()  → channel-routed HTML preview (no send)
  send_report(...)      → build DailyReport and actually e-mail the channels
  get_insights(...)     → LLM analysis over the historical violations table
  chat(...)             → pass-through to the LLM agent
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from datetime import date
from typing import Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
_RULES = os.path.join(_DIR, "rules.yaml")
_DB = os.path.join(_DIR, "watchdog.db")

# Dual import: package mode (qe_agent.*) and flat mode (Docker /app)
try:  # pragma: no cover
    from .jira_adapter import normalize_issue, fetch_tickets as _adapter_fetch, SCAN_JQL
    from .rule_engine import RuleEngine, run as run_rules
    from .models import Ticket, DailyReport, CH_QE, CH_DEV_MS, CH_DEV_CRM
    from . import teams_sender
    from .claude_agent import run_agent, run_agent_stream, _is_checklist_request
    from . import webstore
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, _DIR)
    from jira_adapter import normalize_issue, fetch_tickets as _adapter_fetch, SCAN_JQL
    from rule_engine import RuleEngine, run as run_rules
    from models import Ticket, DailyReport, CH_QE, CH_DEV_MS, CH_DEV_CRM
    import teams_sender
    from claude_agent import run_agent, run_agent_stream, _is_checklist_request
    import webstore


LEVEL_META = {
    0: {"name": "Data hygiene", "emoji": "⚪", "label": "Level 0"},
    1: {"name": "Violent",      "emoji": "🔴", "label": "Level 1"},
    2: {"name": "Risk",         "emoji": "🟠", "label": "Level 2"},
    3: {"name": "Watch",        "emoji": "🟡", "label": "Level 3"},
}


# ---------------------------------------------------------------- helpers
def _snapshot_path(name: str) -> str:
    return name if os.path.isabs(name) else os.path.join(_DIR, name)


def list_snapshots() -> list[dict]:
    """Every *.json in the agent dir that looks like a Jira snapshot."""
    out = []
    for p in sorted(glob.glob(os.path.join(_DIR, "*.json"))):
        name = os.path.basename(p)
        try:
            data = json.load(open(p, encoding="utf-8"))
            issues = data.get("issues") if isinstance(data, dict) else data
            if isinstance(issues, list) and issues and isinstance(issues[0], dict) \
                    and ("key" in issues[0] or "fields" in issues[0]):
                out.append({"file": name, "count": len(issues)})
        except Exception:
            continue
    return out


def _load_issues(snapshot_file: str) -> list:
    snap = json.load(open(_snapshot_path(snapshot_file), encoding="utf-8"))
    return snap["issues"] if isinstance(snap, dict) else snap


def _ticket_dicts(source: str, snapshot_file: Optional[str], jql: Optional[str]) -> list[dict]:
    if source == "live":
        try:
            return _adapter_fetch(jql or SCAN_JQL)
        except Exception as e:
            raise RuntimeError(
                "Không lấy được dữ liệu Live Jira — cần JIRA_BASE_URL/JIRA_PAT "
                f"hợp lệ trong runtime và mạng tới Jira. Hãy dùng Snapshot. ({type(e).__name__}: {e})")
    if not snapshot_file:
        raise ValueError("snapshot_file is required when source != 'live'")
    return [normalize_issue(i) for i in _load_issues(snapshot_file)]


def _parse_d(s):
    if isinstance(s, date):
        return s
    return date.fromisoformat(s) if s else None


def _dict_to_ticket(d: dict) -> Ticket:
    """normalize_issue() dict → Ticket dataclass (for the send pipeline)."""
    return Ticket(
        id=d.get("key"),
        title=d.get("summary") or "",
        status=d.get("status") or "",
        story_point=d.get("story_point"),
        test_start_date=_parse_d(d.get("test_start_date")),
        test_complete_date=_parse_d(d.get("test_complete_date")),
        sandbox_date=_parse_d(d.get("sandbox_date")),
        assignee=d.get("assignee") or "",
        qe_pic=d.get("qe_pic"),
        no_qe="NoQE" in (d.get("labels") or []),
        component=None,
        sprints=[],
    )


def list_rules() -> list[dict]:
    import yaml
    cfg = yaml.safe_load(open(_RULES, encoding="utf-8"))
    rules = []
    for r in cfg.get("rules", []):
        lvl = r.get("level", 2)
        meta = LEVEL_META.get(lvl, LEVEL_META[2])
        rules.append({
            "id": r["id"],
            "name": r.get("name", r["id"]),
            "level": lvl,
            "level_name": meta["name"],
            "emoji": meta["emoji"],
            "type": r.get("type", "reactive"),
            "channels": r.get("channels", []),
            "recipients": r.get("recipients", []),
            "message": r.get("message", ""),
            "condition": " ".join((r.get("condition", "") or "").split()),
        })
    return rules


# ---------------------------------------------------------------- scan
def run_scan(source: str = "snapshot", snapshot_file: Optional[str] = None,
             jql: Optional[str] = None, scan_date: Optional[str] = None,
             record: bool = True, task_type: str = "scan") -> dict:
    scan_date = scan_date or date.today().isoformat()
    task_id = None
    if record:
        task_id = webstore.create_task(
            task_type, source,
            {"snapshot_file": snapshot_file, "jql": jql, "scan_date": scan_date})
    try:
        tickets = _ticket_dicts(source, snapshot_file, jql)
        engine = RuleEngine(_RULES, db_path=_DB)
        result = engine.scan(scan_date, tickets, skip_weekend_check=True)
        violations = result.get("violations", [])

        by_level: dict[int, list] = {0: [], 1: [], 2: [], 3: []}
        for v in violations:
            by_level.setdefault(v["level"], []).append(v)

        status = "pass" if not violations else ("fail" if by_level[1] else "warn")
        counts = {str(k): len(v) for k, v in by_level.items()}
        summary = (f"{len(tickets)} tickets · {len(violations)} vi phạm "
                   f"(L1={counts['1']} L2={counts['2']} L3={counts['3']} L0={counts['0']})")

        payload = {
            "scan_date": scan_date,
            "source": source,
            "snapshot_file": snapshot_file,
            "ticket_count": len(tickets),
            "violation_count": len(violations),
            "counts": counts,
            "by_level": {str(k): v for k, v in by_level.items()},
            "level_meta": {str(k): m for k, m in LEVEL_META.items()},
            "status": status,
            "summary": summary,
            "task_id": task_id,
        }
        if record:
            webstore.finish_task(task_id, status, summary, payload)
        return payload
    except Exception as e:
        if record:
            webstore.finish_task(task_id, "fail", f"Scan lỗi: {e}", {"error": str(e)})
        raise


# ---------------------------------------------------------------- report build / send
def _build_tickets_for_report(source: str, snapshot_file: Optional[str],
                              jql: Optional[str]) -> list[Ticket]:
    if source == "live":
        from importlib import import_module
        try:
            jf = import_module("qe_agent.jira_fetcher")
        except ImportError:
            jf = import_module("jira_fetcher")
        project = os.getenv("JIRA_PROJECT", "GE")
        try:
            return jf.fetch_active_sprint(project)
        except Exception as e:
            raise RuntimeError(
                "Không lấy được dữ liệu Live Jira — cần JIRA_BASE_URL/JIRA_PAT "
                f"hợp lệ trong runtime và mạng tới Jira. Hãy dùng Snapshot. ({type(e).__name__}: {e})")
    return [_dict_to_ticket(normalize_issue(i)) for i in _load_issues(snapshot_file)]


def _report_html(report: DailyReport) -> str:
    routed = teams_sender.route(report)
    date_str = report.report_date.isoformat()
    pages = []
    for ch in [CH_QE, CH_DEV_MS, CH_DEV_CRM]:
        groups = routed.get(ch)
        if groups:
            pages.append(teams_sender._build_html(
                teams_sender.CHANNEL_TITLE[ch], date_str, groups))
    if not pages:
        return "<p style='font-family:sans-serif;padding:24px'>Không có vi phạm 🎉</p>"
    return "\n<hr style='margin:32px 0'>\n".join(pages)


def render_report_html(source: str = "snapshot", snapshot_file: Optional[str] = None,
                       jql: Optional[str] = None, scan_date: Optional[str] = None) -> str:
    scan_date = scan_date or date.today().isoformat()
    tickets = _build_tickets_for_report(source, snapshot_file, jql)
    report = run_rules(tickets, today=date.fromisoformat(scan_date))
    return _report_html(report)


def send_report(source: str = "snapshot", snapshot_file: Optional[str] = None,
                jql: Optional[str] = None, scan_date: Optional[str] = None,
                dry_run: bool = False, record: bool = True,
                task_type: str = "send") -> dict:
    scan_date = scan_date or date.today().isoformat()
    task_id = None
    if record:
        task_id = webstore.create_task(
            task_type, source,
            {"snapshot_file": snapshot_file, "jql": jql, "scan_date": scan_date,
             "dry_run": dry_run})
    try:
        tickets = _build_tickets_for_report(source, snapshot_file, jql)
        report = run_rules(tickets, today=date.fromisoformat(scan_date))
        routed = teams_sender.route(report)
        channels = [teams_sender.CHANNEL_TITLE.get(c, c) for c, g in routed.items() if g]
        html = _report_html(report)

        creds_ok = bool(os.getenv("GMAIL_USER") and os.getenv("GMAIL_APP_PASSWORD"))

        if report.is_empty():
            status, summary = "pass", "Không có vi phạm — không gửi gì 🎉"
        elif dry_run:
            status = "warn"
            summary = f"Dry-run — preview {len(channels)} channel, không gửi thật"
        elif not creds_ok:
            status = "warn"
            summary = ("GMAIL_USER / GMAIL_APP_PASSWORD chưa cấu hình — "
                       "không gửi được. Đã render preview.")
        else:
            teams_sender.send(report, dry_run=False)
            status = "pass"
            summary = f"Đã gửi report tới {len(channels)} channel: {', '.join(channels)}"

        payload = {
            "scan_date": scan_date, "source": source, "snapshot_file": snapshot_file,
            "channels": channels, "dry_run": dry_run, "creds_ok": creds_ok,
            "html": html, "status": status, "summary": summary, "task_id": task_id,
        }
        if record:
            webstore.finish_task(task_id, status, summary,
                                 {k: v for k, v in payload.items() if k != "html"})
        return payload
    except Exception as e:
        if record:
            webstore.finish_task(task_id, "fail", f"Gửi report lỗi: {e}", {"error": str(e)})
        raise


# ---------------------------------------------------------------- insights
def history_summary() -> dict:
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row

    def rows(q, *a):
        return [dict(r) for r in c.execute(q, a).fetchall()]

    total = c.execute("SELECT COUNT(*) FROM violations").fetchone()[0]
    by_level = rows("SELECT level, COUNT(*) n FROM violations GROUP BY level ORDER BY level")
    by_rule = rows("SELECT rule_id, level, COUNT(*) n FROM violations "
                   "GROUP BY rule_id ORDER BY n DESC")
    by_qe = rows("SELECT COALESCE(qe_pic,'(none)') qe_pic, COUNT(*) n FROM violations "
                 "GROUP BY qe_pic ORDER BY n DESC LIMIT 15")
    by_assignee = rows("SELECT COALESCE(assignee,'(none)') assignee, COUNT(*) n "
                       "FROM violations GROUP BY assignee ORDER BY n DESC LIMIT 15")
    by_date = rows("SELECT fired_date, COUNT(*) n FROM violations "
                   "GROUP BY fired_date ORDER BY fired_date")
    repeat = rows("SELECT ticket_key, COUNT(DISTINCT fired_date) days, COUNT(*) n "
                  "FROM violations GROUP BY ticket_key HAVING days > 1 "
                  "ORDER BY days DESC, n DESC LIMIT 20")
    scans = c.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
    c.close()
    return {
        "total_violations": total,
        "total_scans": scans,
        "by_level": by_level,
        "by_rule": by_rule,
        "by_qe_pic": by_qe,
        "by_assignee": by_assignee,
        "by_date": by_date,
        "repeat_offenders": repeat,
    }


def get_insights(question: Optional[str] = None, record: bool = True) -> dict:
    task_id = None
    if record:
        task_id = webstore.create_task("insight", "db", {"question": question})
    try:
        data = history_summary()
        ctx = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        q = question or ("Phân tích dữ liệu lịch sử vi phạm QE bên dưới. Nêu rõ: "
                         "(1) xu hướng theo thời gian, (2) rủi ro nổi bật & repeat offender, "
                         "(3) rule/QE PIC nào đáng chú ý, (4) đề xuất cải thiện quy trình QE cụ thể.")
        msg = (f"{q}\n\nDưới đây là dữ liệu thống kê vi phạm QE lịch sử (JSON). "
               f"Hãy phân tích trực tiếp dữ liệu này, KHÔNG cần gọi tool:\n\n{ctx}")
        text = run_agent(msg, verbose=False)
        status = "pass" if text else "warn"
        summary = "Đã sinh insight từ lịch sử vi phạm" if text else "Không có dữ liệu / không sinh được insight"
        if record:
            webstore.finish_task(task_id, status, summary, {"insights": text, "data": data})
        return {"insights": text, "data": data, "task_id": task_id, "status": status}
    except Exception as e:
        if record:
            webstore.finish_task(task_id, "fail", f"Insight lỗi: {e}", {"error": str(e)})
        raise


# ---------------------------------------------------------------- demo seeding
def seed_demo_data() -> bool:
    """On a fresh (empty) DB, populate History/Insights/Schedule with a
    realistic spread so a restart/redeploy never leaves the UI blank.
    Returns True if it seeded, False if data already existed or no snapshot.
    Only fast, deterministic, non-LLM actions (scans + a dry-run send +
    one schedule) — chat/insight tasks appear when demoed live."""
    if webstore.list_tasks(limit=1):
        return False
    snap = "ge_sprint_snapshot.json"
    if not os.path.exists(_snapshot_path(snap)):
        return False
    # scans across several days → trend + repeat offenders for Insights
    for d in ["2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12", "2026-06-15"]:
        try:
            run_scan(source="snapshot", snapshot_file=snap, scan_date=d)
        except Exception:
            pass
    # an early date → only L0 hygiene issues → a "warn" row for variety
    try:
        run_scan(source="snapshot", snapshot_file=snap, scan_date="2026-05-20")
    except Exception:
        pass
    # a dry-run send → a "send" task in history
    try:
        send_report(source="snapshot", snapshot_file=snap,
                    scan_date="2026-06-15", dry_run=True)
    except Exception:
        pass
    # a sample daily schedule
    try:
        webstore.add_schedule("Daily QE scan 9h", 9, 0, "mon-fri",
                              "scan", "snapshot", snap, None)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- chat
_STATUS_KEYWORDS = [
    "vi phạm", "violation", "level", "trạng thái", "status", "scan", "sprint",
    "quá hạn", "overdue", "ticket nào", "report", "tổng quan", "tình trạng", "vi pham",
]
# Insights = phân tích dữ liệu LỊCH SỬ (tích hợp tab Insights vào Chat)
_INSIGHT_KEYWORDS = [
    "insight", "xu hướng", "xu huong", "trend", "rủi ro", "rui ro", "cải thiện",
    "cai thien", "repeat", "tái phạm", "tai pham", "thống kê", "thong ke",
    "lịch sử vi phạm", "phân tích lịch sử", "offender",
]


def _augment_for_speed(message: str, source: str, snapshot_file: Optional[str]):
    """Part B — giảm round-trip + tích hợp Insights vào Chat.
    Nhúng sẵn dữ liệu vào prompt để model trả lời trong 1 lượt (không gọi tool).
    Trả (augmented_message, used_precompute: bool)."""
    low = message.lower()
    if _is_checklist_request(message):
        return message, False               # checklist cần get_ticket_detail riêng

    # 1) Insights — phân tích lịch sử vi phạm (độc lập source, đọc DB)
    if any(k in low for k in _INSIGHT_KEYWORDS):
        try:
            data = history_summary()
        except Exception:
            return message, False
        aug = (message + "\n\n[DỮ LIỆU THỐNG KÊ VI PHẠM QE LỊCH SỬ — phân tích trực "
               "tiếp, KHÔNG gọi tool. Hãy nêu: (1) xu hướng theo thời gian, "
               "(2) rủi ro nổi bật & repeat offender, (3) rule/QE PIC đáng chú ý, "
               "(4) đề xuất cải thiện quy trình QE]:\n"
               + json.dumps(data, ensure_ascii=False, default=str))
        return aug, True

    # 2) Status/violation — chạy sẵn rule engine trên snapshot
    if source != "snapshot" or not snapshot_file:
        return message, False
    if not any(k in low for k in _STATUS_KEYWORDS):
        return message, False
    try:
        res = run_scan(source="snapshot", snapshot_file=snapshot_file, record=False)
    except Exception:
        return message, False
    ctx = {
        "scan_date": res["scan_date"],
        "ticket_count": res["ticket_count"],
        "counts": res["counts"],
        "violations": res["by_level"],
    }
    aug = (message + "\n\n[DỮ LIỆU ĐÃ CÓ SẴN — KHÔNG cần gọi search_jira hay "
           "run_rule_engine, hãy trả lời trực tiếp từ dữ liệu vi phạm QE dưới đây]:\n"
           + json.dumps(ctx, ensure_ascii=False, default=str))
    return aug, True


def chat_stream(message: str, source: str = "snapshot",
                snapshot_file: Optional[str] = None):
    """Streaming chat — yields events {type: status|clear|delta|done} và ghi task
    history khi kết thúc."""
    task_id = webstore.create_task(
        "chat", source, {"message": message[:200], "snapshot_file": snapshot_file})
    msg = message
    if source != "live" and snapshot_file and "snapshot_file" not in msg:
        abspath = _snapshot_path(snapshot_file)
        msg = msg + f"\n\n(Dùng snapshot_file='{abspath}' khi gọi search_jira / get_ticket_detail)"
    msg, _fast = _augment_for_speed(msg, source, snapshot_file)

    full = ""
    try:
        for ev in run_agent_stream(msg, verbose=False):
            if ev["type"] == "delta":
                full += ev["text"]
            elif ev["type"] == "clear":
                full = ""
            elif ev["type"] == "done":
                full = ev["text"] or full
            yield ev
        status = "pass" if full.strip() else "warn"
        webstore.finish_task(task_id, status, (full or "")[:160], {"reply": full})
    except Exception as e:
        webstore.finish_task(task_id, "fail", f"Chat lỗi: {e}", {"error": str(e)})
        yield {"type": "done", "text": f"Lỗi: {e}"}


def chat(message: str, source: str = "snapshot", snapshot_file: Optional[str] = None,
         record: bool = True) -> dict:
    task_id = None
    if record:
        task_id = webstore.create_task("chat", source,
                                       {"message": message[:200], "snapshot_file": snapshot_file})
    try:
        msg = message
        if source != "live" and snapshot_file and "snapshot_file" not in msg:
            abspath = _snapshot_path(snapshot_file)
            msg = msg + f"\n\n(Dùng snapshot_file='{abspath}' khi gọi search_jira / get_ticket_detail)"
        text = run_agent(msg, verbose=False)
        status = "pass" if text else "warn"
        if record:
            webstore.finish_task(task_id, status, (text or "")[:160], {"reply": text})
        return {"reply": text, "task_id": task_id, "status": status}
    except Exception as e:
        if record:
            webstore.finish_task(task_id, "fail", f"Chat lỗi: {e}", {"error": str(e)})
        raise
