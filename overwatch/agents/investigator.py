"""Investigator: a hypothesis -> query -> evidence loop that ends with a cited root cause.

The model replies with one JSON action per turn, so this works with any OpenAI-compatible
provider (Nightshift routing modes, Nemotron, ...) without relying on native tool calling.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from ..config import Config
from ..llm import LLM, LLMError
from ..models import Evidence, Incident
from ..sources import DataSource, SourceError

SYSTEM_PROMPT = """You are Overwatch, an expert site-reliability engineer investigating a live production problem.
Your job: find the ROOT CAUSE (not just the symptom) and prove it with data.

How to work:
- Form a hypothesis, then run ONE read-only query to test it. Look at the result, refine, repeat.
- Correlate across sources: metrics show WHAT changed, deploy/change history shows WHEN and WHY.
- Similar past incidents are hints, not proof. Confirm them against current data before relying on them.
- Consider every class of cause, not just the latest deploy: code or config changes, a slow or failing
  dependency, resource saturation (CPU, memory, connections, pools), locks and long transactions,
  traffic changes (compare request rates with the normal baseline), retries amplifying a failure,
  crashes and restarts, background or batch jobs.
- The service that reports errors is often not the one at fault. Check whether its dependencies changed.
- If a service runs several instances, compare them: one bad instance looks like a partly broken service.
- External dependencies (third-party APIs, certificates, quotas) fail too, with nothing changing on your side.
- Gradual problems (leaks, growing queues, filling disks) show as a trend: compare several time windows.
- A change shortly before the problem is a suspect, not proof. Show the mechanism that links it to the symptom.
- There can be more than one independent cause. If so, name each one.
- If your own queries fail because a data source is unreachable, that may itself be the incident.
- Never invent tables, columns or numbers. Use only the catalog and what queries return.
- If a query errors, read the error and fix the query.

