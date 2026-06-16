# QE Watchdog Agent — Bài Mô Tả Ngắn

## Vấn đề
Mỗi sprint, đội Quality Engineering (QE) phải tự rà soát hàng chục ticket Jira để phát hiện vi phạm quy trình test: thiếu ngày test, trễ ngày bắt đầu/hoàn thành test, ngày sandbox sắp đến, ticket bị block, hay một QE bị giao quá nhiều việc cùng ngày. Việc rà soát thủ công này tốn 30–60 phút/ngày, dễ bỏ sót, và khi phát hiện trễ thì test đã delay — kéo theo rủi ro lùi lịch release.

## Người dùng mục tiêu
- **Đội QE**: nhận cảnh báo các vi phạm liên quan ngày test, sandbox, ticket block.
- **Đội Dev (MS & CRM)**: nhận cảnh báo riêng theo từng team/component.
- **QE Lead / PM**: dùng web UI để chủ động scan, tra cứu lịch sử và lên lịch.

## Cách agent giải quyết
- **Input**: Ticket Jira (qua REST API hoặc snapshot JSON) + bộ 14 luật nghiệp vụ trong `rules.yaml`.
- **Xử lý**: Agent chạy trên nền tảng **GreenNode AgentBase** (cung cấp endpoint `/invocations`, inject credential và LLM theo cơ chế function-calling). Pipeline: fetch ticket → chuẩn hoá → chạy rule engine (date-math bỏ qua cuối tuần/ngày lễ) → phân loại vi phạm theo 4 mức (L0 vệ sinh dữ liệu → L1 vi phạm nặng → L2 rủi ro → L3 cần theo dõi) → định tuyến theo kênh (QE / Dev-MS / Dev-CRM). LLM hỗ trợ phân tích và sinh checklist (TEP, ISTQB). Các agent tool: `search_jira`, `run_rule_engine`, `get_violations_summary`, `get_ticket_history`...
- **Output**: Email HTML màu hoá theo mức độ gửi vào kênh Teams, có @mention người phụ trách + đề xuất hành động; lưu lịch sử vào SQLite; dashboard web để scan thủ công.

## Giá trị mang lại
Chạy tự động 09:00 các ngày làm việc, agent tiết kiệm 30–60 phút rà soát/ngày cho mỗi reviewer, loại bỏ việc bỏ sót do thủ công, và phát hiện rủi ro sớm thay vì sau khi đã trễ — giúp giữ đúng lịch test và release.
