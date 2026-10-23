"""Responder: sends the report once approved, and teaches memory from the outcome."""

from __future__ import annotations

from ..memory import Memory
from ..models import Incident
from ..notifiers import Notifier


class Responder:
    def __init__(self, notifiers: list[Notifier], memory: Memory):
        self.notifiers = notifiers
        self.memory = memory

    def approve(self, incident: Incident) -> dict[str, str]:
        """Send to every notifier, store the confirmed incident, reward the memories that helped."""
        receipts = {}
        for n in self.notifiers:
            try:
                receipts[n.name] = n.send(incident)
            except Exception as e:
                receipts[n.name] = f"FAILED: {e}"
        incident.status = "reported"
        self._learn(incident, confirmed=True)
        return receipts

    def dismiss(self, incident: Incident) -> None:
        """A human rejected the diagnosis: nothing is sent, and memories that were relied on get penalized."""
        incident.status = "dismissed"
        self._learn(incident, confirmed=False)

    def _learn(self, incident: Incident, confirmed: bool) -> None:
        try:
            for m in incident.similar_incidents:
                if m.get("used"):
                    self.memory.feedback(m["id"], "helpful" if confirmed else "not_helpful")
            if confirmed:
                incident.reasoning.append(f"Stored in memory as {self.memory.remember(incident)}")
        except Exception as e:
            incident.reasoning.append(f"memory update failed: {e}")
