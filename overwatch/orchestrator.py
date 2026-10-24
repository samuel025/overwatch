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
from .config import Config, EvidenceConfig
from .events import EventBus
from .telemetry import DockerTelemetry
from .impact import check_starts, incident_start, measure, spread, timestamp_literal
from .listeners import LISTENER_TYPES
from .llm import LLM
from .memory import build_memory
from .models import Anomaly, Evidence, Incident, now_iso
from .notifiers import build_notifiers
from .changefeeds import build_feeds
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
        self.store = IncidentStore(cfg.state_url or cfg.state_path or self.trace_dir / "overwatch.db")
        self.own_data = self._own_data_target(cfg)
        self.sources = build_sources(self._with_builtin_sources(cfg))
        self.evidence = self._with_default_evidence(cfg)
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
        self._refreshes: dict[str, int] = {}  # incident id -> how often it was investigated again
        self._healthy_streak: dict[str, float] = {}  # check -> when its current healthy stretch began
        self._healed_at: dict[str, float] = {}  # check -> when it last failed after a sustained healthy stretch
        self._opened_precisely: dict[str, float] = {}  # incident id -> exact open time (the stored one is whole seconds)
        self.pool = ThreadPoolExecutor(cfg.max_parallel_investigations, thread_name_prefix="investigation")
        # instance -> {service, since, detail} for containers a pushed event says are down
        self.down: dict[str, dict[str, Any]] = {}
        self.listeners = [
            LISTENER_TYPES[e["type"]](e, self.on_container_event) for e in cfg.events if e.get("type") in LISTENER_TYPES
        ]
        self.listeners += build_feeds(cfg.changes, self._record_feed_change, self.sources,
                                      on_error=lambda msg: self.events.emit("source.error", message=msg))
        docker_cfg = self._docker_config(cfg)
        if self.own_data["dialect"] == "spark" and docker_cfg is not None:  # Zetaris cannot reach Docker: mirror it
            self.listeners.append(DockerTelemetry(self.store, docker_cfg, on_error=lambda m: self.events.emit("source.error", message=m)))
        self._restore()
        self._last_prune = 0.0
        self._register_own_data()

    @staticmethod
    def _docker_config(cfg: Config) -> dict[str, Any] | None:
        src = next((s for s in cfg.sources.values() if s.get("type") == "docker"), None)
        return {**(cfg.docker or {}), **(src or {})} if (src or cfg.docker) else None

    def _own_data_target(self, cfg: Config) -> dict[str, str]:
        """Where the Investigator reads Overwatch's own history: the source, the table prefix and the SQL dialect.

        SQLite file: read directly. Postgres (state_url): read directly, or through Zetaris when this deployment's
        data layer is Zetaris, so the history can be joined with the user's own databases in one query."""
        from .datalayer import zetaris_env

        if self.store.dialect == "sqlite":
            return {"source": "overwatch", "prefix": "", "dialect": "sqlite"}
        if zetaris_env() and any(s.get("type") == "zetaris" for s in cfg.sources.values()):
            zet = next(n for n, s in cfg.sources.items() if s.get("type") == "zetaris")
            return {"source": zet, "prefix": "overwatch.", "dialect": "spark"}
        return {"source": "overwatch", "prefix": "", "dialect": "postgresql"}

    def _register_own_data(self) -> None:
        """With Zetaris as the data layer, register the user's databases and Overwatch's own (safe to repeat)."""
        from .datalayer import register, zetaris_env

        databases = dict(self.cfg.register)
        if self.own_data["dialect"] == "spark":
            databases["overwatch"] = self.store.url
        if not databases or not zetaris_env():
            return
        try:
            register(databases)
        except Exception as e:  # Zetaris is down or unreachable: the console shows it, watching goes on
            self.events.emit("source.error", message=f"registering databases with Zetaris: {e}")

    def _with_builtin_sources(self, cfg: Config) -> dict[str, dict[str, Any]]:
        """Add the read-only `overwatch` change log (and describe the built-ins) so they work with no setup."""
        sources = dict(cfg.sources)
        notes = []
        own = getattr(self, "own_data", {"dialect": "sqlite", "prefix": "", "source": "overwatch"})
        if "overwatch" not in sources and own["dialect"] == "sqlite":
            path = Path(self.store.path).resolve()
            sources["overwatch"] = {"type": "sql", "url": f"sqlite:///file:{path}?mode=ro&uri=true"}
        elif "overwatch" not in sources and own["dialect"] == "postgresql":
            sources["overwatch"] = {"type": "postgres", "url": self.store.url}
        if own["dialect"] == "sqlite":
            where = "Time windows: WHERE ts > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 minutes')"
            label = "overwatch (SQLite SQL, built in)"
        elif own["dialect"] == "postgresql":
            where = "Time windows: WHERE ts::timestamptz > now() - interval '30 minutes'"
            label = "overwatch (PostgreSQL SQL, built in)"
        else:
            where = "Time windows: WHERE to_timestamp(ts) > current_timestamp() - INTERVAL 30 MINUTES"
            label = f"overwatch.<table> (in Zetaris, Spark SQL, built in; query source {own['source']})"
        p = own["prefix"]
        notes.append(
            f"{label}: {p}changes(ts TEXT ISO-8601 UTC, service, version, kind, author, description): every release/"
            f"config change reported to or learned by Overwatch. {p}check_samples(ts TEXT ISO-8601 UTC, check_name, "
            f"service, value, failing 0/1): every check's value on every pass for the last 7 days. {where}"
        )
        if any(s.get("type") == "docker" for s in sources.values()) and own["dialect"] == "spark":
            notes.append(
                f"Docker, as tables (recent, refreshed every ~10 s): {p}container_state(ts, service, container, state "
                f"running/exited, exit_code, oom_killed 0/1, restarts, health, started_at, finished_at): one row per "
                f"container per sample, so use the newest ts for 'now'. {p}container_events(ts, service, container, "
                f"action die/start/kill/stop/oom/restart, exit_code, detail): a `kill` row with a signal means someone "
                f"asked Docker to stop the container; a `die` with no preceding `kill` and a non-zero exit is a crash. "
                f"{p}container_logs(ts, service, container, line, important 0/1): recent log lines (important = errors, "
                f"warnings, shutdowns, restarts). All are in Zetaris and join with everything else."
            )
        elif any(s.get("type") == "docker" for s in sources.values()) and "docker:" not in cfg.catalog:
            notes.append(
                "docker (built in, commands not SQL): `containers` (state, exit_code, oom_killed, restarts, limits), "
                "`logs <service> [lines]`, `errors <service> [lines]`, `events [minutes]` (die/start/oom/kill)"
            )
        if any(s.get("type") == "kubernetes" for s in sources.values()) and "kubernetes:" not in cfg.catalog:
            notes.append(
                "kubernetes (built in, commands not SQL): `pods` (phase, restarts, waiting_reason, last_exit_reason "
                "e.g. OOMKilled, last_exit_code), `logs <service> [lines]`, `errors <service> [lines]`, "
                "`events [minutes]` (BackOff, OOMKilling, Unhealthy, FailedScheduling), `rollouts` (wanted vs ready "
                "replicas, image, generation)"
            )
        if notes:
            cfg.catalog = (cfg.catalog.rstrip() + "\n" + "\n".join(notes)).strip()
        return sources

    def _with_default_evidence(self, cfg: Config) -> list[EvidenceConfig]:
        evidence = list(cfg.evidence)
        names = {e.name for e in evidence}
        own = self.own_data
        window = {"sqlite": "strftime('%Y-%m-%dT%H:%M:%S', 'now', '-60 minutes')",
                  "postgresql": "now() - interval '60 minutes'",
                  "spark": "current_timestamp() - INTERVAL 60 MINUTES"}[own["dialect"]]
        column = {"sqlite": "ts", "postgresql": "ts::timestamptz", "spark": "to_timestamp(ts)"}[own["dialect"]]
        defaults = [EvidenceConfig(
            "reported_changes", "Changes reported to Overwatch in the last hour", own["source"],
            f"SELECT ts, service, version, kind, author, description FROM {own['prefix']}changes "
            f"WHERE {column} > {window} ORDER BY ts DESC LIMIT 20")]
        docker = next((n for n, s in cfg.sources.items() if s.get("type") == "docker"), None) or (
            "docker" if cfg.docker else None)
        if docker and own["dialect"] == "spark":
            p = own["prefix"]
            defaults += [
                EvidenceConfig("containers_now", "Container state now", own["source"],
                               "SELECT service, container, state, exit_code, oom_killed, restarts, health, started_at, "
                               "finished_at FROM (SELECT *, row_number() OVER (PARTITION BY container ORDER BY ts DESC) "
                               f"AS rn FROM {p}container_state WHERE to_timestamp(ts) > current_timestamp() - INTERVAL "
                               "10 MINUTES) WHERE rn = 1 ORDER BY container"),
                EvidenceConfig("container_events", "Container events in the last 30 minutes", own["source"],
                               f"SELECT ts, service, container, action, exit_code, detail FROM {p}container_events WHERE "
                               "to_timestamp(ts) > current_timestamp() - INTERVAL 30 MINUTES ORDER BY ts DESC LIMIT 40"),
                EvidenceConfig("container_log_highlights", "Important container log lines in the last 10 minutes",
                               own["source"],
                               f"SELECT ts, service, container, line FROM {p}container_logs WHERE important = 1 AND "
                               "to_timestamp(ts) > current_timestamp() - INTERVAL 10 MINUTES ORDER BY ts DESC LIMIT 40")]
        elif docker:
            defaults += [EvidenceConfig("containers_now", "Container state now", docker, "containers"),
                         EvidenceConfig("container_events", "Container events in the last 30 minutes", docker, "events 30")]
        kube = next((n for n, s in cfg.sources.items() if s.get("type") == "kubernetes"), None)
        if kube:
            defaults += [EvidenceConfig("pods_now", "Pod state now (restarts, OOM kills, waiting reasons)", kube, "pods"),
                         EvidenceConfig("kube_events", "Kubernetes events in the last 30 minutes", kube, "events 30"),
                         EvidenceConfig("rollouts_now", "Deployments: replicas wanted vs ready, image", kube, "rollouts")]
        cfg.evidence = evidence + [d for d in defaults if d.name not in names]
        return cfg.evidence

    def record_change(self, service: str, version: str = "", kind: str = "deploy", author: str = "",
                      description: str = "", ts: str | None = None, ref: str | None = None) -> dict:
        """A release or config change reported to Overwatch (CLI `overwatch change` or POST /api/changes)."""
        row = self.store.record_change(service, version, kind, author, description, ts, ref)
        if row is not None:
            self.events.emit("change.recorded", **{k: v for k, v in row.items() if k != "ref"})
        return row or {}

    def _record_feed_change(self, change: dict[str, Any]) -> bool:
        """Callback for change feeds: True when the change was new."""
        return bool(self.record_change(change["service"], change.get("version", ""), change.get("kind", "deploy"),
                                       change.get("author", ""), change.get("description", ""), change.get("ts"),
                                       change.get("ref")))

    def start_listeners(self) -> None:
        for listener in self.listeners:
            listener.start()

    # -- pipeline -----------------------------------------------------------------

    def check_once(self) -> Incident | None:
        """One Watcher pass. New problems open an incident that is investigated in the background."""
        anomalies = self.watcher.run()
        self.failing = {a.check for a in anomalies}
        self._note_streaks(time.time(), self.failing)
        self.last_check_at = now_iso()
        self._record_samples()
        self.events.emit("watch.tick", values=dict(self.watcher.last_values),
                         failing=[a.check for a in anomalies], suspected=sorted(self.watcher.suspected))
        self._track_recovery(self.failing)
        return self.ingest(anomalies)

    def _record_samples(self) -> None:
        """Keep every check's value (history survives restarts and is queryable). Never breaks watching."""
        try:
            services = {c.name: c.service for c in self.cfg.checks}
            self.store.record_samples(dict(self.watcher.last_values), services, self.failing)
            if time.time() - self._last_prune > 3600:
                self._last_prune = time.time()
                self.store.prune_samples()
        except Exception as e:
            self.events.emit("source.error", message=f"saving check history: {e}")

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
                self._reinvestigate(target)
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
                timer = threading.Timer(self.cfg.event_grace_seconds, still_down)
                timer.daemon = True  # a pending grace timer must not hold a stopping process open
                timer.start()
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
            self._opened_precisely[incident.id] = time.time()
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

        def run(item) -> Evidence:
            query = item.query.replace("{since}", timestamp_literal(since, self.sources[item.source]))
            self.events.emit("query.started", incident=incident.id, step=0, source=item.source, query=query,
                             purpose=item.label)
            ev = self.investigator._run_query({"source": item.source, "query": query, "purpose": item.label, "_pack": True})
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
        lines += self._suspect_lines(incident, started, check_starts(incident.anomalies, self.watcher.history, detected))
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

    def _suspect_lines(self, incident: Incident, started: float, starts: dict[str, float]) -> list[str]:
        """What changed or restarted before the problem began, ranked, with the timing facts (see suspects.py)."""
        from . import suspects as sus

        def iso(t: float) -> str:
            return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")

        found: list[sus.Suspect] = []
        try:
            cols = ("ts", "service", "version", "kind", "description")
            found += sus.from_changes([dict(zip(cols, r)) for r in self.store.rows(
                "SELECT ts, service, version, kind, description FROM changes ORDER BY ts DESC LIMIT 200")])
            labels = {e.label for e in self.evidence if e.suspects}
            for ev in incident.pack:
                if ev.note.removeprefix("[pack] ") in labels and not ev.error:
                    found += sus.from_changes(ev.rows)
            cutoff = iso(started - sus.WINDOW_SECONDS - 60)
            cols = ("ts", "service", "container", "action", "exit_code", "detail")
            events = [dict(zip(cols, r)) for r in self.store.rows(
                "SELECT ts, service, container, action, exit_code, detail FROM container_events WHERE ts > :c", c=cutoff)]
            docker = next((n for n, s in self.cfg.sources.items() if s.get("type") == "docker"), None)
            if not events and docker:  # direct mode: no telemetry tables, ask Docker
                events = self.sources[docker].query("events 60")
            found += sus.from_container_events(events)

            def failing_between(a: float, b: float) -> bool:
                return b > a and self.store.rows(
                    "SELECT count(*) FROM check_samples WHERE failing = 1 AND ts > :a AND ts < :b",
                    a=iso(a), b=iso(b))[0][0] > 0

            return sus.annotate(found, started, failing_between, starts)
        except Exception as e:  # suspects are a help, never a reason to fail an investigation
            self.events.emit("source.error", message=f"building the suspect list: {e}")
            return []

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
        for listener in self.listeners:
            if hasattr(listener, "stop"):
                listener.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        for source in self.sources.values():  # an open Zetaris (Java) connection keeps the process from exiting
            try:
                source.close()
            except Exception:
                pass

    # -- state --------------------------------------------------------------------

    def data_layer(self) -> dict[str, Any]:
        """What the Investigator's queries go through, for the console: Zetaris and everything it federates."""
        zet = next((n for n, s in self.cfg.sources.items() if s.get("type") == "zetaris"), None)
        if not zet:
            return {"backend": "direct", "federated": []}
        rest = self.cfg.sources[zet].get("rest") or {}
        federated = [f"{n} (database)" for n in self.cfg.register]
        federated += ["overwatch (history and telemetry)"] if self.own_data["dialect"] == "spark" else []
        federated += [f"{n} (via {'promql' if n == 'prometheus' else 'logql'})" for n in ("prometheus", "loki") if rest.get(n)]
        if self.own_data["dialect"] == "spark" and self._docker_config(self.cfg) is not None:
            federated.append("docker (containers, events, logs)")
        return {"backend": "zetaris", "source": zet, "federated": federated}

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
                "sources": [{"name": n, "language": s.language, "type": s.cfg.get("type", "sql")}
                            for n, s in self.sources.items()],
                "data_layer": self.data_layer(),
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

    def _note_streaks(self, now: float, failing: set[str]) -> None:
        """Remember when each check was last healthy for a sustained stretch before failing again. An incident that
        stays open (nobody approved it, or another of its checks never recovers) must not swallow a check that
        healed and then broke again: that is a new problem."""
        for c in self.cfg.checks:
            if c.name not in failing:
                self._healthy_streak.setdefault(c.name, now)
            else:
                began = self._healthy_streak.pop(c.name, None)
                if began is not None and now - began >= self.cfg.resolve_after_seconds:
                    self._healed_at[c.name] = now

    def _covered(self, a: Anomaly) -> bool:
        """Already part of an ongoing incident, or reopened too soon after one (flap protection)."""
        with self.lock:
            healed = self._healed_at.get(a.check, 0.0)
            if any(a.check in {x.check for x in i.anomalies}
                   and self._opened_precisely.get(i.id, float(_ts(i.opened_at))) > healed for i in self._active()):
                return True
            return time.time() - self.last_opened.get(a.check, 0) < self.cfg.cooldown_seconds

    def _merge_target(self) -> Incident | None:
        """The newest ongoing incident opened within the merge window: a cascade is one incident, not five."""
        with self.lock:
            recent = [i for i in self._active() if time.time() - _ts(i.opened_at) < self.cfg.merge_window_seconds]
        return max(recent, key=lambda i: i.opened_at) if recent else None

    MAX_REFRESHES = 2

    def _reinvestigate(self, incident: Incident) -> None:
        """New checks fired after a diagnosis was made but before anyone acted on it: the diagnosis (and the evidence
        behind it) predates them, so investigate again with fresh evidence. Limited, so flapping cannot loop."""
        with self.lock:
            if incident.status != "awaiting_approval" or self._refreshes.get(incident.id, 0) >= self.MAX_REFRESHES:
                return
            self._refreshes[incident.id] = self._refreshes.get(incident.id, 0) + 1
            incident.reasoning.append(
                "New checks fired after this diagnosis, so Overwatch is investigating again with fresh evidence. "
                f"The earlier conclusion was: {incident.root_cause[:300]}")
            incident.status, incident.root_cause, incident.confidence = "investigating", "", 0.0
            incident.recommended_actions, incident.evidence, incident.hypotheses = [], [], []
            incident.cited, incident.first_guess_verdict, incident.grounding, incident.hypothesis = [], {}, {}, {}
        self._save(incident)
        self.events.emit("incident.updated", incident=incident.to_dict())
        try:
            self.pool.submit(self._investigate_safely, incident)
        except RuntimeError:  # shutting down
            pass

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
