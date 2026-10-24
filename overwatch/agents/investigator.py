"""Investigator: a hypothesis -> query -> evidence loop that ends with a cited root cause.

The model replies with one JSON action per turn, so this works with any OpenAI-compatible
provider (Nightshift routing modes, Nemotron, ...) without relying on native tool calling.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .. import grounding, knowledge
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
- Use the incident timeline. A cause must start BEFORE (or exactly when) the problem started, usually just
  before. A change long before the start is unlikely to be the cause unless the data shows a delayed effect.
- A change shortly before the problem is a suspect, not proof. Show the mechanism that links it to the symptom.
- The timeline lists suspect events (S1, S2...) that Overwatch computed, with how long before the problem each
  happened and whether anything failed in between. Say which suspect your conclusion rests on. A suspect whose service is
  not in the failing path is unlikely to be related; do not invent a mechanism that links it. A suspect marked
  as coming after every failing check had already begun, or one after which the system ran clean for minutes, is
  unlikely to have started that problem. Two separate problems can overlap in one incident: judge each suspect
  against the checks that began failing right after it.
  If none of them explains the problem, say so and name the trigger as unknown instead of picking one.
- Timing alone never proves cause. A change counts as the cause only if it touches the failing service or
  something it depends on (see the topology) AND you can name how it produces the symptom. Changes to
  unrelated services (other teams) are coincidences unless the data links them. If nothing explains why a
  service stopped, say that the trigger is unknown rather than picking the nearest change.
- Only state facts that appear in the context or in query results. Never assume infrastructure, tools or
  mechanisms you were not told about (for example, do not assume Kubernetes, load balancers or probes).
- Work as a differential diagnosis: keep 2-3 candidate causes in mind and look for evidence that would rule
  each one in or out.
- There can be more than one independent cause. If so, name each one.
- If your own queries fail because a data source is unreachable, that may itself be the incident.
- Conclude as soon as the evidence clearly supports a cause. After the evidence pack, most incidents need
  only 2-4 more queries; spending the whole budget is a sign you are not converging.
- Never invent tables, columns or numbers. Use only the catalog and what queries return.
- If a query errors, read the error and fix the query.

Reply with exactly ONE JSON object and nothing else. Either:
{"thought": "<what you think and why>", "action": "query", "source": "<source name>", "query": "<read-only query>", "purpose": "<what this will show>"}
or, when the evidence is sufficient:
{"thought": "<summary of reasoning>", "action": "conclude",
 "root_cause": "<one or two sentences: what broke, why, and what triggered it>",
 "confidence": <0.0-1.0>, "severity": "low|medium|high|critical",
 "origin_service": "<the service where the root cause lives, from the topology if one is given>",
 "suspect": "<S1, S2... the suspect event your root cause rests on, or none>",
 "hypotheses": [{"cause": "<candidate cause>", "verdict": "confirmed|ruled out|possible",
                 "why": "<the evidence that decides it>"}],
 "evidence": [{"step": <step number, 0 = the evidence pack>,
               "quote": "<a value or text copied EXACTLY from that step's result>",
               "shows": "<what it proves>"}],
 "first_guess": {"verdict": "agree|disagree|none", "why": "<if a first guess was given: why it is right or wrong>"},
 "memory_used": [<ids of past incidents that matched, or empty>],
 "recommended_actions": ["<concrete fix>", "..."]}
Every claim in root_cause must be backed by an item in "evidence"; quotes are checked against the data."""


CRITIC_SECONDS = 25   # optional extras never hold up the diagnosis: one attempt, this long
TRIAGE_SECONDS = 45

CRITIC_PROMPT = """You check a diagnosis against the data it was based on. You do not diagnose.
Break the diagnosis into its statements of FACT (what happened, what state something was in, numbers, times, what
caused what). Skip recommendations and hedged statements ("may", "likely"). For each statement, copy the exact text
from the data that shows it: a value from a row, a timeline line, or an anomaly line. Copy it character for character.
If nothing in the data shows the statement, leave "support" empty. Reply with exactly ONE JSON object:
{"claims": [{"claim": "<the statement, from the diagnosis>", "support": "<exact text copied from the data, or empty>"}]}"""