Reply with exactly ONE JSON object and nothing else. Either:
{"thought": "<what you think and why>", "action": "query", "source": "<source name>", "query": "<read-only query>", "purpose": "<what this will show>"}
or, when the evidence is sufficient:
{"thought": "<summary of reasoning>", "action": "conclude",
 "root_cause": "<one or two sentences: what broke, why, and what triggered it>",
 "confidence": <0.0-1.0>, "severity": "low|medium|high|critical",
 "evidence_steps": [<step numbers whose results support the root cause>],
 "memory_used": [<ids of past incidents that matched, or empty>],
 "origin_service": "<the service where the root cause lives, from the topology if one is given>",
 "recommended_actions": ["<concrete fix>", "..."]}"""


def _describe_sources(sources: dict[str, DataSource]) -> str:
    return "\n".join(f"- {name} ({src.language})" for name, src in sources.items())


def _clip(rows: list[dict[str, Any]], limit: int = 3500) -> str:
    text = json.dumps(rows, default=str)
    return text if len(text) <= limit else text[:limit] + f"... (truncated, {len(rows)} rows)"


class Investigator:
    def __init__(
        self, cfg: Config, llm: LLM, sources: dict[str, DataSource], emit: Callable[..., None] | None = None
    ):
        self.cfg = cfg
        self.llm = llm
        self.sources = sources
        self.emit = emit or (lambda *_a, **_k: None)

    def investigate(self, incident: Incident, on_step=None) -> Incident:
        cfg = self.cfg
        context = [
            f"System: {cfg.system_name}. {cfg.system_description}",
            f"Data sources:\n{_describe_sources(self.sources)}",
            f"Catalog (what you can query):\n{cfg.catalog}",
            *(["Service topology (caller -> services it calls):\n" + "\n".join(
                f"- {svc} -> {', '.join(deps) or '(nothing)'}" for svc, deps in cfg.topology.items())]
              if cfg.topology else []),
            "Detected anomalies:\n" + "\n".join(f"- {a.summary()} at {a.detected_at}" for a in incident.anomalies),
        ]
        real_memories = [m for m in incident.similar_incidents if m.get("id") not in (None, "-")]
        if real_memories:
            context.append(
                "Similar past incidents (from H-MEM memory):\n"
                + "\n".join(f"- [{m['id']}] {m['content']} (relevance {m['relevance']})" for m in real_memories)
            )
        else:
            context.append("Similar past incidents: none found.")

        budget = cfg.llm.max_steps
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(context)
             + f"\n\nYou can run at most {budget} queries, then you must conclude. Start the investigation. "
             f"Step 1 of {budget}:"},
        ]

        concluded = False
        for step in range(1, budget + 2):  # `budget` query steps, then one final forced conclusion
            final = step == budget + 1
            self.emit("agent.thinking", agent="investigator", incident=incident.id, step=step)
            try:
                action = self.llm.chat_json(messages, cfg.llm.investigator_model, agent="investigator")
            except LLMError as e:
                incident.reasoning.append(f"model error: {e}")
                incident.root_cause = f"Investigation failed: {e}"
                incident.status = "failed"
                self.emit("investigator.failed", incident=incident.id, error=str(e))
                return incident
            messages.append({"role": "assistant", "content": json.dumps(action)})
            thought = str(action.get("thought", ""))
            if thought:
                incident.reasoning.append(f"Step {step}: {thought}")
            self.emit("investigator.thought", incident=incident.id, step=step, thought=thought,
                      action=str(action.get("action", "")))

            if action.get("action") == "conclude" and str(action.get("root_cause", "")).strip():
                self._conclude(incident, action)
                self.emit("investigator.concluded", incident=incident.id, root_cause=incident.root_cause,
                          confidence=incident.confidence, severity=incident.severity,
                          recommended_actions=incident.recommended_actions)
                concluded = True
                break
            if final:
                break
            if action.get("action") != "query":
                messages.append({"role": "user", "content": 'Unknown action. Use "query" or "conclude".'})
                continue

            self.emit("query.started", incident=incident.id, step=step, source=str(action.get("source", "")),
                      query=str(action.get("query", "")), purpose=str(action.get("purpose", "")))
            ev = self._run_query(action)
            incident.evidence.append(ev)
            self.emit("query.finished", incident=incident.id, step=step, source=ev.source, error=ev.error,
                      row_count=len(ev.rows), rows=ev.rows[:5], ms=ev.ms)
            if on_step:
                on_step(incident)
            feedback = (f"Step {step} result: ERROR {ev.error}" if ev.error
                        else f"Step {step} result ({len(ev.rows)} rows): {_clip(ev.rows)}")
            nxt = step + 1
            if nxt > budget:
                feedback += (f"\n\nYou have used all {budget} queries. Conclude now with the best supported root "
                             "cause: reply with the conclude action only, no more queries.")
            elif nxt == budget:
                feedback += f"\n\nStep {nxt} of {budget}. This is your last query; conclude now if the evidence is enough."
            else:
                feedback += f"\n\nStep {nxt} of {budget}:"
            messages.append({"role": "user", "content": feedback})

        if not concluded:
            incident.status = "failed"
            incident.root_cause = (f"Investigation failed: no conclusion after {budget} queries. "
                                   "The evidence gathered so far is below.")
            self.emit("investigator.failed", incident=incident.id, error="no conclusion within the step budget")
        return incident

    def _run_query(self, action: dict[str, Any]) -> Evidence:
        source, query = str(action.get("source", "")), str(action.get("query", ""))
        ev = Evidence(source=source, query=query, rows=[], note=str(action.get("purpose", "")))
        if source not in self.sources:
            ev.error = f"unknown source '{source}'. Available: {', '.join(self.sources)}"
            return ev
        started = time.perf_counter()
        try:
            ev.rows = self.sources[source].query(query)
        except SourceError as e:
            ev.error = str(e)
        ev.ms = round((time.perf_counter() - started) * 1000, 1)
        return ev

    @staticmethod
    def _conclude(incident: Incident, action: dict[str, Any]) -> None:
        incident.root_cause = str(action.get("root_cause", "")).strip()
        try:
            incident.confidence = max(0.0, min(1.0, float(action.get("confidence", 0.5))))
        except (TypeError, ValueError):
            incident.confidence = 0.5
        sev = str(action.get("severity", "medium")).lower()
        incident.severity = sev if sev in ("low", "medium", "high", "critical") else "medium"
        incident.recommended_actions = [str(a) for a in action.get("recommended_actions", [])][:6]
        steps = {int(s) for s in action.get("evidence_steps", []) if str(s).isdigit()}
        for i, ev in enumerate(incident.evidence, 1):
            if i in steps and not ev.note.startswith("[key] "):
                ev.note = "[key] " + ev.note
        origin = str(action.get("origin_service") or "").strip()
        incident.origin_service = origin or None
        used = {str(m) for m in action.get("memory_used", [])}
        for m in incident.similar_incidents:
            m["used"] = m.get("id") in used
