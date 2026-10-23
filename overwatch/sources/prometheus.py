"""Prometheus (or any Prometheus-compatible API such as VictoriaMetrics, Mimir, Thanos)."""

from __future__ import annotations

from typing import Any

import httpx

from .base import DataSource, SourceError


class PrometheusSource(DataSource):
    language = "PromQL"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        if not cfg.get("url"):
            raise SourceError(f"source '{name}' needs a 'url'")
        headers = {}
        if cfg.get("bearer_token"):
            headers["Authorization"] = f"Bearer {cfg['bearer_token']}"
        self.client = httpx.Client(
            base_url=cfg["url"].rstrip("/"),
            headers=headers,
            timeout=float(cfg.get("timeout_seconds", 10)),
        )

    def query(self, q: str) -> list[dict[str, Any]]:
        try:
            r = self.client.get("/api/v1/query", params={"query": q})
            body = r.json()
        except Exception as e:
            raise SourceError(f"prometheus request failed: {e}") from e
        if body.get("status") != "success":
            raise SourceError(body.get("error", f"HTTP {r.status_code}"))
        data = body["data"]
        if data["resultType"] == "scalar":
            return [{"value": float(data["result"][1])}]
        rows = []
        for item in data["result"][: self.max_rows]:
            value = item.get("value") or (item.get("values") or [[None, None]])[-1]
            rows.append({**item.get("metric", {}), "value": float(value[1]) if value[1] is not None else None})
        return rows

    def close(self) -> None:
        self.client.close()
