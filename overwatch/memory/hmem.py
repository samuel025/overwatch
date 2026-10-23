"""Client for the H-MEM memory service in memory-service/ (Meterless H-MEM engine)."""

from __future__ import annotations

from typing import Any

import httpx

from ..models import Incident
from .base import Memory, incident_memory_text, incident_tags


class MemoryServiceError(Exception):
    pass


class HMEMMemory(Memory):
    name = "h-mem"

    def __init__(self, cfg: dict[str, Any]):
        headers = {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}
        self.client = httpx.Client(base_url=cfg.get("url", "http://127.0.0.1:8099"), headers=headers, timeout=10)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = self.client.post(path, json=body)
        except httpx.HTTPError as e:
            raise MemoryServiceError(f"memory service unreachable: {e}. Start it with `npm start` in memory-service/") from e
        data = r.json()
        if r.status_code != 200:
            raise MemoryServiceError(data.get("error", f"HTTP {r.status_code}"))
        return data

    def remember(self, incident: Incident) -> str | None:
        data = self._post(
            "/remember",
            {
                "content": incident_memory_text(incident),
                "source": f"overwatch:incident:{incident.id}",
                "tags": incident_tags(incident),
                "layer": "long_term",  # only human-approved incidents are stored
                "confidence": max(0.5, min(incident.confidence, 0.95)),
                "missionId": incident.id,
            },
        )
        return data["memory"]["id"]

    def recall(self, symptoms: str, top_n: int = 3) -> list[dict[str, Any]]:
        data = self._post("/recall", {"query": symptoms, "topN": top_n})
        return [
            {"id": m["id"], "content": m["content"], "relevance": round(m["relevance"], 3), "tags": m.get("tags", [])}
            for m in data["memories"]
        ]

    def feedback(self, memory_id: str, kind: str) -> None:
        self._post("/feedback", {"memoryId": memory_id, "kind": kind})

    def overview(self, limit: int = 20) -> dict[str, Any]:
        health = self.health()
        if not health.get("ok"):
            return {"health": health, "memories": [], "ledger": []}
        memories = self.client.get("/memories").json()["memories"]
        memories.sort(key=lambda m: m.get("lastAccessed", 0), reverse=True)
        keep = ("id", "content", "layer", "confidence", "accessCount", "source", "tags", "lastAccessed", "domain")
        ledger = self.client.get("/ledger", params={"limit": limit}).json()["entries"]
        return {
            "health": health,
            "memories": [{k: m.get(k) for k in keep} for m in memories[:limit]],
            "ledger": [
                {"memoryId": e["memoryId"], "action": e["action"], "actor": e["actor"], "timestamp": e["timestamp"],
                 "details": e.get("details")}
                for e in reversed(ledger)
            ],
        }

    def health(self) -> dict[str, Any]:
        try:
            return self.client.get("/healthz").json()
        except httpx.HTTPError as e:
            return {"ok": False, "engine": self.name, "error": str(e)}
