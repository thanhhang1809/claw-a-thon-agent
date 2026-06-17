# QE Watchdog Agent

Automated QE process monitoring agent for Zalopay: scans Jira tickets for test violations, generates daily compliance reports, and sends notifications to team channels on Microsoft Teams via Gmail SMTP.

**Runs on:** GreenNode AgentBase | **LLM:** GreenNode AI Platform (OpenAI-compatible)

---

## Purpose

**Problem:** QE teams struggle to track testing compliance across sprints. Manual monitoring is error-prone; violations (delayed test dates, missing test info, sandbox conflicts) go unnoticed until they impact releases.

**Solution:** QE Watchdog automatically scans the current sprint daily, detects violations by level (0–3), routes alerts to team channels, and provides chat-based insights into violations and test readiness.

---

## Key Features

- **🔍 Automated Violation Scanning** — Runs on schedule (9:00 AM daily), scans sprint tickets against 10+ rules, categorizes by level (Data/Violent/Risk/Commit)
- **📊 Level-Based Alerts** — Color-coded severity (L0=blue, L1=red, L2=orange, L3=yellow) with consolidated views by level
- **📧 Smart Email Routing** — Routes violations to component teams (MS/CRM dev channels) + QE daily summary. **Every team gets daily status** (violations or "all clear")
- **💬 Interactive Chat** — Ask agent for violation details, test readiness, rule explanations
- **📅 Scheduler** — Runs daily at 9:00 AM on working days (skips weekends/holidays)
- **🔄 Multi-Source Data** — Fetch from live Jira, snapshots (demo), or specific sprint/keys
- **📱 Web UI** — Chat, scan on-demand, review rules, see run history

---

## How It Works (Pipeline)

```
┌─────────────────────────────────────────────────────────┐
│ FETCH (Source: Live Jira / Snapshot / Sprint)           │
│ • Pulls tickets from JIRA_JQL or snapshot JSON          │
│ • Normalizes fields → Ticket dataclass                  │
└──────────────────┬──────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────┐
│ EVALUATE RULES (RuleEngine)                             │
│ • B1: Level 0 — missing test dates                      │
│ • B2: Level 1–3 — test start/complete/sandbox timing   │
│ • B3: Aggregate — e.g., "≥4 tickets need QE today"     │
└──────────────────┬──────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────┐
│ ROUTE & RENDER (TeamsSender)                            │
│ • Route violations by component (MS/CRM)                │
│ • Group by level, consolidate into 1 table per level   │
│ • Render HTML email + plain text                        │
└──────────────────┬──────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────┐
│ SEND (Gmail SMTP → Teams Channels)                      │
│ • Send violations to dev_ms, dev_crm, qe_daily         │
│ • If no violations: send ✅ "all clear" per team        │
└─────────────────────────────────────────────────────────┘
```

---

## Directory Structure

```
.                     # repo root (main source)
├── server.py            # FastAPI app (web UI + chat endpoints)
├── main.py              # CLI: run pipeline once or schedule daily
├── paths.py             # Centralized paths + .env auto-loading
│
├── agent/               # LLM chat agent
│   └── claude_agent.py  # Tool-calling loop + streaming (GreenNode)
│
├── engine/              # Core business logic (pure Python, no I/O)
│   ├── models.py        # Dataclasses: Ticket, DailyReport, Level, Routing
│   └── rule_engine.py   # Rule evaluator → violations grouped by level
│
├── integrations/        # External system connectors
│   ├── jira_fetcher.py  # Fetch Jira API (live or snapshot JSON)
│   └── teams_sender.py  # Render HTML/text + send via Gmail SMTP
│
├── services/            # Business layer for web/API
│   ├── qa_service.py    # scan() / send() / chat() orchestration
│   └── webstore.py      # SQLite: task runs, schedules
│
├── config/
│   └── rules.yaml       # 10+ rule definitions (Levels 0–3)
│
├── snapshots/           # Demo data: Jira exports (*.json)
├── data/                # Runtime: watchdog.db (gitignored)
├── ui/                  # Web UI (SPA chatbot)
│
├── Dockerfile  .dockerignore  requirements.txt
```

**Import convention:** All modules use absolute imports from ROOT (`from engine.rule_engine import …`). `server.py` and `main.py` add ROOT to `sys.path`. In Docker, ROOT = `/app`.

---

## Rule System & Violation Levels

Rules are defined in `config/rules.yaml`. Each rule has a level (0–3) and a condition (Python expression).


