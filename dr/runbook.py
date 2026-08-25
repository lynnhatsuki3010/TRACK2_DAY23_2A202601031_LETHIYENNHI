"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    ans = input(f"{msg} [y/N] ").strip().lower()
    return ans in ("y", "yes")


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook §4 'Region Chính Down'."""
    t_start = time.time()

    # 1 xac_nhan_outage — nhiều lần probe, không tin 1 lần fail
    probes = []
    for _ in range(3):
        ready, reason = hc.probe(primary, timeout=2.0)
        probes.append({"ready": ready, "reason": reason})
        time.sleep(0.2)
    confirmed = all(not p["ready"] for p in probes)
    step(1, "xac_nhan_outage", region=primary, probes=probes, confirmed=confirmed)

    # 2 thong_bao_incident — ts cua buoc nay LUON SAU t_outage
    chaos_events = []
    chaos_path = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_path.exists():
        chaos_events = [json.loads(l) for l in chaos_path.read_text().splitlines() if l.strip()]
    kills = [e for e in chaos_events if e.get("action") == "kill" and e.get("region") == primary]
    t_outage = kills[-1]["ts"] if kills else None
    t_operator = time.time()
    step(2, "thong_bao_incident", t_outage=t_outage, t_operator=t_operator,
         delay_s=None if t_outage is None else round(t_operator - t_outage, 2))

    if not confirm(auto, f"Xac nhan failover {primary} -> {target}?"):
        step(2, "aborted_by_operator", region=primary, target=target)
        return {"ok": False, "reason": "operator_declined"}

    # 3 scale_gpu_pool — goi failover.failover(...) DUNG 1 LAN
    r = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", target=target, ok=r["ok"], pool_state=r.get("pool_state"))

    # 4 verify_state_replica — chi doc lai ket qua buoc 3, khong goi lai failover
    final_state = r["wait_ready"]["final_state"]
    step(4, "verify_state_replica", target=target,
         rpo_seconds=r["restore"]["rpo_seconds"], docs_lost=r["restore"]["docs_lost"],
         vectors=(final_state or {}).get("vectors"),
         weights=(final_state or {}).get("weights") if final_state else None)

    # 5 dns_cutover — chi doc lai
    step(5, "dns_cutover", target=target, ok=r["cutover"]["ok"])

    # 6 verify_golden_signals — 10 request that qua edge
    latencies, errors = [], 0
    with httpx.Client(timeout=5.0) as c:
        for i in range(10):
            t0 = time.time()
            try:
                resp = c.get("http://127.0.0.1:8080/v1/infer", params={"q": f"check {i}"})
                latencies.append((time.time() - t0) * 1000)
                if resp.status_code != 200:
                    errors += 1
            except Exception:
                latencies.append((time.time() - t0) * 1000)
                errors += 1
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
    step(6, "verify_golden_signals", request_count=len(latencies),
         p95_ms=None if p95 is None else round(p95, 1), error_rate=errors / max(1, len(latencies)))

    # 7 post_incident
    elapsed_s = round(time.time() - t_start, 2)
    step(7, "post_incident", elapsed_s=elapsed_s,
         measure_cmd="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")

    return {"ok": r["ok"], "failover": r, "elapsed_s": elapsed_s}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
