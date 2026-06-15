# -*- coding: utf-8 -*-
"""
QA Watchdog Agent — orchestrator nối toàn pipeline.

    JQL  ->  fetch Jira  ->  normalize (adapter)  ->  rule engine  ->  report

Hai chế độ fetch:
  --live           gọi Jira REST thật (cần env JIRA_BASE_URL, JIRA_PAT)
  --snapshot FILE  đọc kết quả JQL đã lưu sẵn (JSON) — dùng demo/test offline

Ví dụ:
  python agent.py --snapshot jql_snapshot.json --date 2026-06-12 --html report.html
  python agent.py --live --jql 'project = PC AND status not in (DONE,Cancelled)' --date 2026-06-12
"""
import argparse, json, sys, html
from datetime import date
from collections import defaultdict

from jira_adapter import normalize_issue, fetch_tickets, SCAN_JQL
from engine import RuleEngine

LEVEL_META = {
    0: ("DATA",  "⚪", "Data hygiene — thiếu field, rule khác bị bỏ qua"),
    1: ("L1",    "🔴", "Violent — vi phạm rõ ràng, cần xử lý ngay"),
    2: ("L2",    "🟠", "Risk — nguy cơ trễ commit theo plan"),
    3: ("L3",    "🟡", "Watch — theo dõi sát"),
}


# ---------------------------------------------------------------- fetch
def get_tickets(args):
    if args.live:
        jql = args.jql or SCAN_JQL
        print(f"[fetch] LIVE qua Jira REST — JQL: {jql}", file=sys.stderr)
        return fetch_tickets(jql), jql
    snap = json.load(open(args.snapshot, encoding="utf-8"))
    issues = snap["issues"] if isinstance(snap, dict) else snap
    jql = snap.get("jql", "(snapshot)") if isinstance(snap, dict) else "(snapshot)"
    print(f"[fetch] SNAPSHOT {args.snapshot} — {len(issues)} issue", file=sys.stderr)
    return [normalize_issue(i) for i in issues], jql


# ---------------------------------------------------------------- report builders
def group_for_notify(result):
    """Gom notification theo từng recipient -> 1 digest/người (giống Notifier thật)."""
    by_recipient = defaultdict(list)
    for n in result["notifications"]:
        for who in n["to"]:
            by_recipient[who].append(n["msg"])
    return by_recipient


def print_console(result, tickets, jql):
    by_lvl = defaultdict(list)
    for v in result["violations"]:
        by_lvl[v["level"]].append(v)

    print("\n" + "=" * 70)
    print(f" QA WATCHDOG — scan ngày {result['date']}")
    print(f" JQL: {jql}")
    print("=" * 70)
    if result["skipped"]:
        print(" >> Ngày nghỉ/cuối tuần — không scan, không gửi message.")
        return
    print(f" Quét {len(tickets)} ticket | "
          f"{len(result['violations'])} vi phạm | "
          f"{len(result['resolved'])} auto-resolve | "
          f"{len(result['escalations'])} escalation\n")

    for lvl in sorted(by_lvl):
        tag, icon, desc = LEVEL_META[lvl]
        print(f" {icon} {tag} — {desc}")
        for v in by_lvl[lvl]:
            print(f"    {v['ticket']:<10} [{v['rule']}]  {v['msg']}")
        print()

    if result["escalations"]:
        print(" 🚨 ESCALATIONS")
        for e in result["escalations"]:
            print(f"    {e}")
        print()

    print(" 📨 DIGEST GỬI TEAMS (gom theo người nhận)")
    for who, msgs in group_for_notify(result).items():
        print(f"    @{who} ({len(msgs)} mục):")
        for m in msgs:
            print(f"        - {m}")


