"""
Rule Engine — đọc rules.yaml, evaluate reactive/aggregate rules trên danh sách ticket.
Copy từ engine.py (parent dir) để qe_agent/ self-contained.
"""
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
