# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số                      | Giá trị               | Cách đo                                     | Evidence                          |
| ----------------------------- | ----------------------- | --------------------------------------------- | --------------------------------- |
| t_outage                      | `2026-08-25T04:29:42` | chaos kill                                    | `chaos/chaos-events.jsonl:3`    |
| Request fail đầu tiên      | `+0.3s`               | dòng`ok:false` đầu tiên sau t_outage    | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có              | không có dòng`ok:true` nào sau t_outage | `reports/measure-drill-1.json`  |
| RTO                           | `NO_RECOVERY`         | `tools/measure_rto.py`                      | `reports/measure-drill-1.json`  |

Drill này không nhằm mục đích xem kết quả đạt được thành công mà nhằm để chứng minh nếu không chuẩn bị gì thì hệ thống die luôn mà không tự hồi phục. Giết region A lúc 04:29:42, sau 0.3 giây khách đã thấy lỗi và tới hết drill không có request nào thành công lại. 15 trên 32 request hỏng. Số request chỉ có 32 thay vì 80 vì mỗi request hỏng phải chờ hết 2 giây timeout mới bỏ cuộc nên tốc độ gửi tụt hẳn.

## 2. Drill 2 — có DR

| Mốc                        | +giây từ t_outage | Cách đo                       | Evidence                             |
| --------------------------- | ------------------- | ------------------------------- | ------------------------------------ |
| t_outage (mốc 0)           | 0                   | `action:kill`                 | `chaos/chaos-events.jsonl:6`       |
| User thấy lỗi đầu tiên | 0.4                 | dòng`ok:false` đầu         | `reports/drill-2-withdr.jsonl:26`  |
| Health check phát hiện    | 14.3                | `to:UNHEALTHY, region:a`      | `reports/health-events.jsonl:3`    |
| Snapshot restore xong       | 8.4                 | `step:2_restore_snapshot`     | `reports/failover-events.jsonl:11` |
| Region phụ ready           | 15.1                | `step:4_wait_ready`           | `reports/failover-events.jsonl:13` |
| DNS cutover                 | 15.1                | `step:5_dns_cutover`          | `reports/failover-events.jsonl:14` |
| **RTO đo được**   | **20.3**      | dòng`ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:35`  |

| Chỉ số             | Đo được             | Mục tiêu (slide §1) | Verdict |
| -------------------- | ----------------------- | ---------------------- | ------- |
| RTO — Inference API | `20.3s`               | 300s (5 phút)         | PASS    |
| RPO — Vector DB     | `27.01s` / `13` doc | 300s (5 phút)         | PASS    |

Snapshot restore xong ở giây 8.4, sớm hơn lúc health check phát hiện ở giây 14.3, vì failover
được `dr/runbook.py` tự kích hoạt ở giây 8.1 chứ không chờ health check. Automation làm xong
việc ở giây 15.1 khi DNS cutover, nhưng khách phải tới giây 20.3 mới dùng lại được, vì edge
proxy còn cache region cũ thêm 5 giây theo `EDGE_TTL_SECONDS`. RTO tính theo lúc khách dùng
được, không tính theo lúc automation báo xong. RPO 27.01 giây tương đương 13 doc là phần dữ
liệu nhập vào region A sau lần replicate cuối, bản restore ở region B không có. Chu kỳ
replicate là 30 giây nên RPO luôn bị chặn quanh mức đó, không phụ thuộc outage dài bao lâu.
Số tổng hợp ở `reports/measure-drill-2.json`.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần              | Giây | Nó đến từ đâu                                                    | Giảm được bằng cách nào                                                         |
| ------------------------- | ----- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Health-check detect floor | 15.0  | `interval_s × threshold` trong `reports/health-events.jsonl:3`    | Giảm interval hoặc threshold, đổi lại dễ báo động giả và flapping           |
| Snapshot restore          | 0.2   | 2_restore → 3_scale,`reports/failover-events.jsonl:11`              | DB nhỏ nên đã rất nhanh, DB lớn mới lộ chi phí copy                           |
| GPU pool warm-up          | 6.7   | `waited_s` ở `4_wait_ready`, `reports/failover-events.jsonl:13` | Giữ region phụ ấm sẵn, đổi lại tốn tiền GPU 24/7                              |
| DNS/LB TTL cache          | 5.2   | t_recovered − t_cutover,`reports/drill-2-withdr.jsonl:35`           | Giảm`EDGE_TTL_SECONDS`, đổi lại mỗi request phải đọc lại file định tuyến |

Bốn thành phần trên cộng lại không ra 20.3 giây, và đó là điểm đáng nói nhất của lần chạy này.
Con số 15.0 giây chỉ là sàn lý thuyết của health check, nó không nằm trên đường đi thật. Hệ
thống có hai đường phát hiện chạy song song: health check thăm dò 5 giây một lần và cần 3 lần
hỏng liên tiếp nên nhanh nhất là 15 giây, thực tế báo ở giây 14.3; còn `dr/runbook.py` tự thăm
dò 3 phát liên tục ngay khi được gọi nên báo ở giây 8.1. Đường thứ hai nhanh hơn và chính nó
kích hoạt failover.

Đường đi thật là 8.1 giây runbook xác nhận, cộng 0.2 giây restore snapshot, cộng 6.7 giây nạp
model lên GPU, ra 15.1 giây là lúc DNS cutover, rồi cộng thêm 5.2 giây chờ hết hạn cache, tổng
đúng 20.3 giây. Hệ quả thực tế là hạ interval của health check xuống 1 giây sẽ không làm RTO
nhanh hơn giây nào, vì đó không phải chỗ nghẽn. Chỗ nghẽn thật là 3 lần thăm dò nhân 2 giây
timeout trong `dr/runbook.py`, và 6.7 giây nạp model ở region B.
