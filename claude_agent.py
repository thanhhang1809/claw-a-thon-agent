#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QA Watchdog — Claude SDK Agent

Agent nhận JQL (hoặc câu hỏi tự nhiên), gọi tools để fetch tickets & chạy
rule engine, rồi phân tích kết quả bằng ngôn ngữ tự nhiên.

Chế độ fetch (ưu tiên theo thứ tự):
  1. --snapshot FILE  đọc snapshot JSON (offline demo)
  2. --live           gọi Jira REST (cần JIRA_BASE_URL + JIRA_PAT)
  3. Mặc định: tự detect file jql_snapshot.json nếu có

Ví dụ:
  python3 claude_agent.py
  python3 claude_agent.py --snapshot jql_snapshot.json
  python3 claude_agent.py --live
  python3 claude_agent.py --jql 'project = PC AND status not in (DONE,Cancelled)'
  python3 claude_agent.py --message 'Tickets nào đang quá hạn test?'
"""
import argparse, json, sys, os
from datetime import date
from typing import Optional

# Auto-load .env nếu có (không bắt buộc phải install python-dotenv)
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import anthropic

from jira_adapter import normalize_issue, fetch_tickets, SCAN_JQL
from engine import RuleEngine

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Bạn là QA Watchdog Agent của team ZaloPay — chuyên phân tích QA process, phát hiện vi phạm, và sinh test checklist ISTQB.

## Năng lực 1: QA Watchdog (phân tích vi phạm sprint)
Khi user hỏi về trạng thái QA hoặc cung cấp JQL:
1. Dùng `search_jira` để lấy danh sách tickets
2. Dùng `run_rule_engine` để đánh giá vi phạm
3. Tổng hợp kết quả rõ ràng bằng tiếng Việt

Mức vi phạm:
  ⚪ Level 0 — Data hygiene: thiếu field
  🔴 Level 1 — Violent: vi phạm nghiêm trọng, cần xử lý ngay
  🟠 Level 2 — Risk: nguy cơ trễ deadline
  🟡 Level 3 — Watch: theo dõi sát

Sau khi có kết quả:
- Tóm tắt tổng quan (số ticket, số vi phạm, level nghiêm trọng nhất)
- Liệt kê từng vi phạm kèm khuyến nghị hành động cụ thể
- Chỉ rõ ai cần được thông báo (assignee / qe_pic)
- Nếu có escalation thì đề xuất escalate lên ai

## Năng lực 2: TEP + ISTQB Test Checklist
Khi user yêu cầu "sinh test checklist", "phân tích TEP", hoặc "tạo test plan" cho 1 ticket:
1. Dùng `get_ticket_detail` để lấy đầy đủ thông tin ticket
2. Phân tích 4 chiều complexity (Story point, Requirement, Impact breadth, Testing technique) — mỗi chiều cho điểm 1-3
3. Tính TEP: trung bình complexity → TEP 1/2/3/5/8
4. Chọn kỹ thuật ISTQB phù hợp (EP, BVA, Decision Table, State Transition, API Testing, Regression...)
5. Sinh test checklist đầy đủ theo format: Scenario ID | Test Scenario | Risk Priority | Test Steps | Expected Result
6. Kết thúc bằng bảng tổng hợp Part 1 (Feature Testing) và Part 2 (Regression Testing) + coverage stats

Luôn trả lời bằng tiếng Việt trừ khi user yêu cầu tiếng Anh."""

