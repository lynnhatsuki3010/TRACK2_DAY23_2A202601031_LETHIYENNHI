# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time                     | Sự kiện                                         | Evidence                             |
| ---------------------------- | ------------------------------------------------- | ------------------------------------ |
| 2026-08-25T04:33:20          | outage bắt đầu                                 | `chaos/chaos-events.jsonl:6`       |
| 2026-08-25T04:33:20 (+0.4s)  | user đầu tiên bị ảnh hưởng                 | `reports/drill-2-withdr.jsonl:26`  |
| 2026-08-25T04:33:34 (+14.3s) | health check alert                                | `reports/health-events.jsonl:3`    |
| 2026-08-25T04:33:35 (+15.1s) | operator confirm cutover                          | `reports/failover-events.jsonl:14` |
| 2026-08-25T04:33:40 (+20.3s) | resolved (request đầu tiên OK từ region phụ) | `reports/drill-2-withdr.jsonl:35`  |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `20.3s` · gap: `-279.7s`
- RPO mục tiêu: 300s · đo được: `27.01s` (`13` doc bị mất) · gap: `-272.99s`
- **Bước tốn nhiều giây nhất:** `runbook tự xác nhận outage, 8.1s` — vì sao?

Mục tiêu 300s hiện khá rộng so với thời gian thực tế của hệ thống nên gap âm rất lớn và không phản ánh nhiều về rủi ro thực tế. Dù vậy thì kết quả này vẫn hữu ích vì cho thấy những bước nào đang chiếm nhiều thời gian nhất trong tổng RTO 20.3 giây. Đặc biệt là khi phải mất 8.1 giây để runbook tự xác nhận outage (reports/runbook-run.jsonl:7) và 6.7 giây để nạp model lên GPU ở region B (reports/failover-events.jsonl:13). Runbook mất 8.1 giây vì dr/runbook.py đang thăm dò region A ba lần theo tuần tự, mỗi lần chờ tối đa 2 giây. Trong tình huống SIGSTOP, kết nối TCP vẫn có thể mở nhưng process không trả lời, nên mỗi lần thăm dò phải chờ hết timeout mới chuyển sang lần tiếp theo. Nếu process chết hẳn thì kết nối có thể bị từ chối ngay và thời gian này sẽ ngắn hơn.

## 3. Root cause (5 whys)

Vấn đề ở đây không phải là chaos script đã được chạy như thế nào mà là nếu đây là một outage thật thì quy trình failover hiện tại sẽ phản ứng ra sao và điểm nào trong runbook đang làm chậm quá trình.

1. Vì sao user thấy lỗi? Region A ngừng trả lời do `SIGSTOP`, trong khi edge proxy vẫn route sang A vì file `edge/active_region` chưa đổi.
2. Vì sao edge không tự né A? Vì đây là load balancer thụ động, chỉ đọc file cấu hình rồi chuyển tiếp. Việc phát hiện outage do health checker đảm nhiệm; tách hai phần này là đúng với thiết kế hiện tại.
3. Vì sao mất 8.1 giây mới gọi được failover? Vì `dr/runbook.py` ở bước 1 thăm dò tuần tự 3 lần, mỗi lần timeout 2 giây, thay vì kết luận sớm khi đã có đủ tín hiệu.
4. Vì sao mất thêm 6.7 giây sau khi failover bắt đầu? Vì region B đang ở `pool_state=warm`. Khi chuyển sang `full`, hệ thống phải chờ `WARMUP_SECONDS=6` để mô phỏng thời gian nạp model lên GPU.
5. Root cause: không có một bug cụ thể. 20.3 giây là tổng thời gian của các lựa chọn thiết kế hiện tại: runbook thăm dò tuần tự để giảm khả năng kết luận nhầm, còn region phụ được giữ ở trạng thái warm thay vì chạy full 24/7 để tiết kiệm GPU. Đây là các trade-off về độ an toàn và chi phí. Nếu RTO được siết xuống 30 giây, hai phần nên ưu tiên xem lại là thời gian thăm dò và thời gian warm-up của region B.

