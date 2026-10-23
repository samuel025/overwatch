"""Plain data types passed between agents."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Anomaly:
    """A check that crossed its threshold, raised by the Watcher."""

    check: str
    service: str
    value: float | None  # None when the check itself could not run
    threshold: float
    comparison: str
    description: str
    detected_at: str = field(default_factory=now_iso)

    def summary(self) -> str:
        if self.value is None:
            return f"{self.check} on {self.service}: {self.description}"
        return (
            f"{self.check} on {self.service}: value {self.value:g} "
            f"{self.comparison} threshold {self.threshold:g} ({self.description})"
        )


@dataclass
class Evidence:
    """One query the Investigator ran and what it showed."""

    source: str
    query: str
    rows: list[dict[str, Any]]
    note: str = ""
    error: str | None = None
    ms: float = 0.0


@dataclass
class Incident:
    """Everything known about one problem, from detection to report."""

    anomalies: list[Anomaly]
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    opened_at: str = field(default_factory=now_iso)
    status: str = "investigating"  # investigating -> awaiting_approval -> reported | dismissed; or failed
    root_cause: str = ""
    confidence: float = 0.0
    recommended_actions: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    similar_incidents: list[dict[str, Any]] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    severity: str = "medium"
    origin_service: str | None = None
    blast_radius: dict[str, Any] = field(default_factory=dict)
    recovered_at: str | None = None  # set when every check behind it has been healthy for a while

    @property
    def services(self) -> list[str]:
        return sorted({a.service for a in self.anomalies})

    @property
    def title(self) -> str:
        if self.root_cause:
            return f"[{self.severity}] {', '.join(self.services)}: {self.root_cause.splitlines()[0][:120]}"
        return f"[{self.severity}] {', '.join(self.services)}: {self.anomalies[0].check}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["title"] = self.title
        d["services"] = self.services
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Incident":
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in d.items() if k in known}
        data["anomalies"] = [Anomaly(**a) for a in d.get("anomalies", [])]
        data["evidence"] = [Evidence(**e) for e in d.get("evidence", [])]
        return cls(**data)
