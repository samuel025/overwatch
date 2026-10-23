"""Historian: asks memory (H-MEM) whether we have seen these symptoms before."""

from __future__ import annotations

from typing import Any

from ..memory import Memory
from ..models import Anomaly


class Historian:
    def __init__(self, memory: Memory):
        self.memory = memory

    def recall(self, anomalies: list[Anomaly], top_n: int = 3) -> list[dict[str, Any]]:
        symptoms = " ".join(f"{a.service} {a.check} {a.description}" for a in anomalies)
        try:
            return self.memory.recall(symptoms, top_n=top_n)
        except Exception as e:  # memory is an aid, never a reason to skip an investigation
            return [{"id": "-", "content": f"(memory unavailable: {e})", "relevance": 0.0, "tags": []}]
