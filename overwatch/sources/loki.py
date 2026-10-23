"""Grafana Loki (and anything speaking its HTTP API) as a data source: application logs with no log table.

Queries are LogQL:
  {app="payments"} |= "error"                         log lines from the last `since_minutes` (default 15)
  sum(count_over_time({app="payments"} |= "error" [2m]))   a number, for checks and counts
The first form returns rows (ts, line, plus the stream's labels); the second returns the metric's value.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .base import DataSource, SourceError


class LokiSource(DataSource):
    language = "LogQL (Loki): {app=\"x\"} |= \"error\" for log lines, sum(count_over_time({app=\"x\"} |= \"error\" [2m])) for counts"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        if not cfg.get("url"):
            raise SourceError(f"source '{name}' needs a 'url' (the Loki address)")
        headers = {}
        if cfg.get("bearer_token"):
            headers["Authorization"] = f"Bearer {cfg['bearer_token']}"
        if cfg.get("tenant"):  # multi-tenant Loki
            headers["X-Scope-OrgID"] = str(cfg["tenant"])
        self.since_minutes = int(cfg.get("since_minutes", 15))
        self.client = httpx.Client(base_url=cfg["url"].rstrip("/"), headers=headers,
                                   timeout=float(cfg.get("timeout_seconds", 10)))

    @staticmethod
    def _is_metric(q: str) -> bool:
        return not q.lstrip().startswith("{")  # sum(...), rate(...), count_over_time(...)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.client.get(path, params=params)
        except Exception as e:
            raise SourceError(f"loki request failed: {e}") from e
        if r.status_code >= 400:
            raise SourceError(f"loki returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def query(self, q: str) -> list[dict[str, Any]]:
        q = q.strip()
        if not q:
            raise SourceError("empty LogQL query")
        if self._is_metric(q):
            data = self._get("/loki/api/v1/query", {"query": q})["data"]
            rows = []
            for item in (data.get("result") or [])[: self.max_rows]:
                value = item.get("value") or (item.get("values") or [[None, None]])[-1]
                rows.append({**item.get("metric", {}), "value": float(value[1]) if value[1] is not None else None})
            return rows
        end = datetime.now(timezone.utc)
        data = self._get("/loki/api/v1/query_range", {
            "query": q, "limit": self.max_rows, "direction": "backward",
            "start": int((end.timestamp() - self.since_minutes * 60) * 1e9), "end": int(end.timestamp() * 1e9),
        })["data"]
        rows = []
        for stream in data.get("result") or []:
            for ts, line in stream.get("values", []):
                rows.append({"ts": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(),
                             **stream.get("stream", {}), "line": line[:300]})
        rows.sort(key=lambda r: r["ts"])
        return rows[-self.max_rows:]

    def labels(self) -> list[str]:
        """Label names present in Loki: used by discovery to find the one that names a service."""
        return self._get("/loki/api/v1/labels", {}).get("data") or []

    def label_values(self, label: str) -> list[str]:
        return self._get(f"/loki/api/v1/label/{label}/values", {}).get("data") or []

    def close(self) -> None:
        self.client.close()
