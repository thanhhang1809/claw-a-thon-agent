"""
webstore.py — persistence for the Watchdog UI.

Reuses the same SQLite file as the rule engine (watchdog.db) but owns two
extra tables:

  task_runs  — history of every executed action (scan / send / chat / insight /
               scheduled_*). Each row carries a status (running|pass|warn|fail),
               a short summary and a JSON `detail` blob.
  schedules  — user-defined cron jobs (hour/minute/days + action + data source).

All functions open a short-lived connection with check_same_thread=False so they
are safe to call from both the uvicorn request threads and the APScheduler
background thread.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(_DIR, "watchdog.db")

# Zalopay timezone for human-friendly timestamps
_TZ = timezone(timedelta(hours=7))


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_runs(
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT,
                source      TEXT,
                params      TEXT,
                status      TEXT,
                summary     TEXT,
                detail      TEXT,
                created_at  TEXT,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS schedules(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT,
                hour          INTEGER,
                minute        INTEGER,
                days          TEXT,
                action        TEXT,
                source        TEXT,
                snapshot_file TEXT,
                jql           TEXT,
                enabled       INTEGER DEFAULT 1,
                created_at    TEXT,
                last_run      TEXT,
                last_status   TEXT
            );
            -- Ensure the rule-engine tables exist too, so the Insights tab
            -- can query `violations` on a fresh deploy before any scan runs.
            CREATE TABLE IF NOT EXISTS violations(
                id INTEGER PRIMARY KEY,
                ticket_key TEXT, rule_id TEXT, level INT,
                fired_date TEXT, assignee TEXT, qe_pic TEXT,
                status_at_fire TEXT, snapshot TEXT,
                resolved_date TEXT, resolution_type TEXT,
                UNIQUE(ticket_key, rule_id, fired_date));
            CREATE TABLE IF NOT EXISTS scan_log(scan_date TEXT PRIMARY KEY, result TEXT);
            """
        )


# ---------------------------------------------------------------- task_runs
def create_task(task_type: str, source: str, params: dict | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO task_runs(type, source, params, status, created_at) "
            "VALUES(?,?,?,?,?)",
            (task_type, source, json.dumps(params or {}, default=str), "running", _now()),
        )
        return cur.lastrowid


def finish_task(task_id: int, status: str, summary: str,
                detail: Any = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE task_runs SET status=?, summary=?, detail=?, finished_at=? "
            "WHERE id=?",
            (status, summary, json.dumps(detail, default=str) if detail is not None else None,
             _now(), task_id),
        )


def _row_to_task(r: sqlite3.Row, *, with_detail: bool = False) -> dict:
    out = {
        "id": r["id"],
        "type": r["type"],
        "source": r["source"],
        "params": json.loads(r["params"] or "{}"),
        "status": r["status"],
        "summary": r["summary"],
        "created_at": r["created_at"],
        "finished_at": r["finished_at"],
    }
    if with_detail:
        out["detail"] = json.loads(r["detail"]) if r["detail"] else None
    return out


def list_tasks(limit: int = 100, task_type: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM task_runs"
    args: list = []
    if task_type:
        q += " WHERE type = ?"
        args.append(task_type)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [_row_to_task(r) for r in c.execute(q, args).fetchall()]


def get_task(task_id: int) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM task_runs WHERE id=?", (task_id,)).fetchone()
        return _row_to_task(r, with_detail=True) if r else None


def task_stats() -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) n FROM task_runs GROUP BY status"
        ).fetchall()
    out = {"pass": 0, "warn": 0, "fail": 0, "running": 0, "total": 0}
    for r in rows:
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ---------------------------------------------------------------- schedules
def add_schedule(name: str, hour: int, minute: int, days: str, action: str,
                 source: str, snapshot_file: Optional[str], jql: Optional[str]) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO schedules(name, hour, minute, days, action, source, "
            "snapshot_file, jql, enabled, created_at) VALUES(?,?,?,?,?,?,?,?,1,?)",
            (name, hour, minute, days, action, source, snapshot_file, jql, _now()),
        )
        return cur.lastrowid


def _row_to_sched(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "hour": r["hour"],
        "minute": r["minute"],
        "days": r["days"],
        "action": r["action"],
        "source": r["source"],
        "snapshot_file": r["snapshot_file"],
        "jql": r["jql"],
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
        "last_run": r["last_run"],
        "last_status": r["last_status"],
    }


def list_schedules() -> list[dict]:
    with _conn() as c:
        return [_row_to_sched(r) for r in
                c.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()]


def get_schedule(sched_id: int) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM schedules WHERE id=?", (sched_id,)).fetchone()
        return _row_to_sched(r) if r else None


def delete_schedule(sched_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM schedules WHERE id=?", (sched_id,))


def set_schedule_enabled(sched_id: int, enabled: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE schedules SET enabled=? WHERE id=?", (1 if enabled else 0, sched_id))


def mark_schedule_run(sched_id: int, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE schedules SET last_run=?, last_status=? WHERE id=?",
                  (_now(), status, sched_id))
