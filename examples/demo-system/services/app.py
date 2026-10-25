"""Demo shop microservice. SERVICE=gateway|orders|payments picks the role; INSTANCE names the replica.

Each instance records request latency and errors and flushes per-endpoint aggregates to
opsdb.request_metrics every FLUSH_SECONDS; errors also go to opsdb.app_logs (buffered, like a
log shipper). A small background rate of harmless errors keeps the baseline realistic.

POST /admin/fault lets scripts/inject_fault.py change behavior at runtime. Fault knobs are
saved to FAULT_FILE so they survive a container restart (a memory leak becomes a crash loop).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

SERVICE = os.environ["SERVICE"]
INSTANCE = os.environ.get("INSTANCE", SERVICE)
SHOP_DSN = os.environ.get("SHOP_DSN", "postgresql://postgres:postgres@postgres:5432/shopdb")
OPS_DSN = os.environ.get("OPS_DSN", "postgresql://postgres:postgres@postgres:5432/opsdb")
ORDERS_URL = os.environ.get("ORDERS_URL", "http://orders:8000")
# Client-side load balancing across payments replicas.
PAYMENTS_URLS = os.environ.get("PAYMENTS_URLS", "http://payments-a:8000,http://payments-b:8000").split(",")
PROVIDER_URL = os.environ.get("PROVIDER_URL", "http://chaos:8000/psp")  # the external card processor
FLUSH_SECONDS = float(os.environ.get("FLUSH_SECONDS", "10"))
FAULT_FILE = Path(os.environ.get("FAULT_FILE", "/tmp/fault.json"))

# Runtime fault knobs, changed through /admin/fault.
DEFAULT_FAULT = {
    "error_rate": 0.0,          # fraction of requests that fail with an unhandled exception
    "noise_error_rate": 0.003,  # harmless background errors every real service has
    "extra_latency_ms": 0,      # added to every request
    "hold_connection_ms": 0,    # payments: slow statement while holding a pool connection
    "pool_max": 10,             # orders/payments: DB connection pool size
    "orders_timeout_ms": 1000,  # gateway: timeout calling orders
    "payments_timeout_ms": 1000,  # gateway: timeout calling payments
    "retries": 0,               # gateway: immediate retries on failed payment calls (no backoff)
    "n_plus_one": 0,            # orders: 1 = load each order with its own query
    "leak_kb": 0,               # KB retained forever per request (memory leak)
    "leak_connection_rate": 0.0,  # payments: chance per request of never returning a DB connection
    "provider_cert_expired": 0,   # payments: TLS to the card processor fails
}
fault = dict(DEFAULT_FAULT)
if FAULT_FILE.exists():
    fault.update(json.loads(FAULT_FILE.read_text()))

NOISE = [
    "client disconnected before response was sent",
    "request body read timed out after 30s",
    "upstream connection reset by peer, retried",
    "invalid JSON in request body",
]

samples: dict[str, list[tuple[float, bool]]] = defaultdict(list)  # endpoint -> [(ms, is_error)]
log_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=2000)
logs_dropped = 0
leaked: list[bytes] = []
leaked_connections: list = []
shop_pool: AsyncConnectionPool | None = None
ops_pool: AsyncConnectionPool | None = None
http: httpx.AsyncClient | None = None


async def log(level: str, message: str) -> None:
    """Buffered: under a flood, lines are dropped and counted, never allowed to block requests."""
    global logs_dropped
    try:
        log_queue.put_nowait((level, message[:500]))
    except asyncio.QueueFull:
        logs_dropped += 1


async def ship_logs() -> None:
    global logs_dropped
    while True:
        await asyncio.sleep(1)
        batch = []
        while not log_queue.empty() and len(batch) < 500:
            batch.append(log_queue.get_nowait())
        if logs_dropped:
            batch.append(("WARN", f"log buffer full: dropped {logs_dropped} log lines"))
            logs_dropped = 0
        if not batch:
            continue
        for level, message in batch[:3]:
            print(f"[{INSTANCE}] {level} {message}", flush=True)
        try:
            async with ops_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO app_logs (service, instance, level, message) VALUES (%s, %s, %s, %s)",
                        [(SERVICE, INSTANCE, level, message) for level, message in batch],
                    )
        except Exception as e:
            print(f"[{INSTANCE}] log shipping failed: {e}", flush=True)


async def fail(message: str):
    await log("ERROR", message)
    raise HTTPException(500, message)


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


async def flush_metrics() -> None:
    while True:
        await asyncio.sleep(FLUSH_SECONDS)
        batch = {k: v for k, v in samples.items() if v}
        samples.clear()
        if not batch:
            continue
        rows = []
        for endpoint, vals in batch.items():
            ms = [v[0] for v in vals]
            errors = sum(1 for v in vals if v[1])
            rows.append((SERVICE, INSTANCE, endpoint, len(vals), len(vals) / FLUSH_SECONDS,
                         percentile(ms, 0.5), percentile(ms, 0.95), errors / len(vals)))
        try:
            async with ops_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO request_metrics "
                        "(ts, service, instance, endpoint, requests, rps, p50_ms, p95_ms, error_rate)"
                        " VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s)",
                        rows,
                    )
        except Exception as e:
            print(f"[{INSTANCE}] metrics flush failed: {e}", flush=True)


async def make_shop_pool(max_size: int) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        SHOP_DSN, min_size=1, max_size=max_size, timeout=2.0, open=False,
        kwargs={"application_name": f"shop-{INSTANCE}"},
    )
    await pool.open()
    return pool


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global shop_pool, ops_pool, http
    ops_pool = AsyncConnectionPool(OPS_DSN, min_size=1, max_size=3, open=False)
    await ops_pool.open()
    if SERVICE in ("orders", "payments"):
        shop_pool = await make_shop_pool(fault["pool_max"])
    http = httpx.AsyncClient(limits=httpx.Limits(max_connections=100, max_keepalive_connections=50))
    tasks = [asyncio.create_task(flush_metrics()), asyncio.create_task(ship_logs())]
    await log("INFO", f"{SERVICE} started (instance {INSTANCE}, pid {os.getpid()})")
    yield
    for t in tasks:
        t.cancel()
    await http.aclose()
    if shop_pool:
        await shop_pool.close()
    await ops_pool.close()


app = FastAPI(lifespan=lifespan)


tracer = None  # set by setup_tracing()


def db_span(name: str, statement: str):
    """A client span for one database call, so a trace shows the query and how long it took. Does nothing when tracing is off."""
    if tracer is None:
        return contextlib.nullcontext()
    from opentelemetry.trace import SpanKind

    return tracer.start_as_current_span(name, kind=SpanKind.CLIENT, attributes={"db.system": "postgresql", "db.name": "shopdb", "db.statement": statement})


def setup_tracing() -> None:
    """Optional OpenTelemetry traces: only when OTEL_EXPORTER_OTLP_ENDPOINT is set (see docker-compose.yml, profile `tracing`)."""
    global tracer
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE, "service.instance.id": INSTANCE}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("demo-shop")
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,admin", exclude_spans=["receive", "send"])
    HTTPXClientInstrumentor().instrument()


setup_tracing()


@app.middleware("http")
async def measure(request: Request, call_next):
    if request.url.path.startswith("/admin") or request.url.path == "/health":
        return await call_next(request)
    start = time.perf_counter()
    if fault["leak_kb"]:
        leaked.append(b"\x01" * (fault["leak_kb"] * 1024))  # written bytes, so it really is resident
    try:
        if fault["extra_latency_ms"]:
            await asyncio.sleep(fault["extra_latency_ms"] / 1000)
        if random.random() < fault["error_rate"]:
            raise HTTPException(500, f"unhandled exception in {request.url.path}: ValueError('unexpected token')")
        if random.random() < fault["noise_error_rate"]:
            raise HTTPException(500, random.choice(NOISE))
        response = await call_next(request)
        is_error = response.status_code >= 500
    except HTTPException as e:
        await log("ERROR", f"{request.method} {request.url.path} 500: {e.detail}")
        response, is_error = JSONResponse({"error": e.detail}, status_code=e.status_code), True
    samples[request.url.path].append(((time.perf_counter() - start) * 1000, is_error))
    return response


@app.get("/health")
async def health():
    return {
        "service": SERVICE, "instance": INSTANCE, "ok": True,
        "leaked_mb": round(sum(map(len, leaked)) / 2**20, 1), "leaked_connections": len(leaked_connections),
    }


@app.post("/admin/fault")
async def set_fault(body: dict):
    global shop_pool
    if body.get("reset"):
        fault.clear()
        fault.update(DEFAULT_FAULT)
        leaked.clear()
        for conn in leaked_connections:
            await shop_pool.putconn(conn)
        leaked_connections.clear()
    for k, v in body.items():
        if k in fault:
            fault[k] = type(DEFAULT_FAULT[k])(v)
    FAULT_FILE.write_text(json.dumps(fault))
    if ("pool_max" in body or body.get("reset")) and SERVICE in ("orders", "payments"):
        old, shop_pool = shop_pool, await make_shop_pool(fault["pool_max"])
        await old.close()
    return fault


# --- orders ----------------------------------------------------------------------

@app.get("/orders")
async def list_orders(customer_id: int):
    if SERVICE != "orders":
        raise HTTPException(404)
    async with shop_pool.connection() as conn:
        if fault["n_plus_one"]:
            # Load order details lazily: one query for ids, then one query per order.
            sql_ids = "SELECT id FROM orders WHERE customer_id = %s ORDER BY created_at DESC LIMIT 20"
            with db_span("SELECT shopdb.orders ids", sql_ids):
                cur = await conn.execute(sql_ids, (customer_id,))
                order_ids = await cur.fetchall()
            rows = []
            for (order_id,) in order_ids:
                sql_one = ("SELECT o.id, o.amount, o.status, o.created_at, "
                           "(SELECT count(*) FROM payments p WHERE p.order_id = o.id) AS payments FROM orders o WHERE o.id = %s")
                with db_span("SELECT shopdb.orders by id", sql_one):
                    c = await conn.execute(sql_one, (order_id,))
                    rows.append(await c.fetchone())
        else:
            sql = "SELECT id, amount, status, created_at FROM orders WHERE customer_id = %s ORDER BY created_at DESC LIMIT 20"
            with db_span("SELECT shopdb.orders", sql):
                cur = await conn.execute(sql, (customer_id,))
                rows = await cur.fetchall()
    return {"customer_id": customer_id, "orders": len(rows)}


# --- payments --------------------------------------------------------------------

async def charge_card(amount: float) -> None:
    """Authorize the card with the external processor (psp.example.com)."""
    if fault["provider_cert_expired"]:
        await fail("POST https://psp.example.com/v1/charge failed: [SSL: CERTIFICATE_VERIFY_FAILED] "
                   "certificate verify failed: certificate has expired (_ssl.c:1006)")
    try:
        r = await http.post(f"{PROVIDER_URL}/charge", json={"amount": amount}, timeout=2.0)
    except httpx.TimeoutException:
        await fail("POST https://psp.example.com/v1/charge timed out after 2.0s")
    if r.status_code == 429:
        await fail(f"POST https://psp.example.com/v1/charge returned 429 Too Many Requests "
                   f"(retry-after: {r.headers.get('retry-after', '?')}s)")
    if r.status_code >= 400:
        await fail(f"POST https://psp.example.com/v1/charge returned {r.status_code}")


@app.post("/pay")
async def pay(body: dict):
    if SERVICE != "payments":
        raise HTTPException(404)
    amount = body.get("amount", 10)
    await charge_card(amount)
    try:
        if random.random() < fault["leak_connection_rate"]:
            # The bug: an early-return path checks out a connection and never gives it back.
            leaked_connections.append(await shop_pool.getconn())
        async with shop_pool.connection() as conn:
            if fault["hold_connection_ms"]:
                await conn.execute("SELECT pg_sleep(%s)", (fault["hold_connection_ms"] / 1000,))
            sql_pay = "INSERT INTO payments (order_id, amount, status) VALUES (%s, %s, 'captured')"
            with db_span("INSERT shopdb.payments", sql_pay):
                await conn.execute(sql_pay, (body.get("order_id", 0), amount))
    except Exception as e:
        if "timeout" in str(e).lower() or e.__class__.__name__ == "PoolTimeout":
            await fail("PoolTimeout: couldn't get a connection after 2.00 sec")
        raise
    return {"status": "captured"}


# --- gateway ---------------------------------------------------------------------

async def call(method: str, url: str, timeout_ms: int, **kw) -> httpx.Response:
    r = await http.request(method, url, timeout=timeout_ms / 1000, **kw)
    r.raise_for_status()
    return r


@app.get("/checkout")
async def checkout(customer_id: int):
    if SERVICE != "gateway":
        raise HTTPException(404)
    try:
        await call("GET", f"{ORDERS_URL}/orders", fault["orders_timeout_ms"], params={"customer_id": customer_id})
    except httpx.TimeoutException:
        await fail(f"upstream request timeout: orders ({fault['orders_timeout_ms']}ms)")
    except httpx.HTTPStatusError as e:
        await fail(f"upstream orders returned {e.response.status_code}")
    except httpx.TransportError as e:
        await fail(f"upstream connect error: orders ({e.__class__.__name__})")

    attempts = 1 + fault["retries"]
    for attempt in range(1, attempts + 1):
        target = random.choice(PAYMENTS_URLS)
        name = target.split("//")[1].split(":")[0]
        try:
            await call("POST", f"{target}/pay", fault["payments_timeout_ms"], json={"order_id": customer_id, "amount": 42})
            return {"ok": True}
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as e:
            if attempt < attempts:
                continue  # immediate retry, no backoff
            if isinstance(e, httpx.TimeoutException):
                await fail(f"upstream request timeout: {name} ({fault['payments_timeout_ms']}ms)")
            if isinstance(e, httpx.HTTPStatusError):
                await fail(f"upstream {name} returned {e.response.status_code}")
            await fail(f"upstream connect error: {name} ({e.__class__.__name__})")
