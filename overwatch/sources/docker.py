"""Docker as a data source: container state, recent logs and recent events, with no extra setup.

Queries are tiny commands instead of SQL:
  containers                    every container of the project: state, exit code, restarts, OOM, limits
  logs <service> [lines]        the last lines each container of that service printed (default 50)
  errors <service> [lines]      the same, keeping only lines that look like errors
  events [minutes]              container die/start/oom/kill/restart events (default 15 minutes)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..docker_api import COMPOSE_SERVICE, DockerAPI, DockerError
from .base import DataSource, SourceError

_ERRORISH = re.compile(r"error|exception|traceback|fatal|panic|refused|timeout|timed out|failed|critical| 5\d\d ", re.I)


class DockerSource(DataSource):
    language = "Docker commands (containers | logs <service> [lines] | errors <service> [lines] | events [minutes])"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        self.api = DockerAPI(cfg.get("socket", "/var/run/docker.sock"))
        self.project = cfg.get("project")
        self.service_label = cfg.get("service_label", COMPOSE_SERVICE)

    def _service_containers(self, service: str) -> list[dict[str, Any]]:
        found = [c for c in self.api.containers(self.project) if c["Labels"].get(self.service_label) == service]
        if not found:
            known = sorted({c["Labels"].get(self.service_label, "?") for c in self.api.containers(self.project)})
            raise SourceError(f"no containers for service '{service}'. Known services: {', '.join(known)}")
        return found

    def query(self, q: str) -> list[dict[str, Any]]:
        parts = q.strip().split()
        if not parts:
            raise SourceError("empty command")
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "containers":
                return self._containers()[: self.max_rows]
            if cmd in ("logs", "errors"):
                if not args:
                    raise SourceError(f"usage: {cmd} <service> [lines]")
                tail = int(args[1]) if len(args) > 1 and args[1].isdigit() else 50
                return self._logs(args[0], min(tail, 500), errors_only=cmd == "errors")[-self.max_rows :]
            if cmd == "events":
                minutes = int(args[0]) if args and args[0].isdigit() else 15
                return self._events(minutes)[-self.max_rows :]
        except DockerError as e:
            raise SourceError(str(e)) from e
        raise SourceError(f"unknown command '{cmd}'. Use: containers | logs <service> | errors <service> | events")

    def _containers(self) -> list[dict[str, Any]]:
        rows = []
        for c in self.api.containers(self.project):
            info = self.api.inspect(c["Id"])
            state, host = info["State"], info["HostConfig"]
            rows.append({
                "service": c["Labels"].get(self.service_label, ""),
                "container": c["Names"][0].lstrip("/"),
                "state": state["Status"],
                "exit_code": state.get("ExitCode"),
                "oom_killed": state.get("OOMKilled"),
                "restarts": info.get("RestartCount", 0),
                "started_at": state.get("StartedAt"),
                "finished_at": None if state.get("FinishedAt", "").startswith("0001") else state.get("FinishedAt"),
                "health": (state.get("Health") or {}).get("Status"),
                "cpu_limit": (host.get("NanoCpus") or 0) / 1e9 or None,
                "memory_limit_mb": (host.get("Memory") or 0) // 2**20 or None,
                "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
                "image": c.get("Image"),
            })
        return rows

    def _logs(self, service: str, tail: int, errors_only: bool) -> list[dict[str, Any]]:
        rows = []
        for c in self._service_containers(service):
            for ts, line in self.api.logs(c["Id"], tail=tail):
                if errors_only and not _ERRORISH.search(line):
                    continue
                rows.append({"ts": ts, "container": c["Names"][0].lstrip("/"), "line": line[:300]})
        return sorted(rows, key=lambda r: r["ts"])

    def _events(self, minutes: int) -> list[dict[str, Any]]:
        filters = {"label": [f"com.docker.compose.project={self.project}"]} if self.project else None
        rows = []
        for e in self.api.events(minutes * 60, filters):
            action = e.get("Action") or e.get("status") or ""
            if action not in ("die", "start", "oom", "kill", "restart", "stop", "health_status: unhealthy"):
                continue
            attrs = (e.get("Actor") or {}).get("Attributes") or {}
            rows.append({
                "ts": datetime.fromtimestamp(e.get("time", 0), timezone.utc).isoformat(),
                "service": attrs.get(self.service_label, ""),
                "container": attrs.get("name", ""),
                "action": action,
                "exit_code": attrs.get("exitCode"),
            })
        return rows

    def running(self, service: str) -> int:
        """Number of running containers for a service (used by `kind: container` checks)."""
        return sum(1 for c in self.api.containers(self.project, include_stopped=True)
                   if c["Labels"].get(self.service_label) == service and c["State"] == "running")