| Level | Name    | Color     | Meaning                | Example                                                  |
| ----- | ------- | --------- | ---------------------- | -------------------------------------------------------- |
| **0** | DATA    | 📋 Blue   | Missing test metadata  | `test_start_date = NULL`                                 |
| **1** | VIOLENT | 🔴 Red    | Test timeline conflict | Test started 3 days ago but still in "New" status        |
| **2** | RISK    | 🟠 Orange | Approaching deadline   | Test complete date is today but status is still "InTest" |
| **3** | TRACK   | 🟡 Yellow | Sandbox + release prep | Sandbox planned today but not ready for walkthrough      |


**Evaluation flow:**

1. **Level 0 (Data):** Clean data first; flag tickets with missing dates
2. **Levels 1–3 (Reactive):** Test status vs. timeline; e.g., test start date < today but status < "InTest" → VIOLENT
3. **Aggregate:** Count by QE PIC; e.g., ≥4 need testing today → flag

---

## Component Routing (MS / CRM)

Tickets are routed by their `components` field (Jira standard):


| Component             | Dev Channel               | QE Always Sees?            |
| --------------------- | ------------------------- | -------------------------- |
| `Marketing Solutions` | `dev_ms`                  | ✅ Yes (QE Daily shows all) |
| `CRM`                 | `dev_crm`                 | ✅ Yes (QE Daily shows all) |
| (None/Other)          | Both `dev_ms` + `dev_crm` | ✅ Yes                      |


**Email headers:**

- **QE Daily:** All violations from all components (consolidated)
- **MS daily:** Only Marketing Solutions violations
- **CRM daily:** Only CRM violations (or ✅ "all clear" if none)

Each email always includes a status emoji:

- 🔴 = violations found
- ✅ = no violations (all clear)

---

## Setup & Installation

### Prerequisites

- Python 3.9+
- GreenNode credentials (IAM Client ID/Secret)
- Gmail app password for SMTP
- (Optional) Jira API token if using live Jira

### Local Setup

```bash
cd qe_agent
pip install -r requirements.txt

# Run once with demo data
python main.py --once --snapshot demo_watchdog.json

# Run web UI
python server.py
# Open http://localhost:8080
```

### Environment Variables

Create `.env` in the repo root. The `paths.py` module auto-loads it on import.

```bash
# GreenNode (required for scheduler/chat)
GREENNODE_CLIENT_ID=<uuid>
GREENNODE_CLIENT_SECRET=<secret>

# Email (required for report sending)
GMAIL_USER=<your-email@gmail.com>
GMAIL_APP_PASSWORD=<app-specific-password>

# Teams channel emails (where reports are sent)
TEAMS_EMAIL_QE=<qe-channel-email@apac.teams.ms>
TEAMS_EMAIL_DEV_MS=<ms-dev-channel-email@apac.teams.ms>
TEAMS_EMAIL_DEV_CRM=<crm-dev-channel-email@apac.teams.ms>

# Jira (optional; if not set, uses snapshot data)
JIRA_BASE_URL=https://jira.zalopay.vn
JIRA_PAT=<personal-access-token>
JIRA_PROJECT=GE

# Report settings (optional)
REPORT_MAX_ROWS_PER_EMAIL=12

# Auto-injected by GreenNode AgentBase (do not set manually)
GREENNODE_AGENT_IDENTITY=<auto>
GREENNODE_ENDPOINT_URL=<auto>
```

**Note:** `GREENNODE_CLIENT_ID/SECRET` and `GREENNODE_AGENT_IDENTITY/ENDPOINT_URL` are auto-injected by the AgentBase runtime. Only set them locally for development.

---

## Usage

### CLI: Run Once

```bash
# With demo data (snapshot)
python main.py --once --snapshot demo_watchdog.json

# With live Jira
python main.py --once

# By sprint name
python main.py --once --sprint "MS - Sprint 26.06.B" --team MS

# By ticket keys
python main.py --once --keys GE-23511 GE-23407

# Preview HTML in browser (no email sent)
python main.py --once --snapshot ge_sprint_snapshot.json --preview

# Dry-run (print text to console, no email)
python main.py --once --snapshot demo_watchdog.json --dry-run
```

### CLI: Scheduler (Daily 9:00 AM)

```bash
python main.py
# Runs in background, scans at 9:00 AM on working days (Mon–Fri, skips holidays)
```

### Web UI

```bash
python server.py
```

