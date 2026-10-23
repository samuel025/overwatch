"""Container metrics collector: what cAdvisor / the kubelet would report in a real cluster.

Every INTERVAL seconds it reads CPU, memory, restart count and OOM-kill state for each shop
container from the Docker API (read-only socket) and writes them to opsdb.container_metrics.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import psycopg

OPS_DSN = os.environ.get("OPS_DSN", "postgresql://postgres:postgres@postgres:5432/opsdb")
PROJECT = os.environ.get("COMPOSE_PROJECT", "demo-system")
INTERVAL = float(os.environ.get("INTERVAL", "10"))
WATCHED = {"gateway", "orders", "payments", "postgres"}


def cpu_percent(stats: dict) -> float:
    cpu, pre = stats["cpu_stats"], stats.get("precpu_stats", {})
    cpu_delta = cpu["cpu_usage"]["total_usage"] - pre.get("cpu_usage", {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    if system_delta <= 0 or cpu_delta < 0:
        return 0.0
    return cpu_delta / system_delta * cpu.get("online_cpus", 1) * 100  # percent of one core


def memory_mb(stats: dict) -> float:
    mem = stats.get("memory_stats", {})
    cache = mem.get("stats", {}).get("inactive_file", 0)  # page cache is reclaimable, like `docker stats`
    return max(0, mem.get("usage", 0) - cache) / 2**20


async def sample(docker: httpx.AsyncClient, container: dict) -> tuple | None:
    labels = container["Labels"]
    service = labels.get("shop.service") or labels.get("com.docker.compose.service", "")
    if service not in WATCHED:
        return None
    instance = labels.get("shop.instance") or labels.get("com.docker.compose.service", service)
    info = (await docker.get(f"/containers/{container['Id']}/json")).json()
    status = info["State"]["Status"]
    cpu = mem = 0.0
    if status == "running":
        stats = (await docker.get(f"/containers/{container['Id']}/stats", params={"stream": "false"})).json()
        cpu, mem = cpu_percent(stats), memory_mb(stats)
    host = info["HostConfig"]
    return (
        service, instance, round(cpu, 1), (host.get("NanoCpus") or 0) / 1e9 or None, round(mem, 1),
        (host.get("Memory") or 0) / 2**20 or None, info["RestartCount"], info["State"]["OOMKilled"], status,
    )


async def main() -> None:
    transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
    filters = json.dumps({"label": [f"com.docker.compose.project={PROJECT}"]})
    async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=10) as docker:
        while True:
            try:
                containers = (await docker.get("/containers/json", params={"all": "true", "filters": filters})).json()
                rows = [r for r in await asyncio.gather(*(sample(docker, c) for c in containers)) if r]
                async with await psycopg.AsyncConnection.connect(OPS_DSN, autocommit=True) as conn:
                    async with conn.cursor() as cur:
                        await cur.executemany(
                            "INSERT INTO container_metrics (ts, service, instance, cpu_pct, cpu_limit_cores, mem_mb, "
                            "mem_limit_mb, restarts, oom_killed, status) VALUES (now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            rows,
                        )
            except Exception as e:
                print(f"collector: {e}", flush=True)
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
