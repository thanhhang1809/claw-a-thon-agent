#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QE Watchdog — GreenNode LLM Agent (OpenAI-compatible)

Model strategy:
  AGENT_MODEL    minimax/minimax-m2.5          — tool use / QE analysis (paid, agentic)
  CHECKLIST_MODEL deepseek/deepseek-reasoner   — TEP + ISTQB checklist (free, reasoning)
  FALLBACK_MODEL  qwen/qwen3-235b-a22b-instruct-2507  — free 235B, unlimited

Endpoint: https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1

Ví dụ (chạy từ thư mục qe_agent/):
  python3 claude_agent.py
  python3 claude_agent.py --snapshot ge_sprint_snapshot.json
  python3 claude_agent.py --snapshot ge_sprint_snapshot.json --repl
  python3 claude_agent.py -m "Tickets nào đang vi phạm Level 1?"

Hoặc từ thư mục gốc:
  python3 -m qe_agent.claude_agent --snapshot qe_agent/ge_sprint_snapshot.json
"""
import argparse, json, sys, os
from datetime import date
from typing import Optional

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Auto-load .env: ưu tiên thư mục cha (project root), fallback cùng thư mục
for _env_candidate in [
    os.path.join(_SCRIPT_DIR, "..", ".env"),
    os.path.join(_SCRIPT_DIR, ".env"),
]:
    if os.path.exists(_env_candidate):
        for _line in open(_env_candidate, encoding="utf-8"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
        break

from openai import OpenAI

# Import package modules
try:
    from .jira_adapter import normalize_issue, fetch_tickets, SCAN_JQL
    from .rule_engine import RuleEngine
except ImportError:
    sys.path.insert(0, _SCRIPT_DIR)
    from jira_adapter import normalize_issue, fetch_tickets, SCAN_JQL
    from rule_engine import RuleEngine

# ---------------------------------------------------------------- models
GREENNODE_BASE_URL = os.environ.get(
    "GREENNODE_LLM_BASE_URL",
    "https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1",
)
GREENNODE_API_KEY = os.environ.get("GREENNODE_API_KEY", "")

# Task-specific models
AGENT_MODEL     = "minimax/minimax-m2.5"                   # tool use, agentic — best quality
CHECKLIST_MODEL = "deepseek/deepseek-reasoner"             # TEP + ISTQB — free reasoning
FALLBACK_MODEL  = "qwen/qwen3-235b-a22b-instruct-2507"     # free 235B — unlimited fallback

_RULES_YAML = os.path.join(_SCRIPT_DIR, "rules.yaml")
_WATCHDOG_DB = os.path.join(_SCRIPT_DIR, "watchdog.db")

# ---------------------------------------------------------------- system prompts
SYSTEM_PROMPT_AGENT = """Bạn là QE Watchdog Agent của team Zalopay — chuyên phân tích QE process và phát hiện vi phạm.

Khi user hỏi về trạng thái QE hoặc cung cấp JQL:
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

Luôn trả lời bằng tiếng Việt trừ khi user yêu cầu tiếng Anh."""

SYSTEM_PROMPT_CHECKLIST = """Bạn là QE Watchdog Agent của team Zalopay — chuyên sinh test checklist ISTQB chất lượng cao.

Khi user yêu cầu sinh test checklist / phân tích TEP / tạo test plan cho 1 ticket:
1. Dùng `get_ticket_detail` để lấy đầy đủ thông tin ticket
2. Phân tích 4 chiều complexity (Story point, Requirement, Impact breadth, Testing technique) — mỗi chiều cho điểm 1-3
3. Tính TEP: trung bình complexity → TEP 1/2/3/5/8
4. Chọn kỹ thuật ISTQB phù hợp (EP, BVA, Decision Table, State Transition, API Testing, Regression...)
5. Sinh test checklist đầy đủ theo format: Scenario ID | Test Scenario | Risk Priority | Test Steps | Expected Result
6. Kết thúc bằng bảng tổng hợp Part 1 (Feature Testing) và Part 2 (Regression Testing) + coverage stats