def build_html(result, tickets, jql, path):
    rows = []
    by_lvl = defaultdict(list)
    for v in result["violations"]:
        by_lvl[v["level"]].append(v)
    counts = {lvl: len(by_lvl.get(lvl, [])) for lvl in LEVEL_META}

    for lvl in sorted(by_lvl):
        tag, icon, desc = LEVEL_META[lvl]
        for v in by_lvl[lvl]:
            rows.append(
                f"<tr class='lvl{lvl}'><td>{icon} {tag}</td>"
                f"<td class='k'>{html.escape(v['ticket'])}</td>"
                f"<td class='rule'>{html.escape(v['rule'])}</td>"
                f"<td>{html.escape(v['msg'])}</td></tr>")

    digest = []
    for who, msgs in group_for_notify(result).items():
        items = "".join(f"<li>{html.escape(m)}</li>" for m in msgs)
        digest.append(f"<div class='card'><h4>@{html.escape(who)} "
                      f"<span class='badge'>{len(msgs)}</span></h4><ul>{items}</ul></div>")

    doc = f"""<!doctype html><meta charset="utf-8">
<title>QA Watchdog — {result['date']}</title>
<style>
  body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1a1a2e}}
  .wrap{{max-width:1000px;margin:0 auto;padding:28px}}
  h1{{font-size:20px;margin:0 0 4px}} .jql{{font:12px ui-monospace,monospace;color:#667;background:#eef0f3;padding:8px 10px;border-radius:6px;word-break:break-all}}
  .stats{{display:flex;gap:12px;margin:18px 0}}
  .stat{{flex:1;background:#fff;border:1px solid #e4e6eb;border-radius:10px;padding:14px;text-align:center}}
  .stat b{{display:block;font-size:24px}} .stat span{{font-size:12px;color:#778}}
  table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e4e6eb;border-radius:10px;overflow:hidden}}
  th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #eef0f3;vertical-align:top}}
  th{{background:#fafbfc;font-size:12px;color:#667;text-transform:uppercase;letter-spacing:.03em}}
  td.k{{font-weight:600}} td.rule{{font:12px ui-monospace,monospace;color:#5566aa}}
  tr.lvl1{{background:#fff5f5}} tr.lvl2{{background:#fff9f0}} tr.lvl3{{background:#fffdf0}} tr.lvl0{{background:#f7f8fa}}
  h2{{font-size:15px;margin:26px 0 10px}}
  .card{{background:#fff;border:1px solid #e4e6eb;border-radius:10px;padding:12px 16px;margin-bottom:10px}}
  .card h4{{margin:0 0 6px}} .card ul{{margin:0;padding-left:18px}} .card li{{margin:3px 0}}
  .badge{{background:#e8eaf6;color:#3949ab;border-radius:10px;padding:1px 8px;font-size:12px;font-weight:600}}
  .esc{{background:#fdecea;border:1px solid #f5c6cb;border-radius:10px;padding:12px 16px;margin-bottom:10px;color:#a02622}}
</style>
<div class="wrap">
  <h1>🛡️ QA Watchdog — scan {result['date']}</h1>
  <div class="jql">JQL: {html.escape(jql)}</div>
  <div class="stats">
    <div class="stat"><b>{len(tickets)}</b><span>ticket quét</span></div>
    <div class="stat"><b style="color:#d93025">{counts[1]}</b><span>🔴 Level 1</span></div>
    <div class="stat"><b style="color:#e8710a">{counts[2]}</b><span>🟠 Level 2</span></div>
    <div class="stat"><b style="color:#c9a800">{counts[3]}</b><span>🟡 Level 3</span></div>
    <div class="stat"><b style="color:#5f6368">{counts[0]}</b><span>⚪ Data</span></div>
    <div class="stat"><b>{len(result['escalations'])}</b><span>🚨 Escalate</span></div>
  </div>
  {"".join(f"<div class='esc'>🚨 {html.escape(e)}</div>" for e in result['escalations'])}
  <h2>Chi tiết vi phạm</h2>
  <table><tr><th>Level</th><th>Ticket</th><th>Rule</th><th>Mô tả</th></tr>
  {"".join(rows) or "<tr><td colspan=4>Không có vi phạm 🎉</td></tr>"}</table>
  <h2>📨 Digest gửi Teams (gom theo người nhận)</h2>
  {"".join(digest) or "<p>Không có notification.</p>"}
</div>"""
    open(path, "w", encoding="utf-8").write(doc)
    print(f"[report] HTML -> {path}", file=sys.stderr)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="QA Watchdog Agent")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--live", action="store_true", help="Fetch Jira REST thật")
    src.add_argument("--snapshot", help="Đọc JQL result từ file JSON")
    ap.add_argument("--jql", help="JQL (chỉ dùng với --live; mặc định SCAN_JQL)")
    ap.add_argument("--date", default=date.today().isoformat(), help="Ngày scan (yyyy-mm-dd)")
    ap.add_argument("--rules", default="rules.yaml")
    ap.add_argument("--db", default="watchdog.db")
    ap.add_argument("--html", help="Xuất report HTML ra file")
    ap.add_argument("--json", help="Xuất kết quả raw ra file JSON")
    args = ap.parse_args()

    tickets, jql = get_tickets(args)
    eng = RuleEngine(args.rules, db_path=args.db)
    result = eng.scan(args.date, tickets)

    print_console(result, tickets, jql)
    if args.html:
        build_html(result, tickets, jql, args.html)
    if args.json:
        json.dump(result, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[report] JSON -> {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
