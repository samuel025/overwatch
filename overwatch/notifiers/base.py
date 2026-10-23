"""Notifier interface plus the shared plain-text rendering of an incident report."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Incident


class Notifier(ABC):
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.get("name", self.cfg.get("type", self.__class__.__name__))

    @abstractmethod
    def send(self, incident: Incident) -> str:
        """Deliver the report; return a short receipt (URL, ticket key, 'ok')."""


def render_text(incident: Incident, max_evidence: int = 4) -> str:
    lines = [
        f"*{incident.title}*",
        f"Incident `{incident.id}` opened {incident.opened_at} | confidence {incident.confidence:.0%}",
        "",
        "*What was detected*",
        *[f"- {a.summary()}" for a in incident.anomalies],
        "",
        "*Root cause*",
        incident.root_cause or "(not determined)",
    ]
    br = incident.blast_radius
    if br:
        lines += ["", "*Blast radius*"]
        if br.get("origin"):
            lines.append(f"- origin: {br['origin']}; affected: {', '.join(br.get('affected', [])) or '-'}; "
                         f"at risk: {', '.join(br.get('at_risk', [])) or 'none'}")
        lines.append(f"- started {br.get('time_to_detect_s', 0):.0f}s before detection; "
                     f"ongoing for {br.get('duration_s', 0) / 60:.1f} min")
        for item in br.get("impact", []):
            if item.get("value") is not None:
                lines.append(f"- {item['label']}: {item['value']:g} {item.get('unit', '')}".rstrip())
    if incident.evidence:
        lines += ["", "*Evidence*"]
        for ev in [e for e in incident.evidence if not e.error][:max_evidence]:
            lines.append(f"- {ev.note or 'query'} ({ev.source}): `{ev.query.strip()[:200]}`")
    if incident.similar_incidents:
        lines += ["", "*Seen before (H-MEM)*"]
        lines += [f"- {m['content'][:200]} (relevance {m['relevance']})" for m in incident.similar_incidents[:2]]
    if incident.recommended_actions:
        lines += ["", "*Recommended actions*", *[f"{i}. {a}" for i, a in enumerate(incident.recommended_actions, 1)]]
    return "\n".join(lines)
