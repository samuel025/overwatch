"""Kubernetes as a data source: pod state, logs, events and rollouts, straight from the API server.

Queries are tiny commands, like the Docker source:
  pods                              every pod: phase, restarts, last termination (OOMKilled, exit code), node
  logs <service> [lines]            the last lines each pod of that service printed (default 50)
  errors <service> [lines]          the same, keeping only lines that look like errors
  events [minutes]                  Warning/Normal events: BackOff, OOMKilling, FailedScheduling, Unhealthy...
  rollouts                          deployments: replicas wanted/ready, current generation, last update
  count <service[,service]|*> <minutes> <text>   error-like log lines in the window containing the text

A service is whatever the `service_label` label says (default app.kubernetes.io/name, falling back to app).
Runs in-cluster with the pod's service account, or from outside with `url` + `token`.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .base import DataSource, SourceError

_ERRORISH = re.compile(r"error|exception|traceback|fatal|panic|refused|timeout|timed out|failed|critical| 5\d\d ", re.I)
_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubernetesSource(DataSource):
    language = "Kubernetes commands (pods | logs <service> [lines] | errors <service> [lines] | events [minutes] "
    language += "| rollouts | count <service|*> <minutes> <text>)"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        self.namespace = cfg.get("namespace", "default")
        self.service_labels = [cfg["service_label"]] if cfg.get("service_label") else ["app.kubernetes.io/name", "app"]
        url, token, verify = cfg.get("url", ""), cfg.get("token", ""), cfg.get("verify", True)
        if not url and (_SA / "token").exists():  # inside a cluster
            url = f"https://{os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')}:" \
                  f"{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
            token = token or (_SA / "token").read_text().strip()
            verify = str(_SA / "ca.crt") if (_SA / "ca.crt").exists() else verify
            if cfg.get("namespace") is None and (_SA / "namespace").exists():
                self.namespace = (_SA / "namespace").read_text().strip()
        if not url:
            raise SourceError(f"source '{name}' needs a 'url' (API server) or to run inside a cluster")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=url.rstrip("/"), headers=headers, verify=verify,
                                   timeout=float(cfg.get("timeout_seconds", 10)))

    # -- API plumbing -------------------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> Any:
        try:
            r = self.client.get(path, params={k: v for k, v in params.items() if v is not None})
        except Exception as e:
            raise SourceError(f"kubernetes request failed: {e}") from e
        if r.status_code == 403:
            raise SourceError(f"forbidden: the service account cannot read {path} (needs get/list on pods, pods/log, "
                              "events and deployments)")
        if r.status_code >= 400:
            raise SourceError(f"kubernetes returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if "json" in r.headers.get("content-type", "") else r.text

    def service_of(self, pod: dict[str, Any]) -> str:
        labels = pod["metadata"].get("labels") or {}
        for key in self.service_labels:
            if labels.get(key):
                return labels[key]
        return ""

    def pods(self, service: str | None = None) -> list[dict[str, Any]]:
        items = self._get(f"/api/v1/namespaces/{self.namespace}/pods")["items"]
        return [p for p in items if service is None or self.service_of(p) == service]

    def _service_pods(self, service: str) -> list[dict[str, Any]]:
        found = self.pods(service)
        if not found:
            known = sorted({self.service_of(p) or "?" for p in self.pods()})
            raise SourceError(f"no pods for service '{service}' in namespace {self.namespace}. "
                              f"Known services: {', '.join(known)}")
        return found

    def _pod_logs(self, pod: dict[str, Any], tail: int, since: int | None = None) -> list[tuple[str, str]]:
        text = self._get(f"/api/v1/namespaces/{self.namespace}/pods/{pod['metadata']['name']}/log",
                         tailLines=tail, sinceSeconds=since, timestamps="true")
        out = []
        for line in str(text).splitlines():
            ts, _, msg = line.partition(" ")
            out.append((ts, msg))
        return out

    # -- queries ------------------------------------------------------------------------------

    def ping(self) -> None:
        self.pods()

    def query(self, q: str) -> list[dict[str, Any]]:
        parts = q.strip().split()
        if not parts:
            raise SourceError("empty command")
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "pods":
            return self._pods_table()[: self.max_rows]
        if cmd in ("logs", "errors"):
            if not args:
                raise SourceError(f"usage: {cmd} <service> [lines]")
            tail = int(args[1]) if len(args) > 1 and args[1].isdigit() else 50
            return self._logs(args[0], min(tail, 500), errors_only=cmd == "errors")[-self.max_rows:]
        if cmd == "events":
            return self._events(int(args[0]) if args and args[0].isdigit() else 15)[-self.max_rows:]
        if cmd == "rollouts":
            return self._rollouts()[: self.max_rows]
        if cmd == "count":
            if len(args) < 3 or not args[1].isdigit():
                raise SourceError("usage: count <service|*> <minutes> <text>")
            return [{"value": self._count(args[0], int(args[1]), " ".join(args[2:]))}]
        raise SourceError(f"unknown command '{cmd}'. Use: pods | logs <service> | errors <service> | events | rollouts")

    def _pods_table(self) -> list[dict[str, Any]]:
        rows = []
        for p in self.pods():
            status = p.get("status", {})
            cs = (status.get("containerStatuses") or [{}])[0]
            last = (cs.get("lastState") or {}).get("terminated") or {}
            waiting = (cs.get("state") or {}).get("waiting") or {}
            rows.append({
                "service": self.service_of(p), "pod": p["metadata"]["name"], "phase": status.get("phase"),
                "ready": cs.get("ready"), "restarts": cs.get("restartCount", 0),
                "waiting_reason": waiting.get("reason"), "last_exit_reason": last.get("reason"),
                "last_exit_code": last.get("exitCode"), "last_exit_at": last.get("finishedAt"),
                "started_at": status.get("startTime"), "node": p.get("spec", {}).get("nodeName"),
                "image": (p.get("spec", {}).get("containers") or [{}])[0].get("image"),
            })
        return rows

    def _logs(self, service: str, tail: int, errors_only: bool) -> list[dict[str, Any]]:
        rows = []
        for p in self._service_pods(service):
            for ts, line in self._pod_logs(p, tail):
                if errors_only and not _ERRORISH.search(line):
                    continue
                rows.append({"ts": ts, "pod": p["metadata"]["name"], "line": line[:300]})
        return sorted(rows, key=lambda r: r["ts"])

    def _count(self, services: str, minutes: int, text: str) -> int:
        pods = self.pods() if services == "*" else [p for s in services.split(",") if s for p in self._service_pods(s)]
        needle, total = text.lower(), 0
        for p in pods:
            if p.get("status", {}).get("phase") != "Running":
                continue
            try:  # bounded read: a check runs every pass
                lines = self._pod_logs(p, 2000, since=minutes * 60)
            except SourceError:
                continue
            total += sum(1 for _, line in lines if needle in line.lower() and _ERRORISH.search(line))
        return total

    def _events(self, minutes: int) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        rows = []
        for e in self._get(f"/api/v1/namespaces/{self.namespace}/events")["items"]:
            when = e.get("lastTimestamp") or e.get("eventTime") or e["metadata"].get("creationTimestamp")
            if not when or datetime.fromisoformat(when.replace("Z", "+00:00")) < cutoff:
                continue
            obj = e.get("involvedObject", {})
            rows.append({"ts": when, "type": e.get("type"), "reason": e.get("reason"), "object": f"{obj.get('kind')}/{obj.get('name')}",
                         "count": e.get("count", 1), "message": (e.get("message") or "")[:200]})
        return sorted(rows, key=lambda r: r["ts"])

    def _rollouts(self) -> list[dict[str, Any]]:
        rows = []
        for d in self._get(f"/apis/apps/v1/namespaces/{self.namespace}/deployments")["items"]:
            spec, st = d.get("spec", {}), d.get("status", {})
            cond = next((c for c in st.get("conditions", []) if c.get("type") == "Progressing"), {})
            rows.append({
                "deployment": d["metadata"]["name"], "wanted": spec.get("replicas"), "ready": st.get("readyReplicas", 0),
                "updated": st.get("updatedReplicas", 0), "generation": d["metadata"].get("generation"),
                "observed_generation": st.get("observedGeneration"), "image": (spec.get("template", {}).get("spec", {})
                .get("containers") or [{}])[0].get("image"), "progress": cond.get("reason"), "last_update": cond.get("lastUpdateTime"),
            })
        return rows

    def running(self, service: str) -> int:
        """Ready pods for a service (used by `kind: container` checks)."""
        return sum(1 for p in self.pods(service) if p.get("status", {}).get("phase") == "Running"
                   and all(c.get("ready") for c in p["status"].get("containerStatuses") or [{"ready": True}]))

    def close(self) -> None:
        self.client.close()