## 4. Action items (có owner + deadline)

| # | Action                                                                            | Owner         | Deadline   | Giảm RTO/RPO bao nhiêu giây |
| - | --------------------------------------------------------------------------------- | ------------- | ---------- | ------------------------------ |
| 1 | Thăm dò song song trong`dr/runbook.py` bước 1, hạ timeout 2s xuống 1s     | on-call dev   | 2026-09-01 | RTO ~-6s (8.1s còn ~2s)       |
| 2 | Pre-warm region B, giữ`pool_state=warm` sẵn thay vì đợi cutover mới scale | platform team | 2026-09-08 | RTO ~-6.7s                     |
| 3 | Giảm`EDGE_TTL_SECONDS` từ 5s xuống 2s cho route `/v1/infer`                | infra team    | 2026-09-05 | RTO ~-3s                       |
| 4 | Tăng tần suất`state/replicate.py` từ 30s xuống 10s                         | data team     | 2026-09-10 | RPO -17s, RTO không đổi     |

Action item 4 chỉ giúp giảm RPO, không làm RTO nhanh hơn. Replicate thường xuyên hơn thì lượng dữ liệu chưa kịp đồng bộ sẽ ít hơn, nhưng thời gian phát hiện outage và chuyển sang region B vẫn giữ nguyên. Hai chỉ số này cần được xem riêng.

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?

`5.0 × 3 = 15.0s` theo cấu hình tại `reports/health-events.jsonl:3`. So với RTO 20.3 giây thì con số này tương đương khoảng 74%. Tuy nhiên, 74% không phải thời gian thực tế đã mất trong lần drill này. Health check chỉ báo ở giây 14.3, trong khi runbook đã tự phát hiện outage và bắt đầu failover từ giây 8.1. Vì vậy, 15.0 giây là sàn lý thuyết của health check, không phải chi phí trên đường failover thực tế.

2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?

Về lý thuyết, sàn phát hiện sẽ giảm từ `5 × 3 = 15s` xuống `1 × 3 = 3s`. Nhưng với lần drill này, RTO thực tế sẽ không giảm vì health check không phải cơ chế kích hoạt failover. Muốn giảm thời gian thật, cần sửa ba lần thăm dò với timeout 2 giây trong `dr/runbook.py`, như action item 1.

Nếu vẫn hạ interval xuống 1 giây, `/readyz` của cả hai region sẽ bị kiểm tra thường xuyên hơn, làm tăng tải và tăng nguy cơ flapping. Khi region chỉ chậm trong vài giây, ba lần kiểm tra cách nhau 1 giây có thể đều rơi vào đúng khoảng chậm và chạm threshold. Với interval 5 giây, khả năng này thấp hơn.

3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?

`docs_lost=13` nghĩa là có 13 tài liệu được tạo trong 27.01 giây sau lần replicate cuối nhưng chưa được đồng bộ sang region B. Với khách hàng, đây là 13 lần gửi mà hệ thống có thể đã báo lưu thành công nhưng sau failover dữ liệu không còn, buộc họ phải nhập lại.

RPO phụ thuộc vào chu kỳ replicate, ở đây là 30 giây, chứ không phụ thuộc trực tiếp vào việc outage kéo dài bao lâu. Nếu region A chỉ tạm thời mất kết nối rồi hoạt động lại, 13 tài liệu vẫn có thể còn trên đĩa ở A và được đối chiếu sau đó. Nhưng nếu A mất hoàn toàn thì snapshot cuối ở B là bản dữ liệu duy nhất còn lại, và 13 tài liệu chưa replicate sẽ không thể khôi phục. Vì vậy, chu kỳ backup/replicate nên được chọn theo mức mất dữ liệu mà business chấp nhận được.
