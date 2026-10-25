"""Discover services, the real call graph, and checks from a trace store (Grafana Tempo or Jaeger).

Traces know what Docker Compose and labels can only guess: which service actually called which, across servers, including the
databases and outside APIs on the other end. The topology comes from there, not from a file anyone has to keep up to date.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx

from ..sources.traces import TracesSource


def _is_host(name: str) -> bool:
    if ":" in name or "." not in name:  # "postgresql:shopdb" is a database, a bare word is a service name
        return False
    try:
        ipaddress.ip_address(name)
        return False
    except ValueError:
        return True


def discover_traces(url: str, name: str = "traces", tenant: str = "", token: str = "", verify: bool | str = True,
                    transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    cfg = {k: v for k, v in {"url": url, "tenant": tenant, "bearer_token": token}.items() if v}
    cfg["verify"] = verify
    src = TracesSource(name, cfg)
    if transport is not None:
        src.http = httpx.Client(base_url=url, transport=transport)
    try:
        be = src._be()
        services = sorted(s for s in be.services() if s and s != "unknown_service")[:40]
        if not services:
            raise RuntimeError("the trace store has no services yet: send it some OpenTelemetry traces first")
        graph = src.query("graph 60")
        kind = src.kind
    finally:
        src.close()

    known = set(services)
    topology: dict[str, list[str]] = {s: [] for s in services}
    log_hits: dict[str, dict[str, int]] = {}  # outside hosts a service calls: suggested as external services
    databases: dict[str, list[str]] = {}
    for e in graph:
        a, b = e["caller"], e["callee"]
        if a in known and b in known:
            topology[a] = sorted({*topology[a], b})
        elif a in known and _is_host(b):
            log_hits.setdefault(a, {})[b] = e["calls"]
        elif a in known and b.count(":") == 1:
            databases.setdefault(a, []).append(b)

    checks = []
    for svc in services:
        key = svc.replace("-", "_").replace(".", "_")
        checks.append({
            "name": f"{key}_trace_p95_ms", "service": svc, "source": name, "query": f"p95 {svc} 5", "relative_factor": 4,
            "relative_min": 50, "severity": "high", "description": f"{svc} request time (95th percentile of its traced requests), last 5 minutes",
            "why": "from traces: alerts at 4x the service's own normal, so no threshold to guess"})
        checks.append({
            "name": f"{key}_trace_error_pct", "service": svc, "source": name, "query": f"error_pct {svc} 2", "threshold": 5,
            "comparison": ">", "severity": "high", "description": f"share of recent requests through {svc} that failed (last 2 minutes)",
            "why": "from traces: more than 5% of requests failing; a few stray errors in busy traffic do not count"})
    evidence = [
        {"name": f"{name}_slowest", "label": "Slowest traces, last 5 minutes (with the span that took the time)", "source": name, "query": "slow * 5 0 5"},
        {"name": f"{name}_failing", "label": "Failing traces, last 5 minutes (with the span where the failure began)", "source": name, "query": "errors * 5 5"},
        {"name": f"{name}_hotspots", "label": "Where requests spend their time, last 5 minutes (repeats per request flagged)", "source": name, "query": "hotspots * 5"},
    ]
    catalog = (f"{name} ({kind.capitalize()}, distributed traces: one trace is one request across services). Commands: "
               "`slow <service|*> [minutes] [min_ms] [limit]` lists the slowest traces with the span that took the time; "
               "`errors <service|*> [minutes] [limit]` lists failing traces with the span where the failure began and its message; "
               "`trace <trace_id>` shows one request span by span (who called whom, each span's own time and share of the request); "
               "`graph [minutes]` shows who calls whom with call counts, errors and latency; "
               "`hotspots <service|*> [minutes]` ranks operations by the time they really spend and says how often each repeats inside one request (a high repeat count is an N+1 query pattern). "
               f"Services: {', '.join(services)}. Start from `slow` or `errors`, then open one `trace`: it names the cause directly.")
    return {
        "name": name, "kind": kind, "services": services, "topology": {k: v for k, v in topology.items() if v}, "checks": checks,
        "evidence": evidence, "catalog": catalog, "log_hits": log_hits, "databases": databases, "internal": sorted(known),
        "source": {k: v for k, v in {"type": "traces", "kind": kind, "url": url, "tenant": tenant, "bearer_token": token}.items() if v}
                  | ({"verify": verify} if verify is not True else {}),
    }
