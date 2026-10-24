"""Fault catalog for the demo shop, with ground truth for automatic grading.

Every fault knows how to break the system, how the problem surfaces (Watcher alert or a human
report), the true root cause, and keyword groups a correct diagnosis must mention. The eval uses
the same catalog, so adding a fault here adds it to the benchmark.

Realism rules: change records read like real ones ("Release 2.3.0", "config sync from
infra-repo@..."), never like the answer. Error messages are what the software would really log.
The cause must be found in the data (timing, metrics, database state, container state), and the
chaos service keeps adding unrelated changes from other teams in the background.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx
import psycopg

PG = os.environ.get("DEMO_PG", "postgresql://postgres:postgres@localhost:55432")
INSTANCES = {
    "gateway": "http://localhost:8001",
    "orders": "http://localhost:8002",
    "payments-a": "http://localhost:8003",
    "payments-b": "http://localhost:8005",
}
SERVICES = {"gateway": ["gateway"], "orders": ["orders"], "payments": ["payments-a", "payments-b"]}
CHAOS = os.environ.get("CHAOS_URL", "http://localhost:8004")
COMPOSE_DIR = Path(__file__).resolve().parent.parent


# --- primitives ----------------------------------------------------------------------

def sql(db: str, statement: str, params: tuple = ()) -> None:
    with psycopg.connect(f"{PG}/{db}", autocommit=True) as conn:
        conn.execute(statement, params)


def deploy(service: str, version: str, author: str, description: str) -> None:
    sql("opsdb", "INSERT INTO deploys (service, version, author, description) VALUES (%s, %s, %s, %s)",
        (service, version, author, description))


def knobs(target: str, **values) -> None:
    """Set fault knobs on a service (all its instances) or on one instance."""
    for instance in SERVICES.get(target, [target]):
        httpx.post(f"{INSTANCES[instance]}/admin/fault", json=values, timeout=10).raise_for_status()


def chaos(job: str, **params) -> None:
    httpx.post(f"{CHAOS}/jobs/{job}/start", json=params, timeout=10).raise_for_status()


def psp(**settings) -> None:
    httpx.post(f"{CHAOS}/psp/admin", json=settings, timeout=10).raise_for_status()


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=COMPOSE_DIR, check=True, capture_output=True)


def wait_for_db(seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            sql("opsdb", "SELECT 1")
            return
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError("database did not come back")


# --- catalog -----------------------------------------------------------------------

@dataclass
class Fault:
    name: str
    title: str
    category: str              # code | config | database | capacity | dependency | infrastructure | compound
    trigger: str               # "watcher" (a check fires) or "report" (a human notices first)
    truth: str                 # the real root cause, for humans and for the judge
    must_mention: list[list[str]]  # every group needs at least one keyword (case-insensitive)
    apply: Callable[[], None]
    report: str = ""           # symptom text for trigger == "report"
    past_ticket: str | None = None  # a similar incident exists in memory (OPS-xxx)
    note: str = ""
    settle_seconds: int = 45   # how long until the signal is clearly in the data
    extra_reset: list[Callable[[], None]] = field(default_factory=list)


def _slow_query():
    deploy("orders", "v1.8.0", "maria", "Release 1.8.0 (PR #2214, #2219, #2230)")
    sql("shopdb", "DROP INDEX IF EXISTS orders_customer_id_idx")


def _bad_deploy():
    deploy("payments", "v2.3.0", "dev-team", "Release 2.3.0")
    knobs("payments", error_rate=0.35)


def _pool_exhaustion():
    deploy("payments", "config-77", "infra-bot", "config sync from infra-repo@4be1f0")
    knobs("payments", pool_max=2, hold_connection_ms=400)


def _downstream_timeout():
    knobs("payments", extra_latency_ms=1500)


def _red_herring():
    deploy("gateway", "v3.1.5", "li", "Release 3.1.5")
    knobs("payments", extra_latency_ms=1500)


def _lock_contention():
    chaos("lock_holder")


def _config_timeout():
    deploy("gateway", "config-81", "infra-bot", "config sync from infra-repo@91d2aa")
    knobs("gateway", orders_timeout_ms=4)


def _retry_storm():
    deploy("gateway", "v3.2.0", "li", "Release 3.2.0 (resilience improvements)")
    knobs("gateway", retries=4)
    knobs("payments", error_rate=0.5)


def _n_plus_one():
    deploy("orders", "v1.9.0", "maria", "Release 1.9.0")
    knobs("orders", n_plus_one=1)


def _report_job():
    deploy("reports", "config-90", "finance-eng", "cron schedule update")
    chaos("report_job", workers=4)


def _flash_sale():
    chaos("flash_sale", rps=50, max_in_flight=100)


def _memory_leak():
    deploy("orders", "v1.9.1", "maria", "Release 1.9.1 (perf)")
    knobs("orders", leak_kb=300)


def _database_down():
    compose("stop", "postgres")


def _double_trouble():
    _slow_query()
    _bad_deploy()


def _service_down():
    compose("stop", "orders")


def _bad_replica():
    knobs("payments-b", error_rate=1.0)


def _connection_leak():
    deploy("payments", "v2.4.0", "dev-team", "Release 2.4.0")
    knobs("payments", leak_connection_rate=0.01)


def _provider_rate_limit():
    psp(rate_limit=0.4)


def _cert_expired():
    knobs("payments", provider_cert_expired=1)


FAULTS: dict[str, Fault] = {f.name: f for f in [
    Fault("slow_query", "Migration dropped an index", "code", "watcher",
          "orders v1.8.0 dropped orders_customer_id_idx; /orders does a sequential scan of 3M rows, "
          "orders p95 goes to seconds and the gateway times out calling orders.",
          [["index", "idx"], ["v1.8.0", "1.8.0", "migration", "deploy", "release"]], _slow_query, past_ticket="OPS-101"),
    Fault("bad_deploy", "Buggy release", "code", "watcher",
          "payments v2.3.0 fails ~35% of /pay requests with an unhandled ValueError; errors start right after it.",
          [["v2.3.0", "2.3.0", "deploy", "release"], ["payments"]], _bad_deploy, past_ticket="OPS-117"),
    Fault("pool_exhaustion", "Connection pool too small", "config", "watcher",
          "config-77 reduced the payments DB pool to 2 connections while statements hold them ~400ms; requests "
          "time out waiting for a connection (PoolTimeout).",
          [["pool", "connection"], ["config-77", "config", "size"]], _pool_exhaustion, past_ticket="OPS-130"),
    Fault("downstream_timeout", "Slow dependency", "dependency", "watcher",
          "payments responds ~1.5s slower (no deploy involved); gateway's 1s timeout to payments fires.",
          [["payments"], ["timeout", "slow", "latency"]], _downstream_timeout, past_ticket="OPS-122"),
    Fault("red_herring", "Innocent deploy at the wrong time", "dependency", "watcher",
          "payments is ~1.5s slow and the gateway times out calling it. The gateway v3.1.5 release at the same "
          "time is NOT the cause.",
          [["payments"], ["timeout", "slow", "latency"]], _red_herring, past_ticket="OPS-122",
          note="Tests whether the agent blames the most recent deploy without proof."),
    Fault("lock_contention", "Long transaction holding a lock", "database", "watcher",
          "An 'inventory-sync' session holds a SHARE lock on payments and sits idle in transaction; every payment "
          "INSERT waits on the lock, payments pool exhausts, gateway times out.",
          [["lock", "blocked", "blocking"], ["inventory-sync", "idle in transaction", "transaction"]],
          _lock_contention, note="No deploy, no past ticket: only pg_stat_activity / pg_locks explain it."),
    Fault("config_timeout", "Overly aggressive timeout", "config", "watcher",
          "config-81 lowered gateway->orders timeout to 4ms; orders itself is healthy (~10ms p95) but the gateway "
          "fails a large share of checkouts with upstream timeouts to orders.",
          [["timeout"], ["config-81", "config", "4ms", "4 ms"]], _config_timeout, settle_seconds=75,
          note="The service in the error message (orders) is not the one at fault."),
    Fault("retry_storm", "Retries amplify a failure", "compound", "watcher",
          "payments started failing ~50% and gateway v3.2.0's immediate retries (up to 4, no backoff) multiply "
          "load on payments (payments rps well above gateway rps).",
          [["retr"], ["payments"]], _retry_storm, note="Two causes interact: the failure and the amplifier."),
    Fault("n_plus_one", "N+1 queries after a release", "code", "watcher",
          "orders v1.9.0 loads each order with its own query plus a payment-count subquery on the unindexed "
          "payments table; queries per request jump ~20x and orders latency climbs.",
          [["n+1", "per order", "each order", "per-order", "query count", "calls", "many queries", "subquery"],
           ["v1.9.0", "1.9.0", "lazy"]],
          _n_plus_one, note="Evidence is in pg_stat_statements: call counts, not a single slow query."),
    Fault("report_job", "Batch job during business hours", "capacity", "watcher",
          "A revenue report job (config-90 cron change) runs 4 heavy aggregation sessions named "
          "nightly-revenue-report on shopdb during business hours, saturating the database CPU.",
          [["report", "nightly-revenue-report", "batch"], ["config-90", "schedule", "cron", "cpu", "saturat"]],
          _report_job, past_ticket="OPS-141"),
    Fault("flash_sale", "Real traffic surge", "capacity", "watcher",
          "Checkout traffic jumped from ~15 to ~65 rps (marketing flash sale); no code or config change. The "
          "gateway is CPU-saturated; this is load, not a bug.",
          [["traffic", "rps", "load", "surge", "requests per second", "volume"]], _flash_sale,
          note="The right answer is 'scale up', not 'roll back'."),
    Fault("memory_leak", "Memory leak and crash loop", "code", "watcher",
          "orders v1.9.1 caches order history in process memory without bounds; the container climbs to its "
          "256MB limit, is OOM-killed and restarts about every minute, causing bursts of checkout failures.",
          [["memory", "oom", "leak"], ["v1.9.1", "1.9.1", "cache", "restart"]], _memory_leak, settle_seconds=120,
          note="Evidence is a pattern over time: rising memory, OOM kills and restarts in container_metrics."),
    Fault("database_down", "Database unreachable", "infrastructure", "watcher",
          "The Postgres server is down; every service and every Overwatch check fails to connect.",
          [["database", "postgres", "db"], ["down", "unreachable", "unavailable", "connect", "refused"]],
          _database_down, settle_seconds=10, note="The agent's own data sources are broken; it must say so."),
    Fault("double_trouble", "Two unrelated failures at once", "compound", "watcher",
          "Two independent causes: orders v1.8.0 dropped orders_customer_id_idx (slow orders) AND payments v2.3.0 "
          "fails ~35% of payments.",
          [["index", "idx"], ["v2.3.0", "2.3.0", "payments"]], _double_trouble, past_ticket="OPS-101",
          note="A single-cause answer is only half right."),
    Fault("service_down", "Service not responding", "infrastructure", "watcher",
          "The orders service is down (its container stopped); it reports nothing and the gateway cannot connect "
          "to it, so every checkout fails.",
          [["orders"], ["down", "not responding", "stopped", "unreachable", "connect", "no data", "silent"]],
          _service_down, settle_seconds=40, note="Shown on the map as 'Not responding'."),
    Fault("bad_replica", "One bad instance", "infrastructure", "watcher",
          "Instance payments-b fails every request while payments-a is healthy; about half of payment calls "
          "fail. No deploy: one replica is bad and should be taken out of rotation.",
          [["payments-b", "instance", "replica", "pod", "node"]], _bad_replica,
          note="The service looks half-broken; only per-instance data shows it is one copy."),
    Fault("connection_leak", "Connection leak (gradual)", "code", "watcher",
          "payments v2.4.0 leaks DB connections (checked out, never returned); the pools drain over minutes, "
          "idle connections from shop-payments pile up in pg_stat_activity, then requests fail with PoolTimeout.",
          [["leak", "not returned", "never returned", "idle"], ["v2.4.0", "2.4.0", "connection"]], _connection_leak,
          settle_seconds=150, note="Starts small and gets worse: a trend, not a step."),
    Fault("provider_rate_limit", "Third-party rate limit", "dependency", "watcher",
          "The external card processor psp.example.com rejects ~40% of charges with 429 Too Many Requests; "
          "payments fails those requests. Nothing changed on our side.",
          [["429", "rate limit", "rate-limit", "too many requests"], ["psp", "provider", "processor", "third"]],
          _provider_rate_limit, note="The fix is outside the codebase: quota, backoff, or a second provider."),
    Fault("cert_expired", "Expired TLS certificate", "dependency", "watcher",
          "TLS to the card processor psp.example.com fails with 'certificate has expired'; every payment fails.",
          [["certificate", "cert", "tls", "ssl"], ["expired", "expir"]], _cert_expired,
          note="A classic outage cause; the log message is the evidence."),
]}


def _injected_deploys() -> list[tuple[str, str]]:
    """(service, version) of every deploy a fault records, read from this file's own deploy(...) calls."""
    import re
    return sorted(set(re.findall(r'\bdeploy\("([^"]+)",\s*"([^"]+)"', Path(__file__).read_text())))


