# QE Daily Agent — I/O Layer (Person 1)

Agent đọc ticket Jira → check rule → mỗi ngày làm việc (9h, Mon–Fri) bắn report lên Teams.
Phần I/O (fetch Jira + gửi Teams + scheduler) đã hoàn chỉnh. Rules engine do Person 2 điền.

## Cài đặt
```bash
pip3 install -r requirements.txt    # macOS: thêm --break-system-packages nếu cần
```

## Chạy thử (KHÔNG cần Jira/Teams)
```bash
python3 -m qe_agent.main --once --mock --dry-run   # mock data, in card ra màn hình
python3 -m qe_agent.mock_data                       # in mock tickets dạng JSON (cho Person 2)
```

## Chạy thật
Copy `.env.example` → `.env`, điền token + flow URL, rồi export (hoặc dùng tool nạp .env):
```bash
export JIRA_BASE_URL=https://jira.zalopay.vn
export JIRA_PAT=...
```
Các cách input:
```bash
python3 -m qe_agent.main --once                 # mặc định: ACTIVE sprint của project GE
python3 -m qe_agent.main --team MS --once       # chỉ ticket sprint "MS - ..."
python3 -m qe_agent.main --team CRM --once      # chỉ "CRM - ..."
python3 -m qe_agent.main --keys GE-14209 GE-14757   # check theo link/key cụ thể
python3 -m qe_agent.main --sprint 4293          # 1 sprint id cụ thể
python3 -m qe_agent.main --list-sprints <BOARD_ID>  # xem sprint active của board
python3 -m qe_agent.main                         # scheduler treo, tự bắn 9h Mon–Fri
```
Thêm `--dry-run` vào bất kỳ lệnh nào để in card thay vì gửi Teams.

## Cấu hình đã chốt (Zalopay Jira)
- Project: **GE** (Growth Enablement, id 13302)
- Story point `customfield_10801` · QC PIC `customfield_10418`
- Test Start `13703` · Test Complete `13704` · Sandbox `11702`
- Component routing: "Marketing Solutions" → MS, "CRM" → CRM
- Sprint: lấy ACTIVE sprint của project qua Jira Agile API; lọc team theo prefix tên sprint ("MS -" / "CRM -")
- Blocked = status "BLOCKED / ON HOLD"; NoQE = label "NoQE"/"no_qe"

## Channels (mỗi cái 1 Power Automate flow)
- `TEAMS_FLOW_QE` → QE Daily
- `TEAMS_FLOW_DEV_MS` → Dev MS
- `TEAMS_FLOW_DEV_CRM` → Dev CRM
Routing: ticket MS → QE + DevMS; CRM → QE + DevCRM; không component → cả hai Dev.

## Còn để xác nhận khi chạy live
- `JIRA_SPRINT_FIELD` (đang mặc định `customfield_10000`) — nếu `--team` ra rỗng thì sửa.
- Teams @mention thật cần Power Automate / Graph API (webhook thường chỉ hiện "@name" dạng text).

## Bàn giao Person 2
Điền `qe_agent/rules.py`. Dùng chung `Ticket`, `DailyReport`, `RuleResult` trong `qe_agent/models.py`.
Set `RuleResult.send_qe` / `send_dev` / `mention_assignee` theo mindmap để route đúng channel.
