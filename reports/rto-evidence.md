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

Snapshot restore xong ở giây 8.4, trước khi health check phát hiện ở giây 14.3, vì `dr/runbook.py` đã tự kích hoạt failover từ giây 8.1. Automation hoàn tất ở giây 15.1 khi DNS cutover, nhưng khách phải chờ đến giây 20.3 mới dùng lại được vì edge proxy vẫn cache region cũ thêm khoảng 5 giây theo `EDGE_TTL_SECONDS`. RTO tính từ lúc khách dùng lại được, không phải lúc automation báo xong. RPO 27.01 giây, tương đương 13 doc, là phần dữ liệu được ghi vào region A sau lần replicate cuối nhưng chưa có ở region B. Chu kỳ replicate là 30 giây nên lượng dữ liệu có thể mất bị giới hạn quanh mức này, không phụ thuộc outage kéo dài bao lâu. Số tổng hợp nằm ở `reports/measure-drill-2.json`.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần              | Giây | Nó đến từ đâu                                                    | Giảm được bằng cách nào                                                         |
| ------------------------- | ----- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Health-check detect floor | 15.0  | `interval_s × threshold` trong `reports/health-events.jsonl:3`    | Giảm interval hoặc threshold, đổi lại dễ báo động giả và flapping           |
| Snapshot restore          | 0.2   | 2_restore → 3_scale,`reports/failover-events.jsonl:11`              | DB nhỏ nên đã rất nhanh, DB lớn mới lộ chi phí copy                           |
| GPU pool warm-up          | 6.7   | `waited_s` ở `4_wait_ready`, `reports/failover-events.jsonl:13` | Giữ region phụ ấm sẵn, đổi lại tốn tiền GPU 24/7                              |
| DNS/LB TTL cache          | 5.2   | t_recovered − t_cutover,`reports/drill-2-withdr.jsonl:35`           | Giảm`EDGE_TTL_SECONDS`, đổi lại mỗi request phải đọc lại file định tuyến |

Bốn thành phần trên không cộng trực tiếp thành 20.3 giây, vì 15.0 giây của health check chỉ là thời gian lý thuyết. Thực tế có hai đường phát hiện chạy song song: health check kiểm tra mỗi 5 giây và cần 3 lần lỗi liên tiếp, nên nhanh nhất là khoảng 15 giây và lần này báo ở giây 14.3. Trong khi đó, `dr/runbook.py` tự thăm dò 3 lần ngay khi được gọi và phát hiện ở giây 8.1. Chính đường này đã kích hoạt failover.

Đường đi thực tế là 8.1 giây runbook xác nhận, 0.2 giây restore snapshot, 6.7 giây nạp model lên GPU, đến 15.1 giây thì DNS cutover. Sau đó edge cache mất thêm 5.2 giây, nên request đầu tiên thành công ở 20.3 giây. Vì vậy, hạ interval của health check xuống 1 giây sẽ không làm RTO của lần chạy này nhanh hơn. Hai chỗ đang tốn thời gian thực sự là ba lần thăm dò với timeout 2 giây trong `dr/runbook.py` và 6.7 giây warm-up ở region B.
