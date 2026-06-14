"""
Module 2 — Rules Engine integration.
Load rules.yaml, evaluate mỗi ticket, populate DailyReport.
"""
from __future__ import annotations
import os
from datetime import date, timedelta

from .models import Ticket, DailyReport, RuleResult, Level
from .rule_engine import RuleEngine

_RULES_YAML = os.path.join(os.path.dirname(__file__), "rules.yaml")
_LEVEL_MAP = {0: Level.DATA, 1: Level.VIOLENT, 2: Level.RISK, 3: Level.COMMIT}


def _to_dict(t: Ticket) -> dict:
    """Ticket dataclass → dict mà RuleEngine expect (field 'key' thay vì 'id')."""
    return {
        "key": t.id,
        "title": t.title,
        "status": t.status,
        "story_point": t.story_point,
        "test_start_date": t.test_start_date,
        "test_complete_date": t.test_complete_date,
        "sandbox_date": t.sandbox_date,
        "assignee": t.assignee,
        "qe_pic": t.qe_pic,
        "no_qe": t.no_qe,
        "blocked": t.blocked,
        "is_bug": t.is_bug,
        "component": t.component,
        "sprints": t.sprints,
        "labels": [],
    }


def run(tickets: list[Ticket], today: date | None = None) -> DailyReport:
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    report = DailyReport(report_date=today)

    # --- Simple filters (checklists) ---
    report.need_start_today = [t for t in tickets if t.test_start_date == today]
    report.need_complete_today = [t for t in tickets if t.test_complete_date == today]
    report.sandbox_tomorrow = [t for t in tickets if t.sandbox_date == tomorrow]
    report.blocked = [t for t in tickets if t.blocked]

    # --- Rules engine ---
    engine = RuleEngine(_RULES_YAML)
    ticket_dicts = [_to_dict(t) for t in tickets]
    ticket_by_key = {t.id: t for t in tickets}
    rules_by_id = engine.rules_by_id()

    # skip_weekend_check=True: vẫn chạy rule kể cả cuối tuần (để --mock demo được)
    scan = engine.scan(today, ticket_dicts, skip_weekend_check=True)

    for v in scan["violations"]:
        key = v["ticket"]
        level_int = v["level"]

        # Aggregate violation (không có 1 ticket đơn)
        if key.startswith("AGG:"):
            _handle_aggregate(v, rules_by_id, ticket_by_key, report)
            continue

        ticket = ticket_by_key.get(key)
        if not ticket:
            continue

        rule_def = rules_by_id.get(v["rule"], {})
        channels = rule_def.get("channels", [])
        recipients = rule_def.get("recipients", [])

        result = RuleResult(
            rule_id=v["rule"],
            level=_LEVEL_MAP.get(level_int, Level.RISK),
            ticket=ticket,
            reason=v["msg"],
            send_qe="qe_channel" in channels,
            send_dev="dev_channel" in channels,
            mention_assignee="assignee" in recipients,
        )

        if level_int == 0:
            report.level0.append(result)
        elif level_int == 1:
            report.level1.append(result)
        elif level_int == 2:
            report.level2.append(result)
        elif level_int == 3:
            report.level3.append(result)

    return report


def _handle_aggregate(v: dict, rules_by_id: dict, ticket_by_key: dict,
                      report: DailyReport) -> None:
    """Aggregate violation (vd: L1_TOO_MANY_SAME_TEST_START) — dùng ticket đại diện đầu tiên."""
    rule_def = rules_by_id.get(v["rule"], {})
    agg_keys = v.get("agg_tickets", [])
    rep_ticket = ticket_by_key.get(agg_keys[0]) if agg_keys else None
    if not rep_ticket:
        return

    channels = rule_def.get("channels", [])
    recipients = rule_def.get("recipients", [])
    level_int = v["level"]

    result = RuleResult(
        rule_id=v["rule"],
        level=_LEVEL_MAP.get(level_int, Level.RISK),
        ticket=rep_ticket,
        reason=v["msg"],
        send_qe="qe_channel" in channels,
        send_dev="dev_channel" in channels,
        mention_assignee="assignee" in recipients,
    )

    if level_int == 1:
        report.level1.append(result)
    elif level_int == 2:
        report.level2.append(result)
    elif level_int == 3:
        report.level3.append(result)