TOOLS = [
    {
        "name": "search_jira",
        "description": (
            "Tìm kiếm tickets từ Jira bằng JQL. "
            "Trả về list ticket đã normalize (key, status, story_point, test dates, assignee, qe_pic). "
            "Dùng snapshot_file để test offline thay vì gọi Jira thật."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {
                    "type": "string",
                    "description": (
                        "JQL query. Ví dụ: 'project = PC AND status not in (DONE, Cancelled)'. "
                        "Bỏ qua nếu snapshot_file được truyền."
                    ),
                },
                "snapshot_file": {
                    "type": "string",
                    "description": "Đường dẫn file snapshot JSON (tuỳ chọn, ưu tiên hơn JQL live).",
                },
            },
            "required": ["jql"],
        },
    },
    {
        "name": "run_rule_engine",
        "description": (
            "Chạy rule engine QA Watchdog trên danh sách tickets. "
            "Phát hiện vi phạm level 0-3 và dự báo quá tải (predictive). "
            "Lưu vi phạm vào DB, auto-resolve ticket đã hết vi phạm, escalate repeat offender."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickets": {
                    "type": "array",
                    "description": "Danh sách ticket objects — output từ search_jira.",
                    "items": {"type": "object"},
                },
                "scan_date": {
                    "type": "string",
                    "description": "Ngày scan yyyy-mm-dd. Mặc định hôm nay.",
                },
            },
            "required": ["tickets"],
        },
    },
    {
        "name": "get_ticket_history",
        "description": "Xem lịch sử vi phạm của 1 ticket trong DB (đã từng bị flag rule nào, bao nhiêu lần).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_key": {
                    "type": "string",
                    "description": "Jira ticket key. Ví dụ: PC-870",
                },
            },
            "required": ["ticket_key"],
        },
    },
    {
        "name": "get_ticket_detail",
        "description": (
            "Lấy đầy đủ thông tin 1 ticket Jira (summary, description, story points, "
            "issuelinks, comments, components, labels, priority) để phân tích TEP "
            "và sinh test checklist ISTQB. "
            "Dùng snapshot_file để test offline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_key": {
                    "type": "string",
                    "description": "Jira ticket key. Ví dụ: GE-14209",
                },
                "snapshot_file": {
                    "type": "string",
                    "description": "File snapshot JSON (tuỳ chọn, dùng khi offline).",
                },
            },
            "required": ["ticket_key"],
        },
    },
]

# ---------------------------------------------------------------- tool impls
_engine: Optional[RuleEngine] = None


def _get_engine() -> RuleEngine:
    global _engine
    if _engine is None:
        _engine = RuleEngine("rules.yaml", db_path="watchdog.db")
    return _engine


def _tool_search_jira(jql: str, snapshot_file: Optional[str] = None) -> list:
    if snapshot_file:
        snap = json.load(open(snapshot_file, encoding="utf-8"))
        issues = snap["issues"] if isinstance(snap, dict) else snap
        print(f"  [search_jira] snapshot {snapshot_file} — {len(issues)} issues", file=sys.stderr)
        return [normalize_issue(i) for i in issues]
    print(f"  [search_jira] live JQL: {jql}", file=sys.stderr)
    return fetch_tickets(jql)


def _tool_run_rule_engine(tickets: list, scan_date: Optional[str] = None) -> dict:
    scan_date = scan_date or date.today().isoformat()
    print(f"  [run_rule_engine] {len(tickets)} tickets, date={scan_date}", file=sys.stderr)
    return _get_engine().scan(scan_date, tickets)


def _tool_get_ticket_history(ticket_key: str) -> list:
    rows = _get_engine().ticket_history(ticket_key)
    return [
        {
            "rule_id": r[0], "level": r[1], "count": r[2],
            "first": r[3], "last": r[4], "open": r[5],
        }
        for r in rows
    ]


def _tool_get_ticket_detail(ticket_key: str, snapshot_file: Optional[str] = None) -> dict:
    """Return full ticket details for TEP / ISTQB checklist generation."""
    if snapshot_file:
        snap = json.load(open(snapshot_file, encoding="utf-8"))
        issues = snap["issues"] if isinstance(snap, dict) else snap
        for raw in issues:
            key = raw.get("key", "")
            if key == ticket_key:
                fields = raw.get("fields", {})
                return {
                    "key": key,
                    "summary": fields.get("summary", ""),
                    "description": fields.get("description", ""),
                    "issuetype": (fields.get("issuetype") or {}).get("name", ""),
                    "priority": (fields.get("priority") or {}).get("name", ""),
                    "story_points": fields.get("customfield_10801"),
                    "status": (fields.get("status") or {}).get("name", ""),
                    "labels": fields.get("labels", []),
                    "components": [c.get("name") for c in fields.get("components", [])],
                    "issuelinks": [
                        {
                            "type": (lnk.get("type") or {}).get("name", ""),
                            "inward": lnk.get("inwardIssue", {}).get("key") if lnk.get("inwardIssue") else None,
                            "outward": lnk.get("outwardIssue", {}).get("key") if lnk.get("outwardIssue") else None,
                        }
                        for lnk in fields.get("issuelinks", [])
                    ],
                    "comments": [
                        {
                            "author": (c.get("author") or {}).get("displayName", ""),
                            "body": c.get("body", ""),
                            "created": c.get("created", ""),
                        }
                        for c in (fields.get("comment") or {}).get("comments", [])
                    ],
                }
        return {"error": f"Ticket {ticket_key} not found in snapshot"}

    # Live Jira fetch
    tickets = fetch_tickets(f"key = {ticket_key}")
    if not tickets:
        return {"error": f"Ticket {ticket_key} not found"}
    t = tickets[0]
    return {
        "key": t.get("key", ticket_key),
        "summary": t.get("summary", ""),
        "description": t.get("description", ""),
        "story_points": t.get("story_point"),
        "status": t.get("status", ""),
        "assignee": t.get("assignee", ""),
        "qe_pic": t.get("qe_pic", ""),
    }


