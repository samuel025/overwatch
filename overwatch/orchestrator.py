"""Runs the agent pipeline: Watcher -> Historian -> Investigator -> (approval) -> Responder.

Watching never waits for an investigation: incidents are investigated on a small worker pool.
Alerts that arrive while an incident is fresh join it instead of opening a new one, and an
incident recovers on its own once all of its checks have stayed healthy for a while.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agents import Historian, Investigator, Responder, Watcher
from .config import Config
from .events import EventBus
from .impact import incident_start, measure, spread
from .listeners import LISTENER_TYPES
from .llm import LLM
from .memory import build_memory
from .models import Anomaly, Evidence, Incident, now_iso
from .notifiers import build_notifiers
from .sources import build_sources
from .store import IncidentStore

ACTIVE_STATES = ("investigating", "awaiting_approval", "reported")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


class Overwatch:
    def __init__(self, cfg: Config, memory_override: dict[str, Any] | None = None):
        self.cfg = cfg
        self.trace_dir = Path(cfg.trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventBus()
        self.sources = build_sources(cfg.sources)
        self.llm = LLM(cfg.llm, self.trace_dir)
        self.llm.on_call = lambda record: self.events.emit("llm.call", **record)
        self.memory = build_memory(memory_override or cfg.memory)
        self.watcher = Watcher(cfg.checks, self.sources, confirm_samples=cfg.confirm_samples)
        self.historian = Historian(self.memory)
        self.investigator = Investigator(cfg, self.llm, self.sources, emit=self.events.emit)
        self.responder = Responder(build_notifiers(cfg.notifiers), self.memory)
        self.incidents: dict[str, Incident] = {}
        self.last_opened: dict[str, float] = {}  # check name -> time an incident was opened for it
        self.last_check_at: str | None = None
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.failing: set[str] = set()  # checks firing on the latest pass
        self.healthy_since: dict[str, float] = {}  # incident id -> when all its checks first looked healthy
        self.pool = ThreadPoolExecutor(cfg.max_parallel_investigations, thread_name_prefix="investigation")
        self.store = IncidentStore(cfg.state_path or self.trace_dir / "overwatch.db")
        # instance -> {service, since, detail} for containers a pushed event says are down
        self.down: dict[str, dict[str, Any]] = {}
        self.listeners = [
            LISTENER_TYPES[e["type"]](e, self.on_container_event) for e in cfg.events if e.get("type") in LISTENER_TYPES
        ]
        self._restore()

    def start_listeners(self) -> None:
        for listener in self.listeners:
            listener.start()

    # -- pipeline -----------------------------------------------------------------

    def check_once(self) -> Incident | None:
        """One Watcher pass. New problems open an incident that is investigated in the background."""
        anomalies = self.watcher.run()
        self.failing = {a.check for a in anomalies}
        self.last_check_at = now_iso()
        self.events.emit("watch.tick", values=dict(self.watcher.last_values),
                         failing=[a.check for a in anomalies], suspected=sorted(self.watcher.suspected))
        self._track_recovery(self.failing)
        return self.ingest(anomalies)

    def ingest(self, anomalies: list[Anomaly]) -> Incident | None:
        """Route anomalies from any origin (checks, pushed events, external alerts) into incidents."""
        if self._stop.is_set():  # e.g. a delayed event timer firing during shutdown
            return None
        with self.lock:
            fresh = [a for a in anomalies if not self._covered(a)]
            if not fresh:
                return None
            target = self._merge_target()
            if target:
                self._merge(target, fresh)
                return target
            incident = self.open_incident(fresh)
        try:
            self.pool.submit(self._investigate_safely, incident)
        except RuntimeError:  # shutting down between the check above and here
            pass
        return incident

    def on_container_event(self, kind: str, service: str, instance: str, detail: str) -> None:
        """A pushed container event: mark it on the map at once and open/extend an incident on 'down'."""
        if kind == "down":
            since = time.time()
            with self.lock:
                self.down[instance] = {"service": service, "since": since, "detail": detail}
            self.events.emit("service.event", state="down", service=service, instance=instance, detail=detail)

            def still_down() -> None:  # a restart comes back within the grace period: not an outage
                with self.lock:
                    current = self.down.get(instance)
                if current and current["since"] == since:
                    self.ingest([Anomaly(f"event:{instance}_down", service, None, 0.0, "event", detail)])

            if self.cfg.event_grace_seconds > 0:
                threading.Timer(self.cfg.event_grace_seconds, still_down).start()
            else:
                still_down()
        else:
            with self.lock:
                self.down.pop(instance, None)
            self.events.emit("service.event", state="up", service=service, instance=instance, detail=detail)

    def ingest_alert(self, source: str, service: str, title: str, description: str = "") -> Incident | None:
        """An alert pushed by another tool (Alertmanager, CI, a cloud provider)."""
        name = f"alert:{source}:{title}"[:120]
        return self.ingest([Anomaly(name, service or "unknown", None, 0.0, "alert", description or title)])

    def open_incident(self, anomalies: list[Anomaly]) -> Incident:
        incident = Incident(anomalies=anomalies)
        with self.lock:
            self.incidents[incident.id] = incident
            for a in anomalies:
                self.last_opened[a.check] = time.time()
        self._save(incident)
        self.events.emit("incident.opened", incident=incident.to_dict())
        return incident

    def handle(self, anomalies: list[Anomaly], on_update: Callable[[Incident], None] | None = None) -> Incident:
        """Open and investigate synchronously (CLI, eval). The watch loop uses the worker pool instead."""
        return self.investigate(self.open_incident(anomalies), on_update)

    def gather_evidence(self, incident: Incident) -> list[Evidence]:
        """The evidence pack: the configured standard queries, run in parallel the moment an incident opens."""
        if not self.cfg.evidence:
            return []
        since = datetime.fromtimestamp(_ts(incident.opened_at) - 900, timezone.utc).isoformat()
        literal = f"'{since}'::timestamptz"

        def run(item) -> Evidence:
            query = item.query.replace("{since}", literal)
            self.events.emit("query.started", incident=incident.id, step=0, source=item.source, query=query,
                             purpose=item.label)
            ev = self.investigator._run_query({"source": item.source, "query": query, "purpose": item.label})
            ev.note = f"[pack] {item.label}"
            self.events.emit("query.finished", incident=incident.id, step=0, source=ev.source, error=ev.error,
                             row_count=len(ev.rows), rows=ev.rows[:5], ms=ev.ms, purpose=item.label)
            return ev

        with ThreadPoolExecutor(min(8, len(self.cfg.evidence)), thread_name_prefix="evidence") as pool:
            return list(pool.map(run, self.cfg.evidence))

    def build_timeline(self, incident: Incident) -> list[str]:
        """When it started, what each signal normally reads vs now, and pushed events: computed, not guessed."""
        detected = _ts(incident.opened_at)
        started = incident_start(incident.anomalies, self.watcher.history, detected)
        with self.lock:
            down = dict(self.down)
        for instance, d in down.items():
            if any(a.check == f"event:{instance}_down" for a in incident.anomalies):
                started = min(started, d["since"])
        incident.started_at = started

        def clock(t: float) -> str:
            return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")

        def num(v: float | None) -> str:
            return "no data" if v is None else f"{v:.3g}"

        lines = [f"- The problem started at {clock(started)} UTC (first failing signal); "
                 f"Overwatch detected it at {clock(detected)} UTC."]
        lines += [f"- Pushed event at {clock(d['since'])} UTC: {d['detail']}" for d in down.values()]
        signals = []
        for c in self.cfg.checks:
            history = self.watcher.history.get(c.name, [])
            before = [v for t, v in history if v is not None and started - 1800 <= t < started - 30]
            normal = statistics.median(before) if len(before) >= 3 else None
            now = self.watcher.last_values.get(c.name)
            if normal is None and now is None:
                continue
            firing = " (FIRING)" if c.name in self.failing or any(a.check == c.name for a in incident.anomalies) else ""
            signals.append(f"  {c.name} [{c.service}]: normal {num(normal)} -> now {num(now)}{firing}")
        if signals:
            lines.append("- Signals, normal before the start vs now:\n" + "\n".join(signals))
        if incident.pack:
            lines.append("- Evidence-pack rows with a timestamp carry minutes_before_start "
                         "(positive = before the problem started, negative = after).")
        return lines

    @staticmethod
    def _annotate(pack: list[Evidence], started: float) -> None:
        """Label every timestamped row with how long before the start it happened."""
        for ev in pack:
            for row in ev.rows:
                for key, value in list(row.items()):
                    if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and "T" in value[:20] + " T":
                        try:
                            ts = datetime.fromisoformat(value).timestamp()
                        except ValueError:
                            continue
                        row["minutes_before_start"] = round((started - ts) / 60, 1)
                        break

    @staticmethod
    def _pack_text(pack: list[Evidence], limit: int = 800) -> str:
        """Short text from the pack (error messages, change descriptions) to sharpen the memory search."""
        words = []
        for ev in pack:
            for row in ev.rows[:5]:
                words += [str(v) for v in row.values() if isinstance(v, str) and len(v) > 3]
        return " ".join(words)[:limit]

    def investigate(self, incident: Incident, on_update: Callable[[Incident], None] | None = None) -> Incident:
        started = time.monotonic()
        self.events.emit("agent.working", agent="evidence", incident=incident.id)
        incident.pack = self.gather_evidence(incident)
        timeline = self.build_timeline(incident)
        self._annotate(incident.pack, incident.started_at)
        self._save(incident)
        self.events.emit("agent.working", agent="historian", incident=incident.id)
        incident.similar_incidents = self.historian.recall(incident.anomalies, context=self._pack_text(incident.pack))
        self.events.emit("memory.recalled", incident=incident.id, engine=self.memory.name,
                         memories=incident.similar_incidents)

        def step(inc: Incident) -> None:
            self._save(inc)
            if on_update:
                on_update(inc)

        if self.investigator.triage(incident, timeline):
            self._save(incident)
        self.investigator.investigate(incident, on_step=step, timeline=timeline)
        incident.reasoning.append(f"Investigation took {time.monotonic() - started:.1f}s")
        if incident.status == "investigating":  # "failed" stays failed: there is nothing to approve
            incident.status = "awaiting_approval"
        self.update_blast_radius(incident)
        self._trace_investigation(incident, time.monotonic() - started)
        self._save(incident)
        self.events.emit("incident.updated", incident=incident.to_dict())
        if self.cfg.approval == "auto" and incident.status == "awaiting_approval":
            self.approve(incident.id)
        return incident

    def _investigate_safely(self, incident: Incident) -> None:
        try:
            self.investigate(incident)
        except Exception as e:  # a crash must not leave the incident "investigating" forever
            incident.status = "failed"
            incident.root_cause = f"Investigation failed: {e.__class__.__name__}: {e}"
            self._save(incident)
            self.events.emit("investigator.failed", incident=incident.id, error=str(e))
            self.events.emit("incident.updated", incident=incident.to_dict())

    def update_blast_radius(self, incident: Incident) -> None:
        """Deterministic impact: start time from check history, spread from topology, cost from queries."""
        detected = _ts(incident.opened_at)
        started = incident_start(incident.anomalies, self.watcher.history, detected)
        end = _ts(incident.recovered_at) if incident.recovered_at else time.time()
        origin = incident.origin_service
        nodes = set(self.cfg.topology) | {d for deps in self.cfg.topology.values() for d in deps}
        if origin and nodes and origin not in nodes:
            origin = next((n for n in nodes if n in origin or origin in n), None)  # "payments-b" -> "payments"
        failing = {a.service for a in incident.anomalies if a.service in nodes} if nodes else set(incident.services)
        incident.blast_radius = {
            **spread(self.cfg.topology, origin, failing),
            "started_at": started,
            "detected_at": detected,
            "time_to_detect_s": round(detected - started, 1),
            "duration_s": round(end - started, 1),
            "recovered_at": end if incident.recovered_at else None,
            "impact": measure(self.sources, self.cfg.impacts, started),
            "measured_at": time.time(),
        }
        self.events.emit("blast_radius.updated", incident=incident.id, blast_radius=incident.blast_radius)

    def investigate_symptom(
        self, symptom: str, service: str = "unknown", on_update: Callable[[Incident], None] | None = None
    ) -> Incident:
        """Ad-hoc investigation of a human-described problem ("checkout feels slow"). Never auto-recovers."""
        return self.handle([Anomaly("manual_report", service, 0.0, 0.0, "reported", symptom)], on_update)

    def approve(self, incident_id: str) -> dict[str, str]:
        incident = self._get(incident_id)
        if incident.status != "awaiting_approval":
            raise ValueError(f"incident {incident_id} is {incident.status}, not awaiting approval")
        self.events.emit("agent.working", agent="responder", incident=incident.id)
        self.update_blast_radius(incident)  # latest numbers go into the report
        receipts = self.responder.approve(incident)
        incident.reasoning.append(f"Approved and sent: {receipts}")
        self._save(incident)
        self.events.emit("responder.sent", incident=incident.id, receipts=receipts)
        self.events.emit("incident.updated", incident=incident.to_dict())
        return receipts

    def resolve(self, incident_id: str) -> None:
        """A person confirms the problem is over (needed for human reports, which have no check to watch)."""
        incident = self._get(incident_id)
        if incident.recovered_at:
            return
        incident.recovered_at = now_iso()
        incident.reasoning.append("Marked resolved by a person")
        self.healthy_since.pop(incident.id, None)
        if incident.blast_radius:
            self.update_blast_radius(incident)
        self._save(incident)
        self.events.emit("incident.recovered", incident=incident.id, recovered_at=incident.recovered_at)
        self.events.emit("incident.updated", incident=incident.to_dict())

    def dismiss(self, incident_id: str) -> None:
        incident = self._get(incident_id)
        self.responder.dismiss(incident)
        self._save(incident)
        self.events.emit("incident.updated", incident=incident.to_dict())

    # -- loop ---------------------------------------------------------------------

    def run_forever(self, on_error: Callable[[Exception], None] = print) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as e:  # keep watching even if one pass fails
                on_error(e)
            self._stop.wait(self.cfg.interval_seconds)

    def stop(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # -- state --------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "system": self.cfg.system_name,
                "last_check_at": self.last_check_at,
                "checks": [
                    {
                        "name": c.name,
                        "service": c.service,
                        "value": self.watcher.last_values.get(c.name),
                        "threshold": c.threshold,
                        "comparison": c.comparison,
                        "description": c.description,
                        "severity": c.severity,
                        "kind": c.kind,
                        "failing": c.name in self.failing,
                        "suspected": c.name in self.watcher.suspected,
                        "relative_factor": c.relative_factor,
                        "relative_min": c.relative_min,
                        "baseline": self.watcher.baseline(c) if c.relative_factor else None,
                    }
                    for c in self.cfg.checks
                ],
                "history": {name: list(points) for name, points in self.watcher.history.items()},
                "sources": [{"name": n, "language": s.language, "type": self.cfg.sources[n].get("type", "sql")}
                            for n, s in self.sources.items()],
                "interval_seconds": self.cfg.interval_seconds,
                "topology": self.cfg.topology,
                "down": dict(self.down),
                "incidents": [i.to_dict() for i in sorted(self.incidents.values(), key=lambda i: i.opened_at, reverse=True)],
                "approval": self.cfg.approval,
                "memory_engine": self.memory.name,
                "model": self.cfg.llm.investigator_model,
                "provider": self.cfg.llm.base_url,
            }

    def _active(self) -> list[Incident]:
        return [i for i in self.incidents.values() if i.status in ACTIVE_STATES and not i.recovered_at]

    def _covered(self, a: Anomaly) -> bool:
        """Already part of an ongoing incident, or reopened too soon after one (flap protection)."""
        with self.lock:
            if any(a.check in {x.check for x in i.anomalies} for i in self._active()):
                return True
            return time.time() - self.last_opened.get(a.check, 0) < self.cfg.cooldown_seconds

    def _merge_target(self) -> Incident | None:
        """The newest ongoing incident opened within the merge window: a cascade is one incident, not five."""
        with self.lock:
            recent = [i for i in self._active() if time.time() - _ts(i.opened_at) < self.cfg.merge_window_seconds]
        return max(recent, key=lambda i: i.opened_at) if recent else None

    def _merge(self, incident: Incident, anomalies: list[Anomaly]) -> None:
        with self.lock:
            incident.anomalies.extend(anomalies)
            for a in anomalies:
                self.last_opened[a.check] = time.time()
            incident.reasoning.append("Also firing, merged into this incident: " + ", ".join(a.check for a in anomalies))
        self._save(incident)
        self.events.emit("incident.merged", incident=incident.id, checks=[a.check for a in anomalies])
        self.events.emit("incident.updated", incident=incident.to_dict())

    def _track_recovery(self, failing: set[str]) -> None:
        """Recover an incident once every check behind it has stayed healthy for resolve_after_seconds."""
        now = time.time()
        with self.lock:  # failed investigations too: the system can recover whether or not we explained it
            candidates = [i for i in self.incidents.values()
                          if i.status in (*ACTIVE_STATES, "failed") and not i.recovered_at]
            failing = failing | {f"event:{instance}_down" for instance in self.down}
        for inc in candidates:
            checks = {a.check for a in inc.anomalies if a.check != "manual_report" and not a.check.startswith("alert:")}
            if not checks:
                continue  # a human report or external alert has no check to watch; a human closes it
            if checks & failing:
                self.healthy_since.pop(inc.id, None)
                continue
            since = self.healthy_since.setdefault(inc.id, now)
            if now - since < self.cfg.resolve_after_seconds:
                continue
            inc.recovered_at = datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds")
            inc.reasoning.append(f"Recovered: all checks healthy since {inc.recovered_at}")
            self.healthy_since.pop(inc.id, None)
            if inc.blast_radius:
                self.update_blast_radius(inc)  # final duration = time to recover
            self._save(inc)
            self.events.emit("incident.recovered", incident=inc.id, recovered_at=inc.recovered_at)
            self.events.emit("incident.updated", incident=inc.to_dict())

    def _restore(self) -> None:
        for inc in self.store.load():
            if inc.status == "investigating":  # the process stopped mid-investigation
                inc.status = "failed"
                inc.root_cause = "Investigation interrupted: Overwatch was restarted"
                self.store.save(inc)
            self.incidents[inc.id] = inc

    def _get(self, incident_id: str) -> Incident:
        with self.lock:
            if incident_id not in self.incidents:
                raise KeyError(f"unknown incident {incident_id}")
            return self.incidents[incident_id]

    def _save(self, incident: Incident) -> None:
        self.store.save(incident)

    def _trace_investigation(self, incident: Incident, seconds: float) -> None:
        record = {
            "ts": now_iso(),
            "incident": incident.id,
            "memory_engine": self.memory.name,
            "seconds": round(seconds, 2),
            "queries": len(incident.evidence),
            "root_cause": incident.root_cause,
            "confidence": incident.confidence,
            "similar_incidents": incident.similar_incidents,
        }
        with (self.trace_dir / "investigations.jsonl").open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
