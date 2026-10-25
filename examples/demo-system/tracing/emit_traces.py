"""Send realistic OpenTelemetry traces for the demo shop's request flow to an OTLP/HTTP endpoint (no SDK needed).

    python emit_traces.py                       # 12 normal, 4 slow-database and 3 failing-payment traces, to localhost:4318
    python emit_traces.py --endpoint http://tempo:4318 --normal 30 --slow 0 --failing 0

Used to try the traces source against a real Tempo or Jaeger without instrumenting anything. The demo shop's own services emit
the same shape when they are started with tracing on (see the README).
"""

from __future__ import annotations

import argparse
import json
import random
import time
import urllib.request


def _id(n: int) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def _attrs(**kv) -> list[dict]:
    out = []
    for k, v in kv.items():
        key = k.replace("__", ".")
        if isinstance(v, bool):
            out.append({"key": key, "value": {"boolValue": v}})
        elif isinstance(v, int):
            out.append({"key": key, "value": {"intValue": str(v)}})
        else:
            out.append({"key": key, "value": {"stringValue": str(v)}})
    return out


class Trace:
    def __init__(self, start_ns: int):
        self.trace_id, self.start, self.by_service = _id(32), start_ns, {}

    def span(self, service: str, instance: str, name: str, kind: int, offset_ms: float, dur_ms: float, parent: str | None = None,
             error: str | None = None, **attrs) -> str:
        sid = _id(16)
        s = {"traceId": self.trace_id, "spanId": sid, "name": name, "kind": kind,
             "startTimeUnixNano": str(int(self.start + offset_ms * 1e6)), "endTimeUnixNano": str(int(self.start + (offset_ms + dur_ms) * 1e6)),
             "attributes": _attrs(**attrs), "status": {"code": 2, "message": error} if error else {"code": 1}}
        if parent:
            s["parentSpanId"] = parent
        if error:
            s["events"] = [{"name": "exception", "timeUnixNano": s["endTimeUnixNano"],
                            "attributes": _attrs(exception__type="UpstreamError", exception__message=error)}]
        self.by_service.setdefault((service, instance), []).append(s)
        return sid

    def payload(self) -> dict:
        return {"resourceSpans": [
            {"resource": {"attributes": _attrs(service__name=svc, service__instance__id=inst)},
             "scopeSpans": [{"scope": {"name": "demo"}, "spans": spans}]}
            for (svc, inst), spans in self.by_service.items()]}


def checkout(kind: str, now_ns: int) -> Trace:
    """gateway /checkout -> orders /orders -> database, then -> payments /pay -> the card processor."""
    t = Trace(now_ns)
    db_ms = 900 if kind == "slow" else random.uniform(3, 9)
    orders_ms = db_ms + random.uniform(2, 4)
    pay_ms = random.uniform(40, 70)
    psp_ms = pay_ms - random.uniform(5, 12)
    total = 6 + orders_ms + 6 + pay_ms + 4
    err = "upstream card processor returned 502 (connection reset)" if kind == "failing" else None
    root = t.span("gateway", "gateway", "GET /checkout", 2, 0, total, error=("payments failed" if err else None),
                  http__method="GET", http__route="/checkout", http__status_code=502 if err else 200)
    c1 = t.span("gateway", "gateway", "GET orders", 3, 3, orders_ms + 3, parent=root, http__url="http://orders:8000/orders", peer__service="orders")
    s1 = t.span("orders", "orders", "GET /orders", 2, 5, orders_ms, parent=c1, http__route="/orders", http__status_code=200)
    t.span("orders", "orders", "SELECT shopdb.orders", 3, 7, db_ms, parent=s1, db__system="postgresql", db__name="shopdb",
           db__statement="SELECT * FROM orders WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 20")
    off = 6 + orders_ms + 3
    inst = random.choice(["payments-a", "payments-b"])
    c2 = t.span("gateway", "gateway", "POST payments", 3, off, pay_ms + 3, parent=root, error=err, http__url=f"http://{inst}:8000/pay", peer__service="payments")
    s2 = t.span("payments", inst, "POST /pay", 2, off + 2, pay_ms, parent=c2, error=err, http__route="/pay", http__status_code=502 if err else 200)
    t.span("payments", inst, "POST psp.example.com", 3, off + 4, psp_ms, parent=s2, error=err, http__url="http://psp.example.com:8000/psp", peer__service="psp.example.com")
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://127.0.0.1:4318")
    ap.add_argument("--normal", type=int, default=12)
    ap.add_argument("--slow", type=int, default=4)
    ap.add_argument("--failing", type=int, default=3)
    ap.add_argument("--spread", type=float, default=60, help="seconds back in time to spread them over")
    a = ap.parse_args()
    kinds = ["normal"] * a.normal + ["slow"] * a.slow + ["failing"] * a.failing
    random.shuffle(kinds)
    now = time.time_ns()
    for i, k in enumerate(kinds):
        t = checkout(k, now - int((a.spread * (len(kinds) - i) / len(kinds)) * 1e9))
        req = urllib.request.Request(f"{a.endpoint}/v1/traces", data=json.dumps(t.payload()).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    print(f"sent {len(kinds)} traces ({a.normal} normal, {a.slow} slow database, {a.failing} failing payment) to {a.endpoint}")


if __name__ == "__main__":
    main()
