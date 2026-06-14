"""
Module 1C - Teams Sender (multi-channel routing).
Route từng nhóm message tới đúng channel dựa trên (loại message x component).

Channels (mỗi cái 1 Incoming Webhook URL riêng):
  TEAMS_WEBHOOK_QE       -> QE daily channel
  TEAMS_WEBHOOK_DEV_MS   -> Dev channel, component "Marketing Solutions"
  TEAMS_WEBHOOK_DEV_CRM  -> Dev channel, component "CRM"

Routing:
  - Simple lists (test start/complete): QE + Dev (theo component)
  - Sandbox tomorrow: Dev (theo component), mention assignee
  - Blocked: Dev (theo component), mention QE PIC + assignee
  - Rule results: dựa trên RuleResult.send_qe / send_dev (Person 2 set)
  - Ticket không thuộc MS/CRM -> gửi cả Dev MS lẫn Dev CRM

LƯU Ý @mention: Incoming Webhook KHÔNG mention thật được. "@name" ở đây là text.
Muốn mention nảy notification -> Graph API / Power Automate (dùng *_username).
"""
from __future__ import annotations
import os
import requests
from collections import defaultdict
from .models import (
    DailyReport, Ticket, RuleResult,
    CH_QE, CH_DEV_MS, CH_DEV_CRM, dev_channels_for,
)

LEVEL_NAME = {1: "🔴 LEVEL 1 — Violent", 2: "🟠 LEVEL 2 — Risk", 3: "🟡 LEVEL 3 — Commit Risk"}

CHANNEL_ENV = {
    CH_QE:      "TEAMS_WEBHOOK_QE",
    CH_DEV_MS:  "TEAMS_WEBHOOK_DEV_MS",
    CH_DEV_CRM: "TEAMS_WEBHOOK_DEV_CRM",
}
CHANNEL_TITLE = {
    CH_QE: "QE Daily", CH_DEV_MS: "Dev — MS", CH_DEV_CRM: "Dev — CRM",
}


# ---------------------------------------------------------------------------
# Mention helpers
# ---------------------------------------------------------------------------
def _mention(name, no_qe=False):
    if not name:
        return "(unassigned)"
    return f"@{name} [NoQE]" if no_qe else f"@{name}"


def _line(t: Ticket, with_qe=False, with_assignee=True):
    parts = [f"**{t.id}** {t.title}"]
    who = []
    if with_qe:
        who.append(_mention(t.qe_pic))
    if with_assignee:
        who.append(_mention(t.assignee, t.no_qe))
    if who:
        parts.append(" — " + " / ".join(who))
    return "• " + "".join(parts)


# ---------------------------------------------------------------------------
# Build per-channel content: dict[channel] -> list[(header, [card_blocks])]
# ---------------------------------------------------------------------------
def route(report: DailyReport):
    # buckets[channel][header] = list of (ticket, extra_facts)
    buckets: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    def add(channels, header, ticket, extra=None):
        for ch in channels:
            buckets[ch][header].append((ticket, extra))

    for t in report.need_start_today:
        add([CH_QE] + dev_channels_for(t.component), "📋 Test Start Today", t)
    for t in report.need_complete_today:
        add([CH_QE] + dev_channels_for(t.component), "✅ Test Complete Today", t)
    for t in report.sandbox_tomorrow:
        add(dev_channels_for(t.component), "📦 Sandbox Tomorrow", t)
    for t in report.blocked:
        add(dev_channels_for(t.component), "⛔ Blocked", t)

    for results, lvl in [(report.level1, 1), (report.level2, 2), (report.level3, 3)]:
        for r in results:
            chans = ([CH_QE] if r.send_qe else []) + \
                    (dev_channels_for(r.ticket.component) if r.send_dev else [])
            add(chans, LEVEL_NAME[lvl], r.ticket, {"Reason": r.reason})

    # build adaptive card blocks per channel
    out: dict[str, list] = {}
    for ch, by_header in buckets.items():
        sections = []
        for header, items in by_header.items():
            tblocks = []
            for t, extra in items:
                tblocks.extend(_ticket_block(t, extra))
            sections.append((header, tblocks))
        out[ch] = sections
    return out


def _ticket_block(t: Ticket, extra: dict | None = None) -> list[dict]:
    """Một khối ticket: tiêu đề + FactSet field:value + nút mở Jira."""
    base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    facts = [
        {"title": "Status", "value": t.status or "—"},
        {"title": "Story Point", "value": str(t.story_point) if t.story_point is not None else "—"},
        {"title": "QC PIC", "value": _mention(t.qe_pic)},
        {"title": "Assignee", "value": _mention(t.assignee, t.no_qe)},
    ]
    if t.component:
        facts.append({"title": "Component", "value": t.component})
    for k, v in (extra or {}).items():
        facts.append({"title": k, "value": v})

    blocks = [
        {"type": "TextBlock", "text": f"🔸 **{t.id}** — {t.title}",
         "weight": "Bolder", "wrap": True, "spacing": "Medium"},
        {"type": "FactSet", "facts": facts},
    ]
    if base:
        blocks.append({
            "type": "ActionSet",
            "actions": [{"type": "Action.OpenUrl", "title": "View in Jira",
                         "url": f"{base}/browse/{t.id}"}],
        })
    return blocks


def _section_header(text: str) -> dict:
    return {"type": "TextBlock", "text": text, "size": "Medium",
            "weight": "Bolder", "color": "Accent", "wrap": True,
            "separator": True, "spacing": "Large"}


def _build_card(title: str, date_str: str, sections: list[tuple[str, list[dict]]]) -> dict:
    body: list[dict] = [
        {"type": "TextBlock", "text": title, "size": "ExtraLarge",
         "weight": "Bolder", "color": "Accent", "wrap": True},
        {"type": "TextBlock", "text": f"📅 {date_str}", "isSubtle": True,
         "spacing": "None", "wrap": True},
    ]
    for header, ticket_blocks in sections:
        body.append(_section_header(header))
        body.extend(ticket_blocks)
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": body,
                    "msteams": {"width": "Full"}}}]}


def _facts_to_text(facts):
    return "\n".join(f"    - {f['title']}: {f['value']}" for f in facts)


def render_text(report: DailyReport) -> str:
    """Preview tất cả channel ra text — để test không cần Teams."""
    routed = route(report)
    out = []
    for ch in [CH_QE, CH_DEV_MS, CH_DEV_CRM]:
        out.append("=" * 55)
        out.append(f"### CHANNEL: {CHANNEL_TITLE[ch]}")
        out.append("=" * 55)
        sections = routed.get(ch)
        if not sections:
            out.append("(no messages)\n")
            continue
        for header, blocks in sections:
            out.append(f"\n[{header}]")
            for b in blocks:
                if b["type"] == "TextBlock" and b.get("weight") == "Bolder" \
                        and b["text"].startswith("🔸"):
                    out.append("  " + b["text"].replace("**", ""))
                elif b["type"] == "FactSet":
                    out.append(_facts_to_text(b["facts"]))
        out.append("")
    return "\n".join(out)


def send(report: DailyReport, dry_run: bool = False) -> None:
    routed = route(report)
    date_str = report.report_date.isoformat()

    if dry_run:
        print(render_text(report))
        return

    for ch, sections in routed.items():
        if not sections:
            continue
        url = os.getenv(CHANNEL_ENV[ch])
        if not url:
            print(f"[skip] {CHANNEL_ENV[ch]} chưa set — bỏ qua channel {ch}")
            continue
        card = _build_card(CHANNEL_TITLE[ch], date_str, sections)
        resp = requests.post(url, json=card, timeout=30)
        resp.raise_for_status()
