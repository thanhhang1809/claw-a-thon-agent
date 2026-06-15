"""
Module 1A - Jira Data Fetcher (Zalopay Jira Server / Data Center, REST API v2).
Connect Jira, filter theo roadmap (gồm bug), pull fields, normalize -> Ticket.

Set env vars:
  JIRA_BASE_URL    https://jira.zalopay.vn
  JIRA_PAT         Personal Access Token (Profile > Personal Access Tokens)
"""
from __future__ import annotations
import os
import re
import urllib3
from datetime import date, datetime
from typing import Optional
import requests
try:
    from .models import Ticket, STATUS_BLOCKED
except ImportError:  # flat execution (Docker /app)
    from models import Ticket, STATUS_BLOCKED

# Load .env từ thư mục cha (project root)
_env_file = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_env_file):
    for _ln in open(_env_file, encoding="utf-8"):
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#") and "=" in _ln:
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Jira nội bộ dùng self-signed cert → tắt SSL verify
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Normalize status name từ Jira về canonical name
_STATUS_MAP: dict[str, str] = {
    "in dev": "InDev", "in development": "InDev",
    "in test": "InTest", "in testing": "InTest",
    "in review": "InReview",
    "ready for testing": "Ready for testing",
    "in analysis": "In Analysis",
    "done": "Done", "resolved": "Resolved",
    "cancelled": "Cancelled", "canceled": "Cancelled",
    "live": "Live",
    "blocked": "Blocked", "blocked / on hold": "Blocked / On Hold", "on hold": "On Hold",
    "new": "New", "open": "Open", "backlog": "Backlog",
    "walkthrough": "Walkthrough", "reviewed": "Reviewed",
    "in progress": "In Progress",
}


def _norm_status(raw: str) -> str:
    return _STATUS_MAP.get(raw.lower().strip(), raw)

# JQL filter theo roadmap, gồm bug. Sửa cho đúng project/board của bạn.
ROADMAP_JQL = os.getenv(
    "JIRA_JQL",
    f'project = GE AND {ISSUETYPE_CLAUSE} ORDER BY created DESC'
)

# Map tên field Jira -> field của Ticket. (Zalopay Jira Server, đã xác nhận)
FIELD_MAP = {
    "story_point":        "customfield_10801",
    "sandbox_date":       "customfield_11702",
    "test_start_date":    "customfield_13703",
    "test_complete_date": "customfield_13704",
    "qe_pic":             "customfield_10418",  # QC PIC
    # Lưu ý: NoQE KHÔNG ở đây vì nó lấy từ label (xem NOQE_LABELS bên dưới),
    # không phải customfield.
}

# Field Sprint (Jira Server). TODO: điền đúng customfield ID của bạn (tìm "Sprint").
SPRINT_FIELD = os.getenv("JIRA_SPRINT_FIELD", "customfield_10000")

FETCH_FIELDS = ["summary", "status", "assignee", "issuetype", "labels",
                "components", SPRINT_FIELD] + list(FIELD_MAP.values())


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['JIRA_PAT']}",
            "Accept": "application/json"}


def _base() -> str:
    return os.environ["JIRA_BASE_URL"].rstrip("/")