def _dispatch(name: str, inp: dict):
    if name == "search_jira":
        return _tool_search_jira(**inp)
    if name == "run_rule_engine":
        return _tool_run_rule_engine(**inp)
    if name == "get_ticket_history":
        return _tool_get_ticket_history(**inp)
    if name == "get_ticket_detail":
        return _tool_get_ticket_detail(**inp)
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------- agent loop
def run_agent(user_message: str, *, verbose: bool = True) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_message}]
    final_text = ""

    while True:
        # Streaming: text appears token-by-token; tool_use blocks accumulate silently
        collected_text = []
        collected_content = []  # full content blocks for the assistant turn

        with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            for event in stream:
                # Stream text tokens live
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    if verbose:
                        print(event.delta.text, end="", flush=True)
                    collected_text.append(event.delta.text)

            # After stream ends, get the full response for tool use handling
            response = stream.get_final_message()

        if collected_text and verbose:
            print()  # newline after streamed text
        final_text = "".join(collected_text)

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tu in tool_uses:
                short = json.dumps(tu.input, ensure_ascii=False)[:80]
                if verbose:
                    print(f"\n⚙️  {tu.name}({short}…)", flush=True)
                result = _dispatch(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
            messages.append({"role": "user", "content": tool_results})
            if verbose:
                print()  # blank line before next Claude response
        else:
            break

    return final_text


# ---------------------------------------------------------------- REPL / single-shot
def _build_user_message(args) -> str:
    parts = []

    if args.jql:
        parts.append(f"Hãy fetch tickets với JQL sau và chạy rule engine đánh giá:\nJQL: {args.jql}")
    elif args.snapshot:
        parts.append(
            f"Hãy fetch tickets từ snapshot file '{args.snapshot}' và chạy rule engine đánh giá."
        )

    if args.date:
        parts.append(f"Ngày scan: {args.date}")

    if args.message:
        parts.append(args.message)

    if not parts:
        # Gợi ý mặc định
        if os.path.exists("jql_snapshot.json"):
            parts.append(
                "Hãy fetch tickets từ snapshot file 'jql_snapshot.json' "
                f"và chạy rule engine đánh giá cho ngày {date.today().isoformat()}."
            )
        else:
            parts.append(
                f"Hãy dùng JQL mặc định '{SCAN_JQL}' "
                f"và chạy rule engine đánh giá cho ngày {date.today().isoformat()}."
            )

    return "\n".join(parts)


def _inject_snapshot_hint(msg: str, snapshot: Optional[str]) -> str:
    """Nếu user không đề cập snapshot_file, tự inject vào tool call qua prompt."""
    if snapshot and "snapshot_file" not in msg:
        return msg + f"\n\n(Dùng snapshot_file='{snapshot}' khi gọi search_jira)"
    return msg


def main():
    ap = argparse.ArgumentParser(description="QA Watchdog — Claude SDK Agent")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--live", action="store_true", help="Fetch Jira REST thật")
    src.add_argument("--snapshot", metavar="FILE", help="Đọc từ snapshot JSON")
    ap.add_argument("--jql", help="JQL (dùng với --live hoặc để agent tự chọn)")
    ap.add_argument("--date", default=date.today().isoformat(), help="Ngày scan yyyy-mm-dd")
    ap.add_argument("--message", "-m", help="Câu hỏi tuỳ chỉnh gửi cho agent")
    ap.add_argument("--repl", action="store_true", help="Chế độ chat nhiều lượt")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY chưa được set.\n"
            "  Cách 1: export ANTHROPIC_API_KEY=sk-ant-...\n"
            "  Cách 2: tạo file .env trong thư mục này với dòng: ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.repl:
        print("QA Watchdog Agent — REPL mode (Ctrl+C hoặc 'quit' để thoát)\n")
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if user_input.lower() in ("quit", "exit", "q"):
                break
            if not user_input:
                continue
            msg = _inject_snapshot_hint(user_input, args.snapshot)
            print()
            run_agent(msg)
            print()
    else:
        msg = _build_user_message(args)
        msg = _inject_snapshot_hint(msg, args.snapshot)
        print(f"[agent] User message:\n{msg}\n", file=sys.stderr)
        run_agent(msg)


if __name__ == "__main__":
    main()
