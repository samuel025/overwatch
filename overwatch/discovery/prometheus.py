"""Discover services and standard request metrics from Prometheus (RED: rate, errors, duration)."""

from __future__ import annotations

import time
from typing import Any

import httpx

REQUEST_COUNTERS = ("http_requests_total", "http_server_requests_seconds_count",
                    "http_server_request_duration_seconds_count", "requests_total")
SERVICE_LABELS = ("service", "service_name", "app", "job")
STATUS_LABELS = ("status", "code", "status_code", "http_status_code", "http_response_status_code")


def _get(client: httpx.Client, path: str, **params) -> Any:
    r = client.get(path, params=params)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", "prometheus error"))
    return body["data"]


def discover_prometheus(url: str, name: str = "prom", transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    with httpx.Client(base_url=url.rstrip("/"), timeout=10, transport=transport) as client:
        metrics = set(_get(client, "/api/v1/label/__name__/values"))
        counter = next((m for m in REQUEST_COUNTERS if m in metrics), None)
        histogram = None
        if counter:
            base = counter.removesuffix("_total").removesuffix("_count")
            histogram = next((m for m in metrics if m.startswith(base.replace("_requests", "_request")) and m.endswith("_bucket")), None) \
                or next((m for m in metrics if "duration_seconds_bucket" in m and "http" in m), None)
        series = _get(client, "/api/v1/series", **{"match[]": counter, "start": time.time() - 600}) if counter else []
        jobs = _get(client, "/api/v1/label/job/values") if "up" in metrics else []

    labels = set().union(*(s.keys() for s in series)) if series else set()
    svc_label = next((lbl for lbl in SERVICE_LABELS if lbl in labels), None)
    status_label = next((lbl for lbl in STATUS_LABELS if lbl in labels), None)
    services = sorted({s[svc_label] for s in series if s.get(svc_label)}) if svc_label else []

    checks = []
    for svc in services:
        key = svc.replace("-", "_").replace(".", "_")
        sel = f'{svc_label}="{svc}"'
        if histogram:
            checks.append({
                "name": f"{key}_p95_latency_ms", "service": svc, "source": name,
                "query": f"histogram_quantile(0.95, sum(rate({histogram}{{{sel}}}[2m])) by (le)) * 1000",
                "relative_factor": 4, "relative_min": 200, "no_data": "alert", "severity": "high",
                "description": f"{svc} p95 latency (ms)", "why": f"{histogram}: alerts at 4x its own normal",
            })
        if status_label:
            checks.append({
                "name": f"{key}_error_rate", "service": svc, "source": name,
                "query": f'sum(rate({counter}{{{sel},{status_label}=~"5.."}}[2m])) / sum(rate({counter}{{{sel}}}[2m]))',
                "threshold": 0.05, "severity": "high", "description": f"{svc} share of requests failing (5xx)",
                "why": f"{counter} by {status_label}: alerts above 5% errors",
            })
    for job in jobs:
        checks.append({
            "name": f"{job.replace('-', '_')}_up", "kind": "heartbeat", "service": job, "source": name,
            "query": f'sum(up{{job="{job}"}})', "comparison": "<", "threshold": 1, "severity": "critical",
            "description": f"no {job} target is reachable by Prometheus", "why": "Prometheus `up` for the job",
        })

    interesting = sorted(m for m in metrics if not m.startswith(("go_", "process_", "promhttp_", "prometheus_", "scrape_")))
    catalog = [f"{name} (PromQL, instant queries; use rate(...[2m]) for counters):"]
    if counter:
        catalog.append(f"  {counter}{{{svc_label or '...'}, {status_label or '...'}, ...}}: request counter")
    if histogram:
        catalog.append(f"  {histogram}{{{svc_label or '...'}, le}}: request duration histogram")
    if "up" in metrics:
        catalog.append("  up{job, instance}: 1 = target reachable, 0 = down")
    catalog.append("  other metrics: " + ", ".join(interesting[:40]) + (" ..." if len(interesting) > 40 else ""))
    return {
        "name": name, "services": services, "checks": checks, "catalog": "\n".join(catalog),
        "notes": [] if counter else [f"{name}: no standard request metrics found; add checks for your own metrics."],
    }
