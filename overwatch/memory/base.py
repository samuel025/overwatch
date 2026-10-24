"""Memory interface used by the Historian. Implementations: H-MEM service (default) and a local fallback."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Incident


class Memory(ABC):
    name = "memory"

    @abstractmethod
    def remember(self, incident: Incident) -> str | None:
        """Store a confirmed incident; return the memory id."""

    @abstractmethod
    def recall(self, symptoms: str, top_n: int = 3, threshold: float | None = None) -> list[dict[str, Any]]:
        """Return similar past incidents: [{id, content, relevance, tags}]."""

    def feedback(self, memory_id: str, kind: str) -> None:
        """Tell memory whether a recalled incident was helpful / not_helpful / wrong."""

    def health(self) -> dict[str, Any]:
        return {"ok": True, "engine": self.name}

    def overview(self, limit: int = 20) -> dict[str, Any]:
        """What the UI shows in the memory panel: health, stored memories, recent audit entries."""
        return {"health": self.health(), "memories": [], "ledger": []}


def incident_memory_text(incident: Incident) -> str:
    """The sentence stored for a confirmed incident; written so future symptoms match it."""
    symptoms = "; ".join(a.summary() for a in incident.anomalies)
    fix = "; ".join(incident.recommended_actions) or "no fix recorded"
    return (
        f"Incident {incident.opened_at[:10]} on {', '.join(incident.services)}. "
        f"Symptoms: {symptoms}. Root cause: {incident.root_cause}. Fix: {fix}."
    )


def incident_tags(incident: Incident) -> list[str]:
    return [f"service:{s}" for s in incident.services] + [f"check:{a.check}" for a in incident.anomalies]
