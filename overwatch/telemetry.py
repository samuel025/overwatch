"""Docker telemetry as tables: container state, events and log lines, copied into Overwatch's history database.

Docker speaks a socket Zetaris cannot reach, and logs are not tables. So when Zetaris is the data layer, Overwatch
keeps a bounded, recent copy of what Docker shows in its own database (registered in Zetaris like any other), and
the Investigator asks Zetaris about containers, events and logs with ordinary SQL, joinable with the user's
databases and Overwatch's change log. Retention is short on purpose: this is evidence for incidents, not a log store.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .docker_api import DockerAPI, DockerError
from .store import IncidentStore

_IMPORTANT = re.compile(r"error|exception|traceback|fatal|panic|refused|timeout|timed out|failed|critical|warn|"
                        r"shutdown|shut down|terminat|killed|signal|oom|exited|restart|started|ready to accept", re.I)
MAX_LINES_PER_CYCLE = 400


def iso(docker_ts: str) -> str:
    """Docker's RFC3339 nanosecond timestamps -> ISO-8601 with microseconds (what Spark's to_timestamp reads)."""
    if not docker_ts:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?", docker_ts)
    if not m:
        return docker_ts
    return f"{m.group(1)}.{(m.group(2) or '0').ljust(6, '0')[:6]}+00:00"


class DockerTelemetry:
    def __init__(self, store: IncidentStore, cfg: dict[str, Any], interval: float = 10,
                 on_error: Callable[[str], None] = print):
        self.store = store
        self.api = DockerAPI(cfg.get("socket", "/var/run/docker.sock"))
        self.project = cfg.get("project")
        self.service_label = cfg.get("service_label", "com.docker.compose.service")
        self.interval = interval
        self.on_error = on_error
        self._stop = threading.Event()
        self._last_line: dict[str, str] = {}  # container -> newest log timestamp already stored
        self._seen_events: set[tuple] = set()
        self._last_prune = 0.0

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="docker-telemetry").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect()
            except Exception as e:  # Docker restarting, a container vanishing mid-read: never stop collecting
                self.on_error(f"docker telemetry: {e}")
            self._stop.wait(self.interval)

    def collect(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        state, logs = [], []
        for c in self.api.containers(self.project):
            try:
                info = self.api.inspect(c["Id"])
            except DockerError:
                continue
            st = info["State"]
            name = c["Names"][0].lstrip("/")
            service = c["Labels"].get(self.service_label, "")
            state.append({
                "ts": now, "service": service, "container": name, "state": st["Status"], "exit_code": st.get("ExitCode"),
                "oom_killed": int(bool(st.get("OOMKilled"))), "restarts": info.get("RestartCount", 0),
                # Docker keeps a stale health value on stopped containers: only a running one is meaningful
                "health": (st.get("Health") or {}).get("Status") if st["Status"] == "running" else None,
                "started_at": iso(st.get("StartedAt", "")),
                "finished_at": None if st.get("FinishedAt", "").startswith("0001") else iso(st.get("FinishedAt", "")),
            })
            logs += self._new_lines(c["Id"], name, service)
        self.store.add_container_state(state)
        self.store.add_container_events(self._new_events())
        self.store.add_container_logs(logs)
        if time.time() - self._last_prune > 300:
            self.store.prune_telemetry()
            self._last_prune = time.time()

    def _new_lines(self, cid: str, name: str, service: str) -> list[dict[str, Any]]:
        last = self._last_line.get(name)
        try:
            raw = self.api.logs(cid, tail=MAX_LINES_PER_CYCLE if last else 200,
                                since=None if not last else int(datetime.fromisoformat(last).timestamp()) - 1)
        except DockerError:
            return []
        rows = []
        for ts, line in raw[-MAX_LINES_PER_CYCLE:]:
            when = iso(ts)
            if not when or (last and when <= last):
                continue
            rows.append({"ts": when, "service": service, "container": name, "line": line[:500],
                         "important": int(bool(_IMPORTANT.search(line)))})
        if rows:
            self._last_line[name] = max(r["ts"] for r in rows)
        return rows

    def _new_events(self) -> list[dict[str, Any]]:
        filters = {"label": [f"com.docker.compose.project={self.project}"]} if self.project else None
        rows = []
        for e in self.api.events(max(int(self.interval) * 3, 30), filters):
            action = e.get("Action") or e.get("status") or ""
            key = (e.get("timeNano"), action, (e.get("Actor") or {}).get("ID"))
            if key in self._seen_events or not action.split(":")[0] in (
                    "die", "start", "oom", "kill", "restart", "stop", "health_status", "pause", "unpause"):
                continue
            self._seen_events.add(key)
            attrs = (e.get("Actor") or {}).get("Attributes") or {}
            detail = f"signal {attrs['signal']}" if attrs.get("signal") else ""
            rows.append({"ts": datetime.fromtimestamp(e.get("timeNano", 0) / 1e9, timezone.utc).isoformat(timespec="microseconds"),
                         "service": attrs.get(self.service_label, ""), "container": attrs.get("name", ""),
                         "action": action, "exit_code": attrs.get("exitCode"), "detail": detail})
        if len(self._seen_events) > 2000:
            self._seen_events = set(list(self._seen_events)[-1000:])
        return rows
