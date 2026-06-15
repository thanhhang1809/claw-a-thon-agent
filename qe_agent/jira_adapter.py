# -*- coding: utf-8 -*-
"""
Jira Adapter — map field THẬT của project PC (jira.zalopay.vn) sang schema rule engine.

Field mapping (đã verify trên data thật project PC ngày 2026-06-12):
    story_point        <- customfield_10801  "Story Points"        (number)
    test_start_date    <- customfield_13703  "Test Start Date"     (date yyyy-mm-dd)
    test_complete_date <- customfield_13704  "Test Complete Date"  (date)
    sandbox_date       <- customfield_11702  "Sandbox Date"        (date)
    qe_pic             <- customfield_10418  "QC PIC" (user picker, field THẬT)
                          fallback -> labels khi QC PIC trống
    assignee           <- assignee.name      (username, vd "chaubtm")
    squad              <- xem ghi chú SQUAD bên dưới

SQUAD — LƯU Ý QUAN TRỌNG:
    Project PC KHÔNG có field 'components' (đã verify). Các field domain:
        customfield_13711 "Sub Domain"     -> mọi ticket = "Payment Core"
        customfield_13710 "Product Domain" -> mọi ticket = "Payment Platform"
        customfield_11804 "Squad"          -> null toàn bộ
    => Không field nào tách được squad ở mức Jira. Mặc định adapter map squad
       từ Sub Domain (đồng nhất "Payment Core"); muốn tách squad thật phải dùng
       SQUAD_BY_MEMBER (mapping username -> squad do team tự định nghĩa).

Workflow status PC -> canonical (rank dùng cho điều kiện status <= 'InTest'):
    New -> Backlog | In Dev -> InDev | Walkthrough -> Walkthrough
    Ready for testing -> Ready for testing | In Test -> InTest
    Ready Deploy Staging/Production -> InReview (đã qua test)
    DONE/Cancelled -> Done

Dùng được 2 chế độ:
    1) REST mode (production cron):  fetch_tickets()  — cần env JIRA_BASE_URL, JIRA_PAT
    2) Pure mapping (test/agent MCP): normalize_issue(raw_json)
"""
import os, json
from datetime import date

PROJECT_KEY = "PC"

# QE PIC: ưu tiên field QC PIC thật; nếu trống thì fallback các label là username QE.
KNOWN_QE_LABELS = {"baolv", "tinvt3"}   # bổ sung đủ username QE của team

# SQUAD: project PC không có field tách squad (xem docstring).
# Đặt 'subdomain' để dùng Sub Domain (đồng nhất "Payment Core"),
# hoặc 'member' để map theo username qua SQUAD_BY_MEMBER bên dưới.
SQUAD_SOURCE = "subdomain"
SQUAD_BY_MEMBER = {
    # "chaubtm": "squad-retry",
    # "phatnp":  "squad-adapter",
    # ... team tự điền nếu muốn tách squad theo người
}

FIELD_MAP = {
    "story_point":        "customfield_10801",
    "test_start_date":    "customfield_13703",
    "test_complete_date": "customfield_13704",
    "sandbox_date":       "customfield_11702",
    "qc_pic":             "customfield_10418",   # user picker — QE PIC thật
    "sub_domain":         "customfield_13711",   # "Payment Core"
}

STATUS_CANONICAL = {
    "New": "New",
    "Open": "Open",
    "In Dev": "InDev",
    "In Progress": "InDev",
    "Walkthrough": "Walkthrough",
    "Ready for testing": "Ready for testing",
    "In Test": "InTest",
    "In Review": "InReview",
    "Ready Deploy Staging": "InReview",     # đã qua test -> không bị coi là chưa test
    "Ready Deploy Production": "InReview",
    "DONE": "Done",
    "Done": "Done",
    "Cancelled": "Done",
    "Blocked": "Blocked",
}

# JQL cho daily scan — loại ticket đã xong/hủy ngay từ query cho nhẹ
SCAN_JQL = (
    f'project = {PROJECT_KEY} '
    f'AND issuetype in (Story, Bug, Task) '
    f'AND status not in (DONE, Cancelled) '
    f'ORDER BY key ASC'
)

