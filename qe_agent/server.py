"""
QE Watchdog Agent — HTTP server + demo UI for AgentBase Runtime.

Endpoints
  GET  /                 → the chatbot demo UI (single-page app)
  GET  /health           → 200 OK (required by AgentBase)
  POST /invocations      → run the agent, returns {response: str}  (AgentBase contract)
  POST /chat             → alias for /invocations

  GET  /api/snapshots    → available snapshot files
  GET  /api/rules        → rules.yaml parsed
  POST /api/chat         → chatbot turn (LLM agent)
  POST /api/scan         → run rule engine, violations grouped by level
  POST /api/report/preview → channel-routed HTML preview (no send)
  POST /api/report/send  → build report + e-mail the Teams channels (real send)
  GET  /api/history      → task run history (+ /api/history/{id})
  GET  /api/stats        → task status counts
  POST /api/insights     → LLM analysis over historical violations
  GET/POST/DELETE /api/schedules  → manage scheduled scans/sends
"""
import os
import sys
import json
import threading
from functools import partial
from contextlib import asynccontextmanager
from pathlib import Path

# Flat import mode (Docker: all files in /app/, no package prefix)
_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

# Load .env if present (local dev only; prod uses env vars injected by runtime)
_env = Path(_DIR) / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
import uvicorn

from claude_agent import run_agent
import qa_service
import webstore

# ---------------------------------------------------------------- scheduler
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    _HAS_APS = True
except ImportError:
    _HAS_APS = False

_scheduler = None


def _run_scheduled(sched_id: int):
    """Job body — re-reads the schedule each fire so edits take effect."""
    sched = webstore.get_schedule(sched_id)
    if not sched or not sched["enabled"]:
        return
    try:
        if sched["action"] == "send":
            res = qa_service.send_report(
                source=sched["source"], snapshot_file=sched["snapshot_file"],
                jql=sched["jql"], task_type="scheduled_send")
        else:
            res = qa_service.run_scan(
                source=sched["source"], snapshot_file=sched["snapshot_file"],
                jql=sched["jql"], task_type="scheduled_scan")
        webstore.mark_schedule_run(sched_id, res.get("status", "pass"))
    except Exception as e:  # pragma: no cover
        print(f"[scheduler] job {sched_id} failed: {e}", file=sys.stderr)
        webstore.mark_schedule_run(sched_id, "fail")


def _add_job(sched: dict):
    if not _scheduler:
        return
    job_id = f"sched-{sched['id']}"
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass
    if not sched["enabled"]:
        return
    _scheduler.add_job(
        _run_scheduled, "cron", id=job_id, args=[sched["id"]],
        day_of_week=sched["days"] or "mon-fri",
        hour=int(sched["hour"]), minute=int(sched["minute"]),
        replace_existing=True,
    )


def _seed_in_background():
    """Populate demo data on a fresh (empty) DB so History/Insights/Schedule
    are never blank after a restart or redeploy. Runs off the startup path so
    it never delays readiness; safe to fail."""
    try:
        created = qa_service.seed_demo_data()
        if created:
            for s in webstore.list_schedules():
                if s["enabled"]:
                    _add_job(s)
            print("[seed] demo data created", file=sys.stderr)
    except Exception as e:  # pragma: no cover
        print(f"[seed] skipped: {e}", file=sys.stderr)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    webstore.init_db()
    if _HAS_APS:
        _scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")
        _scheduler.start()
        for s in webstore.list_schedules():
            if s["enabled"]:
                _add_job(s)
        print(f"[scheduler] started with {len(webstore.list_schedules())} schedule(s)",
              file=sys.stderr)
    # Seed demo data in a daemon thread (non-blocking — keeps /health responsive)
    threading.Thread(target=_seed_in_background, daemon=True).start()
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="QE Watchdog Agent", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- UI + health
@app.get("/", response_class=HTMLResponse)
def index():
    ui = os.path.join(_DIR, "ui", "index.html")
    if os.path.exists(ui):
        return FileResponse(ui)
    return HTMLResponse("<h1>QE Watchdog</h1><p>UI not found.</p>")


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- AgentBase contract
@app.post("/invocations")
@app.post("/chat")
async def invoke(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    query = body.get("query") or body.get("message") or body.get("input") or ""
    snapshot = body.get("snapshot_file") or body.get("snapshot")
    if not query:
        return JSONResponse({"error": "Field 'query' is required."}, status_code=400)
    if snapshot and not os.path.isabs(snapshot):
        snapshot = os.path.join(_DIR, snapshot)
    if snapshot and "snapshot_file" not in query:
        query = query + f"\n\n(Dùng snapshot_file='{snapshot}' khi gọi search_jira và get_ticket_detail)"
    # run_agent is blocking — offload so the event loop keeps serving /health
    return {"response": await run_in_threadpool(run_agent, query, verbose=False)}


# ---------------------------------------------------------------- UI API
@app.get("/api/snapshots")
def api_snapshots():
    return {"snapshots": qa_service.list_snapshots()}


@app.get("/api/rules")
def api_rules():
    return {"rules": qa_service.list_rules()}


@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.json()
    msg = body.get("message", "").strip()
    if not msg:
        return JSONResponse({"error": "message required"}, status_code=400)
    source = body.get("source", "snapshot")
    snap = body.get("snapshot_file")

    def sse():
        # sync generator → Starlette iterates it in a threadpool (event loop stays free)
        try:
            for ev in qa_service.chat_stream(msg, source, snap):
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
        except Exception as e:  # pragma: no cover
            yield "data: " + json.dumps({"type": "done", "text": f"Lỗi: {e}"}) + "\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",   # tránh ingress buffer SSE
    })


