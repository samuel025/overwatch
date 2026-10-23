"""Dependency-free fallback memory (keyword overlap over a JSONL file).

Used when the H-MEM service is not running. `NoMemory` (`type: none`) is the "memory off" baseline for the eval.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from ..models import Incident
from .base import Memory, incident_memory_text, incident_tags


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) > 2}


class LocalMemory(Memory):
    name = "local"

    def __init__(self, cfg: dict[str, Any]):
        self.path = Path(cfg.get("path", "overwatch-memory.jsonl"))

    def _all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def remember(self, incident: Incident) -> str | None:
        rec = {"id": f"loc-{uuid.uuid4().hex[:6]}", "content": incident_memory_text(incident), "tags": incident_tags(incident)}
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec["id"]

    def recall(self, symptoms: str, top_n: int = 3) -> list[dict[str, Any]]:
        q = _tokens(symptoms)
        scored = []
        for rec in self._all():
            t = _tokens(rec["content"])
            score = len(q & t) / max(1, len(q | t))
            if score > 0.1:
                scored.append({**rec, "relevance": round(score, 3)})
        return sorted(scored, key=lambda r: r["relevance"], reverse=True)[:top_n]


class NoMemory(Memory):
    name = "none"

    def remember(self, incident: Incident) -> str | None:
        return None

    def recall(self, symptoms: str, top_n: int = 3) -> list[dict[str, Any]]:
        return []
