"""Grounding: check a conclusion against the data the investigation actually saw.

Two mechanical checks, no model involved:
- every evidence quote the model cites must appear in the result of the step it cites;
- identifiers and technical terms in the root cause (versions, config ids, instance names, product
  names like "Kubernetes") must appear somewhere in what Overwatch knows about the system or in the
  query results. Anything else is reported as "not found in the data".

The result is shown next to the diagnosis and caps the confidence of a poorly grounded one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import Incident

# Words that look technical but are ordinary English at the start of sentences or in explanations.
_ORDINARY = {
    "utc", "gmt",
    "the", "this", "that", "these", "those", "each", "every", "after", "before", "because", "when", "while",
    "both", "orders", "gateway", "payments", "service", "services", "it", "its", "a", "an", "and", "or", "no",
    "not", "root", "cause", "error", "errors", "users", "checkout", "checkouts", "one", "two", "all", "then",
    "however", "since", "during", "therefore", "as", "in", "on", "at", "of", "to", "with", "by", "from", "so",
}
_TERM = re.compile(
    r"\bv?\d+\.\d+(?:\.\d+)?\b"            # versions: v1.8.0, 2.3
    r"|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"    # identifiers with underscores: customer_id
    r"|\b[a-z]+(?:-[a-z]*\d[a-z0-9]*|-[a-z]{1,2})\b"  # names with a short or numbered part: config-81, payments-b
    r"|\b[A-Z][A-Za-z0-9]*[A-Z0-9][A-Za-z0-9]*\b"  # CamelCase / ALLCAPS: PoolTimeout, SIGTERM, OOM
    r"|(?<=[a-z,;:] )[A-Z][a-z]{3,}\b"       # capitalized mid-sentence: Kubernetes, Redis
)


# Plain-English claims about what state something was in. Each needs support in the query results: a model
# that writes "the container was unhealthy" when no result mentions health is making it up.
_STATE_CLAIMS = [
    ("unhealthy / failed health check", re.compile(r"\bunhealthy\b|health.?checks? (?:fail|was failing|were failing)"),
     re.compile(r"unhealthy|health.?check|healthcheck|\"health\"")),
    ("out of memory", re.compile(r"\bout[- ]of[- ]memory\b|\boom\b|\boom-?kill"),
     re.compile(r"out of memory|oom-?kill|\"oom_killed\": true|\boom\b(?!_killed)|\b137\b")),
    ("deadlock", re.compile(r"\bdeadlock"), re.compile(r"deadlock")),
    ("disk full", re.compile(r"\bdisk (?:is )?full\b|no space left"), re.compile(r"no space left|disk full|enospc")),
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).lower()).strip().strip("'\"")


def _step_text(incident: Incident, step: int) -> str:
    """Step 0 is the evidence pack; steps 1..N are the Investigator's own queries."""
    if step == 0:
        return json.dumps([e.rows for e in incident.pack], default=str)
    if 1 <= step <= len(incident.evidence):
        return json.dumps(incident.evidence[step - 1].rows, default=str)
    return ""


def _number_in(quote: str, text: str) -> bool:
    try:
        target = float(quote.replace(",", "").rstrip("%ms ").strip())
    except ValueError:
        return False
    for n in re.findall(r"-?\d+(?:\.\d+)?", text):
        if abs(float(n) - target) <= max(0.051, abs(target) * 0.01):
            return True
    return False


def _atoms(quote: str) -> list[str]:
    """Split a quote like "ts: 2026-10-23T08:39:39, requests: 145, status: exited" into its values."""
    atoms = []
    for part in re.split(r"[,;|]\s*|\s+and\s+", quote):
        part = part.strip().strip("{}[]()'\"")
        m = re.match(r"^[a-z_ ]{1,30}[:=]\s*(.+)$", part)  # "key: value" -> value
        value = (m.group(1) if m else part).strip().strip("'\"")
        if len(value) >= 2:
            atoms.append(value)
    return atoms or [quote]


_DATA_TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}t?[\d:.]*"          # dates and timestamps
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"        # clock times
    r"|-?\d+(?:\.\d+)?"                     # numbers
    r"|\b[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)+\b"  # identifiers
    r"|\"([^\"]{2,})\"|'([^']{2,})'"          # quoted strings
)
_EMPTY = re.compile(r"^\s*(0 rows?|no rows?|no results?|empty|none)\b")


def _found(quote: str, text: str, empty_result: bool = False) -> bool:
    """A quote is found if the values in it appear in the text: exactly, as a nearby number, or (for prose
    around values) at least three quarters of its data tokens. "0 rows" matches an empty result."""
    q = _norm(quote)
    if not q:
        return False
    if _EMPTY.match(q):
        return empty_result
    if not text:
        return False
    if q in text or _number_in(q, text):
        return True
    tokens = []
    for m in _DATA_TOKEN.finditer(q):
        tokens.append(next((g for g in m.groups() if g), m.group(0)))
    if not tokens:
        tokens = [_norm(a) for a in _atoms(quote)]
    hits = sum(1 for t in tokens if t in text or _number_in(t, text))
    return hits / len(tokens) >= 0.75


def check(incident: Incident, known_text: str) -> dict[str, Any]:
    """`known_text`: what Overwatch told the model (description, catalog, topology, timeline...)."""
    cited = incident.cited
    alerts = " ".join(f"{a.check} {a.service} {a.value} {a.description}" for a in incident.anomalies)
    verified, unverified = 0, []
    for c in cited:
        try:
            step = int(c.get("step", -1))
        except (TypeError, ValueError):
            step = -1
        text = _norm(_step_text(incident, step))
        if step == 0:  # the pack, plus everything Overwatch computed up front
            text += " " + _norm(known_text + " " + alerts)
        empty = 1 <= step <= len(incident.evidence) and not incident.evidence[step - 1].rows
        if _found(str(c.get("quote", "")), text, empty_result=empty):
            verified += 1
        else:
            unverified.append({"step": step, "quote": c.get("quote", "")})

    corpus = _norm(" ".join([
        known_text,
        json.dumps([e.rows for e in incident.pack], default=str),
        json.dumps([e.rows for e in incident.evidence], default=str),
        " ".join(f"{a.check} {a.service} {a.description}" for a in incident.anomalies),
    ]))
    corpus_loose = corpus.replace("_", " ").replace("-", " ")
    unsupported = []
    for term in dict.fromkeys(_TERM.findall(incident.root_cause)):
        t = term.lower()
        if t in _ORDINARY or len(t) < 3:
            continue
        if t not in corpus and t.replace("_", " ").replace("-", " ") not in corpus_loose:
            unsupported.append(term)

    results = _norm(" ".join([
        json.dumps([e.rows for e in incident.pack], default=str),
        json.dumps([e.rows for e in incident.evidence], default=str),
    ]))
    for label, claim, support in _STATE_CLAIMS:
        if claim.search(incident.root_cause.lower()) and not support.search(results):
            unsupported.append(label)

    total = len(cited)
    score = (verified / total) if total else 0.0
    ok = total > 0 and score >= 0.6 and not unsupported
    return {
        "verified": verified,
        "total": total,
        "unverified": unverified,
        "unsupported_terms": unsupported,
        "score": round(score, 2),
        "ok": ok,
    }
