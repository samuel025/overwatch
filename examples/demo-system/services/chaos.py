"""Chaos service: the world outside the shop.

It plays the external card processor (psp.example.com) that payments calls, writes the steady
trickle of unrelated releases and config changes every real company has, and runs background
jobs that cause trouble without any service's code changing.

  POST /jobs/{name}/start   (optional JSON params)
  POST /jobs/{name}/stop
  POST /stop_all
  GET  /jobs

  POST /psp/charge          the card processor; POST /psp/admin {rate_limit, latency_ms} changes it

Jobs:
  lock_holder  - an "inventory-sync" session takes a table lock on payments and sits idle in transaction
  report_job   - a "nightly revenue report" hammers shopdb with heavy aggregations
  flash_sale   - a traffic surge against the gateway (real users, not a bug)
"""

from __future__ import annotations

import asyncio
import os
import random
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

SHOP_DSN = os.environ.get("SHOP_DSN", "postgresql://postgres:postgres@postgres:5432/shopdb")
OPS_DSN = os.environ.get("OPS_DSN", "postgresql://postgres:postgres@postgres:5432/opsdb")
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")

running: dict[str, list[asyncio.Task]] = {}
psp = {"rate_limit": 0.0, "latency_ms": 40}  # external processor behaviour

# Harmless changes from other teams: the background noise an investigator has to see past.
NOISE_CHANGES = [
    ("search", "v4.{n}.0", "search-team", "Release 4.{n}.0"),
    ("notifications", "v2.{n}.1", "growth", "Release 2.{n}.1"),
    ("auth", "config-{c}", "infra-bot", "config sync from infra-repo@{h}"),
    ("web", "v12.{n}.0", "frontend", "Release 12.{n}.0 (PR #{pr})"),
    ("recommendations", "v0.{n}.3", "ml-platform", "Model refresh"),
    ("gateway", "config-{c}", "infra-bot", "config sync from infra-repo@{h}"),
]


async def noise() -> None:
    while True:
        await asyncio.sleep(random.uniform(60, 180))
        service, version, author, desc = random.choice(NOISE_CHANGES)
        fill = {"n": random.randint(10, 60), "c": random.randint(100, 999), "h": f"{random.getrandbits(24):06x}",
                "pr": random.randint(2000, 9999)}
        try:
            async with await psycopg.AsyncConnection.connect(OPS_DSN, autocommit=True) as conn:
                await conn.execute(
                    "INSERT INTO deploys (service, version, author, description) VALUES (%s, %s, %s, %s)",
                    (service, version.format(**fill), author, desc.format(**fill)),
                )
        except Exception as e:
            print(f"noise: {e}", flush=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(noise()) if os.environ.get("NOISE", "1") == "1" else None
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.post("/psp/charge")
async def psp_charge(body: dict):
    await asyncio.sleep(random.uniform(0.5, 1.5) * psp["latency_ms"] / 1000)
    if random.random() < psp["rate_limit"]:
        return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"retry-after": "30"})
    return {"id": f"ch_{random.getrandbits(40):010x}", "status": "authorized", "amount": body.get("amount")}


@app.post("/psp/admin")
async def psp_admin(body: dict):
    for k, v in body.items():
        if k in psp:
            psp[k] = type(psp[k])(v)
    return psp


async def ops_log(service: str, level: str, message: str) -> None:
    async with await psycopg.AsyncConnection.connect(OPS_DSN, autocommit=True) as conn:
        await conn.execute("INSERT INTO app_logs (service, level, message) VALUES (%s, %s, %s)", (service, level, message))


async def lock_holder(params: dict) -> None:
    conn = await psycopg.AsyncConnection.connect(SHOP_DSN, application_name="inventory-sync")
    try:
        await conn.execute("LOCK TABLE payments IN SHARE MODE")  # blocks every INSERT into payments
        await ops_log("inventory-sync", "INFO", "reconciling payments with warehouse ledger")
        await asyncio.Event().wait()  # idle in transaction until stopped
    finally:
        await conn.close()


async def report_worker() -> None:
    async with await psycopg.AsyncConnection.connect(
        SHOP_DSN, autocommit=True, application_name="nightly-revenue-report"
    ) as conn:
        while True:
            await conn.execute(
                "SELECT customer_id, sum(amount), count(*), max(created_at) FROM orders "
                "WHERE status <> 'refunded' GROUP BY customer_id ORDER BY 2 DESC LIMIT 100"
            )


async def report_job(params: dict) -> None:
    await ops_log("reports", "INFO", "revenue report job started")
    await asyncio.gather(*(report_worker() for _ in range(int(params.get("workers", 8)))))


async def flash_sale(params: dict) -> None:
    rps = float(params.get("rps", 250))
    slots = asyncio.Semaphore(int(params.get("max_in_flight", 300)))
    async with httpx.AsyncClient(timeout=10, limits=httpx.Limits(max_connections=300)) as client:
        async def one() -> None:
            try:
                await client.get(f"{GATEWAY}/checkout", params={"customer_id": random.randint(1, 50000)})
            except httpx.HTTPError:
                pass
            finally:
                slots.release()

        while True:
            if not slots.locked():  # shoppers give up instead of queueing forever
                await slots.acquire()
                asyncio.create_task(one())
            await asyncio.sleep(random.expovariate(rps))


JOBS = {"lock_holder": lock_holder, "report_job": report_job, "flash_sale": flash_sale}


async def stop(name: str) -> None:
    for t in running.pop(name, []):
        t.cancel()
    await asyncio.sleep(0.1)


@app.post("/jobs/{name}/start")
async def start_job(name: str, params: dict | None = None):
    if name not in JOBS:
        raise HTTPException(404, f"unknown job {name}; known: {', '.join(JOBS)}")
    await stop(name)
    running[name] = [asyncio.create_task(JOBS[name](params or {}))]
    return {"started": name}


@app.post("/jobs/{name}/stop")
async def stop_job(name: str):
    await stop(name)
    return {"stopped": name}


@app.post("/stop_all")
async def stop_all():
    for name in list(running):
        await stop(name)
    psp.update(rate_limit=0.0, latency_ms=40)
    return {"stopped": "all"}


@app.get("/jobs")
async def jobs():
    return {name: [("done" if t.done() else "running") for t in ts] for name, ts in running.items()}
