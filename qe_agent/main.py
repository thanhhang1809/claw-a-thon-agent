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
import os
import sys
import time
from datetime import date

# đảm bảo ROOT trên sys.path khi chạy CLI (python main.py)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: F401 — nạp .env

from engine.rule_engine import run as run_rules

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("qe_agent")

MAX_RETRY = 3
RETRY_WAIT = 30  # seconds


def run_pipeline(use_mock: bool = False, dry_run: bool = False,
                 preview: bool = False,
                 keys: list[str] | None = None, sprint: str | None = None,
                 use_sprint: bool = False, team: str | None = None,
                 snapshot: str | None = None) -> None:
    log.info("Pipeline start (mock=%s, dry_run=%s, preview=%s, snapshot=%s)",
             use_mock, dry_run, preview, snapshot)

    # 1. fetch
    if snapshot:
        from integrations.jira_fetcher import fetch_from_snapshot
        tickets = fetch_from_snapshot(snapshot)  # demo offline, gọn cho 1 email/channel
    elif use_mock:
        from engine.mock_data import generate_mock_tickets
        tickets = generate_mock_tickets()
    elif keys:
        from integrations.jira_fetcher import fetch_by_keys
        tickets = fetch_by_keys(keys)
    elif sprint:
        from integrations.jira_fetcher import fetch_by_sprint
        tickets = fetch_by_sprint(sprint, team=team)  # sprint id/tên cụ thể
    else:
        # MẶC ĐỊNH: chỉ active sprint của project (env JIRA_PROJECT, mặc định GE)
        import os as _os
        from integrations.jira_fetcher import fetch_active_sprint
        project = _os.getenv("JIRA_PROJECT", "GE")
        tickets = fetch_active_sprint(project, team=team)
    log.info("Fetched %d tickets", len(tickets))

    # 2. rules
    report = run_rules(tickets, today=date.today())
    log.info("Report: start=%d complete=%d sandbox=%d blocked=%d "
             "L1=%d L2=%d L3=%d",
             len(report.need_start_today), len(report.need_complete_today),
             len(report.sandbox_tomorrow), len(report.blocked),
             len(report.level1), len(report.level2), len(report.level3))

    # 3a. preview mode — render HTML and open in browser (demo use)
    if preview:
        import tempfile, webbrowser
        from integrations.teams_sender import route, _build_html, CHANNEL_TITLE
        from engine.models import CH_QE, CH_DEV_MS, CH_DEV_CRM
        routed = route(report)
        date_str = report.report_date.isoformat()
        pages = []
        for ch in [CH_QE, CH_DEV_MS, CH_DEV_CRM]:
            groups = routed.get(ch)
            if groups:
                pages.append(_build_html(CHANNEL_TITLE[ch], date_str, groups))
        combined = "\n<hr style='margin:40px 0'>\n".join(pages) if pages else "<p>Không có vi phạm 🎉</p>"
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(combined)
            path = f.name
        log.info("Preview HTML → %s", path)
        webbrowser.open(f"file://{path}")
        return

    # 3b. send (with retry)
    from integrations.teams_sender import send
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
    # Nguồn dữ liệu mặc định cho daily report: snapshot (override qua env
    # DEFAULT_REPORT_SNAPSHOT; để rỗng -> fetch active sprint từ Jira thật).
    default_snapshot = os.getenv("DEFAULT_REPORT_SNAPSHOT", "hangdtt4_snapshot.json") or None
    # 9:00 sáng, thứ Hai đến thứ Sáu
    sched.add_job(run_pipeline, CronTrigger(day_of_week="mon-fri", hour=9, minute=0),
                  kwargs={"snapshot": default_snapshot})
    log.info("Scheduler started — fires 09:00 Mon–Fri (Asia/Ho_Chi_Minh), source=%s. Ctrl+C to stop.",
             default_snapshot or "live active sprint")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="run pipeline once and exit")
    p.add_argument("--mock", action="store_true", help="use mock data instead of Jira")
    p.add_argument("--dry-run", action="store_true", help="print card to screen, don't send")
    p.add_argument("--preview", action="store_true", help="render HTML report and open in browser (demo)")
    p.add_argument("--keys", nargs="+", metavar="KEY_OR_LINK",
                   help="check theo link/key cụ thể, vd: --keys GE-14209 https://.../browse/CRM-99")
    p.add_argument("--sprint", nargs="?", const="", metavar="NAME_OR_ID",
                   help="lấy theo sprint. Không kèm giá trị = sprint đang mở; "
                        "hoặc --sprint 'Sprint 12' / --sprint 123")
    p.add_argument("--list-sprints", metavar="BOARD_ID",
                   help="liệt kê sprint đang active của board (in tên + id rồi thoát)")
    p.add_argument("--team", choices=["MS", "CRM"],
                   help="chỉ lấy ticket thuộc sprint của team MS hoặc CRM")
    p.add_argument("--snapshot", metavar="FILE",
                   help="demo offline từ file snapshot trong snapshots/ "
                        "(vd: --snapshot demo_watchdog.json) — gọn, 1 email/channel")
    args = p.parse_args()

    if args.list_sprints:
        from integrations.jira_fetcher import list_active_sprints
        for s in list_active_sprints(args.list_sprints):
            print(f"  id={s['id']}  {s['name']}  "
                  f"({s.get('startDate','?')} → {s.get('endDate','?')})")
        return

    sprint = None
    use_sprint = False
    if args.sprint is not None:
        use_sprint = True
        sprint = args.sprint or None  # "" -> None -> open sprint

    if args.once or args.keys or use_sprint or args.team or args.preview or args.snapshot:
        run_pipeline(use_mock=args.mock, dry_run=args.dry_run, preview=args.preview,
                     keys=args.keys, sprint=sprint, use_sprint=use_sprint,
                     team=args.team, snapshot=args.snapshot)
    else:
        start_scheduler()


if __name__ == "__main__":
    main()
