"""
Rule Engine — đọc rules.yaml, evaluate reactive/aggregate rules trên danh sách ticket.
Cũng export hàm run() để chạy pipeline Ticket → DailyReport.
"""
import os
import sqlite3
import yaml
import json
from datetime import date, timedelta

STATUS_RANK = {
    "New": 0, "Open": 0, "Backlog": 0,
    "In Analysis": 0, "Reviewed": 0,
    "Blocked": 1, "InDev": 1, "In Progress": 1,
    "Walkthrough": 2, "Ready for testing": 3,
    "InTest": 4, "InReview": 5,
    "Done": 6, "Resolved": 6, "Live": 7, "Cancelled": 7,
}


# ---------------------------------------------------------------- helpers
def parse_d(s):
    if isinstance(s, date):
        return s
    return date.fromisoformat(s) if s else None


def is_workday(d, holidays):
    return d.weekday() < 5 and d.isoformat() not in holidays


def workdays_add(d, n, holidays=()):
    step = 1 if n >= 0 else -1
    cur, left = d, abs(n)
    while left:
        cur += timedelta(days=step)
        if is_workday(cur, holidays):
            left -= 1
    return cur


def is_empty(v):
    return v is None or v == ""


# ---------------------------------------------------------------- engine
class RuleEngine:
    def __init__(self, rules_path, db_path=":memory:"):
        with open(rules_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        self.holidays = set(self.cfg["schedule"].get("holidays", []))
        self.db = sqlite3.connect(db_path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS violations(
            id INTEGER PRIMARY KEY,
            ticket_key TEXT, rule_id TEXT, level INT,
            fired_date TEXT, assignee TEXT, qe_pic TEXT,
            status_at_fire TEXT, snapshot TEXT,
            resolved_date TEXT, resolution_type TEXT,
            UNIQUE(ticket_key, rule_id, fired_date));
        CREATE TABLE IF NOT EXISTS scan_log(scan_date TEXT PRIMARY KEY, result TEXT);
        """)

    def _eval_condition(self, expr, ticket, today):
        t = dict(ticket)
        ns = {
            "today": today,
            "timedelta": timedelta,
            "is_empty": is_empty,
            "workdays_add": lambda d, n: workdays_add(d, n, self.holidays),
            "rank": lambda s: STATUS_RANK.get(s, 0),
            "status_rank": STATUS_RANK.get(t.get("status"), 0),
            "key": t.get("key"),
            "status": t.get("status"),
            "story_point": t.get("story_point"),
            "assignee": t.get("assignee"),
            "qe_pic": t.get("qe_pic"),
            "no_qe": bool(t.get("no_qe")),
            "test_start_date": parse_d(t.get("test_start_date")),
            "test_complete_date": parse_d(t.get("test_complete_date")),
            "sandbox_date": parse_d(t.get("sandbox_date")),
        }
        try:
            return bool(eval(expr, {"__builtins__": {}}, ns))
        except Exception:
            return False

    def scan(self, today, tickets, skip_weekend_check=False):
        today = parse_d(today)
        out = {"date": today.isoformat(), "skipped": False,
               "violations": [], "resolved": []}

        if not skip_weekend_check and self.cfg["schedule"].get("skip_weekends") \
                and not is_workday(today, self.holidays):
            out["skipped"] = True
            return out

        rules = self.cfg["rules"]
        l0_rules = [r for r in rules if r["level"] == 0]
        reactive = [r for r in rules if r["type"] == "reactive" and r["level"] > 0]
        aggregate = [r for r in rules if r["type"] == "aggregate"]
        fired = set()

        # B1: Level 0 — flag và loại ticket khỏi L1-3
        dirty = set()
        _DATE_LABELS = [
            ("sandbox_date",       "sandbox"),
            ("test_start_date",    "test start"),
            ("test_complete_date", "test complete"),
        ]
        for t in tickets:
            for r in l0_rules:
                if self._eval_condition(r["condition"], t, today):
                    missing = [lbl for field, lbl in _DATE_LABELS if is_empty(t.get(field))]
                    extra = {"missing_dates": ", ".join(missing) if missing else "unknown"}
                    self._record(out, fired, t, r, today, extra_fmt=extra)
                    dirty.add(t["key"])

        # B2: Reactive L1-3 trên ticket sạch data
        for t in tickets:
            if t["key"] in dirty:
                continue
            for r in reactive:
                if self._eval_condition(r["condition"], t, today):
                    self._record(out, fired, t, r, today)

        # B3: Aggregate
        for r in aggregate:
            flt = r.get("filter", "True")
            group_field = r["aggregate_by"]
            fallback_field = r.get("aggregate_by_fallback")
            groups = {}
            for t in tickets:
                if t["key"] in dirty:
                    continue
                if not self._eval_condition(flt, t, today):
                    continue
                gkey = t.get(group_field) or ""
                if not gkey and fallback_field:
                    fb = t.get(fallback_field, [])
                    gkey = (fb[0] if fb else "") if isinstance(fb, list) else (fb or "")
                gkey = gkey or "unknown"
                groups.setdefault(gkey, []).append(t)
            for gkey, ts in groups.items():
                ctx = {group_field: gkey, "count": len(ts),
                       "ticket_list": ", ".join(t["key"] for t in ts)}
                try:
                    fired_agg = bool(eval(r["condition"], {"__builtins__": {}}, ctx))
                except Exception:
                    fired_agg = False
                if fired_agg:
                    msg = r["message"].format(**ctx)
                    out["violations"].append({
                        "ticket": f"AGG:{gkey}", "rule": r["id"],
                        "level": r["level"], "msg": msg,
                        "agg_tickets": [t["key"] for t in ts],
                    })

        self.db.execute("INSERT OR REPLACE INTO scan_log VALUES(?, 'ok')", (today.isoformat(),))
        self.db.commit()
        return out

    def _record(self, out, fired, t, r, today, extra_fmt=None):
        fmt = {k: t.get(k, "") for k in ("key", "status", "story_point",
                                          "test_start_date", "test_complete_date",
                                          "assignee", "sandbox_date")}
        if extra_fmt:
            fmt.update(extra_fmt)
        try:
            msg = r["message"].format(**fmt)
        except KeyError:
            msg = r["message"]
        self.db.execute(
            """INSERT OR IGNORE INTO violations
               (ticket_key, rule_id, level, fired_date, assignee, qe_pic, status_at_fire, snapshot)
               VALUES(?,?,?,?,?,?,?,?)""",
            (t["key"], r["id"], r["level"], today.isoformat(),
             t.get("assignee"), t.get("qe_pic"), t.get("status"), json.dumps(t, default=str)))
        fired.add((t["key"], r["id"]))
        out["violations"].append({
            "ticket": t["key"], "rule": r["id"], "level": r["level"], "msg": msg,
            "assignee": t.get("assignee"), "qe_pic": t.get("qe_pic"),
        })

    def rules_by_id(self):
        return {r["id"]: r for r in self.cfg["rules"]}

    def ticket_history(self, ticket_key: str) -> list:
        cur = self.db.execute(
            """SELECT rule_id, level, COUNT(*) as count,
                      MIN(fired_date) as first, MAX(fired_date) as last,
                      SUM(CASE WHEN resolved_date IS NULL THEN 1 ELSE 0 END) as open
               FROM violations WHERE ticket_key = ?
               GROUP BY rule_id, level""",
            (ticket_key,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------- pipeline helper
# Replaces rules.py — converts Ticket dataclasses → dicts → RuleEngine → DailyReport

_RULES_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.yaml")
_LEVEL_MAP = None  # lazy import to avoid circular


def _get_level_map():
    global _LEVEL_MAP
    if _LEVEL_MAP is None:
        try:
            from .models import Level
        except ImportError:
            from models import Level
        _LEVEL_MAP = {0: Level.DATA, 1: Level.VIOLENT, 2: Level.RISK, 3: Level.COMMIT}
    return _LEVEL_MAP


def _to_dict(t) -> dict:
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


def run(tickets: list, today: date = None):
    try:
        from .models import DailyReport, RuleResult
    except ImportError:
        from models import DailyReport, RuleResult
    today = today or date.today()
    from datetime import timedelta
    tomorrow = today + timedelta(days=1)
    report = DailyReport(report_date=today)

    report.need_start_today = [t for t in tickets if t.test_start_date == today]
    report.need_complete_today = [t for t in tickets if t.test_complete_date == today]
    report.sandbox_tomorrow = [t for t in tickets if t.sandbox_date == tomorrow]
    report.blocked = [t for t in tickets if t.blocked]

    engine = RuleEngine(_RULES_YAML)
    ticket_dicts = [_to_dict(t) for t in tickets]
    ticket_by_key = {t.id: t for t in tickets}
    rules_by_id = engine.rules_by_id()

    scan = engine.scan(today, ticket_dicts, skip_weekend_check=True)
    level_map = _get_level_map()

    for v in scan["violations"]:
        key = v["ticket"]
        level_int = v["level"]

        if key.startswith("AGG:"):
            _handle_aggregate(v, rules_by_id, ticket_by_key, report, level_map)
            continue

        ticket = ticket_by_key.get(key)
        if not ticket:
            continue

        rule_def = rules_by_id.get(v["rule"], {})
        channels = rule_def.get("channels", [])
        recipients = rule_def.get("recipients", [])

        result = RuleResult(
            rule_id=v["rule"],
            level=level_map.get(level_int, level_map[2]),
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


def _handle_aggregate(v, rules_by_id, ticket_by_key, report, level_map):
    try:
        from .models import RuleResult
    except ImportError:
        from models import RuleResult
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
        level=level_map.get(level_int, level_map[2]),
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
