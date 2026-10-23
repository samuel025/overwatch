"""Discover services and error-log checks from Loki: no log table, no agent, just the labels it already has."""

from __future__ import annotations

from typing import Any

import httpx

from ..sources.loki import LokiSource

SERVICE_LABELS = ("service", "service_name", "app", "app_kubernetes_io_name", "container", "job")
ERROR_FILTER = '|~ "(?i)error|exception|fatal|panic|timeout"'


def discover_loki(url: str, name: str = "loki", tenant: str = "", token: str = "",
                  transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    cfg = {k: v for k, v in {"url": url, "tenant": tenant, "bearer_token": token}.items() if v}
    src = LokiSource(name, cfg)
    if transport is not None:
        src.client = httpx.Client(base_url=src.client.base_url, transport=transport)
    try:
        labels = src.labels()
        label = next((l for l in SERVICE_LABELS if l in labels), None)
        if not label:
            raise RuntimeError(f"Loki has no label that names a service (looked for {', '.join(SERVICE_LABELS)}; "
                               f"found: {', '.join(labels) or 'none'})")
        services = sorted(v for v in src.label_values(label) if v)[:40]
    finally:
        src.close()

    checks = []
    for svc in services:
        key = svc.replace("-", "_").replace(".", "_")
        checks.append({
            "name": f"{key}_log_errors", "service": svc, "source": name,
            "query": f'sum(count_over_time({{{label}="{svc}"}} {ERROR_FILTER} [2m]))',
            "relative_factor": 4, "relative_min": 5, "severity": "high",
            "description": f"{svc} error log lines, last 2 minutes",
            "why": "error lines in Loki: alerts at 4x the service's own normal (and at least 5), no threshold to guess",
        })
    evidence = [{
        "name": f"{name}_error_volume", "label": "Error log lines per service, last 5 minutes", "source": name,
        "query": f'sum by ({label}) (count_over_time({{{label}=~".+"}} {ERROR_FILTER} [5m]))',
    }]
    catalog = (f"{name} (Loki, LogQL): logs are labelled by {label} (services: {', '.join(services)}). "
               f'Log lines: {{{label}="payments"}} |= "timeout". Counts: sum(count_over_time({{{label}="payments"}} '
               f'|= "timeout" [5m])).')
    return {
        "name": name, "label": label, "services": services, "checks": checks, "evidence": evidence, "catalog": catalog,
        "source": {k: v for k, v in {"type": "loki", "url": url, "tenant": tenant, "bearer_token": token}.items() if v},
    }