def reset(clean_telemetry: bool = False) -> None:
    """Undo every fault. clean_telemetry also empties the demo's logs and metrics, so the next fault starts from a blank
    slate (the evaluation uses it: evidence windows of 5 to 15 minutes would otherwise include the previous fault)."""
    compose("start", "postgres")
    wait_for_db()
    # Faults record a deploy/config change; leaving them behind would mislead the next investigation
    # (it looks at the last 30 minutes of changes), so the next fault would blame the previous one.
    for service, version in _injected_deploys():
        sql("opsdb", "DELETE FROM deploys WHERE service = %s AND version = %s", (service, version))
    # Stop the faults' own jobs and release their locks FIRST: restarting the services while a lock is still held
    # leaves payments pool errors in the logs, which the next fault's investigation then reads as its own.
    try:
        httpx.post(f"{CHAOS}/stop_all", timeout=10)
    except httpx.HTTPError:
        pass  # the chaos service itself may be down (service_down)
    sql("shopdb", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                  "WHERE application_name IN ('inventory-sync', 'nightly-revenue-report')")
    compose("start")  # anything a fault stopped (service_down)
    # Fresh processes: drops stale DB connections (after database_down), leaked memory and connections.
    compose("restart", "gateway", "orders", "payments-a", "payments-b")
    httpx.post(f"{CHAOS}/stop_all", timeout=10)
    sql("shopdb", "CREATE INDEX IF NOT EXISTS orders_customer_id_idx ON orders (customer_id)")
    # Cumulative query statistics would still show the previous fault (e.g. an earlier DROP INDEX).
    sql("shopdb", "SELECT pg_stat_statements_reset()")
    if clean_telemetry:
        for table in ("app_logs", "request_metrics", "container_metrics"):
            sql("opsdb", f"DELETE FROM {table}")
    for instance in INSTANCES:
        for _ in range(30):  # instances take a moment to come back after the restart
            try:
                knobs(instance, reset=True)
                break
            except httpx.HTTPError:
                time.sleep(1)


def wait_until_healthy(watcher, timeout: int = 240, settle_polls: int = 3) -> bool:
    """Poll the Watcher until no check fires for `settle_polls` passes in a row (5s apart)."""
    deadline, clean = time.time() + timeout, 0
    while time.time() < deadline:
        clean = clean + 1 if not watcher.run() else 0
        if clean >= settle_polls:
            return True
        time.sleep(5)
    return False
