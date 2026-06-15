"""
Rule Engine prototype — QA Process Watchdog
- Load rules.yaml
- Evaluate reactive + predictive rules trên danh sách ticket
- Lưu violations vào SQLite (history), auto-resolve, repeat-offender escalation
- Skip weekend/holiday
Chạy demo: python run_demo.py
"""
import sqlite3, yaml, json
from datetime import date, timedelta

STATUS_RANK = {
    "New": 0, "Open": 0, "Backlog": 0, "Blocked": 1,
    "InDev": 1, "In Progress": 1,
    "Walkthrough": 2, "Ready for testing": 3,
    "InTest": 4, "InReview": 5, "Done": 6,
}

# ---------------------------------------------------------------- helpers
def parse_d(s):
    return date.fromisoformat(s) if isinstance(s, str) else s

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
        self.cfg = yaml.safe_load(open(rules_path))
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
        CREATE TABLE IF NOT EXISTS escalations(
            id INTEGER PRIMARY KEY,
            ticket_key TEXT, rule_id TEXT, consecutive INT,
            escalated_date TEXT, escalated_to TEXT,
            UNIQUE(ticket_key, rule_id, escalated_date));
        CREATE TABLE IF NOT EXISTS scan_log(scan_date TEXT PRIMARY KEY, result TEXT);
        """)

    # ---- evaluate 1 reactive rule trên 1 ticket
    def _eval_condition(self, expr, ticket, today):
        t = dict(ticket)
        ns = {
            "today": today,
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
        return bool(eval(expr, {"__builtins__": {}}, ns))  # demo only; prod dùng parser an toàn

    def _capacity(self, member, days):
        cap = self.cfg.get("capacity", {})
        per_day = cap.get("members", {}).get(member, {}).get(
            "sp_per_day", cap.get("default_sp_per_day", 5))
        return per_day * days

    # ---- 1 lần scan = 1 ngày
    def scan(self, today, tickets):
        today = parse_d(today)
        out = {"date": today.isoformat(), "skipped": False,
               "violations": [], "resolved": [], "risk_resolved": [],
               "escalations": [], "notifications": [], "repeat_today": []}

        if self.cfg["schedule"].get("skip_weekends") and not is_workday(today, self.holidays):
            out["skipped"] = True
            self.db.execute("INSERT OR REPLACE INTO scan_log VALUES(?,?)",
                            (today.isoformat(), "skipped_non_workday"))
            self.db.commit()
            return out

        rules = self.cfg["rules"]
        l0_rules = [r for r in rules if r["level"] == 0]
        reactive = [r for r in rules if r["type"] == "reactive" and r["level"] > 0]
        aggregate = [r for r in rules if r["type"] == "aggregate"]
        predictive = [r for r in rules if r["type"] == "predictive"]
        fired = set()  # (ticket, rule_id) fired hôm nay

        # B1: Level 0 — flag và LOẠI ticket khỏi L1-3
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

        # B3: Aggregate — group tickets by field, count, fire if threshold met
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
                if eval(r["condition"], {"__builtins__": {}}, ctx):
                    msg = r["message"].format(**ctx)
                    out["violations"].append({"ticket": f"AGG:{gkey}", "rule": r["id"],
                                              "level": r["level"], "msg": msg})
                    # ensure group_field is filled so _resolve_recipients maps correctly
                    representative = dict(ts[0])
                    representative[group_field] = gkey
                    out["notifications"].append(
                        {"to": self._resolve_recipients(r["recipients"], representative), "msg": msg})

        # B4: Predictive — aggregate theo qe_pic
        for r in predictive:
            horizon = workdays_add(today, r["window_workdays"], self.holidays)
            groups = {}
            for t in tickets:
                if t["key"] in dirty or is_empty(t.get("story_point")):
                    continue
                d = parse_d(t.get("test_complete_date"))
                if (t.get("status") in r["active_statuses"]
                        and d and today <= d <= horizon):
                    groups.setdefault(t["qe_pic"], []).append(t)
            for pic, ts in groups.items():
                total = sum(t["story_point"] for t in ts)
                cap = self._capacity(pic, r["window_workdays"])
                if total > cap:
                    msg = r["message"].format(qe_pic=pic, total_sp=total, capacity_sp=cap,
                                              tickets=", ".join(t["key"] for t in ts))
                    out["violations"].append({"ticket": f"AGG:{pic}", "rule": r["id"],
                                              "level": r["level"], "msg": msg})
                    out["notifications"].append({"to": r["recipients"], "msg": msg})

        # B4: Auto-resolve — violation cũ chưa resolve mà hôm nay không fire nữa
        fired_tickets = {tk for tk, _ in fired}
        open_v = self.db.execute(
            "SELECT id, ticket_key, rule_id FROM violations WHERE resolved_date IS NULL").fetchall()
        for vid, tk, rid in open_v:
            if (tk, rid) not in fired:
                # Ticket still fires other rules today → risky resolve, not clean fix
                still_active = tk in fired_tickets
                res_type = "risk" if still_active else "fixed"
                self.db.execute("UPDATE violations SET resolved_date=?, resolution_type=? WHERE id=?",
                                (today.isoformat(), res_type, vid))
                if still_active:
                    out["risk_resolved"].append(f"{tk} / {rid}")
                else:
                    out["resolved"].append(f"{tk} / {rid}")

        # B4b: Notes — violations that also fired on the previous scan day
        prev_row = self.db.execute(
            "SELECT scan_date FROM scan_log WHERE result='ok' AND scan_date < ? "
            "ORDER BY scan_date DESC LIMIT 1",
            (today.isoformat(),)
        ).fetchone()
        if prev_row:
            prev_date = prev_row[0]
            for tk, rid in fired:
                still = self.db.execute(
                    "SELECT 1 FROM violations WHERE ticket_key=? AND rule_id=? AND fired_date=?",
                    (tk, rid, prev_date)
                ).fetchone()
                if still:
                    viol = next((v for v in out["violations"]
                                 if v["ticket"] == tk and v["rule"] == rid), None)
                    if viol:
                        out["repeat_today"].append({**viol, "since": prev_date})

        # B5: Repeat offender — fire N lần scan liên tiếp -> escalate
        for r in reactive:
            esc = r.get("escalation")
            if not esc:
                continue
            n = esc["repeat_threshold"]
            recent_scans = [row[0] for row in self.db.execute(
                "SELECT scan_date FROM scan_log WHERE result='ok' ORDER BY scan_date DESC LIMIT ?",
                (n - 1,))]
            check_dates = sorted(recent_scans + [today.isoformat()])
            if len(check_dates) < n:
                continue
            rows = self.db.execute(
                f"""SELECT ticket_key, COUNT(DISTINCT fired_date) FROM violations
                    WHERE rule_id=? AND fired_date IN ({','.join('?'*n)})
                    GROUP BY ticket_key HAVING COUNT(DISTINCT fired_date)=?""",
                (r["id"], *check_dates, n)).fetchall()
            for tk, cnt in rows:
                already = self.db.execute(
                    "SELECT 1 FROM escalations WHERE ticket_key=? AND rule_id=? AND escalated_date>=?",
                    (tk, r["id"], check_dates[0])).fetchone()
                if already:
                    continue
                self.db.execute("INSERT INTO escalations(ticket_key,rule_id,consecutive,escalated_date,escalated_to) VALUES(?,?,?,?,?)",
                                (tk, r["id"], cnt, today.isoformat(), ",".join(esc["escalate_to"])))
                tinfo = next((x for x in tickets if x["key"] == tk), {})
                msg = esc["message"].format(key=tk, rule_name=r["name"], n=cnt,
                                            assignee=tinfo.get("assignee", "?"))
                out["escalations"].append(msg)
                out["notifications"].append({"to": esc["escalate_to"], "msg": msg})

        self.db.execute("INSERT OR REPLACE INTO scan_log VALUES(?, 'ok')", (today.isoformat(),))
        self.db.commit()
        return out

    def _record(self, out, fired, t, r, today, extra_fmt=None):
        fmt = {k: t.get(k, "") for k in ("key", "status", "story_point", "test_complete_date", "assignee")}
        if extra_fmt:
            fmt.update(extra_fmt)
        msg = r["message"].format(**fmt)
        self.db.execute(
            """INSERT OR IGNORE INTO violations
               (ticket_key, rule_id, level, fired_date, assignee, qe_pic, status_at_fire, snapshot)
               VALUES(?,?,?,?,?,?,?,?)""",
            (t["key"], r["id"], r["level"], today.isoformat(),
             t.get("assignee"), t.get("qe_pic"), t.get("status"), json.dumps(t)))
        fired.add((t["key"], r["id"]))
        out["violations"].append({
            "ticket": t["key"], "rule": r["id"], "level": r["level"], "msg": msg,
            "assignee": t.get("assignee"), "qe_pic": t.get("qe_pic"),
        })
        out["notifications"].append({"to": self._resolve_recipients(r["recipients"], t), "msg": msg})

    def _resolve_recipients(self, aliases, ticket):
        """Đổi alias (qe_pic/assignee/...) -> người thật của ticket; alias tĩnh giữ nguyên.
        Nếu qe_pic trống -> fallback labels[0] (convention: label = tên QE)."""
        resolved = []
        for a in aliases:
            v = ticket.get(a)
            if not v and a == "qe_pic":
                labels = ticket.get("labels", [])
                v = labels[0] if labels else None
            resolved.append(v if v else a)
        return resolved

    # ---- report queries
    def ticket_history(self, key):
        return self.db.execute(
            """SELECT rule_id, level, COUNT(*), MIN(fired_date), MAX(fired_date),
                      SUM(CASE WHEN resolved_date IS NULL THEN 1 ELSE 0 END)
               FROM violations WHERE ticket_key=? GROUP BY rule_id, level""", (key,)).fetchall()

    def rule_frequency(self):
        return self.db.execute(
            """SELECT rule_id, level, COUNT(*) fires, COUNT(DISTINCT ticket_key)
               FROM violations GROUP BY rule_id ORDER BY fires DESC""").fetchall()
