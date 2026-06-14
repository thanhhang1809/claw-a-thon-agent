"""
PLACEHOLDER cho Person 2 (Rules Engine).
Person 1 chỉ cần hàm `run(tickets, today) -> DailyReport`.
Bản stub này điền các SIMPLE FILTERS (Module 2C) để pipeline chạy được ngay,
và để chỗ TODO cho 15 rules của Person 2.
"""
from __future__ import annotations
from datetime import date, timedelta
from .models import Ticket, DailyReport


def run(tickets: list[Ticket], today: date | None = None) -> DailyReport:
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    report = DailyReport(report_date=today)

    # --- Module 2C simple filters (đã implement để demo pipeline) ---
    report.need_start_today = [t for t in tickets if t.test_start_date == today]
    report.need_complete_today = [t for t in tickets if t.test_complete_date == today]
    report.sandbox_tomorrow = [t for t in tickets if t.sandbox_date == tomorrow]
    report.blocked = [t for t in tickets if t.blocked]

    # --- Module 2A/2B: Person 2 ráp rules engine vào đây ---
    # for rule in ALL_RULES:
    #     for t in tickets:
    #         res = rule.evaluate(t, context)
    #         if res: report.{level1|level2|level3}.append(res)

    return report
