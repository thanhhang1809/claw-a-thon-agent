"""
Module 1B - Mock Data Generator.
Tạo fake tickets cover edge case của từng rule.
Person 2 dùng cái này để dev rules engine, KHÔNG phải chờ Jira xong.

Run:  python -m qe_agent.mock_data        # in ra JSON
"""
from __future__ import annotations
import json
from datetime import date, timedelta
from .models import Ticket

TODAY = date.today()
def d(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def generate_mock_tickets() -> list[Ticket]:
    """Mỗi ticket được thiết kế để trigger 1 edge case cụ thể."""
    return [
        # --- Simple filters (Module 2C) ---
        Ticket("PROJ-1", "Login API needs test start today", "Ready for Test",
               3, test_start_date=TODAY, test_complete_date=d(3),
               sandbox_date=d(5), assignee="alice", qe_pic="qe_bob",
               component="Marketing Solutions"),
        Ticket("PROJ-2", "Checkout must complete test today", "Testing",
               5, test_start_date=d(-2), test_complete_date=TODAY,
               sandbox_date=d(2), assignee="carol", qe_pic="qe_bob",
               component="CRM"),
        Ticket("PROJ-3", "Payment sandbox tomorrow", "Testing",
               5, test_start_date=d(-3), test_complete_date=d(1),
               sandbox_date=d(1), assignee="dave", qe_pic="qe_eve",
               component="Marketing Solutions"),
        Ticket("PROJ-4", "Search blocked ticket", "Blocked",
               2, test_start_date=d(-1), test_complete_date=d(2),
               sandbox_date=d(4), assignee="frank", qe_pic="qe_eve",
               blocked=True, component=None),  # không MS/CRM -> cả hai Dev

        # --- Level 1 Violent ---
        Ticket("PROJ-5", "Start date violation (overdue start)", "Ready for Test",
               3, test_start_date=d(-2), test_complete_date=d(2),
               sandbox_date=d(4), assignee="grace", qe_pic="qe_bob"),
        Ticket("PROJ-6", "Test complete overdue", "Testing",
               8, test_start_date=d(-5), test_complete_date=d(-1),
               sandbox_date=d(3), assignee="heidi", qe_pic="qe_eve"),
        # >3 tickets cùng start date hôm nay (PROJ-1 + 7,8,9 = 4 tickets)
        Ticket("PROJ-7", "Same start date #2", "Ready for Test",
               2, test_start_date=TODAY, test_complete_date=d(3),
               sandbox_date=d(5), assignee="ivan", qe_pic="qe_bob"),
        Ticket("PROJ-8", "Same start date #3", "Ready for Test",
               2, test_start_date=TODAY, test_complete_date=d(3),
               sandbox_date=d(5), assignee="judy", qe_pic="qe_bob"),
        Ticket("PROJ-9", "Same start date #4", "Ready for Test",
               2, test_start_date=TODAY, test_complete_date=d(3),
               sandbox_date=d(5), assignee="ken", qe_pic="qe_eve"),
        Ticket("PROJ-10", "Sandbox date status violation", "In Progress",
               5, test_start_date=d(-1), test_complete_date=d(1),
               sandbox_date=d(-1), assignee="laura", qe_pic="qe_bob"),

        # --- Level 2 Risk (story point + complete date) ---
        Ticket("PROJ-11", "High SP near complete date", "Testing",
               13, test_start_date=d(-1), test_complete_date=d(1),
               sandbox_date=d(3), assignee="mike", qe_pic="qe_eve"),
        Ticket("PROJ-12", "TEP rule end-sprint risk", "Testing",
               8, test_start_date=d(-2), test_complete_date=d(2),
               sandbox_date=d(4), assignee="nina", qe_pic="qe_bob"),

        # --- Level 3 Commit risk (sandbox -1/-2 + story point) ---
        Ticket("PROJ-13", "Sandbox-1 commit risk high SP", "Testing",
               8, test_start_date=d(-2), test_complete_date=d(2),
               sandbox_date=d(1), assignee="oscar", qe_pic="qe_eve"),
        Ticket("PROJ-14", "Sandbox-2 commit risk", "Testing",
               5, test_start_date=d(-1), test_complete_date=d(3),
               sandbox_date=d(2), assignee="peggy", qe_pic="qe_bob"),

        # --- NoQE flag + bug ---
        Ticket("PROJ-15", "NoQE bug ticket", "Testing",
               3, test_start_date=TODAY, test_complete_date=d(2),
               sandbox_date=d(4), assignee="quinn", qe_pic=None,
               no_qe=True, is_bug=True),
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
    }


if __name__ == "__main__":
    print(json.dumps([_serialize(t) for t in generate_mock_tickets()],
                     indent=2, ensure_ascii=False))
