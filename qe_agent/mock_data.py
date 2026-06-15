"""
Module 1B - Mock Data Generator (gọn).
Mỗi rule trong rules.yaml có ~2 ticket demo. Status khớp STATUS_RANK của engine.

Run:  python -m qe_agent.mock_data
"""
from __future__ import annotations
import json
from datetime import date
from .models import Ticket
from .rule_engine import workdays_add

TODAY = date.today()
def wd(n: int) -> date:
    return workdays_add(TODAY, n)

MS, CRM = "Marketing Solutions", "CRM"


def generate_mock_tickets() -> list[Ticket]:
    return [
        # ── CHECKLISTS (mỗi loại 1-2) ──
        Ticket("GE-1001", "Start test hôm nay", "Ready for testing", 3,
               TODAY, wd(3), wd(5), "alice", qe_pic="qe_bob", component=MS),
        Ticket("GE-1002", "Complete test hôm nay", "InReview", 3,
               wd(-2), TODAY, wd(3), "carol", qe_pic="qe_bob", component=CRM),
        Ticket("GE-1003", "Sandbox ngày mai", "InReview", 3,
               wd(-3), wd(-1), wd(1), "dave", qe_pic="qe_eve", component=MS),
        Ticket("GE-1004", "Blocked ticket", "Blocked", 2,
               wd(1), wd(3), wd(5), "frank", qe_pic="qe_eve", blocked=True, component=CRM),

        # ── L0: thiếu ngày (2) ──
        Ticket("GE-2001", "Thiếu ngày #1", "In Analysis", 3,
               None, None, None, "grace", qe_pic="qe_bob", component=MS),
        Ticket("GE-2002", "Thiếu ngày #2", "In Analysis", 5,
               wd(1), None, wd(4), "hank", qe_pic="qe_eve", component=CRM),

        # ── L1_TEST_START_OVERDUE (2) ──
        Ticket("GE-3001", "Quá test start #1", "InDev", 3,
               wd(-2), wd(3), wd(6), "heidi", qe_pic="qe_bob", component=CRM),
        Ticket("GE-3002", "Quá test start #2", "In Analysis", 5,
               wd(-1), wd(4), wd(6), "iris", qe_pic="qe_eve", component=MS),

        # ── L1_SANDBOX_DATE_WRONG_STATUS (2) ──
        Ticket("GE-3101", "Sandbox hôm nay sai status #1", "In Analysis", 5,
               TODAY, wd(2), TODAY, "jack", qe_pic="qe_bob", component=CRM),
        Ticket("GE-3102", "Sandbox hôm nay sai status #2", "InDev", 8,
               TODAY, wd(2), TODAY, "kara", qe_pic="qe_eve", component=MS),

        # ── L1_TOO_MANY_SAME_TEST_START: 4 ticket cùng qe_pic ──
        Ticket("GE-3201", "Cùng start #1", "Ready for testing", 2,
               TODAY, wd(3), wd(5), "m1", qe_pic="qe_many", component=MS),
        Ticket("GE-3202", "Cùng start #2", "InTest", 2,
               TODAY, wd(3), wd(5), "m2", qe_pic="qe_many", component=MS),
        Ticket("GE-3203", "Cùng start #3", "InTest", 2,
               TODAY, wd(3), wd(5), "m3", qe_pic="qe_many", component=CRM),
        Ticket("GE-3204", "Cùng start #4", "InTest", 2,
               TODAY, wd(3), wd(5), "m4", qe_pic="qe_many", component=CRM),

        # ── L2_OVERDUE_STILL_INREVIEW (2) ──
        Ticket("GE-4001", "Quá hạn, InReview #1", "InReview", 5,
               wd(-4), wd(-1), wd(2), "mia", qe_pic="qe_eve", component=MS),
        Ticket("GE-4002", "Quá hạn, InReview #2", "InReview", 8,
               wd(-5), wd(-2), wd(2), "noah", qe_pic="qe_bob", component=CRM),

        # ── L2_DUE_TODAY_SP3_NOT_TESTING (2) ──
        Ticket("GE-4101", "Due today SP3 #1", "Walkthrough", 3,
               wd(1), TODAY, wd(2), "olivia", qe_pic="qe_bob", component=CRM),
        Ticket("GE-4102", "Due today SP3 #2", "Ready for testing", 3,
               wd(1), TODAY, wd(2), "peter", qe_pic="qe_eve", component=MS),

        # ── L2_DUE_1DAY_LEFT_SP5 (2) ──
        Ticket("GE-4201", "Còn 1 ngày SP5 #1", "Ready for testing", 5,
               wd(-1), wd(1), wd(3), "quinn", qe_pic="qe_eve", component=MS),
        Ticket("GE-4202", "Còn 1 ngày SP5 #2", "Walkthrough", 5,
               wd(-1), wd(1), wd(3), "rita", qe_pic="qe_bob", component=CRM),

        # ── L2_DUE_2DAYS_LEFT_SP8 (2) ──
        Ticket("GE-4301", "Còn 2 ngày SP8 #1", "InTest", 8,
               wd(-3), wd(2), wd(4), "sam", qe_pic="qe_bob", component=CRM),
        Ticket("GE-4302", "Còn 2 ngày SP8 #2", "Ready for testing", 8,
               wd(-2), wd(2), wd(4), "tina", qe_pic="qe_eve", component=MS),

        # ── L3_DUE_TODAY_SP3_IN_TEST (2) ──
        Ticket("GE-5001", "Due today SP3 InTest #1", "InTest", 3,
               wd(-2), TODAY, wd(2), "uma", qe_pic="qe_eve", component=MS),
        Ticket("GE-5002", "Due today SP3 InTest #2", "InTest", 3,
               wd(-2), TODAY, wd(2), "vito", qe_pic="qe_bob", component=CRM),

        # ── L3_DUE_1DAY_LEFT_SP5_OR_8_IN_TEST (2) ──
        Ticket("GE-5101", "Còn 1 ngày SP8 InTest #1", "InTest", 8,
               wd(-3), wd(1), wd(3), "wendy", qe_pic="qe_bob", component=CRM),
        Ticket("GE-5102", "Còn 1 ngày SP5 InTest #2", "InTest", 5,
               wd(-3), wd(1), wd(3), "xavi", qe_pic="qe_eve", component=MS),

        # ── L3_PRE_SANDBOX_2DAYS_SP8_NOT_INDEV (2) ──
        Ticket("GE-5201", "Còn 2 ngày sandbox SP8 #1", "In Analysis", 8,
               wd(1), wd(3), wd(2), "yara", qe_pic="qe_eve", component=MS),
        Ticket("GE-5202", "Còn 2 ngày sandbox SP8 #2", "New", 8,
               wd(1), wd(3), wd(2), "zed", qe_pic="qe_bob", component=CRM),

        # ── L3_PRE_SANDBOX_1DAY_SP5_NOT_INDEV (2, gửi dev_channel) ──
        Ticket("GE-5301", "Còn 1 ngày sandbox SP5 #1", "New", 5,
               wd(1), wd(3), wd(1), "amy", qe_pic="qe_bob", component=CRM),
        Ticket("GE-5302", "Còn 1 ngày sandbox SP5 #2", "In Analysis", 5,
               wd(1), wd(3), wd(1), "ben", qe_pic="qe_eve", component=MS),

        # NoQE label demo
        Ticket("GE-6001", "NoQE bug", "InDev", 3,
               wd(1), wd(3), wd(5), "uma", qe_pic=None,
               no_qe=True, is_bug=True, component=None),
    ]


def _serialize(t: Ticket) -> dict:
    def iso(x): return x.isoformat() if x else None
    return {
        "id": t.id, "title": t.title, "status": t.status,
        "story_point": t.story_point,
        "test_start_date": iso(t.test_start_date),
        "test_complete_date": iso(t.test_complete_date),
        "sandbox_date": iso(t.sandbox_date),
        "assignee": t.assignee, "qe_pic": t.qe_pic,
        "no_qe": t.no_qe, "blocked": t.blocked, "is_bug": t.is_bug,
        "component": t.component,
    }


if __name__ == "__main__":
    print(json.dumps([_serialize(t) for t in generate_mock_tickets()],
                     indent=2, ensure_ascii=False))
