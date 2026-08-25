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

1. Vì sao user thấy lỗi? Region A ngừng trả lời do `SIGSTOP`, mà edge proxy vẫn route sang A
   vì file `edge/active_region` chưa đổi.
2. Vì sao edge không tự né A? Vì nó là load balancer thụ động, chỉ đọc file cấu hình rồi
   chuyển tiếp. Phát hiện outage là việc của health checker, tách lớp như vậy là đúng thiết kế.
3. Vì sao mất 8.1 giây mới gọi được failover? Vì `dr/runbook.py` bước 1 thăm dò tuần tự 3 lần,
   mỗi lần timeout 2 giây, và không kết luận sớm dù hai lần đầu đã đủ rõ.
4. Vì sao mất thêm 6.7 giây sau khi failover bắt đầu? Vì region B khởi động ở `pool_state=warm`,
   khi chuyển sang `full` phải chờ đủ `WARMUP_SECONDS=6` để mô phỏng chi phí nạp model lên GPU.
5. Root cause thật sự: không có bug. Mọi giây trong 20.3 giây đều là chi phí cố ý của kiến trúc
   bán tự động, thăm dò tuần tự để tránh kết luận nhầm và giữ region phụ ấm thay vì bật full
   24/7 để khỏi đốt tiền GPU. Nếu mục tiêu RTO siết còn 30 giây thì bước 3 và bước 4 ở trên là
   hai chỗ phải thiết kế lại đầu tiên, chúng là trade-off chưa tối ưu chứ không phải lỗi.

## 4. Action items (có owner + deadline)

| # | Action                                                                            | Owner         | Deadline   | Giảm RTO/RPO bao nhiêu giây |
| - | --------------------------------------------------------------------------------- | ------------- | ---------- | ------------------------------ |
| 1 | Thăm dò song song trong`dr/runbook.py` bước 1, hạ timeout 2s xuống 1s     | on-call dev   | 2026-09-01 | RTO ~-6s (8.1s còn ~2s)       |
| 2 | Pre-warm region B, giữ`pool_state=warm` sẵn thay vì đợi cutover mới scale | platform team | 2026-09-08 | RTO ~-6.7s                     |
| 3 | Giảm`EDGE_TTL_SECONDS` từ 5s xuống 2s cho route `/v1/infer`                | infra team    | 2026-09-05 | RTO ~-3s                       |
| 4 | Tăng tần suất`state/replicate.py` từ 30s xuống 10s                         | data team     | 2026-09-10 | RPO -17s, RTO không đổi     |

Item 4 chỉ cải thiện RPO chứ không giúp RTO nhanh hơn giây nào. Replicate dày hơn làm mất ít
dữ liệu hơn, không làm hệ thống sống lại sớm hơn. Hai trục độc lập nhau.

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?

`5.0 × 3 = 15.0s` theo cấu hình ghi ở `reports/health-events.jsonl:3`. So với RTO 20.3 giây thì
chiếm khoảng 74%. Nhưng con số 74% đó gây hiểu nhầm, vì lần chạy này health check không phải
đường kích hoạt failover. Runbook đã tự phát hiện và khởi động ở giây 8.1, sớm hơn lúc health
check gắn cờ ở giây 14.3. Nên 15.0 giây là sàn lý thuyết của cấu hình, không phải chi phí thật
đã tiêu trên đường đi.

2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?

Sàn lý thuyết giảm còn `1 × 3 = 3s`, nhưng RTO đo được sẽ không giảm giây nào vì health check
không nằm trên đường quyết định. Muốn nhanh thật thì phải sửa ba lần thăm dò nhân 2 giây
timeout trong `dr/runbook.py`, đúng như action item 1. Giá phải trả nếu vẫn hạ interval xuống
1 giây: thăm dò dày gấp 5 lần làm tăng tải lên `/readyz` của cả hai region, và dễ flap hơn vì
khi region chỉ chậm nhất thời thì ba lần thăm dò cách nhau 1 giây rất dễ rơi trọn vào khoảng
chậm đó và chạm threshold, trong khi cách nhau 5 giây thường vượt qua được.

3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa
   gì với khách hàng?

`docs_lost=13` ở đây là 13 tài liệu khách tạo trong 27.01 giây sau lần replicate cuối, bản
restore ở region B không hề biết chúng tồn tại. Với khách hàng đó là 13 lần bấm gửi, màn hình
báo đã lưu, rồi mất trắng, phải nhập lại từ đầu. Về nguyên tắc RPO chỉ phụ thuộc chu kỳ
replicate, 30 giây ở đây, chứ không phụ thuộc outage dài bao lâu, vì phần mất luôn là phần nằm
giữa lần replicate cuối và lúc region chính chết. Điều mà mất vĩnh viễn thay đổi là bình thường
region A sống lại thì 13 tài liệu đó vẫn còn trên đĩa và đối chiếu gộp lại được, còn nếu A mất
hẳn thì snapshot cuối là thứ duy nhất còn lại, 13 tài liệu đó không cách nào lấy lại. Đây là lý
do chu kỳ backup phải khớp khẩu vị rủi ro của bên kinh doanh, không phải con số kỹ thuật chọn
tuỳ ý.