TRIAGE_PROMPT = """You are an on-call engineer giving a FAST first guess about a live production problem.
You cannot run queries. Base the guess on the CURRENT evidence: the anomalies and the evidence pack
(container state, error messages, recent changes, per-service metrics). Past incidents are only hints;
they are often a different problem, so never prefer them over what the current data shows.
Reply with exactly ONE JSON object:
{"hypothesis": "<one or two sentences: the most likely cause and the mechanism>",
 "confidence": <0.0-1.0>, "origin_service": "<service where the cause most likely lives>",
 "check_next": "<the one thing that would confirm or refute it>"}"""


def _describe_sources(sources: dict[str, DataSource], hidden: set[str] | frozenset[str] = frozenset()) -> str:
    return "\n".join(f"- {name} ({src.language})" for name, src in sources.items() if name not in hidden)


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
        # Sources marked `internal: true` serve the Watcher only; the Investigator reaches that data through Zetaris.
        self.hidden = {n for n, s in cfg.sources.items() if isinstance(s, dict) and s.get("internal")}

    def known_text(self) -> str:
        """Everything Overwatch tells the model about the system (also the reference for grounding)."""
        cfg = self.cfg
        parts = [knowledge.system_description(cfg.system_description) if knowledge.is_placeholder(cfg.system_description)
                 else cfg.system_description, cfg.catalog, cfg.runtime_facts,
                 "\n".join(knowledge.describe_services(cfg.services, cfg.topology, cfg.checks)),
                 json.dumps(cfg.topology), " ".join(self.sources)]
        return " ".join(parts)

    def _context(self, incident: Incident, timeline: list[str] | None = None) -> list[str]:
        cfg = self.cfg
        context = [
            f"System: {cfg.system_name}. "
            + (knowledge.system_description(cfg.system_description) if knowledge.is_placeholder(cfg.system_description)
               else cfg.system_description),
            *(["Service knowledge (the team's own words where they wrote any; otherwise what Overwatch found):\n"
               + "\n".join(knowledge.describe_services(cfg.services, cfg.topology, cfg.checks))] if cfg.services else []),
            *([f"Runtime facts:\n{cfg.runtime_facts.strip()}"] if cfg.runtime_facts else []),
            f"Data sources:\n{_describe_sources(self.sources, self.hidden)}",
            f"Catalog (what you can query):\n{cfg.catalog}",
            *(["Service topology (caller -> services it calls):\n" + "\n".join(
                f"- {svc} -> {', '.join(deps) or '(nothing)'}" for svc, deps in cfg.topology.items())]
              if cfg.topology else []),
            "Detected anomalies:\n" + "\n".join(f"- {a.summary()} at {a.detected_at}" for a in incident.anomalies),
            *([ "Incident timeline (computed by Overwatch from the data):\n" + "\n".join(timeline)] if timeline else []),
        ]
        real_memories = [m for m in incident.similar_incidents if m.get("id") not in (None, "-")]
        if real_memories:
            context.append(
                "Similar past incidents (from H-MEM memory):\n"
                + "\n".join(f"- [{m['id']}] {m['content']} (relevance {m['relevance']})" for m in real_memories)
            )
        else:
            context.append("Similar past incidents: none found.")
        if incident.pack:
            context.append(
                "Evidence already gathered automatically when the incident opened (do not re-run these; "
                "query further only for what they do not answer):\n"
                + "\n".join(
                    f"- {ev.note.removeprefix('[pack] ')} ({ev.source}): "
                    + (f"ERROR {ev.error}" if ev.error else f"{len(ev.rows)} rows {_clip(ev.rows, 1500)}")
                    for ev in incident.pack
                )
            )
        return context

    def triage(self, incident: Incident, timeline: list[str] | None = None) -> dict[str, Any]:
        """One quick call to a fast model for a first guess, shown while the deep investigation runs."""
        model = self.cfg.llm.triage_model
        if not model:
            return {}
        self.emit("agent.thinking", agent="investigator", incident=incident.id, step=0)
        messages = [
            {"role": "system", "content": TRIAGE_PROMPT},
            {"role": "user", "content": "\n\n".join(self._context(incident, timeline)) + "\n\nYour first guess:"},
        ]
        started = time.perf_counter()
        try:
            reply = self.llm.chat_json(messages, model, agent="triage", extra_body=self.cfg.llm.triage_extra_body,
                                       patience=TRIAGE_SECONDS)
        except LLMError as e:
            incident.reasoning.append(f"fast first guess unavailable: {e}")
            return {}
        hypothesis = {
            "text": str(reply.get("hypothesis", "")).strip(),
            "confidence": max(0.0, min(1.0, float(reply.get("confidence") or 0.5))),
            "origin_service": str(reply.get("origin_service") or "") or None,
            "check_next": str(reply.get("check_next", "")),
            "model": model,
            "seconds": round(time.perf_counter() - started, 1),
        }
        if hypothesis["text"]:
            incident.hypothesis = hypothesis
            self.emit("investigator.hypothesis", incident=incident.id, **hypothesis)
        return hypothesis

    def investigate(self, incident: Incident, on_step=None, timeline: list[str] | None = None) -> Incident:
        cfg = self.cfg
        context = self._context(incident, timeline)
        if incident.hypothesis.get("text"):
            context.append(
                f"A fast triage model's first guess (unverified, {incident.hypothesis['confidence']:.0%} sure): "
                f"{incident.hypothesis['text']} Verify or refute it with data; do not assume it is right."
            )

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
                quick = cfg.llm.query_extra_body if not final else None  # no long reasoning just to pick a query
                action = (self.llm.chat_json(messages, cfg.llm.investigator_model, agent="investigator", extra_body=quick)
                          if quick else self.llm.chat_json(messages, cfg.llm.investigator_model, agent="investigator"))
                if quick and action.get("action") == "conclude":  # a conclusion always gets the full reasoning
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
                self._ground(incident, timeline)
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
        visible = [n for n in self.sources if n not in self.hidden or action.get("_pack")]
        if source not in visible:
            ev.error = f"unknown source '{source}'. Available: {', '.join(n for n in self.sources if n not in self.hidden)}"
            return ev
        started = time.perf_counter()
        try:
            ev.rows = self.sources[source].query(query)
        except SourceError as e:
            ev.error = str(e)
        ev.ms = round((time.perf_counter() - started) * 1000, 1)
        return ev

    def _ground(self, incident: Incident, timeline: list[str] | None = None) -> None:
        """Check the conclusion against the data; a poorly grounded diagnosis cannot claim high confidence."""
        result = grounding.check(incident, self.known_text() + " " + " ".join(timeline or []))
        incident.grounding = result
        if not result["ok"] and incident.confidence > 0.6:
            incident.reasoning.append(
                f"Confidence capped at 60% (was {incident.confidence:.0%}): {result['verified']}/{result['total']} "
                f"quotes verified, not found in the data: {', '.join(result['unsupported_terms']) or 'none'}"
            )
            incident.confidence = 0.6
        self._cap_by_suspect(incident, timeline or [])
        guess = incident.first_guess_verdict
        if incident.hypothesis.get("text") and guess.get("verdict") == "disagree" and not str(guess.get("why", "")).strip():
            incident.reasoning.append("Disagrees with the first guess without saying why: treat as two competing explanations")
            incident.confidence = min(incident.confidence, 0.5)

    def critique(self, incident: Incident, timeline: list[str] | None = None) -> list[str]:
        """The fast model lists claims in the conclusion that the data does not show (a cheap second reader; it does
        not diagnose). They are shown with the diagnosis and cap its confidence."""
        model = self.cfg.llm.triage_model
        if not (self.cfg.llm.critic and model and incident.root_cause):
            return []
        data = ["Timeline:\n" + "\n".join(timeline or []),
                "Anomalies:\n" + "\n".join(f"- {a.summary()}" for a in incident.anomalies)]
        data += [f"Evidence pack, {ev.note.removeprefix('[pack] ')}: " + (f"ERROR {ev.error}" if ev.error else _clip(ev.rows, 1500))
                 for ev in incident.pack]
        data += [f"Query result ({ev.source}: {ev.query[:120]}): " + (f"ERROR {ev.error}" if ev.error else _clip(ev.rows, 1500))
                 for ev in incident.evidence]
        messages = [{"role": "system", "content": CRITIC_PROMPT},
                    {"role": "user", "content": f"Diagnosis:\n{incident.root_cause}\n\nData:\n" + "\n\n".join(data)}]
        try:
            reply = self.llm.chat_json(messages, model, agent="critic", extra_body=self.cfg.llm.triage_extra_body,
                                       patience=CRITIC_SECONDS)
        except LLMError as e:
            incident.reasoning.append(f"critic unavailable: {e}")
            return []
        corpus = grounding._norm("\n\n".join(data))
        claims = []
        for item in (reply.get("claims") or []):
            if not isinstance(item, dict) or not str(item.get("claim", "")).strip():
                continue
            support = str(item.get("support", "")).strip()
            if not support or not grounding._found(support, corpus, empty_result=True):  # the quote must really be in the data
                claims.append(str(item["claim"]).strip()[:240])
        claims = claims[:5]
        incident.grounding["unsupported_claims"] = claims
        if claims:
            incident.reasoning.append("A second reader found statements the data does not show: " + " | ".join(claims))
            if incident.confidence > 0.6:
                incident.reasoning.append(f"Confidence capped at 60% (was {incident.confidence:.0%}) because of them.")
                incident.confidence = 0.6
        return claims

    @staticmethod
    def _cap_by_suspect(incident: Incident, timeline: list[str]) -> None:
        """A conclusion resting on a suspect that the timing argues against cannot be confident."""
        label = incident.suspect.strip().upper()
        if not re.fullmatch(r"S\d+", label):
            return
        line = next((l for l in timeline if l.strip().startswith(label + " ")), "")
        weak = ("did not start" in line) or ("only a delayed" in line) or ("already failing in between" in line) \
               or ("not in the failing path" in line)
        if weak and incident.confidence > 0.5:
            incident.reasoning.append(
                f"Confidence capped at 50% (was {incident.confidence:.0%}): the suspect it relies on ({label}) does not "
                f"fit the timing: {line.strip()[:200]}")
            incident.confidence = 0.5

    @staticmethod
    def _conclude(incident: Incident, action: dict[str, Any]) -> None:
        incident.root_cause = str(action.get("root_cause", "")).strip()
        try:
            incident.confidence = max(0.0, min(1.0, float(action.get("confidence", 0.5))))
        except (TypeError, ValueError):
            incident.confidence = 0.5
        incident.suspect = str(action.get("suspect", "") or "").strip()
        sev = str(action.get("severity", "medium")).lower()
        incident.severity = sev if sev in ("low", "medium", "high", "critical") else "medium"
        incident.recommended_actions = [str(a) for a in action.get("recommended_actions", [])][:6]
        cited = [c for c in action.get("evidence", []) if isinstance(c, dict)]
        incident.cited = [
            {"step": c.get("step"), "quote": str(c.get("quote", "")), "shows": str(c.get("shows", ""))} for c in cited
        ][:12]
        incident.hypotheses = [
            {"cause": str(h.get("cause", "")), "verdict": str(h.get("verdict", "possible")), "why": str(h.get("why", ""))}
            for h in action.get("hypotheses", []) if isinstance(h, dict) and h.get("cause")
        ][:5]
        fg = action.get("first_guess")
        incident.first_guess_verdict = (
            {"verdict": str(fg.get("verdict", "none")), "why": str(fg.get("why", ""))} if isinstance(fg, dict) else {}
        )
        steps = {int(s) for s in action.get("evidence_steps", []) if str(s).isdigit()}
        steps |= {int(c["step"]) for c in incident.cited if str(c.get("step")).isdigit()}
        for i, ev in enumerate(incident.evidence, 1):
            if i in steps and not ev.note.startswith("[key] "):
                ev.note = "[key] " + ev.note
        origin = str(action.get("origin_service") or "").strip()
        incident.origin_service = origin or None
        used = {str(m) for m in action.get("memory_used", [])}
        for m in incident.similar_incidents:
            m["used"] = m.get("id") in used