Then open **[http://localhost:8080](http://localhost:8080)** and:

- 💬 **Chat** — Ask agent about violations or test readiness
- 🔍 **Scan** — Trigger scan now (don't wait for 9 AM)
- 📋 **Rules** — View rule definitions and recent violations
- 📅 **Schedule** — See run history and next run time

---

## Email Format & Examples

### Email with Violations

```
Subject: 🔴 QE Watchdog 2026-06-17 — MS daily

┌─────────────────────────────────────┐
│ LEVEL 1 — VIOLATION (6 tickets)     │
├─────────────────────────────────────┤
│ Ticket    │ Issue          │ Reason   │
│ GE-23511  │ [Story] FE...  │ Test     │
│           │                │ started  │
│           │                │ 4 days   │
│           │                │ ago but  │
│           │                │ status   │
│           │                │ not      │
│           │                │ InTest   │
└─────────────────────────────────────┘
```

### Email with No Violations

```
Subject: ✅ QE Watchdog 2026-06-17 — CRM daily

✅ Không có vi phạm ngày hôm nay 🎉
```

### Report Sections (if present)

- **LEVEL 0 (Data)** — Missing test dates (blue bar)
- **LEVEL 1 (Violation)** — Red violations (red bar)
- **LEVEL 2 (Risk)** — Orange warnings (orange bar)
- **LEVEL 3 (Commit)** — Yellow sandbox/release prep (yellow bar)
- **Checklists** — Test start today, test complete today, sandbox tomorrow, blocked tickets

Each row shows:

- **Ticket key** (e.g., GE-23511)
- **Issue type chip** (Bug/Story/Task with color)
- **Status chip** (gray inline badge)
- **Reason** (rule violation message in red)

---

## Data Sources

### Snapshots (Demo / Offline)

Store a Jira export (`.json`) in `snapshots/`. Format:

```json
{
  "issues": [
    {
      "key": "GE-23511",
      "fields": {
        "summary": "[MS][...] Title",
        "status": { "name": "Ready for testing" },
        "components": [{ "name": "Marketing Solutions" }],
        "customfield_10801": 1.0,
        "customfield_13703": "2026-06-10",
        ...
      }
    }
  ]
}
```

Then run:

```bash
python main.py --once --snapshot demo_watchdog.json
```

### Live Jira

Set `JIRA_BASE_URL` and `JIRA_PAT`. Then:

```bash
python main.py --once  # Uses ROADMAP_JQL from config
```

---

## Deployment

### Docker

```bash
docker build -t qe-agent:latest .
docker run -p 8080:8080 \
  -e GREENNODE_CLIENT_ID=<id> \
  -e GREENNODE_CLIENT_SECRET=<secret> \
  -e GMAIL_USER=<user> \
  -e GMAIL_APP_PASSWORD=<pass> \
  -e TEAMS_EMAIL_QE=<email> \
  -e TEAMS_EMAIL_DEV_MS=<email> \
  -e TEAMS_EMAIL_DEV_CRM=<email> \
  qe-agent:latest
```

### GreenNode AgentBase

```bash
bash greennode-agentbase-skills/.claude/skills/agentbase/scripts/runtime.sh create \
  --name "qe-agent" \
  --image "vcr.vngcloud.vn/111480-abp112037/qe-agent:latest" \
  --flavor "runtime-s2-general-2x4" \
  --from-cr
```

See `[greennode-agentbase-skills/.claude/skills/agentbase-deploy/SKILL.md](greennode-agentbase-skills/.claude/skills/agentbase-deploy/SKILL.md)` for details.

---

## Architecture Notes

- **Stateless pipeline:** Each run is independent; no shared state except SQLite (task runs, schedules)
- **Component-based:** Each module (fetch, rules, send) can be tested/swapped independently
- **Configuration-driven:** Rules live in `rules.yaml`, not code
- **LLM optional:** Core scanning works without LLM; chat is a convenience layer
- **Email format:** Multipart (text/plain + text/html) for Gmail + Teams compatibility

---

## Troubleshooting

**Q: Email not sending?**

- Check `GMAIL_USER` and `GMAIL_APP_PASSWORD` in `.env`
- Verify `TEAMS_EMAIL_`* addresses are correct
- Check firewall allows SMTP (port 587)

**Q: No violations found but I expect some?**

- Check ticket data: `test_start_date`, `test_complete_date`, `sandbox_date` must be YYYY-MM-DD
- Check status spelling (case-insensitive, but must match rules.yaml)
- Run `python main.py --once --preview` to inspect HTML without sending

**Q: Scheduler not running at 9 AM?**

- Check process is still running: `ps aux | grep main.py`
- Verify timezone is correct
- Check logs for errors

**Q: "Component not found" or routing to both channels?**

- Add `components` field to Jira ticket (if missing, routes to both MS + CRM)

---

## Contributing

Rules & thresholds can be adjusted in `config/rules.yaml` without restarting.
To add new email features, modify `teams_sender.py` (HTML builder, routing logic).

---

## Support

For issues or questions, check the troubleshooting section above or refer to the project documentation.