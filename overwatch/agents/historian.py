"""Historian: asks memory (H-MEM) whether we have seen these symptoms before."""

from __future__ import annotations

from typing import Any

from ..memory import Memory
from ..models import Anomaly


class Historian:
    def __init__(self, memory: Memory):
        self.memory = memory

    # The engine's default cut-off (0.35) sits right where a near-identical past incident scores once its confidence
    # multiplier is applied (0.3 to 0.5 in testing), so ask for a lower one and let the top few through.
    THRESHOLD = 0.25
    CONTEXT_CHARS = 0  # the symptoms (check names and what they said) match past incidents; evidence text from the first
    # seconds of an outage is noisy and, in testing, pushed a near-identical incident under the threshold

    def recall(self, anomalies: list[Anomaly], top_n: int = 3, context: str = "") -> list[dict[str, Any]]:
        """`context` adds what the evidence pack saw (error messages, recent changes) to the search."""
        symptoms = " ".join(f"{a.service} {a.check} {a.check.replace('_', ' ')} {a.description}" for a in anomalies)
        symptoms = f"{symptoms} {context[:self.CONTEXT_CHARS]}".strip()[:1500]
        try:
            return self.memory.recall(symptoms, top_n=top_n, threshold=self.THRESHOLD)
        except Exception as e:  # memory is an aid, never a reason to skip an investigation
            return [{"id": "-", "content": f"(memory unavailable: {e})", "relevance": 0.0, "tags": []}]
