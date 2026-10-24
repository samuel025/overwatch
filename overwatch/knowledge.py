"""What Overwatch tells the model about the system, and what it says when the team has not described a service.

Setup leaves "TODO: describe what this service does" where a person should write. The Investigator reads the
service notes as facts from the team, so a placeholder must never reach it. An undescribed service is stated as
undescribed (so the model does not guess what it does), together with facts Overwatch worked out itself: what it
calls, what calls it, where it runs, what is watched. Those are computed, not written by anyone.
"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"\bTODO\b", re.IGNORECASE)
_NOT_A_NOTE = ("what", "runs_on", "image")  # the description, and facts shown on their own


def is_placeholder(text: Any) -> bool:
    """Empty, or still the TODO that setup writes for a person to replace."""
    value = str(text or "").strip()
    return not value or bool(_PLACEHOLDER.search(value))


def system_description(text: str) -> str:
    """The system description with the unfilled "TODO: ..." sentence replaced by an honest statement."""
    cleaned = re.sub(r"\s*\bTODO\b.*$", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    return (cleaned + " " if cleaned else "") + "(The team has not said what the system does or how a request flows through it.)"


def _field(check: Any, field: str) -> Any:
    """A check from the loaded config (an object) or from overwatch.yaml / a discovery plan (a dict)."""
    return check.get(field) if isinstance(check, dict) else getattr(check, field, None)


def facts_for(name: str, services: dict[str, dict[str, Any]], topology: dict[str, list[str]], checks: list[Any]) -> str:
    """Facts Overwatch can compute about a service, as one line (empty when it knows nothing)."""
    facts = services.get(name) or {}
    calls = sorted(topology.get(name) or [])
    callers = sorted(c for c, deps in topology.items() if name in (deps or []))
    watched = [str(_field(c, "description") or _field(c, "name") or "") for c in checks if _field(c, "service") == name][:3]
    parts = []
    if calls:
        parts.append(f"calls {', '.join(calls)}")
    if callers:
        parts.append(f"called by {', '.join(callers)}")
    if facts.get("runs_on"):
        parts.append(str(facts["runs_on"]))
    if facts.get("image"):
        parts.append(f"image {facts['image']}")
    if watched:
        parts.append(f"watched: {'; '.join(w for w in watched if w)}")
    return "; ".join(parts)


def describe_services(services: dict[str, dict[str, Any]], topology: dict[str, list[str]], checks: list[Any]) -> list[str]:
    """One line per service for the model: the team's words where there are any, otherwise a clear 'not described'."""
    lines = []
    for name, raw in services.items():
        facts = raw or {}
        notes = "; ".join(f"{k}: {v}" for k, v in facts.items() if k not in _NOT_A_NOTE and not is_placeholder(v))
        if is_placeholder(facts.get("what")):
            found = facts_for(name, services, topology, checks)
            lines.append(f"- {name}: (the team has not described this service; do not assume what it does)"
                         + (f" Overwatch found: {found}." if found else "") + (f" {notes}" if notes else ""))
        else:
            lines.append(f"- {name}: what: {facts['what']}" + (f"; {notes}" if notes else ""))
    return lines
