"""Discover services, dependencies and liveness checks from a Kubernetes namespace (read-only)."""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..sources.base import SourceError
from ..sources.kubernetes import _ERRORISH, KubernetesSource
from .docker import DATABASE_IMAGES, _what
from .externals import hosts_in_env, hosts_in_text

_HOSTS = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^\s/@]*@)?([a-z0-9][a-z0-9.-]*)", re.I)


def discover_kubernetes(url: str = "", token: str = "", namespace: str = "", service_label: str = "",
                        name: str = "k8s", transport: httpx.BaseTransport | None = None,
                        verify: bool | str = True) -> dict[str, Any]:
    """`verify`: True (system CAs), a path to the cluster's CA file, or False (skip certificate checks)."""
    cfg: dict[str, Any] = {k: v for k, v in {"url": url, "token": token, "namespace": namespace or None,
                                              "service_label": service_label}.items() if v}
    cfg["verify"] = verify
    src = KubernetesSource(name, cfg)
    if transport is not None:
        src.client = httpx.Client(base_url=src.client.base_url, transport=transport)
    try:
        pods = src.pods()
    except SourceError as e:
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            msg += (". The cluster's certificate is not signed by a CA this machine trusts (normal for local, on-prem and "
                    "many managed clusters). Give Overwatch the cluster's CA file, or switch off certificate verification.")
        raise RuntimeError(msg) from e
    if not pods:
        raise RuntimeError(f"no pods found in namespace '{src.namespace}'")

    services: dict[str, dict[str, Any]] = {}
    envs: dict[str, list[str]] = {}
    env_hits: dict[str, dict[str, str]] = {}
    log_hits: dict[str, dict[str, int]] = {}
    internal: set[str] = {src.namespace}
    for p in pods:
        svc = src.service_of(p)
        if not svc:
            continue
        containers = p.get("spec", {}).get("containers") or [{}]
        image = containers[0].get("image", "")
        entry = services.setdefault(svc, {"image": image, "instances": 0, "databases": False, "running": False,
                                          "cpu": None, "memory": None, "readiness": None})
        entry["instances"] += 1
        entry["running"] = entry["running"] or p.get("status", {}).get("phase") == "Running"
        entry["databases"] = entry["databases"] or any(d in image.lower() for d in DATABASE_IMAGES)
        limits = (containers[0].get("resources") or {}).get("limits") or {}
        entry["cpu"], entry["memory"] = limits.get("cpu") or entry["cpu"], limits.get("memory") or entry["memory"]
        probe = (containers[0].get("readinessProbe") or {}).get("httpGet") or {}
        entry["readiness"] = entry["readiness"] or (probe.get("path") and f"{probe['path']} on port {probe.get('port')}")
        internal |= {svc, p["metadata"]["name"]}
        values = [f"{e['name']}={e['value']}" for c in containers for e in c.get("env") or [] if e.get("value")]
        envs.setdefault(svc, []).extend(values)  # literal values only; secretKeyRef values are never read
        env_hits.setdefault(svc, {}).update(hosts_in_env(values))
        if p.get("status", {}).get("phase") == "Running":
            try:
                errors = [line for _, line in src._pod_logs(p, 300) if _ERRORISH.search(line)]
                for h, n in hosts_in_text(errors).items():
                    log_hits.setdefault(svc, {})[h] = log_hits.get(svc, {}).get(h, 0) + n
            except SourceError:
                pass

    # Dependencies: an environment value that names another service ("http://orders:8000", "orders.shop.svc").
    topology: dict[str, set[str]] = {}
    for svc, values in envs.items():
        for value in values:
            for host in _HOSTS.findall(value):
                first = host.lower().split(".")[0]
                if first in services and first != svc:
                    topology.setdefault(svc, set()).add(first)

    checks, knowledge = [], {}
    for svc, e in services.items():
        key = svc.replace("-", "_").replace(".", "_")
        checks.append({"name": f"{key}_running", "kind": "container", "service": svc, "source": name,
                       "description": f"{svc} has a ready pod", "severity": "critical",
                       "why": "liveness from Kubernetes: fires when no pod of the service is running and ready"})
        runs = [f"{e['instances']} pod{'s' if e['instances'] > 1 else ''} in namespace {src.namespace}"]
        runs += [f"limits {e['cpu']} CPU"] if e["cpu"] else []
        runs += [f"{e['memory']} memory"] if e["memory"] else []
        runs += [f"readiness probe {e['readiness']}"] if e["readiness"] else []
        knowledge[svc] = {"what": _what(e["image"]) or "TODO: describe what this service does",
                          "runs_on": ", ".join(runs), "image": e["image"]}

    runtime_facts = (
        f"Runs on Kubernetes (namespace {src.namespace}). Pods are restarted by their controller after a crash, "
        "so a service with restarts is crash-looping, not stopped.\n"
        "Container exit codes: 137 with reason OOMKilled = out of memory (memory limit exceeded); 143 = SIGTERM "
        "(rollout, eviction or scale-down); 1/other = application crash. CrashLoopBackOff means repeated crashes "
        "with growing delays. A readiness probe failing removes the pod from service traffic without restarting it.\n"
        "Rollouts replace pods gradually: during one, fewer pods than wanted may be ready for a short time."
    )
    return {
        "name": name, "namespace": src.namespace, "services": knowledge,
        "topology": {k: sorted(v) for k, v in topology.items()}, "checks": checks, "runtime_facts": runtime_facts,
        "notes": [], "env_hits": env_hits, "log_hits": log_hits,
        "internal": sorted(h.lower() for h in internal if h),
        "source": {**{k: v for k, v in {"type": "kubernetes", "url": url, "token": token, "namespace": src.namespace,
                                        "service_label": service_label}.items() if v},
                   **({"verify": verify} if verify is not True else {})},
    }