REQUEST_FIELDS = ",".join(
    ["summary", "status", "assignee", "labels", "issuetype"] + list(FIELD_MAP.values())
)

# ---------------------------------------------------------------- mapping
def _unwrap(v):
    """MCP trả custom field dạng {'value': x}; REST trả raw value. Xử lý cả hai."""
    if isinstance(v, dict) and set(v.keys()) <= {"value", "id", "self", "display_name", "name", "key"}:
        if "value" in v:
            return v["value"]
    return v


def _get(raw, field_id):
    """Lấy field từ cả 2 shape: REST ({'fields': {...}}) và MCP (flat)."""
    src = raw.get("fields", raw)
    return _unwrap(src.get(field_id))


def _extract_qe_pic(raw):
    """QE PIC: ưu tiên field QC PIC (user picker), fallback về label QE."""
    src = raw.get("fields", raw)
    qc = _unwrap(src.get(FIELD_MAP["qc_pic"]))
    if isinstance(qc, dict):
        qc = qc.get("name") or qc.get("value")
    if qc:
        return qc
    for lb in src.get("labels") or []:
        if lb.lower() in KNOWN_QE_LABELS:
            return lb.lower()
    return None


def _extract_squad(raw, assignee):
    if SQUAD_SOURCE == "member":
        return SQUAD_BY_MEMBER.get(assignee)
    # mặc định: Sub Domain (lưu ý: đồng nhất "Payment Core" trên toàn project PC)
    return _get(raw, FIELD_MAP["sub_domain"])


def normalize_issue(raw):
    """Map 1 issue Jira (REST hoặc MCP JSON) -> ticket dict chuẩn của rule engine."""
    src = raw.get("fields", raw)

    assignee = src.get("assignee") or {}
    if isinstance(assignee, dict):
        assignee = assignee.get("name") or assignee.get("display_name")

    raw_status = src.get("status")
    if isinstance(raw_status, dict):
        raw_status = raw_status.get("name")

    sp = _get(raw, FIELD_MAP["story_point"])

    return {
        "key": raw.get("key"),
        "summary": src.get("summary"),
        "status": STATUS_CANONICAL.get(raw_status, raw_status),
        "status_raw": raw_status,                     # giữ tên gốc để hiển thị
        "story_point": float(sp) if sp is not None else None,
        "test_start_date": _get(raw, FIELD_MAP["test_start_date"]),
        "test_complete_date": _get(raw, FIELD_MAP["test_complete_date"]),
        "sandbox_date": _get(raw, FIELD_MAP["sandbox_date"]),
        "squad": _extract_squad(raw, assignee),
        "assignee": assignee,
        "qe_pic": _extract_qe_pic(raw),
        "labels": src.get("labels") or [],
    }


# ---------------------------------------------------------------- REST mode (production)
def fetch_tickets(jql=SCAN_JQL, page_size=50):
    """Kéo toàn bộ ticket theo JQL từ Jira Server/DC. Trả List[ticket_dict].
    Env cần có:  JIRA_BASE_URL=https://jira.zalopay.vn   JIRA_PAT=<personal access token>
    """
    import requests  # import tại đây để test mapping không cần requests
    base = os.environ["JIRA_BASE_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['JIRA_PAT']}"}
    tickets, start = [], 0
    while True:
        resp = requests.get(
            f"{base}/rest/api/2/search", headers=headers, timeout=30,
            params={"jql": jql, "fields": REQUEST_FIELDS,
                    "startAt": start, "maxResults": page_size})
        resp.raise_for_status()
        data = resp.json()
        tickets += [normalize_issue(i) for i in data["issues"]]
        start += page_size
        if start >= data["total"]:
            return tickets


if __name__ == "__main__":
    print(json.dumps(normalize_issue(json.load(open("sample_real_issues.json"))[0]),
                     ensure_ascii=False, indent=2))
