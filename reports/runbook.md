# Runbook 1 trang — Region chính down

| # | Bước                       | Lệnh                                                                                         | Biết là xong khi                                                                               | Ai làm            |
| - | ---------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ------------------ |
| 1 | Xác nhận outage            | `python3 chaos/kill_region.py status`                                                       | `a.alive=false` 3 lần liên tiếp cách nhau ≥5s                                             | on-call            |
| 2 | Mở incident + bấm giờ RTO | ghi ts hiện tại + link`reports/runbook-run.jsonl` vào kênh incident                     | ts ghi vào`reports/runbook-run.jsonl`, dòng `step:2 thong_bao_incident`                    | on-call            |
| 3 | Restore state ở region phụ | `python3 dr/runbook.py --primary a --target b --backend fs --auto` (chạy luôn bước 3-5) | `reports/failover-events.jsonl` có `step:2_restore_snapshot` với `docs_lost` không null | on-call            |
| 4 | Scale pool warm→full        | tự động trong bước 3, hoặc tay:`printf full > state/region-b/pool_state`              | `/readyz` của b trả 200: `curl -s -o /dev/null -w "%{http_code}" localhost:8002/readyz`    | on-call            |
| 5 | DNS/LB cutover               | tự động trong bước 3, hoặc tay:`printf b > edge/active_region`                        | `curl localhost:8080/edge/state` cho `active_region=b`                                       | on-call            |
| 6 | Verify golden signals        | `python3 loadgen/traffic.py --duration 10 --rps 2 --out /tmp/check.jsonl`                   | p95 < 500ms, error rate < 0.1, đo sau khi cutover ≥5s                                          | SRE on-call        |
| 7 | Đo RTO + postmortem         | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl`                       | `rto_verdict` != null, và `valid: true`                                                     | incident commander |


Bước 4 phải xong trước bước 5, Region B chỉ trả 200 ở `/readyz` khi đã có đủ model weights, có dữ liệu, pool ở `full` và model đã được nạp xong. Nếu cutover trước khi B sẵn sàng, khách có thể nhận 503 ở cả hai region và RTO sẽ dài hơn.

Bước 6 phải chờ ít nhất 5 giây sau cutover mới đo vì edge proxy vẫn có thể cache region cũ theo `EDGE_TTL_SECONDS`. Lần chạy vừa rồi đo ở giây 21 nên error rate là 0.2 và p95 là 2088ms (`reports/runbook-run.jsonl:11`). Có 2/10 request đầu vẫn đi nhầm sang A đã chết, đây không phải một sự cố mới. Chờ cache hết hạn rồi đo lại; nếu kết quả vẫn vượt ngưỡng thì quay lại kiểm tra bước 4.

**Rollback (failover ngược):** điều kiện nào thì trả traffic về region A? Ai quyết định?
(§4 Anti-Patterns: full-auto không có circuit breaker → 2 region flap qua lại.)

Chỉ trả traffic về A khi đủ ba điều kiện. Thứ nhất, A phải có `alive=true` và `ready=true` liên tục ít nhất 5 phút. Kiểm tra lặp bằng `python3 chaos/kill_region.py status`, không chỉ kiểm tra một lần, vì A có thể vừa hồi xong lại chết và làm traffic chuyển qua lại giữa hai region. Thứ hai, nguyên nhân gốc phải được xác định và xử lý; không rollback chỉ để thử. Thứ ba, region B phải đang chạy ổn và không có incident khác đang mở.

Người quyết định rollback là incident commander, tức người mở incident ở bước 2. On-call không tự quyết một mình. Rollback quá sớm có thể gây flapping như cảnh báo ở §4, nên cần thêm một người xác nhận. Lệnh rollback dùng lại bước 3 nhưng đảo hai tham số:
`python3 dr/runbook.py --primary b --target a --backend fs`, và bỏ `--auto` để lệnh yêu cầu xác nhận y/N.