def _search(jql: str) -> list[Ticket]:
    """Chạy JQL, phân trang, trả list Ticket."""
    tickets: list[Ticket] = []
    start_at, page = 0, 50
    while True:
        resp = requests.get(
            f"{_base()}/rest/api/2/search",
            params={"jql": jql, "startAt": start_at,
                    "maxResults": page, "fields": ",".join(FETCH_FIELDS)},
            headers=_headers(), timeout=30, verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        for issue in data.get("issues", []):
            tickets.append(_normalize(issue))
        start_at += page
        if start_at >= data.get("total", 0):
            break
    return tickets


def _key_from_link(s: str) -> str:
    """Lấy ticket key từ link hoặc trả nguyên nếu đã là key.
    'https://jira.zalopay.vn/browse/GE-14209' -> 'GE-14209'
    'GE-14209' -> 'GE-14209'"""
    s = s.strip()
    m = re.search(r"/browse/([A-Z][A-Z0-9]+-\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", s)
    return m.group(1) if m else s


# ---------------------------------------------------------------------------
# 3 cách input
# ---------------------------------------------------------------------------
def fetch_tickets() -> list[Ticket]:
    """Mặc định: theo JQL roadmap (gồm bug)."""
    return _search(ROADMAP_JQL)


def fetch_by_keys(keys_or_links: list[str]) -> list[Ticket]:
    """Theo danh sách link/key cụ thể."""
    keys = [_key_from_link(k) for k in keys_or_links if k.strip()]
    if not keys:
        return []
    jql = f"key in ({','.join(keys)})"
    return _search(jql)


def fetch_by_sprint(sprint: str | None = None, team: str | None = None) -> list[Ticket]:
    """Theo sprint. sprint=None -> openSprints() (sprint đang mở).
    sprint='Sprint 12' -> theo tên. sprint='123' -> theo sprint id.
    team='MS'|'CRM' -> chỉ giữ ticket thuộc sprint có tiền tố team đó."""
    if sprint is None:
        clause = "sprint in openSprints()"
    elif sprint.isdigit():
        clause = f"sprint = {sprint}"
    else:
        clause = f'sprint = "{sprint}"'
    jql = f"{clause} AND {ISSUETYPE_CLAUSE} ORDER BY created DESC"
    tickets = _search(jql)
    if team:
        try:
            from .models import ticket_in_team_sprint
        except ImportError:
            from models import ticket_in_team_sprint
        tickets = [t for t in tickets if ticket_in_team_sprint(t.sprints, team)]
    return tickets


def list_active_sprints(board_id: str | int) -> list[dict]:
    """Liệt kê sprint đang active của 1 board (Jira Agile API).
    Trả [{id, name, state, startDate, endDate}, ...]."""
    resp = requests.get(
        f"{_base()}/rest/agile/1.0/board/{board_id}/sprint",
        params={"state": "active"}, headers=_headers(), timeout=30, verify=False,
    )
    resp.raise_for_status()
    out = []
    for s in resp.json().get("values", []):
        out.append({k: s.get(k) for k in
                    ("id", "name", "state", "startDate", "endDate")})
    return out


def current_sprint_name() -> Optional[str]:
    """Tên sprint active đầu tiên của board (cần env JIRA_BOARD_ID)."""
    board = os.getenv("JIRA_BOARD_ID")
    if not board:
        return None
    sprints = list_active_sprints(board)
    return sprints[0]["name"] if sprints else None


def list_boards_for_project(project_key: str) -> list[dict]:
    """Lấy các board (Agile) của 1 project."""
    boards, start = [], 0
    while True:
        resp = requests.get(
            f"{_base()}/rest/agile/1.0/board",
            params={"projectKeyOrId": project_key, "startAt": start, "maxResults": 50},
            headers=_headers(), timeout=30, verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        boards += [{"id": b["id"], "name": b.get("name")} for b in data.get("values", [])]
        if data.get("isLast", True):
            break
        start += 50
    return boards


def active_sprint_ids_for_project(project_key: str) -> list[int]:
    """Gom id của TẤT CẢ active sprint trên các board của project."""
    ids = []
    for b in list_boards_for_project(project_key):
        try:
            for s in list_active_sprints(b["id"]):
                if s["id"] not in ids:
                    ids.append(s["id"])
        except requests.HTTPError:
            continue  # board scrum mới có sprint; kanban thì bỏ qua
    return ids


def fetch_active_sprint(project_key: str, team: str | None = None) -> list[Ticket]:
    """Chỉ lấy ticket trong các ACTIVE sprint của project (loại future/closed).
    team='MS'|'CRM' -> lọc thêm theo tiền tố tên sprint."""
    ids = active_sprint_ids_for_project(project_key)
    if not ids:
        return []
    id_list = ",".join(str(i) for i in ids)
    jql = (f"project = {project_key} AND sprint in ({id_list}) "
           f"AND (issuetype in (Story, Task) OR "
           f"(issuetype = Bug AND (cf[13800] = \"Production\" OR labels = \"BUG_LEAK\"))) "
           f"ORDER BY created DESC")
    tickets = _search(jql)
    if team:
        try:
            from .models import ticket_in_team_sprint
        except ImportError:
            from models import ticket_in_team_sprint
        tickets = [t for t in tickets if ticket_in_team_sprint(t.sprints, team)]
    return tickets


def _parse_sprints(v) -> list[str]:
    """Field sprint (Jira Server) thường là list chuỗi:
    'com...Sprint@xx[id=4293,rapidViewId=..,state=ACTIVE,name=MS - Sprint 26.04.A,...]'
    hoặc list dict {'name': ...}. Bóc ra danh sách tên sprint."""
    if not v:
        return []
    items = v if isinstance(v, list) else [v]
    names = []
    for it in items:
        if isinstance(it, dict):
            if it.get("name"):
                names.append(it["name"])
        else:
            m = re.search(r"name=([^,\]]+)", str(it))
            if m:
                names.append(m.group(1).strip())
    return names


def _parse_date(v) -> Optional[date]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _user(v):
    """single-user-picker: trả (displayName, username). Có thể là dict hoặc str."""
    if isinstance(v, dict):
        return v.get("displayName") or v.get("name"), v.get("name")
    return v, v


# Label đánh dấu NoQE (khớp nhiều biến thể, so sánh lower-case).
NOQE_LABELS = {"noqe", "no_qe", "noqc", "no_qc"}

# Username QE team — dùng để fallback qe_pic từ label khi QC PIC field trống.
QE_USERNAMES = {"hangdtt4", "annhg", "anhldh", "quantm6"}


def _normalize(issue: dict) -> Ticket:
    f = issue["fields"]
    def cf(key):
        return f.get(FIELD_MAP[key]) if key in FIELD_MAP else None
    status_name = _norm_status((f.get("status") or {}).get("name", ""))
    assignee_name, assignee_user = _user(f.get("assignee"))
    qe_name, qe_user = _user(cf("qe_pic"))
    labels = [l.lower() for l in (f.get("labels", []) or [])]

    # Fallback: nếu QC PIC field trống, lấy tất cả username khớp từ label
    if not qe_name:
        matched = [lbl for lbl in labels if lbl in QE_USERNAMES]
        if matched:
            qe_name = ", ".join(matched)
            qe_user = matched[0]

    comps = f.get("components", []) or []
    component = comps[0].get("name") if comps else None
    return Ticket(
        id=issue["key"],
        title=f.get("summary", ""),
        status=status_name,
        story_point=cf("story_point"),
        test_start_date=_parse_date(cf("test_start_date")),
        test_complete_date=_parse_date(cf("test_complete_date")),
        sandbox_date=_parse_date(cf("sandbox_date")),
        assignee=assignee_name or "unassigned",
        assignee_username=assignee_user,
        qe_pic=qe_name,
        qe_pic_username=qe_user,
        no_qe=bool(NOQE_LABELS & set(labels)),
        blocked=status_name.lower() in STATUS_BLOCKED,
        is_bug=("bug" in (f.get("issuetype") or {}).get("name", "").lower()
                or "defect" in (f.get("issuetype") or {}).get("name", "").lower()),
        component=component,
        sprints=_parse_sprints(f.get(SPRINT_FIELD)),
    )
