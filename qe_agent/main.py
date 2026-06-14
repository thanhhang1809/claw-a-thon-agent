"""
Module 1D - Scheduler + main().
Pipeline:  fetch -> rules.run -> send
Chạy đúng 9:00 sáng mỗi NGÀY LÀM VIỆC (Mon-Fri).

Run modes:
  python -m qe_agent.main --once --mock     # chạy 1 lần với mock data (test)
  python -m qe_agent.main --once            # chạy 1 lần với Jira thật
  python -m qe_agent.main                    # chạy scheduler (treo, bắn 9h mỗi ngày)
"""
from __future__ import annotations
import argparse
import logging
import sys
import time
from datetime import date

from . import rules
from .mock_data import generate_mock_tickets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("qe_agent")

MAX_RETRY = 3
RETRY_WAIT = 30  # seconds


def run_pipeline(use_mock: bool = False, dry_run: bool = False,
                 keys: list[str] | None = None, sprint: str | None = None,
                 use_sprint: bool = False, team: str | None = None) -> None:
    log.info("Pipeline start (mock=%s, dry_run=%s)", use_mock, dry_run)

    # 1. fetch
    if use_mock:
        tickets = generate_mock_tickets()
    elif keys:
        from .jira_fetcher import fetch_by_keys
        tickets = fetch_by_keys(keys)
    elif sprint:
        from .jira_fetcher import fetch_by_sprint
        tickets = fetch_by_sprint(sprint, team=team)  # sprint id/tên cụ thể
    else:
        # MẶC ĐỊNH: chỉ active sprint của project (env JIRA_PROJECT, mặc định GE)
        import os as _os
        from .jira_fetcher import fetch_active_sprint
        project = _os.getenv("JIRA_PROJECT", "GE")
        tickets = fetch_active_sprint(project, team=team)
    log.info("Fetched %d tickets", len(tickets))

    # 2. rules
    report = rules.run(tickets, today=date.today())
    log.info("Report: start=%d complete=%d sandbox=%d blocked=%d "
             "L1=%d L2=%d L3=%d",
             len(report.need_start_today), len(report.need_complete_today),
             len(report.sandbox_tomorrow), len(report.blocked),
             len(report.level1), len(report.level2), len(report.level3))

    if report.is_empty():
        log.info("Report empty — nothing to send")
        return

    # 3. send (with retry)
    from .teams_sender import send
    for attempt in range(1, MAX_RETRY + 1):
        try:
            send(report, dry_run=dry_run)
            log.info("Sent ✓")
            return
        except Exception as e:
            log.warning("Send failed (attempt %d/%d): %s", attempt, MAX_RETRY, e)
            if attempt < MAX_RETRY:
                time.sleep(RETRY_WAIT)
    log.error("Send failed after %d attempts", MAX_RETRY)


def start_scheduler() -> None:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.error("Need APScheduler: pip install apscheduler")
        sys.exit(1)

    sched = BlockingScheduler(timezone="Asia/Ho_Chi_Minh")
    # 9:00 sáng, thứ Hai đến thứ Sáu
    sched.add_job(run_pipeline, CronTrigger(day_of_week="mon-fri", hour=9, minute=0))
    log.info("Scheduler started — fires 09:00 Mon–Fri (Asia/Ho_Chi_Minh). Ctrl+C to stop.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="run pipeline once and exit")
    p.add_argument("--mock", action="store_true", help="use mock data instead of Jira")
    p.add_argument("--dry-run", action="store_true", help="print card to screen, don't send")
    p.add_argument("--keys", nargs="+", metavar="KEY_OR_LINK",
                   help="check theo link/key cụ thể, vd: --keys GE-14209 https://.../browse/CRM-99")
    p.add_argument("--sprint", nargs="?", const="", metavar="NAME_OR_ID",
                   help="lấy theo sprint. Không kèm giá trị = sprint đang mở; "
                        "hoặc --sprint 'Sprint 12' / --sprint 123")
    p.add_argument("--list-sprints", metavar="BOARD_ID",
                   help="liệt kê sprint đang active của board (in tên + id rồi thoát)")
    p.add_argument("--team", choices=["MS", "CRM"],
                   help="chỉ lấy ticket thuộc sprint của team MS hoặc CRM")
    args = p.parse_args()

    if args.list_sprints:
        from .jira_fetcher import list_active_sprints
        for s in list_active_sprints(args.list_sprints):
            print(f"  id={s['id']}  {s['name']}  "
                  f"({s.get('startDate','?')} → {s.get('endDate','?')})")
        return

    sprint = None
    use_sprint = False
    if args.sprint is not None:
        use_sprint = True
        sprint = args.sprint or None  # "" -> None -> open sprint

    if args.once or args.keys or use_sprint or args.team:
        run_pipeline(use_mock=args.mock, dry_run=args.dry_run,
                     keys=args.keys, sprint=sprint, use_sprint=use_sprint,
                     team=args.team)
    else:
        start_scheduler()


if __name__ == "__main__":
    main()