@app.post("/api/scan")
async def api_scan(request: Request):
    body = await request.json()
    try:
        return await run_in_threadpool(partial(
            qa_service.run_scan,
            source=body.get("source", "snapshot"),
            snapshot_file=body.get("snapshot_file"),
            jql=body.get("jql"), scan_date=body.get("scan_date")))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/report/preview", response_class=HTMLResponse)
async def api_preview(request: Request):
    body = await request.json()
    try:
        html = await run_in_threadpool(partial(
            qa_service.render_report_html,
            source=body.get("source", "snapshot"),
            snapshot_file=body.get("snapshot_file"),
            jql=body.get("jql"), scan_date=body.get("scan_date")))
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<p style='color:#b91c1c;padding:24px'>Lỗi: {e}</p>", status_code=500)


@app.post("/api/report/send")
async def api_send(request: Request):
    body = await request.json()
    try:
        return await run_in_threadpool(partial(
            qa_service.send_report,
            source=body.get("source", "snapshot"),
            snapshot_file=body.get("snapshot_file"),
            jql=body.get("jql"), scan_date=body.get("scan_date"),
            dry_run=bool(body.get("dry_run", False))))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/insights")
async def api_insights(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    try:
        return await run_in_threadpool(partial(
            qa_service.get_insights, question=body.get("question")))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/history")
def api_history(limit: int = 100, type: str = None):
    return {"tasks": webstore.list_tasks(limit=limit, task_type=type)}


@app.get("/api/history/{task_id}")
def api_history_detail(task_id: int):
    t = webstore.get_task(task_id)
    if not t:
        return JSONResponse({"error": "not found"}, status_code=404)
    return t


@app.get("/api/stats")
def api_stats():
    return webstore.task_stats()


@app.get("/api/schedules")
def api_schedules():
    return {"schedules": webstore.list_schedules()}


@app.post("/api/schedules")
async def api_schedule_create(request: Request):
    body = await request.json()
    sid = webstore.add_schedule(
        name=body.get("name", "Scheduled scan"),
        hour=int(body.get("hour", 9)), minute=int(body.get("minute", 0)),
        days=body.get("days", "mon-fri"),
        action=body.get("action", "scan"),
        source=body.get("source", "snapshot"),
        snapshot_file=body.get("snapshot_file"),
        jql=body.get("jql"))
    _add_job(webstore.get_schedule(sid))
    return webstore.get_schedule(sid)


@app.delete("/api/schedules/{sched_id}")
def api_schedule_delete(sched_id: int):
    if _scheduler:
        try:
            _scheduler.remove_job(f"sched-{sched_id}")
        except Exception:
            pass
    webstore.delete_schedule(sched_id)
    return {"deleted": sched_id}


@app.post("/api/schedules/{sched_id}/toggle")
def api_schedule_toggle(sched_id: int):
    s = webstore.get_schedule(sched_id)
    if not s:
        return JSONResponse({"error": "not found"}, status_code=404)
    webstore.set_schedule_enabled(sched_id, not s["enabled"])
    _add_job(webstore.get_schedule(sched_id))
    return webstore.get_schedule(sched_id)


@app.post("/api/schedules/{sched_id}/run")
def api_schedule_run_now(sched_id: int):
    _run_scheduled(sched_id)
    return {"ran": sched_id}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