Luôn trả lời bằng tiếng Việt trừ khi user yêu cầu tiếng Anh."""

# ---------------------------------------------------------------- tools (OpenAI format)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_jira",
            "description": (
                "Tìm kiếm tickets từ Jira bằng JQL. "
                "Trả về list ticket đã normalize (key, status, story_point, test dates, assignee, qe_pic). "
                "Dùng snapshot_file để test offline thay vì gọi Jira thật."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "jql": {
                        "type": "string",
                        "description": "JQL query. Ví dụ: 'project = GE AND status not in (DONE, Cancelled)'.",
                    },
                    "snapshot_file": {
                        "type": "string",
                        "description": "Đường dẫn file snapshot JSON (tuỳ chọn, ưu tiên hơn JQL live).",
                    },
                },
                "required": ["jql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_rule_engine",
            "description": (
                "Chạy rule engine QE Watchdog trên danh sách tickets. "
                "Phát hiện vi phạm level 0-3. "
                "Lưu vi phạm vào DB, auto-resolve ticket đã hết vi phạm, escalate repeat offender."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket_history",
            "description": "Xem lịch sử vi phạm của 1 ticket trong DB (đã từng bị flag rule nào, bao nhiêu lần).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_key": {
                        "type": "string",
                        "description": "Jira ticket key. Ví dụ: GE-14209",
                    },
                },
                "required": ["ticket_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket_detail",
            "description": (
                "Lấy đầy đủ thông tin 1 ticket Jira (summary, description, story points, "
                "issuelinks, comments, components, labels, priority) để phân tích TEP "
                "và sinh test checklist ISTQB. Dùng snapshot_file để test offline."
            ),
            "parameters": {
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
    },
]

# ---------------------------------------------------------------- tool implementations
_engine: Optional[RuleEngine] = None


def _get_engine() -> RuleEngine:
    global _engine
    if _engine is None:
        _engine = RuleEngine(_RULES_YAML, db_path=_WATCHDOG_DB)
    return _engine


def _tool_search_jira(jql: str, snapshot_file: Optional[str] = None) -> list:
    if snapshot_file:
        if not os.path.isabs(snapshot_file):
            snapshot_file = os.path.join(_SCRIPT_DIR, snapshot_file)
        snap = json.load(open(snapshot_file, encoding="utf-8"))
        issues = snap["issues"] if isinstance(snap, dict) else snap
        print(f"  [search_jira] snapshot ({len(issues)} issues)", file=sys.stderr)
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
        {"rule_id": r[0], "level": r[1], "count": r[2],
         "first": r[3], "last": r[4], "open": r[5]}
        for r in rows
    ]


def _tool_get_ticket_detail(ticket_key: str, snapshot_file: Optional[str] = None) -> dict:
    if snapshot_file:
        if not os.path.isabs(snapshot_file):
            snapshot_file = os.path.join(_SCRIPT_DIR, snapshot_file)
        snap = json.load(open(snapshot_file, encoding="utf-8"))
        issues = snap["issues"] if isinstance(snap, dict) else snap
        for raw in issues:
            if raw.get("key") == ticket_key:
                fields = raw.get("fields", {})
                return {
                    "key": ticket_key,
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
    tickets = fetch_tickets(f"key = {ticket_key}")
    if not tickets:
        return {"error": f"Ticket {ticket_key} not found"}
    t = tickets[0]
    return {"key": t.get("key", ticket_key), "summary": t.get("summary", ""),
            "story_points": t.get("story_point"), "status": t.get("status", "")}


def _dispatch(name: str, inp: dict):
    if name == "search_jira":     return _tool_search_jira(**inp)
    if name == "run_rule_engine": return _tool_run_rule_engine(**inp)
    if name == "get_ticket_history": return _tool_get_ticket_history(**inp)
    if name == "get_ticket_detail":  return _tool_get_ticket_detail(**inp)
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------- detect task type
def _is_checklist_request(msg: str) -> bool:
    keywords = ["checklist", "tep", "test plan", "test checklist", "istqb",
                "phân tích ticket", "sinh test", "tạo test", "test scenario"]
    msg_lower = msg.lower()
    return any(k in msg_lower for k in keywords)


# ---------------------------------------------------------------- agent loop (OpenAI SDK)
# Friendly progress labels for the streaming UI
TOOL_LABELS = {
    "search_jira":        "🔍 Đang tìm tickets từ Jira…",
    "run_rule_engine":    "⚙️ Đang đánh giá vi phạm QE…",
    "get_ticket_history": "📜 Đang xem lịch sử ticket…",
    "get_ticket_detail":  "📄 Đang lấy chi tiết ticket…",
}


def run_agent_stream(user_message: str, *, verbose: bool = False):
    """Generator phát sự kiện cho UI streaming:
      {"type":"status","msg":...}  — đang chạy tool (tiến trình)
      {"type":"clear"}             — xoá text tạm (thinking) trước khi gọi tool
      {"type":"delta","text":...}  — token text mới (stream dần)
      {"type":"done","text":...}   — câu trả lời cuối cùng đầy đủ
    """
    if not GREENNODE_API_KEY:
        yield {"type": "done", "text": "ERROR: GREENNODE_API_KEY chưa được set."}
        return

    client = OpenAI(api_key=GREENNODE_API_KEY, base_url=GREENNODE_BASE_URL)

    is_checklist = _is_checklist_request(user_message)
    primary_model = CHECKLIST_MODEL if is_checklist else AGENT_MODEL
    system_prompt = SYSTEM_PROMPT_CHECKLIST if is_checklist else SYSTEM_PROMPT_AGENT

    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    messages = list(base_messages)
    final_text = ""
    MAX_TURNS = 8
    MAX_EMPTY = 2

    for attempt in range(2):  # fallback model on empty/failure
        model = primary_model if attempt == 0 else FALLBACK_MODEL
        try:
            turns = 0
            empty_streak = 0
            while turns < MAX_TURNS:
                turns += 1
                stream = client.chat.completions.create(
                    model=model, messages=messages, tools=TOOLS,
                    tool_choice="auto", stream=True,
                )

                collected_text = []
                tool_calls_map = {}
                finish_reason = "stop"

                for chunk in stream:
                    if not chunk.choices:
                        continue
                    fr = chunk.choices[0].finish_reason
                    if fr:
                        finish_reason = fr
                    delta = chunk.choices[0].delta
                    if not delta:
                        continue
                    if delta.content:
                        if verbose:
                            print(delta.content, end="", flush=True)
                        collected_text.append(delta.content)
                        yield {"type": "delta", "text": delta.content}
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {"id": "", "name": "", "arguments": ""}
                            if tc.id:
                                tool_calls_map[idx]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls_map[idx]["name"] = tc.function.name
                                if tc.function.arguments:
                                    tool_calls_map[idx]["arguments"] += tc.function.arguments

                if collected_text:
                    final_text = "".join(collected_text)

                # 1) Có tool calls → LUÔN thực thi (bất kể finish_reason)
                if tool_calls_map:
                    empty_streak = 0
                    # text vừa stream là "thinking" tạm → báo UI xoá đi
                    if collected_text:
                        yield {"type": "clear"}
                        final_text = ""
                    tool_calls_list = []
                    for idx in sorted(tool_calls_map.keys()):
                        tc = tool_calls_map[idx]
                        tool_calls_list.append({
                            "id": tc["id"], "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        })
                    messages.append({
                        "role": "assistant",
                        "content": "".join(collected_text) or None,
                        "tool_calls": tool_calls_list,
                    })
                    for tc in tool_calls_list:
                        name = tc["function"]["name"]
                        yield {"type": "status", "msg": TOOL_LABELS.get(name, f"⚙️ {name}…")}
                        try:
                            inp = json.loads(tc["function"]["arguments"])
                        except json.JSONDecodeError:
                            inp = {}
                        if verbose:
                            print(f"\n⚙️  {name}({json.dumps(inp, ensure_ascii=False)[:80]}…)", flush=True)
                        result = _dispatch(name, inp)
                        messages.append({
                            "role": "tool", "tool_call_id": tc["id"],
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        })
                    continue

                # 2) Không tool, có text → xong
                if collected_text:
                    break

                # 3) Turn rỗng — stream hiccup, thử lại vài lần
                empty_streak += 1
                if empty_streak >= MAX_EMPTY:
                    break

            if final_text.strip():
                break
            messages = list(base_messages)  # empty → thử fallback

        except Exception as e:
            print(f"\n[agent] {model} failed: {e}", file=sys.stderr)
            yield {"type": "clear"}
            final_text = ""
            messages = list(base_messages)

    if not final_text.strip():
        final_text = ("Xin lỗi, mình chưa tạo được phản hồi cho yêu cầu này "
                      "(model trả rỗng). Bạn thử lại hoặc diễn đạt rõ hơn giúp mình nhé.")
        yield {"type": "delta", "text": final_text}
    yield {"type": "done", "text": final_text}


def run_agent(user_message: str, *, verbose: bool = True,
              snapshot: Optional[str] = None) -> str:
    """Non-streaming wrapper — consumes the event stream, returns final text.
    Dùng cho /invocations, scheduled jobs, insights, CLI."""
    final_text = ""
    for ev in run_agent_stream(user_message, verbose=verbose):
        if ev["type"] == "done":
            final_text = ev["text"]
    return final_text


# ---------------------------------------------------------------- CLI helpers
def _build_user_message(args) -> str:
    scan_date = args.date or date.today().isoformat()
    if args.message:
        return args.message
    if args.jql:
        return (
            f"Gọi tool search_jira với jql='{args.jql}' để lấy danh sách tickets, "
            f"sau đó gọi run_rule_engine để đánh giá vi phạm QE cho ngày {scan_date}. "
            f"Tổng hợp kết quả rõ ràng bằng tiếng Việt."
        )
    if args.snapshot:
        return (
            f"Gọi tool search_jira với jql='project=GE' và snapshot_file='{args.snapshot}' "
            f"để lấy danh sách tickets, sau đó gọi run_rule_engine để đánh giá vi phạm QE "
            f"cho ngày {scan_date}. Tổng hợp kết quả rõ ràng bằng tiếng Việt."
        )
    default_snap = os.path.join(_SCRIPT_DIR, "ge_sprint_snapshot.json")
    if os.path.exists(default_snap):
        return (
            f"Gọi tool search_jira với jql='project=GE' và snapshot_file='{default_snap}' "
            f"để lấy danh sách tickets, sau đó gọi run_rule_engine để đánh giá vi phạm QE "
            f"cho ngày {scan_date}. Tổng hợp kết quả rõ ràng bằng tiếng Việt."
        )
    return (
        f"Gọi tool search_jira với jql='{SCAN_JQL}' để lấy danh sách tickets, "
        f"sau đó gọi run_rule_engine để đánh giá vi phạm QE cho ngày {scan_date}. "
        f"Tổng hợp kết quả rõ ràng bằng tiếng Việt."
    )


def _inject_snapshot_hint(msg: str, snapshot: Optional[str]) -> str:
    if snapshot and "snapshot_file" not in msg:
        return msg + f"\n\n(Dùng snapshot_file='{snapshot}' khi gọi search_jira và get_ticket_detail)"
    return msg


def main():
    ap = argparse.ArgumentParser(description="QE Watchdog — GreenNode LLM Agent")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--live", action="store_true", help="Fetch Jira REST thật")
    src.add_argument("--snapshot", metavar="FILE", help="Đọc từ snapshot JSON")
    ap.add_argument("--jql", help="JQL query")
    ap.add_argument("--date", default=date.today().isoformat(), help="Ngày scan yyyy-mm-dd")
    ap.add_argument("--message", "-m", help="Câu hỏi tuỳ chỉnh gửi cho agent")
    ap.add_argument("--repl", action="store_true", help="Chế độ chat nhiều lượt")
    args = ap.parse_args()

    # Resolve snapshot to absolute path so the tool can find it regardless of CWD
    if args.snapshot and not os.path.isabs(args.snapshot):
        args.snapshot = os.path.abspath(args.snapshot)

    if not GREENNODE_API_KEY:
        print(
            "ERROR: GREENNODE_API_KEY chưa được set.\n"
            "  Thêm vào .env: GREENNODE_API_KEY=vn-...",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.repl:
        print("QE Watchdog Agent — REPL mode (Ctrl+C hoặc 'quit' để thoát)")
        print(f"Models: agent={AGENT_MODEL} | checklist={CHECKLIST_MODEL} | fallback={FALLBACK_MODEL}\n")
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
        print(f"[agent] {msg[:120]}…\n", file=sys.stderr)
        run_agent(msg)


if __name__ == "__main__":
    main()
