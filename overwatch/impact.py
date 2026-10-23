"""Blast radius: when it started, what it touches, what is at risk, and what it is costing.

All of it is computed from data, not from the model: start time from the Watcher's history,
spread from the configured service topology, cost from the configured impact queries. The only
model input is which service the root cause lives in.
"""

from __future__ import annotations

import operator
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .config import ImpactConfig
from .models import Anomaly
from .sources import DataSource, SourceError

_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "x_normal": operator.gt}


def incident_start(anomalies: list[Anomaly], history: dict[str, deque], detected_at: float) -> float:
    """Earliest sample of the unbroken run of bad values that led to each anomaly."""
    start = detected_at
    for a in anomalies:
        samples = list(history.get(a.check, []))
        if not samples:
            continue

        def bad(v: float | None) -> bool:
            if a.comparison in ("no_data", "error"):
                return v is None
            return v is not None and a.comparison in _OPS and _OPS[a.comparison](v, a.threshold)

        earliest = None
        for t, v in reversed(samples):
            if not bad(v):
                break
            earliest = t
        if earliest is not None:
            start = min(start, earliest)
    return start


def spread(topology: dict[str, list[str]], origin: str | None, failing: set[str]) -> dict[str, Any]:
    """Who is affected, who is at risk, and which call edges carry the failure.

    topology maps a service to the services it calls. Everything that (transitively) calls the
    origin can be hurt by it; those not already failing are "at risk".
    """
    callers: dict[str, set[str]] = {}
    for svc, deps in topology.items():
        for d in deps:
            callers.setdefault(d, set()).add(svc)

    affected = set(failing) | ({origin} if origin else set())
    exposed: set[str] = set()
    if origin:
        todo = [origin]
        while todo:
            for caller in callers.get(todo.pop(), ()):
                if caller not in exposed:
                    exposed.add(caller)
                    todo.append(caller)
    at_risk = exposed - affected

    hot = affected | exposed
    edges = [
        {"from": svc, "to": d}
        for svc, deps in topology.items()
        for d in deps
        if svc in hot and (d == origin or d in exposed)
    ]
    return {
        "origin": origin,
        "affected": sorted(affected),
        "at_risk": sorted(at_risk),
        "edges": edges,
    }


def timestamp_literal(iso: str, source: DataSource) -> str:
    """A timestamp the source's SQL dialect accepts: Postgres casts, Zetaris (Spark SQL) converts."""
    return f"to_timestamp('{iso}')" if source.__class__.__name__ == "ZetarisSource" else f"'{iso}'::timestamptz"


def measure(sources: dict[str, DataSource], impacts: list[ImpactConfig], since: float) -> list[dict[str, Any]]:
    """Run each impact query from the incident start. `{since}` becomes a timestamp literal."""
    iso = datetime.fromtimestamp(since, timezone.utc).isoformat()
    out = []
    for imp in impacts:
        item: dict[str, Any] = {"name": imp.name, "label": imp.label, "unit": imp.unit, "value": None}
        try:
            rows = sources[imp.source].query(imp.query.replace("{since}", timestamp_literal(iso, sources[imp.source])))
            value = next((v for v in (rows[0].values() if rows else []) if isinstance(v, (int, float))), None)
            item["value"] = round(float(value), 2) if value is not None else None
        except (SourceError, KeyError) as e:
            item["error"] = str(e)
        out.append(item)
    return out
