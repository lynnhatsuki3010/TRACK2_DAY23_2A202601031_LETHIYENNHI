# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `a.alive=false` 3 lần liên tiếp cách nhau ≥5s | on-call |
| 2 | Mở incident + bấm giờ RTO | ghi ts hiện tại + link `reports/runbook-run.jsonl` vào kênh incident | ts ghi vào `reports/runbook-run.jsonl`, dòng `step:2 thong_bao_incident` | on-call |
| 3 | Restore state ở region phụ | `python3 dr/runbook.py --primary a --target b --backend fs --auto` (chạy luôn bước 3-5) | `reports/failover-events.jsonl` có `step:2_restore_snapshot` với `docs_lost` không null | on-call |
| 4 | Scale pool warm→full | tự động trong bước 3, hoặc tay: `printf full > state/region-b/pool_state` | `/readyz` của b trả 200: `curl -s -o /dev/null -w "%{http_code}" localhost:8002/readyz` | on-call |
| 5 | DNS/LB cutover | tự động trong bước 3, hoặc tay: `printf b > edge/active_region` | `curl localhost:8080/edge/state` cho `active_region=b` | on-call |
| 6 | Verify golden signals | `python3 loadgen/traffic.py --duration 10 --rps 2 --out /tmp/check.jsonl` | p95 < 500ms, error rate < 0.1, đo sau khi cutover ≥5s | SRE on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl` | `rto_verdict` != null, và `valid: true` | incident commander |

Bước 4 phải xong trước bước 5, không được đảo. Region B chỉ trả 200 ở `/readyz` khi có đủ
model weights, có dữ liệu, pool ở `full` và đã nạp model xong. Cutover trước khi B sẵn sàng
thì khách ăn 503 ở cả hai region, RTO dài ra chứ không ngắn lại.

Bước 6 phải chờ ít nhất 5 giây sau cutover mới đo, vì edge proxy còn cache region cũ theo
`EDGE_TTL_SECONDS`. Lần chạy vừa rồi đo ngay ở giây 21 nên ra error rate 0.2 và p95 2088ms
(`reports/runbook-run.jsonl:11`), đó là 2 trong 10 request đầu vẫn đi nhầm sang A đã chết,
không phải sự cố mới. Chờ hết cache rồi đo lại, nếu vẫn quá ngưỡng thì mới quay lại bước 4.

**Rollback (failover ngược):** điều kiện nào thì trả traffic về region A? Ai quyết định?
(§4 Anti-Patterns: full-auto không có circuit breaker → 2 region flap qua lại.)

Chỉ trả traffic về A khi đủ ba điều kiện. Một là A phải `alive=true` và `ready=true` liên tục
ít nhất 5 phút, kiểm bằng cách chạy lặp `python3 chaos/kill_region.py status` chứ không nhìn
một lần, vì A vừa hồi rồi chết tiếp sẽ làm traffic nảy qua nảy lại giữa hai region. Hai là đã
tìm ra và sửa được nguyên nhân gốc, không rollback kiểu thử xem sao. Ba là region B đang chạy
ổn, không có sự cố nào khác đang mở trên B.

Người quyết định là incident commander, tức người mở incident ở bước 2, không phải on-call tự
quyết một mình. Rollback quá sớm chính là kịch bản flapping mà §4 cảnh báo nên cần thêm một
người xác nhận. Lệnh rollback dùng lại đúng bước 3 nhưng đảo hai tham số:
`python3 dr/runbook.py --primary b --target a --backend fs`, và bỏ `--auto` để nó hỏi y/N.
